# Принятые решения

Журнал решений по конвейеру. Новые записи — сверху. Если спецификация (`ARCHITECTURE.md`) расходится с этим файлом, верен этот файл.

## Текущее состояние (обновляется каждой сессией)

- **Дата:** 29.10.2026.
- **Кредит:** облачный кредит израсходован, ведение учёта закончено 25.10.2026 (таблица ниже — история). Разработка — локальным Claude Code на подписке Max, в отдельной рабочей копии.
- **Сделано последним:**
  - **S4-29** — сессии очереди запускаются с `--remote-control <код>` и просьбой прислать push по итогу или с вопросом; Remote Control в окружении Терминала доступен (запрещающих переменных нет).
  - **E4b** — правила агента конвейера вынесены в `docs/agents/pipeline.md` (≈ 2,9 КБ вместо 12,6 КБ `CLAUDE.md`); файлы из `project_rules` / `manager_rules` и вложенные `CLAUDE.md` / `AGENTS.md` закрыты для карточек; у доски `oms` стоит `project_rules`.
  - **E4** — изоляция агента, одинаковая в обоих режимах оплаты: `api` — `--bare`, `plan` — `--setting-sources local` без MCP и навыков; файл настроек доски `claude-settings.json` теперь всегда (запреты `Edit` на `protected_dirs`, `.claude/`, `.mcp.json`, `CLAUDE.md`, `AGENTS.md`; таймаут Bash не меньше прогона `test.sh`); ключи `[agent] protected_dirs` / `project_rules` / `manager_rules`; те же четыре файла в запретах `check.py`. Подробности и выводы по документации — запись «E4» ниже; решения архитектора 29.10 (цены, таблица уровней, лестница) — отдельная запись ниже.
  - **E3b** — обёртки `bin/lint` и `bin/typecheck` в папке доски EVE, проверка типов в `test.sh`; запись «E3b» в записи «E3».
  - **E3** — доска EVE подключена (клон `~/dev/eve-pipeline`, доска `~/symphony-projects/eve/`, порт 9973, замер базы 3519 / 25 / 0); служба **не запущена**, оплата пока `plan`.
- **Раньше сделано (коды, подробности — в записях ниже):** E2, E1b, E1a; S4-27, S4-25, S4-26, S4-24, S4-22, S4-21, S4-20, S4-19, S4-18, S4-17, S4-16, S4-15, S4-14, S4-13, S4-12, S4-11, S4-10, S4-09, S4-08, S4-07, S4-06, S4-05, S4-04, S4-03, S4-02, S4-01; S4-23 (очередь закрывает окно после слияния); этап 3 (K14 принята, K13, K12, K11, K10, K09, K08); этап 1; правки ядра K01–K04, K07; `check.py` (K05).
- **В работе:** ничего (PR E4b влит; PR S4-29 ждёт слияния по «добро» Сергея; E4 влита). После слияния E4/E4b `ours/bin/post-merge` пересоберёт `WORKFLOW.md` доски `oms` (промпты со строкой правил), создаст её `claude-settings.json` и запустит `project-doctor oms`; доску `eve` (она вне `post-merge`) пересобирает чат этапа вручную: `project-render eve`, `project-doctor eve`. Если PR E4b не сольют, ключ `project_rules` в `project.toml` доски `oms` нужно убрать.
- **Следующее:** дымовая карточка на доске `oms` (проверка изоляции E4 в режиме подписки) → E3c (доска EVE к пробе: правила проекта, запрет `docs/**`, новая таблица уровней) → проба EVE после 30.10 (потолок трат в Console → ключ в `~/.symphony-keys/eve.key` → чат этапа переключает `[billing]` на `api`, `project-render`, `project-doctor`, `launchctl bootstrap`; порядок и уровни карточек — решения архитектора 29.10) → S4-28 (повторная проверка при сдвинувшемся `our-main` или лёгком пути: часть Б шага 7) → дальше по порядку: PR ядра (сквозная нумерация карточек, прокрутка доски) → `[plan] max_subtask_level` + заготовка `project-new` → статус `Merging` → `.clean` → правило лестницы (после пробы EVE, часть 1) → шаги 8–9.
- **Подписки и конфигурация:** Claude Max 5x (с 23.10.2026); ChatGPT Plus — временно, до переезда интеграции EVE на собственный сервер. Действует конфигурация B из раздела 17 спецификации (Claude Max); пока Plus активен, Codex — дополнительный исполнитель.

## Расход облачного кредита

Строки добавляются по мере завершения задач (правило из `CLAUDE.md`). Стоимость — из итогов сессии; где прямой записи нет, это отмечено.

| Задача | Стоимость | Остаток |
|---|---|---|
| K01 + K02 (Opus) | ≈ $7 (K01 ≈ $1, K02 ≈ $6) | $124 |
| K03 (Sonnet) | ≈ $2 | $122 |
| K04 | ≈ $1 | $121 |
| K05 + K06 + K07 (Opus) | ≈ $27 вместе | $94 |
| K09 | ≈ $5 (по разнице остатков, прямой записи нет) | $89 |
| K08a (Opus) | ≈ $10 | $79 |
| PR #13 (блок состояния, правила) | ≈ $1 | $78 |
| K08b + K10 (Opus + Sonnet) | ≈ $9 вместе | $69 |
| K11 (Sonnet) | ≈ $1 | $68 |
| Чат EVE (вне конвейера) | ≈ $9 | $59 |
| K12 + K13 + сессии по EVE (вне конвейера), разделить нельзя | ≈ $27 вместе | $32 |
| K14 (Sonnet) | ≈ $7 | $25 |
| Сессии по EVE (вне конвейера) | ≈ $24 | ≈ $1 |

## Решения

### 2026-10-29 — S4-29 — Remote Control для сессий очереди

Модель: Sonnet (effort=medium). Основа: `docs/our/tasks/TASK-S4-29.md`. Ядро (`src/`, `tests/`) не менялось.

**Решение архитектора.** Сергею нужно следить за задачами и отвечать на вопросы с телефона. Вместо отдельной задачи с хуком `Stop` (скрипт, который сам слал бы уведомление) — два маленьких изменения в очереди: сессия получает имя, а Claude Code сам решает, когда прислать push (в `/config` у Сергея push уже включены).

**Что сделано** (`ours/hooks/inbox_runner.py`):
- `terminal_command` добавляет `--remote-control <код>` перед текстом задания, после `--model` / `--effort`. Имя пишется явно: у флага оно необязательное, и без него именем стал бы текст задания.
- `TASK_PROMPT` просит прислать push-уведомление с итогом или вопросом, если инструмент доступен. После пути в тексте стоит пробел, а не точка: поиск процесса задачи (закрытие окна после `.merge`, S4-23) не принимает точку сразу после пути. Опасных в двойных кавычках символов (`"`, `$`, обратная кавычка, `\`, `!`) в тексте нет — это проверяют утверждение в коде и тест.
- Тесты: два существующих теста с точным совпадением команды (`test_terminal_command_exact`, `test_terminal_command_effort`) обновлены осознанно — они сверяли старую строку. Новые: имя перед текстом (с `--effort` и без), push-строка без опасных символов, поиск процесса находит новую командную строку.
- Документы: `ours/README.md` (раздел очереди, памятка для Сергея), `CLAUDE.md` (правило про push).

**Выводы проверки окружения** (окно Терминала этой сессии): ни одна из `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, `DISABLE_GROWTHBOOK`, `DISABLE_TELEMETRY`, `DO_NOT_TRACK` не задана ни в `env`, ни в блоке `env` файла `~/.claude/settings.json`; `remoteControlAtStartup` = `true`. `claude --version`: 2.1.312. По документации Remote Control работает на Max при входе через claude.ai, API-ключ не поддерживается.

**Что не затронуто.** Агенты досок (`claude -p` в `WORKFLOW.md`) — их команда не менялась. Новая строка действует со следующего `.run` после слияния: очередь увидит изменение своих файлов, выйдет, `launchd` поднимет новую версию; работающие сессии не меняются.

### 2026-10-29 — E4b — правила агента конвейера

Модель: Sonnet (effort=medium). Основа: `docs/our/tasks/TASK-E4b.md`. Ядро (`src/`, `tests/`) не менялось.

**Решение архитектора: одно устройство правил для всех проектов.** Правила агента конвейера лежат в репозитории проекта в `docs/agents/pipeline.md` (и `docs/agents/pipeline-map.md` для руководителя, этап `Plan`); пишет их ведущий чат проекта, агенты не правят. Доска подключает их ключами `[agent] project_rules` / `manager_rules`.

**Почему не весь `CLAUDE.md`.** Он ≈ 12,6 КБ, агенту нужно ≈ 1,5 КБ: остальное — правила задач Claude Code (рабочие копии, PR, слияние, очередь) и «перед любой задачей прочитай DECISIONS / ARCHITECTURE / AGENTS.md», то, что E4 как раз отрезал. После E4 агент `CLAUDE.md` не получает, а у `oms` ключа `project_rules` не было — агент остался вовсе без правил.

**Что сделано.**

1. `docs/agents/pipeline.md` (2,9 КБ): прежний раздел «Если ты агент конвейера» плюс то, что относится к агенту из остального `CLAUDE.md` (существующие тесты не трогать, новые зависимости с обоснованием, язык, безопасность). Без ссылок на DECISIONS / ARCHITECTURE / ROADMAP / AGENTS.md. В `CLAUDE.md` раздел заменён одной строкой-ссылкой.
2. `check.py`: пути из `[agent] project_rules` и `manager_rules` закрыты для карточек этой доски автоматически — через `forbidden_rules`, поэтому попадают и в `## Forbidden paths` сводки `limits.md`; `allow_paths` их не снимает. Если путь закрыт ещё и шаблоном (у EVE `docs/**`), отказ один, с той же причиной.
3. Добавка архитектора: `DEFAULT_FORBIDDEN_PATHS += "**/CLAUDE.md", "**/AGENTS.md"` — вложенные файлы держат обе защиты (запрет `check.py` и запрет `Edit` в файле настроек). Корневые записи оставлены. **Следствие:** шаблоны регистронезависимы (`compile_glob`), поэтому закрыт и `docs/claude.md`; в существующем тесте `test_other_files_stay_open` (`test_isolation.py`) пример «открытого» пути заменён на `docs/claude-notes.md` — иначе тест противоречил бы добавке; больше в существующих тестах ничего не менялось.
4. Доска `oms`: `project_rules = "docs/agents/pipeline.md"` в `project.toml` (резервная копия рядом: `project.toml.bak-E4b`). `project-render oms` и `project-doctor oms` до слияния не запускались: файла нет на `our-main`. `pipeline-map.md` для `oms` — перед следующей задачей с нарезкой.

Закомментированные строки `project_rules` / `manager_rules` в заготовке `project-new` уже есть (E4); пояснение об устройстве правил к ним — в задаче `[plan] max_subtask_level`. Тесты — `ours/tests/test_pipeline_rules.py`.

### 2026-10-29 — E4 — изоляция агента

Модель: Sonnet (effort=high). Основа: `docs/our/tasks/TASK-E4.md`; решения архитектора — запись «Решения архитектора: выбор модели, лестница, таблица уровней» ниже. Ядро (`src/`, `tests/`) не менялось. Правились `ours/hooks/project_config.py`, `ours/hooks/check.py`, `ours/bin/project-render`, `ours/bin/project-doctor`, `ours/bin/project-new` (заготовка и запись файлов при создании доски), `ours/prompts/stages/plan.md` (и `CHANGES.md`), новый `ours/tests/test_isolation.py`, `ours/tests/test_billing.py` (осознанно, ниже), `ours/README.md`, документы. `claude` не запускался (кроме `--version` и `--help`).

**Решение и принцип (архитектор).** Изоляция агента одинаковая в обоих режимах оплаты, разница только в том, чем платим. Причина: агент — это `claude -p` под учёткой Сергея. Без изоляции он грузил личные настройки `~/.claude` (разрешения, плагины, навыки, MCP — в том числе коннектор EVE Companion с инструментом записи), проектные `.claude/settings.json`, `.mcp.json`, `CLAUDE.md` и `AGENTS.md` репозитория (у EVE `CLAUDE.md` велит читать `AGENTS.md` на 270 КБ и выполнять старые задания — автономному агенту нельзя) и мог править `node_modules` / `.venv` рабочей копии: такие правки не видны в диффе, а проверка типов и тесты идут их кодом. Кончился кредит — доска переключается на подписку одной настройкой и работает с той же изоляцией.

**Что сделано.**

1. **Команда агента** (`project_config.template_values`). Всегда в конце `--settings <доска>/claude-settings.json`. `api`: перед ним `--bare`. `plan`: `--setting-sources local --strict-mcp-config --disallowedTools 'mcp__*' --disable-slash-commands`. Порядок фиксирован, кавычки — `shlex.quote`; ядро дописывает `--model`/`--effort` сразу за словом `claude` и `--resume` в конец, а запрос идёт через stdin, поэтому «жадные» ключи (`--disallowedTools`, `--add-dir`) ничего не съедают. Выбрано `local`, а не пустой список: документация и `claude --help` не говорят, что `--setting-sources ''` допустимо (раздел о `--setting-sources`: «comma-separated list … (user, project, local)»). В клонах досок `.claude/settings.local.json` нет (проверено), так что `local` ничего не подгружает; `.claude/settings.local.json` в клоне агент править не может (`Edit(.claude/**)`), а `CLAUDE.local.md` закрывает `claudeMdExcludes`.
2. **Файл настроек доски** `claude-settings.json` теперь пишется в **обоих** режимах (права 600, атомарно; ветки «removed» больше нет). Ключи в постоянном порядке: `apiKeyHelper` (только `api`); `permissions.deny` — `Edit(<папка>/**)` на каждую папку `protected_dirs`, `Edit(.claude/**)`, `Edit(.mcp.json)`, `Edit(CLAUDE.md)`, `Edit(AGENTS.md)` без ведущей `/`, в `api` ещё `Read(//<папка ключей>/**)`; `env` — `BASH_DEFAULT_TIMEOUT_MS` и `BASH_MAX_TIMEOUT_MS`, оба `(tests_s + 120) × 1000` мс (для `oms` при `tests_s = 900` — 1 020 000); только `plan`: `claudeMdExcludes`, `autoMemoryEnabled: false`, `disableAllHooks: true`, `disableClaudeAiConnectors: true`, `syncClaudeAiSkills: false`, `syncClaudeAiPlugins: false`.
3. **`claudeMdExcludes`** (формат по документации: абсолютные шаблоны с `**`): `<корень рабочих копий>/**/CLAUDE.md`, `…/CLAUDE.local.md`, `…/AGENTS.md` и `~/.claude/CLAUDE.md` абсолютным путём. **`AGENTS.md` добавлен сверх задания:** документация (раздел AGENTS.md) — `claude` читает `AGENTS.md`, когда `CLAUDE.md` нет, и `claudeMdExcludes` на него действует; без этого исключение `CLAUDE.md` могло бы подтянуть 270-килобайтный `AGENTS.md` EVE.
4. **Что `plan` отрезает сверх источников** (сверка с `--bare`, список из документации: хуки, навыки, команды, subagents, плагины, MCP, автопамять, `CLAUDE.md`, личные и проектные настройки). Источники `local` снимают личные и проектные настройки, хуки и плагины из них; недостающее закрыто **документированными ключами**: `disableAllHooks` (хуки из `local` и плагинов), `disableClaudeAiConnectors` (коннекторы claude.ai, которые `claude` тянет сам: `--strict-mcp-config` по документации «ignoring all other MCP configurations», но про коннекторы claude.ai не говорит прямо; ещё и `--disallowedTools 'mcp__*'` убирает любой MCP-инструмент), `syncClaudeAiSkills: false` и `syncClaudeAiPlugins: false` (в `~/.claude/skills/synced` на Mac лежат навыки claude.ai), `autoMemoryEnabled: false`. **Сверх задания** добавлен флаг `--disable-slash-commands` (в `claude --help`: «Disable all skills and commands»): личные и подключённые навыки в `plan` иначе остались бы. Недокументированных ключей нет.
5. **`[agent]`**: `protected_dirs` (умолчание `["node_modules", ".venv"]`), `project_rules`, `manager_rules` (необязательные; путь от корня репозитория кода). Форма у всех одна: относительный путь без `..`, без `.` и пустых частей, без ведущей `/`, без `* ? [ ] !`, скобок `( ) { }`, обратной косой и управляющих символов; `/` в конце отбрасывается, повторы в `protected_dirs` убираются; пустой список — ошибка. Неизвестный ключ в `[agent]` — ошибка, как раньше. `project-doctor` для заданных `project_rules` / `manager_rules` проверяет `git cat-file -e refs/heads/<база>:<путь>`; нет файла на базовой ветке — FAIL.
6. **Промпты доски.** Ядро ставит `base.md` перед промптом каждого этапа, поэтому строка `project_rules` лежит в `base.md` (попадает во все этапы), строка `manager_rules` — в промпт `Plan`. Без ключей строк нет, и `WORKFLOW.md` ссылается на `ours/prompts/`, как раньше. С ключами `project-render` пишет **копии** промптов в `<доска>/prompts/` (`base.md` = `ours/prompts/base.md` + строка; `stages/plan.md` + строка карты; остальные этапы — точные копии) и `WORKFLOW.md` ссылается на них; убрал ключи — папка удаляется. **После правки `ours/prompts/` промпты доски нужно пересобрать:** `post-merge` делает это для `oms`, для `eve` — вручную (`project-render eve`).
7. **Подсказка руководителю** (решение архитектора) — в `ours/prompts/stages/plan.md`, в правиле выбора уровня: `Prefer simple/standard (Haiku). Use complex only for pieces that need cross-module decisions.` — постоянно, независимо от ключей.
8. **`check.py`:** `DEFAULT_FORBIDDEN_PATHS += ".claude/**", ".mcp.json", "CLAUDE.md", "AGENTS.md"` с комментарием «агент не правит настройки и инструкции Claude Code». `allow_paths` их не снимает: он освобождает только `ours/**` (загрузчик требует начало `ours/`, снимается только шаблон `ours/**`); проверено тестами. Задачи Claude Code, запущенные человеком, это не касается: `check.py` проверяет только карточки. Шаблоны привязаны к корню репозитория (как все остальные), вложенные `docs/CLAUDE.md` запретом не закрыты — агенту их закрывает `Edit(CLAUDE.md)` в файле настроек (одиночное имя файла действует на любой глубине).
9. **`project-render` / `project-new` / `project-doctor`.** `project-render` пишет файл настроек и (при ключах) промпты доски **до** `WORKFLOW.md`, на который они указывают; при отказе по ключу (режим `api`) ничего не трогает. `project-new` теперь тоже пишет файл настроек и в заготовке `project.toml` даёт три закомментированные строки по-русски. `project-doctor` после своего `project-render` сверяет файл настроек и промпты доски с тем, что пишет `project-render` (иначе FAIL «пересобери: project-render <доска>») — **после**, а не до: он сам пересобирает файлы и сам меняет `[timeouts]`, поэтому проверка «до» давала бы ложный FAIL при каждой правке `tests_s` (два существующих теста doctor это показали); теперь FAIL означает, что запись не удалась.

**Порядок оплаты** (записано, ничего не автоматизировано). В начале месяца — кредит (остаток сгорает). Кончился → пауза с причиной (E1a: `needs_human`, `## Quota Exhausted`, выдача остановлена) → переключение на подписку одной настройкой (`[billing] mode = "plan"` → `project-render` → перезапуск службы). Автоматически не переключаем.

**Почему `--bare` только в `api`, и риск.** На подписке `--bare` не авторизуется: вход пользователя (OAuth, связка ключей) он никогда не читает, ключ — только `ANTHROPIC_API_KEY` или `apiKeyHelper` из `--settings`. Поэтому на подписке то же достигается по частям (пункты 1–4). **Риск:** документация объявляет `--bare` будущим умолчанием для `-p` («will become the default for `-p` in a future release»). Тогда доски на подписке перестанут авторизоваться — **следить при каждом обновлении Claude Code** (`claude --version` и заметки о выпуске; проверка — один пробный ход доски на подписке после обновления). Кандидат на замену, если так случится: `--safe-mode` (в `claude --help`: отключает CLAUDE.md, навыки, плагины, хуки, MCP, автопамять; «authentication … work normally, which differs from `--bare`») — не взят сейчас, потому что описан как режим диагностики и неясно, читает ли он `--settings`.

**Уточнение чата этапа (только `Edit`).** Архитектор назвал «Edit и Write». По документации (Configure permissions, «`Write(path)`, `NotebookEdit(path)`, and `Glob(path)` rules never match») проверка записи любым встроенным инструментом идёт по `Edit(path)`, а `Write(path)` принимается, но не срабатывает никогда. Поэтому в файле настроек только `Edit`, и он закрывает инструмент Write. Подтверждено документацией; боем не проверялось.

**Выводы п. 7 (проверено 29.10.2026; ссылки — на страницы code.claude.com/docs/en).**

| Что | Итог |
|---|---|
| `claude --version`, `--help` | **2.1.312** (нужно ≥ 2.1.269 ради `tee`). В `--help` есть `--bare`, `--setting-sources`, `--strict-mcp-config`, `--disallowedTools`, `--settings`, а также `--disable-slash-commands`, `--safe-mode`, `--restricted` |
| `--bare` (headless) | **Подтверждено** (`headless`, «Start faster with bare mode»): не грузит хуки, навыки, команды, subagents, плагины, MCP, автопамять, `CLAUDE.md`; авторизация только `ANTHROPIC_API_KEY` или `apiKeyHelper` из `--settings`, OAuth и связка ключей не читаются; Claude получает Bash, чтение и правку файлов; команда по таймауту обрывается и не уходит в фон (полностью с v2.1.286); «will become the default for `-p`» |
| `--setting-sources` | **Подтверждено частично** (`cli-reference`, `memory`): список из `user`, `project`, `local`; правила `.claude/rules/` и `CLAUDE.md` проекта — при `project` (`memory`: «Project rules are skipped if you exclude `project`»), `CLAUDE.local.md` пропускается без `local`. Прямой фразы «`~/.claude/CLAUDE.md` только при `user`» не нашлось — поэтому он в `claudeMdExcludes`. Пустое значение документацией не подтверждено → `local`. `--settings` — отдельный уровень «command line» (`settings`: выше `local`, проектных и личных, ниже managed) |
| `claudeMdExcludes` | **Подтверждено** (`memory`): шаблоны сопоставляются с абсолютными путями, синтаксис glob, допустимо «на любом уровне настроек» (в списке слоёв `--settings` не назван; `settings-reference` даёт область «Any file» — **в `--settings` боем не проверялось**); managed `CLAUDE.md` исключить нельзя; на `AGENTS.md` шаблоны действуют |
| `Write(path)` не срабатывает | **Подтверждено** (`permissions`, «`Write(path)` … rules never match»). `Edit`-запрет действует на `>`, `>>`, `2>`, `tee` (с v2.1.269), `sed` и другие распознанные файловые команды; **не** действует на подпроцессы, которые пишут сами (скрипт Python, Node) |
| Шаблоны путей | **Подтверждено** (`permissions`, «How path patterns match»): `/путь` отсчитывается от папки файла `--settings`; `путь` без косой — от текущей папки (для `claude` это рабочая копия карточки); в `deny` одиночная папка (`node_modules/**`) действует на любой глубине. Мы пишем без ведущей `/` |
| Коннекторы, автопамять, `~/.claude.json` | **Подтверждено** (`mcp`): коннекторы claude.ai, которые `claude` тянет сам, выключают `disableClaudeAiConnectors` (любой файл настроек) и `ENABLE_CLAUDEAI_MCP_SERVERS=false`; `--strict-mcp-config` — «only use MCP servers from `--mcp-config`», про коннекторы прямо не сказано. `autoMemoryEnabled` — любой файл. `~/.claude.json` (MCP пользовательского и локального уровня) `claude` пишет сам; чтение «всегда» прямо не сформулировано — закрыто `--strict-mcp-config` и `--disallowedTools 'mcp__*'` |
| `--bare` и ключи команды (п. 1 задания) | `--settings` (в том числе `apiKeyHelper`) — **подтверждено** прямо. `permissions.deny` и `env` из `--settings` — **подтверждено в общей форме** («`--settings` can set any key your user settings file can set», `settings`; `env` и `permissions` — ключи обычного файла настроек); для `--bare` отдельно не оговорено, боем не проверено. `--allowedTools` — пример в `headless` именно с `--bare`. `--add-dir` — назван в `--help` для `--bare`; `--permission-mode acceptEdits` — обычный флаг, от обнаружения файлов не зависит. `BASH_DEFAULT_TIMEOUT_MS` (по умолчанию 120 000) и `BASH_MAX_TIMEOUT_MS` (по умолчанию 600 000; рабочий предел — большее из двух) — **подтверждено** (`env-vars`) |
| Инструменты в `--bare` | Bash, чтение и правка файлов. Промпты `ours/prompts/` опираются только на них: `grep` в них — это команда в Bash или текст проверки; встроенных Grep / Glob / Task / WebFetch / TodoWrite / NotebookEdit промпты не требуют. Единственное место — `in-progress.md`, п. 1: «Delegate broad exploration … to fresh-context subagents **when the CLI supports them**» — условное, при `--bare` просто не выполняется. Поиск по репозиторию агенту придётся делать через Bash (`grep`, `find` — из набора команд только для чтения); `--tools` по умолчанию на macOS и так не включает Glob и Grep (`cli-reference`) |
| `.venv` доски `oms` | `setup.sh` доски делает `python3.14 -m venv .venv` **в самой рабочей копии** карточки и `pip install -e ".[dev]"`; `test.sh` берёт `.venv/bin/python`. То есть `.venv` лежит в корне рабочей копии → `Edit(.venv/**)` его закрывает. Клон `eve` — `node_modules` в корне рабочей копии |
| `~/.claude/settings.json` | В нём только ключи `theme` и `statusLine`; раздела `permissions` и списка `deny` **нет** → без `user` агент не теряет ни одного запрета. Рядом лежит `~/.claude/skills/synced` (навыки claude.ai) — их закрывают `syncClaudeAiSkills` и `--disable-slash-commands`. Ничего не переносилось |
| Клоны досок (имена ключей верхнего уровня) | `oms` (`~/dev/oms-pipeline`) и `eve` (`~/dev/eve-pipeline`): `.claude/settings.json`, `.claude/settings.local.json`, `.mcp.json`, `.claude/CLAUDE.md`, `CLAUDE.local.md` — **нет**. Есть `CLAUDE.md` (oms 12 644 байта, eve 5 630) и `AGENTS.md` (oms 2 999, eve 267 615) — оба закрыты `claudeMdExcludes` в `plan` и `--bare` в `api`. `~/CLAUDE.md`, `~/dev/CLAUDE.md`, `~/symphony_workspaces/CLAUDE.md` и `AGENTS.md` в родительских папках нет |

**Дифф `WORKFLOW.md` доски `oms` (режим `plan`), до и после.** Отрисован `project.toml` доски `oms` старым и новым кодом во временной копии (в `scratchpad`; `project-render oms` и `eve` не запускались). Отличается ровно одна строка — `claude.command`: к концу добавлено ` --setting-sources local --strict-mcp-config --disallowedTools 'mcp__*' --disable-slash-commands --settings <папка доски>/claude-settings.json` (замена этого куска на пустую строку возвращает прежний текст байт в байт; то же для `eve`). Пути промптов прежние.

**Существующие тесты, изменённые осознанно** (`ours/tests/test_billing.py`): `legacy_render` (теперь убирает ключи изоляции E4 из `plan`-отрисовки и проверяет, что отличаются только они — тест побайтного равенства E1b сохранён), `test_plan_mode_renders_exactly_as_before` (проверка «нет `--settings`» заменена на «нет `--bare` и `apiKeyHelper`, ключи изоляции на месте»), `test_project_render_in_plan_mode_removes_a_leftover_settings_file` → `…_keeps_the_settings_file_without_the_key_parts` (файл больше не удаляется), `test_api_mode_writes_settings_command_and_quota_keys` (набор ключей и порядок `deny`; команда кончается `--bare --settings …`), `test_switching_back_to_plan_restores_the_old_workflow` (учитывает ключи изоляции). Остальные существующие тесты не менялись. Новые тесты — `ours/tests/test_isolation.py`.

**Остаточный риск.** Код, который запускают тесты (`test.sh`, `npm test`, скрипты проекта), работает под учёткой Сергея: он может писать в `node_modules`, `.claude/`, `~/.claude.json`, читать личные файлы и файл ключа — правила `deny` на подпроцессы не действуют. Закрыто только то, что делает сам агент встроенными инструментами и распознанными командами. Песочница Claude Code (`sandbox.filesystem.*`) — после пробы. Дополнительно: `Edit(.claude/**)` привязан к текущей папке (рабочая копия); путь в `artifacts/` или `kanban/` по абсолютному имени (мимо ссылки `docs/<ID>`) шаблоном не покрыт — в `--bare` навыки из `.claude/skills/` каталогов `--add-dir` загружаются (`headless`); путь `~/.claude` при заданном `CLAUDE_CONFIG_DIR` в `claudeMdExcludes` не учитывается.

**Не проверено боем** (`claude` не запускался). Как проверить на первой карточке `oms` после слияния: в журнале хода (кадр `system/init` потока stream-json) — пустой список MCP-серверов, нет плагинов и навыков, инструменты `Bash`/`Read`/`Edit`; ход не читает `CLAUDE.md`; `test.sh` не обрывается по таймауту Bash; попытка записи в `.venv/…` отклоняется (`permission_denials`). Отдельно: что `--setting-sources local` не ломает авторизацию по подписке, что `claudeMdExcludes` в `--settings` действует, что в режиме `api` с `--bare` читаются `env` и `deny` из `--settings`.

**Влияние на доски после слияния.** `ours/bin/post-merge` пересоберёт `WORKFLOW.md` доски `oms` (ровно ключи п. 1) и создаст её `claude-settings.json`. `oms` ключей `project_rules` / `manager_rules` не получает: правил проекта в её промпте нет, решение — при подключении. Доска `eve` (вне `post-merge`) остаётся со старым `WORKFLOW.md`, пока чат этапа не выполнит `project-render eve` и `project-doctor eve`.

- **Модель:** Sonnet.

### 2026-10-29 — Решения архитектора: выбор модели, лестница, таблица уровней

Принятые решения, не пересматривать. Текст ниже — дословно из файла архитектора `E4-arch-levels.md` (п. 1–5).

От архитектора — решения о выборе модели, лестнице и таблице
уровней (в DECISIONS отдельной записью, вместе с пересчётом K14).
Утверждено Сергеем.

Основание: реальные цены — Haiku 5.5 $0,10/$0,50, Sonnet 5.5
$2/$10, Opus 5.5 $4/$20 за млн токенов; по журналу доски oms
попытка Haiku $0,03–0,40, Sonnet $0,5–1,5, задача OMS-9 целиком
$1,71. Опыт Сергея (K14): архитектор и руководитель хорошо выбирают
уровень; лестница — страховка, а не обычный путь. Гипотеза:
повторный провал выбранной модели чаще вызван данными, правами,
окружением или формулировкой, чем силой модели.

1. Таблица уровней (заменяет simple и standard из записи 27.10):
   уровни отстоят ровно на шаг повтора (+2 ступени), повтор нижнего
   = первая попытка следующего.
     simple:   haiku low      (было medium)
     standard: haiku high     (было xhigh)
     complex:  sonnet high    (без изменений)
     critical: opus medium    (без изменений)
   Применить на досках oms и eve (project.toml → project-render,
   как скриптом set-oms-level-table.py); заготовку project-new —
   в задаче max_subtask_level.

2. Работники: начальный уровень — simple/standard (Haiku);
   complex (Sonnet) — для кусков, явно требующих межмодульных
   решений. Руководитель — Sonnet (нарезку не удешевляем). В plan.md
   — строка-подсказка руководителю: «Prefer simple/standard (Haiku).
   Use complex only for pieces that need cross-module decisions».
   critical не удешевляем через Sonnet: Opus дороже Sonnet лишь ×2,
   а цена ошибки на таких задачах высока.

3. Лестница (шаг 8) — упрощённо и раньше. ЗАМЕНЯЕТ правило
   «Таблицы уровней» (27.10) «если +2 за max — модель следующего
   уровня». Новое правило:
   - попытка 2 — только после МЕХАНИЧЕСКОГО провала с логом (упали
     тесты, типы, линтер; check.py отказал max_check_failures раз
     или исчерпаны возвраты при таком провале): ТА ЖЕ модель, усилие
     +2 ступени; reset-budget, раздел ## Escalated (что упало, что
     меняется), событие в журнале;
   - модель автоматически НЕ повышается; второй провал → needs_human
     с отчётом (что пробовали, что упало); разбор руководителем —
     позже;
   - провал из-за окружения (отказ в правах, запрещённая команда или
     путь, сбой setup.sh, исчерпан кредит, нет данных) и ход без
     продвижения / «не понял задание» — без повтора, сразу
     needs_human с причиной.
   Цепочки по умолчанию (настройка [ladder] в project.toml, меняет
   Сергей):
     simple:   haiku low    → haiku high
     standard: haiku high   → haiku max
     complex:  sonnet high  → sonnet max
     critical: opus medium  → opus xhigh
   Статистика по карточке: назначенный уровень; на какой попытке
   закрылась; причина каждого провала (механический / окружение /
   без продвижения); стоимость каждой попытки (runs.cost_usd);
   отклонение Сергеем на Human Review = провал. Классификация причин
   — главная часть задачи: проверь по коду, что уже различают
   check.py (запрещённые пути, тесты, разделы) и ядро
   (permission_denials в stream-json, quota). Задача — после пробы
   EVE (часть 1), с её данными.

4. Проба EVE: задача 1 — simple, задача 2 — standard, задача 3 —
   standard сознательно, как проверка гипотезы на пограничной задаче
   (чат EVE советовал Sonnet); при механическом провале — вручную
   попытка 2 (+2 ступени); второй провал — решение Сергея (дать ли
   Sonnet). Все попытки с ценой и причиной провала — в итоги пробы.
   Часть 2 — руководитель complex (Sonnet), подсказка из п. 2 в
   описании карточки.

5. Стоимость карточек — в итоги каждой пробы: сумма runs.cost_usd по
   карточке из state.db доски (оценка Claude Code по ценам API).

- **Пересчёт оценок K14.** В записях K14 и в `REPORT-16` числовых оценок стоимости попыток нет (кроме мелких вызовов ≈ $0,1 при проверке `--help`), поэтому пересчитывать нечего: основанием становятся цены и журнал доски `oms`, указанные в «Основании»; стоимость карточек считается по п. 5 в итогах каждой пробы.
- **Что от этого уже сделано, а что нет.** В E4 — только строка-подсказка руководителю в `ours/prompts/stages/plan.md` (п. 2). Таблица уровней на досках `oms` и `eve` (п. 1) меняется чатом этапа через `project.toml` → `project-render`; заготовка `project-new` — в задаче `max_subtask_level`; правило лестницы и `[ladder]` (п. 3) — задача после пробы EVE, часть 1; порядок пробы (п. 4) и стоимость карточек (п. 5) — в `ROADMAP.md`.

### 2026-10-28 — E3 — доска EVE

Модель: Sonnet (effort=high). Основа: `docs/our/tasks/TASK-E3.md`; решения — запись «Решения архитектора: оплата досок и подключение EVE», записи E1b, E2 и «Итоги K05–K07 … параметры EVE». Ядро и `ours/` не менялись; доска и клон лежат вне репозитория форка (в репозиторий не копировались).

**Что подключено.**

