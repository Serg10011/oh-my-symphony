"""Opt-in whole-ticket API bridge; existing stage backends remain unchanged.

The configured command is trusted operator code. HTTP callers cannot change its
argv, settings path, project grants or action vocabulary. API/provider logic stays
in the fork's ours/ tools rather than becoming a second orchestrator.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import stat
import time
from typing import Any, TypeGuard

from ._shell import process_identity, terminate_process_tree
from .errors import SymphonyError
from .issue import Issue
from .logging import get_logger
from .workflow import AgentProfileConfig, ServiceConfig

FORMAT = "api-team-v1"
FINISHED_HOLD = "API-запуск завершён; автоматического повтора нет"
VERIFY_STATES = {
    "api_verify_waiting": ("Ожидает проверки", "Подтверждённого запуска проверки этой версии пока нет."),
    "api_verify_queued": ("Проверка в очереди", "GitHub поставил проверку этой версии в очередь."),
    "api_verify_running": ("Проверяется", "GitHub выполняет проверку этой версии."),
    "api_verify_result": ("Ожидает результата", "Конвейер сверяет результат проверки этой версии."),
    "api_verify_pending": ("Ожидает проверки", "Предыдущая проверка не подтверждена; доска ждёт другую попытку."),
}
MAX_REPLY_BYTES = 8 * 1024 * 1024
# A negotiated run may prefix its JSON reply with ASCII record separators,
# one per durable journal checkpoint. No model text travels in these frames.
CHECKPOINT_ENV = "SYMPHONY_API_TEAM_CHECKPOINTS"
CHECKPOINT = b"\x1e"
PUBLIC_ERRORS = {
    "api_board_settings_busy": "Дождись завершения переданных API-задач и сохрани настройки снова.",
    "api_board_settings_changed": "Условия доски изменились. Проверь изменения ещё раз.",
    "invalid_api_board_mode": "Выбери режим работы доски.",
    "invalid_api_work_settings": "Не удалось прочитать сохранённые условия работы доски.",
    "invalid_project_spec": "Укажи от 1 до 32 путей внутри репозитория.",
    "invalid_project_path": "Укажи пути от корня репозитория. Запись с «..» и абсолютные пути недоступны.",
    "project_scope_denied": "Один из путей закрыт правилами доступа.",
    "no_project_files": "В указанных путях нет доступных файлов базовой ветки.",
    "api_board_path_missing": "Один из путей не содержит доступных файлов в базовой ветке.",
    "invalid_project_context": "Проверь список файлов общего контекста в настройках API-доски.",
    "project_context_file_unavailable": "Файл общего контекста отсутствует, закрыт или не читается как текст в версии проекта.",
    "project_context_too_large": "Сократи объём общего контекста до 128 КиБ.",
    "api_permission_required": "Для передачи задачи нужно действующее разрешение доски",
    "api_permission_changed": "Условия доски изменились. Владелец должен проверить и подтвердить их заново",
    "api_permission_revoked": "Работа API-команды остановлена: разрешение доски отозвано или изменилось",
    "api_permission_card_not_eligible": "Эта карточка создана до действующего разрешения. Для неё нужен отдельный запуск",
    "api_permission_task_changed": "Карточка или её назначение изменились. Прочитай актуальную версию перед передачей",
    "api_permission_confirmation_invalid": "Подтверждение разрешения недействительно. Сначала посмотри условия доски",
    "api_permission_incomplete": "Сохранение разрешения прервалось. Проверь условия доски заново",
    "api_permission_invalid": "Не удалось прочитать разрешение доски. Требуется проверка оператором",
    "api_permission_storage_unsafe": "Права папки разрешений не подходят для платной работы",
    "invalid_composition": "Такое сочетание ведущего и исполнителя не поддерживается",
    "api_assignment_changed": "Задача или настройки изменились. Посмотри назначение заново перед подтверждением",
    "api_confirmation_expired": "Время подтверждения истекло. Посмотри назначение заново",
    "api_plan_not_configured": "Для плана нужны колонки Plan и Subtasks и настройки проекта.",
    "api_plan_subtask_draft": "Это черновик подзадачи из принятого плана.",
    "api_plan_not_recoverable": "Готовый ответ ведущего для восстановления плана недоступен.",
    "api_plan_not_ready": "Для запуска подзадач нужен проверенный план.",
    "api_plan_parent_changed": "Исходный план, версия проекта или условия допуска изменились.",
    "api_plan_subtask_changed": "Карточка подзадачи изменилась после подготовки плана.",
    "api_subtask_retry_unavailable": "Штатный повтор доступен после отказа Flash до сдачи результата.",
    "api_subtasks_already_used": "Подзадачи этого плана уже получили допуск. Новый запуск потребует отдельного решения.",
    "api_subtasks_budget_exhausted": "Достигнут общий предел бюджета или входных токенов составной задачи.",
    "api_subtask_local_only": "Версия подзадачи хранится для сборки результата родительской карточки.",
    "invalid_api_confirmation": "Подтверждение недействительно. Посмотри назначение заново",
    "api_assignment_already_used": "Это назначение уже использовано. Обнови панель, чтобы увидеть результат",
    "api_result_not_ready": "Изменения будут доступны после завершения или остановки команды",
    "api_card_must_wait_for_confirmation": "Назначение можно подготовить только у ожидающей запуска карточки",
    "api_keychain_unavailable": "Не удалось получить ключ из Связки ключей",
    "gemini_quota_unavailable": "Не удалось прочитать квоту Gemini в Antigravity. Проверь вход и повтори обновление.",
    "session_busy": "Команда обновляет журнал. Повтори чтение через несколько секунд",
    "cloud_invalid_run": "Введи номер запуска GitHub Actions цифрами",
    "cloud_not_enabled": "Для этого назначения чтение публичных проверок не включено",
    "cloud_stale": "Карточка, файлы, база или настройки изменились. Сверить этот результат уже нельзя",
    "cloud_other_candidate": "В этом запуске проверяли другую версию файлов",
    "cloud_unavailable": "GitHub сейчас не отдал данные запуска. Проверь номер или повтори чтение позже",
    "cloud_untrusted": "Не удалось подтвердить репозиторий, сценарий или шаги проверки",
    "cloud_incomplete": "У прогона пока нет завершённого результата с подтверждённой изоляцией и уборкой",
    "cloud_conflict": "Данные этой попытки расходятся с записью в истории карточки",
    "release_offer_changed": "Версия или результат проверки изменились. Обнови карточку перед подтверждением",
    "release_base_not_synced": "База проекта и GitHub расходятся. Синхронизируй клон проекта и снова открой карточку",
    "release_destination_invalid": "Для этой доставки нужны публичная OMS, база our-main и обычная карточка проекта",
    "release_target_changed": "Файлы или настройки проверки изменились. Нужна сверка результата",
    "release_receipt_changed": "GitHub изменил результат попытки. Доска обновит статус проверки",
    "release_branch_occupied": "Ветка или запись доставки уже используется. Проверь её до публикации",
    "release_branch_changed": "Ветка на GitHub изменилась или была удалена. Нужна проверка человеком",
    "release_branch_unconfirmed": "GitHub не подтвердил создание ветки. Повтори действие для показанной версии",
    "release_pr_unconfirmed": "GitHub пока не подтвердил PR. Повтори доставку, чтобы сверить ветку и PR",
    "release_pr_changed": "PR относится к другой ветке, базе или версии. Требуется проверка человеком",
    "release_remote_unavailable": "Не удалось завершить доставку через GitHub. Повтори действие для этой версии",
    "release_check_recipe_changed": "Сценарий или инструменты Linux-проверки отличаются от проверенных",
    "release_gh_unavailable": "Для публикации нужен установленный GitHub CLI",
    "release_merge_conflict": "При объединении с новой базой возник конфликт. Требуется внимание человека",
    "release_base_diverged": "История базовой ветки разошлась с версией задачи. Требуется внимание человека",
    "release_paths_changed": "Объединение с новой базой затронуло защищённые файлы или вышло за состав задачи",
    "release_possible_secret": "В изменённых файлах найдена строка, похожая на ключ. Требуется проверка человеком",
    "release_already_in_base": "В текущей базе уже есть эти изменения. Проверь, можно ли закрыть задачу",
    "release_base_after_acceptance": "База сдвинулась после разрешения доставки. Проверь опубликованную версию",
}


def api_profile(cfg: ServiceConfig, kind: str | None) -> AgentProfileConfig | None:
    profile = cfg.agent_profile(kind or "")
    return profile if profile is not None and profile.format == FORMAT else None


def supports_team_settings(command: str) -> bool:
    """Old custom runners do not implement the board model directory."""
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    return (any(Path(arg).is_absolute() and Path(arg).name == "api_board.py" for arg in args)
            and any(arg == "--settings" or arg.startswith("--settings=") for arg in args))


def uses_board_queue(cfg: ServiceConfig, kind: str | None) -> bool:
    profile = api_profile(cfg, kind)
    return profile is not None and supports_team_settings(profile.command)


def needs_manual_start(cfg: ServiceConfig, issue: Issue) -> bool:
    if not cfg.tracker.manual_start or issue.start_requested:
        return False
    kind = cfg.agent.kind_for_state(issue.state, issue.agent_kind)
    return api_profile(cfg, kind) is None


async def defaults_for_new_card(cfg, kind, prefix, identifier):
    """Freeze operator defaults during creation through either the board API or the CLI."""
    if not cfg.agent.new_issue_kind or not uses_board_queue(cfg, kind):
        return None
    expected = cfg.tracker.id_prefix
    if (prefix and (not isinstance(prefix, str) or prefix.strip().upper() != expected)
            or identifier and (not isinstance(identifier, str) or not identifier.startswith(expected + "-"))):
        raise SymphonyError(f"Для API-карточки этой доски нужен префикс {expected}")
    listing = await invoke(cfg, kind, "", "team-options", payload={"identifiers": []})
    frozen = listing.get("creation_defaults")
    if not isinstance(frozen, dict):
        raise SymphonyError("Настройки проекта для новой карточки недоступны")
    return frozen


def normal_review_lane(states) -> str | None:
    return next((s for s in states if s.casefold() in {"human review", "приёмка", "приемка"}), None)


FALLBACK_REASON = "Причина не записана"

ATTENTION_REASONS: dict[str, str] = {
    "operator_stopped": "Остановлено оператором или после потери связи с доской.",
    "board_process_interrupted": "Служба прервала процесс команды. Подробности сохранены в журнале службы.",
    "board_execution_failed": "Процесс команды прерван. Автоматического платного повтора нет.",
    "api_assignment_changed": "Задача или настройки изменились после подтверждения; платный запуск отменён.",
    "api_permission_revoked": "Разрешение доски отозвано или изменилось. Новые обращения к моделям остановлены.",
    "execution_preflight_failed": "Проверка изоляции не прошла; модели не запускались.",
    "flash_cli_unavailable": "На Mac недоступен Antigravity CLI для Flash.",
    "flash_customizations_active": "Активные расширения Antigravity мешают проверить ограничения Flash.",
    "flash_worker_failed": "Сбой исполнителя Flash. Сохранённый расход доступен в отчёте.",
    "flash_gate_rejected": "Ограничения доступа Flash не подтверждены.",
    "flash_gate_unverified": "Проверка ограничений Flash не прошла.",
    "flash_capacity_unavailable": "Antigravity сообщил об ограничении квоты или доступности Flash. Остаток можно обновить в настройках.",
    "flash_quota_exhausted": "По последнему снимку квота Gemini исчерпана.",
    "flash_output_limit": "Ответ Flash превысил порог выходных токенов.",
    "workspace_command_outside_scope": "Команда записала файл вне разрешённых путей. Её изменения отклонены.",
    "api_plan_invalid": "План отклонён. Причины указаны в отчёте.",
    "api_plan_not_configured": "Проверь колонки Plan и Subtasks и настройки проекта.",
    "api_plan_staging_failed": "Запись подзадач прервана. Сохранённый план можно восстановить.",
    "api_plan_parent_changed": "Исходная карточка или версия задачи изменились.",
    "api_plan_subtask_changed": "Номер подзадачи занят или черновик изменён.",
    "api_subtask_check_failed": "Целевая проверка подзадачи не пройдена.",
    "api_subtask_check_changed_files": "Проверка изменила файлы результата.",
    "api_subtask_result_changed": "Версия ранее проверенной подзадачи изменилась.",
    "api_subtasks_budget_exhausted": "Достигнут общий предел составной задачи.",
    "api_subtasks_not_active": "Выполнение составной задачи остановлено.",
    "api_final_result_changed": "Закреплённая версия, база или исходная карточка изменились.",
    "api_final_settings_changed": "Настройки итоговой проверки изменились.",
    "api_final_test_interrupted": "Итоговая проверка была прервана.",
    "api_final_tests_unavailable": "Команда проверки проекта недоступна в изоляции.",
    "api_final_tests_failed": "Общий результат не прошёл итоговую проверку.",
    "api_final_test_copy_changed": "Итоговая проверка изменила отслеживаемые файлы.",
    "api_final_review_lane_required": "На доске нужна колонка Human Review для передачи результата.",
    "budget_exhausted": "Бюджет ходов исчерпан.",
    "max_state_turns": "Превышен лимит ходов на этапе.",
}


_ATTENTION_FILE_LIMIT = 1024 * 1024
_ATTENTION_REASON_LIMIT = 4096
_SAFE_ATTENTION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_STOPPED_STATUSES = ("stopped", "failed", "blocked", "needs_human")


def _safe_attention_id(value: Any) -> bool:
    return isinstance(value, str) and _SAFE_ATTENTION_ID.fullmatch(value) is not None


def _read_bounded_attention_file(
    root: Path, parts: tuple[str, ...], *, private: bool, limit: int = _ATTENTION_FILE_LIMIT,
) -> bytes | None:
    """Open each component beneath a trusted root without following symlinks.

    Directory descriptors also close the check/open race for intermediate paths.
    No directories, lock files or recovery records are created by this reader.
    """
    if (not parts or any(not part or part in {".", ".."} or "/" in part
                         or "\\" in part or "\0" in part for part in parts)
            or not isinstance(limit, int) or not 0 < limit <= _ATTENTION_FILE_LIMIT):
        return None
    try:
        with ExitStack() as stack:
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            directory = os.open(root.resolve(strict=True), flags | os.O_DIRECTORY)
            stack.callback(os.close, directory)
            for part in parts[:-1]:
                directory = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
                stack.callback(os.close, directory)
            fd = os.open(parts[-1], flags, dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_size > limit
                        or (private and (stat.S_IMODE(info.st_mode) != 0o600
                                         or info.st_uid != os.getuid()))):
                    return None
                raw = stream.read(limit + 1)
                return raw if len(raw) <= limit else None
    except (OSError, ValueError, RuntimeError):
        return None


def _state_file_parts(filename: Any) -> tuple[str, ...] | None:
    if not isinstance(filename, str) or len(filename) > 512 or "\\" in filename:
        return None
    parts = tuple(filename.split("/"))
    if (not parts or not filename.endswith(".json")
            or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", part)
                   or part in {".", ".."} for part in parts)):
        return None
    return parts


def _read_safe_state_file(
    cfg: ServiceConfig, identifier: str, filename: str, *,
    require_matching_identifier: bool = True, max_bytes: int = _ATTENTION_FILE_LIMIT,
) -> dict | None:
    parts = _state_file_parts(filename)
    if not _safe_attention_id(identifier) or parts is None or cfg is None:
        return None
    workflow = getattr(cfg, "workflow_path", None)
    if workflow is None:
        return None
    raw = _read_bounded_attention_file(
        Path(workflow).resolve().parent, (".ours-state", "api-team", identifier, *parts),
        private=True, limit=max_bytes,
    )
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    if require_matching_identifier and data.get("identifier") != identifier:
        return None
    return data


def _read_assignment(cfg: ServiceConfig, identifier: str) -> dict | None:
    return _read_safe_state_file(cfg, identifier, "assignment.json")


def _read_subtasks(cfg: ServiceConfig, identifier: str) -> dict | None:
    data = _read_safe_state_file(cfg, identifier, "subtasks.json", require_matching_identifier=False)
    if data is not None and data.get("identifier", identifier) != identifier:
        return None
    return data


def _read_session(
    cfg: ServiceConfig, identifier: str, *, assignment: dict | None = None,
    session_file: str | None = None,
) -> dict | None:
    """Read a stopped session selected by the current assignment, not an old attempt.

    Supported references are session_file/session_path (or a string session),
    relative to this card's state directory. Absolute references are accepted
    only beneath that same directory; every component is still opened no-follow.
    Without a reference, a session must share a run/assignment/attempt ID with
    the assignment. A standalone canonical session.json needs its own matching
    card identifier. Missing identity is allowed only for an explicitly selected
    file. Conflicting identities always fail closed.
    """
    if not _safe_attention_id(identifier):
        return None
    selected = session_file
    if assignment is not None:
        if assignment.get("status") not in (*_STOPPED_STATUSES, "result"):
            return None
        references = [assignment[key] for key in ("session_file", "session_path", "session")
                      if key in assignment and assignment[key] is not None
                      and not (key == "session" and isinstance(assignment[key], dict))]
        if references:
            if any(not isinstance(ref, str) or ref != references[0] for ref in references):
                return None
            selected = references[0]
    explicit = selected is not None
    if explicit:
        if not isinstance(selected, str) or not selected or "\\" in selected:
            return None
        if Path(selected).is_absolute():
            workflow = getattr(cfg, "workflow_path", None)
            if workflow is None:
                return None
            card_root = Path(workflow).resolve().parent / ".ours-state" / "api-team" / identifier
            try:
                selected = str(Path(selected).relative_to(card_root))
            except ValueError:
                return None
    else:
        selected = "session.json"
    if _state_file_parts(selected) is None or selected in {"assignment.json", "subtasks.json"}:
        return None
    data = _read_safe_state_file(cfg, identifier, selected, require_matching_identifier=False)
    if data is None or data.get("status") not in _STOPPED_STATUSES:
        return None
    identities = [data[key] for key in ("identifier", "issue_identifier") if key in data]
    if any(value != identifier for value in identities) or (not explicit and not identities):
        return None
    shared_identity = False
    if assignment is not None:
        for key in ("run_id", "assignment_id", "attempt_id"):
            if key in assignment and key in data:
                value = assignment[key]
                if not isinstance(value, str) or not value or value != data[key]:
                    return None
                shared_identity = True
        if not explicit and not shared_identity:
            return None
    return data


def _attention_state_key(state: Any) -> str:
    if not isinstance(state, str):
        return ""
    words = " ".join(state.strip().casefold().replace("_", " ").replace("-", " ").split())
    if words in {"needs human", "нужна помощь"}:
        return "needs_human"
    if words in {"blocked", "заблокирован", "заблокирована", "заблокировано"}:
        return "blocked"
    return words


def is_attention_state(state: str | None) -> bool:
    return _attention_state_key(state) in {"needs_human", "blocked"}


def _card_scalar(value: str) -> str | None:
    """Conservatively read a YAML string scalar, never stringify objects/errors."""
    value = value.strip()
    if value.startswith('"'):
        try:
            result, end = json.JSONDecoder().raw_decode(value)
            rest = value[end:].strip()
            return result if isinstance(result, str) and (not rest or rest.startswith("#")) else None
        except ValueError:
            return None
    if value.startswith("'"):
        match = re.fullmatch(r"'((?:[^']|'')*)'(?:\s+#.*)?", value)
        return match[1].replace("''", "'") if match else None
    value = value.split(" #", 1)[0].rstrip()
    if (not value or value[0] in "[{|>&*!" or value.casefold() in {"null", "~", "true", "false"}
            or re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value)):
        return None
    return value


def _read_safe_card_content(cfg: ServiceConfig, identifier: str) -> tuple[dict[str, Any], str] | None:
    """Read bounded card frontmatter and body, without the tracker's mutating APIs.

    Only top-level string scalars (including literal/folded blocks) are used.
    Unsupported or malformed metadata is not guessed into a stop reason.
    """
    if not _safe_attention_id(identifier):
        return None
    board_root = getattr(getattr(cfg, "tracker", None), "board_root", None)
    if board_root is None:
        return None
    raw = _read_bounded_attention_file(Path(board_root), (f"{identifier}.md",), private=False)
    if raw is None:
        return None
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError:
        return None
    if not lines or lines[0] != "---":
        return None
    end = next((i for i in range(1, len(lines)) if lines[i] == "---"), None)
    if end is None:
        return None
    front: dict[str, Any] = {}
    keys: set[str] = set()
    i = 1
    while i < end:
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#") or line[0].isspace():
            continue
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)", line)
        if match is None or match[1] in keys:
            return None
        key, value = match.groups()
        keys.add(key)
        if value in {"|", "|-", "|+", ">", ">-", ">+"}:
            block: list[str] = []
            while i < end and (not lines[i].strip() or lines[i][0].isspace()):
                block.append(lines[i])
                i += 1
            indent = min((len(row) - len(row.lstrip()) for row in block if row.strip()), default=0)
            separator = " " if value.startswith(">") else "\n"
            front[key] = separator.join(row[indent:] for row in block).strip()
        else:
            scalar = _card_scalar(value)
            if scalar is not None:
                front[key] = scalar
    return front, "\n".join(lines[end + 1:])


def _matching_attention_card(front: dict[str, Any] | None, issue: Issue) -> TypeGuard[dict[str, Any]]:
    return bool(isinstance(front, dict)
                and front.get("identifier", front.get("id")) == issue.identifier
                and _attention_state_key(front.get("state")) == _attention_state_key(issue.state))


def _reason_text(value: Any) -> str | None:
    return value.strip()[:_ATTENTION_REASON_LIMIT] if isinstance(value, str) and value.strip() else None


def _recorded_reason(record: dict) -> str | None:
    text = _reason_text(record.get("stop_reason")) or _reason_text(record.get("reason"))
    return ATTENTION_REASONS.get(text, PUBLIC_ERRORS.get(text, text)) if text else None


def _current_stop_heading(title: str, updated_at: datetime | None) -> bool:
    if title.casefold() in {"current stop", "stop reason", "актуальная остановка", "текущая остановка"}:
        return True
    # Legacy failure sections are append-only history. Accept a dated one only
    # when its aware timestamp exactly matches the saved card revision.
    match = re.fullmatch(
        r"(Check Failure|Quota Exhausted|Budget Exceeded|Integration|Level|Plan Failure|Final Check)\s+\(([^()]+)\)",
        title, re.IGNORECASE,
    )
    if match is None or not isinstance(updated_at, datetime) or updated_at.utcoffset() is None:
        return False
    try:
        recorded = datetime.fromisoformat(match[2].replace("Z", "+00:00").replace(" UTC", "+00:00"))
        return recorded.utcoffset() is not None and recorded == updated_at
    except ValueError:
        return False


def _extract_reason_from_markdown(
    card_text: str | None, frontmatter: dict | None = None, *, updated_at: datetime | None = None,
) -> str | None:
    if isinstance(frontmatter, dict):
        for key in ("current_stop_reason", "stop_reason"):
            reason = _reason_text(frontmatter.get(key))
            if reason:
                return reason
    # Only an explicitly named *current* stop slot is authoritative, not a
    # historical Check Failure/Integration/Budget section or a heading prefix.
    current: list[str] | None = None
    latest: list[str] | None = None
    fence: str | None = None
    for line in (card_text or "").splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[0]
            fence = None if fence == marker else (marker if fence is None else fence)
            continue
        if fence is not None:
            continue
        heading = re.fullmatch(r" {0,3}(#{1,6})\s+(.+?)\s*#*\s*", line)
        if heading:
            if current is not None:
                latest = current
            current = [] if len(heading[1]) == 2 and _current_stop_heading(heading[2], updated_at) else None
        elif current is not None:
            current.append(line)
    if current is not None:
        latest = current
    return _reason_text("\n".join(latest)) if latest is not None else None


class AttentionRecords:
    """One read-only snapshot per board response; never a process-global cache."""

    def __init__(self, cfg: ServiceConfig):
        self.cfg = cfg
        self.assignments: dict[str, dict | None] = {}

    def assignment(self, identifier: str) -> dict | None:
        if identifier not in self.assignments:
            self.assignments[identifier] = _read_assignment(self.cfg, identifier)
        return self.assignments[identifier]


def resolve_stop_reason(
    cfg: ServiceConfig, issue: Issue, *, card_text: str | None = None,
    frontmatter: dict[str, Any] | None = None, records: AttentionRecords | None = None,
) -> str:
    """Priority: explicit current assignment stop, failed final, stopped launch,
    bound stopped session, then the card's explicit current-stop slot.

    Only the canonical assignment.json is examined. Active/prepared/queued (or
    unknown) assignments invalidate historical nested stops and old sessions.
    A result may carry a failed final/launch or select a stopped session, but
    its old top-level error/reason and generic candidate status are not causes.
    A later final/launch record, even with no reason, invalidates earlier-stage
    failures. Legacy dated card sections require the exact saved revision time.
    We never mine errors, previous attempts, events, scheduler decisions, or
    generic Markdown failure sections. No corroborated record means fallback.
    """
    if not is_attention_state(issue.state):
        return FALLBACK_REASON
    records = records or AttentionRecords(cfg)
    assignment = records.assignment(issue.identifier)
    earlier_stages_are_history = False
    if assignment is not None and assignment.get("status") in (*_STOPPED_STATUSES, "result"):
        if assignment.get("status") in _STOPPED_STATUSES:
            reason = _recorded_reason(assignment)
            if reason:
                return reason
        final = assignment.get("final")
        if isinstance(final, dict) and final.get("status") in ("failed", "checking", "ready", "complete", "passed"):
            earlier_stages_are_history = True
            if final["status"] == "failed":
                reason = _recorded_reason(final)
                if reason:
                    return reason
        plan = assignment.get("plan")
        launch = plan.get("launch") if isinstance(plan, dict) else None
        if not isinstance(launch, dict):
            launch = assignment.get("launch")
        if (not earlier_stages_are_history and isinstance(launch, dict)
                and launch.get("status") in ("stopped", "active", "complete")):
            earlier_stages_are_history = True
            if launch["status"] == "stopped":
                reason = _recorded_reason(launch)
                if reason:
                    return reason
    if (not earlier_stages_are_history
            and (assignment is None or assignment.get("status") in (*_STOPPED_STATUSES, "result"))):
        session = _read_session(cfg, issue.identifier, assignment=assignment)
        if session is not None:
            reason = _recorded_reason(session)
            if reason:
                return reason
    if _matching_attention_card(frontmatter, issue):
        reason = _extract_reason_from_markdown(
            card_text, frontmatter, updated_at=getattr(issue, "updated_at", None),
        )
        if reason:
            return reason
    return FALLBACK_REASON


def build_parent_metadata_index(
    cfg: ServiceConfig, card_identifiers: list[str], *, records: AttentionRecords | None = None,
) -> dict[str, str]:
    """Index exact saved plan.cards/subtasks membership once, rejecting ambiguity."""
    records = records or AttentionRecords(cfg)
    identifiers = dict.fromkeys(pid for pid in card_identifiers if _safe_attention_id(pid))
    claims: dict[str, set[str]] = {}
    for parent in identifiers:
        assignment = records.assignment(parent)
        plan = assignment.get("plan") if assignment else None
        manifest = _read_subtasks(cfg, parent)
        lists = [plan.get("cards")] if isinstance(plan, dict) else []
        if manifest:
            lists.extend([manifest.get("subtasks"), manifest.get("cards")])
        for items in lists:
            if not isinstance(items, list):
                continue
            for item in items:
                child = item.get("identifier") if isinstance(item, dict) else item
                if isinstance(child, str) and child != parent and child in identifiers:
                    claims.setdefault(child, set()).add(parent)
    return {child: next(iter(parents)) for child, parents in claims.items() if len(parents) == 1}


def resolve_parent_identifier(
    cfg: ServiceConfig, issue: Issue, all_issue_identifiers: set[str], *,
    card_frontmatter: dict[str, Any] | None = None, parent_metadata_index: dict[str, str] | None = None,
    records: AttentionRecords | None = None,
) -> str | None:
    """Prefer the explicit request card, then direct metadata, then saved plan membership.

    A request with no matching board card is just a group. Dependencies and
    identifier suffixes never establish parenthood. Session history is not plan
    metadata. Parent references must name a different existing card.
    """
    records = records or AttentionRecords(cfg)
    candidates: list[Any] = [issue.request]
    if _matching_attention_card(card_frontmatter, issue):
        candidates.extend(card_frontmatter.get(key) for key in ("parent_identifier", "parent"))
    assignment = records.assignment(issue.identifier)
    if assignment:
        candidates.append(assignment.get("parent_identifier"))
        subtask = assignment.get("subtask")
        if isinstance(subtask, dict):
            candidates.extend(subtask.get(key) for key in ("parent_identifier", "parent"))
    candidates.append((parent_metadata_index or {}).get(issue.identifier))
    for candidate in candidates:
        if isinstance(candidate, str):
            candidate = candidate.strip()
            if candidate != issue.identifier and candidate in all_issue_identifiers:
                return candidate
    return None


def confirmed_in_queue(cfg: ServiceConfig, identifier: str) -> bool:
    """The runner still checks the full binding under lock before spending."""
    data = _read_assignment(cfg, identifier)
    return bool(data and data.get("status") == "queued" and data.get("confirmed_at"))


def verification_wait(cfg, issue, pause_reason: str | None) -> str | None:
    """Describe the free check of a finished API run; never grant dispatch or acceptance."""
    if (pause_reason != FINISHED_HOLD or cfg is None or issue.state.casefold() not in {"verify", "проверка"}
            or not uses_board_queue(cfg, issue.agent_kind)):
        return None
    data = _read_assignment(cfg, issue.identifier)
    if not data or data.get("status") != "result":
        return None
    if data.get("subtask"):
        check = data.get("subtask_check")
        return "api_verify_result" if isinstance(check, dict) and check.get("status") == "passed" else None
    if data.get("aggregate", {}).get("method") == "local":
        return "api_verify_result"
    if (data.get("check_lane") != issue.state or not isinstance(candidate := data.get("candidate"), dict)
            or candidate.get("status") != "awaiting_full_checks"):
        return None
    seen = data.get("cloud_seen")
    phase = seen.get("phase") if isinstance(seen, dict) else None
    if phase == "queued":
        return "api_verify_queued"
    if phase == "running":
        return "api_verify_running"
    if phase in ("finished", "waiting", "verified"):
        return "api_verify_result"
    if phase in ("interrupted", "unverified"):
        return "api_verify_pending"
    return "api_verify_waiting"


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
    if action not in {"prepare", "confirm", "show", "stop", "review", "attach-check", "sync-checks", "run",
                      "team-options", "set-team-default", "publish-check", "accept", "submit",
                      "permission-show", "permission-prepare", "permission-authorize", "permission-revoke",
                      "settings-preview", "settings-save", "gemini-quota", "prepare-subtasks", "start-subtasks",
                      "check-local-final"}:
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
    timeout = profile.turn_timeout_ms / 1000 if action in {"run", "check-local-final"} else 60
    if action == "sync-checks":
        timeout = 120
    elif action in {"publish-check", "accept"}:
        timeout = 900
    elif action == "permission-authorize":
        timeout = 300  # Two owner-controlled Keychain prompts; no provider call.
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
    """Reconcile verification and approved subtasks even with the browser closed."""
    now = time.monotonic()
    if cfg.tracker.kind != "file" or now < orch._next_cloud_check:
        return
    if orch._cloud_check_task is not None and not orch._cloud_check_task.done():
        return
    orch._next_cloud_check = now + 60
    kinds = [kind for kind, p in cfg.agent.profiles.items() if p.format == FORMAT]
    state = cfg.workflow_path.parent / ".ours-state/api-team"
    if not kinds or not state.is_dir():
        return

    async def check_local(kind, identifier):
        try:
            await invoke(cfg, kind, identifier, "check-local-final")
        except (OSError, SymphonyError, TimeoutError) as exc:
            get_logger().warning("api_team_local_final_unavailable", profile=kind, error_type=type(exc).__name__)
        finally:
            orch.request_refresh()

    async def sync():
        subtasks = await asyncio.to_thread(lambda: next(state.glob("*/subtasks.json"), None) is not None)
        for kind in kinds:
            command = cfg.agent.profiles[kind].command
            if not (subtasks and supports_team_settings(command)) and not await asyncio.to_thread(_cloud_enabled, command):
                continue
            try:
                result = await invoke(cfg, kind, "", "sync-checks")
            except (OSError, SymphonyError, TimeoutError) as exc:
                get_logger().warning("api_team_cloud_sync_unavailable", profile=kind, error_type=type(exc).__name__)
                continue
            ids = result.get("local_checks", []) if isinstance(result, dict) else []
            task = getattr(orch, "_api_final_check_task", None)
            if ids and (task is None or task.done()):
                orch._api_final_check_task = orch._spawn_supervised(check_local(kind, ids[0]), name="api-team-local-final")

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
