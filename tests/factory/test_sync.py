from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from symphony.factory.sync import sync_wayfinder
from symphony.trackers.file import FileBoardTracker, parse_ticket_file
from symphony.workflow import TrackerConfig


def _tracker(root: Path) -> FileBoardTracker:
    return FileBoardTracker(
        TrackerConfig(
            kind="file",
            endpoint="",
            api_key="",
            project_slug="",
            active_states=("Ready", "Build", "Verify"),
            terminal_states=("Done", "Blocked"),
            board_root=root,
        )
    )


def _ticket(path: Path, key: str, title: str, blocked_by: tuple[str, ...] = ()) -> None:
    deps = "[" + ", ".join(blocked_by) + "]"
    path.write_text(
        f"""---
id: {key}
title: {title}
route: LEGACY
blocked_by: {deps}
skills: [superqa]
---

## Acceptance criteria

- The slice is independently verified.

## Proof commands

- `pytest`

## Non-goals

- Unrelated work.
""",
        encoding="utf-8",
    )


def test_sync_allocates_in_order_maps_dependencies_and_is_idempotent(tmp_path: Path) -> None:
    tickets = tmp_path / "wayfinder" / "tickets"
    tickets.mkdir(parents=True)
    _ticket(tickets / "001-foundation.md", "foundation", "Foundation")
    _ticket(tickets / "002-feature.md", "feature", "Feature", ("foundation",))
    tracker = _tracker(tmp_path / "kanban")

    first = sync_wayfinder(tickets.parent, tracker, prefix="TASK")
    before_second_sync = {
        path.name: path.read_bytes() for path in (tmp_path / "kanban").glob("TASK-*.md")
    }
    second = sync_wayfinder(tickets.parent, tracker, prefix="TASK")

    assert [(item.identifier, item.created) for item in first] == [
        ("TASK-1", True),
        ("TASK-2", True),
    ]
    assert all(not item.created for item in second)
    feature_front, feature_body = parse_ticket_file(tmp_path / "kanban" / "TASK-2.md")
    assert feature_front["skills"] == ["supergoal", "superqa"]
    assert feature_front["blocked_by"] == ["TASK-1"]
    assert "## Dependencies\n\n- TASK-1" in feature_body
    assert len(list((tmp_path / "kanban").glob("TASK-*.md"))) == 2
    assert before_second_sync == {
        path.name: path.read_bytes() for path in (tmp_path / "kanban").glob("TASK-*.md")
    }


def test_sync_frontier_only_imports_only_currently_unblocked_tickets(
    tmp_path: Path,
) -> None:
    tickets = tmp_path / "wayfinder" / "tickets"
    tickets.mkdir(parents=True)
    _ticket(tickets / "001-foundation.md", "foundation", "Foundation")
    _ticket(tickets / "002-feature.md", "feature", "Feature", ("foundation",))
    tracker = _tracker(tmp_path / "kanban")

    results = sync_wayfinder(
        tickets.parent, tracker, prefix="TASK", all_tickets=False
    )

    assert [item.key for item in results] == ["foundation"]
    assert [path.name for path in (tmp_path / "kanban").glob("TASK-*.md")] == [
        "TASK-1.md"
    ]


def test_sync_refuses_to_overwrite_ready_ticket_with_corrupt_managed_boundary(
    tmp_path: Path,
) -> None:
    tickets = tmp_path / "wayfinder" / "tickets"
    tickets.mkdir(parents=True)
    _ticket(tickets / "001-feature.md", "feature", "Feature")
    tracker = _tracker(tmp_path / "kanban")
    sync_wayfinder(tickets.parent, tracker, prefix="TASK")
    path = tmp_path / "kanban" / "TASK-1.md"
    original = path.read_text(encoding="utf-8").replace(
        "<!-- /symphony-factory-managed -->", "operator-owned content"
    )
    path.write_text(original, encoding="utf-8")

    try:
        sync_wayfinder(tickets.parent, tracker, prefix="TASK")
    except ValueError as exc:
        assert "managed end marker is missing" in str(exc)
    else:
        raise AssertionError("corrupt managed boundary was overwritten")
    assert path.read_text(encoding="utf-8") == original


