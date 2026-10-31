"""Board script delivery against real temporary Git repositories and files."""

from __future__ import annotations

import base64
import fcntl
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

OURS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OURS / "hooks"))
import project_update as pu  # noqa: E402

CLI = OURS / "bin" / "project-update"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def put(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def tree(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mode & 0o777)
            for p in root.rglob("*") if p.is_file() and not p.is_symlink()}


class World:
    def __init__(self, tmp):
        self.source = tmp / "source"
        self.code = tmp / "product-code"
        self.root = tmp / "boards"
        self.board = self.root / "update-test"
        self.kit = self.source / "ours/projects/demo"
        self.source.mkdir()
        self.code.mkdir()
        git(self.source, "init", "-q", "-b", "our-main")
        git(self.source, "config", "user.email", "test@example.invalid")
        git(self.source, "config", "user.name", "test")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        put(self.board / "project.toml", f'''[project]
name = "update-test"
repo = "{self.code}"
base_branch = "main"
id_prefix = "UPD"
port = {self.port}
[tools]
python = "{sys.executable}"
symphony = "/test/bin/symphony"
claude = "/test/bin/claude"
''')
        put(self.board / "test.sh", "old runner\n", 0o755)
        put(self.board / "bin/custom", "local helper\n", 0o700)
        put(self.board / "kanban/UPD-1.md", "keep this card\n")
        put(self.board / ".ours-state/baseline.json", '{"passed": 17}\n')
        self.release("new runner\n", {"bin/helper.py": "print('helper')\n"})
        self.project = pu.read_board(self.root, "update-test")[1]

    def release(self, runner, extras=None, *, machine_lock=False, legacy=None):
        extras = extras or {}
        paths = {"test.sh": runner, **extras}
        # Git records deletions too when a release retires a managed script.
        for old in self.kit.rglob("*") if self.kit.exists() else []:
            if old.is_file() and str(old.relative_to(self.kit)) not in paths:
                old.unlink()
        for name, text in paths.items():
            put(self.kit / name, text)
        manifest = {"schema": 1, "name": "demo", "machine_test_lock": machine_lock,
                    "legacy_directory_lock_sha256": legacy or [],
                    "files": {name: {"mode": "0755" if name == "test.sh" else "0644",
                                     "adopt_sha256": [pu.digest(b"old runner\n")] if name == "test.sh" else []}
                              for name in paths}}
        put(self.kit / "bundle.json", json.dumps(manifest))
        git(self.source, "add", "-A")
        git(self.source, "commit", "-q", "-m", "release")

    def bundle(self):
        return pu.load_bundle(self.source, "demo")

    def install(self, expected=None):
        return pu.install(self.board, self.project, self.bundle(), expected)

    def cli(self, *args):
        return subprocess.run([sys.executable, str(CLI), "update-test", "--root", str(self.root),
                               "--source", str(self.source), "--kit", "demo", *args],
                              capture_output=True, text=True, timeout=30)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


@pytest.fixture
def stopped(monkeypatch):
    # Filesystem transaction tests isolate process discovery, which has separate
    # real-port, real-process and CLI coverage below.
    monkeypatch.setattr(pu, "ensure_stopped", lambda *args: None)


def test_default_preview_has_no_filesystem_writes(world):
    before = tree(world.root)
    result = world.cli("--diff")
    assert result.returncode == 0, result.stderr
    assert "Только просмотр" in result.stdout
    assert "-old runner" in result.stdout and "+new runner" in result.stdout
    assert tree(world.root) == before
    assert not (world.board / pu.STATE).exists()


