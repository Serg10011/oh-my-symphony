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
assert not any(key in os.environ for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "GEMINI_API_KEY"))
assert not Path("README.md").exists()
if sys.argv[1:] == ["models"]:
    if MODE == "catalog_failure":
        print("Private catalog diagnostic", file=sys.stderr)
        sys.exit(1)
    slug = model + "-other" if MODE == "catalog_missing" else model
    print(slug + "\tGemini 3.8 Flash")
    sys.exit(0)
settings = json.loads(sys.argv[sys.argv.index("--settings")+1])
assert settings["useG1Credits"] is False and settings["modelProvider"] == ""

def emit(event, value):
    print(json.dumps({"event": event, event: value}), flush=True)

if not Path(".agents/hooks.json").exists():
    if MODE == "connection_failure":
        sys.exit(1)
    if MODE == "wrong_model":
        model += "-other"
    Path(LOG+".ready").touch()
    emit("init", {"model": model, "agent": "symphony-controlled-worker", "permission_mode": "strict",
                  "cwd": os.getcwd(), "tools": ["finish", "view_file", "run_command"]})
    assert not sys.stdin.buffer.read()
    sys.exit(0)

hooks = json.loads(Path(".agents/hooks.json").read_text())["symphony-gate"]

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
    if MODE == "region_denied":
        emit("result", {"status": "ERROR",
                        "error": "403 Forbidden: User location is not supported for the API. Private service detail",
                        "usage": usage(0), "conversation_id": cid})
        break
    assert hook("PreToolUse")["decision"] == "allow"
    if turn == 1:
        assert len(messages) == 1
        assignment = messages[0]["assignment"]
        assert assignment == "Prepare the test." or assignment.endswith("\n\nLead's assignment (scope):\nPrepare the test.")
        assert "delegate_worker" not in {item["name"] for item in envelope["tools"]}
        assert task["resources"]["response_output_cap_enforced_by_provider"] is False
        assert "input_tokens_remaining" not in task["resources"]
        requests = [operation("read_project_file", path=path, offset=0, max_chars=4096)
                    for path in ("private.txt", "README.md")]
        if MODE == "batch_discovery":
            requests = [
                operation("read_project_file", path="README.md", offset=0, max_chars=4096),
                operation("search_project_files", queries=["committed"], prefix="src",
                          case_sensitive=True, file_offset=0, line_offset=0),
                operation("read_project_file", path="src/main.py", offset=0, max_chars=4096),
            ]
        if MODE == "oversized":
            oversized = usage(1)
            oversized.update(output_tokens=2000, total_tokens=2010)
            emit("result", {"status": "SUCCESS", "structured_output": {"text": "", "requests": requests},
                            "usage": oversized, "conversation_id": cid})
            break
    elif turn == 2:
        assert "tools" not in envelope
        if MODE == "batch_discovery":
            readme, search, source = messages
            assert readme["result"]["text"] == "Project instructions\n"
            assert search["result"]["matches"] == [
                {"path": "src/main.py", "line": 1, "text": "print('committed')"}]
            assert source["result"]["text"] == "print('committed')\n"
        else:
            denied, allowed = messages
            assert denied["arguments"]["path"] == "private.txt" and denied["result"]["error"] == "workspace_path_denied"
            assert allowed["result"]["text"] == "Project instructions\n"
        requests = [operation("write_project_file", path="tests/test_flash.py",
                              expected_sha256="", text="assert 2 + 2 == 4\n")]
    elif turn == 3:
        assert len(messages) == 1 and messages[0]["result"]["path"] == "tests/test_flash.py"
        requests = [
            operation("run_project_command", command=command, timeout_seconds=10)
            for command in ("check-before-edit", "edit-and-check", "check-final " + "я" * 256,
                            "fail-final", "timeout-final")
        ] if MODE == "handoff_checks" else []
    elif turn == 4 and MODE == "handoff_checks":
        assert len(messages) == 5 and all(m["tool"] == "run_project_command" for m in messages)
        assert all("before_revision" not in m["result"] for m in messages)
        requests = []
    else:
        assert turn == 4 and messages == [{"assignment": "Confirm the result."}]
        requests = []
    emit("result", {"status": "SUCCESS", "structured_output": {"text": "" if requests else "Ready",
                    "requests": requests}, "usage": usage(turn), "conversation_id": cid})
