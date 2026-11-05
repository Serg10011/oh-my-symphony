"""Read-only observation of two failures in the fixed public S4-62 trial."""
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import sysconfig
import time


def emit(name, value):
    print("DIAG_" + name + " " + json.dumps(value, sort_keys=True), flush=True)


def ps_control():
    script = Path("/tmp/pytest-of-tester/pytest-91/test_running_manual_script_is_0/boards/update-test/test.sh")
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/bash\nsleep 30\n")
    proc = subprocess.Popen(["/bin/bash", str(script)], start_new_session=True)
    try:
        # Wait for the command to enter its intended state; observe the exact same PID.
        for _ in range(50):
            if Path(f"/proc/{proc.pid}/task/{proc.pid}/children").read_text().strip():
                break
            time.sleep(.02)
        raw = Path(f"/proc/{proc.pid}/cmdline").read_bytes().split(b"\0")
        result = {"exact_arg_in_proc": os.fsencode(script) in raw, "pid_is_session_leader": os.getsid(proc.pid) == proc.pid}
        for name, flags in {"as_used": ["-axo"], "wide_same_selection": ["-axwwo"],
                            "all_wide": ["-eww", "-o"]}.items():
            out = subprocess.check_output(["ps", *flags, "pid=,args="], text=True)
            rows = [parts[1] for row in out.splitlines() if len(parts := row.split(None, 1)) == 2 and parts[0] == str(proc.pid)]
            result[name] = {"pid_present": bool(rows), "exact_path_present": any(str(script) in s for s in rows)}
        emit("PROCESS", result)
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


def observe_actual_process(args):
    import pytest

    def observe(frame, event, _arg):
        if event != "return" or frame.f_code.co_name != "ensure_stopped" or not frame.f_code.co_filename.endswith("/project_update.py"):
            return
        values = frame.f_locals
        proc = frame.f_back.f_locals.get("proc")
        if proc is None or "result" not in values:
            return
        pid = str(proc.pid)
        script = str(values["board"] / "test.sh")
        def entry(output):
            return next((cols[1] for row in output.splitlines() if len(cols := row.split(None, 1)) == 2 and cols[0] == pid), None)
        first = entry(values["result"].stdout)
        again = entry(subprocess.check_output(["ps", "-axo", "pid=,args="], text=True))
        wide = entry(subprocess.check_output(["ps", "-eww", "-o", "pid=,args="], text=True))
        actual = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        # All reported arguments are from this fixture's child, not other processes.
        summary = {"source_entry": first, "repeat_entry": again, "wide_entry": wide,
                   "expected_script": script, "target_in_proc": os.fsencode(script) in actual,
                   "target_in_source_args": script in shlex.split(first or ""),
                   "input_columns": os.environ.get("COLUMNS")}
        Path("/tmp/s462-process-result").write_text(json.dumps(summary))
    sys.setprofile(observe)
    try:
        return pytest.main(args)
    finally:
        sys.setprofile(None)


def board_control():
    # Observe only the original test's provider fixture subprocess, including
    # exceptions normally converted to safe public error codes by the controller.
    site = Path(sysconfig.get_paths()["purelib"])
    (site / "s462_observe.pth").write_text("import s462_observe\n")
    (site / "s462_observe.py").write_text("""import sys
from pathlib import Path
if Path(sys.argv[0]).name == 'offline provider fixture.py' and 'run' in sys.argv:
    def local(frame, event, arg):
        if event == 'exception' and str(arg[1]) == 'sandbox_platform_unsupported':
            with open('/tmp/s462-exact-reason', 'a') as sink:
                sink.write('sandbox_platform_unsupported\\n')
        return local
    def observe(frame, event, arg):
        if event == 'call' and frame.f_code.co_name == 'validate' and frame.f_code.co_filename.endswith('/api_sandbox.py'):
            return local
    sys.settrace(observe)
""")
    pytest = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=line", "--basetemp=/tmp/diagnostic-tests"]
    proc_id = "ours/tests/test_project_update.py::test_running_manual_script_is_detected"
    board_id = "ours/tests/test_api_board.py::test_core_bridge_and_real_controller_exchange_without_provider_access"
    for name, target in (("manual_script", proc_id), ("offline_controller", board_id)):
        command = pytest if name != "manual_script" else [sys.executable, __file__, "observe-process", *pytest[3:]]
        done = subprocess.run([*command, target], check=False)
        emit("TARGET", {"test": name, "exit": done.returncode})
    if (process_result := Path("/tmp/s462-process-result")).is_file():
        emit("ACTUAL", json.loads(process_result.read_text()))
    flag = Path("/tmp/s462-exact-reason")
    matched = flag.exists() and set(flag.read_text().splitlines()) == {"sandbox_platform_unsupported"}
    emit("BOARD", {"observed_child_error": "sandbox_platform_unsupported" if matched else None})
    subprocess.run(["git", "diff", "--exit-code", "HEAD", "--"], check=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["observe-process"]:
        sys.exit(observe_actual_process(sys.argv[2:]))
    ps_control()
    board_control()
