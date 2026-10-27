"""Tests of ours/bin/inbox-runner (TASK-S4-02).

External world (subprocesses, osascript, gh, post-merge) is substituted through
`Ops`; real git is used only on temporary repositories. Scripts of ours/ are
started through sys.executable, never through the shebang.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

OURS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OURS / "hooks"))

import inbox_runner as ir  # noqa: E402

LAUNCHD = OURS / "bin" / "inbox-runner-launchd"
RUNNER = OURS / "bin" / "inbox-runner"
OLD = 100.0  # mtime far in the past; the fake clock says "now" = 1000
NOW = 1000.0


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def identify(repo: Path) -> None:
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "t")


class Fake:
    """Records every external call; `rules` decide the answer by a part of the command line."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.windows: list[tuple[str, str]] = []
        self.rules: list[tuple[str, ir.Proc]] = []
        self.window_ok = True
        self.hash = "h1"
        self.on_call = None  # callable(argv) run at call time
        self.branch_rc = 1  # `show-ref` answer: 1 = branch absent

    def on(self, fragment: str, rc: int = 0, out: str = "", timed_out: bool = False) -> None:
        self.rules.insert(0, (fragment, ir.Proc(rc, out, timed_out)))

    def proc(self, argv, cwd=None, env=None, timeout=120, stdin_text=None) -> ir.Proc:
        line = " ".join(str(a) for a in argv)
        self.calls.append({"argv": [str(a) for a in argv], "line": line, "cwd": cwd, "env": env, "timeout": timeout})
        if self.on_call:
            self.on_call(argv)
        for fragment, answer in self.rules:
            if fragment in line:
                return answer
        if "show-ref" in line:
            return ir.Proc(self.branch_rc)
        return ir.Proc(0, "")

    def terminal(self, osascript: str, command: str) -> tuple[bool, str]:
        self.windows.append((osascript, command))
        if self.on_call:
            self.on_call(["terminal", command])
        return self.window_ok, "" if self.window_ok else "не вышло"

    def ops(self) -> ir.Ops:
        return ir.Ops(proc=self.proc, terminal=self.terminal, code_hash=lambda: self.hash, clock=lambda: NOW,
                      sleep=lambda s: None)

    def lines(self, fragment: str) -> list[str]:
        return [c["line"] for c in self.calls if fragment in c["line"]]


@pytest.fixture(autouse=True)
def _reset_stop():
    ir.STOP.clear()
    yield
    ir.STOP.clear()


class Env:
    def __init__(self, root: Path) -> None:
        self.main = root / "main"
        self.wroot = root / "wt"
        self.main.mkdir()
        (self.main / ".git").mkdir()  # enough for the fake-ops tests
        self.gh = root / "bin" / "gh"
        self.gh.parent.mkdir()
        self.gh.write_text("#!/bin/sh\n")
        self.fake = Fake()
        self.settings = ir.Settings(self.main, self.wroot, str(self.gh), "/usr/bin/osascript", 0.01)
        self.inbox = self.main / "tmp_workspaces" / "inbox"
        self.outbox = self.main / "tmp_workspaces" / "outbox"
        self.inbox.mkdir(parents=True)

    def put(self, name: str, text: str | bytes = "", age: float = NOW - OLD) -> Path:
        path = self.inbox / name
        path.write_bytes(text.encode() if isinstance(text, str) else text)
        os.utime(path, (NOW - age, NOW - age))
        return path

    def task(self, code: str) -> None:
        self.put(f"{code}.md", "task")

    def runner(self) -> ir.Runner:
        return ir.Runner(self.settings, self.fake.ops())

    def pass_(self) -> bool:
        return self.runner().run_pass()

    def log(self, name: str) -> str:
        return (self.outbox / name).read_text()

    def skipped(self) -> str:
        path = self.outbox / "_skipped.log"
        return path.read_text() if path.exists() else ""

    def done(self) -> list[str]:
        return sorted(p.name for p in (self.inbox / "done").iterdir()) if (self.inbox / "done").exists() else []


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


# --- pure validation ----------------------------------------------------------


@pytest.mark.parametrize("name", [
    "S4-02", "a", "A" * 31, "0abc", "x-y-z",
])
def test_code_accepted(name: str) -> None:
    assert ir.valid_code(name)


@pytest.mark.parametrize("name", [
    "", "a;b", "a b", "$(id)", "a..b", "a.b", "Задача", "A" * 32, "-x", "a\n", "a/b", "a`b`", "a|b",
])
def test_code_rejected(name: str) -> None:
    assert not ir.valid_code(name)


@pytest.mark.parametrize("branch", ["dev/S4-03", "feature/x.y", "a" * 81])
def test_branch_accepted(branch: str) -> None:
    assert ir.valid_branch(branch)


@pytest.mark.parametrize("branch", [
    "our-main", "main", "--admin", "a b", "x;y", "-x", "a..b", "a//b", "dev/", "x.lock", "dev/x.lock", "x.",
    "dev/.hidden", "a\n", "", "a" * 82, "ветка", "$(id)",
])
def test_branch_rejected(branch: str) -> None:
    assert not ir.valid_branch(branch)


@pytest.mark.parametrize("name", ["S4-02.RUN", "S4-02.Test", "S4-02.Merge", "S4-02.sh", "S4-02", "S4-02.run.bak",
                                  ".run", "a b.run", "a;b.run", "a..b.run", "$(id).run", "Задача.run",
                                  ("A" * 32) + ".run"])
def test_bad_names(name: str) -> None:
    assert ir.split_name(name) is None


def test_good_names() -> None:
    assert ir.split_name("S4-02.run") == ("S4-02", "run")
    assert ir.split_name("S4-02.test") == ("S4-02", "test")
    assert ir.split_name("S4-02.merge") == ("S4-02", "merge")


def test_abs_path_rules() -> None:
    assert ir.valid_abs_path("/Users/x/dev/oms-dev")
    for bad in ("rel/path", "/a b", "/a;b", "/a/../b", "/a$b", "/a'b", "/a\nb", "/"):
        assert not ir.valid_abs_path(bad), bad


# --- .run: command building ------------------------------------------------------


def test_terminal_command_exact() -> None:
    cmd = ir.terminal_command(Path("/m/main"), Path("/m/wt"), "S4-03", "sonnet")
    assert cmd == 'cd /m/wt/S4-03 && claude --model sonnet "Выполни задание из файла /m/main/tmp_workspaces/inbox/S4-03.md"'


def test_terminal_command_rejects_unvalidated_parts() -> None:
    for code, model in (("a;b", "sonnet"), ("S4", "sonnet;rm -rf ~"), ("S4", "gpt4")):
        with pytest.raises(AssertionError):
            ir.terminal_command(Path("/m/main"), Path("/m/wt"), code, model)
    with pytest.raises(AssertionError):
        ir.terminal_command(Path("/m/ma in"), Path("/m/wt"), "S4", "opus")


def test_run_default_model_order_and_window(env: Env) -> None:
    env.task("S4-03")
    env.put("S4-03.run")
    assert env.pass_() is False
    f = env.fake
    lines = [c["line"] for c in f.calls]
    assert "show-ref --verify --quiet refs/heads/dev/S4-03" in lines[0]
    assert lines[1].endswith("fetch origin our-main")
    assert lines[2].endswith(f"worktree add --no-track -b dev/S4-03 {env.wroot}/S4-03 origin/our-main")
    assert len(f.calls) == 3 and len(f.windows) == 1
    osa, command = f.windows[0]
    assert osa == "/usr/bin/osascript"
    assert command == (f'cd {env.wroot}/S4-03 && claude --model sonnet '
                       f'"Выполни задание из файла {env.main}/tmp_workspaces/inbox/S4-03.md"')
    assert all(c["argv"][0] == "/usr/bin/git" for c in f.calls)
    assert not (env.inbox / "S4-03.run").exists() and len(env.done()) == 1
    assert "Окно Терминала открыто" in env.log("S4-03.run.log")


def test_run_model_opus(env: Env) -> None:
    env.task("S4-03")
    env.put("S4-03.run", "model=opus\n")
    env.pass_()
    assert "--model opus " in env.fake.windows[0][1]


@pytest.mark.parametrize("text, expected", [
    ("", {"model": "sonnet", "effort": None}),
    ("model=opus\n", {"model": "opus", "effort": None}),
    ("effort=high\n", {"model": "sonnet", "effort": "high"}),
    ("model=sonnet effort=high", {"model": "sonnet", "effort": "high"}),
    ("effort=high model=sonnet", {"model": "sonnet", "effort": "high"}),
    ("effort=max model=haiku\n", {"model": "haiku", "effort": "max"}),
])
def test_parse_run_accepts(text: str, expected: dict) -> None:
    assert ir.parse_content("run", text) == (expected, "")


@pytest.mark.parametrize("text", [
    "effort=turbo", "model=gpt", "speed=high", "model=sonnet model=opus", "effort=high effort=low",
    "model=sonnet extra", "model=sonnet\neffort=high", "effort=", "EFFORT=high", "effort=High",
    "model=sonnet  effort=high", "effort=high ", "effort",
])
def test_parse_run_rejects(text: str) -> None:
    params, reason = ir.parse_content("run", text)
    assert params is None and reason


def test_terminal_command_effort() -> None:
    base = ir.terminal_command(Path("/m/main"), Path("/m/wt"), "S4-03", "sonnet")
    assert "--effort" not in base
    cmd = ir.terminal_command(Path("/m/main"), Path("/m/wt"), "S4-03", "sonnet", "high")
    assert cmd == ('cd /m/wt/S4-03 && claude --model sonnet --effort high '
                   '"Выполни задание из файла /m/main/tmp_workspaces/inbox/S4-03.md"')
    for bad in ("turbo", "high;id", "", "High"):
        with pytest.raises(AssertionError):
            ir.terminal_command(Path("/m/main"), Path("/m/wt"), "S4-03", "sonnet", bad)


