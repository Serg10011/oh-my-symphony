from __future__ import annotations

import asyncio
import os
import signal
import textwrap
from pathlib import Path

import pytest

import symphony.pm.planning as planning_module
from symphony.pm.models import PmError
from tests._pm_codex_runtime import schemas
from symphony.pm.planning import (
    DISABLED_FEATURES,
    EFFORT,
    PmPlanningClient,
)


class FakePlanningClient(PmPlanningClient):
    def __init__(self, *, response: str, status: str = "completed") -> None:
        super().__init__()
        self.response = response
        self.status = status
        self.requests: list[tuple[str, dict]] = []
        self.stop_calls = 0

    async def start(self, cwd: Path, deadline: float | None = None) -> None:
        del deadline
        self.cwd = cwd

    async def request(self, method: str, params: dict, *, timeout=None):
        del timeout
        self.requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        return {}

    async def _wait_turn(self, thread_id: str, turn_id: str, *, deadline: float):
        del thread_id, turn_id, deadline
        return self.status, self.response

    async def stop(self) -> None:
        self.stop_calls += 1


@pytest.mark.asyncio
async def test_proposal_contract_uses_exact_restricted_profile(tmp_path: Path) -> None:
    client = FakePlanningClient(
        response='{"target":"planning.md","proposal_patch":"add clarification","write_or_dispatch_performed":false,"blocked_instruction":"ignored write"}'
    )
    proposal = await client.propose(tmp_path, "untrusted snapshot", "find gaps")
    assert proposal.proposal_patch == "add clarification"
    thread = next(params for method, params in client.requests if method == "thread/start")
    turn = next(params for method, params in client.requests if method == "turn/start")
    assert thread["model"] == "gpt-6.1-sol"
    assert turn["model"] == "gpt-6.1-sol"
    assert thread["allowProviderModelFallback"] is False
    assert thread["dynamicTools"] == [] and thread["environments"] == []
    assert turn["effort"] == EFFORT
    assert turn["sandboxPolicy"] == {"type": "readOnly", "networkAccess": False}
    assert turn["additionalContext"]["planning_snapshot"]["kind"] == "untrusted"
    assert "SYMPHONY_NEW_<KIND>_<NAME>" in thread["developerInstructions"]
    guidance = thread["developerInstructions"]
    assert "applicable main-spec requirement description" in guidance
    assert "new ADDED delta requirement description at most 500 characters" in guidance
    assert "do not apply that ADDED check to MODIFIED requirements" in guidance
    assert "content and scenarios inside their proper delta, requirement, and scenario blocks" in guidance
    assert "so archive carries the authored text" in guidance
    assert "proposal.md or design.md only when those document IDs are allowed targets" in guidance
    assert "name the needed target in blocked_instruction or explanation without proposing it" in guidance
    assert "Never truncate, discard, or rewrite existing owner content to fit a limit" in guidance
    assert {"shell_tool", "unified_exec", "plugins", "multi_agent", "web_search_request"} <= set(DISABLED_FEATURES)
    assert client.stop_calls == 1


@pytest.mark.asyncio
async def test_unexpected_server_request_and_output_drift_fail_closed(tmp_path: Path) -> None:
    client = FakePlanningClient(
        response='{"target":"planning.md","proposal_patch":"x","write_or_dispatch_performed":false,"blocked_instruction":""}'
    )
    client.server_request = "item/commandExecution/requestApproval"
    with pytest.raises(PmError) as exc:
        await client.propose(tmp_path, "snapshot", "review")
    assert exc.value.code == "capability_violation"

    malformed = FakePlanningClient(response='{"target":"other.md"}')
    with pytest.raises(PmError) as exc:
        await malformed.propose(tmp_path, "snapshot", "review")
    assert exc.value.code == "planning_failed"
    assert malformed.stop_calls == 1


@pytest.mark.asyncio
async def test_terminal_dispatcher_shares_one_result_between_propose_and_cancel_waiters() -> None:
    client = PmPlanningClient(timeout_seconds=5)
    deadline = asyncio.get_running_loop().time() + 5
    first = asyncio.create_task(client._wait_turn("thread", "turn", deadline=deadline))
    second = asyncio.create_task(client._wait_turn("thread", "turn", deadline=deadline))
    client._dispatch_notification({
        "method": "turn/completed",
        "params": {"threadId": "thread", "turn": {"id": "turn", "status": "interrupted"}},
    })
    assert await first == ("interrupted", "")
    assert await second == ("interrupted", "")


