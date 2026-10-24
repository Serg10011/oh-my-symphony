#!/bin/bash
# End-to-end run of the project kit (TASK-K08a, REPORT-11): project-new for a
# temporary code repo, the real symphony service, fake_claude.py as the agent.
# Usage: E2E_OUT=/tmp/kit-e2e ours/tests/e2e/run.sh
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
mkdir tests; printf 'def test_base():\n    assert True\n' > tests/test_base.py
echo readme > README.md; git add -A; git commit -qm base; git branch dev
echo "main only" >> README.md; git commit -qam "main moves"
cd "$R"
"$PY" "$FORK/ours/bin/project-new" demo --repo "$R/code" --base dev --prefix KIT --port 9981 --root "$R/projects"
B="$R/projects/demo"
cat > "$B/setup.sh" <<'S'
#!/bin/bash
echo "setup.sh: prepared $PWD"
S
cat > "$B/test.sh" <<T
#!/bin/bash
export PYTHONDONTWRITEBYTECODE=1
"$PY" -m pytest -q -p no:cacheprovider tests
T
symphony board new KIT-1 "Double a number" --root "$B/kanban" --state Todo \
  --description "Add double(x). Acceptance: AC1 tests/test_feature.py passes." >/dev/null
cd "$B"
symphony "$B/WORKFLOW.md" --no-progress-md --no-keep-awake > "$R/service.log" 2>&1 &
PID=$!
for i in $(seq 1 240); do
  st="$(sed -n 's/^state: *//p' "$B/kanban/KIT-1.md" | head -1)"
  if [ "$st" = "Human Review" ] && [ -s "$B/.ours-state/check.log" ]; then break; fi
  if [ "$st" = "needs_human" ] || [ "$st" = "Blocked" ]; then break; fi
  sleep 1
done
sleep 5
kill $PID; wait $PID 2>/dev/null || true
echo "final state: $(sed -n 's/^state: *//p' "$B/kanban/KIT-1.md" | head -1) (waited ${i}s); results in $R"
