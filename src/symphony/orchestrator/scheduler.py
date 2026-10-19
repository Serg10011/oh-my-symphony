"""Pure dispatch ordering and request-grouping helpers."""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from collections.abc import Iterable
from typing import Literal, Sequence

from ..issue import BlockerRef, Issue, registration_order_key, sort_for_dispatch

SchedulingPolicy = Literal["fifo", "dag"]
RequestGroupKind = Literal["request", "ticket"]
_SCHEDULING_POLICIES = frozenset({"fifo", "dag"})
MAX_DEPENDENCY_NODES = 5_000
MAX_DEPENDENCY_EDGES = 20_000


@dataclass(frozen=True)
class RequestGroupKey:
    """Collision-safe identity for grouped and standalone work."""

    kind: RequestGroupKind
    value: str


def normalize_scheduling_policy(value: object | None) -> SchedulingPolicy:
    """Return a canonical scheduling policy or reject an unsupported value."""

    if value is None:
        return "fifo"
    if not isinstance(value, str):
        raise ValueError(f"unsupported scheduling policy: {value!r}")
    normalized = value.strip().casefold()
    if normalized not in _SCHEDULING_POLICIES:
        raise ValueError(f"unsupported scheduling policy: {value!r}")
    return normalized  # type: ignore[return-value]


@dataclass(frozen=True)
class DependencyAnalysis:
    critical_path_lengths: dict[str, int]
    waves: dict[str, int]


def analyze_dependencies(issues: Sequence[Issue]) -> DependencyAnalysis:
    """Build the graph and SCC condensation once for all schedule projections."""

    nodes, downstream = _dependency_graph(issues)
    if not nodes:
        return DependencyAnalysis({}, {})
    components = _strongly_connected_components(nodes, downstream)
    component_by_id = {
        issue_id: component_index
        for component_index, component in enumerate(components)
        for issue_id in component
    }
    component_downstream: list[set[int]] = [set() for _ in components]
    component_upstream: list[set[int]] = [set() for _ in components]
    indegree = [0] * len(components)
    for blocker_id, dependent_ids in downstream.items():
        source = component_by_id[blocker_id]
        for dependent_id in dependent_ids:
            target = component_by_id[dependent_id]
            if source == target or target in component_downstream[source]:
                continue
            component_downstream[source].add(target)
            component_upstream[target].add(source)
            indegree[target] += 1

    lengths = [0] * len(components)
    remaining_downstream = [len(edges) for edges in component_downstream]
    ready = [
        component_index
        for component_index, remaining in enumerate(remaining_downstream)
        if remaining == 0
    ]
    heapq.heapify(ready)
    while ready:
        component_index = heapq.heappop(ready)
        for upstream in sorted(component_upstream[component_index]):
            lengths[upstream] = max(
                lengths[upstream], 1 + lengths[component_index]
            )
            remaining_downstream[upstream] -= 1
            if remaining_downstream[upstream] == 0:
                heapq.heappush(ready, upstream)

    waves = [0] * len(components)
    wave_ready = [index for index, value in enumerate(indegree) if value == 0]
    heapq.heapify(wave_ready)
    while wave_ready:
        component = heapq.heappop(wave_ready)
        for dependent in sorted(component_downstream[component]):
            waves[dependent] = max(waves[dependent], waves[component] + 1)
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(wave_ready, dependent)

    return DependencyAnalysis(
        {
            issue_id: lengths[component_by_id[issue_id]]
            for issue_id in nodes
        },
        {
            issue_id: waves[component_by_id[issue_id]]
            for issue_id in nodes
        },
    )


def critical_path_lengths(issues: Sequence[Issue]) -> dict[str, int]:
    """Measure each issue's longest downstream dependency path in edges."""

    return analyze_dependencies(issues).critical_path_lengths


def sort_candidates(
    issues: Sequence[Issue],
    policy: object | None = "fifo",
    *,
    analysis: DependencyAnalysis | None = None,
) -> list[Issue]:
    """Sort candidates under FIFO or dependency-aware DAG policy.

    FIFO delegates unchanged to the existing registration-order sorter. DAG
    ranks lower numeric priorities first (with no priority last), then longer
    downstream critical paths, then the same registration-order key.
    """

    normalized = normalize_scheduling_policy(policy)
    candidates = list(issues)
    if normalized == "fifo":
        return sort_for_dispatch(candidates)

    path_lengths = (
        analysis.critical_path_lengths
        if analysis is not None
        else critical_path_lengths(candidates)
    )
    return sorted(
        candidates,
        key=lambda issue: (
            issue.priority is None,
            issue.priority if issue.priority is not None else 0,
            -path_lengths.get(issue.id, 0),
            registration_order_key(issue),
        ),
    )


