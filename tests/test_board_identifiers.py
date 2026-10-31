"""Board IDs belong to one task forever, including after deletion/restart."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from symphony.errors import SymphonyError
from symphony.issue import PlanningRef
from symphony.orchestrator.run_registry import RunRegistry
from symphony.trackers import identifiers as id_module
from symphony.trackers import file as file_module
from symphony.trackers.file import FileBoardTracker, issue_from_file
from symphony.trackers.identifiers import LEDGER_NAME
from symphony.workflow import TrackerConfig, build_service_config, load_workflow


def tracker(root: Path, history: Path | None = None) -> FileBoardTracker:
    return FileBoardTracker(TrackerConfig(
        kind="file", endpoint="", api_key="", project_slug="",
        active_states=("Todo",), terminal_states=("Done", "Archive"),
        board_root=root, run_history_path=history,
    ))


def legacy_card(root: Path, identifier: str, *, filename: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / (filename or f"{identifier}.md")
    path.write_text(f"---\nid: {identifier}\ntitle: Old task\nstate: Archive\n---\n")
    return path


@pytest.mark.parametrize("api", ["next", "validated"])
def test_deleted_highest_number_is_not_reissued_after_restart(tmp_path, api):
    root = tmp_path / "board"

    def create(board):
        if api == "next":
            return board.create_with_next_identifier("DEMO", title="task")[0]
        return board.create_validated(identifier=None, prefix="DEMO", title="task")[0]

    board = tracker(root)
    assert create(board) == "DEMO-1"
    assert create(board) == "DEMO-2"
    board.delete("DEMO-2")
    assert create(tracker(root)) == "DEMO-3"
    board.delete("DEMO-1")
    board.delete("DEMO-3")
    assert create(tracker(root)) == "DEMO-4"


@pytest.mark.parametrize("api", ["direct", "validated"])
@pytest.mark.parametrize("identifier", ["DEMO-7", "demo-7", "DEMO-007"])
def test_manual_creation_cannot_reuse_an_issued_number(tmp_path, api, identifier):
    board = tracker(tmp_path / "board")
    board.create(identifier="DEMO-7", title="old")
    board.delete("DEMO-7")
    with pytest.raises(SymphonyError, match="already issued"):
        if api == "direct":
            board.create(identifier=identifier, title="unrelated")
        else:
            board.create_validated(identifier=identifier, title="unrelated")
    assert not list(board.board_root.glob("*.md"))


def test_manual_ids_advance_only_their_prefix_and_allow_unused_holes(tmp_path):
    board = tracker(tmp_path / "board")
    board.create(identifier="TASK-020", title="high")
    board.create(identifier="TASK-2", title="unused lower number")
    board.create(identifier="CUSTOM", title="named")
    board.delete("CUSTOM")
    with pytest.raises(SymphonyError, match="already issued"):
        board.create(identifier="custom", title="different task")
    assert board.create_with_next_identifier("task", title="next")[0] == "task-21"
    assert board.create_with_next_identifier("OTHER", title="independent")[0] == "OTHER-1"


def test_migration_uses_file_names_and_front_matter_in_all_states(tmp_path):
    root = tmp_path / "board"
    old = legacy_card(root, "DEMO-40", filename="copied-card.md")
    original = old.read_bytes()
    legacy_card(root, "DEMO-7")
    assert tracker(root).create_with_next_identifier("DEMO", title="new")[0] == "DEMO-41"
    assert old.read_bytes() == original


def test_first_operation_can_delete_legacy_highest_without_losing_it(tmp_path):
    root = tmp_path / "board"
    legacy_card(root, "DEMO-10")
    legacy_card(root, "DEMO-3")
    board = tracker(root)
    board.delete("DEMO-10")
    board.delete("DEMO-3")
    assert tracker(root).create_with_next_identifier("DEMO", title="new")[0] == "DEMO-11"


def test_migration_recovers_deleted_ids_from_old_lock_files(tmp_path):
    root = tmp_path / "board"
    (root / ".locks").mkdir(parents=True)
    (root / ".locks/DEMO-12.lock").touch()
    board = tracker(root)
    assert board.create_with_next_identifier("DEMO", title="new")[0] == "DEMO-13"
    with pytest.raises(SymphonyError, match="already issued"):
        board.create(identifier="DEMO-12", title="old number")
    assert (root / ".locks/DEMO-12.lock").exists()


def test_removing_ticket_file_directly_does_not_reset_number(tmp_path):
    root = tmp_path / "board"
    board = tracker(root)
    _, path = board.create_with_next_identifier("DEMO", title="old")
    path.unlink()
    assert tracker(root).create_with_next_identifier("DEMO", title="new")[0] == "DEMO-2"


def test_lost_ledger_is_recovered_from_persistent_ticket_locks(tmp_path):
    root = tmp_path / "board"
    board = tracker(root)
    board.create(identifier="DEMO-9", title="old")
    board.delete("DEMO-9")
    (root / LEDGER_NAME).unlink()
    assert tracker(root).create_with_next_identifier("DEMO", title="new")[0] == "DEMO-10"


def test_legacy_run_history_reserves_deleted_id_without_changing_history(tmp_path):
    root = tmp_path / "external-board"
    workflow = tmp_path / "project/WORKFLOW.md"
    workflow.parent.mkdir()
    workflow.write_text(f"---\ntracker:\n  kind: file\n  board_root: {root}\n---\nprompt\n")
    config = build_service_config(load_workflow(workflow))
    history = workflow.parent / ".symphony/state.db"
    assert config.tracker.run_history_path == history
    old = legacy_card(root, "DEMO-42")
    issue = issue_from_file(old)
    assert issue is not None
    registry = RunRegistry(history)
    now = datetime.now(timezone.utc)
    run = registry.acquire_run(
        issue, workspace_path=tmp_path / "old-workspace", attempt=None,
        attempt_kind="initial", agent_kind="codex", now=now,
    )
    assert run
    registry.complete_run(issue_id=issue.id, run_id=run, status="normal", now=now + timedelta(seconds=1))
    registry.close()
    old.unlink()  # Old deployment had no ledger and the card is now gone.
    before = history.read_bytes()
    board = FileBoardTracker(config.tracker)
    new_id, _ = board.create_with_next_identifier("DEMO", title="unrelated")
    assert new_id == "DEMO-43"
    with pytest.raises(SymphonyError, match="already issued"):
        board.create(identifier="DEMO-42", title="must not inherit history")
    assert history.read_bytes() == before
    registry = RunRegistry(history)
    assert [record.run_id for record in registry.recent_runs(issue_id="DEMO-42")] == [run]
    assert registry.recent_runs(issue_id=new_id) == []
    registry.close()


@pytest.mark.parametrize("data", [
    "{", '{"version": 2}', '{"version": 1, "issued": {}, "high_water": {}}',
    '{"version": 1, "issued": [], "high_water": {"DEMO": -1}}',
    '{"version": 1, "issued": [], "high_water": {"DEMO": true}}',
    '{"version": true, "issued": [], "high_water": {}}',
])
def test_bad_ledger_blocks_creation_and_deletion_without_data_loss(tmp_path, data):
    root = tmp_path / "board"
    card = legacy_card(root, "DEMO-1")
    ledger = root / LEDGER_NAME
    ledger.write_text(data)
    original = card.read_bytes()
    board = tracker(root)
    with pytest.raises(SymphonyError, match="cannot read identifier ledger"):
        board.create_with_next_identifier("DEMO", title="new")
    with pytest.raises(SymphonyError, match="cannot read identifier ledger"):
        board.delete("DEMO-1")
    assert card.read_bytes() == original
    assert ledger.read_text() == data


def test_unreadable_history_blocks_reissuing_ids(tmp_path):
    root = tmp_path / "board"
    card = legacy_card(root, "DEMO-1")
    history = tmp_path / ".symphony/state.db"
    history.parent.mkdir()
    history.write_bytes(b"not a SQLite database")
    board = tracker(root)
    with pytest.raises(SymphonyError, match="cannot read ticket history"):
        board.create_with_next_identifier("DEMO", title="new")
    with pytest.raises(SymphonyError, match="cannot read ticket history"):
        board.delete("DEMO-1")
    assert card.exists()
    assert not (root / LEDGER_NAME).exists()


def test_reservation_failure_never_publishes_or_deletes_a_ticket(tmp_path, monkeypatch):
    root = tmp_path / "board"
    old = legacy_card(root, "DEMO-1")
    replace = id_module.os.replace

    def fail_ledger(source, target):
        if Path(target).name == LEDGER_NAME:
            raise OSError("simulated disk failure")
        return replace(source, target)

    monkeypatch.setattr(id_module.os, "replace", fail_ledger)
    board = tracker(root)
    with pytest.raises(SymphonyError, match="cannot save identifier ledger"):
        board.create_with_next_identifier("DEMO", title="new")
    with pytest.raises(SymphonyError, match="cannot save identifier ledger"):
        board.delete("DEMO-1")
    assert old.exists()
    assert not (root / "DEMO-2.md").exists()
    assert not list(root.glob(".tmp-symphony-ids-*"))


def test_ticket_write_failure_leaves_a_safe_gap(tmp_path, monkeypatch):
    root = tmp_path / "board"
    board = tracker(root)
    write = file_module.write_ticket_atomic

    def fail(*args):
        raise OSError("simulated crash after reservation")

    with monkeypatch.context() as patch:
        patch.setattr(file_module, "write_ticket_atomic", fail)
        with pytest.raises(OSError, match="simulated crash"):
            board.create_with_next_identifier("DEMO", title="interrupted")
    assert file_module.write_ticket_atomic is write
    assert tracker(root).create_with_next_identifier("DEMO", title="next")[0] == "DEMO-2"
    with pytest.raises(SymphonyError, match="already issued"):
        board.create(identifier="DEMO-1", title="different task")


def test_rejected_validation_does_not_consume_a_number(tmp_path):
    board = tracker(tmp_path / "board")

    def reject(*args):
        raise ValueError("invalid dependencies")

    with pytest.raises(ValueError, match="invalid dependencies"):
        board.create_validated(identifier=None, prefix="DEMO", title="bad", validate=reject)
    assert board.create_with_next_identifier("DEMO", title="valid")[0] == "DEMO-1"


def test_allocation_is_unique_across_processes_after_deletion(tmp_path):
    root = tmp_path / "board"
    board = tracker(root)
    board.create(identifier="DEMO-1", title="old")
    board.delete("DEMO-1")
    code = """
