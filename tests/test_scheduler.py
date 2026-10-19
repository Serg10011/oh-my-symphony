from __future__ import annotations

from datetime import datetime, timezone

import pytest

from symphony.issue import BlockerRef, Issue
from symphony.orchestrator.scheduler import (
    critical_path_lengths,
    dependency_cycle_nodes,
    analyze_dependencies,
    group_by_request,
    RequestGroupKey,
    request_group_key,
    sort_candidates,
)


def issue(
    identifier: str,
    *,
    priority: int | None = None,
    blockers: tuple[str, ...] = (),
    request: str | None = None,
) -> Issue:
    return Issue(
        id=identifier,
        identifier=identifier,
        title=identifier,
        description="",
        priority=priority,
        state="Todo",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        blocked_by=tuple(
            BlockerRef(id=blocker, identifier=blocker, state="Todo")
            for blocker in blockers
        ),
        request=request,
    )


def test_fifo_preserves_registration_order_and_ignores_priority() -> None:
    later = issue("TASK-20", priority=0)
    earlier = issue("TASK-3", priority=4)
    assert [row.identifier for row in sort_candidates([later, earlier], "fifo")] == [
        "TASK-3",
        "TASK-20",
    ]


def test_dag_orders_priority_then_downstream_length_then_registration() -> None:
    short_high = issue("TASK-9", priority=0)
    root = issue("TASK-20", priority=1)
    middle = issue("TASK-21", priority=1, blockers=("TASK-20",))
    leaf = issue("TASK-22", priority=1, blockers=("TASK-21",))
    independent = issue("TASK-2", priority=1)
    rows = [leaf, independent, middle, root, short_high]

    assert [row.identifier for row in sort_candidates(rows, "dag")] == [
        "TASK-9",
        "TASK-20",
        "TASK-21",
        "TASK-2",
        "TASK-22",
    ]
    assert analyze_dependencies(rows).waves == {
        "TASK-9": 0,
        "TASK-20": 0,
        "TASK-21": 1,
        "TASK-22": 2,
        "TASK-2": 0,
    }


def test_critical_path_collapses_cycle_deterministically() -> None:
    a = issue("A-1", blockers=("B-1",))
    b = issue("B-1", blockers=("A-1",))
    tail = issue("C-1", blockers=("B-1",))
    lengths = critical_path_lengths([tail, b, a])
    assert lengths == {"A-1": 1, "B-1": 1, "C-1": 0}


def test_request_grouping_is_explicit_and_ungrouped_is_standalone() -> None:
    grouped = issue("TASK-1", request="REQ-7")
    second = issue("TASK-2", request="REQ-7")
    standalone = issue("TASK-3")
    groups = group_by_request([standalone, second, grouped])
    request_key = RequestGroupKey("request", "REQ-7")
    standalone_key = RequestGroupKey("ticket", "TASK-3")
    assert list(groups) == [request_key, standalone_key]
    assert [row.identifier for row in groups[request_key]] == ["TASK-1", "TASK-2"]
    assert request_group_key(standalone) == standalone_key


def test_unknown_policy_fails_closed() -> None:
    with pytest.raises(ValueError, match="unsupported scheduling policy"):
        sort_candidates([issue("TASK-1")], "magic")


def test_dependency_cycle_detection_handles_deep_acyclic_chain_iteratively():
    identifiers = {str(index) for index in range(1_500)}
    edges = {(str(index), str(index + 1)) for index in range(1_499)}

    assert dependency_cycle_nodes(identifiers, edges) == set()
