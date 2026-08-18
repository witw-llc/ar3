# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## Repo shape

This repo is **ar3** — four apps that ship together, plus the guide that
teaches them.

- **Top level** — polyglot CLI shims (`ar3`, `a8s`, `tell`, `tells`, `r4t`,
  `k7e`) plus their `.cmd`/`.ps1` siblings, `install.sh`, `VERSION`.
  `install.sh` adds the repo dir to `$PATH` and links `docs/` as skills.
- **`apps/a8s/`** — Agent Infinity System. Filesystem-based message router
  letting independent CLI agents (Claude, Gemini, Codex, scripts) talk to each
  other via `tell`. See [`docs/a8s.md`](docs/a8s.md) for concept and usage,
  [`docs/a8s-development.md`](docs/a8s-development.md) for hard constraints and
  historical decisions.
- **`apps/r4t/`** — The roster. Rigs, dispatch, verdicts, isolation.
  See [`docs/r4t.md`](docs/r4t.md) and the `docs/r4t-*.md` pages.
- **`apps/k7e/`** — Knowledge accumulation engine. Flat markdown files +
  SQLite FTS5 + optional ollama embeddings. Its core imports nothing beyond
  the standard library — the suite doctrine's dependency rule, which k7e
  happens to satisfy with no dependencies at all.
  See [`docs/k7e.md`](docs/k7e.md) for usage and architecture.
- **`ark/`** — The foundation package: the code every app shares. `ark.ulid`,
  `ark.home` (config-home resolution), `ark.fsio` (`atomic_write_text`),
  `ark.proc` (`spawn` / `terminate_group`), `ark.envseam` (the reserved-env
  contract), `ark.vendor` (the vendoring hook). Beyond stdlib there are two
  tiers and no third: `ark/_vendor/` carries pinned, sha256-verified PyPI
  releases (tier 1); the foundation's deps mechanism fetches the rest (tier 2).
  Apps import it via the same repo-root `sys.path` mechanics they already use
  for `arkver`.
- **`apps/ar3/`** — The front door. Reads suite state and probes prerequisites;
  it never mutates anything and never wraps another product's verbs. See
  [`docs/ar3.md`](docs/ar3.md).
- **`guide/`** — *The Ark Raising*, the chapter-by-chapter build-along.
- **`docs/`** — the whole suite's doc tree, flat: every page a reader or an
  agent needs lives here and nowhere else. `a8s.md` / `r4t.md` / `k7e.md` /
  `ar3.md` are the per-app entry points; `<app>-*.md` pages go deeper.
  **YAML frontmatter is the skill gate** — `install.sh --skills` symlinks
  exactly those `docs/*.md` whose first line is `---` into
  `~/.claude/skills/` and `~/.cursor/skills/`, and skips every other page.
  Skill docs load into an agent's context, so keep them short; a deep app
  page must not grow frontmatter.
- **`requirements/`** — dependency groups (`a8s-test.txt`, `r4t.txt`). Per-app
  `tests/requirements.txt` files point here.
- **`tools/`** — repo-local checkers, stdlib-only, run on demand at zero Actions
  cost. They run in `release.yml` and never in the per-PR workflow. See
  [`tools/README.md`](tools/README.md).

The sibling layout under `apps/` is load-bearing: r4t resolves a8s at
`apps/a8s/a8s.py` relative to its own file, and `apps/a8s/definitions/r4t.json`
expands `$A8S_DIR/../r4t/r4t.py`. The shims live at the repo root because
`apps/r4t/isolate.py` mounts the repo root into containers and puts it on
`PATH` so `tell` resolves inside a turn. Do not reshuffle either.

## Versioning — every merge bumps `VERSION`

The repo carries a single suite semver in `VERSION`, and **every merge to `main`
increments it**. CI fails any PR whose `VERSION` equals `main`'s: patch bump
minimum, minor or major at the author's judgment per semver semantics. Merge and
version bump are the same event, so `main`'s history doubles as the release
ledger. Bump `VERSION` in the same PR as the change it describes.

**A merge to `main` publishes.** The push runs `release.yml` — full suites on
ubuntu and macOS, the Docker isolation test, a PII scan — and on success pushes
a squash snapshot and a `v<version>` tag to the public mirror `witw-llc/ar3`.
There is no second switch: the owner's merge is the release. Batch branches cost
nothing, so iterate there.

Add user-visible changes to `CHANGELOG.md` under `Unreleased` in the same PR,
and rename that heading to the version when the batch is ready to merge.

Pre-1.0, the usual semver freedoms apply — 0.x minor bumps may break.

