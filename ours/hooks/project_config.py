"""Shared layout and settings of a project board folder (TASK-K08a).

A board folder lives outside the code repository:

    <root>/<name>/
        project.toml      the only project settings file (schema below)
        setup.sh          prepares a fresh worktree (run by worktree.py)
        test.sh           full project test run (run by check.py)
        WORKFLOW.md       rendered from ours/templates/WORKFLOW.md.tmpl
        kanban/           the board
        artifacts/<ID>/   agent artifacts, linked as docs/<ID> in a worktree
        wiki/             linked as docs/llm-wiki in a worktree
        worktree-exclude  core.excludesFile of every worktree
        .ours-state/      state of our scripts (base records, baseline.json,
                          locks, logs)

Used by ours/bin/project-new, ours/bin/project-render, ours/bin/
project-doctor, worktree.py and check.py. Standard library only,
Python >= 3.11.
"""

from __future__ import annotations

import json
import math
import re
import shlex
import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path

OURS_DIR = Path(__file__).resolve().parents[1]
TEMPLATE = OURS_DIR / "templates" / "WORKFLOW.md.tmpl"
PROMPTS_DIR = OURS_DIR / "prompts"

PROJECT_FILE = "project.toml"
BOARD_DIR = "kanban"
ARTIFACTS_DIR = "artifacts"
WIKI_DIR = "wiki"
EXCLUDE_FILE = "worktree-exclude"
STATE_DIR = ".ours-state"
BASELINE_FILE = "baseline.json"
# A line of the setup.sh / test.sh stubs; project-doctor refuses while it is there.
STUB_MARKER = "OURS-STUB"
DEFAULT_ROOT = Path.home() / "symphony-projects"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")

DEFAULT_TIMEOUTS = {"tests_s": 900, "gate_ms": 960000, "hooks_ms": 1200000}
# max_state_turns 30 = core default, so a project.toml without the key behaves as before K14.
DEFAULT_LIMITS = {"max_turns": 30, "max_total_turns": 60, "max_attempts": 3, "max_state_turns": 30}
# Bash allowances every project gets (the set that passed the Mac probe, K13);
# `/bin/bash <board>/test.sh` is added per board by agent_bash_rules().
BASE_ALLOWED_BASH = ("git merge-tree", "test", "echo", "cat", "wc", "head", "tail")
# claude -p refuses Bash outside the allow list; a rule that lets any command
# through (or switches the permission checks off) is a config error.
_BARE_WILDCARDS = {"*", "bash", "sh", "zsh", "env", "sudo", "eval", "exec"}
_PERMISSION_BYPASS_RE = re.compile(
    r"dangerously|bypass|skip[-_ ]?permissions|permission[-_ ]?mode|allowed[-_ ]?tools|yolo",
    re.IGNORECASE,
)
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]-]*$")
STALL_MIN_MS = 300_000
# Margin over the test run for the model's own output around a long Bash call.
STALL_MARGIN_S = 120
# Active states of the template: the core refuses agent.max_turns below this.
ACTIVE_STATE_COUNT = 4

# section -> key -> (type, required)
_SCHEMA: dict[str, dict[str, tuple[type, bool]]] = {
    "project": {
        "name": (str, True),
        "repo": (str, True),
        "base_branch": (str, True),
        "id_prefix": (str, True),
        "port": (int, True),
    },
    "tools": {"python": (str, True), "symphony": (str, True), "claude": (str, True)},
    "checks": {
        "forbidden_paths": (list, False),
        "test_globs": (list, False),
        "allow_modified_tests": (list, False),
        "required_sections": (list, False),
        "max_check_failures": (int, False),
    },
    "timeouts": {
        "tests_s": (int, False),
        "gate_ms": (int, False),
        "hooks_ms": (int, False),
        "fixed": (bool, False),
    },
    "limits": {
        "max_turns": (int, False),
        "max_total_turns": (int, False),
        "max_attempts": (int, False),
        "max_state_turns": (int, False),
    },
    "agent": {"allowed_bash": (list, False)},
    "service": {"model": (str, False)},
}
# [levels]: `default_level` plus one table per level (TASK-K14). Mirrors the core
# parser (src/symphony/workflow/levels.py); the core re-checks on WORKFLOW.md load.
LEVEL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_LEVEL_KEYS = ("claude", "codex", "max_turns", "max_state_turns")
_BACKEND_KEYS = ("model", "effort")
# Keys that moved; a stale project.toml gets a hint instead of "unknown key".
_MOVED_KEYS = {("checks", "test_timeout_s"): "[timeouts] tests_s"}
_REQUIRED_SECTIONS = ("project", "tools")


