"""Module-level helpers shared by the ``Orchestrator`` method modules.

Extracted from ``core.Orchestrator`` (LR-3 lean-core split). Each method is a
module-level function whose first parameter is ``self: Orchestrator``;
``core.Orchestrator`` binds it as a class attribute, so ``self.<name>(...)``
calls, ``Orchestrator.<name>`` patches, and behaviour are unchanged.
Collaborators that tests monkeypatch on ``symphony.orchestrator.core`` are read
through the late-bound ``_core()`` handle at call time, never bound here.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..issue import Issue, normalize_state
from ..workflow import DEFAULT_TERMINAL_STATES, ServiceConfig
from .entries import _IssueDebug


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


_FAILED_BLOCKER_TERMINAL_STATES = {
    "archive",
    "archived",
    "blocked",
    "cancelled",
    "canceled",
    "duplicate",
    "human review",
}


_RETRYABLE_WORKER_ERROR_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "too many requests",
    "temporarily overloaded",
    "overloaded",
    "service unavailable",
    # Google-backed CLIs (agy) report a transient 503 as
    # "UNAVAILABLE (code 503): The service is currently unavailable.",
    # which the "service unavailable" substring above does not match.
    "service is currently unavailable",
    "unavailable (code 503)",
    "connection error",
    "network error",
    "connection reset",
    "connection timed out",
    "try again later",
    # Transient backend stream faults (acceptance finding 3). The strings
    # below are emitted verbatim by the backends; `test_backend_contract.py`
    # pins them so the two lists cannot drift apart.
    "stream unreadable",  # claude_code.py / codex.py / pi.py
    "no result event",  # claude_code.py — rc!=0 with no result frame
)


class _EligibilityDisposition(str, Enum):
    READY = "ready"
    WAIT_SLOT = "wait_slot"
    WAIT_NON_SLOT = "wait_non_slot"
    REJECT = "reject"


@dataclass(frozen=True)
class _EligibilityDecision:
    disposition: _EligibilityDisposition
    code: str
    reason: str


def _worktree_status_snapshot(cwd: Path) -> dict[str, str] | None:
    """`path -> porcelain status code` for a worktree, or None when not git.

    Untracked files are listed individually (`-uall`) so a new file in a
    previously-clean directory is visible as its own path.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-uall", "-z"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    snapshot: dict[str, str] = {}
    for record in result.stdout.split("\0"):
        if len(record) < 4:
            continue
        code, path = record[:2], record[3:]
        if path:
            snapshot[path] = code
    return snapshot


def _clean_board_error_message(message: str) -> str:
    without_ansi = _ANSI_ESCAPE_RE.sub("", message)
    without_controls = _CONTROL_CHAR_RE.sub("", without_ansi)
    return " ".join(without_controls.split())


# Errors that mean "this backend's account is out of budget for now": a
# retry with backoff on the same backend cannot help, but another backend
# can (2026-09-05 E2E: codex usage limit paused the worker until an operator
# re-pinned the ticket by hand). Distinct from the transient
# `_RETRYABLE_WORKER_ERROR_MARKERS` (429 / rate limit), which retry in place.
_QUOTA_WORKER_ERROR_MARKERS = (
    "usage limit",
    "usage_limit",
    "session limit",
    "quota",
    "insufficient_quota",
    "insufficient credits",
    "out of credits",
    "credit balance",
    "billing",
    "plan limit",
    "weekly limit",
    "monthly limit",
)


def _is_quota_worker_error(reason: str, error: str | None) -> bool:
    detail = f"{reason}: {error}" if error else reason
    clean = _clean_board_error_message(detail).lower()
    if any(marker in clean for marker in _QUOTA_WORKER_ERROR_MARKERS):
        return True
    # Generic limit wording also appears in transient rate-limit errors.
    # Explicit account/quota markers above still win when both are present.
    return not _has_retryable_worker_marker(clean) and any(
        marker in clean for marker in ("limit reached", "limit exceeded")
    )


def _worker_error_pause_reason(reason: str, error: str | None) -> str:
    detail = f"{reason}: {error}" if error else reason
    clean = _clean_board_error_message(detail)
    return f"worker error: {clean}; paused for operator inspection"


def _has_retryable_worker_marker(clean_error: str) -> bool:
    return any(marker in clean_error for marker in _RETRYABLE_WORKER_ERROR_MARKERS)


def _is_opencode_sigterm_retry(agent_kind: str, clean_error: str) -> bool:
    if "exit -15" not in clean_error:
        return False
    return agent_kind == "opencode" or "opencode" in clean_error


def _is_retryable_worker_error(agent_kind: str, reason: str, error: str | None) -> bool:
    detail = f"{reason}: {error}" if error else reason
    clean = _clean_board_error_message(detail).lower()
    if _has_retryable_worker_marker(clean):
        return True
    # Live OpenCode throttling can surface as SIGTERM with no stderr.
    return _is_opencode_sigterm_retry(agent_kind, clean)


def _update_state_turn_counter(debug: _IssueDebug, state: str) -> int:
    state = normalize_state(state)
    if not debug.state_turn_state:
        debug.state_turn_state = state
        debug.state_turn_count = 1
    elif debug.state_turn_state == state:
        debug.state_turn_count += 1
    else:
        debug.state_turn_state = state
        debug.state_turn_count = 0
    return debug.state_turn_count


def _normalize_agent_pid(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _attention_signal(
    kind: str,
    label: str,
    message: str,
    severity: str,
    *,
    due_at: str | None = None,
) -> dict[str, str | None]:
    return {
        "kind": kind,
        "label": label,
        "message": message,
        "severity": severity,
        "due_at": due_at,
    }


def _successful_blocker_terminal_states(cfg: ServiceConfig | None) -> set[str]:
    if cfg is None:
        terminal_states = DEFAULT_TERMINAL_STATES
        archive_state = "Archive"
    else:
        terminal_states = cfg.tracker.terminal_states
        archive_state = cfg.tracker.archive_state
    failed = set(_FAILED_BLOCKER_TERMINAL_STATES)
    failed.add(normalize_state(archive_state).strip())
    return {normalize_state(s).strip() for s in terminal_states} - failed


def _blocker_dependency_is_resolved(
    blocker_state: str | None, cfg: ServiceConfig | None
) -> bool:
    state = normalize_state(blocker_state).strip()
    if not state:
        return False
    return state in _successful_blocker_terminal_states(cfg)


_REOPEN_APPROVED_RE = re.compile(r"^##\s+Reopen Approved\b", re.MULTILINE)


def _has_app_release_label(issue: Issue) -> bool:
    return any(label.strip().lower() == "app-release" for label in issue.labels)


_BLOCKED_RCA_HEADING_RE = re.compile(
    r"^##\s+Blocked\s+(?:Fix|RCA)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


_BLOCKED_RCA_RESOLVED_HEADING_RE = re.compile(
    r"^##\s+(?:Blocked\s+(?:Fix|RCA)\s+Resolved|(?:Fix|RCA)\s+Resolution)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _blocked_rca_already_requested(issue: Issue) -> bool:
    """Whether the current Blocked episode already requested an RCA.

    Resolution notes close the preceding request.  A source that later blocks
    again is a new episode and may open a new RCA, provided no RCA for that
    source is currently active on the board.
    """
    description = issue.description or ""
    requested = list(_BLOCKED_RCA_HEADING_RE.finditer(description))
    if not requested:
        return False
    resolved = list(_BLOCKED_RCA_RESOLVED_HEADING_RE.finditer(description))
    return not resolved or requested[-1].start() > resolved[-1].start()
