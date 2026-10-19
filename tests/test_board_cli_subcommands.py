"""`symphony board` subcommand coverage beyond the new --root override.

`test_board_cli.py` pinned the cross-agent `new --root` override. This
file walks the rest of the surface:

  * init       seeds a sample ticket; idempotent on rerun.
  * ls         filters by --state (case-insensitive).
  * new        --workflow path picks board_root from WORKFLOW.md.
  * new        rejects unsupported agent-kind after loading the workflow.
  * mv         transitions a ticket between states.
  * mv         non-zero exit when the ticket is missing.
  * show       prints front-matter + body.
  * show       non-zero exit when the ticket is missing.

Each test isolates to a tmp_path so they're hermetic.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from symphony.cli import board as board_cli


def _make_workflow(tmp_path: Path, board_dir: str = "board") -> Path:
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text(
        "\n".join(
            [
                "---",
                "tracker:",
                "  kind: file",
                f"  board_root: ./{board_dir}",
                "---",
                "prompt",
            ]
        ),
        encoding="utf-8",
    )
    return workflow


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_init_creates_board_dir_and_sample_ticket(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    board = tmp_path / "fresh-board"
    rc = board_cli.main(["init", str(board)])
    assert rc == 0
    assert (board / "DEMO-001.md").exists()
    captured = capsys.readouterr()
    assert "initialized board at" in captured.out


def test_init_is_idempotent_when_sample_already_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    board = tmp_path / "fresh-board"
    board_cli.main(["init", str(board)])
    capsys.readouterr()  # discard first
    rc = board_cli.main(["init", str(board)])
    assert rc == 0
    assert "already initialized" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# new + ls + mv + show happy path against a workflow-rooted board
# ---------------------------------------------------------------------------


def test_new_uses_workflow_board_root_when_no_root_override(tmp_path: Path) -> None:
    workflow = _make_workflow(tmp_path, "my-board")
    rc = board_cli.main(
        [
            "new",
            "--workflow",
            str(workflow),
            "TKT-1",
            "first ticket",
            "--priority",
            "1",
            "--labels",
            "alpha,beta",
        ]
    )
    assert rc == 0
    ticket = tmp_path / "my-board" / "TKT-1.md"
    assert ticket.exists()
    content = ticket.read_text(encoding="utf-8")
    assert "TKT-1" in content
    assert "priority: 1" in content
    # comma-split labels propagated.
    assert "alpha" in content and "beta" in content


def test_new_rejects_unknown_agent_kind(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _make_workflow(tmp_path)
    assert board_cli.main(
            [
                "new",
                "--workflow",
                str(workflow),
                "TKT-2",
                "x",
                "--agent-kind",
                "totally-not-an-agent",
            ]
        ) == 1
    error = capsys.readouterr().err
    assert "unknown agent kind 'totally-not-an-agent'" in error
    assert "expected one of" in error
    assert "codex" in error
    assert not (tmp_path / "board" / "TKT-2.md").exists()


def test_ls_filters_by_state_case_insensitively(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _make_workflow(tmp_path)
    board_cli.main(["new", "--workflow", str(workflow), "TKT-A", "a"])
    board_cli.main(
        ["new", "--workflow", str(workflow), "TKT-B", "b", "--state", "In Progress"]
    )
    capsys.readouterr()  # discard new prints

    rc = board_cli.main(["ls", "--workflow", str(workflow), "--state", "in progress"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "TKT-B" in out
    assert "TKT-A" not in out


def test_ls_prints_empty_marker_when_no_tickets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _make_workflow(tmp_path, "empty-board")
    # Create the board dir but no tickets.
    (tmp_path / "empty-board").mkdir()
    rc = board_cli.main(["ls", "--workflow", str(workflow)])
    assert rc == 0
    assert "no tickets" in capsys.readouterr().out


def test_mv_transitions_ticket_and_prints_arrow(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _make_workflow(tmp_path)
    board_cli.main(["new", "--workflow", str(workflow), "TKT-3", "x"])
    capsys.readouterr()
    rc = board_cli.main(["mv", "--workflow", str(workflow), "TKT-3", "In Progress"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "TKT-3 -> In Progress" in out
    ticket = (tmp_path / "board" / "TKT-3.md").read_text(encoding="utf-8")
    assert "state: In Progress" in ticket


def test_mv_returns_nonzero_when_ticket_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _make_workflow(tmp_path)
    (tmp_path / "board").mkdir()
    rc = board_cli.main(["mv", "--workflow", str(workflow), "DOES-NOT-EXIST", "Done"])
    assert rc == 1
    assert "error:" in capsys.readouterr().err.lower()


def test_show_prints_front_matter_and_body(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _make_workflow(tmp_path)
    board_cli.main(
        [
            "new",
            "--workflow",
            str(workflow),
            "TKT-4",
            "show me",
            "--priority",
            "2",
            "--labels",
            "x,y",
            "--description",
            "body line one",
        ]
    )
    capsys.readouterr()
    rc = board_cli.main(["show", "--workflow", str(workflow), "TKT-4"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "# TKT-4" in out
    assert "title: show me" in out
    assert "priority: 2" in out
    # Labels list normalized into a comma-joined display.
    assert "x" in out and "y" in out
    # Body printed after a blank line separator.
    assert "body line one" in out


@pytest.mark.parametrize("state", ["Done", "done", "DONE", "dOnE", " Done ", "\tDone\t"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_show_done_qualifies_delivery_without_changing_ticket(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], state: str, newline: str
) -> None:
    workflow = _make_workflow(tmp_path)
    board = tmp_path / "board"
    board.mkdir()
    ticket = board / "TKT-4.md"
    original = (
        "---\n# Preserve this comment.\nidentifier: TKT-4\ntitle: show me\n"
        f'state: "{state}"\n'
        "priority: 2\nlabels: [x, y]\nrequest: REQ-1\nagent: {kind: codex}\n"
        "---\n\n## Proof\n\nKeep  two spaces.\n"
    ).replace("\n", newline).encode()
    ticket.write_bytes(original)

    assert board_cli.main(["show", "--workflow", str(workflow), "TKT-4"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == (
        f"# TKT-4 ({state})\n"
        "title: show me\npriority: 2\nlabels: x, y\nrequest: REQ-1\nagent: codex\n"
        "delivery: Done alone does not confirm target merge or workspace cleanup.\n"
        "\n## Proof\n\nKeep  two spaces.\n"
    )
    assert ticket.read_bytes() == original


@pytest.mark.parametrize(("state_yaml", "display_state"), [
    ("state: Verify", "Verify"),
    ("state: Blocked", "Blocked"),
    ("state: Human Review", "Human Review"),
    ("state: undone", "undone"),
    ("", "?"),
    ('state: ""', ""),
    ("state: null", "None"),
    ("state: 42", "42"),
    ("state: false", "False"),
    ("state: [Done]", "['Done']"),
    ("state: {value: Done}", "{'value': 'Done'}"),
])
def test_show_other_states_preserves_output_and_ticket(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], state_yaml: str, display_state: str
) -> None:
    workflow = _make_workflow(tmp_path)
    board = tmp_path / "board"
    board.mkdir()
    ticket = board / "TKT-4.md"
    original = (
        "---\n# Preserve this comment.\nidentifier: TKT-4\ntitle: show me\n"
        f"{state_yaml}\n"
        "priority: 2\nlabels: [x, y]\nrequest: REQ-1\nagent_kind: codex\n"
        "---\n\n## Proof\n\nKeep  two spaces.\n"
    ).replace("\n", "\r\n").encode()
    ticket.write_bytes(original)

    assert board_cli.main(["show", "--workflow", str(workflow), "TKT-4"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == (
        f"# TKT-4 ({display_state})\n"
        "title: show me\npriority: 2\nlabels: x, y\nrequest: REQ-1\nagent: codex\n"
        "\n## Proof\n\nKeep  two spaces.\n"
    )
    assert ticket.read_bytes() == original


def test_show_returns_nonzero_when_ticket_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _make_workflow(tmp_path)
    (tmp_path / "board").mkdir()
    rc = board_cli.main(["show", "--workflow", str(workflow), "MISSING"])
    assert rc == 1
    assert "not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# new — structured creation flags + DAG validation
# ---------------------------------------------------------------------------


def test_new_writes_blocked_by_request_and_repeatable_labels(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _make_workflow(tmp_path)
    board_cli.main(["new", "--workflow", str(workflow), "DAG-1", "root"])
    rc = board_cli.main(
        [
            "new",
            "--workflow",
            str(workflow),
            "DAG-2",
            "child",
            "--blocked-by",
            "DAG-1",
            "--request",
            "REQ-1",
            "--label",
            "alpha",
            "--label",
            "beta",
        ]
    )
    assert rc == 0
    content = (tmp_path / "board" / "DAG-2.md").read_text(encoding="utf-8")
    assert "- DAG-1" in content
    assert "request: REQ-1" in content
    assert "alpha" in content and "beta" in content


def test_new_reads_description_from_file_and_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = _make_workflow(tmp_path)
    desc = tmp_path / "desc.md"
    desc.write_text("Body from file.", encoding="utf-8")
    rc = board_cli.main(
        ["new", "--workflow", str(workflow), "DESC-1", "t", "--description-file", str(desc)]
    )
    assert rc == 0
    assert "Body from file." in (tmp_path / "board" / "DESC-1.md").read_text(
        encoding="utf-8"
    )

    monkeypatch.setattr("sys.stdin", io.StringIO("Body from stdin."))
    rc = board_cli.main(
        ["new", "--workflow", str(workflow), "DESC-2", "t", "--description-file", "-"]
    )
    assert rc == 0
    assert "Body from stdin." in (tmp_path / "board" / "DESC-2.md").read_text(
        encoding="utf-8"
    )


def test_new_rejects_description_and_description_file_together(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _make_workflow(tmp_path)
    rc = board_cli.main(
        [
            "new",
            "--workflow",
            str(workflow),
            "DESC-3",
            "t",
            "--description",
            "a",
            "--description-file",
            "b.md",
        ]
    )
    assert rc == 1
    assert "not both" in capsys.readouterr().err


def test_new_rejects_unknown_blocked_by_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _make_workflow(tmp_path)
    rc = board_cli.main(
        ["new", "--workflow", str(workflow), "DAG-3", "x", "--blocked-by", "GHOST-1"]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown blocked_by" in err and "GHOST-1" in err
    assert not (tmp_path / "board" / "DAG-3.md").exists()


def test_new_rejects_duplicate_identifier(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _make_workflow(tmp_path)
    board_cli.main(["new", "--workflow", str(workflow), "DUP-1", "first"])
    capsys.readouterr()
    rc = board_cli.main(["new", "--workflow", str(workflow), "DUP-1", "second"])
    assert rc == 1
    assert "already exists" in capsys.readouterr().err


def test_new_rejects_state_outside_tracker_states(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _make_workflow(tmp_path)
    rc = board_cli.main(
        ["new", "--workflow", str(workflow), "ST-1", "x", "--state", "Nope"]
    )
    assert rc == 1
    assert "unknown state" in capsys.readouterr().err


def test_new_rejects_cycle_and_prints_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _make_workflow(tmp_path)
    board = tmp_path / "board"
    board.mkdir()
    # CYC-1 already (danglingly) depends on CYC-2; creating CYC-2 <- CYC-1
    # would close the loop.
    (board / "CYC-1.md").write_text(
        "---\nid: CYC-1\ntitle: a\nstate: Todo\nblocked_by: [CYC-2]\n---\n",
        encoding="utf-8",
    )
    rc = board_cli.main(
        ["new", "--workflow", str(workflow), "CYC-2", "b", "--blocked-by", "CYC-1"]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "cycle" in err
    assert "CYC-2 -> CYC-1 -> CYC-2" in err
    assert not (board / "CYC-2.md").exists()


# ---------------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------------


def test_graph_prints_topological_indented_dag_with_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _make_workflow(tmp_path)
    board_cli.main(["new", "--workflow", str(workflow), "G-1", "root"])
    board_cli.main(
        ["new", "--workflow", str(workflow), "G-2", "mid", "--blocked-by", "G-1"]
    )
    board_cli.main(
        ["new", "--workflow", str(workflow), "G-3", "leaf", "--blocked-by", "G-2"]
    )
    board_cli.main(["mv", "--workflow", str(workflow), "G-1", "Cancelled"])
    board = tmp_path / "board"
    (board / "DANG-1.md").write_text(
        "---\nid: DANG-1\ntitle: dangling\nstate: Todo\nblocked_by: [GHOST-9]\n---\n",
        encoding="utf-8",
    )
    capsys.readouterr()

    rc = board_cli.main(["graph", "--workflow", str(workflow)])

    assert rc == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert "G-1 Cancelled root" in lines
    assert "  G-2 Todo mid <- G-1" in lines
    assert "    G-3 Todo leaf <- G-2" in lines
    assert "WARN DANG-1: blocked_by GHOST-9 not on board" in lines
    assert "WARN G-2: blocker G-1 is Cancelled" in lines
    # Topological: blockers print before their dependents.
    assert lines.index("G-1 Cancelled root") < lines.index("  G-2 Todo mid <- G-1")


def test_graph_filters_by_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _make_workflow(tmp_path)
    board_cli.main(
        ["new", "--workflow", str(workflow), "R-1", "in", "--request", "REQ-7"]
    )
    board_cli.main(["new", "--workflow", str(workflow), "R-2", "out"])
    capsys.readouterr()

    rc = board_cli.main(["graph", "--workflow", str(workflow), "--request", "REQ-7"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "R-1" in out
    assert "R-2" not in out


def test_graph_exits_nonzero_and_prints_cycle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _make_workflow(tmp_path)
    board = tmp_path / "board"
    board.mkdir()
    (board / "CYC-1.md").write_text(
        "---\nid: CYC-1\ntitle: a\nstate: Todo\nblocked_by: [CYC-2]\n---\n",
        encoding="utf-8",
    )
    (board / "CYC-2.md").write_text(
        "---\nid: CYC-2\ntitle: b\nstate: Todo\nblocked_by: [CYC-1]\n---\n",
        encoding="utf-8",
    )

    rc = board_cli.main(["graph", "--workflow", str(workflow)])

    assert rc == 1
    err = capsys.readouterr().err
    assert "dependency cycle" in err
    assert "CYC-1" in err and "CYC-2" in err


# ---------------------------------------------------------------------------
# F-03 — identifier validation (path escape, shell metacharacters, length)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../evil",
        "../escape",
        "sub/dir",
        "back\\slash",
        "A 5; echo pwned",
        "",
        "   ",
        "1-leading-digit",
        "TKT.1",
        "A" * 65,
    ],
)
def test_new_rejects_malformed_identifier(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], bad_id: str
) -> None:
    workflow = _make_workflow(tmp_path)
    rc = board_cli.main(["new", "--workflow", str(workflow), bad_id, "escape"])
    assert rc == 1
    assert "must match" in capsys.readouterr().err


def test_new_path_escape_writes_nothing_outside_board_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exact probe from the review: `board new ../../evil` must not write."""
    board = tmp_path / "nested" / "board"
    board.mkdir(parents=True)
    rc = board_cli.main(["new", "--root", str(board), "../../evil", "escape"])
    assert rc == 1
    assert not (tmp_path / "evil.md").exists()
    assert not (tmp_path / "nested" / "evil.md").exists()
    assert list(board.glob("*.md")) == []
    assert "must match" in capsys.readouterr().err


