"""A Sol lead reviews the controller's actual changes when Flash hands work back."""

import json
from pathlib import Path

import pytest

from test_api_board import board as board
from test_api_flash_board import TEAM, approve_flash, assignment, fake_worker, lead_frames, sol
from test_api_session import KEYS
from test_api_workspace import portable_offline_policy as portable_offline_policy, prepared, state, tool

import api_executor as api
import api_session as session
import api_workspace as workspace


def tool_result(request, call_id):
    result = next(i for i in request["input"] if i.get("type") == "function_call_output" and i["call_id"] == call_id)
    return json.loads(result["output"][0]["text"])


def test_completed_handoff_includes_the_checked_diff_in_the_next_lead_request(board, tmp_path, monkeypatch):
    wire = fake_worker(tmp_path, monkeypatch, "batch_discovery")
    approve_flash(board, {**TEAM, "lead_profile": api.SOL_62_LEAD})
    model = api.SOL_62_MODEL
    transport, sent = lead_frames(
        sol(calls=[assignment(call_id="hand", message="Prepare the test.")], model=model),
        sol(calls=[assignment(call_id="amend", kind="correction", message="Confirm the result.")], model=model),
        sol("Reviewed", model=model), model=model)
    assert board.run("a" * 32, credentials=lambda _: KEYS, transport=transport)["status"] == "result"
    assert len(sent) == 3
    assert session._EARLY_HANDOFF in sent[0]["input"][0]["content"]
    assert session._PLAN_CHANGE_REPORT in sent[0]["input"][0]["content"]
    returned = tool_result(sent[1], "hand")
    assert returned["text"] == "Ready"
    first = returned["workspace_changes"]
    actual = board.review()
    assert first["revision"] == actual["revision"] and first["base_commit"] == actual["base_commit"]
    assert first["next"] is None and first["total"] == 1
    assert first["files"][0] == {**actual["files"][0], "diff_offset": 0}
    assert "+assert 2 + 2 == 4" in first["files"][0]["diff"]
    assert tool_result(sent[2], "amend")["workspace_changes"] == {
        "revision": first["revision"], "base_commit": first["base_commit"], "total": 1, "same_as": "hand", "next": None}
    assert "workspace_changes" not in wire.read_text()
    saved = session._load(board.session_path)
    assert saved["role_policy_version"] == 9
    assert all(item["role_policy_version"] == 9 for item in saved["calls"])
    assert all(item["name"] != "project_changes" for item in saved["file_calls"])


@pytest.mark.parametrize("version", [7, 8, 9])
def test_handoff_checks_use_completed_commands_at_the_submitted_revision(board, tmp_path, monkeypatch, version):
    fake_worker(tmp_path, monkeypatch, "handoff_checks")
    if version in (7, 8):
        monkeypatch.setattr(session, "_new_role_policy", lambda _spec: version)
    approve_flash(board, {**TEAM, "lead_profile": api.SOL_62_LEAD})
    full_output = "detailed test output\n" * 500

    def run_check(policy, command, **_kwargs):
        if command == "edit-and-check":
            (policy.workspace / "tests/test_flash.py").write_text("assert 3 + 3 == 6\n")
        return {"exit_code": 1 if command == "fail-final" else 0,
                "stop_reason": "timeout" if command == "timeout-final" else None,
                "output": full_output}

    monkeypatch.setattr(workspace.sandbox, "run", run_check)
    model = api.SOL_62_MODEL
    transport, sent = lead_frames(sol(calls=[assignment(call_id="hand", message="Prepare the test.")], model=model),
                                  sol("Reviewed", model=model), model=model)
    assert board.run("a" * 32, credentials=lambda _: KEYS, transport=transport)["status"] == "result"
    assert len(sent) == 2
    assert (session._CHECK_HANDOFF in sent[0]["input"][0]["content"]) == (version >= 8)
    returned = tool_result(sent[-1], "hand")
    assert "detailed test output" not in json.dumps(returned)
    revision = returned["workspace_changes"]["revision"]
    saved = session._load(board.session_path)
    records = [row for row in saved["file_calls"] if row["name"] == "run_project_command"]
    assert len(records) == 5
    history = saved["parts"]["part_1"]["history"]
    outputs = {
        item["call_id"]: json.loads(item["output"])
        for item in history if item.get("type") == "function_call_output"
    }
    assert all(outputs[row["call_id"]]["output"] == full_output for row in records)
    if version == 7:
        assert "workspace_checks" not in returned
        assert all("command_result" not in row for row in records)
        return
    editing_command = records[1]["command_result"]
    assert editing_command["before_revision"] != editing_command["after_revision"] == revision
    checks = returned["workspace_checks"]
    assert checks["revision"] == revision
    assert (checks["total"], checks["failures"], checks["other_revisions"]) == (3, 2, 2)
    assert [row["call_id"] for row in checks["commands"]] == [row["call_id"] for row in records[2:]]
    passed, failed, stopped = checks["commands"]
    assert passed["exit_code"] == 0 and passed["stop_reason"] is None
    assert passed["command"].startswith("check-final ") and passed["command_truncated"]
    assert len(passed["command"].encode("utf-8")) <= session.MAX_CHECK_COMMAND_BYTES
    assert failed["exit_code"] == 1 and not failed["command_truncated"]
    assert stopped["stop_reason"] == "timeout"


