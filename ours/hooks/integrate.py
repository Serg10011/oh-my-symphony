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
Then (S4-21) it writes `- <ID> merged (k of N)` into `## Subtask Progress` of the
task card and, when every subtask of `## Plan Accepted` is Done, calls the final
check of the task (finish_task) - after leaving every lock of the merge.

    integrate.py finish-task <TASK> [--board <board folder>]

The same final check by hand (the last subtask was moved to Done by a human).
The task card (label `role:manager-plan`, section `## Plan Accepted`) must be in
`Subtasks` and every subtask Done. Under the task's card lock and the worktree
lock: integration/<TASK> is merged with the base branch (a real two-parent
commit, or nothing when the base did not move), the task's working copy is put
on the result, the diff files for Verify are written, the base record moves to
the new base, `.final.json` is written and the card goes to `Verify`. Any
problem: nothing in the branches changes, the card goes to `needs_human` with
`## Final Check`. Exit codes: 0 moved to Verify, 1 not (the reason is printed),
2 bad arguments.

The branch is NOT called symphony/<TASK>: the task's own manager card is run
by an agent in a worktree on that very name, and worktree.py renames it on a
clean start, which would destroy the integration branch.

As a library (worktree.py, check.py): read_card_front, is_subtask, task_of,
integration_branch, ensure_integration_branch. ensure_integration_branch takes no lock; the caller
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


def is_subtask(front: dict, integration: dict) -> bool:
    """A card is a subtask when [integration] is on, it carries the worker label
    and has a `request`. The one definition: worktree.py, the hook and check.py
    (card sections) all use it; the `request` value itself is checked by task_of."""
    return bool(
        integration["enabled"]
        and integration["worker_label"].lower() in front["labels"]
        and front["request"] is not None
    )


def task_of(front: dict, project: dict, card_id: str | None = None) -> str | None:
    """The task a subtask belongs to, or None for an ordinary card.

    None when [integration] is off, the card lacks the worker label or has no
    `request`. A `request` that is present but wrong stops the start: quietly
    starting from the base branch would hide a broken link.
    """
    if not is_subtask(front, project["integration"]):
        return None
    return check_task_id(front["request"], project, card_id)


def integration_branch(project: dict, task: str) -> str:
    return project["integration"]["branch_prefix"] + task


# ---------------------------------------------------------------------------
# the card of a task (S4-21)
# ---------------------------------------------------------------------------

MANAGER_LABEL = "role:manager-plan"
NOTE_ACCEPTED = "Plan Accepted"
NOTE_PROGRESS = "Subtask Progress"
STATE_SUBTASKS = "Subtasks"
_ACCEPTED_RE = re.compile(rf"^##\s+{re.escape(NOTE_ACCEPTED)}\s*$", re.IGNORECASE | re.MULTILINE)
_NEXT_HEADING_RE = re.compile(r"^##\s+\S", re.MULTILINE)
# A subtask line of `## Plan Accepted` (plan.py): `- <ID> — <title> — <level>`.
_PLAN_LINE_RE = re.compile(r"^-\s+([A-Za-z][A-Za-z0-9_-]{0,63}) — ", re.MULTILINE)


def is_task_card(front: dict, body: str) -> bool:
    """The one definition of a task card: the manager label and a `## Plan
    Accepted` section (plan.py writes it when it creates the subtask cards).
    Used by the final check, by check.py and by the progress lines."""
    return MANAGER_LABEL in front["labels"] and _ACCEPTED_RE.search(body) is not None


def plan_accepted_ids(body: str) -> list[str]:
    """IDs of the subtasks in `## Plan Accepted`, in the order of the section."""
    match = _ACCEPTED_RE.search(body)
    if match is None:
        return []
    after = body[match.end():]
    following = _NEXT_HEADING_RE.search(after)
    section = after if following is None else after[: following.start()]
    return _PLAN_LINE_RE.findall(section)


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


