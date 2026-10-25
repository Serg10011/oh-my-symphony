"""Command queue for the chat assistant (TASK-S4-02). Logic of ours/bin/inbox-runner.

The service watches <main>/tmp_workspaces/inbox/ and executes exactly three kinds
of command files: `<code>.run` (prepare a dev worktree and open Terminal with
Claude Code), `<code>.test` (run the fixed checks in that worktree) and
`<code>.merge` (merge a verified PR, then post-merge). Nothing else.

Security rules (everything here is a constant in code, never read from a file):
the file content is never executed or put into a shell as is; the command code
comes from the file name and is matched by a strict pattern; the model and the
branch are checked as a whole; subprocesses get an argument list, no shell,
closed stdin and a timeout. Everything external goes through `Ops`, so tests
substitute it. Standard library only.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# constants: the only place to edit what the service may do
# ---------------------------------------------------------------------------

REPO = "Serg10011/oh-my-symphony"
REPO_OWNER = "Serg10011"
BASE_BRANCH = "our-main"
REMOTE = "origin"
BRANCH_PREFIX = "dev/"
COMMANDS = ("run", "test", "merge")
MODELS = ("sonnet", "opus", "haiku")
DEFAULT_MODEL = "sonnet"
DEFAULT_WORKTREE_ROOT = "/Users/helloworld10011/dev/oms-dev"
GIT = "/usr/bin/git"
OSASCRIPT = "/usr/bin/osascript"
SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,30}")
BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,80}")
PATH_RE = re.compile(r"/[A-Za-z0-9._/-]+")
MODEL_LINE_RE = re.compile(r"model=(sonnet|opus|haiku)")

MAX_FILE_BYTES = 1024
SETTLE_S = 2.0
DEFAULT_INTERVAL_S = 3.0
GIT_TIMEOUT_S = 120
POST_MERGE_TIMEOUT_S = 1800
OSASCRIPT_TIMEOUT_S = 60
TIMEOUTS = {"ruff": 300, "ours-tests": 1200, "full-tests": 3600, "env": 900}

TERMINAL_SCRIPT = """on run argv
  tell application "Terminal"
    activate
    do script (item 1 of argv)
  end tell