@pytest.fixture
def review_copy(tmp_path, portable_offline_policy):
    path, _ = prepared(tmp_path, lead_profile=api.SOL_62_LEAD, worker_profile=api.FLASH_WORKER)
    return state(path)


def changes(saved, cursor):
    return workspace.lead_changes(saved["spec"], saved["execution_state"], saved["project_snapshot"], cursor)


def test_pages_reconstruct_the_operator_diff_including_unicode_deletions_and_binary(review_copy):
    saved = review_copy
    root = Path(saved["execution_state"]["root"])
    assert changes(saved, workspace.DIFF_START)["total"] == 0
    (root / "README.md").unlink()
    (root / "src/main.py").write_text("пример" * 50)
    (root / "src/binary").write_bytes(b"\x00\xff")
    (root / "src/added").write_text("new\n")
    (root / "src/added").chmod(0o755)
    expected = workspace.review(saved["spec"], saved["execution_state"], saved["project_snapshot"])
    assembled = {}
    cursor = {**workspace.DIFF_START, "max_chars": 45}
    seen = set()
    while cursor is not None:
        key = (cursor["offset"], cursor["diff_offset"])
        assert key not in seen
        seen.add(key)
        page = changes(saved, cursor)
        assert page["revision"] == expected["revision"] and page["total"] == 4
        assert sum(len(row["diff"] or "") for row in page["files"]) <= 45
        for row in page["files"]:
            path = row["path"]
            if path not in assembled:
                assembled[path] = {k: v for k, v in row.items() if k != "diff_offset"}
                assert row["diff_offset"] == 0
            else:
                assert row["diff_offset"] == len(assembled[path]["diff"])
                assembled[path]["diff"] += row["diff"]
        cursor = page["next"]
    assert list(assembled.values()) == expected["files"]
    assert assembled["README.md"]["change"] == "deleted" and assembled["src/binary"]["diff"] is None


def test_stale_page_cannot_mix_revisions_and_returns_a_restart(review_copy):
    saved = review_copy
    root = Path(saved["execution_state"]["root"])
    (root / "src/main.py").write_text("x = 1\n")
    cursor = changes(saved, {**workspace.DIFF_START, "max_chars": 5})["next"]
    (root / "src/main.py").write_text("print('committed')\n")
    output, fatal = session._project_answer(saved, tool("project_changes", "page", **cursor))
    assert not fatal and output == {"error": "workspace_diff_cursor_stale", "restart": workspace.DIFF_START}
    assert changes(saved, output["restart"])["files"] == []


def test_files_with_no_text_diff_remain_visible_across_the_file_page_boundary(review_copy):
    root = Path(review_copy["execution_state"]["root"])
    (root / "README.md").chmod(0o755)
    for i in range(21):
        (root / f"src/empty-{i:02d}").touch()
    first = changes(review_copy, workspace.DIFF_START)
    second = changes(review_copy, first["next"])
    rows = first["files"] + second["files"]
    assert first["total"] == 22 and second["next"] is None
    assert len({row["path"] for row in rows}) == 22
    assert all(row["diff"] == "" for row in rows)
    assert rows[0]["before"]["mode"] == 0o644 and rows[0]["after"]["mode"] == 0o755


