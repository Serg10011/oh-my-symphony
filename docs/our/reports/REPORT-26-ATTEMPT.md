# REPORT-26 — S4-36, попытка B

Дата: 01.11.2026. Задание: [TASK-S4-36](../tasks/TASK-S4-36.md), SHA-256
`7579a9fe4827155b9873892038709d726299130bab9edf7ca53b39b42421744c`.
Профиль: GPT-6 Luna / max. Ветка `codex/flaky-report-luna`, база
`our-main @ 22b692f85c6b333d6038e593fb95f833fe535619`.

Статус: подготовлено и проверено, ожидает окончательной архитектурной проверки.
Предварительный просмотр реализации архитектором не выявил замечаний к коду.
Коммита, PR, приёмки Сергеем, слияния и применения нет.

## Изменения

В `_format_report` успешный результат теперь выбирает из уже ограниченного
`Result.tail` только полные строки с буквальным префиксом `FLAKY:`. Текст,
порядок и повторы сохраняются; остальной успешный вывод остаётся скрытым.
Если таких строк нет, отчёт остаётся прежним. При FAIL форматирование полного
хвоста не меняется, отдельного списка предупреждений нет. `hook` и `gate`
используют общий форматировщик; API `run_tests`, журнал событий, карточки,
откаты и допуск Done не менялись.

В `ours/tests/test_check.py` добавлены сквозные проверки фактического вывода
`hook` и `gate`: обычный успех без хвоста, несколько предупреждений с шумом,
повтор строки и `FLAKY:` в середине другой строки; отдельно проверяется
прежний полный хвост при неуспехе. Существующие проверки не менялись и не
ослаблялись.

По просьбе архитектора исправлен только устаревший комментарий в
`ours/projects/eve/test.sh`: теперь он говорит, что успешный отчёт показывает
`FLAKY`, а файл доски также сохраняет эти строки. Команды и работа обёртки
не изменены. Эта правка комментария сделана после успешного полного прогона;
повторять набор из-за неё не требовалось.

## Среда и проверки

Собственная среда: Python **3.14.5** в
`/private/tmp/s4-36-experiment/b/.venv`; установлены зависимости проекта
командой `/private/tmp/s4-36-experiment/b/.venv/bin/python -m pip install -e '.[dev]'`
(12,4 с). Новых зависимостей проекта нет.

| Команда | Время | Результат |
|---|---:|---|
| `/private/tmp/s4-36-experiment/b/.venv/bin/python -m ruff check ours` | 0,22 с | PASS, `All checks passed!` |
| `/private/tmp/s4-36-experiment/b/.venv/bin/python -m pytest ours/tests/test_check.py -q --junitxml=/private/tmp/s4-36-experiment/b/results/target-test-check.xml --basetemp=/private/tmp/s4-36-experiment/b/target-tmp -o cache_dir=/private/tmp/s4-36-experiment/b/results/pytest-cache` | 61,80 с | **157 passed**, 0 failures/errors |
| `/private/tmp/s4-36-experiment/b/.venv/bin/python -m pytest ours/tests -q --junitxml=/private/tmp/s4-36-experiment/b/results/full-ours-tests.xml --basetemp=/private/tmp/s4-36-experiment/b/full-tmp -o cache_dir=/private/tmp/s4-36-experiment/b/results/pytest-cache-full` | 395,10 с | **1462 passed, 9 failed, 73 errors**; запуск в ограниченной среде, результат не считается проверкой набора |
| `/private/tmp/s4-36-experiment/b/.venv/bin/python -m pytest ours/tests -q --junitxml=/private/tmp/s4-36-experiment/b/results/full-ours-tests-escalated.xml --basetemp=/private/tmp/s4-36-experiment/b/full-tmp-escalated -o cache_dir=/private/tmp/s4-36-experiment/b/results/pytest-cache-escalated` (`require_escalated`) | 449,10 с | **1544 passed**, 0 failures/errors |

У первого полного запуска все 82 проблемных случая содержат `EPERM` /
`Operation not permitted`: запрещён bind к `127.0.0.1` или запуск `/bin/ps`.
Это ограничение sandbox; ошибка не связана с кодом задачи. После подтверждения
причины набор выполнен повторно с разрешённым доступом, без изменения кода,
тестов или тайм-аутов. Ошибок E3c / `TIME_WAIT` не наблюдалось.

Pytest сообщил предупреждение `pytest-asyncio`: не задан
`asyncio_default_fixture_loop_scope`, и будущая версия изменит его значение
по умолчанию. Настройки не менялись: предупреждение не относится к S4-36.

## Артефакты, время и учёт

JUnit-отчёты и временные Git-репозитории сохранены только в
`/private/tmp/s4-36-experiment/b/`. Первый полный запуск и его traceback —
`results/full-ours-tests.xml`; успешный повтор —
`results/full-ours-tests-escalated.xml`; целевой модуль —
`results/target-test-check.xml`. Соответствующие временные каталоги:
`full-tmp`, `full-tmp-escalated`, `target-tmp`.

Первый зафиксированный момент работы — 01.11.2026 10:59:55 UTC; итоговая
сверка файлов завершена 01.11.2026 11:21:09 UTC. Прошедшее время между этими
отметками — **21 мин 14 с**. Время каждого прогона указано отдельно выше.

Счётчики чата и стоимость не читались в рамках границ независимой попытки.
Для сравнения архитектору нужно учитывать расход с передачи задания до
завершения проверки и комментариев, как указано в общем задании; кэшированные
входные токены и reasoning повторно не складывать.

## SHA-256 исходников

| Файл | SHA-256 |
|---|---|
| `ours/hooks/check.py` | `4e105e4f3a595e099cb3b2109e486afd414e1ec29a05c07e9a95c610c9e88dc3` |
| `ours/tests/test_check.py` | `3c2f31498d076f8b9e1244c000604331b3d1979903d6718a2c3cc9e08e660d90` |
| `ours/projects/eve/test.sh` | `a624f0e9d751c95764853c28ce12a80e8eed07cf6e62d18a96fe52a62442c429` |
| `docs/our/tasks/TASK-S4-36.md` | `7579a9fe4827155b9873892038709d726299130bab9edf7ca53b39b42421744c` |
