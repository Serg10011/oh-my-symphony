"""Tests for the integration branch of a task (TASK-S4-11): the [integration]
section of project.toml, ours/hooks/integrate.py and the start branch chosen
by ours/hooks/worktree.py.

Real temporary git repositories; worktree.create runs in-process against a
hand-made board folder (project.toml, empty setup.sh / test.sh, kanban/) with
the real `symphony._worktree_cleanup --prepare-setup`. The integrate.py CLI
runs as a subprocess.

Run: python -m pytest ours/tests -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the hooks are POSIX-only")

OURS = Path(__file__).resolve().parents[1]
INTEGRATE = OURS / "hooks" / "integrate.py"
sys.path.insert(0, str(OURS / "hooks"))

import check  # noqa: E402
import integrate  # noqa: E402
import project_config as pc  # noqa: E402
import worktree  # noqa: E402

PREFIX = "OMS"
TASK = "OMS-7"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def project_data(repo: str = "/repo", **integration: object) -> dict:
    data: dict = {
        "project": {"name": "demo", "repo": repo, "base_branch": "main",
                    "id_prefix": PREFIX, "port": 9971},
        "tools": {"python": "/usr/bin/python3", "symphony": "/bin/symphony", "claude": "/bin/claude"},
    }
    if integration:
        data["integration"] = dict(integration)
    return data


def project(**integration: object) -> dict:
    return pc.validate_project(project_data(**integration))


def card_text(state: str = "Todo", *, labels: str = "[role:worker]", request: str | None = TASK) -> str:
    lines = ["---", "id: x", f"state: {state}"]
    if labels:
        lines.append(f"labels: {labels}")
    if request is not None:
        lines.append(f"request: {request}")
    return "\n".join([*lines, "---", "", "body", ""])


# ---------------------------------------------------------------------------
# read_card_front
# ---------------------------------------------------------------------------


def front_of(tmp_path: Path, text: str) -> dict:
    return integrate.read_card_front(write(tmp_path / "card.md", text))


@pytest.mark.parametrize(
    ("labels_lines", "expected"),
    [
        ("labels: [role:worker, x]", ("role:worker", "x")),
        ("labels: [Role:Worker, 'X Y', \"z\"]", ("role:worker", "x y", "z")),
        ("labels: []", ()),
        ("labels: [a, b]  # note", ("a", "b")),
        ("labels:\n  - role:worker\n  - X", ("role:worker", "x")),
        ("labels:\n- role:worker\n- 'quoted'", ("role:worker", "quoted")),
        ("labels:", ()),
        ("labels: null", ()),
        ("", ()),
    ],
)
def test_read_card_front_labels(tmp_path: Path, labels_lines: str, expected: tuple) -> None:
    text = "---\nid: OMS-1\nstate: Todo\n" + (labels_lines + "\n" if labels_lines else "") + "title: t\n---\nbody\n"
    front = front_of(tmp_path, text)
    assert front["labels"] == expected
    assert front["state"] == "Todo" and front["request"] is None


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("request: OMS-7", "OMS-7"),
        ("request: OMS-7   # parent task", "OMS-7"),
        ("request: 'OMS-7'", "OMS-7"),
        ('request: "OMS-7" # note', "OMS-7"),
        ("request:", None),
        ("request: ''", None),
    ],
)
def test_read_card_front_request(tmp_path: Path, line: str, expected: str | None) -> None:
    front = front_of(tmp_path, f"---\nstate: 'In Progress'\n{line}\nlabels: [a]\n---\nbody\n")
    assert front == {"state": "In Progress", "request": expected, "labels": ("a",)}


def test_read_card_front_ignores_other_keys_and_the_body(tmp_path: Path) -> None:
    text = "---\nstate: Todo\nlevel: L1\nrequest: OMS-7\nblocked_by:\n  - OMS-1\n---\nlabels: [x]\nrequest: OMS-9\n"
    assert front_of(tmp_path, text) == {"state": "Todo", "request": "OMS-7", "labels": ()}


def test_read_card_front_without_delimiters(tmp_path: Path) -> None:
    front = front_of(tmp_path, "state: Todo\nlabels: [role:worker]\nrequest: OMS-7\n\nbody\n")
    assert front == {"state": "Todo", "request": "OMS-7", "labels": ("role:worker",)}


@pytest.mark.parametrize(
    "text",
    [
        "---\nlabels: [a]\n---\nno state\n",
        "---\nstate: Todo\nlabels: role:worker\n---\n",
        "---\nstate: Todo\nlabels: [a, b\n---\n",
        "",
    ],
)
def test_read_card_front_unreadable_front(tmp_path: Path, text: str) -> None:
    with pytest.raises(integrate.IntegrateError):
        front_of(tmp_path, text)


def test_read_card_front_missing_file(tmp_path: Path) -> None:
    with pytest.raises(integrate.IntegrateError, match="не читается"):
        integrate.read_card_front(tmp_path / "nope.md")


# ---------------------------------------------------------------------------
# task_of
# ---------------------------------------------------------------------------

WORKER = {"state": "Todo", "labels": ("role:worker",), "request": TASK}


def test_task_of_off_by_default() -> None:
    assert integrate.task_of(WORKER, project()) is None
    assert integrate.task_of(WORKER, project(enabled=False)) is None


def test_task_of_needs_label_and_request() -> None:
    on = project(enabled=True)
    assert integrate.task_of({**WORKER, "labels": ("other",)}, on) is None
    assert integrate.task_of({**WORKER, "labels": ()}, on) is None
    assert integrate.task_of({**WORKER, "request": None}, on) is None
    assert integrate.task_of(WORKER, on, "OMS-12") == TASK


def test_task_of_label_is_configurable_and_case_insensitive() -> None:
    on = project(enabled=True, worker_label="Team:Dev")
    assert integrate.task_of({**WORKER, "labels": ("team:dev",)}, on) == TASK
    assert integrate.task_of(WORKER, on) is None


@pytest.mark.parametrize(
    ("request_id", "card_id"),
    [("../x", "OMS-12"), ("OTHER-1", "OMS-12"), ("OMS-12", "OMS-12"), ("OMS 7", "OMS-12"),
     ("OMS-", "OMS-12"), ("-OMS-7", "OMS-12"), ("OMS-7/../x", "OMS-12")],
)
def test_task_of_refuses_a_wrong_request(request_id: str, card_id: str) -> None:
    with pytest.raises(integrate.IntegrateError):
        integrate.task_of({**WORKER, "request": request_id}, project(enabled=True), card_id)


def test_integration_branch_uses_the_prefix() -> None:
    assert integrate.integration_branch(project(), TASK) == "integration/OMS-7"
    assert integrate.integration_branch(project(branch_prefix="int/"), TASK) == "int/OMS-7"


# ---------------------------------------------------------------------------
# [integration] in project.toml
# ---------------------------------------------------------------------------


def test_integration_defaults() -> None:
    expected = {"enabled": False, "branch_prefix": "integration/", "worker_label": "role:worker"}
    assert project()["integration"] == expected
    assert project(enabled=True)["integration"] == {**expected, "enabled": True}
    assert pc.validate_project(project_data())["integration"] == expected


@pytest.mark.parametrize(
    "prefix",
    ["integration", "symphony/", "attempts/", "-x/", "a b/", "a/../b/", "x\ty/"],
)
def test_integration_refuses_a_bad_branch_prefix(prefix: str) -> None:
    with pytest.raises(pc.ProjectError, match="branch_prefix"):
        project(branch_prefix=prefix)


def test_integration_refuses_unknown_key_and_wrong_types() -> None:
    with pytest.raises(pc.ProjectError, match="неизвестный ключ.*typo"):
        project(typo=1)
    with pytest.raises(pc.ProjectError, match="enabled"):
        project(enabled="yes")
    with pytest.raises(pc.ProjectError, match="worker_label"):
        project(worker_label="")


def test_project_new_template_has_the_commented_section() -> None:
    text = (OURS / "bin" / "project-new").read_text(encoding="utf-8")
    assert "[integration]" in text and "# enabled = false" in text


# ---------------------------------------------------------------------------
# ensure_integration_branch
# ---------------------------------------------------------------------------


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.invalid")
    git(path, "config", "user.name", "t")
    write(path / "README.md", "readme\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "base")
    return path


def advance(repo: Path, branch: str, name: str) -> str:
    """Commit on `branch` without touching any checkout; return the new tip."""
    tip = git(repo, "rev-parse", f"refs/heads/{branch}")
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=repo, input=name, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    index = subprocess.run(
        ["git", "ls-tree", "-r", tip], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout + f"100644 blob {blob}\t{name}\n"
    tree = subprocess.run(
        ["git", "mktree"], cwd=repo, input=index, check=True, capture_output=True, text=True,
    ).stdout.strip()
    commit = git(repo, "commit-tree", tree, "-p", tip, "-m", name)
    git(repo, "update-ref", f"refs/heads/{branch}", commit, tip)
    return commit


def test_ensure_creates_from_the_base_tip(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "code")
    sha, created = integrate.ensure_integration_branch(repo, "integration/OMS-7", "main")
    assert created is True
    assert sha == git(repo, "rev-parse", "main") == git(repo, "rev-parse", "integration/OMS-7")


def test_ensure_never_moves_an_existing_branch(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "code")
    first, _ = integrate.ensure_integration_branch(repo, "integration/OMS-7", "main")
    advance(repo, "main", "later.txt")
    again, created = integrate.ensure_integration_branch(repo, "integration/OMS-7", "main")
    assert (again, created) == (first, False)
    assert git(repo, "rev-parse", "main") != first
    tip = advance(repo, "integration/OMS-7", "work.txt")
    assert integrate.ensure_integration_branch(repo, "integration/OMS-7", "main") == (tip, False)


def test_ensure_refuses_a_missing_base_and_a_bad_name(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "code")
    with pytest.raises(integrate.IntegrateError, match="нет ветки refs/heads/nope"):
        integrate.ensure_integration_branch(repo, "integration/OMS-7", "nope")
    assert git(repo, "branch", "--list", "integration/*") == ""
    for bad in ("integration/a..b", "integration/a b", "-x"):
        with pytest.raises(integrate.IntegrateError, match="не годится"):
            integrate.ensure_integration_branch(repo, bad, "main")


# ---------------------------------------------------------------------------
# a board folder and the hook
# ---------------------------------------------------------------------------


class Env:
    def __init__(self, tmp: Path, **integration: object) -> None:
        self.tmp = tmp
        self.code = make_repo(tmp / "code")
        self.board = tmp / "projects" / "demo"
        self.ws_root = tmp / "workspaces"
        self.ws_root.mkdir()
        (self.board / "kanban").mkdir(parents=True)
        write(self.board / "setup.sh", "exit 0\n")
        write(self.board / "test.sh", "exit 0\n")
        self.set_integration(**integration)

    def set_integration(self, **integration: object) -> None:
        lines = [
            "[project]", 'name = "demo"', f"repo = {json.dumps(str(self.code))}",
            'base_branch = "main"', f'id_prefix = "{PREFIX}"', "port = 9971",
            "[tools]", f"python = {json.dumps(sys.executable)}", 'symphony = "/bin/symphony"',
            'claude = "/bin/claude"',
        ]
        if integration:
            lines.append("[integration]")
            lines += [f"{key} = {json.dumps(value)}" for key, value in integration.items()]
        write(self.board / "project.toml", "\n".join(lines) + "\n")

    def card(self, ident: str, text: str) -> None:
        write(self.board / "kanban" / f"{ident}.md", text)

    def ws(self, ident: str) -> Path:
        return self.ws_root / ident

    def start(self, ident: str, monkeypatch: pytest.MonkeyPatch) -> int:
        ws = self.ws(ident)
        ws.mkdir(exist_ok=True)  # the core pre-creates the empty workspace
        monkeypatch.chdir(ws)
        return worktree.main(["create"])

    def record(self, ident: str) -> dict:
        return json.loads(pc.base_record_path(self.board, ident).read_text(encoding="utf-8"))

    def branches(self) -> list[str]:
        return git(self.code, "branch", "--format=%(refname:short)").split()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    for key in [k for k in os.environ if k.startswith("SYMPHONY_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("SYMPHONY_WORKFLOW_DIR", str(tmp_path / "projects" / "demo"))
    monkeypatch.setenv("SYMPHONY_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("SYMPHONY_PYTHON", sys.executable)
    return Env(tmp_path, enabled=True)


def test_an_ordinary_card_starts_from_the_base_branch(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env.card("OMS-12", card_text(labels="[backend]", request=None))
    assert env.start("OMS-12", monkeypatch) == 0
    main_tip = git(env.code, "rev-parse", "main")
    assert git(env.ws("OMS-12"), "rev-parse", "HEAD") == main_tip
    assert git(env.ws("OMS-12"), "config", "--worktree", "symphony.basebranch") == "main"
    assert env.record("OMS-12") == {
        **env.record("OMS-12"), "base_sha": main_tip, "base_branch": "main",
        "start_branch": "main", "task": None,
    }
    assert "integration/OMS-7" not in env.branches()
    assert f"на symphony/OMS-12 от main ({main_tip[:12]})" in capsys.readouterr().err


def test_a_subtask_starts_from_the_integration_branch(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env.card("OMS-12", card_text())
    assert env.start("OMS-12", monkeypatch) == 0
    main_tip = git(env.code, "rev-parse", "main")
    assert git(env.code, "rev-parse", "integration/OMS-7") == main_tip
    ws = env.ws("OMS-12")
    assert git(ws, "rev-parse", "HEAD") == main_tip
    assert git(ws, "symbolic-ref", "HEAD") == "refs/heads/symphony/OMS-12"
    for key in ("symphony.basebranch", "symphony.mergetargetbranch"):
        assert git(ws, "config", "--worktree", key) == "integration/OMS-7"
    assert git(ws, "config", "--worktree", "symphony.basesha") == main_tip
    record = env.record("OMS-12")
    assert (record["task"], record["start_branch"]) == (TASK, "integration/OMS-7")
    assert (record["base_branch"], record["base_sha"]) == ("main", main_tip)
    err = capsys.readouterr().err
    assert f"на symphony/OMS-12 от integration/OMS-7 (задача OMS-7, {main_tip[:12]})" in err
    # check.py reads only base_sha: the extra record fields change nothing for it.
    assert pc.read_base_record(env.board, "OMS-12")["base_sha"] == main_tip
    assert check.resolve_base(ws, env.board, "OMS-12")[1] == main_tip


def test_the_second_subtask_starts_from_the_new_tip(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.card("OMS-12", card_text())
    env.card("OMS-13", card_text())
    assert env.start("OMS-12", monkeypatch) == 0
    first_tip = git(env.code, "rev-parse", "integration/OMS-7")
    tip = advance(env.code, "integration/OMS-7", "from-oms-12.txt")
    advance(env.code, "main", "main-moves-on.txt")
    assert env.start("OMS-13", monkeypatch) == 0
    ws = env.ws("OMS-13")
    assert git(ws, "rev-parse", "HEAD") == tip != first_tip
    assert (ws / "from-oms-12.txt").is_file() and not (ws / "main-moves-on.txt").exists()
    assert env.record("OMS-13")["base_sha"] == tip
    assert git(env.code, "rev-parse", "integration/OMS-7") == tip


def test_off_means_the_card_is_not_read(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.set_integration(enabled=False)
    env.card("OMS-12", "\x00 not a card\n")
    assert env.start("OMS-12", monkeypatch) == 0
    assert env.record("OMS-12")["start_branch"] == "main"
    assert git(env.ws("OMS-12"), "rev-parse", "HEAD") == git(env.code, "rev-parse", "main")
    assert not [b for b in env.branches() if b.startswith("integration/")]
    # no [integration] section at all: the same
    env.set_integration()
    env.card("OMS-13", card_text())
    assert env.start("OMS-13", monkeypatch) == 0
    assert env.record("OMS-13")["task"] is None


@pytest.mark.parametrize("request_id", ["../x", "OTHER-1", "OMS-12", "OMS 7"])
def test_a_wrong_request_stops_the_start(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], request_id: str
) -> None:
    env.card("OMS-12", card_text(request=request_id))
    assert env.start("OMS-12", monkeypatch) == 1
    assert "отказ:" in capsys.readouterr().err
    assert not (env.ws("OMS-12") / ".git").exists()
    assert "symphony/OMS-12" not in env.branches()
    assert not [b for b in env.branches() if b.startswith("integration/")]
    assert not pc.base_record_path(env.board, "OMS-12").exists()


def test_a_missing_or_unreadable_card_stops_the_start(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert env.start("OMS-12", monkeypatch) == 1
    assert "нет карточки" in capsys.readouterr().err
    env.card("OMS-12", "no front matter state\n")
    assert env.start("OMS-12", monkeypatch) == 1
    assert "нет state" in capsys.readouterr().err
    assert not (env.ws("OMS-12") / ".git").exists() and "symphony/OMS-12" not in env.branches()


def test_a_failed_setup_keeps_the_integration_branch(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(env.board / "setup.sh", "exit 3\n")
    env.card("OMS-12", card_text())
    assert env.start("OMS-12", monkeypatch) == 1
    assert "symphony/OMS-12" not in env.branches()
    assert not pc.base_record_path(env.board, "OMS-12").exists()
    assert "integration/OMS-7" in env.branches()


def test_a_clean_restart_does_not_touch_the_integration_branch(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manager card of the task runs on symphony/<TASK>; its restart parks that
    branch, and the integration branch survives under its own name."""
    env.card("OMS-12", card_text())
    assert env.start("OMS-12", monkeypatch) == 0
    tip = advance(env.code, "integration/OMS-7", "work.txt")
    env.card(TASK, card_text(labels="[role:manager-plan]", request=None))
    assert env.start(TASK, monkeypatch) == 0
    git(env.code, "worktree", "remove", "--force", str(env.ws(TASK)))
    assert env.start(TASK, monkeypatch) == 0
    assert any(b.startswith(f"attempts/{TASK}-") for b in env.branches())
    assert git(env.code, "rev-parse", "integration/OMS-7") == tip


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def run_cli(*args: str, board: Path | None = None) -> subprocess.CompletedProcess[str]:
    cli_env = {k: v for k, v in os.environ.items() if not k.startswith("SYMPHONY_")}
    if board is not None:
        cli_env["SYMPHONY_WORKFLOW_DIR"] = str(board)
    return subprocess.run(
        [sys.executable, str(INTEGRATE), *args], env=cli_env, capture_output=True, text=True,
        timeout=60, check=False,
    )


