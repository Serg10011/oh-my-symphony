"""Browser assignments: explicit approval, immutable grants and no paid replay."""

from dataclasses import replace
import io
import json
from pathlib import Path
import subprocess
import shlex
import signal
import sys
import time
from types import SimpleNamespace

import httpx
import pytest

from test_api_project import git, repository
from test_api_session import KEYS, delegate, exchange, frame
from test_api_workspace import tool

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ours/hooks"))
import api_board as board_api  # noqa: E402
import api_session as session  # noqa: E402
import api_workspace as workspace  # noqa: E402
import api_sandbox as sandbox  # noqa: E402


@pytest.fixture()
def board(tmp_path, monkeypatch):
    root = repository(tmp_path)
    git(root, "branch", "our-main")
    folder = tmp_path / "board"
    folder.mkdir()
    kanban = folder / "kanban"
    kanban.mkdir()
    workflow = folder / "WORKFLOW.md"
    workflow.write_text("trusted workflow\n")
    (kanban / "TEST-1.md").write_text("---\nid: TEST-1\nidentifier: TEST-1\ntitle: Исправь функцию\n"
        "state: needs_human\nagent_kind: api-team\ncreated_at: '2026-11-05T01:00:00Z'\n---\n\nПроверь результат.\n")
    settings = folder / "api-settings.json"
    settings.write_text(json.dumps({"version": 1, "paths": ["src", "tests", "README.md"], "session": {},
        "execution": {"python": str(Path(sys.executable).resolve()), "read_roots": [str(Path(sys.base_prefix).resolve())]}}))
    context = {"workflow": str(workflow), "board_root": str(kanban), "repo": str(root),
               "identifier": "TEST-1", "agent_kind": "api-team", "command": "/absolute/trusted/runner",
               "active_states": ["Todo"], "terminal_states": ["needs_human", "Done", "Archive"],
               "base_branch": "our-main", "process_timeout_seconds": 7200}
    original = workspace.policy
    monkeypatch.setattr(sandbox, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(workspace, "policy", lambda spec, value: replace(
        original(spec, value), executable=str(Path(sys.executable).resolve())))
    monkeypatch.setattr(workspace, "preflight", lambda *a: {"offline_fixture": True})
    monkeypatch.setattr(board_api, "keychain_credentials", lambda *a: pytest.fail("No real credentials in tests"))
    return board_api.Board(settings, context)


def approve(board):
    token = board.prepare()["confirmation_token"]
    board.confirm(token)
    return token


def test_project_context_reused_across_cards_and_kept_on_its_board(board, tmp_path):
    settings = json.loads(board.settings.read_text())
    settings["context_paths"] = ["README.md"]
    board.settings.write_text(json.dumps(settings))
    assert board.permission.proposal()[0]["terms"]["context_paths"] == ["README.md"]
    lead_only_material = (board.repo / "README.md").read_text().strip()
    (board.repo / "README.md").write_text("Uncommitted change")

    approve(board)
    transport, requests = exchange([
        frame(None, [delegate()]), frame("Part complete", model="gpt-6-luna"), frame("Reviewed"),
    ])
    result = board.run("a" * 32, credentials=lambda _: KEYS, transport=transport)
    first = requests[0]["body"]
    first_prefix = first["instructions"].split("\n\n", 1)[0]
    assert lead_only_material in first_prefix and "Uncommitted change" not in first_prefix
    assert lead_only_material not in json.dumps(requests[1]["body"])
    assert requests[2]["body"]["instructions"].startswith(first_prefix + "\n\n")
    assert result["project_context"] == result["session"]["project_context"] == session.report(board.session_path)["project_context"]
    assert result["project_context"]["paths"] == ["README.md"]
    assert result["project_context"]["sha256"][:12] in result["report_text"]
    assert lead_only_material not in json.dumps(result)

    (board.repo / "src/main.py").write_text("# Update unrelated to the reference\n")
    git(board.repo, "add", "src/main.py")
    git(board.repo, "commit", "-qm", "Update code only")
    git(board.repo, "branch", "-f", "our-main", "HEAD")
    settings["session"]["limits"] = {"output_tokens_per_call": 256}
    board.settings.write_text(json.dumps(settings))

    def add_card(context, settings_path, identifier, title):
        path = Path(context["board_root"]) / f"{identifier}.md"
        path.write_text(f"---\nid: {identifier}\ntitle: {title}\nstate: needs_human\nagent_kind: api-team\n---\n")
        return board_api.Board(settings_path, {**context, "identifier": identifier})

    second = add_card(board.context, board.settings, "TEST-2", "Another task")
    approve(second)
    transport, requests = exchange([frame("Second result")])
    second_result = second.run("b" * 32, credentials=lambda _: KEYS, transport=transport)
    second_wire = requests[0]["body"]
    assert second_result["project"]["commit"] != result["project"]["commit"]
    assert second_result["project_context"] == result["project_context"]
    assert second_wire["instructions"].startswith(first_prefix + "\n\n")
    assert first["instructions"] != second_wire["instructions"] and first["input"] != second_wire["input"]

    (board.repo / "README.md").write_text("Updated committed reference\n")
    git(board.repo, "add", "README.md")
    git(board.repo, "commit", "-qm", "Update reference")
    git(board.repo, "branch", "-f", "our-main", "HEAD")
    changed = add_card(board.context, board.settings, "TEST-3", "Use the updated reference")
    assert changed.prepare()["project_context"]["sha256"] != result["project_context"]["sha256"]
    assert session.show(board.session_path)["project_context"] == result["project_context"]

    other = tmp_path / "other-board"
    other.mkdir()
    other_repo = repository(other, {"README.md": "Other project reference\n", "src/main.py": "# Other program\n"})
    git(other_repo, "branch", "our-main")
    (other / "kanban").mkdir()
    (other / "WORKFLOW.md").write_text("Other trusted workflow\n")
    other_settings = other / "api-settings.json"
    other_settings.write_text(json.dumps(settings))
    third = add_card({**board.context, "repo": str(other_repo), "workflow": str(other / "WORKFLOW.md"),
                      "board_root": str(other / "kanban")}, other_settings, "OTHER-1", "Different project")
    approve(third)
    transport, requests = exchange([frame("Other project result")])
    third_result = third.run("c" * 32, credentials=lambda _: KEYS, transport=transport)
    third_wire = requests[0]["body"]
    assert "Other project reference" in third_wire["instructions"] and lead_only_material not in json.dumps(third_wire)
    assert third_result["project_context"]["sha256"] != result["project_context"]["sha256"]


@pytest.mark.parametrize(("selection", "grants", "code"), [
    (["README.md"], ["src"], "project_context_file_unavailable"),
    (["src"], ["."], "project_context_file_unavailable"),
    (["../README.md"], ["."], "invalid_project_path"),
    ([".env"], ["."], "project_scope_denied"),
])
def test_project_context_requires_named_granted_files(board, selection, grants, code):
    settings = json.loads(board.settings.read_text())
    board.settings.write_text(json.dumps({**settings, "paths": grants, "context_paths": selection}))
    with pytest.raises((session.SessionError, session.project.ProjectError), match=code):
        board.prepare()
    assert not board.path.exists() and not board.session_path.exists()


def test_project_context_total_size_is_bounded_before_confirmation(board):
    from api_project_context import MAX_CONTEXT_BYTES

    settings = json.loads(board.settings.read_text())
    board.settings.write_text(json.dumps({**settings, "context_paths": ["README.md", "src/main.py"]}))
    for path in ("README.md", "src/main.py"):
        (board.repo / path).write_text("a" * (MAX_CONTEXT_BYTES // 2 + 1))
    git(board.repo, "add", "README.md", "src/main.py")
    git(board.repo, "commit", "-qm", "Oversize reference")
    git(board.repo, "branch", "-f", "our-main", "HEAD")
    with pytest.raises(session.project.ProjectError, match="project_context_too_large"):
        board.prepare()
    assert not board.path.exists() and not board.session_path.exists()


def test_project_context_for_deepseek_is_not_added_to_sol(board):
    settings = json.loads(board.settings.read_text())
    board.settings.write_text(json.dumps({**settings, "context_paths": ["README.md"]}))
    view = board.prepare({"lead_profile": "sol-lead", "worker_profile": None})
    assert "project_context" not in view
    assert "project_context" not in board._load()["spec"]


def test_saved_project_context_must_match_frozen_project_files(board):
    settings = json.loads(board.settings.read_text())
    board.settings.write_text(json.dumps({**settings, "context_paths": ["README.md"]}))
    board.prepare()
    spec = board._load()["spec"]
    session.create(board.session_path, spec)
    value = json.loads(board.session_path.read_text())
    value["spec"]["project_context"]["files"]["README.md"] = "Changed after approval"
    session._save(board.session_path, value)
    with pytest.raises(session.SessionError, match="invalid_project_state"):
        session.show(board.session_path)


def test_prepare_is_free_and_confirm_is_bound_and_idempotent(board):
    view = board.prepare()
    assert view["status"] == "prepared" and view["project"]["commit"] != "our-main"
    assert view["task"].startswith("Исправь функцию") and view["profiles"]["lead"]["effort"] == "high"
    assert board.card()[0]["state"] == "needs_human" and not board.session_path.exists()
    assert "confirmation_token" not in board.show() and "token_sha256" not in board.show()
    token = view["confirmation_token"]
    assert board.confirm(token)["status"] == "queued" and board.card()[0]["state"] == "Todo"
    assert board.confirm(token)["status"] == "queued"
    board.stop()
    assert board.confirm(token)["status"] == "stopped" and board.card()[0]["state"] == "needs_human"
    with pytest.raises(board_api.BoardError, match="already_used"):
        board.prepare()


@pytest.mark.parametrize("change", ["title", "description", "agent_kind", "created_at", "settings", "workflow", "commit"])
def test_changed_approval_is_refused_before_queue_or_credentials(board, change):
    token = board.prepare()["confirmation_token"]
    if change == "settings":
        raw = json.loads(board.settings.read_text())
        raw["session"]["lead_effort"] = "max"
        board.settings.write_text(json.dumps(raw))
    elif change == "workflow":
        board.workflow.write_text("different grants")
    elif change == "commit":
        (board.repo / "README.md").write_text("new base")
        git(board.repo, "add", "-A")
        git(board.repo, "commit", "-qm", "new base")
        git(board.repo, "branch", "-f", "our-main", "HEAD")
    elif change == "created_at":
        path = board.tracker.find_path(board.identifier)
        path.write_text(path.read_text().replace("2026-11-05", "2026-11-06"))
    else:
        board.tracker.update_fields(board.identifier, **{change: "different"})
    with pytest.raises(board_api.BoardError, match="changed"):
        board.confirm(token)
    assert board.show()["status"] == "prepared" and not board.session_path.exists()


def test_wrong_and_expired_confirmation_are_refused(board, monkeypatch):
    token = board.prepare()["confirmation_token"]
    with pytest.raises(board_api.BoardError, match="invalid_api_confirmation"):
        board.confirm("wrong")
    monkeypatch.setattr(board_api.time, "time", lambda: 1e15)
    with pytest.raises(board_api.BoardError, match="expired"):
        board.confirm(token)
    assert board.card()[0]["state"] == "needs_human"


def test_changed_queued_assignment_never_calls_provider(board):
    approve(board)
    board.workflow.write_text("changed after confirmation")
    result = board.run("a" * 32)
    assert result["status"] == "stopped" and result["reason"] == "api_assignment_changed"
    assert not board.session_path.exists()


def test_invalidated_queued_profile_stops_instead_of_remaining_queued(board):
    approve(board)
    board.tracker.update_fields(board.identifier, agent_kind="claude")
    result = board.run("a" * 32)
    assert result["status"] == "stopped" and result["reason"] == "api_assignment_changed"
    assert not board.session_path.exists()


def test_archived_queue_entry_cannot_charge_or_reopen_the_card(board):
    approve(board)
    board.tracker.update_fields(board.identifier, state="Archive")
    result = board.run("a" * 32)
    assert result["status"] == "stopped" and result["session"]["calls"] == 0
    assert board.card()[0]["state"] == "Archive"


def test_archiving_prepared_card_invalidates_old_confirmation(board):
    token = board.prepare()["confirmation_token"]
    board.tracker.update_fields(board.identifier, state="Archive")
    with pytest.raises(board_api.BoardError, match="api_assignment_changed"):
        board.confirm(token)
    with pytest.raises(board_api.BoardError, match="must_wait_for_confirmation"):
        board.prepare()
    assert board.card()[0]["state"] == "Archive" and not board.session_path.exists()


@pytest.mark.parametrize("change", [{"state": "Archive"}, {"description": "Новая задача"}])
def test_changed_running_card_stops_before_next_provider_call(board, change):
    approve(board)
    calls = []
    def answer(request):
        calls.append(request)
        board.tracker.update_fields(board.identifier, **change)
        return httpx.Response(200, json=frame(None, [delegate()]))
    result = board.run("a" * 32, credentials=lambda _: KEYS, transport=httpx.MockTransport(answer))
    assert len(calls) == 1 and result["reason"] == "operator_stopped"
    if "state" in change:
        assert board.card()[0]["state"] == "Archive"
    else:
        assert "Новая задача" in board.card()[1]


def test_no_assignment_no_charge_even_if_card_is_manually_moved(board):
    board.tracker.update_fields(board.identifier, state="Todo")
    with pytest.raises(board_api.BoardError, match="not_queued"):
        board.run("a" * 32)


def test_team_produces_current_files_and_costs_but_never_claims_acceptance(board):
    token = approve(board)
    transport, sent = exchange([
        frame(None, [delegate()]),
        frame(None, [tool("write_project_file", "write", path="tests/test_new.py", text="assert 1 + 1 == 2\n",
                          expected_sha256="")], model="gpt-6-luna"),
        frame("Добавлен тест", model="gpt-6-luna"),
        frame(None, [tool("read_project_file", "review", path="tests/test_new.py", offset=0, max_chars=4096)]),
        frame("Проверен фактический новый файл; тесты доски ещё не запускались.")])
    loaded = []
    result = board.run("a" * 32, credentials=lambda spec: loaded.append(spec) or KEYS, transport=transport)
    assert len(sent) == 5 and len(loaded) == 1
    assert result["status"] == "result" and result["session"]["calls"] == 5
    assert not result["accepted"] and not result["delivered"]
    assert "USD" in result["report_text"] and "gpt-6-luna" in result["report_text"]
    assert board.card()[0]["state"] == "needs_human"
    review = board.review()
    assert review["files"][0]["path"] == "tests/test_new.py"
    assert not (board.repo / "tests/test_new.py").exists()
    assert board.confirm(token)["status"] == "result"
    with pytest.raises(board_api.BoardError, match="not_queued"):
        board.run("b" * 32, credentials=lambda _: KEYS, transport=transport)
    assert len(sent) == 5


def test_isolation_failure_precedes_key_access(board, monkeypatch):
    approve(board)
    def fail(*a):
        raise sandbox.SandboxError("isolation_test_failed")
    monkeypatch.setattr(workspace, "preflight", fail)
    result = board.run("a" * 32)
    assert result["reason"] == "execution_preflight_failed"
    assert result["session"]["calls"] == 0


def test_stop_between_roles_prevents_worker_charge_and_preserves_lead_usage(board):
    approve(board)
    calls = []
    def answer(request):
        calls.append(request)
        board.stop()
        return httpx.Response(200, json=frame(None, [delegate()]))
    result = board.run("a" * 32, credentials=lambda _: KEYS, transport=httpx.MockTransport(answer))
    assert len(calls) == 1 and result["reason"] == "operator_stopped"
    assert result["session"]["usage"]["deepseek-lead"]["input_tokens"] == 12
    assert result["session"]["usage"]["luna-worker"]["calls"] == 0


def test_interrupt_keeps_unknown_request_cost_and_never_retries(board):
    approve(board)
    def interrupt(request):
        raise board_api.Interrupted()
    result = board.run("a" * 32, credentials=lambda _: KEYS, transport=httpx.MockTransport(interrupt))
    assert result["status"] == "stopped" and result["session"]["calls"] == 1
    assert result["session"]["usage"]["deepseek-lead"]["input_tokens"] is None
    assert session.spend_report(session._load(board.session_path))["totals"]["unknown_usage_calls"] == 1


def test_orphan_process_stops_before_credentials(board):
    approve(board)
    result = board.run("a" * 32, cancelled=lambda: True)
    assert result["reason"] == "operator_stopped" and result["session"]["calls"] == 0


def test_stop_interrupts_command_and_seals_its_partial_files(board, monkeypatch):
    approve(board)
    value = board._load()
    session.create(board.session_path, value["spec"])
    state = session._load(board.session_path)
    root = Path(state["execution_state"]["root"])
    script = f"import pathlib,time; pathlib.Path({str(root / 'src/partial.py')!r}).write_text('partial'); time.sleep(10)"
    monkeypatch.setattr(sandbox.Policy, "argv", lambda *a: [sys.executable, "-I", "-c", script])
    started = time.monotonic()
    result = workspace.call(state["spec"], state["execution_state"], "run_project_command",
        {"command": "test-owned command", "timeout_seconds": 20}, cancelled=lambda: time.monotonic() - started > .2)
    assert result["stop_reason"] == "operator_stopped" and time.monotonic() - started < 3
    assert state["execution_state"]["sealed"] and Path(state["execution_state"]["root"]) != root
    assert (Path(state["execution_state"]["root"]) / "src/partial.py").read_text() == "partial"


def test_reading_crashed_owner_recovers_status_without_paid_replay(board):
    approve(board)
    value = board._load()
    value.update(status="running", run_id="a" * 32)
    board._save(value)
    session.create(board.session_path, value["spec"])
    result = board.show()
    assert result["status"] == "stopped" and result["reason"] == "board_execution_failed"
    assert board.card()[0]["state"] == "needs_human"
    assert board.show()["session"]["calls"] == 0


def test_keys_read_once_per_provider_and_only_in_host_memory(board, monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(board_api, "sys", SimpleNamespace(platform="darwin"))
    calls = []
    def command(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(returncode=0, stdout=b"dummy-provider-key\n")
    monkeypatch.setattr(subprocess, "run", command)
    spec = session.validate_spec({"task": "test"})
    result = board_api.keychain_credentials(spec)
    assert len(calls) == 2 and set(result) == set(KEYS)
    assert [c[0][3] for c in calls] == ["symphony.deepseek", "symphony.openai"]
    assert all(not any(k in kw["env"] for k in KEYS) for _, kw in calls)
    assert not board.session_path.exists()


@pytest.mark.asyncio
async def test_core_bridge_and_real_controller_exchange_without_provider_access(board):
    from symphony.api_team import board_context, invoke
    from symphony.workflow import build_service_config, parse_workflow_text

    # This subprocess is a test-owned provider fixture, never a production
    # bypass. It runs the actual CLI, controller and session state machine.
    runner = board.workflow.parent / "offline provider fixture.py"
    hooks = str(Path(board_api.__file__).parent)
    replies = [frame(None, [tool("write_project_file", "write", path="tests/fixture.py",
        text="assert True\n", expected_sha256="")]), frame("Изменение подготовлено")]
    runner.write_text(f"""import sys,json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, {hooks!r})
import httpx, api_board, api_workspace, api_sandbox
# Mirror the parent fixture's platform double inside this test-only child.
api_sandbox.sys = SimpleNamespace(platform="darwin")
api_board.keychain_credentials = lambda _: {KEYS!r}
api_workspace.preflight = lambda *a: {{'offline_fixture': True}}
original = api_workspace.policy
api_workspace.policy = lambda spec,value: replace(original(spec,value),executable=str(Path(sys.executable).resolve()))
replies = iter(json.loads({json.dumps(replies)!r}))
transport = httpx.MockTransport(lambda request: httpx.Response(200,json=next(replies)))
original_run = api_board.session.run
api_board.session.run = lambda *a,**kw: original_run(*a,**{{**kw,'transport':transport}})
raise SystemExit(api_board.cli())
""")
    # The trusted runner needs the project's already-installed dependencies.
    command = shlex.join([str(Path(sys.prefix) / "bin/python"), str(runner), "--settings", str(board.settings)])
    text = "---\ntracker: {kind: file, board_root: ./kanban, active_states: [Todo], terminal_states: [needs_human, Done]}\n" \
           "agent:\n  kind: claude\n  feature_base_branch: our-main\n  profiles:\n    api-team:\n      format: api-team-v1\n      command: " \
           + json.dumps(command) + "\n---\nFixture\n"
    board.workflow.write_text(text)
    cfg = build_service_config(parse_workflow_text(text, board.workflow))
    cfg = replace(cfg, code_repo=board.repo)
    board.context = board_context(cfg, "api-team", board.identifier)
    prepared = await invoke(cfg, "api-team", board.identifier, "prepare")
    assert prepared["status"] == "prepared"
    confirmed = await invoke(cfg, "api-team", board.identifier, "confirm",
        payload={"confirmation_token": prepared["confirmation_token"]})
    assert confirmed["status"] == "queued"
    result = await invoke(cfg, "api-team", board.identifier, "run", run_id="a" * 32)
    assert result["status"] == "result" and result["session"]["calls"] == 2
    review = await invoke(cfg, "api-team", board.identifier, "review")
    assert review["files"][0]["path"] == "tests/fixture.py"
    assert board.card()[0]["state"] == "needs_human" and not (board.repo / "tests/fixture.py").exists()


@pytest.mark.parametrize("finish", ["success", "service_stop", "operator_stop"])
def test_cli_signals_durable_progress_and_identifies_interrupts(board, monkeypatch, capsysbinary, finish):
    from symphony.api_team import CHECKPOINT, CHECKPOINT_ENV

    approve(board)
    raw = json.dumps({"context": board.context, "run_id": "a" * 32}).encode()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw)))
    monkeypatch.setenv(CHECKPOINT_ENV, "1")
    monkeypatch.setattr(board_api, "keychain_credentials", lambda _: KEYS)

    def reply(request):
        # The request is already durable when its checkpoint reaches the pipe.
        prefix = capsysbinary.readouterr().out
        assert prefix and not prefix.strip(CHECKPOINT)
        assert session._load(board.session_path)["calls"][-1]["status"] == "in_flight"
        if finish == "operator_stop":
            board.stop()
        if finish != "success":
            signal.raise_signal(signal.SIGTERM)
        return httpx.Response(200, json=frame("Готово"))

    run = session.run
    def offline_run(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(reply)
        return run(*args, **kwargs)
    monkeypatch.setattr(session, "run", offline_run)
    previous = signal.getsignal(signal.SIGTERM)
    try:
        assert board_api.cli(["--settings", str(board.settings), "--action", "run"]) == 0
    finally:
        signal.signal(signal.SIGTERM, previous)
    raw_reply = capsysbinary.readouterr().out
    result = json.loads(raw_reply.lstrip(CHECKPOINT))
    if finish == "success":
        assert raw_reply.startswith(CHECKPOINT) and result["status"] == "result"
    else:
        expected = "board_process_interrupted" if finish == "service_stop" else "operator_stopped"
        assert result["status"] == "stopped" and result["reason"] == expected
        report = session.spend_report(session._load(board.session_path))
        assert report["stop_reason"] == expected and report["totals"]["unknown_usage_calls"] == 1
