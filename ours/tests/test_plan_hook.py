"""Tests for the plan hand-in (TASK-S4-19): ours/hooks/plan.py (`hook`, `brief`)
and the [plan] section of project.toml.

Real temporary git repositories and the real `symphony board` of this working
copy (as test_integrate.py); the service is not started. A broken `symphony`
is a small script that hands over to the real one.

Run: python -m pytest ours/tests -q
"""

from __future__ import annotations

import http.server
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the hooks are POSIX-only")

OURS = Path(__file__).resolve().parents[1]
PLAN = OURS / "hooks" / "plan.py"
sys.path.insert(0, str(OURS / "hooks"))

import check  # noqa: E402
import plan  # noqa: E402
import project_config as pc  # noqa: E402

REAL_SYMPHONY = Path(sys.executable).parent / "symphony"
pytestmark = [
    pytestmark,
    pytest.mark.skipif(not REAL_SYMPHONY.exists(), reason="no `symphony` next to the test python"),
]

TASK = "OMS-9"
BRANCH = "integration/OMS-9"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.invalid")
    git(path, "config", "user.name", "t")
    write(path / "README.md", "readme\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "base")
    return path


# ---------------------------------------------------------------------------
# plan.toml text
# ---------------------------------------------------------------------------


def toml_value(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    return str(value)


def sub(key: str = "a", **fields: object) -> dict:
    """A good subtask; a field set to None is left out."""
    data: dict = {
        "key": key,
        "title": "Добавить словарь терминов",
        "level": "simple",
        "level_reason": "одна страница текста, кода нет",
        "goal": "Написать словарь.\nВторая строка цели.",
        "criteria": ["Словарь лежит в docs/our/GLOSSARY.md"],
        "check": "test -s docs/a.md",
        "paths": ["docs/our/GLOSSARY.md"],
        "blocked_by": [],
        "non_goals": ["Не править код"],
    }
    data.update(fields)
    return {name: value for name, value in data.items() if value is not None}


def plan_text(*subtasks: dict, version: object = 1, extra: str = "") -> str:
    lines = [] if version is None else [f"version = {toml_value(version)}"]
    if extra:
        lines.append(extra)
    for item in subtasks:
        lines.append("")
        lines.append("[[subtask]]")
        lines += [f"{name} = {toml_value(value)}" for name, value in item.items()]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# a board folder
# ---------------------------------------------------------------------------

LEVELS = """\
[levels]
default_level = "simple"
[levels.simple]
[levels.simple.claude]
model = "haiku"
effort = "medium"
[levels.standard]
max_turns = 16
[levels.standard.claude]
model = "haiku"
effort = "high"
[levels.complex]
[levels.complex.claude]
model = "sonnet"
effort = "high"
"""

BREAK_ON_SECOND = """\
#!{python}
import os, sys
args = sys.argv[1:]
if args[:2] == ["board", "new"] and args[2].endswith("-2"):
    print("boom on the second card", file=sys.stderr)
    sys.exit(1)
os.execv({real!r}, [{real!r}, *args])
"""

LOCK_ON_FIRST = """\
#!{python}
import os, subprocess, sys
args = sys.argv[1:]
code = subprocess.run([{real!r}, *args]).returncode
if args[:2] == ["board", "new"] and args[2].endswith("-1"):
    os.chmod(args[args.index("--root") + 1], 0o555)  # the card cannot be removed now
if args[:2] == ["board", "new"] and args[2].endswith("-2"):
    sys.exit(1)
sys.exit(code)
"""


class Env:
    def __init__(self, tmp: Path, *, integration: bool = True, levels: str = LEVELS, plan_section: str = "",
                 checks: str = "", port: int = 9971) -> None:
        self.tmp = tmp
        self.code = make_repo(tmp / "code")
        self.board = tmp / "projects" / "demo"
        self.kanban = self.board / "kanban"
        self.kanban.mkdir(parents=True)
        self.ws = tmp / "workspaces" / TASK
        self.ws.mkdir(parents=True)
        write(self.board / "test.sh", "exit 0\n")
        write(self.board / 'WORKFLOW.md', '''---
tracker:
  kind: file
  board_root: ./kanban
  active_states: [Todo, Plan, In Progress, Verify, Document]
  terminal_states: [Subtasks, needs_human, Done, Blocked, Archive]
---
''')
        self.symphony = str(REAL_SYMPHONY)
        self.write_project(integration=integration, levels=levels, plan_section=plan_section, checks=checks,
                           port=port)

    def write_project(self, *, integration: bool = True, levels: str = LEVELS, plan_section: str = "",
                      checks: str = "", port: int = 9971) -> None:
        text = [
            "[project]", 'name = "demo"', f"repo = {json.dumps(str(self.code))}",
            'base_branch = "main"', 'id_prefix = "OMS"', f"port = {port}",
            "[tools]", f"python = {json.dumps(sys.executable)}", f"symphony = {json.dumps(self.symphony)}",
            'claude = "/bin/claude"',
        ]
        if integration:
            text += ["[integration]", "enabled = true"]
        if checks:
            text += ["[checks]", checks]
        if plan_section:
            text += ["[plan]", plan_section]
        text.append(levels)
        write(self.board / "project.toml", "\n".join(text) + "\n")

    def use_symphony(self, script: str) -> None:
        binary = write(self.tmp / "bin" / "symphony", script.format(python=sys.executable, real=str(REAL_SYMPHONY)))
        binary.chmod(0o755)
        self.symphony = str(binary)
        toml = self.board / "project.toml"
        toml.write_text(toml.read_text().replace(json.dumps(str(REAL_SYMPHONY)), json.dumps(self.symphony)),
                        encoding="utf-8")

    def task_card(self, *, state: str = "Subtasks", labels: str = "[role:manager-plan]", level: str | None = "standard",
                  body: str = "task body\n") -> None:
        lines = ["---", f"id: {TASK}", f"identifier: {TASK}", "title: Big task", f"state: {state}"]
        if labels:
            lines.append(f"labels: {labels}")
        if level:
            lines.append(f"level: {level}")
        write(self.kanban / f"{TASK}.md", "\n".join([*lines, "---", "", body]))

    def plan(self, text: str) -> Path:
        return write(self.board / "artifacts" / TASK / "work" / "plan.toml", text)

    def card_path(self, ident: str) -> Path:
        return self.kanban / f"{ident}.md"

    def state(self, ident: str = TASK) -> str:
        return check.read_card_state(self.card_path(ident))

    def body(self, ident: str = TASK) -> str:
        return check.split_card(self.card_path(ident).read_text(encoding="utf-8"))[1]

    def raw(self, ident: str) -> str:
        return self.card_path(ident).read_text(encoding="utf-8")

    def cards(self) -> list[str]:
        return sorted(p.stem for p in self.kanban.glob(f"{TASK}-*.md"))

    def branches(self) -> list[str]:
        return git(self.code, "branch", "--format=%(refname:short)").split()

    def events(self, mode: str = "plan") -> list[dict]:
        path = self.board / ".ours-state" / "check.log"
        if not path.exists():
            return []
        records = [json.loads(line) for line in path.read_text().splitlines()]
        return [r for r in records if r["mode"] == mode]

    def counter(self) -> int | None:
        path = plan.counter_path(self.board / ".ours-state", TASK)
        return json.loads(path.read_text())["failures"] if path.exists() else None

    def hook(self, monkeypatch: pytest.MonkeyPatch) -> int:
        monkeypatch.chdir(self.ws)
        return plan.hook_main()

    def brief(self, monkeypatch: pytest.MonkeyPatch) -> int:
        monkeypatch.chdir(self.ws)
        return plan.brief_main()

    def limits(self) -> Path:
        return self.board / "artifacts" / TASK / "work" / "limits.md"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    for key in [k for k in os.environ if k.startswith("SYMPHONY_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("SYMPHONY_WORKFLOW_DIR", str(tmp_path / "projects" / "demo"))
    monkeypatch.setenv("SYMPHONY_ISSUE_ID", TASK)
    monkeypatch.setenv("SYMPHONY_PYTHON", sys.executable)
    return Env(tmp_path)


def reasons_of(note: str) -> str:
    """The bullet list of reasons of a `## Plan Failure` text."""
    return note.split("Причины:", 1)[1].split("Карточки подзадач не созданы", 1)[0].strip()


def rejected(env: Env, monkeypatch: pytest.MonkeyPatch, text: str, *, level: str | None = "standard") -> str:
    """Hand in `text`; the plan must be rejected. Returns the `## Plan Failure` text."""
    env.task_card(level=level)
    env.plan(text)
    assert env.hook(monkeypatch) == 0
    assert env.cards() == [], "a rejected plan must not leave a card"
    assert [e["outcome"] for e in env.events()] == ["rejected"]
    note = env.body()
    assert "## Plan Failure" in note
    return note.split("## Plan Failure", 1)[1]


# ---------------------------------------------------------------------------
# the checks of the plan (library level)
# ---------------------------------------------------------------------------


def checked(env: Env, text: str, *, level: str | None = "standard") -> tuple[list, list[str]]:
    project = pc.load_project(env.board)
    return plan.check_plan(text, project, check.load_config(env.board), level)


def test_a_good_plan_is_read(env: Env) -> None:
    subtasks, reasons = checked(env, plan_text(sub("a"), sub("b", blocked_by=["a"], level="standard")))
    assert reasons == []
    assert [s.key for s in subtasks] == ["a", "b"]
    assert subtasks[1].blocked_by == ("a",) and subtasks[1].level == "standard"
    assert subtasks[0].goal == "Написать словарь.\nВторая строка цели."


def test_optional_keys_may_be_left_out(env: Env) -> None:
    subtasks, reasons = checked(env, plan_text(sub("a", blocked_by=None, non_goals=None)))
    assert reasons == [] and subtasks[0].blocked_by == () and subtasks[0].non_goals == ()


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("version = 1\n[[subtask\n", "TOML"),
        (plan_text(sub(), version=None), "version"),
        (plan_text(sub(), version=2), "version"),
        (plan_text(sub(), version=True), "version"),
        (plan_text(sub(), version="1"), "version"),
        ("version = 1\n", "нет ни одной подзадачи"),
        (plan_text(sub(), extra="oops = 1"), "неизвестный ключ(и) верхнего уровня: oops"),
        ("version = 1\nsubtask = 5\n", "нет ни одной подзадачи"),
    ],
)
def test_file_level_problems(env: Env, text: str, fragment: str) -> None:
    subtasks, reasons = checked(env, text)
    assert subtasks == [] and any(fragment in reason for reason in reasons), reasons


def test_one_subtask_more_than_max_subtasks_is_a_problem(env: Env) -> None:
    ok = [sub(f"k{n}") for n in range(5)]
    assert checked(env, plan_text(*ok))[1] == []
    _, reasons = checked(env, plan_text(*ok, sub("k5")))
    assert reasons == ["в плане 6 подзадач, а можно не больше 5 ([plan] max_subtasks)"]


def test_max_subtasks_comes_from_project_toml(tmp_path: Path) -> None:
    env = Env(tmp_path, plan_section="max_subtasks = 2")
    _, reasons = checked(env, plan_text(sub("a"), sub("b"), sub("c")))
    assert reasons == ["в плане 3 подзадач, а можно не больше 2 ([plan] max_subtasks)"]


@pytest.mark.parametrize(
    "fields, fragment",
    [
        ({"extra": "x"}, "неизвестный ключ(и): extra"),
        ({"goal": None}, "нет ключа goal"),
        ({"criteria": None}, "нет ключа criteria"),
        ({"title": 5}, "title: нужна строка"),
        ({"criteria": "one"}, "criteria: нужен список строк"),
        ({"paths": "docs/a.md"}, "paths: нужен список строк"),
        ({"key": "Bad Key"}, "key: только a-z"),
        ({"key": "k" * 33}, "key: только a-z"),
        ({"key": 7}, "key: нужна строка"),
        ({"title": "x" * 81}, "title: 81 знаков, не больше 80"),
        ({"title": "x" * 80}, None),
        ({"title": " padded"}, "title: пробелы по краям"),
        ({"title": "-flag"}, "title: не должно начинаться"),
        ({"title": "two\nlines"}, "title: управляющие символы"),
        ({"level_reason": "two\nlines"}, "level_reason: управляющие символы"),
        ({"level_reason": ""}, "level_reason: пусто"),
        ({"goal": "a\x00b"}, "goal: управляющие символы"),
        ({"goal": "text\n## Touched Files\n- `a`"}, "goal: строка-заголовок Markdown"),
        ({"criteria": []}, "criteria: нужно от 1 до 5 строк, а их 0"),
        ({"criteria": ["x"] * 6}, "criteria: нужно от 1 до 5 строк, а их 6"),
        ({"criteria": ["x"] * 5}, None),
        ({"criteria": ["a\nb"]}, "criteria[1]: управляющие символы"),
        ({"non_goals": ["x"] * 6}, "non_goals: нужно от 0 до 5 строк, а их 6"),
        ({"paths": []}, "paths: нужно от 1 до 20 строк, а их 0"),
        ({"paths": [f"docs/f{n}.md" for n in range(21)]}, "paths: нужно от 1 до 20 строк, а их 21"),
        ({"check": "a\nb"}, "check: управляющие символы"),
        ({"check": "echo `x`"}, "check: обратная кавычка"),
    ],
)
def test_field_checks(env: Env, fields: dict, fragment: str | None) -> None:
    _, reasons = checked(env, plan_text(sub(**fields)))
    if fragment is None:
        assert reasons == []
    else:
        assert any(fragment in reason for reason in reasons), reasons
        assert all(reason.startswith("подзадача 1") for reason in reasons)


def test_keys_must_be_unique(env: Env) -> None:
    _, reasons = checked(env, plan_text(sub("a"), sub("a")))
    assert len(reasons) == 2 and all("key 'a' повторяется" in reason for reason in reasons)


@pytest.mark.parametrize(
    "blocked, fragment",
    [
        (["b"], "ниже в файле"),
        (["a"], "не может ждать саму себя"),
        (["zzz"], "такого ключа в плане нет"),
    ],
)
def test_blocked_by_points_only_upward(env: Env, blocked: list[str], fragment: str) -> None:
    first = sub("a", blocked_by=blocked) if blocked == ["b"] or blocked == ["a"] else sub("a")
    second = sub("b", blocked_by=blocked) if blocked == ["zzz"] else sub("b")
    _, reasons = checked(env, plan_text(first, second))
    assert any(fragment in reason for reason in reasons), reasons


def test_blocked_by_upward_is_fine_and_a_chain_has_no_cycle(env: Env) -> None:
    _, reasons = checked(env, plan_text(sub("a"), sub("b", blocked_by=["a"]), sub("c", blocked_by=["a", "b"])))
    assert reasons == []


def test_a_level_must_exist_in_levels(env: Env) -> None:
    _, reasons = checked(env, plan_text(sub(level="epic")))
    assert any("level 'epic': такого уровня нет в [levels]" in reason for reason in reasons)


def test_a_level_above_the_task_is_refused(env: Env) -> None:
    _, reasons = checked(env, plan_text(sub(level="complex")), level="standard")
    assert any("выше уровня задачи 'standard'" in reason for reason in reasons), reasons
    assert checked(env, plan_text(sub(level="standard")), level="standard")[1] == []
    assert checked(env, plan_text(sub(level="complex")), level="complex")[1] == []


def test_the_order_of_levels_is_the_order_of_declaration(tmp_path: Path) -> None:
    reversed_levels = LEVELS.replace("[levels.simple]", "[levels.zzz]").replace("[levels.simple.claude]", "[levels.zzz.claude]")
    env = Env(tmp_path, levels=reversed_levels.replace('default_level = "simple"', 'default_level = "zzz"'))
    # zzz is declared first, so it is the lowest level whatever its name
    assert checked(env, plan_text(sub(level="zzz")), level="zzz")[1] == []
    assert checked(env, plan_text(sub(level="standard")), level="zzz")[1] != []


def test_without_a_card_level_the_default_level_is_the_ceiling(env: Env) -> None:
    _, reasons = checked(env, plan_text(sub(level="standard")), level=None)
    assert any("выше уровня задачи 'simple'" in reason for reason in reasons), reasons


def test_without_any_task_level_there_is_no_ceiling(tmp_path: Path) -> None:
    env = Env(tmp_path, levels=LEVELS.replace('default_level = "simple"\n', ""))
    assert checked(env, plan_text(sub(level="complex")), level=None)[1] == []


@pytest.mark.parametrize(
    "path, fragment",
    [
        ("/etc/passwd", "без ведущего /"),
        ("../x", "'..' не допускается"),
        ("docs/../ours/x", "'..' не допускается"),
        ("./docs/a.md", "пустые сегменты"),
        ("docs//a.md", "пустые сегменты"),
        ("docs\\a.md", "обратная косая"),
        ("ours/hooks/x.py", "контур контроля"),
        ("ours/hooks/**", "контур контроля"),
        ("ours/bin/*.py", "контур контроля"),
        ("ours/templates/WORKFLOW.md.tmpl", "контур контроля"),
        (".github/workflows/ci.yml", "запрещённые пути проекта"),
        ("WORKFLOW.md", "запрещённые пути проекта"),
        ("ours/prompts/stages/plan.md", "запрещённые пути проекта"),
        ("secrets/*.pem", "запрещённые пути проекта"),
        (".env", "запрещённые пути проекта"),
    ],
)
def test_paths_stay_out_of_forbidden_paths_and_the_contour(env: Env, path: str, fragment: str) -> None:
    _, reasons = checked(env, plan_text(sub(paths=["docs/ok.md", path])))
    assert len(reasons) == 1 and fragment in reasons[0] and repr(path) in reasons[0], reasons


def test_allowed_paths_open_ours_but_never_the_contour(tmp_path: Path) -> None:
    env = Env(tmp_path, checks='allow_paths = ["ours/prompts/**", "ours/hooks/**"]')
    assert checked(env, plan_text(sub(paths=["ours/prompts/stages/plan.md", "src/**", "docs/**"])))[1] == []
    _, reasons = checked(env, plan_text(sub(paths=["ours/hooks/plan.py"])))
    assert "контур контроля" in reasons[0]


def test_every_reason_is_collected_not_only_the_first(env: Env) -> None:
    text = plan_text(
        sub("a", title="x" * 90, level="complex", paths=["ours/hooks/x.py"]),
        sub("b", blocked_by=["c"], criteria=[]),
        sub("c", extra="1"),
        version=2,
    )
    _, reasons = checked(env, text)
    joined = "\n".join(reasons)
    for fragment in ("version", "title: 90 знаков", "выше уровня задачи", "контур контроля", "ниже в файле",
                     "criteria: нужно от 1 до 5", "неизвестный ключ(и): extra"):
        assert fragment in joined, (fragment, reasons)
    assert len(reasons) >= 7


# ---------------------------------------------------------------------------
# reading the file
# ---------------------------------------------------------------------------


def test_a_missing_plan_file_is_a_rejection(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.task_card()
    assert env.hook(monkeypatch) == 0
    assert env.state() == "Plan" and env.cards() == []
    assert "нет файла плана docs/OMS-9/work/plan.toml" in env.body()


def test_a_symlinked_plan_file_is_refused(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    real = write(env.tmp / "elsewhere.toml", plan_text(sub()))
    link = env.board / "artifacts" / TASK / "work" / "plan.toml"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    env.task_card()
    assert env.hook(monkeypatch) == 0
    assert env.state() == "Plan" and env.cards() == [] and "обычный файл" in env.body()


def test_a_symlinked_work_folder_is_refused(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    write(env.tmp / "elsewhere" / "plan.toml", plan_text(sub()))
    work = env.board / "artifacts" / TASK / "work"
    work.parent.mkdir(parents=True)
    work.symlink_to(env.tmp / "elsewhere")
    env.task_card()
    assert env.hook(monkeypatch) == 0
    assert env.state() == "Plan" and env.cards() == [] and "ссылка" in env.body()


def test_a_big_or_broken_file_is_refused(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.task_card()
    env.plan("version = 1\n# " + "x" * 70000 + "\n")
    assert env.hook(monkeypatch) == 0
    assert "больше 64 КБ" in env.body()
    env.task_card()
    path = env.plan("")
    path.write_bytes(b"version = 1\n\xff\xfe\n")
    assert env.hook(monkeypatch) == 0
    assert "не в кодировке UTF-8" in env.body()


def test_a_directory_instead_of_the_file_is_refused(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    (env.board / "artifacts" / TASK / "work" / "plan.toml").mkdir(parents=True)
    env.task_card()
    assert env.hook(monkeypatch) == 0
    assert env.state() == "Plan" and env.cards() == []


# ---------------------------------------------------------------------------
# acceptance
# ---------------------------------------------------------------------------


def front_value(raw: str, key: str) -> str | None:
    front, _ = check.split_card(raw)
    for line in front:
        match = re.match(rf"^{key}\s*:\s*(.*)$", line)
        if match:
            return match.group(1).strip()
    return None


def front_list(raw: str, key: str) -> list[str]:
    front, _ = check.split_card(raw)
    items: list[str] = []
    for index, line in enumerate(front):
        match = re.match(rf"^{key}\s*:\s*(.*)$", line)
        if not match:
            continue
        inline = match.group(1).strip()
        if inline.startswith("["):
            return [part.strip().strip("'\"") for part in inline[1:-1].split(",") if part.strip()]
        for item in front[index + 1:]:
            found = re.match(r"^\s*-\s+(.*)$", item)
            if not found:
                break
            items.append(found.group(1).strip().strip("'\""))
    return items


THREE = (
    sub("glossary"),
    sub("index", title="Собрать оглавление", level="standard", paths=["docs/our/INDEX.md", "docs/our/**"],
        blocked_by=["glossary"], non_goals=None),
    sub("links", title="Проверить ссылки", blocked_by=["glossary", "index"]),
)


def accept(env: Env, monkeypatch: pytest.MonkeyPatch, *subtasks: dict, level: str | None = "standard") -> None:
    env.task_card(level=level)
    env.plan(plan_text(*(subtasks or THREE)))
    assert env.hook(monkeypatch) == 0


def test_an_accepted_plan_creates_the_cards_in_order(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    accept(env, monkeypatch)
    assert env.cards() == ["OMS-9-1", "OMS-9-2", "OMS-9-3"]
    for ident in env.cards():
        raw = env.raw(ident)
        assert front_value(raw, "state") == "In Progress"
        assert [label.lower() for label in front_list(raw, "labels")] == ["role:worker"]
        assert front_value(raw, "request") == TASK
    assert [front_value(env.raw(i), "level") for i in env.cards()] == ["simple", "standard", "simple"]
    assert front_list(env.raw("OMS-9-1"), "blocked_by") == []
    assert front_list(env.raw("OMS-9-2"), "blocked_by") == ["OMS-9-1"]  # IDs, not keys
    assert front_list(env.raw("OMS-9-3"), "blocked_by") == ["OMS-9-1", "OMS-9-2"]
    assert front_value(env.raw("OMS-9-2"), "title") in ("Собрать оглавление", '"Собрать оглавление"')
    assert env.state() == "Subtasks"
    assert [e["outcome"] for e in env.events()] == ["accepted"]
    assert env.events()[0]["cards"] == "OMS-9-1, OMS-9-2, OMS-9-3"


def test_the_body_of_a_subtask_card(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    accept(env, monkeypatch)
    body = env.body("OMS-9-2").lstrip("\n")
    assert body.startswith("Task: OMS-9 (subtask 2 of 3). Level: standard — одна страница текста, кода нет\n")
    for section in ("## Goal", "## Acceptance Criteria", "## Check", "## Scope"):
        assert section in body, section
    assert "- AC1: Словарь лежит в docs/our/GLOSSARY.md" in body
    assert "`test -s docs/a.md`" in body
    assert "- `docs/our/INDEX.md`" in body and "- `docs/our/**`" in body
    # no non_goals in subtask 2: no empty section; the others have it
    assert "## Non-goals" not in body and "## Non-goals" in env.body("OMS-9-1")
    # the sections the agent writes itself, and the one the core's conflict check reads
    for card in env.cards():
        text = env.raw(card)
        assert "## Touched Files" not in text and "## Plan\n" not in text and "## Acceptance Tests" not in text


def test_the_core_does_not_see_touched_files_in_a_subtask(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from symphony.orchestrator.parsing import _parse_touched_files

    accept(env, monkeypatch)
    for card in env.cards():
        assert _parse_touched_files(env.body(card)) == set()


def test_the_integration_branch_and_notes(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    assert BRANCH not in env.branches()
    accept(env, monkeypatch)
    assert BRANCH in env.branches()
    assert git(env.code, "rev-parse", BRANCH) == git(env.code, "rev-parse", "main")
    body = env.body()
    assert "## Plan Accepted" in body and "## Subtask Progress" in body
    assert f"- branch: {BRANCH} (created)" in body
    assert "- OMS-9-1 — Добавить словарь терминов — simple" in body
    assert "- OMS-9-2 — Собрать оглавление — standard" in body
    assert body.rstrip().endswith("0 of 3 merged")


def test_an_existing_integration_branch_is_not_touched(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    git(env.code, "branch", BRANCH, "main")
    write(env.code / "later.txt", "later\n")
    git(env.code, "add", "-A")
    git(env.code, "commit", "-q", "-m", "later")
    accept(env, monkeypatch)
    assert git(env.code, "rev-parse", BRANCH) == git(env.code, "rev-parse", "main~1")
    assert "already existed" in env.body()
    assert env.cards() == ["OMS-9-1", "OMS-9-2", "OMS-9-3"]


def test_a_second_call_does_nothing(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    accept(env, monkeypatch)
    before = (env.raw(TASK), env.cards(), len(env.events()))
    assert env.hook(monkeypatch) == 0
    assert (env.raw(TASK), env.cards(), len(env.events())) == before


def test_the_failure_counter_is_reset_by_an_accepted_plan(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.task_card()
    env.plan(plan_text(sub(), version=2))
    assert env.hook(monkeypatch) == 0
    assert env.counter() == 1 and env.state() == "Plan"
    accept(env, monkeypatch, sub())
    assert env.cards() == ["OMS-9-1"] and env.counter() is None


# ---------------------------------------------------------------------------
# rejection
# ---------------------------------------------------------------------------


def test_a_bad_plan_creates_nothing_and_goes_back_to_plan(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    note = rejected(env, monkeypatch, plan_text(sub("a"), sub("b", paths=["ours/hooks/x.py"])))
    assert "контур контроля" in note and "failures: 1 of 2" in note and "moved to: Plan" in note
    assert env.state() == "Plan" and env.counter() == 1
    assert BRANCH not in env.branches()
    (event,) = env.events()
    assert event["moved_to"] == "Plan" and "контур контроля" in event["reason"] and "needs_human" not in event


def test_the_second_rejection_calls_a_human(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    rejected(env, monkeypatch, plan_text(sub(), version=2))
    assert env.state() == "Plan"
    env.task_card()  # the manager hands in again: back to Subtasks
    assert env.hook(monkeypatch) == 0
    assert env.state() == "needs_human" and env.counter() == 2 and env.cards() == []
    assert "failures: 2 of 2" in env.body() and "moved to: needs_human" in env.body()


def test_max_failures_comes_from_project_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path, plan_section="max_failures = 1")
    monkeypatch.setenv("SYMPHONY_WORKFLOW_DIR", str(env.board))
    monkeypatch.setenv("SYMPHONY_ISSUE_ID", TASK)
    monkeypatch.setenv("SYMPHONY_PYTHON", sys.executable)
    rejected(env, monkeypatch, plan_text(sub(), version=2))
    assert env.state() == "needs_human" and env.counter() == 1


def test_a_long_list_of_reasons_is_cut(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    items = [sub(f"k{n}", extra="x") for n in range(5)]
    note = rejected(env, monkeypatch, plan_text(*items, version=2, extra="more = 1"))
    # 1 (version) + 1 (top key) + 5 (extra keys) = 7: all listed; now many more
    assert reasons_of(note).count("\n- ") + 1 == 7
    many = [sub("k", criteria=["x"] * 6, paths=[f"ours/hooks/f{n}" for n in range(3)]) for _ in range(12)]
    env.task_card()
    env.plan(plan_text(*many))
    assert env.hook(monkeypatch) == 0
    body = reasons_of(env.body().split("## Plan Failure")[-1])
    assert re.search(r"- и ещё \d+", body) and body.count("\n- ") + 1 == 21


# ---------------------------------------------------------------------------
# at once to needs_human: nobody can fix it in the plan
# ---------------------------------------------------------------------------


def immediate(env: Env, monkeypatch: pytest.MonkeyPatch, fragment: str) -> None:
    env.plan(plan_text(sub()))
    before = env.counter()
    assert env.hook(monkeypatch) == 0
    assert env.state() == "needs_human"
    assert env.counter() == before, "no attempt is spent"
    assert fragment in env.body().split("## Plan Failure")[-1]
    (event,) = env.events()
    assert event["outcome"] == "rejected" and event["needs_human"] == "environment"
    assert BRANCH not in env.branches()


def test_integration_off_calls_a_human_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path, integration=False)
    for key, value in (("SYMPHONY_WORKFLOW_DIR", str(env.board)), ("SYMPHONY_ISSUE_ID", TASK),
                       ("SYMPHONY_PYTHON", sys.executable)):
        monkeypatch.setenv(key, value)
    env.task_card()
    immediate(env, monkeypatch, "интеграция выключена")


def test_an_existing_subtask_card_calls_a_human_at_once(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.task_card()
    write(env.kanban / "OMS-9-1.md", "---\nid: OMS-9-1\nstate: Todo\n---\n\nold\n")
    immediate(env, monkeypatch, "уже есть карточки подзадач")
    assert env.cards() == ["OMS-9-1"] and env.raw("OMS-9-1").endswith("old\n")


def test_empty_levels_call_a_human_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path, levels="")
    for key, value in (("SYMPHONY_WORKFLOW_DIR", str(env.board)), ("SYMPHONY_ISSUE_ID", TASK),
                       ("SYMPHONY_PYTHON", sys.executable)):
        monkeypatch.setenv(key, value)
    env.task_card(level=None)
    immediate(env, monkeypatch, "пуст раздел [levels]")


def test_several_environment_reasons_are_listed_together(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path, integration=False, levels="")
    for key, value in (("SYMPHONY_WORKFLOW_DIR", str(env.board)), ("SYMPHONY_ISSUE_ID", TASK),
                       ("SYMPHONY_PYTHON", sys.executable)):
        monkeypatch.setenv(key, value)
    env.task_card(level=None)
    env.plan(plan_text(sub()))
    assert env.hook(monkeypatch) == 0
    note = env.body()
    assert "интеграция выключена" in note and "пуст раздел [levels]" in note and env.state() == "needs_human"


def test_the_environment_is_checked_before_the_plan_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path, integration=False)
    for key, value in (("SYMPHONY_WORKFLOW_DIR", str(env.board)), ("SYMPHONY_ISSUE_ID", TASK),
                       ("SYMPHONY_PYTHON", sys.executable)):
        monkeypatch.setenv(key, value)
    env.task_card()  # no plan file at all
    assert env.hook(monkeypatch) == 0
    assert env.state() == "needs_human" and "нет файла плана" not in env.body()


def test_a_task_id_that_cannot_make_subtask_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path)
    monkeypatch.setenv("SYMPHONY_WORKFLOW_DIR", str(env.board))
    monkeypatch.setenv("SYMPHONY_PYTHON", sys.executable)
    monkeypatch.setenv("SYMPHONY_ISSUE_ID", "OMS-1.2")
    write(env.kanban / "OMS-1.2.md", "---\nid: OMS-1.2\ntitle: t\nstate: Subtasks\nlabels: [role:manager-plan]\n---\n\nb\n")
    write(env.board / "artifacts" / "OMS-1.2" / "work" / "plan.toml", plan_text(sub()))
    monkeypatch.chdir(env.ws)
    assert plan.hook_main() == 0
    assert check.read_card_state(env.kanban / "OMS-1.2.md") == "needs_human"
    assert "без точек" in (env.kanban / "OMS-1.2.md").read_text()


# ---------------------------------------------------------------------------
# a failure in the middle
# ---------------------------------------------------------------------------


def test_a_failure_on_the_second_card_retains_the_first_staged(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.use_symphony(BREAK_ON_SECOND)
    env.task_card()
    env.plan(plan_text(sub("a"), sub("b"), sub("c")))
    assert env.hook(monkeypatch) == 0
    assert env.cards() == ['OMS-9-1']
    assert env.state('OMS-9-1') == 'Subtasks'
    assert env.state() == "needs_human" and env.counter() is None
    note = env.body()
    assert "не создалась карточка OMS-9-2 (подзадача 2 из 3)" in note and "boom on the second card" in note
    assert "## Plan Accepted" not in note
    assert [e["outcome"] for e in env.events()] == ["rejected"]


def test_partial_creation_resumes_the_same_ids_on_retry(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.use_symphony(BREAK_ON_SECOND)
    env.task_card()
    env.plan(plan_text(sub("a"), sub("b")))
    assert env.hook(monkeypatch) == 0
    assert env.cards() == ['OMS-9-1']
    ledger = env.kanban / ".symphony-identifiers.json"
    assert "oms-9-1" in json.loads(ledger.read_text())["issued"]

    env.symphony = str(REAL_SYMPHONY)
    env.write_project()
    assert plan.recover_main(TASK, env.board) == 0
    assert env.cards() == ['OMS-9-1', 'OMS-9-2']
    assert env.state() == 'Subtasks'
    assert all(env.state(sid) == 'In Progress' for sid in env.cards())
    assert env.counter() is None
    assert env.body().count('## Plan Accepted') == 1


def test_partial_creation_keeps_foreign_cards_unchanged(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.use_symphony(BREAK_ON_SECOND)
    env.task_card()
    other = write(env.kanban / "OMS-8-1.md", "---\nid: OMS-8-1\nstate: Todo\n---\n\nmine\n")
    before = other.read_bytes()
    env.plan(plan_text(sub("a"), sub("b")))
    assert env.hook(monkeypatch) == 0
    assert other.read_bytes() == before and env.cards() == ['OMS-9-1']


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_board_write_failure_retains_staged_cards_and_calls_a_human(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.use_symphony(LOCK_ON_FIRST)
    env.task_card()
    env.plan(plan_text(sub("a"), sub("b")))
    try:
        assert env.hook(monkeypatch) == 0
    finally:
        env.kanban.chmod(0o755)
    assert env.cards() == ["OMS-9-1"]
    (event,) = [e for e in env.events() if e["outcome"] == "rejected"]
    assert event["needs_human"] == "environment"
    assert "сохранено в ожидании: OMS-9-1.md" in event["reason"]
    assert env.counter() is None


def test_a_missing_symphony_binary_creates_nothing(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.symphony = str(env.tmp / "no-such-symphony")
    env.write_project()
    env.task_card()
    env.plan(plan_text(sub()))
    assert env.hook(monkeypatch) == 0
    assert env.cards() == []
    assert any("symphony" in e["reason"] for e in env.events())


# ---------------------------------------------------------------------------
# silence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["Plan", "In Progress", "Human Review", "Done", "needs_human", "Todo"])
def test_a_card_not_in_subtasks_is_passed_over_silently(env: Env, monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    env.task_card(state=state)
    env.plan(plan_text(sub()))
    before = env.raw(TASK)
    assert env.hook(monkeypatch) == 0
    assert env.raw(TASK) == before and env.cards() == [] and env.events() == []
    assert not (env.board / ".ours-state" / "OMS-9.plan.json").exists()


def test_a_card_in_subtasks_without_the_label_is_skipped_with_a_reason(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.task_card(labels="[role:worker]")
    env.plan(plan_text(sub()))
    before = env.raw(TASK)
    assert env.hook(monkeypatch) == 0
    assert env.raw(TASK) == before and env.cards() == [] and env.state() == "Subtasks"
    (event,) = env.events()
    assert event["outcome"] == "skipped" and "role:manager-plan" in event["reason"]


def test_a_card_with_plan_accepted_is_passed_over_silently(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.task_card(body="task body\n\n## Plan Accepted\n\n- OMS-9-1 — x — simple\n")
    env.plan(plan_text(sub(), version=2))
    before = env.raw(TASK)
    assert env.hook(monkeypatch) == 0
    assert env.raw(TASK) == before and env.events() == [] and env.cards() == []


def test_the_hook_never_fails(env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(env.ws)
    monkeypatch.delenv("SYMPHONY_WORKFLOW_DIR")
    assert plan.hook_main() == 0 and "SYMPHONY_WORKFLOW_DIR" in capsys.readouterr().err
    monkeypatch.setenv("SYMPHONY_WORKFLOW_DIR", str(env.board))
    assert plan.hook_main() == 0  # there is no card file at all
    assert any(e["outcome"] == "error" for e in env.events())
    assert "plan.py hook:" in capsys.readouterr().err
    assert plan.brief_main() == 0


def test_cli_usage_and_exit_codes(env: Env) -> None:
    result = subprocess.run([sys.executable, str(PLAN), "nonsense"], capture_output=True, text=True, check=False)
    assert result.returncode == 2 and "plan.py hook | plan.py brief" in result.stderr
    result = subprocess.run([sys.executable, str(PLAN), "hook"], capture_output=True, text=True, check=False,
                            env={"PATH": os.environ.get("PATH", "")}, cwd=env.ws)
    assert result.returncode == 0 and "SYMPHONY_WORKFLOW_DIR" in result.stderr


# ---------------------------------------------------------------------------
# the rendered hooks of the template
# ---------------------------------------------------------------------------


def rendered_hooks(tmp_path: Path):
    from symphony.workflow import build_service_config, load_workflow

    board = tmp_path / "board"
    board.mkdir()
    code = make_repo(tmp_path / "code")
    data = {
        "project": {"name": "demo", "repo": str(code), "base_branch": "main", "id_prefix": "OMS", "port": 9971},
        "tools": {"python": "/usr/bin/python3", "symphony": "/bin/symphony", "claude": "/bin/claude"},
        "integration": {"enabled": True},
    }
    write(board / "WORKFLOW.md", pc.render_workflow(board, pc.validate_project(data)))
    return build_service_config(load_workflow(board / "WORKFLOW.md")).hooks


def run_script(tmp_path: Path, script: str, *, change: bool) -> list[str]:
    ws = make_repo(tmp_path / "ws")
    calls = tmp_path / "calls.txt"
    fake = write(tmp_path / "fake-python", f'#!/bin/sh\necho "$@" >> {calls}\n')
    fake.chmod(0o755)
    if change:
        write(ws / "new.txt", "new\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("SYMPHONY_")}
    env.update(SYMPHONY_PYTHON=str(fake), SYMPHONY_WORKFLOW_DIR=str(tmp_path / "board"))
    proc = subprocess.run(["/bin/bash", "-c", script], cwd=ws, env=env, capture_output=True, text=True,
                          timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return [line.rsplit("/", 1)[-1] for line in calls.read_text().splitlines()]


@pytest.mark.parametrize("change", [False, True])
def test_after_run_calls_plan_py_second_with_and_without_changes(tmp_path: Path, change: bool) -> None:
    hooks = rendered_hooks(tmp_path)
    assert run_script(tmp_path, hooks.after_run, change=change) == [
        "check.py hook", "plan.py hook", "integrate.py hook"]


def test_before_run_calls_plan_py_brief_last(tmp_path: Path) -> None:
    hooks = rendered_hooks(tmp_path)
    commands = [line.strip() for line in hooks.before_run.splitlines() if line.strip() and not line.strip().startswith("#")]
    assert commands[-1] == f'"$SYMPHONY_PYTHON" {OURS / "hooks" / "plan.py"} brief || true'
    # the board has no kanban / artifacts here: the loop of the hook passes, the last line runs
    board = tmp_path / "board"
    ws = make_repo(tmp_path / "ws")
    calls = tmp_path / "calls.txt"
    fake = write(tmp_path / "fake-python", f'#!/bin/sh\necho "$@" >> {calls}\n')
    fake.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SYMPHONY_")}
    env.update(SYMPHONY_PYTHON=str(fake), SYMPHONY_WORKFLOW_DIR=str(board))
    proc = subprocess.run(["/bin/bash", "-c", hooks.before_run], cwd=ws, env=env, capture_output=True,
                          text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    assert [line.rsplit("/", 1)[-1] for line in calls.read_text().splitlines()] == ["plan.py brief"]


# ---------------------------------------------------------------------------
# brief
# ---------------------------------------------------------------------------


class _Budget(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - the http.server name
        body = json.dumps({"budgets": {"available": True, "items": [
            {"name": "attempt_turns", "limit": 12, "used": 3, "remaining": 9, "status": "remaining"},
            {"name": "total_turns", "limit": 20, "used": 3, "remaining": 17, "status": "remaining"},
        ]}}).encode()
        self.send_response(200 if self.path.endswith("/OMS-9") else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def service():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Budget)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_brief_lists_the_levels_not_above_the_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path, port=free_port())
    for key, value in (("SYMPHONY_WORKFLOW_DIR", str(env.board)), ("SYMPHONY_ISSUE_ID", TASK),
                       ("SYMPHONY_PYTHON", sys.executable)):
        monkeypatch.setenv(key, value)
    env.task_card(state="Plan", level="standard")
    assert env.brief(monkeypatch) == 0
    text = env.limits().read_text(encoding="utf-8")
    assert "| simple | haiku | medium | 30 | 30 |" in text
    assert "| standard | haiku | high | 16 | 30 |" in text  # its own max_turns, the project's max_state_turns
    assert "complex" not in text and "sonnet" not in text
    assert "- max_subtasks: 5" in text and "plan failures so far: 0 of 2" in text
    assert "бюджет недоступен" in text, "no service on that port"
    assert 'docs/OMS-9/work/plan.toml' in text and 'entry "S4-19"' in text


def test_brief_with_the_budget_of_the_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service: int) -> None:
    env = Env(tmp_path, port=service)
    for key, value in (("SYMPHONY_WORKFLOW_DIR", str(env.board)), ("SYMPHONY_ISSUE_ID", TASK),
                       ("SYMPHONY_PYTHON", sys.executable)):
        monkeypatch.setenv(key, value)
    env.task_card(state="Plan", level="complex")
    write(plan.counter_path(env.board / ".ours-state", TASK), json.dumps({"failures": 1}))
    assert env.brief(monkeypatch) == 0
    text = env.limits().read_text(encoding="utf-8")
    assert "- attempt_turns: limit 12, used 3, remaining 9" in text and "- total_turns: limit 20" in text
    assert "бюджет недоступен" not in text
    assert "| complex | sonnet | high |" in text and "plan failures so far: 1 of 2" in text


def test_brief_is_silent_for_other_cards(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    for state, labels in (("Subtasks", "[role:manager-plan]"), ("In Progress", "[role:manager-plan]"),
                          ("Plan", "[role:worker]"), ("Plan", "")):
        env.task_card(state=state, labels=labels)
        assert env.brief(monkeypatch) == 0
        assert not env.limits().exists(), (state, labels)
    assert env.events() == []


def test_brief_never_writes_through_a_link(env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    outside = env.tmp / "outside"
    outside.mkdir()
    work = env.board / "artifacts" / TASK / "work"
    work.parent.mkdir(parents=True)
    work.symlink_to(outside)
    env.task_card(state="Plan")
    assert env.brief(monkeypatch) == 0
    assert list(outside.iterdir()) == []
    assert any(e["outcome"] == "error" for e in env.events())
    assert "plan.py brief:" in capsys.readouterr().err


def test_brief_without_levels_has_no_ceiling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = Env(tmp_path, levels=LEVELS.replace('default_level = "simple"\n', ""), port=free_port())
    for key, value in (("SYMPHONY_WORKFLOW_DIR", str(env.board)), ("SYMPHONY_ISSUE_ID", TASK),
                       ("SYMPHONY_PYTHON", sys.executable)):
        monkeypatch.setenv(key, value)
    env.task_card(state="Plan", level=None)
    assert env.brief(monkeypatch) == 0
    text = env.limits().read_text(encoding="utf-8")
    assert "не задан" in text and "| complex |" in text and "| simple |" in text


# ---------------------------------------------------------------------------
# [plan] in project.toml
# ---------------------------------------------------------------------------


def project(**plan_keys: object) -> dict:
    data: dict = {
        "project": {"name": "demo", "repo": "/repo", "base_branch": "main", "id_prefix": "OMS", "port": 9971},
        "tools": {"python": "/usr/bin/python3", "symphony": "/bin/symphony", "claude": "/bin/claude"},
    }
    if plan_keys:
        data["plan"] = dict(plan_keys)
    return pc.validate_project(data)


def test_plan_defaults_are_always_filled() -> None:
    assert project()["plan"] == {"max_subtasks": 5, "max_failures": 2}
    assert project(max_subtasks=3)["plan"] == {"max_subtasks": 3, "max_failures": 2}
    assert project(max_failures=4)["plan"] == {"max_subtasks": 5, "max_failures": 4}


@pytest.mark.parametrize("value", [1, 20])
def test_max_subtasks_limits_are_inclusive(value: int) -> None:
    assert project(max_subtasks=value)["plan"]["max_subtasks"] == value


@pytest.mark.parametrize("value", [0, -1, 21, True, "5", 2.5])
def test_max_subtasks_outside_one_to_twenty_is_refused(value: object) -> None:
    with pytest.raises(pc.ProjectError, match="max_subtasks"):
        project(max_subtasks=value)


@pytest.mark.parametrize("value", [0, -3, False, "2"])
def test_max_failures_must_be_a_positive_integer(value: object) -> None:
    with pytest.raises(pc.ProjectError, match="max_failures"):
        project(max_failures=value)


def test_an_unknown_plan_key_is_refused() -> None:
    with pytest.raises(pc.ProjectError, match="неизвестный ключ.*max_subtask"):
        project(max_subtask=3)


def test_a_board_like_oms_without_plan_passes(tmp_path: Path) -> None:
    text = """\
[project]
name = "oms"
repo = "/repo"
base_branch = "our-main"
id_prefix = "OMS"
port = 9971
[tools]
python = "/usr/bin/python3"
symphony = "/bin/symphony"
claude = "/bin/claude"
[limits]
max_turns = 12
max_attempts = 2
"""
    write(tmp_path / "project.toml", text)
    loaded = pc.load_project(tmp_path)
    assert loaded["plan"] == pc.DEFAULT_PLAN and loaded["limits"]["max_turns"] == 12
    assert pc.template_values(tmp_path, loaded)["plan_py"] == str(OURS / "hooks" / "plan.py")


def test_project_new_template_has_the_commented_plan_section() -> None:
    text = (OURS / "bin" / "project-new").read_text(encoding="utf-8")
    assert "[plan]" in text and "# max_subtasks = 5" in text and "# max_failures = 2" in text
    assert "Порядок уровней — порядок, в котором они объявлены" in text
