"""SQLite connection and lease-lookup base shared by run registry parts."""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from .migrations import LATEST_SCHEMA_VERSION, apply_migrations, current_schema_version
from .run_registry_records import (
    DEFAULT_LEASE_TTL,
    SQLITE_BUSY_TIMEOUT_S,
    _iso,
)


class _RunRegistryBase:
    """Connection, schema, and live-lease lookups for `RunRegistry`."""

    def __init__(
        self,
        path: Path,
        lease_ttl: timedelta = DEFAULT_LEASE_TTL,
        *,
        owner_pid: int | None = None,
        boot_id: str | None = None,
    ) -> None:
        self._path = path
        self._lease_ttl = lease_ttl
        self._conn: sqlite3.Connection | None = None
        # Owner identity lets a restarted process distinguish "a dead
        # process's leftover lease" (reclaim now) from "a live peer's lease"
        # (honor until TTL). The boot id is unique per registry instance so
        # our own live leases are never self-reclaimed.
        self._owner_pid = owner_pid if owner_pid is not None else os.getpid()
        self._boot_id = boot_id or uuid.uuid4().hex
        self._applied_migrations: list[int] = []
        self._ensure_schema()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def applied_migrations(self) -> tuple[int, ...]:
        """Schema versions this instance applied at open time (doctor uses it)."""
        return tuple(self._applied_migrations)

    def schema_version(self) -> int:
        return current_schema_version(self._connect())

    def schema_is_current(self) -> bool:
        return self.schema_version() >= LATEST_SCHEMA_VERSION

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                self._path, timeout=SQLITE_BUSY_TIMEOUT_S, isolation_level=None
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def _ensure_schema(self) -> None:
        """Bring the database up to the latest schema version.

        The DDL itself lives in `migrations.py`; this stays a one-liner so
        there is exactly one place that decides what the schema looks like.
        """
        self._applied_migrations = apply_migrations(self._connect(), self._path)

    def _run_is_live_locked(self, run_id: str, now: datetime) -> bool:
        row = self._connect().execute(
            "SELECT status, lease_expires_at FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return False
        return str(row["status"]) == "reclaiming" or (
            str(row["status"]) == "active"
            and row["lease_expires_at"] is not None
            and str(row["lease_expires_at"]) > _iso(now)
        )

    def _active_issue_locked(
        self, issue_id: str, now: datetime
    ) -> sqlite3.Row | None:
        return self._connect().execute(
            """
            SELECT * FROM runs
            WHERE issue_id = ?
              AND (
                  status = 'reclaiming'
                  OR (status = 'active' AND lease_expires_at > ?)
              )
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (issue_id, _iso(now)),
        ).fetchone()

    def _active_identifier_locked(
        self, identifier: str, now: datetime
    ) -> sqlite3.Row | None:
        return self._connect().execute(
            """
            SELECT * FROM runs
            WHERE identifier = ?
              AND (
                  status = 'reclaiming'
                  OR (status = 'active' AND lease_expires_at > ?)
              )
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (identifier, _iso(now)),
        ).fetchone()

    def _expire_stale_locked(self, now: datetime) -> int:
        """Expire lapsed leases; implemented by `RunRegistry`."""
        raise NotImplementedError
