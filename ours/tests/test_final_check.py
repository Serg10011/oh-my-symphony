"""Tests for the final check of a task (TASK-S4-21): `## Subtask Progress`, the signal
"every subtask is Done", integrate.finish_task / `integrate.py finish-task`, the rules of
check.py for the card of a task and the branches of verify.md / document.md.

Real temporary git repositories and the real `symphony board` of this working copy (as
test_integrate.py and test_plan_hook.py); the service is not started. The locks are the real
ones (worktree_lock, try_lock): nothing in this file replaces them.

Run: python -m pytest ours/tests -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import test_check as tc
from test_integrate import (
    BRANCH,
    NOOP_SYMPHONY,
    TASK,
    Env,
    card_body,
    card_state,
    events,
    git,
    put_file,
    racing,
    review_card,
    run_hook,
    subtask,
    write,
    write_mark,
)
from test_planning_states import PREFIX as CARD_PREFIX
from test_planning_states import render_prompt

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the hooks are POSIX-only")

OURS = Path(__file__).resolve().parents[1]
INTEGRATE = OURS / "hooks" / "integrate.py"
sys.path.insert(0, str(OURS / "hooks"))

import check  # noqa: E402
import integrate  # noqa: E402
import project_config as pc  # noqa: E402

REAL_SYMPHONY = Path(sys.executable).parent / "symphony"
pytestmark = [
    pytestmark,
    pytest.mark.skipif(not REAL_SYMPHONY.exists(), reason="no `symphony` next to the test python"),
]

SUBS = ("OMS-7-1", "OMS-7-2")
MAIN_REF = "refs/heads/main"


# ---------------------------------------------------------------------------
# a board with a task card
# ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    for key in [k for k in os.environ if k.startswith("SYMPHONY_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("SYMPHONY_WORKFLOW_DIR", str(tmp_path / "projects" / "demo"))
    monkeypatch.setenv("SYMPHONY_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("SYMPHONY_PYTHON", sys.executable)
    made = Env(tmp_path, enabled=True)
    toml = made.board / "project.toml"
    toml.write_text(toml.read_text().replace('"/bin/symphony"', json.dumps(str(REAL_SYMPHONY))),
                    encoding="utf-8")
    return made


def use_symphony(env: Env, script: str) -> None:
    """The board's [tools] symphony: a fake script instead of the real one."""
    binary = write(env.tmp / "bin" / "symphony", script.format(python=sys.executable))
    binary.chmod(0o755)
    toml = env.board / "project.toml"
    toml.write_text(toml.read_text().replace(json.dumps(str(REAL_SYMPHONY)), json.dumps(str(binary))),
                    encoding="utf-8")


def task_card_text(state: str = "Subtasks", ids: tuple[str, ...] = SUBS, *,
                   labels: str = "[role:manager-plan]", accepted: bool = True) -> str:
    lines = ["---", f"id: {TASK}", f"identifier: {TASK}", "title: The task", f"state: {state}"]
    if labels:
        lines.append(f"labels: {labels}")
    text = "\n".join([*lines, "---", "", "The goal of the task.", ""])
    if accepted:
        text += "\n".join([
            "## Plan Accepted", "", "- at: 2026-10-27T00:00:00Z", f"- branch: {BRANCH} (created)",
            f"- subtasks: {len(ids)}", "",
            *(f"- {ident} — Subtask {ident} — simple" for ident in ids), "",
            "## Subtask Progress", "", f"0 of {len(ids)} merged", "",
        ])
    return text


def make_task(env: Env, monkeypatch: pytest.MonkeyPatch, *, start: bool = True, state: str = "Subtasks",
              ids: tuple[str, ...] = SUBS) -> None:
    """The card of the task, its working copy (as worktree.py makes it) and the integration branch."""
    env.card(TASK, task_card_text(state, ids))
    if start:
        assert env.start(TASK, monkeypatch) == 0
    integrate.ensure_integration_branch(env.code, BRANCH, "main")


def all_done(env: Env, ids: tuple[str, ...] = SUBS) -> None:
    """Every subtask is Done by hand (a human did it) and the integration branch holds their work."""
    for ident in ids:
        review_card(env, ident, state="Done")
    put_file(env.code, BRANCH, "feature.txt", "feature\n")


