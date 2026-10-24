# REPORT-11 — комплект проекта: папка доски, хуки рабочей копии, шаблон WORKFLOW, промпты (TASK-K08a)

Задача: `docs/our/tasks/TASK-K08a-project-kit.md`. Ветка `ours/project-kit` от `our-main` (`3088bfe`, REPORT-10 на месте). Ядро (`src/`), `DECISIONS.md`, `ROADMAP.md` не менялись. Пометки: **[запуск]** — проверено запуском, **[код]** — по чтению кода.

## Коротко

Комплект готов и прошёл сквозной прогон на настоящей службе: `project-new` создаёт папку доски, наш `after_create` строит рабочую копию от базовой ветки, агент (поддельный `claude`, см. ниже) проводит карточку `KIT-1` через Todo → In Progress → Verify → Document до `Human Review`, `check.py hook` проверяет её, «Confirm done» проходит через `check.py gate` в `Done`, `before_remove` убирает рабочую копию. Репозиторий кода остаётся чистым, в ветке `symphony/KIT-1` нет `docs/KIT-1` и `docs/llm-wiki` **[запуск]**. Ядро не проверяет факт слияния на переходе Verify → Document, поэтому «Verify не вливает» обходом не является **[код]**.

## Что сделано

| Файл | Что |
|---|---|
| `ours/hooks/project_config.py` | разбор и проверка `project.toml` (неизвестный раздел/ключ — ошибка), раскладка папки доски, сборка `WORKFLOW.md` из шаблона |
| `ours/bin/project-new`, `ours/bin/project-render` | создание папки доски со всеми отказами из задания; пересборка `WORKFLOW.md` с печатью diff |
| `ours/hooks/worktree.py` | `create` (режим «обновить» и чистый старт с откатом) и `remove` |
| `ours/templates/WORKFLOW.md.tmpl` | все решения DECISIONS в одном месте; штатный блок `after_run` скопирован без изменений |
| `ours/hooks/check.py` | `project.toml` вместо `gate.toml`, `test.sh`, база из записи, блокировка на карточку; `gate.example.toml` удалён |
| `ours/prompts/` + `CHANGES.md` | копия промптов, 11 правок в трёх файлах |
| `ours/tests/test_project_kit.py`, `test_check.py`, `e2e/` | 25 новых тестов комплекта, 10 новых тестов `check.py`, сквозной прогон |

Объём без тестов: ≈730 строк кода без комментариев и строк документации (`worktree.py` 279, `project_config.py` 182, `project-new` 126, `project-render` 42, правка `check.py` ≈100), плюс шаблон 224 строки, из них ≈120 — скопированный штатный `after_run`.

## Проверка запуском

**Тесты [запуск].** `ours/tests` на временных git-репозиториях, с настоящими `symphony board init` и `_worktree_cleanup`: чистый старт (ветка от базы, настройки, запись базы, ссылки, `git status` пуст и в рабочей копии, и в репозитории кода, `.git/info/exclude` не тронут); переименование старой ветки в `attempts/<ID>-<UTC>`; режим «обновить» — ветка с коммитом агента не меняется, `setup.sh` не запускается, ссылки, `core.excludesFile` и запись базы восстановлены; ловушка «папка доски внутри git»; чужой префикс; сбой `setup.sh` с откатом (старая ветка, артефакты и запись базы вернулись, следующая попытка проходит); `docs/llm-wiki` уже в репозитории; `remove`. `project-new`: созданные файлы, разбор итогового `WORKFLOW.md` настоящим `build_service_config`, 12 отказов (каждый — без остатков на диске). `project-render`: «без изменений», diff при смене порта, отказ при плохом `project.toml`. `check.py`: 12 случаев плохого конфига или базы, база из записи переживает вливание ветки в целевую, «не предок HEAD», `test.sh` без login-оболочки, таймауты, блокировка в hook (пропуск) и в gate (ожидание и отказ).

**Сквозной прогон [запуск]** — `ours/tests/e2e/run.sh`. Мок-бэкенд ядра `symphony.mock_codex` говорит на протоколе codex и карточки не двигает, поэтому вместо него — поддельный `claude` (`e2e/fake_claude.py`): его находит `project-new` через `which`, шаблон остаётся нетронутым. За ход он делает работу одного этапа (код и новый тест, `docs/KIT-1/work|qa`, wiki, разделы карточки) и печатает кадры `stream-json`. Контракты этапов включены (`stage_contracts: auto`).

| Сценарий | Итог |
|---|---|
| обычный | 4 хода, `Human Review` за 4 с; `check.log`: `hook pass`; артефакты в `artifacts/KIT-1/{work,qa}` и `wiki/` папки доски; рабочая копия создана нашим хуком (вывод `after_create`, `setup.sh` отработал); `git status` кода пуст; в ветке только `src/feature.py` и `tests/test_feature.py` |
| затем «Confirm done» через `POST /api/v1/KIT-1/confirm-done` | `done_gate_passed`, `check.log`: `gate pass`, карточка `Done`, `before_remove` убрал рабочую копию, ветка осталась |
| агент ослабляет существующий тест | два отката в `In Progress` с `## Check Failure`, на третьем — `needs_human` |
| агент не пишет `docs/<ID>/work` | `## Contract Failure` ядра 4 раза, затем `Blocked` (`max_attempts`) — контракт видит артефакты через ссылку |

