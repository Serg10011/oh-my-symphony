"""Records, constants, and row/value helpers for the run registry."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, overload

from .diagnostics import (
    MAX_DIAGNOSTIC_COUNTER,
)


DEFAULT_LEASE_TTL = timedelta(minutes=5)

# Bound how long a locked database can stall a caller. Registry ops run
# inline on the event loop (sqlite connections are thread-affine), so this
# is the worst-case tick delay a contended WAL database can inflict.
SQLITE_BUSY_TIMEOUT_S = 5.0

# Checkpoints contain only the opaque backend session handle and the completed
# workflow boundary needed for recovery. Reject rather than truncate: a
# truncated session id could resume the wrong conversation or silently fall
# back to a fresh one.
MAX_RESUME_SESSION_ID_BYTES = 4_096
MAX_RESUME_SESSION_ID_CHARS = 512
MAX_CHECKPOINT_STATE_BYTES = 256


@dataclass(frozen=True)
class ContinuationCheckpoint:
    """Bounded private value handed only to the backend continuation path."""

    resume_session_id: str
    state: str
    turn: int
    checkpointed_at: datetime

    def __post_init__(self) -> None:
        _validate_checkpoint_fields(
            resume_session_id=self.resume_session_id,
            state=self.state,
            turn=self.turn,
        )
        if not isinstance(self.checkpointed_at, datetime):
            raise ValueError("checkpointed_at must be a datetime")
        object.__setattr__(self, "checkpointed_at", _utc(self.checkpointed_at))


@dataclass(frozen=True)
class ContinuationAcquisition:
    """A new run lease plus the exact predecessor boundary it consumed."""

    run_id: str
    continued_from_run_id: str
    checkpoint: ContinuationCheckpoint


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    issue_id: str
    identifier: str
    title: str
    state: str
    status: str
    workspace_path: Path
    lease_expires_at: datetime | None
    last_progress_at: datetime | None
    attempt: int | None = None
    attempt_kind: str = ""
    agent_kind: str = ""
    started_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None
    owner_pid: int | None = None
    owner_boot_id: str | None = None
    backend_agent_pid: int | None = None
    backend_process_identity: str | None = None
    input_tokens: int | None = None
    cache_input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    failure_class: str | None = None
    failure_message: str | None = None
    branch_name: str | None = None
    commit_sha: str | None = None
    continued_from_run_id: str | None = None
    checkpoint_state: str | None = None
    checkpoint_turn: int | None = None
    checkpointed_at: datetime | None = None


@dataclass(frozen=True)
class IssueFlags:
    issue_id: str
    retry_attempt: int | None
    budget_exhausted: bool
    paused: bool
    pause_reason: str | None
    updated_at: datetime


@dataclass(frozen=True)
class ReleaseGate:
    """Host-owned pending/approved identity for one release finalizer."""

    finalizer_identifier: str
    verifier_issue_id: str
    verifier_identifier: str
    expected_contract_sha256: str
    cycle_fingerprint: str
    approved_fingerprint: str | None
    status: str
    target_branch: str | None
    approved_target_sha: str | None
    verifier_run_id: str | None
    updated_at: datetime
    finalizer_run_id: str | None = None
    generation: str = ""
    finalizer_completed_at: datetime | None = None
    finalizer_completion_token: str | None = None


@dataclass(frozen=True)
class ReleaseEvidenceIdentity:
    """Append-only host identity for an evidence-only release ticket."""

    issue_id: str
    identifier: str
    finalizer_identifier: str
    role: str
    cycle_generation: str
    retired: bool
    recorded_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ReleaseCycleItem:
    """Host-owned identity for one repair or fresh verifier board ticket."""

    finalizer_identifier: str
    cycle_fingerprint: str
    item_role: str
    item_key: str
    issue_id: str
    identifier: str
    recorded_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class PmWorkspaceRecord:
    project_id: str
    change_id: str
    workspace_path: Path
    base_git_sha: str
    owner: str
    fence: int
    state: str
    observed_hashes: dict[str, str]
    updated_at: datetime


@dataclass(frozen=True)
class PmManagedTicketRecord:
    project_id: str
    ticket_identifier: str
    change_id: str
    task_id: str
    task_revision: str
    handoff_id: str
    state: str
    updated_at: datetime


@dataclass(frozen=True)
class PmDispatchPermitRecord:
    permit_id: str
    project_id: str
    ticket_identifier: str
    handoff_id: str
    task_revision: str
    issue_hash: str
    scope_hash: str
    fence: int
    state: str
    process_pid: int | None
    run_id: str | None
    lease_expires_at: datetime
    updated_at: datetime


def registry_path_for_workflow(workflow_path: str | Path) -> Path:
    return Path(workflow_path).expanduser().resolve().parent / ".symphony" / "state.db"


def clamp_run_history_limit(limit: int) -> int:
    return max(1, min(int(limit), 200))


def _pid_alive_win32(pid: int) -> bool:
    """Windows liveness probe: OpenProcess + GetExitCodeProcess.

    ``os.kill(pid, 0)`` cannot answer this question on Windows — every
    outcome lands in the generic ``OSError`` bucket, which the POSIX
    mapping below reads as "alive". Probe the process object directly:

    - ``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`` fails with
      ``ERROR_INVALID_PARAMETER`` (87) for a pid the kernel no longer
      knows → dead. ``ERROR_ACCESS_DENIED`` (5) means the process
      exists but belongs to another context → conservatively alive
      (the safe direction for lease honoring is to wait out the TTL).
    - ``GetExitCodeProcess == STILL_ACTIVE (259)`` → alive; any other
      exit code means the process has terminated and only an open
      handle keeps the pid resolvable → dead.

    The theoretical "process legitimately exited with code 259" case
    reads as alive; the lease then lapses via TTL — acceptable for a
    best-effort liveness probe. Handles are always closed; the access
    right is query-only, so no privilege escalation is involved.
    """
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_ACCESS_DENIED = 5

    # These attributes are only defined by ctypes on Windows. Resolve them
    # dynamically so Linux Pyright can analyze this guarded helper; if an
    # unusual Windows runtime lacks either API, conservatively honor the lease.
    win_dll = getattr(ctypes, "WinDLL", None)
    get_last_error = getattr(ctypes, "get_last_error", None)
    if win_dll is None or get_last_error is None:
        return True
    kernel32 = win_dll("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, ctypes.c_ulong(pid)
    )
    if not handle:
        return get_last_error() == ERROR_ACCESS_DENIED
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            # Cannot prove dead — honor the lease until TTL.
            return True
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        return _pid_alive_win32(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # PermissionError and friends: the pid exists but isn't ours.
        # Unknown errors default to "alive" — the safe direction is to
        # honor the lease until its TTL rather than double-dispatch.
        return True
    return True


def _validate_checkpoint_fields(
    *, resume_session_id: str, state: str, turn: int
) -> None:
    if not isinstance(resume_session_id, str) or not resume_session_id.strip():
        raise ValueError("resume_session_id must be a non-empty string")
    if (
        len(resume_session_id) > MAX_RESUME_SESSION_ID_CHARS
        or len(resume_session_id.encode("utf-8")) > MAX_RESUME_SESSION_ID_BYTES
    ):
        raise ValueError("resume_session_id exceeds the private checkpoint bound")
    if not all(char.isprintable() for char in resume_session_id):
        raise ValueError("resume_session_id contains control characters")
    if not isinstance(state, str) or not state.strip():
        raise ValueError("checkpoint state must be a non-empty string")
    if len(state.encode("utf-8")) > MAX_CHECKPOINT_STATE_BYTES:
        raise ValueError("checkpoint state exceeds the checkpoint bound")
    if isinstance(turn, bool) or not isinstance(turn, int):
        raise ValueError("checkpoint turn must be an integer")
    if turn < 1 or turn > MAX_DIAGNOSTIC_COUNTER:
        raise ValueError("checkpoint turn is outside the supported range")


def _optional_nonnegative(value: int | None, fallback: Any = None) -> int | None:
    if value is None and fallback is None:
        return None
    return _nonnegative(value, fallback)


def _nonnegative(value: int | None, fallback: Any = 0) -> int:
    candidate = fallback if value is None else value
    try:
        return min(max(int(candidate or 0), 0), MAX_DIAGNOSTIC_COUNTER)
    except (TypeError, ValueError):
        return 0


def _valid_sha(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = str(value).strip().lower()
    if 4 <= len(candidate) <= 64 and all(char in "0123456789abcdef" for char in candidate):
        return candidate
    return None


def _like_pattern(value: str) -> str:
    escaped = value.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _run_summary(record: RunRecord) -> dict[str, Any]:
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
        "started_at": _iso(record.started_at),
        "updated_at": _iso(record.updated_at),
        "completed_at": _iso(record.completed_at),
        "workspace_path": str(record.workspace_path),
        "branch_name": record.branch_name or f"symphony/{record.identifier}",
        "commit_sha": record.commit_sha,
        "continued_from_run_id": record.continued_from_run_id,
        "checkpoint": (
            {
                "state": record.checkpoint_state,
                "turn": record.checkpoint_turn,
                "checkpointed_at": _iso(record.checkpointed_at),
            }
            if record.checkpoint_state is not None
            and record.checkpoint_turn is not None
            and record.checkpointed_at is not None
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


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@overload
def _iso(value: datetime) -> str: ...


@overload
def _iso(value: None) -> None: ...


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _utc(value).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _record(row: sqlite3.Row) -> RunRecord:
    owner_pid = row["owner_pid"]
    backend_agent_pid = row["backend_agent_pid"]
    return RunRecord(
        run_id=str(row["run_id"]),
        issue_id=str(row["issue_id"]),
        identifier=str(row["identifier"]),
        title=str(row["title"]),
        state=str(row["state"]),
        status=str(row["status"]),
        workspace_path=Path(str(row["workspace_path"])),
        lease_expires_at=_parse(row["lease_expires_at"]),
        last_progress_at=_parse(row["last_progress_at"]),
        attempt=int(row["attempt"]) if row["attempt"] is not None else None,
        attempt_kind=str(row["attempt_kind"]),
        agent_kind=str(row["agent_kind"]),
        started_at=_parse(row["started_at"]),
        updated_at=_parse(row["updated_at"]),
        completed_at=_parse(row["completed_at"]),
        owner_pid=int(owner_pid) if owner_pid is not None else None,
        owner_boot_id=row["owner_boot_id"],
        backend_agent_pid=(
            int(backend_agent_pid) if backend_agent_pid is not None else None
        ),
        backend_process_identity=(
            str(row["backend_process_identity"])
            if row["backend_process_identity"]
            else None
        ),
        input_tokens=(int(row["input_tokens"]) if row["input_tokens"] is not None else None),
        cache_input_tokens=(int(row["cache_input_tokens"]) if row["cache_input_tokens"] is not None else None),
        output_tokens=(int(row["output_tokens"]) if row["output_tokens"] is not None else None),
        total_tokens=(int(row["total_tokens"]) if row["total_tokens"] is not None else None),
        failure_class=str(row["failure_class"]) if row["failure_class"] else None,
        failure_message=str(row["failure_message"]) if row["failure_message"] else None,
        branch_name=str(row["branch_name"]) if row["branch_name"] else None,
        commit_sha=str(row["commit_sha"]) if row["commit_sha"] else None,
        continued_from_run_id=(
            str(row["continued_from_run_id"])
            if row["continued_from_run_id"]
            else None
        ),
        checkpoint_state=(
            str(row["checkpoint_state"]) if row["checkpoint_state"] else None
        ),
        checkpoint_turn=(
            int(row["checkpoint_turn"])
            if row["checkpoint_turn"] is not None
            else None
        ),
        checkpointed_at=_parse(row["checkpointed_at"]),
    )


def _issue_flags(row: sqlite3.Row) -> IssueFlags:
    return IssueFlags(
        issue_id=str(row["issue_id"]),
        retry_attempt=(
            int(row["retry_attempt"]) if row["retry_attempt"] is not None else None
        ),
        budget_exhausted=bool(row["budget_exhausted"]),
        paused=bool(row["paused"]),
        pause_reason=row["pause_reason"],
        updated_at=_parse(row["updated_at"]) or datetime.now(timezone.utc),
    )


def _release_gate(row: sqlite3.Row) -> ReleaseGate:
    return ReleaseGate(
        finalizer_identifier=str(row["finalizer_identifier"]),
        verifier_issue_id=str(row["verifier_issue_id"]),
        verifier_identifier=str(row["verifier_identifier"]),
        expected_contract_sha256=str(row["expected_contract_sha256"]),
        cycle_fingerprint=str(row["cycle_fingerprint"]),
        approved_fingerprint=(
            str(row["approved_fingerprint"])
            if row["approved_fingerprint"]
            else None
        ),
        status=str(row["status"]),
        target_branch=(str(row["target_branch"]) if row["target_branch"] else None),
        approved_target_sha=(
            str(row["approved_target_sha"])
            if row["approved_target_sha"]
            else None
        ),
        verifier_run_id=(
            str(row["verifier_run_id"]) if row["verifier_run_id"] else None
        ),
        updated_at=_parse(row["updated_at"]) or datetime.now(timezone.utc),
        finalizer_run_id=(
            str(row["finalizer_run_id"]) if row["finalizer_run_id"] else None
        ),
        generation=str(row["generation"] or ""),
        finalizer_completed_at=_parse(row["finalizer_completed_at"]),
        finalizer_completion_token=(
            str(row["finalizer_completion_token"])
            if row["finalizer_completion_token"]
            else None
        ),
    )


def _release_evidence_identity(row: sqlite3.Row) -> ReleaseEvidenceIdentity:
    return ReleaseEvidenceIdentity(
        issue_id=str(row["issue_id"]),
        identifier=str(row["identifier"]),
        finalizer_identifier=str(row["finalizer_identifier"]),
        role=str(row["role"]),
        cycle_generation=str(row["cycle_generation"]),
        retired=bool(row["retired"]),
        recorded_at=_parse(row["recorded_at"]) or datetime.now(timezone.utc),
        updated_at=_parse(row["updated_at"]) or datetime.now(timezone.utc),
    )


def _release_cycle_item(row: sqlite3.Row) -> ReleaseCycleItem:
    return ReleaseCycleItem(
        finalizer_identifier=str(row["finalizer_identifier"]),
        cycle_fingerprint=str(row["cycle_fingerprint"]),
        item_role=str(row["item_role"]),
        item_key=str(row["item_key"]),
        issue_id=str(row["issue_id"]),
        identifier=str(row["identifier"]),
        recorded_at=_parse(row["recorded_at"]) or datetime.now(timezone.utc),
        updated_at=_parse(row["updated_at"]) or datetime.now(timezone.utc),
    )


def _pm_workspace(row: sqlite3.Row) -> PmWorkspaceRecord:
    observed = json.loads(str(row["observed_hashes_json"] or "{}"))
    return PmWorkspaceRecord(
        project_id=str(row["project_id"]),
        change_id=str(row["change_id"]),
        workspace_path=Path(str(row["workspace_path"])),
        base_git_sha=str(row["base_git_sha"]),
        owner=str(row["owner"]),
        fence=int(row["fence"]),
        state=str(row["state"]),
        observed_hashes={
            str(key): str(value)
            for key, value in observed.items()
            if isinstance(key, str) and isinstance(value, str)
        },
        updated_at=_parse(str(row["updated_at"])) or datetime.now(timezone.utc),
    )


def _pm_managed_ticket(row: sqlite3.Row) -> PmManagedTicketRecord:
    return PmManagedTicketRecord(
        project_id=str(row["project_id"]),
        ticket_identifier=str(row["ticket_identifier"]),
        change_id=str(row["change_id"]),
        task_id=str(row["task_id"]),
        task_revision=str(row["task_revision"]),
        handoff_id=str(row["handoff_id"]),
        state=str(row["state"]),
        updated_at=_parse(str(row["updated_at"])) or datetime.now(timezone.utc),
    )


def _pm_receipt_matches_policy(
    receipt: Mapping[str, Any], policy: Any
) -> bool:
    if (
        str(receipt.get("result")) != "passed"
        or bool(receipt.get("timed_out"))
        or bool(receipt.get("cancelled"))
    ):
        return False
    if isinstance(policy, str):
        return receipt.get("return_code") == 0 and str(receipt.get("command_digest")) == policy
    if isinstance(policy, dict) and policy.get("kind") == "native":
        return (
            receipt.get("return_code") == 0
            and str(receipt.get("definition_digest") or receipt.get("command_digest"))
            == str(policy.get("definition_digest"))
        )
    if isinstance(policy, dict) and policy.get("kind") == "manual":
        return (
            receipt.get("return_code") is None
            and str(receipt.get("procedure_kind")) == "manual"
            and str(receipt.get("definition_digest")) == str(policy.get("definition_digest"))
            and bool(receipt.get("report_ref_json"))
        )
    return False


def _pm_dispatch_permit(row: sqlite3.Row) -> PmDispatchPermitRecord:
    return PmDispatchPermitRecord(
        permit_id=str(row["permit_id"]),
        project_id=str(row["project_id"]),
        ticket_identifier=str(row["ticket_identifier"]),
        handoff_id=str(row["handoff_id"]),
        task_revision=str(row["task_revision"]),
        issue_hash=str(row["issue_hash"]),
        scope_hash=str(row["scope_hash"]),
        fence=int(row["fence"]),
        state=str(row["state"]),
        process_pid=_optional_nonnegative(row["process_pid"]),
        run_id=str(row["run_id"]) if row["run_id"] else None,
        lease_expires_at=_parse(str(row["lease_expires_at"]))
        or datetime.now(timezone.utc),
        updated_at=_parse(str(row["updated_at"])) or datetime.now(timezone.utc),
    )
