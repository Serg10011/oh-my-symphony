"""Read Gemini subscription quota via the documented Antigravity status line.

Only a generated empty workspace is opened. No prompt is sent, no model runs,
and runtime settings do not replace the user's persistent preferences.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import pty
import re
import selectors
import shlex
import signal
import subprocess
import sys
import tempfile
import time

import api_flash_probe as flash

MAX_BYTES = 256 * 1024
BUCKETS = {"five_hour": "gemini-5h", "weekly": "gemini-weekly"}


def _bucket(value):
    if not isinstance(value, dict):
        return None
    result = {}
    fraction = value.get("remaining_fraction")
    if type(fraction) in (float, int) and 0 <= fraction <= 1 and math.isfinite(fraction):
        result["remaining_fraction"] = fraction
        result["remaining_percent"] = round(fraction * 100, 2)
    amount = value.get("remaining_amount")
    if type(amount) in (float, int) and 0 <= amount <= 10**18 and math.isfinite(amount):
        result["remaining_amount"] = amount  # Units are unspecified; never call these tasks.
    if type(value.get("disabled")) is bool:
        result["disabled"] = value["disabled"]
    reset = value.get("reset_time")
    if isinstance(reset, str) and len(reset) <= 64:
        try:
            parsed = datetime.fromisoformat(reset)
            if parsed.tzinfo is not None:
                result["reset_time"] = parsed.isoformat()
        except ValueError:
            pass
    if type(value.get("reset_in_seconds")) is int and value["reset_in_seconds"] >= 0:
        result["reset_in_seconds"] = value["reset_in_seconds"]
    return result if "remaining_fraction" in result or "remaining_amount" in result or result.get("disabled") else None


def normalize(value):
    if not isinstance(value, dict):
        raise flash.ProbeError("gemini_quota_invalid_payload")
    quotas = value.get("quota") if isinstance(value.get("quota"), dict) else {}
    buckets = {name: _bucket(quotas.get(key)) for name, key in BUCKETS.items()}
    result = {"source": "antigravity_status_line", "observed_at": datetime.now(timezone.utc).isoformat(),
              "buckets": buckets, "status": "available" if all(buckets.values()) else "partial" if any(buckets.values()) else "unavailable",
              "model_prompt_sent": False, "provider_refresh_verified": False}
    version = value.get("version")
    if isinstance(version, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        result["cli_version"] = version
    return result


def saved_snapshot(value):
    """Return only the observed quota fields from a private board snapshot."""
    if not isinstance(value, dict) or value.get("source") != "antigravity_status_line":
        raise flash.ProbeError("gemini_quota_invalid_snapshot")
    try:
        observed = datetime.fromisoformat(value["observed_at"])
        buckets = value["buckets"]
        if observed.tzinfo is None or not isinstance(buckets, dict):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise flash.ProbeError("gemini_quota_invalid_snapshot") from None
    snapshot = normalize({"version": value.get("cli_version"),
                          "quota": {key: buckets.get(name) for name, key in BUCKETS.items()}})
    snapshot["observed_at"] = observed.isoformat()
    return snapshot


def recent(snapshot, seconds, *, now=None):
    now = now if now is not None else datetime.now(timezone.utc)
    return bool(snapshot and 0 <= (now - datetime.fromisoformat(snapshot["observed_at"])).total_seconds() < seconds)


def blocked_until(snapshot, *, now=None):
    """Only a recent observation of exactly zero with a future reset blocks a run."""
    now = now if now is not None else datetime.now(timezone.utc)
    if not recent(snapshot, 60, now=now):
        return None
    resets = [datetime.fromisoformat(row["reset_time"]) for row in snapshot["buckets"].values()
              if row and row.get("remaining_fraction") == 0 and not row.get("disabled") and "reset_time" in row]
    return max((reset for reset in resets if reset > now), default=None)


def capture(path):
    raw = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise flash.ProbeError("gemini_quota_payload_too_large")
    try:
        value = normalize(json.loads(raw))
    except (ValueError, RecursionError):
        raise flash.ProbeError("gemini_quota_invalid_payload") from None
    path = Path(path)
    parent = path.parent.resolve(strict=True)
    if parent != path.parent or parent.stat().st_mode & 0o077 or parent.stat().st_uid != os.getuid():
        raise flash.ProbeError("gemini_quota_private_directory_required")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=parent, delete=False, mode="w") as file:
            temporary = Path(file.name)
            json.dump(value, file, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print("Symphony: сведения о квоте обновлены")


def read(*, executable=None, timeout=30, cancelled=None, temporary_root=None):
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise flash.ProbeError("gemini_quota_invalid_timeout")
    executable = flash.subscription_executable(executable)
    with tempfile.TemporaryDirectory(prefix="symphony-quota-", dir=temporary_root) as folder:
        directory = Path(folder).resolve()
        target = directory / "quota.json"
        command = shlex.join([str(Path(sys.executable).resolve()), "-B", str(Path(__file__).resolve()), "--capture", str(target)])
        settings = {**flash.SETTINGS, "trustedWorkspaces": [str(directory)],
                    "statusLine": {"enabled": True, "command": command}}
        argv = [str(executable), "--model", flash.MODEL, "--effort", "high", "--settings", json.dumps(settings)]
        return _read(argv, directory, target, timeout, cancelled)


def _read(argv, directory, target, timeout, cancelled):
    master, slave = pty.openpty()
    process = None
    selector = selectors.DefaultSelector()
    deadline, received = time.monotonic() + timeout, 0
    try:
        process = subprocess.Popen(argv, cwd=directory, env={**flash._environment(), "TERM": "xterm-256color"},
                                   stdin=slave, stdout=slave, stderr=slave, start_new_session=True, close_fds=True)
        os.close(slave)
        slave = None
        selector.register(master, selectors.EVENT_READ)
        while time.monotonic() < deadline:
            if cancelled is not None and cancelled():
                raise flash.ProbeError("gemini_quota_cancelled")
            for _, _ in selector.select(.1):
                try:
                    block = os.read(master, 65536)
                except OSError:
                    block = b""
                if not block:
                    raise flash.ProbeError("gemini_quota_cli_exited")
                received += len(block)
                if received > MAX_BYTES:
                    raise flash.ProbeError("gemini_quota_output_limit")
            if target.exists():
                value = json.loads(target.read_bytes())
                if value.get("status") == "available":
                    return value
        if target.exists():
            return json.loads(target.read_bytes())
        raise flash.ProbeError("gemini_quota_timeout")
    except (OSError, ValueError, RecursionError):
        raise flash.ProbeError("gemini_quota_read_failed") from None
    finally:
        selector.close()
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                if process.poll() is None:
                    process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise flash.ProbeError("gemini_quota_cleanup_unconfirmed") from None
        os.close(master)
        if slave is not None:
            os.close(slave)


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.capture:
            capture(args.capture)
            return 0
        result = read()
    except (flash.ProbeError, OSError) as exc:
        code = str(exc) if isinstance(exc, flash.ProbeError) else "gemini_quota_io_error"
        print(json.dumps({"status": "unavailable", "reason": code, "model_prompt_sent": False}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "available" else 2


if __name__ == "__main__":
    raise SystemExit(cli())
