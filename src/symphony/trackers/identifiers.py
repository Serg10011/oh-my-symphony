"""Durable board ID reservations; callers must hold the board allocator lock."""

from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import tempfile
from dataclasses import asdict
from pathlib import Path

from ..errors import SymphonyError
from ..issue import PlanningRef, parse_iso_timestamp
from .validate import IDENTIFIER_RE, validate_identifier

LEDGER_NAME = ".symphony-identifiers.json"
_NUMBERED_ID = re.compile(r"^(.+)-(\d+)$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


def creation_fingerprint(front: dict, body: str) -> str:
    authored = {key: value for key, value in front.items()
                if key not in ('created_at', 'updated_at') and value is not None}
    payload = json.dumps({'front': authored, 'body': body.strip()}, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode()).hexdigest()


def validate_creation_key(key: str) -> None:
    if not isinstance(key, str) or not _DIGEST.fullmatch(key):
        raise SymphonyError('creation key must be a 64-character lowercase hex digest')


def _exists(path: Path) -> bool:
    # Path.exists() can hide permission/I/O errors; those must stop issuance.
    try:
        path.stat()
    except FileNotFoundError:
        return False
    return True


def _planning_ref(value: object) -> PlanningRef | None:
    fields = {"change_id", "task_id", "task_revision", "handoff_id", "snapshot_id"}
    if not isinstance(value, dict) or set(value) != fields:
        return None
    if not all(isinstance(item, str) and item.strip() for item in value.values()):
        return None
    return PlanningRef(**value)


def _key(identifier: str) -> str:
    key = identifier.casefold()
    match = _NUMBERED_ID.fullmatch(key)
    if match:
        return f"{match[1]}-{int(match[2])}"
    return key


class IdentifierLedger:
    """Remember issued IDs even when their Markdown files have disappeared."""

    def __init__(self, root: Path) -> None:
        self.path = root / LEDGER_NAME
        self.issued: set[str] = set()
        self.high_water: dict[str, int] = {}
        self.planning_refs: dict[str, PlanningRef] = {}
        self.creations: dict[str, dict[str, str]] = {}
        self.migrating = False
        try:
            self.migrating = not _exists(self.path)
            if self.path.is_symlink():
                raise ValueError("identifier ledger must not be a symlink")
            if self.migrating:
                return
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
                raise ValueError("unsupported identifier ledger version")
            issued, counters = data.get("issued"), data.get("high_water")
            if not isinstance(issued, list) or not isinstance(counters, dict):
                raise ValueError("invalid identifier ledger")
            for identifier in issued:
                if not isinstance(identifier, str) or not IDENTIFIER_RE.fullmatch(identifier):
                    raise ValueError("invalid reserved identifier")
                self.observe(identifier)
            for prefix, number in counters.items():
                if not isinstance(prefix, str) or not IDENTIFIER_RE.fullmatch(prefix):
                    raise ValueError("invalid identifier prefix")
                if type(number) is not int or number < 0:
                    raise ValueError("invalid identifier counter")
                self.high_water[prefix.casefold()] = max(
                    self.high_water.get(prefix.casefold(), 0), number
                )
            references = data.get("planning_refs", {})
            if not isinstance(references, dict):
                raise ValueError("invalid planning identities")
            for identifier, reference in references.items():
                if not isinstance(identifier, str) or not IDENTIFIER_RE.fullmatch(identifier) or _key(identifier) not in self.issued:
                    raise ValueError("planning identity without an issued ID")
                ref = _planning_ref(reference)
                if ref is None:
                    raise ValueError("invalid planning identity")
                self.planning_refs[_key(identifier)] = ref
            creations = data.get('creations', {})
            if not isinstance(creations, dict):
                raise ValueError('invalid creation receipts')
            for identifier, receipt in creations.items():
                if not isinstance(identifier, str) or not IDENTIFIER_RE.fullmatch(identifier) or _key(identifier) not in self.issued:
                    raise ValueError('creation receipt without an issued ID')
                if not isinstance(receipt, dict) or set(receipt) != {'key', 'input_hash', 'created_at', 'identifier'}:
                    raise ValueError('invalid creation receipt')
                if not all(isinstance(value, str) and value for value in receipt.values()):
                    raise ValueError('invalid creation receipt fields')
                if not _DIGEST.fullmatch(receipt['key']) or not _DIGEST.fullmatch(receipt['input_hash']):
                    raise ValueError('invalid creation digest')
                if not IDENTIFIER_RE.fullmatch(receipt['identifier']) or _key(receipt['identifier']) != _key(identifier):
                    raise ValueError('creation identity mismatch')
                if parse_iso_timestamp(receipt['created_at']) is None:
                    raise ValueError('invalid creation timestamp')
                if _key(identifier) in self.creations:
                    raise ValueError('duplicate creation identity')
                self.creations[_key(identifier)] = receipt
        except (OSError, ValueError) as exc:
            raise SymphonyError(
                "cannot read identifier ledger; restore it before creating or deleting tickets",
                path=str(self.path), error=str(exc),
            ) from exc

    def observe(self, identifier: object, planning_ref: PlanningRef | None = None) -> None:
        if not isinstance(identifier, str) or not IDENTIFIER_RE.fullmatch(identifier):
            return
        key = _key(identifier)
        self.issued.add(key)
        if planning_ref is not None:
            self.planning_refs.setdefault(key, planning_ref)
        match = _NUMBERED_ID.fullmatch(key)
        if match:
            self.high_water[match[1]] = max(
                self.high_water.get(match[1], 0), int(match[2])
            )

    def observe_history(self, path: Path) -> None:
        """Read old run identities without opening/migrating a RunRegistry."""
        try:
            if not _exists(path):
                return
            connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
            try:
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('runs', 'pm_handoff_items')"
                )}
                if "runs" in tables:
                    for (identifier,) in connection.execute(
                        "SELECT issue_id FROM runs UNION SELECT identifier FROM runs"
                    ):
                        self.observe(identifier)
                if "pm_handoff_items" in tables:
                    for identifier, change, task, revision, handoff, snapshot in connection.execute(
                        "SELECT i.deterministic_ticket_id, i.change_id, i.task_id, "
                        "i.task_revision, i.handoff_id, b.snapshot_id FROM pm_handoff_items i "
                        "LEFT JOIN pm_handoff_batches b ON b.handoff_id=i.handoff_id "
                        "AND b.project_id=i.project_id AND b.change_id=i.change_id"
                    ):
                        self.observe(identifier, _planning_ref({
                            "change_id": change, "task_id": task, "task_revision": revision,
                            "handoff_id": handoff, "snapshot_id": snapshot,
                        }))
            finally:
                connection.close()
        except (OSError, sqlite3.Error) as exc:
            raise SymphonyError(
                "cannot read ticket history; ID allocation stopped",
                path=str(path), error=str(exc),
            ) from exc

    def next_identifier(self, prefix: str) -> str:
        prefix = validate_identifier(prefix, field="prefix")
        return validate_identifier(f"{prefix}-{self.high_water.get(prefix.casefold(), 0) + 1}")

    def matching_creation(self, identifier: str, creation_key: str, input_hash: str) -> dict[str, str] | None:
        receipt = self.creations.get(_key(identifier))
        if receipt and receipt['identifier'] == identifier and receipt['key'] == creation_key and receipt['input_hash'] == input_hash:
            return receipt
        return None

    def reserve(self, identifier: str, planning_ref: PlanningRef | None = None, *,
                creation_key: str | None = None, input_hash: str = '', created_at: str = '') -> str | None:
        key = _key(identifier)
        # PM reconciliation restores the same staged task after a crash or
        # deletion. Ordinary CLI/API creates carry no planning identity.
        same_task = planning_ref is not None and self.planning_refs.get(key) == planning_ref
        receipt = self.matching_creation(identifier, creation_key, input_hash) if creation_key else None
        if key in self.issued and not same_task and receipt is None:
            raise SymphonyError("ticket identifier was already issued", identifier=identifier)
        self.observe(identifier, planning_ref)
        if creation_key:
            self.creations[key] = receipt or {'identifier': identifier, 'key': creation_key,
                                              'input_hash': input_hash, 'created_at': created_at}
            return self.creations[key]['created_at']
        return None

    def save(self) -> None:
        """Persist reservations before publishing/deleting a ticket; gaps are safe."""
        data = json.dumps(
            {"version": 1, "issued": sorted(self.issued), "high_water": self.high_water,
             "planning_refs": {key: asdict(ref) for key, ref in self.planning_refs.items()},
             "creations": self.creations},
            sort_keys=True, indent=2,
        ) + "\n"
        name: str | None = None
        try:
            fd, name = tempfile.mkstemp(prefix=".tmp-symphony-ids-", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            if os.name != "nt":
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except OSError as exc:
            raise SymphonyError(
                "cannot save identifier ledger; ticket write stopped",
                path=str(self.path), error=str(exc),
            ) from exc
        finally:
            if name is not None:
                Path(name).unlink(missing_ok=True)
