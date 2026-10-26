# S4-09 — PR ядра №2 «лестница»: `reset-budget`, `level` при создании карточки, `state_turns` и `budget_exhausted` в бюджетах

Модель: Sonnet, высокое усилие (сессия запущена с `--effort high`: это правка ядра). Правка ядра ≈90 строк в `src/symphony/` (три независимые части) + тесты + документы. Читай только то, что указано. Это ядро: ничего существующего не переписывай, только добавляй; каждая часть — отдельный коммит, чтобы её можно было откатить по одной.

Рабочая копия и ветка `dev/S4-09` созданы службой очереди; сам их не создавай и не переключай. Файл задания читай по полному пути в основной папке. Первым коммитом сохрани это задание в `docs/our/tasks/TASK-S4-09.md`. Дальше действуют правила `CLAUDE.md` (PR в `our-main` репозитория `Serg10011/oh-my-symphony`, PR не сливать, в `tmp_workspaces/inbox/` и `outbox/` не писать). Ветка меняет `src/`, поэтому нужна **своя `.venv` в рабочей копии** (см. раздел «Проверка»).

## Зачем

Решение архитектора (`docs/our/DECISIONS.md`, запись «Решения архитектора по REPORT-17», п. 5, PR №2) и REPORT-17, вопросы 6 и 8 (`docs/our/reports/REPORT-17.md`, прочитай только их). Правило 1 `CLAUDE.md`: в описании PR нужен раздел «Почему иначе нельзя», по каждой части:
- флаг `budget_exhausted` хранится в памяти службы и в `state.db`, и ни одно место кода его не снимает; снаружи карточку после остановки по лимиту продолжить нельзя;
- `level` карточки можно задать только правкой файла карточки руками — у `board new` и `POST /api/v1/issues` поля нет, а наш будущий хук «план → карточки» создаёт карточки именно через них;
- `GET /api/v1/issues/{id}` → `budgets` не показывает самый частый лимит (`max_state_turns`) и флаг `budget_exhausted`, а `limit` берёт из общей конфигурации, а не из уровня карточки, — руководитель видит не то, по чему карточку остановят.

Сам сброс вызывает только наш код (`ours/`, в следующих задачах), не агент: `curl` в `allowed_bash` не добавлять, промпты не трогать. Предел числа сбросов на карточку ядро не ведёт — это учёт в `ours/` (решение архитектора, п. 6).

## Часть А. Точечный сброс: `POST /api/v1/{identifier}/reset-budget`

Где что лежит (по REPORT-17, вопрос 6; сверь с кодом):
- память службы: `Orchestrator._issue_debug[issue_id]` — `_IssueDebug` в `orchestrator/entries.py` (`completed_turn_count`, `rewind_count`, `state_turn_state`, `state_turn_count`, `last_error`, `restart_count`);
- флаг: `self._turn_budget_exhausted` (множество, `orchestrator/core.py`) и таблица `issue_flags` в `state.db` (реестр `orchestrator/run_registry.py`, методы `get_issue_flags` / `clear_issue_flags`; обёртка `_clear_issue_flags` уже есть в `orchestrator/run_leases.py`, но не вызывается нигде);
- `self._persisted_retry_attempts` — словарь попыток повтора;
- пауза: `is_paused` / `resume_worker` в `orchestrator/api_views.py`.

Сделать:

1. Метод `async def reset_issue_budget(self, identifier: str) -> dict[str, Any]` в `orchestrator/api_views.py` (рядом с `confirm_done`, в том же стиле) и одна строка привязки в `orchestrator/core.py` (как `confirm_done = api_views.confirm_done`).
   - Найти карточку через трекер, как в `confirm_done` (`_tracker_call_fetch_issue_full_by_id`); нет карточки → `FileNotFoundError`.
   - Отказ `ValueError`, если у карточки есть работающий воркер (`find_running_issue_id`) или активная аренда запуска (`_has_active_run_lease`, `run_leases.py`) — сброс во время работы запрещён.
   - Снимок «до»: `completed_turn_count`, `rewind_count`, `state_turn_state`, `state_turn_count`, `restart_count` (из `_issue_debug`, если запись есть; иначе нули/пусто), `budget_exhausted` (есть ли `issue.id` в `_turn_budget_exhausted`), `retry_attempt` (`_persisted_retry_attempts.get`), `paused` (`is_paused`).
   - Сброс: в `_issue_debug[issue.id]` (если есть) обнулить `completed_turn_count`, `rewind_count`, `state_turn_count`, очистить `state_turn_state` и `last_error`; `restart_count` и `recent_events` не трогать. `_turn_budget_exhausted.discard(issue.id)`; `_persisted_retry_attempts.pop(issue.id, None)`; `self._clear_issue_flags(issue.id, budget_exhausted=True, retry_attempt=True)` — чтобы флаг не вернулся из `state.db` после перезапуска службы.
   - Паузу **не** снимать: это отдельный маршрут `/resume`, и наш код вызовет его сам, если `paused` в ответе `true`. Так у каждого маршрута одна обязанность.
   - Журнал: одна строка `log.warning("budget_reset", issue_id=…, identifier=…, state=…, **снимок_до)`. Событие запуска (`_append_run_event`) не писать: карточка не выполняется, активного запуска нет, а реестр принимает события только для активных запусков (`append_attempt_event`) — укажи это в PR; учёт сбросов ведёт `ours/` по ответу маршрута.
   - Ответ: `{"issue_identifier", "issue_id", "state", "changed": bool, "before": {…снимок…}}`; `changed` = было что сбрасывать (хоть один счётчик/флаг ненулевой).
