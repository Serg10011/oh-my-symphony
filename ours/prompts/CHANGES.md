# Правки промптов относительно `docs/symphony-prompts/file/`

Папка `ours/prompts/` — копия штатных промптов с минимальными правками под правила конвейера (TASK-K08a, REPORT-06 Q1). На неё ссылается `WORKFLOW.md`, собранный из `ours/templates/WORKFLOW.md.tmpl`. Файлы `stages/todo.md`, `stages/in-progress.md`, `stages/done.md` не менялись. Номера строк — в нашей копии (до правок номера те же, кроме добавленных строк в `base.md`).

Правила, ради которых правим: агент заканчивает Document в `Human Review`, `Done` ставит только человек через `confirm_done`; слияния нет (`auto_merge_on_done: false`), Verify не вливает ветку; `docs/<ID>/` и `docs/llm-wiki/` — ссылки в папку доски, в git не попадают.

| Файл | Строка | Было | Стало |
|---|---|---|---|
| `base.md` | 25 | схема `... -> Document -> Done` | `... -> Document -> Human Review -> Done (set by a human)` |
| `base.md` | 27 | `+-> critical/manual intervention -> Human Review` | ветка схемы убрана (Human Review теперь обычный финал) |
| `base.md` | 64 | `Use Human Review only for real critical/manual intervention, never as the normal completion path.` | Document заканчивается в `Human Review` — обычный финал; `Done` ставит только человек через `confirm_done` |
| `base.md` | 69 | «оркестратор создаёт единственный `--no-ff` merge при Done… wiki доставит слияние Done» | слияния в конвейере нет: Verify — только `git merge-tree` для проверки, сливает человек после разбора |
| `base.md` | 71–73 (новые) | — | общие правила: не делать `rebase`, `merge`, `reset`, не переключаться на другие ветки и не менять базовую ветку; не править git config и `.git`; не менять существующие тесты и файлы запуска тестов; `docs/<ID>/` и `docs/llm-wiki/` — ссылки в папку доски, не коммитятся, `git add -f` и замена ссылок запрещены |
| `stages/document.md` | 5 | `do not merge; the orchestrator merges once, at Done.` | `do not merge; a human merges after review.` |
| `stages/document.md` | 9 | wiki «доставит слияние Done» | wiki — ссылка в папку доски, не коммитится |
| `stages/document.md` | 16 | `Update the ticket frontmatter to Done for normal success, or Human Review only for the intervention branch.` | `Human Review` — и для обычного успеха, и для ветки вмешательства |
| `stages/document.md` | 17 | после этапа оркестратор коммитит, публикует ветку, читает `git ls-remote`, пишет SHA | хост коммитит рабочую копию в ветку карточки; ничего не сливается и не публикуется |
| `stages/document.md` | 19 | `Set state to Done for normal success. Set state to Human Review only for …intervention` | `Set state to Human Review. Never set Done: only a human sets it, through Confirm done (confirm_done).` |
| `stages/verify.md` | 5 | `the orchestrator merges -- exactly one merge … when the ticket reaches Done` | `a human merges -- nothing is merged automatically in this pipeline` |
| `stages/verify.md` | 28 | ветка `auto_merge_on_done: false`: тот же preflight «из host repo», в `## Merge Status` — что интеграция оставлена оператору | не сливать, не делать rebase/reset; preflight `git merge-tree --write-tree "$(git config --get symphony.mergetargetbranch)" HEAD` из рабочей копии (папка доски — не git), вывод в `docs/<ID>/qa/merge-tree.log`; в `## Merge Status` — цель, результат и строка `Merge deferred to a human by pipeline rule; no merge performed.` |

Почему preflight из рабочей копии: штатный текст велит запускать `git merge-tree` «from the host repo», а у нас host — папка доски без git (REPORT-09). Целевая ветка в промпт не передаётся (в шаблон попадают только `agent.max_attempts` и `agent.auto_merge_on_done`, `prompt.py:508`), поэтому агент берёт её из `symphony.mergetargetbranch` — эту настройку пишет `ours/hooks/worktree.py`.

Done подзадачам `role:worker` (S4-12) ставит не агент, а `ours/hooks/integrate.py hook` в конце `after_run`, после вливания их ветки в `integration/<ЗАДАЧА>`; промпты по-прежнему велят агенту ставить `Human Review`, правок текста не потребовалось.

Не менялось сознательно: `stages/verify.md:13,16` (ветки `app-release` и интеграционного прогона — мы их не используем); `stages/done.md` (`Done` — терминальный статус, этот промпт не вызывается); `stages/document.md:14` (раздел `## Human Review` по-прежнему пишется только при реальном вмешательстве).
