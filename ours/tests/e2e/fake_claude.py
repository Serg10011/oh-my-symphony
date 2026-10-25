#!/usr/bin/env python3
"""Fake `claude -p --output-format stream-json` for run.sh (TASK-K08a).

Plays one stage of the standard pipeline per turn: reads the card through
the workspace `kanban` link, writes code, artifacts (docs/<ID>, a link into
the board folder) and wiki, moves the state, prints stream-json frames.
E2E_BREAK_TEST=1 also weakens an existing test; E2E_SKIP_WORK=1 skips the
work notes the In Progress contract requires; E2E_DROP_TEST=1 drops the
existing tests from the run through a runner config (pytest.ini), without
touching any test file; E2E_NO_PROGRESS=1 never moves the card (the
max_state_turns watchdog must stop it). `--version` (project-doctor) prints a
version. Every call logs its arguments to claude-calls.jsonl.
"""

import json
import os
import re
import sys
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print("fake-claude 0.0 (e2e)")
    sys.exit(0)
log = Path(os.environ["E2E_DIR"]) / "claude-calls.jsonl"
prompt = sys.stdin.read()
ws = Path.cwd()
ident = ws.name
sid = f"sess-{ident}"
if "--resume" in args:
    sid = args[args.index("--resume") + 1]
card = ws / "kanban" / f"{ident}.md"
text = card.read_text()
state = re.search(r"(?m)^state:\s*(.*)$", text).group(1).strip().strip("'\"")
docs = ws / "docs" / ident
with log.open("a") as fh:
    fh.write(json.dumps({"id": ident, "state": state, "args": args, "cwd": str(ws)}) + "\n")

def move(new_state, sections):
    global text
    text = re.sub(r"(?m)^state:.*$", f"state: {new_state}", text, count=1)
    text = text.rstrip() + "\n\n" + sections.strip() + "\n"
    card.write_text(text)

s = state.lower()
if os.environ.get("E2E_NO_PROGRESS"):
    s = "no-progress"  # no branch below matches: the card stays where it is
if s == "todo":
    move("In Progress", "## Triage\n\nticket is actionable; routing to In Progress because it has acceptance criteria")
elif s == "in progress":
    (ws / "src").mkdir(exist_ok=True)
    (ws / "src" / "feature.py").write_text("def double(x):\n    return 2 * x\n")
    (ws / "tests" / "test_feature.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parents[1] / 'src'))\n"
        "from feature import double\n\ndef test_double():\n    assert double(2) == 4\n")
    if not os.environ.get("E2E_SKIP_WORK"):
        (docs / "work" / "notes.md").write_text("work notes\n")
    if os.environ.get("E2E_DROP_TEST"):
        (ws / "pytest.ini").write_text("[pytest]\naddopts = --ignore=tests/test_base.py\n")
    if os.environ.get("E2E_BREAK_TEST"):
        (ws / "tests" / "test_base.py").write_text("def test_base():\n    pass  # weakened\n")
    move("Verify", """## Plan

double a number

## Acceptance Tests

- AC1: `tests/test_feature.py`

## Done Signals

tests green

## Difficulty

trivial

## Implementation

src/feature.py

## Self-Critique

none

## Pipeline Route

Verify""")
elif s == "verify":
    (docs / "qa" / "tests.log").write_text("1 passed\n")
    (docs / "qa" / "security.md").write_text("no secrets\n")
    (docs / "qa" / "merge-tree.log").write_text("clean\n")
    rows = "\n".join(f"| {c} | n/a | not applicable |" for c in
                     ("input-validation", "injection", "xss", "csrf", "authz", "rate-limit"))
    move("Document", f"""## Security Audit

| check | result | evidence |
|---|---|---|
| secrets | pass | `qa/security.md` |
{rows}

## Review

clean: diff matches the plan

## QA Evidence

pytest: exit 0, `qa/tests.log`

## AC Scorecard

| AC | signal | source | result | evidence |
|---|---|---|---|---|
| AC1 | test passes | pytest | pass | `qa/tests.log` |

## Merge Status

preflight clean against dev; Merge deferred to a human by pipeline rule; no merge performed.""")
elif s == "document":
    wiki = ws / "docs" / "llm-wiki"
    (wiki / "INDEX.md").write_text("| topic | file |\n|---|---|\n| double | double.md |\n")
    (wiki / "double.md").write_text("double() lives in src/feature.py\n")
    move("Human Review", """## Learnings

- small

## Wiki Updates

- docs/llm-wiki/double.md created

## As-Is -> To-Be Report

### Goal
double numbers
### As-Is
none
### To-Be
double()
### Reasoning
simple
### Evidence
`qa/tests.log`
### Not Covered
none
### How To Re-run
pytest""")

def out(obj):
    print(json.dumps(obj), flush=True)
out({"type": "system", "subtype": "init", "session_id": sid, "model": "fake"})
out({"type": "assistant", "session_id": sid,
     "message": {"content": [{"type": "text", "text": f"{ident}: {state} done"}]}})
out({"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
     "result": f"{ident}: handled {state}", "usage": {"input_tokens": 10, "output_tokens": 5},
     "total_cost_usd": 0.0})
