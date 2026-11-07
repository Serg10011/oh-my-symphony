"""Existing mixed boards retain their SSD binding and legacy card history."""

import json
from pathlib import Path
import subprocess
import sys

from aiohttp.test_utils import TestClient, TestServer
import pytest

from test_project_kit import Kit, PROJECT_RENDER
import project_config as pc
import project_connect as connect
from symphony import api_team
from symphony.server import build_app
from symphony.trackers.file import parse_ticket_file
from symphony.workflow import WorkflowState
from tests.test_webapi import _StubOrchestrator


@pytest.fixture
def mixed(tmp_path, monkeypatch):
    kit = Kit(tmp_path)
    kit.ready()
    physical = tmp_path / "SSD" / "workspaces"
    physical.mkdir(parents=True)
    (kit.home / "symphony_workspaces").symlink_to(physical, target_is_directory=True)
    monkeypatch.setattr(Path, "home", lambda: kit.home)
    request = {"project": "demo", "repo": str(kit.code), "base": "dev", "paths": ["README.md"],
               "team": {"lead_profile": "deepseek-lead", "worker_profile": "luna-worker"},
               "mode": "economy", "verification": "manual"}
    settings = connect.settings(request)
    settings.pop("route")
    (kit.board / "api-settings.json").write_text(json.dumps(settings))
    config = kit.board / "project.toml"
    config.write_text(config.read_text().replace("# enabled = false", "enabled = true", 1)
                      + '\n[api_team]\nsettings_file = "api-settings.json"\ndefault = false\nnew_cards = true\n')
    card = kit.board / "kanban/KIT-1.md"
    card.write_text('---\nid: KIT-1\nidentifier: KIT-1\ntitle: Previous plan\nstate: Done\n'
                    'labels: ["role:manager-plan"]\nagent:\n  kind: claude\n---\nAccepted legacy result.\n')
    history = kit.board / ".ours-state/legacy-result.json"
    history.write_text('{"result": "accepted"}\n')
    return kit, request, physical / "demo"


def render(kit):
    result = subprocess.run([sys.executable, str(PROJECT_RENDER), "demo", "--root", str(kit.root)],
                            env=kit.env(), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("existing", [False, True])
def test_render_and_reconnect_agree_after_ssd_normalization(mixed, existing):
    kit, request, physical = mixed
    if existing:
        physical.mkdir()
    original = {p: p.read_bytes() for p in [kit.board / "kanban/KIT-1.md", kit.board / ".ours-state/legacy-result.json"]}
    render(kit)
    state = WorkflowState(kit.board / "WORKFLOW.md")
    cfg, error = state.reload()
    assert error is None
    assert cfg.workspace_root == physical
    assert not physical.exists() or existing
    assert cfg.agent.kind == "claude" and cfg.agent.new_issue_kind == "api-team"
    assert "Plan" in cfg.tracker.active_states and "Subtasks" in cfg.tracker.terminal_states
    assert "plan.py" in cfg.hooks.after_run and "integrate.py" in cfg.hooks.after_run
    assert pc.load_project(kit.board)["integration"]["enabled"]
    settings = json.loads((kit.board / "claude-settings.json").read_text())
    assert f"{physical}/**/AGENTS.md" in settings["claudeMdExcludes"]
    workflow = kit.board / "WORKFLOW.md"
    text = workflow.read_text()
    # The host's post-merge step must now be a no-op, rather than change a
    # workflow that the constructor subsequently refuses to recognize.
    assert text.replace(str(kit.home / "symphony_workspaces/demo"), str(physical)) == text
    assert connect.ensure_profile(kit.root, kit.board, {"request": request})[1] is False
    render(kit)
    assert workflow.read_text() == text
    assert connect.ensure_profile(kit.root, kit.board, {"request": request})[1] is False
    assert all(p.read_bytes() == data for p, data in original.items())


@pytest.mark.parametrize("change", ["other_directory", "retarget_registration", "hook", "extra_text"])
def test_ssd_binding_does_not_relax_configuration_checks(mixed, change):
    kit, request, physical = mixed
    render(kit)
    workflow = kit.board / "WORKFLOW.md"
    text = workflow.read_text()
    if change == "retarget_registration":
        registration = kit.home / "symphony_workspaces"
        registration.unlink()
        elsewhere = kit.tmp / "elsewhere"
        elsewhere.mkdir()
        registration.symlink_to(elsewhere, target_is_directory=True)
    elif change == "other_directory":
        workflow.write_text(text.replace(str(physical), str(kit.tmp / "other-project")))
    elif change == "hook":
        workflow.write_text(text.replace("plan.py", "unexpected.py"))
    else:
        workflow.write_text(text + "\n")
    with pytest.raises(connect.ConnectError, match="board_configuration_differs"):
        connect.ensure_profile(kit.root, kit.board, {"request": request})
    assert not (kit.board / ".ours-state/api-team").exists()


@pytest.mark.parametrize(("mode", "effort"), [("economy", "high"), ("productivity", "max")])
@pytest.mark.asyncio
async def test_new_card_inherits_api_pair_while_legacy_history_remains(mixed, mode, effort):
    kit, request, _ = mixed
    policy = kit.board / "api-settings.json"
    settings = json.loads(policy.read_text()) | {"mode": mode}
    policy.write_text(json.dumps(settings))
    render(kit)
    previous = (kit.board / "kanban/KIT-1.md").read_bytes()
    history = (kit.board / ".ours-state/legacy-result.json").read_bytes()
    state = WorkflowState(kit.board / "WORKFLOW.md")
    cfg, error = state.reload()
    assert error is None
    async with TestClient(TestServer(build_app(_StubOrchestrator(state)))) as http:
        response = await http.post("/api/v1/issues", json={"title": "Update the instructions"})
        assert response.status == 201, await response.text()
        identifier = (await response.json())["identifier"]
        assert identifier.startswith("KIT-") and identifier != "KIT-1"
        front, _ = parse_ticket_file(kit.board / "kanban" / (identifier + ".md"))
        assert front["agent"]["kind"] == "api-team"
        assert front["api_defaults"]["team"] == request["team"]
        assert front["api_defaults"]["mode"] == mode
        assert "authorization_id" not in front["api_defaults"]
        origin = {"Origin": str(http.make_url("/")).rstrip("/")}
        prepared = await http.post(f"/api/v1/issues/{identifier}/api-team/prepare", json={}, headers=origin)
        assert prepared.status == 200, await prepared.text()
        assignment = await prepared.json()
        assert assignment["profiles"]["lead"]["effort"] == effort
        assert assignment["profiles"]["worker"]["effort"] == "max"
        assert not api_team.confirmed_in_queue(cfg, identifier)
        assert not (kit.board / f".ours-state/api-team/{identifier}/session.json").exists()
    assert (kit.board / "kanban/KIT-1.md").read_bytes() == previous
    assert (kit.board / ".ours-state/legacy-result.json").read_bytes() == history
    assert not list((kit.board / ".ours-state/api-permissions").glob("*/permission.json"))
