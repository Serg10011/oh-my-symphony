"""Shared editable artifacts and real command evidence, with offline model replies."""

from __future__ import annotations

import hashlib
from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import pytest

from test_api_project import repository
from test_api_session import KEYS, delegate, exchange, frame

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ours/hooks"))
import api_project as project  # noqa: E402
import api_resume as resume  # noqa: E402
import api_sandbox as sandbox  # noqa: E402
import api_session as session  # noqa: E402
import api_workspace as workspace  # noqa: E402


@pytest.fixture(autouse=True)
def portable_offline_policy(request, monkeypatch):
    # Ordinary file/transport tests do not need a Mac or installed Codex.
    # The explicit native probes below use the unmodified production policy.
    if request.node.name in {
        "test_offline_team_edits_runs_real_check_and_reviews_new_test",
        "test_detached_writer_cannot_change_captured_result_even_with_open_descriptor",
    }:
        return
    original = workspace.policy
    monkeypatch.setattr(sandbox, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(workspace, "policy", lambda spec, value: replace(
        original(spec, value), executable=str(Path(sys.executable).resolve())))


def tool(name, identifier, **args):
    return {"type": "function_call", "name": name, "call_id": identifier, "arguments": json.dumps(args)}


def prepared(tmp_path, **extra):
    root = repository(tmp_path)
    path = tmp_path / "session.json"
    config = {"python": str(Path(sys.executable).resolve()), "read_roots": [str(Path(sys.base_prefix).resolve())]}
    spec = {"task": "Исправь функцию и проверь результат.",
            "project": {"root": str(root), "paths": ["src", "tests", "README.md"]},
            "execution": config, **extra}
    session.create(path, spec)
    return path, root


def state(path):
    return json.loads(path.read_text())


def invoke(journal, name, **args):
    value = state(journal)
    return workspace.call(value["spec"], value["execution_state"], name, args)


def test_snapshot_is_independent_and_current_reads_include_new_tests(tmp_path):
    path, root = prepared(tmp_path)
    before = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain"])
    original = (root / "src/main.py").read_bytes()
    first = invoke(path, "read_project_file", path="src/main.py", offset=0, max_chars=4096)
    assert first["text"] == "print('committed')\n"  # Not the source checkout's uncommitted version.
    invoke(path, "write_project_file", path="src/main.py", text="answer = 42\n", expected_sha256=first["sha256"])
    invoke(path, "write_project_file", path="tests/test_answer.py", text="assert 42 == 42\n", expected_sha256="")
    now = invoke(path, "read_project_file", path="src/main.py", offset=0, max_chars=4096)
    assert now["text"] == "answer = 42\n" and now["sha256"] != first["sha256"]
    changes = invoke(path, "project_changes", offset=0)
    assert {p["path"]: p["change"] for p in changes["changes"]} == {
        "src/main.py": "modified", "tests/test_answer.py": "added"}
    assert (root / "src/main.py").read_bytes() == original
    assert subprocess.check_output(["git", "-C", str(root), "status", "--porcelain"]) == before
    assert not (Path(state(path)["execution_state"]["root"]) / ".git").exists()
    assert session.show(path)["project"]["read_only"] is False


def test_unseen_edits_are_not_overwritten_or_deleted(tmp_path):
    path, _ = prepared(tmp_path)
    first = invoke(path, "read_project_file", path="src/main.py", offset=0, max_chars=4096)
    invoke(path, "write_project_file", path="src/main.py", text="new = True\n", expected_sha256=first["sha256"])
    for name, extra in [("write_project_file", {"text": "old"}), ("delete_project_file", {})]:
        with pytest.raises(workspace.WorkspaceError, match="edit_conflict"):
            invoke(path, name, path="src/main.py", expected_sha256=first["sha256"], **extra)
    assert invoke(path, "read_project_file", path="src/main.py", offset=0, max_chars=4096)["text"] == "new = True\n"
    current = invoke(path, "read_project_file", path="src/main.py", offset=0, max_chars=4096)
    invoke(path, "delete_project_file", path="src/main.py", expected_sha256=current["sha256"])
    assert invoke(path, "project_changes", offset=0)["changes"][0]["change"] == "deleted"


@pytest.mark.parametrize("name", ["../outside", "/etc/passwd", "src/../outside", ".git/config", ".env", "outside.py", "src/credentials.json", "src/key.pem"])
def test_mutation_scope_does_not_expand_the_original_grant(tmp_path, name):
    path, _ = prepared(tmp_path)
    with pytest.raises(workspace.WorkspaceError, match="path_denied"):
        invoke(path, "write_project_file", path=name, text="not allowed", expected_sha256="")


@pytest.mark.parametrize("kind", ["file_link", "directory_link", "hardlink", "fifo"])
def test_non_regular_and_linked_files_cannot_leak_or_modify_outside_data(tmp_path, kind):
    path, _ = prepared(tmp_path)
    root = Path(state(path)["execution_state"]["root"])
    outside = tmp_path / "private.txt"
    outside.write_text("fake private data")
    target = root / "src/attack"
    if kind == "file_link":
        target.symlink_to(outside)
    elif kind == "directory_link":
        target.symlink_to(tmp_path, target_is_directory=True)
        target = target / "private.txt"
    elif kind == "hardlink":
        os.link(outside, target)
    else:
        os.mkfifo(target)
    name = target.relative_to(root).as_posix()
    for op, args in [("read_project_file", {"offset": 0, "max_chars": 4096}),
                     ("write_project_file", {"text": "overwrite", "expected_sha256": hashlib.sha256(outside.read_bytes()).hexdigest()})]:
        with pytest.raises(workspace.WorkspaceError, match="file_unavailable"):
            invoke(path, op, path=name, **args)
    assert outside.read_text() == "fake private data"


def test_search_and_changed_revision_reflect_current_worker_files(tmp_path):
    path, _ = prepared(tmp_path)
    before = invoke(path, "project_changes", offset=0)["revision"]
    invoke(path, "write_project_file", path="tests/new_test.py", text="needle = 42\n", expected_sha256="")
    result = invoke(path, "search_project_files", queries=["needle"], prefix="tests", case_sensitive=True,
                    file_offset=0, line_offset=0)
    assert result["matches"] == [{"path": "tests/new_test.py", "line": 1, "text": "needle = 42"}]
    assert result["revision"] != before
    assert invoke(path, "list_project_files", prefix="tests", offset=0)["files"][0]["path"] == "tests/new_test.py"


def test_command_created_source_outside_grant_is_not_silently_omitted(tmp_path):
    path, _ = prepared(tmp_path)
    root = Path(state(path)["execution_state"]["root"])
    (root / "outside.py").write_text("unexpected = True\n")
    with pytest.raises(workspace.WorkspaceError, match="workspace_scope_changed"):
        invoke(path, "project_changes", offset=0)


def test_human_review_uses_actual_files_including_added_tests_without_keys(tmp_path, monkeypatch, capsys):
    path, _ = prepared(tmp_path)
    invoke(path, "write_project_file", path="tests/actual.py", text="assert 6 * 7 == 42", expected_sha256="")
    before = path.read_bytes()
    monkeypatch.setattr(session.api, "execute", lambda *a, **k: pytest.fail("review called a model"))
    monkeypatch.setattr(sandbox, "run", lambda *a, **k: pytest.fail("review executed a command"))
    assert session.cli(["review", "--state", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["task_acceptance"] == "not_evaluated"
    assert result["files"][0]["path"] == "tests/actual.py" and "+assert 6 * 7 == 42\n" in result["files"][0]["diff"]
    assert "\\ No newline at end of file" in result["files"][0]["diff"]
    assert path.read_bytes() == before


def test_review_detects_change_while_collecting_evidence(tmp_path, monkeypatch):
    path, _ = prepared(tmp_path)
    invoke(path, "write_project_file", path="tests/new.py", text="x = 1\n", expected_sha256="")
    real_read = workspace.read
    def changing(spec, value, name):
        data, mode = real_read(spec, value, name)
        (Path(value["root"]) / name).write_text("x = 2\n")
        return data, mode
    monkeypatch.setattr(workspace, "read", changing)
    with pytest.raises(session.SessionError, match="workspace_review_changed"):
        session.review(path)


def test_review_rejects_unfinished_run_and_does_not_modify_history(tmp_path):
    path, _ = prepared(tmp_path)
    data = state(path)
    data["status"] = "running"
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(session.SessionError, match="execution_review_not_ready"):
        session.review(path)
    assert path.read_bytes() == before


def test_command_result_is_copied_to_fresh_inodes_before_next_tool(tmp_path, monkeypatch):
    path, _ = prepared(tmp_path)
    value = state(path)
    source = Path(value["execution_state"]["root"])
    old_inode = (source / "src/main.py").stat().st_ino
    monkeypatch.setattr(sandbox, "run", lambda *a, **k: {"output": "ok", "exit_code": 0})
    result = workspace.call(value["spec"], value["execution_state"], "run_project_command",
                            {"command": "test fixture", "timeout_seconds": 1})
    target = Path(value["execution_state"]["root"])
    assert target != source and (target / "src/main.py").stat().st_ino != old_inode
    assert value["execution_state"]["retired_roots"] == [str(source)]
    assert value["execution_state"]["sealed"] and result["revision"]
    (source / "src/main.py").write_text("late write")
    assert (target / "src/main.py").read_text() == "print('committed')\n"
    workspace.validate_state(value["execution_state"], value["spec"], value["project_snapshot"], path)


def test_command_capture_failure_stops_paid_loop_and_refuses_review(tmp_path, monkeypatch):
    path, _ = prepared(tmp_path)
    monkeypatch.setattr(workspace, "preflight", lambda *a: {"test_fixture": True})
    monkeypatch.setattr(sandbox, "run", lambda *a, **k: {"output": "ok", "exit_code": 0})
    def failed(*a):
        raise workspace.WorkspaceError("workspace_size_limit")
    monkeypatch.setattr(workspace, "_seal", failed)
    transport, sent = exchange([frame(None, [tool("run_project_command", "command", command="test fixture", timeout_seconds=1)])])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "stopped" and result["stop_reason"] == "project_tool_error" and len(sent) == 1
    assert not state(path)["execution_state"]["sealed"]
    with pytest.raises(session.SessionError, match="workspace_result_not_sealed"):
        session.review(path)


def test_invalid_command_arguments_do_not_invalidate_an_untouched_copy(tmp_path, monkeypatch):
    path, _ = prepared(tmp_path)
    monkeypatch.setattr(workspace, "preflight", lambda *a: {"test_fixture": True})
    transport, sent = exchange([frame(None, [tool("run_project_command", "bad", command="echo missing timeout")]), frame("Остановился без команды")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and len(sent) == 2
    assert state(path)["execution_state"]["sealed"]
    assert session.review(path)["total"] == 0


def test_command_permission_is_saved_before_process_launch(tmp_path, monkeypatch):
    path, _ = prepared(tmp_path)
    monkeypatch.setattr(workspace, "preflight", lambda *a: {"test_fixture": True})
    def execute(*a, **k):
        raw = state(path)
        assert raw["file_calls"][-1]["status"] == "in_flight"
        assert raw["execution_state"]["sealed"] is False
        return {"output": "ok", "exit_code": 0}
    monkeypatch.setattr(sandbox, "run", execute)
    transport, _ = exchange([frame(None, [tool("run_project_command", "check", command="test fixture", timeout_seconds=1)]), frame("Done")])
    assert session.run(path, environ=KEYS, transport=transport)["status"] == "dialogue_complete"
    assert state(path)["execution_state"]["sealed"] is True


def test_failed_isolation_preflight_stops_before_any_model_request(tmp_path, monkeypatch):
    path, _ = prepared(tmp_path)
    def denied(*args, **kwargs):
        raise sandbox.SandboxError("sandbox_probe_failed")
    monkeypatch.setattr(workspace, "preflight", denied)
    transport, sent = exchange([])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "stopped" and result["stop_reason"] == "execution_preflight_failed"
    assert result["calls"] == 0 and sent == []
    assert result["execution"]["preflight"]["error"] == "sandbox_probe_failed"
    assert session.run(path, environ=KEYS, transport=transport) == result


def test_changed_command_runtime_and_state_location_are_not_accepted(tmp_path, monkeypatch):
    path, _ = prepared(tmp_path)
    data = state(path)
    data["execution_state"]["root"] = str(tmp_path)
    path.write_text(json.dumps(data))
    monkeypatch.setattr(workspace, "preflight", lambda *a: pytest.fail("invalid location reached sandbox"))
    transport, sent = exchange([])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["execution"]["preflight"]["error"] == "execution_location_changed" and not sent


def test_read_only_sessions_cannot_acquire_execution_tools_from_model_reply(tmp_path):
    path = tmp_path / "old.json"
    root = repository(tmp_path)
    session.create(path, {"task": "Read", "project": {"root": str(root), "paths": ["src"]}})
    transport, sent = exchange([frame(None, [tool("run_project_command", "exec", command="echo forbidden", timeout_seconds=1)])])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "stopped" and result["stop_reason"] == "api_error"
    assert "execution" not in result and result["project"]["read_only"]
    assert {t["name"] for t in sent[0]["body"]["tools"]} == project.TOOL_NAMES | {"delegate_luna"}


def test_unknown_charge_after_edit_is_preserved_and_never_automatically_replayed(tmp_path, monkeypatch):
    path, _ = prepared(tmp_path)
    monkeypatch.setattr(workspace, "preflight", lambda *a: {"test_fixture": True})
    transport, sent = exchange([frame(None, [tool("write_project_file", "write", path="tests/new.py", text="x=1\n", expected_sha256="")]),
                                httpx.ReadTimeout("test-only")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "stopped" and result["calls"] == len(sent) == 2
    raw = state(path)
    assert raw["calls"][-1]["error"]["charge_possible"] is True
    assert raw["file_calls"][0]["status"] == "completed"
    assert invoke(path, "read_project_file", path="tests/new.py", offset=0, max_chars=10)["text"] == "x=1\n"
    assert session.run(path, environ={}, transport=transport) == result and len(sent) == 2
    with pytest.raises(session.SessionError, match="execution_resume_not_supported"):
        resume._boundary(raw)


def test_both_roles_receive_current_file_tools_without_private_reasoning_mix(tmp_path, monkeypatch):
    path, _ = prepared(tmp_path)
    monkeypatch.setattr(workspace, "preflight", lambda *a: {"test_fixture": True})
    transport, sent = exchange([
        frame(None, [tool("write_project_file", "write", path="tests/new.py", text="new_test = True\n", expected_sha256="")]),
        frame(None, [delegate()]),
        frame(None, [tool("project_changes", "changes", offset=0)], model="gpt-6-luna"),
        frame(None, [tool("read_project_file", "read", path="tests/new.py", offset=0, max_chars=100)], model="gpt-6-luna"),
        frame("Проверил новый файл", model="gpt-6-luna"), frame("Готово")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and result["execution"]["result"]["changes_total"] == 1
    assert result["task_acceptance"] == "not_evaluated"
    assert "new_test = True" in json.dumps(sent[4]["body"])
    assert "private-lead-reasoning" not in json.dumps(sent[4]["body"])
    for request in sent:
        names = {t["name"] for t in request["body"]["tools"]}
        assert workspace.TOOL_NAMES <= names
        assert ("delegate_luna" in names) == (request["body"]["model"] == "deepseek-flash")
    assert all(value not in path.read_text() for value in KEYS.values())
    # History remains readable when archived separately from the working copy.
    archive = tmp_path / "archived.json"
    shutil.copy2(path, archive)
    assert session.show(archive)["calls"] == 6


@pytest.mark.skipif(sys.platform != "darwin" or os.environ.get("SYMPHONY_TEST_NATIVE_SANDBOX") != "1",
                    reason="explicit native isolation integration; model responses remain offline")
def test_offline_team_edits_runs_real_check_and_reviews_new_test(tmp_path):
    path, source = prepared(tmp_path)
    original = (source / "src/main.py").read_bytes()
    sha = invoke(path, "read_project_file", path="src/main.py", offset=0, max_chars=4096)["sha256"]
    python = state(path)["spec"]["execution"]["python"]
    command = shlex.join([python, "tests/check.py"])
    check = 'import runpy\nassert runpy.run_path("src/main.py")["answer"] == 42\nprint("CHECK PASSED")\n'
    transport, sent = exchange([
        frame(None, [tool("write_project_file", "implementation", path="src/main.py", text="answer = 42\n", expected_sha256=sha)]),
        frame(None, [delegate(message="Добавь и выполни проверку текущего кода.")]),
        frame(None, [tool("read_project_file", "current", path="src/main.py", offset=0, max_chars=4096)], model="gpt-6-luna"),
        frame(None, [tool("write_project_file", "test", path="tests/check.py", text=check, expected_sha256="")], model="gpt-6-luna"),
        frame(None, [tool("run_project_command", "verify", command=command, timeout_seconds=30)], model="gpt-6-luna"),
        frame("Проверка выполнена.", model="gpt-6-luna"),
        frame(None, [tool("project_changes", "review_all", offset=0)]),
        frame(None, [tool("read_project_file", "review_test", path="tests/check.py", offset=0, max_chars=4096)]),
        frame("Изменён код, добавлена и выполнена проверка. Требуется приёмка человеком.")])
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and result["calls"] == len(sent) == 9
    assert result["execution"]["result"]["changes_total"] == 2
    assert all(result["execution"]["preflight"]["checks"].values())
    raw = state(path)
    outputs = [json.loads(i["output"]) for i in raw["parts"]["part_1"]["history"] if i.get("type") == "function_call_output"]
    evidence = next(o for o in outputs if "exit_code" in o)
    assert evidence["exit_code"] == 0 and evidence["output"].strip() == "CHECK PASSED"
    reviewed = [json.loads(i["output"]) for i in sent[-1]["body"]["input"] if i.get("type") == "function_call_output"]
    assert any(item.get("path") == "tests/check.py" and item.get("text") == check for item in reviewed)
    assert (source / "src/main.py").read_bytes() == original


@pytest.mark.skipif(sys.platform != "darwin" or os.environ.get("SYMPHONY_TEST_NATIVE_SANDBOX") != "1",
                    reason="explicit native detached-process regression; no providers")
def test_detached_writer_cannot_change_captured_result_even_with_open_descriptor(tmp_path):
    path, _ = prepared(tmp_path)
    value = state(path)
    root = Path(value["execution_state"]["root"])
    python = value["spec"]["execution"]["python"]
    child = ("import time,pathlib; f=open('src/late.py','w'); f.write('before'); f.flush(); "
             "pathlib.Path('src/ready').write_text('ready'); time.sleep(1); f.write('after'); f.flush()")
    command = (f"import subprocess,pathlib,time; subprocess.Popen([{python!r},'-I','-c',{child!r}], "
               "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True);\n"
               "while not pathlib.Path('src/ready').exists(): time.sleep(.01)")
    result = workspace.call(value["spec"], value["execution_state"], "run_project_command",
                            {"command": shlex.join([python, "-I", "-c", command]), "timeout_seconds": 10})
    assert result["exit_code"] == 0
    captured = Path(value["execution_state"]["root"])
    assert captured != root and (captured / "src/late.py").read_text() == "before"
    time.sleep(1.5)
    assert (root / "src/late.py").read_text() == "beforeafter"  # The old descriptor really wrote.
    assert (captured / "src/late.py").read_text() == "before"  # Accepted material is immutable to it.
    assert workspace.review(value["spec"], value["execution_state"], value["project_snapshot"])["revision"] == result["revision"]
