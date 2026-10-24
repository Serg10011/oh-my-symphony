# TASK-K08a — комплект проекта: папка доски, хуки рабочей копии, шаблон WORKFLOW, промпты

## Что прочитать
- CLAUDE.md; docs/our/DECISIONS.md (все записи 2026-10-24, особенно «Итоги K05–K07, схема
  подключения проектов, параметры EVE»).
- docs/our/reports/REPORT-09.md целиком (схема; Q5 — прототип хуков), REPORT-07.md (check.py),
  REPORT-10.md (after_run_started_at, preserve_workspace_states), REPORT-06.md (Q1 — промпты).
- ours/hooks/check.py, ours/hooks/README.md, scripts/symphony-setup-worktree.sh,
  src/symphony/_worktree_cleanup.py, docs/symphony-prompts/file/.
Если REPORT-10 в our-main нет, остановись (значит, PR K09 не влит).

## Цель
Единый для всех проектов комплект в ours/ (Python >= 3.11, только стандартная библиотека; на Mac
нет команды flock — блокировки через fcntl). Сообщения команд для человека — по-русски.

### 1. Папка доски проекта (вне репозитория кода)
~/symphony-projects/<имя>/ (корень задаётся --root):
  project.toml      — единственный файл настроек проекта (схема ниже)
  setup.sh, test.sh — заготовки с комментариями; по умолчанию выходят с кодом 1 «не настроено»
  WORKFLOW.md       — генерируется из шаблона; в начале тела предупреждение «не править руками,
                      меняй project.toml и запускай project-render»
  kanban/           — доска (создай штатно, например `symphony board init`; выясни интерфейс)
  artifacts/, wiki/ — артефакты агента (п. 4)
  worktree-exclude  — файл исключений git для рабочих копий
  .ours-state/      — состояние наших скриптов (внутри .gitignore со строкой *)

Схема project.toml:
  [project]  name, repo (абсолютный путь), base_branch, id_prefix, port
  [tools]    python, symphony, claude — абсолютные пути (заполняет project-new)
  [checks]   forbidden_paths, test_globs, allow_modified_tests, required_sections,
             max_check_failures, test_timeout_s (смысл как в gate.toml из K05)
  [timeouts] tests_s, gate_ms, hooks_ms (заполнит project-doctor в K08b; пока нет —
             безопасные умолчания: 900, 960000, 1200000)
Неизвестный ключ в любом разделе — ошибка (как в check.py).

### 2. ours/bin/project-new <имя> --repo <путь> --base <ветка> --prefix <ПРЕФИКС> --port <порт> [--root <папка>]
Создаёт папку доски. Отказ (с понятным сообщением), если: папка уже есть (без перезаписи);
репозиторий не git; нет refs/heads/<база>; папка доски оказывается внутри какого-либо
git-репозитория (ловушка REPORT-09, п. 17); имя не ^[a-z0-9][a-z0-9-]*$; префикс не
^[A-Z][A-Z0-9]{1,9}$; порт или префикс уже заняты другим проектом в том же корне; не найден
claude (через which), python или symphony оркестратора.

### 3. ours/bin/project-render <имя> [--root <папка>]
Перегенерирует WORKFLOW.md из шаблона и project.toml и печатает diff старого и нового: правка
общего шаблона расходится по всем проектам одной командой.

### 4. ours/hooks/worktree.py create|remove — свои after_create и before_remove (REPORT-09, Q5)
create:
- ID = имя рабочей папки; если ID не начинается с «<id_prefix>-», это отказ.
- РЕЖИМ «ОБНОВИТЬ»: если рабочая папка уже является рабочей копией репозитория кода на ветке
  symphony/<ID> (повторный вызов при повторном использовании папки) — только восстановить
  ссылки, исключения и запись базы (если её нет). Ветку НЕ переименовывать, setup.sh НЕ запускать.
  Иначе повторный after_create уничтожит работу карточки. Обязательный тест.
- Иначе: блокировка (fcntl) на <git-common-dir кода>/symphony-worktree.lock; штатная подготовка
  `SYMPHONY_WORKFLOW_DIR=<код> $SYMPHONY_PYTHON -m symphony._worktree_cleanup <папка>
  --prepare-setup symphony/<ID>`; чистый старт: существующая symphony/<ID> переименовывается в
  attempts/<ID>-<UTC>; `git -C <код> worktree add <папка> -b symphony/<ID> refs/heads/<база>`;
  настройки --worktree: symphony.basesha/basebranch/mergetargetbranch,
  symphony.autocommitExclude, core.excludesFile = <доска>/worktree-exclude;
  запись базы в <доска>/.ours-state/<ID>.base.json (base_sha, base_branch, created_at);
  ссылки: kanban → <доска>/kanban; docs/<ID> → <доска>/artifacts/<ID> (создать в ней work/ и qa/);
  docs/llm-wiki → <доска>/wiki (если такой путь в репозитории кода уже есть, не трогать и
  предупредить); worktree-exclude содержит /kanban, /.venv/, /.symphony-artifacts/,
  /docs/<id_prefix>-*, /docs/llm-wiki; затем `/bin/bash <доска>/setup.sh` в рабочей папке.
  Если setup.sh упал: убрать рабочую копию и только что созданную ветку, вернуть
  переименованную старую ветку на место, выход != 0.
remove: убрать только ссылки, ведущие в папку доски; затем штатный _worktree_cleanup с
SYMPHONY_WORKFLOW_DIR=<код> (как в Q5). Ветки не трогать.

