"""`level` can be set when a ticket is created (tracker, `board new`, web API)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient

from symphony.cli import board as board_cli
from symphony.trackers.file import FileBoardTracker, parse_ticket_file
from symphony.workflow import TrackerConfig, WorkflowState
from tests.test_board_cli_subcommands import _make_workflow
from tests.test_webapi import board_dir as board_dir
from tests.test_webapi import client as client


def _tracker(root: Path) -> FileBoardTracker:
    root.mkdir(parents=True, exist_ok=True)
    return FileBoardTracker(
        TrackerConfig(
            kind="file", endpoint="", api_key="", project_slug="",
            active_states=("Todo",), terminal_states=("Done",), board_root=root,
        )
    )


# ---------------------------------------------------------------------------
# tracker
# ---------------------------------------------------------------------------


def test_create_writes_level_and_it_is_read_back(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path / "board")
    path = tracker.create(identifier="LV-1", title="t", level=" S1 ")
    front, _ = parse_ticket_file(path)
    assert front["level"] == "S1"
    issue = tracker.fetch_issue_full_by_id("LV-1")
    assert issue is not None and issue.level == "S1"


@pytest.mark.parametrize("level", [None, "", "   "])
def test_create_without_level_has_no_key(tmp_path: Path, level: str | None) -> None:
    tracker = _tracker(tmp_path / "board")
    path = tracker.create(identifier="LV-2", title="t", level=level)
    front, _ = parse_ticket_file(path)
    assert "level" not in front
    path = tracker.create(identifier="LV-3", title="t")
    assert "level" not in parse_ticket_file(path)[0]


def test_create_validated_and_next_identifier_pass_level(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path / "board")
    identifier, _ = tracker.create_validated(identifier=None, title="a", level="L1")
    other, _ = tracker.create_with_next_identifier("TASK", title="b", level="L2")
    assert tracker.fetch_issue_full_by_id(identifier).level == "L1"  # type: ignore[union-attr]
    assert tracker.fetch_issue_full_by_id(other).level == "L2"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# board new
# ---------------------------------------------------------------------------


def _workflow_with_levels(tmp_path: Path) -> Path:
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text(
        "---\n"
        "tracker:\n  kind: file\n  board_root: ./board\n"
        "levels:\n  S1:\n    max_turns: 20\n  L1:\n    max_turns: 10\n"
        "---\nprompt\n",
        encoding="utf-8",
    )
    return workflow


def test_board_new_level_is_saved(tmp_path: Path) -> None:
    workflow = _workflow_with_levels(tmp_path)
    rc = board_cli.main(["new", "--workflow", str(workflow), "CLI-1", "t", "--level", "S1"])
    assert rc == 0
    front, _ = parse_ticket_file(tmp_path / "board" / "CLI-1.md")
    assert front["level"] == "S1"


def test_board_new_without_level_is_unchanged(tmp_path: Path) -> None:
    workflow = _make_workflow(tmp_path)
    assert board_cli.main(["new", "--workflow", str(workflow), "CLI-2", "t"]) == 0
    assert "level" not in parse_ticket_file(tmp_path / "board" / "CLI-2.md")[0]


def test_board_new_bad_level_format(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _make_workflow(tmp_path)
    rc = board_cli.main(
        ["new", "--workflow", str(workflow), "CLI-3", "t", "--level", "bad name!"]
    )
    assert rc == 1
    assert "invalid level" in capsys.readouterr().err
    assert not (tmp_path / "board" / "CLI-3.md").exists()


def test_board_new_level_too_long(tmp_path: Path) -> None:
    workflow = _make_workflow(tmp_path)
    rc = board_cli.main(
        ["new", "--workflow", str(workflow), "CLI-4", "t", "--level", "x" * 33]
    )
    assert rc == 1


def test_board_new_unknown_level_lists_known(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _workflow_with_levels(tmp_path)
    rc = board_cli.main(["new", "--workflow", str(workflow), "CLI-5", "t", "--level", "Z9"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown level" in err and "'L1'" in err and "'S1'" in err
    assert not (tmp_path / "board" / "CLI-5.md").exists()


def test_board_new_root_checks_format_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "plain"
    assert board_cli.main(["new", "--root", str(root), "R-1", "t", "--level", "Any1"]) == 0
    assert parse_ticket_file(root / "R-1.md")[0]["level"] == "Any1"
    assert board_cli.main(["new", "--root", str(root), "R-2", "t", "--level", "no way"]) == 1
    assert "invalid level" in capsys.readouterr().err


def test_board_new_level_works_from_a_subprocess(tmp_path: Path) -> None:
    import subprocess

    workflow = _workflow_with_levels(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "symphony", "board", "new", "--workflow", str(workflow),
         "SUB-1", "t", "--level", "L1"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert parse_ticket_file(tmp_path / "board" / "SUB-1.md")[0]["level"] == "L1"


# ---------------------------------------------------------------------------
# POST /api/v1/issues
# ---------------------------------------------------------------------------


@pytest.fixture()
def level_client(client: TestClient, board_dir: Path) -> TestClient:
    path = board_dir / "WORKFLOW.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "agent:\n  kind: claude\n",
            "agent:\n  kind: claude\nlevels:\n  S1:\n    max_turns: 20\n  L1:\n    max_turns: 10\n",
        ),
        encoding="utf-8",
    )
    state: WorkflowState = client.stub.workflow_state  # type: ignore[attr-defined]
    cfg, err = state.reload()
    assert err is None and cfg is not None and set(cfg.levels) == {"S1", "L1"}
    return client


async def test_api_create_with_level(level_client: TestClient, board_dir: Path) -> None:
    resp = await level_client.post("/api/v1/issues", json={"title": "x", "level": " S1 "})
    assert resp.status == 201
    identifier = (await resp.json())["identifier"]
    front, _ = parse_ticket_file(board_dir / "kanban" / f"{identifier}.md")
    assert front["level"] == "S1"


async def test_api_create_without_level_is_unchanged(
    level_client: TestClient, board_dir: Path
) -> None:
    resp = await level_client.post("/api/v1/issues", json={"title": "x"})
    assert resp.status == 201
    identifier = (await resp.json())["identifier"]
    assert "level" not in parse_ticket_file(board_dir / "kanban" / f"{identifier}.md")[0]


@pytest.mark.parametrize("level", ["bad name!", "x" * 33, 5, ["S1"]])
async def test_api_create_rejects_bad_level_format(level_client: TestClient, level) -> None:
    resp = await level_client.post("/api/v1/issues", json={"title": "x", "level": level})
    assert resp.status == 400
    assert "level" in (await resp.text())


async def test_api_create_rejects_unknown_level(level_client: TestClient) -> None:
    resp = await level_client.post("/api/v1/issues", json={"title": "x", "level": "Z9"})
    assert resp.status == 400
    text = await resp.text()
    assert "unknown level" in text and "S1" in text


async def test_api_create_any_valid_name_when_no_levels_defined(client: TestClient) -> None:
    resp = await client.post("/api/v1/issues", json={"title": "x", "level": "Free1"})
    assert resp.status == 201
