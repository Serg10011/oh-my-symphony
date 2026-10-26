"""Tests for ours/hooks/check.py (TASK-K05, TASK-K08a, TASK-K08b).

Every scenario builds a real code repository, a board folder outside it
(project.toml, test.sh, kanban/, .ours-state/<ID>.base.json and
baseline.json) and a linked
worktree laid out like ours/hooks/worktree.py does (branch `symphony/<ID>`,
`kanban` symlinked to the board, hidden through core.excludesFile), a card
on the board, and a fake `symphony` executable that records its calls and
rewrites the card state. check.py always runs as a real subprocess.

Run: python -m pytest ours/tests -q
"""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="check.py is POSIX-only")

CHECK = Path(__file__).resolve().parents[1] / "hooks" / "check.py"
ID = "T-1"
sys.path.insert(0, str(CHECK.parent))

_spec = importlib.util.spec_from_file_location("ours_check", CHECK)
assert _spec is not None and _spec.loader is not None
check = importlib.util.module_from_spec(_spec)
sys.modules["ours_check"] = check
_spec.loader.exec_module(check)

GOOD_BODY = """\
## Plan

do it

## Wiki Updates

- docs updated

## As-Is -> To-Be Report

before / after
"""

FAKE_SYMPHONY = """\
#!{python}
import json, re, sys
from pathlib import Path
with open({calls!r}, "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if args[:2] == ["board", "mv"]:
    ident, state = args[2], args[3]
    root = Path(args[args.index("--root") + 1])
    card = root / f"{{ident}}.md"
    text = card.read_text()
    card.write_text(re.sub(r"(?m)^state:.*$", "state: " + state, text, count=1))
    print(f"{{ident}} -> {{state}}")
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class Env:
    """Code repo + board folder + worktree + fake symphony for one scenario."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.code = tmp / "code"
        self.board = tmp / "board"
        self.ws = tmp / "workspaces" / ID
        self.calls = tmp / "calls.jsonl"
        self.bin = tmp / "bin"
        self.marker = tmp / "tests-ran"
        self.code.mkdir()
        git(self.code, "init", "-q", "-b", "main")
        git(self.code, "config", "user.email", "t@example.invalid")
        git(self.code, "config", "user.name", "t")
        write(self.code / "README.md", "readme\n")
        write(self.code / "WORKFLOW.md", "---\n---\n")
        write(self.code / "src" / "app.py", "x = 1\n")
        write(self.code / "tests" / "test_app.py", "def test_a():\n    assert True\n")
        write(self.code / "pkg" / "deep" / "test_deep.py", "def test_b():\n    pass\n")
        write(self.code / "web" / "button.test.ts", "it('x', () => {})\n")
        git(self.code, "add", "-A")
        git(self.code, "commit", "-q", "-m", "base")
        # Board folder and worktree as project-new / worktree.py build them.
        (self.board / "kanban").mkdir(parents=True)
        exclude = write(self.board / "worktree-exclude", "/kanban\n/.venv/\n")
        self.ws.parent.mkdir(parents=True)
        git(self.code, "worktree", "add", "-q", "-b", f"symphony/{ID}", str(self.ws), "main")
        git(self.ws, "config", "extensions.worktreeConfig", "true")
        git(self.ws, "config", "--worktree", "core.excludesFile", str(exclude))
        (self.ws / "kanban").symlink_to(self.board / "kanban")
        self.record(git(self.code, "rev-parse", "main").strip())
        self.baseline()
        self.card = self.board / "kanban" / f"{ID}.md"
        self.set_card("Human Review", GOOD_BODY)
        self.bin.mkdir()
        self.symphony = write(
            self.bin / "symphony",
            FAKE_SYMPHONY.format(python=sys.executable, calls=str(self.calls)),
        )
        self.symphony.chmod(0o755)
        self.config(test_command=f"touch {self.marker}")

    # -- setup helpers ---------------------------------------------------
    def set_card(self, state: str, body: str = GOOD_BODY) -> None:
        write(self.card, f"---\nid: {ID}\nidentifier: {ID}\ntitle: t\nstate: {state}\n---\n\n{body}")

    def record(self, sha: str) -> None:
        payload = {"base_sha": sha, "base_branch": "main", "created_at": "2026-10-24T00:00:00Z"}
        write(self.board / ".ours-state" / f"{ID}.base.json", json.dumps(payload))

    def baseline(self, passed: int = 1, skipped: int = 0, failed: int = 0,
                 branch: str = "main") -> None:
        payload = {"base_branch": branch, "base_sha": "0" * 40, "passed": passed,
                   "skipped": skipped, "failed": failed, "setup_s": 0.1, "tests_s": 0.1,
                   "measured_at": "2026-10-24T00:00:00Z"}
        write(self.board / ".ours-state" / "baseline.json", json.dumps(payload))

    def config(self, raw: str | None = None, timeouts: dict | None = None,
               summary: str | None = "passed=1 skipped=0 failed=0", **keys: object) -> None:
        """project.toml; `test_command` becomes the board test.sh, followed by
        the summary line `PIPELINE_TESTS <summary>` unless summary is None."""
        if "test_command" in keys:
            tail = f"echo 'PIPELINE_TESTS {summary}'\n" if summary is not None else ""
            write(self.board / "test.sh", f"{keys.pop('test_command')}\n{tail}")
        if raw is None:
            project = {"name": "t", "repo": str(self.code), "id_prefix": "TT", "port": 9990,
                       "base_branch": keys.pop("base_ref", "main")}
            tools = {"python": sys.executable, "claude": "/bin/true",
                     "symphony": keys.pop("symphony_bin", str(self.symphony))}
            sections = {"project": project, "tools": tools, "checks": keys,
                        "timeouts": timeouts or {}}
            raw = "".join(
                f"[{name}]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in values.items())
                for name, values in sections.items()
            )
        write(self.board / "project.toml", raw)

    def commit(self, message: str = "work") -> None:
        git(self.ws, "add", "-A")
        git(self.ws, "commit", "-q", "-m", message)

    # -- running ---------------------------------------------------------
    def env(self, **extra: str | None) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("SYMPHONY_")}
        env["SYMPHONY_WORKFLOW_DIR"] = str(self.board)
        env["SYMPHONY_PYTHON"] = sys.executable
        for key, value in extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def run(self, mode: str, *args: str, cwd: Path | None = None, **extra: str | None):
        return subprocess.run(
            [sys.executable, str(CHECK), mode, *args],
            cwd=cwd or self.ws,
            env=self.env(**extra),
            capture_output=True,
            text=True,
            timeout=120,
        )

    # -- observations ----------------------------------------------------
    def state(self) -> str:
        return check.read_card_state(self.card)

    def mv_calls(self) -> list[list[str]]:
        if not self.calls.exists():
            return []
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        return [c for c in calls if c[:2] == ["board", "mv"]]

    def counter(self) -> dict:
        return json.loads((self.board / ".ours-state" / f"{ID}.json").read_text())

    def log(self) -> list[dict]:
        path = self.board / ".ours-state" / "check.log"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def assert_refused(result, *needles: str) -> None:
    assert result.returncode != 0, result.stdout + result.stderr
    for needle in needles:
        assert needle in result.stdout, result.stdout


