"""Read the latest hook/gate failures without changing a board."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

FIELDS = ("id", "at", "mode", "outcome", "reason")


class ReportError(Exception):
    """A board log cannot be safely interpreted."""


def _reject_constant(value: str) -> None:
    raise ValueError("non-JSON constant")


def read_failures(board: Path) -> list[dict[str, str]]:
    if not board.is_dir():
        raise ReportError("board path must be an existing directory")
    path = board / ".ours-state" / "check.log"
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return []

    failures: dict[str, dict[str, str]] = {}
    with handle:
        for number, raw in enumerate(handle, 1):
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise ReportError(f"line {number}: invalid UTF-8") from None
            if not line.strip():
                continue
            try:
                record = json.loads(line, parse_constant=_reject_constant)
            except (ValueError, RecursionError):
                raise ReportError(f"line {number}: invalid JSON") from None
            if not isinstance(record, dict):
                raise ReportError(f"line {number}: expected a JSON object")
            if record.get("mode") not in ("hook", "gate"):
                continue
            for field in FIELDS:
                if not isinstance(record.get(field), str):
                    raise ReportError(f"line {number}: {field} must be a string")
            issue_id, outcome = record["id"], record["outcome"]
            if outcome == "pass":
                failures.pop(issue_id, None)
            elif outcome in ("fail", "error") or outcome.startswith("rollback:"):
                failures[issue_id] = {field: record[field] for field in FIELDS}
    return [failures[issue_id] for issue_id in sorted(failures)]


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("board_path", type=Path)
    args = parser.parse_args(argv)
    try:
        failures = read_failures(args.board_path.expanduser())
    except ReportError as exc:
        print(f"check-report: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"check-report: cannot read board log ({exc.strerror})", file=sys.stderr)
        return 1
    print(json.dumps({"failures": failures}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(run())
