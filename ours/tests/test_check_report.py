"""Exercise the read-only report through its public command."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
sys.path.insert(0, str(HOOKS))
import check_report  # noqa: E402

CLI = HOOKS.parent / "bin" / "check-report"


def event(issue_id="OMS-1", outcome="fail", mode="hook", **extra):
    return {"id": issue_id, "at": "2026-11-01T12:00:00Z", "mode": mode,
            "outcome": outcome, "reason": "Проверка не прошла", **extra}


def command(*args):
    return subprocess.run([sys.executable, str(CLI), *map(str, args)],
                          capture_output=True, text=True, encoding="utf-8", timeout=10)


def write_log(board, records):
    path = board / ".ours-state" / "check.log"
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
                    encoding="utf-8")
    return path


def test_latest_significant_record_and_order(tmp_path):
    last = event("OMS-2", "rollback:In Progress", "gate", at="1999-01-01")
    first = event("OMS-10", "error", "gate", extra="private")
    write_log(tmp_path, [event("OMS-1"), event("OMS-1", "pass", "gate"),
                         event("OMS-2", "error"), first, last,
                         event("OMS-2", "skipped"), event("OMS-2", "baseline_unknown"),
                         event("OMS-2", "pass", "deliver"),
                         {"mode": "integrate", "anything": [1, 2]}, {}])
    result = command(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout) == {"failures": [
        {k: first[k] for k in check_report.FIELDS}, last]}
    assert "Проверка не прошла" in result.stdout
    assert "private" not in result.stdout


@pytest.mark.parametrize("outcome", ["fail", "error", "rollback:", "rollback:needs_human"])
def test_failure_outcomes(tmp_path, outcome):
    record = event(outcome=outcome)
    write_log(tmp_path, [record])
    result = command(tmp_path)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"failures": [record]}


@pytest.mark.parametrize("history", [[], [event(outcome="pass")],
                                     [event(), event(outcome="pass")]])
def test_no_current_failures(tmp_path, history):
    write_log(tmp_path, history)
    result = command(tmp_path)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"failures": []}


def test_no_log_does_not_create_state(tmp_path):
    result = command(tmp_path)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"failures": []}
    assert list(tmp_path.iterdir()) == []


def test_read_only_and_blank_lines(tmp_path):
    path = write_log(tmp_path, [event()])
    path.write_bytes(b"\n \t\n" + path.read_bytes() + b"\n")
    (tmp_path / "sentinel").write_text("unchanged")
    before = {p.relative_to(tmp_path): (p.read_bytes(), p.stat().st_mtime_ns)
              for p in tmp_path.rglob("*") if p.is_file()}
    assert command(tmp_path).returncode == 0
    after = {p.relative_to(tmp_path): (p.read_bytes(), p.stat().st_mtime_ns)
             for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


@pytest.mark.parametrize("raw", [b"secret{", b"\xffsecret", b"[1]", b"null", b"7",
                                 b'{"mode":"other","x":NaN}'])
def test_corrupt_log_is_not_disclosed(tmp_path, raw):
    path = write_log(tmp_path, [event()])
    with path.open("ab") as handle:
        handle.write(b"\n" + raw + b"\n")
    result = command(tmp_path)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "line 3" in result.stderr
    assert "secret" not in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("field", ["id", "at", "outcome", "reason"])
@pytest.mark.parametrize("bad_value", [None, 42, [], {}])
def test_hook_schema_errors(tmp_path, field, bad_value):
    record = event()
    record[field] = bad_value
    write_log(tmp_path, [record])
    result = command(tmp_path)
    assert result.returncode == 1
    assert f"line 1: {field} must be a string" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("field", ["id", "at", "outcome", "reason"])
def test_missing_fields_are_rejected_even_for_ignored_outcome(tmp_path, field):
    record = event(outcome="skipped", mode="gate")
    del record[field]
    write_log(tmp_path, [record])
    result = command(tmp_path)
    assert result.returncode == 1
    assert field in result.stderr


@pytest.mark.parametrize("mode", [None, [], {}, 4, "deliver"])
def test_other_modes_do_not_require_hook_fields(tmp_path, mode):
    write_log(tmp_path, [{"mode": mode}])
    result = command(tmp_path)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"failures": []}


def test_invalid_board_and_unreadable_log(tmp_path):
    missing = command(tmp_path / "missing")
    assert missing.returncode == 1
    file_board = tmp_path / "file"
    file_board.touch()
    assert command(file_board).returncode == 1
    path = write_log(tmp_path, [])
    path.unlink()
    path.mkdir()
    unreadable = command(tmp_path)
    assert unreadable.returncode == 1
    assert "cannot read" in unreadable.stderr
    assert "Traceback" not in unreadable.stderr


def test_read_error(monkeypatch, tmp_path, capsys):
    def denied(*args, **kwargs):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(Path, "open", denied)
    assert check_report.run([str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "Permission denied" in output.err
    assert "Traceback" not in output.err


@pytest.mark.parametrize("args", [[], ["one", "two"]])
def test_argument_errors(args):
    result = command(*args)
    assert result.returncode == 2
    assert "usage:" in result.stderr