def _note(ctx: check.Context, lines: list[str], heading: str = NOTE_HEADING,
          issue_id: str | None = None) -> str:
    """The note goes to the card of `ctx`, or to `issue_id` (another card of the board)."""
    try:
        return check.append_note(ctx, "\n".join(lines), heading, issue_id)
    except Exception as exc:  # noqa: BLE001 - the card note is secondary to the outcome
        _report(ctx, f"cannot append the note {heading!r}: {type(exc).__name__}: {exc}")
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
        code, done = _integrate(ctx, project, front, task)
    # Every lock of this merge is released: the final check takes its own (flock
    # does not nest: a second open of the same lock file in this process waits
    # for itself), and the task card lock is another file than the subtask's.
    if done:
        _signal_task(ctx, project, task)
    return code


def _integrate(ctx: check.Context, project: dict, front: dict, task: str) -> tuple[int, bool]:
    """(exit code, True when the subtask is Done and the task may be ready)."""
    mark = _read_pass_mark(ctx)
    if mark is None or mark.get("ppid") != os.getppid() or not isinstance(mark.get("tree"), str):
        return _skip(ctx, "проверка этого хода не пройдена (нет отметки .pass.json или она от другого хода)"), False
    ws = ctx.workspace
    if check.git(ws, "rev-parse", "HEAD^{tree}").decode().strip() != mark["tree"]:
        return _skip(ctx, "содержимое не совпадает с проверенным: после проверки файлов менялись"), False
    dirty = _dirty_files(ws)
    if dirty:
        shown = ", ".join(check._clean(name) for name in dirty[:MAX_LISTED_FILES])
        return _skip(ctx, f"рабочая копия не закоммичена, вливать нельзя: {shown}"), False
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
        return _conflict(ctx, cfg, task, branch, source, exc), False
    return _done(ctx, cfg, project, task, branch, source, result, mark["tree"])


def _head(task: str, branch: str, source: str) -> list[str]:
    return [f"- at: {check._now()}", f"- task: {task}", f"- branch: {branch}", f"- source: {source}"]


def _write_merged(ctx: check.Context, task: str, branch: str, source: str, tree: str,
                  commit: str | None, *, source_commit: str | None = None,
                  source_tree: str | None = None) -> str:
    """The one-shot record check.py gate reads; "written" or "failed"."""
    payload = {"tree": tree, "commit": commit, "task": task, "branch": branch,
               "source": source, "at": check._now()}
    if source_commit is not None or source_tree is not None:
        payload.update(source_commit=source_commit, source_tree=source_tree)
    try:
        check.ensure_state_dir(ctx.state_dir)
        check._write_atomic(
            check.merged_path(ctx), (json.dumps(payload, indent=2) + "\n").encode("utf-8")
        )
    except OSError as exc:
        _report(ctx, f"cannot write the merged record: {exc}")
        return "failed"
    return "written"


def _done(ctx: check.Context, cfg: check.Config | None, project: dict, task: str, branch: str,
          source: str, result: dict, tree: str) -> tuple[int, bool]:
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
        return 0, False
    _progress(ctx, project, task)
    return 0, True


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


# ---------------------------------------------------------------------------
# progress of the task and the signal "every subtask is Done" (S4-21)
# ---------------------------------------------------------------------------


def _read_task_card(board: Path, task: str) -> tuple[dict, str]:
    """(front matter, body) of the task card; IntegrateError when unreadable."""
    path = board / pc.BOARD_DIR / f"{task}.md"
    front = read_card_front(path, with_title=True)
    return front, check.split_card(path.read_text(encoding="utf-8"))[1]


def _not_done(board: Path, ids: list[str]) -> list[str]:
    """`<ID> (<state>)` for every subtask that is not Done (a missing card too)."""
    waiting = []
    for ident in ids:
        try:
            state = read_card_front(board / pc.BOARD_DIR / f"{ident}.md")["state"]
        except IntegrateError:
            state = "нет карточки"
        if state.lower() != STATE_DONE.lower():
            waiting.append(f"{ident} ({state})")
    return waiting


