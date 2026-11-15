---
name: skill-manager
description: Activate, deactivate, list, and inspect ai-helpers plugin skills for the current session on operator request; symlinks skills into the OpenCode skills directory so standing skill advertisements stay minimal. Use when the operator asks to list plugin categories or to enable or disable specific skills or collections.
---

# Skill Manager

Orchestrates which ai-helpers plugin skills (17 collections under
`/opt/ai-helpers`) are registered. Activation is a symlink and is LIVE
IMMEDIATELY — OpenCode re-scans skill sources at every model step, so
enabled skills become advertised and invocable on the next turn.

Run the script for every request; never manage the symlinks by hand
(paths are relative to this skill's base directory):

    bash scripts/skill-manager.sh <command> [args]

## Commands

- `list [category]` — index of collections with descriptions and counts,
  or one collection's skills with their descriptions
- `enable <spec>...` / `disable <spec>...` — a spec is a collection
  (`odh-jira`), a collection/skill (`odh-jira/jira-activity`), or a bare
  skill id (refused as ambiguous if it exists in multiple collections)
- `status` — the activated set with its context-cost ledger
- `reset` — deactivate every catalog skill

## Operating rules

- Confirm intent before a full-collection enable and report the skill
  count first: every activated skill is advertised to the model at each
  step — that is the context cost being traded.
- After any change, summarize what was activated or deactivated and offer
  `status` for the current ledger.
- Skill scripts under an activated skill resolve through the symlinked
  base directory even when the loader's file sampler does not list them.
- This manager harvests plugin.json metadata only; it does not implement
  Claude plugin runtime. Only catalog symlinks are ever created or
  removed — operator-maintained skills in the config directory are never
  modified.
