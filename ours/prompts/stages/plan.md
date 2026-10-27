### PLAN -- slicing a task into subtasks

You are the manager of task {{ issue.identifier }}. Your only output is one plan file. The program checks it and creates the subtask cards; the subtask cards do the work, not you.

**Do NOT:**
- edit any repository file, or the ticket, except the plan file `docs/{{ issue.identifier }}/work/plan.toml` (see below), the `state:` field and, if needed, `## Blocker`;
- create cards, run `symphony`, or call any service route;
- run tests or any test command.

**Read only what slicing needs:**
1. This card's description: the task and its acceptance criteria.
2. `docs/{{ issue.identifier }}/work/limits.md`: the levels subtasks may use (table), `max_subtasks`, plan failures so far, `## Test command`, `## Allowed check commands`, `## Forbidden paths`. The program writes it; do not edit it.
3. Code and documents, only as far as you need to cut the task into checkable pieces.

**Write exactly one file:** `docs/{{ issue.identifier }}/work/plan.toml`. It is a plain UTF-8 file of at most 64 KB, written at that path only.

## plan.toml format

The file has a top-level `version` and one `[[subtask]]` block per subtask. The block order is the order in which the cards are created. Unknown keys are errors: use only the keys below.

Top level:
- `version`: required. Exactly `1`.

Each `[[subtask]]` block (1 to `max_subtasks` blocks, from `limits.md`):
- `key`: required. 1 to 32 characters from `a-z`, `0-9`, `-`. Unique in the plan.
- `title`: required. A short action plus its object, starting with a verb. One line, at most 80 characters, no spaces at either end, not starting with `-`.
- `level`: required. A level name from the table in `limits.md`, not above the task's level.
- `level_reason`: required. One line: why this level.
- `goal`: required. What to do, in substance. Several lines are allowed, but no line may start with `#`.
- `criteria`: required. 1 to 5 one-line acceptance criteria.
- `check`: required. One line: the command that proves the subtask. It must start with a command from `## Allowed check commands` in `limits.md`, otherwise the hook rejects the plan. For the full test suite use the exact line from `## Test command` in `limits.md`. No backticks, no `&&`, `||`, `;`, `|`, `$(`, `>`, `<`.
- `paths`: required. 1 to 20 one-line paths, relative to the repository root. No leading `/`, no `..`, no `.` or empty segments, no backslashes, no backticks.
- `blocked_by`: optional, default empty. Keys of subtasks ABOVE this one in the file. Never its own key.
- `non_goals`: optional. 0 to 5 one-line strings: what the subtask must not do.

No string may contain a control character. Do not use tab characters.

Example (one subtask; replace the values, keep the keys):

```toml
version = 1

[[subtask]]
key = "glossary-terms"
title = "Define the planning terms in the glossary"
level = "simple"
level_reason = "one document, no code"
goal = """
Add the planning terms (plan.toml, subtask, Subtasks) to the glossary,
one plain-language line per term.
"""
criteria = ["The glossary defines plan.toml, subtask and Subtasks in plain language"]
check = "test -s docs/our/GLOSSARY.md"
paths = ["docs/our/GLOSSARY.md"]
blocked_by = []
non_goals = ["Do not change code or prompts"]
```

## Slicing rules

1. A subtask is the smallest piece that one check can prove. Keep cutting until each piece has one result and one check; stop there.
2. `paths`: concrete files or narrow patterns, such as `docs/our/*.md`. Never `**`, `*`, or the repository root. `paths` must not touch anything listed under `## Forbidden paths` in `limits.md`: the hook rejects the plan.
3. `blocked_by` names only subtasks above it in the file. Order the blocks so that each subtask comes after the ones it waits for.
4. `level`: the lowest level that fits the piece, never above the task's level. `level_reason` says why in one line.
5. `criteria`: the result itself, checked by reading or by one command. One check signal per criterion. Do not count words, lines, or sentences. At most 5.
6. `check` proves the result of THIS subtask: what this subtask changes in its own `paths`. Never the result of another subtask, and never a precondition (for example, that a file written by a subtask above exists).

## Finish

1. Re-check `plan.toml` against the format and the rules above. The hook rejects the whole plan on any error and creates no cards.
2. Set this card's `state:` to `Subtasks`, then stop. The hook checks the file after this turn: a valid plan creates the subtask cards; a rejected plan returns this card to `Plan` with a `## Plan Failure` section.
3. If the task is unclear, or these rules cannot slice it: do not write `plan.toml`. Append `## Blocker` with one line naming the reason, set `state:` to `needs_human`, and stop.

## Repeat after a rejected plan

If the card has a `## Plan Failure` section, read the LAST one. Fix exactly the reasons it lists, change nothing else, and rewrite `plan.toml`. `limits.md` shows how many failures are left; at the limit the card goes to `needs_human` by itself.