def assert_passed(result) -> None:
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout


# ---------------------------------------------------------------------------
# globs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("tests/**", "tests", True),
        ("tests/**", "tests/test_a.py", True),
        ("tests/**", "tests/a/b/c.py", True),
        ("tests/**", "src/tests/a.py", False),
        ("tests/**", "testsx/a.py", False),
        ("**/test_*.py", "test_a.py", True),
        ("**/test_*.py", "a/b/test_a.py", True),
        ("**/test_*.py", "a/b/xtest_a.py", False),
        ("**/test_*.py", "a/test_a.pyc", False),
        ("**/*.spec.*", "web/x.spec.ts", True),
        ("**/*.spec.*", "x.spec.js", True),
        ("**/*.pem", "a/.hidden.pem", True),
        ("a/**/b", "a/b", True),
        ("a/**/b", "a/x/y/b", True),
        ("a/**/b", "a/x/y/c", False),
        ("**", "anything/at/all", True),
        ("**/**/x", "x", True),
        ("ours/**", "ours/hooks/check.py", True),
        ("ours/**", "src/ours/x", False),
        ("WORKFLOW.md", "WORKFLOW.md", True),
        ("WORKFLOW.md", "workflow.md", True),  # case-insensitive
        ("WORKFLOW.md", "docs/WORKFLOW.md", False),  # anchored at the root
        (".env.*", ".env.local", True),
        (".env.*", ".env", False),
        ("*.py", "a/b.py", False),  # `*` never crosses a slash
        ("src/?.py", "src/a.py", True),
        ("src/?.py", "src/ab.py", False),
        ("src/[ab].py", "src/b.py", True),
        ("src/[!ab].py", "src/c.py", True),
        ("src/[!ab].py", "src/a.py", False),
        ("a.b", "aXb", False),  # dots are literal
    ],
)
def test_glob_match(pattern: str, path: str, expected: bool) -> None:
    assert check.glob_match(path, pattern) is expected


# ---------------------------------------------------------------------------
# 1. clean run
# ---------------------------------------------------------------------------


def test_clean_gate_passes_and_runs_tests(env: Env) -> None:
    write(env.ws / "src" / "app.py", "x = 2\n")
    env.commit()
    result = env.run("gate")
    assert_passed(result)
    assert env.marker.exists()
    assert env.log()[-1]["outcome"] == "pass"


def test_clean_hook_does_not_roll_back(env: Env) -> None:
    result = env.run("hook")
    assert result.returncode == 0
    assert env.mv_calls() == []
    assert env.state() == "Human Review"
    assert env.marker.exists()
    assert "Check Failure" not in env.card.read_text()


# ---------------------------------------------------------------------------
# 2. existing tests unchanged
# ---------------------------------------------------------------------------


def test_modified_test_committed_refused(env: Env) -> None:
    write(env.ws / "tests" / "test_app.py", "def test_a():\n    pass\n")
    env.commit()
    assert_refused(env.run("gate"), "existing test changed: tests/test_app.py (HEAD, index, working tree)")
    assert not env.marker.exists()


def test_modified_test_in_index_refused(env: Env) -> None:
    write(env.ws / "pkg" / "deep" / "test_deep.py", "def test_b():\n    assert 0\n")
    git(env.ws, "add", "pkg/deep/test_deep.py")
    assert_refused(env.run("gate"), "existing test changed: pkg/deep/test_deep.py (index, working tree)")


def test_modified_test_in_working_tree_refused(env: Env) -> None:
    write(env.ws / "web" / "button.test.ts", "// skipped\n")
    assert_refused(env.run("gate"), "existing test changed: web/button.test.ts (working tree)")


def test_modified_test_hidden_by_skip_worktree_refused(env: Env) -> None:
    git(env.ws, "update-index", "--skip-worktree", "tests/test_app.py")
    write(env.ws / "tests" / "test_app.py", "def test_a():\n    pass\n")
    assert_refused(env.run("gate"), "existing test changed: tests/test_app.py (working tree)")


def test_deleted_test_refused(env: Env) -> None:
    git(env.ws, "rm", "-q", "tests/test_app.py")
    env.commit()
    assert_refused(env.run("gate"), "tests/test_app.py (deleted in HEAD, index, working tree)")


def test_renamed_test_refused(env: Env) -> None:
    git(env.ws, "mv", "tests/test_app.py", "tests/test_app_renamed.py")
    env.commit()
    assert_refused(env.run("gate"), "existing test changed: tests/test_app.py (deleted in HEAD")


def test_new_test_allowed(env: Env) -> None:
    write(env.ws / "tests" / "test_new.py", "def test_new():\n    pass\n")
    write(env.ws / "src" / "test_helpers_test.py", "x = 1\n")
    env.commit()
    write(env.ws / "tests" / "test_untracked.py", "x = 1\n")
    assert_passed(env.run("gate"))


