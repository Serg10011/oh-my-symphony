from pathlib import Path

import pytest

from symphony.factory.wayfinder import parse_wayfinder_ticket


def test_parser_reads_route_dependencies_and_explicit_overlays(tmp_path: Path) -> None:
    path = tmp_path / "001-dashboard.md"
    path.write_text(
        """---
id: dashboard
title: Build the dashboard
route: GREENFIELD
blocked_by: [foundation]
skills: [superdesign, superqa, superdesign]
---

## Acceptance criteria

- WHEN the page opens THEN it SHALL show current status.

## Proof commands

- `npm test`

## Non-goals

- Unrelated work.
""",
        encoding="utf-8",
    )

    ticket = parse_wayfinder_ticket(path)

    assert ticket.title == "Build the dashboard"
    assert ticket.route == "GREENFIELD"
    assert ticket.key == "dashboard"
    assert ticket.blocked_by == ("foundation",)
    assert ticket.skills == ("supergoal", "superdesign", "superqa")
    assert "Acceptance criteria" in ticket.description


def test_parser_accepts_path_safe_custom_skill_names(tmp_path: Path) -> None:
    path = tmp_path / "custom.md"
    path.write_text(
        """---
id: custom
title: Run a custom check
route: LEGACY
blocked_by: []
skills: [custom-check.v2]
---

## Acceptance criteria

- The custom check runs.

## Proof commands

- `pytest`

## Non-goals

- Unrelated work.
""",
        encoding="utf-8",
    )

    assert parse_wayfinder_ticket(path).skills == ("supergoal", "custom-check.v2")


