"""Versioned script delivery for an existing, stopped project board.

Only manifest-listed setup.sh/test.sh/bin files are managed. Configuration,
generated prompts, credentials, cards, baselines and project code are not.
Preview performs no writes. Mutations keep a complete before/after journal;
an interrupted transaction must be rolled back before another installation.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import fcntl
import hashlib
import json
import os
import re
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import project_config as pc

STATE = ".ours-state/script-updates"
DIGEST = re.compile(r"^[0-9a-f]{64}$")
TX_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{12}$")
MAX_FILE = 2 * 1024 * 1024
MAX_JOURNAL = 32 * 1024 * 1024


class UpdateError(Exception):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def script_path(value: str) -> str:
    if not isinstance(value, str):
        raise UpdateError("путь скрипта должен быть строкой")
    path = PurePosixPath(value)
    if (not isinstance(value, str) or str(path) != value or path.is_absolute()
            or any(not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", p) for p in path.parts)
            or not (value in ("setup.sh", "test.sh") or (len(path.parts) > 1 and path.parts[0] == "bin"))):
        raise UpdateError(f"недопустимый путь скрипта: {value!r}")
    return value


def safe_path(root: Path, rel: str) -> Path:
    path = root
    for part in PurePosixPath(rel).parts:
        if part in ("", ".", ".."):
            raise UpdateError(f"недопустимый путь: {rel}")
        path /= part
        if path.is_symlink():
            raise UpdateError(f"ссылка вместо управляемого файла/каталога: {path}")
    return path


def read_file(path: Path, limit: int = MAX_FILE) -> dict | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_size > limit
            or stat.S_IMODE(info.st_mode) & ~0o777):
        raise UpdateError(f"нужен обычный файл размером до {limit} байт без специальных прав: {path}")
    data = path.read_bytes()
    return {"sha256": digest(data), "mode": stat.S_IMODE(info.st_mode),
            "data": base64.b64encode(data).decode("ascii")}


def stamp(item: dict | None) -> dict | None:
    return None if item is None else {k: item[k] for k in ("sha256", "mode")}


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode()


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def load_json(path: Path) -> dict | None:
    item = read_file(path, MAX_JOURNAL)
    if item is None:
        return None
    try:
        value = json.loads(base64.b64decode(item["data"]))
    except (ValueError, UnicodeError) as exc:
        raise UpdateError(f"повреждён JSON {path}: {exc}") from None
    if not isinstance(value, dict):
        raise UpdateError(f"ожидался объект JSON: {path}")
    return value


def validate_stamps(files: object) -> dict:
    if not isinstance(files, dict):
        raise UpdateError("повреждён список файлов установленной версии")
    for name, item in files.items():
        script_path(name)
        if (not isinstance(item, dict) or not DIGEST.fullmatch(str(item.get("sha256", "")))
                or type(item.get("mode")) is not int or not 0 <= item["mode"] <= 0o777):
            raise UpdateError(f"повреждена запись файла {name}")
    return files


def validate_receipt(board: Path, value: dict | None) -> dict | None:
    if value is not None:
        if (not isinstance(value, dict) or value.get("schema") != 1 or value.get("board") != str(board)
                or not pc.NAME_RE.fullmatch(str(value.get("kit", "")))
                or not TX_ID.fullmatch(str(value.get("transaction", "")))
                or not DIGEST.fullmatch(str(value.get("version", "")))):
            raise UpdateError("повреждена запись установленного комплекта")
        validate_stamps(value.get("files"))
    return value


def receipt(board: Path) -> dict | None:
    return validate_receipt(board, load_json(safe_path(board, f"{STATE}/current.json")))


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=30)
    if result.returncode:
        raise UpdateError(f"Git: не удалось прочитать {' '.join(args)} в {repo}")
    return result.stdout


@dataclass
class Bundle:
    name: str
    commit: str
    version: str
    files: dict
    adopt: dict
    machine_lock: bool
    legacy: list[str]


def load_bundle(repo: Path, name: str) -> Bundle:
    if not pc.NAME_RE.fullmatch(name):
        raise UpdateError("неверное имя комплекта")
    prefix = f"ours/projects/{name}"
    commit = git(repo, "rev-parse", "HEAD").decode().strip()
    try:
        manifest = json.loads(git(repo, "show", f"{commit}:{prefix}/bundle.json"))
    except ValueError:
        raise UpdateError("повреждён bundle.json") from None
    if (not isinstance(manifest, dict) or manifest.get("schema") != 1
            or manifest.get("name") != name or not isinstance(manifest.get("files"), dict)
            or not manifest["files"] or type(manifest.get("machine_test_lock", False)) is not bool):
        raise UpdateError("некорректный bundle.json")
    files, adopt = {}, {}
    for rel, rule in manifest["files"].items():
        script_path(rel)
        if not isinstance(rule, dict) or rule.get("mode") not in ("0644", "0755"):
            raise UpdateError(f"некорректные права для {rel}")
        accepted = rule.get("adopt_sha256", [])
        if not isinstance(accepted, list) or any(not DIGEST.fullmatch(str(x)) for x in accepted):
            raise UpdateError(f"некорректные исходные хеши для {rel}")
        entry = git(repo, "ls-tree", commit, "--", f"{prefix}/{rel}").decode()
        if not entry.startswith(("100644 blob ", "100755 blob ")):
            raise UpdateError(f"в Git нужен обычный файл: {rel}")
        data = git(repo, "show", f"{commit}:{prefix}/{rel}")
        if len(data) > MAX_FILE:
            raise UpdateError(f"слишком большой скрипт: {rel}")
        files[rel] = {"sha256": digest(data), "mode": int(rule["mode"], 8),
                      "data": base64.b64encode(data).decode("ascii")}
        adopt[rel] = accepted
    legacy = manifest.get("legacy_directory_lock_sha256", [])
    if not isinstance(legacy, list) or any(not DIGEST.fullmatch(str(x)) for x in legacy):
        raise UpdateError("некорректные хеши прежнего каталога-замка")
    version = digest(json_bytes({"manifest": manifest, "files": {k: stamp(v) for k, v in files.items()}}))
    return Bundle(name, commit, version, files, adopt, manifest.get("machine_test_lock", False), legacy)


def read_board(root: Path, name: str) -> tuple[Path, dict]:
    if not pc.NAME_RE.fullmatch(name):
        raise UpdateError("неверное имя доски")
    board = safe_path(root.resolve(), name)
    if not board.is_dir():
        raise UpdateError(f"нет доски {board}")
    safe_path(board, pc.PROJECT_FILE)
    project = pc.load_project(board)
    if project["project"]["name"] != name:
        raise UpdateError("имя доски не совпадает с project.toml")
    repo = Path(project["project"]["repo"]).resolve()
    if board == repo or board.is_relative_to(repo) or board.is_relative_to(pc.FORK_DIR.resolve()):
        raise UpdateError("папка доски должна находиться вне репозиториев кода")
    return board, project


def pending(board: Path) -> str | None:
    value = load_json(safe_path(board, f"{STATE}/pending.json"))
    if value is None:
        return None
    identifier = value.get("transaction", "")
    if not isinstance(identifier, str) or not TX_ID.fullmatch(identifier):
        raise UpdateError("повреждён указатель незавершённого обновления")
    return identifier


def plan(board: Path, bundle: Bundle) -> tuple[dict | None, dict, dict, list[str]]:
    previous = receipt(board)
    if previous and previous["kit"] != bundle.name:
        raise UpdateError(f"доской уже управляет комплект {previous['kit']}")
    owned = previous["files"] if previous else {}
    names = sorted(set(owned) | set(bundle.files))
    before = {name: read_file(safe_path(board, name)) for name in names}
    after = {name: bundle.files.get(name) for name in names}
    conflicts = []
    for name in names:
        actual = stamp(before[name])
        if name in owned:
            if actual != owned[name]:
                conflicts.append(f"{name}: локально изменён/удалён после установки")
        elif actual is not None and actual["sha256"] not in [
            bundle.files[name]["sha256"], *bundle.adopt[name],
        ]:
            conflicts.append(f"{name}: неизвестная локальная версия, перезапись запрещена")
    return previous, before, after, conflicts


def describe(before: dict, after: dict, show_diff: bool) -> list[str]:
    changed = [name for name in sorted(before) if stamp(before[name]) != stamp(after[name])]
    for name in changed:
        a, b = before[name], after[name]
        action = "добавить" if a is None else ("удалить управляемый файл" if b is None else "обновить")
        print(f"  {action}: {name} ({a['sha256'][:12] if a else 'нет'} → {b['sha256'][:12] if b else 'нет'})")
        if show_diff:
            old = base64.b64decode(a["data"]).decode("utf-8", "replace") if a else ""
            new = base64.b64decode(b["data"]).decode("utf-8", "replace") if b else ""
            sys.stdout.writelines(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                                      fromfile=f"{name} (установлено)", tofile=f"{name} (цель)"))
    return changed


def ensure_stopped(board: Path, project: dict, names: set[str]) -> None:
    port = project["project"]["port"]
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            raise UpdateError(f"порт доски {port} занят: сначала останови её службу")
    except ConnectionRefusedError:
        pass
    except OSError as exc:
        raise UpdateError(f"не удалось проверить порт доски: {exc}") from None
    if sys.platform == "darwin":
        result = subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/local.symphony.{board.name}"],
                                capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            raise UpdateError("служба загружена в launchd: сначала останови её")
        if result.returncode != 113:
            raise UpdateError("не удалось подтвердить отсутствие службы в launchd")
    result = subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise UpdateError("не удалось проверить процессы доски")
    targets = {str(board / name) for name in names | {"WORKFLOW.md", "setup.sh", "test.sh"}}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or parts[0] == str(os.getpid()):
            continue
        try:
            args = shlex.split(parts[1])
        except ValueError:
            args = parts[1].split()
        if targets.intersection(args):
            raise UpdateError(f"процесс {parts[0]} использует файлы доски; дождись его завершения")


@contextmanager
def mutation_lock(board: Path, project: dict, names: set[str], machine_lock: bool):
    ensure_stopped(board, project, names)
    folder = safe_path(board, STATE)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = safe_path(board, f"{STATE}/update.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    machine_fd = None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdateError("другая команда уже обновляет эту доску") from None
        if machine_lock:
            shared = safe_path(board.parent, ".locks/test.lock")
            if shared.is_dir():
                raise UpdateError("существует старый каталог test.lock: проверь прежний прогон; автоматической очистки нет")
            if shared.exists():
                machine_fd = os.open(shared, os.O_RDWR | os.O_NOFOLLOW)
                if not stat.S_ISREG(os.fstat(machine_fd).st_mode):
                    raise UpdateError("test.lock должен быть обычным файлом")
                try:
                    fcntl.flock(machine_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise UpdateError("идёт тестовый прогон (test.lock занят)") from None
        ensure_stopped(board, project, names)
        yield
    finally:
        if machine_fd is not None:
            os.close(machine_fd)
        os.close(fd)


def write_snapshot(board: Path, files: dict) -> None:
    for name, item in files.items():
        path = safe_path(board, script_path(name))
        if item is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, base64.b64decode(item["data"]), item["mode"])


def save_receipt(board: Path, value: dict | None) -> None:
    path = safe_path(board, f"{STATE}/current.json")
    if value is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write(path, json_bytes(value))


def read_transaction(board: Path, identifier: str) -> tuple[Path, dict]:
    if not TX_ID.fullmatch(identifier):
        raise UpdateError("неверный идентификатор резервной копии")
    path = safe_path(board, f"{STATE}/history/{identifier}.json")
    data = load_json(path)
    if (not data or data.get("schema") != 1 or data.get("board") != str(board)
            or data.get("id") != identifier or data.get("status") not in ("prepared", "applied", "rolled_back")
            or type(data.get("machine_lock")) is not bool or not isinstance(data.get("legacy"), list)
            or any(not DIGEST.fullmatch(str(x)) for x in data["legacy"])):
        raise UpdateError("резервная копия отсутствует, повреждена или относится к другой доске")
    for key in ("before", "after"):
        files = data.get(key)
        if not isinstance(files, dict):
            raise UpdateError("повреждён список файлов резервной копии")
        for name, item in files.items():
            script_path(name)
            if item is None:
                continue
            validate_stamps({name: item})
            try:
                raw = base64.b64decode(item["data"], validate=True)
            except (ValueError, KeyError, TypeError):
                raise UpdateError(f"повреждено содержимое {name} в резервной копии") from None
            if len(raw) > MAX_FILE or digest(raw) != item["sha256"]:
                raise UpdateError(f"не совпадает контрольная сумма резервной копии {name}")
    if set(data["before"]) != set(data["after"]):
        raise UpdateError("несогласованный состав резервной копии")
    validate_receipt(board, data.get("receipt_before"))
    return path, data


def check_restore(board: Path, identifier: str) -> tuple[Path, dict]:
    path, record = read_transaction(board, identifier)
    active = receipt(board)
    unfinished = pending(board)
    if unfinished and unfinished != identifier:
        raise UpdateError(f"сначала восстанови незавершённое обновление: --rollback {unfinished}")
    interrupted = unfinished == identifier
    if not interrupted and (not active or active["transaction"] != identifier):
        raise UpdateError("можно вернуть только последнюю установленную версию; более новые изменения не трогаю")
    for name, before in record["before"].items():
        actual = stamp(read_file(safe_path(board, name)))
        allowed = [stamp(record["after"][name])]
        if interrupted:
            allowed.append(stamp(before))
        if actual not in allowed:
            raise UpdateError(f"{name}: локальные изменения после установки; возврат остановлен")
    # Do not unlink the persistent machine lock: another participant may have
    # opened its inode already. Returning to the old mkdir protocol needs an
    # explicit, separately reviewed migration after all participants stop.
    before_test = record["before"].get("test.sh")
    if (before_test and before_test["sha256"] in record.get("legacy", [])
            and safe_path(board.parent, ".locks/test.lock").exists()):
        raise UpdateError("возврат к старому каталогу-замку требует отдельной миграции test.lock; файл не удалён")
    return path, record


def restore(board: Path, identifier: str) -> None:
    path, record = check_restore(board, identifier)
    atomic_write(safe_path(board, f"{STATE}/pending.json"), json_bytes({"transaction": identifier}))
    write_snapshot(board, record["before"])
    save_receipt(board, record.get("receipt_before"))
    record["status"] = "rolled_back"
    atomic_write(path, json_bytes(record))
    safe_path(board, f"{STATE}/pending.json").unlink()


def install(board: Path, project: dict, bundle: Bundle, expected: str | None) -> str | None:
    if expected and expected != bundle.version:
        raise UpdateError("доступная версия отличается от --expect; сначала просмотри новый план")
    names = set(bundle.files) | set((receipt(board) or {}).get("files", {}))
    with mutation_lock(board, project, names, bundle.machine_lock):
        if pending(board):
            raise UpdateError(f"сначала восстанови незавершённое обновление: --rollback {pending(board)}")
        previous, before, after, conflicts = plan(board, bundle)
        if conflicts:
            raise UpdateError("; ".join(conflicts))
        if previous and previous["version"] == bundle.version and all(stamp(before[k]) == stamp(after[k]) for k in before):
            return None
        identifier = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
        record = {"schema": 1, "id": identifier, "board": str(board), "kit": bundle.name,
                  "status": "prepared", "source_commit": bundle.commit, "version": bundle.version,
                  "before": before, "after": after, "receipt_before": previous,
                  "machine_lock": bundle.machine_lock, "legacy": bundle.legacy}
        history = safe_path(board, f"{STATE}/history/{identifier}.json")
        if len(json_bytes(record)) > MAX_JOURNAL:
            raise UpdateError("резервная копия превышает допустимый размер")
        atomic_write(history, json_bytes(record))
        atomic_write(safe_path(board, f"{STATE}/pending.json"), json_bytes({"transaction": identifier}))
        try:
            # Recheck after the backup and immediately before writing any script.
            if any(stamp(read_file(safe_path(board, k))) != stamp(v) for k, v in before.items()):
                raise UpdateError("файлы доски изменились во время подготовки")
            write_snapshot(board, after)
            save_receipt(board, {"schema": 1, "board": str(board), "kit": bundle.name,
                                "transaction": identifier, "source_commit": bundle.commit,
                                "version": bundle.version,
                                "files": {k: stamp(v) for k, v in after.items() if v is not None}})
            record["status"] = "applied"
            atomic_write(history, json_bytes(record))
            safe_path(board, f"{STATE}/pending.json").unlink()
        except BaseException as exc:
            try:
                restore(board, identifier)
            except (OSError, UpdateError) as recovery:
                raise UpdateError(f"обновление прервано ({exc}); нужен --rollback {identifier}: {recovery}") from exc
            raise UpdateError(f"обновление отменено, прежние файлы восстановлены: {exc}") from exc
        return identifier


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Показать, установить или вернуть версию скриптов доски")
    parser.add_argument("name")
    parser.add_argument("--root", type=Path, default=pc.DEFAULT_ROOT)
    parser.add_argument("--source", type=Path, default=pc.FORK_DIR, help="репозиторий проверенного комплекта (HEAD)")
    parser.add_argument("--kit", help="имя комплекта; по умолчанию имя доски")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true")
    action.add_argument("--rollback", metavar="ID")
    parser.add_argument("--dry-run", action="store_true", help="только просмотр; для возврата тоже")
    parser.add_argument("--diff", action="store_true")
    parser.add_argument("--expect", help="полная версия из предварительного просмотра")
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error("--apply и --dry-run несовместимы")
    try:
        board, project = read_board(args.root, args.name)
        if args.rollback:
            _, record = check_restore(board, args.rollback)
            print(f"Возврат {board.name}: {args.rollback}")
            describe(record["after"], record["before"], args.diff)
            if args.dry_run:
                print("Только просмотр; файлы не менялись.")
                return 0
            with mutation_lock(board, project, set(record["before"]), record.get("machine_lock", False)):
                restore(board, args.rollback)
            print("Прежние файлы и запись версии восстановлены. Служба не запускалась.")
            return 0
        bundle = load_bundle(args.source.resolve(), args.kit or args.name)
        previous, before, after, conflicts = plan(board, bundle)
        print(f"Доска: {board}\nКомплект: {bundle.name}; коммит источника: {bundle.commit}")
        print(f"Установлено: {previous['version'] if previous else 'ещё не зарегистрировано'}\nДоступно: {bundle.version}")
        if previous:
            print(f"Резервная копия для возврата: {previous['transaction']}")
        if pending(board):
            raise UpdateError(f"незавершённое обновление: сначала --rollback {pending(board)}")
        if conflicts:
            raise UpdateError("; ".join(conflicts))
        changed = describe(before, after, args.diff)
        if not changed:
            print("Содержимое и права файлов совпадают." if not previous else "Скрипты актуальны.")
        if args.apply:
            identifier = install(board, project, bundle, args.expect)
            print(f"Установлено; резервная копия {identifier}." if identifier else "Повторная установка не требуется.")
            print("Служба не запускалась. Готовность проекта проверяется отдельно через project-doctor.")
        else:
            print("Только просмотр; файлы не менялись. Установка: --apply --expect " + bundle.version)
        return 0
    except (UpdateError, pc.ProjectError, OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        print(f"project-update: отказ: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
