"""Read-only CLI readiness rejects unsafe/malformed startup without a prompt."""

import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ours/hooks"))
import api_flash_probe as flash  # noqa: E402


def init(directory, **changes):
    return {"event": "init", "init": {"model": flash.MODEL, "agent": flash.AGENT,
            "cwd": str(directory), "tools": ["finish"], "permission_mode": "strict", **changes}}


def test_catalog_does_not_establish_effective_permissions_or_readiness(tmp_path):
    result = flash.inspect_init(init(tmp_path, tools=["finish", "run_command", "open_browser_url"]), tmp_path)
    assert result["ready_for_tasks"] is False and result["model_prompt_sent"] is False
    assert result["reason"] == "flash_effective_permissions_unverified"
    assert result["startup_contract_matches"] and not result["tool_catalog_matches_profile"]
    assert result["effective_permissions_verified"] is False
    assert result["unexpected_tools"] == ["open_browser_url", "run_command"]
    assert result["quota"]["five_hour_remaining"] is None
    assert result["quota"]["weekly_remaining"] is None


def test_safe_startup_does_not_claim_finished_transport(tmp_path):
    result = flash.inspect_init(init(tmp_path), tmp_path)
    assert result["startup_contract_matches"] is True and result["ready_for_tasks"] is False
    assert result["reason"] == "flash_effective_permissions_unverified"


@pytest.mark.parametrize("changes", [{"model": "other"}, {"cwd": "/"}, {"agent": "default"},
                                      {"permission_mode": "always-proceed"}, {"tools": [None]}, {"tools": None}])
def test_effective_configuration_must_match(tmp_path, changes):
    with pytest.raises(flash.ProbeError, match="flash_startup_contract_mismatch"):
        flash.inspect_init(init(tmp_path, **changes), tmp_path)


@pytest.mark.parametrize("name,contents", [
    ("config/hooks.json", '{"hooks":{"SessionStart":["something"]}}'),
    ("config/mcp_config.json", '{"mcpServers":{"host":{"command":"something"}}}'),
    ("config/plugins.json", "{}"), ("config/agents/worker.md", "x"),
    ("skills/plugin/SKILL.md", "x"), ("antigravity-cli/hooks.json", '{"hooks":[1]}'),
])
def test_customizations_are_checked_before_launch(tmp_path, name, contents):
    path = tmp_path / ".gemini" / name
    path.parent.mkdir(parents=True)
    path.write_text(contents)
    with pytest.raises(flash.ProbeError, match="flash_customizations_active"):
        flash.check_customizations(tmp_path)


def test_empty_mcp_config_is_inert(tmp_path):
    directory = tmp_path / ".gemini/config"
    directory.mkdir(parents=True)
    (directory / "mcp_config.json").write_text('{"mcpServers":{}}')
    flash.check_customizations(tmp_path)


def fake_cli(tmp_path, body):
    executable = tmp_path / "agy-fake"
    executable.write_text(f"#!{sys.executable}\n" + body)
    executable.chmod(0o700)
    return executable


def test_real_stdio_init_sends_no_prompt_or_keys_and_uses_runtime_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "private-sentinel")
    monkeypatch.setenv("GEMINI_API_KEY", "paid-route-must-not-leak")
    executable = fake_cli(tmp_path, '''import json, os, select, sys, time
assert "OPENAI_API_KEY" not in os.environ and "GEMINI_API_KEY" not in os.environ
assert not select.select([sys.stdin], [], [], .05)[0], "Unexpected model prompt or EOF"
settings=json.loads(sys.argv[sys.argv.index("--settings")+1])
assert settings["useG1Credits"] is False and settings["modelProvider"] == ""
assert settings["permissions"]["deny"] and settings["toolPermission"] == "strict"
print(json.dumps({"event":"init","init":{"model":"gemini-3.8-flash-high",
"agent":"symphony-controlled-worker","permission_mode":"strict","cwd":os.getcwd(),"tools":["finish"]}}), flush=True)
time.sleep(60)
''')
    result = flash.probe(executable, timeout=3, temporary_root=tmp_path)
    assert result["startup_contract_matches"] is True
    assert not list(tmp_path.glob("symphony-flash-probe-*"))


@pytest.mark.parametrize("body,code", [
    ('import time;time.sleep(60)', "flash_probe_timeout"),
    ('print("raw private error", flush=True)', "flash_probe_failed"),
    ('import sys;print("private-key", file=sys.stderr)', "flash_probe_no_init"),
    ('print("x" * 300000, flush=True)', "flash_probe_output_limit"),
    ('import time;time.sleep(60)', "flash_probe_cancelled"),
])
def test_failures_are_bounded_cleaned_and_redacted(tmp_path, monkeypatch, body, code):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    executable = fake_cli(tmp_path, body)
    with pytest.raises(flash.ProbeError) as caught:
        flash.probe(executable, timeout=.3 if code.endswith("timeout") else 3, temporary_root=tmp_path,
                    cancelled=(lambda: True) if code.endswith("cancelled") else None)
    assert str(caught.value) == code
    assert "private" not in str(caught.value)
    assert not list(tmp_path.glob("symphony-flash-probe-*"))


def test_environment_does_not_forward_credentials(monkeypatch):
    for key in ("DEEPSEEK_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "HTTP_PROXY", "AGY_API_KEY"):
        monkeypatch.setenv(key, "secret")
    assert "secret" not in flash._environment().values()
    assert flash._environment().get("HOME") == os.environ.get("HOME")
