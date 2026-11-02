"""Read project metadata and existing service GET APIs; expose no mutations."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from aiohttp import web

ASSETS = Path(__file__).resolve().parents[1] / "templates"
MAX_BYTES = 4 * 1024 * 1024
MAX_ISSUES = 5000
REQUEST_TIMEOUT_S = 2
IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
STATES = {
    "todo": "К выполнению", "plan": "Планирование", "in progress": "Выполняется",
    "verify": "Проверка", "document": "Документация", "subtasks": "Выполняются подзадачи",
    "merging": "Сборка результата", "human review": "Ждёт тебя", "needs_human": "Нужна помощь",
    "blocked": "Заблокирована", "done": "Готово", "archive": "Архив",
    "cancelled": "Отменена", "canceled": "Отменена",
}


class DataError(ValueError):
    """The service returned an incomplete or incompatible snapshot."""


def _bucket(state: str, terminal: set[str]) -> str:
    key = state.casefold()
    if key == "human review":
        return "review"
    if key in {"needs_human", "blocked"}:
        return "help"
    if key in {"done", "archive", "cancelled", "canceled"}:
        return "completed"
    if key in {"subtasks", "merging"} or key not in terminal:
        return "working"
    return "other"


def _text(value, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise DataError("Недопустимые данные карточек")
    return value


def board_view(board: dict, state: dict) -> dict:
    if not isinstance(board, dict) or not isinstance(state, dict):
        raise DataError("Неполный ответ службы")
    if not isinstance(board.get("board"), dict) or board["board"].get("tracker_kind") != "file":
        raise DataError("Этот тип доски пока не поддерживается")
    columns, issues = board.get("columns"), board.get("issues")
    if not isinstance(columns, list) or not isinstance(issues, list) or len(issues) > MAX_ISSUES:
        raise DataError("Неполный или слишком большой список карточек")
    terminal = set()
    for column in columns:
        if not isinstance(column, dict) or type(column.get("terminal")) is not bool:
            raise DataError("Неизвестные состояния доски")
        name = _text(column.get("name"), 200)
        if column["terminal"]:
            terminal.add(name.casefold())
    counts = state.get("counts")
    if not isinstance(counts, dict) or any(type(counts.get(k)) is not int or counts[k] < 0 for k in ("running", "retrying")):
        raise DataError("Неизвестное состояние выполнения")
    summary = dict(working=0, review=0, help=0, completed=0, other=0)
    nodes, parents = {}, {}
    for issue in issues:
        if not isinstance(issue, dict):
            raise DataError("Недопустимая карточка")
        identifier = _text(issue.get("identifier"), 64)
        if not IDENTIFIER.fullmatch(identifier) or identifier in nodes:
            raise DataError("Некорректный или повторный номер карточки")
        status = _text(issue.get("state"), 200)
        bucket = _bucket(status, terminal)
        summary[bucket] += 1
        nodes[identifier] = {"key": identifier, "kind": "issue", "identifier": identifier,
                             "title": _text(issue.get("title"), 1000), "state": status,
                             "state_label": STATES.get(status.casefold(), status), "bucket": bucket}
        parents[identifier] = _text(issue.get("request", ""), 300, empty=True).strip() or None
    for key, parent in list(parents.items()):
        if parent and parent not in nodes:
            group = "group/" + parent
            nodes.setdefault(group, {"key": group, "kind": "group", "title": "Группа " + parent})
            parents[key] = group
    warnings, visited = [], set()
    for key in parents:
        chain, positions, current = [], {}, key
        while current in parents and current not in visited:
            if current in positions:
                for member in chain[positions[current]:]:
                    parents[member] = None
                warnings = ["Циклические связи: соответствующие карточки показаны отдельно."]
                break
            positions[current] = len(chain)
            chain.append(current)
            current = parents[current]
        visited.update(chain)
    children = {key: [] for key in nodes}
    roots = []
    for key in nodes:
        parent = parents.get(key)
        (children[parent] if parent else roots).append(key)
    trees = []
    for key in roots:
        rows, pending = [], [(key, 0)]
        while pending:
            identifier, depth = pending.pop()
            rows.append(dict(nodes[identifier], depth=depth))
            pending.extend((child, depth + 1) for child in reversed(children[identifier]))
        cards = [row for row in rows if row["kind"] == "issue"]
        trees.append({"key": key, "completed": all(row["bucket"] == "completed" for row in cards), "rows": rows})
    return {"summary": summary, "trees": trees, "warnings": warnings,
            "runtime": {"running": counts["running"], "retrying": counts["retrying"],
                        "dispatch_held": state.get("dispatch_hold") is not None}}


def discover(root: Path) -> list[dict]:
    if root.is_symlink() or not root.is_dir():
        raise DataError("Папка проектов недоступна")
    projects = []
    for folder in sorted(root.iterdir()):
        if folder.is_symlink() or not folder.is_dir():
            continue
        path = folder / "project.toml"
        if not path.exists() and not path.is_symlink():
            continue
        row = {"id": folder.name, "name": folder.name}
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
                raise DataError("Настройки проекта недоступны")
            project = tomllib.loads(path.read_text(encoding="utf-8"))["project"]
            name, port = _text(project["name"], 200), project["port"]
            if type(port) is not int or not 1 <= port <= 65535:
                raise DataError("Некорректный порт проекта")
            workflow = folder / "WORKFLOW.md"
            if workflow.is_symlink() or not workflow.is_file():
                raise DataError("Описание проекта недоступно")
            row.update(name=name, base=f"http://127.0.0.1:{port}", workflow=str(workflow.resolve()))
        except (OSError, ValueError, KeyError, TypeError):
            row["error"] = "Не удалось прочитать настройки проекта"
        projects.append(row)
    return projects


def _reject_constant(value):
    raise DataError("Некорректный JSON службы")


async def fetch_json(session: aiohttp.ClientSession, url: str):
    async with session.get(url, allow_redirects=False) as response:
        if response.status != 200:
            raise DataError("Служба не отдала данные")
        chunks, size = [], 0
        async for chunk in response.content.iter_chunked(65536):
            size += len(chunk)
            if size > MAX_BYTES:
                raise DataError("Ответ службы слишком большой")
            chunks.append(chunk)
        try:
            return json.loads(b"".join(chunks).decode("utf-8"), parse_constant=_reject_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise DataError("Некорректный JSON службы") from None


async def project_snapshot(project: dict, fetch) -> dict:
    result = {"id": project["id"], "name": project["name"], "service_state": "error",
              "message": project.get("error", ""), "board_url": None,
              "summary": None, "trees": [], "warnings": [], "runtime": None}
    if "error" in project:
        return result
    try:
        base = project["base"]
        health = await fetch(base + "/api/v1/health")
        workflow = health.get("workflow_path") if isinstance(health, dict) else None
        if not isinstance(workflow, str) or not Path(workflow).is_absolute() or str(Path(workflow).resolve()) != project["workflow"]:
            result.update(service_state="mismatch", message="По этому адресу отвечает другой проект")
            return result
        result["board_url"] = base + "/"
        board, state = await asyncio.gather(fetch(base + "/api/v1/board"), fetch(base + "/api/v1/state"), return_exceptions=True)
        for response in (board, state):
            if isinstance(response, BaseException):
                raise response
        result.update(board_view(board, state), service_state="running", message="")
    except (aiohttp.ClientError, asyncio.TimeoutError):
        result.update(service_state="unavailable", message="Нет связи со службой", board_url=None)
    except (OSError, ValueError, TypeError, RuntimeError):
        result.update(service_state="error", message="Не удалось прочитать данные службы")
    return result


async def snapshot(root: Path, fetch=None) -> dict:
    projects = discover(root)
    if fetch is None:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S), trust_env=False) as session:
            rows = await asyncio.gather(*(project_snapshot(p, lambda url: fetch_json(session, url)) for p in projects))
    else:
        rows = await asyncio.gather(*(project_snapshot(p, fetch) for p in projects))
    return {"version": 1, "read_only": True, "generated_at": datetime.now(timezone.utc).isoformat(), "projects": rows}


@web.middleware
async def guard(request, handler):
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
               "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"}
    host = request.host.rsplit(":", 1)[0]
    if host not in {"127.0.0.1", "localhost"}:
        return web.json_response({"error": "Доступ разрешён только локально"}, status=403, headers=headers)
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        exc.headers.update(headers)
        raise
    response.headers.update(headers)
    return response


def build_app(root: Path, fetch=None) -> web.Application:
    app = web.Application(middlewares=[guard])
    async def index(request):
        return web.FileResponse(ASSETS / "overview.html")
    async def script(request):
        return web.FileResponse(ASSETS / "overview.js")
    async def projects(request):
        try:
            return web.json_response(await snapshot(root, fetch))
        except (OSError, ValueError):
            return web.json_response({"error": "Папка проектов недоступна", "projects": []}, status=503)
    app.router.add_get("/", index)
    app.router.add_get("/overview.js", script)
    app.router.add_get("/api/projects", projects)
    return app


def run(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Локальный обзор проектов: только наблюдение")
    parser.add_argument("--projects-root", type=Path, default=Path.home() / "symphony-projects")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--snapshot", action="store_true", help="напечатать один JSON-снимок без веб-сервера")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("порт должен быть от 0 до 65535")
    root = args.projects_root.expanduser().absolute()
    if args.snapshot:
        try:
            data = asyncio.run(snapshot(root))
        except (OSError, ValueError):
            print(json.dumps({"error": "Папка проектов недоступна", "projects": []}, ensure_ascii=False))
            return 1
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    web.run_app(build_app(root), host="127.0.0.1", port=args.port, access_log=None)
    return 0