end run
"""
TASK_PROMPT = "Выполни задание из файла {path}"

INBOX_REL = Path("tmp_workspaces") / "inbox"
OUTBOX_REL = Path("tmp_workspaces") / "outbox"
LOCK_REL = Path("tmp_workspaces") / "inbox-runner.lock"
SKIPPED_LOG = "_skipped.log"
SELF_FILES = (
    Path(__file__).resolve().parents[1] / "bin" / "inbox-runner",
    Path(__file__).resolve(),
)
POST_MERGE_REL = Path("ours") / "bin" / "post-merge"

EXIT_OK, EXIT_FAIL = 0, 1

STOP = threading.Event()  # set by SIGTERM: kill the running step, finish the report, exit


# ---------------------------------------------------------------------------
# validation (pure functions)
# ---------------------------------------------------------------------------


FRESH = "файл ещё пишется"  # reason meaning "look again on the next pass"


def valid_code(code: str) -> bool:
    return CODE_RE.fullmatch(code) is not None


def valid_branch(branch: str) -> bool:
    if BRANCH_RE.fullmatch(branch) is None:
        return False
    if ".." in branch or "//" in branch or branch.endswith(("/", ".", ".lock")):
        return False
    if any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/")):
        return False
    return branch not in (BASE_BRANCH, "main")


def valid_abs_path(text: str) -> bool:
    return (
        PATH_RE.fullmatch(text) is not None
        and ".." not in text.split("/")
        and "//" not in text
    )


def split_name(name: str) -> tuple[str, str] | None:
    """(code, command) of a file name, or None when it is not a command name."""
    code, dot, ext = name.rpartition(".")
    if not dot or ext not in COMMANDS or not valid_code(code):
        return None
    return code, ext


def content_lines(text: str) -> list[str]:
    """Lines of the file; one trailing newline is allowed and dropped."""
    if text == "":
        return []
    if text.endswith("\n"):
        text = text[:-1]
    return text.split("\n")


def parse_content(command: str, text: str) -> tuple[dict | None, str]:
    """Parameters of the command, or (None, reason in Russian)."""
    lines = content_lines(text)
    empty = lines in ([], [""])
    if command == "test":
        return ({}, "") if empty else (None, "файл .test должен быть пустым")
    if command == "run":
        if empty:
            return {"model": DEFAULT_MODEL}, ""
        m = MODEL_LINE_RE.fullmatch(lines[0]) if len(lines) == 1 else None
        if m is None:
            return None, "в файле .run допустима пустота или одна строка model=sonnet|opus|haiku"
        return {"model": m.group(1)}, ""
    if len(lines) != 1 or not lines[0]:
        return None, "в файле .merge должна быть ровно одна непустая строка — имя ветки"
    if not valid_branch(lines[0]):
        return None, "недопустимое имя ветки"
    return {"branch": lines[0]}, ""


def read_command_file(path: Path, now: float, settle: bool = True) -> tuple[str | None, str]:
    """Strictly read a command file: (text, "") or (None, reason).

    The reason `FRESH` means "not settled yet, try again on the next pass".
    """
    try:
        st = os.lstat(path)
    except OSError as exc:
        return None, f"не удалось прочитать: {exc.strerror}"
    if not stat.S_ISREG(st.st_mode):
        return None, "это не обычный файл (ссылка или каталог)"
    if st.st_size > MAX_FILE_BYTES:
        return None, f"файл больше {MAX_FILE_BYTES} байт"
    if settle and now - st.st_mtime < SETTLE_S:
        return None, FRESH
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        return None, f"не удалось открыть: {exc.strerror}"
    try:
        with os.fdopen(fd, "rb") as fh:
            data = fh.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        return None, f"не удалось прочитать: {exc.strerror}"
    if len(data) > MAX_FILE_BYTES:
        return None, f"файл больше {MAX_FILE_BYTES} байт"
    try:
        return data.decode("utf-8", errors="strict"), ""
    except UnicodeDecodeError:
        return None, "файл не в кодировке UTF-8"


# ---------------------------------------------------------------------------
# external world
# ---------------------------------------------------------------------------


@dataclass
class Proc:
    rc: int
    out: str = ""
    timed_out: bool = False
    interrupted: bool = False


def run_process(
    argv: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = GIT_TIMEOUT_S,
    stdin_text: str | None = None,
) -> Proc:
    """Run argv (no shell, own process group, closed stdin); kill the group on timeout or SIGTERM."""
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL if stdin_text is None else subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
        )
    except OSError as exc:
        return Proc(127, f"не удалось запустить {argv[0]}: {exc.strerror or exc}")
    deadline = time.monotonic() + timeout
    timed_out = interrupted = False
    while True:
        try:
            out, _ = proc.communicate(input=stdin_text, timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if STOP.is_set():
                interrupted = True
            elif time.monotonic() >= deadline:
                timed_out = True
            else:
                continue
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            try:
                out, _ = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                out = ""
            break
    return Proc(proc.returncode if proc.returncode is not None else -9, out or "", timed_out, interrupted)


def open_terminal(osascript: str, command: str) -> tuple[bool, str]:
    """Open a new Terminal window with `command`; the command is an argv item, not script text."""
    if not os.path.isfile(osascript) or not os.access(osascript, os.X_OK):
        return False, f"нет программы osascript по пути {osascript}"
    done = run_process([osascript, "-", command], timeout=OSASCRIPT_TIMEOUT_S, stdin_text=TERMINAL_SCRIPT)
    return done.rc == 0, done.out.strip()


def code_hash() -> str:
    h = hashlib.sha256()
    for path in SELF_FILES:
        try:
            h.update(path.read_bytes())
        except OSError:
            h.update(b"?")
    return h.hexdigest()


@dataclass
class Ops:
    proc: Callable[..., Proc] = run_process
    terminal: Callable[[str, str], tuple[bool, str]] = open_terminal
    code_hash: Callable[[], str] = code_hash
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep


@dataclass
class Settings:
    main: Path
    worktree_root: Path
    gh: str
    osascript: str = OSASCRIPT
    interval: float = DEFAULT_INTERVAL_S

    @property
    def inbox(self) -> Path:
        return self.main / INBOX_REL

    @property
    def done(self) -> Path:
        return self.inbox / "done"

    @property
    def outbox(self) -> Path:
        return self.main / OUTBOX_REL


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def utc_now(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)


def stamp(ts: float) -> str:
    return utc_now(ts).strftime("%Y%m%dT%H%M%S%fZ")


def iso(ts: float) -> str:
    return utc_now(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


class Report:
    """Append-only log of one command; every line is flushed so a crash leaves a trace."""

    def __init__(self, path: Path, clock: Callable[[], float]) -> None:
        # O_EXCL: an existing report means the command was already executed
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.fh = os.fdopen(fd, "w", encoding="utf-8")
        self.path = path
        self.clock = clock

    def line(self, text: str) -> None:
        self.fh.write(f"[{iso(self.clock())}] {text}\n")
        self.fh.flush()

    def output(self, text: str) -> None:
        body = text if text.endswith("\n") else text + "\n"
        if not text:
            body = "(пусто)\n"
        self.fh.write(f"----- вывод -----\n{body}-----------------\n")
        self.fh.flush()

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.fh.flush()
            os.fsync(self.fh.fileno())
        self.fh.close()


def unique_path(path: Path) -> Path:
    candidate, n = path, 0
    while os.path.lexists(candidate):
        n += 1
        candidate = path.with_name(f"{path.name}.{n}")
    return candidate


# ---------------------------------------------------------------------------
# command builders (separate functions: each can be changed and tested alone)
# ---------------------------------------------------------------------------


def branch_for(code: str) -> str:
    return BRANCH_PREFIX + code


def branch_exists_argv(main: Path, code: str) -> list[str]:
    return [GIT, "-C", str(main), "show-ref", "--verify", "--quiet", f"refs/heads/{branch_for(code)}"]


def fetch_argv(main: Path) -> list[str]:
    return [GIT, "-C", str(main), "fetch", REMOTE, BASE_BRANCH]


def worktree_add_argv(main: Path, worktree_root: Path, code: str) -> list[str]:
    return [GIT, "-C", str(main), "worktree", "add", "--no-track", "-b", branch_for(code),
            str(worktree_root / code), f"{REMOTE}/{BASE_BRANCH}"]


def terminal_command(main: Path, worktree_root: Path, code: str, model: str) -> str:
    """The line typed into Terminal. Only validated parts reach it."""
    assert valid_code(code) and model in MODELS and valid_abs_path(str(main)) and valid_abs_path(str(worktree_root))
    task = main / INBOX_REL / f"{code}.md"
    prompt = TASK_PROMPT.format(path=task)
    # the task path has only [A-Za-z0-9._/-], so it is safe inside double quotes
    assert not re.search(r'[\s"$`\\!\']', str(task))
    return (f"cd {shlex.quote(str(worktree_root / code))} && claude --model {shlex.quote(model)} "
            f'"{prompt}"')


def pr_list_argv(gh: str, branch: str) -> list[str]:
    return [gh, "pr", "list", "--repo", REPO, "--head", branch, "--base", BASE_BRANCH, "--state", "open",
            "--json", "number,headRefName,headRepositoryOwner,isCrossRepository,baseRefName"]


def pr_merge_argv(gh: str, branch: str) -> list[str]:
    return [gh, "pr", "merge", branch, "--repo", REPO, "--merge"]


def post_merge_argv(main: Path) -> list[str]:
    return [sys.executable, str(main / POST_MERGE_REL), "--main", str(main)]


def check_pull_request(raw: str, branch: str) -> tuple[int | None, str]:
    """(PR number, "") when exactly one open PR of this repo from `branch` into our-main exists."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None, "gh вернул не JSON"
    if not isinstance(data, list):
        return None, "gh вернул не список"
    if not data:
        return None, "нет открытого PR из этой ветки в our-main"
    if len(data) != 1:
        return None, f"открытых PR из этой ветки {len(data)}, нужен ровно один"
    pr = data[0]
    if not isinstance(pr, dict):
        return None, "неожиданный ответ gh"
    owner = pr.get("headRepositoryOwner")
    number = pr.get("number")
    if pr.get("headRefName") != branch:
        return None, "PR из другой ветки"
    if pr.get("baseRefName") != BASE_BRANCH:
        return None, f"база PR не {BASE_BRANCH}"
    if pr.get("isCrossRepository") is not False:
        return None, "PR из чужого форка"
    if not isinstance(owner, dict) or owner.get("login") != REPO_OWNER:
        return None, "ветка PR не из репозитория " + REPO
    if not isinstance(number, int) or isinstance(number, bool):
        return None, "у PR нет номера"
    return number, ""


