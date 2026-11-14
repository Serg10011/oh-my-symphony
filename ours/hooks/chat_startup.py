"""Opt-in recovery of the existing SSD-backed OMS and project connector."""

from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import socket
import sqlite3
import stat
import subprocess
import tempfile
import time

import yaml

from post_merge import STATE_REL
from symphony import api_team, service
from symphony.issue import normalize_state, parse_iso_timestamp
from symphony.workflow import WorkflowState


LOGIN = "local.symphony.chat-availability"
CONNECTOR = "local.symphony.project-connect"
OMS = "local.symphony.oms"
CONNECTOR_PORT = 8792
OMS_PORT = 9972
INTERVAL = 60
SAFE_LANES = {"done", "archive", "human review"}
SYSTEM_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
COMMIT = re.compile(r"[0-9a-f]{40}")
UUID = re.compile(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}")
RUNTIME = ("ours/bin/project-connect", "ours/bin/oms-chat-startup", "ours/hooks", "src/symphony", "pyproject.toml")
UNAVAILABLE = (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError, subprocess.SubprocessError)
REASONS = {
    "ready": "Работает на локальном адресе.",
    "starting": "Запускается. Ожидаем ответ службы.",
    "not_checked": "Автозапуск ещё не проверял службу.",
    "ssd_waiting": "Ожидаем подключение зарегистрированного SSD.",
    "runtime_missing": "На SSD недоступна установленная среда Symphony.",
    "installation_pending": "Ожидаем завершения штатного обновления.",
    "queue_waiting": "OMS выключена. Очередь или история требуют проверки владельцем перед запуском.",
    "port_conflict": "Порт занят процессом, который автозапуск не может подтвердить.",
    "registration_conflict": "Регистрация службы отличается от ожидаемой.",
    "configuration_unavailable": "Не удалось подтвердить локальную конфигурацию службы.",
    "doctor_failed": "Штатная проверка OMS выявила проблему. Доска ожидает владельца.",
    "start_failed": "macOS отказала в запуске службы. Ожидаем проверки регистрации.",
}

# launchd can invoke the guard even when the external disk is absent. Only
# system tools run before the physical mount and its enrolled UUID agree.
MOUNT_GUARD = r'''
set -eu
volume=$1; expected=$2; python=$3; entry=$4; status=$5
shift 5
waiting() {
    umask 077
    tmp=$(/usr/bin/mktemp "${status}.XXXXXX")
    printf '{"version":1,"oms":"%s","connector":"%s"}\n' "$1" "$1" > "$tmp"
    /bin/mv -fh "$tmp" "$status"
    exit 0
}
[ -d "$volume" ] && [ ! -L "$volume" ] || waiting ssd_waiting
device=$(/usr/bin/stat -f %d "$volume") || waiting ssd_waiting
parent=$(/usr/bin/stat -f %d "$volume/..") || waiting ssd_waiting
[ "$device" != "$parent" ] || waiting ssd_waiting
actual=$(/usr/sbin/diskutil info -plist "$volume" 2>/dev/null | /usr/bin/plutil -extract VolumeUUID raw -o - - 2>/dev/null) || waiting ssd_waiting
[ "$actual" = "$expected" ] || waiting ssd_waiting
[ -x "$python" ] && [ -f "$entry" ] || waiting runtime_missing
exec "$python" -I -B "$entry" "$@"
'''


class StartupError(RuntimeError):
    pass


@dataclass(frozen=True)
class Layout:
    work: Path
    volume: Path
    home: Path

    @property
    def main(self):
        return self.work / "dev/oh-my-symphony"

    @property
    def clone(self):
        return self.work / "dev/oms-pipeline"

    @property
    def python(self):
        return self.main / ".venv/bin/python"

    @property
    def board(self):
        return self.work / "symphony-projects/oms"

    @property
    def workflow(self):
        return self.board / "WORKFLOW.md"

    @property
    def local(self):
        return self.home / "Library/Application Support/SymphonySSD/services"

    @property
    def status(self):
        return self.local / "chat-availability.json"

    @property
    def receipt(self):
        return self.local / "project-connect-process.json"

    def plist(self, label):
        parent = self.home / "Library/LaunchAgents" if label == LOGIN else self.local
        return parent / (label + ".plist")


