### VERIFY — independent proof

Re-read the acceptance criteria and run every exact command declared under
`## Proof commands` (or `## Proof`) independently. Preserve each command
literally, including case and arguments; do not substitute an easier check or
a no-op. Missing environment or authority is a Blocker, never a claimed pass.
For a browser surface with attached `superqa`, run SuperQA in REGRESSION mode
and record its report path, pass/fail counts, and side-effect counts. A required
browser run must append `## Browser Verification` with a
`report | passed | failed | side effects` table. Copy the nonempty SuperQA
report into this worktree, use its relative path, and report its actual counts.
The report must come from this Verify phase, with passed > 0, failed = 0,
and side effects >= 0 before success. Explain any side effects after the table.
A required behavior or regression defect rewinds this ticket. Record a non-blocking UX or
product improvement as a follow-up Wayfinder ticket; do not widen this ticket.
Append `## Verification` with a `criterion | command | result` table. Include
exactly one row for each bullet or numbered item under `## Acceptance criteria`.
Copy the complete item wording into the criterion cell (Markdown formatting may
differ). Do not split one item into multiple rows or merge multiple items into
one row. Name the exact relevant test or proof command, and make the result cell
exactly `pass`; every declared proof command must appear in the table. When
one criterion needs several commands, put each in its own inline-code span
in that row's command cell, separated by `<br>` or semicolons. Escape shell
pipes as `\|` inside Markdown table cells. Put
fresh command output, the current UTC verification timestamp, and supporting
detail after the table in the same Verification section. Rewrite that section
for this run even when the command rows are unchanged; old evidence is stale.
A bare claimed pass without the named command is not evidence. Record remaining risk
after the table. If every row passes, set state to `Done`. If a product defect exists,
append `## QA Failure` with expected versus actual evidence and set state to
`Build`. If proof needs missing authority or environment, append `## Blocker`
and set state to `Blocked`.
