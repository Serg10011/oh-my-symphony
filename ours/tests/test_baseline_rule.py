"""Tests for the rule "which files change what the tests measure", per project (E2).

project.toml [baseline] paths replaces the Python rule BASELINE_RULE as a whole; the
same rule serves check.py (which older measure to trust) and post-merge (measure again?).
Without the section nothing changes.

Real temporary git repositories (test_baselines / test_post_merge helpers).

Run: python -m pytest ours/tests -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import test_check as tc
from test_baselines import history, main_commit, main_head, outcome, rec, start_from
from test_post_merge import FakeOps, idle, write
from test_post_merge import Env as PmEnv

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the kit is POSIX-only")

OURS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OURS / "hooks"))

import project_config as pc  # noqa: E402

# The example of the project-new stub: Vitest, aligned with [checks] test_globs below.
VITEST_PATHS = [
    "tests/**", "**/*.test.ts", "**/*.test.tsx", "**/*.spec.*", "vitest.*",
    "vite.config.*", "package.json", "package-lock.json", ".nvmrc", "tsconfig*.json",
]
VITEST_TEST_GLOBS = ["tests/**", "**/*.test.ts", "**/*.test.tsx", "**/*.spec.*"]
PYTHON_PATHS = ["tests/a.py", "ours/tests/e2e/fake_claude.py", "pkg/conftest.py", "pyproject.toml",
                "docs/a.md", "src/app.py", "pkg/test_a.py", "pkg/a_test.py", "uv.lock", "README.md"]


def toml_list(items: list[str]) -> str:
    return "[" + ", ".join(f'"{item}"' for item in items) + "]"


def project_text(baseline: str = "", checks: str = "") -> str:
    """A valid project.toml (the board of a project), with the given extra text."""
    return (
        '[project]\nname = "demo"\nrepo = "/tmp/demo"\nbase_branch = "main"\nid_prefix = "DM"\nport = 9990\n'
        '[tools]\npython = "/usr/bin/python3"\nsymphony = "/bin/symphony"\nclaude = "/bin/claude"\n'
        f"[checks]\n{checks}\n{baseline}"
    )


def vitest_project(extra: str = "") -> dict:
    text = project_text(
        f"[baseline]\npaths = {toml_list(VITEST_PATHS)}\n{extra}",
        f"test_globs = {toml_list(VITEST_TEST_GLOBS)}",
    )
    import tomllib

    return pc.validate_project(tomllib.loads(text))


def load(text: str) -> dict:
    import tomllib

    return pc.validate_project(tomllib.loads(text))


# ---------------------------------------------------------------------------
# the rule (tests 1, 2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", PYTHON_PATHS)
def test_without_the_section_the_rule_is_the_python_one(path: str) -> None:
    project = load(project_text())
    assert project["baseline"] == {}
    expected = path in ("tests/a.py", "ours/tests/e2e/fake_claude.py", "pkg/conftest.py", "pyproject.toml",
                        "pkg/test_a.py", "pkg/a_test.py", "uv.lock")
    assert pc.affects_baseline(path, project) is expected
    assert pc.affects_baseline(path) is expected
    assert pc.paths_rule(None)(path) is expected


@pytest.mark.parametrize("path", [
    "vitest.config.ts", "vitest.setup.ts", "vitest.workspace.ts", "tests/x.test.ts", "src/y.test.tsx",
    "src/z.spec.ts", ".nvmrc", "tsconfig.base.json",
])
def test_with_the_section_the_vitest_files_change_the_measure(path: str) -> None:
    assert pc.affects_baseline(path, vitest_project())


@pytest.mark.parametrize("path", ["src/app.ts", "docs/a.md", "pkg/test_a.py", "pkg/conftest.py", "pyproject.toml"])
def test_with_the_section_the_python_rule_is_gone(path: str) -> None:
    assert not pc.affects_baseline(path, vitest_project())


# ---------------------------------------------------------------------------
# consistency with [checks] test_globs (test 2a)
# ---------------------------------------------------------------------------


def refusal(text: str) -> str:
    with pytest.raises(pc.ProjectError) as caught:
        load(text)
    return str(caught.value)


def test_a_missing_spec_glob_is_named() -> None:
    paths = [p for p in VITEST_PATHS if p != "**/*.spec.*"]
    message = refusal(project_text(f"[baseline]\npaths = {toml_list(paths)}\n"))
    assert "**/*.spec.*" in message and "нет шаблонов" in message


def test_the_example_paths_with_default_test_globs_miss_exactly_the_python_and_go_ones() -> None:
    message = refusal(project_text(f"[baseline]\npaths = {toml_list(VITEST_PATHS)}\n"))
    missing = message.split(". Файл", 1)[0].rsplit(": ", 1)[1]
    assert missing.split(", ") == ["**/test_*.py", "**/*_test.py", "**/*_test.go"]
    assert "по умолчанию" in message


def test_explicit_test_globs_of_the_section_are_enough() -> None:
    assert vitest_project()["baseline"]["paths"] == VITEST_PATHS


def test_explicit_test_globs_missing_in_paths_are_named() -> None:
    message = refusal(project_text(
        f"[baseline]\npaths = {toml_list(VITEST_PATHS)}\n", 'test_globs = ["tests/**", "e2e/**"]'
    ))
    assert "e2e/**" in message and "[checks] test_globs" in message and "tests/**," not in message


def test_the_match_is_literal() -> None:
    message = refusal(project_text('[baseline]\npaths = ["tests/*"]\n', 'test_globs = ["tests/**"]'))
    assert "tests/**" in message


def test_without_the_section_there_is_no_consistency_check() -> None:
    load(project_text("", 'test_globs = ["tests/**", "e2e/**"]'))


# ---------------------------------------------------------------------------
# the loader (test 5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("body", "needle"), [
    ("paths = []", "пуст"),
    ('paths = "tests/**"', "список"),
    ("paths = [1]", "список"),
    ('paths = ["tests/**", ""]', "список"),
    ('paths = ["../tests/**"]', ".."),
    ('paths = ["a/../tests/**"]', ".."),
    ('paths = ["/tests/**"]', "/"),
    ('paths = ["."]', "empty glob pattern"),
    ('paths = ["tests/**"]\nfiles = ["x"]', "неизвестный ключ"),
    ("", "нет ключа"),
])
def test_a_wrong_section_is_refused_with_a_reason(body: str, needle: str) -> None:
    message = refusal(project_text(f"[baseline]\n{body}\n", 'test_globs = ["tests/**"]'))
    assert needle in message and "project.toml" in message


def test_the_section_must_be_a_table() -> None:
    assert "должен быть разделом" in refusal("baseline = 1\n" + project_text())


def test_a_board_without_the_section_loads_as_before() -> None:
    project = load(project_text())
    assert project["baseline"] == {}


def test_the_board_rule_is_read_from_project_toml_alone(tmp_path: Path) -> None:
    write(tmp_path / pc.PROJECT_FILE, '[project]\nname = "x"\n[baseline]\npaths = ["tests/**"]\n[checks]\ntest_globs = ["tests/**"]\n')
    rule = pc.load_baseline_rule(tmp_path)
    assert rule("tests/a.ts") and not rule("pkg/test_a.py")
    write(tmp_path / pc.PROJECT_FILE, "[project]\nname = 'x'\n")
    assert pc.load_baseline_rule(tmp_path)("pkg/test_a.py")
    write(tmp_path / pc.PROJECT_FILE, "[project\n")
    with pytest.raises(pc.ProjectError, match="TOML"):
        pc.load_baseline_rule(tmp_path)
    (tmp_path / pc.PROJECT_FILE).unlink()
    with pytest.raises(pc.ProjectError, match="нет файла"):
        pc.load_baseline_rule(tmp_path)


# ---------------------------------------------------------------------------
# check.py: the rule of the board in the search of an older measure (test 3)
# ---------------------------------------------------------------------------


def vitest_board(env: tc.Env) -> None:
    raw = (
        f'[project]\nname = "t"\nrepo = "{env.code}"\nid_prefix = "TT"\nport = 9990\nbase_branch = "main"\n'
        f'[tools]\npython = "{sys.executable}"\nclaude = "/bin/true"\nsymphony = "{env.symphony}"\n'
        f"[checks]\ntest_globs = {toml_list(VITEST_TEST_GLOBS)}\n"
        f"[baseline]\npaths = {toml_list(VITEST_PATHS)}\n"
    )
    env.config(raw=raw, test_command="true", summary="passed=1 skipped=0 failed=0")


@pytest.fixture
def env(tmp_path: Path) -> tc.Env:
    return tc.Env(tmp_path)


def test_a_changed_vitest_config_makes_the_older_measure_void(env: tc.Env) -> None:
    a = main_head(env)
    c = main_commit(env, "vitest.config.ts", "export default {}\n")
    history(env, rec(a, 3))
    start_from(env, c)
    vitest_board(env)
    assert env.run("gate").returncode == 0  # no measure: the tests run, only a failure refuses
    event = outcome(env, "baseline_unknown")
    assert "vitest.config.ts" in event["reason"]
    assert outcome(env, "pass")["baseline_match"] == "unknown"


def test_a_changed_document_keeps_the_older_measure(env: tc.Env) -> None:
    a = main_head(env)
    c = main_commit(env, "docs/a.md", "just a document\n")
    history(env, rec(a, 3))
    start_from(env, c)
    vitest_board(env)
    assert env.run("gate").returncode != 0  # passed=1 is less than the measure 3
    assert outcome(env, "fail")["baseline_match"] == "ancestor"


def test_a_python_test_file_does_not_void_the_measure_of_a_vitest_board(env: tc.Env) -> None:
    a = main_head(env)
    c = main_commit(env, "pkg/conftest.py", "x = 1\n")
    history(env, rec(a, 3))
    start_from(env, c)
    vitest_board(env)
    assert env.run("gate").returncode != 0
    assert outcome(env, "fail")["baseline_match"] == "ancestor"


def test_a_wrong_baseline_section_refuses_the_card_checks(env: tc.Env) -> None:
    env.config(raw=(
        f'[project]\nname = "t"\nrepo = "{env.code}"\nid_prefix = "TT"\nport = 9990\nbase_branch = "main"\n'
        f'[tools]\npython = "{sys.executable}"\nclaude = "/bin/true"\nsymphony = "{env.symphony}"\n'
        '[baseline]\npaths = ["vitest.*"]\n'
    ))
    tc.assert_refused(env.run("gate"), "config:", "**/*.spec.*")


# ---------------------------------------------------------------------------
# post-merge (test 4)
# ---------------------------------------------------------------------------


@pytest.fixture
def pm_env(tmp_path: Path) -> PmEnv:
    return PmEnv(tmp_path)


def set_vitest_rule(env: PmEnv) -> None:
    board = env.root / "oms"
    text = (board / "project.toml").read_text()
    write(board / "project.toml", text + f"[checks]\ntest_globs = {toml_list(VITEST_TEST_GLOBS)}\n"
          f"[baseline]\npaths = {toml_list(VITEST_PATHS)}\n")


def test_a_project_with_the_vitest_rule_measures_again_when_a_test_changes(pm_env: PmEnv) -> None:
    set_vitest_rule(pm_env)
    pm_env.push({"tests/x.test.ts": "it('x', () => {})\n"})
    fake = FakeOps(api=None)
    assert pm_env.run(fake) == 0
    assert fake.calls == ["project-doctor"]


def test_a_project_with_the_vitest_rule_ignores_sources_and_python_tests(pm_env: PmEnv) -> None:
    set_vitest_rule(pm_env)
    pm_env.push({"web/a.ts": "export {}\n", "pkg/test_c.py": "def test_c(): pass\n"})
    fake = FakeOps(api=None)
    assert pm_env.run(fake) == 0
    assert fake.calls == []


def test_a_project_without_the_section_keeps_the_python_rule(pm_env: PmEnv) -> None:
    pm_env.push({"web/x.test.ts": "it('x', () => {})\n"})
    fake = FakeOps(api=None)
    assert pm_env.run(fake) == 0 and fake.calls == []
    pm_env.push({"pkg/test_c.py": "def test_c(): pass\n"})
    fake = FakeOps(api=None)
    assert pm_env.run(fake) == 0 and fake.calls == ["project-doctor"]


def test_an_unreadable_project_toml_is_a_refusal_with_a_reason(pm_env: PmEnv, capsys) -> None:
    write(pm_env.root / "oms" / "project.toml", "[project\n")
    pm_env.push({"tests/x.test.ts": "it('x', () => {})\n"})
    fake = FakeOps(api=None)
    assert pm_env.run(fake) == 1
    out = capsys.readouterr().out
    assert "Ошибка: проект oms" in out and "Отказ" in out and "ошибка TOML" not in out.split("не читается")[0]
    assert "Expected" in out  # the reason of the parser is shown
    assert fake.calls == []
    assert not pm_env.state.exists()  # nothing was applied


def test_an_unreadable_toml_of_another_project_is_not_a_refusal_when_filtered_out(pm_env: PmEnv) -> None:
    write(pm_env.root / "elsewhere" / "project.toml", "[project\n")
    assert pm_env.run(FakeOps(), "--project", "oms") == 0


def test_a_wrong_baseline_section_is_a_refusal_in_dry_run_too(pm_env: PmEnv, capsys) -> None:
    board = pm_env.root / "oms"
    write(board / "project.toml", (board / "project.toml").read_text() + '[baseline]\npaths = []\n')
    assert pm_env.run(FakeOps(api=idle()), dry=True) == 0
    out = capsys.readouterr().out
    assert "ОТКАЗ" in out and "пуст" in out


def test_a_project_with_a_wrong_baseline_does_not_stop_the_others(pm_env: PmEnv, capsys) -> None:
    pm_env.project("second", pm_env.clone, 9979)
    board = pm_env.root / "oms"
    write(board / "project.toml", (board / "project.toml").read_text() + "[baseline]\npaths = []\n")
    pm_env.push({"pkg/test_c.py": "def test_c(): pass\n"})
    fake = FakeOps(api=None)
    assert pm_env.run(fake) == 1
    assert fake.calls == ["project-doctor"]  # "second" was served, "oms" was refused
    assert "ОТКАЗ" in capsys.readouterr().out
    assert str(pm_env.clone) not in pm_env.applied()  # the next run tries again


def test_post_merge_keeps_the_legacy_table() -> None:
    import post_merge

    assert post_merge.RULES["doctor"] is pc.BASELINE_RULE
