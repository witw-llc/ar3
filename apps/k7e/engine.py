"""k7e engine — store, search, append, reindex, assets.

Flat markdown files are source of truth. SQLite FTS5 + optional embeddings
are derived indexes, rebuildable from files via reindex().

Binary assets stored content-addressed (SHA256 hash + extension).
Same content = same hash = one file.

Zero non-stdlib dependencies. Embeddings use ollama HTTP API (urllib).
Configurable root via K7E_HOME env var (defaults to ~/.config/k7e, honoring
XDG_CONFIG_HOME).
"""

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import struct
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _k7e_home():
    override = os.environ.get("K7E_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "k7e"


NODES_DIR = None
MOCS_DIR = None
ASSETS_DIR = None
INDEX_DB = None

def _ollama_url():
    return os.environ.get("OLLAMA_URL") or _load_config_val("ollama_url", "http://localhost:11434")

def _embed_model():
    return os.environ.get("EMBED_MODEL") or _load_config_val("embed_model", "nomic-embed-text")

def _load_config_val(key, default):
    try:
        import config
        return config.get(key, default)
    except ImportError:
        return default

RRF_K = 60

EMBEDDINGS_OFF = {"off", "none", "false", "0", "no"}
EMBED_TIMEOUT = 10.0        # backlog embedding, driven from idle passes
QUERY_EMBED_TIMEOUT = 2.0   # read path: a wake must not wait on a sick ollama

# Wall time of the last query embedding, for callers that price a retrieval.
LAST_QUERY_EMBED_MS = None
LAST_QUERY_EMBED_OK = False

# Recency decay + use-count ranking.
# Defaults tuned for dev-knowledge churn (tighter than the article's 5yr).
DECAY_OFFSET_DAYS = 30.0   # flat zone: facts younger than this don't decay
DECAY_SCALE_DAYS = 365.0   # days past the flat zone at which the multiplier hits 0.5
USE_COUNT_WEIGHT = 0.2     # log10 use-count boost weight


def _num_config(key, default):
    val = _load_config_val(key, default)
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _decay_config():
    return (
        _num_config("decay_offset_days", DECAY_OFFSET_DAYS),
        _num_config("decay_scale_days", DECAY_SCALE_DAYS),
        _num_config("use_count_weight", USE_COUNT_WEIGHT),
    )


def _embeddings_enabled():
    """The semantic track rides ollama when it answers. Setting `embeddings`
    to an off value turns the track off outright — no queueing, no query
    embedding, FTS5 alone."""
    return str(_load_config_val("embeddings", "ollama")).strip().lower() not in EMBEDDINGS_OFF


def _rerank_enabled():
    val = _load_config_val("rerank", None)
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _days_since(date_str):
    if not date_str:
        return None
    try:
        t = time.strptime(str(date_str)[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    return max(0.0, (time.time() - time.mktime(t)) / 86400.0)


def _recency_factor(last_used_at, last_updated, offset, scale):
    """Gauss-shaped relevance decay. 1.0 inside the flat zone, 0.5 at `scale`
    days past it. Basis date is last_used_at (recall-time freshness) if present,
    else the persisted last_updated."""
    if scale <= 0:
        return 1.0
    age = _days_since(last_used_at) if last_used_at else None
    if age is None:
        age = _days_since(last_updated)
    if age is None:
        return 1.0
    effective = age - offset
    if effective <= 0:
        return 1.0
    s = scale / math.sqrt(2 * math.log(2))
    return math.exp(-(effective * effective) / (2 * s * s))


def _use_boost(use_count, weight):
    if not use_count or weight <= 0:
        return 1.0
    return 1.0 + math.log10(1 + use_count) * weight


def _bump_usage(node_ids):
    """Increment use_count and refresh last_used_at for the given nodes.
    Index-only signal; reset on reindex (re-earns ranking from usage)."""
    if not node_ids:
        return
    now = time.strftime("%Y-%m-%d")
    conn = _connect()
    for nid in node_ids:
        conn.execute(
            "UPDATE nodes SET use_count = COALESCE(use_count, 0) + 1, last_used_at = ? WHERE id = ?",
            (now, nid),
        )
    conn.commit()
    conn.close()


def reset(home=None):
    """Reset store paths. For testing or multi-store usage."""
    global NODES_DIR, MOCS_DIR, ASSETS_DIR, INDEX_DB
    h = Path(home) if home else _k7e_home()
    NODES_DIR = h / "nodes"
    MOCS_DIR = h / "mocs"
    ASSETS_DIR = h / "assets"
    INDEX_DB = h / ".index.db"


def init():
    global NODES_DIR, MOCS_DIR, ASSETS_DIR, INDEX_DB
    if NODES_DIR is None:
        home = _k7e_home()
        NODES_DIR = home / "nodes"
        MOCS_DIR = home / "mocs"
        ASSETS_DIR = home / "assets"
        INDEX_DB = home / ".index.db"
    NODES_DIR.mkdir(parents=True, exist_ok=True)
    MOCS_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    conn = _connect()
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.close()


def next_id():
    """Generate next K7E-BBB-NNNNN ID. Sequential across all buckets.
    Uses a counter in the sqlite meta table for O(1) performance.
    Falls back to filesystem scan once to initialize if counter is missing."""
    conn = _connect()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
    )
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'next_id_counter'"
    ).fetchone()

    if row is not None:
        total = int(row[0]) + 1
    else:
        # Initialize from filesystem scan (one-time fallback)
        highest = 0
        for bucket_dir in sorted(NODES_DIR.iterdir()):
            if not bucket_dir.is_dir():
                continue
            for f in bucket_dir.glob("K7E-*.md"):
                parts = f.stem.split("-")
                if len(parts) == 3:
                    try:
                        num = int(parts[1]) * 100000 + int(parts[2])
                        highest = max(highest, num)
                    except ValueError:
                        pass
        total = highest + 1

    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('next_id_counter', ?)",
        (str(total),)
    )
    conn.commit()
    conn.close()

    bucket = total // 100000
    seq = total % 100000
    return f"K7E-{bucket:03d}-{seq:05d}"


