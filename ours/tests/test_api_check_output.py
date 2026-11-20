"""Sol gets compact check evidence while the session retains the original log."""

import hashlib
import json
import shlex

import pytest

from test_api_session import KEYS, exchange, frame
from test_api_workspace import portable_offline_policy as portable_offline_policy, prepared, tool

import api_check_output
import api_executor as api
import api_session as session
import api_workspace as workspace


def unittest_log(count=36, skipped=1):
    progress = "".join(f"test_case_{i} (test_module.Checks.test_case_{i}) ... ok\n"
                       for i in range(count - skipped))
    progress += "test_skipped (test_module.Checks.test_skipped) ... skipped 'optional fixture'\n" * skipped
    footer = f"OK (skipped={skipped})" if skipped else "OK"
    return f"{progress}\n{'-' * 70}\nRan {count} tests in 0.016s\n\n{footer}\n"


def outcome(log, **overrides):
    return {
        "exit_code": 0, "stop_reason": None, "output": log, "output_bytes": len(log.encode()),
        "output_sha256": hashlib.sha256(log.encode()).hexdigest(), "truncated": False,
        "seconds": 0.016,
    } | overrides


def test_multiple_standard_checks_have_distinct_counts_and_keep_skip_reasons():
    python = "/runtime/bin/python3"
    command = (f'"$SYMPHONY_NODE" --check src/panel.js && {python} -m unittest tests.unit -v'
               f" && {python} -m unittest discover -s tests -q")
    quiet = f".ss\n{'-' * 70}\nRan 3 tests in 0.002s\n\nOK (skipped=2)\n"
    log = outcome(unittest_log() + quiet, revision="a" * 64)
    summary = api_check_output.summarize(command, python, log)
    assert summary["checks"] == [
        {"check": "node --check src/panel.js"},
        {"check": "unittest tests.unit", "passed": 35, "skipped": 1, "skip_reasons": ["'optional fixture'"]},
        {"check": "unittest discover -s tests", "passed": 1, "skipped": 2},
    ]
    assert summary["revision"] == log["revision"]
    assert summary["output_saved"] and "output" not in summary
    assert summary["output_sha256"] == log["output_sha256"]


@pytest.mark.parametrize(("args", "target"), [
    ("-B -m unittest discover -v -s tests/api_team -t .", "discover -s tests/api_team -t ."),
    ("-m unittest discover -t . -s tests/api_team -v", "discover -s tests/api_team -t ."),
    ("-m unittest discover --top-level-directory . --verbose --start-directory tests",
     "discover -s tests -t ."),
    ("-B -m unittest -v tests.api_team.test_lead_effort", "tests.api_team.test_lead_effort"),
    ("-B -m unittest -v", ""),
])
def test_unittest_flags_keep_the_test_selection(args, target):
    python = "/runtime with space/bin/python3"
    command = f"{shlex.quote(python)} {args}"
    summary = api_check_output.summarize(command, python, outcome(unittest_log()))
    assert summary["checks"] == [
        {"check": ("unittest " + target).rstrip(), "passed": 35, "skipped": 1,
         "skip_reasons": ["'optional fixture'"]},
    ]


@pytest.mark.parametrize("args", [
    "-X dev -m unittest discover -v -s tests -t .",
    "-m unittest discover -t . --top-level-directory src",
    "-m unittest discover -s -v tests -t .",
    "-m unittest discover -s tests -t",
    "-m unittest discover -v --quiet -s tests -t .",
    "-m unittest discover -s tests/* -t .",
    "-m unittest discover -s tests -t . | cat",
    "-m unittest discover -s tests -t .; echo extra",
    "-m unittest discover -s tests -t .\necho extra",
    "-m unittest -v discover -s tests -t .",
    "-m unittest -v tests.unit -q",
])
def test_ambiguous_flags_and_shell_syntax_keep_the_log(args):
    python = "/runtime/bin/python3"
    assert api_check_output.summarize(f"{python} {args}", python, outcome(unittest_log())) is None


def test_discovery_with_python_flags_keeps_reported_diagnostics():
    python = "/runtime/bin/python3"
    command = f"{python} -B -m unittest discover -v -s tests/api_team -t ."
    log = outcome("DeprecationWarning: optional fixture\n" + unittest_log())
    assert api_check_output.summarize(command, python, log) is None


