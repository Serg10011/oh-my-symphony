# TASK-K13 — итоги пробы на Mac, правки комплекта, порядок подключения

## Что прочитать
- CLAUDE.md; docs/our/DECISIONS.md (блок «Текущее состояние», таблица «Расход облачного кредита», записи «Итоги K08a», «Итоги K08b», «Итоги K09», «Итоги K10 и K11»); ours/README.md; ours/templates/WORKFLOW.md.tmpl; ours/hooks/project_config.py, check.py, summarize.py; ours/bin/project-new, project-doctor, project-render; docs/our/reports/REPORT-11..14.
- Код ядра (src/) не меняется. Сначала по кодам задач убедись, что ниже описанного ещё нет; что уже есть — не дублируй, опиши в итоге.

## Контекст: что показала проба на Mac (факты, не пересказывай их иначе)
Проба проведена Сергеем на Mac mini. Проект `oms` = сам форк (ветка our-main, клон ~/dev/oms-pipeline). «Запуск» — проверено запуском на Mac.
- Среда: Python 3.14.5 (python.org), Node из Homebrew (/opt/homebrew/bin), `claude` в ~/.local/bin/claude. Модель проекта — claude-haiku-5-5: задана переменной окружения `ANTHROPIC_MODEL=haiku` в окружении службы (процесс claude наследует её).
- Запуск: `ours/tests` 198 passed; `ours/tests/e2e/run.sh` PASS; `fcntl.flock` и `killpg` на macOS работают.
- Настоящий `claude`: карточка OMS-2 (Todo → Human Review → Done) и OMS-3 (то же, службой под launchd) прошли все этапы. Строка контракта у базы и у обеих карточек: `PIPELINE_TESTS passed=4084 skipped=137 failed=0` (основной набор 3886 / 136 / 0 с исключением одного теста tests/test_pm_ui_node.py::test_pm_ui_node_regressions плюс ours/tests 198).
- `git merge-tree --write-tree` из Bash агента работает (результат — чистое дерево, qa/merge-tree.log). Нужны две вещи: разрешение `Bash(git merge-tree:*)` в `--allowedTools` и доступ к `.git`, который служба выдаёт сама (в логе `claude_git_roots_granted`).
- Долгий хук `check.py hook` (полный прогон test.sh): 3 мин 52 с (OMS-2) и 3 мин 35 с (OMS-3), воркер не отменён (подтверждение K09).
- Confirm done через шлюз `check.py gate`: прошёл для OMS-2 и OMS-3 (последний — под launchd).
- Защиты ядра на настоящем claude: `max_turns` (30) → `budget_exhausted` + `needs_human` (OMS-1); сторож `stalled_session`; пауза `paused for operator inspection`, снимается `POST /api/v1/<ID>/resume`.
- `project-doctor`: сначала FAIL (113 падений, недетерминированно; в одном воспроизведении sqlite «unable to open database file»); PASS после `ulimit -n 10240` в начале test.sh. Причина «лимит открытых файлов 256 по умолчанию на macOS» — предположение (один зелёный прогон не доказательство). Замеренные таймауты: tests_s=420, gate_ms=480000, hooks_ms=480000.
- launchd (замерено `ps eww -p <pid>` у службы, запущенной из plist): `PATH=/usr/bin:/bin:/usr/sbin:/sbin` (как задан в plist), `HOME`, `USER`, `LOGNAME`, `SHELL=/bin/zsh`, `TMPDIR=/var/folders/…/T/` (выдал launchd); `LANG` и `LC_ALL` НЕ заданы; `launchctl limit maxfiles` = 256 (hard — без ограничения). setup.sh и test.sh в таком окружении отработали. Файл plist (процесс в переднем плане, команда та же, что строит `symphony service start`: `<python> -m symphony.cli <WORKFLOW.md> --host 127.0.0.1 --port <порт>`, RunAtLoad, KeepAlive=false) лежит у Сергея в ~/symphony-projects/oms/launchd.plist.
- Стоимость пробы: идёт на подписке Max, облачный кредит не тратит. По счётчику `cost_usd` в логе службы OMS-3 ≈ $0,11 (4 хода Haiku).
- Не проверено на Mac: Vitest и Jest (проект EVE); автозапуск службы после перезагрузки Mac и поведение при закрытой крышке/сне; `--effort` у claude и модель на уровне задачи.

