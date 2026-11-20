"""Recognize standard successful check output without discarding diagnostics."""

import json
import re
import shlex

_PATH = r"[A-Za-z0-9_./][A-Za-z0-9_./-]*"
_NODE = re.compile(r'"\$SYMPHONY_NODE" --check (' + _PATH + r")")
_UNITTEST = re.compile(r"-m unittest(?: (discover(?: -s " + _PATH + r")?|" + _PATH + r"))?(?: -[qv])?")
_FOOTER = re.compile(r"^-{70}\nRan ([1-9]\d{0,4}) tests? in \d+\.\d{3}s\n\n"
                     r"OK(?: \(skipped=([1-9]\d{0,4})\))?\n", re.MULTILINE)
_PROGRESS = re.compile(r"[\w.]+ \([\w.]+\) \.\.\. (?:ok|skipped (.+))")
_MIN_SAVING_BYTES = 1024


def _check_commands(command, python):
    checks = []
    python_prefix = shlex.quote(python) + " "
    for part in command.split("&&"):
        part = part.strip(" \t")
        node = _NODE.fullmatch(part)
        if node:
            checks.append(("node", node[1]))
            continue
        unit = _UNITTEST.fullmatch(part.removeprefix(python_prefix)) if part.startswith(python_prefix) else None
        if unit is None:
            return []
        checks.append(("unittest", unit[1] or ""))
    return checks


def _unittest_results(log):
    results = []
    start = 0
    for footer in _FOOTER.finditer(log):
        total, skipped = int(footer[1]), int(footer[2] or 0)
        progress = log[start:footer.start()].strip("\n")
        reasons = set()
        if re.fullmatch(r"[.s]+", progress):
            progress_skips = progress.count("s")
            progress_total = len(progress)
        else:
            lines = progress.splitlines()
            progress_skips = 0
            for line in lines:
                match = _PROGRESS.fullmatch(line)
                if match is None:
                    return []
                if match[1] is not None:
                    progress_skips += 1
                    reasons.add(match[1])
            progress_total = len(lines)
        if (progress_total, progress_skips) != (total, skipped):
            return []
        result = {"passed": total - skipped, "skipped": skipped}
        if reasons:
            result["skip_reasons"] = sorted(reasons)
        results.append(result)
        start = footer.end()
    return results if start == len(log) else []


def summarize(command, python, outcome):
    log = outcome["output"]
    if (outcome["exit_code"] != 0 or outcome["stop_reason"] is not None
            or outcome.get("truncated") or outcome.get("excerpt") or len(log) < _MIN_SAVING_BYTES):
        return None
    commands = _check_commands(command, python)
    if not commands:
        return None
    results = _unittest_results(log)
    if len(results) != sum(kind == "unittest" for kind, _ in commands):
        return None
    checks = []
    results = iter(results)
    for kind, target in commands:
        if kind == "node":
            checks.append({"check": "node --check " + target})
        else:
            checks.append({"check": ("unittest " + target).rstrip(), **next(results)})
    summary = {key: value for key, value in outcome.items() if key != "output"}
    summary.update(checks=checks, output_saved=True)
    size = len(json.dumps(summary, ensure_ascii=False, separators=(",", ":")).encode())
    return summary if len(log.encode()) - size >= _MIN_SAVING_BYTES else None