@pytest.mark.parametrize("reason", [
    "failure", "timeout", "truncated", "excerpt", "warning", "wrong_count",
    "unexpected_footer", "non_test", "mixed_command", "missing_batch_result", "short",
])
def test_only_recognized_successful_check_output_is_compacted(reason):
    python = "/runtime/bin/python3"
    command = f"{python} -m unittest tests.unit -v"
    output = outcome(unittest_log())
    if reason == "failure":
        output["exit_code"] = 1
    elif reason == "timeout":
        output["stop_reason"] = "timeout"
    elif reason in {"truncated", "excerpt"}:
        output[reason] = True
    elif reason == "warning":
        output["output"] += "Warning: fixture was read from a fallback directory.\n"
    elif reason == "wrong_count":
        output["output"] = output["output"].replace("Ran 36 tests", "Ran 37 tests")
    elif reason == "unexpected_footer":
        output["output"] = output["output"].replace("OK (skipped=1)", "OK (skipped=1, expected failures=1)")
    elif reason == "non_test":
        command = "cat run.log"
    elif reason == "mixed_command":
        command += " && git status"
    elif reason == "missing_batch_result":
        command += f" && {command}"
    else:
        output = outcome(unittest_log(count=3, skipped=0))
    assert api_check_output.summarize(command, python, output) is None


@pytest.mark.parametrize(("worker", "version"), [
    (None, 3), (api.FLASH_WORKER, 8), (api.FLASH_WORKER, 9), (None, 10),
])
def test_dispatch_preserves_saved_policies_failures_and_moved_revisions(
    tmp_path, portable_offline_policy, monkeypatch, worker, version,
):
    if version in (3, 8):
        monkeypatch.setattr(session, "_new_role_policy", lambda _spec: version)
    if worker:
        monkeypatch.setattr(session.flash_probe, "check_readiness", lambda *_args, **_options: None)
    path, _ = prepared(tmp_path, lead_profile=api.SOL_62_LEAD, worker_profile=worker)
    saved = session._load(path)
    assert saved["role_policy_version"] == version
    python = saved["spec"]["execution"]["python"]
    targets = ("passed", "failed", "mutated")
    commands = {target: f"{shlex.quote(python)} -m unittest tests.{target} -v" for target in targets}
    passed = unittest_log()
    failed = "AssertionError: 3 is not 4\n"

    def run_check(policy, command, **_options):
        if command == commands["mutated"]:
            (policy.workspace / "src/main.py").write_text("answer = 42\n")
        if command == commands["failed"]:
            return outcome(failed, exit_code=1)
        return outcome(passed)

    monkeypatch.setattr(workspace, "preflight", lambda *_args: {"fixture": True})
    monkeypatch.setattr(workspace.sandbox, "run", run_check)
    calls = [tool("run_project_command", f"call_{target}", command=commands[target], timeout_seconds=2)
             for target in targets]
    transport, sent = exchange([
        frame(None, calls, model=api.SOL_62_MODEL),
        frame("Проверено на копии.", model=api.SOL_62_MODEL),
    ])
    assert session.run(path, environ=KEYS, transport=transport)["status"] == "dialogue_complete"
    initial = sent[0]["body"]
    assert any(value["name"] == "delegate_worker" for value in initial["tools"]) == bool(worker)
    instructions = initial["input"][0]["content"]
    assert (session._CHECK_HANDOFF in instructions) == bool(worker)
    assert (session._SOLO_LEAD_ROLES in instructions) == (version == 10)
    assert (session._CHECK_SUMMARY in instructions) == (version in (9, 10))
    outputs = {
        row["call_id"]: json.loads(row["output"][0]["text"])
        for row in sent[-1]["body"]["input"] if row.get("type") == "function_call_output"
    }
    saved = session._load(path)
    checks = outputs["call_passed"]
    assert outputs["call_failed"]["output"] == failed
    assert outputs["call_mutated"]["output"] == passed
    assert "checks" not in outputs["call_mutated"]
    if version in (9, 10):
        assert checks["checks"][0]["passed"] == 35
        assert checks["revision"] != outputs["call_mutated"]["revision"]
        assert "output" not in checks
        assert saved["file_calls"][0]["command_output"] == passed
    else:
        assert checks["output"] == passed
        assert "command_output" not in saved["file_calls"][0]