def test_allow_modified_tests_exempts_listed_files(env: Env) -> None:
    env.config(test_command="true", allow_modified_tests=["tests/test_app.py"])
    write(env.ws / "tests" / "test_app.py", "def test_a():\n    pass\n")
    env.commit()
    assert_passed(env.run("gate"))


def test_test_globs_replace_defaults(env: Env) -> None:
    env.config(test_command="true", test_globs=["web/**"])
    write(env.ws / "tests" / "test_app.py", "changed\n")
    env.commit()
    assert_passed(env.run("gate"))
    write(env.ws / "web" / "button.test.ts", "changed\n")
    assert_refused(env.run("gate"), "web/button.test.ts")


# ---------------------------------------------------------------------------
# 3. forbidden paths
# ---------------------------------------------------------------------------


def test_forbidden_committed_refused(env: Env) -> None:
    write(env.ws / "ours" / "hooks" / "evil.sh", "exit 0\n")
    env.commit()
    assert_refused(env.run("gate"), "forbidden path changed: ours/hooks/evil.sh")
    assert not env.marker.exists()


def test_forbidden_staged_refused(env: Env) -> None:
    write(env.ws / ".github" / "workflows" / "ci.yml", "on: push\n")
    git(env.ws, "add", ".github")
    assert_refused(env.run("gate"), "forbidden path changed: .github/workflows/ci.yml")


def test_forbidden_untracked_refused(env: Env) -> None:
    write(env.ws / ".env", "TOKEN=x\n")
    write(env.ws / "certs" / "server.key", "k\n")
    result = env.run("gate")
    assert_refused(result, "forbidden path changed: .env", "forbidden path changed: certs/server.key")


def test_forbidden_workflow_md_modified_refused(env: Env) -> None:
    write(env.ws / "WORKFLOW.md", "---\nagent: {}\n---\n")
    assert_refused(env.run("gate"), "forbidden path changed: WORKFLOW.md")


def test_rename_onto_forbidden_path_refused(env: Env) -> None:
    (env.ws / "ours").mkdir()
    git(env.ws, "mv", "src/app.py", "ours/app.py")
    env.commit()
    assert_refused(env.run("gate"), "forbidden path changed: ours/app.py")


def test_rename_away_from_forbidden_path_refused(env: Env) -> None:
    # The old name counts too: moving WORKFLOW.md away changes WORKFLOW.md.
    (env.ws / "docs").mkdir()
    git(env.ws, "mv", "WORKFLOW.md", "docs/WORKFLOW.md")
    env.commit()
    assert_refused(env.run("gate"), "forbidden path changed: WORKFLOW.md")


def test_config_forbidden_paths_extend_defaults(env: Env) -> None:
    env.config(test_command="true", forbidden_paths=["secrets/**"])
    write(env.ws / "secrets" / "a.txt", "x\n")
    write(env.ws / "ours" / "b.txt", "x\n")
    assert_refused(env.run("gate"), "secrets/a.txt", "ours/b.txt")


def test_agent_legal_paths_allowed(env: Env) -> None:
    write(env.ws / ".symphony" / "commit-message.txt", "msg\n")
    write(env.ws / "docs" / ID / "x.md", "notes\n")
    env.commit()
    write(env.ws / ".symphony" / "artifacts" / ID / "y.txt", "a\n")
    assert_passed(env.run("gate"))


def test_all_fast_violations_are_collected(env: Env) -> None:
    write(env.ws / "ours" / "x", "1\n")
    write(env.ws / "tests" / "test_app.py", "changed\n")
    env.set_card("Done", "## Plan\n\nx\n")
    result = env.run("gate")
    assert_refused(
        result,
        "forbidden path changed: ours/x",
        "existing test changed: tests/test_app.py",
        "card section missing or empty: ## Wiki Updates",
        "card section missing or empty: ## As-Is -> To-Be Report",
    )
    assert not env.marker.exists()


# ---------------------------------------------------------------------------
# 4. test.sh
# ---------------------------------------------------------------------------


def test_test_command_failure_refused(env: Env) -> None:
    env.config(test_command="echo 'FAILED tests/test_x.py'; exit 1")
    result = env.run("gate")
    assert_refused(result, "test.sh failed (exit 1)", "FAILED tests/test_x.py")


def test_test_command_output_tail_is_bounded(env: Env) -> None:
    env.config(test_command="for i in $(seq 1 3000); do echo line-$i; done; exit 1")
    result = env.run("gate")
    assert_refused(result, "line-3000")
    assert "line-1\n" not in result.stdout
    assert len(result.stdout) < check.TAIL_BYTES + 1000


def _alive(pid: int) -> bool:
    status = Path(f"/proc/{pid}/status")
    if status.exists():
        try:
            state = next(
                line for line in status.read_text().splitlines() if line.startswith("State:")
            )
        except (OSError, StopIteration):
            return False
        return " Z " not in state and "zombie" not in state
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_test_command_timeout_kills_whole_group(env: Env) -> None:
    pids = env.tmp / "pids"
    env.config(
        test_command=f"sleep 300 & echo $! >> {pids}; echo $$ >> {pids}; sleep 300",
        timeouts={"tests_s": 1},
    )
    started = time.monotonic()
    result = env.run("gate")
    assert time.monotonic() - started < 30
    assert_refused(result, "test.sh timed out after 1s")
    recorded = [int(p) for p in pids.read_text().split()]
    assert len(recorded) == 2
    deadline = time.monotonic() + 5
    while any(_alive(p) for p in recorded) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not any(_alive(p) for p in recorded)


def test_test_command_not_found_refused(env: Env) -> None:
    env.config(test_command="definitely-not-a-command-k05", summary=None)
    assert_refused(env.run("gate"), "command not found (exit 127)")


def test_background_straggler_does_not_turn_pass_into_timeout(env: Env) -> None:
    pids = env.tmp / "pids"
    env.config(test_command=f"sleep 300 & echo $! > {pids}; true", timeouts={"tests_s": 20})
    started = time.monotonic()
    assert_passed(env.run("gate"))
    assert time.monotonic() - started < 15
    pid = int(pids.read_text())
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(pid)


