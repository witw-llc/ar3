# Knowledge — a member's private k7e memory

Experimental (#41): default off, and the defaults below are lab knobs until
the K0 experiment freezes them.

A member with a `Knowledge:` roster line gets a private
[k7e](k7e.md) store and two automatic behaviors around it:

```markdown
### Wren
- **Rig:** claude
- **Knowledge:** on          # or: off | 4k | 4096  (inject budget)
```

`on` takes the default inject budget (2 KiB); a size sets it exactly. Absent
or `off`, every prompt is byte-identical to a roster that has never heard of
the field.

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

## Inject on the way in

Waking a knowledge-carrying member, dispatch searches its store — seeded with
the newest message, the member's name and role, and the mission's first line
— and pastes the top entries into a `## Knowledge` prompt section: ranked
snippets with provenance (`id`, date), framed as fallible background, never
as instructions. The section rides after the how-to-work doctrine and before
`Reinforce:`, so the closing line keeps last-read primacy. The budget bounds
the section in bytes; truncation is deterministic. Echo members never get the
section, and any k7e failure logs `KNOWLEDGE-SKIP` and costs only the section
— never the turn. Reading an entry for a prompt bumps its k7e usage counter,
so `k7e stats` shows what recall actually earns its keep.

The section is a first-class prompt section, so the `r4t: PROMPT` day-log
line prices it per wake next to mission, history, and messages
([operations](r4t-operations.md)).

## Distill on the way out — dreaming, not per-turn

Turn captures are already the cheap per-turn log. The extraction pass runs
async from `run_idle` — bounded per pass, never inside a turn — feeding fresh
captures to `k7e distill` and advancing a `.dreamed` watermark only on
success. A store whose k7e has no `distill_command` configured just waits
(`DREAM-SKIP` in the day log); configure the LLM bridge per
[k7e-distillation](k7e-distillation.md) and the backlog dreams on the next
idle pass. Failed turns are never distilled — their batch returns to the
queue, and facts extracted from them would be premature.
