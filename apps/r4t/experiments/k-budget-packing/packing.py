"""Four candidate packers for the `## Knowledge` inject budget (#52/#12).

Pure functions over (entries in rank order, budget in bytes) -> packed blocks.
No k7e, no r4t state, no I/O: the experiment feeds them LongMemEval entries,
and `knowledge.knowledge_section` can adopt a winner by swapping its loop for
one call.

An entry is `{"preamble": str, "snippet": str}`. Every emitted block is
`preamble + "\\n\\n" + snippet`, and **the preamble is atomic** — it carries
the id/date provenance stamp today and the age campaign's `Status:` line
tomorrow, both of which mean less than nothing when half-eaten. So truncation
only ever shortens the snippet, and an allocation too small for
preamble + MIN_SNIPPET skips the entry rather than emitting a stub.

s1_greedy_whole is the shipped control and is deliberately exempt from that
rule: it reproduces `knowledge_section`'s byte-slice fallback verbatim,
including the one case (a lone oversized first entry) where the slice can land
inside the stamp.
"""
from __future__ import annotations

MIN_SNIPPET = 120
DEFAULT_TARGET_ENTRIES = 3
DEFAULT_HEAD_BYTES = 256
DEFAULT_RANK_POOL = 8
# How much of an allocation a line/sentence backoff may spend to land on a
# clean edge. Below this the cut is arbitrary anyway, so keep the bytes.
BOUNDARY_KEEP = 0.6


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def _fit(text: str, budget: int) -> str:
    return text.encode("utf-8")[:budget].decode("utf-8", "ignore")


def _boundary(text: str) -> int:
    """Index just past the last line break or sentence end in `text`."""
    best = text.rfind("\n") + 1
    for end in (". ", "! ", "? "):
        found = text.rfind(end)
        if found != -1:
            best = max(best, found + 1)
    return best


def fit_snippet(snippet: str, budget: int) -> str:
    if _size(snippet) <= budget:
        return snippet
    cut = _fit(snippet, budget)
    edge = _boundary(cut)
    if edge >= len(cut) * BOUNDARY_KEEP:
        cut = cut[:edge]
    return cut.rstrip()


def _overhead(entry: dict) -> int:
    return _size(entry["preamble"]) + 2


def _pack(index: int, entry: dict, snippet: str) -> dict:
    block = f"{entry['preamble']}\n\n{snippet}"
    return {"index": index, "block": block, "bytes": _size(block)}


def _deepen(entries: list[dict], grant: dict[int, int], leftover: int) -> None:
    """Spend `leftover` snippet bytes top-down, taking each granted entry to
    its full snippet before moving on to the next."""
    for i in sorted(grant):
        if leftover <= 0:
            break
        full = _size(entries[i]["snippet"])
        add = min(leftover, full - grant[i])
        if add > 0:
            grant[i] += add
            leftover -= add


def _emit(entries: list[dict], grant: dict[int, int]) -> list[dict]:
    return [
        _pack(i, entries[i], fit_snippet(entries[i]["snippet"], grant[i]))
        for i in sorted(grant)
    ]


def s1_greedy_whole(entries: list[dict], budget: int) -> list[dict]:
    """The control: whole entries in rank order, stop at the first overflow.
    One long top hit spends the whole budget and the rest never appear."""
    packed: list[dict] = []
    used = 0
    for i, entry in enumerate(entries):
        if used >= budget:
            break
        block = _pack(i, entry, entry["snippet"])
        if used + block["bytes"] > budget:
            if packed:
                break
            text = _fit(block["block"], budget)
            block = {"index": i, "block": text, "bytes": _size(text)}
        packed.append(block)
        used += block["bytes"]
    return packed


def s2_per_entry_cap(
    entries: list[dict], budget: int, target_entries: int = DEFAULT_TARGET_ENTRIES
) -> list[dict]:
    """Cap every entry at budget // target_entries, then keep walking the rank
    order with whatever budget the caps left over."""
    cap = budget // target_entries
    packed: list[dict] = []
    used = 0
    for i, entry in enumerate(entries):
        allowance = min(cap, budget - used) - _overhead(entry)
        if allowance < min(MIN_SNIPPET, _size(entry["snippet"])):
            continue
        block = _pack(i, entry, fit_snippet(entry["snippet"], allowance))
        packed.append(block)
        used += block["bytes"]
    return packed


def s3_head_then_fill(
    entries: list[dict], budget: int, head_bytes: int = DEFAULT_HEAD_BYTES
) -> list[dict]:
    """Guarantee the top entries a small head each, then spend what is left
    deepening them from the top down."""
    grant: dict[int, int] = {}
    used = 0
    for i, entry in enumerate(entries):
        head = min(head_bytes, _size(entry["snippet"]))
        cost = _overhead(entry) + head
        if used + cost > budget:
            break
        grant[i] = head
        used += cost
    _deepen(entries, grant, budget - used)
    return _emit(entries, grant)


def s4_rank_proportional(
    entries: list[dict], budget: int, pool: int = DEFAULT_RANK_POOL
) -> list[dict]:
    """Split the budget across the top `pool` hits by a 1/(rank+1) weight,
    then sweep the slack (allocations bigger than their entry) back down the
    ranks so nothing is left on the table."""
    weights = [1 / (i + 1) for i in range(min(pool, len(entries)))]
    total = sum(weights) or 1.0
    grant: dict[int, int] = {}
    used = 0
    for i, weight in enumerate(weights):
        full = _size(entries[i]["snippet"])
        allowance = min(int(budget * weight / total) - _overhead(entries[i]), full)
        if allowance < min(MIN_SNIPPET, full):
            continue
        grant[i] = allowance
        used += _overhead(entries[i]) + allowance
    _deepen(entries, grant, budget - used)
    return _emit(entries, grant)


STRATEGIES = {
    "s1-greedy-whole": s1_greedy_whole,
    "s2-per-entry-cap": s2_per_entry_cap,
    "s3-head-then-fill": s3_head_then_fill,
    "s4-rank-proportional": s4_rank_proportional,
}
