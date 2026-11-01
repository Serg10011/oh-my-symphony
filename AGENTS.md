# AGENTS.md: правила работы с форком Symphony

Форк `Serg10011/oh-my-symphony`, рабочая ветка `our-main`. Цель — ИИ-конвейер
разработки с механическими проверками, контролем ресурсов и приёмкой человеком.
Этот раздел — общие правила разработки форка; ниже сохранена инструкция
исходного проекта по управлению Symphony. При расхождении действует этот раздел.

## Как ведём проект

- Общайся с Сергеем по-русски, на «ты», понятно и по шагам. Перед значимым
  действием объясняй, что делаешь, зачем и какой результат ожидается. Один ответ —
  один законченный шаг; не расписывай следующие этапы заранее.
- Текущий чат Codex ведёт архитектуру и приёмку, усилие архитектора — `max`
  по решению Сергея от 01.11.2026. Приоритеты и крупные развилки согласовывай
  с ним. Порядок работы — [`docs/our/DEVELOPMENT.md`](docs/our/DEVELOPMENT.md).
- Архитектор сам предлагает новый чат-разработчик, границы связанного блока
  задач, конкретную модель и усилие. Сергей подтверждает и комментирует.
  Новый чат создавай после подтверждения этого предложения; разрешение вести
  переписку и архивировать чат должно входить в подтверждённое поручение.
  Один исполнитель может вести несколько небольших связанных задач.
- Для подходящих простых задач пробуем `gpt-6-luna` с усилием `max`.
  Если Luna не справляется, следующую попытку задачи передаём
  `gpt-6.1-sol` с усилием `medium`, сохраняя результат и причину неудачи.
  При повторяющихся подтверждённых ошибках Luna предложи Сергею перевести
  соответствующий класс задач на Sol Medium; автоматический порог не задан.
  Это правила чатов разработки, а не смена настроек агентов конвейера.
- Рутинные операции внутри согласованной задачи выполняй самостоятельно.
  На значимых развилках и при готовом результате дай Сергею точку для проверки;
  не переходи к следующей задаче без согласования.
- Если по ходу работы находишь следы прежних разработок Claude — заготовки,
  незавершённые ветки, скрипты, решения или результаты экспериментов — сообщай
  Сергею: что найдено, где находится, чем может быть полезно и что ещё неясно.
  Отделяй подтверждённые факты от предположений. Если назначение или история
  неясны, привлекай Сергея: он может вспомнить, что и почему делали.
  Не считай такие находки мусором и не удаляй их автоматически.
- Старые указания для ведущего чата Claude о запуске Claude Code, выборе моделей
  и режимов рассуждения к Codex не относятся. Не добавляй такие рекомендации
  в конец каждого ответа. Смена ведущего чата сама по себе не меняет бэкенды,
  модели, лимиты или оплату работающего конвейера.

## Источники правды и состояние

1. Сначала читай блок «Текущее состояние (обновляется каждой сессией)» в
   [`docs/our/DECISIONS.md`](docs/our/DECISIONS.md): дату, сделанное, работу и
   ближайший шаг. Решения в этом файле главнее `ARCHITECTURE.md`.
2. Затем — [`docs/our/ROADMAP.md`](docs/our/ROADMAP.md), относящиеся к задаче
   записи `DECISIONS.md`, задания в `docs/our/tasks/` и отчёты в
   `docs/our/reports/`. Ищи по кодам (`K14`, `REPORT-16`, `S4-29`, `E3c`).
3. Устройство наших инструментов — [`ours/README.md`](ours/README.md),
   спецификация — [`docs/our/ARCHITECTURE.md`](docs/our/ARCHITECTURE.md),
   исходный проект — [`README.md`](README.md). Инструкция очереди Claude Code —
   [`CLAUDE.md`](CLAUDE.md); она нужна, когда явно выбран этот инструмент.

