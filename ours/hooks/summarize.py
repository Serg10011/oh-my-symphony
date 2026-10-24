#!/usr/bin/env python3
"""Print the test.sh summary line from a JUnit XML report (TASK-K08b).

    summarize.py <junit.xml>

Prints `PIPELINE_TESTS passed=<n> skipped=<n> failed=<n>` and exits 0.
Every <testcase> anywhere in the tree counts once (nested <testsuite>
elements included): a <failure> or <error> child = failed, else a
<skipped> child = skipped, else passed. The suite-level counters
(`tests=`, `failures=`) are ignored: runners fill them differently.

A missing, empty or unparsable file, or a root other than <testsuites> /
<testsuite>, prints nothing to stdout and exits 1: test.sh must then fail
instead of reporting a summary nobody measured.

Standalone on purpose (standard library only, no imports from ours/): it
runs inside the agent's worktree.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET

ROOTS = ("testsuites", "testsuite")


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def summarize(path: str) -> dict[str, int]:
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from None
    if not data.strip():
        raise ValueError(f"{path} is empty")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"{path} is not XML: {exc}") from None
    if _local(root.tag) not in ROOTS:
        raise ValueError(f"{path}: root <{_local(root.tag)}> is not <testsuites>/<testsuite>")
    counts = {"passed": 0, "skipped": 0, "failed": 0}
    for case in root.iter():
        if _local(case.tag) != "testcase":
            continue
        kinds = {_local(child.tag) for child in case}
        if kinds & {"failure", "error"}:
            counts["failed"] += 1
        elif "skipped" in kinds:
            counts["skipped"] += 1
        else:
            counts["passed"] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: summarize.py <junit.xml>", file=sys.stderr)
        return 2
    try:
        counts = summarize(args[0])
    except ValueError as exc:
        print(f"summarize.py: {exc}", file=sys.stderr)
        return 1
    print("PIPELINE_TESTS " + " ".join(f"{k}={v}" for k, v in counts.items()), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
