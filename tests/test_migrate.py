"""End-to-end tests building real OpenAI Codex CLI directory layouts in temp dirs."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "migrate-from-codex" / "scripts"
DISCOVER = SCRIPTS / "discover.py"
APPLY = SCRIPTS / "apply.py"


def run_py(script: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def build_realistic_codex_env(tmp: Path) -> tuple[Path, Path]:
    home = tmp / "home"
    codex = home / ".codex"
    project = tmp / "my-service"

    # Credentials and sessions (must NEVER be read or copied)
    codex.mkdir(parents=True)
    (codex / "auth.json").write_text('{"OPENAI_API_KEY": "sk-SECRET-DO-NOT-COPY"}\n', encoding="utf-8")
    sessions = codex / "sessions" / "2026" / "11"
    sessions.mkdir(parents=True)
    (sessions / "rollout-1.jsonl").write_text('{"role":"user","text":"PRIVATE TRANSCRIPT"}\n', encoding="utf-8")
    (codex / "history.jsonl").write_text('{"text":"PRIVATE HISTORY"}\n', encoding="utf-8")

    # User config.toml
    (codex / "config.toml").write_text(
        """
model = "gpt-5.3-codex"
model_reasoning_effort = "high"
approval_policy = "on-request"
sandbox_mode = "workspace-write"

[profiles.fast]
model = "gpt-5.3-codex-mini"

[mcp_servers.local_db]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-postgres"]
cwd = "~/work"
startup_timeout_sec = 30
env = { PGURL = "postgres://localhost/dev", PATH = "${PATH}" }

[mcp_servers.remote_api]
url = "https://mcp.example.com/v1"
bearer_token_env_var = "EXAMPLE_API_TOKEN"
http_headers = { "X-Team" = "platform" }
env_http_headers = { "X-Org-Key" = "ORG_KEY" }

[mcp_servers.disabled_srv]
command = "echo"
enabled = false
""".strip() + "\n",
        encoding="utf-8",
    )

    # User AGENTS.md
    (codex / "AGENTS.md").write_text("# Global Codex Rules\nAlways run unit tests.\n", encoding="utf-8")

    # User skill (missing frontmatter description on purpose to test normalization)
    sk = codex / "skills" / "deploy-check"
    (sk / "scripts").mkdir(parents=True)
    (sk / "SKILL.md").write_text("# Deploy Check\nVerify staging health before releasing.\n", encoding="utf-8")
    (sk / "scripts" / "check.sh").write_text("#!/bin/sh\necho ok\n", encoding="utf-8")

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
    (project / ".codex" / "skills" / "release-notes").mkdir(parents=True)
    (project / ".codex" / "skills" / "release-notes" / "SKILL.md").write_text(
        "---\nname: release-notes\ndescription: Generate release notes\n---\nSummarize merged PRs.\n",
        encoding="utf-8",
    )
    (project / ".codex" / "config.toml").write_text(
        '[mcp_servers.proj_mcp]\ncommand = "python3"\nargs = ["server.py"]\ncwd = "."\n',
        encoding="utf-8",
    )

    return home, project


def test_full_discover_plan_apply_flow():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home, project = build_realistic_codex_env(tmp)
        inv_path = tmp / "inv.json"

        # 1. Discover
        res = run_py(DISCOVER, ["--home", str(home), "--codex-home", str(home / ".codex"), "--project", str(project), "--pretty"])
        assert res.returncode == 0, res.stderr
        inv = json.loads(res.stdout)
        inv_path.write_text(res.stdout, encoding="utf-8")

        assert inv["codex_dir_exists"] is True
        assert inv["stays_behind"]["credentials_present"] == ["auth.json"]
        assert inv["stays_behind"]["session_files_count"] == 2
        # Secret values must never appear in inventory
        assert "sk-SECRET-DO-NOT-COPY" not in res.stdout
        assert "PRIVATE TRANSCRIPT" not in res.stdout

        # 2. Dry run
        dry = run_py(APPLY, ["--inventory", str(inv_path), "--home", str(home)])
        assert dry.returncode == 0, dry.stderr
        assert "Plan ID:" in dry.stdout
        assert not (home / ".gemini" / "config" / "plugins").exists()

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
        assert mcp["remote_api"]["url"] == "https://mcp.example.com/v1"
        assert mcp["remote_api"]["headers"]["Authorization"] == "Bearer ${EXAMPLE_API_TOKEN}"
        assert mcp["remote_api"]["headers"]["X-Org-Key"] == "${ORG_KEY}"
        var_names = {v["name"] for v in mcp["remote_api"]["variables"]}
        assert var_names == {"EXAMPLE_API_TOKEN", "ORG_KEY"}

        # Hooks written disabled as proposal, never hooks.json
        assert (user_plug / "hooks.proposed.json").is_file()
        assert not (user_plug / "hooks.json").exists()
        assert (user_plug / "MIGRATION_NOTES.md").is_file()

        # Verify Codex plugin split into codex-linear-kit
        lin_plug = home / ".gemini" / "config" / "plugins" / "codex-linear-kit"
        assert (lin_plug / "plugin.json").is_file()
        assert (lin_plug / "skills" / "triage" / "SKILL.md").is_file()
        assert (lin_plug / "mcp_config.json").is_file()

        # Verify project plugin & native AGENTS.md untouched
        proj_plug = project / ".agents" / "plugins" / "codex-project"
        assert (proj_plug / "plugin.json").is_file()
        assert (proj_plug / "skills" / "release-notes" / "SKILL.md").is_file()
        proj_mcp = json.loads((proj_plug / "mcp_config.json").read_text(encoding="utf-8"))["mcpServers"]
        assert proj_mcp["proj_mcp"]["cwd"] == str(project.resolve())
        assert (project / "AGENTS.md").read_text(encoding="utf-8") == "# My Service\nRun pytest before commit.\n"

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
