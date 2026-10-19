"""Exhaustive interruption matrix over every persisted PM checkpoint.

Each row interrupts exactly one durable boundary (an ``os.replace``, a journal
unlink, a SQLite statement/COMMIT, or a registry/adapter call) by raising a
``BaseException`` so no product ``except Exception`` handler can tidy up, the
way a killed process would not. The test then drops every in-memory object,
re-opens fresh registry/repository/tracker instances on the same paths
("restart"), runs the product's own recovery entry point, and asserts:

* before recovery the durable state is one of the two legal states and no
  ticket of an unpublished batch is natively runnable;
* after recovery and a retry of the SAME operation the state is the new one,
  with the exact ticket count;
* replaying the same operation again changes nothing.

The report lives in .sdlc/work/261004-pm-workspace/scratch/checkpoint-matrix/.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, cast

import pytest
import yaml

import symphony.orchestrator.run_registry_pm as registry_module
import symphony.pm.application as application_module
import symphony.pm.documents as documents_module
import symphony.trackers.file as tracker_module
from symphony.orchestrator.run_registry import RunRegistry
from symphony.pm.application import PmApplication
from symphony.pm.approval import PmApprovalService
from symphony.pm.authority import PmAuthority
from symphony.pm.documents import PmDocumentRepository
from symphony.pm.exchange import WorkbookExchange
from symphony.pm.handoff import PmHandoffService
from symphony.pm.models import (
    ArchiveCandidate,
    ArchiveResult,
    PmError,
    PmRequestContext,
    ValidationReport,
)
from symphony.pm.verification import PmVerificationService
from tests.test_pm_approval import _populate
from tests.test_pm_archive_application import _ArchiveAdapter, _ready_application
from tests.test_pm_documents import _repository
from tests.test_pm_handoff import _approved, _tracker


class Crash(BaseException):
    """Simulated process death: bypasses every ``except Exception``."""


# --------------------------------------------------------------------------
# Fault injection
# --------------------------------------------------------------------------


@dataclass
class Fault:
    """Raise ``Crash`` before/after the ``nth`` matching durable call."""

    kind: str  # "replace" | "unlink" | "sql" | "call"
    match: Callable[[tuple[Any, ...]], bool]
    nth: int = 1
    when: str = "before"
    scope: str | None = None  # registry method that must be on the stack
    hits: int = 0
    fired: bool = False
    active_scopes: list[str] = field(default_factory=list)

    def wrap(self, kind: str, args: tuple[Any, ...], thunk: Callable[[], Any]) -> Any:
        if (
            self.fired
            or kind != self.kind
            or (self.scope is not None and self.scope not in self.active_scopes)
            or not self.match(args)
        ):
            return thunk()
        self.hits += 1
        if self.hits == self.nth and self.when == "before":
            self.fired = True
            raise Crash(f"crash before {kind} #{self.nth}")
        result = thunk()
        if self.hits == self.nth and self.when == "after":
            self.fired = True
            raise Crash(f"crash after {kind} #{self.nth}")
        return result


class _OsProxy:
    def __init__(self, real: Any, fault: Fault) -> None:
        self._real = real
        self._fault = fault

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def replace(self, src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        return self._fault.wrap(
            "replace", (Path(src), Path(dst)),
            lambda: self._real.replace(src, dst, *args, **kwargs),
        )


class _ConnProxy:
    def __init__(self, real: sqlite3.Connection, fault: Fault) -> None:
        self._real = real
        self._fault = fault

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def execute(self, sql: str, *args: Any) -> Any:
        return self._fault.wrap(
            "sql", (sql.strip(),), lambda: self._real.execute(sql, *args)
        )


@contextmanager
def inject(
    fault: Fault,
    *,
    os_modules: tuple[Any, ...] = (),
    registry: RunRegistry | None = None,
    scoped_methods: tuple[str, ...] = (),
    calls: tuple[tuple[Any, str], ...] = (),
) -> Iterator[Fault]:
    """Install the fault on the named seams and always restore them."""
    restore: list[Callable[[], None]] = []
    try:
        for module in os_modules:
            original_os = module.os
            module.os = _OsProxy(original_os, fault)
            restore.append(lambda module=module, original_os=original_os: setattr(module, "os", original_os))
        if fault.kind == "unlink":
            original_unlink = Path.unlink

            def unlink(self: Path, missing_ok: bool = False) -> None:
                return fault.wrap(
                    "unlink", (self,),
                    lambda: original_unlink(self, missing_ok=missing_ok),
                )

            Path.unlink = unlink  # type: ignore[method-assign]
            restore.append(lambda: setattr(Path, "unlink", original_unlink))
        if registry is not None and fault.kind == "sql":
            registry._connect()
            real_conn = registry._conn
            assert real_conn is not None
            registry._conn = _ConnProxy(real_conn, fault)  # type: ignore[assignment]

            def unproxy() -> None:
                if isinstance(registry._conn, _ConnProxy):
                    registry._conn = registry._conn._real

            restore.append(unproxy)
        for name in scoped_methods:
            assert registry is not None
            original_method = getattr(registry, name)

            def scoped(*args: Any, _name: str = name, _original: Any = original_method, **kwargs: Any) -> Any:
                fault.active_scopes.append(_name)
                try:
                    return _original(*args, **kwargs)
                finally:
                    fault.active_scopes.pop()

            setattr(registry, name, scoped)
            restore.append(lambda name=name: registry.__dict__.pop(name, None))
        for target, name in calls:
            original_call = getattr(target, name)

            def wrapped(*args: Any, _original: Any = original_call, **kwargs: Any) -> Any:
                return fault.wrap("call", (args, kwargs), lambda: _original(*args, **kwargs))

            setattr(target, name, wrapped)
            restore.append(lambda target=target, name=name: target.__dict__.pop(name, None))
        yield fault
    finally:
        for undo in reversed(restore):
            undo()


def _name(path: Path) -> str:
    return path.name


def _crash(
    action: Callable[[], Any], fault: Fault, **seams: Any
) -> None:
    with inject(fault, **seams):
        with pytest.raises(Crash):
            action()
    assert fault.fired, "the interruption point was never reached"


# --------------------------------------------------------------------------
# Handoff batch create/publish matrix
# --------------------------------------------------------------------------


OP = "matrix-op"


@dataclass
class HandoffEnv:
    tmp: Path
    change_id: str
    revision: str
    registry: RunRegistry
    repo: PmDocumentRepository
    tracker: Any
    service: PmHandoffService

    @property
    def ctx(self) -> PmRequestContext:
        return PmRequestContext("p1", self.change_id, self.revision, OP)


def _handoff_restart(old: HandoffEnv) -> HandoffEnv:
    old.registry.close()  # an open write transaction dies with the process
    registry = RunRegistry(old.tmp / "state.db")
    repo = PmDocumentRepository(
        project_id="p1", project_root=old.tmp / "project",
        registry=registry, planning_workspace_root=old.tmp / "planning-root",
    )
    tracker = _tracker(old.tmp)
    return HandoffEnv(
        old.tmp, old.change_id, old.revision, registry, repo, tracker,
        PmHandoffService(repo, registry, tracker),
    )


def _handoff_env(tmp_path: Path) -> tuple[HandoffEnv, str]:
    repo, change_id, change = _approved(tmp_path)
    tracker = _tracker(tmp_path)
    env = HandoffEnv(
        tmp_path, change_id, change.revision, repo.registry, repo, tracker,
        PmHandoffService(repo, repo.registry, tracker),
    )
    token = env.service.preview(env.ctx, owner="owner").preview_token
    return env, token


def _publish(env: HandoffEnv, token: str):
    return env.service.publish(env.ctx, token, token, owner="owner")


def _durable_handoff(tmp: Path, change_id: str) -> dict[str, Any]:
    """Committed rows only: a fresh connection never sees uncommitted work."""
    with sqlite3.connect(tmp / "state.db") as conn:
        batches = conn.execute(
            "SELECT handoff_id, state FROM pm_handoff_batches WHERE change_id = ?",
            (change_id,),
        ).fetchall()
        items = conn.execute(
            "SELECT state FROM pm_handoff_items WHERE change_id = ?", (change_id,)
        ).fetchall()
        managed = conn.execute(
            "SELECT state FROM pm_managed_tickets WHERE change_id = ?", (change_id,)
        ).fetchall()
    fallback_path = tmp / "state.db.pm-managed.json"
    fallback = (
        json.loads(fallback_path.read_text(encoding="utf-8")).get("p1", {})
        if fallback_path.exists() else {}
    )
    return {
        "batches": batches,
        "item_states": sorted(row[0] for row in items),
        "managed_states": sorted(row[0] for row in managed),
        "fallback": fallback,
    }


def _assert_not_mixed(state: dict[str, Any]) -> str:
    if not state["batches"]:
        assert state["item_states"] == [] and state["managed_states"] == []
        return "NONE"
    assert len(state["batches"]) == 1
    batch_state = state["batches"][0][1]
    assert batch_state in {"STAGING", "PUBLISHED"}
    assert state["item_states"] == [batch_state, batch_state]
    assert state["managed_states"] == [batch_state, batch_state]
    return batch_state


def _assert_handoff_published(env: HandoffEnv) -> None:
    expected_id = uuid.uuid5(uuid.NAMESPACE_URL, f"symphony-pm:{OP}").hex
    durable = _durable_handoff(env.tmp, env.change_id)
    assert durable["batches"] == [(expected_id, "PUBLISHED")]
    assert durable["item_states"] == ["PUBLISHED", "PUBLISHED"]
    assert durable["managed_states"] == ["PUBLISHED", "PUBLISHED"]
    issues = env.tracker.scan_all()
    assert len(issues) == 2
    assert set(durable["fallback"]) == {issue.identifier for issue in issues}
    authority = PmAuthority(env.registry, env.tmp / "locks")
    eligibility = {
        issue.planning_ref.task_id: authority.recognize(issue, "p1").runnable
        for issue in issues
    }
    # Prerequisite is natively runnable; the dependent waits on it.
    assert eligibility == {"T-ZZZZZZZZ": True, "T-ABCDEFGH": False}


def _recover_and_replay_handoff(env: HandoffEnv, token: str) -> HandoffEnv:
    env = _handoff_restart(env)
    assert env.service.preview(env.ctx, owner="owner").preview_token == token
    expected_id = uuid.uuid5(uuid.NAMESPACE_URL, f"symphony-pm:{OP}").hex
    if env.registry.get_pm_handoff_by_operation(OP) is not None:
        reconciled = env.service.reconcile(OP)
        assert (reconciled.handoff_id, reconciled.state) == (expected_id, "PUBLISHED")
        _assert_handoff_published(env)
    retried = _publish(env, token)
    assert (retried.handoff_id, retried.state) == (expected_id, "PUBLISHED")
    _assert_handoff_published(env)
    replay = _publish(env, token)
    assert replay == retried
    assert env.service.reconcile(OP).handoff_id == expected_id
    _assert_handoff_published(env)
    return env


def _fallback_replace() -> Fault:
    return Fault("replace", lambda a: a[1].name.endswith(".pm-managed.json"), scope="create_pm_handoff_batch")


def _ticket_replace(nth: int, when: str) -> Fault:
    return Fault("replace", lambda a: a[1].name.startswith("PM-") and a[1].suffix == ".md", nth=nth, when=when)


def _sql(prefix: str, scope: str, nth: int = 1, when: str = "before") -> Fault:
    return Fault("sql", lambda a: a[0].startswith(prefix), nth=nth, when=when, scope=scope)


_REGISTRY_SCOPES = ("create_pm_handoff_batch", "publish_pm_handoff")

# boundary id -> (fault factory, durable batch state, ticket files on board,
#                 fallback index present) observed after restart, pre-recovery.
HANDOFF_ROWS: dict[str, tuple[Callable[[], Fault], str, int, bool]] = {
    "H01-create-before-fallback-replace": (lambda: _fallback_replace(), "NONE", 0, False),
    "H02-create-after-fallback-before-commit": (lambda: _sql("COMMIT", "create_pm_handoff_batch"), "NONE", 0, True),
    "H03-create-after-commit": (lambda: _sql("COMMIT", "create_pm_handoff_batch", when="after"), "STAGING", 0, True),
    "H04-ticket1-before-replace": (lambda: _ticket_replace(1, "before"), "STAGING", 0, True),
    "H05-ticket1-after-replace": (lambda: _ticket_replace(1, "after"), "STAGING", 1, True),
    "H06-ticket2-before-replace": (lambda: _ticket_replace(2, "before"), "STAGING", 1, True),
    "H07-ticket2-after-replace": (lambda: _ticket_replace(2, "after"), "STAGING", 2, True),
    "H08-publish-mid-transaction": (lambda: _sql("UPDATE pm_handoff_items", "publish_pm_handoff", nth=2), "STAGING", 2, True),
    "H09-publish-before-commit": (lambda: _sql("COMMIT", "publish_pm_handoff"), "STAGING", 2, True),
    "H10-publish-after-commit": (lambda: _sql("COMMIT", "publish_pm_handoff", when="after"), "PUBLISHED", 2, True),
}


def _handoff_seams(env: HandoffEnv, fault: Fault) -> dict[str, Any]:
    seams: dict[str, Any] = {"registry": env.registry, "scoped_methods": _REGISTRY_SCOPES}
    if fault.kind == "replace":
        seams["os_modules"] = (registry_module, tracker_module)
    return seams


@pytest.mark.parametrize("boundary", list(HANDOFF_ROWS))
def test_handoff_checkpoint(tmp_path: Path, boundary: str) -> None:
    factory, expected_state, expected_files, fallback_present = HANDOFF_ROWS[boundary]
    env, token = _handoff_env(tmp_path)
    fault = factory()
    _crash(lambda: _publish(env, token), fault, **_handoff_seams(env, fault))

    env = _handoff_restart(env)
    durable = _durable_handoff(tmp_path, env.change_id)
    assert _assert_not_mixed(durable) == expected_state
    assert bool(durable["fallback"]) is fallback_present
    issues = env.tracker.scan_all()
    assert len(issues) == expected_files
    authority = PmAuthority(env.registry, tmp_path / "locks")
    for issue in issues:
        membership = authority.recognize(issue, "p1")
        assert membership.managed
        if expected_state != "PUBLISHED":
            assert not membership.runnable, boundary
    _recover_and_replay_handoff(env, token)


@pytest.mark.parametrize(
    ("boundary", "first", "second", "second_entry"),
    [
        # Crash in the replay branch of create_pm_handoff_batch, which rewrites
        # the fallback index inside its own transaction.
        ("H11-replay-create-fallback-replace", lambda: _sql("COMMIT", "create_pm_handoff_batch", when="after"),
         _fallback_replace, "publish"),
        # Crash while reconcile re-creates the second, missing ticket.
        ("H12-reconcile-recreate-ticket-replace", lambda: _ticket_replace(1, "after"),
         lambda: _ticket_replace(1, "before"), "reconcile"),
        # Crash in reconcile's publish transaction before COMMIT.
        ("H13-reconcile-publish-before-commit", lambda: _ticket_replace(2, "after"),
         lambda: _sql("COMMIT", "publish_pm_handoff"), "reconcile"),
    ],
)
def test_handoff_recovery_double_fault(
    tmp_path: Path, boundary: str, first: Callable[[], Fault],
    second: Callable[[], Fault], second_entry: str,
) -> None:
    env, token = _handoff_env(tmp_path)
    fault = first()
    _crash(lambda: _publish(env, token), fault, **_handoff_seams(env, fault))
    env = _handoff_restart(env)
    fault = second()
    action = (
        (lambda: _publish(env, token)) if second_entry == "publish"
        else (lambda: env.service.reconcile(OP))
    )
    _crash(action, fault, **_handoff_seams(env, fault))
    env = _handoff_restart(env)
    durable = _durable_handoff(tmp_path, env.change_id)
    assert _assert_not_mixed(durable) == "STAGING", boundary
    authority = PmAuthority(env.registry, tmp_path / "locks")
    assert all(not authority.recognize(issue, "p1").runnable for issue in env.tracker.scan_all())
    _recover_and_replay_handoff(env, token)


# --------------------------------------------------------------------------
# Document journals: single-document semantic write + multi-document apply
# --------------------------------------------------------------------------


def _doc_restart(tmp: Path, registry: RunRegistry) -> PmDocumentRepository:
    registry.close()
    return PmDocumentRepository(
        project_id="p1", project_root=tmp / "project",
        registry=RunRegistry(tmp / "state.db"),
        planning_workspace_root=tmp / "planning-root",
    )


def _replace_dst(name: str) -> Callable[[tuple[Any, ...]], bool]:
    return lambda a: a[1].name == name


def _replace_src(name: str) -> Callable[[tuple[Any, ...]], bool]:
    return lambda a: a[0].name == name


_SEM_DOC = ".symphony-semantic-document.next"
_SEM_ID = ".symphony-semantic-identity.next"
_SEM_JOURNAL = ".symphony-semantic-journal.json"

# boundary id -> (fault factory, expected recovered state: "old" | "new")
SEMANTIC_ROWS: dict[str, tuple[Callable[[], Fault], str]] = {
    "S01-before-document-stage": (lambda: Fault("replace", _replace_dst(_SEM_DOC)), "old"),
    "S02-before-identity-stage": (lambda: Fault("replace", _replace_dst(_SEM_ID)), "old"),
    "S03-before-journal-write": (lambda: Fault("replace", _replace_dst(_SEM_JOURNAL)), "old"),
    "S04-before-document-publish": (lambda: Fault("replace", _replace_src(_SEM_DOC)), "new"),
    "S05-before-identity-publish": (lambda: Fault("replace", _replace_src(_SEM_ID)), "new"),
    "S06-before-journal-unlink": (lambda: Fault("unlink", lambda a: a[0].name == _SEM_JOURNAL), "new"),
    "S07-after-journal-unlink": (lambda: Fault("unlink", lambda a: a[0].name == _SEM_JOURNAL, when="after"), "new"),
}

_STORYBOARD_OP = ({"op": "create_semantic", "kind": "storyboard", "title": "Matrix frame"},)


def _semantic_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo, change_id, workspace = _repository(tmp_path)
    change = repo.create_change(change_id=change_id, locator="matrix-semantic", title="Matrix", owner="owner")
    root = workspace / "openspec" / "changes" / "matrix-semantic"
    old = ((root / "ux.md").read_bytes(), (root / "symphony.yaml").read_bytes())
    planned: dict[str, bytes] = {}
    original = documents_module._atomic_semantic_write

    def spy(root_arg, document_path, source, identity, ledger):
        planned["doc"] = source.encode()
        planned["identity"] = documents_module._identity_source(identity, ledger).encode()
        return original(root_arg, document_path, source, identity, ledger)

    monkeypatch.setattr(documents_module, "_atomic_semantic_write", spy)
    return repo, change, root, old, planned


def _semantic_patch(repo: PmDocumentRepository, change_id: str, revision: str):
    return repo.patch_document(
        PmRequestContext("p1", change_id, revision, "semantic-op"), "ux",
        _STORYBOARD_OP, owner="owner",
    )


def _semantic_state(root: Path) -> tuple[bytes, bytes]:
    return (root / "ux.md").read_bytes(), (root / "symphony.yaml").read_bytes()


def _assert_semantic_final(repo: PmDocumentRepository, change, root: Path) -> None:
    current = repo.get_change(PmRequestContext("p1", change.change_id), owner="owner")
    frames = [item for item in current.semantic_index if item.kind == "storyboard"]
    assert len(frames) == 2 and sum(item.title == "Matrix frame" for item in frames) == 1
    active = set(
        yaml.safe_load((root / "symphony.yaml").read_text(encoding="utf-8"))["semantic_ids"]["active"]
    )
    assert {item.semantic_id for item in frames} <= active
    assert not list(root.glob(".symphony-semantic-*"))
    # Idempotent replay: the same request against the old revision is refused.
    with pytest.raises(PmError) as replay:
        _semantic_patch(repo, change.change_id, change.revision)
    assert replay.value.code == "revision_conflict"
    assert repo.get_change(PmRequestContext("p1", change.change_id), owner="owner").revision == current.revision


def _retry_semantic(repo: PmDocumentRepository, change, expected: str) -> None:
    if expected == "old":
        result = _semantic_patch(repo, change.change_id, change.revision)
        assert result.previous_revision == change.revision
    else:
        with pytest.raises(PmError) as conflict:
            _semantic_patch(repo, change.change_id, change.revision)
        assert conflict.value.code == "revision_conflict"


@pytest.mark.parametrize("boundary", list(SEMANTIC_ROWS))
def test_semantic_write_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    factory, expected = SEMANTIC_ROWS[boundary]
    repo, change, root, old, planned = _semantic_env(tmp_path, monkeypatch)
    _crash(lambda: _semantic_patch(repo, change.change_id, change.revision), factory(), os_modules=(documents_module,))
    new = (planned["doc"], planned["identity"])
    assert old != new

    repo = _doc_restart(tmp_path, repo.registry)
    recovered = repo.get_change(PmRequestContext("p1", change.change_id), owner="owner")  # recovery entry
    assert _semantic_state(root) == (old if expected == "old" else new), boundary
    assert (recovered.revision == change.revision) is (expected == "old")
    assert not (root / _SEM_JOURNAL).exists()
    _retry_semantic(repo, change, expected)
    _assert_semantic_final(repo, change, root)


@pytest.mark.parametrize(
    ("boundary", "recovery_fault"),
    [
        ("SR1-recovery-after-document-publish", lambda: Fault("replace", _replace_src(_SEM_DOC), when="after")),
        ("SR2-recovery-before-journal-unlink", lambda: Fault("unlink", lambda a: a[0].name == _SEM_JOURNAL)),
    ],
)
def test_semantic_recovery_double_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str,
    recovery_fault: Callable[[], Fault],
) -> None:
    repo, change, root, _old, planned = _semantic_env(tmp_path, monkeypatch)
    _crash(lambda: _semantic_patch(repo, change.change_id, change.revision),
           Fault("replace", _replace_src(_SEM_DOC)), os_modules=(documents_module,))
    new = (planned["doc"], planned["identity"])
    repo = _doc_restart(tmp_path, repo.registry)
    _crash(lambda: repo.get_change(PmRequestContext("p1", change.change_id), owner="owner"),
           recovery_fault(), os_modules=(documents_module,))
    assert (root / _SEM_JOURNAL).exists(), boundary
    repo = _doc_restart(tmp_path, repo.registry)
    repo.get_change(PmRequestContext("p1", change.change_id), owner="owner")
    assert _semantic_state(root) == new
    _retry_semantic(repo, change, "new")
    _assert_semantic_final(repo, change, root)


_MULTI_JOURNAL = ".symphony-multi-document-journal.json"

MULTI_ROWS: dict[str, tuple[Callable[[], Fault], str]] = {
    "M01-before-prepared-journal": (lambda: Fault("replace", _replace_dst(_MULTI_JOURNAL)), "old"),
    "M02-before-document-1": (lambda: Fault("replace", _replace_dst("proposal.md")), "new"),
    "M03-before-document-2": (lambda: Fault("replace", _replace_dst("design.md")), "new"),
    "M04-before-identity": (lambda: Fault("replace", _replace_dst("symphony.yaml")), "new"),
    "M05-before-completed-journal": (lambda: Fault("replace", _replace_dst(_MULTI_JOURNAL), nth=2), "new"),
    "M06-before-journal-unlink": (lambda: Fault("unlink", lambda a: a[0].name == _MULTI_JOURNAL), "new"),
    "M07-after-journal-unlink": (lambda: Fault("unlink", lambda a: a[0].name == _MULTI_JOURNAL, when="after"), "new"),
}


def _multi_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo, change_id, workspace = _repository(tmp_path)
    change = _populate(repo, change_id, workspace, "matrix-multi")
    root = workspace / "openspec" / "changes" / "matrix-multi"
    ctx = PmRequestContext("p1", change_id)
    sources = {
        "proposal": repo.get_document(ctx, "proposal", owner="owner").source + "\nMatrix proposal note.\n",
        "design": repo.get_document(ctx, "design", owner="owner").source + "\nMatrix design note.\n",
    }
    old = _multi_state(root)
    planned: dict[str, bytes] = {}
    original = documents_module._atomic_multi_document_write

    def spy(root_arg, paths, sources_arg, identity_source):
        planned["identity"] = identity_source.encode()
        return original(root_arg, paths, sources_arg, identity_source)

    monkeypatch.setattr(documents_module, "_atomic_multi_document_write", spy)
    return repo, change, root, sources, old, planned


def _multi_state(root: Path) -> tuple[bytes, bytes, bytes]:
    return tuple(  # type: ignore[return-value]
        (root / name).read_bytes() for name in ("proposal.md", "design.md", "symphony.yaml")
    )


def _multi_patch(repo: PmDocumentRepository, change, sources: dict[str, str]):
    return repo.patch_documents(
        PmRequestContext("p1", change.change_id, change.revision, "multi-op"),
        sources, owner="owner",
    )


def _assert_multi_final(repo, change, root: Path, sources: dict[str, str], expected: str) -> None:
    if expected == "old":
        assert _multi_patch(repo, change, sources).previous_revision == change.revision
    else:
        with pytest.raises(PmError) as conflict:
            _multi_patch(repo, change, sources)
        assert conflict.value.code == "revision_conflict"
    current = repo.get_change(PmRequestContext("p1", change.change_id), owner="owner")
    assert current.revision != change.revision
    assert (root / "proposal.md").read_text(encoding="utf-8").count("Matrix proposal note.") == 1
    assert (root / "design.md").read_text(encoding="utf-8").count("Matrix design note.") == 1
    assert not (root / _MULTI_JOURNAL).exists()
    with pytest.raises(PmError) as replay:
        _multi_patch(repo, change, sources)
    assert replay.value.code == "revision_conflict"
    assert repo.get_change(PmRequestContext("p1", change.change_id), owner="owner").revision == current.revision


@pytest.mark.parametrize("boundary", list(MULTI_ROWS))
def test_multi_document_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    factory, expected = MULTI_ROWS[boundary]
    repo, change, root, sources, old, planned = _multi_env(tmp_path, monkeypatch)
    _crash(lambda: _multi_patch(repo, change, sources), factory(), os_modules=(documents_module,))
    new = (sources["proposal"].encode(), sources["design"].encode(), planned["identity"])
    assert old != new
    repo = _doc_restart(tmp_path, repo.registry)
    recovered = repo.get_change(PmRequestContext("p1", change.change_id), owner="owner")
    assert _multi_state(root) == (old if expected == "old" else new), boundary
    assert (recovered.revision == change.revision) is (expected == "old")
    assert not (root / _MULTI_JOURNAL).exists()
    _assert_multi_final(repo, change, root, sources, expected)


@pytest.mark.parametrize(
    ("boundary", "recovery_fault"),
    [
        ("MR1-recovery-after-document-1", lambda: Fault("replace", _replace_dst("proposal.md"), when="after")),
        ("MR2-recovery-before-journal-unlink", lambda: Fault("unlink", lambda a: a[0].name == _MULTI_JOURNAL)),
    ],
)
def test_multi_document_recovery_double_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str,
    recovery_fault: Callable[[], Fault],
) -> None:
    repo, change, root, sources, _old, planned = _multi_env(tmp_path, monkeypatch)
    _crash(lambda: _multi_patch(repo, change, sources),
           Fault("replace", _replace_dst("proposal.md")), os_modules=(documents_module,))
    new = (sources["proposal"].encode(), sources["design"].encode(), planned["identity"])
    repo = _doc_restart(tmp_path, repo.registry)
    _crash(lambda: repo.get_change(PmRequestContext("p1", change.change_id), owner="owner"),
           recovery_fault(), os_modules=(documents_module,))
    assert (root / _MULTI_JOURNAL).exists(), boundary
    repo = _doc_restart(tmp_path, repo.registry)
    repo.get_change(PmRequestContext("p1", change.change_id), owner="owner")
    assert _multi_state(root) == new
    _assert_multi_final(repo, change, root, sources, "new")


# --------------------------------------------------------------------------
# Archive application (fake OpenSpec adapter: the pinned CLI is not on PATH)
# --------------------------------------------------------------------------


ARCHIVE_OP = "archive-matrix-op"
_FLAGS = {"authoring": True, "exchange": True, "assistance": True, "handoff": True}


class _CrashingAdapter(_ArchiveAdapter):
    """Fake adapter that can die mid-archive on the controlled workspace only."""

    def __init__(self, workspace: Path, mode: str | None = None) -> None:
        self.workspace = workspace.resolve()
        self.mode = mode
        self.fired = False

    def archive(self, candidate: ArchiveCandidate, controlled_workspace: Path) -> ArchiveResult:
        real = controlled_workspace.resolve() == self.workspace
        if real and self.mode == "before":
            self.fired = True
            raise Crash("crash before OpenSpec archive")
        if real and self.mode == "baseline_first":
            # The pinned OpenSpec 1.14 writes the merged baseline before it
            # moves the change directory (dist/core/archive.js).
            baseline = controlled_workspace / "openspec" / "specs" / "requirements" / "spec.md"
            baseline.parent.mkdir(parents=True, exist_ok=True)
            baseline.write_text("# Archived baseline\n", encoding="utf-8")
            self.fired = True
            raise Crash("crash after baseline write, before directory move")
        if real and self.mode == "partial":
            target = Path(candidate.target_paths[0])
            source = controlled_workspace / "openspec" / "changes" / target.name.removeprefix("2026-10-04-")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(source, target)
            self.fired = True
            raise Crash("crash after move, before baseline write")
        return super().archive(candidate, controlled_workspace)


@dataclass
class ArchiveEnv:
    tmp: Path
    application: PmApplication
    repo: PmDocumentRepository
    adapter: _CrashingAdapter
    change_id: str
    revision: str
    workspace: Path
    procedures: dict[str, tuple[str, ...]]

    @property
    def ctx(self) -> PmRequestContext:
        return PmRequestContext("p1", self.change_id, self.revision, ARCHIVE_OP)


def _archive_env(tmp_path: Path) -> tuple[ArchiveEnv, ArchiveCandidate, str]:
    application, repo, approved, handoff_preview, procedures = _ready_application(tmp_path)
    item = handoff_preview.items[0]
    digest = "sha256:" + hashlib.sha256(
        json.dumps(procedures["focused"], separators=(",", ":")).encode()
    ).hexdigest()
    repo.registry.record_pm_verification(
        receipt_id="receipt", project_id="p1",
        ticket_identifier=item.deterministic_ticket_id,
        task_revision=item.task_revision, operation_id="verify", run_id="run",
        procedure_id="focused", command_digest=digest, tested_commit="a" * 40,
        return_code=0, timed_out=False, cancelled=False,
        stdout_hash="sha256:out", stderr_hash="sha256:err",
        started_at="2026-10-04T00:00:00+00:00",
        completed_at="2026-10-04T00:00:01+00:00",
        artifact_refs=(), result="passed",
    )
    verification = PmVerificationService(repo.registry, procedures)
    acceptance = verification.preview_acceptance(
        project_id="p1", ticket_identifier=item.deterministic_ticket_id,
        task_revision=item.task_revision, tested_commit="a" * 40,
    )
    verification.accept(
        preview=acceptance, confirmation_token=acceptance.confirmation_token,
        reviewer_label="reviewer",
    )
    workspace = Path(repo._claim(approved.change_id, owner="owner").workspace_path)
    adapter = _CrashingAdapter(workspace)
    application.openspec = adapter  # type: ignore[assignment]
    env = ArchiveEnv(tmp_path, application, repo, adapter, approved.change_id,
                     approved.revision, workspace, procedures)
    preview = application.preview_archive(env.ctx, "owner")
    return env, ArchiveCandidate(**preview["candidate"]), preview["confirmation_token"]


def _archive_restart(old: ArchiveEnv) -> ArchiveEnv:
    old.repo.registry.close()
    registry = RunRegistry(old.tmp / "state.db")
    repo = PmDocumentRepository(
        project_id="p1", project_root=old.tmp / "project",
        registry=registry, planning_workspace_root=old.tmp / "planning-root",
    )
    repo.validate = lambda *_args, **_kwargs: ValidationReport(True)  # type: ignore[method-assign]
    approval = PmApprovalService(
        repo, registry, lambda _root, _locator: ValidationReport(True),
        verification_procedures=old.procedures,
    )
    adapter = _CrashingAdapter(old.workspace)
    application = PmApplication(
        repo, approval, PmHandoffService(repo, registry, _tracker(old.tmp)), cast(Any, adapter),
        WorkbookExchange(), project_root=old.tmp, flags=dict(_FLAGS),
        archive_journal_root=old.tmp / "journals", development_verification=True,
    )
    return ArchiveEnv(old.tmp, application, repo, adapter, old.change_id,
                      old.revision, old.workspace, old.procedures)


def _archive_state(env: ArchiveEnv, candidate: ArchiveCandidate, op: str = ARCHIVE_OP) -> str:
    """Classify on-disk + registry state as old, new, or mixed."""
    source_dir = env.workspace / "openspec" / "changes" / "archive-ready"
    target = Path(candidate.target_paths[0])
    baseline = env.workspace / "openspec" / "specs" / "requirements" / "spec.md"
    record = env.repo.registry.get_pm_workspace("p1", env.change_id)
    assert record is not None
    journal_path = env.tmp / "journals" / f"{op}.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8")) if journal_path.exists() else {}
    if source_dir.is_dir() and not target.exists() and not baseline.exists() and record.state == "ACTIVE":
        assert journal.get("state") != "COMPLETED"
        return "old"
    effects_complete = (
        target.is_dir() and not source_dir.exists()
        and application_module._archive_effects_match(
            env.workspace.resolve(), target.resolve(), journal.get("expected_archive")
        )
    )
    if effects_complete and record.state == "RELEASED" and journal.get("state") == "COMPLETED":
        return "new"
    if effects_complete:
        # Journaled roll-forward state: effects are byte-identical to the
        # prepared transaction; only the release/COMPLETED bookkeeping lags.
        return "rollforward"
    return "mixed"


def _assert_archive_tickets_not_runnable(env: ArchiveEnv) -> None:
    tracker = _tracker(env.tmp)
    issues = tracker.scan_all()
    assert len(issues) == 1
    authority = PmAuthority(env.repo.registry, env.tmp / "locks")
    assert all(not authority.recognize(issue, "p1").runnable for issue in issues)


def _assert_archive_new_and_replay(env: ArchiveEnv, candidate: ArchiveCandidate, token: str, result: ArchiveResult, op: str = ARCHIVE_OP) -> None:
    assert _archive_state(env, candidate, op) == "new"
    assert Path(result.archived_path).resolve() == Path(candidate.target_paths[0]).resolve()
    assert env.repo.get_change(PmRequestContext("p1", env.change_id), owner="owner").state == "Archived"
    _assert_archive_tickets_not_runnable(env)
    ctx = PmRequestContext("p1", env.change_id, env.revision, op)
    replay = env.application.apply_archive(ctx, "owner", candidate, token)
    assert (replay.archived_path, replay.baseline_paths) == (result.archived_path, result.baseline_paths)
    assert _archive_state(env, candidate, op) == "new"
    assert len(list((env.workspace / "openspec" / "changes" / "archive").iterdir())) == 1
    _assert_archive_tickets_not_runnable(env)


def _journal_replace(nth: int, when: str = "before", op: str = ARCHIVE_OP) -> Fault:
    return Fault("replace", _replace_dst(f"{op}.json"), nth=nth, when=when)


def _release_call(when: str) -> Fault:
    return Fault("call", lambda a: a[1].get("state") == "RELEASED", when=when)


def _archive_crash(env: ArchiveEnv, candidate, token, fault: Fault | None, adapter_mode: str | None = None) -> None:
    action = lambda: env.application.apply_archive(env.ctx, "owner", candidate, token)  # noqa: E731
    if adapter_mode is not None:
        env.adapter.mode = adapter_mode
        with pytest.raises(Crash):
            action()
        assert env.adapter.fired
        env.adapter.mode = None
        return
    assert fault is not None
    seams: dict[str, Any] = (
        {"calls": ((env.repo.registry, "set_pm_workspace_state"),)}
        if fault.kind == "call" else {"os_modules": (application_module,)}
    )
    _crash(action, fault, **seams)


# boundary id -> (fault factory or adapter mode, state after restart)
ARCHIVE_ROWS: dict[str, tuple[Any, str]] = {
    "A01-before-prepared-journal": (lambda: _journal_replace(1), "old"),
    "A04-after-effects-before-completed-journal": (lambda: _journal_replace(2), "rollforward"),
    "A05-after-completed-journal-before-release": (lambda: _journal_replace(2, "after"), "rollforward"),
    "A06-after-release-commit": (lambda: _release_call("after"), "new"),
}


@pytest.mark.parametrize("boundary", list(ARCHIVE_ROWS))
def test_archive_checkpoint(tmp_path: Path, boundary: str) -> None:
    factory, expected = ARCHIVE_ROWS[boundary]
    env, candidate, token = _archive_env(tmp_path)
    _archive_crash(env, candidate, token, factory())
    env = _archive_restart(env)
    assert _archive_state(env, candidate) == expected, boundary
    _assert_archive_tickets_not_runnable(env)
    # Recovery entry point: retry the same operation id.
    result = env.application.apply_archive(env.ctx, "owner", candidate, token)
    if boundary.startswith("A04"):
        assert result.output == {"reconciled": True, "operation_id": ARCHIVE_OP}
    _assert_archive_new_and_replay(env, candidate, token, result)


@pytest.mark.parametrize(
    ("boundary", "recovery_fault"),
    [
        ("AR1-reconcile-after-release-before-completed", lambda: _release_call("after")),
        ("AR2-reconcile-after-completed-journal", lambda: _journal_replace(1, "after")),
    ],
)
def test_archive_reconcile_double_fault(
    tmp_path: Path, boundary: str, recovery_fault: Callable[[], Fault],
) -> None:
    env, candidate, token = _archive_env(tmp_path)
    _archive_crash(env, candidate, token, _journal_replace(2))
    env = _archive_restart(env)
    _archive_crash(env, candidate, token, recovery_fault())
    env = _archive_restart(env)
    assert _archive_state(env, candidate) in {"rollforward", "new"}, boundary
    result = env.application.apply_archive(env.ctx, "owner", candidate, token)
    _assert_archive_new_and_replay(env, candidate, token, result)


def test_archive_A02_prepared_before_effects_fails_closed_and_new_operation_recovers(
    tmp_path: Path,
) -> None:
    env, candidate, token = _archive_env(tmp_path)
    _archive_crash(env, candidate, token, None, adapter_mode="before")
    env = _archive_restart(env)
    assert _archive_state(env, candidate) == "old"
    journal = json.loads((tmp_path / "journals" / f"{ARCHIVE_OP}.json").read_text(encoding="utf-8"))
    assert journal["state"] == "PREPARED"
    _assert_archive_tickets_not_runnable(env)
    assert env.repo.get_change(PmRequestContext("p1", env.change_id), owner="owner").revision == env.revision
    # A new operation id also completes (the same id is covered below).
    retry_op = ARCHIVE_OP + "-2"
    retry_ctx = PmRequestContext("p1", env.change_id, env.revision, retry_op)
    preview = env.application.preview_archive(retry_ctx, "owner")
    assert ArchiveCandidate(**preview["candidate"]) == candidate
    result = env.application.apply_archive(retry_ctx, "owner", candidate, preview["confirmation_token"])
    _assert_archive_new_and_replay(env, candidate, preview["confirmation_token"], result, retry_op)


def test_archive_A02_same_operation_retry_is_idempotent(tmp_path: Path) -> None:
    env, candidate, token = _archive_env(tmp_path)
    _archive_crash(env, candidate, token, None, adapter_mode="before")
    env = _archive_restart(env)
    result = env.application.apply_archive(env.ctx, "owner", candidate, token)
    _assert_archive_new_and_replay(env, candidate, token, result)


def test_archive_A03_partial_openspec_effects_fail_closed(tmp_path: Path) -> None:
    env, candidate, token = _archive_env(tmp_path)
    _archive_crash(env, candidate, token, None, adapter_mode="partial")
    env = _archive_restart(env)
    assert _archive_state(env, candidate) == "mixed"
    record = env.repo.registry.get_pm_workspace("p1", env.change_id)
    assert record is not None and record.state == "ACTIVE"
    _assert_archive_tickets_not_runnable(env)
    journal = json.loads((tmp_path / "journals" / f"{ARCHIVE_OP}.json").read_text(encoding="utf-8"))
    assert journal["state"] == "PREPARED"
    # Before recovery runs, a different operation cannot start a second archive.
    with pytest.raises(PmError) as new_op:
        env.application.preview_archive(
            PmRequestContext("p1", env.change_id, env.revision, ARCHIVE_OP + "-2"), "owner"
        )
    assert new_op.value.code == "change_not_found"


def test_archive_A03_partial_effects_recover_to_legal_state(tmp_path: Path) -> None:
    env, candidate, token = _archive_env(tmp_path)
    _archive_crash(env, candidate, token, None, adapter_mode="partial")
    env = _archive_restart(env)
    try:
        result = env.application.apply_archive(env.ctx, "owner", candidate, token)
    except PmError:
        assert _archive_state(env, candidate) == "old"
    else:
        _assert_archive_new_and_replay(env, candidate, token, result)


def test_archive_A07_baseline_written_before_move_recovers_same_operation(tmp_path: Path) -> None:
    env, candidate, token = _archive_env(tmp_path)
    _archive_crash(env, candidate, token, None, adapter_mode="baseline_first")
    env = _archive_restart(env)
    assert _archive_state(env, candidate) == "mixed"
    _assert_archive_tickets_not_runnable(env)
    result = env.application.apply_archive(env.ctx, "owner", candidate, token)
    _assert_archive_new_and_replay(env, candidate, token, result)


def test_archive_recovery_refuses_symlinked_baseline_without_writing_outside(tmp_path: Path) -> None:
    env, candidate, token = _archive_env(tmp_path)
    _archive_crash(env, candidate, token, None, adapter_mode="partial")
    env = _archive_restart(env)
    specs = env.workspace / "openspec" / "specs"
    outside = tmp_path / "outside-planning-workspace"
    outside.mkdir()
    if specs.exists():
        shutil.rmtree(specs)
    specs.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PmError) as refused:
        env.application.apply_archive(env.ctx, "owner", candidate, token)
    assert refused.value.code == "archive_reconcile"
    assert list(outside.rglob("*")) == []
    record = env.repo.registry.get_pm_workspace("p1", env.change_id)
    assert record is not None and record.state == "ACTIVE"


def test_archive_recovery_by_another_owner_writes_nothing(tmp_path: Path) -> None:
    env, candidate, token = _archive_env(tmp_path)
    _archive_crash(env, candidate, token, None, adapter_mode="partial")
    env = _archive_restart(env)
    baseline = env.workspace / "openspec" / "specs"
    before = {path: path.read_bytes() for path in baseline.rglob("*") if path.is_file()} if baseline.exists() else {}
    with pytest.raises(PmError) as refused:
        env.application.apply_archive(env.ctx, "intruder", candidate, token)
    assert refused.value.code == "archive_reconcile"
    after = {path: path.read_bytes() for path in baseline.rglob("*") if path.is_file()} if baseline.exists() else {}
    assert after == before
    # The real owner can still finish the prepared archive.
    result = env.application.apply_archive(env.ctx, "owner", candidate, token)
    _assert_archive_new_and_replay(env, candidate, token, result)


def test_archive_recovery_never_overwrites_a_foreign_baseline_edit(tmp_path: Path) -> None:
    env, candidate, token = _archive_env(tmp_path)
    _archive_crash(env, candidate, token, None, adapter_mode="partial")
    env = _archive_restart(env)
    baseline = env.workspace / "openspec" / "specs" / "requirements" / "spec.md"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text("# Concurrent approved baseline\n", encoding="utf-8")
    with pytest.raises(PmError) as refused:
        env.application.apply_archive(env.ctx, "owner", candidate, token)
    assert refused.value.code == "archive_reconcile"
    assert baseline.read_text(encoding="utf-8") == "# Concurrent approved baseline\n"


def test_archive_recovery_refuses_target_symlinked_outside_workspace(tmp_path: Path) -> None:
    env, candidate, token = _archive_env(tmp_path)
    _archive_crash(env, candidate, token, None, adapter_mode="partial")
    env = _archive_restart(env)
    target = Path(candidate.target_paths[0])
    outside = tmp_path / "outside-archive"
    shutil.move(str(target), str(outside))
    target.symlink_to(outside, target_is_directory=True)
    baseline = env.workspace / "openspec" / "specs"
    before = sorted(str(p) for p in baseline.rglob("*")) if baseline.exists() else []
    with pytest.raises(PmError) as refused:
        env.application.apply_archive(env.ctx, "owner", candidate, token)
    assert refused.value.code == "archive_reconcile"
    after = sorted(str(p) for p in baseline.rglob("*")) if baseline.exists() else []
    assert after == before
    record = env.repo.registry.get_pm_workspace("p1", env.change_id)
    assert record is not None and record.state == "ACTIVE"


def test_completed_archive_replay_refuses_target_swapped_for_outside_symlink(tmp_path: Path) -> None:
    env, candidate, token = _archive_env(tmp_path)
    result = env.application.apply_archive(env.ctx, "owner", candidate, token)
    target = Path(result.archived_path)
    outside = tmp_path / "outside-archive"
    shutil.move(str(target), str(outside))
    target.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PmError) as refused:
        env.application.apply_archive(env.ctx, "owner", candidate, token)
    assert refused.value.code == "archive_reconcile"


def test_archive_recovery_ignores_a_planted_temp_symlink(tmp_path: Path) -> None:
    env, candidate, token = _archive_env(tmp_path)
    _archive_crash(env, candidate, token, None, adapter_mode="partial")
    env = _archive_restart(env)
    victim = tmp_path / "outside" / "victim.txt"
    victim.parent.mkdir()
    victim.write_text("untouched\n", encoding="utf-8")
    baseline = env.workspace / "openspec" / "specs" / "requirements" / "spec.md"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    (baseline.parent / "spec.md.pm-archive-tmp").symlink_to(victim)
    result = env.application.apply_archive(env.ctx, "owner", candidate, token)
    assert Path(result.archived_path).resolve() == Path(candidate.target_paths[0]).resolve()
    assert victim.read_text(encoding="utf-8") == "untouched\n"
    assert not baseline.is_symlink()
    assert baseline.read_text(encoding="utf-8") == "# Archived baseline\n"


def test_archive_recovery_refuses_an_aliased_change_directory(tmp_path: Path) -> None:
    env, candidate, token = _archive_env(tmp_path)
    _archive_crash(env, candidate, token, None, adapter_mode="baseline_first")
    env = _archive_restart(env)
    changes = env.workspace / "openspec" / "changes"
    (changes / "archive-ready").rename(changes / "internal-alias")
    (changes / "archive-ready").symlink_to(changes / "internal-alias", target_is_directory=True)
    with pytest.raises(PmError) as refused:
        env.application.apply_archive(env.ctx, "owner", candidate, token)
    assert refused.value.code == "archive_reconcile"
    assert (changes / "internal-alias").is_dir()
    record = env.repo.registry.get_pm_workspace("p1", env.change_id)
    assert record is not None and record.state == "ACTIVE"


def archive_recovery_fixture(tmp_path):
    target = tmp_path / "openspec/changes/archive/2026-10-14-demo"
    target.mkdir(parents=True)
    (target / "proposal.md").write_text("archived")
    baseline = "requirements/spec.md"
    text = "recovered baseline\n"
    candidate = ArchiveCandidate("p1", "c1", "r1", str(tmp_path), (str(tmp_path / "openspec/changes/demo/proposal.md"),), (str(target),))
    record = {"state": "RECONCILE", "baseline_before": {}, "expected_archive": {
        "baseline_files": {baseline: text}, "baseline_manifest": {baseline: "sha256:" + hashlib.sha256(text.encode()).hexdigest()},
        "archive_manifest": application_module._file_manifest(target),
    }}
    return target, candidate, record


def test_R32_05_archive_recovery_fsyncs_temporary_file(tmp_path, monkeypatch):
    target, candidate, record = archive_recovery_fixture(tmp_path)
    calls = []
    monkeypatch.setattr(application_module.os, "fsync", lambda fd: calls.append(fd))
    assert application_module._roll_archive_forward(tmp_path, target, candidate, record)
    print("RECOVERY_FSYNC_CALLS", calls)
    assert calls


def test_R32_05_archive_recovery_cleans_failed_temporary_file(tmp_path, monkeypatch):
    target, candidate, record = archive_recovery_fixture(tmp_path)
    def fail_replace(*_args):
        raise OSError("injected replace failure")
    monkeypatch.setattr(application_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace"):
        application_module._roll_archive_forward(tmp_path, target, candidate, record)
    leftovers = list(tmp_path.glob("openspec/specs/requirements/.spec.md.*"))
    print("RECOVERY_LEFTOVERS", [str(p.relative_to(tmp_path)) for p in leftovers])
    assert not leftovers



def test_actual_archive_transaction_waits_for_complete_snapshot_and_retains_history(tmp_path, monkeypatch):
    import threading
    import symphony.pm.documents as documents
    from symphony.pm.routes import _read_change_snapshot, _with_thread_application

    env, candidate, token = _archive_env(tmp_path)
    ready, resume, archived, archive_started = (threading.Event() for _ in range(4))
    original = documents._resolve_change_root
    outcomes = {}
    def pause_snapshot(*args, **kwargs):
        result = original(*args, **kwargs)
        if threading.current_thread().name == "archive-snapshot":
            ready.set()
            assert resume.wait(2)
        return result
    def read():
        try:
            outcomes["before"] = _read_change_snapshot(env.repo, PmRequestContext("p1", env.change_id), "owner")
        except BaseException as exc:
            outcomes["reader_error"] = exc
    def archive():
        archive_started.set()
        try:
            outcomes["result"] = _with_thread_application(env.application, lambda app: app.apply_archive(env.ctx, "owner", candidate, token), env.ctx)
            archived.set()
        except BaseException as exc:
            outcomes["archive_error"] = exc
    monkeypatch.setattr(documents, "_resolve_change_root", pause_snapshot)
    reader = threading.Thread(target=read, name="archive-snapshot")
    writer = threading.Thread(target=archive)
    reader.start()
    assert ready.wait(2)
    writer.start()
    try:
        assert archive_started.wait(2)
        assert not archived.wait(.1), "archive changed files during an active snapshot"
    finally:
        resume.set()
        reader.join(3)
        writer.join(3)
    assert not reader.is_alive() and not writer.is_alive()
    assert "reader_error" not in outcomes and "archive_error" not in outcomes, outcomes
    assert outcomes["before"].state == "Approved"
    assert archived.is_set()
    after = _read_change_snapshot(env.repo, PmRequestContext("p1", env.change_id), "owner")
    assert after.state == "Archived" and after.revision == outcomes["before"].revision
    assert all(not item.editable for item in after.documents)
    _assert_archive_tickets_not_runnable(env)
    env.repo.registry.close()
