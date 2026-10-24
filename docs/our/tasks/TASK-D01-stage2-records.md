# TASK-D01 — итоги этапа 2 (часть 2) в документы

**Тип:** документы. Код проекта НЕ меняется.
**Модель:** Sonnet. **Базовая ветка:** `claude/r02-done-gate` (REPORT-02 ещё не влит в `our-main`). **Новая ветка:** `claude/d01-stage2-docs`. PR — в `our-main` нашего форка (не в репозиторий автора).

Сначала прочитать `CLAUDE.md`, `docs/our/DECISIONS.md`, `docs/our/ROADMAP.md`, `docs/our/reports/REPORT-01.md` и `REPORT-02.md`. Если `REPORT-02.md` в ветке нет — остановиться и сообщить.
Первым коммитом сохранить этот текст как `docs/our/tasks/TASK-D01-stage2-records.md`.

Меняются только `DECISIONS.md` и `ROADMAP.md`. Язык — русский, простой, термины объясняй (правило 9 из `CLAUDE.md`). Старые записи не удалять и не переписывать; новое добавлять сверху в формате «### 2026-10-24 — заголовок», как в `DECISIONS.md`.

## 1. DECISIONS.md, запись «Итоги локальной пробы (этап 2, часть 2)»

Каждый пункт помечать «проверено запуском на Mac» или «по коду (REPORT-02)»:

- а) `needs_human` в `terminal_states` (порядок `[Human Review, needs_human, Done, Blocked, Archive]`): `doctor` по-прежнему пишет «agent.stage_contracts: auto — contracts enforced», ядро контракты не отключает. Находка 5 закрыта.
- б) Исчерпание бюджета ходов: ядро переносит карточку в статус из `agent.budget_exhausted_state` (по умолчанию Blocked); проверены оба значения, Blocked и needs_human. При Blocked ядро само заводит карточку `FIX-<ID>-1` (метки `blocked-fix`, `source-<id>`), у FIX-карточки второго уровня нет. При needs_human FIX не создаётся.
- в) Выключатель автолечения: `agent.auto_recover_blocked: false` (по умолчанию true; `workflow/config.py:190`), проверено запуском: карточка в Blocked, FIX не создаётся. Побочный эффект: в той же функции отключается и «хостовое спасение» истории git. Находка 4 закрыта. Рабочий конфиг: `budget_exhausted_state: needs_human` + `auto_recover_blocked: false`.
- г) Какой терминальный статус ядро выбирает само (по коду): «к человеку» → первый по списку с human/review/block, при нашем порядке это Human Review; сбои бюджета повторов и истории git → Blocked; needs_human ядро само не выбирает, его ставит только наш скрипт. Значит: Blocked = сбой процесса, needs_human = лестница пройдена, есть диагностика.
- д) База ветки в `after_create` (`scripts/symphony-setup-worktree.sh`): при пустой `SYMPHONY_FEATURE_BASE_BRANCH` берётся текущая ветка главного репозитория на момент создания; если ветка `symphony/<ID>` уже есть, используется она и новая не создаётся. Решение: в рабочем `WORKFLOW.md` задавать `feature_base_branch` явно. Правка 4 из REPORT-01 (внешний скрипт чистого старта) подтверждена как нужная.
- е) Откат Done из `after_run` работает: хук, вставленный ПЕРВОЙ строкой блока `after_run`, читает state из файла карточки (`<workflow>/kanban/<ID>.md`) и возвращает карточку командой `symphony board mv <ID> "In Progress" --root <kanban>`; оркестратор не закрыл задачу посреди хода. Откат не тратит `max_attempts`, счётчик неудач хук ведёт сам. Штатный `after_run` имеет ранний выход при отсутствии изменений, поэтому наш вызов обязан стоять первой строкой.
- ж) Сломанный хук (код возврата 1) пропускает Done: карточка Todo→Done осталась в Done. Значит, нужен второй рубеж в ядре.
- з) Рабочая папка: при Done удаляется, ветка `symphony/<ID>` остаётся. Для needs_human на старте службы папка удаляется (сохраняются только blocked и human review, зашито в `retry_reconcile.py:1192`); при сверке (reconcile) удаляется любой не-Done терминальный статус; если воркер закончил сам, не-Done папка остаётся. Диагностику хранить вне рабочей папки: в карточке, ветке `symphony/<ID>`, `.symphony-workspace-hook-output/`, `.symphony/artifacts/<ID>/`.
- и) Формат лимитов claude (проверено запуском, claude установлен нативным установщиком, вход через подписку, ключ API не задан): в потоке stream-json есть кадр `rate_limit_event` → `rate_limit_info` с полями `status` (в пробе allowed), `resetsAt`, `rateLimitType`, `overageStatus`, `overageDisabledReason` (org_level_disabled), `isUsingOverage`, `unifiedWindows.five_hour` и `unifiedWindows.seven_day`, у каждого `utilization` и `resetsAt`. Итоговый кадр `result` содержит `total_cost_usd` (0.0023), `usage`, `modelUsage`; `--model haiku` принимается. Условие правки №7 из REPORT-01 выполнено. Не подтверждено: шкала `utilization` (в пробе 0.15 и 0.04: доля или проценты; сверить со страницей Usage в аккаунте), другие значения `status` (неизвестное считать «стоп»), приходит ли кадр на каждый запуск.
- к) Окружение хуков: в `bash -lc` команда `python` — 2.7, поэтому в `WORKFLOW.md` и хуках нужны полные пути к python, symphony, claude (claude лежит в `~/.local/bin`, из `bash -lc` виден). Добавить в чеклист конфигурации.
- л) Базовый уровень тестов `our-main` (коммит 555d19c): 3780 прошли, 136 пропущены, 2 упали из-за окружения (`test_pm_planning.py::test_stop_kills_term_resistant_group_after_leader_exits` и `test_pm_ui_node.py::test_pm_ui_node_regressions`).

