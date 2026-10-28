"""`agent.quota_exhausted_state` — park a ticket on an exhausted credit and
hold dispatch until the operator changes `WORKFLOW.md`.

A turn that fails with an exhausted API credit (or a failing apiKeyHelper)
cannot succeed on retry and nothing switches the account on its own. With
`agent.quota_exhausted_state` set, the ticket moves to that terminal state
with a `## Quota Exhausted` note instead of pausing, and the service stops
picking up new tickets (one ticket parks, the rest wait) until the content
of `WORKFLOW.md` changes or the service restarts. The hold lives in memory
only. Without the key none of this runs.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from ..logging import get_logger
from .helpers import _utc_iso_z
from .core_support import (
    _clean_board_error_message,
    _EligibilityDecision,
    _EligibilityDisposition,
    _is_api_key_helper_error,
    _is_quota_worker_error,
)

if TYPE_CHECKING:
    from ..issue import Issue
    from ..workflow import ServiceConfig
    from .core import Orchestrator
    from .entries import RunningEntry, _IssueDebug

log = get_logger()

# `dispatch_held_quota` is logged at most this often for the whole board.
HOLD_LOG_INTERVAL_SEC = 60.0


@dataclass
class DispatchHold:
    reason: str
    since: str
    workflow_digest: str | None


def is_quota_exhausted_error(reason: str, error: str | None) -> bool:
    return _is_quota_worker_error(reason, error) or _is_api_key_helper_error(
        reason, error
    )


def _workflow_digest(cfg: ServiceConfig) -> str | None:
    try:
        return hashlib.sha256(cfg.workflow_path.read_bytes()).hexdigest()
    except OSError:
        return None


def park_on_quota_exhausted(
    orch: Orchestrator,
    cfg: ServiceConfig | None,
    issue_id: str,
    entry: RunningEntry,
    debug: _IssueDebug,
    reason: str,
    error: str | None,
) -> bool:
    """Move the ticket to `agent.quota_exhausted_state`; True when it moved.

    False leaves the existing retry/auto-pause decision to the caller —
    including when the tracker write fails, so the ticket never ends up
    without a sign that it stopped.
    """
    if cfg is None or not cfg.agent.quota_exhausted_state:
        return False
    if not is_quota_exhausted_error(reason, error):
        return False
    target = cfg.agent.quota_exhausted_state
    was = entry.issue.state
    detail = f"{reason}: {error}" if error else reason
    cleaned = _clean_board_error_message(detail)
    try:
        orch._tracker_call_update_state(cfg, entry.issue, target)
    except Exception as exc:
        log.warning(
            "worker_quota_exhausted_state_failed",
            issue_id=issue_id,
            issue_identifier=entry.issue.identifier,
            state=target,
            error=cleaned,
            failure=str(exc),
        )
        return False
    lines = [
        f"The backend reported: {cleaned}",
        f"was: {was}",
        f"moved to {target} by agent.quota_exhausted_state; no automatic switch",
    ]
    if cfg.agent.quota_exhausted_note:
        lines += ["", cfg.agent.quota_exhausted_note]
    try:
        orch._tracker_call_append_note(
            cfg, entry.issue, "Quota Exhausted", "\n".join(lines)
        )
    except Exception as exc:
        log.warning(
            "worker_quota_exhausted_note_failed",
            issue_id=issue_id,
            issue_identifier=entry.issue.identifier,
            error=str(exc),
        )
    entry.issue = _with_state(entry.issue, target)
    # Terminal now: no retry timer, no claim, no persisted attempt.
    orch._dispatch_state.cancel_pending_retry(issue_id)
    orch._claimed.discard(issue_id)
    orch._persisted_retry_attempts.pop(issue_id, None)
    orch._clear_issue_flags(issue_id, retry_attempt=True)
    debug.last_error = f"quota exhausted: {cleaned}; moved to {target}"
    log.warning(
        "worker_quota_exhausted_state",
        issue_id=issue_id,
        issue_identifier=entry.issue.identifier,
        state=target,
        was=was,
        error=cleaned,
    )
    _engage_hold(orch, cfg, f"{entry.issue.identifier}: {cleaned}")
    return True


def _with_state(issue: Issue, state: str) -> Issue:
    return replace(issue, state=state)


def _engage_hold(orch: Orchestrator, cfg: ServiceConfig, reason: str) -> None:
    if orch._dispatch_hold is not None:
        return
    orch._dispatch_hold = DispatchHold(
        reason=reason,
        since=_utc_iso_z(),
        workflow_digest=_workflow_digest(cfg),
    )
    orch._dispatch_hold_logged_at = None
    log.warning("dispatch_hold_engaged", reason=reason, since=orch._dispatch_hold.since)


def hold_decision(orch: Orchestrator, issue: Issue) -> _EligibilityDecision | None:
    """Refuse to pick `issue` while the quota hold stands."""
    hold = orch._dispatch_hold
    if hold is None:
        return None
    now = time.monotonic()
    last = orch._dispatch_hold_logged_at
    if last is None or now - last >= HOLD_LOG_INTERVAL_SEC:
        orch._dispatch_hold_logged_at = now
        log.warning(
            "dispatch_held_quota",
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            reason=hold.reason,
            since=hold.since,
        )
    return _EligibilityDecision(
        _EligibilityDisposition.WAIT_NON_SLOT,
        "dispatch_held_quota",
        f"dispatch held since {hold.since}: {hold.reason}",
    )


def release_hold_if_workflow_changed(
    orch: Orchestrator, cfg: ServiceConfig
) -> bool:
    """Lift the hold when the content of `WORKFLOW.md` is not what it was."""
    hold = orch._dispatch_hold
    if hold is None:
        return False
    digest = _workflow_digest(cfg)
    if digest is None or digest == hold.workflow_digest:
        return False
    orch._dispatch_hold = None
    orch._dispatch_hold_logged_at = None
    log.info("dispatch_hold_released", reason=hold.reason, since=hold.since)
    return True


def hold_snapshot(orch: Orchestrator) -> dict[str, str] | None:
    hold = orch._dispatch_hold
    if hold is None:
        return None
    return {"reason": hold.reason, "since": hold.since}
