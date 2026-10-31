"""E3c regression checks with real subprocesses and disposable board files.

No agent, EVE checkout, Node installation or running board is used here.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

OURS = Path(__file__).resolve().parents[1]
KIT = OURS / "projects" / "eve"
MERGE = KIT / "bin" / "junit-merge.py"
LOCK = KIT / "bin" / "test-lock.py"
SUMMARIZE = OURS / "hooks" / "summarize.py"


def run_python(script, *args):
    return subprocess.run(
        [sys.executable, str(script), *map(str, args)],
        text=True, capture_output=True, timeout=15,
    )


def suite(name="retry.test.ts", outcome="pass"):
    child = {"pass": "", "fail": "<failure/>", "error": "<error/>"}[outcome]
    return f'<testsuite name="{name}"><testcase name="case">{child}</testcase></testsuite>'


@pytest.mark.parametrize("first_wrapped", [False, True])
@pytest.mark.parametrize("retry_wrapped", [False, True])
@pytest.mark.parametrize("namespace", ["", "urn:junit"])
def test_green_retry_replaces_failed_suite_and_agrees_with_gate_summary(
    tmp_path, first_wrapped, retry_wrapped, namespace,
):
    def report(text, wrapped):
        text = f"<testsuites>{text}</testsuites>" if wrapped else text
        if namespace:
            text = text.replace(">", f' xmlns="{namespace}">', 1)
        return text

    first, retry, merged = (tmp_path / n for n in ("first.xml", "retry.xml", "merged.xml"))
    first.write_text(report(suite(outcome="fail"), first_wrapped))
    retry.write_text(report(suite(), retry_wrapped))
    result = run_python(MERGE, "merge", first, retry, merged)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "FLAKY: retry.test.ts\n"
    summary = run_python(SUMMARIZE, merged)
    assert summary.returncode == 0, summary.stderr
    assert summary.stdout.strip() == "PIPELINE_TESTS passed=1 skipped=0 failed=0"


@pytest.mark.parametrize("outcome", ["fail", "error", "missing"])
@pytest.mark.parametrize("wrapped", [False, True])
def test_red_or_missing_retry_stays_red(tmp_path, outcome, wrapped):
    first, retry, merged = (tmp_path / n for n in ("first.xml", "retry.xml", "merged.xml"))
    original = suite(outcome="fail")
    first.write_text(f"<testsuites>{original}</testsuites>" if wrapped else original)
    retry.write_text(suite("another.test.ts") if outcome == "missing" else suite(outcome=outcome))
    result = run_python(MERGE, "merge", first, retry, merged)
    assert result.returncode == 1, result.stderr
    assert result.stdout == "FAILED AGAIN: retry.test.ts\n"
    assert run_python(SUMMARIZE, merged).stdout.strip() == "PIPELINE_TESTS passed=0 skipped=0 failed=1"


def test_retry_does_not_double_count_other_files(tmp_path):
    first, retry, merged = (tmp_path / n for n in ("first.xml", "retry.xml", "merged.xml"))
    first.write_text(f'<testsuites>{suite("keep.test.ts")}{suite(outcome="fail")}</testsuites>')
    retry.write_text(f'<testsuites>{suite("keep.test.ts")}{suite()}</testsuites>')
    assert run_python(MERGE, "merge", first, retry, merged).returncode == 0
    assert run_python(SUMMARIZE, merged).stdout.strip() == "PIPELINE_TESTS passed=2 skipped=0 failed=0"


@pytest.mark.parametrize("retry_text", [None, "", "<broken>", "<wrong/>"])
def test_unusable_retry_is_not_success(tmp_path, retry_text):
    first, retry, merged = (tmp_path / n for n in ("first.xml", "retry.xml", "merged.xml"))
    first.write_text(suite(outcome="fail"))
    if retry_text is not None:
        retry.write_text(retry_text)
    result = run_python(MERGE, "merge", first, retry, merged)
    assert result.returncode == 2
    assert "FLAKY" not in result.stdout
    assert not merged.exists()


def try_lock(path, *, wait=0, poll=0.02):
    with path.open("a") as handle:
        return subprocess.run(
            [sys.executable, str(LOCK), "--fd", str(handle.fileno()),
             "--deadline", str(time.time() + wait), "--poll", str(poll)],
            pass_fds=(handle.fileno(),), capture_output=True, text=True, timeout=5,
        )


def wait_for_file(path, proc):
    deadline = time.monotonic() + 5
    while not path.exists():
        assert proc.poll() is None, proc.communicate()
        assert time.monotonic() < deadline, "holder did not become ready"
        time.sleep(0.01)


@contextmanager
def holding_shell(tmp_path):
    path, ready = tmp_path / "test.lock", tmp_path / "ready"
    script = '''exec 9>>"$1"
"$2" "$3" --fd 9 --deadline "$4" --poll 0.01 || exit
"$2" -c 'import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)' "$5"
status=$?
exit "$status"
'''
    proc = subprocess.Popen(
        ["/bin/bash", "-c", script, "holder", str(path), sys.executable,
         str(LOCK), str(time.time() + 5), str(ready)],
        start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        wait_for_file(ready, proc)
        yield proc, path, int(ready.read_text())
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate(timeout=5)


def test_wait_respects_budget_even_when_poll_interval_is_long(tmp_path):
    with holding_shell(tmp_path) as (_, path, _child):
        started = time.monotonic()
        result = try_lock(path, wait=0.15, poll=3)
        assert result.returncode == 75
        assert "ENV: test lock busy" in result.stderr
        assert time.monotonic() - started < 2
    assert try_lock(path).returncode == 0


def test_killing_shell_does_not_release_its_running_child_lock(tmp_path):
    with holding_shell(tmp_path) as (proc, path, child):
        inode = path.stat().st_ino
        # The acquisition helper already exited; the shell/child still own FD 9.
        assert try_lock(path).returncode == 75
        proc.kill()
        proc.wait(timeout=5)
        os.kill(child, 0)
        assert try_lock(path).returncode == 75
    assert try_lock(path).returncode == 0
    assert path.stat().st_ino == inode


def test_concurrent_processes_keep_one_owner_without_removing_stale_metadata(tmp_path):
    path = tmp_path / "test.lock"
    path.write_text("old PID metadata is not authority to remove the lock\n")
    inode = path.stat().st_ino
    critical = tmp_path / "critical.py"
    critical.write_text('''import os, sys, time
from pathlib import Path
root = Path(sys.argv[1])
guard = root / "active"
fd = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
time.sleep(0.04)
os.close(fd)
guard.unlink()
(root / sys.argv[2]).touch()
''')
    script = '''exec 9>>"$1"
"$2" "$3" --fd 9 --deadline "$4" --poll 0.01 || exit
"$2" "$5" "$6" "$7"
'''
    workers = [subprocess.Popen(
        ["/bin/bash", "-c", script, "contender", str(path), sys.executable,
         str(LOCK), str(time.time() + 10), str(critical), str(tmp_path), f"done-{i}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) for i in range(6)]
    try:
        for proc in workers:
            out, err = proc.communicate(timeout=15)
            assert proc.returncode == 0, (out, err)
    finally:
        for proc in workers:
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=5)
    assert len(list(tmp_path.glob("done-*"))) == 6
    assert path.stat().st_ino == inode
    assert "old PID" in path.read_text()


@pytest.mark.parametrize("poll", [0, -1, "nan", "inf"])
def test_invalid_poll_cannot_create_unbounded_wait(tmp_path, poll):
    result = try_lock(tmp_path / "test.lock", poll=poll)
    assert result.returncode == 2


@pytest.fixture
def fake_board(tmp_path):
    """Run the complete wrapper with fake Node/typecheck, real lock and JUnit tools."""
    import shutil

    board, node, work, locks = (tmp_path / n for n in ("board", "node", "work", "locks"))
    shutil.copytree(KIT, board)
    for path in (node, work / "tests" / "helpers", locks, board / ".ours-state"):
        path.mkdir(parents=True, exist_ok=True)
    script = (board / "test.sh").read_text()
    script = script.replace(
        "NODE_BIN=/Users/helloworld10011/.local/node/v24.22.1/bin", f'NODE_BIN="{node}"',
    ).replace(
        "LOCK_ROOT=/Users/helloworld10011/symphony-projects/.locks", f'LOCK_ROOT="{locks}"',
    ).replace(
        "SUMMARIZE=/Users/helloworld10011/dev/oh-my-symphony/ours/hooks/summarize.py", f'SUMMARIZE="{SUMMARIZE}"',
    )
    (board / "test.sh").write_text(script)
    (board / "project.toml").write_text("[timeouts]\ntests_s = 720\n")
    (board / ".ours-state" / "baseline.json").write_text('{"tests_s": 1}')
    (board / "bin" / "typecheck").write_text("exit 0\n")
    (work / "package.json").write_text("{}")
    (work / "tests" / "helpers" / "history-store-local-connections.mjs").touch()
    (node / "node").write_text("#!/bin/bash\necho v24.22.1\n")
    (node / "npm").write_text(f"#!{sys.executable}\n" + '''import fcntl, os, sys
from pathlib import Path
with open(os.environ["FAKE_LOCK"], "a") as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pass
    else:
        raise RuntimeError("wrapper released the lock before npm")
counter = Path("calls")
counter.write_text(counter.read_text() + "call\\n" if counter.exists() else "call\\n")
report = Path(next(x.split("=", 1)[1] for x in sys.argv if x.startswith("--outputFile.junit=")))
retry = report.name == "junit-retry.xml"
mode = os.environ.get("FAKE_RETRY", "pass")
if retry and mode == "missing":
    sys.exit(1)
failed = not retry or mode == "fail"
case = "<failure/>" if failed else ""
report.write_text('<testsuite name="retry.test.ts"><testcase name="flaky">' + case + '</testcase></testsuite>')
sys.exit(1 if failed else 0)
''')
    for name in ("node", "npm"):
        (node / name).chmod(0o755)
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "SYMPHONY_PYTHON": sys.executable,
        "FAKE_LOCK": str(locks / "test.lock"),
    }
    return board, work, locks, env


@pytest.mark.parametrize("retry", ["pass", "fail", "missing"])
def test_complete_wrapper_holds_lock_across_first_run_and_retry(fake_board, retry):
    board, work, locks, env = fake_board
    env["FAKE_RETRY"] = retry
    result = subprocess.run(
        ["/bin/bash", str(board / "test.sh")], cwd=work, env=env,
        text=True, capture_output=True, timeout=20,
    )
    assert (work / "calls").read_text().splitlines() == ["call", "call"], result.stderr
    assert result.returncode == (0 if retry == "pass" else 1), (result.stdout, result.stderr)
    expected = "passed=1 skipped=0 failed=0" if retry == "pass" else "passed=0 skipped=0 failed=1"
    assert result.stdout.strip().endswith("PIPELINE_TESTS " + expected)
    assert locks.joinpath("test.lock").is_file()
    assert try_lock(locks / "test.lock").returncode == 0
    if retry == "pass":
        assert "FLAKY: retry.test.ts" in (board / "log" / "flaky.log").read_text()


def test_wrapper_refuses_legacy_directory_without_removing_it(fake_board):
    board, work, locks, env = fake_board
    legacy = locks / "test.lock"
    legacy.mkdir()
    (legacy / "pid").write_text("obsolete-pid")
    result = subprocess.run(
        ["/bin/bash", str(board / "test.sh")], cwd=work, env=env,
        text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == 75, (result.stdout, result.stderr)
    assert "ENV: test lock busy" in result.stderr
    assert (legacy / "pid").read_text() == "obsolete-pid"
    assert not (work / "calls").exists()
