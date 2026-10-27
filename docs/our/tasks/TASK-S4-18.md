# S4-18 — статусы нарезки `Plan` и `Subtasks`: шаблон `WORKFLOW`, промпты, заготовка

Модель: Sonnet, высокое усилие (`effort=high`). Ядро (`src/`, `tests/`) **не менять**. Правятся `ours/templates/`, `ours/hooks/project_config.py` (контур контроля — можно, задачу запустил человек), `ours/prompts/`, тесты `ours/tests/` и документы. Читай только то, что указано.

Рабочая копия и ветка `dev/S4-18` созданы службой очереди; сам их не создавай и не переключай. Файл задания читай по полному пути в основной папке. Первым коммитом сохрани его в `docs/our/tasks/TASK-S4-18.md`. Дальше действуют правила `CLAUDE.md` (PR в `our-main` репозитория `Serg10011/oh-my-symphony`, PR не сливать, в `tmp_workspaces/inbox/` и `outbox/` не писать).

## Зачем

Шаг 6 порядка архитектора: нарезку делает руководитель на карточке-задаче с меткой `role:manager-plan`. Решения архитектора от 27.10.2026:

- **`Plan` — новый рабочий (активный) статус руководителя.** Промпт ядро выбирает по статусу, а шаблонизатор по метке умеет только **добавить** текст (`{% for l in issue.labels %}{% if l == … %}`), но не убрать текст работника: руководитель в `In Progress` получил бы «пиши код» и «не пиши код» сразу. Путь карточки-задачи: `Plan` → `Subtasks` → итоговая проверка (`Verify` → `Document` → `Human Review`). Своих инструкций требует только нарезка.
- **`Subtasks` — конечный (для ядра) статус ожидания.** План сдан, подзадачи делают работники, агент на карточке-задаче не работает. S4-17 (влит, PR #39) дал ядру ключ `agent.dependency_resolving_states`, без которого `Subtasks` считался бы «выполненной» зависимостью.

Эта задача **только вводит статусы**. Хук «план-файл → карточки» (S4-19), текст промпта нарезки (S4-20, карточкой на доске `oms`), выход из `Subtasks` на итоговую проверку (S4-21) — отдельные задачи, здесь их не делать.

## Часть 1. Шаблон `ours/templates/WORKFLOW.md.tmpl`

1. `tracker.active_states: [Todo, Plan, "In Progress", Verify, Document]`. **`Todo` остаётся первым**: `symphony board new` без `--state` ставит `active_states[0]` (`cli/board.py` ≈ стр. 201). Комментарий: `Plan` — нарезка задачи руководителем (`role:manager-plan`).
2. `tracker.terminal_states: ["Human Review", needs_human, Subtasks, Done, Blocked, Archive]` — `Subtasks` после `needs_human`, остальной порядок прежний. Ядро выбирает статус передачи человеку и статус отказа по словам `human`/`review`/`block` в имени (`orchestrator/helpers.py::_terminal_state_matching`, `_human_review_target_state`, `_rewind_budget_target_state`); `Subtasks` ни с одним словом не совпадает. Комментарий: что такое `Subtasks`.
3. **Ловушка контрактов этапов.** `agent.stage_contracts: auto` включает контракты, только если все активные статусы из стандартного набора (`workflow/presets.py::board_uses_shipped_contracts` → `board_uses_default_contracts`, `_CONTRACT_LANES`). С `Plan` контракты **тихо выключились бы**. Поставить `stage_contracts: on` с комментарием «почему не auto». Проверить по коду (`orchestrator/contracts.py::evaluate_contract`, `_evaluate_lane_contract`, `contract_producing_states`; `presets.py::guess_lane_preset` для нового набора статусов): при `on` у `Plan` контракта нет («stages outside the enforcement set pass through») или он проходит, а у `Todo`/`In Progress`/`Verify`/`Document` набор контрактов **тот же, что был** (какой `preset` ядро выберет для нового набора — по умолчанию или иной; если иной — остановиться и описать в PR). Если ядро при `on` требует что-то для `Plan` — остановиться и описать в PR, ядро не чинить.
4. `agent.dependency_resolving_states: [Done]` — рядом с `preserve_workspace_states`, с комментарием: зависимость `blocked_by` выполнена только в `Done`; без ключа `Subtasks` считался бы выполненным (S4-17). Для прежних статусов поведение не меняется — доказать тестом.
5. `agent.preserve_workspace_states: ["Human Review", "needs_human", "Subtasks"]`: рабочая копия карточки-задачи нужна на итоговой проверке (S4-21); без записи её снимут уборка при переходе в конечный статус и `_startup_terminal_cleanup` при перезапуске службы (как S4-15 для `needs_human`).
6. `prompts.stages`: строка `Plan: {{yaml:prompt_plan}}` (между `Todo` и `"In Progress"`).
7. **Автоархив** — только строка в комментарии и пункт в «Рисках» PR. `tracker.archive_after_days` не задан, умолчание ядра 30 дней (`utils/archive.py`, `orchestrator/sweeps.py::_archive_sweep`): конечная карточка без правок 30 дней уходит в `Archive`; правка карточки сбрасывает отсчёт. Умолчание **не менять** (решение архитектора). Лечение — в S4-19/S4-21: `integrate.py` будет дописывать в карточку-задачу раздел `## Subtask Progress` при `Done` каждой подзадачи.

## Часть 2. `ours/hooks/project_config.py` (и `project-new`, если затронут)

1. `template_values`: добавить `"plan"` в перечень этапов, для которых строится `prompt_<этап>` (сейчас `("todo", "in-progress", "verify", "document", "done")`).
2. **Две разные величины вместо одной `ACTIVE_STATE_COUNT`.** Поправка к пункту архитектора «`ACTIVE_STATE_COUNT` остаётся 4»: ядро требует `agent.max_turns` (`workflow/preflight.py` ≈ стр. 26) и `max_turns` каждого уровня (`workflow/levels.py::parse_levels`, `required_turns = len(active_states)`) **не меньше числа активных статусов**, а их теперь 5. Если оставить нижнюю границу 4, `project.toml` с `max_turns = 4` наш загрузчик пропустит, а ядро откажет. Поэтому:
   - `STAGES_ON_LONGEST_PATH = 4` — этапов на самом длинном пути карточки (обычная: Todo, In Progress, Verify, Document; работник — 3: Todo, In Progress, Verify; руководитель — Plan, затем Verify, Document). Используется в правиле `max_turns ≥ 4 × (1 + max_attempts)` (`_check_turns_cover_attempts`): докстринг и текст ошибки — «этапов на самом длинном пути» вместо «число активных состояний»;
   - нижняя граница `max_turns` (две проверки, ≈ стр. 243 и 345) — **число активных статусов шаблона** (5), лучше вычислить из шаблона или держать константой с тестом, что она равна длине `active_states` отрисованного `WORKFLOW.md`; текст ошибки — «не меньше числа активных статусов доски (N); иначе ядро не примет WORKFLOW.md».
   - Числа доски `oms` (`max_turns = 12`, `max_attempts = 2`) по-прежнему проходят — закрепить тестом. Если на доске `oms` у какого-либо уровня `max_turns < 5` (прочитай `~/symphony-projects/oms/project.toml`, **только читать**), — остановиться и написать в PR: после слияния доска перестанет загружаться.
3. `ours/bin/project-new`: комментарии про «4 этапа × (1 + max_attempts)» оставить по смыслу («этапов на самом длинном пути»); поправить, только если стало неверно. Найди по коду, перечисляет ли что-либо статусы доски (`grep -n "Human Review\|needs_human\|terminal\|In Progress" ours/bin/project-new ours/bin/project-doctor ours/bin/project-render ours/hooks/project_config.py`), поправь только ставшее неверным; если нечего — так и напиши в PR. Строк `[plan]` в `project.toml` здесь **нет** (S4-19). `card-template.md` не менять.

## Часть 3. Промпты

1. **`ours/prompts/stages/plan.md` — заглушка** (настоящий текст — S4-20, карточкой на доске `oms`): по-английски, коротко: стадия нарезки ещё не настроена (S4-20); код не править, карточки не создавать, ничего не запускать; дописать `## Blocker` одной строкой `Plan stage is not configured yet (S4-20)`; поставить статус `needs_human`; остановиться.
2. **`ours/prompts/stages/todo.md` — ветка по метке** (запасной путь; основной — карточку-задачу создают сразу в `Plan`: `board new --state Plan --label role:manager-plan --level …`). После шага 3:
   ```
   {% for label in issue.labels %}{% if label == "role:manager-plan" %}
   Manager card (`role:manager-plan`): this replaces step 3. Do not route to `In Progress`; append the one-line `## Triage` and set state to `Plan`.
   {% endif %}{% endfor %}
   ```
   Формулировку можно уточнить, но фраза «this replaces step 3» (ветка перекрывает шаг 3) обязательна. Шаг 2 (неясная карточка → `Blocked`) действует и для руководителя.
3. **`ours/prompts/base.md`**: после строки `Labels:` — строка (условие по истинности шаблонизатор ядра понимает; `issue.request` ядро отдаёт, `issue.py::to_template_dict`, пустая строка без поля):
   ```
   {% if issue.request %}Request: {{ issue.request }}{% endif %}
   ```
   Схему пути в `base.md` дополнить одной строкой: `manager (role:manager-plan): Plan -> Subtasks -> Verify -> Document -> Human Review`. Больше в `base.md` ничего.
4. `ours/prompts/CHANGES.md`: строки по каждой правке и абзац «Почему» (статус `Plan` — чтобы у руководителя был свой промпт без текста работника; `Request:` — подзадача определяется меткой `role:worker` **и** полем `request:`, второе агент раньше не видел, ветка Verify «подзадача — сразу в Human Review» могла не сработать).

## Часть 4. Что ядро делает с новыми статусами (обязательная проверка по коду)

Ниже — заметки по коду на `our-main` = `3f6963a` (архитектор их перепроверил). **Пройди по каждому месту сам**, подтверди или поправь, результат — сжатой таблицей в DECISIONS с пометками «проверено по коду» / «расхождение: …». Если найдёшь место, где `Plan` или `Subtasks` ломают работу, — остановись и опиши в PR, ядро не чини.

| Место | Что делает | Для `Plan` / `Subtasks` |
|---|---|---|
| `orchestrator/dispatch.py::_eligibility_contract_decision` | конечный или не активный статус → `REJECT inactive` | `Plan` берётся в работу; `Subtasks` — нет |
| `orchestrator/attempt.py` ≈ 1079 | контракт на входе в конечный статус — только для успешного (`Done`) | вход в `Subtasks` и в `needs_human` (заглушка) — без контракта, как в `Human Review` |
| `workflow/presets.py::board_uses_shipped_contracts`, `config.py::stage_contracts_enabled` | `auto` включает контракты только для стандартного набора статусов | с `Plan` нужен `on` (часть 1, п. 3) |
| `orchestrator/contracts.py::evaluate_contract` | статусы вне набора контрактов проходят без проверки | у `Plan` контракта нет; у остальных — прежний набор |
| `orchestrator/worker_exit.py` ≈ 695–725 | `is_terminal` → продолжение не планируется | ход, закончившийся в `Subtasks`, завершается штатно |
| `orchestrator/worker_exit.py` ≈ 775 | снимок `auto_commit_on_done` только для `done`/`human review` | для `Subtasks` коммит делает штатный блок `after_run` (`wip: …`) — убедись |
| `orchestrator/retry_reconcile.py`: путь `reconcile_terminate_terminal` и защита долгого `after_run` (K09, `after_run_started_at`) | что происходит, когда карточка ушла в конечный статус во время хода | **подтвердить**, что ход, закончившийся в `Subtasks`, проходит `after_run` так же, как ход в `Human Review` (в этом `after_run` будет жить хук S4-19) |
| `orchestrator/retry_reconcile.py` ≈ 1076 (`_reconcile_running`), ≈ 1238 (`_startup_terminal_cleanup`) | рабочую копию конечной карточки убирают, кроме `Done`, `Blocked`, `Human Review` и `preserve_workspace_states` | нужен `Subtasks` в `preserve_workspace_states` |
| `orchestrator/sweeps.py::_archive_sweep`, `utils/archive.py` | конечные карточки без правок 30 дней → `Archive` | ловушка (часть 1, п. 7) |
| `orchestrator/core_support.py::_successful_blocker_terminal_states` | какие статусы блокера «выполнены» | `[Done]` |
| `orchestrator/helpers.py` (`_terminal_state_matching`, `_human_review_target_state`, `_rewind_budget_target_state`), `release_cycle.py` | выбор статуса по ключевым словам в имени | `Subtasks` не совпадает; `Human Review` и `Blocked` остаются |
| `workflow/preflight.py`, `workflow/levels.py::parse_levels` | `max_turns` (общий и уровня) ≥ числа активных статусов | теперь ≥ 5 (часть 2, п. 2) |
| `cli/board.py` ≈ 201 | `board new` без `--state` → `active_states[0]` | остаётся `Todo` |
| `webapi/*`, `tui/*`, `progress_md.py`, `pm/*`, `trackers/file.py` | колонки и сводки | новые столбцы `Plan` и `Subtasks`; в сводке группы `request` `dependency_state_resolved("Subtasks")` ложно; `board mv X Plan` / `Subtasks` принимается |
| `ours/hooks/check.py`, `integrate.py` | хуки `after_run` | `check.py hook` работает только для `Done`/`Human Review`, `integrate.py hook` — только для `Human Review`: карточку в `Plan`/`Subtasks` молча пропускают — убедись |

## Тесты

Новый файл `ours/tests/test_planning_states.py` (образцы — `test_limits_preserve.py`, `test_light_path_e2e.py`: отрисовка через `project-new`, загрузка ядром, промпты — движком ядра):

- ядро принимает отрисованный `WORKFLOW.md`; `active_states == ("Todo", "Plan", "In Progress", "Verify", "Document")`; `Subtasks` в `terminal_states` после `needs_human`; `prompts.stages` содержит `Plan` → `ours/prompts/stages/plan.md`;
- контракты включены: `stage_contracts_enabled(active_states)` истинно; `evaluate_contract("Plan", …)` проходит без требований; для `Todo`/`In Progress`/`Verify`/`Document` выбранный набор контрактов (`contract_producing_states` для того `preset`, что выберет ядро) тот же, что для старого набора статусов;
- `dependency_resolving_states == ("done",)`; `_blocker_dependency_is_resolved`: `Done` → истина; `Subtasks`, `Plan`, `Human Review`, `needs_human`, `Blocked`, `Archive` → ложь; **без** ключа (копия конфигурации с `()`) для прежних статусов — то же, а `Subtasks` — истина (зачем ключ);
- `_human_review_target_state(cfg) == "Human Review"`, `_rewind_budget_target_state(cfg) == "Blocked"`;
- диспетчер: карточка в `Plan` берётся (`_eligibility_contract_decision` → нет отказа), в `Subtasks` — `REJECT`; карточка с `blocked_by` на карточку в `Subtasks` — `waiting_dependency`, после `Done` блокера — готова;
- `board new` без `--state` в отрисованной доске даёт `Todo`; `board mv <ID> Plan` и `Subtasks` принимаются;
- настоящая `_startup_terminal_cleanup` (как в `test_limits_preserve.py`): рабочая копия карточки в `Subtasks` остаётся;
- промпты: `todo.md` с меткой `role:manager-plan` содержит ветку про `Plan`, без метки — нет; `base.md` с `request` — строка `Request: <ЗАДАЧА>`, без — нет; `plan.md` отрисовывается и содержит `needs_human`;
- `project_config`: `project.toml` с числами доски `oms` (`max_turns = 12`, `max_attempts = 2`) проходит; `max_turns = 4` — понятный отказ с числом 5 (до правки проходил бы); у уровня `max_turns = 4` — тоже отказ; нижняя граница равна длине `active_states` отрисованного шаблона.

Правки существующих тестов — **только утверждения о содержимом шаблона и тексты ошибок** (`test_project_kit.py`: кортежи `terminal_states`, `active_states`, `preserve_workspace_states`, `stage_contracts`; `test_limits_preserve.py`, `test_levels_kit.py`, `test_project_doctor.py` — по факту падения). Каждую назвать в PR с причиной (как в S4-15). Если упал тест, где дело не в тексте шаблона, — остановиться.

## Документы (последний шаг, в тот же PR)

- `docs/our/DECISIONS.md`, блок «Текущее состояние»: дата; сделано — S4-18 (статусы `Plan` и `Subtasks`); в работе — ничего (PR ждёт слияния); следующее — S4-19 хук «план-файл → карточки» (ошибка плана возвращает карточку в `Plan` с `## Plan Failure`; «план сдан» = метка `role:manager-plan` + статус `Subtasks` + нет `## Plan Accepted`), S4-20 промпт `plan.md` карточкой на доске `oms` (заменяет заглушку), S4-21 выход из `Subtasks`, S4-22 проверка боем.
- Запись «2026-10-27 — Решение архитектора: статус `Plan`»: причина (промпт по статусу; шаблонизатор не убирает текст работника), путь карточки-задачи, основной и запасной путь в `Plan`, ловушка `stage_contracts: auto`, шаг 8 (`role:manager-diagnose`) — тот же приём, если понадобятся свои инструкции (сейчас не делать).
- Запись «2026-10-27 — S4-18 — статусы нарезки `Plan` и `Subtasks`»: что добавлено и где; таблица части 4; две величины вместо `ACTIVE_STATE_COUNT` и почему (ядро требует `max_turns ≥ 5`); ловушка автоархива и её лечение (S4-19/S4-21, умолчание 30 дней не меняем); `Request:` и зачем; правки существующих тестов. «Не проверено: в работающей службе и на доске `oms`». «Модель: Sonnet, effort=high». Пунктом — **что делает чат этапа после слияния**: `post-merge` пересоберёт `WORKFLOW.md` доски `oms` (изменились шаблон и промпты); проверить `symphony doctor`, столбцы `Plan` и `Subtasks` в веб-доске, что прежние карточки на месте; `project.toml` и карточки `oms` не меняются.
- `docs/our/ROADMAP.md` (этап 4): отметить S4-18, заголовок пункта — «статусы нарезки `Plan` и `Subtasks`».
- `docs/our/GLOSSARY.md`: термины `Plan` (рабочий статус руководителя: нарезка задачи на подзадачи) и `Subtasks` (карточка-задача ждёт подзадачи; конечный для ядра, но не выполняет `blocked_by`).
- `ours/README.md`: если там перечислены статусы доски или правило «4 этапа» — поправить; иначе не трогать. `ours/hooks/README.md` — только ставшее неверным.

## Проверка (в описание PR: команды и результат)

```
.venv/bin/python -m ruff check ours
env -i HOME="$HOME" PATH=/usr/bin:/bin:/usr/sbin:/sbin .venv/bin/python -m pytest ours/tests -q
```

Если в рабочей копии нет `.venv`, создай (`python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`), путь `symphony.__file__` должен вести в эту рабочую копию. Полный `pytest -q` по `src`/`tests` не нужен (ядро не менялось). Службу `oms`, очередь, `launchctl`, `claude`, `osascript` не запускай; доску `oms` только читать (п. 2.2), не менять.

## PR

```
git push -u origin dev/S4-18
gh pr create --repo Serg10011/oh-my-symphony --base our-main --head dev/S4-18
```

Описание PR на русском: что сделано, как проверено (команды и результат), правки существующих тестов (список с причинами), расхождения с таблицей части 4, что не сделано, риски. Риски обязательно: после слияния `post-merge` пересоберёт `WORKFLOW.md` доски `oms` с новыми статусами и `stage_contracts: on`; нижняя граница `max_turns` поднялась до 5; ловушка автоархива (лечение в S4-19/S4-21); поведение службы с `Plan`/`Subtasks` не проверено до S4-22; заглушка `plan.md` уводит карточку в `needs_human` до S4-20. PR не сливай. В конце — ссылка на PR. Неясное — остановиться и спросить Сергея в окне.
