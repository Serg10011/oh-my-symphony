from symphony.orchestrator.contracts import evaluate_contract
from symphony.orchestrator.factory_contracts import factory_browser_failures, factory_browser_required

import os
from pathlib import Path

import pytest


def test_factory_build_contract_requires_each_supergoal_ledger_pass() -> None:
    result = evaluate_contract(
        "Build", "## Implementation\n\nchanged it", "TASK-1", profile="factory"
    )
    assert result.passed is False
    assert result.missing == [
        "## Full Spec Review",
        "## Edge Case Review",
        "## Adversarial Review",
        "## Test Evidence",
    ]


def test_factory_build_contract_accepts_nonempty_compact_supergoal_ledger() -> None:
    body = """## Implementation

- Added the bounded behavior and regression test.

## Full Spec Review

- Re-read every acceptance criterion; no gap remains.

## Edge Case Review

- Covered the empty-input boundary.

## Adversarial Review

- Tried to disprove completion; no unresolved finding.

## Test Evidence

- `pytest tests/test_api.py -q`: pass.
"""

    assert evaluate_contract("Build", body, "TASK-1", profile="factory").passed


def test_factory_verify_contract_rejects_bare_claimed_pass() -> None:
    result = evaluate_contract(
        "Verify",
        "## Verification\n\n- `pytest`: pass\n- Acceptance criteria: pass",
        "TASK-1",
        profile="factory",
    )
    assert result.passed is False
    assert any("criterion | command | result" in item for item in result.missing)


def test_factory_verify_contract_requires_structured_passing_command_evidence() -> None:
    body = """## Acceptance criteria

- API returns the saved item

## Proof commands

- `pytest tests/test_api.py -q`

## Verification

| criterion | command | result |
| --- | --- | --- |
| API returns the saved item | `pytest tests/test_api.py -q` | pass |
"""

    assert evaluate_contract("Verify", body, "TASK-1", profile="factory").passed


def test_factory_verify_contract_rejects_missing_command_or_failed_result() -> None:
    body = """## Acceptance criteria

- API returns the saved item
- Existing API tests remain green

## Proof commands

- `pytest tests/test_api.py -q`

## Verification

| criterion | command | result |
| --- | --- | --- |
| API returns the saved item | not run | pass |
| Existing API tests remain green | `pytest tests/test_api.py -q` | fail |
"""

    result = evaluate_contract("Verify", body, "TASK-1", profile="factory")

    assert result.passed is False
    assert any("row 1" in item and "command" in item for item in result.missing)
    assert any("row 2" in item and "result" in item for item in result.missing)


def test_factory_verify_contract_requires_every_acceptance_criterion() -> None:
    body = """## Acceptance criteria

- API returns the saved item.
2. Existing API tests remain green.

## Proof commands

- `pytest tests/test_api.py -q`

## Verification

| criterion | command | result |
| --- | --- | --- |
| **API** returns the `saved item`. | `pytest tests/test_api.py -q` | pass |
"""

    result = evaluate_contract("Verify", body, "TASK-1", profile="factory")

    assert result.passed is False
    assert any("Existing API tests remain green." in item for item in result.missing)


def test_factory_verify_contract_matches_normalized_markdown_in_distinct_rows() -> None:
    body = """## Acceptance criteria

- [ ] **API** returns the `saved item`.
2) Existing [API tests](tests/test_api.py) remain green.

## Proof commands

- `pytest tests/test_api.py -q`

## Verification

| criterion | command | result |
| --- | --- | --- |
| API returns the saved item. | `pytest tests/test_api.py -q` | pass |
| Existing API tests remain green. | `pytest tests/test_api.py -q` | pass |
"""

    assert evaluate_contract("Verify", body, "TASK-1", profile="factory").passed


def test_factory_verify_contract_requires_literal_pass_and_no_extra_rows() -> None:
    body = """## Acceptance criteria

- API returns the saved item.

## Proof commands

- `pytest tests/test_api.py -q`

## Verification

| criterion | command | result |
| --- | --- | --- |
| API returns the saved item. | `pytest tests/test_api.py -q` | green |
| An undeclared behavior. | `pytest tests/test_api.py -q` | pass |
"""

    result = evaluate_contract("Verify", body, "TASK-1", profile="factory")

    assert result.passed is False
    assert any("result is not exactly `pass`" in item for item in result.missing)
    assert any("undeclared criterion" in item for item in result.missing)


