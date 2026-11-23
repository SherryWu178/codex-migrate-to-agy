# codex-migrate-to-agy

An [Antigravity](https://antigravity.google) plugin that migrates your **OpenAI Codex CLI** setup into Antigravity: `config.toml` MCP servers, `AGENTS.md` instructions, skills, prompts, custom agents, installed Codex plugins, hooks, and sandbox/rule notes. It shows a reviewable plan first and writes nothing until you approve it.

> [!NOTE]
> This is not an officially supported Google or OpenAI product.

## Install

```bash
agy plugin install https://github.com/SherryWu178/codex-migrate-to-agy
```

Then, in `agy` CLI or the Antigravity app:

```text
migrate my Codex setup
```

The `migrate-from-codex` skill takes it from there.

## How it works

1. **Discover (read-only).** Scans `$CODEX_HOME` (default `~/.codex`) and any `--project` folders you specify. It never opens `auth.json`, credential files, or session transcripts (`sessions/`, `archived_sessions/`, `history.jsonl`). It only counts sessions.
2. **Plan (dry run).** Prints three tables: what moves over, what you need to check, and what stays behind, along with a deterministic **Plan ID**.
3. **Apply (after you approve).** `--apply` refuses to write unless you pass the exact `--plan-id` from the dry run. Existing generated plugins are backed up with a `.bak-<timestamp>` suffix before being replaced. Re-running is safe.
4. **Verify.** Restart `agy` (or open a new conversation) and run `/plugin list` and `/context` to confirm what loaded.

Every source becomes its own toggleable Antigravity plugin so you can disable any piece with `agy plugin disable <name>`:

| Generated plugin | Location | Contains |
|:-|:-|:-|
| `codex-user` | `~/.gemini/config/plugins/` | User skills, prompts (as skills), subagents, MCP servers, and an always-on rule from `~/.codex/AGENTS.md` |
| `codex-<plugin>` | `~/.gemini/config/plugins/` | One plugin per installed Codex plugin (supports both `plugins/cache/` and flat layouts) |
| `codex-project` | `<project>/.agents/plugins/` | Project `.codex/skills/`, `.codex/prompts/`, `.codex/agents/`, and `.codex/config.toml` MCP servers |

Project `<project>/AGENTS.md` and `<project>/.agents/skills/` are already read natively by Antigravity, so they are left untouched and not duplicated.

## What maps where

| OpenAI Codex CLI | Antigravity | Notes |
|:-|:-|:-|
| `~/.codex/AGENTS.md` (+ `AGENTS.override.md`) | `codex-user/rules/codex-user-memory.md` | `trigger: always_on`; flags if combined size > 24 KB |
| `<project>/AGENTS.md` | Native (untouched) | Antigravity reads `AGENTS.md` natively |
| `~/.codex/skills/<name>/` | `codex-user/skills/<name>/` | Copied with symlinks resolved; missing frontmatter normalized |
| `~/.codex/prompts/<name>.md` | `codex-user/skills/<name>/SKILL.md` | `$ARGUMENTS` / `$1` rewritten to natural-language instructions |
| `~/.codex/agents/<name>.md` or `.toml` | `codex-user/agents/<name>.md` | Tool names translated to Antigravity native tools; unknown tools dropped |
| `~/.codex/plugins/` & `plugins/cache/` | `codex-<plugin>/` | One toggleable plugin per installed Codex plugin |
| `config.toml` `[mcp_servers.*]` (stdio) | `mcp_config.json` | `cwd` resolved to absolute path; redundant passthrough env vars dropped |
| `config.toml` `[mcp_servers.*]` (remote) | `mcp_config.json` | `url`, `http_headers`, `env_http_headers`, and `bearer_token_env_var` mapped with `variables` declarations |
| `hooks.json` | `hooks.proposed.json` (disabled) | Event names and stdin/stdout contracts differ; rewrite before activating |
| `sandbox_mode`, `approval_policy`, `profiles`, `.rules` | `MIGRATION_NOTES.md` | Reference notes only, never applied automatically |
| `auth.json`, `sessions/`, `history.jsonl`, `model` | Not moved | Credentials and chat transcripts are never opened |

## Run the scripts directly

Requires Python 3.11+ (standard library only, using `tomllib`):

```bash
S=~/.gemini/config/plugins/codex-migrate-to-agy/skills/migrate-from-codex/scripts
python3 "$S/discover.py" --project ~/code/my-service > /tmp/codex-inventory.json
python3 "$S/apply.py" --inventory /tmp/codex-inventory.json
python3 "$S/apply.py" --inventory /tmp/codex-inventory.json --apply --plan-id <PLAN_ID>
```

## Tests

```bash
python3 tests/run_stdlib.py
```

The test suite builds real Codex CLI directory layouts in temporary folders, runs `discover.py` and `apply.py`, and verifies the generated Antigravity plugins. A static check ensures the shipped scripts use only the Python standard library and contain no mock paths.

## Credits & Attribution

- Architecture, UX workflow (4-step Discover → Plan → Apply → Verify with `--plan-id` guard), and plugin-splitting pattern are inspired by and adapted from **[pauldatta/claude-migrate-to-agy](https://github.com/pauldatta/claude-migrate-to-agy)** by Paul Datta (Apache-2.0).
- Antigravity documentation: [Plugins](https://antigravity.google/docs/plugins), [Skills](https://antigravity.google/docs/skills), [Rules](https://antigravity.google/docs/rules), [Hooks](https://antigravity.google/docs/hooks), [Subagents](https://antigravity.google/docs/subagents).

## License

Apache-2.0
