"""Opt-in whole-ticket API bridge; existing stage backends remain unchanged.

The configured command is trusted operator code. HTTP callers cannot change its
argv, settings path, project grants or action vocabulary. API/provider logic stays
in the fork's ours/ tools rather than becoming a second orchestrator.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import time
from typing import Any

from ._shell import process_identity, terminate_process_tree
from .errors import SymphonyError
from .logging import get_logger
from .workflow import AgentProfileConfig, ServiceConfig

FORMAT = "api-team-v1"
MAX_REPLY_BYTES = 8 * 1024 * 1024
# A negotiated run may prefix its JSON reply with ASCII record separators,
# one per durable journal checkpoint. No model text travels in these frames.
CHECKPOINT_ENV = "SYMPHONY_API_TEAM_CHECKPOINTS"
CHECKPOINT = b"\x1e"
PUBLIC_ERRORS = {
    "api_assignment_changed": "Задача или настройки изменились. Посмотри назначение заново перед подтверждением",
    "api_confirmation_expired": "Время подтверждения истекло. Посмотри назначение заново",
    "invalid_api_confirmation": "Подтверждение недействительно. Посмотри назначение заново",
    "api_assignment_already_used": "Это назначение уже использовано. Обнови панель, чтобы увидеть результат",
    "api_result_not_ready": "Изменения будут доступны после завершения или остановки команды",
    "api_card_must_wait_for_confirmation": "Перед подготовкой назначения переведи карточку в needs_human",
    "api_keychain_unavailable": "Не удалось получить ключ из Связки ключей",
    "session_busy": "Команда обновляет журнал. Повтори чтение через несколько секунд",
    "cloud_invalid_run": "Введи номер запуска GitHub Actions цифрами",
    "cloud_not_enabled": "Для этого назначения чтение публичных проверок не включено",
    "cloud_stale": "Карточка, файлы, база или настройки изменились. Сверить этот результат уже нельзя",
    "cloud_other_candidate": "В этом запуске проверяли другую версию файлов",
    "cloud_unavailable": "GitHub сейчас не отдал данные запуска. Проверь номер или повтори чтение позже",
    "cloud_untrusted": "Не удалось подтвердить репозиторий, сценарий или шаги проверки",
    "cloud_incomplete": "У прогона пока нет завершённого результата с подтверждённой изоляцией и уборкой",
    "cloud_conflict": "Данные этой попытки расходятся с записью в истории карточки",
}


def api_profile(cfg: ServiceConfig, kind: str | None) -> AgentProfileConfig | None:
    profile = cfg.agent_profile(kind or "")
    return profile if profile is not None and profile.format == FORMAT else None


def board_context(cfg: ServiceConfig, kind: str, identifier: str) -> dict[str, Any]:
    profile = api_profile(cfg, kind)
    if profile is None or cfg.tracker.kind != "file" or cfg.tracker.board_root is None:
        raise SymphonyError("API-команда не настроена для этой карточки")
    if "needs_human" not in cfg.tracker.terminal_states:
        raise SymphonyError("Для API-команды нужен статус needs_human")
    return {"workflow": str(cfg.workflow_path.resolve()), "board_root": str(cfg.tracker.board_root.resolve()),
            "repo": str((cfg.code_repo or cfg.workflow_path.parent).resolve()),
            "identifier": identifier, "agent_kind": kind, "command": profile.command,
            "active_states": list(cfg.tracker.active_states), "terminal_states": list(cfg.tracker.terminal_states),
            "base_branch": cfg.agent.feature_base_branch,
            "process_timeout_seconds": profile.turn_timeout_ms / 1000}


async def invoke(cfg: ServiceConfig, kind: str, identifier: str, action: str, *,
                 payload: dict | None = None, run_id: str | None = None, heartbeat=None,
                 on_checkpoint: Callable[[], None] | None = None) -> dict:
    if action not in {"prepare", "confirm", "show", "stop", "review", "attach-check", "sync-checks", "run"}:
        raise SymphonyError("Неизвестное действие API-команды")
    profile = api_profile(cfg, kind)
    context = board_context(cfg, kind, identifier)
    assert profile is not None
    argv = shlex.split(profile.command)
    if not argv or not Path(argv[0]).is_absolute():
        raise SymphonyError("Команда API-команды должна начинаться с полного пути")
    message = json.dumps({"context": context, "payload": payload or {}, "run_id": run_id},
                         ensure_ascii=False).encode()
    # No provider credentials, Python/shell startup settings or proxies leak to
    # the subprocess. The trusted runner obtains keys only for an approved run.
    env = {"PATH": os.defpath, "HOME": str(Path.home()), "LANG": "en_US.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
    checkpoint = on_checkpoint if action == "run" else None
    if checkpoint is not None:
        env[CHECKPOINT_ENV] = "1"
    proc = await asyncio.create_subprocess_exec(*argv, "--action", action,
        cwd=cfg.workflow_path.parent, env=env, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True)
    identity = await asyncio.to_thread(process_identity, proc.pid)
    stdin, stdout = proc.stdin, proc.stdout
    assert stdin is not None and stdout is not None
    async def exchange():
        stdin.write(message)
        await stdin.drain()
        stdin.close()
        chunks = bytearray()
        received = 0
        while block := await stdout.read(min(65536, MAX_REPLY_BYTES + 1 - received)):
            received += len(block)
            if received > MAX_REPLY_BYTES:
                raise SymphonyError("Ответ API-команды превышает допустимый размер")
            if checkpoint is not None and not chunks and block.startswith(CHECKPOINT):
                checkpoint()
                block = block.lstrip(CHECKPOINT)
            chunks.extend(block)
        await proc.wait()
        try:
            reply = json.loads(chunks)
        except (ValueError, UnicodeError):
            raise SymphonyError("API-команда не вернула читаемый результат") from None
        if not isinstance(reply, dict) or proc.returncode != 0 or "error" in reply:
            code = reply.get("error", "api_bridge_failed") if isinstance(reply, dict) else "api_bridge_failed"
            # Public runner errors are fixed machine codes, not stderr.
            if not isinstance(code, str) or not code.replace("_", "").isalnum() or len(code) > 100:
                code = "api_bridge_failed"
            raise SymphonyError(PUBLIC_ERRORS.get(code, f"Действие API-команды отклонено (код: {code})"), reason=code)
        return reply
    task = asyncio.create_task(exchange())
    timeout = profile.turn_timeout_ms / 1000 if action == "run" else 60
    if action == "sync-checks":
        timeout = 120
    try:
        async with asyncio.timeout(timeout):
            while not task.done():
                await asyncio.wait({task}, timeout=1)
                if heartbeat is not None:
                    await heartbeat()
            return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if proc.returncode is None:
            await asyncio.shield(terminate_process_tree(proc, identity=identity, term_timeout=5))


def _cloud_enabled(command: str) -> bool:
    # Only an explicitly opted-in runner may be started by the service.
    # Legacy API profiles (including wrappers without --settings) stay idle.
    try:
        args = shlex.split(command)
        settings = [args[i + 1] if arg == "--settings" else arg.split("=", 1)[1]
                    for i, arg in enumerate(args) if arg == "--settings" or arg.startswith("--settings=")]
        if len(settings) != 1:
            return False
        path = Path(settings[0])
        if not path.is_absolute() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            return False
        with path.open("rb") as stream:
            config = json.loads(stream.read(1024 * 1024 + 1))
        return isinstance(config, dict) and config.get("cloud_check") == "oms-public-linux-v1"
    except (OSError, ValueError, IndexError, RecursionError):
        return False


def schedule_cloud_checks(orch, cfg: ServiceConfig) -> None:
    """The service owns this read-only work, even with the board closed."""
    now = time.monotonic()
    if cfg.tracker.kind != "file" or now < orch._next_cloud_check:
        return
    if orch._cloud_check_task is not None and not orch._cloud_check_task.done():
        return
    orch._next_cloud_check = now + 60
    kinds = [kind for kind, p in cfg.agent.profiles.items() if p.format == FORMAT]
    if not kinds or not (cfg.workflow_path.parent / ".ours-state/api-team").is_dir():
        return

    async def sync():
        for kind in kinds:
            if not await asyncio.to_thread(_cloud_enabled, cfg.agent.profiles[kind].command):
                continue
            try:
                await invoke(cfg, kind, "", "sync-checks")
            except (OSError, SymphonyError, TimeoutError) as exc:
                get_logger().warning("api_team_cloud_sync_unavailable", profile=kind, error_type=type(exc).__name__)

    orch._cloud_check_task = orch._spawn_supervised(sync(), name="api-team-cloud-checks")


class ApiTeamExecutor:
    mode = FORMAT

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator

    async def execute(self, context) -> None:
        orch = self.orchestrator
        owning_task = asyncio.current_task()
        stopped = False
        reason, error = "api_team_finished", None

        def checkpoint():
            entry = orch._dispatch_state.entry_owned_by(context.issue.id, owning_task)
            if entry is not None:
                entry.last_progress_timestamp = datetime.now(timezone.utc)

        async def heartbeat():
            nonlocal stopped
            entry = orch._dispatch_state.entry_owned_by(context.issue.id, owning_task)
            if entry is None:
                raise asyncio.CancelledError()
            # Liveness is useful for the UI and pause polling. Only a journal
            # checkpoint above moves the stall watchdog's separate clock.
            entry.last_codex_timestamp = datetime.now(timezone.utc)
            if orch.is_paused(context.issue.id) and not stopped:
                await invoke(context.cfg, context.agent_kind, context.issue.identifier, "stop")
                stopped = True
        try:
            result = await invoke(context.cfg, context.agent_kind, context.issue.identifier, "run",
                                  run_id=context.run_id, heartbeat=heartbeat, on_checkpoint=checkpoint)
            entry = orch._dispatch_state.entry_owned_by(context.issue.id, owning_task)
            if entry is not None:
                orch._append_run_event(entry, "api_team_finished", {
                    "status": result.get("status"), "accepted": False, "delivered": False})
        except asyncio.CancelledError:
            reason = "api_team_interrupted"
            raise
        except Exception:
            reason, error = "api_team_failed", "API-команда остановлена; требуется проверка панели карточки"
        finally:
            # Paid sessions never inherit the legacy automatic stage retry.
            await asyncio.shield(orch._on_worker_exit(context.issue.id, reason, error,
                                                      owning_task=owning_task))