# ---------------------------------------------------------------------------
# 4b. summary line and baseline (TASK-K08b)
# ---------------------------------------------------------------------------


def test_exit_0_without_summary_line_refused(env: Env) -> None:
    env.config(test_command=f"touch {env.marker}", summary=None)
    result = env.run("gate")
    assert_refused(result, "нет строки итогов `PIPELINE_TESTS passed=<n> skipped=<n> failed=<n>`")
    assert env.marker.exists()


def test_failed_tests_refused_even_with_exit_0(env: Env) -> None:
    env.config(test_command="true", summary="passed=5 skipped=0 failed=1")
    assert_refused(env.run("gate"), "test.sh: failed=1 при коде 0")


def test_fewer_passed_than_baseline_refused_with_both_triples(env: Env) -> None:
    env.baseline(passed=3, skipped=1)
    env.config(test_command="true", summary="passed=2 skipped=1 failed=0")
    result = env.run("gate")
    assert_refused(
        result,
        "test.sh: passed=2 меньше базы 3",
        "сейчас passed=2 skipped=1 failed=0; база passed=3 skipped=1 failed=0",
        "перезапусти project-doctor",
    )


def test_more_skipped_than_baseline_refused(env: Env) -> None:
    env.baseline(passed=3, skipped=1)
    env.config(test_command="true", summary="passed=4 skipped=2 failed=0")
    assert_refused(env.run("gate"), "test.sh: skipped=2 больше базы 1")


def test_more_passed_and_fewer_skipped_than_baseline_pass(env: Env) -> None:
    env.baseline(passed=3, skipped=1)
    env.config(test_command="true", summary="passed=9 skipped=0 failed=0")
    assert_passed(env.run("gate"))


def test_last_summary_line_counts(env: Env) -> None:
    env.config(test_command="echo 'PIPELINE_TESTS passed=0 skipped=9 failed=9'")
    assert_passed(env.run("gate"))
    env.config(test_command="echo 'PIPELINE_TESTS passed=7 skipped=0 failed=0'",
               summary="passed=0 skipped=0 failed=0")
    assert_refused(env.run("gate"), "passed=0 меньше базы 1")


def test_summary_line_outside_the_output_tail_is_found(env: Env) -> None:
    # The summary must come from the whole output, not the 4000-byte tail.
    env.config(test_command="echo 'PIPELINE_TESTS passed=1 skipped=0 failed=0'; "
                            "for i in $(seq 1 3000); do echo line-$i; done", summary=None)
    assert_passed(env.run("gate"))


def test_indented_or_partial_summary_lines_do_not_count(env: Env) -> None:
    env.config(test_command="echo '  PIPELINE_TESTS passed=1 skipped=0 failed=0'; "
                            "echo 'PIPELINE_TESTS passed=1 skipped=0'", summary=None)
    assert_refused(env.run("gate"), "нет строки итогов")


def test_missing_baseline_refused_before_tests(env: Env) -> None:
    (env.board / ".ours-state" / "baseline.json").unlink()
    result = env.run("gate")
    assert_refused(result, "нет замера базы", "запусти project-doctor")
    assert not env.marker.exists()


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ("{not json", "повреждён"),
        (json.dumps({"base_branch": "main", "passed": 1, "skipped": 0}), "неполный"),
        (json.dumps({"base_branch": "main", "passed": -1, "skipped": 0, "failed": 0}), "неполный"),
        (json.dumps({"base_branch": "dev", "passed": 1, "skipped": 0, "failed": 0}),
         "замер сделан на ветке 'dev', а base_branch 'main'"),
    ],
)
def test_bad_baseline_refused(env: Env, payload: str, reason: str) -> None:
    write(env.board / ".ours-state" / "baseline.json", payload)
    result = env.run("gate")
    assert_refused(result, reason, "project-doctor")
    assert not env.marker.exists()


def test_hook_rolls_back_when_a_test_drops_out_of_the_run(env: Env) -> None:
    env.baseline(passed=2)
    env.config(test_command="true", summary="passed=1 skipped=0 failed=0")
    env.set_card("Done")
    assert env.run("hook").returncode == 0
    assert env.state() == "In Progress"
    text = env.card.read_text()
    assert "## Check Failure" in text and "passed=1 меньше базы 2" in text


# ---------------------------------------------------------------------------
# 5. config and base
# ---------------------------------------------------------------------------


def _assert_hook_rolls_back(env: Env, reason: str) -> None:
    env.set_card("Done")
    result = env.run("hook")
    assert result.returncode == 0
    assert env.mv_calls() == [["board", "mv", ID, "In Progress", "--root", str(env.board / "kanban")]]
    assert env.state() == "In Progress"
    assert reason in env.card.read_text()


def _drop_line(prefix: str):
    def mutate(env: Env) -> None:
        path = env.board / "project.toml"
        lines = path.read_text().splitlines(keepends=True)
        path.write_text("".join(line for line in lines if not line.startswith(prefix)))
    return mutate


def _append(text: str):
    def mutate(env: Env) -> None:
        path = env.board / "project.toml"
        path.write_text(path.read_text() + text)
    return mutate


def _base_not_ancestor(env: Env) -> None:
    write(env.code / "README.md", "moved\n")
    git(env.code, "commit", "-qam", "main moves on")
    env.record(git(env.code, "rev-parse", "main").strip())


BAD_CONFIG_OR_BASE = [
    (lambda env: (env.board / "project.toml").unlink(), "project.toml"),
    (lambda env: write(env.board / "project.toml", "[project\n"), "ошибка TOML"),
    (_drop_line("base_branch"), "нет ключа [project] base_branch"),
    (_drop_line("[tools]"), "неизвестный ключ(и) в [project]: claude"),
    (_append("[extra]\n"), "неизвестный раздел(ы) extra"),
    (lambda env: env.config(test_command="true", forbiden_paths=[]),
     "неизвестный ключ(и) в [checks]: forbiden_paths"),
    (lambda env: env.config(test_command="true", timeouts={"tests_s": "9"}), "целое число"),
    (lambda env: env.config(test_command="true", test_timeout_s=9),
     "ключ [checks] test_timeout_s убран (TASK-K08b): перенеси значение в [timeouts] tests_s"),
    (lambda env: env.config(test_command="true", forbidden_paths="ours"), "список непустых строк"),
    (lambda env: env.config(test_command="true", base_ref="-x"), "не может начинаться с '-'"),
    (lambda env: (env.board / ".ours-state" / f"{ID}.base.json").unlink(), "нет записи базы"),
    (lambda env: write(env.board / ".ours-state" / f"{ID}.base.json", "{}"), "нет корректного base_sha"),
    (_base_not_ancestor, "not an ancestor of HEAD (branch rewritten relative to the base)"),
]