def git_env() -> dict[str, str]:
    return clean_env(SAFE_PATH, passthrough=("SSH_AUTH_SOCK", "GH_TOKEN", "GITHUB_TOKEN"), prompt_off=True)


def gh_env(gh: str) -> dict[str, str]:
    return clean_env(f"{Path(gh).parent}:{SAFE_PATH}", passthrough=("SSH_AUTH_SOCK", "GH_TOKEN", "GITHUB_TOKEN"),
                     prompt_off=True)


def clean_env(path: str, passthrough: tuple[str, ...] = (), prompt_off: bool = False) -> dict[str, str]:
    """Short explicit environment; no SYMPHONY_* variables ever."""
    env = {"PATH": path, "HOME": str(Path.home()), "LANG": os.environ.get("LANG", "en_US.UTF-8")}
    for key in passthrough:
        if key in os.environ:
            env[key] = os.environ[key]
    if prompt_off:
        env["GIT_TERMINAL_PROMPT"] = "0"
    return env


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


class Runner:
    def __init__(self, settings: Settings, ops: Ops) -> None:
        self.s = settings
        self.ops = ops
        self.skipped_seen: set[tuple[str, int, int]] = set()
        self.start_hash = ops.code_hash()

    # --- skipped log -------------------------------------------------------

    def skip(self, name: str, reason: str, key: tuple[str, int, int] | None = None) -> None:
        """Write to _skipped.log once per file (key = name, mtime, size)."""
        if key is not None:
            if key in self.skipped_seen:
                return
            self.skipped_seen.add(key)
        self.s.outbox.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.s.outbox / SKIPPED_LOG
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(f"[{iso(self.ops.clock())}] {name!r}: {reason}\n")

    # --- one pass over the inbox ------------------------------------------

    def run_pass(self) -> bool:
        """Handle the files now in the inbox. True when the service must restart (code changed)."""
        s = self.s
        for d in (s.inbox, s.done, s.outbox):
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            names = sorted(os.listdir(s.inbox))
        except OSError:
            return False
        for name in names:
            if STOP.is_set():
                return False
            if name == "done" or name.endswith(".md"):
                continue
            try:
                handled = self.handle(name)
            except Exception as exc:  # noqa: BLE001 - the service must not die on one bad file
                self.skip(name, f"внутренняя ошибка: {exc!r}")
                handled = None
            if handled == "merge" and self.ops.code_hash() != self.start_hash:
                return True
        return False

    def handle(self, name: str) -> str | None:
        """Process one inbox entry; returns the command name when a command was executed."""
        s = self.s
        path = s.inbox / name
        try:
            st = os.lstat(path)
        except OSError:
            return None
        key = (name, st.st_mtime_ns, st.st_size)
        parsed = split_name(name)
        if parsed is None:
            self.skip(name, "неизвестный файл: имя не подходит под <код>.run, <код>.test, <код>.merge", key)
            return None
        code, command = parsed
        text, reason = read_command_file(path, self.ops.clock())
        if text is None:
            if reason != FRESH:
                self.skip(name, reason, key)
            return None
        params, reason = parse_content(command, text)
        if params is None:
            self.skip(name, reason, key)
            return None
        if command == "run":
            md = s.inbox / f"{code}.md"
            if not (md.is_file() and not md.is_symlink()):
                self.skip(name, f"нет файла задания {code}.md рядом", key)
                return None
        if command in ("run", "merge") and (s.outbox / f"{code}.{command}.log").exists():
            self.claim(path, name, text)
            self.skip(name, f"повтор: отчёт {code}.{command}.log уже есть; для повтора нужен новый код")
            return None

        # claim first: if the service dies later, the command is not repeated
        if not self.claim(path, name, text):
            return None
        report_path = s.outbox / f"{code}.{command}.log"
        if command == "test" and report_path.exists():
            os.rename(report_path, unique_path(s.outbox / f"{code}.test.{stamp(self.ops.clock())}.log"))
        try:
            report = Report(report_path, self.ops.clock)
        except FileExistsError:
            self.skip(name, "повтор: отчёт уже есть")
            return None
        try:
            report.line(f"Начало: команда .{command}, код {code}, файл {name!r}")
            if command == "run":
                self.do_run(report, code, params["model"])
            elif command == "merge":
                self.do_merge(report, params["branch"])
            else:
                self.do_test(report, code)
        except Exception as exc:  # noqa: BLE001
            report.line(f"ВНУТРЕННЯЯ ОШИБКА: {exc!r}")
        finally:
            report.line("Конец.")
            report.close()
        return command

    def claim(self, path: Path, name: str, expected: str) -> bool:
        """Atomically move the file into done/ and check that it is still what was validated."""
        target = unique_path(self.s.done / f"{name}.{stamp(self.ops.clock())}")
        try:
            os.rename(path, target)
        except OSError as exc:
            self.skip(name, f"не удалось забрать файл в done/: {exc.strerror}")
            return False
        text, reason = read_command_file(target, self.ops.clock(), settle=False)
        if text != expected:
            self.skip(name, f"файл изменился при приёме ({reason or 'содержимое другое'}); не выполнен")
            return False
        return True

    # --- shared step runner ------------------------------------------------

    def step(self, report: Report, title: str, argv: list[str], cwd: Path | None, env: dict[str, str],
             timeout: float, ok_codes: tuple[int, ...] = (0,)) -> Proc:
        report.line(f"Шаг: {title}")
        report.line("Команда: " + " ".join(shlex.quote(a) for a in argv))
        done = self.ops.proc(argv, cwd=cwd, env=env, timeout=timeout)
        report.output(done.out)
        note = " (таймаут, группа процессов убита)" if done.timed_out else (
            " (служба останавливается, шаг прерван)" if done.interrupted else "")
        report.line(f"Код возврата: {done.rc}{note}")
        return done

    # --- .run --------------------------------------------------------------

    def do_run(self, report: Report, code: str, model: str) -> None:
        s = self.s
        branch, wt = branch_for(code), s.worktree_root / code
        env = git_env()
        exists = self.ops.proc(branch_exists_argv(s.main, code), cwd=None, env=env, timeout=GIT_TIMEOUT_S)
        if exists.rc == 0:
            report.line(f"ОТКАЗ: ветка {branch} уже есть. Ничего не сделано. Для повтора нужен новый код.")
            return
        if exists.rc != 1:
            report.line(f"ОТКАЗ: не удалось проверить ветку {branch} (код {exists.rc}).")
            report.output(exists.out)
            return
        if os.path.lexists(wt):
            report.line(f"ОТКАЗ: папка {wt} уже есть. Ничего не сделано. Для повтора нужен новый код.")
            return
        fetch = self.step(report, "git fetch", fetch_argv(s.main), None, env, GIT_TIMEOUT_S)
        if fetch.rc != 0:
            report.line("ОТКАЗ: fetch не прошёл; рабочая копия и окно не созданы.")
            return
        add = self.step(report, "git worktree add", worktree_add_argv(s.main, s.worktree_root, code),
                        None, env, GIT_TIMEOUT_S)
        if add.rc != 0:
            report.line("ОТКАЗ: рабочая копия не создана; окно не открыто.")
            return
        command = terminal_command(s.main, s.worktree_root, code, model)
        report.line("Шаг: открыть окно Терминала")
        ok, text = self.ops.terminal(s.osascript, command)
        if text:
            report.output(text)
        if ok:
            report.line("Окно Терминала открыто, Claude Code запускается.")
        else:
            report.line("ОШИБКА: окно Терминала открыть не удалось. Рабочая копия создана и оставлена.")
            report.line("Строка запуска, чтобы набрать её самому в Терминале:")
            report.line(command)

    # --- .merge ------------------------------------------------------------

    def do_merge(self, report: Report, branch: str) -> None:
        s = self.s
        env = gh_env(s.gh)
        listed = self.step(report, "проверить PR", pr_list_argv(s.gh, branch), s.main, env, GIT_TIMEOUT_S)
        if listed.rc != 0:
            report.line("ОТКАЗ: не удалось получить список PR; слияние не выполнялось.")
            return
        number, why = check_pull_request(listed.out, branch)
        if number is None:
            report.line(f"ОТКАЗ: {why}. Слияние не выполнялось.")
            return
        report.line(f"Найден один подходящий PR #{number}.")
        merged = self.step(report, "gh pr merge", pr_merge_argv(s.gh, branch), s.main, env, GIT_TIMEOUT_S)
        if merged.rc != 0:
            report.line("ОШИБКА: слияние не прошло; post-merge не запускался.")
            return
        report.line("PR влит.")
        pm = self.step(report, "post-merge", post_merge_argv(s.main), s.main,
                       clean_env(SAFE_PATH, passthrough=("SSH_AUTH_SOCK", "GH_TOKEN", "GITHUB_TOKEN")),
                       POST_MERGE_TIMEOUT_S)
        if pm.rc == 0:
            report.line("post-merge: готово.")
        elif pm.rc == 2:
            report.line("post-merge: часть действий отложена, пока карточки в работе (код 2 — это нормально). "
                        "Запустить ещё раз позже: ours/bin/post-merge.")
        else:
            report.line(f"post-merge: ОШИБКА (код {pm.rc}). PR уже влит; разберитесь по выводу выше.")

    # --- .test -------------------------------------------------------------

    def registered_branch(self, wt: Path) -> str | None:
        """Branch of the worktree `wt` according to `git worktree list`, or None when not registered."""
        done = self.ops.proc([GIT, "-C", str(self.s.main), "worktree", "list", "--porcelain"],
                             cwd=None, env=git_env(), timeout=GIT_TIMEOUT_S)
        if done.rc != 0:
            return None
        want = os.path.realpath(wt)
        found, branch = False, ""
        for line in done.out.splitlines() + [""]:
            if line.startswith("worktree "):
                found, branch = os.path.realpath(line.removeprefix("worktree ")) == want, ""
            elif found and line.startswith("branch "):
                branch = line.removeprefix("branch ")
            elif found and line == "":
                return branch or "(без ветки)"
        return None

    def do_test(self, report: Report, code: str) -> None:
        s = self.s
        wt, branch = s.worktree_root / code, branch_for(code)
        actual = self.registered_branch(wt)
        if actual is None:
            report.line(f"ОТКАЗ: рабочая копия {wt} не числится в git worktree list. Проверки не запускались.")
            return
        if actual != f"refs/heads/{branch}":
            report.line(f"ОТКАЗ: в {wt} ветка {actual}, а нужна refs/heads/{branch}. Проверки не запускались.")
            return
        env = git_env()
        diff = self.step(report, "изменённые файлы", [GIT, "-C", str(wt), "diff", "--name-only",
                         f"{REMOTE}/{BASE_BRANCH}...{branch}"], None, env, GIT_TIMEOUT_S)
        if diff.rc != 0:
            report.line("ОТКАЗ: не удалось узнать, что изменила ветка. Проверки не запускались.")
            return
        changed = [p for p in diff.out.splitlines() if p]
        dirs = sorted({p.split("/", 1)[0] + "/" if "/" in p else "(корень)" for p in changed})
        touches_src = any(p.startswith("src/") for p in changed)
        full = touches_src or any(p.startswith("tests/") for p in changed)
        report.line(f"Изменённые каталоги: {', '.join(dirs) or 'нет'}.")
        report.line("Набор проверок: " + (
            "полный (ветка меняет src/ или tests/)" if full else "только ours/ (src/ и tests/ не тронуты)"))

        if touches_src:
            python = wt / ".venv" / "bin" / "python"
            if not self.prepare_venv(report, wt, python):
                report.line("ТЕСТЫ: FAIL")
                return
        else:
            python = s.main / ".venv" / "bin" / "python"
            if not python.is_file():
                report.line(f"ОТКАЗ: нет {python}; создайте окружение в основной папке. Проверки не запускались.")
                return
        report.line(f"Python: {python}")

        check_env = clean_env(f"{python.parent}:{SAFE_PATH}")
        py = str(python)
        plan = [
            ("ruff check ours", [py, "-m", "ruff", "check", "ours"], TIMEOUTS["ruff"]),
            ("pytest ours/tests -q", [py, "-m", "pytest", "ours/tests", "-q"], TIMEOUTS["ours-tests"]),
        ]
        if full:
            plan += [
                ("ruff check src tests", [py, "-m", "ruff", "check", "src", "tests"], TIMEOUTS["ruff"]),
                ("pytest -q", [py, "-m", "pytest", "-q"], TIMEOUTS["full-tests"]),
            ]
        results: list[tuple[str, Proc]] = []
        for title, argv, timeout in plan:
            results.append((title, self.step(report, title, argv, wt, check_env, timeout)))
            if STOP.is_set():
                break
        report.line("Сводка:")
        for title, res in results:
            verdict = "ок" if res.rc == 0 else ("таймаут" if res.timed_out else f"ПАДЕНИЕ (код {res.rc})")
            report.line(f"  {title}: {verdict}")
        passed = len(results) == len(plan) and all(res.rc == 0 for _, res in results)
        report.line("ТЕСТЫ: PASS" if passed else "ТЕСТЫ: FAIL")

    def prepare_venv(self, report: Report, wt: Path, python: Path) -> bool:
        """Own .venv in the worktree, so the tests check this code and not the main folder's."""
        env = clean_env(SAFE_PATH, passthrough=("SSH_AUTH_SOCK",))
        if not (wt / ".venv").exists():
            made = self.step(report, "создать .venv", [sys.executable, "-m", "venv", ".venv"], wt, env,
                             TIMEOUTS["env"])
            if made.rc != 0:
                report.line("Не удалось создать окружение; проверки не запускались.")
                return False
            pip = self.step(report, "pip install -e .[dev]", [str(wt / ".venv" / "bin" / "pip"), "install", "-e",
                            ".[dev]"], wt, env, TIMEOUTS["env"])
            if pip.rc != 0:
                report.line("Не удалось установить пакет; проверки не запускались.")
                return False
        probe = self.step(report, "откуда импортируется symphony",
                          [str(python), "-c", "import symphony; print(symphony.__file__)"], wt,
                          clean_env(f"{python.parent}:{SAFE_PATH}"), 60)
        located = os.path.realpath(probe.out.strip().splitlines()[-1]) if probe.out.strip() else ""
        if probe.rc != 0 or not located.startswith(os.path.realpath(wt) + os.sep):
            report.line(f"symphony импортируется не из рабочей копии ({located or 'не импортируется'}): "
                        "тесты проверили бы чужой код.")
            return False
        return True


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------


