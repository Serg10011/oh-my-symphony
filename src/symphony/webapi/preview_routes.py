"""Routes: product preview."""

from __future__ import annotations

from functools import partial
from typing import Any

from aiohttp import web

from ..orchestrator import Orchestrator
from ..product_preview import ProductPreviewError, ProductPreviewManager
from ..trackers import context_manager as tracker_context_manager
from ..workflow import ServiceConfig
from .context import _Ctx
from .security import (
    _LOOPBACK_BINDS,
    BIND_HOST_KEY,
    _json_error,
    _read_json,
    _wrap,
)

# ---------------------------------------------------------------------------
# routes: stats + static SPA
# ---------------------------------------------------------------------------


def _register_preview_routes(
    app: web.Application, ctx: _Ctx, orchestrator: Orchestrator
) -> None:
    manager = ProductPreviewManager()

    def release_gate(
        cfg: ServiceConfig, *, authoritative: bool = False
    ) -> dict[str, Any]:
        ticket = cfg.preview.release_ticket
        if not ticket:
            return {
                "ticket": "",
                "state": None,
                "ready": True,
                "reason": "No release ticket configured",
            }
        state: str | None = None
        title: str | None = None
        if cfg.tracker.kind == "file":
            issue = ctx.file_tracker().fetch_issue_full_by_id(ticket)
            if issue is not None:
                state, title = issue.state, issue.title
        elif authoritative:
            # Launch/restart must use tracker truth, not a possibly stale live
            # orchestrator snapshot. Status polling keeps the cheap snapshot.
            with tracker_context_manager(cfg) as tracker:
                issue = tracker.fetch_issue_full_by_id(ticket)
            if issue is not None:
                state, title = issue.state, issue.title
        else:
            snapshot = orchestrator.issue_snapshot(ticket)
            if snapshot:
                state = snapshot.get("state")
                title = snapshot.get("title")
        ready = isinstance(state, str) and state.strip().lower() == "done"
        reason = (
            "Release verification passed"
            if ready
            else f"{ticket} must be Done before launch"
        )
        return {
            "ticket": ticket,
            "title": title,
            "state": state,
            "ready": ready,
            "reason": reason,
        }

    async def payload() -> dict[str, Any]:
        cfg = ctx.config()
        status = await manager.status(cfg)
        if not cfg.preview.enabled and status["phase"] == "stopped":
            status["phase"] = "disabled"
        return {
            "configured": bool(cfg.preview.command),
            "enabled": cfg.preview.enabled,
            **status,
            "release_gate": release_gate(cfg),
            "acceptance": list(cfg.preview.acceptance),
        }

    async def get_preview(_request: web.Request) -> web.Response:
        return web.json_response(await payload())

    async def mutate(request: web.Request, action: str) -> web.Response:
        # Unlike ordinary JSON mutations, these bodyless process controls
        # would otherwise be submit-able by a cross-origin HTML form without
        # a CORS preflight. Require JSON even when the body is empty.
        if request.content_type != "application/json":
            return _json_error(
                415,
                "unsupported_media_type",
                "Product Preview actions require application/json",
            )
        bind = str(request.app.get(BIND_HOST_KEY) or "127.0.0.1").lower()
        if bind not in _LOOPBACK_BINDS:
            return _json_error(
                403,
                "preview_loopback_required",
                "Product Preview process control is available only on loopback",
            )
        body = await _read_json(request)
        if body:
            return _json_error(
                400,
                "invalid_body",
                "Product Preview actions do not accept command or path input",
            )
        cfg = ctx.config()
        gate = release_gate(cfg, authoritative=action in {"start", "restart"})
        if action in {"start", "restart"} and not gate["ready"]:
            return _json_error(409, "release_not_ready", str(gate["reason"]))
        try:
            if action == "start":
                await manager.start(cfg)
            elif action == "restart":
                await manager.restart(cfg)
            else:
                await manager.stop(cfg)
        except ProductPreviewError as exc:
            return _json_error(409, "preview_error", str(exc))
        return web.json_response(await payload())

    app.router.add_get("/api/v1/preview", _wrap(get_preview))
    app.router.add_post("/api/v1/preview/start", _wrap(partial(mutate, action="start")))
    app.router.add_post(
        "/api/v1/preview/restart", _wrap(partial(mutate, action="restart"))
    )
    app.router.add_post("/api/v1/preview/stop", _wrap(partial(mutate, action="stop")))

    async def cleanup(_app: web.Application) -> None:
        await manager.close()

    app.on_cleanup.append(cleanup)
