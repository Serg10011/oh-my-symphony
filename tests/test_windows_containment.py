"""Native Windows ownership: exact jobs outlive broken PID ancestry."""

import ctypes
import asyncio
import json
import os
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import Mock
import subprocess
import time
import shlex

import pytest

from symphony.orchestrator.core import _reclaim_kill_confirm

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows job objects")


async def _published(pid):
    assert pid > 0


async def _spawn(argv, cwd, callback=None, *, managed=False, publish=_published, pipe=True):
    from symphony._windows_backend import spawn_backend

    return await spawn_backend(
        argv, cwd=str(cwd), env={**os.environ, "OWNED_TEST_UNICODE": "한글 ✓"}, limit=65536,
        pipe_stdin=pipe, on_identified=callback, on_started=None,
        publish_started=publish, require_durable=managed,
    )


def _wait(predicate, timeout=10):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= end:
            raise AssertionError("owned process condition timed out")
        time.sleep(0.02)


@pytest.mark.parametrize("status", [0, 23])
async def test_native_stdio_unicode_cwd_env_and_exact_exit(tmp_path, status):
    from symphony._windows_job import job_exists

    cwd = tmp_path / "한글 space"
    cwd.mkdir()
    code = (
        "import sys,os,json; data=sys.stdin.buffer.read(); "
        "sys.stdout.buffer.write((json.dumps([os.getcwd(),os.environ['OWNED_TEST_UNICODE'],data.decode()],"
        "ensure_ascii=False)+'\\n').encode('utf-8')); "
        "sys.stderr.buffer.write('별도 오류\\n'.encode()); sys.exit(" + str(status) + ")"
    )
    proc, identity = await _spawn([sys.executable, "-c", code], cwd)
    stdout, stderr = await asyncio.wait_for(proc.communicate("입력\n".encode()), 10)
    assert proc.returncode == status
    assert json.loads(stdout) == [str(cwd), "한글 ✓", "입력\n"]
    assert stderr == "별도 오류\n".encode()
    _wait(lambda: job_exists(identity) is False)


async def test_full_windows_dword_exit_status_is_preserved(tmp_path):
    code = (
        "import ctypes; api=ctypes.WinDLL('kernel32'); "
        "api.ExitProcess.argtypes=[ctypes.c_uint32]; api.ExitProcess.restype=None; "
        "api.ExitProcess(0xC0000001)"
    )
    proc, _ = await _spawn([sys.executable, "-c", code], tmp_path)
    await asyncio.wait_for(proc.communicate(), 5)
    assert proc.returncode == 0xC0000001


@pytest.mark.parametrize("ack,managed,allowed", [(None, False, True), (True, True, True), (None, True, False), (False, True, False)])
async def test_no_backend_before_persistence_and_pm_publication(tmp_path, ack, managed, allowed):
    sentinel = tmp_path / "backend-executed"
    seen = []

    def identified(pid, token):
        assert not sentinel.exists()
        seen.append((pid, token))
        return ack

    async def published(pid):
        assert seen and seen[0][0] == pid
        assert not sentinel.exists()

    code = "from pathlib import Path; Path(" + repr(str(sentinel)) + ").write_text('ran')"
    if allowed:
        proc, _ = await _spawn([sys.executable, "-c", code], tmp_path, identified, managed=managed, publish=published)
        await asyncio.wait_for(proc.communicate(), 10)
        assert proc.returncode == 0 and sentinel.read_text() == "ran"
    else:
        with pytest.raises(RuntimeError, match="durably recorded"):
            await _spawn([sys.executable, "-c", code], tmp_path, identified, managed=managed, publish=published)
        assert not sentinel.exists()


