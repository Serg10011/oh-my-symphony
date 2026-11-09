"""Quota snapshots are bounded metadata, never prompts or inferred task counts."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from test_api_flash_probe import fake_cli

import api_flash_probe as flash
import api_gemini_quota as quota


def payload(fraction=1):
    return {"email": "PRIVATE_EMAIL", "conversation_title": "PRIVATE_TASK", "version": "1.5.3",
            "quota": {name: {"remaining_fraction": fraction, "reset_time": "2026-11-15T21:20:52Z",
                             "reset_in_seconds": 100} for name in [*quota.BUCKETS.values(), "3p-weekly"]}}


@pytest.mark.parametrize("fraction,percent", [(1, 100), (.9378, 93.78), (0, 0)])
def test_remaining_quota_and_known_zero_are_preserved_without_private_fields(fraction, percent):
    value = quota.normalize(payload(fraction))
    assert value["status"] == "available" and value["buckets"]["weekly"]["remaining_percent"] == percent
    assert value["provider_refresh_verified"] is False and value["model_prompt_sent"] is False
    assert "PRIVATE" not in json.dumps(value) and "3p-weekly" not in json.dumps(value)


@pytest.mark.parametrize("invalid", [None, True, "1", -1, 1.1, float('nan'), float('inf'), 10**400])
def test_invalid_or_missing_quota_never_becomes_zero(invalid):
    value = quota.normalize(payload(invalid))
    assert value["status"] == "unavailable" and value["buckets"] == {"five_hour": None, "weekly": None}


def test_partial_and_disabled_buckets_and_unlabelled_amount():
    value = quota.normalize({"quota": {"gemini-weekly": {"remaining_amount": 7, "disabled": True}}})
    assert value["status"] == "partial" and value["buckets"]["five_hour"] is None
    assert value["buckets"]["weekly"] == {"remaining_amount": 7, "disabled": True}


def test_callback_persists_only_sanitized_owner_private_snapshot(tmp_path):
    output = tmp_path / "quota.json"
    result = subprocess.run([sys.executable, "-B", quota.__file__, "--capture", str(output)],
                            input=json.dumps(payload()), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert output.stat().st_mode & 0o777 == 0o600
    assert "PRIVATE" not in output.read_text() and "PRIVATE" not in result.stdout
    assert json.loads(output.read_text())["status"] == "available"


def test_real_terminal_transport_gets_metadata_without_input_or_persistent_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "private-key")
    executable = fake_cli(tmp_path, '''import json,os,select,shlex,subprocess,sys,time
assert "GEMINI_API_KEY" not in os.environ
assert sys.stdin.isatty() and not select.select([sys.stdin],[],[],.03)[0]
assert "--print" not in sys.argv and "--print=" not in sys.argv
settings=json.loads(sys.argv[sys.argv.index("--settings")+1])
assert settings["useG1Credits"] is False and settings["trustedWorkspaces"] == [os.getcwd()]
data={"quota":{k:{"remaining_fraction":1} for k in ["gemini-5h","gemini-weekly"]},"email":"PRIVATE"}
subprocess.run(shlex.split(settings["statusLine"]["command"]),input=json.dumps(data),text=True,check=True)
time.sleep(60)
''')
    result = quota.read(executable=executable, timeout=3, temporary_root=tmp_path)
    assert result["status"] == "available" and result["model_prompt_sent"] is False
    assert not list(tmp_path.glob("symphony-quota-*"))


def test_timeout_cleans_up_without_sending_slash_commands_as_a_model_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    executable = fake_cli(tmp_path, 'import time;time.sleep(60)')
    with pytest.raises(flash.ProbeError, match="gemini_quota_timeout"):
        quota.read(executable=executable, timeout=.2, temporary_root=tmp_path)
    assert not list(tmp_path.glob("symphony-quota-*"))
