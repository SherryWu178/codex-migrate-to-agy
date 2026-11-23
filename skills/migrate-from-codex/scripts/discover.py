#!/usr/bin/env python3
"""Read-only inventory of an OpenAI Codex CLI setup, for migration to Antigravity.

Prints JSON describing what was found. Never writes files, never opens
credential files (auth.json), and never opens session transcripts (it only
counts them).

Inspired by pauldatta/claude-migrate-to-agy (Apache-2.0).

Usage:
  python3 discover.py [--home DIR] [--codex-home DIR] [--project DIR ...] [--pretty]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from pathlib import Path

# Files that may hold credentials or tokens. Never opened.
CREDENTIAL_NAMES = {"auth.json", ".credentials.json", "credentials.json"}

# Non-secret top-level keys in config.toml that we record as migration notes.
NOTABLE_SETTINGS = (
    "model",
    "model_provider",
    "model_reasoning_effort",
    "approval_policy",
    "sandbox_mode",
    "web_search",
)


# ---------------------------------------------------------------- helpers
def read_json(path: Path) -> dict | None:
    try:
        with path.open(encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def read_toml(path: Path) -> dict | None:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Minimal YAML-frontmatter reader (flat keys, scalar or list values)."""
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


def md_entry(path: Path, kind: str, scope: str) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"kind": kind, "scope": scope, "path": str(path), "error": str(exc)}
    meta, body = split_frontmatter(text)
    return {
        "kind": kind,
        "scope": scope,
        "path": str(path),
        "meta": meta,
        "chars": len(text),
        "body_chars": len(body),
    }


def list_md(dirpath: Path, kind: str, scope: str) -> list[dict]:
    if not dirpath.is_dir():
        return []
    return [md_entry(p, kind, scope) for p in sorted(dirpath.glob("*.md")) if p.is_file()]


def list_agents(dirpath: Path, scope: str) -> list[dict]:
    """List custom subagents defined as .md or .toml files."""
    out: list[dict] = []
    if not dirpath.is_dir():
        return out
    for p in sorted(dirpath.iterdir()):
        if not p.is_file():
            continue
        if p.suffix == ".md":
            out.append(md_entry(p, "agent", scope))
        elif p.suffix == ".toml":
            data = read_toml(p)
            if data is None:
                out.append({"kind": "agent", "scope": scope, "path": str(p), "error": "invalid toml"})
            else:
                out.append({
                    "kind": "agent",
                    "format": "toml",
                    "scope": scope,
                    "path": str(p),
                    "meta": {
                        "name": data.get("name") or p.stem,
                        "description": data.get("description", ""),
                        "model": data.get("model", ""),
                        "sandbox_mode": data.get("sandbox_mode", ""),
                        "developer_instructions": bool(data.get("developer_instructions")),
                    },
                    "chars": p.stat().st_size,
                })
    return out


def list_skills(dirpath: Path, scope: str) -> list[dict]:
    out: list[dict] = []
    if not dirpath.is_dir():
        return out
    for skill_md in sorted(dirpath.glob("*/SKILL.md")):
        entry = md_entry(skill_md, "skill", scope)
        entry["dir"] = str(skill_md.parent)
        entry["name"] = entry.get("meta", {}).get("name") or skill_md.parent.name
        out.append(entry)
    return out


def list_rule_files(dirpath: Path, scope: str) -> list[dict]:
    out: list[dict] = []
    if not dirpath.is_dir():
        return out
    for p in sorted(dirpath.glob("*.rules")):
        if not p.is_file():
            continue
        try:
            lines = len(p.read_text(encoding="utf-8").splitlines())
        except OSError:
            lines = 0
        out.append({"scope": scope, "path": str(p), "name": p.name, "lines": lines})
    return out


def mcp_servers_from_toml(data: dict | None, source: str) -> list[dict]:
    out: list[dict] = []
    if not data:
        return out
    servers = data.get("mcp_servers")
    if isinstance(servers, dict):
        for name, cfg in sorted(servers.items()):
            if isinstance(cfg, dict):
                out.append({"name": name, "source": source, "config": cfg})
    return out


def mcp_servers_from_json(data: dict | None, source: str) -> list[dict]:
    out: list[dict] = []
    if not data:
        return out
    servers = data.get("mcpServers")
    if isinstance(servers, dict):
        for name, cfg in sorted(servers.items()):
            if isinstance(cfg, dict):
                out.append({"name": name, "source": source, "config": cfg})
    return out


