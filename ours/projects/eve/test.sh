#!/bin/bash
# EVE-Companion's full Vitest run, called from a disposable project worktree
# by check.py or project-doctor. JUnit files stay outside that worktree (S4-12).
# Runner flags preserve EVE's validated setup: Node 24.22.1, two workers,
# 15s test timeout and the local-connection preload. Without the preload,
# Miniflare can exhaust macOS ephemeral ports with TIME_WAIT connections.
#
# E3c adds a shared test lock, bounded TIME_WAIT waits, benchmark-variable
# cleanup, removal of stale .next types, and one retry of failed files only.
# Retry suites replace their first-run results instead of double-counting.
# Keep absolute tool paths: launchd supplies a minimal PATH.
# workerd/Miniflare need more than the default macOS open-file limit.
ulimit -n 10240 || echo "test.sh: не удалось поднять лимит открытых файлов (сейчас $(ulimit -n))" >&2
echo "test.sh: лимит открытых файлов: $(ulimit -n)"

# Keep only the two Symphony variables used by this wrapper; project tests
# should not inherit the agent's SYMPHONY_* environment.
for name in $(compgen -e | grep '^SYMPHONY_'); do
  case "$name" in
    SYMPHONY_PYTHON|SYMPHONY_WORKFLOW_DIR) ;;
    *) unset "$name" ;;
  esac
done

# These opt-in benchmarks are expensive and write to docs/reference.
unset RUN_HISTORY_MCP_VOLUME EVE_HISTORY_ECONOMY_BASELINE EVE_HISTORY_PROFILE_BENCH EVE_HISTORY_PROFILE_DAILY

START_S=$(date +%s)
BOARD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${SYMPHONY_PYTHON:-/Users/helloworld10011/dev/oh-my-symphony/.venv/bin/python}"
SUMMARIZE=/Users/helloworld10011/dev/oh-my-symphony/ours/hooks/summarize.py
JUNIT_MERGE="$BOARD_DIR/bin/junit-merge.py"
NETSTAT=/usr/sbin/netstat

NODE_BIN=/Users/helloworld10011/.local/node/v24.22.1/bin
export PATH="$NODE_BIN:/usr/bin:/bin:/usr/sbin:/sbin"
found="$("$NODE_BIN/node" --version 2>&1 || true)"
if [ "$found" != "v24.22.1" ]; then
  echo "test.sh: нужен Node v24.22.1 в $NODE_BIN, найдено: ${found:-ничего}" >&2
  exit 1
fi
echo "test.sh: node $found"

# ---- time budget -----------------------------------------------------------------------------
# check.py's tests_s includes lock waits. Fall back to 720s if absent.
TESTS_S="$(sed -n 's/^tests_s[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$BOARD_DIR/project.toml" 2>/dev/null | head -n 1)"
case "$TESTS_S" in ''|*[!0-9]*) TESTS_S=720 ;; esac
DEADLINE_S=$(( START_S + TESTS_S ))

# Reserve the measured baseline, both TIME_WAIT waits and a failed-file retry.
# Use half of tests_s if project-doctor has not recorded a measurement.
TIME_WAIT_MAX_S=120      # cap for each TIME_WAIT wait
RESTART_RUN_S=180        # reserve for retrying failed files
MEASURED_S="$(sed -n 's/.*"tests_s":[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$BOARD_DIR/.ours-state/baseline.json" 2>/dev/null | head -n 1)"
case "$MEASURED_S" in ''|*[!0-9]*) MEASURED_S=$(( TESTS_S / 2 )) ;; esac
RUN_BUDGET_S=$(( MEASURED_S + TIME_WAIT_MAX_S + TIME_WAIT_MAX_S + RESTART_RUN_S ))

# ---- cleanup and type check ------------------------------------------------------------------
# tsconfig includes .next/types/**; stale build output can break tsc.
# Only remove .next in the current project worktree with a package.json.
if [ -f "$PWD/package.json" ] && { [ -e "$PWD/.next" ] || [ -L "$PWD/.next" ]; }; then
  rm -rf "$PWD/.next"
  echo "test.sh: удалён $PWD/.next (его типы входят в tsconfig)"
fi