def test_worker_and_saved_policy_keep_the_original_changes_tool(review_copy):
    saved = review_copy
    for version, part in [(5, None), (6, "worker"), (7, "worker"), (8, "worker"), (9, "worker")]:
        saved["role_policy_version"] = version
        request = session._build_request(saved, [], 100, {}, part_id=part)
        original = next(item for item in request["tools"] if item["name"] == "project_changes")
        assert original["parameters"]["required"] == ["offset"]
        assert session._EARLY_HANDOFF not in request["instructions"]
        output, fatal = session._project_answer(saved, tool("project_changes", "old", offset=0), part_id=part)
        assert not fatal and output["changes"] == [] and "files" not in output


@pytest.mark.parametrize(("lead", "worker", "effort", "version"), [
    (api.SOL_62_LEAD, api.FLASH_WORKER, "max", 5),
    ("sol-lead", api.FLASH_WORKER, "high", 5),
    (api.SOL_62_LEAD, None, "high", 10),
    (api.SOL_62_LEAD, None, "max", 10),
])
def test_new_policy_is_scoped_to_the_assigned_pair_and_starting_effort(tmp_path, portable_offline_policy,
                                                                      lead, worker, effort, version):
    path, _ = prepared(tmp_path, lead_profile=lead, worker_profile=worker, lead_effort=effort)
    saved = state(path)
    assert saved["role_policy_version"] == version
    for incompatible in {6, 7, 8, 9, 10} - {version}:
        saved["role_policy_version"] = incompatible
        with pytest.raises(session.SessionError, match="invalid_role_policy"):
            session._load(path, raw=json.dumps(saved).encode())


@pytest.mark.parametrize("version", [6, 7, 8, 9])
def test_only_first_assignment_can_include_the_original_task(review_copy, tmp_path, monkeypatch, version):
    saved = review_copy
    saved["role_policy_version"] = version
    path = tmp_path / "handoff.json"
    assert session._load(path, raw=json.dumps(saved).encode())["role_policy_version"] == version
    assignments = []

    def receive_assignment(_path, _saved, part_id, part, **_kwargs):
        assignments.append((part_id, part["history"][-1]["content"]))
        return session._WorkerOutcome("Checked")

    monkeypatch.setattr(session, "_worker", receive_assignment)
    first = "Проверь реализацию. Запись файлов за пределами поручения запрещена."
    calls = [
        assignment(call_id="initial", message=first),
        assignment(call_id="again", kind="correction", message="Сверь результат."),
        assignment(call_id="other", part="part_2", message="Проверь второй файл."),
    ]
    for call in calls:
        saved["lead_history"].append(call)
        session._dispatch(path, saved, [call], environ={}, transport=None)
    task = saved["spec"]["task"]
    if version >= 7:
        first = f"Architect's task (reference):\n{task}\n\nLead's assignment (scope):\n{first}"
    assert assignments == [("part_1", first), ("part_1", "Сверь результат."), ("part_2", "Проверь второй файл.")]


def test_correction_with_new_edits_delivers_the_new_diff(review_copy):
    saved = review_copy
    source = Path(saved["execution_state"]["root"]) / "src/main.py"
    source.write_text("first = True\n")
    first = {}
    assert not session._handoff_diff(saved, first)
    saved["lead_history"].extend([
        assignment(call_id="first"),
        {"type": "function_call_output", "call_id": "first", "output": json.dumps(first)}])
    source.write_text("corrected = True\n")
    second = {}
    assert not session._handoff_diff(saved, second)
    page = second["workspace_changes"]
    assert page["revision"] != first["workspace_changes"]["revision"] and "same_as" not in page
    assert "+corrected = True" in page["files"][0]["diff"]


