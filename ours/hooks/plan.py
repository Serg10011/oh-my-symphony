#!/usr/bin/env python3
"""Plan hand-in: the manager's plan file becomes subtask cards (TASK-S4-19;
architect's decisions "статус Plan" and "итоговая проверка задачи и доставка").

A manager card (label `role:manager-plan`) is sliced in the state `Plan`. The
agent writes `<board>/artifacts/<TASK>/work/plan.toml` (in its worktree:
`docs/<TASK>/work/plan.toml`) and hands the plan in by moving the card to
`Subtasks`. The agent creates no cards itself: this program does, after the
plan passed every check, and all or nothing.

    plan.py hook    hooks.after_run, second line (after check.py hook). Acts
                    only on a card in `Subtasks` with the manager label and
                    without `## Plan Accepted`; any other card is passed over
                    silently (the hook runs after every turn of every card).
                    Accepted plan: the integration branch, the subtask cards
                    (state `In Progress`, `request: <TASK>`), `## Plan
                    Accepted` and `## Subtask Progress` in the task card, the
                    card stays in `Subtasks`. Rejected plan: no card at all,
                    `## Plan Failure` with every reason, the card goes back to
                    `Plan`; after [plan] max_failures rejections - `needs_human`.
                    Always exits 0; the outcome is in <board>/.ours-state/
                    check.log (mode "plan").
    plan.py brief   hooks.before_run, last line. For a manager card in `Plan`
                    writes <board>/artifacts/<TASK>/work/limits.md: levels the
                    subtasks may have, turn limits, plan failures so far, the
                    card's own budget from the service API. Information only;
                    the limits are held by the core. Always exits 0.

Plan file format: docs/our/DECISIONS.md, entry "S4-19".

Standard library only, Python >= 3.11, POSIX.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import tomllib
import unicodedata
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import check
import integrate
import project_config as pc

MODE = "plan"
MANAGER_LABEL = integrate.MANAGER_LABEL
STATE_PLAN = "Plan"
STATE_SUBTASKS = "Subtasks"
STATE_ESCALATE = "needs_human"
# Where a subtask card starts: the manager's analysis replaces Todo.
STATE_WORKER = "In Progress"
NOTE_ACCEPTED = "Plan Accepted"
NOTE_FAILURE = "Plan Failure"
NOTE_PROGRESS = integrate.NOTE_PROGRESS
PLAN_DIR = "work"
PLAN_NAME = "plan.toml"
LIMITS_NAME = "limits.md"
PLAN_VERSION = 1
PLAN_MAX_BYTES = 64 * 1024
LOCK_WAIT_S = 5
MAX_LISTED_REASONS = 20
BUDGET_TIMEOUT_S = 5
BUDGET_MAX_BYTES = 1024 * 1024

KEY_RE = re.compile(r"^[a-z0-9-]{1,32}$")
# Ticket identifiers of the core (trackers/validate.py): no dots, 64 characters.
SUBTASK_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
# A Markdown heading in free text would become a card section: `## Touched Files`
# would wake the core's conflict pre-check (C1), which this plan must never do.
HEADING_LINE_RE = re.compile(r"^\s{0,3}#{1,6}(?:\s|$)")
TITLE_MAX = 80
CRITERIA_MAX = 5
NON_GOALS_MAX = 5
PATHS_MAX = 20

_REQUIRED = ("key", "title", "level", "level_reason", "goal", "criteria", "check", "paths")
_OPTIONAL = ("blocked_by", "non_goals")


class PlanError(Exception):
    """Something this program cannot continue with; the message is for the log."""


@dataclass(frozen=True)
class Subtask:
    key: str
    title: str
    level: str
    level_reason: str
    goal: str
    criteria: tuple[str, ...]
    check: str
    paths: tuple[str, ...]
    blocked_by: tuple[str, ...]
    non_goals: tuple[str, ...]


# ---------------------------------------------------------------------------
# the card of the task
# ---------------------------------------------------------------------------


def _card_level(card: Path) -> str | None:
    """`level:` of the card front matter, or None."""
    front, _ = check.split_card(card.read_text(encoding="utf-8"))
    for line in front:
        match = integrate._KEY_RE.match(line)
        if match and match.group(1) == "level":
            return integrate._scalar(match.group(2) or "") or None
    return None


_ACCEPTED_RE = re.compile(rf"^##\s+{re.escape(NOTE_ACCEPTED)}\s*$", re.IGNORECASE | re.MULTILINE)


def _has_accepted(card: Path) -> bool:
    return _ACCEPTED_RE.search(check.split_card(card.read_text(encoding="utf-8"))[1]) is not None


def _waiting_front(ctx: check.Context) -> dict | None:
    """Front matter of a card whose plan is waiting for the hook, else None.

    Waiting = state Subtasks + manager label + no `## Plan Accepted`. A card in
    Subtasks without the label is logged as skipped; every other card is silent.
    """
    front = integrate.read_card_front(ctx.card, with_title=True)
    if front["state"].lower() != STATE_SUBTASKS.lower():
        return None
    if MANAGER_LABEL not in front["labels"]:
        check.log_event(ctx, ctx.issue_id, MODE, "skipped",
                        f"карточка в {STATE_SUBTASKS} без метки {MANAGER_LABEL}: это не сданный план")
        return None
    if _has_accepted(ctx.card):
        return None
    return front


# ---------------------------------------------------------------------------
# counter of rejected plans
# ---------------------------------------------------------------------------


def counter_path(state_dir: Path, task: str) -> Path:
    return state_dir / f"{task}.plan.json"


def read_failures(state_dir: Path, task: str) -> int:
    try:
        return max(0, int(json.loads(counter_path(state_dir, task).read_text(encoding="utf-8"))["failures"]))
    except (OSError, ValueError, KeyError, TypeError):
        return 0  # no file or a damaged one: no rejections counted


def _bump_failures(ctx: check.Context, reason: str) -> int:
    failures = read_failures(ctx.state_dir, ctx.issue_id) + 1
    check.ensure_state_dir(ctx.state_dir)
    payload = {"failures": failures, "last_reason": check._clean(reason)[:2000], "last_at": check._now()}
    check._write_atomic(
        counter_path(ctx.state_dir, ctx.issue_id),
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return failures


def _reset_failures(ctx: check.Context) -> None:
    try:
        counter_path(ctx.state_dir, ctx.issue_id).unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# reading the plan file (from the board side, never through the worktree)
# ---------------------------------------------------------------------------


def plan_file(board: Path, task: str) -> Path:
    return board / pc.ARTIFACTS_DIR / task / PLAN_DIR / PLAN_NAME


def _agent_path(task: str, name: str) -> str:
    return f"docs/{task}/{PLAN_DIR}/{name}"


def _read_plan_text(board: Path, task: str) -> tuple[str | None, list[str]]:
    """(text, []) or (None, reasons). A plain file only, at most 64 KiB, no links."""
    shown = _agent_path(task, PLAN_NAME)
    path = plan_file(board, task)
    # The agent can replace `work` with a link; refuse anything but a plain path.
    expected = Path(os.path.realpath(board)) / pc.ARTIFACTS_DIR / task / PLAN_DIR
    if os.path.realpath(path.parent) != str(expected):
        if not path.parent.exists():
            return None, [f"нет файла плана {shown}: его пишет руководитель перед переводом в {STATE_SUBTASKS}"]
        return None, [f"папка {PLAN_DIR} плана — ссылка или лежит не там: нужна обычная папка docs/{task}/{PLAN_DIR}"]
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, [f"нет файла плана {shown}: его пишет руководитель перед переводом в {STATE_SUBTASKS}"]
    except OSError as exc:
        return None, [f"файл плана {shown} не открыть как обычный файл (ссылка?): {exc.strerror or exc}"]
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, [f"{shown} — не обычный файл"]
        if info.st_size > PLAN_MAX_BYTES:
            return None, [f"файл плана {shown} больше {PLAN_MAX_BYTES // 1024} КБ ({info.st_size} байт)"]
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(PLAN_MAX_BYTES + 1)
    except OSError as exc:
        return None, [f"файл плана {shown} не прочитать: {exc}"]
    finally:
        os.close(fd)
    if len(raw) > PLAN_MAX_BYTES:
        return None, [f"файл плана {shown} больше {PLAN_MAX_BYTES // 1024} КБ"]
    try:
        return raw.decode("utf-8"), []
    except UnicodeDecodeError as exc:
        return None, [f"файл плана {shown} не в кодировке UTF-8: {exc.reason}"]


# ---------------------------------------------------------------------------
# checks of the plan
# ---------------------------------------------------------------------------


def _has_control(text: str, *, newline_ok: bool = False) -> bool:
    return any(
        unicodedata.category(ch) in ("Cc", "Zl", "Zp") and not (newline_ok and ch == "\n")
        for ch in text
    )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _text_problem(name: str, value: object, *, multiline: bool = False) -> str | None:
    """Why `value` is not an acceptable text field `name`, or None."""
    if not isinstance(value, str):
        return f"{name}: нужна строка"
    if not value.strip():
        return f"{name}: пусто"
    if _has_control(value, newline_ok=multiline):
        return f"{name}: управляющие символы не допускаются" + (" (кроме переводов строк)" if multiline else "")
    if multiline:
        for line in value.split("\n"):
            if HEADING_LINE_RE.match(line):
                return f"{name}: строка-заголовок Markdown ({line.strip()[:40]!r}) не допускается: из неё получился бы раздел карточки"
    return None


def _list_problems(name: str, value: object, low: int, high: int) -> list[str]:
    if not isinstance(value, list):
        return [f"{name}: нужен список строк"]
    problems = []
    if not low <= len(value) <= high:
        problems.append(f"{name}: нужно от {low} до {high} строк, а их {len(value)}")
    for index, item in enumerate(value, 1):
        problem = _text_problem(f"{name}[{index}]", item)
        if problem:
            problems.append(problem)
    return problems


def _path_problem(pattern: str) -> str | None:
    if pattern.startswith("/"):
        return "путь должен быть от корня репозитория, без ведущего /"
    if "\\" in pattern or "`" in pattern:
        return "обратная косая черта и обратная кавычка не допускаются"
    segments = pattern.split("/")
    if ".." in segments:
        return "'..' не допускается"
    if "" in segments or "." in segments:
        return "пустые сегменты и '.' не допускаются"
    return None


def _forbidden_problem(pattern: str, cfg: check.Config) -> str | None:
    """A pattern that reaches a forbidden path or the control contour.

    The pattern is checked as a path and, when it has wildcards, so is its
    prefix before the first wildcard.
    """
    probes = {pattern}
    cuts = [pattern.index(ch) for ch in "*?[" if ch in pattern]
    if cuts:
        prefix = pattern[: min(cuts)].rstrip("/")
        if prefix:
            probes.add(prefix)
    problems = check.forbidden_violations(probes, cfg)
    if not problems:
        return None
    where = "контур контроля" if any("control contour" in p for p in problems) else "запрещённые пути проекта"
    return f"путь попадает в {where}: агент там ничего менять не может"


def _check_subtask(index: int, raw: object, keys: list[str | None], levels: list[str],
                   ceiling: int | None, task_level: str | None, cfg: check.Config,
                   errors: list[str]) -> Subtask | None:
    """Validate one [[subtask]]; the problems go to `errors`. The Subtask when it is clean."""
    if not isinstance(raw, dict):
        errors.append(f"подзадача {index}: нужна таблица [[subtask]]")
        return None
    key = raw.get("key")
    label = f"подзадача {index} ({key!r})" if isinstance(key, str) else f"подзадача {index}"
    problems: list[str] = []
    unknown = sorted(set(raw) - set(_REQUIRED) - set(_OPTIONAL))
    if unknown:
        problems.append(f"неизвестный ключ(и): {', '.join(unknown)}; допустимы: {', '.join((*_REQUIRED, *_OPTIONAL))}")
    for name in _REQUIRED:
        if name not in raw:
            problems.append(f"нет ключа {name}")
    if isinstance(key, str) and not KEY_RE.match(key):
        problems.append("key: только a-z, 0-9 и «-», от 1 до 32 знаков")
    elif "key" in raw and not isinstance(key, str):
        problems.append("key: нужна строка")
    elif isinstance(key, str) and keys.count(key) > 1:
        problems.append(f"key {key!r} повторяется: ключи должны быть разными")
    title = raw.get("title")
    if "title" in raw:
        problem = _text_problem("title", title)
        if problem is None and isinstance(title, str):
            if title != title.strip():
                problem = "title: пробелы по краям"
            elif len(title) > TITLE_MAX:
                problem = f"title: {len(title)} знаков, не больше {TITLE_MAX}"
            elif title.startswith("-"):
                problem = "title: не должно начинаться с «-»"
        if problem:
            problems.append(problem)
    level = raw.get("level")
    if "level" in raw:
        problem = _text_problem("level", level)
        if problem:
            problems.append(problem)
        elif level not in levels:
            problems.append(f"level {level!r}: такого уровня нет в [levels]; есть: {', '.join(levels)}")
        elif ceiling is not None and levels.index(level) > ceiling:
            problems.append(
                f"level {level!r} выше уровня задачи {task_level!r}; допустимы: {', '.join(levels[: ceiling + 1])}"
            )
    for name, multiline in (("level_reason", False), ("goal", True), ("check", False)):
        if name in raw:
            value = raw[name]
            if multiline and isinstance(value, str):
                value = value.replace("\r\n", "\n")
            problem = _text_problem(name, value, multiline=multiline)
            if problem is None and name == "check" and "`" in raw[name]:
                problem = "check: обратная кавычка не допускается"
            if problem:
                problems.append(problem)
    if "criteria" in raw:
        problems += _list_problems("criteria", raw["criteria"], 1, CRITERIA_MAX)
    if "non_goals" in raw:
        problems += _list_problems("non_goals", raw["non_goals"], 0, NON_GOALS_MAX)
    paths = raw.get("paths")
    if "paths" in raw:
        listed = _list_problems("paths", paths, 1, PATHS_MAX)
        problems += listed
        if not listed and isinstance(paths, list):
            for pattern in paths:
                problem = _path_problem(pattern) or _forbidden_problem(pattern, cfg)
                if problem:
                    problems.append(f"paths {pattern!r}: {problem}")
    blocked_by = raw.get("blocked_by", [])
    if "blocked_by" in raw:
        listed = _list_problems("blocked_by", blocked_by, 0, PATHS_MAX)
        problems += listed
        if not listed and isinstance(blocked_by, list):
            above = [k for k in keys[: index - 1] if k]
            for ref in blocked_by:
                if ref == key:
                    problems.append(f"blocked_by {ref!r}: подзадача не может ждать саму себя")
                elif ref in above:
                    continue
                elif ref in keys:
                    problems.append(f"blocked_by {ref!r}: эта подзадача ниже в файле; ссылаться можно только на подзадачи выше")
                else:
                    problems.append(f"blocked_by {ref!r}: такого ключа в плане нет")
    errors.extend(f"{label}: {problem}" for problem in problems)
    if problems:
        return None
    return Subtask(
        key=raw["key"], title=raw["title"], level=raw["level"], level_reason=raw["level_reason"],
        goal=raw["goal"].replace("\r\n", "\n"), criteria=tuple(raw["criteria"]), check=raw["check"],
        paths=tuple(raw["paths"]), blocked_by=tuple(dict.fromkeys(blocked_by)),
        non_goals=tuple(raw.get("non_goals", ())),
    )


def level_ceiling(project: dict, front_level: str | None) -> tuple[int | None, str | None]:
    """(index of the task's level in the [levels] order, its name), or (None, None).

    The order of levels is the order of their declaration in project.toml. The
    task's level is `level:` of its card, else `default_level`; none, or one
    that is not in [levels]: no ceiling.
    """
    names = list(project["levels"]["levels"])
    name = front_level or project["levels"]["default_level"]
    if name in names:
        return names.index(name), name
    return None, None


def check_plan(text: str, project: dict, cfg: check.Config, task_level: str | None
               ) -> tuple[list[Subtask], list[str]]:
    """(subtasks, []) for a good plan, ([], reasons) with every reason found."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return [], [f"файл не разбирается как TOML: {exc}"]
    errors: list[str] = []
    unknown = sorted(set(data) - {"version", "subtask"})
    if unknown:
        errors.append(f"неизвестный ключ(и) верхнего уровня: {', '.join(unknown)}; допустимы: version, subtask")
    if "version" not in data or type(data["version"]) is not int or data["version"] != PLAN_VERSION:
        errors.append(f"version: нужно целое {PLAN_VERSION} (первая строка файла: version = {PLAN_VERSION})")
    raw = data.get("subtask")
    if not isinstance(raw, list) or not raw:
        errors.append("нет ни одной подзадачи: нужен хотя бы один блок [[subtask]]")
        raw = []
    limit = project["plan"]["max_subtasks"]
    if len(raw) > limit:
        errors.append(f"в плане {len(raw)} подзадач, а можно не больше {limit} ([plan] max_subtasks)")
    levels = list(project["levels"]["levels"])
    ceiling, level_name = level_ceiling(project, task_level)
    keys = [item.get("key") if isinstance(item, dict) and isinstance(item.get("key"), str) else None for item in raw]
    subtasks = [
        _check_subtask(i, item, keys, levels, ceiling, level_name, cfg, errors)
        for i, item in enumerate(raw, 1)
    ]
    if errors:
        return [], errors
    return [s for s in subtasks if s is not None], []


# ---------------------------------------------------------------------------
# what the board must look like before the plan is even read
# ---------------------------------------------------------------------------


def _existing_subtask_cards(ctx: check.Context, task: str) -> list[str]:
    pattern = re.compile(rf"^{re.escape(task)}-\d+\.md$", re.IGNORECASE)
    try:
        names = [entry.name for entry in os.scandir(ctx.workflow_dir / pc.BOARD_DIR) if pattern.match(entry.name)]
    except OSError:
        return []
    return sorted(names)


def environment_problems(ctx: check.Context, project: dict) -> list[str]:
    """Reasons that the manager cannot fix: the card goes to needs_human at once."""
    task = ctx.issue_id
    problems = []
    if not project["integration"]["enabled"]:
        problems.append("интеграция выключена ([integration] enabled = false в project.toml): "
                        "подзадачам некуда вливаться; включает человек")
    if not project["levels"]["levels"]:
        problems.append("в project.toml пуст раздел [levels]: уровни подзадач взять негде; настраивает человек")
    try:
        integrate.check_task_id(task, project)
    except integrate.IntegrateError as exc:
        problems.append(str(exc))
    if not SUBTASK_ID_RE.match(f"{task}-{pc.MAX_SUBTASKS_LIMIT}"):
        problems.append(f"из ID задачи {task!r} не получаются ID подзадач <ЗАДАЧА>-N "
                        f"(нужно {SUBTASK_ID_RE.pattern}: без точек, до 64 знаков)")
    existing = _existing_subtask_cards(ctx, task)
    if existing:
        shown = ", ".join(name[:-3] for name in existing[:5])
        more = f" и ещё {len(existing) - 5}" if len(existing) > 5 else ""
        problems.append(f"на доске уже есть карточки подзадач этой задачи: {shown}{more}; "
                        "план не создаёт карточки поверх существующих; разбор — человек")
    return problems


# ---------------------------------------------------------------------------
# cards
# ---------------------------------------------------------------------------


def render_body(task: str, index: int, total: int, sub: Subtask) -> str:
    lines = [f"Task: {task} (subtask {index} of {total}). Level: {sub.level} — {sub.level_reason}", "",
             "## Goal", sub.goal.strip(), "", "## Acceptance Criteria"]
    lines += [f"- AC{n}: {text}" for n, text in enumerate(sub.criteria, 1)]
    lines += ["", "## Check", f"`{sub.check}`", "", "## Scope"]
    lines += [f"- `{path}`" for path in sub.paths]
    if sub.non_goals:
        lines += ["", "## Non-goals", *(f"- {text}" for text in sub.non_goals)]
    return "\n".join(lines) + "\n"


def _new_card(ctx: check.Context, binary: str, board_root: Path, task: str, sid: str, sub: Subtask,
              blockers: list[str], label: str, description: Path) -> str | None:
    """`symphony board new`; None on success, else what went wrong."""
    argv = [binary, "board", "new", sid, sub.title, "--root", str(board_root), "--state", STATE_WORKER,
            "--labels", label, "--request", task, "--level", sub.level]
    for blocker in blockers:
        argv += ["--blocked-by", blocker]
    argv += ["--description-file", str(description)]
    try:
        proc = subprocess.run(argv, cwd=str(ctx.workflow_dir), stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=check.SYMPHONY_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return f"код {proc.returncode}: {(proc.stdout + proc.stderr).decode('utf-8', errors='replace').strip()[:300]}"
    return None


def create_cards(ctx: check.Context, cfg: check.Config | None, project: dict, plan: list[Subtask]
                 ) -> tuple[list[tuple[str, Subtask]], str | None, list[str]]:
    """Create the cards in file order, all or nothing.

    Returns (created, failure, leftovers): `failure` is why it stopped (the cards
    of this run are removed then), `leftovers` the files that could not be
    removed. Only the files this run created are ever removed.
    """
    task = ctx.issue_id
    binary = check.resolve_symphony_bin(cfg)
    if binary is None:
        return [], "не найден symphony ([tools] symphony / SYMPHONY_PYTHON): карточки создать нечем", []
    board_root = ctx.workflow_dir / pc.BOARD_DIR
    label = project["integration"]["worker_label"]
    created: list[tuple[str, Subtask]] = []
    made: list[Path] = []  # files that did not exist before and exist now
    ids = {sub.key: f"{task}-{n}" for n, sub in enumerate(plan, 1)}
    failure: str | None = None
    with tempfile.TemporaryDirectory(prefix="ours-plan-") as tmp:
        for n, sub in enumerate(plan, 1):
            sid = ids[sub.key]
            path = board_root / f"{sid}.md"
            existed = os.path.lexists(path)
            description = Path(tmp) / f"{sid}.md"
            description.write_text(render_body(task, n, len(plan), sub), encoding="utf-8")
            problem = _new_card(ctx, binary, board_root, task, sid, sub, [ids[k] for k in sub.blocked_by],
                                label, description)
            if not existed and os.path.lexists(path):
                made.append(path)
            if problem is None and not os.path.isfile(path):
                problem = "команда board new отработала, но файла карточки нет"
            if problem is not None:
                failure = f"не создалась карточка {sid} (подзадача {n} из {len(plan)}): {problem}"
                break
            created.append((sid, sub))
    if failure is None:
        return created, None, []
    leftovers = []
    for path in made:
        try:
            path.unlink()
        except OSError:
            leftovers.append(path.name)
    return [], failure, leftovers


# ---------------------------------------------------------------------------
# outcomes
# ---------------------------------------------------------------------------


def _report(ctx: check.Context, text: str) -> None:
    check.log_event(ctx, ctx.issue_id, MODE, "error", text)
    check._emit(f"plan.py hook: {text}", stream=sys.stderr)


def _note(ctx: check.Context, heading: str, lines: list[str]) -> str:
    try:
        return check.append_note(ctx, "\n".join(lines), heading)
    except Exception as exc:  # noqa: BLE001 - the note is secondary to the outcome
        _report(ctx, f"cannot append the note {heading!r}: {type(exc).__name__}: {exc}")
        return "failed"


def _reason_lines(reasons: list[str]) -> list[str]:
    shown = [f"- {check._clean(reason)}" for reason in reasons[:MAX_LISTED_REASONS]]
    if len(reasons) > MAX_LISTED_REASONS:
        shown.append(f"- и ещё {len(reasons) - MAX_LISTED_REASONS}")
    return shown


def _fail(ctx: check.Context, cfg: check.Config | None, project: dict, reasons: list[str], *,
          environment: bool = False) -> int:
    """No card is created. Back to Plan, or to needs_human: at once for an
    `environment` reason (the manager cannot fix it, no attempt is spent), else
    after [plan] max_failures rejections."""
    limit = project["plan"]["max_failures"]
    if environment:
        failures, target = read_failures(ctx.state_dir, ctx.issue_id), STATE_ESCALATE
    else:
        failures = _bump_failures(ctx, "; ".join(reasons))
        target = STATE_ESCALATE if failures >= limit else STATE_PLAN
    moved = integrate._board_mv(ctx, cfg, target)
    head = [f"- at: {check._now()}", "- check: ours/hooks/plan.py hook"]
    if environment:
        head.append("- attempts: не расходуется (причину руководитель исправить не может)")
    else:
        head.append(f"- failures: {failures} of {limit}")
    head.append(f"- moved to: {target}" if moved is None else f"- moved to: {target} FAILED (see check.log)")
    tail = ["", "Карточки подзадач не созданы." + (
        " Исправь план-файл и снова переведи карточку в Subtasks." if target == STATE_PLAN else
        " Дальше разбирает человек.")]
    note_via = _note(ctx, NOTE_FAILURE, [*head, "", "Причины:", "", *_reason_lines(reasons), *tail])
    extra = {"needs_human": "environment"} if environment else {}
    check.log_event(ctx, ctx.issue_id, MODE, "rejected", "; ".join(reasons), failures=str(failures),
                    moved_to=target, note=note_via, **extra)
    if moved is not None:
        _report(ctx, f"the plan is rejected, but the card did not move to {target}: {moved}")
    return 0


def _accept(ctx: check.Context, cfg: check.Config | None, project: dict, plan: list[Subtask]) -> int:
    import worktree  # lazy: worktree.py imports integrate.py

    task = ctx.issue_id
    repo = Path(project["project"]["repo"])
    branch = integrate.integration_branch(project, task)
    try:
        with worktree.worktree_lock(worktree.common_dir(repo)):
            _, created_branch = integrate.ensure_integration_branch(repo, branch, project["project"]["base_branch"])
    except (integrate.IntegrateError, worktree.HookError) as exc:
        return _fail(ctx, cfg, project, [f"не создалась интеграционная ветка {branch}: {exc}"], environment=True)
    cards, failure, leftovers = create_cards(ctx, cfg, project, plan)
    if failure is not None:
        if leftovers:
            return _fail(ctx, cfg, project, [
                failure,
                "созданные карточки удалить не удалось, осталось: " + ", ".join(leftovers),
            ], environment=True)
        return _fail(ctx, cfg, project, [failure])
    _reset_failures(ctx)
    lines = [f"- at: {check._now()}", f"- branch: {branch} ({'created' if created_branch else 'already existed'})",
             f"- subtasks: {len(cards)}", ""]
    lines += [f"- {sid} — {sub.title} — {sub.level}" for sid, sub in cards]
    note_via = _note(ctx, NOTE_ACCEPTED, lines)
    progress_via = _note(ctx, NOTE_PROGRESS, [f"0 of {len(cards)} merged"])
    check.log_event(ctx, ctx.issue_id, MODE, "accepted", f"{len(cards)} subtask cards created",
                    branch=branch, cards=", ".join(sid for sid, _ in cards), note=note_via, progress=progress_via)
    if "failed" in (note_via, progress_via):
        _report(ctx, "the cards are created but the card of the task has no full notes; "
                     "a repeated hook will send it to needs_human (cards already exist)")
    return 0


def _run_hook(ctx: check.Context, project: dict) -> int:
    if _waiting_front(ctx) is None:
        return 0  # silent: after_run runs after every turn of every card
    lock = check.try_lock(ctx, LOCK_WAIT_S)
    if lock is None:
        check.log_event(ctx, ctx.issue_id, MODE, "skipped", "проверка этой карточки ещё идёт (блокировка занята)")
        return 0
    with lock:
        front = _waiting_front(ctx)  # again under the lock
        if front is None:
            return 0
        try:
            cfg: check.Config | None = check.load_config(ctx.workflow_dir)
        except check.CheckError:
            cfg = None
        problems = environment_problems(ctx, project)
        if problems:
            return _fail(ctx, cfg, project, problems, environment=True)
        if cfg is None:
            return _fail(ctx, cfg, project, ["не читаются правила проекта (project.toml)"], environment=True)
        text, reasons = _read_plan_text(ctx.workflow_dir, ctx.issue_id)
        plan: list[Subtask] = []
        if text is not None:
            plan, reasons = check_plan(text, project, cfg, _card_level(ctx.card))
        if reasons:
            return _fail(ctx, cfg, project, reasons)
        return _accept(ctx, cfg, project, plan)


def hook_main() -> int:
    """hooks.after_run, second line. Always 0: it must never break the turn."""
    try:
        ctx = check.build_context(MODE)
    except check.CheckError as exc:
        check._emit(f"plan.py hook: {exc}", stream=sys.stderr)
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
# brief: limits for the manager (before_run)
# ---------------------------------------------------------------------------


def fetch_budgets(port: int, task: str) -> tuple[list[dict] | None, str]:
    """(items, "") from the service API, or (None, why not). 5 s, no proxy."""
    url = f"http://127.0.0.1:{port}/api/v1/issues/{task}"
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=BUDGET_TIMEOUT_S) as response:
            data = json.loads(response.read(BUDGET_MAX_BYTES))
        budgets = data["budgets"]
        items = budgets["items"]
        if not budgets.get("available") or not isinstance(items, list):
            return None, "служба не отдала бюджет"
        return items, ""
    except Exception as exc:  # noqa: BLE001 - any failure means "no budget line"
        return None, f"{type(exc).__name__}: {exc}"


def _budget_lines(port: int, task: str) -> list[str]:
    items, why = fetch_budgets(port, task)
    if items is None:
        return [f"бюджет недоступен ({check._clean(why)[:200]})"]
    lines = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            lines.append(f"- {item['name']}: limit {item.get('limit')}, used {item.get('used')}, "
                         f"remaining {item.get('remaining')}")
    return lines or ["бюджет недоступен (пустой ответ службы)"]


def render_limits(ctx: check.Context, project: dict) -> str:
    levels = project["levels"]["levels"]
    names = list(levels)
    ceiling, level_name = level_ceiling(project, _card_level(ctx.card))
    allowed = names if ceiling is None else names[: ceiling + 1]
    base = project["limits"]
    plan = project["plan"]
    failures = read_failures(ctx.state_dir, ctx.issue_id)
    level_line = level_name or "не задан (в карточке нет `level:` и нет default_level): ограничения нет"
    lines = [
        f"# Limits for planning {ctx.issue_id}", "",
        f"Written by ours/hooks/plan.py brief at {check._now()}. Information only: the limits are held by code.",
        "", "## Task level", "", f"- {level_line}",
        "- порядок уровней — порядок объявления в [levels] project.toml (от простого к сложному); "
        "подзадача не может быть выше уровня задачи", "",
        "## Levels allowed for subtasks", "",
        "| level | model | effort | max_turns | max_state_turns |", "|---|---|---|---|---|",
    ]
    for name in allowed:
        body = levels[name]
        claude = body.get("claude", {})
        lines.append(f"| {name} | {claude.get('model', '—')} | {claude.get('effort', '—')} | "
                     f"{body.get('max_turns', base['max_turns'])} | {body.get('max_state_turns', base['max_state_turns'])} |")
    if not allowed:
        lines.append("| — | | | | |")
    lines += ["", "Если у уровня нет своего max_turns / max_state_turns, действуют значения проекта ([limits]).",
              "Модель и усилие — из перевода уровня для claude; «—» значит «как задано в службе».", "",
              "## Plan", "",
              f"- max_subtasks: {plan['max_subtasks']}",
              f"- plan failures so far: {failures} of {plan['max_failures']} (после этого — needs_human)", "",
              f"## Budget of {ctx.issue_id}", ""]
    lines += _budget_lines(project["project"]["port"], ctx.issue_id)
    lines += ["", "## Plan file", "",
              f"- path: {_agent_path(ctx.issue_id, PLAN_NAME)}",
              "- format: docs/our/DECISIONS.md, entry \"S4-19\" (формат plan.toml)", ""]
    return "\n".join(lines)


def _write_limits(ctx: check.Context, text: str) -> None:
    directory = ctx.workflow_dir / pc.ARTIFACTS_DIR / ctx.issue_id / PLAN_DIR
    expected = Path(os.path.realpath(ctx.workflow_dir)) / pc.ARTIFACTS_DIR / ctx.issue_id / PLAN_DIR
    directory.mkdir(parents=True, exist_ok=True)
    if os.path.realpath(directory) != str(expected):
        raise PlanError(f"{directory} — ссылка или лежит не там: сводку не пишу")
    check._write_atomic(directory / LIMITS_NAME, text.encode("utf-8"))


def _run_brief(ctx: check.Context, project: dict) -> int:
    front = integrate.read_card_front(ctx.card)
    if front["state"].lower() != STATE_PLAN.lower() or MANAGER_LABEL not in front["labels"]:
        return 0
    _write_limits(ctx, render_limits(ctx, project))
    return 0


def brief_main() -> int:
    """hooks.before_run, last line. Always 0: a failed brief must not stop the run."""
    try:
        ctx = check.build_context(MODE)
    except check.CheckError as exc:
        check._emit(f"plan.py brief: {exc}", stream=sys.stderr)
        return 0
    try:
        return _run_brief(ctx, pc.load_project(ctx.workflow_dir))
    except BaseException as exc:  # noqa: BLE001 - the brief never fails the run
        check.log_event(ctx, ctx.issue_id, MODE, "error", f"brief: {type(exc).__name__}: {exc}")
        check._emit(f"plan.py brief: {type(exc).__name__}: {exc}", stream=sys.stderr)
        return 0


USAGE = "использование: plan.py hook | plan.py brief"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["hook"]:
        return hook_main()
    if args == ["brief"]:
        return brief_main()
    print(f"plan.py: {USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
