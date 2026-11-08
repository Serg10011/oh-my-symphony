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
