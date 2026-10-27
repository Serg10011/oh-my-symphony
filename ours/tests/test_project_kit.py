"""Tests for the project kit (TASK-K08a): ours/bin/project-new,
ours/bin/project-render and ours/hooks/worktree.py.

Real temporary git repositories, the real `symphony board init` and the real
`symphony._worktree_cleanup`; every script runs as a subprocess. `claude` is
a fake executable on PATH.

Run: python -m pytest ours/tests -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the kit is POSIX-only")

OURS = Path(__file__).resolve().parents[1]
PROJECT_NEW = OURS / "bin" / "project-new"
PROJECT_RENDER = OURS / "bin" / "project-render"
WORKTREE = OURS / "hooks" / "worktree.py"
sys.path.insert(0, str(OURS / "hooks"))

import project_config as pc  # noqa: E402

PREFIX = "KIT"
ID = f"{PREFIX}-1"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class Kit:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.code = tmp / "code"
        self.root = tmp / "projects"
        self.board = self.root / "demo"
        self.home = tmp / "home"
        self.bin = tmp / "bin"
        self.ws_root = tmp / "workspaces"
        self.ws = self.ws_root / ID
        self.setup_marker = tmp / "setup-ran"
        self.home.mkdir()
        self.ws_root.mkdir()
        write(self.bin / "claude", "#!/bin/sh\nexit 0\n").chmod(0o755)
        self.code.mkdir()
        git(self.code, "init", "-q", "-b", "main")
        git(self.code, "config", "user.email", "t@example.invalid")
        git(self.code, "config", "user.name", "t")
        write(self.code / "README.md", "readme\n")
        write(self.code / "tests" / "test_a.py", "def test_a():\n    pass\n")
        git(self.code, "add", "-A")
        git(self.code, "commit", "-q", "-m", "base")
        git(self.code, "branch", "dev")
        write(self.code / "README.md", "main only\n")
        git(self.code, "commit", "-qam", "main moves on")

    def env(self, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("SYMPHONY_")}
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["HOME"] = str(self.home)
        env.update(extra)
        return env

    def new(self, name: str = "demo", *, repo: Path | None = None, base: str = "dev",
            prefix: str = PREFIX, port: int = 9971, root: Path | None = None,
            python: str = sys.executable, **env: str):
        return subprocess.run(
            [python, str(PROJECT_NEW), name, "--repo", str(repo or self.code), "--base", base,
             "--prefix", prefix, "--port", str(port), "--root", str(root or self.root)],
            env=self.env(**env), capture_output=True, text=True, timeout=120,
        )

    def ready(self, setup: str | None = None) -> None:
        result = self.new()
        assert result.returncode == 0, result.stderr
        write(self.board / "setup.sh", setup or f"echo \"$PWD\" > {self.setup_marker}\n")

    def hook(self, mode: str, ws: Path | None = None):
        ws = ws or self.ws
        ws.mkdir(exist_ok=True)  # the core pre-creates the empty workspace
        return subprocess.run(
            [sys.executable, str(WORKTREE), mode], cwd=ws, capture_output=True, text=True,
            timeout=120, env=self.env(SYMPHONY_WORKFLOW_DIR=str(self.board),
                                      SYMPHONY_PYTHON=sys.executable,
                                      SYMPHONY_WORKSPACE_ROOT=str(self.ws_root)),
        )

    def branches(self) -> list[str]:
        return git(self.code, "branch", "--format=%(refname:short)").split()

    def sha(self, ref: str) -> str:
        return git(self.code, "rev-parse", ref)


@pytest.fixture
def kit(tmp_path: Path) -> Kit:
    return Kit(tmp_path)


# ---------------------------------------------------------------------------
# project-new
# ---------------------------------------------------------------------------


def test_project_new_creates_the_board_folder(kit: Kit) -> None:
    result = kit.new()
    assert result.returncode == 0, result.stderr
    b = kit.board
    project = pc.load_project(b)
    assert project["project"] == {"name": "demo", "repo": str(kit.code.resolve()),
                                  "base_branch": "dev", "id_prefix": PREFIX, "port": 9971}
    assert project["tools"]["claude"] == str(kit.bin / "claude")
    assert all(Path(v).is_absolute() for v in project["tools"].values())
    assert project["timeouts"] == pc.DEFAULT_TIMEOUTS
    for name in ("setup.sh", "test.sh"):
        run = subprocess.run(["/bin/bash", str(b / name)], capture_output=True, text=True)
        assert run.returncode == 1 and "не настроен" in run.stderr
    assert (b / "kanban").is_dir() and not list((b / "kanban").glob("*.md"))
    assert (b / "artifacts").is_dir() and (b / "wiki").is_dir()
    assert (b / ".ours-state" / ".gitignore").read_text() == "*\n"
    assert (b / "worktree-exclude").read_text() == pc.exclude_text(PREFIX)
    text = (b / "WORKFLOW.md").read_text()
    assert "НЕ ПРАВИТЬ РУКАМИ" in text and "project-render" in text


def test_rendered_workflow_is_accepted_by_the_core(kit: Kit) -> None:
    from symphony.workflow import build_service_config, load_workflow

    assert kit.new().returncode == 0
    cfg = build_service_config(load_workflow(kit.board / "WORKFLOW.md"))
    a = cfg.agent
    assert cfg.tracker.terminal_states == ("Human Review", "needs_human", "Subtasks", "Done", "Blocked", "Archive")
    assert cfg.tracker.active_states == ("Todo", "Plan", "In Progress", "Verify", "Document")
    assert a.stage_contracts_enabled(cfg.tracker.active_states)
    assert (a.kind, a.max_concurrent_agents, a.budget_exhausted_state) == ("claude", 1, "needs_human")
    assert not (a.auto_recover_blocked or a.normalize_legacy_human_review
                or a.auto_merge_on_done or a.auto_merge_push_target)
    assert a.auto_commit_on_done
    assert a.feature_base_branch == a.auto_merge_target_branch == "dev"
    assert a.preserve_workspace_states == ("human review", "needs_human", "subtasks")
    assert a.done_gate_command == f'"$SYMPHONY_PYTHON" {OURS / "hooks" / "check.py"} gate'
    assert a.done_gate_timeout_ms == 960000 and cfg.hooks.timeout_ms == 1200000
    assert cfg.wiki.sweep_every_n == 0 and cfg.server.port == 9971
    # The web board reads the code repository (TASK-K10), the same path as [project].repo.
    assert cfg.code_repo == Path(pc.load_project(kit.board)["project"]["repo"]) == kit.code.resolve()
    # check.py is the first command, before the early "no changes" exit.
    commands = [line for line in cfg.hooks.after_run.splitlines() if not line.lstrip().startswith("#")]
    assert commands[0] == f'"$SYMPHONY_PYTHON" {OURS / "hooks" / "check.py"} hook || true'
    # S4-12: the standard block is a subshell now, so its early `exit 0` cannot skip integrate.py.
    assert commands[1] == "(" and commands[2] == "set -uo pipefail"
    assert commands[-1] == f'"$SYMPHONY_PYTHON" {OURS / "hooks" / "integrate.py"} hook || true'
    assert "worktree.py create" in cfg.hooks.after_create
    assert "worktree.py remove" in cfg.hooks.before_remove
    assert cfg.claude.command.startswith(str(kit.bin / "claude") + " -p ")
    for sub in ("kanban", "artifacts", "wiki"):
        assert f"--add-dir {kit.board / sub}" in cfg.claude.command
    assert cfg.prompts.base_path == OURS / "prompts" / "base.md"
    assert cfg.prompts.stage_paths["document"] == OURS / "prompts" / "stages" / "document.md"


def _taken(kit: Kit) -> None:
    assert kit.new("other", port=9971, prefix="OTH").returncode == 0


@pytest.mark.parametrize(
    ("prepare", "kwargs", "reason"),
    [
        (lambda kit: (kit.root / "demo").mkdir(parents=True), {}, "уже есть"),
        (lambda kit: (kit.tmp / "plain").mkdir(), {"repo": "plain"}, "не корень git-репозитория"),
        (None, {"base": "nope"}, "нет ветки refs/heads/nope"),
        (None, {"root": "code/projects"}, "внутри git-репозитория"),
        (None, {"name": "Demo"}, "имя"),
        (None, {"name": "de_mo"}, "имя"),
        (None, {"prefix": "K"}, "префикс"),
        (None, {"prefix": "kit"}, "префикс"),
        (_taken, {}, "порт 9971 уже занят проектом other"),
        (lambda kit: kit.new("other", port=9972), {}, "префикс KIT уже занят проектом other"),
        (lambda kit: (kit.bin / "claude").unlink(), {"PATH": "/usr/bin:/bin"}, "claude не найдена"),
    ],
)
def test_project_new_refusals(kit: Kit, prepare, kwargs: dict, reason: str) -> None:
    if prepare:
        prepare(kit)
    for key in ("repo", "root"):
        if key in kwargs:
            kwargs[key] = kit.tmp / kwargs[key]
    existed = kit.board.exists()
    result = kit.new(**kwargs)
    assert result.returncode == 1
    assert reason in result.stderr, result.stderr
    if not existed:
        assert not kit.board.exists()


def test_project_new_refuses_a_python_without_symphony(kit: Kit) -> None:
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(kit.tmp / "venv")], check=True)
    result = kit.new(python=str(kit.tmp / "venv" / "bin" / "python"))
    assert result.returncode == 1
    assert "не видит пакет symphony" in result.stderr
    assert not kit.board.exists()


# ---------------------------------------------------------------------------
# project-render
# ---------------------------------------------------------------------------


def render(kit: Kit, name: str = "demo"):
    return subprocess.run([sys.executable, str(PROJECT_RENDER), name, "--root", str(kit.root)],
                          env=kit.env(), capture_output=True, text=True, timeout=60)


def test_project_render_prints_the_diff_and_rewrites(kit: Kit) -> None:
    assert kit.new().returncode == 0
    same = render(kit)
    assert same.returncode == 0 and "без изменений" in same.stdout
    toml = kit.board / "project.toml"
    toml.write_text(toml.read_text().replace("port = 9971", "port = 9975"))
    changed = render(kit)
    assert changed.returncode == 0
    assert "-  port: 9971" in changed.stdout and "+  port: 9975" in changed.stdout
    assert "port: 9975" in (kit.board / "WORKFLOW.md").read_text()
    assert "без изменений" in render(kit).stdout


def test_project_render_refuses_a_bad_project_toml(kit: Kit) -> None:
    assert kit.new().returncode == 0
    toml = kit.board / "project.toml"
    toml.write_text(toml.read_text() + "[extra]\n")
    before = (kit.board / "WORKFLOW.md").read_text()
    result = render(kit)
    assert result.returncode == 1 and "неизвестный раздел" in result.stderr
    assert (kit.board / "WORKFLOW.md").read_text() == before


# ---------------------------------------------------------------------------
# worktree.py create
# ---------------------------------------------------------------------------


def test_create_clean_start(kit: Kit) -> None:
    kit.ready()
    result = kit.hook("create")
    assert result.returncode == 0, result.stderr
    ws, b = kit.ws, kit.board
    assert git(ws, "symbolic-ref", "HEAD") == f"refs/heads/symphony/{ID}"
    assert kit.sha(f"symphony/{ID}") == kit.sha("dev")
    assert git(ws, "config", "--worktree", "symphony.basesha") == kit.sha("dev")
    assert git(ws, "config", "--worktree", "symphony.basebranch") == "dev"
    assert git(ws, "config", "--worktree", "symphony.mergetargetbranch") == "dev"
    assert git(ws, "config", "--worktree", "core.excludesFile") == str(b / "worktree-exclude")
    assert git(ws, "config", "--worktree", "--get-all", "symphony.autocommitExclude").split() == [
        "kanban", f"docs/{ID}", "docs/llm-wiki"]
    record = json.loads((b / ".ours-state" / f"{ID}.base.json").read_text())
    assert record["base_sha"] == kit.sha("dev") and record["base_branch"] == "dev"
    assert record["created_at"].endswith("Z")
    assert (ws / "kanban").resolve() == b / "kanban"
    assert (ws / "docs" / ID).resolve() == b / "artifacts" / ID
    assert (ws / "docs" / "llm-wiki").resolve() == b / "wiki"
    assert (b / "artifacts" / ID / "work").is_dir() and (b / "artifacts" / ID / "qa").is_dir()
    assert kit.setup_marker.read_text().strip() == str(ws)
    # Agent output through the links never reaches git.
    write(ws / "docs" / ID / "work" / "notes.md", "n\n")
    write(ws / "docs" / "llm-wiki" / "INDEX.md", "i\n")
    write(ws / ".venv" / "x", "v\n")
    assert git(ws, "status", "--porcelain", "--untracked-files=all") == ""
    assert git(kit.code, "status", "--porcelain", "--untracked-files=all") == ""
    assert not (kit.code / ".git" / "info" / "exclude").read_text().count("kanban")


def test_create_renames_an_existing_branch_and_artifacts(kit: Kit) -> None:
    kit.ready()
    git(kit.code, "branch", f"symphony/{ID}", "main")
    old = kit.sha(f"symphony/{ID}")
    write(kit.board / "artifacts" / ID / "work" / "old.md", "stale evidence\n")
    assert kit.hook("create").returncode == 0
    parked = [b for b in kit.branches() if b.startswith(f"attempts/{ID}-")]
    assert len(parked) == 1 and kit.sha(parked[0]) == old
    assert kit.sha(f"symphony/{ID}") == kit.sha("dev")
    assert not (kit.board / "artifacts" / ID / "work" / "old.md").exists()
    aside = list((kit.board / "artifacts" / ".attempts").glob(f"{ID}-*/work/old.md"))
    assert len(aside) == 1


def test_create_refresh_mode_keeps_the_branch_and_skips_setup(kit: Kit) -> None:
    kit.ready()
    assert kit.hook("create").returncode == 0
    write(kit.ws / "src" / "work.py", "x = 1\n")
    git(kit.ws, "add", "-A")
    git(kit.ws, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "wip")
    work = kit.sha(f"symphony/{ID}")
    kit.setup_marker.unlink()
    (kit.ws / "kanban").unlink()
    (kit.ws / "docs" / ID).unlink()
    (kit.board / ".ours-state" / f"{ID}.base.json").unlink()
    git(kit.ws, "config", "--worktree", "--unset", "core.excludesFile")
    result = kit.hook("create")
    assert result.returncode == 0, result.stderr
    assert "только восстанавливаю" in result.stderr
    assert kit.sha(f"symphony/{ID}") == work
    assert (kit.ws / "src" / "work.py").exists()
    assert not [b for b in kit.branches() if b.startswith("attempts/")]
    assert not kit.setup_marker.exists()
    assert (kit.ws / "kanban").resolve() == kit.board / "kanban"
    assert (kit.ws / "docs" / ID).resolve() == kit.board / "artifacts" / ID
    assert git(kit.ws, "config", "--worktree", "core.excludesFile") == str(kit.board / "worktree-exclude")
    record = json.loads((kit.board / ".ours-state" / f"{ID}.base.json").read_text())
    assert record["base_sha"] == kit.sha("dev")


def test_create_refuses_a_board_inside_a_git_repository(kit: Kit) -> None:
    kit.ready()
    git(kit.root, "init", "-q")
    result = kit.hook("create")
    assert result.returncode == 1 and "внутри git-репозитория" in result.stderr
    assert f"symphony/{ID}" not in kit.branches()


def test_create_refuses_a_foreign_prefix(kit: Kit) -> None:
    kit.ready()
    result = kit.hook("create", kit.ws_root / "OTHER-1")
    assert result.returncode == 1 and "не начинается с KIT-" in result.stderr
    assert "symphony/OTHER-1" not in kit.branches()
    assert not kit.setup_marker.exists()


def test_failed_setup_rolls_everything_back(kit: Kit) -> None:
    kit.ready(setup="echo boom >&2; exit 3\n")
    git(kit.code, "branch", f"symphony/{ID}", "main")
    old = kit.sha(f"symphony/{ID}")
    write(kit.board / "artifacts" / ID / "work" / "old.md", "old\n")
    old_record = write(kit.board / ".ours-state" / f"{ID}.base.json", '{"old": true}\n')
    result = kit.hook("create")
    assert result.returncode == 1
    assert "setup.sh завершился с кодом 3" in result.stderr and "откат выполнен" in result.stderr
    assert kit.sha(f"symphony/{ID}") == old
    assert not [b for b in kit.branches() if b.startswith("attempts/")]
    assert str(kit.ws) not in git(kit.code, "worktree", "list")
    assert (kit.board / "artifacts" / ID / "work" / "old.md").read_text() == "old\n"
    assert old_record.read_text() == '{"old": true}\n'
    # The next attempt starts cleanly.
    write(kit.board / "setup.sh", "exit 0\n")
    assert kit.hook("create").returncode == 0


def test_failed_setup_without_old_branch_removes_the_new_one(kit: Kit) -> None:
    kit.ready(setup="exit 1\n")
    assert kit.hook("create").returncode == 1
    assert f"symphony/{ID}" not in kit.branches()
    assert not (kit.board / ".ours-state" / f"{ID}.base.json").exists()


def test_existing_wiki_in_code_repo_is_left_alone(kit: Kit) -> None:
    write(kit.code / "docs" / "llm-wiki" / "INDEX.md", "repo wiki\n")
    git(kit.code, "add", "-A")
    git(kit.code, "commit", "-qm", "wiki")
    git(kit.code, "branch", "-f", "dev", "main")
    kit.ready()
    result = kit.hook("create")
    assert result.returncode == 0 and "docs/llm-wiki уже есть" in result.stderr
    assert not (kit.ws / "docs" / "llm-wiki").is_symlink()
    assert (kit.ws / "docs" / ID).is_symlink()


# ---------------------------------------------------------------------------
# worktree.py remove
# ---------------------------------------------------------------------------


def test_remove_drops_links_and_worktree_but_keeps_branch(kit: Kit) -> None:
    kit.ready()
    assert kit.hook("create").returncode == 0
    write(kit.board / "kanban" / f"{ID}.md", "---\nid: x\n---\n")
    write(kit.board / "artifacts" / ID / "work" / "notes.md", "keep\n")
    result = kit.hook("remove")
    assert result.returncode == 0, result.stderr
    assert not kit.ws.exists()
    assert str(kit.ws) not in git(kit.code, "worktree", "list")
    assert f"symphony/{ID}" in kit.branches()
    assert (kit.board / "kanban" / f"{ID}.md").exists()
    assert (kit.board / "artifacts" / ID / "work" / "notes.md").read_text() == "keep\n"
    assert git(kit.code, "status", "--porcelain") == ""
