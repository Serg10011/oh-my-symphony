#!/usr/bin/env bash
# Keep OMS/test.sh's suite selection, serial timing pass and recorded exclusion.
set -euo pipefail
mode=${1:?}
expected=${2:?}
mkdir -p "$HOME"
git config --global user.name "Symphony Linux check"
git config --global user.email "check@localhost"
git config --global --add safe.directory /sources.git
git clone --quiet --depth 1 --branch "$mode" file:///sources.git /tmp/project
cd /tmp/project
test "$(git rev-parse HEAD)" = "$expected"
echo "CHECK_TREE $mode $(git rev-parse HEAD) $(git rev-parse 'HEAD^{tree}')"
python -m venv --system-site-packages .venv
export PATH="$PWD/.venv/bin:$PATH"
export PIP_NO_INDEX=1
python -m pip install --no-index --no-build-isolation --no-deps -e .
python -m pip check
python -c 'from pathlib import Path; import symphony; assert Path(symphony.__file__).resolve().is_relative_to(Path.cwd() / "src")'
python --version
node --version
python -m ruff check ours

junit=$(mktemp -d)
failed=0
run_suite() {
  local name=$1
  shift
  local status=0
  python -m pytest -q -p no:cacheprovider --tb=short --junitxml="$junit/$name.xml" "$@" \
    >"$junit/$name.log" 2>&1 || status=$?
  echo "CHECK_SUITE $mode $name exit=$status"
  tail -n 100 "$junit/$name.log"
  python -I /opt/check/summarize.py "$junit/$name.xml" || return 1
  if (( status != 0 )); then failed=1; fi
}
excluded=tests/test_pm_ui_node.py::test_pm_ui_node_regressions
run_suite main tests -n 4 -m "not timing" --deselect "$excluded"
run_suite timing tests -n 0 -m timing --deselect "$excluded"
run_suite ours ours/tests
echo "CHECK_TOTAL $mode"
python -I /opt/check/summarize.py "$junit/main.xml" "$junit/timing.xml" "$junit/ours.xml"
exit "$failed"