def test_cli_installs_and_restores_original_files(world):
    before = tree(world.board)
    result = world.cli("--apply", "--expect", world.bundle().version)
    assert result.returncode == 0, (result.stdout, result.stderr)
    current = pu.receipt(world.board)
    assert current["source_commit"] == git(world.source, "rev-parse", "HEAD")
    assert (world.board / "test.sh").read_text() == "new runner\n"
    assert (world.board / "test.sh").stat().st_mode & 0o777 == 0o755
    assert (world.board / "bin/helper.py").exists()
    for name in ["project.toml", "bin/custom", "kanban/UPD-1.md", ".ours-state/baseline.json"]:
        assert tree(world.board)[name] == before[name]
    identifier = current["transaction"]
    unchanged = tree(world.board)
    assert world.cli("--rollback", identifier, "--dry-run").returncode == 0
    assert tree(world.board) == unchanged
    rollback = world.cli("--rollback", identifier)
    assert rollback.returncode == 0, rollback.stderr
    assert not (world.board / "bin/helper.py").exists()
    assert pu.receipt(world.board) is None
    for name, content in before.items():
        assert tree(world.board)[name] == content


def test_second_apply_is_noop_and_content_version_ignores_unrelated_commits(world, stopped):
    identifier = world.install()
    before = tree(world.board)
    old_version = world.bundle().version
    put(world.source / "README.md", "unrelated docs\n")
    git(world.source, "add", "README.md")
    git(world.source, "commit", "-qm", "docs")
    assert world.bundle().version == old_version
    assert world.install() is None
    assert tree(world.board) == before
    assert pu.receipt(world.board)["transaction"] == identifier


@pytest.mark.parametrize("change", ["content", "mode", "missing"])
def test_local_changes_are_not_overwritten_or_rolled_back(world, stopped, change):
    identifier = world.install()
    path = world.board / "test.sh"
    if change == "content":
        path.write_text("operator change\n")
    elif change == "mode":
        path.chmod(0o600)
    else:
        path.unlink()
    before = tree(world.board)
    with pytest.raises(pu.UpdateError, match="локально"):
        world.install()
    with pytest.raises(pu.UpdateError, match="локальные"):
        pu.restore(world.board, identifier)
    assert tree(world.board) == before


def test_first_install_does_not_adopt_unknown_scripts(world, stopped):
    (world.board / "test.sh").write_text("unknown customization\n")
    before = tree(world.board)
    result = world.cli()
    assert result.returncode == 1
    assert "неизвестная локальная версия" in result.stderr
    assert tree(world.board) == before


def test_wrong_expected_version_refuses_before_writes(world, stopped):
    before = tree(world.board)
    with pytest.raises(pu.UpdateError, match="--expect"):
        world.install("0" * 64)
    assert tree(world.board) == before


def test_source_is_pinned_to_git_not_uncommitted_working_files(world):
    old = world.bundle()
    put(world.kit / "test.sh", "not reviewed\n")
    put(world.kit / "bin/unknown.py", "untracked\n")
    current = world.bundle()
    assert current.version == old.version
    assert base64.b64decode(current.files["test.sh"]["data"]) == b"new runner\n"


def test_new_release_and_rollback_restore_previous_receipt_and_retired_file(world, stopped):
    first = world.install()
    old = pu.receipt(world.board)
    world.release("runner v2\n", {"bin/another.py": "print(2)\n"})
    second = world.install()
    assert second != first
    assert not (world.board / "bin/helper.py").exists()
    assert (world.board / "bin/custom").exists()
    with pytest.raises(pu.UpdateError, match="последнюю"):
        pu.restore(world.board, first)
    pu.restore(world.board, second)
    assert pu.receipt(world.board) == old
    assert (world.board / "bin/helper.py").exists()
    assert not (world.board / "bin/another.py").exists()


def test_write_failure_automatically_restores_all_files(world, stopped, monkeypatch):
    original = pu.atomic_write
    failed = False

    def fail_once(path, data, mode=0o600):
        nonlocal failed
        if path == world.board / "bin/helper.py" and not failed:
            failed = True
            raise OSError("injected write failure")
        return original(path, data, mode)

    monkeypatch.setattr(pu, "atomic_write", fail_once)
    before = tree(world.board)
    with pytest.raises(pu.UpdateError, match="прежние файлы восстановлены"):
        world.install()
    assert (world.board / "test.sh").read_text() == "old runner\n"
    assert not (world.board / "bin/helper.py").exists()
    assert pu.pending(world.board) is None
    assert pu.receipt(world.board) is None
    for name, content in before.items():
        assert tree(world.board)[name] == content


