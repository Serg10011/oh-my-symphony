"""A subscribed worker follows the same approved card, workspace and report."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys

import httpx
import pytest

from test_api_board import board as board_fixture
from test_api_project import git
from test_api_session import KEYS, delegate, frame
from test_api_workspace import tool

import api_budget as budget
import api_executor as api
import api_flash_probe as flash
import api_gemini_quota as quota
import api_session as session

board = board_fixture
TEAM = {"lead_profile": "sol-lead", "worker_profile": api.FLASH_WORKER}

CLI = r'''
import json, os, shlex, subprocess, sys
from pathlib import Path

cid = "00000000-0000-4000-8000-000000000001"
model = "gemini-3.8-flash-high"
settings = json.loads(sys.argv[sys.argv.index("--settings")+1])
assert settings["useG1Credits"] is False and settings["modelProvider"] == ""
assert not any(key in os.environ for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "GEMINI_API_KEY"))
assert not Path("README.md").exists()
hooks = json.loads(Path(".agents/hooks.json").read_text())["symphony-gate"]

def emit(event, value):
    print(json.dumps({"event": event, event: value}), flush=True)

def hook(event):
    handler = hooks[event][0]
    if event == "PreToolUse":
        handler = handler["hooks"][0]
    payload = {"conversationId": cid, "workspacePaths": [os.getcwd()], "modelName": model,
               "toolCall": {"name": "finish"}}
    result = subprocess.run(shlex.split(handler["command"]), input=json.dumps(payload),
                            text=True, capture_output=True, check=True)
    return json.loads(result.stdout)

def usage(turn):
    return {"input_tokens": 10 * turn, "output_tokens": 3 * turn, "cache_read_tokens": 2 * turn,
            "thinking_tokens": turn, "total_tokens": 13 * turn}

def operation(name, **arguments):
    return {"name": name, "arguments": arguments}

emit("init", {"model": model, "agent": "symphony-controlled-worker", "permission_mode": "strict",
              "cwd": os.getcwd(), "tools": ["finish", "view_file", "run_command"]})
for turn, line in enumerate(sys.stdin.buffer, 1):
    content = json.loads(line)["message"]["content"]
    if turn == 1:
        hook("SessionStart")
    gate = hook("PreInvocation")
    if gate.get("terminationBehavior") == "terminate":
        assert "SYMPHONY_GATE_CHECK" in content and "private.txt" not in content
        emit("result", {"status": "ERROR", "error": "SYMPHONY_FLASH_GATE_CLOSED", "usage": usage(0)})
        break
    with Path(LOG).open("a") as log:
        log.write(json.dumps({"pid": os.getpid(), "wire": line.decode()})+"\n")
    envelope = json.loads("{" + content.split("\n{", 1)[1])
    assert "Private sentinel" not in content and "opaque-worker-reasoning" not in content
    assert "Lead-only context" not in content
    task = envelope["task_data"]
    assert task["part_id"] == "part_1"
    messages = task["messages"]
    if MODE == "lost":
        print("broken terminal stream", flush=True)
        break
    if MODE == "limited":
        emit("result", {"status": "ERROR", "error": "RESOURCE_EXHAUSTED", "usage": usage(1),
                        "conversation_id": cid})
        break
    assert hook("PreToolUse")["decision"] == "allow"
    if turn == 1:
        assert messages == [{"assignment": "Prepare the test."}]
        assert "delegate_worker" not in {item["name"] for item in envelope["tools"]}
        assert task["resources"]["response_output_cap_enforced_by_provider"] is False
        assert "input_tokens_remaining" not in task["resources"]
        requests = [operation("read_project_file", path=path, offset=0, max_chars=4096)
                    for path in ("private.txt", "README.md")]
        if MODE == "oversized":
            oversized = usage(1)
            oversized.update(output_tokens=2000, total_tokens=2010)
            emit("result", {"status": "SUCCESS", "structured_output": {"text": "", "requests": requests},
                            "usage": oversized, "conversation_id": cid})
            break
    elif turn == 2:
        assert len(messages) == 2 and "tools" not in envelope
        denied, allowed = messages
        assert denied["arguments"]["path"] == "private.txt" and denied["result"]["error"] == "workspace_path_denied"
        assert allowed["result"]["text"] == "Project instructions\n"
        requests = [operation("write_project_file", path="tests/test_flash.py",
                              expected_sha256="", text="assert 2 + 2 == 4\n")]
    elif turn == 3:
        assert len(messages) == 1 and messages[0]["result"]["path"] == "tests/test_flash.py"
        requests = []
    else:
        assert turn == 4 and messages == [{"assignment": "Confirm the result."}]
        requests = []
    emit("result", {"status": "SUCCESS", "structured_output": {"text": "" if requests else "Ready",
                    "requests": requests}, "usage": usage(turn), "conversation_id": cid})
'''


def approve_flash(board):
    preview = board.prepare(TEAM)
    assert preview["profiles"]["worker"]["provider"] == "antigravity"
    assert preview["profiles"]["lead"]["effort"] == "high" and preview["lead_escalation"] == "high-to-max"
    return board.confirm(preview["confirmation_token"])


def fake_worker(tmp_path, monkeypatch, mode="normal"):
    executable = tmp_path / "fake-agy"
    log = tmp_path / "flash-wire.jsonl"
    executable.write_text(f"#!{sys.executable}\nMODE={mode!r}\nLOG={str(log)!r}\n" + CLI)
    executable.chmod(0o700)
    monkeypatch.setattr(flash, "subscription_executable", lambda executable_arg=None: executable)
    return log


def lead_frames(*frames):
    replies, recorded = iter(frames), []

    def handler(request):
        assert request.url.host == "api.openai.com"
        assert request.headers["authorization"] == "Bearer " + KEYS["OPENAI_API_KEY"]
        body = json.loads(request.content)
        assert body["model"] == "gpt-6.1-sol"
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 80})
        recorded.append(body)
        return httpx.Response(200, json=next(replies))

    return httpx.MockTransport(handler), recorded


def sol(text=None, calls=()):
    return frame(text, calls, model="gpt-6.1-sol", service_tier="default")


def assignment(**kwargs):
    return delegate(name="delegate_worker", **kwargs)


def test_confirmed_flash_edits_only_the_task_copy_and_is_billed_separately(board, tmp_path, monkeypatch):
    path = fake_worker(tmp_path, monkeypatch)
    raw = json.loads(board.settings.read_text())
    raw["session"] = {"context": "Lead-only context", "limits": {"output_tokens_per_call": 1024},
                      "budget": {"max_usd": "1", "input_tokens_total": 1000}}
    board.settings.write_text(json.dumps(raw))
    (board.repo / "private.txt").write_text("Private sentinel")
    git(board.repo, "add", "private.txt")
    git(board.repo, "commit", "-qm", "fixture denied file")
    git(board.repo, "branch", "-f", "our-main", "HEAD")
    monkeypatch.setattr(budget, "today", lambda: datetime.fromisoformat(budget.CATALOG["verified_on"]).date())
    assert approve_flash(board)["status"] == "queued"
    transport, sent = lead_frames(
        sol(calls=[assignment(message="Prepare the test.")]),
        sol(calls=[tool("read_project_file", "verify", path="tests/test_flash.py", offset=0, max_chars=4096),
                   tool("raise_lead_effort", "raise", reason="Review the complete change")]),
        sol(calls=[assignment(call_id="correction", kind="correction", message="Confirm the result.")]),
        sol("Проверен тест в копии задачи."),
    )
    result = board.run("a" * 32, credentials=lambda _: {"OPENAI_API_KEY": KEYS["OPENAI_API_KEY"]},
                       transport=transport)
    assert result["status"] == "result", result.get("report_text")
    assert [request["reasoning"]["effort"] for request in sent] == ["high", "high", "max", "max"]
    assert "assert 2 + 2 == 4" in json.dumps(sent[2]["input"])
    assert result["session"]["parts"] == {"part_1": {"attempts": 2}}
    assert result["session"]["budget"]["preflight_requests"] == 4
    assert result["session"]["budget"]["input_tokens_committed"] == 48
    assert result["session"]["usage"][api.FLASH_WORKER]["total_tokens"] == 60
    report = session.report(board.session_path)
    assert report["cost"]["estimate_complete"] and report["cost"]["estimated_requests"] == 4
    assert report["cost"]["requests_without_estimate"] == 0
    assert report["by_model"][api.FLASH_MODEL]["estimated_usd"] is None
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    flash_calls = [row for row in session._load(board.session_path)["calls"] if row["profile"] == api.FLASH_WORKER]
    assert len(rows) == len(flash_calls) == 4 and len({row["pid"] for row in rows}) == 1
    assert [hashlib.sha256(row["wire"].encode()).hexdigest() for row in rows] == [
        call["request_sha256"] for call in flash_calls]
    assert board.review()["files"][0]["path"] == "tests/test_flash.py"
    assert not (board.repo / "tests/test_flash.py").exists()
    assert not list(board.root.glob("symphony-flash-*"))


@pytest.mark.parametrize(("mode", "reason", "tokens"), [
    ("lost", "flash_worker_failed", None),
    ("limited", "flash_capacity_unavailable", 15),
    ("oversized", "flash_output_limit", 2012),
])
def test_flash_failure_preserves_known_usage_or_uncertainty_without_retry(board, tmp_path, monkeypatch,
                                                                        mode, reason, tokens):
    log = fake_worker(tmp_path, monkeypatch, mode)
    raw = json.loads(board.settings.read_text())
    raw["session"]["limits"] = {"output_tokens_per_call": 1024}
    board.settings.write_text(json.dumps(raw))
    approve_flash(board)
    transport, sent = lead_frames(sol(calls=[assignment(message="Prepare the test.")]))
    result = board.run("a" * 32, credentials=lambda _: KEYS, transport=transport)
    assert result["status"] == "stopped" and result["reason"] == reason
    assert result["session"]["usage"][api.FLASH_WORKER]["total_tokens"] == tokens
    assert not session._load(board.session_path)["file_calls"]
    assert len(sent) == len(log.read_text().splitlines()) == 1
    assert not list(board.root.glob("symphony-flash-*"))
    report = session.report(board.session_path)
    assert report["cost"]["estimate_complete"] and report["cost"]["estimated_requests"] == 1


def test_recent_zero_quota_stops_before_credentials_and_keeps_reset(board, monkeypatch):
    reset = (datetime.now(timezone.utc) + timedelta(minutes=30)).replace(microsecond=0)
    sample = quota.normalize({"quota": {"gemini-5h": {"remaining_fraction": 0, "reset_time": reset.isoformat()}}})
    monkeypatch.setattr(quota, "read", lambda **_: sample)
    assert board.teams.refresh_gemini_quota()["gemini_quota"]["buckets"]["five_hour"]["remaining_percent"] == 0
    assert board.teams.directory([])["gemini_quota"]["provider_refresh_verified"] is False
    approve_flash(board)
    result = board.run("a" * 32, credentials=lambda _: pytest.fail("No credentials with exhausted quota"))
    assert result["status"] == "stopped" and result["reason"] == "flash_quota_exhausted"
    assert result["gemini_wait_until"] == reset.isoformat() and not board.session_path.exists()
    assert quota.blocked_until(sample, now=reset) is None


def test_missing_cli_stops_before_openai_key_access(board, monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    approve_flash(board)
    result = board.run("a" * 32, credentials=lambda _: pytest.fail("No credentials before local readiness"))
    assert result["status"] == "stopped" and result["reason"] == "flash_cli_unavailable"
    assert result["session"]["calls"] == 0
