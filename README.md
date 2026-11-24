# codex-migrate-to-agy

An [Antigravity](https://antigravity.google) plugin that migrates your **OpenAI Codex CLI** setup into Antigravity: `config.toml` MCP servers, `AGENTS.md` instructions, skills, prompts, custom agents, installed Codex plugins, hooks, and sandbox/rule notes. It shows a reviewable plan first and writes nothing until you approve it. Anything you disabled in Codex (`[[skills.config]]`, `[plugins.*]`) stays disabled.

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

1. **Discover (read-only).** Scans `$CODEX_HOME` (default `~/.codex`) and any `--project` folders you specify. It never opens `auth.json`, `.env`, OAuth/token stores, session transcripts (`sessions/`, `archived_sessions/`, `history.jsonl`), or `memories/`. It only counts them. It also runs `codex --version` (5 s timeout, honours `$CODEX_BIN`; skip with `--no-exec`) so the plan can tell you which Codex generation you are migrating from.
2. **Plan (dry run).** Prints three tables: what moves over, what you need to check, and what stays behind, along with a deterministic **Plan ID**. If `config.toml` trusts projects you did not pass with `--project`, the plan tells you so you can re-run discovery.
3. **Apply (after you approve).** `--apply` refuses to write unless you pass the exact `--plan-id` from the dry run. Existing generated plugins are backed up with a `.bak-<timestamp>` suffix before being replaced. Re-running is safe.
4. **Verify.** Restart `agy` (or open a new conversation) and run `/plugin list` and `/context` to confirm what loaded.

### Codex versions

Codex's config format has changed several times (TypeScript-era `config.json` → Rust-era `config.toml` with inline `[profiles.*]` → per-file `~/.codex/<name>.config.toml` profile overlays, `[model_providers.*.auth]` credential helpers, and so on). The tool **detects the installed version for reporting**, but **parses by feature detection**: every config generation physically present is read and labelled with its source, because real machines routinely carry leftovers from older releases. The only version-based logic is a mismatch warning (e.g. a legacy 0.1.x binary alongside a Rust-era `config.toml`), never a decision to skip data.

Every source becomes its own toggleable Antigravity plugin so you can disable any piece with `agy plugin disable <name>`:

| Generated plugin | Location | Contains |
|:-|:-|:-|
| `codex-user` | `~/.gemini/config/plugins/` | User skills, prompts (as skills), subagents, MCP servers, and an always-on rule built from `~/.codex/AGENTS.md`, legacy `instructions.md`, and `model_instructions_file` |
| `codex-<plugin>` | `~/.gemini/config/plugins/` | One plugin per *enabled* installed Codex plugin (supports both `plugins/cache/` and flat layouts) |
| `codex-project` | `<project>/.agents/plugins/` | Project `.codex/skills/`, `.codex/prompts/`, `.codex/agents/`, `.codex/config.toml` MCP servers, and — only when the project has no root `AGENTS.md` — an always-on rule built from `project_doc_fallback_filenames` (e.g. `CLAUDE.md`) or legacy `codex.md` |

Project `<project>/AGENTS.md` and `<project>/.agents/skills/` are already read natively by Antigravity, so they are left untouched and not duplicated. Nested `AGENTS.md` files in subdirectories (which Codex concatenates) are listed in the plan for you to review.

## What maps where

| OpenAI Codex CLI | Antigravity | Notes |
|:-|:-|:-|
| `~/.codex/AGENTS.md` (+ `AGENTS.override.md`) | `codex-user/rules/codex-user-memory.md` | `trigger: always_on`; flags if combined size > 24 KB |
| `~/.codex/instructions.md` (legacy TS CLI), `model_instructions_file` | `codex-user/rules/codex-user-memory.md` | Merged into the same rule with a source comment; flagged for dedupe/review |
| `<project>/AGENTS.md` | Native (untouched) | Antigravity reads `AGENTS.md` natively |
| `<project>/**/AGENTS.md` (nested) | Plan "You check" | Codex concatenated these; confirm Antigravity's behaviour or merge into root |
| `<project>/CLAUDE.md` etc. (`project_doc_fallback_filenames`), legacy `codex.md` | `codex-project/rules/codex-project-instructions.md` | Only when root `AGENTS.md` is absent (mirrors Codex's own fallback logic) |
| `~/.codex/skills/<name>/` | `codex-user/skills/<name>/` | Copied with symlinks resolved; missing frontmatter normalized; skipped if `[[skills.config]] enabled = false`; `agents/openai.yaml` `allow_implicit_invocation: false` flagged |
| `~/.codex/prompts/<name>.md` | `codex-user/skills/<name>/SKILL.md` | `$ARGUMENTS` / `$1` rewritten to natural-language instructions |
| `~/.codex/agents/<name>.md` or `.toml` | `codex-user/agents/<name>.md` | Tool names translated to Antigravity native tools; unknown tools dropped |
| `~/.codex/plugins/` & `plugins/cache/` | `codex-<plugin>/` | One toggleable plugin per installed Codex plugin; skipped if `[plugins."name@marketplace"] enabled = false`; `.app.json` connectors reported as not portable |
| `config.toml` `[mcp_servers.*]` (stdio) | `mcp_config.json` | `cwd` resolved to absolute path; redundant passthrough env vars and `env_vars` lists dropped; `enabled_tools` / `disabled_tools` flagged **SECURITY** (not enforced by Antigravity); timeouts noted |
| `config.toml` `[mcp_servers.*]` (remote) | `mcp_config.json` | `url`, `http_headers`, `env_http_headers`, and `bearer_token_env_var` mapped with `variables` declarations |
| `hooks.json`, `config.toml` `notify` | `hooks.proposed.json` (disabled) | Event names and stdin/stdout contracts differ; `notify` becomes a proposed `Stop` hook; rewrite before activating |
| `[model_providers.*]` (incl. `[model_providers.*.auth]` helpers) | `MIGRATION_NOTES.md` | `base_url`, `env_key`, `wire_api`, helper command recorded; header/query-param **values** and helper output never copied |
| `[profiles.*]` (inline) and `~/.codex/<name>.config.toml` (per-file) | `MIGRATION_NOTES.md` | Both styles recorded with `_source`; flagged if both exist |
| `sandbox_mode`, `[sandbox_workspace_write]`, `approval_policy`, `[shell_environment_policy]`, `[features]`, `[tools]`, `[history]`, `.rules` | `MIGRATION_NOTES.md` | Reference notes only, never applied automatically |
| `[projects."/path"] trust_level` | Plan "You check" | Trusted projects you did not pass via `--project` are listed so you can re-run discovery |
| `~/.codex/config.json` (legacy TS CLI) | `MIGRATION_NOTES.md` | `model`, `provider`, `approvalMode`, provider names/baseURL/envKey recorded; superseded by `config.toml` |
| `/etc/codex/*.toml`, `~/.codex/requirements.toml` (managed layers) | Plan "You check" | Presence reported; your effective Codex settings may differ from the user layer |
| `plugins/marketplaces/`, `.agents/plugins/marketplace.json` | Plan "Stays behind" | Antigravity installs plugins by URL; names listed for reinstall |
| `auth.json`, `.env`, `*oauth*`/`*token*` files, `sessions/`, `history.jsonl`, `memories/`, `model` | Not moved | Credentials, transcripts, and agent memories are never opened |

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
