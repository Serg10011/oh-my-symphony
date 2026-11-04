"""Explicit same-task recovery, unknown charges and stale approvals, fully offline."""

from datetime import date
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from test_api_session import KEYS, delegate, exchange, frame, state_file
from test_api_project import repository
from test_api_project_session import state_file as project_state, tool
import test_api_budget as guarded

import api_executor as api
import api_resume as recovery
import api_session as session


@pytest.fixture(autouse=True)
def no_real_keys(monkeypatch):
    for name in [*KEYS, "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(session.budget, "today", lambda: date(2026, 11, 4))


def failed(path, frames=()):
    transport, sent = exchange([*frames, httpx.ReadTimeout("fake-private-error")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["stop_reason"] == "api_error"
    return sent


def resume(path, frames, **kwargs):
    planned = recovery.plan(path)
    transport, sent = exchange(frames)
    result = recovery.resume(path, expected_plan_sha256=planned["plan_sha256"], accept_possible_charge=True,
                             environ=KEYS, transport=transport, **kwargs)
    return result, sent


@pytest.mark.parametrize("lead,worker,model", [("deepseek-lead", "luna-worker", "deepseek-flash"),
                                            ("sol-lead", "luna-worker", "gpt-6.1-sol"),
                                            ("sol-lead", None, "gpt-6.1-sol")])
def test_lead_recovery_preserves_input_unknown_usage_and_exact_backup(tmp_path, lead, worker, model):
    path = state_file(tmp_path, lead_profile=lead, worker_profile=worker, limits={"output_tokens_per_call": 64})
    original_sent = failed(path)
    before = path.read_bytes()
    planned = recovery.plan(path)
    assert path.read_bytes() == before and planned["new_api_requests"] == 0
    assert planned["uncertain_call_index"] == 1 and planned["next_call_index"] == 2
    assert planned["possible_previous_charge"] and planned["resources"]["output_tokens_remaining"] == 2097152 - 64
    result, sent = resume(path, [frame("Готово", model=model)])
    assert result["status"] == "dialogue_complete" and result["calls"] == 2
    assert result["output_tokens_committed"] == 72 and result["usage"][lead]["input_tokens"] is None
    assert sent[0]["body"]["input"][:-1] == original_sent[0]["body"]["input"][:-1]
    raw = json.loads(path.read_text())
    assert raw["calls"][0] == json.loads(before)["calls"][0]
    assert raw["calls"][1]["resume_of"] == 1
    assert raw["started_at"] == json.loads(before)["started_at"]
    backup = Path(raw["resumptions"][0]["backup"])
    assert backup.read_bytes() == before and backup.stat().st_mode & 0o777 == 0o600
    assert session.run(path, environ={}) == result
    assert all(key not in path.read_text() for key in KEYS.values())


def test_worker_recovers_same_attempt_and_finishes_remaining_lead_batch(tmp_path):
    root = repository(tmp_path)
    path = project_state(tmp_path, root)
    sent_before = failed(path, [
        frame(None, [delegate("already", "finished"), delegate("active", "working"),
                     tool(call_id="read_later"), delegate("later", "next")]),
        frame("Уже выполнено", model="gpt-6-luna"),
        frame(None, [tool(call_id="read_once")], model="gpt-6-luna"),
    ])
    before = json.loads(path.read_text())
    planned = recovery.plan(path)
    assert planned["part_id"] == "working" and planned["pending_lead_tool_ids"] == ["active", "read_later", "later"]
    result, sent = resume(path, [frame("Продолжено", model="gpt-6-luna"),
                               frame("Следующая часть", model="gpt-6-luna"), frame("Итог")])
    raw = json.loads(path.read_text())
    assert result["status"] == "dialogue_complete" and result["calls"] == 7
    assert raw["calls"][:4] == before["calls"]
    assert raw["file_calls"][:1] == before["file_calls"] and len(raw["file_calls"]) == 2
    assert raw["parts"]["finished"] == before["parts"]["finished"]
    assert all(p["attempts"] == 1 for p in raw["parts"].values())
    assert sent[0]["body"]["input"][:-1] == sent_before[-1]["body"]["input"][:-1]
    outputs = [i for i in sent[-1]["body"]["input"] if i.get("type") == "function_call_output"]
    assert [i["call_id"] for i in outputs] == ["already", "active", "read_later", "later"]
    assert "opaque-worker-reasoning" not in json.dumps(sent[-1])
    assert "PRIVATE_LEAD_CONTEXT" not in json.dumps(sent[0])
    assert "opaque-worker-reasoning" in json.dumps(sent[0])


def test_recovery_of_correction_does_not_grant_another_correction(tmp_path):
    path = state_file(tmp_path)
    failed(path, [frame(None, [delegate()]), frame("Первая версия", model="gpt-6-luna"),
                  frame(None, [delegate("fix", kind="correction", message="Исправь ошибку")])])
    result, sent = resume(path, [frame("Исправлено", model="gpt-6-luna"),
                               frame(None, [delegate("extra", kind="correction")]), frame("Итог")])
    assert result["parts"]["part_1"]["attempts"] == 2
    assert result["tool_errors"][-1]["error"] == "correction_limit"
    assert len([r for r in sent if r["body"]["model"] == "gpt-6-luna"]) == 1


def test_second_network_error_needs_a_new_plan_and_keeps_both_reservations(tmp_path):
    path = state_file(tmp_path, limits={"output_tokens_per_call": 64})
    failed(path)
    old = recovery.plan(path)
    result, sent = resume(path, [httpx.ConnectError("fake")])
    assert result["stop_reason"] == "api_error" and len(sent) == 1
    assert result["output_tokens_committed"] == 128
    with pytest.raises(session.SessionError, match="resume_plan_changed"):
        recovery.resume(path, expected_plan_sha256=old["plan_sha256"], accept_possible_charge=True, environ={})
    result, sent = resume(path, [frame("Итог")])
    assert result["calls"] == 3 and result["output_tokens_committed"] == 136
    assert len(result["resumptions"]) == 2
    assert result["usage"]["deepseek-lead"]["output_tokens"] is None


@pytest.mark.parametrize("kind", ["missing_ack", "wrong_plan", "missing_key", "required_budget", "busy", "copied_path"])
def test_unapproved_or_changed_plan_never_reaches_provider(tmp_path, kind):
    path = state_file(tmp_path)
    failed(path)
    before = path.read_bytes()
    planned = recovery.plan(path)
    transport, sent = exchange([])
    options = {"expected_plan_sha256": planned["plan_sha256"], "accept_possible_charge": True,
               "environ": KEYS, "transport": transport}
    if kind == "missing_ack":
        options["accept_possible_charge"] = False
    elif kind == "wrong_plan":
        options["expected_plan_sha256"] = "0" * 64
    elif kind == "missing_key":
        options["environ"] = {}
    elif kind == "required_budget":
        options["require_budget"] = True
    elif kind == "copied_path":
        path = tmp_path / "copy.json"
        path.write_bytes(before)
        path.chmod(0o600)
    if kind == "busy":
        with session._lock(path), pytest.raises(session.SessionError, match="session_busy"):
            recovery.resume(path, **options)
    else:
        with pytest.raises(session.SessionError):
            recovery.resume(path, **options)
    assert path.read_bytes() == before and not sent
    assert not list(tmp_path.glob("*.before-resume-*.json"))


@pytest.mark.parametrize("mutation", ["history", "hash", "resources", "part", "assignment", "orphan_output",
                                     "pending_file", "older_unknown", "profiles", "instructions"])
def test_inconsistent_boundary_is_rejected_locally(tmp_path, monkeypatch, mutation):
    root = repository(tmp_path)
    path = project_state(tmp_path, root)
    failed(path, [frame(None, [delegate()]), frame(None, [tool()], model="gpt-6-luna")])
    raw = json.loads(path.read_text())
    if mutation == "history":
        raw["parts"]["part_1"]["history"].append({"role": "user", "content": "Changed"})
    elif mutation == "hash":
        raw["calls"][-1]["request_sha256"] = "0" * 64
    elif mutation == "resources":
        raw["calls"][-1]["resources"]["calls_remaining"] += 1
    elif mutation == "part":
        raw["calls"][-1]["part_id"] = None
    elif mutation == "assignment":
        for item in raw["lead_history"]:
            if item.get("type") == "function_call":
                item["arguments"] = delegate(part="wrong")["arguments"]
    elif mutation == "orphan_output":
        raw["lead_history"].append({"type": "function_call_output", "call_id": "no_call", "output": "{}"})
    elif mutation == "pending_file":
        raw["file_calls"][-1]["status"] = "in_flight"
    elif mutation == "older_unknown":
        raw["calls"][1].update(status="error", error={"error": "network_error", "charge_possible": True})
    elif mutation == "profiles":
        raw["profiles"]["worker"]["effort"] = "high"
    elif mutation == "instructions":
        monkeypatch.setattr(session, "PROJECT_WORKER_INSTRUCTIONS", "Changed instructions")
    session._save(path, raw)
    before = path.read_bytes()
    with pytest.raises(session.SessionError):
        recovery.plan(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("reason", ["unknown_usage", "incomplete_response", "model_refusal", "call_limit",
                                   "project_tool_error", "interrupted_no_automatic_retry"])
def test_other_stop_reasons_remain_terminal(tmp_path, reason):
    path = state_file(tmp_path)
    failed(path)
    raw = json.loads(path.read_text())
    raw["stop_reason"] = reason
    session._save(path, raw)
    with pytest.raises(session.SessionError, match="resume_not_supported"):
        recovery.plan(path)


def test_backup_failure_prevents_start_and_preserves_source(tmp_path, monkeypatch):
    path = state_file(tmp_path)
    failed(path)
    before = path.read_bytes()
    planned = recovery.plan(path)
    monkeypatch.setattr(recovery, "_backup", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    transport, sent = exchange([])
    with pytest.raises(session.SessionError, match="state_io_error"):
        recovery.resume(path, expected_plan_sha256=planned["plan_sha256"], accept_possible_charge=True,
                        environ=KEYS, transport=transport)
    assert not sent and path.read_bytes() == before


def test_crash_during_recovery_stays_interrupted_without_automatic_retry(tmp_path):
    path = state_file(tmp_path, limits={"output_tokens_per_call": 64})
    failed(path)
    with pytest.raises(KeyboardInterrupt):
        resume(path, [KeyboardInterrupt()])
    result = session.run(path, environ={})
    assert result["stop_reason"] == "interrupted_no_automatic_retry"
    assert result["calls"] == 2 and result["output_tokens_committed"] == 128
    with pytest.raises(session.SessionError, match="resume_not_supported"):
        recovery.plan(path)


def test_budget_carries_the_unknown_cost_and_input_reservation_forward(tmp_path):
    path = guarded.create(tmp_path, limits={"output_tokens_per_call": 64})
    transport, _ = guarded.exchange([httpx.ReadTimeout("fake")])
    initial = session.run(path, environ=KEYS, transport=transport)
    before = json.loads(path.read_text())
    planned = recovery.plan(path)
    transport, sent = guarded.exchange([guarded.frame()])
    result = recovery.resume(path, expected_plan_sha256=planned["plan_sha256"], accept_possible_charge=True,
                             require_budget=True, environ=KEYS, transport=transport)
    assert len(guarded.generated(sent)) == 1 and result["calls"] == 2
    assert result["budget"]["input_tokens_committed"] == 24
    assert result["budget"]["generation_cost_upper_usd"] > initial["budget"]["generation_cost_upper_usd"]
    assert json.loads(path.read_text())["calls"][0] == before["calls"][0]
    assert result["usage"]["sol-lead"]["input_tokens"] is None


def test_budget_still_blocks_resumed_generation_before_spend(tmp_path):
    path = guarded.create(tmp_path, inputs=12, limits={"output_tokens_per_call": 64})
    transport, _ = guarded.exchange([httpx.ReadTimeout("fake")])
    session.run(path, environ=KEYS, transport=transport)
    planned = recovery.plan(path)
    transport, sent = guarded.exchange([])
    result = recovery.resume(path, expected_plan_sha256=planned["plan_sha256"], accept_possible_charge=True,
                             environ=KEYS, transport=transport)
    assert result["stop_reason"] == "input_budget_exhausted" and result["calls"] == 1
    assert not guarded.generated(sent) and result["budget"]["input_tokens_committed"] == 12


@pytest.mark.parametrize("limits", [{"max_calls": 1}, {"output_tokens_per_call": 32, "output_tokens_total": 32}])
def test_recovery_does_not_reset_exhausted_limits(tmp_path, limits):
    path = state_file(tmp_path, limits=limits)
    failed(path)
    with pytest.raises(session.SessionError, match="resume_limits_exhausted"):
        recovery.plan(path)


def test_cli_plan_is_local_and_resume_requires_acknowledgement(tmp_path):
    path = state_file(tmp_path)
    failed(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    entry = Path(session.__file__).parents[1] / "bin/api-session"
    command = [sys.executable, str(entry)]
    inspected = subprocess.run([*command, "resume-plan", "--state", str(path)], cwd=tmp_path,
                               text=True, capture_output=True)
    assert inspected.returncode == 0, inspected.stdout
    planned = json.loads(inspected.stdout)
    rejected = subprocess.run([*command, "resume", "--state", str(path), "--plan-sha256", planned["plan_sha256"]],
                              cwd=tmp_path, text=True, capture_output=True)
    assert rejected.returncode == 2 and "resume_charge_acknowledgement_required" in rejected.stdout
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_recovery_preserves_partial_worker_text_when_reserve_returns_to_lead(tmp_path):
    root = repository(tmp_path)
    path = project_state(tmp_path, root, limits={"output_tokens_per_call": 16, "output_tokens_total": 64})
    failed(path, [frame(None, [delegate()]), frame("До сбоя", [tool()], model="gpt-6-luna")])
    result, sent = resume(path, [frame("После сбоя", [tool(call_id="after")], model="gpt-6-luna"), frame("Итог")])
    assert result["status"] == "dialogue_complete" and len(sent) == 2
    outputs = [i for i in sent[-1]["body"]["input"] if i.get("type") == "function_call_output"]
    returned = json.loads(outputs[-1]["output"])
    assert returned["partial"] and returned["text"] == "До сбоя\n\nПосле сбоя"
    assert returned["error"] == "budget_reserves_lead_return"


def test_recovery_keeps_old_lead_tool_ids_in_duplicate_protection(tmp_path):
    path = state_file(tmp_path)
    failed(path, [frame(None, [delegate()])])
    result, sent = resume(path, [frame("Часть", model="gpt-6-luna"), frame(None, [delegate()])])
    assert result["stop_reason"] == "invalid_tool_batch" and len(sent) == 2


def test_changed_runtime_invalidates_approval_before_generation(tmp_path, monkeypatch):
    path = state_file(tmp_path)
    failed(path)
    planned = recovery.plan(path)
    read = Path.read_bytes

    def changed(p):
        data = read(p)
        return data + b"\n# changed build\n" if p == Path(recovery.__file__) else data

    monkeypatch.setattr(Path, "read_bytes", changed)
    monkeypatch.setattr(api, "execute", lambda *a, **k: pytest.fail("Unexpected provider call"))
    with pytest.raises(session.SessionError, match="resume_plan_changed"):
        recovery.resume(path, expected_plan_sha256=planned["plan_sha256"], accept_possible_charge=True, environ=KEYS)


def test_missing_fixed_project_blocks_plan(tmp_path):
    root = repository(tmp_path)
    path = project_state(tmp_path, root)
    failed(path)
    root.rename(root.with_name("moved-repo"))
    with pytest.raises(session.SessionError, match="project_git_failed"):
        recovery.plan(path)


@pytest.mark.parametrize("error", [None, [], "secret-error", {"error": "http_error", "http_status": 429},
                                   {"error": "network_error", "charge_possible": False}])
def test_other_or_malformed_errors_never_authorize_recovery(tmp_path, error):
    path = state_file(tmp_path)
    failed(path)
    raw = json.loads(path.read_text())
    raw["calls"][-1]["error"] = error
    session._save(path, raw)
    with pytest.raises(session.SessionError):
        recovery.plan(path)


def test_conflicting_backup_is_not_overwritten(tmp_path):
    path = state_file(tmp_path)
    failed(path)
    before = path.read_bytes()
    planned = recovery.plan(path)
    backup = path.with_name(path.name + ".before-resume-" + planned["state_sha256"] + ".json")
    backup.write_text("other preserved data")
    backup.chmod(0o600)
    transport, sent = exchange([])
    with pytest.raises(session.SessionError, match="resume_backup_conflict"):
        recovery.resume(path, expected_plan_sha256=planned["plan_sha256"], accept_possible_charge=True,
                        environ=KEYS, transport=transport)
    assert path.read_bytes() == before and backup.read_text() == "other preserved data" and not sent


def test_failed_resume_checkpoint_cannot_send_a_request(tmp_path, monkeypatch):
    path = state_file(tmp_path)
    failed(path)
    before = path.read_bytes()
    planned = recovery.plan(path)
    original = session._save

    def fail_start(p, state):
        if state["status"] == "running":
            raise session.SessionError("state_write_failed")
        original(p, state)

    monkeypatch.setattr(session, "_save", fail_start)
    transport, sent = exchange([])
    with pytest.raises(session.SessionError, match="state_write_failed"):
        recovery.resume(path, expected_plan_sha256=planned["plan_sha256"], accept_possible_charge=True,
                        environ=KEYS, transport=transport)
    assert path.read_bytes() == before and not sent
    assert next(tmp_path.glob("*.before-resume-*.json")).read_bytes() == before


def test_same_confirmation_cannot_launch_a_second_completed_recovery(tmp_path):
    path = state_file(tmp_path)
    failed(path)
    planned = recovery.plan(path)
    result, _ = resume(path, [frame("Итог")])
    before = path.read_bytes()
    transport, sent = exchange([])
    with pytest.raises(session.SessionError, match="resume_not_supported"):
        recovery.resume(path, expected_plan_sha256=planned["plan_sha256"], accept_possible_charge=True,
                        environ=KEYS, transport=transport)
    assert not sent and path.read_bytes() == before and session.show(path) == result


def test_worker_recovery_preserves_separate_caps_and_requires_lead_reserve(tmp_path):
    path = state_file(tmp_path, limits={"max_calls": 3})
    failed(path, [frame(None, [delegate()])])
    with pytest.raises(session.SessionError, match="resume_limits_exhausted"):
        recovery.plan(path)
