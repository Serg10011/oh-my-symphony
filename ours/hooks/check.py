#!/usr/bin/env python3
"""Mechanical checks for the night pipeline (TASK-K05).

Two modes share one set of checks:

    check.py gate [--config PATH]   agent.done_gate_command; exit 0 = pass,
                                    any other outcome refuses Done.
    check.py hook [--config PATH]   first line of hooks.after_run; acts only
                                    when the card is in Done / Human Review,
                                    rolls the card back on failure, always
                                    exits 0.

Checks, fast ones first (all fast violations are collected), the project
test command last and only when the fast checks passed:

    1. config   $SYMPHONY_WORKFLOW_DIR/gate.toml (never from the workspace)
    2. base     git merge-base HEAD <base_ref>
    3. forbidden paths changed relative to the base
    4. existing tests changed relative to the base
    5. card shape: required sections present and non-empty
    6. project tests: test_command via `bash -lc`, own process group

Standard library only, Python >= 3.11, POSIX. See ours/hooks/README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

DEFAULT_FORBIDDEN_PATHS = (
    "WORKFLOW.md",
    "gate.toml",
    "ours/**",
    ".github/**",
    ".git/**",
    ".env",
    ".env.*",
    "**/*.pem",
    "**/*.key",
)
DEFAULT_TEST_GLOBS = (
    "tests/**",
    "**/test_*.py",
    "**/*_test.py",
    "**/*.test.ts",
    "**/*.test.tsx",
    "**/*_test.go",
    "**/*.spec.*",
)
DEFAULT_REQUIRED_SECTIONS = ("## Wiki Updates", "## As-Is -> To-Be Report")
DEFAULT_TEST_TIMEOUT_S = 900
DEFAULT_MAX_CHECK_FAILURES = 3

CONFIG_NAME = "gate.toml"
BOARD_DIR = "kanban"
STATE_DIR = ".ours-state"
LOG_NAME = "check.log"
NOTE_HEADING = "Check Failure"

CHECKED_STATES = frozenset({"done", "human review"})
ROLLBACK_STATES = frozenset({"In Progress", "needs_human"})
STATE_RETRY = "In Progress"
STATE_ESCALATE = "needs_human"

ISSUE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
TAIL_BYTES = 4000
GIT_TIMEOUT_S = 120
SYMPHONY_TIMEOUT_S = 60
POLL_S = 0.2

# Repository-controlled git settings must not change what the checks see:
# an fsmonitor hook could report a dirty tree as clean.
GIT_SAFE_ARGS = ("-c", "core.fsmonitor=false", "-c", "core.quotePath=false")
GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")

_CONFIG_KEYS = frozenset(
    {
        "base_ref",
        "test_command",
        "test_timeout_s",
        "forbidden_paths",
        "test_globs",
        "allow_modified_tests",
        "required_sections",
        "max_check_failures",
        "symphony_bin",
    }
)


class CheckError(Exception):
    """A refusal with an operator-readable reason."""


class Orphaned(Exception):
    """The parent hook shell died (hooks.timeout_ms); stop without acting."""


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    base_ref: str
    test_command: str
    test_timeout_s: int = DEFAULT_TEST_TIMEOUT_S
    forbidden_paths: tuple[str, ...] = DEFAULT_FORBIDDEN_PATHS
    test_globs: tuple[str, ...] = DEFAULT_TEST_GLOBS
    allow_modified_tests: tuple[str, ...] = ()
    required_sections: tuple[str, ...] = DEFAULT_REQUIRED_SECTIONS
    max_check_failures: int = DEFAULT_MAX_CHECK_FAILURES
    symphony_bin: str | None = None


def _cfg_str(data: dict, key: str, *, required: bool) -> str | None:
    if key not in data:
        if required:
            raise CheckError(f"config: missing required key {key!r}")
        return None
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise CheckError(f"config: {key!r} must be a non-empty string")
    return value


def _cfg_int(data: dict, key: str, default: int) -> int:
    if key not in data:
        return default
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CheckError(f"config: {key!r} must be a positive integer")
    return value


def _cfg_list(data: dict, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise CheckError(f"config: {key!r} must be a list of non-empty strings")
    return tuple(value)


def load_config(path: Path) -> Config:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise CheckError(f"config: {path} not found") from None
    except OSError as exc:
        raise CheckError(f"config: cannot read {path}: {exc}") from None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise CheckError(f"config: {path} is not valid TOML: {exc}") from None
    unknown = sorted(set(data) - _CONFIG_KEYS)
    if unknown:
        # A typo such as `forbiden_paths` must not silently drop a check.
        raise CheckError(f"config: unknown key(s) {', '.join(unknown)}")
    base_ref = _cfg_str(data, "base_ref", required=True)
    assert base_ref is not None
    if base_ref.startswith("-"):
        raise CheckError("config: 'base_ref' must not start with '-'")
    test_command = _cfg_str(data, "test_command", required=True)
    assert test_command is not None
    sections = _cfg_list(data, "required_sections", DEFAULT_REQUIRED_SECTIONS)
    for heading in sections:
        if not re.match(r"^##\s+\S", heading):
            raise CheckError(f"config: required section {heading!r} must start with '## '")
    cfg = Config(
        base_ref=base_ref,
        test_command=test_command,
        test_timeout_s=_cfg_int(data, "test_timeout_s", DEFAULT_TEST_TIMEOUT_S),
        forbidden_paths=DEFAULT_FORBIDDEN_PATHS
        + _cfg_list(data, "forbidden_paths", ()),
        test_globs=_cfg_list(data, "test_globs", DEFAULT_TEST_GLOBS),
        allow_modified_tests=_cfg_list(data, "allow_modified_tests", ()),
        required_sections=sections,
        max_check_failures=_cfg_int(
            data, "max_check_failures", DEFAULT_MAX_CHECK_FAILURES
        ),
        symphony_bin=_cfg_str(data, "symphony_bin", required=False),
    )
    for pattern in (*cfg.forbidden_paths, *cfg.test_globs, *cfg.allow_modified_tests):
        compile_glob(pattern)
    return cfg


# ---------------------------------------------------------------------------
# globs: `*` and `?` stay inside one path segment, `**` spans any number of
# segments (zero included). Patterns are anchored at the repository root and
# matched case-insensitively (macOS file systems are case-insensitive).
# ---------------------------------------------------------------------------


def _segment_regex(segment: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(segment):
        char = segment[i]
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            start = i + 1
            if start < len(segment) and segment[start] in "!^":
                start += 1
            if start < len(segment) and segment[start] == "]":
                start += 1
            end = segment.find("]", start)
            if end == -1:
                out.append(re.escape(char))
            else:
                body = segment[i + 1 : end]
                negate = body[:1] in ("!", "^")
                if negate:
                    body = body[1:]
                body = body.replace("\\", "\\\\")
                out.append(f"[^/{body}]" if negate else f"[{body}]")
                i = end
        else:
            out.append(re.escape(char))
        i += 1
    return "".join(out)


def compile_glob(pattern: str) -> re.Pattern[str]:
    segments: list[str] = []
    for segment in pattern.replace("\\", "/").split("/"):
        if segment in ("", "."):
            continue
        if segment == "**" and segments and segments[-1] == "**":
            continue
        segments.append(segment)
    if not segments:
        raise CheckError(f"config: empty glob pattern {pattern!r}")
    regex = ""
    last = len(segments) - 1
    for index, segment in enumerate(segments):
        if segment == "**":
            if index == last:
                # `a/**` matches `a` itself and everything below it.
                regex = ".*" if index == 0 else regex[:-1] + "(?:/.*)?"
            else:
                regex += "(?:[^/]+/)*"
        else:
            regex += _segment_regex(segment)
            if index < last:
                regex += "/"
    return re.compile(regex, re.IGNORECASE | re.DOTALL)


def glob_match(path: str, pattern: str) -> bool:
    return compile_glob(pattern).fullmatch(path) is not None


def _matcher(patterns: tuple[str, ...]):
    compiled = [compile_glob(p) for p in patterns]
    return lambda path: any(rx.fullmatch(path) for rx in compiled)


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV_DROP}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    proc = subprocess.run(
        ["git", *GIT_SAFE_ARGS, *args],
        cwd=str(repo),
        input=stdin,
        stdin=None if stdin is not None else subprocess.DEVNULL,
        capture_output=True,
        timeout=GIT_TIMEOUT_S,
        env=_git_env(),
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise CheckError(f"git {args[0]} failed (exit {proc.returncode}): {detail[:300]}")
    return proc.stdout


def _z(output: bytes) -> list[str]:
    return [item.decode("utf-8", errors="surrogateescape") for item in output.split(b"\0") if item]


def resolve_base(workspace: Path, base_ref: str) -> tuple[Path, str]:
    """Return (repository top level, merge-base sha) or raise CheckError."""
    if shutil.which("git") is None:
        raise CheckError("base: git not found on PATH")
    try:
        top = git(workspace, "rev-parse", "--show-toplevel").decode().strip()
    except CheckError:
        raise CheckError(f"base: {workspace} is not a git working tree") from None
    repo = Path(top)
    try:
        git(repo, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    except CheckError:
        raise CheckError("base: repository has no HEAD commit") from None
    try:
        ref_sha = git(repo, "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}")
    except CheckError:
        raise CheckError(f"base: ref {base_ref!r} not found") from None
    try:
        merge_base = git(repo, "merge-base", "HEAD", ref_sha.decode().strip())
    except CheckError:
        raise CheckError(f"base: no merge-base between HEAD and {base_ref!r}") from None
    sha = merge_base.decode().strip()
    if not sha:
        raise CheckError(f"base: no merge-base between HEAD and {base_ref!r}")
    return repo, sha


def _tree(repo: Path, rev: str) -> dict[str, tuple[str, str]]:
    """path -> (mode, object id) for every blob/link in `rev`."""
    entries: dict[str, tuple[str, str]] = {}
    for item in _z(git(repo, "ls-tree", "-r", "-z", "--full-tree", rev)):
        meta, _, path = item.partition("\t")
        mode, kind, oid = meta.split()
        if kind == "blob":
            entries[path] = (mode, oid)
    return entries


def _index(repo: Path) -> dict[str, tuple[str, str] | None]:
    """path -> (mode, object id); None for unmerged entries."""
    entries: dict[str, tuple[str, str] | None] = {}
    for item in _z(git(repo, "ls-files", "-s", "-z")):
        meta, _, path = item.partition("\t")
        mode, oid, stage = meta.split()
        entries[path] = (mode, oid) if stage == "0" and path not in entries else None
    return entries


def _hash_files(repo: Path, paths: list[str]) -> dict[str, str]:
    """Object ids of the raw bytes on disk (no filters)."""
    if not paths:
        return {}
    out = git(
        repo,
        "hash-object",
        "--no-filters",
        "--stdin-paths",
        stdin="".join(f"{p}\n" for p in paths).encode("utf-8", errors="surrogateescape"),
    )
    oids = out.decode().split()
    if len(oids) != len(paths):
        raise CheckError("git hash-object returned an unexpected number of ids")
    return dict(zip(paths, oids))


def changed_paths(repo: Path, base: str, board_dir: str) -> set[str]:
    """Every path that differs from the base anywhere: committed, staged,
    unstaged, untracked; renames contribute both names (--no-renames)."""
    paths: set[str] = set()
    common = ("--name-only", "-z", "--no-renames", "--no-ext-diff", "--no-textconv")
    paths.update(_z(git(repo, "diff", *common, base, "HEAD")))
    paths.update(_z(git(repo, "diff", *common, "--cached", base)))
    paths.update(_z(git(repo, "diff", *common, base)))
    paths.update(_z(git(repo, "ls-files", "--others", "--exclude-standard", "-z")))
    # skip-worktree / assume-unchanged entries hide working-tree edits from
    # `git diff`; after_create sets skip-worktree on the board files only.
    hidden = []
    for item in _z(git(repo, "ls-files", "-v", "-z")):
        tag, _, path = item.partition(" ")
        if (tag == "S" or tag.islower()) and not _under(path, board_dir):
            hidden.append(path)
    if hidden:
        index = _index(repo)
        present = [p for p in hidden if (repo / p).is_file() and not (repo / p).is_symlink()]
        disk = _hash_files(repo, present)
        for path in hidden:
            entry = index.get(path)
            if entry is None or path not in disk or disk[path] != entry[1]:
                paths.add(path)
    return paths


def _under(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory + "/")


# ---------------------------------------------------------------------------
# checks 3-5
# ---------------------------------------------------------------------------


def forbidden_violations(changed: set[str], cfg: Config) -> list[str]:
    is_forbidden = _matcher(cfg.forbidden_paths)
    return [f"forbidden path changed: {_clean(p)}" for p in sorted(changed) if is_forbidden(p)]


def test_violations(repo: Path, base: str, cfg: Config) -> list[str]:
    is_test = _matcher(cfg.test_globs)
    is_allowed = _matcher(cfg.allow_modified_tests)
    base_tree = _tree(repo, base)
    guarded = {p: e for p, e in base_tree.items() if is_test(p) and not is_allowed(p)}
    if not guarded:
        return []
    head_tree = _tree(repo, "HEAD")
    index = _index(repo)
    regular = []
    for path, (mode, _) in guarded.items():
        disk = repo / path
        if mode != "120000" and disk.is_file() and not disk.is_symlink():
            regular.append(path)
    disk_oids = _hash_files(repo, regular)
    problems: list[str] = []
    for path in sorted(guarded):
        mode, oid = guarded[path]
        where = []
        if head_tree.get(path) != (mode, oid):
            where.append("deleted in HEAD" if path not in head_tree else "HEAD")
        if index.get(path) != (mode, oid):
            where.append("index")
        if not _disk_equal(repo, path, mode, oid, disk_oids):
            where.append("working tree")
        if where:
            problems.append(f"existing test changed: {_clean(path)} ({', '.join(where)})")
    return problems


def _disk_equal(repo: Path, path: str, mode: str, oid: str, disk_oids: dict[str, str]) -> bool:
    disk = repo / path
    if mode == "120000":
        if not disk.is_symlink():
            return False
        target = os.readlink(disk).encode("utf-8", errors="surrogateescape")
        return target == git(repo, "cat-file", "blob", oid)
    if disk.is_symlink() or not disk.is_file():
        return False
    if mode == "100755" and not os.access(disk, os.X_OK):
        return False
    return disk_oids.get(path) == oid


def split_card(text: str) -> tuple[list[str], str]:
    """(front matter lines, body) of a ticket file."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                return lines[1:index], "\n".join(lines[index + 1 :])
        return lines[1:], ""
    # Front matter without delimiters (the tracker heals this form): leading
    # `key: value` lines up to the first blank line.
    head: list[str] = []
    for line in lines:
        if not line.strip():
            break
        head.append(line)
    return head, text