class ProjectError(Exception):
    """A refusal with a message for the operator (in Russian)."""


def _check_value(where: str, value: object, kind: type) -> None:
    if kind is bool:
        if not isinstance(value, bool):
            raise ProjectError(f"{where}: нужно true или false")
    elif kind is str:
        if not isinstance(value, str) or not value.strip() or "\n" in value:
            raise ProjectError(f"{where}: нужна непустая строка в одну строку")
    elif kind is int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ProjectError(f"{where}: нужно целое число больше нуля")
    elif not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ProjectError(f"{where}: нужен список непустых строк")


def _check_allowed_bash(entries: list[str]) -> list[str]:
    """[agent] allowed_bash: command prefixes for `Bash(<entry>:*)`, de-duplicated."""
    where = f"{PROJECT_FILE}: [agent] allowed_bash"
    checked: list[str] = []
    for raw in entries:
        entry = raw.strip()
        if not entry or any(ord(ch) < 32 for ch in entry):
            raise ProjectError(f"{where}: пустая запись или управляющие символы в {raw!r}")
        if "(" in entry or ")" in entry:
            raise ProjectError(
                f"{where}: {raw!r}: пиши команду без обёртки Bash(...), "
                "например \".venv/bin/python -m ruff\""
            )
        if entry.endswith(":*"):
            raise ProjectError(f"{where}: {raw!r}: суффикс :* добавляется сам, убери его")
        if entry.lower() in _BARE_WILDCARDS:
            raise ProjectError(
                f"{where}: {raw!r} разрешает любую команду; "
                "перечисли нужные команды целиком (проверки разрешений агента не отключаются)"
            )
        if _PERMISSION_BYPASS_RE.search(entry):
            raise ProjectError(
                f"{where}: {raw!r} похоже на отключение проверок разрешений агента; "
                "это запрещено правилами проекта"
            )
        if entry not in checked:
            checked.append(entry)
    return checked


def _check_levels(raw: object) -> dict:
    """[levels]: `default_level` (a level name) and tables `[levels.<name>]`."""
    where = f"{PROJECT_FILE}: [levels]"
    if not isinstance(raw, dict):
        raise ProjectError(f"{where} должен быть разделом")
    default = raw.get("default_level")
    levels = {name: body for name, body in raw.items() if name != "default_level"}
    if "default_level" in raw and not isinstance(default, str):
        raise ProjectError(f"{where} default_level: нужно имя уровня (непустая строка)")
    for name, body in levels.items():
        label = f"{where.rstrip(']')}.{name}]"
        if not LEVEL_NAME_RE.match(name):
            raise ProjectError(f"{label}: имя уровня только из букв, цифр, _ и -")
        if not isinstance(body, dict):
            raise ProjectError(f"{where}: {name!r} должен быть таблицей [levels.{name}]")
        extra = sorted(set(body) - set(_LEVEL_KEYS))
        if extra:
            raise ProjectError(
                f"{label}: неизвестный ключ(и) {', '.join(extra)}; допустимы: "
                f"{', '.join(_LEVEL_KEYS)}. Уровни поддерживают только claude и codex; "
                "другой бэкенд карточке задают полем agent_kind"
            )
        for backend in ("claude", "codex"):
            part = body.get(backend, {})
            if not isinstance(part, dict):
                raise ProjectError(f"{label}: {backend} должен быть таблицей [levels.{name}.{backend}]")
            extra = sorted(set(part) - set(_BACKEND_KEYS))
            if extra:
                raise ProjectError(
                    f"{label}: неизвестный ключ(и) в {backend}: {', '.join(extra)}; "
                    f"допустимы: {', '.join(_BACKEND_KEYS)}"
                )
            for key, value in part.items():
                _check_value(f"{label} {backend}.{key}", value, str)
            if backend == "claude" and part.get("effort", "low") not in CLAUDE_EFFORTS:
                raise ProjectError(
                    f"{label}: claude.effort {part['effort']!r}; допустимо: {', '.join(CLAUDE_EFFORTS)}"
                )
            if "model" in part and not MODEL_RE.match(part["model"]):
                raise ProjectError(
                    f"{label}: {backend}.model {part['model']!r}: "
                    "только буквы, цифры и . _ : [ ] - (например haiku)"
                )
        for key in ("max_turns", "max_state_turns"):
            if key in body:
                _check_value(f"{label} {key}", body[key], int)
        if body.get("max_turns", ACTIVE_STATE_COUNT) < ACTIVE_STATE_COUNT:
            raise ProjectError(
                f"{label}: max_turns не меньше {ACTIVE_STATE_COUNT} "
                "(число активных состояний; иначе ядро не примет WORKFLOW.md)"
            )
    if "default_level" in raw:
        _check_value(f"{where} default_level", default, str)
        if default not in levels:
            raise ProjectError(
                f"{where} default_level {default!r}: такого уровня нет; есть: {', '.join(levels) or 'ни одного'}"
            )
    return {"default_level": default, "levels": levels}


