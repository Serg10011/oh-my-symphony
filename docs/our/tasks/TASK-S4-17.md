# S4-17 — PR ядра №4: `agent.dependency_resolving_states` и исправление `budget_reopen_history`

Модель: Sonnet, высокое усилие (`effort=high`: это правка ядра). Правка ядра ≈30–40 строк в `src/symphony/` (две независимые части, каждая — отдельный коммит) + тесты + документы. Читай только то, что указано. Это ядро: существующего не переписывай, только добавляй; поведение без нового ключа не меняется.

Рабочая копия и ветка `dev/S4-17` созданы службой очереди; сам их не создавай и не переключай. Файл задания читай по полному пути в основной папке. Первым коммитом сохрани это задание в `docs/our/tasks/TASK-S4-17.md`. Дальше действуют правила `CLAUDE.md` (PR в `our-main` репозитория `Serg10011/oh-my-symphony`, PR не сливать, в `tmp_workspaces/inbox/` и `outbox/` не писать). Ветка меняет `src/`, поэтому нужна **своя `.venv` в рабочей копии** (раздел «Проверка»).

## Зачем

Шаг 6 порядка архитектора (`docs/our/DECISIONS.md`, «Решения архитектора по REPORT-17», п. 5 и «Порядок реализации», п. 6). Для нарезки карточке-задаче нужен статус `Subtasks`: агент на ней не работает, пока подзадачи не влиты. Такой статус придётся объявить конечным (`terminal_states`), иначе ядро будет брать карточку в работу. Но сейчас ядро считает зависимость выполненной для **любого** конечного статуса, кроме жёсткого списка «неудачных» (`_FAILED_BLOCKER_TERMINAL_STATES` в `orchestrator/core_support.py`: archive, blocked, human review, needs_human и др.). Значит, `Subtasks` стал бы «успешным»: карточка с `blocked_by: <ЗАДАЧА>` стартовала бы до конца задачи, а сводка группы `request` (`webapi/issue_routes.py`, `dependency_state_resolved`) считала бы задачу успешной. Нужен явный список «в каких статусах зависимость выполнена».

Вторая часть — находка проверки боем OMS-5/6/7 и K13 (`docs/our/tasks/TASK-K13-mac-probe-results.md`, п. 6): `run_registry_error` с текстом `SQLite objects created in a thread can only be used in that same thread`, операция `budget_reopen_history`.

Правило 1 `CLAUDE.md`: в описании PR раздел «Почему иначе нельзя» по обеим частям:
- список успешных статусов блокера вычисляется внутри ядра из `terminal_states` минус зашитый список; снаружи (конфигурацией или хуком) его не сузить, а добавлять «неудачные» имена в зашитый список под наш статус — хуже для переноса правок при обновлении ядра, чем общий ключ;
- ошибка — в коде ядра (`orchestrator/api_views.py::issue_budget_snapshot`), снаружи её не обойти.

## Часть А. `agent.dependency_resolving_states`

Где что лежит (сверь с кодом):
- `orchestrator/core_support.py`: `_FAILED_BLOCKER_TERMINAL_STATES`, `_successful_blocker_terminal_states(cfg)`, `_blocker_dependency_is_resolved(blocker_state, cfg)`;
- вызовы `_blocker_dependency_is_resolved`: `dispatch.py::_eligibility_contention_decision` (диспетчер), `api_views.py::dependency_state_resolved` и ≈ стр. 625, `blocked_recovery.py` ≈ стр. 1156; `dependency_state_resolved` используют `webapi/payloads.py` и `webapi/issue_routes.py`;
- конфигурация: `workflow/config.py` (`AgentConfig`, рядом с `preserve_workspace_states`), `workflow/builder.py` (разбор `agent_raw`, проверки, где известны `tracker.active_states` / `terminal_states`).

Сделать:
1. Поле `dependency_resolving_states: tuple[str, ...] = ()` в `AgentConfig`.
2. Разбор в `builder.py` из `agent.dependency_resolving_states`: отсутствует или пустой список — `()`; иначе список непустых строк. Каждое имя обязано быть в `tracker.terminal_states` (сравнение без учёта регистра и крайних пробелов, как в соседних проверках); иначе `ConfigValidationError` с понятным текстом и перечнем допустимых. Не список / не строки — тоже ошибка. Проверку поставь туда, где уже сверяются имена статусов с `active_states`/`terminal_states` (найди по коду).
3. `_successful_blocker_terminal_states(cfg)`: если `cfg` не `None` и список непустой — вернуть ровно эти имена (`normalize_state(...).strip()`); иначе — прежнее вычисление без изменений. Так меняется одно место, а все вызовы (диспетчер, сводки, восстановление `Blocked`) получают одно правило.
4. Найди поиском, нет ли в ядре **другой** копии правила «успешный блокер» (например, в `trackers/`, `cli/board.py`, `tui/`, `board graph`, `webapi/`): `grep -rn "human review\|_FAILED_BLOCKER\|successful.*terminal" src/symphony`. Если есть место, которое решает то же самое отдельно, — опиши его в PR; переводи на общую функцию, только если правка ≤ 5 строк, иначе только опиши.
5. `docs/reference.md`: строка про ключ рядом с `preserve_workspace_states` (или с описанием `blocked_by`): что делает, умолчание («пусто — все конечные, кроме неудачных»), что имена — из `terminal_states`.