@pytest.mark.parametrize("name", ["../escape", "/absolute", "nested/skill"])
def test_parser_rejects_path_unsafe_custom_skill_names(
    tmp_path: Path, name: str
) -> None:
    path = tmp_path / "unsafe.md"
    path.write_text(
        f"""---
id: unsafe
title: Reject an unsafe skill
route: LEGACY
blocked_by: []
skills: [{name}]
---

## Acceptance criteria

- Unsafe paths are rejected.

## Proof commands

- `pytest`

## Non-goals

- None.
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid skill name"):
        parse_wayfinder_ticket(path)


def test_parser_requires_title_route_and_acceptance(tmp_path: Path) -> None:
    path = tmp_path / "bad.md"
    path.write_text("---\nid: bad\ntitle: Incomplete\n---\n", encoding="utf-8")

    try:
        parse_wayfinder_ticket(path)
    except ValueError as exc:
        assert "Route" in str(exc)
    else:
        raise AssertionError("invalid Wayfinder ticket was accepted")


def test_overlay_routing_uses_metadata_not_title_heuristics(tmp_path: Path) -> None:
    path = tmp_path / "ui.md"
    path.write_text(
        """---
id: ui
title: Design a polished dashboard UI
route: GREENFIELD
blocked_by: []
skills: []
---

## Acceptance criteria

- The dashboard is usable.

## Proof

- `pytest -q`

## Non-goals

- Unrelated work.
""",
        encoding="utf-8",
    )

    assert parse_wayfinder_ticket(path).skills == ("supergoal",)


def test_overlay_routing_infers_ui_browser_and_product_research_metadata(
    tmp_path: Path,
) -> None:
    ui = tmp_path / "ui.md"
    ui.write_text(
        """---
id: ui
title: Build the dashboard
route: GREENFIELD
kind: design
browser: false
blocked_by: []
skills: []
---

## Acceptance criteria

- The dashboard is usable.

## Proof

- `playwright-cli snapshot`

## Non-goals

- Unrelated work.
""",
        encoding="utf-8",
    )
    research = tmp_path / "research.md"
    research.write_text(
        """---
id: research
title: Validate customer demand
route: LEGACY
kind: research
blocked_by: []
skills: [superqa]
---

## Acceptance criteria

- Customer evidence is recorded.

## Proof

- `python scripts/check-evidence.py`

## Non-goals

- Unrelated work.
""",
        encoding="utf-8",
    )
    browser = tmp_path / "browser.md"
    browser.write_text(
        """---
id: browser
title: Verify the existing flow
route: LEGACY
kind: qa
browser: false
blocked_by: []
skills: []
---

## Acceptance criteria

- The browser flow passes.

## Proof

- `playwright-cli snapshot`

## Non-goals

- Unrelated work.
""",
        encoding="utf-8",
    )

    assert parse_wayfinder_ticket(ui).skills == (
        "supergoal",
        "superdesign",
        "superqa",
    )
    assert parse_wayfinder_ticket(research).skills == (
        "supergoal",
        "superpm",
        "superqa",
    )
    assert parse_wayfinder_ticket(browser).skills == ("supergoal", "superqa")


def test_overlay_metadata_types_and_values_are_validated(tmp_path: Path) -> None:
    invalid_kind = tmp_path / "kind.md"
    invalid_kind.write_text(
        """---
id: invalid-kind
title: Invalid kind
route: LEGACY
kind: backend
---

## Acceptance criteria

- It is rejected.

## Proof

- `pytest tests/factory/test_wayfinder.py`

## Non-goals

- Unrelated work.
""",
        encoding="utf-8",
    )
    invalid_browser = tmp_path / "browser-type.md"
    invalid_browser.write_text(
        """---
id: invalid-browser
title: Invalid browser marker
route: LEGACY
browser: "yes"
---

## Acceptance criteria

- It is rejected.

## Proof

- `pytest tests/factory/test_wayfinder.py`

## Non-goals

- Unrelated work.
""",
        encoding="utf-8",
    )

    for path, message in (
        (invalid_kind, "kind must be one of"),
        (invalid_browser, "browser must be a boolean"),
    ):
        try:
            parse_wayfinder_ticket(path)
        except ValueError as exc:
            assert message in str(exc)
        else:
            raise AssertionError(f"invalid metadata was accepted: {path}")


def test_delivery_ticket_rejects_non_delivery_supergoal_route(tmp_path: Path) -> None:
    path = tmp_path / "review.md"
    path.write_text(
        """---
id: review
title: Review only
route: REVIEW-ONLY
---

## Acceptance criteria

- The review is written.

## Proof

- Read the report.

## Non-goals

- Product code.
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported Route"):
        parse_wayfinder_ticket(path)


@pytest.mark.parametrize(
    ("acceptance", "proof", "error"),
    [("", "- `pytest`", "Acceptance criteria"),
     ("Prose only", "- `pytest`", "Acceptance criteria"),
     ("- [ ]", "- `pytest`", "Acceptance criteria"),
     ("- Verified", "", "Proof commands"),
     ("- Verified", "- Manual check", "Proof commands"),
     ("- Verified", "- `   `", "Proof commands")],
)
def test_parser_rejects_empty_acceptance_or_non_command_proof(
    tmp_path: Path, acceptance: str, proof: str, error: str,
) -> None:
    path = tmp_path / "empty.md"
    path.write_text(
        "---\nid: empty\ntitle: Empty declaration\nroute: LEGACY\n---\n"
        f"## Acceptance criteria\n{acceptance}\n## Proof commands\n{proof}\n## Non-goals\n- None\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=error):
        parse_wayfinder_ticket(path)


def test_parser_preserves_multiple_unicode_proofs_and_numbered_acceptance(tmp_path: Path) -> None:
    from symphony.factory.wayfinder import factory_acceptance_items, factory_proof_commands

    path = tmp_path / "unicode.md"
    body = (
        "## Acceptance criteria\n1. [ ] 한글 결과를 검증한다.\n"
        "   Preserve the complete statement.\n"
        "## Proof\n- `python 검증.py --name \"값\"`\n- `npm run test && npm run build`\n"
        "## Non-goals\n- None\n"
    )
    path.write_text("---\nid: unicode\ntitle: Unicode proof\nroute: LEGACY\n---\n" + body, encoding="utf-8")
    ticket = parse_wayfinder_ticket(path)
    assert factory_acceptance_items(ticket.description) == ["한글 결과를 검증한다. Preserve the complete statement."]
    assert factory_proof_commands(ticket.description) == [
        'python 검증.py --name "값"', "npm run test && npm run build",
    ]
