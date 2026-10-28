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
                          baselines.json, locks, logs)

Used by ours/bin/project-new, ours/bin/project-render, ours/bin/
project-doctor, worktree.py and check.py. Standard library only,
Python >= 3.11.
"""

from __future__ import annotations

import fnmatch
import json
import math
import os
import re
import shlex
import stat
import sys
import tempfile
import tomllib
from collections.abc import Callable, Iterable
from pathlib import Path

OURS_DIR = Path(__file__).resolve().parents[1]
# The folder of the fork that holds ours/ (the main folder when run from the service).
FORK_DIR = OURS_DIR.parent
TEMPLATE = OURS_DIR / "templates" / "WORKFLOW.md.tmpl"
PROMPTS_DIR = OURS_DIR / "prompts"

PROJECT_FILE = "project.toml"
BOARD_DIR = "kanban"
ARTIFACTS_DIR = "artifacts"
WIKI_DIR = "wiki"
EXCLUDE_FILE = "worktree-exclude"
STATE_DIR = ".ours-state"
BASELINE_FILE = "baseline.json"
BASELINES_FILE = "baselines.json"
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
# Active states of the template (Todo, Plan, In Progress, Verify, Document): the
# core refuses agent.max_turns, and max_turns of a level, below this. A test
# keeps it equal to the length of `active_states` in the rendered WORKFLOW.md.
ACTIVE_STATE_COUNT = 5
# Stages on the longest path of one card (the ordinary card: Todo, In Progress,
# Verify, Document; a worker has 3, a manager: Plan, then Verify, Document).
# Not the same number as above: a card never visits every active state.
STAGES_ON_LONGEST_PATH = 4

# [agent] max_concurrent_agents: 1 keeps one subtask at a time, which the merge
# into the integration branch (S4-12) relies on.
DEFAULT_MAX_CONCURRENT_AGENTS = 1

# [integration] (S4-11): subtasks start from the task's integration branch.
DEFAULT_INTEGRATION = {"enabled": False, "branch_prefix": "integration/", "worker_label": "role:worker"}
# Prefixes worktree.py owns: a branch under them is renamed or removed by the hook.
RESERVED_BRANCH_PREFIXES = ("symphony/", "attempts/")

# [plan] (S4-19): plan.py checks the manager's plan.toml. max_failures counts
# plan rejections before needs_human (like [checks] max_check_failures).
DEFAULT_PLAN = {"max_subtasks": 5, "max_failures": 2}
MAX_SUBTASKS_LIMIT = 20

# [delivery] (S4-27): where `ours/bin/deliver` pushes the result of a card. The
# section is optional; without it the board has no delivery.
DELIVERY_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
DELIVERY_BASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,80}$")

# [billing] (E1b): how the agent's turns are paid. `plan` = the subscription (the login
# of the user, nothing is passed); `api` = an API key of a Console workspace, given to
# `claude` only through `apiKeyHelper` of a settings file of the board.
BILLING_MODES = ("plan", "api")
DEFAULT_BILLING = {"mode": "plan"}
# The path goes into a command for /bin/sh and into a permission rule: a plain alphabet only.
KEY_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")
# What an Anthropic API key looks like; check.py searches diffs with it too.
API_KEY_PATTERN = r"sk-ant-[A-Za-z0-9_-]{20,}"
API_KEY_RE = re.compile(API_KEY_PATTERN)
KEY_FILE_MAX_BYTES = 1024
CLAUDE_SETTINGS_FILE = "claude-settings.json"
# In the environment of the service these beat apiKeyHelper and the subscription login.
KEY_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
QUOTA_NOTE = (
    "API-кредит исчерпан или ключ недоступен (apiKeyHelper) — переключи доску на подписку: "
    "в project.toml раздел [billing] mode = \"plan\", затем ours/bin/project-render <имя>. "
    "Остальные карточки ждут: служба не берёт новые, пока WORKFLOW.md не пересобран. "
    "Автоматически доска на подписку не переключается."
)

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
        "allow_paths": (list, False),
        "test_globs": (list, False),
        "allow_modified_tests": (list, False),
        "required_sections": (list, False),
        "worker_required_sections": (list, False),
        "doc_paths": (list, False),
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
    "agent": {"allowed_bash": (list, False), "max_concurrent_agents": (int, False)},
    "integration": {
        "enabled": (bool, False),
        "branch_prefix": (str, False),
        "worker_label": (str, False),
    },
    "plan": {"max_subtasks": (int, False), "max_failures": (int, False)},
    "delivery": {"repo": (str, False), "base": (str, False)},
    "service": {"model": (str, False)},
    "billing": {"mode": (str, False), "api_key_file": (str, False)},
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


def _check_branch_prefix(prefix: str) -> None:
    """[integration] branch_prefix: a directory-like prefix no hook already owns."""
    where = f"{PROJECT_FILE}: [integration] branch_prefix {prefix!r}"
    if not prefix.endswith("/"):
        raise ProjectError(f"{where}: должен заканчиваться на /")
    if prefix.startswith("-"):
        raise ProjectError(f"{where}: не может начинаться с '-'")
    if any(ch.isspace() for ch in prefix) or ".." in prefix:
        raise ProjectError(f"{where}: без пробелов и '..'")
    if prefix in RESERVED_BRANCH_PREFIXES:
        raise ProjectError(
            f"{where}: префикс занят хуком worktree.py "
            f"({', '.join(RESERVED_BRANCH_PREFIXES)}); возьми другой, например integration/"
        )


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
                f"{label}: max_turns не меньше числа активных статусов доски ({ACTIVE_STATE_COUNT}); "
                "иначе ядро не примет WORKFLOW.md"
            )
    if "default_level" in raw:
        _check_value(f"{where} default_level", default, str)
        if default not in levels:
            raise ProjectError(
                f"{where} default_level {default!r}: такого уровня нет; есть: {', '.join(levels) or 'ни одного'}"
            )
    return {"default_level": default, "levels": levels}


# ---------------------------------------------------------------------------
# path patterns (E2): one syntax for [checks] forbidden_paths / test_globs / ... and
# [baseline] paths. `*` and `?` stay inside one path segment, `**` spans any number of
# segments (zero included). Patterns are anchored at the repository root and
# matched case-insensitively (macOS file systems are case-insensitive).
# ---------------------------------------------------------------------------

# Files that count as tests when [checks] test_globs is not set. With [baseline] every
# one of the test globs of the board must also be in [baseline] paths (_check_baseline).
DEFAULT_TEST_GLOBS = (
    "tests/**",
    "**/test_*.py",
    "**/*_test.py",
    "**/*.test.ts",
    "**/*.test.tsx",
    "**/*_test.go",
    "**/*.spec.*",
)


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
        raise ProjectError(f"пустой шаблон пути (empty glob pattern) {pattern!r}")
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


def matcher(patterns: Iterable[str]) -> Callable[[str], bool]:
    compiled = [compile_glob(p) for p in patterns]
    return lambda path: any(rx.fullmatch(path) for rx in compiled)


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
    """Validate parsed project.toml; fill [timeouts], [limits], [integration], [plan] and [agent] defaults."""
    unknown = sorted(set(data) - set(_SCHEMA) - {"levels", "baseline"})
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
    agents = out["agent"].setdefault("max_concurrent_agents", DEFAULT_MAX_CONCURRENT_AGENTS)
    if "model" in out["service"] and not MODEL_RE.match(out["service"]["model"]):
        raise ProjectError(
            f"{PROJECT_FILE}: [service] model {out['service']['model']!r}: "
            "только буквы, цифры и . _ : [ ] - (например haiku)"
        )
    out["timeouts"] = {**DEFAULT_TIMEOUTS, **out["timeouts"]}
    integration = out["integration"] = {**DEFAULT_INTEGRATION, **out["integration"]}
    _check_branch_prefix(integration["branch_prefix"])
    plan = out["plan"] = {**DEFAULT_PLAN, **out["plan"]}
    if plan["max_subtasks"] > MAX_SUBTASKS_LIMIT:
        raise ProjectError(
            f"{PROJECT_FILE}: [plan] max_subtasks = {plan['max_subtasks']}: "
            f"допустимо от 1 до {MAX_SUBTASKS_LIMIT}"
        )
    _check_delivery(data.get("delivery"), out["delivery"])
    out["baseline"] = _check_baseline(data.get("baseline"), data.get("checks"))
    out["billing"] = _check_billing(out["billing"])
    if integration["enabled"] and agents != 1:
        raise ProjectError(
            f"{PROJECT_FILE}: [agent] max_concurrent_agents = {agents} при [integration] enabled = true: "
            "интеграционная ветка: вливание и Done подзадачи рассчитаны на одного агента "
            "(одно вливание за раз); выключи интеграцию или оставь max_concurrent_agents = 1"
        )
    limits = out["limits"] = {**DEFAULT_LIMITS, **out["limits"]}
    if limits["max_turns"] < ACTIVE_STATE_COUNT:
        raise ProjectError(
            f"{PROJECT_FILE}: [limits] max_turns не меньше числа активных статусов доски ({ACTIVE_STATE_COUNT}); "
            "иначе ядро не примет WORKFLOW.md"
        )
    attempts = limits["max_attempts"]
    _check_turns_cover_attempts(f"{PROJECT_FILE}: [limits] max_turns", limits["max_turns"], attempts)
    for name, body in out["levels"]["levels"].items():
        if "max_turns" in body:
            _check_turns_cover_attempts(f"{PROJECT_FILE}: [levels.{name}] max_turns", body["max_turns"], attempts)
    if limits["max_total_turns"] < limits["max_turns"]:
        raise ProjectError(f"{PROJECT_FILE}: [limits] max_total_turns меньше max_turns")
    return out


def _check_delivery(raw: object, delivery: dict) -> None:
    """[delivery]: both keys or no section; `repo` is `owner/name`, `base` a plain branch name."""
    if raw is None:
        return
    where = f"{PROJECT_FILE}: [delivery]"
    for key in ("repo", "base"):
        if key not in delivery:
            raise ProjectError(f"{where}: нет ключа {key}")
    repo, base = delivery["repo"], delivery["base"]
    if not DELIVERY_REPO_RE.match(repo) or any(part in (".", "..") for part in repo.split("/")):
        raise ProjectError(
            f"{where} repo {repo!r}: нужна строка вида владелец/имя из букв, цифр и . _ -"
        )
    if (
        not DELIVERY_BASE_RE.match(base) or ".." in base or "//" in base
        or base.endswith(("/", ".", ".lock"))
    ):
        raise ProjectError(f"{where} base {base!r}: нужно простое имя ветки (например our-main)")


def _check_baseline(raw: object, checks: object) -> dict:
    """[baseline] paths (E2): {} without the section, else {"paths": [...]}.

    The paths replace the Python rule BASELINE_RULE as a whole. Every test glob of the
    board ([checks] test_globs, else DEFAULT_TEST_GLOBS) must be in `paths` literally:
    a file protected as a test must also make post-merge measure again and make an
    older measure stale.
    """
    if raw is None:
        return {}
    where = f"{PROJECT_FILE}: [baseline]"
    if not isinstance(raw, dict):
        raise ProjectError(f"{where} должен быть разделом")
    extra = sorted(set(raw) - {"paths"})
    if extra:
        raise ProjectError(f"{PROJECT_FILE}: неизвестный ключ(и) в [baseline]: {', '.join(extra)}")
    if "paths" not in raw:
        raise ProjectError(f"{PROJECT_FILE}: нет ключа [baseline] paths")
    paths = raw["paths"]
    _check_value(f"{where} paths", paths, list)
    if not paths:
        raise ProjectError(f"{where} paths: список пуст; перечисли шаблоны файлов, меняющих исход тестов")
    for pattern in paths:
        segments = pattern.replace("\\", "/").split("/")
        if pattern.replace("\\", "/").startswith("/") or ".." in segments:
            raise ProjectError(
                f"{where} paths: {pattern!r}: шаблон от корня репозитория кода, "
                "без ведущего / и без '..'"
            )
        try:
            compile_glob(pattern)
        except ProjectError as exc:
            raise ProjectError(f"{where} paths: {pattern!r}: {exc}") from None
    globs = checks.get("test_globs", list(DEFAULT_TEST_GLOBS)) if isinstance(checks, dict) else list(DEFAULT_TEST_GLOBS)
    _check_value(f"{PROJECT_FILE}: [checks] test_globs", globs, list)
    missing = [glob for glob in dict.fromkeys(globs) if glob not in paths]
    if missing:
        source = "[checks] test_globs" if isinstance(checks, dict) and "test_globs" in checks else (
            "шаблоны тестов по умолчанию (в project.toml нет [checks] test_globs)"
        )
        raise ProjectError(
            f"{where} paths: нет шаблонов из {source}: {', '.join(missing)}. "
            "Файл, который защищён как тест, обязан и вызывать перемер базы, и обесценивать "
            "старый замер: добавь их в paths (или сузь test_globs)"
        )
    return {"paths": list(paths)}


def _check_billing(raw: dict) -> dict:
    """[billing]: `mode` (default plan) and, for `api`, the absolute path of the key file.

    Only the form is checked here: a missing or loose file must not break every hook
    of the board. The file and its folder are checked by `billing_key_problems`
    (project-render, project-doctor).
    """
    where = f"{PROJECT_FILE}: [billing]"
    billing = {**DEFAULT_BILLING, **raw}
    if billing["mode"] not in BILLING_MODES:
        raise ProjectError(
            f"{where} mode {billing['mode']!r}: допустимо {' или '.join(repr(m) for m in BILLING_MODES)}"
        )
    path = billing.get("api_key_file")
    if path is None:
        if billing["mode"] == "api":
            raise ProjectError(f"{where}: при mode = \"api\" нужен ключ api_key_file (путь к файлу с ключом)")
        return billing
    if not KEY_PATH_RE.match(path) or ".." in path.split("/"):
        raise ProjectError(
            f"{where} api_key_file {path!r}: нужен абсолютный путь без '..' из букв, цифр и . _ / -"
        )
    return billing


def _check_turns_cover_attempts(label: str, max_turns: int, max_attempts: int) -> None:
    """max_turns must fit the stages of one card's longest path plus `max_attempts` returns.

    A return re-runs the stages after it, so a card that uses all its returns
    needs up to STAGES_ON_LONGEST_PATH * (1 + max_attempts) turns to reach Human
    Review. `max_attempts = 0` means returns are unlimited: the active-state
    count rule alone applies (a loop is stopped by max_state_turns).
    """
    if max_attempts <= 0:
        return
    needed = STAGES_ON_LONGEST_PATH * (1 + max_attempts)
    if max_turns < needed:
        raise ProjectError(
            f"{label} = {max_turns} меньше {STAGES_ON_LONGEST_PATH} × (1 + max_attempts) = {needed} "
            f"({STAGES_ON_LONGEST_PATH} — этапов на самом длинном пути карточки): "
            f"карточка с max_attempts = {max_attempts} возвратами не успеет дойти до Human Review; "
            f"увеличь max_turns до {needed} или уменьши max_attempts"
        )


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


def other_projects(root: Path, unreadable: list[tuple[Path, str]] | None = None) -> list[tuple[Path, dict]]:
    """(board folder, raw [project]) of every project.toml under `root`.

    A project.toml that cannot be read is skipped with a warning; with `unreadable` given
    the caller gets (board folder, reason) instead and decides (post-merge refuses).
    """
    found = []
    for toml in sorted(root.glob(f"*/{PROJECT_FILE}")):
        try:
            data = tomllib.loads(toml.read_text(encoding="utf-8")).get("project", {})
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            if unreadable is None:
                print(f"предупреждение: не читается {toml}: {exc}", file=sys.stderr)
            else:
                unreadable.append((toml.parent, f"не читается {toml}: {exc}"))
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


def default_workspace_root(project: dict) -> Path:
    """Where the service puts the working copies of this project (`workspace.root`
    of the rendered WORKFLOW.md)."""
    return Path.home() / "symphony_workspaces" / project["project"]["name"]


_SHA_RE = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")


def is_sha(value: object) -> bool:
    return isinstance(value, str) and _SHA_RE.fullmatch(value) is not None


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
    if not is_sha(sha):
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


# ---------------------------------------------------------------------------
# history of baselines (S4-25): <board>/.ours-state/baselines.json
# ---------------------------------------------------------------------------

BASELINES_VERSION = 1
BASELINES_KEEP = 50
# What the tests of the project measure: a change to one of these files makes
# an older baseline stale. One rule for post-merge ("measure again?") and
# check.py ("trust the older baseline?"). Written for Python projects; a project
# in another language sets its own in project.toml [baseline] paths (E2), which
# replaces this rule.
BASELINE_RULE = {
    "prefixes": ("tests/", "ours/tests/"),
    "files": ("conftest.py", "pytest.ini", "pyproject.toml", "uv.lock",
              "package.json", "package-lock.json"),
    "basenames": ("test_*.py", "*_test.py", "conftest.py"),
}


def _python_rule(path: str) -> bool:
    rule = BASELINE_RULE
    return (
        path.startswith(rule["prefixes"]) or path in rule["files"]
        or any(fnmatch.fnmatch(Path(path).name, p) for p in rule["basenames"])
    )


def paths_rule(paths: Iterable[str] | None) -> Callable[[str], bool]:
    """The rule "does this file change what the tests measure": [baseline] paths,
    or the Python rule BASELINE_RULE when there are none (no section)."""
    return matcher(paths) if paths is not None else _python_rule


def affects_baseline(path: str, project: dict | None = None) -> bool:
    """Whether a changed file (repository-relative, `/`-separated) can change test results.

    `project` is a validated project.toml; its [baseline] paths, when set, replace the
    Python rule as a whole. For several paths use paths_rule: it compiles the patterns once.
    """
    return paths_rule((project or {}).get("baseline", {}).get("paths"))(path)


def load_baseline_rule(board: Path) -> Callable[[str], bool]:
    """The rule of a board read from its project.toml alone (post-merge: a project
    whose other sections are not valid is still served as before). Raises ProjectError
    when the file cannot be read or parsed or [baseline] is wrong."""
    path = board / PROJECT_FILE
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ProjectError(f"нет файла {path}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ProjectError(f"не читается {path}: {exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ProjectError(f"{path}: ошибка TOML: {exc}") from None
    return paths_rule(_check_baseline(data.get("baseline"), data.get("checks")).get("paths"))


def baselines_path(board: Path) -> Path:
    return board / STATE_DIR / BASELINES_FILE


def _check_baseline_record(record: object) -> bool:
    return (
        isinstance(record, dict)
        and isinstance(record.get("base_branch"), str)
        and is_sha(record.get("base_sha"))
        and all(
            isinstance(record.get(key), int) and not isinstance(record.get(key), bool)
            and record[key] >= 0
            for key in SUMMARY_KEYS
        )
    )


def read_baselines(board: Path) -> list[dict] | None:
    """Records of baselines.json, oldest first; None when the file does not exist."""
    path = baselines_path(board)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ProjectError(f"история замеров {path} повреждена ({exc}): перезапусти project-doctor") from None
    if not isinstance(data, dict) or data.get("version") != BASELINES_VERSION:
        raise ProjectError(f"история замеров {path}: неизвестная версия: перезапусти project-doctor")
    records = data.get("records")
    if not isinstance(records, list) or not all(_check_baseline_record(r) for r in records):
        raise ProjectError(
            f"история замеров {path}: запись без нужных полей или с неверным base_sha: "
            "перезапусти project-doctor"
        )
    return records


def add_baseline(records: list[dict], new: dict) -> list[dict]:
    """`records` plus `new` at the end: one record per commit of a branch (a
    repeated measure replaces the old one), the last BASELINES_KEEP kept."""
    kept = [
        r for r in records
        if (r["base_branch"], r["base_sha"]) != (new["base_branch"], new["base_sha"])
    ]
    return [*kept, new][-BASELINES_KEEP:]


def is_real_baseline(data: dict) -> bool:
    """A baseline.json whose commit is a real one (not a placeholder of zeros)."""
    return is_sha(data.get("base_sha")) and set(data["base_sha"]) != {"0"}


def baselines_text(records: list[dict]) -> str:
    return json.dumps({"version": BASELINES_VERSION, "records": records},
                      ensure_ascii=False, indent=2) + "\n"


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
# [billing] mode = "api": the key file, the settings file of `claude` (E1b)
# ---------------------------------------------------------------------------


def billing_mode(project: dict) -> str:
    return project.get("billing", DEFAULT_BILLING)["mode"]


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def key_folder(project: dict) -> Path:
    """The folder of the key file: it holds keys only, one deny rule closes all of it."""
    return Path(project["billing"]["api_key_file"]).parent


def api_key_helper_command(project: dict) -> str:
    """`apiKeyHelper`: prints the key without spaces and the line feed (a command for /bin/sh)."""
    return f"/usr/bin/tr -d '[:space:]' < '{project['billing']['api_key_file']}'"


def claude_settings_text(project: dict) -> str:
    """Content of <board>/claude-settings.json: the helper and a deny rule on the key folder."""
    folder = str(key_folder(project)).lstrip("/")
    data = {
        "apiKeyHelper": api_key_helper_command(project),
        "permissions": {"deny": [f"Read(//{folder}/**)"]},
    }
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _private(info: os.stat_result) -> bool:
    return info.st_uid == os.getuid() and not info.st_mode & 0o077


def billing_key_problems(board: Path, project: dict, fork_dir: Path | None = None) -> list[str]:
    """What is wrong with the key file and its folder (in Russian); empty = accepted.

    The file: exists, a regular file (not a link), ours, closed to group and others,
    up to 1 KB, not empty, outside the code repository, the board folder, the
    working copies and the fork folder. The folder holds keys only: not the home
    folder and not a parent of any of those (the deny rule would shut the agent out
    of its own work), ours, closed to group and others. No link anywhere in the path.
    Never reads the key.
    """
    path = Path(project["billing"]["api_key_file"])
    folder = key_folder(project)
    fork = (fork_dir or FORK_DIR).resolve()
    work = {
        "репозиторий кода [project] repo": Path(project["project"]["repo"]).resolve(),
        "папка доски": board.resolve(),
        "корень рабочих копий": default_workspace_root(project).resolve(),
        "основная папка форка": fork,
    }
    problems: list[str] = []
    if path.resolve() != path:
        problems.append(
            f"путь к ключу {path} содержит символьную ссылку (настоящий путь {path.resolve()}): "
            "укажи настоящий путь, иначе правило deny не закроет файл"
        )
    try:
        info = path.lstat()
    except OSError:
        problems.append(f"файл ключа {path} не найден")
        info = None
    if info is not None:
        if not stat.S_ISREG(info.st_mode):
            problems.append(f"файл ключа {path}: нужен обычный файл (не ссылка и не папка)")
        else:
            if info.st_uid != os.getuid():
                problems.append(f"файл ключа {path}: владелец не текущий пользователь")
            if info.st_mode & 0o077:
                problems.append(
                    f"файл ключа {path}: права {stat.S_IMODE(info.st_mode):03o} шире 600 "
                    f"(исправь: chmod 600 {path})"
                )
            if info.st_size > KEY_FILE_MAX_BYTES:
                problems.append(f"файл ключа {path}: больше {KEY_FILE_MAX_BYTES} байт — это не ключ")
            elif info.st_size == 0:
                problems.append(f"файл ключа {path} пуст: вставь ключ редактором")
    for label, where in work.items():
        if _inside(path.resolve(), where):
            problems.append(f"файл ключа {path} лежит внутри: {label} ({where}); нужна отдельная папка для ключей")
        if _inside(where, folder.resolve()):
            problems.append(
                f"папка ключей {folder} совпадает с папкой «{label}» ({where}) или содержит её: "
                "правило deny закрыло бы агенту его собственную работу; папка нужна только для ключей"
            )
    if _inside(Path.home().resolve(), folder.resolve()):
        problems.append(
            f"папка ключей {folder} — домашняя папка или её родитель: нужна отдельная папка только для "
            "ключей (например ~/.symphony-keys)"
        )
    try:
        folder_info = folder.lstat()
    except OSError:
        problems.append(f"папки ключей {folder} нет")
    else:
        if not stat.S_ISDIR(folder_info.st_mode):
            problems.append(f"папка ключей {folder}: нужна папка")
        elif not _private(folder_info):
            problems.append(
                f"папка ключей {folder}: права {stat.S_IMODE(folder_info.st_mode):03o} или чужой владелец; "
                f"нужны права без доступа группе и остальным (chmod 700 {folder})"
            )
    return problems


def _write_private(path: Path, text: str) -> None:
    """Atomically, with mode 600 from the start."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def sync_claude_settings(board: Path, project: dict) -> str:
    """Make <board>/claude-settings.json match the mode; returns what was done:
    "written" | "removed" | "unchanged"."""
    path = board / CLAUDE_SETTINGS_FILE
    if billing_mode(project) == "api":
        text = claude_settings_text(project)
        try:
            if path.read_text(encoding="utf-8") == text and stat.S_IMODE(path.stat().st_mode) == 0o600:
                return "unchanged"
        except (OSError, UnicodeDecodeError):
            pass
        _write_private(path, text)
        return "written"
    try:
        path.unlink()
    except FileNotFoundError:
        return "unchanged"
    return "removed"


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
    quota_lines = ""
    if billing_mode(project) == "api":
        problems = billing_key_problems(board, project)
        if problems:
            raise ProjectError(
                f"{PROJECT_FILE}: [billing] mode = \"api\": ключ не принят, WORKFLOW.md не пересобран:\n  - "
                + "\n  - ".join(problems)
            )
        claude += f" --settings {q(str(board / CLAUDE_SETTINGS_FILE))}"
        quota_lines = (
            '# [billing] mode = "api": an exhausted credit moves the card to needs_human and holds '
            "the board until this file changes.\n  "
            "quota_exhausted_state: needs_human\n"
            f"  quota_exhausted_note: {json.dumps(QUOTA_NOTE, ensure_ascii=False)}\n  "
        )
    values: dict[str, object] = {
        "name": p["name"],
        "base": p["base_branch"],
        "port": p["port"],
        "code_repo": p["repo"],
        "workspace_root": str(default_workspace_root(project)),
        "worktree_py": str(hooks / "worktree.py"),
        "check_py": str(hooks / "check.py"),
        "integrate_py": str(hooks / "integrate.py"),
        "plan_py": str(hooks / "plan.py"),
        "claude_command": claude,
        "quota_lines": quota_lines,
        "done_gate_command": f'"$SYMPHONY_PYTHON" {q(str(hooks / "check.py"))} gate',
        "gate_ms": timeouts["gate_ms"],
        "stall_ms": stall_timeout_ms(timeouts["tests_s"]),
        "hooks_ms": timeouts["hooks_ms"],
        "max_concurrent_agents": project["agent"]["max_concurrent_agents"],
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
    for stage in ("todo", "plan", "in-progress", "verify", "document", "done"):
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