def read_card_state(card: Path) -> str:
    front, _ = split_card(card.read_text(encoding="utf-8"))
    for line in front:
        match = re.match(r"^state\s*:\s*(.*?)\s*$", line)
        if not match:
            continue
        value = match.group(1)
        if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) >= 2:
            return value[1:-1].strip()
        return value.split(" #", 1)[0].strip()
    raise CheckError(f"card {card.name} has no state")


def section_nonempty(body: str, heading: str) -> bool:
    """Same rule as the core stage contracts (`_section_present_nonempty`)."""
    pattern = re.compile(
        r"^##\s+" + re.escape(heading[3:].strip()) + r"\s*(?::\s*(?P<inline>.*))?$",
        re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(body)
    if not match:
        return False
    inline = match.group("inline") or ""
    after = body[match.end() :]
    next_heading = re.search(r"^##\s+\S", after, re.MULTILINE)
    section = after if next_heading is None else after[: next_heading.start()]
    return bool(inline.strip() or section.strip())


def card_violations(card: Path, cfg: Config) -> list[str]:
    try:
        text = card.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [f"card not found: {BOARD_DIR}/{card.name}"]
    except (OSError, UnicodeDecodeError) as exc:
        return [f"card unreadable: {BOARD_DIR}/{card.name}: {exc}"]
    _, body = split_card(text)
    return [
        f"card section missing or empty: {heading}"
        for heading in cfg.required_sections
        if not section_nonempty(body, heading)
    ]


# ---------------------------------------------------------------------------
# check 6: project tests
# ---------------------------------------------------------------------------


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    if proc.returncode is None:
        proc.wait()


def run_tests(workspace: Path, cfg: Config, parent_pid: int) -> tuple[str | None, str]:
    """Run test_command; return (failure reason or None, output tail)."""
    bash = shutil.which("bash") or "/bin/bash"
    with tempfile.TemporaryFile() as out:
        # Output goes to a file, not a pipe: a straggler holding the pipe
        # could otherwise turn a passing run into a timeout.
        proc = subprocess.Popen(
            [bash, "-lc", cfg.test_command],
            cwd=str(workspace),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + cfg.test_timeout_s
        timed_out = orphaned = False
        try:
            while proc.poll() is None:
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                if os.getppid() != parent_pid:
                    orphaned = True
                    break
                time.sleep(POLL_S)
        finally:
            # Always kill the whole group: on timeout, and for leftovers of
            # a finished run.
            _kill_group(proc)
        size = out.seek(0, os.SEEK_END)
        out.seek(max(0, size - TAIL_BYTES))
        tail = out.read().decode("utf-8", errors="replace")
    if orphaned:
        raise Orphaned("parent hook shell died while tests were running")
    if timed_out:
        return f"test_command timed out after {cfg.test_timeout_s}s", tail
    if proc.returncode == 0:
        return None, tail
    if proc.returncode == 127:
        return "test_command: command not found (exit 127)", tail
    return f"test_command failed (exit {proc.returncode})", tail


# ---------------------------------------------------------------------------
# orchestration of the checks
# ---------------------------------------------------------------------------


@dataclass
class Context:
    mode: str
    workflow_dir: Path
    workspace: Path
    issue_id: str
    config_path: Path
    parent_pid: int = field(default_factory=os.getppid)

    @property
    def card(self) -> Path:
        return self.workflow_dir / BOARD_DIR / f"{self.issue_id}.md"

    @property
    def state_dir(self) -> Path:
        return self.workflow_dir / STATE_DIR


@dataclass
class Result:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    tail: str = ""
    cfg: Config | None = None


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def validate_issue_id(value: str) -> str:
    if not ISSUE_ID_RE.match(value) or value.startswith("-") or not value.strip("."):
        raise CheckError(f"invalid issue id {value!r}")
    return value


def workflow_dir_from_env() -> Path:
    raw = os.environ.get("SYMPHONY_WORKFLOW_DIR", "").strip()
    if not raw:
        raise CheckError("SYMPHONY_WORKFLOW_DIR is not set")
    path = Path(raw).resolve()
    if not path.is_dir():
        raise CheckError(f"SYMPHONY_WORKFLOW_DIR {path} is not a directory")
    return path


def build_context(mode: str, config_arg: str | None) -> Context:
    workflow_dir = workflow_dir_from_env()
    workspace = Path.cwd().resolve()
    config_path = (Path(config_arg) if config_arg else workflow_dir / CONFIG_NAME).resolve()
    if _inside(config_path, workspace):
        raise CheckError(f"config {config_path} is inside the agent workspace; refusing")
    # SYMPHONY_ISSUE_TITLE is agent-controlled and deliberately never read.
    issue_id = validate_issue_id(os.environ.get("SYMPHONY_ISSUE_ID") or workspace.name)
    return Context(mode, workflow_dir, workspace, issue_id, config_path)


def run_checks(ctx: Context) -> Result:
    cfg = load_config(ctx.config_path)
    reasons: list[str] = []
    try:
        repo, base = resolve_base(ctx.workspace, cfg.base_ref)
    except CheckError as exc:
        reasons.append(str(exc))
    else:
        board_dir = os.environ.get("SYMPHONY_BOARD_ROOT_NAME") or BOARD_DIR
        reasons += forbidden_violations(changed_paths(repo, base, board_dir), cfg)
        reasons += test_violations(repo, base, cfg)
    reasons += card_violations(ctx.card, cfg)
    if reasons:
        return Result(False, reasons, "", cfg)
    failure, tail = run_tests(ctx.workspace, cfg, ctx.parent_pid)
    if failure is not None:
        return Result(False, [failure], tail, cfg)
    return Result(True, [], tail, cfg)


# ---------------------------------------------------------------------------
# rollback, note, counter, log (hook mode)
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(text: str) -> str:
    """One line, control characters escaped (paths come from the agent)."""
    return "".join(ch if ch.isprintable() else repr(ch)[1:-1] for ch in text)


def ensure_state_dir(state_dir: Path) -> None:
    state_dir.mkdir(exist_ok=True)  # inside an existing workflow dir only
    ignore = state_dir / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".tmp-ours-check-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def log_event(
    ctx_or_dir: Context | Path, issue_id: str, mode: str, outcome: str, reason: str, **extra: str
) -> None:
    state_dir = ctx_or_dir.state_dir if isinstance(ctx_or_dir, Context) else ctx_or_dir
    record = {
        "at": _now(),
        "id": issue_id,
        "mode": mode,
        "outcome": outcome,
        "reason": _clean(reason)[:2000],
        **extra,
    }
    try:
        ensure_state_dir(state_dir)
        with open(state_dir / LOG_NAME, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        _emit(f"check.py: cannot write log: {exc}", stream=sys.stderr)


def bump_counter(ctx: Context, reason: str) -> int:
    ensure_state_dir(ctx.state_dir)
    path = ctx.state_dir / f"{ctx.issue_id}.json"
    failures = 0
    try:
        failures = int(json.loads(path.read_text(encoding="utf-8")).get("failures", 0))
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError, TypeError):
        failures = 0  # a damaged counter is rewritten below
    failures += 1
    payload = {"failures": failures, "last_reason": _clean(reason)[:2000], "last_at": _now()}
    _write_atomic(path, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode())
    return failures


def resolve_symphony_bin(cfg: Config | None) -> str | None:
    if cfg is not None and cfg.symphony_bin:
        candidate = Path(cfg.symphony_bin).expanduser()
    else:
        python = os.environ.get("SYMPHONY_PYTHON", "").strip()
        if not python:
            return None
        candidate = Path(python).parent / "symphony"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def render_note(
    result: Result, *, mode: str, failures: int, limit: int, target: str, moved: bool
) -> str:
    lines = [
        f"- at: {_now()}",
        f"- check: ours/hooks/check.py {mode}",
        f"- failures: {failures} of {limit}",
        f"- moved to: {target}" if moved else f"- rollback to {target} FAILED (see check.log)",
        "",
        "Reasons:",
        "",
    ]
    lines += [f"- {_clean(reason)}" for reason in result.reasons]
    if result.tail.strip():
        # Indented block, not a fence: a `## ...` line in the test output
        # must never start at column 0, or it would read as a card section.
        lines += ["", "Test output (tail):", ""]
        lines += ["    " + line for line in result.tail.rstrip().splitlines()]
    return "\n".join(lines)


_APPEND_NOTE_CODE = """\
import sys
from pathlib import Path
from symphony.issue import Issue
from symphony.trackers.file import FileBoardTracker
from symphony.workflow import TrackerConfig
root, ident, heading, body = sys.argv[1:5]
tracker = FileBoardTracker(TrackerConfig(
    kind="file", endpoint="", api_key="", project_slug="",
    active_states=("Todo", "In Progress"), terminal_states=("Done", "Cancelled"),
    board_root=Path(root).resolve(),
))
tracker.append_note(
    Issue(id=ident, identifier=ident, title="", description=None, priority=None, state=""),
    heading, body,
)
"""


def append_note(ctx: Context, body: str) -> str:
    """Append `## Check Failure` through the tracker API; file fallback."""
    python = os.environ.get("SYMPHONY_PYTHON", "").strip()
    board = ctx.workflow_dir / BOARD_DIR
    if python and os.access(python, os.X_OK):
        try:
            proc = subprocess.run(
                # -P: never put the cwd on sys.path (the agent controls it).
                [python, "-P", "-c", _APPEND_NOTE_CODE, str(board), ctx.issue_id, NOTE_HEADING, body],
                cwd=str(ctx.workflow_dir),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=SYMPHONY_TIMEOUT_S,
                check=False,
            )
            if proc.returncode == 0:
                return "tracker"
        except (OSError, subprocess.TimeoutExpired):
            pass
    _append_note_file(ctx.card, board, ctx.issue_id, body)
    return "file"


def _append_note_file(card: Path, board: Path, issue_id: str, body: str) -> None:
    lock_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", issue_id).strip("._") or "ticket"
    lock_path = board / ".locks" / f"{lock_name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock:
        # Same per-ticket lock file as the file tracker's mutations.
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        text = card.read_text(encoding="utf-8")
        new = f"{text.rstrip()}\n\n## {NOTE_HEADING}\n\n{body.strip()}\n"
        _write_atomic(card, new.encode("utf-8"))


def rollback(ctx: Context, result: Result) -> None:
    limit = result.cfg.max_check_failures if result.cfg else DEFAULT_MAX_CHECK_FAILURES
    reason = "; ".join(result.reasons)
    failures = bump_counter(ctx, reason)
    target = STATE_ESCALATE if failures >= limit else STATE_RETRY
    if target not in ROLLBACK_STATES:  # pragma: no cover - defensive
        raise CheckError(f"refusing rollback target {target!r}")
    errors: list[str] = []
    binary = resolve_symphony_bin(result.cfg)
    if binary is None:
        errors.append("symphony binary not found (symphony_bin / SYMPHONY_PYTHON); rollback impossible")
    else:
        board = ctx.workflow_dir / BOARD_DIR
        try:
            proc = subprocess.run(
                [binary, "board", "mv", ctx.issue_id, target, "--root", str(board)],
                cwd=str(ctx.workflow_dir),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=SYMPHONY_TIMEOUT_S,
                check=False,
            )
            detail = (proc.stdout + proc.stderr).decode("utf-8", errors="replace").strip()
            rc = proc.returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            detail, rc = f"{type(exc).__name__}: {exc}", None
        try:
            now_state = read_card_state(ctx.card)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            now_state = f"<unreadable: {exc}>"
        if now_state.lower() != target.lower():
            errors.append(
                f"board mv to {target!r} did not take effect (exit {rc}, state now "
                f"{now_state!r}): {detail[:300]}"
            )
    note = render_note(
        result, mode=ctx.mode, failures=failures, limit=limit, target=target, moved=not errors
    )
    try:
        note_via = append_note(ctx, note)
    except Exception as exc:  # noqa: BLE001
        note_via = "failed"
        errors.append(f"cannot append note: {type(exc).__name__}: {exc}")
    log_event(ctx, ctx.issue_id, ctx.mode, f"rollback:{target}", reason, note=note_via)
    for error in errors:
        log_event(ctx, ctx.issue_id, ctx.mode, "error", error)
        _emit(f"check.py hook: {error}", stream=sys.stderr)


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------


def _emit(text: str, *, stream=None) -> None:
    try:
        print(text, file=stream or sys.stdout, flush=True)
    except (OSError, ValueError):
        pass  # the reader is gone (hook timed out); nothing to tell


def _format_report(mode: str, issue_id: str, result: Result) -> str:
    head = f"check.py {mode}: {'PASS' if result.ok else 'FAIL'} ({issue_id})"
    lines = [head] + [f"- {_clean(reason)}" for reason in result.reasons]
    if not result.ok and result.tail.strip():
        lines += ["--- test output (tail) ---", result.tail.rstrip()]
    return "\n".join(lines)


def gate_main(config_arg: str | None) -> int:
    ctx: Context | None = None
    try:
        ctx = build_context("gate", config_arg)
        result = run_checks(ctx)
    except CheckError as exc:
        result = Result(False, [str(exc)])
    except Exception as exc:  # noqa: BLE001 - fail closed
        result = Result(False, [f"internal error: {type(exc).__name__}: {exc}"],
                        traceback.format_exc()[-TAIL_BYTES:])
    issue_id = ctx.issue_id if ctx else "?"
    _emit(_format_report("gate", issue_id, result))
    if ctx is not None:
        log_event(ctx, issue_id, "gate", "pass" if result.ok else "fail", "; ".join(result.reasons))
    return 0 if result.ok else 1


def hook_main(config_arg: str | None) -> int:
    try:
        ctx = build_context("hook", config_arg)
    except CheckError as exc:
        _emit(f"check.py hook: {exc}", stream=sys.stderr)
        try:
            log_event(workflow_dir_from_env() / STATE_DIR, "?", "hook", "error", str(exc))
        except CheckError:
            pass
        return 0
    try:
        state = read_card_state(ctx.card)
    except Exception as exc:  # noqa: BLE001 - nothing to roll back without a state
        log_event(ctx, ctx.issue_id, "hook", "error", f"cannot read card state: {exc}")
        return 0
    if state.lower() not in CHECKED_STATES:
        return 0
    try:
        result = run_checks(ctx)
    except Orphaned as exc:
        log_event(ctx, ctx.issue_id, "hook", "skipped",
                  f"{exc}; card left as is, the done gate decides")
        return 0
    except CheckError as exc:
        result = Result(False, [str(exc)])
    except Exception as exc:  # noqa: BLE001 - fail closed: roll back
        result = Result(False, [f"internal error: {type(exc).__name__}: {exc}"],
                        traceback.format_exc()[-TAIL_BYTES:])
        log_event(ctx, ctx.issue_id, "hook", "error", traceback.format_exc())
        try:
            # Keep symphony_bin / max_check_failures for the rollback.
            result.cfg = load_config(ctx.config_path)
        except Exception:  # noqa: BLE001
            pass
    if result.ok:
        log_event(ctx, ctx.issue_id, "hook", "pass", "")
        _emit(_format_report("hook", ctx.issue_id, result))
        return 0
    if os.getppid() != ctx.parent_pid:
        log_event(ctx, ctx.issue_id, "hook", "skipped",
                  "parent hook shell died; card left as is, the done gate decides")
        return 0
    if not ctx.workspace.is_dir():
        # Reconcile reaps a terminal card's workspace 60 s after it first
        # sees the state, even while this hook runs: not the agent's failure.
        log_event(ctx, ctx.issue_id, "hook", "skipped",
                  "workspace removed during the check (reconcile?); card left as is; "
                  + "; ".join(result.reasons))
        return 0
    _emit(_format_report("hook", ctx.issue_id, result))
    rollback(ctx, result)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check.py", description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("gate", "hook"))
    parser.add_argument("--config", default=None, help="path to gate.toml (outside the workspace)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if argv and "-h" in argv else 2
    if args.mode == "gate":
        return gate_main(args.config)
    try:
        return hook_main(args.config)
    except BaseException as exc:  # noqa: BLE001 - the hook never fails the turn
        _emit(f"check.py hook: unexpected {type(exc).__name__}: {exc}", stream=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
