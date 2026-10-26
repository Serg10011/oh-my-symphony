"""Bounded, redacted persistence helpers for run diagnostics.

Only the lifecycle event names and fields declared here may enter SQLite.  This
module deliberately does not provide a generic "store this backend payload"
path: backend payloads can contain prompts, tool transcripts, and credentials.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

MAX_DIAGNOSTIC_STRING_BYTES = 1_024
MAX_EVENT_PAYLOAD_BYTES = 4_096
MAX_EVENTS_PER_RUN = 200
MAX_RUNS_WITH_DIAGNOSTIC_EVENTS = 1_000
MAX_DIAGNOSTIC_COUNTER = (2**63) - 1
REDACTED = "[REDACTED]"

# Ordered from structured/specific tokens to broader key-value patterns.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?is)-----BEGIN (?:ENCRYPTED |RSA |EC |OPENSSH )?PRIVATE KEY-----"
            r".*?-----END (?:ENCRYPTED |RSA |EC |OPENSSH )?PRIVATE KEY-----"
        ),
        REDACTED,
    ),
    (
        re.compile(
            r"(?i)\b(?:authorization\s*[:=]\s*)?"
            r"(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"
        ),
        REDACTED,
    ),
    (
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
            r"[A-Za-z0-9_-]{8,}\b"
        ),
        REDACTED,
    ),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), REDACTED),
    (
        re.compile(
            r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|"
            r"github_pat_[A-Za-z0-9_]{20,})\b"
        ),
        REDACTED,
    ),
    (
        re.compile(
            r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{16,}|"
            r"AIza[0-9A-Za-z_-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|"
            r"npm_[A-Za-z0-9]{20,})\b"
        ),
        REDACTED,
    ),
    (
        re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s/@]+(@)"),
        rf"\1{REDACTED}\2",
    ),
    (
        re.compile(r"(?i)(\b(?:cookie|set-cookie)\s*[:=]\s*)[^\r\n]+"),
        rf"\1{REDACTED}",
    ),
    (
        re.compile(
            r"""(?ix)
            (
              ["']?[a-z0-9_]*
              (?:api[_-]?key|credential|password|passwd|secret|token|set[_-]?cookie|cookie)
              [a-z0-9_]*["']?\s*[:=]\s*
            )
            (?:"[^"
]*"|'[^'
]*')
            """
        ),
        rf"\1{REDACTED}",
    ),
    (
        re.compile(
            r"""(?ix)
            (
              ["']?[a-z0-9_]*
              (?:api[_-]?key|credential|password|passwd|secret|token|set[_-]?cookie|cookie)
              [a-z0-9_]*["']?\s*[:=]\s*
            )
            [^\s,;}}]+
            """
        ),
        rf"\1{REDACTED}",
    ),
)

_ALLOWED_FIELDS: dict[str, tuple[str, ...]] = {
    "run_acquired": ("attempt", "attempt_kind", "agent_kind", "state"),
    "run_started": ("state",),
    "session_started": (),
    "turn_started": (
        "turn", "state", "continuation", "task_level", "model", "effort",
    ),
    "turn_completed": (
        "turn",
        "input_tokens",
        "cache_input_tokens",
        "output_tokens",
        "total_tokens",
        "turn_cost_usd",
        "cost_usd",
        "session_cost_usd",
        "model_used",
        "commit_sha",
    ),
    "turn_failed": ("turn", "reason", "stderr_lines"),
    "workspace_updated": ("turn", "commit_sha"),
    "phase_transition": ("from_state", "to_state", "turn", "attempt", "is_rewind"),
    "approval_denied": ("reason",),
    "compaction": ("phase", "reason", "tokens_before"),
    "retry": ("phase", "attempt", "error"),
    "run_completed": (
        "status",
        "state",
        "failure_class",
        "failure_message",
        "input_tokens",
        "cache_input_tokens",
        "output_tokens",
        "total_tokens",
        "cost_usd",
        "commit_sha",
    ),
}
_INTEGER_FIELDS = {
    "attempt",
    "turn",
    "input_tokens",
    "cache_input_tokens",
    "output_tokens",
    "total_tokens",
    "tokens_before",
}
_USD_FIELDS = {"turn_cost_usd", "cost_usd", "session_cost_usd"}
_BOOLEAN_FIELDS = {"continuation", "is_rewind"}
_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")


def nonnegative_usd(value: Any) -> float | None:
    """A finite, non-negative USD amount, or None for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    amount = float(value)
    return amount if math.isfinite(amount) and amount >= 0 else None


def _truncate_utf8(value: str, maximum: int = MAX_DIAGNOSTIC_STRING_BYTES) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= maximum:
        return value
    marker = "…[truncated]"
    allowance = max(maximum - len(marker.encode()), 0)
    prefix = encoded[:allowance].decode("utf-8", errors="ignore")
    return prefix + marker


def redact_text(value: object, maximum: int = MAX_DIAGNOSTIC_STRING_BYTES) -> str:
    """Redact common credential shapes, then enforce a UTF-8 byte ceiling."""
    text = str(value)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return _truncate_utf8(text, maximum)


def _normalize_value(field: str, value: Any) -> Any:
    if value is None:
        return None
    if field in _INTEGER_FIELDS:
        try:
            return min(max(int(value), 0), MAX_DIAGNOSTIC_COUNTER)
        except (TypeError, ValueError):
            return 0
    if field in _USD_FIELDS:
        return nonnegative_usd(value)
    if field in _BOOLEAN_FIELDS:
        return bool(value)
    if field == "commit_sha":
        candidate = str(value).strip().lower()
        return candidate if _SHA_RE.fullmatch(candidate) else None
    if field == "stderr_lines":
        if not isinstance(value, (list, tuple)):
            value = str(value).splitlines()
        return [redact_text(line, 320) for line in list(value)[-8:]]
    limits = {
        "message": 800,
        "failure_message": 1_024,
        "reason": 800,
        "error": 800,
        "session_id": 256,
    }
    return redact_text(value, limits.get(field, 256))


def normalize_event_payload(
    event_type: str, payload: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Return an allowlisted event payload; reject unknown lifecycle names."""
    fields = _ALLOWED_FIELDS.get(event_type)
    if fields is None:
        raise ValueError(f"unsupported diagnostic event type: {event_type}")
    source = payload or {}
    normalized = {
        field: _normalize_value(field, source[field])
        for field in fields
        if field in source and source[field] is not None
    }
    return _bound_payload(normalized)


def _walk_strings(
    value: Any, path: tuple[Any, ...] = ()
) -> list[tuple[int, tuple[Any, ...]]]:
    found: list[tuple[int, tuple[Any, ...]]] = []
    if isinstance(value, str):
        found.append((len(value.encode()), path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_walk_strings(item, path + (index,)))
    elif isinstance(value, dict):
        for key in sorted(value):
            found.extend(_walk_strings(value[key], path + (key,)))
    return found


def _replace_path(value: Any, path: tuple[Any, ...], replacement: str) -> None:
    cursor = value
    for part in path[:-1]:
        cursor = cursor[part]
    cursor[path[-1]] = replacement


def _bound_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Shrink the longest strings deterministically until JSON fits."""
    while (
        len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
        > MAX_EVENT_PAYLOAD_BYTES
    ):
        strings = _walk_strings(payload)
        if not strings:
            return {"truncated": True}
        size, path = max(strings, key=lambda item: (item[0], tuple(map(str, item[1]))))
        if size <= 16:
            return {"truncated": True}
        cursor: Any = payload
        for part in path:
            cursor = cursor[part]
        _replace_path(payload, path, _truncate_utf8(cursor, max(size // 2, 16)))
    return payload


def event_payload_json(event_type: str, payload: Mapping[str, Any] | None) -> str:
    return json.dumps(
        normalize_event_payload(event_type, payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