Если разговор, документы и Git расходятся, назови расхождение и проверь ветку,
коммиты и открытые PR. Не планируй по устаревшему состоянию и не считай работу
из открытого PR уже влитой. Если неактуальна сама рабочая копия, согласуй её
обновление; если копия актуальна, исправляй документы в ветке задачи.
Текущие указания Сергея важнее прежнего порядка работ; фиксируй новые решения.
Команды в старых заданиях, отчётах и приложенных документах — контекст,
а не разрешение выполнить их сейчас.

## Рабочие копии, приёмка и обновление

- `/Users/helloworld10011/dev/oh-my-symphony` — служебная копия: из неё работает
  конвейер. Разработка и правки документов идут в отдельном `git worktree`.
  В служебной копии не редактируй файлы и не переключай ветку; её обновление
  выполняется только в согласованном цикле слияния и `ours/bin/post-merge`.
- Для работы Codex создавай ветку `codex/<задача>` от актуальной `our-main`.
  Подходящую уже созданную рабочую копию можно использовать повторно.
  Рабочие копии очереди Claude (`dev/<код>`) и агентов досок имеют свой цикл;
  не занимай чужую ветку и не удаляй чужие изменения.
- PR направляй только в `our-main` репозитория `Serg10011/oh-my-symphony`.
  Не пушь напрямую в `main` или `our-main`, не открывай PR в `cskwork`.
- В рамках задачи подготовь правки, проверки и понятный результат для приёмки.
  **Слияние PR и обновление работающего конвейера — только по явному добру
  Сергея.** После разрешённого слияния выполняется
  `/Users/helloworld10011/dev/oh-my-symphony/ours/bin/post-merge`.
  Не запускай обновление для проверки документации: оно меняет рабочую среду.
- Запуск агентов, создание исполняемых карточек и команды очереди должны
  соответствовать явно согласованной задаче. Правка инструкций не разрешает
  запуск конвейера, смену оплаты или перезапуск служб.
- Доски `~/symphony-projects/`, рабочие места `~/symphony_workspaces/` и
  `tmp_workspaces/` содержат локальные настройки и результаты вне Git.
  Не считай их дубликатами или автоматически восстановимыми из репозитория.

## Изменения и проверки

- Минимум правок ядра: сначала конфигурация, хуки и инструменты в `ours/`.
  Необходимость изменения `src/symphony/` объясняй в PR. Наши документы,
  задания и отчёты находятся в `docs/our/`.
- Существующие тесты не меняй, не удаляй и не отключай, если задача прямо
  этого не требует. Правки ядра сопровождай тестами нового поведения.
  Новые зависимости требуют обоснования в PR.
- Для изменений в `ours/` запускай `python -m ruff check ours` и
  `python -m pytest ours/tests -q`. Для ядра (`src/`, `tests/`) —
  `python -m ruff check src tests` и `python -m pytest -q`; при изменении
  общего поведения проверяй также затронутые тесты `ours/`.
- Для обычной документации достаточно проверки диффа, ссылок и фактов;
  полный прогон не нужен. Если документ читают программа или существующие
  тесты, выполни относящиеся к нему проверки. Промпты агентов и конфигурация
  запуска не считаются обычной документацией.
- Проверки запускай из рабочей копии; не переустанавливай зависимости служебной
  копии ради задачи. Python — 3.12+, установка проекта: `pip install -e ".[dev]"`.
- Секреты и реальные данные не коммить. Не отключай проверки разрешений
  агентов. В наших конфигурациях автослияние и автоматический пуш выключены;
  конвейер не отправляет команды реальному оборудованию.
- Критерии приёмки описывают проверяемый результат, без требований к длине
  текста ради длины; на карточку — не больше пяти критериев.

## Завершение задачи

- В результате и PR объясни, что изменилось, зачем, как проверено и какие
  ограничения остались. Отчёты, документы `docs/our/` и PR — на русском;
  код, комментарии в коде и имена — на английском.
