"""Store hygiene auditor — checks structural integrity of knowledge nodes."""

import re
import time
from pathlib import Path

import engine


def run_audit(fix=False):
    """Audit store for structural issues. Returns list of issues found."""
    engine.init()
    nodes = list(engine._all_node_files())
    mocs = list(engine.MOCS_DIR.glob("*.md"))
    assets = [f for f in engine.ASSETS_DIR.rglob("*.*") if f.name != ".gitkeep"]

    node_ids = {n.stem for n in nodes}
    tag_to_nodes = {}
    referenced_assets = set()
    issues = []

    for node_path in nodes:
        text = node_path.read_text(encoding="utf-8")
        meta = engine._parse_frontmatter(text)
        node_id = node_path.stem

        required = ["id", "title", "status", "last_updated", "tags"]
        missing = [f for f in required if f not in meta]
        if missing:
            issues.append(f"[{node_id}] Missing fields: {', '.join(missing)}")

        tags = meta.get("tags", [])
        if not tags:
            issues.append(f"[{node_id}] No tags assigned")
        for tag in tags:
            tag_to_nodes.setdefault(tag, []).append(node_id)

        for link in re.findall(r"\[\[(K7E-\d{3}-\d{5})\]\]", text):
            if link not in node_ids:
                issues.append(f"[{node_id}] Dead link to [[{link}]]")

        for ref in re.findall(r"assets/([a-f0-9]{2}/[a-f0-9]+\.[a-z0-9]+)", text):
            referenced_assets.add(ref)

    moc_filenames = {m.name for m in mocs}
    for tag in tag_to_nodes:
        if engine._moc_filename(tag) not in moc_filenames:
            issues.append(f"[Tag: {tag}] No MOC file exists")
            if fix:
                first_node_id = tag_to_nodes[tag][0]
                first_node_path = engine._node_path(first_node_id)
                engine._update_mocs(
                    first_node_id,
                    engine._parse_frontmatter(
                        first_node_path.read_text(encoding="utf-8")
                    ).get("title", ""),
                    [tag]
                )

    for asset in assets:
        # Build relative path: bucket/filename
        rel = f"{asset.parent.name}/{asset.name}"
        if rel not in referenced_assets:
            issues.append(f"[Asset: {rel}] Unreferenced")
            if fix:
                asset.unlink()

    return issues


def index_disagreement():
    """Compare on-disk node count to the SQLite-indexed count. Markdown files
    are the store's source of truth; the index is a derived, rebuildable
    cache that can go stale or missing without touching a single node file.
    Returns a message describing the gap, or None when they agree."""
    engine.init()
    store_count = sum(1 for _ in engine._all_node_files())
    conn = engine._connect()
    indexed_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    conn.close()
    if store_count == indexed_count:
        return None
    return f"{store_count} entr(ies), {indexed_count} indexed — run k7e reindex"
