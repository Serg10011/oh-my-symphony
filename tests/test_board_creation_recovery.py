"""A creation retry restores its own operation and never resets later work."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from symphony.cli.board import main
from symphony.errors import SymphonyError
from symphony.issue import PlanningRef
from symphony.trackers import file as file_module
from symphony.trackers.file import FileBoardTracker, parse_ticket_file, write_ticket_atomic
from symphony.trackers.identifiers import LEDGER_NAME
from symphony.workflow import TrackerConfig

KEY = 'a' * 64
PARAMS = dict(identifier='TASK-1', title='Original', state='Subtasks',
              description='Original scope', labels=['role:worker'], creation_key=KEY)


def tracker(root: Path) -> FileBoardTracker:
    return FileBoardTracker(TrackerConfig(
        kind='file', endpoint='', api_key='', project_slug='', board_root=root,
        active_states=('In Progress',), terminal_states=('Subtasks', 'Done'),
    ))


def test_replay_returns_existing_card_without_resetting_work(tmp_path):
    board = tracker(tmp_path)
    path = board.create(**PARAMS)
    board.update_fields('TASK-1', state='Done', description='Completed work')
    before = path.read_bytes(), path.stat().st_mtime_ns
    assert tracker(tmp_path).create(**PARAMS) == path
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize('field,value', [('title', 'Other'), ('description', 'Other'),
                                       ('state', 'In Progress'), ('creation_key', 'b' * 64),
                                       ('identifier', 'task-1'), ('labels', ['other'])])
def test_replay_refuses_changed_original_parameters(tmp_path, field, value):
    board = tracker(tmp_path)
    path = board.create(**PARAMS)
    before = path.read_bytes()
    with pytest.raises(SymphonyError):
        board.create(**{**PARAMS, field: value})
    assert path.read_bytes() == before


def test_missing_card_restores_original_generation_only(tmp_path):
    board = tracker(tmp_path)
    path = board.create(**PARAMS)
    original, _ = parse_ticket_file(path)
    board.delete('TASK-1')
    with pytest.raises(SymphonyError, match='already issued'):
        board.create(identifier='TASK-1', title='Unrelated')
    assert tracker(tmp_path).create(**PARAMS) == path
    assert parse_ticket_file(path)[0]['created_at'] == original['created_at']


def test_reservation_before_failed_card_write_can_be_resumed(tmp_path, monkeypatch):
    board = tracker(tmp_path)
    write = file_module.write_ticket_atomic
    with monkeypatch.context() as patch:
        patch.setattr(file_module, 'write_ticket_atomic', lambda *args: (_ for _ in ()).throw(OSError('power loss')))
        with pytest.raises(OSError, match='power loss'):
            board.create(**PARAMS)
    assert 'task-1' in json.loads((tmp_path / LEDGER_NAME).read_text())['issued']
    assert not (tmp_path / 'TASK-1.md').exists()
    assert file_module.write_ticket_atomic is write
    assert tracker(tmp_path).create(**PARAMS).is_file()


@pytest.mark.parametrize('deleted', [False, True])
def test_old_reservation_cannot_be_adopted_by_a_creation_key(tmp_path, deleted):
    board = tracker(tmp_path)
    board.create(identifier='TASK-1', title='Original')
    if deleted:
        board.delete('TASK-1')
    with pytest.raises(SymphonyError):
        board.create(**PARAMS)


@pytest.mark.parametrize('key', ['', 'a', 'A' * 64, 'x' * 64, None])
def test_invalid_or_missing_key_cannot_release_a_card(tmp_path, key):
    board = tracker(tmp_path)
    board.create(**PARAMS)
    with pytest.raises(SymphonyError):
        board.release_created('TASK-1', key, 'In Progress')


def test_release_is_atomic_and_replay_preserves_later_state(tmp_path):
    board = tracker(tmp_path)
    path = board.create(**PARAMS)
    board.release_created('TASK-1', KEY, 'In Progress')
    front, _ = parse_ticket_file(path)
    assert front['state'] == 'In Progress' and front['creation_release']['key'] == KEY
    board.update_fields('TASK-1', state='Subtasks', description='Paused by operator')
    before = path.read_bytes()
    tracker(tmp_path).release_created('TASK-1', KEY, 'In Progress')
    assert path.read_bytes() == before
    with pytest.raises(SymphonyError, match='different parameters'):
        board.release_created('TASK-1', KEY, 'Done')


@pytest.mark.parametrize('field,value', [('state', 'Done'), ('title', 'Changed'),
                                       ('description', 'Changed scope'), ('request', 'OTHER-1')])
def test_release_refuses_staged_card_edits(tmp_path, field, value):
    board = tracker(tmp_path)
    path = board.create(**PARAMS)
    board.update_fields('TASK-1', **{field: value})
    before = path.read_bytes()
    with pytest.raises(SymphonyError, match='changed before release'):
        board.release_created('TASK-1', KEY, 'In Progress')
    assert path.read_bytes() == before


@pytest.mark.parametrize('operation', ['create', 'release'])
def test_replaced_generation_is_not_overwritten(tmp_path, operation):
    board = tracker(tmp_path)
    path = board.create(**PARAMS)
    front, body = parse_ticket_file(path)
    front['created_at'] = '2000-01-01T00:00:00Z'
    write_ticket_atomic(path, front, body)
    before = path.read_bytes()
    with pytest.raises(SymphonyError, match='identity mismatch'):
        if operation == 'create':
            board.create(**PARAMS)
        else:
            board.release_created('TASK-1', KEY, 'In Progress')
    assert path.read_bytes() == before


def test_creation_key_requires_explicit_non_pm_identity(tmp_path):
    board = tracker(tmp_path)
    with pytest.raises(SymphonyError, match='explicit non-PM'):
        board.create_validated(identifier=None, title='Automatic', creation_key=KEY)
    reference = PlanningRef(change_id='c', task_id='t', task_revision='r', handoff_id='h', snapshot_id='s')
    with pytest.raises(SymphonyError, match='explicit non-PM'):
        board.create(**PARAMS, planning_ref=reference)


def test_cli_staging_uses_matching_workflow_and_returns_replay_json(tmp_path, capsys):
    root = tmp_path / 'kanban'
    workflow = tmp_path / 'WORKFLOW.md'
    workflow.write_text('---\ntracker:\n  kind: file\n  board_root: ./kanban\n'
                        '  active_states: [In Progress]\n  terminal_states: [Subtasks, Done]\n---\n')
    args = ['new', 'TASK-1', 'Original', '--root', str(root), '--workflow', str(workflow),
            '--creation-key', KEY, '--staged', '--state', 'Subtasks', '--json']
    assert main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first['identifier'] == 'TASK-1' and first['front']['state'] == 'Subtasks'
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out) == first
    assert main(['release', 'TASK-1', '--root', str(root), '--workflow', str(workflow),
                 '--creation-key', KEY, '--state', 'In Progress']) == 0
    capsys.readouterr()
    assert parse_ticket_file(root / 'TASK-1.md')[0]['state'] == 'In Progress'
    assert main([*args[:-1], '--state', 'In Progress']) == 1
    assert 'non-dispatched' in capsys.readouterr().err


def test_root_override_does_not_borrow_another_boards_states(tmp_path, capsys):
    workflow = tmp_path / 'WORKFLOW.md'
    workflow.write_text('---\ntracker:\n  kind: file\n  board_root: ./configured\n'
                        '  terminal_states: [Subtasks, Done]\n---\n')
    root = tmp_path / 'different'
    assert main(['new', 'TASK-1', 'Staged', '--root', str(root), '--workflow', str(workflow),
                 '--state', 'Subtasks', '--staged', '--creation-key', KEY]) == 1
    assert not (root / 'TASK-1.md').exists()
    assert 'unknown state' in capsys.readouterr().err


def test_standalone_root_command_works_after_its_cwd_was_reaped(tmp_path):
    root = tmp_path / 'board'
    board = tracker(root)
    path = board.create(**PARAMS)
    workspace = tmp_path / 'reaped-workspace'
    workspace.mkdir()
    script = '''import os, pathlib, sys
from symphony.cli.board import main
workspace, root = sys.argv[1:]
os.chdir(workspace)
pathlib.Path(workspace).rmdir()
sys.exit(main(['mv', 'TASK-1', 'Done', '--root', root]))
'''
    process = subprocess.run([sys.executable, '-c', script, str(workspace), str(root)],
                             capture_output=True, text=True, timeout=10)
    assert process.returncode == 0, process.stderr
    assert parse_ticket_file(path)[0]['state'] == 'Done'


def test_interruption_after_release_write_does_not_reset_progress_on_replay(tmp_path, monkeypatch):
    board = tracker(tmp_path)
    path = board.create(**PARAMS)
    with monkeypatch.context() as patch:
        patch.setattr(file_module, '_sync_ticket', lambda *args: (_ for _ in ()).throw(OSError('lost reply')))
        with pytest.raises(OSError, match='lost reply'):
            board.release_created('TASK-1', KEY, 'In Progress')
    board.update_fields('TASK-1', state='Done', description='Finished during restart')
    before = path.read_bytes()
    tracker(tmp_path).release_created('TASK-1', KEY, 'In Progress')
    assert path.read_bytes() == before


@pytest.mark.parametrize('field,value', [('created_at', 'invalid'), ('input_hash', 'oops'), ('key', 'oops')])
def test_malformed_receipt_stops_issuance(tmp_path, field, value):
    board = tracker(tmp_path)
    board.create(**PARAMS)
    path = tmp_path / LEDGER_NAME
    data = json.loads(path.read_text())
    data['creations']['task-1'][field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(SymphonyError, match='cannot read identifier ledger'):
        board.create(identifier='TASK-2', title='Next')
