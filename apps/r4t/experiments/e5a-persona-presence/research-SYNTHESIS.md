# E5a research-gate synthesis — 2026-07-16 (Fable)

Source: e5a-research-result.md (o4-mini-deep-research; persona-anchored
LLM judging). Gate verdict: **PASS with design updates** — the experiment
is worth codifying; the field explicitly lacks and recommends it.

## The headline

"We are not aware of published evidence comparing a named persona to an
equivalent generic instruction *holding all else equal*" — and the report
closes by recommending "a carefully controlled A/B test (persona vs
neutral rubric, measuring consistency on paraphrases)." E5a is novel and
named-valuable by the literature. Our ledger will be comparison data the
field doesn't have.

## Wild sightings (problem confirmed in practice)

- Shipped persona judges exist: Reviseo (selectable reviewer personas),
  Chorus (multi-persona panel w/ aggregation), structured-debate review
  bots. Practitioners bet on personas without the controlled evidence.
- Persona sweep benchmark (TechLoom): code-reviewer persona = +1.0 quality
  and LOWER variance; mismatched "patient mentor" persona = worst
  performer. Principled Personas (EMNLP 2025): expert personas help or
  neutral; irrelevant persona details can cost up to ~30pp.

## Design updates adopted for the E5a package

1. **Domain-matched persona is mandatory** for arm B (mismatch is a known
   harm, not a neutral choice). Persona named at protocol-authoring time;
   the report's Grace Hopper template is the borrowable shape.
2. **Sharpened falsifier** from the literature's own null hypothesis:
   persona effects may be attention/style alignment only — if B's
   paraphrase consistency ≤ A's, the packed-token theory is dead for
   judging; style-only is the default explanation.
3. **Judge model family ≠ fixture-author family** (self-preference bias):
   ttt fixtures are qwen-authored → judge on Gemini Flash pin (or
   vice versa). Recorded in the manifest.
4. **Single-shot judging designed out the drift risk** (71% of personas
   drift by turn 80 — but our judge lives for one invocation). Stated in
   PROTOCOL.md so nobody adds multi-turn judging casually.
5. **Observation dimension added, not a metric:** watch whether the
   exacting persona under-credits honest confessions (sycophancy research:
   persona shifts compliance 1.7–19.3%). The d5n ground truth already
   contains honest-confession cases to check against.

## Ladder amendments (ride the next EXPERIMENT-LADDER.md touch)

- **New sub-rung E5a′ (candidate):** famous name vs anonymous EQUIVALENT
  role ("You are Grace Hopper..." vs "You are a meticulous veteran
  engineer...") — the cleanest isolation of the packed-token claim
  itself, distinct from persona-presence. E5a must run first.
- **E5c arms corrected by evidence:** yes/no flags vs **1–4 integer
  scale** (not 1–10): narrow integer scales doubled human alignment vs
  5-point Likert in the field's data; 1–10 is already known-bad, so
  testing against it would be a strawman.
- **E5d reinforced:** PairS (pairwise) reports SOTA alignment vs direct
  scoring — pairwise arm stays the strongest challenger to Neil's
  flags-doctrine; structured-debate/meta-judge shapes noted as a later
  rung, not v1.

## Rejected / held

- Panel-of-personas ensembles (Chorus-style) and structured debate:
  interesting, expensive, out of E5 scope — a later rung if E5a/E5b
  results justify ensembles. Neil's prior: multiple personas =
  inconsistent; the panel question is E5b's.
- No changes to E0 (noise floor) — hand-authored fixtures avoid the
  self-preference concern entirely.