def test_factory_verify_contract_joins_wrapped_acceptance_criterion() -> None:
    body = """## Acceptance criteria

- `python -m unittest -q test_factory_probe.py` exits 0 and proves
  `factory_value()` returns exactly `factory-ok`.

## Proof commands

- `python -m unittest -q test_factory_probe.py`

## Verification

| criterion | command | result |
| --- | --- | --- |
| `python -m unittest -q test_factory_probe.py` exits 0 and proves `factory_value()` returns exactly `factory-ok`. | `python -m unittest -q test_factory_probe.py` | pass |
"""

    assert evaluate_contract("Verify", body, "TASK-1", profile="factory").passed


def test_factory_verify_contract_rejects_missing_or_empty_acceptance_criteria() -> None:
    verification = """## Proof commands

- `pytest tests/test_api.py -q`

## Verification

| criterion | command | result |
| --- | --- | --- |
| API returns the saved item | `pytest tests/test_api.py -q` | pass |
"""

    missing = evaluate_contract("Verify", verification, "TASK-1", profile="factory")
    empty = evaluate_contract(
        "Verify",
        "## Acceptance criteria\n\n## Verification\n\n"
        "| criterion | command | result |\n"
        "| --- | --- | --- |\n"
        "| API returns the saved item | `pytest tests/test_api.py -q` | pass |",
        "TASK-1",
        profile="factory",
    )

    assert missing.passed is False
    assert empty.passed is False
    assert "## Acceptance criteria" in missing.missing
    assert "## Acceptance criteria" in empty.missing


def test_factory_verify_contract_does_not_reuse_one_row_for_duplicate_criteria() -> (
    None
):
    body = """## Acceptance criteria

- Same behavior is proven.
- Same behavior is proven.

## Proof commands

- `pytest tests/test_api.py -q`

## Verification

| criterion | command | result |
| --- | --- | --- |
| Same behavior is proven. | `pytest tests/test_api.py -q` | pass |
"""

    result = evaluate_contract("Verify", body, "TASK-1", profile="factory")

    assert result.passed is False
    assert any("distinct Verification row" in item for item in result.missing)


def test_advanced_contract_remains_unchanged() -> None:
    result = evaluate_contract(
        "Verify", "## Verification\n\npass", "TASK-1", profile="advanced"
    )
    assert result.passed is False
    assert "## QA Evidence" in result.missing


@pytest.mark.parametrize(
    "command",
    ["echo pass", "pytest -q --disable-warnings", "PYTEST tests/test_api.py -q"],
)
def test_factory_verify_rejects_substituted_declared_proof(command: str) -> None:
    body = (
        "## Acceptance criteria\n\n- API works\n\n"
        "## Proof commands\n\n- `pytest tests/test_api.py -q`\n\n"
        "## Verification\n\n| criterion | command | result |\n| --- | --- | --- |\n"
        f"| API works | `{command}` | pass |\n"
    )
    result = evaluate_contract("Verify", body, "TASK-1", profile="factory")
    assert not result.passed
    assert any("not an exact declared proof" in item for item in result.missing)


def test_factory_verify_rejects_even_declared_no_op_proof() -> None:
    body = (
        "## Acceptance criteria\n\n- API works\n\n## Proof\n\n- `echo pass`\n\n"
        "## Verification\n\n| criterion | command | result |\n| --- | --- | --- |\n"
        "| API works | `echo pass` | pass |\n"
    )
    result = evaluate_contract("Verify", body, "TASK-1", profile="factory")
    assert not result.passed
    assert any("no-op proof" in item for item in result.missing)


def test_factory_verify_allows_declared_pipeline_with_real_check() -> None:
    command = "printf pass | python check_output.py"
    body = (
        "## Acceptance criteria\n\n- Output is validated\n\n"
        f"## Proof commands\n\n- `{command}`\n\n"
        "## Verification\n\n| criterion | command | result |\n| --- | --- | --- |\n"
        f"| Output is validated | `{command}` | pass |\n"
    )
    # Pipe characters need escaping in a Markdown cell; that is a literal shell pipe.
    body = body.replace(f"| `{command}` |", f"| `{command.replace('|', r'\|')}` |")
    assert evaluate_contract("Verify", body, "TASK-1", profile="factory").passed


@pytest.mark.parametrize(("commands", "passed"), [
    ("`pytest tests/test_api.py -q`<br>`python smoke.py`", True),
    ("`pytest tests/test_api.py -q`", False),
    ("`pytest tests/test_api.py -q`; `echo pass`", False),
    ("`pytest tests/test_api.py -q` || echo pass", False),
])
def test_factory_one_criterion_can_require_multiple_exact_proofs(commands: str, passed: bool) -> None:
    body = (
        "## Acceptance criteria\n\n- API works\n\n"
        "## Proof commands\n\n- `pytest tests/test_api.py -q`\n- `python smoke.py`\n\n"
        "## Verification\n\n| criterion | command | result |\n| --- | --- | --- |\n"
        f"| API works | {commands} | pass |\n"
    )
    assert evaluate_contract("Verify", body, "TASK-1", profile="factory").passed is passed


