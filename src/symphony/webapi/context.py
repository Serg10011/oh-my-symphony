"""`_Ctx`: per-request access to config, tracker, stats and artifacts."""

from __future__ import annotations

from pathlib import Path

from ..artifacts import ArtifactStore
from ..orchestrator import Orchestrator
from ..stats import StatsStore, stats_store_for
from ..trackers.file import FileBoardTracker
from ..workflow import ServiceConfig
from ..workflow.mutate import WorkflowMutationError


class _Ctx:
    """Per-request access to config, tracker and stats for one board."""

    def __init__(self, orchestrator: Orchestrator) -> None:
        self.orchestrator = orchestrator

    def config(self) -> ServiceConfig:
        cfg = self.orchestrator.workflow_state.current()
        if cfg is None:
            cfg, err = self.orchestrator.workflow_state.reload()
            if cfg is None:
                raise WorkflowMutationError(f"workflow not loaded: {err}")
        return cfg

    def workflow_dir(self) -> Path:
        return self.config().workflow_path.parent

    def file_tracker(self) -> FileBoardTracker:
        cfg = self.config()
        if cfg.tracker.kind != "file":
            raise WorkflowMutationError(
                "board editing requires tracker.kind: file in WORKFLOW.md"
            )
        return FileBoardTracker(cfg.tracker)

    def stats(self) -> StatsStore:
        return stats_store_for(self.workflow_dir() / ".symphony" / "stats.jsonl")

    def artifacts(self) -> ArtifactStore | None:
        """Read-side view of the ticket artifact store, or None when off.

        Collection is the orchestrator's job, so the size caps do not apply
        here. Not strictly read-only: a corrupt index is rebuilt (and
        rewritten) on read. That write is `os.replace`, so a reader never
        sees a torn index even though this instance's lock is not the
        orchestrator's.
        """
        cfg = self.config()
        if not cfg.artifacts.enabled:
            return None
        return ArtifactStore(self.workflow_dir() / ".symphony" / "artifacts")
