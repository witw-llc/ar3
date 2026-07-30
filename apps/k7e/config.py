"""k7e configuration — LLM commands, embedding backend, status reporting.

Config file: $K7E_HOME/config.json (created by `k7e init` or `k7e config`)
Falls back to env vars, then sensible defaults.

LLM integration is explicit and CLI-based. Each command is a shell string
invoked with the prompt on stdin; the model response is read from stdout.
No auto-detection. Embeddings use ollama's HTTP API separately.

Config format:
{
  "llm_command": "ollama run qwen3",    # fallback for all LLM purposes
  "summarize_command": null,            # recall synthesis
  "decompose_command": null,            # long-text query extraction
  "distill_command": null,              # knowledge extraction
  "compile_command": null,              # tag synthesis
  "rerank_command": null,               # search/recall reranking
  "embeddings": "ollama",
  "embed_model": "nomic-embed-text",
  "ollama_url": "http://localhost:11434",
  "rerank": false,
  "decay_offset_days": 30,
  "decay_scale_days": 365,
  "use_count_weight": 0.2
}
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

LLM_FALLBACK_KEY = "llm_command"
LLM_FALLBACK_ENV = "K7E_LLM_COMMAND"

LLM_PURPOSES = {
    "summarize": ("summarize_command", "K7E_SUMMARIZE_COMMAND"),
    "decompose": ("decompose_command", "K7E_DECOMPOSE_COMMAND"),
    "distill": ("distill_command", "K7E_DISTILL_COMMAND"),
    "compile": ("compile_command", "K7E_COMPILE_COMMAND"),
    "rerank": ("rerank_command", "K7E_RERANK_COMMAND"),
}


def _k7e_home():
    override = os.environ.get("K7E_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "k7e"


def config_path():
    return _k7e_home() / "config.json"


def load_config():
    path = config_path()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config(cfg):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def _get_raw(key, env_key=None):
    if env_key and os.environ.get(env_key):
        return os.environ[env_key]
    return load_config().get(key)


def get(key, default=None):
    """Read config value with env var override."""
    env_map = {
        LLM_FALLBACK_KEY: LLM_FALLBACK_ENV,
        "summarize_command": "K7E_SUMMARIZE_COMMAND",
        "decompose_command": "K7E_DECOMPOSE_COMMAND",
        "distill_command": "K7E_DISTILL_COMMAND",
        "compile_command": "K7E_COMPILE_COMMAND",
        "rerank_command": "K7E_RERANK_COMMAND",
        "embeddings": "K7E_EMBEDDINGS",
        "embed_model": "EMBED_MODEL",
        "ollama_url": "OLLAMA_URL",
        "decay_offset_days": "K7E_DECAY_OFFSET",
        "decay_scale_days": "K7E_DECAY_SCALE",
        "use_count_weight": "K7E_USE_WEIGHT",
        "rerank": "K7E_RERANK",
    }
    env_key = env_map.get(key)
    val = _get_raw(key, env_key)
    return val if val is not None else default


def resolve_command(purpose):
    """Return the shell command string for an LLM purpose, or None."""
    cfg_key, env_key = LLM_PURPOSES[purpose]
    cmd = _get_raw(cfg_key, env_key)
    if cmd and str(cmd).strip():
        return str(cmd).strip()
    fallback = _get_raw(LLM_FALLBACK_KEY, LLM_FALLBACK_ENV)
    return str(fallback).strip() if fallback else None


def command_source(purpose):
    """Return (command, source_label) for status/config display."""
    cfg_key, env_key = LLM_PURPOSES[purpose]
    cmd = _get_raw(cfg_key, env_key)
    if cmd and str(cmd).strip():
        return str(cmd).strip(), cfg_key
    fallback = _get_raw(LLM_FALLBACK_KEY, LLM_FALLBACK_ENV)
    if fallback and str(fallback).strip():
        return str(fallback).strip(), LLM_FALLBACK_KEY
    return None, "not configured"


def llm_configured(purpose):
    """True when a command is configured for the given LLM purpose."""
    return resolve_command(purpose) is not None


# Back-compat alias used by CLI fail-fast guards.
llm_available = llm_configured


# --- Provider detection (embeddings only) ---

def detect_providers():
    """Check what's available on this system."""
    results = {}

    ollama_url = get("ollama_url", "http://localhost:11434")
    try:
        req = urllib.request.Request(f"{ollama_url}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            models = [m["name"] for m in data.get("models", [])]
            embed_model = get("embed_model", "nomic-embed-text")
            has_embed = any(embed_model in m for m in models)
            results["embeddings:ollama"] = {
                "available": True,
                "models": models,
                "has_embed_model": has_embed,
                "embed_model": embed_model,
            }
    except (urllib.error.URLError, OSError):
        results["embeddings:ollama"] = {"available": False}

    results["search:fts5"] = {"available": True}
    return results


def status():
    """Human-readable status report."""
    providers = detect_providers()
    home = _k7e_home()
    lines = ["k7e status:", ""]

    lines.append(f"  Home: {home}")
    if not home.exists():
        lines.append("    → Not initialized. Run any k7e command to create.")
    lines.append("")

    fallback = _get_raw(LLM_FALLBACK_KEY, LLM_FALLBACK_ENV)
    if fallback and str(fallback).strip():
        lines.append(f"  LLM fallback: {fallback.strip()} ✓")
    else:
        lines.append("  LLM fallback: not configured")
        lines.append("    → Set: k7e config llm_command 'your-stdin-stdout-cli'")

    for purpose, (cfg_key, _) in LLM_PURPOSES.items():
        cmd, source = command_source(purpose)
        if source == cfg_key:
            lines.append(f"  LLM {purpose}: {cmd} ({source})")
        elif cmd:
            lines.append(f"  LLM {purpose}: {cmd} (via llm_command)")
        else:
            lines.append(f"  LLM {purpose}: unavailable")

    embed_status = providers.get("embeddings:ollama", {})
    if embed_status.get("available"):
        if embed_status.get("has_embed_model"):
            lines.append(f"  Embeddings: ollama ({embed_status['embed_model']}) ✓")
        else:
            model = embed_status.get("embed_model", "nomic-embed-text")
            lines.append(f"  Embeddings: ollama running but model '{model}' not found")
            lines.append(f"    → Install: ollama pull {model}")
    else:
        lines.append("  Embeddings: UNAVAILABLE (ollama not running)")
        lines.append("    → Install: curl -fsSL https://ollama.com/install.sh | sh")
        lines.append(f"    → Then: ollama pull {get('embed_model', 'nomic-embed-text')}")

    lines.append("  Search: FTS5 (keyword) ✓")
    if embed_status.get("available") and embed_status.get("has_embed_model"):
        lines.append("  Search: Semantic (embeddings) ✓")
    else:
        lines.append("  Search: Semantic (embeddings) ✗ — FTS5-only mode")

    lines.append("")
    missing = []
    if not (fallback and str(fallback).strip()):
        missing.append("Set llm_command (stdin→stdout CLI) for distill/recall/compile")
    if not embed_status.get("has_embed_model"):
        missing.append(f"Run `ollama pull {get('embed_model', 'nomic-embed-text')}` for semantic search")
    if missing:
        lines.append("  Recommendations:")
        for m in missing:
            lines.append(f"    • {m}")
    else:
        lines.append("  All capabilities active.")

    return "\n".join(lines)
