# AGENTS.md: Codex CLI entry point

This repository is a **Symphony-managed project**. Symphony
(`oh-my-symphony`) is a separate control-plane checkout that dispatches coding
agents (Codex / Claude Code / Gemini / AGY / Kiro / OpenCode / Pi / Prime
Agent) at the Kanban board in this repository. This file is the discovery
point that Codex (and any other `AGENTS.md`-respecting CLI) reads on startup.

## What Symphony added here

- `WORKFLOW.md`: board states, agent backend, hooks, and prompt routing.
- `kanban/`: one Markdown file per ticket; the `state:` frontmatter is the
  board column.
- `docs/symphony-prompts/`: stage prompts rendered into each worker turn.
- `scripts/symphony-setup-worktree.sh`: provisions the per-ticket
  `symphony/<ID>` worktree.
- `skills/symphony-skill/`: the operator skill (routing, troubleshooting,
  customization). `.claude/skills/` is a thin symlink layer for Claude Code.
  Edit the canonical files under `skills/`.

Everything else in this repository is the product. Treat it as the user's
code, not as Symphony's.

## Operator-facing skill

Load `skills/symphony-skill/SKILL.md` when the user asks to add, list, or
move tickets, launch the TUI or web board, inspect orchestrator state,
customize lanes or per-state prompts, delegate sub-tasks, one-shot a prompt
into an evidence-gated board, or diagnose dispatch failures. Open only the
reference page named by the router's decision table.

## Worker-side guidance

Dispatched workers (the agent CLI running inside a per-ticket worktree) do
**not** consume the operator skill. Worker behavior comes from
`WORKFLOW.md`'s `prompts.base` + `prompts.stages` map, rendered from
`docs/symphony-prompts/<flavor>/`. Every backend receives the same rendered
prompt for a given ticket state.

## Conventions

- Read `WORKFLOW.md` and a couple of `kanban/*.md` files before any
  recommendation. Settings vary per project.
- Run `symphony doctor ./WORKFLOW.md` before launching anything.
- Never hard-reset a worker worktree between turns; turns are committed in
  place and a reset discards in-progress work.
