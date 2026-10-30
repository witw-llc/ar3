# tools

Repo-local checkers run on demand from a checkout. Explicit full verification
in `test.yml` runs the suites and policy gates before merge; `release.yml`
checks the recorded evidence and current publication policy without repeating
the suites. Pull-request events check current-tip evidence and privacy.

The checkers and verification helper use stdlib-only Python; the shard adapter
loads inside pytest. The verification helper uses the GitHub API, and the other
checkers inspect their input paths.

## `verification.py`

Request full verification after review, including while the PR is draft:

```bash
gh workflow run test.yml --ref VERSION_BRANCH -f pr=PR_NUMBER
```

Select the open same-repository PR's head branch. The workflow captures its
immutable SHA, validates the selection, and runs all six suite roots separately
on Ubuntu 24.04 with Python 3.12. Suite failures do not cancel other lanes.
The k7e scope excludes model-backed tests with `-m "not llm"`.
Docker isolation, version/changelog, wiki, source audits, and both
diff and whole-tree privacy checks are mandatory. Each run retains suite reports
and a JSON record of SHA, tree, version, run/attempt and job conclusions.

The `full-suite` commit status belongs to the exact tested SHA.
A new tip needs new verification. An initializer reports absent or failed
evidence without running suites. Status writers query the current run attempt
and every mandatory job; artifacts alone never authorize success. A full rerun
is required: a partial rerun without the complete attempt's job inventory is
not certification. A failed newer attempt revokes the older result. The status
can turn green just before its writer job exits; release additionally requires
the entire run to have completed successfully.

The `release.yml` lifecycle listener reconciles requested, running and completed
manual verification runs, including cancellation that prevents either writer
from executing. It shares the writers' per-SHA serialization and cannot publish.
GitHub activates this listener only from the default branch: the first PR that
introduces it uses direct certification writers; cancellation reconciliation
becomes active once that workflow reaches main. Initializer checks and final
release validation still reject canceled evidence during that bootstrap.

`tools/verification.py` and the workflow are reviewed together as trusted
same-repository code. The isolated status jobs fetch only this fixed helper at
`GITHUB_WORKFLOW_SHA`; they perform no candidate checkout, execute no suites,
and receive no publication key. This is evidence of the reviewed candidate's
tests, not independent attestation of hostile workflow edits. Forks are refused.

The `pii` job checks the diff and whole tracked tree on every PR event and
manual certification. PR scans use the captured head and base SHAs; forks are
refused before checkout, and missing privacy patterns fail the check. A failed
privacy check also prevents full certification.

Configure the main ruleset to require `full-suite` and `pii` from GitHub Actions
and an up-to-date branch through an owner-approved repository settings change.
The workflow files do not install or establish that external enforcement.
Release finds the PR merged into its immutable main SHA, validates the
GitHub run and jobs for that PR head, and requires identical Git trees. This
supports squash merges while preventing publication of untested merged content.
Current wiki and privacy patterns remain release gates. Immutable source audits
run during certification. The owner alone merges.

## `wiki-gardener.py`