def request_group_key(issue: Issue) -> RequestGroupKey:
    """Group only by an explicit nonblank request; otherwise isolate a ticket."""

    request = issue.request.strip() if isinstance(issue.request, str) else ""
    if request:
        return RequestGroupKey("request", request)
    return RequestGroupKey("ticket", issue.identifier)


def group_by_request(
    issues: Sequence[Issue],
) -> dict[RequestGroupKey, list[Issue]]:
    """Group issues under collision-safe keys in registration order."""

    groups: dict[RequestGroupKey, list[Issue]] = {}
    for issue in sort_for_dispatch(list(issues)):
        groups.setdefault(request_group_key(issue), []).append(issue)
    return groups


def _dependency_graph(
    issues: Sequence[Issue],
) -> tuple[dict[str, Issue], dict[str, set[str]]]:
    ordered = sorted(issues, key=_deterministic_issue_key)
    nodes = {issue.id: issue for issue in ordered}
    by_identifier: dict[str, Issue] = {}
    by_id: dict[str, Issue] = {}
    for issue in ordered:
        by_identifier.setdefault(issue.identifier, issue)
        by_id.setdefault(issue.id, issue)

    downstream = {issue_id: set() for issue_id in nodes}
    for dependent in ordered:
        for blocker in dependent.blocked_by:
            resolved = _resolve_blocker(blocker, by_identifier, by_id)
            if resolved is not None:
                downstream[resolved.id].add(dependent.id)
    return nodes, downstream


def _resolve_blocker(
    blocker: BlockerRef,
    by_identifier: dict[str, Issue],
    by_id: dict[str, Issue],
) -> Issue | None:
    if blocker.identifier:
        resolved = by_identifier.get(blocker.identifier)
        if resolved is not None:
            return resolved
    if blocker.id:
        resolved = by_id.get(blocker.id)
        if resolved is not None:
            return resolved
    # Some trackers expose the same opaque value in the other reference field.
    if blocker.identifier:
        resolved = by_id.get(blocker.identifier)
        if resolved is not None:
            return resolved
    if blocker.id:
        return by_identifier.get(blocker.id)
    return None


def _strongly_connected_components(
    nodes: dict[str, Issue] | set[str], downstream: dict[str, set[str]]
) -> list[tuple[str, ...]]:
    """Iterative Kosaraju traversal, avoiding recursion limits and cycle loops."""

    ordered_ids = (
        sorted(
            nodes,
            key=lambda issue_id: _deterministic_issue_key(nodes[issue_id]),
        )
        if isinstance(nodes, dict)
        else sorted(nodes, key=lambda issue_id: (issue_id.casefold(), issue_id))
    )
    visited: set[str] = set()
    finished: list[str] = []
    for start in ordered_ids:
        if start in visited:
            continue
        visited.add(start)
        stack: list[tuple[str, bool]] = [(start, False)]
        while stack:
            issue_id, expanded = stack.pop()
            if expanded:
                finished.append(issue_id)
                continue
            stack.append((issue_id, True))
            for dependent_id in sorted(downstream[issue_id], reverse=True):
                if dependent_id not in visited:
                    visited.add(dependent_id)
                    stack.append((dependent_id, False))

    upstream = {issue_id: set() for issue_id in nodes}
    for issue_id, dependent_ids in downstream.items():
        for dependent_id in dependent_ids:
            upstream[dependent_id].add(issue_id)

    assigned: set[str] = set()
    components: list[tuple[str, ...]] = []
    for start in reversed(finished):
        if start in assigned:
            continue
        assigned.add(start)
        component: list[str] = []
        component_stack = [start]
        while component_stack:
            issue_id = component_stack.pop()
            component.append(issue_id)
            for blocker_id in sorted(upstream[issue_id], reverse=True):
                if blocker_id not in assigned:
                    assigned.add(blocker_id)
                    component_stack.append(blocker_id)
        components.append(tuple(sorted(component)))
    return components


def dependency_cycle_nodes(
    identifiers: Iterable[str], edges: Iterable[tuple[str, str]]
) -> set[str]:
    """Return exact cycle members using the scheduler's iterative SCC walk."""

    nodes = set(identifiers)
    downstream = {identifier: set() for identifier in nodes}
    self_edges: set[str] = set()
    for source, target in edges:
        if source not in nodes or target not in nodes:
            continue
        downstream[source].add(target)
        if source == target:
            self_edges.add(source)
    cycles = set(self_edges)
    for component in _strongly_connected_components(nodes, downstream):
        if len(component) > 1:
            cycles.update(component)
    return cycles


def _deterministic_issue_key(issue: Issue) -> tuple[object, ...]:
    return (
        registration_order_key(issue),
        issue.identifier.casefold(),
        issue.identifier,
        issue.id.casefold(),
        issue.id,
    )
