"""The opt-in API bridge cannot inherit legacy paid retries or browser bypasses."""

import asyncio
from dataclasses import replace
from pathlib import Path
import shlex
import sys
from typing import cast

from aiohttp.test_utils import TestClient, TestServer
import pytest
import pytest_asyncio

from symphony import api_team
from symphony.backends import BackendInit, build_backend
from symphony.errors import ConfigValidationError, SymphonyError
from symphony.orchestrator import Orchestrator
from symphony.orchestrator.executors import LegacyStageExecutor, TicketRunContext
from symphony.server import build_app
from symphony.trackers.file import FileBoardTracker
from symphony.workflow import WorkflowState
from tests.test_backends import _noop_event
from tests.test_orchestrator_max_retries import _entry_for_exit
from tests.test_webapi import _StubOrchestrator, board_dir  # noqa: F401


@pytest.fixture()
def workflow(request):
    path = request.getfixturevalue("board_dir") / "WORKFLOW.md"
    path.write_text(path.read_text().replace("[Done, Archive]", "[Done, Archive, needs_human]")
        .replace("kind: claude", "kind: claude\n  profiles:\n    api-team:\n      format: api-team-v1\n"
                 "      command: /absolute/trusted/runner\n      turn_timeout_ms: 7200000"))
    state = WorkflowState(path)
    cfg, error = state.reload()
    assert error is None and cfg is not None
    return state


@pytest_asyncio.fixture()
async def client(workflow):
    stub = _StubOrchestrator(workflow)
    app = build_app(cast(Orchestrator, stub))
    async with TestClient(TestServer(app)) as client:
        client.stub = stub
        yield client


def test_whole_ticket_strategy_is_opt_in_and_cli_fallback_is_refused(workflow, tmp_path):
    cfg = workflow.current()
    orch = Orchestrator(workflow)
    assert isinstance(orch._executor_for(cfg, "claude"), LegacyStageExecutor)
    assert isinstance(orch._executor_for(cfg, "api-team"), api_team.ApiTeamExecutor)
    api_cfg = replace(cfg, agent=replace(cfg.agent, kind="api-team"))
    with pytest.raises(ConfigValidationError, match="whole-ticket"):
        build_backend(BackendInit(cfg=api_cfg, cwd=tmp_path, workspace_root=tmp_path, on_event=_noop_event))


@pytest.mark.parametrize("extra", ["resume_across_turns: true", "continuation_flag: --resume", "unattended_flags: [--yes]"])
def test_api_profile_refuses_legacy_resume_flags(workflow, extra):
    path = workflow.current().workflow_path
    path.write_text(path.read_text().replace("format: api-team-v1", "format: api-team-v1\n      " + extra))
    _, error = workflow.reload()
    assert error and "do not support" in str(error)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["api-team", " API-TEAM "])
async def test_new_api_card_waits_but_legacy_card_remains_executable(client, kind):
    response = await client.post("/api/v1/issues", json={"title": "API task", "agent_kind": kind, "state": "Todo"})
    assert response.status == 201
    data = await response.json()
    identifier = data["identifier"]
    detail = await (await client.get(f"/api/v1/issues/{identifier}")).json()
    assert detail["state"] == "needs_human"
    for state in ("Todo", "Done"):
        response = await client.patch(f"/api/v1/issues/{identifier}", json={"state": state})
        assert response.status == 409
    response = await client.post("/api/v1/issues", json={"title": "Legacy task"})
    assert response.status == 201
    data = await response.json()
    detail = await (await client.get(f"/api/v1/issues/{data['identifier']}")).json()
    assert detail["state"] == "Todo"
    board = await (await client.get("/api/v1/board")).json()
    assert board["board"]["api_team_kinds"] == ["api-team"]


async def select_api(client):
    response = await client.patch("/api/v1/issues/SEED-1", json={"agent_kind": "api-team"})
    assert response.status == 200


