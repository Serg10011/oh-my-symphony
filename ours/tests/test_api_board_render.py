"""Persistent API configuration through real project-render, without paid calls."""

import json
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from test_project_kit import Kit, PROJECT_RENDER
from symphony import api_team
from symphony.workflow import build_service_config, load_workflow

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import api_board as board_api  # noqa: E402
import api_board_models as models  # noqa: E402
import project_config as pc  # noqa: E402


@pytest.fixture()
def kit(tmp_path):
    result = Kit(tmp_path)
    result.ready()
    (result.board / "api-settings.json").write_text(json.dumps({
        "version": 1, "paths": ["README.md", "tests"], "session": {},
        "execution": {"python": str(Path(sys.executable).resolve()),
                      "read_roots": [str(Path(sys.base_prefix).resolve())]},
    }))
    return result


def configure(kit, *, default=False):
    path = kit.board / "project.toml"
    text = path.read_text()
    if default:
        text = "\n".join(line for line in text.splitlines() if not line.startswith("claude = ")) + "\n"
    path.write_text(text + '\n[api_team]\nsettings_file = "api-settings.json"\n'
                    + f'default = {str(default).lower()}\n')


def render(kit):
    return subprocess.run([sys.executable, str(PROJECT_RENDER), "demo", "--root", str(kit.root)],
                          capture_output=True, text=True, env=kit.env(), timeout=30)


@pytest.mark.parametrize("default", [False, True])
def test_profile_survives_render_and_keeps_defaults_and_card_history(kit, default):
    configure(kit, default=default)
    assert render(kit).returncode == 0
    cfg = build_service_config(load_workflow(kit.board / "WORKFLOW.md"))
    assert cfg.agent.kind == ("api-team" if default else "claude")
    profile = api_team.api_profile(cfg, "api-team")
    assert profile is not None
    command = shlex.split(profile.command)
    assert command[0] == pc.load_project(kit.board)["tools"]["python"]
    assert command[-2:] == ["--settings", str(kit.board / "api-settings.json")]
    assert profile.turn_timeout_ms == 7_200_000 and profile.stall_timeout_ms == 2_100_000
    assert not cfg.agent.auto_merge_on_done and not cfg.agent.auto_merge_push_target
    if default:
        assert cfg.hooks.after_create is None and not cfg.agent.auto_commit_on_done
    else:
        assert "worktree.py" in cfg.hooks.after_create

    context = api_team.board_context(cfg, "api-team", "")
    teams = models.Teams(kit.board / "api-settings.json", context)
    teams.save({"lead_profile": "sol-lead", "worker_profile": None})
    previous = kit.board / ".ours-state/api-team/KIT-1/assignment.json"
    previous.parent.parent.mkdir(mode=0o700)
    previous.parent.mkdir(mode=0o700)
    previous.write_text('{"historical_result": true}\n')
    card = kit.board / "kanban/KIT-1.md"
    card.write_text('---\nid: KIT-1\ntitle: History\nstate: Human Review\n---\nDo not rewrite.\n')
    first = (kit.board / "WORKFLOW.md").read_bytes()
    assert render(kit).returncode == 0
    assert (kit.board / "WORKFLOW.md").read_bytes() == first
    assert teams.default() == {"lead_profile": "sol-lead", "worker_profile": None}
    assert previous.read_text() == '{"historical_result": true}\n'
    assert "Do not rewrite." in card.read_text()
    assert not (kit.board / ".ours-state/api-team/KIT-2").exists()
    # New assignments use the registered runner and the same saved board default.
    card2 = kit.board / "kanban/KIT-2.md"
    card2.write_text('---\nid: KIT-2\ntitle: Fresh\nstate: Todo\nagent_kind: api-team\n---\nRead the file.\n')
    context["identifier"] = "KIT-2"
    assignment = board_api.Board(kit.board / "api-settings.json", context).prepare()
    assert assignment["profiles"]["lead"]["name"] == "sol-lead"
    assert assignment["profiles"]["worker"] is None
    assert not (kit.board / ".ours-state/api-team/KIT-2/session.json").exists()