async def test_pm_failure_and_cancellation_do_not_send_go(tmp_path):
    sentinel = tmp_path / "not-started"
    command = [sys.executable, "-c", "from pathlib import Path; Path(" + repr(str(sentinel)) + ").touch()"]

    async def rejected(pid):
        raise ValueError("owned PM publication denied")

    with pytest.raises(ValueError, match="publication denied"):
        await _spawn(command, tmp_path, lambda *_: True, managed=True, publish=rejected)
    assert not sentinel.exists()
    publishing = asyncio.Event()

    async def parked(pid):
        publishing.set()
        await asyncio.Future()

    task = asyncio.create_task(_spawn(command, tmp_path, lambda *_: True, managed=True, publish=parked))
    await asyncio.wait_for(publishing.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not sentinel.exists()


@pytest.mark.parametrize("stage", ["ready", "persisted", "go"])
def test_kill_only_owner_cleans_job_and_disconnected_grandchild(tmp_path, stage):
    from symphony._windows_job import _api, job_exists, terminate_job

    grandchild = tmp_path / "grandchild.json"
    receipt = tmp_path / "receipt.json"
    barrier = tmp_path / "owner-stage"
    grand_code = f"import os,time,json; open({str(grandchild)!r},'w').write(json.dumps({{'pid':os.getpid()}})); time.sleep(3600)"
    child_code = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{grand_code!r}])"
    root_code = f"import subprocess,sys,time; subprocess.run([sys.executable,'-c',{child_code!r}],check=True); time.sleep(3600)"
    owner_script = tmp_path / "owner.py"
    owner_script.write_text(f"""
import asyncio,os,json,sys,time
from pathlib import Path
from symphony._windows_backend import spawn_backend
async def main():
    def identified(pid,token):
        Path({str(receipt)!r}).write_text(json.dumps({{'pid':pid,'token':token}}))
        if {stage!r} == 'ready':
            Path({str(barrier)!r}).touch()
            time.sleep(3600)
        return True
    async def published(pid):
        if {stage!r} == 'persisted':
            Path({str(barrier)!r}).touch()
            await asyncio.Future()
    await spawn_backend([sys.executable,'-c',{root_code!r}],cwd={str(tmp_path)!r},env=os.environ.copy(),limit=65536,pipe_stdin=True,on_identified=identified,on_started=None,publish_started=published,require_durable=True)
    Path({str(barrier)!r}).touch()
    await asyncio.Future()
asyncio.run(main())
""", encoding="utf-8")
    owner = subprocess.Popen([sys.executable, str(owner_script)], stderr=subprocess.PIPE)
    handle = None
    identity = None
    try:
        _wait(lambda: barrier.exists() or owner.poll() is not None)
        assert owner.poll() is None, owner.stderr.read().decode() if owner.stderr else ""
        identity = json.loads(receipt.read_text())["token"]
        assert job_exists(identity) is True
        if stage == "go":
            _wait(grandchild.exists)
            pid = json.loads(grandchild.read_text())["pid"]
            handle = _api().OpenProcess(0x100000 | 0x1000, False, pid)
            assert handle and _api().WaitForSingleObject(handle, 0) == 258
        else:
            assert not grandchild.exists()
        owner.kill()  # Exact Popen handle, owner only; intentionally no taskkill /T.
        owner.wait(timeout=5)
        _wait(lambda: job_exists(identity) is False)
        if handle:
            assert _api().WaitForSingleObject(handle, 2000) == 0
        else:
            assert not grandchild.exists()
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)
        if identity:
            terminate_job(identity)
        if handle:
            _api().CloseHandle(handle)


async def test_exact_job_cleanup_preserves_concurrent_job_and_unrelated_process(tmp_path):
    from symphony._shell import terminate_process_tree
    from symphony._windows_job import job_exists, session_id

    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
    first, first_token = await _spawn([sys.executable, "-c", "import time; time.sleep(3600)"], tmp_path)
    second, second_token = await _spawn([sys.executable, "-c", "import time; time.sleep(3600)"], tmp_path)
    try:
        assert first_token != second_token
        assert job_exists(first_token.replace(f":{session_id()}:", f":{session_id()+1}:")) is None
        assert job_exists("winjob-v2:malformed") is None
        record = SimpleNamespace(backend_agent_pid=unrelated.pid, backend_process_identity=first_token, issue_id="owned", identifier="OWNED-2")
        assert await asyncio.to_thread(_reclaim_kill_confirm, record) == "killed"
        await asyncio.wait_for(first.communicate(), 5)
        assert job_exists(first_token) is False
        assert job_exists(second_token) is True and second.returncode is None
        assert unrelated.poll() is None
    finally:
        await terminate_process_tree(first, identity=first_token)
        await terminate_process_tree(second, identity=second_token)
        unrelated.terminate()
        unrelated.wait(timeout=5)


@pytest.mark.parametrize("frame", [b"", b"BAD-CONTROL\n", b"SYMPHONY-GO"])
def test_invalid_or_eof_go_never_executes_backend(tmp_path, frame):
    from symphony._windows_backend import _ready
    from symphony._windows_job import new_job_identity, owner_creation, job_exists

    token = new_job_identity()
    sentinel = tmp_path / "never-executed"
    command = [sys.executable, "-m", "symphony._windows_backend", token, str(os.getpid()), owner_creation(), "null", sys.executable, "-c", f"open({str(sentinel)!r},'w').write('bad')"]
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    assert proc.stdout.read(len(_ready(token))) == _ready(token)
    proc.communicate(frame, timeout=5)
    assert proc.returncode != 0 and not sentinel.exists()
    _wait(lambda: job_exists(token) is False)


