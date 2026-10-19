# ADR 0006: Reduce bloat by removing duplication, not features

- **Status:** Accepted
- **Date:** 2026-10-20

## Context

The v1.0 PRD draft (v3) proposed a ≤2,200 LOC rewrite. At v0.27.0 the
package is 76,800 Python LOC and ships the PM Workspace, `symphony factory`,
chat intake, Windows service recovery, and Linear/Jira trackers. A rewrite to
that budget would remove them.

Earlier owner decisions (2026-02, previously in `docs/plans/minimal-symphony-plan.md`,
see the v0.27.0 tag) already fixed what stays:

1. All agent backends stay.
2. The governed `flow/` engine is deleted; board-level `blocked_by` is the one DAG.
3. The TUI stays.
4. Linear and Jira trackers stay in core.
5. The Document lane, `wiki_sweep`, and `docs/llm-wiki` stay.
6. i18n and continuous improvement stay.

## Decision

v1.0 targets bloat, defined as repository debris, duplicate entry points,
generated-artifact pipelines, and duplicate code paths. Shipped commands,
`WORKFLOW.md` keys, and API routes are not removed. The PM Workspace and
`symphony factory` join the list above. The plan and metrics live in
`docs/prd/v1.0-lean-core.md`.

## Consequences

Each phase lands as its own PR to `dev` with the full suite green. Historical
plans, quality reports, and handoffs leave the tree; the `v0.27.0` tag and
GitHub Releases keep them. Size targets are relative to the measured baseline,
not an absolute LOC budget.
