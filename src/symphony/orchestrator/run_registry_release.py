"""Release gate, release cycle, and release run authority for the run registry."""

from __future__ import annotations

import uuid
from datetime import datetime

from ..issue import Issue
from .run_registry_base import _RunRegistryBase
from .run_registry_records import (
    ReleaseGate,
    ReleaseEvidenceIdentity,
    ReleaseCycleItem,
    _utc,
    _iso,
    _release_gate,
    _release_evidence_identity,
    _release_cycle_item,
)


class _ReleaseRegistry(_RunRegistryBase):
    """Host-owned release gate and release cycle state."""

    def bind_release_verifier_run(
        self,
        *,
        gate: ReleaseGate,
        verifier_run_id: str,
        now: datetime | None = None,
    ) -> bool:
        """Bind one exact active run to the current pending verifier cycle."""
        if gate.status != "pending":
            return False
        now = _utc(now)
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._expire_stale_locked(now)
            active = self._active_issue_locked(gate.verifier_issue_id, now)
            if (
                active is None
                or str(active["run_id"]) != verifier_run_id
                or str(active["identifier"]) != gate.verifier_identifier
                or str(active["status"]) != "active"
            ):
                conn.execute("ROLLBACK")
                return False
            current = conn.execute(
                """
                SELECT verifier_run_id, updated_at FROM release_gates
                WHERE finalizer_identifier = ?
                  AND verifier_issue_id = ?
                  AND verifier_identifier = ?
                  AND expected_contract_sha256 = ?
                  AND cycle_fingerprint = ?
                  AND generation = ?
                  AND status = 'pending'
                """,
                (
                    gate.finalizer_identifier,
                    gate.verifier_issue_id,
                    gate.verifier_identifier,
                    gate.expected_contract_sha256,
                    gate.cycle_fingerprint,
                    gate.generation,
                ),
            ).fetchone()
            if current is None:
                conn.execute("ROLLBACK")
                return False
            if str(active["started_at"]) < str(current["updated_at"]):
                conn.execute("ROLLBACK")
                return False
            bound_run_id = current["verifier_run_id"]
            if bound_run_id and str(bound_run_id) != verifier_run_id:
                prior = conn.execute(
                    "SELECT status, lease_expires_at FROM runs WHERE run_id = ?",
                    (str(bound_run_id),),
                ).fetchone()
                if prior is not None and (
                    str(prior["status"]) == "reclaiming"
                    or (
                        str(prior["status"]) == "active"
                        and prior["lease_expires_at"] is not None
                        and str(prior["lease_expires_at"]) > _iso(now)
                    )
                ):
                    conn.execute("ROLLBACK")
                    return False
            cur = conn.execute(
                """
                UPDATE release_gates SET verifier_run_id = ?
                WHERE finalizer_identifier = ?
                  AND verifier_issue_id = ?
                  AND verifier_identifier = ?
                  AND expected_contract_sha256 = ?
                  AND cycle_fingerprint = ?
                  AND generation = ?
                  AND status = 'pending'
                """,
                (
                    verifier_run_id,
                    gate.finalizer_identifier,
                    gate.verifier_issue_id,
                    gate.verifier_identifier,
                    gate.expected_contract_sha256,
                    gate.cycle_fingerprint,
                    gate.generation,
                ),
            )
            conn.execute("COMMIT")
            return cur.rowcount == 1
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def bind_release_finalizer_run(
        self,
        *,
        gate: ReleaseGate,
        finalizer_issue_id: str,
        finalizer_run_id: str,
        now: datetime | None = None,
    ) -> bool:
        """Bind an exact active finalizer lease to the approved gate cycle."""
        if gate.status != "approved":
            return False
        now = _utc(now)
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._expire_stale_locked(now)
            active = self._active_issue_locked(finalizer_issue_id, now)
            if (
                active is None
                or str(active["run_id"]) != finalizer_run_id
                or str(active["identifier"]) != gate.finalizer_identifier
                or str(active["status"]) != "active"
            ):
                conn.execute("ROLLBACK")
                return False
            if self._active_issue_locked(gate.verifier_issue_id, now) is not None:
                conn.execute("ROLLBACK")
                return False
            current = conn.execute(
                """
                SELECT updated_at, finalizer_run_id, finalizer_completed_at
                FROM release_gates
                WHERE finalizer_identifier = ?
                  AND verifier_issue_id = ?
                  AND verifier_identifier = ?
                  AND expected_contract_sha256 = ?
                  AND cycle_fingerprint = ?
                  AND generation = ?
                  AND approved_fingerprint = ?
                  AND target_branch = ?
                  AND approved_target_sha = ?
                  AND verifier_run_id = ?
                  AND status = 'approved'
                """,
                (
                    gate.finalizer_identifier,
                    gate.verifier_issue_id,
                    gate.verifier_identifier,
                    gate.expected_contract_sha256,
                    gate.cycle_fingerprint,
                    gate.generation,
                    gate.approved_fingerprint,
                    gate.target_branch,
                    gate.approved_target_sha,
                    gate.verifier_run_id,
                ),
            ).fetchone()
            if current is None or str(active["started_at"]) < str(
                current["updated_at"]
            ):
                conn.execute("ROLLBACK")
                return False
            prior_finalizer_run_id = current["finalizer_run_id"]
            if (
                current["finalizer_completed_at"] is not None
                or (
                    prior_finalizer_run_id is not None
                    and str(prior_finalizer_run_id) != finalizer_run_id
                    and self._run_is_live_locked(
                        str(prior_finalizer_run_id), now
                    )
                )
            ):
                conn.execute("ROLLBACK")
                return False
            cur = conn.execute(
                """
                UPDATE release_gates SET finalizer_run_id = ?
                WHERE finalizer_identifier = ?
                  AND verifier_issue_id = ?
                  AND verifier_identifier = ?
                  AND expected_contract_sha256 = ?
                  AND cycle_fingerprint = ?
                  AND generation = ?
                  AND approved_fingerprint = ?
                  AND target_branch = ?
                  AND approved_target_sha = ?
                  AND verifier_run_id = ?
                  AND status = 'approved'
                  AND finalizer_completed_at IS NULL
                """,
                (
                    finalizer_run_id,
                    gate.finalizer_identifier,
                    gate.verifier_issue_id,
                    gate.verifier_identifier,
                    gate.expected_contract_sha256,
                    gate.cycle_fingerprint,
                    gate.generation,
                    gate.approved_fingerprint,
                    gate.target_branch,
                    gate.approved_target_sha,
                    gate.verifier_run_id,
                ),
            )
            conn.execute("COMMIT")
            return cur.rowcount == 1
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def release_finalizer_run_is_authorized(
        self,
        *,
        gate: ReleaseGate,
        finalizer_issue_id: str,
        allow_active: bool = True,
        now: datetime | None = None,
    ) -> bool:
        """Check current gate and exact bound run in one read transaction."""
        if gate.status != "approved" or not gate.finalizer_run_id:
            return False
        now = _utc(now)
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._expire_stale_locked(now)
            row = conn.execute(
                """
                SELECT 1
                FROM release_gates AS gate
                JOIN runs AS run ON run.run_id = gate.finalizer_run_id
                WHERE gate.finalizer_identifier = ?
                  AND gate.verifier_issue_id = ?
                  AND gate.verifier_identifier = ?
                  AND gate.expected_contract_sha256 = ?
                  AND gate.cycle_fingerprint = ?
                  AND gate.generation = ?
                  AND gate.approved_fingerprint = ?
                  AND gate.target_branch = ?
                  AND gate.approved_target_sha = ?
                  AND gate.verifier_run_id = ?
                  AND gate.finalizer_run_id = ?
                  AND gate.status = 'approved'
                  AND (
                    (gate.finalizer_completed_at IS NULL AND ? IS NULL)
                    OR gate.finalizer_completed_at = ?
                  )
                  AND (
                    (gate.finalizer_completion_token IS NULL AND ? IS NULL)
                    OR gate.finalizer_completion_token = ?
                  )
                  AND run.issue_id = ?
                  AND run.identifier = gate.finalizer_identifier
                  AND (
                    (? = 1 AND run.status = 'active' AND run.lease_expires_at > ?)
                    OR (
                        run.status = 'normal'
                        AND run.completed_at IS NOT NULL
                        AND gate.finalizer_completed_at IS NOT NULL
                        AND gate.finalizer_completion_token IS NOT NULL
                    )
                  )
                LIMIT 1
                """,
                (
                    gate.finalizer_identifier,
                    gate.verifier_issue_id,
                    gate.verifier_identifier,
                    gate.expected_contract_sha256,
                    gate.cycle_fingerprint,
                    gate.generation,
                    gate.approved_fingerprint,
                    gate.target_branch,
                    gate.approved_target_sha,
                    gate.verifier_run_id,
                    gate.finalizer_run_id,
                    (
                        _iso(gate.finalizer_completed_at)
                        if gate.finalizer_completed_at is not None
                        else None
                    ),
                    (
                        _iso(gate.finalizer_completed_at)
                        if gate.finalizer_completed_at is not None
                        else None
                    ),
                    gate.finalizer_completion_token,
                    gate.finalizer_completion_token,
                    finalizer_issue_id,
                    1 if allow_active else 0,
                    _iso(now),
                ),
            ).fetchone()
            conn.execute("COMMIT")
            return row is not None
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def release_verifier_run_is_authorized(
        self,
        *,
        gate: ReleaseGate,
        verifier_issue_id: str,
        now: datetime | None = None,
    ) -> bool:
        """Check the exact current gate generation and active verifier run."""
        if not gate.verifier_run_id:
            return False
        now = _utc(now)
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._expire_stale_locked(now)
            row = conn.execute(
                """
                SELECT 1
                FROM release_gates AS gate
                JOIN runs AS run ON run.run_id = gate.verifier_run_id
                WHERE gate.finalizer_identifier = ?
                  AND gate.verifier_issue_id = ?
                  AND gate.verifier_identifier = ?
                  AND gate.expected_contract_sha256 = ?
                  AND gate.cycle_fingerprint = ?
                  AND gate.generation = ?
                  AND gate.verifier_run_id = ?
                  AND gate.status IN ('pending', 'approved')
                  AND run.issue_id = ?
                  AND run.identifier = gate.verifier_identifier
                  AND run.status = 'active'
                  AND run.lease_expires_at > ?
                LIMIT 1
                """,
                (
                    gate.finalizer_identifier,
                    gate.verifier_issue_id,
                    gate.verifier_identifier,
                    gate.expected_contract_sha256,
                    gate.cycle_fingerprint,
                    gate.generation,
                    gate.verifier_run_id,
                    verifier_issue_id,
                    _iso(now),
                ),
            ).fetchone()
            conn.execute("COMMIT")
            return row is not None
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def mark_release_finalizer_completed(
        self,
        *,
        gate: ReleaseGate,
        finalizer_issue_id: str,
        completion_token: str,
        now: datetime | None = None,
    ) -> bool:
        """Persist terminal delivery proof for the exact active finalizer run."""
        if (
            gate.status != "approved"
            or not gate.finalizer_run_id
            or not completion_token.strip()
        ):
            return False
        now = _utc(now)
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._expire_stale_locked(now)
            active = self._active_issue_locked(finalizer_issue_id, now)
            if (
                active is None
                or str(active["run_id"]) != gate.finalizer_run_id
                or str(active["identifier"]) != gate.finalizer_identifier
                or str(active["status"]) != "active"
                or self._active_issue_locked(gate.verifier_issue_id, now) is not None
            ):
                conn.execute("ROLLBACK")
                return False
            cur = conn.execute(
                """
                UPDATE release_gates
                SET finalizer_completed_at = COALESCE(finalizer_completed_at, ?),
                    finalizer_completion_token = COALESCE(
                        finalizer_completion_token, ?
                    )
                WHERE finalizer_identifier = ?
                  AND verifier_issue_id = ?
                  AND verifier_identifier = ?
                  AND expected_contract_sha256 = ?
                  AND cycle_fingerprint = ?
                  AND generation = ?
                  AND approved_fingerprint = ?
                  AND target_branch = ?
                  AND approved_target_sha = ?
                  AND verifier_run_id = ?
                  AND finalizer_run_id = ?
                  AND status = 'approved'
                """,
                (
                    _iso(now),
                    completion_token,
                    gate.finalizer_identifier,
                    gate.verifier_issue_id,
                    gate.verifier_identifier,
                    gate.expected_contract_sha256,
                    gate.cycle_fingerprint,
                    gate.generation,
                    gate.approved_fingerprint,
                    gate.target_branch,
                    gate.approved_target_sha,
                    gate.verifier_run_id,
                    gate.finalizer_run_id,
                ),
            )
            conn.execute("COMMIT")
            return cur.rowcount == 1
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def replace_pending_release_gate(
        self,
        gate: ReleaseGate,
        *,
        now: datetime | None = None,
        invalidating_finalizer_run_id: str | None = None,
    ) -> ReleaseGate:
        """Atomically invalidate approval and create a new cycle generation.

        A live finalizer normally fences replacement.  The one exception is
        the exact finalizer run that discovered its own approval became stale;
        it may invalidate that cycle before releasing its lease.  The caller
        must provide that bound run id, which is checked under the same write
        transaction as the replacement.
        """
        if gate.status != "pending":
            raise ValueError("replacement release gate must have pending status")
        now = _utc(now)
        generation = uuid.uuid4().hex
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            prior = conn.execute(
                """
                SELECT verifier_issue_id, verifier_identifier, verifier_run_id,
                       generation, finalizer_run_id
                FROM release_gates WHERE finalizer_identifier = ?
                """,
                (gate.finalizer_identifier,),
            ).fetchone()
            if prior is not None:
                self._expire_stale_locked(now)
                prior_verifier_run_id = prior["verifier_run_id"]
                active_verifier = self._active_issue_locked(
                    str(prior["verifier_issue_id"]), now
                )
                if active_verifier is not None and (
                    prior_verifier_run_id is None
                    or str(active_verifier["run_id"])
                    != str(prior_verifier_run_id)
                ):
                    raise RuntimeError(
                        "release verifier cleanup or foreign lease must finish "
                        "before replacing its gate"
                    )
                prior_finalizer_run_id = prior["finalizer_run_id"]
                active_finalizer = self._active_identifier_locked(
                    gate.finalizer_identifier, now
                )
                if invalidating_finalizer_run_id is not None:
                    if (
                        prior_finalizer_run_id is None
                        or str(prior_finalizer_run_id)
                        != invalidating_finalizer_run_id
                        or active_finalizer is None
                        or str(active_finalizer["run_id"])
                        != invalidating_finalizer_run_id
                    ):
                        raise RuntimeError(
                            "only the exact active release finalizer may invalidate "
                            "its gate"
                        )
                elif active_finalizer is not None:
                    raise RuntimeError(
                        "active release finalizer must finish before replacing its gate"
                    )
                conn.execute(
                    """
                    INSERT INTO release_evidence_issues (
                        issue_id, identifier, finalizer_identifier, role,
                        cycle_generation, retired, recorded_at, updated_at
                    ) VALUES (?, ?, ?, 'verifier', ?, 1, ?, ?)
                    ON CONFLICT(issue_id) DO UPDATE SET
                        identifier = excluded.identifier,
                        finalizer_identifier = excluded.finalizer_identifier,
                        role = 'verifier',
                        cycle_generation = excluded.cycle_generation,
                        retired = 1,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(prior["verifier_issue_id"]),
                        str(prior["verifier_identifier"]),
                        gate.finalizer_identifier,
                        str(prior["generation"] or ""),
                        _iso(now),
                        _iso(now),
                    ),
                )
            conn.execute(
                """
                INSERT INTO release_gates (
                    finalizer_identifier, verifier_issue_id, verifier_identifier,
                    expected_contract_sha256, cycle_fingerprint,
                    approved_fingerprint, status, target_branch,
                    approved_target_sha, verifier_run_id, finalizer_run_id,
                    generation, finalizer_completed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, 'pending', NULL, NULL, NULL, NULL,
                          ?, NULL, ?)
                ON CONFLICT(finalizer_identifier) DO UPDATE SET
                    verifier_issue_id = excluded.verifier_issue_id,
                    verifier_identifier = excluded.verifier_identifier,
                    expected_contract_sha256 = excluded.expected_contract_sha256,
                    cycle_fingerprint = excluded.cycle_fingerprint,
                    approved_fingerprint = NULL,
                    status = 'pending',
                    target_branch = NULL,
                    approved_target_sha = NULL,
                    verifier_run_id = NULL,
                    finalizer_run_id = NULL,
                    generation = excluded.generation,
                    finalizer_completed_at = NULL,
                    finalizer_completion_token = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    gate.finalizer_identifier,
                    gate.verifier_issue_id,
                    gate.verifier_identifier,
                    gate.expected_contract_sha256,
                    gate.cycle_fingerprint,
                    generation,
                    _iso(now),
                ),
            )
            conn.execute(
                """
                INSERT INTO release_evidence_issues (
                    issue_id, identifier, finalizer_identifier, role,
                    cycle_generation, retired, recorded_at, updated_at
                ) VALUES (?, ?, ?, 'verifier', ?, 0, ?, ?)
                ON CONFLICT(issue_id) DO UPDATE SET
                    identifier = excluded.identifier,
                    finalizer_identifier = excluded.finalizer_identifier,
                    role = 'verifier',
                    cycle_generation = excluded.cycle_generation,
                    retired = 0,
                    updated_at = excluded.updated_at
                """,
                (
                    gate.verifier_issue_id,
                    gate.verifier_identifier,
                    gate.finalizer_identifier,
                    generation,
                    _iso(now),
                    _iso(now),
                ),
            )
            row = conn.execute(
                "SELECT * FROM release_gates WHERE finalizer_identifier = ?",
                (gate.finalizer_identifier,),
            ).fetchone()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        if row is None:
            raise RuntimeError("pending release gate disappeared after replacement")
        return _release_gate(row)

    def approve_release_gate(
        self,
        *,
        finalizer_identifier: str,
        verifier_issue_id: str,
        verifier_identifier: str,
        expected_contract_sha256: str,
        expected_cycle_fingerprint: str,
        expected_generation: str,
        approved_fingerprint: str,
        target_branch: str,
        target_sha: str,
        verifier_run_id: str,
        now: datetime | None = None,
    ) -> bool:
        """Approve the exact pending tuple and exact active run atomically."""
        now = _utc(now)
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._expire_stale_locked(now)
            active = self._active_issue_locked(verifier_issue_id, now)
            if (
                active is None
                or str(active["run_id"]) != verifier_run_id
                or str(active["identifier"]) != verifier_identifier
                or str(active["status"]) != "active"
            ):
                conn.execute("ROLLBACK")
                return False
            pending = conn.execute(
                """
                SELECT updated_at, verifier_run_id FROM release_gates
                WHERE finalizer_identifier = ?
                  AND verifier_issue_id = ?
                  AND verifier_identifier = ?
                  AND expected_contract_sha256 = ?
                  AND cycle_fingerprint = ?
                  AND generation = ?
                  AND status = 'pending'
                """,
                (
                    finalizer_identifier,
                    verifier_issue_id,
                    verifier_identifier,
                    expected_contract_sha256,
                    expected_cycle_fingerprint,
                    expected_generation,
                ),
            ).fetchone()
            if (
                pending is None
                or str(pending["verifier_run_id"] or "") != verifier_run_id
                or str(active["started_at"]) < str(pending["updated_at"])
            ):
                conn.execute("ROLLBACK")
                return False
            cur = conn.execute(
                """
                UPDATE release_gates
                SET status = 'approved', target_branch = ?, approved_target_sha = ?,
                    approved_fingerprint = ?, verifier_run_id = ?,
                    finalizer_run_id = NULL, updated_at = ?
                WHERE finalizer_identifier = ?
                  AND verifier_issue_id = ?
                  AND verifier_identifier = ?
                  AND expected_contract_sha256 = ?
                  AND cycle_fingerprint = ?
                  AND generation = ?
                  AND verifier_run_id = ?
                  AND status = 'pending'
                """,
                (
                    target_branch,
                    target_sha,
                    approved_fingerprint,
                    verifier_run_id,
                    _iso(now),
                    finalizer_identifier,
                    verifier_issue_id,
                    verifier_identifier,
                    expected_contract_sha256,
                    expected_cycle_fingerprint,
                    expected_generation,
                    verifier_run_id,
                ),
            )
            conn.execute("COMMIT")
            return cur.rowcount == 1
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def get_release_gate(
        self, finalizer_identifier: str
    ) -> ReleaseGate | None:
        row = self._connect().execute(
            "SELECT * FROM release_gates WHERE finalizer_identifier = ?",
            (finalizer_identifier,),
        ).fetchone()
        return _release_gate(row) if row is not None else None

    def get_release_gate_for_verifier(
        self, verifier_identifier: str
    ) -> ReleaseGate | None:
        row = self._connect().execute(
            "SELECT * FROM release_gates WHERE verifier_identifier = ?",
            (verifier_identifier,),
        ).fetchone()
        return _release_gate(row) if row is not None else None

    def get_release_evidence_identity(
        self, identifier: str
    ) -> ReleaseEvidenceIdentity | None:
        row = self._connect().execute(
            "SELECT * FROM release_evidence_issues WHERE identifier = ?",
            (identifier,),
        ).fetchone()
        return _release_evidence_identity(row) if row is not None else None

    def get_release_evidence_identity_by_issue_id(
        self, issue_id: str
    ) -> ReleaseEvidenceIdentity | None:
        """Return one evidence identity by its stable tracker issue id."""
        row = self._connect().execute(
            "SELECT * FROM release_evidence_issues WHERE issue_id = ?",
            (issue_id,),
        ).fetchone()
        return _release_evidence_identity(row) if row is not None else None

    def get_release_cycle_item(
        self,
        *,
        finalizer_identifier: str,
        cycle_fingerprint: str,
        item_role: str,
        item_key: str,
    ) -> ReleaseCycleItem | None:
        """Return the host-owned ticket identity for one lifecycle item."""
        row = self._connect().execute(
            """
            SELECT * FROM release_cycle_items
            WHERE finalizer_identifier = ?
              AND cycle_fingerprint = ?
              AND item_role = ?
              AND item_key = ?
            """,
            (
                finalizer_identifier,
                cycle_fingerprint,
                item_role,
                item_key,
            ),
        ).fetchone()
        return _release_cycle_item(row) if row is not None else None

    def record_release_cycle_item(
        self,
        *,
        finalizer_identifier: str,
        cycle_fingerprint: str,
        item_role: str,
        item_key: str,
        issue: Issue,
        now: datetime | None = None,
    ) -> ReleaseCycleItem:
        """Bind one logical release-cycle item to exactly one board ticket.

        The mapping is immutable. A retry may record the same tuple again,
        but it may never redirect a repair/verifier key to a different ticket
        or reuse one ticket for a different logical item.
        """
        return self._record_release_cycle_item_identity(
            finalizer_identifier=finalizer_identifier,
            cycle_fingerprint=cycle_fingerprint,
            item_role=item_role,
            item_key=item_key,
            issue_id=issue.id,
            identifier=issue.identifier,
            now=now,
        )

    def reserve_release_cycle_item(
        self,
        *,
        finalizer_identifier: str,
        cycle_fingerprint: str,
        item_role: str,
        item_key: str,
        identifier: str,
        now: datetime | None = None,
    ) -> ReleaseCycleItem:
        """Durably reserve a deterministic local-board ticket before create.

        File-board creation and SQLite cannot share one transaction.  By
        writing the exact host-derived identifier first, a crash after the
        board write can be resumed without trusting worker-editable labels or
        allocating a duplicate ticket.  File-board issue ids equal their
        identifiers, so the final readback can only confirm this same tuple.
        """
        return self._record_release_cycle_item_identity(
            finalizer_identifier=finalizer_identifier,
            cycle_fingerprint=cycle_fingerprint,
            item_role=item_role,
            item_key=item_key,
            issue_id=identifier,
            identifier=identifier,
            now=now,
        )

    def _record_release_cycle_item_identity(
        self,
        *,
        finalizer_identifier: str,
        cycle_fingerprint: str,
        item_role: str,
        item_key: str,
        issue_id: str,
        identifier: str,
        now: datetime | None,
    ) -> ReleaseCycleItem:
        if item_role not in {"repair", "verifier"}:
            raise ValueError("release cycle item role must be repair or verifier")
        if not all(
            value.strip()
            for value in (
                finalizer_identifier,
                cycle_fingerprint,
                item_key,
                issue_id,
                identifier,
            )
        ):
            raise ValueError("release cycle item identity fields must be non-empty")
        now = _utc(now)
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                """
                SELECT * FROM release_cycle_items
                WHERE finalizer_identifier = ?
                  AND cycle_fingerprint = ?
                  AND item_role = ?
                  AND item_key = ?
                """,
                (
                    finalizer_identifier,
                    cycle_fingerprint,
                    item_role,
                    item_key,
                ),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["issue_id"]) != issue_id
                    or str(existing["identifier"]) != identifier
                ):
                    raise RuntimeError(
                        "release cycle item is already bound to a different ticket"
                    )
                conn.execute(
                    """
                    UPDATE release_cycle_items SET updated_at = ?
                    WHERE finalizer_identifier = ?
                      AND cycle_fingerprint = ?
                      AND item_role = ?
                      AND item_key = ?
                    """,
                    (
                        _iso(now),
                        finalizer_identifier,
                        cycle_fingerprint,
                        item_role,
                        item_key,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO release_cycle_items (
                        finalizer_identifier, cycle_fingerprint, item_role,
                        item_key, issue_id, identifier, recorded_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        finalizer_identifier,
                        cycle_fingerprint,
                        item_role,
                        item_key,
                        issue_id,
                        identifier,
                        _iso(now),
                        _iso(now),
                    ),
                )
            row = conn.execute(
                """
                SELECT * FROM release_cycle_items
                WHERE finalizer_identifier = ?
                  AND cycle_fingerprint = ?
                  AND item_role = ?
                  AND item_key = ?
                """,
                (
                    finalizer_identifier,
                    cycle_fingerprint,
                    item_role,
                    item_key,
                ),
            ).fetchone()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        if row is None:
            raise RuntimeError("release cycle item disappeared after recording")
        return _release_cycle_item(row)

    def has_release_authority(self) -> bool:
        row = self._connect().execute(
            """
            SELECT 1 FROM release_gates
            UNION ALL
            SELECT 1 FROM release_evidence_issues
            UNION ALL
            SELECT 1 FROM release_cycle_items
            LIMIT 1
            """
        ).fetchone()
        return row is not None

    def has_unresolved_backend_ownership(self, identifier: str) -> bool:
        """Return whether any run still owns a recorded backend process."""
        row = self._connect().execute(
            """
            SELECT 1 FROM runs
            WHERE identifier = ? AND backend_agent_pid IS NOT NULL
            LIMIT 1
            """,
            (identifier,),
        ).fetchone()
        return row is not None

    def invalidate_release_gate(self, finalizer_identifier: str) -> bool:
        cur = self._connect().execute(
            "DELETE FROM release_gates WHERE finalizer_identifier = ?",
            (finalizer_identifier,),
        )
        return cur.rowcount > 0