def test_new_rejects_malformed_blocked_by_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _make_workflow(tmp_path)
    board_cli.main(["new", "--workflow", str(workflow), "TKT-1", "root"])
    capsys.readouterr()
    rc = board_cli.main(
        [
            "new",
            "--workflow",
            str(workflow),
            "TKT-2",
            "child",
            "--blocked-by",
            "../../evil",
        ]
    )
    assert rc == 1
    assert "must match" in capsys.readouterr().err


def test_tracker_create_rejects_malformed_identifier_directly(tmp_path: Path) -> None:
    """Defence in depth: the tracker refuses even when the CLI is bypassed."""
    from symphony.errors import BoardDependencyError
    from symphony.trackers.file import FileBoardTracker

    board = tmp_path / "board"
    board.mkdir()
    tracker = FileBoardTracker(board_cli._tracker_from_root(board))
    with pytest.raises(BoardDependencyError):
        tracker.create(identifier="../../evil", title="escape")
    assert not (tmp_path / "evil.md").exists()


def test_tracker_update_fields_rejects_malformed_identifier(tmp_path: Path) -> None:
    from symphony.errors import BoardDependencyError
    from symphony.trackers.file import FileBoardTracker

    board = tmp_path / "board"
    board.mkdir()
    tracker = FileBoardTracker(board_cli._tracker_from_root(board))
    tracker.create(identifier="TKT-1", title="ok")
    with pytest.raises(BoardDependencyError):
        tracker.update_fields("../TKT-1", title="hijacked")


