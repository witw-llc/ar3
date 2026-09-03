<!-- one-pager: ratified rulings 2026-09-03, the hand-off 2026-09-30; edit history in PR #247 -->

# AR3

**You already knew you could not do it alone. This is the how.**

You have a list of things you always meant to build: a game, an album, a novel,
the tool you keep re-imagining. Years ago you accepted you could not build them
alone, because a team costs capital you were never going to spend on a side
project. That is the part that changed. AR3 turns the AI subscriptions already
on your machine into a working team, run from one file, reporting to you.
Technology is the capital now. Your experience is what it scales.

It refuses to waste what you pay for. Each member carries a budget and a
throttle, and a queue that survives a crash. The work runs flat out, stops at
the line you set, and leaves you progress and a decision.

## Who this is for

You came up in the personal-computing boom and went straight at programming.
You still want to build things. You have far fewer hours than you did.

- **The shelved backlog.** The games, the music, the film, the novel, the tool:
  still there, and you stopped arguing about doing them alone years ago.
- The hours went to family and to work, so the building has to happen while you
  are away from the desk.
- **The clock.** What you know how to do is worth less every month you do not
  put it to work.
- One long-running assistant gets slow and foggy, says a message went out when
  it did not, and dies unnoticed. You are the one who finds out.

Put a team of agents on the work and hours stop being the scarce input.
Judgment takes over, and agents have none of their own. Decades of lived,
opinionated taste is the one thing they cannot supply, and this is the first
technology that scales it.

## What it is

**One file is the whole surface.** You write the team down in `r4t.md`: mission,
charter, roster, cells, rigs, rituals. One file names the team, and everything
else defaults until it earns the tuning.

**Be Docker.** Docker won on one committed file wrapping primitives already in
the kernel, because the file was short and the defaults were long. AR3 drives
the agent CLIs already on your machine: claude, codex, copilot, cursor, agy,
opencode, muse and ollama. Each runs as the vendor's own CLI in its own process,
the sanctioned path under their terms, and no subscription token goes into a
foreign tool. When a vendor's CLI moves, AR3 moves with it.

You are not being asked to learn agent plumbing first. The mechanics are there
and the roster already has agents in it.

**Tip of the iceberg.** You talk to one agent. The roster carries the depth, and
one person facing a dozen agents directly is a stack overflow.

**Director, not operator.** Ceremony that puts you on a mechanical step is a
defect in the loop, so technical steps never carry your name.

### The four apps

- **a8s**, the router. Any CLI agent reaches any other through one verb, `tell`.
- **r4t**, the roster. Every turn dispatched, budgeted, throttled and audited.
- **k7e** is the knowledge engine: flat markdown and search, so nothing gets
  learned twice.
- **ar3**, the front door. It reads state and probes prerequisites, and never
  runs another product's commands for you.

All four share one doctrine. Pure standard library, files are truth, no daemons
to babysit, and no subscription you do not already hold.

## What you get today

- **One address that never sleeps.** The roster's leader receives mail around
  the clock, holds what arrives until it can reach you on a channel you own,
  and a failed wake backs off instead of wedging the inbox.
- **A roster on your repo, from one command.** `r4t add ~/your-repo triforce`
  registers a lead, a builder and a critic from a shipped runbook.
- Eight engines and thirteen presets, found on your machine instead of typed
  into a config. `r4t rig swap leader claude` changes what a name runs, and a
  preset that grades poorly says so with its number.
- **What is left before you spend it.** `r4t engine <id> quota` returns the
  remaining fraction and the reset time without spending a turn, and it runs on
  Windows too, with a `.cmd` and a `.ps1` beside every shim.
- **Proof before you spend a token.** `r4t sandbox --fake` runs the pipeline
  with scripted agents, no key and no model. `r4t check` then sweeps the work for
  patterns you forbid, and Docker isolation keeps runs off your tree.
- `k7e store`, `k7e search`, `k7e recall`. The knowledge is flat markdown you
  own, over an index you can delete and rebuild.
- **No agent can impersonate another.** The router stamps the sender from the
  directory that owns the message. The filesystem is the identity.

