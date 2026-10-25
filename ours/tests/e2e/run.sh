#!/bin/bash
# End-to-end run of the project kit (TASK-K08a/K08b, REPORT-11/12): project-new
# for a temporary code repo, project-doctor (baseline + [timeouts]), the real
# symphony service, fake_claude.py as the agent, the web board read route.
# Usage: E2E_OUT=/tmp/kit-e2e ours/tests/e2e/run.sh
# Scenarios (fake_claude.py): E2E_BREAK_TEST=1, E2E_SKIP_WORK=1, E2E_DROP_TEST=1.
# Task levels (TASK-K14): the plain run checks that a card without `level` runs
# with the default level (--model haiku --effort medium). E2E_LEVELS=1 adds KIT-2
# (level: S1 -> --model sonnet --effort high) and KIT-3 (level: S9, unknown ->
# Blocked before any turn). E2E_NO_PROGRESS=1: the agent never advances the stage;
# with max_state_turns = 2 the card stops after the second turn, no third turn.
set -euo pipefail
E="$(cd "$(dirname "$0")" && pwd)"
FORK="$(cd "$E/../../.." && pwd)"
R="${E2E_OUT:?set E2E_OUT to an empty scratch directory}"
rm -rf "$R"; mkdir -p "$R/bin" "$R/home"; export E2E_DIR="$R" HOME="$R/home"
ln -s "$E/fake_claude.py" "$R/bin/claude"
export PATH="$R/bin:$PATH"
# The orchestrator's Python (symphony installed); also runs the project tests.
PY="$(command -v "${PYTHON:-python3}")"
# code repo
git init -q -b main "$R/code"; cd "$R/code"
git config user.email e2e@example.invalid; git config user.name e2e
mkdir tests; printf 'def test_base():\n    assert True\n\ndef test_base_two():\n    assert True\n' > tests/test_base.py
echo readme > README.md; git add -A; git commit -qm base; git branch dev
echo "main only" >> README.md; git commit -qam "main moves"
cd "$R"
"$PY" "$FORK/ours/bin/project-new" demo --repo "$R/code" --base dev --prefix KIT --port 9981 --root "$R/projects"
B="$R/projects/demo"
cat > "$B/setup.sh" <<'S'
#!/bin/bash
echo "setup.sh: prepared $PWD"
S
# test.sh contract: JUnit report outside the worktree, summary line last.
cat > "$B/test.sh" <<T
#!/bin/bash
export PYTHONDONTWRITEBYTECODE=1
report="\$(mktemp -t kit-junit.XXXXXX)"; trap 'rm -f "\$report"' EXIT
"$PY" -m pytest -q -p no:cacheprovider --junitxml="\$report" tests
rc=\$?
"\$SYMPHONY_PYTHON" "$FORK/ours/hooks/summarize.py" "\$report" || exit \$(( rc ? rc : 1 ))
exit \$rc
T
"$PY" "$FORK/ours/bin/project-doctor" demo --root "$R/projects" > "$R/doctor.log" 2>&1 \
  || { cat "$R/doctor.log"; exit 1; }
grep -E '^(Итог|PASS  test.sh)' "$R/doctor.log"
symphony board new KIT-1 "Double a number" --root "$B/kanban" --state Todo \
  --description "Add double(x). Acceptance: AC1 tests/test_feature.py passes." >/dev/null
WANT=1
if [ -n "${E2E_LEVELS:-}" ]; then
  WANT=2
  for n in 2 3; do
    symphony board new KIT-$n "Double a number ($n)" --root "$B/kanban" --state Todo \
      --description "Add double(x). Acceptance: AC1 tests/test_feature.py passes." >/dev/null
  done
  # level goes into the frontmatter of the finished card files (service not started yet);
  # awk, not sed -i: the same line must work with BSD sed on macOS.
  for pair in "2 S1" "3 S9"; do
    set -- $pair
    awk -v lvl="$2" '{ print } /^state: Todo$/ && !done { print "level: " lvl; done = 1 }' \
      "$B/kanban/KIT-$1.md" > "$B/kanban/KIT-$1.md.new" && mv "$B/kanban/KIT-$1.md.new" "$B/kanban/KIT-$1.md"
  done
  grep -q '^level: S1$' "$B/kanban/KIT-2.md" && grep -q '^level: S9$' "$B/kanban/KIT-3.md" \
    || { echo "FAIL: level not written into KIT-2/KIT-3"; exit 1; }
fi
cd "$B"
symphony "$B/WORKFLOW.md" --no-progress-md --no-keep-awake > "$R/service.log" 2>&1 &
PID=$!
for i in $(seq 1 240); do
  st="$(sed -n 's/^state: *//p' "$B/kanban/KIT-1.md" | head -1)"
  passes="$(grep -c '"outcome": "pass"' "$B/.ours-state/check.log" 2>/dev/null || true)"
  # Human Review counts once check.py passed it (a rollback logs a failure).
  # With E2E_LEVELS the unknown-level card must be blocked too (it is looked at
  # only when the single agent slot is free, so it comes last).
  st3="$(sed -n 's/^state: *//p' "$B/kanban/KIT-3.md" 2>/dev/null | head -1 || true)"
  if [ "$st" = "Human Review" ] && [ "${passes:-0}" -ge "$WANT" ] \
     && { [ -z "${E2E_LEVELS:-}" ] || [ "$st3" = "Blocked" ]; }; then break; fi
  if [ "$st" = "needs_human" ] || [ "$st" = "Blocked" ]; then break; fi
  sleep 1
