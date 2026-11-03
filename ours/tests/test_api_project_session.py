"""Read-only tools across lead and worker histories without real providers."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import sys

import pytest

from test_api_project import repository
from test_api_session import KEYS, delegate, exchange, frame
import test_api_budget as guarded

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ours/hooks"))
import api_project as project  # noqa: E402
import api_session as session  # noqa: E402


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch):
    for name in [*KEYS, "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(session.budget, "today", lambda: date(2026, 11, 3))


def state_file(tmp_path, root, **changes):
    path = tmp_path / "project-session.json"
    session.create(path, {"task": "Review the committed project", "context": "PRIVATE_LEAD_CONTEXT",
                          "project": {"root": str(root), "paths": ["src", "README.md"]}, **changes})
    return path


def tool(name="read_project_file", call_id="file_1", **changes):
    args = {"path": "src/main.py", "offset": 0, "max_chars": 4096}
    return {"type": "function_call", "call_id": call_id, "name": name,
            "arguments": json.dumps(changes or args)}


def test_lead_and_worker_read_independently_without_spending_correction_attempt(tmp_path):
    root = repository(tmp_path)
    path = state_file(tmp_path, root)
    transport, sent = exchange([
        frame(None, [tool("list_project_files", "shared_id", prefix="", offset=0)]),
        frame(None, [tool(call_id="lead_read")]),
        frame(None, [delegate("assign")]),
        frame(None, [tool(call_id="shared_id")], model="gpt-6-luna"),
        frame("Worker reviewed the committed code", model="gpt-6-luna"),
        frame("Lead reviewed the result")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and result["calls"] == 6
    assert result["parts"]["part_1"]["attempts"] == 1 and result["project"]["file_calls"] == 3
    assert result["project"]["read_only"] and result["task_acceptance"] == "not_evaluated"
    raw = json.loads(path.read_text())
    assert all(call["status"] == "completed" for call in raw["file_calls"])
    assert [call["part_id"] for call in raw["file_calls"]] == [None, None, "part_1"]
    assert session.show(path) == result
    assert str(root) not in json.dumps(sent)
    workers = [r for r in sent if r["body"]["model"] == "gpt-6-luna"]
    assert all({t["name"] for t in r["body"]["tools"]} == project.TOOL_NAMES for r in workers)
    assert all("PRIVATE_LEAD_CONTEXT" not in json.dumps(r) and "private-lead-reasoning" not in json.dumps(r)
               for r in workers)
    assert "opaque-worker-reasoning" in json.dumps(workers[1])
    assert "opaque-worker-reasoning" not in json.dumps([r for r in sent if r["body"]["model"] == "deepseek-v4-pro"])
    assert all(key not in path.read_text() for key in KEYS.values())
    assert session.run(path, environ={}, transport=transport) == result and len(sent) == 6


def test_file_rounds_preserve_one_addressed_correction_and_shared_call_budget(tmp_path):
    root = repository(tmp_path)
    path = state_file(tmp_path, root)
    transport, sent = exchange([
        frame(None, [delegate("assign")]),
        frame(None, [tool(call_id="worker_first")], model="gpt-6-luna"),
        frame("Initial review", model="gpt-6-luna"),
        frame(None, [delegate("correct", kind="correction")]),
        frame(None, [tool(call_id="worker_correction")], model="gpt-6-luna"),
        frame("Corrected review", model="gpt-6-luna"),
        frame(None, [delegate("over_limit", kind="correction")]),
        frame("Final")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and result["calls"] == 8
    assert result["parts"]["part_1"]["attempts"] == 2 and result["project"]["file_calls"] == 2
    assert result["tool_errors"][-1]["error"] == "correction_limit"
    assert sum(r["body"]["model"] == "gpt-6-luna" for r in sent) == 4
    worker_history = json.loads(path.read_text())["parts"]["part_1"]["history"]
    assert len([i for i in worker_history if i.get("role") == "user"]) == 2


def test_worker_file_round_cannot_consume_the_reserved_lead_call(tmp_path):
    root = repository(tmp_path)
    path = state_file(tmp_path, root, limits={"max_calls": 3})
    transport, sent = exchange([frame(None, [delegate()]),
                                frame(None, [tool()], model="gpt-6-luna")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "stopped" and result["stop_reason"] == "budget_reserves_lead_return"
    assert result["calls"] == len(sent) == 2 and result["project"]["file_calls"] == 1
    assert result["parts"]["part_1"]["attempts"] == 1


@pytest.mark.parametrize("argument", [{"path": "/etc/passwd", "offset": 0, "max_chars": 1},
                                    {"path": ".env", "offset": 0, "max_chars": 1},
                                    {"path": "../outside", "offset": 0, "max_chars": 1},
                                    {"path": "src/main.py", "offset": 0, "max_chars": 1, "root": "/etc"}])
def test_denied_queries_return_data_errors_without_host_reads(tmp_path, monkeypatch, argument):
    root = repository(tmp_path)
    path = state_file(tmp_path, root)
    monkeypatch.setattr(project, "_git", lambda *a: pytest.fail("Denied query reached Git"))
    transport, sent = exchange([frame(None, [tool(**argument)]), frame("Need granted data")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and result["calls"] == 2
    assert result["tool_errors"] and result["project"]["file_calls"] == 1
    raw = json.loads(path.read_text())
    assert raw["file_calls"][0]["status"] == "error"
    assert json.loads(sent[1]["body"]["input"][-1]["output"])["error"] == result["tool_errors"][0]["error"]


def test_integrity_failure_stops_before_another_provider_request(tmp_path, monkeypatch):
    root = repository(tmp_path)
    path = state_file(tmp_path, root)
    monkeypatch.setattr(project, "_git", lambda *a: b"PRIVATE_WRONG_CONTENT")
    transport, sent = exchange([frame(None, [tool()])])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "stopped" and result["stop_reason"] == "project_tool_error"
    assert len(sent) == result["calls"] == 1
    assert result["tool_errors"][-1]["error"] == "project_content_changed"
    assert "PRIVATE_WRONG_CONTENT" not in path.read_text()


def test_file_audit_is_persisted_before_read_and_results_before_next_request(tmp_path, monkeypatch):
    root = repository(tmp_path)
    path = state_file(tmp_path, root)
    original = project.call
    observed = []

    def read(snapshot, name, args):
        raw = json.loads(path.read_text())
        observed.append(raw["file_calls"][-1]["status"])
        assert raw["calls"][-1]["status"] == "received"
        return original(snapshot, name, args)

    monkeypatch.setattr(project, "call", read)
    transport, sent = exchange([frame(None, [tool()]), frame("Final")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and observed == ["in_flight"]
    assert json.loads(sent[1]["body"]["input"][-1]["output"])["text"] == "print('committed')\n"
    raw = json.loads(path.read_text())
    assert raw["file_calls"][0]["status"] == "completed" and raw["file_calls"][0]["reply_bytes"] > 0


def test_worker_duplicate_call_ids_stop_before_local_reads(tmp_path, monkeypatch):
    root = repository(tmp_path)
    path = state_file(tmp_path, root)
    monkeypatch.setattr(project, "_git", lambda *a: pytest.fail("Duplicate batch reached Git"))
    transport, sent = exchange([frame(None, [delegate()]),
                                frame(None, [tool(), tool()], model="gpt-6-luna")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["stop_reason"] == "invalid_tool_batch" and result["project"]["file_calls"] == 0
    assert result["calls"] == len(sent) == 2


def test_worker_cannot_activate_commands_or_recursive_delegation(tmp_path):
    root = repository(tmp_path)
    path = state_file(tmp_path, root)
    transport, sent = exchange([frame(None, [delegate()]),
                                frame(None, [delegate("recursive")], model="gpt-6-luna")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["stop_reason"] == "api_error" and result["calls"] == len(sent) == 2
    assert result["project"]["file_calls"] == 0


@pytest.mark.parametrize("mutation", ["scope", "snapshot", "audit", "disabled"])
def test_corrupt_read_grants_and_audit_cannot_reach_provider(tmp_path, monkeypatch, mutation):
    root = repository(tmp_path)
    path = state_file(tmp_path, root)
    raw = json.loads(path.read_text())
    if mutation == "scope":
        raw["spec"]["project"]["paths"] = ["."]
    elif mutation == "snapshot":
        raw["project_snapshot"]["files"]["src/main.py"]["oid"] = "0" * 40
    elif mutation == "audit":
        raw["file_calls"] = [{"index": 1, "name": [], "status": "completed"}]
    else:
        del raw["spec"]["project"]
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(session.api.httpx, "Client", lambda **kwargs: pytest.fail("Corrupt state reached network"))
    with pytest.raises(session.SessionError, match="invalid_project_state"):
        session.run(path, environ=KEYS)


def test_absent_project_config_does_not_register_read_tools_or_run_git(tmp_path, monkeypatch):
    monkeypatch.setattr(project, "_git", lambda *a: pytest.fail("Text-only session reached Git"))
    path = tmp_path / "text-only.json"
    session.create(path, {"task": "Text only"})
    transport, sent = exchange([frame("Final")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert "project" not in result and "project_snapshot" not in path.read_text()
    assert {t["name"] for t in sent[0]["body"]["tools"]} == {"delegate_luna"}


@pytest.mark.parametrize("lead,worker,model", [("deepseek-lead", "luna-worker", "deepseek-v4-pro"),
                                           ("sol-lead", "luna-worker", "gpt-6.1-sol"),
                                           ("sol-lead", None, "gpt-6.1-sol")])
def test_all_agreed_compositions_can_use_fixed_read_grants(tmp_path, lead, worker, model):
    root = repository(tmp_path)
    path = state_file(tmp_path, root, lead_profile=lead, worker_profile=worker)
    transport, sent = exchange([frame(None, [tool()], model=model), frame("Final", model=model)])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and result["project"]["file_calls"] == 1
    names = {t["name"] for t in sent[0]["body"]["tools"]}
    assert project.TOOL_NAMES <= names and ("delegate_luna" in names) == bool(worker)


def test_configured_shared_budget_includes_context_from_file_tools(tmp_path):
    root = repository(tmp_path)
    path = state_file(tmp_path, root, lead_profile="sol-lead", worker_profile=None,
                      budget={"max_usd": "2", "input_tokens_total": 1000})
    transport, sent = guarded.exchange([guarded.frame(calls=[tool()]), guarded.frame()])
    result = session.run(path, environ=KEYS, transport=transport, require_budget=True)
    assert result["status"] == "dialogue_complete" and result["budget_enforced"] is True
    assert result["calls"] == 2 and result["project"]["file_calls"] == 1
    assert result["budget"]["preflight_requests"] == 2
    assert result["budget"]["input_tokens_committed"] == 24 and len(sent) == 4
    counter, generation = sent[2], sent[3]
    assert counter["body"]["input"] == generation["body"]["input"]
    assert json.loads(counter["body"]["input"][-1]["output"])["text"] == "print('committed')\n"