def run(*argv, timeout=15):
    return subprocess.run(list(argv), capture_output=True, timeout=timeout, check=False)


def read_owned(path: Path, limit=2 * 1024 * 1024):
    if path.resolve(strict=True) != path:
        raise StartupError("unsafe_state_path")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise StartupError("unsafe_state_file")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise StartupError("state_too_large")
    return data


def write_atomic(path, payload, *, create=False):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".chat-startup-", delete=False) as stream:
        tmp = Path(stream.name)
        try:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            if create:
                os.link(tmp, path)
            else:
                tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)


def save(path, data):
    write_atomic(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode())


def volume_uuid(layout):
    volume, work = layout.volume, layout.work
    if (volume == Path("/") or volume.resolve() != volume or not volume.is_mount()
            or work.resolve() != work or not work.is_relative_to(volume)
            or work.stat().st_dev != volume.stat().st_dev):
        raise StartupError("ssd_waiting")
    p = run("/usr/sbin/diskutil", "info", "-plist", str(volume))
    info = plistlib.loads(p.stdout) if not p.returncode else {}
    uuid = info.get("VolumeUUID")
    if info.get("MountPoint") != str(volume) or not isinstance(uuid, str) or not UUID.fullmatch(uuid):
        raise StartupError("ssd_waiting")
    return uuid


def oms_registration(layout):
    path = layout.plist(OMS)
    spec = plistlib.loads(read_owned(layout.board / "launchd.plist"))
    registration = plistlib.loads(read_owned(path))
    start = layout.work / "host/start_service.py"
    read_owned(start)
    program = [str(layout.python), "-m", "symphony.cli", str(layout.workflow),
               "--host", "127.0.0.1", "--port", str(OMS_PORT)]
    log = str(layout.home / "Library/Logs/SymphonyLauncher/bootstrap.log")
    expected = {**spec, "ProgramArguments": [str(layout.python), "-I", "-B", str(start), OMS],
                "StandardOutPath": log, "StandardErrorPath": log}
    if (spec.get("Label") != OMS or spec.get("ProgramArguments") != program
            or spec.get("WorkingDirectory") != str(layout.board) or spec.get("RunAtLoad") is not True
            or spec.get("KeepAlive") is not False or registration != expected
            or set(spec) - {"Label", "ProgramArguments", "WorkingDirectory", "RunAtLoad", "KeepAlive",
                            "StandardOutPath", "StandardErrorPath", "EnvironmentVariables"}):
        raise StartupError("oms_registration_changed")
    return path


def registrations(layout, uuid):
    entry = layout.main / "ours/bin/oms-chat-startup"
    log = layout.home / "Library/Logs/SymphonyLauncher/chat-availability.log"
    result = {}
    for label, action in ((CONNECTOR, "connector"), (LOGIN, "tick")):
        arguments = ["/bin/sh", "-c", MOUNT_GUARD, "symphony-ssd", str(layout.volume), uuid, str(layout.python),
                     str(entry), str(layout.status), action, "--work", str(layout.work), "--volume", str(layout.volume),
                     "--volume-uuid", uuid]
        data = {"Label": label, "ProgramArguments": arguments, "RunAtLoad": True,
                "StandardOutPath": str(log), "StandardErrorPath": str(log),
                "EnvironmentVariables": {"PATH": SYSTEM_PATH, "HOME": str(layout.home), "LANG": "en_US.UTF-8",
                                         "PYTHONDONTWRITEBYTECODE": "1"}}
        if label == LOGIN:
            data["StartInterval"] = INTERVAL
        result[layout.plist(label)] = plistlib.dumps(data)
    return result


