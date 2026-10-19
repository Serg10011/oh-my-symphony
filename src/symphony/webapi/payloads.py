"""JSON serialization of board cards, schedules, columns and workflow config."""

from __future__ import annotations

import heapq
from datetime import date, datetime
from typing import Any

from ..issue import Issue
from ..orchestrator import Orchestrator
from ..orchestrator.scheduler import (
    RequestGroupKey,
    dependency_cycle_nodes,
)
from ..workflow import (
    selectable_agent_kinds,
    SUPPORTED_CI_MODES,
    ServiceConfig,
)

# ---------------------------------------------------------------------------
# serialization
# ---------------------------------------------------------------------------


def _issue_card(
    issue: Issue,
    *,
    attention: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "identifier": issue.identifier,
        "title": issue.title,
        "state": issue.state,
        "priority": issue.priority,
        "labels": list(issue.labels),
        "skills": list(issue.skills),
        "agent_kind": issue.agent_kind or "",
        # Audit stamp on `stage_kinds`-routed boards, where the pin stays
        # empty on purpose (F-20).
        "last_agent_kind": issue.last_agent_kind or "",
        "request": issue.request or "",
        "blocked_by": [
            {"identifier": b.identifier, "state": b.state} for b in issue.blocked_by
        ],
        "attention": attention,
        "created_at": issue.created_at.isoformat() if issue.created_at else None,
        "updated_at": issue.updated_at.isoformat() if issue.updated_at else None,
    }


_PUBLIC_SCHEDULE_REASONS = {
    "not_evaluated": "scheduler evaluation is pending",
    "ready": "eligible for dispatch",
    "dispatched": "agent run started",
    "running": "agent run is active",
    "retry_scheduled": "owned by the retry timer",
    "auto_triage": "ticket advanced by automatic triage",
    "continuous_improvement": "continuous improvement owns the idle board",
    "leased_elsewhere": "owned by another durable run lease",
    "registry_unavailable": "durable authority registry is unavailable",
    "historical_release_verifier": "historical release verifier is evidence-only",
    "claimed": "another scheduler path owns the ticket",
    "paused": "ticket is paused",
    "budget_exhausted": "turn budget is exhausted",
    "finalizing": "finalization owns the ticket",
    "inactive": "ticket state is not active",
    "incomplete_identity": "ticket identity is incomplete",
    "unsupported_agent": "ticket requests an unsupported agent",
    "waiting_dependency": "waiting for dependencies",
    "waiting_global_capacity": "waiting for global capacity",
    "waiting_state_capacity": "waiting for state capacity",
    "refused_conflict": "final conflict check refused dispatch",
    "refused_dispatch_authority": "final dispatch authority was not acquired",
    "terminal_success": "dependency is complete",
    "terminal_needs_action": "terminal state does not resolve dependencies",
    "dangling_dependency": "blocker is not present on the board",
    "snapshot_unavailable": "not present in the last scheduler evaluation",
    "decision_stale": "ticket changed after scheduler evaluation",
}


def _public_schedule_reason(code: str) -> str:
    return _PUBLIC_SCHEDULE_REASONS.get(code, "scheduler decision is available by code")


