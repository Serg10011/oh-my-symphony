# REPORT-09 — папка доски (WORKFLOW.md + kanban) вне репозитория кода

**Задача:** `docs/our/tasks/TASK-K06-external-workflow-research.md`. Код в `src/` и `scripts/` не менялся.
**База:** `our-main` на коммите `e8f1fcc`. Ссылки `файл:строка` относятся к нему. Python 3.13.17, git 2.43.0.
**Пометки:** **[запуск]** — проверено запуском на временных репозиториях в облаке; **[код]** — по чтению кода.

**Термины.** *Папка доски* — `~/symphony-projects/<имя>/`: в ней `WORKFLOW.md` и `kanban/`, для ядра это `SYMPHONY_WORKFLOW_DIR`. *Репозиторий кода* — внешний git-репозиторий проекта. *Рабочая копия* (worktree) — отдельная папка карточки, привязанная к репозиторию кода, на ветке `symphony/<ID>`.

## Коротко

Схема реализуема **без правок ядра** при нашей конфигурации: автослияние, пуш, автолечение и preview выключены. Нужны два своих хука: `after_create` (≈50–60 строк bash) и `before_remove` (≈15 строк). Прототип прошёл полный цикл на настоящей службе с мок-бэкендом: создание рабочей копии от явной базы, коммит при Done, удаление, чистый старт, перезапуск службы, уборка на старте, две службы параллельно **[запуск]**. Что теряется: в веб-доске пропадают список веток, diff и лог; `doctor` выдаёт одно предупреждение (WARN); не работают пути автослияния и спасения истории (у нас они и так выключены).

## Как проверяла [запуск]

Стенд: репозиторий кода `code/` (ветки `main` и `dev`), папка доски `proj/` без git, в ней `WORKFLOW.md`, `kanban/`, `project.env` (путь к коду и база `dev`), `setup.sh` и прототипы хуков. Бэкенд — встроенный мок `python -m symphony.mock_codex`. Агента изображает `after_run`: пишет файл и переводит карточку в Done. Настройки: `auto_merge_on_done: false`, `auto_commit_on_done: true`, `auto_recover_blocked: false`, `stage_contracts: "off"`. Прогоны:

1. Штатный `after_create` на папке доски без git.
2. TASK-1: создание, Done, коммит, удаление.
3. TASK-1 повторно: проверка чистого старта.
4. TASK-2 и TASK-3 → `needs_human`, затем перезапуск службы (уборка на старте).
5. TASK-4: перезапуск при живой рабочей папке.
6. Вторая папка доски на порту 9972 с тем же репозиторием кода, одновременно с первой.
7. Запасной вариант `.pipeline/` на штатных скриптах.

## Q1. Где ядро и скрипты считают папку WORKFLOW.md репозиторием кода

