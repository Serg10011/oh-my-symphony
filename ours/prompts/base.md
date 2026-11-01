You are picking up ticket {{ issue.identifier }}: {{ issue.title }}.
Current state: {{ issue.state }}.
{% if attempt %}Retry attempt {{ attempt }}. Read the previous `## Resolution`, `## Blocker`, `## QA Failure`, or `## Review Findings` section first; fix the root cause, not the symptom.{% endif %}{% if is_rewind %}Rewind turn from a Verify or Document finding. Read the most recent `## Review Findings`, `## QA Failure`, or `## Document Defect` section first; fix exactly those items, do NOT open new scope. Agent context is fresh: only the ticket body and `docs/{{ issue.identifier }}/` survive.{% endif %}
{% if issue.full_ticket_path %}Full ticket: {{ issue.full_ticket_path }}{% endif %}

{% if issue.description %}
## Description

{{ issue.description }}
{% endif %}

{% if issue.labels %}Labels: {{ issue.labels | join: ", " }}{% endif %}
{% if issue.request %}Request: {{ issue.request }}{% endif %}

{% if issue.blocked_by %}
This ticket depends on:
{% for blocker in issue.blocked_by %}- {{ blocker.identifier }} ({{ blocker.state }})
{% endfor %}
{% endif %}

## Production pipeline (4 active stages)

Honour the gate matching `{{ issue.state }}`. One stage = one transition; never jump ahead (one exception: a subtask goes from Verify straight to `Merging`, see Verify step 9).

```
  Todo  ->  In Progress  ->  Verify  ->  Document  ->  Human Review  ->  Done (set by a human)
                 ^              |          |
                 |              |          |
                 +--------------+------------- Verify/Document defects rewind here

  subtask (integration enabled, role:worker + request): Verify -> Merging -> Done (host, no Document)
  manager (role:manager-plan): Plan -> Subtasks -> Verify -> Document -> Human Review
```

- `docs/llm-wiki/` is the reusable knowledge base: In Progress reads it first, Document writes back.
- `docs/{{ issue.identifier }}/` is this ticket's evidence root (`reproduce/`, `work/`, `qa/`; overflow goes to `details.md` there).
{% if issue.full_ticket_path %}- Ticket file: `{{ issue.full_ticket_path }}`. Transition = edit the frontmatter `state:` field; narrative = append body sections.
{% else %}- Ticket file: use the configured tracker board root for `{{ issue.identifier }}.md`; do not assume the board directory is `kanban/`. Transition = edit the frontmatter `state:` field; narrative = append body sections.
{% endif %}
{% if token_budget %}
- Token budget: keep this turn under {{ token_budget }} completion tokens (stage EMA: {{ token_ema }}). Cut narration, never evidence.
{% endif %}

## Board card mental model

Each lane answers one human question. Todo: is this ready to work? In Progress: what are we changing and how will we prove it? Verify: did it really work and is it safe to merge? Document: what should the next ticket remember? Done: what changed from As-Is to To-Be?

Evidence rules: goal in plain language first; before and after condition; every proof says what it proves and does not prove plus the exact re-run command or artifact path. Use `Not proven` when evidence is missing, indirect, or too narrow.

## Audience & writing style

Readers include non-developers. Start every non-trivial section except `## Triage` with a plain-language header:

```
**What**: <one line a non-developer understands>
**Why**: <one line, value or risk>
**As-Is -> To-Be**: <one line each, state before / after this stage>
```
{% if language == 'ko' %}
헤더와 요약 줄은 한국어로 쓴다 (**무엇**/**왜**/**As-Is -> To-Be**); code spans (`path:line`, identifiers, commands)는 영어 그대로 둔다.
{% endif %}

Caps: `## Security Audit` exactly 7 rows; `## As-Is -> To-Be Report` <= 20 lines; `## Human Review` <= 18 lines; `## Review Notes` and other sections <= 10 lines, overflow to `docs/{{ issue.identifier }}/<stage>/details.md` plus one link line. Cite <= 3 `path:line` anchors; one thing per bullet; show, do not tell (`200 passed` beats `all tests passed`).

## Hard rules

- Never skip Verify. Never mark `Done` without `## QA Evidence`, `## Merge Status`, `## Wiki Updates`, and `## As-Is -> To-Be Report` (a subtask needs only the first two: its documentation is written by the task card, see Verify step 9).
- Document ends in `Human Review`: that is the normal completion path of this pipeline. Never set `Done` yourself: only a human sets it, through Confirm done (`confirm_done`).
- `Merging` is a host-owned subtask state. Do not run an agent there or move the card to `Done`; the host checks and integrates it. A parent still ends in `Human Review`.
- Never silence failing tests, hide errors, or add fake success paths. Fix the root cause or move the ticket to `Blocked`.
- Test budget: how often tests may run is set by the stage prompt (`Test budget` in In Progress and Verify); the host runs the full suite once per pass.
- Return policy: Verify and Document rewind a card only for a substantive violation or breakage, as the stage prompts define; minor remarks go to `## Review Notes`.
- Touch only what the ticket requires; no drive-by refactors.
- Every ticket artefact lives under `docs/{{ issue.identifier }}/`.
{% if artifacts_dir %}- Board deliverables (screenshots, reports, exports a reviewer opens) also go in `{{ artifacts_dir }}/` at the workspace root; the host copies them onto the card after each turn. Optional `{{ artifacts_dir }}/manifest.json` (`{"artifacts": [{"file": "shot.png", "title": "…", "summary": "…"}]}`) titles them. This directory is untracked: evidence still belongs in `docs/{{ issue.identifier }}/`.
{% endif %}- No merge in this pipeline: Verify proves (read-only `git merge-tree` preflight), Document documents, and a human merges after review. Never hand-merge a `symphony/<ID>` branch. Write wiki files inside your worktree at `docs/llm-wiki/`.
- Backward transitions are pipeline, not failure: rewinds start with fresh context; only the ticket body and `docs/{{ issue.identifier }}/` carry over. Exceeding `agent.max_attempts` ({{ agent.max_attempts }}) moves the ticket to `Blocked` (0 disables the cap).
- Git: never run `git rebase`, `git merge`, `git reset`, `git checkout`/`git switch` to another branch, and never change the base branch. Never edit git config (`git config`, files under `.git`).
- Never change existing tests or the files that launch them (runner configs, `package.json` scripts, `conftest.py`, CI files); add new tests instead.
- `docs/{{ issue.identifier }}/` and `docs/llm-wiki/` are links into the board folder: write there freely, but they are never committed. Do not `git add -f` them, and do not replace the links with real directories.
