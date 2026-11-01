#!/usr/bin/env python3
"""Mechanical checks for the night pipeline (TASK-K05, TASK-K08a, TASK-K08b).

Two modes share one set of checks:

    check.py gate   agent.done_gate_command; exit 0 = pass, any other
                    outcome refuses Done.
    check.py hook   first line of hooks.after_run; acts only when the card
                    is in Done / Human Review, rolls the card back on
                    failure, always exits 0.

$SYMPHONY_WORKFLOW_DIR is the project board folder (ours/hooks/
project_config.py). Checks, fast ones first (all fast violations are
collected), the project tests last and only when the fast checks passed:

    1. config   <board>/project.toml: [project], [tools], [checks],
                [timeouts]; <board>/.ours-state/baselines.json (project-doctor;
                S4-25: the history of measures, else baseline.json)
    2. base     <board>/.ours-state/<ID>.base.json, written by worktree.py
                when the worktree was created; must be an ancestor of HEAD
    3. forbidden paths changed relative to the base; an Anthropic API key
       (`sk-ant-…`) in an added line of any change (E1b: the refusal names
       path:line, never the line)
    4. existing tests changed relative to the base
    5. card shape: required sections present and non-empty (a subtask,
       see integrate.is_subtask, needs [checks] worker_required_sections:
       Verify writes those, its card has no Document stage)
    6. project tests: `/bin/bash <board>/test.sh`, own process group; exit 0
       and the last `PIPELINE_TESTS passed=.. skipped=.. failed=..` line:
       failed = 0, passed >= baseline, skipped <= baseline. The baseline is
       the measure of the branching point of the card from the base branch
       (S4-25), not the latest one. This one check is
       skipped (S4-13) when the tree of the working copy is the tree that
       passed `hook` and was merged by integrate.py (<ID>.merged.json), and
       (S4-16) when every changed path matches [checks] doc_paths (a
       non-empty change set; checks 1-5 always run). Neither skip applies to
       the card of a task (integrate.is_task_card; S4-21): its run is always
       the full suite, and a failure sends it to needs_human at once. The gate
       of such a card takes only the merged record of its own delivery
       (`source` = deliver/<ID>, written by `ours/bin/deliver merged`; S4-27).
       The `hook pass` event names the content (`tree`, `head`) and says
       `tests` = full | skipped-docs: `ours/bin/deliver` looks for it.

One check per card at a time (<board>/.ours-state/<ID>.lock): a second hook
skips, the gate waits within its timeout.

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
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import fcntl

import project_config as pc

DEFAULT_FORBIDDEN_PATHS = (
    "WORKFLOW.md",
    "ours/**",
    ".github/**",
    ".git/**",
    ".env",
    ".env.*",
    "**/*.pem",
    "**/*.key",
    # The agent does not edit the settings and instructions of Claude Code: a later turn (or a
    # session of a person) would start with foreign rights or instructions (E4).
    ".claude/**",
    ".mcp.json",
    "CLAUDE.md",
    "AGENTS.md",
    # Nested rule files too (E4b); `**/` also matches the root.
    "**/CLAUDE.md",
    "**/AGENTS.md",
)
# `ours/**` is the only default pattern that [checks] allow_paths can lift.
OURS_PATTERN = "ours/**"
# Files that check the agent's work or set its rights. Forbidden always, even
# when allow_paths matches them: only a human-run task changes these.
CONTROL_CONTOUR = ("ours/hooks/**", "ours/bin/**", "ours/templates/**")
# The default `.env` patterns are anchored at the repository root; inside the
# zone that allow_paths can open, secret files stay forbidden at any depth.
OURS_SECRET_PATTERNS = ("ours/**/.env", "ours/**/.env.*")
DEFAULT_REQUIRED_SECTIONS = ("## Wiki Updates", "## As-Is -> To-Be Report")
# A subtask has no Document stage: Verify writes these two.
DEFAULT_WORKER_REQUIRED_SECTIONS = ("## QA Evidence", "## Merge Status")
DEFAULT_MAX_CHECK_FAILURES = 3

BOARD_DIR = pc.BOARD_DIR
STATE_DIR = pc.STATE_DIR
TEST_SCRIPT = "test.sh"
LOG_NAME = "check.log"
NOTE_HEADING = "Check Failure"
# S4-25: a note with one of these headings goes to the end of the existing
# section (the last one, when a card has several), not into a new section.
APPEND_TO_SECTION = frozenset({"Subtask Progress", "Delivery"})
# One-shot mark "this turn's content passed the checks" for integrate.py hook.
PASS_SUFFIX = ".pass.json"
# One-shot record "this tree was merged into the integration branch" written
# by integrate.py hook; the gate may then skip the tests for the same tree.
MERGED_SUFFIX = ".merged.json"
# `source` of the merged record that `ours/bin/deliver merged` writes (S4-27): the
# gate of a task card takes only this one, `deliver/<its ID>`.
DELIVER_PREFIX = "deliver/"
# Pathspec of the standard after_run block (templates/WORKFLOW.md.tmpl).
STAGE_PATHSPEC = (".", ":(exclude)kanban", ":(exclude).symphony")

CHECKED_STATES = frozenset({"done", "human review", "merging"})
ROLLBACK_STATES = frozenset({"In Progress", "needs_human"})
STATE_RETRY = "In Progress"
STATE_ESCALATE = "needs_human"
# S4-21: the card of a task never gets the doc_paths skip (S4-16, item 4).
TASK_CARD_FULL_REASON = "task card: full suite (S4-16 п. 4)"

ISSUE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
TAIL_BYTES = 4000
GIT_TIMEOUT_S = 120
SYMPHONY_TIMEOUT_S = 60
POLL_S = 0.2
# The gate needs this much of done_gate_timeout_ms beyond the test run.
GATE_MARGIN_S = 30

# Repository-controlled git settings must not change what the checks see:
# an fsmonitor hook could report a dirty tree as clean.
# E1b: findings listed in one refusal; an untracked file bigger than this is not read.
KEY_FINDINGS_SHOWN = 10
KEY_SCAN_MAX_BYTES = 8_000_000
KEY_REFUSAL = "в добавленных строках найден ключ API Anthropic (sk-ant-…): {where}; сама строка не выводится"
GIT_SAFE_ARGS = ("-c", "core.fsmonitor=false", "-c", "core.quotePath=false")
GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")

class CheckError(Exception):
    """A refusal with an operator-readable reason."""


class Orphaned(Exception):
    """The parent hook shell died (hooks.timeout_ms); stop without acting."""


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    base_branch: str
    symphony_bin: str
    test_timeout_s: int = pc.DEFAULT_TIMEOUTS["tests_s"]  # [timeouts] tests_s
    gate_timeout_s: int = pc.DEFAULT_TIMEOUTS["gate_ms"] // 1000
    forbidden_paths: tuple[str, ...] = DEFAULT_FORBIDDEN_PATHS
    allow_paths: tuple[str, ...] = ()
    test_globs: tuple[str, ...] = pc.DEFAULT_TEST_GLOBS
    allow_modified_tests: tuple[str, ...] = ()
    required_sections: tuple[str, ...] = DEFAULT_REQUIRED_SECTIONS
    worker_required_sections: tuple[str, ...] = DEFAULT_WORKER_REQUIRED_SECTIONS
    doc_paths: tuple[str, ...] = ()  # changes only here: the full test run is skipped
    integration: bool = False  # [integration] enabled: who is a subtask
    worker_label: str = pc.DEFAULT_INTEGRATION["worker_label"]
    max_check_failures: int = DEFAULT_MAX_CHECK_FAILURES
    # E2: [baseline] paths; None = the Python rule of project_config.BASELINE_RULE
    baseline_paths: tuple[str, ...] | None = None
    # E4b: [agent] project_rules / manager_rules: files of the board's rules, forbidden for cards
    rule_files: tuple[str, ...] = ()


def load_config(board: Path) -> Config:
    """Rules from <board>/project.toml (schema: project_config.py)."""
    try:
        project = pc.load_project(board)
    except pc.ProjectError as exc:
        raise CheckError(f"config: {exc}") from None
    checks = project["checks"]
    sections = tuple(checks.get("required_sections", DEFAULT_REQUIRED_SECTIONS))
    worker_sections = tuple(
        checks.get("worker_required_sections", DEFAULT_WORKER_REQUIRED_SECTIONS)
    )
    for heading in (*sections, *worker_sections):
        if not re.match(r"^##\s+\S", heading):
            raise CheckError(f"config: required section {heading!r} must start with '## '")
    agent = project.get("agent") or {}
    allow_paths = tuple(checks.get("allow_paths", ()))
    for pattern in allow_paths:
        if not pattern.strip().startswith("ours/"):
            raise CheckError(f"config: allow_paths {pattern!r} must start with 'ours/'")
    cfg = Config(
        base_branch=project["project"]["base_branch"],
        symphony_bin=project["tools"]["symphony"],
        test_timeout_s=project["timeouts"]["tests_s"],
        gate_timeout_s=project["timeouts"]["gate_ms"] // 1000,
        forbidden_paths=DEFAULT_FORBIDDEN_PATHS + tuple(checks.get("forbidden_paths", ())),
        allow_paths=allow_paths,
        test_globs=tuple(checks.get("test_globs", pc.DEFAULT_TEST_GLOBS)),
        allow_modified_tests=tuple(checks.get("allow_modified_tests", ())),
        required_sections=sections,
        worker_required_sections=worker_sections,
        doc_paths=tuple(checks.get("doc_paths", ())),
        integration=project["integration"]["enabled"],
        worker_label=project["integration"]["worker_label"],
        max_check_failures=checks.get("max_check_failures", DEFAULT_MAX_CHECK_FAILURES),
        baseline_paths=tuple(project["baseline"]["paths"]) if project["baseline"] else None,
        rule_files=tuple(agent[key] for key in ("project_rules", "manager_rules") if key in agent),
    )
    for pattern in (
        *cfg.forbidden_paths, *cfg.rule_files, *cfg.allow_paths, *cfg.test_globs, *cfg.allow_modified_tests,
        *cfg.doc_paths,
    ):
        try:
            pc.compile_glob(pattern)
        except pc.ProjectError as exc:
            raise CheckError(f"config: {exc}") from None
    return cfg


# ---------------------------------------------------------------------------
# globs: the syntax and the matcher live in project_config.py (one parser for
# [checks] patterns and for [baseline] paths).
# ---------------------------------------------------------------------------

glob_match = pc.glob_match
_matcher = pc.matcher


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV_DROP}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def git(
    repo: Path, *args: str, stdin: bytes | None = None, env: dict[str, str] | None = None
) -> bytes:
    proc = subprocess.run(
        ["git", *GIT_SAFE_ARGS, *args],
        cwd=str(repo),
        input=stdin,
        stdin=None if stdin is not None else subprocess.DEVNULL,
        capture_output=True,
        timeout=GIT_TIMEOUT_S,
        env={**_git_env(), **(env or {})},
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise CheckError(f"git {args[0]} failed (exit {proc.returncode}): {detail[:300]}")
    return proc.stdout


def _z(output: bytes) -> list[str]:
    return [item.decode("utf-8", errors="surrogateescape") for item in output.split(b"\0") if item]


def resolve_base(workspace: Path, board: Path, issue_id: str) -> tuple[Path, str]:
    """Return (repository top level, recorded base sha) or raise CheckError.

    The base is the commit the worktree was created from (worktree.py),
    never a merge-base with a moving branch: a merge into the ticket branch
    would otherwise make every change invisible.
    """
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
        sha = pc.read_base_record(board, issue_id)["base_sha"]
    except pc.ProjectError as exc:
        raise CheckError(f"base: {exc}") from None
    try:
        git(repo, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    except CheckError:
        raise CheckError(f"base: recorded base {sha[:12]} is not a commit here") from None
    try:
        git(repo, "merge-base", "--is-ancestor", sha, "HEAD")
    except CheckError:
        raise CheckError(
            f"base: recorded base {sha[:12]} is not an ancestor of HEAD "
            "(branch rewritten relative to the base)"
        ) from None
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


def worktree_tree(workspace: Path) -> str:
    """Tree object of the working-tree content the standard after_run block
    would commit: HEAD plus `git add -A` with its exclusions, in a temporary
    index. The real index is not touched."""
    with tempfile.TemporaryDirectory(prefix="ours-index-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git(workspace, "read-tree", "HEAD", env=env)
        try:
            git(workspace, "add", "-A", "--", *STAGE_PATHSPEC, env=env)
        except CheckError:
            # Same as the standard block's `|| true`: git exits 1 when the
            # excluded `kanban` link is ignored, yet the rest is added. A real
            # failure only makes the tree differ, and integrate.py then skips.
            pass
        return git(workspace, "write-tree", env=env).decode().strip()


def _under(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory + "/")


# ---------------------------------------------------------------------------
# checks 3-5
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ForbiddenRules:
    """The paths an agent may not change, as `forbidden_violations` applies them."""

    contour: tuple[str, ...]  # forbidden always, allow_paths does not lift them
    closed: tuple[str, ...]  # forbidden outright: defaults, project list, secrets
    ours: str  # forbidden except where `opened` matches
    opened: tuple[str, ...]  # [checks] allow_paths: the exceptions to `ours`


def forbidden_rules(cfg: Config) -> ForbiddenRules:
    """The one place that says what is forbidden; the check and the brief use it."""
    others = tuple(p for p in cfg.forbidden_paths if p != OURS_PATTERN)
    return ForbiddenRules(
        contour=CONTROL_CONTOUR,
        closed=tuple(dict.fromkeys(others + cfg.rule_files + OURS_SECRET_PATTERNS)),
        ours=OURS_PATTERN,
        opened=cfg.allow_paths,
    )


def forbidden_violations(changed: set[str], cfg: Config) -> list[str]:
    rules = forbidden_rules(cfg)
    in_contour = _matcher(rules.contour)
    in_other = _matcher(rules.closed)
    in_ours = _matcher((rules.ours,))
    is_allowed = _matcher(rules.opened)
    problems = []
    for path in sorted(changed):
        if in_contour(path):
            problems.append(f"forbidden path changed: {_clean(path)} (control contour)")
        elif in_other(path) or (in_ours(path) and not is_allowed(path)):
            problems.append(f"forbidden path changed: {_clean(path)}")
    return problems


# ---------------------------------------------------------------------------
# an API key in the added lines (E1b)
# ---------------------------------------------------------------------------

_HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# `-U0 --text` and fixed prefixes: the same output whatever the user's git config says.
KEY_DIFF_ARGS = (
    "diff", "-U0", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--text",
    "--src-prefix=a/", "--dst-prefix=b/",
)


def has_api_key(text: str) -> bool:
    return pc.API_KEY_RE.search(text) is not None


def _diff_path(header: str) -> str | None:
    """Path of a `+++ ` line of a diff; None for /dev/null."""
    name = header[4:].rstrip("\r")
    if name == "/dev/null":
        return None
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1]
    return name[2:] if name.startswith("b/") else name


def diff_key_hits(diff: str) -> list[tuple[str, int]]:
    """(path, line number) of every added line of a `git diff -U0` that holds an API key.

    The line itself is never returned. A file whose added lines hold a NUL byte is
    binary and is skipped, like git does without --text.
    """
    hits: dict[str, list[int]] = {}
    binary: set[str] = set()
    path: str | None = None
    old_left = new_left = new_line = 0
    for raw in diff.split("\n"):
        if old_left > 0 or new_left > 0:
            if raw.startswith("+"):
                new_left -= 1
                if path is not None:
                    if "\0" in raw:
                        binary.add(path)
                    if has_api_key(raw[1:]):
                        hits.setdefault(path, []).append(new_line)
                new_line += 1
                continue
            if raw.startswith("-"):
                old_left -= 1
                continue
            if raw.startswith("\\"):
                continue
            old_left = new_left = 0  # damaged hunk: read the line as a header
        if raw.startswith("+++ "):
            path = _diff_path(raw)
            continue
        match = _HUNK_RE.match(raw)
        if match:
            old_left = int(match.group(1) or 1) if match.group(1) != "0" else 0
            new_line = int(match.group(2))
            new_left = int(match.group(3) or 1) if match.group(3) != "0" else 0
    return sorted((p, n) for p, lines in hits.items() if p not in binary for n in lines)


def key_refusals(hits: list[tuple[str, int]]) -> list[str]:
    """One refusal reason for the findings (empty without findings); no key text in it."""
    if not hits:
        return []
    shown = [f"{_clean(path)}:{line}" for path, line in hits[:KEY_FINDINGS_SHOWN]]
    if len(hits) > KEY_FINDINGS_SHOWN:
        shown.append(f"и ещё {len(hits) - KEY_FINDINGS_SHOWN}")
    return [KEY_REFUSAL.format(where=", ".join(shown))]


def untracked_key_hits(repo: Path, board_dir: str) -> list[tuple[str, int]]:
    hits = []
    for path in _z(git(repo, "ls-files", "--others", "--exclude-standard", "-z")):
        if _under(path, board_dir) or _under(path, ".symphony"):
            continue
        disk = repo / path
        try:
            if disk.is_symlink() or not disk.is_file() or disk.stat().st_size > KEY_SCAN_MAX_BYTES:
                continue
            data = disk.read_bytes()
        except OSError:
            continue
        if b"\0" in data:
            continue
        for number, line in enumerate(data.decode("utf-8", errors="replace").split("\n"), 1):
            if has_api_key(line):
                hits.append((path, number))
    return hits


def key_violations(repo: Path, base: str, board_dir: str) -> list[str]:
    """Refusal for an API key in the lines added relative to the base: committed, staged,
    unstaged, untracked (the paths of `changed_paths`; the board and .symphony are left out)."""
    pathspec = ("--", ".", f":(exclude){board_dir}", ":(exclude).symphony")
    diffs = (
        (base, "HEAD", *pathspec),
        ("--cached", base, *pathspec),
        (base, *pathspec),
    )
    hits: set[tuple[str, int]] = set(untracked_key_hits(repo, board_dir))
    for args in diffs:
        text = git(repo, *KEY_DIFF_ARGS, *args).decode("utf-8", errors="replace")
        hits.update(diff_key_hits(text))
    return key_refusals(sorted(hits))


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


def is_subtask_card(card: Path, cfg: Config) -> bool:
    """Same definition as integrate.py (integrate.is_subtask); a card whose
    front matter cannot be read is an ordinary card (the stricter sections)."""
    if not cfg.integration:
        return False
    import integrate  # lazy: integrate.py imports this module

    try:
        front = integrate.read_card_front(card)
    except integrate.IntegrateError:
        return False
    return integrate.is_subtask(front, {"enabled": True, "worker_label": cfg.worker_label})


def is_task_card_file(card: Path) -> bool:
    """Same definition as integrate.py (integrate.is_task_card): the manager
    label and a `## Plan Accepted` section. A card that cannot be read is an
    ordinary card."""
    import integrate  # lazy: integrate.py imports this module

    try:
        text = card.read_text(encoding="utf-8")
        front = integrate.read_card_front(card)
    except (OSError, UnicodeDecodeError, integrate.IntegrateError):
        return False
    return integrate.is_task_card(front, split_card(text)[1])


def card_violations(card: Path, cfg: Config) -> list[str]:
    try:
        text = card.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [f"card not found: {BOARD_DIR}/{card.name}"]
    except (OSError, UnicodeDecodeError) as exc:
        return [f"card unreadable: {BOARD_DIR}/{card.name}: {exc}"]
    _, body = split_card(text)
    required = cfg.worker_required_sections if is_subtask_card(card, cfg) else cfg.required_sections
    return [
        f"card section missing or empty: {heading}"
        for heading in required
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


def run_tests(
    workspace: Path, script: Path, cfg: Config, parent_pid: int
) -> tuple[str | None, str, dict[str, int] | None]:
    """Run test.sh; return (failure reason or None, output tail, summary)."""
    with tempfile.TemporaryFile() as out:
        # Output goes to a file, not a pipe: a straggler holding the pipe
        # could otherwise turn a passing run into a timeout. No login shell:
        # test.sh sets its own PATH (DECISIONS, item к).
        proc = subprocess.Popen(
            ["/bin/bash", str(script)],
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
        out.seek(0)
        summary = pc.find_summary(out)
    if orphaned:
        raise Orphaned("parent hook shell died while tests were running")
    if timed_out:
        return f"test.sh timed out after {cfg.test_timeout_s}s", tail, summary
    if proc.returncode == 0:
        return None, tail, summary
    if proc.returncode == 127:
        return "test.sh: command not found (exit 127)", tail, summary
    return f"test.sh failed (exit {proc.returncode})", tail, summary


def summary_violations(
    summary: dict[str, int] | None, baseline: dict | None, effective: Effective | None = None
) -> list[str]:
    """test.sh exited 0: its summary line must exist and hold the baseline.

    `baseline` None: no measure applies (S4-25), only failed = 0 is required.
    `effective` names the measure of a branching point; without it the text
    is the one of the single baseline.json (`legacy`)."""
    if summary is None:
        return [
            f"test.sh: код 0, но нет строки итогов `{pc.SUMMARY_FORMAT}` "
            "(её печатает ours/hooks/summarize.py в конце test.sh)"
        ]
    problems = []
    if summary["failed"] > 0:
        problems.append(f"test.sh: failed={summary['failed']} при коде 0")
    if baseline is None:
        return problems
    if summary["passed"] < baseline["passed"]:
        problems.append(f"test.sh: passed={summary['passed']} меньше базы {baseline['passed']}")
    if summary["skipped"] > baseline["skipped"]:
        problems.append(f"test.sh: skipped={summary['skipped']} больше базы {baseline['skipped']}")
    if problems:
        sha = str(baseline.get("base_sha", ""))[:12] or "?"
        if effective is None or effective.match == "legacy":
            problems.append(
                f"итоги: сейчас {pc.triple(summary)}; база {pc.triple(baseline)} "
                f"(замер project-doctor на {baseline['base_branch']} @ {sha}). "
                "Если базовая ветка менялась (тесты добавлены или убраны там), "
                "перезапусти project-doctor"
            )
        else:
            problems.append(
                f"итоги: сейчас {pc.triple(summary)}; замер {pc.triple(baseline)} "
                f"({baseline['base_branch']} @ {sha}, {effective.match}); точка ответвления "
                f"карточки {effective.point[:12]}. Сравнение идёт с замером точки ответвления: "
                "тесты, которые были на ней, не должны выпасть"
            )
    return problems


# ---------------------------------------------------------------------------
# orchestration of the checks
# ---------------------------------------------------------------------------


@dataclass
class Context:
    mode: str
    workflow_dir: Path
    workspace: Path
    issue_id: str
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
    tests_skipped: bool = False  # tests not run: the merged tree (gate) or only doc paths changed
    note: str = ""  # one line for the report and the log when tests were skipped
    tests: str = ""  # "skipped" | "full" for a gate that met a merged record; "skipped-docs"
    tests_reason: str = ""  # why the record could not skip the tests
    task_card: bool = False  # the card of a task: full suite, no skips (S4-21)
    baseline_match: str = ""  # "exact" | "ancestor" | "unknown" | "legacy" (S4-25); "" = not resolved
    baseline_sha: str = ""
    baseline_point: str = ""


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


def build_context(mode: str) -> Context:
    workflow_dir = workflow_dir_from_env()
    workspace = Path.cwd().resolve()
    if _inside(workflow_dir, workspace):
        raise CheckError(f"board folder {workflow_dir} is inside the agent workspace; refusing")
    # SYMPHONY_ISSUE_TITLE is agent-controlled and deliberately never read.
    issue_id = validate_issue_id(os.environ.get("SYMPHONY_ISSUE_ID") or workspace.name)
    return Context(mode, workflow_dir, workspace, issue_id)


def try_lock(ctx: Context, wait_s: float):
    """Exclusive per-card lock file handle, or None when still busy."""
    pc.ensure_state_dir(ctx.state_dir.parent)
    # Opened non-inheritable: the test processes never hold the lock.
    handle = open(ctx.state_dir / f"{ctx.issue_id}.lock", "a")
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                return None
            time.sleep(POLL_S)


@dataclass
class Baselines:
    """What the card is compared with: the history (baselines.json) or, when the
    board has none yet, the single measure of baseline.json (`legacy`)."""

    legacy: dict | None = None
    records: list[dict] = field(default_factory=list)  # of the base branch, oldest first


def load_baselines(board: Path, cfg: Config) -> Baselines:
    try:
        history = pc.read_baselines(board)
    except pc.ProjectError as exc:
        raise CheckError(f"baseline: {exc}") from None  # never falls back to baseline.json
    if history is None:
        return Baselines(legacy=load_baseline(board, cfg))
    records = [r for r in history if r["base_branch"] == cfg.base_branch]
    if not records:
        raise CheckError(
            f"baseline: в истории замеров нет записей для ветки {cfg.base_branch!r}: "
            "перезапусти project-doctor"
        )
    return Baselines(records=records)


def load_baseline(board: Path, cfg: Config) -> dict:
    try:
        baseline = pc.read_baseline(board)
    except pc.ProjectError as exc:
        raise CheckError(f"baseline: {exc}") from None
    if baseline["base_branch"] != cfg.base_branch:
        raise CheckError(
            f"baseline: замер сделан на ветке {baseline['base_branch']!r}, а base_branch "
            f"{cfg.base_branch!r}: перезапусти project-doctor"
        )
    return baseline


@dataclass(frozen=True)
class Effective:
    """The measure that applies to the branching point of a card (S4-25)."""

    match: str  # "exact" | "ancestor" | "unknown" | "legacy"
    record: dict | None
    point: str = ""
    reason: str = ""  # why there is none ("unknown")


def branching_point(repo: Path, base_branch: str) -> str:
    """The commit of the base branch the card's branch started from."""
    try:
        return git(repo, "merge-base", "HEAD", f"refs/heads/{base_branch}").decode().strip()
    except CheckError as exc:
        raise CheckError(f"baseline: точка ответвления от {base_branch!r} не найдена: {exc}") from None


