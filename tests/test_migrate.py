"""End-to-end tests building real OpenAI Codex CLI directory layouts in temp dirs."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "migrate-from-codex" / "scripts"
DISCOVER = SCRIPTS / "discover.py"
APPLY = SCRIPTS / "apply.py"


def run_py(script: Path, args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def build_realistic_codex_env(tmp: Path) -> tuple[Path, Path]:
    home = tmp / "home"
    codex = home / ".codex"
    project = tmp / "my-service"

    # Credentials and sessions (must NEVER be read or copied)
    codex.mkdir(parents=True)
    (codex / "auth.json").write_text('{"OPENAI_API_KEY": "sk-SECRET-DO-NOT-COPY"}\n', encoding="utf-8")
    (codex / ".env").write_text("OPENAI_API_KEY=sk-DOTENV-SECRET\n", encoding="utf-8")
    (codex / "mcp_oauth_tokens.json").write_text('{"tok": "OAUTH-SECRET"}\n', encoding="utf-8")
    sessions = codex / "sessions" / "2026" / "11"
    sessions.mkdir(parents=True)
    (sessions / "rollout-1.jsonl").write_text('{"role":"user","text":"PRIVATE TRANSCRIPT"}\n', encoding="utf-8")
    (codex / "history.jsonl").write_text('{"text":"PRIVATE HISTORY"}\n', encoding="utf-8")
    (codex / "memories").mkdir()
    (codex / "memories" / "2026-11-01.md").write_text("PRIVATE MEMORY\n", encoding="utf-8")

    # Custom system-prompt file referenced by model_instructions_file
    (codex / "my-system-prompt.md").write_text("# System Prompt\nBe terse and cite files.\n", encoding="utf-8")

    # Legacy TS-era files
    (codex / "instructions.md").write_text("# Legacy Global\nPrefer small diffs.\n", encoding="utf-8")
    (codex / "config.json").write_text(
        json.dumps({
            "model": "o4-mini",
            "provider": "azure",
            "approvalMode": "suggest",
            "providers": {"azure": {"name": "Azure", "baseURL": "https://x.openai.azure.com", "envKey": "AZURE_KEY"}},
        }),
        encoding="utf-8",
    )

    # User config.toml
    (codex / "config.toml").write_text(
        f"""
model = "gpt-5.3-codex"
model_provider = "my_gateway"
model_reasoning_effort = "high"
approval_policy = "on-request"
sandbox_mode = "workspace-write"
model_instructions_file = "{codex / 'my-system-prompt.md'}"
project_doc_fallback_filenames = ["CLAUDE.md"]
notify = ["python3", "/usr/local/bin/notify.py"]

[sandbox_workspace_write]
writable_roots = ["/tmp/scratch"]
network_access = true

[shell_environment_policy]
inherit = "core"
set = {{ CI = "1" }}

[features]
codex_hooks = true

[model_providers.my_gateway]
name = "My Gateway"
base_url = "https://gw.example.com/v1"
env_key = "GW_API_KEY"
wire_api = "responses"
http_headers = {{ "X-Static-Token" = "LITERAL-HEADER-SECRET" }}
env_http_headers = {{ "X-Org" = "GW_ORG" }}

[profiles.fast]
model = "gpt-5.3-codex-mini"
approval_policy = "never"

[projects."{tmp / 'my-service'}"]
trust_level = "trusted"

[projects."{tmp / 'other-trusted'}"]
trust_level = "trusted"

[[skills.config]]
path = "{codex / 'skills' / 'noisy-skill'}"
enabled = false

[plugins."dead-plugin@official"]
enabled = false

[mcp_servers.local_db]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-postgres"]
cwd = "~/work"
startup_timeout_sec = 30
env = {{ PGURL = "postgres://localhost/dev", PATH = "${{PATH}}" }}
env_vars = ["HOME", "PGPASSFILE"]
enabled_tools = ["query"]
disabled_tools = ["drop_table"]