- **Клон и база.** `https://github.com/Serg10011/EVE-Companion.git` → `/Users/helloworld10011/dev/eve-pipeline` (отдельная копия только для службы; папки до этого не было). Базовая ветка по умолчанию на GitHub (`origin/HEAD`) — `main`, конец на момент замера `b935df2`. В клоне ничего не коммитилось и не пушилось; `git status` чистый, рабочих копий (worktree) кроме основной нет.
- **Доска.** `~/symphony-projects/eve/` создана штатным `project-new eve --repo /Users/helloworld10011/dev/eve-pipeline --base main --prefix EVE --port 9973`. Порт 9973 — первый свободный после `oms` (9972); `project-new` проверил доски, `lsof -iTCP:9973 -sTCP:LISTEN` пуст. Карточек на доске нет.
- **Node 24.22.1.** На Mac был только Homebrew `v26.12.1`, менеджеров версий нет, `.nvmrc` в репозитории нет. По решению Сергея (ответ в окне) поставлен официальный tarball `node-v24.22.1-darwin-arm64.tar.gz` с nodejs.org в `/Users/helloworld10011/.local/node/v24.22.1` (npm 11.19.1); SHA256 архива сверен с `SHASUMS256.txt` с nodejs.org до распаковки (совпал: `89c68259…fadfbc0a7`). Homebrew не затронут. Оба скрипта первой строкой ставят `PATH` на `…/v24.22.1/bin` и падают с кодом 1 и понятной причиной, если `node --version` не `v24.22.1`.
- **`setup.sh`:** `npm run install:ci` (`set -euo pipefail`, инструменты полными путями).
- **`test.sh`:** `ulimit -n 10240`, снятие лишних `SYMPHONY_*` (как у `oms`), проверка версии Node, затем **`npm test -- --maxWorkers=2 --testTimeout=15000 --reporter=default --reporter=junit --outputFile.junit="$dir/junit.xml"`** с `NODE_OPTIONS=--import=$PWD/tests/helpers/history-store-local-connections.mjs`; отчёт — в `mktemp -d` вне рабочей копии (`trap … rm -rf EXIT`); в конце `ours/hooks/summarize.py` по отчёту, код возврата — код тестов.
  - **Как получен JUnit и почему так.** Скрипт `test` репозитория — просто `vitest run` (`package.json`), поэтому репортёр и путь отчёта передаются флагами командной строки после `--`; ни `package.json`, ни `vitest.config.ts` не меняются. Консольный вывод остаётся обычным (`default`), JUnit добавлен вторым репортёром.
  - **Режим запуска — тот, который в самом репозитории EVE указан как рабочий** (`docs/handoff-2026-10.md`, `AGENTS.md`: Node 24.22.1, `maxWorkers 2`, `testTimeout 15000`, загрузка `tests/helpers/history-store-local-connections.mjs`). Это не подгонка: **без него база красная и нестабильная**. Четыре первых прогона на свежей копии (голый `npm test` и с `--maxWorkers=2 --testTimeout=15000`, но без загрузчика) дали от 247 до 501 упавших тестов, число каждый раз разное (`TypeError: fetch failed`, `HistoryStoreError: storage-failed`, таймауты) — причина `EADDRNOTAVAIL` на `127.0.0.1`: Miniflare сбрасывает локальное соединение после каждого вызова, за прогон набирается >10 тысяч сокетов `TIME_WAIT`, и временные порты macOS кончаются (в документах EVE эта проблема описана). Файл-загрузчик лежит в самом репозитории (`tests/helpers/`, защищён `tests/**`), в продукт он не импортируется. Флаги `--maxWorkers=2` вместо четырёх выбраны потому, что документы EVE отмечают один старый таймаут при четырёх; цена — время (около 330–350 с вместо ≈259 с из K05).
  - Оба скрипта прогнаны руками с коротким `PATH` в отдельной временной копии `main`; `git status` копии после полного прогона пуст (отчёт и `node_modules` в дерево не попали).

**Что нашлось в конфиге Vitest (AC4).** `vitest.config.ts`: `test.include = ["tests/**/*.test.ts"]`, `environment: "node"`, алиас `@`; **`setupFiles` не задан, `workspace`/`projects` нет**. Раскладка: 184 файла `tests/*.test.ts`, помощники в `tests/helpers/` (в том числе `.mjs`, `.tsx`, подпапка `history-store-0017/`), `.gitkeep`. Файлов `*.test.*` и `*.spec.*` вне `tests/` нет. Тесты сами читают из репозитория: миграции `drizzle/meta/_journal.json` и `drizzle/*.sql` (`tests/helpers/d1.ts`), `docs/reference/*.json` (метаданные SSO и опорные замеры), `tests/helpers/history-store-local-connections.mjs` (через `NODE_OPTIONS`). Каталога `.github` в репозитории нет.

**Итоговые настройки `project.toml`.**

- **`test_globs`** = `["tests/**", "**/*.test.ts", "**/*.test.tsx", "**/*.spec.*"]` (стартовая точка архитектора подтверждена: `tests/**` покрывает и `tests/helpers/`; остальные шаблоны — на будущее).
- **`[baseline] paths`** = `["tests/**", "**/*.test.ts", "**/*.test.tsx", "**/*.spec.*", "vitest.*", "vite.config.*", "package.json", "package-lock.json", ".nvmrc", "tsconfig*.json", ".npmrc", "drizzle/**", "docs/reference/**"]`. К списку архитектора добавлено: `.npmrc` (настройки npm — меняют установку), `drizzle/**` и `docs/reference/**` (тесты читают их как данные). Код приложения (`lib/`, `app/` и др.) не включён — как у `oms`, правило описывает тесты и их окружение. Каждый шаблон `test_globs` стоит в `paths` буквально (правило E2), загрузчик принял.
- **`forbidden_paths`** (добавка к встроенным): `package.json`, `package-lock.json`, `vitest.*`, `vite.config.*`, `.nvmrc` (решение K05), а также найденное в репозитории из той же категории: `tsconfig*.json` (Vitest преобразует TypeScript по нему), `.npmrc` (настройки npm), `.dev.vars` (локальные секреты Cloudflare, в git не попадает), и по образцу `oms` правила для агентов: `CLAUDE.md`, `AGENTS.md`, `.claude/**`, `.githooks/**`. **Сознательно не запрещено:** `.dev.vars.example` (образец без секретов); `eslint.config.mjs` и `scripts/**` (к запуску тестов не относятся: `npm test` = `vitest run`, `scripts/install-ci.mjs` исполняет только `setup.sh` на базе до работы агента) — вернуться к `eslint.config.mjs`, когда в `allowed_bash` появится `npm run lint`. **Нельзя разрешить:** `.env.example` запрещён встроенным `.env.*` (корень репозитория), снять запрет можно только правкой `check.py`; в этой задаче не меняли, пробные карточки его не трогают. **Про `eslint.config.mjs` изменено в E3b:** теперь запрещён.
- **`allow_modified_tests`, `doc_paths`** — пусто, **`required_sections`** — по умолчанию; **`[integration]`** и **`[delivery]`** не включены.
- **`[levels]`, `[limits]`, `[service] model`** — как у `oms`: `simple` Haiku `medium`, `standard` Haiku `xhigh`, `complex` Sonnet `high`, `critical` Opus `medium`, `default_level = "simple"`; лимиты 2 / 12 / 20 / 2; `model = "haiku"`. (Заготовка `project-new` даёт `standard` `high` и `critical` Sonnet `max` — устарело относительно таблицы уровней в этой записи; обновление заготовки уже в планах, «`[plan] max_subtask_level` + заготовка `project-new`».)
- **`[agent] allowed_bash` — пусто.** Решение архитектора: только `npm run <скрипт>` полным путём к `npm`, где скрипт ничего не пишет. Кандидаты прочитаны по содержимому `package.json`: `lint` = `eslint . --ignore-pattern dist --ignore-pattern .next` и `typecheck` = `tsc --noEmit --incremental false` — оба без записи, без `--fix`, без сети. **Но они не запускаются в среде службы:** проверено `env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin …/v24.22.1/bin/npm run lint` → `env: node: No such file or directory` (скрипт `npm` начинается с `#!/usr/bin/env node`, а `PATH` службы короткий). Подставить `PATH` агенту нельзя, и какой `node` оказался бы первым при другом `PATH` (Homebrew `v26`), неизвестно. «Сомнение — не добавлять»: список пуст. Вопрос архитектору: как давать агенту EVE `npm run lint`/`typecheck` (обёртка в папке доски, проверенный `PATH`?) — вне E3. **Изменено в E3b** (см. подпункт «E3b» ниже).
- **Запрет `package.json` защищает и список `allowed_bash`** (без него агент подменил бы содержимое скрипта `npm run` и обошёл бы список), поэтому держим его, пока в `allowed_bash` есть `npm run`; сейчас список пуст, запрет остаётся по решению K05 (файлы запуска тестов). **Изменено в E3b:** в `allowed_bash` теперь две обёртки `bin/lint` и `bin/typecheck`, запрет `package.json` защищает и содержимое скриптов, которые они вызывают.
- **`[billing]`: `mode = "plan"` до появления ключа** (режим `api` требует файла ключа, без него `project-render` откажет); закомментированные строки для `api` — `api_key_file = "/Users/helloworld10011/.symphony-keys/eve.key"`.

**Замер базы (`project-doctor eve`, код 0, FAIL нет).** `main @ b935df2`: **3519 прошли, 25 пропущены, 0 упали**; `test.sh` — **348,4 с**; `setup.sh` (`npm run install:ci`, тёплый кэш npm) — **6,5 с** (холодная ручная установка — 13 с, до порога «2–3 минуты» далеко: пометки «общий кэш npm — позже» не нужно). Записаны `.ours-state/baseline.json` и `baselines.json` (1 запись). `[timeouts]` заполнил doctor: `tests_s = 720`, `gate_ms = 780000`, `hooks_ms = 780000` (`stall_timeout_ms` = 840000). Ориентир K05 (≈3419 прошли, 20 пропущены, ≈259 с) отличается: репозиторий вырос (3544 теста против ≈3439), режим запуска — `maxWorkers 2` (см. выше). Итоговая сводка: `FAIL: 0, WARN: 1`; проверки ядра — 21 PASS. Два WARN doctor пометил «известно, не провал» (папка доски не git-репозиторий; абсолютный путь к `claude`). Единственный засчитанный WARN — `board.cli` («`symphony` не в PATH login-shell»; рабочим даётся `SYMPHONY_CLI`, штатные промпты это учитывают); с выводом доски `oms` я его не сравнивал.

