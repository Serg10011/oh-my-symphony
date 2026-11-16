"""A read-only plan creates idle child cards and can resume partial writes."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import cast

from aiohttp.test_utils import TestClient, TestServer
import pytest

from symphony import api_team
from symphony.orchestrator import Orchestrator
from symphony.server import build_app
from symphony.trackers.file import parse_ticket_file, write_ticket_atomic
from symphony.workflow import WorkflowState
from test_api_board import approve, board as basic_board  # noqa: F401
from test_api_session import KEYS, exchange, frame
from test_plan_hook import LEVELS, plan_text, sub
from tests.test_webapi import _StubOrchestrator

import api_board

OURS = Path(__file__).resolve().parents[1]


@pytest.fixture()
def planning_board(request):
    board = request.getfixturevalue("basic_board")
    folder = board.workflow.parent
    command = shlex.join([sys.executable, "-B", str(OURS / "hooks/api_board.py"), "--settings", str(board.settings)])
    (folder / "project.toml").write_text(f"""\
[project]
name = "board"
repo = {json.dumps(str(board.repo))}
base_branch = "our-main"
id_prefix = "TEST"
port = 9971
[tools]
python = {json.dumps(sys.executable)}
symphony = "/unused/symphony"
claude = "/unused/claude"
[api_team]
settings_file = "api-settings.json"
{LEVELS}
""")
    board.workflow.write_text(f"""\
---
workspace:
  code_repo: {json.dumps(str(board.repo))}
tracker:
  kind: file
  board_root: ./kanban
  manual_start: true
  active_states: [Todo, Plan, In Progress, Verify, Document]
  terminal_states: [Subtasks, Human Review, needs_human, Done, Archive]
agent:
  kind: claude
  feature_base_branch: our-main
  profiles:
    api-team:
      format: api-team-v1
      command: {json.dumps(command)}