def test_run_model_and_effort(env: Env) -> None:
    env.task("S4-03")
    env.put("S4-03.run", "model=opus effort=xhigh\n")
    env.pass_()
    assert "claude --model opus --effort xhigh " in env.fake.windows[0][1]
    assert "Запуск: model=opus effort=xhigh" in env.log("S4-03.run.log")


def test_run_effort_only(env: Env) -> None:
    env.task("S4-03")
    env.put("S4-03.run", "effort=high")
    env.pass_()
    assert "claude --model sonnet --effort high " in env.fake.windows[0][1]
    assert "Запуск: model=sonnet effort=high" in env.log("S4-03.run.log")


def test_run_empty_reports_default_effort(env: Env) -> None:
    env.task("S4-03")
    env.put("S4-03.run")
    env.pass_()
    assert "--effort" not in env.fake.windows[0][1]
    assert "Запуск: model=sonnet effort=по умолчанию" in env.log("S4-03.run.log")


def test_run_bad_effort_skipped(env: Env) -> None:
    env.task("S4-03")
    env.put("S4-03.run", "effort=turbo")
    env.pass_()
    assert env.fake.calls == [] and env.fake.windows == []
    assert "S4-03.run" in env.skipped()
    assert not (env.wroot / "S4-03").exists()


@pytest.mark.parametrize("text", [
    "model=gpt4", "model=sonnet;rm -rf ~", "model=sonnet\nmodel=opus", "model = opus", "model= opus", "model =opus",
    " model=opus", "model=opus ", "model=Opus", "MODEL=opus", "opus", "model=", "model=opus\r\n", "\n\nmodel=opus",
    "model=sonnet opus", "model=$(id)",
])
def test_run_bad_content_skipped(env: Env, text: str) -> None:
    env.task("S4-03")
    env.put("S4-03.run", text)
    env.pass_()
    assert env.fake.calls == [] and env.fake.windows == []
    assert "S4-03.run" in env.skipped()
    assert (env.inbox / "S4-03.run").exists()  # untouched
    assert not (env.outbox / "S4-03.run.log").exists()


def test_run_without_task_file_skipped(env: Env) -> None:
    env.put("S4-03.run")
    env.pass_()
    assert env.fake.calls == [] and "нет файла задания" in env.skipped()


def test_run_task_symlink_does_not_count(env: Env, tmp_path: Path) -> None:
    (env.inbox / "S4-03.md").symlink_to(tmp_path)
    env.put("S4-03.run")
    env.pass_()
    assert env.fake.calls == []


def test_run_branch_exists_nothing_done(env: Env) -> None:
    env.task("S4-03")
    env.put("S4-03.run")
    env.fake.branch_rc = 0
    env.pass_()
    assert len(env.fake.calls) == 1 and env.fake.windows == []
    assert "уже есть" in env.log("S4-03.run.log")


def test_run_folder_exists_nothing_done(env: Env) -> None:
    env.task("S4-03")
    env.put("S4-03.run")
    (env.wroot / "S4-03").mkdir(parents=True)
    env.pass_()
    assert not env.fake.lines("fetch") and env.fake.windows == []
    assert "папка" in env.log("S4-03.run.log")


def test_run_branch_check_error_is_refusal(env: Env) -> None:
    env.task("S4-03")
    env.put("S4-03.run")
    env.fake.branch_rc = 128
    env.pass_()
    assert not env.fake.lines("fetch") and env.fake.windows == []


def test_run_fetch_failure_stops(env: Env) -> None:
    env.task("S4-03")
    env.put("S4-03.run")
    env.fake.on("fetch", 1, "fatal: no network")
    env.pass_()
    assert not env.fake.lines("worktree add") and env.fake.windows == []
    log = env.log("S4-03.run.log")
    assert "fatal: no network" in log and "fetch не прошёл" in log


def test_run_worktree_add_failure_no_window(env: Env) -> None:
    env.task("S4-03")
    env.put("S4-03.run")
    env.fake.on("worktree add", 128, "fatal: boom")
    env.pass_()
    assert env.fake.windows == [] and "fatal: boom" in env.log("S4-03.run.log")


def test_run_window_failure_keeps_worktree_and_prints_command(env: Env) -> None:
    env.task("S4-03")
    env.put("S4-03.run")
    env.fake.window_ok = False
    env.pass_()
    log = env.log("S4-03.run.log")
    assert "оставлена" in log and env.fake.windows[0][1] in log
    assert not env.fake.lines("remove")


def test_run_repeat_not_executed(env: Env) -> None:
    env.task("S4-03")
    env.put("S4-03.run")
    env.pass_()
    n = len(env.fake.calls)
    env.put("S4-03.run")
    env.pass_()
    assert len(env.fake.calls) == n and len(env.fake.windows) == 1
    assert "повтор" in env.skipped()


# --- .run on real git ---------------------------------------------------------------


def test_run_with_real_git(tmp_path: Path) -> None:
    origin, main = tmp_path / "origin.git", tmp_path / "main"
    git(tmp_path, "init", "-q", "--bare", "-b", "our-main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(main))
    identify(main)
    git(main, "checkout", "-q", "-b", "our-main")
    (main / "a.txt").write_text("1\n")
    git(main, "add", ".")
    git(main, "commit", "-q", "-m", "init")
    git(main, "push", "-q", "origin", "our-main")
    git(main, "branch", "--set-upstream-to=origin/our-main")
    inbox = main / "tmp_workspaces" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "T1.md").write_text("task")
    (inbox / "T1.run").write_text("")
    os.utime(inbox / "T1.run", (OLD, OLD))
    head, status = git(main, "rev-parse", "HEAD"), git(main, "status", "--porcelain")
    fake = Fake()

    def real(argv, cwd=None, env=None, timeout=120, stdin_text=None):
        done = subprocess.run(["git", *argv[1:]] if argv[0] == ir.GIT else argv, cwd=cwd, capture_output=True,
                              text=True, stdin=subprocess.DEVNULL)
        return ir.Proc(done.returncode, done.stdout + done.stderr)

    ops = fake.ops()
    ops.proc = real
    settings = ir.Settings(main, tmp_path / "wt", "/x/gh")
    ir.Runner(settings, ops).run_pass()
    wt = tmp_path / "wt" / "T1"
    assert (wt / "a.txt").exists()
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "dev/T1"
    assert git(wt, "rev-parse", "HEAD") == head
    up = subprocess.run(["git", "rev-parse", "--abbrev-ref", "dev/T1@{upstream}"], cwd=main, capture_output=True)
    assert up.returncode != 0
    assert git(main, "rev-parse", "HEAD") == head
    assert git(main, "status", "--porcelain") == status
    assert len(fake.windows) == 1
    # a second command with the same code does nothing more
    (inbox / "T1.run").write_text("")
    os.utime(inbox / "T1.run", (OLD, OLD))
    ir.Runner(settings, ops).run_pass()
    assert len(fake.windows) == 1


# --- .merge ------------------------------------------------------------------------


def pr(**over) -> dict:
    base = {"number": 7, "headRefName": "dev/S4-03", "baseRefName": "our-main", "isCrossRepository": False,
            "headRepositoryOwner": {"login": "Serg10011"}}
    base.update(over)
    return base


def merge_env(env: Env, prs: list, text: str = "dev/S4-03\n") -> None:
    env.put("S4-03.merge", text)
    env.fake.on("pr list", 0, json.dumps(prs))


def test_merge_happy_path(env: Env) -> None:
    merge_env(env, [pr()])
    env.pass_()
    lines = [c["line"] for c in env.fake.calls]
    assert lines[0].startswith(f"{env.gh} pr list --repo Serg10011/oh-my-symphony --head dev/S4-03 --base our-main "
                               "--state open --json ")
    assert lines[1] == f"{env.gh} pr merge dev/S4-03 --repo Serg10011/oh-my-symphony --merge"
    assert lines[2] == f"{sys.executable} {env.main}/ours/bin/post-merge --main {env.main}"
    assert env.fake.calls[2]["timeout"] == ir.POST_MERGE_TIMEOUT_S
    assert env.fake.calls[2]["cwd"] == env.main
    assert "--admin" not in " ".join(lines) and "--auto" not in " ".join(lines)
    assert "--delete-branch" not in " ".join(lines)
    assert "post-merge: готово" in env.log("S4-03.merge.log")


@pytest.mark.parametrize("prs", [
    [],
    [pr(), pr(number=8)],
    [pr(isCrossRepository=True, headRepositoryOwner={"login": "someone"})],
    [pr(isCrossRepository=True)],
    [pr(headRepositoryOwner={"login": "someone"})],
    [pr(headRepositoryOwner=None)],
    [pr(baseRefName="main")],
    [pr(baseRefName=None)],
    [pr(headRefName="other")],
    [pr(isCrossRepository="false")],
    [pr(number="7")],
    [pr(number=True)],
    {"number": 7},
    ["x"],
])
def test_merge_refused_when_pr_not_suitable(env: Env, prs) -> None:
    merge_env(env, prs)
    env.pass_()
    assert not env.fake.lines("pr merge") and not env.fake.lines("post-merge")
    assert "ОТКАЗ" in env.log("S4-03.merge.log")


def test_merge_not_json(env: Env) -> None:
    env.put("S4-03.merge", "dev/S4-03")
    env.fake.on("pr list", 0, "not json")
    env.pass_()
    assert not env.fake.lines("pr merge")


