#!/bin/bash
# End-to-end run of the project kit (TASK-K08a/K08b, REPORT-11/12): project-new
# for a temporary code repo, project-doctor (baseline + [timeouts]), the real
# symphony service, fake_claude.py as the agent, the web board read route.
# Usage: E2E_OUT=/tmp/kit-e2e ours/tests/e2e/run.sh
# Scenarios (fake_claude.py): E2E_BREAK_TEST=1, E2E_SKIP_WORK=1, E2E_DROP_TEST=1.
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
cd "$B"
symphony "$B/WORKFLOW.md" --no-progress-md --no-keep-awake > "$R/service.log" 2>&1 &
PID=$!
for i in $(seq 1 240); do
  st="$(sed -n 's/^state: *//p' "$B/kanban/KIT-1.md" | head -1)"
  # Human Review counts once check.py passed it (a rollback logs a failure).
  if [ "$st" = "Human Review" ] && grep -q '"outcome": "pass"' "$B/.ours-state/check.log" 2>/dev/null; then break; fi
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
kill $PID; wait $PID 2>/dev/null || true
echo "final state: $(sed -n 's/^state: *//p' "$B/kanban/KIT-1.md" | head -1) (waited ${i}s); results in $R"
"$PY" - "$B/.ours-state/check.log" <<'P'
import json, sys
for line in open(sys.argv[1], encoding="utf-8"):
    r = json.loads(line)
    print(f"check.log: {r['mode']} {r['outcome']} {r['reason'][:300]}")
P
