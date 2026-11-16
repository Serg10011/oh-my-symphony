"""An approved plan passes only checked versions to its dependent cards."""

import asyncio
from dataclasses import replace
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace
from typing import cast

from aiohttp.test_utils import TestClient, TestServer
import pytest

from symphony import api_team
from symphony.orchestrator import Orchestrator
from symphony.server import build_app
from symphony.trackers.file import write_ticket_atomic
from symphony.workflow import WorkflowState
from test_api_flash_board import assignment, fake_worker, lead_frames, sol
from test_api_plan import basic_board as basic_board
from test_api_plan import part, plan_text, start
from test_api_plan import planning_board as planning_board
from test_api_project import git
from test_api_session import KEYS, exchange, frame
from test_api_workspace import tool
from tests.test_webapi import _StubOrchestrator

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


def stopped_flash_part(board, tmp_path, monkeypatch, *, checked_first=False, legacy=False):
    settings = json.loads(board.settings.read_text())
    settings.update(session={"lead_profile": "sol-lead", "worker_profile": api_board.api.FLASH_WORKER}, mode="economy")
    if checked_first:
        monkeypatch.setattr(session.budget, "today", lambda: date.fromisoformat(session.budget.CATALOG["verified_on"]))
        settings["session"]["limits"] = {"output_tokens_per_call": 1024}
        settings["session"]["budget"] = {"max_usd": "1", "input_tokens_total": 10000}
    board.settings.write_text(json.dumps(settings))
    fake_worker(tmp_path, monkeypatch, "region_denied")
    plan = plan_text(part("memo", paths=["src/memo.md"], check="test -s src/memo.md"),
                     part("link", blocked_by=["memo"], check="test -s src/memo.md"))
    board.confirm(board.prepare()["confirmation_token"])
    transport, _ = lead_frames(sol(plan))
    planned = board.run("a" * 32, credentials=lambda _: KEYS, transport=transport)
    assert planned["plan"]["status"] == "ready", planned.get("report_text")
    token = board.prepare_subtasks()["confirmation_token"]
    board.start_subtasks(token)
    first, second = children(board)
    if checked_first:
        transport, _ = lead_frames(sol(calls=[memo()]), sol("Готово"))
        passed = first.run("e" * 32, credentials=lambda _: KEYS, transport=transport)
        assert passed["subtask"]["check"]["status"] == "passed", passed
        tick(board)
    child = second if checked_first else first
    if legacy:
        old_stop = session._stop

        def stop(path, state, reason):
            if reason == "flash_region_unsupported":
                reason = "flash_worker_failed"
                state["calls"][-1]["error"]["error"] = "flash_worker_stopped"
            return old_stop(path, state, reason)

        monkeypatch.setattr(session, "_stop", stop)
    transport, requests = lead_frames(sol(calls=[assignment(message="Prepare the test.")]))
    result = child.run("f" * 32, credentials=lambda _: KEYS, transport=transport)
    assert result["reason"] == ("flash_worker_failed" if legacy else "flash_region_unsupported"), result
    assert len(requests) == 1
    tick(board)
    assert board.card()[0]["state"] == "needs_human"
    return first, second, token


@pytest.mark.parametrize("checked_first", [False, True])
def test_confirmed_retry_reuses_saved_plan_and_cost_history(planning_board, checks, tmp_path, monkeypatch, checked_first):
    board = planning_board
    first, second, old_token = stopped_flash_part(board, tmp_path, monkeypatch,
                                                 checked_first=checked_first, legacy=not checked_first)
    child = second if checked_first else first
    old_file = child.session_path
    stopped_session = old_file.read_bytes()
    parent_session = board.session_path.read_bytes()
    old_assignment = child.path.read_bytes()
    stopped = api_subtasks._load(board)
    original_base = stopped["terms"]["spec"]["project"]["commit"]
    first_assignment = first.path.read_bytes()
    if not checked_first:
        (board.repo / "new-version.txt").write_text("Installed after the plan\n")
        git(board.repo, "add", "new-version.txt")
        git(board.repo, "commit", "-m", "New base")
        original = api_board.Board.proposal

        def new_release(self, *args, **kwargs):
            result = original(self, *args, **kwargs)
            result["binding"] = hashlib.sha256(("updated controller:" + result["binding"]).encode()).hexdigest()
            return result

        monkeypatch.setattr(api_board.Board, "proposal", new_release)

    before = board.show()["plan"]["launch"]
    assert before["retry"] == {"identifier": child.identifier, "base_commit": original_base}
    prepared = board.prepare_subtasks()
    assert prepared["plan"]["launch"]["status"] == "prepared"
    assert prepared["plan"]["launch"]["checked"] == int(checked_first)
    board.start_subtasks(old_token)
    assert child.path.read_bytes() == old_assignment and child.card()[0]["state"] == "needs_human"
    assert api_subtasks._load(board)["status"] == "stopped"
    token = prepared["confirmation_token"]
    move = api_subtasks._move

    def interrupted(selected, value, *args):
        if selected.identifier == child.identifier and value.get("attempt_id"):
            raise SystemExit("interrupt retry after saving assignment")
        return move(selected, value, *args)

    with monkeypatch.context() as staged:
        staged.setattr(api_subtasks, "_move", interrupted)
        with pytest.raises(SystemExit):
            board.start_subtasks(token)
    assert child.card()[0]["state"] == "needs_human" and child._load()["status"] == "queued"
    tick(board)
    queued = child.path.read_bytes()
    assert child.card()[0]["state"] == "Todo" and child.session_path != old_file
    board.start_subtasks(token)
    assert child.path.read_bytes() == queued and old_file.read_bytes() == stopped_session
    run = api_subtasks._load(board)
    assert run["terms"] == stopped["terms"] and run["checked"] == stopped["checked"]
    assert len(run["previous_attempts"]) == 1
    assert run["previous_attempts"][0]["session_file"] == old_file.name
    assert run["previous_attempts"][0]["spend"]["gpt-6.1-sol"]["calls"] == 1
    assert board.session_path.read_bytes() == parent_session
    assert f"{child.identifier}, остановка 1 · gpt-6.1-sol" in board.show()["report_text"]
    if checked_first:
        assert first.path.read_bytes() == first_assignment and first.card()[0]["state"] == "Done"
        prior = session.budget.totals(json.loads(stopped_session))
        expected = api_subtasks._remaining(stopped["terms"]["spec"]["budget"], [stopped["checked"][0]["usage"], prior])
        assert child._load()["spec"]["budget"] == prepared["plan"]["launch"]["budget"] == expected
    else:
        assert not second.path.exists()
        transport, requests = lead_frames(sol(calls=[memo()]), sol("Готово"))
        result = first.run("1" * 32, credentials=lambda _: KEYS, transport=transport)
        assert len(requests) == 2 and result["subtask"]["check"]["status"] == "passed"
        tick(board)
        assert second.card()[0]["state"] == "Todo"
    sha = hashlib.sha256((board.repo / "README.md").read_bytes()).hexdigest()
    write = tool("write_project_file", "link", path="README.md", text="[Мануал](src/memo.md)\n", expected_sha256=sha)
    transport, requests = lead_frames(sol(calls=[write]), sol("Готово"))
    result = second.run("2" * 32, credentials=lambda _: KEYS, transport=transport)
    assert len(requests) == 2 and result["subtask"]["check"]["status"] == "passed"
    tick(board)
    assert api_subtasks._load(board)["status"] == "complete"
    assert board.card()[0]["state"] == "Verify" and len(checks) == 2
    assert board._load()["candidate"]["base_commit"] == original_base


