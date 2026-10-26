#!/usr/bin/env python3
"""Integration branch of a task (TASK-S4-11; architect's decision on REPORT-17,
part I, item 1).

Subtasks of one task (cards labelled `role:worker` with `request: <TASK>`)
must see each other's code. They start from the end of the task's shared
branch `<branch_prefix><TASK>` (default `integration/<TASK>`) instead of the
project's base branch. Merging finished subtasks into it is S4-12, not here.

    integrate.py ensure-branch <TASK> [--board <board folder>]

Creates the branch from refs/heads/<base_branch> if it does not exist yet; an
existing branch is never recreated or moved. The board folder defaults to
$SYMPHONY_WORKFLOW_DIR. Exit codes: 0 done, 1 refusal with a reason,
2 bad arguments.

The branch is NOT called symphony/<TASK>: the task's own manager card is run
by an agent in a worktree on that very name, and worktree.py renames it on a
clean start, which would destroy the integration branch.

As a library (worktree.py, S4-12): read_card_front, task_of, integration_branch,
ensure_integration_branch. ensure_integration_branch takes no lock; the caller
holds the shared worktree lock (the CLI does).

Standard library only, Python >= 3.11, POSIX.
"""

from __future__ import annotations

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


def read_card_front(path: Path) -> dict:
    """{state, request, labels} of a card file, without PyYAML.

    `request` is a string or None; `labels` a tuple, lower-cased (the core
    lower-cases labels too). Both list forms the file tracker writes are read:
    `labels: [a, b]` and a block of `- a` lines. Other keys are ignored.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise IntegrateError(f"карточка {path} не читается: {exc}") from None
    front, _ = check.split_card(text)
    state: str | None = None
    request: str | None = None
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
    return {"state": state, "request": request, "labels": labels}


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
# CLI
# ---------------------------------------------------------------------------

USAGE = "использование: integrate.py ensure-branch <ЗАДАЧА> [--board <папка доски>]"


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
