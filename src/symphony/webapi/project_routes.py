"""Routes: project registry."""

from __future__ import annotations

import asyncio
from pathlib import Path
import tomllib
from typing import Any

from aiohttp import web

from ..errors import SymphonyError
from ..issue import registration_order_key
from ..logging import get_logger
from ..projects import (
    Project,
    ProjectError,
    ProjectRegistry,
    ProjectTargetExpectation,
    canonical_project_repo,
    validate_id,
    validate_port,
)
from ..service import is_symphony_workflow_reachable
from ..trackers.file import FileBoardTracker
from ..workflow import ServiceConfig, build_service_config, load_workflow
from .context import _Ctx
from .issue_routes import can_start_issue, check_start_request, startable_issue
from .payloads import _columns_payload, _issue_card
from .validation import _check_identifier, _valid_states
from .security import (
    _LOOPBACK_BINDS,
    BIND_HOST_KEY,
    TRUSTED_ORIGINS_ENV,
    _json_error,
    _host_is_loopback,
    _origin_is_trusted,
    _read_json,
    _request_host,
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


def _saved_projects(cfg: ServiceConfig) -> list[Project]:
    """Discover sibling boards for the fork's external project layout."""
    current = cfg.workflow_path.resolve().parent
    root = (Path.home() / "symphony-projects").resolve()
    if current.parent != root or not (current / "project.toml").is_file():
        return []
    projects = []
    for folder in sorted(root.iterdir()):
        settings = folder / "project.toml"
        workflow = folder / "WORKFLOW.md"
        try:
            if (folder.is_symlink() or settings.is_symlink() or workflow.is_symlink()
                    or not settings.is_file() or not workflow.is_file()
                    or settings.stat().st_size > 65536):
                continue
            item = tomllib.loads(settings.read_text(encoding="utf-8"))["project"]
            port = item["port"]
            if item["name"] != folder.name or type(port) is not int:
                continue
            validate_id(folder.name)
            validate_port(port)
            repo = Path(item["repo"])
            if repo.is_absolute():
                projects.append(Project(folder.name, folder.name, str(repo),
                                        str(workflow), "127.0.0.1", port))
        except (OSError, KeyError, ValueError, ProjectError, TypeError):
            continue
    return projects


def _preview_workflow(project: Project) -> ServiceConfig:
    cfg = build_service_config(load_workflow(project.workflow), log_decisions=False)
    board = cfg.tracker.board_root
    if (cfg.tracker.kind != "file" or board is None or not board.is_dir()
            or cfg.workflow_path != Path(project.workflow)
            or board != cfg.workflow_path.parent / "kanban"):
        raise ProjectError("saved file board unavailable")
    return cfg


async def _ready_service(project: Project, *, require_manual_start: bool = False) -> bool:
    from ..service import load_record

    record = await asyncio.to_thread(load_record, project.workflow)
    if record is not None and record.port != project.port:
        return False
    return await asyncio.to_thread(
        is_symphony_workflow_reachable, project.host, project.port, project.workflow,
        service_instance_id=record.service_instance_id if record else None,
        require_manual_start=require_manual_start,
    )


def _register_project_routes(app: web.Application, ctx: _Ctx) -> None:
    registry = ProjectRegistry()

    def visible_projects(request: web.Request) -> tuple[list[Project], set[str]]:
        projects = registry.list()
        if (request.app.get(BIND_HOST_KEY, "127.0.0.1") not in _LOOPBACK_BINDS
                or not _host_is_loopback(_request_host(request))
                or not _request_is_loopback(request)):
            return projects, set()
        saved = _saved_projects(ctx.config())
        # A managed board's deployment metadata owns its port and layout.
        saved_paths = {Path(project.workflow).resolve() for project in saved}
        saved_ids = {project.id for project in saved}
        others = [project for project in projects if project.id not in saved_ids
                  and Path(project.workflow).resolve() not in saved_paths]
        return [*saved, *others], saved_ids

    async def handle_projects(request: web.Request) -> web.Response:
        try:
            projects, saved_ids = await asyncio.to_thread(visible_projects, request)
            statuses = await asyncio.gather(
                *(
                    _ready_service(project)
                    if project.id in saved_ids
                    else asyncio.to_thread(registry.status, project.id)
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
                    "saved": project.id in saved_ids,
                    "url": _project_url(project, None if status_error else status),
                }
            )
        return web.json_response({"projects": payload, "current": current})

    async def handle_current(request: web.Request) -> web.Response:
        try:
            projects, _ = await asyncio.to_thread(visible_projects, request)
        except ProjectError as exc:
            return _json_error(409, "project_registry_error", str(exc))
        current = await asyncio.to_thread(_current_project_payload, ctx, projects)
        return web.json_response({"current": current})

    async def handle_saved_board(request: web.Request) -> web.Response:
        project_id = request.match_info["project_id"]
        identifier = request.match_info.get("identifier")
        if identifier is not None:
            identifier = _check_identifier(identifier)

        def read() -> dict[str, Any] | None:
            projects, saved_ids = visible_projects(request)
            if project_id not in saved_ids:
                return None
            project = next(project for project in projects if project.id == project_id)
            cfg = _preview_workflow(project)
            tracker = FileBoardTracker(cfg.tracker, read_only=True)
            states = _valid_states(cfg)
            if identifier is not None:
                issue = tracker.fetch_issue_full_by_id(identifier)
                if issue is None:
                    return None
                return {
                    **_issue_card(issue), "description": issue.description,
                    "can_start": can_start_issue(cfg, issue),
                }
            issues = tracker.fetch_issues_by_states(states.values())
            return {
                "board": {"name": project.name, "tracker_kind": "file", "read_only": True,
                          "path": str(tracker.board_root)},
                "columns": _columns_payload(cfg),
                "issues": [_issue_card(issue) for issue in sorted(issues, key=registration_order_key)],
                "live": {},
            }

        try:
            payload = await asyncio.to_thread(read)
        except (OSError, ValueError, ProjectError, SymphonyError):
            return _json_error(409, "saved_board_unavailable", "Сохранённая доска недоступна")
        if payload is None:
            return _json_error(404, "saved_board_not_found", "Доска или карточка не найдена")
        return web.json_response(payload, headers={"Cache-Control": "no-store, private"})

    async def handle_start_saved_issue(request: web.Request) -> web.Response:
        if (denied := _project_mutation_error(request)) is not None:
            return denied
        if (denied := check_start_request(request)) is not None:
            return denied
        project_id = request.match_info["project_id"]
        identifier = _check_identifier(request.match_info["identifier"])
        body = await _read_json(request)
        projects, saved_ids = await asyncio.to_thread(visible_projects, request)
        if project_id not in saved_ids:
            return _json_error(404, "saved_board_not_found", "Сохранённая доска не найдена")
        project = next(project for project in projects if project.id == project_id)
        try:
            cfg = await asyncio.to_thread(_preview_workflow, project)
            if not cfg.tracker.manual_start:
                return _json_error(409, "manual_start_required",
                                   "Для этой доски сначала нужно применить режим ручного запуска")
            tracker = FileBoardTracker(cfg.tracker, read_only=True)
            try:
                issue = await startable_issue(tracker, cfg, identifier, body.get("updated_at"))
            except SymphonyError as exc:
                return _json_error(409, "start_changed", exc.message)
            from ..service import main as service_command

            if await _ready_service(project):
                if not await _ready_service(project, require_manual_start=True):
                    return _json_error(409, "live_manual_start_required",
                                       "Работающая служба ещё не подтвердила ручной режим")
            else:
                code = await asyncio.to_thread(
                    service_command, ["start", project.workflow, "--host", project.host,
                                      "--port", str(project.port), "--require-manual-start"]
                )
                if code != 0:
                    raise ProjectError("service start failed")
                for _ in range(20):
                    if await _ready_service(project, require_manual_start=True):
                        break
                    await asyncio.sleep(0.2)
                else:
                    raise ProjectError("expected service unavailable")
        except (OSError, ValueError, ProjectError, SymphonyError) as exc:
            log.warning("saved_board_start_failed", project_id=project_id, error=str(exc))
            return _json_error(409, "board_start_failed", "Службу доски не удалось запустить")
        try:
            await asyncio.to_thread(tracker.request_start, issue)
        except SymphonyError:
            return _json_error(409, "start_changed", "Карточка изменилась. Открой её заново")
        return web.json_response({"url": _project_url(project), "queued": True}, status=202)

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
            _, saved_ids = await asyncio.to_thread(visible_projects, request)
            if project_id in saved_ids:
                return web.json_response({
                    "project_id": project_id,
                    "url": f"/?board={project_id}",
                })
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
    app.router.add_get("/api/v1/projects/{project_id}/board", _wrap(handle_saved_board))
    app.router.add_get("/api/v1/projects/{project_id}/board/{identifier}", _wrap(handle_saved_board))
    app.router.add_post("/api/v1/projects/{project_id}/board/{identifier}/start",
                        _wrap(handle_start_saved_issue))
    app.router.add_post("/api/v1/projects", _wrap(handle_create))
    app.router.add_post("/api/v1/projects/{project_id}/open", _wrap(handle_open))