**Служба.** `project-launchd eve` записал `~/symphony-projects/eve/launchd.plist` (метка `local.symphony.eve`, порт 9973, `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, `ANTHROPIC_MODEL=haiku`); `ANTHROPIC_API_KEY` и `ANTHROPIC_AUTH_TOKEN` в нём нет. **Служба не запущена** (`launchctl bootstrap` не выполнялся; `launchctl print gui/501/local.symphony.eve` — «Could not find service»).

**Порядок запуска пробы** (решение архитектора).

1. Сергей подтверждает, что в Console у рабочего пространства `eve` стоит потолок трат.
2. Сергей заводит ключ этого рабочего пространства в `~/.symphony-keys/eve.key` (права 600, папка 700).
3. Чат этапа: `[billing] mode = "api"` → `project-render eve` → `project-doctor eve` → `launchctl bootstrap` службы.


#### E3b — обёртки `lint` и `typecheck` для агента EVE

Модель: Sonnet (effort=medium). Основа: `docs/our/tasks/TASK-E3b.md`. Код форка (`src/`, `tests/`, `ours/`) не менялся; правки — в папке доски `~/symphony-projects/eve/` (вне репозитория): `bin/lint`, `bin/typecheck` (новые, права 755), `test.sh`, `project.toml` (и пересобранный `WORKFLOW.md`).

- **Решение архитектора и почему.** Агент не мог сам проверить линт и типы: `npm` начинается с `#!/usr/bin/env node`, а `PATH` службы короткий. Vitest типы не проверяет (только преобразует TypeScript), поэтому ошибка типов проходила зелёной проверкой. Две обёртки в папке доски сами задают Node 24.22.1 и **игнорируют все аргументы** (`"$@"` до `npm` не доходит): правило `allowed_bash` — префикс, и запись `npm run lint` пропустила бы `npm run lint -- --fix` (правку файлов). Правила держит код обёртки, а не агент. Node в `PATH` службы (`plist`) не кладём: он действует на все процессы службы. Скрипты сверены с `package.json` ветки `main`: `lint` = `eslint . --ignore-pattern dist --ignore-pattern .next`, `typecheck` = `tsc --noEmit --incremental false` — оба без записи файлов, обёртки сделаны для обоих. Версия Node не `v24.22.1` или нет `package.json` в текущей папке — код 2 и понятная строка в stderr.
- **Проверка на базе** (временная копия `main @ b935df2`, окружение службы `env -i`): `typecheck` — код 0, 0 ошибок, 6 с; `lint` — код 0, 0 ошибок, 1 предупреждение (`_policyRevision` не используется в `lib/server/background-sync/admission.ts`), 7 с; `git status --porcelain --ignored` до и после `typecheck`, `lint` и `lint -- --fix` совпадает (везде только `node_modules/`; кешей нет). Вызов с `-- --fix` печатает «обёртка аргументы не принимает, игнорирую» и ничего не правит. Копия убрана, `git status` репозитория EVE чист, лишних worktree нет.
- **`test.sh`.** Типы на базе чистые, поэтому `test.sh` после проверки версии Node и до `NODE_OPTIONS` вызывает `bin/typecheck` (одно место правил). Ошибка типов → код 1, строка в stderr «проверка типов (npm run typecheck) не прошла — тесты не запускались», строка итогов не печатается (`check.py` откажет — так задумано). Путь отказа показан на временном файле `lib/zz-typeerr.ts` с ошибкой типа: код 1, строка есть, итогов нет; файл удалён. Линтер в `test.sh` не ставили.
- **Замеры.** Весь `test.sh` (типы + тесты) на базе — **334,4 с** (предел `tests_s = 720` не затронут, таймауты не менялись); `project-doctor eve` — код 0, FAIL нет, 3519 прошли / 25 пропущены / 0 упали, как прежде; новый замер записан в `baselines.json`.
- **`allowed_bash`** = ровно `/bin/bash …/bin/lint` и `/bin/bash …/bin/typecheck`; в `WORKFLOW.md` появились правила `Bash(/bin/bash …/bin/lint:*)` и `Bash(/bin/bash …/bin/typecheck:*)`.
- **Запреты.** По уточнению чата этапа в `forbidden_paths` добавлены `eslint.config.*` (в EVE есть `eslint.config.mjs`), `.eslintrc*`, `.eslintignore`: иначе агент ослабил бы правила линта и сам получил бы чистый `lint`. `tsconfig*.json` уже был в запретах — теперь он важен вдвойне: ослабить `tsconfig` значит обойти проверку типов в шлюзе (добавка архитектора); `typecheck` вызывает `tsc` без `-p`, других файлов настроек типов нет. Комментарий у `package.json` обновлён: запрет держит и содержимое скриптов, которые вызывают обёртки.
- **Позже, если понадобится третьему проекту:** обобщить в комплект — `project-render` создаёт обёртки по `[agent] checks` в `project.toml`.

**Правила подбора карточек пробы EVE** (решение архитектора, следуют из настроек E3): без новых пакетов и правок `package.json`; без изменения поведения, закреплённого существующими тестами (новые тесты можно, старые менять нельзя — `allow_modified_tests` пуст); небольшие (уровень `simple` или `standard`), один модуль, проверка тестами.

**Риски.** Запрет файлов запуска тестов не даёт агентам EVE менять зависимости — для пробы допустимо. Полный прогон идёт около 6 минут и сильно нагружает `127.0.0.1` (десятки тысяч сокетов): два прогона подряд лучше разнести минимум на минуту (иначе `TIME_WAIT` не успевает очиститься и начинаются ложные падения) — для цикла `check.py` + `gate` это стоит учесть при первой карточке. `claude -p` в среде службы с `apiKeyHelper` и ключом EVE не проверялся (план — запись E1b).

- **Модель:** Sonnet.

### 2026-10-28 — E2 — правило «какие файлы — тесты» по проекту (`[baseline] paths`)

Модель: Sonnet (effort=high). Основа: `docs/our/tasks/TASK-E2.md`; решения архитектора — запись «Решения архитектора: оплата досок и подключение EVE» ниже. Ядро (`src/`, `tests/`) не менялось. Правились `ours/hooks/project_config.py`, `ours/hooks/check.py`, `ours/hooks/post_merge.py`, `ours/bin/project-new` (заготовка), добавлен `ours/tests/test_baseline_rule.py`, документы.

**Зачем.** Одно правило решает и «перемерять ли базу после слияния» (`post-merge`), и «верить ли старому замеру» (`check.py`, S4-25). Оно было одно на все проекты и рассчитано на Python. У EVE тесты — Vitest: правка `vitest.config.ts`, `*.test.ts` или `.nvmrc` меняет исход тестов, а старое правило её не видит, и `check.py` поверил бы устаревшему замеру.

**Что сделано.**

1. **Раздел `[baseline]` в `project.toml`.** Ключ `paths` — непустой список шаблонов путей от корня репозитория кода, тот же синтаксис, что у `[checks] forbidden_paths`; без ведущего `/` и без `..`; неразбираемый шаблон и неизвестный ключ — ошибка загрузки по-русски. Разбор шаблонов (`compile_glob`, `glob_match`, `matcher`) и умолчание `DEFAULT_TEST_GLOBS` перенесены из `check.py` в `project_config.py` (в `check.py` остались псевдонимы `glob_match`, `_matcher`; копий нет).
2. **Раздел заменяет правило целиком, а не дополняет.** Причина: проект на другом языке не должен наследовать питоновских шаблонов (`pkg/test_a.py` для доски Vitest ничего не меняет). **Раздела нет — правило прежнее**, ровно `project_config.BASELINE_RULE` (`post_merge.RULES["doctor"] is pc.BASELINE_RULE` остаётся верным, существующие тесты не менялись); доска `oms` раздела не получает.
3. **Одна функция правила:** `project_config.paths_rule(paths)` (правило проекта как вызываемый объект, шаблоны компилируются один раз), `affects_baseline(path, project=None)` (прежняя сигнатура сохранена) и `load_baseline_rule(board)` (правило, прочитанное только из `project.toml`).
4. **Где применяется.** `check.py`: `effective_baseline` берёт правило **этой доски** (`Config.baseline_paths`), поэтому после замера A изменение `vitest.config.ts` в базовой ветке делает замер недействительным (`baseline_unknown`), а изменение одного `docs/a.md` — нет (`ancestor`). `post-merge`: `classify` решает «нужен ли `project-doctor`» правилом проекта (`ProjectPlan.baseline_rule`).
5. **Отказы `post-merge` вместо молчания.** Если `project.toml` проекта не читается (нет файла, ошибка TOML), запуск отказывает до любых действий («Ошибка: проект …», код 1): без файла неизвестны ни репозиторий, ни правило проекта, и «перемерять не нужно» было бы догадкой (раньше такой проект тихо пропускался с предупреждением; для этого у `project_config.other_projects` появился необязательный список нечитаемых файлов). Если читается файл, но неверен раздел `[baseline]`, отказ получает один этот проект («ОТКАЗ: …», код 1, коммит для него не запоминается — повтор попробует снова), остальные проекты обрабатываются. `post-merge` читает из `project.toml` только `[baseline]` и `[checks] test_globs`: остальные ошибки проекта по-прежнему его не останавливают.
6. **Заготовка `project-new`:** закомментированный раздел с пояснением по-русски (что значит правило, где используется, какое правило действует без раздела, что шаблоны `test_globs` обязаны быть в `paths`), пример для Vitest и строка про явные `test_globs`.
7. **Тесты** `ours/tests/test_baseline_rule.py`: правило без раздела совпадает с прежним (в том числе `affects_baseline` без второго аргумента); Vitest-пути меняют, питоновские — нет; согласованность с `test_globs` (нет `**/*.spec.*`; пример при умолчательных `test_globs` — ровно `**/test_*.py`, `**/*_test.py`, `**/*_test.go`; явные `test_globs` — загружается; буквальное совпадение; без раздела проверки нет); ошибки загрузчика (пустой список, не список, `..`, ведущий `/`, неразбираемый шаблон, неизвестный ключ, нет `paths`); `check.py` на настоящем git (`baseline_unknown` / `ancestor`; неверный раздел — отказ шлюза); `post-merge` (слияние тестов Vitest требует `project-doctor`, исходник — нет; проект без раздела — прежнее правило; нечитаемый `project.toml`; неверный `[baseline]` одного проекта не останавливает остальных).

**Решения архитектора по E2.**

- **Согласованность с `[checks] test_globs`:** при заданном `[baseline]` каждый шаблон `test_globs` (явный или встроенный) обязан стоять в `paths` буквально; иначе ошибка загрузки с перечнем недостающих. Причина: файл, защищённый как тест, обязан и вызывать перемер, и обесценивать старый замер. Без раздела проверки нет.
- **Пример для Vitest выровнен с `DEFAULT_TEST_GLOBS`** (`vitest.*` — конфиг, файлы подготовки, workspace).
- **Пока нет доставки EVE**, после изменений базовой ветки EVE чат этапа запускает `project-doctor` доски EVE вручную: `post-merge` обслуживает только проекты, чей репозиторий — клон того же репозитория, что основная папка форка (`find_projects`), а EVE — другой репозиторий; для неё правило работает сейчас только в `check.py`. Перемер базы EVE после слияний — часть доставки EVE (не в этой задаче; во время пробы EVE без доставки база не двигается).

**Уточнение по коду (чат этапа).** В `DEFAULT_TEST_GLOBS` есть ещё `**/test_*.py`, `**/*_test.py`, `**/*_test.go`, поэтому пример `paths` для Vitest проходит проверку согласованности только вместе с явным `test_globs = ["tests/**", "**/*.test.ts", "**/*.test.tsx", "**/*.spec.*"]`; иначе к `paths` нужно добавить и эти три шаблона. Об этом сказано в заготовке `project-new` и в `ours/README.md`.

**Для E3.** Сверить `[baseline] paths` и `[checks] test_globs` доски EVE с vitest-конфигом самого репозитория EVE (`include`, `setupFiles`) по самому репозиторию, а не по примеру: пример — только образец.

### 2026-10-28 — E1b — режим оплаты `[billing]`, `apiKeyHelper`, защита ключа

Модель: Sonnet (effort=high). Основа: `docs/our/tasks/TASK-E1b.md`; решения — запись «Решения архитектора: оплата досок и подключение EVE» ниже и E1a (ключи ядра `agent.quota_exhausted_*`). Ядро (`src/`, `tests/`) не менялось. Правились `ours/hooks/project_config.py`, `ours/templates/WORKFLOW.md.tmpl`, `ours/bin/project-render`, `ours/bin/project-doctor`, `ours/bin/project-new` (заготовка), `ours/hooks/check.py`, `ours/hooks/deliver.py`, новый `ours/tests/test_billing.py`, документы.

**Что выяснено по документации Claude Code (проверено чатом этапа 28.10.2026).**

- Порядок выбора учётных данных: `ANTHROPIC_AUTH_TOKEN` → `ANTHROPIC_API_KEY` → вывод `apiKeyHelper` → `CLAUDE_CODE_OAUTH_TOKEN` → … → вход по подписке. То есть `apiKeyHelper` главнее входа по подписке: при заданном помощнике ходы оплачиваются ключом, иначе — подпиской.
- `apiKeyHelper` можно передать файлом `claude --settings <файл>` (уровень «command line», выше пользовательских и проектных файлов). Помощник — команда для `/bin/sh`, печатает ключ на stdout одной строкой и выходит с кодом 0; при ошибке запросы падают с «Your apiKeyHelper script is failing». Попадает ли вывод помощника в окружение команд агента, документация прямо не говорит — **проверить боем** (ниже).
- Правила `permissions.deny` вида `Read(//<абсолютный путь>)` действуют на файловые инструменты Claude и на распознанные файловые команды Bash (`cat`, `head`, `tail`, `sed`, `tee`, перенаправления), но **не на произвольные подпроцессы** (скрипт Python или Node, который сам открывает файл). Полная защита уровня ОС — песочница Claude Code (`sandbox.filesystem.denyRead`).
- Служба перечитывает `WORKFLOW.md` на каждом такте опроса (проверено по коду: `orchestrator/dispatch.py` вызывает `reload()` в начале выбора карточек; `post_merge.py` так и записывает: `project-render` — «служба подхватит сама, без перезапуска»). Поэтому новый режим действует со следующего хода, а пересборка снимает остановку доски E1a.

**Что сделано.**

1. **Раздел `[billing]`** в `project.toml`: `mode` = `plan` (умолчание) | `api`, `api_key_file` (обязателен при `api`). Загрузчик проверяет только форму пути: абсолютный, без сегмента `..`, только `[A-Za-z0-9._/-]` (путь попадает в команду для `/bin/sh` и в правило запрета); неизвестный ключ — ошибка. Наличие и права файла загрузчик не смотрит (пропавший файл иначе ломал бы каждый хук доски); это делают `project-render` и `project-doctor` общей функцией `project_config.billing_key_problems`.
2. **Сборка в режиме `api`** (`project-render`): проверка файла ключа (существует, обычный файл, не ссылка, владелец — текущий пользователь, права без доступа группе и остальным, до 1 КБ, не пуст, в пути нет символьных ссылок, лежит вне репозитория кода, папки доски, корня рабочих копий и основной папки форка) и папки ключей (не домашняя и не родитель домашней, не совпадает и не содержит ничего из перечисленного, права 700, владелец — мы). Нарушение — `ProjectError`, `WORKFLOW.md` не пересобирается. Файл `<доска>/claude-settings.json` (атомарно, права 600): `apiKeyHelper` = `/usr/bin/tr -d '[:space:]' < '<файл>'` и `permissions.deny` = `Read(//<папка ключей>/**)`. Команда `claude` получает `--settings <доска>/claude-settings.json`; в `agent:` — `quota_exhausted_state: needs_human` и `quota_exhausted_note` (по-русски, что делать). Файл настроек пишется до `WORKFLOW.md`.
3. **Сборка в режиме `plan`** (и без раздела): ни `--settings`, ни ключей `quota_exhausted_*`; `WORKFLOW.md` побайтно прежний (тест: шаблон без единственного нового места подстановки даёт тот же текст); оставшийся `claude-settings.json` удаляется. В шаблоне новое место подстановки — пустая строка в `plan`, поэтому в шаблон не добавлено ни комментария.
4. **Защита ключа «ключ в диффе»** (все доски, любой режим): быстрая проверка `check.py` (вместе с запрещёнными путями, до тестов) ищет `sk-ant-[A-Za-z0-9_-]{20,}` в **добавленных** строках изменений относительно базы (закоммиченных, в индексе, не в индексе, в неотслеживаемых файлах; `kanban` и `.symphony` не смотрятся). Отказ называет только `<путь>:<номер строки>`; текст строки не попадает в отчёт, `## Check Failure`, `check.log` и счётчик. Удалённая строка, `sk-ant-…` в тексте и короткие строки отказа не дают; двоичные файлы (NUL в добавленных строках) пропускаются. `deliver push` перед пушем делает ту же проверку по `git diff <база>..<D>` и по заголовку и описанию PR; отказ — ничего не пушится. Одна функция поиска (`check.diff_key_hits`) на обоих; `git diff` вызывается с `--text` и фиксированными префиксами, чтобы `.gitattributes` или настройки пользователя не прятали файл («двоичным» файл считаем сами, по NUL).
5. **`project-doctor`.** Режим `api`: те же проверки файла и папки и запуск команды помощника (`/bin/sh -c`, 10 с): вывод должен быть одной строкой вида ключа Anthropic; сам вывод не печатается (PASS или FAIL с причиной), запроса к API нет. Режим `plan`: «оплата: подписка». **В обоих режимах** — FAIL, если `ANTHROPIC_API_KEY` или `ANTHROPIC_AUTH_TOKEN` есть в `EnvironmentVariables` файла `launchd.plist` доски или в окружении самого `project-doctor`; в выводе только имя и место. `project-launchd` этих переменных не пишет (закреплено тестом). Заготовка `project-new` получила закомментированный пример `[billing]` с шагами.
6. **Тесты** `ours/tests/test_billing.py` (75 штук): план без изменений и удаление лишнего файла настроек; режим `api` (файл настроек, права 600, команда, ключи в `agent:`, принятие ядром, идемпотентность, возврат на `plan`, отказ не трогает файлы); отказы сборки и загрузчика; команда помощника выдаёт ровно ключ; `check.py` (закоммиченный, в индексе, не в индексе, неотслеживаемый, ключ в отчёт, карточку и журнал не попадает, длинный список, удалённая строка, не-ключи, двоичный файл, разбор диффа); `deliver push` (дифф, заголовок, описание; ничего не запушено); `project-doctor` (режимы, права, домашняя папка, чужой вывод помощника, обе переменные в окружении и в plist в обоих режимах, чистый plist); `project-launchd`. Настоящий ключ нигде не используется: образец собирается во время выполнения (`"sk-" + "ant-" + …`), в документах — только `sk-ant-…`. Существующие тесты не менялись.

**Решения архитектора по E1b.**

- **FAIL при переменных ключа в окружении** (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`) — в обоих режимах. Они выбираются раньше `apiKeyHelper` и входа по подписке: при `plan` ходы тихо пошли бы на API, при `api` — с чужого ключа мимо потолка трат.
- **Отдельная папка только для ключей** (образец — `~/.symphony-keys/`) и правило `deny` на всю папку: поэтому она не может быть домашней и не может содержать репозиторий кода, папку доски, корень рабочих копий или основную папку форка (иначе правило закрыло бы агенту его собственную работу).
- **Песочница Claude Code не включается** (меняет права всех команд агента; отдельное решение).
- **Потолок трат рабочего пространства в Console — обязательная часть страховки** (заводится вместе с ключом).
- **Остановка доски при исчерпании кредита и её снятие пересборкой** (E1a): в `needs_human` уходит одна карточка с `## Quota Exhausted`, остальные ждут; автоматически доска на подписку не переключается — это делает человек (`mode = "plan"`, `project-render`).

**Что не защищено.** Код, который запускает агент (`test.sh`, `npm test`), работает под тем же пользователем и может прочитать файл ключа сам; правило `deny` на произвольные подпроцессы не действует. Страховка: проверка «ключ в диффе» (`check.py`, `deliver`, заголовок и описание PR) и потолок трат в Console. Проверка «ключ в диффе» ловит только строки вида `sk-ant-…` в добавленных строках, не ключ в виде, который агент нарочно исказил.

**Не проверено (проверка боем после 30.10.2026).** Настоящий ключ и `claude -p` с `apiKeyHelper` не запускались. План на доске `oms` с `mode = "api"` (включает чат этапа, когда будет ключ): (а) карточка проходит ход — расход виден в Console рабочего пространства, а не в лимите подписки; (б) во время хода у процесса Bash агента нет `ANTHROPIC_API_KEY` в окружении (`ps -E -p <pid>` на Mac); (в) при исчерпанном кредите (потолок трат рабочего пространства, временно минимальный) карточка уходит в `needs_human` с `## Quota Exhausted`.

**Влияние на `oms`.** После слияния `post-merge` пересоберёт `WORKFLOW.md` доски `oms`: без раздела `[billing]` это режим `plan`, файл не меняется.

- **Модель:** Sonnet.

### 2026-10-28 — E1a — PR ядра №5: `agent.quota_exhausted_state`

Модель: Sonnet (effort=high). Основа: `docs/our/tasks/TASK-E1a.md`; решения — запись «Решения архитектора: оплата досок и подключение EVE» ниже. Правка ядра небольшая и изолированная; `ours/` не менялся.

1. **Что сделано.** Два ключа `agent.*`: `quota_exhausted_state` (имя статуса из `tracker.terminal_states`, без учёта регистра и крайних пробелов; пусто — поведение прежнее) и `quota_exhausted_note` (текст до 2000 знаков, по умолчанию пусто). Если ход упал с исчерпанным кредитом («Credit balance is too low» и прочие ошибки квоты), `agent.fallback_kinds` не подобрал запасной бэкенд и ключ задан, то карточка переводится трекером в этот статус, в неё пишется раздел `## Quota Exhausted` (ответ бэкенда, строка `was: <статус до перевода>`, строка `moved to <статус> by agent.quota_exhausted_state; no automatic switch`, затем заметка из конфигурации). Пауза не ставится, повтор не планируется, счётчики попыток (`rewind_count`, `max_attempts`, `max_reopens`) не трогаются; в журнал уходит событие `worker_quota_exhausted_state`. Если трекер не принял перевод — прежняя пауза «paused for operator inspection» и событие `worker_quota_exhausted_state_failed`. Временные ошибки (429, rate limit, overloaded) идут прежним повтором.
2. **Где.** Разбор и проверка ключей — `workflow/builder.py` (`_validated_quota_exhausted_state`, `_validated_quota_exhausted_note`), поля — `workflow/config.py`. Распознаватель ошибки помощника ключа `_is_api_key_helper_error` — рядом с `_is_quota_worker_error` (`orchestrator/core_support.py`). Решение принимается в `_handle_worker_error` (`orchestrator/worker_exit.py`); сама логика, остановка выдачи и снятие остановки — в новом файле `orchestrator/quota_hold.py`, чтобы правка ядра осталась маленькой. Остановка проверяется последним шагом `_eligibility_decision` (`orchestrator/dispatch.py`), снимается в начале `_on_tick`; признак виден в `/api/v1/state` как `dispatch_hold` (`orchestrator/api_views.py`).
3. **Решения архитектора по E1a.**
   - **Остановка доски, а не каскад.** После успешного перевода оркестратор ставит флаг в памяти «выдача остановлена: <причина>, с <время>». Пока флаг стоит, новые карточки не берутся; каждый пропуск — событие `dispatch_held_quota` (ID карточки, причина), не чаще раза в минуту на всю доску. Флаг снимается, когда меняется содержимое `WORKFLOW.md` (сравнивается хэш содержимого, а не время изменения: простое «касание» файла остановку не снимает) или при перезапуске службы; снятие — событие `dispatch_hold_released`. Без ключа флага нет никогда.
   - **Строка `was: <статус до перевода>`** в разделе `## Quota Exhausted`: по ней видно, куда вернуть карточку.
   - **«Your apiKeyHelper script is failing»** (и `apiKeyHelper failed:`) при заданном ключе идёт тем же путём, что исчерпание кредита; к запасным бэкендам (`fallback_kinds`) не ведёт — другой бэкенд ключ не починит. Без ключа — как раньше.
4. **Порядок с `fallback_kinds`.** Сначала по-прежнему пробуется переключение на запасной бэкенд; ключ срабатывает, только когда запасных не осталось.
5. **Почему иначе нельзя.** Решение принимается внутри `_handle_worker_error` по тексту ошибки хода; хуки этого текста не видят (`after_run` срабатывает раньше, чем ядро ставит паузу, а на паузе карточка больше не получает ходов), поэтому снаружи перевести карточку нельзя; то же с остановкой выдачи — выбор карточек делает ядро.
6. **Как проверено.** Новые тесты `tests/test_quota_exhausted_state.py` (настоящая файловая доска: перевод, раздел, `was:`, заметка, отсутствие паузы и повтора, неизменные счётчики — для «Credit balance is too low» и «apiKeyHelper»; прежняя пауза без ключа; `fallback_kinds` первым; 429 повторяется; сбой трекера возвращает паузу; остановка доски через `_on_tick`, не чаще раза в минуту, снятие по смене содержимого `WORKFLOW.md`, без ключа флага нет; проверка конфигурации). Существующие тесты не менялись.
- **Не проверено:** настоящая ошибка кредита не воспроизводилась — проверка боем после 30.10.2026 (E1b и проба EVE). После слияния `post-merge` перезапустит службу `oms` (менялся `src/`); поведение доски не меняется, пока ключ не задан — его задаст E1b только для режима `api`.
- **Модель:** Sonnet.

### 2026-10-28 — Решения архитектора: оплата досок и подключение EVE

Принятые решения, не пересматривать.

Факты (документация Anthropic, проверено 28.10): Max 5x — $100/мес API-кредита на Claude Platform (не переносится); интерактивный Claude Code кредитом не оплачивается; `claude -p` с API-ключом привязанной организации Console — оплачивается кредитом, с входом по подписке — лимитом подписки. Кредит кончился → «Credit balance too low», ходы останавливаются, на подписку не переключается. Получить — через 7 дней после начала подписки (Max с 23.10 → с 30.10).

Решения:
1. Обе доски (`oms` и `eve`) — на кредит; в Console два рабочих пространства со своими ключами и потолками трат.
2. Режим оплаты — настройка доски в `project.toml`: `plan` | `api` (умолчание `plan` — без изменений). Ключ — в файле вне репозитория (права 600), путь — в `project.toml`; ключ получает только процесс агента. Проверить по документации Claude Code `apiKeyHelper` (ключ не в окружении команд агента) — если работает с `claude -p`, использовать его вместо переменной окружения.
3. Исчерпание кредита: ответ «Credit balance too low» в ходе → карточка в `needs_human` с причиной «API-кредит исчерпан — переключи доску на подписку», без сжигания попыток. Автоматически на подписку НЕ переключать.
4. Защита ключа: `check.py` (быстрая проверка) и `deliver` отказывают, если в добавленных строках диффа есть строка вида `sk-ant-…`; отказ без вывода самой строки.

Порядок: S4-27 → E1 (п. 1–4, до 30.10) → E2 (правило «какие файлы — тесты» по проекту в `project.toml`, используют `post-merge` и история замеров S4-25; EVE — Vitest) + E3 (доска EVE на копии репозитория EVE-Companion: `project-new`, префикс `EVE`, свой порт, таблица уровней как у `oms`, интеграция выключена, `setup.sh` с Node 24.22.1 и `npm run install:ci`, `test.sh` через `npm test` со строкой итогов, запреты — существующие тесты, `.github`, файлы окружения; `project-doctor`, служба `launchd`, оплата `api`) → проба EVE после 30.10 (3–5 обычных карточек, Haiku, финал `Human Review`, без доставки) → S4-28 → дальше по порядку. Параметры EVE — запись DECISIONS «Итоги K05–K07 … параметры EVE».

Нарезка E1 (чат этапа): E1a — правка ядра (п. 3: ключ `agent.quota_exhausted_state`); E1b — `ours/` (п. 1, 2, 4: режим оплаты, `apiKeyHelper`, защита ключа, перевод в `needs_human` через ключ E1a в режиме `api`).

Ответ архитектора по E1 (28.10.2026): нарезка E1a/E1b и пункты 1–4 чата этапа — да (п. 1: правка ядра неизбежна; п. 2: `apiKeyHelper` через `--settings`; п. 3: песочницу не включаем, потолок трат рабочего пространства в Console — обязательная часть страховки; п. 4: переключение режима + `project-render`). Добавки к E1a: остановка доски, а не каскад; строка `was:`; ошибка `apiKeyHelper` — тем же путём. Добавки к E1b: `project-doctor` — FAIL при `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` в окружении службы и самого doctor (без вывода значения); ключ — в отдельной папке только для ключей (`~/.symphony-keys/`), правило `deny` закрывает всю папку; при исчерпании кредита в `needs_human` уходит одна карточка, остальные ждут, переключение режима + `project-render` снимают остановку.

### 2026-10-28 — S4-27 — доставка `.deliver`, часть А (без повторной проверки)

Модель: Sonnet (effort=high). Основа: `docs/our/tasks/TASK-S4-27.md`; решения — запись «Решения архитектора: доставка `.deliver` (шаг 7)» ниже. Ядро (`src/`, `tests/`) не менялось. Правились (контур контроля — задачу запустил человек): `ours/hooks/inbox_runner.py`, `ours/hooks/check.py`, `ours/hooks/project_config.py`, `ours/bin/project-new`; новое — `ours/hooks/deliver.py`, `ours/bin/deliver`, `ours/tests/test_deliver.py`; дополнения в `test_check.py` и `test_inbox_runner.py` (новые тесты в конце файлов, существующие не менялись); `ours/README.md`, `ours/hooks/README.md`, `CLAUDE.md`, `docs/our/HOWTO-TASKS.md`.

**Нарезка шага 7.** А (эта задача) — доставка, когда `our-main` не сдвинулся с зелёной проверки (D = конец ветки карточки), пуш, PR, `.deliver`, `.merge` для `deliver/<ID>`, запись «влито». Б (S4-28) — слияние с новым `our-main` двумя родителями и проверка во временной копии. **S4-28 гоняет полный набор во временной копии, когда `our-main` сдвинулся ИЛИ зелёная проверка была лёгкой.** В части А оба случая — отказ с причиной.

**Как это работает по-простому.** Карточка дошла до `Human Review`. Чат кладёт `<ID>.deliver`; очередь запускает `deliver push <ID>`. Программа проверяет, что есть зелёная проверка **именно этого** содержимого ветки, что `our-main` с тех пор не ушёл вперёд и что клон не отстаёт от GitHub, и пушит один коммит в новую ветку `deliver/<ID>` (по явному адресу `https://github.com/<repo>.git` из `[delivery]`, без `--force`), потом открывает PR в `our-main`. Локальные ветки и рабочая копия карточки не меняются никогда. После «добро» Сергея чат кладёт `<ID>.merge` со строкой `deliver/<ID>`: очередь вливает PR, вызывает `deliver merged <ID>` и `post-merge`; `Done` ставит Сергей в веб-доске.

- **Отметка в журнале проверок (`check.py`).** Раньше событие `hook pass` не несло дерева, а отметка `<ID>.pass.json` у обычной карточки никем не забиралась и после следующего провала оставалась устаревшей: надёжной записи не было. Теперь в событие `pass` добавлены `tree` и `head` (те же, что в `.pass.json`, которую не меняли) и `tests`: `full`, если `test.sh` действительно прогнан (раньше у обычной карточки поля не было), `skipped-docs` — лёгкий путь. Событие `gate` не менялось (на отсутствие у него `tests` при полном прогоне опираются тесты `test_light_path.py`). **Зелёная проверка** — последнее *решающее* событие карточки (`hook pass`, `hook rollback:*`, `gate fail`) есть `hook pass`, его `tree` равно дереву конца источника и `tests = full`. Причины отказа: нет событий; откат или провал шлюза после проверки; `pass` без дерева (событие до S4-27); дерево ветки не то; лёгкий путь.
- **Лёгкий путь не годится** (решение архитектора, S4-16 п. 4): `post-merge` после слияния одних документов тестов не гоняет, поэтому полный прогон перед доставкой обязателен независимо от `doc_paths`.
- **Источник и «не сдвинулся».** Карточка-задача — конец `integration/<ЗАДАЧА>` плюс `<ЗАДАЧА>.final.json` (`task`, `branch`, `integration_commit` = конец источника — иначе «в ветке карточки-задачи правки после итоговой проверки»; `our_main` = конец базы). Обычная — конец `symphony/<ID>`; база не сдвинулась, если её конец — предок конца ветки.
- **Клон не отстаёт от GitHub.** `git ls-remote` ветки базы = ветка базы клона, иначе отказ «сначала `ours/bin/post-merge`»: замеры базы и проверки идут по клону.
- **`[delivery] base` = `[project] base_branch`.** Иначе проверка «база не сдвинулась» шла бы не по той ветке. Владелец `cskwork` — отказ всегда (константа в коде); раздел необязателен, без него отказ «нет раздела [delivery]». Загрузчик проверяет форму `владелец/имя`, имя ветки и неизвестные ключи; обе части обязательны, если раздел есть.
- **Продолжение после сбоя `gh pr create`.** Сразу после пуша пишется `<ID>.delivered.json` с `pr: null`. Сбой создания PR — код 2, запись остаётся. Повторный `.deliver` пушит заново **не** (ветка на GitHub указывает ровно на коммит записи и карточка не сдвинулась), а переходит к PR; ветка на другом коммите, чужая запись или сдвинувшаяся карточка — отказ. Перед `gh pr create` всегда `gh pr list --head deliver/<ID> --state open`: PR уже есть (ответ на создание потерялся) — записать его, второй не создавать; больше одного или PR в другую базу — ошибка.
- **Запись «влито» для обоих видов карточек** (решение архитектора по п. 5): `deliver merged` пишет `<ID>.merged.json` в формате S4-13 с `source = deliver/<ID>`, `commit` = D, `tree` = дерево D, `task: null`. Шлюз обычной карточки и раньше принимал такую запись; шлюз карточки-задачи теперь принимает **только** `source = deliver/<её ID>` (оговорка к правилу S4-21, см. выше). Основание: дерево доставки уже прошло полный прогон.
- **`## Delivery`** дописывается в конец существующего раздела (`check.APPEND_TO_SECTION`, как `## Subtask Progress`): строки PR, коммита, источника, `our-main`, «повторной проверки не было» и позже «PR влит».
- **Очередь.** `.deliver` — четвёртая команда (пустой файл, повтор разрешён, как `.test`; таймаут 900 с, в S4-28 вырастет); `.merge` со строкой `deliver/…` принимается, только если код в строке совпадает с именем файла; для `deliver/<ID>` шаг закрытия окна не выполняется, вместо него `deliver merged` (его провал — только строка в отчёте), затем `post-merge`. Для `dev/<код>` всё как было.
- **Мелочь в `run_checks`.** Ветка «запись „влито“ не совпала по дереву» теряла поля `baseline_*` (S4-25): теперь дополняет отметку, а не перезаписывает.

**Как проверено.** `ours/tests/test_deliver.py` — настоящий git во временных репозиториях, «GitHub» — локальный голый репозиторий, `gh` — подмена, настоящие `check.log`, карточки и блокировки: оба вида карточек; точные аргументы пуша (без `--force`, один refspec) и `gh pr create`; снимок ссылок клона, `HEAD` и состояния рабочей копии до и после; все отказы (нет события с деревом, другое дерево, лёгкий путь, откат/провал шлюза, база сдвинулась, клон отстаёт, ветка уже есть, уже доставлена, не `Human Review`, подзадача, нет `[delivery]`, `cskwork`, `base` ≠ `base_branch`, нет/не совпал `.final.json`, блокировка занята) — ничего не запушено, PR не создан, ссылки на месте; сбой `gh pr create` и повтор; существующий PR; `deliver merged` и `check.py gate` (`tests: skipped`) для обоих видов, запись подзадачи шлюз карточки-задачи не принимает; `[delivery]` в загрузчике. В `test_check.py` — события `hook pass`; в `test_inbox_runner.py` — четвёртая команда и `.merge` для `deliver/<ID>`. Команды и итоги — в описании PR.

**Не проверено:** настоящий пуш на GitHub и `gh pr create` (в тестах — локальный голый репозиторий и подмена `gh`); учётные данные через `gh auth git-credential` под `launchd` — первая проверка боем на ближайшей карточке. Риск: `deliver` пушит на GitHub — только в `deliver/<ID>`, только без `--force`, только по адресу из `[delivery]`. После слияния `post-merge` пересоберёт `WORKFLOW.md` доски `oms` (менялся `project_config.py`) и запустит `project-doctor` (менялись тесты).

### 2026-10-28 — Решения архитектора: доставка `.deliver` (шаг 7)

Вводные архитектора к шагу 7 и его ответ на уточнения чата этапа — как есть. Принятые решения, не пересматривать.

1. Объём: и карточка-задача (`role:manager-plan`, после итоговой проверки), и ОБЫЧНАЯ карточка в `Human Review`. Основная выгода — обычные карточки (OMS-5, OMS-8 доставляли задачи Claude Code).
2. Ветки карточек не трогать. Доставка собирает и пушит отдельную ветку `deliver/<ID>`. Рабочая копия и ветки карточки (`symphony/<ID>`, `integration/<ЗАДАЧА>`) не меняются ни при успехе, ни при отказе.
3. Что доставляется (коммит D):
   - источник: карточка-задача — конец `integration/<ЗАДАЧА>`; обычная — конец `symphony/<ID>`;
   - «зелёная проверка» — по записям: карточка-задача — `.final.json` + событие `hook pass` с её деревом; обычная — последнее событие `hook pass` в `check.log` с деревом, равным дереву конца ветки (если надёжной записи для обычной карточки нет — добавить отметку по образцу `.pass.json`); нет зелёной проверки → отказ;
   - `our-main` не сдвинулся с зелёной проверки (у карточки-задачи — `our_main` из `.final.json`; у обычной — конец `our-main` уже предок ветки) → D = конец ветки;
   - сдвинулся → M = слияние двумя родителями (конец ветки + конец `our-main`) через `merge-tree` / `commit-tree`, без рабочих копий; конфликт → `needs_human` со списком файлов, ничего не пушится. M проверяется во временной рабочей копии ТЕМ ЖЕ механизмом, что замер `project-doctor` (`setup.sh` + полный `test.sh`), итоги — с историей замеров S4-25 (точка — конец `our-main`), плюс быстрые проверки `check.py` по диффу M относительно `our-main` (запрещённые пути). Зелёное → D = M; красное → `needs_human` с хвостом лога, ничего не пушится. Временная копия убирается всегда.
4. Пуш и PR:
   - куда: новый раздел `project.toml` `[delivery]`: `repo` (`"Serg10011/oh-my-symphony"`), `base` (`"our-main"`); пуш по явному URL этого репозитория (не полагаться на `origin` клона), ветка строго `deliver/<ID>`, без `--force`; ветка уже есть на GitHub → отказ с причиной; `repo` с владельцем `cskwork` и база `main`/`our-main` как ветка пуша — отказ всегда;
   - `gh pr create --repo <repo> --base <base> --head deliver/<ID>`; заголовок — «<ID>: <название карточки>»; описание — отчёт As-Is → To-Be из карточки, для карточки-задачи — список подзадач, итог проверки (какой коммит, повторная проверка была или нет);
   - после: раздел `## Delivery` в карточке (PR, коммит D, `our-main`), запись `.ours-state/<ID>.delivered.json`, `git fetch` в основной папке. Карточка остаётся в `Human Review`.
5. `.merge` принимает и `deliver/<ID>` (как `dev/<код>`): `gh pr merge` + `post-merge`. `Done` ставит Сергей (веб-доска, State → `Done`). Оценить: можно ли при этом не гонять шлюз повторно (запись «влито» с деревом D по образцу S4-13) — если просто, сделать; если нет — отдельно.
6. Пушит только эта команда; агенты и служба не пушат (как было). Добро Сергея для `.deliver` не нужно, для `.merge` — нужно.
7. Нарезка — на усмотрение чата этапа, но задания Claude Code держать небольшими: например, А — доставка без повторной проверки (D = конец ветки) + пуш + PR + `.merge` для `deliver/<ID>`; Б — слияние с новым `our-main` и проверка во временной копии.

**Ответ архитектора на уточнения чата этапа.** Решения 1–4, 6, 7 чата этапа — да: (1) отметка `tree`/`head` в событии `hook pass`, зелёная проверка — последнее решающее событие (`hook pass`, `hook rollback:*`, `gate fail`); (2) клон не должен отставать от GitHub; (3) `[delivery] base` = `[project] base_branch`; (4) продолжение после сбоя `gh pr create`; (6) `.deliver` можно повторять, как `.test`; (7) `.merge` для `deliver/<ID>` — только для репозитория форка, другие — с подключением EVE. Дополнительно:

- А. «Лёгкий путь» — не зелёная проверка для доставки (решение S4-16, п. 4: полный прогон перед доставкой обязателен независимо от `doc_paths`; `post-merge` после слияния одних документов тестов не гоняет). Зелёная проверка — последнее решающее событие `hook pass` с `tree` = дереву конца источника И `tests = "full"`. `tests = "skipped-docs"` → отказ «проверка шла лёгким путём: полный прогон перед доставкой — S4-28». S4-28 гоняет полный набор во временной копии, когда `our-main` сдвинулся ИЛИ зелёная проверка была лёгкой.
- Б. Перед `gh pr create` (и при продолжении после сбоя) — сначала `gh pr list --repo <repo> --head deliver/<ID> --state open`; PR уже есть → записать его в `.delivered.json`, второй не создавать (случай «PR создан, ответ потерян»).
- В. П. 5: запись «влито» после слияния пишется для ОБОИХ видов карточек, с `source = deliver/<ID>` (и `commit` = D, `tree` = дерево D). `check.py gate` карточки-задачи принимает ТОЛЬКО записи с `source = deliver/<её ID>` (записи подзадач — по-прежнему нет). Основание: дерево доставки прошло полный прогон (у карточки-задачи — итоговая проверка; у обычной — по п. А). Правило S4-21 в DECISIONS дополнить этой оговоркой.

### 2026-10-28 — S4-25 — история замеров базы, `## Subtask Progress` одним разделом, правило `check`

Модель: Sonnet. Основа: `docs/our/tasks/TASK-S4-25.md`. Ядро (`src/`, `tests/`) не менялось. Правились `ours/bin/project-doctor`, `ours/hooks/project_config.py`, `ours/hooks/check.py`, `ours/hooks/post_merge.py`, промпт `plan.md`, тесты `ours/tests/` и документы.

**Что и почему.**

- **История замеров.** Раньше `check.py` сравнивал итоги `test.sh` карточки с одним замером `baseline.json`, а `project-doctor` перезаписывал его при каждом замере базовой ветки. Если `our-main` получал новые тесты, пока карточка работала, у карточки `passed` было меньше нового замера — ложный отказ (случай OMS-8). Теперь `project-doctor` ведёт `<доска>/.ours-state/baselines.json` (`{"version": 1, "records": [...]}`, одна запись на коммит ветки, последние 50), а `baseline.json` пишет как раньше (последний замер; его читают `apply_timeouts`, люди и тесты). Неудачный замер не меняет ни один файл. Если истории ещё нет, первой записью становится прежний `baseline.json` (с настоящим, не нулевым `base_sha`).
- **С чем сравнивается карточка.** `check.py` берёт точку ответвления — `git merge-base HEAD refs/heads/<base_branch>` (у подзадачи это `our-main` момента плана, а не интеграционная ветка; ошибка `git` — отказ) — и сравнивает с замером этой точки. Строго: запись точно на точке (`exact`) → иначе ближайшая запись-предок, если между ней и точкой не менялись файлы тестов (`ancestor`) → иначе замера нет (`unknown`): тесты идут, отказ только при коде не 0, без строки итогов или `failed > 0`; в `check.log` событие `baseline_unknown`. Журнал `pass` / `fail` несёт `baseline_match`, `baseline_sha`, `baseline_point`.
- **Правило «предок без изменений тестов»** (согласовано архитектором 28.10.2026). `post-merge` запускает `project-doctor`, только когда слияние меняет файлы, влияющие на тесты; после слияния одних документов (S4-03, S4-05, S4-14, S4-26) конец `our-main` не замеряется (на момент задачи `our-main` = `b9eb90e` без замера, последний замер — S4-24, `8766b04`). Без этого правила каждая карточка, начатая после такого слияния, шла бы без сравнения. Правило решает и «перемерять ли» (`post-merge`), и «верить ли старому замеру» (`check.py`), поэтому оно в одном месте — `project_config.BASELINE_RULE` / `affects_baseline(path)`; `post_merge.RULES["doctor"]` берёт его оттуда.
- **Расширение правила `doctor`** (решение архитектора): добавлены `ours/tests/` (вспомогательные файлы тестов, например `ours/tests/e2e/fake_claude.py`, меняют исход) и `conftest.py` на любом уровне (раньше ловился только корневой). Последствие: правки в `ours/tests/` теперь вызывают перемер в `post-merge`. Правило рассчитано на Python; для проектов на других языках (EVE: Vitest) понадобится настройка правила по проекту в `project.toml` — сделаем в задаче подключения EVE.
- **Режим `legacy`.** Нет `baselines.json` — всё как до S4-25, по `baseline.json` (на этом стоят существующие тесты `test_check.py`, `test_final_check.py`, `test_light_path_e2e.py`). Есть, но повреждён (или нет записей для `base_branch`) — отказ «перезапусти project-doctor», откат к `baseline.json` не делается. После слияния `post-merge` сам запустит `project-doctor`, и на доске `oms` история появится.
- **`## Subtask Progress` одним разделом** (находка (а) S4-22). `check.append_note` для заголовка `Subtask Progress` (перечень `APPEND_TO_SECTION`) дописывает строку в конец существующего раздела (у карточек с несколькими такими разделами — в последний), а не новым разделом; остальные заголовки — как раньше. Подпись и путь вызова не менялись. **Зависимость от частного метода ядра:** публичного «дописать в раздел» у `FileBoardTracker` нет, а `update_fields(description=…)` заменяет тело целиком и потеряла бы правку, сделанную между чтением и записью, поэтому тело меняется через `FileBoardTracker._mutate_ticket` (под блокировкой карточки, с проверкой `updated_at`; `updated_at` обновляется, отсчёт автоархива сбрасывается). При обновлении ядра проверить, что метод на месте; запасной путь (файл под блокировкой `.locks/<ID>.lock`) от него не зависит.
- **Правило `plan.md`** (находка (б) S4-22): `check` доказывает результат **своей** подзадачи, а не результат другой подзадачи и не предусловие. Хук `plan.py` не менялся: механически это не проверить.
- **Уточнение к находке (г) S4-22.** Подтверждение `Done` в веб-доске есть: `PATCH /api/v1/issues/<ID>` со `state: Done` для карточки в `Human Review` вызывает тот же `orchestrator.confirm_done`, что маршрут `confirm-done` и клавиша `c` (`src/symphony/webapi/issue_routes.py`, `handle_issue_patch`); и перетаскивание в колонку, и поле State в панели карточки шлют этот `PATCH`. Из других статусов выбор `Done` — обычная смена статуса, шлюз не запускается. Кнопка «Confirm done» не нужна (решение архитектора 27.10.2026) и снята из п. 3 «Порядка после шага 6». `HOWTO-TASKS.md` исправлен.
- **Проверено.** `ruff check ours` и полный `ours/tests` (в урезанном окружении, как у службы) — результат в описании PR. Новый файл `ours/tests/test_baselines.py`; в существующих тестах изменено только ожидание в `test_final_check.py::test_done_of_a_subtask_writes_a_progress_line_into_the_task_card` (строка теперь идёт под `0 of 2 merged`). В работающей службе не проверялось.

### 2026-10-27 — Итоги проверки боем нарезки (S4-22, OMS-9)

Модель задачи S4-26: Sonnet. Основа: `docs/our/tasks/TASK-S4-26.md`. Факты — из `check.log` и `stats.jsonl` доски `oms` (перепроверены по файлам). Ядро не менялось.

- **Нарезка.** Карточка-задача OMS-9 (`complex`, Sonnet `high`) создана сразу в `Plan`. Руководитель отработал 1 ход за 43 с; план принят с первого раза (событие `accepted`), создано 3 подзадачи: OMS-9-1 памятка (`complex`), OMS-9-2 ссылка в `ours/README.md` (`simple`, `blocked_by` OMS-9-1), OMS-9-3 словарь (`simple`, без зависимостей).
- **Подзадачи.** По 2 хода (`In Progress`, `Verify`), без возвратов: OMS-9-1 ≈ 3,6 мин, OMS-9-2 ≈ 7,7 мин (полный `test.sh`: `ours/README.md` вне `doc_paths`), OMS-9-3 ≈ 2,9 мин. OMS-9-1 и OMS-9-3 прошли лёгким путём (`tests: skipped-docs`). Каждая влита плоским коммитом, `Done` поставила программа, шлюз не гонял тесты повторно (`tests: skipped`). Раздел `## Subtask Progress` писался после каждой подзадачи.
- **Итоговая проверка.** После последней подзадачи `finish_task` сам перевёл карточку в `Verify` (`final:started`, `merged: false` — `our-main` не двигался, на время боя был запрет слияний). Verify и Document итоговой проверки — 2 хода; полный `test.sh` в `Human Review` прошёл (`tests: full`, причина `task card: full suite`). От создания до `Human Review` ≈ 24 минуты, 9 ходов.
- **Не проверено боем:** слияние с `our-main` двумя родителями (`our-main` не двигался); провал плана и возврат в `Plan`; `needs_human` подзадачи; ручная `finish-task`.
- **Находки.**
  - (а) `## Subtask Progress`: каждая строка добавляется новым разделом с тем же заголовком (в карточке 4 заголовка). Исправить: дописывать в существующий раздел — в S4-25 (`integrate.py`, `check.append_note`).
  - (б) Команда проверки OMS-9-2 `test -s docs/our/HOWTO-TASKS.md` проверяет наличие памятки, а не ссылки. Хук формально прав — это вопрос качества плана.
  - (в) Ложный отказ шлюза OMS-8 (замер базы перемерен после слияния). Решение архитектора: S4-25 — история замеров `baselines.json`, сравнение с замером точки ответвления.
  - (г) В веб-доске нет «Confirm done». Подтверждение — `curl -X POST http://127.0.0.1:<порт>/api/v1/<ID>/confirm-done` или клавиша `c` в терминальной доске (TUI).
- **Доставка** сделана вручную задачей S4-26: ветка `integration/OMS-9` (коммиты `e36726f`, `335f636`, `6847b44`) влита в `dev/S4-26` слиянием `--no-ff`, затем отдельным коммитом исправлены замечания Verify к памятке.
- **Решение: шаг 6 закрыт.**

**Порядок после шага 6** (решение архитектора, 27.10.2026):

1. **S4-25** — история замеров базы (`baselines.json`, сравнение с замером точки ответвления) + исправление `## Subtask Progress` (дописывать в существующий раздел) + правило в `plan.md`: «`check` проверяет результат **своей** подзадачи (то, что она меняет), а не результат другой подзадачи или предусловие».
2. **`.deliver` (шаг 7)** — сразу после S4-25: сейчас каждая доставка — отдельная задача Claude Code и лимит подписки; решения — запись «Решения архитектора: итоговая проверка задачи и доставка (шаги 6–7)».
3. **PR ядра:** сквозная нумерация карточек (доска хранит наибольший выданный номер под той же блокировкой, что создание карточки; выдача = max(хранимый, существующие) + 1; удалённые номера не возвращаются; наши `create-*.py` — без своего подсчёта номера) + прокрутка доски. *(Кнопка «Confirm done» снята 28.10.2026: подтверждение в веб-доске уже есть, см. запись S4-25.)*
4. `[plan] max_subtask_level` + заготовка `project-new` под таблицу уровней.
5. Статус `Merging` для подзадач.
6. Команда уборки `.clean` (после PR нумерации).
7. Шаги 8–9 (`role:manager-diagnose` и `[ladder]`; PR ядра №3 `stage_levels` и стратегии).

Подключение доски EVE (копия репозитория, проба после 30.10.2026) — после S4-25, параллельно с `.deliver`; план подключения — отдельно. Отдельно: первое настоящее слияние `our-main` двумя родителями в `finish_task` (когда `our-main` будет двигаться во время задачи) — смотреть внимательно при приёмке.

### 2026-10-27 — Таблица уровней (решение архитектора)

> **Изменено 29.10.2026** (запись «Решения архитектора: выбор модели, лестница, таблица уровней» ниже): строки `simple` (Haiku `low`) и `standard` (Haiku `high`) таблицы ниже и правило «если +2 за max — модель следующего уровня» заменены; `complex` и `critical` без изменений. Ниже — запись 27.10 как она была.

На доске `oms` 27.10.2026 таблица уже изменена (скрипт `tmp_workspaces/set-oms-level-table.py`; проверка `claude --version` ≥ 2.1.293 прошла, `project-render` выполнен). Заготовка `project-new` пока **не менялась**: это задача после S4-22 (ниже).

| Уровень | Модель | Усилие | Что изменилось |
|---|---|---|---|
| `simple` | Haiku | `medium` | без изменений |
| `standard` | Haiku | `xhigh` | было `high` |
| `complex` | Sonnet | `high` | без изменений |
| `critical` | Opus | `medium` | было Sonnet / `max` |

- **Почему так.** С версии Claude Code 2.1.293 псевдоним `haiku` — это Haiku 5.5, и у неё есть все ступени усилия. Haiku 4.5 усилие не поддерживала — отсюда запись K14 «влияние `--effort` на haiku не видно»: теперь усилие у Haiku работает.
- **Принцип.** Соседние уровни отстоят друг от друга не меньше, чем на шаг повтора работника (+2 ступени усилия, K14). Иначе повтор задачи нижнего уровня оказывается сильнее первого захода следующего, и уровни теряют смысл.
- **`critical` = Opus `medium`.** Для редких задач с высокой ценой ошибки сила модели важнее длины рассуждения.
- **Правило лестницы для шага 8.** Если повтор на +2 ступени усилия выходит за `max`, повтор идёт на модели следующего уровня.
- **Решение: `[plan] max_subtask_level`.** Потолок уровня подзадач задаётся отдельно от уровня карточки-задачи: руководитель может работать на Opus, а работники — не выше заданного. Задача — первая после S4-22, вместе с обновлением заготовки `project-new` под эту таблицу.
- **До этой задачи** задачам для нарезки давать уровень `complex`.

### 2026-10-27 — S4-20 — промпт нарезки карточкой на доске `oms` (OMS-8)

Основа: `docs/our/tasks/TASK-S4-24.md` (карточка S4-20 лежит на доске `oms`). Ядро не менялось. Модель правок S4-24: Sonnet.

- **Карточка.** Обычная карточка на доске `oms`, уровень `standard` (Haiku `high`, до смены таблицы). 3 хода, ≈ 23 минуты, ≈ 5,6 млн токенов (по веб-доске), без возвратов. Полный `test.sh` прошёл в `check.py hook`. Результат — `ours/prompts/stages/plan.md`, строка S4-20 в `ours/prompts/CHANGES.md`, новый `ours/tests/test_plan_prompt.py`.
- **Замечание Verify.** AC1 и AC4 отмечены «Not proven»: по бюджету тестов агент их не запускал (прямой `pytest` конвейерному агенту не разрешён). Доказательство дал полный прогон в `check.py hook`: новый тест входит в набор.
- **Четыре замечания `## Review Notes`, исправлены в S4-24.** (1) «Do NOT» запрещало править любые файлы, хотя одну запись — `plan.toml` — разрешает; добавлено исключение. (2) Список запрещённых путей в промпте был неполным (`ours/**`, `.env.*`). (3) У `title` не сказано «действие + объект, с глагола». (4) Пример `check` — заглушка `<board folder>/test.sh`, которую хук пропустил бы, а у работника она не запустилась бы.
- **Решение архитектора по существу замечаний: правила держит код, программа готовит сведения, промпт общий для всех проектов** (у EVE не будет `ours/`, будут свои запреты и команды). Поэтому:
  - **Запрещённые пути не перечисляются в промпте.** `plan.py brief` пишет в `limits.md` раздел `## Forbidden paths` из `check.load_config(<доска>)` через новую функцию `check.forbidden_rules` — один источник для проверки `paths` в хуке (`forbidden_violations`) и для сводки. Учтены запреты по умолчанию, проектный `forbidden_paths`, контур контроля, секреты и исключения `allow_paths` (на доске `oms` открыты `ours/prompts/**`, `ours/README.md`, `ours/tests/test_*.py`).
  - **Команда проверки проверяется хуком.** `plan.py check_plan` требует, чтобы `check` начинался с разрешённой агенту команды — список тот же, что `--allowedTools` (`project_config.agent_bash_rules`: `/bin/bash <доска>/test.sh`, база `BASE_ALLOWED_BASH`, `[agent] allowed_bash`). Граница префикса — пробел или конец строки; `&&`, `||`, `;`, `|`, `$(`, `>`, `<` запрещены. `limits.md` получил разделы `## Test command` (точная строка) и `## Allowed check commands`. Пример в промпте — `test -s docs/our/GLOSSARY.md`.
  - `check_plan` получил необязательный параметр `board`: без него правило команд не применяется (вызов как библиотеки); хук всегда передаёт доску.
- **Доставка — вручную этой задачей** (S4-24); автоматическая `.deliver` — шаг 7.
- **Правка данных существующего теста** (по уточнению архитектора): в `ours/tests/test_plan_hook.py` образец подзадачи имел `check = "/bin/bash /somewhere/test.sh"` (чужой путь, новое правило его законно отклоняет) — заменён на `test -s docs/a.md` и ожидание тела карточки в одном тесте. Смысл тестов не менялся; больше ни один прежний тест не упал.
### 2026-10-27 — S4-21 — выход из `Subtasks` на итоговую проверку

Основа: `docs/our/tasks/TASK-S4-21.md`; решения архитектора «итоговая проверка задачи и доставка (шаги 6–7)», п. 1–3. Ядро (`src/`, `tests/`) не менялось. Правились: `ours/hooks/integrate.py`, `ours/hooks/check.py` (контур контроля — задачу запустил человек), `ours/hooks/project_config.py` (одна функция `default_workspace_root`), `ours/hooks/plan.py` (две константы теперь берутся из `integrate.py`), промпты `verify.md` и `document.md` (строки в `ours/prompts/CHANGES.md`), один существующий тест, новый `ours/tests/test_final_check.py`, документы.

**Как это работает по-простому.** Карточка-задача (метка `role:manager-plan` и раздел `## Plan Accepted` — одна функция `integrate.is_task_card`, её используют все) ждёт в `Subtasks`, пока работники делают подзадачи. Каждая вливаемая подзадача после `Done` записывает в карточку-задачу строку хода; последняя же сама запускает итоговую проверку всей задачи. Если последнюю подзадачу в `Done` перевёл человек, итоговую проверку запускает команда `integrate.py finish-task <ЗАДАЧА> [--board <папка доски>]` (код 0 — переведена в `Verify`, 1 — нет, причина в выводе по-русски).

1. **`## Subtask Progress`.** После успешного `Done` (только если `Done` действительно встал) в карточку-задачу (`request` подзадачи) добавляется `- <ПОДЗАДАЧА> merged (k of N)`: `N` — число строк `## Plan Accepted`, `k` — сколько из них сейчас в `Done`. Запись — `check.append_note`, обобщённая параметром `issue_id` (трекер, запасной путь — файл), под блокировкой карточки-**задачи**. Правка карточки сбрасывает отсчёт автоархива ядра (30 дней). Ошибка записи — только событие `error`, `Done` подзадачи не страдает. Трекер добавляет каждый раз новый раздел с тем же заголовком, поэтому в карточке будет несколько `## Subtask Progress` подряд — по одной строке на подзадачу, вместе с исходным `0 of N merged` от `plan.py`.
2. **Сигнал и блокировки.** `finish_task` вызывается **после выхода из всех блокировок вливания подзадачи** (`_run_hook`: блокировка подзадачи отпущена, затем `_signal_task`), потому что `fcntl.flock` не вкладывается: второе открытие того же файла блокировки в том же процессе ждёт само себя. Сам `finish_task` берёт блокировку карточки-задачи и `worktree_lock` заново. Сигнал срабатывает, когда карточка-задача в `Subtasks` и **все** ID из `## Plan Accepted` в `Done`; одна подзадача в `needs_human` или в работе — задача остаётся в `Subtasks`. Любая ошибка — событие `error` и строка в stderr. Если карточки задачи на доске нет или в ней нет `## Plan Accepted` (подзадача сделана вручную), хук молчит — ни строки, ни события (иначе пришлось бы менять существующие тесты вливания).
3. **`finish_task`.** Под блокировкой карточки-задачи и `worktree_lock`: (а) условия — рабочая копия `<корень>/<ЗАДАЧА>` есть, это копия репозитория кода на `symphony/<ЗАДАЧА>`, она чистая (`git status` по `check.STAGE_PATHSPEC`), в ветке руководителя нет правок относительно записи базы (`git diff --quiet <base_sha> HEAD`), ветки `integration/<ЗАДАЧА>` и базовая есть; (б) слияние — если `main` предок `int`, слияния нет; иначе `git merge-tree --write-tree` (конфликт — `needs_human` со списком файлов), `git commit-tree -p <int> -p <main>` (сообщение `<ЗАДАЧА>: merge <база> for the final check`, автор — `symphony`, как у `merge_subtask`), `git update-ref` с проверкой старого значения (один повтор, вторая неудача — `needs_human`); (в) `git reset --keep <цель>` в рабочей копии, проверка `HEAD == цель`; (г) дифф для Verify; (д) запись базы; (е) `.final.json`; (ж) раздел `## Final Check` и `board mv <ЗАДАЧА> Verify` с проверкой, что статус сменился; событие `final:started`. Любая проблема — `needs_human`, раздел `## Final Check` (причины, файлы конфликта, **что уже сдвинуто в ветках**), событие `final:needs_human`. Корень рабочих копий: `$SYMPHONY_WORKSPACE_ROOT` (его служба выдаёт хукам), руками — `~/symphony_workspaces/<имя проекта>`, как в шаблоне `WORKFLOW.md` (`project_config.default_workspace_root`).
4. **Дифф для Verify готовит программа.** `git diff` агенту не разрешён (`BASE_ALLOWED_BASH`), а `git merge-tree` диффа не показывает; права агента не расширялись. Файлы пишутся со стороны доски, не сквозь ссылку (та же защита, что у `plan.py`): `work/final-diff.md` (`--name-status`, `--stat`, какие коммиты сравниваются) и `work/final-diff.patch` (полный `git diff <main>..<цель>`; больше 200 КБ — обрезка по границе строки и последняя строка `обрезано, N КБ всего, полный дифф: git diff <main> <цель>`). Сравнение `main..цель` показывает именно правки задачи, а не правки `our-main`, потому что `main` — родитель коммита слияния.
5. **Запись базы и `.final.json`.** `base_sha` в `<ЗАДАЧА>.base.json` = использованный `main` (`final_from` = прежний; остальные поля сохранены), `git config --worktree symphony.basesha`; `check.py` теперь видит правки `our-main` не как правки задачи (запрещённые пути считаются от нового `base_sha`, он предок `HEAD`). `<ЗАДАЧА>.final.json`: `task`, `branch` (= `integration/<ЗАДАЧА>`, ветка, которую будет пушить доставка), `integration_commit` (цель), `our_main`, `merged`, `at`. Шаг 7 (`.deliver`) сравнивает `our_main` с текущим концом базы (сделано в S4-27, часть А).
6. **`check.py` для карточки-задачи.** (а) Пропуск по `doc_paths` не применяется ни в `hook`, ни в `gate`: причина `task card: full suite (S4-16 п. 4)` в отчёте и в событии (`tests: full`, `tests_reason`). (б) Пропуск по записи «влито» у неё не встречается (`integrate.py` вливает только подзадачи); для надёжности шлюз карточки-задачи записи не использует (одноразовая запись всё равно забирается), тест подкладывает подделанную запись с совпадающим деревом — тесты идут. **Оговорка (S4-27):** кроме записи доставки с `source = deliver/<её ID>` (её пишет `ours/bin/deliver merged` после слияния PR доставки; запись подзадачи, запись без `source` и любая другая по-прежнему не принимаются). (в) Провал `hook` сразу ведёт в `needs_human`, не в `In Progress`: там карточка получила бы промпт работника и чинила бы код сама, мимо подзадач; разбор ошибок итоговой проверки — шаг 8 (`role:manager-diagnose`), до него — Сергей. `ROLLBACK_STATES` не расширялся; в `## Check Failure` добавлена строка `task card: straight to needs_human`. (г) Обязательные разделы — обычные (`## Wiki Updates`, `## As-Is -> To-Be Report`, пишет Document); карточка-задача не подзадача (нет `request:` и метки работника) — закреплено тестом.
7. **Промпты.** `verify.md`: ветка по метке `role:manager-plan` в конце файла (тот же приём, что в `todo.md`; отрисовка для остальных карточек не изменилась — проверено сравнением с прежним файлом и тестом). Решения архитектора: сверять критерии описания, `## Plan Accepted`, карточки подзадач `kanban/<ЗАДАЧА>-N.md` (`## QA Evidence`, `## AC Scorecard`) и дифф из файлов `final-diff.*`; явно сказано, что разделов `## Plan`, `## Acceptance Tests`, `## Done Signals` у карточки-задачи нет — это нормально; нарушение по существу — `## Review Findings` и `needs_human`; чисто — `Document` (не `Human Review`: у карточки-задачи нет `request:`, ветка подзадачи в шаге 9 её не касается); итоги подзадач сводятся в `qa/final-check.md`; полный набор тестов Verify не запускает. `document.md`: понадобилась минимальная приписка — шаг 1 читает `## Plan`, `## Implementation`, `## Self-Critique`, которых у карточки-задачи нет, и агент мог принять это за `## Document Defect`; в ветке сказано, что читать вместо них и что вики и отчёт As-Is → To-Be покрывают всю задачу.

**Выводы по коду (проверено).**
- *Перевод `Subtasks` → `Verify` не расходует `max_reopens` и `max_attempts`.* `max_reopens`: `blocked_recovery.py::_prior_done_run_count` считает только прошлые завершённые запуски, дошедшие до `Done`; запуск руководителя кончается в `Subtasks`. `max_attempts`: `rewind_count` растёт в `attempt.py` (≈1116) и `agent_phase.py` (≈181) только при переходе внутри запуска; перевод извне идёт между запусками, а новый запуск начинается с `prev_phase_state = Verify` (`attempt.py` ≈588).
- *Ядро возьмёт карточку в `Verify` с сохранённой рабочей копией.* `Subtasks` в `preserve_workspace_states` (S4-18); умолчание `workspace.reuse_policy` — `preserve` (`workspace.py::create_or_reuse`): существующая папка не пересоздаётся и `after_create` не запускается; даже в режиме «обновить» `worktree.py create` только восстанавливает ссылки (рабочая копия на `symphony/<ЗАДАЧА>`). Быстрая перемотка `_refresh_linked_worktree_in_place` срабатывает только когда ветка — предок цели и не равна ей; после `finish_task` ветка содержит `our-main`, так что не срабатывает.
- *Контракт этапа при входе в `Verify` извне не проверяется:* контракт считается по состоянию, из которого уходят (`producing_state`), а у нового запуска он начинается с `Verify`.
- *`## AC Scorecard` ссылается только на файлы своей карточки:* `contracts.py::_cited_path_failures` → `_row_evidence_failures` проверяет `docs_root / <идентификатор карточки> / <qa|work/…>`; ссылка `docs/<ЗАДАЧА>-1/qa/…` не начинается с `qa/`/`work/` после снятия префикса своей карточки и считается «нет ссылки». Поэтому итоги подзадач сводятся в `qa/final-check.md`.
- *`symphony.mergetargetbranch` / `basebranch` у карточки-задачи = базовая ветка:* `worktree.py` (≈357) для карточки без метки работника ставит `start = base`. Закреплено тестом; на всякий случай `finish_task` сам исправляет расхождение. Предварительная проверка слияния в `## Merge Status` (Verify) покажет слияние именно в `our-main`.

**Отступления и уточнения к заданию (маленькие).**
- *Ссылка на `worktree_lock` в задании неточна:* «`merge_subtask` уже работает внутри `worktree_lock` (≈ стр. 540)» — по коду стр. 540 это `ensure_branch_command`, а `merge_subtask` блокировку рабочих копий не берёт (держит только блокировку подзадачи в `_run_hook`). Требование выполнено в сути: `finish_task` вызывается вне всех блокировок вливания.
- *«Не время» — не `needs_human`.* Не карточка-задача, не в `Subtasks` или не все подзадачи в `Done` — отказ с причиной (событие `final:refused`, код 1, ничего не меняется). Задание велит `needs_human` «при любой проблеме»; для ранней ручной команды это было бы наказанием карточки за чужую поспешность, а автоматический путь сюда не доходит (проверка готовности перед вызовом). Всё остальное (рабочая копия, ветка руководителя, конфликт, сбой после сдвига) — `needs_human`.
- *Порядок «заметка → `Verify`».* Раздел `## Final Check` пишется до `board mv`, чтобы агент, которого служба возьмёт сразу, его уже видел; если перевод не удался — второй `## Final Check` с `needs_human` и пометкой, что сдвинуто.
- *Шапка `Final Check`:* ключи — `integration tip before`, `<база> tip`, `result` (`merge commit <sha>` или `no merge needed`).

**Правка существующего теста (задание, ч. 6).** `ours/tests/test_planning_states.py`: `test_the_plan_placeholder_renders_and_hands_the_card_to_a_human` переименован в `test_the_plan_prompt_renders_by_the_core_engine_and_is_not_empty` и теперь проверяет только «отрисовывается движком ядра и не пуст» — без текста заглушки и без `needs_human` (проверки содержания настоящего промпта добавит S4-20 новым файлом). Других существующих тестов не менялось; тестов, проверяющих полный текст `verify.md`, нет (`test_light_path_e2e.py` ищет в нём отдельные фразы — они на месте).

**Как проверено.** `ours/tests/test_final_check.py` (49 тестов; настоящий git во временных репозиториях, настоящий `symphony board` рабочей копии, настоящие блокировки): строки хода и сбой записи; последняя подзадача → `Verify`, не последняя и подзадача в `needs_human` — остаются в `Subtasks`; слияние (не сдвигался — без коммита и `merged: false`; сдвигался — коммит с двумя родителями `[int, main]`, обе ветки на нём; конфликт — `needs_human`, список файлов, ветки и рабочая копия не изменились; сдвиг во время слияния — повтор, вторая неудача — `needs_human`); грязная копия, правки в ветке руководителя, нет копии, копия на другой ветке — `needs_human` и снимок всех веток/записей без изменений; не время — отказ без перевода; запись базы, `.final.json`, дифф и его обрезка, запись не сквозь ссылку; `check.py` на копии после `finish_task` (база — предок `HEAD`, запрещённый файл `our-main` не считается правкой задачи, полный прогон в `hook` и `gate`, подделанная запись «влито» ничего не пропускает, провал `hook` — сразу `needs_human`, обычная карточка — по-прежнему `In Progress`); команда `finish-task` (коды 0, 1, 2, вывод по-русски); путь `integrate.py hook` → `finish_task` отдельным процессом с настоящими блокировками укладывается в 30 с; ветки `verify.md` и `document.md` движком ядра. Команды и итоги — в описании PR.

**Не проверено:** в работающей службе и с настоящим `claude` — это проверка боем S4-22 (в частности, что служба берёт карточку из `Verify` после внешнего перевода, как мы прочитали по коду); не проверено и поведение `git reset --keep` на большой рабочей копии `oms` (в тестах — маленькие репозитории). Риск: `finish_task` двигает ветки в репозитории кода (`integration/<ЗАДАЧА>`, `symphony/<ЗАДАЧА>`) — только с проверкой старого значения и только при чистой рабочей копии; после слияния `post-merge` пересоберёт `WORKFLOW.md` доски `oms` (изменились промпты).

**Модель:** Sonnet, effort=high.

### 2026-10-27 — S4-19 — хук «план-файл → карточки»

Основа: `docs/our/tasks/TASK-S4-19.md`; шаг 6 порядка архитектора. Ядро (`src/`, `tests/`) и промпты (`ours/prompts/**`) не менялись. Новое: `ours/hooks/plan.py`, `ours/tests/test_plan_hook.py`; правки `ours/hooks/project_config.py` (раздел `[plan]`, плейсхолдер `plan_py`), `ours/templates/WORKFLOW.md.tmpl` (две строки), `ours/bin/project-new` (заготовка), документы.

**Как это работает по-простому.** Руководитель (карточка с меткой `role:manager-plan`, статус `Plan`) пишет план в файл и сдаёт его, переводя карточку в `Subtasks`. Агент сам карточек не создаёт и ничего не вызывает (права агента не расширялись). После хода `plan.py hook` читает файл **со стороны доски**, проверяет, и если всё в порядке — сам создаёт карточки подзадач. План сдан = метка `role:manager-plan` **и** статус `Subtasks` **и** нет раздела `## Plan Accepted`; любая другая карточка пропускается молча (хук идёт после каждого хода каждой карточки); карточка в `Subtasks` без метки — событие `skipped` с причиной, ничего не двигается.

**Формат план-файла `plan.toml`** (его читает карточка S4-20). Путь: `<доска>/artifacts/<ЗАДАЧА>/work/plan.toml`; в рабочей копии агента это `docs/<ЗАДАЧА>/work/plan.toml` (`docs/<ID>` — ссылка в папку доски). Обычный файл (не ссылка и не файл за ссылкой в пути), до 64 КБ, UTF-8. Неизвестный ключ — ошибка (опечатка не должна пройти молча).

```toml
version = 1                            # ровно целое 1

[[subtask]]                            # от 1 до [plan] max_subtasks (5) блоков, порядок = порядок создания
key = "glossary"                       # [a-z0-9-]{1,32}, уникален в плане
title = "Добавить словарь терминов"    # одна строка, ≤ 80 знаков, без пробелов по краям, не с «-»
level = "simple"                       # имя из [levels] project.toml, не выше уровня задачи
level_reason = "одна страница текста, кода нет"   # одна строка, обязательна
goal = """Что сделать, по существу (несколько строк можно)."""
criteria = ["Результат по существу 1", "…"]       # 1–5 строк, по правилу критериев из CLAUDE.md
check = "/bin/bash /Users/…/symphony-projects/oms/test.sh"   # одна строка: чем проверить
paths = ["docs/our/GLOSSARY.md"]       # область файлов: 1–20 шаблонов путей от корня репозитория
blocked_by = []                        # ключи подзадач ВЫШЕ в файле; можно опустить (= пусто)
non_goals = ["…"]                      # необязательно, 0–5 строк
```

Все строки — без управляющих символов; переводы строк допустимы только в `goal`; в `goal` нельзя строки-заголовки Markdown (`#`, `##`…): из них получился бы раздел карточки; в `check` и `paths` нельзя обратную кавычку (они выводятся в кавычках-«кодовых» спанах).

**Проверки плана.** Каждая провальная — строка в `## Plan Failure`; собираются **все** причины, а не первая. (1) файл есть, обычный, ≤ 64 КБ, разбирается `tomllib`, `version = 1`, есть хотя бы одна подзадача, их не больше `max_subtasks`; (2) ключи и типы строго по схеме, длины и количества как в комментариях выше; (3) `key` уникальны, `blocked_by` — только на ключи **выше** в файле и не на себя (так циклов не бывает, а порядок создания задан); (4) `level` есть в `[levels]` и **не выше уровня задачи**: порядок уровней — порядок их объявления в `[levels]` `project.toml` (в заготовке `simple` → `standard` → `complex` → `critical`; правило записано в комментарии заготовки); уровень задачи — `level:` её карточки, иначе `default_level`, нет ни того ни другого — ограничения нет; (5) `paths`: относительные, без `..`, `.`, пустых сегментов и ведущего `/`, и **ни один шаблон не попадает в запрещённые пути и контур контроля** — `check.forbidden_violations` с настройками доски, по самому шаблону и (если есть `*`, `?`, `[`) по его префиксу до первого такого знака; `allow_paths` открывает `ours/**` как и для `check.py`, но не контур (`ours/hooks/**`, `ours/bin/**`, `ours/templates/**`). Подзадача, которой заранее нечего делать в разрешённом, — ошибка плана, а не будущий отказ `check.py`.

**Сразу `needs_human`, без траты попытки плана** (решение архитектора: руководитель этого не исправит; счётчик провалов не растёт). Проверяется **до** разбора плана; причины перечисляются все сразу в `## Plan Failure`, событие `rejected` с пометкой `needs_human: environment`: интеграция выключена; на доске уже есть хотя бы одна карточка `<ЗАДАЧА>-N`; `[levels]` в `project.toml` пуст; ещё — из ID задачи не получаются ID подзадач (точки ядро не пускает, до 64 знаков), ветку `integration/<ЗАДАЧА>` создать не удалось, созданные карточки удалить не удалось (отступление от задания, см. ниже). Всё остальное — провал плана.

**Принятие.** (1) `integrate.ensure_integration_branch` под общей блокировкой рабочих копий (существующая ветка не трогается, как в S4-11); (2) карточки по порядку файла командой `symphony board new <ЗАДАЧА>-N "<title>" --root <доска>/kanban --state "In Progress" --labels <worker_label> --request <ЗАДАЧА> --level <level> --blocked-by <ID> … --description-file <временный файл>` (путь к `symphony` — `check.resolve_symphony_bin`, без оболочки). По `cli/board.py` и `trackers/file.py` проверено: все эти ключи есть; `--root` пишет в тот же `kanban/`, что читает служба (`board_root: ./kanban`); при `--root` допустимы статусы `Todo` и `In Progress`, членство уровня в `[levels]` не проверяется (его проверил хук); `--blocked-by` повторяется по одному ID, цель обязана уже существовать (поэтому порядок «вверх по файлу»). Номера — `<ЗАДАЧА>-1`, `-2`, … в порядке файла (точки ядро не пускает); (3) если создание упало посередине — удаляются **только** файлы, созданные этим запуском, затем обычный провал с причиной; не удалось удалить — сразу `needs_human` со списком оставшихся; (4) в карточку-задачу: `## Plan Accepted` (строки `- <ID> — <title> — <level>`, ветка) и `## Subtask Progress` (строка `0 of N merged`; дописывать при `Done` подзадачи будет S4-21 — это решение архитектора против автоархива) через `check.append_note`; карточка остаётся в `Subtasks`; счётчик провалов обнуляется (файл удаляется); событие `accepted`.

**Провал.** Карточки не создаются вовсе («всё или ничего»). Счётчик `<доска>/.ours-state/<ЗАДАЧА>.plan.json` +1; меньше `[plan] max_failures` (по умолчанию **2**, как `max_check_failures`) — `board mv <ЗАДАЧА> Plan`, иначе `needs_human` (перевод — как `integrate._board_mv`, с проверкой, что статус сменился); раздел `## Plan Failure` (причины списком, не больше 20 строк, остальное — «и ещё K») и событие `rejected`. Возврат в `Plan` происходит внутри `after_run`: ядро после хука перечитывает статус и видит `Plan` — то же состояние, в котором ход начался, так что ход продолжается тем же заходом (ограничивают его `max_turns`, `max_state_turns` и `max_total_turns`, расходуя ходы).

**Тело карточки подзадачи.** Заголовки по-английски, как в промптах этапов:

```
Task: <ЗАДАЧА> (subtask N of M). Level: <level> — <level_reason>

## Goal
## Acceptance Criteria      (строки «- AC1: …»)
## Check                    (`<check>` в кодовых кавычках)
## Scope                    (строки «- `<путь>`»)
## Non-goals                (только если есть)
```

Заголовков `## Plan` и `## Acceptance Tests` нет — их пишет сам агент в `In Progress` (контракт этапа требует непустые). Заголовка `## Touched Files` **нет сознательно**: это штатный раздел проверки пересечений ядра (C1: `dispatch.py::_conflict_blocker`), при пересечении путей она переводит подзадачу в `Blocked` и сама не возвращает — подзадача 1 ждёт продолжения в `Verify`, независимая подзадача 2 с общим путём ушла бы в `Blocked`, и задача навсегда застряла бы в `Subtasks`; нам это не нужно (`max_concurrent_agents = 1`, порядок даёт `blocked_by` и вливание). Поэтому нужный контракту раздел назван `## Scope`, а строки-заголовки в тексте плана запрещены. Тест подаёт тело парсеру ядра `_parse_touched_files`.

**Начальный статус подзадачи — `In Progress`** (решение архитектора): разбор, который делает `Todo`, уже сделали руководитель и проверка хука; лишний ход агента на каждую подзадачу не нужен. Путь работника: `In Progress` → `Verify` → дальше программа (`integrate.py`): два этапа агента. Формула лимитов не меняется (самый длинный путь — у обычной карточки, 4 этапа). `## Triage` по коду не требует ни один контракт ядра (`orchestrator/contracts.py`: `In Progress` ждёт `## Plan`, `## Acceptance Tests`, `## Done Signals`, `## Implementation`, `## Self-Critique`) и ни один наш хук (`check.py`: обязательные разделы — настройки `[checks]`); регулярка `## Triage` в ядре (`constants.py::_AUTO_TRIAGE_TRIAGE_RE`) нужна только автоматическому разбору карточек в `Todo`, куда подзадача не попадает.

**Сводка для руководителя `plan.py brief`** (последняя строка `before_run`; для карточки с меткой в `Plan`, иначе молча). Пишет `<доска>/artifacts/<ЗАДАЧА>/work/limits.md` (агент видит `docs/<ЗАДАЧА>/work/limits.md`): уровень задачи и **разрешённые уровни подзадач** с моделью, усилием, `max_turns`, `max_state_turns` (у уровня нет своих — взяты из `[limits]`); `max_subtasks`; сколько провалов плана уже было и `max_failures`; бюджет самой карточки из `GET http://127.0.0.1:<port>/api/v1/issues/<ЗАДАЧА>` → `budgets.items` (`name`, `limit`, `used`, `remaining`; `urllib`, таймаут 5 с, без прокси; не ответило — строка «бюджет недоступен», остальное пишется); путь и формат плана — ссылкой на эту запись. Сводка только информирует; лимиты держит код. Запись не идёт сквозь ссылку: если `work` — ссылка или лежит не там, сводка не пишется, ошибка — в журнал.

**`[plan]` в `project.toml`:** `max_subtasks` (целое 1–20, умолчание 5), `max_failures` (целое ≥ 1, умолчание 2); умолчания заполняются всегда; неизвестный ключ — ошибка. В заготовке `project-new` раздел — закомментированным примером по-русски, правило порядка уровней — рядом с `[levels]`. Доска `oms` не менялась; доска без `[plan]` (`max_turns = 12`, `max_attempts = 2`) проходит (тест).

**Вывод по `max_reopens` и `rewind` (проверено по коду).** Возврат `Subtasks → Plan` из `after_run` не расходует ни одно из них. `max_reopens`: ядро считает повторные открытия только по прошлым **завершённым** запускам, дошедшим до `Done` (`blocked_recovery.py::_prior_done_run_count` — состояния `Done`/`Completed`; `_hold_reopened_ticket_over_budget` вызывается из диспетчера при взятии карточки); запуск руководителя заканчивается в `Subtasks`/`Plan`, а не в `Done`. `rewind_count` / `max_attempts`: растёт только при переходе **назад по активным статусам** внутри одного запуска (`helpers._is_rewind_transition`, `attempt.py::_evaluate_turn_result`, `agent_phase.py`), а ядро перечитывает статус **после** `after_run` и видит `Plan` — то же, что на старте хода, перехода нет. Ядро возврат не запрещает — так же, как откат `check.py` из `Human Review` в `In Progress` (K08a). Расходуются только ходы (`max_turns`, `max_state_turns`, `max_total_turns`).

**Отступления от задания** (маленькие, для спокойствия архитектора): (а) `blocked_by` можно опустить — тогда пусто (иначе забытая строка `blocked_by = []` тратила бы попытку плана из двух); (б) не удалось создать интеграционную ветку (например, нет базовой ветки) — это тоже «сразу `needs_human`»: руководитель этого не исправит; (в) из ID задачи не получаются ID подзадач — то же; (г) в тексте `goal` запрещены строки-заголовки, в `check` и `paths` — обратная кавычка (защита от разделов-«двойников», в первую очередь `## Touched Files`); (д) заголовок подзадачи не может начинаться с «-» (его принял бы `board new` за ключ); (е) сводка и план читаются со стороны доски, ссылки и ссылочные папки `work` отвергаются (агент может подменить папку в `docs/<ID>`).

**Правки существующих тестов** (только там, где проверяется полный перечень строк хуков): `ours/tests/test_integrate.py` — два теста `test_after_run_calls_both_hooks_when_nothing_changed` и `test_after_run_commits_and_then_calls_integrate`: ожидаемый список вызовов `check.py hook`, `plan.py hook`, `integrate.py hook` (раньше без `plan.py hook`); `ours/tests/test_project_kit.py::test_rendered_workflow_is_accepted_by_the_core`: вторая команда `after_run` — `plan.py hook || true`, подоболочка `(` и `set -uo pipefail` сдвинулись на 3-ю и 4-ю. Других правок нет.

**Как проверено.** `ours/tests/test_plan_hook.py` (126 тестов) — на настоящем git, настоящем `symphony board` этой рабочей копии и настоящем `bash` (отрисованные `after_run` и `before_run` исполняются, `plan.py hook` выполняется и при «нет изменений»): каждая проверка плана, сбор всех причин сразу, принятие (карточки, метки, `request`, `level`, `blocked_by` как ID, тело без `## Touched Files`, ветка, оба раздела, повторный вызов ничего не делает, счётчик обнуляется), провал и второй провал, три случая «сразу `needs_human`», сбой посередине (подменённый `symphony`: первая карточка удалена, итог — провал; карточки не удаляются — `needs_human` со списком), молчание, `brief` с настоящим локальным HTTP-ответом и без службы, `[plan]`. Команды и итоги — в описании PR.

**Не проверено:** в работающей службе (после слияния `post-merge` пересоберёт `WORKFLOW.md` доски `oms`; до S4-20 руководитель в `Plan` получает заглушку и план не пишет, хук на доске ещё не сработает) и с настоящим `claude` — это проверка боем S4-22. Не проверено и чтение живого `/api/v1/issues/<ID>` (формат взят из кода `webapi/issue_routes.py`, `orchestrator/api_views.py`).

**Модель:** Sonnet, effort=high.

### 2026-10-27 — S4-23 — очередь закрывает окно Claude Code после слияния

Основа: `docs/our/tasks/TASK-S4-23.md` (решение Сергея от 27.10.2026). Правился только `ours/hooks/inbox_runner.py` (контур контроля), его тесты и документы; ядро (`src/`, `tests/`), `post_merge.py`, шаблоны и промпты не менялись.

**Зачем.** Команда `.run` открывает новое окно Терминала с Claude Code; Сергей закрывал его руками после каждой задачи, окна копились, а сессия сидела в папке рабочей копии, которую `post-merge` после слияния удаляет.

**Что и когда.**

1. `.run` запоминает окно. Скрипт открытия окна (`TERMINAL_SCRIPT`) теперь печатает две строки: `window_id=<число>` и `tty=/dev/ttysNNN`. Номер окна скрипт находит, перебирая окна и сверяя `tty` вкладки (у вкладки в словаре Терминала нет свойства «окно», поэтому ссылку на вкладку не разбираем). Вывод разбирается строго; не разобрался — запись не создаётся, в отчёте строка «окно не записано: …», запуск **не** считается неудачным. Запись — `tmp_workspaces/outbox/<код>.terminal.json` (`code`, `window_id`, `tty`, `opened_at`), пишет только `.run` после успешного открытия; в отчёте строка `Окно: id <N>, tty <tty>`.
2. `.merge` после строки «PR влит.» и **до** `post-merge` (он убирает рабочую копию) выполняет отдельный шаг «закрыть окно Claude Code».
   - Нет записи или она испорчена → «окно не записано — закрой сам, если открыто».
   - **Проверка «то самое окно»:** `osascript` спрашивает, есть ли у окна с записанным номером вкладка с записанным `tty`. Не совпало, окна нет, Терминал не запущен, `osascript` упал → ничего не останавливается и не закрывается, в отчёте причина.
   - **Остановка Claude Code.** На `tty` вкладки ищутся процессы, у которых в командной строке есть `claude` и полный путь к `…/tmp_workspaces/inbox/<код>.md` (слово `claude` из самого пути не считается, путь не должен быть куском более длинного пути). От каждого найденного строится дерево потомков по `ppid` из одного снимка `ps -ww -A`; `SIGTERM` всему дереву, ожидание до 10 с, оставшимся **из этого же дерева** — `SIGKILL`. Выживший — только тот, чей pid по-прежнему с той же командной строкой и не зомби (защита от повторно выданного pid). К снимку добавлена колонка `stat` (к `pid`, `ppid`, `command` из задания), чтобы не считать живыми зомби.
   - **Закрытие:** `close window id <N>`; затем окно проверяется ещё раз. Окно исчезло → запись `<код>.terminal.json` удаляется. Осталось (например, Терминал спросил подтверждение) → ничего силой, в отчёте «окно не закрыто: …, закрой сам», запись остаётся.
3. Данные в скрипты AppleScript идут только через `argv`; скрипты — константы. Все внешние вызовы (`osascript`, `ps`, сигналы) идут через `Ops` (`proc`, `kill`, `sleep`), путь `osascript` — из `Settings`, таймаут — `OSASCRIPT_TIMEOUT_S`.

**Что никогда не трогается.** Оболочка вкладки (родитель Claude Code); любой процесс вне дерева найденного Claude Code (в том числе другого кода, без пути к заданию, редактор с этим файлом); окна без записи `.terminal.json` (открытые не очередью); окна после `.test`, при отказе `.merge` и при неудачном `gh pr merge`; слияние, где ветка не равна `dev/<код>` из имени файла (окно по такому коду не закрывается).

**Неудача не мешает слиянию.** Любая ошибка шага «закрыть окно» (в том числе неожиданное исключение) — только строка в отчёте; `post-merge` запускается как раньше, код возврата не меняется. `post_merge.py` и уборка рабочих копий не менялись.

**Как проверено.** Тесты в `ours/tests/test_inbox_runner.py`: разбор вывода и запись `.run`; порядок «PR влит → закрыть окно → `post-merge`» по журналу подменённых вызовов; настоящие процессы (заглушка «claude» с ребёнком и внуком, внук игнорирует `SIGTERM` и добивается `SIGKILL`; оболочка-родитель, процесс другого кода, без пути и без `claude` остаются живы); все отказные ветки (tty не совпал, окна нет, записи нет, `osascript` упал, Терминал не закрыл, `ps` упал, `kill` упал).

Не проверено: на настоящем `osascript` и Терминале — первая проверка при ближайшем `.merge`. Риск: если Терминал спросит подтверждение закрытия, окно останется открытым (безопасно, запись остаётся). Модель: Sonnet, effort=high.

### 2026-10-27 — S4-18 — статусы нарезки `Plan` и `Subtasks`

Основа: `docs/our/tasks/TASK-S4-18.md`; шаг 6 порядка архитектора. Ядро (`src/`, `tests/`) не менялось.

**Что добавлено и где.**

1. `ours/templates/WORKFLOW.md.tmpl`: `tracker.active_states: [Todo, Plan, "In Progress", Verify, Document]` (`Todo` остаётся первым: `board new` без `--state` ставит первый активный статус); `terminal_states: ["Human Review", needs_human, Subtasks, Done, Blocked, Archive]`; `agent.stage_contracts: "on"` (вместо `auto`); `agent.dependency_resolving_states: [Done]`; `preserve_workspace_states: ["Human Review", "needs_human", "Subtasks"]`; в `prompts.stages` строка `Plan`.
2. `ours/hooks/project_config.py`: в перечень промптов добавлен `plan`; **две разные величины** вместо одной (ниже).
3. Промпты: `ours/prompts/stages/plan.md` (заглушка: пишет `## Blocker` одной строкой и ставит `needs_human`), ветка по метке `role:manager-plan` в `stages/todo.md` (запасной путь), строка `Request:` и схема пути руководителя в `base.md`; правки перечислены в `ours/prompts/CHANGES.md`.
4. `ours/bin/project-new` (комментарии про лимиты) и `ours/README.md` (путь карточки-задачи, число 5) — только тексты.

**Две величины вместо `ACTIVE_STATE_COUNT`.** Поправка к пункту архитектора «`ACTIVE_STATE_COUNT` остаётся 4»: ядро требует, чтобы `agent.max_turns` и `max_turns` каждого уровня были **не меньше числа активных статусов** (`workflow/preflight.py`, `workflow/levels.py::parse_levels`), а их теперь 5. Если оставить границу 4, `project.toml` с `max_turns = 4` наш загрузчик пропустил бы, а ядро отказало бы при загрузке `WORKFLOW.md`. Поэтому: `ACTIVE_STATE_COUNT = 5` — нижняя граница `max_turns` («не меньше числа активных статусов доски (5); иначе ядро не примет WORKFLOW.md»), тест следит, чтобы число совпадало с длиной `active_states` отрисованного шаблона; `STAGES_ON_LONGEST_PATH = 4` — этапов на самом длинном пути карточки (обычная: Todo, In Progress, Verify, Document; работник — 3; руководитель — Plan, Verify, Document), по нему считается правило `max_turns ≥ 4 × (1 + max_attempts)`. Числа доски `oms` (`max_turns = 12`, `max_attempts = 2`) проходят; у уровней доски `oms` своего `max_turns` нет (прочитано в `~/symphony-projects/oms/project.toml`, не менялось), так что после слияния доска продолжит загружаться.

**Почему `stage_contracts: "on"`, а не `auto`.** `auto` включает контракты этапов, только если все активные статусы входят в стандартный набор (`workflow/presets.py::board_uses_shipped_contracts`). С `Plan` набор нестандартный, контракты **тихо выключились бы**. При `on` для нового набора статусов `guess_lane_preset` не находит пресет и возвращает `None` (раньше для старого набора — `"default"`), но набор контрактов тот же: `contract_producing_states(None)` и `(`"default"`)` оба дают `{in progress, verify, document, learn, done}`. У `Plan` и `Todo` контракта нет (этап вне набора — проходит без проверки), у In Progress / Verify / Document / Done — прежний. Это закреплено тестом.

**Автоархив — ловушка.** `tracker.archive_after_days` не задан, умолчание ядра 30 дней: конечная карточка, которую не правили 30 дней, уходит в `Archive`; любая правка карточки сбрасывает отсчёт (`utils/archive.py::select_archivable` смотрит на `updated_at`). Карточка-задача в `Subtasks` может ждать дольше. Умолчание **не меняем** (решение архитектора). Лечение — в S4-19/S4-21: `integrate.py` будет дописывать в карточку-задачу раздел `## Subtask Progress` при `Done` каждой подзадачи.

**Зачем `Request:` в `base.md`.** Подзадача определяется меткой `role:worker` **и** полем `request:`, а второе агент раньше не видел: ветка Verify «подзадача — сразу в `Human Review`» могла не сработать. Теперь строка `Request: <ЗАДАЧА>` выводится, если поле есть.

**Проверка ядра по коду (часть 4 задания).** Все места пройдены лично.

| Место | Результат |
|---|---|
| `dispatch.py::_eligibility_contract_decision` | проверено по коду: конечный или не активный статус → `REJECT inactive`; `Plan` берётся, `Subtasks` — нет (тест) |
| `attempt.py` (≈1040) — контракт на входе в конечный статус | проверено по коду: только для `done`/`completed` (`_is_successful_terminal_state`); вход в `Subtasks` и `needs_human` — без контракта, как в `Human Review` |
| `presets.py::board_uses_shipped_contracts`, `stage_contracts_enabled` | проверено по коду: с `Plan` `auto` выключил бы контракты, нужен `on` (тест) |
| `contracts.py::evaluate_contract`, `agent_phase.py::_enforce_stage_contract` | проверено по коду: у `Plan` контракта нет, у остальных прежний набор. **Уточнение:** для нового набора ядро выбирает пресет `None`, а не `"default"`; набор контрактов тот же (тест). Пресет влияет ещё на `webapi/workflow_routes.py` (поле `current` покажет `null`) и `cli/doctor.py` (проверка только для `deep`) — на работу не влияет |
| `worker_exit.py` ≈695–725 | проверено по коду: `is_terminal` → продолжение не планируется, ход в `Subtasks` завершается штатно |
| `worker_exit.py` ≈775 | расхождение с заметкой: история `finalize_delivery_history` только для `done`/`human review`, но для остальных статусов есть третья ветка — `commit_workspace_on_done` делает снимок рабочей копии сам. Для `Subtasks` коммит делают и блок `after_run` (`wip: …`), и эта ветка; ошибка снимка ставит карточку на паузу с сохранением копии |
| `retry_reconcile.py`: `reconcile_terminate_terminal`, `after_run_started_at` | проверено по коду: защита долгого `after_run` (`_after_run_age_s`) не зависит от статуса; ход, закончившийся в `Subtasks`, проходит `after_run` так же, как в `Human Review`; при уходе в конечный статус во время хода сначала снимок, потом рабочая копия сохраняется (`reconcile_preserved_workspace`), если статус в `preserve_workspace_states` |
| `retry_reconcile.py` ≈1076, ≈1238 | проверено по коду: копию убирают, кроме `Done`, `Blocked`, `Human Review` и `preserve_workspace_states`; `Subtasks` в списке; перезапуск службы копию оставляет (тест с настоящей `_startup_terminal_cleanup`) |
| `sweeps.py::_archive_sweep`, `utils/archive.py` | проверено по коду: ловушка, см. выше |
| `core_support.py::_successful_blocker_terminal_states` | проверено по коду: при ключе — только `done`; без ключа `Subtasks` считался бы выполненным (тест) |
| `helpers.py` (`_terminal_state_matching` и др.), `release_cycle.py` | проверено по коду: `Subtasks` не совпадает ни с `human`/`review`, ни с `block`; цели — `Human Review` и `Blocked` (тест). `release_cycle.py` (успешные конечные статусы — свой зашитый список) относится только к карточкам `app-release`, которых у нас нет |
| `preflight.py`, `levels.py::parse_levels` | проверено по коду: `max_turns` (общий и уровня) ≥ числа активных статусов, теперь 5 |
| `cli/board.py` ≈201 | проверено по коду и тестом: `board new` без `--state` → `Todo`; `board mv X Plan` и `Subtasks` принимаются |
| `webapi/*`, `tui/*`, `progress_md.py`, `pm/*`, `trackers/file.py` | проверено по коду: статусы нигде не зашиты списком (кроме демо `pm/demo.py`), колонки берутся из конфигурации; `dependency_state_resolved("Subtasks")` ложно (тест) |
| `ours/hooks/check.py`, `integrate.py` | проверено по коду: `check.py hook` работает для `done`/`human review` (`CHECKED_STATES`), `integrate.py hook` — только для `Human Review`; карточку в `Plan` и `Subtasks` пропускают молча |

Мест, где `Plan` или `Subtasks` ломают работу, не найдено.

**Тесты.** Новый файл `ours/tests/test_planning_states.py` (18 тестов: ядро принимает `WORKFLOW.md`, контракты, зависимости с ключом и без, цели передачи, диспетчер, `board new`/`mv`, `_startup_terminal_cleanup`, промпты движком ядра, границы `max_turns`). Правки существующих тестов — только утверждения о содержимом шаблона и тексты ошибок: `test_project_kit.py::test_rendered_workflow_is_accepted_by_the_core` (кортежи `terminal_states`, `active_states`, `preserve_workspace_states` — новые статусы); `test_levels_kit.py` (два места: `test_bad_levels_refused`, `test_max_turns_still_covers_the_active_states`) и `test_project_doctor.py::test_bad_limits_refused` (текст ошибки «max_turns не меньше числа активных статусов доски» вместо «не меньше 4»); `test_limits_preserve.py::test_stub_limits_pass_the_loader_and_the_formula` (`ACTIVE_STATE_COUNT` → `STAGES_ON_LONGEST_PATH`: проверяется формула 4 × (1 + `max_attempts`) = 12).
- **Не проверено:** в работающей службе и на доске `oms` (поведение службы с `Plan`/`Subtasks` проверит S4-22).
- **Что делает чат этапа после слияния:** `ours/bin/post-merge` пересоберёт `WORKFLOW.md` доски `oms`; затем проверить `symphony doctor`, что в веб-доске появились столбцы `Plan` и `Subtasks`, что прежние карточки на месте. `project.toml` и карточки `oms` не менять.
- **Модель:** Sonnet, effort=high.

### 2026-10-27 — Решение архитектора: статус `Plan`

1. **Причина.** Промпт ядро выбирает по статусу карточки, а шаблонизатор по метке умеет только **добавить** текст, но не убрать текст работника: руководитель в `In Progress` получил бы «пиши код» и «не пиши код» сразу. Поэтому нарезка идёт в отдельном рабочем (активном) статусе `Plan` со своим промптом.
2. **Путь карточки-задачи:** `Plan` → `Subtasks` (конечный для ядра статус ожидания: план сдан, подзадачи делают работники, агент на карточке не работает) → итоговая проверка (`Verify` → `Document` → `Human Review`). Своих инструкций требует только нарезка.
3. **Основной и запасной путь.** Основной: карточку-задачу создают сразу в `Plan` (`board new --state Plan --label role:manager-plan --level …`). Запасной: карточка с этой меткой попала в `Todo` — ветка в `todo.md` отправляет её в `Plan`.
4. **Ловушка `stage_contracts: auto`.** С нестандартным активным статусом `auto` тихо выключает контракты; нужен `on`.
5. **Шаг 8 (`role:manager-diagnose`)** — тот же приём (свой статус и промпт), если понадобятся свои инструкции; сейчас не делается.

### 2026-10-27 — S4-17 — PR ядра №4: `agent.dependency_resolving_states` и `budget_reopen_history`

Основа: `docs/our/tasks/TASK-S4-17.md`; шаг 6 порядка архитектора («Решения архитектора по REPORT-17», п. 5 и «Порядок реализации», п. 6). Правка ядра небольшая, две независимые части — два отдельных коммита.

1. **Ключ `agent.dependency_resolving_states`.** Список имён статусов, в которых зависимость `blocked_by` считается выполненной. **Умолчание — пусто**, и тогда правило прежнее: все конечные статусы, кроме «неудачных» (`Archive`, `Blocked`, `Cancelled`, `Duplicate`, `Human Review`, `needs_human`). Если список задан — выполненной считается зависимость ровно в этих статусах и больше ни в каких.
2. **Где проверяется.** Разбор и проверка — `workflow/builder.py` (`_validated_dependency_resolving_states`): каждое имя обязано быть в `tracker.terminal_states` (без учёта регистра и крайних пробелов), иначе `ConfigValidationError` с перечнем допустимых; не список или пустая строка — тоже ошибка; пустой список — как без ключа. Правило применяется в одном месте, `_successful_blocker_terminal_states` (`orchestrator/core_support.py`), поэтому диспетчер (`waiting_dependency`), сводки группы `request` и восстановление `Blocked` получают одно и то же. Описание — `docs/reference.md`, рядом с `preserve_workspace_states`.
3. **Зачем.** Статус `Subtasks` карточки-задачи придётся объявить конечным (иначе ядро возьмёт её в работу). Без ключа он стал бы «успешным», и карточка с `blocked_by: <ЗАДАЧА>` стартовала бы до конца задачи. **Статус `Subtasks` в нашем шаблоне `WORKFLOW.md` появится в S4-18 вместе со значением `dependency_resolving_states: [Done]`**; в этой задаче `ours/` не менялся.
4. **Другие копии правила «успешный блокер».** Поиском по `src/symphony` (`_FAILED_BLOCKER`, `human review`, `terminal_states`, `blocked_by`) отдельных копий не найдено: все четыре места (`dispatch.py`, `api_views.py` — два вызова, `blocked_recovery.py`) идут через `_blocker_dependency_is_resolved`; `webapi/payloads.py` и `webapi/issue_routes.py` лишь делят карточки на конечные/активные и для конечных зовут `dependency_state_resolved`. Рядом есть **другие** правила, их не трогали: `helpers._is_successful_terminal_state` («доставлено» = Done для учёта `reopens`) и `release_cycle.is_release_success_state` (какие конечные статусы разрешают финальную доставку). Колонки в `tui/` и `cli/board.py` просто перечисляют статусы.
5. **`budget_reopen_history`: причина.** Подтверждена по коду и воспроизведена тестом: SQLite-соединение реестра (`run_registry_base.py::_connect`, `sqlite3.connect` без `check_same_thread=False`) создаётся в потоке цикла службы, а `issue_budget_snapshot` вызывал `registry.recent_runs(...)` через `asyncio.to_thread` — в другом потоке. Ошибка: `SQLite objects created in a thread can only be used in that same thread`; в `budgets` у `reopens` всегда `used = null`, в журнале `run_registry_error` при каждом запросе карточки.
6. **Исправление — синхронный вызов `_registry_guard`**, как во всех остальных местах (в том числе `blocked_recovery.py::_prior_done_run_count` с тем же запросом). `check_same_thread=False` не вводили: это меняло бы правила для всего реестра. Цена — один быстрый запрос (до 200 строк) в цикле службы на запрос бюджета; такой же запрос уже делает `_prior_done_run_count`.
7. **Почему иначе нельзя.** Список успешных статусов вычисляется внутри ядра из `terminal_states` минус зашитый список — снаружи (конфигурацией или хуком) его не сузить; добавлять «неудачные» имена под наш статус в зашитый список хуже для переноса правок при обновлении ядра, чем общий ключ. Ошибка реестра — в коде ядра (`orchestrator/api_views.py::issue_budget_snapshot`), снаружи её не обойти.
8. **Тесты.** `tests/test_dependency_resolving_states.py` (прежнее поведение без ключа, `Subtasks` без ключа считается выполненным, с `[Done]` — нет, регистр, диспетчер и `dependency_state_resolved`, ошибки конфигурации) и `tests/test_budget_reopen_history.py` (настоящий `RunRegistry` на временном `state.db`: `used` — число, счётчик ошибок реестра не растёт; формула подсчёта прежняя). Новый тест реестра до правки падает с той самой ошибкой потока (проверено). Существующие тесты не менялись.
- **Не проверено:** в работающей службе (после слияния `post-merge` перезапустит `oms`; стоит убедиться, что в `budgets` у `reopens` появилось число, а `run_registry_error` в журнале нет); `dependency_resolving_states` на настоящей доске со статусом `Subtasks` (его заводит S4-18).
- **Модель:** Sonnet, effort=high.

### 2026-10-27 — Решения архитектора: итоговая проверка задачи и доставка (шаги 6–7)

Принятые решения, не пересматривать.

1. **Слияние `integration/<ЗАДАЧА>` с `our-main` при итоговой проверке — настоящим коммитом слияния с двумя родителями** (конец интеграционной ветки + конец `our-main`): `git merge-tree --write-tree` → `git commit-tree -p <интеграция> -p <our-main>` → `git update-ref` с проверкой старого значения. Плоский коммит нельзя: GitHub показал бы правки `our-main` как правки задачи. Если `our-main` не сдвинулся с создания ветки — слияния нет. Конфликт — `needs_human` со списком файлов, ветка не меняется. Дальше: рабочая копия карточки-задачи ставится на результат слияния; `base_sha` в её записи базы = использованный конец `our-main`; интеграционная ветка сдвигается на тот же коммит; полный `test.sh` без пропуска по `doc_paths`.
2. **В S4-21** хэш `our-main`, с которым прошла итоговая проверка, записывается в `.ours-state/<ЗАДАЧА>.final.json`.
3. **Шаг 7 (`.deliver`; решение записываем сейчас, делаем потом):** если `our-main` отличается от записанного в `.final.json`, программа сама повторяет слияние с новым `our-main` + полный `test.sh` + `check.py`, без этапов агента. Зелёное — пуш; конфликт или красное — пуша нет, карточка в `needs_human` с причиной.
4. **Сдвиг `our-main` между доставкой и «добро» Сергея — принятый риск**, как у наших PR S4; страховка — полный прогон в `post-merge`.

Принятая нарезка шага 6: S4-17 (PR ядра №4) … S4-22; S4-21 — выход из `Subtasks`, вынесен отдельно, чтобы хук нарезки не разросся. Принятые решения по нарезке: формат `plan.toml`, отправка плана переходом в `Subtasks`, «всё или ничего», `[plan] max_subtasks = 5`, сигнал «последняя подзадача получила `Done`» и ручная команда `integrate.py finish-task`.

### 2026-10-26 — S4-16 — лёгкий путь и подзадачи без Document

Основа: `docs/our/tasks/TASK-S4-16.md`; решения архитектора от 26.10.2026 «по скорости OMS-6», п. 3 (вторая половина бывшего S4-15), и отступление «поле `class` не вводится». Ядро (`src/`, `tests/`) не менялось; правились `ours/hooks/check.py`, `integrate.py`, `project_config.py`, заготовка `project-new` и промпты.

1. **`[checks] doc_paths`.** Список шаблонов путей (синтаксис `forbidden_paths`, без учёта регистра), по умолчанию пустой — тогда всё как раньше. Если множество изменённых путей (`changed_paths`: закоммиченное, индекс, рабочая копия, неотслеживаемое) **не пустое** и **каждый** путь совпадает с одним из шаблонов, `check.py` (одинаково `hook` и `gate`) не запускает `test.sh`: результат `ok`, в отчёте `tests skipped: only doc paths changed (N files)`, в событии `pass` — `reason` того же содержания и `tests: skipped-docs` (у пропуска по записи «влито» остаётся `tests: skipped`). Пустое множество изменений — полный прогон. В режиме `hook` отметка `<ID>.pass.json` пишется так же, как после успешных тестов (иначе `integrate.py` не вольёт подзадачу).
2. **Почему только по путям.** Лёгкий путь решает код по тому, что изменено на самом деле, а не агент по своей разметке: ошибиться или схитрить нельзя (отступление «поле `class` не вводится» выше). **Быстрые проверки идут всегда**: конфиг, база, запрещённые пути, изменённые существующие тесты, разделы карточки. Пропускаются только тесты. Одного пути вне списка (например, `.py` в папке документов) достаточно для полного прогона.
3. **Порядок в `gate` с записью «влито» (S4-13).** Запись `.merged.json` по-прежнему забирается и удаляется первой. Дерево совпало — пропуск по записи (`tests: skipped`). Не совпало — проверяется `doc_paths` (`tests: skipped-docs`; `tests_reason` остаётся в событии). Иначе — полный прогон.
4. **Итоговая проверка перед доставкой.** Проверка задачи перед доставкой (шаг 7, `.deliver`) **обязана гонять полный набор независимо от `doc_paths`**: пропуск по документам безопасен только потому, что полный прогон всё равно будет в конце. Сейчас это только записано (в ROADMAP, пункт доставки), не сделано.
5. **Значение для доски `oms`** (поставит чат этапа после слияния, в этой задаче доска не менялась): `doc_paths = ["docs/our/**/*.md", "docs/llm-wiki/**/*.md"]`. Почему: `docs/our/` и `docs/llm-wiki/` не читает ни один тест форка (из `docs/` тесты читают только `docs/symphony-prompts/`, `docs/index.html`, `docs/PIPELINE-DEMO.md` и `CHANGELOG.md` в корне). `docs/llm-wiki/` в форке — папка внутри репозитория (не ссылка в папку доски), и Document обычной карточки пишет вики именно туда. Файлы доказательств `docs/<ID>/` — ссылки в папку доски, в `changed_paths` не попадают. Расширение `.md` — чтобы файл кода в такой папке не пролез без тестов; `CHANGELOG.md` не включён, его читает тест. В заготовке `project-new` строка закомментирована, пояснение — там же.
6. **Подзадача.** Определение то же, что у `integrate.py`: `[integration] enabled = true` и у карточки метка `worker_label` (по умолчанию `role:worker`, без учёта регистра) и поле `request:`. Общая функция `integrate.is_subtask(front, integration)`; `task_of` теперь опирается на неё, `check.py` вызывает её лениво (цикл импортов). Если шапку карточки прочитать нельзя — карточка считается обычной (требования строже).
7. **`[checks] worker_required_sections`.** Для подзадачи `check.py` требует эти разделы вместо `required_sections`; по умолчанию `## QA Evidence`, `## Merge Status` (их пишет Verify). Проверка формы заголовков та же. Ключ — в схеме `project_config.py` и в заготовке `project-new` (закомментирован).
8. **Правки промптов** (`ours/prompts/CHANGES.md`): `verify.md`, шаг 9 — для подзадачи после чистого Verify раздел `## Document Skipped` одной строкой «Subtask: documentation is written by the task card» и `Human Review`, для остальных `Document`; `base.md` — исключение из правил «один этап = один переход» и «`Done` только с `## Wiki Updates`…», ветка схемы «subtask: Verify -> Human Review»; `document.md` — **сужена самоправка**: мелкие неточности только в `## Review Notes` без возврата, файлы, изменённые карточкой, Document не правит (решение архитектора: правку после Verify никто не проверяет).
9. **Условие в промпте.** Шаблонизатор ядра (`src/symphony/prompt.py`, `_eval_condition`) понимает `==`, `!=`, истинность и `!`; проверки «метка есть в списке» (`contains`) нет, поэтому надёжного условия по меткам нет. Сделана текстовая инструкция по строке `Labels:` и полю `request:`. Это не опасно: подзадачу определяет код, ветка в `verify.md` только экономит ход; ошибка агента в любую сторону ведёт либо к отказу шлюза (нет `## Wiki Updates`), либо к лишнему ходу Document. Тест фиксирует отсутствие `contains` в движке: когда оно появится, условие можно будет сделать шаблонным.
10. **Тесты.** `ours/tests/test_light_path.py` (check.py: шаблон, пропуск в `hook` и `gate`, граничные случаи, быстрые проверки всегда, запись «влито» вместе с `doc_paths`, разделы подзадачи) и `test_light_path_e2e.py` (подзадача на настоящем git и `bash`: изменения только в `doc_paths` → `check.py hook` без тестов, `.pass.json`, `integrate.py hook` вливает и ставит `Done`, шлюз после этого — пропуск по записи; отрисовка `base.md`, `verify.md`, `document.md` движком ядра). Единственная правка существующего теста: в `ours/tests/test_project_doctor.py` (`test_limits_reach_the_core_config`) убрана замена `"max_turns = 8 "` → `"max_turns = 12"`, которая после S4-15 ничего не делала (в заготовке уже 12); проверка прежняя — `max_attempts = 1` доходит до ядра.
- **Открытый вопрос остаётся открытым:** `test.sh <пути>` (запуск тестов только затронутого) — не делалось.
- **Не проверено:** в работающей службе и на настоящей карточке (ход Verify подзадачи в `Human Review`, разрешает ли ядро переход Verify → Human Review мимо Document на настоящем запуске — по коду контракт проверяется по производящему этапу и переход не запрещён); на настоящем `claude`.
- **Модель:** Sonnet.

### 2026-10-26 — Несколько проектов (решения архитектора)

1. **Сейчас.** У каждого проекта своя доска и своя служба (папка доски, порт, карточки, Done/Archive); карточки разных проектов не смешиваются. Так уже устроено.
2. **Этап 6.** Обзорная страница всех проектов: по разделу на проект, внутри задачи → подзадачи, статусы по-русски, сводка по проекту («в работе / ждёт Сергея / нужна помощь»). Строится поверх API служб, ядро не трогаем. Она объединяет русскую страницу, иерархию задач и обзор проектов.
3. **Мелкое исследование без кода.** Подойдёт ли хаб ядра (`symphony hub`, порт 8787) как временный обзор для наших внешних папок досок — без файлов в репозиториях проектов и без `git init` в папке доски (`project add` делает и то и другое).
4. **Этап 5.** Порог остановки (75–80 % подписки) и резерв Сергея (≥ 10 %) общие на все службы: подписка одна, нужен общий учёт, а не по службе.

### 2026-10-26 — Отступление: поле `class` (ARCHITECTURE 9) пока не вводится

Лёгкий путь (S4-16) определяется кодом по фактически изменённым путям; агент на него не влияет. Поле `class` введём, когда появится потребитель (нарезка руководителем или учёт расходов). Причина: нет потребителя; решение по путям надёжнее, его нельзя обойти разметкой.

### 2026-10-26 — S4-15 — уровни по сложности задачи, согласованные лимиты, сохранение копии needs_human

Основа: `docs/our/tasks/TASK-S4-15.md`; решения архитектора от 26.10.2026 («имена уровней и разные шкалы моделей», «лимиты противоречат друг другу», «копии удаляются в двух местах»). Ядро (`src/symphony/`) не менялось.

1. **Имена уровней.** Уровень описывает сложность задачи, а не модель.

   | Было | Стало | По-русски | claude (не менялось) |
   |---|---|---|---|
   | `L1` | `simple` | простая | haiku, medium |
   | `L2` | `standard` | обычная | haiku, high |
   | `S1` | `complex` | сложная | sonnet, high |
   | `S2` | `critical` | критическая (самая трудная, цена ошибки высока) | sonnet, max |

   Правка — в заготовке `project-new` (`[levels]`, `default_level = "simple"`, `CARD_TEMPLATE`), в `ours/README.md` и `GLOSSARY.md`. Имена уровней в коде свободные (`LEVEL_NAME_RE`), жёстко прошитых `L1`/`S1` вне тестов нет; в промптах этапов имён уровней нет.
2. **Правило.** Уровень описывает задачу; перевод уровня в исполнителя — отдельно для каждого бэкенда, в его шкале (`[levels.<имя>.claude]`, `[levels.<имя>.codex]`). Позже — список исполнителей с запасным вариантом (маршрут, ARCHITECTURE 4.6); сейчас не делается.
3. **Поправка к «Решениям для этапа 4».** Правило «вторая попытка — усилие на 2 ступени выше» заменяется на «лестница шагает по уровням задачи». «Та же модель, думает дольше» — это соседние уровни с одной моделью и разным усилием (`simple` и `standard`). Шаг в `[ladder]` — число уровней, а не ступеней усилия.
4. **Лимиты.** Правило: `max_turns ≥ число активных этапов × (1 + max_attempts)`, для заготовки 4 × 3 = 12 (на доске `oms` было 8 при `max_attempts = 2`, OMS-6 упёрлась в лимит). Проверяет загрузчик `project.toml` (`ours/hooks/project_config.py`, `_check_turns_cover_attempts`): для `[limits] max_turns` и для `max_turns` уровня (с `max_attempts` проекта); отказ — по-русски, с нужным числом. Уже было и осталось: `max_turns ≥ 4` и `max_total_turns ≥ max_turns`. Зацикливание по-прежнему держит `max_state_turns`. Заготовка: `max_turns = 12`, `max_total_turns = 20`. **Замечание:** загрузчик принимает `max_attempts` только ≥ 1 (существующий тест `test_project_doctor.py`), поэтому ветка «`max_attempts = 0` — возвраты без ограничения» (прежнее правило `≥ 4`) в `project.toml` недостижима; в коде она оставлена как защита и проверена прямым вызовом функции.
5. **`needs_human` в `preserve_workspace_states`** (`ours/templates/WORKFLOW.md.tmpl`). Проверено по коду: ядро удаляет рабочую копию карточки в конечном статусе в двух местах — `_reconcile_running` (`retry_reconcile.py` ≈ 1076) и `_startup_terminal_cleanup` (≈ 1238) — кроме `Done`, `Blocked`, `Human Review` и `preserve_workspace_states` (сравнение по нижнему регистру). Копия OMS-6 в `needs_human` уцелела только потому, что службу не перезапускали. Тест в `ours/tests/test_limits_preserve.py` отрисовывает шаблон, загружает его ядром и вызывает настоящую `_startup_terminal_cleanup`: копия карточки в `needs_human` остаётся. `worktree.py` не менялся: на живой папке `after_create` ядро не вызывает, продолжение работает само (проверено боем на OMS-6). Порядок продолжения — в `ours/README.md`, «Ходы, попытки и лимиты».
6. **Тесты.** Новый файл `ours/tests/test_limits_preserve.py`. В существующих тестах менялись только утверждения о содержимом заготовки (имена уровней, `default_level`, `max_turns` 8 → 12, `preserve_workspace_states`, текст комментария к `[levels.LOCAL]`) и данные двух тестов `test_levels_kit.py` с противоречивыми лимитами (с согласия Сергея); список — в описании PR.
- **Что делает чат этапа после слияния:** переименовывает уровни на доске `oms` (`project.toml` и строка `level:` во всех карточках; служба на время замены остановлена), затем `project-render`. Сам `oms` в этой задаче не менялся.
- **Не проверено:** в работающей службе (перезапуск службы с карточкой в `needs_human`, продолжение через `reset-budget`).
- **Модель:** Sonnet.

### 2026-10-26 — S4-14 — дисциплина проверки

Основа: `docs/our/tasks/TASK-S4-14.md`; решения архитектора от 26.10.2026 «по скорости OMS-6» (п. 1, 2, 4; п. 3 — в S4-15). Только тексты и правила; ядро, хуки, `ours/bin/`, шаблоны и `project.toml` не менялись.

1. **Бюджет тестов.** `stages/in-progress.md`: полный `test.sh` делает хост один раз за проход (`check.py hook` после Document). Только тексты (`docs/`, `*.md`) — тесты не запускать, только дешёвые проверки из `## Acceptance Tests`; код — только названное в `## Acceptance Tests`, а полный `test.sh` (если другой команды нет) — не более одного раза, последним шагом, вывод в `work/`; после возврата с текстовой правкой — не повторять. `stages/verify.md`: полный набор не запускать; в `## QA Evidence` одна строка `Full suite: run by the host hook (check.py hook) after Document`; в `## AC Scorecard` полный набор не вносить, кроме критерия «весь набор зелёный» (тогда ссылка на `qa/full-suite.md`). Причина: каждая строка `## AC Scorecard` обязана ссылаться на существующий файл `qa/...` или `work/...`, иначе ядро (`contracts.py`, `_row_evidence_failures`) возвращает карточку с «Contract Failure». `stages/document.md`: Document тесты не запускает.
2. **Политика возврата.** В `In Progress` карточку возвращают только если критерий приёмки нарушен по существу или что-то сломано (CRITICAL/HIGH, неработающий код, провал реальной проверки, утечка секрета, выход за разрешённые пути). Остальное (формулировки, стиль, числа, LOW и мелкие MEDIUM) — в новый раздел `## Review Notes` (≤ 10 строк) без возврата. Каждый пункт `## Review Findings` называет нарушенный критерий или поломку. Повторный Verify проверяет только пункты последнего `## Review Findings` / `## QA Failure` и то, что правка осталась в разрешённом; прежние доказательства — по ссылке; Security Audit и merge preflight после текстовой правки заново не строятся. Document возвращает (`## Document Defect`) только по существу.
3. **Критерии приёмки по существу.** В `CLAUDE.md` — раздел «Как писать критерии приёмки» (результат по существу, один сигнал проверки на критерий, без мелочных измеримых правил, не больше 5 критериев на карточку). `CLAUDE.md`, пункт про тесты агента конвейера: «не чаще, чем разрешает промпт этапа».
4. **Словарь.** `GLOSSARY.md`: «Роль» — понятнее про шаги порядка реализации; «Human Review» — что ждёт подзадача при включённой интеграции.
5. **`ours/prompts/CHANGES.md`** — строки по каждому месту и абзац «Почему» (что показали OMS-6 и OMS-7).

**Правило для промпта `role:manager-plan` (шаг 6).** То же правило критериев приёмки должно войти в промпт руководителя при нарезке: критерий — результат по существу, проверяемый чтением и одним понятным действием; без «1–3 предложения на термин», точной длины, порядка слов; число или формат — только настоящее требование; один сигнал на критерий; не больше 5 критериев на подзадачу.

- **Ограничение.** Формулировка архитектора «In Progress гоняет только тесты затронутого» сейчас невыполнима напрямую: у агента разрешены только `test.sh` доски (без аргументов — полный набор) и его `allowed_bash`, а прямой `pytest` по `CLAUDE.md` в окружении службы даёт ложные падения. Поэтому правило в промпте — «ничего для текстов, один раз для кода».
- **Открытый вопрос для S4-15.** Нужен ли `test.sh <пути>` (тесты только по указанным путям)? Файл `test.sh` доски создаёт `project-new`.
- **Не проверено:** на настоящей карточке и в работающей службе (после слияния `post-merge` сам пересоберёт `WORKFLOW.md` доски `oms`, т. к. изменены промпты).
- **Модель:** Sonnet.

### 2026-10-26 — Итоги проверки боем интеграционной схемы (OMS-5/6/7)

1. **OMS-5** — карточка-задача (заглушка) в Human Review; закрыта переносом в Archive, а не через Confirm done: у неё нет рабочей копии и записи базы, шлюз бы упал.
2. **Подзадачи OMS-6 (словарь) и OMS-7 (ссылка в README)** влиты программой в `integration/OMS-5` двумя плоскими коммитами: `35c0023` и `86dd8b3`. Вторая стартовала от конца первой (`base_sha` = `35c0023`).
3. **OMS-6** — два возврата и упор в лимит `max_turns = 8`; продолжение через `reset-budget` и перенос в Todo с уровнем L2; копия цела (службу не перезапускали). **OMS-7** — без возвратов, ≈20 минут.
4. **Тесты.** Один полный прогон на подзадачу (в `check.py hook`, ≈5 минут); шлюз тесты пропустил (событие `tests skipped`, S4-13).
5. **Доставка** в `our-main` — вручную, PR #35 (влит, `b4451d9`).
6. **Находки.**
   - Лимиты противоречили друг другу. Правило: `max_turns ≥ число активных этапов × (1 + max_attempts)` = 12; на доске `oms` 12 с 26.10. Проверка согласованности — S4-15.
   - `budget_reopen_history` падает с ошибкой SQLite из-за вызова через поток (PR ядра №4).
   - Копии карточек уборка снимает в двух местах `retry_reconcile.py` (`_reconcile_running`, `_startup_terminal_cleanup`): `needs_human` надо добавить в `preserve_workspace_states` (S4-15).
   - Скорость OMS-6 и меры — запись «S4-14 — дисциплина проверки».

### 2026-10-26 — Оценка: автоматический пропуск Document для подзадач `role:worker`

В ядре пропуск Document есть только ручной (`POST /api/v1/issues/{id}/skip-document`, клавиша `S` в TUI); автоматического нет. В промпте пропуск сделать можно, но `check.py hook` требует разделы `## Wiki Updates` и `## As-Is -> To-Be Report` (`DEFAULT_REQUIRED_SECTIONS`, `check.py` ≈ стр. 83), которые пишет Document; эти требования ослабляются только для всего проекта через `[checks] required_sections`. Поэтому автоматический пропуск для `role:worker` — задача S4-15 (требуемые разделы по роли); в S4-14 не делается.

### 2026-10-26 — S4-13 — шлюз без повторного прогона для влитого дерева

Основа: `docs/our/tasks/TASK-S4-13.md`; решение архитектора от 26.10.2026 (запись S4-12, «На будущее», п. 1).

1. **Механизм.** После успешного `board mv Done` `integrate.py hook` пишет `<доска>/.ours-state/<ID>.merged.json`: `{tree, commit, task, branch, source, at}`. `tree` — дерево из отметки `.pass.json` (то, что прошло `check.py hook` и было влито), `commit` — коммит в интеграционной ветке или `null` («без изменений»). В событии `done` — поле `merged_record: written`. Не поставился `Done` — записи нет (`not written`): шлюз, если придёт позже руками, гоняет тесты полностью.
2. **Шлюз.** Только `check.py gate` (`hook` запись не читает и не трогает). Быстрые проверки (конфиг, база, запрещённые пути, изменённые тесты, разделы карточки) идут **всегда**. Если они прошли и запись есть, считается `worktree_tree` рабочей копии (та же функция, что в хуке). Совпало с `tree` записи — `run_tests` не вызывается, результат «прошло», в отчёте строка `tests skipped: tree <12 знаков> passed check.py hook and was merged into <ветка> (<коммит|no changes>)`, в событии `gate` `pass` — `tests: skipped`. Не совпало — полный прогон, в событии `tests: full` и `tests_reason: tree differs from the merged one`. Нет записи — как раньше.
3. **Запись одноразовая.** Шлюз читает и удаляет её сразу, до быстрых проверок: при пропуске, при несовпадении дерева и при провале быстрых проверок. Второго шанса у записи нет, повторный шлюз по тому же `Done` прогонит тесты полностью. Повреждённая запись или запись без `tree` — полный прогон.
4. **Почему безопасно.** Дерево — это содержимое файлов, а не история. Схлопывание коммитов ядром (`finalize_delivery_history`: soft reset к базе и один коммит) дерево не меняет, поэтому сравнение по дереву надёжно; любая правка файла после вливания дерево меняет, и тесты идут заново. `integrate.py` вливает только то, что совпало с отметкой `check.py hook`, так что «дерево записи = дерево, прошедшее тесты» держится цепочкой отметка → вливание → запись. Пропуск касается только тестов.
5. **Что закрывает.** Двойной прогон `test.sh` на каждую подзадачу (≈5–10 минут впустую) и риск из записи S4-12 (п. 7 и «Риск»): нестабильный тест больше не может отказать шлюзу уже после вливания, когда дерево то же, — второго прогона нет. Остаётся случай «дерево изменилось после вливания» — он честно идёт в полный прогон.
6. **Две мелочи из приёмки S4-12.** `_dirty_files` теперь смотрит `git status` по тем же путям, что штатный блок и `worktree_tree` (`.`, `:(exclude)kanban`, `:(exclude).symphony`): служебные файлы в `.symphony/` не считаются грязью. `integrate.py hook` для карточки не в `Human Review` выходит **молча**, без события `skipped` (раньше каждый ход каждой карточки писал строку в `check.log` — шум из записи S4-12 снят); остальные `skipped` прежние.
7. **Правка существующего теста.** В `ours/tests/test_integrate.py` из параметров `test_the_hook_skips_what_is_not_a_finished_subtask` убран случай `in-progress` (он ждал событие `skipped` для карточки не в `Human Review`); вместо него — новый тест `test_a_card_outside_human_review_is_passed_over_silently`. Других существующих тестов не менялось. Ядро (`src/`, `tests/`) не менялось.
8. **Что сделано.** `ours/hooks/check.py` (`MERGED_SUFFIX`, `merged_path`, `_take_merged`, `_merged_skip`, поля `Result`, отчёт и событие шлюза), `ours/hooks/integrate.py` (`_write_merged`, `_dirty_files`, молчание вне `Human Review`), тесты в `test_check.py` и `test_integrate.py`, `ours/hooks/README.md`, `ours/README.md`.

- **Не проверено:** в работающей службе и на настоящей доске `oms` (в частности, что ядро вызывает шлюз после `Done` в той же рабочей копии, где лежит влитое дерево).
- **Модель:** Sonnet, effort=high.

### 2026-10-26 — S4-12 — вливание подзадачи и программное Done

Основа: `docs/our/tasks/TASK-S4-12.md` (с разделом «Уточнение архитектора (после запуска)», он главнее текста задания); решения архитектора по REPORT-17, часть I, п. 1 б/в, п. 2. Вторая половина шага 5. Модель: Sonnet, effort=high.

1. **Когда вливаем.** Внутри `hooks.after_run`, **последней строкой, после коммита штатного блока**. Порядок хода: агент ставит `Human Review` → `after_run`: `check.py hook` (проверки, полный `test.sh`) → штатный блок (`git add -A`, коммит `wip: …` или `exit 0`, если изменений нет) → `integrate.py hook` (вливание, `board mv Done`) → ядро завершает ход (`finalize_delivery_history`, шлюз, уборка). Это единственный момент, когда работа уже в коммите, а слот агента ещё занят (запись подзадачи в `_running`) и зависимая подзадача (`blocked_by`) ещё не может стартовать: `blocked_by` отпускает только `Done`, а его ставит сам `integrate.py` — уже после сдвига ветки. Это доказательство п. 1в: вливание и `Done` происходят до конца хода, до освобождения слота и до старта зависимой. `after_run` выполняется в `attempt.py::_after_run_hook`, то есть внутри рабочей задачи агента.
2. **Подоболочка в шаблоне.** Штатный блок завёрнут в `( … )`: его `exit 0` («нет изменений») теперь завершает только подоболочку, и строка `integrate.py hook` выполняется всегда. `|| true` обязателен (иначе `bash` подменит себя скриптом и `os.getppid()` будет другим). Тест запускает настоящий `bash` на отрисованном `after_run` — с изменениями и без.
3. **Одноразовая отметка `<ID>.pass.json`.** `check.py hook` до прогона тестов считает дерево содержимого рабочей копии через временный индекс (`read-tree HEAD`, `add -A` с теми же исключениями, `write-tree`) и при успехе пишет `{ppid, tree, head, at}`. `integrate.py hook` вливает, только если `ppid` совпал с его родителем (оба — дети одной оболочки хука), а `HEAD^{tree}` равен `tree` (добавка архитектора: вливается ровно проверенное; `commit --amend` дерево не меняет, правка файла после проверки — меняет). `head` — только для журнала. Отметка удаляется после чтения. К заданию добавлено `read-tree HEAD` перед `add -A`: так временный индекс повторяет настоящий (отслеживаемые, но игнорируемые файлы остаются), а ошибка `git add` из-за игнорируемой ссылки `kanban` (код 1) допускается, как `|| true` штатного блока. Следствие: если `test.sh` создаёт неигнорируемые файлы, вливание пропускается (`skipped`) — безопасно, но заметно.
4. **Плоский коммит с одним родителем** — уточнение формулы архитектора «merge-tree → commit-tree → update-ref»: все три шага на месте, но у коммита один родитель (старый конец ветки), как ядро само делает при `Done`. История `wip` остаётся в `symphony/<ID>` и в трейлере `Source: symphony/<ID>@<sha>`. `update-ref` проверяет старое значение; сдвинули ветку — одна повторная попытка от нового конца, вторая неудача — как конфликт.
5. **Исходы.** Влито или «без изменений» → `board mv <ID> Done`, раздел `## Integration`, событие `done`; конфликт → ветка цела, `needs_human`, событие `conflict`; ошибка → ничего не двигается, событие `error`. Если `Done` не поставился после вливания — событие `error`, `moved to: Done FAILED`, ветка остаётся сдвинутой (код влит, статус поправляет Сергей). `allowed_bash` не менялся (в `project_config.py` его базовый список прежний), агент маршрутов не вызывает.
6. **Условие `max_concurrent_agents = 1`.** Новый ключ `[agent] max_concurrent_agents` (целое ≥ 1, умолчание 1) выведен в шаблон; при `[integration] enabled = true` и значении не 1 `project.toml` не принимается. Причина — по уточнению архитектора: «вливание и Done подзадачи рассчитаны на одного агента (одно вливание за раз)», **а не** «защита от гонки со шлюзом» (см. п. 7).
7. **Слот агента во время шлюза.** По коду ядра запись убирается из `_running` **до** шлюза: `handle_worker_exit` первым делом вызывает `_pop_running_entry` (`worker_exit.py`, `orch._running.pop`), и только потом `_handle_normal_exit` → `_finalize_clean_exit` → `_record_delivery_history` → `_post_process_done` → `_auto_merge_done_gate_or_block` → `check.py gate`. Свободные слоты считаются как `max_concurrent_agents − len(running) − удерживающие повторы` (`dispatch_state.py::available_slots`). Значит, **оговорка архитектора «пока шлюз гоняет тесты, слот занят» неверна.** Вывод меняется частично: база зависимой подзадачи верна (ветка уже сдвинута до `Done`), но защиты от «шлюз отказал после вливания» нет даже при одном агенте — зависимая подзадача может стартовать до вердикта шлюза. Риск мал: сверка дерева гарантирует, что влито ровно проверенное; отказ шлюза — только нестабильный тест или окружение.
8. **Повторный шлюз — известная цена.** После `Done` ядро ещё раз гоняет `check.py gate` (то же дерево, повторные тесты). Не оптимизировано, как и требовало задание.
9. **Правка существующего теста.** В `ours/tests/test_project_kit.py` утверждение `commands[1] == "set -uo pipefail"` заменено на `commands[1] == "("` и `commands[2] == "set -uo pipefail"` (между `check.py hook` и штатным блоком теперь стоит открывающая скобка подоболочки) плюс проверка, что последняя команда — `integrate.py hook || true`. Других существующих тестов не менялось.
10. **Отклонение от текста задания.** `read_card_front(path, *, with_title=False)`: поле `title` добавляется только по флагу, иначе существующие тесты, сравнивающие результат со словарём из трёх ключей, пришлось бы менять.

- **Риск** (слова архитектора): если шлюз ядра откажет уже после вливания (нестабильный тест), код подзадачи остаётся в интеграционной ветке, а карточка уходит в `Blocked`. Это ловит итоговая проверка задачи перед доставкой (запись S4-11, «На будущее», п. 1).
- **Шум в журнале.** При включённой интеграции каждый ход каждой карточки пишет в `check.log` строку `skipped` (карточка не в `Human Review` или не подзадача) — так требовало задание; объём мал.
- **Что сделано.** `ours/hooks/integrate.py` (подкоманда `hook`), `ours/hooks/check.py` (отметка, `worktree_tree`, заголовок заметки, `git(..., env=)`), `ours/hooks/project_config.py` (`max_concurrent_agents`, плейсхолдер `integrate_py`), шаблон `WORKFLOW.md.tmpl`, заготовка `ours/bin/project-new`, тесты (`test_integrate.py`, `test_check.py`, правка одной строки в `test_project_kit.py`), README. Ядро (`src/`, `tests/`) не менялось.

**На будущее** (по уточнению архитектора):

1. **S4-13, до включения интеграции на доске `oms`:** шлюз `check.py gate` не гоняет тесты повторно, если `HEAD^{tree}` проверяемого коммита совпадает с деревом, которое прошло `check.py hook` и было влито (`integrate.py` пишет запись с этим деревом; схлопывание коммитов ядром дерево не меняет); не совпадает — полный прогон, как сейчас. Это убирает и двойной прогон, и риск отказа шлюза после вливания. В S4-12 не делалось.
2. Итоговая проверка задачи и доставка — как в записи S4-11, «На будущее», п. 1.

- **Не проверено:** на настоящей доске `oms` и в работающей службе.
- **Модель:** Sonnet, effort=high.

### 2026-10-26 — S4-11 — интеграционная ветка задачи: основа

Основа: `docs/our/tasks/TASK-S4-11.md`; решения архитектора по REPORT-17, часть I, п. 1 а/д. Шаг 5 порядка архитектора разделён на S4-11 (основа — здесь) и S4-12 (вливание и Done); деление принято архитектором. Модель: Sonnet, effort=high.

Термин: **интеграционная ветка задачи** — общая ветка, от конца которой стартуют подзадачи задачи и в которую (в S4-12) будут вливаться готовые.

1. **Имя ветки — `integration/<ЗАДАЧА>`, не `symphony/<ЗАДАЧА>`.** Архитектор 26.10.2026 принял это как **исправление своего решения п. 1а** («`symphony/<ЗАДАЧА>`»). Причина: карточка-задача (`role:manager-plan`) сама выполняется агентом, и `worktree.py` создаёт для неё рабочую копию на ветке `symphony/<ЗАДАЧА>`; при чистом старте той же карточки `park()` переименовывает `symphony/<ЗАДАЧА>` в `attempts/…` — интеграционная ветка с таким именем была бы уничтожена. Префикс задаётся ключом `[integration] branch_prefix` (нельзя `symphony/` и `attempts/`); доставка (`.deliver`, будущая задача) будет пушить именно эту ветку.
2. **Связь подзадачи с задачей** — поле `request: <ЗАДАЧА>` в шапке карточки (в ядре это единственная групповая связь, REPORT-17) плюс метка `role:worker` (ключ `worker_label`). Карточка без метки или без `request` — обычная, старт от `base_branch`. Неверный `request` (не по шаблону ID, не с префиксом проекта, равен ID самой карточки) — отказ старта: неправильную связь лучше остановить, чем молча стартовать от `our-main`.
3. **Создание ветки.** Явно — `integrate.py ensure-branch <ЗАДАЧА>` (его вызовет хук нарезки шага 6 «когда план принят»); страховка — `worktree.py` создаёт ветку сам от `refs/heads/<base_branch>`, если при старте первой подзадачи её нет, под той же блокировкой `worktree_lock`, после `_worktree_cleanup --prepare-setup` и до `park()`. Оба пути идемпотентны: существующая ветка не пересоздаётся и не сдвигается. Откат (`rollback`) созданную ветку не удаляет: на неё могла встать другая подзадача.
4. **Выключатель** — `[integration] enabled` (по умолчанию `false`) — запасной вариант (а) архитектора «Сергей вливает A до B». Пока `false`, поведение прежнее и карточка не читается. В `project.toml` проекта `oms` ничего не менялось: включит Сергей позже.

- **Запись базы** (`<ID>.base.json`): прежние поля не изменились (`base_branch` — по-прежнему ветка проекта, на которой снят `baseline.json`), добавлены `start_branch` (откуда взята копия) и `task` (`null` для обычной карточки). `check.py` не менялся: `resolve_base` берёт `base_sha` из записи, запрещённые пути и изменённые тесты сравниваются с ней, `load_baseline` сверяет `base_branch` проекта, а не ветку старта — замер остаётся действительным.
- **Что сделано.** `ours/hooks/project_config.py` (раздел `[integration]`, умолчания всегда заполнены), `ours/hooks/integrate.py` (новый), `ours/hooks/worktree.py` (выбор базы только в чистом старте; режим «обновить» не менялся), заготовка в `ours/bin/project-new`, `ours/tests/test_integrate.py` (60 проверок на настоящих git-репозиториях; `worktree.create` целиком, с настоящей `_worktree_cleanup --prepare-setup`), README. Ядро (`src/`, `tests/`) не менялось.
- **Отклонение от текста задания.** `task_of(front, project, card_id=None)` получила необязательный третий аргумент — ID самой карточки: из шапки (`state`, `request`, `labels`) его не взять, а проверка «`request` равен самой карточке» нужна.

**На будущее** (решения архитектора для S4-12 и доставки):

1. Итоговая проверка задачи (п. 1г) идёт по `integration/<ЗАДАЧА>`, а не по ветке карточки руководителя `symphony/<ЗАДАЧА>` (там план, не код), и на **результате слияния** `integration/<ЗАДАЧА>` с текущим `our-main` (`git merge-tree`; конфликт → `needs_human`): `our-main` уходит вперёд параллельно.
2. Повторный прогон тестов шлюзом (`check.py gate`) после программного Done можно пропускать **только** если шлюз проверяет ровно тот же коммит, на котором только что прошёл `check.py hook`; иначе — полный прогон. Оптимизация — позже, отдельной задачей.

- **Не проверено:** на настоящей доске `oms` и в работающей службе (включать будет Сергей).
- **Модель:** Sonnet, effort=high.

### 2026-10-26 — S4-10 — усилие (`effort`) в `.run`

Основа: `docs/our/tasks/TASK-S4-10.md`, решение архитектора от 26.10.2026. Модель: Sonnet.

- **Зачем.** Правки ядра Claude Code делает с высоким усилием (`claude --effort high`), а очередь умела передавать только модель; S4-09 пришлось запускать строкой вручную, с `--effort high`. Отдельным PR — потому что `ours/hooks/**` входит в контур контроля, правка должна быть простой и строгой.
- **Формат `.run`.** Пусто или одна строка слов через один пробел: `model=…` и/или `effort=…`, каждый ключ не больше раза, порядок любой. Значения только из списков: `model` — `sonnet`/`opus`/`haiku` (по умолчанию `sonnet`), `effort` — `low`/`medium`/`high`/`xhigh`/`max` (константа `EFFORTS`, список `claude --effort`). Неизвестный ключ или значение, повтор, лишние слова, вторая строка, пустое значение, другой регистр — команда не выполняется, причина в `_skipped.log`. Старые файлы (пустые и `model=opus`) работают как раньше.
- **Команда Терминала.** `--effort <значение>` ставится сразу после `--model <модель>` и только при заданном усилии; без него строка прежняя. В `terminal_command` добавлена проверка `effort in EFFORTS` (как для модели).
- **Отчёт.** В `outbox/<код>.run.log` перед шагом открытия Терминала пишется `Запуск: model=… effort=…` (или `effort=по умолчанию`).
- **Не проверено.** На настоящем `osascript`.
- **Модель:** Sonnet.

### 2026-10-26 — S4-09 — PR ядра №2 «лестница»

Основа: `docs/our/tasks/TASK-S4-09.md`, решения архитектора по REPORT-17 (п. 5, PR №2). Модель: Sonnet. Три независимые части, каждая — отдельный коммит (можно откатить по одной).

- **Часть А — `POST /api/v1/{identifier}/reset-budget`** (`orchestrator/api_views.py::reset_issue_budget`, привязка в `core.py`, маршрут в `server.py`). Обнуляет у карточки, которая сейчас не работает: счётчики ходов (`completed_turn_count`, `state_turn_count`, `state_turn_state`), `rewind_count`, `last_error`, флаг `budget_exhausted` (в памяти службы **и** в `state.db`, чтобы он не вернулся после перезапуска) и сохранённую попытку повтора. `restart_count` и `recent_events` не трогаются. Работает под тем же замком, что `confirm-done`. Ответ: `issue_identifier`, `issue_id`, `state`, `changed`, `before` (значения до сброса, включая `paused`). Ошибки: нет карточки — 404 `issue_not_found`; есть работающий воркер или активная аренда — 409 `not_resettable`; прочее — 500 `reset_budget_failed`.
  - **Пауза не снимается.** Это обязанность отдельного маршрута `/resume`; наш код вызовет его сам, если в ответе `before.paused` равно `true`.
  - **Событие запуска не пишется.** У карточки нет активного запуска, а реестр принимает события только для активных запусков (`append_attempt_event`). Вместо этого — одна строка журнала `budget_reset`; учёт сбросов ведёт `ours/` по ответу маршрута.
- **Часть Б — `level` при создании.** `FileBoardTracker.create` / `create_validated` / `create_with_next_identifier` принимают `level` (пишется во frontmatter, только если непустой). `symphony board new --level` и поле `level` в `POST /api/v1/issues` проверяют формат (`[A-Za-z0-9_-]`, до 32 символов) и, если в `WORKFLOW.md` есть `levels`, — что имя там определено (ошибка с перечнем известных). С `--root` (без конфигурации) проверяется только формат. Вызовы без `level` работают как раньше.
- **Часть В — бюджеты.** В `GET /api/v1/issues/{id}` → `budgets`: новый элемент `state_turns` (после `attempt_turns`; `used` — счётчик, если состояние то же, `0` — если другое, `null` — если записи нет), поля верхнего уровня `budget_exhausted` и `paused`. Лимиты теперь берутся из конфигурации **с уровнем карточки** (`_config_for_issue_agent`), то есть показывают то, по чему карточку реально остановят. Если уровень испорчен — тихо берутся общие лимиты (это только чтение). Форма существующих элементов не менялась.
  - **Отличие от текста задания.** Сравнение состояния ведётся по `normalize_state(...)`: ядро хранит `state_turn_state` уже приведённым к нижнему регистру («in progress»), прямое сравнение с «In Progress» никогда бы не совпало. При ошибке конфигурации перехватывается `ConfigValidationError` (общий родитель `LevelError`): так же упала бы и неверная метка `agent.kind` карточки, а чтение не должно давать 500.
  - **Прежние тесты.** Новый элемент в `items` сделал неверными два прежних теста в `tests/test_issue_budgets.py` (полный набор элементов и «все, кроме `attempt_turns`, отключены»). По решению Сергея они обновлены минимально: `state_turns` добавлен в ожидаемое. Смысл тестов не изменился.
- **Что осталось в `ours/`.** Предел числа сбросов на карточку и учёт сбросов (ядро их не ведёт, п. 6 архитектора). **При продолжении карточки наш код обнуляет и счётчик провалов `check.py` в `.ours-state` — `reset-budget` ядра его не трогает.** Сам сброс вызывает только наш код, не агент: `curl` в `allowed_bash` не добавлялся, промпты не менялись.
- **Не проверено.** На настоящем `claude` и в работающей службе (маршрут проверен подменой оркестратора и настоящим `state.db` во временной папке).

### 2026-10-26 — S4-08 — снятие запрета `ours/**` с защитой контура

Основа: `docs/our/tasks/TASK-S4-08.md`, решение архитектора по REPORT-17 (п. 9). Модель: Sonnet.

- **Схема.** Умолчание не меняется: `ours/**` запрещён для всех проектов. Новый необязательный ключ `[checks] allow_paths` (список шаблонов, по умолчанию пуст) освобождает путь **только от `ours/**`**; остальные запреты (`WORKFLOW.md`, `.github/**`, `.git/**`, `.env`, `.env.*`, `*.pem`, `*.key`, проектный `forbidden_paths`) остаются в силе. Каждый шаблон обязан начинаться с `ours/`, иначе ошибка `config: …`.
- **Контур.** Константа `CONTROL_CONTOUR` в `ours/hooks/check.py`: `ours/hooks/**`, `ours/bin/**`, `ours/templates/**`. Запрещён всегда, даже при ошибочном `allow_paths = ["ours/**"]`. Новые файлы управления в этих папках защищены без правки списка. Сообщение нарушения: `forbidden path changed: <путь>`, для контура в конце добавлено `(control contour)`.
- **Расширение списка архитектора.** У архитектора перечислены отдельные файлы; здесь в контуре `ours/bin/**` целиком и `ours/templates/**` целиком, а значит — и `summarize.py`, `project-new`, `project-doctor`, `project-render`, `project-launchd` (они пишут права, `WORKFLOW.md` и базу замеров).
- **Секреты в `ours/`.** Встроенные `.env` и `.env.*` привязаны к корню репозитория и вложенный `ours/prompts/.env` не ловили бы. Добавлены шаблоны `ours/**/.env` и `ours/**/.env.*` (действуют внутри `ours/` всегда); `*.pem` и `*.key` уже ловят любую глубину.
- **Что разрешено агенту (после записи Сергеем в `project.toml` `oms`):** `allow_paths = ["ours/prompts/**", "ours/README.md", "ours/tests/**"]` — промпты, `ours/README.md`, новые тесты в `ours/tests/` (существующие менять нельзя — проверка «существующие тесты»). Остальное в `ours/` запрещено. Файл `project.toml` `oms` в этой задаче не менялся.
- **Тесты.** Новый файл `ours/tests/test_check_contour.py` (28 проверок: схема, контур, секреты, проектный запрет, переименования через `changed_paths`, ошибки конфигурации, охранный перебор всех файлов контура в репозитории).
- **Не проверено.** На настоящей карточке `oms`: первая мелкая карточка, правящая `ours/prompts/`, — проверка в бою.
- **Известная нестабильность (S4-07).** Тест `test_hanging_probe_times_out_off_loop_and_health_stays_live[codex]` (метка `timing`) один раз упал в `project-doctor` под нагрузкой после слияния S4-07 и прошёл при повторе; правка не нужна.

### 2026-10-26 — S4-07 — лимит открытых файлов в очереди команд

Основа: `docs/our/tasks/TASK-S4-07.md`. Модель: Sonnet.

- **Симптом.** `S4-06.test` (ветка меняет `src/`, значит, запускается полный `pytest -q`) дала `245 failed, 97 errors`, почти все — `OSError: [Errno 24] Too many open files`. Те же тесты вручную с `ulimit -n 10240` прошли (3888 passed; 2 падения только из-за node в урезанном окружении).
- **Причина.** Служба очереди работает под `launchd`, у его процессов мягкий лимит открытых файлов — 256, и все дочерние процессы (проверки) его наследуют. Полный набор тестов ядра столько файлов не умещает. Для веток только с правками `ours/` набор маленький, поэтому раньше не проявлялось. `project-doctor` проходит, потому что его `test.sh` сам поднимает лимит.
- **Исправлено** (`ours/hooks/inbox_runner.py`): функция `raise_open_files_limit` при старте службы поднимает мягкий лимит до 10240 (но не выше жёсткого; никогда не понижает; ошибки гасит), дочерние процессы наследуют его; значение пишется в журнал службы. В отчёт `.test` добавлена строка `Лимит открытых файлов: мягкий N`.
- **Урок.** Проверки в службе должны идти с теми же лимитами, что и в обычном Терминале. Для веток с правками `src/` результат `.test` до S4-07 недостоверен.
- **Не проверено.** На настоящем `launchd`: проверить первой же командой `.test` для ветки с правками `src/` (в отчёте должен быть лимит 10240).

### 2026-10-26 — S4-06 — PR ядра №1 «исправления»

Что исправлено (правки в `src/symphony/`, обе внутри ядра — снаружи хуками их не сделать):

1. **Испорченный `level` посреди захода** (замечание приёмки K14 №1). Если карточку правят во время работы воркера и пишут неизвестное имя уровня:
   - `_stall_timeout_ms_for_entry` (`retry_reconcile.py`) ловит `LevelError` и считает таймаут по базовой конфигурации (как для карточки без уровня), поэтому сторож зависания работает до конца захода. В журнал пишется одна запись `level_unusable_for_stall` на запуск; «уже писали» хранит новое поле `level_stall_warned` в `RunningEntry` (`entries.py`).
   - `_transition_agent_phase` (`agent_phase.py`) при смене этапа ловит `LevelError`, кладёт карточку в `Blocked` с разделом `## Level` и возвращает `None` — заход заканчивается штатно, без `phase_transition_error`. Укладка вынесена в общую функцию `_block_ticket_for_bad_level` (`dispatch.py`), её же использует проверка на старте (`_hold_ticket_with_bad_level`); поведение старта не изменилось.
   - Остальные вызовы `_config_for_issue_agent` во время захода: `attempt.py:291` — начало попытки (после проверки на старте, уровень уже проверен); другие места в `src/symphony/orchestrator/` — только старт. Новых незащищённых мест посреди захода не найдено.
2. **Поле `model_used`** в событии `turn_completed`: функция `_model_used` (`agent_phase.py`) берёт `model_usage_since_last_result`, иначе `modelUsage`, выбирает модель с наибольшим `costUSD`. Ключа нет, если данных нет. Поле добавлено в разрешённые в `diagnostics.py`; `docs/reference.md` поправлен.

Как проверено: новые тесты `tests/test_level_midrun.py` и `tests/test_model_used.py`; `ruff check src tests ours` — чисто; `pytest -q` — 3964 passed, 137 skipped, 1 failed (`test_pm_ui_node_regressions`: в рабочей копии нет `node_modules`, пакет `mermaid` не найден — окружение, к правкам не относится); `ours/tests` в чистом окружении — 482 passed; `symphony-pyright` — 0 errors. **Не проверено:** на настоящем `claude` и в работающей службе `oms` не запускалось.

Модель: Sonnet.

### 2026-10-26 — Решения архитектора по REPORT-17 (этап 4): интеграционная ветка, программное Done, доставка, роли, правки ядра, контур контроля

Текст архитектора окончательный и заменяет его предыдущие сообщения. Код этой записью не меняется — решения войдут в задания, которые их реализуют.

Термины:
- **Интеграционная ветка** — общая ветка задачи `symphony/<ЗАДАЧА>`: в неё по очереди «вливаются» готовые подзадачи, чтобы следующая подзадача видела код предыдущих.
- **Вливание без рабочих копий** — слияние прямо в базе git, без переключения веток и папок: `git merge-tree --write-tree` считает результат слияния, `commit-tree` создаёт коммит, `update-ref` сдвигает ветку, только если она не успела измениться.
- **Программное Done** — статус `Done` ставит не агент и не человек, а наш код после успешной проверки.
- **Контур контроля** — набор файлов, которые проверяют работу агента и задают его права; агент конвейера их менять не может.

**Поправка к решению этапа 1:** «Done ставит только человек» теперь относится к задачам; Done подзадачам `role:worker` ставит программа (п. 2).

**Очередь команд расширяется четвёртой командой `.deliver` (п. 4); добро Сергея нужно только для `.merge`.**

**Часть I**

1. **Схема веток — вариант (в), интеграционная ветка на задачу.** Условия:
   - а) `symphony/<ЗАДАЧА>` создаётся от `our-main`, когда план нарезки принят (до старта первой подзадачи); подзадачи стартуют от её текущего конца.
   - б) Вливание подзадачи — без рабочих копий: `git merge-tree --write-tree` → `commit-tree` → `update-ref` с проверкой старого значения (если ветку успели сдвинуть — не вливать). Конфликт → подзадача в `needs_human` со списком файлов, интеграционная ветка не меняется.
   - в) Гонка: следующая подзадача не должна стартовать от конца ветки до вливания предыдущей. Вливание и Done — до освобождения слота агента (в том же `after_run` после `check.py hook`), либо доказать по коду, что диспетчер раньше не возьмёт. Описать в отчёте задачи.
   - г) Итоговая проверка задачи обязательна: подзадачи проверены по отдельности, вместе — нет. Перед доставкой — полный `test.sh` и `check.py` на конце интеграционной ветки относительно `our-main` (карточка-задача проходит свой шлюз). Без зелёной итоговой проверки доставки нет.
   - д) Вариант (а) — запасной переключатель в `project.toml`.
