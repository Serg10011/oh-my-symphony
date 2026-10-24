# TASK-K08b — project-doctor, строка итогов тестов, лимиты ходов, единый таймаут

## Что прочитать
- CLAUDE.md; docs/our/DECISIONS.md (записи 2026-10-24); docs/our/reports/REPORT-11.md;
  ours/README.md, ours/hooks/README.md, ours/hooks/check.py, ours/hooks/project_config.py,
  ours/bin/project-new, ours/templates/WORKFLOW.md.tmpl.
Если REPORT-11 в our-main нет, остановись (значит, PR K08a не влит).

## 1. Строка итогов тестов (контракт test.sh)
- test.sh проекта в конце печатает строку `PIPELINE_TESTS passed=<n> skipped=<n> failed=<n>`;
  считается ПОСЛЕДНЯЯ такая строка в выводе.
- Новый общий помощник ours/hooks/summarize.py <junit.xml>: читает JUnit XML (testsuites/testsuite/
  testcase; <skipped/> = пропущен, <failure>/<error> = упал, иначе прошёл) и печатает эту строку.
  Неразборчивый или пустой файл — код != 0 и никакой строки. Тесты на образцах JUnit от Vitest,
  pytest и Jest (собери образцы по документации форматов) и на вложенных testsuite.
- Заготовка test.sh из project-new: комментарии с примерами для Vitest
  (`--reporter=junit --outputFile=<tmp>`) и pytest (`--junitxml=<tmp>`); отчёт пишется во временный
  файл вне рабочей папки (mktemp), затем `"$SYMPHONY_PYTHON" <ours>/hooks/summarize.py <файл>`.
- check.py: после кода возврата 0 требует строку итогов; нет строки — отказ; failed > 0 — отказ
  даже при коде 0; сравнение с базой из .ours-state/baseline.json: passed меньше базы — отказ;
  skipped больше базы — отказ; нет baseline.json — отказ «запусти project-doctor». В тексте
  отказа — обе тройки чисел и подсказка перезапустить project-doctor, если базовая ветка менялась.

## 2. ours/bin/project-doctor <имя> [--root <папка>]
Проверка готовности проекта, вывод по-русски списком PASS/FAIL/WARN, код 0 только без FAIL:
- project.toml разбирается; инструменты из [tools] существуют и исполняемы (`claude --version`
  запускается); репозиторий — git, есть refs/heads/<база>; папка доски не внутри git; порт и
  префикс не заняты другим проектом в том же корне.
- setup.sh и test.sh — не заготовки (метка-строка заготовки удалена), иначе FAIL «не настроено».
- Пробная рабочая копия: `git -C <код> worktree add --detach <временная папка вне доски и кода>
  refs/heads/<база>`; в ней с замером времени `/bin/bash setup.sh` (предел 3600 с), затем
  `/bin/bash test.sh` (предел 3600 с), своя группа процессов и убийство группы, как в check.py.
  Требуется: код 0, строка итогов есть, failed = 0. Результат пишется в .ours-state/baseline.json
  (base_branch, base_sha, passed, skipped, failed, setup_s, tests_s, measured_at). Пробная копия
  удаляется всегда (finally) + `git worktree prune`; ветки не создаются.
- Таймауты в [timeouts]: tests_s = max(300, вверх до минуты(2 × замер тестов));
  gate_ms = (tests_s + 60) × 1000; hooks_ms = (max(вверх(1.5 × замер setup), tests_s) + 60) × 1000.
  Если в [timeouts] стоит fixed = true — не перезаписывать, только показать рекомендацию.
  Запись в project.toml — правкой только раздела [timeouts], остальной текст и комментарии
  сохраняются побайтно (тест на это).
- Затем project-render и `<[tools].symphony> doctor` в папке доски; ожидаемые WARN (папка доски
  не git, agent git grant) показать как известные, не как провал.

## 3. Единый таймаут тестов и лимиты ходов
- Убрать `[checks] test_timeout_s`: check.py берёт [timeouts].tests_s. Старый ключ — ошибка с
  подсказкой, куда перенести.
- Новый раздел [limits] в project.toml: max_turns (умолчание 30), max_total_turns (60),
  max_attempts (3) → в шаблон, раздел agent. Выясни по коду точный смысл каждого ключа ядра и
  опиши в комментариях project.toml; если для дневного/ночного контроля важен ещё какой-то
  лимит (например, токены), назови его в отчёте, но не добавляй.
- Если в our-main уже есть ключ ядра workspace.code_repo (параллельная задача K10), добавь его в
  шаблон (значение — [project].repo); если ключа ещё нет — не добавляй и отметь в отчёте.