@pytest.mark.parametrize(("mutate", "reason"), BAD_CONFIG_OR_BASE)
def test_bad_config_or_base_refused(env: Env, mutate, reason: str) -> None:
    mutate(env)
    assert_refused(env.run("gate"), reason)
    assert not env.marker.exists()


@pytest.mark.parametrize(("mutate", "reason"), [BAD_CONFIG_OR_BASE[i] for i in (0, 1, 2, 10, 12)])
def test_bad_config_or_base_hook_rolls_back(env: Env, mutate, reason: str) -> None:
    mutate(env)
    # Without a readable config the binary comes from SYMPHONY_PYTHON's dir.
    py_dir = env.tmp / "pybin"
    py_dir.mkdir()
    (py_dir / "python").symlink_to(sys.executable)
    (py_dir / "symphony").symlink_to(env.symphony)
    env.set_card("Done")
    result = env.run("hook", SYMPHONY_PYTHON=str(py_dir / "python"))
    assert result.returncode == 0
    assert env.state() == "In Progress"
    assert len(env.mv_calls()) == 1
    assert reason in env.card.read_text()


def test_base_from_record_survives_merge_into_target(env: Env) -> None:
    # Verify used to merge the ticket branch into the target; a merge-base
    # with the moving branch then hid every change (REPORT-07, item 1).
    write(env.ws / "tests" / "test_app.py", "def test_a():\n    pass\n")
    env.commit()
    git(env.code, "merge", "-q", "--ff-only", f"symphony/{ID}")
    assert_refused(env.run("gate"), "existing test changed: tests/test_app.py")


def test_test_sh_runs_without_login_shell(env: Env) -> None:
    env.config(test_command="shopt -q login_shell && exit 9; [ \"$PWD\" = \"" + str(env.ws)
               + "\" ] || exit 8; touch " + str(env.marker))
    assert_passed(env.run("gate"))
    assert env.marker.exists()


def test_timeouts_tests_s_is_the_default_test_timeout(env: Env) -> None:
    env.config(test_command="sleep 30", timeouts={"tests_s": 1})
    assert_refused(env.run("gate"), "test.sh timed out after 1s")


def test_old_checks_test_timeout_key_is_refused_with_a_hint(env: Env) -> None:
    env.config(test_command="true", test_timeout_s=1, timeouts={"tests_s": 600})
    result = env.run("gate")
    assert_refused(result, "[checks] test_timeout_s убран", "[timeouts] tests_s")
    assert not env.marker.exists()


@contextmanager
def held_lock(env: Env):
    path = env.board / ".ours-state" / f"{ID}.lock"
    with open(path, "a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield handle


def test_hook_skips_while_another_check_holds_the_lock(env: Env) -> None:
    write(env.ws / "ours" / "x", "violation\n")
    with held_lock(env):
        result = env.run("hook")
    assert result.returncode == 0
    assert env.state() == "Human Review"
    assert env.mv_calls() == []
    assert not env.marker.exists()
    assert env.log()[-1]["outcome"] == "skipped"
    assert "another check" in env.log()[-1]["reason"]


def test_gate_waits_for_the_lock_then_checks(env: Env) -> None:
    # wait budget = gate 45 s - tests 10 s - margin 30 s = 5 s
    env.config(test_command=f"touch {env.marker}", timeouts={"tests_s": 10, "gate_ms": 45000})
    with held_lock(env) as handle:
        timer = threading.Timer(1.0, lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN))
        timer.start()
        started = time.monotonic()
        result = env.run("gate")
        timer.join()
    assert time.monotonic() - started >= 1.0
    assert_passed(result)
    assert env.marker.exists()


def test_gate_refuses_when_the_lock_stays_busy(env: Env) -> None:
    env.config(test_command=f"touch {env.marker}", timeouts={"tests_s": 10, "gate_ms": 41000})
    with held_lock(env):
        result = env.run("gate")
    assert_refused(result, "still runs after 1s")
    assert not env.marker.exists()


def test_not_a_git_repo_refused(env: Env) -> None:
    plain = env.tmp / "workspaces" / "PLAIN-1"
    plain.mkdir()
    result = env.run("gate", cwd=plain, SYMPHONY_ISSUE_ID=ID)
    assert_refused(result, "is not a git working tree")


def test_not_a_git_repo_hook_rolls_back(env: Env) -> None:
    plain = env.tmp / "workspaces" / ID / "sub"  # unrelated cwd, same id
    plain.mkdir()
    (env.ws / ".git").rename(env.ws / ".git-away")
    env.set_card("Done")
    result = env.run("hook", cwd=env.ws)
    assert result.returncode == 0
    assert env.state() == "In Progress"
    assert "is not a git working tree" in env.card.read_text()


def test_board_inside_workspace_refused(env: Env) -> None:
    inside = env.ws / "board"
    inside.mkdir()
    assert_refused(env.run("gate", SYMPHONY_WORKFLOW_DIR=str(inside)), "inside the agent workspace")


def test_workspace_project_files_are_never_read(env: Env) -> None:
    # A permissive project.toml / test.sh committed on the branch changes nothing.
    write(env.ws / "project.toml", "[checks]\n")
    write(env.ws / "test.sh", "exit 0\n")
    env.commit()
    env.config(test_command="exit 3")
    assert_refused(env.run("gate"), "test.sh failed (exit 3)")


def test_missing_workflow_dir_refused(env: Env) -> None:
    assert_refused(env.run("gate", SYMPHONY_WORKFLOW_DIR=None), "SYMPHONY_WORKFLOW_DIR is not set")


