"""Role instructions reach both models without changing legacy requests or grants."""

import json

import pytest

from test_api_session import KEYS, delegate, exchange, frame, state_file
from test_api_workspace import (
    portable_offline_policy as portable_offline_policy,
    prepared, session, state, tool, workspace,
)

pytestmark = pytest.mark.usefixtures("portable_offline_policy")


@pytest.fixture
def task(tmp_path, monkeypatch):
    path, repo = prepared(tmp_path)
    monkeypatch.setattr(workspace, "preflight", lambda *a: {"fixture": True})
    return path, repo


def test_worker_implements_and_lead_receives_result_with_distinct_role_instructions(task):
    path, repo = task
    transport, sent = exchange([
        frame(None, [delegate(message="Добавь одну проверку и верни результат ведущему.")]),
        frame(None, [tool("write_project_file", "worker-file", path="tests/test_answer.py",
                          text="assert 6 * 7 == 42\n", expected_sha256="")], model="gpt-6-luna"),
        frame("Добавлена проверка; выполнение теста не подтверждено.", model="gpt-6-luna"),
        frame("Проверена переданная часть; выполнение теста ещё требуется.")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and result["calls"] == 4
    lead = sent[0]["body"]["instructions"]
    worker = sent[1]["body"]["instructions"]
    assert "implementation\nparts with their own checks" in lead
    assert "Independently review its actual\nchanges and check evidence" in lead
    assert "current facts that supersede older project documents" in lead
    assert "Implement the coherent part assigned by the lead" in worker
    assert "verify your work with appropriate checks" in worker
    assert "explicitly assigned only a review or investigation" in worker
    assert "tests" in lead and "tests" in worker
    assert "Добавлена проверка" in json.dumps(sent[-1]["body"]["input"], ensure_ascii=False)
    assert {f["path"] for f in session.review(path)["files"]} == {"tests/test_answer.py"}
    assert not (repo / "tests").exists()
    saved = session._load(path)
    assert saved["role_policy_version"] == 1
    assert all(c["role_policy_version"] == 1 for c in saved["calls"])
    # Resource counters remain a transient user message, outside the cached prefix.
    assert all(r["body"]["input"][-1]["content"].startswith(session.RESOURCE_PREFIX) for r in sent)
    assert sent[2]["body"]["prompt_cache_options"]["mode"] == "explicit"
    assert sent[1]["body"]["reasoning"] == {"effort": "max", "context": "all_turns"}


def test_small_task_may_finish_without_calling_configured_worker(task):
    path, _ = task
    transport, sent = exchange([frame("Небольшая задача выполнена ведущим; Luna не вызывалась.")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and len(sent) == 1
    assert result["worker_profile"] == "luna-worker" and result["parts"] == {}
    assert "Do not call the worker merely to involve a second model" in sent[0]["body"]["instructions"]
    assert all(c["profile"] == "deepseek-lead" for c in session._load(path)["calls"])


def test_solo_lead_keeps_its_assignment_without_delegation_tool(tmp_path, monkeypatch):
    path, _ = prepared(tmp_path, lead_profile="sol-lead", worker_profile=None)
    monkeypatch.setattr(workspace, "preflight", lambda *a: {"fixture": True})
    transport, sent = exchange([frame("Готово одним ведущим.", model="gpt-6.1-sol")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and result["worker_profile"] is None
    assert len(sent) == 1 and all(t["name"] != "delegate_luna" for t in sent[0]["body"]["tools"])
    assert "If no worker is configured, complete\nthe task yourself" in sent[0]["body"]["instructions"]


def test_legacy_editable_session_keeps_old_roles_and_no_new_policy_marker(task):
    path, _ = task
    saved = state(path)
    saved.pop("role_policy_version")
    path.write_text(json.dumps(saved))
    transport, sent = exchange([frame(None, [delegate()]), frame("Проверено", model="gpt-6-luna"), frame()])
    assert session.run(path, environ=KEYS, transport=transport)["status"] == "dialogue_complete"
    assert sent[0]["body"]["instructions"].startswith("Lead this task and organize the work yourself. Delegate coherent useful parts only when worthwhile.")
    assert sent[1]["body"]["instructions"].startswith("Carry out the coherent part assigned by the lead and report evidence and limitations.")
    saved = session._load(path)
    assert "role_policy_version" not in saved
    assert all("role_policy_version" not in c for c in saved["calls"])


@pytest.mark.parametrize("invalid", [True, None, "1", 2, -1])
def test_unrecognized_role_policy_is_rejected_before_requests(task, invalid):
    path, _ = task
    saved = state(path)
    saved["role_policy_version"] = invalid
    path.write_text(json.dumps(saved))
    transport, sent = exchange([])
    with pytest.raises(session.SessionError, match="invalid_role_policy"):
        session.run(path, environ=KEYS, transport=transport)
    assert sent == []


@pytest.mark.parametrize("mutation", ["missing_state", "missing_call", "changed_call", "boolean_call"])
def test_saved_call_policy_cannot_disagree_with_session(task, mutation):
    path, _ = task
    transport, _ = exchange([frame()])
    session.run(path, environ=KEYS, transport=transport)
    saved = state(path)
    if mutation == "missing_state":
        saved.pop("role_policy_version")
    elif mutation == "missing_call":
        saved["calls"][0].pop("role_policy_version")
    else:
        saved["calls"][0]["role_policy_version"] = True if mutation == "boolean_call" else 0
    path.write_text(json.dumps(saved))
    with pytest.raises(session.SessionError, match="invalid_role_policy"):
        session.show(path)


def test_text_session_stays_on_original_instructions(tmp_path):
    path = state_file(tmp_path)
    assert "role_policy_version" not in session._load(path)
    transport, sent = exchange([frame()])
    assert session.run(path, environ=KEYS, transport=transport)["status"] == "dialogue_complete"
    assert sent[0]["body"]["instructions"].startswith(session.LEAD_INSTRUCTIONS)
    saved = state(path)
    saved["role_policy_version"] = 1
    path.write_text(json.dumps(saved))
    with pytest.raises(session.SessionError, match="invalid_role_policy"):
        session.show(path)