Что пошло не так по ходу пробы (причина → что нужно в комплекте):
1. В `claude -p --permission-mode acceptEdits` Bash-команды вне списка отклоняются; агент зациклился до `max_turns`. Нужен узкий `--allowedTools`. Составные команды (`&&`, `;`, `|`) отклоняются, если хоть одна часть не разрешена. Флаги, отключающие проверки разрешений, запрещены правилом проекта — не использовать.
2. Сторож `stall_timeout_ms` (в шаблоне жёстко 300000) срабатывал во время долгого `test.sh` (агент молчит 3–5 минут) → пауза. Вручную поставлено 1200000 — помогло.
3. Флаг `budget_exhausted` хранится в state.db и штатно не снимается; карточку пришлось перенести в Archive и завести новую с подсказкой.
4. `project-render` и `project-doctor` пересоздают WORKFLOW.md по шаблону и project.toml: ручные правки WORKFLOW.md живут только до следующего запуска.
5. Окружение агента отличается от окружения замера базы: в нём переменные `SYMPHONY_*` службы; прямой `pytest` дал 9 падений, а `test.sh` — 0. Лечение в test.sh проекта: очистка `SYMPHONY_*` (кроме `SYMPHONY_PYTHON` и `SYMPHONY_WORKFLOW_DIR`), `ulimit -n 10240`, `PATH` с Node, тест времени (`-m timing`) отдельным последовательным проходом (под нагрузкой он флейкал). Агент должен гонять только `test.sh`.
6. Ошибка `SQLite objects created in a thread can only be used in that same thread` (сообщение `run_registry_error`, операция `budget_reopen_history`): 25 раз с 21:20 до 21:56 UTC 24.10 в службе, запущенной из Терминала; в окне launchd-запуска (22:12–03:18) не встречалась. Работе не мешала. Только записать как открытый вопрос к ядру — не чинить.
7. Для проекта-форка встроенный запрет `ours/**` в check.py остаётся, поэтому агент конвейера не может править сам комплект. Решение (оставлять так или нет) за Сергеем — только описать в README.

