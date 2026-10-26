#!/usr/bin/env python3
"""Our after_create / before_remove hooks for a board folder outside the
code repository (TASK-K08a; prototype: REPORT-09, Q5).

    worktree.py create   hooks.after_create, cwd = the new ticket workspace
    worktree.py remove   hooks.before_remove, cwd = the ticket workspace

The board folder is $SYMPHONY_WORKFLOW_DIR; the code repository and the
base branch come from its project.toml. The ticket ID is the workspace name.

create, workspace already a worktree of the code repo on symphony/<ID>
(the core re-ran after_create on a reused folder): only restore the links,
the exclusions and a missing base record. Never rename the branch, never
run setup.sh: that would destroy the ticket's work.

create, otherwise (clean start): under the shared worktree lock, the
standard `_worktree_cleanup --prepare-setup`; an existing symphony/<ID>
becomes attempts/<ID>-<UTC> (and artifacts/<ID> moves aside the same
way); `git worktree add` from refs/heads/<base>; worktree config, base
record, links; then setup.sh. Any failure after the add rolls everything
back and exits non-zero.

Start branch (S4-11): with [integration] enabled, a card labelled
`role:worker` with `request: <TASK>` starts from refs/heads/<branch_prefix>
<TASK> instead of the base branch (symphony.basebranch and
symphony.mergetargetbranch become that branch). The branch is created from
the base branch if missing, under the same lock, before park(); an existing
one is never recreated or moved. With [integration] off the card is not read.
The base record keeps `base_branch` (the project's, what baseline.json was
measured on) and adds `start_branch` and `task`. A rollback never deletes
the integration branch: another subtask may already stand on it.

remove: drop only the links that point into the board folder, then the
standard `_worktree_cleanup` with SYMPHONY_WORKFLOW_DIR = code repository.
Branches are never touched.

Standard library only, Python >= 3.11, POSIX (locks through fcntl).
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import integrate
import project_config as pc

LOCK_WAIT_S = 120
SETUP_MARGIN_S = 30


class HookError(Exception):
    """A refusal with a message for the operator."""


def say(text: str) -> None:
    print(f"worktree.py: {text}", file=sys.stderr, flush=True)


def git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
    )
    if check and proc.returncode != 0:
        raise HookError(f"git {' '.join(args)}: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip() if proc.returncode == 0 else ""


def has_ref(repo: Path, ref: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(repo), "show-ref", "--verify", "--quiet", ref], check=False
    ).returncode == 0


def common_dir(path: Path) -> Path:
    return (path / git(path, "rev-parse", "--git-common-dir")).resolve()


@contextmanager
def worktree_lock(common: Path):
    # Same file as scripts/symphony-setup-worktree.sh (flock(1) = flock(2)).
    with open(common / "symphony-worktree.lock", "a") as handle:
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise HookError(f"не дождалась блокировки {handle.name}") from None
                time.sleep(0.2)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Ticket:
    def __init__(self) -> None:
        raw = os.environ.get("SYMPHONY_WORKFLOW_DIR", "").strip()
        if not raw:
            raise HookError("SYMPHONY_WORKFLOW_DIR не задан")
        self.board = Path(raw).resolve()
        self.project = pc.load_project(self.board)
        p = self.project["project"]
        self.repo = Path(p["repo"])
        self.base = p["base_branch"]
        self.prefix = p["id_prefix"]
        self.workspace = Path.cwd().absolute()
        self.id = self.workspace.name
        self.branch = f"symphony/{self.id}"
        self.python = os.environ.get("SYMPHONY_PYTHON") or self.project["tools"]["python"]

    def link_targets(self) -> list[tuple[Path, Path]]:
        """(link in the workspace, target in the board folder)."""
        return [
            (self.workspace / pc.BOARD_DIR, self.board / pc.BOARD_DIR),
            (self.workspace / "docs" / self.id, self.board / pc.ARTIFACTS_DIR / self.id),
            (self.workspace / "docs" / "llm-wiki", self.board / pc.WIKI_DIR),
        ]

    def cleanup_module(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "SYMPHONY_WORKFLOW_DIR": str(self.repo)}
        return subprocess.run(
            [self.python, "-m", "symphony._worktree_cleanup", str(self.workspace), *args],
            cwd=str(self.repo), env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, check=False,
        )


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def preflight(t: Ticket) -> Path:
    if not t.id.startswith(f"{t.prefix}-") or t.id == f"{t.prefix}-":
        raise HookError(f"ID {t.id!r} не начинается с {t.prefix}- (префикс проекта)")
    # REPORT-09, item 17: git inside the board folder must find no repository.
    if subprocess.run(["git", "-C", str(t.board), "rev-parse", "--git-dir"],
                      capture_output=True, check=False).returncode == 0:
        raise HookError(f"папка доски {t.board} лежит внутри git-репозитория; так нельзя")
    top = git(t.repo, "rev-parse", "--show-toplevel", check=False)
    if not top or Path(top).resolve() != t.repo.resolve():
        raise HookError(f"{t.repo} — не корень git-репозитория")
    if not has_ref(t.repo, f"refs/heads/{t.base}"):
        raise HookError(f"в {t.repo} нет ветки refs/heads/{t.base}")
    return common_dir(t.repo)


def is_ticket_worktree(t: Ticket, common: Path) -> bool:
    if not (t.workspace / ".git").is_file():
        return False
    try:
        same_repo = common_dir(t.workspace) == common
    except HookError:
        return False
    head = git(t.workspace, "symbolic-ref", "--quiet", "HEAD", check=False)
    return same_repo and head == f"refs/heads/{t.branch}"


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def restore_exclusions(t: Ticket) -> None:
    exclude = t.board / pc.EXCLUDE_FILE
    text = pc.exclude_text(t.prefix)
    if not exclude.is_file() or exclude.read_text(encoding="utf-8") != text:
        exclude.write_text(text, encoding="utf-8")
    git(t.workspace, "config", "extensions.worktreeConfig", "true")
    git(t.workspace, "config", "--worktree", "core.excludesFile", str(exclude))


def restore_links(t: Ticket) -> None:
    artifacts = t.board / pc.ARTIFACTS_DIR / t.id
    for sub in ("work", "qa"):
        (artifacts / sub).mkdir(parents=True, exist_ok=True)
    (t.board / pc.WIKI_DIR).mkdir(exist_ok=True)
    docs = t.workspace / "docs"
    if docs.is_symlink() or (docs.exists() and not docs.is_dir()):
        raise HookError("docs в рабочей копии — не обычная папка; ссылки не создать")
    docs.mkdir(exist_ok=True)
    for link, target in t.link_targets():
        if link.is_symlink():
            if link.resolve() == target.resolve():
                continue
            link.unlink()
        elif link.exists():
            if link.name == "llm-wiki":
                say("предупреждение: docs/llm-wiki уже есть в репозитории кода; "
                    "ссылку на wiki доски не создаю, wiki агента попадёт в ветку")
                continue
            raise HookError(f"{link.relative_to(t.workspace)} уже есть в репозитории кода; "
                            "ссылку в папку доски не создать")
        link.symlink_to(target, target_is_directory=True)


def write_base_record(t: Ticket, sha: str, start_branch: str | None = None,
                      task: str | None = None) -> None:
    pc.ensure_state_dir(t.board)
    write_json(pc.base_record_path(t.board, t.id), {
        "base_sha": sha,
        "base_branch": t.base,
        "start_branch": start_branch or t.base,
        "task": task,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


def refresh(t: Ticket) -> None:
    say(f"{t.workspace} уже рабочая копия на {t.branch}: только восстанавливаю "
        "ссылки, исключения и запись базы; ветку и setup.sh не трогаю")
    restore_exclusions(t)
    restore_links(t)
    if not pc.base_record_path(t.board, t.id).exists():
        sha = git(t.workspace, "config", "--worktree", "--get", "symphony.basesha", check=False)
        if sha:
            write_base_record(t, sha)
        else:
            say("предупреждение: нет ни записи базы, ни symphony.basesha; "
                "check.py откажет до ручного разбора")


class Plan:
    """What a clean start changed, so a failure can undo exactly that."""

    def __init__(self) -> None:
        self.parked_branch: str | None = None
        self.parked_artifacts: Path | None = None
        self.old_record: bytes | None = None
        self.added = False


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def park(t: Ticket, plan: Plan) -> None:
    if has_ref(t.repo, f"refs/heads/{t.branch}"):
        name, n = f"attempts/{t.id}-{stamp()}", 1
        while has_ref(t.repo, f"refs/heads/{name}"):
            n += 1
            name = f"attempts/{t.id}-{stamp()}-{n}"
        git(t.repo, "branch", "-m", t.branch, name)
        plan.parked_branch = name
        say(f"чистый старт: прежняя ветка {t.branch} переименована в {name}")
    artifacts = t.board / pc.ARTIFACTS_DIR / t.id
    if artifacts.is_dir() and any(artifacts.rglob("*")):
        # Stale evidence would satisfy the stage contracts of the new attempt.
        aside = t.board / pc.ARTIFACTS_DIR / ".attempts" / f"{t.id}-{stamp()}"
        aside.parent.mkdir(parents=True, exist_ok=True)
        artifacts.rename(aside)
        plan.parked_artifacts = aside
        say(f"чистый старт: прежние артефакты перенесены в {aside}")
    record = pc.base_record_path(t.board, t.id)
    plan.old_record = record.read_bytes() if record.exists() else None


def rollback(t: Ticket, plan: Plan) -> None:
    """Undo a clean start. The integration branch (S4-11) is never deleted, even
    if this start created it: another subtask may already stand on it."""
    problems = []

    def step(fn, *args) -> None:
        try:
            fn(*args)
        except (HookError, OSError) as exc:
            problems.append(str(exc))

    if plan.added:
        step(git, t.repo, "worktree", "remove", "--force", str(t.workspace))
        step(git, t.repo, "worktree", "prune")
        step(git, t.repo, "branch", "-D", t.branch)
    if plan.parked_branch:
        step(git, t.repo, "branch", "-m", plan.parked_branch, t.branch)
    artifacts = t.board / pc.ARTIFACTS_DIR / t.id
    if artifacts.exists():
        step(shutil.rmtree, artifacts)
    if plan.parked_artifacts:
        step(plan.parked_artifacts.rename, artifacts)
    record = pc.base_record_path(t.board, t.id)
    if plan.old_record is None:
        step(record.unlink, True)
    else:
        step(record.write_bytes, plan.old_record)
    for problem in problems:
        say(f"откат: {problem}")
    say("откат выполнен" if not problems else "откат выполнен не полностью")


def run_setup(t: Ticket) -> int:
    setup = t.board / "setup.sh"
    budget = max(30, t.project["timeouts"]["hooks_ms"] // 1000 - SETUP_MARGIN_S)
    proc = subprocess.Popen(["/bin/bash", str(setup)], cwd=str(t.workspace),
                            stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        return proc.wait(timeout=budget)
    except subprocess.TimeoutExpired:
        say(f"setup.sh не уложился в {budget} с (hooks_ms − {SETUP_MARGIN_S} с)")
        return 124
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()


def subtask_task(t: Ticket) -> str | None:
    """The task this card is a subtask of, or None (also when [integration] is off)."""
    if not t.project["integration"]["enabled"]:
        return None
    card = t.board / pc.BOARD_DIR / f"{t.id}.md"
    if not card.is_file():
        raise HookError(f"нет карточки {card}: интеграционная ветка требует прочитать её шапку")
    return integrate.task_of(integrate.read_card_front(card), t.project, t.id)


def create(t: Ticket) -> int:
    common = preflight(t)
    if is_ticket_worktree(t, common):
        refresh(t)
        return 0
    task = subtask_task(t)
    start = integrate.integration_branch(t.project, task) if task else t.base
    plan = Plan()
    with worktree_lock(common):
        prepared = t.cleanup_module("--prepare-setup", t.branch)
        if prepared.returncode != 0:
            raise HookError("штатная подготовка (_worktree_cleanup --prepare-setup) отказала: "
                            + (prepared.stderr or prepared.stdout).strip()[-500:])
        if task:
            # Not undone by rollback(): another subtask may already stand on it.
            _, created = integrate.ensure_integration_branch(t.repo, start, t.base)
            if created:
                say(f"ветка {start} создана от {t.base}")
        park(t, plan)
        try:
            git(t.repo, "worktree", "add", str(t.workspace), "-b", t.branch,
                f"refs/heads/{start}")
            plan.added = True
            sha = git(t.workspace, "rev-parse", "HEAD")
            restore_exclusions(t)
            for key, value in (("symphony.basesha", sha), ("symphony.basebranch", start),
                               ("symphony.mergetargetbranch", start)):
                git(t.workspace, "config", "--worktree", key, value)
            git(t.workspace, "config", "--worktree", "--unset-all",
                "symphony.autocommitExclude", check=False)
            for path in (pc.BOARD_DIR, f"docs/{t.id}", "docs/llm-wiki"):
                git(t.workspace, "config", "--worktree", "--add", "symphony.autocommitExclude", path)
            write_base_record(t, sha, start, task)
            restore_links(t)
        except BaseException:
            rollback(t, plan)
            raise
    code = run_setup(t)
    if code != 0:
        say(f"setup.sh завершился с кодом {code}: убираю рабочую копию и новую ветку")
        with worktree_lock(common):
            rollback(t, plan)
        return 1
    origin = f"от {start} (задача {task}, {sha[:12]})" if task else f"от {start} ({sha[:12]})"
    say(f"рабочая копия {t.workspace} на {t.branch} {origin}")
    return 0


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def remove(t: Ticket) -> int:
    for link, _ in t.link_targets():
        if link.is_symlink() and t.board in Path(os.path.realpath(link)).parents:
            link.unlink()
    result = t.cleanup_module()
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args not in (["create"], ["remove"]):
        say("использование: worktree.py create|remove")
        return 2
    try:
        ticket = Ticket()
        return create(ticket) if args[0] == "create" else remove(ticket)
    except (HookError, integrate.IntegrateError, pc.ProjectError) as exc:
        say(f"отказ: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
