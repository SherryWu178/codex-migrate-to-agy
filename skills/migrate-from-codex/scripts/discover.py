#!/usr/bin/env python3
"""Read-only inventory of an OpenAI Codex CLI setup, for migration to Antigravity.

Prints JSON describing what was found. Never writes files, never opens
credential files (auth.json, .env, token stores), and never opens session
transcripts or agent memories (it only counts them).

Inspired by pauldatta/claude-migrate-to-agy (Apache-2.0).

Usage:
  python3 discover.py [--home DIR] [--codex-home DIR] [--project DIR ...] [--no-exec] [--pretty]

Version detection: by default `codex --version` is run (5 s timeout, honours
$CODEX_BIN) purely to *report* the installed version. Config parsing never
branches on the version number; it branches on which files/keys exist, since
users routinely have config left over from several Codex releases. Pass
--no-exec to skip spawning the binary.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
# The TypeScript-era CLI shipped as 0.1.x; the Rust rewrite took over from 0.2.0.
# This is the only version boundary we rely on, and only for labelling.
RUST_CLI_MIN = (0, 2, 0)

# Files that may hold credentials or tokens. Never opened.
CREDENTIAL_NAMES = {"auth.json", ".credentials.json", "credentials.json", ".env"}
# Any file in $CODEX_HOME whose name contains one of these is treated as a
# credential store (e.g. MCP OAuth token caches). Never opened.
CREDENTIAL_NAME_HINTS = ("oauth", "token", "secret", "credential")

# Scalar top-level keys in config.toml recorded as migration notes.
NOTABLE_SETTINGS = (
    "model",
    "model_provider",
    "model_reasoning_effort",
    "model_reasoning_summary",
    "model_verbosity",
    "model_context_window",
    "model_max_output_tokens",
    "model_auto_compact_token_limit",
    "approval_policy",
    "sandbox_mode",
    "web_search",
    "profile",
    "project_doc_max_bytes",
    "file_opener",
    "hide_agent_reasoning",
    "show_raw_agent_reasoning",
    "check_for_update_on_startup",
    "disable_response_storage",
    "preferred_auth_method",
)

# Table-valued keys in config.toml recorded verbatim as migration notes.
NOTABLE_TABLES = (
    "sandbox_workspace_write",
    "shell_environment_policy",
    "features",
    "tools",
    "agents",
    "history",
    "tui",
    "otel",
)

# Keys of [model_providers.*] whose *values* are safe to record. Header and
# query-param values may be literal secrets, so only their key names are kept.
PROVIDER_SAFE_KEYS = (
    "name",
    "base_url",
    "env_key",
    "wire_api",
    "requires_openai_auth",
    "request_max_retries",
    "stream_max_retries",
    "stream_idle_timeout_ms",
)

# Legacy TypeScript-era Codex CLI files.
LEGACY_USER_INSTRUCTIONS = "instructions.md"
LEGACY_USER_CONFIG = "config.json"
LEGACY_PROJECT_DOCS = ("codex.md", "CODEX.md", ".codex.md")

# Directories never descended into when looking for nested AGENTS.md.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", "target", ".next", ".turbo", ".cache", ".agents", ".codex",
    ".gemini", ".idea", ".vscode",
}
NESTED_AGENTS_MAX_DEPTH = 6

# System / enterprise-managed config layers (presence only; never parsed).
MANAGED_CONFIG_CANDIDATES = (
    "/etc/codex/config.toml",
    "/etc/codex/requirements.toml",
    "/etc/codex/managed_config.toml",
)


# ---------------------------------------------------------------- version
def _parse_semver(text: str) -> str | None:
    m = SEMVER_RE.search(text or "")
    return ".".join(m.groups()) if m else None


def _semver_tuple(version: str | None) -> tuple[int, int, int] | None:
    m = SEMVER_RE.search(version or "")
    return tuple(int(g) for g in m.groups()) if m else None  # type: ignore[return-value]


def detect_codex_version(codex_dir: Path, allow_exec: bool) -> dict:
    """Best-effort installed-version detection. Reported, never relied upon.

    Order: `codex --version` (if allowed) -> version embedded in the resolved
    binary path (Homebrew Cellar / npm) -> ~/.codex/version.json (which only
    records the *latest known* release, so it is labelled as a hint).
    """
    out: dict = {"version": None, "source": None, "binary": None, "era": None, "hints": []}
    binary = os.environ.get("CODEX_BIN") or shutil.which("codex")
    if binary:
        out["binary"] = binary
        try:
            resolved = str(Path(binary).resolve())
        except OSError:
            resolved = binary
        if allow_exec:
            try:
                proc = subprocess.run(
                    [binary, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                    env={**os.environ, "NO_COLOR": "1"},
                )
                ver = _parse_semver(proc.stdout) or _parse_semver(proc.stderr)
                if ver:
                    out["version"], out["source"] = ver, "codex --version"
                else:
                    out["hints"].append("`codex --version` ran but printed no semver")
            except (OSError, subprocess.SubprocessError) as exc:
                out["hints"].append(f"`codex --version` failed: {exc.__class__.__name__}")
        else:
            out["hints"].append("binary found but --no-exec given; version not queried")
        if out["version"] is None:
            # e.g. /opt/homebrew/Cellar/codex/0.143.0/bin/codex or
            #      .../node_modules/@openai/codex/.../codex-0.143.0
            ver = _parse_semver(resolved)
            if ver:
                out["version"], out["source"] = ver, f"binary path ({resolved})"
    else:
        out["hints"].append("no `codex` binary on PATH (set $CODEX_BIN to override)")

    vj = read_json(codex_dir / "version.json")
    if vj:
        latest = _parse_semver(str(vj.get("latest_version") or ""))
        if latest:
            out["latest_known_release"] = latest
            if out["version"] is None:
                out["hints"].append(f"version.json says latest known release is {latest} (not necessarily installed)")

    tup = _semver_tuple(out["version"])
    if tup is not None:
        out["era"] = "rust" if tup >= RUST_CLI_MIN else "legacy-ts"
    return out


def detect_config_era(codex_dir: Path, version_info: dict) -> dict:
    """Feature-detect which config generations are physically present.

    Users frequently carry files from several releases, so every generation
    found is reported and parsed; the version only helps label which one the
    installed binary actually honours.
    """
    has_toml = (codex_dir / "config.toml").is_file()
    has_json = (codex_dir / LEGACY_USER_CONFIG).is_file()
    file_profiles = sorted(p.name for p in codex_dir.glob("*.config.toml") if p.is_file())
    inline_profiles = False
    if has_toml:
        data = read_toml(codex_dir / "config.toml") or {}
        inline_profiles = isinstance(data.get("profiles"), dict) and bool(data["profiles"])
    generations: list[str] = []
    if has_json:
        generations.append("legacy-ts (config.json)")
    if has_toml:
        generations.append("rust (config.toml)")
    if file_profiles:
        generations.append("rust, per-file profiles (<name>.config.toml)")
    return {
        "generations_present": generations,
        "config_toml": has_toml,
        "legacy_config_json": has_json,
        "inline_profiles": inline_profiles,
        "file_profiles": file_profiles,
        "installed_era": version_info.get("era"),
    }


def extract_file_profiles(codex_dir: Path) -> dict:
    """Newer Codex releases accept ~/.codex/<name>.config.toml as profile overlays."""
    out: dict = {}
    for p in sorted(codex_dir.glob("*.config.toml")):
        if not p.is_file():
            continue
        name = p.name[: -len(".config.toml")]
        if not name:
            continue
        data = read_toml(p)
        if data is None:
            out[name] = {"_source": str(p), "_error": "invalid toml"}
            continue
        rec = _profile_summary(data)
        rec["_source"] = str(p)
        providers = extract_model_providers(data)
        if providers:
            rec["model_providers"] = providers
        mcp = data.get("mcp_servers")
        if isinstance(mcp, dict) and mcp:
            rec["mcp_server_names"] = sorted(str(k) for k in mcp)
        out[name] = rec
    return out


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


def read_skill_policy(skill_dir: Path) -> dict:
    """Read the optional agents/openai.yaml next to SKILL.md (minimal parse).

    Only `policy.allow_implicit_invocation` is extracted; everything else is
    ignored. Returns {} when the file is absent or unparseable.
    """
    p = skill_dir / "agents" / "openai.yaml"
    if not p.is_file():
        return {}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict = {}
    in_policy = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            in_policy = stripped.rstrip(":") == "policy"
            continue
        if in_policy and ":" in stripped:
            k, _, v = stripped.partition(":")
            if k.strip() == "allow_implicit_invocation":
                out["allow_implicit_invocation"] = v.strip().lower() not in {"false", "no", "0"}
    return out


def list_skills(dirpath: Path, scope: str) -> list[dict]:
    out: list[dict] = []
    if not dirpath.is_dir():
        return out
    for skill_md in sorted(dirpath.glob("*/SKILL.md")):
        entry = md_entry(skill_md, "skill", scope)
        entry["dir"] = str(skill_md.parent)
        entry["name"] = entry.get("meta", {}).get("name") or skill_md.parent.name
        entry["enabled"] = True
        policy = read_skill_policy(skill_md.parent)
        if policy:
            entry["policy"] = policy
        out.append(entry)
    return out


def apply_skill_config(skills: list[dict], skills_config: list[dict]) -> None:
    """Mark skills disabled per `[[skills.config]]` (last matching rule wins)."""
    if not skills_config:
        return
    for sk in skills:
        sk_dir = Path(sk["dir"]).resolve()
        sk_md = sk_dir / "SKILL.md"
        for rule in skills_config:
            if "enabled" not in rule:
                continue
            matched = False
            rule_path = rule.get("path")
            if rule_path:
                rp = Path(str(rule_path)).expanduser()
                try:
                    rp = rp.resolve()
                except OSError:
                    pass
                matched = rp in (sk_dir, sk_md)
            rule_name = rule.get("name")
            if not matched and rule_name:
                matched = str(rule_name) == sk["name"] or str(rule_name) == Path(sk["dir"]).name
            if matched:
                sk["enabled"] = bool(rule["enabled"])
                sk["enabled_by"] = "skills.config"


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


def _profile_summary(profile: dict) -> dict:
    out: dict = {}
    for key in NOTABLE_SETTINGS:
        if key in profile:
            out[key] = profile[key]
    for key in NOTABLE_TABLES:
        if isinstance(profile.get(key), dict):
            out[key] = profile[key]
    return out


def extract_model_providers(data: dict | None) -> dict:
    """Record [model_providers.*] without copying header/query-param values."""
    out: dict = {}
    providers = (data or {}).get("model_providers")
    if not isinstance(providers, dict):
        return out
    for pid, cfg in sorted(providers.items()):
        if not isinstance(cfg, dict):
            continue
        rec: dict = {k: cfg[k] for k in PROVIDER_SAFE_KEYS if k in cfg}
        for hk in ("http_headers", "query_params"):
            if isinstance(cfg.get(hk), dict):
                rec[f"{hk}_keys"] = sorted(str(k) for k in cfg[hk].keys())
        if isinstance(cfg.get("env_http_headers"), dict):
            rec["env_http_headers"] = {str(k): str(v) for k, v in cfg["env_http_headers"].items()}
        # Newer releases: [model_providers.X.auth] command = ..., args = [...]
        # runs a credential helper. Record the command (not its output).
        auth = cfg.get("auth")
        if isinstance(auth, dict) and auth.get("command"):
            rec["auth_command"] = [str(auth["command"])] + [str(a) for a in auth.get("args", []) if a is not None]
        out[str(pid)] = rec
    return out


def extract_settings_summary(data: dict | None) -> dict:
    if not data:
        return {}
    summary: dict = {}
    for key in NOTABLE_SETTINGS:
        if key in data:
            summary[key] = data[key]
    for key in NOTABLE_TABLES:
        if isinstance(data.get(key), dict) and data[key]:
            summary[key] = data[key]
    profiles = data.get("profiles")
    if isinstance(profiles, dict) and profiles:
        summary["profiles"] = {
            str(name): {**_profile_summary(p), "_source": "inline [profiles.*]"}
            for name, p in sorted(profiles.items()) if isinstance(p, dict)
        }
    providers = extract_model_providers(data)
    if providers:
        summary["model_providers"] = providers
    return summary


def extract_skills_config(data: dict | None) -> list[dict]:
    skills = (data or {}).get("skills")
    if not isinstance(skills, dict):
        return []
    cfg = skills.get("config")
    if not isinstance(cfg, list):
        return []
    return [c for c in cfg if isinstance(c, dict)]


def extract_notify(data: dict | None) -> list[str]:
    notify = (data or {}).get("notify")
    if isinstance(notify, list) and notify:
        return [str(x) for x in notify]
    if isinstance(notify, str) and notify:
        return [notify]
    return []


def extract_trusted_projects(data: dict | None) -> list[dict]:
    projects = (data or {}).get("projects")
    if not isinstance(projects, dict):
        return []
    out: list[dict] = []
    for path, cfg in sorted(projects.items()):
        rec = {"path": str(path)}
        if isinstance(cfg, dict) and "trust_level" in cfg:
            rec["trust_level"] = str(cfg["trust_level"])
        out.append(rec)
    return out


def extract_plugin_states(data: dict | None) -> dict[str, bool]:
    """Map '<plugin>@<marketplace>' (and bare '<plugin>') -> enabled flag."""
    plugins = (data or {}).get("plugins")
    if not isinstance(plugins, dict):
        return {}
    out: dict[str, bool] = {}
    for key, cfg in plugins.items():
        if isinstance(cfg, dict) and "enabled" in cfg:
            enabled = bool(cfg["enabled"])
            out[str(key)] = enabled
            bare = str(key).split("@", 1)[0]
            out.setdefault(bare, enabled)
    return out


def resolve_instructions_file(data: dict | None, home: Path, codex_home: Path) -> dict | None:
    """Resolve `model_instructions_file` (or legacy `experimental_instructions_file`)."""
    raw = None
    key_used = None
    for key in ("model_instructions_file", "experimental_instructions_file"):
        val = (data or {}).get(key)
        if isinstance(val, str) and val.strip():
            raw, key_used = val.strip(), key
            break
    if raw is None:
        return None
    p = Path(raw.replace("$CODEX_HOME", str(codex_home)).replace("${CODEX_HOME}", str(codex_home)))
    p = Path(str(p).replace("$HOME", str(home)).replace("${HOME}", str(home))).expanduser()
    if not p.is_absolute():
        p = codex_home / p
    if not p.is_file():
        return {"kind": "model-instructions", "scope": "user", "path": str(p), "config_key": key_used, "error": "file not found"}
    entry = md_entry(p, "model-instructions", "user")
    entry["config_key"] = key_used
    return entry


def legacy_config_summary(path: Path) -> dict | None:
    """Summarize the TypeScript-era ~/.codex/config.json without copying secrets."""
    data = read_json(path)
    if data is None:
        return None
    out: dict = {"path": str(path)}
    for key in ("model", "provider", "approvalMode", "fullAutoErrorMode", "notify", "reasoningEffort", "flexMode"):
        if key in data:
            out[key] = data[key]
    if isinstance(data.get("history"), dict):
        out["history"] = data["history"]
    providers = data.get("providers")
    if isinstance(providers, dict):
        out["providers"] = {
            str(name): {k: v for k, v in cfg.items() if k in ("name", "baseURL", "envKey")}
            for name, cfg in sorted(providers.items())
            if isinstance(cfg, dict)
        }
    return out


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


def find_nested_agents_md(root: Path) -> list[dict]:
    """AGENTS.md / AGENTS.override.md below the project root (Codex concatenates these)."""
    out: list[dict] = []
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        rel_depth = len(Path(dirpath).relative_to(root).parts)
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        if rel_depth >= NESTED_AGENTS_MAX_DEPTH:
            dirnames[:] = []
        if rel_depth == 0:
            continue
        for fname in ("AGENTS.md", "AGENTS.override.md"):
            if fname in filenames:
                out.append(md_entry(Path(dirpath) / fname, "nested-instructions", "project"))
    return out


def find_fallback_project_docs(root: Path, fallback_names: list[str]) -> list[dict]:
    """Project instruction files Codex would read when AGENTS.md is absent."""
    out: list[dict] = []
    seen: set[Path] = set()
    for fname in list(fallback_names) + list(LEGACY_PROJECT_DOCS):
        p = root / fname
        if p.is_file() and p.resolve() not in seen:
            seen.add(p.resolve())
            kind = "legacy-project-doc" if fname in LEGACY_PROJECT_DOCS else "fallback-project-doc"
            out.append(md_entry(p, kind, "project"))
    return out


def read_marketplace_json(path: Path) -> dict | None:
    data = read_json(path)
    if data is None:
        return None
    plugins = data.get("plugins")
    names: list[str] = []
    if isinstance(plugins, list):
        for p in plugins:
            if isinstance(p, dict) and p.get("name"):
                names.append(str(p["name"]))
    return {"path": str(path), "name": data.get("name"), "plugin_names": names}


def detect_credential_files(codex_dir: Path) -> list[str]:
    """Names of credential-like files in $CODEX_HOME. Never opened."""
    found: set[str] = set()
    if not codex_dir.is_dir():
        return []
    for p in codex_dir.iterdir():
        if not p.is_file():
            continue
        lower = p.name.lower()
        if p.name in CREDENTIAL_NAMES or any(h in lower for h in CREDENTIAL_NAME_HINTS):
            found.add(p.name)
    return sorted(found)


def detect_managed_config(home: Path, codex_dir: Path) -> list[str]:
    candidates = [Path(c) for c in MANAGED_CONFIG_CANDIDATES]
    candidates += [codex_dir / "requirements.toml", codex_dir / "managed_config.toml"]
    program_data = os.environ.get("ProgramData")
    if program_data:
        for fname in ("config.toml", "requirements.toml", "managed_config.toml"):
            candidates.append(Path(program_data) / "OpenAI" / "Codex" / fname)
    return [str(c) for c in candidates if c.is_file()]


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
    app_connectors = [str(root / rel) for rel in (".app.json", ".codex-plugin/app.json") if (root / rel).is_file()]
    return {
        "name": name,
        "marketplace": marketplace,
        "layout": layout,
        "root": str(root),
        "enabled": True,
        "skills": list_skills(root / "skills", f"plugin:{name}"),
        "prompts": list_md(root / "prompts", "prompt", f"plugin:{name}"),
        "agents": list_agents(root / "agents", f"plugin:{name}"),
        "mcp_servers": mcp_servers,
        "hooks": hooks,
        "app_connectors": app_connectors,
    }


def discover_plugins(codex_dir: Path, plugin_states: dict[str, bool]) -> list[dict]:
    plugins_dir = codex_dir / "plugins"
    if not plugins_dir.is_dir():
        return []

    found: list[dict] = []
    seen_names: set[str] = set()

    def finalize(rec: dict) -> dict:
        key = f"{rec['name']}@{rec['marketplace']}" if rec.get("marketplace") else rec["name"]
        state = plugin_states.get(key, plugin_states.get(rec["name"]))
        if state is not None:
            rec["enabled"] = state
            rec["enabled_by"] = "config.toml [plugins]"
        return rec

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
                            found.append(finalize(plugin_record(name, ver_dir, "cache", mkt_dir.name)))
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
                found.append(finalize(plugin_record(name, sub, "flat", None)))

    return found


def discover_marketplaces(home: Path, codex_dir: Path) -> dict:
    cloned = []
    mdir = codex_dir / "plugins" / "marketplaces"
    if mdir.is_dir():
        cloned = sorted(p.name for p in mdir.iterdir() if p.is_dir())
    user_mj = read_marketplace_json(home / ".agents" / "plugins" / "marketplace.json")
    return {"cloned": cloned, "user_marketplace_json": user_mj}


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


def count_memories(codex_dir: Path) -> int:
    """Count agent-written memory files without opening them."""
    d = codex_dir / "memories"
    if not d.is_dir():
        return 0
    total = 0
    for _root, _dirs, files in os.walk(d):
        total += sum(1 for f in files if not f.startswith("."))
    return total


# ---------------------------------------------------------------- main
def discover(home: Path, codex_home: Path, projects: list[Path], allow_exec: bool = True) -> dict:
    version_info = detect_codex_version(codex_home, allow_exec)
    config_era = detect_config_era(codex_home, version_info)

    user_config_path = codex_home / "config.toml"
    user_config = read_toml(user_config_path) if user_config_path.is_file() else None
    user_settings = extract_settings_summary(user_config)

    # Per-file profiles (<name>.config.toml) are merged next to inline ones;
    # each entry carries `_source` so the notes show which style it came from.
    file_profiles = extract_file_profiles(codex_home)
    if file_profiles:
        merged = dict(user_settings.get("profiles", {}))
        for name, rec in file_profiles.items():
            merged[name if name not in merged else f"{name} (file)"] = rec
        user_settings["profiles"] = merged

    user_instructions = []
    for fname in ("AGENTS.md", "AGENTS.override.md"):
        p = codex_home / fname
        if p.is_file():
            user_instructions.append(md_entry(p, "instructions", "user"))
    legacy_instr = codex_home / LEGACY_USER_INSTRUCTIONS
    if legacy_instr.is_file():
        user_instructions.append(md_entry(legacy_instr, "legacy-instructions", "user"))
    model_instr = resolve_instructions_file(user_config, home, codex_home)
    if model_instr is not None and "error" not in model_instr:
        user_instructions.append(model_instr)

    skills_config = extract_skills_config(user_config)
    user_skills = list_skills(codex_home / "skills", "user")
    apply_skill_config(user_skills, skills_config)
    shared_agents_skills = list_skills(home / ".agents" / "skills", "shared-agents")

    user_hooks_path = codex_home / "hooks.json"
    user_hooks = read_hooks_file(user_hooks_path)

    fallback_names = (user_config or {}).get("project_doc_fallback_filenames")
    fallback_names = [str(x) for x in fallback_names] if isinstance(fallback_names, list) else []

    plugin_states = extract_plugin_states(user_config)

    legacy_cfg_path = codex_home / LEGACY_USER_CONFIG
    legacy_cfg = legacy_config_summary(legacy_cfg_path) if legacy_cfg_path.is_file() else None

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

        fallback_docs = find_fallback_project_docs(proj, fallback_names)

        proj_hooks_path = proj_codex / "hooks.json"
        proj_hooks = read_hooks_file(proj_hooks_path)

        proj_skills = list_skills(proj_codex / "skills", "project")
        apply_skill_config(proj_skills, skills_config + extract_skills_config(proj_cfg))

        proj_mj = read_marketplace_json(proj / ".agents" / "plugins" / "marketplace.json")

        project_records.append({
            "root": str(proj),
            "name": proj.name,
            "native_agents_md": native_agents_md,
            "nested_agents_md": find_nested_agents_md(proj),
            "fallback_project_docs": fallback_docs,
            "native_agents_skills": list_skills(proj / ".agents" / "skills", "project-native"),
            "skills": proj_skills,
            "prompts": list_md(proj_codex / "prompts", "prompt", "project"),
            "agents": list_agents(proj_codex / "agents", "project"),
            "mcp_servers": mcp_servers_from_toml(proj_cfg, str(proj_cfg_path)),
            "settings": extract_settings_summary(proj_cfg),
            "hooks": {"path": str(proj_hooks_path), "events": sorted(proj_hooks.keys())} if proj_hooks else None,
            "rules": list_rule_files(proj_codex / "rules", "project"),
            "marketplace_json": proj_mj,
        })

    return {
        "schema_version": 3,
        "home": str(home),
        "codex_home": str(codex_home),
        "codex_dir_exists": codex_home.is_dir(),
        "codex": {**version_info, "config_era": config_era},
        "user": {
            "instructions": user_instructions,
            "skills": user_skills,
            "skills_config": skills_config,
            "shared_agents_skills_count": len(shared_agents_skills),
            "prompts": list_md(codex_home / "prompts", "prompt", "user"),
            "agents": list_agents(codex_home / "agents", "user"),
            "mcp_servers": mcp_servers_from_toml(user_config, str(user_config_path)),
            "settings": user_settings,
            "notify": extract_notify(user_config),
            "project_doc_fallback_filenames": fallback_names,
            "trusted_projects": extract_trusted_projects(user_config),
            "plugin_states": plugin_states,
            "hooks": {"path": str(user_hooks_path), "events": sorted(user_hooks.keys())} if user_hooks else None,
            "rules": list_rule_files(codex_home / "rules", "user"),
            "legacy_config_json": legacy_cfg,
            "model_instructions_missing": model_instr if (model_instr and "error" in model_instr) else None,
        },
        "plugins": discover_plugins(codex_home, plugin_states),
        "marketplaces": discover_marketplaces(home, codex_home),
        "projects": project_records,
        "managed_config_present": detect_managed_config(home, codex_home),
        "stays_behind": {
            "credentials_present": detect_credential_files(codex_home),
            "session_files_count": count_sessions(codex_home),
            "memory_files_count": count_memories(codex_home),
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
    parser.add_argument(
        "--no-exec",
        action="store_true",
        help="Do not run `codex --version`; detect version from paths/files only",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    args = parser.parse_args(argv)

    home = args.home.expanduser().resolve()
    if args.codex_home is not None:
        codex_home = args.codex_home.expanduser().resolve()
    elif os.environ.get("CODEX_HOME"):
        codex_home = Path(os.environ["CODEX_HOME"]).expanduser().resolve()
    else:
        codex_home = home / ".codex"

    inv = discover(home, codex_home, args.project, allow_exec=not args.no_exec)
    json.dump(inv, sys.stdout, indent=2 if args.pretty else None)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
