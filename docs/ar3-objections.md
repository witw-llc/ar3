# Objections, and the story for each

Every objection this product meets, written down with the story that answers it;
an answer that does not settle the reader gets replaced, never defended.

## Trust

*"I have to give it everything. How do I trust it?"*

The roster in your repo may only name a rig. Argv, timeouts and limits live in
out-of-repo config, so a repo edit can never change what runs, and an unknown
rig fails closed. A runbook may ask for a permission stance, and the machine
caps which one until `r4t add <dir> --trust` raises it for that node, re-checked
every turn. Harness invocation never goes through a shell, and identity comes
from the filesystem: the router force-stamps `from` by outbox ownership, so no
message can claim to be another member. You keep the merge; members branch and
open pull requests. See [r4t-security.md](r4t-security.md) and
[a8s.md](a8s.md).

`Status: answered`

## Confidentiality

*"Where does my data go?"*

It goes where it already goes. r4t drives the CLI harnesses already installed on
your machine, so the model vendor you accepted when you installed one is the
whole trust surface; AR3 adds no new party to it. State lives under
`~/.config/r4t/` and `~/.config/a8s/`, and messages move over your own
filesystem or a broker you run. The leak to guard is the vendor, not the
network. Repo-side, `tools/pii-scan.py` sweeps every tracked file on every merge,
because a diff guard never sees a name that is already in.

`Status: answered`

## Effectiveness

*"How do I know it will get work done?"*

The verdict is machinery the agent cannot see into: `r4t check` prints
`check passed` or `check failed: N finding(s)` and nothing else, findings go to
stderr where only you read them, and the checklist lives outside the repo. You
can prove the whole pipeline before spending a token with `r4t sandbox --fake`.
What is missing is a measured claim: no published number shows a governed roster
finishing more of a solo builder's backlog than an ungoverned one. The live
roster building this suite is the strongest available evidence, and a verifiable
count beats a percentage. See [r4t-verification.md](r4t-verification.md).

`Status: partial` (mechanism shipped; the sentence needs one measured number)

## Staying on target

*"What stops them from thanking each other all night?"*

Structure comes from outside the agents, because a member cannot supply its own.
Every limit is enforced at dispatch or outbox release, never inside the model.
Duplicate collapse folds a burst of identical arrivals into one turn. Spend
budgets are token buckets, one per member and one for the cell, and an empty
bucket rests the member while its queue holds. A node runs one turn at a time,
with no number to raise, and an optional cadence floor degrades a perfect storm
into a slow visible drip. A failing member trips a breaker instead of burning a
turn per arrival. See [r4t-governance.md](r4t-governance.md).

`Status: answered`

## Why not just one strong agent?

*"One good agent already handles my work. Why a roster?"*

The claim is coverage, not raw capability. A roster is how work continues while
you are away from the desk, and how a member's context stays small: the tree
hides information and reroutes it, so no one member carries the whole node. One
turn at a time keeps the stream watchable, which is what a single agent running
flat out costs you. No benchmark shows a roster beating one strong agent for a
solo builder, so the page must not imply one.

`Status: partial` (the coverage argument holds; the performance argument has no
evidence and must not be made)

## Cost, and the subscriptions' terms

*"Will this burn my quota, or my account?"*

r4t runs each vendor's own CLI as its own process, in the automation mode that
CLI documents: `claude`, `codex`, `cursor`, `copilot`, `agy`, `opencode`, `muse`,
and local `ollama-*` wrappers. No subscription token is handed to a foreign
tool, which is the line the vendors' terms actually draw. Spend is capped twice:
the budget buckets above, and `max_ai_credits`, which composes the vendor's own
fuse; a rig naming it on a fuse-less preset fails to load. Put frontier rigs on a
low budget and local rigs on a high one. The open question is which pool a
headless run bills against, and that must be re-read against each vendor's
current policy page before this claim is published.

`Status: partial` (mechanism shipped; the billing-pool sentence needs primary
sources)

## Reliability

*"What if the thing running my work dies while I sleep?"*

The posture answers half of it: AR3 is files on your disk driving binaries you
already installed, not a service you depend on, and isolation puts a rig behind a
real OS boundary so it cannot change what runs. The other half is open. On
2026-09-03 a coordinating assistant on a shared box stopped in the night while
the ordinary seats on the same host came back on their own, and nothing signalled
the difference: a seat that is idle and a seat that is dead look identical from
outside. A per-turn liveness signal would close it, together with a health probe
that tests the far side rather than a local round trip. See
[r4t-isolation.md](r4t-isolation.md).

`Status: open` (needs #93 delivery failures visible to the sender, #223 a
non-local health probe, #205 durable deferred delivery, #141 sender-stamped
checksums)

## Is this a framework?

*"Another framework to learn?"*

No. You write one file that names the roster, and you run `r4t`. There is
nothing to import, subclass or wire, and the agents are the CLIs already on the
machine rather than something you build. The installer only prepends the repo
directory to `PATH`; AR3 plants nothing inside your project, and skills reach an
agent through the environment r4t and a8s inject at wake. If the first
instruction does not fit in one sentence, the surface is wrong.

`Status: answered`

## Do I need a new account or a new device?

*"What else do I have to sign up for?"*

Nothing. No account, no device, no dashboard and no web page to watch. Messaging
rides your own filesystem or a broker you already run, and the seats are the
harnesses you already pay for. What AR3 does not yet promise is reaching you off
the machine: there is no phone, SMS or email path, and delivery is
envelope-accepted rather than confirmed read.

`Status: answered` (on the account question; the off-machine delivery gap is
tracked under Reliability above)

## How to use this page

The ledger grows. Add a row the first time an objection lands, and record the
answer you actually gave. When an objection keeps landing, the answer is the
thing that is wrong: write a new story for it, never a defence of the old one.
An entry marked `open` is a work order, not a disclaimer.