def test_merge_pr_list_failure(env: Env) -> None:
    env.put("S4-03.merge", "dev/S4-03")
    env.fake.on("pr list", 1, "gh: not logged in")
    env.pass_()
    assert not env.fake.lines("pr merge") and "not logged in" in env.log("S4-03.merge.log")


def test_merge_failure_no_post_merge(env: Env) -> None:
    merge_env(env, [pr()])
    env.fake.on("pr merge", 1, "merge conflict")
    env.pass_()
    assert not env.fake.lines("post-merge")
    assert "post-merge не запускался" in env.log("S4-03.merge.log")


def test_merge_post_merge_rc2_is_normal(env: Env) -> None:
    merge_env(env, [pr()])
    env.fake.on("post-merge", 2, "deferred")
    assert env.pass_() is False
    assert "код 2 — это нормально" in env.log("S4-03.merge.log")


def test_merge_post_merge_rc1_reported(env: Env) -> None:
    merge_env(env, [pr()])
    env.fake.on("post-merge", 1, "boom")
    env.pass_()
    assert "ОШИБКА (код 1)" in env.log("S4-03.merge.log")


@pytest.mark.parametrize("branch", ["our-main", "main", "--admin", "a b", "x;y", "-x", "a..b", "dev/", "x.lock"])
def test_merge_bad_branch_skipped(env: Env, branch: str) -> None:
    env.put("S4-03.merge", branch)
    env.pass_()
    assert env.fake.calls == [] and "S4-03.merge" in env.skipped()


@pytest.mark.parametrize("text", ["", "\n", "a\nb", "a\n\n", "dev/a dev/b"])
def test_merge_bad_content_skipped(env: Env, text: str) -> None:
    env.put("S4-03.merge", text)
    env.pass_()
    assert env.fake.calls == []


def test_merge_repeat_not_executed(env: Env) -> None:
    merge_env(env, [pr()])
    env.pass_()
    n = len(env.fake.calls)
    env.put("S4-03.merge", "dev/S4-03")
    env.pass_()
    assert len(env.fake.calls) == n and "повтор" in env.skipped()


def test_self_update_after_merge_restarts_after_report(env: Env) -> None:
    merge_env(env, [pr()])
    runner = env.runner()
    seen: dict = {}

    def change_hash(argv):
        if "post-merge" in " ".join(map(str, argv)):
            env.fake.hash = "h2"
    env.fake.on_call = change_hash
    real_hash = env.fake.ops().code_hash

    def checking_hash():
        # called after the command: the report must be complete on disk by now
        log = env.outbox / "S4-03.merge.log"
        if log.exists() and env.fake.hash == "h2":
            seen["log"] = log.read_text()
        return real_hash()
    runner.ops.code_hash = checking_hash
    assert runner.run_pass() is True
    assert "Конец." in seen["log"] and "post-merge" in seen["log"]


def test_no_restart_when_hash_unchanged_or_other_command(env: Env) -> None:
    env.task("S4-04")
    env.put("S4-04.run")
    env.fake.hash = "h1"
    runner = env.runner()
    env.fake.hash = "h2"  # changed, but .run does not trigger the restart check
    assert runner.run_pass() is False


# --- file acceptance ----------------------------------------------------------------


def test_symlink_not_processed(env: Env, tmp_path: Path) -> None:
    target = tmp_path / "real.txt"
    target.write_text("")
    (env.inbox / "S4-03.test").symlink_to(target)
    env.task("S4-03")
    env.pass_()
    assert env.fake.calls == [] and "обычный файл" in env.skipped()
    assert (env.inbox / "S4-03.test").is_symlink()


def test_directory_named_like_command_not_processed(env: Env) -> None:
    (env.inbox / "S4-03.test").mkdir()
    env.pass_()
    assert env.fake.calls == [] and "обычный файл" in env.skipped()


def test_big_file_not_processed(env: Env) -> None:
    env.put("S4-03.merge", "dev/" + "a" * 2000)
    env.pass_()
    assert env.fake.calls == [] and "больше" in env.skipped()


def test_non_utf8_not_processed(env: Env) -> None:
    env.put("S4-03.merge", b"dev/\xff\xfe")
    env.pass_()
    assert env.fake.calls == [] and "UTF-8" in env.skipped()


def test_fresh_file_waits_and_is_not_logged(env: Env) -> None:
    env.task("S4-03")
    env.put("S4-03.run", age=0.5)
    env.pass_()
    assert env.fake.calls == [] and env.skipped() == ""
    assert (env.inbox / "S4-03.run").exists()
    env.put("S4-03.run", age=5)
    env.pass_()
    assert len(env.fake.windows) == 1


def test_unknown_files_logged_once_md_and_done_silent(env: Env) -> None:
    env.put("notes.txt", "x")
    env.put("S4-03.RUN", "")
    env.put("S4-03.sh", "rm -rf /")
    env.task("S4-03")
    (env.inbox / "done").mkdir()
    (env.inbox / "done" / "old.run.20250101").write_text("")
    runner = env.runner()
    runner.run_pass()
    first = env.skipped()
    runner.run_pass()
    runner.run_pass()
    assert env.skipped() == first
    assert "notes.txt" in first and "S4-03.RUN" in first and "S4-03.sh" in first
    assert "S4-03.md" not in first and "done" not in first
    assert env.fake.calls == []
    assert (env.inbox / "notes.txt").exists()


@pytest.mark.parametrize("name", [
    "a;b.run", "a b.run", "$(id).run", "a..b.run", "Задача.run", ("a" * 40) + ".run", "S4-02.RUN", "x\ny.run",
])
def test_bad_code_names_skipped(env: Env, name: str) -> None:
    env.put(name, "")
    env.pass_()
    assert env.fake.calls == [] and env.fake.windows == []
    assert (env.inbox / name).exists()


# --- accounting -----------------------------------------------------------------------


def test_file_claimed_into_done_before_execution(env: Env) -> None:
    env.task("S4-03")
    env.put("S4-03.run")
    seen: list[tuple[bool, list[str]]] = []
    env.fake.on_call = lambda argv: seen.append(((env.inbox / "S4-03.run").exists(), env.done()))
    env.pass_()
    assert seen and all(not present and len(done) == 1 for present, done in seen)
    assert env.done()[0].startswith("S4-03.run.") and env.done()[0].endswith("Z")


def test_after_failure_file_stays_in_done_and_second_pass_idle(env: Env) -> None:
    env.task("S4-03")
    env.put("S4-03.run")
    env.fake.on("fetch", 1, "x")
    runner = env.runner()
    runner.run_pass()
    n = len(env.fake.calls)
    runner.run_pass()
    assert len(env.fake.calls) == n and len(env.done()) == 1


def test_internal_error_does_not_crash_pass(env: Env) -> None:
    env.task("S4-03")
    env.put("S4-03.run")

    def boom(argv):
        raise RuntimeError("boom")
    env.fake.on_call = boom
    env.pass_()  # must not raise
    assert "ВНУТРЕННЯЯ ОШИБКА" in env.log("S4-03.run.log")


def test_internal_error_report_has_traceback(env: Env) -> None:
    env.task("S4-03")
    env.put("S4-03.run")

    def boom(argv):
        raise RuntimeError("boom-marker")
    env.fake.on_call = boom
    env.pass_()
    log = env.log("S4-03.run.log")
    assert "ВНУТРЕННЯЯ ОШИБКА" in log
    assert "Traceback (most recent call last)" in log and "boom-marker" in log


def test_files_and_dirs_are_owner_only(env: Env) -> None:
    env.task("S4-03")
    env.put("S4-03.run")
    env.pass_()
    for p in (env.outbox / "S4-03.run.log", env.outbox / "_skipped.log"):
        if p.exists():
            assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE((env.inbox / "done").stat().st_mode) & 0o077 == 0
    assert stat.S_IMODE(env.outbox.stat().st_mode) & 0o077 == 0


def test_lock_second_copy_refuses(env: Env, capsys) -> None:
    first = ir.acquire_lock(env.main)
    assert first is not None
    try:
        assert ir.serve(env.settings, env.fake.ops(), once=True) == ir.EXIT_FAIL
        assert "уже запущена" in capsys.readouterr().err
    finally:
        first.close()
    assert ir.serve(env.settings, env.fake.ops(), once=True) == ir.EXIT_OK


def test_serve_returns_ok_on_restart(env: Env, capsys) -> None:
    merge_env(env, [pr()])
    env.fake.on_call = lambda argv: setattr(env.fake, "hash", "h2") if "post-merge" in " ".join(map(str, argv)) else None
    assert ir.serve(env.settings, env.fake.ops(), once=False) == 0
    assert "launchd запустит новую версию" in capsys.readouterr().out


def test_stop_flag_ends_loop(env: Env) -> None:
    ir.STOP.set()
    assert ir.serve(env.settings, env.fake.ops(), once=False) == 0
    assert env.fake.calls == []


# --- .test ------------------------------------------------------------------------------


def wt_listing(env: Env, code: str, branch: str | None = None) -> str:
    branch = branch or f"dev/{code}"
    return (f"worktree {env.main}\nHEAD abc\nbranch refs/heads/our-main\n\n"
            f"worktree {env.wroot}/{code}\nHEAD def\nbranch refs/heads/{branch}\n")


def prep_test(env: Env, changed: str, code: str = "S4-05", main_venv: bool = True, listing: str | None = None) -> None:
    env.put(f"{code}.test")
    (env.wroot / code).mkdir(parents=True)
    if main_venv:
        py = env.main / ".venv" / "bin" / "python"
        py.parent.mkdir(parents=True)
        py.write_text("")
    env.fake.on("worktree list", 0, listing if listing is not None else wt_listing(env, code))
    env.fake.on("diff --name-only", 0, changed)