---
""")
    config, error = WorkflowState(board.workflow).reload()
    assert config is not None, error
    path = folder / "kanban/TEST-1.md"
    front, body = parse_ticket_file(path)
    write_ticket_atomic(path, {**front, "state": "Todo", "labels": ["role:api-plan"]}, body)
    return api_board.Board(board.settings, api_team.board_context(config, "api-team", "TEST-1"))


def part(key, **changes):
    return sub(key, **({"title": f"Часть {key}", "criteria": ["Результат описан в README.md"],
                       "check": "test -s README.md", "paths": ["README.md"]} | changes))


def start(board, plan, *, model="deepseek-flash", credentials=lambda _: KEYS):
    approve(board)
    transport, requests = exchange([frame(plan, model=model)])
    result = board.run("a" * 32, credentials=credentials, transport=transport)
    assert len(requests) == 1
    return result, requests[0]["body"]


def test_plan_uses_only_lead_and_stages_children(planning_board, monkeypatch):
    board = planning_board
    settings = json.loads(board.settings.read_text())
    settings.update(session={"lead_profile": "sol-lead", "worker_profile": api_board.api.FLASH_WORKER}, mode="economy")
    board.settings.write_text(json.dumps(settings))
    monkeypatch.setattr(api_board.gemini_quota, "blocked_until", lambda *a: pytest.fail("Must not check worker quota"))
    original_body = board.card()[1]

    def credentials(spec):
        assert spec["worker_profile"] is None and "execution" not in spec
        return KEYS

    result, request = start(board, plan_text(part("one"), part("two", blocked_by=["one"])),
                            model="gpt-6.1-sol", credentials=credentials)
    assert result["status"] == "result", result
    assert result["plan"]["status"] == "ready"
    assert result["board_stages"] == ["Todo", "Plan", "Subtasks"]
    assert result["profiles"]["lead"]["effort"] == "high" and result["profiles"]["worker"] is None
    assert "Return only the requested TOML" in request["instructions"]
    assert {tool["name"] for tool in request["tools"]} == {
        "list_project_files", "read_project_file", "search_project_files", "raise_lead_effort",
    }
    assert board.card()[0]["state"] == "Subtasks" and board.card()[1] == original_body
    assert "candidate" not in result and result["delivery_status"] == "not_configured"

    first, second = result["plan"]["cards"]
    assert first["identifier"] == "TEST-1-1" and first["blocked_by"] == []
    assert second["identifier"] == "TEST-1-2" and second["blocked_by"] == ["TEST-1-1"]
    for card in (first, second):
        child = api_board.Board(board.settings, {**board.context, "identifier": card["identifier"]})
        front, _ = child.card()
        assert front["request"] == "TEST-1"
        assert front.get("blocked_by", []) == card["blocked_by"]
        assert front["state"] == "Subtasks" and front["labels"] == ["role:api-subtask"]
        assert child.show()["plan_draft"] and not child.path.exists()
        path = child.tracker.find_path(child.identifier)
        assert path is not None
        write_ticket_atomic(path, {**front, "state": "Todo"}, child.card()[1])
        with pytest.raises(api_board.BoardError, match="api_plan_subtask_draft"):
            child.prepare()


@pytest.mark.parametrize("plan", [
    plan_text(part("one"), part("two", paths=["docs/other.md"])),
    plan_text(part("one", blocked_by=["two"]), part("two")),
    "Broken plan",
])
def test_invalid_plan_rejects_entire_batch(planning_board, plan):
    result, _ = start(planning_board, plan)
    assert result["status"] == "stopped" and result["reason"] == "api_plan_invalid", result
    assert result["plan"]["errors"] and "Проверка плана" in result["report_text"]
    assert planning_board.card()[0]["state"] == "needs_human"
    assert planning_board.tracker.list_all_identifiers() == {"TEST-1"}


@pytest.mark.parametrize("failure", [OSError, SystemExit])
def test_partial_write_recovers_from_saved_reply_without_new_requests(planning_board, monkeypatch, failure):
    board = planning_board
    create = board.tracker.create_validated

    def interrupted(**kwargs):
        if kwargs["identifier"] == "TEST-1-2":
            raise failure("write interrupted")
        return create(**kwargs)

    monkeypatch.setattr(board.tracker, "create_validated", interrupted)
    plan = plan_text(part("one"), part("two", blocked_by=["one"]))
    if failure is SystemExit:
        with pytest.raises(SystemExit, match="write interrupted"):
            start(board, plan)
        assert board._load()["status"] == "running"
        result = board.show()
        assert result["reason"] == "board_execution_failed"
    else:
        result, _ = start(board, plan)
        assert result["reason"] == "api_plan_staging_failed", result
    assert result["plan"]["status"] == "staging"
    assert board.card()[0]["state"] == "needs_human"
    first = board.board_root / "TEST-1-1.md"
    preserved = first.read_bytes()
    saved_reply = board.session_path.read_bytes()
    recovered = subprocess.run([sys.executable, "-B", OURS / "bin/api-plan", board.workflow,
                                "--recover", board.identifier], check=True, capture_output=True, text=True)
    assert json.loads(recovered.stdout)["plan"]["status"] == "ready"
    assert board.recover_plan()["plan"]["status"] == "ready"
    assert first.read_bytes() == preserved and board.session_path.read_bytes() == saved_reply
    assert sorted(board.tracker.list_all_identifiers()) == ["TEST-1", "TEST-1-1", "TEST-1-2"]


@pytest.mark.asyncio
@pytest.mark.browser_e2e
@pytest.mark.skipif(os.environ.get("SYMPHONY_BROWSER_E2E") != "1", reason="needs Chromium")
async def test_browser_shows_staged_subtasks_and_hides_their_launch(planning_board, monkeypatch):
    playwright = pytest.importorskip("playwright.async_api")
    board = planning_board
    result, _ = start(board, plan_text(part("one"), part("two", blocked_by=["one"])))
    assert result["plan"]["status"] == "ready"
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
            page = await browser.new_page(viewport={"width": 1280, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(str(client.make_url("/#/board")))
            lane = page.locator('[data-board-scroll="terminal:Subtasks"]')
            await playwright.expect(lane.locator(".card")).to_have_count(3)
            await lane.locator(".card", has_text="Исправь функцию").click()
            panel = page.get_by_role("region", name="API-команда")
            await playwright.expect(panel).to_contain_text("План проверен. Черновиков подзадач — 2.")
            await page.keyboard.press("Escape")
            child = lane.locator(".card", has_text="Часть two")
            await playwright.expect(child.locator("[title*=TEST-1-1]")).to_have_count(1)
            await child.click()
            await playwright.expect(panel).to_contain_text("Черновик подзадачи")
            await playwright.expect(panel.get_by_role("button", name="Посмотреть назначение")).to_be_hidden()
            assert actions.count("show") >= 2 and "prepare" not in actions and not errors
            await page.keyboard.press("Escape")
            await lane.locator(".card", has_text="Исправь функцию").click()
            await panel.get_by_role("button", name="Посмотреть запуск подзадач").click()
            await playwright.expect(panel).to_contain_text("Подзадач — 2. Ведущий")
            assert "start-subtasks" not in actions
            await panel.get_by_role("button", name="Подтвердить запуск подзадач").click()
            await playwright.expect(panel).to_contain_text("Проверено подзадач — 0 из 2. Текущая — TEST-1-1.")
            assert actions.count("start-subtasks") == 1 and "run" not in actions and not errors
        finally:
            await browser.close()