def _git_ok(repo: Path, *args: str) -> bool:
    """Exit 0 of a git query; exit 1 is the answer "no"; anything else is an error."""
    proc = subprocess.run(
        ["git", *GIT_SAFE_ARGS, *args], cwd=str(repo), stdin=subprocess.DEVNULL,
        capture_output=True, timeout=GIT_TIMEOUT_S, env=_git_env(), check=False,
    )
    if proc.returncode not in (0, 1):
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise CheckError(f"git {args[0]} failed (exit {proc.returncode}): {detail[:300]}")
    return proc.returncode == 0


def effective_baseline(
    repo: Path, baselines: Baselines, base_branch: str,
    affects: Callable[[str], bool] = pc.affects_baseline,
) -> Effective:
    """Strictly: the record of the point itself, else the nearest record that is an
    ancestor of the point when no file that changes test results lies between them
    (post-merge measures again exactly when such a file changes, so the older record
    is as good as an own one), else none. `affects` is the rule of the board
    ([baseline] paths of project.toml, else the Python rule)."""
    if baselines.legacy is not None:
        return Effective("legacy", baselines.legacy)
    point = branching_point(repo, base_branch)
    for record in reversed(baselines.records):
        if record["base_sha"] == point:
            return Effective("exact", record, point)
    nearest: tuple[int, dict] | None = None
    for record in baselines.records:
        sha = record["base_sha"]
        if not _git_ok(repo, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"):
            continue  # a commit this repository does not have: skipped, not an error
        if not _git_ok(repo, "merge-base", "--is-ancestor", sha, point):
            continue
        distance = int(git(repo, "rev-list", "--count", f"{sha}..{point}").decode().strip())
        if nearest is None or distance <= nearest[0]:
            nearest = (distance, record)
    if nearest is None:
        return Effective("unknown", None, point, "нет записи")
    record = nearest[1]
    touched = [
        path for path in _z(git(repo, "diff", "--name-only", "-z", "--no-renames", record["base_sha"], point))
        if affects(path)
    ]
    if touched:
        shown = ", ".join(_clean(path) for path in touched[:5])
        return Effective(
            "unknown", None, point,
            f"между записью {record['base_sha'][:12]} и точкой менялись файлы тестов: {shown}",
        )
    return Effective("ancestor", record, point)


def merged_path(ctx: Context) -> Path:
    return ctx.state_dir / f"{ctx.issue_id}{MERGED_SUFFIX}"


def _take_merged(ctx: Context) -> dict | None:
    """The one-shot record of integrate.py hook; removed whatever it says."""
    path = merged_path(ctx)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    try:
        path.unlink()
    except OSError:
        pass
    return data if isinstance(data, dict) else None


def _merged_skip(ctx: Context, merged: dict) -> tuple[str | None, str]:
    """(skip note, "") when the working copy holds the merged tree, else (None, reason)."""
    tree = merged.get("tree")
    if not isinstance(tree, str) or not tree:
        return None, "merged record has no tree"
    try:
        current = worktree_tree(ctx.workspace)
    except CheckError as exc:
        return None, f"cannot compute the content tree: {exc}"
    commit = merged.get("commit")
    # A delivery may merge a newer base while leaving the card's branch untouched.
    # Its original tree is eligible only with a verifiable link to the merged D.
    if merged.get("source") == f"{DELIVER_PREFIX}{ctx.issue_id}" and "source_tree" in merged:
        source_tree, source_commit = merged.get("source_tree"), merged.get("source_commit")
        if not all(pc.is_sha(sha) for sha in (source_tree, source_commit, commit)):
            return None, "delivery record has invalid object IDs"
        try:
            actual_source = git(ctx.workspace, "rev-parse", f"{source_commit}^{{tree}}").decode().strip()
            actual_target = git(ctx.workspace, "rev-parse", f"{commit}^{{tree}}").decode().strip()
            git(ctx.workspace, "merge-base", "--is-ancestor", source_commit, commit)
        except CheckError as exc:
            return None, f"cannot verify delivery provenance: {exc}"
        if actual_source != source_tree or actual_target != tree:
            return None, "delivery tree provenance differs"
        if current not in (source_tree, tree):
            return None, "tree differs from the delivered source and result"
        return (f"tests skipped: source tree {source_tree[:12]} was fully checked in delivery "
                f"{commit[:12]} (tree {tree[:12]}) and merged into {_clean(str(merged.get('branch', '?')))}", "")
    if current != tree:
        return None, "tree differs from the merged one"
    where = str(commit)[:12] if commit else "no changes"
    return (
        f"tests skipped: tree {tree[:12]} passed check.py hook and was merged into "
        f"{_clean(str(merged.get('branch', '?')))} ({_clean(where)})",
        "",
    )


def _docs_only(changed: set[str], cfg: Config) -> str | None:
    """The skip note when the change set is non-empty and every path is in doc_paths."""
    if not cfg.doc_paths or not changed:
        return None
    in_docs = _matcher(cfg.doc_paths)
    if not all(in_docs(path) for path in changed):
        return None
    return f"tests skipped: only doc paths changed ({len(changed)} files)"


def _baseline_marks(effective: Effective | None) -> dict[str, str]:
    """The Result fields that tell which measure the card was compared with."""
    if effective is None:
        return {}
    return {
        "baseline_match": effective.match,
        "baseline_sha": str(effective.record["base_sha"]) if effective.record else "",
        "baseline_point": effective.point,
    }


def _baseline_extra(result: Result) -> dict[str, str]:
    """The same as log fields (an unresolved measure adds none)."""
    if not result.baseline_match:
        return {}
    extra = {"baseline_match": result.baseline_match, "baseline_sha": result.baseline_sha}
    if result.baseline_point:
        extra["baseline_point"] = result.baseline_point
    return extra


def run_checks(ctx: Context, cfg: Config | None = None) -> Result:
    cfg = cfg or load_config(ctx.workflow_dir)
    # Only the gate reads the record, and it is used up here, passed or not.
    merged = _take_merged(ctx) if ctx.mode == "gate" else None
    task_card = is_task_card_file(ctx.card)
    if task_card and merged is not None and merged.get("source") != f"{DELIVER_PREFIX}{ctx.issue_id}":
        # taken (used up) above, but not used: integrate.py never merges a task card; only the
        # record of its own delivery counts (S4-27). A subtask's record or one without `source` does not.
        merged = None
    reasons: list[str] = []
    baselines: Baselines | None = None
    effective: Effective | None = None
    changed: set[str] = set()
    try:
        baselines = load_baselines(ctx.workflow_dir, cfg)
    except CheckError as exc:
        reasons.append(str(exc))
    try:
        repo, base = resolve_base(ctx.workspace, ctx.workflow_dir, ctx.issue_id)
    except CheckError as exc:
        reasons.append(str(exc))
    else:
        board_dir = os.environ.get("SYMPHONY_BOARD_ROOT_NAME") or BOARD_DIR
        changed = changed_paths(repo, base, board_dir)
        reasons += forbidden_violations(changed, cfg)
        reasons += key_violations(repo, base, board_dir)
        reasons += test_violations(repo, base, cfg)
        if baselines is not None:
            try:
                effective = effective_baseline(
                    repo, baselines, cfg.base_branch, pc.paths_rule(cfg.baseline_paths)
                )
            except CheckError as exc:
                reasons.append(str(exc))
    reasons += card_violations(ctx.card, cfg)
    marks = _baseline_marks(effective)
    if reasons:
        return Result(False, reasons, "", cfg, task_card=task_card, **marks)
    mark: dict[str, object] = {"task_card": True} if task_card else {}
    mark.update(marks)
    if task_card:
        mark.update(tests="full", tests_reason=TASK_CARD_FULL_REASON)
    if merged is not None:
        note, why = _merged_skip(ctx, merged)
        if note is not None:
            return Result(True, [], "", cfg, tests_skipped=True, note=note, tests="skipped")
        mark.update(tests="full", tests_reason=why)
    note = None if task_card else _docs_only(changed, cfg)
    if note is not None:
        mark["tests"] = "skipped-docs"
        return Result(True, [], "", cfg, tests_skipped=True, note=note, **mark)
    assert effective is not None  # no reasons: the baselines were loaded and resolved
    if effective.match == "unknown":
        log_event(ctx, ctx.issue_id, ctx.mode, "baseline_unknown", effective.reason,
                  baseline_point=effective.point, base_branch=cfg.base_branch)
    failure, tail, summary = run_tests(
        ctx.workspace, ctx.workflow_dir / TEST_SCRIPT, cfg, ctx.parent_pid
    )
    if failure is not None:
        return Result(False, [failure], tail, cfg, **mark)
    problems = summary_violations(summary, effective.record, effective)
    if problems:
        return Result(False, problems, tail, cfg, **mark)
    return Result(True, [], tail, cfg, **mark)


# ---------------------------------------------------------------------------
# rollback, note, counter, log (hook mode)
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(text: str) -> str:
    """One line, control characters escaped (paths come from the agent)."""
    return "".join(ch if ch.isprintable() else repr(ch)[1:-1] for ch in text)


def ensure_state_dir(state_dir: Path) -> None:
    pc.ensure_state_dir(state_dir.parent)  # inside an existing board folder only


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
    if cfg is not None:
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
    result: Result, *, mode: str, failures: int, limit: int, target: str, moved: bool,
    task_card: bool = False,
) -> str:
    lines = [
        f"- at: {_now()}",
        f"- check: ours/hooks/check.py {mode}",
        f"- failures: {failures} of {limit}",
    ]
    if task_card:
        lines.append("- task card: straight to needs_human, no worker retry (S4-21)")
    lines += [
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


_APPEND_SECTION_CODE = """\
import sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, sys.argv[5])
from check import add_to_section
from symphony.trackers.file import FileBoardTracker
from symphony.workflow import TrackerConfig
root, ident, heading, text = sys.argv[1:5]
tracker = FileBoardTracker(TrackerConfig(
    kind="file", endpoint="", api_key="", project_slug="",
    active_states=("Todo", "In Progress"), terminal_states=("Done", "Cancelled"),
    board_root=Path(root).resolve(),
))


def mutate(front, body):
    front["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return front, add_to_section(body, heading, text)


# Private method of the core: there is no public "append to a section"
# (DECISIONS, S4-25). It holds the card lock and checks updated_at.
tracker._mutate_ticket(ident, mutate)
"""

_SECTION_HEADING_RE = re.compile(r"^#{1,2}\s")


def add_to_section(body: str, heading: str, text: str) -> str:
    """`text` at the end of the last `## <heading>` section of `body` (the section
    runs to the next `#` / `##` line or the end); no such section: a new one."""
    text = text.strip()
    lines = body.split("\n")
    start = None
    for index, line in enumerate(lines):
        if re.fullmatch(rf"##\s+{re.escape(heading)}\s*", line, re.IGNORECASE):
            start = index
    if start is None:
        return "\n\n".join(part for part in (body.strip(), f"## {heading}\n\n{text}") if part)
    end = next(
        (i for i in range(start + 1, len(lines)) if _SECTION_HEADING_RE.match(lines[i])), len(lines)
    )
    last = end - 1
    while last > start and not lines[last].strip():
        last -= 1
    # A list item follows a list item directly; anything else gets a blank line.
    glue = [] if lines[last].lstrip().startswith(("- ", "* ")) else [""]
    return "\n".join([*lines[: last + 1], *glue, text, *lines[last + 1 :]])


def append_note(
    ctx: Context, body: str, heading: str = NOTE_HEADING, issue_id: str | None = None
) -> str:
    """Append `## <heading>` (default `## Check Failure`) through the tracker API; file fallback.

    `issue_id` names another card of the same board (S4-21: the task card gets
    progress lines from its subtask's hook); default: the card of `ctx`.
    A heading in APPEND_TO_SECTION (S4-25) adds `body` to the end of the existing
    section of that name instead: `## Subtask Progress` stays one section."""
    target = issue_id or ctx.issue_id
    python = os.environ.get("SYMPHONY_PYTHON", "").strip()
    board = ctx.workflow_dir / BOARD_DIR
    in_section = heading in APPEND_TO_SECTION
    if python and os.access(python, os.X_OK):
        code, args = (
            (_APPEND_SECTION_CODE, [str(Path(__file__).resolve().parent)])
            if in_section else (_APPEND_NOTE_CODE, [])
        )
        try:
            proc = subprocess.run(
                # -P: never put the cwd on sys.path (the agent controls it).
                [python, "-P", "-c", code, str(board), target, heading, body, *args],
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
    _append_note_file(board / f"{target}.md", board, target, body, heading)
    return "file"


def _append_note_file(
    card: Path, board: Path, issue_id: str, body: str, heading: str = NOTE_HEADING
) -> None:
    lock_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", issue_id).strip("._") or "ticket"
    lock_path = board / ".locks" / f"{lock_name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock:
        # Same per-ticket lock file as the file tracker's mutations.
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        text = card.read_text(encoding="utf-8")
        if heading in APPEND_TO_SECTION:
            new = add_to_section(text, heading, body).rstrip() + "\n"
        else:
            new = f"{text.rstrip()}\n\n## {heading}\n\n{body.strip()}\n"
        _write_atomic(card, new.encode("utf-8"))


def rollback(ctx: Context, result: Result) -> None:
    limit = result.cfg.max_check_failures if result.cfg else DEFAULT_MAX_CHECK_FAILURES
    reason = "; ".join(result.reasons)
    failures = bump_counter(ctx, reason)
    # The card of a task never goes back to In Progress (S4-21): it would get the
    # worker prompt and fix code itself, past the subtasks. A human decides.
    task_card = is_task_card_file(ctx.card)
    target = STATE_ESCALATE if failures >= limit or task_card else STATE_RETRY
    if target not in ROLLBACK_STATES:  # pragma: no cover - defensive
        raise CheckError(f"refusing rollback target {target!r}")
    errors: list[str] = []
    binary = resolve_symphony_bin(result.cfg)
    if binary is None:
        errors.append("symphony binary not found ([tools] symphony / SYMPHONY_PYTHON); rollback impossible")
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
        result, mode=ctx.mode, failures=failures, limit=limit, target=target, moved=not errors,
        task_card=task_card,
    )
    try:
        note_via = append_note(ctx, note)
    except Exception as exc:  # noqa: BLE001
        note_via = "failed"
        errors.append(f"cannot append note: {type(exc).__name__}: {exc}")
    log_event(ctx, ctx.issue_id, ctx.mode, f"rollback:{target}", reason, note=note_via,
              **_baseline_extra(result))
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
    if result.tests_skipped:
        lines.append(f"- {_clean(result.note)}")
    if result.task_card:
        lines.append(f"- {TASK_CARD_FULL_REASON}")
    if result.ok:
        flaky_lines = [line for line in result.tail.splitlines() if line.startswith("FLAKY:")]
        if flaky_lines:
            lines += ["--- flaky test warnings ---", *flaky_lines]
    elif result.tail.strip():
        lines += ["--- test output (tail) ---", result.tail.rstrip()]
    return "\n".join(lines)


def gate_main() -> int:
    ctx: Context | None = None
    lock = None
    try:
        ctx = build_context("gate")
        cfg = load_config(ctx.workflow_dir)
        # A hook may still be checking this card: wait for it, but leave
        # enough of done_gate_timeout_ms for our own test run.
        wait_s = cfg.gate_timeout_s - cfg.test_timeout_s - GATE_MARGIN_S
        lock = try_lock(ctx, wait_s)
        if lock is None:
            raise CheckError(
                f"another check of {ctx.issue_id} still runs after {max(0, wait_s)}s "
                "(.ours-state lock busy); try again later"
            )
        result = run_checks(ctx, cfg)
    except CheckError as exc:
        result = Result(False, [str(exc)])
    except Exception as exc:  # noqa: BLE001 - fail closed
        result = Result(False, [f"internal error: {type(exc).__name__}: {exc}"],
                        traceback.format_exc()[-TAIL_BYTES:])
    finally:
        if lock is not None:
            lock.close()
    issue_id = ctx.issue_id if ctx else "?"
    _emit(_format_report("gate", issue_id, result))
    if ctx is not None:
        extra = {}
        if result.tests:
            extra["tests"] = result.tests
        if result.tests_reason:
            extra["tests_reason"] = result.tests_reason
        extra.update(_baseline_extra(result))
        reason = result.note if result.tests_skipped else "; ".join(result.reasons)
        log_event(ctx, issue_id, "gate", "pass" if result.ok else "fail", reason, **extra)
    return 0 if result.ok else 1


def hook_main() -> int:
    try:
        ctx = build_context("hook")
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
    lock = try_lock(ctx, 0)
    if lock is None:
        # A second after_run of the same card (REPORT-10, risks, item 1).
        log_event(ctx, ctx.issue_id, "hook", "skipped",
                  "another check of this card is running; card left as is")
        return 0
    with lock:
        if state.lower() == "merging":
            try:
                cfg = load_config(ctx.workflow_dir)
                if not is_subtask_card(ctx.card, cfg):
                    raise CheckError("Merging is only for integration subtasks (worker label + request)")
            except CheckError as exc:
                log_event(ctx, ctx.issue_id, "hook", "error", str(exc))
                return 0
        return _hook_checked(ctx)


def pass_path(ctx: Context) -> Path:
    return ctx.state_dir / f"{ctx.issue_id}{PASS_SUFFIX}"


def _write_pass_mark(ctx: Context, tree: str, head: str) -> None:
    """The one-shot mark integrate.py hook reads in this same after_run: which
    content (`tree`) passed, and who ran the check (`ppid`, the hook shell)."""
    ensure_state_dir(ctx.state_dir)
    payload = {"ppid": ctx.parent_pid, "tree": tree, "head": head, "at": _now()}
    _write_atomic(pass_path(ctx), (json.dumps(payload, indent=2) + "\n").encode())


def _hook_checked(ctx: Context) -> int:
    # The tree goes first: it must describe the content the tests will see.
    tree = head = None
    try:
        tree = worktree_tree(ctx.workspace)
        head = git(ctx.workspace, "rev-parse", "HEAD").decode().strip()
    except Exception as exc:  # noqa: BLE001 - no mark then; the checks still run
        log_event(ctx, ctx.issue_id, "hook", "error", f"cannot compute the content tree: {exc}")
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
            # Keep [tools] symphony / max_check_failures for the rollback.
            result.cfg = load_config(ctx.workflow_dir)
        except Exception:  # noqa: BLE001
            pass
    if result.ok:
        if tree and head:
            try:
                _write_pass_mark(ctx, tree, head)
            except OSError as exc:
                log_event(ctx, ctx.issue_id, "hook", "error", f"cannot write the pass mark: {exc}")
        # S4-27: `tests` is always there ("full" when test.sh really ran) and the content
        # that passed is named (`tree`, `head`), so that ours/bin/deliver can find a green check.
        tests = result.tests or ("" if result.tests_skipped else "full")
        extra = {"tests": tests} if tests else {}
        if tree and head:
            extra.update(tree=tree, head=head)
        if result.task_card:
            extra["tests_reason"] = result.tests_reason
        extra.update(_baseline_extra(result))
        reason = result.note if result.tests_skipped else ""
        log_event(ctx, ctx.issue_id, "hook", "pass", reason, **extra)
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
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if argv and "-h" in argv else 2
    if args.mode == "gate":
        return gate_main()
    try:
        return hook_main()
    except BaseException as exc:  # noqa: BLE001 - the hook never fails the turn
        _emit(f"check.py hook: unexpected {type(exc).__name__}: {exc}", stream=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