def test_factory_build_cannot_self_promote_with_complete_ledger() -> None:
    body = "\n\n".join(
        f"## {heading}\n\nComplete"
        for heading in (
            "Implementation",
            "Full Spec Review",
            "Edge Case Review",
            "Adversarial Review",
            "Test Evidence",
        )
    )
    assert evaluate_contract(
        "Build", body, "TASK-1", profile="factory", advanced_state="Verify"
    ).passed
    result = evaluate_contract(
        "Build", body, "TASK-1", profile="factory", advanced_state="Done"
    )
    assert not result.passed
    assert any("independent fresh-context turn" in item for item in result.missing)


@pytest.mark.parametrize("source", ["Build", "Verify"])
def test_factory_explicit_blocker_does_not_need_success_proof(source: str) -> None:
    result = evaluate_contract(
        source,
        "## Blocker\n\nTest database credentials unavailable.",
        "TASK-1",
        profile="factory",
        advanced_state="Blocked",
    )
    assert result.passed
    assert not evaluate_contract(
        source, "## Blocker\n", "TASK-1", profile="factory", advanced_state="Blocked"
    ).passed


@pytest.mark.parametrize(
    "report,passed,failed,effects",
    [
        ("missing.md", "1", "0", "0"),
        ("report.md", "0", "0", "0"),
        ("report.md", "1", "1", "0"),
        ("report.md", "1", "0", "-1"),
        ("../outside.md", "1", "0", "0"),
    ],
)
def test_factory_browser_requires_real_contained_report_and_counts(
    tmp_path: Path, report: str, passed: str, failed: str, effects: str
) -> None:
    (tmp_path / "report.md").write_text("SuperQA report", encoding="utf-8")
    body = f"## Browser Verification\n\n| report | passed | failed | side effects |\n| --- | --- | --- | --- |\n| `{report}` | {passed} | {failed} | {effects} |\n"
    assert factory_browser_failures(body, tmp_path, 0.0)


def test_factory_browser_rejects_old_report_and_accepts_fresh_one(
    tmp_path: Path,
) -> None:
    report = tmp_path / "report.md"
    report.write_text("SuperQA report", encoding="utf-8")
    os.utime(report, (10.0, 10.0))
    body = "## Browser Verification\n\n| report | passed | failed | side effects |\n| --- | --- | --- | --- |\n| `report.md` | 2 | 0 | 0 |\n"
    assert factory_browser_failures(body, tmp_path, 20.0)
    os.utime(report, (30.0, 30.0))
    assert not factory_browser_failures(body, tmp_path, 20.0)


@pytest.mark.parametrize(
    "body,skills,required",
    [
        ("No browser surface or attached `superqa` applies.", ("supergoal",), False),
        (
            "No browser surface or attached `superqa` applies.",
            ("supergoal", "superqa"),
            True,
        ),
        ("Browser verification is outside this Python slice.", (), False),
        ("Mention `## Browser Verification` only as documentation.", (), False),
        ("## Browser Verification\n\nClaimed complete.", (), True),
        ("## browser verification:\n", (), True),
        ("## Browser Verification\r\n", (), True),
    ],
)
def test_factory_browser_applicability_uses_routing_or_explicit_heading(
    body: str,
    skills: tuple[str, ...],
    required: bool,
) -> None:
    assert factory_browser_required(body, skills) is required


@pytest.mark.parametrize("routing", ["browser: true", "kind: ui"])
def test_factory_browser_metadata_still_requires_report_after_sync(
    tmp_path: Path,
    routing: str,
) -> None:
    from symphony.factory.sync import sync_wayfinder
    from tests.factory.test_sync import _tracker

    tickets = tmp_path / "wayfinder" / "tickets"
    tickets.mkdir(parents=True)
    (tickets / "001-ui.md").write_text(
        "---\nid: ui\ntitle: Browser flow\nroute: GREENFIELD\n"
        + routing
        + "\n---\n\n## Acceptance criteria\n\n- Flow works.\n\n"
        "## Proof commands\n\n- `python -m unittest -q`\n\n"
        "## Non-goals\n\n- Unrelated work.\n",
        encoding="utf-8",
    )
    tracker = _tracker(tmp_path / "kanban")
    result = sync_wayfinder(tickets.parent, tracker)
    issue = tracker.fetch_issue_full_by_id(result[0].identifier)
    assert issue is not None and "superqa" in issue.skills
    body = (
        issue.description or ""
    ) + "\nNo browser surface or attached `superqa` applies.\n"
    assert factory_browser_required(body, issue.skills)
    assert factory_browser_failures(body, tmp_path, 0.0)
