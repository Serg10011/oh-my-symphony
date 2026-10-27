"""Tests of the light path (TASK-S4-16): [checks] doc_paths and subtasks without Document.

check.py on the scenarios of test_check.py (real code repository, board
folder, linked worktree, real check.py subprocess). The end-to-end run of a
subtask and the prompt templates are in test_light_path_e2e.py.

Run: python -m pytest ours/tests -q
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import test_check as tc
from test_check import ID, Env, assert_passed, assert_refused

check = tc.check

pytestmark = pytest.mark.skipif(os.name != "posix", reason="check.py is POSIX-only")

@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


DOCS = ["docs/our/**/*.md"]
SKIPPED_LINE = "tests skipped: only doc paths changed"

SUBTASK_BODY = """\
## QA Evidence

- Full suite: run by the host hook (check.py hook) after Document

## Merge Status

Merge deferred to a human by pipeline rule; no merge performed.
"""


def doc_change(env: Env, *names: str) -> None:
    for name in names or ("docs/our/NOTE.md",):
        tc.write(env.ws / name, "text\n")
    env.commit()


def events(env: Env, mode: str) -> list[dict]:
    return [e for e in env.log() if e["mode"] == mode]


def pass_mark_path(env: Env) -> Path:
    return env.board / ".ours-state" / f"{ID}.pass.json"


# ---------------------------------------------------------------------------
# the pattern
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("docs/our/GLOSSARY.md", True),  # `**` allows zero folders
        ("docs/our/tasks/TASK-X.md", True),
        ("docs/our/a/b/c/d.md", True),
        ("DOCS/OUR/Glossary.MD", True),  # case-insensitive like every pattern
        ("docs/our/x.py", False),
        ("docs/ours.md", False),
        ("docs/ours/x.md", False),
        ("docs/our", False),
        ("CHANGELOG.md", False),
        ("src/docs/our/x.md", False),  # anchored at the root
    ],
)
def test_doc_paths_pattern(path: str, expected: bool) -> None:
    assert check.glob_match(path, "docs/our/**/*.md") is expected


# ---------------------------------------------------------------------------
# the tests are not run
# ---------------------------------------------------------------------------


def test_gate_skips_the_tests_when_only_doc_paths_changed(env: Env) -> None:
    env.config(test_command=f"touch {env.marker}", doc_paths=DOCS)
    doc_change(env, "docs/our/NOTE.md", "docs/our/tasks/TASK-X.md")
    result = env.run("gate")
    assert_passed(result)
    assert not env.marker.exists()
    assert f"{SKIPPED_LINE} (2 files)" in result.stdout
    (event,) = events(env, "gate")
    assert event["outcome"] == "pass" and event["tests"] == "skipped-docs"
    assert event["reason"] == f"{SKIPPED_LINE} (2 files)"


def test_hook_skips_the_tests_and_still_writes_the_pass_mark(env: Env) -> None:
    env.config(test_command=f"touch {env.marker}", doc_paths=DOCS)
    doc_change(env)
    result = env.run("hook")
    assert result.returncode == 0
    assert f"{SKIPPED_LINE} (1 files)" in result.stdout and "PASS" in result.stdout
    assert not env.marker.exists()
    assert env.mv_calls() == []
    assert env.state() == "Human Review"
    mark = json.loads(pass_mark_path(env).read_text())
    assert mark["tree"] == check.worktree_tree(env.ws)
    (event,) = events(env, "hook")
    assert event["outcome"] == "pass" and event["tests"] == "skipped-docs"
    assert event["reason"].startswith(SKIPPED_LINE)


def test_uncommitted_and_untracked_doc_changes_count_too(env: Env) -> None:
    env.config(test_command=f"touch {env.marker}", doc_paths=DOCS)
    tc.write(env.ws / "docs" / "our" / "NEW.md", "untracked\n")
    assert_passed(env.run("gate"))
    assert not env.marker.exists()


def test_a_path_outside_doc_paths_runs_the_tests(env: Env) -> None:
    env.config(test_command=f"touch {env.marker}", doc_paths=DOCS)
    doc_change(env, "docs/our/NOTE.md", "src/other.py")
    result = env.run("gate")
    assert_passed(result)
    assert env.marker.exists() and SKIPPED_LINE not in result.stdout
    assert "tests" not in events(env, "gate")[0]


def test_a_code_file_in_a_docs_folder_runs_the_tests(env: Env) -> None:
    env.config(test_command=f"touch {env.marker}", doc_paths=DOCS)
    doc_change(env, "docs/our/NOTE.md", "docs/our/script.py")
    assert_passed(env.run("hook"))
    assert env.marker.exists()


def test_empty_doc_paths_run_the_tests(env: Env) -> None:
    env.config(test_command=f"touch {env.marker}")  # the default: empty
    doc_change(env)
    assert_passed(env.run("gate"))
    assert env.marker.exists()


def test_no_changes_run_the_tests(env: Env) -> None:
    env.config(test_command=f"touch {env.marker}", doc_paths=DOCS)
    assert_passed(env.run("gate"))
    assert env.marker.exists()
    assert "tests" not in events(env, "gate")[0]


# ---------------------------------------------------------------------------
# the fast checks always run
# ---------------------------------------------------------------------------


def test_a_forbidden_path_is_refused_even_next_to_doc_changes(env: Env) -> None:
    env.config(test_command=f"touch {env.marker}", doc_paths=DOCS + ["WORKFLOW.md"])
    doc_change(env, "docs/our/NOTE.md", "WORKFLOW.md")
    assert_refused(env.run("gate"), "forbidden path changed: WORKFLOW.md")
    assert not env.marker.exists()


def test_a_changed_existing_test_is_refused_even_when_doc_paths_match_it(env: Env) -> None:
    env.config(test_command=f"touch {env.marker}", doc_paths=["tests/**"])
    tc.write(env.ws / "tests" / "test_app.py", "def test_a():\n    pass\n")
    env.commit()
    assert_refused(env.run("gate"), "existing test changed: tests/test_app.py")
    assert not env.marker.exists()


def test_a_missing_card_section_is_refused_for_doc_changes(env: Env) -> None:
    env.config(test_command=f"touch {env.marker}", doc_paths=DOCS)
    env.set_card("Human Review", "## Plan\n\nonly a plan\n")
    doc_change(env)
    assert_refused(env.run("gate"), "card section missing or empty: ## Wiki Updates")
    assert not env.marker.exists()
    assert events(env, "gate")[0]["outcome"] == "fail"


def test_a_refused_hook_rolls_back_and_writes_no_mark_for_doc_changes(env: Env) -> None:
    env.config(test_command=f"touch {env.marker}", doc_paths=DOCS)
    env.set_card("Human Review", "## Plan\n\nonly a plan\n")
    doc_change(env)
    assert env.run("hook").returncode == 0
    assert env.state() == "In Progress"
    assert not env.marker.exists() and not pass_mark_path(env).exists()


# ---------------------------------------------------------------------------
# the merged record (S4-13) and doc_paths together
# ---------------------------------------------------------------------------


def test_the_merged_record_wins_when_the_tree_matches(env: Env) -> None:
    env.config(test_command=f"touch {env.marker}", doc_paths=DOCS)
    doc_change(env)
    tc.put_merged(env)
    assert_passed(env.run("gate"))
    assert not env.marker.exists()
    (event,) = events(env, "gate")
    assert event["tests"] == "skipped" and event["reason"].startswith("tests skipped: tree ")
    assert not tc.merged_file(env).exists()


def test_doc_paths_apply_when_the_merged_tree_differs(env: Env) -> None:
    env.config(test_command=f"touch {env.marker}", doc_paths=DOCS)
    doc_change(env)
    tc.put_merged(env)
    tc.write(env.ws / "docs" / "our" / "LATER.md", "after the merge\n")
    result = env.run("gate")
    assert_passed(result)
    assert not env.marker.exists()
    assert f"{SKIPPED_LINE} (2 files)" in result.stdout
    (event,) = events(env, "gate")
    assert event["tests"] == "skipped-docs"
    assert not tc.merged_file(env).exists()


def test_a_different_tree_outside_doc_paths_still_runs_the_tests(env: Env) -> None:
    env.config(test_command=f"touch {env.marker}", doc_paths=DOCS)
    doc_change(env)
    tc.put_merged(env)
    tc.write(env.ws / "src" / "app.py", "x = 3\n")
    assert_passed(env.run("gate"))
    assert env.marker.exists()
    assert events(env, "gate")[0]["tests"] == "full"
    assert not tc.merged_file(env).exists()


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_doc_paths_must_be_a_list_of_strings(env: Env) -> None:
    env.config(test_command="true", doc_paths="docs/our/**")
    assert_refused(env.run("gate"), "doc_paths")


def test_a_bad_doc_paths_pattern_is_refused(env: Env) -> None:
    env.config(test_command="true", doc_paths=["/"])
    assert_refused(env.run("gate"), "empty glob pattern")


def test_the_new_keys_have_safe_defaults(env: Env) -> None:
    cfg = check.load_config(env.board)
    assert cfg.doc_paths == ()
    assert cfg.worker_required_sections == ("## QA Evidence", "## Merge Status")
    assert cfg.integration is False


def test_a_worker_section_without_the_prefix_is_refused(env: Env) -> None:
    env.config(test_command="true", worker_required_sections=["QA Evidence"])
    assert_refused(env.run("gate"), "must start with '## '")


def test_project_new_template_documents_both_keys() -> None:
    text = (Path(tc.__file__).resolve().parents[1] / "bin" / "project-new").read_text(encoding="utf-8")
    assert '# doc_paths = ["docs/our/**/*.md"]' in text
    assert '# worker_required_sections = ["## QA Evidence", "## Merge Status"]' in text
    assert "CHANGELOG.md" in text


# ---------------------------------------------------------------------------
# subtask: the sections of a card without Document
# ---------------------------------------------------------------------------


def subtask_env(env: Env, *, integration: bool = True, labels: str | None = "[role:worker]",
                request: str | None = "T-0", body: str = SUBTASK_BODY, **keys: object) -> None:
    env.config(test_command=f"touch {env.marker}", **keys)
    if integration:
        toml = env.board / "project.toml"
        toml.write_text(toml.read_text() + "[integration]\nenabled = true\n")
    lines = [f"id: {ID}", f"identifier: {ID}", "title: t", "state: Human Review"]
    if labels is not None:
        lines.append(f"labels: {labels}")
    if request is not None:
        lines.append(f"request: {request}")
    tc.write(env.card, "---\n" + "\n".join(lines) + "\n---\n\n" + body)


def test_a_subtask_needs_the_verify_sections_only(env: Env) -> None:
    subtask_env(env)
    assert_passed(env.run("gate"))
    assert env.marker.exists()  # not a doc change: the full run stays


def test_a_subtask_without_merge_status_is_refused_by_name(env: Env) -> None:
    subtask_env(env, body="## QA Evidence\n\nproof\n")
    assert_refused(env.run("gate"), "card section missing or empty: ## Merge Status")
    assert not env.marker.exists()


def test_a_subtask_does_not_need_the_document_sections(env: Env) -> None:
    subtask_env(env)
    cfg = check.load_config(env.board)
    assert check.card_violations(env.card, cfg) == []
    assert check.is_subtask_card(env.card, cfg)


def test_worker_sections_can_be_configured(env: Env) -> None:
    subtask_env(env, worker_required_sections=["## QA Evidence", "## Merge Status", "## Review"])
    assert_refused(env.run("gate"), "card section missing or empty: ## Review")


def test_the_same_card_with_integration_off_needs_the_ordinary_sections(env: Env) -> None:
    subtask_env(env, integration=False)
    result = env.run("gate")
    assert_refused(result, "## Wiki Updates", "## As-Is -> To-Be Report")
    assert "## Merge Status" not in result.stdout


def test_a_card_without_the_label_needs_the_ordinary_sections(env: Env) -> None:
    subtask_env(env, labels=None)
    assert_refused(env.run("gate"), "## Wiki Updates")
    subtask_env(env, labels="[bug]")
    assert_refused(env.run("gate"), "## Wiki Updates")


def test_a_card_without_request_needs_the_ordinary_sections(env: Env) -> None:
    subtask_env(env, request=None)
    assert_refused(env.run("gate"), "## Wiki Updates")


def test_the_worker_label_is_the_configured_one_and_case_insensitive(env: Env) -> None:
    subtask_env(env, labels="[Role:Worker]")
    assert_passed(env.run("gate"))
    toml = env.board / "project.toml"
    toml.write_text(toml.read_text() + 'worker_label = "kind:sub"\n')
    assert_refused(env.run("gate"), "## Wiki Updates")  # `role:worker` is no longer the label
    subtask_env(env, labels="[kind:sub]")
    toml.write_text(toml.read_text() + 'worker_label = "kind:sub"\n')
    assert_passed(env.run("gate"))


def test_a_subtask_with_ordinary_sections_still_passes_if_it_has_the_verify_ones(env: Env) -> None:
    subtask_env(env, body=tc.GOOD_BODY + "\n" + SUBTASK_BODY)
    assert_passed(env.run("gate"))


def test_an_unreadable_front_matter_means_an_ordinary_card(env: Env) -> None:
    subtask_env(env, labels="not-a-list")
    assert_refused(env.run("gate"), "## Wiki Updates")