# ---------------------------------------------------------------------------
# 6. card shape
# ---------------------------------------------------------------------------


def test_card_missing_section_refused(env: Env) -> None:
    env.set_card("Done", "## Wiki Updates\n\n- x\n")
    assert_refused(env.run("gate"), "card section missing or empty: ## As-Is -> To-Be Report")


def test_card_empty_section_refused(env: Env) -> None:
    env.set_card("Done", "## Wiki Updates\n\n   \n\n## As-Is -> To-Be Report\n\nok\n")
    assert_refused(env.run("gate"), "card section missing or empty: ## Wiki Updates")


def test_card_inline_section_text_counts(env: Env) -> None:
    env.set_card("Done", "## Wiki Updates: none needed\n## As-Is -> To-Be Report\n\nok\n")
    assert_passed(env.run("gate"))


def test_card_file_missing_refused(env: Env) -> None:
    env.card.unlink()
    assert_refused(env.run("gate"), f"card not found: kanban/{ID}.md")


def test_required_sections_from_config(env: Env) -> None:
    env.config(test_command="true", required_sections=["## Merge Status"])
    assert_refused(env.run("gate"), "## Merge Status")


# ---------------------------------------------------------------------------
# 7. hook behaviour
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["In Progress", "Verify", "Document", "Todo", "Blocked", "needs_human"])
def test_hook_ignores_other_states(env: Env, state: str) -> None:
    write(env.ws / "ours" / "x", "violation\n")
    env.set_card(state, "")
    result = env.run("hook")
    assert result.returncode == 0
    assert not env.marker.exists()
    assert env.mv_calls() == []
    assert env.state() == state
    assert not (env.board / ".ours-state" / f"{ID}.json").exists()


def test_hook_human_review_failure_rolls_back_with_note_and_counter(env: Env) -> None:
    write(env.ws / "ours" / "x", "violation\n")
    result = env.run("hook")
    assert result.returncode == 0
    assert env.state() == "In Progress"
    text = env.card.read_text()
    assert "## Check Failure" in text
    assert "forbidden path changed: ours/x" in text
    assert "failures: 1 of 3" in text
    assert "moved to: In Progress" in text
    assert env.counter()["failures"] == 1
    assert "ours/x" in env.counter()["last_reason"]
    state_dir = env.board / ".ours-state"
    assert (state_dir / ".gitignore").read_text() == "*\n"
    outcomes = [e["outcome"] for e in env.log()]
    assert outcomes == ["rollback:In Progress"]
    assert env.log()[-1]["note"] == "tracker"  # FileBoardTracker.append_note
    assert not env.marker.exists()


def test_hook_third_failure_escalates_to_needs_human(env: Env) -> None:
    env.config(test_command="exit 1")
    for expected in ("In Progress", "In Progress", "needs_human"):
        env.set_card("Human Review")
        assert env.run("hook").returncode == 0
        assert env.state() == expected
    assert env.counter()["failures"] == 3
    assert [c[3] for c in env.mv_calls()] == ["In Progress", "In Progress", "needs_human"]
    # The counter is never reset: a pass in between does not clear it.
    env.config(test_command="true")
    env.set_card("Human Review")
    env.run("hook")
    assert env.counter()["failures"] == 3


def test_hook_max_check_failures_from_config(env: Env) -> None:
    env.config(test_command="exit 1", max_check_failures=1)
    env.run("hook")
    assert env.state() == "needs_human"


def test_hook_done_failure_rolls_back(env: Env) -> None:
    env.config(test_command="echo boom; exit 2")
    env.set_card("Done")
    assert env.run("hook").returncode == 0
    assert env.state() == "In Progress"
    text = env.card.read_text()
    assert "test.sh failed (exit 2)" in text
    assert "    boom" in text  # tail as an indented block


def test_hook_internal_exception_in_done_rolls_back(env: Env) -> None:
    # A `git` that prints garbage for ls-tree makes the parser raise.
    real_git = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
    fake = write(
        env.bin / "git",
        textwrap.dedent(
            f"""\
            #!/bin/sh
            for a in "$@"; do
              if [ "$a" = ls-tree ]; then printf 'garbage\\0'; exit 0; fi
            done
            exec {real_git} "$@"
            """
        ),
    )
    fake.chmod(0o755)
    env.set_card("Done")
    result = env.run("hook", PATH=f"{env.bin}:{os.environ['PATH']}")
    assert result.returncode == 0
    assert env.state() == "In Progress"
    assert "internal error: ValueError" in env.card.read_text()
    assert "error" in [e["outcome"] for e in env.log()]


def test_gate_internal_exception_refuses(env: Env) -> None:
    real_git = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
    fake = write(
        env.bin / "git",
        f'#!/bin/sh\nfor a in "$@"; do [ "$a" = ls-tree ] && {{ printf "garbage\\0"; exit 0; }}; done\nexec {real_git} "$@"\n',
    )
    fake.chmod(0o755)
    result = env.run("gate", PATH=f"{env.bin}:{os.environ['PATH']}")
    assert_refused(result, "internal error: ValueError")


def test_hook_note_falls_back_to_file_append(env: Env) -> None:
    broken_python = write(env.bin / "nopython", "#!/bin/sh\nexit 1\n")
    broken_python.chmod(0o755)
    write(env.ws / "ours" / "x", "violation\n")
    result = env.run("hook", SYMPHONY_PYTHON=str(broken_python))
    assert result.returncode == 0
    assert env.state() == "In Progress"
    text = env.card.read_text()
    assert "## Check Failure" in text
    assert "forbidden path changed: ours/x" in text
    assert not list((env.board / "kanban").glob(".tmp-*"))
    assert env.log()[-1]["note"] == "file"