def extract_settings_summary(data: dict | None) -> dict:
    if not data:
        return {}
    summary: dict = {}
    for key in NOTABLE_SETTINGS:
        if key in data:
            summary[key] = data[key]
    profiles = data.get("profiles")
    if isinstance(profiles, dict) and profiles:
        summary["profiles"] = sorted(profiles.keys())
    return summary


def version_key(path: Path) -> tuple:
    parts = []
    for piece in path.name.replace("-", ".").split("."):
        parts.append((0, int(piece)) if piece.isdigit() else (1, piece))
    return tuple(parts)


def read_hooks_file(path: Path) -> dict | None:
    if not path.is_file():
        return None
    data = read_json(path)
    if not data:
        return None
    hooks = data.get("hooks", data)
    return hooks if isinstance(hooks, dict) and hooks else None


# ---------------------------------------------------------------- plugins
def find_plugin_manifest(root: Path) -> dict | None:
    for rel in (".codex-plugin/plugin.json", "plugin.json"):
        p = root / rel
        if p.is_file():
            data = read_json(p)
            if data:
                return data
    return None


def plugin_record(name: str, root: Path, layout: str, marketplace: str | None) -> dict:
    mcp_servers: list[dict] = []
    for rel in ("mcp.json", ".mcp.json", ".codex-plugin/mcp.json"):
        p = root / rel
        if p.is_file():
            mcp_servers.extend(mcp_servers_from_json(read_json(p), str(p)))
            break
    hooks = None
    for rel in ("hooks/hooks.json", "hooks.json"):
        h = read_hooks_file(root / rel)
        if h:
            hooks = {"path": str(root / rel), "events": sorted(h.keys())}
            break
    return {
        "name": name,
        "marketplace": marketplace,
        "layout": layout,
        "root": str(root),
        "skills": list_skills(root / "skills", f"plugin:{name}"),
        "prompts": list_md(root / "prompts", "prompt", f"plugin:{name}"),
        "agents": list_agents(root / "agents", f"plugin:{name}"),
        "mcp_servers": mcp_servers,
        "hooks": hooks,
    }


def discover_plugins(codex_dir: Path) -> list[dict]:
    plugins_dir = codex_dir / "plugins"
    if not plugins_dir.is_dir():
        return []

    found: list[dict] = []
    seen_names: set[str] = set()

    # 1. Cache layout: ~/.codex/plugins/cache/<marketplace>/<plugin>/<version>/
    cache_dir = plugins_dir / "cache"
    if cache_dir.is_dir():
        for mkt_dir in sorted(p for p in cache_dir.iterdir() if p.is_dir()):
            for plug_dir in sorted(p for p in mkt_dir.iterdir() if p.is_dir()):
                versions = sorted(
                    (v for v in plug_dir.iterdir() if v.is_dir()),
                    key=version_key,
                    reverse=True,
                )
                for ver_dir in versions:
                    manifest = find_plugin_manifest(ver_dir)
                    if manifest is not None or (ver_dir / "skills").is_dir():
                        name = (manifest or {}).get("name") or plug_dir.name
                        if name not in seen_names:
                            seen_names.add(name)
                            found.append(plugin_record(name, ver_dir, "cache", mkt_dir.name))
                        break

    # 2. Flat layout: ~/.codex/plugins/<plugin>/
    for sub in sorted(p for p in plugins_dir.iterdir() if p.is_dir()):
        if sub.name in {"cache", "marketplaces"}:
            continue
        manifest = find_plugin_manifest(sub)
        if manifest is not None or (sub / "skills").is_dir():
            name = (manifest or {}).get("name") or sub.name
            if name not in seen_names:
                seen_names.add(name)
                found.append(plugin_record(name, sub, "flat", None))

    return found


def count_sessions(codex_dir: Path) -> int:
    """Count session transcript files without opening any of them."""
    total = 0
    for sub in ("sessions", "archived_sessions"):
        d = codex_dir / sub
        if d.is_dir():
            for _root, _dirs, files in os.walk(d):
                total += sum(1 for f in files if f.endswith((".jsonl", ".json")))
    if (codex_dir / "history.jsonl").is_file():
        total += 1
    return total