def steps(env: Env) -> list[str]:
    return [" ".join(c["argv"][1:]) for c in env.fake.calls if "-m" in c["argv"] and c["argv"][1] == "-m"]


def test_test_ours_only(env: Env) -> None:
    prep_test(env, "ours/a.py\ndocs/b.md\n")
    env.pass_()
    py = str(env.main / ".venv" / "bin" / "python")
    checks = [c for c in env.fake.calls if c["argv"][0] == py]
    assert [" ".join(c["argv"][1:]) for c in checks] == ["-m ruff check ours", "-m pytest ours/tests -q"]
    assert all(c["cwd"] == env.wroot / "S4-05" for c in checks)
    log = env.log("S4-05.test.log")
    assert "ТЕСТЫ: PASS" in log and "docs/, ours/" in log and py in log
    assert "Код возврата: 0" in log
    assert not [c for c in env.fake.calls if "venv" in c["line"] and "-m venv" in c["line"]]


def test_test_tests_dir_runs_four_steps_with_main_python(env: Env) -> None:
    prep_test(env, "tests/test_a.py\nours/a.py\n")
    env.pass_()
    py = str(env.main / ".venv" / "bin" / "python")
    checks = [" ".join(c["argv"][1:]) for c in env.fake.calls if c["argv"][0] == py]
    assert checks == ["-m ruff check ours", "-m pytest ours/tests -q", "-m ruff check src tests", "-m pytest -q"]
    assert not any("-m venv" in c["line"] for c in env.fake.calls)


def test_test_src_creates_own_venv(env: Env) -> None:
    prep_test(env, "src/symphony/a.py\n")
    wt = env.wroot / "S4-05"
    env.fake.on("import symphony", 0, f"{wt}/src/symphony/__init__.py\n")
    env.pass_()
    own = str(wt / ".venv" / "bin" / "python")
    lines = [c["line"] for c in env.fake.calls]
    venv_i = next(i for i, ln in enumerate(lines) if "-m venv .venv" in ln)
    pip_i = next(i for i, ln in enumerate(lines) if "pip install -e .[dev]" in ln)
    probe_i = next(i for i, ln in enumerate(lines) if "import symphony" in ln)
    first_check = next(i for i, ln in enumerate(lines) if ln.startswith(f"{own} -m ruff check ours"))
    assert venv_i < pip_i < probe_i < first_check
    checks = [" ".join(c["argv"][1:]) for c in env.fake.calls if c["argv"][0] == own and c["argv"][1] == "-m"]
    assert checks == ["-m ruff check ours", "-m pytest ours/tests -q", "-m ruff check src tests", "-m pytest -q"]
    assert "ТЕСТЫ: PASS" in env.log("S4-05.test.log")
    assert env.fake.calls[venv_i]["timeout"] == 900 and env.fake.calls[pip_i]["timeout"] == 900


def test_test_src_existing_venv_not_recreated_but_checked(env: Env) -> None:
    prep_test(env, "src/a.py\n")
    wt = env.wroot / "S4-05"
    (wt / ".venv").mkdir()
    env.fake.on("import symphony", 0, f"{wt}/src/symphony/__init__.py\n")
    env.pass_()
    assert not any("-m venv" in c["line"] for c in env.fake.calls)
    assert "ТЕСТЫ: PASS" in env.log("S4-05.test.log")


def test_test_src_wrong_symphony_path_fails(env: Env, tmp_path: Path) -> None:
    prep_test(env, "src/a.py\n")
    env.fake.on("import symphony", 0, f"{env.main}/src/symphony/__init__.py\n")
    env.pass_()
    log = env.log("S4-05.test.log")
    assert "ТЕСТЫ: FAIL" in log and "чужой код" in log
    assert not [c for c in env.fake.calls if c["argv"][1:3] == ["-m", "ruff"]]


def test_test_src_pip_failure_fails_without_checks(env: Env) -> None:
    prep_test(env, "src/a.py\n")
    env.fake.on("pip install", 1, "no network")
    env.pass_()
    log = env.log("S4-05.test.log")
    assert "ТЕСТЫ: FAIL" in log and "no network" in log
    assert not [c for c in env.fake.calls if c["argv"][1:3] == ["-m", "ruff"]]


def test_test_no_main_venv_refused(env: Env) -> None:
    prep_test(env, "ours/a.py\n", main_venv=False)
    env.pass_()
    assert not [c for c in env.fake.calls if c["argv"][1:3] == ["-m", "ruff"]]
    assert "ОТКАЗ" in env.log("S4-05.test.log") and "ТЕСТЫ: PASS" not in env.log("S4-05.test.log")


def test_test_worktree_not_registered(env: Env) -> None:
    prep_test(env, "ours/a.py\n", listing=f"worktree {env.main}\nHEAD a\nbranch refs/heads/our-main\n")
    env.pass_()
    assert not [c for c in env.fake.calls if c["argv"][1:3] == ["-m", "ruff"]]
    assert "не числится" in env.log("S4-05.test.log")


def test_test_wrong_branch_refused(env: Env) -> None:
    prep_test(env, "ours/a.py\n", listing=wt_listing(env, "S4-05", "dev/other"))
    env.pass_()
    assert not [c for c in env.fake.calls if c["argv"][1:3] == ["-m", "ruff"]]
    assert "ОТКАЗ" in env.log("S4-05.test.log")


def test_test_env_has_no_symphony_vars(env: Env, monkeypatch) -> None:
    monkeypatch.setenv("SYMPHONY_WORKFLOW_DIR", "/x")
    monkeypatch.setenv("SYMPHONY_PYTHON", "/y")
    monkeypatch.setenv("VIRTUAL_ENV", "/z")
    prep_test(env, "tests/a.py\n")
    env.pass_()
    assert env.fake.calls
    for c in env.fake.calls:
        assert c["env"] is not None
        assert not [k for k in c["env"] if k.startswith("SYMPHONY_")]
        assert "VIRTUAL_ENV" not in c["env"]
    check = next(c for c in env.fake.calls if c["argv"][1:3] == ["-m", "ruff"])
    assert check["env"]["PATH"] == f"{env.main}/.venv/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    assert set(check["env"]) == {"PATH", "HOME", "LANG"}


def test_test_one_failure_does_not_stop_others(env: Env) -> None:
    prep_test(env, "tests/a.py\n")
    env.fake.on("-m ruff check ours", 1, "E501 lint problem")
    env.pass_()
    log = env.log("S4-05.test.log")
    py = str(env.main / ".venv" / "bin" / "python")
    assert len([c for c in env.fake.calls if c["argv"][0] == py]) == 4
    assert "ТЕСТЫ: FAIL" in log and "E501 lint problem" in log and "Код возврата: 1" in log
    assert "ПАДЕНИЕ (код 1)" in log


def test_test_timeout_marks_step_failed(env: Env) -> None:
    prep_test(env, "ours/a.py\n")
    env.fake.on("pytest ours/tests", -9, "partial", timed_out=True)
    env.pass_()
    log = env.log("S4-05.test.log")
    assert "таймаут" in log and "ТЕСТЫ: FAIL" in log
    timeouts = {" ".join(c["argv"][1:]): c["timeout"] for c in env.fake.calls if c["argv"][1:2] == ["-m"]}
    assert timeouts["-m ruff check ours"] == 300 and timeouts["-m pytest ours/tests -q"] == 1200


def test_test_full_pytest_timeout_3600(env: Env) -> None:
    prep_test(env, "tests/a.py\n")
    env.pass_()
    full = next(c for c in env.fake.calls if c["argv"][1:] == ["-m", "pytest", "-q"])
    assert full["timeout"] == 3600


def test_test_nonempty_content_skipped(env: Env) -> None:
    prep_test(env, "ours/a.py\n")
    env.put("S4-05.test", "pytest -x; rm -rf ~")
    env.pass_()
    assert env.fake.calls == [] and "S4-05.test" in env.skipped()


def test_test_repeat_allowed_old_report_renamed(env: Env) -> None:
    prep_test(env, "ours/a.py\n")
    env.pass_()
    first = env.log("S4-05.test.log")
    env.fake.on("pytest ours/tests", 1, "now failing")
    env.put("S4-05.test")
    env.pass_()
    rotated = [p for p in env.outbox.iterdir() if p.name.startswith("S4-05.test.") and p.name != "S4-05.test.log"]
    assert len(rotated) == 1 and rotated[0].name.endswith("Z.log")
    assert rotated[0].read_text() == first
    assert "ТЕСТЫ: FAIL" in env.log("S4-05.test.log")
    assert len(env.done()) == 2


def test_test_file_in_done_before_checks(env: Env) -> None:
    prep_test(env, "ours/a.py\n")
    seen: list[bool] = []
    env.fake.on_call = lambda argv: seen.append(not (env.inbox / "S4-05.test").exists() and len(env.done()) == 1)
    env.pass_()
    assert seen and all(seen)


def test_test_diff_failure_refused(env: Env) -> None:
    prep_test(env, "")
    env.fake.on("diff --name-only", 128, "fatal: bad revision")
    env.pass_()
    assert "ОТКАЗ" in env.log("S4-05.test.log")
    assert not [c for c in env.fake.calls if c["argv"][1:3] == ["-m", "ruff"]]


# --- real process runner ----------------------------------------------------------------


