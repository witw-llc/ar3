"""Member-level knowledge — k7e inject on wake, batch distill when idle.

The store is private to one member: `agents/<member>/k7e` under the roster's
state dir, one store per security principal per the research gate — tags
organize within a store, they never enforce between members. The shared tier
is the workplace repo itself. Inject happens host-side at prompt build, so no
mount crosses the isolation boundary; distill happens in idle passes
("dreaming") over turn captures, never inside a turn. Everything fails soft:
a knowledge problem may cost the section, never the turn.

The semantic track follows the same rule. Dreaming pays for entry vectors
(`k7e embed-pending` over the store's queue); a wake embeds only the seed query,
on a budget short enough that an absent ollama costs the section its semantic
half and the turn nothing.

k7e is driven as a subprocess — the CLI is the stable surface, and importing
k7e in-process would couple r4t to k7e's internals for no reason.

Dreaming's distill rig is the member's own turn rig by default (the K2
verdict: least surprising, costs nothing extra, and not broken at 88%
fact-write fidelity) or the `Knowledge:` line's rig-name override for a
member whose own rig writes smoothed-over notes. Either way it bridges to k7e
as `K7E_DISTILL_COMMAND` in the subprocess env — `Rig.distill_command` turns
the rig's own invoke into the stdin->stdout shell command k7e expects.

The `## Knowledge` section is packed rank-proportionally, not greedily
(k-budget-packing): the budget splits across the top search hits by
a 1/(rank+1) weight with unspent slack swept back down the ranks, so a
budget too small for the top hit's whole snippet still surfaces evidence
from ranks 2 and 3 instead of spending everything on rank 1. Each entry's
preamble — the provenance header plus the staleness status line when present
— is atomic and never truncated; only the snippet backs off to a line or
sentence boundary. An entry whose share can't cover preamble + a minimum
snippet is skipped outright, never emitted as a content-free stub.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import state
from rig import RigConfig, resolve_framing, resolve_knowledge_bytes

K7E_ENTRY = Path(__file__).resolve().parent.parent / "k7e" / "k7e.py"
SEARCH_LIMIT = 8
# The rank-proportional packer's weighting pool (k-budget-packing
# s4-rank-proportional) — matches SEARCH_LIMIT because a search
# already returns at most that many hits, so every hit found earns a
# 1/(rank+1) share before slack sweeps back down the ranks.
RANK_POOL = SEARCH_LIMIT
# The smallest snippet worth an entry's overhead. Below this the content is
# gone in all but name, so the entry is skipped rather than reduced to a
# stub carrying only its provenance.
MIN_SNIPPET = 120
# How much of a truncated snippet a line/sentence boundary backoff may spend
# to land on a clean edge. Below this the cut is arbitrary anyway, so keep
# the bytes over chasing a boundary that costs too much of the budget.
BOUNDARY_KEEP = 0.6
DREAM_BATCH = 5  # captures distilled per member per idle pass
SEED_BODY_MAX = 400
# The wake budget. k7e's own query-embed timeout is a couple of seconds, so a
# search that runs past this is a sick store, not a slow one — and a member's
# turn is not the place to wait it out.
SEARCH_TIMEOUT = 15
EMBED_TIMEOUT = 300  # dreaming embeds a whole backlog; nobody is waiting

_EMBED_MS_RE = re.compile(r"^embed (\d+)ms( \(semantic track unavailable\))?$", re.MULTILINE)

KNOWLEDGE_HEADER = "## Knowledge (recalled from your private store)"
# The built-in framing line — `resolve_framing`'s fallback when neither the
# member's `Framing:` roster line nor the rig's own default says
# otherwise. A member/rig `Framing: off` drops this line but never the
# header: the entries still need SOME introduction.
KNOWLEDGE_FRAMING = (
    "Notes your past turns distilled — background that may be stale or "
    "wrong. When they disagree with the messages above or your own files, "
    "the messages and files win."
)
# k-age-presentation: on the 4B floor the absolute-date stamp measured
# worse than no date at all (official-looking dates read as authority, not
# staleness); relative age + this status line was the only presentation that
# worked on both reader classes. Threshold and wording are copied verbatim
# from the experiment's fixtures/presentation.json, not re-derived here.
KNOWLEDGE_STALE_DAYS = 30
KNOWLEDGE_STATUS_LINE = (
    "Status: possibly superseded -- do not treat as current unless corroborated."
)


def store_home(node: str, name: str) -> Path:
    return state.agent_dir(node, name) / "k7e"


def _run_k7e(
    home: Path, *args: str, timeout: float = 60, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["K7E_HOME"] = str(home)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(K7E_ENTRY), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def _embed_note(stderr: str) -> str:
    """What the semantic track cost this wake, from k7e's own timing note.
    No note means the track never ran — FTS5 carried the search alone."""
    match = _EMBED_MS_RE.search(stderr)
    if not match:
        return "fts-only"
    if match.group(2):
        return f"embed {match.group(1)}ms unanswered, fts-only"
    return f"embed {match.group(1)}ms"


def _distill_skips(stdout: str) -> list[str]:
    """The files k7e could not read, from the lines its CLI prints for them."""
    return [
        line.strip()[len("[skipped] "):]
        for line in stdout.splitlines()
        if line.strip().startswith("[skipped] ")
    ]


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


def _age_days(date: str) -> int | None:
    """Days between `date` (k7e's `last_updated`, `YYYY-MM-DD`) and today, or
    None when `date` is empty or unparseable — the bare-id provenance case."""
    if not date:
        return None
    try:
        stamped = datetime.date.fromisoformat(date)
    except ValueError:
        return None
    return (datetime.date.today() - stamped).days


def _age_label(days: int) -> str:
    return "today" if days < 1 else f"{days}d old"


def _boundary(text: str) -> int:
    """Index just past the last line break or sentence end in `text`."""
    best = text.rfind("\n") + 1
    for end in (". ", "! ", "? "):
        found = text.rfind(end)
        if found != -1:
            best = max(best, found + 1)
    return best


def _fit_snippet(snippet: str, budget: int) -> str:
    """`snippet` cut to `budget` bytes, backed off to the last line or
    sentence boundary when that keeps most of the cut — below BOUNDARY_KEEP
    the boundary is arbitrary anyway, so keep the bytes instead."""
    if len(snippet.encode("utf-8")) <= budget:
        return snippet
    cut = _fit(snippet, budget)
    edge = _boundary(cut)
    if edge >= len(cut) * BOUNDARY_KEEP:
        cut = cut[:edge]
    return cut.rstrip()


def _entry_overhead(preamble: str) -> int:
    return len(preamble.encode("utf-8")) + 2  # the "\n\n" before the snippet


def _pack_rank_proportional(entries: list[dict], budget: int) -> list[tuple[int, str]]:
    """`budget` bytes of `entries` (already in rank order, each a
    `{"preamble", "snippet"}` dict) as `(index, block)` pairs in rank order,
    packed by k-budget-packing's s4-rank-proportional strategy:
    split the budget across the entries by a 1/(rank+1) weight, then sweep
    unspent slack back down the ranks so nothing is left on the table. The
    preamble is atomic — provenance and a staleness stamp mean nothing
    half-eaten — so only the snippet ever truncates, and an entry whose
    share can't cover its preamble plus MIN_SNIPPET bytes of snippet is
    skipped rather than emitted as a stub. The index lets the caller — which
    fetched every entry to size it but only injects the ones that survive —
    tell which entries actually made the prompt."""
    weights = [1 / (i + 1) for i in range(len(entries))]
    total = sum(weights) or 1.0
    grant: dict[int, int] = {}
    used = 0
    for i, weight in enumerate(weights):
        preamble, snippet = entries[i]["preamble"], entries[i]["snippet"]
        full = len(snippet.encode("utf-8"))
        allowance = min(int(budget * weight / total) - _entry_overhead(preamble), full)
        if allowance < min(MIN_SNIPPET, full):
            continue
        grant[i] = allowance
        used += _entry_overhead(preamble) + allowance
    leftover = budget - used
    for i in sorted(grant):
        if leftover <= 0:
            break
        full = len(entries[i]["snippet"].encode("utf-8"))
        add = min(leftover, full - grant[i])
        if add > 0:
            grant[i] += add
            leftover -= add
    return [
        (i, f"{entries[i]['preamble']}\n\n{_fit_snippet(entries[i]['snippet'], grant[i])}")
        for i in sorted(grant)
    ]


def _touch_injected(home: Path, ids: list[str]) -> None:
    """Bump k7e's usage ranking signal for the entries that survived packing
    into the prompt — a fetch during sizing reads `--no-track`, so this is
    the one write that makes "recalled" mean "the model actually saw it".
    Best-effort: a knowledge problem may cost the section, never the turn."""
    if not ids:
        return
    try:
        _run_k7e(home, "touch", *ids)
    except Exception:
        pass


def knowledge_section(ctx, member, batch: list[dict], rig=None) -> list[str]:
    """The `## Knowledge` prompt section for a member whose flag is on, or []
    — on any failure, empty store included. `rig` is the member's own turn
    rig, which tiers the inject budget when the roster line named no explicit
    size (`resolve_knowledge_bytes`) and supplies the rig-level `Framing:`
    default when the member names none (`resolve_framing`). Injection,
    not fetch, is what counts as a recall: sizing reads every pool entry with
    `get --no-track`, and one `touch` afterward bumps the k7e usage counter
    only for entries that survived packing into the prompt."""
    budget = resolve_knowledge_bytes(member, rig)
    if budget <= 0:
        return []
    home = store_home(ctx.node, member.name)
    if not (home / "nodes").is_dir():
        return []
    query = _seed_query(ctx, member, batch)
    started = time.perf_counter()
    try:
        res = _run_k7e(
            home, "search", query, "--json", "--limit", str(SEARCH_LIMIT),
            timeout=SEARCH_TIMEOUT,
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
    search_ms = round((time.perf_counter() - started) * 1000)
    embed_note = _embed_note(res.stderr or "")
    # Fetch before packing: the rank-proportional split needs every pool
    # entry's snippet size to weigh and redistribute, so — unlike the old
    # greedy loop — fetching does not stop early just because the entries
    # seen so far already cover the budget. It still stops at RANK_POOL:
    # a hit past the weighting pool would never earn a share, so getting it
    # would only cost bytes for nothing. This sizing read is untracked
    # (`--no-track`) because most of what it reads never reaches the prompt
    # — `_touch_injected` below bumps usage for what does. One call for the
    # whole pool: the per-entry read is trivial next to interpreter startup,
    # so N gets cost N startups and a wake must never wait on this path.
    pool = [str(hit["id"]) for hit in hits[:RANK_POOL]]
    texts: dict[str, str] = {}
    if pool:
        try:
            got = _run_k7e(home, "get", *pool, "--no-track", "--json")
            if got.returncode == 0 or got.stdout.strip():
                texts = {e["id"]: e["text"] for e in json.loads(got.stdout or "[]")}
        except Exception:
            texts = {}
    entries: list[dict] = []
    for hit in hits[:RANK_POOL]:
        text = texts.get(str(hit["id"]))
        if text is None:
            continue
        date, snippet = _entry_snippet(text)
        if not snippet:
            continue
        age_days = _age_days(date)
        provenance = (
            f"({hit['id']}, {_age_label(age_days)})" if age_days is not None
            else f"({hit['id']})"
        )
        header = f"### {hit.get('title', hit['id'])} {provenance}"
        preamble = (
            f"{header}\n\n{KNOWLEDGE_STATUS_LINE}"
            if age_days is not None and age_days > KNOWLEDGE_STALE_DAYS
            else header
        )
        entries.append({"id": hit["id"], "preamble": preamble, "snippet": snippet})
    packed = _pack_rank_proportional(entries, budget)
    blocks = [block for _, block in packed]
    used = sum(len(b.encode("utf-8")) for b in blocks)
    _touch_injected(home, [entries[i]["id"] for i, _ in packed])
    total_ms = round((time.perf_counter() - started) * 1000)
    state.append_log(
        ctx.node,
        f"r4t: KNOWLEDGE {member.name.lower()} {len(blocks)} "
        f"entr{'y' if len(blocks) == 1 else 'ies'} {used}B in {total_ms}ms "
        f"(search {search_ms}ms, {embed_note})",
    )
    if not blocks:
        return []
    spec = resolve_framing(member, rig)
    parts = [KNOWLEDGE_HEADER]
    if not spec.off:
        parts.append(spec.text if spec.text is not None else KNOWLEDGE_FRAMING)
    parts.append("")
    for block in blocks:
        parts += [block, ""]
    return parts


def _embed_backlog(ctx, member_name: str, home: Path) -> None:
    """Give the store's queued entries their vectors. Storing an entry only
    queues it, so this pass is where the semantic track pays for itself —
    dreaming has all the time in the world and a waking member has none.
    An unreachable ollama leaves the queue intact for the next pass."""
    if not (home / "nodes").is_dir():
        return
    started = time.perf_counter()
    try:
        res = _run_k7e(home, "embed-pending", "--json", timeout=EMBED_TIMEOUT)
        if res.returncode != 0:
            raise RuntimeError(
                (res.stderr or res.stdout).strip() or f"exit {res.returncode}"
            )
        report = json.loads(res.stdout or "{}")
    except Exception as e:
        state.append_log(
            ctx.node, f"r4t: DREAM-EMBED-SKIP {member_name.lower()} {e}"
        )
        return
    embedded = report.get("embedded", 0)
    pending = report.get("pending", 0)
    if not embedded and not pending:
        return
    elapsed = time.perf_counter() - started
    if embedded:
        per = round(elapsed * 1000 / embedded)
        state.append_log(
            ctx.node,
            f"r4t: DREAM-EMBED {member_name.lower()} embedded {embedded} "
            f"entr{'y' if embedded == 1 else 'ies'} in {elapsed:.1f}s "
            f"({per}ms each)",
        )
    if pending:
        state.append_log(
            ctx.node,
            f"r4t: DREAM-EMBED-SKIP {member_name.lower()} {pending} "
            f"entr{'y' if pending == 1 else 'ies'} still queued — embeddings "
            "unavailable; the store searches FTS-only until ollama answers",
        )


def resolve_distill_rig(member, config: RigConfig):
    """(rig, error) for `member`'s dreaming pass: the `Knowledge:` line's rig
    name override when present, else the member's own turn rig (resolved the
    same way dispatch resolves it, pins included). A named override that
    doesn't match a configured rig is a member error, not a soft skip — it is
    surfaced here with a message dream_sweep logs, and by `r4t roster check`
    before it ever gets this far."""
    if member.knowledge_distill_rig:
        name = member.knowledge_distill_rig
        if config.missing:
            return None, f"rig config missing ({config.path})"
        rig = config.rigs.get(name)
        if rig is None:
            return None, f"Knowledge distill rig {name!r} not found in {config.path}"
        if rig.error:
            return None, f"Knowledge distill rig {name!r} is invalid: {rig.error}"
        return rig, None
    return config.rig_for(member)[:2]


def dream_sweep(ctx, roster, config: RigConfig) -> list[str]:
    """Distill fresh turn captures into each knowledge-carrying member's store
    — the async consolidation pass the research gate calls dreaming. Runs only
    from idle, bounded per member per pass; the `.dreamed` watermark advances
    only on success, so nothing is lost to a transient failure and a store
    without a distill LLM simply waits. Every pass also drains the store's
    embedding backlog, so the semantic track never rides a wake. Returns
    members that dreamed."""
    dreamed: list[str] = []
    for m in roster.members:
        if m.is_human or not m.knowledge_on:
            continue
        home = store_home(ctx.node, m.name)
        if _distill_fresh(ctx, m, home, config):
            dreamed.append(m.name)
        _embed_backlog(ctx, m.name, home)
    return dreamed


def _distill_fresh(ctx, m, home: Path, config: RigConfig) -> bool:
    captures = state.list_turn_captures(ctx.node, m.name)
    mark = home / ".dreamed"
    try:
        last = mark.read_text(encoding="utf-8").strip()
    except OSError:
        last = ""
    fresh = [c for c in captures if c.name > last][:DREAM_BATCH]
    if not fresh:
        return False
    distill_rig, rig_err = resolve_distill_rig(m, config)
    if distill_rig is None:
        state.append_log(
            ctx.node,
            f"r4t: DREAM-SKIP {m.name.lower()} {rig_err}; "
            f"{len(fresh)} capture(s) wait",
        )
        return False
    extra_env = {}
    cmd = distill_rig.distill_command(home)
    if cmd:
        extra_env["K7E_DISTILL_COMMAND"] = cmd
    try:
        res = _run_k7e(
            home, "distill", *[str(c) for c in fresh], timeout=600, extra_env=extra_env
        )
    except Exception as e:
        state.append_log(
            ctx.node,
            f"r4t: DREAM-FAIL {m.name.lower()} distill died: {e}; "
            f"{len(fresh)} capture(s) wait for the next idle pass",
        )
        return False
    if res.returncode != 0:
        reason = (res.stderr or res.stdout).strip().splitlines()
        state.append_log(
            ctx.node,
            f"r4t: DREAM-SKIP {m.name.lower()} distill exit "
            f"{res.returncode} ({reason[0] if reason else 'no output'}); "
            f"{len(fresh)} capture(s) wait",
        )
        return False
    home.mkdir(parents=True, exist_ok=True)
    mark.write_text(fresh[-1].name + "\n", encoding="utf-8")
    state.append_log(
        ctx.node,
        f"r4t: DREAM {m.name.lower()} distilled {len(fresh)} capture(s) "
        "into the knowledge store",
    )
    # A capture k7e could not read is dropped from the sweep and never comes
    # back — the mark advances past it. A successful dream is exactly when that
    # would go unnoticed, so the skips get their own line.
    for line in _distill_skips(res.stdout or ""):
        state.append_log(ctx.node, f"r4t: DREAM-SKIPPED {m.name.lower()} {line}")
    return True