2. **Done подзадачам ставит программа — принято:**
   - а) только карточкам `role:worker` с request задачи, после успешного `check.py` и вливания в интеграционную ветку; Done подзадачи = «влита в интеграционную ветку»;
   - б) задаче целиком Done ставит только Сергей (поправка к решению этапа 1: «Done ставит только человек» теперь относится к задачам);
   - в) агент Done не ставит и маршрутов не вызывает (`curl` в `allowed_bash` не добавлять); каждое программное Done — событие в журнале с результатом проверки.
3. **Автослияние ядром — отказ подтверждён:** `auto_merge_on_done: false` остаётся; в интеграционную ветку вливает наш код в `ours/`; в `our-main` — только PR по добру Сергея.
4. **Доставка** — сразу скрипт `ours/bin/deliver <ЗАДАЧА>` (можно запускать и руками) и четвёртая команда очереди `<ЗАДАЧА>.deliver`, которая его вызывает. Расширение очереди разрешаю.
   - Добро Сергея для `.deliver` не нужно: пуш ветки задачи и PR не меняют `our-main`; добро остаётся на `.merge`.
   - Проверки: только карточка-задача в Human Review с зелёной итоговой проверкой; ветка строго `symphony/<ЗАДАЧА>` проекта из `project.toml`; пуш без `--force`; PR только в `Serg10011/oh-my-symphony`, база `our-main`; заголовок и описание — из карточки задачи и её отчёта.
   - Проверить, куда смотрит `origin` клона `~/dev/oms-pipeline` (GitHub или основная папка) — пушить на GitHub явно.
   - После пуша — `git fetch` в основной папке, чтобы ветку задачи можно было проверять из папки, как обычно.
   - Пушит только эта команда; агенты и служба конвейера не пушат.

