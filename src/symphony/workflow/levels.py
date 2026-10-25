"""Task levels: per-ticket model, effort and turn limits.

`levels` in WORKFLOW.md maps a level name to optional backend settings and
turn limits. A ticket picks one with `level:` in its frontmatter (or falls
back to `agent.default_level`); `apply_level` then returns a per-ticket
`ServiceConfig`. Which level a ticket should run at is the operator's call —
nothing here raises or lowers a level on its own.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Sequence

from ..errors import ConfigValidationError

if TYPE_CHECKING:
    from ..issue import Issue
    from .config import ServiceConfig

LEVEL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# `claude --effort` choices (claude 2.1.310).
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_BACKEND_KEYS = ("claude", "codex")
_LEVEL_KEYS = _BACKEND_KEYS + ("max_turns", "max_state_turns")


class LevelError(ConfigValidationError):
    """A ticket's level cannot be applied (unknown name, unusable command)."""

    code = "level_error"


@dataclass(frozen=True)
class BackendLevel:
    model: str | None = None
    effort: str | None = None


@dataclass(frozen=True)
class LevelConfig:
    claude: BackendLevel = field(default_factory=BackendLevel)
    codex: BackendLevel = field(default_factory=BackendLevel)
    max_turns: int | None = None
    max_state_turns: int | None = None


def _nonempty_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigValidationError(f"{name} must be a non-empty string", value=value)
    return value.strip()


def _backend_level(value: Any, backend: str, name: str) -> BackendLevel:
    if not isinstance(value, dict):
        raise ConfigValidationError(f"{name} must be a map with model/effort", value=value)
    unknown = sorted(str(k) for k in value if k not in ("model", "effort"))
    if unknown:
        raise ConfigValidationError(
            f"{name}: unknown key(s) {unknown}; allowed: model, effort"
        )
    model = _nonempty_str(value["model"], f"{name}.model") if "model" in value else None
    effort = _nonempty_str(value["effort"], f"{name}.effort") if "effort" in value else None
    if backend == "claude" and effort is not None and effort not in CLAUDE_EFFORTS:
        raise ConfigValidationError(
            f"{name}.effort must be one of {list(CLAUDE_EFFORTS)}", value=effort
        )
    return BackendLevel(model=model, effort=effort)


