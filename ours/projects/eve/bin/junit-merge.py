#!/usr/bin/env python3
"""JUnit helpers for the board's test.sh: restart of failed test files (E3c).

    junit-merge.py failed <first.xml>
        Print the failed test files of the first run, one per line (exit 0).
        A file is failed when one of its test cases has a <failure>/<error>
        child, or when its suite itself reports failures/errors and holds no
        test case at all (the file could not be loaded).
    junit-merge.py seconds <first.xml>
        Print the summed run time (whole seconds, rounded up) of the failed
        files of the first run: the estimate for their restart.
    junit-merge.py merge <first.xml> <retry.xml> <merged.xml>
        Write the combined report: the suites of the failed files of the first
        run are replaced by their suites from the restart; every other suite
        stays as it was, so `passed` is never counted twice. Prints one line
        `FLAKY: <file>` for a file that failed first and passed on the
        restart, and one line `FAILED AGAIN: <file>` for a file that failed
        again (or is missing from the restart report). Exit 0 when no file
        failed again, 1 otherwise.

Standalone on purpose (standard library only): runs from test.sh in the
agent's worktree. A missing, empty or unparsable report (the restart report
included) exits 2 with the reason on stderr: test.sh then treats the restart
as failed, never as passed.
"""

from __future__ import annotations

import math
import sys
import xml.etree.ElementTree as ET

ROOTS = ("testsuites", "testsuite")


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def load(path: str) -> ET.ElementTree:
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
    return ET.ElementTree(root)


def _file_suites(root: ET.Element) -> list[tuple[ET.Element, ET.Element]]:
    """(parent, suite) of every <testsuite> that stands for a test file.

    Vitest writes one <testsuite name="<file>"> per file under <testsuites>;
    a root that is itself a <testsuite> is a one-file report.
    """
    if _local(root.tag) == "testsuite":
        return [(root, root)]
    return [(root, child) for child in root if _local(child.tag) == "testsuite"]


def _suite_failed(suite: ET.Element) -> bool:
    cases = [c for c in suite.iter() if _local(c.tag) == "testcase"]
    for case in cases:
        if {_local(child.tag) for child in case} & {"failure", "error"}:
            return True
    if not cases:
        # A file that cannot be loaded: no cases, but the suite says so.
        for key in ("failures", "errors"):
            try:
                if int(suite.get(key, "0")) > 0:
                    return True
            except ValueError:
                return True
    return False


def failed_files(root: ET.Element) -> dict[str, ET.Element]:
    return {
        suite.get("name", ""): suite
        for _, suite in _file_suites(root)
        if suite.get("name") and _suite_failed(suite)
    }


def suite_seconds(suite: ET.Element) -> float:
    try:
        return max(0.0, float(suite.get("time", "0")))
    except ValueError:
        return 0.0


def merge(first: ET.ElementTree, retry: ET.ElementTree) -> tuple[list[str], list[str]]:
    """Replace the failed suites of `first` by their restart; return (flaky, failed again)."""
    first_root = first.getroot()
    failed = failed_files(first_root)
    retried = {
        suite.get("name", ""): suite for _, suite in _file_suites(retry.getroot()) if suite.get("name")
    }
    flaky: list[str] = []
    again: list[str] = []
    for name in sorted(failed):
        new = retried.get(name)
        if new is None or _suite_failed(new):
            again.append(name)
            if new is None:
                continue  # nothing to put in place: the first (failed) suite stays
        else:
            flaky.append(name)
        for parent, suite in _file_suites(first_root):
            if suite.get("name") == name:
                if suite is parent:
                    first._setroot(new)
                    first_root = new
                else:
                    parent.remove(suite)
                    parent.append(new)
                break
    return flaky, again


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    usage = (
        "usage: junit-merge.py failed <first.xml> | seconds <first.xml> | "
        "merge <first.xml> <retry.xml> <merged.xml>"
    )
    if not args:
        print(usage, file=sys.stderr)
        return 2
    command, rest = args[0], args[1:]
    try:
        if command in ("failed", "seconds") and len(rest) == 1:
            failed = failed_files(load(rest[0]).getroot())
            if command == "failed":
                for name in sorted(failed):
                    print(name)
            else:
                print(math.ceil(sum(suite_seconds(s) for s in failed.values())))
            return 0
        if command == "merge" and len(rest) == 3:
            first, retry = load(rest[0]), load(rest[1])
            flaky, again = merge(first, retry)
            first.write(rest[2], encoding="utf-8", xml_declaration=True)
            for name in flaky:
                print(f"FLAKY: {name}")
            for name in again:
                print(f"FAILED AGAIN: {name}")
            return 1 if again else 0
    except ValueError as exc:
        print(f"junit-merge.py: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"junit-merge.py: {exc}", file=sys.stderr)
        return 2
    print(usage, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