## 2. DECISIONS.md, запись «Решения по REPORT-02: Done-шлюз»

- `agent.done_gate_command` размещаем в `_auto_merge_done_gate_or_block` (`blocked_recovery.py:467`) ДО раннего выхода при `auto_merge_on_done: false`; размещение рядом с `evaluate_contract` отклонено (контракты не вызываются вне хода агента, у этапа todo контракта нет, условие (2) не выполняется). Оценка выросла с 40–70 до ≈100 строк в 4 файлах + ≈150–200 строк тестов (`tests/test_done_gate.py`, 9 сценариев из REPORT-02, 5.6). Пути приёмки: воркер закончил, reconcile увидел Done, confirm_done.
- Отказ шлюза → статус Blocked, как у ворот слияния; ключ `agent.done_gate_failure_state` не добавляем; `auto_recover_blocked: false` обязателен во всех рабочих конфигурациях.
- Вход скрипта: рабочая папка, `SYMPHONY_ISSUE_ID`, `SYMPHONY_WORKSPACE`, `SYMPHONY_BRANCH`, `SYMPHONY_ISSUE_STATE` + обычные хуковые переменные; команда только из `WORKFLOW.md`. Закрытый режим: код ≠ 0, таймаут, команда не найдена, любое исключение = отказ; пустая команда = шлюз выключен.
- Вторая правка ядра: выключатель автопереноса Human Review → Done (`_auto_normalize_legacy_human_review_done`, `blocked_recovery.py:520-576`, вызов `dispatch.py:141` на каждом такте; Done ставится без проверок, если в карточке есть отчёт «## As-Is -> To-Be Report»), например `agent.normalize_legacy_human_review: false`, ≈6 строк + 1–2 теста. Без неё решение «финал — Human Review» не работает.
- Сознательно не закрываем сейчас (закрываем правилами): `board mv`, веб-правка, «Done без воркера», в том числе правка агентом файла другой карточки через общую папку kanban. Вариант C (проверка всех Done-карточек на каждом такте, ≈+60 строк) отложен до случая, когда на реальных прогонах увидим Done без проверки. Правила: Done ставит только `confirm_done`; `board mv` принимает любое имя статуса, скрипты конвейера проверяют имена сами.
- Контракты этапов проверяют только форму разделов; переход Todo→Done их обходит; ядро сравнивает статус только в начале и конце хода.
- Падение хука `after_run` оставляет след в журнале службы (`hook_failed`, `hook_after_run_failed`) и в файлах `.symphony-workspace-hook-output/<ключ>/*-after_run.json` (поле `returncode`); в карточке и API следа нет. Утренний отчёт может собирать эти файлы.
- Поправки: строка «delivery: …» печатается командой `board show`, в карточку не пишется; раздел 4.4 REPORT-01 про папки уточнён REPORT-02.
- Открытые вопросы: окно между «Done выставлен» и «Done отменён» (запускаются ли зависимые карточки); создание карточки сразу в Done через веб-API; настройка сохранения папки needs_human (`agent.preserve_workspace_states`, ≈12 строк, пока не нужна).

## 3. ROADMAP.md

- Этап 2: первый пустой пункт («Проверить по REPORT-01 на демо-доске…») отметить `[x]` и дописать «(итоги — DECISIONS.md, 2026-10-24)»; пункт «После 30.10…» оставить пустым.
- Этап 3: пункт «Задача не может попасть в Done…» разбить на отдельные пункты: `done_gate_command`; выключатель автопереноса Human Review → Done; рабочая конфигурация (needs_human в terminal_states, `budget_exhausted_state: needs_human`, `auto_recover_blocked: false`, `auto_merge_on_done` и `auto_merge_push_target: false`, явная `feature_base_branch`, полные пути); хук `after_run` с вызовом первой строкой (прототип — `scripts/gate-probe.sh` из локальной пробы); правка №7 (лимиты claude в `/api/v1/state`, формат проверен). К хуку и правке №4 добавить пометку «внешний скрипт чистого старта». Все пункты без отметок.
- В начало файла к фразе про облачный кредит добавить: «Осталось до 05.11 — приоритет правки ядра этапа 3».

## Правила

Код, тесты, существующие записи не менять; в конце — описание PR (что сделано, зачем, как проверено, что не сделано, риски; PR объединяет R02 и D01).