[mcp_servers.remote_api]
url = "https://mcp.example.com/v1"
bearer_token_env_var = "EXAMPLE_API_TOKEN"
http_headers = {{ "X-Team" = "platform" }}
env_http_headers = {{ "X-Org-Key" = "ORG_KEY" }}

[mcp_servers.disabled_srv]
command = "echo"
enabled = false
""".strip() + "\n",
        encoding="utf-8",
    )
    (tmp / "other-trusted").mkdir()

    # Per-file profile overlay (newer Codex) with a credential-helper provider
    (codex / "work.config.toml").write_text(
        """
model = "gpt-5.6"
model_provider = "openai-api"

[model_providers.openai-api]
name = "OpenAI with Custom API Key"
base_url = "https://api.openai.com/v1"

[model_providers.openai-api.auth]
command = "/bin/bash"
args = ["-c", "cat ~/.codex/api-key.txt"]
""".strip() + "\n",
        encoding="utf-8",
    )
    (codex / "api-key.txt").write_text("sk-HELPER-SECRET\n", encoding="utf-8")

    # version.json only knows the *latest* release, not the installed one
    (codex / "version.json").write_text(json.dumps({"latest_version": "0.200.0", "last_checked_at": "2026-11-01T00:00:00Z"}), encoding="utf-8")

    # Stub `codex` binary so version detection is exercised hermetically
    bindir = tmp / "bin"
    bindir.mkdir()
    stub = bindir / "codex"
    stub.write_text("#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo 'codex-cli 0.143.0'; exit 0; fi\nexit 1\n", encoding="utf-8")
    stub.chmod(0o755)

    # User AGENTS.md
    (codex / "AGENTS.md").write_text("# Global Codex Rules\nAlways run unit tests.\n", encoding="utf-8")

    # User skill (missing frontmatter description on purpose to test normalization)
    sk = codex / "skills" / "deploy-check"
    (sk / "scripts").mkdir(parents=True)
    (sk / "SKILL.md").write_text("# Deploy Check\nVerify staging health before releasing.\n", encoding="utf-8")
    (sk / "scripts" / "check.sh").write_text("#!/bin/sh\necho ok\n", encoding="utf-8")

    # Skill disabled via [[skills.config]] — must NOT be migrated
    noisy = codex / "skills" / "noisy-skill"
    noisy.mkdir(parents=True)
    (noisy / "SKILL.md").write_text("---\nname: noisy-skill\ndescription: Too chatty\n---\nTalk a lot.\n", encoding="utf-8")

    # Skill with openai.yaml policy disallowing implicit invocation
    quiet = codex / "skills" / "manual-only"
    (quiet / "agents").mkdir(parents=True)
    (quiet / "SKILL.md").write_text("---\nname: manual-only\ndescription: Only when asked\n---\nDo it.\n", encoding="utf-8")
    (quiet / "agents" / "openai.yaml").write_text("policy:\n  allow_implicit_invocation: false\n", encoding="utf-8")

    # User prompt (slash command)
    prompts = codex / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "pr-review.md").write_text(
        "---\ndescription: Review a pull request\n---\nReview $ARGUMENTS for edge cases and security issues.\n",
        encoding="utf-8",
    )

    # User custom subagent (.toml)
    agents = codex / "agents"
    agents.mkdir(parents=True)
    (agents / "sec-auditor.toml").write_text(
        """