def test_replacement_owner_identity_fails_before_ready_and_backend(tmp_path):
    from symphony._windows_job import new_job_identity

    sentinel = tmp_path / "never-executed"
    proc = subprocess.run(
        [sys.executable, "-m", "symphony._windows_backend", new_job_identity(), str(os.getpid()), "1", "null", sys.executable, "-c", f"open({str(sentinel)!r},'w').write('bad')"],
        input=b"SYMPHONY-GO-v1\n", capture_output=True, timeout=5,
    )
    assert proc.returncode != 0 and not proc.stdout and not sentinel.exists()


async def test_normal_root_exit_kills_grandchild_and_preserves_status(tmp_path):
    from symphony._windows_job import _api, job_exists

    pid_file, release = tmp_path / "grandchild", tmp_path / "release"
    grand_code = f"import os,time; open({str(pid_file)!r},'w').write(str(os.getpid())); time.sleep(3600)"
    child_code = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{grand_code!r}])"
    root_code = f"import subprocess,sys,time,os; subprocess.run([sys.executable,'-c',{child_code!r}],check=True)\nwhile not os.path.exists({str(release)!r}): time.sleep(.01)\nsys.exit(23)"
    proc, token = await _spawn([sys.executable, "-c", root_code], tmp_path)
    handle = None
    try:
        _wait(pid_file.exists)
        handle = _api().OpenProcess(0x100000 | 0x1000, False, int(pid_file.read_text()))
        assert handle and _api().WaitForSingleObject(handle, 0) == 258
        release.touch()
        await asyncio.wait_for(proc.communicate(), 5)
        assert proc.returncode == 23
        _wait(lambda: job_exists(token) is False)
        assert _api().WaitForSingleObject(handle, 2000) == 0
    finally:
        from symphony._shell import terminate_process_tree

        await terminate_process_tree(proc, identity=token)
        if handle:
            _api().CloseHandle(handle)


@pytest.mark.parametrize("mode", ["good", "false", "none", "error", "missing-registry", "missing-entry"])
async def test_managed_backend_requires_exact_actual_lease_persistence(tmp_path, monkeypatch, mode):
    from symphony._windows_job import is_job_identity
    from tests.test_orchestrator_dispatch import _orch, _make_config, _issue, _install_running_entry

    cfg = _make_config(workflow_path=tmp_path / "WORKFLOW.md")
    orch = _orch()
    monkeypatch.setattr(orch._workflow_state, "current", lambda: cfg)
    orch._ensure_run_registry(cfg)
    registry = orch._run_registry
    assert registry is not None
    issue = _issue("OWNED-LEASE")
    entry = _install_running_entry(orch, issue)
    entry.workspace_path = tmp_path
    entry.run_id = registry.acquire_run(issue, workspace_path=tmp_path, attempt=None, attempt_kind="test", agent_kind="codex")
    assert entry.run_id
    sentinel = tmp_path / "backend-executed"

    def identified(pid, token):
        assert is_job_identity(token) and not sentinel.exists()
        result = orch._sync_backend_agent_pid(issue.id, pid, backend_process_identity=token, identity_captured=True)
        if mode == "good":
            record = registry.get_run(entry.run_id)
            assert record.backend_agent_pid == pid and record.backend_process_identity == token
        return result

    if mode in {"false", "none"}:
        monkeypatch.setattr(registry, "heartbeat", lambda **_: False if mode == "false" else None)
    elif mode == "error":
        def unavailable(**_):
            raise OSError("owned registry failure")
        monkeypatch.setattr(registry, "heartbeat", unavailable)
    elif mode == "missing-registry":
        orch._run_registry = None
    elif mode == "missing-entry":
        orch._running.pop(issue.id)
    command = [sys.executable, "-c", f"open({str(sentinel)!r},'w').write('ran')"]
    try:
        if mode == "good":
            proc, token = await _spawn(command, tmp_path, identified, managed=True)
            await asyncio.wait_for(proc.communicate(), 5)
            assert sentinel.read_text() == "ran"
            orch._run_registry = None
            assert orch._agent_pid_identity(entry) == token
        else:
            with pytest.raises(RuntimeError, match="durably recorded"):
                await _spawn(command, tmp_path, identified, managed=True)
            assert not sentinel.exists()
    finally:
        registry.close()


