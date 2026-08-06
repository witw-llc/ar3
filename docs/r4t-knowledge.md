# Knowledge — a member's private k7e memory

Experimental (#41, #52): default off, and the defaults below are lab knobs
until the K0 experiment freezes them.

A member with a `Knowledge:` roster line gets a private
[k7e](k7e.md) store and two automatic behaviors around it:

```markdown
### Wren
- **Rig:** claude
- **Knowledge:** on          # or: off | small | medium | large | 4k | 4096
```

**Every amount here is BYTES, never tokens.** `4k` means 4096 bytes of UTF-8
prompt text, not 4000 tokens — a token is several bytes, so this budgets far
less context than the number alone suggests.

## Grammar

In ascending specificity:

| Line | Meaning |
|---|---|
| `off` (or absent) | No store, no section — the prompt is byte-identical to a roster that never heard of the field. |
| `on` | Store on, inject budget from the rig's tier (below). |
| `small` (4096) / `medium` (8192) / `large` (32768) | T-shirt sizes — the **primary** grammar. Mapped to bytes by a table r4t owns (`roster.KNOWLEDGE_SIZES`), so a roster written today stays meaningful as usable context grows: move what `large` means in one place and every roster using it moves with it. `large` is currently unreachable in practice: `SEARCH_LIMIT` caps the retrieved pool well under 32768 bytes mean, tracked as its own issue rather than folded into this one. |
| `4k` / `4096` | An exact byte count — the escape hatch for a budget the sizes don't fit. |
| `<rig>` (e.g. `claude`) | A distill-rig override (below) at the tier default budget. |
| `<size> <rig>` (e.g. `4k claude`, `large agy`) | Both together, in either order. |

Sizes are a closed set (the three words above, plus a bare byte count); any
other single token is read as a rig name. A name that doesn't match a
configured rig is caught by `r4t roster check`, not by the roster parser —
parsing a roster never needs rig config.

## Budget resolution — member explicit size, then rig tier, then a floor

The effective inject budget resolves in this order:

1. An explicit size on the `Knowledge:` line (`small`/`medium`/`large`/`4k`/`4096`) always wins.
2. Otherwise, the member's own rig's harness-class tier: lower for
   opencode/ollama-class local models, mid for agy/cursor, upper for
   codex/claude. The tier lives with the preset (`rig.py`), not the roster —
   a rig swap changes a member's default budget without touching `Knowledge:`.
3. A rig with no preset (a custom CLI) gets the global floor, same value as
   `small` (4096 bytes).

`r4t roster check` warns — never refuses — when a member's Knowledge-carrying
rig sits in the lower tier: local/opencode-class models measured (K2 campaign)
smoothing over specifics rather than keeping them at a given byte budget.
Models improve, so this is a courtesy nudge toward a bigger rig or a
[distill-rig override](#distill-rig--a-different-writer-for-the-same-member)
(`agy`-class rigs matched the fidelity of a much slower model at a tenth the
wall clock), never a gate.

## The store — one per member, nothing shared

The store lives host-side at `agents/<member>/k7e/` under the roster's state
dir (`R4T_HOME`), one store per member. That boundary is deliberate, per the
#41 research gate: separate principals get physically separate stores — tags
organize *within* a store, they do not enforce between members. The shared
tier of memory is the workplace repo itself, which every member already
reads. Seed or inspect a store directly with the k7e CLI:

```bash
K7E_HOME=~/.config/r4t/rosters/<node>/agents/wren/k7e k7e store "Deploy notes" --tags ops
```

### The separation is prompt-path only until the OS enforces it

One store per member bounds what a member is *given*. It does not bound what a
member can *take*. The stores are files under `R4T_HOME`, r4t exports
`R4T_HOME` into the turn environment, and a tool-capable rig has a shell.

**On a bare org — no `run_as`, no container — there is no boundary between a
tool-capable member and any member's store, its own or a sibling's.** This is
observed, not theoretical: in the K1 rig matrix a full-tool preset stated a
codeword that existed only in a seeded store, with no Knowledge line and
nothing in the prompt, in half its trials. Other presets on the same condition
could have looked and did not — which is the point. It is a choice the member
makes, so nothing on the inject path can prevent it.

Under `run_as` or container isolation the stores are another OS user's files
and the cage holds. The isolation result that matters is an OS boundary; the
prompt path was never the whole surface. Consistent with a8s doctrine — the
filesystem is the identity boundary — but worth stating where someone is
deciding whether an org needs isolation, because "each member has its own
store" reads like a guarantee and on a bare org it is a convention.

## Inject on the way in

Waking a knowledge-carrying member, dispatch searches its store — seeded with
the newest message, the member's name and role, and the mission's first line
— and pastes the top entries into a `## Knowledge` prompt section: ranked
snippets with provenance (`id`, relative age), framed as fallible background,
never as instructions. The section rides after the how-to-work doctrine and before
`Reinforce:`, so the closing line keeps last-read primacy. The budget bounds
the section in bytes; packing (below) is deterministic. Echo members never get
the section, and any k7e failure logs `KNOWLEDGE-SKIP` and costs only the
section — never the turn. Sizing reads the whole weighting pool in one
`k7e get --no-track --json` call, which does not count as a use — the packer
needs every entry's size before it can weigh any of them, and most never make
the prompt. One call rather than one per entry keeps the pass off the wake's
latency budget; the per-entry read is trivial next to interpreter startup.
Injection is what
counts: after packing, one `k7e touch` call bumps the usage counter for
exactly the entries that survived into the section, so `k7e stats` shows what
recall actually earns its keep.

The section is a first-class prompt section, so the `r4t: PROMPT` day-log
line prices it per wake next to mission, history, and messages
([operations](r4t-operations.md)). A second line, `r4t: KNOWLEDGE`, prices the
retrieval itself — entries, bytes, total milliseconds, and what the search and
the query embedding cost inside that:

```
r4t: KNOWLEDGE wren 3 entries 1840B in 512ms (search 190ms, embed 41ms)
r4t: KNOWLEDGE wren 3 entries 1840B in 470ms (search 150ms, fts-only)
```

## Packing — rank-proportional allocation

The budget splits across the search hits by a `1/(rank+1)` weight — rank 1
gets the biggest share, rank 2 half that, and so on down to the search
pool — and any slack an entry's share leaves unspent (its snippet is
shorter than its allowance, or a low-ranked entry didn't earn enough to
qualify) sweeps back down the ranks so nothing goes unused. A budget too
small for the top hit's whole snippet still surfaces evidence from ranks 2
and 3, instead of spending everything on rank 1 and never reaching the rest.

Each entry's **preamble** — the `### title (id, age)` header plus the
staleness status line when present — is atomic: it never truncates, because
half a provenance stamp is worse than none. Only the snippet backs off, on a
line or sentence boundary where one lands cleanly. An entry whose share can't
cover its preamble plus a minimum snippet is **skipped outright** rather than
emitted as a content-free stub.

This is `s4-rank-proportional` from the `k-budget-packing` experiment
(#12/#52): measured against the old whole-entry-in-rank-order packer on 48
LongMemEval questions, it raised full-evidence coverage at the `small` budget
from 14/48 to 26/48 — the reason `small` moved from 2048 to 4096 bytes — and
to 38/48 at `medium` (95% of the retrieval ceiling), with no regression on
any question type.

## Provenance stamp — relative age, and a staleness status line

Each entry's block header stamps `(id, age)`: `today` under 24 hours old,
otherwise `<N>d old` — `(K7E-000-00003, 36d old)`. An entry with no
parseable date (k7e's `last_updated` frontmatter missing or unreadable)
keeps the bare-id form, `(K7E-000-00003)`.

An entry older than 30 days additionally gets one line appended to its
block, before the body:

```
Status: possibly superseded -- do not treat as current unless corroborated.
```

Both the relative-age form and the status line's threshold and wording are
the measured production change from the `k-age-presentation` experiment
(#62): the absolute-date stamp this section carried scored worse than no
date at all on the small-model floor, and relative age plus this status
line was the only presentation that worked on both reader classes tested —
see `apps/r4t/experiments/k-age-presentation/`. The status line's bytes
count against the section's inject budget like every other block byte.

The stamped id is a second, weaker recency signal whether or not anyone
intends it — k7e allocates ids in sequence, and models compare the ordinals
unprompted. It agrees with the age stamp until a store is imported, merged, or
rebuilt, at which point the ordinals say one thing and the dates another.
That is why the age stamp has to be right rather than merely present; see
[k7e-architecture.md](k7e-architecture.md#ids-leak-write-order).

## Framing — the cautionary line, as a knob

Every `## Knowledge` section carries a header and, under it, one line framing
the entries as fallible background rather than instructions. That line is
configurable per member and per rig (#62):

```markdown
- **Framing:** off                          # drop the line; header and entries stay
- **Framing:** default                      # the built-in wording (also the default when absent)
- **Framing:** "background notes, verify"   # custom wording, taken verbatim
```

Quotes are mandatory for custom text on a roster line — without them there is
no way to tell the keyword `off` apart from an operator's sentence that
happens to start with the word off.

A rig may set its own default in rigs.json, the same three forms, unquoted
(a JSON string is already delimited, so there is no keyword collision to
guard against):

```json
{
  "leader": {
    "invoke": ["claude", "{prompt}"],
    "framing": "off"
  }
}
```

Resolution: a member's explicit `Framing:` line always wins; absent that, the
member's own turn rig's `framing` default applies; absent both, the built-in
line renders — the section is byte-identical to a roster and rig config that
never heard of the field. `off` removes only the framing line: the header,
provenance stamps, and entry blocks render exactly as they do today.

## The semantic track — the query at wake, the backlog at night

A member's store searches BM25 **and** embeddings when ollama answers, which
closes the lexical gap: a note distilled in one vocabulary still surfaces for a
question phrased in another. Nothing to configure — `ollama pull
nomic-embed-text` and member stores use it.

The wake path embeds **the query and nothing else**, on k7e's two-second
`embed_query_timeout`, and the whole search runs under a 15-second cap. An
ollama that is down or slow costs that budget once and the search returns FTS5
results — the inject degrades, the turn does not wait. The day log says which
happened (`embed 41ms` vs `embed 2011ms unanswered, fts-only`).

Entry vectors are never computed at wake. Storing an entry queues it; dreaming
drains the queue with `k7e embed-pending` on the same idle pass that distills,
and logs the cost per entry:

```
r4t: DREAM-EMBED wren embedded 5 entries in 0.2s (35ms each)
r4t: DREAM-EMBED-SKIP wren 5 entries still queued — embeddings unavailable; …
```

A queue that outlives a pass is not a loss: the store searches FTS-only until
ollama answers, and the next pass embeds the backlog.

## Distill on the way out — dreaming, not per-turn

This is the **dream** sweep on an [idle pass](r4t-idle.md). Turn captures are
already the cheap per-turn log. The extraction pass runs async from
`run_idle` — bounded per pass, never inside a turn — feeding fresh captures
to `k7e distill` and advancing a `.dreamed` watermark only on success. Failed
turns are never distilled — their batch returns to the queue, and facts
extracted from them would be premature.

### Distill rig — a different writer for the same member

Dreaming defaults to the member's own turn rig — the least surprising choice,
and measured (K2 campaign) as not broken: 88% of facts survived a write pass
by a 1.7B local model. A `Knowledge:` line naming a rig (`Knowledge: claude`,
`Knowledge: large agy`) overrides which rig writes that member's notes,
independent of which rig runs its turns. r4t resolves the override (or the
member's own rig, pins included) and bridges it to k7e as
`K7E_DISTILL_COMMAND` for that one distill pass — the rig's own invoke under
an `sh -c` wrapper that substitutes `"$(cat)"` where the prompt argument
goes, because k7e pipes the prompt to stdin and not every harness reads
stdin as its prompt. A store whose
resolved rig has nothing to run, or whose distill-rig name matches no
configured rig, just waits (`DREAM-SKIP` in the day log); `r4t roster check`
catches an unresolvable override before it ever reaches a dream pass.

An agy-class advanced rig matched a much slower model's fidelity at a tenth
of the wall clock in K2 — the recommended override for a member whose own
rig is small enough to trip the [floor warning](#budget-resolution--member-explicit-size-then-rig-tier-then-a-floor)
above.