2. Маршрут `POST /api/v1/{identifier}/reset-budget` в `src/symphony/server.py` рядом с `confirm-done`, обработчик по образцу `handle_confirm_done`: `FileNotFoundError` → 404 `issue_not_found`; `ValueError` → 409 `not_resettable`; прочее → 500 `reset_budget_failed` с `log.warning`. Проверь, как у `confirm-done` устроены права/замок (`_manual_completion_lock`) — используй тот же замок.
3. Докстринг модуля `server.py` (список маршрутов вверху файла) и `docs/reference.md` (раздел про маршруты `pause/resume/confirm-done`) — добавить строку про `reset-budget`.

## Часть Б. `level` при создании карточки

1. `src/symphony/trackers/file.py`: параметр `level: str | None = None` у `create`, `create_validated`, `create_with_next_identifier` и `_new_ticket_front`; в front записывать `front["level"] = level.strip()` только если непустой. Чтение `level` из front уже есть (`level=str(front.get("level") …)`), ничего там не менять. Существующие вызовы без `level` должны работать как раньше.
2. `src/symphony/cli/board.py`, команда `board new`: аргумент `--level` (по умолчанию `None`); проверка формата именем уровня (`LEVEL_NAME_RE` из `workflow/levels.py`, длина ≤ 32) → при нарушении `error: …` и код 1, как у других ошибок команды; если конфигурация (`_resolve_config(args)`) доступна и в ней есть `levels`, а имени там нет — тоже ошибка с перечнем известных уровней. Если конфигурации нет (`--root`) — только проверка формата.
3. `src/symphony/webapi/issue_routes.py`, `handle_issue_create`: поле `level` тела запроса (строка или отсутствует); те же проверки (формат; при непустом `cfg.levels` — известность имени), нарушение → `WorkflowMutationError` с понятным текстом (как у других полей); передать в `tracker.create_validated(level=…)`.
4. `docs/reference.md`: строка про `--level` у `board new` и поле `level` у `POST /api/v1/issues` (найди, где описаны `--skills`/`skills`, добавь рядом).

## Часть В. Бюджеты: `state_turns`, `budget_exhausted`, лимиты уровня

В `orchestrator/api_views.py::issue_budget_snapshot`:
1. Лимиты брать из конфигурации **с уровнем карточки**: `cfg_issue = _config_for_issue_agent(cfg, issue)` в `try`, при `LevelError` — `cfg_issue = cfg` (и ничего не логировать: это только чтение). Все `cfg.agent.*` в `item(...)` заменить на `cfg_issue.agent.*`; `self._token_budget_for_state` и `_max_state_turns_for_state` вызывать с `cfg_issue`.
2. Новый элемент `item("state_turns", limit, used)`: `limit = self._max_state_turns_for_state(cfg_issue, current_state)`; `used = debug.state_turn_count`, если `debug` есть и `debug.state_turn_state == current_state`; `0`, если `debug` есть, но состояние другое (счётчик обнулится первым же ходом); `None`, если записи нет. Поставить в списке после `attempt_turns`.
3. Два поля верхнего уровня в ответе (рядом с `"state"`): `"budget_exhausted": issue.id in self._turn_budget_exhausted` и `"paused": self.is_paused(issue.id)`.
Форму существующих элементов (`name/limit/used/remaining/status`) не менять.

## Тесты (новые файлы; существующие тесты не менять)

Все подпроцессы — через `sys.executable`. Образцы подмен: `tests/test_issue_budgets.py` (бюджеты, фиктивный оркестратор), `tests/test_server_routes.py` (маршруты), `tests/test_board_cli_subcommands.py` (команды `board`), `tests/test_task_levels.py` (уровни).

