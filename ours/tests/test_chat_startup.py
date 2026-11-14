"""Keep the optional OMS login recovery separate from task dispatch."""

import fcntl
import json
from pathlib import Path
import plistlib
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from symphony.api_team import FINISHED_HOLD
from symphony.orchestrator.run_registry import RunRegistry
from symphony.trackers.file import issue_from_file

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import chat_startup as startup  # noqa: E402

UUID = "0BDFAD53-1C02-4F8C-9BA7-A8E99B74D7D2"


@pytest.fixture
def layout(tmp_path):
    root = tmp_path.resolve()
    layout = startup.Layout(root / "work", root, root / "home")
    for path in (layout.local, layout.work / "host", layout.board / ".symphony",
                 layout.board / "kanban", layout.main / "ours/bin", layout.main / startup.STATE_REL.parent):
        path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(layout.clone)], check=True)
    start = layout.work / "host/start_service.py"
    start.write_text("# Existing host entry point\n")
    (layout.main / "ours/bin/oms-chat-startup").write_text("# Installed entry point\n")
    log = str(layout.home / "Library/Logs/SymphonyLauncher/bootstrap.log")
    spec = {"Label": startup.OMS, "RunAtLoad": True, "KeepAlive": False, "WorkingDirectory": str(layout.board),
            "ProgramArguments": [str(layout.python), "-m", "symphony.cli", str(layout.workflow),
                                 "--host", "127.0.0.1", "--port", str(startup.OMS_PORT)]}
    (layout.board / "launchd.plist").write_bytes(plistlib.dumps(spec))
    registration = {**spec, "ProgramArguments": [str(layout.python), "-I", "-B", str(start), startup.OMS],
                    "StandardOutPath": log, "StandardErrorPath": log}
    layout.plist(startup.OMS).write_bytes(plistlib.dumps(registration))
    layout.workflow.write_text(f"""---
tracker:
  kind: file
  board_root: ./kanban
  active_states: [Todo, In Progress, Verify]
  terminal_states: [Done, Archive, Human Review, needs_human]
workspace:
  code_repo: {json.dumps(str(layout.clone))}
agent:
  auto_merge_on_done: false
---
Work on the selected card.
""")
    with sqlite3.connect(database(layout)) as conn:
        conn.executescript("""CREATE TABLE runs (completed_at TEXT);
            INSERT INTO runs VALUES ('2026-11-08');
            CREATE TABLE issue_flags (retry_attempt INTEGER);""")
    (layout.main / startup.STATE_REL.with_suffix(".json.lock")).touch()
    card(layout).write_text("---\nidentifier: OMS-11\nstate: Done\n---\nSaved result.\n")
    return layout


def card(layout):
    return layout.board / "kanban/OMS-11.md"


def database(layout):
    return layout.board / ".symphony/state.db"


@pytest.fixture
def saved_api_result(layout):
    layout.workflow.write_text(layout.workflow.read_text().replace(
        "  auto_merge_on_done: false\n",
        "  auto_merge_on_done: false\n"
        "  profiles:\n"
        "    api-team:\n"
        "      format: api-team-v1\n"
        "      command: /usr/bin/python3 /test/api_board.py --settings /test/settings.json\n",
    ))
    card(layout).write_text(
        "---\nid: OMS-11\nidentifier: OMS-11\ntitle: Saved result\nstate: Verify\n"
        "agent:\n  kind: api-team\n---\nSaved result.\n"
    )
    database(layout).unlink()
    registry = RunRegistry(database(layout))
    issue = issue_from_file(card(layout))
    assert issue is not None
    run_id = registry.acquire_run(issue, workspace_path=layout.work, attempt=1,
                                  attempt_kind="initial", agent_kind="api-team")
    assert run_id is not None
    assert registry.complete_run(issue_id=issue.id, run_id=run_id, status="api_team_finished")
    registry.set_issue_flags(issue.id, paused=True, pause_reason=FINISHED_HOLD)
    finished_at = registry.get_run(run_id).completed_at
    registry.close()
    assert finished_at is not None
    assignment = layout.board / ".ours-state/api-team/OMS-11/assignment.json"
    assignment.parent.mkdir(parents=True)
    startup.save(assignment, {
        "version": 1, "identifier": issue.identifier, "status": "result",
        "run_id": run_id, "finished_at": finished_at.isoformat(),
        "result_lane": "Verify", "check_lane": "Verify",
        "candidate": {"identifier": issue.identifier, "status": "awaiting_full_checks",
                      "commit": "a" * 40, "checks": {"static": "passed", "full": "not_run"}},
    })
    return assignment


