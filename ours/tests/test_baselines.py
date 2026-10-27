"""Tests for the history of baselines and what depends on it (TASK-S4-25).

project-doctor keeps <board>/.ours-state/baselines.json; check.py compares a
card with the measure of the branching point of its branch (not with the latest
measure); `## Subtask Progress` of a task card stays one section; the rule
"which files change what the tests measure" lives in project_config.

Real temporary git repositories; check.py runs as a subprocess (test_check.Env),
project-doctor as a subprocess (test_project_doctor.Doc).

Run: python -m pytest ours/tests -q
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import test_check as tc
import test_project_doctor as tpd
from test_integrate import BRANCH, TASK, Env as TaskEnv
from test_integrate import card_body, review_card, run_hook, subtask

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the kit is POSIX-only")

OURS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OURS / "hooks"))

import check  # noqa: E402
import post_merge  # noqa: E402
import project_config as pc  # noqa: E402

REAL_SYMPHONY = Path(sys.executable).parent / "symphony"
GHOST = "1" * 40  # a commit the repository does not have


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def rec(sha: str, passed: int, skipped: int = 0, failed: int = 0, branch: str = "main") -> dict:
    return {"base_branch": branch, "base_sha": sha, "passed": passed, "skipped": skipped,
            "failed": failed, "setup_s": 0.1, "tests_s": 0.1, "measured_at": "2026-10-28T00:00:00Z"}


def history(env: tc.Env, *records: dict, version: int = 1) -> None:
    tc.write(env.board / ".ours-state" / "baselines.json",
             json.dumps({"version": version, "records": list(records)}))


def main_head(env: tc.Env) -> str:
    return tc.git(env.code, "rev-parse", "main").strip()


def main_commit(env: tc.Env, name: str, text: str) -> str:
    """A commit on `main` of the code repository (it is checked out there)."""
    tc.write(env.code / name, text)
    tc.git(env.code, "add", "-A")
    tc.git(env.code, "commit", "-q", "-m", f"main: {name}")
    return main_head(env)


def branch_commit(env: tc.Env, branch: str, name: str, text: str) -> str:
    """A commit on `branch` (made in a temporary working copy of it)."""
    tmp = env.tmp / f"tmp-{branch.replace('/', '-')}"
    tc.git(env.code, "worktree", "add", "-q", str(tmp), branch)
    tc.write(tmp / name, text)
    tc.git(tmp, "add", "-A")
    tc.git(tmp, "commit", "-q", "-m", f"{branch}: {name}")
    sha = tc.git(tmp, "rev-parse", "HEAD").strip()
    tc.git(env.code, "worktree", "remove", "--force", str(tmp))
    return sha


def start_from(env: tc.Env, sha: str) -> None:
    """The working copy of the card as if worktree.py had made it from `sha`."""
    tc.git(env.ws, "reset", "-q", "--hard", sha)
    env.record(sha)


def gate(env: tc.Env, passed: int, skipped: int = 0, failed: int = 0, **checks: object):
    env.config(test_command="true", summary=f"passed={passed} skipped={skipped} failed={failed}",
               **checks)
    return env.run("gate")


def outcome(env: tc.Env, *names: str) -> dict:
    found = [e for e in env.log() if e["outcome"] in names]
    assert found, env.log()
    return found[-1]


@pytest.fixture
def env(tmp_path: Path) -> tc.Env:
    return tc.Env(tmp_path)


@pytest.fixture
def doc(tmp_path: Path) -> tpd.Doc:
    return tpd.Doc(tmp_path)


@pytest.fixture
def task_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TaskEnv:
    for key in [k for k in os.environ if k.startswith("SYMPHONY_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("SYMPHONY_WORKFLOW_DIR", str(tmp_path / "projects" / "demo"))
    monkeypatch.setenv("SYMPHONY_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("SYMPHONY_PYTHON", sys.executable)
    made = TaskEnv(tmp_path, enabled=True)
    toml = made.board / "project.toml"
    toml.write_text(toml.read_text().replace('"/bin/symphony"', json.dumps(str(REAL_SYMPHONY))),
                    encoding="utf-8")
    return made


# ---------------------------------------------------------------------------
# check.py: the measure of the branching point (tests 1, 2, 5)
# ---------------------------------------------------------------------------


def test_a_card_started_before_new_tests_is_compared_with_its_own_point(env: tc.Env) -> None:
    a = main_head(env)  # the card starts here (OMS-8 case)
    b = main_commit(env, "tests/test_new.py", "def test_n():\n    pass\n")
    history(env, rec(a, 3), rec(b, 4))
    result = gate(env, passed=3)
    assert result.returncode == 0, result.stdout
    event = outcome(env, "pass")
    assert (event["baseline_match"], event["baseline_sha"], event["baseline_point"]) == ("exact", a, a)


def test_a_dropped_test_is_refused_with_the_measure_and_the_point(env: tc.Env) -> None:
    a = main_head(env)
    b = main_commit(env, "tests/test_new.py", "def test_n():\n    pass\n")
    history(env, rec(a, 3), rec(b, 4))
    result = gate(env, passed=2)
    tc.assert_refused(result, "passed=2 меньше базы 3", a[:12], "точка ответвления", "passed=2")
    fail = outcome(env, "fail")
    assert fail["baseline_match"] == "exact" and fail["baseline_sha"] == a
    more_skipped = gate(env, passed=3, skipped=1)
    tc.assert_refused(more_skipped, "skipped=1 больше базы 0")


def test_a_subtask_is_compared_with_main_of_the_moment_of_the_plan(env: tc.Env) -> None:
    a = main_head(env)
    tc.git(env.code, "branch", "integration/T-9", a)
    work = branch_commit(env, "integration/T-9", "feature.txt", "f\n")  # work of earlier subtasks
    b = main_commit(env, "tests/test_new.py", "def test_n():\n    pass\n")
    history(env, rec(a, 3), rec(b, 4))
    start_from(env, work)  # the subtask starts from the integration branch, not from main
    assert gate(env, passed=3).returncode == 0
    event = outcome(env, "pass")
    assert (event["baseline_match"], event["baseline_sha"], event["baseline_point"]) == ("exact", a, a)
    tc.assert_refused(gate(env, passed=2), "passed=2 меньше базы 3")


# ---------------------------------------------------------------------------
# no measure (test 3) and the ancestor rule (test 4)
# ---------------------------------------------------------------------------


def test_without_a_measure_for_the_point_tests_run_and_only_failed_refuses(env: tc.Env) -> None:
    a = main_head(env)
    c1 = main_commit(env, "tests/test_app.py", "def test_a():\n    assert 1\n")  # a test file changed
    history(env, rec(a, 3))
    start_from(env, c1)
    assert gate(env, passed=1).returncode == 0  # fewer than 3: nothing to compare with
    unknown = outcome(env, "baseline_unknown")
    assert unknown["baseline_point"] == c1 and unknown["base_branch"] == "main"
    assert "между записью" in unknown["reason"] and a[:12] in unknown["reason"]
    assert "tests/test_app.py" in unknown["reason"]
    passed = outcome(env, "pass")
    assert (passed["baseline_match"], passed["baseline_sha"], passed["baseline_point"]) == ("unknown", "", c1)
    tc.assert_refused(gate(env, passed=1, failed=1), "failed=1")


def test_no_record_at_all_for_the_point(env: tc.Env) -> None:
    a = main_head(env)
    tree = tc.git(env.code, "rev-parse", f"{a}^{{tree}}").strip()
    side = tc.git(env.code, "commit-tree", tree, "-p", a, "-m", "side").strip()  # not on main
    history(env, rec(side, 3))
    assert gate(env, passed=1).returncode == 0
    assert outcome(env, "baseline_unknown")["reason"] == "нет записи"


def test_a_skipped_run_does_not_write_baseline_unknown(env: tc.Env) -> None:
    a = main_head(env)
    c1 = main_commit(env, "tests/test_app.py", "def test_a():\n    assert 1\n")
    history(env, rec(a, 3))
    start_from(env, c1)
    tc.write(env.ws / "docs" / "x.md", "doc\n")
    env.commit("docs")
    result = gate(env, passed=1, doc_paths=["docs/**"])
    assert result.returncode == 0 and "only doc paths changed" in result.stdout
    assert not [e for e in env.log() if e["outcome"] == "baseline_unknown"]
    assert outcome(env, "pass")["baseline_match"] == "unknown"


def test_an_older_record_holds_when_only_documents_changed_after_it(env: tc.Env) -> None:
    a = main_head(env)
    c = main_commit(env, "docs/a.md", "just a document\n")
    history(env, rec(GHOST, 99), rec(a, 3))  # a record without a commit here is skipped
    start_from(env, c)
    result = gate(env, passed=2)
    tc.assert_refused(result, "passed=2 меньше базы 3", "ancestor")
    assert gate(env, passed=3).returncode == 0
    event = outcome(env, "pass")
    assert (event["baseline_match"], event["baseline_sha"], event["baseline_point"]) == ("ancestor", a, c)


def test_the_nearest_ancestor_wins(env: tc.Env) -> None:
    a = main_head(env)
    b = main_commit(env, "docs/a.md", "one\n")
    c = main_commit(env, "docs/b.md", "two\n")
    history(env, rec(b, 5), rec(a, 3))  # written in another order than the commits
    start_from(env, c)
    assert gate(env, passed=4).returncode != 0
    assert outcome(env, "fail")["baseline_sha"] == b


@pytest.mark.parametrize("path", ["ours/tests/e2e/fake_claude.py", "pkg/conftest.py", "tests/test_x.py"])
def test_a_changed_test_file_makes_the_older_record_void(env: tc.Env, path: str) -> None:
    a = main_head(env)
    c = main_commit(env, path, "x = 1\n")
    history(env, rec(a, 3))
    start_from(env, c)
    assert gate(env, passed=1).returncode == 0
    assert path in outcome(env, "baseline_unknown")["reason"]


# ---------------------------------------------------------------------------
# modes (test 6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        json.dumps({"version": 2, "records": []}),
        json.dumps({"version": 1, "records": [{"base_branch": "main"}]}),
        json.dumps({"version": 1, "records": [rec("xyz", 3)]}),
        json.dumps({"version": 1, "records": "none"}),
    ],
)
def test_a_damaged_history_refuses_and_never_falls_back_to_baseline_json(env: tc.Env, payload: str) -> None:
    tc.write(env.board / ".ours-state" / "baselines.json", payload)  # baseline.json is fine: passed=1
    result = gate(env, passed=1)
    tc.assert_refused(result, "перезапусти project-doctor")
    assert not env.marker.exists()  # refused before the tests


def test_a_history_without_the_base_branch_refuses(env: tc.Env) -> None:
    history(env, rec(main_head(env), 3, branch="other"))
    tc.assert_refused(gate(env, passed=3), "нет записей для ветки 'main'", "перезапусти project-doctor")


def test_without_a_history_the_single_baseline_decides_as_before(env: tc.Env) -> None:
    assert not (env.board / ".ours-state" / "baselines.json").exists()
    env.baseline(passed=3)
    tc.assert_refused(gate(env, passed=2), "passed=2 меньше базы 3", "перезапусти project-doctor")
    assert gate(env, passed=3).returncode == 0
    event = outcome(env, "pass")
    assert (event["baseline_match"], event["baseline_sha"]) == ("legacy", "0" * 40)
    assert "baseline_point" not in event


def test_a_failed_hook_logs_the_measure_too(env: tc.Env) -> None:
    a = main_head(env)
    history(env, rec(a, 3))
    env.config(test_command="true", summary="passed=2 skipped=0 failed=0")
    assert env.run("hook").returncode == 0
    event = outcome(env, "rollback:In Progress")
    assert (event["baseline_match"], event["baseline_sha"], event["baseline_point"]) == ("exact", a, a)


def test_a_missing_base_branch_is_a_refusal_not_a_skip(env: tc.Env) -> None:
    history(env, rec(main_head(env), 3, branch="gone"))
    env.config(test_command="true", base_ref="gone")
    result = env.run("gate")
    tc.assert_refused(result, "точка ответвления от 'gone' не найдена")


# ---------------------------------------------------------------------------
# project-doctor keeps the history (test 7)
# ---------------------------------------------------------------------------


def test_add_baseline_keeps_one_record_per_commit_and_the_last_fifty() -> None:
    a, b = rec("a" * 40, 1), rec("b" * 40, 2)
    assert pc.add_baseline([a, b], rec("a" * 40, 5)) == [b, rec("a" * 40, 5)]  # replaced, moved to the end
    other = rec("a" * 40, 7, branch="dev")  # the same commit on another branch is another record
    assert len(pc.add_baseline([a], other)) == 2
    records: list[dict] = []
    for n in range(60):
        records = pc.add_baseline(records, rec(f"{n:040x}", n))
    assert len(records) == pc.BASELINES_KEEP == 50
    assert [r["passed"] for r in records] == list(range(10, 60))


def test_read_baselines_tells_what_is_wrong(tmp_path: Path) -> None:
    (tmp_path / ".ours-state").mkdir()
    assert pc.read_baselines(tmp_path) is None
    tc.write(pc.baselines_path(tmp_path), pc.baselines_text([rec("a" * 40, 1)]))
    assert pc.read_baselines(tmp_path) == [rec("a" * 40, 1)]
    for payload in ("[", '{"version": 9, "records": []}', '{"version": 1, "records": [{}]}',
                    json.dumps({"version": 1, "records": [rec("zz", 1)]})):
        tc.write(pc.baselines_path(tmp_path), payload)
        with pytest.raises(pc.ProjectError, match="перезапусти project-doctor"):
            pc.read_baselines(tmp_path)


def history_of(doc: tpd.Doc) -> list[dict]:
    data = json.loads((doc.board / ".ours-state" / "baselines.json").read_text())
    assert data["version"] == 1
    return data["records"]


def test_doctor_adds_a_record_and_keeps_baseline_json_as_before(doc: tpd.Doc) -> None:
    doc.ready()
    result = doc.doctor()
    assert result.returncode == 0, result.stdout + result.stderr
    baseline = json.loads(doc.baseline.read_text())
    assert history_of(doc) == [baseline]
    assert baseline["base_sha"] == tpd.git(doc.code, "rev-parse", "dev")


def test_a_second_measure_of_the_same_commit_is_one_record_a_new_commit_is_the_last(doc: tpd.Doc) -> None:
    doc.ready()
    assert doc.doctor().returncode == 0
    first = history_of(doc)[0]
    assert doc.doctor().returncode == 0
    assert len(history_of(doc)) == 1
    tpd.git(doc.code, "branch", "-f", "dev", "main")  # dev moves to a new commit
    assert doc.doctor().returncode == 0
    records = history_of(doc)
    assert [r["base_sha"] for r in records][0] == first["base_sha"]
    assert records[-1]["base_sha"] == tpd.git(doc.code, "rev-parse", "dev")
    assert len(records) == 2
    assert records[-1] == json.loads(doc.baseline.read_text())


def test_doctor_takes_an_existing_baseline_json_as_the_first_record(doc: tpd.Doc) -> None:
    doc.ready()
    previous = rec("2" * 40, 7, branch="dev")
    tc.write(doc.baseline, json.dumps(previous))
    assert doc.doctor().returncode == 0
    records = history_of(doc)
    assert records[0] == previous and len(records) == 2
    assert records[1]["base_sha"] == tpd.git(doc.code, "rev-parse", "dev")


def test_doctor_does_not_take_a_placeholder_baseline_json(doc: tpd.Doc) -> None:
    doc.ready()
    tc.write(doc.baseline, json.dumps(rec("0" * 40, 7, branch="dev")))
    assert doc.doctor().returncode == 0
    assert len(history_of(doc)) == 1


def test_doctor_starts_a_damaged_history_again(doc: tpd.Doc) -> None:
    doc.ready()
    tc.write(pc.baselines_path(doc.board), "{broken")
    result = doc.doctor()
    assert result.returncode == 0 and "история начинается заново" in result.stdout
    assert len(history_of(doc)) == 1


def test_a_failed_measure_changes_neither_file(doc: tpd.Doc) -> None:
    doc.ready(test="echo 'PIPELINE_TESTS passed=1 skipped=0 failed=1'\n")
    tc.write(doc.baseline, json.dumps(rec("2" * 40, 7, branch="dev")))
    tc.write(pc.baselines_path(doc.board), pc.baselines_text([rec("2" * 40, 7, branch="dev")]))
    before = (doc.baseline.read_bytes(), pc.baselines_path(doc.board).read_bytes())
    assert doc.doctor().returncode != 0
    assert (doc.baseline.read_bytes(), pc.baselines_path(doc.board).read_bytes()) == before


# ---------------------------------------------------------------------------
# ## Subtask Progress in one section (test 8)
# ---------------------------------------------------------------------------


def test_add_to_section_extends_the_last_section_of_that_name() -> None:
    body = "text\n\n## Subtask Progress\n\n0 of 2 merged\n\n## Other\n\nx\n"
    once = check.add_to_section(body, "Subtask Progress", "- A merged (1 of 2)")
    assert once == "text\n\n## Subtask Progress\n\n0 of 2 merged\n\n- A merged (1 of 2)\n\n## Other\n\nx\n"
    twice = check.add_to_section(once, "Subtask Progress", "- B merged (2 of 2)")
    assert "- A merged (1 of 2)\n- B merged (2 of 2)\n\n## Other" in twice
    assert twice.count("## Subtask Progress") == 1


def test_add_to_section_creates_the_section_when_there_is_none() -> None:
    assert check.add_to_section("text\n", "Subtask Progress", "- A") == "text\n\n## Subtask Progress\n\n- A"
    assert check.add_to_section("", "Subtask Progress", "- A") == "## Subtask Progress\n\n- A"
    # an empty section gets the line after a blank one; a `###` subheading stays inside the section
    assert check.add_to_section("## Subtask Progress\n", "Subtask Progress", "- A") == "## Subtask Progress\n\n- A\n"
    nested = "## Subtask Progress\n\n- A\n\n### Notes\n\nn\n\n# Top\n"
    assert check.add_to_section(nested, "Subtask Progress", "- B").endswith("n\n\n- B\n\n# Top\n")


def card_with_sections(board: Path, *sections: str) -> Path:
    card = board / "kanban" / "T-1.md"
    body = "\n".join(f"## {name}\n\n{text}\n" for name, text in sections)
    tc.write(card, "---\nid: T-1\nidentifier: T-1\ntitle: t\nstate: Subtasks\n"
                   f"updated_at: '2020-01-01T00:00:00Z'\n---\n\ngoal\n\n{body}")
    return card


def context(tmp_path: Path) -> check.Context:
    (tmp_path / "ws").mkdir(exist_ok=True)
    return check.Context("hook", tmp_path, tmp_path / "ws", "T-1")


def test_the_line_goes_into_the_last_of_several_sections_through_the_tracker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYMPHONY_PYTHON", sys.executable)
    card = card_with_sections(tmp_path, ("Subtask Progress", "0 of 2 merged"),
                              ("Subtask Progress", "- A merged (1 of 2)"), ("Final Check", "later"))
    assert check.append_note(context(tmp_path), "- B merged (2 of 2)", "Subtask Progress") == "tracker"
    text = card.read_text()
    assert text.count("## Subtask Progress") == 2
    assert "- A merged (1 of 2)\n- B merged (2 of 2)\n\n## Final Check" in text
    assert "2020-01-01" not in text  # updated_at is renewed: the archive timer restarts


def test_the_file_path_does_the_same_transformation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SYMPHONY_PYTHON", raising=False)
    card = card_with_sections(tmp_path, ("Subtask Progress", "0 of 2 merged"), ("Final Check", "later"))
    assert check.append_note(context(tmp_path), "- A merged (1 of 2)", "Subtask Progress") == "file"
    assert check.append_note(context(tmp_path), "- B merged (2 of 2)", "Subtask Progress") == "file"
    assert "## Subtask Progress\n\n0 of 2 merged\n\n- A merged (1 of 2)\n- B merged (2 of 2)\n\n## Final Check" in card.read_text()


@pytest.mark.parametrize("python", [True, False])
def test_other_headings_are_still_new_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python: bool
) -> None:
    if python:
        monkeypatch.setenv("SYMPHONY_PYTHON", sys.executable)
    else:
        monkeypatch.delenv("SYMPHONY_PYTHON", raising=False)
    card = card_with_sections(tmp_path, ("Subtask Progress", "0 of 2 merged"))
    ctx = context(tmp_path)
    check.append_note(ctx, "- first")
    check.append_note(ctx, "- second")
    assert card.read_text().count("## Check Failure") == 2


def test_two_subtasks_write_one_section_in_the_order_of_their_turns(
    task_env: TaskEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    subs = ("OMS-7-1", "OMS-7-2")
    task_env.card(TASK, "\n".join([
        "---", f"id: {TASK}", f"identifier: {TASK}", "title: The task", "state: Subtasks",
        "labels: [role:manager-plan]", "---", "", "The goal.", "",
        "## Plan Accepted", "", "- at: 2026-10-27T00:00:00Z", f"- branch: {BRANCH} (created)",
        "- subtasks: 2", "", *(f"- {ident} — Subtask {ident} — simple" for ident in subs), "",
        "## Subtask Progress", "", "0 of 2 merged", "",
    ]))
    assert task_env.start(TASK, monkeypatch) == 0
    import integrate
    integrate.ensure_integration_branch(task_env.code, BRANCH, "main")
    review_card(task_env, subs[1], state="In Progress")
    subtask(task_env, subs[0], monkeypatch, {"a.txt": "a\n"})
    assert run_hook(task_env, subs[0], monkeypatch) == 0
    body = card_body(task_env, TASK)
    assert f"## Subtask Progress\n\n0 of 2 merged\n\n- {subs[0]} merged (1 of 2)" in body
    assert body.count("## Subtask Progress") == 1


# ---------------------------------------------------------------------------
# the one rule (test 9)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "ours/tests/e2e/fake_claude.py", "pkg/conftest.py", "conftest.py", "tests/a.txt", "a/test_b.py",
    "a/b_test.py", "pyproject.toml", "uv.lock", "package.json",
])
def test_the_rule_catches_what_changes_the_tests(path: str) -> None:
    assert pc.affects_baseline(path)
    assert post_merge.matches("doctor", path)


@pytest.mark.parametrize("path", ["docs/a.md", "README.md", "src/app.py", "ours/hooks/check.py", "ours/README.md"])
def test_the_rule_ignores_the_rest(path: str) -> None:
    assert not pc.affects_baseline(path)
    assert not post_merge.matches("doctor", path)


def test_post_merge_takes_the_rule_from_project_config() -> None:
    assert post_merge.RULES["doctor"] is pc.BASELINE_RULE
