"""Member-level knowledge — k7e inject on wake, batch distill when idle (#41).

The store is private to one member: `agents/<member>/k7e` under the roster's
state dir, one store per security principal per the research gate — tags
organize within a store, they never enforce between members. The shared tier
is the workplace repo itself. Inject happens host-side at prompt build, so no
mount crosses the isolation boundary; distill happens in idle passes
("dreaming") over turn captures, never inside a turn. Everything fails soft:
a knowledge problem may cost the section, never the turn.

k7e is driven as a subprocess — the a8s/r4t `ulid` modules shadow an import,
and the CLI is the stable surface anyway.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import state

K7E_ENTRY = Path(__file__).resolve().parent.parent / "k7e" / "k7e.py"
SEARCH_LIMIT = 8
DREAM_BATCH = 5  # captures distilled per member per idle pass
SEED_BODY_MAX = 400

KNOWLEDGE_HEADER = "## Knowledge (recalled from your private store)"
KNOWLEDGE_FRAMING = (
    "Notes your past turns distilled — background that may be stale or "
    "wrong. When they disagree with the messages above or your own files, "
    "the messages and files win."
)


def store_home(node: str, name: str) -> Path:
    return state.agent_dir(node, name) / "k7e"


def _run_k7e(home: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["K7E_HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(K7E_ENTRY), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def _seed_query(ctx, member, batch: list[dict]) -> str:
    """Retrieval seed per the research gate: newest message + who is waking +
    the mission's first line. The member never sees this — it only steers
    which notes surface."""
    parts = [member.name, member.role]
    try:
        mission = (ctx.root / "MISSION.md").read_text(encoding="utf-8").strip()
        if mission:
            parts.append(mission.splitlines()[0].lstrip("# ").strip())
    except OSError:
        pass
    if batch:
        parts.append(str(batch[-1].get("body", ""))[:SEED_BODY_MAX])
    return " ".join(p for p in parts if p)


def _entry_snippet(raw: str) -> tuple[str, str]:
    """(date, body) from a full k7e entry. The body keeps only sections that
    say something — the empty scaffolding headers and the History changelog
    would spend inject budget on noise."""
    date = ""
    body = raw
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            front = raw[3:end]
            m = re.search(r"^last_updated:\s*(\S+)", front, re.MULTILINE)
            if m:
                date = m.group(1)
            body = raw[end + 4:]
    kept: list[str] = []
    for section in re.split(r"(?m)^(?=## )", body):
        section = section.strip()
        if not section:
            continue
        if section.startswith("## "):
            head, _, rest = section.partition("\n")
            if head.removeprefix("## ").strip().lower() == "history":
                continue
            if not rest.strip():
                continue
        kept.append(section)
    return date, "\n\n".join(kept).strip()


def _fit(text: str, budget: int) -> str:
    return text.encode("utf-8")[:budget].decode("utf-8", "ignore")


def knowledge_section(ctx, member, batch: list[dict]) -> list[str]:
    """The `## Knowledge` prompt section for a member whose flag is on, or []
    — on any failure, empty store included. Reading an entry bumps its k7e
    usage counter (get, not search, is what counts as a recall)."""
    budget = member.knowledge_bytes
    if budget <= 0:
        return []
    home = store_home(ctx.node, member.name)
    if not (home / "nodes").is_dir():
        return []
    query = _seed_query(ctx, member, batch)
    try:
        res = _run_k7e(
            home, "search", query, "--json", "--limit", str(SEARCH_LIMIT)
        )
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or f"search exit {res.returncode}")
        hits = json.loads(res.stdout or "[]")
    except Exception as e:
        state.append_log(
            ctx.node,
            f"r4t: KNOWLEDGE-SKIP {member.name.lower()} search failed: {e}",
        )
        return []
    blocks: list[str] = []
    used = 0
    for hit in hits:
        if used >= budget:
            break
        try:
            got = _run_k7e(home, "get", str(hit["id"]))
        except Exception:
            continue
        if got.returncode != 0:
            continue
        date, snippet = _entry_snippet(got.stdout)
        if not snippet:
            continue
        provenance = f"({hit['id']}, {date})" if date else f"({hit['id']})"
        block = f"### {hit.get('title', hit['id'])} {provenance}\n\n{snippet}"
        size = len(block.encode("utf-8"))
        if used + size > budget:
            if blocks:
                break
            block = _fit(block, budget)
            size = len(block.encode("utf-8"))
        blocks.append(block)
        used += size
    if not blocks:
        return []
    parts = [KNOWLEDGE_HEADER, KNOWLEDGE_FRAMING, ""]
    for block in blocks:
        parts += [block, ""]
    return parts


def dream_sweep(ctx, roster) -> list[str]:
    """Distill fresh turn captures into each knowledge-carrying member's store
    — the async consolidation pass the research gate calls dreaming. Runs only
    from idle, bounded per member per pass; the `.dreamed` watermark advances
    only on success, so nothing is lost to a transient failure and a store
    without a distill LLM simply waits. Returns members that dreamed."""
    dreamed: list[str] = []
    for m in roster.members:
        if m.is_human or m.knowledge_bytes <= 0:
            continue
        captures = state.list_turn_captures(ctx.node, m.name)
        home = store_home(ctx.node, m.name)
        mark = home / ".dreamed"
        try:
            last = mark.read_text(encoding="utf-8").strip()
        except OSError:
            last = ""
        fresh = [c for c in captures if c.name > last][:DREAM_BATCH]
        if not fresh:
            continue
        try:
            res = _run_k7e(
                home, "distill", *[str(c) for c in fresh], timeout=600
            )
        except Exception as e:
            state.append_log(
                ctx.node,
                f"r4t: DREAM-FAIL {m.name.lower()} distill died: {e}; "
                f"{len(fresh)} capture(s) wait for the next idle pass",
            )
            continue
        if res.returncode != 0:
            reason = (res.stderr or res.stdout).strip().splitlines()
            state.append_log(
                ctx.node,
                f"r4t: DREAM-SKIP {m.name.lower()} distill exit "
                f"{res.returncode} ({reason[0] if reason else 'no output'}); "
                f"{len(fresh)} capture(s) wait",
            )
            continue
        home.mkdir(parents=True, exist_ok=True)
        mark.write_text(fresh[-1].name + "\n", encoding="utf-8")
        state.append_log(
            ctx.node,
            f"r4t: DREAM {m.name.lower()} distilled {len(fresh)} capture(s) "
            "into the knowledge store",
        )
        dreamed.append(m.name)
    return dreamed
