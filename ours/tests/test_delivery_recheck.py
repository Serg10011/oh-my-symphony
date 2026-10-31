"""S4-28: real detached checkouts, merge commits and scripts; a local bare GitHub."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_deliver import CARD, TASK, Env, gate, git, merged_file, put_file, write

OURS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OURS / "hooks"))
import check  # noqa: E402
import deliver  # noqa: E402
import project_config as pc  # noqa: E402
import project_trial  # noqa: E402

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the hooks are POSIX-only")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in [k for k in os.environ if k.startswith("SYMPHONY_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("SYMPHONY_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    world = Env(tmp_path)
    config = world.board / "project.toml"
    config.write_text(config.read_text().replace('/bin/symphony', str(Path(sys.executable).with_name('symphony'))))
    world.trials = tmp_path / "trials"
    world.trials.mkdir()
    monkeypatch.setattr(project_trial.tempfile, "tempdir", str(world.trials))
    world.runs = world.board / "runs.txt"
    write(world.board / "setup.sh", f"echo setup >> {shlex.quote(str(world.runs))}\n")
    write(world.board / "test.sh", f"echo test >> {shlex.quote(str(world.runs))}\n"
          "test -f feature.txt || exit 23\necho 'PIPELINE_TESTS passed=7 skipped=1 failed=0'\n")
    return world


def baseline(env, passed=5, skipped=1, sha=None):
    measure = {"base_branch": "main", "base_sha": sha or git(env.code, "rev-parse", "main"),
               "passed": passed, "skipped": skipped, "failed": 0,
               "setup_s": 0.1, "tests_s": 0.1, "measured_at": "2026-10-31T00:00:00Z"}
    write(env.board / pc.STATE_DIR / "baseline.json", json.dumps(measure))
    write(pc.baselines_path(env.board), pc.baselines_text([measure]))
    return measure


def move_base(env, path="base.txt", text="base\n"):
    tip = put_file(env.code, "main", path, text)
    git(env.code, "push", "-q", str(env.github), "main")
    baseline(env)
    return tip


def clean_trials(env):
    assert list(env.trials.iterdir()) == []
    assert "ours-deliver-" not in git(env.code, "worktree", "list", "--porcelain")


def stopped(env, before, card_id=CARD):
    assert env.push(card_id) == deliver.EXIT_REFUSED
    assert env.state(card_id) == "needs_human"
    assert not env.pushes() and not env.creates()
    assert env.snapshot() == before
    assert not deliver.delivered_path(env.board, card_id).exists()
    assert env.events()[-1]["outcome"] == "needs_human"
    clean_trials(env)


@pytest.mark.parametrize("task", [False, True])
def test_moved_base_is_checked_and_delivered_as_two_parent_commit(env, task):
    source = env.task() if task else env.ordinary()
    ident = TASK if task else CARD
    base = move_base(env)
    previous = env.snapshot()
    original_baseline = (env.board / pc.STATE_DIR / "baseline.json").read_bytes()
    write(env.board / "test.sh", "test -f base.txt && test -f feature.txt || exit 23\n"
          f"echo test >> {shlex.quote(str(env.runs))}\necho 'PIPELINE_TESTS passed=7 skipped=1 failed=0'\n")
    assert env.push(ident) == deliver.EXIT_OK
    record = env.record(ident)
    target = record["commit"]
    assert git(env.code, "show", "-s", "--format=%P", target).split() == [source, base]
    assert env.remote_branch(f"deliver/{ident}") == target
    assert record["source_commit"] == source and record["recheck"] is True
    assert record["source_tree"] == git(env.code, "rev-parse", f"{source}^{{tree}}")
    assert record["measure"]["comparison_base"] == base
    assert record["measure"]["passed"] == 7
    assert "повторная проверка: полный прогон" in env.creates()[0]["body"]
    assert "повторной проверки не было" not in env.creates()[0]["body"]
    assert env.runs.read_text().splitlines() == ["setup", "test"]
    assert env.snapshot() == previous
    assert (env.board / pc.STATE_DIR / "baseline.json").read_bytes() == original_baseline
    history = pc.read_baselines(env.board)
    assert len(history) == 2 and history[-1]["base_sha"] == target and history[0]["base_sha"] == base
    assert env.state(ident) == "Human Review"
    clean_trials(env)


@pytest.mark.parametrize("task", [False, True])
def test_doc_only_proof_runs_full_scripts_even_when_base_did_not_move(env, task):
    source = env.task(tests="skipped-docs") if task else env.ordinary(tests="skipped-docs")
    ident = TASK if task else CARD
    baseline(env)
    previous = env.snapshot()
    assert env.push(ident) == deliver.EXIT_OK
    assert env.record(ident)["commit"] == source and env.record(ident)["recheck"]
    assert env.runs.read_text().splitlines() == ["setup", "test"]
    assert env.snapshot() == previous
    clean_trials(env)


def test_full_green_proof_on_same_base_keeps_the_existing_fast_path(env):
    source = env.ordinary()
    assert env.push() == deliver.EXIT_OK
    assert env.record()["commit"] == source and not env.record()["recheck"]
    assert not env.runs.exists()
    assert "повторной проверки не было" in env.creates()[0]["body"]
    clean_trials(env)


@pytest.mark.parametrize("task", [False, True])
def test_conflict_goes_to_human_without_checkout_or_push(env, task):
    if task:
        env.task()
        source = f"integration/{TASK}"
    else:
        env.ordinary()
        source = f"symphony/{CARD}"
    ident = TASK if task else CARD
    move_base(env, "feature.txt", "a conflicting feature\n")
    before = env.snapshot()
    stopped(env, before, ident)
    assert "feature.txt" in env.card_text(ident) and "конфликт" in env.card_text(ident)
    assert not env.runs.exists()
    assert git(env.code, "rev-parse", source) in before["refs"]


@pytest.mark.parametrize(("script", "content", "reason"), [
    ("setup.sh", "echo setup-failed\nexit 8\n", "setup.sh: код 8"),
    ("test.sh", "echo test-failed\nexit 9\n", "test.sh: код 9"),
    ("test.sh", "echo no-summary\n", "нет строки итогов"),
    ("test.sh", "echo 'PIPELINE_TESTS passed=7 skipped=1 failed=1'\n", "failed=1"),
    ("test.sh", "echo 'PIPELINE_TESTS passed=4 skipped=1 failed=0'\n", "меньше базы"),
    ("test.sh", "echo 'PIPELINE_TESTS passed=7 skipped=2 failed=0'\n", "больше базы"),
])
def test_failed_recheck_stops_and_keeps_history_and_refs(env, script, content, reason):
    env.ordinary()
    move_base(env)
    history = pc.baselines_path(env.board).read_bytes()
    write(env.board / script, content)
    before = env.snapshot()
    stopped(env, before)
    assert reason in env.card_text()
    assert pc.baselines_path(env.board).read_bytes() == history


def test_forbidden_file_is_rechecked_before_running_any_script(env):
    env.ordinary()
    put_file(env.code, f"symphony/{CARD}", ".env", "FAKE_TOKEN=value\n")
    env.event(CARD, f"symphony/{CARD}")
    move_base(env)
    stopped(env, env.snapshot())
    assert ".env" in env.card_text() and not env.runs.exists()


def test_existing_tests_are_compared_with_the_new_base(env):
    put_file(env.code, "main", "test_existing.py", "original\n")
    git(env.code, "push", "-q", str(env.github), "main")
    env.ordinary()
    put_file(env.code, f"symphony/{CARD}", "test_existing.py", "changed\n")
    env.event(CARD, f"symphony/{CARD}")
    move_base(env)
    stopped(env, env.snapshot())
    assert "existing test changed" in env.card_text() and not env.runs.exists()


def test_source_secret_never_runs_scripts_or_reaches_a_push(env, capsys):
    env.ordinary()
    secret = "sk-ant-api03-" + "x" * 60
    put_file(env.code, f"symphony/{CARD}", "secret.txt", secret)
    env.event(CARD, f"symphony/{CARD}")
    move_base(env)
    assert env.push() == deliver.EXIT_REFUSED
    assert secret not in capsys.readouterr().out
    assert not env.pushes() and not env.creates() and not env.runs.exists()
    clean_trials(env)


def test_failure_tail_redacts_api_keys(env, capsys):
    env.ordinary()
    move_base(env)
    secret = "sk-ant-api03-" + "x" * 60
    write(env.board / "test.sh", f"echo '{secret}'\nexit 1\n")
    stopped(env, env.snapshot())
    assert secret not in env.card_text() and secret not in capsys.readouterr().out
    assert "<API key hidden>" in env.card_text()


@pytest.mark.parametrize("script", ["setup.sh", "test.sh"])
def test_mutation_of_checked_content_is_never_published(env, script):
    env.ordinary()
    move_base(env)
    write(env.board / script, "echo changed >> feature.txt\necho 'PIPELINE_TESTS passed=7 skipped=1 failed=0'\n")
    stopped(env, env.snapshot())
    assert "дерево" in env.card_text()


@pytest.mark.parametrize("mutation", ["source", "base", "remote", "card", "proof", "config"])
def test_concurrent_changes_after_test_are_detected_before_publish(env, monkeypatch, mutation):
    env.ordinary()
    move_base(env)
    original = project_trial.run_script

    def run(*args):
        result = original(*args)
        if args[0].name == "test.sh":
            if mutation == "source":
                put_file(env.code, f"symphony/{CARD}", "late.txt", "late\n")
            elif mutation == "base":
                put_file(env.code, "main", "late.txt", "late\n")
            elif mutation == "remote":
                moved = put_file(env.code, "main", "late.txt", "late\n")
                git(env.code, "push", "-q", str(env.github), "main")
                git(env.code, "update-ref", "refs/heads/main", git(env.code, "rev-parse", f"{moved}^"))
            elif mutation == "card":
                env.card(CARD, env.card_text().replace("Human Review", "Blocked"))
            elif mutation == "proof":
                check.log_event(env.board / pc.STATE_DIR, CARD, "gate", "fail", "concurrent failure")
            elif mutation == "config":
                p = env.board / "project.toml"
                p.write_text(p.read_text() + "\n# edited\n")
        return result

    monkeypatch.setattr(project_trial, "run_script", run)
    assert env.push() == deliver.EXIT_REFUSED
    assert not env.pushes() and not env.creates()
    assert not deliver.delivered_path(env.board, CARD).exists()
    clean_trials(env)


def test_retry_after_pr_failure_reuses_verified_merge_without_running_again(env):
    env.ordinary()
    move_base(env)
    env.behave(create_fail=True)
    assert env.push() == deliver.EXIT_ERROR
    target = env.record()["commit"]
    assert env.record()["recheck"] and env.record()["pr"] is None
    runs = env.runs.read_bytes()
    env.behave(number=43)
    assert env.push() == deliver.EXIT_OK
    assert env.record()["commit"] == target and env.record()["pr"]["number"] == 43
    assert len(env.pushes()) == 1 and env.runs.read_bytes() == runs
    clean_trials(env)


def test_retry_refuses_when_base_moved_after_the_published_merge(env):
    env.ordinary()
    move_base(env)
    env.behave(create_fail=True)
    assert env.push() == deliver.EXIT_ERROR
    target = env.record()["commit"]
    move_base(env, "later.txt", "later\n")
    assert env.push() == deliver.EXIT_REFUSED
    assert env.remote_branch(f"deliver/{CARD}") == target
    assert len(env.pushes()) == 1
    clean_trials(env)


@pytest.mark.parametrize("task", [False, True])
def test_done_accepts_unchanged_original_tree_via_delivery_provenance(env, task):
    source = env.task() if task else env.ordinary()
    ident = TASK if task else CARD
    original_base = git(env.code, "rev-parse", "main")
    move_base(env)
    write(pc.base_record_path(env.board, ident), json.dumps(
        {"base_sha": original_base, "base_branch": "main", "created_at": "2026-10-31T00:00:00Z"}))
    assert env.push(ident) == deliver.EXIT_OK
    delivered = env.record(ident)["commit"]
    assert git(env.code, "rev-parse", f"{source}^{{tree}}") != git(env.code, "rev-parse", f"{delivered}^{{tree}}")
    assert env.merged(ident) == deliver.EXIT_OK
    mark = json.loads(merged_file(env, ident).read_text())
    assert mark["commit"] == delivered and mark["source_commit"] == source
    runs = env.runs.read_bytes()
    result = gate(env, ident)
    assert result.ok and result.tests == "skipped", result.reasons
    assert env.runs.read_bytes() == runs and not merged_file(env, ident).exists()


@pytest.mark.parametrize("field", ["source_tree", "source_commit", "tree", "commit"])
def test_done_refuses_tampered_delivery_provenance(env, field):
    env.ordinary()
    move_base(env)
    assert env.push() == deliver.EXIT_OK
    assert env.merged() == deliver.EXIT_OK
    path = merged_file(env, CARD)
    record = json.loads(path.read_text())
    record[field] = "a" * 40
    note, reason = check._merged_skip(check.Context("gate", env.board, env.ws_root / CARD, CARD), record)
    assert note is None and reason


def test_done_refuses_source_edits_after_delivery(env):
    env.ordinary()
    move_base(env)
    assert env.push() == deliver.EXIT_OK
    assert env.merged() == deliver.EXIT_OK
    write(env.ws_root / CARD / "feature.txt", "changed after delivery\n")
    mark = json.loads(merged_file(env, CARD).read_text())
    note, reason = check._merged_skip(check.Context("gate", env.board, env.ws_root / CARD, CARD), mark)
    assert note is None and "differs" in reason


def test_done_also_accepts_the_exact_checked_delivery_tree(env):
    env.ordinary()
    move_base(env)
    assert env.push() == deliver.EXIT_OK
    assert env.merged() == deliver.EXIT_OK
    git(env.ws_root / CARD, "checkout", "--detach", env.record()['commit'])
    mark = json.loads(merged_file(env, CARD).read_text())
    note, reason = check._merged_skip(check.Context("gate", env.board, env.ws_root / CARD, CARD), mark)
    assert note and not reason


def test_script_timeout_kills_child_and_removes_checkout(env, monkeypatch):
    env.ordinary()
    move_base(env)
    marker = env.board / "child.pid"
    write(env.board / "test.sh", f"/bin/sleep 60 &\necho $! > {shlex.quote(str(marker))}\nwait\n")
    monkeypatch.setattr(project_trial, "SCRIPT_LIMIT_S", 0.4)
    stopped(env, env.snapshot())
    assert "таймаут" in env.card_text()
    child = int(marker.read_text())
    deadline = time.monotonic() + 3
    while True:
        p = subprocess.run(['ps', '-o', 'stat=', '-p', str(child)],capture_output=True,text=True)
        if not p.stdout.strip() or p.stdout.strip().startswith('Z'):
            break
        assert time.monotonic() < deadline, "a test child survived the timeout"
        time.sleep(0.05)


def test_runner_preserves_doctor_environment_and_kills_stragglers(tmp_path):
    board = tmp_path / "board"
    board.mkdir()
    env = project_trial.script_env(sys.executable, board)
    assert env['PATH'] == '/usr/bin:/bin:/usr/sbin:/sbin'
    assert env['SYMPHONY_PYTHON'] == sys.executable and env['SYMPHONY_WORKFLOW_DIR'] == str(board)
    assert not any(key in env for key in ['ANTHROPIC_API_KEY','GH_TOKEN','GITHUB_TOKEN'])
    marker = tmp_path / "straggler.pid"
    script = write(board / "test.sh", f"/bin/sleep 60 &\necho $! > {shlex.quote(str(marker))}\n"
                   "echo 'PIPELINE_TESTS passed=7 skipped=1 failed=0'\n")
    code, _, _, summary = project_trial.run_script(script, tmp_path, env, 3)
    assert code == 0 and summary == {'passed':7,'skipped':1,'failed':0}
    child = int(marker.read_text())
    try:
        os.kill(child, 0)
    except ProcessLookupError:
        return
    proc = subprocess.run(['ps','-o','stat=','-p',str(child)],capture_output=True,text=True)
    assert not proc.stdout.strip() or proc.stdout.strip().startswith('Z'), 'a background process survived a successful script'


def test_unknown_check_proof_still_refuses_without_recheck(env):
    env.ordinary(tests="skipped")
    baseline(env)
    assert env.push() == deliver.EXIT_REFUSED
    assert not env.pushes() and not env.runs.exists()
    clean_trials(env)


def test_measure_uses_compatible_ancestor_of_current_base_not_unrelated_latest_record(env):
    env.ordinary()
    old = baseline(env)
    move_base(env)
    other = {**old, 'base_sha': 'a' * 40, 'passed': 999}
    write(pc.baselines_path(env.board), pc.baselines_text([old, other]))
    assert env.push() == deliver.EXIT_OK
    measure = env.record()['measure']
    assert measure['comparison_match'] == 'ancestor' and measure['comparison_sha'] == old['base_sha']
    assert measure['passed'] == 7
    clean_trials(env)


def test_failed_recheck_does_not_overwrite_a_concurrent_human_card_edit(env, monkeypatch):
    env.ordinary()
    move_base(env)
    original = project_trial.run_script

    def run(*args):
        result = original(*args)
        if args[0].name == 'test.sh':
            env.card(CARD, env.card_text().replace('Human Review','Blocked'))
            return 8, result[1], 'failed after user edit', None
        return result

    monkeypatch.setattr(project_trial,'run_script',run)
    assert env.push() == deliver.EXIT_REFUSED
    assert env.state() == 'Blocked' and not env.pushes()
    clean_trials(env)


def test_resume_refuses_a_different_candidate_without_test_evidence(env):
    env.ordinary()
    move_base(env)
    env.behave(create_fail=True)
    assert env.push() == deliver.EXIT_ERROR
    record = env.record()
    record['measure']['base_sha'] = record['source_commit']
    deliver.delivered_path(env.board,CARD).write_text(json.dumps(record))
    assert env.push() == deliver.EXIT_REFUSED
    assert len(env.pushes()) == 1
    clean_trials(env)


@pytest.mark.parametrize('doctor',[False,True])
def test_doctor_and_delivery_history_writers_share_a_process_lock(env, doctor):
    old = baseline(env)
    measure = {**old,'base_sha': 'b' * 40,'passed': 7}
    marker = env.board/'writer-ready'
    script = f'''import json,sys
from pathlib import Path
sys.path.insert(0,{str(OURS / 'hooks')!r})
import project_trial
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec,spec_from_loader
loader=SourceFileLoader('doctor_history',{str(OURS / 'bin/project-doctor')!r})
doctor=module_from_spec(spec_from_loader(loader.name,loader))
loader.exec_module(doctor)
board=Path({str(env.board)!r})
measure=json.loads({json.dumps(measure)!r})
Path({str(marker)!r}).touch()
if {doctor!r}:
    doctor.update_history(doctor.Report(),board,measure)
else:
    project_trial.record_measure(board,measure)
'''
    proc = None
    try:
        with project_trial.history_lock(env.board):
            proc = subprocess.Popen([sys.executable,'-B','-c',script],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            deadline = time.monotonic()+5
            while not marker.exists():
                assert proc.poll() is None
                assert time.monotonic() < deadline
                time.sleep(0.02)
            time.sleep(0.15)
            assert proc.poll() is None
            assert pc.read_baselines(env.board) == [old]
        out, err = proc.communicate(timeout=5)
        assert proc.returncode == 0, (out,err)
        assert pc.read_baselines(env.board) == [old,measure]
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)


def test_repeated_delivery_checks_do_not_evict_base_measurements():
    def measure(n):
        return {'base_branch':'main','base_sha':f'{n:040x}','passed':n,'skipped':0,'failed':0}

    bases = [measure(n) for n in range(1,51)]
    records = list(bases)
    for n in range(1001,1061):
        records = pc.add_baseline(records,{**measure(n),'delivery':'OMS-12','comparison_base':bases[-1]['base_sha']})
    assert len(records) == 100
    assert [r for r in records if not r.get('delivery')] == bases
    assert [r['passed'] for r in records if r.get('delivery')] == list(range(1011,1061))
    new_base = measure(61)
    records = pc.add_baseline(records,new_base)
    assert [r for r in records if not r.get('delivery')] == [*bases[1:],new_base]
    assert len([r for r in records if r.get('delivery')]) == 50


def test_doctor_can_replace_a_candidate_with_a_measure_of_the_same_merged_commit():
    base = {'base_branch':'main','base_sha':'a'*40,'passed':5,'skipped':0,'failed':0}
    candidate = {**base,'base_sha':'b'*40,'passed':6,'delivery':'OMS-12','comparison_base':base['base_sha']}
    measured_after_merge = {k:v for k,v in candidate.items() if k not in ('delivery','comparison_base')}
    records = pc.add_baseline([base,candidate],measured_after_merge)
    assert records == [base,measured_after_merge]
