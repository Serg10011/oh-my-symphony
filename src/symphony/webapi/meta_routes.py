"""Routes: stats, SPA index and static assets."""

from __future__ import annotations

import asyncio

from aiohttp import web

from ..orchestrator import Orchestrator
from .context import _Ctx
from .security import (
    STATIC_DIR,
    _json_error,
    _wrap,
)


def _register_meta_routes(
    app: web.Application, ctx: _Ctx, orchestrator: Orchestrator
) -> None:
    async def handle_stats(request: web.Request) -> web.Response:
        try:
            days = int(request.query.get("days", "30"))
        except ValueError:
            return _json_error(400, "invalid_days", "days must be an integer")
        days = max(1, min(days, 365))
        cfg = ctx.config()
        # Completion = arrival in "Done" when the board has one; otherwise
        # any terminal state that is not a parking lane.
        terminal = {s.lower() for s in cfg.tracker.terminal_states}
        skip = {cfg.tracker.archive_state.lower(), "cancelled", "blocked"}
        done_states = {"done"} if "done" in terminal else (terminal - skip or {"done"})
        aggregated = await asyncio.to_thread(
            ctx.stats().aggregate, days, done_states, cfg.tracker.active_states
        )
        snapshot = orchestrator.snapshot()
        aggregated["live"] = {
            "running": snapshot["counts"]["running"],
            "retrying": snapshot["counts"]["retrying"],
            "session_totals": snapshot["codex_totals"],
        }
        return web.json_response(aggregated)

    async def handle_index(_request: web.Request) -> web.StreamResponse:
        index = STATIC_DIR / "index.html"
        if not index.exists():
            return web.Response(
                text="symphony web UI assets missing; reinstall the package",
                status=503,
            )
        return web.FileResponse(index)

    app.router.add_get("/api/v1/stats", _wrap(handle_stats))
    app.router.add_get("/", handle_index)
    if STATIC_DIR.is_dir():
        app.router.add_static("/static/", STATIC_DIR, show_index=False)
