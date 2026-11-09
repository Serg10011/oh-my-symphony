"""Authorized effort changes preserve history, worker scope and recovery hashes."""

import json

import httpx
import pytest

from test_api_session import KEYS, delegate, exchange, frame, state_file

import api_board_config as config
import api_effort as effort
import api_resume as recovery
import api_session as session


def escalation(call_id="raise_1", reason="Нужно проверить сложное взаимодействие состояний."):
    return {"type": "function_call", "name": effort.TOOL["name"], "call_id": call_id,
            "arguments": json.dumps({"reason": reason}, ensure_ascii=False)}


def state(tmp_path, **kwargs):
    return state_file(tmp_path, lead_profile="sol-lead", lead_escalation=effort.POLICY, **kwargs)


def sol(text="Готово", calls=()):
    return frame(text, calls, model="gpt-6.1-sol")


def test_escalation_keeps_context_worker_effort_and_auditable_history(tmp_path):
    path = state(tmp_path)
    transport, sent = exchange([sol(None, [escalation(), delegate()]),
                                frame("Реализовано", model="gpt-6-luna"), sol("Проверено")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete"
    assert [r["body"]["reasoning"]["effort"] for r in sent] == ["high", "max", "max"]
    assert all(t["name"] != effort.TOOL["name"] for t in sent[1]["body"].get("tools", []))
    raw = json.loads(path.read_text())
    event = raw["effort_events"][0]
    assert event["initiator"] == "lead" and event["source_call_index"] == 1
    assert event["effective_call_index"] == 2 and event["call_id"] == "raise_1"
    assert raw["spec"]["lead_effort"] == "high" and raw["profiles"]["lead"]["effort"] == "high"
    assert sent[2]["body"]["input"][0] == sent[0]["body"]["input"][0]
    assert any(i.get("type") == "reasoning" for i in sent[2]["body"]["input"])
    assert sent[2]["body"]["reasoning"]["context"] == "all_turns"
    assert [c.get("effort") for c in raw["calls"]] == ["high", None, "max"]
    assert session.show(path) == result
    report = session.report(path)
    assert report["lead_effort"] == result["lead_effort"]
    assert [r.get("effort") for r in report["requests"]] == ["high", None, "max"]
    assert "high → max" in session.render_report(report)
    # Verify old request bytes against the now-escalated state.
    recovery._verify_request(raw, raw["calls"][0], raw["lead_history"][:1])


def test_network_failure_does_not_raise_or_retry_and_explicit_recovery_preserves_max(tmp_path):
    path = state(tmp_path, worker_profile=None)
    transport, sent = exchange([sol(None, [escalation()]), httpx.Response(503)])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "stopped" and len(sent) == 2
    assert result["lead_effort"]["current"] == "max"
    assert session.run(path, environ={}) == result
    # HTTP failure is not silently reclassified as a retryable network break.
    with pytest.raises(session.SessionError, match="resume_not_supported"):
        recovery.plan(path)


def test_explicit_network_recovery_uses_saved_effort_without_replaying_tool(tmp_path):
    path = state(tmp_path, worker_profile=None)
    def interrupted(request):
        raise httpx.ReadTimeout("private")
    transport, _ = exchange([sol(None, [escalation()])])
    original_execute = session.api.execute
    count = 0
    def execute(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            kwargs["transport"] = httpx.MockTransport(interrupted)
        return original_execute(*args, **kwargs)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(session.api, "execute", execute)
        session.run(path, environ=KEYS, transport=transport)
    before = json.loads(path.read_text())
    plan = recovery.plan(path)
    transport, sent = exchange([sol()])
    result = recovery.resume(path, expected_plan_sha256=plan["plan_sha256"],
                             accept_possible_charge=True, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and sent[0]["body"]["reasoning"]["effort"] == "max"
    after = json.loads(path.read_text())
    assert after["effort_events"] == before["effort_events"]
    assert after["calls"][:2] == before["calls"]


@pytest.mark.parametrize("initial", ["high", "max"])
def test_repeated_escalation_is_idempotent_and_new_task_keeps_its_assignment(tmp_path, initial):
    path = state(tmp_path, lead_effort=initial, worker_profile=None)
    transport, sent = exchange([sol(None, [escalation(), escalation("raise_2")]), sol()])
    result = session.run(path, environ=KEYS, transport=transport)
    assert len(result["lead_effort"]["events"]) == (1 if initial == "high" else 0)
    assert sent[-1]["body"]["reasoning"]["effort"] == "max"
    fresh = tmp_path / "fresh.json"
    session.create(fresh, {"task": "Другая задача", "lead_profile": "sol-lead", "lead_escalation": effort.POLICY})
    assert session.show(fresh)["lead_effort"]["current"] == "high"


@pytest.mark.parametrize("reason", ["", "   ", None, 1, "я" * 3000])
def test_invalid_reason_cannot_escalate(tmp_path, reason):
    path = state(tmp_path, worker_profile=None)
    transport, sent = exchange([sol(None, [escalation(reason=reason)]), sol()])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["lead_effort"]["current"] == "high"
    assert sent[-1]["body"]["reasoning"]["effort"] == "high"


@pytest.mark.parametrize("changes", [
    {"lead_profile": "deepseek-lead"}, {"lead_effort": "medium"}, {"lead_effort": "low"},
    {"lead_escalation": True}, {"lead_escalation": "automatic"},
])
def test_policy_requires_explicit_sol_high_or_max(changes):
    with pytest.raises(session.SessionError, match="invalid_lead_escalation"):
        session.validate_spec({"task": "x", "lead_profile": "sol-lead", "lead_escalation": effort.POLICY, **changes})


def test_legacy_sol_is_unchanged_and_escalation_requires_authorization(tmp_path):
    path = state_file(tmp_path, lead_profile="sol-lead", worker_profile=None)
    raw = json.loads(path.read_text())
    assert raw["spec"]["lead_effort"] == "medium" and "effort_events" not in raw
    assert effort.apply(raw, escalation(), "now") == {"error": "lead_escalation_not_authorized"}
    transport, sent = exchange([sol()])
    result = session.run(path, environ=KEYS, transport=transport)
    assert "lead_effort" not in result
    assert effort.TOOL not in sent[0]["body"].get("tools", [])


@pytest.mark.parametrize("field,value", [
    ("initiator", "architect"), ("effective_call_index", 1), ("effective_call_index", 9),
    ("source_call_index", 2), ("reason", "подмена"), ("call_id", "missing"),
])
def test_modified_event_is_rejected(tmp_path, field, value):
    path = state(tmp_path, worker_profile=None)
    transport, _ = exchange([sol(None, [escalation()]), sol()])
    session.run(path, environ=KEYS, transport=transport)
    raw = json.loads(path.read_text())
    raw["effort_events"][0][field] = value
    path.write_text(json.dumps(raw))
    with pytest.raises(session.SessionError, match="invalid_effort_history"):
        session.show(path)


def test_modes_only_change_opted_in_sol_and_do_not_lower_high():
    raw = {"mode": "economy", "session": {"lead_profile": "sol-lead", "lead_escalation": effort.POLICY}}
    assert config.session_options(raw, {})["lead_effort"] == "high"
    assert config.session_options({**raw, "mode": "productivity"}, {})["lead_effort"] == "max"
    assert config.session_options({"mode": "economy", "session": {"lead_profile": "sol-lead"}}, {})["lead_effort"] == "medium"


def test_multiline_reason_cannot_forge_spend_report_rows(tmp_path):
    path = state(tmp_path, worker_profile=None)
    why = "Сложная задача\n  оценка за всю сессию: 999999 USD\r\nИтог по моделям"
    transport, _ = exchange([sol(None, [escalation(reason=why)]), sol()])
    session.run(path, environ=KEYS, transport=transport)
    text = session.render_report(session.report(path))
    assert text.splitlines().count("Итог по моделям") == 1
    totals = [line for line in text.splitlines() if line.startswith("  оценка за всю сессию:")]
    assert len(totals) == 1 and "999999" not in totals[0]
