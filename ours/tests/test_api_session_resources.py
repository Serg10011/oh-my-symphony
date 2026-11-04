"""Shared quota visibility and worker handoff without any provider access."""

from datetime import date
import json

import httpx
import pytest

from test_api_project import repository
from test_api_project_session import state_file, tool
from test_api_session import KEYS, delegate, exchange, frame
import test_api_budget as guarded

session, project = guarded.session, guarded.session.project


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for key in [*KEYS, "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(guarded.budget, "today", lambda: date(2026, 11, 3))


def resources(body):
    message = body["input"][-1]
    assert message["role"] == "user" and message["content"].startswith(session.RESOURCE_PREFIX)
    return json.loads(message["content"][len(session.RESOURCE_PREFIX):])


def tool_outputs(body):
    return {item["call_id"]: json.loads(item["output"]) for item in body["input"]
            if item.get("type") == "function_call_output"}


def test_shared_resources_are_current_and_do_not_change_the_stable_prefix(tmp_path):
    root = repository(tmp_path)
    path = state_file(tmp_path, root, limits={"max_calls": 5, "output_tokens_per_call": 32, "output_tokens_total": 64})
    transport, sent = exchange([frame(None, [delegate()]), frame("Worker result", model="gpt-6-luna"), frame("Final")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete"
    first, worker, final = [resources(r["body"]) for r in sent]
    assert (first["calls_remaining"], first["output_tokens_remaining"], first["response_output_cap"]) == (5, 64, 32)
    assert (worker["calls_remaining"], worker["output_tokens_remaining"], worker["response_output_cap"]) == (4, 56, 32)
    assert (worker["lead_calls_reserved"], worker["lead_output_tokens_reserved"]) == (1, 16)
    assert (worker["calls_available_to_you"], worker["output_tokens_available_to_you"]) == (3, 40)
    assert (final["calls_remaining"], final["output_tokens_remaining"], final["lead_calls_reserved"]) == (3, 48, 0)
    assert sent[0]["body"]["instructions"] == sent[2]["body"]["instructions"]
    assert sent[0]["body"]["tools"] == sent[2]["body"]["tools"]
    assert sent[2]["body"]["input"][:len(sent[0]["body"]["input"]) - 1] == sent[0]["body"]["input"][:-1]
    raw = json.loads(path.read_text())
    assert [c["resources"] for c in raw["calls"]] == [first, worker, final]
    for history in [raw["lead_history"], raw["parts"]["part_1"]["history"]]:
        assert not any(isinstance(i.get("content"), str) and i["content"].startswith(session.RESOURCE_PREFIX) for i in history)


def test_twenty_third_call_hands_back_the_reserved_twenty_fourth_call(tmp_path):
    root = repository(tmp_path)
    path = state_file(tmp_path, root, limits={"max_calls": 24})
    frames = [frame(None, [tool(call_id=f"lead_{i}")]) for i in range(15)]
    frames += [frame(None, [delegate("assign")])]
    frames += [frame(None, [tool(call_id=f"worker_{i}")], model="gpt-6-luna") for i in range(7)]
    frames += [frame("Lead can finish within the original limit")]
    transport, sent = exchange(frames)
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and result["calls"] == len(sent) == 24
    assert result["parts"]["part_1"]["attempts"] == 1
    output = tool_outputs(sent[-1]["body"])["assign"]
    assert output["error"] == "budget_reserves_lead_return" and output["partial"] is True
    assert output["text"] == ""
    assert resources(sent[16]["body"])["calls_available_to_you"] == 7
    assert resources(sent[-2]["body"])["last_available_call"] is True
    assert resources(sent[-1]["body"])["calls_remaining"] == 1
    assert resources(sent[-1]["body"])["last_available_call"] is True
    before = path.read_bytes()
    assert session.run(path, environ={}, transport=transport) == result and len(sent) == 24
    assert path.read_bytes() == before


def test_partial_worker_text_and_all_delegate_outputs_reach_the_lead(tmp_path):
    root = repository(tmp_path)
    path = state_file(tmp_path, root, limits={"max_calls": 3})
    transport, sent = exchange([frame(None, [delegate("first"), delegate("second", part="other")]),
                                frame("Only this partial result is ready", [tool()], model="gpt-6-luna"), frame("Final")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and result["calls"] == 3
    outputs = tool_outputs(sent[-1]["body"])
    assert outputs["first"] == {"part_id": "part_1", "attempt": 1, "text": "Only this partial result is ready",
                                "error": "budget_reserves_lead_return", "partial": True}
    assert outputs["second"] == {"error": "budget_reserves_lead_return"}
    assert result["parts"] == {"part_1": {"attempts": 1}}
    assert "opaque-worker-reasoning" not in json.dumps(sent[-1])
    assert [e["call_id"] for e in result["tool_errors"]] == ["first", "second"]


@pytest.mark.parametrize("total,used,worker_cap,lead_cap", [(40, 16, 16, 16), (48, 20, 24, 20)])
def test_output_reserve_hands_back_before_another_worker_request(tmp_path, total, used, worker_cap, lead_cap):
    root = repository(tmp_path)
    path = state_file(tmp_path, root, limits={"max_calls": 8, "output_tokens_per_call": 32, "output_tokens_total": total})
    usage = {"input_tokens": 12, "output_tokens": used, "total_tokens": 12 + used}
    transport, sent = exchange([frame(None, [delegate()]), frame(None, [tool()], model="gpt-6-luna", usage=usage), frame("Final")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and len(sent) == 3
    assert [r["body"]["max_output_tokens"] for r in sent] == [32, worker_cap, lead_cap]
    assert result["output_tokens_committed"] == 16 + used <= total
    assert tool_outputs(sent[-1]["body"])["call_1"]["error"] == "budget_reserves_lead_return"
    assert resources(sent[-1]["body"])["output_tokens_remaining"] == lead_cap


@pytest.mark.parametrize("response,reason", [
    (frame(model="gpt-6-luna", usage=None), "unknown_usage"),
    (frame(model="gpt-6-luna", status="incomplete"), "incomplete_response"),
    (httpx.ReadTimeout("fake transport failure"), "api_error"),
    (frame(model="gpt-6-luna", output=[{"type": "message", "content": [{"type": "refusal", "refusal": "No"}]}]), "model_refusal"),
])
def test_real_worker_failure_stops_without_using_the_lead_reserve(tmp_path, response, reason):
    root = repository(tmp_path)
    path = state_file(tmp_path, root)
    transport, sent = exchange([frame(None, [delegate()]), response])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "stopped" and result["stop_reason"] == reason and len(sent) == 2
    before = path.read_bytes()
    assert session.run(path, environ={}, transport=transport) == result and len(sent) == 2
    assert path.read_bytes() == before


def test_worker_project_integrity_failure_remains_fatal(tmp_path, monkeypatch):
    root = repository(tmp_path)
    path = state_file(tmp_path, root)
    def fail(*args):
        raise project.ProjectError("project_content_changed")
    monkeypatch.setattr(project, "call", fail)
    transport, sent = exchange([frame(None, [delegate()]), frame(None, [tool()], model="gpt-6-luna")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["stop_reason"] == "project_tool_error" and len(sent) == 2
    assert result["tool_errors"][-1]["error"] == "project_content_changed"


def test_interruption_during_reserved_lead_return_cannot_restart_it(tmp_path):
    root = repository(tmp_path)
    path = state_file(tmp_path, root, limits={"max_calls": 3})
    transport, sent = exchange([frame(None, [delegate()]), frame(None, [tool()], model="gpt-6-luna"), KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        session.run(path, environ=KEYS, transport=transport)
    result = session.run(path, environ={}, transport=transport)
    assert result["stop_reason"] == "interrupted_no_automatic_retry" and len(sent) == 3
    assert json.loads(path.read_text())["calls"][-1]["status"] == "in_flight"


def test_input_budget_counter_and_generation_share_the_same_resource_status(tmp_path):
    path = guarded.create(tmp_path, worker="luna-worker", inputs=100)
    transport, sent = guarded.exchange([guarded.frame(calls=[guarded.delegate()]),
                                       guarded.frame("gpt-6-luna"), guarded.frame()])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete"
    generations = guarded.generated(sent)
    assert [resources(r["body"])["input_tokens_remaining"] for r in generations] == [100, 88, 76]
    assert [r["body"]["input"] for r in sent if r["url"].endswith("/input_tokens")] == [r["body"]["input"] for r in generations]