'''


def approve_flash(board, team=TEAM):
    preview = board.prepare(team)
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


def lead_frames(*frames, model="gpt-6.1-sol"):
    replies, recorded = iter(frames), []

    def handler(request):
        assert request.url.host == "api.openai.com"
        assert request.headers["authorization"] == "Bearer " + KEYS["OPENAI_API_KEY"]
        body = json.loads(request.content)
        assert body["model"] == model
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(200, json={"object": "response.input_tokens", "input_tokens": 80})
        recorded.append(body)
        return httpx.Response(200, json=next(replies))

    return httpx.MockTransport(handler), recorded


def sol(text=None, calls=(), *, model="gpt-6.1-sol"):
    return frame(text, calls, model=model, service_tier="default")


def assignment(**kwargs):
    return delegate(name="delegate_worker", **kwargs)


@pytest.mark.parametrize("lead", ["sol-lead", api.SOL_62_LEAD])
def test_confirmed_flash_edits_only_the_task_copy_and_is_billed_separately(board, tmp_path, monkeypatch, lead):
    path = fake_worker(tmp_path, monkeypatch)
    raw = json.loads(board.settings.read_text())
    raw["session"] = {"context": "Lead-only context", "limits": {"output_tokens_per_call": 1024},
                      "budget": {"max_usd": "1", "input_tokens_total": 1000}}
    raw["mode"] = "economy"
    board.settings.write_text(json.dumps(raw))
    (board.repo / "private.txt").write_text("Private sentinel")
    git(board.repo, "add", "private.txt")
    git(board.repo, "commit", "-qm", "fixture denied file")
    git(board.repo, "branch", "-f", "our-main", "HEAD")
    lead_model = api.profile_for(lead).model
    tariff = budget.catalog_for(lead_model)
    monkeypatch.setattr(budget, "today", lambda: datetime.fromisoformat(tariff["verified_on"]).date())
    assert approve_flash(board, {**TEAM, "lead_profile": lead})["status"] == "queued"
    transport, sent = lead_frames(
        sol(calls=[assignment(message="Prepare the test.")], model=lead_model),
        sol(calls=[tool("read_project_file", "verify", path="tests/test_flash.py", offset=0, max_chars=4096),
                   tool("raise_lead_effort", "raise", reason="Review the complete change")], model=lead_model),
        sol(calls=[assignment(call_id="correction", kind="correction", message="Confirm the result.")],
            model=lead_model),
        sol("Проверен тест в копии задачи.", model=lead_model),
        model=lead_model,
    )

    def credentials(_):
        assert Path(str(path) + ".ready").is_file()
        return {"OPENAI_API_KEY": KEYS["OPENAI_API_KEY"]}

    result = board.run("a" * 32, credentials=credentials, transport=transport)
    assert result["status"] == "result", result.get("report_text")
    assert [request["reasoning"]["effort"] for request in sent] == ["high", "high", "max", "max"]
    if lead == api.SOL_62_LEAD:
        prefix = sent[0]["input"][0]
        assert prefix["role"] == "developer" and "Lead-only context" in prefix["content"]
        assert all("instructions" not in body and body["input"][0] == prefix for body in sent)
    assert "assert 2 + 2 == 4" in json.dumps(sent[2]["input"])
    assert result["session"]["parts"] == {"part_1": {"attempts": 2}}
    assert result["session"]["budget"]["preflight_requests"] == 4
    assert result["session"]["budget"]["input_tokens_committed"] == 48
    assert result["session"]["usage"][api.FLASH_WORKER]["total_tokens"] == 60
    report = session.report(board.session_path)
    assert report["cost"]["estimate_complete"] and report["cost"]["estimated_requests"] == 4
    assert report["cost"]["requests_without_estimate"] == 0
    assert report["cost"]["tariff"]["version"] == tariff["version"]
    assert report["by_model"][lead_model]["estimated_requests"] == 4
    assert report["by_model"][api.FLASH_MODEL]["estimated_usd"] is None
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    saved = session._load(board.session_path)
    flash_calls = [row for row in saved["calls"] if row["profile"] == api.FLASH_WORKER]
    assert len(rows) == len(flash_calls) == 4 and len({row["pid"] for row in rows}) == 1
    assert [hashlib.sha256(row["wire"].encode()).hexdigest() for row in rows] == [
        call["request_sha256"] for call in flash_calls]
    first = json.loads(rows[0]["wire"])["message"]["content"]
    envelope = json.loads("{" + first.split("\n{", 1)[1])
    task = saved["spec"]["task"]
    expected = "Prepare the test."
    if lead == api.SOL_62_LEAD:
        expected = f"Architect's task (reference):\n{task}\n\nLead's assignment (scope):\n{expected}"
        assert session._ORIGINAL_TASK_CONTEXT in envelope["instructions"]
    assert envelope["task_data"]["messages"] == [{"assignment": expected}]
    assert all(task not in json.loads(row["wire"])["message"]["content"] for row in rows[1:])
    assert board.review()["files"][0]["path"] == "tests/test_flash.py"
    assert not (board.repo / "tests/test_flash.py").exists()
    assert not list(board.root.glob("symphony-flash-*"))


def test_flash_receives_a_batch_of_independent_reads_and_searches(board, tmp_path, monkeypatch):
    log = fake_worker(tmp_path, monkeypatch, "batch_discovery")
    approve_flash(board, {**TEAM, "lead_profile": api.SOL_62_LEAD})
    model = api.profile_for(api.SOL_62_LEAD).model
    transport, sent = lead_frames(sol(calls=[assignment(message="Prepare the test.")], model=model),
                                  sol("Проверен результат.", model=model), model=model)

    result = board.run("a" * 32, credentials=lambda _: KEYS, transport=transport)

    assert result["status"] == "result", result.get("report_text")
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    contents = [json.loads(row["wire"])["message"]["content"] for row in rows]
    turns = [json.loads("{" + content.split("\n{", 1)[1]) for content in contents]
    assert len(turns) == 3
    assert turns[0]["instructions"].count(session.FLASH_BATCH_INSTRUCTIONS) == 1
    assert all("instructions" not in turn for turn in turns[1:])
    assert all(session.FLASH_BATCH_INSTRUCTIONS not in body["input"][0]["content"] for body in sent)
    saved = session._load(board.session_path)
    flash_calls = [call for call in saved["calls"] if call["profile"] == api.FLASH_WORKER]
    assert len(flash_calls) == 3
    assert [call["name"] for call in flash_calls[0]["response"]["function_calls"]] == [
        "read_project_file", "search_project_files", "read_project_file",
    ]


@pytest.mark.parametrize(("mode", "reason", "tokens"), [
    ("lost", "flash_worker_failed", None),
    ("limited", "flash_capacity_unavailable", 15),
    ("region_denied", "flash_region_unsupported", 0),
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
    assert f"причина остановки: {session.REPORT_STOP_REASONS[reason]}" in result["report_text"]
    assert result["session"]["usage"][api.FLASH_WORKER]["total_tokens"] == tokens
    stored = session._load(board.session_path)
    assert not stored["file_calls"]
    assert "Private service detail" not in json.dumps(stored)
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


@pytest.mark.parametrize(("mode", "reason"), [
    ("catalog_failure", "flash_catalog_unavailable"),
    ("catalog_missing", "flash_model_unavailable"),
    ("connection_failure", "flash_connection_unavailable"),
    ("wrong_model", "flash_startup_contract_mismatch"),
])
def test_subscription_readiness_failure_stops_before_sol(board, tmp_path, monkeypatch, mode, reason):
    wire = fake_worker(tmp_path, monkeypatch, mode)
    approve_flash(board)
    result = board.run(
        "a" * 32, credentials=lambda _: pytest.fail("The Sol key must not be read"),
        transport=httpx.MockTransport(lambda _: pytest.fail("No API request before worker readiness")),
    )
    assert result["status"] == "stopped" and result["reason"] == reason
    assert result["session"]["calls"] == 0 and not wire.exists()
    front, body = board.card()
    assert front["state"] == "needs_human"
    assert session.FLASH_READINESS_REASONS[reason] in body and "Private catalog diagnostic" not in body
    assert "Private catalog diagnostic" not in result["report_text"]
    assert not list(board.root.glob("symphony-flash-ready-*"))
