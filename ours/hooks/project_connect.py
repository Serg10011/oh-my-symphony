"""Host side of project-chat onboarding. Only an exact, separately authorized request can provision a board."""
from __future__ import annotations

import contextlib
import ctypes
import errno
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request

import api_board_config as board_config
import api_board_models as models
import api_cloud_check as cloud
import api_project
import api_session
import project_config as pc
from symphony import service


class ConnectError(Exception):
    """A public error code. Commands, credentials and raw provider output stay on the host."""


GUIDE = "https://github.com/Serg10011/oh-my-symphony/blob/our-main/docs/our/PROJECT-ARCHITECT.md"
SCHEMA = {"project", "repo", "base", "team", "mode", "paths", "verification"}
MAX_REQUEST = 32 * 1024
REASONS = {
    "invalid_json": "Пришли корректный JSON без повторяющихся полей.",
    "invalid_request": "Запрос подключения должен быть объектом JSON.",
    "request_too_large": "Сократи описание подключения до 32 КиБ.",
    "invalid_project": "Укажи короткое имя проекта из латинских строчных букв, цифр и дефисов.",
    "invalid_branch": "Укажи имя существующей локальной ветки проекта.",
    "local_client_required": "Отправь запрос локальным клиентом конвейера из чата проекта.",
    "local_configuration_error": "Оператору нужно проверить локальные файлы и службу подключения.",
    "unsafe_board_root": "Проверь владельца и права папки досок на машине конвейера.",
    "credential_unavailable": "Оператору нужно проверить локальный доступ этого проекта.",
    "unauthorized": "Нужно разрешение хозяина конвейера для этого проекта.",
    "different_from_approval": "Команда, файлы или другие параметры отличаются от согласованных.",
    "approval_changed": "Проверь текущую сводку и подтверждение владельца.",
    "project_already_authorized_differently": "Для проекта уже согласованы другие параметры.",
    "repository_changed": "Каталог проекта заменён после согласования.",
    "invalid_repository": "Укажи канонический путь к отдельному корню Git-репозитория.",
    "repository_or_branch_unavailable": "Проверь репозиторий и наличие проектной ветки на этом компьютере.",
    "repository_already_bound": "У репозитория уже есть доска. Используй её согласованное имя.",
    "project_files_unavailable": "В области доступа нужен хотя бы один доступный файл на выбранной ветке.",
    "specific_paths_required": "Выбери конкретные пути проекта вместо всего репозитория.",
    "unsupported_public_destination": "Этот сценарий GitHub поддерживает только публичную OMS и ветку our-main.",
    "invalid_fields": "Проверь поля запроса по инструкции подключения.",
    "unsupported_mode_or_checks": "Выбери поддерживаемый режим и способ проверки.",
    "invalid_team_scope_or_budget": "Проверь совместимость моделей, допустимые пути и параметры бюджета.",
    "board_configuration_differs": "Существующая доска отличается от согласованного профиля.",
    "board_conflict": "Имя доски или её файлы уже заняты другой конфигурацией.",
    "team_changed": "Действующая пара моделей отличается от согласованной.",
    "offline_board_requires_operator_start": "Существующая доска выключена. Её запуск решает оператор.",
    "nonempty_board_requires_operator_start": "На выключенной доске есть задачи. Перед запуском их проверяет оператор.",
    "board_identity_changed": "Не подтверждена принадлежность процесса именно этой доске.",
    "board_not_reachable": "Доска не подтвердила готовность. Проверь состояние службы и повтори запрос.",
    "board_start_failed": "Проверка или запуск доски завершились ошибкой. Проверь конфигурацию на машине конвейера.",
    "stale_service_record": "Проверь старую регистрацию процесса доски на машине конвейера.",
    "connector_unavailable": "Служба подключения недоступна на указанном локальном порту.",
    "unverified_connector_reply": "Служба не подтвердила подлинность ответа для этого проекта.",
}


def refusal(reason):
    return {"status": "refused", "reason": reason, "message": REASONS.get(reason, "Уточни условие подключения у оператора.")}


def decode(data):
    if len(data) > MAX_REQUEST:
        raise ConnectError("request_too_large")
    try:
        value = api_session._decode(data)
    except (ValueError, api_session.SessionError, UnicodeError) as exc:
        raise ConnectError("invalid_json") from exc
    if not isinstance(value, dict):
        raise ConnectError("invalid_request")
    return value