# ---------------------------------------------------------------- main
def discover(home: Path, codex_home: Path, projects: list[Path]) -> dict:
    user_config_path = codex_home / "config.toml"
    user_config = read_toml(user_config_path) if user_config_path.is_file() else None

    user_instructions = []
    for fname in ("AGENTS.md", "AGENTS.override.md"):
        p = codex_home / fname
        if p.is_file():
            user_instructions.append(md_entry(p, "instructions", "user"))

    # Skills in $CODEX_HOME/skills/ (and note ~/.agents/skills/ if present)
    user_skills = list_skills(codex_home / "skills", "user")
    shared_agents_skills = list_skills(home / ".agents" / "skills", "shared-agents")

    user_hooks_path = codex_home / "hooks.json"
    user_hooks = read_hooks_file(user_hooks_path)

    credentials_present = [
        name for name in sorted(CREDENTIAL_NAMES) if (codex_home / name).is_file()
    ]

    project_records: list[dict] = []
    resolved_home = home.resolve()
    seen_projects: set[Path] = set()
    for raw_proj in projects:
        proj = raw_proj.expanduser().resolve()
        if proj == resolved_home or proj in seen_projects or not proj.is_dir():
            continue
        seen_projects.add(proj)

        proj_codex = proj / ".codex"
        proj_cfg_path = proj_codex / "config.toml"
        proj_cfg = read_toml(proj_cfg_path) if proj_cfg_path.is_file() else None

        native_agents_md = []
        for fname in ("AGENTS.md", "AGENTS.override.md"):
            p = proj / fname
            if p.is_file():
                native_agents_md.append(md_entry(p, "native-instructions", "project"))

        proj_hooks_path = proj_codex / "hooks.json"
        proj_hooks = read_hooks_file(proj_hooks_path)

        project_records.append({
            "root": str(proj),
            "name": proj.name,
            "native_agents_md": native_agents_md,
            "native_agents_skills": list_skills(proj / ".agents" / "skills", "project-native"),
            "skills": list_skills(proj_codex / "skills", "project"),
            "prompts": list_md(proj_codex / "prompts", "prompt", "project"),
            "agents": list_agents(proj_codex / "agents", "project"),
            "mcp_servers": mcp_servers_from_toml(proj_cfg, str(proj_cfg_path)),
            "settings": extract_settings_summary(proj_cfg),
            "hooks": {"path": str(proj_hooks_path), "events": sorted(proj_hooks.keys())} if proj_hooks else None,
            "rules": list_rule_files(proj_codex / "rules", "project"),
        })

    return {
        "schema_version": 1,
        "home": str(home),
        "codex_home": str(codex_home),
        "codex_dir_exists": codex_home.is_dir(),
        "user": {
            "instructions": user_instructions,
            "skills": user_skills,
            "shared_agents_skills_count": len(shared_agents_skills),
            "prompts": list_md(codex_home / "prompts", "prompt", "user"),
            "agents": list_agents(codex_home / "agents", "user"),
            "mcp_servers": mcp_servers_from_toml(user_config, str(user_config_path)),
            "settings": extract_settings_summary(user_config),
            "hooks": {"path": str(user_hooks_path), "events": sorted(user_hooks.keys())} if user_hooks else None,
            "rules": list_rule_files(codex_home / "rules", "user"),
        },
        "plugins": discover_plugins(codex_home),
        "projects": project_records,
        "stays_behind": {
            "credentials_present": credentials_present,
            "session_files_count": count_sessions(codex_home),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only inventory of an OpenAI Codex CLI setup.")
    parser.add_argument("--home", type=Path, default=Path.home(), help="User home directory (default: ~)")
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=None,
        help="Codex config directory (default: $CODEX_HOME or <home>/.codex)",
    )
    parser.add_argument("--project", type=Path, action="append", default=[], help="Project root to scan (repeatable)")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    args = parser.parse_args(argv)

    home = args.home.expanduser().resolve()
    if args.codex_home is not None:
        codex_home = args.codex_home.expanduser().resolve()
    elif os.environ.get("CODEX_HOME"):
        codex_home = Path(os.environ["CODEX_HOME"]).expanduser().resolve()
    else:
        codex_home = home / ".codex"

    inv = discover(home, codex_home, args.project)
    json.dump(inv, sys.stdout, indent=2 if args.pretty else None)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