def test_sync_refuses_ready_ticket_with_edited_managed_content(tmp_path: Path) -> None:
    tickets = tmp_path / "wayfinder" / "tickets"
    tickets.mkdir(parents=True)
    source = tickets / "001-feature.md"
    _ticket(source, "feature", "Feature")
    tracker = _tracker(tmp_path / "kanban")
    sync_wayfinder(tickets.parent, tracker, prefix="TASK")
    path = tmp_path / "kanban" / "TASK-1.md"
    original = path.read_text(encoding="utf-8").replace(
        "The slice is independently verified.", "human edited managed text"
    )
    path.write_text(original, encoding="utf-8")
    _ticket(source, "feature", "Renamed feature")

    with pytest.raises(ValueError, match="managed fields or region were edited"):
        sync_wayfinder(tickets.parent, tracker, prefix="TASK")

    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("title", "Operator title"),
        ("labels", ["human"]),
        ("skills", ["supergoal"]),
        ("blocked_by", ["TASK-99"]),
    ),
)
def test_sync_refuses_ready_ticket_with_edited_managed_frontmatter(
    field: str, replacement: object, tmp_path: Path
) -> None:
    tickets = tmp_path / "wayfinder" / "tickets"
    tickets.mkdir(parents=True)
    source = tickets / "001-feature.md"
    _ticket(source, "feature", "Feature")
    tracker = _tracker(tmp_path / "kanban")
    sync_wayfinder(tickets.parent, tracker, prefix="TASK")
    path = tmp_path / "kanban" / "TASK-1.md"
    front, body = parse_ticket_file(path)
    front[field] = replacement
    from symphony.trackers.file import write_ticket_atomic

    write_ticket_atomic(path, front, body)
    original = path.read_bytes()
    _ticket(source, "feature", "Refreshed feature")

    with pytest.raises(ValueError, match="managed fields or region were edited"):
        sync_wayfinder(tickets.parent, tracker, prefix="TASK")

    assert path.read_bytes() == original


def test_sync_accepts_source_paths_with_spaces(tmp_path: Path) -> None:
    tickets = tmp_path / "wayfinder" / "tickets"
    tickets.mkdir(parents=True)
    _ticket(tickets / "001 user feature.md", "feature", "Feature")
    tracker = _tracker(tmp_path / "kanban")

    first = sync_wayfinder(tickets.parent, tracker, prefix="TASK")
    second = sync_wayfinder(tickets.parent, tracker, prefix="TASK")

    assert [item.created for item in first] == [True]
    assert [item.created for item in second] == [False]


@pytest.mark.parametrize("prefix", ("", "../ESCAPE", "nested/TASK", "TASK 1"))
def test_sync_rejects_unsafe_identifier_prefix(prefix: str, tmp_path: Path) -> None:
    tickets = tmp_path / "wayfinder" / "tickets"
    tickets.mkdir(parents=True)
    _ticket(tickets / "001-feature.md", "feature", "Feature")
    tracker = _tracker(tmp_path / "kanban")

    with pytest.raises(ValueError, match="ticket prefix"):
        sync_wayfinder(tickets.parent, tracker, prefix=prefix)

    assert not list(tmp_path.rglob("ESCAPE-*.md"))
    assert not list((tmp_path / "kanban").glob("*.md"))


