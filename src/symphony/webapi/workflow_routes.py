"""Routes: workflow (columns, prompts, branch policy, continuous improvement)."""

from __future__ import annotations

import asyncio

from aiohttp import web

from ..logging import get_logger
from ..orchestrator import Orchestrator
from ..workflow.mutate import (
    WorkflowMutationError,
    apply_lane_preset,
    apply_states_update,
    read_prompt,
    set_branch_policy,
    set_continuous_improvement_settings,
    write_prompt,
)
from ..workflow.preflight import stage_turn_budget_error
from ..workflow.presets import LANE_PRESETS, get_lane_preset, guess_lane_preset
from .context import _Ctx
from .payloads import (
    _continuous_improvement_payload,
    _workflow_payload,
)
from .security import (
    _BRANCH_RE,
    _json_error,
    _read_json,
    _wrap,
)
from .validation import (
    _parse_ci_settings,
    _parse_state_specs,
)

log = get_logger()


# ---------------------------------------------------------------------------
# routes: workflow (columns + prompts + branch policy)
# ---------------------------------------------------------------------------


def _register_workflow_routes(
    app: web.Application, ctx: _Ctx, orchestrator: Orchestrator
) -> None:
    async def handle_workflow_get(_request: web.Request) -> web.Response:
        cfg = ctx.config()
        return web.json_response(_workflow_payload(cfg))

    async def handle_states_put(request: web.Request) -> web.Response:
        body = await _read_json(request)
        specs = _parse_state_specs(body)
        cfg = ctx.config()
        tracker = ctx.file_tracker()
        # A running worker owns its ticket's state string — refuse edits
        # that would rename or remove that state under it. (Best-effort:
        # a worker dispatched during the write below is handled by the
        # orchestrator's normal mid-run state reconciliation.)
        running_states = {i.state.lower() for i in orchestrator.iter_running_issues()}
        new_names = {s.name.lower() for s in specs}
        rename_sources = {
            (s.previous_name or "").lower() for s in specs if s.previous_name
        }
        for state in running_states:
            if state not in new_names or state in rename_sources:
                return _json_error(
                    409,
                    "state_in_use",
                    f"column {state!r} has a running worker; wait or pause first",
                )

        plan = await asyncio.to_thread(apply_states_update, cfg.workflow_path, specs)
        # Migrate tickets before the next poll sees the new config. Skip any
        # ticket whose worker started while the write was in flight.
        migrated: dict[str, str] = {}
        skipped: list[str] = []
        moves = [(old, new) for old, new in plan.renamed.items()]
        moves.extend((old, plan.fallback_state) for old in plan.removed)
        for old, target in moves:
            for issue in await asyncio.to_thread(tracker.fetch_issues_by_states, [old]):
                if orchestrator.find_running_issue_id(issue.identifier) is not None:
                    skipped.append(issue.identifier)
                    continue
                await asyncio.to_thread(tracker.transition, issue.identifier, target)
                migrated[issue.identifier] = target
        orchestrator.workflow_state.reload()
        orchestrator.request_refresh()
        return web.json_response(
            {
                "renamed": plan.renamed,
                "removed": plan.removed,
                "added": plan.added,
                "migrated": migrated,
                "skipped_running": skipped,
            }
        )

    async def handle_prompt_get(request: web.Request) -> web.Response:
        state = request.match_info["state"]
        cfg = ctx.config()
        payload = await asyncio.to_thread(read_prompt, cfg.workflow_path, state)
        if payload is None:
            return _json_error(
                404, "prompt_not_configured", f"no prompt file for state {state!r}"
            )
        return web.json_response(payload)

    async def handle_prompt_put(request: web.Request) -> web.Response:
        state = request.match_info["state"]
        body = await _read_json(request)
        content = body.get("content")
        if not isinstance(content, str):
            raise WorkflowMutationError("body must contain string `content`")
        cfg = ctx.config()
        path = await asyncio.to_thread(write_prompt, cfg.workflow_path, state, content)
        orchestrator.workflow_state.reload()
        return web.json_response(
            {"state": state, "path": str(path), "bytes": len(content.encode("utf-8"))}
        )

    async def handle_branch_policy_put(request: web.Request) -> web.Response:
        body = await _read_json(request)
        updates: dict[str, str] = {}
        for key in ("feature_base_branch", "auto_merge_target_branch"):
            if key in body:
                value = body.get(key)
                if not isinstance(value, str):
                    raise WorkflowMutationError(f"{key} must be a string")
                value = value.strip()
                if value and not _BRANCH_RE.match(value):
                    raise WorkflowMutationError(f"invalid branch name {value!r}")
                updates[key] = value
        if not updates:
            raise WorkflowMutationError(
                "body must set feature_base_branch and/or auto_merge_target_branch"
            )
        cfg = ctx.config()
        await asyncio.to_thread(
            set_branch_policy,
            cfg.workflow_path,
            feature_base_branch=updates.get("feature_base_branch"),
            auto_merge_target_branch=updates.get("auto_merge_target_branch"),
        )
        orchestrator.workflow_state.reload()
        return web.json_response({"updated": sorted(updates)})

    async def handle_lane_presets_get(_request: web.Request) -> web.Response:
        cfg = ctx.config()
        return web.json_response(
            {
                "presets": [
                    {
                        "name": preset.name,
                        "label": preset.label,
                        "active_states": list(preset.active_states),
                        "terminal_states": list(preset.terminal_states),
                    }
                    for preset in LANE_PRESETS.values()
                ],
                "current": guess_lane_preset(cfg.tracker.active_states),
            }
        )

    async def handle_lane_preset_apply(request: web.Request) -> web.Response:
        body = await _read_json(request)
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            raise WorkflowMutationError("body must contain string `name`")
        try:
            preset = get_lane_preset(name)
        except ValueError as exc:
            raise WorkflowMutationError(str(exc)) from exc
        cfg = ctx.config()
        tracker = ctx.file_tracker()
        # Same guard as handle_states_put: a running worker owns its
        # ticket's state string — refuse to pull a lane out from under it.
        preset_active = {s.lower() for s in preset.active_states}
        for issue in orchestrator.iter_running_issues():
            if issue.state.lower() not in preset_active:
                return _json_error(
                    409,
                    "state_in_use",
                    f"column {issue.state!r} has a running worker; wait or pause first",
                )

        plan = await asyncio.to_thread(apply_lane_preset, cfg.workflow_path, name)
        migrated: dict[str, str] = {}
        skipped: list[str] = []
        for old in plan.removed:
            for issue in await asyncio.to_thread(tracker.fetch_issues_by_states, [old]):
                if orchestrator.find_running_issue_id(issue.identifier) is not None:
                    skipped.append(issue.identifier)
                    continue
                await asyncio.to_thread(
                    tracker.transition, issue.identifier, plan.fallback_state
                )
                migrated[issue.identifier] = plan.fallback_state
        orchestrator.workflow_state.reload()
        orchestrator.request_refresh()
        # F-23: an 8-lane preset needs `agent.max_turns >= len(active_states)`
        # or the very next dispatch fails preflight. Report it in the response
        # instead of letting the operator discover it at run time.
        reloaded = orchestrator.workflow_state.current()
        warning = stage_turn_budget_error(reloaded) if reloaded is not None else None
        if warning:
            log.warning(
                "lane_preset_turn_budget_warning", preset=preset.name, detail=warning
            )
        return web.json_response(
            {
                "applied": preset.name,
                "added": plan.added,
                "removed": plan.removed,
                "migrated": migrated,
                "skipped_running": skipped,
                "fallback_state": plan.fallback_state,
                "warning": warning,
            }
        )

    async def handle_continuous_improvement_put(
        request: web.Request,
    ) -> web.Response:
        body = await _read_json(request)
        cfg = ctx.config()
        updates = _parse_ci_settings(body, cfg)
        await asyncio.to_thread(
            set_continuous_improvement_settings,
            cfg.workflow_path,
            enabled=updates.get("enabled"),
            interval_ms=updates.get("interval_ms"),
            max_turns=updates.get("max_turns"),
            agent_kind=updates.get("agent_kind"),
            modes=updates.get("modes"),
        )
        new_cfg, err = orchestrator.workflow_state.reload()
        if new_cfg is None:
            raise WorkflowMutationError(f"workflow not loaded: {err}")
        orchestrator.request_refresh()
        return web.json_response(
            {
                "updated": sorted(updates),
                "continuous_improvement": _continuous_improvement_payload(new_cfg),
            }
        )

    async def handle_continuous_improvement_reset(
        request: web.Request,
    ) -> web.Response:
        if request.body_exists:
            await _read_json(request)
        orchestrator.reset_continuous_improvement_turns()
        return web.json_response(
            {"status": orchestrator.continuous_improvement_status()}
        )

    async def handle_continuous_improvement_status(
        _request: web.Request,
    ) -> web.Response:
        return web.json_response(orchestrator.continuous_improvement_status())

    app.router.add_get("/api/v1/workflow", _wrap(handle_workflow_get))
    app.router.add_put("/api/v1/workflow/states", _wrap(handle_states_put))
    app.router.add_get("/api/v1/workflow/prompts/{state}", _wrap(handle_prompt_get))
    app.router.add_put("/api/v1/workflow/prompts/{state}", _wrap(handle_prompt_put))
    app.router.add_put(
        "/api/v1/workflow/branch-policy", _wrap(handle_branch_policy_put)
    )
    app.router.add_get("/api/v1/workflow/presets", _wrap(handle_lane_presets_get))
    app.router.add_post(
        "/api/v1/workflow/presets/apply", _wrap(handle_lane_preset_apply)
    )
    app.router.add_put(
        "/api/v1/workflow/continuous-improvement",
        _wrap(handle_continuous_improvement_put),
    )
    app.router.add_post(
        "/api/v1/workflow/continuous-improvement/reset-turns",
        _wrap(handle_continuous_improvement_reset),
    )
    app.router.add_get(
        "/api/v1/continuous-improvement/status",
        _wrap(handle_continuous_improvement_status),
    )