def test_cli_ensure_branch(env: Env) -> None:
    first = run_cli("ensure-branch", TASK, board=env.board)
    assert first.returncode == 0, first.stderr
    tip = git(env.code, "rev-parse", "integration/OMS-7")
    assert first.stdout.strip() == f"integrate.py: ветка integration/OMS-7 создана от main ({tip[:12]})"
    advance(env.code, "main", "x.txt")
    second = run_cli("ensure-branch", TASK, "--board", str(env.board))
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == f"integrate.py: ветка integration/OMS-7 уже есть ({tip[:12]})"
    assert git(env.code, "rev-parse", "integration/OMS-7") == tip


def test_cli_refuses_a_bad_task(env: Env) -> None:
    for bad in ("../x", "OTHER-1", "OMS-", "OMS 7"):
        result = run_cli("ensure-branch", bad, board=env.board)
        assert result.returncode == 1 and "отказ:" in result.stderr, bad
    assert not [b for b in env.branches() if b.startswith("integration/")]
    result = run_cli("ensure-branch", TASK, board=env.tmp / "no-board")
    assert result.returncode == 1 and "project.toml" in result.stderr


def test_cli_wrong_arguments(env: Env) -> None:
    assert run_cli(board=env.board).returncode == 2
    assert run_cli("ensure-branch", board=env.board).returncode == 2
    assert run_cli("ensure-branch", TASK, "extra", board=env.board).returncode == 2
    assert run_cli("other", TASK, board=env.board).returncode == 2
    assert run_cli("ensure-branch", TASK, "--board").returncode == 2
    assert run_cli("ensure-branch", TASK).returncode == 2  # no board, no $SYMPHONY_WORKFLOW_DIR


