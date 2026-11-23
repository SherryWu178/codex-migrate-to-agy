---
name: migrate-from-codex
description: Migrates an OpenAI Codex CLI setup (config.toml MCP servers, AGENTS.md, skills, prompts, custom agents, plugins, hooks, approval/sandbox rules) into Antigravity. Use when the user asks to migrate, import, move or port their Codex configuration to Antigravity or agy.
---

# Migrate from OpenAI Codex CLI

Move the user's OpenAI Codex CLI setup into Antigravity in four steps: discover, plan, apply, verify. Nothing is written until the user approves the plan.

The two scripts live in this skill's `scripts/` folder. Resolve that folder's absolute path from this file's location and use it below as `<SKILL_DIR>`. They require only Python 3.11+ and the standard library (`tomllib`).

## 1. Discover (read-only)

Ask the user which project folders to include. Default to the current workspace root. Then run:

```bash
python3 <SKILL_DIR>/scripts/discover.py --project <project-root> [--project <another>] > /tmp/codex-inventory.json
```

`discover.py` reads `$CODEX_HOME` (default `~/.codex`), including `config.toml`, `AGENTS.md`, `AGENTS.override.md`, `skills/`, `prompts/`, `agents/`, `plugins/` (including `plugins/cache/<marketplace>/<plugin>/<version>/`), `rules/`, and `hooks.json`, plus each project's `AGENTS.md`, `.codex/`, and `.agents/skills/`. It never opens `auth.json`, credential files, or session transcripts (`sessions/`, `archived_sessions/`, `history.jsonl`). It only counts sessions.

If `codex_dir_exists` is false and nothing was found, tell the user and stop.

## 2. Plan (dry run)

```bash
python3 <SKILL_DIR>/scripts/apply.py --inventory /tmp/codex-inventory.json
```

Show the user the three tables from the output in plain language:

- **Moves over**: what will be copied or converted, and where it goes.
- **You check**: items that need a human decision. Common ones:
  - MCP servers with `env_http_headers` or `bearer_token_env_var` (declared as Antigravity plugin `variables`, which must be filled in before enabling the plugin) or literal `env` vars that may hold secrets.
  - Project `AGENTS.md` files (already read natively by Antigravity — not duplicated).
  - Hooks (written as disabled `hooks.proposed.json` because event names and stdin/stdout contracts differ).
  - `sandbox_mode`, `approval_policy`, `profiles`, and `.rules` files (written to `MIGRATION_NOTES.md` as guidance only).
- **Stays behind**: chat history/sessions, `auth.json` credentials, and model/reasoning settings.

Explain that each source becomes its own Antigravity plugin (`codex-user`, `codex-<plugin>`, `codex-project`), so the user can turn any of them off with `agy plugin disable <name>`.

Then **end your turn**. Never run `--apply` in the same turn as the plan, even if the user asked for both, or asked for "plan only". Wait for a new message from the user that approves the plan. If they want to exclude something, edit `/tmp/codex-inventory.json` to remove it and re-run the dry run.

Pass `--project` only for real project folders. If the workspace is the home directory, skip `--project`, since `~/.codex` is already scanned at user scope.

## 3. Apply

Only after the user approves in a new message. The dry run prints a **Plan ID**. `--apply` refuses to write unless you pass the ID of the plan the user approved, so if anything changed, show the new plan first:

```bash
python3 <SKILL_DIR>/scripts/apply.py --inventory /tmp/codex-inventory.json --apply --plan-id <PLAN_ID>
```

Existing generated plugins are backed up with a `.bak-<timestamp>` suffix first. Re-running is safe.

Never do any of the following, even if asked to "just make it work":
- copy `auth.json` or any credential file
- convert session transcripts (`sessions/`, `history.jsonl`)
- rename `hooks.proposed.json` to `hooks.json`
- weaken Antigravity sandbox or approval settings automatically

The user does those after review.

## 4. Verify

Tell the user to restart `agy` (or start a new conversation in Antigravity), then run:

- `/plugin list`: the `codex-*` plugins should appear.
- `/context`: rules (`codex-user-memory.md`) and native project `AGENTS.md` should load.
- If any MCP server declared `variables`, remind the user to fill them in and enable the plugin.

Report only what those commands actually show. If something didn't load, check:
1. Whether the plugin folder has a valid `plugin.json` (run `agy plugin validate <plugin-dir>`).
2. Whether rule files start with `trigger:` frontmatter.
3. Whether the workspace is trusted.

## What maps where

| OpenAI Codex CLI | Antigravity |
|:-|:-|
| `~/.codex/AGENTS.md` (and `AGENTS.override.md`) | `codex-user/rules/codex-user-memory.md` (`trigger: always_on`) |
| `<project>/AGENTS.md` | Already read natively by Antigravity (left untouched, not duplicated) |
| `~/.codex/skills/<name>/` | `codex-user/skills/<name>/` |
| `~/.codex/prompts/<name>.md` | `codex-user/skills/<name>/SKILL.md` |
| `~/.codex/agents/<name>.md` or `.toml` | `codex-user/agents/<name>.md` (normalized frontmatter + safe tool list) |
| `~/.codex/plugins/` & `plugins/cache/` | `codex-<plugin>/` (skills, MCP, and hooks as a proposal) |
| `~/.codex/config.toml` `[mcp_servers.*]` | `codex-user/mcp_config.json` (`url`, `headers`, `variables` for env-backed tokens) |
| `<project>/.codex/config.toml` `[mcp_servers.*]` & `.codex/skills/` | `<project>/.agents/plugins/codex-project/` |
| `hooks.json` (user / project / plugin) | `hooks.proposed.json` (disabled; review and rewrite scripts first) |
| `sandbox_mode`, `approval_policy`, `profiles`, `~/.codex/rules/*.rules` | `MIGRATION_NOTES.md` (reference only, never applied automatically) |