def agent_bash_rules(board: Path, allowed_bash: Iterable[str]) -> list[str]:
    """`--allowedTools` rules: the built-in base, then the project's own."""
    base = [f"/bin/bash {board.resolve() / 'test.sh'}", *BASE_ALLOWED_BASH]
    rules: list[str] = []
    for entry in (*base, *allowed_bash):
        rule = f"Bash({entry}:*)"
        if rule not in rules:
            rules.append(rule)
    return rules


def stall_timeout_ms(tests_s: int) -> int:
    """agent stall watchdog (claude.stall_timeout_ms) from the test timeout.

    The core counts only `assistant` frames, lifecycle events and output-token
    deltas as progress (not `tool_result` echoes), so one long Bash call such
    as test.sh is silent for its whole duration.
    """
    return max(STALL_MIN_MS, (tests_s + STALL_MARGIN_S) * 1000)


def validate_project(data: dict) -> dict:
    """Validate parsed project.toml; fill [checks] and [timeouts] defaults."""
    unknown = sorted(set(data) - set(_SCHEMA) - {"levels"})
    if unknown:
        raise ProjectError(f"{PROJECT_FILE}: неизвестный раздел(ы) {', '.join(unknown)}")
    out: dict[str, dict] = {}
    for section, keys in _SCHEMA.items():
        raw = data.get(section, {})
        if section not in data and section in _REQUIRED_SECTIONS:
            raise ProjectError(f"{PROJECT_FILE}: нет раздела [{section}]")
        if not isinstance(raw, dict):
            raise ProjectError(f"{PROJECT_FILE}: [{section}] должен быть разделом")
        for key in raw:
            if (section, key) in _MOVED_KEYS:
                raise ProjectError(
                    f"{PROJECT_FILE}: ключ [{section}] {key} убран (TASK-K08b): "
                    f"перенеси значение в {_MOVED_KEYS[section, key]} "
                    "или удали строку и запусти project-doctor"
                )
        # A typo such as `forbiden_paths` must not silently drop a check.
        extra = sorted(set(raw) - set(keys))
        if extra:
            raise ProjectError(
                f"{PROJECT_FILE}: неизвестный ключ(и) в [{section}]: {', '.join(extra)}"
            )
        for key, (kind, required) in keys.items():
            if key in raw:
                _check_value(f"{PROJECT_FILE}: [{section}] {key}", raw[key], kind)
            elif required:
                raise ProjectError(f"{PROJECT_FILE}: нет ключа [{section}] {key}")
        out[section] = dict(raw)
    project, tools = out["project"], out["tools"]
    if not NAME_RE.match(project["name"]):
        raise ProjectError(f"{PROJECT_FILE}: имя {project['name']!r} не подходит под ^[a-z0-9][a-z0-9-]*$")
    if not PREFIX_RE.match(project["id_prefix"]):
        raise ProjectError(
            f"{PROJECT_FILE}: префикс {project['id_prefix']!r} не подходит под ^[A-Z][A-Z0-9]{{1,9}}$"
        )
    if project["base_branch"].startswith("-"):
        raise ProjectError(f"{PROJECT_FILE}: base_branch не может начинаться с '-'")
    if project["port"] > 65535:
        raise ProjectError(f"{PROJECT_FILE}: порт {project['port']} больше 65535")
    for where, path in (("[project] repo", project["repo"]), *(
        (f"[tools] {key}", tools[key]) for key in ("python", "symphony", "claude")
    )):
        if not Path(path).is_absolute():
            raise ProjectError(f"{PROJECT_FILE}: {where} должен быть абсолютным путём")
    out["levels"] = _check_levels(data["levels"]) if "levels" in data else {
        "default_level": None, "levels": {},
    }
    out["agent"]["allowed_bash"] = _check_allowed_bash(out["agent"].get("allowed_bash", []))
    if "model" in out["service"] and not MODEL_RE.match(out["service"]["model"]):
        raise ProjectError(
            f"{PROJECT_FILE}: [service] model {out['service']['model']!r}: "
            "только буквы, цифры и . _ : [ ] - (например haiku)"
        )
    out["timeouts"] = {**DEFAULT_TIMEOUTS, **out["timeouts"]}
    limits = out["limits"] = {**DEFAULT_LIMITS, **out["limits"]}
    if limits["max_turns"] < ACTIVE_STATE_COUNT:
        raise ProjectError(
            f"{PROJECT_FILE}: [limits] max_turns не меньше {ACTIVE_STATE_COUNT} "
            "(число активных состояний; иначе ядро не примет WORKFLOW.md)"
        )
    if limits["max_total_turns"] < limits["max_turns"]:
        raise ProjectError(f"{PROJECT_FILE}: [limits] max_total_turns меньше max_turns")
    return out