def test_sync_restores_refreshed_cards_when_later_write_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tickets = tmp_path / "wayfinder" / "tickets"
    tickets.mkdir(parents=True)
    source = tickets / "001-existing.md"
    _ticket(source, "existing", "Existing")
    tracker = _tracker(tmp_path / "kanban")
    sync_wayfinder(tickets.parent, tracker, prefix="TASK")
    existing = tmp_path / "kanban" / "TASK-1.md"
    before = existing.read_bytes()
    _ticket(source, "existing", "Refreshed")
    _ticket(tickets / "002-new.md", "new", "New")
    monkeypatch.setattr(
        tracker,
        "create_with_next_identifier",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        sync_wayfinder(tickets.parent, tracker, prefix="TASK", all_tickets=True)

    assert existing.read_bytes() == before


def test_sync_refreshes_dependencies_in_managed_frontmatter(tmp_path: Path) -> None:
    tickets = tmp_path / "wayfinder" / "tickets"
    tickets.mkdir(parents=True)
    base = tickets / "001-base.md"
    feature = tickets / "002-feature.md"
    _ticket(base, "base", "Base")
    _ticket(feature, "feature", "Feature")
    tracker = _tracker(tmp_path / "kanban")
    sync_wayfinder(tickets.parent, tracker, prefix="TASK", all_tickets=True)
    _ticket(feature, "feature", "Feature", ("base",))

    sync_wayfinder(tickets.parent, tracker, prefix="TASK", all_tickets=True)

    front, _ = parse_ticket_file(tmp_path / "kanban" / "TASK-2.md")
    assert front["blocked_by"] == ["TASK-1"]


def test_sync_preserves_state_and_worker_notes_on_source_update(tmp_path: Path) -> None:
    tickets = tmp_path / "wayfinder" / "tickets"
    tickets.mkdir(parents=True)
    source = tickets / "001-feature.md"
    _ticket(source, "feature", "Feature")
    tracker = _tracker(tmp_path / "kanban")
    sync_wayfinder(tickets.parent, tracker, prefix="TASK")
    tracker.transition("TASK-1", "Build")
    issue = tracker.fetch_issue_full_by_id("TASK-1")
    assert issue is not None
    tracker.append_note(issue, "Implementation", "worker note")
    _ticket(source, "feature", "Renamed feature")

    sync_wayfinder(tickets.parent, tracker, prefix="TASK")

    front, body = parse_ticket_file(tmp_path / "kanban" / "TASK-1.md")
    assert front["state"] == "Build"
    assert front["title"] == "Feature"
    assert "worker note" in body


def test_sync_validates_whole_graph_before_writing(tmp_path: Path) -> None:
    tickets = tmp_path / "wayfinder" / "tickets"
    tickets.mkdir(parents=True)
    _ticket(tickets / "001-feature.md", "feature", "Feature", ("missing",))
    tracker = _tracker(tmp_path / "kanban")

    try:
        sync_wayfinder(tickets.parent, tracker, prefix="TASK")
    except ValueError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("invalid graph was accepted")
    assert not list((tmp_path / "kanban").glob("TASK-*.md"))


def test_sync_imports_complete_diamond_graph_and_preserves_rerun_bytes(tmp_path: Path) -> None:
    tickets = tmp_path / "wayfinder/tickets"
    tickets.mkdir(parents=True)
    _ticket(tickets / "base.md", "base", "Base")
    _ticket(tickets / "left.md", "left", "Left", ("base",))
    _ticket(tickets / "right.md", "right", "Right", ("base",))
    _ticket(tickets / "join.md", "join", "Join", ("left", "right"))
    tracker = _tracker(tmp_path / "kanban")

    results = sync_wayfinder(tickets.parent, tracker)
    identifiers = {item.key: item.identifier for item in results}
    front, _ = parse_ticket_file(tracker.board_root / f"{identifiers['join']}.md")
    assert front["blocked_by"] == [identifiers["left"], identifiers["right"]]
    before = {path: path.read_bytes() for path in tracker.board_root.glob("*.md")}

    rerun = sync_wayfinder(tickets.parent, tracker)
    assert len(results) == len(rerun) == 4
    assert all(not item.created for item in rerun)
    assert before == {path: path.read_bytes() for path in tracker.board_root.glob("*.md")}


def test_concurrent_syncs_share_one_provenance_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    sync_module = importlib.import_module("symphony.factory.sync")
    tickets = tmp_path / "wayfinder/tickets"
    tickets.mkdir(parents=True)
    _ticket(tickets / "one.md", "one", "One")
    board = tmp_path / "kanban"
    scan_barrier = threading.Barrier(2)
    scan = sync_module._existing_sources

    def concurrent_scan(tracker):
        snapshot = scan(tracker)
        try:
            # Without the sync lock, both calls return the same empty snapshot.
            scan_barrier.wait(timeout=2)
        except threading.BrokenBarrierError:
            pass
        return snapshot

    monkeypatch.setattr(sync_module, "_existing_sources", concurrent_scan)
    with ThreadPoolExecutor(max_workers=2) as pool:
        runs = [pool.submit(sync_wayfinder, tickets.parent, _tracker(board)) for _ in range(2)]
        results = [run.result(timeout=10) for run in runs]

    assert [result[0].identifier for result in results] == ["TASK-1", "TASK-1"]
    assert sum(result[0].created for result in results) == 1
    assert len(list(board.glob("TASK-*.md"))) == 1


def test_sync_rollback_preserves_external_build_transition_after_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tickets = tmp_path / "wayfinder/tickets"
    tickets.mkdir(parents=True)
    source = tickets / "001-existing.md"
    _ticket(source, "existing", "Existing")
    tracker = _tracker(tmp_path / "kanban")
    sync_wayfinder(tickets.parent, tracker)
    _ticket(source, "existing", "Refreshed")
    _ticket(tickets / "002-new.md", "new", "New")

    def fail_after_transition(*_args, **_kwargs):
        tracker.transition("TASK-1", "Build")
        raise OSError("disk full")

    monkeypatch.setattr(tracker, "create_with_next_identifier", fail_after_transition)
    with pytest.raises(OSError, match="disk full"):
        sync_wayfinder(tickets.parent, tracker)

    front, _ = parse_ticket_file(tracker.board_root / "TASK-1.md")
    assert front["state"] == "Build"
    assert front["title"] == "Refreshed"


@pytest.mark.parametrize("external_transition", [False, True])
def test_sync_rollback_removes_only_unchanged_new_cards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, external_transition: bool
) -> None:
    tickets = tmp_path / "wayfinder/tickets"
    tickets.mkdir(parents=True)
    for key in ("one", "two", "three"):
        _ticket(tickets / f"{key}.md", key, key)
    tracker = _tracker(tmp_path / "kanban")
    create = tracker.create_with_next_identifier
    calls = 0

    def fail_on_third(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            if external_transition:
                tracker.transition("TASK-1", "Build")
            raise OSError("disk full")
        return create(*args, **kwargs)

    monkeypatch.setattr(tracker, "create_with_next_identifier", fail_on_third)
    with pytest.raises(OSError, match="disk full"):
        sync_wayfinder(tickets.parent, tracker)

    remaining = list(tracker.board_root.glob("TASK-*.md"))
    assert len(remaining) == int(external_transition)
    if external_transition:
        assert parse_ticket_file(remaining[0])[0]["state"] == "Build"


@pytest.mark.parametrize("depth", [2, 3])
def test_sync_rollback_preserves_transitive_dependencies_of_edited_new_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, depth: int,
) -> None:
    tickets = tmp_path / "wayfinder/tickets"
    tickets.mkdir(parents=True)
    keys = ["a", "b", "c"][:depth]
    for index, key in enumerate(keys):
        _ticket(tickets / f"{key}.md", key, key, tuple(keys[index - 1:index]) if index else ())
    _ticket(tickets / "z.md", "z", "z", (keys[-1],))
    _ticket(tickets / "unrelated.md", "unrelated", "unrelated")
    tracker = _tracker(tmp_path / "kanban")
    create = tracker.create_with_next_identifier
    created: dict[str, str] = {}

    def fail_after_operator_note(*args, **kwargs):
        if kwargs["title"] == "z":
            issue = tracker.fetch_issue_full_by_id(created[keys[-1]])
            assert issue is not None
            tracker.append_note(issue, "Operator Note", "Retain this legitimate note.")
            raise OSError("disk full")
        result = create(*args, **kwargs)
        created[kwargs["title"]] = result[0]
        return result

    monkeypatch.setattr(tracker, "create_with_next_identifier", fail_after_operator_note)
    with pytest.raises(OSError, match="disk full"):
        sync_wayfinder(tickets.parent, tracker)

    remaining = {issue.title: issue for issue in tracker.scan_all()}
    assert set(remaining) == set(keys)
    assert "Retain this legitimate note." in (remaining[keys[-1]].description or "")
    assert all(blocker.identifier in {issue.identifier for issue in remaining.values()}
               for issue in remaining.values() for blocker in issue.blocked_by)


