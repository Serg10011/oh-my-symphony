"""Request-body validators shared by the route modules."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ..artifacts import ArtifactRecord
from ..errors import ConfigValidationError
from ..trackers.validate import IDENTIFIER_RE as _IDENTIFIER_RE
from ..trackers.validate import IDENTIFIER_RULE as _IDENTIFIER_RULE
from ..workflow import (
    ServiceConfig,
    selectable_agent_kinds,
    validated_ci_modes,
)
from ..workflow.mutate import (
    StateSpec,
    WorkflowMutationError,
)
from .security import (
    _BRANCH_RE,
    _CHAT_CONFIRMATION_TOKEN_RE,
    _CHAT_SESSION_RE,
    _CI_EDITABLE_KEYS,
    _INTENT_ACTION_RE,
    _MAX_BODY,
    _MAX_LABELS,
    _MAX_TITLE,
    _PROJECT_SETUP_ACTION_RE,
)

# ---------------------------------------------------------------------------
# validation helpers
# ---------------------------------------------------------------------------


def _valid_states(cfg: ServiceConfig) -> dict[str, str]:
    """lowercase -> canonical casing for every configured state."""
    return {
        s.lower(): s for s in (*cfg.tracker.active_states, *cfg.tracker.terminal_states)
    }


# Content types the board may render in-document. Deliberately excludes
# text/html and image/svg+xml — both can execute script against the board's
# own origin, and workers author these bytes.
_INLINE_ARTIFACT_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/bmp",
        "application/pdf",
        "text/plain",
    }
)


def _ascii_filename(name: str) -> str:
    """Header-safe filename: no quotes, no control chars, ASCII only."""
    cleaned = "".join(ch for ch in name if ch.isprintable() and ch not in '"\\')
    return cleaned.encode("ascii", "replace").decode("ascii") or "artifact"


def _artifact_json(identifier: str, record: ArtifactRecord) -> dict[str, Any]:
    return {
        "name": record.name,
        "title": record.title,
        "summary": record.summary,
        "content_type": record.content_type,
        "byte_size": record.byte_size,
        "collected_at": record.collected_at,
        "run_id": record.run_id,
        "turn": record.turn,
        "inline": record.content_type in _INLINE_ARTIFACT_TYPES,
        "url": f"/api/v1/issues/{identifier}/artifacts/{quote(record.name)}",
    }


def _check_identifier(raw: str) -> str:
    """Whitelist ticket identifiers before they touch the filesystem.

    `find_path` builds `board_root / f"{identifier}.md"`; on Windows a
    backslash in the identifier traverses directories, so every route
    parameter must pass this gate (not just creation).
    """
    identifier = (raw or "").strip()
    if not _IDENTIFIER_RE.match(identifier):
        raise WorkflowMutationError(f"identifier must match {_IDENTIFIER_RULE}")
    return identifier


def _check_branch(raw: Any, *, key: str = "branch") -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise WorkflowMutationError(f"{key} is required")
    branch = raw.strip()
    if not _BRANCH_RE.match(branch):
        raise WorkflowMutationError(f"invalid branch name {branch!r}")
    return branch


def _check_title(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise WorkflowMutationError("title is required")
    title = raw.strip()
    if len(title) > _MAX_TITLE:
        raise WorkflowMutationError(f"title too long (max {_MAX_TITLE} chars)")
    return title


def _check_description(raw: Any) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise WorkflowMutationError("description must be a string")
    if len(raw) > _MAX_BODY:
        raise WorkflowMutationError(f"description too long (max {_MAX_BODY} chars)")
    return raw


def _check_priority(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise WorkflowMutationError("priority must be an integer 0-4 or null")
    if not 0 <= raw <= 4:
        raise WorkflowMutationError("priority must be between 0 and 4")
    return raw


def _check_labels(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise WorkflowMutationError("labels must be a list of strings")
    labels: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        label = item.strip().lower()
        if label and len(label) <= 50 and label not in labels:
            labels.append(label)
    if len(labels) > _MAX_LABELS:
        raise WorkflowMutationError(f"too many labels (max {_MAX_LABELS})")
    return labels


def _check_request(raw: Any) -> str:
    """Optional request grouping id (e.g. REQ-1); empty string clears."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise WorkflowMutationError("request must be a string")
    request = raw.strip()
    if request and not _IDENTIFIER_RE.match(request):
        raise WorkflowMutationError(f"request must match {_IDENTIFIER_RULE}")
    return request