def prepare(layout):
    uuid = volume_uuid(layout)
    oms_registration(layout)
    applied_revision(layout)
    read_owned(layout.main / "ours/bin/oms-chat-startup")
    files = registrations(layout, uuid)
    for path, payload in files.items():
        if (path.exists() or path.is_symlink()) and read_owned(path) != payload:
            raise StartupError("different_registration")
    for folder in (layout.local, layout.plist(LOGIN).parent, layout.home / "Library/Logs/SymphonyLauncher"):
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if folder.resolve() != folder or folder.stat().st_uid != os.getuid() or folder.stat().st_mode & 0o022:
            raise StartupError("unsafe_registration_directory")
    for path, payload in files.items():
        if not path.exists():
            write_atomic(path, payload, create=True)
    return files


def git(repo, *args):
    p = run("/usr/bin/git", "--no-optional-locks", "--no-replace-objects", "-c", "core.fsmonitor=false",
            "-C", str(repo), *args)
    if p.returncode:
        raise StartupError("installation_pending")
    return p.stdout.decode().splitlines()


def applied_revision(layout):
    applied = json.loads(read_owned(layout.main / STATE_REL))["applied"]
    head = applied.get(str(layout.main))
    if (not isinstance(head, str) or not COMMIT.fullmatch(head) or applied.get(str(layout.clone)) != head
            or any(git(repo, "rev-parse", "--verify", "HEAD^{commit}") != [head] for repo in (layout.main, layout.clone))):
        raise StartupError("installation_pending")
    git(layout.main, "diff", "--quiet", "--no-ext-diff", "--no-textconv", head, "--", *RUNTIME)
    if git(layout.main, "ls-files", "--others", "--exclude-standard", "--", *RUNTIME):
        raise StartupError("installation_pending")
    return head


def connector_version(layout, head):
    objects = git(layout.main, "rev-parse", *(f"{head}:{path}" for path in RUNTIME))
    if len(objects) != len(RUNTIME) or any(not COMMIT.fullmatch(obj) for obj in objects):
        raise StartupError("installation_pending")
    return hashlib.sha256("\n".join(objects).encode()).hexdigest()


def _held_api_result(cfg, path, front, db):
    """Match a saved result to the queue hold for its most recent run."""
    identifier = path.stem
    if ((front.get("id") or front.get("identifier")) != identifier
            or any(front.get(key, identifier) != identifier for key in ("id", "identifier"))):
        return False
    kind = front.get("agent_kind")
    agent = front.get("agent")
    if kind is None and isinstance(agent, dict):
        kind = agent.get("kind")
    if not isinstance(kind, str):
        return False
    kind = kind.strip().lower()
    if not api_team.uses_board_queue(cfg, kind):
        return False
    assignment = api_team._read_assignment(cfg, identifier)
    if (not assignment or assignment.get("version") != 1 or assignment.get("status") != "result"
            or assignment.get("result_lane") != front["state"] or assignment.get("check_lane") != front["state"]
            or parse_iso_timestamp(assignment.get("finished_at")) is None):
        return False
    candidate = assignment.get("candidate")
    run_id = assignment.get("run_id")
    if (not isinstance(candidate, dict) or candidate.get("identifier") != identifier
            or candidate.get("status") != "awaiting_full_checks"
            or not isinstance(commit := candidate.get("commit"), str) or not COMMIT.fullmatch(commit)
            or not isinstance(candidate.get("checks"), dict) or candidate["checks"].get("static") != "passed"
            or not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id)):
        return False
    return db.execute(
        """SELECT 1 FROM runs JOIN issue_flags USING (issue_id)
           WHERE runs.run_id = ? AND runs.issue_id = ? AND runs.identifier = ? AND runs.agent_kind = ?
             AND runs.completed_at IS NOT NULL AND runs.status = 'api_team_finished'
             AND issue_flags.paused = 1 AND issue_flags.pause_reason = ?
             AND runs.rowid = (SELECT max(rowid) FROM runs WHERE issue_id = ?)""",
        (run_id, identifier, identifier, kind, api_team.FINISHED_HOLD, identifier),
    ).fetchone() is not None