`_is_successful_terminal_state` (`helpers.py`, «доставлено» = Done для учёта `reopens`) — другое правило, **не трогать**.

## Часть Б. `budget_reopen_history`

Причина (по коду, подтверди): соединение SQLite реестра (`run_registry_base.py::_connect`, `sqlite3.connect` без `check_same_thread=False`) создаётся в потоке цикла службы, а `issue_budget_snapshot` вызывает `registry.recent_runs(...)` через `asyncio.to_thread` — в другом потоке. Все остальные обращения к реестру через `_registry_guard` (22 места, в том числе `blocked_recovery.py::_prior_done_run_count` с тем же запросом, и `run_leases.py::recent_runs`) идут синхронно в потоке цикла. Следствие: в `budgets` элемент `reopens` всегда `used = null`, и в журнал пишется `run_registry_error` при каждом запросе карточки.

Сделать: в `issue_budget_snapshot` вызвать `self._registry_guard("budget_reopen_history", lambda: registry.recent_runs(...), None)` синхронно, как остальные места. `check_same_thread=False` не вводить (это меняет правила для всего реестра). Если по коду причина другая — остановись и опиши в PR, не чини вслепую.

## Тесты (новые файлы; существующие тесты не менять)

Образцы: `tests/test_budget_snapshot_fields.py`, `tests/test_issue_budgets.py` (бюджеты), тесты зависимостей диспетчера (найди по `waiting_dependency` / `blocker unresolved`), тесты разбора `WORKFLOW` (найди по `preserve_workspace_states`). Подпроцессы — через `sys.executable`.

`tests/test_dependency_resolving_states.py`:
- без ключа поведение прежнее: `Done` — выполнена, `Human Review`, `needs_human`, `Blocked`, `Archive` — нет;
- `terminal_states` с дополнительным `Subtasks`, без ключа — `Subtasks` считается выполненной (фиксирует, зачем ключ);
- тот же набор с `dependency_resolving_states: [Done]` — `Subtasks` не выполнена, `Done` выполнена; регистр имени в ключе не важен;
- диспетчер: карточка с `blocked_by` на карточку в `Subtasks` при ключе `[Done]` получает `waiting_dependency`, после перевода блокера в `Done` — готова; `dependency_state_resolved` даёт то же;
- конфигурация: имя не из `terminal_states` (в том числе активный статус, например `In Progress`) → `ConfigValidationError`; не список, пустая строка → ошибка; пустой список — как без ключа.

`tests/test_budget_reopen_history.py`:
- настоящий `RunRegistry` на временном `state.db` (создан в потоке теста, как в службе) и оркестратор, как в образцах: `issue_budget_snapshot` возвращает `reopens` с числом в `used` (не `None`), счётчик ошибок реестра (`_registry_error_count`) не вырос, `_last_registry_error` пуст;
- с записью успешного запуска карточки в реестре `used` считается как раньше (формула не менялась);
- проверь, что новый тест **падает** на `our-main` без правки (запусти его до правки и укажи в PR).

## Документы (последний шаг, в тот же PR)