def put_path(repo: Path, branch: str, path: str, content: str) -> str:
    """Commit a file at a nested path on `branch` without touching any checkout."""
    old = git(repo, "rev-parse", f"refs/heads/{branch}")
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=repo, input=content, check=True,
                          capture_output=True, text=True).stdout.strip()
    index = {"GIT_INDEX_FILE": str(repo / ".git" / "tmp-index-final-check")}
    env = {**os.environ, **index}

    def run(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True,
                              text=True).stdout.strip()

    try:
        run("read-tree", old)
        run("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
        tree = run("write-tree")
    finally:
        (repo / ".git" / "tmp-index-final-check").unlink(missing_ok=True)
    commit = git(repo, "commit-tree", tree, "-p", old, "-m", f"put {path}")
    git(repo, "update-ref", f"refs/heads/{branch}", commit, old)
    return commit


def project(env: Env) -> dict:
    return pc.load_project(env.board)


def finish(env: Env) -> tuple[bool, str]:
    return integrate.finish_task(env.board, project(env), TASK)


def tip(env: Env, ref: str) -> str:
    return git(env.code, "rev-parse", ref)


def snapshot(env: Env) -> dict:
    ws = env.ws(TASK)
    record = pc.base_record_path(env.board, TASK)
    return {
        "int": tip(env, BRANCH), "main": tip(env, "main"),
        "task_branch": tip(env, f"symphony/{TASK}") if f"symphony/{TASK}" in env.branches() else None,
        "head": git(ws, "rev-parse", "HEAD") if (ws / ".git").exists() else None,
        "record": record.read_bytes() if record.exists() else None,
        "final": integrate.final_path(env.board, TASK).exists(),
        "diff": (env.board / "artifacts" / TASK / "work" / "final-diff.md").exists(),
    }


def final_events(env: Env) -> list[dict]:
    return [e for e in events(env) if e["outcome"].startswith("final:")]


def final_record(env: Env) -> dict:
    return json.loads(integrate.final_path(env.board, TASK).read_text(encoding="utf-8"))


def work_file(env: Env, name: str) -> str:
    return (env.board / "artifacts" / TASK / "work" / name).read_text(encoding="utf-8")


def assert_stopped(env: Env, before: dict, *needles: str) -> None:
    """The task card is in needs_human with `## Final Check`, and nothing else moved."""
    assert card_state(env, TASK) == "needs_human"
    body = card_body(env, TASK)
    assert "## Final Check" in body and "- result: needs_human" in body
    for needle in needles:
        assert needle in body, needle
    assert snapshot(env) == before
    assert [e["outcome"] for e in final_events(env)] == ["final:needs_human"]


# ---------------------------------------------------------------------------
# what is a task card
# ---------------------------------------------------------------------------


def test_is_task_card_needs_the_label_and_the_accepted_plan() -> None:
    body = task_card_text().split("---\n", 2)[2]
    manager = {"state": "Subtasks", "labels": ("role:manager-plan",), "request": None}
    assert integrate.is_task_card(manager, body)
    assert not integrate.is_task_card({**manager, "labels": ("role:worker",)}, body)
    assert not integrate.is_task_card({**manager, "labels": ()}, body)
    assert not integrate.is_task_card(manager, "## Plan\n\nno accepted plan\n")
    assert integrate.is_task_card(manager, "text\n## plan accepted\n\n- OMS-7-1 — t — simple\n")


def test_plan_accepted_ids_reads_only_the_subtask_lines() -> None:
    body = task_card_text(ids=("OMS-7-1", "OMS-7-2", "OMS-7-3")).split("---\n", 2)[2]
    assert integrate.plan_accepted_ids(body) == ["OMS-7-1", "OMS-7-2", "OMS-7-3"]
    assert integrate.plan_accepted_ids("## Plan\n") == []
    # the next section ends the list; the header lines (`- at:`, `- subtasks:`) are not IDs
    assert integrate.plan_accepted_ids("## Plan Accepted\n\n- at: x\n\n## Other\n\n- OMS-9 — t — simple\n") == []


# ---------------------------------------------------------------------------
# ## Subtask Progress
# ---------------------------------------------------------------------------


def test_done_of_a_subtask_writes_a_progress_line_into_the_task_card(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_task(env, monkeypatch)
    review_card(env, SUBS[1], state="In Progress")
    subtask(env, SUBS[0], monkeypatch, {"a.txt": "a\n"})
    assert run_hook(env, SUBS[0], monkeypatch) == 0
    body = card_body(env, TASK)
    assert f"## Subtask Progress\n\n0 of 2 merged\n\n- {SUBS[0]} merged (1 of 2)" in body
    assert card_state(env, TASK) == "Subtasks"
    assert "## Subtask Progress" not in card_body(env, SUBS[0])  # the line goes to the task, not the subtask
    assert [e["outcome"] for e in events(env)] == ["done", "progress"]
    assert events(env)[1]["task"] == TASK


def test_a_failed_progress_line_does_not_touch_done(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    make_task(env, monkeypatch)
    review_card(env, SUBS[1], state="In Progress")
    subtask(env, SUBS[0], monkeypatch, {"a.txt": "a\n"})
    real = check.append_note

    def broken(ctx, body, heading=check.NOTE_HEADING, issue_id=None):
        if heading == integrate.NOTE_PROGRESS:
            raise OSError("disk full")
        return real(ctx, body, heading, issue_id)

    monkeypatch.setattr(check, "append_note", broken)
    assert run_hook(env, SUBS[0], monkeypatch) == 0
    assert card_state(env, SUBS[0]) == "Done"
    assert git(env.code, "show", f"{BRANCH}:a.txt") == "a"
    outcomes = [e["outcome"] for e in events(env)]
    assert outcomes == ["done", "error"]
    assert "progress of OMS-7" in events(env)[1]["reason"] and "disk full" in events(env)[1]["reason"]
    assert "(1 of 2)" not in card_body(env, TASK)


def test_the_progress_line_is_written_under_the_lock_of_the_task_card(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_task(env, monkeypatch)
    review_card(env, SUBS[1], state="In Progress")
    subtask(env, SUBS[0], monkeypatch, {"a.txt": "a\n"})
    taken: list[str] = []
    real = check.try_lock

    def watching(ctx, wait_s):
        taken.append(ctx.issue_id)
        return real(ctx, wait_s)

    monkeypatch.setattr(check, "try_lock", watching)
    assert run_hook(env, SUBS[0], monkeypatch) == 0
    assert taken == [SUBS[0], TASK]  # the subtask's lock for the merge, then the task card's


def test_a_task_without_an_accepted_plan_gets_no_line_and_no_noise(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    subtask(env, SUBS[0], monkeypatch, {"a.txt": "a\n"})  # no card of the task on this board at all
    assert run_hook(env, SUBS[0], monkeypatch) == 0
    assert [e["outcome"] for e in events(env)] == ["done"]
    env.card(TASK, task_card_text(accepted=False))
    subtask(env, SUBS[1], monkeypatch, {"b.txt": "b\n"})
    assert run_hook(env, SUBS[1], monkeypatch) == 0
    assert [e["outcome"] for e in events(env)] == ["done", "done"]
    assert "merged (" not in card_body(env, TASK)


def test_append_note_can_write_into_another_card(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.card(TASK, task_card_text())
    review_card(env, SUBS[0], state="In Progress")
    ctx = check.Context("integrate", env.board, env.ws(SUBS[0]), SUBS[0])
    assert check.append_note(ctx, "- via tracker", "Subtask Progress", TASK) == "tracker"
    monkeypatch.delenv("SYMPHONY_PYTHON")
    assert check.append_note(ctx, "- via file", "Subtask Progress", TASK) == "file"
    assert "- via tracker" in card_body(env, TASK) and "- via file" in card_body(env, TASK)
    assert "- via" not in card_body(env, SUBS[0])


# ---------------------------------------------------------------------------
# the signal "every subtask is Done"
# ---------------------------------------------------------------------------


def test_the_last_subtask_sends_the_task_card_to_verify(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    make_task(env, monkeypatch)
    review_card(env, SUBS[1], state="In Progress")
    subtask(env, SUBS[0], monkeypatch, {"a.txt": "a\n"})
    assert run_hook(env, SUBS[0], monkeypatch) == 0
    assert card_state(env, TASK) == "Subtasks"  # not the last one
    assert final_events(env) == []
    subtask(env, SUBS[1], monkeypatch, {"b.txt": "b\n"}, title="Second")
    assert run_hook(env, SUBS[1], monkeypatch) == 0
    assert card_state(env, SUBS[1]) == "Done"
    assert card_state(env, TASK) == "Verify"
    body = card_body(env, TASK)
    assert f"- {SUBS[0]} merged (1 of 2)" in body and f"- {SUBS[1]} merged (2 of 2)" in body
    assert "## Final Check" in body
    assert [e["outcome"] for e in final_events(env)] == ["final:started"]
    # the working copy of the task stands on the integration branch with the work of both subtasks
    ws = env.ws(TASK)
    assert git(ws, "rev-parse", "HEAD") == tip(env, BRANCH) == tip(env, f"symphony/{TASK}")
    assert (ws / "a.txt").read_text() == "a\n" and (ws / "b.txt").read_text() == "b\n"


def test_a_subtask_in_needs_human_keeps_the_task_in_subtasks(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    make_task(env, monkeypatch)
    review_card(env, SUBS[1], state="needs_human")
    subtask(env, SUBS[0], monkeypatch, {"a.txt": "a\n"})
    assert run_hook(env, SUBS[0], monkeypatch) == 0
    assert card_state(env, TASK) == "Subtasks"
    assert final_events(env) == []
    assert snapshot(env)["final"] is False


def test_a_task_card_not_in_subtasks_is_left_alone(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    make_task(env, monkeypatch, state="needs_human")
    review_card(env, SUBS[1], state="Done")
    subtask(env, SUBS[0], monkeypatch, {"a.txt": "a\n"})
    assert run_hook(env, SUBS[0], monkeypatch) == 0
    assert card_state(env, TASK) == "needs_human"
    assert final_events(env) == []


def test_the_real_locks_do_not_wait_for_themselves(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """integrate.py hook of the last subtask -> finish_task with the real worktree_lock and try_lock,
    in a separate process: it reaches Verify at once instead of waiting out a lock timeout."""
    make_task(env, monkeypatch)
    review_card(env, SUBS[1], state="Done")
    subtask(env, SUBS[0], monkeypatch, {"a.txt": "a\n"})
    mark = json.loads((env.board / ".ours-state" / f"{SUBS[0]}.pass.json").read_text())
    write_mark(env, SUBS[0], mark["tree"], ppid=os.getpid())  # the parent of the child below is this process
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("SYMPHONY_")}
    child_env.update(SYMPHONY_WORKFLOW_DIR=str(env.board), SYMPHONY_WORKSPACE_ROOT=str(env.ws_root),
                     SYMPHONY_PYTHON=sys.executable, SYMPHONY_ISSUE_ID=SUBS[0])
    started = time.monotonic()
    proc = subprocess.run([sys.executable, str(INTEGRATE), "hook"], cwd=env.ws(SUBS[0]), env=child_env,
                          capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    assert time.monotonic() - started < 30
    assert card_state(env, SUBS[0]) == "Done"
    assert card_state(env, TASK) == "Verify", proc.stderr
    assert "блокировк" not in proc.stderr


# ---------------------------------------------------------------------------
# the merge with the base branch
# ---------------------------------------------------------------------------


def test_no_merge_commit_when_the_base_did_not_move(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    make_task(env, monkeypatch)
    all_done(env)
    main, before = tip(env, "main"), tip(env, BRANCH)
    ok, text = finish(env)
    assert ok, text
    assert "слияния не нужно" in text
    assert tip(env, BRANCH) == before  # not moved
    assert git(env.ws(TASK), "rev-parse", "HEAD") == before == tip(env, f"symphony/{TASK}")
    record = final_record(env)
    assert record["merged"] is False and record["our_main"] == main and record["integration_commit"] == before
    assert card_state(env, TASK) == "Verify"
    assert "- result: no merge needed" in card_body(env, TASK)


def test_a_moved_base_is_merged_with_a_two_parent_commit(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    make_task(env, monkeypatch)
    all_done(env)
    put_file(env.code, "main", "main-change.txt", "from main\n")
    main, before = tip(env, "main"), tip(env, BRANCH)
    ok, text = finish(env)
    assert ok, text
    new = tip(env, BRANCH)
    assert new != before
    assert git(env.code, "rev-parse", f"{new}^@").split() == [before, main]  # int first, then main
    assert git(env.code, "log", "-1", "--format=%s", new) == f"{TASK}: merge main for the final check"
    assert git(env.code, "log", "-1", "--format=%an <%ae>|%cn <%ce>", new) == (
        "symphony <symphony@local>|symphony <symphony@local>")
    assert tip(env, f"symphony/{TASK}") == new == git(env.ws(TASK), "rev-parse", "HEAD")
    ws = env.ws(TASK)
    assert (ws / "feature.txt").is_file() and (ws / "main-change.txt").is_file()
    assert git(ws, "status", "--porcelain") == ""
    record = final_record(env)
    assert (record["merged"], record["integration_commit"], record["our_main"]) == (True, new, main)
    assert tip(env, "main") == main  # the base branch itself is never touched
    assert "- result: merge commit " + new in card_body(env, TASK)
    (started,) = final_events(env)
    assert started["outcome"] == "final:started" and started["commit"] == new and started["merged"] == "true"


def test_a_conflict_with_the_base_calls_a_human_and_changes_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(env.code / "shared.txt", "one\ntwo\nthree\n")
    git(env.code, "add", "-A")
    git(env.code, "commit", "-q", "-m", "shared")
    make_task(env, monkeypatch)
    all_done(env)
    put_file(env.code, BRANCH, "shared.txt", "one\nTWO FROM THE TASK\nthree\n")
    put_file(env.code, "main", "shared.txt", "one\nTWO FROM MAIN\nthree\n")
    before = snapshot(env)
    ok, text = finish(env)
    assert not ok and "needs_human" in text
    assert_stopped(env, before, "shared.txt", "ветки и рабочая копия не менялись")
    assert [e["outcome"] for e in events(env)] == ["final:needs_human"]
    assert events(env)[0]["files"] == "shared.txt"


def test_a_branch_moved_during_the_merge_gets_one_retry(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    make_task(env, monkeypatch)
    all_done(env)
    put_file(env.code, "main", "main-change.txt", "from main\n")
    moved = racing(env, monkeypatch, 1)
    ok, text = finish(env)
    assert ok, text
    new = tip(env, BRANCH)
    assert git(env.code, "rev-parse", f"{new}^@").split() == [moved[0], tip(env, "main")]
    assert git(env.code, "show", f"{BRANCH}:race0.txt") == "r"
    assert tip(env, f"symphony/{TASK}") == new == git(env.ws(TASK), "rev-parse", "HEAD")
    assert card_state(env, TASK) == "Verify"


def test_a_branch_moved_twice_calls_a_human_and_the_working_copy_stays(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_task(env, monkeypatch)
    all_done(env)
    put_file(env.code, "main", "main-change.txt", "from main\n")
    ws_head = git(env.ws(TASK), "rev-parse", "HEAD")
    moved = racing(env, monkeypatch, 2)
    ok, text = finish(env)
    assert not ok
    assert tip(env, BRANCH) == moved[-1]  # what the other side wrote, nothing of ours
    assert git(env.ws(TASK), "rev-parse", "HEAD") == ws_head
    assert card_state(env, TASK) == "needs_human"
    assert "сдвинулась дважды" in card_body(env, TASK)
    assert [e["outcome"] for e in final_events(env)] == ["final:needs_human"]
    assert not integrate.final_path(env.board, TASK).exists()


# ---------------------------------------------------------------------------
# what is checked before anything moves
# ---------------------------------------------------------------------------


def test_a_dirty_working_copy_calls_a_human_and_changes_nothing(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    make_task(env, monkeypatch)
    all_done(env)
    write(env.ws(TASK) / "leftover.txt", "x\n")
    before = snapshot(env)
    ok, _ = finish(env)
    assert not ok
    assert_stopped(env, before, "не чистая", "leftover.txt")
    assert (env.ws(TASK) / "leftover.txt").exists()


def test_edits_in_the_manager_branch_call_a_human_and_change_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_task(env, monkeypatch)
    all_done(env)
    ws = env.ws(TASK)
    write(ws / "manager-wrote-code.txt", "x\n")
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "-m", "the manager must not write code")
    before = snapshot(env)
    ok, _ = finish(env)
    assert not ok
    assert_stopped(env, before, "в ветке руководителя есть правки файлов")


def test_a_missing_working_copy_calls_a_human_and_changes_nothing(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    make_task(env, monkeypatch, start=False)
    all_done(env)
    before = snapshot(env)
    ok, _ = finish(env)
    assert not ok
    assert_stopped(env, before, "нет рабочей копии карточки-задачи")


def test_a_working_copy_on_another_branch_calls_a_human(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    make_task(env, monkeypatch)
    all_done(env)
    git(env.ws(TASK), "checkout", "-q", "-b", "elsewhere")
    before = snapshot(env)
    ok, _ = finish(env)
    assert not ok
    assert_stopped(env, before, f"стоит не на symphony/{TASK}")


@pytest.mark.parametrize("case", ["not-all-done", "wrong-state", "no-label", "no-plan"])
def test_a_task_that_is_not_ready_is_refused_without_a_move(
    env: Env, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    make_task(env, monkeypatch)
    all_done(env)
    if case == "not-all-done":
        review_card(env, SUBS[1], state="Verify")
    elif case == "wrong-state":
        env.card(TASK, task_card_text("Verify"))
    elif case == "no-label":
        env.card(TASK, task_card_text(labels="[backend]"))
    else:
        env.card(TASK, task_card_text(accepted=False))
    state = card_state(env, TASK)
    before = snapshot(env)
    ok, text = finish(env)
    assert not ok and text.startswith(f"{TASK}:")
    assert card_state(env, TASK) == state  # the card is not blamed for being early
    assert snapshot(env) == before
    assert [e["outcome"] for e in final_events(env)] == ["final:refused"]


def test_a_card_that_does_not_move_is_reported(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    make_task(env, monkeypatch)
    all_done(env)
    use_symphony(env, NOOP_SYMPHONY)
    ok, text = finish(env)
    assert not ok and "не удалось перевести в Verify" in text
    assert card_state(env, TASK) == "Subtasks"  # the fake never moves cards
    assert [e["outcome"] for e in final_events(env)] == ["final:needs_human"]
    assert any(e["outcome"] == "error" for e in events(env))


# ---------------------------------------------------------------------------
# the base record, .final.json, the diff files
# ---------------------------------------------------------------------------


def test_the_base_moves_to_the_used_end_of_the_base_branch(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    make_task(env, monkeypatch)
    ws = env.ws(TASK)
    old = env.record(TASK)["base_sha"]
    # worktree.py gives a task card the base branch as the target of the merge preflight (Verify)
    assert git(ws, "config", "--worktree", "symphony.mergetargetbranch") == "main"
    assert git(ws, "config", "--worktree", "symphony.basebranch") == "main"
    all_done(env)
    put_file(env.code, "main", "main-change.txt", "from main\n")
    main = tip(env, "main")
    ok, text = finish(env)
    assert ok, text
    record = env.record(TASK)
    assert record["base_sha"] == main and record["final_from"] == old and old != main
    assert record["base_branch"] == "main" and record["task"] is None  # the other fields are kept
    assert git(ws, "config", "--worktree", "symphony.basesha") == main
    assert git(ws, "config", "--worktree", "symphony.mergetargetbranch") == "main"
    assert git(ws, "config", "--worktree", "symphony.basebranch") == "main"
    assert set(final_record(env)) == {"task", "branch", "integration_commit", "our_main", "merged", "at"}
    assert final_record(env)["task"] == TASK and final_record(env)["branch"] == BRANCH


def test_a_wrong_merge_target_is_put_right(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    make_task(env, monkeypatch)
    all_done(env)
    ws = env.ws(TASK)
    git(ws, "config", "--worktree", "symphony.mergetargetbranch", BRANCH)
    ok, text = finish(env)
    assert ok, text
    assert git(ws, "config", "--worktree", "symphony.mergetargetbranch") == "main"


def test_the_diff_files_for_verify(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    make_task(env, monkeypatch)
    all_done(env)
    put_file(env.code, "main", "main-change.txt", "from main\n")
    main = tip(env, "main")
    ok, text = finish(env)
    assert ok, text
    target = tip(env, BRANCH)
    summary = work_file(env, "final-diff.md")
    assert f"`{main}`" in summary and f"`{target}`" in summary
    assert "A\tfeature.txt" in summary and "feature.txt |" in summary
    assert "main-change.txt" not in summary  # the changes of the base branch are not the task's
    patch = work_file(env, "final-diff.patch")
    assert "diff --git a/feature.txt b/feature.txt" in patch and "+feature" in patch
    assert "main-change.txt" not in patch and "обрезано" not in patch


def test_a_big_diff_is_cut_at_a_line_and_says_so(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    make_task(env, monkeypatch)
    all_done(env)
    put_file(env.code, BRANCH, "big.txt", "".join(f"line {n:06d} {'x' * 100}\n" for n in range(3000)))
    main = tip(env, "main")
    ok, text = finish(env)
    assert ok, text
    target = tip(env, BRANCH)
    patch = work_file(env, "final-diff.patch")
    *body, last = patch.splitlines()
    assert len(patch.encode()) < integrate.DIFF_LIMIT + 400
    assert last.startswith("обрезано, ") and last.endswith(f"полный дифф: git diff {main} {target}")
    assert " КБ всего" in last
    assert body[-1].startswith("+line ") and body[-1].endswith("x" * 100)  # cut at a line boundary
    assert "big.txt" in work_file(env, "final-diff.md")


def test_the_diff_is_not_written_through_a_link(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    make_task(env, monkeypatch)
    all_done(env)
    aside = env.tmp / "elsewhere"
    aside.mkdir()
    work = env.board / "artifacts" / TASK / "work"
    work.rmdir()  # worktree.py made it empty
    work.symlink_to(aside, target_is_directory=True)
    ok, text = finish(env)
    assert not ok and "ссылка" in text
    assert list(aside.iterdir()) == []
    assert card_state(env, TASK) == "needs_human"
    # the branches had moved already (the text says so): the human gets the list
    assert "symphony/OMS-7: на " in card_body(env, TASK)


# ---------------------------------------------------------------------------
# check.py on the working copy of the task after the final check
# ---------------------------------------------------------------------------


def write_checks(env: Env, doc_paths: str = '["docs/**/*.md", "*.md"]') -> Path:
    """Baseline, test.sh with its summary line and [checks] for run_checks on the task's working copy."""
    base = tip(env, "main")
    write(env.board / ".ours-state" / "baseline.json", json.dumps({
        "base_branch": "main", "base_sha": base, "passed": 1, "skipped": 0, "failed": 0,
        "setup_s": 0.1, "tests_s": 0.1, "measured_at": "2026-10-27T00:00:00Z"}))
    marker = env.tmp / "tests-ran"
    write(env.board / "test.sh", f"touch {marker}\necho 'PIPELINE_TESTS passed=1 skipped=0 failed=0'\n")
    toml = env.board / "project.toml"
    toml.write_text(toml.read_text() + f"\n[checks]\ndoc_paths = {doc_paths}\n", encoding="utf-8")
    return marker


def done_task_card(env: Env, state: str = "Human Review") -> None:
    text = task_card_text(state)
    env.card(TASK, text + "\n## Wiki Updates\n\n- none\n\n## As-Is -> To-Be Report\n\nreport\n")


def test_check_py_accepts_the_working_copy_after_the_final_check(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    make_task(env, monkeypatch)
    all_done(env)
    # the base branch gets a file that the checks forbid; it is not the task's change
    put_path(env.code, "main", ".github/ci.yml", "ci\n")
    old_base = env.record(TASK)["base_sha"]
    ok, text = finish(env)
    assert ok, text
    marker = write_checks(env, "[]")
    ws, main = env.ws(TASK), tip(env, "main")
    repo, base = check.resolve_base(ws, env.board, TASK)  # base_sha is an ancestor of HEAD
    assert base == main
    cfg = check.load_config(env.board)
    assert check.forbidden_violations(check.changed_paths(repo, base, "kanban"), cfg) == []
    # with the old base the same change set would be a violation: this is what the move of the base fixes
    assert ".github/ci.yml" in check.changed_paths(repo, old_base, "kanban")
    done_task_card(env)
    result = check.run_checks(check.Context("hook", env.board, ws, TASK), cfg)
    assert result.ok, result.reasons
    assert marker.exists()


@pytest.mark.parametrize("mode", ["hook", "gate"])
def test_the_task_card_never_skips_the_full_suite_for_doc_paths(
    env: Env, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    make_task(env, monkeypatch)
    all_done(env)
    put_path(env.code, BRANCH, "docs/our/NOTE.md", "only docs\n")
    ok, text = finish(env)
    assert ok, text
    marker = write_checks(env, '["docs/**/*.md", "feature.txt"]')  # every changed path is a doc path
    done_task_card(env, "Human Review" if mode == "hook" else "Done")
    ws = env.ws(TASK)
    cfg = check.load_config(env.board)
    changed = check.changed_paths(check.resolve_base(ws, env.board, TASK)[0], tip(env, "main"), "kanban")
    assert changed and check._docs_only(changed, cfg) is not None  # an ordinary card would skip here
    result = check.run_checks(check.Context(mode, env.board, ws, TASK), cfg)
    assert result.ok, result.reasons
    assert marker.exists() and not result.tests_skipped
    assert (result.tests, result.tests_reason, result.task_card) == ("full", check.TASK_CARD_FULL_REASON, True)
    assert check.TASK_CARD_FULL_REASON in check._format_report(mode, TASK, result)


def test_the_gate_of_a_task_card_ignores_a_merged_record(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    make_task(env, monkeypatch)
    all_done(env)
    ok, text = finish(env)
    assert ok, text
    marker = write_checks(env, "[]")
    done_task_card(env, "Done")
    ws = env.ws(TASK)
    # integrate.py hook never writes this record for a task card; even a forged one skips nothing
    write(env.board / ".ours-state" / f"{TASK}.merged.json",
          json.dumps({"tree": check.worktree_tree(ws), "commit": None, "branch": BRANCH}))
    result = check.run_checks(check.Context("gate", env.board, ws, TASK))
    assert result.ok, result.reasons
    assert marker.exists() and not result.tests_skipped and result.tests == "full"
    assert not (env.board / ".ours-state" / f"{TASK}.merged.json").exists()  # used up all the same


def test_required_sections_of_a_task_card_are_the_ordinary_ones(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    make_task(env, monkeypatch)
    cfg = check.load_config(env.board)
    env.card(TASK, task_card_text("Human Review") + "\n## QA Evidence\n\nx\n\n## Merge Status\n\nx\n")
    problems = check.card_violations(env.board / "kanban" / f"{TASK}.md", cfg)
    assert problems == ["card section missing or empty: ## Wiki Updates",
                        "card section missing or empty: ## As-Is -> To-Be Report"]
    assert not check.is_subtask_card(env.board / "kanban" / f"{TASK}.md", cfg)
    done_task_card(env)
    assert check.card_violations(env.board / "kanban" / f"{TASK}.md", cfg) == []


class TaskCardEnv:
    """test_check.Env whose card is the card of a task."""

    def __init__(self, tmp: Path, **config: object) -> None:
        self.env = tc.Env(tmp)
        self.env.config(test_command=f"touch {self.env.marker}", doc_paths=["docs/our/**/*.md"], **config)

    def card(self, task: bool, state: str = "Human Review") -> None:
        labels = "labels: [role:manager-plan]\n" if task else ""
        accepted = "\n## Plan Accepted\n\n- TT-1-1 — t — simple\n" if task else ""
        write(self.env.card, f"---\nid: {tc.ID}\nidentifier: {tc.ID}\ntitle: t\nstate: {state}\n{labels}---\n\n"
                             f"{tc.GOOD_BODY}{accepted}")


def test_a_failed_hook_sends_the_task_card_straight_to_needs_human(tmp_path: Path) -> None:
    kit = TaskCardEnv(tmp_path)
    kit.card(task=True)
    write(kit.env.board / "test.sh", "echo broken\nexit 1\n")
    tc.write(kit.env.ws / "docs" / "our" / "NOTE.md", "text\n")
    kit.env.commit()
    result = kit.env.run("hook")
    assert result.returncode == 0
    assert "FAIL" in result.stdout and "task card: full suite (S4-16 п. 4)" in result.stdout
    assert kit.env.mv_calls()[-1][2:4] == [tc.ID, "needs_human"]
    assert kit.env.state() == "needs_human"
    assert kit.env.counter()["failures"] == 1  # one failure of three is enough for a task card
    card = kit.env.card.read_text()
    assert "- task card: straight to needs_human, no worker retry (S4-21)" in card
    assert "- failures: 1 of 3" in card
    (event,) = [e for e in kit.env.log() if e["outcome"].startswith("rollback")]
    assert event["outcome"] == "rollback:needs_human"


def test_the_same_failure_of_an_ordinary_card_goes_back_to_in_progress(tmp_path: Path) -> None:
    kit = TaskCardEnv(tmp_path)
    kit.card(task=False)
    write(kit.env.board / "test.sh", "echo broken\nexit 1\n")
    tc.write(kit.env.ws / "src" / "other.py", "y = 1\n")
    kit.env.commit()
    kit.env.run("hook")
    assert kit.env.state() == "In Progress"


def test_the_hook_runs_the_tests_of_a_task_card_even_for_docs_only(tmp_path: Path) -> None:
    kit = TaskCardEnv(tmp_path)
    tc.write(kit.env.ws / "docs" / "our" / "NOTE.md", "text\n")
    kit.env.commit()
    kit.card(task=False)
    assert kit.env.run("hook").returncode == 0
    assert not kit.env.marker.exists()  # an ordinary card: skipped by doc_paths
    kit.card(task=True)
    result = kit.env.run("hook")
    assert result.returncode == 0 and kit.env.marker.exists()
    assert "PASS" in result.stdout and "task card: full suite (S4-16 п. 4)" in result.stdout
    passed = [e for e in kit.env.log() if e["mode"] == "hook" and e["outcome"] == "pass"][-1]
    assert (passed["tests"], passed["tests_reason"]) == ("full", check.TASK_CARD_FULL_REASON)


# ---------------------------------------------------------------------------
# finish-task by hand
# ---------------------------------------------------------------------------


def run_cli(env: Env, *args: str, board: Path | None = None) -> subprocess.CompletedProcess[str]:
    cli_env = {k: v for k, v in os.environ.items() if not k.startswith("SYMPHONY_")}
    cli_env.update(SYMPHONY_WORKSPACE_ROOT=str(env.ws_root), SYMPHONY_PYTHON=sys.executable)
    return subprocess.run([sys.executable, str(INTEGRATE), *args], env=cli_env, capture_output=True, text=True,
                          timeout=60, check=False)


def test_cli_finish_task_moves_the_card_and_says_so_in_russian(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    make_task(env, monkeypatch)
    all_done(env)
    result = run_cli(env, "finish-task", TASK, "--board", str(env.board))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith(f"integrate.py: {TASK}: итоговая проверка начата, карточка → Verify;")
    assert card_state(env, TASK) == "Verify"


def test_cli_finish_task_takes_the_board_from_the_environment(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    make_task(env, monkeypatch)
    all_done(env)
    cli_env = {k: v for k, v in os.environ.items() if not k.startswith("SYMPHONY_")}
    cli_env.update(SYMPHONY_WORKFLOW_DIR=str(env.board), SYMPHONY_WORKSPACE_ROOT=str(env.ws_root))
    result = subprocess.run([sys.executable, str(INTEGRATE), "finish-task", TASK], env=cli_env,
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert card_state(env, TASK) == "Verify"


def test_cli_finish_task_refuses_with_the_reason(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    make_task(env, monkeypatch)
    all_done(env)
    review_card(env, SUBS[1], state="Verify")
    result = run_cli(env, "finish-task", TASK, "--board", str(env.board))
    assert result.returncode == 1
    assert f"{TASK}: не все подзадачи в Done: {SUBS[1]} (Verify)." in result.stdout
    assert card_state(env, TASK) == "Subtasks"


def test_cli_finish_task_stopped_by_a_problem_says_needs_human(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    make_task(env, monkeypatch)
    all_done(env)
    write(env.ws(TASK) / "leftover.txt", "x\n")
    result = run_cli(env, "finish-task", TASK, "--board", str(env.board))
    assert result.returncode == 1
    assert "итоговая проверка не начата, карточка → needs_human" in result.stdout
    assert "leftover.txt" in result.stdout


def test_cli_finish_task_arguments(env: Env) -> None:
    assert run_cli(env, "finish-task").returncode == 2
    assert run_cli(env, "finish-task", TASK, "extra", "--board", str(env.board)).returncode == 2
    assert run_cli(env, "finish-task", TASK).returncode == 2  # no board, no $SYMPHONY_WORKFLOW_DIR
    bad = run_cli(env, "finish-task", "../x", "--board", str(env.board))
    assert bad.returncode == 1 and "отказ:" in bad.stderr


# ---------------------------------------------------------------------------
# the prompts, rendered by the engine of the core
# ---------------------------------------------------------------------------


def test_verify_has_the_branch_of_a_task_card() -> None:
    ident = f"{CARD_PREFIX}-2"
    manager = render_prompt("stages/verify.md", state="Verify", labels=("role:manager-plan",))
    for phrase in ("## Plan Accepted", f"docs/{ident}/work/final-diff.md", f"docs/{ident}/work/final-diff.patch",
                   f"docs/{ident}/qa/final-check.md", "needs_human", f"kanban/{ident}-N.md",
                   "`## Plan`, `## Acceptance Tests` and `## Done Signals` do not exist on this ticket",
                   "do not run `git diff`", "Not `Human Review`"):
        assert phrase in manager, phrase
    plain = render_prompt("stages/verify.md", state="Verify")
    assert manager.startswith(plain[:-1])  # the old text is untouched, the branch is an addition


@pytest.mark.parametrize("labels", [(), ("bug",), ("role:worker",), ("role:manager-diagnose",)])
def test_verify_and_document_of_other_cards_have_no_such_branch(labels: tuple[str, ...]) -> None:
    verify = render_prompt("stages/verify.md", state="Verify", labels=labels, request="X-9" if labels else None)
    assert "Final check of a whole task" not in verify and "final-diff" not in verify
    assert "### VERIFY -- prove it, and prove it would merge" in verify
    assert "Any other ticket: set state to `Document`." in verify
    document = render_prompt("stages/document.md", state="Document", labels=labels)
    assert "Document of a whole task" not in document
    assert "10. Set state to `Human Review`. Never set `Done`: only a human sets it, through Confirm done (`confirm_done`).\n\nOperator skip:" in document


def test_document_has_the_branch_of_a_task_card() -> None:
    manager = render_prompt("stages/document.md", state="Document", labels=("role:manager-plan",))
    assert "Document of a whole task" in manager and "qa/final-check.md" in manager
    assert "not a `## Document Defect`" in manager
    plain = render_prompt("stages/document.md", state="Document")
    head, tail = plain.split("\n\nOperator skip:", 1)  # the branch sits between step 10 and the operator note
    assert manager.startswith(head) and manager.endswith("\n\nOperator skip:" + tail)
    assert manager.replace(manager[len(head):-len("\n\nOperator skip:" + tail)], "") == plain