def test_prepares_two_registrations_without_starting_a_service(layout, monkeypatch):
    monkeypatch.setattr(startup, "volume_uuid", lambda _layout: UUID)
    monkeypatch.setattr(startup, "applied_revision", lambda _layout: "a" * 40)
    files = startup.prepare(layout)
    assert list(files) == [layout.plist(startup.CONNECTOR), layout.plist(startup.LOGIN)]
    assert startup.prepare(layout) == files
    for path, payload in files.items():
        assert path.read_bytes() == payload
        assert path.stat().st_mode & 0o777 == 0o600
        spec = plistlib.loads(payload)
        assert spec["RunAtLoad"] and spec["ProgramArguments"][:2] == ["/bin/sh", "-c"]
        assert str(layout.home) in spec["StandardOutPath"]
    assert plistlib.loads(files[layout.plist(startup.LOGIN)])["StartInterval"] == 60
    assert "StartInterval" not in plistlib.loads(files[layout.plist(startup.CONNECTOR)])
    changed = layout.plist(startup.CONNECTOR)
    changed.write_bytes(b"a different registration")
    with pytest.raises(startup.StartupError, match="different_registration"):
        startup.prepare(layout)
    assert changed.read_bytes() == b"a different registration"


def test_existing_oms_registration_must_match_the_ssd_wrapper(layout):
    path = startup.oms_registration(layout)
    spec = plistlib.loads(path.read_bytes())
    path.write_bytes(plistlib.dumps({**spec, "KeepAlive": True}))
    with pytest.raises(startup.StartupError, match="oms_registration_changed"):
        startup.oms_registration(layout)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS launch guard")
@pytest.mark.parametrize("missing", [False, True])
def test_shell_waits_before_executing_any_ssd_program(layout, missing):
    volume = layout.volume / "absent" if missing else layout.volume
    marker = layout.home / "executed"
    program = layout.volume / "probe"
    program.write_text('#!/bin/sh\n: > "$4"\n')
    program.chmod(0o700)
    subprocess.run(["/bin/sh", "-c", startup.MOUNT_GUARD, "test", str(volume), UUID, str(program),
                    str(program), str(layout.status), str(marker)], check=True)
    assert startup.status(layout)["oms"] == startup.status(layout)["connector"] == "ssd_waiting"
    assert not marker.exists()


@pytest.mark.parametrize("state", ["Done", "Archive", "Human Review"])
def test_only_known_terminal_lanes_can_recover(layout, state):
    card(layout).write_text(f"---\nidentifier: OMS-11\nstate: {state}\n---\n")
    assert startup.queue_is_safe(layout)


@pytest.mark.parametrize("state", ["Todo", "In Progress", "Verify", "needs_human", "unknown", "[Done]"])
def test_unfinished_or_unrecognized_card_holds_oms(layout, state):
    card(layout).write_text(f"---\nidentifier: OMS-11\nstate: {state}\n---\n")
    assert not startup.queue_is_safe(layout)


def test_unreadable_card_or_db_fails_closed(layout):
    card(layout).write_text("Unrecognized card")
    assert not startup.queue_is_safe(layout)
    card(layout).unlink()
    outside = layout.home / "another.md"
    outside.write_text("---\nidentifier: OMS-11\nstate: Done\n---\n")
    card(layout).symlink_to(outside)
    assert not startup.queue_is_safe(layout)
    card(layout).unlink()
    database(layout).unlink()
    assert not startup.queue_is_safe(layout)


@pytest.mark.parametrize("statement", ["INSERT INTO runs VALUES (NULL)", "INSERT INTO issue_flags VALUES (2)"])
def test_unfinished_run_or_retry_holds_oms(layout, statement):
    with sqlite3.connect(database(layout)) as conn:
        conn.execute(statement)
    assert not startup.queue_is_safe(layout)


def test_workflow_cannot_make_a_safe_lane_dispatchable(layout):
    workflow = layout.workflow.read_text().replace("[Todo, In Progress, Verify]", "[Todo, Done]")
    layout.workflow.write_text(workflow)
    assert not startup.queue_is_safe(layout)


