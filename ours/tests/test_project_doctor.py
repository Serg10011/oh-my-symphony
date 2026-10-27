"""Tests for ours/bin/project-doctor, [timeouts] editing and [limits] (TASK-K08b).

A real temporary code repository, a board folder made by the real
project-new, fake setup.sh / test.sh, a fake `claude` on PATH and the real
`symphony doctor`. project-doctor always runs as a subprocess.

Run: python -m pytest ours/tests -q
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the kit is POSIX-only")

OURS = Path(__file__).resolve().parents[1]
PROJECT_NEW = OURS / "bin" / "project-new"
PROJECT_DOCTOR = OURS / "bin" / "project-doctor"
sys.path.insert(0, str(OURS / "hooks"))

import project_config as pc  # noqa: E402

GOOD_TEST = "echo 'PIPELINE_TESTS passed=2 skipped=1 failed=0'\n"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Doc:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.code = tmp / "code"
        self.root = tmp / "projects"
        self.board = self.root / "demo"
        self.home = tmp / "home"
        self.bin = tmp / "bin"
        self.tmpdir = tmp / "t"  # TMPDIR of project-doctor: the trial worktree
        self.setup_marker = tmp / "setup-ran"
        self.env_extra: dict[str, str] = {}
        self.port = free_port()
        for path in (self.home, self.tmpdir):
            path.mkdir()
        write(self.bin / "claude",
              '#!/bin/sh\n[ "$1" = --version ] && echo "fake-claude 9.9" && exit 0\nexit 0\n'
              ).chmod(0o755)
        self.code.mkdir()
        git(self.code, "init", "-q", "-b", "main")
        git(self.code, "config", "user.email", "t@example.invalid")
        git(self.code, "config", "user.name", "t")
        write(self.code / "tests" / "test_a.py", "def test_a():\n    pass\n")
        git(self.code, "add", "-A")
        git(self.code, "commit", "-q", "-m", "base")
        git(self.code, "branch", "dev")
        write(self.code / "README.md", "main only\n")
        git(self.code, "add", "-A")
        git(self.code, "commit", "-q", "-m", "main moves on")

    def env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("SYMPHONY_")}
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["HOME"] = str(self.home)
        env["TMPDIR"] = str(self.tmpdir)
        env.update(self.env_extra)
        return env

    def new(self, name: str = "demo", *, port: int | None = None, prefix: str = "DOC"):
        result = subprocess.run(
            [sys.executable, str(PROJECT_NEW), name, "--repo", str(self.code), "--base", "dev",
             "--prefix", prefix, "--port", str(port or self.port), "--root", str(self.root)],
            env=self.env(), capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stderr

    def ready(self, test: str = GOOD_TEST, setup: str | None = None) -> None:
        self.new()
        write(self.board / "setup.sh",
              setup or f'echo "$PWD" > {self.setup_marker}\necho "$SYMPHONY_PYTHON" >> '
                        f'{self.setup_marker}\n')
        write(self.board / "test.sh", test)

    def doctor(self, name: str = "demo"):
        return subprocess.run(
            [sys.executable, str(PROJECT_DOCTOR), name, "--root", str(self.root)],
            env=self.env(), capture_output=True, text=True, timeout=300,
        )

    @property
    def toml(self) -> Path:
        return self.board / "project.toml"

    @property
    def baseline(self) -> Path:
        return self.board / ".ours-state" / "baseline.json"

    def assert_clean_repo(self) -> None:
        """The trial worktree is gone and no branch was added."""
        assert git(self.code, "worktree", "list", "--porcelain").count("worktree ") == 1
        assert sorted(git(self.code, "branch", "--format=%(refname:short)").split()) == [
            "dev", "main"]
        assert list(self.tmpdir.iterdir()) == []


@pytest.fixture
def doc(tmp_path: Path) -> Doc:
    return Doc(tmp_path)


# ---------------------------------------------------------------------------
# project-doctor runs
# ---------------------------------------------------------------------------


def test_doctor_success_writes_baseline_timeouts_and_workflow(doc: Doc) -> None:
    doc.ready()
    before = doc.toml.read_text()
    result = doc.doctor()
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert "FAIL" not in out.replace("FAIL: 0", "")
    assert "Итог: проект готов" in out
    assert "PASS  claude --version: fake-claude 9.9" in out
    # Known warnings of every board are shown, but not as failures.
    assert "WARN  (известно, не провал" in out and "git history writable" in out
    assert "agent git grant" in out
    baseline = json.loads(doc.baseline.read_text())
    assert baseline["base_branch"] == "dev"
    assert baseline["base_sha"] == git(doc.code, "rev-parse", "dev")
    assert (baseline["passed"], baseline["skipped"], baseline["failed"]) == (2, 1, 0)
    assert baseline["setup_s"] >= 0 and baseline["tests_s"] >= 0
    assert baseline["measured_at"].endswith("Z")
    # setup.sh ran in a detached trial worktree of dev, outside board and code.
    ran_in, python = doc.setup_marker.read_text().split("\n")[:2]
    assert Path(ran_in).parent.parent == doc.tmpdir.resolve()
    assert python == sys.executable
    doc.assert_clean_repo()
    # Only [timeouts] changed; fast scripts -> the minimum values.
    project = pc.load_project(doc.board)
    assert project["timeouts"] == {"tests_s": 300, "gate_ms": 360000, "hooks_ms": 360000}
    after = doc.toml.read_text()
    head = before.index("[timeouts]")
    assert after[:head] == before[:head]
    assert after[after.index("[limits]"):] == before[before.index("[limits]"):]
    workflow = (doc.board / "WORKFLOW.md").read_text()
    assert "timeout_ms: 360000" in workflow and "done_gate_timeout_ms: 360000" in workflow
    # A second run measures again and keeps the file as is.
    again = doc.doctor()
    assert again.returncode == 0 and "[timeouts] без изменений" in again.stdout
    assert doc.toml.read_text() == after


@pytest.mark.parametrize(
    ("test", "reason"),
    [
        ("echo 'PIPELINE_TESTS passed=1 skipped=0 failed=1'\n", "на базе падают тесты"),
        ("echo 'FAILED x'\nexit 3\n", "test.sh: код 3"),
        ("echo 'all good'\n", "нет строки итогов"),
    ],
    ids=["failed-test", "exit-code", "no-summary"],
)
def test_doctor_refuses_a_red_base(doc: Doc, test: str, reason: str) -> None:
    doc.ready(test=test)
    before = doc.toml.read_text()
    result = doc.doctor()
    assert result.returncode == 1
    assert f"FAIL  {reason}" in result.stdout or reason in result.stdout
    assert "Итог: проект НЕ готов" in result.stdout
    assert not doc.baseline.exists()
    assert doc.toml.read_text() == before
    doc.assert_clean_repo()


def test_doctor_refuses_a_failing_setup(doc: Doc) -> None:
    doc.ready(setup="echo broken >&2\nexit 7\n")
    result = doc.doctor()
    assert result.returncode == 1
    assert "FAIL  setup.sh: код 7" in result.stdout and "broken" in result.stdout
    assert not doc.baseline.exists()
    doc.assert_clean_repo()


def test_doctor_refuses_stub_scripts_without_a_trial_run(doc: Doc) -> None:
    doc.new()
    result = doc.doctor()
    assert result.returncode == 1
    assert "FAIL  setup.sh: не настроено" in result.stdout
    assert "FAIL  test.sh: не настроено" in result.stdout
    assert "SKIP  пробный прогон" in result.stdout
    assert not doc.setup_marker.exists() and not doc.baseline.exists()
    doc.assert_clean_repo()


def test_doctor_keeps_fixed_timeouts_and_shows_the_recommendation(doc: Doc) -> None:
    doc.ready()
    doc.toml.write_text(doc.toml.read_text().replace(
        "# fixed = true ", "fixed = true   "))
    before = doc.toml.read_text()
    result = doc.doctor()
    assert result.returncode == 0, result.stdout
    assert "[timeouts] fixed = true, не трогаю; рекомендация: tests_s = 300" in result.stdout
    assert doc.toml.read_text() == before
    assert doc.baseline.exists()


def test_doctor_warns_when_fixed_timeouts_are_below_the_recommendation(doc: Doc) -> None:
    doc.ready()
    text = doc.toml.read_text().replace("# fixed = true ", "fixed = true   ").replace(
        "# tests_s = 900 ", "tests_s = 60   ")
    doc.toml.write_text(text)
    result = doc.doctor()
    assert result.returncode == 0, result.stdout
    assert "WARN  [timeouts] fixed = true, текущие меньше рекомендации" in result.stdout
    assert doc.toml.read_text() == text


def test_doctor_preserves_every_other_byte_of_project_toml(doc: Doc) -> None:
    doc.ready()
    # A hand-edited file: comments, odd spacing, keys in an unusual order.
    custom = doc.toml.read_text().split("[timeouts]")[0] + (
        "[timeouts]   # мои таймауты\n"
        "hooks_ms   =   999000   # оставь комментарий\n"
        "#   gate_ms = 1   # что-то\n"
        "\n"
        "# комментарий к следующему разделу\n"
        "[limits]\n"
        "max_turns = 40  # больше обычного\n"
    )
    doc.toml.write_text(custom)
    assert doc.doctor().returncode == 0
    expected = custom.replace(
        "[timeouts]   # мои таймауты\n",
        "[timeouts]   # мои таймауты\ntests_s = 300\n",
    ).replace("hooks_ms   =   999000   # оставь", "hooks_ms   =   360000   # оставь").replace(
        "#   gate_ms = 1   # что-то", "gate_ms = 360000   # что-то")
    assert doc.toml.read_text() == expected
    assert pc.load_project(doc.board)["limits"]["max_turns"] == 40


def test_doctor_adds_a_missing_timeouts_section_at_the_end(doc: Doc) -> None:
    doc.ready()
    text = doc.toml.read_text()
    custom = text[:text.index("[timeouts]")] + text[text.index("[limits]"):]
    doc.toml.write_text(custom)
    assert doc.doctor().returncode == 0
    assert doc.toml.read_text() == (
        custom + "\n[timeouts]\ntests_s = 300\ngate_ms = 360000\nhooks_ms = 360000\n")


def test_doctor_refuses_a_taken_port_and_a_broken_tool(doc: Doc) -> None:
    doc.ready()
    doc.new("other", prefix="OTH", port=free_port())
    # Edited by hand later: project-new itself refuses a taken port.
    other = doc.root / "other" / "project.toml"
    text = other.read_text()
    other.write_text(text.replace(f"port = {pc.load_project(other.parent)['project']['port']}",
                                  f"port = {doc.port}"))
    (doc.bin / "claude").write_text("#!/bin/sh\nexit 5\n")
    result = doc.doctor()
    assert result.returncode == 1
    assert f"FAIL  порт {doc.port} занят проектом other" in result.stdout
    assert "FAIL  claude --version: код 5" in result.stdout
    assert not doc.baseline.exists()


def test_doctor_refuses_the_old_test_timeout_key(doc: Doc) -> None:
    doc.ready()
    doc.toml.write_text(doc.toml.read_text().replace(
        "max_check_failures = 2 ", "test_timeout_s = 900 "))
    result = doc.doctor()
    assert result.returncode == 1
    assert "[checks] test_timeout_s убран" in result.stdout
    assert "перенеси значение в [timeouts] tests_s" in result.stdout


# ---------------------------------------------------------------------------
# library: timeouts and [limits]
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("setup_s", "tests_s", "expected"),
    [
        (0, 0, (300, 360000, 360000)),
        (10, 149.9, (300, 360000, 360000)),
        (10, 151, (360, 420000, 420000)),
        (100, 200, (420, 480000, 480000)),
        (1000, 10, (300, 360000, 1560000)),
        (400.2, 300, (600, 660000, 661000)),
    ],
)
def test_recommend_timeouts(setup_s: float, tests_s: float, expected: tuple) -> None:
    got = pc.recommend_timeouts(setup_s, tests_s)
    assert (got["tests_s"], got["gate_ms"], got["hooks_ms"]) == expected


def test_update_timeouts_text_keeps_crlf_and_other_lines() -> None:
    text = "[a]\r\nx = 1\r\n[timeouts]\r\nfixed = false\r\n[b]\r\ny = 2\r\n"
    out = pc.update_timeouts_text(text, {"tests_s": 300, "gate_ms": 1})
    assert out == "[a]\r\nx = 1\r\n[timeouts]\r\ntests_s = 300\r\ngate_ms = 1\r\nfixed = false\r\n[b]\r\ny = 2\r\n"
    assert tomllib.loads(out)["timeouts"] == {"tests_s": 300, "gate_ms": 1, "fixed": False}


def test_limits_reach_the_core_config(doc: Doc) -> None:
    from symphony.workflow import build_service_config, load_workflow

    doc.new()
    cfg = build_service_config(load_workflow(doc.board / "WORKFLOW.md"))
    # Strict defaults of the project-new stub (TASK-K14).
    assert (cfg.agent.max_turns, cfg.agent.max_total_turns, cfg.agent.max_attempts) == (12, 20, 2)
    assert cfg.agent.max_state_turns == 2
    doc.toml.write_text(doc.toml.read_text().replace("max_attempts = 2 ", "max_attempts = 1"))
    project = pc.load_project(doc.board)
    (doc.board / "WORKFLOW.md").write_text(pc.render_workflow(doc.board, project))
    cfg = build_service_config(load_workflow(doc.board / "WORKFLOW.md"))
    assert (cfg.agent.max_turns, cfg.agent.max_total_turns, cfg.agent.max_attempts) == (12, 20, 1)


@pytest.mark.parametrize(
    ("limits", "reason"),
    [
        ({"max_turns": 3}, "max_turns не меньше числа активных статусов доски"),
        ({"max_turns": 70}, "max_total_turns меньше max_turns"),
        ({"max_attempts": 0}, "целое число больше нуля"),
        ({"max_tokens": 5}, "неизвестный ключ(и) в [limits]: max_tokens"),
    ],
)
def test_bad_limits_refused(limits: dict, reason: str) -> None:
    data = {
        "project": {"name": "x", "repo": "/r", "base_branch": "dev", "id_prefix": "XX",
                    "port": 9000},
        "tools": {"python": "/p", "symphony": "/s", "claude": "/c"},
        "limits": limits,
    }
    with pytest.raises(pc.ProjectError, match=re.escape(reason)):
        pc.validate_project(data)


# ---------------------------------------------------------------------------
# reduced environment of the trial (TASK-K11)
# ---------------------------------------------------------------------------

PATH_HINT = "скрипты должны сами задавать инструменты полными путями"


def fake_tool(doc: Doc) -> Path:
    """A program that exists only on the test's PATH, like node from nvm."""
    tool = write(doc.bin / "fake-node", "#!/bin/sh\necho v0.0.0\n")
    tool.chmod(0o755)
    return tool