`symphony doctor` на собранном `WORKFLOW.md`: все PASS, кроме двух ожидаемых WARN (папка доски не git; см. находку 3) **[запуск]**.

## Находки и решения

1. **Ядро не проверяет слияние на Verify → Document [код].** Контракт Verify требует только непустые разделы, среди них `## Merge Status` (`contracts.py:151, 380`), и цитаты `qa/…`/`work/…`; ворота слияния работают только на Done и только при `auto_merge_on_done` (`blocked_recovery.py:565`). Уточнение к DECISIONS (K05, п. 1): в текущих штатных промптах Verify не вливает ни в одной ветке (`verify.md:21,26` — «you do NOT merge»), сливает оркестратор при Done, если `auto_merge_on_done: true`. Наша правка — явный запрет слияния и строка «отложено до человека»; от слияния, сделанного кем угодно, `check.py` теперь защищает база из записи (тест `test_base_from_record_survives_merge_into_target`).
2. **`symphony board init` всегда кладёт демо-карточку `DEMO-001` в Todo** (`cli/board.py:118`) — служба отдала бы её настоящему агенту. `project-new` её сразу удаляет **[запуск]**.
3. **Абсолютный путь к `claude` отключает автоматический `--add-dir` для git-папок** (`claude_code.py:48-66`: подстановка только для команды, начинающейся со слова `claude`); `doctor` пишет WARN `agent git grant`. Последствие: файловые инструменты агента не пишут в `.git` кода — агенту это и не нужно (коммитит хук). Добавлять `.git` в `--add-dir` сознательно не стала: это дало бы агенту правку `.git/config` и git-хуков. Работают ли из Bash-инструмента `claude` записи в `.git` (нужны `git merge-tree --write-tree` в Verify) — **не проверено**, проверить на Mac первой пробой.
4. **Два ключа таймаута тестов** в схеме: `[checks] test_timeout_s` и `[timeouts] tests_s`. Решение: первый, если задан, важнее; иначе второй (умолчание 900). Шлюз ждёт занятую блокировку не дольше `gate_ms/1000 − таймаут тестов − 30 с` — при умолчаниях 30 с; если человек нажмёт Confirm done, пока идёт хук, шлюз откажет и карточка уйдёт в `Blocked` с понятной причиной.
5. **Сверх задания:** при чистом старте старые артефакты `artifacts/<ID>` переносятся в `artifacts/.attempts/<ID>-<UTC>` (иначе старые `work/` прошли бы контракт новой попытки); `setup.sh` ограничен `hooks_ms − 30 с` и убивается группой, чтобы откат успел до таймаута ядра; из `before_run` убран `git fetch origin` (база локальная, сеть не нужна); `symphony.autocommitExclude` — три значения (`kanban`, `docs/<ID>`, `docs/llm-wiki`); в `after_create` добавлена строка-комментарий с `${SYMPHONY_BOARD_ROOT_NAME:-kanban}`, иначе `doctor` выдаёт ложный WARN `board.reachable`.
6. **Цель для `git merge-tree` в Verify:** штатный текст велит запускать его «из host repo», а host у нас — папка доски без git; целевую ветку ядро в промпт не передаёт (`prompt.py:508`). Промпт велит брать её из `git config symphony.mergetargetbranch` в рабочей копии.
7. Неуказанные в DECISIONS ключи (`max_turns`, `max_attempts`, `reuse_policy`, архивация) оставлены по умолчанию ядра.

## Не проверено

- Настоящий `claude` (только поддельный) и Mac: `fcntl.flock` на macOS есть (стандартная библиотека), но запуском не проверялось.
- Режим «обновить» через настоящее ядро: ядро не вызывает `after_create` повторно на живой папке (REPORT-09, Q1, п. 8), так что режим проверен только тестами — это страховка.
- Таймаут `setup.sh` и смерть хука по `hooks.timeout_ms` во время `setup.sh`.
- Две карточки одновременно (`max_concurrent_agents: 1`).

## Тесты

| Набор | До | После |
|---|---|---|
| `python -m pytest ours/tests -q` | 107 прошли | 142 прошли [запуск] |
| `python -m pytest -q -n 4` | 3878 / 136 / 2 | 3878 прошли, 136 пропущены, 2 упали [запуск] |

Два падения — известные, из окружения (`test_pm_planning::test_stop_kills_term_resistant_group_after_leader_exits`, `test_pm_ui_node::test_pm_ui_node_regressions`). В первом прогоне третьим упал `test_pm_performance::test_reference_1000_task_source_parse_stays_below_event_loop_budget` — замер времени, а я параллельно гоняла `ours/tests`; в одиночку 3 из 3 прошли, повторный полный прогон без нагрузки — ровно 3878/136/2. `ours/tests` в основной набор не входит. `python -m ruff check src tests ours` — без замечаний; `symphony-pyright` (отдельно от pytest) — 0 ошибок [запуск].

## Расхождения с заданием

- Вместо `symphony.mock_codex` — поддельный `claude` (причина в «Проверка запуском»).
- Ключ `--config` у `check.py` убран вместе с `gate.toml`: конфиг всегда `<папка доски>/project.toml`.
- Сообщения `check.py` оставлены на английском (их читают агент в `## Check Failure` и существующие тесты); новые команды и `project.toml` говорят по-русски.
