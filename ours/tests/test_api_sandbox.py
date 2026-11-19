"""Isolation policy and bounded lifecycle, without providers or real credentials."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ours/hooks"))
import api_sandbox as sandbox  # noqa: E402


@pytest.fixture
def policy(tmp_path):
    workspace = tmp_path / "workspace"
    control = tmp_path / "control"
    runtime = tmp_path / "runtime"
    for path in (workspace, control, runtime):
        path.mkdir(mode=0o700)
    return sandbox.Policy(workspace, control, (runtime,), executable=sys.executable)


def test_profile_is_not_resolved_in_editable_project_and_command_is_one_argument(policy, monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    command = 'echo "quoted"; $(anything); `anything`; printf "line\\n"'
    argv = policy.argv(command)
    assert argv[argv.index("-C") + 1] == str(policy.control)
    assert argv[-1] == command and argv[-2] == str(policy.workspace)
    config = tomllib.loads(argv[argv.index("-c") + 1])
    profile, = config["permissions"].values()
    assert profile["filesystem"][":root"] == "deny"
    assert profile["filesystem"][":slash_tmp"] == "deny"
    assert profile["filesystem"][":workspace_roots"] == "read"
    assert profile["filesystem"][str(policy.workspace)] == "write"
    assert profile["filesystem"][str(policy.read_roots[0])] == "read"
    assert profile["network"]["enabled"] is False
    assert policy.argv(command)[3] != argv[3]


def test_node_adds_only_read_grants_and_a_fixed_environment_entry(policy, monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    monkeypatch.setenv("SYMPHONY_NODE", "/inherited/node")
    monkeypatch.setenv("NODE_OPTIONS", "--require /inherited/script.js")
    node = policy.workspace.parent / "node"
    node.touch(mode=0o700)

    def permissions(configured):
        args = configured.argv("true")
        profile, = tomllib.loads(args[args.index("-c") + 1])["permissions"].values()
        return profile

    original = permissions(policy)
    configured = replace(policy, node=node)
    expected = dict(original["filesystem"]) | {str(node): "read", sandbox.NODE_OPENSSL_CONFIG: "read"}
    assert permissions(configured) == {"filesystem": expected, "network": original["network"]}
    assert {"SYMPHONY_NODE", "NODE_OPTIONS"}.isdisjoint(sandbox._environment(policy))
    assert sandbox._environment(configured) == sandbox._environment(policy) | {"SYMPHONY_NODE": str(node)}


@pytest.mark.parametrize("location", ["workspace", "symlink", "missing", "not_executable"])
def test_node_refuses_unusable_or_task_controlled_paths(policy, monkeypatch, location):
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    parent = policy.workspace.parent
    node = (policy.workspace if location == "workspace" else parent) / "node"
    if location == "symlink":
        node.symlink_to(Path(sys.executable).resolve())
    elif location != "missing":
        node.touch(mode=0o600 if location == "not_executable" else 0o700)
    with pytest.raises(sandbox.SandboxError, match="sandbox_node_invalid"):
        replace(policy, node=node).argv("true")


def test_unsupported_platform_never_runs_an_unisolated_command(policy, monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("command spawned"))
    with pytest.raises(sandbox.SandboxError, match="sandbox_platform_unsupported"):
        sandbox.run(policy, "true")


@pytest.mark.parametrize("grant", ["root", "home", "parent", "workspace", "control"])
def test_runtime_grants_cannot_reopen_host_or_control(policy, monkeypatch, grant):
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    root = {"root": Path("/"), "home": Path.home(), "parent": policy.workspace.parent,
            "workspace": policy.workspace, "control": policy.control}[grant]
    bad = sandbox.Policy(policy.workspace, policy.control, (root,), executable=sys.executable)
    with pytest.raises(sandbox.SandboxError, match="runtime_root_too_broad"):
        bad.argv("true")


def test_untrusted_control_or_symlink_directory_refused(policy, monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    (policy.control / "config.toml").write_text("untrusted")
    with pytest.raises(sandbox.SandboxError, match="control_must_be_empty"):
        policy.argv("true")
    (policy.control / "config.toml").unlink()
    alias = policy.workspace.parent / "alias"
    alias.symlink_to(policy.workspace, target_is_directory=True)
    with pytest.raises(sandbox.SandboxError, match="directory_invalid"):
        sandbox.Policy(alias, policy.control, (), executable=sys.executable).argv("true")


def test_missing_sandbox_never_falls_back_to_direct_execution(policy, monkeypatch):
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    bad = sandbox.Policy(policy.workspace, policy.control, (), executable="/nonexistent/sandbox")
    monkeypatch.setattr(sandbox.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned without sandbox"))
    with pytest.raises(sandbox.SandboxError, match="executable_unavailable"):
        sandbox.run(bad, "touch unsafe")


@pytest.mark.parametrize("command", ["", " ", "echo\x00secret", "\ud800", "x" * (sandbox.MAX_COMMAND_BYTES + 1)])
def test_invalid_commands_never_spawn(policy, monkeypatch, command):
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    monkeypatch.setattr(sandbox.subprocess, "Popen", lambda *a, **k: pytest.fail("invalid spawn"))
    with pytest.raises(sandbox.SandboxError):
        sandbox.run(policy, command)


def _known_test_program(monkeypatch, script):
    # This tests process bookkeeping only, using fixed test-owned programs.
    # The real OS boundary is tested separately below; there is no runtime bypass.
    monkeypatch.setattr(sandbox.Policy, "argv", lambda self, command: [sys.executable, "-I", "-c", script])


def test_environment_allowlist_excludes_keys_and_startup_injection(policy, monkeypatch):
    for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                 "BASH_ENV", "ENV", "PYTHONPATH", "DYLD_INSERT_LIBRARIES", "HTTPS_PROXY", "GIT_CONFIG_COUNT"):
        monkeypatch.setenv(name, "not-a-real-secret")
    _known_test_program(monkeypatch, "import os,json; print(json.dumps(dict(os.environ)))")
    result = sandbox.run(policy, "ignored")
    environment = json.loads(result["output"])
    assert "not-a-real-secret" not in result["output"]
    assert environment["TMPDIR"] == str(policy.workspace / ".tmp")
    assert result["exit_code"] == 0 and result["stop_reason"] is None


def test_nonzero_exit_is_evidence_not_silent_success(policy, monkeypatch):
    _known_test_program(monkeypatch, "import sys; print('assertion failed'); sys.exit(7)")
    result = sandbox.run(policy, "ignored")
    assert result["exit_code"] == 7 and result["stop_reason"] is None
    assert result["output"] == "assertion failed\n"


def test_process_group_cleanup_holds_the_child_pid_until_signalling(policy, monkeypatch):
    _known_test_program(monkeypatch, "print('done')")
    real_kill = os.killpg
    observed = []
    def check(pid, sig):
        # ChildProcessError here would mean the PID was already reaped and could
        # identify an unrelated process group. WNOWAIT preserves the assertion.
        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        observed.append(pid)
        return real_kill(pid, sig)
    monkeypatch.setattr(sandbox.os, "killpg", check)
    assert sandbox.run(policy, "ignored")["exit_code"] == 0
    assert len(observed) == 1


def test_timeout_and_infinite_output_are_bounded(policy, monkeypatch):
    _known_test_program(monkeypatch, "import time; time.sleep(30)")
    result = sandbox.run(policy, "ignored", timeout=1)
    assert result["stop_reason"] == "timeout" and result["seconds"] < 6
    _known_test_program(monkeypatch, "import sys; sys.stdout.write('x'*1000000); sys.stdout.flush()")
    result = sandbox.run(policy, "ignored", output_limit=1024)
    assert result["stop_reason"] == "output_limit" and result["truncated"]
    assert len(result["output"]) == result["output_bytes"] == 1024


def test_symlink_temporary_directory_cannot_escape_workspace(policy, monkeypatch):
    _known_test_program(monkeypatch, "raise AssertionError('must not execute')")
    (policy.workspace / ".tmp").symlink_to(policy.control, target_is_directory=True)
    with pytest.raises(sandbox.SandboxError, match="temporary_directory_invalid"):
        sandbox.run(policy, "ignored")


@pytest.mark.parametrize("output", ["not json", "{}", '{"workspace_write_read":true}'])
def test_inconclusive_probe_is_a_failure(policy, monkeypatch, output):
    monkeypatch.setattr(sandbox, "run", lambda *a, **k: {"output": output, "exit_code": 0, "stop_reason": None})
    with pytest.raises(sandbox.SandboxError, match="sandbox_probe_failed"):
        sandbox.probe(policy, python=sys.executable)
    assert not list(policy.workspace.glob("probe-*"))
    assert not list(policy.workspace.glob("link-*"))
    assert not list(policy.workspace.parent.glob("canary-*"))


def test_configured_node_probe_requires_the_runtime_response(policy, monkeypatch):
    checks = dict.fromkeys(("workspace_write_read", "outside_read_denied", "symlink_read_denied",
                           "outside_write_denied", "network_denied", "credentials_absent"), True)
    node = policy.workspace.parent / "node"
    commands = []
    output = ""

    def run(configured, command, **options):
        args = shlex.split(command)
        commands.append(args)
        assert configured.node == node
        response = output if args[0] == str(node) else json.dumps(checks)
        return {"output": response, "exit_code": 0, "stop_reason": None, "seconds": 0.1}

    monkeypatch.setattr(sandbox, "run", run)
    configured = replace(policy, node=node)
    with pytest.raises(sandbox.SandboxError, match="sandbox_node_probe_failed"):
        sandbox.probe(configured, python=sys.executable)
    output = "node"
    assert sandbox.probe(configured, python=sys.executable) == {
        "checks": checks | {"node_available": True}, "seconds": 0.2,
    }
    assert [args[0] for args in commands] == [sys.executable, str(node)] * 2


@pytest.mark.skipif(sys.platform != "darwin" or os.environ.get("SYMPHONY_TEST_NATIVE_SANDBOX") != "1",
                    reason="explicit macOS isolation probe; cannot nest inside another Seatbelt profile")
def test_native_node_keeps_the_file_and_network_boundary(tmp_path):
    playwright = pytest.importorskip("playwright")
    node = (Path(playwright.__file__).resolve().parent / "driver" / "node").resolve()
    sibling = node.parent / "package" / "package.json"
    if not node.is_file() or not sibling.is_file():
        pytest.skip("Playwright's bundled Node and its adjacent package are required")
    workspace, control = tmp_path / "workspace", tmp_path / "control"
    workspace.mkdir(mode=0o700)
    control.mkdir(mode=0o700)
    policy = sandbox.Policy(workspace.resolve(), control.resolve(), (Path(sys.base_prefix).resolve(),), node=node)
    assert sandbox.probe(policy, python=str(Path(sys.executable).resolve()))["checks"]["node_available"]
    outside = tmp_path / "canary.txt"
    outside.write_text("non-secret")
    script = """