def load_project(board: Path) -> dict:
    path = board / PROJECT_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ProjectError(f"нет файла {path}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ProjectError(f"не читается {path}: {exc}") from None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ProjectError(f"{path}: ошибка TOML: {exc}") from None
    return validate_project(data)


def other_projects(root: Path) -> list[tuple[Path, dict]]:
    """(board folder, raw [project]) of every project.toml under `root`."""
    found = []
    for toml in sorted(root.glob(f"*/{PROJECT_FILE}")):
        try:
            data = tomllib.loads(toml.read_text(encoding="utf-8")).get("project", {})
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            print(f"предупреждение: не читается {toml}: {exc}", file=sys.stderr)
            continue
        found.append((toml.parent, data))
    return found


def exclude_text(id_prefix: str) -> str:
    return "".join(
        f"{line}\n"
        for line in (
            "/kanban",
            "/.venv/",
            "/.symphony-artifacts/",
            f"/docs/{id_prefix}-*",
            "/docs/llm-wiki",
        )
    )


def base_record_path(board: Path, issue_id: str) -> Path:
    return board / STATE_DIR / f"{issue_id}.base.json"


def read_base_record(board: Path, issue_id: str) -> dict:
    path = base_record_path(board, issue_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ProjectError(f"нет записи базы {path}") from None
    except (OSError, ValueError) as exc:
        raise ProjectError(f"запись базы {path} повреждена: {exc}") from None
    sha = data.get("base_sha") if isinstance(data, dict) else None
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", sha):
        raise ProjectError(f"запись базы {path}: нет корректного base_sha")
    return data


# ---------------------------------------------------------------------------
# test summary line (test.sh contract) and the baseline of the base branch
# ---------------------------------------------------------------------------

SUMMARY_PREFIX = "PIPELINE_TESTS"
SUMMARY_FORMAT = SUMMARY_PREFIX + " passed=<n> skipped=<n> failed=<n>"
_SUMMARY_RE = re.compile(
    rb"^" + SUMMARY_PREFIX.encode() + rb" passed=(\d+) skipped=(\d+) failed=(\d+)\s*$"
)
SUMMARY_KEYS = ("passed", "skipped", "failed")


def find_summary(lines: Iterable[bytes | str]) -> dict[str, int] | None:
    """Counts from the LAST summary line of test.sh output, or None."""
    found = None
    for line in lines:
        raw = line.encode("utf-8", errors="replace") if isinstance(line, str) else line
        match = _SUMMARY_RE.match(raw.rstrip(b"\r\n"))
        if match:
            found = dict(zip(SUMMARY_KEYS, (int(g) for g in match.groups())))
    return found


def triple(counts: dict) -> str:
    return " ".join(f"{key}={counts[key]}" for key in SUMMARY_KEYS)


def baseline_path(board: Path) -> Path:
    return board / STATE_DIR / BASELINE_FILE


def read_baseline(board: Path) -> dict:
    path = baseline_path(board)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ProjectError(f"нет замера базы {path}: запусти project-doctor") from None
    except (OSError, ValueError) as exc:
        raise ProjectError(f"замер базы {path} повреждён ({exc}): перезапусти project-doctor") from None
    ok = isinstance(data, dict) and isinstance(data.get("base_branch"), str) and all(
        isinstance(data.get(key), int) and not isinstance(data.get(key), bool) and data[key] >= 0
        for key in SUMMARY_KEYS
    )
    if not ok:
        raise ProjectError(f"замер базы {path} неполный: перезапусти project-doctor")
    return data


def recommend_timeouts(setup_s: float, tests_s: float) -> dict[str, int]:
    """[timeouts] from measured setup.sh and test.sh times (TASK-K08b)."""
    tests = max(300, math.ceil(2 * tests_s / 60) * 60)
    return {
        "tests_s": tests,
        "gate_ms": (tests + 60) * 1000,
        "hooks_ms": (max(math.ceil(1.5 * setup_s), tests) + 60) * 1000,
    }


_HEADER_RE = re.compile(r"^\s*\[")
_TIMEOUTS_HEADER_RE = re.compile(r"^\s*\[\s*timeouts\s*\]\s*(#.*)?$")


def update_timeouts_text(text: str, values: dict[str, int]) -> str:
    """Set [timeouts] keys in project.toml text; every other byte is kept.

    Inside [timeouts] an active `key = ...` line gets the new value (its
    inline comment stays), else the first commented `# key = ...` line is
    uncommented with the new value (its inline comment stays), else the key
    is added after the last key line touched. No [timeouts] section: one is
    appended at the end.
    """
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    start = next((i for i, line in enumerate(lines) if _TIMEOUTS_HEADER_RE.match(line)), None)
    if start is None:
        tail = "" if not text or text.endswith(("\n", "\r")) else newline
        body = "".join(f"{key} = {value}{newline}" for key, value in values.items())
        return f"{text}{tail}{newline}[timeouts]{newline}{body}"
    end = next((i for i in range(start + 1, len(lines)) if _HEADER_RE.match(lines[i])), len(lines))
    insert_at = start + 1
    for key, value in values.items():
        active = re.compile(rf"^(\s*{key}\s*=\s*)([^#\r\n]*?)(\s*#[^\r\n]*)?(\r?\n)?$")
        commented = re.compile(rf"^(\s*)#\s*{key}\s*=\s*([^#\r\n]*?)(\s*#[^\r\n]*)?(\r?\n)?$")
        hit = next((i for i in range(start + 1, end) if active.match(lines[i])), None)
        if hit is not None:
            m = active.match(lines[hit])
            assert m is not None
            lines[hit] = f"{m.group(1)}{value}{m.group(3) or ''}{m.group(4) or ''}"
            insert_at = max(insert_at, hit + 1)
            continue
        hit = next((i for i in range(start + 1, end) if commented.match(lines[i])), None)
        if hit is not None:
            m = commented.match(lines[hit])
            assert m is not None
            # `# tests_s = 900   # note` -> `tests_s = 300   # note`
            lines[hit] = f"{m.group(1)}{key} = {value}{m.group(3) or ''}{m.group(4) or ''}"
            insert_at = max(insert_at, hit + 1)
            continue
        if insert_at > 0 and not lines[insert_at - 1].endswith(("\n", "\r")):
            lines[insert_at - 1] += newline
        lines.insert(insert_at, f"{key} = {value}{newline}")
        insert_at += 1
        end += 1
    return "".join(lines)


def ensure_state_dir(board: Path) -> Path:
    state = board / STATE_DIR
    state.mkdir(exist_ok=True)
    ignore = state / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")
    return state


# ---------------------------------------------------------------------------
# WORKFLOW.md rendering
# ---------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"\{\{(yaml|sh|int|raw):([a-z_]+)\}\}")


def levels_yaml(project: dict) -> str:
    """The workflow `levels:` block ("" without [levels]); strings as JSON scalars."""
    levels = project["levels"]["levels"]
    if not levels:
        return ""
    q = json.dumps
    lines = ["levels:"]
    for name, body in levels.items():
        if not body:
            lines.append(f"  {name}: {{}}")
            continue
        lines.append(f"  {name}:")
        for backend in ("claude", "codex"):
            part = body.get(backend)
            if part:
                lines.append(f"    {backend}:")
                lines.extend(f"      {k}: {q(v, ensure_ascii=False)}" for k, v in part.items())
        for key in ("max_turns", "max_state_turns"):
            if key in body:
                lines.append(f"    {key}: {body[key]}")
    return "\n".join(lines) + "\n"


def template_values(board: Path, project: dict) -> dict[str, object]:
    p, tools, timeouts = project["project"], project["tools"], project["timeouts"]
    limits = project["limits"]
    board = board.resolve()
    hooks = OURS_DIR / "hooks"
    q = shlex.quote
    claude = " ".join(
        [q(tools["claude"]), "-p", "--output-format", "stream-json", "--verbose",
         "--permission-mode", "acceptEdits", "--allowedTools"]
        + [q(rule) for rule in agent_bash_rules(board, project["agent"]["allowed_bash"])]
        + [f"--add-dir {q(str(board / d))}" for d in (BOARD_DIR, ARTIFACTS_DIR, WIKI_DIR)]
    )
    values: dict[str, object] = {
        "name": p["name"],
        "base": p["base_branch"],
        "port": p["port"],
        "code_repo": p["repo"],
        "workspace_root": str(Path.home() / "symphony_workspaces" / p["name"]),
        "worktree_py": str(hooks / "worktree.py"),
        "check_py": str(hooks / "check.py"),
        "claude_command": claude,
        "done_gate_command": f'"$SYMPHONY_PYTHON" {q(str(hooks / "check.py"))} gate',
        "gate_ms": timeouts["gate_ms"],
        "stall_ms": stall_timeout_ms(timeouts["tests_s"]),
        "hooks_ms": timeouts["hooks_ms"],
        "max_turns": limits["max_turns"],
        "max_total_turns": limits["max_total_turns"],
        "max_attempts": limits["max_attempts"],
        "max_state_turns": limits["max_state_turns"],
        "levels_block": levels_yaml(project),
        "default_level_line": (
            f"default_level: {json.dumps(default)}\n  " if (default := project["levels"]["default_level"]) else ""
        ),
        "prompt_base": str(PROMPTS_DIR / "base.md"),
    }
    for stage in ("todo", "in-progress", "verify", "document", "done"):
        values["prompt_" + stage.replace("-", "_")] = str(PROMPTS_DIR / "stages" / f"{stage}.md")
    return values


def render_workflow(board: Path, project: dict, template: Path = TEMPLATE) -> str:
    values = template_values(board, project)

    def substitute(match: re.Match[str]) -> str:
        kind, key = match.groups()
        if key not in values:
            raise ProjectError(f"шаблон {template}: неизвестная подстановка {match.group(0)}")
        value = values[key]
        if kind == "raw":
            return str(value)
        if kind == "int":
            if not isinstance(value, int):
                raise ProjectError(f"шаблон: {key} не число")
            return str(value)
        text = str(value)
        # JSON strings are valid YAML double-quoted scalars.
        return json.dumps(text, ensure_ascii=False) if kind == "yaml" else shlex.quote(text)

    return _PLACEHOLDER.sub(substitute, template.read_text(encoding="utf-8"))
