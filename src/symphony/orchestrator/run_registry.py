"""SQLite-backed run registry for crash-safe dispatch leases."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, cast

from .._shell import process_identity
from ..issue import Issue
from .diagnostics import (
    MAX_DIAGNOSTIC_COUNTER,
    MAX_EVENTS_PER_RUN,
    MAX_RUNS_WITH_DIAGNOSTIC_EVENTS,
    event_payload_json,
    redact_text,
)
from .run_registry_base import _RunRegistryBase
from .run_registry_pm import _PmRegistry
from .run_registry_release import _ReleaseRegistry
from .run_registry_records import (
    DEFAULT_LEASE_TTL as DEFAULT_LEASE_TTL,
    SQLITE_BUSY_TIMEOUT_S as SQLITE_BUSY_TIMEOUT_S,
    MAX_RESUME_SESSION_ID_BYTES as MAX_RESUME_SESSION_ID_BYTES,
    MAX_RESUME_SESSION_ID_CHARS as MAX_RESUME_SESSION_ID_CHARS,
    MAX_CHECKPOINT_STATE_BYTES as MAX_CHECKPOINT_STATE_BYTES,
    ContinuationCheckpoint as ContinuationCheckpoint,
    ContinuationAcquisition as ContinuationAcquisition,
    RunRecord as RunRecord,
    IssueFlags as IssueFlags,
    ReleaseGate as ReleaseGate,
    ReleaseEvidenceIdentity as ReleaseEvidenceIdentity,
    ReleaseCycleItem as ReleaseCycleItem,
    PmWorkspaceRecord as PmWorkspaceRecord,
    PmManagedTicketRecord as PmManagedTicketRecord,
    PmDispatchPermitRecord as PmDispatchPermitRecord,
    registry_path_for_workflow as registry_path_for_workflow,
    clamp_run_history_limit as clamp_run_history_limit,
    _pid_alive_win32 as _pid_alive_win32,
    _pid_alive,
    _validate_checkpoint_fields,
    _optional_nonnegative,
    _valid_sha,
    _like_pattern,
    _run_summary,
    _utc,
    _iso,
    _parse,
    _record,
    _issue_flags,
)


_UNSET = object()


class RunRegistry(_ReleaseRegistry, _PmRegistry, _RunRegistryBase):
    """Persist one active dispatch lease per issue in `.symphony/state.db`."""

    def acquire_run(
        self,
        issue: Issue,
        *,
        workspace_path: Path,
        attempt: int | None,
        attempt_kind: str,
        agent_kind: str,
        now: datetime | None = None,
    ) -> str | None:
        now = _utc(now)
        expires = now + self._lease_ttl
        run_id = uuid.uuid4().hex
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._expire_stale_locked(now)
            if self._active_issue_locked(issue.id, now) is not None:
                conn.execute("COMMIT")
                return None
            converted = conn.execute(
                "SELECT 1 FROM pm_legacy_conversions WHERE source_ticket_identifier = ? LIMIT 1",
                (issue.identifier,),
            ).fetchone()
            if converted is not None:
                conn.execute("COMMIT")
                return None
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, issue_id, identifier, title, state, attempt,
                    attempt_kind, agent_kind, workspace_path, status, started_at,
                    updated_at, lease_expires_at, last_progress_at, completed_at,
                    owner_pid, owner_boot_id, branch_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    run_id,
                    issue.id,
                    issue.identifier,
                    issue.title,
                    issue.state,
                    attempt,
                    attempt_kind,
                    agent_kind,
                    str(workspace_path),
                    _iso(now),
                    _iso(now),
                    _iso(expires),
                    _iso(now),
                    self._owner_pid,
                    self._boot_id,
                    f"symphony/{issue.identifier}",
                ),
            )
            self._append_attempt_event_best_effort_locked(
                run_id=run_id,
                event_type="run_acquired",
                payload={
                    "attempt": attempt,
                    "attempt_kind": attempt_kind,
                    "agent_kind": agent_kind,
                    "state": issue.state,
                },
                now=now,
            )
            conn.execute("COMMIT")
            return run_id
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def latest_continuation_source(
        self,
        *,
        issue_id: str,
        agent_kind: str,
        state: str,
        issue_updated_at: datetime | None = None,
    ) -> str | None:
        """Return the newest eligible, unconsumed recovery source.

        This is intentionally only a discovery hint. Acquisition repeats all
        predicates under ``BEGIN IMMEDIATE`` so a stale answer is harmless.
        """
        row = self._connect().execute(
            """
            SELECT source.*
            FROM runs AS source
            WHERE source.issue_id = ?
              AND source.agent_kind = ?
              AND source.state = ?
              AND source.checkpoint_state = ?
              AND ((source.status = 'orphaned' AND source.failure_class = 'orphaned')
                   OR source.status = 'shutdown_interrupted')
              AND source.backend_agent_pid IS NULL
              AND source.completed_at IS NOT NULL
              AND source.lease_expires_at IS NULL
              AND source.resume_session_id IS NOT NULL
              AND length(CAST(source.resume_session_id AS BLOB)) BETWEEN 1 AND ?
              AND source.checkpoint_turn BETWEEN 1 AND ?
              AND source.checkpointed_at IS NOT NULL
              AND source.checkpointed_at >= ?
              AND NOT EXISTS (
                  SELECT 1 FROM runs AS successor
                  WHERE successor.continued_from_run_id = source.run_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM runs AS newer
                  WHERE newer.issue_id = source.issue_id
                    AND newer.rowid > source.rowid
              )
            ORDER BY source.checkpointed_at DESC, source.rowid DESC
            LIMIT 1
            """,
            (
                issue_id,
                agent_kind,
                state,
                state,
                MAX_RESUME_SESSION_ID_BYTES,
                MAX_DIAGNOSTIC_COUNTER,
                _iso(issue_updated_at) if issue_updated_at is not None else "",
            ),
        ).fetchone()
        if row is None:
            return None
        try:
            checkpointed_at = _parse(str(row["checkpointed_at"]))
            if checkpointed_at is None:
                return None
            ContinuationCheckpoint(
                resume_session_id=str(row["resume_session_id"]),
                state=str(row["checkpoint_state"]),
                turn=int(row["checkpoint_turn"]),
                checkpointed_at=checkpointed_at,
            )
        except (TypeError, ValueError, OverflowError):
            return None
        return str(row["run_id"])

    def acquire_continuation_run(
        self,
        issue: Issue,
        *,
        continued_from_run_id: str,
        workspace_path: Path,
        attempt: int | None,
        attempt_kind: str,
        agent_kind: str,
        now: datetime | None = None,
    ) -> ContinuationAcquisition | None:
        """Atomically consume one safe checkpoint into a new active run.

        Only kill-confirmed orphan rows and gracefully stopped rows whose
        backend pid was cleared are eligible. The explicit predecessor id and
        exact issue/identifier/agent/state tuple prevent a stale discovery
        result from resuming into a different dispatch.
        """
        now = _utc(now)
        expires = now + self._lease_ttl
        run_id = uuid.uuid4().hex
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Preserve ordinary lease compatibility, while never selecting an
            # active/reclaiming predecessor as a continuation source.
            self._expire_stale_locked(now)
            if self._active_issue_locked(issue.id, now) is not None:
                conn.execute("COMMIT")
                return None
            source = conn.execute(
                """
                SELECT * FROM runs AS source
                WHERE source.run_id = ?
                  AND source.issue_id = ?
                  AND source.identifier = ?
                  AND source.agent_kind = ?
                  AND source.state = ?
                  AND source.checkpoint_state = ?
                  AND ((source.status = 'orphaned' AND source.failure_class = 'orphaned')
                   OR source.status = 'shutdown_interrupted')
                  AND source.backend_agent_pid IS NULL
                  AND source.completed_at IS NOT NULL
                  AND source.lease_expires_at IS NULL
                  AND source.resume_session_id IS NOT NULL
                  AND source.checkpoint_turn IS NOT NULL
                  AND source.checkpointed_at IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM runs AS successor
                      WHERE successor.continued_from_run_id = source.run_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM runs AS newer
                      WHERE newer.issue_id = source.issue_id
                        AND newer.rowid > source.rowid
                  )
                LIMIT 1
                """,
                (
                    continued_from_run_id,
                    issue.id,
                    issue.identifier,
                    agent_kind,
                    issue.state,
                    issue.state,
                ),
            ).fetchone()
            if source is None:
                conn.execute("COMMIT")
                return None
            try:
                checkpointed_at = _parse(str(source["checkpointed_at"]))
                if checkpointed_at is None:
                    conn.execute("ROLLBACK")
                    return None
                checkpoint = ContinuationCheckpoint(
                    resume_session_id=str(source["resume_session_id"]),
                    state=str(source["checkpoint_state"]),
                    turn=int(source["checkpoint_turn"]),
                    checkpointed_at=checkpointed_at,
                )
                if issue.updated_at is not None and _utc(issue.updated_at) > checkpointed_at:
                    conn.execute("ROLLBACK")
                    return None
            except (TypeError, ValueError, OverflowError):
                # Corrupt or manually edited recovery material never degrades
                # into a fresh-looking continuation lease.
                conn.execute("ROLLBACK")
                return None
            try:
                conn.execute(
                    """
                    INSERT INTO runs (
                        run_id, issue_id, identifier, title, state, attempt,
                        attempt_kind, agent_kind, workspace_path, status,
                        started_at, updated_at, lease_expires_at,
                        last_progress_at, completed_at, owner_pid,
                        owner_boot_id, branch_name, resume_session_id,
                        checkpoint_state, checkpoint_turn, checkpointed_at,
                        continued_from_run_id
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, NULL,
                        ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        run_id,
                        issue.id,
                        issue.identifier,
                        issue.title,
                        issue.state,
                        attempt,
                        attempt_kind,
                        agent_kind,
                        str(workspace_path),
                        _iso(now),
                        _iso(now),
                        _iso(expires),
                        _iso(now),
                        self._owner_pid,
                        self._boot_id,
                        f"symphony/{issue.identifier}",
                        checkpoint.resume_session_id,
                        checkpoint.state,
                        checkpoint.turn,
                        _iso(checkpoint.checkpointed_at),
                        continued_from_run_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # The partial unique index is the final cross-process
                # one-successor fence if discovery raced. Other integrity
                # failures are registry errors and must not look like "no
                # eligible checkpoint" to a caller that may fall back fresh.
                if "runs.continued_from_run_id" not in str(exc):
                    raise
                conn.execute("ROLLBACK")
                return None
            self._append_attempt_event_best_effort_locked(
                run_id=run_id,
                event_type="run_acquired",
                payload={
                    "attempt": attempt,
                    "attempt_kind": attempt_kind,
                    "agent_kind": agent_kind,
                    "state": issue.state,
                },
                now=now,
            )
            conn.execute("COMMIT")
            return ContinuationAcquisition(
                run_id=run_id,
                continued_from_run_id=continued_from_run_id,
                checkpoint=checkpoint,
            )
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def checkpoint_completed_turn(
        self,
        *,
        issue_id: str,
        run_id: str,
        resume_session_id: str,
        state: str,
        turn: int,
        now: datetime | None = None,
    ) -> bool:
        """Persist the newest fully completed turn for this exact run owner."""
        _validate_checkpoint_fields(
            resume_session_id=resume_session_id,
            state=state,
            turn=turn,
        )
        now = _utc(now)
        cur = self._connect().execute(
            """
            UPDATE runs
            SET resume_session_id = ?, checkpoint_state = ?,
                checkpoint_turn = ?, checkpointed_at = ?, state = ?,
                updated_at = ?
            WHERE issue_id = ?
              AND run_id = ?
              AND status = 'active'
              AND owner_pid = ?
              AND owner_boot_id = ?
              AND (checkpoint_turn IS NULL OR checkpoint_turn < ?)
            """,
            (
                resume_session_id,
                state,
                turn,
                _iso(now),
                state,
                _iso(now),
                issue_id,
                run_id,
                self._owner_pid,
                self._boot_id,
                turn,
            ),
        )
        return cur.rowcount == 1

    def heartbeat(
        self,
        *,
        issue_id: str,
        run_id: str,
        now: datetime | None = None,
        progress_at: datetime | None = None,
        backend_agent_pid: int | None = None,
        backend_process_identity: str | None = None,
        backend_process_identity_captured: bool = False,
    ) -> bool:
        now = _utc(now)
        expires = now + self._lease_ttl
        progress = _utc(progress_at) if progress_at is not None else None
        conn = self._connect()
        backend_identity: str | None = None
        if backend_agent_pid is not None:
            if backend_process_identity_captured:
                backend_identity = backend_process_identity
            else:
                existing = conn.execute(
                    """
                    SELECT backend_agent_pid, backend_process_identity
                    FROM runs WHERE issue_id = ? AND run_id = ? AND status = 'active'
                    """,
                    (issue_id, run_id),
                ).fetchone()
                if (
                    existing is not None
                    and existing["backend_agent_pid"] == backend_agent_pid
                    and existing["backend_process_identity"]
                ):
                    backend_identity = str(existing["backend_process_identity"])
                else:
                    backend_identity = process_identity(backend_agent_pid)
        if progress is None and backend_agent_pid is None:
            sql = """
                UPDATE runs
                SET updated_at = ?, lease_expires_at = ?
                WHERE issue_id = ? AND run_id = ? AND status = 'active'
            """
            args = (_iso(now), _iso(expires), issue_id, run_id)
        elif progress is None:
            sql = """
                UPDATE runs
                SET updated_at = ?, lease_expires_at = ?, backend_agent_pid = ?,
                    backend_process_identity = ?
                WHERE issue_id = ? AND run_id = ? AND status = 'active'
            """
            args = (
                _iso(now),
                _iso(expires),
                backend_agent_pid,
                backend_identity,
                issue_id,
                run_id,
            )
        elif backend_agent_pid is None:
            sql = """
                UPDATE runs
                SET updated_at = ?, lease_expires_at = ?, last_progress_at = ?
                WHERE issue_id = ? AND run_id = ? AND status = 'active'
            """
            args = (_iso(now), _iso(expires), _iso(progress), issue_id, run_id)
        else:
            sql = """
                UPDATE runs
                SET updated_at = ?, lease_expires_at = ?, last_progress_at = ?,
                    backend_agent_pid = ?, backend_process_identity = ?
                WHERE issue_id = ? AND run_id = ? AND status = 'active'
            """
            args = (
                _iso(now),
                _iso(expires),
                _iso(progress),
                backend_agent_pid,
                backend_identity,
                issue_id,
                run_id,
            )
        cur = conn.execute(sql, args)
        if cur.rowcount > 0:
            # A live run keeps the PM dispatch permit it launched under, so a
            # later turn or phase of the same run is not refused as stale.
            # Only permits that are still unexpired and owned by this run are
            # extended; expired or revoked authority is never revived.
            conn.execute(
                """
                UPDATE pm_dispatch_permits
                SET lease_expires_at = ?, updated_at = ?
                WHERE run_id = ? AND state IN ('STARTING', 'RUNNING')
                  AND lease_expires_at > ? AND lease_expires_at < ?
                """,
                (_iso(expires), _iso(now), run_id, _iso(now), _iso(expires)),
            )
        return cur.rowcount > 0


    def clear_backend_agent_pid(
        self,
        *,
        issue_id: str,
        run_id: str,
        now: datetime | None = None,
    ) -> bool:
        """Clear process ownership without overloading heartbeat(None)."""
        now = _utc(now)
        expires = now + self._lease_ttl
        cur = self._connect().execute(
            """
            UPDATE runs
            SET updated_at = ?, lease_expires_at = ?, backend_agent_pid = NULL,
                backend_process_identity = NULL
            WHERE issue_id = ? AND run_id = ? AND status = 'active'
            """,
            (_iso(now), _iso(expires), issue_id, run_id),
        )
        return cur.rowcount > 0

    def complete_run(
        self,
        *,
        issue_id: str,
        run_id: str,
        status: str,
        now: datetime | None = None,
        state: str | None = None,
        input_tokens: int | None = None,
        cache_input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        failure_class: str | None = None,
        failure_message: str | None = None,
        commit_sha: str | None = None,
        pm_dispatch_permit_id: str | None = None,
        pm_dispatch_fence: int | None = None,
    ) -> bool:
        """Complete a run while preserving source compatibility for old callers.

        The terminal status and lease release are authoritative. Explorer
        summary fields and the completion event are best-effort inside a
        savepoint, so malformed or unavailable telemetry cannot strand a run.
        """
        now = _utc(now)
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = conn.execute(
                "SELECT * FROM runs WHERE issue_id = ? AND run_id = ?",
                (issue_id, run_id),
            ).fetchone()
            if current is None:
                conn.execute("ROLLBACK")
                return False
            conn.execute(
                """
                UPDATE runs
                SET status = ?, updated_at = ?, completed_at = ?,
                    lease_expires_at = NULL
                WHERE issue_id = ? AND run_id = ?
                """,
                (status, _iso(now), _iso(now), issue_id, run_id),
            )
            if pm_dispatch_permit_id is not None:
                if pm_dispatch_fence is None or not self._consume_pm_dispatch_permit_locked(
                    conn,
                    pm_dispatch_permit_id,
                    fence=pm_dispatch_fence,
                    run_id=run_id,
                    stamp=_iso(now),
                ):
                    conn.execute("ROLLBACK")
                    return False
            try:
                conn.execute("SAVEPOINT run_diagnostic")
            except Exception:
                conn.execute("COMMIT")
                return True
            try:
                values = {
                    "state": state if state is not None else str(current["state"]),
                    "input_tokens": _optional_nonnegative(
                        input_tokens, current["input_tokens"]
                    ),
                    "cache_input_tokens": _optional_nonnegative(
                        cache_input_tokens, current["cache_input_tokens"]
                    ),
                    "output_tokens": _optional_nonnegative(
                        output_tokens, current["output_tokens"]
                    ),
                    "total_tokens": _optional_nonnegative(
                        total_tokens, current["total_tokens"]
                    ),
                    "failure_class": (
                        redact_text(failure_class, 256) if failure_class else None
                    ),
                    "failure_message": (
                        redact_text(failure_message) if failure_message else None
                    ),
                    "commit_sha": _valid_sha(commit_sha) or current["commit_sha"],
                }
                conn.execute(
                    """
                    UPDATE runs
                    SET state = ?, input_tokens = ?, cache_input_tokens = ?,
                        output_tokens = ?, total_tokens = ?, failure_class = ?,
                        failure_message = ?, commit_sha = ?
                    WHERE issue_id = ? AND run_id = ?
                    """,
                    (
                        values["state"],
                        values["input_tokens"],
                        values["cache_input_tokens"],
                        values["output_tokens"],
                        values["total_tokens"],
                        values["failure_class"],
                        values["failure_message"],
                        values["commit_sha"],
                        issue_id,
                        run_id,
                    ),
                )
                self._append_attempt_event_locked(
                    run_id=run_id,
                    event_type="run_completed",
                    payload={"status": status, **values},
                    now=now,
                )
                self._prune_terminal_diagnostics_locked()
            except Exception:
                conn.execute("ROLLBACK TO run_diagnostic")
            finally:
                conn.execute("RELEASE run_diagnostic")
            conn.execute("COMMIT")
            return True
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def append_attempt_event(
        self,
        *,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Append one bounded event without waiting behind authoritative writers."""
        now = _utc(now)
        conn = self._connect()
        row = conn.execute("PRAGMA busy_timeout").fetchone()
        prior_busy_timeout_ms = int(row[0]) if row is not None else 0
        began = False
        try:
            # This method is observational and runs on the asyncio control
            # thread. Drop on cross-process contention instead of delaying
            # worker events or authoritative lease heartbeats for up to 5s.
            conn.execute("PRAGMA busy_timeout = 0")
            conn.execute("BEGIN IMMEDIATE")
            began = True
            owned = conn.execute(
                """
                SELECT status, owner_pid, owner_boot_id
                FROM runs WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if (
                owned is None
                or str(owned["status"]) != "active"
                or owned["owner_pid"] != self._owner_pid
                or owned["owner_boot_id"] != self._boot_id
            ):
                conn.execute("ROLLBACK")
                began = False
                return False
            normalized_json = event_payload_json(event_type, payload)
            self._append_attempt_event_locked(
                run_id=run_id,
                event_type=event_type,
                payload_json=normalized_json,
                now=now,
            )
            if event_type in {"turn_completed", "workspace_updated"}:
                normalized = json.loads(normalized_json)
                assignments: list[str] = []
                values: list[Any] = []
                for field in (
                    "input_tokens",
                    "cache_input_tokens",
                    "output_tokens",
                    "total_tokens",
                ):
                    if field in normalized:
                        assignments.append(f"{field} = ?")
                        values.append(normalized[field])
                if normalized.get("commit_sha"):
                    assignments.append("commit_sha = ?")
                    values.append(normalized["commit_sha"])
                if assignments:
                    values.append(run_id)
                    conn.execute(
                        f"UPDATE runs SET {', '.join(assignments)} WHERE run_id = ?",
                        values,
                    )
            conn.execute("COMMIT")
            began = False
            return True
        except Exception:
            if began and conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute(f"PRAGMA busy_timeout = {prior_busy_timeout_ms}")

    def _append_attempt_event_locked(
        self,
        *,
        run_id: str,
        event_type: str,
        now: datetime,
        payload: Mapping[str, Any] | None = None,
        payload_json: str | None = None,
    ) -> None:
        encoded = payload_json if payload_json is not None else event_payload_json(event_type, payload)
        conn = self._connect()
        conn.execute(
            "INSERT INTO attempt_events(run_id, event_type, created_at, payload_json) VALUES (?, ?, ?, ?)",
            (run_id, event_type, _iso(now), encoded),
        )
        conn.execute(
            """
            DELETE FROM attempt_events
            WHERE run_id = ? AND event_id NOT IN (
                SELECT event_id FROM attempt_events
                WHERE run_id = ? ORDER BY event_id DESC LIMIT ?
            )
            """,
            (run_id, run_id, MAX_EVENTS_PER_RUN),
        )

    def _append_attempt_event_best_effort_locked(
        self,
        *,
        run_id: str,
        event_type: str,
        now: datetime,
        payload: Mapping[str, Any] | None = None,
        prune_terminal: bool = False,
    ) -> None:
        """Fence optional telemetry so it cannot roll back an owning transaction."""
        conn = self._connect()
        try:
            conn.execute("SAVEPOINT run_diagnostic")
        except Exception:
            return
        try:
            self._append_attempt_event_locked(
                run_id=run_id,
                event_type=event_type,
                payload=payload,
                now=now,
            )
            if prune_terminal:
                self._prune_terminal_diagnostics_locked()
        except Exception:
            conn.execute("ROLLBACK TO run_diagnostic")
        finally:
            conn.execute("RELEASE run_diagnostic")

    def _prune_terminal_diagnostics_locked(self) -> None:
        """Keep events/excerpts only for the newest bounded terminal run set."""
        conn = self._connect()
        stale = """
            SELECT run_id FROM runs
            WHERE status NOT IN ('active', 'reclaiming')
            ORDER BY completed_at DESC, rowid DESC
            LIMIT -1 OFFSET ?
        """
        conn.execute(
            f"DELETE FROM attempt_events WHERE run_id IN ({stale})",
            (MAX_RUNS_WITH_DIAGNOSTIC_EVENTS,),
        )
        conn.execute(
            f"UPDATE runs SET failure_message = NULL WHERE run_id IN ({stale})",
            (MAX_RUNS_WITH_DIAGNOSTIC_EVENTS,),
        )

    def has_active_lease(self, issue_id: str, now: datetime | None = None) -> bool:
        now = _utc(now)
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._expire_stale_locked(now)
            found = self._active_issue_locked(issue_id, now) is not None
            conn.execute("COMMIT")
            return found
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def active_leases(self, now: datetime | None = None) -> list[RunRecord]:
        now = _utc(now)
        self.expire_stale(now=now)
        rows = self._connect().execute(
            """
            SELECT * FROM runs
            WHERE status = 'reclaiming'
               OR (status = 'active' AND lease_expires_at > ?)
            ORDER BY started_at, run_id
            """,
            (_iso(now),),
        )
        return [_record(row) for row in rows.fetchall()]

    def expire_stale(self, now: datetime | None = None) -> int:
        now = _utc(now)
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            count = self._expire_stale_locked(now)
            conn.execute("COMMIT")
            return count
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def reclaim_dead_owner_leases(
        self,
        now: datetime | None = None,
        *,
        pid_alive: Callable[[int], bool] | None = None,
    ) -> list[RunRecord]:
        """Free unexpired leases whose owner process is gone.

        A crashed process's last heartbeat can push `lease_expires_at` up to
        a full TTL into the future; without this pass a restart cannot
        re-dispatch the interrupted ticket for minutes. Leases owned by this
        registry instance (same boot id) or by a live pid are left alone —
        the safe direction for pid reuse is to wait out the TTL.
        Rows first enter `reclaiming`, which remains lease-blocking while the
        caller performs OS cleanup outside this transaction. Existing
        `reclaiming` rows are returned again so a crash between claim, kill,
        and finalize is retry-safe.
        """
        now = _utc(now)
        alive = pid_alive or _pid_alive
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                """
                SELECT * FROM runs
                WHERE status IN ('reclaiming', 'active')
                ORDER BY started_at, run_id
                """
            ).fetchall()
            reclaimed: list[RunRecord] = []
            for row in rows:
                if row["status"] == "reclaiming":
                    reclaimed.append(_record(row))
                    continue
                if row["owner_boot_id"] == self._boot_id:
                    continue
                pid = row["owner_pid"]
                lease_expires_at = _parse(row["lease_expires_at"])
                lease_expired = (
                    lease_expires_at is not None and lease_expires_at <= now
                )
                if pid is not None and alive(int(pid)) and not lease_expired:
                    continue
                conn.execute(
                    """
                    UPDATE runs
                    SET status = 'reclaiming', updated_at = ?, completed_at = NULL
                    WHERE run_id = ?
                    """,
                    (_iso(now), row["run_id"]),
                )
                claimed = conn.execute(
                    "SELECT * FROM runs WHERE run_id = ?", (row["run_id"],)
                ).fetchone()
                if claimed is not None:
                    reclaimed.append(_record(claimed))
            conn.execute("COMMIT")
            return reclaimed
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def finalize_reclaimed_lease(
        self, run_id: str, now: datetime | None = None
    ) -> bool:
        """Release a reclaim fence after external process cleanup completes."""
        now = _utc(now)
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = conn.execute(
                "SELECT identifier FROM runs WHERE run_id = ? AND status = 'reclaiming'",
                (run_id,),
            ).fetchone()
            if current is None:
                conn.execute("ROLLBACK")
                return False
            cur = conn.execute(
                """
                UPDATE runs
                SET status = 'orphaned', updated_at = ?, completed_at = ?,
                    lease_expires_at = NULL, backend_agent_pid = NULL,
                    backend_process_identity = NULL, failure_class = 'orphaned'
                WHERE run_id = ? AND status = 'reclaiming'
                """,
                (_iso(now), _iso(now), run_id),
            )
            if cur.rowcount:
                conn.execute(
                    """
                    UPDATE pm_dispatch_permits
                    SET state = 'CONSUMED', updated_at = ?
                    WHERE run_id = ? AND ticket_identifier = ?
                      AND state = 'RUNNING'
                    """,
                    (_iso(now), run_id, str(current["identifier"])),
                )
                self._append_attempt_event_best_effort_locked(
                    run_id=run_id,
                    event_type="run_completed",
                    payload={"status": "orphaned", "failure_class": "orphaned"},
                    now=now,
                    prune_terminal=True,
                )
            conn.execute("COMMIT")
            return cur.rowcount > 0
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def get_run(self, run_id: str) -> RunRecord:
        row = self._connect().execute(
            "SELECT * FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return _record(row)

    def recent_runs(
        self,
        issue_id: str | None = None,
        limit: int = 50,
        *,
        query: str | None = None,
        status: str | None = None,
        agent: str | None = None,
    ) -> list[RunRecord]:
        """Return newest rows with optional, backwards-compatible filters."""
        limit = clamp_run_history_limit(limit)
        clauses: list[str] = []
        params: list[Any] = []
        if issue_id:
            clauses.append("(issue_id = ? OR identifier = ?)")
            params.extend((issue_id, issue_id))
        if query:
            escaped = _like_pattern(query)
            clauses.append(
                "(issue_id LIKE ? ESCAPE '\\' OR identifier LIKE ? ESCAPE '\\' "
                "OR title LIKE ? ESCAPE '\\' OR agent_kind LIKE ? ESCAPE '\\' "
                "OR status LIKE ? ESCAPE '\\')"
            )
            params.extend((escaped, escaped, escaped, escaped, escaped))
        if status:
            clauses.append("status = ?")
            params.append(status)
        if agent:
            clauses.append("agent_kind = ?")
            params.append(agent)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self._connect().execute(
            f"SELECT * FROM runs {where} ORDER BY rowid DESC LIMIT ?",
            params,
        ).fetchall()
        return [_record(row) for row in rows]

    def run_events(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._connect().execute(
            """
            SELECT event_id, event_type, created_at, payload_json
            FROM attempt_events WHERE run_id = ? ORDER BY event_id
            """,
            (run_id,),
        ).fetchall()
        return [
            {
                "event_id": int(row["event_id"]),
                "event_type": str(row["event_type"]),
                "created_at": str(row["created_at"]),
                "payload": json.loads(str(row["payload_json"])),
            }
            for row in rows
        ]

    def run_detail(self, run_id: str) -> dict[str, Any]:
        """Read summary and events from one SQLite snapshot."""
        conn = self._connect()
        conn.execute("BEGIN")
        try:
            record = self.get_run(run_id)
            events = self.run_events(run_id)
            conn.execute("COMMIT")
            return {"run": _run_summary(record), "events": events}
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def diagnostic_json(self, run_id: str) -> dict[str, Any]:
        detail = self.run_detail(run_id)
        return {"schema_version": 1, **detail}

    def get_issue_flags(self, issue_id: str) -> IssueFlags | None:
        row = self._connect().execute(
            "SELECT * FROM issue_flags WHERE issue_id = ?",
            (issue_id,),
        ).fetchone()
        return _issue_flags(row) if row is not None else None

    def list_issue_flags(self) -> list[IssueFlags]:
        rows = self._connect().execute(
            "SELECT * FROM issue_flags ORDER BY issue_id"
        ).fetchall()
        return [_issue_flags(row) for row in rows]

    def set_issue_flags(
        self,
        issue_id: str,
        *,
        retry_attempt: int | None | object = _UNSET,
        budget_exhausted: bool | object = _UNSET,
        paused: bool | object = _UNSET,
        pause_reason: str | None | object = _UNSET,
        now: datetime | None = None,
    ) -> None:
        existing = self.get_issue_flags(issue_id)
        next_retry_attempt = (
            existing.retry_attempt if existing is not None else None
        )
        next_budget_exhausted = (
            existing.budget_exhausted if existing is not None else False
        )
        next_paused = existing.paused if existing is not None else False
        next_pause_reason = existing.pause_reason if existing is not None else None
        if retry_attempt is not _UNSET:
            next_retry_attempt = cast("int | None", retry_attempt)
        if budget_exhausted is not _UNSET:
            next_budget_exhausted = bool(budget_exhausted)
        if paused is not _UNSET:
            next_paused = bool(paused)
        if pause_reason is not _UNSET:
            next_pause_reason = cast("str | None", pause_reason)
        elif not next_paused:
            next_pause_reason = None
        self._write_issue_flags(
            issue_id,
            retry_attempt=next_retry_attempt,
            budget_exhausted=next_budget_exhausted,
            paused=next_paused,
            pause_reason=next_pause_reason,
            now=now,
        )

    def clear_issue_flags(
        self,
        issue_id: str,
        *,
        retry_attempt: bool = False,
        budget_exhausted: bool = False,
        paused: bool = False,
        now: datetime | None = None,
    ) -> None:
        existing = self.get_issue_flags(issue_id)
        if existing is None:
            return
        self._write_issue_flags(
            issue_id,
            retry_attempt=None if retry_attempt else existing.retry_attempt,
            budget_exhausted=False if budget_exhausted else existing.budget_exhausted,
            paused=False if paused else existing.paused,
            pause_reason=None if paused else existing.pause_reason,
            now=now,
        )

    def _write_issue_flags(
        self,
        issue_id: str,
        *,
        retry_attempt: int | None,
        budget_exhausted: bool,
        paused: bool,
        pause_reason: str | None,
        now: datetime | None,
    ) -> None:
        conn = self._connect()
        if not paused:
            pause_reason = None
        if retry_attempt is None and not budget_exhausted and not paused:
            conn.execute("DELETE FROM issue_flags WHERE issue_id = ?", (issue_id,))
            return
        updated_at = _iso(_utc(now))
        conn.execute(
            """
            INSERT INTO issue_flags (
                issue_id, retry_attempt, budget_exhausted, paused,
                pause_reason, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(issue_id) DO UPDATE SET
                retry_attempt = excluded.retry_attempt,
                budget_exhausted = excluded.budget_exhausted,
                paused = excluded.paused,
                pause_reason = excluded.pause_reason,
                updated_at = excluded.updated_at
            """,
            (
                issue_id,
                retry_attempt,
                1 if budget_exhausted else 0,
                1 if paused else 0,
                pause_reason,
                updated_at,
            ),
        )

    def _expire_stale_locked(self, now: datetime) -> int:
        conn = self._connect()
        expired = conn.execute(
            """
            SELECT run_id FROM runs
            WHERE status = 'active'
              AND backend_agent_pid IS NULL
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at <= ?
            ORDER BY rowid
            """,
            (_iso(now),),
        ).fetchall()
        cur = conn.execute(
            """
            UPDATE runs
            SET status = 'expired', updated_at = ?, completed_at = ?,
                lease_expires_at = NULL, failure_class = 'lease_expired'
            WHERE status = 'active'
              AND backend_agent_pid IS NULL
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at <= ?
            """,
            (_iso(now), _iso(now), _iso(now)),
        )
        for index, row in enumerate(expired):
            self._append_attempt_event_best_effort_locked(
                run_id=str(row["run_id"]),
                event_type="run_completed",
                payload={
                    "status": "expired",
                    "failure_class": "lease_expired",
                },
                now=now,
                prune_terminal=index == len(expired) - 1,
            )
        return cur.rowcount