def queue_is_safe(layout):
    try:
        read_owned(layout.workflow)
        cfg, error = WorkflowState(layout.workflow).reload()
        cards = layout.board / "kanban"
        if (error or cfg is None or cfg.tracker.kind != "file" or cfg.tracker.board_root != cards or cfg.code_repo != layout.clone
                or cfg.agent.auto_merge_on_done or cards.resolve(strict=True) != cards
                or SAFE_LANES & {normalize_state(s) for s in cfg.tracker.active_states}):
            return False
        terminal = SAFE_LANES & {normalize_state(s) for s in cfg.tracker.terminal_states}
        verifying = []
        for path in cards.iterdir():
            if path.suffix != ".md" or path.name.startswith(".tmp-"):
                continue
            lines = read_owned(path).decode().splitlines()
            delimiters = [i for i, line in enumerate(lines) if line.strip() == "---"]
            if len(delimiters) < 2 or delimiters[0] != 0:
                return False
            front = yaml.safe_load("\n".join(lines[1:delimiters[1]]))
            if not isinstance(front, dict) or not isinstance(front.get("state"), str):
                return False
            state = normalize_state(front["state"])
            if state in {"verify", "проверка"}:
                verifying.append((path, front))
            elif state not in terminal:
                return False
        db = layout.board / ".symphony/state.db"
        if cfg.tracker.run_history_path != db or db.resolve(strict=True) != db or not db.is_file():
            return False
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
            # Check the saved holds and run history in the same read snapshot.
            conn.execute("BEGIN")
            return (conn.execute("SELECT 1 FROM runs WHERE completed_at IS NULL LIMIT 1").fetchone() is None
                    and conn.execute("SELECT 1 FROM issue_flags WHERE retry_attempt IS NOT NULL LIMIT 1").fetchone() is None
                    and all(_held_api_result(cfg, path, front, conn) for path, front in verifying))
    except (*UNAVAILABLE, sqlite3.Error, yaml.YAMLError, OverflowError):
        return False


def job(label):
    p = run("/bin/launchctl", "print", target(label))
    if p.returncode:
        return None
    text = p.stdout.decode()
    path = re.search(r"^\s*path = (.+)$", text, re.M)
    pid = re.search(r"^\s*pid = ([0-9]+)$", text, re.M)
    return (Path(path[1]) if path else None, int(pid[1]) if pid else None)


def target(label):
    return f"gui/{os.getuid()}/{label}"


def listeners(port):
    p = run("/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpn")
    if p.returncode not in (0, 1):
        raise StartupError("port_check_failed")
    pid = None
    result = set()
    for line in p.stdout.decode().splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            result.add((pid, line[1:]))
    if not result:
        with socket.socket() as sock:
            sock.settimeout(1)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                result.add((0, "unknown"))
    return result


def start_job(label, path, loaded, *, replace=False):
    if loaded:
        if job(label) != loaded:
            return "starting"
        args = ["kickstart", "-k", target(label)] if replace else ["kickstart", target(label)]
    else:
        args = ["bootstrap", f"gui/{os.getuid()}", str(path)]
    return "starting" if not run("/bin/launchctl", *args).returncode else "start_failed"


def ensure_oms(layout):
    path = oms_registration(layout)
    loaded = job(OMS)
    if loaded and loaded[0] != path:
        return "registration_conflict"
    bound = listeners(OMS_PORT)
    if bound:
        if (loaded and loaded[1] and bound == {(loaded[1], f"127.0.0.1:{OMS_PORT}")}
                and service.is_symphony_workflow_reachable("127.0.0.1", OMS_PORT, layout.workflow)):
            return "ready"
        return "port_conflict"
    if loaded and loaded[1]:
        return "starting"
    if not queue_is_safe(layout):
        return "queue_waiting"
    if run(str(layout.python), "-I", "-B", "-m", "symphony.cli", "doctor", str(layout.workflow), timeout=90).returncode:
        return "doctor_failed"
    if not queue_is_safe(layout):
        return "queue_waiting"
    if listeners(OMS_PORT):
        return "port_conflict"
    return start_job(OMS, path, loaded)


