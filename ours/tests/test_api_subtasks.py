"""An approved plan passes only checked versions to its dependent cards."""

import asyncio
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace

import pytest

from symphony import api_team
from symphony.trackers.file import write_ticket_atomic
from symphony.workflow import WorkflowState
from test_api_plan import basic_board as basic_board
from test_api_plan import part, plan_text, start
from test_api_plan import planning_board as planning_board
from test_api_project import git
from test_api_session import KEYS, exchange, frame
from test_api_workspace import tool

import api_board
import api_subtasks
import api_session as session


def children(board):
    return [api_board.Board(board.settings, {**board.context, "identifier": f"{board.identifier}-{i}"})
            for i in (1, 2)]


def tick(board):
    return api_board.sync_checks(board.settings, {**board.context, "identifier": ""})


def make_plan(board, *, check="test -s src/memo.md"):
    planned, _ = start(board, plan_text(part("memo", paths=["src/memo.md"], check=check),
                                      part("link", blocked_by=["memo"], check="test -s src/memo.md")))
    assert planned["plan"]["status"] == "ready"


@pytest.fixture()
def checks(monkeypatch):
    recorded = []
    native = sys.platform == "darwin" and os.environ.get("SYMPHONY_TEST_NATIVE_SANDBOX") == "1"
    real_run = session.sandbox.run

    def run(policy, command, *, timeout, cancelled):
        args = shlex.split(command)
        assert args[:2] == ["test", "-s"] and len(args) == 3
        state = Path(policy.workspace) / args[2]
        recorded.append((command, policy.workspace))
        if native:
            return real_run(replace(policy, executable=session.sandbox.CODEX), command,
                            timeout=timeout, cancelled=cancelled)
        return {"exit_code": 0 if state.is_file() and state.stat().st_size else 1,
                "stop_reason": "operator_stopped" if cancelled() else None, "output": "", "seconds": 0.01}

    monkeypatch.setattr(session.sandbox, "run", run)
    return recorded


def spend(child, *tools):
    transport, requests = exchange([*(frame(None, [call]) for call in tools), frame("Часть готова")])
    result = child.run("e" * 32, credentials=lambda _: KEYS, transport=transport)
    assert len(requests) == len(tools) + 1, result
    return result, requests


def memo():
    return tool("write_project_file", "write", path="src/memo.md", text="Проверенная памятка\n", expected_sha256="")


def test_one_approval_hands_checked_commit_to_dependent_card(planning_board, checks):
    board = planning_board
    before = git(board.repo, "show-ref")
    make_plan(board)
    first, second = children(board)
    tick(board)
    assert not first.path.exists() and not second.path.exists()
    prepared = board.prepare_subtasks()
    token = prepared["confirmation_token"]
    assert prepared["plan"]["launch"]["total"] == 2 and not first.path.exists()
    with pytest.raises(api_subtasks.PlanError, match="invalid_api_confirmation"):
        board.start_subtasks("unrelated-token")
    board.start_subtasks(token)
    queued = first.path.read_bytes()
    assert first.card()[0]["state"] == "Todo" and not second.path.exists()
    board.start_subtasks(token)
    assert first.path.read_bytes() == queued

    result, _ = spend(first, memo())
    assert result["subtask"]["check"]["status"] == "passed", result
    assert "release" not in result and first.card()[0]["state"] == "Verify"
    assert not second.path.exists()
    tick(board)
    assert first.card()[0]["state"] == "Done" and second.card()[0]["state"] == "Todo"
    assert second._load()["spec"]["project"]["commit"] == result["candidate"]["commit"]
    assert second._load()["spec"]["execution"]["write_paths"] == ["README.md"]

    read = tool("read_project_file", "read", path="src/memo.md", offset=0, max_chars=100)
    forbidden = tool("write_project_file", "outside", path="src/new.py", text="No", expected_sha256="")
    readme = hashlib.sha256((board.repo / "README.md").read_bytes()).hexdigest()
    write = tool("write_project_file", "link", path="README.md", text="[Мануал](src/memo.md)\n", expected_sha256=readme)
    result, requests = spend(second, read, forbidden, write)
    assert "Проверенная памятка" in str(requests[1]["body"]["input"])
    assert "workspace_path_denied" in str(requests[2]["body"]["input"])
    tick(board)
    assert result["subtask"]["check"]["status"] == "passed"
    status = board.show()
    assert status["plan"]["launch"]["status"] == "complete" and board.card()[0]["state"] == "Verify"
    assert status["final"]["status"] == "checking"
    assert second.card()[0]["state"] == "Done" and len(checks) == 2
    shared = board.root / "subtasks.git"
    assert git(shared, "show", result["candidate"]["commit"] + ":src/memo.md").decode().strip() == "Проверенная памятка"
    assert git(shared, "show", result["candidate"]["commit"] + ":README.md").decode().strip() == "[Мануал](src/memo.md)"
    assert git(board.repo, "show-ref") == before and git(board.repo, "status", "--porcelain") == b""
    assert all(not copy.exists() for _, copy in checks)


