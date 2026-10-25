"""Tests for ours/bin/post-merge (TASK-S4-01).

Real temporary git repositories: a bare "origin", the main folder, a project
clone and a projects root with project.toml. No real service: launchctl, the
API query and project-doctor / project-render are fields of `post_merge.Ops`.

Run: python -m pytest ours/tests -q
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the kit is POSIX-only")

OURS = Path(__file__).resolve().parents[1]
POST_MERGE = OURS / "bin" / "post-merge"
sys.path.insert(0, str(OURS / "hooks"))

import post_merge as pm  # noqa: E402

PORT = 9971
BRANCH = "our-main"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def identify(repo: Path) -> None:
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "t")


class FakeOps:
    """Records every external call; `api` is the service answer (None = down)."""

    def __init__(self, api: dict | None = None, doctor_rc: int = 0) -> None:
        self.api = api
        self.doctor_rc = doctor_rc
        self.calls: list[str] = []

    def ops(self) -> pm.Ops:
        return pm.Ops(
            api_state=self._api, launchctl=self._launchctl, run_script=self._script,
            sleep=lambda s: None, uid=501, wait_s=0,
        )

    def _api(self, port: int):
        return self.api

    def _launchctl(self, args: list[str]):
        self.calls.append("launchctl " + args[0])
        return 0, ""

    def _script(self, name: str, args: list[str]) -> int:
        self.calls.append(name)
        return self.doctor_rc if name == "project-doctor" else 0


def idle(**counts: int) -> dict:
    return {"counts": {"running": 0, "retrying": 0, **counts}}


class Env:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.origin = tmp / "origin.git"
        self.main = tmp / "fork"
        self.clone = tmp / "pipeline"
        self.other = tmp / "other-repo"
        self.root = tmp / "projects"
        self.state = tmp / "state" / "post-merge-state.json"
        subprocess.run(["git", "init", "-q", "--bare", "-b", BRANCH, str(self.origin)], check=True)
        self.work = tmp / "work"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.work)], check=True)
        identify(self.work)
        git(self.work, "checkout", "-q", "-b", BRANCH)
        write(self.work / "README.md", "r\n")
        write(self.work / "src" / "a.py", "x = 1\n")
        write(self.work / "tests" / "test_a.py", "def test_a(): pass\n")
        write(self.work / "docs" / "d.md", "d\n")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", "base")
        git(self.work, "push", "-q", "origin", BRANCH)
        for path in (self.main, self.clone):
            subprocess.run(["git", "clone", "-q", "-b", BRANCH, str(self.origin), str(path)], check=True)
            identify(path)
        # Another repository with its own project.
        self.other.mkdir()
        git(self.other, "init", "-q", "-b", BRANCH)
        git(self.other, "remote", "add", "origin", "https://example.invalid/someone/else.git")
        self.project("oms", self.clone, PORT)
        self.project("elsewhere", self.other, PORT + 1)

    def project(self, name: str, repo: Path, port: int) -> Path:
        board = self.root / name
        write(board / "project.toml", f'[project]\nname = "{name}"\nrepo = "{repo}"\nport = {port}\n')
        return board

    def push(self, files: dict[str, str]) -> None:
        git(self.work, "pull", "-q", "--ff-only", "origin", BRANCH)
        for rel, text in files.items():
            write(self.work / rel, text)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-qm", "change " + ",".join(files))
        git(self.work, "push", "-q", "origin", BRANCH)

    def run(self, fake: FakeOps, *extra: str, dry: bool = False) -> int:
        argv = ["--root", str(self.root), "--main", str(self.main), "--state", str(self.state), *extra]
        if dry:
            argv.append("--dry-run")
        return pm.run(argv, fake.ops())

    def applied(self) -> dict[str, str]:
        return json.loads(self.state.read_text())["applied"]

    def head(self, repo: Path) -> str:
        return git(repo, "rev-parse", "HEAD")



@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def first_run(env: Env) -> None:
    """Nothing new yet: records the starting point."""
    assert env.run(FakeOps()) == 0


# --- decisions -------------------------------------------------------------


def test_docs_only_runs_nothing_and_records_sha(env: Env, capsys) -> None:
    env.push({"docs/new.md": "x\n", "README.md": "y\n"})
    fake = FakeOps(api=idle())
    assert env.run(fake) == 0
    assert fake.calls == []
    assert env.applied() == {str(env.main): env.head(env.main), str(env.clone): env.head(env.clone)}
    assert env.head(env.main) == git(env.work, "rev-parse", "HEAD")
    assert "ничего" in capsys.readouterr().out


def test_src_change_restarts_once(env: Env) -> None:
    env.push({"src/a.py": "x = 2\n"})
    fake = FakeOps(api=idle())
    assert env.run(fake) == 0
    assert fake.calls == ["launchctl kickstart"]


def test_src_and_test_change_is_stop_doctor_start(env: Env) -> None:
    write(env.root / "oms" / "launchd.plist", "<plist/>")
    env.push({"src/a.py": "x = 2\n", "tests/test_b.py": "def test_b(): pass\n"})
    fake = FakeOps(api=idle())
    assert env.run(fake) == 0
    assert fake.calls == ["launchctl bootout", "project-doctor", "launchctl bootstrap"]


def test_template_change_renders_without_restart(env: Env) -> None:
    env.push({"ours/templates/t.md": "t\n", "ours/prompts/p.md": "p\n"})
    fake = FakeOps(api=idle(running=3))
    assert env.run(fake) == 0
    assert fake.calls == ["project-render"]


def test_render_with_doctor_is_doctor_only(env: Env) -> None:
    env.push({"ours/prompts/p.md": "p\n", "pyproject.toml": "[x]\n"})
    fake = FakeOps(api=None)
    assert env.run(fake) == 0
    assert fake.calls == ["project-doctor"]


def test_render_and_restart_render_first(env: Env) -> None:
    env.push({"ours/prompts/p.md": "p\n", "src/a.py": "x = 3\n"})
    fake = FakeOps(api=idle())
    assert env.run(fake) == 0
    assert fake.calls == ["project-render", "launchctl kickstart"]


def test_rules_table() -> None:
    assert pm.matches("doctor", "pkg/test_x.py") and pm.matches("doctor", "pkg/x_test.py")
    assert pm.matches("doctor", "uv.lock") and not pm.matches("doctor", "docs/a.md")
    assert pm.matches("render", "ours/hooks/project_config.py")
    assert not pm.matches("render", "ours/hooks/check.py")
    assert not pm.matches("restart", "ours/bin/post-merge")


# --- cards in work ---------------------------------------------------------


@pytest.mark.parametrize("busy", [{"running": 1}, {"retrying": 2}])
def test_busy_cards_defer_then_repeat_runs_it(env: Env, busy: dict, capsys) -> None:
    first_run(env)
    before = env.applied()
    env.push({"src/a.py": "x = 2\n", "tests/test_b.py": "def test_b(): pass\n"})
    fake = FakeOps(api=idle(**busy))
    assert env.run(fake) == 2
    assert fake.calls == []
    out = capsys.readouterr().out
    assert "ОТЛОЖЕНО" in out and "post-merge" in out
    after = env.applied()
    assert after == before  # not moved: the same changes are seen again
    assert env.head(env.main) != before[str(env.main)]  # but the pull happened

    write(env.root / "oms" / "launchd.plist", "<plist/>")
    fake = FakeOps(api=idle())
    assert env.run(fake) == 0
    assert fake.calls == ["launchctl bootout", "project-doctor", "launchctl bootstrap"]
    assert env.applied()[str(env.main)] == env.head(env.main)


# --- service down / doctor fails ------------------------------------------


def test_api_down_skips_restart_with_hint_and_doctor_runs_unstopped(env: Env, capsys) -> None:
    env.push({"src/a.py": "x = 2\n"})
    fake = FakeOps(api=None)
    assert env.run(fake) == 0
    assert fake.calls == []
    assert "project-launchd oms" in capsys.readouterr().out
    env.push({"tests/test_c.py": "def test_c(): pass\n"})
    assert env.run(fake) == 0
    assert fake.calls == ["project-doctor"]


def test_doctor_failure_restarts_service_and_keeps_sha(env: Env) -> None:
    first_run(env)
    before = env.applied()
    write(env.root / "oms" / "launchd.plist", "<plist/>")
    env.push({"tests/test_b.py": "def test_b(): pass\n"})
    fake = FakeOps(api=idle(), doctor_rc=1)
    assert env.run(fake) == 1
    assert fake.calls == ["launchctl bootout", "project-doctor", "launchctl bootstrap"]
    assert env.applied()[str(env.clone)] == before[str(env.clone)]


def test_no_launchctl_gives_message_not_crash(env: Env, capsys) -> None:
    env.push({"src/a.py": "x = 2\n"})
    ops = pm.Ops(api_state=lambda port: idle(), launchctl=lambda args: None,
                 run_script=lambda n, a: 0, sleep=lambda s: None, wait_s=0)
    argv = ["--root", str(env.root), "--main", str(env.main), "--state", str(env.state)]
    assert pm.run(argv, ops) == 1
    assert "нет launchctl" in capsys.readouterr().out


# --- dry run, repeat, refusals --------------------------------------------


def snapshot(env: Env) -> tuple:
    return (env.head(env.main), env.head(env.clone), env.state.exists(),
            git(env.main, "worktree", "list"), git(env.main, "status", "--porcelain"))


def test_dry_run_changes_nothing_and_prints_plan(env: Env, capsys) -> None:
    env.push({"src/a.py": "x = 2\n"})
    state = snapshot(env)
    fake = FakeOps(api=idle(running=1))
    assert env.run(fake, dry=True) == 0
    assert snapshot(env) == state
    assert fake.calls == []
    out = capsys.readouterr().out
    assert "ПЛАН" in out and "перезапуск службы" in out and "отложено" in out


def test_second_run_without_commits_does_nothing(env: Env) -> None:
    env.push({"src/a.py": "x = 2\n"})
    assert env.run(FakeOps(api=idle())) == 0
    fake = FakeOps(api=idle())
    assert env.run(fake) == 0
    assert fake.calls == []


@pytest.mark.parametrize("target", ["main", "clone"])
def test_wrong_branch_or_dirty_tree_refuses_without_pull(env: Env, target: str, capsys) -> None:
    env.push({"src/a.py": "x = 2\n"})
    repo = env.main if target == "main" else env.clone
    before = env.head(repo)
    git(repo, "checkout", "-q", "-b", "other")
    fake = FakeOps(api=idle())
    assert env.run(fake) == 1
    assert env.head(repo) == before and fake.calls == []
    assert "ОТКАЗ" in capsys.readouterr().out
    git(repo, "checkout", "-q", BRANCH)
    write(repo / "README.md", "dirty\n")
    assert env.run(fake) == 1
    assert env.head(repo) == before and fake.calls == []


def test_other_repo_untouched_and_project_filter(env: Env, capsys) -> None:
    env.push({"src/a.py": "x = 2\n"})
    fake = FakeOps(api=idle())
    assert env.run(fake) == 0
    out = capsys.readouterr().out
    assert "elsewhere" not in out and "oms" in out
    env.project("second", env.clone, PORT + 2)
    env.push({"src/a.py": "x = 3\n"})
    fake = FakeOps(api=idle())
    assert env.run(fake, "--project", "second") == 0
    assert fake.calls == ["launchctl kickstart"]  # only "second", not "oms"
    assert env.run(FakeOps(), "--project", "nope") == 1


def test_project_with_repo_equal_to_main_counts(env: Env) -> None:
    env.project("selfie", env.main, PORT + 3)
    found, _ = pm.find_projects(env.root, env.main, ["selfie"])
    assert [b.name for b, _, _ in found] == ["selfie"]


def test_lock_refuses_second_run(env: Env, capsys) -> None:
    import fcntl

    env.state.parent.mkdir(parents=True)
    with open(str(env.state) + ".lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert env.run(FakeOps()) == 1
    assert "уже запущен" in capsys.readouterr().out


def test_url_normalisation() -> None:
    n = pm.normalize_url
    assert n("git@github.com:Serg/Repo.git") == n("https://token@github.com/serg/repo/") == "github.com/serg/repo"


# --- cleanup ---------------------------------------------------------------


def make_dev(env: Env, code: str, *, merged: bool, untracked: bool = False) -> Path:
    path = env.tmp / "dev" / code
    git(env.main, "worktree", "add", "-q", "-b", f"dev/{code}", str(path), BRANCH)
    identify(path)
    write(path / f"{code}.txt", code)
    git(path, "add", "-A")
    git(path, "commit", "-qm", code)
    if merged:
        git(env.main, "merge", "-q", "--no-ff", "-m", f"merge {code}", f"dev/{code}")
        git(env.main, "push", "-q", "origin", BRANCH)
    if untracked:
        write(path / "scratch.txt", "x")
    write(env.main / "tmp_workspaces" / "inbox" / f"{code}.md", "task")
    return path


def test_cleanup_removes_only_merged_clean_dev_worktrees(env: Env) -> None:
    write(env.main / ".gitignore", "tmp_workspaces/\n")
    git(env.main, "add", ".gitignore")
    git(env.main, "commit", "-qm", "ignore")
    git(env.main, "push", "-q", "origin", BRANCH)
    clean = make_dev(env, "A1", merged=True)
    dirty = make_dev(env, "B1", merged=True, untracked=True)
    open_ = make_dev(env, "C1", merged=False)
    fake = FakeOps(api=idle())
    assert env.run(fake) == 0
    assert not clean.exists() and not (env.main / "tmp_workspaces/inbox/A1.md").exists()
    assert "dev/A1" not in git(env.main, "branch", "--format=%(refname:short)").split()
    assert dirty.exists() and open_.exists()
    assert (env.main / "tmp_workspaces/inbox/B1.md").exists()
    assert (env.main / "tmp_workspaces/inbox/C1.md").exists()


def test_cleanup_keeps_fresh_branch_without_own_commits(env: Env) -> None:
    path = env.tmp / "dev" / "N1"
    git(env.main, "worktree", "add", "-q", "-b", "dev/N1", str(path), BRANCH)
    assert env.run(FakeOps(api=idle())) == 0
    assert path.exists()


# --- the whole command as a subprocess -------------------------------------


def test_command_dry_run_with_fake_api(env: Env) -> None:
    env.push({"src/a.py": "x = 2\n"})

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = json.dumps({"counts": {"running": 0, "retrying": 0}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        env.project("oms", env.clone, server.server_address[1])
        before = snapshot(env)
        done = subprocess.run(
            [str(POST_MERGE), "--dry-run", "--root", str(env.root), "--main", str(env.main),
             "--state", str(env.state)],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        server.shutdown()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "ПЛАН" in done.stdout and "перезапуск службы" in done.stdout
    assert snapshot(env) == before
