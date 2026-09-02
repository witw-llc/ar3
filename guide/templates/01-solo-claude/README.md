Chapter 1's agent on the main path (Claude Code), backed by the bundled
`engine-claude` definition — one headless turn per wake, with
STATUS.md/LESSONS.md as its memory. Copy AGENTS.md into your agent
directory (e.g. `~/ar3/solo/`), then:

    # claude can be replaced with cursor, codex, or agy
    a8s add solo ~/ar3/solo engine-claude
    a8s start solo

No definition file to write: `engine-claude` ships with the suite. The
persona is the opening paragraph of AGENTS.md — the chapter's customize
step swaps that paragraph. STATUS.md and LESSONS.md appear on their own,
on the first turn.
Used by [guide/01-hello-agent.md](../../01-hello-agent.md).