def test_oms_checks_queue_again_after_doctor(layout, monkeypatch):
    commands = []

    def doctor(*argv, timeout):
        commands.append(argv)
        card(layout).write_text("---\nidentifier: OMS-11\nstate: Todo\n---\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(startup, "run", doctor)
    monkeypatch.setattr(startup, "job", lambda _label: None)
    monkeypatch.setattr(startup, "listeners", lambda _port: set())
    assert startup.ensure_oms(layout) == "queue_waiting"
    assert len(commands) == 1 and commands[0][-2:] == ("doctor", str(layout.workflow))


def test_oms_recovers_with_saved_api_result(layout, saved_api_result, monkeypatch):
    files = (card(layout), saved_api_result, database(layout))
    before = [path.read_bytes() for path in files]
    commands = []

    def run(*argv, **_kwargs):
        commands.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(startup, "job", lambda _label: None)
    monkeypatch.setattr(startup, "listeners", lambda _port: set())
    monkeypatch.setattr(startup, "run", run)
    assert startup.ensure_oms(layout) == "starting"
    assert [command[0] for command in commands] == [str(layout.python), "/bin/launchctl"]
    assert commands[1][1:] == ("bootstrap", startup.target(startup.OMS).rsplit("/", 1)[0],
                               str(layout.plist(startup.OMS)))
    assert [path.read_bytes() for path in files] == before


@pytest.mark.parametrize("change", [
    "missing_assignment", "queued_assignment", "missing_candidate", "other_run",
    "legacy_agent", "missing_hold", "retry_pending", "newer_run",
])
def test_verify_requires_a_finished_api_result_without_retry(layout, saved_api_result, change):
    if change == "legacy_agent":
        ticket = card(layout)
        ticket.write_text(ticket.read_text().replace("  kind: api-team", "  kind: claude"))
    elif change == "missing_assignment":
        saved_api_result.unlink()
    elif change == "newer_run":
        registry = RunRegistry(database(layout))
        issue = issue_from_file(card(layout))
        assert issue is not None
        other = registry.acquire_run(issue, workspace_path=layout.work, attempt=2,
                                     attempt_kind="initial", agent_kind="api-team")
        assert other is not None
        assert registry.complete_run(issue_id=issue.id, run_id=other, status="api_team_failed")
        registry.close()
    elif change in {"missing_hold", "retry_pending"}:
        with sqlite3.connect(database(layout)) as conn:
            if change == "missing_hold":
                conn.execute("DELETE FROM issue_flags")
            else:
                conn.execute("UPDATE issue_flags SET retry_attempt = 2")
    else:
        value = json.loads(saved_api_result.read_bytes())
        if change == "queued_assignment":
            value["status"] = "queued"
        elif change == "missing_candidate":
            del value["candidate"]
        else:
            value["run_id"] = "b" * 32
        startup.save(saved_api_result, value)
    assert not startup.queue_is_safe(layout)


def test_oms_starts_once_and_accepts_only_its_own_health(layout, monkeypatch):
    commands, jobs, sockets = [], {}, set()

    def run(*argv, **kwargs):
        commands.append(argv)
        if argv[1] == "bootstrap":
            jobs[startup.OMS] = (layout.plist(startup.OMS), 531)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(startup, "job", jobs.get)
    monkeypatch.setattr(startup, "run", run)
    monkeypatch.setattr(startup, "listeners", lambda _port: sockets)
    monkeypatch.setattr(startup.service, "is_symphony_workflow_reachable",
                        lambda host, port, path: (host, port, path) == ("127.0.0.1", startup.OMS_PORT, layout.workflow))
    assert startup.ensure_oms(layout) == "starting"
    assert startup.ensure_oms(layout) == "starting"
    sockets.add((531, f"127.0.0.1:{startup.OMS_PORT}"))
    assert startup.ensure_oms(layout) == "ready"
    sockets.add((831, f"*:{startup.OMS_PORT}"))
    assert startup.ensure_oms(layout) == "port_conflict"
    assert len(commands) == 2 and commands[1][-1] == str(layout.plist(startup.OMS))


def test_installation_lock_holds_both_actions_and_incomplete_install_does_too(layout):
    lock = layout.main / startup.STATE_REL.with_suffix(".json.lock")
    with lock.open("r+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        assert startup.tick(layout, UUID)["oms"] == "installation_pending"
    assert startup.tick(layout, UUID)["connector"] == "installation_pending"


def test_connector_restarts_only_a_managed_outdated_process(layout, monkeypatch):
    path = layout.plist(startup.CONNECTOR)
    path.write_bytes(startup.registrations(layout, UUID)[path])
    job = (path, 401)
    bound = {(401, f"127.0.0.1:{startup.CONNECTOR_PORT}")}
    commands = []
    monkeypatch.setattr(startup, "job", lambda _label: job)
    monkeypatch.setattr(startup, "listeners", lambda _port: bound)
    monkeypatch.setattr(startup, "connector_version", lambda _layout, head: head)
    monkeypatch.setattr(startup, "run", lambda *args: commands.append(args) or SimpleNamespace(returncode=0))
    startup.save(layout.receipt, {"pid": 401, "work": str(layout.work), "version": "old"})
    assert startup.ensure_connector(layout, UUID, "old") == "ready"
    assert startup.ensure_connector(layout, UUID, "new") == "starting"
    assert commands == [("/bin/launchctl", "kickstart", "-k", startup.target(startup.CONNECTOR))]
    job = (path, 402)
    bound = {(402, f"127.0.0.1:{startup.CONNECTOR_PORT}")}
    startup.save(layout.receipt, {"pid": 402, "work": str(layout.work), "version": "new"})
    assert startup.ensure_connector(layout, UUID, "new") == "ready"
    job = (path.with_suffix(".other"), 402)
    assert startup.ensure_connector(layout, UUID, "new") == "registration_conflict"
    job = (path, 403)
    assert startup.ensure_connector(layout, UUID, "new") == "port_conflict"
    assert len(commands) == 1
