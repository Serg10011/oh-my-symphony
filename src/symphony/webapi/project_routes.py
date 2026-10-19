"""Routes: project registry."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from aiohttp import web

from ..logging import get_logger
from ..projects import (
    Project,
    ProjectError,
    ProjectRegistry,
    ProjectTargetExpectation,
    canonical_project_repo,
)
from .context import _Ctx
from .security import (
    _LOOPBACK_BINDS,
    BIND_HOST_KEY,
    TRUSTED_ORIGINS_ENV,
    _json_error,
    _origin_is_trusted,
    _read_json,
    _request_is_loopback,
    _wrap,
)

log = get_logger()


def _status_is_running(status: Any) -> bool:
    if isinstance(status, bool):
        return status
    if isinstance(status, dict):
        if "running" in status:
            return bool(status["running"])
        return str(status.get("state", "")).lower() == "running"
    return str(getattr(status, "state", "")).lower() == "running"


def _project_url(project: Project, status: Any | None = None) -> str:
    record = getattr(status, "record", None) if status is not None else None
    host = str(getattr(record, "host", None) or project.host)
    port = int(getattr(record, "port", None) or project.port)
    if host in {"", "0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}/"


def _create_or_adopt_registered_project(
    registry: ProjectRegistry,
    *,
    name: str,
    path: Path,
    expected_target: ProjectTargetExpectation | None = None,
    preset: str = "default",
) -> Project:
    """Keep the web boundary thin around the shared project setup service."""
    from ..projects import create_or_adopt_project, source_checkout

    return create_or_adopt_project(
        path,
        source=source_checkout(),
        registry=registry,
        name=name,
        expected_target=expected_target,
        preset=preset,
    )


def _current_project_payload(ctx: _Ctx, projects: list[Project]) -> dict[str, Any]:
    cfg = ctx.config()
    workflow = cfg.workflow_path.expanduser().resolve()
    current = next(
        (
            project
            for project in projects
            if Path(project.workflow).expanduser().resolve() == workflow
        ),
        None,
    )
    repo = (
        Path(current.git_repo).expanduser().resolve()
        if current is not None
        else canonical_project_repo(workflow.parent)
    )
    board = cfg.tracker.board_root
    return {
        "id": current.id if current is not None else None,
        "name": current.name if current is not None else repo.name,
        "repo_path": str(repo),
        "workflow_path": str(workflow),
        "board_path": str(board.expanduser().resolve()) if board is not None else None,
        "registered": current is not None,
    }


def _project_mutation_error(request: web.Request) -> web.Response | None:
    """Project setup can write arbitrary paths, so it is loopback-only."""
    bind = str(request.app.get(BIND_HOST_KEY) or "127.0.0.1").lower()
    if bind not in _LOOPBACK_BINDS:
        return _json_error(
            403, "project_mutation_forbidden", "project management is loopback-only"
        )
    if request.remote is None or not _request_is_loopback(request):
        return _json_error(
            403, "project_mutation_forbidden", "project management is loopback-only"
        )
    origin = request.headers.get("Origin")
    if not _origin_is_trusted(request, origin or ""):
        return _json_error(
            403,
            "forbidden_origin",
            f"origin {origin!r} may not manage projects; set "
            f"{TRUSTED_ORIGINS_ENV}={origin} if you front this board with a "
            "reverse proxy or tunnel",
        )
    if request.content_length is not None and request.content_length > 16_384:
        return _json_error(413, "request_too_large", "project request is too large")
    return None


def _register_project_routes(app: web.Application, ctx: _Ctx) -> None:
    registry = ProjectRegistry()

    async def handle_projects(_request: web.Request) -> web.Response:
        try:
            projects = await asyncio.to_thread(registry.list)
            statuses = await asyncio.gather(
                *(
                    asyncio.to_thread(registry.status, project.id)
                    for project in projects
                ),
                return_exceptions=True,
            )
        except ProjectError as exc:
            return _json_error(409, "project_registry_error", str(exc))
        current = await asyncio.to_thread(_current_project_payload, ctx, projects)
        payload = []
        for project, status in zip(projects, statuses, strict=True):
            status_error = isinstance(status, BaseException)
            running = False if status_error else _status_is_running(status)
            payload.append(
                {
                    "id": project.id,
                    "name": project.name,
                    "repo_path": str(Path(project.git_repo).expanduser().resolve()),
                    "workflow_path": str(Path(project.workflow).expanduser().resolve()),
                    "host": project.host,
                    "port": project.port,
                    "running": running,
                    "status_error": "status unavailable" if status_error else None,
                    "current": project.id == current["id"],
                    "url": _project_url(project, None if status_error else status),
                }
            )
        return web.json_response({"projects": payload, "current": current})

    async def handle_current(_request: web.Request) -> web.Response:
        try:
            projects = await asyncio.to_thread(registry.list)
        except ProjectError as exc:
            return _json_error(409, "project_registry_error", str(exc))
        current = await asyncio.to_thread(_current_project_payload, ctx, projects)
        return web.json_response({"current": current})

    async def handle_create(request: web.Request) -> web.Response:
        denied = _project_mutation_error(request)
        if denied is not None:
            return denied
        body = await _read_json(request)
        name = body.get("name")
        raw_path = body.get("path")
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            return _json_error(
                400, "invalid_project_name", "name must be 1-100 characters"
            )
        if (
            not isinstance(raw_path, str)
            or not raw_path.strip()
            or len(raw_path) > 4096
        ):
            return _json_error(
                400, "invalid_project_path", "path must be 1-4096 characters"
            )
        try:
            project = await asyncio.to_thread(
                _create_or_adopt_registered_project,
                registry,
                name=name.strip(),
                path=Path(raw_path).expanduser(),
            )
        except ProjectError as exc:
            return _json_error(409, "project_setup_failed", str(exc))
        return web.json_response(
            {
                "project": {
                    "id": project.id,
                    "name": project.name,
                    "repo_path": str(Path(project.git_repo).expanduser().resolve()),
                    "workflow_path": str(Path(project.workflow).expanduser().resolve()),
                    "url": _project_url(project),
                }
            },
            status=201,
        )

    async def handle_open(request: web.Request) -> web.Response:
        if request.content_type != "application/json":
            return _json_error(
                415, "unsupported_media_type", "mutations require application/json"
            )
        denied = _project_mutation_error(request)
        if denied is not None:
            return denied
        project_id = request.match_info["project_id"]
        try:
            project = await asyncio.to_thread(registry.get, project_id)
            status = await asyncio.to_thread(registry.status, project_id)
            if not _status_is_running(status):
                result = await asyncio.to_thread(registry.start, project_id)
                if isinstance(result, int) and result != 0:
                    raise ProjectError(
                        f"could not start {project.name!r}; run "
                        f"`symphony service status {project.workflow}` and inspect "
                        "the service log, then retry"
                    )
                status = await asyncio.to_thread(registry.status, project_id)
            if not _status_is_running(status):
                raise ProjectError(
                    f"{project.name!r} did not report running; run "
                    f"`symphony service status {project.workflow}` and inspect "
                    "the service log, then retry"
                )
        except ProjectError as exc:
            missing = str(exc).startswith("unknown project ")
            return _json_error(
                404 if missing else 409,
                "project_not_found" if missing else "project_open_failed",
                str(exc),
            )
        except Exception as exc:
            log.warning("project_open_failed", project_id=project_id, error=str(exc))
            return _json_error(409, "project_open_failed", "could not open project")
        return web.json_response(
            {
                "project_id": project.id,
                "running": True,
                "url": _project_url(project, status),
            }
        )

    app.router.add_get("/api/v1/projects", _wrap(handle_projects))
    app.router.add_get("/api/v1/projects/current", _wrap(handle_current))
    app.router.add_post("/api/v1/projects", _wrap(handle_create))
    app.router.add_post("/api/v1/projects/{project_id}/open", _wrap(handle_open))
