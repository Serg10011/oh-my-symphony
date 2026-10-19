"""Routes: git (branches, diffs, merges)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from aiohttp import web

from ..issue import Issue
from ..logging import get_logger
from ..orchestrator import Orchestrator
from ..utils import git_inspect, git_ops
from ..utils.auto_merge import auto_merge_on_done_best_effort
from ..utils.git_ops import GitOpResult
from ..workflow import (
    SYMPHONY_BRANCH_PREFIX,
    ServiceConfig,
)
from ..workflow.mutate import WorkflowMutationError
from .context import _Ctx
from .security import (
    _COMMIT_RE,
    _json_error,
    _read_json,
    _wrap,
)
from .validation import (
    _check_branch,
    _check_description,
    _check_identifier,
    _check_title,
)

log = get_logger()


# ---------------------------------------------------------------------------
# routes: git (host-repo history, task branches, manual merge)
# ---------------------------------------------------------------------------


def _register_git_routes(
    app: web.Application, ctx: _Ctx, orchestrator: Orchestrator
) -> None:
    def _effective_target(cfg: ServiceConfig, workflow_dir: Path) -> str | None:
        return cfg.agent.auto_merge_target_branch or git_inspect.current_branch(
            workflow_dir
        )

    async def handle_git_branches(_request: web.Request) -> web.Response:
        branches = await asyncio.to_thread(
            git_inspect.list_branches, ctx.workflow_dir()
        )
        return web.json_response({"branches": branches})

    async def handle_git_log(request: web.Request) -> web.Response:
        raw_branch = (request.query.get("branch") or "").strip()
        branch = _check_branch(raw_branch) if raw_branch else ""
        raw_limit = (request.query.get("limit") or "").strip()
        limit = git_inspect.DEFAULT_LOG_LIMIT
        if raw_limit:
            try:
                limit = int(raw_limit)
            except ValueError:
                return _json_error(400, "invalid_limit", "limit must be an integer")
        workflow_dir = ctx.workflow_dir()

        def _load() -> tuple[str | None, list[dict[str, object]] | None]:
            if not git_inspect.is_git_repo(workflow_dir):
                return "not_a_git_repo", []
            if branch and not git_inspect.ref_exists(workflow_dir, branch):
                return None, None
            return None, git_inspect.commit_log(
                workflow_dir, ref=branch or None, limit=limit
            )

        note, commits = await asyncio.to_thread(_load)
        if commits is None:
            return _json_error(400, "unknown_ref", f"unknown ref {branch!r}")
        return web.json_response(
            {"branch": branch or None, "commits": commits, "note": note}
        )

    async def handle_git_task_branches(_request: web.Request) -> web.Response:
        cfg = ctx.config()
        workflow_dir = cfg.workflow_path.parent

        def _load() -> tuple[str | None, list[dict[str, object]], str | None]:
            if not git_inspect.is_git_repo(workflow_dir):
                return None, [], "not_a_git_repo"
            target = _effective_target(cfg, workflow_dir)
            return target, git_inspect.list_task_branches(workflow_dir, target), None

        target, rows, note = await asyncio.to_thread(_load)
        tickets: dict[str, Issue] = {}
        if rows and cfg.tracker.kind == "file":
            try:
                issues = await asyncio.to_thread(
                    ctx.file_tracker().fetch_issues_by_states,
                    [*cfg.tracker.active_states, *cfg.tracker.terminal_states],
                )
                tickets = {i.identifier: i for i in issues}
            except Exception as exc:
                # Ticket enrichment is best-effort; the git view stays useful.
                log.warning("git_task_branches_ticket_lookup_failed", error=str(exc))
        for row in rows:
            identifier = str(row["identifier"])
            issue = tickets.get(identifier)
            row["ticket"] = (
                {
                    "identifier": issue.identifier,
                    "title": issue.title,
                    "state": issue.state,
                }
                if issue is not None
                else None
            )
            row["running"] = orchestrator.find_running_issue_id(identifier) is not None
        return web.json_response(
            {
                "target_branch": target,
                "auto_merge_enabled": cfg.agent.auto_merge_on_done,
                "auto_merge_push_target": cfg.agent.auto_merge_push_target,
                "merge_delivery": (
                    "upstream-publishing"
                    if cfg.agent.auto_merge_push_target
                    else "local-only"
                ),
                "branches": rows,
                "note": note,
            }
        )

    async def handle_git_compare(request: web.Request) -> web.Response:
        branch = _check_branch(request.query.get("branch"))
        raw_target = (request.query.get("target") or "").strip()
        target = _check_branch(raw_target, key="target") if raw_target else None
        cfg = ctx.config()
        workflow_dir = cfg.workflow_path.parent

        def _load() -> tuple[str, str, None] | tuple[None, None, dict[str, object]]:
            if not git_inspect.is_git_repo(workflow_dir):
                return "not_a_git_repo", "workflow dir is not a git repository", None
            resolved = target or _effective_target(cfg, workflow_dir)
            if not resolved:
                return (
                    "no_target",
                    "no target branch; set auto_merge_target_branch or pass target",
                    None,
                )
            for ref in (branch, resolved):
                if not git_inspect.ref_exists(workflow_dir, ref):
                    return "unknown_ref", f"unknown ref {ref!r}", None
            return None, None, git_inspect.compare_refs(workflow_dir, branch, resolved)

        code, message, payload = await asyncio.to_thread(_load)
        if payload is None:
            assert code is not None and message is not None
            return _json_error(400, code, message)
        return web.json_response(payload)

    async def handle_git_diff(request: web.Request) -> web.Response:
        commit = (request.query.get("commit") or "").strip()
        cfg = ctx.config()
        workflow_dir = cfg.workflow_path.parent

        if commit:
            if not _COMMIT_RE.match(commit):
                raise WorkflowMutationError(f"invalid commit {commit!r}")

            def _load_commit() -> dict[str, object] | None:
                if not git_inspect.is_git_repo(workflow_dir):
                    return None
                if not git_inspect.ref_exists(workflow_dir, commit):
                    return None
                return git_inspect.commit_patch(workflow_dir, commit)

            payload = await asyncio.to_thread(_load_commit)
            if payload is None:
                return _json_error(400, "unknown_ref", f"unknown commit {commit!r}")
            return web.json_response({"commit": commit, **payload})

        branch = _check_branch(request.query.get("branch"))
        raw_target = (request.query.get("target") or "").strip()
        target = _check_branch(raw_target, key="target") if raw_target else None
        path = (request.query.get("path") or "").strip() or None
        if path is not None and (path.startswith("-") or "\x00" in path):
            raise WorkflowMutationError(f"invalid path {path!r}")

        def _load() -> tuple[str, str, None] | tuple[None, str, dict[str, object]]:
            if not git_inspect.is_git_repo(workflow_dir):
                return "not_a_git_repo", "workflow dir is not a git repository", None
            resolved = target or _effective_target(cfg, workflow_dir)
            if not resolved:
                return (
                    "no_target",
                    "no target branch; set auto_merge_target_branch or pass target",
                    None,
                )
            for ref in (branch, resolved):
                if not git_inspect.ref_exists(workflow_dir, ref):
                    return "unknown_ref", f"unknown ref {ref!r}", None
            return (
                None,
                resolved,
                git_inspect.diff_patch(workflow_dir, branch, resolved, path),
            )

        code, detail, payload = await asyncio.to_thread(_load)
        if payload is None:
            assert code is not None
            return _json_error(400, code, detail)
        return web.json_response({"branch": branch, "target": detail, **payload})

    merge_lock = asyncio.Lock()

    async def handle_git_merge(request: web.Request) -> web.Response:
        body = await _read_json(request)
        branch = _check_branch(body.get("branch"))
        if not branch.startswith(SYMPHONY_BRANCH_PREFIX):
            raise WorkflowMutationError(
                f"merge is limited to {SYMPHONY_BRANCH_PREFIX}* task branches"
            )
        identifier = _check_identifier(branch[len(SYMPHONY_BRANCH_PREFIX) :])
        raw_target = body.get("target")
        target = (
            _check_branch(raw_target, key="target")
            if raw_target not in (None, "")
            else None
        )
        cfg = ctx.config()
        workflow_dir = cfg.workflow_path.parent
        if orchestrator.find_running_issue_id(identifier) is not None:
            return _json_error(
                409,
                "state_in_use",
                f"{identifier} has a running worker; pause or wait before merging",
            )
        if merge_lock.locked():
            return _json_error(
                409, "merge_in_progress", "another merge is already running"
            )
        async with merge_lock:
            resolved_target = target or await asyncio.to_thread(
                _effective_target, cfg, workflow_dir
            )
            if not resolved_target:
                return _json_error(
                    400,
                    "no_target",
                    "no target branch; set auto_merge_target_branch or pass target",
                )
            issue: Issue | None = None
            if cfg.tracker.kind == "file":
                try:
                    issue = await asyncio.to_thread(
                        ctx.file_tracker().fetch_issue_full_by_id, identifier
                    )
                except Exception as exc:
                    log.warning(
                        "git_merge_ticket_lookup_failed",
                        identifier=identifier,
                        error=str(exc),
                    )
            # Same engine and policy as the automatic Verify merge gate:
            # exclude_paths, --no-ff, conflict preflight, dirty-host guard.
            result = await auto_merge_on_done_best_effort(
                workflow_dir=workflow_dir,
                branch=branch,
                identifier=identifier,
                title=issue.title if issue is not None else identifier,
                target_branch=resolved_target,
                exclude_paths=cfg.agent.auto_merge_exclude_paths,
                capture_untracked=cfg.agent.auto_merge_capture_untracked,
                push_target=cfg.agent.auto_merge_push_target,
            )
        if not result.ok:
            return _json_error(
                409, f"merge_{result.status}", result.detail or result.status
            )
        note_appended = False
        if issue is not None:
            note_body = (
                f"Operator merged `{branch}` into `{resolved_target}` from "
                "the web UI's Git page.\n\n"
                f"- status: `{result.status}`"
            )
            if result.detail.strip():
                note_body = f"{note_body}\n- detail: {result.detail.strip()[:1000]}"
            try:
                await asyncio.to_thread(
                    ctx.file_tracker().append_note, issue, "Manual Merge", note_body
                )
                note_appended = True
            except Exception as exc:
                log.warning(
                    "git_merge_note_append_failed",
                    identifier=identifier,
                    error=str(exc),
                )
        orchestrator.request_refresh()
        return web.json_response(
            {
                "ok": True,
                "status": result.status,
                "detail": result.detail,
                "branch": branch,
                "target": resolved_target,
                "ticket_note_appended": note_appended,
            }
        )

    # ---- mutating branch actions ------------------------------------
    # Scope: task branches always; the merge target only for `push`, and
    # only when the operator retypes its name. Nothing here ever forces a
    # push, and a delete only uses `-D` after an explicit force or a
    # verified merge into the target.

    def _task_branch_identifier(branch: str) -> str:
        if not branch.startswith(SYMPHONY_BRANCH_PREFIX):
            raise WorkflowMutationError(
                f"limited to {SYMPHONY_BRANCH_PREFIX}* task branches"
            )
        return _check_identifier(branch[len(SYMPHONY_BRANCH_PREFIX) :])

    def _resolve_remote(workflow_dir: Path, raw: Any) -> str | None:
        if raw in (None, ""):
            return git_ops.default_remote(workflow_dir)
        if not isinstance(raw, str) or not git_ops.is_valid_remote_name(raw.strip()):
            raise WorkflowMutationError(f"invalid remote name {raw!r}")
        remote = raw.strip()
        return remote if remote in git_ops.list_remotes(workflow_dir) else None

    async def handle_git_remote_status(_request: web.Request) -> web.Response:
        cfg = ctx.config()
        workflow_dir = cfg.workflow_path.parent

        def _load() -> dict[str, object]:
            if not git_inspect.is_git_repo(workflow_dir):
                return {"remotes": [], "note": "not_a_git_repo"}
            return {
                "remotes": git_ops.list_remotes(workflow_dir),
                "default_remote": git_ops.default_remote(workflow_dir),
                "current_branch": git_inspect.current_branch(workflow_dir),
                "target_branch": _effective_target(cfg, workflow_dir),
                "note": None,
            }

        payload = await asyncio.to_thread(_load)
        # `gh` presence is a property of the host, not of the repo.
        payload["gh_available"] = git_ops.gh_available()
        return web.json_response(payload)

    async def handle_git_branch_delete(request: web.Request) -> web.Response:
        body = await _read_json(request)
        branch = _check_branch(body.get("branch"))
        identifier = _task_branch_identifier(branch)
        force = bool(body.get("force"))
        cfg = ctx.config()
        workflow_dir = cfg.workflow_path.parent
        if orchestrator.find_running_issue_id(identifier) is not None:
            return _json_error(
                409,
                "state_in_use",
                f"{identifier} has a running worker; pause or wait before deleting",
            )

        def _delete() -> tuple[str, str] | GitOpResult:
            if not git_inspect.is_git_repo(workflow_dir):
                return "not_a_git_repo", "workflow dir is not a git repository"
            if not git_inspect.ref_exists(workflow_dir, branch):
                return "unknown_ref", f"unknown branch {branch!r}"
            if git_inspect.current_branch(workflow_dir) == branch:
                return "checked_out", f"{branch} is checked out; switch away first"
            target = _effective_target(cfg, workflow_dir)
            merged = bool(target) and git_inspect.is_merged(
                workflow_dir, branch, target
            )
            if not merged and not force:
                return (
                    "not_merged",
                    f"{branch} is not merged into {target or 'the target branch'}; "
                    "merge it first or repeat with force",
                )
            # `-D` only once the merge is proven or the operator forced it;
            # plain `-d` can still refuse a branch merged into the target
            # but not into HEAD.
            return git_ops.delete_branch(workflow_dir, branch, force=True)

        outcome = await asyncio.to_thread(_delete)
        if isinstance(outcome, tuple):
            code, message = outcome
            return _json_error(409 if code != "unknown_ref" else 400, code, message)
        if not outcome.ok:
            return _json_error(409, outcome.status, outcome.detail)
        orchestrator.request_refresh()
        return web.json_response({**outcome.as_dict(), "branch": branch})

    async def handle_git_push(request: web.Request) -> web.Response:
        body = await _read_json(request)
        branch = _check_branch(body.get("branch"))
        cfg = ctx.config()
        workflow_dir = cfg.workflow_path.parent
        target = await asyncio.to_thread(_effective_target, cfg, workflow_dir)
        is_task_branch = branch.startswith(SYMPHONY_BRANCH_PREFIX)
        if not is_task_branch:
            if not target or branch != target:
                return _json_error(
                    400,
                    "branch_not_pushable",
                    f"push is limited to {SYMPHONY_BRANCH_PREFIX}* task branches "
                    "and the merge target branch",
                )
            # Pushing the shared target moves work other people pull, so it
            # takes a deliberate second act, not one click.
            if body.get("confirm") != branch:
                return _json_error(
                    400,
                    "confirm_required",
                    f"pushing {branch} requires confirm: {branch!r}",
                )
        remote = await asyncio.to_thread(
            _resolve_remote, workflow_dir, body.get("remote")
        )
        if not remote:
            return _json_error(
                400, "no_remote", "this repository has no matching git remote"
            )
        if not await asyncio.to_thread(git_inspect.ref_exists, workflow_dir, branch):
            return _json_error(400, "unknown_ref", f"unknown branch {branch!r}")
        result = await asyncio.to_thread(
            git_ops.push_branch, workflow_dir, branch, remote
        )
        if not result.ok:
            return _json_error(409, result.status, result.detail)
        return web.json_response(
            {**result.as_dict(), "branch": branch, "remote": remote}
        )

    async def handle_git_pull_request(request: web.Request) -> web.Response:
        body = await _read_json(request)
        branch = _check_branch(body.get("branch"))
        identifier = _task_branch_identifier(branch)
        raw_target = body.get("target")
        requested_target = (
            _check_branch(raw_target, key="target")
            if raw_target not in (None, "")
            else None
        )
        cfg = ctx.config()
        workflow_dir = cfg.workflow_path.parent
        target = requested_target or await asyncio.to_thread(
            _effective_target, cfg, workflow_dir
        )
        if not target:
            return _json_error(
                400,
                "no_target",
                "no target branch; set auto_merge_target_branch or pass target",
            )
        if not git_ops.gh_available():
            return _json_error(
                400,
                "gh_unavailable",
                "the GitHub CLI (gh) is not installed or not on PATH",
            )
        remote = await asyncio.to_thread(
            _resolve_remote, workflow_dir, body.get("remote")
        )
        if not remote:
            return _json_error(
                400, "no_remote", "this repository has no matching git remote"
            )
        if not await asyncio.to_thread(
            git_ops.branch_on_remote, workflow_dir, remote, branch
        ):
            return _json_error(
                409,
                "branch_not_pushed",
                f"{branch} is not on {remote} yet; push it before opening a PR",
            )
        issue: Issue | None = None
        if cfg.tracker.kind == "file":
            try:
                issue = await asyncio.to_thread(
                    ctx.file_tracker().fetch_issue_full_by_id, identifier
                )
            except Exception as exc:
                log.warning(
                    "git_pr_ticket_lookup_failed", identifier=identifier, error=str(exc)
                )
        raw_title = body.get("title")
        title = (
            _check_title(raw_title)
            if raw_title not in (None, "")
            else f"{identifier}: {issue.title}"
            if issue is not None
            else identifier
        )
        raw_body = body.get("body")
        pr_body = (
            _check_description(raw_body)
            if raw_body not in (None, "")
            else f"Symphony task branch for `{identifier}`."
        )
        result = await asyncio.to_thread(
            git_ops.create_pull_request, workflow_dir, branch, target, title, pr_body
        )
        if not result.ok:
            return _json_error(409, result.status, result.detail)
        return web.json_response(
            {**result.as_dict(), "branch": branch, "target": target}
        )

    app.router.add_get("/api/v1/git/branches", _wrap(handle_git_branches))
    app.router.add_get("/api/v1/git/remote-status", _wrap(handle_git_remote_status))
    app.router.add_post("/api/v1/git/branch/delete", _wrap(handle_git_branch_delete))
    app.router.add_post("/api/v1/git/push", _wrap(handle_git_push))
    app.router.add_post("/api/v1/git/pr", _wrap(handle_git_pull_request))
    app.router.add_get("/api/v1/git/log", _wrap(handle_git_log))
    app.router.add_get("/api/v1/git/task-branches", _wrap(handle_git_task_branches))
    app.router.add_get("/api/v1/git/compare", _wrap(handle_git_compare))
    app.router.add_get("/api/v1/git/diff", _wrap(handle_git_diff))
    app.router.add_post("/api/v1/git/merge", _wrap(handle_git_merge))