@pytest.mark.parametrize("script", ["setup.sh", "test.sh"])
def test_doctor_fails_a_script_that_relies_on_the_terminal_path(doc: Doc, script: str) -> None:
    doc.ready()
    tool = fake_tool(doc)
    call = "fake-node --version || exit $?\n"
    write(doc.board / script, call if script == "setup.sh" else call + GOOD_TEST)
    result = doc.doctor()
    assert result.returncode == 1, result.stdout
    assert f"FAIL  {script}: код 127" in result.stdout
    assert "fake-node" in result.stdout and PATH_HINT in result.stdout
    assert "Итог: проект НЕ готов" in result.stdout
    assert not doc.baseline.exists()
    doc.assert_clean_repo()
    # The same script with a full path passes.
    full = f"{tool} --version || exit $?\n"
    write(doc.board / script, full if script == "setup.sh" else full + GOOD_TEST)
    ok = doc.doctor()
    assert ok.returncode == 0, ok.stdout
    assert PATH_HINT not in ok.stdout


def test_trial_scripts_get_only_the_reduced_environment(doc: Doc) -> None:
    doc.ready()
    dump = doc.tmp / "env-dump"
    write(doc.board / "setup.sh", f'compgen -e > {dump}\necho "$PATH" >> {dump}\n')
    doc.env_extra = {"LANG": "C.UTF-8", "SECRET_TERMINAL_VAR": "x", "NODE_PATH": "/x"}
    result = doc.doctor()
    assert result.returncode == 0, result.stdout
    *names, path = dump.read_text().split("\n")[:-1]
    assert path == "/usr/bin:/bin:/usr/sbin:/sbin"
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR",
               "SYMPHONY_PYTHON", "SYMPHONY_WORKFLOW_DIR",
               "PWD", "OLDPWD", "SHLVL", "_"}  # set by bash itself
    assert set(names) <= allowed, sorted(set(names) - allowed)
    assert {"PATH", "HOME", "TMPDIR", "SYMPHONY_PYTHON", "SYMPHONY_WORKFLOW_DIR"} <= set(names)
    assert "LANG" in names