**Часть II**

5. **Правки ядра — как рекомендовано, тремя PR,** каждый под порог правила 1, с тестами и заявкой «иначе нельзя» в описании:
   - №1 «исправления» (≈30 строк): замечание приёмки K14 №1 + `model_used`;
   - №2 «лестница» (≈90): `reset-budget` (отказ, если карточка выполняется; событие `budget_reset`), `level` в API/CLI создания карточки, `state_turns` и `budget_exhausted` в `budgets`;
   - №3 «уровень этапа» (≈40, `stage_levels`) — вместе со стратегиями (п. 8).
   Сброс вызывает только наш код, не агент.
6. **Предел сбросов и потолок — как рекомендовано:** не больше 1 сброса на карточку; общий потолок на задачу в ходах; учёт в `ours/` по записям запусков (не по памяти ядра — она обнуляется при перезапуске); исчерпан — стоп, `needs_human`, короткий отчёт; все числа — в `[ladder]` `project.toml`.
7. **Руководитель — в той же доске, роль меткой.** Метки названы по должности (в коде — английские, в документах — с русской расшифровкой):
   - `role:worker` («работник») — подзадача работника (только им программа ставит Done);
   - `role:manager-plan` («руководитель: план») — руководитель нарезает задачу на подзадачи (карточка-задача; она же потом проходит итоговую проверку и доставку);
   - `role:manager-diagnose` («руководитель: разбор») — руководитель разбирает провал и решает, что дальше.

   Архитектор на доске не работает (он в чате) — метки для него нет. Метка — вид работы, модель задаёт уровень (`level`): лимиты ролей — через уровень карточки (S1/S2 со своими `max_turns` и `max_state_turns`); трудная подзадача может остаться `role:worker`, но получить уровень S1.