Checks a checkout of the private wiki against the gardening charter
(`Charter-Wiki` on the wiki, from #133).

```bash
git clone git@github.com:witw-llc/ar3-private.wiki.git /tmp/wiki
tools/wiki-gardener.py /tmp/wiki           # one line per defect, then a count
tools/wiki-gardener.py /tmp/wiki --json    # the same report, machine-readable
```

Exit 0 means clean, 1 means defects. Nine classes:

| Defect | What it means |
|---|---|
| `uncategorized` | no category from the charter's eleven declared in the page header |
| `stateless` | no state from the charter's five declared in the page header |
| `unexplained-flag` | a banner without a reason, without a date, or without either |
| `orphan` | unreachable from `_Sidebar.md` within two link hops |
| `sidebar-leaf` | a `_Sidebar.md` entry that is not an index page |
| `dead-link` | a `[[wiki]]` or relative link to a page that does not exist |
| `ledger-oversize` | the decision ledger's live table is over its row ceiling (120); at the ceiling the summary asks for a consolidation pass down to 90 |
| `unarchived-row` | a superseded ledger row still sitting in the live table |
| `unconsolidated-pair` | two live ledger rows on one surface in one domain |

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

The last three classes read the wiki's `Decisions` page, whose own preamble
carries the rule they check: a new ruling that covers a live row's ground merges
into that row rather than appending, the absorbed rows move verbatim to
`Decisions-Archive`, and the live table stays under 120 rows. Every run prints
the live row count against that ceiling, clean or not.

Which two rows cover the same ground is a judgement, so the tool compares only
the **surface name** — the row title's words before the first dash or colon, or
its first three words where fewer than half the titles carry such a separator.
The report says which of the two it used, because the answer changes as titles
are rewritten. Rows in different domain sections never pair, and a row whose
Status names a successor is reported as belonging in the archive instead.

The checks are deterministic and read no meaning: whether a reason is a good
reason, whether a category is the right one, and whether two rows on one surface
should actually merge, stay with the gardener.
Renames are not checked at all — the tool's docstring says why.

`test.yml` runs the tool during explicit certification and `release.yml` on
every release, both blocking. `tests/test_wiki_gardener.py` drives every check
over miniature wikis built in `tmp_path`, in the certification's repository suite.

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

The certification policy job in `test.yml` runs this as a blocking check.

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

The certification policy job runs this check. `tests/test_surface_audit.py`
drives the classifier over a fixture suite in the full repository test lane.

## `changelog-heading.py`

Fails if `CHANGELOG.md` has no `## <VERSION>` heading, or still has entries
under `## Unreleased`. 0.1.80, 0.1.81 and 0.1.82 each merged with their notes
still under `Unreleased`; the rename was a human step at merge time, and merge
time is exactly when nobody is looking at the changelog (#246).

```bash
tools/changelog-heading.py 0.1.96              # reads CHANGELOG.md
tools/changelog-heading.py 0.1.96 --changelog PATH
```

Exit 0 and silent when the changelog is ready to ship that version. Exit 1
with a one-line message naming the fix otherwise. Certification and release
both run this against `VERSION`.

## `changelog-closes.py`

Prints the issue numbers a `CHANGELOG.md` version section closes, parsed from
its `Closes #N` lines. A squash merge does not honour a `Closes #N` written in
a commit body, so the changelog entry is the only durable record of what a
release closes; `release.yml`'s `publish` job reads this after a release ships
and closes each issue it names.

```bash
tools/changelog-closes.py 0.1.96              # reads CHANGELOG.md
tools/changelog-closes.py 0.1.96 --changelog PATH
```

Prints one issue number per line, ascending, de-duplicated. Exit 0 always — a
release with nothing to close is not a failure.

## `version-bump.py`

Enforces the suite's version-bump shape: `major.minor.build`, where exactly
one digit moves from `main`'s `VERSION` to the PR's. The build increments on
every merge; the minor bumps with build reset to 0 when the owner closes a
phase; the major bumps at the 1.0 hand-off (ruled 2026-09-25, #272).

```bash
tools/version-bump.py 0.1.95 0.1.96      # main's version, then the PR's
tools/version-bump.py 0.1.95 0.2.0
tools/version-bump.py 0.1.95 1.0.0
```

Exit 0 and silent when the PR's version is one of the three allowed next
versions. Exit 1 with a one-line message naming all three otherwise. The
certification policy job compares against `main`; release compares against
the preceding main commit.

## Process launch policy

Run `python3 tools/process-launch-audit.py` to check every production Python subprocess in apps, foundation, repository tools and CI helpers. Each launch must call `ar3.proc.launch_options`; Windows flags belong only to the foundation. Intentional inherited-console launches declare `interactive=True`. Test fixtures, vendored code and archived experiments are excluded. `--self-test` checks alias detection and bypass rejection. The certification policy job runs both.


Certification partitions the complete a8s and r4t collections into two isolated
VM jobs each; the other four suites each retain one job. `shard_plugin.py`
collects exactly the whole application before assigning whole modules. It refuses
pytest filters and narrower roots. `shard_weights.json` contains approximate
module costs from timestamped progress blocks, not measured per-test durations;
unknown modules remain included with a deterministic default weight. Randomized
parameter values require stable explicit pytest case IDs; the sync-marker URL
cases retain their random inputs and assertions with descriptive IDs.

Each shard retains full, selected, and actually started node IDs, digests, exact
checkout SHA, run ID, and attempt. The record job validates the exact artifact
set, bounded JSON schemas, identical full inventories, whole-module assignment,
and exhaustive disjoint execution before recording complete coverage. GitHub's
native mandatory job/run results remain authoritative; artifact claims alone
cannot certify a commit. Collection-only receipts cannot certify execution.
All eight lanes retain JUnit and report the 30 slowest durations.

The owner's timing target is seven minutes with a ten-minute ceiling. These are
targets, not measured achievements. Record certification and publication elapsed
times separately; use the first full certification's job/JUnit timings to assess
the allocation before claiming the target is met.