def load_doctor():
    from importlib.machinery import SourceFileLoader
    from importlib.util import module_from_spec, spec_from_loader

    loader = SourceFileLoader("project_doctor_under_test", str(PROJECT_DOCTOR))
    module = module_from_spec(spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def test_run_script_reports_a_timeout_not_exit_minus_nine(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time as time_module

    doctor = load_doctor()
    real_killpg = doctor.os.killpg

    def slow_killpg(pid: int, sig: int) -> None:
        real_killpg(pid, sig)
        time_module.sleep(0.5)  # the leader is surely dead when run_script looks again

    monkeypatch.setattr(doctor.os, "killpg", slow_killpg)
    script = write(tmp_path / "slow.sh", "sleep 60\n")
    code, elapsed, _tail, _summary = doctor.run_script(
        script, tmp_path, {"PATH": "/usr/bin:/bin"}, 0.3)
    assert code is None  # not -9
    assert elapsed < 30


# ---------------------------------------------------------------------------
# stall watchdog follows [timeouts] tests_s (TASK-K13)
# ---------------------------------------------------------------------------


def test_doctor_recomputes_the_stall_watchdog_with_the_timeouts(doc: Doc) -> None:
    doc.ready()
    result = doc.doctor()
    assert result.returncode == 0, result.stdout + result.stderr
    # tests_s = 300 was just measured -> max(300 s, 300 + 120 s) = 420 s
    assert "PASS  stall_timeout_ms = 420000 (max(300 с, tests_s 300 + 120 с))" in result.stdout
    assert "stall_timeout_ms: 420000\n" in (doc.board / "WORKFLOW.md").read_text()
    # A fixed, larger tests_s is not overwritten and the watchdog follows it.
    doc.toml.write_text(doc.toml.read_text().replace("tests_s = 300", "tests_s = 900")
                        .replace("[timeouts]", "[timeouts]\nfixed = true", 1))
    again = doc.doctor()
    assert again.returncode == 0, again.stdout + again.stderr
    assert "PASS  stall_timeout_ms = 1020000" in again.stdout
    assert "stall_timeout_ms: 1020000\n" in (doc.board / "WORKFLOW.md").read_text()
