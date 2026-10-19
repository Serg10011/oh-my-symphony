"""Run-registry access, durable run leases, and run-history views for ``Orchestrator``.

Extracted from ``core.Orchestrator`` (LR-3 lean-core split). Each method is a
module-level function whose first parameter is ``self: Orchestrator``;
``core.Orchestrator`` binds it as a class attribute, so ``self.<name>(...)``
calls, ``Orchestrator.<name>`` patches, and behaviour are unchanged.
Collaborators that tests monkeypatch on ``symphony.orchestrator.core`` are read
through the late-bound ``_core()`` handle at call time, never bound here.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, cast

from .._windows_job import is_job_identity, job_exists, terminate_job
from ..errors import SymphonyError
from ..issue import Issue
from ..workflow import ServiceConfig, SYMPHONY_BRANCH_PREFIX
from .entries import RunningEntry, _IssueDebug
from .run_registry import (
    ContinuationCheckpoint,
    ReleaseEvidenceIdentity,
    RunRecord,
    RunRegistry,
    registry_path_for_workflow,
)
from .core_support import (
    _clean_board_error_message,
    _has_retryable_worker_marker,
    _is_opencode_sigterm_retry,
    _normalize_agent_pid,
)
from ..logging import get_logger

if TYPE_CHECKING:
    from .core import Orchestrator

log = get_logger()


def _core():
    """Late-bound handle to ``symphony.orchestrator.core`` (imports this module)."""
    from . import core

    return core


@dataclass(frozen=True)
class _RunLeaseAcquisition:
    """One new lease and an optional predecessor checkpoint."""

    run_id: str
    continued_from_run_id: str = ""
    checkpoint: ContinuationCheckpoint | None = None


def _is_retryable_auto_pause_reason(pause_reason: str | None) -> bool:
    if not pause_reason:
        return False
    clean = _clean_board_error_message(pause_reason).lower()
    if "worker error:" not in clean or "paused for operator inspection" not in clean:
        return False
    return _has_retryable_worker_marker(clean) or _is_opencode_sigterm_retry("", clean)


def _reclaim_kill_confirm(record: RunRecord) -> str | None:
    """Prove-and-kill one reclaimed run's recorded backend process.

    Pure OS work — identity probe, process-group kill, and the bounded
    gone-confirm poll — with no registry access, so callers can run it
    inside ``asyncio.to_thread`` without breaking the registry's
    thread-affine SQLite connection. Returns the outcome label
    ("killed" / "not_found" / "identity_mismatch"), or ``None`` when the
    incarnation could not be proven and the caller must NOT finalize.
    """
    pid = record.backend_agent_pid
    if pid is not None and pid <= 0:
        log.warning(
            "reclaim_invalid_orphan_agent_pid",
            issue_id=record.issue_id,
            identifier=record.identifier,
            pid=pid,
        )
        return None
    expected_identity = record.backend_process_identity
    if pid is None:
        return None if is_job_identity(expected_identity) else "no_backend_pid"
    if is_job_identity(expected_identity):
        assert expected_identity is not None
        exists = job_exists(expected_identity)
        if exists is False:
            return "not_found"
        if exists is None or not terminate_job(expected_identity):
            return None
        deadline = time.monotonic() + 1.0
        while job_exists(expected_identity) is not False:
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)
        return "killed"
    current_identity = _core().process_identity(pid)
    group_exists = _core().process_group_exists(pid)
    if current_identity is None:
        if group_exists is not False:
            log.warning(
                "reclaim_backend_identity_ambiguous",
                issue_id=record.issue_id,
                identifier=record.identifier,
                pid=pid,
            )
            return None
        outcome = "not_found"
    elif expected_identity is None:
        # Pre-v8 or failed identity capture: never signal a reusable
        # numeric pid without proving it is the recorded incarnation.
        log.warning(
            "reclaim_backend_identity_missing",
            issue_id=record.issue_id,
            identifier=record.identifier,
            pid=pid,
        )
        return None
    elif current_identity != expected_identity:
        # The recorded process incarnation is gone and this pid was
        # reused. Do not signal the unrelated replacement.
        outcome = "identity_mismatch"
    else:
        try:
            killed = _core().kill_process_group(pid, identity=expected_identity)
        except Exception as exc:
            log.warning(
                "reclaim_killed_orphan_agent",
                issue_id=record.issue_id,
                identifier=record.identifier,
                pid=pid,
                outcome=f"error: {type(exc).__name__}",
            )
            return None
        if killed:
            deadline = time.monotonic() + 1.0
            confirmed_gone = _core().process_group_exists(pid) is False
            while not confirmed_gone and time.monotonic() < deadline:
                time.sleep(0.05)
                confirmed_gone = _core().process_group_exists(pid) is False
            if not confirmed_gone:
                log.warning(
                    "reclaim_killed_orphan_agent",
                    issue_id=record.issue_id,
                    identifier=record.identifier,
                    pid=pid,
                    outcome="unconfirmed",
                )
                return None
            outcome = "killed"
        elif _core().process_group_exists(pid) is False:
            outcome = "not_found"
        else:
            log.warning(
                "reclaim_killed_orphan_agent",
                issue_id=record.issue_id,
                identifier=record.identifier,
                pid=pid,
                outcome="ambiguous",
            )
            return None
    log.warning(
        "reclaim_killed_orphan_agent",
        issue_id=record.issue_id,
        identifier=record.identifier,
        pid=pid,
        outcome=outcome,
    )
    return outcome


def _run_record_payload(record: RunRecord) -> dict[str, Any]:
    return {
        "run_id": record.run_id,
        "issue_id": record.issue_id,
        "identifier": record.identifier,
        "title": record.title,
        "state": record.state,
        "attempt": record.attempt,
        "attempt_kind": record.attempt_kind,
        "agent_kind": record.agent_kind,
        "status": record.status,
        "started_at": record.started_at.isoformat() if record.started_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        "completed_at": record.completed_at.isoformat() if record.completed_at else None,
        "workspace_path": str(record.workspace_path) if record.workspace_path else None,
        "branch_name": record.branch_name or f"{SYMPHONY_BRANCH_PREFIX}{record.identifier}",
        "commit_sha": record.commit_sha,
        "continued_from_run_id": record.continued_from_run_id,
        "checkpoint": (
            {
                "state": record.checkpoint_state,
                "turn": record.checkpoint_turn,
                "checkpointed_at": (
                    record.checkpointed_at.isoformat()
                    if record.checkpointed_at is not None
                    else None
                ),
            }
            if record.checkpoint_state is not None
            and record.checkpoint_turn is not None
            else None
        ),
        "tokens": {
            "input": record.input_tokens,
            "cache": record.cache_input_tokens,
            "output": record.output_tokens,
            "total": record.total_tokens,
        },
        "failure_class": record.failure_class,
        "failure_message": record.failure_message,
    }


def _registry_guard(self: Orchestrator, op: str, fn: Callable[[], Any], default: Any) -> Any:
    """Run one registry op; a broken registry degrades, never raises.

        The lease is a secondary guard on top of the in-process `_running`/
        `_claimed` sets, so registry failures fail-open (callers get
        `default`) and surface through health() instead of killing the tick.
        """
    try:
        result = fn()
    except Exception as exc:
        self._registry_error_count += 1
        self._last_registry_error = f"{op}: {exc}"
        log.error("run_registry_error", op=op, error=str(exc))
        return default
    self._last_registry_error = None
    return result


def _open_run_registry(self: Orchestrator, cfg: ServiceConfig) -> tuple[RunRegistry | None, bool]:
    """Open (or reuse) the run registry connection.

        Returns ``(registry, fresh)``. ``fresh`` is True only when a NEW
        connection was opened (callers run reclaim + open-maintenance
        then); an existing path-matched registry returns ``(registry,
        False)`` so maintenance is skipped, mirroring the historical
        early return. Open failures log, bump the error counters, and
        return ``(None, False)``.
        """
    self._run_registry_initialized = True
    path = registry_path_for_workflow(cfg.workflow_path)
    if self._run_registry is not None and self._run_registry.path == path:
        return self._run_registry, False
    if self._run_registry is not None:
        self._run_registry.close()
    try:
        self._run_registry = RunRegistry(path)
    except Exception as exc:
        self._run_registry = None
        self._registry_error_count += 1
        self._last_registry_error = f"open: {exc}"
        log.error("run_registry_open_failed", path=str(path), error=str(exc))
        return None, False
    return self._run_registry, True


def _finish_run_registry_open(
    self: Orchestrator, registry: RunRegistry, cfg: ServiceConfig, path: Path
) -> None:
    """Post-open maintenance: expire stale leases, rehydrate flags."""
    expired = self._registry_guard("expire_stale", registry.expire_stale, 0)
    if expired:
        log.info("run_leases_expired_on_start", count=expired, path=str(path))
    flags = self._registry_guard("list_issue_flags", registry.list_issue_flags, [])
    self._rehydrate_issue_flags(flags, cfg=cfg, registry=registry)


def _ensure_run_registry(self: Orchestrator, cfg: ServiceConfig) -> None:
    """Open the registry and reclaim dead-owner leases (blocking form).

        The per-record kill+confirm (taskkill / SIGKILL plus the bounded
        gone-confirm poll) runs inline on the calling thread, completing
        before this returns. Event-loop callers must use
        `_ensure_run_registry_async` instead so the OS kill never stalls
        the tick loop.
        """
    registry, fresh = self._open_run_registry(cfg)
    if not fresh or registry is None:
        return
    self._reclaim_dead_owner_runs(registry, path=registry.path)
    self._finish_run_registry_open(registry, cfg, registry.path)


async def _ensure_run_registry_async(self: Orchestrator, cfg: ServiceConfig) -> None:
    """Event-loop form of `_ensure_run_registry`.

        Identical semantics — open, reclaim dead owners (kill+confirm
        completed before return, 'reclaiming' fence observable mid-kill),
        expire, rehydrate — except each record's blocking kill+confirm
        runs in a worker thread, so the tick loop keeps servicing ticks
        while a slow taskkill drains.
        """
    registry, fresh = self._open_run_registry(cfg)
    if not fresh or registry is None:
        return
    await self._reclaim_dead_owner_runs_async(registry, path=registry.path)
    self._finish_run_registry_open(registry, cfg, registry.path)


def _ensure_run_registry_open_only(self: Orchestrator, cfg: ServiceConfig) -> None:
    """Open + maintenance for sync callers that must never block.

        Same as `_ensure_run_registry` minus the OS-level dead-owner
        reclaim: the release-gate fallback runs on the event loop and a
        taskkill there would stall ticks. Dead-owner leases are still
        honored (they block acquisition) and reclaimed by the next tick's
        off-loop pass, so correctness never depends on this side effect.
        """
    registry, fresh = self._open_run_registry(cfg)
    if not fresh or registry is None:
        return
    self._finish_run_registry_open(registry, cfg, registry.path)


def _reclaim_dead_owner_runs(
    self: Orchestrator, registry: RunRegistry, *, path: Path | None = None
) -> None:
    reclaimed = self._registry_guard(
        "reclaim_dead_owner", registry.reclaim_dead_owner_leases, []
    )
    if not reclaimed:
        return
    finalized = [
        record
        for record in reclaimed
        if self._reap_and_finalize_reclaimed_run(registry, record)
    ]
    log.info(
        "run_leases_reclaimed_dead_owner",
        count=len(finalized),
        pending_count=len(reclaimed) - len(finalized),
        identifiers=[record.identifier for record in finalized],
        path=str(path or registry.path),
    )


async def _reclaim_dead_owner_runs_async(
    self: Orchestrator, registry: RunRegistry, *, path: Path | None = None
) -> None:
    """`_reclaim_dead_owner_runs` with each kill+confirm off the loop."""
    reclaimed = self._registry_guard(
        "reclaim_dead_owner", registry.reclaim_dead_owner_leases, []
    )
    if not reclaimed:
        return
    finalized = []
    for record in reclaimed:
        if await self._reap_and_finalize_reclaimed_run_async(registry, record):
            finalized.append(record)
    log.info(
        "run_leases_reclaimed_dead_owner",
        count=len(finalized),
        pending_count=len(reclaimed) - len(finalized),
        identifiers=[record.identifier for record in finalized],
        path=str(path or registry.path),
    )


def _reap_and_finalize_reclaimed_run(
    self: Orchestrator, registry: RunRegistry, record: RunRecord
) -> bool:
    """Verify process incarnation, reap it, then release the SQLite fence.

        Blocking form for synchronous callers (tests, the sync ensure
        fallback): the OS-level kill+confirm runs inline on this thread.
        The registry finalize below stays on the caller's thread so the
        SQLite connection never crosses threads.
        """
    outcome = _core()._reclaim_kill_confirm(record)
    if outcome is None:
        return False
    return bool(
        self._registry_guard(
            "finalize_reclaimed_lease",
            lambda: registry.finalize_reclaimed_lease(record.run_id),
            False,
        )
    )


async def _reap_and_finalize_reclaimed_run_async(
    self: Orchestrator, registry: RunRegistry, record: RunRecord
) -> bool:
    """Off-event-loop form of `_reap_and_finalize_reclaimed_run`.

        The blocking kill+confirm (taskkill + bounded gone-confirm poll)
        runs in a worker thread; the SQLite claim state and the finalize
        below stay on the event-loop thread, preserving the registry's
        thread-affine connection. The kill still completes before this
        coroutine returns, so the 'reclaiming' fence is observable
        mid-kill and finalized only afterwards.
        """
    outcome = await asyncio.to_thread(_core()._reclaim_kill_confirm, record)
    if outcome is None:
        return False
    return bool(
        self._registry_guard(
            "finalize_reclaimed_lease",
            lambda: registry.finalize_reclaimed_lease(record.run_id),
            False,
        )
    )


async def recent_runs(
    self: Orchestrator,
    issue_id: str | None = None,
    limit: int = 50,
    *,
    query: str | None = None,
    status: str | None = None,
    agent: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    registry = self._run_registry
    if registry is None:
        cfg = self._workflow_state.current()
        if cfg is not None:
            # Async: the fallback open's dead-owner reclaim (taskkill)
            # must not block the web loop this handler runs on.
            await self._ensure_run_registry_async(cfg)
            registry = self._run_registry
    if registry is None:
        return [], "run registry unavailable"

    rows = self._registry_guard(
        "recent_runs",
        lambda: registry.recent_runs(
            issue_id=issue_id,
            limit=limit,
            query=query,
            status=status,
            agent=agent,
        ),
        None,
    )
    if rows is None:
        return [], self._last_registry_error
    return [_run_record_payload(row) for row in rows], None


async def run_detail(self: Orchestrator, run_id: str) -> tuple[dict[str, Any] | None, str | None]:
    registry = self._run_registry
    if registry is None:
        cfg = self._workflow_state.current()
        if cfg is not None:
            await self._ensure_run_registry_async(cfg)
            registry = self._run_registry
    if registry is None:
        return None, "run registry unavailable"
    try:
        return registry.run_detail(run_id), None
    except KeyError:
        return None, None
    except Exception as exc:
        self._registry_error_count += 1
        self._last_registry_error = f"run_detail: {exc}"
        return None, self._last_registry_error


async def run_diagnostic(self: Orchestrator, run_id: str) -> tuple[dict[str, Any] | None, str | None]:
    registry = self._run_registry
    if registry is None:
        cfg = self._workflow_state.current()
        if cfg is not None:
            await self._ensure_run_registry_async(cfg)
            registry = self._run_registry
    if registry is None:
        return None, "run registry unavailable"
    try:
        return registry.diagnostic_json(run_id), None
    except KeyError:
        return None, None
    except Exception as exc:
        self._registry_error_count += 1
        self._last_registry_error = f"run_diagnostic: {exc}"
        return None, self._last_registry_error


def _append_run_event(
    self: Orchestrator,
    entry: RunningEntry,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    registry = self._run_registry
    if registry is None or not entry.run_id:
        return
    self._registry_guard(
        "append_attempt_event",
        lambda: registry.append_attempt_event(
            run_id=entry.run_id,
            event_type=event_type,
            payload=payload,
        ),
        False,
    )


def _rehydrate_issue_flags(
    self: Orchestrator,
    flags: list[Any],
    *,
    cfg: ServiceConfig,
    registry: RunRegistry,
) -> None:
    self._persisted_retry_attempts.clear()
    for flag in flags:
        issue_id = flag.issue_id
        if flag.budget_exhausted:
            self._turn_budget_exhausted.add(issue_id)
        identity: ReleaseEvidenceIdentity | None = None
        handoff_complete = False
        if flag.retry_attempt is not None:
            try:
                identity = registry.get_release_evidence_identity_by_issue_id(
                    issue_id
                )
                handoff_complete = bool(
                    identity is not None
                    and self._release_verifier_handoff_is_durable(
                        cfg=cfg,
                        registry=registry,
                        identity=identity,
                    )
                )
            except Exception as exc:
                # Recovery is an allow-list: unreadable authority keeps the
                # existing retry/pause rather than forgiving it.
                log.warning(
                    "historical_release_verifier_handoff_recovery_deferred",
                    issue_id=issue_id,
                    error=str(exc),
                )
        if handoff_complete:
            assert identity is not None
            self._dispatch_state.cancel_pending_retry(issue_id)
            self._persisted_retry_attempts.pop(issue_id, None)
            self._paused_issue_ids.discard(issue_id)
            self._pause_reasons.pop(issue_id, None)
            self._clear_issue_flags(
                issue_id,
                retry_attempt=True,
                paused=True,
            )
            log.info(
                "historical_release_verifier_handoff_recovered",
                issue_id=issue_id,
                identifier=identity.identifier,
                finalizer=identity.finalizer_identifier,
                generation=identity.cycle_generation,
            )
            continue
        if flag.paused:
            pause_reason = str(flag.pause_reason) if flag.pause_reason else None
            if flag.retry_attempt is not None and _is_retryable_auto_pause_reason(
                pause_reason
            ):
                self._paused_issue_ids.discard(issue_id)
                self._pause_reasons.pop(issue_id, None)
                self._clear_issue_flags(issue_id, paused=True)
                log.info(
                    "retryable_worker_pause_released",
                    issue_id=issue_id,
                    pause_reason=pause_reason,
                )
            else:
                self._paused_issue_ids.add(issue_id)
                if pause_reason:
                    self._pause_reasons[issue_id] = pause_reason
        if flag.retry_attempt is not None:
            self._persisted_retry_attempts[issue_id] = int(flag.retry_attempt)
            debug = self._issue_debug.setdefault(issue_id, _IssueDebug())
            debug.current_retry_attempt = int(flag.retry_attempt)
            debug.current_attempt_kind = "retry"


def _set_issue_flags(self: Orchestrator, issue_id: str, **flags: Any) -> None:
    registry = self._run_registry
    if registry is None:
        return
    self._registry_guard(
        "set_issue_flags",
        lambda: registry.set_issue_flags(issue_id, **flags),
        None,
    )


def _clear_issue_flags(
    self: Orchestrator,
    issue_id: str,
    *,
    retry_attempt: bool = False,
    budget_exhausted: bool = False,
    paused: bool = False,
) -> None:
    registry = self._run_registry
    if registry is None:
        return
    self._registry_guard(
        "clear_issue_flags",
        lambda: registry.clear_issue_flags(
            issue_id,
            retry_attempt=retry_attempt,
            budget_exhausted=budget_exhausted,
            paused=paused,
        ),
        None,
    )


def _mark_budget_exhausted(self: Orchestrator, issue_id: str) -> None:
    self._turn_budget_exhausted.add(issue_id)
    self._persisted_retry_attempts.pop(issue_id, None)
    self._set_issue_flags(issue_id, budget_exhausted=True, retry_attempt=None)


def _has_active_run_lease(self: Orchestrator, issue_id: str) -> bool:
    if self._run_registry is None:
        return False
    registry = self._run_registry
    return bool(
        self._registry_guard(
            "has_active_lease", lambda: registry.has_active_lease(issue_id), False
        )
    )


def _heartbeat_run_lease(
    self: Orchestrator,
    issue_id: str,
    entry: RunningEntry,
    *,
    progress: datetime | None = None,
    backend_agent_pid: int | None = None,
    backend_process_identity: str | None = None,
    identity_captured: bool = False,
) -> bool:
    """Refresh the entry's lease; returns False only on a real conflict.

        A missed heartbeat means the row is no longer active — either the
        lease TTL lapsed (e.g. a blocked tick) or a peer replaced it. A
        healthy worker should not keep running leaseless, so try to take a
        fresh lease; only an actual conflicting holder returns False.
        """
    effective_backend_pid = backend_agent_pid or entry.agent_pgid
    if (
        not identity_captured
        and effective_backend_pid is not None
        and entry.agent_process_identity_captured
    ):
        backend_process_identity = entry.agent_process_identity
        identity_captured = True
    registry = self._run_registry
    release_required = entry.release_authority_resolved and (
        entry.known_app_release
        or entry.known_release_cycle_verifier
        or entry.known_app_release_finalizer
    )
    current_cfg = self._workflow_state.current()
    continuation_authority_required = bool(
        current_cfg is not None and current_cfg.agent.crash_continuation
    )
    if registry is None or not entry.run_id:
        if release_required or (
            continuation_authority_required and self._run_registry_initialized
        ):
            entry.lease_lost = True
            log.error(
                "run_lease_authority_unavailable",
                issue_id=issue_id,
                issue_identifier=entry.issue.identifier,
                release_required=release_required,
            )
            return False
        return True
    if release_required:
        try:
            ok = registry.heartbeat(
                issue_id=issue_id,
                run_id=entry.run_id,
                progress_at=progress,
                backend_agent_pid=effective_backend_pid,
                backend_process_identity=backend_process_identity,
                backend_process_identity_captured=identity_captured,
            )
        except Exception as exc:
            self._registry_error_count += 1
            self._last_registry_error = f"release_heartbeat: {exc}"
            entry.lease_lost = True
            log.error(
                "release_registry_error",
                op="heartbeat",
                issue_id=issue_id,
                error=str(exc),
            )
            return False
        self._last_registry_error = None
    elif continuation_authority_required:
        try:
            ok = registry.heartbeat(
                issue_id=issue_id,
                run_id=entry.run_id,
                progress_at=progress,
                backend_agent_pid=effective_backend_pid,
                backend_process_identity=backend_process_identity,
                backend_process_identity_captured=identity_captured,
            )
        except Exception as exc:
            self._registry_error_count += 1
            self._last_registry_error = f"continuation_heartbeat: {exc}"
            entry.lease_lost = True
            log.error(
                "continuation_registry_error",
                op="heartbeat",
                issue_id=issue_id,
                error=str(exc),
            )
            return False
        self._last_registry_error = None
    else:
        ok = self._registry_guard(
            "heartbeat",
            lambda: registry.heartbeat(
                issue_id=issue_id,
                run_id=entry.run_id,
                progress_at=progress,
                backend_agent_pid=effective_backend_pid,
                backend_process_identity=backend_process_identity,
                backend_process_identity_captured=identity_captured,
            ),
            True,
        )
    if ok:
        return True
    if entry.lease_lost:
        return False
    if release_required:
        entry.lease_lost = True
        log.error(
            "release_run_lease_lost",
            issue_id=issue_id,
            issue_identifier=entry.issue.identifier,
        )
        return False
    new_run_id = self._registry_guard(
        "reacquire",
        lambda: registry.acquire_run(
            entry.issue,
            workspace_path=entry.workspace_path,
            attempt=entry.retry_attempt,
            attempt_kind="reacquired",
            agent_kind=entry.agent_kind,
        ),
        "",
    )
    if new_run_id == "":
        # Registry error mid-reacquire: keep the worker; health is
        # already flagged degraded by the guard.
        return True
    if new_run_id:
        entry.run_id = new_run_id
        log.warning(
            "run_lease_reacquired",
            issue_id=issue_id,
            issue_identifier=entry.issue.identifier,
            run_id=new_run_id,
        )
        return True
    entry.lease_lost = True
    log.error(
        "run_lease_conflict",
        issue_id=issue_id,
        issue_identifier=entry.issue.identifier,
    )
    return False


def _agent_pid_identity(self: Orchestrator, entry: RunningEntry) -> str | None:
    """Return the identity fingerprint recorded with this entry's pid.

        The registry's heartbeat persists ``process_identity(pid)`` as
        ``backend_process_identity`` at the moment `_sync_backend_agent_pid`
        first records the pid, so the fingerprint survives restarts. None
        when no registry/run row backs the entry, or when the platform
        cannot prove identity (win32) — callers pass ``identity=None``
        through, which keeps the ungated kill-with-warn-once behavior.
        Lookup failures degrade to None rather than blocking a legitimate
        kill: the fingerprint is hardening, not authority.
        """
    if entry.agent_process_identity_captured and is_job_identity(entry.agent_process_identity):
        return entry.agent_process_identity
    registry = self._run_registry
    if registry is None or not entry.run_id:
        return None
    try:
        record = registry.get_run(entry.run_id)
    except KeyError:
        return None
    except Exception:
        return None
    return record.backend_process_identity


def _sync_backend_agent_pid(
    self: Orchestrator,
    issue_id: str,
    backend_agent_pid: int | None,
    *,
    backend_process_identity: str | None = None,
    identity_captured: bool = False,
) -> bool:
    """Keep in-memory and persisted backend ownership in lockstep."""
    entry = self._running.get(issue_id)
    if entry is None:
        return False
    if (
        not identity_captured
        and backend_agent_pid is not None
        and entry.agent_pgid == backend_agent_pid
        and entry.agent_process_identity_captured
    ):
        backend_process_identity = entry.agent_process_identity
        identity_captured = True
    entry.agent_pgid = backend_agent_pid
    if backend_agent_pid is not None:
        if identity_captured:
            entry.agent_process_identity = backend_process_identity
            entry.agent_process_identity_captured = True
        if is_job_identity(backend_process_identity):
            # GO requires exact current-lease persistence, never best-effort
            # heartbeat defaults or reacquiring an identity-less replacement row.
            registry = self._run_registry
            try:
                lease_ok = bool(registry is not None and entry.run_id and registry.heartbeat(
                    issue_id=issue_id, run_id=entry.run_id,
                    backend_agent_pid=backend_agent_pid,
                    backend_process_identity=backend_process_identity,
                    backend_process_identity_captured=True,
                ) is True)
            except Exception as exc:
                self._registry_error_count += 1
                self._last_registry_error = f"windows_backend_heartbeat: {exc}"
                lease_ok = False
            if not lease_ok:
                entry.lease_lost = True
        else:
            lease_ok = self._heartbeat_run_lease(
                issue_id, entry, backend_agent_pid=backend_agent_pid,
                backend_process_identity=backend_process_identity,
                identity_captured=identity_captured,
            )
        if not lease_ok:
            task = entry.worker_task
            if task is not None and not task.done() and entry.cancelled_at is None:
                log.error(
                    "worker_cancelled_lease_conflict",
                    issue_id=issue_id,
                    issue_identifier=entry.issue.identifier,
                )
                task.cancel()
                entry.cancelled_at = datetime.now(timezone.utc)
        return lease_ok
    entry.agent_process_identity = None
    entry.agent_process_identity_captured = False
    registry = self._run_registry
    if registry is None or not entry.run_id:
        return True
    return self._registry_guard(
        "clear_backend_agent_pid",
        lambda: registry.clear_backend_agent_pid(
            issue_id=issue_id,
            run_id=entry.run_id,
        ),
        True,
    )


def _heartbeat_running_leases(self: Orchestrator) -> None:
    """Per-tick lease refresh; a conflicting holder stops our worker.

        Cancelling stamps `cancelled_at`, so the existing two-stage
        reconcile (cancel -> force-eject after grace) owns the cleanup if
        the worker is stuck on a non-cancellable await.
        """
    for issue_id, entry in list(self._running.items()):
        if self._heartbeat_run_lease(issue_id, entry):
            continue
        task = entry.worker_task
        if task is not None and not task.done() and entry.cancelled_at is None:
            log.error(
                "worker_cancelled_lease_conflict",
                issue_id=issue_id,
                issue_identifier=entry.issue.identifier,
            )
            task.cancel()
            entry.cancelled_at = datetime.now(timezone.utc)


def _finish_run_lease(
    self: Orchestrator,
    issue_id: str,
    entry: RunningEntry,
    status: str,
    error: str | None = None,
) -> None:
    registry = self._run_registry
    if registry is None or not entry.run_id:
        return
    if entry.backend_cleanup_unconfirmed:
        # Preserve the active owner/backend fence. The next service
        # instance must reclaim, kill/not-found the recorded process
        # group, and only then finalize the predecessor.
        log.warning(
            "run_lease_finish_deferred_for_backend_reap",
            issue_id=issue_id,
            issue_identifier=entry.issue.identifier,
            status=status,
            backend_agent_pid=entry.agent_pgid,
        )
        return
    permit = entry.pm_dispatch_permit
    completed = bool(self._registry_guard(
        "complete_run",
        lambda: registry.complete_run(
            issue_id=issue_id,
            run_id=entry.run_id,
            status=status,
            state=entry.issue.state,
            input_tokens=entry.codex_input_tokens,
            cache_input_tokens=entry.codex_cache_input_tokens,
            output_tokens=entry.codex_output_tokens,
            total_tokens=entry.codex_total_tokens,
            failure_class=(None if status == "normal" else status),
            failure_message=error,
            pm_dispatch_permit_id=(permit.permit_id if permit else None),
            pm_dispatch_fence=(permit.fence if permit else None),
        ),
        None,
    ))
    if not completed:
        log.error(
            "run_lease_finish_failed",
            issue_id=issue_id,
            issue_identifier=entry.issue.identifier,
            run_id=entry.run_id,
            permit_id=permit.permit_id if permit else None,
        )


def _entry_backend_cleanup_record(
    self: Orchestrator, issue_id: str, entry: RunningEntry
) -> RunRecord | None:
    registry = self._run_registry
    if registry is None or not entry.run_id:
        return None
    record = self._registry_guard(
        "get_run_for_backend_cleanup",
        lambda: registry.get_run(entry.run_id),
        None,
    )
    if not isinstance(record, RunRecord) or record.issue_id != issue_id:
        return None
    entry_pid = _normalize_agent_pid(entry.agent_pgid)
    if entry_pid != record.backend_agent_pid:
        log.warning(
            "worker_backend_cleanup_identity_unconfirmed",
            issue_id=issue_id,
            run_id=entry.run_id,
            entry_pid=entry_pid,
            recorded_pid=record.backend_agent_pid,
        )
        return None
    return record


def _confirm_entry_backend_cleanup(
    self: Orchestrator, issue_id: str, entry: RunningEntry
) -> bool:
    record = self._entry_backend_cleanup_record(issue_id, entry)
    if entry.run_id and self._run_registry is not None:
        outcome = _core()._reclaim_kill_confirm(record) if record is not None else None
        log.warning(
            "force_eject_killed_process_group",
            issue_id=issue_id,
            identifier=entry.issue.identifier,
            agent_kind=self._entry_agent_kind(entry),
            pid=_normalize_agent_pid(entry.agent_pgid),
            killed=outcome == "killed",
            cleanup_outcome=outcome or "unconfirmed",
        )
        return outcome is not None
    agent_pgid = _normalize_agent_pid(entry.agent_pgid)
    if agent_pgid is None:
        return True
    try:
        killed = bool(
            _core().kill_process_group(
                agent_pgid, identity=self._agent_pid_identity(entry)
            )
        )
    except Exception as exc:
        log.warning(
            "worker_process_kill_failed",
            issue_id=issue_id,
            agent_kind=self._entry_agent_kind(entry),
            pid=agent_pgid,
            error=str(exc),
        )
        return False
    log.warning(
        "force_eject_killed_process_group",
        issue_id=issue_id,
        identifier=entry.issue.identifier,
        agent_kind=self._entry_agent_kind(entry),
        pid=agent_pgid,
        killed=killed,
    )
    return killed


async def _confirm_entry_backend_cleanup_async(
    self: Orchestrator, issue_id: str, entry: RunningEntry
) -> bool:
    record = self._entry_backend_cleanup_record(issue_id, entry)
    if entry.run_id and self._run_registry is not None:
        if record is None:
            log.warning(
                "force_eject_killed_process_group",
                issue_id=issue_id,
                identifier=entry.issue.identifier,
                agent_kind=self._entry_agent_kind(entry),
                pid=_normalize_agent_pid(entry.agent_pgid),
                killed=False,
                cleanup_outcome="unconfirmed",
            )
            return False
        outcome = await asyncio.to_thread(_core()._reclaim_kill_confirm, record)
        log.warning(
            "force_eject_killed_process_group",
            issue_id=issue_id,
            identifier=entry.issue.identifier,
            agent_kind=self._entry_agent_kind(entry),
            pid=_normalize_agent_pid(entry.agent_pgid),
            killed=outcome == "killed",
            cleanup_outcome=outcome or "unconfirmed",
        )
        return outcome is not None
    agent_pgid = _normalize_agent_pid(entry.agent_pgid)
    if agent_pgid is None:
        return True
    try:
        killed = bool(
            await asyncio.to_thread(
                _core().kill_process_group,
                agent_pgid,
                identity=self._agent_pid_identity(entry),
            )
        )
    except Exception as exc:
        log.warning(
            "worker_process_kill_failed",
            issue_id=issue_id,
            agent_kind=self._entry_agent_kind(entry),
            pid=agent_pgid,
            error=str(exc),
        )
        return False
    log.warning(
        "force_eject_killed_process_group",
        issue_id=issue_id,
        identifier=entry.issue.identifier,
        agent_kind=self._entry_agent_kind(entry),
        pid=agent_pgid,
        killed=killed,
    )
    return killed


def _try_acquire_run_lease(
    self: Orchestrator,
    *,
    cfg: ServiceConfig,
    issue: Issue,
    workspace_path: Path,
    attempt: int | None,
    attempt_kind: str,
    agent_kind: str,
    release_required: bool = False,
) -> _RunLeaseAcquisition | None:
    if release_required:
        try:
            run_id = self._release_registry_call(
                cfg,
                "acquire_release_run",
                lambda registry: registry.acquire_run(
                    issue,
                    workspace_path=workspace_path,
                    attempt=attempt,
                    attempt_kind=attempt_kind,
                    agent_kind=agent_kind,
                ),
            )
        except SymphonyError:
            return None
        if run_id:
            return _RunLeaseAcquisition(run_id=cast(str, run_id))
        log.info(
            "dispatch_lease_held",
            issue_id=issue.id,
            issue_identifier=issue.identifier,
        )
        return None
    registry = self._run_registry
    if registry is None:
        if cfg.agent.crash_continuation and self._run_registry_initialized:
            return None
        return _RunLeaseAcquisition(run_id="")

    if cfg.agent.crash_continuation:
        try:
            source_run_id = registry.latest_continuation_source(
                issue_id=issue.id,
                agent_kind=agent_kind,
                state=issue.state,
                issue_updated_at=issue.updated_at,
            )
            if source_run_id is not None:
                recovered = registry.acquire_continuation_run(
                    issue,
                    continued_from_run_id=source_run_id,
                    workspace_path=workspace_path,
                    attempt=attempt,
                    attempt_kind="recovery",
                    agent_kind=agent_kind,
                )
                if recovered is None:
                    # Discovery raced or the source became ineligible. Do
                    # not degrade an authoritative continuation decision
                    # into a second fresh writer on this tick.
                    return None
                self._last_registry_error = None
                return _RunLeaseAcquisition(
                    run_id=recovered.run_id,
                    continued_from_run_id=recovered.continued_from_run_id,
                    checkpoint=recovered.checkpoint,
                )
        except Exception as exc:
            # Session continuation is authority, not telemetry. A broken
            # read/claim fails closed even though ordinary lease
            # bookkeeping retains its historical fail-open behavior.
            self._registry_error_count += 1
            self._last_registry_error = f"acquire_continuation: {exc}"
            log.error(
                "run_registry_error",
                op="acquire_continuation",
                error=str(exc),
            )
            return None

    run_id = self._registry_guard(
        "acquire_run",
        lambda: registry.acquire_run(
            issue,
            workspace_path=workspace_path,
            attempt=attempt,
            attempt_kind=attempt_kind,
            agent_kind=agent_kind,
        ),
        "",
    )
    if run_id == "":
        if cfg.agent.crash_continuation:
            # Recovery authority cannot degrade into a leaseless writer.
            return None
        # Explicit opt-out preserves the historical fail-open lease path.
        return _RunLeaseAcquisition(run_id="")
    if run_id:
        return _RunLeaseAcquisition(run_id=run_id)
    log.info(
        "dispatch_lease_held",
        issue_id=issue.id,
        issue_identifier=issue.identifier,
    )
    return None