@pytest.mark.parametrize("change", ["missing", "symlink", "json", "extra", "model", "limits", "scope", "credentials"])
def test_bad_settings_never_replace_existing_workflow_or_state(kit, change):
    configure(kit, default=True)
    path = kit.board / "api-settings.json"
    value = json.loads(path.read_text())
    if change == "missing":
        path.unlink()
    elif change == "symlink":
        saved = kit.board / "other.json"
        path.rename(saved)
        path.symlink_to(saved)
    elif change == "json":
        path.write_text('{"version": 1, "version": 1}')
    else:
        if change == "extra":
            value["cloud_check"] = "untrusted-profile"
        elif change == "model":
            value["session"]["lead_profile"] = "unregistered-model"
        elif change == "limits":
            value["session"]["limits"] = {"max_calls": 0}
        elif change == "scope":
            value["paths"] = [".git"]
        else:
            value["session"]["api_key"] = "must-not-be-accepted-or-printed"
        path.write_text(json.dumps(value))
    workflow = kit.board / "WORKFLOW.md"
    original = workflow.read_bytes()
    state = kit.board / ".ours-state/keep.json"
    state.write_text("keep\n")
    result = render(kit)
    assert result.returncode == 1 and "API-команды не приняты" in result.stderr
    assert "must-not-be-accepted-or-printed" not in result.stdout + result.stderr
    assert workflow.read_bytes() == original and state.read_text() == "keep\n"


@pytest.mark.parametrize("settings", [{}, {"settings_file": "../outside.json"},
    {"settings_file": "/outside.json"}, {"settings_file": "api.json", "typo": True},
    {"settings_file": "api.json", "stall_timeout_ms": 1},
    {"settings_file": "api.json", "turn_timeout_ms": 300_000, "stall_timeout_ms": 300_000}])
def test_invalid_project_options_are_refused(kit, settings):
    project = pc.load_project(kit.board)
    # Pass the original TOML, rather than the normalized project with defaults.
    import tomllib

    data = tomllib.loads((kit.board / "project.toml").read_text())
    data["api_team"] = settings
    with pytest.raises(pc.ProjectError, match="api_team"):
        pc.validate_project(data)
    assert not project["api_team"]


def test_api_default_refuses_unimplemented_integration_and_legacy_still_requires_claude(kit):
    import tomllib

    data = tomllib.loads((kit.board / "project.toml").read_text())
    data["api_team"] = {"settings_file": "api-settings.json", "default": True}
    data["integration"] = {"enabled": True}
    with pytest.raises(pc.ProjectError, match="составные API-задачи"):
        pc.validate_project(data)
    data.pop("api_team")
    data["tools"].pop("claude")
    with pytest.raises(pc.ProjectError, match="tools.*claude"):
        pc.validate_project(data)


def test_api_doctor_needs_no_claude_and_never_runs_host_tests_or_reads_keys(kit):
    configure(kit, default=True)
    (kit.bin / "claude").unlink()
    marker = kit.tmp / "forbidden-host-tests"
    (kit.board / "test.sh").write_text(f'echo executed > "{marker}"\n')
    old_settings = kit.board / "claude-settings.json"
    previous = old_settings.read_bytes()
    doctor = PROJECT_RENDER.with_name("project-doctor")
    checked = subprocess.run([sys.executable, str(doctor), "demo", "--root", str(kit.root)],
        capture_output=True, text=True, env=kit.env(), timeout=60)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "полный прогон выполняется отдельно в облаке" in checked.stdout
    assert "ключи здесь не читаются" in checked.stdout
    assert not marker.exists() and not kit.setup_marker.exists()
    assert not (kit.board / ".ours-state/baseline.json").exists()
    assert old_settings.read_bytes() == previous