# ---------------------------------------------------------------------------
# [agent] max_concurrent_agents (S4-12)
# ---------------------------------------------------------------------------


def with_agents(count: object, **integration: object) -> dict:
    data = project_data(**integration)
    data["agent"] = {"max_concurrent_agents": count}
    return data


def test_max_concurrent_agents_defaults_to_one() -> None:
    assert project()["agent"]["max_concurrent_agents"] == 1
    assert project(enabled=True)["agent"]["max_concurrent_agents"] == 1


def test_integration_needs_a_single_agent() -> None:
    with pytest.raises(pc.ProjectError, match="рассчитаны на одного агента.*max_concurrent_agents = 1"):
        pc.validate_project(with_agents(2, enabled=True))
    assert pc.validate_project(with_agents(1, enabled=True))["agent"]["max_concurrent_agents"] == 1


def test_several_agents_are_fine_without_integration() -> None:
    assert pc.validate_project(with_agents(2, enabled=False))["agent"]["max_concurrent_agents"] == 2
    assert pc.validate_project(with_agents(3))["agent"]["max_concurrent_agents"] == 3


@pytest.mark.parametrize("bad", [0, -1, "2", True])
def test_max_concurrent_agents_must_be_a_positive_int(bad: object) -> None:
    with pytest.raises(pc.ProjectError, match="max_concurrent_agents"):
        pc.validate_project(with_agents(bad))


