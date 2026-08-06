---
name: "ar3"
description: "Show whether The Ark is set up and working on this machine."
---

# ar3

The front door to The Ark: **a8s** (agent message router), **r4t**
(the roster), **k7e** (knowledge engine).

`ar3` orients and verifies. It reads state and probes prerequisites — it never
changes anything, and it never runs another product's commands for you. When
something is missing, `ar3` names the real command to fix it.

```
ar3            # where the suite stands right now
ar3 doctor     # are the harnesses and tools it runs on actually working?
ar3 --version  # the suite semver (every Ark CLI answers this)
```

## `ar3` — the greeter

```
A R K
8 4 7
S T E
```

Then one panel per product, each line marked ✓ or ✗ with a short fact, and
every ✗ followed by the command that fixes it:

```
a8s — agent message router  (~/.config/a8s)
  ✓ cli       a8s -> /path/to/a8s
  ✓ registry  3 agent(s), 1 alias(es), 0 namespace(s)
  ✗ router    no agent attached   (try: a8s start <agent>)

r4t — the roster  (~/.config/r4t)
  ✓ cli    r4t -> /path/to/r4t
  ✓ rigs   2 rig(s): leader, worker
  ✗ rosters  none under ~/.config/r4t/rosters   (try: r4t init)

k7e — knowledge engine  (~/.config/k7e)
  ✓ cli    k7e -> /path/to/k7e
  ✓ store  41 entr(ies) under ~/.config/k7e/nodes
  ✗ index  no search index   (try: k7e reindex)
```

A product that is not installed at all degrades to its ✗ rows and hints; `ar3`
still prints the rest.

State locations follow each product exactly, including the environment
overrides: `A8S_HOME`, `R4T_HOME` (and `XDG_CONFIG_HOME`), `K7E_HOME`.

## `ar3 doctor`

Functional probes for the CLIs the suite runs on — every one is read-only and
time-bounded, so `doctor` never hangs and never installs, starts, or
configures anything.

- **Suite** — the version this copy is running, and whether a newer one is
  published on the public mirror. This is the one probe that reaches the
  network, so it lives here and not in bare `ar3`; when GitHub cannot be
  reached it says so rather than claiming you are current.
- **Harnesses** — `claude`, `agent` (Cursor), `codex`, `copilot`, `opencode`,
  `agy`, `ollama`: on PATH, and does a version probe actually answer?
- **Services** — the ollama server (reachable? which models are pulled?) and
  docker (binary present *and* daemon reachable — they fail differently).
- **Tooling** — git present with `user.name` and `user.email` configured.

Exit code is 0 when the core prerequisites hold (git configured, and at least
one agent harness answering), 1 otherwise — so it can gate a setup script.

## What `ar3` is not

`ar3` has exactly one subcommand. It does not wrap, alias, or pass through the
products' own verbs: sending a message is still `tell`, running a roster is still
`r4t dispatch`, storing knowledge is still `k7e`. Those commands appear in
`ar3` output only as hints pointing you at the tool that owns them.