def _limit(value: Any, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigValidationError(f"{name} must be an integer >= {minimum}", value=value)
    return value


def parse_levels(raw: Any, *, active_states: Sequence[str]) -> dict[str, LevelConfig]:
    """Strict parse of the `levels` block; absent means no levels."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigValidationError("levels must be a map of level name to settings")
    required_turns = len([s for s in active_states if s])
    levels: dict[str, LevelConfig] = {}
    for key, body in raw.items():
        if not isinstance(key, str) or not LEVEL_NAME_RE.match(key):
            raise ConfigValidationError(
                "level name must match [A-Za-z0-9_-]+", value=key
            )
        name = f"levels.{key}"
        if not isinstance(body, dict):
            raise ConfigValidationError(f"{name} must be a map", value=body)
        unknown = sorted(str(k) for k in body if k not in _LEVEL_KEYS)
        if unknown:
            raise ConfigValidationError(
                f"{name}: unknown key(s) {unknown}; allowed: {list(_LEVEL_KEYS)}. "
                "Levels support only the claude and codex backends; pin any "
                "other backend with `agent_kind` on the ticket instead."
            )
        max_turns = _limit(body["max_turns"], f"{name}.max_turns", 1) if "max_turns" in body else None
        if max_turns is not None and required_turns > 1 and max_turns < required_turns:
            raise ConfigValidationError(
                f"{name}.max_turns={max_turns} cannot cover {required_turns} "
                f"active states; set it to >= {required_turns}"
            )
        levels[key] = LevelConfig(
            claude=_backend_level(body.get("claude", {}), "claude", f"{name}.claude"),
            codex=_backend_level(body.get("codex", {}), "codex", f"{name}.codex"),
            max_turns=max_turns,
            max_state_turns=(
                _limit(body["max_state_turns"], f"{name}.max_state_turns", 1)
                if "max_state_turns" in body
                else None
            ),
        )
    return levels


def parse_default_level(raw: Any, levels: dict[str, LevelConfig]) -> str:
    """`agent.default_level`: absent -> "", otherwise must name a level."""
    if raw is None:
        return ""
    name = _nonempty_str(raw, "agent.default_level")
    if name not in levels:
        raise ConfigValidationError(
            f"agent.default_level {name!r} is not defined in levels; "
            f"known: {sorted(levels)}"
        )
    return name


def level_name(cfg: "ServiceConfig", issue: "Issue") -> str:
    return issue.level or cfg.agent.default_level


_PROGRAM_RE = re.compile(r"""^\s*('[^']*'|"[^"]*"|\S+)""")


def _flag_pattern(flag: str) -> str:
    return rf"(?<!\S){flag}(?:=|\s+)(?:'[^']*'|\"[^\"]*\"|\S+)"


def inject_claude_flags(command: str, model: str | None, effort: str | None) -> str | None:
    """Put `--model`/`--effort` right after the program word of a claude command.

    The program may be ``claude`` or a path ending in ``/claude`` (the kit
    writes an absolute path). Existing occurrences of the same flags are
    replaced, not duplicated. Returns None when the program is something
    else (a wrapper script): the flags cannot be placed safely there.
    """
    match = _PROGRAM_RE.match(command)
    if match is None or os.path.basename(match.group(1).strip("'\"")) != "claude":
        return None
    head, rest = command[: match.end()], command[match.end() :]
    flags = []
    for flag, value in (("--model", model), ("--effort", effort)):
        if value is not None:
            rest = re.sub(_flag_pattern(flag) + r"\s*", "", rest)
            flags.append(f"{flag} {shlex.quote(value)}")
    if not flags:
        return command
    return f"{head} {' '.join(flags)}{rest if rest.startswith(' ') else ' ' + rest}".rstrip()


def apply_level(cfg: "ServiceConfig", issue: "Issue", kind: str) -> "ServiceConfig":
    """Return `cfg` with the ticket's level applied (priority: card > default)."""
    name = level_name(cfg, issue)
    if not name:
        return cfg
    level = cfg.levels.get(name)
    if level is None:
        raise LevelError(
            f"level {name!r} is not defined in WORKFLOW.md levels; "
            f"known: {sorted(cfg.levels)}",
            issue=issue.identifier,
        )
    agent = cfg.agent
    if level.max_turns is not None:
        agent = replace(agent, max_turns=level.max_turns)
    if level.max_state_turns is not None:
        agent = replace(agent, max_state_turns=level.max_state_turns)
    claude, codex = cfg.claude, cfg.codex
    if level.claude.model or level.claude.effort:
        command = inject_claude_flags(
            claude.command, level.claude.model, level.claude.effort
        )
        if command is not None:
            claude = replace(claude, command=command)
        elif kind == "claude":
            raise LevelError(
                f"level {name!r} sets a claude model/effort but claude.command "
                "does not run `claude` (a wrapper script?); flags cannot be injected",
                issue=issue.identifier,
            )
    if level.codex.model:
        codex = replace(codex, model=level.codex.model)
    if level.codex.effort:
        codex = replace(codex, reasoning_effort=level.codex.effort)
    return replace(cfg, agent=agent, claude=claude, codex=codex)


def _flag_value(command: str, flag: str) -> str | None:
    match = re.search(_flag_pattern(flag), command)
    if match is None:
        return None
    token = match.group(0).replace("=", " ", 1).split(None, 1)[1]
    return token.strip("'\"")


def turn_fields(cfg: "ServiceConfig", issue: "Issue") -> dict[str, str | None]:
    """Level, model and effort of a turn, for the service log and run events."""
    if cfg.agent.kind == "codex":
        model, effort = cfg.codex.model, cfg.codex.reasoning_effort
    elif cfg.agent.kind == "claude":
        model = _flag_value(cfg.claude.command, "--model") or os.environ.get("ANTHROPIC_MODEL")
        effort = _flag_value(cfg.claude.command, "--effort")
    else:
        model = effort = None
    # `task_level`, not `level`: structlog reserves `level` for the log severity.
    return {"task_level": level_name(cfg, issue) or None, "model": model, "effort": effort}