def _progress(ctx: check.Context, project: dict, task: str) -> None:
    """`- <ID> merged (k of N)` into `## Subtask Progress` of the task card, under
    the lock of the TASK card. Any failure is an `error` event only: the subtask
    is Done already."""
    try:
        try:
            _, body = _read_task_card(ctx.workflow_dir, task)
        except IntegrateError:
            return  # no card of the task on this board: nothing to keep up
        ids = plan_accepted_ids(body)
        if not ids:
            return  # the task has no accepted plan (a subtask made by hand)
        if ctx.issue_id not in ids:
            _report(ctx, f"progress: {ctx.issue_id} is not in `## {NOTE_ACCEPTED}` of {task}; no line written")
            return
        done = len(ids) - len(_not_done(ctx.workflow_dir, ids))
        lock = check.try_lock(_task_ctx(ctx.workflow_dir, project, task), LOCK_WAIT_S)
        if lock is None:
            _report(ctx, f"progress: the lock of the task card {task} is busy; no line written")
            return
        with lock:
            via = check.append_note(ctx, f"- {ctx.issue_id} merged ({done} of {len(ids)})", NOTE_PROGRESS, task)
        check.log_event(ctx, ctx.issue_id, MODE, "progress", f"{done} of {len(ids)}", task=task, note=via)
    except Exception as exc:  # noqa: BLE001 - never touches the merge that is done
        _report(ctx, f"progress of {task}: {type(exc).__name__}: {exc}")


def _signal_task(ctx: check.Context, project: dict, task: str) -> None:
    """The subtask just became Done: when it was the last one, finish the task."""
    try:
        try:
            front, body = _read_task_card(ctx.workflow_dir, task)
        except IntegrateError:
            return  # no card of the task on this board
        ids = plan_accepted_ids(body)
        if (not is_task_card(front, body) or front["state"].lower() != STATE_SUBTASKS.lower()
                or not ids or _not_done(ctx.workflow_dir, ids)):
            return
        ok, text = finish_task(ctx.workflow_dir, project, task)
        if not ok:
            check._emit(f"integrate.py hook: {text}", stream=sys.stderr)
    except BaseException as exc:  # noqa: BLE001 - the subtask is Done, whatever happens here
        _report(ctx, f"final check of {task}: {type(exc).__name__}: {exc}")


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
# the final check of a task (S4-21)
# ---------------------------------------------------------------------------

STATE_VERIFY = "Verify"
NOTE_FINAL = "Final Check"
FINAL_SUFFIX = ".final.json"
DIFF_MD = "final-diff.md"
DIFF_PATCH = "final-diff.patch"
DIFF_LIMIT = 200 * 1024  # bytes of the patch kept for Verify
WORK_DIR = "work"


def workspace_root(project: dict) -> Path:
    """Where the working copies of this project are. The service tells its hooks
    ($SYMPHONY_WORKSPACE_ROOT); by hand the rendered WORKFLOW.md default is used."""
    raw = os.environ.get("SYMPHONY_WORKSPACE_ROOT", "").strip()
    return Path(raw) if raw else pc.default_workspace_root(project)


def _task_ctx(board: Path, project: dict, task: str) -> check.Context:
    """A context for the task card: its lock, its card, its working copy."""
    return check.Context(MODE, board, workspace_root(project) / task, task)


def final_path(board: Path, task: str) -> Path:
    return board / pc.STATE_DIR / f"{task}{FINAL_SUFFIX}"


def _git_bytes(repo: Path, *args: str) -> bytes:
    try:
        return check.git(repo, *args)
    except (check.CheckError, OSError, subprocess.TimeoutExpired) as exc:
        raise IntegrateError(f"git {args[0]}: {str(exc)[:300]}") from None


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    code = _git_run(repo, "merge-base", "--is-ancestor", ancestor, descendant).returncode
    if code not in (0, 1):
        raise IntegrateError(f"git merge-base --is-ancestor {ancestor[:12]} {descendant[:12]} (код {code})")
    return code == 0