## 4. Проверка запуском
- ours/tests: summarize.py; check.py (строка итогов, сравнение с baseline, нет baseline);
  project-doctor на временном проекте с поддельными setup.sh/test.sh (успех; упавший тест; нет
  строки итогов; test.sh-заготовка; fixed = true; сохранение текста project.toml).
- Сквозной ours/tests/e2e/run.sh: дополни так, чтобы test.sh печатал строку итогов через
  summarize.py, перед службой запускался project-doctor, и проверь отказ check.py, когда агент
  удаляет тест из запуска (passed меньше базы).

## 5. Документы (отдельным последним коммитом)
- В docs/our/DECISIONS.md добавь запись ниже дословно сверху раздела «## Решения», перед записью
  «2026-10-24 — Итоги K05–K07, схема подключения проектов, параметры EVE».
- В docs/our/ROADMAP.md, этап 3: поставь [x] у «Правка №4 … скрипт чистого старта» (допиши
  « — ours/hooks/worktree.py, K08a»), у «Хук after_run с откатом Done» (допиши « — check.py hook +
  шлюз Done»), у «Рабочая конфигурация WORKFLOW.md» (допиши « — шаблон K08a; видимость needs_human
  и Blocked — отдельной задачей»).

=== начало записи ===
### 2026-10-24 — Итоги K08a и K09 (REPORT-10, REPORT-11)

- **K09:** сверка не трогает воркера, пока идёт `after_run` (отметка `after_run_started_at`, предел `hooks.timeout_ms` + 60 с; действует и на проверку зависания). Ключ `agent.preserve_workspace_states` (в шаблоне `[Human Review]`; `Done` запрещён). Остаётся: при отмене посреди хука (после предела, пауза, остановка службы) ядро запускает второй `after_run`; `check.py` защищён блокировкой на карточку.
- **K08a: комплект проекта** — `ours/bin/project-new`, `project-render`, `ours/hooks/worktree.py`, `ours/templates/WORKFLOW.md.tmpl`, `ours/prompts/` (правки в `CHANGES.md`). `check.py` читает `project.toml`, запускает `<доска>/test.sh` без login-оболочки, база — из `.ours-state/<ID>.base.json`, блокировка на карточку. Сквозной прогон на настоящей службе с поддельным `claude`: до Human Review и через шлюз в Done; репозиторий кода чист; артефакты и wiki — в папке доски через ссылки, контракты ядра их видят.
- **Поправка к записи K05, п. 1:** штатный Verify ветку не вливает (`verify.md`: «you do NOT merge»); сливало ядро при Done и только при `auto_merge_on_done`. Комментарии мок-конфига устарели. База из записи всё равно нужна: защищает от слияния кем угодно.
- **Ядро не проверяет факт слияния на Verify → Document:** контракт требует только непустой `## Merge Status`.
- **Проверить на Mac первым делом:** настоящий `claude`; при абсолютном пути к `claude` ядро не даёт агенту доступ к `.git` (WARN `agent git grant`, доступ сознательно не добавлен) — работает ли `git merge-tree --write-tree` из командной строки агента (если нет — убрать этот шаг из нашего Verify); `fcntl` на macOS; подтверждение во время хука — шлюз ждёт блокировку до 30 с и отказывает (Blocked).
- **Мелочи:** `symphony board init` кладёт демо-карточку — `project-new` её удаляет; при чистом старте старые артефакты уходят в `artifacts/.attempts/`; `setup.sh` ограничен `hooks_ms − 30 с`; режим «обновить» проверен только тестами (ядро повторно `after_create` не вызывает).
- **Базовый уровень тестов:** основной набор 3878 / 136 / 2; `ours/tests` — 142.
- **Кредит:** K09 ≈ $5, K08a ≈ $10. Остаток $79 на 24.10.
=== конец записи ===

## Рамки
- git pull our-main, ветка ours/doctor-summary. Не трогай src/ (параллельно K10).
- ruff на ours/; python -m pytest ours/tests -q; полный основной прогон без новых падений
  (база 3878 / 136 / 2; pyright отдельно от pytest; ours/tests не гонять параллельно с основным).
- ours/README.md: дополнить порядок подключения (project-doctor, строка итогов, лимиты).
- docs/our/reports/REPORT-12.md (до двух страниц), пометки «проверено запуском» / «по коду».
- PR только в наш форк, ветка our-main. Итоговое сообщение: ссылка на PR, тесты, расхождения.