8. **Verify другой моделью — как предварительно у Сергея:** по умолчанию и в «автомате» — проверка другой моделью; в «экономии» — та же модель, экономию проверить на практике. Нужен PR ядра №3 (`stage_levels`). Пока лестница выключена и копится статистика — действует текущий порядок.
9. **Запрет `ours/**` для `oms` — снять, но сохранить защищённый «контур контроля»,** который агент конвейера править не может: `ours/hooks/check.py`, `ours/hooks/worktree.py`, `ours/hooks/project_config.py` (проверяет права агента), хук вливания в интеграционную ветку и программного Done, `ours/templates/WORKFLOW.md.tmpl` (хуки и права агента), `ours/bin/inbox-runner`, `ours/bin/post-merge`, `ours/bin/deliver`. Правило: контур контроля и прав меняют только задачи, запущенные человеком (локальный Claude Code), не карточки конвейера.

**Порядок реализации.**
1. S4-04 (баг очереди) — доделать.
2. PR ядра №1 «исправления».
3. Снятие запрета `ours/**` с защитой контура (п. 9). После него часть мелких задач этапа (документы, `ours/` вне контура) можно пускать карточками на доску `oms` с уровнем от архитектора — копим статистику уже сейчас.
4. PR ядра №2 «лестница».
5. Интеграционная ветка + программное Done (п. 1–2).
6. Нарезка: хук «план-файл → карточки», промпты `role:manager-plan`.
7. Доставка (п. 4).
8. `role:manager-diagnose` и правила лестницы (`[ladder]`).
9. PR ядра №3 (`stage_levels`) и стратегии.

Если по ходу порядок упрётся в зависимость, которой архитектор не видит, — порядок можно поменять, написав почему.

Основа: REPORT-17, раздел 5; предварительное обсуждение в чате этапа 4 (Opus) 26.10.2026; решение — архитектурный чат. Модель: Sonnet.

### 2026-10-26 — S4-04: исправление очереди и итоги первого боевого запуска очереди

Основа: `docs/our/tasks/TASK-S4-04.md`. Модель: Sonnet.

- **Итоги боевого запуска.** `S4-03.run`: `git fetch` из `launchd` прошёл, рабочая копия создана, окно Терминала открылось после ответа на запрос macOS «Автоматизация» (разрешение запомнено). Но служба упала на повторной передаче stdin (этот баг). `S4-03.merge`: первое слияние через очередь прошло целиком — PR #24, `post-merge` из окружения службы, уборка рабочей копии и файла задания.
- **Причина бага.** `run_process` вызывал `proc.communicate(input=…, timeout=0.5)` в цикле. Если процесс не завершился за 0,5 с (`osascript` ждал ответа на запрос macOS), второй вызов с тем же `input=` падал с `ValueError`, а процесс оставался без присмотра.
- **Исправлено** (`ours/hooks/inbox_runner.py`): `input` передаётся только при первом вызове; любое исключение внутри ожидания убивает группу процессов (`SIGKILL`) и поднимается дальше. При `ВНУТРЕННЯЯ ОШИБКА` в отчёт и в `_skipped.log` попадает полная трассировка. В `post_merge.py` того же класса ошибок нет: там только `subprocess.run` без `input=`.
- **Урок.** Подмены, которые отвечают мгновенно, не ловят ошибки с таймингом — нужен тест с медленным дочерним процессом (добавлены: ~1,5 с ожидания у `run_process` и у `open_terminal`).

### 2026-10-26 — S4-03: нарезка руководителем и продолжение карточки (REPORT-17)

Основа: `docs/our/tasks/TASK-S4-03.md`, `docs/our/reports/REPORT-17.md`. Код не менялся. «Запуск» — проверено запуском (один вызов настоящего `claude`, чтение копии `state.db` проекта `oms`), остальное — по коду.

- Зависимости `blocked_by` в ядре есть; успешный блокер у нас — только `Done`; блокер в `needs_human`/`Blocked`/`Human Review` держит зависимого вечно; «родитель закрывается, когда закрыты дети» в ядре нет. `factory` и PM Workspace целиком не подходят (чужие колонки и человеческие согласования); у `factory` берём формат плана. Нарезка — своя программа в `ours/` («агент пишет файл-план, хук создаёт карточки»).
- Ловушка веток подтверждена: `symphony/B` создаётся от локальной базы, код A туда попадает только после PR человека. Варианты: слияние A человеком, цепочка веток, интеграционная ветка на задачу (рекомендована).
- Продолжение той же карточки: флаг `budget_exhausted` ничем не снимается (проверено в `state.db`: у `OMS-1` стоит) — нужен точечный сброс в ядре (≈45 строк, 3 файла `src/`); запасной путь — новая карточка с отсылкой.
- Цена карточки: догадка подтверждена, `cost_usd` в событии — нарастающий итог; цена = сумма `runs.cost_usd` по запускам (OMS-4 ≈ $0,065, OMS-3 ≈ $0,051). Фактическая модель ответа есть в `modelUsage` кадра `result` (проверено запуском).
- Замечание приёмки №1 найдено в двух местах (`retry_reconcile._stall_timeout_ms_for_entry`, `agent_phase._transition_agent_phase`), исправление ≈15 строк. Решения для человека — REPORT-17, раздел 5. Модель: Sonnet.

### 2026-10-26 — S4-02: очередь команд (`ours/bin/inbox-runner`)

Основа: `docs/our/tasks/TASK-S4-02.md`. «Запуск» — проверено тестами; «по коду» — по чтению кода.

**Что сделано.**
- `ours/hooks/inbox_runner.py` (вся логика) и тонкая обёртка `ours/bin/inbox-runner`: служба смотрит `tmp_workspaces/inbox/` и выполняет ровно три команды-файла: `.run`, `.test`, `.merge`. Файл сначала забирается в `inbox/done/`, потом выполняется; отчёты — в `outbox/`; одна копия службы (замок); самообновление после `.merge`; SIGTERM убивает группу процессов текущего шага и дописывает отчёт.
- `ours/bin/inbox-runner-launchd`: пишет plist агента пользователя (`KeepAlive`), `launchctl` не вызывает.
- Тесты `ours/tests/test_inbox_runner.py`: проверки входа (имена, ветки, модели, содержимое, ссылки, размер, кодировка, свежесть), порядок шагов `.run`/`.test`/`.merge`, настоящий git во временных репозиториях, подпроцесс `inbox-runner --once` с поддельными `gh` и `osascript`.
- `CLAUDE.md` — правила очереди; `ours/README.md` — раздел «Очередь команд».

**Решение архитектора по `.run`.** Служба очереди сама готовит рабочую копию (`git fetch origin our-main`, `git worktree add --no-track -b dev/<код> … origin/our-main`) и только потом открывает Терминал с Claude Code внутри неё. Если ветка или папка уже есть — ничего не делается. `.test` повторять можно (старый отчёт сохраняется с меткой времени), `.run` и `.merge` — только под новым кодом.

**Отступление от текста задачи.** В запрос `gh pr list` добавлено поле `baseRefName`, и служба требует `our-main`: фильтр `--base` у `gh` не заменяет явную проверку ответа.

**Риски.**
- Кто может писать в `tmp_workspaces/inbox/`, тот может запустить эти три команды; защита — жёсткие шаблоны, проверка PR, запрет записи для сессий Claude Code.
- Разрешение macOS «Автоматизация» (управление Терминалом) нужно подтвердить один раз.
- Для `.test` с правками `src/` служба ставит пакет в `.venv` копии (`pip install -e`) — нужна сеть.

**Не проверено.** Поведение на настоящем `osascript` (вызов `osascript -` с командой аргументом) и `gh`; доступ `git fetch` из окружения launchd к GitHub. Модель: Sonnet.

### 2026-10-25 — Итоги боевого запуска S4-01

Основа: первый запуск `ours/bin/post-merge` на настоящем Mac (25.10.2026).

- PR #22 влит. `post-merge` подтянул клон `oms-pipeline` (5394a93 → a05d8c5), выбрал план «остановить службу → project-doctor → запустить»; настоящие `launchctl bootout` и `bootstrap` отработали, служба после doctor снова отвечала; уборка убрала рабочую копию `oms-dev/S4-01` и `inbox/S4-01.md`.
- `project-doctor` честно показал провал `test.sh` в короткой среде: `1 failed, 307 passed`. Причина — тест S4-01 запускал `post-merge` через шебанг (`#!/usr/bin/env python3`), а при коротком `PATH` (`/usr/bin:/bin:/usr/sbin:/sbin`) это системный Python без `tomllib`. Исправлено в S4-02 (запуск через `sys.executable`).
- Поэтому «последний обработанный коммит» для клона не записан, и следующий запуск `post-merge` повторит doctor.
- **Урок:** прогон тестов вручную в обычном Терминале этого не ловит. Нужен прогон под `env -i HOME="$HOME" PATH=/usr/bin:/bin:/usr/sbin:/sbin` — он теперь в разделе «Проверка» `ours/README.md`, а правило для новых тестов: подпроцессы со скриптами `ours/` — только через `sys.executable`.