def _node_path(node_id):
    """Resolve node ID to file path: nodes/BBB/K7E-BBB-NNNNN.md"""
    parts = node_id.split("-")
    if len(parts) == 3:
        bucket = parts[1]
    else:
        bucket = "000"
    return NODES_DIR / bucket / f"{node_id}.md"


def _all_node_files():
    """Iterate all node files across all buckets."""
    for bucket_dir in sorted(NODES_DIR.iterdir()):
        if not bucket_dir.is_dir():
            continue
        for f in sorted(bucket_dir.glob("K7E-*.md")):
            yield f


def store_entry(title, content, tags=None, aliases=None, importance=5):
    """Store a new knowledge entry. Deduplicates by content hash at storage layer.
    For semantic dedup-aware ingestion, use distill."""
    tags = tags or []
    aliases = aliases or []
    init()

    # Content-hash dedup: check for exact duplicate before writing
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
    conn = _connect()
    existing = conn.execute(
        "SELECT id FROM nodes WHERE content_hash = ?", (content_hash,)
    ).fetchone()
    conn.close()
    if existing:
        return existing[0]

    node_id = next_id()
    now = time.strftime("%Y-%m-%d")
    confidence = round(importance / 10, 1)

    body = f"""---
id: {node_id}
title: {title}
aliases: [{', '.join(aliases)}]
status: active
confidence: {confidence}
verification_count: 0
last_updated: {now}
tags: [{', '.join(tags)}]
---

## Verified Protocol

{content.strip()}

## Edge Cases

## False Paths

## History
* {now}: Initial entry.
"""

    node_path = _node_path(node_id)
    node_path.parent.mkdir(parents=True, exist_ok=True)
    node_path.write_text(body, encoding="utf-8")

    _index_node(node_id, title, aliases, tags, content, now, content_hash=content_hash, confidence=confidence)
    _update_mocs(node_id, title, tags)

    return node_id


def append_entry(node_id, section, content):
    node_path = _node_path(node_id)
    if not node_path.exists():
        raise FileNotFoundError(f"Node {node_id} not found")

    text = node_path.read_text(encoding="utf-8")
    now = time.strftime("%Y-%m-%d")

    section_header = f"## {section}"
    if section_header in text:
        parts = text.split(section_header)
        before = parts[0]
        after = parts[1]
        next_section = re.search(r"\n## ", after)
        if next_section:
            section_body = after[:next_section.start()]
            remainder = after[next_section.start():]
        else:
            section_body = after
            remainder = ""
        section_body = section_body.rstrip() + f"\n* {content.strip()}\n"
        text = before + section_header + section_body + remainder
    else:
        text = text.rstrip() + f"\n\n{section_header}\n* {content.strip()}\n"

    # Update last_updated in frontmatter
    text = re.sub(r"last_updated: .+", f"last_updated: {now}", text)

    # Bump verification_count
    match = re.search(r"verification_count: (\d+)", text)
    if match:
        count = int(match.group(1)) + 1
        text = re.sub(r"verification_count: \d+", f"verification_count: {count}", text)

    node_path.write_text(text, encoding="utf-8")

    meta = _parse_frontmatter(text)
    full_content = _extract_body(text)
    _index_node(
        node_id, meta.get("title", ""),
        meta.get("aliases", []), meta.get("tags", []),
        full_content, now
    )

    return node_id


def supersede(old_id, new_id):
    """Mark old_id as superseded by new_id. Returns True if old_id existed."""
    node_path = _node_path(old_id)
    if not node_path.exists():
        return False
    text = node_path.read_text(encoding="utf-8")
    text = re.sub(r"status: active", "status: superseded", text)
    text = re.sub(r"(tags: \[.*?\])", r"\1\nsuperseded_by: " + new_id, text)
    node_path.write_text(text, encoding="utf-8")
    # Update index
    conn = _connect()
    conn.execute("UPDATE nodes SET status = 'superseded', superseded_by = ? WHERE id = ?", (new_id, old_id))
    conn.commit()
    conn.close()
    return True


