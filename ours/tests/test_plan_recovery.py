"""Ordinary-plan recovery across durable-write and activation boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_plan_hook import (BREAK_ON_SECOND, PLAN, REAL_SYMPHONY, TASK, Env,
                            git, plan, plan_text, sub, write)

BREAK_ON_RELEASE_TWO = '''#!{python}
import os, sys
args = sys.argv[1:]
if args[:3] == ['board', 'release', 'OMS-9-2']:
    print('interrupted activation', file=sys.stderr)
    sys.exit(1)
os.execv({real!r}, [{real!r}, *args])
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in [key for key in os.environ if key.startswith('SYMPHONY_')]:
        monkeypatch.delenv(key)
    result = Env(tmp_path)
    monkeypatch.setenv('SYMPHONY_WORKFLOW_DIR', str(result.board))
    monkeypatch.setenv('SYMPHONY_ISSUE_ID', TASK)
    monkeypatch.setenv('SYMPHONY_PYTHON', sys.executable)
    return result


def journal(env: Env) -> Path:
    return env.board / '.ours-state' / f'{TASK}.plan-create.json'


def start_partial(env: Env, monkeypatch) -> None:
    env.use_symphony(BREAK_ON_SECOND)
    env.task_card()
    env.plan(plan_text(sub('a'), sub('b'), sub('c')))
    assert env.hook(monkeypatch) == 0
    assert env.cards() == ['OMS-9-1'] and env.state('OMS-9-1') == 'Subtasks'
    assert env.state() == 'needs_human'


def repair_binary(env: Env) -> None:
    env.symphony = str(REAL_SYMPHONY)
    env.write_project()


def test_no_child_is_runnable_until_the_whole_batch_and_notes_exist(env: Env, monkeypatch):
    env.use_symphony('''#!{python}
import os, pathlib, sys
args = sys.argv[1:]
root = pathlib.Path(args[args.index('--root') + 1])
if args[:2] == ['board', 'new'] and args[2] != 'OMS-9-1':
    for card in root.glob('OMS-9-*.md'):
        assert 'state: Subtasks' in card.read_text()
    assert '## Plan Accepted' not in (root / 'OMS-9.md').read_text()
if args[:2] == ['board', 'release']:
    assert len(list(root.glob('OMS-9-*.md'))) == 3
    body = (root / 'OMS-9.md').read_text()
    assert '## Plan Accepted' in body and '## Subtask Progress' in body
os.execv({real!r}, [{real!r}, *args])
''')
    env.task_card()
    env.plan(plan_text(sub('a'), sub('b'), sub('c')))
    env.hook(monkeypatch)
    assert len(env.cards()) == 3 and all(env.state(sid) == 'In Progress' for sid in env.cards())


def test_lost_cli_reply_replays_the_card_already_created(env: Env, monkeypatch):
    env.use_symphony('''#!{python}
import os, subprocess, sys
args = sys.argv[1:]
if args[:3] == ['board', 'new', 'OMS-9-1']:
    subprocess.run([{real!r}, *args], check=True)
    os._exit(19)
os.execv({real!r}, [{real!r}, *args])
''')
    env.task_card()
    env.plan(plan_text(sub('a'), sub('b')))
    env.hook(monkeypatch)
    original = plan._front_value(env.card_path('OMS-9-1'), 'created_at')
    repair_binary(env)
    assert plan.recover_main(TASK, env.board) == 0
    assert plan._front_value(env.card_path('OMS-9-1'), 'created_at') == original
    assert len(env.cards()) == 2


def test_failed_initial_journal_write_creates_no_cards(env: Env, monkeypatch):
    env.task_card()
    env.plan(plan_text(sub()))
    monkeypatch.setattr(plan, 'write_journal', lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    env.hook(monkeypatch)
    assert env.cards() == [] and not journal(env).exists()
    assert env.state() == 'needs_human' and env.counter() is None


@pytest.mark.parametrize('heading', [plan.NOTE_ACCEPTED, plan.NOTE_PROGRESS])
def test_note_write_failure_keeps_every_child_staged_and_can_resume(env: Env, monkeypatch, heading):
    env.task_card()
    env.plan(plan_text(sub('a'), sub('b')))
    original_note = plan._note
    with monkeypatch.context() as patch:
        patch.setattr(plan, '_note', lambda ctx, name, lines: 'failed' if name == heading else original_note(ctx, name, lines))
        env.hook(monkeypatch)
    assert all(env.state(sid) == 'Subtasks' for sid in env.cards()) and len(env.cards()) == 2
    assert env.state() == 'needs_human'
    assert plan.recover_main(TASK, env.board) == 0
    assert env.body().count('## Plan Accepted') == 1
    assert env.body().count('## Subtask Progress') == 1


def test_failure_to_commit_the_journal_never_activates_children(env: Env, monkeypatch):
    env.task_card()
    env.plan(plan_text(sub('a'), sub('b')))
    original_write = plan.write_journal
    with monkeypatch.context() as patch:
        def interrupted(ctx, data):
            if data['phase'] == 'committed':
                raise OSError('interrupted commit')
            original_write(ctx, data)
        patch.setattr(plan, 'write_journal', interrupted)
        env.hook(monkeypatch)
    assert all(env.state(sid) == 'Subtasks' for sid in env.cards())
    assert json.loads(journal(env).read_text())['payload']['phase'] == 'creating'
    assert plan.recover_main(TASK, env.board) == 0
    assert env.body().count('## Plan Accepted') == 1


def test_partial_activation_preserves_a_completed_worker_on_recovery(env: Env, monkeypatch):
    env.use_symphony(BREAK_ON_RELEASE_TWO)
    env.task_card()
    env.plan(plan_text(sub('a'), sub('b'), sub('c')))
    env.hook(monkeypatch)
    assert env.state('OMS-9-1') == 'In Progress' and env.state('OMS-9-2') == 'Subtasks'
    first = env.card_path('OMS-9-1')
    first.write_text(first.read_text().replace('state: In Progress', 'state: Done', 1) + '\n## Worker result\nDone\n')
    before = first.read_bytes()
    repair_binary(env)
    assert plan.recover_main(TASK, env.board) == 0
    assert first.read_bytes() == before
    assert env.state('OMS-9-2') == env.state('OMS-9-3') == 'In Progress'


@pytest.mark.parametrize('change', ['body', 'request', 'plan', 'project', 'branch', 'extra-card', 'parent-body'])
def test_changed_operation_stops_without_adopting_or_overwriting_cards(env: Env, monkeypatch, change):
    start_partial(env, monkeypatch)
    repair_binary(env)
    first = env.card_path('OMS-9-1')
    if change == 'body':
        first.write_text(first.read_text() + '\nOperator edit\n')
    elif change == 'request':
        first.write_text(first.read_text().replace('request: OMS-9', 'request: OTHER-1'))
    elif change == 'plan':
        env.plan(plan_text(sub('a', title='Different'), sub('b'), sub('c')))
    elif change == 'project':
        cfg = env.board / 'project.toml'
        cfg.write_text(cfg.read_text().replace('base_branch = "main"', 'base_branch = "other"'))
    elif change == 'branch':
        git(env.code, 'checkout', '--orphan', 'other')
        git(env.code, 'commit', '--allow-empty', '-qm', 'unrelated')
        git(env.code, 'branch', '-f', 'integration/OMS-9', 'HEAD')
    elif change == 'parent-body':
        parent = env.card_path(TASK)
        parent.write_text(parent.read_text().replace('task body', 'Different task goal'))
    else:
        write(env.card_path('OMS-9-4'), '---\nid: OMS-9-4\nstate: Todo\n---\nForeign\n')
    before = first.read_bytes()
    assert plan.recover_main(TASK, env.board) == 1
    assert first.read_bytes() == before
    assert not env.card_path('OMS-9-2').exists()


def test_deleted_staged_child_can_be_restored_as_the_same_operation(env: Env, monkeypatch):
    start_partial(env, monkeypatch)
    first = env.card_path('OMS-9-1')
    original_created = plan._front_value(first, 'created_at')
    first.unlink()
    repair_binary(env)
    assert plan.recover_main(TASK, env.board) == 0
    assert plan._front_value(first, 'created_at') == original_created
    assert len(env.cards()) == 3


def test_interruption_after_all_releases_never_resets_a_paused_child(env: Env, monkeypatch):
    env.task_card()
    env.plan(plan_text(sub('a'), sub('b')))
    original_write = plan.write_journal
    with monkeypatch.context() as patch:
        def interrupted(ctx, data):
            if data['phase'] == 'done':
                raise OSError('lost completion write')
            original_write(ctx, data)
        patch.setattr(plan, 'write_journal', interrupted)
        env.hook(monkeypatch)
    assert all(env.state(sid) == 'In Progress' for sid in env.cards())
    first = env.card_path('OMS-9-1')
    first.write_text(first.read_text().replace('state: In Progress', 'state: Subtasks', 1) + '\nOperator pause\n')
    before = first.read_bytes()
    assert plan.recover_main(TASK, env.board) == 0
    assert first.read_bytes() == before


def test_snapshot_can_resume_when_original_plan_artifact_is_missing(env: Env, monkeypatch):
    start_partial(env, monkeypatch)
    repair_binary(env)
    plan.plan_file(env.board, TASK).unlink()
    assert plan.recover_main(TASK, env.board) == 0
    assert len(env.cards()) == 3


def test_missing_committed_card_is_never_recreated_over_lost_work(env: Env, monkeypatch):
    env.use_symphony(BREAK_ON_RELEASE_TWO)
    env.task_card()
    env.plan(plan_text(sub('a'), sub('b')))
    env.hook(monkeypatch)
    env.card_path('OMS-9-2').unlink()
    first = env.card_path('OMS-9-1').read_bytes()
    repair_binary(env)
    assert plan.recover_main(TASK, env.board) == 1
    assert not env.card_path('OMS-9-2').exists()
    assert env.card_path('OMS-9-1').read_bytes() == first


def test_edited_acceptance_record_stops_further_activation(env: Env, monkeypatch):
    env.use_symphony(BREAK_ON_RELEASE_TWO)
    env.task_card()
    env.plan(plan_text(sub('a'), sub('b')))
    env.hook(monkeypatch)
    parent = env.card_path(TASK)
    parent.write_text(parent.read_text().replace('- OMS-9-2 —', '- OTHER-2 —'))
    first = env.card_path('OMS-9-1').read_bytes()
    repair_binary(env)
    assert plan.recover_main(TASK, env.board) == 1
    assert env.state('OMS-9-2') == 'Subtasks'
    assert env.card_path('OMS-9-1').read_bytes() == first


@pytest.mark.parametrize('damage', ['json', 'checksum', 'symlink', 'foreign-board'])
def test_bad_journal_is_a_safe_refusal(env: Env, monkeypatch, damage):
    start_partial(env, monkeypatch)
    repair_binary(env)
    path = journal(env)
    if damage == 'json':
        path.write_text('{')
    elif damage == 'symlink':
        target = env.tmp / 'original-journal.json'
        path.rename(target)
        path.symlink_to(target)
    else:
        data = json.loads(path.read_text())
        data['payload']['board'] = str(env.tmp)
        if damage == 'foreign-board':
            data['sha256'] = plan._digest(data['payload'])
        path.write_text(json.dumps(data))
    before = env.card_path('OMS-9-1').read_bytes(), env.card_path(TASK).read_bytes()
    assert plan.recover_main(TASK, env.board) == 1
    assert (env.card_path('OMS-9-1').read_bytes(), env.card_path(TASK).read_bytes()) == before


def test_hook_can_resume_after_notes_exist_but_creation_was_not_committed(env: Env, monkeypatch):
    env.task_card()
    env.plan(plan_text(sub('a'), sub('b')))
    with monkeypatch.context() as patch:
        patch.setattr(plan, '_release_cards', lambda *args: (_ for _ in ()).throw(plan.PlanError('interrupted')))
        env.hook(monkeypatch)
    assert '## Plan Accepted' in env.body()
    parent = env.card_path(TASK)
    parent.write_text(parent.read_text().replace('state: needs_human', 'state: Subtasks'))
    env.hook(monkeypatch)
    assert all(env.state(sid) == 'In Progress' for sid in env.cards())
    assert env.body().count('## Plan Accepted') == 1


def test_legacy_partial_plan_without_journal_requires_a_human(env: Env, monkeypatch):
    start_partial(env, monkeypatch)
    journal(env).unlink()
    before = env.card_path('OMS-9-1').read_bytes()
    assert plan.recover_main(TASK, env.board) == 1
    assert env.card_path('OMS-9-1').read_bytes() == before


def test_concurrent_operator_recovery_creates_one_batch(env: Env, monkeypatch):
    start_partial(env, monkeypatch)
    repair_binary(env)
    command = [sys.executable, str(PLAN), 'recover', TASK, '--board', str(env.board)]
    first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    second = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    outputs = [process.communicate(timeout=30) for process in (first, second)]
    assert first.returncode == second.returncode == 0, outputs
    assert len(env.cards()) == 3
    assert env.body().count('## Plan Accepted') == 1


def test_operator_state_change_is_preserved(env: Env, monkeypatch):
    start_partial(env, monkeypatch)
    repair_binary(env)
    parent = env.card_path(TASK)
    parent.write_text(parent.read_text().replace('state: needs_human', 'state: Archive'))
    before = parent.read_bytes()
    assert plan.recover_main(TASK, env.board) == 1
    assert parent.read_bytes() == before


def test_staging_refuses_a_workflow_that_would_dispatch_subtasks(env: Env, monkeypatch):
    workflow = env.board / 'WORKFLOW.md'
    workflow.write_text(workflow.read_text().replace('Todo, Plan, In Progress', 'Todo, Subtasks, Plan, In Progress')
                        .replace('terminal_states: [Subtasks, ', 'terminal_states: ['))
    env.task_card()
    env.plan(plan_text(sub()))
    env.hook(monkeypatch)
    assert env.cards() == [] and env.state() == 'needs_human'
    assert 'non-dispatched' in env.body()