| # | Место | Что будет при внешнем репозитории | Исправление, строк |
|---|---|---|---|
| 1 | `scripts/symphony-setup-worktree.sh:22,28-31` — `HOST_REPO=$SYMPHONY_WORKFLOW_DIR`, `cd`, `rev-parse --git-common-dir` | Папка доски без git: `fatal: not a git repository`, выход 1, карточка не стартует. Попутно скрипт оставляет `symphony-worktree.lock` в папке доски (`:30,59`) **[запуск]**. Если подставить в переменную путь к коду: скрипт отработает, но доска не подключится (ищется `код/kanban`, `:211-212`), база возьмётся с HEAD основной копии (`:31-33`), а `.venv` запишется в `.git/info/exclude` кода (`:248-250`) **[запуск]**. Старая ветка переиспользуется (`:161-162`), `pip install -e '.[dev,browser]'` (`:254`) — под наш проект | Свой скрипт (Q5), ≈50–60 |
| 2 | `src/symphony/_worktree_cleanup.py:44` (`host = SYMPHONY_WORKFLOW_DIR`), `:21-24`, `:122-124`, `:149-156` | Папка доски без git: `CalledProcessError` на `git worktree list`. Если host = код: `cleanup board link does not target the host board`, потому что ссылка ведёт в папку доски **[запуск]** | Наш `before_remove`: убрать ссылку `kanban`, затем вызвать модуль с `SYMPHONY_WORKFLOW_DIR=<код>`. Работает и для `--prepare-setup` **[запуск]**. 0 строк ядра |
| 3 | `src/symphony/workspace.py:1135` `commit_workspace_on_done` и снимки | Работают только внутри рабочей копии и берут `symphony.basesha` (`:1210`). Коммит `TASK-1: …` лёг на `symphony/TASK-1` поверх `dev`, `kanban` в коммит не попал **[запуск]**. Защита `WORKFLOW.md` от удаления (`:1273-1275`) теряет смысл: этого файла в репозитории кода нет | Ничего |
| 3а | Артефакты: `orchestrator/sweeps.py:331` пишет `/.symphony-artifacts/` в `.git/info/exclude` папки доски | Папка без git: предупреждение `artifact_dir_git_exclude_skipped` **[запуск]**. `finalize_delivery_history` (`workspace.py:1443`) и уборка на старте (`retry_reconcile.py:1162,1266`) не передают `extra_excludes`, поэтому артефакты агента могут попасть в коммит **[код]** | Строка `/.symphony-artifacts/` в нашем файле исключений (Q5), 1 |
| 4 | `workspace.py:1425` `finalize_delivery_history` (вызов `worker_exit.py:777`) | Работает в рабочей копии. Пуш только при `auto_merge_push_target` (у нас `false`) **[код]** | Ничего |
| 5 | «Хостовое спасение» истории: `blocked_recovery.py:813,840` → `verify_branch_history(workflow_dir)` (`workspace.py:1576`) | В папке доски ветки нет, спасение молча отказывается. У нас выключено: `auto_recover_blocked: false` → ранний выход `blocked_recovery.py:1065` **[код]** | Не нужно. В ядре — 1 строка плюс ключ (см. Q6) |
| 6 | Ворота и автослияние: `blocked_recovery.py:567-576` → `utils/auto_merge.py:103` (`cwd=workflow_dir`), `:260`; веб `webapi/git_routes.py:270` | Папка без git: `FAIL: not a git repo`, каждая Done → `Blocked`. Автослияние у нас выключено (`:565`), шлюз Done работает в рабочей копии **[код]** | Не нужно. `auto_merge_exclude_paths` (`auto_merge.py:274-298`) действует только здесь, и `kanban` в репозитории кода нет |
| 7 | Старт службы: `orchestrator/core.py:592` (`ensure_workflow_repo_is_safe`), `:600` (экспорт), `:625` (артефакты); уборка `retry_reconcile.py:1110` | Защита «не запускаться на исходниках symphony» (`runtime_safety.py:53`) проверяет папку доски, а не код. Сравнение `_branch_already_merged_into_target(workflow_dir)` (`:1199-1207`, `helpers.py:132`) делается только при автослиянии. Снимок и удаление `needs_human` на старте работают (`:1252-1266`) **[запуск]** | Ничего |
| 8 | Повторное использование папки: `workspace.py:554-610`, `:168` (`_linked_worktree_refresh_plan`) | `git symbolic-ref` в папке доски возвращает пусто, план пустой, перемотка пропускается. Перезапуск при живой папке TASK-4 прошёл без ошибок, `after_create` повторно не запускался **[запуск]**. Теряется только перемотка уже слитой ветки (нужна при автослиянии) | Ничего |
| 9 | `symphony.basesha/basebranch/mergetargetbranch` (`setup-worktree.sh:174-177`) | Ядро читает их только в рабочей копии (`workspace.py:1210-1211`). Наш хук пишет их так же **[запуск]** | Ничего |
| 10 | `feature_base_branch`: `orchestrator/helpers.py:63-81` → `SYMPHONY_FEATURE_BASE_BRANCH` | Работает, наш хук может брать базу отсюда **[код]** | Ничего |
| 10а | Веб-доска: `webapi/git_routes.py:52-56` (список веток), `:68-453` (лог, ветки задач, compare, diff, merge, push), `webapi/context.py:29` | Список веток `{"branches": []}` **[запуск]**. Diff и лог в веб-доске пустые: в Human Review изменения придётся смотреть в репозитории кода. Сохранение политики веток проверяет только формат имени (`workflow_routes.py:124-149`) | Ядро: ≈15 строк плюс тесты (Q6), либо смотреть diff в репозитории кода |
| 11 | Preview: `product_preview.py:84-97` (`rev-parse --show-toplevel` в папке доски) | Ошибка при запуске preview **[код]**. Мы preview не используем | Ядро, если понадобится |
| 12 | `symphony doctor`: `cli/doctor.py:796` | WARN «not a git repository». `service start` блокирует только FAIL (`service.py:902-905`). Скрипты хуков ищутся относительно папки доски (`:543-562`); наши, вызванные через `$SYMPHONY_WORKFLOW_DIR/…`, читаются, `board.reachable` = PASS **[запуск]** | Ничего |
| 13 | Wiki sweep: `workflow/builder.py:963-980` (`wiki.root` относительно папки доски) | Git не трогает, но проверяет пустую папку: агент пишет wiki в ветке кода **[код]** | `wiki.sweep_every_n: 0` или абсолютный путь |
| 14 | Пути промптов: `workflow/builder.py:120-141` | Считаются от папки доски, git не нужен. Это даже удобно: общий шаблон лежит рядом с доской **[код]** | Ничего |
| 15 | Ссылка `kanban` и её исключение из git | Штатно — **`.git/info/exclude`**, не `.gitignore` (`setup-worktree.sh:228-230`), плюс `skip-worktree` для отслеживаемых карточек (`:213-226`). `info/exclude` один на все рабочие копии репозитория, то есть это правка внутри `.git` кода. `project create` тоже пишет туда (`projects.py:698-767`). Проверка ядра `_enforce_board_reachable` (`workspace.py:641`) требует, чтобы ссылка вела в `board_root`; наш вариант её проходит **[запуск]** | Наш вариант: `git config --worktree core.excludesFile <папка доски>/worktree-exclude`, `.git/info/exclude` кода не меняется **[запуск]** |
| 16 | Прочее: `factory-setup-worktree.sh:4-27`, `factory-remove-worktree.sh:8-9`, `continuous_improvement.py:459,526-570`, релизный цикл (`release_contracts.py:1539`) | То же допущение. Мы этого не используем **[код]** | — |
| 17 | Ловушка: если родительская папка (например `~`) — git-репозиторий, git в папке доски молча работает с ним (`utils/git_inspect.py:53-66`) **[код]** | Ядро получит чужой репозиторий | Наш хук проверяет, что `git -C <папка доски> rev-parse` падает |