const fs = require('node:fs'), net = require('node:net'), assert = require('node:assert/strict');
const [outside, sibling, port] = process.argv.slice(2);
const denied = error => ['EACCES', 'EPERM'].includes(error.code);
fs.writeFileSync('inside.txt', 'inside');
assert.equal(fs.readFileSync('inside.txt', 'utf8'), 'inside');
assert.throws(() => fs.readFileSync(outside), denied);
assert.throws(() => fs.readFileSync(sibling), denied);
assert.throws(() => fs.openSync(outside, 'r+'), denied);
assert.throws(() => fs.openSync(process.execPath, 'r+'), denied);
const client = net.connect({host: '127.0.0.1', port: Number(port)});
client.setTimeout(1500, () => { throw Error('No evidence of network denial'); });
client.on('connect', () => { throw Error('Network unexpectedly available'); });
client.on('error', error => { assert(denied(error)); console.log('node boundaries verified'); });
"""
    (workspace / "probe.cjs").write_text(script)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        arguments = shlex.join(["probe.cjs", str(outside), str(sibling), str(listener.getsockname()[1])])
        result = sandbox.run(policy, '"$SYMPHONY_NODE" ' + arguments, timeout=12)
    assert result["exit_code"] == 0 and result["stop_reason"] is None, result["output"]
    assert result["output"].strip() == "node boundaries verified"
    assert outside.read_text() == "non-secret"


@pytest.mark.skipif(sys.platform != "darwin" or os.environ.get("SYMPHONY_TEST_NATIVE_SANDBOX") != "1",
                    reason="explicit macOS isolation probe; cannot nest inside another Seatbelt profile")
def test_native_sandbox_boundaries(tmp_path, monkeypatch):
    workspace, control = tmp_path / "workspace", tmp_path / "control"
    workspace.mkdir(mode=0o700)
    control.mkdir(mode=0o700)
    policy = sandbox.Policy(workspace.resolve(), control.resolve(), (Path(sys.base_prefix).resolve(),))
    monkeypatch.setenv("OPENAI_API_KEY", "FAKE_PROBE_VALUE")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "FAKE_PROBE_VALUE")
    assert all(sandbox.probe(policy, python=str(Path(sys.executable).resolve()))["checks"].values())
    # Temporary files and subprocesses used by ordinary tests work inside the
    # private task directory. Project-controlled Codex config is never loaded.
    config = workspace / ".codex"
    config.mkdir()
    (config / "config.toml").write_text('this is deliberately invalid TOML [[[')
    command = shlex.join([str(Path(sys.executable).resolve()), "-I", "-c",
        "import tempfile,pathlib,subprocess; p=pathlib.Path(tempfile.mkstemp()[1]); "
        "p.write_text('ok'); assert p.read_text()=='ok'; "
        "subprocess.run(['/bin/echo','child-ok'],check=True)"])
    result = sandbox.run(policy, command)
    assert result["exit_code"] == 0 and result["output"].strip() == "child-ok"
    # A fake sibling controller holds a canary only. Project commands must not
    # obtain its environment or terminate it; no real service/key is involved.
    sibling = subprocess.Popen([str(Path(sys.executable).resolve()), "-I", "-c", "import time; time.sleep(30)"],
                               env={"PATH": os.defpath, "FAKE_SECRET": "S4_62_CONTROLLER_CANARY"})
    try:
        result = sandbox.run(policy, f"/bin/ps eww -p {sibling.pid}")
        assert "S4_62_CONTROLLER_CANARY" not in result["output"]
        result = sandbox.run(policy, f"/bin/kill -TERM {sibling.pid}")
        assert result["exit_code"] != 0 and sibling.poll() is None
    finally:
        sibling.terminate()
        sibling.wait(timeout=5)
