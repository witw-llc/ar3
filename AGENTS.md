# AGENTS.md

Onboarding for any AI agent working in this repository. `CLAUDE.md`
carries the same ground rules plus Claude-specific notes; when the two
differ, `CLAUDE.md` wins for Claude sessions.

## What this repo is

**The Ark** — a suite for governing a roster of AI CLI agents:

| App | One line | Start at |
|---|---|---|
| `apps/a8s/` | Filesystem message router — agents talk via `tell` | [`docs/a8s.md`](docs/a8s.md) |
| `apps/r4t/` | Roster governance — budgets, queues, dispatch, isolation | [`docs/r4t.md`](docs/r4t.md) |
| `apps/k7e/` | Knowledge engine — markdown + SQLite FTS5 | [`docs/k7e.md`](docs/k7e.md) |
| `apps/ar3/` | Front door — reads suite state, mutates nothing | [`docs/ar3.md`](docs/ar3.md) |

Every doc lives flat under `docs/` — the apps carry code, not prose.
`install.sh --skills` symlinks exactly the `docs/*.md` pages that open with
YAML frontmatter; frontmatter is the skill gate, so a deep app page must
never grow one.

`guide/` is *The Ark Raising*, the user-facing build-along. It is a
shipped artifact under its own license (CC BY-NC-ND 4.0 — see
`guide/LICENSE.md`); code is Apache-2.0.

## Hard rules

- **Issues + feature branches off `main`; no direct commits to `main`.**
  Every merge to `main` bumps `VERSION` (patch minimum) — CI enforces it.
- **a8s is pre-v1: no migration code, no back-compat shims.** Schema
  changes are scorch-the-earth; the owner wipes state and re-derives.
- **The `apps/` sibling layout and root shims are load-bearing** (r4t
  resolves `apps/a8s/a8s.py` relative to itself; containers mount the
  repo root). Do not reshuffle.
- **PII guard:** CI scans every PR diff. Never commit personal names,
  device names, or private hostnames; `tests/test_pii.py` and
  `.github/pii_check.py` are the gate.
- **Docs speak present truth** — no "previously"/"used to" framing
  (git holds history), and the words "honest"/"honestly" are banned.
- **SKILL.md frontmatter uses quoted scalars** for `name:` and
  `description:` — some harness YAML parsers hard-fail otherwise.
- **Code style:** no comments except non-obvious *why*; validate at
  boundaries only; three similar lines are fine, abstract on the
  fourth; no speculative abstractions.

## Conventions

- Bash shebangs are `#!/usr/bin/env bash`, never `#!/bin/bash`.
- Cross-platform entry points are bash+PowerShell polyglots with
  `.ps1`/`.cmd` siblings; read an existing one (`a8s`) before writing
  one.
- Commits follow Conventional Commits with an app scope:
  `feat(a8s):` / `fix(r4t):` / `docs(k7e):` / `feat(ar3):`. Bodies
  explain the why. AI-assisted work carries a co-author trailer.

## Running the tests

```bash
python3 -m pytest apps/a8s/tests/    # ~850
python3 -m pytest apps/r4t/tests/    # ~860 (run separately from a8s —
                                     #  the two ulid modules shadow)
python3 -m pytest apps/ar3/tests/
cd apps/k7e && tests/run             # ~126, builds its own venv
```

CI is budget-conscious: suite jobs are path-filtered, so touching one
app runs one suite. Keep it that way.

## The resident agent

The repo's assistant seat on the a8s network is named **Ares**. Agents
messaging this repo's assistant address it by that name.
