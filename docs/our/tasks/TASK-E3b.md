# E3b — обёртки `lint` / `typecheck` для агента доски EVE и проверка типов в `test.sh`

Модель: Sonnet, среднее усилие (`effort=medium`). Код форка (`src/`, `tests/`, `ours/`) **не менять**. Правятся файлы доски EVE вне репозитория — `/Users/helloworld10011/symphony-projects/eve/` (`bin/lint`, `bin/typecheck`, `test.sh`, `project.toml`) — и документы форка (`docs/our/DECISIONS.md`, `docs/our/ROADMAP.md`). Репозиторий EVE (`/Users/helloworld10011/dev/eve-pipeline`) — только читать; временную рабочую копию создавать и убирать так же, как `project-doctor` (без новых веток и коммитов).

Рабочая копия форка и ветка `dev/E3b` созданы службой очереди; сам их не создавай и не переключай. Файл задания читай по полному пути в основной папке: `/Users/helloworld10011/dev/oh-my-symphony/tmp_workspaces/inbox/E3b.md`. Первым коммитом сохрани его в `docs/our/tasks/TASK-E3b.md`. Дальше — правила `CLAUDE.md` (PR в `our-main` репозитория `Serg10011/oh-my-symphony`, PR не сливать, в `tmp_workspaces/inbox/` и `outbox/` не писать).

Прочитай: `docs/our/DECISIONS.md` — блок «Текущее состояние», запись «E3 — доска EVE» (пункты про `forbidden_paths` и `eslint.config.mjs`, `[agent] allowed_bash — пусто`, `test.sh`, время прогона); `ours/README.md` — «Права агента: `[agent] allowed_bash`». Код только читать: `ours/hooks/project_config.py` — `_check_allowed_bash`, `agent_bash_rules` (запись становится правилом-префиксом `Bash(<запись>:*)`, то есть агенту доступна та же команда **с любыми аргументами**). Файлы доски: `project.toml`, `setup.sh`, `test.sh`. В репозитории EVE: `package.json` (скрипты `lint`, `typecheck`), `tsconfig*.json` (`include`), файлы настроек ESLint.

## Зачем

Агент EVE не может сам проверить линт и типы: `npm` начинается с `#!/usr/bin/env node`, а `PATH` службы короткий (E3). Vitest типов не проверяет, поэтому ошибка типов сейчас проходит зелёной проверкой. Решение архитектора — две обёртки в папке доски; правила держит код обёртки, а не агент. Не через `PATH` службы в `plist`: он действует на всё, а правило `npm run lint` пропускает и `npm run lint -- --fix`.

## 1. Обёртки

`/Users/helloworld10011/symphony-projects/eve/bin/lint` и `bin/typecheck`, права 755. Папка `bin/` — в корне доски (не в `kanban/`, `artifacts/`, `wiki/`: эти папки открыты агенту на запись через `--add-dir`).

**Сначала сверь скрипты по самому `package.json` EVE (ветка `main`).** По записи E3: `lint` = `eslint . --ignore-pattern dist --ignore-pattern .next`, `typecheck` = `tsc --noEmit --incremental false`. Если имя другое — бери тот скрипт, что есть. Если скрипт пишет файлы (`--fix`, `--write`, `--cache`, сборка, вывод `tsc`) — обёртку для него не делай, запиши причину.

Каждая обёртка:

- `#!/bin/bash`, `set -euo pipefail`; шапка-комментарий по-русски: зачем, почему аргументы игнорируются, что правило держит этот код;
- `NODE_BIN=/Users/helloworld10011/.local/node/v24.22.1/bin`, `export PATH="$NODE_BIN:/usr/bin:/bin:/usr/sbin:/sbin"` (Node первой);
- `"$NODE_BIN/node" --version` должно быть `v24.22.1`, иначе понятная строка в stderr и код 2;
- в текущей папке нет `package.json` — строка в stderr и код 2 (запуск — в текущей папке, то есть в рабочей копии карточки);
- **все аргументы игнорируются**: `"$@"` никуда не передаётся; если аргументы есть — одна строка в stderr «обёртка аргументы не принимает, игнорирую» и работа как без них;
- в конце `exec "$NODE_BIN/npm" run lint` (или `run typecheck`); `npm_config_update_notifier=false`, `npm_config_fund=false`.