def test_run_process_timeout_kills_group(tmp_path: Path) -> None:
    marker = tmp_path / "alive"
    child = tmp_path / "child.py"
    child.write_text(f"import time, pathlib\ntime.sleep(3)\npathlib.Path({str(marker)!r}).write_text('x')\n")
    parent = tmp_path / "parent.py"
    parent.write_text(f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, {str(child)!r}])\ntime.sleep(30)\n")
    done = ir.run_process([sys.executable, str(parent)], timeout=1)
    assert done.timed_out
    import time
    time.sleep(4)
    assert not marker.exists()  # the grandchild died with the group


def test_run_process_stdin_closed_and_output_merged() -> None:
    done = ir.run_process([sys.executable, "-c", "import sys; print(repr(sys.stdin.read())); print('e', file=sys.stderr)"],
                          timeout=20)
    assert done.rc == 0 and "''" in done.out and "e" in done.out


def test_run_process_missing_binary_is_clean_error() -> None:
    done = ir.run_process(["/nonexistent/gh", "x"])
    assert done.rc == 127 and "не удалось запустить" in done.out


SLOW_ECHO = "import sys, time; data = sys.stdin.read(); time.sleep(1.5); print('got:' + data)"


def test_run_process_slow_child_gets_stdin_once() -> None:
    # the child outlives the 0.5 s poll, so communicate() is called again: it must not get input twice
    done = ir.run_process([sys.executable, "-c", SLOW_ECHO], timeout=20, stdin_text="hello-stdin")
    assert done.rc == 0 and "got:hello-stdin" in done.out


def test_open_terminal_slow_osascript(tmp_path: Path) -> None:
    out = tmp_path / "args.txt"
    stub = tmp_path / "osascript"
    stub.write_text(f"#!{sys.executable}\nimport sys, time, pathlib\ntime.sleep(1.5)\n"
                    f"pathlib.Path({str(out)!r}).write_text(repr(sys.argv[1:]) + '\\n' + sys.stdin.read())\n")
    stub.chmod(0o755)
    ok, _ = ir.open_terminal(str(stub), "the-command")
    assert ok
    argv_line, script = out.read_text().split("\n", 1)
    assert argv_line == repr(["-", "the-command"]) and script == ir.TERMINAL_SCRIPT


def test_run_process_exception_while_waiting_kills_child(monkeypatch) -> None:
    started: list[subprocess.Popen] = []

    class Flaky(subprocess.Popen):
        calls = 0

        def communicate(self, *args, **kwargs):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise subprocess.TimeoutExpired(self.args, 0.5)
            if Flaky.calls == 2:
                raise RuntimeError("wait broke")
            return super().communicate(*args, **kwargs)

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            started.append(self)

    monkeypatch.setattr(ir.subprocess, "Popen", Flaky)
    with pytest.raises(RuntimeError, match="wait broke"):
        ir.run_process([sys.executable, "-c", "import time; time.sleep(60)"], timeout=60)
    (proc,) = started
    assert proc.poll() is not None  # killed and reaped, not left running
    with pytest.raises(ProcessLookupError):
        os.kill(proc.pid, 0)


def test_run_process_stop_interrupts() -> None:
    threading.Timer(0.7, ir.STOP.set).start()
    done = ir.run_process([sys.executable, "-c", "import time; time.sleep(30)"], timeout=60)
    assert done.interrupted


def test_open_terminal_passes_command_as_argument(tmp_path: Path) -> None:
    out = tmp_path / "args.txt"
    stub = tmp_path / "osascript"
    stub.write_text(f"#!{sys.executable}\nimport sys, pathlib\n"
                    f"pathlib.Path({str(out)!r}).write_text(repr(sys.argv[1:]) + '\\n' + sys.stdin.read())\n")
    stub.chmod(0o755)
    command = ir.terminal_command(Path("/m/main"), Path("/m/wt"), "S4-03", "opus")
    ok, _ = ir.open_terminal(str(stub), command)
    assert ok
    written = out.read_text()
    argv_line, script = written.split("\n", 1)
    assert argv_line == repr(["-", command])
    assert script == ir.TERMINAL_SCRIPT
    assert "Выполни задание" not in script and "S4-03" not in script
    assert "do script (item 1 of argv)" in script


def test_open_terminal_missing_program() -> None:
    ok, text = ir.open_terminal("/nonexistent/osascript", "x")
    assert not ok and "нет программы" in text


# --- inbox-runner-launchd ------------------------------------------------------------------


def launchd(main: Path, *extra: str, path: str | None = None) -> subprocess.CompletedProcess:
    env = {"HOME": os.environ["HOME"], "PATH": path if path is not None else "/usr/bin:/bin:/usr/sbin:/sbin"}
    return subprocess.run([sys.executable, str(LAUNCHD), "--main", str(main), *extra], capture_output=True,
                          text=True, env=env, timeout=60)


