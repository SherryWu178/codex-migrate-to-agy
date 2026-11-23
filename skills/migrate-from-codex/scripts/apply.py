#!/usr/bin/env python3
"""Turn an OpenAI Codex CLI inventory (from discover.py) into Antigravity config.

Dry-run by default: prints the plan and Plan ID, and writes nothing.
Pass --apply --plan-id <ID> to write.

What it writes:
  ~/.gemini/config/plugins/codex-user/        user skills, prompts->skills,
                                              subagents, user AGENTS.md rule, MCP
  ~/.gemini/config/plugins/codex-<plugin>/    one agy plugin per Codex plugin
  <project>/.agents/plugins/codex-project/    project .codex skills, prompts,
                                              subagents, MCP

Project-level <project>/AGENTS.md and <project>/.agents/skills/ are already
read natively by Antigravity and are left untouched (not duplicated).

Each generated plugin can be switched off as a unit (agy plugin disable <name>).
Hooks, sandbox/approval settings, profiles, and .rules files are written as
proposals/notes only (hooks.proposed.json, MIGRATION_NOTES.md); they are never
activated automatically.

Inspired by pauldatta/claude-migrate-to-agy (Apache-2.0).

Usage:
  python3 discover.py --project . > /tmp/codex-inventory.json
  python3 apply.py --inventory /tmp/codex-inventory.json
  python3 apply.py --inventory /tmp/codex-inventory.json --apply --plan-id <ID>
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import tomllib
from pathlib import Path

# Codex / generic tool names -> Antigravity native tool names.
TOOL_MAP = {
    "shell": "run_command",
    "bash": "run_command",
    "exec_command": "run_command",
    "apply_patch": "edit_file",
    "edit": "edit_file",
    "write_file": "write_file",
    "read_file": "read_file",
    "view_file": "read_file",
    "grep": "read_file",
    "glob": "read_file",
    "web_search": "search_web",
    "spawn_agent": "invoke_subagent",
    "agent": "invoke_subagent",
}

DEFAULT_AGENT_TOOLS = [
    "read_file",
    "edit_file",
    "write_file",
    "run_command",
    "manage_task",
    "search_web",
]

# Hook event mapping for proposed hooks (disabled by default).
HOOK_EVENT_MAP = {
    "PreToolUse": ("PreToolUse", "exact"),
    "PostToolUse": ("PostToolUse", "exact"),
    "Stop": ("Stop", "exact"),
    "UserPromptSubmit": ("PreInvocation", "approximate"),
    "SessionStart": ("SessionStart", "approximate"),
}

NAME_OK = re.compile(r"[^a-zA-Z0-9_-]+")
PLACEHOLDER_RE = re.compile(r"\$(?:ARGUMENTS|[1-9])\b|\{\{\s*args\s*\}\}")
PASSTHROUGH_ENV_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$|^\$([A-Za-z_][A-Za-z0-9_]*)$")
VAR_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")

MAX_RULE_BYTES = 24 * 1024  # Antigravity rule file cap (24 KB)


def slug(text: str) -> str:
    return NAME_OK.sub("-", text).strip("-").lower() or "unnamed"


def yaml_scalar(val: str) -> str:
    val = str(val)
    if val == "" or any(c in val for c in ":#{}[],&*!|>'\"%@`\n") or val != val.strip():
        return json.dumps(val)
    return val


def frontmatter(meta: dict) -> str:
    lines = ["---"]
    for key, val in meta.items():
        if isinstance(val, list):
            lines.append(f"{key}:")
            lines += [f"  - {yaml_scalar(v)}" for v in val]
        elif isinstance(val, bool):
            lines.append(f"{key}: {'true' if val else 'false'}")
        else:
            lines.append(f"{key}: {yaml_scalar(val)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    block = text[3:end].strip("\n")
    body = text[end + 4:].lstrip("\n")
    meta: dict = {}
    key = None
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t")) and key and line.strip().startswith("- "):
            meta.setdefault(key, [])
            if isinstance(meta[key], list):
                meta[key].append(line.strip()[2:].strip().strip("'\""))
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if val.startswith("[") and val.endswith("]"):
                meta[key] = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
            elif val == "":
                meta[key] = []
            else:
                meta[key] = val.strip("'\"")
    return meta, body


def first_line(text: str, limit: int = 160) -> str:
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:limit]
    return "Migrated from OpenAI Codex CLI."


def resolve_cwd(cwd: str, home: Path, project_root: Path | None) -> str:
    val = cwd
    if project_root is not None:
        val = val.replace("${workspaceFolder}", str(project_root))
    val = val.replace("$HOME", str(home)).replace("${HOME}", str(home))
    if val == "~":
        val = str(home)
    elif val.startswith("~/"):
        val = str(home / val[2:])
    p = Path(val)
    if not p.is_absolute():
        base = project_root if project_root is not None else home
        p = base / p
    return str(p.resolve())


# ---------------------------------------------------------------- conversions
def convert_mcp(cfg: dict, home: Path, project_root: Path | None = None) -> tuple[dict, list[str]]:
    """Convert a Codex [mcp_servers.<name>] or plugin mcp.json entry to Antigravity."""
    notes: list[str] = []
    if cfg.get("enabled") is False:
        return {}, ["disabled in Codex config; skipped"]

    out: dict = {}
    variables: dict[str, dict] = {}

    def declare_var(var_name: str, desc: str) -> None:
        if var_name not in variables:
            variables[var_name] = {
                "name": var_name,
                "title": var_name.replace("_", " ").title(),
                "description": desc,
            }

    url = cfg.get("url") or cfg.get("httpUrl") or cfg.get("serverUrl")
    if url:
        if cfg.get("type") == "sse" or str(url).rstrip("/").endswith("/sse"):
            notes.append("uses legacy SSE transport; consider Streamable HTTP or `npx -y mcp-remote`")
        out["url"] = str(url)
        headers: dict[str, str] = {}
        for src_key in ("headers", "http_headers"):
            raw_h = cfg.get(src_key)
            if isinstance(raw_h, dict):
                for hk, hv in raw_h.items():
                    headers[str(hk)] = str(hv)
        env_headers = cfg.get("env_http_headers")
        if isinstance(env_headers, dict):
            for hk, env_name in env_headers.items():
                var = str(env_name)
                headers[str(hk)] = f"${{{var}}}"
                declare_var(var, f"Value for HTTP header {hk} (migrated from Codex env_http_headers).")
        bearer_var = cfg.get("bearer_token_env_var")
        if bearer_var:
            var = str(bearer_var)
            headers["Authorization"] = f"Bearer ${{{var}}}"
            declare_var(var, f"Bearer token (migrated from Codex bearer_token_env_var={var}).")
        if headers:
            out["headers"] = headers
            for hv in headers.values():
                for match in VAR_REF_RE.findall(hv):
                    declare_var(match, f"Referenced in MCP header ({match}).")
    elif cfg.get("command"):
        out["command"] = str(cfg["command"])
        if isinstance(cfg.get("args"), list) and cfg["args"]:
            out["args"] = [str(a) for a in cfg["args"]]
        if cfg.get("cwd"):
            out["cwd"] = resolve_cwd(str(cfg["cwd"]), home, project_root)
    else:
        return {}, ["neither command nor url; skipped"]

    raw_env = cfg.get("env")
    if isinstance(raw_env, dict) and raw_env:
        cleaned_env: dict[str, str] = {}
        for ek, ev in raw_env.items():
            ev_s = str(ev)
            m = PASSTHROUGH_ENV_RE.match(ev_s)
            if m and (m.group(1) == ek or m.group(2) == ek):
                # Redundant passthrough: stdio servers inherit OS env directly.
                continue
            cleaned_env[str(ek)] = ev_s
            for match in VAR_REF_RE.findall(ev_s):
                declare_var(match, f"Environment variable value for {ek}.")
        if cleaned_env:
            out["env"] = cleaned_env
            notes.append(f"{len(cleaned_env)} env var(s) copied as-is: review for secrets")

    if variables:
        out["variables"] = list(variables.values())
        var_names = ", ".join(sorted(variables.keys()))
        notes.append(f"declares plugin variable(s) ({var_names}): fill in and enable plugin after apply")

    dropped = sorted(
        k for k in cfg
        if k not in {
            "command", "args", "env", "cwd", "url", "httpUrl", "serverUrl",
            "headers", "http_headers", "env_http_headers", "bearer_token_env_var",
            "enabled", "type",
        }
    )
    if dropped:
        notes.append(f"dropped Codex-only MCP field(s): {', '.join(dropped)}")

    return out, notes


def convert_skill_md(skill_md_path: Path, folder_name: str) -> tuple[str, list[str]]:
    text = skill_md_path.read_text(encoding="utf-8")
    meta, body = split_frontmatter(text)
    notes: list[str] = []
    name = slug(folder_name)
    desc = str(meta.get("description") or "").strip() or first_line(body)
    out_meta = {"name": name, "description": desc}
    if meta.get("name") and slug(str(meta["name"])) != name:
        notes.append(f"normalized skill name to folder name '{name}'")
    if not meta.get("description"):
        notes.append("added missing description from opening line")
    return frontmatter(out_meta) + "\n" + body.lstrip("\n"), notes


def convert_prompt_to_skill(entry: dict) -> tuple[str, str, list[str]]:
    path = Path(entry["path"])
    text = path.read_text(encoding="utf-8")
    meta, body = split_frontmatter(text)
    name = slug(str(meta.get("name") or path.stem))
    desc = str(meta.get("description") or "").strip() or first_line(body)
    notes: list[str] = []
    if PLACEHOLDER_RE.search(body):
        body = PLACEHOLDER_RE.sub("the user's request", body)
        notes.append("replaced prompt argument placeholder ($ARGUMENTS/$1) with natural-language instruction")
    content = frontmatter({"name": name, "description": desc}) + "\n" + body.lstrip("\n")
    return name, content, notes


def convert_agent(entry: dict) -> tuple[str, str, list[str]]:
    path = Path(entry["path"])
    notes: list[str] = []
    if entry.get("format") == "toml":
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        name = slug(str(data.get("name") or path.stem))
        desc = str(data.get("description") or "").strip() or f"Migrated Codex agent ({name})."
        body = str(data.get("developer_instructions") or "").strip()
        if not body:
            body = f"You are {name}. Follow the user's instructions carefully."
            notes.append("no developer_instructions in TOML; added fallback prompt")
        raw_tools = data.get("tools")
        if data.get("model"):
            notes.append(f"dropped Codex model '{data['model']}' (Antigravity uses its own model tiers)")
        if data.get("sandbox_mode"):
            notes.append(f"Codex sandbox_mode='{data['sandbox_mode']}' not transferred; review permissions")
    else:
        text = path.read_text(encoding="utf-8")
        meta, body = split_frontmatter(text)
        name = slug(str(meta.get("name") or path.stem))
        desc = str(meta.get("description") or "").strip() or first_line(body)
        raw_tools = meta.get("tools")
        if meta.get("model"):
            notes.append(f"dropped model '{meta['model']}'")

    if isinstance(raw_tools, list) and raw_tools:
        mapped: list[str] = []
        dropped: list[str] = []
        for t in raw_tools:
            tgt = TOOL_MAP.get(str(t).lower()) or TOOL_MAP.get(str(t))
            if tgt:
                if tgt not in mapped:
                    mapped.append(tgt)
            else:
                dropped.append(str(t))
        tools = mapped or list(DEFAULT_AGENT_TOOLS)
        if dropped:
            notes.append(f"dropped unmapped tool(s): {', '.join(dropped)}")
    else:
        tools = list(DEFAULT_AGENT_TOOLS)

    out_meta = {
        "name": name,
        "description": desc,
        "subagent": True,
        "mainAgent": False,
        "tools": tools,
    }
    return name, frontmatter(out_meta) + "\n" + body.lstrip("\n"), notes


def convert_hooks_proposal(hooks_path: Path) -> tuple[dict, list[str]]:
    notes: list[str] = []
    try:
        raw = json.loads(hooks_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}, ["failed to parse hooks.json"]
    src_hooks = raw.get("hooks", raw) if isinstance(raw, dict) else {}
    proposed: dict = {
        "_notice": "DISABLED PROPOSAL. Antigravity hook event names and stdin/stdout JSON contracts differ from Codex. Review and rewrite scripts before renaming to hooks.json.",
        "hooks": {},
    }
    for event, handlers in src_hooks.items():
        if event in HOOK_EVENT_MAP:
            mapped, kind = HOOK_EVENT_MAP[event]
            proposed["hooks"][mapped] = handlers
            if kind == "approximate":
                notes.append(f"hook event '{event}' mapped approximately to '{mapped}'; review")
        else:
            proposed["hooks"][f"UNMAPPED_{event}"] = handlers
            notes.append(f"hook event '{event}' has no Antigravity equivalent")
    return proposed, notes


def build_migration_notes(scope_label: str, settings: dict, rules: list[dict], extra: list[str]) -> str | None:
    lines: list[str] = []
    if settings:
        lines.append("## Codex settings not applied automatically\n")
        for k, v in sorted(settings.items()):
            lines.append(f"- `{k}` = `{json.dumps(v)}`")
        lines.append("")
    if rules:
        lines.append("## Codex `.rules` files (review manually)\n")
        lines.append("Codex execution rules use a Starlark-like prefix syntax that differs from Antigravity command allowlists:\n")
        for r in rules:
            lines.append(f"- `{r['path']}` ({r['lines']} lines)")
        lines.append("")
    if extra:
        lines.append("## Additional notes\n")
        for item in extra:
            lines.append(f"- {item}")
        lines.append("")
    if not lines:
        return None
    header = f"# Migration Notes ({scope_label})\n\nGenerated by `codex-migrate-to-agy`. Nothing below was activated automatically.\n\n"
    return header + "\n".join(lines)


# ---------------------------------------------------------------- planning
def compute_plan_id(inv: dict, gemini_plugins_dir: Path) -> str:
    payload = json.dumps(
        {"inventory": inv, "target": str(gemini_plugins_dir)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def build_plan(inv: dict, home: Path) -> dict:
    gemini_plugins_dir = home / ".gemini" / "config" / "plugins"
    plan_id = compute_plan_id(inv, gemini_plugins_dir)

    moves: list[tuple[str, str, str]] = []
    checks: list[tuple[str, str]] = []
    stays: list[tuple[str, str]] = []
    actions: list[dict] = []

    # ---- 1. User plugin (codex-user)
    user = inv.get("user", {})
    user_dir = gemini_plugins_dir / "codex-user"
    user_action = {
        "kind": "plugin",
        "plugin_name": "codex-user",
        "display_name": "Codex User Settings",
        "description": "User-scoped skills, prompts, subagents, instructions and MCP servers migrated from OpenAI Codex CLI.",
        "target_dir": str(user_dir),
        "instructions": user.get("instructions", []),
        "skills": user.get("skills", []),
        "prompts": user.get("prompts", []),
        "agents": user.get("agents", []),
        "mcp_servers": user.get("mcp_servers", []),
        "hooks": user.get("hooks"),
        "settings": user.get("settings", {}),
        "rules": user.get("rules", []),
        "project_root": None,
    }
    has_user_content = any([
        user_action["instructions"],
        user_action["skills"],
        user_action["prompts"],
        user_action["agents"],
        user_action["mcp_servers"],
        user_action["hooks"],
        user_action["rules"],
        user_action["settings"],
    ])
    if has_user_content:
        actions.append(user_action)
        if user_action["instructions"]:
            total_bytes = sum(Path(i["path"]).stat().st_size for i in user_action["instructions"] if Path(i["path"]).is_file())
            moves.append(("User AGENTS.md", ", ".join(Path(i["path"]).name for i in user_action["instructions"]), f"{user_dir}/rules/codex-user-memory.md"))
            if total_bytes > MAX_RULE_BYTES:
                checks.append(("User AGENTS.md", f"combined size ({total_bytes} B) exceeds Antigravity 24 KB rule cap; trim after migration"))
        for s in user_action["skills"]:
            moves.append(("User skill", s["name"], f"{user_dir}/skills/{slug(s['name'])}/"))
        for p in user_action["prompts"]:
            pname = slug(p.get("meta", {}).get("name") or Path(p["path"]).stem)
            moves.append(("User prompt → skill", Path(p["path"]).name, f"{user_dir}/skills/{pname}/SKILL.md"))
        for a in user_action["agents"]:
            aname = slug(a.get("meta", {}).get("name") or Path(a["path"]).stem)
            moves.append(("User subagent", Path(a["path"]).name, f"{user_dir}/agents/{aname}.md"))
            checks.append((f"Subagent `{aname}`", "verify mapped Antigravity tool list"))
        for m in user_action["mcp_servers"]:
            conv, notes = convert_mcp(m["config"], home, None)
            if conv:
                moves.append(("User MCP server", m["name"], f"{user_dir}/mcp_config.json"))
            for n in notes:
                checks.append((f"MCP `{m['name']}`", n))
        if user_action["hooks"]:
            moves.append(("User hooks (disabled)", user_action["hooks"]["path"], f"{user_dir}/hooks.proposed.json"))
            checks.append(("User hooks", "written disabled as hooks.proposed.json; rewrite scripts before activating"))
        if user_action["rules"]:
            moves.append(("User .rules notes", f"{len(user_action['rules'])} file(s)", f"{user_dir}/MIGRATION_NOTES.md"))
            checks.append(("Codex .rules", "not auto-applied; review MIGRATION_NOTES.md"))
        if user_action["settings"]:
            checks.append(("Codex config.toml settings", f"recorded in MIGRATION_NOTES.md: {', '.join(sorted(user_action['settings'].keys()))}"))

    if user.get("shared_agents_skills_count", 0) > 0:
        checks.append(("~/.agents/skills/", f"{user['shared_agents_skills_count']} skill(s) already in cross-agent ~/.agents/skills/ (not duplicated)"))

    # ---- 2. Installed Codex plugins (codex-<plugin>)
    for plug in inv.get("plugins", []):
        pname = f"codex-{slug(plug['name'])}"
        pdir = gemini_plugins_dir / pname
        actions.append({
            "kind": "plugin",
            "plugin_name": pname,
            "display_name": f"Codex Plugin: {plug['name']}",
            "description": f"Migrated from Codex plugin '{plug['name']}' ({plug['layout']} layout).",
            "target_dir": str(pdir),
            "instructions": [],
            "skills": plug.get("skills", []),
            "prompts": plug.get("prompts", []),
            "agents": plug.get("agents", []),
            "mcp_servers": plug.get("mcp_servers", []),
            "hooks": plug.get("hooks"),
            "settings": {},
            "rules": [],
            "project_root": None,
        })
        summary = f"{len(plug.get('skills', []))} skills, {len(plug.get('mcp_servers', []))} MCP"
        moves.append((f"Codex plugin `{plug['name']}`", summary, str(pdir)))
        for m in plug.get("mcp_servers", []):
            _, notes = convert_mcp(m["config"], home, None)
            for n in notes:
                checks.append((f"Plugin `{plug['name']}` MCP `{m['name']}`", n))
        if plug.get("hooks"):
            checks.append((f"Plugin `{plug['name']}` hooks", "written disabled as hooks.proposed.json"))

    # ---- 3. Project plugins (<project>/.agents/plugins/codex-project)
    for proj in inv.get("projects", []):
        proot = Path(proj["root"])
        if proj.get("native_agents_md"):
            names = ", ".join(Path(x["path"]).name for x in proj["native_agents_md"])
            checks.append((f"Project `{proj['name']}` {names}", "already read natively by Antigravity (left untouched, not duplicated)"))
        if proj.get("native_agents_skills"):
            checks.append((f"Project `{proj['name']}` .agents/skills/", f"{len(proj['native_agents_skills'])} skill(s) already native to Antigravity"))

        proj_dir = proot / ".agents" / "plugins" / "codex-project"
        proj_action = {
            "kind": "plugin",
            "plugin_name": "codex-project",
            "display_name": f"Codex Project ({proj['name']})",
            "description": f"Project-scoped Codex skills, prompts, subagents and MCP servers for {proj['name']}.",
            "target_dir": str(proj_dir),
            "instructions": [],
            "skills": proj.get("skills", []),
            "prompts": proj.get("prompts", []),
            "agents": proj.get("agents", []),
            "mcp_servers": proj.get("mcp_servers", []),
            "hooks": proj.get("hooks"),
            "settings": proj.get("settings", {}),
            "rules": proj.get("rules", []),
            "project_root": str(proot),
        }
        has_proj_content = any([
            proj_action["skills"],
            proj_action["prompts"],
            proj_action["agents"],
            proj_action["mcp_servers"],
            proj_action["hooks"],
            proj_action["settings"],
            proj_action["rules"],
        ])
        if has_proj_content:
            actions.append(proj_action)
            for s in proj_action["skills"]:
                moves.append((f"Project `{proj['name']}` skill", s["name"], f"{proj_dir}/skills/{slug(s['name'])}/"))
            for p in proj_action["prompts"]:
                pname = slug(p.get("meta", {}).get("name") or Path(p["path"]).stem)
                moves.append((f"Project `{proj['name']}` prompt → skill", Path(p["path"]).name, f"{proj_dir}/skills/{pname}/SKILL.md"))
            for a in proj_action["agents"]:
                aname = slug(a.get("meta", {}).get("name") or Path(a["path"]).stem)
                moves.append((f"Project `{proj['name']}` subagent", Path(a["path"]).name, f"{proj_dir}/agents/{aname}.md"))
            for m in proj_action["mcp_servers"]:
                conv, notes = convert_mcp(m["config"], home, proot)
                if conv:
                    moves.append((f"Project `{proj['name']}` MCP", m["name"], f"{proj_dir}/mcp_config.json"))
                for n in notes:
                    checks.append((f"Project `{proj['name']}` MCP `{m['name']}`", n))
            if proj_action["hooks"]:
                moves.append((f"Project `{proj['name']}` hooks (disabled)", proj_action["hooks"]["path"], f"{proj_dir}/hooks.proposed.json"))
                checks.append((f"Project `{proj['name']}` hooks", "written disabled as hooks.proposed.json"))

    # ---- 4. Stays behind
    sb = inv.get("stays_behind", {})
    if sb.get("credentials_present"):
        stays.append(("Credentials", f"{', '.join(sb['credentials_present'])} — never read or copied"))
    if sb.get("session_files_count", 0) > 0:
        stays.append(("Chat history / sessions", f"{sb['session_files_count']} session/history file(s) — never opened or migrated"))
    if user.get("settings", {}).get("model"):
        stays.append(("Model / reasoning defaults", f"model={user['settings'].get('model')} — Antigravity manages models separately"))

    return {
        "plan_id": plan_id,
        "moves": moves,
        "checks": checks,
        "stays": stays,
        "actions": actions,
    }


def render_plan(plan: dict) -> str:
    lines = [f"# Codex → Antigravity Migration Plan (Plan ID: `{plan['plan_id']}`)\n"]

    lines.append("## Moves over\n")
    if plan["moves"]:
        lines.append("| Kind | Item | Destination |")
        lines.append("|:-|:-|:-|")
        for kind, item, dest in plan["moves"]:
            lines.append(f"| {kind} | {item} | `{dest}` |")
    else:
        lines.append("_Nothing to migrate._")
    lines.append("")

    lines.append("## You check\n")
    if plan["checks"]:
        lines.append("| Item | Note |")
        lines.append("|:-|:-|")
        for item, note in plan["checks"]:
            lines.append(f"| {item} | {note} |")
    else:
        lines.append("_No manual follow-ups flagged._")
    lines.append("")

    lines.append("## Stays behind\n")
    if plan["stays"]:
        lines.append("| Category | Details |")
        lines.append("|:-|:-|")
        for cat, det in plan["stays"]:
            lines.append(f"| {cat} | {det} |")
    else:
        lines.append("_None._")
    lines.append("")

    lines.append(f"To apply this exact plan, run:\n\n```bash\npython3 apply.py --inventory <file> --apply --plan-id {plan['plan_id']}\n```\n")
    return "\n".join(lines)


# ---------------------------------------------------------------- apply
def backup_if_exists(target: Path) -> Path | None:
    if not target.exists():
        return None
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    bak = target.with_name(f"{target.name}.bak-{ts}")
    counter = 1
    while bak.exists():
        bak = target.with_name(f"{target.name}.bak-{ts}-{counter}")
        counter += 1
    shutil.move(str(target), str(bak))
    return bak


def write_plugin(action: dict, home: Path) -> list[str]:
    target = Path(action["target_dir"])
    target.parent.mkdir(parents=True, exist_ok=True)
    bak = backup_if_exists(target)
    target.mkdir(parents=True, exist_ok=True)

    log: list[str] = []
    if bak:
        log.append(f"Backed up previous {target.name} to {bak}")

    plugin_manifest = {
        "$schema": "https://antigravity.google/schemas/v1/plugin.json",
        "name": action["plugin_name"],
        "displayName": action["display_name"],
        "version": "1.0.0",
        "description": action["description"],
    }
    (target / "plugin.json").write_text(json.dumps(plugin_manifest, indent=2) + "\n", encoding="utf-8")

    extra_notes: list[str] = []
    project_root = Path(action["project_root"]) if action["project_root"] else None

    # 1. User-level AGENTS.md -> rules/codex-user-memory.md
    if action["instructions"]:
        rules_dir = target / "rules"
        rules_dir.mkdir(parents=True, exist_ok=True)
        parts: list[str] = []
        for entry in action["instructions"]:
            p = Path(entry["path"])
            if p.is_file():
                parts.append(f"<!-- Migrated from {p} -->\n\n" + p.read_text(encoding="utf-8").strip())
        rule_text = frontmatter({"trigger": "always_on"}) + "\n" + "\n\n".join(parts) + "\n"
        (rules_dir / "codex-user-memory.md").write_text(rule_text, encoding="utf-8")
        if len(rule_text.encode("utf-8")) > MAX_RULE_BYTES:
            extra_notes.append("`rules/codex-user-memory.md` exceeds 24 KB; trim it so Antigravity loads it cleanly.")

    # 2. Skills (copy full directory, resolving symlinks, normalizing SKILL.md frontmatter)
    for s in action["skills"]:
        src_dir = Path(s["dir"])
        if not src_dir.is_dir():
            continue
        sname = slug(s["name"])
        dst_dir = target / "skills" / sname
        shutil.copytree(src_dir, dst_dir, symlinks=False, dirs_exist_ok=True)
        skill_md = dst_dir / "SKILL.md"
        if skill_md.is_file():
            normalized, notes = convert_skill_md(skill_md, sname)
            skill_md.write_text(normalized, encoding="utf-8")
            for n in notes:
                extra_notes.append(f"Skill `{sname}`: {n}")

    # 3. Prompts -> skills
    for p in action["prompts"]:
        pname, content, notes = convert_prompt_to_skill(p)
        dst_dir = target / "skills" / pname
        if dst_dir.exists():
            pname = f"prompt-{pname}"
            dst_dir = target / "skills" / pname
            content = content.replace(f"name: {slug(Path(p['path']).stem)}", f"name: {pname}", 1)
        dst_dir.mkdir(parents=True, exist_ok=True)
        (dst_dir / "SKILL.md").write_text(content, encoding="utf-8")
        for n in notes:
            extra_notes.append(f"Prompt `{pname}`: {n}")

    # 4. Custom subagents
    for a in action["agents"]:
        aname, content, notes = convert_agent(a)
        agents_dir = target / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / f"{aname}.md").write_text(content, encoding="utf-8")
        for n in notes:
            extra_notes.append(f"Subagent `{aname}`: {n}")

    # 5. MCP servers
    mcp_map: dict[str, dict] = {}
    for m in action["mcp_servers"]:
        conv, notes = convert_mcp(m["config"], home, project_root)
        if conv:
            mcp_map[m["name"]] = conv
        for n in notes:
            extra_notes.append(f"MCP `{m['name']}`: {n}")
    if mcp_map:
        (target / "mcp_config.json").write_text(
            json.dumps({"mcpServers": mcp_map}, indent=2) + "\n",
            encoding="utf-8",
        )

    # 6. Hooks (always written disabled as hooks.proposed.json)
    if action["hooks"]:
        proposed, notes = convert_hooks_proposal(Path(action["hooks"]["path"]))
        if proposed:
            (target / "hooks.proposed.json").write_text(
                json.dumps(proposed, indent=2) + "\n",
                encoding="utf-8",
            )
        for n in notes:
            extra_notes.append(f"Hooks: {n}")

    # 7. Migration notes
    notes_md = build_migration_notes(
        action["plugin_name"],
        action["settings"],
        action["rules"],
        extra_notes,
    )
    if notes_md:
        (target / "MIGRATION_NOTES.md").write_text(notes_md, encoding="utf-8")

    log.append(f"Wrote plugin `{action['plugin_name']}` -> {target}")
    return log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan or apply a Codex -> Antigravity migration.")
    parser.add_argument("--inventory", type=Path, required=True, help="JSON output from discover.py")
    parser.add_argument("--home", type=Path, default=None, help="Override home directory (default: from inventory)")
    parser.add_argument("--apply", action="store_true", help="Write files (requires matching --plan-id)")
    parser.add_argument("--plan-id", type=str, default=None, help="Plan ID from the dry run")
    args = parser.parse_args(argv)

    inv = json.loads(args.inventory.read_text(encoding="utf-8"))
    home = (args.home or Path(inv["home"])).expanduser().resolve()
    plan = build_plan(inv, home)

    if not args.apply:
        sys.stdout.write(render_plan(plan))
        return 0

    if not args.plan_id:
        sys.stderr.write(
            f"Refusing to apply without --plan-id. Run without --apply first, then pass --plan-id {plan['plan_id']}.\n"
        )
        return 2

    if args.plan_id.strip() != plan["plan_id"]:
        sys.stderr.write(
            f"Plan ID mismatch: got '{args.plan_id}', current plan is '{plan['plan_id']}'. Re-run dry run and review the updated plan.\n"
        )
        return 2

    if not plan["actions"]:
        sys.stdout.write("Nothing to apply.\n")
        return 0

    for action in plan["actions"]:
        for line in write_plugin(action, home):
            sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