def ensure_connector(layout, uuid, head):
    path = layout.plist(CONNECTOR)
    if read_owned(path) != registrations(layout, uuid)[path]:
        return "registration_conflict"
    loaded = job(CONNECTOR)
    if loaded and loaded[0] != path:
        return "registration_conflict"
    bound = listeners(CONNECTOR_PORT)
    if bound:
        if not loaded or not loaded[1] or bound != {(loaded[1], f"127.0.0.1:{CONNECTOR_PORT}")}:
            return "port_conflict"
        process = json.loads(read_owned(layout.receipt))
        if process.get("pid") != loaded[1] or process.get("work") != str(layout.work):
            return "registration_conflict"
        if process.get("version") != connector_version(layout, head):
            return start_job(CONNECTOR, path, loaded, replace=True)
        return "ready"
    if loaded and loaded[1]:
        return "starting"
    return start_job(CONNECTOR, path, loaded)


def status(layout):
    try:
        value = json.loads(read_owned(layout.status))
        if (value.get("version") == 1 and value.get("board", str(layout.board)) == str(layout.board)
                and all(value.get(name) in REASONS for name in ("oms", "connector"))):
            return value
    except UNAVAILABLE:
        pass
    return {"version": 1, "oms": "not_checked", "connector": "not_checked"}


def tick(layout, uuid):
    result = {"version": 1, "board": str(layout.board)}
    lock = (layout.main / STATE_REL).with_suffix(".json.lock")
    if lock.parent.resolve(strict=True) != lock.parent:
        raise StartupError("unsafe_state_path")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "r+b") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise StartupError("unsafe_state_file")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result.update(oms="installation_pending", connector="installation_pending")
        else:
            try:
                head = applied_revision(layout)
            except UNAVAILABLE:
                result.update(oms="installation_pending", connector="installation_pending")
            else:
                for name, check in (("connector", lambda: ensure_connector(layout, uuid, head)),
                                    ("oms", lambda: ensure_oms(layout))):
                    try:
                        result[name] = check()
                    except UNAVAILABLE:
                        result[name] = "configuration_unavailable"
    previous = status(layout)
    result["checked_at"] = int(time.time())
    save(layout.status, result)
    if any(previous.get(key) != result[key] for key in ("oms", "connector")):
        print(json.dumps(result, ensure_ascii=False))
    return result


def serve_connector(layout):
    head = applied_revision(layout)
    log = layout.work / "logs/project-connect.log"
    if log.parent.resolve(strict=True) != log.parent:
        raise StartupError("unsafe_log_directory")
    fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "ab") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise StartupError("unsafe_log_file")
        os.dup2(fd, 1)
        os.dup2(fd, 2)
    save(layout.receipt, {"pid": os.getpid(), "work": str(layout.work), "version": connector_version(layout, head)})
    os.chdir(layout.work)
    command = [str(layout.python), "-I", "-B", str(layout.main / "ours/bin/project-connect"), "serve",
               "--root", str(layout.board.parent), "--port", str(CONNECTOR_PORT)]
    os.execv(command[0], command)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "tick", "connector", "status"))
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--volume", type=Path, required=True)
    parser.add_argument("--volume-uuid")
    args = parser.parse_args(argv)
    layout = Layout(args.work.absolute(), args.volume.absolute(), Path.home())
    os.umask(0o077)
    try:
        if args.action == "status":
            value = status(layout)
            messages = {name: REASONS[value[name]] for name in ("oms", "connector")}
            print(json.dumps({**value, "messages": messages}, ensure_ascii=False, indent=2))
        elif args.action == "prepare":
            for path in prepare(layout):
                print(path)
        else:
            if volume_uuid(layout) != args.volume_uuid:
                raise StartupError("ssd_waiting")
            if args.action == "tick":
                tick(layout, args.volume_uuid)
            else:
                serve_connector(layout)
    except UNAVAILABLE as exc:
        reason = str(exc) if isinstance(exc, StartupError) else "configuration_unavailable"
        print(json.dumps({"error": reason}, ensure_ascii=False))
        return 1
    return 0
