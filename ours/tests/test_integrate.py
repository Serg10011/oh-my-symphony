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