def test_interrupted_apply_can_be_recovered_without_source_repository(world, stopped, monkeypatch):
    original = pu.atomic_write
    armed = True

    def fail_until_recovery(path, data, mode=0o600):
        if armed and path == world.board / "bin/helper.py":
            raise OSError("persistent write failure")
        return original(path, data, mode)

    # Leave an incomplete journal exactly as a killed updater would do.
    monkeypatch.setattr(pu, "atomic_write", fail_until_recovery)
    monkeypatch.setattr(pu, "restore", lambda *args: (_ for _ in ()).throw(pu.UpdateError("interrupted")))
    with pytest.raises(pu.UpdateError, match="нужен --rollback"):
        world.install()
    identifier = pu.pending(world.board)
    assert identifier
    with pytest.raises(pu.UpdateError, match="незавершённое"):
        world.install()
    armed = False
    world.source.rename(world.source.with_name("source-unavailable"))
    # A new CLI process does not inherit the injected fault or patched restore.
    result = world.cli("--rollback", identifier)
    assert result.returncode == 0, result.stderr
    assert (world.board / "test.sh").read_text() == "old runner\n"
    assert pu.pending(world.board) is None


def test_unknown_edit_during_backup_is_preserved(world, stopped, monkeypatch):
    original = pu.atomic_write

    def edit_after_backup(path, data, mode=0o600):
        original(path, data, mode)
        if path.name == "pending.json":
            (world.board / "test.sh").write_text("concurrent operator edit\n")

    monkeypatch.setattr(pu, "atomic_write", edit_after_backup)
    with pytest.raises(pu.UpdateError, match="нужен --rollback"):
        world.install()
    assert (world.board / "test.sh").read_text() == "concurrent operator edit\n"
    assert pu.pending(world.board)


@pytest.mark.parametrize("path", ["../outside", "/tmp/outside", "project.toml", "kanban/x.md", "bin/../../outside", ".ours-state/current.json"])
def test_manifest_cannot_manage_config_cards_or_escape(world, path):
    manifest = json.loads((world.kit / "bundle.json").read_text())
    manifest["files"] = {path: {"mode": "0644"}}
    put(world.kit / "bundle.json", json.dumps(manifest))
    git(world.source, "add", "-A")
    git(world.source, "commit", "-qm", "bad manifest")
    with pytest.raises(pu.UpdateError, match="недопустимый"):
        world.bundle()