### 2026-10-25 — S4-01: слияние и обновление (`ours/bin/post-merge`)

Основа: `docs/our/tasks/TASK-S4-01.md`. «Запуск» — проверено тестами; «по коду» — по чтению кода.

**Что сделано.**
- `ours/bin/post-merge` (тонкая обёртка) и `ours/hooks/post_merge.py` (вся логика): проверки до действий, `fetch` + `pull --ff-only` в основной папке и клонах проектов, решения по таблице `RULES` в начале модуля, отказ трогать службу при карточках в работе (код 2), `project-doctor` с обязательным запуском службы обратно, `--dry-run`, замок от двух запусков, уборка влитых рабочих копий `dev/*`. «Последний обработанный коммит» хранится в `tmp_workspaces/post-merge-state.json` и обновляется только после успешных действий.
- Тесты `ours/tests/test_post_merge.py` (23): настоящие временные git-репозитории, внешнее (`launchctl`, API, doctor/render) подменяется полями `Ops`; один тест запускает команду подпроцессом с поддельным API.
- `CLAUDE.md`: рабочая копия, порядок слияния, модель вместо кредита, `post-merge` вместо `git pull` в итоговом сообщении. `ours/README.md`: раздел «После слияния».

**Найдено в ядре (по коду).** `_on_tick` перечитывает `WORKFLOW.md` на каждом такте, файлы промптов читаются при каждой сборке конфигурации — после `project-render` и правки `ours/prompts/*` перезапуск не нужен. Перезапуск нужен только после правки `src/` (код в памяти процесса); хуки `ours/hooks/*` — отдельный процесс на вызов.

**Решения по мелочам.**
- `applied_sha` хранится на цель (основная папка / клон), не на проект: если из двух проектов один отложен, второй при повторе получит лишний, но безвредный перезапуск.
- Уборка не трогает ветку `dev/*` без собственных коммитов (только что созданная ветка формально «влита»).
- Если служба отвечает, но не управляется launchd (запущена из Терминала), перезапуск считается ошибкой (код 1) с подсказкой.

**Не проверено.** `kickstart` на настоящем `launchctl` (`bootout` и `bootstrap` проверены боевым запуском 25.10.2026, см. запись ниже); `--dry-run` на настоящих папках — см. описание PR. Модель: Sonnet.

### 2026-10-25 — Приёмка K14 и закрытие этапа 3 (проверка на Mac)

Основа: приёмка K14 и проверка на Mac mini (чат этапа 3). «Запуск» — проверено запуском на Mac.

- **K14 принята целиком,** вырезать ничего не стали (правка ядра больше ориентира — ≈240 строк, 8 файлов + `workflow/levels.py` — оставлена как есть).
- **Проверено запуском на Mac:**
  - конвейер на настоящем `claude`: карточки OMS-2, OMS-3 и OMS-4 прошли весь путь от Todo до Done через шлюз `check.py gate`;
  - служба под launchd — сначала с ручным plist, затем с файлом от `project-launchd` (K13);
  - комплект K13: права агента из `project.toml` (`[agent] allowed_bash`), сторож зависания из `tests_s`, пересоздание `WORKFLOW.md` без ручных правок;
  - K14: `level: L2` в карточке дал в журнале `model=haiku effort=high`; строгие лимиты 2 / 8 / 20 / 2 карточке не помешали — по одному ходу на этап;
  - клон `~/dev/oms-pipeline` обновлён до `5394a93`, база переснята: `PIPELINE_TESTS passed=4227 skipped=137 failed=0`.
- **Замечания приёмки — в задание этапа 4:**
  1. Уровень, испорченный посреди захода (карточку правят во время работы), роняет воркер, и сторож зависания у этой карточки перестаёт работать до конца захода.
  2. Лимит «2 хода на этап» (`max_state_turns`) может мешать повторному Verify после возврата в In Progress — проверить при пересмотре лимитов под роли.
  3. Влияние `--effort` на haiku по журналу не видно.
- **Находки последней карточки:**
  1. `claude` отклонил команду с кавычками (ограничение `--allowedTools`); агент обошёлся без неё.
  2. Какой моделью реально отвечал `claude`, по журналу службы не видно — там только то, что ядро вписало в команду (`--model`). Для статистики этапа 4 нужна фактическая модель ответа (например, `modelUsage` из итогового кадра `result`).
- **Цена карточек (догадка, не проверено):** счётчик `cost_usd` при `--resume`, похоже, накопительный; тогда OMS-4 стоила ≈ $0,065, OMS-3 — ≈ $0,05, а не $0,11, как записано в итогах K13. Проверить на этапе 4 вместе с учётом цены по уровням.
- **Кредит:** K14 ≈ $7, остаток $25 на 25.10; затем ≈ $24 ушло на облачные сессии по EVE (вне конвейера); до 05.11 осталось ≈ $1, облачных задач по оркестратору до конца срока нет. Разработка оркестратора дальше — локальным Claude Code на подписке Max (расходует лимит подписки, резерв для Сергея — не меньше 10%), в отдельной рабочей копии (git worktree), а не в основной папке, из которой работает служба.
- **Состояние Mac:** служба `oms` работает под launchd и держит Mac бодрствующим; остановить, если не нужна: `launchctl bootout gui/$(id -u)/local.symphony.oms`. В клоне остались ветки `symphony/OMS-1…4` — ни на что не влияют.

### 2026-10-25 — Итоги K14 (REPORT-16)

Основа: `docs/our/tasks/TASK-K14-task-level.md`, `docs/our/reports/REPORT-16.md`. «Запуск» — проверено запуском (тесты, сквозной прогон с поддельным `claude`, мелкие вызовы настоящего `claude` в облачной среде), «по коду» — по чтению кода.

**Что сделано.**
- Поле `level: S1` во frontmatter карточки; уровни — ключ `levels` в `WORKFLOW.md` (модель и усилие для `claude` и `codex`, необязательные `max_turns` и `max_state_turns`), `agent.default_level`. Строгий разбор: имя `[A-Za-z0-9_-]+`, неизвестный ключ, пустая строка, усилие claude вне `low|medium|high|xhigh|max`, `max_turns` меньше числа активных состояний, `max_state_turns` меньше 1, другой бэкенд — ошибка. Приоритет: уровень карточки → `default_level` → как раньше.
- `_config_for_issue_agent` вставляет `--model` и `--effort` сразу после программы `claude` (существующие заменяются), для `codex` подставляет `codex.model` и `codex.reasoning_effort`, лимиты уровня подменяют `agent.max_turns` и `agent.max_state_turns` (`max_state_turns_by_state` важнее; `max_total_turns` и `max_attempts` общие). `--model` важнее `ANTHROPIC_MODEL` службы (запуск).
- Неизвестный уровень: карточка до хода уходит в `Blocked`, в ней раздел `## Level` с причиной; журнал `level_rejected`.
- Учёт: `task_level`, `model`, `effort` в `worker_turn_started` и в событии `turn_started` запуска (рядом с `cost_usd` из `turn_completed`).
- Комплект: `[levels]` (L1/L2 — haiku medium/high, S1/S2 — sonnet high/max, `default_level = "L1"`), `[limits] max_state_turns`, строгая заготовка `project-new` (2 / 8 / 20 / 2, `max_check_failures = 2`), `card-template.md`, разделы «Уровни задач» и «Ходы, попытки и лимиты» в `ours/README.md`.

**Проверка (запуск).** `python -m pytest -q`: 3942 прошли, 136 пропущены, 2 упали — те же известные (`test_pm_ui_node_regressions`, `test_stop_kills_term_resistant_group_after_leader_exits`); новых тестов ядра 56. `python -m pytest ours/tests -q`: 285 прошли (было 244). `ruff check src tests ours` и `symphony-pyright` — чисто. `ours/tests/e2e/run.sh`: карточка без `level` идёт с `--model haiku --effort medium`, `level: S1` — с `--model sonnet --effort high`, `level: S9` — в `Blocked` без хода; агент без продвижения этапа получает ровно 2 хода и уходит в `needs_human`. Существующие тесты проекта не менялись; в `ours/tests` поправлены два ожидания под новые значения заготовки.

**Что выяснено (REPORT-16).**
- `claude` 2.1.310: `--effort` — `low, medium, high, xhigh, max`; неизвестное значение — только предупреждение и усилие по умолчанию, поэтому проверка в конфигурации строгая. У haiku `medium`, `high`, `max` приняты без предупреждения (запуск); влияет ли усилие на haiku — не видно.
- Продолжение сессии с другой моделью работает (запуск); по коду `--resume` используется только внутри одного этапа и при восстановлении после аварии, поэтому новая сессия при смене `level` не нужна. Смена `level` действует со следующего этапа или попытки.
- Лимиты (п. 7 задания): `max_state_turns` → `needs_human` + пауза, флага `budget_exhausted` нет (запуск); `max_turns` и `max_total_turns` → `needs_human` + флаг `budget_exhausted`, штатно не снимается (запуск для `max_turns`, по коду для `max_total_turns`); `max_attempts` → `Blocked`, флага нет (по коду). Счётчики (`state_turn_count`, `rewind_count`, `completed_turn_count`) живут в памяти службы и при возврате карточки не сбрасываются — для этапа 4 («руководитель продолжает ту же карточку») нужен точечный сброс счётчиков и флага.
- Заметка «Budget Exceeded», `limits` в `/api/v1` показывают общие лимиты, а не лимиты уровня (по коду); остановки работают по лимитам уровня.

**Расхождение с заданием.** Правка ядра вышла за порог «≈100 строк, 5 файлов»: 8 существующих файлов + новый модуль `workflow/levels.py`, ≈240 строк кода без тестов (REPORT-16, начало). Работа доведена до конца, чтобы решать по готовому диффу; что можно вырезать — там же. Решает Сергей при слиянии.

**Не проверено.**
- Настоящий `claude` на Mac и прохождение карточки со `level` на настоящем `claude` (сделает Сергей); влияние `--effort` на haiku; `low` и `xhigh` с haiku. Закрыто на Mac (приёмка K14): карточка со `level: L2` прошла на настоящем `claude` (`model=haiku effort=high` в журнале); влияние `--effort` на haiku по-прежнему не видно.
- Остановки по `max_total_turns` и `max_attempts` — только по коду.
- Строки существующего проекта `oms`: заготовка его не обновляет, `[levels]` и новые `[limits]` переносятся вручную.

**Открытые вопросы.**
1. Укладывать ли правку ядра в порог (что вырезать) или принять как есть.
2. Сброс `budget_exhausted` и счётчиков (из K13) — теперь тем острее со строгими лимитами; решается в этапе 4.

**Решения для этапа 4 (лестница), приняты до K14.**
Уточнено и частично заменено записью «2026-10-26 — Решения архитектора по REPORT-17» (роли — метками `role:worker` / `role:manager-plan` / `role:manager-diagnose`; нарезку делает руководитель на доске; Verify — по стратегии).
- Роли (уточняет ARCHITECTURE 17, где нарезку делает архитектор):
  Сергей — решения и слияние; архитектор (Claude в чате) — план работ и
  описание каждой задачи с критериями приёмки, назначает задаче уровень и
  усилие руководителя; руководитель (Sonnet в конфигурации B, на доске) —
  нарезает задачу на подзадачи, пригодные для работника, назначает каждой
  уровень работника и обоснование, разбирает провалы, переписывает задание,
  при необходимости один раз решает подзадачу сам; работник (Haiku, позже
  локальная модель) — исполнение подзадач.
- Зачем нарезка у руководителя, а не у архитектора: архитектор — сильная
  модель в чате на подписке Сергея плюс его время и ручная пересылка;
  руководитель работает на конвейере автоматически, дешевле, видит код
  репозитория, а при провале разбирает то, что сам нарезал. Руководитель
  режет под самый дешёвый уровень работника, который допускает стратегия, и
  поднимает уровень подзадачи только там, где кусок нельзя упростить. Правило
  «сначала режь мельче, потом поднимай уровень» ограничено накладными
  расходами: каждая карточка проходит четыре шага и полный прогон тестов,
  поэтому подзадача — наименьший кусок, который можно проверить отдельно.
- Как это ляжет на доску: задача от архитектора — карточка, у которой первый
  шаг «нарезка» (руководитель); подзадачи — отдельные карточки со штатными
  зависимостями ядра (blocked_by: подзадача стартует, когда готовы
  предыдущие); задача закрывается, когда закрыты все подзадачи. Перед своей
  разработкой изучить готовые средства ядра: factory (Wayfinder → доска) и
  PM Workspace (план → WBS → карточки с зависимостями), docs/reference.md и
  docs/pm-workspace.md.
- Основной механизм экономии — точные уровни: архитектор — для задачи
  руководителя, руководитель — для подзадач работника (поле level,
  ARCHITECTURE 5.1 и 9). Опыт Сергея: когда архитектор сам назначал уровень
  модели, проблем практически не было. Лестница — страховка на редкий
  случай, а не обычный путь: частые повышения — сигнал уточнить нарезку или
  назначение. В карточке рядом с уровнем — короткое обоснование; точность
  назначений проверяется статистикой (назначенный уровень против уровня, на
  котором подзадача закрылась) и возвращается тому, кто назначал.
- Предварительный расчёт выгоды (условные цены, не данные: попытка Haiku — 1;
  повторная попытка Haiku с усилием на 2 ступени выше — 1,5; решение
  задачи Sonnet — 3; разбор провала руководителем — 0,5; подробная нарезка
  под дешёвую модель — 0,5; вторая попытка спасает 40% провалов). Путь
  «работник → вторая попытка → разбор → руководитель решает сам» в среднем
  дешевле, чем сразу руководитель, если работник справляется с первой
  попытки чаще чем примерно в 60% случаев (90% → 1,9 против 3; 80% → 2,2;
  70% → 2,6; 60% → 2,9; 50% → 3,3); в худшем случае (провалилось всё) задача
  стоит примерно вдвое дороже (6,5 против 3). Вторая попытка окупается, только
  если спасает больше ~40% провалов. Пересчитать по журналу (стоимость хода
  K07 + уровень и модель хода K14) после 1–2 недель работы.
- Следствия расчёта: (1) уровень назначается по ожидаемому успеху: если
  руководитель не уверен, что работник справится с первой попытки, и кусок
  нельзя нарезать мельче, подзадача сразу получает S1 (пограничная — выше,
  ARCHITECTURE 5.1); цена нарезки руководителем тоже входит в расчёт; (2) вторая
  попытка работника — только после механического провала с понятным логом
  (упали тесты, линтер, сборка): работник получает лог и исправляет, усилие
  на 2 ступени выше (шаг настраивается); после хода без продвижения, отказа в
  разрешении или «не понял задание» второй попытки нет — сразу руководитель;
  (3) первые 1–2 недели лестница выключена; пока шага нарезки руководителем
  на доске нет (появится в этапе 4), карточки пишет и уровень назначает
  архитектор; провал → needs_human; копим цифры для пересчёта.
- Порядок при провале (когда лестница включена): на этапе — не больше 2
  ходов без продвижения (max_state_turns = 2); на задачу — не больше 2
  попыток работника (вторая — по правилу выше). Затем — руководитель, а не
  слепое повышение модели: он определяет причину. Окружение или доступ (отказ
  разрешения, падение базы, нет данных) → needs_human без смены модели;
  неясное задание → переписать задание, ещё одна попытка работника на том же
  уровне; задача трудна для модели → руководитель один раз решает её сам с
  чистой ветки. Если и это не прошло — needs_human с диагностическим отчётом
  для архитектора. Повтор одной и той же ошибки — признак окружения или
  задания, а не слабости модели.
- Этап Verify сейчас выполняет та же модель, что и работу; по спецификации
  проверяющий — другая модель (ARCHITECTURE 4.3). Отдавать ли Verify
  руководителю — решить и посчитать в этапе 4 (это добавочная цена на каждую
  карточку).
- Лимиты ходов: для облачных моделей строгие (по умолчанию 2 хода на этап,
  8 за заход, 20 на карточку, 2 возврата назад, 2 провала check.py); у
  локальной модели — свои, щедрее, задаются её уровнем. Со строгими лимитами
  карточки чаще останавливаются по лимиту, поэтому сброс budget_exhausted
  (открытый вопрос K13) решается в этапе 4: руководитель продолжает ту же
  карточку, а не заводит новую. Все эти значения стартовые: в этапе 4
  пересмотреть все лимиты ходов под роли (нарезка руководителем, шаги
  работника, проверка, разбор провала, решение руководителем), добавить
  общий потолок на задачу целиком (сумма по всем её подзадачам, иначе
  нарезка умножает расход) и уточнить по статистике первых недель.
- Стратегии запуска — именованные наборы настроек лестницы и лимитов:
  «экономия» (работник по уровню; вторая попытка только после механического
  провала; наименьший расход, больше участия человека; открыто, решить
  расчётом и опытом на одинаковых задачах: после двух провалов — сразу
  архитектору (Claude в чате, сильная модель на подписке Сергея плюс его
  время — не факт, что дешевле) или сначала короткий разбор руководителем;
  Verify — той же моделью или руководителем);
  «баланс» (порядок при провале из пункта выше, с руководителем; Verify —
  руководитель, то есть другая модель);
  «автомат» (руководитель разбирает, переписывает и решает сам; Verify —
  руководитель; needs_human только при причине «окружение или доступ»;
  наименьшее участие человека, наибольший расход в пределах потолка).
  Стратегия выбирается в project.toml, может задаваться профилем лимитов
  (этап 5: например, ночью «автомат», в рабочий день «экономия») и позже из
  интерфейса; любое значение стратегии можно переопределить.
- Главное условие для всех стратегий, включая «автомат»: средняя цена
  принятой задачи на конвейере ниже, чем та же работа в приложении (Claude
  Code или чат с сильной моделью). Защита: (1) потолок трат на карточку — не
  больше цены задачи «сразу сильной моделью»; дошла до потолка — стоп и
  needs_human с отчётом, а не продолжение трат; (2) регулярное сравнение
  цены принятой задачи по стратегиям с ценой в приложении (для оценки —
  стоимость облачных сессий Claude Code на похожих задачах); стратегия,
  проигрывающая приложению, пересматривается; (3) накладные расходы этапов
  тоже считаются: четыре этапа заново читают задание и код, промпт Verify
  требует аудит безопасности и таблицу критериев — для «экономии» решить на
  этапе 4, нужны ли облегчённые промпты этапов.
- Руководитель планирует с учётом лимитов ходов. При нарезке и перед
  запуском каждой следующей подзадачи он видит лимиты и их остаток: ходы на
  шаг, за заход и на карточку, возвраты назад и провалы check.py по каждой
  подзадаче, общий потолок ходов на задачу целиком (счётчики по карточке ядро
  уже отдаёт: attempt_turns, total_turns, retries, rewinds — api_views.py).
  По ним он нарезает подзадачи такого размера, чтобы каждая укладывалась в
  лимиты с запасом, а задача доводилась до конца в пределах общего потолка;
  сначала доводит до конца начатое, а не открывает новое; если остатка не
  хватает — не начинает новую подзадачу и оставляет сводку на утро. Лимиты
  при этом соблюдает код, а не модель (ARCHITECTURE 15.3). Данные о деньгах и
  окнах подписки руководителю пока не даём: их подсчёт сам стоит лимитов;
  вернуться к этому, если понадобится. Сводка — несколько строк.
- K14 — только переключатель уровня, лимиты и учёт модели хода; когда
  повышать уровень и звать руководителя, решает лестница (этап 4).
- Все правила лестницы — настройки пользователя, а не код: включена ли
  лестница, порядок уровней, кто руководитель, сколько попыток на уровне, на
  сколько ступеней поднимать усилие, что делать при каждом типе причины,
  ограничения по профилю лимитов (этап 5, например «в рабочий день не выше
  L2»). Хранятся в project.toml (раздел [ladder]); в карточке можно
  переопределить (level, запрет повышения — ARCHITECTURE 9 уже предусматривает
  поле ladder в задаче). Общие значения для всех проектов — возможно, один
  файл в ~/symphony-projects/, проект переопределяет (решить на этапе 4).
  Интерфейс — позже (веб-страница или бот, этап 6) поверх того же
  project.toml, без второго места хранения настроек.

**Кредит:** K14 ≈ $7. Остаток $25 на 25.10.

### 2026-10-25 — Итоги пробы на Mac (K13, REPORT-15)

Основа: проба Сергея на Mac mini и `docs/our/reports/REPORT-15.md` (правки комплекта). Ядро (`src/`) не менялось. «Запуск» — проверено запуском на Mac (проба) или в тестах, «по коду» — по чтению кода.

**Стенд.** Проект `oms` = сам форк (база `our-main`, клон `~/dev/oms-pipeline`). Python 3.14.5 (python.org), Node из Homebrew (`/opt/homebrew/bin`), `claude` в `~/.local/bin/claude`. Модель проекта — `claude-haiku-5-5`: `ANTHROPIC_MODEL=haiku` в окружении службы, процесс `claude` наследует её.

**Что закрыто запуском на Mac.**
- `ours/tests` — 198 passed; `ours/tests/e2e/run.sh` — PASS; `fcntl.flock` и `killpg` на macOS работают.
- Настоящий `claude`: карточки OMS-2 (Todo → Human Review → Done) и OMS-3 (то же, службой под launchd) прошли все этапы. Строка контракта у базы и у обеих карточек — `PIPELINE_TESTS passed=4084 skipped=137 failed=0` (основной набор 3886 / 136 / 0 без одного теста `tests/test_pm_ui_node.py::test_pm_ui_node_regressions`, плюс `ours/tests` 198).
- `git merge-tree --write-tree` из Bash агента работает (чистое дерево, `qa/merge-tree.log`). Нужны разрешение `Bash(git merge-tree:*)` в `--allowedTools` и доступ к `.git`, который служба выдаёт сама (в логе `claude_git_roots_granted`).
- Долгий хук `check.py hook` (полный прогон `test.sh`): 3 мин 52 с (OMS-2) и 3 мин 35 с (OMS-3), воркер не отменён.
- Подтверждение Done через шлюз `check.py gate` прошло для OMS-2 и OMS-3 (последняя — под launchd).
- Защиты ядра на настоящем `claude`: `max_turns` (30) → `budget_exhausted` + `needs_human` (OMS-1); сторож `stalled_session`; пауза `paused for operator inspection`, снимается `POST /api/v1/<ID>/resume`.
- Окружение службы под launchd (`ps eww -p <pid>`): `PATH` как в plist (`/usr/bin:/bin:/usr/sbin:/sbin`), `HOME`, `USER`, `LOGNAME`, `SHELL=/bin/zsh`, `TMPDIR=/var/folders/…/T/` (выдал launchd); `LANG` и `LC_ALL` не заданы; `launchctl limit maxfiles` = 256. `setup.sh` и `test.sh` в таком окружении отработали.
- `project-doctor`: сначала FAIL (113 падений, недетерминированно, в одном воспроизведении sqlite «unable to open database file»), PASS после `ulimit -n 10240` в начале `test.sh`. Замеренные таймауты: `tests_s` = 420, `gate_ms` = 480000, `hooks_ms` = 480000. Причина «лимит файлов 256» — предположение (один зелёный прогон не доказательство).

**Что пошло не так и чем исправлено в K13.**
1. `claude -p --permission-mode acceptEdits` отклоняет Bash вне списка; агент крутился до `max_turns`. → Ключ `[agent] allowed_bash`, шаблон добавляет `--allowedTools` со встроенной базой (`/bin/bash <доска>/test.sh`, `git merge-tree`, `test`, `echo`, `cat`, `wc`, `head`, `tail`) и значениями проекта; проверка ключа отказывает на пустых записях, `*`, `Bash(*)`, голых `bash`/`sh`/`env`… и записях, похожих на отключение проверок. Составные команды (`&&`, `;`, `|`) отклоняются, если хоть одна часть не разрешена — это в подсказке карточки и в `CLAUDE.md`.
2. Сторож `stall_timeout_ms` (жёстко 300000) срабатывал во время долгого `test.sh`. → Шаблон считает `max(300000, (tests_s + 120) × 1000)`. По коду ядра прогрессом считаются только кадры `assistant`, события жизненного цикла и прирост выходных токенов; эхо `tool_result` — нет, поэтому один долгий вызов Bash молчит целиком, и формула с запасом на ответ модели подходит. Хук `after_run` под сторож не попадает (по коду). Для проб с `tests_s` = 420 значение — 540000 (на пробе вручную стояло 1200000).
3. Флаг `budget_exhausted` хранится в `state.db` и штатно не снимается. → Правило «карточку в Archive, завести новую»; штатный сброс — открытый вопрос (ниже).
4. `project-render` и `project-doctor` пересоздают `WORKFLOW.md`. → Записано в README: ручные правки не живут.
5. Окружение агента отличается от окружения замера базы (переменные `SYMPHONY_*` службы; прямой `pytest` — 9 падений, `test.sh` — 0). → Агент гоняет только `test.sh` (`CLAUDE.md`, подсказка карточки); `summarize.py` принимает несколько отчётов (отдельный проход для тестов с таймингом); подсказки про `ulimit -n`, бедное окружение и `SYMPHONY_*` — комментариями в заготовках.
6. Автозапуск: добавлена команда `ours/bin/project-launchd <имя>` (пишет `launchd.plist`, ключ `[service] model` → `ANTHROPIC_MODEL`).

**Тесты K13.** `ours/tests` 198 → 244 passed (+46: права агента, формула сторожа, модель службы, `project-launchd`, несколько отчётов в `summarize.py`, строка сторожа в `project-doctor`); `ruff check src tests` и `ruff check ours` — чисто; `e2e/run.sh` — PASS. Основной набор не гонялся: `src/` не менялся. Существующие тесты не менялись.

**Не проверено.**
- Vitest и Jest (проект EVE) — только образцы отчётов.
- Автозапуск службы после перезагрузки Mac; поведение при закрытой крышке и сне.
- `--effort` у `claude` и модель на уровне задачи.
- Собственный предел времени одного вызова Bash у `claude` (формула сторожа его не учитывает).
- `project-launchd` и новая команда `claude` с `--allowedTools` проверены тестами и ядром (WORKFLOW.md принимается), но не запуском на Mac.

**Открытые вопросы.**
1. **SQLite в потоке (к ядру, не чинить).** Сообщение `run_registry_error` «SQLite objects created in a thread can only be used in that same thread», операция `budget_reopen_history`: 25 раз с 21:20 до 21:56 UTC 24.10 в службе, запущенной из Терминала; в окне запуска под launchd (22:12–03:18) не встречалась. Работе не мешала. Нужны данные: повторится ли, и связано ли с запуском из Терминала.
2. **Сброс `budget_exhausted`.** Нужно решение: штатный сброс (правка ядра) или постоянное правило «новая карточка». Пока действует правило «новая карточка».
3. **Запрет `ours/**` для проекта-форка.** Встроенный запрет в `check.py` остаётся, агент конвейера не может править сам комплект. Оставлять так или нет — решает Сергей.
4. **Модель и усилие на уровне задачи.** Не проверено: `--effort` у `claude`, выбор модели для отдельной карточки (сейчас модель одна на службу — `ANTHROPIC_MODEL`).

**Стоимость пробы.** Идёт на подписке Max, облачный кредит не тратит. По счётчику `cost_usd` в логе службы OMS-3 ≈ $0,11 (4 хода Haiku).

**Кредит:** K12, K13 и сессии по EVE вместе ≈ $27 (разделить нельзя). Остаток $32 на 25.10.

### 2026-10-24 — Итоги K10 и K11 (REPORT-13, REPORT-14)

Основа: `docs/our/reports/REPORT-13.md` (K10) и `REPORT-14.md` (K11). «Запуск» — проверено запуском, «по коду» — по чтению кода.

- **Ключ `workspace.code_repo`** (K10, правка ядра: 40 новых строк, логики ≈ 30) читают только пять маршрутов чтения веб-доски: список веток, лог, ветки задач, сравнение, diff. Без ключа всё как раньше (запуск).
- **Слияние, push и удаление ветки** ключ не читают: при внешней папке доски (не git) они отказывают, ветку сливает человек сам (запуск).
- **Корень git проверяется при каждой загрузке конфигурации** — на старте и при перечитывании. Путь должен быть абсолютным и быть корнем репозитория, а не подпапкой (запуск).
- **`code_repo` добавлен в шаблон** `WORKFLOW.md.tmpl` (значение `[project].repo`), у каждого проекта из `project-new` веб-доска смотрит в репозиторий кода. Сквозной прогон проверяет `GET /git/task-branches` (запуск).
- **Проба `project-doctor` идёт в урезанном окружении:** `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, `HOME`, `USER`, `LOGNAME`, `LANG`, `LC_ALL`, `TMPDIR`, `SYMPHONY_PYTHON`, `SYMPHONY_WORKFLOW_DIR`. Скрипты `setup.sh` и `test.sh` должны сами задавать инструменты полными путями — иначе ночью при запуске службы из launchd они упадут; при FAIL выводится подсказка. Заготовки в `project-new` описывают правило. Признак таймаута в `run_script` теперь определяется до `killpg` (запуск).
- **Не проверено:** Mac и launchd (`PATH` и `TMPDIR` службы там могут отличаться от пробы), настоящий `claude`. **Закрыто пробой на Mac (K13):** Mac, launchd и настоящий `claude`. Замерено `ps eww -p <pid>` у службы из plist: `PATH` как в plist, `HOME`, `USER`, `LOGNAME`, `SHELL`, `TMPDIR` выдаёт launchd, `LANG` и `LC_ALL` не заданы — проба `project-doctor` их задаёт, если они есть в терминале, так что она мягче службы; `setup.sh` и `test.sh` в окружении службы отработали.
- **Тесты:** `ours/tests` 194 → 198 (K11); основной набор не гонялся (`src/` в K11 не менялся), после K10 — 3886 / 136 / 2 (те же два падения окружения, REPORT-09); `ruff` — чисто.
- **Кредит:** K08b и K10 вместе ≈ $9, K11 ≈ $1. Остаток $68 на 24.10.

### 2026-10-24 — Итоги K08b (REPORT-12): project-doctor, строка итогов тестов, лимиты

Основа: `docs/our/reports/REPORT-12.md`. Ядро (`src/`) не менялось. «Запуск» — проверено запуском, «по коду» — по чтению кода.

- **Контракт `test.sh`.** В конце — строка `PIPELINE_TESTS passed=<n> skipped=<n> failed=<n>`, считается последняя. Печатает её `ours/hooks/summarize.py` по отчёту JUnit XML (Vitest `--reporter=junit`, pytest `--junitxml`, Jest через `jest-junit`); отчёт — во временный файл вне рабочей папки. `check.py` при коде 0 отказывает, если строки нет, `failed > 0`, `passed` меньше замера базы или `skipped` больше; нет замера — отказ «запусти project-doctor». Так ловится агент, который убрал тест из запуска через конфиг раннера, не трогая файл теста (запуск: сквозной прогон, сценарий `E2E_DROP_TEST`, два отката и `needs_human`).
- **`project-doctor`.** Проверяет `project.toml`, программы (`claude --version`), репозиторий и базовую ветку, что доска не внутри git, порт и префикс, что `setup.sh`/`test.sh` не заготовки (метка `OURS-STUB`). Гоняет их в пробной рабочей копии базовой ветки (без веток, всегда удаляется), пишет `.ours-state/baseline.json`, заполняет `[timeouts]` (правится только этот раздел; `fixed = true` — не трогать), пересобирает `WORKFLOW.md`, запускает `symphony doctor` (два WARN помечены как известные). Перезапускать, когда в базовой ветке менялись тесты, и при остановленной службе (иначе порт занят).
- **Таймауты.** Один ключ — `[timeouts] tests_s`; `[checks] test_timeout_s` убран (ошибка с подсказкой; отменяет решение K08a о двух ключах). Расчёт: `tests_s` = max(300, 2 × время тестов вверх до минуты), `gate_ms` = (`tests_s` + 60) × 1000, `hooks_ms` = (max(1,5 × время `setup.sh` вверх до секунды, `tests_s`) + 60) × 1000.
- **Лимиты `[limits]` → `agent.*`:** `max_turns` 30 (ходов за запуск воркера; исчерпан — `needs_human`), `max_total_turns` 60 (по всем запускам; откат `check.py` счёт не сбрасывает; исчерпан — `needs_human`), `max_attempts` 3 (возвратов назад по активным этапам, включая провал контракта; четвёртый — `Blocked`; откаты `check.py` не входят). Раньше действовали умолчания ядра 100 / 200 / 3. Счётчики ядра в памяти: перезапуск службы обнуляет (по коду). Для ночного контроля важны, но не добавлены: `agent.max_total_tokens` (потолок токенов в состоянии, по умолчанию выключен), `max_retries`, `max_state_turns`.
- **Не сделано.** `workspace.code_repo` (K10) в `our-main` ещё нет — в шаблон не добавлен. Дословная запись «Итоги K08a и K09» из задания K08b не вставлена: её содержание уже в двух записях ниже (PR #13, влит после написания задания), а цифры в ней устарели (остаток $79, `ours/tests` 142). Пункты ROADMAP «Правка №4» и «Хук after_run» уже были отмечены; «Рабочая конфигурация» отмечена сейчас.
- **Не проверено.** Настоящий `claude`, Mac, Vitest и Jest запуском (только образцы), предел 3600 с в `project-doctor`. **Закрыто пробой на Mac (K13):** настоящий `claude` и Mac; `project-doctor` на Mac сначала дал FAIL (лимит файлов, предположение), после `ulimit -n 10240` PASS с замером `tests_s` = 420. Vitest, Jest и предел 3600 с остаются не проверены.
- **Тесты:** основной набор 3878 / 136 / 2 (без изменений); `ours/tests` 142 → 194; `ruff`, `symphony-pyright` — чисто.
- **Кредит:** K08b и K10 вместе ≈ $9. Остаток $69 на 24.10.

### 2026-10-24 — Итоги K08a (REPORT-11): комплект подключения проектов

Основа: `docs/our/reports/REPORT-11.md` (PR #12). Ядро (`src/`) не менялось. «Запуск» — проверено запуском, «по коду» — по чтению кода.

- **Что сделано.** Комплект в `ours/`: `bin/project-new` (создаёт папку доски проекта) и `bin/project-render` (пересобирает `WORKFLOW.md` с печатью diff); `hooks/project_config.py` (разбор `project.toml`: неизвестный раздел или ключ — ошибка); `hooks/worktree.py` (создание и уборка рабочей копии); шаблон `templates/WORKFLOW.md.tmpl` (все решения этого журнала в одном месте, штатный блок `after_run` скопирован без изменений, `check.py hook` стоит в нём первой строкой); копия промптов `ours/prompts/` с 11 правками в трёх файлах (`CHANGES.md`); `check.py` переведён на `project.toml` и `test.sh` (`gate.toml` убран). Объём — ≈730 строк кода плюс шаблон 224 строки.
- **Проверено запуском (сквозной прогон на настоящей службе).** `ours/tests/e2e/run.sh`: настоящая служба oh-my-symphony, контракты этапов включены (`stage_contracts: auto`), вместо `claude` — поддельный (`e2e/fake_claude.py`, печатает кадры `stream-json` и за ход делает работу одного этапа; штатный мок `symphony.mock_codex` карточки не двигает). Четыре сценария:
  - **Обычный.** Карточка `KIT-1` за 4 хода проходит Todo → In Progress → Verify → Document и приходит в Human Review (≈4 с). Контракты ядра пройдены, `check.py hook` пишет `hook pass`. Рабочую копию создал наш хук `after_create` (`setup.sh` отработал); артефакты лежат в `artifacts/KIT-1/{work,qa}` и `wiki/` папки доски.
  - **Приёмка.** «Confirm done» (`POST /api/v1/KIT-1/confirm-done`) → шлюз `check.py gate` → `done_gate_passed` → Done; `before_remove` убрал рабочую копию, ветка `symphony/KIT-1` осталась. **Репозиторий кода остался чистым:** `git status` пуст, в ветке только `src/feature.py` и `tests/test_feature.py`, без `docs/KIT-1` и `docs/llm-wiki`.
  - **Отрицательный 1: агент ослабляет существующий тест.** Откат в In Progress с `## Check Failure`, второй такой же откат, на третьем провале — `needs_human`.
  - **Отрицательный 2: агент не пишет артефакты `docs/<ID>/work`.** Ядро 4 раза пишет `## Contract Failure` (контракт видит артефакты через ссылку на папку доски), затем карточка уходит в `Blocked` по `max_attempts`.
  - **Остальное запуском:** 35 новых тестов (`ours/tests`: 107 → 142) на временных git-репозиториях — чистый старт, переименование старой ветки, режим «обновить», ловушка «папка доски внутри git», чужой префикс, откат при сбое `setup.sh`, 12 отказов `project-new`, `project-render`, 12 случаев плохого конфига `check.py`; `symphony doctor` на собранном `WORKFLOW.md` — PASS, кроме двух ожидаемых WARN. Тесты репозитория: 3878 прошли, 136 пропущены, 2 упали из-за окружения (те же два); `ruff` и `symphony-pyright` без замечаний.
