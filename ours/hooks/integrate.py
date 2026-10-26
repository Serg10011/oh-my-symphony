#!/usr/bin/env python3
"""Integration branch of a task (TASK-S4-11; architect's decision on REPORT-17,
part I, item 1).

Subtasks of one task (cards labelled `role:worker` with `request: <TASK>`)
must see each other's code. They start from the end of the task's shared
branch `<branch_prefix><TASK>` (default `integration/<TASK>`) instead of the
project's base branch. Finished subtasks are merged into it by `hook` (S4-12).

    integrate.py ensure-branch <TASK> [--board <board folder>]

Creates the branch from refs/heads/<base_branch> if it does not exist yet; an
existing branch is never recreated or moved. The board folder defaults to
$SYMPHONY_WORKFLOW_DIR. Exit codes: 0 done, 1 refusal with a reason,
2 bad arguments.

    integrate.py hook

Last line of hooks.after_run (TASK-S4-12). Merges the finished subtask into
its integration branch and moves its card to Done (see hook_main). Always
exits 0; the outcome is in <board>/.ours-state/check.log (mode "integrate")
and in the `## Integration` section of the card. A card that is not in Human
Review is passed over silently (no log line: the hook runs after every turn).
After Done it leaves <board>/.ours-state/<ID>.merged.json (S4-13): the tree
that was merged, so that the done gate does not run the tests a second time.

The branch is NOT called symphony/<TASK>: the task's own manager card is run
by an agent in a worktree on that very name, and worktree.py renames it on a
clean start, which would destroy the integration branch.

As a library (worktree.py): read_card_front, task_of, integration_branch,
ensure_integration_branch. ensure_integration_branch takes no lock; the caller
holds the shared worktree lock (the CLI does).

Standard library only, Python >= 3.11, POSIX.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import check
import project_config as pc

TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:(?:\s+(.*?))?\s*$")
_ITEM_RE = re.compile(r"^\s*-(?:\s+(.*?))?\s*$")
_QUOTED_RE = re.compile(r"""^(['"])(.*?)\1\s*(?:#.*)?$""")
_OID_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class IntegrateError(Exception):
    """A refusal with a message for the operator."""


def say(text: str) -> None:
    print(f"integrate.py: {text}", flush=True)


# ---------------------------------------------------------------------------
# card front matter
# ---------------------------------------------------------------------------


def _scalar(raw: str) -> str:
    """A YAML scalar without quotes and without a trailing ` #` comment."""
    raw = raw.strip()
    quoted = _QUOTED_RE.match(raw)
    if quoted:
        return quoted.group(2).strip()
    return raw.split(" #", 1)[0].strip()


def _inline_list(raw: str) -> tuple[str, ...]:
    body = raw.split(" #", 1)[0].strip()
    if not (body.startswith("[") and body.endswith("]")):
        raise IntegrateError(f"labels: ожидался список [a, b], а не {raw!r}")
    items = (_scalar(part) for part in body[1:-1].split(","))
    return tuple(item.lower() for item in items if item)


def read_card_front(path: Path, *, with_title: bool = False) -> dict:
    """{state, request, labels} of a card file, without PyYAML.

    `request` is a string or None; `labels` a tuple, lower-cased (the core
    lower-cases labels too). Both list forms the file tracker writes are read:
    `labels: [a, b]` and a block of `- a` lines. Other keys are ignored, except
    `title` with with_title=True: it is added to the result ("" if absent).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise IntegrateError(f"карточка {path} не читается: {exc}") from None
    front, _ = check.split_card(text)
    state: str | None = None
    request: str | None = None
    title = ""
    labels: tuple[str, ...] = ()
    index = 0
    while index < len(front):
        match = _KEY_RE.match(front[index])
        index += 1
        if not match:
            continue
        key, raw = match.group(1), match.group(2) or ""
        if key == "state":
            state = _scalar(raw)
        elif key == "request":
            request = _scalar(raw) or None
        elif key == "title":
            title = _scalar(raw)
        elif key == "labels":
            if raw and not raw.startswith("#"):
                if _scalar(raw) in ("null", "~"):
                    labels = ()
                else:
                    labels = _inline_list(raw)
                continue
            items: list[str] = []
            while index < len(front):
                item = _ITEM_RE.match(front[index])
                if item is None:
                    if front[index].strip():
                        break
                else:
                    items.append(_scalar(item.group(1) or ""))
                index += 1
            labels = tuple(item.lower() for item in items if item)
    if not state:
        raise IntegrateError(f"в шапке карточки {path.name} нет state")
    front = {"state": state, "request": request, "labels": labels}
    if with_title:
        front["title"] = title
    return front


def check_task_id(task: str, project: dict, card_id: str | None = None) -> str:
    """`task` if it can be the ID of a task of this project, else IntegrateError."""
    prefix = project["project"]["id_prefix"]
    if not TASK_RE.match(task):
        raise IntegrateError(f"ID задачи {task!r} не подходит под {TASK_RE.pattern}")
    if not task.startswith(f"{prefix}-") or task == f"{prefix}-":
        raise IntegrateError(f"ID задачи {task!r} не начинается с {prefix}- (префикс проекта)")
    if card_id is not None and task == card_id:
        raise IntegrateError(f"карточка {card_id} указывает request на саму себя")
    return task


def task_of(front: dict, project: dict, card_id: str | None = None) -> str | None:
    """The task a subtask belongs to, or None for an ordinary card.

    None when [integration] is off, the card lacks the worker label or has no
    `request`. A `request` that is present but wrong stops the start: quietly
    starting from the base branch would hide a broken link.
    """
    integration = project["integration"]
    if not integration["enabled"]:
        return None
    if integration["worker_label"].lower() not in front["labels"]:
        return None
    request = front["request"]
    if request is None:
        return None
    return check_task_id(request, project, card_id)


def integration_branch(project: dict, task: str) -> str:
    return project["integration"]["branch_prefix"] + task


# ---------------------------------------------------------------------------
# the branch
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
    )
    return proc.returncode, (proc.stdout if proc.returncode == 0 else proc.stderr).strip()


def _has_branch(repo: Path, branch: str) -> bool:
    return _git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")[0] == 0


def _tip(repo: Path, branch: str) -> str:
    code, out = _git(repo, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")
    if code != 0:
        raise IntegrateError(f"не прочитать конец ветки {branch}: {out[:300]}")
    return out


def ensure_integration_branch(repo: Path, branch: str, base_branch: str) -> tuple[str, bool]:
    """(sha, created): the branch tip; created from the base branch if missing.

    An existing branch is returned as it is, never recreated or moved. No
    lock here: the caller holds the shared worktree lock.
    """
    code, out = _git(repo, "check-ref-format", "--branch", branch)
    if code != 0 or out != branch:
        raise IntegrateError(f"имя ветки {branch!r} не годится для git")
    if _has_branch(repo, branch):
        return _tip(repo, branch), False
    if not _has_branch(repo, base_branch):
        raise IntegrateError(f"в {repo} нет ветки refs/heads/{base_branch}")
    code, out = _git(repo, "branch", branch, f"refs/heads/{base_branch}")
    if code != 0:
        raise IntegrateError(f"git branch {branch}: {out[:300]}")
    return _tip(repo, branch), True


# ---------------------------------------------------------------------------
# hook: merge a finished subtask into the integration branch (S4-12)
# ---------------------------------------------------------------------------

MODE = "integrate"
STATE_REVIEW = "Human Review"
STATE_DONE = "Done"
STATE_ESCALATE = "needs_human"
NOTE_HEADING = "Integration"
IDENTITY = {"NAME": "symphony", "EMAIL": "symphony@local"}
LOCK_WAIT_S = 5
MAX_LISTED_FILES = 20


class Conflict(Exception):
    """The subtask cannot be merged; the branch is unchanged."""

    def __init__(self, reason: str, files: tuple[str, ...] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.files = files


def _git_run(repo: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """git in the code repository: (code, stdout, stderr), never raises on a non-zero code."""
    try:
        return subprocess.run(
            ["git", *check.GIT_SAFE_ARGS, *args], cwd=str(repo), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, check=False, timeout=check.GIT_TIMEOUT_S,
            env={**check._git_env(), **(env or {})},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IntegrateError(f"git {args[0]}: {type(exc).__name__}: {exc}") from None


def _git_out(repo: Path, *args: str) -> str:
    proc = _git_run(repo, *args)
    if proc.returncode != 0:
        raise IntegrateError(f"git {args[0]} (код {proc.returncode}): {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


def _merge_trees(repo: Path, old: str, sub: str) -> str:
    """Tree of the merge of `sub` into `old`; Conflict when git reports one."""
    proc = _git_run(repo, "merge-tree", "--write-tree", "--name-only", old, sub)
    lines = proc.stdout.split("\n")
    if proc.returncode not in (0, 1) or not _OID_RE.match(lines[0].strip()):
        raise IntegrateError(
            f"git merge-tree (код {proc.returncode}): {(proc.stderr or proc.stdout).strip()[:300]}"
        )
    if proc.returncode == 1:
        files = []
        for line in lines[1:]:
            if not line.strip():
                break  # the rest is informational messages
            files.append(line.strip())
        raise Conflict("слияние с интеграционной веткой даёт конфликт", tuple(files))
    return lines[0].strip()


def _commit_message(card_id: str, title: str, task: str, sub: str) -> list[str]:
    subject = check._clean(f"{card_id}: {title}".strip().rstrip(":"))[:200]
    return ["-m", subject, "-m", f"Task: {task}\nSource: symphony/{card_id}@{sub}"]


def merge_subtask(repo: Path, branch: str, card_id: str, title: str, task: str, sub: str) -> dict:
    """Merge commit `sub` into refs/heads/<branch> as one flat commit.

    git merge-tree -> git commit-tree (one parent: the old tip) -> git
    update-ref with the old value checked. If the branch moved in between,
    one retry from the new tip, then Conflict("ветка сдвинулась"). Returns
    {"old_tip", "commit"}; commit is None when the merge changes nothing.
    """
    for attempt in (1, 2):
        old = _tip(repo, branch)
        tree = _merge_trees(repo, old, sub)
        if tree == _git_out(repo, "rev-parse", f"{old}^{{tree}}"):
            return {"old_tip": old, "commit": None}
        env = {f"GIT_{role}_{key}": value for role in ("AUTHOR", "COMMITTER") for key, value in IDENTITY.items()}
        proc = _git_run(
            repo, "commit-tree", tree, "-p", old, *_commit_message(card_id, title, task, sub), env=env
        )
        if proc.returncode != 0 or not _OID_RE.match(proc.stdout.strip()):
            raise IntegrateError(f"git commit-tree (код {proc.returncode}): {proc.stderr.strip()[:300]}")
        new = proc.stdout.strip()
        moved = _git_run(repo, "update-ref", "-m", f"integrate {card_id}", f"refs/heads/{branch}", new, old)
        if moved.returncode == 0:
            return {"old_tip": old, "commit": new}
        if attempt == 2:
            raise Conflict(
                f"ветка {branch} сдвинулась дважды за время слияния: {moved.stderr.strip()[:200]}"
            )
    raise AssertionError("unreachable")  # pragma: no cover


def _board_mv(ctx: check.Context, cfg: check.Config | None, target: str) -> str | None:
    """Move the card with `symphony board mv`; None on success, else what went wrong."""
    binary = check.resolve_symphony_bin(cfg)
    if binary is None:
        return "symphony binary not found ([tools] symphony / SYMPHONY_PYTHON)"
    try:
        proc = subprocess.run(
            [binary, "board", "mv", ctx.issue_id, target, "--root", str(ctx.workflow_dir / check.BOARD_DIR)],
            cwd=str(ctx.workflow_dir), stdin=subprocess.DEVNULL, capture_output=True,
            timeout=check.SYMPHONY_TIMEOUT_S, check=False,
        )
        detail = (proc.stdout + proc.stderr).decode("utf-8", errors="replace").strip()
        rc: int | None = proc.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        detail, rc = f"{type(exc).__name__}: {exc}", None
    try:
        now = check.read_card_state(ctx.card)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        now = f"<unreadable: {exc}>"
    if now.lower() == target.lower():
        return None
    return f"board mv to {target!r} did not take effect (exit {rc}, state now {now!r}): {detail[:300]}"


def _note(ctx: check.Context, lines: list[str]) -> str:
    try:
        return check.append_note(ctx, "\n".join(lines), NOTE_HEADING)
    except Exception as exc:  # noqa: BLE001 - the card note is secondary to the outcome
        _report(ctx, f"cannot append the note: {type(exc).__name__}: {exc}")
        return "failed"


def _report(ctx: check.Context, text: str) -> None:
    check.log_event(ctx, ctx.issue_id, MODE, "error", text)
    check._emit(f"integrate.py hook: {text}", stream=sys.stderr)


def _skip(ctx: check.Context, reason: str) -> int:
    check.log_event(ctx, ctx.issue_id, MODE, "skipped", reason)
    return 0


def _read_pass_mark(ctx: check.Context) -> dict | None:
    """The one-shot mark of check.py hook; removed whatever it says."""
    path = check.pass_path(ctx)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    try:
        path.unlink()
    except OSError:
        pass
    return data if isinstance(data, dict) else None


def _dirty_files(workspace: Path) -> list[str]:
    # The same pathspec as the standard block and worktree_tree: `.symphony` and
    # the board link are not the agent's work.
    out = check.git(
        workspace, "status", "--porcelain", "-z", "--untracked-files=all", "--", *check.STAGE_PATHSPEC
    )
    return [item[3:] for item in check._z(out)]


def _run_hook(ctx: check.Context, project: dict) -> int:
    integration = project["integration"]
    if not integration["enabled"]:
        return 0
    front = read_card_front(ctx.card, with_title=True)
    if front["state"].lower() != STATE_REVIEW.lower():
        return 0  # silent: after_run runs after every turn of every card
    task = task_of(front, project, ctx.issue_id)
    if task is None:
        return _skip(ctx, "карточка не подзадача (нет метки, request или [integration] выключен)")
    record = pc.read_base_record(ctx.workflow_dir, ctx.issue_id)
    if record.get("task") != task:
        return _skip(ctx, f"карточка стартовала не от интеграционной ветки задачи {task} "
                          f"(в записи базы task = {record.get('task')!r})")
    lock = check.try_lock(ctx, LOCK_WAIT_S)
    if lock is None:
        return _skip(ctx, "проверка или шлюз этой карточки ещё идёт (блокировка занята)")
    with lock:
        return _integrate(ctx, project, front, task)


def _integrate(ctx: check.Context, project: dict, front: dict, task: str) -> int:
    mark = _read_pass_mark(ctx)
    if mark is None or mark.get("ppid") != os.getppid() or not isinstance(mark.get("tree"), str):
        return _skip(ctx, "проверка этого хода не пройдена (нет отметки .pass.json или она от другого хода)")
    ws = ctx.workspace
    if check.git(ws, "rev-parse", "HEAD^{tree}").decode().strip() != mark["tree"]:
        return _skip(ctx, "содержимое не совпадает с проверенным: после проверки файлы менялись")
    dirty = _dirty_files(ws)
    if dirty:
        shown = ", ".join(check._clean(name) for name in dirty[:MAX_LISTED_FILES])
        return _skip(ctx, f"рабочая копия не закоммичена, вливать нельзя: {shown}")
    sub = check.git(ws, "rev-parse", "HEAD").decode().strip()
    repo = Path(project["project"]["repo"])
    branch = integration_branch(project, task)
    source = f"symphony/{ctx.issue_id}@{sub}"
    try:
        cfg: check.Config | None = check.load_config(ctx.workflow_dir)
    except check.CheckError:
        cfg = None
    try:
        result = merge_subtask(repo, branch, ctx.issue_id, front.get("title", ""), task, sub)
    except Conflict as exc:
        return _conflict(ctx, cfg, task, branch, source, exc)
    return _done(ctx, cfg, task, branch, source, result, mark["tree"])


def _head(task: str, branch: str, source: str) -> list[str]:
    return [f"- at: {check._now()}", f"- task: {task}", f"- branch: {branch}", f"- source: {source}"]


def _write_merged(ctx: check.Context, task: str, branch: str, source: str, tree: str,
                  commit: str | None) -> str:
    """The one-shot record check.py gate reads; "written" or "failed"."""
    payload = {"tree": tree, "commit": commit, "task": task, "branch": branch,
               "source": source, "at": check._now()}
    try:
        check.ensure_state_dir(ctx.state_dir)
        check._write_atomic(
            check.merged_path(ctx), (json.dumps(payload, indent=2) + "\n").encode("utf-8")
        )
    except OSError as exc:
        _report(ctx, f"cannot write the merged record: {exc}")
        return "failed"
    return "written"


def _done(ctx: check.Context, cfg: check.Config | None, task: str, branch: str, source: str,
          result: dict, tree: str) -> int:
    commit = result["commit"] or "no changes"
    failure = _board_mv(ctx, cfg, STATE_DONE)
    # Only after Done took effect: without Done the gate is not coming, and a
    # gate run by hand later must test in full.
    record = "not written" if failure is not None else _write_merged(
        ctx, task, branch, source, tree, result["commit"])
    lines = [*_head(task, branch, source), f"- commit: {commit}", "- check: passed (check.py hook)",
             f"- moved to: {STATE_DONE}" if failure is None else f"- moved to: {STATE_DONE} FAILED (see check.log)"]
    note_via = _note(ctx, lines)
    check.log_event(ctx, ctx.issue_id, MODE, "done", "merged into the integration branch",
                    task=task, branch=branch, commit=commit, old_tip=result["old_tip"], note=note_via,
                    merged_record=record)
    if failure is not None:
        _report(ctx, f"the code is merged ({commit}) but the card is not Done: {failure}")
    return 0


def _conflict(ctx: check.Context, cfg: check.Config | None, task: str, branch: str, source: str,
              exc: Conflict) -> int:
    failure = _board_mv(ctx, cfg, STATE_ESCALATE)
    lines = [*_head(task, branch, source), "- result: conflict", f"- reason: {check._clean(exc.reason)}"]
    if exc.files:
        lines += ["- files:", *(f"  - {check._clean(name)}" for name in exc.files[:MAX_LISTED_FILES])]
    lines += [f"- moved to: {STATE_ESCALATE}" if failure is None
              else f"- moved to: {STATE_ESCALATE} FAILED (see check.log)",
              "", "Интеграционная ветка не изменена; разбор — человек."]
    note_via = _note(ctx, lines)
    check.log_event(ctx, ctx.issue_id, MODE, "conflict", exc.reason, task=task, branch=branch,
                    files=", ".join(check._clean(name) for name in exc.files), note=note_via)
    if failure is not None:
        _report(ctx, f"conflict, but the card did not move to {STATE_ESCALATE}: {failure}")
    return 0


def hook_main() -> int:
    """hooks.after_run, last line. Always 0: it must never break the turn."""
    try:
        ctx = check.build_context(MODE)
    except check.CheckError as exc:
        check._emit(f"integrate.py hook: {exc}", stream=sys.stderr)
        try:
            check.log_event(check.workflow_dir_from_env() / check.STATE_DIR, "?", MODE, "error", str(exc))
        except check.CheckError:
            pass
        return 0
    try:
        return _run_hook(ctx, pc.load_project(ctx.workflow_dir))
    except BaseException as exc:  # noqa: BLE001 - the hook never fails the turn
        _report(ctx, f"{type(exc).__name__}: {exc}")
        return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

USAGE = "использование: integrate.py ensure-branch <ЗАДАЧА> [--board <папка доски>] | integrate.py hook"


def _parse(args: list[str]) -> tuple[str, Path] | None:
    board: str | None = None
    rest: list[str] = []
    it = iter(args)
    for arg in it:
        if arg == "--board":
            board = next(it, None)
            if not board:
                return None
        else:
            rest.append(arg)
    if len(rest) != 2 or rest[0] != "ensure-branch" or rest[1].startswith("-"):
        return None
    raw = board if board is not None else os.environ.get("SYMPHONY_WORKFLOW_DIR", "").strip()
    if not raw:
        return None
    return rest[1], Path(raw).resolve()


def ensure_branch_command(task: str, board: Path) -> int:
    import worktree  # lazy: worktree.py imports this module

    project = pc.load_project(board)
    check_task_id(task, project)
    repo = Path(project["project"]["repo"])
    base = project["project"]["base_branch"]
    branch = integration_branch(project, task)
    common = worktree.common_dir(repo)
    with worktree.worktree_lock(common):
        sha, created = ensure_integration_branch(repo, branch, base)
    if created:
        say(f"ветка {branch} создана от {base} ({sha[:12]})")
    else:
        say(f"ветка {branch} уже есть ({sha[:12]})")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["hook"]:
        return hook_main()
    parsed = _parse(args)
    if parsed is None:
        print(f"integrate.py: {USAGE} (без --board берётся $SYMPHONY_WORKFLOW_DIR)", file=sys.stderr)
        return 2
    import worktree  # lazy: worktree.py imports this module

    try:
        return ensure_branch_command(*parsed)
    except (IntegrateError, pc.ProjectError, worktree.HookError) as exc:
        print(f"integrate.py: отказ: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