@pytest.mark.asyncio
async def test_browser_actions_are_same_origin_and_never_expose_run(client, monkeypatch):
    await select_api(client)
    calls = []
    async def invoke(cfg, kind, identifier, action, **kw):
        calls.append((kind, identifier, action, kw))
        return {"status": "prepared", "confirmation_token": "dummy-token"}
    monkeypatch.setattr(api_team, "invoke", invoke)
    prefix = "/api/v1/issues/SEED-1/api-team"
    origin = str(client.make_url("/")).rstrip("/")
    response = await client.post(prefix + "/prepare", json={}, headers={"Origin": origin})
    assert response.status == 200 and response.headers["Cache-Control"] == "no-store, private"
    assert [x[2] for x in calls] == ["prepare"]
    for headers in ({"Origin": "http://127.0.0.1:9"}, {"Origin": "null"}, {"Origin": "https://evil.invalid"},
                    {"Sec-Fetch-Site": "same-site"}, {"Referer": origin + "/"}):
        response = await client.post(prefix + "/confirm", json={"confirmation_token": "dummy-token"}, headers=headers)
        assert response.status == 403
    assert len(calls) == 1
    response = await client.post(prefix + "/confirm", json={"confirmation_token": "dummy-token"}, headers={"Origin": origin})
    assert response.status == 200 and calls[-1][2] == "confirm"
    assert calls[-1][3]["payload"] == {"confirmation_token": "dummy-token"}
    for action in ("run", "shell", "resume"):
        response = await client.post(prefix + "/" + action, json={}, headers={"Origin": origin})
        assert response.status == 404
    response = await client.get(prefix)
    assert response.status == 200 and calls[-1][2] == "show"


@pytest.mark.asyncio
async def test_unselected_card_cannot_prepare_paid_assignment(client, monkeypatch):
    async def forbidden(*a, **kw):
        pytest.fail("No bridge process for unselected card")
    monkeypatch.setattr(api_team, "invoke", forbidden)
    response = await client.post("/api/v1/issues/SEED-1/api-team/prepare", json={})
    assert response.status == 409


def runner_config(workflow, tmp_path, body, *, timeout=5000):
    path = tmp_path / "trusted runner with spaces.py"
    path.write_text(body)
    cfg = workflow.current()
    profile = replace(cfg.agent.profiles["api-team"], turn_timeout_ms=timeout,
        command=shlex.join([str(Path(sys.executable).resolve()), str(path)]))
    return replace(cfg, agent=replace(cfg.agent, profiles={"api-team": profile}))


@pytest.mark.asyncio
async def test_bridge_has_fixed_argv_and_clean_environment(workflow, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-never-forwarded")
    monkeypatch.setenv("PYTHONPATH", "/private/untrusted")
    cfg = runner_config(workflow, tmp_path, "import sys,json,os\nmsg=json.load(sys.stdin)\n"
        "print(json.dumps({'message':msg,'argv':sys.argv[1:],'leaked':[k for k in os.environ if k in ['OPENAI_API_KEY','PYTHONPATH']]}))\n")
    result = await api_team.invoke(cfg, "api-team", "SEED-1", "prepare")
    assert result["argv"] == ["--action", "prepare"] and result["leaked"] == []
    assert result["message"]["context"]["identifier"] == "SEED-1"
    with pytest.raises(SymphonyError, match="Неизвестное"):
        await api_team.invoke(cfg, "api-team", "SEED-1", "run; echo unsafe")


@pytest.mark.asyncio
async def test_bridge_bounds_output_and_hides_untrusted_error_body(workflow, tmp_path, monkeypatch):
    monkeypatch.setattr(api_team, "MAX_REPLY_BYTES", 1024)
    cfg = runner_config(workflow, tmp_path, "print('x'*2048)")
    with pytest.raises(SymphonyError, match="размер"):
        await api_team.invoke(cfg, "api-team", "SEED-1", "show")
    cfg = runner_config(workflow, tmp_path, "print('{\"error\":\"dummy secret credential\"}')")
    with pytest.raises(SymphonyError) as exc:
        await api_team.invoke(cfg, "api-team", "SEED-1", "show")
    assert "dummy secret" not in str(exc.value)


@pytest.mark.asyncio
async def test_bridge_run_timeout_terminates_child(workflow, tmp_path):
    marker = tmp_path / "completed"
    cfg = runner_config(workflow, tmp_path, "import sys,json,time,pathlib\njson.load(sys.stdin)\n"
        f"time.sleep(0.5)\npathlib.Path({str(marker)!r}).write_text('should not run')\nprint('{{}}')", timeout=50)
    with pytest.raises(TimeoutError):
        await api_team.invoke(cfg, "api-team", "SEED-1", "run", run_id="a" * 32)
    await asyncio.sleep(.6)
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["api_team_finished", "api_team_failed", "error", "cancelled", "shutdown_interrupted"])
async def test_any_api_exit_pauses_instead_of_legacy_retry(workflow, monkeypatch, reason):
    orch = Orchestrator(workflow)
    orch._loop = asyncio.get_running_loop()
    entry = _entry_for_exit("TEST-1")
    entry.agent_kind = "api-team"
    orch._running[entry.issue.id] = entry
    async def refreshed(*a):
        return None
    monkeypatch.setattr(orch, "_refresh_issue_state", refreshed)
    monkeypatch.setattr(orch, "_schedule_retry", lambda *a, **kw: pytest.fail("No paid automatic retry"))
    await orch._on_worker_exit(entry.issue.id, reason, None)
    assert entry.issue.id not in orch._running and orch.is_paused(entry.issue.id)