def project_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", value):
        raise ConnectError("invalid_project")
    return value


def settings(request):
    python = Path(sys.executable).resolve()
    value = {"version": 1, "paths": request["paths"], "route": "single-api", "mode": request["mode"],
             "session": dict(request["team"]),
             "execution": {"python": str(python), "read_roots": [str(Path(sys.base_prefix).resolve())]}}
    if "budget" in request:
        value["session"]["budget"] = request["budget"]
    if request["verification"] == cloud.PROFILE:
        value["cloud_check"] = cloud.PROFILE
    return value


def normalize(raw):
    if set(raw) - {"budget"} != SCHEMA:
        raise ConnectError("invalid_fields")
    project_name(raw["project"])
    if raw["mode"] not in ("economy", "productivity") or raw["verification"] not in ("manual", cloud.PROFILE):
        raise ConnectError("unsupported_mode_or_checks")
    if not isinstance(raw["base"], str) or not raw["base"] or raw["base"].startswith("-") or len(raw["base"]) > 255:
        raise ConnectError("invalid_branch")
    if not isinstance(raw["repo"], str) or len(raw["repo"]) > 4096 or not Path(raw["repo"]).is_absolute():
        raise ConnectError("invalid_repository")
    try:
        request = {**raw, "team": models.pair(raw["team"])}
        raw_settings = settings(request)
        spec = api_session.validate_spec({**board_config.session_options(raw_settings, {}), "task": "Connect board",
            "project": {"root": request["repo"], "paths": request["paths"], "commit": "0" * 40},
            "execution": raw_settings["execution"]})
    except (api_session.SessionError, api_project.ProjectError, TypeError, ValueError) as exc:
        raise ConnectError("invalid_team_scope_or_budget") from exc
    request["repo"] = spec["project"]["root"]
    request["paths"] = spec["project"]["paths"]
    if "." in request["paths"]:
        raise ConnectError("specific_paths_required")
    if "budget" in request:
        if not spec.get("budget"):
            raise ConnectError("invalid_team_scope_or_budget")
        request["budget"] = spec["budget"]
    return request


