#!/usr/bin/env python3
"""Turn an OpenAI Codex CLI inventory (from discover.py) into Antigravity config.

Dry-run by default: prints the plan and Plan ID, and writes nothing.
Pass --apply --plan-id <ID> to write.

What it writes:
  ~/.gemini/config/plugins/codex-user/        user skills, prompts->skills,
                                              subagents, user instructions rule
                                              (AGENTS.md, legacy instructions.md,
                                              model_instructions_file), MCP
  ~/.gemini/config/plugins/codex-<plugin>/    one agy plugin per *enabled* Codex plugin
  <project>/.agents/plugins/codex-project/    project .codex skills, prompts,
                                              subagents, MCP, and a rule built from
                                              project_doc_fallback_filenames /
                                              legacy codex.md when AGENTS.md is absent

Project-level <project>/AGENTS.md and <project>/.agents/skills/ are already
read natively by Antigravity and are left untouched (not duplicated).

Skills disabled via [[skills.config]] and plugins disabled via [plugins.*]
in config.toml are respected and NOT migrated.

Each generated plugin can be switched off as a unit (agy plugin disable <name>).
Hooks, `notify`, sandbox/approval settings, model_providers, profiles, and
.rules files are written as proposals/notes only (hooks.proposed.json,
MIGRATION_NOTES.md); they are never activated automatically.

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

    # Codex `env_vars = ["A", "B"]` is a passthrough allowlist; Antigravity stdio
    # servers inherit the OS environment, so nothing needs copying.
    passthrough = cfg.get("env_vars")
    if isinstance(passthrough, list) and passthrough:
        notes.append(
            f"env_vars passthrough list ({', '.join(str(v) for v in passthrough)}) not needed: "
            "Antigravity stdio servers inherit the OS environment"
        )

    # Tool allow/deny lists are a security decision; surface them loudly.
    for list_key, label in (("enabled_tools", "allowlist"), ("disabled_tools", "denylist")):
        tool_list = cfg.get(list_key)
        if isinstance(tool_list, list) and tool_list:
            notes.append(
                f"SECURITY: Codex tool {label} `{list_key}` = [{', '.join(str(t) for t in tool_list)}] "
                "is NOT enforced by Antigravity mcp_config.json; restrict tools server-side or review before enabling"
            )

    timeouts = [f"{k}={cfg[k]}" for k in ("startup_timeout_sec", "tool_timeout_sec", "startup_timeout_ms") if k in cfg]
    if timeouts:
        notes.append(f"timeout setting(s) not transferable ({', '.join(timeouts)}); Antigravity uses its own defaults")

    if variables:
        out["variables"] = list(variables.values())
        var_names = ", ".join(sorted(variables.keys()))
        notes.append(f"declares plugin variable(s) ({var_names}): fill in and enable plugin after apply")

    dropped = sorted(
        k for k in cfg
        if k not in {
            "command", "args", "env", "cwd", "url", "httpUrl", "serverUrl",
            "headers", "http_headers", "env_http_headers", "bearer_token_env_var",
            "enabled", "type", "env_vars", "enabled_tools", "disabled_tools",
            "startup_timeout_sec", "tool_timeout_sec", "startup_timeout_ms",
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


def convert_hooks_proposal(hooks_path: Path | None, notify: list[str] | None = None) -> tuple[dict, list[str]]:
    notes: list[str] = []
    src_hooks: dict = {}
    if hooks_path is not None:
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
    if notify:
        # Codex `notify = [...]` runs after each turn with a JSON payload on
        # argv/stdin; closest Antigravity analogue is a Stop hook.
        proposed["hooks"].setdefault("Stop", [])
        if isinstance(proposed["hooks"]["Stop"], list):
            proposed["hooks"]["Stop"].append({
                "_source": "config.toml notify",
                "command": " ".join(json.dumps(part) if " " in part else part for part in notify),
            })
        notes.append("config.toml `notify` command proposed as a Stop hook; the JSON payload shape differs from Codex")
    if not proposed["hooks"]:
        return {}, notes
    return proposed, notes


def _render_value(val, indent: int = 0) -> list[str]:
    pad = "  " * indent
    if isinstance(val, dict):
        lines: list[str] = []
        for k, v in sorted(val.items()):
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}- `{k}`:")
                lines.extend(_render_value(v, indent + 1))
            else:
                lines.append(f"{pad}- `{k}` = `{json.dumps(v)}`")
        return lines
    if isinstance(val, list):
        return [f"{pad}- `{json.dumps(v)}`" for v in val]
    return [f"{pad}- `{json.dumps(val)}`"]


def build_migration_notes(scope_label: str, settings: dict, rules: list[dict], extra: list[str]) -> str | None:
    lines: list[str] = []
    settings = dict(settings or {})
    providers = settings.pop("model_providers", None)
    profiles = settings.pop("profiles", None)
    if settings:
        lines.append("## Codex settings not applied automatically\n")
        lines.extend(_render_value(settings))
        lines.append("")
    if providers:
        lines.append("## Custom model providers (`[model_providers.*]`)\n")
        lines.append(
            "Antigravity manages its own models; these are recorded so you can recreate any gateway, "
            "proxy or local-model setup elsewhere. Header/query-param values were NOT copied (only key names).\n"
        )
        lines.extend(_render_value(providers))
        lines.append("")
    if profiles:
        lines.append("## Codex profiles (`[profiles.*]`)\n")
        lines.append("Antigravity has no profile switcher; pick the defaults you want and drop the rest.\n")
        lines.extend(_render_value(profiles))
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


INSTRUCTION_KIND_LABEL = {
    "instructions": "User AGENTS.md",
    "legacy-instructions": "Legacy ~/.codex/instructions.md",
    "model-instructions": "model_instructions_file",
    "fallback-project-doc": "Project fallback instructions",
    "legacy-project-doc": "Legacy project codex.md",
}


def _split_enabled(items: list[dict]) -> tuple[list[dict], list[dict]]:
    on = [i for i in items if i.get("enabled", True) is not False]
    off = [i for i in items if i.get("enabled", True) is False]
    return on, off


def describe_codex_version(inv: dict) -> tuple[str, list[tuple[str, str]]]:
    """One-line version summary for the plan header + any checks it implies."""
    cx = inv.get("codex") or {}
    era = cx.get("config_era") or {}
    checks: list[tuple[str, str]] = []
    if cx.get("version"):
        line = f"Installed Codex CLI: `{cx['version']}` ({cx.get('era') or 'unknown era'}; via {cx.get('source')})"
    else:
        line = "Installed Codex CLI: version unknown"
        if cx.get("hints"):
            line += f" — {'; '.join(cx['hints'])}"
    gens = era.get("generations_present") or []
    if gens:
        line += f". Config generations on disk: {', '.join(gens)}"

    if era.get("inline_profiles") and era.get("file_profiles"):
        checks.append((
            "Profiles in two styles",
            f"both inline `[profiles.*]` and per-file {era['file_profiles']} exist; newer Codex builds read the per-file style, "
            "older ones the inline style — both were recorded in MIGRATION_NOTES.md, pick the set you actually used",
        ))
    elif era.get("file_profiles"):
        checks.append(("Per-file profiles", f"{len(era['file_profiles'])} `<name>.config.toml` overlay(s) recorded in MIGRATION_NOTES.md"))
    if cx.get("era") == "legacy-ts" and era.get("config_toml"):
        checks.append(("Version mismatch", f"installed Codex {cx['version']} is the legacy TS CLI but a Rust-era config.toml exists; the binary ignores it"))
    if cx.get("era") == "rust" and era.get("legacy_config_json") and not era.get("config_toml"):
        checks.append(("Version mismatch", f"installed Codex {cx['version']} ignores the legacy config.json found; nothing modern to migrate from config"))
    return line, checks


def build_plan(inv: dict, home: Path) -> dict:
    gemini_plugins_dir = home / ".gemini" / "config" / "plugins"
    plan_id = compute_plan_id(inv, gemini_plugins_dir)

    moves: list[tuple[str, str, str]] = []
    checks: list[tuple[str, str]] = []
    stays: list[tuple[str, str]] = []
    actions: list[dict] = []

    version_line, version_checks = describe_codex_version(inv)
    checks.extend(version_checks)

    # ---- 1. User plugin (codex-user)
    user = inv.get("user", {})
    user_dir = gemini_plugins_dir / "codex-user"
    user_skills_on, user_skills_off = _split_enabled(user.get("skills", []))
    user_action = {
        "kind": "plugin",
        "plugin_name": "codex-user",
        "display_name": "Codex User Settings",
        "description": "User-scoped skills, prompts, subagents, instructions and MCP servers migrated from OpenAI Codex CLI.",
        "target_dir": str(user_dir),
        "instructions": user.get("instructions", []),
        "rule_file": "codex-user-memory.md",
        "skills": user_skills_on,
        "prompts": user.get("prompts", []),
        "agents": user.get("agents", []),
        "mcp_servers": user.get("mcp_servers", []),
        "hooks": user.get("hooks"),
        "notify": user.get("notify", []),
        "settings": user.get("settings", {}),
        "rules": user.get("rules", []),
        "legacy_config_json": user.get("legacy_config_json"),
        "codex_version_line": version_line,
        "project_root": None,
    }
    has_user_content = any([
        user_action["instructions"],
        user_action["skills"],
        user_action["prompts"],
        user_action["agents"],
        user_action["mcp_servers"],
        user_action["hooks"],
        user_action["notify"],
        user_action["rules"],
        user_action["settings"],
    ])
    if has_user_content:
        actions.append(user_action)
        if user_action["instructions"]:
            total_bytes = sum(Path(i["path"]).stat().st_size for i in user_action["instructions"] if Path(i["path"]).is_file())
            for i in user_action["instructions"]:
                label = INSTRUCTION_KIND_LABEL.get(i.get("kind", "instructions"), "User instructions")
                moves.append((label, Path(i["path"]).name, f"{user_dir}/rules/codex-user-memory.md"))
            if total_bytes > MAX_RULE_BYTES:
                checks.append(("User instructions", f"combined size ({total_bytes} B) exceeds Antigravity 24 KB rule cap; trim after migration"))
            kinds = {i.get("kind") for i in user_action["instructions"]}
            if "legacy-instructions" in kinds and "instructions" in kinds:
                checks.append(("Legacy instructions.md", "both AGENTS.md and legacy instructions.md found; dedupe the merged rule"))
            if "model-instructions" in kinds:
                checks.append(("model_instructions_file", "Codex used this as a full system-prompt replacement; it is now an always-on rule — review tone/length"))
        for s in user_action["skills"]:
            moves.append(("User skill", s["name"], f"{user_dir}/skills/{slug(s['name'])}/"))
            if s.get("policy", {}).get("allow_implicit_invocation") is False:
                checks.append((f"Skill `{s['name']}`", "Codex openai.yaml disallowed implicit invocation; Antigravity skills are always model-invocable"))
        for s in user_skills_off:
            stays.append((f"Disabled skill `{s['name']}`", "disabled via [[skills.config]] in config.toml — not migrated (re-run after re-enabling if wanted)"))
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
        if user_action["notify"]:
            moves.append(("config.toml `notify` (disabled)", " ".join(user_action["notify"]), f"{user_dir}/hooks.proposed.json"))
            checks.append(("notify command", "proposed as a disabled Stop hook; Antigravity's payload differs from Codex's turn-complete JSON"))
        if user_action["rules"]:
            moves.append(("User .rules notes", f"{len(user_action['rules'])} file(s)", f"{user_dir}/MIGRATION_NOTES.md"))
            checks.append(("Codex .rules", "not auto-applied; review MIGRATION_NOTES.md"))
        if user_action["settings"]:
            keys = sorted(user_action["settings"].keys())
            checks.append(("Codex config.toml settings", f"recorded in MIGRATION_NOTES.md: {', '.join(keys)}"))
            providers = user_action["settings"].get("model_providers") or {}
            if providers:
                checks.append(("Custom model providers", f"{len(providers)} provider(s) ({', '.join(sorted(providers))}) recorded in MIGRATION_NOTES.md; Antigravity cannot route to them"))
            if user_action["settings"].get("shell_environment_policy", {}).get("set"):
                checks.append(("shell_environment_policy.set", "Codex injected env vars into every shell; replicate in your shell profile if MCP servers/scripts depend on them"))

    if user.get("model_instructions_missing"):
        mi = user["model_instructions_missing"]
        checks.append((f"{mi.get('config_key', 'model_instructions_file')}", f"points to `{mi['path']}` which does not exist; nothing migrated"))

    if user.get("shared_agents_skills_count", 0) > 0:
        checks.append(("~/.agents/skills/", f"{user['shared_agents_skills_count']} skill(s) already in cross-agent ~/.agents/skills/ (not duplicated)"))

    # ---- 1b. Discovery hints: trusted projects not passed via --project
    scanned_roots = {str(Path(p["root"]).resolve()) for p in inv.get("projects", [])}
    unscanned = [
        tp for tp in user.get("trusted_projects", [])
        if tp.get("trust_level", "trusted") == "trusted"
        and str(Path(tp["path"]).expanduser().resolve()) not in scanned_roots
        and Path(tp["path"]).expanduser().is_dir()
    ]
    if unscanned:
        paths = ", ".join(f"`{tp['path']}`" for tp in unscanned[:8])
        more = f" (+{len(unscanned) - 8} more)" if len(unscanned) > 8 else ""
        checks.append(("Trusted Codex projects not scanned", f"config.toml trusts {len(unscanned)} project(s) you did not pass with --project: {paths}{more}"))

    if user.get("project_doc_fallback_filenames"):
        checks.append(("project_doc_fallback_filenames", f"Codex read {user['project_doc_fallback_filenames']} as project instructions; those files are migrated per project below when AGENTS.md is absent"))

    if user.get("legacy_config_json"):
        lc = user["legacy_config_json"]
        details = ", ".join(f"{k}={json.dumps(v)}" for k, v in lc.items() if k not in {"path", "providers", "history"})
        stays.append(("Legacy TS-era config.json", f"`{lc['path']}`: {details or 'present'} — superseded by config.toml; recorded in MIGRATION_NOTES.md"))
        if lc.get("providers"):
            checks.append(("Legacy config.json providers", f"{len(lc['providers'])} custom provider(s) ({', '.join(sorted(lc['providers']))}); see MIGRATION_NOTES.md"))

    # ---- 2. Installed Codex plugins (codex-<plugin>)
    for plug in inv.get("plugins", []):
        if plug.get("enabled", True) is False:
            stays.append((f"Disabled Codex plugin `{plug['name']}`", "disabled in config.toml [plugins] — not migrated (enable in Codex and re-run, or install manually)"))
            continue
        pname = f"codex-{slug(plug['name'])}"
        pdir = gemini_plugins_dir / pname
        plug_skills_on, plug_skills_off = _split_enabled(plug.get("skills", []))
        actions.append({
            "kind": "plugin",
            "plugin_name": pname,
            "display_name": f"Codex Plugin: {plug['name']}",
            "description": f"Migrated from Codex plugin '{plug['name']}' ({plug['layout']} layout).",
            "target_dir": str(pdir),
            "instructions": [],
            "rule_file": None,
            "skills": plug_skills_on,
            "prompts": plug.get("prompts", []),
            "agents": plug.get("agents", []),
            "mcp_servers": plug.get("mcp_servers", []),
            "hooks": plug.get("hooks"),
            "notify": [],
            "settings": {},
            "rules": [],
            "project_root": None,
        })
        summary = f"{len(plug_skills_on)} skills, {len(plug.get('mcp_servers', []))} MCP"
        if plug.get("marketplace"):
            summary += f" (from marketplace `{plug['marketplace']}`)"
        moves.append((f"Codex plugin `{plug['name']}`", summary, str(pdir)))
        for s in plug_skills_off:
            stays.append((f"Plugin `{plug['name']}` skill `{s['name']}`", "disabled via [[skills.config]] — not migrated"))
        for m in plug.get("mcp_servers", []):
            _, notes = convert_mcp(m["config"], home, None)
            for n in notes:
                checks.append((f"Plugin `{plug['name']}` MCP `{m['name']}`", n))
        if plug.get("hooks"):
            checks.append((f"Plugin `{plug['name']}` hooks", "written disabled as hooks.proposed.json"))
        if plug.get("app_connectors"):
            stays.append((f"Plugin `{plug['name']}` app connectors", f"{len(plug['app_connectors'])} .app.json file(s) — no Antigravity equivalent"))

    mk = inv.get("marketplaces", {}) or {}
    if mk.get("cloned"):
        stays.append(("Codex plugin marketplaces", f"{len(mk['cloned'])} cloned marketplace repo(s) under plugins/marketplaces/ ({', '.join(mk['cloned'])}) — Antigravity installs plugins by URL instead"))
    if mk.get("user_marketplace_json"):
        umj = mk["user_marketplace_json"]
        checks.append(("~/.agents/plugins/marketplace.json", f"user marketplace `{umj.get('name') or '?'}` lists {len(umj.get('plugin_names', []))} plugin(s); install the ones you still want with `agy plugin install <url>`"))

    # ---- 3. Project plugins (<project>/.agents/plugins/codex-project)
    for proj in inv.get("projects", []):
        proot = Path(proj["root"])
        if proj.get("native_agents_md"):
            names = ", ".join(Path(x["path"]).name for x in proj["native_agents_md"])
            checks.append((f"Project `{proj['name']}` {names}", "already read natively by Antigravity (left untouched, not duplicated)"))
        if proj.get("nested_agents_md"):
            rels = ", ".join(str(Path(x["path"]).relative_to(proot)) for x in proj["nested_agents_md"][:6])
            more = f" (+{len(proj['nested_agents_md']) - 6} more)" if len(proj["nested_agents_md"]) > 6 else ""
            checks.append((f"Project `{proj['name']}` nested AGENTS.md", f"Codex concatenated {len(proj['nested_agents_md'])} nested file(s) ({rels}{more}); confirm Antigravity picks them up or merge key rules into the root AGENTS.md"))
        if proj.get("native_agents_skills"):
            checks.append((f"Project `{proj['name']}` .agents/skills/", f"{len(proj['native_agents_skills'])} skill(s) already native to Antigravity"))
        if proj.get("marketplace_json"):
            pmj = proj["marketplace_json"]
            checks.append((f"Project `{proj['name']}` .agents/plugins/marketplace.json", f"project marketplace lists {len(pmj.get('plugin_names', []))} plugin(s); reinstall with `agy plugin install` if still needed"))

        # Project instruction files Codex read because AGENTS.md was absent
        # (project_doc_fallback_filenames or legacy codex.md). Antigravity does
        # not read these natively, so they become an always-on project rule.
        fallback_docs = proj.get("fallback_project_docs", [])
        proj_instructions: list[dict] = []
        if fallback_docs and not proj.get("native_agents_md"):
            proj_instructions = fallback_docs
        elif fallback_docs:
            names = ", ".join(Path(x["path"]).name for x in fallback_docs)
            checks.append((f"Project `{proj['name']}` {names}", "found alongside AGENTS.md; Codex ignored it when AGENTS.md exists, so it was not migrated"))

        proj_dir = proot / ".agents" / "plugins" / "codex-project"
        proj_skills_on, proj_skills_off = _split_enabled(proj.get("skills", []))
        proj_action = {
            "kind": "plugin",
            "plugin_name": "codex-project",
            "display_name": f"Codex Project ({proj['name']})",
            "description": f"Project-scoped Codex skills, prompts, subagents and MCP servers for {proj['name']}.",
            "target_dir": str(proj_dir),
            "instructions": proj_instructions,
            "rule_file": "codex-project-instructions.md",
            "skills": proj_skills_on,
            "prompts": proj.get("prompts", []),
            "agents": proj.get("agents", []),
            "mcp_servers": proj.get("mcp_servers", []),
            "hooks": proj.get("hooks"),
            "notify": [],
            "settings": proj.get("settings", {}),
            "rules": proj.get("rules", []),
            "project_root": str(proot),
        }
        has_proj_content = any([
            proj_action["instructions"],
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
            for i in proj_action["instructions"]:
                label = INSTRUCTION_KIND_LABEL.get(i.get("kind"), "Project instructions")
                moves.append((f"Project `{proj['name']}` {label}", Path(i["path"]).name, f"{proj_dir}/rules/codex-project-instructions.md"))
            for s in proj_action["skills"]:
                moves.append((f"Project `{proj['name']}` skill", s["name"], f"{proj_dir}/skills/{slug(s['name'])}/"))
            for s in proj_skills_off:
                stays.append((f"Project `{proj['name']}` skill `{s['name']}`", "disabled via [[skills.config]] — not migrated"))
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
    if sb.get("memory_files_count", 0) > 0:
        checks.append(("Codex memories/", f"{sb['memory_files_count']} agent-written memory file(s) in ~/.codex/memories/ — not opened; skim them and move anything durable into AGENTS.md yourself"))
    if user.get("settings", {}).get("model"):
        stays.append(("Model / reasoning defaults", f"model={user['settings'].get('model')} — Antigravity manages models separately"))
    if inv.get("managed_config_present"):
        checks.append(("Managed / enterprise config layers", f"{', '.join(inv['managed_config_present'])} present — your effective Codex settings may differ from ~/.codex/config.toml; this tool only migrated the user layer"))

    return {
        "plan_id": plan_id,
        "version_line": version_line,
        "moves": moves,
        "checks": checks,
        "stays": stays,
        "actions": actions,
    }


def render_plan(plan: dict) -> str:
    lines = [f"# Codex → Antigravity Migration Plan (Plan ID: `{plan['plan_id']}`)\n"]
    if plan.get("version_line"):
        lines.append(f"_{plan['version_line']}_\n")

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

    # 1. Instructions -> rules/<rule_file> (always_on)
    rule_file = action.get("rule_file") or "codex-user-memory.md"
    if action["instructions"]:
        rules_dir = target / "rules"
        rules_dir.mkdir(parents=True, exist_ok=True)
        parts: list[str] = []
        for entry in action["instructions"]:
            p = Path(entry["path"])
            if p.is_file():
                label = INSTRUCTION_KIND_LABEL.get(entry.get("kind", "instructions"), "Instructions")
                parts.append(f"<!-- Migrated from {p} ({label}) -->\n\n" + p.read_text(encoding="utf-8").strip())
        rule_text = frontmatter({"trigger": "always_on"}) + "\n" + "\n\n".join(parts) + "\n"
        (rules_dir / rule_file).write_text(rule_text, encoding="utf-8")
        if len(rule_text.encode("utf-8")) > MAX_RULE_BYTES:
            extra_notes.append(f"`rules/{rule_file}` exceeds 24 KB; trim it so Antigravity loads it cleanly.")

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

    # 6. Hooks + notify (always written disabled as hooks.proposed.json)
    hooks_path = Path(action["hooks"]["path"]) if action.get("hooks") else None
    notify = action.get("notify") or []
    if hooks_path is not None or notify:
        proposed, notes = convert_hooks_proposal(hooks_path, notify)
        if proposed:
            (target / "hooks.proposed.json").write_text(
                json.dumps(proposed, indent=2) + "\n",
                encoding="utf-8",
            )
        for n in notes:
            extra_notes.append(f"Hooks: {n}")

    # 7. Legacy TS-era config.json (user plugin only)
    legacy = action.get("legacy_config_json")
    if legacy:
        extra_notes.append(
            "Legacy `~/.codex/config.json` (TypeScript-era Codex CLI) found: "
            + json.dumps({k: v for k, v in legacy.items() if k != "path"}, sort_keys=True)
        )

    # 8. Migration notes
    if action.get("codex_version_line"):
        extra_notes.insert(0, action["codex_version_line"])
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