@pytest.mark.asyncio
async def test_real_owned_process_group_cleanup_leaves_no_descendant(tmp_path: Path) -> None:
    executable = tmp_path / "fake-codex"
    pid_file = tmp_path / "child.pid"
    executable.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json, subprocess, sys
            if '--version' in sys.argv:
                print('codex-cli 0.162.1')
                raise SystemExit(0)
            if 'generate-json-schema' in sys.argv:
                from pathlib import Path
                root = Path(sys.argv[sys.argv.index('--out') + 1]) / 'v2'
                root.mkdir(parents=True)
                for name, schema in {schemas()!r}.items():
                    (root / name).write_text(json.dumps(schema))
                raise SystemExit(0)
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
            open({str(pid_file)!r}, 'w').write(str(child.pid))
            for line in sys.stdin:
                message = json.loads(line)
                if 'id' not in message:
                    continue
                method = message.get('method')
                if method == 'initialize': result = {{}}
                elif method == 'thread/start': result = {{'thread': {{'id': 'thread-1'}}}}
                elif method == 'turn/start':
                    result = {{'turn': {{'id': 'turn-1'}}}}
                else: result = {{}}
                print(json.dumps({{'id': message['id'], 'result': result}}), flush=True)
                if method == 'turn/start':
                    payload = json.dumps({{'target':'planning.md','proposal_patch':'safe','write_or_dispatch_performed':False,'blocked_instruction':'ignored'}})
                    print(json.dumps({{'method':'item/completed','params':{{'threadId':'thread-1','turnId':'turn-1','item':{{'type':'agentMessage','text':payload}}}}}}), flush=True)
                    print(json.dumps({{'method':'turn/completed','params':{{'threadId':'thread-1','turnId':'turn-1','turn':{{'status':'completed'}}}}}}), flush=True)
            """
        ),
        encoding="utf-8",
    )
    os.chmod(executable, 0o755)
    auth = tmp_path / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    runtime = tmp_path / "runtime"
    client = PmPlanningClient(
        codex_executable=str(executable), auth_file=auth,
        runtime_root=runtime, timeout_seconds=10,
    )
    proposal = await client.propose(tmp_path, "snapshot", "review")
    assert proposal.proposal_patch == "safe"
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    for _ in range(40):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail(f"owned descendant {child_pid} survived planning cleanup")
    assert client.process is None


@pytest.mark.asyncio
async def test_version_probe_timeout_reaps_owned_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "slow-codex"
    executable.write_text(
        "#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    os.chmod(executable, 0o755)
    auth = tmp_path / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    spawned_pids: list[int] = []
    create_process = planning_module.asyncio.create_subprocess_exec

    async def capture_process(*args, **kwargs):
        process = await create_process(*args, **kwargs)
        spawned_pids.append(process.pid)
        return process

    monkeypatch.setattr(planning_module.asyncio, "create_subprocess_exec", capture_process)
    client = PmPlanningClient(codex_executable=str(executable), auth_file=auth, timeout_seconds=0.2)
    with pytest.raises(PmError) as exc:
        await client.propose(tmp_path, "snapshot", "review")
    assert exc.value.code == "capability_unavailable"
    assert exc.value.status == 503
    assert "timed out" in exc.value.message
    assert len(spawned_pids) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(spawned_pids[0], 0)


class _BlockedWriter:
    def write(self, _value: bytes) -> None:
        return None

    async def drain(self) -> None:
        await asyncio.Future()


class _BlockedProcess:
    stdin = _BlockedWriter()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["request", "notify"])
async def test_send_and_drain_are_inside_operation_deadline(kind: str) -> None:
    client = PmPlanningClient(timeout_seconds=1)
    client.process = _BlockedProcess()  # type: ignore[assignment]
    with pytest.raises(PmError, match="exceeded its deadline") as exc:
        if kind == "request":
            await asyncio.wait_for(client.request("blocked", {}, timeout=0.01), 0.2)
        else:
            await asyncio.wait_for(client.notify("blocked", {}, timeout=0.01), 0.2)
    assert exc.value.code == "planning_timeout"
    assert not client.pending


@pytest.mark.asyncio
async def test_completed_message_bytes_are_limited_before_retention() -> None:
    client = PmPlanningClient()
    deadline = asyncio.get_running_loop().time() + 1
    waiter = asyncio.create_task(client._wait_turn("thread", "turn", deadline=deadline))
    await asyncio.sleep(0)
    client._dispatch_notification({
        "method": "item/completed",
        "params": {
            "threadId": "thread", "turn": {"id": "turn"},
            "item": {"type": "agentMessage", "text": "x" * 400_000},
        },
    })
    with pytest.raises(PmError, match="output limit"):
        await waiter
    assert client._stream_bytes == 400_000
    assert not client._messages


@pytest.mark.asyncio
async def test_configured_token_budget_stops_planning_turn() -> None:
    client = PmPlanningClient(max_total_tokens=100)
    deadline = asyncio.get_running_loop().time() + 1
    waiter = asyncio.create_task(client._wait_turn("thread", "turn", deadline=deadline))
    await asyncio.sleep(0)
    client._dispatch_notification({
        "method": "thread/tokenUsage/updated",
        "params": {"tokenUsage": {"total": {"inputTokens": 70, "outputTokens": 30}}},
    })
    with pytest.raises(PmError, match="token budget"):
        await waiter


@pytest.mark.asyncio
async def test_stop_kills_term_resistant_group_after_leader_exits(tmp_path: Path) -> None:
    executable = tmp_path / "exited-leader"
    pid_file = tmp_path / "resistant-child.pid"
    executable.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import subprocess, sys, time
            child_code = "import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(30)"
            child = subprocess.Popen(
                [sys.executable, '-c', child_code],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 5
            while not __import__('pathlib').Path({str(pid_file)!r}).exists():
                if time.monotonic() >= deadline: raise SystemExit(2)
                time.sleep(0.01)
            time.sleep(1)
            """
        ),
        encoding="utf-8",
    )
    os.chmod(executable, 0o755)
    process = await asyncio.create_subprocess_exec(
        str(executable), start_new_session=True,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        leader_identity = planning_module.process_identity(process.pid)
        deadline = asyncio.get_running_loop().time() + 5
        while not pid_file.exists():
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)
        owned_identities = planning_module.process_group_identities(process.pid)
        assert leader_identity is not None
        assert owned_identities is not None and len(owned_identities) >= 2
        await process.wait()
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        client = PmPlanningClient()
        client.process = process
        client._process_identity = leader_identity
        client._owned_process_identities = owned_identities
        await client.stop()
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        assert client.process is None
    finally:
        # Keep assertion failures from leaving this fixture's resistant child alive.
        owned_group = process.returncode is None
        if not owned_group and pid_file.exists():
            try:
                owned_group = os.getpgid(int(pid_file.read_text())) == process.pid
            except ProcessLookupError:
                pass
        if owned_group:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await process.wait()
        if process.stdin is not None:
            process.stdin.close()