def merge_with_base(repo: Path, branch: str, base_branch: str, task: str) -> dict:
    """Bring refs/heads/<base_branch> into refs/heads/<branch> with a real merge commit.

    {"int", "main", "target", "merged"}: `int` and `main` are the tips used, `target`
    the new tip of the branch. When `main` is an ancestor of `int` nothing is done
    (target = int). Otherwise git merge-tree -> git commit-tree with TWO parents
    (int, main) -> git update-ref with the old value checked; if the branch moved
    in between, one retry from the start, then Conflict. A flat commit would show
    the changes of the base branch as the changes of the task (architect, S4-21).
    """
    for attempt in (1, 2):
        int_sha, main_sha = _tip(repo, branch), _tip(repo, base_branch)
        if _is_ancestor(repo, main_sha, int_sha):
            return {"int": int_sha, "main": main_sha, "target": int_sha, "merged": False}
        try:
            tree = _merge_trees(repo, int_sha, main_sha)
        except Conflict as exc:
            raise Conflict(f"слияние {branch} с {base_branch} даёт конфликт", exc.files) from None
        env = {f"GIT_{role}_{key}": value for role in ("AUTHOR", "COMMITTER") for key, value in IDENTITY.items()}
        proc = _git_run(repo, "commit-tree", tree, "-p", int_sha, "-p", main_sha,
                        "-m", f"{task}: merge {base_branch} for the final check", env=env)
        if proc.returncode != 0 or not _OID_RE.match(proc.stdout.strip()):
            raise IntegrateError(f"git commit-tree (код {proc.returncode}): {proc.stderr.strip()[:300]}")
        new = proc.stdout.strip()
        moved = _git_run(repo, "update-ref", "-m", f"final check {task}", f"refs/heads/{branch}", new, int_sha)
        if moved.returncode == 0:
            return {"int": int_sha, "main": main_sha, "target": new, "merged": True}
        if attempt == 2:
            raise Conflict(f"ветка {branch} сдвинулась дважды за время слияния: {moved.stderr.strip()[:200]}")
    raise AssertionError("unreachable")  # pragma: no cover


def _write_work_file(board: Path, task: str, name: str, data: bytes) -> None:
    """<board>/artifacts/<task>/work/<name>; not through a link (the agent can replace
    `docs/<task>/work` with one), the same guard as plan.py."""
    directory = board / pc.ARTIFACTS_DIR / task / WORK_DIR
    expected = Path(os.path.realpath(board)) / pc.ARTIFACTS_DIR / task / WORK_DIR
    directory.mkdir(parents=True, exist_ok=True)
    if os.path.realpath(directory) != str(expected):
        raise IntegrateError(f"{directory} — ссылка или лежит не там: файлы диффа не пишу")
    check._write_atomic(directory / name, data)