done
sleep 5
# Web board read route (TASK-K10/K11): workspace.code_repo makes it list the
# symphony/<ID> branch of the code repository, not of the board folder.
curl -fsS "http://127.0.0.1:9981/api/v1/git/task-branches" > "$R/task-branches.json" \
  || { echo "FAIL: GET /api/v1/git/task-branches"; kill $PID; exit 1; }
"$PY" - "$R/task-branches.json" <<'P'
import json, sys
branches = {b["branch"] for b in json.load(open(sys.argv[1], encoding="utf-8"))["branches"]}
assert "symphony/KIT-1" in branches, f"symphony/KIT-1 not in {sorted(branches)}"
print("web board: /git/task-branches lists symphony/KIT-1 from the code repo")
P
# Let the service react to the last state change before it is stopped.
sleep 2
kill $PID; wait $PID 2>/dev/null || true
echo "final state: $(sed -n 's/^state: *//p' "$B/kanban/KIT-1.md" | head -1) (waited ${i}s); results in $R"
"$PY" - "$B/.ours-state/check.log" <<'P'
import json, sys
from pathlib import Path
# No check.log when no card reached Human Review (E2E_NO_PROGRESS).
for line in open(sys.argv[1], encoding="utf-8") if Path(sys.argv[1]).exists() else []:
    r = json.loads(line)
    print(f"check.log: {r['mode']} {r['outcome']} {r['reason'][:300]}")
P
# TASK-K14: levels and the max_state_turns watchdog, from the arguments the fake
# claude received (claude-calls.jsonl) and from the cards.
"$PY" - "$R/claude-calls.jsonl" "$B/kanban" "${E2E_LEVELS:-}" "${E2E_NO_PROGRESS:-}" <<'P'
import json, re, sys
from pathlib import Path

calls_file, board, levels, no_progress = sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.argv[4]
calls: dict[str, list[list[str]]] = {}
if Path(calls_file).exists():
    for line in open(calls_file, encoding="utf-8"):
        row = json.loads(line)
        calls.setdefault(row["id"], []).append(row["args"])

def flags(args):
    return args[args.index("--model") + 1], args[args.index("--effort") + 1], args.index("--model")

def expect(ident, model, effort):
    assert calls.get(ident), f"{ident}: no claude call"
    for args in calls[ident]:
        got_model, got_effort, pos = flags(args)
        assert (got_model, got_effort) == (model, effort), (ident, args)
        assert pos == 0, f"{ident}: flags must come right after the program: {args[:6]}"
        assert args.count("--model") == 1 and args.count("--effort") == 1, (ident, args)
    print(f"levels: {ident} {len(calls[ident])} call(s) with --model {model} --effort {effort}")

def state(ident):
    return re.search(r"(?m)^state:\s*(.*)$", (board / f"{ident}.md").read_text()).group(1).strip().strip("'\"")

expect("KIT-1", "haiku", "medium")  # no `level:` -> default_level L1
# The same level/model/effort in the service log (worker_turn_started) and in the
# run events (turn_started), for pricing tasks by level next to cost_usd.
import sqlite3
service_log = (Path(calls_file).parent / "service.log").read_text(encoding="utf-8")
started = [l for l in service_log.splitlines() if "worker_turn_started" in l and "identifier=KIT-1" in l]
assert started and all("task_level=L1 model=haiku effort=medium" in l for l in started), started[:2]
db = sqlite3.connect(str(board.parent / ".symphony" / "state.db"))
events = [json.loads(p) for (p,) in db.execute(
    "select payload_json from attempt_events where event_type = 'turn_started'")]
assert events and any(e.get("task_level") == "L1" and e.get("model") == "haiku"
                      and e.get("effort") == "medium" for e in events), events[:2]
print(f"levels: {len(started)} worker_turn_started log line(s) and {len(events)} turn_started run event(s) carry task_level/model/effort")
if levels:
    expect("KIT-2", "sonnet", "high")
    assert "KIT-3" not in calls, "KIT-3 (unknown level) must not start a turn"
    text = (board / "KIT-3.md").read_text()
    assert state("KIT-3") == "Blocked" and "## Level" in text and "level 'S9'" in text, text
    print("levels: KIT-3 (level S9) is Blocked with a ## Level note, no turn started")
if no_progress:
    assert len(calls["KIT-1"]) == 2, f"expected exactly 2 turns, got {len(calls['KIT-1'])}"
    assert state("KIT-1") == "needs_human", state("KIT-1")
    print("watchdog: 2 turns without progress, then needs_human; no third turn")
P