### 5. Шаблон ours/templates/WORKFLOW.md.tmpl — все решения DECISIONS в одном месте
- tracker: active_states — стандартные 4 (контракты auto не отключаются); terminal_states
  [Human Review, needs_human, Done, Blocked, Archive] — именно в этом порядке.
- agent: kind claude; max_concurrent_agents 1; budget_exhausted_state needs_human;
  auto_recover_blocked false; normalize_legacy_human_review false; auto_merge_on_done false;
  auto_merge_push_target false; auto_commit_on_done true; feature_base_branch и
  auto_merge_target_branch = база; preserve_workspace_states [Human Review];
  done_gate_command = "$SYMPHONY_PYTHON" <ours>/hooks/check.py gate; done_gate_timeout_ms и
  hooks.timeout_ms — из [timeouts].
- wiki.sweep_every_n 0; свои server.port и workspace.root (~/symphony_workspaces/<имя>).
- hooks: after_create и before_remove → worktree.py; before_run — проверка ссылки kanban, как в
  штатном шаблоне; after_run — ПЕРВОЙ строкой `"$SYMPHONY_PYTHON" <ours>/hooks/check.py hook || true`,
  дальше штатный блок коммита хода.
- claude.command: абсолютный путь к claude из [tools], штатные флаги, и --add-dir для
  <доска>/kanban, <доска>/artifacts и <доска>/wiki (иначе агент не сможет писать через ссылки за
  пределы своей папки).
- prompts: абсолютные пути в ours/prompts/. Все пути в форк — абсолютные (путь форка
  определяется по расположению скриптов).
Если ядро не принимает какой-то ключ — выясни и опиши, не выдумывай обход.

### 6. Доработка ours/hooks/check.py
- Конфиг: <папка доски>/project.toml — base_branch из [project], symphony из [tools], правила из
  [checks]. gate.toml и ключи base_ref/test_command убрать.
- Тесты проекта: всегда <папка доски>/test.sh, запуск `/bin/bash <путь>` (без login-оболочки) в
  рабочей папке; группа процессов и таймаут — как сейчас.
- База: из .ours-state/<ID>.base.json; нет файла — отказ; base_sha не предок HEAD — отказ
  («ветка переписана относительно базы»). merge-base с подвижной веткой больше не используется.
- Блокировка на карточку .ours-state/<ID>.lock (fcntl): hook при занятой блокировке пишет
  skipped и выходит (второй параллельный after_run, REPORT-10 «Риски» п. 1); gate ждёт
  освобождения в пределах своего таймаута, потом проверяет.
- Остальное поведение K05 сохранить; затронутые тесты обновить; README.md хуков обновить.

### 7. Промпты ours/prompts/ — копия docs/symphony-prompts/file/ с минимальными правками
- Document заканчивает ход в Human Review (stages/document.md около строк 16 и 19); в base.md
  около строки 64 снять запрет Human Review как обычного финала; Done ставит только человек
  (confirm_done).
- Verify НЕ вливает ветку в целевую; в `## Merge Status` пишет, что слияние отложено до
  человека по правилу конвейера.
- Общие правила для агента: не делать rebase, merge, reset и не менять базовую ветку; не
  править git config; не менять существующие тесты и файлы запуска тестов; артефакты в
  docs/<ID>/ и wiki не коммитятся (это ссылки в папку доски).
- Все правки перечислить в ours/prompts/CHANGES.md (файл, строка, было/стало).
- ВАЖНО: выясни по коду, не проверяет ли ядро на переходе Verify → Document сам факт слияния
  (ворота, контракт, Merge Status). Если проверяет — ОСТАНОВИСЬ и опиши, не обходи.

## Проверка запуском (обязательно)
- ours/tests на временных git-репозиториях: worktree create (чистый старт с переименованием;
  режим «обновить» не трогает ветку и не запускает setup.sh; ловушка; неверный префикс; сбой
  setup.sh с откатом; ссылки и исключения; запись базы), remove; project-new (созданные файлы,
  каждый отказ); project-render; check.py (project.toml, база из записи, не предок HEAD,
  блокировка в hook и gate).
- Сквозной прогон, как в REPORT-09: project-new создаёт папку доски для временного репозитория
  кода; настоящая служба symphony с мок-бэкендом ведёт карточку <ПРЕФИКС>-1 до Human Review.
  Проверь: рабочая копия создана нашим хуком; артефакты лежат в папке доски; check.py hook
  отработал (.ours-state/check.log); git status репозитория кода пуст; в ветке symphony/<ID> нет
  файлов docs/<ID> и docs/llm-wiki. Если мок не проходит этапы с контрактами — опиши, что удалось
  проверить, а что нет.

## Рамки
- git pull our-main, ветка ours/project-kit. Не трогай src/, DECISIONS.md, ROADMAP.md.
- Ориентир: код без тестов ≈600–900 строк. Если выходит заметно больше — остановись после
  worktree.py, project-new и шаблона и пришли промежуточный отчёт.
- ruff на ours/; python -m pytest ours/tests -q; полный основной прогон без новых падений
  (база 3878 / 136 / 2); pyright отдельно от pytest.
- ours/README.md простым языком: порядок подключения проекта (project-new → заполнить
  setup.sh, test.sh, [checks] → project-doctor (появится в K08b) → запуск службы).
- docs/our/reports/REPORT-11.md (до трёх страниц), пометки «проверено запуском» / «по коду».
- PR только в наш форк, ветка our-main. Итоговое сообщение: ссылка на PR, тесты, расхождения,
  что не удалось проверить.