def test_retry_refuses_changed_card_after_preview(planning_board, tmp_path, monkeypatch):
    board = planning_board
    first, second, _ = stopped_flash_part(board, tmp_path, monkeypatch)
    token = board.prepare_subtasks()["confirmation_token"]
    front, body = first.card()
    write_ticket_atomic(first.tracker.find_path(first.identifier), front, body + "\nNew instruction.\n")
    with pytest.raises(api_subtasks.PlanError, match="api_plan_subtask_changed"):
        board.start_subtasks(token)
    assert api_subtasks._load(board)["status"] == "stopped"
    assert board.card()[0]["state"] == "needs_human" and not second.path.exists()


def test_retry_requires_the_closed_session_for_cost_history(planning_board, tmp_path, monkeypatch):
    board = planning_board
    first, second, _ = stopped_flash_part(board, tmp_path, monkeypatch)
    first.session_path.rename(first.session_path.with_name("missing-session.json"))
    with pytest.raises(api_subtasks.PlanError, match="api_subtask_retry_unavailable"):
        board.prepare_subtasks()
    assert api_subtasks._load(board)["status"] == "stopped" and not second.path.exists()


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("SYMPHONY_BROWSER_E2E") != "1", reason="needs Chromium")
async def test_browser_confirms_retry_from_the_parent_card(planning_board, tmp_path, monkeypatch):
    playwright = pytest.importorskip("playwright.async_api")
    board = planning_board
    first, _, _ = stopped_flash_part(board, tmp_path, monkeypatch)
    workflow = WorkflowState(board.workflow)
    workflow.reload()
    app = build_app(cast(Orchestrator, _StubOrchestrator(workflow)))
    actions = []

    async def invoke(cfg, kind, identifier, action, **kw):
        actions.append(action)
        selected = api_board.Board(board.settings, api_team.board_context(cfg, kind, identifier))
        if action == "prepare-subtasks":
            return selected.prepare_subtasks()
        if action == "start-subtasks":
            return selected.start_subtasks(kw["payload"]["confirmation_token"])
        return selected.show() if action == "show" else {}

    monkeypatch.setattr(api_team, "invoke", invoke)
    async with TestClient(TestServer(app)) as client, playwright.async_playwright() as driver:
        browser = await driver.chromium.launch()
        try:
            page = await browser.new_page()
            await page.add_init_script("localStorage.setItem('symphony.lang', 'ru')")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(str(client.make_url("/#/board")), wait_until="networkidle")
            card = page.locator(".card", has_text="Исправь функцию")
            if not await card.count():
                await page.get_by_role("button", name="Все", exact=True).click()
            await card.click()
            panel = page.get_by_role("region", name="API-команда")
            await playwright.expect(panel).to_contain_text("по региону подключения")
            confirm = panel.get_by_role("button", name="Подтвердить повтор подзадачи")
            await playwright.expect(confirm).to_be_hidden()
            await panel.get_by_role("button", name="Посмотреть повтор подзадачи").click()
            await playwright.expect(panel).to_contain_text(f"Повтор {first.identifier} на базе {board._base_commit()[:12]}")
            await playwright.expect(panel).to_contain_text("Уже проверено частей — 0")
            assert "start-subtasks" not in actions and first.card()[0]["state"] == "needs_human"
            await confirm.click()
            await playwright.expect(panel).to_contain_text(f"Текущая — {first.identifier}")
            await playwright.expect(confirm).to_be_hidden()
            assert first.card()[0]["state"] == "Todo"
            assert actions.count("start-subtasks") == 1 and not errors
        finally:
            await browser.close()


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