def search(query, limit=5, json_output=False, include_superseded=False, rerank=None):
    init()
    if rerank is None:
        rerank = _rerank_enabled()

    # Over-fetch a wider candidate pool when reranking so the reranker has
    # something to reorder; otherwise keep the historical limit-sized pool.
    pool = max(limit, 15) if rerank else limit

    conn = _connect()
    bm25_results = _search_bm25(conn, query, pool * 3, include_superseded)
    meta_results = _search_metadata(conn, query, pool * 3, include_superseded)
    embed_results = _search_embeddings(conn, query, pool * 3, include_superseded)

    tracks = [bm25_results, meta_results, embed_results]
    fused = _rrf_fuse(tracks, pool)
    conn.close()

    # Filter out noise: require minimum RRF score.
    # rank-0 in one track = 1/(60+1) ≈ 0.0164
    # rank-0 in two tracks = 2/(60+1) ≈ 0.0328
    # We accept rank-0 single-track hits (0.0164) but reject lower.
    # With only one track live the fused score is a pure rank ladder, so the
    # floor would truncate at rank 5 instead of separating signal from noise.
    if sum(1 for t in tracks if t) > 1:
        min_score = 1.0 / (RRF_K + 1) - 0.001  # ~0.0154
        fused = [r for r in fused if r["score"] >= min_score]

    # Apply confidence, recency decay, and use-count boost as score multipliers.
    if fused:
        offset, scale, weight = _decay_config()
        conn2 = _connect()
        for r in fused:
            row = conn2.execute(
                "SELECT confidence, last_used_at, use_count, last_updated FROM nodes WHERE id = ?",
                (r["id"],),
            ).fetchone()
            if row:
                conf_factor = 0.7 + 0.3 * (row[0] if row[0] else 0.5)
                recency = _recency_factor(row[1], row[3], offset, scale)
                use_boost = _use_boost(row[2], weight)
                r["score"] = round(r["score"] * conf_factor * recency * use_boost, 4)
        conn2.close()
        fused.sort(key=lambda x: -x["score"])

    if rerank and fused:
        fused = _rerank(query, fused, limit)
    else:
        fused = fused[:limit]

    return fused


def get(node_id, track_usage=False):
    node_path = _node_path(node_id)
    if not node_path.exists():
        raise FileNotFoundError(f"Node {node_id} not found")
    text = node_path.read_text(encoding="utf-8")
    if track_usage:
        _bump_usage([node_id])
    return text


def reindex(embeddings=False):
    init()
    conn = _connect()
    conn.execute("DELETE FROM nodes")
    conn.execute("DELETE FROM nodes_fts")
    conn.execute("DELETE FROM pending_embeddings")
    if embeddings:
        conn.execute("DELETE FROM embeddings")
    conn.commit()

    for path in _all_node_files():
        text = path.read_text(encoding="utf-8")
        meta = _parse_frontmatter(text)
        body = _extract_body(text)
        node_id = meta.get("id", path.stem)
        title = meta.get("title", "")
        aliases = meta.get("aliases", [])
        tags = meta.get("tags", [])
        now = meta.get("last_updated", time.strftime("%Y-%m-%d"))
        content_hash = hashlib.sha256(body.encode()).hexdigest()[:16]

        conn.execute(
            "INSERT OR REPLACE INTO nodes (id, title, aliases, status, confidence, "
            "verification_count, last_updated, tags, created_at, updated_at, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (node_id, title, ", ".join(aliases), meta.get("status", "active"),
             meta.get("confidence", 0.5), meta.get("verification_count", 0),
             now, ", ".join(tags), now, now, content_hash)
        )
        conn.execute(
            "INSERT INTO nodes_fts (rowid, title, aliases, tags, content) "
            "VALUES ((SELECT rowid FROM nodes WHERE id = ?), ?, ?, ?, ?)",
            (node_id, title, " ".join(aliases), " ".join(tags), body)
        )

        if embeddings:
            vec = embed_text(f"{title} {body[:500]}")
            if vec:
                conn.execute(
                    "INSERT OR REPLACE INTO embeddings (node_id, vector, model, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (node_id, _pack_vector(vec), _embed_model(), now)
                )
            else:
                # Queue for later if embedding service unavailable
                conn.execute(
                    "INSERT OR REPLACE INTO pending_embeddings (node_id, queued_at) VALUES (?, ?)",
                    (node_id, now)
                )

    conn.commit()
    conn.close()

    # Process any pending embeddings queued during reindex
    if embeddings:
        process_pending_embeddings()