## 1. Правки комплекта (ours/)
а) **Права агента.** Новая секция `[agent]` в project.toml, ключ `allowed_bash` — список дополнительных разрешений для Bash проекта (например, `.venv/bin/python -m ruff`). Шаблон добавляет в команду claude `--allowedTools` со встроенной базой: `/bin/bash <папка доски>/test.sh`, `git merge-tree`, `test`, `echo`, `cat`, `wc`, `head`, `tail` (именно этот набор прошёл пробу; не расширяй) плюс значения проекта. `project_config.py` проверяет ключ: пустые записи, одиночный `*`, `Bash(*)` и всё, что похоже на отключение проверок разрешений, — ошибка с понятным текстом. В заготовке project.toml из `project-new` — закомментированный пример. Тесты в ours/tests.
б) **Сторож зависания.** `stall_timeout_ms` в шаблоне вычисляется из `[timeouts] tests_s`: не меньше 300000 и не меньше (tests_s + 120) × 1000. Сначала по коду ядра (src/symphony/orchestrator, поиск `stall_timeout_ms`) выясни, что именно сбрасывает таймер (поток событий агента или что-то ещё), и опиши в итоге; если формула не годится для долгого Bash-вызова — скажи об этом, а не подгоняй. `project-doctor` пересчитывает значение при пересчёте таймаутов.
в) **`summarize.py`** принимает несколько отчётов (`summarize.py a.xml b.xml …`) и печатает общую строку `PIPELINE_TESTS` (суммы passed/skipped/failed). Сейчас test.sh проекта oms склеивает три отчёта встроенным Python-кодом — после этого он станет проще. Тесты; поведение с одним отчётом не меняется.
г) **Заготовки setup.sh/test.sh** в `project-new`: комментарии-подсказки про `ulimit -n` (macOS даёт 256), про то, что окружение службы бедное (`PATH` коротким, `LANG` нет, лишние `SYMPHONY_*` у агента и хуков), про отдельный последовательный проход для тестов с таймингом. Логику в заготовки не добавляй — только комментарии.
д) **Автозапуск через launchd.** Команда `ours/bin/project-launchd <имя>`: читает project.toml и пишет `launchd.plist` в папку доски (как `project-render` пишет WORKFLOW.md), по образцу plist из контекста выше: label `local.symphony.<имя>`, `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, `HOME`, `PYTHONUNBUFFERED=1`, `SYMPHONY_PYTHON` из `[tools] python`, `WorkingDirectory` — папка доски, лог — `log/symphony.log`, RunAtLoad true, KeepAlive false. Необязательный ключ `[service] model` (например `haiku`) добавляет `ANTHROPIC_MODEL`; без ключа переменная не задаётся. После записи печатает команды `launchctl bootstrap/bootout gui/$(id -u) …` и напоминание, что служба из Терминала (`symphony service start`) и служба из launchd — одно и то же вместо другого, не одновременно. Если окажется больше ~150 строк — сделай вместо команды шаблон plist и инструкцию в README. Тесты.

## 2. ours/README.md — «Как подключить проект»
Уточни порядок по итогам пробы (по шагам, как сейчас) и добавь: права агента (`[agent] allowed_bash`) перед первой карточкой; запуск через launchd (`project-launchd`) и проверка окружения службы (`ps eww -p <pid>`); первая карточка маленькая, с подсказкой «тесты только через test.sh, по одной команде на вызов Bash, без `&&`, `;`, `|`»; карточка с исчерпанным бюджетом (`budget_exhausted`) не оживает — заводи новую; перед `project-doctor` служба должна быть остановлена; ручные правки WORKFLOW.md не живут; для проекта-форка — три ловушки (отдельный клон, не основная папка; своя venv в каждой рабочей копии и проверка, что symphony импортируется из неё; CLAUDE.md с разделом «Если ты агент конвейера») и факт про `ours/**` (п.7 контекста); окружение launchd (`LANG` нет, `TMPDIR` выдаёт launchd, лимит файлов 256).

## 3. CLAUDE.md
В раздел «Если ты агент конвейера» добавь: тесты — только командой, которую даёт карточка или промпт этапа (обычно `/bin/bash <папка доски>/test.sh`); прямой `pytest` в окружении службы даёт ложные падения; Python — `.venv/bin/python` рабочей копии; одна команда на вызов Bash.

## 4. DECISIONS.md
- Новая запись «Итоги пробы на Mac (K13)» сверху: контекст выше (что закрыто запуском, что пошло не так и чем исправлено, что осталось не проверено), цифры, стоимость.
- В записях K08a, K08b, K09, K10/K11 к строкам «Не проверено» добавь «Закрыто пробой на Mac (K13): …» с тем, что именно закрыто; сами строки не удаляй.
- Открытые вопросы в запись: п.6 (SQLite), п.3 (сброс budget_exhausted — нужно решение; пока правило «новая карточка»), п.7 (`ours/**`), модель/усилие на уровне задачи.
- Блок «Текущее состояние» по правилам CLAUDE.md: дата 25.10.2026; сделано — проба на Mac и K13; в работе — открытые задачи этапа 3 (утренний отчёт, модель/усилие на уровне задачи) — сохрани то, что там сейчас сказано; следующее — пересоздать WORKFLOW.md проекта oms (`project-doctor` после слияния этого PR), потом по ROADMAP.
- Таблица кредита: строка «K13 (Sonnet) | уточнить | уточнить»; остаток $59 без изменений (стоимость K12 тоже «уточнить» — не выдумывай).

## Рамки
- git pull our-main, ветка ours/mac-probe. src/ не трогать. Существующие тесты не менять, кроме строго нужных для шаблона (если менял — перечисли в PR).
- Проверки: python -m ruff check src tests; python -m pytest ours/tests -q; ours/tests/e2e/run.sh. Основной набор не нужен (src/ не менялся).
- Сохрани задание как docs/our/tasks/TASK-K13-mac-probe-results.md и напиши отчёт docs/our/reports/REPORT-15.md (проверь следующий свободный номер) в том же PR.
- Описание PR на русском: что сделано, зачем, как проверено («запуск» / «по коду»), что не сделано, риски. PR только в наш форк, ветка our-main.
- Итог: ссылка на PR; затем список строк, которые Сергей добавит в ~/symphony-projects/oms/project.toml (секция `[agent] allowed_bash` с `.venv/bin/python -m ruff` и при необходимости `[service] model = "haiku"`); затем команда одной строкой:
  git -C /Users/helloworld10011/dev/oh-my-symphony checkout our-main && git -C /Users/helloworld10011/dev/oh-my-symphony pull --ff-only