- **По коду (запуском не проверялось).** Ядро не проверяет слияние на переходе Verify → Document (`contracts.py:151, 380`; ворота слияния — только на Done и только при `auto_merge_on_done`, `blocked_recovery.py:565`). Абсолютный путь к `claude` отключает автоматический `--add-dir` для git-папок (`claude_code.py:48-66`), отсюда WARN `agent git grant` в `doctor`. `prompt.py:508`: целевую ветку ядро в промпт не передаёт. Ядро не вызывает `after_create` повторно на живой папке — режим «обновить» страховочный.
- **Решения.**
  - Открытый вопрос K08 закрыт: артефакты агента (`docs/<ID>/work|qa`, wiki) в рабочей копии — ссылки в папку доски, исключённые из git (три значения `symphony.autocommitExclude`). В ветку кода они не попадают.
  - Два ключа таймаута тестов: `[checks] test_timeout_s`, если задан, важнее `[timeouts] tests_s` (умолчание 900). Шлюз ждёт занятую блокировку не дольше `done_gate_timeout_ms/1000 − таймаут тестов − 30 с`; если человек нажмёт Confirm done, пока идёт хук, шлюз откажет и карточка уйдёт в `Blocked` с понятной причиной.
  - При чистом старте старые артефакты `artifacts/<ID>` переносятся в `artifacts/.attempts/<ID>-<UTC>` (иначе старые `work/` прошли бы контракт новой попытки). `setup.sh` ограничен `hooks_ms − 30 с` и убивается группой процессов, чтобы откат успел до таймаута ядра. Из `before_run` убран `git fetch origin` (база локальная).
  - Verify и слияние: ядро не проверяет слияние на переходе Verify → Document (по коду); штатный `verify.md:21,26` слияние запрещает, сливает оркестратор при Done и только при `auto_merge_on_done: true`. Наша копия промптов добавляет явный запрет и строку «отложено до человека»; защита `check.py` от слияния, сделанного кем угодно, — база из записи (тест `test_base_from_record_survives_merge_into_target`). Это уточняет п. 1 записи K05.
  - Целевую ветку для `git merge-tree` в Verify промпт берёт из `git config symphony.mergetargetbranch` в рабочей копии: ядро её в промпт не передаёт, а штатный текст велит запускать команду «из host repo», у нас это папка доски без git.
- **Находки в ядре (не правили).** `symphony board init` всегда кладёт демо-карточку `DEMO-001` в Todo (`cli/board.py:118`) — `project-new` её сразу удаляет (запуск). Абсолютный путь к `claude` отключает автоматический `--add-dir` для git-папок (`claude_code.py:48-66`), `doctor` пишет WARN `agent git grant`: файловые инструменты агента не пишут в `.git` кода, ему это и не нужно (коммитит хук). `.git` в `--add-dir` не добавляли сознательно: это дало бы агенту правку `.git/config` и git-хуков.
- **Не проверено.** Настоящий `claude` (только поддельный) и Mac, в том числе `fcntl.flock` на macOS. Работают ли из Bash-инструмента `claude` записи в `.git` (нужны для `git merge-tree --write-tree` в Verify) — проверить первой пробой на Mac. Режим «обновить» через настоящее ядро (ядро не вызывает `after_create` повторно на живой папке; режим проверен только тестами, как страховка). Таймаут `setup.sh` и смерть хука по `hooks.timeout_ms` во время `setup.sh`. Две карточки одновременно (`max_concurrent_agents: 1`). Ключи `max_turns`, `max_attempts`, `reuse_policy`, архивация оставлены по умолчанию ядра. **Закрыто пробой на Mac (K13):** настоящий `claude`; Mac; `fcntl.flock` и `killpg` на macOS работают; запись в `.git` из Bash агента для `git merge-tree --write-tree` работает (разрешение `Bash(git merge-tree:*)` и доступ к `.git`, который служба выдаёт сама, `claude_git_roots_granted`). Остальное (режим «обновить» через ядро, таймаут `setup.sh`, две карточки одновременно, ключи по умолчанию) не проверялось.
- **Открытые вопросы.** K10: вернуть ветки, diff и лог в веб-доску при внешней папке доски (≈15 строк ядра). Нужна ли в рабочей конфигурации видимость `needs_human` и `Blocked` для утреннего разбора — в K08a не рассматривалось.
- **Кредит:** K08a ≈ $10 (Opus). Остаток $79 на 24.10.

### 2026-10-24 — Итоги K09 (REPORT-10): долгий `after_run` и `preserve_workspace_states`

Основа: `docs/our/reports/REPORT-10.md` (PR #11). Правка ядра, ≈30 строк плюс ключ конфигурации и 18 новых тестов. «Сверка» (reconcile) — проверка ядра на каждом такте опроса: что стало с карточками, над которыми работают воркеры.

- **Проблема (воспроизведена запуском).** Пока долгий `after_run` (наш `check.py hook` гоняет тесты 4–5 минут) ещё идёт, сверка считала воркера лишним. Если карточка терминальна дольше 60 с — воркер отменялся, папка Human Review удалялась посреди хука, а откат хука в In Progress записывался, когда воркера уже не было: карточка в In Progress без воркера, второго хода нет. Если нет прогресса дольше `stall_timeout_ms` (по умолчанию 300 с) — воркер отменялся проверкой зависания, вердикт хука терялся, а второй `after_run` стартовал параллельно с первым.
- **Что сделано.** (1) Поле `RunningEntry.after_run_started_at`: ставится перед каждым `after_run` через один помощник `_after_run_hook`, сбрасывается при следующем ходе; сверка не трогает такого воркера ни в ветке «карточка терминальна», ни в проверке зависания (в журнале — `reconcile_skip_after_run`). Верхний предел — `hooks.timeout_ms` + 60 с (`AFTER_RUN_RECONCILE_GRACE_S` в `constants.py`); при нашем правиле таймаутов это ≈17–18 минут. Если воркер застрял сам, сверка действует как раньше. (2) Ключ `agent.preserve_workspace_states` — список статусов, чьи папки не удаляются (нет ключа — поведение прежнее; значения хранятся в нижнем регистре; строка вместо списка, пустая строка, число — ошибка конфигурации; `Done` в списке — тоже ошибка, у него свои правила уборки: шлюз, слияние, `after_done`). Зашитые `blocked` и `human review` объединяются с ключом. В шаблоне K08a: `preserve_workspace_states: ["Human Review"]`.
- **Проверено запуском.** Настоящий оркестратор, доска из файлов, настоящий `WorkspaceManager` и хук `bash`, подменён только бэкенд агента: до правки воспроизведены обе ветки сбоя; после правки хук доходит до конца, папка цела, откат применён, воркер делает второй ход в In Progress и снова приводит карточку в Human Review. Тест предела: отметка старше `timeout_ms` + 60 с — воркер отменён, папка удалена (старое поведение). `confirm_done` из сохранённого Human Review — папка удалена после Done; переход в Archive — удалена на следующем старте. Тесты: 3860 → 3878 прошли, 136 пропущены, 2 упали из-за окружения; `ruff` и `symphony-pyright` без замечаний.
- **По коду.** На старте для сохранённых статусов снимок в ветку не делается (папка остаётся, снимать нечего). Пока хук идёт, Done-карточку обрабатывает сам воркер после хука, а не сверка: шлюз Done и снимок теперь не работают параллельно с тестами хука.
- **Не проверено.** Реальный хук на 4–5 минут на Mac — проверено только тестом с подменой времени. **Закрыто пробой на Mac (K13):** реальный хук `check.py hook` на 3 мин 52 с (OMS-2) и 3 мин 35 с (OMS-3, под launchd), воркер не отменён, шлюз `check.py gate` прошёл.
- **Открытые вопросы.** Второй `after_run` при отмене посреди хука: поток с `bash` после отмены продолжает работать, флаг `after_run_pending` остаётся, и аварийная ветка запускает хук ещё раз (`attempt.py:1221`) — два прогона тестов одновременно. После правки так бывает только при отмене после предела или по другой причине (пауза оператора, остановка службы). Не чинили: вне рамок, нужна отдельная правка.
- **Кредит:** K09 ≈ $5 (оценка по разнице остатков $94 → $89, прямой записи нет). Остаток $89 на 24.10.

### 2026-10-24 — Итоги K05–K07, схема подключения проектов, параметры EVE

- **Единая схема подключения проектов (решение, основа — REPORT-09).** Каждый проект — папка доски вне репозитория кода (`~/symphony-projects/<имя>/`): `project.toml` (путь к репозиторию, базовая ветка, префикс ID, правила проверок), `setup.sh` (подготовка свежей рабочей копии), `test.sh` (полный прогон, код 0 = зелено, в конце строка итогов единого формата), `WORKFLOW.md` из общего шаблона (руками не правится), `kanban/`. Последовательность: `project-new` → заполнить `setup.sh`, `test.sh`, правила → `project-doctor` (база зелёная, замер времени, таймауты) → запуск службы. Запасной вариант `.pipeline/` внутри репозитория не используем: единообразие и правила работодателя.
- **Что теряем при внешней папке:** ветки, diff и лог в веб-доске (вернуть — ключ ядра для веб-доски, ≈15 строк, задача K10); WARN в `doctor`; автослияние и «хостовое спасение» (у нас выключены). В шаблоне: `wiki.sweep_every_n: 0`, свои `server.port`, `workspace.root` и префикс ID на проект (одинаковые ID в двух проектах над одним репозиторием дают одну ветку).
- **Свои хуки рабочей копии** (REPORT-09, Q5; на Python, без `flock`): worktree `symphony/<ID>` от явной базы; существующая ветка переименовывается в `attempts/<ID>-<UTC>` (чистый старт); `core.excludesFile` на рабочую копию вместо `.git/info/exclude`; базовый sha пишется в папку состояния доски, вне досягаемости агента; проверка, что папка доски не лежит внутри другого git-репозитория.
- **Открытый вопрос (K08):** артефакты агента (`docs/<ID>/work`, `docs/<ID>/qa`, wiki) коммитятся в ветку кода. Предложение: в рабочей копии это ссылки в папку доски, исключённые из git, как `kanban`.
- **K05 (REPORT-07): `check.py` готов, не подключён.** До подключения закрыть: (1) база через `merge-base` с подвижной веткой — штатный Verify вливает ветку в целевую, после чего проверки путей и тестов слепнут → база из записи при создании рабочей копии, отказ, если она не предок HEAD; Verify в наших промптах не вливает; (2) обход через настройки запуска тестов (`package.json`, конфиг раннера, новый `conftest.py`) → `test.sh` вызывает раннер напрямую, файлы запуска тестов в запрещённых путях проекта, строка итогов `PIPELINE_TESTS passed=… skipped=… failed=…` сравнивается с базой (прошедших не меньше, пропущенных не больше); (3) долгий хук и сверка ядра → K09. В `after_run` хук пишется как `check.py hook || true`.
- **K07 (REPORT-08): стоимость запуска** пишется в `runs.cost_usd`, отдаётся в `GET /api/v1/runs` и в событиях `turn_completed`/`run_completed`. `total_cost_usd` у claude накопительный за сессию (проверено на Mac тремя ходами): ход = значение − прошлое значение той же сессии; новая сессия или сброс — значение целиком; нет годного значения — `NULL`. Это цена по прейскуранту (`costBasis: list`): на подписке условная, для бюджета подписки — проценты окон (K04). Продолжение после рестарта считается от сохранённого значения; без него первый ход завышен (`cost_baseline_missing`).
- **Параметры EVE (первый проект):** тесты — Vitest через `npm test`; `node_modules` и `.sites-runtime` не в git; Node 24.22.1 задаётся явно в скриптах проекта; установка `npm run install:ci`; полный прогон ≈259 с (3419 прошли, 20 пропущены); `test_timeout_s = 900`; `tests/helpers/` под защитой `tests/**`.
- **Правило таймаутов:** внутренний меньше внешнего. `done_gate_timeout_ms` = `test_timeout_s` + 60 с; `hooks.timeout_ms` ≥ max(время установки, `test_timeout_s`) + 60 с.
- **Базовый уровень тестов после K07:** 3860 прошли, 136 пропущены, 2 упали из окружения; `ours/tests` — 107 (отдельной командой `python -m pytest ours/tests -q`).
- **Кредит:** K05, K06, K07 вместе ≈ $27 (Opus). Остаток $94 на 24.10.

### 2026-10-24 — Итоги K04 (REPORT-06)

- **Правка №7 сделана:** кадр `rate_limit_event` целиком кладётся в `_latest_rate_limits` (`claude_code.py:153`), 11 тестов с фикстурой реального кадра. Поле одно на все бэкенды (правка №13 не делалась); шкала `utilization` не разобрана; истории нет (внешний сборщик).
- **Стоимость запуска не реализована:** ≈40–50 строк в 8 файлах, миграция не нужна (`runs.cost_usd` есть в новых и старых базах); маршрут в REPORT-06, часть 2. Не проверено: `total_cost_usd` при `--resume` — за ход или за сессию. Проверить тремя ходами на Mac, потом реализовать (K07).
- **Промпты:** `Done` велят ставить `stages/document.md:16,19`, файлы `deep/*` и `base.md:63,69`; `base.md:64` запрещает Human Review как обычный финал. Решение: копия промптов в `ours/prompts/` с тремя правками (`document.md:16,19`, `base.md:64`), `WORKFLOW.md` ссылается на копию (K06).
- **Контракты этапов при завершении в Human Review не проверяются** (`attempt.py:1058-1084`); теряется проверка Document (`## Wiki Updates`, итоговый отчёт). Решение: проверка формы в хуке `after_run` вместе со скриптом проверок (K05). Правка ядра на 4–6 строк — только если на пробе агент часто выходит в Human Review без разделов.
- **Базовый уровень тестов после K04:** 3837 прошли, 136 пропущены, 2 упали из-за окружения. Тесты `test_cli_pm::test_subprocess_serves_language…` падают при параллельной нагрузке (pyright одновременно с pytest): не запускать вместе.
- **Кредит:** K04 ≈ $1. Остаток $121 на 24.10.

### 2026-10-24 — Итоги K03 (REPORT-05)

- **`needs_human` — невыполненная зависимость** (`core_support.py:38`, 8 тестов): карточки с `blocked_by` на задачу в `needs_human` ждут. Статус писать точно `needs_human`: другие написания («Needs Human») зашитые списки не распознают. Побочно: карточка-лечение в `needs_human` больше не считается решённой (`blocked_recovery.py:1156`).
- **Текст отказа `confirm_done`:** при отказе шлюза Done человек видит `done gate failed (<причина>)`; при отказе ворот слияния текст прежний.
- **`confirm_done` без рабочей папки** отказывает до шлюза (`api_views.py:86`), карточка остаётся в Human Review, не в Blocked (проверено запуском). Пересоздание папки из ветки `symphony/<ID>` не делаем (15–25 строк плюс 60–80 строк тестов, нужна проверка существования ветки, иначе шлюз пропустит пустое дерево). Папка Human Review пропадает: при сверке с живым воркером после 60 с, при автоархивации, при ручном удалении. Решение: смотреть на частоту на пробе; в утренний отчёт добавить «карточки в Human Review без рабочей папки».
- **Остановка службы во время шлюза** оставляет Done без вердикта, после перезапуска шлюз не запускается (проверено запуском). Закрыто правилом «Done ставит только `confirm_done`» (на этом пути Done не пишется) и утренней сверкой «Done без `done_gate_passed`». Кодом не чиним.
- **Открытый вопрос (K04):** контракты этапов проверяют форму только при переходе в Done; если агент заканчивает в Human Review, они, возможно, не срабатывают, а промпты линии Document могут велеть ставить Done.
  Результат K04, часть 2: стоимость запуска не реализована — нужно ≈40–50 строк в 8 файлах (миграция не нужна), порог задачи ≈30 строк в ≤4 файлах; маршрут описан в REPORT-06.
- **Базовый уровень тестов после K03:** 3826 прошли, 136 пропущены, 2 упали из-за окружения (те же два); `test_chat::test_failed_mode_rebuild_moves_session_to_explicit_resume` нестабилен при `-n 4`.
- **Кредит:** K03 ≈ $2 (Sonnet). Остаток $122 на 24.10.

### 2026-10-24 — Итоги правок ядра K01 и K02

Основа: `docs/our/reports/REPORT-03.md` (PR #4) и `docs/our/reports/REPORT-04.md` (PR #5).

- **K01: выключатель автопереноса.** Ключ `agent.normalize_legacy_human_review` (по умолчанию `true`, строгий разбор: строка `"false"` или `0` — ошибка конфигурации). В рабочих конфигурациях обязателен `false`: автоперенос Human Review → Done идёт мимо шлюза (проверено запуском).
- **`auto_recover_blocked` читается мягким `bool()`:** строка `"false"` даёт `True`. В `WORKFLOW.md` писать `false` без кавычек. Строгий разбор этого ключа — мелочь на потом.
- **K02: шлюз приёмки Done.** Ключи `agent.done_gate_command` (пусто = выключен) и `agent.done_gate_timeout_ms` (по умолчанию 600000). Команда запускается через `bash -lc` в рабочей папке карточки, в своей группе процессов; при таймауте убивается вся группа. Пропуск — только код 0; любой другой исход (код ≠ 0, 127, таймаут, исключение, нет рабочей папки) → `Blocked` и заметка `## Done Gate Failure` с хвостом вывода. События журнала `done_gate_passed` / `done_gate_failed`, полный вывод — в `.symphony-workspace-hook-output/<ID>/*-done_gate.*`. Шлюз стоит первым в `_auto_merge_done_gate_or_block`, независимо от `auto_merge_on_done`. Тесты: 31 в `tests/test_done_gate.py`, с настоящими подпроцессами.
- **Окно Done → Blocked (открытый вопрос закрыт).** На путях «воркер закончил» и «сверка» карточка лежит на диске в Done, пока работает шлюз; зависимые карточки при этом не запускаются (проверено запуском), Done видят только внешние читатели (веб-доска, `board ls`). На пути `confirm_done` Done не пишется вообще. Пока шлюз работает на пути сверки, новые карточки не раздаются (по коду), поэтому `done_gate_timeout_ms` задавать явно и не больше реально нужного.
- **Карточка сразу в Done (открытый вопрос закрыт).** `board new --state Done` и веб-API создают её без шлюза (проверено запуском). Закрываем правилом: скрипты конвейера так не делают.
- **Пути Done мимо шлюза** (REPORT-03, Q1; REPORT-04, Q3). Маршрут приёмки PM нами не используется. Остальные пять закрываем конфигом: `Human Review` первым в `terminal_states`; `budget_exhausted_state: needs_human` точно (значение не проверяется); `no_stage_change_action` не ведёт в Done.
- **Правила для скрипта шлюза:** полные пути к `python` и `symphony`; `SYMPHONY_ISSUE_TITLE` — данные от агента (только в кавычках, без `eval`); не оставлять фоновых процессов, держащих stdout/stderr (иначе успешная проверка превратится в таймаут и отказ).
- **Остановка службы во время шлюза** (по коду, уточняется в K03): команда дорабатывает в потоке, карточка, по-видимому, остаётся в Done без вердикта. При правиле «агент заканчивает в Human Review, Done ставит только `confirm_done`» сюда не попасть; утренний отчёт может отмечать Done-карточки без события `done_gate_passed`.
- **Находки, переданные в K03:** `needs_human` считается выполненной зависимостью (`core_support.py:30-38`, проверено запуском) → правка ядра; текст отказа в `confirm_done` («merge gate failed») вводит в заблуждение → передавать `debug.last_error`.
- **Базовый уровень тестов после K02:** 3818 прошли, 136 пропущены, 2 упали из-за окружения (те же два).
- **Облачный кредит:** K01 ≈ $1, K02 ≈ $6 (Opus). Остаток $124 на 24.10. В облако переносятся также правка №7 (K04) и скрипт механических проверок с тестами (K05); на Mac остаются конфигурация и проба.

### 2026-10-24 — Решения по REPORT-02: Done-шлюз

Основа: `docs/our/reports/REPORT-02.md`. «Шлюз» здесь — внешняя команда, которая запускается при приёмке Done и может не пустить задачу в Done.

- **Где ставим `agent.done_gate_command`.** В функцию `_auto_merge_done_gate_or_block` (`blocked_recovery.py:467`), **до** раннего выхода при `auto_merge_on_done: false` (у нас автослияние выключено, иначе шлюз не сработал бы никогда). Размещение рядом с `evaluate_contract` (как предлагал REPORT-01) отклонено: контракты этапов не вызываются вне хода агента, у этапа `todo` контракта нет, поэтому условие (2) варианта Б не выполняется. Пути приёмки, которые шлюз покроет: воркер закончил, `reconcile` (сверка) увидел Done, `confirm_done` («Confirm done» в TUI, веб, API).
- **Объём.** Оценка выросла с 40–70 до ≈100 строк в 4 файлах + ≈150–200 строк тестов (`tests/test_done_gate.py`, 9 сценариев из REPORT-02, раздел 5.6).
- **Отказ шлюза → статус `Blocked`**, как у ворот слияния. Ключ `agent.done_gate_failure_state` не добавляем. `auto_recover_blocked: false` обязателен во всех рабочих конфигурациях (иначе `Blocked` породит FIX-карточку и расход лимитов).
- **Вход скрипта.** Рабочая папка (текущая папка), переменные `SYMPHONY_ISSUE_ID`, `SYMPHONY_WORKSPACE`, `SYMPHONY_BRANCH`, `SYMPHONY_ISSUE_STATE` и обычные хуковые переменные. Команда берётся только из `WORKFLOW.md`, не из карточки.
- **Закрытый режим** (при сомнении — не пускать): код возврата ≠ 0, таймаут, команда не найдена, любое исключение при запуске = отказ. Пустая команда = шлюз выключен (так не ломаются существующие настройки).
- **Вторая правка ядра: выключатель автопереноса Human Review → Done.** Функция `_auto_normalize_legacy_human_review_done` (`blocked_recovery.py:520-576`, вызов `dispatch.py:141`) на каждом такте опроса сама ставит Done без всяких проверок, если в карточке есть раздел «## As-Is -> To-Be Report». Нужен ключ, например `agent.normalize_legacy_human_review: false` (≈6 строк + 1–2 теста). Без него решение «финал — Human Review, Done подтверждает человек» не работает.
- **Сознательно не закрываем сейчас — закрываем правилами:** `board mv`, правку статуса в веб-доске, «Done без воркера», в том числе правку агентом файла чужой карточки через общую папку `kanban`. Вариант C (проверять все Done-карточки на каждом такте, ≈+60 строк) отложен до случая, когда на реальных прогонах увидим Done без проверки. Правила: Done ставит только `confirm_done`; `board mv` принимает **любое** имя статуса (проверено запуском в REPORT-02), поэтому скрипты конвейера сами проверяют имена статусов.
- **Что умеют и не умеют контракты этапов.** Проверяют только форму (наличие разделов и папок), не работу кода. Переход Todo→Done их обходит. Ядро сравнивает статус только в начале и в конце хода: если за один ход карточка прошла Todo→In Progress→Done, для ядра это Todo→Done.
- **След от упавшего `after_run`.** Остаётся в журнале службы (события `hook_failed`, `hook_after_run_failed`) и в файлах `.symphony-workspace-hook-output/<ключ>/*-after_run.json` (поле `returncode`). В карточке и в API следа нет. Утренний отчёт может собирать эти файлы.
- **Поправки к прежним записям.** Строку «delivery: Done alone does not confirm target merge or workspace cleanup.» печатает команда `symphony board show`, в файл карточки она не пишется. Раздел 4.4 REPORT-01 про удаление папок уточнён в REPORT-02 (раздел 6.1).
- **Открытые вопросы:** окно между «Done выставлен» и «Done отменён» при отказе шлюза (успеют ли запуститься зависимые карточки); можно ли создать карточку сразу в Done через веб-API; настройка сохранения папки `needs_human` (`agent.preserve_workspace_states`, ≈12 строк) — пока не нужна; в TUI нет колонок для `Blocked` и `needs_human` (проверено на Mac: карточки в этих статусах пропадают с экрана, видны только в `symphony board ls`). Как показывать такие задачи при утреннем разборе: колонка через настройки доски, веб-доска или скрипт поверх `board ls`; выяснить на этапе 3.

### 2026-10-24 — Итоги локальной пробы (этап 2, часть 2)

Пометка у каждого пункта: «проверено запуском на Mac» (проба на твоём компьютере) или «по коду (REPORT-02)» (чтение кода, не запускалось).

- **а) `needs_human` в `terminal_states`** (порядок `[Human Review, needs_human, Done, Blocked, Archive]`) — проверено запуском на Mac: `symphony doctor` по-прежнему пишет «agent.stage_contracts: auto — contracts enforced». Ядро контракты не отключает. Находка 5 из записи 23.10 закрыта.
- **б) Исчерпание бюджета ходов** — проверено запуском на Mac. Ядро переносит карточку в статус из `agent.budget_exhausted_state` (по умолчанию `Blocked`); проверены оба значения. При `Blocked` ядро само заводит карточку `FIX-<ID>-1` (метки `blocked-fix`, `source-<id>`); у FIX-карточки второго уровня нет. При `needs_human` FIX не создаётся.
- **в) Выключатель автолечения** `agent.auto_recover_blocked: false` (по умолчанию `true`, `workflow/config.py:190`) — проверено запуском на Mac: карточка уходит в `Blocked`, FIX не создаётся. Побочный эффект (по коду): в той же функции отключается и «хостовое спасение» истории git. Находка 4 закрыта. **Рабочий конфиг: `budget_exhausted_state: needs_human` + `auto_recover_blocked: false`.**
- **г) Какой терминальный статус ядро выбирает само** — по коду (REPORT-02, раздел 1.6). «К человеку» → первый по списку статус с `human`/`review`/`block` в имени; при нашем порядке это `Human Review`. Сбои бюджета повторов и истории git → `Blocked`. `needs_human` ядро само не выбирает, его ставит только наш скрипт. Отсюда смысл статусов: **`Blocked` = сбой процесса, `needs_human` = лестница пройдена, есть диагностика.** Порядок в `terminal_states` менять нельзя: если поставить `needs_human` раньше `Human Review`, «к человеку» начнёт выбирать его.
- **д) База ветки в `after_create`** (`scripts/symphony-setup-worktree.sh`) — При пустой `SYMPHONY_FEATURE_BASE_BRANCH` берётся текущая ветка главного репозитория (проверено на Mac: `symphony.basebranch=main` у TASK-C). Если ветка `symphony/<ID>` уже есть, используется она: только по коду, запуском не проверено. **Решение:** в рабочем `WORKFLOW.md` задавать `feature_base_branch` явно. Правка 4 из REPORT-01 (внешний скрипт чистого старта) подтверждена как нужная.
- **е) Откат Done из `after_run` работает** — проверено запуском на Mac. Хук, вставленный **первой строкой** блока `after_run`, читает `state` из файла карточки (`<workflow>/kanban/<ID>.md`) и возвращает карточку командой `symphony board mv <ID> "In Progress" --root <kanban>`; оркестратор не закрыл задачу посреди хода. Откат Done хуком работает (проверено на Mac). То, что он не тратит `max_attempts`, — по коду REPORT-01, запуском не проверено; в пробе прогон остановил лимит `max_turns`. Счётчик неудач хук ведёт сам. Штатный `after_run` имеет ранний выход при отсутствии изменений, поэтому наш вызов обязан стоять первой строкой.
- **ж) Сломанный хук пропускает Done** — проверено запуском на Mac. При коде возврата 1 карточка, переведённая Todo→Done, осталась в Done (ядро ошибку `after_run` проглатывает; по коду — REPORT-02, раздел 4). Значит, нужен второй рубеж в ядре — см. запись «Решения по REPORT-02».
- **з) Рабочая папка** — запуском на Mac: при Done удаляется, ветка `symphony/<ID>` остаётся; для `needs_human` на старте службы папка удаляется. По коду (REPORT-02, раздел 6.1): на старте сохраняются только `blocked` и `human review` (зашито в `retry_reconcile.py:1192`); при сверке (`reconcile`) удаляется любой не-Done терминальный статус; если воркер закончил сам, папка не-Done статуса остаётся. **Диагностику хранить вне рабочей папки:** в карточке, в ветке `symphony/<ID>`, в `.symphony-workspace-hook-output/`, в `.symphony/artifacts/<ID>/`.
- **и) Формат лимитов `claude`** — проверено запуском на Mac (`claude` установлен нативным установщиком, вход через подписку, ключ API не задан). В потоке `stream-json` есть кадр `rate_limit_event` → `rate_limit_info` с полями `status` (в пробе `allowed`), `resetsAt`, `rateLimitType`, `overageStatus`, `overageDisabledReason` (`org_level_disabled`), `isUsingOverage`, `unifiedWindows.five_hour` и `unifiedWindows.seven_day`; у каждого окна есть `utilization` и `resetsAt`. Итоговый кадр `result` содержит `total_cost_usd` (в пробе 0.0023), `usage`, `modelUsage`; ключ `--model haiku` принимается. Условие правки №7 из REPORT-01 выполнено. **Не подтверждено:** шкала `utilization` (в пробе 0.15 и 0.04 — доля или проценты; сверить со страницей Usage в аккаунте); другие значения `status` (неизвестное значение считать «стоп»); приходит ли кадр на каждый запуск.
- **к) Окружение хуков** — проверено запуском на Mac. В `bash -lc` команда `python` — это 2.7, поэтому в `WORKFLOW.md` и в хуках нужны **полные пути** к `python`, `symphony`, `claude` (`claude` лежит в `~/.local/bin`, из `bash -lc` виден). Пункт добавить в чеклист конфигурации.
- **л) Базовый уровень тестов `our-main`** (коммит 555d19c) — проверено запуском (REPORT-02): 3780 прошли, 136 пропущены, 2 упали из-за окружения: `test_pm_planning.py::test_stop_kills_term_resistant_group_after_leader_exits` и `test_pm_ui_node.py::test_pm_ui_node_regressions`. Новые падения сравнивать с этим уровнем.

### 2026-10-23 — Итоги этапа 1 и первой части этапа 2 (локальная проба)

Проверено запуском (форк `our-main`, коммит d066c95, oh-my-symphony 0.28.0, Python 3.14.5, Mac mini):

- Установка по инструкции работает; `symphony doctor` на демо — все PASS. Демо создаётся командой `symphony project create "<имя>" --path ../symphony-demo` вне репозитория.
- Встроенный мок-бэкенд (`python -m symphony.mock_codex`) имитирует ходы и токены, но карточки не двигает. Карточка под моком ставится на паузу в TUI, снимается кликом по ней и клавишей P.

Находки, которые влияют на конфигурацию:

1. **`PATH` в логин-оболочке.** Оркестратор запускает команды через `bash -lc`; там `python` оказался системным 2.7.18, мок падал (`port_exit`), а `doctor` этого не поймал. Правило: в `WORKFLOW.md` и в хуках `ours/` — только полные пути к интерпретатору и к `symphony` (или `$SYMPHONY_PYTHON`). Перед пробой `claude` проверить `bash -lc 'which claude'`.
2. **Пауза после серии аварий.** Задача, у которой процесс агента несколько раз упал, ставится на паузу «для осмотра оператором»; пауза переживает перезапуск.
3. **Бюджет ходов.** Если статус не меняется дольше `agent.max_turns` на попытку, ядро само переводит задачу в `Blocked` и пишет `## Budget Exceeded`. Это ещё один автоматический перенос; лестницу (этап 4) надо согласовать с ним.
4. **Автовосстановление.** Для `Blocked` ядро само создаёт задачу `FIX-<ID>-1` (метки `blocked-fix`, `source-...`) и отдаёт её агенту. С реальным `claude` это расход лимитов без нашего решения. Нужно выяснить, как это отключить и ограничено ли число таких задач. Пока не выяснено — реальных агентов на доске не запускаем.
5. `doctor` показал `agent.stage_contracts: auto — contracts enforced` на стандартных колонках; сохранится ли это после добавления `needs_human` в `terminal_states`, проверяем на второй части этапа.

### 2026-10-23 — Итоги разбора REPORT-01: Done только после внешней проверки (вариант Б)

- Задача не должна попадать в Done без успешной внешней проверки. Делаем вариант Б: хук `after_run` (проверки, откат Done, счётчик неудач) плюс небольшая правка ядра `agent.done_gate_command` (внешняя команда проверки при Done, ~40–70 строк и тесты).
- Вариант Б считается сделанным, если: (1) проверка закрыта по умолчанию: скрипт упал, завис, не найден или вернул ошибку — в Done задача не проходит; (2) проверка срабатывает в нашей конфигурации доски со статусом `needs_human`, а не только на стандартных колонках: до написания кода выяснить, работает ли проверка при выключенных контрактах этапов, и если нет — остановиться и описать варианты; (3) есть тесты на успех, провал, таймаут и отсутствие команды; (4) проверено на демо-доске (этап 2).
- Автослияние и пуш в `WORKFLOW.md` выключаем явно: в ядре они включены по умолчанию.
- Порядок правок (нумерация по REPORT-01): 2 → 1 → 6 → 3 → 4. Правки 7–14 (лимиты, стоимость, API бота) — после пробы на локальном компьютере. Правка 5 (уровень задачи в ядре для Codex) отложена, пока Codex дополнительный.

### 2026-10-23 — Основа: oh-my-symphony

Берём форк oh-my-symphony (ветка `our-main`), а не пишем диспетчер с нуля. Своё — снаружи (хуки, скрипты, бот поверх JSON API), в ядро — минимальные изолированные правки. Запасные варианты: Contrabass, собственный тонкий диспетчер.

### 2026-10-23 — Распределение ресурса

- Конвейер на локальном компьютере работает на API-кредите Max и на части лимита подписки.
- Пользователю всегда остаётся не меньше 10% недельного лимита подписки.
- Конвейер прекращает брать подписку примерно при 75–80% недельного лимита; в рабочие дни пользователя днём — только кредит.

### 2026-10-23 — Уровни задач и лестница

- Уровень модели назначается задаче при нарезке, **с запасом**: провал и повторы дороже одного уверенного прохода.
- В конфигурации B: L1 и L2 — Haiku (позже локальная LLM), S1 и S2 — Sonnet.
- Лестница: попытки на стартовой ступени → тренер (уточнение задания) → решение более сильной моделью с чистой ветки → `needs_human` с диагностикой.
- Механические проверки (тесты, запрещённые пути, неизменённые существующие тесты) важнее мнения модели.
- Финальное состояние — Human Review; автослияние выключено.

### 2026-10-23 — Управление

Веб-панель oh-my-symphony через VPN до дома; встроенные уведомления. Telegram-бот — по желанию, поверх JSON API.

### 2026-10-23 — Отклонено

- Gemini — доступ закрыт на уровне аккаунта.
- Вторая подписка под куратора.
- Агентные оркестраторы, где управление ведёт модель (Agent Orchestrator), и облачные агенты (OpenAI Dots).