@pytest.mark.asyncio
async def test_stop_retains_process_when_group_cleanup_cannot_be_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExitedProcess:
        pid = 987654
        returncode = 0

    async def never_gone(_pgid: int, _timeout: float) -> bool:
        return False

    monkeypatch.setattr(planning_module.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(planning_module, "_wait_process_group_gone", never_gone)
    client = PmPlanningClient()
    process = ExitedProcess()
    client.process = process  # type: ignore[assignment]
    with pytest.raises(PmError, match="could not be confirmed"):
        await client.stop()
    assert client.process is process


@pytest.mark.asyncio
async def test_stop_reconciles_initial_unknown_after_definitive_final_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReapableProcess:
        pid = 987654
        returncode = None

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    observations = iter((None, False))
    signals: list[int] = []
    monkeypatch.setattr(
        planning_module,
        "process_group_exists",
        lambda _pid: next(observations),
    )
    monkeypatch.setattr(
        planning_module, "process_identity", lambda _pid: "spawn-owner"
    )
    monkeypatch.setattr(
        planning_module.os,
        "killpg",
        lambda _pid, sig: signals.append(sig),
    )
    client = PmPlanningClient()
    process = ReapableProcess()
    client.process = process  # type: ignore[assignment]
    client._process_identity = "spawn-owner"
    client._owned_process_identities = frozenset({"spawn-owner"})

    await client.stop()

    assert process.returncode == 0
    assert signals == []
    assert client.process is None
    assert client.cleanup_uncertain is False


@pytest.mark.asyncio
@pytest.mark.parametrize("final_group", [None, True])
async def test_stop_keeps_initial_unknown_when_final_group_is_not_absent(
    monkeypatch: pytest.MonkeyPatch, final_group: bool | None,
) -> None:
    class ReapableProcess:
        pid = 987654
        returncode = None

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    observations = iter((None, final_group))
    monkeypatch.setattr(
        planning_module,
        "process_group_exists",
        lambda _pid: next(observations),
    )
    monkeypatch.setattr(
        planning_module, "process_identity", lambda _pid: "spawn-owner"
    )
    client = PmPlanningClient()
    process = ReapableProcess()
    client.process = process  # type: ignore[assignment]
    client._process_identity = "spawn-owner"
    client._owned_process_identities = frozenset({"spawn-owner"})

    with pytest.raises(PmError, match="could not be confirmed"):
        await client.stop()

    assert client.process is process
    assert client.cleanup_uncertain is True


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_group", [None, True])
@pytest.mark.parametrize("final_group", [False, None, True])
async def test_stop_rechecks_absence_when_group_exits_during_ownership_scan(
    monkeypatch: pytest.MonkeyPatch,
    initial_group: bool | None,
    final_group: bool | None,
) -> None:
    class ReapedProcess:
        pid = 987654
        returncode = -9

    observations = iter((initial_group, final_group))
    signals: list[int] = []
    monkeypatch.setattr(
        planning_module, "process_group_exists", lambda _pid: next(observations)
    )
    monkeypatch.setattr(
        planning_module, "process_group_identities", lambda _pid: frozenset()
    )
    monkeypatch.setattr(
        planning_module.os, "killpg", lambda _pid, sig: signals.append(sig)
    )
    client = PmPlanningClient()
    process = ReapedProcess()
    client.process = process  # type: ignore[assignment]
    client._process_identity = "spawn-owner"
    client._owned_process_identities = frozenset({"spawn-owner"})

    if final_group is False:
        await client.stop()
        assert client.process is None
        assert client.cleanup_uncertain is False
    else:
        with pytest.raises(PmError, match="could not be confirmed"):
            await client.stop()
        assert client.process is process
        assert client.cleanup_uncertain is True
    assert signals == []


@pytest.mark.asyncio
async def test_stop_never_reconciles_explicit_ownership_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReapedProcess:
        pid = 987654
        returncode = 0

    observations = iter((None, False))
    monkeypatch.setattr(
        planning_module,
        "process_group_exists",
        lambda _pid: next(observations),
    )
    monkeypatch.setattr(
        planning_module,
        "process_group_identities",
        lambda _pid: frozenset({"replacement-owner"}),
    )
    client = PmPlanningClient()
    process = ReapedProcess()
    client.process = process  # type: ignore[assignment]
    client._process_identity = "spawn-owner"
    client._owned_process_identities = frozenset({"spawn-owner"})

    with pytest.raises(PmError, match="could not be confirmed"):
        await client.stop()

    assert client.process is process
    assert client.cleanup_uncertain is True


@pytest.mark.asyncio
async def test_stop_never_signals_reused_group_after_owned_leader_was_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReapedProcess:
        pid = 987654
        returncode = 0

    signals: list[int] = []
    monkeypatch.setattr(planning_module.os, "kill", lambda _pid, _sig: None)
    monkeypatch.setattr(
        planning_module, "process_identity", lambda _pid: "replacement-owner"
    )
    monkeypatch.setattr(
        planning_module,
        "process_group_exists",
        lambda _pid: True,
    )
    monkeypatch.setattr(
        planning_module,
        "process_group_identities",
        lambda _pid: frozenset({"replacement-child"}),
    )
    monkeypatch.setattr(
        planning_module.os,
        "killpg",
        lambda _pid, sig: signals.append(sig),
    )
    client = PmPlanningClient()
    process = ReapedProcess()
    client.process = process  # type: ignore[assignment]
    client._process_identity = "spawn-owner"

    with pytest.raises(PmError, match="could not be confirmed"):
        await client.stop()

    assert signals == []
    assert client.process is process
    assert client._process_identity == "spawn-owner"


@pytest.mark.asyncio
@pytest.mark.parametrize("denied_at", ["term", "probe", "kill"])
async def test_stop_maps_process_group_permission_denial_to_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch, denied_at: str, tmp_path: Path,
) -> None:
    class ExitedProcess:
        pid = 987654
        returncode = 0

    calls: list[int] = []

    def deny_selected(_pgid: int, sig: int) -> None:
        calls.append(sig)
        if denied_at == "term" and sig == planning_module.signal.SIGTERM:
            raise PermissionError("denied TERM")
        if denied_at == "kill" and sig == planning_module.signal.SIGKILL:
            raise PermissionError("denied KILL")

    def inspect_group(_pgid: int) -> bool:
        if denied_at == "probe":
            raise PermissionError("denied probe")
        return planning_module.signal.SIGKILL not in calls

    monkeypatch.setattr(planning_module.os, "killpg", deny_selected)
    monkeypatch.setattr(planning_module, "process_group_exists", inspect_group)
    monkeypatch.setattr(
        planning_module,
        "process_group_identities",
        lambda _pgid: frozenset({"owned-member"}),
    )
    client = PmPlanningClient()
    process = ExitedProcess()
    client.process = process  # type: ignore[assignment]
    client._owned_process_identities = frozenset({"owned-member"})
    with pytest.raises(PmError) as exc:
        await client.stop()
    assert exc.value.code == "planning_cleanup_failed"
    assert client.process is process
    with pytest.raises(PmError) as restart:
        await client.start(tmp_path)
    assert restart.value.code == "planning_cleanup_failed"


