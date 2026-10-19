"""Tests for the file-board helper CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

from symphony.cli import board as board_cli
from symphony.workflow import SUPPORTED_AGENT_KINDS
from symphony.trackers.file import parse_ticket_file


@pytest.mark.parametrize("root_override", [False, True])
def test_board_new_and_update_accept_workflow_profiles(
    tmp_path: Path, root_override: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text(
        "---\ntracker:\n  kind: file\n  board_root: ./board\n"
        "agent:\n  profiles:\n    local:\n      command: cat\n---\nprompt\n",
        encoding="utf-8",
    )
    root = tmp_path / ("override" if root_override else "board")
    options = ["--workflow", str(workflow)]
    if root_override:
        options += ["--root", str(root)]
    assert board_cli.main(["new", *options, "T-1", "profile", "--agent", "local"]) == 0
    ticket = root / "T-1.md"
    assert parse_ticket_file(ticket)[0]["agent"] == {"kind": "local"}
    assert board_cli.main(["update", *options, "T-1", "--agent", "codex"]) == 0
    assert parse_ticket_file(ticket)[0]["agent"] == {"kind": "codex"}
    assert board_cli.main(["update", *options, "T-1", "--agent-kind", "local"]) == 0
    assert parse_ticket_file(ticket)[0]["agent"] == {"kind": "local"}

    before = ticket.read_bytes()
    assert board_cli.main(["update", *options, "T-1", "--agent", "unknown"]) == 1
    error = capsys.readouterr().err
    assert "unknown agent kind 'unknown'" in error
    assert "local" in error and "codex" in error
    assert ticket.read_bytes() == before


def test_board_new_without_workflow_rejects_unknown_agent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert board_cli.main([
        "new", "T-1", "profile", "--root", str(tmp_path / "board"),
        "--workflow", str(tmp_path / "missing.md"), "--agent", "unknown",
    ]) == 1
    error = capsys.readouterr().err
    assert "expected one of" in error
    assert all(kind in error for kind in SUPPORTED_AGENT_KINDS)
    assert not (tmp_path / "board" / "T-1.md").exists()


@pytest.mark.parametrize("agent_kind", sorted(SUPPORTED_AGENT_KINDS))
def test_board_new_root_overrides_existing_workflow(
    tmp_path: Path, agent_kind: str
) -> None:
    workflow_board = tmp_path / "workflow-board"
    override_board = tmp_path / "override-board"
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text(
        "\n".join(
            [
                "---",
                "tracker:",
                "  kind: file",
                "  board_root: ./workflow-board",
                "---",
                "prompt",
            ]
        ),
        encoding="utf-8",
    )

    rc = board_cli.main(
        [
            "new",
            "--workflow",
            str(workflow),
            "--root",
            str(override_board),
            "CLI-ROOT",
            "Root override",
            "--agent-kind",
            agent_kind,
        ]
    )

    assert rc == 0
    ticket = override_board / "CLI-ROOT.md"
    assert ticket.exists()
    assert f"kind: {agent_kind}" in ticket.read_text(encoding="utf-8")
    assert not (workflow_board / "CLI-ROOT.md").exists()