name = "sec-auditor"
description = "Audit diffs for security issues"
model = "gpt-5.3-codex"
sandbox_mode = "read-only"
tools = ["read_file", "grep", "shell", "unknown_codex_tool"]
developer_instructions = "Inspect all auth and input validation code."
""".strip() + "\n",
        encoding="utf-8",
    )

    # Installed Codex plugin in cache layout
    plug = codex / "plugins" / "cache" / "official" / "linear-kit" / "1.2.0"
    (plug / ".codex-plugin").mkdir(parents=True)
    (plug / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "linear-kit", "version": "1.2.0"}),
        encoding="utf-8",
    )
    (plug / "skills" / "triage").mkdir(parents=True)
    (plug / "skills" / "triage" / "SKILL.md").write_text(
        "---\nname: triage\ndescription: Triage Linear issues\n---\nCheck priority and assignee.\n",
        encoding="utf-8",
    )
    (plug / "mcp.json").write_text(
        json.dumps({"mcpServers": {"linear": {"url": "https://mcp.linear.app/mcp"}}}),
        encoding="utf-8",
    )
    (plug / ".app.json").write_text('{"connector": "linear"}', encoding="utf-8")

    # Plugin disabled in config.toml [plugins] — must NOT be migrated
    dead = codex / "plugins" / "cache" / "official" / "dead-plugin" / "0.1.0"
    (dead / ".codex-plugin").mkdir(parents=True)
    (dead / ".codex-plugin" / "plugin.json").write_text(json.dumps({"name": "dead-plugin"}), encoding="utf-8")
    (dead / "skills" / "zombie").mkdir(parents=True)
    (dead / "skills" / "zombie" / "SKILL.md").write_text("---\nname: zombie\ndescription: x\n---\nx\n", encoding="utf-8")

    # Cloned marketplace repo (should be reported, never migrated)
    (codex / "plugins" / "marketplaces" / "official").mkdir(parents=True)

    # User hooks & .rules
    (codex / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "shell", "command": "echo check"}]}}),
        encoding="utf-8",
    )
    rules = codex / "rules"
    rules.mkdir(parents=True)
    (rules / "default.rules").write_text('prefix_rule(pattern=["git", "status"], decision="allow")\n', encoding="utf-8")

    # Project setup
    project.mkdir(parents=True)
    (project / "AGENTS.md").write_text("# My Service\nRun pytest before commit.\n", encoding="utf-8")
    (project / "CLAUDE.md").write_text("# Ignored because AGENTS.md exists\n", encoding="utf-8")
    (project / "backend" / "api").mkdir(parents=True)
    (project / "backend" / "AGENTS.md").write_text("# Backend\nUse uv.\n", encoding="utf-8")
    (project / "backend" / "api" / "AGENTS.override.md").write_text("# API\n", encoding="utf-8")
    (project / "node_modules" / "pkg").mkdir(parents=True)
    (project / "node_modules" / "pkg" / "AGENTS.md").write_text("# vendored, must be skipped\n", encoding="utf-8")
    (project / ".codex" / "skills" / "release-notes").mkdir(parents=True)
    (project / ".codex" / "skills" / "release-notes" / "SKILL.md").write_text(
        "---\nname: release-notes\ndescription: Generate release notes\n---\nSummarize merged PRs.\n",
        encoding="utf-8",
    )
    (project / ".codex" / "config.toml").write_text(
        '[mcp_servers.proj_mcp]\ncommand = "python3"\nargs = ["server.py"]\ncwd = "."\n',
        encoding="utf-8",
    )

    # Second project: no AGENTS.md, only the CLAUDE.md fallback + legacy codex.md
    fallback_proj = tmp / "legacy-app"
    fallback_proj.mkdir()
    (fallback_proj / "CLAUDE.md").write_text("# Legacy App\nAlways lint.\n", encoding="utf-8")
    (fallback_proj / "codex.md").write_text("# Old codex.md\nUse make.\n", encoding="utf-8")

    return home, project


def test_full_discover_plan_apply_flow():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home, project = build_realistic_codex_env(tmp)
        inv_path = tmp / "inv.json"

        # 1. Discover (with stub codex binary on CODEX_BIN)
        fallback_proj = tmp / "legacy-app"
        env = {"CODEX_BIN": str(tmp / "bin" / "codex")}
        res = run_py(DISCOVER, [
            "--home", str(home), "--codex-home", str(home / ".codex"),
            "--project", str(project), "--project", str(fallback_proj), "--pretty",
        ], env=env)
        assert res.returncode == 0, res.stderr
        inv = json.loads(res.stdout)
        inv_path.write_text(res.stdout, encoding="utf-8")

        # Version detection + config-era feature detection
        cx = inv["codex"]
        assert cx["version"] == "0.143.0"
        assert cx["source"] == "codex --version"
        assert cx["era"] == "rust"
        assert cx["latest_known_release"] == "0.200.0"
        assert cx["config_era"]["inline_profiles"] is True
        assert cx["config_era"]["file_profiles"] == ["work.config.toml"]
        assert "legacy-ts (config.json)" in cx["config_era"]["generations_present"]

        # --no-exec never spawns the binary; falls back to path/version.json hints
        noexec = run_py(DISCOVER, ["--home", str(home), "--codex-home", str(home / ".codex"), "--no-exec"], env=env)
        assert noexec.returncode == 0, noexec.stderr
        cx2 = json.loads(noexec.stdout)["codex"]
        assert cx2["version"] is None
        assert any("--no-exec" in h for h in cx2["hints"])
        assert any("0.200.0" in h for h in cx2["hints"])

        assert inv["codex_dir_exists"] is True
        assert inv["stays_behind"]["credentials_present"] == [".env", "auth.json", "mcp_oauth_tokens.json"]
        assert inv["stays_behind"]["session_files_count"] == 2
        assert inv["stays_behind"]["memory_files_count"] == 1
        # Secret values must never appear in inventory
        for secret in ("sk-SECRET-DO-NOT-COPY", "sk-DOTENV-SECRET", "OAUTH-SECRET", "PRIVATE TRANSCRIPT",
                       "PRIVATE MEMORY", "LITERAL-HEADER-SECRET", "sk-HELPER-SECRET"):
            assert secret not in res.stdout, secret

        user = inv["user"]
        # config.toml surface
        assert user["settings"]["model_providers"]["my_gateway"]["env_key"] == "GW_API_KEY"
        assert user["settings"]["model_providers"]["my_gateway"]["http_headers_keys"] == ["X-Static-Token"]
        profiles = user["settings"]["profiles"]
        assert profiles["fast"]["approval_policy"] == "never"
        assert profiles["fast"]["_source"] == "inline [profiles.*]"
        assert profiles["work"]["model"] == "gpt-5.6"
        assert profiles["work"]["_source"].endswith("work.config.toml")
        assert profiles["work"]["model_providers"]["openai-api"]["auth_command"][0] == "/bin/bash"
        assert user["settings"]["sandbox_workspace_write"]["network_access"] is True
        assert user["settings"]["shell_environment_policy"]["set"] == {"CI": "1"}
        assert user["notify"] == ["python3", "/usr/local/bin/notify.py"]
        assert user["project_doc_fallback_filenames"] == ["CLAUDE.md"]
        assert {tp["path"] for tp in user["trusted_projects"]} == {str(project), str(tmp / "other-trusted")}
        assert user["plugin_states"]["dead-plugin@official"] is False
        assert user["legacy_config_json"]["provider"] == "azure"
        assert user["legacy_config_json"]["providers"]["azure"]["envKey"] == "AZURE_KEY"
        # instructions: AGENTS.md + legacy instructions.md + model_instructions_file
        kinds = {i["kind"] for i in user["instructions"]}
        assert kinds == {"instructions", "legacy-instructions", "model-instructions"}
        # skills: disabled one flagged, policy captured
        by_name = {s["name"]: s for s in user["skills"]}
        assert by_name["noisy-skill"]["enabled"] is False
        assert by_name["deploy-check"]["enabled"] is True
        assert by_name["manual-only"]["policy"]["allow_implicit_invocation"] is False
        # plugins: disabled one flagged, app connector + marketplace seen
        plugs = {p["name"]: p for p in inv["plugins"]}
        assert plugs["dead-plugin"]["enabled"] is False
        assert plugs["linear-kit"]["enabled"] is True
        assert len(plugs["linear-kit"]["app_connectors"]) == 1
        assert inv["marketplaces"]["cloned"] == ["official"]
        # projects: nested AGENTS.md found, node_modules skipped, fallback docs seen
        projs = {p["name"]: p for p in inv["projects"]}
        nested = sorted(Path(x["path"]).relative_to(project).as_posix() for x in projs["my-service"]["nested_agents_md"])
        assert nested == ["backend/AGENTS.md", "backend/api/AGENTS.override.md"]
        assert [Path(x["path"]).name for x in projs["my-service"]["fallback_project_docs"]] == ["CLAUDE.md"]
        assert projs["legacy-app"]["native_agents_md"] == []
        assert sorted(Path(x["path"]).name for x in projs["legacy-app"]["fallback_project_docs"]) == ["CLAUDE.md", "codex.md"]

        # 2. Dry run
        dry = run_py(APPLY, ["--inventory", str(inv_path), "--home", str(home)])
        assert dry.returncode == 0, dry.stderr
        assert "Plan ID:" in dry.stdout
        assert not (home / ".gemini" / "config" / "plugins").exists()
        # Plan surfaces the new findings
        assert "Installed Codex CLI: `0.143.0`" in dry.stdout
        assert "Profiles in two styles" in dry.stdout
        assert "Disabled skill `noisy-skill`" in dry.stdout
        assert "Disabled Codex plugin `dead-plugin`" in dry.stdout
        assert "other-trusted" in dry.stdout  # trusted-but-unscanned hint
        assert "nested AGENTS.md" in dry.stdout
        assert "SECURITY" in dry.stdout and "enabled_tools" in dry.stdout
        assert "Custom model providers" in dry.stdout
        assert "memories/" in dry.stdout
        assert "Legacy TS-era config.json" in dry.stdout
        assert "`notify`" in dry.stdout

        # Extract plan ID
        marker = "Plan ID: `"
        start = dry.stdout.index(marker) + len(marker)
        plan_id = dry.stdout[start:dry.stdout.index("`", start)]

        # 3. Apply without plan-id or wrong plan-id must fail
        bad1 = run_py(APPLY, ["--inventory", str(inv_path), "--home", str(home), "--apply"])
        assert bad1.returncode == 2
        bad2 = run_py(APPLY, ["--inventory", str(inv_path), "--home", str(home), "--apply", "--plan-id", "deadbeef0000"])
        assert bad2.returncode == 2

        # 4. Apply with valid plan-id
        app = run_py(APPLY, ["--inventory", str(inv_path), "--home", str(home), "--apply", "--plan-id", plan_id])
        assert app.returncode == 0, app.stderr

        # Verify codex-user plugin
        user_plug = home / ".gemini" / "config" / "plugins" / "codex-user"
        assert (user_plug / "plugin.json").is_file()
        rule_text = (user_plug / "rules" / "codex-user-memory.md").read_text(encoding="utf-8")
        assert "trigger: always_on" in rule_text
        assert "Always run unit tests." in rule_text
        assert "Prefer small diffs." in rule_text            # legacy instructions.md
        assert "Be terse and cite files." in rule_text       # model_instructions_file

        # Disabled skill NOT written; enabled ones are
        assert not (user_plug / "skills" / "noisy-skill").exists()
        assert (user_plug / "skills" / "manual-only" / "SKILL.md").is_file()

        # Skill copied + normalized frontmatter + bundled script preserved
        sk_md = (user_plug / "skills" / "deploy-check" / "SKILL.md").read_text(encoding="utf-8")
        assert "name: deploy-check" in sk_md
        assert "description:" in sk_md
        assert (user_plug / "skills" / "deploy-check" / "scripts" / "check.sh").is_file()

        # Prompt converted to skill + $ARGUMENTS rewritten
        pr_md = (user_plug / "skills" / "pr-review" / "SKILL.md").read_text(encoding="utf-8")
        assert "$ARGUMENTS" not in pr_md
        assert "the user's request" in pr_md

        # Subagent converted with mapped tools and unknown tool dropped
        ag_md = (user_plug / "agents" / "sec-auditor.md").read_text(encoding="utf-8")
        assert "subagent: true" in ag_md
        assert "run_command" in ag_md
        assert "unknown_codex_tool" not in ag_md

        # MCP config converted accurately
        mcp = json.loads((user_plug / "mcp_config.json").read_text(encoding="utf-8"))["mcpServers"]
        assert "disabled_srv" not in mcp
        assert mcp["local_db"]["command"] == "npx"
        assert mcp["local_db"]["cwd"] == str((home / "work").resolve())
        assert "PATH" not in mcp["local_db"]["env"]
        assert mcp["local_db"]["env"]["PGURL"] == "postgres://localhost/dev"
        for codex_only in ("env_vars", "enabled_tools", "disabled_tools", "startup_timeout_sec"):
            assert codex_only not in mcp["local_db"]
        assert mcp["remote_api"]["url"] == "https://mcp.example.com/v1"
        assert mcp["remote_api"]["headers"]["Authorization"] == "Bearer ${EXAMPLE_API_TOKEN}"
        assert mcp["remote_api"]["headers"]["X-Org-Key"] == "${ORG_KEY}"
        var_names = {v["name"] for v in mcp["remote_api"]["variables"]}
        assert var_names == {"EXAMPLE_API_TOKEN", "ORG_KEY"}

        # Hooks written disabled as proposal (incl. notify as Stop), never hooks.json
        proposed = json.loads((user_plug / "hooks.proposed.json").read_text(encoding="utf-8"))
        assert "PreToolUse" in proposed["hooks"]
        assert any(h.get("_source") == "config.toml notify" for h in proposed["hooks"]["Stop"])
        assert not (user_plug / "hooks.json").exists()
        notes = (user_plug / "MIGRATION_NOTES.md").read_text(encoding="utf-8")
        assert "Installed Codex CLI: `0.143.0`" in notes
        assert "work.config.toml" in notes
        assert "sk-HELPER-SECRET" not in notes
        assert "Custom model providers" in notes
        assert "GW_API_KEY" in notes
        assert "LITERAL-HEADER-SECRET" not in notes
        assert "Codex profiles" in notes
        assert "config.json" in notes
        assert "SECURITY" in notes

        # Verify Codex plugin split into codex-linear-kit; disabled plugin skipped
        lin_plug = home / ".gemini" / "config" / "plugins" / "codex-linear-kit"
        assert (lin_plug / "plugin.json").is_file()
        assert (lin_plug / "skills" / "triage" / "SKILL.md").is_file()
        assert (lin_plug / "mcp_config.json").is_file()
        assert not (home / ".gemini" / "config" / "plugins" / "codex-dead-plugin").exists()

        # Verify project plugin & native AGENTS.md untouched; CLAUDE.md not duplicated
        proj_plug = project / ".agents" / "plugins" / "codex-project"
        assert (proj_plug / "plugin.json").is_file()
        assert (proj_plug / "skills" / "release-notes" / "SKILL.md").is_file()
        proj_mcp = json.loads((proj_plug / "mcp_config.json").read_text(encoding="utf-8"))["mcpServers"]
        assert proj_mcp["proj_mcp"]["cwd"] == str(project.resolve())
        assert (project / "AGENTS.md").read_text(encoding="utf-8") == "# My Service\nRun pytest before commit.\n"
        assert not (proj_plug / "rules").exists()

        # Fallback project: CLAUDE.md + codex.md become an always-on project rule
        fb_plug = fallback_proj / ".agents" / "plugins" / "codex-project"
        fb_rule = (fb_plug / "rules" / "codex-project-instructions.md").read_text(encoding="utf-8")
        assert "trigger: always_on" in fb_rule
        assert "Always lint." in fb_rule
        assert "Use make." in fb_rule
        assert (fallback_proj / "CLAUDE.md").is_file()  # source untouched

        # 5. Re-running apply creates backup cleanly
        app2 = run_py(APPLY, ["--inventory", str(inv_path), "--home", str(home), "--apply", "--plan-id", plan_id])
        assert app2.returncode == 0, app2.stderr
        backups = list((home / ".gemini" / "config" / "plugins").glob("codex-user.bak-*"))
        assert len(backups) == 1


def test_missing_codex_dir_is_graceful():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "empty-home"
        home.mkdir()
        res = run_py(DISCOVER, ["--home", str(home), "--codex-home", str(home / ".codex")])
        assert res.returncode == 0
        inv = json.loads(res.stdout)
        assert inv["codex_dir_exists"] is False
        assert inv["plugins"] == []
        assert inv["user"]["skills"] == []