- `docs/our/DECISIONS.md`, блок «Текущее состояние» — **переписать с правкой**: строка «В работе» сейчас устарела. Верно: S4-16 влит (PR #38, `our-main` = `3ff3c31`), на доске `oms` `doc_paths` поставлен, `project-render` выполнен. После твоей задачи: дата; сделано — S4-17 (PR ядра №4); в работе — ничего (PR S4-17 ждёт слияния); следующее — шаг 6: статус `Subtasks` (S4-18), хук «план-файл → карточки» (S4-19), промпты `role:manager-plan` (S4-20, карточкой на доску `oms`), выход из `Subtasks` на итоговую проверку (S4-21), проверка боем (S4-22).
- Новая запись «S4-17 — PR ядра №4»: ключ и его умолчание, где проверяется, что `Subtasks` в нашем шаблоне появится в S4-18 вместе со значением `dependency_resolving_states: [Done]`; причина ошибки реестра и почему исправление синхронным вызовом, а не `check_same_thread=False`; найденные другие копии правила (п. 4); «Не проверено: в работающей службе»; «Модель: Sonnet, effort=high».
- Отдельная запись в `DECISIONS.md` «2026-10-27 — Решения архитектора: итоговая проверка задачи и доставка (шаги 6–7)». Перенеси четыре пункта как есть (это решения архитектора, не пересматривать):
  1. Слияние `integration/<ЗАДАЧА>` с `our-main` при итоговой проверке — **настоящим коммитом слияния с двумя родителями** (конец интеграционной ветки + конец `our-main`): `git merge-tree --write-tree` → `git commit-tree -p <интеграция> -p <our-main>` → `git update-ref` с проверкой старого значения. Плоский коммит нельзя: GitHub показал бы правки `our-main` как правки задачи. Если `our-main` не сдвинулся с создания ветки — слияния нет. Конфликт — `needs_human` со списком файлов, ветка не меняется. Дальше: рабочая копия карточки-задачи ставится на результат слияния; `base_sha` в её записи базы = использованный конец `our-main`; интеграционная ветка сдвигается на тот же коммит; полный `test.sh` без пропуска по `doc_paths`.
  2. В S4-21 хэш `our-main`, с которым прошла итоговая проверка, записывается в `.ours-state/<ЗАДАЧА>.final.json`.
  3. Шаг 7 (`.deliver`, решение записываем сейчас, делаем потом): если `our-main` отличается от записанного в `.final.json`, программа сама повторяет слияние с новым `our-main` + полный `test.sh` + `check.py`, без этапов агента. Зелёное — пуш; конфликт или красное — пуша нет, карточка в `needs_human` с причиной.
  4. Сдвиг `our-main` между доставкой и «добро» Сергея — принятый риск, как у наших PR S4; страховка — полный прогон в `post-merge`.
  Плюс одной строкой записать принятую нарезку шага 6 (S4-17 … S4-22; S4-21 — выход из `Subtasks`, вынесен отдельно, чтобы хук нарезки не разросся) и принятые решения по нарезке: формат `plan.toml`, отправка плана переходом в `Subtasks`, «всё или ничего», `[plan] max_subtasks = 5`, сигнал «последняя подзадача получила `Done`» и ручная команда `integrate.py finish-task`.
- `docs/our/ROADMAP.md` (этап 4): отметить «PR ядра №4…»; строку «Нарезка: хук…» заменить строками без галочки: «S4-18 — статус `Subtasks` в шаблоне и заготовке», «S4-19 — хук «план-файл → карточки»», «S4-20 — промпты `role:manager-plan` (карточкой на доску `oms`)», «S4-21 — выход из `Subtasks` на итоговую проверку (слияние с `our-main` двумя родителями)», «S4-22 — проверка боем нарезки на `oms`»; к пункту «Доставка» добавить: «если `our-main` ушёл от записанного в `.final.json` — повтор слияния и полного прогона без агента; конфликт или красное — пуша нет».

## Проверка (в описание PR: команды и результат)

В рабочей копии нет `.venv`; **ветка меняет `src/`, поэтому нужна своя**: `python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`, затем `.venv/bin/python -c "import symphony; print(symphony.__file__)"` — путь должен вести в эту рабочую копию.

```
.venv/bin/python -m ruff check src tests ours
.venv/bin/python -m pytest -q
env -i HOME="$HOME" PATH=/usr/bin:/bin:/usr/sbin:/sbin .venv/bin/python -m pytest ours/tests -q
.venv/bin/symphony-pyright
```

Все прежние тесты проходят. Известное падение среды: `test_pm_ui_node_regressions` (нет `node_modules`) — допустимо, укажи в PR. Любой другой упавший прежний тест — остановиться и выяснить, не подгонять. Службу `oms`, очередь, `launchctl`, настоящие `claude`, `osascript` и `gh` (кроме создания PR) не запускай; доску `oms` и её `state.db` не трогай; `ours/` не меняй (шаблон и заготовка — S4-18).

## PR

```
git push -u origin dev/S4-17
gh pr create --repo Serg10011/oh-my-symphony --base our-main --head dev/S4-17
```

`git remote -v` должен показать `Serg10011/oh-my-symphony`. Описание PR на русском: что сделано (части А/Б), «Почему иначе нельзя», как проверено (команды и результат, падение нового теста до правки), что не сделано, риски. Риски обязательно: правка `src/` — после слияния `post-merge` перезапустит службу `oms` и прогонит `project-doctor` с полным набором; без ключа поведение прежнее; синхронный запрос к реестру в цикле службы — тот же, что уже делает `_prior_done_run_count` (до 200 строк). PR не сливай. В конце — ссылка на PR. Неясное — остановиться и спросить Сергея в окне.
