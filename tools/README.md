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