def list_nodes(status=None, tag=None):
    init()
    conn = _connect()
    query = "SELECT id, title, status, confidence, tags FROM nodes"
    conditions = []
    params = []
    if status:
        conditions.append("status = ?")
        params.append(status)
    if tag:
        conditions.append("tags LIKE ?")
        params.append(f"%{tag}%")
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY last_updated DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [{"id": r[0], "title": r[1], "status": r[2], "confidence": r[3], "tags": r[4]} for r in rows]


def rebuild_mocs():
    """Rebuild all MOC files from node tags. Destructive — replaces existing MOCs."""
    init()
    mocs = {}

    for path in _all_node_files():
        text = path.read_text(encoding="utf-8")
        meta = _parse_frontmatter(text)
        node_id = meta.get("id", path.stem)
        title = meta.get("title", "Unknown")
        status = meta.get("status", "active")
        tags = meta.get("tags", [])
        for tag in tags:
            mocs.setdefault(tag, []).append((node_id, title, status))

    for path in MOCS_DIR.glob("*.md"):
        path.unlink()

    for tag, nodes in sorted(mocs.items()):
        active = [(nid, t) for nid, t, s in nodes if s == "active"]
        other = [(nid, t, s) for nid, t, s in nodes if s != "active"]
        content = f"# {tag}\n\n"
        if active:
            content += "## Active\n"
            for nid, title in active:
                content += f"* [[{nid}]] — {title}\n"
            content += "\n"
        if other:
            content += "## Archived\n"
            for nid, title, status in other:
                content += f"* [[{nid}]] — {title} ({status})\n"
            content += "\n"
        (MOCS_DIR / _moc_filename(tag)).write_text(content, encoding="utf-8")


def stats():
    """Return store statistics."""
    init()
    conn = _connect()
    total_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    avg_conf = conn.execute("SELECT AVG(confidence) FROM nodes").fetchone()[0] or 0.0
    all_tags = conn.execute("SELECT tags FROM nodes").fetchall()
    conn.close()

    tag_freq = {}
    for row in all_tags:
        if row[0]:
            for t in (t.strip() for t in row[0].split(",") if t.strip()):
                tag_freq[t] = tag_freq.get(t, 0) + 1

    return {
        "total_nodes": total_nodes,
        "total_mocs": len(list(MOCS_DIR.glob("*.md"))),
        "total_assets": len([f for f in ASSETS_DIR.rglob("*.*") if f.name != ".gitkeep"]),
        "avg_confidence": round(avg_conf, 2),
        "top_tags": sorted(tag_freq.items(), key=lambda x: -x[1])[:10],
    }


# --- LLM ---

_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:"
    r"\[[0-9;?]*[ -/]*[@-~]"          # CSI: ESC[ params intermediates final
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC: ESC] ... BEL or ST (ESC \)
    r"|[P_^][^\x1b]*\x1b\\"            # DCS/APC/PM: ESC P/_/^ ... ST (ESC \)
    r"|[@-Z\\-_]"                      # remaining single-char C1-style escapes
    r")"
)


def _strip_ansi(text):
    """Strip ANSI/CSI escape sequences (e.g. terminal-wrapping CLIs like
    `ollama run` splice cursor-control codes into piped stdout)."""
    return _ANSI_ESCAPE_RE.sub("", text)