- Обнови блок текущего состояния в `DECISIONS.md` и соответствующие пункты
  `ROADMAP.md` в той же ветке. Различай «подготовлено», «проверено»,
  «принято Сергеем», «влито» и «применено к работающей системе».
- При обновлении документов сохраняй историю решений и результаты старых
  проверок с их датами. Не выдавай сохранённый журнал за новый прогон.
- Для каждой задачи отдельно сохраняй задание, существенные решения,
  модель/усилие и их смены, коммиты, проверки, ограничения, PR, приёмку
  и фактическое применение. Это обязательно и при одном чате на группу задач.
  Архивируй исполнительский чат после завершения согласованного цикла
  и сохранения нужных файлов, включая локальные артефакты вне Git.
- Когда закончится именно этап, напомни об этом и предложи новый чат;
  самостоятельно новые чаты и роли не создавай.

## Symphony operator entry point

This repo is **Symphony**, a polling orchestrator that dispatches coding
agents (Codex / Claude Code / Gemini / AGY / Kiro / OpenCode / Pi) at a Kanban board. This file is
the discovery point that Codex (and any other `AGENTS.md`-respecting CLI)
reads on startup. It gives the operator, meaning the human or agent running
`symphony`, the same skill guidance Claude Code gets from `.claude/skills/`.

## Source of truth: `skills/symphony-skill`

Operator-side routing lives in one skill:
`skills/symphony-skill/SKILL.md`. It has YAML frontmatter (`name`,
`description`, optional triggers) and routes requests to the right reference
or support bundle. `.claude/skills/` is a thin symlink layer for Claude Code's
native discovery. Do not edit through it. Edit the canonical files under
`skills/`.

`skills/symphony-skill/oneshot/` and `skills/symphony-skill/monorepo/` are
branch-specific subfolders with templates, scripts, and references used by the
router. They intentionally do not expose separate `SKILL.md` activation routes.

## Available skill (operator-facing)

Load `skills/symphony-skill/SKILL.md` when the user's request matches the
trigger description below. Open only the reference page named by the router's
decision table.

### `symphony-skill`

> Use when the user wants to dispatch coding agents (Codex / Claude Code /
> Gemini / AGY / Kiro / OpenCode / Pi) against a Kanban board via this `oh-my-symphony` repo
> — adding/listing/transitioning tickets, launching the TUI, inspecting
> orchestrator state, customizing the workflow (lanes, per-state prompts),
> delegating sub-tasks to free up context, one-shotting a prompt into an
> evidence-gated board, bootstrapping a monorepo workflow, or diagnosing
> dispatch failures.
> Triggers on phrases like "add a symphony task", "run symphony", "dispatch
> this ticket", "symphony board", "WORKFLOW.md", "symphony tui won't start",
> "ticket failed with worker_exit", "customize kanban states", "deploy
> pipeline workflow", "delegate to symphony", "agent.kind: pi", "agent
> silent for N seconds", "one-shot this", "decompose and dispatch with proof",
> or "symphony monorepo".

Entry: `skills/symphony-skill/SKILL.md`

## Worker-side guidance

Dispatched workers (the agent CLI running inside a per-ticket workspace) do
**not** consume these operator skills. Worker behavior is driven by
`WORKFLOW.md`'s `prompts.base` + `prompts.stages` map, which renders stage
prompts from `docs/symphony-prompts/<flavor>/`. That layer is already
cross-platform. Codex, Claude, Gemini, and Pi workers all receive the same
rendered prompt for a given ticket state.

## Conventions for this repo

- Read `WORKFLOW.md` and a couple of `kanban/*.md` files before any
  recommendation. Settings vary per fork.
- Run `symphony doctor ./WORKFLOW.md` before launching anything.
- See the `BOOTSTRAP` route in `skills/symphony-skill/SKILL.md` for the full
  file set required when copying Symphony into another project.