# Vitest does not type-check. Run the existing board wrapper first, without
# the test preload. No PIPELINE_TESTS on failure: check.py must reject it.
if ! /bin/bash "$BOARD_DIR/bin/typecheck"; then
  echo "test.sh: проверка типов (npm run typecheck) не прошла — тесты не запускались" >&2
  exit 1
fi

# ---- shared machine lock ---------------------------------------------------------------------
# The shell keeps FD 9 open throughout both runs. The helper locks the inherited
# open file description; its exit does not release the shell's copy. Children
# also inherit it, so killing the shell cannot unlock a still-running test child.
# Never unlink/rename the lock file: all contenders must open the same inode.
# Only the EVE board currently participates; OMS integration is a separate task.
LOCK_ROOT=/Users/helloworld10011/symphony-projects/.locks
LOCK_FILE="$LOCK_ROOT/test.lock"
LOCK_POLL_S=${EVE_TEST_LOCK_POLL_S:-5}

dir="$(mktemp -d -t eve-junit.XXXXXX)" || exit 1
cleanup() {
  rm -rf "$dir"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
trap 'exit 129' HUP

acquire_lock() {
  mkdir -p "$LOCK_ROOT" || { echo "ENV: не создать $LOCK_ROOT" >&2; return 75; }
  if [ -d "$LOCK_FILE" ]; then
    echo "ENV: test lock busy (старый каталог-замок $LOCK_FILE; проверь завершение прежнего прогона перед миграцией)" >&2
    return 75
  fi
  if ! { exec 9>>"$LOCK_FILE"; } 2>/dev/null; then
    echo "ENV: не открыть замок $LOCK_FILE" >&2
    return 75
  fi
  "$PY" "$BOARD_DIR/bin/test-lock.py" --fd 9 \
    --deadline "$(( DEADLINE_S - RUN_BUDGET_S ))" --poll "$LOCK_POLL_S"
}
acquire_lock || exit "$?"

# ---- TIME_WAIT wait --------------------------------------------------------------------------
# E3c measured 0 idle / 273 peak / 0 after ~30s on the baseline run.
# 1000 leaves margin above that peak within the 16384-port macOS pool.
TIME_WAIT_LIMIT=${EVE_TEST_TIME_WAIT_LIMIT:-1000}

time_wait_count() {
  "$NETSTAT" -an -p tcp 2>/dev/null | /usr/bin/awk '$6 == "TIME_WAIT" && $4 ~ /^127\.0\.0\.1\./ { n++ } END { print n + 0 }'
}

wait_time_wait() {
  local since now n
  since=$(date +%s)
  n="$(time_wait_count)"
  echo "test.sh: TIME_WAIT на 127.0.0.1: $n (порог $TIME_WAIT_LIMIT)"
  while [ "$n" -ge "$TIME_WAIT_LIMIT" ]; do
    now=$(date +%s)
    if [ $(( now - since )) -ge "$TIME_WAIT_MAX_S" ]; then
      echo "test.sh: ПРЕДУПРЕЖДЕНИЕ: TIME_WAIT на 127.0.0.1 всё ещё $n (порог $TIME_WAIT_LIMIT) после $TIME_WAIT_MAX_S с, запускаю тесты"
      return 0
    fi
    echo "test.sh: жду TIME_WAIT на 127.0.0.1: $n (порог $TIME_WAIT_LIMIT), прошло $(( now - since )) с"
    sleep 10
    n="$(time_wait_count)"
  done
  if [ $(( $(date +%s) - since )) -gt 0 ]; then
    echo "test.sh: TIME_WAIT на 127.0.0.1 упал до $n, ждал $(( $(date +%s) - since )) с"
  fi
}

# ---- Vitest run ------------------------------------------------------------------------------
preload="$PWD/tests/helpers/history-store-local-connections.mjs"
if [ ! -f "$preload" ]; then
  echo "test.sh: нет файла $preload (его требует режим запуска тестов EVE)" >&2
  exit 1
fi
export NODE_OPTIONS="--import=$preload"

# $1 is the JUnit path; remaining arguments select files (empty = full suite).
run_vitest() {
  local report="$1"
  shift
  "$NODE_BIN/npm" test -- --maxWorkers=2 --testTimeout=15000 --reporter=default --reporter=junit --outputFile.junit="$report" "$@"
}

wait_time_wait
run_vitest "$dir/junit.xml"
rc=$?

if [ "$rc" -eq 0 ]; then
  "$PY" "$SUMMARIZE" "$dir/junit.xml" || exit $(( rc ? rc : 1 ))
  exit 0
fi

# ---- retry failed files once ------------------------------------------------------------------
# Keep the same runner flags and write a separate JUnit file outside the
# worktree. A green retry is FLAKY; a second failure remains a failure.
echo "test.sh: первый прогон упал (код $rc)"
"$PY" "$SUMMARIZE" "$dir/junit.xml" 2>/dev/null | sed 's/^PIPELINE_TESTS/test.sh: итоги первого прогона:/'

fail_summary() {
  # Preserve the first failed summary and exit code if retry is unusable.
  "$PY" "$SUMMARIZE" "$dir/junit.xml" || true
  exit "$rc"
}

failed_list="$("$PY" "$JUNIT_MERGE" failed "$dir/junit.xml")"
if [ $? -ne 0 ] || [ -z "$failed_list" ]; then
  echo "test.sh: в отчёте нет упавших файлов (сбой вне тестов?) — перезапуск не делаю, падение настоящее"
  fail_summary
fi
files=()
while IFS= read -r line; do
  [ -n "$line" ] && files+=("$line")
done <<EOF
$failed_list
EOF
restart_s="$("$PY" "$JUNIT_MERGE" seconds "$dir/junit.xml")"
case "$restart_s" in ''|*[!0-9]*) restart_s=$RESTART_RUN_S ;; esac
need_s=$(( restart_s + 60 + TIME_WAIT_MAX_S ))
left_s=$(( DEADLINE_S - $(date +%s) ))
if [ "$left_s" -lt "$need_s" ]; then
  echo "test.sh: упало файлов: ${#files[@]}; до tests_s осталось $left_s с, на перезапуск нужно ≈ $need_s с — перезапуск не делаю, падение настоящее"
  fail_summary