def _call_llm(prompt, purpose="summarize", timeout=120):
    """Invoke the configured stdin→stdout CLI for an LLM purpose."""
    import config
    import shlex
    import subprocess

    cmd_str = config.resolve_command(purpose)
    if not cmd_str:
        return None

    try:
        cmd = shlex.split(cmd_str)
        result = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
            cwd=str(config._k7e_home()),
        )
        if result.returncode == 0 and result.stdout.strip():
            return _strip_ansi(result.stdout).strip()
        if result.returncode != 0:
            print(f"  [llm:{purpose}] exit {result.returncode}", file=sys.stderr)
            if result.stderr.strip():
                print(f"  [llm:{purpose}] {result.stderr.strip()}", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print(f"  [llm:{purpose}] timed out ({timeout}s)", file=sys.stderr)
    except OSError as e:
        print(f"  [llm:{purpose}] launch failed: {e}", file=sys.stderr)
    return None


# --- Reranking ---

_ID_RE = re.compile(r"K7E-\d{3}-\d{5}")


def _snippet(node_id, length=200):
    try:
        body = _extract_body(get(node_id))
    except FileNotFoundError:
        return ""
    return " ".join(body.split())[:length]


def _rerank(query, results, limit):
    """Reorder candidates using the LLM as a cross-encoder-style relevance
    scorer. Over-fetch wide, rerank a small pool.
    Degrades gracefully to the input order when no LLM is available or the
    response can't be parsed, so callers can always rely on it."""
    if len(results) <= 1:
        return results[:limit]

    candidates = results[:15]
    by_id = {r["id"]: r for r in candidates}
    listing = "\n".join(
        f"[{r['id']}] {r['title']}: {_snippet(r['id'])}" for r in candidates
    )
    prompt = (
        "Rank these knowledge entries by how well they answer the query. "
        "Return ONLY the entry IDs in descending relevance order, one per line, "
        "with no other text.\n\n"
        f"Query: {query[:500]}\n\nEntries:\n{listing}"
    )

    response = _call_llm(prompt, purpose="rerank", timeout=30)
    if not response:
        return results[:limit]

    ordered = []
    seen = set()
    for line in response.splitlines():
        m = _ID_RE.search(line)
        if m and m.group(0) in by_id and m.group(0) not in seen:
            ordered.append(by_id[m.group(0)])
            seen.add(m.group(0))

    if not ordered:
        return results[:limit]

    # Append candidates the LLM dropped (preserve original order), then any
    # results beyond the rerank window.
    for r in candidates:
        if r["id"] not in seen:
            ordered.append(r)
            seen.add(r["id"])
    for r in results[15:]:
        if r["id"] not in seen:
            ordered.append(r)
            seen.add(r["id"])

    return ordered[:limit]


# --- Recall (RAG) ---

def recall(text, limit=8, include_superseded=False):
    """RAG recall: given arbitrary text, find relevant knowledge and synthesize.

    Returns (answer_text, source_entries). The CLI gates this behind an LLM
    availability check (fail fast); answer_text is None only when nothing was
    found or the LLM call failed at runtime.
    """
    init()
    text = text.strip()
    if not text:
        return None, []

    # Decompose into search queries
    if len(text) <= 100:
        queries = [text]
    else:
        queries = _decompose_queries(text)
        if not queries:
            queries = [text[:100]]

    # Search across all queries, merge by node ID (keep max score). Subquery
    # searches don't rerank (one LLM rerank over the merged pool below is enough).
    hit_counts = {}
    hit_info = {}
    for q in queries:
        results = search(q, limit=limit, include_superseded=include_superseded, rerank=False)
        for r in results:
            hit_counts[r["id"]] = hit_counts.get(r["id"], 0) + 1
            if r["id"] not in hit_info or r.get("score", 0) > hit_info[r["id"]].get("score", 0):
                hit_info[r["id"]] = r

    if not hit_counts:
        return None, []

    # Rank by frequency across queries, then by score, into a wide pool
    pool_size = max(limit, 15)
    ranked_ids = sorted(
        hit_counts.keys(),
        key=lambda nid: (hit_counts[nid], hit_info[nid].get("score", 0)),
        reverse=True,
    )[:pool_size]

    # Rerank the merged pool with the LLM (no-op/fallback when no LLM), then trim.
    candidates = [{"id": nid, "title": hit_info[nid]["title"]} for nid in ranked_ids]
    ranked_ids = [r["id"] for r in _rerank(text, candidates, limit)]

    # Retrieve full content
    entries = []
    for nid in ranked_ids:
        try:
            node_text = get(nid)
            body = _extract_body(node_text)
            entries.append({"id": nid, "title": hit_info[nid]["title"], "content": body.strip()})
        except FileNotFoundError:
            continue

    if not entries:
        return None, []

    _bump_usage([e["id"] for e in entries])

    # Synthesize
    context_text = "\n\n---\n\n".join(
        f"[{e['id']}] {e['title']}\n{e['content']}" for e in entries
    )
    prompt = (
        "Summarize everything relevant from the knowledge entries below "
        "about the given context. Be concise and factual. Cite entry IDs "
        "in brackets when referencing specific facts. If the entries contain "
        "nothing relevant, say so briefly.\n\n"
        f"Context: {text[:2000]}\n\n"
        f"Knowledge entries:\n{context_text}"
    )

    answer = _call_llm(prompt, purpose="summarize")
    return answer, entries


def _decompose_queries(text):
    """Use the LLM to extract search queries from long text. Returns [] if the
    LLM is unavailable or returns nothing; recall() then searches the raw text."""
    prompt = (
        "Extract 3-5 short search queries (2-4 words each) that capture the "
        "key topics in this text. Return one query per line, nothing else.\n\n"
        f"Text: {text[:2000]}"
    )
    response = _call_llm(prompt, purpose="decompose", timeout=30)
    if not response:
        return []
    lines = [l.strip().strip("-•*").strip() for l in response.splitlines() if l.strip()]
    queries = [l for l in lines if 2 <= len(l.split()) <= 8 and len(l) < 80]
    return queries[:5]


# --- Compile (knowledge compounding) ---

def compile_tag(tag, dry_run=False):
    """Synthesize all active entries for a tag into a single reference page.

    Requires 3+ active nodes with the given tag. Uses configured LLM to
    produce a compiled overview. Source nodes are NOT modified or deleted.
    Returns the new compiled node ID, or None if dry_run.
    """
    init()
    nodes = list_nodes(tag=tag, status="active")
    if len(nodes) < 3:
        print(f"Need 3+ active nodes with tag '{tag}', found {len(nodes)}.", file=sys.stderr)
        return None

    # Gather content from source nodes
    entries = []
    for n in nodes:
        try:
            text = get(n["id"])
            body = _extract_body(text)
            entries.append({"id": n["id"], "title": n["title"], "content": body.strip()})
        except FileNotFoundError:
            continue

    if len(entries) < 3:
        print(f"Need 3+ readable nodes with tag '{tag}', found {len(entries)}.", file=sys.stderr)
        return None

    # Build prompt
    entry_texts = "\n\n---\n\n".join(
        f"[Entry {e['id']}] {e['title']}\n{e['content']}" for e in entries
    )
    prompt = (
        f"Synthesize these {len(entries)} knowledge entries about '{tag}' into a single "
        f"authoritative reference. Include sections: Overview, Procedures, Gotchas, "
        f"Open Questions. Preserve specific technical details (commands, flags, ports). "
        f"Cite source entry IDs.\n\n{entry_texts}"
    )

    if dry_run:
        print(f"Would compile {len(entries)} entries for tag '{tag}':")
        for e in entries:
            print(f"  {e['id']}  {e['title']}")
        return None

    compiled_content = _call_llm(prompt, purpose="compile")
    if not compiled_content:
        print("Error: compile_command (or llm_command) not configured or call failed.", file=sys.stderr)
        return None

    # Store as a new compiled node
    source_ids = [e["id"] for e in entries]
    content_with_sources = (
        f"{compiled_content}\n\n"
        f"## Sources\n"
        + "\n".join(f"* [[{sid}]]" for sid in source_ids)
    )

    tags_list = [tag, "compiled"]
    node_id = next_id()
    now = time.strftime("%Y-%m-%d")

    body = f"""---
id: {node_id}
title: {tag} — Compiled Reference
aliases: []
status: compiled
confidence: 0.8
verification_count: 0
last_updated: {now}
tags: [{', '.join(tags_list)}]
---

{content_with_sources}

## History
* {now}: Compiled from {len(entries)} entries.
"""

    node_path = _node_path(node_id)
    node_path.parent.mkdir(parents=True, exist_ok=True)
    node_path.write_text(body, encoding="utf-8")

    _index_node(node_id, f"{tag} — Compiled Reference", [], tags_list, content_with_sources, now)
    _update_mocs(node_id, f"{tag} — Compiled Reference", tags_list)

    return node_id


# --- Assets ---

def store_asset(source_path):
    """Content-addressed asset storage. Returns relative path for markdown embedding.
    Same file content = same hash = one copy. Safe to call multiple times."""
    source = Path(source_path)
    if not source.exists():
        raise FileNotFoundError(f"Asset source not found: {source_path}")

    # Hash the file content
    h = hashlib.sha256()
    with open(source, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    content_hash = h.hexdigest()[:12]
    ext = source.suffix.lower()
    asset_name = f"{content_hash}{ext}"
    # Bucket by first 2 chars of hash (256 buckets, same as git objects)
    bucket = content_hash[:2]
    bucket_dir = ASSETS_DIR / bucket
    bucket_dir.mkdir(parents=True, exist_ok=True)
    dest = bucket_dir / asset_name

    if not dest.exists():
        shutil.copy2(source, dest)

    return f"assets/{bucket}/{asset_name}"


# --- Embedding ---

def embed_text(text, timeout=EMBED_TIMEOUT):
    try:
        data = json.dumps({"model": _embed_model(), "input": text}).encode()
        req = urllib.request.Request(
            f"{_ollama_url()}/api/embed",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
            embeddings = result.get("embeddings", [])
            if embeddings:
                return embeddings[0]
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError):
        pass
    return None


def cosine_similarity(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# --- Internal ---

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    aliases TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    confidence REAL DEFAULT 0.5,
    verification_count INTEGER DEFAULT 0,
    last_updated TEXT,
    tags TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT,
    content_hash TEXT DEFAULT '',
    superseded_by TEXT DEFAULT '',
    last_used_at TEXT,
    use_count INTEGER DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    title, aliases, tags, content,
    tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS embeddings (
    node_id TEXT PRIMARY KEY,
    vector BLOB,
    model TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS pending_embeddings (
    node_id TEXT PRIMARY KEY,
    queued_at TEXT
);

INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '1');
"""

_EMBED_SCAN_LIMIT = 10000


def _migrate(conn):
    """Add columns that may be missing from older databases."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(nodes)").fetchall()}
    if "content_hash" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN content_hash TEXT DEFAULT ''")
    if "superseded_by" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN superseded_by TEXT DEFAULT ''")
    if "last_used_at" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN last_used_at TEXT")
    if "use_count" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN use_count INTEGER DEFAULT 0")
    conn.commit()


def _connect():
    conn = sqlite3.connect(str(INDEX_DB))
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _index_node(node_id, title, aliases, tags, content, now, content_hash=None, confidence=0.5):
    conn = _connect()
    alias_str = ", ".join(aliases) if isinstance(aliases, list) else aliases
    tag_str = ", ".join(tags) if isinstance(tags, list) else tags

    conn.execute(
        "INSERT OR REPLACE INTO nodes (id, title, aliases, status, confidence, "
        "verification_count, last_updated, tags, created_at, updated_at, content_hash) "
        "VALUES (?, ?, ?, 'active', ?, 0, ?, ?, ?, ?, ?)",
        (node_id, title, alias_str, confidence, now, tag_str, now, now, content_hash)
    )

    conn.execute("DELETE FROM nodes_fts WHERE rowid = (SELECT rowid FROM nodes WHERE id = ?)", (node_id,))
    conn.execute(
        "INSERT INTO nodes_fts (rowid, title, aliases, tags, content) "
        "VALUES ((SELECT rowid FROM nodes WHERE id = ?), ?, ?, ?, ?)",
        (node_id, title, " ".join(aliases) if isinstance(aliases, list) else aliases,
         " ".join(tags) if isinstance(tags, list) else tags, content)
    )

    # Queue embedding for async processing instead of blocking
    if _embeddings_enabled():
        conn.execute(
            "INSERT OR REPLACE INTO pending_embeddings (node_id, queued_at) VALUES (?, ?)",
            (node_id, now)
        )

    conn.commit()
    conn.close()


def pending_embedding_count():
    """How many entries are waiting for a vector."""
    init()
    conn = _connect()
    count = conn.execute("SELECT COUNT(*) FROM pending_embeddings").fetchone()[0]
    conn.close()
    return count


def process_pending_embeddings():
    """Process queued embeddings. Returns count of embeddings generated."""
    init()
    if not _embeddings_enabled():
        return 0
    conn = _connect()
    pending = conn.execute(
        "SELECT node_id, queued_at FROM pending_embeddings"
    ).fetchall()

    if not pending:
        conn.close()
        return 0

    processed = 0
    for node_id, _queued_at in pending:
        row = conn.execute(
            "SELECT title FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        if not row:
            # Node was deleted; remove from queue
            conn.execute("DELETE FROM pending_embeddings WHERE node_id = ?", (node_id,))
            continue

        title = row[0]
        # Read content from FTS table
        fts_row = conn.execute(
            "SELECT content FROM nodes_fts WHERE rowid = (SELECT rowid FROM nodes WHERE id = ?)",
            (node_id,)
        ).fetchone()
        content = fts_row[0] if fts_row else ""

        vec = embed_text(f"{title} {content[:500]}")
        if vec:
            now = time.strftime("%Y-%m-%d")
            conn.execute(
                "INSERT OR REPLACE INTO embeddings (node_id, vector, model, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (node_id, _pack_vector(vec), _embed_model(), now)
            )
            conn.execute("DELETE FROM pending_embeddings WHERE node_id = ?", (node_id,))
            processed += 1
        else:
            # Embedding service unavailable; leave in queue for retry
            break

    conn.commit()
    conn.close()
    return processed


_STOPWORDS = {"a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
              "have", "has", "had", "do", "does", "did", "will", "would", "could",
              "should", "may", "might", "shall", "can", "need", "dare", "to", "of",
              "in", "for", "on", "with", "at", "by", "from", "as", "into", "about",
              "like", "through", "after", "over", "between", "out", "against", "during",
              "without", "before", "under", "around", "among", "it", "its", "this",
              "that", "these", "those", "i", "me", "my", "we", "our", "you", "your",
              "he", "him", "his", "she", "her", "they", "them", "their", "what", "which",
              "who", "when", "where", "why", "how", "not", "no", "nor", "and", "but",
              "or", "so", "if", "then", "than", "too", "very", "just"}


def _search_bm25(conn, query, limit, include_superseded=False):
    expanded = query.replace("-", " ").replace("_", " ")
    queries_to_try = [query, expanded]

    # OR fallback: alphanumeric terms only (punctuation is FTS5 syntax — a
    # sentence query like `codeword?` errors every attempt otherwise), each
    # quoted so reserved words stay terms, deduped, capped so an arbitrarily
    # long caller query cannot balloon the MATCH expression.
    seen = set()
    meaningful = []
    for w in re.findall(r"[A-Za-z0-9]+", expanded):
        lw = w.lower()
        if lw in _STOPWORDS or len(w) < 2 or lw in seen:
            continue
        seen.add(lw)
        meaningful.append(w)
    if meaningful:
        queries_to_try.append(" OR ".join(f'"{w}"' for w in meaningful[:40]))

    status_clause = "" if include_superseded else "AND nodes.status = 'active'"
    for q in queries_to_try:
        try:
            rows = conn.execute(
                "SELECT nodes.id, nodes.title, bm25(nodes_fts) as score "
                "FROM nodes_fts JOIN nodes ON nodes_fts.rowid = nodes.rowid "
                f"WHERE nodes_fts MATCH ? {status_clause} ORDER BY score LIMIT ?",
                (q, limit)
            ).fetchall()
            if rows:
                return [(r[0], r[1], -r[2]) for r in rows]
        except sqlite3.OperationalError:
            continue
    return []


def _search_metadata(conn, query, limit, include_superseded=False):
    # Same \w+ extraction as text_words below, so punctuation in the query
    # ("codeword?") cannot mask a term that is plainly present.
    terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 2]
    if not terms:
        return []
    status_clause = "" if include_superseded else "WHERE status = 'active'"
    rows = conn.execute(
        f"SELECT id, title, tags, aliases FROM nodes {status_clause}"
    ).fetchall()
    scored = []
    for r in rows:
        text_words = set(re.findall(r"\b\w+\b", f"{r[1]} {r[2]} {r[3]}".lower()))
        hits = sum(1 for t in terms if t in text_words)
        ratio = hits / len(terms)
        if ratio >= 0.4:
            scored.append((r[0], r[1], ratio))
    scored.sort(key=lambda x: -x[2])
    return scored[:limit]


def _search_embeddings(conn, query, limit, include_superseded=False):
    global LAST_QUERY_EMBED_MS, LAST_QUERY_EMBED_OK
    LAST_QUERY_EMBED_MS = None
    LAST_QUERY_EMBED_OK = False
    if not _embeddings_enabled():
        return []
    # The read path embeds the query and nothing else — entry vectors are the
    # backlog's job. The short timeout is what keeps a sick ollama from
    # stalling a caller that is waiting on the search.
    start = time.perf_counter()
    query_vec = embed_text(query, timeout=_num_config("embed_query_timeout", QUERY_EMBED_TIMEOUT))
    LAST_QUERY_EMBED_MS = round((time.perf_counter() - start) * 1000)
    LAST_QUERY_EMBED_OK = bool(query_vec)
    if not query_vec:
        return []
    count = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    if count > _EMBED_SCAN_LIMIT:
        return []
    status_clause = "" if include_superseded else "WHERE n.status = 'active'"
    rows = conn.execute(
        "SELECT e.node_id, e.vector, n.title FROM embeddings e "
        f"JOIN nodes n ON e.node_id = n.id {status_clause}"
    ).fetchall()
    scored = []
    for node_id, vec_blob, title in rows:
        node_vec = _unpack_vector(vec_blob)
        sim = cosine_similarity(query_vec, node_vec)
        if sim > 0.3:
            scored.append((node_id, title or "", sim))
    scored.sort(key=lambda x: -x[2])
    return scored[:limit]


def _rrf_fuse(result_lists, limit):
    scores = {}
    titles = {}
    for results in result_lists:
        for rank, (node_id, title, _score) in enumerate(results):
            scores[node_id] = scores.get(node_id, 0) + 1.0 / (RRF_K + rank + 1)
            titles[node_id] = title
    ranked = sorted(scores.items(), key=lambda x: -x[1])[:limit]
    return [{"id": nid, "title": titles[nid], "score": round(score, 4)} for nid, score in ranked]


_MOC_FILENAME_UNSAFE_RE = re.compile(r"[\\/]")


def _moc_filename(tag):
    """Map a tag to a filesystem-safe MOC filename. Tags travel through
    frontmatter and MOC titles unchanged; only this derived filename is
    sanitized, so `/`-bearing tags like `I/O` or `CI/CD` don't get read as
    path structure by `Path`."""
    return f"{_MOC_FILENAME_UNSAFE_RE.sub('_', tag)}.md"


def _update_mocs(node_id, title, tags):
    """Update the MOC file for each tag. The node file is the durable
    artifact — a MOC write failure is logged and skipped rather than
    raised, so it can't abort a batch or strand the caller with an
    orphan node that already exists on disk."""
    for tag in tags:
        moc_path = MOCS_DIR / _moc_filename(tag)
        entry = f"* [[{node_id}]] — {title}\n"
        try:
            if moc_path.exists():
                content = moc_path.read_text(encoding="utf-8")
                if node_id not in content:
                    content = content.rstrip() + "\n" + entry
                    moc_path.write_text(content, encoding="utf-8")
            else:
                moc_path.write_text(f"# {tag}\n\n## Active\n{entry}", encoding="utf-8")
        except OSError as e:
            print(f"Warning: failed to update MOC for tag {tag!r}: {e}", file=sys.stderr)


def _parse_frontmatter(text):
    match = re.match(r"^---\n(.+?)\n---", text, re.DOTALL)
    if not match:
        return {}
    meta = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            key = key.strip()
            val = val.strip()
            if val.startswith("[") and val.endswith("]"):
                val = [v.strip() for v in val[1:-1].split(",") if v.strip()]
            elif val.replace(".", "").isdigit():
                val = float(val) if "." in val else int(val)
            meta[key] = val
    return meta


def _extract_body(text):
    match = re.match(r"^---\n.+?\n---\n?", text, re.DOTALL)
    if match:
        return text[match.end():]
    return text


def _pack_vector(vec):
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack_vector(blob):
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))