def test_rendered_workflow_takes_the_agent_count_from_the_key(tmp_path: Path) -> None:
    board = tmp_path / "board"
    board.mkdir()
    assert "  max_concurrent_agents: 1\n" in pc.render_workflow(board, project())
    assert "  max_concurrent_agents: 3\n" in pc.render_workflow(board, pc.validate_project(with_agents(3)))


def test_project_new_template_warns_about_the_agent_count() -> None:
    text = (OURS / "bin" / "project-new").read_text(encoding="utf-8")
    assert "# max_concurrent_agents = 1" in text


# ---------------------------------------------------------------------------
# integrate.py hook (S4-12)
# ---------------------------------------------------------------------------

FAKE_SYMPHONY = """\
#!{python}
import re, sys
from pathlib import Path
args = sys.argv[1:]
if args[:2] == ["board", "mv"]:
    ident, state = args[2], args[3]
    card = Path(args[args.index("--root") + 1]) / f"{{ident}}.md"
    card.write_text(re.sub(r"(?m)^state:.*$", "state: " + state, card.read_text(), count=1))
"""
NOOP_SYMPHONY = "#!{python}\n"


def point_to_symphony(env: Env, script: str = FAKE_SYMPHONY) -> None:
    """The board's [tools] symphony: a fake that moves cards by editing the file."""
    binary = write(env.tmp / "bin" / "symphony", script.format(python=sys.executable))
    binary.chmod(0o755)
    toml = env.board / "project.toml"
    toml.write_text(toml.read_text().replace('"/bin/symphony"', json.dumps(str(binary))), encoding="utf-8")


