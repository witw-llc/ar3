# AR3

AR3 is a set of command-line tools that turns the AI coding agents already on
your machine into a team: you write the team down in one file, hand it work,
and go do something else. Apache-2.0, macOS, pre-1.0. No account, no server,
and no subscription you do not already pay for.

```bash
curl -fsSL https://raw.githubusercontent.com/witw-llc/ar3/main/get.sh | sh
```

**You already knew you could not do it alone. This is the how.**

You have a list of things you always meant to build: a game, an album, a novel,
the tool you keep re-imagining. Years ago you accepted you could not build them
alone, because a team costs capital you were never going to spend on a side
project. That is the part that changed. AR3 turns the AI subscriptions already
on your machine into a working team, run from one file, reporting to you.
Technology is the capital now. Your experience is what it scales.

It refuses to waste what you pay for. Each member carries a budget, a throttle
and a queue that survives a crash, so the work runs flat out, stops at the line
you set, and leaves you progress and a decision.

## Quickstart

Three lines. This is the target:

```bash
# 1. install. Clones to ~/.ar3 and adds one source line to your shell rc.
curl -fsSL https://raw.githubusercontent.com/witw-llc/ar3/main/get.sh | sh
# 2. build the team on a repo you already have
ar3 init ~/my-repo
# 3. give it work
tell my-repo "Introduce yourselves."
```

Line 2 ships next; until it does, [The Ark Raising](guide/README.md) walks the
same ground in the verbs that exist today. That one command gives you a
three-member team, a lead who talks to you, a builder, and a critic whose only
job is to break what the builder made, plus one machine-wide address named `me`
so the answer has somewhere to arrive.

## What comes back

The outcome waits in a local inbox you read with one command. Run on
2026-09-03 against a throwaway repository holding a single file, this reply
reached that inbox 82 seconds after the tell.

```text
$ tell demo "Introduce yourselves, one line each, and say what you would do
  first with this repo."
$ a8s convo me

demo:lead
  Three of us, one line each:

  - **Lead (me)** — I hold the mission and talk to you; every question routes
    through me and comes back as one answer, not three.
  - **Dev** — writes the code, the tests, and the changelog line; opens
    branches, never merges them.
  - **Critic** — adversarial review only; tries to break what Dev built before
    you see it.

  **First move — Dev and Critic converged independently on the same thing:** a
  `CLAUDE.md` stating what this repo is actually for, before any feature work.

  **One thing blocks all of it:** the repo is one commit, one file. Dev won't
  write code until you name the stack. Tell me the language and what this repo
  is meant to become, and I'll put both of them on it.
```

## What it is

**One file is the whole surface.** You write the team down in `r4t.md`: mission,
charter, roster, cells, rigs, rituals. Everything else defaults until it earns
the tuning.

**One short file, long defaults.** AR3 drives the agent CLIs already on your
machine: claude, codex, copilot, cursor, agy, opencode, muse and ollama. Each
runs as the vendor's own CLI in its own process, the sanctioned path under
their terms, and no subscription token goes into a foreign tool.

**Tip of the iceberg.** You talk to one agent. The roster carries the depth, and
one person facing a dozen agents directly is a stack overflow.

**Director, not operator.** Ceremony that puts you on a mechanical step is a
defect in the loop, so technical steps never carry your name.

### The four apps

- **a8s**, the router. Any CLI agent reaches any other through one verb, `tell`.
- **r4t**, the roster. Every turn dispatched, budgeted, throttled and audited.
- **k7e**, the knowledge engine. Flat markdown and search, so nothing gets
  learned twice.
- **ar3**, the front door. Today it reads suite state and probes what is
  installed; `ar3 init`, the one command that composes what the other three do
  by hand, ships next.

One doctrine across all four: pure standard library, files are truth, no
daemons to babysit, and a turn can be held inside a container.

## The problem

You chased the ambition, and the hours went to work and to family instead. The
subscriptions are already on the card, and most of the month they sit idle. The
scarce input stopped being hours a while ago: it is your attention. Put a team
on the work and judgment takes over, and agents have none of their own.

## What you get today

- **A roster on your repo, from one command.** `r4t add ~/your-repo triforce`
  registers a lead, a builder and a critic from a shipped runbook.
- **The answer waits for you.** `a8s convo me` reads what came back, whenever
  you get to it; a reply stays in the archive until the retention you set
  prunes it.
- **The rigs you already pay for.** `r4t engine list` names eight engines and
  twelve presets today, found on your machine instead of typed into a config,
  and `r4t engine <id> quota` says what is left before you spend it.
- **No agent can impersonate another.** The router stamps the sender from the
  directory that owns the message. The filesystem is the identity.

## The executive loop

One agent stands between you and the team and owns disposition. You own
direction, and the acts nobody else may perform: merge to main, production
deploy, money movement, vendor mutation, secret put. Two questions do the
filtering. Going up, *"what does this change about the product?"*, and a
sentence that cannot answer it stays in the team layer. Coming down, *"is this
a product decision or an irreversible act?"*, and if it is neither, the
lieutenant decides.

## What it is not

**"Will they just talk to each other all day?"** Structure comes from outside
the agents, because they cannot supply it. Left unsupervised, agents will spend
a plan's whole month thanking each other. Roles and cadence stop that.

**"Why not the chat window I already have?"** You type, you read, you process,
you reply, and every vendor is about to hand you one more chat surface to
attend.

Agent tools do keep dying, and that is a fair thing to hold against a new one.
AR3 is not a business you depend on. It is files on your disk driving binaries
you already installed.

## Get it

The install line is at the top of this page. It clones to `~/.ar3`, adds one
`source` line to your shell rc, and installs nothing into your projects.
Re-running it updates in place, and so does `ar3 update`. If piping a script
into a shell makes you uneasy, download `get.sh` and read it first. It is short.

macOS is what is claimed today, and Windows is next: the `.cmd` and `.ps1`
shims ship beside every command, and nothing is claimed there until it is
tested there.

- **[The Ark Raising](guide/README.md)** is the build-along that raises a roster
  from nothing, chapter by chapter.
- **Recipe: Desktop Filedrop Agent** and **Recipe: Executive Loop** are public.
  Point an agent at either one and it configures itself. Reaching you by mail or
  on a phone is recipe territory too, wired from a8s remotes and connectors, and
  not something AR3 claims to do for you yet.
- **[docs/ar3.md](docs/ar3.md)** carries the install variants, and
  **[docs/a8s.md](docs/a8s.md)**, **[docs/r4t.md](docs/r4t.md)** and
  **[docs/k7e.md](docs/k7e.md)** cover the apps, flat under [`docs/`](docs/).

## Where it is going

**V1 is a life assistant on your own machine**, connected to your life through
channels you already own, adding no account and no second device. Then the
roster library: pick a team off the shelf, answer the same two questions, and
you are in the director's chair.

**1.0** is the version a person who has never met us can install from one
public URL, describe a week of work to, walk away from, and be reached by on a
channel they already own, proved by one timed walk a stranger completes with
nobody helping, and declared ready when it is done rather than on a date.

**Versioning.** `VERSION` carries one semver for the suite and every merge to
`main` increments it. [witw-llc/ar3](https://github.com/witw-llc/ar3) is the mirror.

**Licensing.** Code, meaning everything outside `guide/`, is Apache-2.0. See
[`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). *The Ark Raising*, everything under
`guide/`, is CC BY-NC-ND 4.0. See [`guide/LICENSE.md`](guide/LICENSE.md).