@pytest.mark.parametrize("target", ["test.sh", "bin", ".ours-state", ".ours-state/script-updates"])
def test_symlink_targets_refuse_without_touching_outside(world, stopped, tmp_path, target):
    import shutil

    path = world.board / target
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
    outside = put(tmp_path / "outside" / "data", "untouched\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(outside if target == "test.sh" else outside.parent)
    with pytest.raises(pu.UpdateError, match="ссылка"):
        world.install()
    assert outside.read_text() == "untouched\n"


def test_corrupt_backup_refuses_before_changing_installed_files(world, stopped):
    identifier = world.install()
    path, record = pu.read_transaction(world.board, identifier)
    record["before"]["test.sh"]["data"] = base64.b64encode(b"corrupted").decode()
    path.write_text(json.dumps(record))
    before = tree(world.board)
    with pytest.raises(pu.UpdateError, match="контрольная сумма"):
        pu.restore(world.board, identifier)
    assert tree(world.board) == before


def test_busy_port_refuses_before_creating_state(world):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", world.port))
        sock.listen()
        with pytest.raises(pu.UpdateError, match="порт доски"):
            world.install()
    assert not (world.board / pu.STATE).exists()


def test_running_manual_script_is_detected(world):
    sleeper = put(world.board / "test.sh", "#!/bin/bash\nsleep 30\n")
    proc = subprocess.Popen(["/bin/bash", str(sleeper)], start_new_session=True)
    try:
        with pytest.raises(pu.UpdateError, match="процесс"):
            pu.ensure_stopped(world.board, world.project, {"test.sh"})
    finally:
        import signal
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


def test_concurrent_updater_is_refused(world, stopped):
    with pu.mutation_lock(world.board, world.project, {"test.sh"}, False):
        with pytest.raises(pu.UpdateError, match="другая команда"):
            world.install()
    assert (world.board / "test.sh").read_text() == "old runner\n"


def test_busy_machine_lock_and_legacy_directory_are_not_removed(world, stopped):
    world.release("runner with flock\n", machine_lock=True)
    path = world.root / ".locks/test.lock"
    path.parent.mkdir()
    path.mkdir()
    with pytest.raises(pu.UpdateError, match="старый каталог"):
        world.install()
    assert path.is_dir()
    path.rmdir()
    with path.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(pu.UpdateError, match="test.lock занят"):
            world.install()
    assert path.is_file()


def test_rollback_does_not_delete_persistent_machine_lock_for_legacy_runner(world, stopped):
    world.release("runner with flock\n", machine_lock=True, legacy=[pu.digest(b"old runner\n")])
    identifier = world.install()
    path = put(world.root / ".locks/test.lock", "")
    before = tree(world.board)
    with pytest.raises(pu.UpdateError, match="отдельной миграции"):
        pu.restore(world.board, identifier)
    assert path.is_file()
    assert tree(world.board) == before


def test_killed_updater_restores_partial_install_in_new_process(world, tmp_path):
    marker = tmp_path / "written"
    script = put(tmp_path / "interrupted.py", f'''import sys,time
from pathlib import Path
sys.path.insert(0, {str(OURS / "hooks")!r})
import project_update as pu
board,project=pu.read_board(Path({str(world.root)!r}), "update-test")
original=pu.atomic_write
def pause_after_one_file(path,data,mode=0o600):
    original(path,data,mode)
    if path == board / "bin/helper.py":
        Path({str(marker)!r}).write_text("ready")
        time.sleep(60)
pu.atomic_write=pause_after_one_file
pu.install(board,project,pu.load_bundle(Path({str(world.source)!r}),"demo"),None)
''')
    proc = subprocess.Popen([sys.executable, "-B", str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not marker.exists():
            assert proc.poll() is None, proc.communicate()
            assert time.monotonic() < deadline, "updater did not reach the injection point"
            time.sleep(0.02)
        identifier = pu.pending(world.board)
        assert identifier
        proc.kill()
        proc.wait(timeout=5)
        assert (world.board / "bin/helper.py").exists()
        assert (world.board / "test.sh").read_text() == "old runner\n"
        result = world.cli("--rollback", identifier)
        assert result.returncode == 0, result.stderr
        assert not (world.board / "bin/helper.py").exists()
        assert (world.board / "test.sh").read_text() == "old runner\n"
        assert pu.pending(world.board) is None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=5)


def test_cannot_replace_pending_update_with_an_older_rollback(world, stopped):
    identifier = world.install()
    unfinished = "20261031T120000Z-123456789abc"
    pu.atomic_write(world.board / pu.STATE / "pending.json", pu.json_bytes({"transaction": unfinished}))
    before = tree(world.board)
    with pytest.raises(pu.UpdateError, match="сначала восстанови"):
        pu.restore(world.board, identifier)
    assert tree(world.board) == before


def test_tracked_source_symlink_is_refused(world):
    path = world.kit / "test.sh"
    path.unlink()
    path.symlink_to("/outside/script")
    git(world.source, "add", "-A")
    git(world.source, "commit", "-qm", "symlink")
    with pytest.raises(pu.UpdateError, match="обычный файл"):
        world.bundle()


def test_loaded_launchd_service_is_refused_even_with_free_port(world, monkeypatch):
    original = subprocess.run

    def loaded(args, **kwargs):
        if args[:2] == ["/bin/launchctl", "print"]:
            return subprocess.CompletedProcess(args, 0, "state = waiting", "")
        return original(args, **kwargs)

    monkeypatch.setattr(pu.sys, "platform", "darwin")
    monkeypatch.setattr(pu.subprocess, "run", loaded)
    with pytest.raises(pu.UpdateError, match="служба загружена"):
        world.install()
    assert not (world.board / pu.STATE).exists()
