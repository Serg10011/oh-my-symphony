"""The seam between "which ticket runs" and "how that ticket runs".

`Orchestrator` keeps every responsibility it already had: polling trackers,
deciding eligibility, acquiring the dispatch lease, creating and cleaning
workspaces, enforcing global concurrency, publishing snapshots, and
applying terminal policy. What it delegates through this Protocol is only
the shape of the work *inside* one ticket run.

`LegacyStageExecutor` preserves the original implementation — stage prompts,
agent-authored board transitions, the multi-turn loop in
`core._run_agent_attempt`. It delegates rather than duplicating, so the
seam moves no legacy code and cannot regress it.

The opt-in `ApiTeamExecutor` in `symphony.api_team` instead runs one explicitly
confirmed session through a trusted host command. It never enters the legacy
stage retry or automatic completion path.

Selection happens once per dispatch, before the worker task is created, so
a run's execution mode is fixed for its whole lifetime.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..issue import Issue
from ..workflow import ServiceConfig


@dataclass(frozen=True)
class TicketRunContext:
    """Everything an executor is given for one ticket attempt.

    Frozen because an executor must not be able to change which ticket,
    workspace, or lease it was handed; those belong to the orchestrator.
    """

    issue: Issue
    attempt: int | None
    cfg: ServiceConfig
    run_id: str
    workspace_path: Path
    agent_kind: str
    attempt_kind: str


class TicketExecutor(Protocol):
    """Runs one ticket attempt to completion.

    Implementations own no lifecycle beyond the run: the orchestrator has
    already acquired the lease before `execute` is awaited, and will call
    its own worker-exit handling after `execute` returns or raises.
    """

    async def execute(self, context: TicketRunContext) -> None: ...


class LegacyStageExecutor:
    """The stage-prompt loop, unchanged.

    Holds a reference to the orchestrator rather than a copy of its logic.
    The indirection is worth one attribute lookup: it makes the execution
    mode an explicit, testable choice without touching the code path that
    every existing ticket already runs through.
    """

    mode = "legacy_stage_loop"

    def __init__(self, orchestrator: "_LegacyHost") -> None:
        self._orchestrator = orchestrator

    async def execute(self, context: TicketRunContext) -> None:
        await self._orchestrator.run_legacy_stage_loop(
            context.issue, context.attempt, context.cfg
        )


class _LegacyHost(Protocol):
    """The one orchestrator method `LegacyStageExecutor` needs."""

    async def run_legacy_stage_loop(
        self, issue: Issue, attempt: int | None, cfg: ServiceConfig
    ) -> None: ...
