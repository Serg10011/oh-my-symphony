"""Explicit API-team confirmation in the existing board, local and same-origin."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from aiohttp import web

from .. import api_team
from .security import _json_error, _read_json, _request_is_loopback, _wrap
from .validation import _check_identifier


def _same_origin(request: web.Request) -> bool:
    origin = request.headers.get("Origin")
    if not origin:
        # Non-browser clients still need JSON + a single-use capability for
        # confirmation. A browser provenance header without Origin is refused.
        return not any(k in request.headers for k in ("Sec-Fetch-Site", "Referer"))
    try:
        parsed = urlsplit(origin)
        return (parsed.scheme == request.scheme and parsed.netloc == request.host
                and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


def register_api_team_routes(app, ctx, orchestrator):
    async def team_settings(request):
        if not _request_is_loopback(request):
            return _json_error(403, "api_team_local_only", "Настройки команды доступны только с этого компьютера")
        kind = request.match_info["kind"]
        cfg = ctx.config()
        profile = api_team.api_profile(cfg, kind)
        if not profile or not api_team.supports_team_settings(profile.command) or cfg.tracker.kind != "file":
            return _json_error(404, "api_team_not_selected", "Такая API-команда не настроена")
        if request.method == "POST":
            if request.content_type != "application/json" or not _same_origin(request):
                return _json_error(403, "api_team_origin", "Изменяй назначение на странице своей доски")
            operation = request.match_info.get("operation")
            action = "settings-" + operation if operation else "set-team-default"
            payload = await _read_json(request)
            fields = {"mode", "paths"} if operation else {"team"}
            if operation == "save":
                fields.add("expected")
            if set(payload) != fields:
                return _json_error(400, "invalid_board_payload", "Проверь параметры настройки доски")
        else:
            ids = request.query.getall("id", [])
            if len(ids) > 100:
                return _json_error(400, "invalid_board_payload", "Слишком много карточек")
            action, payload = "team-options", {"identifiers": [_check_identifier(i) for i in ids]}
        result = await api_team.invoke(cfg, kind, "", action, payload=payload)
        return web.json_response(result, headers={"Cache-Control": "no-store, private"})
    app.router.add_get("/api/v1/api-teams/{kind}", _wrap(team_settings))
    app.router.add_post("/api/v1/api-teams/{kind}", _wrap(team_settings))
    app.router.add_post("/api/v1/api-teams/{kind}/settings/{operation:preview|save}", _wrap(team_settings))

    async def permission(request):
        if not _request_is_loopback(request):
            return _json_error(403, "api_team_local_only", "Разрешение доступно только с этого компьютера")
        kind = request.match_info["kind"]
        cfg = ctx.config()
        profile = api_team.api_profile(cfg, kind)
        if not profile or not api_team.supports_team_settings(profile.command) or cfg.tracker.kind != "file":
            return _json_error(404, "api_team_not_selected", "Такая API-команда не настроена")
        action = request.match_info.get("action", "show")
        if request.method == "POST":
            if request.content_type != "application/json" or not _same_origin(request):
                return _json_error(403, "api_team_origin", "Управляй разрешением на странице своей доски")
            payload = await _read_json(request)
        else:
            payload = {}
        if (set(payload) != ({"confirmation_token"} if action == "authorize" else set())):
            return _json_error(400, "invalid_board_payload", "Проверь параметры подтверждения")
        result = await api_team.invoke(cfg, kind, "", "permission-" + action, payload=payload)
        if action in {"authorize", "revoke"}:
            orchestrator.request_refresh()
        return web.json_response(result, headers={"Cache-Control": "no-store, private"})
    app.router.add_get("/api/v1/api-teams/{kind}/permission", _wrap(permission))
    app.router.add_post("/api/v1/api-teams/{kind}/permission/{action:prepare|authorize|revoke}", _wrap(permission))

    async def handle(request, action="show"):
        if not _request_is_loopback(request):
            return _json_error(403, "api_team_local_only", "API-команда пока доступна только с этого компьютера")
        if request.method == "POST":
            if request.content_type != "application/json" or not _same_origin(request):
                return _json_error(403, "api_team_origin", "Подтверждай запуск на странице этой доски")
            payload = await _read_json(request)
        else:
            payload = {}
        identifier = _check_identifier(request.match_info["identifier"])
        cfg = ctx.config()
        issue = await asyncio.to_thread(ctx.file_tracker().fetch_issue_full_by_id, identifier)
        if issue is None:
            return _json_error(404, "issue_not_found", "Карточка не найдена")
        if api_team.api_profile(cfg, issue.agent_kind) is None:
            return _json_error(409, "api_team_not_selected", "Для карточки не назначена API-команда")
        if action in {"publish-check", "accept"} and not api_team.uses_board_queue(cfg, issue.agent_kind):
            return _json_error(409, "release_destination_invalid", "Доставка доступна через штатный профиль API-доски")
        result = await api_team.invoke(cfg, issue.agent_kind, identifier, action, payload=payload)
        if action in {"confirm", "submit", "stop", "publish-check", "accept"}:
            orchestrator.request_refresh()
        return web.json_response(result, headers={"Cache-Control": "no-store, private"})
    app.router.add_get("/api/v1/issues/{identifier}/api-team", _wrap(handle))
    for action in ("prepare", "confirm", "submit", "stop", "review", "attach-check", "publish-check", "accept"):
        app.router.add_post("/api/v1/issues/{identifier}/api-team/" + action,
                            _wrap(_with_action(handle, action)))


def _with_action(handle, action):
    async def wrapped(request):
        return await handle(request, action)
    return wrapped
