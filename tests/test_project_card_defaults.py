"""New card defaults apply through the normal API and CLI, without rerouting legacy cards."""
import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer

from symphony.cli import board
from symphony.errors import ConfigValidationError
from symphony.server import build_app
from symphony.trackers.file import parse_ticket_file
from symphony.workflow import WorkflowState, build_service_config, load_workflow
from tests.test_webapi import _StubOrchestrator


def workflow(tmp_path, *, agent=None, tracker=None):
    cards = tmp_path / "kanban"
    cards.mkdir(exist_ok=True)
    path = tmp_path / "WORKFLOW.md"
    data = {"tracker": {"kind":"file", "board_root":"./kanban", "active_states":["Todo","Doing"],
                        "terminal_states":["Done"], "id_prefix":"mod", **(tracker or {})},
            "agent":{"kind":"claude", "new_issue_kind":"codex", **(agent or {})}}
    path.write_text("---\n" + yaml.safe_dump(data) + "---\nProject test.")
    return path


@pytest.mark.parametrize("agent,tracker", [
    ({"new_issue_kind":"missing"}, {}), ({}, {"id_prefix":"../outside"}),
    ({"allowed_kinds":["codex"]}, {}), ({"allowed_kinds":["claude"]}, {}),
])
def test_invalid_project_card_configuration_is_rejected(tmp_path, agent, tracker):
    with pytest.raises(ConfigValidationError):
        build_service_config(load_workflow(workflow(tmp_path, agent=agent, tracker=tracker)))


def test_cli_creates_project_number_and_new_backend_but_respects_explicit_choice(tmp_path):
    path = workflow(tmp_path)
    args = ["new", "--workflow", str(path)]
    assert board.main([*args, "First"]) == 0
    front, _ = parse_ticket_file(tmp_path / "kanban/MOD-1.md")
    assert front["agent"]["kind"] == "codex"
    assert board.main([*args, "Second", "--agent-kind", "claude", "--prefix", "ALT"]) == 0
    front, _ = parse_ticket_file(tmp_path / "kanban/ALT-1.md")
    assert front["agent"]["kind"] == "claude"


@pytest.mark.asyncio
async def test_browser_api_new_defaults_are_distinct_from_legacy_dispatch(tmp_path):
    path = workflow(tmp_path)
    old = tmp_path / "kanban/MOD-8.md"
    old.write_text("---\nid: MOD-8\ntitle: Earlier work\nstate: Done\n---\nExisting.\n")
    original = old.read_bytes()
    state = WorkflowState(path)
    cfg, _ = state.reload()
    assert cfg.agent.kind_for_state("Todo", None) == "claude"
    async with TestClient(TestServer(build_app(_StubOrchestrator(state)))) as http:
        meta = (await (await http.get("/api/v1/board")).json())["board"]
        assert (meta["default_agent_kind"], meta["new_issue_kind"], meta["id_prefix"]) == ("claude","codex","MOD")
        created = await http.post("/api/v1/issues", json={"title":"Ordinary job"})
        assert created.status == 201 and (await created.json())["identifier"] == "MOD-9"
        assert parse_ticket_file(tmp_path / "kanban/MOD-9.md")[0]["agent"]["kind"] == "codex"
        assert old.read_bytes() == original