def default_main() -> Path | None:
    done = subprocess.run([GIT, "worktree", "list", "--porcelain"], cwd=Path(__file__).resolve().parent,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)
    for line in done.stdout.splitlines():
        if line.startswith("worktree "):
            return Path(line.removeprefix("worktree "))
    return None


def acquire_lock(main: Path):
    """Exclusive lock file; None when another copy of the service holds it."""
    path = main / LOCK_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    fh = os.fdopen(fd, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def serve(settings: Settings, ops: Ops, once: bool = False) -> int:
    """Poll the inbox until SIGTERM (or once). Returns the exit code."""
    lock = acquire_lock(settings.main)
    if lock is None:
        print("inbox-runner: уже запущена другая копия службы (занят файл-замок). Остановите её сначала.",
              file=sys.stderr)
        return EXIT_FAIL
    try:
        runner = Runner(settings, ops)
        while not STOP.is_set():
            if runner.run_pass():
                print("inbox-runner: файлы службы изменились после слияния; выхожу, launchd запустит новую версию.",
                      flush=True)
                return EXIT_OK
            if once:
                break
            waited = 0.0
            while waited < settings.interval and not STOP.is_set():
                ops.sleep(min(0.5, settings.interval))
                waited += 0.5
        return EXIT_OK
    finally:
        lock.close()


def run(argv: list[str] | None = None, ops: Ops | None = None) -> int:
    parser = argparse.ArgumentParser(prog="inbox-runner", description="Очередь команд для чата (.run, .test, .merge).")
    parser.add_argument("--main", default=None, help="основная папка форка")
    parser.add_argument("--worktree-root", default=DEFAULT_WORKTREE_ROOT, help="каталог рабочих копий разработки")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S, help="период опроса, секунд")
    parser.add_argument("--once", action="store_true", help="один проход и выход")
    parser.add_argument("--gh", default=None, help="абсолютный путь к gh")
    parser.add_argument("--osascript", default=OSASCRIPT, help="путь к osascript (для проверок)")
    args = parser.parse_args(argv)

    main = Path(args.main).expanduser() if args.main else default_main()
    if main is None or not (main / ".git").exists():
        print("inbox-runner: не нашёл основную папку форка; укажите --main", file=sys.stderr)
        return EXIT_FAIL
    main = main.resolve()
    root = Path(args.worktree_root).expanduser()
    for label, p in (("основная папка", str(main)), ("каталог рабочих копий", str(root)),
                     ("osascript", args.osascript), ("gh", args.gh or "/")):
        if not valid_abs_path(p):
            print(f"inbox-runner: {label}: нужен абсолютный путь без пробелов и спецсимволов, получено {p!r}",
                  file=sys.stderr)
            return EXIT_FAIL
    if args.interval <= 0:
        print("inbox-runner: --interval должен быть больше нуля", file=sys.stderr)
        return EXIT_FAIL
    if args.gh is None:
        print("inbox-runner: укажите --gh (абсолютный путь к gh)", file=sys.stderr)
        return EXIT_FAIL
    if not os.path.isfile(args.gh):
        print(f"inbox-runner: ВНИМАНИЕ: нет файла gh по пути {args.gh}; команды .merge будут отклоняться",
              file=sys.stderr)
    os.umask(0o077)
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    settings = Settings(main, root, args.gh, args.osascript, args.interval)
    return serve(settings, ops or Ops(), once=args.once)