def fingerprint(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def git(repo, *args):
    cmd = ["git", "--no-pager", "--no-replace-objects", "--no-optional-locks",
           "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args]
    env = {"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_ALLOW_PROTOCOL": "", "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run(cmd, env=env, capture_output=True, timeout=10)
    if result.returncode:
        raise ConnectError("repository_or_branch_unavailable")
    return result.stdout.decode().strip()


def inspect_repo(request, root):
    repo = Path(request["repo"])
    if (repo.resolve() != repo or repo.is_relative_to(root) or root.is_relative_to(repo)
            or Path(git(repo, "rev-parse", "--show-toplevel")).resolve() != repo):
        raise ConnectError("invalid_repository")
    ref = "refs/heads/" + request["base"]
    git(repo, "check-ref-format", ref)
    commit = git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}")
    try:
        api_project.snapshot({"root": str(repo), "paths": request["paths"], "commit": commit})
    except api_project.ProjectError as exc:
        raise ConnectError("project_files_unavailable") from exc
    if request["verification"] == cloud.PROFILE:
        public = f"https://github.com/{cloud.REPO}"
        if request["base"] != "our-main" or git(repo, "config", "--local", "--no-includes", "--get", "remote.origin.url") not in (public, public + ".git"):
            raise ConnectError("unsupported_public_destination")
    info = repo.stat()
    return {"device": info.st_dev, "inode": info.st_ino}


def private_dir(path):
    path.mkdir(mode=0o700, exist_ok=True)
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise ConnectError("unsafe_host_directory")


def read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077 or st.st_size > 128 * 1024:
            raise ConnectError("unsafe_private_file")
        return stream.read()


@contextlib.contextmanager
def serial(store):
    fd = os.open(store / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def host_root(path):
    root = path.expanduser().resolve(strict=True)
    if not root.is_dir() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o022:
        raise ConnectError("unsafe_board_root")
    if any((p / ".git").exists() for p in (root, *root.parents)):
        raise ConnectError("board_root_inside_git")
    return root


def review(raw, root):
    request = normalize(raw)
    identity = inspect_repo(request, root)
    approved = {"version": 1, "request": request, "repository_identity": identity}
    return {**approved, "approval_id": fingerprint(approved)}


def authorize(raw, root, expected):
    reviewed = review(raw, root)
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or not hmac.compare_digest(reviewed["approval_id"], expected):
        raise ConnectError("approval_changed")
    store = root / ".connections"
    private_dir(store)
    name = reviewed["request"]["project"]
    grant = store / (name + ".json")
    credential = store / (name + ".token")
    with serial(store):
        if grant.exists() or grant.is_symlink():
            previous = json.loads(read_private(grant))
            if previous["approval_id"] != expected:
                raise ConnectError("project_already_authorized_differently")
        if not credential.exists() and not credential.is_symlink():
            if grant.exists():
                raise ConnectError("credential_unavailable")
            api_session._save(credential, {"token": secrets.token_urlsafe(32)})
        token = json.loads(read_private(credential))["token"]
        record = {**reviewed, "token_hash": hashlib.sha256(token.encode()).hexdigest()}
        if grant.exists() and previous != record:
            raise ConnectError("credential_changed")
        api_session._save(grant, record)
    return {"status": "authorized", "project": name, "approval_id": expected, "credential_file": str(credential)}


def signature(token, direction, challenge, body):
    message = b"Symphony project-connect v1\0" + direction.encode() + b"\0" + challenge.encode() + b"\0" + body
    return hmac.new(token.encode(), message, hashlib.sha256).hexdigest()


def authenticate(raw, root, challenge, proof, body):
    if (not re.fullmatch(r"[0-9]{10}-[0-9a-f]{32}", challenge) or abs(time.time() - int(challenge[:10])) > 120
            or not re.fullmatch(r"[0-9a-f]{64}", proof)):
        raise ConnectError("unauthorized")
    try:
        name = project_name(raw.get("project"))
        grant = json.loads(read_private(root / ".connections" / (name + ".json")))
        token = json.loads(read_private(root / ".connections" / (name + ".token")))["token"]
    except (FileNotFoundError, ConnectError) as exc:
        raise ConnectError("unauthorized") from exc
    if (not hmac.compare_digest(grant["token_hash"], hashlib.sha256(token.encode()).hexdigest())
            or not hmac.compare_digest(signature(token, "request", challenge, body), proof)):
        raise ConnectError("unauthorized")
    return grant, token


def choose_address(root, name, repo):
    prefixes, ports = set(), set()
    for other in root.iterdir():
        if other.name.startswith(".") or not (other / pc.PROJECT_FILE).exists():
            continue
        if other.is_symlink() or (other / pc.PROJECT_FILE).is_symlink():
            raise ConnectError("unsafe_board_root")
        config = pc.load_project(other)["project"]
        if Path(config["repo"]).resolve() == repo:
            raise ConnectError("repository_already_bound")
        prefixes.add(config["id_prefix"])
        ports.add(config["port"])
    stem = name.replace("-", "").upper()[:10]
    prefix = stem if len(stem) >= 2 else stem + "1"
    for number in range(2, 1000):
        if prefix not in prefixes:
            break
        suffix = str(number)
        prefix = stem[:10 - len(suffix)] + suffix
    else:
        raise ConnectError("no_free_prefix")
    for port in range(10020, 11020):
        if port in ports:
            continue
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
        return prefix, port
    raise ConnectError("no_free_port")


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_directory(stage, board):
    # POSIX rename may replace another operator's empty directory. Both supported
    # host platforms provide a single atomic, create-only rename on the same volume.
    libc = ctypes.CDLL(None, use_errno=True)
    src, dest = os.fsencode(stage), os.fsencode(board)
    if sys.platform == "darwin":
        rc = libc.renamex_np(src, dest, 4)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        rc = libc.renameat2(-100, src, -100, dest, 1)  # AT_FDCWD, RENAME_NOREPLACE
    else:
        raise ConnectError("atomic_publish_unavailable")
    if rc != 0:
        if ctypes.get_errno() in (errno.EEXIST, errno.ENOTEMPTY):
            raise ConnectError("board_conflict")
        raise ConnectError("board_publish_failed")
    sync_directory(board.parent)


def create_board(root, board, grant):
    request = grant["request"]
    prefix, port = choose_address(root, board.name, Path(request["repo"]))
    python = Path(sys.executable).absolute()
    # TOML basic strings share JSON's escaping for our validated input.
    def quoted(value):
        return json.dumps(str(value), ensure_ascii=False)
    config = (f"[project]\nname = {quoted(board.name)}\nrepo = {quoted(request['repo'])}\n"
              f"base_branch = {quoted(request['base'])}\nid_prefix = {quoted(prefix)}\nport = {port}\n"
              f"\n[tools]\npython = {quoted(python)}\nsymphony = {quoted(python.with_name('symphony'))}\n"
              '\n[api_team]\nsettings_file = "api-settings.json"\ndefault = true\n')
    if request["verification"] == cloud.PROFILE:
        config += f'\n[delivery]\nrepo = "{cloud.REPO}"\nbase = "our-main"\n'
    with tempfile.TemporaryDirectory(prefix=".connect-", dir=root) as tmp:
        stage = Path(tmp) / board.name
        stage.mkdir(mode=0o700)
        for name in (pc.BOARD_DIR, pc.ARTIFACTS_DIR, pc.WIKI_DIR, pc.STATE_DIR):
            (stage / name).mkdir(mode=0o700)
        def write_config(name, content):
            with (stage / name).open("x", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        write_config(pc.PROJECT_FILE, config)
        api_session._save(stage / "api-settings.json", settings(request))
        cfg = pc.load_project(stage)
        write_config("WORKFLOW.md", pc.render_workflow(board, cfg, settings_dir=stage))
        api_session._save(stage / pc.STATE_DIR / "connection.json", {"approval_id": grant["approval_id"]})
        sync_directory(stage)
        if board.exists() or board.is_symlink():
            raise ConnectError("board_conflict")
        publish_directory(stage, board)


def ensure_profile(root, board, grant):
    request = grant["request"]
    if board.is_symlink() or board.resolve() != board:
        raise ConnectError("board_conflict")
    if not board.exists():
        create_board(root, board, grant)
    if any((board / p).is_symlink() for p in (pc.STATE_DIR, pc.BOARD_DIR, pc.PROJECT_FILE, "WORKFLOW.md")):
        raise ConnectError("board_conflict")
    if not (board / pc.PROJECT_FILE).is_file():
        raise ConnectError("board_conflict")
    cfg = pc.load_project(board)
    identity = cfg["project"]
    opts = cfg["api_team"]
    if (identity["name"] != board.name or identity["repo"] != request["repo"] or identity["base_branch"] != request["base"]
            or not opts or not (opts["default"] or opts.get("new_cards"))):
        raise ConnectError("board_configuration_differs")
    expected_settings = settings(request)
    if not opts["default"]:
        expected_settings.pop("route")
    if board_config.load_settings(board / opts["settings_file"]) != expected_settings:
        raise ConnectError("board_configuration_differs")
    if request["verification"] == cloud.PROFILE and cfg["delivery"] != {"repo": cloud.REPO, "base": "our-main"}:
        raise ConnectError("board_configuration_differs")
    marker = board / pc.STATE_DIR / "connection.json"
    managed = marker.exists()
    if managed and json.loads(read_private(marker)) != {"approval_id": grant["approval_id"]}:
        raise ConnectError("board_conflict")
    if (board / "WORKFLOW.md").read_text() != pc.render_workflow(board, cfg):
        raise ConnectError("board_configuration_differs")
    teams = models.Teams(board / opts["settings_file"], {"workflow": str(board / "WORKFLOW.md"), "agent_kind": "api-team"})
    if teams.default() != request["team"]:
        raise ConnectError("team_changed")
    return cfg, managed


def ensure_live(board, port, can_start):
    workflow = board / "WORKFLOW.md"
    host = "127.0.0.1"
    if service.is_symphony_workflow_reachable(host, port, workflow):
        return
    if not can_start:
        raise ConnectError("offline_board_requires_operator_start")
    # A reconnect may find an offline board with real work. Starting it can
    # resume paid/legacy work, so only an empty board may be started here.
    if any((board / pc.BOARD_DIR).glob("*.md")) or (board / pc.STATE_DIR / "api-team").exists():
        raise ConnectError("nonempty_board_requires_operator_start")
    current = service.load_record(workflow)
    if current is not None:
        # Never stop or replace a recorded process. The operator can resolve
        # a stale registration. A live one may still be starting after a retry.
        if current.port != port or not service.is_process_running(current.orchestrator_pid):
            raise ConnectError("stale_service_record")
    else:
        executable = Path(sys.executable).with_name("symphony")
        args = [str(executable), "service", "start", str(workflow), "--host", host, "--port", str(port)]
        env = {"PATH": os.defpath, "HOME": str(Path.home()), "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
        run = subprocess.run(args, cwd=board, env=env, capture_output=True, timeout=60)
        if run.returncode:
            raise ConnectError("board_start_failed")
    for _ in range(60):
        if service.is_symphony_workflow_reachable(host, port, workflow):
            return
        time.sleep(0.2)
    raise ConnectError("board_not_reachable")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def verify_instance(workflow, port, managed):
    record = service.load_record(workflow)
    if record is None:
        if managed or not service.is_symphony_workflow_reachable("127.0.0.1", port, workflow):
            raise ConnectError("board_identity_changed")
        return
    if record.port != port or record.host != "127.0.0.1" or not record.service_instance_id:
        raise ConnectError("board_identity_changed")
    live = service.probe_service_endpoint_identity(record.host, port, workflow, record.service_instance_id)
    if not live or live.orchestrator_pid != record.orchestrator_pid:
        raise ConnectError("board_identity_changed")


def verify_live(board, cfg, request, managed):
    project = cfg["project"]
    port = project["port"]
    workflow = board / "WORKFLOW.md"
    verify_instance(workflow, port, managed)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    def get(path):
        with opener.open(f"http://127.0.0.1:{port}/api/v1/{path}", timeout=5) as reply:
            body = reply.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            raise ConnectError("board_not_reachable")
        return json.loads(body)
    live = get("board")
    directory = get("api-teams/api-team")
    meta = live["board"]
    opts = cfg["api_team"]
    actual_settings = board_config.load_settings(board / opts["settings_file"])
    binding = models.project_binding({"workflow": str(workflow), "board_root": str(board / pc.BOARD_DIR),
                                      "repo": request["repo"], "base_branch": request["base"]})
    if (meta["name"] != board.name or meta["tracker_kind"] != "file" or meta["id_prefix"] != project["id_prefix"]
            or meta["new_issue_kind"] != "api-team" or "api-team" not in meta["api_team_model_kinds"]
            or opts["default"] and meta["default_agent_kind"] != "api-team"
            or actual_settings.get("route") == "single-api" and meta["agent_kinds"] != ["api-team"]
            or directory.get("project_binding") != binding
            or directory["default"] != request["team"] or directory["creation_defaults"]["mode"] != request["mode"]
            or directory["creation_defaults"]["policy"] != models.policy_digest(actual_settings)):
        raise ConnectError("board_configuration_differs")
    verify_instance(workflow, port, managed)
    return [column["name"] for column in live["columns"]]


def connect(raw, root, grant):
    request = normalize(raw)
    if grant["request"] != request:
        raise ConnectError("different_from_approval")
    board = root / request["project"]
    with serial(root / ".connections"):
        if json.loads(read_private(root / ".connections" / (board.name + ".json"))) != grant:
            raise ConnectError("approval_changed")
        if grant["repository_identity"] != inspect_repo(request, root):
            raise ConnectError("repository_changed")
        cfg, managed = ensure_profile(root, board, grant)
        identity = cfg["project"]
        receipt = root / ".connections" / (board.name + ".ready")
        if receipt.exists() and json.loads(read_private(receipt)).get("approval_id") != grant["approval_id"]:
            raise ConnectError("board_conflict")
        ensure_live(board, identity["port"], managed and not receipt.exists())
        stages = verify_live(board, cfg, request, managed)
        effective = board_config.session_options(settings(request), request["team"])
        result = {"status": "ready", "project": board.name, "board_url": f"http://127.0.0.1:{identity['port']}/#/board",
                "guide_url": GUIDE, "repo": identity["repo"], "base": identity["base_branch"],
                "prefix": identity["id_prefix"], "team": request["team"], "mode": request["mode"],
                "lead_effort": effective["lead_effort"], "paths": request["paths"], "verification": request["verification"],
                "route": "single-api" if cfg["api_team"]["default"] else "mixed",
                "stages": stages, "budget": request.get("budget"),
                "payment": "explicit_per_card", "approval_id": grant["approval_id"]}
        if not receipt.exists():
            api_session._save(receipt, result)
        return result