## Conventions

Every app in ar3 shares one doctrine — dependencies, filesystem, CLI feel,
processes, integration, docs and release. It is stated as rules in
[`docs/ark.md`](docs/ark.md); read it before adding a convention here.

### Shebangs

All bash scripts use `#!/usr/bin/env bash` (not `#!/bin/bash`). macOS ships
bash 3.2.57; users with Homebrew bash get a modern version this way. Don't
introduce `#!/bin/bash`.

### Polyglot bash + PowerShell scripts

Cross-platform CLIs (`a8s`, `tell`) are polyglots — the same file is valid
bash AND PowerShell. The bash side delegates to Python; the PowerShell side
finds `python3`/`python`/`py` via `Get-Command`. The pattern uses
`echo \`# <#` >/dev/null` as a no-op for bash that opens a PowerShell
multi-line comment. `tell` is a thin shim around `a8s tell`; don't add new
polyglots without reading an existing one first.

Windows can't run the extensionless polyglot from `PATH`, so the important
top-level commands also ship a sibling `.ps1` (PowerShell prefers it over the
extensionless file) and a `.cmd` for `cmd.exe`. Both are thin: resolve the
repo dir, find python, exec the entry-point `.py`, propagate the exit code.

### Install hook

`install.sh` is sourced from a shell rc. It adds the repo dir to `$PATH`. Pass
`--skills` to also symlink the frontmatter-bearing `docs/*.md` into
`~/.claude/skills/` (when Claude Code is present) and `~/.cursor/skills/` for
Cursor. That mechanism installs the user's own tool docs; a8s installs nothing
into a project.

Adding a new top-level CLI: write the shim, write `docs/<name>.md` with YAML
frontmatter if it should be installable as a Claude skill.

### Workflow

**Batch onto a version branch; the owner merges.** Work accumulates on one
branch named for the target semver (`0.1.55`), and the PR from it stays open
until the owner flips the switch. No direct commits to `main`, and nobody else
merges to `main` — a stream of individual merges is a stream of things the owner
has to track, and the whole point of the suite is to spend less of his attention,
not more. His merge also spends the Actions budget and ships to the public
mirror, which is why the gate is his alone.

Inside a batch, work however suits the change: commit straight to the version
branch, or open sub-PRs targeting it. Bump `VERSION` to the branch's number once,
not per change. After the owner merges, start the next batch from fresh `main` —
squash hashes don't match the branch's commits, so stacking causes conflicts.

### Pre-v1 / scorch-the-earth

The suite is explicitly pre-v1. **Do not write migration code.** When the schema
changes, the user wipes `~/.config/a8s/` and re-derives state via `a8s discover`
+ `a8s add`. This applies to registry shape, mailbox layout, definition schema,
and on-disk pid/log paths. The contract changes only when the user declares 1.0.

### Commit style

- Commits prefixed `feat(a8s)` / `fix(r4t)` / `refactor(k7e)` / `test(ar3)` /
  `docs(guides)` per Conventional Commits.
- Body explains the *why* and the design decision, not just the mechanical
  *what*.
- Co-author trailer for AI-assisted work:
  `Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>`
- **PR titles are synopses, not labels.** A batch PR is still titled by what it
  contains — `0.1.55 — merge to main is the release, changelog, plan states,
  and the Ark's own roster`, never `0.1.55 — batch`. The title is the only part
  most readers see, and after the squash it is the commit subject on `main`.

### Issue references — forward-looking only, and `#N` is this repo

**A code comment carries a number only when the number names work still to be
done.** "This preset cannot pin a session; that is `#17`" earns its reference —
a reader who hits the limit needs somewhere to go. "Load configured remotes
(bin#63)" does not: it records which change added the line, and `git blame`
already answers that better than a comment can. History belongs to git, not to
arbitrary comments. The same holds for PR numbers and line numbers.

When a number does belong: **a bare `#N` always means this repo. A reference to
the pre-carve repo `neilobremski/bin` is written `bin#N`.** The two namespaces
overlap — `#90` is a real issue in both — so a bare legacy number reads as this
repo's and GitHub links it here, silently pointing at unrelated work. Write the
prefix when quoting a legacy number, including inside a markdown link label.

Docs and `apps/r4t/experiments/` are outside this rule. A doc page cites issues
as references a reader follows, and an experiment record is a dated account by
design.

### Code style

- Default to no comments. Names should explain what; comments are only for
  *why* something non-obvious is done.
- Avoid emojis in source unless asked.
- Don't add abstractions that aren't being used today. Three similar lines is
  fine; abstract on the fourth.
- Don't add error handling for cases that can't happen. Trust internal
  guarantees; validate at boundaries (CLI input, external APIs, filesystem).
- Don't add backwards-compat hacks. See pre-v1 above.

### SKILL.md YAML — quoted scalars only

Harness YAML parsers differ: copilot rejects unquoted descriptions
containing colons outright (skill dropped), and other parsers have their
own strictness. Always quote `name:` and `description:` in skill
frontmatter.

### Docs voice

Docs speak present truth. No "used to" / "previously" framing — git holds the
history. Never the words honest/honestly. User-facing surfaces (CLI help, skill
descriptions) get one short sentence with no internals; mechanics go in the
app's `docs/` page or the docstring.

### Where a page goes

Four homes, split by what the reader is doing — the Diátaxis quadrants, named
here so new pages land deliberately instead of by feel:

- **`guide/`** — *tutorial*. A lesson for someone who does not yet know the
  thing. Hand-held, ordered, guaranteed to work if followed.
- **`docs/<app>.md`** — *how-to*. A recipe for a goal the reader already has.
- **`docs/<app>-*.md`** — *reference*. Facts consulted mid-task and never read
  start to finish. Precise, scannable, no narrative.
- **the private wiki** — *explanation*. Why it is built this way: rulings,
  research, the design journey. Rationale in a `docs/` page belongs here.

A page that is trying to be two of these is the usual reason it reads badly.

## Top-level scripts: `tell`

`tell` is a **thin shim** to `a8s tell` (plus `tell.cmd` on Windows).
Implementation lives in `apps/a8s/tell.py`. Outbox resolution: `TELL_OUTBOX_DIR`
when set (a8s injects it on wake); otherwise a unique configured outbox matched
from CWD when `~/.config/a8s` is readable (desktop filedrop seats — see
`docs/a8s-filedrop.md`). When the registry is reachable and CWD is inside
a registered agent, `from` stamping and agent logging apply on top. Recipient
validation follows the *outbox* instead: it runs only when the resolved outbox
is a registered agent's own outbox. Writing anywhere else makes `tell` a
staging writer whose consumer owns routing — r4t points a caged roster member's
`TELL_OUTBOX_DIR` at a per-turn staging dir, and roster members are not a8s
agents.

The router (`mailbox.py:_process_pending`) force-overwrites `from` based on
which agent owns the enclosing root — the filesystem is the unforgeable
identity.

a8s plants no skill files in an agent's repo: `tell` reads `TELL_OUTBOX_DIR`
from the environment a8s injects on wake. Top-level doc skills for the user's
own harness come from `source <repo>/install.sh --skills`.

## Common operations

```bash
# Every suite runs through its own `tests/run`, which builds and reuses a venv
# at apps/<app>/tests/.venv from that suite's requirements.txt. Never install
# pytest into the system or Homebrew python — extra args pass through to pytest.
apps/a8s/tests/run          # ~1440
apps/r4t/tests/run          # ~1450 (run separately from a8s — ulid modules shadow)
apps/ar3/tests/run          # ~175
cd apps/k7e && tests/run    # ~190; add -m "not llm" to skip model-backed tests

# Rebuild a suite's venv after its requirements change
rm -rf apps/a8s/tests/.venv

# Suite status and prerequisite probes
ar3
ar3 doctor

# Start fresh after a schema change (pre-v1 scorch-the-earth)
rm -rf ~/.config/a8s/agents/ ~/.config/a8s/a8s.json
a8s discover apps/a8s/tests/agents

# Tail per-agent activity
a8s logs CLAUDE GEMINI -f

# Clear local inbox without invoking
a8s drain my-agent

# Flush MQTT-queued messages (connect, trash for N seconds, exit)
a8s run my-agent --drain 5
```

## The resident agent: Ares

Claude Code sessions in this repo operate the a8s seat named **Ares**
(ar3 + s). Messages on the a8s network from this repo's assistant carry
that name, and mail addressed to `Ares` reaches it. Session start for
a8s work: read [the filedrop playbook](https://github.com/witw-llc/ar3-private/wiki/Playbook-a8s-Filedrop-Agent)
(the seat's operating manual) and arm a persistent inbox monitor so
incoming tells get answered without the owner having to relay them.
Operate the seat only — never other seats or router infrastructure.

## Memory note

The resident agent keeps a private memory outside this repo (personal
preferences, ongoing project state, feedback rules). THIS file is the
checked-in onboarding doc; keep it free of anything that would not
belong in a released repo.
