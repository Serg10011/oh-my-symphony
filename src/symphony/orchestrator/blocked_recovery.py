"""Done-gate holds, Blocked RCA tickets, and blocked-source recovery for ``Orchestrator``.

Extracted from ``core.Orchestrator`` (LR-3 lean-core split). Each method is a
module-level function whose first parameter is ``self: Orchestrator``;
``core.Orchestrator`` binds it as a class attribute, so ``self.<name>(...)``
calls, ``Orchestrator.<name>`` patches, and behaviour are unchanged.
Collaborators that tests monkeypatch on ``symphony.orchestrator.core`` are read
through the late-bound ``_core()`` handle at call time, never bound here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import asyncio
import re
from dataclasses import replace
from pathlib import Path

from ..errors import SymphonyError
from ..issue import Issue, normalize_state
from ..workflow import ServiceConfig, selectable_agent_kinds, SYMPHONY_BRANCH_PREFIX
from ..utils.auto_merge import AutoMergeResult
from ..utils.git_sandbox import SANDBOX_WRITE_DENIED, classify_history_failure
from ..workspace import HISTORY_PUSH_FAILED, CommitOutcome, HistoryGateResult
from .constants import AUTO_TRIAGE_NOTE, AUTO_TRIAGE_TARGET_STATE
from .entries import _IssueDebug
from .helpers import (
    _is_successful_terminal_state,
    _is_auto_triage_todo_candidate,
    _rewind_budget_target_state,
    _sort_for_dispatch_fifo,
    _terminal_state_matching,
)
from .core_support import (
    _BLOCKED_RCA_HEADING_RE,
    _BLOCKED_RCA_RESOLVED_HEADING_RE,
    _REOPEN_APPROVED_RE,
    _blocked_rca_already_requested,
    _blocker_dependency_is_resolved,
)
from ..logging import get_logger

if TYPE_CHECKING:
    from .core import Orchestrator

log = get_logger()


def _core():
    """Late-bound handle to ``symphony.orchestrator.core`` (imports this module)."""
    from . import core

    return core


def _blocked_rca_work_state(cfg: ServiceConfig) -> str:
    for state in cfg.tracker.active_states:
        if normalize_state(state) == "in progress":
            return state
    return cfg.tracker.active_states[0] if cfg.tracker.active_states else "Todo"


def _blocked_source_reopen_state(cfg: ServiceConfig) -> str:
    for state in cfg.tracker.active_states:
        if normalize_state(state) == "todo":
            return state
    return cfg.tracker.active_states[0] if cfg.tracker.active_states else "Todo"


def _blocked_rca_labels(issue: Issue) -> list[str]:
    labels: list[str] = []
    for label in ("blocked-fix", f"source-{issue.identifier.lower()}"):
        normalized = re.sub(r"[^a-z0-9_.-]+", "-", label.lower()).strip("-")
        if normalized and normalized not in labels:
            labels.append(normalized)
    for label in issue.labels:
        if label not in labels:
            labels.append(label)
    return labels


def _blocked_rca_identifier_prefix(issue: Issue) -> str:
    source = re.sub(r"[^A-Za-z0-9]+", "-", issue.identifier).strip("-")
    return f"FIX-{source or 'SOURCE'}"


_BLOCKED_RCA_HOST_RESOLVED_HEADING_RE = re.compile(
    r"^##\s+Blocked\s+(?:Fix|RCA)\s+Resolved\s*$",
    re.IGNORECASE | re.MULTILINE,
)


_BLOCKED_RCA_SOURCE_IDENTIFIER_RE = re.compile(
    r"^-\s*Identifier:\s*`(?P<identifier>[^`]+)`\s*$",
    re.IGNORECASE | re.MULTILINE,
)


_BLOCKED_RCA_TITLE_RE = re.compile(
    r"^(?:RCA\s+unblock|Fix\s+and\s+unblock)\s+(?P<identifier>[^:]+):",
    re.IGNORECASE,
)


_BLOCKED_RCA_OPERATOR_BLOCKER_RE = re.compile(
    r"^##\s+((?:Fix|RCA)\s+Blocker|Operator\s+Action|Intervention\s+Required)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


_HUMAN_REVIEW_INTERVENTION_RE = re.compile(
    r"^(##\s+(Operator\s+Action|Intervention\s+Required|History\s+Failure|"
    r"Merge\s+Missing)|###\s+Intervention\s+Required)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


_HUMAN_REVIEW_DO_NOT_CONFIRM_RE = re.compile(
    r"^\s*`?Do\s+not\s+confirm\b",
    re.IGNORECASE | re.MULTILINE,
)


_HUMAN_REVIEW_CONFIRM_DONE_RE = re.compile(
    r"^\s*`?Confirm\s+Done`?\.?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


_HUMAN_REVIEW_COMPLETION_RE = re.compile(
    r"^##\s+(As-Is\s*->\s*To-Be\s+Report|Unblock\s+Note)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


_HUMAN_REVIEW_MERGE_FAILURE_RE = re.compile(
    r"^##\s+Merge\s+Failure\s*$",
    re.IGNORECASE | re.MULTILINE,
)


_HISTORY_FAILURE_HEADING_RE = re.compile(
    r"^##\s+History\s+Failure\s*$",
    re.IGNORECASE | re.MULTILINE,
)


_HISTORY_RECOVERY_HEADING_RE = re.compile(
    r"^##\s+History\s+Recovery\s*$",
    re.IGNORECASE | re.MULTILINE,
)


_NEXT_H2_RE = re.compile(r"^##\s+", re.MULTILINE)


def _markdown_section(description: str, heading_re: re.Pattern[str]) -> str:
    """Body under the first heading matching ``heading_re``, up to the next `## `.

    Scoping the read to one section matters for failure classification: a
    ticket description also carries QA logs and prior-run notes, and matching
    a phrase like `permission denied` anywhere in the body would mislabel
    unrelated tickets.
    """
    match = heading_re.search(description or "")
    if match is None:
        return ""
    rest = description[match.end() :]
    nxt = _NEXT_H2_RE.search(rest)
    return (rest[: nxt.start()] if nxt else rest).strip()


def _is_blocked_rca_ticket(issue: Issue) -> bool:
    return any(
        label.lower() in {"blocked-fix", "blocked-rca"} for label in issue.labels
    )


def _is_blocked_fix_ticket(issue: Issue) -> bool:
    return any(label.lower() == "blocked-fix" for label in issue.labels)


def _looks_like_blocked_rca_ticket(issue: Issue) -> bool:
    if _is_blocked_rca_ticket(issue):
        return True
    return bool(_BLOCKED_RCA_TITLE_RE.match(issue.title or ""))


def _blocked_rca_source_identifier(issue: Issue) -> str | None:
    description = issue.description or ""
    match = _BLOCKED_RCA_SOURCE_IDENTIFIER_RE.search(description)
    if match is not None:
        return match.group("identifier").strip()
    match = _BLOCKED_RCA_TITLE_RE.match(issue.title or "")
    if match is not None:
        return match.group("identifier").strip()
    for label in issue.labels:
        normalized = label.lower()
        if normalized.startswith("source-"):
            return label[len("source-") :].strip()
    return None


def _blocked_rca_current_episode_resolved(issue: Issue) -> bool:
    body = issue.description or ""
    resolutions = list(_BLOCKED_RCA_RESOLVED_HEADING_RE.finditer(body))
    if not resolutions:
        return False
    requests = list(_BLOCKED_RCA_HEADING_RE.finditer(body))
    return not requests or resolutions[-1].start() > requests[-1].start()


def _blocked_rca_requires_operator_intervention(issue: Issue) -> bool:
    body = issue.description or ""
    blockers = list(_BLOCKED_RCA_OPERATOR_BLOCKER_RE.finditer(body))
    if not blockers:
        return False
    resolutions = list(_BLOCKED_RCA_RESOLVED_HEADING_RE.finditer(body))
    return not resolutions or blockers[-1].start() > resolutions[-1].start()


def _human_review_done_state(cfg: ServiceConfig) -> str | None:
    for state in cfg.tracker.terminal_states:
        if normalize_state(state).strip() == "done":
            return state
    return None


def _human_review_requires_operator_intervention(issue: Issue) -> bool:
    body = issue.description or ""
    return bool(
        _blocked_rca_requires_operator_intervention(issue)
        or _HUMAN_REVIEW_INTERVENTION_RE.search(body)
        or _HUMAN_REVIEW_DO_NOT_CONFIRM_RE.search(body)
    )


def _legacy_human_review_is_done(issue: Issue) -> bool:
    if normalize_state(issue.state).strip() != "human review":
        return False
    if _looks_like_blocked_rca_ticket(issue):
        return False
    body = issue.description or ""
    if not body or _human_review_requires_operator_intervention(issue):
        return False
    if _HUMAN_REVIEW_MERGE_FAILURE_RE.search(body) and not re.search(
        r"^##\s+Unblock\s+Note\s*$",
        body,
        re.IGNORECASE | re.MULTILINE,
    ):
        return False
    return bool(
        _HUMAN_REVIEW_CONFIRM_DONE_RE.search(body)
        or _HUMAN_REVIEW_COMPLETION_RE.search(body)
    )


def _blocked_rca_description(
    issue: Issue,
    *,
    reopen_state: str,
) -> str:
    return (
        "## Goal\n\n"
        f"Make `{issue.identifier}` actionable again and remove the verified blocker "
        "so automated delivery can continue.\n\n"
        "## Source Ticket\n\n"
        f"- Identifier: `{issue.identifier}`\n"
        f"- Title: {issue.title}\n"
        f"- Current state: `{issue.state}`\n"
        f"- Reopen target after proven fix: `{reopen_state}`\n\n"
        "## Required Sequence\n\n"
        f"1. Read the source ticket `{issue.identifier}` first, including its title, "
        "request, description, acceptance criteria, and latest `## Blocker`, "
        "`## Budget Exceeded`, `## QA Failure`, `## Review Findings`, or "
        "legacy `## Blocked RCA` sections.\n"
        "2. Compare the requested outcome and acceptance criteria with the failure "
        "evidence. Ambiguity in the request or acceptance criteria is itself a blocker; "
        "do not merely restate it.\n"
        f"3. When instructions are vague, incomplete, or not testable, append "
        f"`## Clarified Request` to `{issue.identifier}` with the intended outcome, "
        "in-scope work, non-goals, constraints, assumptions, concrete, testable "
        "acceptance criteria, and exact verification commands or artifacts. Preserve "
        "the operator's intent; do not invent consequential product decisions.\n"
        "4. Resolve any code, configuration, environment, or process defect that is "
        "safe for an agent to change, then verify the fix with concrete commands or "
        "artifacts. If clarification is the only blocker, the improved source "
        "instructions are the fix.\n"
        f"5. Only after the blocker is resolved and the source ticket is actionable, "
        f"append `## Fix Resolution` to both `{issue.identifier}` and this fix ticket, "
        f"then move that source ticket to `{reopen_state}`. Do not move the fix ticket "
        "to Done before both updates are persisted.\n"
        "6. Do not skip the source ticket's normal workflow. Once it is back in Todo, "
        "it must pass through the configured Todo/In Progress/Verify/Document review "
        "path like any other ticket.\n"
        "7. If the blocker requires credentials, destructive operations, external "
        "approval, or a consequential product decision, leave the source ticket "
        "Blocked and append `## Fix Blocker` with the exact operator action or "
        "decision required.\n\n"
        "## Done Evidence\n\n"
        "- Root cause category and evidence.\n"
        "- Source instructions changed: yes/no, with the resulting acceptance criteria "
        "or the reason no clarification was needed.\n"
        "- Fix and verification evidence, or exact operator action required.\n"
        f"- Final state decision for `{issue.identifier}`.\n"
    )


async def _after_done_then_remove_per_policy(
    self: Orchestrator,
    cfg: "ServiceConfig",
    path: Path,
    *,
    identifier: str,
    title: str,
    debug_target: "_IssueDebug | None",
) -> None:
    """Fire `after_done` hook and remove the workspace per failure policy.

        Default policy `warn`: hook failure logs and the workspace is
        removed anyway (legacy behaviour — a failed hook can look like a
        clean Done). Policy `block`: hook failure preserves the workspace
        and records `last_error` on the debug entry so the operator can
        investigate before the worktree is reaped. Pair `block` with a
        production-critical `after_done` script (deploy, host-apply).
        """
    if self._workspace_manager is None:
        return
    ok = await self._workspace_manager.after_done_best_effort(
        path, identifier=identifier, title=title
    )
    if not ok and cfg.agent.after_done_failure_policy == "block":
        log.warning(
            "after_done_block_workspace_preserved",
            identifier=identifier,
            path=str(path),
        )
        if debug_target is not None:
            debug_target.last_error = (
                "after_done failed; workspace preserved (policy=block) "
                "— operator action required"
            )
        return
    await self._workspace_manager.remove(path)


async def _preserve_history_failure(
    self: Orchestrator,
    cfg: ServiceConfig,
    issue: Issue,
    workspace_path: Path,
    outcome: CommitOutcome | HistoryGateResult,
) -> Issue:
    """Stop delivery and cleanup when the host could not snapshot this work."""
    message = (
        "Git history snapshot failed; workspace preserved; operator action required"
    )
    debug = self._issue_debug.setdefault(issue.id, _IssueDebug())
    debug.last_workspace = workspace_path
    debug.last_error = message
    self._paused_issue_ids.add(issue.id)
    self._pause_reasons[issue.id] = message
    self._set_issue_flags(issue.id, paused=True, pause_reason=message)
    target = _terminal_state_matching(cfg, "block", "human")
    if not target and cfg.tracker.active_states:
        target = cfg.tracker.active_states[-1]
    note = (
        f"{message}.\n\n"
        "The host could not record this workspace in Git. Delivery and cleanup "
        "were stopped, and the workspace and its product files were preserved.\n\n"
        f"- status: `{outcome.status}`\n"
        f"- classification: `{outcome.failure_kind}`\n"
        f"- workspace: `{workspace_path}`\n"
        f"- detail: {outcome.detail[:1000]}\n\n"
        "Fix the reported Git/index problem without deleting the workspace. "
        "Then return this ticket to its verification lane and resume it through "
        "the operator resume control. Do not treat this ticket as delivered."
    )
    try:
        if target:
            await asyncio.to_thread(
                self._tracker_call_update_state, cfg, issue, target
            )
            issue = replace(issue, state=target)
        await asyncio.to_thread(
            self._tracker_call_append_note, cfg, issue, "Blocker", note
        )
    except Exception as exc:
        self._record_tracker_error(issue.id, exc)
        log.error(
            "history_failure_persist_failed",
            identifier=issue.identifier,
            error=str(exc),
        )
    self._record_stats_gate(
        issue.identifier, issue.state, "history_commit_failed", (outcome.detail,)
    )
    log.error(
        "history_commit_failed_workspace_preserved",
        identifier=issue.identifier,
        path=str(workspace_path),
        detail=outcome.detail,
    )
    return issue


async def _block_done_ticket_for_merge_gate(
    self: Orchestrator,
    cfg: "ServiceConfig",
    issue: Issue,
    workspace_path: Path,
    *,
    result: AutoMergeResult,
    debug_target: "_IssueDebug | None",
) -> None:
    branch = f"{SYMPHONY_BRANCH_PREFIX}{issue.identifier}"
    target = cfg.agent.auto_merge_target_branch or "(current branch)"
    detail = result.detail.strip()
    note_body = (
        f"Symphony could not merge `{branch}` into `{target}` after this "
        "ticket reached `Done`, so the ticket was moved to `Blocked` to "
        "prevent dependents from running against an incomplete target branch.\n\n"
        f"- status: `{result.status}`\n"
        f"- workspace preserved: `{workspace_path}`"
    )
    if detail:
        note_body = f"{note_body}\n- detail: {detail[:1000]}"
    if debug_target is not None:
        debug_target.last_error = (
            f"auto_merge failed ({result.status}); moved to Blocked; "
            "workspace preserved"
        )
    try:
        await asyncio.to_thread(
            self._tracker_call_update_state,
            cfg,
            issue,
            "Blocked",
        )
        await asyncio.to_thread(
            self._tracker_call_append_note,
            cfg,
            issue,
            "Merge Gate Failed",
            note_body,
        )
        log.warning(
            "auto_merge_gate_blocked_ticket",
            identifier=issue.identifier,
            branch=branch,
            target=target,
            status=result.status,
            path=str(workspace_path),
        )
    except Exception as exc:
        log.warning(
            "auto_merge_gate_block_persist_failed",
            identifier=issue.identifier,
            branch=branch,
            target=target,
            status=result.status,
            error=str(exc),
            path=str(workspace_path),
        )


async def _auto_merge_done_gate_or_block(
    self: Orchestrator,
    cfg: "ServiceConfig",
    issue: Issue,
    workspace_path: Path,
    *,
    debug_target: "_IssueDebug | None",
) -> bool:
    if not cfg.agent.auto_merge_on_done:
        return True
    result = await _core().auto_merge_on_done_best_effort(
        workflow_dir=cfg.workflow_path.parent,
        branch=f"{SYMPHONY_BRANCH_PREFIX}{issue.identifier}",
        identifier=issue.identifier,
        title=issue.title,
        target_branch=cfg.agent.auto_merge_target_branch,
        exclude_paths=cfg.agent.auto_merge_exclude_paths,
        capture_untracked=cfg.agent.auto_merge_capture_untracked,
        push_target=cfg.agent.auto_merge_push_target,
    )
    if result is None or result.ok:
        if result is not None and result.status in {"merged", "nothing_to_apply"}:
            await self._resolve_delivered_factory_blockers(cfg, issue)
        return True
    await self._block_done_ticket_for_merge_gate(
        cfg,
        issue,
        workspace_path,
        result=result,
        debug_target=debug_target,
    )
    return False


async def _resolve_delivered_factory_blockers(
    self: Orchestrator, cfg: ServiceConfig, issue: Issue
) -> None:
    if not self._is_factory_profile(cfg) or cfg.tracker.kind != "file":
        return
    from ..trackers.file import FileBoardTracker

    try:
        await asyncio.to_thread(
            FileBoardTracker(cfg.tracker)._resolve_factory_recovery_blockers,
            issue.identifier,
        )
    except Exception as exc:
        log.warning(
            "factory_recovery_history_update_failed",
            identifier=issue.identifier, error=str(exc),
        )


async def _auto_normalize_legacy_human_review_done(self: Orchestrator, cfg: ServiceConfig) -> int:
    """Move legacy completion handoffs out of Human Review.

        Current prompts reserve Human Review for real manual intervention.
        Older file boards used it as the normal "Confirm Done" lane, which
        freezes dependencies once Human Review becomes intervention-only.
        """
    if cfg.tracker.kind != "file":
        return 0
    done_state = _human_review_done_state(cfg)
    if done_state is None:
        return 0
    try:
        terminal_issues = await asyncio.to_thread(
            self._tracker_call_terminal_issues, cfg
        )
    except Exception as exc:
        log.warning("human_review_normalize_fetch_failed", error=str(exc))
        return 0
    moved = 0
    for issue in terminal_issues:
        if not _legacy_human_review_is_done(issue):
            continue
        note_body = (
            "Moved this legacy `Human Review` card to `Done` because the "
            "current workflow reserves `Human Review` for critical/manual "
            "intervention, and this card contains completion evidence with "
            "no intervention marker."
        )
        try:
            await asyncio.to_thread(
                self._tracker_call_append_note,
                cfg,
                issue,
                "Human Review Normalized",
                note_body,
            )
            await asyncio.to_thread(
                self._tracker_call_update_state,
                cfg,
                issue,
                done_state,
            )
        except Exception as exc:
            log.warning(
                "human_review_normalize_failed",
                identifier=issue.identifier,
                error=str(exc),
            )
            self._record_tracker_error(issue.id, exc)
            continue
        self._clear_tracker_error(issue.id)
        moved += 1
        log.info(
            "human_review_normalized_done",
            identifier=issue.identifier,
            target=done_state,
        )
    return moved


def _tracker_call_link_blocked_fix(
    cfg: ServiceConfig, source_issue: Issue, fix_identifier: str
) -> bool:
    client = _core().build_tracker_client(cfg)
    try:
        update_fields = getattr(client, "update_fields", None)
        if not callable(update_fields):
            return False
        persisted = client.fetch_issue_full_by_id(source_issue.identifier)
        if persisted is None:
            return False
        current = [
            blocker.identifier or blocker.id
            for blocker in persisted.blocked_by
            if blocker.identifier or blocker.id
        ]
        if fix_identifier not in current:
            update_fields(
                source_issue.identifier,
                blocked_by=[*current, fix_identifier],
            )
        return True
    except Exception as exc:
        log.warning(
            "blocked_fix_dependency_link_failed",
            source_identifier=source_issue.identifier,
            fix_identifier=fix_identifier,
            error=str(exc),
        )
        return False
    finally:
        client.close()


def _tracker_call_active_rca_for_source(
    cfg: ServiceConfig, source_identifier: str
) -> str | None:
    """Return the persisted active RCA for a source, if one exists."""
    client = _core().build_tracker_client(cfg)
    try:
        # Blocked/Human Review RCA cards are still unresolved work even
        # when the workflow models those parking lanes as terminal. Only
        # completed/discarded RCA states stop suppressing duplicates.
        resolved_states = {"done", "archive", "cancelled"}
        scan_states = tuple(
            dict.fromkeys(
                (*cfg.tracker.active_states, *cfg.tracker.terminal_states)
            )
        )
        unresolved_states = tuple(
            state
            for state in scan_states
            if normalize_state(state) not in resolved_states
        )
        issues = client.fetch_issues_by_states(unresolved_states)
    finally:
        client.close()
    source_key = source_identifier.casefold()
    for candidate in issues:
        if not _looks_like_blocked_rca_ticket(candidate):
            continue
        candidate_source = _blocked_rca_source_identifier(candidate)
        if candidate_source and candidate_source.casefold() == source_key:
            return candidate.identifier
    return None


def _tracker_call_create_blocked_rca_issue(
    cfg: ServiceConfig,
    issue: Issue,
    rca_state: str,
    reopen_state: str,
    agent_kind: str,
) -> str | None:
    client = _core().build_tracker_client(cfg)
    try:
        create_with_next_identifier = getattr(
            client, "create_with_next_identifier", None
        )
        if not callable(create_with_next_identifier):
            return None
        created = create_with_next_identifier(
            _blocked_rca_identifier_prefix(issue),
            title=f"Fix and unblock {issue.identifier}: {issue.title}",
            state=rca_state,
            priority=issue.priority,
            labels=_blocked_rca_labels(issue),
            description=_blocked_rca_description(
                issue,
                reopen_state=reopen_state,
            ),
            agent_kind=agent_kind,
        )
        if not isinstance(created, tuple) or not created:
            raise SymphonyError("tracker returned invalid created-ticket payload")
        return str(created[0])
    finally:
        client.close()


async def recover_blocked_issue(
    self: Orchestrator,
    identifier: str,
    *,
    target_state: str | None = None,
    agent_kind: str | None = None,
) -> tuple[bool, str, dict[str, str]]:
    """Open an RCA ticket for a Blocked issue without reopening it first."""
    cfg = self._workflow_state.current()
    if cfg is None:
        cfg, err = self._workflow_state.reload()
        if cfg is None:
            return False, f"workflow config unavailable: {err}", {}

    if self.find_running_issue_id(identifier) is not None:
        return False, f"{identifier} has a running worker; wait or pause first", {}

    issue = await asyncio.to_thread(
        self._tracker_call_fetch_issue_full_by_id, cfg, identifier
    )
    if issue is None:
        return False, f"unknown issue {identifier}", {}
    if normalize_state(issue.state) != "blocked":
        return (
            False,
            f"only Blocked tickets can be recovered (state={issue.state})",
            {},
        )
    if self.find_running_issue_id(identifier) is not None:
        return False, f"{identifier} started running; retry after it stops", {}

    return await self._open_blocked_rca_for_issue(
        cfg,
        issue,
        target_state=target_state,
        agent_kind=agent_kind,
        manual=True,
    )


async def _recover_blocked_history_gate(
    self: Orchestrator, cfg: ServiceConfig, issue: Issue
) -> bool:
    """Rescue a ticket blocked on a git write its sandbox never allowed.

        The agent runs the Final History Gate from inside a sandbox that may
        not reach the object database (``utils.git_sandbox``), so a finished
        ticket could self-park in ``Blocked`` on a housekeeping commit —
        while the host's own snapshot commit, running unsandboxed moments
        later, had already recorded the same work. Opening an RCA ticket for
        that is worse than useless: the RCA worker inherits the identical
        sandbox, blocks the same way, and cannot spawn a further RCA, which
        is where the board stops moving entirely.

        So before any RCA, the host re-checks the claim against real git
        state. Returns True when the ticket was moved and no RCA is needed.
        """
    description = issue.description or ""
    blocker = _markdown_section(description, _HISTORY_FAILURE_HEADING_RE)
    if not blocker or classify_history_failure(blocker) != SANDBOX_WRITE_DENIED:
        return False

    branch = f"{SYMPHONY_BRANCH_PREFIX}{issue.identifier}"
    # One rescue per ticket. A second identical block means the retry did
    # not help, and looping a ticket through the pipeline forever is a
    # worse failure mode than handing it to a human.
    already_recovered = bool(_HISTORY_RECOVERY_HEADING_RE.search(description))
    result = await _core().verify_branch_history(
        cfg.workflow_path.parent,
        branch=branch,
        push=cfg.agent.auto_merge_push_target,
    )

    if not result.durable:
        log.info(
            "history_recovery_declined",
            identifier=issue.identifier,
            branch=branch,
            status=result.status,
        )
        return False

    published = (
        f"remote `{result.remote_sha[:12]}`"
        if result.remote_sha
        else "no upstream configured (local history only)"
    )
    note = (
        "Symphony re-checked this ticket's git history from the host repo, "
        "outside the agent sandbox that produced the failure.\n\n"
        f"- branch: `{branch}`\n"
        f"- local commit: `{result.local_sha[:12] or 'none'}`\n"
        f"- published: {published}\n"
        f"- host gate status: `{result.status}`\n\n"
        "The delivery record is in git history, so the reported "
        "`git add` failure was a sandbox permission limit, not lost work."
    )

    if result.status == HISTORY_PUSH_FAILED or already_recovered:
        reason = (
            "the remote tip could not be verified"
            if result.status == HISTORY_PUSH_FAILED
            else "this ticket was already rescued once and blocked again"
        )
        target = "Human Review"
        note = f"{note}\n\nMoved to `Human Review` because {reason}."
    else:
        target = _blocked_source_reopen_state(cfg)
        note = (
            f"{note}\n\nReopened into `{target}` for one automated retry; "
            "the history gate now runs on the host, so the same sandbox "
            "limit cannot block it again."
        )

    try:
        await asyncio.to_thread(
            self._tracker_call_append_note, cfg, issue, "History Recovery", note
        )
        await asyncio.to_thread(self._tracker_call_update_state, cfg, issue, target)
    except Exception as exc:
        log.warning(
            "history_recovery_persist_failed",
            identifier=issue.identifier,
            branch=branch,
            error=str(exc),
        )
        return False

    log.info(
        "history_recovery_applied",
        identifier=issue.identifier,
        branch=branch,
        status=result.status,
        target_state=target,
        local_sha=result.local_sha,
        remote_sha=result.remote_sha,
    )
    self.request_refresh()
    return True


async def _open_blocked_rca_for_issue(
    self: Orchestrator,
    cfg: ServiceConfig,
    issue: Issue,
    *,
    target_state: str | None = None,
    agent_kind: str | None = None,
    manual: bool = False,
) -> tuple[bool, str, dict[str, str]]:
    if _is_blocked_rca_ticket(issue):
        return False, f"{issue.identifier} is already a fix ticket", {}

    reopen_state = _blocked_source_reopen_state(cfg)
    rca_state = _blocked_rca_work_state(cfg)
    if target_state is not None:
        active_by_key = {
            normalize_state(state): state for state in cfg.tracker.active_states
        }
        requested_rca_state = active_by_key.get(normalize_state(target_state))
        if requested_rca_state is None:
            return (
                False,
                f"target_state must be one of active states: {list(cfg.tracker.active_states)}",
                {},
            )
        rca_state = requested_rca_state

    requested_agent = (
        agent_kind or ""
    ).strip().lower() or cfg.agent.kind_for_state(rca_state, issue.agent_kind)
    if requested_agent not in selectable_agent_kinds(cfg.agent.profiles):
        requested_agent = cfg.agent.kind

    async with self._blocked_rca_creation_lock:
        # The board is authoritative. If a previous create succeeded but
        # its source edge/note did not, reconcile that partial write rather
        # than suppressing the retry or creating a second FIX ticket.
        active_rca = await asyncio.to_thread(
            self._tracker_call_active_rca_for_source,
            cfg,
            issue.identifier,
        )
        if active_rca is not None:
            await asyncio.to_thread(
                self._tracker_call_link_blocked_fix,
                cfg,
                issue,
                active_rca,
            )
            if not _blocked_rca_already_requested(issue):
                try:
                    await asyncio.to_thread(
                        self._tracker_call_append_note,
                        cfg,
                        issue,
                        "Blocked Fix",
                        f"Fix ticket `{active_rca}` is already open. Symphony "
                        "reconciled the source dependency after an interrupted "
                        "recovery write; the source remains Blocked until the "
                        "fix is proven.",
                    )
                except Exception as exc:
                    # Keep deduplication authoritative even if the source was
                    # concurrently removed or its note cannot yet be repaired.
                    log.warning(
                        "blocked_fix_source_note_reconcile_failed",
                        source_identifier=issue.identifier,
                        fix_identifier=active_rca,
                        error=str(exc),
                    )
            self._blocked_rca_source_ids.add(issue.id)
            return (
                False,
                f"blocked fix already opened for {issue.identifier}",
                {},
            )
        if (
            _blocked_rca_already_requested(issue)
            or issue.id in self._blocked_rca_source_ids
        ):
            self._blocked_rca_source_ids.add(issue.id)
            return (
                False,
                f"blocked fix already opened for {issue.identifier}",
                {},
            )
        rca_identifier = await asyncio.to_thread(
            self._tracker_call_create_blocked_rca_issue,
            cfg,
            issue,
            rca_state,
            reopen_state,
            requested_agent,
        )
        if rca_identifier is None:
            return (
                False,
                "blocked fix creation requires a tracker that can create tickets",
                {},
            )

        body = (
            f"Fix ticket `{rca_identifier}` opened in `{rca_state}` for "
            f"`{requested_agent}` worker dispatch.\n\n"
            f"This source ticket remains `{issue.state}`. The fix worker must "
            "resolve and verify the root cause before moving this ticket back "
            f"to `{reopen_state}`. After that reopen, the source ticket still "
            "must pass the normal configured workflow. If the root cause cannot "
            "be resolved safely by an agent, the fix worker should leave this "
            "ticket Blocked with exact operator action."
        )
        if not manual:
            body = "Opened automatically by the orchestrator.\n\n" + body
        await asyncio.to_thread(
            self._tracker_call_link_blocked_fix,
            cfg,
            issue,
            rca_identifier,
        )
        await asyncio.to_thread(
            self._tracker_call_append_note,
            cfg,
            issue,
            "Blocked Fix",
            body,
        )
        # Only mark the in-process episode after its durable source note is
        # present. Partial creates are repaired by the active-board path.
        self._blocked_rca_source_ids.add(issue.id)

    self._record_stats_transition(rca_identifier, "", rca_state)
    self.request_refresh()
    return (
        True,
        f"{rca_identifier} opened to unblock {issue.identifier}; "
        f"{issue.identifier} remains {issue.state}",
        {
            "original_state": issue.state,
            "target_state": reopen_state,
            "source_reopen_state": reopen_state,
            "fix_identifier": rca_identifier,
            "fix_state": rca_state,
            # Deprecated response aliases retained for API compatibility.
            "rca_identifier": rca_identifier,
            "rca_state": rca_state,
            "agent_kind": requested_agent,
        },
    )


async def _auto_recover_blocked_sources(self: Orchestrator, cfg: ServiceConfig) -> int:
    if not cfg.agent.auto_recover_blocked:
        return 0
    slots = self._available_slots(cfg)
    if slots <= 0:
        return 0
    try:
        terminal_issues = await asyncio.to_thread(
            self._tracker_call_terminal_issues, cfg
        )
    except Exception as exc:
        log.warning("blocked_rca_fetch_failed", error=str(exc))
        return 0

    opened = 0
    for issue in _sort_for_dispatch_fifo(terminal_issues, cfg):
        if issue.id in self._paused_issue_ids:
            continue
        if opened >= slots:
            break
        if normalize_state(issue.state) != "blocked":
            self._blocked_rca_source_ids.discard(issue.id)
            self._history_recovery_attempted.discard(issue.id)
            continue
        recovery_pending = issue.id not in self._history_recovery_attempted
        if not recovery_pending and _is_blocked_rca_ticket(issue):
            continue

        full_issue = issue
        if full_issue.description is None:
            fetched = await asyncio.to_thread(
                self._tracker_call_fetch_issue_full_by_id,
                cfg,
                issue.id or issue.identifier,
            )
            if fetched is not None:
                full_issue = fetched

        # Host-side rescue runs before any RCA, and deliberately also runs
        # for RCA tickets: an RCA blocked by the same sandbox limit is the
        # dead end, because it cannot open a further RCA of its own.
        if recovery_pending:
            self._history_recovery_attempted.add(issue.id)
            try:
                if await self._recover_blocked_history_gate(cfg, full_issue):
                    self._clear_tracker_error(issue.id)
                    continue
            except Exception as exc:
                log.warning(
                    "history_recovery_errored",
                    identifier=issue.identifier,
                    error=str(exc),
                )
        if _is_blocked_rca_ticket(issue):
            continue

        try:
            changed, message, _details = await self._open_blocked_rca_for_issue(
                cfg,
                full_issue,
                manual=False,
            )
        except Exception as exc:
            log.warning(
                "blocked_rca_auto_failed",
                identifier=issue.identifier,
                error=str(exc),
            )
            self._record_tracker_error(issue.id, exc)
            continue
        if changed:
            self._clear_tracker_error(issue.id)
            opened += 1
            log.info(
                "blocked_rca_auto_opened",
                identifier=issue.identifier,
                detail=message,
            )
        else:
            log.info(
                "blocked_rca_auto_skipped",
                identifier=issue.identifier,
                reason=message,
            )
    return opened


async def _resolved_blocked_rca_issue(
    self: Orchestrator, cfg: ServiceConfig, issue: Issue
) -> Issue | None:
    if not _looks_like_blocked_rca_ticket(issue):
        return None
    if not _blocker_dependency_is_resolved(issue.state, cfg):
        return None
    if _is_blocked_rca_ticket(issue) and issue.description is not None:
        return issue
    fetched = await asyncio.to_thread(
        self._tracker_call_fetch_issue_full_by_id,
        cfg,
        issue.id or issue.identifier,
    )
    if fetched is None or not _looks_like_blocked_rca_ticket(fetched):
        return None
    return fetched


async def _source_issue_for_blocked_rca(
    self: Orchestrator,
    cfg: ServiceConfig,
    rca_issue: Issue,
    terminal_by_identifier: dict[str, Issue],
) -> Issue | None:
    source_identifier = _blocked_rca_source_identifier(rca_issue)
    if not source_identifier:
        log.warning(
            "blocked_rca_resolution_missing_source",
            identifier=rca_issue.identifier,
        )
        return None
    source_issue = terminal_by_identifier.get(source_identifier.casefold())
    if source_issue is not None and source_issue.description is not None:
        return source_issue
    fetched_source = await asyncio.to_thread(
        self._tracker_call_fetch_issue_full_by_id,
        cfg,
        source_identifier,
    )
    if fetched_source is None:
        log.warning(
            "blocked_rca_resolution_source_missing",
            rca_identifier=rca_issue.identifier,
            source_identifier=source_identifier,
        )
    return fetched_source


async def _reopen_source_for_resolved_rca(
    self: Orchestrator, cfg: ServiceConfig, rca_issue: Issue, source_issue: Issue
) -> bool:
    if normalize_state(source_issue.state) != "blocked":
        return False
    if _is_blocked_rca_ticket(source_issue):
        return False
    if _blocked_rca_requires_operator_intervention(
        rca_issue
    ) or _blocked_rca_requires_operator_intervention(source_issue):
        log.info(
            "blocked_rca_source_kept_blocked",
            rca_identifier=rca_issue.identifier,
            source_identifier=source_issue.identifier,
            reason="operator_intervention_required",
        )
        return False
    target_state = _blocked_source_reopen_state(cfg)
    body = (
        f"Fix ticket `{rca_issue.identifier}` reached `{rca_issue.state}`. "
        f"Symphony is moving `{source_issue.identifier}` back to "
        f"`{target_state}` so it can continue through the configured workflow."
    )
    try:
        if not _BLOCKED_RCA_HOST_RESOLVED_HEADING_RE.search(
            source_issue.description or ""
        ):
            await asyncio.to_thread(
                self._tracker_call_append_note,
                cfg,
                source_issue,
                "Blocked Fix Resolved",
                body,
            )
        await asyncio.to_thread(
            self._tracker_call_update_state,
            cfg,
            source_issue,
            target_state,
        )
    except Exception as exc:
        log.warning(
            "blocked_rca_resolution_reopen_failed",
            rca_identifier=rca_issue.identifier,
            source_identifier=source_issue.identifier,
            error=str(exc),
        )
        self._record_tracker_error(source_issue.id, exc)
        return False
    # Keep the episode guard until the freshly reopened source appears in
    # the active candidate fetch, which prunes it. This prevents the same
    # tick's terminal snapshot from opening a second FIX.
    self._blocked_rca_source_ids.add(source_issue.id)
    self._clear_tracker_error(source_issue.id)
    log.info(
        "blocked_rca_source_reopened",
        rca_identifier=rca_issue.identifier,
        source_identifier=source_issue.identifier,
        target_state=target_state,
    )
    return True


async def _move_blocked_fix_out_of_done(
    self: Orchestrator,
    cfg: ServiceConfig,
    fix_issue: Issue,
    source_issue: Issue,
    *,
    reason: str,
    reason_code: str,
) -> bool:
    target_state = next(
        (
            state
            for state in cfg.tracker.terminal_states
            if normalize_state(state) == "human review"
        ),
        next(
            (
                state
                for state in cfg.tracker.terminal_states
                if normalize_state(state) == "blocked"
            ),
            "Blocked",
        ),
    )
    body = (
        f"`{fix_issue.identifier}` cannot remain `Done` because {reason}. "
        f"Moved to `{target_state}`; the source ticket remains Blocked. "
        "Clarify the source request and acceptance criteria, verify the fix, "
        "then append `## Fix Resolution` before completing this ticket."
    )
    # Demotion is the safety boundary: perform it before best-effort notes,
    # and fail the whole tick closed if it cannot be persisted.
    try:
        await asyncio.to_thread(
            self._tracker_call_update_state,
            cfg,
            fix_issue,
            target_state,
        )
    except Exception as exc:
        log.warning(
            "blocked_fix_completion_hold_failed",
            fix_identifier=fix_issue.identifier,
            error=str(exc),
        )
        self._record_tracker_error(fix_issue.id, exc)
        raise

    if normalize_state(source_issue.state) != "blocked":
        try:
            await asyncio.to_thread(
                self._tracker_call_update_state,
                cfg,
                source_issue,
                "Blocked",
            )
        except Exception as exc:
            # The now-unresolved FIX dependency still prevents dispatch.
            log.warning(
                "blocked_fix_source_reblock_failed",
                fix_identifier=fix_issue.identifier,
                source_identifier=source_issue.identifier,
                error=str(exc),
            )
            self._record_tracker_error(source_issue.id, exc)

    try:
        await asyncio.to_thread(
            self._tracker_call_append_note,
            cfg,
            fix_issue,
            "Fix Completion Blocked",
            body,
        )
    except Exception as exc:
        log.warning(
            "blocked_fix_completion_note_failed",
            fix_identifier=fix_issue.identifier,
            error=str(exc),
        )

    self._clear_tracker_error(fix_issue.id)
    log.warning(
        "blocked_fix_completion_held",
        fix_identifier=fix_issue.identifier,
        target_state=target_state,
        reason=reason_code,
    )
    return True


async def _hold_unproven_blocked_fix(
    self: Orchestrator, cfg: ServiceConfig, fix_issue: Issue, source_issue: Issue
) -> bool:
    """Keep a new FIX ticket out of Done until both records prove recovery."""
    if not _is_blocked_fix_ticket(fix_issue):
        return False
    fix_has_resolution = _blocked_rca_current_episode_resolved(fix_issue)
    source_has_resolution = _blocked_rca_current_episode_resolved(source_issue)
    needs_operator = _blocked_rca_requires_operator_intervention(
        fix_issue
    ) or _blocked_rca_requires_operator_intervention(source_issue)
    if fix_has_resolution and source_has_resolution and not needs_operator:
        return False
    if needs_operator:
        reason = "it still requires operator action"
        reason_code = "operator_action"
    elif not fix_has_resolution:
        reason = "the FIX ticket has no current `## Fix Resolution`"
        reason_code = "missing_fix_resolution"
    else:
        reason = "the linked source has no current `## Fix Resolution`"
        reason_code = "missing_source_resolution"
    return await self._move_blocked_fix_out_of_done(
        cfg,
        fix_issue,
        source_issue,
        reason=reason,
        reason_code=reason_code,
    )


async def _auto_reopen_sources_from_resolved_rcas(self: Orchestrator, cfg: ServiceConfig) -> int:
    try:
        terminal_issues = await asyncio.to_thread(
            self._tracker_call_terminal_issues, cfg
        )
    except Exception as exc:
        log.warning("blocked_rca_resolution_fetch_failed", error=str(exc))
        raise
    terminal_by_identifier = {
        issue.identifier.casefold(): issue
        for issue in terminal_issues
        if issue.identifier
    }
    reopened = 0
    for issue in _sort_for_dispatch_fifo(terminal_issues, cfg):
        rca_issue = await self._resolved_blocked_rca_issue(cfg, issue)
        if rca_issue is None:
            continue
        source_issue = await self._source_issue_for_blocked_rca(
            cfg, rca_issue, terminal_by_identifier
        )
        if source_issue is None:
            continue
        if await self._hold_unproven_blocked_fix(cfg, rca_issue, source_issue):
            continue
        source_was_blocked = normalize_state(source_issue.state) == "blocked"
        if await self._reopen_source_for_resolved_rca(cfg, rca_issue, source_issue):
            reopened += 1
        elif source_was_blocked and _is_blocked_fix_ticket(rca_issue):
            await self._move_blocked_fix_out_of_done(
                cfg,
                rca_issue,
                source_issue,
                reason="Symphony could not persist the linked source reopen",
                reason_code="source_reopen_failed",
            )
    return reopened


def _prior_done_run_count(self: Orchestrator, issue: Issue) -> int:
    """How many completed runs already carried this ticket to Done."""
    registry = self._run_registry
    if registry is None:
        return 0
    records = self._registry_guard(
        "reopen_history",
        lambda: registry.recent_runs(issue.id, limit=200, status="normal"),
        [],
    )
    return sum(
        1 for record in records if _is_successful_terminal_state(record.state)
    )


async def _hold_reopened_ticket_over_budget(
    self: Orchestrator, issue: Issue, cfg: ServiceConfig
) -> bool:
    """Block a ticket that keeps coming back from Done; True when held.

        A rewind inside one run is capped by `agent.max_attempts`, but a
        reopen is a *new* run with a fresh counter: the deep preset's Verify
        RED / QA BLOCKED reopen a merged Build slice by moving it from Done
        back to Build, and nothing bounded that loop. Count the ticket's
        prior Done runs in the registry; past `agent.max_reopens` (plus one
        per `## Reopen Approved` section an operator appended) append
        `## Reopen Budget` and park the ticket in Blocked.
        """
    cap = cfg.agent.max_reopens
    if cap <= 0:
        return False
    prior_done = self._prior_done_run_count(issue)
    if prior_done == 0:
        return False
    approvals = len(_REOPEN_APPROVED_RE.findall(issue.description or ""))
    allowed = cap + approvals
    if prior_done <= allowed:
        return False
    target = _rewind_budget_target_state(cfg)
    if not target:
        log.warning(
            "reopen_budget_exceeded",
            identifier=issue.identifier,
            prior_done_runs=prior_done,
            max_reopens=cap,
            approvals=approvals,
            target_state="(none)",
        )
        return False
    note = (
        f"This ticket reached Done {prior_done} time(s) and was reopened "
        f"again (agent.max_reopens: {cap}, approvals: {approvals}). Symphony "
        f"moved it to {target} instead of dispatching another cycle: an "
        "operator must decide whether the latest Verify/QA findings are "
        "real or the verifier is wrong. To allow one more cycle, append a "
        "`## Reopen Approved` section here and move the ticket back to its "
        "lane; raise `agent.max_reopens` (0 disables) for more."
    )
    try:
        await asyncio.to_thread(
            self._tracker_call_append_note, cfg, issue, "Reopen Budget", note
        )
        await asyncio.to_thread(self._tracker_call_update_state, cfg, issue, target)
    except Exception as exc:
        log.warning(
            "reopen_budget_hold_failed",
            identifier=issue.identifier,
            error=str(exc),
        )
        self._record_tracker_error(issue.id, exc)
        return False
    self._clear_tracker_error(issue.id)
    self._record_stats_gate(issue.identifier, issue.state, "reopen_budget")
    log.warning(
        "reopen_budget_exceeded",
        identifier=issue.identifier,
        prior_done_runs=prior_done,
        max_reopens=cap,
        approvals=approvals,
        target_state=target,
    )
    return True


async def _auto_triage_todo_if_actionable(
    self: Orchestrator, issue: Issue, cfg: ServiceConfig
) -> bool:
    if not _is_auto_triage_todo_candidate(issue, cfg):
        return False
    try:
        await asyncio.to_thread(
            self._tracker_call_append_note,
            cfg,
            issue,
            "Triage",
            AUTO_TRIAGE_NOTE,
        )
        await asyncio.to_thread(
            self._tracker_call_update_state,
            cfg,
            issue,
            AUTO_TRIAGE_TARGET_STATE,
        )
    except Exception as exc:
        log.warning(
            "auto_triage_todo_failed",
            identifier=issue.identifier,
            error=str(exc),
        )
        self._record_tracker_error(issue.id, exc)
        return False
    self._clear_tracker_error(issue.id)
    log.info(
        "auto_triage_todo",
        identifier=issue.identifier,
        target=AUTO_TRIAGE_TARGET_STATE,
    )
    return True
