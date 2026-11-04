"""Regression coverage for cache boundaries, bounded discovery and saved batches."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import api_executor as api
import api_project as project
import api_resume as recovery
import api_session as session
from test_api_project_session import repository, state_file as project_state, tool
from test_api_session import KEYS, delegate, exchange, frame, state_file


def block(item):
    value = item.get("output") if item.get("type") == "function_call_output" else item.get("content")
    return value[-1] if isinstance(value, list) and value else {}


def plain(value):
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items() if k != "prompt_cache_breakpoint"}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


@pytest.mark.parametrize("profile,part_id", [("sol-lead", None), ("deepseek-lead", "part")])
def test_openai_reuses_previous_boundary_even_after_large_tool_group(tmp_path, profile, part_id):
    path = state_file(tmp_path, lead_profile=profile)
    state = session._load(path)
    history = [{"role": "user", "content": "stable context " * 2000}]
    original = deepcopy(history)
    previous = None
    for turn in range(8):
        resources = {"calls_remaining": 1000 - turn}
        request = session._build_request(state, history, 1024, resources, part_id=part_id)
        _, body = api.prepare("luna-worker" if part_id else profile, request)
        assert body["prompt_cache_options"] == {"mode": "explicit"}
        assert "prompt_cache_breakpoint" not in str(body["input"][-1])
        boundaries = [i for i, item in enumerate(body["input"]) if block(item).get("prompt_cache_breakpoint")]
        assert 1 <= len(boundaries) <= 4 and max(boundaries) == len(history) - 1
        if previous is not None:
            end = max(i for i, item in enumerate(previous) if block(item).get("prompt_cache_breakpoint"))
            assert end in boundaries
            assert plain(previous[:end + 1]) == plain(body["input"][:end + 1])
        previous = body["input"]
        state["calls"].append({"request_version": 2, "history_items": len(history) + 1, "part_id": part_id})
        for n in range(40):
            history.append({"type": "function_call", "call_id": f"{turn}-{n}", "name": "read_project_file", "arguments": "{}"})
        for n in range(40):
            history.append({"type": "function_call_output", "call_id": f"{turn}-{n}", "output": f"read result {turn}-{n}"})
    assert history[0] == original[0] and "prompt_cache_breakpoint" not in str(history)


def test_legacy_request_is_identical_and_deepseek_uses_no_openai_controls(tmp_path):
    path = state_file(tmp_path)
    state = session._load(path)
    resources = session._resources(state, 1024, worker=False)
    old = session._build_request(state, state["lead_history"], 1024, resources, request_version=1)
    new = session._build_request(state, state["lead_history"], 1024, resources)
    assert api.prepare("deepseek-lead", old) == api.prepare("deepseek-lead", new)
    assert "prompt_cache_options" not in new


@pytest.mark.parametrize("options", [{"mode": "implicit"}, {"mode": "explicit", "ttl": "12h"}, None, "explicit"])
def test_only_authorized_cache_policy_is_forwarded(options):
    with pytest.raises(api.ExecutorError, match="invalid_cache_options"):
        api.prepare("luna-worker", {"input": "text", "prompt_cache_options": options})


def test_openai_policy_not_sent_to_deepseek():
    with pytest.raises(api.ExecutorError, match="invalid_cache_options"):
        api.prepare("deepseek-lead", {"input": "text", "prompt_cache_options": {"mode": "explicit"}})


def test_cache_diagnostics_and_reasoning_mode_are_recorded(tmp_path):
    root = repository(tmp_path)
    path = project_state(tmp_path, root, lead_profile="sol-lead", worker_profile=None)
    transport, sent = exchange([
        frame(None, [tool()], model="gpt-6.1-sol"),
        frame("Done", model="gpt-6.1-sol", reasoning={"context": "all_turns"},
              prompt_cache_diagnostics={"type": "cache_miss", "reason": "tools_changed", "cache_missed_tokens": 1200,
                                        "comparison_reusable_tokens": 1300, "secret": "do not record"})])
    assert session.run(path, environ=KEYS, transport=transport)["status"] == "dialogue_complete"
    assert sent[0]["body"]["reasoning"]["context"] == "all_turns"
    assert sent[1]["body"]["prompt_cache_options"]["comparison_response_id"] == "resp_local"
    response = session._load(path)["calls"][-1]["response"]
    assert response["reasoning_context"] == "all_turns"
    assert response["prompt_cache_diagnostics"]["reason"] == "tools_changed"
    assert "secret" not in response["prompt_cache_diagnostics"]


def test_unrecognized_optional_diagnostics_do_not_discard_a_received_answer():
    raw = frame("Done", model="gpt-6-luna", reasoning={"context": []}, prompt_cache_diagnostics=["unknown"])
    profile, body = api.prepare("luna-worker", {"input": "task"})
    result = api.normalize(raw, profile, body)
    assert result["output_text"] == "Done" and "reasoning_context" not in result
    assert "prompt_cache_diagnostics" not in result


def search_args(**changes):
    return {"queries": ["needle"], "prefix": "", "case_sensitive": True,
            "file_offset": 0, "line_offset": 0, **changes}


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def search_snapshot(tmp_path, files):
    root = repository(tmp_path)
    for name, text in files.items():
        dest = root / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(text if isinstance(text, bytes) else text.encode())
    git(root, "add", ".")
    git(root, "commit", "-m", "search fixtures")
    return root, project.snapshot({"root": str(root), "paths": ["src"], "commit": "HEAD"})[1]


def test_search_is_literal_granted_and_pinned_with_unicode_snippets(tmp_path):
    root, snap = search_snapshot(tmp_path, {"src/a.py": "needle\n" + "x" * 700 + "STRAẞE\n[.*]\n",
                                         "outside.txt": "needle secret", "src/.env": "needle secret"})
    (root / "src/a.py").write_text("CHANGED WORKTREE")
    answer = project.call(snap, "search_project_files", search_args(queries=["strasse"], case_sensitive=False))
    assert answer["matches"][0]["line"] == 2 and "STRAẞE" in answer["matches"][0]["text"]
    assert answer["matches"][0]["truncated"]
    answer = project.call(snap, "search_project_files", search_args(queries=["[.*]"]))
    assert [(m["path"], m["line"]) for m in answer["matches"]] == [("src/a.py", 3)]
    assert not project.call(snap, "search_project_files", search_args(prefix="outside.txt"))["matches"]
    assert not project.call(snap, "search_project_files", search_args(prefix="src/.env"))["matches"]


def test_search_pagination_has_no_gaps_and_skips_binary(tmp_path):
    _, snap = search_snapshot(tmp_path, {"src/a.py": "needle\n" * 123, "src/b.bin": b"needle\x00",
                                       "src/c.py": "needle\n"})
    args = search_args()
    matches = []
    rounds = 0
    while True:
        answer = project.call(snap, "search_project_files", args)
        assert len(project._json(answer)) <= project.MAX_REPLY_BYTES
        matches += [(m["path"], m["line"]) for m in answer["matches"]]
        rounds += 1
        if answer["next_cursor"] is None:
            assert answer["skipped_nontext"] == 1
            break
        args.update(answer["next_cursor"])
    assert rounds == 3 and len(matches) == len(set(matches)) == 124


def test_search_bounds_files_and_reply_bytes(tmp_path):
    _, snap = search_snapshot(tmp_path, {f"src/f{n:03}.txt": "no match\n" for n in range(102)})
    first = project.call(snap, "search_project_files", search_args())
    assert first["files_scanned"] == 100 and first["next_cursor"] == {"file_offset": 100, "line_offset": 0}
    second = project.call(snap, "search_project_files", search_args(**first["next_cursor"]))
    assert second["next_cursor"] is None


@pytest.mark.parametrize("changes", [{"prefix": "../"}, {"queries": []}, {"queries": [""]}, {"queries": ["x"] * 9},
                                     {"file_offset": True}, {"line_offset": -1}, {"case_sensitive": 1}, {"extra": 1}])
def test_search_rejects_invalid_queries_before_git(tmp_path, monkeypatch, changes):
    _, snap = search_snapshot(tmp_path, {"src/a.py": "needle"})
    monkeypatch.setattr(project, "_git", lambda *a: pytest.fail("invalid query reached Git"))
    with pytest.raises(project.ProjectError):
        project.call(snap, "search_project_files", search_args(**changes))


@pytest.mark.parametrize("exception", [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_network_class_preserved_without_sensitive_exception_text(exception):
    def fail(request):
        raise exception("SECRET_KEY and private prompt", request=request)
    with pytest.raises(api.ExecutorError) as caught:
        api.execute("luna-worker", {"input": "private prompt"}, environ=KEYS, transport=httpx.MockTransport(fail))
    details = caught.value.as_dict()
    assert details["failure_class"] == exception.__name__ and details["phase"] == "send_or_headers"
    assert details["charge_possible"] and "SECRET_KEY" not in str(details) and "private prompt" not in str(details)


def test_body_error_has_request_id_and_phase_without_response_data():
    class Broken(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"secret":'
            raise httpx.ReadError("sensitive")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, headers={"x-request-id": "req_123"}, stream=Broken()))
    with pytest.raises(api.ExecutorError) as caught:
        api.execute("luna-worker", {"input": "text"}, environ=KEYS, transport=transport)
    details = caught.value.as_dict()
    assert details["request_id"] == "req_123" and details["phase"] == "response_body"
    assert details["failure_class"] == "ReadError" and "secret" not in str(details)


def test_large_read_batch_completes_and_excess_is_deferred_without_stopping(tmp_path, monkeypatch):
    root = repository(tmp_path)
    path = project_state(tmp_path, root)
    monkeypatch.setattr(session, "PROJECT_CALLS_PER_RESPONSE", 33)
    batch = [tool(call_id=f"read_{i}") for i in range(34)]
    transport, _ = exchange([frame(None, batch), frame("Итог")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and result["calls"] == 2
    saved = session._load(path)
    assert len(saved["file_calls"]) == 34
    assert saved["file_calls"][-1]["error"] == "project_batch_deferred"
    assert all(x["status"] == "completed" for x in saved["file_calls"][:-1])


def stopped_legacy_batch(tmp_path, monkeypatch, *, worker):
    root = repository(tmp_path)
    path = project_state(tmp_path, root)
    builder = session._build_request
    validator = session._batch
    def old_builder(*args, **kwargs):
        kwargs["request_version"] = 1
        return builder(*args, **kwargs)
    def old_batch(path, state, calls, seen):
        if len(calls) > 32:
            session._stop(path, state, "invalid_tool_batch")
            return False
        return validator(path, state, calls, seen)
    batch = [tool(call_id=f"read_{i}") for i in range(34)]
    frames = ([frame(None, [delegate()])] if worker else []) + [frame(None, batch, model="gpt-6-luna" if worker else "deepseek-flash")]
    with monkeypatch.context() as old:
        old.setattr(session, "_build_request", old_builder)
        old.setattr(session, "_batch", old_batch)
        transport, _ = exchange(frames)
        assert session.run(path, environ=KEYS, transport=transport)["stop_reason"] == "invalid_tool_batch"
    state = json.loads(path.read_text())
    for call in state["calls"]:
        call.pop("request_version")
    session._save(path, state)
    return path


@pytest.mark.parametrize("worker", [False, True])
def test_resume_legacy_batch_uses_received_response_without_repaying_it(tmp_path, monkeypatch, worker):
    path = stopped_legacy_batch(tmp_path, monkeypatch, worker=worker)
    before = path.read_bytes()
    old = json.loads(before)
    plan = recovery.plan(path)
    assert path.read_bytes() == before and plan["local_read_replies"] == 34
    assert plan["uncertain_call_index"] is None and plan["possible_previous_charge"] is False
    frames = ([frame("Worker result", model="gpt-6-luna")] if worker else []) + [frame("Done")]
    transport, sent = exchange(frames)
    result = recovery.resume(path, expected_plan_sha256=plan["plan_sha256"], accept_possible_charge=True,
                             environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and len(sent) == len(frames)
    saved = session._load(path)
    assert saved["calls"][:len(old["calls"])] == old["calls"]
    assert len(saved["file_calls"]) == 34
    outputs = [i for i in sent[0]["body"]["input"] if i.get("type") == "function_call_output"]
    assert len(outputs) == 34
    assert saved["calls"][len(old["calls"])]["resume_of"] == old["calls"][-1]["index"]


@pytest.mark.parametrize("change", ["duplicate", "executed", "changed_output", "changed_prompt"])
def test_corrupted_or_partly_executed_batch_cannot_resume(tmp_path, monkeypatch, change):
    path = stopped_legacy_batch(tmp_path, monkeypatch, worker=True)
    state = json.loads(path.read_text())
    history = state["parts"]["part_1"]["history"]
    if change == "duplicate":
        state["calls"][-1]["response"]["function_calls"][1]["call_id"] = "read_0"
    elif change == "executed":
        history.append({"type": "function_call_output", "call_id": "read_0", "output": "{}"})
    elif change == "changed_output":
        history[-1]["arguments"] = "{}"
    else:
        state["spec"]["task"] += " changed"
    session._save(path, state)
    with pytest.raises(session.SessionError):
        recovery.plan(path)


def test_new_network_failure_after_saved_batch_preserves_both_recovery_records(tmp_path, monkeypatch):
    path = stopped_legacy_batch(tmp_path, monkeypatch, worker=True)
    plan = recovery.plan(path)
    transport, sent = exchange([httpx.ConnectError("fake failure")])
    result = recovery.resume(path, expected_plan_sha256=plan["plan_sha256"], accept_possible_charge=True,
                             environ=KEYS, transport=transport)
    assert result["stop_reason"] == "api_error" and len(sent) == 1
    before = session._load(path)
    plan = recovery.plan(path)
    assert plan["recovery_kind"] == "network_error" and plan["local_read_replies"] == 0
    transport, sent = exchange([frame("Worker result", model="gpt-6-luna"), frame("Done")])
    result = recovery.resume(path, expected_plan_sha256=plan["plan_sha256"], accept_possible_charge=True,
                             environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and len(sent) == 2
    saved = session._load(path)
    assert saved["calls"][:len(before["calls"])] == before["calls"]
    assert saved["file_calls"] == before["file_calls"] and len(saved["resumptions"]) == 2
