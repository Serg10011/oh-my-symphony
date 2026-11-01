"""Inventory saved workspaces; never infer permission to delete from Git state."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tomllib
from pathlib import Path

import yaml

METADATA = {".symphony-workspace-owners", ".symphony-workspace-hook-output"}
GIT_TIMEOUT_S = 10
CARD_HEADER_LIMIT = 65536
KINDS = {
    "worktree": "зарегистрированная рабочая копия Git",
    "repository": "самостоятельный репозиторий",
    "unregistered_worktree": "Git-копия без подтверждённой регистрации",
    "directory": "папка без собственного Git",
    "file": "файл",
    "linked_path": "ссылка — не обходится",
    "service_metadata": "служебные данные Symphony",
    "unreadable": "не удалось прочитать",
}


class ReportError(Exception):
    """A read-only query failed or the input cannot be interpreted."""


def _git(path: Path, *args: str, absent_ok: bool = False) -> str | None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1")
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c",
             "core.untrackedCache=false", "-C", str(path), *args],
            capture_output=True, text=True, encoding="utf-8", errors="surrogateescape",
            timeout=GIT_TIMEOUT_S, env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ReportError(f"не выполнен ограниченный запрос git {args[0]}") from None
    if result.returncode == 1 and absent_ok:
        return None
    if result.returncode:
        raise ReportError(f"git {args[0]}: код {result.returncode}")
    return result.stdout


def _directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ReportError(f"нужна существующая папка без ссылки: {path}")


def _boards(root: Path, errors: list[str]) -> dict[str, dict]:
    _directory(root)
    owners: dict[str, dict] = {}
    duplicates: set[str] = set()
    for board in sorted(root.iterdir()):
        if board.is_symlink():
            errors.append(f"{board}: ссылка на доску не обходилась")
            continue
        if not board.is_dir():
            continue
        config = board / "project.toml"
        if not config.exists() and not config.is_symlink():
            continue
        try:
            if config.is_symlink():
                raise ReportError("project.toml является ссылкой")
            data = tomllib.loads(config.read_text(encoding="utf-8"))["project"]
            name, repo = data["name"], data["repo"]
            if (not isinstance(name, str) or name in {"", ".", ".."}
                    or Path(name).name != name or not isinstance(repo, str)
                    or not Path(repo).expanduser().is_absolute()):
                raise ReportError("неверные project.name или project.repo")
            if name in owners or name in duplicates:
                duplicates.add(name)
                owners.pop(name, None)
                raise ReportError(f"несколько досок с project.name={name}")
            owners[name] = {"board": board, "repo": Path(repo).expanduser()}
        except (OSError, ValueError, UnicodeError, KeyError, TypeError, ReportError) as exc:
            errors.append(f"{config}: {exc}")
    return owners


def _status_counts(status: str) -> tuple[int, int]:
    tracked = untracked = 0
    fields = iter(status.split("\0"))
    for field in fields:
        if not field:
            continue
        if field.startswith("?? "):
            untracked += 1
        else:
            tracked += 1
            if "R" in field[:2] or "C" in field[:2]:
                next(fields, None)
    return tracked, untracked


def _git_info(path: Path) -> dict:
    common = Path(_git(path, "rev-parse", "--path-format=absolute", "--git-common-dir").removesuffix("\n"))
    top = Path(_git(path, "rev-parse", "--show-toplevel").removesuffix("\n"))
    if top.resolve() != path.resolve():
        raise ReportError("Git относится к другой корневой папке")
    records = _git(path, "worktree", "list", "--porcelain", "-z")
    registered = any(
        Path(field.removeprefix("worktree ")).resolve() == path.resolve()
        for field in records.split("\0") if field.startswith("worktree ")
    )
    kind = "unregistered_worktree"
    if (path / ".git").is_dir() and common.resolve() == (path / ".git").resolve():
        kind = "repository"
    elif registered:
        kind = "worktree"
    filters = _git(path, "config", "--null", "--name-only", "--get-regexp",
                   r"^filter\..*\.(clean|process)$", absent_ok=True)
    tracked = untracked = None
    if not filters:
        tracked, untracked = _status_counts(_git(
            path, "status", "--porcelain=v1", "-z", "--untracked-files=normal", "--ignore-submodules=all",
        ))
    ignored = _git(path, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z")
    branch = _git(path, "symbolic-ref", "--quiet", "--short", "HEAD", absent_ok=True)
    head = _git(path, "rev-parse", "--verify", "--quiet", "HEAD", absent_ok=True)
    return {"kind": kind, "common_dir": str(common.resolve()), "registered": registered,
            "branch": branch.removesuffix("\n") if branch else None,
            "head": head.removesuffix("\n") if head else None,
            "status_checked": not bool(filters), "external_filters": bool(filters),
            "tracked_changes": tracked, "untracked_entries": untracked,
            "ignored_entries": sum(bool(field) for field in ignored.split("\0"))}


def _card_state(board: Path, identifier: str) -> str | None:
    folder = board / "kanban"
    path = folder / f"{identifier}.md"
    if folder.is_symlink() or path.is_symlink():
        raise ReportError("папка карточек или карточка является ссылкой")
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return None
    with handle:
        if handle.readline(CARD_HEADER_LIMIT + 1).strip() != b"---":
            raise ReportError("неверный заголовок карточки")
        header = bytearray()
        while True:
            line = handle.readline(CARD_HEADER_LIMIT - len(header) + 1)
            if not line or len(header) + len(line) > CARD_HEADER_LIMIT:
                raise ReportError("не завершён или слишком велик заголовок карточки")
            if line.strip() == b"---":
                break
            header.extend(line)
    try:
        data = yaml.safe_load(header.decode("utf-8"))
    except yaml.YAMLError:
        raise ReportError("неверный YAML карточки") from None
    if (not isinstance(data, dict) or data.get("identifier", data.get("id")) != identifier
            or any(data[key] != identifier for key in ("identifier", "id") if key in data)
            or not isinstance(data.get("state"), str) or not data["state"].strip()):
        raise ReportError("не совпадает номер карточки или не задано состояние")
    return data["state"]


def _entry(path: Path, owner: dict | None, errors: list[str]) -> dict:
    row = {"path": str(path), "kind": "directory", "git": None,
           "repository_matches_board": None, "card_state": None, "notes": []}
    try:
        if path.is_symlink() or (path / ".git").is_symlink():
            row.update(kind="linked_path")
            row["notes"].append("ссылка не обходилась; принадлежность не подтверждена")
            return row
        if path.name in METADATA:
            row.update(kind="service_metadata")
            row["notes"].append("данные владения или журналов службы; это не рабочая копия")
            return row
        if not path.is_dir():
            row.update(kind="file")
            row["notes"].append("не является рабочей папкой")
            return row
        if not (path / ".git").exists():
            row["notes"].append("нет собственного Git; назначение нужно выяснить")
            return row
        info = _git_info(path)
        row.update(kind=info["kind"], git=info)
        if not info["status_checked"]:
            note = "состояние файлов не проверено: настроены внешние Git-фильтры"
            row["notes"].append(note)
            errors.append(f"{path}: {note}")
        if owner and owner.get("common_dir"):
            row["repository_matches_board"] = info["common_dir"] == owner["common_dir"]
        if row["repository_matches_board"]:
            row["card_state"] = _card_state(owner["board"], path.name)
            if row["card_state"] is None:
                row["notes"].append("карточка с точным именем папки не найдена")
        else:
            row["notes"].append("принадлежность доске не подтверждена")
        if info["tracked_changes"] or info["untracked_entries"]:
            row["notes"].append("есть незакоммиченные или неотслеживаемые файлы")
        if info["ignored_entries"]:
            row["notes"].append("есть игнорируемые данные вне обычного статуса Git")
        if row["kind"] == "repository":
            row["notes"].append("здесь собственная история Git; это не временная Git-копия")
        row["notes"].append("состояние Git и карточки не разрешает удаление")
    except (OSError, ReportError, UnicodeError) as exc:
        row.update(kind="unreadable")
        row["notes"].append(str(exc))
        errors.append(f"{path}: {exc}")
    return row


def inventory(workspace_root: Path, projects_root: Path) -> dict:
    workspace_root, projects_root = workspace_root.expanduser().absolute(), projects_root.expanduser().absolute()
    _directory(workspace_root)
    errors: list[str] = []
    owners = _boards(projects_root, errors)
    for owner in owners.values():
        try:
            owner["common_dir"] = str(Path(_git(
                owner["repo"], "rev-parse", "--path-format=absolute", "--git-common-dir",
            ).removesuffix("\n")).resolve())
        except (OSError, ReportError) as exc:
            errors.append(f"{owner['repo']}: {exc}")
    projects = []
    for folder in sorted(workspace_root.iterdir()):
        owner = owners.get(folder.name)
        group = {"path": str(folder), "name": folder.name,
                 "board": str(owner["board"]) if owner else None,
                 "repository": str(owner["repo"]) if owner else None, "entries": []}
        if folder.is_symlink() or not folder.is_dir() or (folder / ".git").exists():
            group["entries"].append(_entry(folder, owner, errors))
        else:
            try:
                group["entries"] = [_entry(path, owner, errors) for path in sorted(folder.iterdir())]
            except OSError as exc:
                errors.append(f"{folder}: {exc}")
        projects.append(group)
    return {"version": 1, "read_only": True, "deletion_assessed": False,
            "workspace_root": str(workspace_root), "projects_root": str(projects_root),
            "complete": not errors, "projects": projects, "errors": errors,
            "limitations": ["обзор не является атомарным снимком; занятость процессов не проверяется",
                            "Done и чистый Git не подтверждают сохранность данных или допустимость удаления",
                            "игнорируемые записи считаются без чтения содержимого; подмодули не проверяются",
                            "обходятся только два уровня указанного корня; метки владения службы не проверяются"]}


def render(report: dict) -> str:
    lines = ["Обзор рабочих копий — только чтение. Удаление не оценивается."]
    for project in report["projects"]:
        lines.append(f"\n{json.dumps(project['name'], ensure_ascii=False)}:")
        if not project["entries"]:
            lines.append("  Папка пуста.")
        for row in project["entries"]:
            lines.append(f"  {json.dumps(row['path'], ensure_ascii=False)} — {KINDS[row['kind']]}")
            if row["git"]:
                info = row["git"]
                lines.append(f"    Ветка: {json.dumps(info['branch'], ensure_ascii=False)}; HEAD: {info['head'] or 'нет коммитов'}")
                if info["status_checked"]:
                    lines.append(f"    Изменённых: {info['tracked_changes']}; неотслеживаемых: {info['untracked_entries']}; игнорируемых записей: {info['ignored_entries']}")
                else:
                    lines.append(f"    Состояние файлов не проверено; игнорируемых записей: {info['ignored_entries']}")
                lines.append(f"    Общая Git-папка: {json.dumps(info['common_dir'], ensure_ascii=False)}")
            if row["card_state"]:
                lines.append(f"    Карточка: {json.dumps(row['card_state'], ensure_ascii=False)}")
            lines.extend("    " + note for note in row["notes"])
    lines.extend("\nОграничение: " + text for text in report["limitations"])
    lines.extend("ОШИБКА: " + text for text in report["errors"])
    lines.append("\nОбзор полный." if report["complete"] else "\nОбзор неполный: смотри ошибки.")
    return "\n".join(lines)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Обзор рабочих копий без изменений и удаления")
    parser.add_argument("--workspace-root", type=Path, default=Path.home() / "symphony_workspaces")
    parser.add_argument("--projects-root", type=Path, default=Path.home() / "symphony-projects")
    parser.add_argument("--json", action="store_true", help="вывести JSON")
    args = parser.parse_args(argv)
    try:
        report = inventory(args.workspace_root, args.projects_root)
    except (OSError, ReportError) as exc:
        report = {"version": 1, "read_only": True, "deletion_assessed": False,
                  "complete": False, "projects": [], "errors": [str(exc)], "limitations": []}
    text = json.dumps(report, ensure_ascii=True, indent=2) if args.json else render(report)
    print(text.encode("utf-8", errors="backslashreplace").decode("utf-8"))
    return 0 if report["complete"] else 1