`tests/test_reset_budget.py`:
- после сброса: счётчики в `_issue_debug` обнулены, `issue.id` нет в `_turn_budget_exhausted` и `_persisted_retry_attempts`, у реестра `get_issue_flags(issue.id)` без `budget_exhausted` и без `retry_attempt`; `restart_count` сохранён; `before` в ответе содержит прежние значения; `changed=True`;
- повторный сброс чистой карточки: `changed=False`, ошибок нет;
- карточка с работающим воркером (запись в `_running`) → `ValueError`; с активной арендой → `ValueError`; неизвестная → `FileNotFoundError`;
- пауза не снимается: `paused` до и после одинаков;
- **после перезапуска**: настоящий `RunRegistry` на временном `state.db` — поставить флаг (`set_issue_flags(..., budget_exhausted=True)`), выполнить сброс, создать новый оркестратор/реестр на том же файле и загрузить флаги (тот код, что при старте службы добавляет в `_turn_budget_exhausted`, `run_leases.py` ≈ строка 523) — карточки в множестве нет;
- маршрут: 200 с телом, 404, 409 (через подмену оркестратора, как в `test_server_routes.py`).

`tests/test_create_level.py`:
- `FileBoardTracker.create(... level="S1")` → в файле карточки `level: S1`, а `fetch_issue_full_by_id(...).level == "S1"`; без `level` — ключа в front нет;
- `board new --level S1` создаёт карточку с уровнем; `--level "bad name!"` → код 1 и сообщение; при конфигурации с `levels` и незнакомом имени → код 1 с перечнем;
- `POST /api/v1/issues` с `level` → 201 и карточка с уровнем; с неверным форматом → 400; с незнакомым уровнем при непустых `levels` → 400; без поля — как раньше.

`tests/test_budget_snapshot_fields.py`:
- в `items` есть `state_turns` с правильными `limit/used` в трёх случаях (то же состояние; другое состояние; нет записи);
- `budget_exhausted` и `paused` присутствуют и отражают состояние;
- лимиты уровня: карточка с `level`, у которого свой `max_turns`/`max_state_turns`, — `limit` у `attempt_turns` и `state_turns` берётся из уровня; с испорченным `level` — из общей конфигурации, без исключения.

## Документы (последний шаг, в тот же PR)

- `docs/our/DECISIONS.md`: блок «Текущее состояние» (дата; сделано — S4-09: PR ядра №2 «лестница» (`reset-budget`, `level` при создании, `state_turns`/`budget_exhausted`/лимиты уровня в бюджетах); в работе — ничего; следующее — интеграционная ветка на задачу и программное Done (п. 1–2 решений архитектора)); запись «S4-09 — PR ядра №2 «лестница»»: что добавлено и где (по трём частям), решения этой задачи — сброс не снимает паузу, событие запуска не пишется (почему), лимиты в бюджетах теперь по уровню; что осталось в `ours/` (предел сбросов, учёт; **при продолжении карточки наш код обнуляет и счётчик провалов `check.py` в `.ours-state` — `reset-budget` ядра его не трогает**); «Не проверено»: на настоящем `claude` и в работающей службе; «Модель: Sonnet».
- `docs/our/ROADMAP.md` (этап 4): отметить пункт «PR ядра №2 «лестница»…» выполненным.
- `ours/README.md`: ничего, если там нет фраз, ставших неверными (например, про невозможность задать `level` командой).

## Проверка (в описание PR: команды и результат)

В этой рабочей копии нет `.venv`; **ветка меняет `src/`, поэтому нужна своя**: `python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`, затем `.venv/bin/python -c "import symphony; print(symphony.__file__)"` — путь должен вести в эту рабочую копию.

```
.venv/bin/python -m ruff check src tests ours
.venv/bin/python -m pytest -q
env -i HOME="$HOME" PATH=/usr/bin:/bin:/usr/sbin:/sbin .venv/bin/python -m pytest ours/tests -q
.venv/bin/symphony-pyright
```

Ожидание: все прежние тесты проходят (в клоне `oms` база `passed=4454 skipped=137 failed=0`; у тебя больше на новые). Известные падения среды: `test_pm_ui_node_regressions` (нет `node_modules`) — допустимо, укажи в PR. Любой другой упавший прежний тест — остановиться и выяснить, не подгонять. Службу `oms`, очередь, `launchctl`, настоящие `claude`, `osascript` и `gh` не запускай; `state.db` проекта `oms` не трогай.

## PR

```
git push -u origin dev/S4-09
gh pr create --repo Serg10011/oh-my-symphony --base our-main --head dev/S4-09
```

`git remote -v` должен показать `Serg10011/oh-my-symphony`. Описание PR на русском: что сделано (по частям А/Б/В), раздел «Почему иначе нельзя», как проверено (команды и результат), что не сделано, риски. Риски обязательно: правка `src/` — после слияния `post-merge` перезапустит службу `oms` и прогонит `project-doctor` с полным набором тестов; маршрут `reset-budget` доступен любому, кто достучится до порта службы (как и `confirm-done`), — порт локальный; форма ответа `GET /api/v1/issues/{id}` расширена только добавлением полей. PR не сливай. В конце — ссылка на PR. Неясное — остановиться и спросить Сергея в окне.