## 2. Проверка на базе (временная рабочая копия)

Как `project-doctor`: `git -C /Users/helloworld10011/dev/eve-pipeline worktree add --detach <tmp> main`, где `<tmp>` из `mktemp -d` (вне папок доски и форка); в ней `/bin/bash /Users/helloworld10011/symphony-projects/eve/setup.sh`. Затем там же, в окружении службы (`env -i HOME="$HOME" PATH=/usr/bin:/bin:/usr/sbin:/sbin /bin/bash <обёртка>`):

- а) `bin/typecheck` — код, число ошибок, время;
- б) `bin/lint` — код, число ошибок и предупреждений, время;
- в) `git status --porcelain --ignored` до а), после а) и б) и после `/bin/bash …/bin/lint -- --fix` — совпадает. Если меняются только игнорируемые кеши — запиши, какие; если меняются отслеживаемые или неотслеживаемые файлы — обёртку не ставь, остановись и спроси Сергея в окне.

Убрать копию: `git -C /Users/helloworld10011/dev/eve-pipeline worktree remove --force <tmp>`, затем `git worktree prune`. В конце `git -C /Users/helloworld10011/dev/eve-pipeline status` — чисто, лишних worktree нет.

## 3. Проверка типов в `test.sh`

- **Типы на базе чистые (код 0)** → в `test.sh` после проверки версии Node и **до** `export NODE_OPTIONS` (предзагрузку тестов в `tsc` не тащить): вызов `/bin/bash /Users/helloworld10011/symphony-projects/eve/bin/typecheck` (одно место правил). Код не 0 → строка в stderr `test.sh: проверка типов (npm run typecheck) не прошла — тесты не запускались` и `exit 1`; строка итогов при этом не печатается (`check.py` откажет — так задумано). Комментарий в `test.sh`: зачем (Vitest типов не проверяет). **Покажи путь отказа** во временной копии из п. 2, до её удаления: временный файл с ошибкой типов в папке, которую включает `tsconfig` (сверь по `include`), → `test.sh` даёт код 1, эту строку и не печатает строку итогов; файл удалить.
- **Не чистые** → `test.sh` не трогать; записать число ошибок на базе.
- Линтер в `test.sh` не ставить.
- Если весь `test.sh` на базе (типы + тесты) идёт дольше 600 с (при `tests_s = 720`) — остановись и напиши в окне; таймауты не менять.

## 4. `project.toml` доски

- `[agent] allowed_bash = ["/bin/bash /Users/helloworld10011/symphony-projects/eve/bin/lint", "/bin/bash /Users/helloworld10011/symphony-projects/eve/bin/typecheck"]` — только для созданных обёрток. Комментарий над ключом: почему обёртки, почему не `npm run` и не `PATH` в `plist`, что аргументы игнорируются.
- **Уточнение чата этапа:** в `[checks] forbidden_paths` добавить `eslint.config.*` и другие файлы настроек ESLint, если они есть в репозитории (`.eslintrc*`, `.eslintignore`). Причина (E3: «вернуться к `eslint.config.mjs`, когда в `allowed_bash` появится `npm run lint`»): иначе агент ослабит правила линта и сам получит чистый `lint`.
- Комментарий у запрета `package.json` обновить: запрет держит и содержимое скриптов, которые вызывают обёртки.
- **Добавка архитектора:** `tsconfig*.json` в `forbidden_paths` — по записи E3 он там уже есть (Vitest преобразует TypeScript по нему); проверь, что запрет на месте, и допиши в комментарий: теперь он держит и проверку типов в `test.sh` — ослабить `tsconfig` значит обойти шлюз. Если `typecheck` вызывает `tsc` с другим файлом настроек (`-p …`), который под `tsconfig*.json` не попадает, — добавь в запреты и его.

