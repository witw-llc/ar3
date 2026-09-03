# tools

Repo-local checkers. Each one runs on demand, from a checkout, with no
GitHub Actions minutes attached — that is the whole point. Actions minutes are
metered on this repo and a release must never wait on a budget, so a checker
runs "for real" in `release.yml` and never in the per-PR workflow.

Every tool here is stdlib-only Python and reads nothing but the paths it is
given.

## `wiki-gardener.py`

Checks a checkout of the private wiki against the gardening charter
(`Charter-Wiki` on the wiki, from #133).

```bash
git clone git@github.com:witw-llc/ar3-private.wiki.git /tmp/wiki
tools/wiki-gardener.py /tmp/wiki           # one line per defect, then a count
tools/wiki-gardener.py /tmp/wiki --json    # the same report, machine-readable
```

Exit 0 means clean, 1 means defects. Six classes:

| Defect | What it means |
|---|---|
| `uncategorized` | no category from the charter's eleven declared in the page header |
| `stateless` | no state from the charter's five declared in the page header |
| `unexplained-flag` | a banner without a reason, without a date, or without either |
| `orphan` | unreachable from `_Sidebar.md` within two link hops |
| `sidebar-leaf` | a `_Sidebar.md` entry that is not an index page |
| `dead-link` | a `[[wiki]]` or relative link to a page that does not exist |

The header forms it reads, both on the page's first line:

```markdown
`Category: Playbook` · `State: Validated`
> **Out of date** — the layout it describes shipped in 0.1.40 — 2026-08-06
```

A category or state declaration is read anywhere above the first `##` heading,
in backticks, bold, or bare. A banner is one of the six flag names — Out of
date, Accuracy, Expansion, Merge, Move, Archive — in backticks or bold in that
same region; the rest of the line, or of the blockquote, carries its reason and
its ISO date.

An index page is `Home`, a category's own listing page (`Engines`,
`Owner-Memos`, `Stories` — derived from the eleven category names), or one of
the two cross-cutting indexes the charter names, `Attention` and `Charters`. A
new index of any other name goes in `CROSS_CUTTING_INDEXES` at the top of the
tool.

The checks are deterministic and read no meaning: whether a reason is a good
reason, and whether a category is the right one, stay with the gardener.
Renames are not checked at all — the tool's docstring says why.

`release.yml` runs the tool over the wiki on every release, non-blocking until
the wiki reports clean.

## `claim-sweep.py`

Fails if `README.md`, `docs/*.md` or anything under `guide/` states a
configured number as a promise.

```bash
tools/claim-sweep.py                 # path:line: <rule> — <the matched text>
```

The standard comes from the 2026-08-30 cadence correction: a published cadence
reads "every few minutes", never the interval a config file happens to hold
today. Three rules carry it. `cadence` catches a bare interval or latency
figure — `every 30 seconds`, `within 5 seconds`, `in under 3`, `40ms latency` —
and clears it the moment its sentence says where the number came from:
`measured`, `observed`, a date, an `about`/`roughly`/`~` hedge, or a link to
the run. `absolute` catches a reliability absolute asserted rather than denied
— `never fails`, `guaranteed`, `zero downtime`, `instantly`, and `always`
paired with a word like *works* or *available*, because this repo's docs use
`always` for deterministic contracts a hundred times over. `banned` catches
the one-pager's forbidden register — `orchestrate a team`, `framework`,
`no-code`, `dashboard` — on `README.md` and `guide/README.md` only, since a
docs page may call someone else's framework a framework.

Prose only: fenced blocks and inline code are blanked first, so a flag named
`--always-approve` and an interval inside a config sample are the thing itself
rather than a claim about it. `tools/claim-sweep.allow` holds the lines a human
has already judged — one regex per line, matched against `<path>: <line>`, each
with the reason written in the comment directly above it. Consuming a pattern
spends its reason, so two patterns stacked back to back each need their own
comment immediately above — a reason never carries over to the next entry, and
a blank line also ends one. An entry without a reason is a suppressed claim,
which is what the tool exists to prevent, so a pattern with nothing of its own
above it suppresses nothing and fails the run instead. The reason sits above
the pattern rather than after a `#` on the same line, the way
`surface-audit.allow` writes one, because `#` is a character a regex may need.

`release.yml` runs it on every release, blocking: the tree reports clean as of
2026-09-03.

## `surface-audit.py`

Counts every surface a user meets — CLI verb, config key, bundled runbook —
as `wired`, `deferred`, or `unaccounted`. The 1.0 bar says there is no third
category; this is what turns that from a judgement into a number.

```bash
tools/surface-audit.py           # a table per surface, then the totals
tools/surface-audit.py --json    # the same report, machine-readable
```

Exit 0 means nothing is unaccounted for. Exit 1 lists what is.

| Verdict | What it means |
|---|---|
| `wired` | a test names the item as a string literal **and** a `docs/` page names it |
| `deferred` | the surface where the user meets it carries a deferral marker |
| `unaccounted` | neither |

Both halves are read one span at a time. A nested command counts as tested when
one argv or one call names every word of it — `["rig", "run"]` — and as
documented when one code span or one fenced line shows the whole command.
`rig` in one test and `run` in another are two tests that both keep passing
after `rig run` is deleted, so they are not evidence that it works.

The three surfaces it enumerates, by static parse — nothing is imported or
run:

- **cli** — argparse subcommands (nesting resolved, so `rig list` is not a
  second `list`), `aliases=`, a positional's `choices=`, and the `COMMANDS` /
  `ALIASES` tables a8s and k7e carry their verbs in.
- **config** — every module-level table whose name ends in `KEYS`, `FIELDS`,
  `SECTIONS`, `SETTINGS`, `OPTIONS` or `KNOBS`. A table opts in by being named
  that way, which is how a key table written next year gets audited without
  anyone editing this tool.
- **runbook** — the bundled `apps/a8s/definitions/*.json` and
  `apps/r4t/runbooks/*.md`.

### The deferral marker

"Marked deferred in the surface where the user meets it" only means something
if a machine can see it, so:

- **In code** — an argparse `help=`, or the comment on a key's declaration
  line — the words `deferred` or `not yet`, or a bare `#NNN`. The repo already
  rules that a number in source names work still to be done, so a verb whose
  help text cites an issue is a verb whose author said it is not finished.
  A key table named `DEFERRED_*` marks every key in it; a table named `GONE_*`
  declares rejections and is not a surface at all.
- **In `docs/`** — only `deferred` or `not yet`, on a line naming the item.
  Docs cite issues as references a reader follows, so `#NNN` in a doc page
  says nothing about deferral.

A test's **docstring is not evidence of wiring**. `"""`a8s ps` lists running
nodes"""` would still read true after `ps` stopped working; an argv element or
an assertion naming the command would not.

### `surface-audit.allow`

The seed is the 1.0 to-do list. Every line carries the reason it is allowed,
and a line without one fails the check the same way an unaccounted item does.
The tool also names a line it no longer needs, so the file shrinks as the
suite is wired. Deleting the last line is the win condition.

`release.yml` runs this on every release. `tests/test_surface_audit.py` drives
the classifier over a fixture suite, and the tool, its allowlist and that test
are all routed into the per-PR `a8s` job, so a change to any of the three runs
it.
