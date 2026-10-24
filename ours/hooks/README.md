# Хуки механических проверок (этап 3)

`check.py` — одна программа механических проверок (только стандартная библиотека Python ≥ 3.11) с двумя режимами:

- `check.py gate` — шлюз приёмки Done (`agent.done_gate_command`). Код 0 — пропуск, любой другой исход — отказ, ядро переводит карточку в `Blocked`.
- `check.py hook` — первая строка `hooks.after_run`. Работает, только если карточка в `Done` или `Human Review`; при провале возвращает её в `In Progress` (на третьем провале — в `needs_human`) и дописывает раздел `## Check Failure`. Всегда завершается с кодом 0.

Что проверяется (подробно — `docs/our/reports/REPORT-07.md`): конфиг, база ветки, запрещённые пути, неизменность существующих тестов, обязательные разделы карточки, затем тесты проекта.

## Как подключить

1. Скрипт держать вне рабочих папок агентов, например в клоне нашего форка: `/Users/me/oh-my-symphony/ours/hooks/check.py`.
2. Скопировать `gate.example.toml` в `$SYMPHONY_WORKFLOW_DIR/gate.toml` (рядом с `WORKFLOW.md` доски), заполнить `base_ref` и `test_command`. В git его не добавлять.
3. В `WORKFLOW.md` (пути полные; `$SYMPHONY_PYTHON` ядро задаёт само):

```yaml
hooks:
  # Выше test_timeout_s с запасом на git и быстрые проверки.
  timeout_ms: 1080000          # 18 мин при test_timeout_s = 900
  after_run: |
    "$SYMPHONY_PYTHON" /Users/me/oh-my-symphony/ours/hooks/check.py hook || true
    # ... дальше штатный after_run без изменений ...

agent:
  done_gate_command: '"$SYMPHONY_PYTHON" /Users/me/oh-my-symphony/ours/hooks/check.py gate'
  done_gate_timeout_ms: 1080000  # тоже выше test_timeout_s
```

Правила:

- `check.py hook` — **первой строкой**: у штатного `after_run` есть ранний выход «нет изменений».
- `|| true` (или любая следующая строка) обязателен: если `check.py` — единственная команда, `bash` подменяет себя им, и при таймауте хука тесты остаются жить (проверено запуском, REPORT-07, Q3).
- `hooks.timeout_ms` и `done_gate_timeout_ms` — выше `test_timeout_s`. Если хук убит по таймауту, откат не происходит и Done проходит дальше, поэтому шлюз `done_gate_command` обязателен.
- Тесты в хуке дольше ~60 с при карточке в Done/Human Review опасны: сверка ядра через 60 с отменяет воркера и удаляет папку (REPORT-07, риски). Хук это распознаёт и пишет `skipped`, но вердикт тогда даёт только шлюз.

## Где смотреть результат

- `$SYMPHONY_WORKFLOW_DIR/.ours-state/check.log` — журнал (JSON-строки: время, ID, режим, исход, причина).
- `$SYMPHONY_WORKFLOW_DIR/.ours-state/<ID>.json` — счётчик провалов (не сбрасывается сам).
- Раздел `## Check Failure` в карточке.

## Тесты

```bash
python -m pytest ours/tests -q
python -m ruff check ours
```