## 5. Применить и перемерить

`project-render eve` → в `WORKFLOW.md` есть правила `Bash(/bin/bash …/bin/lint:*)` и `Bash(/bin/bash …/bin/typecheck:*)`. Затем `project-doctor eve` — код 0, без FAIL; если `test.sh` менялся, это и есть перемер: новая запись в `baselines.json`, числа как прежде (3519 / 25 / 0) или объясни расхождение; время прогона записать. Службу `eve` не запускать, `launchctl` не трогать, доску `oms` не трогать.

## Критерии приёмки

- AC1: обёртки есть, 755, в `bin/` доски; работают в окружении службы; аргументы до `npm` не доходят. Сигнал: п. 2 а–в.
- AC2: `allowed_bash` — ровно созданные обёртки; правила в `WORKFLOW.md`; `project-doctor eve` без FAIL. Сигнал: п. 5.
- AC3: типы на базе чистые → `test.sh` проверяет их до тестов, ошибка типов даёт код 1 и понятную строку без строки итогов, новый замер записан; не чистые → `test.sh` не изменён, число ошибок записано. Сигнал: п. 3.
- AC4: `eslint.config.*` (и найденные файлы настроек ESLint) и `tsconfig*.json` в `forbidden_paths`. Сигнал: `project.toml`, `project-doctor` грузит его без ошибок.

## Документы (последний шаг, в тот же PR)

`docs/our/DECISIONS.md`, в записи «E3 — доска EVE» — подпункт «E3b»:

- решение архитектора и почему: Vitest типов не проверяет; обёртки игнорируют аргументы (правило-префикс пропустило бы `-- --fix`); правила держит код; почему не `PATH` в `plist`;
- итог по `test.sh` (проверка типов есть, или почему нет — с числом ошибок на базе);
- замеры: время `typecheck`, `lint`, всего `test.sh`; итог `lint` на базе (ошибки и предупреждения);
- `eslint.config.*` в запретах (уточнение чата этапа); одной строкой — почему `tsconfig*.json` в запретах важен теперь вдвойне (ослабить `tsconfig` = обойти проверку типов в шлюзе; добавка архитектора);
- обобщение в комплект (`project-render` создаёт обёртки по `[agent] checks` в `project.toml`) — позже, если понадобится третьему проекту;
- строки E3 «`[agent] allowed_bash` — пусто» и «Сознательно не запрещено: … `eslint.config.mjs`» пометить «изменено в E3b».

Блок «Текущее состояние»: сделано — E3, E3b; следующее — E4 (запрет агенту на правку `node_modules`/`.venv`, решение архитектора: до пробы), затем проба EVE после 30.10, затем S4-28. `docs/our/ROADMAP.md`: отметить E3b. Блок «В работе» не наращивай строками «Прежнее:» — оставь только текущее.

## Проверка (в описание PR: команды и результат)

Вывод п. 2 а–в, п. 3 (путь отказа), п. 5 (сводка `project-doctor eve` без ключей и путей к секретам), `git -C /Users/helloworld10011/dev/eve-pipeline status`, `ls -la /Users/helloworld10011/symphony-projects/eve /Users/helloworld10011/symphony-projects/eve/bin`. Кода форка нет — ruff и pytest не нужны.

## PR

```
git push -u origin dev/E3b
gh pr create --repo Serg10011/oh-my-symphony --base our-main --head dev/E3b
```

Описание PR на русском: что изменено в папке доски (вне репозитория — перечислить файлы и итог), что в документах, как проверено, риски. PR не сливай. В конце — ссылка на PR. Неясное — остановиться и спросить Сергея в окне.
