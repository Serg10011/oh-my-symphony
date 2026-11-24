"""Neon uses the card's confirmation and one sealed subscription workspace."""

import json
import sys

import pytest

from test_api_board import board as board_fixture
from test_api_candidate import configure
from test_api_project import git

import api_executor as api
import api_neon
import api_session as session

board = board_fixture
TEAM = {"lead_profile": api.NEON_SOLO, "worker_profile": None}


def enable(board):
    settings = json.loads(board.settings.read_text())
    board.settings.write_text(json.dumps({**settings, "neon": True}))


def test_neon_requires_board_opt_in_and_remains_a_solo_card_choice(board):
    assert TEAM not in board.show()["team_picker"]["pairs"]
    with pytest.raises(session.SessionError, match="neon_disabled"):
        board.prepare(TEAM)
    enable(board)
    assert TEAM in board.show()["team_picker"]["pairs"]
    assert TEAM not in board.teams.directory([])["pairs"]
    with pytest.raises(session.SessionError, match="neon_card_only"):
        board.teams.save(TEAM)
    with pytest.raises(session.SessionError, match="invalid_composition"):
        board.prepare({**TEAM, "worker_profile": api.FLASH_WORKER})
    assert board.teams.default()["lead_profile"] != api.NEON_SOLO
    assert not board.session_path.exists()


def _fake_cli(tmp_path, monkeypatch, *, outside=False, wrong_model=False, exhausted=False):
    marker = tmp_path / "native-prompt"
    script = tmp_path / "agy-fixture"
    script.write_text(f"#!{sys.executable}\n" + f"MARKER = {str(marker)!r}\nOUTSIDE = {outside!r}\n"
                      + f"EXHAUSTED = {exhausted!r}\n"
                      + f"WRONG_MODEL = {wrong_model!r}\n" + r'''
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

root = Path.cwd()
settings = json.loads(sys.argv[sys.argv.index("--settings") + 1])
assert settings["useG1Credits"] is False and settings["modelProvider"] == ""
assert not settings["allowNonWorkspaceAccess"] and "--sandbox" in sys.argv
assert "--dangerously-skip-permissions" not in sys.argv
assert not any(key in os.environ for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "GEMINI_API_KEY"))
model = sys.argv[sys.argv.index("--model") + 1]

def emit(event, value):
    print(json.dumps({"event": event, event: value}), flush=True)

emit("init", {"model": model if not WRONG_MODEL else "unexpected",
              "permission_mode": "request-review", "cwd": str(root)})
message = sys.stdin.readline()
if not message:
    sys.exit(0)
assert json.loads(message)["event"] == "user"
assert not sys.stdin.read()
with Path(MARKER).open("x") as receipt:
    receipt.write("one native turn")

if EXHAUSTED:
    emit("result", {"status": "ERROR", "error": "RESOURCE_EXHAUSTED", "usage": {"input_tokens": 27}})
    sys.exit(1)

hook = json.loads((root / ".agents/hooks.json").read_text())["symphony-neon-scope"]["PreToolUse"][0]["hooks"][0]
def ask(name, **args):
    data = {"workspacePaths": [str(root)], "toolCall": {"name": name, "args": args}}
    result = subprocess.run(shlex.split(hook["command"]), input=json.dumps(data), text=True, capture_output=True, check=True)
    return json.loads(result.stdout)["decision"]

assert ask("read_file", Path="src/main.py") == "allow"
assert ask("edit_file", Path="src/main.py") == "allow"
assert ask("write_file", Path=".agents/hooks.json") == "deny"
assert ask("write_file", Path="../outside") == "deny"
assert ask("run_command", CommandLine="ls -la") == "allow"
assert ask("run_command", CommandLine="ls && pwd") == "deny"
assert ask("call_mcp_tool", Name="irrelevant") == "deny"
(root / "src/main.py").write_text("answer = 5\n")
if OUTSIDE:
    (root / "out-of-scope.txt").write_text("must not reach the candidate")
emit("result", {"status": "SUCCESS", "response": "Changed the source.",
                "usage": {"input_tokens": 127, "output_tokens": 20, "total_tokens": 147}})
''')
    script.chmod(0o700)
    monkeypatch.setattr(api_neon.probe, "subscription_executable", lambda: script)
    return marker


def _approved(board):
    enable(board)
    configure(board)
    board.context.update(active_states=["Todo", "In Progress", "Verify"],
                         terminal_states=["Human Review", "needs_human", "Done"])
    board.review_lane = "Human Review"
    prepared = board.prepare(TEAM)
    assert prepared["profiles"]["lead"]["model"] == api.NEON_MODEL
    assert prepared["profiles"]["worker"] is None and prepared["budget"] is None
    token = prepared["confirmation_token"]
    board.confirm(token)
    return token


def test_neon_uses_one_confirmed_turn_and_standard_candidate(board, tmp_path, monkeypatch):
    marker = _fake_cli(tmp_path, monkeypatch)
    token = _approved(board)
    assert not marker.exists()
    before = git(board.repo, "rev-parse", "HEAD")
    result = board.run("a" * 32)
    assert marker.read_text() == "one native turn"
    assert result["status"] == "result"
    assert result["candidate"]["status"] == "awaiting_full_checks"
    assert result["board_stages"][:3] == ["Todo", "In Progress", "Verify"]
    assert result["session"]["neon"] == {
        "extra_credits_enabled": False, "usage": {"input_tokens": 127, "output_tokens": 20, "total_tokens": 147},
    }
    assert "Подписка Antigravity" in result["report_text"]
    assert board.confirm(token)["status"] == "result"
    assert git(board.repo, "rev-parse", "HEAD") == before


@pytest.mark.parametrize(("mode", "reason"), [
    ("outside", "neon_scope_changed"),
    ("wrong_model", "neon_startup_failed"),
])
def test_neon_does_not_publish_a_bad_result(board, tmp_path, monkeypatch, mode, reason):
    marker = _fake_cli(tmp_path, monkeypatch, outside=mode == "outside", wrong_model=mode == "wrong_model")
    _approved(board)
    result = board.run("b" * 32)
    assert result["status"] == "stopped"
    assert result["reason"] == reason
    assert board.card()[0]["state"] == "needs_human"
    assert "candidate" not in result
    assert marker.exists() == (mode == "outside")


def test_neon_preserves_usage_on_provider_error(board, tmp_path, monkeypatch):
    _fake_cli(tmp_path, monkeypatch, exhausted=True)
    _approved(board)
    result = board.run("c" * 32)
    assert result["reason"] == "neon_quota_exhausted"
    assert result["session"]["neon"]["usage"] == {"input_tokens": 27}
    assert board.card()[0]["state"] == "needs_human"