def test_failed_check_cannot_release_dependency_by_changing_lane(planning_board, checks):
    board = planning_board
    make_plan(board, check="test -s src/missing.md")
    board.start_subtasks(board.prepare_subtasks()["confirmation_token"])
    first, second = children(board)
    result, _ = spend(first, memo())
    assert result["reason"] == "api_subtask_check_failed" and first.card()[0]["state"] == "needs_human", result
    front, body = first.card()
    write_ticket_atomic(first.tracker.find_path(first.identifier), {**front, "state": "Done"}, body)
    tick(board)
    launch = board.show()["plan"]["launch"]
    assert launch["status"] == "stopped" and launch["checked"] == 0
    assert board.card()[0]["state"] == "needs_human"
    assert not second.path.exists()


def test_restart_repairs_partially_queued_card_and_never_replays_interrupted_child(planning_board, monkeypatch):
    board = planning_board
    make_plan(board)
    first, second = children(board)
    move = api_subtasks._move

    def interrupted(child, *args):
        if child.identifier == first.identifier:
            raise SystemExit("interrupt after saving queued assignment")
        return move(child, *args)

    token = board.prepare_subtasks()["confirmation_token"]
    with monkeypatch.context() as staged:
        staged.setattr(api_subtasks, "_move", interrupted)
        with pytest.raises(SystemExit):
            board.start_subtasks(token)
    assert first.card()[0]["state"] == "Subtasks" and first._load()["status"] == "queued"
    tick(board)
    assert first.card()[0]["state"] == "Todo"

    def aborted(*a, **kw):
        raise SystemExit("interrupt paid runner")

    monkeypatch.setattr(session, "run", aborted)
    with pytest.raises(SystemExit):
        first.run("e" * 32, credentials=lambda _: KEYS)
    tick(board)
    assert first._load()["status"] == "stopped" and not second.path.exists()
    assert board.start_subtasks(token)["plan"]["launch"]["status"] == "stopped"
    with pytest.raises(api_board.BoardError, match="api_assignment_not_queued"):
        first.run("f" * 32)


def test_stop_parent_after_queue_refuses_before_creating_session(planning_board):
    board = planning_board
    make_plan(board)
    board.start_subtasks(board.prepare_subtasks()["confirmation_token"])
    first, second = children(board)
    assert board.stop()["plan"]["launch"]["status"] == "stopped"
    result = first.run("a" * 32, credentials=lambda _: pytest.fail("Stopped before any credentials"))
    assert result["reason"] == "api_subtasks_not_active" and not first.session_path.exists()
    tick(board)
    assert not second.path.exists()


@pytest.mark.asyncio
async def test_service_advances_a_plan_with_no_cloud_profile_or_browser(planning_board, checks, monkeypatch):
    board = planning_board
    make_plan(board)
    board.start_subtasks(board.prepare_subtasks()["confirmation_token"])
    first, second = children(board)
    result, _ = spend(first, memo())
    assert result["subtask"]["check"]["status"] == "passed"
    config, error = WorkflowState(board.workflow).reload()
    assert not error and config
    orch = SimpleNamespace(_next_cloud_check=0, _cloud_check_task=None, _spawn_supervised=asyncio.create_task)
    monkeypatch.setattr(api_team, "_cloud_enabled", lambda command: False)

    async def invoke(cfg, kind, identifier, action):
        assert action == "sync-checks" and identifier == ""
        return tick(board)

    monkeypatch.setattr(api_team, "invoke", invoke)
    api_team.schedule_cloud_checks(orch, config)
    assert orch._cloud_check_task
    await orch._cloud_check_task
    assert second.card()[0]["state"] == "Todo"
