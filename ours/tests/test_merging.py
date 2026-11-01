"""Host-only subtask handoff and explicit recovery on real temporary repositories."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import test_final_check as final
import test_integrate as ti
import test_light_path_e2e as light
import test_planning_states as planning

check = ti.check
integrate = ti.integrate
henv = light.henv
kit = planning.kit
ID = "OMS-12"
NOTE = "docs/our/NOTE.md"
pytestmark = pytest.mark.skipif(os.name != "posix", reason="the hooks are POSIX-only")


def pending(env: ti.Env, monkeypatch, state: str = "Merging") -> str:
    source = ti.subtask(env, ID, monkeypatch, {NOTE: "new notes\n"})
    card = env.board / "kanban" / f"{ID}.md"
    card.write_text(card.read_text().replace("state: Human Review", f"state: {state}") + light.SUBTASK_BODY)
    toml = env.board / "project.toml"
    toml.write_text(toml.read_text() + '[checks]\ndoc_paths = ["docs/our/**/*.md"]\n')
    ti.write(env.board / ".ours-state/baseline.json", json.dumps({
        "base_branch": "main", "base_sha": "0" * 40, "passed": 1, "skipped": 0, "failed": 0,
        "setup_s": 0.1, "tests_s": 0.1, "measured_at": "2026-11-01T00:00:00Z",
    }))
    ti.write(env.board / "test.sh", "echo 'PIPELINE_TESTS passed=1 skipped=0 failed=0'\n")
    ti.write(env.board / "WORKFLOW.md", ti.pc.render_workflow(env.board, ti.pc.load_project(env.board)))
    return source


def resume(env: ti.Env, ident: str = ID):
    return subprocess.run(
        [sys.executable, str(ti.INTEGRATE), "resume-subtask", ident, "--board", str(env.board)],
        env=os.environ.copy(), capture_output=True, text=True, timeout=90, check=False,
    )


def test_merging_is_inactive_does_not_resolve_dependencies_and_survives_cleanup(kit, monkeypatch):
    from symphony.issue import BlockerRef
    from symphony.orchestrator.core_support import _EligibilityDisposition as Disposition
    from symphony.workspace import WorkspaceManager

    cfg = planning.core_cfg(kit.board)
    assert "Merging" in cfg.tracker.terminal_states
    assert "Merging" not in cfg.tracker.active_states
    assert "merging" in cfg.agent.preserve_workspace_states
    assert cfg.agent.dependency_resolving_states == ("done",)
    orch = planning.make_orchestrator(cfg, monkeypatch)
    refused = orch._eligibility_contract_decision(planning.issue("Merging"), cfg)
    assert (refused.disposition, refused.code) == (Disposition.REJECT, "inactive")
    blocker = BlockerRef(id="b", identifier="b", state="Merging")
    waiting = orch._eligibility_decision(
        planning.issue("In Progress", blocked_by=(blocker,)), cfg, owning_retry=False,
    )
    assert waiting.disposition == Disposition.WAIT_NON_SLOT
    ident = f"{planning.PREFIX}-7"
    ti.write(kit.board / "kanban" / f"{ident}.md", f"---\nid: {ident}\ntitle: T\nstate: Merging\n---\nbody\n")
    manager = WorkspaceManager(cfg.workspace_root, cfg.hooks, workflow_dir=kit.board)
    orch._workspace_manager = manager
    workspace = manager.path_for(ident)
    workspace.mkdir(parents=True)
    asyncio.run(orch._startup_terminal_cleanup(cfg))
    assert workspace.is_dir()


@pytest.mark.parametrize("state", ["Merging", "Human Review"])
def test_actual_after_run_checks_commits_merges_and_done_gate_reuses_proof(henv, monkeypatch, state):
    from symphony.workflow import build_service_config, load_workflow

    pending(henv, monkeypatch, state)
    # Leave one more edit for the real after_run commit block.
    ti.write(henv.ws(ID) / NOTE, "final notes\n")
    hook = build_service_config(load_workflow(henv.board / "WORKFLOW.md")).hooks.after_run
    env = {**os.environ, "SYMPHONY_WORKFLOW_DIR": str(henv.board), "SYMPHONY_PYTHON": sys.executable}
    proc = subprocess.run(["/bin/bash", "-c", hook], cwd=henv.ws(ID), env=env,
                          capture_output=True, text=True, timeout=90, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert ti.card_state(henv, ID) == "Done"
    assert ti.git(henv.code, "show", f"{ti.BRANCH}:{NOTE}") == "final notes"
    gate = subprocess.run([sys.executable, str(Path(check.__file__)), "gate"], cwd=henv.ws(ID),
                          env=env, capture_output=True, text=True, timeout=90, check=False)
    assert gate.returncode == 0, gate.stdout + gate.stderr
    assert "tests skipped: tree" in gate.stdout


def test_conflict_from_merging_leaves_integration_tip_and_requests_human(henv, monkeypatch):
    pending(henv, monkeypatch)
    other = henv.tmp / "integration-copy"
    ti.git(henv.code, "worktree", "add", str(other), ti.BRANCH)
    ti.write(other / NOTE, "conflicting notes\n")
    ti.git(other, "add", "-A")
    ti.git(other, "commit", "-m", "Conflicting notes")
    old = ti.git(other, "rev-parse", "HEAD")
    ti.git(henv.code, "worktree", "remove", str(other))
    assert ti.run_hook(henv, ID, monkeypatch) == 0
    assert ti.card_state(henv, ID) == "needs_human"
    assert ti.git(henv.code, "rev-parse", ti.BRANCH) == old
    assert "result: conflict" in ti.card_body(henv, ID)


def test_failed_hook_checks_never_merge_or_mark_done(henv, monkeypatch):
    pending(henv, monkeypatch)
    ti.mark_path(henv, ID).unlink()
    ti.review_card(henv, ID, state="Merging")  # Missing required evidence.
    old = ti.git(henv.code, "rev-parse", ti.BRANCH)
    assert check.hook_main() == 0
    assert ti.run_hook(henv, ID, monkeypatch) == 0
    assert ti.git(henv.code, "rev-parse", ti.BRANCH) == old
    assert ti.card_state(henv, ID) in {"In Progress", "needs_human"}
    assert not ti.mark_path(henv, ID).exists()


@pytest.mark.parametrize("case", ["no-label", "no-request", "integration-off"])
def test_status_alone_does_not_make_a_card_a_subtask(henv, monkeypatch, case):
    pending(henv, monkeypatch)
    ti.mark_path(henv, ID).unlink()
    if case == "integration-off":
        henv.set_integration(enabled=False)
        ti.point_to_symphony(henv)
    else:
        ti.review_card(henv, ID, state="Merging", labels="[]" if case == "no-label" else "[role:worker]",
                       request=None if case == "no-request" else ti.TASK)
    old = ti.git(henv.code, "rev-parse", ti.BRANCH)
    assert check.hook_main() == 0
    assert not ti.mark_path(henv, ID).exists()
    proc = resume(henv)
    assert proc.returncode == 1 and "Traceback" not in proc.stderr
    assert ti.git(henv.code, "rev-parse", ti.BRANCH) == old
    assert ti.card_state(henv, ID) == "Merging"


@pytest.mark.parametrize("state", ["Merging", "Human Review"])
def test_resume_rechecks_without_trusting_an_old_pass_mark(henv, monkeypatch, state):
    pending(henv, monkeypatch, state)
    ti.write_mark(henv, ID, "0" * 40, ppid=-1)
    proc = resume(henv)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert ti.card_state(henv, ID) == "Done"
    assert not ti.mark_path(henv, ID).exists()
    tip = ti.git(henv.code, "rev-parse", ti.BRANCH)
    again = resume(henv)
    assert again.returncode == 0 and "уже Done" in again.stdout
    assert ti.git(henv.code, "rev-parse", ti.BRANCH) == tip


def test_resume_after_merge_before_done_creates_no_duplicate_commit(henv, monkeypatch):
    pending(henv, monkeypatch)
    ti.point_to_symphony(henv, ti.NOOP_SYMPHONY)
    assert ti.run_hook(henv, ID, monkeypatch) == 0
    assert ti.card_state(henv, ID) == "Merging"
    tip = ti.git(henv.code, "rev-parse", ti.BRANCH)
    assert ti.git(henv.code, "show", f"{tip}:{NOTE}") == "new notes"
    assert not (henv.board / ".ours-state" / f"{ID}.merged.json").exists()
    ti.point_to_symphony(henv)
    proc = resume(henv)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert ti.git(henv.code, "rev-parse", ti.BRANCH) == tip
    assert ti.card_state(henv, ID) == "Done"


def test_resuming_last_subtask_starts_parent_verification_after_releasing_locks(henv, monkeypatch):
    final.make_task(henv, monkeypatch, ids=(ID,))
    pending(henv, monkeypatch)
    proc = resume(henv)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert ti.card_state(henv, ID) == "Done"
    assert ti.card_state(henv, ti.TASK) == "Verify"
    assert integrate.final_path(henv.board, ti.TASK).is_file()


@pytest.mark.parametrize("case", ["dirty", "wrong-branch", "wrong-base", "wrong-state", "foreign-repo"])
def test_resume_refuses_an_unsafe_workspace_without_advancing_any_branch(henv, monkeypatch, case):
    pending(henv, monkeypatch)
    ws = henv.ws(ID)
    old = ti.git(henv.code, "rev-parse", ti.BRANCH)
    if case == "dirty":
        ti.write(ws / NOTE, "unsaved\n")
    elif case == "wrong-branch":
        ti.git(ws, "switch", "-c", "unrelated")
    elif case == "wrong-base":
        record = ti.pc.base_record_path(henv.board, ID)
        data = json.loads(record.read_text())
        data['task'] = "OMS-999"
        record.write_text(json.dumps(data))
    elif case == "foreign-repo":
        alien = ti.make_repo(henv.tmp / "alien")
        ti.git(alien, "branch", f"symphony/{ID}")
        ti.git(henv.code, "worktree", "remove", str(ws))
        ti.git(alien, "worktree", "add", str(ws), f"symphony/{ID}")
    else:
        card = henv.board / "kanban" / f"{ID}.md"
        card.write_text(card.read_text().replace("state: Merging", "state: In Progress"))
    proc = resume(henv)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stderr
    assert ti.git(henv.code, "rev-parse", ti.BRANCH) == old


def test_resume_failed_test_run_requests_human_without_merge_or_agent(henv, monkeypatch):
    pending(henv, monkeypatch)
    toml = henv.board / "project.toml"
    toml.write_text(toml.read_text().replace('doc_paths = ["docs/our/**/*.md"]', 'doc_paths = []'))
    ti.write(henv.board / "test.sh", "echo 'PIPELINE_TESTS passed=0 skipped=0 failed=1'\nexit 1\n")
    old = ti.git(henv.code, "rev-parse", ti.BRANCH)
    proc = resume(henv)
    assert proc.returncode == 1 and "повторная проверка" in proc.stderr
    assert ti.card_state(henv, ID) == "needs_human"
    assert ti.git(henv.code, "rev-parse", ti.BRANCH) == old
    assert not ti.mark_path(henv, ID).exists()


def test_resume_detects_changes_made_during_recheck(henv, monkeypatch):
    pending(henv, monkeypatch)
    real = check.run_checks
    old = ti.git(henv.code, "rev-parse", ti.BRANCH)

    def changed(ctx, cfg):
        result = real(ctx, cfg)
        ti.write(ctx.workspace / NOTE, "changed during tests\n")
        return result

    monkeypatch.setattr(check, "run_checks", changed)
    assert integrate.main(["resume-subtask", ID, "--board", str(henv.board)]) == 1
    assert ti.card_state(henv, ID) == "Merging"
    assert ti.git(henv.code, "rev-parse", ti.BRANCH) == old


def test_resume_respects_an_existing_check_lock(henv, monkeypatch):
    pending(henv, monkeypatch)
    ctx = check.Context("hook", henv.board, henv.ws(ID), ID)
    with check.try_lock(ctx, 0):
        monkeypatch.setattr(integrate, "LOCK_WAIT_S", 0)
        assert integrate.main(["resume-subtask", ID, "--board", str(henv.board)]) == 1
    assert ti.card_state(henv, ID) == "Merging"


@pytest.mark.parametrize("ident", ["../OMS-12", "-wrong", "EVE-1"])
def test_resume_rejects_wrong_identifiers(henv, ident):
    proc = resume(henv, ident)
    assert proc.returncode in {1, 2} and "Traceback" not in proc.stderr