@pytest.mark.asyncio
async def test_removed_profile_cannot_restore_legacy_retry_for_existing_run(workflow, monkeypatch):
    orch = Orchestrator(workflow)
    entry = _entry_for_exit("TEST-1")
    entry.agent_kind, entry.execution_mode = "api-team", "api-team-v1"
    orch._running[entry.issue.id] = entry
    cfg = workflow.current()
    workflow._config = replace(cfg, agent=replace(cfg.agent, profiles={}))
    monkeypatch.setattr(orch, "_schedule_retry", lambda *a, **kw: pytest.fail("No fallback after config reload"))
    await orch._on_worker_exit(entry.issue.id, "error", "test error")
    assert orch.is_paused(entry.issue.id)


@pytest.mark.asyncio
async def test_executor_runs_one_session_and_uses_owned_cleanup(workflow, tmp_path, monkeypatch):
    orch = Orchestrator(workflow)
    entry = _entry_for_exit("TEST-1")
    entry.agent_kind = "api-team"
    entry.worker_task = asyncio.current_task()
    orch._running[entry.issue.id] = entry
    calls, exits = [], []
    async def invoke(cfg, kind, identifier, action, **kwargs):
        calls.append(action)
        await kwargs["heartbeat"]()
        return {"status": "result"}
    async def finish(*args, **kw):
        exits.append((args, kw))
    monkeypatch.setattr(api_team, "invoke", invoke)
    monkeypatch.setattr(orch, "_on_worker_exit", finish)
    context = TicketRunContext(entry.issue, None, workflow.current(), "a" * 32, tmp_path, "api-team", "run")
    await api_team.ApiTeamExecutor(orch).execute(context)
    assert calls == ["run"] and exits[0][0][1] == "api_team_finished"
    assert exits[0][1]["owning_task"] is asyncio.current_task()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_before_start", [False, True])
async def test_real_dispatch_keeps_registry_lease_and_never_runs_legacy_stages(workflow, monkeypatch, cancel_before_start):
    cfg = workflow.current()
    tracker = FileBoardTracker(cfg.tracker)
    tracker.update_fields("SEED-1", agent_kind="api-team")
    issue = tracker.fetch_issue_full_by_id("SEED-1")
    orch = Orchestrator(workflow)
    orch._loop = asyncio.get_running_loop()
    orch._ensure_run_registry(cfg)
    calls = []
    async def invoke(cfg, kind, identifier, action, **kw):
        calls.append(action)
        assert len(kw["run_id"]) == 32
        await kw["heartbeat"]()
        return {"status": "result"}
    async def legacy(*args):
        pytest.fail("API assignment cannot enter the legacy stage loop")
    monkeypatch.setattr(api_team, "invoke", invoke)
    monkeypatch.setattr(orch, "run_legacy_stage_loop", legacy)
    try:
        assert orch._dispatch(issue, cfg, attempt=None)
        entry = orch._running[issue.id]
        assert entry.execution_mode == "api-team-v1"
        if cancel_before_start:
            entry.worker_task.cancel()
        await asyncio.gather(entry.worker_task, return_exceptions=True)
        for _ in range(20):
            if issue.id not in orch._running:
                break
            await asyncio.sleep(.01)
        assert issue.id not in orch._running and orch.is_paused(issue.id)
        assert calls == ([] if cancel_before_start else ["run"])
        record = orch._run_registry.get_run(entry.run_id)
        assert record.completed_at is not None
        if not cancel_before_start:
            assert record.failure_class is None
    finally:
        orch._run_registry.close()


def test_api_panel_is_loaded_before_app_and_uses_nested_board_metadata():
    root = Path(__file__).resolve().parents[1] / "src/symphony/web/static"
    html = (root / "index.html").read_text()
    assert html.index('/static/api-team.js') < html.index('/static/app.js')
    assert "state.board?.board?.api_team_kinds" in (root / "app.js").read_text()
    assert "innerHTML" not in (root / "api-team.js").read_text()