def write_final_diff(repo: Path, board: Path, task: str, branch: str, base_branch: str,
                     main: str, target: str, merged: bool) -> None:
    """The diff of the task against the base branch for Verify, written by the
    program: `git diff` is not allowed to the agent and its rights are not widened."""
    span = f"{main}..{target}"
    flags = ("--no-ext-diff", "--no-textconv", "--no-color")
    names = _git_bytes(repo, "diff", *flags, "--name-status", span).decode("utf-8", errors="replace")
    stat = _git_bytes(repo, "diff", *flags, "--stat", span).decode("utf-8", errors="replace")
    patch = _git_bytes(repo, "diff", *flags, span)
    how = "merge commit of the final check" if merged else "no merge was needed"
    text = "\n".join([
        f"# Final diff of {task}", "",
        f"Written by ours/hooks/integrate.py at {check._now()}. `git diff` is not allowed for the agent: "
        "the program prepares the diff.", "",
        f"- compared: `{main}` ({base_branch}) .. `{target}` ({branch}, {how})",
        f"- full patch: `docs/{task}/work/{DIFF_PATCH}`", "",
        "## Files (git diff --name-status)", "", "```", names.rstrip() or "(no changes)", "```", "",
        "## Size (git diff --stat)", "", "```", stat.rstrip() or "(no changes)", "```", "",
    ])
    if len(patch) > DIFF_LIMIT:
        kept = patch[:DIFF_LIMIT].rsplit(b"\n", 1)[0] + b"\n"
        total_kb = -(-len(patch) // 1024)
        patch = kept + f"обрезано, {total_kb} КБ всего, полный дифф: git diff {main} {target}\n".encode()
    _write_work_file(board, task, DIFF_MD, text.encode("utf-8"))
    _write_work_file(board, task, DIFF_PATCH, patch)


def _final_lines(task: str, **fields: str) -> list[str]:
    return [f"- at: {check._now()}", f"- task: {task}", *(f"- {key}: {value}" for key, value in fields.items())]


def _final_stop(tctx: check.Context, cfg: check.Config | None, reasons: list[str], *,
                files: tuple[str, ...] = (), moved: list[str] | None = None) -> tuple[bool, str]:
    """The task card goes to needs_human with `## Final Check`; the event final:needs_human."""
    task = tctx.issue_id
    failure = _board_mv(tctx, cfg, STATE_ESCALATE)
    lines = _final_lines(task, result="needs_human",
                         **{"moved to": STATE_ESCALATE if failure is None else f"{STATE_ESCALATE} FAILED (see check.log)"})
    lines += ["", "Причины:", ""]
    lines += [f"- {check._clean(reason)}" for reason in reasons]
    if files:
        lines += ["", "Файлы с конфликтом:", ""]
        lines += [f"- {check._clean(name)}" for name in files[:MAX_LISTED_FILES]]
    lines += ["", "Сдвинуто в ветках:", ""]
    lines += [f"- {check._clean(item)}" for item in moved] if moved else ["- ничего: ветки и рабочая копия не менялись."]
    lines += ["", "Разбор — человек."]
    via = _note(tctx, lines, NOTE_FINAL)
    check.log_event(tctx, task, MODE, "final:needs_human", "; ".join(reasons), note=via,
                    files=", ".join(check._clean(name) for name in files),
                    moved="; ".join(moved or []))
    if failure is not None:
        _report(tctx, f"the final check stopped, but the card did not move to {STATE_ESCALATE}: {failure}")
    text = f"{task}: итоговая проверка не начата, карточка → {STATE_ESCALATE}. " + " ".join(reasons)
    if moved:
        text += " Уже сдвинуто: " + "; ".join(moved) + "."
    return False, text


def _workspace_problems(tctx: check.Context, repo: Path, common: Path, task: str) -> list[str]:
    import worktree  # lazy: worktree.py imports this module

    ws = tctx.workspace
    if not (ws / ".git").exists():
        return [f"нет рабочей копии карточки-задачи: {ws}"]
    problems = []
    try:
        top = _git_out(ws, "rev-parse", "--show-toplevel")
        if Path(top).resolve() != ws.resolve():
            problems.append(f"{ws} — не корень рабочей копии")
        if worktree.common_dir(ws) != common:
            problems.append(f"{ws} — рабочая копия не того репозитория, что {repo}")
        head = _git_run(ws, "symbolic-ref", "--quiet", "HEAD").stdout.strip()
        if head != f"refs/heads/symphony/{task}":
            problems.append(f"рабочая копия стоит не на symphony/{task} (HEAD: {head or 'отсоединён'})")
    except (IntegrateError, worktree.HookError) as exc:
        return [f"рабочая копия карточки-задачи {ws} не читается: {exc}"]
    if problems:
        return problems
    dirty = _dirty_files(ws)
    if dirty:
        shown = ", ".join(check._clean(name) for name in dirty[:MAX_LISTED_FILES])
        problems.append(f"рабочая копия карточки-задачи не чистая (есть незакоммиченное): {shown}")
    try:
        base = pc.read_base_record(tctx.workflow_dir, task)["base_sha"]
    except pc.ProjectError as exc:
        problems.append(str(exc))
    else:
        code = _git_run(ws, "diff", "--quiet", base, "HEAD").returncode
        if code == 1:
            problems.append("в ветке руководителя есть правки файлов (руководитель код не пишет)")
        elif code != 0:
            problems.append(f"не сравнить HEAD с записанной базой {base[:12]} (git diff, код {code})")
    return problems


def _write_state_json(path: Path, payload: dict) -> None:
    check._write_atomic(path, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _move_base(tctx: check.Context, base_branch: str, main: str) -> None:
    """The base record and the worktree config move to `main`, the end of the base
    branch the task was merged with. `symphony.mergetargetbranch` / `basebranch` must
    name the base branch (worktree.py gives a task card exactly that): the
    `## Merge Status` of Verify then checks the merge into it."""
    board, task, ws = tctx.workflow_dir, tctx.issue_id, tctx.workspace
    record = pc.read_base_record(board, task)
    old = record["base_sha"]
    record["final_from"] = record.get("final_from", old)
    record["base_sha"] = main
    check.ensure_state_dir(tctx.state_dir)
    _write_state_json(pc.base_record_path(board, task), record)
    _git_out(ws, "config", "--worktree", "symphony.basesha", main)
    for key in ("symphony.basebranch", "symphony.mergetargetbranch"):
        if _git_run(ws, "config", "--worktree", "--get", key).stdout.strip() != base_branch:
            _git_out(ws, "config", "--worktree", key, base_branch)


def finish_task(board: Path, project: dict, task: str) -> tuple[bool, str]:
    """The final check of a task: (True, text) when the task card went to Verify.

    Takes the lock of the task card and the worktree lock itself: call it only
    after leaving every lock of a subtask merge (flock does not nest). Not ready
    (not a task card, not in Subtasks, a subtask not Done): (False, why), nothing
    changes. A problem once it is ready: needs_human + `## Final Check`
    (_final_stop); the branches are not touched unless the text says so.
    """
    import worktree  # lazy: worktree.py imports this module

    check_task_id(task, project)
    tctx = _task_ctx(board, project, task)
    try:
        cfg: check.Config | None = check.load_config(board)
    except check.CheckError:
        cfg = None
    repo = Path(project["project"]["repo"])
    lock = check.try_lock(tctx, LOCK_WAIT_S)
    if lock is None:
        return False, f"{task}: блокировка карточки занята (идёт проверка или шлюз), повтори позже."
    with lock:
        try:
            common = worktree.common_dir(repo)
            with worktree.worktree_lock(common):
                return _finish_locked(tctx, cfg, project, repo, common)
        except worktree.HookError as exc:
            check.log_event(tctx, task, MODE, "error", f"final check: {exc}")
            return False, f"{task}: {exc}"


def _finish_locked(tctx: check.Context, cfg: check.Config | None, project: dict, repo: Path,
                   common: Path) -> tuple[bool, str]:
    board, task = tctx.workflow_dir, tctx.issue_id
    branch = integration_branch(project, task)
    base_branch = project["project"]["base_branch"]
    try:
        front, body = _read_task_card(board, task)
    except IntegrateError as exc:
        return False, f"{task}: {exc}"
    ids = plan_accepted_ids(body)
    refusal = None
    if not is_task_card(front, body):
        refusal = f"{task}: это не карточка-задача (нужны метка {MANAGER_LABEL} и раздел ## {NOTE_ACCEPTED})."
    elif front["state"].lower() != STATE_SUBTASKS.lower():
        refusal = f"{task}: карточка в статусе {front['state']}, а итоговая проверка начинается из {STATE_SUBTASKS}."
    elif not ids:
        refusal = f"{task}: в разделе ## {NOTE_ACCEPTED} нет подзадач."
    elif waiting := _not_done(board, ids):
        refusal = f"{task}: не все подзадачи в {STATE_DONE}: {', '.join(waiting)}."
    if refusal is not None:
        check.log_event(tctx, task, MODE, "final:refused", refusal)
        return False, refusal

    problems = _workspace_problems(tctx, repo, common, task)
    if not problems:
        for name in (branch, base_branch):
            if not _has_branch(repo, name):
                problems.append(f"в {repo} нет ветки refs/heads/{name}")
    if problems:
        return _final_stop(tctx, cfg, problems)

    ws = tctx.workspace
    moved: list[str] = []
    try:
        try:
            merge = merge_with_base(repo, branch, base_branch, task)
        except Conflict as exc:
            return _final_stop(tctx, cfg, [exc.reason], files=exc.files)
        main, target = merge["main"], merge["target"]
        if merge["merged"]:
            moved.append(f"{branch}: {merge['int'][:12]} → {target[:12]} (коммит слияния с {base_branch})")
        proc = _git_run(ws, "reset", "--keep", target)
        head = _git_run(ws, "rev-parse", "HEAD").stdout.strip()
        if proc.returncode != 0 or head != target:
            raise IntegrateError(f"git reset --keep {target[:12]} в рабочей копии (код {proc.returncode}): "
                                 f"{proc.stderr.strip()[:300]}")
        moved.append(f"symphony/{task}: на {target[:12]}")
        write_final_diff(repo, board, task, branch, base_branch, main, target, merge["merged"])
        _move_base(tctx, base_branch, main)
        check.ensure_state_dir(tctx.state_dir)
        _write_state_json(final_path(board, task), {
            "task": task, "branch": branch, "integration_commit": target, "our_main": main,
            "merged": merge["merged"], "at": check._now(),
        })
    except (IntegrateError, OSError, pc.ProjectError) as exc:
        return _final_stop(tctx, cfg, [str(exc)], moved=moved)

    result = f"merge commit {target}" if merge["merged"] else "no merge needed"
    lines = _final_lines(
        task, branch=f"{branch} (symphony/{task} stands on the same commit)",
        **{"integration tip before": merge["int"], f"{base_branch} tip": main, "result": result},
    )
    lines += ["- итоговая проверка: Verify → Document → Human Review, полный test.sh",
              f"- diff for Verify: docs/{task}/work/{DIFF_MD}, docs/{task}/work/{DIFF_PATCH}"]
    via = _note(tctx, lines, NOTE_FINAL)
    failure = _board_mv(tctx, cfg, STATE_VERIFY)
    if failure is not None:
        return _final_stop(tctx, cfg, [f"не удалось перевести в {STATE_VERIFY}: {failure}"], moved=moved)
    check.log_event(tctx, task, MODE, "final:started", result, branch=branch, int=merge["int"], main=main,
                    commit=target, merged=str(merge["merged"]).lower(), note=via)
    how = (f"коммит слияния {target[:12]} (два родителя: {merge['int'][:12]} и {main[:12]})"
           if merge["merged"] else f"слияния не нужно ({base_branch} не сдвигался)")
    return True, f"{task}: итоговая проверка начата, карточка → {STATE_VERIFY}; {how}."


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

USAGE = ("использование: integrate.py ensure-branch <ЗАДАЧА> [--board <папка доски>] | "
         "integrate.py finish-task <ЗАДАЧА> [--board <папка доски>] | integrate.py hook")
COMMANDS = ("ensure-branch", "finish-task")


def _parse(args: list[str]) -> tuple[str, str, Path] | None:
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
    if len(rest) != 2 or rest[0] not in COMMANDS or rest[1].startswith("-"):
        return None
    raw = board if board is not None else os.environ.get("SYMPHONY_WORKFLOW_DIR", "").strip()
    if not raw:
        return None
    return rest[0], rest[1], Path(raw).resolve()


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


def finish_task_command(task: str, board: Path) -> int:
    project = pc.load_project(board)
    ok, text = finish_task(board, project, task)
    say(text)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["hook"]:
        return hook_main()
    parsed = _parse(args)
    if parsed is None:
        print(f"integrate.py: {USAGE} (без --board берётся $SYMPHONY_WORKFLOW_DIR)", file=sys.stderr)
        return 2
    import worktree  # lazy: worktree.py imports this module

    command, task, board = parsed
    try:
        if command == "finish-task":
            return finish_task_command(task, board)
        return ensure_branch_command(task, board)
    except (IntegrateError, pc.ProjectError, worktree.HookError) as exc:
        print(f"integrate.py: отказ: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