@pytest.fixture
def henv(env: Env) -> Env:
    point_to_symphony(env)
    return env


def put_file(repo: Path, branch: str, name: str, content: str) -> str:
    """Commit `name` = `content` on `branch` without touching any checkout."""
    tip = git(repo, "rev-parse", f"refs/heads/{branch}")
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=repo, input=content, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    entries = [line for line in git(repo, "ls-tree", "-r", tip).splitlines() if not line.endswith(f"\t{name}")]
    tree = subprocess.run(
        ["git", "mktree"], cwd=repo, input="\n".join([*entries, f"100644 blob {blob}\t{name}"]) + "\n",
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    commit = git(repo, "commit-tree", tree, "-p", tip, "-m", f"put {name}")
    git(repo, "update-ref", f"refs/heads/{branch}", commit, tip)
    return commit


def review_card(env: Env, ident: str, *, title: str = "Add feature", state: str = "Human Review",
                labels: str = "[role:worker]", request: str | None = TASK) -> None:
    lines = ["---", f"id: {ident}", f"title: {title}", f"state: {state}"]
    if labels:
        lines.append(f"labels: {labels}")
    if request is not None:
        lines.append(f"request: {request}")
    env.card(ident, "\n".join([*lines, "---", "", "body", ""]))


def write_mark(env: Env, ident: str, tree: str, ppid: int | None = None) -> None:
    payload = {"ppid": os.getppid() if ppid is None else ppid, "tree": tree, "head": "0" * 40, "at": "t"}
    write(env.board / ".ours-state" / f"{ident}.pass.json", json.dumps(payload))


def mark_path(env: Env, ident: str) -> Path:
    return env.board / ".ours-state" / f"{ident}.pass.json"


def subtask(env: Env, ident: str, monkeypatch: pytest.MonkeyPatch, files: dict[str, str] | None = None,
            *, title: str = "Add feature", mark: bool = True) -> str:
    """A finished subtask as after_run sees it: started by worktree.py, work committed
    by the standard block, card in Human Review, check.py's pass mark in place.
    Returns the commit of the work."""
    env.card(ident, card_text())
    assert env.start(ident, monkeypatch) == 0
    ws = env.ws(ident)
    for name, text in (files or {}).items():
        write(ws / name, text)
    tree = check.worktree_tree(ws)  # what check.py hook computes before the tests
    if files:
        git(ws, "add", "-A")
        git(ws, "commit", "-q", "-m", "wip: turn")
    review_card(env, ident, title=title)
    if mark:
        write_mark(env, ident, tree)
    return git(ws, "rev-parse", "HEAD")


def run_hook(env: Env, ident: str, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.chdir(env.ws(ident))
    return integrate.hook_main()


def events(env: Env) -> list[dict]:
    path = env.board / ".ours-state" / "check.log"
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text().splitlines()]
    return [r for r in records if r["mode"] == "integrate"]


def card_state(env: Env, ident: str) -> str:
    return check.read_card_state(env.board / "kanban" / f"{ident}.md")


def card_body(env: Env, ident: str) -> str:
    return (env.board / "kanban" / f"{ident}.md").read_text()


BRANCH = "integration/OMS-7"


def test_the_hook_does_nothing_when_integration_is_off(henv: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    subtask(henv, "OMS-12", monkeypatch, {"a.txt": "a\n"})
    henv.set_integration(enabled=False)
    point_to_symphony(henv)
    tip = git(henv.code, "rev-parse", BRANCH)
    assert run_hook(henv, "OMS-12", monkeypatch) == 0
    assert events(henv) == []
    assert git(henv.code, "rev-parse", BRANCH) == tip
    assert card_state(henv, "OMS-12") == "Human Review"
    assert "## Integration" not in card_body(henv, "OMS-12")


@pytest.mark.parametrize("case", ["no-label", "no-request", "null-task"])
def test_the_hook_skips_what_is_not_a_finished_subtask(
    henv: Env, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    subtask(henv, "OMS-12", monkeypatch, {"a.txt": "a\n"})
    tip = git(henv.code, "rev-parse", BRANCH)
    if case == "no-label":
        review_card(henv, "OMS-12", labels="[backend]")
    elif case == "no-request":
        review_card(henv, "OMS-12", request=None)
    else:
        record = pc.base_record_path(henv.board, "OMS-12")
        record.write_text(json.dumps({**henv.record("OMS-12"), "task": None}), encoding="utf-8")
    state_before = card_state(henv, "OMS-12")
    assert run_hook(henv, "OMS-12", monkeypatch) == 0
    assert [e["outcome"] for e in events(henv)] == ["skipped"]
    assert git(henv.code, "rev-parse", BRANCH) == tip
    assert card_state(henv, "OMS-12") == state_before
    assert "## Integration" not in card_body(henv, "OMS-12")


def test_the_hook_needs_this_turns_pass_mark(henv: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    tip = git(henv.code, "rev-parse", "main")
    subtask(henv, "OMS-12", monkeypatch, {"a.txt": "a\n"}, mark=False)
    assert run_hook(henv, "OMS-12", monkeypatch) == 0
    assert [e["outcome"] for e in events(henv)] == ["skipped"]
    assert "проверка этого хода не пройдена" in events(henv)[0]["reason"]
    assert git(henv.code, "rev-parse", BRANCH) == tip and card_state(henv, "OMS-12") == "Human Review"


def test_a_foreign_pass_mark_is_refused_and_removed(henv: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    tip = git(henv.code, "rev-parse", "main")
    subtask(henv, "OMS-12", monkeypatch, {"a.txt": "a\n"})
    mark = json.loads(mark_path(henv, "OMS-12").read_text())
    write_mark(henv, "OMS-12", mark["tree"], ppid=os.getppid() + 1)
    assert run_hook(henv, "OMS-12", monkeypatch) == 0
    assert "проверка этого хода не пройдена" in events(henv)[0]["reason"]
    assert not mark_path(henv, "OMS-12").exists()
    assert git(henv.code, "rev-parse", BRANCH) == tip and card_state(henv, "OMS-12") == "Human Review"


def test_the_mark_is_used_up_by_one_hook(henv: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    subtask(henv, "OMS-12", monkeypatch, {"a.txt": "a\n"})
    assert run_hook(henv, "OMS-12", monkeypatch) == 0
    assert not mark_path(henv, "OMS-12").exists()
    review_card(henv, "OMS-12")  # back to Human Review, no new check
    assert run_hook(henv, "OMS-12", monkeypatch) == 0
    assert [e["outcome"] for e in events(henv)] == ["done", "skipped"]


def test_an_amended_commit_keeps_the_tree_and_merges(henv: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    subtask(henv, "OMS-12", monkeypatch, {"a.txt": "a\n"})
    ws = henv.ws("OMS-12")
    first = git(ws, "rev-parse", "HEAD")
    git(ws, "commit", "-q", "--amend", "-m", "wip: later turn")  # what the standard block does
    assert git(ws, "rev-parse", "HEAD") != first
    assert run_hook(henv, "OMS-12", monkeypatch) == 0
    assert [e["outcome"] for e in events(henv)] == ["done"]
    assert git(henv.code, "show", f"{BRANCH}:a.txt") == "a"


def test_a_change_after_the_check_is_not_merged(henv: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    tip = git(henv.code, "rev-parse", "main")
    subtask(henv, "OMS-12", monkeypatch, {"a.txt": "a\n"})
    ws = henv.ws("OMS-12")
    write(ws / "a.txt", "changed after the check\n")
    git(ws, "commit", "-qam", "wip: sneaky")
    assert run_hook(henv, "OMS-12", monkeypatch) == 0
    assert [e["outcome"] for e in events(henv)] == ["skipped"]
    assert "содержимое не совпадает с проверенным" in events(henv)[0]["reason"]
    assert not mark_path(henv, "OMS-12").exists()
    assert git(henv.code, "rev-parse", BRANCH) == tip and card_state(henv, "OMS-12") == "Human Review"


def test_a_dirty_working_copy_is_not_merged(henv: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    tip = git(henv.code, "rev-parse", "main")
    subtask(henv, "OMS-12", monkeypatch, {"a.txt": "a\n"})
    ws = henv.ws("OMS-12")
    # An uncommitted change whose content is the one that was checked: the tree of HEAD
    # still matches the mark, so only the dirty check can stop it.
    write(ws / "untracked-leftover.txt", "x\n")
    assert run_hook(henv, "OMS-12", monkeypatch) == 0
    assert [e["outcome"] for e in events(henv)] == ["skipped"]
    assert "untracked-leftover.txt" in events(henv)[0]["reason"]
    assert git(henv.code, "rev-parse", BRANCH) == tip and card_state(henv, "OMS-12") == "Human Review"


def test_a_subtask_is_merged_as_one_flat_commit_and_done(henv: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    sub = subtask(henv, "OMS-12", monkeypatch, {"a.txt": "a\n"}, title="Add the feature: part 1")
    old = git(henv.code, "rev-parse", BRANCH)
    ws_branch = git(henv.code, "rev-parse", "symphony/OMS-12")
    assert run_hook(henv, "OMS-12", monkeypatch) == 0
    new = git(henv.code, "rev-parse", BRANCH)
    assert new != old
    assert git(henv.code, "rev-list", f"{old}..{new}").split() == [new]
    assert git(henv.code, "rev-parse", f"{new}^@").split() == [old]
    assert git(henv.code, "rev-parse", f"{new}^{{tree}}") == git(henv.code, "rev-parse", f"{sub}^{{tree}}")
    assert git(henv.code, "log", "-1", "--format=%s", new) == "OMS-12: Add the feature: part 1"
    body = git(henv.code, "log", "-1", "--format=%b", new)
    assert body.splitlines() == ["Task: OMS-7", f"Source: symphony/OMS-12@{sub}"]
    assert git(henv.code, "log", "-1", "--format=%an <%ae>|%cn <%ce>", new) == (
        "symphony <symphony@local>|symphony <symphony@local>"
    )
    assert git(henv.code, "rev-parse", "symphony/OMS-12") == ws_branch  # the card's branch is untouched
    assert card_state(henv, "OMS-12") == "Done"
    text = card_body(henv, "OMS-12")
    assert "## Integration" in text
    for line in (f"- task: {TASK}", f"- branch: {BRANCH}", f"- source: symphony/OMS-12@{sub}",
                 f"- commit: {new}", "- check: passed (check.py hook)", "- moved to: Done"):
        assert line in text
    (event,) = events(henv)
    assert (event["outcome"], event["task"], event["branch"], event["commit"], event["old_tip"]) == (
        "done", TASK, BRANCH, new, old)


def test_the_second_subtask_starts_from_the_new_tip_and_merges_on_top(
    henv: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    subtask(henv, "OMS-12", monkeypatch, {"a.txt": "a\n"})
    assert run_hook(henv, "OMS-12", monkeypatch) == 0
    first = git(henv.code, "rev-parse", BRANCH)
    sub = subtask(henv, "OMS-13", monkeypatch, {"b.txt": "b\n"}, title="Second")
    assert git(henv.ws("OMS-13"), "rev-parse", "HEAD~1") == first  # started from the new tip
    assert (henv.ws("OMS-13") / "a.txt").is_file()
    assert run_hook(henv, "OMS-13", monkeypatch) == 0
    second = git(henv.code, "rev-parse", BRANCH)
    assert git(henv.code, "rev-parse", f"{second}^@").split() == [first]
    assert git(henv.code, "show", f"{BRANCH}:a.txt") == "a" and git(henv.code, "show", f"{BRANCH}:b.txt") == "b"
    assert git(henv.code, "log", "-1", "--format=%b", second).splitlines()[1] == f"Source: symphony/OMS-13@{sub}"
    assert [e["outcome"] for e in events(henv)] == ["done", "done"]
    assert card_state(henv, "OMS-13") == "Done"


def test_a_subtask_without_changes_gets_done_and_the_branch_stays(
    henv: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    subtask(henv, "OMS-12", monkeypatch)  # no files: HEAD is the integration tip
    tip = git(henv.code, "rev-parse", BRANCH)
    assert run_hook(henv, "OMS-12", monkeypatch) == 0
    assert git(henv.code, "rev-parse", BRANCH) == tip
    assert card_state(henv, "OMS-12") == "Done"
    assert "- commit: no changes" in card_body(henv, "OMS-12")
    (event,) = events(henv)
    assert (event["outcome"], event["commit"], event["old_tip"]) == ("done", "no changes", tip)


def test_a_conflict_leaves_the_branch_and_calls_a_human(henv: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    write(henv.code / "shared.txt", "one\ntwo\nthree\n")
    git(henv.code, "add", "-A")
    git(henv.code, "commit", "-q", "-m", "shared")
    henv.card("OMS-12", card_text())
    sub = subtask(henv, "OMS-12", monkeypatch, {"shared.txt": "one\nTWO FROM THE SUBTASK\nthree\n"})
    tip = put_file(henv.code, BRANCH, "shared.txt", "one\nTWO FROM THE BRANCH\nthree\n")
    assert run_hook(henv, "OMS-12", monkeypatch) == 0
    assert git(henv.code, "rev-parse", BRANCH) == tip
    assert card_state(henv, "OMS-12") == "needs_human"
    text = card_body(henv, "OMS-12")
    assert "## Integration" in text and "- result: conflict" in text
    assert "shared.txt" in text and "Интеграционная ветка не изменена; разбор — человек." in text
    assert f"- source: symphony/OMS-12@{sub}" in text
    (event,) = events(henv)
    assert event["outcome"] == "conflict" and event["files"] == "shared.txt"


def racing(henv: Env, monkeypatch: pytest.MonkeyPatch, moves: int) -> list[str]:
    """The branch is moved by someone else right before the first `moves` update-refs."""
    real = integrate._git_run
    done: list[str] = []

    def wrapper(repo: Path, *args: str, **kwargs: object):
        if args[0] == "update-ref" and len(done) < moves:
            done.append(put_file(henv.code, BRANCH, f"race{len(done)}.txt", "r\n"))
        return real(repo, *args, **kwargs)

    monkeypatch.setattr(integrate, "_git_run", wrapper)
    return done


def test_a_moved_branch_gets_one_retry(henv: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    subtask(henv, "OMS-12", monkeypatch, {"a.txt": "a\n"})
    moved = racing(henv, monkeypatch, 1)
    assert run_hook(henv, "OMS-12", monkeypatch) == 0
    new = git(henv.code, "rev-parse", BRANCH)
    assert git(henv.code, "rev-parse", f"{new}^@").split() == moved
    assert git(henv.code, "show", f"{BRANCH}:a.txt") == "a" and git(henv.code, "show", f"{BRANCH}:race0.txt") == "r"
    assert card_state(henv, "OMS-12") == "Done"
    assert [e["outcome"] for e in events(henv)] == ["done"]


def test_a_branch_moved_twice_counts_as_a_conflict(henv: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    subtask(henv, "OMS-12", monkeypatch, {"a.txt": "a\n"})
    moved = racing(henv, monkeypatch, 2)
    assert run_hook(henv, "OMS-12", monkeypatch) == 0
    assert git(henv.code, "rev-parse", BRANCH) == moved[-1]
    assert card_state(henv, "OMS-12") == "needs_human"
    (event,) = events(henv)
    assert event["outcome"] == "conflict" and "сдвинулась" in event["reason"]


def test_a_card_that_does_not_move_is_reported(henv: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    point_to_symphony(henv, NOOP_SYMPHONY)
    subtask(henv, "OMS-12", monkeypatch, {"a.txt": "a\n"})
    old = git(henv.code, "rev-parse", BRANCH)
    assert run_hook(henv, "OMS-12", monkeypatch) == 0
    new = git(henv.code, "rev-parse", BRANCH)
    assert new != old  # the code is merged: honest, the status is for a human to fix
    assert card_state(henv, "OMS-12") == "Human Review"
    assert "- moved to: Done FAILED (see check.log)" in card_body(henv, "OMS-12")
    assert [e["outcome"] for e in events(henv)] == ["done", "error"]
    assert "did not take effect" in events(henv)[1]["reason"]


def test_a_failing_git_moves_nothing(henv: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    subtask(henv, "OMS-12", monkeypatch, {"a.txt": "a\n"})
    git(henv.code, "branch", "-D", BRANCH)
    assert run_hook(henv, "OMS-12", monkeypatch) == 0
    assert [e["outcome"] for e in events(henv)] == ["error"]
    assert card_state(henv, "OMS-12") == "Human Review"
    assert "integrate.py hook:" in capsys.readouterr().err


def test_cli_hook_never_fails(env: Env) -> None:
    result = run_cli("hook", board=env.board)  # cwd is not a workspace of the board
    assert result.returncode == 0
    result = run_cli("hook")  # no $SYMPHONY_WORKFLOW_DIR at all
    assert result.returncode == 0 and "SYMPHONY_WORKFLOW_DIR" in result.stderr


# ---------------------------------------------------------------------------
# the after_run of the template (S4-12)
# ---------------------------------------------------------------------------


def rendered_after_run(tmp_path: Path) -> str:
    from symphony.workflow import build_service_config, load_workflow

    board = tmp_path / "board"
    board.mkdir()
    code = make_repo(tmp_path / "code")  # the core wants workspace.code_repo to be a repository
    text = pc.render_workflow(board, pc.validate_project(project_data(repo=str(code), enabled=True)))
    write(board / "WORKFLOW.md", text)
    return build_service_config(load_workflow(board / "WORKFLOW.md")).hooks.after_run


def run_after_run(tmp_path: Path, after_run: str, *, change: bool) -> tuple[list[str], Path]:
    ws = make_repo(tmp_path / "ws")
    calls = tmp_path / "calls.txt"
    fake = write(tmp_path / "fake-python", f'#!/bin/sh\necho "$@" >> {calls}\n')
    fake.chmod(0o755)
    if change:
        write(ws / "new.txt", "new\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("SYMPHONY_")}
    env["SYMPHONY_PYTHON"] = str(fake)
    proc = subprocess.run(["/bin/bash", "-c", after_run], cwd=ws, env=env, capture_output=True,
                          text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return calls.read_text().splitlines(), ws


def test_after_run_calls_both_hooks_when_nothing_changed(tmp_path: Path) -> None:
    after_run = rendered_after_run(tmp_path)
    calls, ws = run_after_run(tmp_path, after_run, change=False)
    assert [c.rsplit("/", 1)[-1] for c in calls] == ["check.py hook", "integrate.py hook"]
    assert git(ws, "log", "--format=%s").splitlines() == ["base"]  # "no changes": no commit


def test_after_run_commits_and_then_calls_integrate(tmp_path: Path) -> None:
    after_run = rendered_after_run(tmp_path)
    calls, ws = run_after_run(tmp_path, after_run, change=True)
    assert [c.rsplit("/", 1)[-1] for c in calls] == ["check.py hook", "integrate.py hook"]
    assert "wip: turn " in git(ws, "log", "-1", "--format=%s")  # maybe after a [no-test] marker
    assert git(ws, "status", "--porcelain") == ""


# ---------------------------------------------------------------------------
# the merged record and two small things (S4-13)
# ---------------------------------------------------------------------------


def merged_record_path(env: Env, ident: str) -> Path:
    return env.board / ".ours-state" / f"{ident}.merged.json"


def test_done_leaves_the_merged_record_with_the_tree(henv: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    sub = subtask(henv, "OMS-12", monkeypatch, {"a.txt": "a\n"})
    assert run_hook(henv, "OMS-12", monkeypatch) == 0
    new = git(henv.code, "rev-parse", BRANCH)
    record = json.loads(merged_record_path(henv, "OMS-12").read_text(encoding="utf-8"))
    assert set(record) == {"tree", "commit", "task", "branch", "source", "at"}
    assert record["tree"] == git(henv.ws("OMS-12"), "rev-parse", "HEAD^{tree}")
    assert record["commit"] == new
    assert (record["task"], record["branch"], record["source"]) == (TASK, BRANCH, f"symphony/OMS-12@{sub}")
    (event,) = events(henv)
    assert event["outcome"] == "done" and event["merged_record"] == "written"


def test_a_merge_without_changes_leaves_the_record_too(henv: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    subtask(henv, "OMS-12", monkeypatch)  # no files
    assert run_hook(henv, "OMS-12", monkeypatch) == 0
    record = json.loads(merged_record_path(henv, "OMS-12").read_text(encoding="utf-8"))
    assert record["commit"] is None
    assert record["tree"] == git(henv.ws("OMS-12"), "rev-parse", "HEAD^{tree}")


def test_no_record_when_the_card_does_not_become_done(henv: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    point_to_symphony(henv, NOOP_SYMPHONY)
    subtask(henv, "OMS-12", monkeypatch, {"a.txt": "a\n"})
    assert run_hook(henv, "OMS-12", monkeypatch) == 0
    assert card_state(henv, "OMS-12") == "Human Review"
    assert not merged_record_path(henv, "OMS-12").exists()
    assert events(henv)[0]["merged_record"] == "not written"


def test_no_record_after_a_skip(henv: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    subtask(henv, "OMS-12", monkeypatch, {"a.txt": "a\n"}, mark=False)
    assert run_hook(henv, "OMS-12", monkeypatch) == 0
    assert [e["outcome"] for e in events(henv)] == ["skipped"]
    assert not merged_record_path(henv, "OMS-12").exists()


def test_an_untouched_file_in_symphony_is_not_dirt(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    write(repo / ".symphony" / "state.json", "{}\n")
    write(repo / "kanban" / "card.md", "card\n")
    # Both are outside the pathspec of the standard block: neither counts.
    assert integrate._dirty_files(repo) == []
    write(repo / "root.txt", "x\n")
    assert integrate._dirty_files(repo) == ["root.txt"]


def test_a_card_outside_human_review_is_passed_over_silently(
    henv: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    subtask(henv, "OMS-12", monkeypatch, {"a.txt": "a\n"})
    tip = git(henv.code, "rev-parse", BRANCH)
    review_card(henv, "OMS-12", state="In Progress")
    assert run_hook(henv, "OMS-12", monkeypatch) == 0
    assert events(henv) == []
    assert git(henv.code, "rev-parse", BRANCH) == tip
    assert not merged_record_path(henv, "OMS-12").exists()