# ---------------------------------------------------------------------------
# F-09 — `symphony board update` (the write verb the prompts already assume)
# ---------------------------------------------------------------------------


def test_update_adds_a_blocker_keeping_the_existing_ones(tmp_path: Path) -> None:
    workflow = _make_workflow(tmp_path)
    board_cli.main(["new", "--workflow", str(workflow), "TKT-1", "root"])
    board_cli.main(["new", "--workflow", str(workflow), "BUG-1", "bug"])
    board_cli.main(["new", "--workflow", str(workflow), "BUG-2", "bug two"])
    board_cli.main(
        [
            "new",
            "--workflow",
            str(workflow),
            "TKT-2",
            "child",
            "--blocked-by",
            "TKT-1",
        ]
    )

    rc = board_cli.main(
        [
            "update",
            "--workflow",
            str(workflow),
            "TKT-2",
            "--add-blocked-by",
            "BUG-1",
            "--add-blocked-by",
            "BUG-2",
        ]
    )

    assert rc == 0
    content = (tmp_path / "board" / "TKT-2.md").read_text(encoding="utf-8")
    assert "TKT-1" in content and "BUG-1" in content and "BUG-2" in content


def test_update_replaces_the_blocker_list(tmp_path: Path) -> None:
    workflow = _make_workflow(tmp_path)
    board_cli.main(["new", "--workflow", str(workflow), "TKT-1", "root"])
    board_cli.main(["new", "--workflow", str(workflow), "TKT-3", "other"])
    board_cli.main(
        ["new", "--workflow", str(workflow), "TKT-2", "child", "--blocked-by", "TKT-1"]
    )

    rc = board_cli.main(
        ["update", "--workflow", str(workflow), "TKT-2", "--blocked-by", "TKT-3"]
    )

    assert rc == 0
    content = (tmp_path / "board" / "TKT-2.md").read_text(encoding="utf-8")
    assert "TKT-3" in content
    assert "TKT-1" not in content.split("---")[1]