def test_sync_rollback_preserves_refreshed_dependency_of_edited_retained_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tickets = tmp_path / "wayfinder/tickets"
    tickets.mkdir(parents=True)
    source = tickets / "a.md"
    _ticket(source, "a", "Original A")
    tracker = _tracker(tmp_path / "kanban")
    sync_wayfinder(tickets.parent, tracker)
    _ticket(tickets / "base.md", "base", "New Base")
    _ticket(source, "a", "Refreshed A", ("base",))
    _ticket(tickets / "b.md", "b", "B", ("a",))
    _ticket(tickets / "z.md", "z", "Z", ("b",))
    create = tracker.create_with_next_identifier

    def fail_after_note(*args, **kwargs):
        if kwargs["title"] == "Z":
            issue = tracker.fetch_issue_full_by_id("TASK-3")
            assert issue is not None
            tracker.append_note(issue, "Operator Note", "B must retain its approved prerequisites.")
            raise OSError("disk full")
        return create(*args, **kwargs)

    monkeypatch.setattr(tracker, "create_with_next_identifier", fail_after_note)
    with pytest.raises(OSError, match="disk full"):
        sync_wayfinder(tickets.parent, tracker)

    first, _ = parse_ticket_file(tracker.board_root / "TASK-1.md")
    second, body = parse_ticket_file(tracker.board_root / "TASK-3.md")
    assert first["title"] == "Refreshed A"
    assert first["blocked_by"] == ["TASK-2"]
    assert (tracker.board_root / "TASK-2.md").is_file()
    assert second["blocked_by"] == ["TASK-1"]
    assert "B must retain its approved prerequisites." in body