@pytest.mark.timing
def test_no_go_times_out_without_starting_backend(tmp_path):
    from symphony._windows_backend import _ready
    from symphony._windows_job import new_job_identity, owner_creation

    token = new_job_identity()
    sentinel = tmp_path / "never-executed"
    proc = subprocess.Popen(
        [sys.executable, "-m", "symphony._windows_backend", token, str(os.getpid()), owner_creation(), "null", sys.executable, "-c", f"open({str(sentinel)!r},'w').write('bad')"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        assert proc.stdout is not None and proc.stdout.read(len(_ready(token))) == _ready(token)
        proc.wait(timeout=15)
        assert proc.returncode != 0 and not sentinel.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows job objects")
def test_missing_exact_job_reclaim_never_signals_reused_pid(monkeypatch):
    session = ctypes.c_ulong()
    assert ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session))
    record = SimpleNamespace(
        backend_agent_pid=os.getpid(),
        backend_process_identity=f"winjob-v1:{session.value}:{uuid.uuid4().hex}",
        issue_id="owned-test", identifier="OWNED-1",
    )
    kill = Mock(side_effect=AssertionError("must not signal numeric PID"))
    monkeypatch.setattr("symphony.orchestrator.core.kill_process_group", kill)
    assert _reclaim_kill_confirm(record) == "not_found"
    kill.assert_not_called()
    record.backend_agent_pid = None
    assert _reclaim_kill_confirm(record) is None


async def test_native_bash_descendants_stay_in_exact_job(tmp_path):
    from symphony._shell import resolve_bash, terminate_process_tree
    from symphony._windows_job import _api, job_exists

    pid_file = tmp_path / "bash-grandchild"
    grand_script, root_script = tmp_path / "grand.py", tmp_path / "root.py"
    grand_script.write_text(f"import os,time; open({pid_file.as_posix()!r},'w').write(str(os.getpid())); time.sleep(3600)")
    root_script.write_text(f"import subprocess,sys,time; subprocess.Popen([sys.executable,{grand_script.as_posix()!r}]); time.sleep(3600)")
    command = shlex.quote(sys.executable.replace("\\", "/")) + " " + shlex.quote(root_script.as_posix())
    proc, token = await _spawn([resolve_bash(), "-lc", command], tmp_path)
    handle = None
    try:
        _wait(pid_file.exists)
        handle = _api().OpenProcess(0x100000 | 0x1000, False, int(pid_file.read_text()))
        assert handle and _api().WaitForSingleObject(handle, 0) == 258
        assert await terminate_process_tree(proc, identity=token) is not None
        assert job_exists(token) is False and _api().WaitForSingleObject(handle, 2000) == 0
    finally:
        await terminate_process_tree(proc, identity=token)
        if handle:
            _api().CloseHandle(handle)


def test_native_existing_job_collision_aborts_before_ready(tmp_path):
    from symphony._windows_job import _api, _job_name, new_job_identity, owner_creation

    token = new_job_identity()
    handle = _api().CreateJobObjectW(None, _job_name(token))
    assert handle
    sentinel = tmp_path / "never-executed"
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "symphony._windows_backend", token, str(os.getpid()), owner_creation(), "null", sys.executable, "-c", f"open({str(sentinel)!r},'w').write('bad')"],
            input=b"SYMPHONY-GO-v1\n", capture_output=True, timeout=5,
        )
        assert proc.returncode != 0 and not proc.stdout and not sentinel.exists()
    finally:
        _api().CloseHandle(handle)


def test_native_assignment_failure_has_no_uncontained_fallback(tmp_path):
    from symphony._windows_job import new_job_identity, owner_creation

    token = new_job_identity()
    sentinel = tmp_path / "never-executed"
    argv = ["symphony._windows_backend", token, str(os.getpid()), owner_creation(), "null", sys.executable, "-c", f"open({str(sentinel)!r},'w').write('bad')"]
    code = f"""
import sys
from symphony._windows_job import _api
from symphony._windows_backend import _main
api = _api()
original = api.AssignProcessToJobObject
def invalid_assignment(job,process):
    api.CloseHandle(job)
    return original(job,process)
api.AssignProcessToJobObject = invalid_assignment
sys.argv = {argv!r}
_main()
"""
    proc = subprocess.run([sys.executable, "-c", code], input=b"SYMPHONY-GO-v1\n", capture_output=True, timeout=5)
    assert proc.returncode != 0 and not proc.stdout and not sentinel.exists()
    assert b"OSError" in proc.stderr