Штатные хуки `before_run` и `after_run` из `WORKFLOW.md` (`:61-78`, `:87`) и `--add-dir "$SYMPHONY_WORKFLOW_DIR/kanban"` для `claude` продолжают работать: `kanban` лежит в папке доски **[код]**.

## Q2. Как передать хукам свои переменные

Ключа `hooks.env` нет: разбор `hooks` читает только сами хуки, `timeout_ms` и `fail_on_warning_patterns` (`workflow/builder.py:298-311`). Атрибут `self._hook_env` — это результат `_branch_hook_env(cfg)` (`orchestrator/helpers.py:63-81`). В нём ровно четыре ключа: `SYMPHONY_FEATURE_BASE_BRANCH`, `SYMPHONY_MERGE_TARGET_BRANCH`, `SYMPHONY_BOARD_ROOT`, `SYMPHONY_BOARD_ROOT_NAME`. Заполняется при старте (`core.py:611`) и при перечитывании `WORKFLOW.md` (`dispatch.py:89,98`). Окружение хука складывается в `workspace.py:877-895`: окружение службы, затем `SYMPHONY_WORKFLOW_DIR`, затем `_hook_env`, затем доп. переменные, затем `SYMPHONY_PYTHON` и `SYMPHONY_WORKSPACE_ROOT`.

Штатные способы без ядра:

- **(а) рекомендую:** хук сам читает `$SYMPHONY_WORKFLOW_DIR/project.toml` через `"$SYMPHONY_PYTHON"` (в 3.12+ есть `tomllib`). В прототипе так читался `project.env` **[запуск]**;
- (б) переменная прямо в тексте хука: `CODE_REPO=… bash "$SYMPHONY_WORKFLOW_DIR/hooks/after_create.sh"`;
- (в) переменная окружения службы при запуске, её наследуют все хуки;
- (г) базовая ветка — штатно через `agent.feature_base_branch`.

## Q3. `symphony project create` и внешний репозиторий

Не поддерживает **[код]**:

- у команды есть только флаги `--path`, `--id`, `--workflow` (путь внутри репозитория), `--host`, `--port` (`cli/project.py:146-160`);
- целевая папка становится git-репозиторием (`git init -b main`, `projects.py:1047-1052`);
- `WORKFLOW.md` и доска обязаны лежать внутри неё (`projects.py:492-501`, `:859-864`).