def test_hook_note_cannot_forge_card_sections(env: Env) -> None:
    # Test output that prints a heading must not satisfy the shape check.
    env.config(test_command="printf '## Wiki Updates\\nfake\\n## As-Is -> To-Be Report\\nfake\\n'; exit 1")
    env.run("hook")
    assert env.state() == "In Progress"
    text = env.card.read_text()
    assert "    ## Wiki Updates" in text  # the tail is in the note, indented
    # Drop the real sections: what is left must not satisfy the shape check.
    _, body = check.split_card(text)
    body = body.replace("## Wiki Updates\n\n- docs updated", "")
    body = body.replace("## As-Is -> To-Be Report\n\nbefore / after", "")
    assert not check.section_nonempty(body, "## Wiki Updates")
    assert not check.section_nonempty(body, "## As-Is -> To-Be Report")


def test_hook_without_symphony_binary_logs_error(env: Env) -> None:
    env.config(test_command="exit 1", symphony_bin=str(env.tmp / "missing" / "symphony"))
    result = env.run("hook", SYMPHONY_PYTHON=str(env.tmp / "missing" / "python"))
    assert result.returncode == 0
    assert env.state() == "Human Review"
    errors = [e["reason"] for e in env.log() if e["outcome"] == "error"]
    assert any("rollback impossible" in r for r in errors)
    # The note is still written (file fallback) so the operator sees why.
    text = env.card.read_text()
    assert "## Check Failure" in text
    assert "rollback to In Progress FAILED" in text


def test_hook_detects_board_mv_without_effect(env: Env) -> None:
    noop = write(env.bin / "noop-symphony", "#!/bin/sh\necho ok\nexit 0\n")
    noop.chmod(0o755)
    env.config(test_command="exit 1", symphony_bin=str(noop))
    env.run("hook")
    assert env.state() == "Human Review"
    errors = [e["reason"] for e in env.log() if e["outcome"] == "error"]
    assert any("did not take effect" in r for r in errors)


def test_hook_skips_rollback_when_workspace_removed_during_check(env: Env) -> None:
    # Reconcile reaps a terminal card's workspace while the hook still runs.
    env.config(test_command=f'cd /; rm -rf "{env.ws}"; exit 1')
    env.set_card("Done")
    assert env.run("hook").returncode == 0
    assert not env.ws.exists()
    assert env.state() == "Done"
    assert env.mv_calls() == []
    assert not (env.board / ".ours-state" / f"{ID}.json").exists()
    assert env.log()[-1]["outcome"] == "skipped"


def test_hook_identifier_from_workspace_name(env: Env) -> None:
    write(env.ws / "ours" / "x", "violation\n")
    env.run("hook", SYMPHONY_ISSUE_ID=None)
    assert env.mv_calls()[0][2] == ID


