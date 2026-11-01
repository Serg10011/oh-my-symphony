from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path


import pytest

from symphony._shell import _host_boot_token

from symphony.pm import codex_compatibility, host_tools, planning
from symphony.pm.models import PmError
from symphony.pm.planning import DISABLED_FEATURES, PmPlanningClient
from tests._pm_codex_runtime import fake_codex, schemas


@pytest.fixture(autouse=True)
def process_identity_cache_matches_test_path():
    # These fixtures deliberately replace PATH; do not retain a failed sysctl lookup.
    _host_boot_token.cache_clear()
    yield
    _host_boot_token.cache_clear()


@pytest.mark.parametrize("version", ["0.162.1", "0.163.0-alpha.1"])
def test_installed_unpinned_version_is_admitted_without_auth_read(tmp_path, monkeypatch, version):
    fake_codex(tmp_path, version, mode="auth_read")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-probe")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-reach-probe")
    result = host_tools.probe_codex()
    assert result["found"] == version
    assert "Codex login" in result["reason"]
    assert "required" in result and result["required"] != "0.160.0"
    assert "controls" not in result["reason"]


@pytest.mark.parametrize("missing", [
    (filename, key) for filename, schema in schemas().items() for key in schema["properties"]
])
def test_missing_controls_fail_with_specific_recovery(tmp_path, monkeypatch, missing):
    fake_codex(tmp_path, missing=missing)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    result = host_tools.probe_codex()
    assert result["available"] is False
    assert missing[1] in result["reason"]
    assert "compatible" in result["reason"]


@pytest.mark.parametrize("mode", ["unsupported_flags", "server_request"])
def test_failed_strict_initialize_never_admits_runtime(tmp_path, monkeypatch, mode):
    fake_codex(tmp_path, mode=mode)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    result = host_tools.probe_codex()
    assert result["available"] is False
    assert "strict" in result["reason"]


@pytest.mark.asyncio
async def test_start_shares_schema_refusal_and_preserves_no_side_effects(tmp_path):
    executable = fake_codex(tmp_path, missing=("TurnStartParams.json", "outputSchema"))
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    runtime = tmp_path / "runtime"
    client = PmPlanningClient(codex_executable=str(executable), auth_file=auth, runtime_root=runtime)
    with pytest.raises(PmError) as exc:
        await client.start(tmp_path)
    assert exc.value.code == "capability_unavailable" and exc.value.status == 503
    assert "outputSchema" in exc.value.message
    assert client.process is None
    assert not runtime.exists()