import sys
from pathlib import Path
from symphony.trackers.file import FileBoardTracker
from symphony.workflow import TrackerConfig
cfg = TrackerConfig(kind='file', endpoint='', api_key='', project_slug='',
    active_states=('Todo',), terminal_states=('Done',), board_root=Path(sys.argv[1]))
print(FileBoardTracker(cfg).create_with_next_identifier('DEMO', title='parallel')[0])
"""
    processes = [subprocess.Popen(
        [sys.executable, "-c", code, str(root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) for _ in range(6)]
    identifiers = []
    try:
        for process in processes:
            out, err = process.communicate(timeout=20)
            assert process.returncode == 0, err
            identifiers.append(out.strip())
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait()
    assert set(identifiers) == {f"DEMO-{i}" for i in range(2, 8)}
    data = json.loads((root / LEDGER_NAME).read_text())
    assert data["high_water"]["demo"] == 7


@pytest.mark.parametrize("field", ["change_id", "task_id", "task_revision", "handoff_id", "snapshot_id"])
def test_pm_identity_can_restore_only_the_same_original_task(tmp_path, field):
    root = tmp_path / "board"
    board = tracker(root)
    reference = PlanningRef("change", "task", "revision", "handoff", "snapshot")
    board.create(identifier="PM-1", title="original", planning_ref=reference)
    board.delete("PM-1")
    board = tracker(root)
    with pytest.raises(SymphonyError, match="already issued"):
        board.create(identifier="PM-1", title="other task", planning_ref=replace(reference, **{field: "different"}))
    with pytest.raises(SymphonyError, match="already issued"):
        board.create(identifier="PM-1", title="ordinary creation")
    path = board.create(identifier="PM-1", title="original", planning_ref=reference)
    assert issue_from_file(path).planning_ref == reference
    assert board.create_with_next_identifier("PM", title="new task")[0] == "PM-2"


def test_pm_write_interruption_can_resume_the_same_identity(tmp_path, monkeypatch):
    root = tmp_path / "board"
    reference = PlanningRef("change", "task", "revision", "handoff", "snapshot")

    def crash(*args):
        raise OSError("interrupted before publication")

    with monkeypatch.context() as patch:
        patch.setattr(file_module, "write_ticket_atomic", crash)
        with pytest.raises(OSError):
            tracker(root).create(identifier="PM-1", title="original", planning_ref=reference)
    restored = tracker(root).create(identifier="PM-1", title="original", planning_ref=reference)
    assert restored.exists()


@pytest.mark.parametrize("valid_identity", [True, False])
def test_migration_uses_pm_handoff_identity_for_original_task(tmp_path, valid_identity):
    root = tmp_path / "board"
    history = tmp_path / ".symphony/state.db"
    history.parent.mkdir()
    reference = PlanningRef("change", "task", "revision", "handoff", "snapshot")
    with sqlite3.connect(history) as connection:
        connection.execute("CREATE TABLE pm_handoff_items (deterministic_ticket_id TEXT, change_id TEXT, task_id TEXT, task_revision TEXT, handoff_id TEXT, project_id TEXT)")
        connection.execute("CREATE TABLE pm_handoff_batches (handoff_id TEXT, project_id TEXT, change_id TEXT, snapshot_id TEXT)")
        connection.execute("INSERT INTO pm_handoff_items VALUES ('PM-9','change','task','revision','handoff','project')")
        connection.execute("INSERT INTO pm_handoff_batches VALUES ('handoff','project','change',?)", ("snapshot" if valid_identity else None,))
    before = history.read_bytes()
    board = tracker(root)
    with pytest.raises(SymphonyError, match="already issued"):
        board.create(identifier="PM-9", title="different task")
    if valid_identity:
        assert board.create(identifier="PM-9", title="original", planning_ref=reference).exists()
    else:
        with pytest.raises(SymphonyError, match="already issued"):
            board.create(identifier="PM-9", title="unverified original", planning_ref=reference)
    assert board.create_with_next_identifier("PM", title="new")[0] == "PM-10"
    assert history.read_bytes() == before


@pytest.mark.parametrize("blocked", ["ledger", "history"])
def test_stat_permission_error_cannot_be_mistaken_for_missing_id_data(tmp_path, monkeypatch, blocked):
    root = tmp_path / "board"
    old = legacy_card(root, "DEMO-1")
    board = tracker(root)
    target = root / LEDGER_NAME if blocked == "ledger" else tmp_path / ".symphony/state.db"
    original_stat = Path.stat

    def denied(path, *args, **kwargs):
        if path == target:
            raise PermissionError("denied by filesystem")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)
    with pytest.raises(SymphonyError, match="cannot read"):
        board.create_with_next_identifier("DEMO", title="unrelated")
    with pytest.raises(SymphonyError, match="cannot read"):
        board.delete("DEMO-1")
    assert old.exists()
    assert not (root / "DEMO-2.md").exists()


@pytest.mark.parametrize("identifier", ["allocator", "ALLOCATOR", "allocator___"])
def test_valid_id_alias_of_allocator_lock_does_not_deadlock(tmp_path, identifier):
    code = """
import sys
from pathlib import Path
from symphony.errors import SymphonyError
from symphony.trackers.file import FileBoardTracker
from symphony.workflow import TrackerConfig
root, identifier = Path(sys.argv[1]), sys.argv[2]
board = FileBoardTracker(TrackerConfig(kind='file', endpoint='', api_key='', project_slug='',
    active_states=('Todo',), terminal_states=('Done',), board_root=root))
board.create(identifier=identifier, title='original')
board.transition(identifier, 'Done')
board.delete(identifier)
try:
    board.create(identifier=identifier, title='unrelated')
except SymphonyError as error:
    assert 'already issued' in str(error)
else:
    raise AssertionError('reissued ID')
assert board.create_with_next_identifier('DEMO', title='new')[0] == 'DEMO-1'
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "board"), identifier],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