Папку доски создаём своим скриптом из общего шаблона, доску — `symphony board init`. Авторы такую схему допускают. Шаблон для монорепозитория создаёт рабочие копии из другого репозитория через `git -C` (`skills/symphony-skill/monorepo/references/workflow-template.md:33-58`; флаг `--dangerously-skip-permissions` оттуда не переносим). Комментарии в ядре прямо упоминают «worktree в другом репозитории» (`sweeps.py:140-147`, `workspace.py:1222-1227`).

## Q4. Несколько проектов одновременно

Одна служба = один `WORKFLOW.md`: `core.py:600` пишет `SYMPHONY_WORKFLOW_DIR` в окружение всего процесса. Хаб (`hub.py`, порт 8787) только ведёт список проектов, каждый проект работает своей службой. Состояние службы лежит в `<папка доски>/.symphony/`: база SQLite `state.db` (`run_registry_records.py:201`), `stats.jsonl`, артефакты, запись о службе (`service.py:156-176`). Общий файл один — `~/.symphony/projects.json`, реестр `project`. Две службы на портах 9971 и 9972 с одним репозиторием кода отработали параллельно, без конфликтов, ветки `symphony/TASK-1` и `symphony/PB-1` легли в один репозиторий **[запуск]**.

Что мешает и как закрыть:

- порт по умолчанию 9999 (`service.py:60`) — задавать `server.port` для каждого проекта;
- `workspace.root` по умолчанию общий `<tmp>/symphony_workspaces` (`builder.py:280-282`) — задавать для каждого проекта;
- одинаковые ID карточек в двух проектах над одним репозиторием кода дают одну ветку `symphony/<ID>`, а чистый старт может «запарковать» ветку чужого проекта — нужен свой префикс ID у каждого проекта;
- лимит агентов задаётся для каждой службы отдельно, общий потолок по лимитам подписки ведём сами.

## Q5. Свой `after_create` и `before_remove` (описание; прототип проверен запуском)

**`after_create`** (рабочая папка = `$PWD`, `ID=$(basename "$PWD")`):

1. Читает `project.toml` (путь к коду, база; база может прийти и из `SYMPHONY_FEATURE_BASE_BRANCH`). Проверяет: код — git-репозиторий; есть `refs/heads/<база>` (если база с сервера — сначала `git fetch`, решить заранее); папка доски не лежит внутри git (ловушка из п. 17).
2. Берёт блокировку `flock` на `<git-common-dir кода>/symphony-worktree.lock` — тот же файл, что у штатного скрипта.
3. Запускает `SYMPHONY_WORKFLOW_DIR=<код> "$SYMPHONY_PYTHON" -m symphony._worktree_cleanup "$PWD" --prepare-setup symphony/<ID>`. Это штатные проверки: ветка не выгружена в другой рабочей копии, папка пустая, устаревшая регистрация убирается.
4. **Чистый старт.** Если `symphony/<ID>` уже есть — `git branch -m symphony/<ID> attempts/<ID>-<UTC>`. Переименование лучше отказа: отказ превращается в повторы с ошибкой `after_create`, а переименование сохраняет историю для разбора. Проверено: при повторном запуске старая ветка ушла в `attempts/TASK-1-…`, новая создана от `dev` **[запуск]**. Оговорка: карточка, возвращённая в работу после удаления папки, тоже начнёт с нуля. Если нужно «продолжить», хук может читать признак из `kanban/<ID>.md`.
5. `git -C <код> worktree add "$PWD" -b symphony/<ID> refs/heads/<база>`.
6. В рабочей копии:
   - `git config extensions.worktreeConfig true` — одна строка в `.git/config` кода, это не файл в дереве;
   - `--worktree symphony.basesha` (HEAD), `symphony.basebranch` и `symphony.mergetargetbranch` (база);
   - `--worktree symphony.autocommitExclude kanban`;
   - `--worktree core.excludesFile <папка доски>/worktree-exclude` — постоянный файл со строками `/kanban`, `/.venv/`, `/.symphony-artifacts/`. Оговорка: в этой копии он заменяет глобальный `excludesFile` пользователя.