def _request_group_schedule_payload(
    *,
    key: RequestGroupKey,
    members: list[Issue],
    all_issues: list[Issue],
    schedule: dict[str, Any],
    orchestrator: Orchestrator,
    cfg: ServiceConfig,
) -> dict[str, Any]:
    by_identifier = {issue.identifier: issue for issue in all_issues}
    by_reference = dict(by_identifier)
    by_reference.update({issue.id: issue for issue in all_issues})
    decisions = {
        row.get("identifier"): row
        for row in schedule.get("entries", [])
        if isinstance(row, dict) and isinstance(row.get("identifier"), str)
    }
    member_ids = {issue.identifier for issue in members}
    node_issues: dict[str, Issue | None] = {
        issue.identifier: issue for issue in members
    }
    edge_set: set[tuple[str, str]] = set()
    pending = list(members)
    traversed: set[str] = set()
    while pending:
        issue = pending.pop()
        if issue.identifier in traversed:
            continue
        traversed.add(issue.identifier)
        node_issues.setdefault(issue.identifier, issue)
        for blocker in issue.blocked_by:
            blocker_reference = blocker.identifier or blocker.id
            if not blocker_reference:
                continue
            blocker_issue = by_reference.get(blocker_reference)
            blocker_id = (
                blocker_issue.identifier
                if blocker_issue is not None
                else (blocker.identifier or blocker.id)
            )
            if not blocker_id:
                continue
            edge_set.add((blocker_id, issue.identifier))
            node_issues.setdefault(blocker_id, blocker_issue)
            if blocker_issue is not None and blocker_issue.identifier not in traversed:
                pending.append(blocker_issue)

    downstream: dict[str, list[str]] = {identifier: [] for identifier in node_issues}
    indegree = {identifier: 0 for identifier in node_issues}
    for blocker_id, dependent_id in sorted(edge_set):
        downstream.setdefault(blocker_id, []).append(dependent_id)
        indegree.setdefault(dependent_id, 0)
        indegree[dependent_id] += 1
    ready = [identifier for identifier, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    execution_order: list[str] = []
    while ready:
        identifier = heapq.heappop(ready)
        execution_order.append(identifier)
        for dependent in sorted(downstream.get(identifier, [])):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(ready, dependent)
    warnings: list[str] = []
    unordered = sorted(set(node_issues) - set(execution_order))
    cycle_nodes = dependency_cycle_nodes(set(node_issues), edge_set)
    if unordered:
        execution_order.extend(unordered)
    if cycle_nodes:
        warnings.append("dependency_cycle")

    terminal_states = {state.strip().lower() for state in cfg.tracker.terminal_states}
    counts = {
        "running": 0,
        "ready": 0,
        "waiting": 0,
        "retrying": 0,
        "successful": 0,
        "needs_action": 0,
    }
    nodes: list[dict[str, Any]] = []
    for identifier in execution_order:
        issue = node_issues[identifier]
        scope = "request" if identifier in member_ids else "external"
        decision = decisions.get(identifier)
        if decision is None and issue is not None:
            decision = decisions.get(issue.identifier)
        if decision is not None:
            status = str(decision.get("status") or "waiting")
            code = str(decision.get("code") or "not_evaluated")
            reason = str(decision.get("reason") or "not evaluated")
        elif issue is None:
            status = "needs_action"
            code = "dangling_dependency"
            reason = "blocker is not present on the board"
            warnings.append(f"dangling_dependency:{identifier}")
        elif issue.state.strip().lower() in terminal_states:
            if orchestrator.dependency_state_resolved(issue.state):
                status = "successful"
                code = "terminal_success"
                reason = "dependency is complete"
            else:
                status = "needs_action"
                code = "terminal_needs_action"
                reason = f"terminal state does not resolve dependencies: {issue.state}"
        else:
            status = "waiting"
            code = "snapshot_unavailable"
            reason = "not present in the last scheduler evaluation"
        evaluated_state = decision.get("evaluated_state") if decision else None
        evaluated_updated_at = (
            decision.get("evaluated_updated_at") if decision else None
        )
        current_updated_at = (
            issue.updated_at.isoformat()
            if issue is not None and issue.updated_at is not None
            else None
        )
        current_blockers = (
            [
                {
                    "id": blocker.id,
                    "identifier": blocker.identifier,
                    "state": blocker.state,
                }
                for blocker in issue.blocked_by
            ]
            if issue is not None
            else []
        )
        decision_drifted = bool(
            issue is not None
            and (
                (evaluated_state is not None and issue.state != evaluated_state)
                or (
                    evaluated_updated_at is not None
                    and current_updated_at != evaluated_updated_at
                )
                or (
                    decision is not None
                    and "evaluated_priority" in decision
                    and issue.priority != decision.get("evaluated_priority")
                )
                or (
                    decision is not None
                    and "evaluated_request" in decision
                    and ((issue.request or "").strip() or None)
                    != decision.get("evaluated_request")
                )
                or (
                    decision is not None
                    and "evaluated_blocked_by" in decision
                    and current_blockers != decision.get("evaluated_blocked_by")
                )
                or (
                    decision is None
                    and bool(schedule.get("available"))
                    and issue.state.strip().lower() not in terminal_states
                )
            )
        )
        if decision_drifted:
            status = "needs_action"
            code = "decision_stale"
        reason = _public_schedule_reason(code)
        if scope == "request":
            counts[status if status in counts else "waiting"] += 1
        blockers: list[dict[str, Any]] = []
        for blocker, dependent in sorted(edge_set):
            if dependent != identifier:
                continue
            blocker_issue = node_issues.get(blocker)
            blockers.append(
                {
                    "identifier": blocker,
                    "state": blocker_issue.state if blocker_issue is not None else None,
                    "resolved": (
                        orchestrator.dependency_state_resolved(blocker_issue.state)
                        if blocker_issue is not None
                        else False
                    ),
                }
            )
        node = {
            "identifier": identifier,
            "title": issue.title if issue is not None else identifier,
            "exists": issue is not None,
            "state": issue.state if issue is not None else None,
            "priority": issue.priority if issue is not None else None,
            "scope": scope,
            "cycle": identifier in cycle_nodes,
            "decision_drifted": decision_drifted,
            "evaluated_state": evaluated_state,
            "blocked_by": blockers,
            "unlocks": sorted(downstream.get(identifier, [])),
            "decision": {"status": status, "code": code, "reason": reason},
            "queue_rank": decision.get("queue_rank") if decision else None,
            "scan_position": decision.get("scan_position") if decision else None,
            "wave": decision.get("wave") if decision else None,
            "global_critical_path_length": (
                decision.get("critical_path_length") if decision else None
            ),
            "starvation_promoted": bool(
                decision.get("starvation_promoted") if decision else False
            ),
            "retry": decision.get("retry") if decision else None,
        }
        nodes.append(node)

    longest_unresolved_nodes: int | None
    if cycle_nodes:
        longest_unresolved_nodes = None
    else:
        unresolved = {
            node["identifier"]
            for node in nodes
            if node["decision"]["status"] != "successful"
        }
        chain_nodes: dict[str, int] = {}
        for identifier in reversed(execution_order):
            if identifier not in unresolved:
                continue
            chain_nodes[identifier] = 1 + max(
                (
                    chain_nodes.get(dependent, 0)
                    for dependent in downstream.get(identifier, [])
                    if dependent in unresolved
                ),
                default=0,
            )
        longest_unresolved_nodes = max(chain_nodes.values(), default=0)
    has_decision_drift = any(node["decision_drifted"] for node in nodes)
    return {
        "schema_version": 1,
        "available": bool(schedule.get("available")),
        "request": {"kind": key.kind, "id": key.value},
        "generated_at": schedule.get("generated_at"),
        "stale": bool(schedule.get("stale")) or has_decision_drift,
        "decision_drifted": has_decision_drift,
        "execution_valid": not cycle_nodes,
        "policy": schedule.get("policy", "fifo"),
        "policy_order": schedule.get("policy_order", "starvation, registration"),
        "slots": schedule.get("slots", {}),
        "summary": {
            "counts": counts,
            "longest_unresolved_chain_nodes": longest_unresolved_nodes,
        },
        "nodes": nodes,
        "edges": [
            {"from": blocker, "to": dependent}
            for blocker, dependent in sorted(edge_set)
        ],
        "warnings": sorted(set(warnings)),
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    return value


def _columns_payload(cfg: ServiceConfig) -> list[dict[str, Any]]:
    descriptions = {
        k.lower(): v for k, v in (cfg.tracker.state_descriptions or {}).items()
    }
    stage_paths = {k.lower(): str(v) for k, v in cfg.prompts.stage_paths.items()}
    out: list[dict[str, Any]] = []
    for name in cfg.tracker.active_states:
        out.append(
            {
                "name": name,
                "terminal": False,
                "description": descriptions.get(name.lower(), ""),
                "has_prompt": name.lower() in stage_paths,
            }
        )
    for name in cfg.tracker.terminal_states:
        out.append(
            {
                "name": name,
                "terminal": True,
                "description": descriptions.get(name.lower(), ""),
                "has_prompt": False,
            }
        )
    return out


def _continuous_improvement_payload(cfg: ServiceConfig) -> dict[str, Any]:
    ci = cfg.continuous_improvement
    return {
        "enabled": ci.enabled,
        "interval_ms": ci.interval_ms,
        "max_turns": ci.max_turns,
        "agent_kind": ci.agent_kind,
        "ticket_prefix": ci.ticket_prefix,
        "max_tickets_per_run": ci.max_tickets_per_run,
        "require_idle_board": ci.require_idle_board,
        "modes": list(ci.modes),
        "resolved_modes": list(ci.resolved_modes()),
        "supported_modes": list(SUPPORTED_CI_MODES),
        "mode_interval_hours": {
            mode: ci.interval_hours_for(mode) for mode in SUPPORTED_CI_MODES
        },
        "max_improvement_tickets_per_run": ci.max_improvement_tickets_per_run,
    }


def _workflow_payload(cfg: ServiceConfig) -> dict[str, Any]:
    return {
        "workflow_path": str(cfg.workflow_path),
        "columns": _columns_payload(cfg),
        "agent": {
            "kind": cfg.agent.kind,
            "max_concurrent_agents": cfg.agent.max_concurrent_agents,
            "max_turns": cfg.agent.max_turns,
            "max_attempts": cfg.agent.max_attempts,
            "scheduling_policy": cfg.agent.scheduling_policy,
            "feature_base_branch": cfg.agent.feature_base_branch,
            "auto_merge_target_branch": cfg.agent.auto_merge_target_branch,
            "auto_merge_on_done": cfg.agent.auto_merge_on_done,
            "auto_merge_push_target": cfg.agent.auto_merge_push_target,
            "merge_delivery": (
                "upstream-publishing"
                if cfg.agent.auto_merge_push_target
                else "local-only"
            ),
            # F-06: the mechanical evidence floor is lane-name gated by
            # default, so the UI must be able to say when it is off.
            "stage_contracts": cfg.agent.stage_contracts,
            "stage_contracts_enabled": cfg.agent.stage_contracts_enabled(
                cfg.tracker.active_states
            ),
        },
        "agent_kinds": sorted(selectable_agent_kinds(cfg.agent.profiles)),
        "continuous_improvement": _continuous_improvement_payload(cfg),
        "preview": {
            "enabled": cfg.preview.enabled,
            "cwd": cfg.preview.cwd,
            "health_path": cfg.preview.health_path,
            "url_path": cfg.preview.url_path,
            "release_ticket": cfg.preview.release_ticket,
            "acceptance": list(cfg.preview.acceptance),
        },
        "polling_interval_ms": cfg.poll_interval_ms,
    }


def _live_by_identifier(orchestrator: Orchestrator) -> dict[str, dict[str, Any]]:
    snapshot = orchestrator.snapshot()
    live: dict[str, dict[str, Any]] = {}
    for row in snapshot.get("running", []):
        live[row["issue_identifier"]] = {"status": "running", **row}
    for row in snapshot.get("retrying", []):
        identifier = row.get("issue_identifier") or row.get("identifier") or ""
        live[identifier] = {"status": "retrying", **row}
    return live