def test_same_path_binary_replacement_invalidates_cached_admission(tmp_path, monkeypatch):
    executable = fake_codex(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    auth = tmp_path / ".codex" / "auth.json"
    auth.parent.mkdir()
    auth.write_text("{}")
    assert host_tools.probe_codex()["available"] is True
    before = executable.stat().st_ino
    executable.unlink()
    fake_codex(tmp_path, missing=("ThreadStartParams.json", "ephemeral"))
    assert executable.stat().st_ino != before or executable.stat().st_ctime_ns
    result = host_tools.probe_codex()
    assert result["found"] == "0.162.1" and result["available"] is False
    assert "ephemeral" in result["reason"]


@pytest.mark.parametrize("schema", [
    {"type": "object", "properties": {"type": {"enum": ["readOnly"]}}},
    {"type": "object", "properties": {"type": {"enum": ["readOnly"]}, "networkAccess": {"type": "boolean", "const": True}}},
    {"not": {}},
])
def test_nested_network_control_must_explicitly_accept_false(tmp_path, monkeypatch, schema):
    fake_codex(tmp_path, mutate=lambda data: data["TurnStartParams.json"]["properties"].update(sandboxPolicy=schema))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    result = host_tools.probe_codex()
    assert "sandboxPolicy" in result["reason"] and result["available"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ok", "unsupported_flags", "server_request"])
async def test_local_probe_processes_are_reaped_and_profile_is_unchanged(tmp_path, monkeypatch, mode):
    executable = fake_codex(tmp_path, mode=mode)
    spawned = []
    homes = []
    create = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        process = await create(*args, **kwargs)
        spawned.append(process.pid)
        homes.append(Path(kwargs["env"]["CODEX_HOME"]))
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    admission = await codex_compatibility.admit_codex(str(executable),
        deadline=asyncio.get_running_loop().time() + 5)
    assert (admission.reason is None) == (mode == "ok")
    for pid in spawned:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert all(not home.exists() for home in homes)
    calls = [json.loads(line) for line in executable.with_suffix(".calls").read_text().splitlines()]
    startup = calls[-1]
    assert startup[:2] == ["app-server", "--listen"]
    assert {startup[index + 1] for index, value in enumerate(startup) if value == "--disable"} == set(DISABLED_FEATURES)


@pytest.mark.asyncio
@pytest.mark.parametrize(("expiry", "reason"), [
    ("before_wait", "planning operation exceeded its absolute deadline"),
    ("during_wait", "timed out"),
])
async def test_expired_probe_reaps_started_processes_and_removes_profiles(tmp_path, monkeypatch, expiry, reason):
    ready = tmp_path / "waiting.pid"
    executable = fake_codex(tmp_path, mode="timeout", ready_file=ready)
    spawned = []
    homes = []
    create = asyncio.create_subprocess_exec
    remaining = planning._remaining
    expiry_pending = False
    expirations = []

    async def capture(*args, **kwargs):
        nonlocal expiry_pending
        process = await create(*args, **kwargs)
        spawned.append(process.pid)
        homes.append(Path(kwargs["env"]["CODEX_HOME"]))
        if "generate-json-schema" in args:
            async def wait_until_stalled():
                while not ready.exists():
                    if process.returncode is not None:
                        pytest.fail("probe exited before reaching its controlled wait")
                    await asyncio.sleep(0.01)
                assert int(ready.read_text()) == process.pid

            try:
                # This is only a fixture liveness guard, not the tested deadline.
                await asyncio.wait_for(wait_until_stalled(), 10)
            except BaseException:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                raise
            expiry_pending = True
        return process

    def expire_stalled_probe(deadline):
        nonlocal expiry_pending
        if not expiry_pending:
            return remaining(deadline)
        expiry_pending = False
        expirations.append(expiry)
        if expiry == "before_wait":
            return remaining(float("-inf"))
        # The process has acknowledged its 30-second wait. Startup latency
        # cannot decide which expiry path this short read budget exercises.
        return 0.01

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    monkeypatch.setattr(planning, "_remaining", expire_stalled_probe)
    loop = asyncio.get_running_loop()
    errors = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _, context: errors.append(context["message"]))
    try:
        admission = await codex_compatibility.admit_codex(str(executable))
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert expirations == [expiry]
    assert admission.found == "0.162.1"
    assert admission.reason is not None and reason in admission.reason
    calls = [json.loads(line) for line in executable.with_suffix(".calls").read_text().splitlines()]
    assert len(spawned) == len(homes) == len(calls) == 2
    assert calls[0] == ["--version"]
    assert calls[1][:2] == ["app-server", "generate-json-schema"]
    assert int(ready.read_text()) == spawned[1]
    for pid in spawned:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert all(not home.exists() for home in homes)
    assert errors == []


@pytest.mark.asyncio
async def test_auth_unavailable_refuses_before_any_probe_or_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_compatibility, "admit_codex", lambda *a, **k: pytest.fail("authless planning probed"))
    client = PmPlanningClient(auth_file=tmp_path / "missing-auth")
    with pytest.raises(PmError) as exc:
        await client.start(tmp_path)
    assert exc.value.code == "capability_unavailable" and exc.value.status == 503
    assert "authentication" in exc.value.message
    assert client.process is None


def test_delegating_javascript_launcher_never_reuses_admission(tmp_path, monkeypatch):
    original = fake_codex(tmp_path)
    launcher = original.with_suffix(".js")
    original.rename(launcher)
    original.symlink_to(launcher)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    host_tools.probe_codex()
    host_tools.probe_codex()
    import json
    calls = [json.loads(line) for line in launcher.with_suffix(".calls").read_text().splitlines()]
    assert sum("generate-json-schema" in call for call in calls) == 2
    assert str(launcher.resolve()) not in codex_compatibility._CACHE