fi
echo "test.sh: перезапускаю упавшие файлы (${#files[@]}): ${files[*]}"
wait_time_wait
run_vitest "$dir/junit-retry.xml" "${files[@]}"
rc2=$?
echo "test.sh: перезапуск закончился с кодом $rc2"

merge_out="$("$PY" "$JUNIT_MERGE" merge "$dir/junit.xml" "$dir/junit-retry.xml" "$dir/junit-merged.xml")"
merge_rc=$?
"$PY" "$SUMMARIZE" "$dir/junit-retry.xml" 2>/dev/null | sed 's/^PIPELINE_TESTS/test.sh: итоги перезапуска (только упавшие файлы):/'
if [ "$merge_rc" -eq 2 ]; then
  echo "test.sh: отчёт перезапуска не записан или не читается — перезапуск считаю неудавшимся, падение настоящее"
  fail_summary
fi
[ -n "$merge_out" ] && echo "$merge_out"
if [ "$merge_rc" -ne 0 ]; then
  echo "test.sh: файлы упали и при перезапуске — падение настоящее"
  "$PY" "$SUMMARIZE" "$dir/junit-merged.xml" || true
  exit "$rc"
fi
if [ "$rc2" -ne 0 ]; then
  echo "test.sh: упавшие файлы прошли, но перезапуск закончился с кодом $rc2 (сбой вне тестов) — падение настоящее"
  "$PY" "$SUMMARIZE" "$dir/junit-merged.xml" || true
  exit "$rc2"
fi

# check.py only shows the output tail on failure; also retain FLAKY in the board log.
mkdir -p "$BOARD_DIR/log" 2>/dev/null
echo "$merge_out" | grep '^FLAKY: ' | while IFS= read -r line; do
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $(basename "$PWD") $line" >> "$BOARD_DIR/log/flaky.log"
done
echo "test.sh: тесты прошли ТОЛЬКО после перезапуска упавших файлов (FLAKY выше): код 0"
"$PY" "$SUMMARIZE" "$dir/junit-merged.xml" || exit 1
exit 0