def test_automatic_diff_can_be_deferred_before_sending_a_size_limited_lead_request(board, tmp_path, monkeypatch):
    fake_worker(tmp_path, monkeypatch)
    raw = json.loads(board.settings.read_text())
    raw["session"]["limits"] = {"request_bytes": 20_000}
    board.settings.write_text(json.dumps(raw))
    approve_flash(board, {**TEAM, "lead_profile": api.SOL_62_LEAD})

    def large_change(path, saved, *args, **kwargs):
        (Path(saved["execution_state"]["root"]) / "src/main.py").write_text("#" + "Ж" * 11_000 + "\n")
        return session._WorkerOutcome("Edit complete")

    monkeypatch.setattr(session, "_worker", large_change)
    model = api.SOL_62_MODEL
    short_page = tool("project_changes", "short", **{**workspace.DIFF_START, "max_chars": 256})
    transport, sent = lead_frames(sol(calls=[assignment(call_id="large")], model=model),
                                  sol(calls=[short_page], model=model),
                                  sol("Final report", model=model), model=model)
    assert board.run("b" * 32, credentials=lambda _: KEYS, transport=transport)["status"] == "result"
    assert len(sent) == 3
    evidence = tool_result(sent[1], "large")["workspace_changes"]
    assert evidence["total"] == 1 and "files" not in evidence
    assert evidence["next"] == {**workspace.DIFF_START, "revision": evidence["revision"],
                                "max_chars": session.project.MAX_CHARS}
    returned = tool_result(sent[2], "short")
    assert returned["revision"] == evidence["revision"] and returned["next"]["diff_offset"] == 256
    assert "+#ЖЖ" in returned["files"][0]["diff"]


def test_deferral_keeps_sent_evidence_and_fixes_references_in_an_unsent_batch(review_copy):
    saved = review_copy
    (Path(saved["execution_state"]["root"]) / "src/main.py").write_text("x = 1\n")
    page = changes(saved, workspace.DIFF_START)
    history = saved["lead_history"]

    def handoff(call_id, evidence):
        history.extend([assignment(call_id=call_id),
                        {"type": "function_call_output", "call_id": call_id,
                         "output": json.dumps({"workspace_changes": evidence})}])

    handoff("sent", page)
    sent_output = history[-1]["output"]
    saved["calls"] = [{"part_id": None, "history_items": len(history) + 1}]
    handoff("pending", page)
    handoff("duplicate", {"revision": page["revision"], "same_as": "pending", "next": None})
    assert session._defer_unsent_handoff_evidence(saved, history)
    assert history[2]["output"] == sent_output
    for item in (history[4], history[6]):
        deferred = json.loads(item["output"])["workspace_changes"]
        assert deferred["next"]["revision"] == page["revision"]
        assert "files" not in deferred and "same_as" not in deferred
    assert not session._defer_unsent_handoff_evidence(saved, history)


def test_deferral_keeps_failure_totals_when_check_details_do_not_fit(review_copy):
    saved = review_copy
    (Path(saved["execution_state"]["root"]) / "src/main.py").write_text("x = 1\n")
    page = changes(saved, workspace.DIFF_START)
    revision = page["revision"]
    counts = {"revision": revision, "total": 1, "failures": 1, "other_revisions": 0}
    checks = {**counts, "commands": [{"command": "check", "exit_code": 1}]}
    history = saved["lead_history"]
    history.extend([assignment(call_id="hand"), {
        "type": "function_call_output", "call_id": "hand",
        "output": json.dumps({"workspace_changes": page, "workspace_checks": checks}),
    }])
    assert session._defer_unsent_handoff_evidence(saved, history)
    assert json.loads(history[-1]["output"])["workspace_checks"] == checks
    assert session._defer_unsent_handoff_evidence(saved, history)
    output = json.loads(history[-1]["output"])
    assert output["workspace_checks"] == counts and output["workspace_changes"]["next"]["revision"] == revision
    assert not session._defer_unsent_handoff_evidence(saved, history)


def test_cursor_rejects_ungranted_paths_and_unsealed_data(review_copy):
    saved = review_copy
    for extra in ({"path": "private.txt"}, {"offset": True}, {"diff_offset": 1}, {"revision": "other"},
                  {"max_chars": False}, {"max_chars": workspace.LEAD_DIFF_CHARS + 1}):
        with pytest.raises(workspace.WorkspaceError, match="invalid_workspace_query"):
            changes(saved, {**workspace.DIFF_START, **extra})
    root = Path(saved["execution_state"]["root"])
    (root / "private.txt").write_text("outside the grant")
    with pytest.raises(workspace.WorkspaceError, match="workspace_scope_changed"):
        changes(saved, workspace.DIFF_START)
    saved["execution_state"]["sealed"] = False
    with pytest.raises(workspace.WorkspaceError, match="workspace_result_not_sealed"):
        changes(saved, workspace.DIFF_START)
