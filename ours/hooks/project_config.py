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
        .ours-state/      state of our scripts (base records, locks, logs)

Used by ours/bin/project-new, ours/bin/project-render, worktree.py and
check.py. Standard library only, Python >= 3.11.
"""

from __future__ import annotations

import json
import re
import shlex
import tomllib
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
DEFAULT_ROOT = Path.home() / "symphony-projects"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")

DEFAULT_TIMEOUTS = {"tests_s": 900, "gate_ms": 960000, "hooks_ms": 1200000}

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
        "test_timeout_s": (int, False),
    },
    "timeouts": {"tests_s": (int, False), "gate_ms": (int, False), "hooks_ms": (int, False)},
}
_REQUIRED_SECTIONS = ("project", "tools")


class ProjectError(Exception):
    """A refusal with a message for the operator (in Russian)."""


def _check_value(where: str, value: object, kind: type) -> None:
    if kind is str:
        if not isinstance(value, str) or not value.strip() or "\n" in value:
            raise ProjectError(f"{where}: нужна непустая строка в одну строку")
    elif kind is int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ProjectError(f"{where}: нужно целое число больше нуля")
    elif not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ProjectError(f"{where}: нужен список непустых строк")


def validate_project(data: dict) -> dict:
    """Validate parsed project.toml; fill [checks] and [timeouts] defaults."""
    unknown = sorted(set(data) - set(_SCHEMA))
    if unknown:
        raise ProjectError(f"{PROJECT_FILE}: неизвестный раздел(ы) {', '.join(unknown)}")
    out: dict[str, dict] = {}
    for section, keys in _SCHEMA.items():
        raw = data.get(section, {})
        if section not in data and section in _REQUIRED_SECTIONS:
            raise ProjectError(f"{PROJECT_FILE}: нет раздела [{section}]")
        if not isinstance(raw, dict):
            raise ProjectError(f"{PROJECT_FILE}: [{section}] должен быть разделом")
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
    out["timeouts"] = {**DEFAULT_TIMEOUTS, **out["timeouts"]}
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


def test_timeout_s(project: dict) -> int:
    """[checks].test_timeout_s overrides [timeouts].tests_s."""
    return project["checks"].get("test_timeout_s", project["timeouts"]["tests_s"])


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

_PLACEHOLDER = re.compile(r"\{\{(yaml|sh|int):([a-z_]+)\}\}")


def template_values(board: Path, project: dict) -> dict[str, object]:
    p, tools, timeouts = project["project"], project["tools"], project["timeouts"]
    board = board.resolve()
    hooks = OURS_DIR / "hooks"
    q = shlex.quote
    claude = " ".join(
        [q(tools["claude"]), "-p", "--output-format", "stream-json", "--verbose",
         "--permission-mode", "acceptEdits"]
        + [f"--add-dir {q(str(board / d))}" for d in (BOARD_DIR, ARTIFACTS_DIR, WIKI_DIR)]
    )
    values: dict[str, object] = {
        "name": p["name"],
        "base": p["base_branch"],
        "port": p["port"],
        "workspace_root": str(Path.home() / "symphony_workspaces" / p["name"]),
        "worktree_py": str(hooks / "worktree.py"),
        "check_py": str(hooks / "check.py"),
        "claude_command": claude,
        "done_gate_command": f'"$SYMPHONY_PYTHON" {q(str(hooks / "check.py"))} gate',
        "gate_ms": timeouts["gate_ms"],
        "hooks_ms": timeouts["hooks_ms"],
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
        if kind == "int":
            if not isinstance(value, int):
                raise ProjectError(f"шаблон: {key} не число")
            return str(value)
        text = str(value)
        # JSON strings are valid YAML double-quoted scalars.
        return json.dumps(text, ensure_ascii=False) if kind == "yaml" else shlex.quote(text)

    return _PLACEHOLDER.sub(substitute, template.read_text(encoding="utf-8"))