7. `ln -s <папка доски>/kanban kanban`, снимает блокировку, запускает `bash <папка доски>/setup.sh`. Если `setup.sh` упал, хук сам убирает рабочую копию и только что созданную пустую ветку, иначе ядро удалит лишь папку (`workspace.py:611-622`) **[код]**.

**`before_remove`:** проверяет, что `kanban` — ссылка именно в папку доски, удаляет только ссылку. Затем `cd <код>` и `SYMPHONY_WORKFLOW_DIR=<код> exec "$SYMPHONY_PYTHON" -m symphony._worktree_cleanup "$PWD"`, без `--restore-board-tracking`: карточек в репозитории кода нет. Ветку не трогает, ядро тоже. Если хук упадёт, ядро всё равно удалит папку (`workspace.py:735-738`), а оставшуюся регистрацию уберёт шаг 3 при следующем создании **[код]**.

**Итог прогонов [запуск]:** в репозитории кода нет новых файлов (`git status` пуст, `.git/info/exclude` не изменён). Изменения только внутри `.git`: `extensions.worktreeConfig`, ветки `symphony/*` и `attempts/*`, файл блокировки. Объём прототипа — 33 и 13 строк. Рабочая версия с разбором `project.toml`, сообщениями и откатом — ≈50–60 и ≈15 строк плюс тесты на временных репозиториях.

## Q6. Вердикт и сравнение

**Без правок ядра — да**, при условии: `auto_merge_on_done: false`, `auto_merge_push_target: false`, `auto_recover_blocked: false`, без preview и релизного цикла, `wiki.sweep_every_n: 0`. Чего не будет: веток, diff и лога в веб-доске; одно WARN в `doctor`; защита от запуска на исходниках symphony не видит репозиторий кода.

**Правки ядра по желанию** (если захотим diff в веб-доске или автослияние). Один ключ, например `workspace.code_repo`, и один помощник «git-папка проекта» со значением по умолчанию — папка `WORKFLOW.md`. Минимальный шаг — только веб `git_routes` и `context.py`: ≈15 строк плюс ≈40 строк тестов. Полный — ≈15 мест (автослияние ×4, сверка на старте, спасение истории, preview, doctor ×2, `runtime_safety`, артефакты, перемотка ×2, веб): ≈40–60 строк плюс ≈80–120 строк тестов.

**Запасной вариант `.pipeline/`** (`WORKFLOW.md` и `kanban` в `<код>/.pipeline/`, папка спрятана строкой в `.git/info/exclude`). Штатный `after_create` и `_worktree_cleanup` на нём работают как есть: git сам находит репозиторий выше папки, ссылка `kanban` ведёт в `.pipeline/kanban`, база `dev` **[запуск]**.

| | Внешняя папка доски | `.pipeline/` внутри кода |
|---|---|---|
| Проще | Свои 2 скрипта (≈70 строк) | Всё штатное, плюс diff и автослияние в веб-доске |
| Надёжнее | Основная копия кода не трогается: нет зависимости от её HEAD и от `checkout` при слиянии; `git clean -fdx` доску не сотрёт | Проверенный авторами путь. Но доска живёт в рабочей копии пользователя: её стирает `git clean -fdx`, её видят IDE и индексаторы, её можно случайно закоммитить, если пропадёт строка в `exclude` |
| Правила работодателя | В дереве кода ничего нашего. Остаются только внутренности `.git` и локальные ветки (пуш выключен) | Наши файлы физически лежат в рабочей копии, хоть и не в коммитах. Правило «никаких сторонних файлов в репозитории» это может нарушать |

**Рекомендация:** для проектов работодателя — внешняя папка доски со своими хуками; ядро пока не трогаем, Human Review смотрим в репозитории кода. `.pipeline/` — для своих проектов, где важен diff в веб-доске.

## Не проверено

- Реальные агенты `claude` и `codex` (только мок).
- Mac и Windows: стенд на Linux, на Mac `flock` может отсутствовать — нужна замена через `mkdir`, как в штатном скрипте `:69-90`.
- Откат при сбое `setup.sh`.
- Пути `reconcile` с живым воркером.

## Проверки

`python -m ruff check src tests` — без замечаний. `python -m pytest -q -n 4` — 3837 прошли, 136 пропущены, 2 упали: те же два теста окружения, что в базовом уровне (`test_pm_planning.py::test_stop_kills_term_resistant_group_after_leader_exits`, `test_pm_ui_node.py::test_pm_ui_node_regressions`).