def test_launchd_plist_content_and_idempotence(tmp_path: Path) -> None:
    import plistlib
    main = tmp_path / "main"
    (main / ".git").mkdir(parents=True)
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    gh.write_text("")
    first = launchd(main, "--gh", str(gh))
    assert first.returncode == 0, first.stderr
    target = main.resolve() / "tmp_workspaces" / "launchd" / "local.symphony.inbox-runner.plist"
    data = plistlib.loads(target.read_bytes())
    assert data["Label"] == "local.symphony.inbox-runner"
    assert data["ProgramArguments"] == [sys.executable, str(main.resolve() / "ours/bin/inbox-runner"), "--main",
                                        str(main.resolve()), "--gh", str(gh)]
    assert all(os.path.isabs(a) for a in (data["ProgramArguments"][0], data["ProgramArguments"][1],
                                          data["ProgramArguments"][3], data["ProgramArguments"][5]))
    assert data["RunAtLoad"] is True and data["KeepAlive"] is True and data["ThrottleInterval"] == 10
    assert data["EnvironmentVariables"]["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"
    assert "HOME" in data["EnvironmentVariables"]
    assert data["StandardOutPath"] == str(main.resolve() / "tmp_workspaces/outbox/_launchd.log")
    assert "launchctl bootstrap gui/$(id -u)" in first.stdout and "launchctl bootout gui/$(id -u)" in first.stdout
    second = launchd(main, "--gh", str(gh))
    assert "без изменений" in second.stdout


def test_launchd_refuses_without_gh(tmp_path: Path) -> None:
    main = tmp_path / "main"
    (main / ".git").mkdir(parents=True)
    done = launchd(main, path=str(tmp_path / "empty"))
    assert done.returncode == 1 and "gh" in done.stderr
    assert not (main / "tmp_workspaces" / "launchd").exists()


def test_launchd_never_calls_launchctl() -> None:
    src = LAUNCHD.read_text()
    assert "subprocess" not in src and "os.system" not in src


# --- the whole command as a subprocess ---------------------------------------------------


def test_command_once_end_to_end(tmp_path: Path) -> None:
    origin, main = tmp_path / "origin.git", tmp_path / "main"
    git(tmp_path, "init", "-q", "--bare", "-b", "our-main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(main))
    identify(main)
    git(main, "checkout", "-q", "-b", "our-main")
    (main / "a.txt").write_text("1\n")
    git(main, "add", ".")
    git(main, "commit", "-q", "-m", "init")
    git(main, "push", "-q", "origin", "our-main")
    main = main.resolve()
    inbox = main / "tmp_workspaces" / "inbox"
    inbox.mkdir(parents=True)
    out = tmp_path / "osascript-args.txt"
    osa = tmp_path / "osascript"
    osa.write_text(f"#!{sys.executable}\nimport sys, pathlib\npathlib.Path({str(out)!r}).write_text(repr(sys.argv[1:]))\n")
    osa.chmod(0o755)
    gh = tmp_path / "gh"
    gh.write_text(f"#!{sys.executable}\nimport sys\nprint('[]')\n")
    gh.chmod(0o755)
    (inbox / "E2E-1.md").write_text("task")
    for name, text in (("E2E-1.run", "model=haiku\n"), ("E2E-2.merge", "dev/E2E-2\n"), ("junk.txt", "x")):
        (inbox / name).write_text(text)
    for p in inbox.iterdir():
        os.utime(p, (OLD, OLD))
    env = {"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    done = subprocess.run(
        [sys.executable, str(RUNNER), "--main", str(main), "--worktree-root", str(tmp_path / "wt"), "--once",
         "--gh", str(gh), "--osascript", str(osa)],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert (tmp_path / "wt" / "E2E-1" / "a.txt").exists()
    assert git(main, "rev-parse", "--abbrev-ref", "dev/E2E-1") == "dev/E2E-1"
    args = eval(out.read_text())  # noqa: S307 - our own stub wrote a repr of a list of str
    assert args[0] == "-"
    assert args[1] == (f'cd {tmp_path / "wt" / "E2E-1"} && claude --model haiku '
                       f'"Выполни задание из файла {main}/tmp_workspaces/inbox/E2E-1.md"')
    outbox = main / "tmp_workspaces" / "outbox"
    assert "Окно Терминала открыто" in (outbox / "E2E-1.run.log").read_text()
    assert "нет открытого PR" in (outbox / "E2E-2.merge.log").read_text()
    assert "junk.txt" in (outbox / "_skipped.log").read_text()
    assert sorted(p.name.split(".")[0] + "." + p.name.split(".")[1] for p in (inbox / "done").iterdir()) == [
        "E2E-1.run", "E2E-2.merge"]
    # a second copy while the lock is held refuses
    holder = ir.acquire_lock(main)
    try:
        again = subprocess.run([sys.executable, str(RUNNER), "--main", str(main), "--once", "--gh", str(gh)],
                               capture_output=True, text=True, env=env, timeout=60)
    finally:
        holder.close()
    assert again.returncode == 1 and "уже запущена" in again.stderr


def test_command_rejects_bad_paths(tmp_path: Path) -> None:
    main = tmp_path / "main dir"
    (main / ".git").mkdir(parents=True)
    done = subprocess.run([sys.executable, str(RUNNER), "--main", str(main), "--once", "--gh", "/x/gh"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 1 and "без пробелов" in done.stderr


# --- open files limit -------------------------------------------------------------------


class FakeResource:
    RLIMIT_NOFILE = 8
    RLIM_INFINITY = -1

    def __init__(self, soft: int, hard: int, error: Exception | None = None) -> None:
        self.limits = (soft, hard)
        self.error = error
        self.set_calls: list[tuple[int, int]] = []

    def getrlimit(self, _which: int) -> tuple[int, int]:
        return self.limits

    def setrlimit(self, _which: int, value: tuple[int, int]) -> None:
        if self.error:
            raise self.error
        self.set_calls.append(value)
        self.limits = value


def test_raise_limit_to_wanted(monkeypatch) -> None:
    fake = FakeResource(256, 1_000_000)
    monkeypatch.setattr(ir, "resource", fake)
    assert ir.raise_open_files_limit() == (10240, 1_000_000)
    assert fake.set_calls == [(10240, 1_000_000)]


def test_raise_limit_infinite_hard(monkeypatch) -> None:
    fake = FakeResource(256, FakeResource.RLIM_INFINITY)
    monkeypatch.setattr(ir, "resource", fake)
    assert ir.raise_open_files_limit()[0] == 10240


def test_raise_limit_capped_by_hard(monkeypatch) -> None:
    fake = FakeResource(256, 4096)
    monkeypatch.setattr(ir, "resource", fake)
    assert ir.raise_open_files_limit() == (4096, 4096)


def test_raise_limit_never_lowers(monkeypatch) -> None:
    fake = FakeResource(20000, 30000)
    monkeypatch.setattr(ir, "resource", fake)
    assert ir.raise_open_files_limit() == (20000, 30000)
    assert fake.set_calls == []


@pytest.mark.parametrize("error", [OSError("no"), ValueError("bad")])
def test_raise_limit_swallows_errors(monkeypatch, error) -> None:
    fake = FakeResource(256, 4096, error)
    monkeypatch.setattr(ir, "resource", fake)
    assert ir.raise_open_files_limit() == (256, 4096)


def test_raise_limit_inherited_by_child() -> None:
    import resource

    hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    if hard != resource.RLIM_INFINITY and hard < 1024:
        pytest.skip("hard open-files limit is below 1024")
    expected = 10240 if hard == resource.RLIM_INFINITY else min(10240, hard)
    script = (
        "import importlib.util, resource, sys\n"
        "hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]\n"
        "resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))\n"
        "spec = importlib.util.spec_from_file_location('inbox_runner', sys.argv[1])\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "sys.modules['inbox_runner'] = m\n"
        "spec.loader.exec_module(m)\n"
        "m.raise_open_files_limit()\n"
        "child = m.run_process([sys.executable, '-c', "
        "'import resource; print(resource.getrlimit(resource.RLIMIT_NOFILE)[0])'])\n"
        "print(child.out.strip())\n"
    )
    done = subprocess.run([sys.executable, "-c", script, str(OURS / "hooks" / "inbox_runner.py")],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    assert done.returncode == 0, done.stderr
    assert int(done.stdout.strip().splitlines()[-1]) >= expected


def test_test_report_shows_open_files_limit(env: Env) -> None:
    prep_test(env, "ours/a.py\n")
    env.pass_()
    assert "Лимит открытых файлов: мягкий " in env.log("S4-05.test.log")


# --- closing the Claude Code window after a merge (TASK-S4-23) -------------------------------

TTY = "/dev/ttys004"
WIN = 18816
PS_ALL, PS_TTY = "-ww -A -o", f"-ww -t {TTY} -o"


class WinFake(Fake):
    """Fake with a process table, a Terminal window and one journal of everything that happens."""

    def __init__(self, task: str) -> None:
        super().__init__()
        self.journal: list[str] = []
        self.task = task
        self.terminal_out = ""
        self.state = "match"  # what the window check script answers while the window is open
        self.check_rc = 0
        self.close_rc = 0
        self.close_works = True
        self.ps_rc = 0
        self.kills: list[tuple[int, int]] = []
        self.ignore_term: set[int] = set()
        self.kill_error: Exception | None = None
        # pid, ppid, stat, command, on the window's tty?
        self.table: list[list] = [
            [100, 1, "Ss", "-zsh", True],
            [101, 100, "S+", f"node claude --model sonnet Выполни задание из файла {task}", True],
            [102, 101, "S", "node mcp-server", False],  # helper of Claude Code, another tty
            [103, 102, "S", "sleep 999", False],
            [200, 1, "S", f"vim {task}", True],  # the task file, but not claude
            [300, 1, "S", "claude --model opus Выполни задание из файла /other/inbox/S4-99.md", True],
        ]

    def terminal(self, osascript: str, command: str) -> tuple[bool, str]:
        super().terminal(osascript, command)
        return self.window_ok, self.terminal_out

    def proc(self, argv, cwd=None, env=None, timeout=120, stdin_text=None) -> ir.Proc:
        line = " ".join(str(a) for a in argv)
        if argv[0] == "/usr/bin/osascript" and stdin_text == ir.WINDOW_CHECK_SCRIPT:
            self.journal.append("osa:check")
            self.calls.append({"argv": [str(a) for a in argv], "line": line, "cwd": cwd, "env": env,
                               "timeout": timeout, "stdin": stdin_text})
            if self.check_rc:
                return ir.Proc(self.check_rc, "osascript: boom")
            return ir.Proc(0, self.state + "\n")
        if argv[0] == "/usr/bin/osascript" and stdin_text == ir.WINDOW_CLOSE_SCRIPT:
            self.journal.append("osa:close")
            self.calls.append({"argv": [str(a) for a in argv], "line": line, "cwd": cwd, "env": env,
                               "timeout": timeout, "stdin": stdin_text})
            if self.close_rc == 0 and self.close_works:
                self.state = "no window"
            return ir.Proc(self.close_rc, "" if self.close_rc == 0 else "execution error")
        if argv[0] == ir.PS:
            self.journal.append("ps")
            if self.on_call:
                self.on_call(argv)
            self.calls.append({"argv": [str(a) for a in argv], "line": line, "cwd": cwd, "env": env,
                               "timeout": timeout})
            if self.ps_rc:
                return ir.Proc(self.ps_rc, "ps: boom")
            only_tty = PS_TTY in line
            return ir.Proc(0, "".join(f"{p} {pp} {st} {cmd}\n" for p, pp, st, cmd, on in self.table
                                      if on or not only_tty))
        if "pr merge" in line:
            self.journal.append("merge")
        elif "post-merge" in line:
            self.journal.append("post-merge")
        return super().proc(argv, cwd, env, timeout, stdin_text)

    def kill(self, pid: int, sig: int) -> None:
        self.journal.append(f"kill:{pid}:{sig}")
        if self.kill_error:
            raise self.kill_error
        self.kills.append((pid, sig))
        if sig == 15 and pid in self.ignore_term:
            return
        self.table = [r for r in self.table if r[0] != pid]

    def ops(self) -> ir.Ops:
        ops = super().ops()
        ops.kill = self.kill
        return ops


@pytest.fixture
def wenv(env: Env) -> Env:
    env.fake = WinFake(str(env.main / "tmp_workspaces" / "inbox" / "S4-03.md"))
    return env


def record_window(env: Env, code: str = "S4-03") -> Path:
    env.outbox.mkdir(parents=True, exist_ok=True)
    path = env.outbox / f"{code}.terminal.json"
    ir.write_terminal_record(path, code, WIN, TTY, "2026-10-27T10:00:00Z")
    return path


def order(journal: list[str], *labels: str) -> None:
    """The labels appear in the journal in this order (other entries may sit between them)."""
    at = -1
    for label in labels:
        later = [i for i, e in enumerate(journal) if i > at and e.startswith(label)]
        assert later, f"{label!r} not found after position {at} in {journal}"
        at = later[0]


def test_terminal_scripts_take_data_from_argv_only() -> None:
    for script in (ir.TERMINAL_SCRIPT, ir.WINDOW_CHECK_SCRIPT, ir.WINDOW_CLOSE_SCRIPT):
        assert script.startswith("on run argv") and "{" not in script and "%" not in script
    assert "item 1 of argv" in ir.WINDOW_CHECK_SCRIPT and "item 2 of argv" in ir.WINDOW_CHECK_SCRIPT
    assert "close window id wantId" in ir.WINDOW_CLOSE_SCRIPT


@pytest.mark.parametrize("text, expected", [
    ("window_id=18816\ntty=/dev/ttys004", (18816, "/dev/ttys004")),
    ("window_id=1\ntty=/dev/ttys0\n", (1, "/dev/ttys0")),
])
def test_parse_window_output_accepts(text: str, expected: tuple) -> None:
    assert ir.parse_window_output(text) == (expected, "")


@pytest.mark.parametrize("text", [
    "", "tab 1 of window id 18816", "tty=/dev/ttys004", "window_id=18816", "window_id=x\ntty=/dev/ttys004",
    "tty=/dev/ttys004\nwindow_id=18816", "window_id=18816\ntty=/dev/null", "window_id=-5\ntty=/dev/ttys004",
    "window_id=18816\ntty=/dev/ttys004\nextra", "window_id=18816\ntty=/dev/ttys004; rm -rf /",
])
def test_parse_window_output_rejects(text: str) -> None:
    parsed, why = ir.parse_window_output(text)
    assert parsed is None and why


def test_run_records_window(wenv: Env) -> None:
    wenv.task("S4-03")
    wenv.put("S4-03.run")
    wenv.fake.terminal_out = f"window_id={WIN}\ntty={TTY}"
    wenv.pass_()
    rec = json.loads((wenv.outbox / "S4-03.terminal.json").read_text())
    assert rec == {"code": "S4-03", "window_id": WIN, "tty": TTY, "opened_at": "1970-01-01T00:16:40Z"}
    assert stat.S_IMODE((wenv.outbox / "S4-03.terminal.json").stat().st_mode) == 0o600
    assert f"Окно: id {WIN}, tty {TTY}" in wenv.log("S4-03.run.log")
    assert not list(wenv.outbox.glob("*.tmp"))


@pytest.mark.parametrize("out", ["", "tab 1 of window id 18816", "window_id=18816", f"tty={TTY}\nwindow_id={WIN}"])
def test_run_unparsable_window_output_no_record_launch_ok(wenv: Env, out: str) -> None:
    wenv.task("S4-03")
    wenv.put("S4-03.run")
    wenv.fake.terminal_out = out
    wenv.pass_()
    log = wenv.log("S4-03.run.log")
    assert not (wenv.outbox / "S4-03.terminal.json").exists()
    assert "окно не записано:" in log and "Окно Терминала открыто" in log and "ОШИБКА" not in log


def test_run_failed_window_writes_no_record(wenv: Env) -> None:
    wenv.task("S4-03")
    wenv.put("S4-03.run")
    wenv.fake.window_ok = False
    wenv.fake.terminal_out = f"window_id={WIN}\ntty={TTY}"
    wenv.pass_()
    assert not (wenv.outbox / "S4-03.terminal.json").exists()


def test_record_reading_is_strict(tmp_path: Path) -> None:
    path = tmp_path / "S4-03.terminal.json"
    assert ir.read_terminal_record(path, "S4-03") == (None, "")
    good = {"code": "S4-03", "window_id": WIN, "tty": TTY, "opened_at": "x"}
    path.write_text(json.dumps(good))
    assert ir.read_terminal_record(path, "S4-03")[0] == {"code": "S4-03", "window_id": WIN, "tty": TTY}
    assert ir.read_terminal_record(path, "S4-04")[0] is None  # a record of another task
    for bad in ("not json", "[]", "null", json.dumps({**good, "window_id": True}), json.dumps({**good, "window_id": 0}),
                json.dumps({**good, "window_id": "5"}), json.dumps({**good, "tty": "/dev/null"}),
                json.dumps({**good, "tty": "/dev/ttys1; x"}), json.dumps({"code": "S4-03"})):
        path.write_text(bad)
        record, why = ir.read_terminal_record(path, "S4-03")
        assert record is None and why, bad


def test_merge_closes_window_after_merge_before_post_merge(wenv: Env) -> None:
    f = wenv.fake
    path = record_window(wenv)
    merge_env(wenv, [pr()])
    assert wenv.pass_() is False
    order(f.journal, "merge", "osa:check", "ps", "kill:101:15", "osa:close", "osa:check", "post-merge")
    assert [(p, s) for p, s in f.kills if s == 15] == [(101, 15), (102, 15), (103, 15)]
    assert all(s == 15 for _, s in f.kills)  # everything died of SIGTERM, no SIGKILL needed
    # nothing outside the tree of Claude Code was touched: shell, vim on the file, other task
    assert {p for p, _ in f.kills} == {101, 102, 103}
    assert [r[0] for r in f.table] == [100, 200, 300]
    assert not path.exists()
    log = wenv.log("S4-03.merge.log")
    assert "PR влит." in log and "Окно закрыто; запись окна удалена." in log
    assert log.index("PR влит.") < log.index("Шаг: закрыть окно Claude Code") < log.index("Шаг: post-merge")
    assert "post-merge: готово" in log


def test_merge_window_scripts_get_data_by_argv(wenv: Env) -> None:
    record_window(wenv)
    merge_env(wenv, [pr()])
    wenv.pass_()
    osa = [c for c in wenv.fake.calls if c["argv"][0] == "/usr/bin/osascript"]
    assert [c["argv"][1:] for c in osa] == [["-", str(WIN), TTY], ["-", str(WIN)], ["-", str(WIN), TTY]]
    assert all(c["timeout"] == ir.OSASCRIPT_TIMEOUT_S for c in osa)
    assert all(str(WIN) not in c["stdin"] and TTY not in c["stdin"] for c in osa)
    ps_calls = [c["argv"] for c in wenv.fake.calls if c["argv"][0] == ir.PS]
    assert ps_calls[0] == [ir.PS, "-ww", "-t", TTY, "-o", ir.PS_COLUMNS]
    assert ps_calls[1] == [ir.PS, "-ww", "-A", "-o", ir.PS_COLUMNS]


def test_merge_term_ignored_gets_kill_after_wait(wenv: Env) -> None:
    f = wenv.fake
    f.ignore_term = {103}
    record_window(wenv)
    merge_env(wenv, [pr()])
    wenv.pass_()
    assert (103, 15) in f.kills and (103, 9) in f.kills and (101, 9) not in f.kills
    assert [r[0] for r in f.table] == [100, 200, 300]
    assert "SIGKILL" in wenv.log("S4-03.merge.log") and "Окно закрыто" in wenv.log("S4-03.merge.log")


def test_merge_pid_reused_after_term_is_not_killed(wenv: Env) -> None:
    f = wenv.fake
    f.ignore_term = {103}
    record_window(wenv)
    merge_env(wenv, [pr()])

    def reuse(argv):
        if argv and argv[0] == ir.PS and "-A" in argv and (103, 15) in f.kills:
            f.table = [r for r in f.table if r[0] != 103] + [[103, 1, "S", "somebody-else", False]]

    f.on_call = reuse
    wenv.pass_()
    assert (103, 9) not in f.kills


@pytest.mark.parametrize("state, reason", [
    ("tty mismatch", "это уже не то окно"),
    ("no window", "окна с таким номером уже нет"),
    ("not running", "Терминал не запущен"),
    ("garbage", "неожиданный ответ osascript"),
])
def test_merge_window_is_not_the_one_nothing_closed(wenv: Env, state: str, reason: str) -> None:
    f = wenv.fake
    f.state = state
    path = record_window(wenv)
    merge_env(wenv, [pr()])
    wenv.pass_()
    assert f.kills == [] and "osa:close" not in f.journal and "ps" not in f.journal
    order(f.journal, "merge", "osa:check", "post-merge")
    log = wenv.log("S4-03.merge.log")
    assert reason in log and "post-merge: готово" in log
    assert path.exists()


def test_merge_osascript_failure_nothing_closed(wenv: Env) -> None:
    f = wenv.fake
    f.check_rc = 1
    path = record_window(wenv)
    merge_env(wenv, [pr()])
    wenv.pass_()
    assert f.kills == [] and "osa:close" not in f.journal and path.exists()
    assert "post-merge" in f.journal and "osascript не отработал" in wenv.log("S4-03.merge.log")


@pytest.mark.parametrize("content", [None, "not json", json.dumps({"code": "S4-03"})])
def test_merge_no_or_broken_record_nothing_closed(wenv: Env, content: str | None) -> None:
    f = wenv.fake
    if content is not None:
        wenv.outbox.mkdir(parents=True, exist_ok=True)
        (wenv.outbox / "S4-03.terminal.json").write_text(content)
    merge_env(wenv, [pr()])
    wenv.pass_()
    assert not [e for e in f.journal if e.startswith(("osa", "ps", "kill"))]
    assert "post-merge" in f.journal
    assert "окно не записано — закрой сам, если открыто" in wenv.log("S4-03.merge.log")


def test_merge_record_of_other_task_is_not_used(wenv: Env) -> None:
    record_window(wenv, "S4-04")
    merge_env(wenv, [pr()])
    wenv.pass_()
    assert not [e for e in wenv.fake.journal if e.startswith(("osa", "ps", "kill"))]
    assert (wenv.outbox / "S4-04.terminal.json").exists()


def test_merge_terminal_refuses_to_close_keeps_record(wenv: Env) -> None:
    f = wenv.fake
    f.close_works = False  # Terminal asked for confirmation: the window stays
    path = record_window(wenv)
    merge_env(wenv, [pr()])
    wenv.pass_()
    log = wenv.log("S4-03.merge.log")
    assert "окно не закрыто:" in log and "закрой сам" in log and "спросил подтверждение" in log
    assert path.exists() and "post-merge: готово" in log
    assert f.journal.count("osa:close") == 1  # nothing is retried or forced


def test_merge_close_script_error_reported(wenv: Env) -> None:
    f = wenv.fake
    f.close_rc = 1
    f.close_works = False
    record_window(wenv)
    merge_env(wenv, [pr()])
    wenv.pass_()
    assert "osascript close: код 1" in wenv.log("S4-03.merge.log") and "post-merge" in f.journal


def test_merge_ps_failure_window_left_open(wenv: Env) -> None:
    f = wenv.fake
    f.ps_rc = 2
    path = record_window(wenv)
    merge_env(wenv, [pr()])
    wenv.pass_()
    assert f.kills == [] and "osa:close" not in f.journal and path.exists()
    assert "post-merge" in f.journal and "окно не закрыто" in wenv.log("S4-03.merge.log")


def test_merge_claude_already_gone_window_still_closed(wenv: Env) -> None:
    f = wenv.fake
    f.table = [r for r in f.table if r[0] not in (101, 102, 103)]
    path = record_window(wenv)
    merge_env(wenv, [pr()])
    wenv.pass_()
    assert f.kills == [] and "osa:close" in f.journal and not path.exists()
    assert "нет процесса Claude Code этой задачи" in wenv.log("S4-03.merge.log")


def test_merge_internal_error_in_close_step_does_not_stop_post_merge(wenv: Env) -> None:
    f = wenv.fake
    f.kill_error = RuntimeError("boom")
    record_window(wenv)
    merge_env(wenv, [pr()])
    wenv.pass_()
    log = wenv.log("S4-03.merge.log")
    assert "внутренняя ошибка" in log and "post-merge: готово" in log and "ВНУТРЕННЯЯ ОШИБКА" not in log


def test_merge_kill_permission_error_is_reported_and_window_check_goes_on(wenv: Env) -> None:
    f = wenv.fake
    f.kill_error = PermissionError(1, "Operation not permitted")
    record_window(wenv)
    merge_env(wenv, [pr()])
    wenv.pass_()
    assert "не прошёл" in wenv.log("S4-03.merge.log") and "post-merge" in f.journal


def test_merge_of_other_branch_does_not_close_window(wenv: Env) -> None:
    record_window(wenv)
    merge_env(wenv, [pr(headRefName="dev/other")], text="dev/other\n")
    wenv.pass_()
    assert "post-merge" in wenv.fake.journal
    assert not [e for e in wenv.fake.journal if e.startswith(("osa", "ps", "kill"))]
    assert "не совпадает" in wenv.log("S4-03.merge.log")


def test_failed_merge_and_refusals_never_touch_window(wenv: Env) -> None:
    f = wenv.fake
    record_window(wenv)
    merge_env(wenv, [pr()])
    f.on("pr merge", 1, "merge conflict")
    wenv.pass_()
    assert not [e for e in f.journal if e.startswith(("osa", "ps", "kill", "post-merge"))]
    assert (wenv.outbox / "S4-03.terminal.json").exists()
    assert "Шаг: закрыть окно" not in wenv.log("S4-03.merge.log")


@pytest.mark.parametrize("prs", [[], [pr(), pr(number=8)], [pr(baseRefName="main")]])
def test_refused_merge_never_touches_window(wenv: Env, prs) -> None:
    record_window(wenv)
    merge_env(wenv, prs)
    wenv.pass_()
    assert not [e for e in wenv.fake.journal if e.startswith(("osa", "ps", "kill", "post-merge"))]
    assert (wenv.outbox / "S4-03.terminal.json").exists()


def test_test_command_never_touches_window(wenv: Env) -> None:
    record_window(wenv, "S4-05")
    prep_test(wenv, "ours/a.py\n")
    wenv.pass_()
    assert not [e for e in wenv.fake.journal if e.startswith(("osa", "ps", "kill"))]
    assert (wenv.outbox / "S4-05.terminal.json").exists() and "ТЕСТЫ: PASS" in wenv.log("S4-05.test.log")


# --- process selection and stopping on real processes --------------------------------------


def ps_row(pid: int, command: str, state: str = "S", ppid: int = 1) -> ir.ProcRow:
    return ir.ProcRow(pid, ppid, state, command)


def test_find_roots_needs_claude_and_exact_task_path() -> None:
    task = "/m/tmp_workspaces/inbox/S4-23.md"
    rows = [
        ps_row(1, f"claude --model sonnet Выполни задание из файла {task}"),
        ps_row(2, f"vim {task}"),  # the path, but not claude
        ps_row(3, "claude --model sonnet nothing"),  # claude, but no path
        ps_row(4, "claude Выполни задание из файла /m/tmp_workspaces/inbox/S4-2.md"),
        ps_row(5, "claude Выполни задание из файла /m/tmp_workspaces/inbox/S4-233.md"),
        ps_row(6, f"claude Выполни задание из файла /x{task}"),  # a longer path that ends with ours
        ps_row(7, f"claude Выполни задание из файла {task}", state="Z+"),  # zombie
        ps_row(8, f'claude "Выполни задание из файла {task}"'),
    ]
    assert [r.pid for r in ir.find_roots(rows, task)] == [1, 8]


def test_find_roots_claude_in_the_path_does_not_count() -> None:
    task = "/Users/x/claude/tmp_workspaces/inbox/S4-23.md"
    rows = [ps_row(1, f"vim {task}"), ps_row(2, f"node claude Выполни задание из файла {task}")]
    assert [r.pid for r in ir.find_roots(rows, task)] == [2]


def test_process_tree_takes_descendants_only() -> None:
    rows = [ps_row(10, "zsh", ppid=1), ps_row(11, "claude", ppid=10), ps_row(12, "mcp", ppid=11),
            ps_row(13, "sleep", ppid=12), ps_row(14, "other", ppid=10), ps_row(15, "x", ppid=1)]
    tree = ir.process_tree(rows, [rows[1]])
    assert sorted(tree) == [11, 12, 13]
    assert ir.process_tree([ps_row(1, "launchd", ppid=0), ps_row(os.getpid(), "me", ppid=1)],
                           [ps_row(1, "launchd", ppid=0), ps_row(os.getpid(), "me", ppid=1)]) == {}


def test_parse_ps_skips_junk() -> None:
    rows = ir.parse_ps("  12   1 Ss   /bin/zsh -l\nnot a row\n 13 12 S+ node  a   b\n14 1 S\n")
    assert [(r.pid, r.ppid, r.state, r.command) for r in rows] == [(12, 1, "Ss", "/bin/zsh -l"),
                                                                   (13, 12, "S+", "node  a   b")]


def alive(pid: int) -> bool:
    """Running and not a zombie."""
    done = subprocess.run([ir.PS, "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
    state = done.stdout.strip()
    return bool(state) and not state.startswith("Z")


def wait_for(path: Path, seconds: float = 20.0) -> int:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.exists() and path.read_text().strip():
            return int(path.read_text())
        time.sleep(0.05)
    raise AssertionError(f"{path} did not appear")


def test_stop_tree_real_processes(tmp_path: Path) -> None:
    py = sys.executable
    task = f"{tmp_path}/main/tmp_workspaces/inbox/S4-23.md"
    other = f"{tmp_path}/main/tmp_workspaces/inbox/S4-99.md"
    prompt = "Выполни задание из файла "
    grand_src = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"  # ignores SIGTERM
    child_src = ("import subprocess,sys,time;"
                 "g=subprocess.Popen([sys.executable,'-c',sys.argv[1],'grandchild']);"
                 "open(sys.argv[2],'w').write(str(g.pid));time.sleep(60)")
    stub_src = ("import subprocess,sys,time;"
                "c=subprocess.Popen([sys.executable,'-c',sys.argv[5],sys.argv[6],sys.argv[7]]);"
                "open(sys.argv[8],'w').write(str(c.pid));time.sleep(60)")
    shell_src = ("import json,subprocess,sys,time;a=json.load(open(sys.argv[1]));c=subprocess.Popen(a);"
                 "open(sys.argv[2],'w').write(str(c.pid));time.sleep(60)")
    idle_src = "import time;time.sleep(60)"
    argv_file, stub_pid_file, child_pid_file, grand_pid_file = (tmp_path / n for n in
                                                                 ("argv.json", "stub.pid", "child.pid", "grand.pid"))
    # "claude"-looking stub: runs a child which runs a grandchild that ignores SIGTERM
    stub_argv = [py, "-c", stub_src, "claude", "--model", "sonnet", prompt + task, child_src, grand_src,
                 str(grand_pid_file), str(child_pid_file)]
    spawned: list[subprocess.Popen] = []

    def start(argv: list[str]) -> subprocess.Popen:
        proc = subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        spawned.append(proc)
        return proc

    try:
        # the stub is started by `shell`, which must survive (it plays the tab's shell)
        argv_file.write_text(json.dumps(stub_argv))
        shell = start([py, "-c", shell_src, str(argv_file), str(stub_pid_file)])
        stub_pid = wait_for(stub_pid_file)
        grand_pid = wait_for(grand_pid_file)
        child_pid = int(child_pid_file.read_text())
        no_path = start([py, "-c", idle_src, "claude", "--model", "sonnet", "no task here"])
        other_code = start([py, "-c", idle_src, "claude", "--model", "sonnet", prompt + other])
        no_claude = start([py, "-c", idle_src, "editor", prompt + task])
        assert all(alive(p) for p in (stub_pid, child_pid, grand_pid, shell.pid, no_path.pid, other_code.pid,
                                      no_claude.pid))

        ops = ir.Ops()  # real ps, real kill, real sleep
        rows = ir.ps_rows(ops, "-A")
        assert rows is not None
        roots = ir.find_roots(rows, task)
        assert [r.pid for r in roots] == [stub_pid]
        stopped, notes = ir.stop_tree(ops, rows, roots, wait_s=1.0, poll_s=0.1)

        assert stopped, notes
        assert not any(alive(p) for p in (stub_pid, child_pid, grand_pid))  # root and all descendants
        assert any("SIGKILL" in n and str(grand_pid) in n for n in notes)  # the SIGTERM-proof one needed it
        assert not any("SIGKILL" in n and str(stub_pid) in n for n in notes)
        # nobody outside the tree was touched
        assert all(alive(p) for p in (shell.pid, no_path.pid, other_code.pid, no_claude.pid))
    finally:
        for proc in spawned:
            proc.kill()
        for pid_file in (stub_pid_file, child_pid_file, grand_pid_file):
            if pid_file.exists() and pid_file.read_text().strip():
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                except OSError:
                    pass
        for proc in spawned:
            proc.wait(timeout=10)

