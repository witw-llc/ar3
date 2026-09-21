"""Store hygiene auditor — checks structural integrity of knowledge nodes."""

import re
import time
from pathlib import Path

import engine
from ar3.fsio import atomic_write_text


@engine._write_locked
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
    conn = engine._connect()
    indexed = dict(conn.execute("SELECT id, superseded_by FROM nodes").fetchall())
    conn.close()
    changed = False

    for node_path in nodes:
        text = node_path.read_text(encoding="utf-8")
        meta = engine._parse_frontmatter(text)
        node_id = node_path.stem
        repaired = text
        body = engine._extract_body(text)
        collapsed, duplicates = engine.collapse_sections(body)
        if duplicates:
            issues.append(f"[{node_id}] Duplicate sections: {', '.join(dict.fromkeys(duplicates))}")
            repaired = text[:len(text) - len(body)] + "\n" + collapsed
        front = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        pointers = re.findall(r"(?m)^superseded_by:[ \t]*(.*)$", front.group(1)) if front else []
        if len(pointers) > 1:
            issues.append(f"[{node_id}] Duplicate superseded_by pointers")
            # The old writer prepended its newest pointer. A surviving index
            # value disambiguates that malformed file; valid files remain truth.
            pointer = indexed.get(node_id) or pointers[0]
            repaired = engine._set_frontmatter_key(repaired, "superseded_by", pointer)
        if fix and repaired != text:
            atomic_write_text(node_path, repaired, fsync=True)
            changed = True

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

    if fix and (changed or index_disagreement()):
        engine.reindex()
    return issues


def index_disagreement():
    """Compare the store's files to the SQLite index — how many, and what each
    one's status is. Markdown files are the store's source of truth; the index
    is a derived, rebuildable cache that can go stale or missing without
    touching a single node file.

    Status is worth its own comparison because search reads only the index. A
    file that says superseded and a row that says active is a retired claim
    that still ranks, and nothing in a count catches it.

    Returns a message describing the gap, or None when they agree."""
    engine.init()
    conn = engine._connect()
    indexed = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, status, superseded_by FROM nodes")}
    conn.close()
    store_count = 0
    disagreed = []
    file_ids = set()
    for path in engine._all_node_files():
        store_count += 1
        meta = engine._parse_frontmatter(path.read_text(encoding="utf-8"))
        node_id = meta.get("id", path.stem)
        file_ids.add(node_id)
        on_disk = meta.get("status", "active")
        if node_id in indexed:
            if indexed[node_id][0] != on_disk:
                disagreed.append(f"{node_id} is {on_disk} but indexed {indexed[node_id][0]}")
            if (indexed[node_id][1] or "") != meta.get("superseded_by", ""):
                disagreed.append(f"{node_id} superseded_by differs from its index")
    if store_count == len(indexed) and file_ids != set(indexed):
        disagreed.append("indexed IDs differ from node files")
    if store_count == len(indexed) and not disagreed:
        return None
    gaps = []
    if store_count != len(indexed):
        gaps.append(f"{store_count} entr(ies), {len(indexed)} indexed")
    gaps.extend(disagreed)
    return "; ".join(gaps) + " — run k7e reindex"
