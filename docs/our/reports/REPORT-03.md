# REPORT-03 — выключатель автопереноса Human Review → Done (TASK-K01)

Пометки: **[запуск]** — проверено запуском, **[код]** — по чтению кода.

## Что изменено

Новая настройка `agent.normalize_legacy_human_review` (bool, по умолчанию `true` — без ключа поведение прежнее). При `false` функция, которая на каждом такте сама переводит карточки из `Human Review` в `Done`, сразу выходит и ничего не читает и не пишет.

| Файл | Строки | Что |
|---|---|---|
| `src/symphony/workflow/constants.py` | 46 | значение по умолчанию `DEFAULT_NORMALIZE_LEGACY_HUMAN_REVIEW = True` |
| `src/symphony/workflow/config.py` | 27, 192-195 | поле `AgentConfig.normalize_legacy_human_review` |
| `src/symphony/workflow/builder.py` | 68, 390-394 | разбор ключа из `WORKFLOW.md` |
| `src/symphony/orchestrator/blocked_recovery.py` | 527-528 | сама проверка «выключено» → `return 0` |
| `docs/reference.md` | 368-374 | описание в справочнике настроек |
| `tests/test_normalize_legacy_human_review_switch.py` | новый файл, 7 тестов | см. ниже |

**Где стоит проверка.** Внутри функции `_auto_normalize_legacy_human_review_done`, а не у вызова в `dispatch.py:141`. Причина: вызов один, но функция привязана к оркестратору (`core.py:901`) и существующий тест подменяет её (`test_orchestrator_dispatch.py:5075`); проверка внутри защищает любой будущий вызов и не трогает `dispatch.py`. Выключатель стоит до обращения к доске, так что при `false` ядро не читает карточки.

**Расхождение с заданием (разбор значения).** Образец `auto_recover_blocked` читается мягким `bool(...)`: строка `"false"` дала бы `True`. Для предохранителя это опасно (опечатка молча оставит автоперенос включённым), поэтому взят строгий `_validated_bool`, как у `agent.crash_continuation` и ещё 11 ключей: не-булево значение (`"false"`, `0`) — ошибка конфигурации при загрузке. Генератор WORKFLOW перечисления ключей `agent.*` не содержит (его роль выполняет `builder.py`).

## Тесты

| | Прошли | Пропущены | Упали |
|---|---|---|---|
| До правок [запуск] | 3780 | 136 | 2 |
| После [запуск] | 3787 | 136 | 2 |

Упали те же два теста, что в REPORT-02 (`test_pm_planning.py::test_stop_kills_term_resistant_group_after_leader_exits`, `test_pm_ui_node.py::test_pm_ui_node_regressions`); новых падений нет. `ruff check src tests` и `symphony-pyright` — без замечаний [запуск].

Новые тесты (на настоящей файловой доске во временной папке): (а) без ключа и при `true` карточка уходит в Done; (б) при `false` перенос не происходит, файл карточки байт-в-байт тот же, ошибок нет; (в) ключ читается из `WORKFLOW.md`, значения `"false"`, `0`, текст — ошибка. Проверено [запуск], что тест (б) падает, если убрать проверку.

## Вопросы

**Q1. Какие ещё места ядра ставят Done сами (без воркера и `confirm_done`), которых нет в REPORT-02?** Все — [код], без правок:
- `src/symphony/pm/routes.py:1461` — маршрут приёмки PM: после `verifier.accept` вызывает `tracker.update_fields(..., state=done_state)`. Запускается человеком через веб, мимо шлюза.
- `src/symphony/orchestrator/helpers.py:290` (`_rewind_budget_target_state`; вызовы `agent_phase.py:183`, `attempt.py:1107`, `blocked_recovery.py:1372`) — если в `terminal_states` нет статуса с `block`/`human`, берётся `terminal_states[0]`; при порядке по умолчанию `[Done, Cancelled]` это Done.
- `src/symphony/orchestrator/retry_reconcile.py:356-357` — тот же запасной вариант `terminal_states[0]` при исчерпании `max_retries`.
- `src/symphony/orchestrator/helpers.py:262-268` (`_human_review_target_state`; вызов `api_views.py:788`, ручной пропуск Document) — тот же запасной вариант.
- `src/symphony/workflow/builder.py:443` — `agent.budget_exhausted_state` не проверяется, любой статус, в том числе Done, будет записан (`worker_exit.py:546`, `agent_phase.py:715`).
- `src/symphony/orchestrator/worker_exit.py:1183` — `agent.no_stage_change_action` может быть любым терминальным статусом (`builder.py:1366-1386`), включая Done.

В нашем рабочем конфиге (`Human Review` первым, `budget_exhausted_state: needs_human`) пункты 2-4 не сработают, а 5-6 зависят только от значения, которое мы зададим сами [код]. Всё это — ответ «есть, но только при определённой конфигурации».

**Q2. Идёт ли перенос через `_auto_merge_done_gate_or_block`?** **Нет, идёт мимо.** Функция дописывает заметку и вызывает `_tracker_call_update_state` напрямую (`blocked_recovery.py:550-563`); шлюз вызывают только `worker_exit.py:865`, `retry_reconcile.py:1019`, `api_views.py:168` [код]. Запуском [запуск]: на временной доске с подменой шлюза счётчиком вызовов функция вернула `moved: 1`, карточка стала `Done`, вызовов шлюза — 0. Вывод: пока выключатель включён, будущий шлюз (TASK-K02) этот путь **не покроет**; для рабочих конфигураций выключатель должен быть `false`.
