"""Both roles can discover new paths; a rejected shell cannot supply a result."""

import json
from pathlib import Path

import pytest

from test_api_project import repository
from test_api_session import KEYS, delegate, exchange, frame
from test_api_workspace import (
    invoke, portable_offline_policy as portable_offline_policy, sandbox, session, state, tool, workspace,
)

pytestmark = pytest.mark.usefixtures("portable_offline_policy")
SCOPES = ["src", "tests/scratch", "tests/test_costs.py"]


@pytest.fixture
def task(tmp_path, monkeypatch):
    import sys

    repo = repository(tmp_path)
    path = tmp_path / "session.json"
    session.create(path, {"task": "Проверь сводку.", "project": {"root": str(repo), "paths": SCOPES},
                          "execution": {"python": str(Path(sys.executable).resolve()),
                                        "read_roots": [str(Path(sys.base_prefix).resolve())]}})
    monkeypatch.setattr(workspace, "preflight", lambda *a: {"fixture": True})
    return path, repo


def write(call_id, path, text, expected=""):
    return tool("write_project_file", call_id, path=path, text=text, expected_sha256=expected)


def test_lead_and_worker_receive_real_grant_and_can_correct_a_denied_new_name(task):
    path, repo = task
    transport, sent = exchange([
        frame(None, [delegate()]),
        frame(None, [write("outside", "tests/test_cost.py", "rejected")], model="gpt-6-luna"),
        frame(None, [write("inside", "tests/test_costs.py", "assert True\n")], model="gpt-6-luna"),
        frame("Проверка добавлена", model="gpt-6-luna"), frame("Готово")])
    assert invoke(path, "list_project_files", prefix="tests", offset=0)["files"] == []
    result = session.run(path, environ=KEYS, transport=transport)
    assert result["status"] == "dialogue_complete" and len(sent) == 5
    for request in sent[:2]:
        assert all(name in request["body"]["instructions"] for name in SCOPES)
        assert str(repo) not in request["body"]["instructions"]
    outputs = [i for i in sent[2]["body"]["input"] if i.get("call_id") == "outside" and "output" in i]
    denied = json.loads(outputs[-1]["output"][-1]["text"])
    assert denied["error"] == "workspace_path_denied" and denied["scope_examples"][0] == "tests/test_costs.py"
    # A later worker turn still offers the prior tool boundary for cache lookup.
    previous = next(i for i in sent[3]["body"]["input"] if i.get("call_id") == "outside" and "output" in i)
    assert previous["output"][-1]["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert {f["path"] for f in session.review(path)["files"]} == {"tests/test_costs.py"}
    assert all(c["request_version"] == 3 for c in state(path)["calls"])
    assert not (repo / "tests").exists()


def test_rejected_command_stops_once_and_only_prior_edits_are_reviewable(task, monkeypatch):
    path, repo = task
    original = (repo / "src/main.py").read_bytes()
    first = invoke(path, "read_project_file", path="src/main.py", offset=0, max_chars=100)
    def run_command(*a, **kw):
        saved = state(path)["execution_state"]
        checkpoint, work = Path(saved["before_command"]["root"]), Path(saved["root"])
        assert not saved["sealed"] and (checkpoint / "src/main.py").read_text() == "prior = 1\n"
        assert (checkpoint / "src/main.py").stat().st_ino != (work / "src/main.py").stat().st_ino
        (work / "src/main.py").write_text("from_rejected_command = 1\n")
        (work / "outside.py").write_text("rejected")
        return {"output": "not proof", "exit_code": 0}
    monkeypatch.setattr(sandbox, "run", run_command)
    transport, sent = exchange([frame(None, [write("prior", "src/main.py", "prior = 1\n", first["sha256"])]),
                                frame(None, [tool("run_project_command", "cmd", command="fixture", timeout_seconds=1)])])
    result = session.run(path, environ=KEYS, transport=transport)
    assert (result["status"], result["stop_reason"]) == ("stopped", "workspace_command_outside_scope")
    assert result["execution"]["review_source"] == "before_command"
    assert session.run(path, environ={}, transport=transport) == result and len(sent) == 2
    saved = state(path)["execution_state"]
    preview = session.review(path)
    assert preview["source"] == "before_command" and preview["revision"] == saved["before_command"]["revision"]
    assert [f["path"] for f in preview["files"]] == ["src/main.py"]
    assert "+prior = 1" in preview["files"][0]["diff"] and "rejected" not in preview["files"][0]["diff"]
    assert not saved["sealed"] and result["execution"]["result"] is None and (repo / "src/main.py").read_bytes() == original
    checkpoint = Path(saved["before_command"]["root"])
    (checkpoint / "src/main.py").write_text("tampered")
    with pytest.raises(session.SessionError, match="workspace_review_changed"):
        session.review(path)
    checkpoint.rename(checkpoint.with_suffix(".missing"))
    with pytest.raises(session.SessionError, match="workspace_review_changed"):
        session.review(path)


def test_successful_command_returns_its_entire_result_and_drops_temporary_checkpoint(task, monkeypatch):
    path, _ = task
    def run_command(*a, **kw):
        saved = state(path)["execution_state"]
        assert Path(saved["before_command"]["root"]).is_dir()
        (Path(saved["root"]) / "src/new.py").write_text("value = 1\n")
        return {"output": "ok", "exit_code": 0}
    monkeypatch.setattr(sandbox, "run", run_command)
    transport, _ = exchange([frame(None, [write("test", "tests/test_costs.py", "assert True\n")]),
                             frame(None, [tool("run_project_command", "cmd", command="fixture", timeout_seconds=1)]), frame()])
    assert session.run(path, environ=KEYS, transport=transport)["status"] == "dialogue_complete"
    saved = state(path)["execution_state"]
    assert saved["sealed"] and "before_command" not in saved and not list(path.parent.glob(path.name + ".before-*"))
    preview = session.review(path)
    assert "source" not in preview and {f["path"] for f in preview["files"]} == {"src/new.py", "tests/test_costs.py"}