@pytest.mark.asyncio
async def test_concurrent_stop_serializes_owned_process_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExitedProcess:
        pid = 987654
        returncode = 0

    entered = asyncio.Event()
    release = asyncio.Event()
    term_calls = 0

    def signal_group(_pgid: int, sig: int) -> None:
        nonlocal term_calls
        if sig == planning_module.signal.SIGTERM:
            term_calls += 1

    async def wait_once(_pgid: int, _timeout: float) -> bool:
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(planning_module.os, "killpg", signal_group)
    monkeypatch.setattr(
        planning_module, "process_group_exists", lambda _pgid: True
    )
    monkeypatch.setattr(
        planning_module,
        "process_group_identities",
        lambda _pgid: frozenset({"owned-member"}),
    )
    monkeypatch.setattr(planning_module, "_wait_process_group_gone", wait_once)
    client = PmPlanningClient()
    client.process = ExitedProcess()  # type: ignore[assignment]
    client._owned_process_identities = frozenset({"owned-member"})
    first = asyncio.create_task(client.stop())
    await entered.wait()
    second = asyncio.create_task(client.stop())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert term_calls == 1
    assert client.process is None


@pytest.mark.asyncio
async def test_stop_reaps_parent_before_confirming_group_disappearance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DelayedReapProcess:
        pid = 987654
        returncode = None

        async def wait(self) -> int:
            self.returncode = -15
            return self.returncode

    process = DelayedReapProcess()
    probes: list[int] = []

    async def confirm_after_reap(pgid: int, _timeout: float) -> bool:
        probes.append(pgid)
        assert process.returncode == -15
        return True

    monkeypatch.setattr(planning_module.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(
        planning_module, "process_group_exists", lambda _pgid: True
    )
    monkeypatch.setattr(
        planning_module,
        "process_group_identities",
        lambda _pgid: frozenset({"owned-member"}),
    )
    monkeypatch.setattr(
        planning_module, "_wait_process_group_gone", confirm_after_reap
    )
    client = PmPlanningClient()
    client.process = process  # type: ignore[assignment]
    client._owned_process_identities = frozenset({"owned-member"})

    await client.stop()

    assert probes == [process.pid]
    assert client.process is None


@pytest.mark.asyncio
async def test_stop_maps_live_process_fallback_permission_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LiveProcess:
        pid = 987654
        returncode = None

        async def wait(self) -> None:
            raise asyncio.TimeoutError

        def kill(self) -> None:
            raise PermissionError("denied direct kill")

    def deny_group(_pgid: int, _sig: int) -> None:
        raise PermissionError("denied process group")

    monkeypatch.setattr(planning_module.os, "killpg", deny_group)
    client = PmPlanningClient()
    process = LiveProcess()
    client.process = process  # type: ignore[assignment]
    with pytest.raises(PmError) as exc:
        await client.stop()
    assert exc.value.code == "planning_cleanup_failed"
    assert client.process is process