def _check_request_schedule_key(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise WorkflowMutationError("request schedule id is required")
    value = raw.strip()
    if len(value) > 512 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise WorkflowMutationError("request schedule id is invalid")
    return value


def _check_blocked_by(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise WorkflowMutationError("blocked_by must be a list of ticket identifiers")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise WorkflowMutationError(
                "blocked_by must be a list of ticket identifiers"
            )
        identifier = _check_identifier(item)
        if identifier not in out:
            out.append(identifier)
    return out


def _check_agent_kind(raw: Any, cfg: ServiceConfig) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise WorkflowMutationError("agent_kind must be a string")
    kind = raw.strip().lower()
    kinds = selectable_agent_kinds(cfg.agent.profiles)
    if kind and kind not in kinds:
        raise WorkflowMutationError(
            f"unknown agent_kind {kind!r}; supported: {sorted(kinds)}"
        )
    return kind


def _check_chat_session_id(raw: Any) -> str:
    """Session ids index into `.symphony/chat/<id>.jsonl` — validate strictly."""
    session_id = raw.strip() if isinstance(raw, str) else ""
    if not _CHAT_SESSION_RE.match(session_id):
        raise WorkflowMutationError(f"invalid chat session id {session_id!r}")
    return session_id


def _check_project_setup_action_id(raw: Any) -> str:
    action_id = raw.strip() if isinstance(raw, str) else ""
    if not _PROJECT_SETUP_ACTION_RE.fullmatch(action_id):
        raise WorkflowMutationError(f"invalid project setup action id {action_id!r}")
    return action_id


def _check_intent_action_id(raw: Any) -> str:
    action_id = raw.strip() if isinstance(raw, str) else ""
    if not _INTENT_ACTION_RE.fullmatch(action_id):
        raise WorkflowMutationError(f"invalid intent action id {action_id!r}")
    return action_id


def _check_chat_confirmation_token(raw: Any) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not _CHAT_CONFIRMATION_TOKEN_RE.fullmatch(raw):
        raise WorkflowMutationError("invalid chat confirmation token")
    return raw


def _check_budget(raw: Any, key: str) -> int | None:
    """Optional advisory chat limit; 0 means unlimited, absent means default."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise WorkflowMutationError(f"{key} must be a non-negative integer")
    if raw < 0:
        raise WorkflowMutationError(f"{key} must be a non-negative integer")
    return raw


def _parse_ci_settings(body: dict[str, Any], cfg: ServiceConfig) -> dict[str, Any]:
    unknown = sorted(set(body) - _CI_EDITABLE_KEYS)
    if unknown:
        raise WorkflowMutationError(
            f"unknown continuous_improvement field(s): {', '.join(unknown)}"
        )
    updates: dict[str, Any] = {}
    if "enabled" in body:
        value = body["enabled"]
        if not isinstance(value, bool):
            raise WorkflowMutationError("enabled must be a boolean")
        updates["enabled"] = value
    if "interval_ms" in body:
        value = body["interval_ms"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise WorkflowMutationError("interval_ms must be an integer")
        updates["interval_ms"] = value
    if "max_turns" in body:
        value = body["max_turns"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise WorkflowMutationError("max_turns must be an integer")
        updates["max_turns"] = value
    if "agent_kind" in body:
        updates["agent_kind"] = _check_agent_kind(body["agent_kind"], cfg)
    if "modes" in body:
        updates["modes"] = _check_ci_modes(body["modes"])
    if not updates:
        raise WorkflowMutationError(
            "body must set enabled, interval_ms, max_turns, modes, and/or agent_kind"
        )
    return updates


def _check_ci_modes(raw: Any) -> list[str]:
    """Improvement modes; `[]` clears back to readiness-only."""
    if raw is None:
        return []
    try:
        return list(validated_ci_modes(raw))
    except ConfigValidationError as exc:
        raise WorkflowMutationError(exc.message) from exc


def _check_state(cfg: ServiceConfig, raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise WorkflowMutationError("state is required")
    canonical = _valid_states(cfg).get(raw.strip().lower())
    if canonical is None:
        raise WorkflowMutationError(f"unknown state {raw.strip()!r}")
    return canonical


def _parse_state_specs(body: dict[str, Any]) -> list[StateSpec]:
    raw_states = body.get("states")
    if not isinstance(raw_states, list):
        raise WorkflowMutationError("body must contain a `states` list")
    specs: list[StateSpec] = []
    for raw in raw_states:
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            raise WorkflowMutationError("each state needs at least a `name`")
        raw_description = raw.get("description")
        specs.append(
            StateSpec(
                name=raw["name"],
                # None = "not provided, keep the current description";
                # "" = explicit clear. See StateSpec.
                description=raw_description
                if isinstance(raw_description, str)
                else None,
                terminal=bool(raw.get("terminal")),
                previous_name=(
                    str(raw["previous_name"])
                    if isinstance(raw.get("previous_name"), str)
                    else None
                ),
            )
        )
    return specs