## The executive loop

One agent stands between you and the team and owns disposition. You own
direction, and you own the acts nobody else may perform: merge to main,
production deploy, money movement, vendor mutation, secret put. What arrives
when you sit down is where you left off, numbered next actions, and what waits
on you with a recommendation. Two questions do the filtering. Going up:
*"What does this change about the product?"* A sentence that cannot answer it
stays in the team layer. Coming down: *"Is this a product decision or an
irreversible act?"* If it is neither, the lieutenant decides. The loop is
written down as a recipe you can point an agent at.

## Just add water

```bash
# 0. install. Clones to ~/.ar3 and adds one source line to your shell rc.
curl --proto '=https' --tlsv1.2 -fsSL \
  https://raw.githubusercontent.com/witw-llc/ar3/main/get.sh | sh
# 1. prove the whole pipeline. No model, no key, throwaway state.
r4t sandbox --fake
# 2. a real roster on a real repo
r4t add ~/your-repo triforce
# 3. give yourself an address
a8s add me ~/a8s-me && a8s start me
export TELL_OUTBOX_DIR=~/a8s-me/.outbox
# 4. speak
tell your-repo "Introduce yourselves."
# 5. read the answer
a8s convo me
```

One of those five costs nothing to run. You answer two questions, which
directory and which team shape, and defaults cover the rest: the engine from the
runbook, the rig from the preset, the address from the directory's own name.

## What it is not

**"Will they just talk to each other all day?"** Structure comes from outside
the agents, because they cannot supply it. Unsupervised, agents will spend 40%
of a monthly plan thanking each other. Roles and cadence stop that.

**"Why not the chat window I already have?"** You type, you read, you process,
you reply, and every vendor is about to hand you one more chat surface to
attend.

Agent tools do keep dying, and that is a fair thing to hold against a new one.
AR3 is not a business you depend on. It is files on your disk driving binaries
you already installed.

## Where it is going

**V1 is a life assistant on your own machine, connected to your life** through
channels you already own. It adds no account and no second device.

**Then the roster library.** Pick a company off the shelf, answer the same two
questions, and you are in the director's chair. Two runbooks ship today. After
that, reports reach you where you already are, spoken or by mail, with a send
that arrives or says it did not.

**1.0 is the version a person who has never met us can install from one public
URL, describe a week of work to, walk away from, and be reached by on a channel
they already own, with a story on file for what it is and why.** It ships
2026-09-30, through three gates.

- **The story.** A stranger reads this page and can say who AR3 is for and what
  it gives back, without opening a second page.
- **Devices.** Install and prerequisite probes work on macOS and on Windows, and
  your subscriptions reach a working rig without you writing config.
- **Communication.** A report reaches you on a channel you own, you reply to it,
  and the reply reaches the roster.

## Get it

The install line is step 0 above. It clones to `~/.ar3`, adds one `source` line
to your shell rc, and installs nothing into your projects. Re-running it updates
in place, and so does `ar3 update`. If piping a script into a shell makes you
uneasy, download `get.sh` and read it before you run it. It is short.

- **[The Ark Raising](guide/README.md)** is the build-along that raises a roster
  from nothing, chapter by chapter.
- **Recipe: Desktop Filedrop Agent** and **Recipe: Executive Loop** are public.
  Point an agent at either one and it configures itself.
- **[docs/ar3.md](docs/ar3.md)** carries the install variants, and
  **[docs/a8s.md](docs/a8s.md)**, **[docs/r4t.md](docs/r4t.md)** and
  **[docs/k7e.md](docs/k7e.md)** cover the apps, flat under [`docs/`](docs/).

**Versioning.** `VERSION` carries one semver for the suite and every merge to
`main` increments it. [witw-llc/ar3](https://github.com/witw-llc/ar3) is the mirror.

**Licensing.** Code, meaning everything outside `guide/`, is Apache-2.0. See
[`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). *The Ark Raising*, everything under
`guide/`, is CC BY-NC-ND 4.0. See [`guide/LICENSE.md`](guide/LICENSE.md).