def test_hook_skips_rollback_when_parent_shell_dies(env: Env) -> None:
    """Q3: after_run killed by hooks.timeout_ms -> tests stop, card untouched."""
    pids = env.tmp / "pids"
    env.config(
        test_command=f"echo $PPID > {env.tmp / 'checkpid'}; sleep 300 & echo $! >> {pids}; sleep 300",
        timeouts={"tests_s": 600},
    )
    env.set_card("Done")
    shell = subprocess.Popen(
        ["bash", "-c", f'"{sys.executable}" "{CHECK}" hook; echo after'],
        cwd=env.ws,
        env=env.env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    deadline = time.monotonic() + 30
    while not pids.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert pids.exists()
    check_pid = int((env.tmp / "checkpid").read_text())
    shell.kill()  # what subprocess.run(timeout=...) does to the hook shell
    shell.wait()
    sleeper = int(pids.read_text().split()[0])
    deadline = time.monotonic() + 10
    while (_alive(check_pid) or _alive(sleeper)) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(check_pid)
    assert not _alive(sleeper)
    assert env.state() == "Done"
    assert env.mv_calls() == []
    assert env.log()[-1]["outcome"] == "skipped"


# ---------------------------------------------------------------------------
# 8. identifier validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["../x", "a b", "a/b", "-h", "..", "x;rm"])
def test_invalid_identifier_refused(env: Env, bad: str) -> None:
    assert_refused(env.run("gate", SYMPHONY_ISSUE_ID=bad), "invalid issue id")


def test_invalid_identifier_hook_does_nothing(env: Env) -> None:
    env.set_card("Done")
    result = env.run("hook", SYMPHONY_ISSUE_ID="../T-1")
    assert result.returncode == 0
    assert env.mv_calls() == []
    assert env.state() == "Done"
    assert any("invalid issue id" in e["reason"] for e in env.log())


def test_issue_title_is_ignored(env: Env) -> None:
    pwned = env.tmp / "pwned"
    result = env.run("gate", SYMPHONY_ISSUE_TITLE=f"$(touch {pwned})`touch {pwned}`")
    assert_passed(result)
    assert not pwned.exists()


# ---------------------------------------------------------------------------
# the pass mark for integrate.py hook and the note heading (S4-12)
# ---------------------------------------------------------------------------


def pass_mark(env: Env) -> dict:
    return json.loads((env.board / ".ours-state" / f"{ID}.pass.json").read_text())


def test_hook_pass_writes_the_one_shot_mark(env: Env) -> None:
    write(env.ws / "src" / "new.py", "y = 2\n")  # uncommitted: the mark must describe it
    result = env.run("hook")
    assert result.returncode == 0 and "PASS" in result.stdout
    mark = pass_mark(env)
    assert set(mark) == {"ppid", "tree", "head", "at"}
    assert mark["ppid"] == os.getpid()  # the hook shell: here, the test process
    assert mark["head"] == git(env.ws, "rev-parse", "HEAD").strip()
    assert mark["tree"] == check.worktree_tree(env.ws)
    assert mark["tree"] != git(env.ws, "rev-parse", "HEAD^{tree}").strip()
    # What the standard block commits next has exactly this tree.
    git(env.ws, "add", "-A")
    git(env.ws, "commit", "-q", "-m", "wip: turn")
    assert git(env.ws, "rev-parse", "HEAD^{tree}").strip() == mark["tree"]
    # The real index was not touched by the temporary one.
    assert git(env.ws, "status", "--porcelain").strip() == ""


def test_hook_mark_tree_ignores_the_board_link_and_amend(env: Env) -> None:
    result = env.run("hook")
    assert result.returncode == 0
    assert pass_mark(env)["tree"] == git(env.ws, "rev-parse", "HEAD^{tree}").strip()
    git(env.ws, "commit", "-q", "--amend", "-m", "other subject")
    assert pass_mark(env)["tree"] == git(env.ws, "rev-parse", "HEAD^{tree}").strip()


def test_hook_failure_writes_no_mark(env: Env) -> None:
    write(env.ws / "ours" / "x", "violation\n")
    assert env.run("hook").returncode == 0
    assert not (env.board / ".ours-state" / f"{ID}.pass.json").exists()


def test_hook_that_does_not_act_writes_no_mark(env: Env) -> None:
    env.set_card("In Progress")
    assert env.run("hook").returncode == 0
    assert not (env.board / ".ours-state" / f"{ID}.pass.json").exists()


def test_a_new_pass_overwrites_an_old_mark(env: Env) -> None:
    write(env.board / ".ours-state" / f"{ID}.pass.json", json.dumps({"ppid": 1, "tree": "old"}))
    assert env.run("hook").returncode == 0
    assert pass_mark(env)["tree"] != "old" and pass_mark(env)["ppid"] == os.getpid()


def test_append_note_takes_a_heading(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = check.Context("hook", env.board, env.ws, ID)
    monkeypatch.delenv("SYMPHONY_PYTHON", raising=False)
    assert check.append_note(ctx, "- by file", "Integration") == "file"
    monkeypatch.setenv("SYMPHONY_PYTHON", sys.executable)
    assert check.append_note(ctx, "- by tracker", "Integration") == "tracker"
    assert check.append_note(ctx, "- default heading") == "tracker"
    text = env.card.read_text()
    assert text.count("## Integration") == 2 and "- by file" in text and "- by tracker" in text
    assert text.count("## Check Failure") == 1 and "- default heading" in text


# ---------------------------------------------------------------------------
# the gate skips the tests for a tree that was merged (S4-13)
# ---------------------------------------------------------------------------


def merged_file(env: Env) -> Path:
    return env.board / ".ours-state" / f"{ID}.merged.json"


def put_merged(env: Env, tree: str | None = None, commit: str | None = "c" * 40) -> None:
    """What integrate.py hook leaves after Done; by default for the tree of the working copy."""
    payload = {"tree": tree or check.worktree_tree(env.ws), "commit": commit, "task": "T-0",
               "branch": "integration/T-0", "source": f"symphony/{ID}@{'a' * 40}", "at": "t"}
    write(merged_file(env), json.dumps(payload))


def gate_events(env: Env) -> list[dict]:
    return [e for e in env.log() if e["mode"] == "gate"]


def test_gate_skips_the_tests_for_the_merged_tree(env: Env) -> None:
    write(env.ws / "src" / "app.py", "x = 2\n")
    env.commit()
    put_merged(env)
    result = env.run("gate")
    assert_passed(result)
    assert not env.marker.exists()
    assert f"tests skipped: tree {check.worktree_tree(env.ws)[:12]} passed check.py hook" in result.stdout
    assert f"merged into integration/T-0 ({'c' * 12})" in result.stdout
    (event,) = gate_events(env)
    assert event["outcome"] == "pass" and event["tests"] == "skipped"
    assert event["reason"].startswith("tests skipped: tree ")
    assert not merged_file(env).exists()


def test_gate_skip_says_no_changes_for_an_empty_merge(env: Env) -> None:
    put_merged(env, commit=None)
    result = env.run("gate")
    assert_passed(result)
    assert "(no changes)" in result.stdout and not env.marker.exists()


def test_gate_skip_survives_the_core_squash(env: Env) -> None:
    write(env.ws / "src" / "a.py", "a = 1\n")
    env.commit("one")
    write(env.ws / "src" / "b.py", "b = 1\n")
    env.commit("two")
    put_merged(env)
    # finalize_delivery_history: soft reset to the base and one new commit.
    git(env.ws, "reset", "--soft", git(env.code, "rev-parse", "main").strip())
    git(env.ws, "commit", "-q", "-m", "squashed")
    assert_passed(env.run("gate"))
    assert not env.marker.exists()
    assert gate_events(env)[-1]["tests"] == "skipped"


def test_gate_runs_the_tests_when_the_tree_differs(env: Env) -> None:
    write(env.ws / "src" / "app.py", "x = 2\n")
    env.commit()
    put_merged(env)
    write(env.ws / "src" / "app.py", "x = 3\n")  # after the merge
    assert_passed(env.run("gate"))
    assert env.marker.exists()
    (event,) = gate_events(env)
    assert event["tests"] == "full" and event["tests_reason"] == "tree differs from the merged one"
    assert not merged_file(env).exists()


def test_gate_without_a_record_runs_the_tests(env: Env) -> None:
    assert_passed(env.run("gate"))
    assert env.marker.exists()
    assert "tests skipped" not in env.run("gate").stdout


def test_gate_record_does_not_excuse_a_failed_fast_check(env: Env) -> None:
    write(env.ws / "tests" / "test_app.py", "def test_a():\n    pass\n")
    env.commit()
    put_merged(env)
    assert_refused(env.run("gate"), "existing test changed: tests/test_app.py")
    assert not env.marker.exists()
    assert not merged_file(env).exists()


def test_gate_with_a_damaged_record_runs_the_tests(env: Env) -> None:
    write(merged_file(env), "not json")
    assert_passed(env.run("gate"))
    assert env.marker.exists() and not merged_file(env).exists()
    write(merged_file(env), json.dumps({"commit": None}))
    env.marker.unlink()
    assert_passed(env.run("gate"))
    assert env.marker.exists()
    assert gate_events(env)[-1]["tests_reason"] == "merged record has no tree"


def test_the_record_is_used_up_by_one_gate(env: Env) -> None:
    put_merged(env)
    assert_passed(env.run("gate"))
    assert not env.marker.exists()
    assert_passed(env.run("gate"))
    assert env.marker.exists()


def test_hook_ignores_the_merged_record(env: Env) -> None:
    put_merged(env)
    assert env.run("hook").returncode == 0
    assert env.marker.exists()
    assert merged_file(env).exists()