def test_update_changes_state(tmp_path: Path) -> None:
    workflow = _make_workflow(tmp_path)
    board_cli.main(["new", "--workflow", str(workflow), "TKT-1", "t"])

    rc = board_cli.main(
        ["update", "--workflow", str(workflow), "TKT-1", "--state", "in progress"]
    )

    assert rc == 0
    content = (tmp_path / "board" / "TKT-1.md").read_text(encoding="utf-8")
    assert "state: In Progress" in content


def test_update_rejects_a_cycle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole point of the verb: cycle validation, unlike hand-editing."""
    workflow = _make_workflow(tmp_path)
    board_cli.main(["new", "--workflow", str(workflow), "TKT-1", "root"])
    board_cli.main(
        ["new", "--workflow", str(workflow), "TKT-2", "child", "--blocked-by", "TKT-1"]
    )
    capsys.readouterr()

    rc = board_cli.main(
        ["update", "--workflow", str(workflow), "TKT-1", "--add-blocked-by", "TKT-2"]
    )

    assert rc == 1
    assert "cycle" in capsys.readouterr().err


def test_update_rejects_unknown_blocker_and_unknown_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _make_workflow(tmp_path)
    board_cli.main(["new", "--workflow", str(workflow), "TKT-1", "t"])
    capsys.readouterr()

    assert (
        board_cli.main(
            ["update", "--workflow", str(workflow), "TKT-1", "--add-blocked-by", "NOPE-9"]
        )
        == 1
    )
    assert "unknown blocked_by target" in capsys.readouterr().err

    assert (
        board_cli.main(
            ["update", "--workflow", str(workflow), "TKT-1", "--state", "Nowhere"]
        )
        == 1
    )
    assert "unknown state" in capsys.readouterr().err


def test_update_rejects_missing_ticket_and_malformed_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _make_workflow(tmp_path)

    assert board_cli.main(["update", "--workflow", str(workflow), "GHOST-1"]) == 1
    assert "not found" in capsys.readouterr().err

    assert board_cli.main(["update", "--workflow", str(workflow), "../../evil"]) == 1
    assert "must match" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# F-15 — validate + create is atomic under the board lock
# ---------------------------------------------------------------------------


def test_concurrent_generated_id_creates_never_collide(tmp_path: Path) -> None:
    """Validate+allocate+write happen under one board lock.

    Eight threads asking for the next `GEN-<n>` used to be able to observe the
    same snapshot; each ticket must still get its own id and its own file.
    """
    import threading

    from symphony.trackers.file import FileBoardTracker
    from symphony.trackers.validate import validate_ticket_dependencies

    board = tmp_path / "board"
    board.mkdir()
    tracker = FileBoardTracker(board_cli._tracker_from_root(board))
    created: list[str] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def _worker(n: int) -> None:
        def _validate(issues, resolved):
            validate_ticket_dependencies(
                issues, identifier=resolved, blocked_by=[], new_ticket=True
            )

        try:
            identifier, _ = tracker.create_validated(
                identifier=None,
                prefix="GEN",
                validate=_validate,
                title=f"ticket {n}",
            )
        except Exception as exc:  # pragma: no cover - failure path
            with lock:
                errors.append(exc)
            return
        with lock:
            created.append(identifier)

    threads = [threading.Thread(target=_worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert not errors, errors
    assert sorted(created) == sorted(set(created)), f"id collision: {created}"
    assert len(list(board.glob("*.md"))) == 8


def test_create_validated_runs_the_validator_with_the_in_lock_snapshot(
    tmp_path: Path,
) -> None:
    from symphony.trackers.file import FileBoardTracker

    board = tmp_path / "board"
    board.mkdir()
    tracker = FileBoardTracker(board_cli._tracker_from_root(board))
    tracker.create(identifier="A-1", title="root")
    seen: list[tuple[int, str]] = []

    def _validate(issues, resolved):
        seen.append((len(issues), resolved))

    identifier, path = tracker.create_validated(
        identifier=None,
        prefix="GEN",
        validate=_validate,
        title="generated",
    )

    assert identifier == "GEN-1"
    assert path.is_file()
    assert seen == [(1, "GEN-1")]


def test_create_validated_aborts_the_write_when_the_validator_raises(
    tmp_path: Path,
) -> None:
    from symphony.errors import BoardDependencyError
    from symphony.trackers.file import FileBoardTracker

    board = tmp_path / "board"
    board.mkdir()
    tracker = FileBoardTracker(board_cli._tracker_from_root(board))

    def _reject(issues, resolved):
        raise BoardDependencyError("nope", identifier=resolved)

    with pytest.raises(BoardDependencyError):
        tracker.create_validated(
            identifier="X-1", validate=_reject, title="never written"
        )

    assert list(board.glob("*.md")) == []


def test_cli_errors_do_not_leak_the_internal_error_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """F-31: `board_dependency_error:` is an API code, not operator text."""
    workflow = _make_workflow(tmp_path)
    board_cli.main(["new", "--workflow", str(workflow), "A-1", "first"])
    capsys.readouterr()

    rc = board_cli.main(["new", "--workflow", str(workflow), "A-1", "duplicate"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "ticket already exists" in err
    assert "identifier='A-1'" in err
    assert "board_dependency_error" not in err
    assert "symphony_error" not in err
