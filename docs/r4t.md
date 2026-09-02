# r4t — the roster

An unsupervised agent roster once burned 40% of a monthly AI plan thanking
each other for thanking each other. The quieter waste is the opposite one:
a subscription costs the same idle or busy, so every unspent prompt is money
already paid and thrown away. r4t exists to end both — the plan you pay for
stays earning, and no roster can ever blow it. The spend underneath both is
attention: every sharp edge a model mishandles pulls you out of the vision
seat and into the trenches, so the rule here is that the harness holds the
edges — defaults do the right thing, prompts remind, skills instruct — and
neither you nor the model is trusted to be careful.

AI CLI agents — Claude Code, Codex, OpenCode, Copilot, Antigravity, local
Ollama models — already message each other over [a8s](a8s.md).
But a8s is deliberately dumb: it delivers messages and files, nothing more.
No budgets, no retries, no queue. r4t is the layer any AI CLI connects to
a8s **through**: name your members in one `r4t.md`, say what each one runs,
and every turn is dispatched, budgeted,
throttled, queued, and audited — no agent polices itself, and nothing ever
waits on a human. Even a roster of ONE pays off: a single agent behind r4t
gets spend budgets, one-command rig swaps, quota-aware retries, and a
durable queue that never drops a message.

The name is lineage: r4t reads as *runner for tells*. A tell arriving from
a8s is the impetus for everything here, and a governed turn is what runs in
reply.

## Quick start

Prove the pipeline first — no LLM, no API keys, all state in a throwaway dir:

```bash
r4t sandbox --fake
```

Three scripted agents build and test a tiny game; a report lands on stdout
(`Program runs and exits 0 | PASS`, dead letters 0, ...).

Now a real roster on your repo. Three facts get you there — your directory,
a runbook, and the engine each member names:

```bash
r4t add ~/your-repo triforce   # a shipped runbook: a lead, a builder, a critic
```

One command validates the runbook and registers the node: the a8s agent, the
namespace prefix and the address you type are all **one name**, the
directory's own. To check in a runbook you can edit instead, write one first:

```bash
cd ~/your-repo
r4t init            # writes r4t.md, extending triforce
r4t runbook check   # -> ".../r4t.md: OK (3 member(s), leader Lead)"
r4t add ~/your-repo # the file is already there; name no runbook
```

`r4t init` writes the file and nothing else; `r4t add` registers the node and
writes nothing into your repo. A member that names its `Engine:` inline needs
no machine config at all — `~/.config/r4t/rigs.json` is for symbolic rigs
shared across rosters (`r4t rig presets`, `r4t rig swap leader claude`).

Give yourself an address and say hello:

```bash
a8s add me ~/a8s-me && a8s start me
export TELL_OUTBOX_DIR=~/a8s-me/.outbox
tell your-repo "Introduce yourselves."
```

Watch it work (from inside the repo):

```bash
r4t status   # health verdicts, member budgets, queues, dead letters
r4t logs -f  # every governance decision and turn as it happens
```

The roster's reply arrives in `a8s convo me`. Full walkthrough, including
what fails closed when the roster and rig config disagree:
[r4t-tutorial.md](r4t-tutorial.md).

Ask any engine how much subscription is left before pointing a roster at it,
or run one directly with no roster at all:

```bash
r4t engine list                       # every engine, and the presets each one serves
r4t engine codex quota                # remaining fraction + reset time, no turn spent
r4t engine claude quota --json
r4t engine claude run --agent my-node "check the deploy and report"
```

Live answers persist as snapshots, so a check still answers (age-stamped)
when the engine's own surface cannot. `run` is the bare tier: one headless
turn, no roster, memory is whatever `STATUS.md`/`LESSONS.md` say — see
[Engine](r4t-engine.md).

## The runbook — one file that says what the team is

A node's whole configuration is one markdown file, `r4t.md`, at the node
directory. It replaces `ROSTER.md` + `MISSION.md` + `CHARTER.md` + `rigs.json`
+ `r4t-org.json`, and it wins when both formats are present — the legacy files
beside it are named as ignored, never blended. Every key, with its allowed
values, its default and a line to paste: [Runbook options](r4t-runbook.md).

The shortest whole runbook builds on a shipped one. `triforce` is three
members around one repo — one who talks to you, one who builds, one who tries
to break it — and `ar3-suite` is the same three under release discipline.
They resolve by name, like an a8s bundled definition; you never copy one to
use it.

```markdown
---
name: "myproject"
extends: "triforce"
---

## Mission

Get the 0.4 release out. Nothing else matters until it ships.
```

```bash
r4t runbook check
r4t runbook show --resolved --sources
```

**Frontmatter, then six closed H2 sections.** An unknown or repeated `##` is a
hard error naming the set.

| Section | Kind | What it is |
|---|---|---|
| `## Mission` | prose | The north star. Injected to leads verbatim. The one mutable part. |
| `## Charter` | prose | How the team operates whatever it is working on. Injected to **every** member. |
| `## Roster` | collection | `###` per member. Required. |
| `## Cells` | collection | `###` per cell. Optional. |
| `## Rigs` | collection | `###` per rig. Optional. Shadows the machine's `rigs.json` by name. |
| `## Rituals` | collection | `###` per ritual. Optional. |

**One block grammar, everywhere.** A `###` block is a leading run of
`- **Key:** value` bullets, then free prose that reaches the model verbatim.
The bold is optional and keys ignore case, spaces and underscores, so
`- Allowed tools: Read`, `- **allowed_tools:** Read` and `- allowedtools: Read`
are one key. Prose *before* the first `###` in a collection section is the
reader's orientation text and is ignored.

```markdown
### Lead
- **Engine:** claude --model opus
- **Leader:** yes
- **Role:** Talks to the owner, holds the mission

You are the only member the owner talks to. Route each question to whoever
owns it and return one reconciled answer.
```

**A member is complete with one field.** `Engine:` is the inline style — a
`r4t engine <id> run` invocation minus the prompt, so a member that misbehaves
is debugged by copying its own line out of the file and running it. `Rig:` is
the class. Both are written in the same property language, so promoting one to
the other is cut, paste, name it. A member carrying both is refused.

| Member key | Values | Default |
|---|---|---|
| `Engine:` | `<engine> [--model M] [--permissions ask\|auto\|bypass] [--allowed-tools SPEC] [--timeout S]` | — |
| `Rig:` | a rig name, resolved in `## Rigs` first, then `rigs.json` | — |
| `Leader:` | `yes`/`no` — exactly one per runbook | `no` |
| `Ingress:` | `on`/`off` | `off`; `on` for the leader |
| `Cell:` · `Lead:` · `Role:` | a cell name · a member name · one line of prose | — |
| `Workdir:` | a path, relative to **the node dir** | frontmatter `workdir:` |
| `Continue:` · `Knowledge:` · `ProseReply:` · `Framing:` · `Reinforce:` | as in [Rigs](r4t-rigs.md) and [Knowledge](r4t-knowledge.md) | |

Rig blocks take `Engine:`, `Allowed tools:`, `Rig budget:`/`Member budget:`
(`12 per hour, max 12`), `Env:` (the one repeatable key), `MCP:`, `Echo:`,
`Echo max:`, `Max sends:`, `History:`. **A rig declared here shadows a machine
rig of the same name, whole-block** — never field-merged, so a runbook rig can
never inherit a permission stance you cannot see. Cell blocks take `Lead:` and
`Ingress:`; ritual blocks take `When:`, `To:` and `Budget:`.

**The runbook proposes; the machine caps.** A runbook is checked in, so the
stance it names is capped by a permission ceiling that lives out of the repo —
`auto` until you raise it, per node:

```bash
r4t add ~/their-repo --trust   # this runbook may name --permissions bypass
```

A rig asking above the ceiling fails closed with the remedy, and the ceiling
is re-checked every turn — editing `r4t.md` to `bypass` after an untrusted
`add` stops that member at the next wake. Cloning a stranger's repo and
registering it is therefore not a code-execution decision made silently.

**`extends:` declares the base**, and there are two merge rules, one per
document level: frontmatter merges per key, and an H2 section **replaces the
base's whole**. The base is a built-in name (`triforce`, `ar3-suite`) or a path
relative to the file. Chains compose up to five deep, which is also how a
runbook splits across files — each file naming only its own sections.

```yaml
extends: "triforce"        # a built-in
extends: "./parts/rigs.md" # a path
```

**Frontmatter** carries what `r4t-org.json` held: `name:`, `extends:`,
`workdir:` (the workplace repo, relative to the node dir — `"."` means the node
dir itself), `comms:`, `egress:`, `leader_sees_lateral:`, `priority_senders:`,
`run_as:` / `container:` / `container_args:`. Quote string scalars.

**Variables** are `${VAR}`, `${VAR:-default}` and `${VAR:?message}`, resolved
from the node's a8s vars (`a8s vars <node> set KEY value`, or `A8S_VAR_<KEY>`
in the environment). They reach field values and prose only — never a heading
and never frontmatter — so the shape of the file reads the same whether or not
you know what a variable holds. An unset variable with no default is a hard
error at load, never an empty string. A node var named `MISSION` replaces the
`## Mission` section outright, which is how one runbook serves two projects.

**`r4t runbook show --resolved` is not a convenience.** With inheritance, the
file you read is not the file that runs; this is the command that closes the
gap, and `--sources` names the layer every section came from.

```
$ r4t runbook show --resolved --sources | head -4
## Mission                                    [node var MISSION]
Get the 0.4 release out. Nothing else matters until it ships.
## Charter                                    [triforce]
```

`r4t runbook check` lints the resolved file: every error names the line, the
offending token, and the closed set it should have come from.

v1 does not carry H3-level block merge, `Remove:` tombstones, or an implicit
`r4t/` directory convention — the `extends:` chain is the split. A runbook
using one of them is refused by name, as deferred rather than unknown.

## Addressing — the node is the namespace

`r4t add` binds one name to the a8s agent, the namespace prefix and the node
directory, so `a8s ls` and `r4t status` say the same word and nothing carries a
`-node` suffix. Three forms, and no fourth:

| Typed | Means |
|---|---|
| `node` | the roster **leader** — the node's door |
| `node:member` | that member, when it carries `- **Ingress:** yes` |
| `:name` | the **global** a8s recipient `name`, from any vantage |

**Colons are for namespacing only.** A member, cell or node name uses a8s's own
grammar — `[A-Za-z0-9][A-Za-z0-9_-]*` — so a colon can never appear inside one
and `node:member` parses one way. `node:` and `::name` are errors.

**Inside the walls a roster name wins.** A member writes `tell amy` and reaches
the member `amy`, even when an a8s node of that name is visible from the host;
a bare name matching no member is still routed outward, and logged. `:amy` is
the escape hatch, and the one case in the grammar where the colon is
mandatory. It is stripped on resolution, so the recipient sees `amy` and
replies to it without knowing a colon was ever typed. `r4t roster check` names
every collision where you will need it.

**A member behind the wall is refused, not redirected.**

```
r4t: dana does not accept ingress; external mail enters at the leader —
     send to acme, or set `- **Ingress:** yes` on dana in the runbook.
```

Silently landing it on the leader would have the leader answer for a member who
never saw the message, and the sender would never learn its address was
ignored. An unknown sub-address and — until one post can fork to a whole cell —
a cell address are refused the same way, each dead-lettered with its reason and
one `REFUSED` line on the ticker.

**Qualifying your own node is a no-op**, so runbook and charter text is portable
verbatim: `acme:bob` means the same thing typed inside `acme` as outside it.
`:acme:bob` does not — a leading colon means the address leaves the walls, so
it comes back at the ingress gate like anyone else's.

## How it works

The roster leader — the one member marked `- **Leader:** yes` — is the node's
door: a message addressed to the node with nothing past the colon (`tell acme`)
is the leader's mail, and a roster that marks no leader or marks two is refused
when the roster loads rather than guessed at. Inside the walls, members message
each other by first name with the ordinary `tell`, delegate, and end their turn
— nobody blocks waiting.

**One turn at a time is a contract, not a setting.** A node runs exactly one
member turn, start to finish, and only then asks who goes next. That is what
makes `a8s logs <node> -f` a stream a person can follow: nothing interleaves,
because nothing is concurrent. There is no knob to raise, and the rotation is
always arithmetic — never a model deciding who speaks, because a queue whose
order came out of a model is a queue nobody can explain.

Who goes next: a member holding mail from a priority sender goes next, always
(`priority_senders` in [the org config](r4t-org.md), default empty — no
priority sender ships); otherwise
the highest `2*ask + 1*ingress + passes`, oldest mail breaking ties. Priority
never preempts — a running turn always finishes, so the promise is "next", not
"now". Nobody is passed over more than four times, because four passes outrank
anything the classes can add to freshly arrived mail. `r4t status` prints the
decomposition next to the number, so the answer to "why is that one next" is
one line and not an inference. Full rules:
[Operations](r4t-operations.md#the-rotation).

**The parallelism answer is a second node**, not a second turn: run the same
structure under another node name, on another machine or this one. The rig
spend buckets are machine-global, so two nodes on one Mac cannot double your
burn behind your back. What you give up is the single watchable stream — the
never-interleaved promise is per node — and one hung member stalling the whole
roster until its per-turn timeout ends it, which is why `r4t status` shows the
running turn's elapsed time against that timeout.

Every turn costs budget; a member out of
budget rests while its queue holds, and refill is the retry, so the machine's
one shared subscription never idles while any project has work. A member that
answers in prose instead of sending gets its output delivered as the reply
anyway — weak local models do this routinely, and strong models have done it
in production too (`- **ProseReply:** off` in the roster mutes this per member).
Traffic is fire-and-forget: a message carries no task and demands no answer,
and nothing tracks whether one came back. What r4t watches is whether the org
is moving — when every queue is empty and nobody has finished a turn, the
mission-review heartbeat hands the leader the mission again.
Full flow: [r4t-message-flow.md](r4t-message-flow.md).

## Learn more

- [The Ark Raising](../guide/README.md) — the suite build-along; [chapter 2](../guide/02-the-founding.md) founds a governed roster of one
- [Tutorial](r4t-tutorial.md) — first roster, step by step, fail-closed rules
- [Runbook options](r4t-runbook.md) — every `r4t.md` key, its values, its default, and a line to paste
- [Rigs](r4t-rigs.md) — presets, `--model`, settings, the governance knob table
- [Engine](r4t-engine.md) — talk to one CLI directly: `quota`, and the bare
  stateless `run` (scaffold, idle latch, a8s definition recipe)
- [Message flow](r4t-message-flow.md) — threads, queues, the stdout fallback
- [Operations](r4t-operations.md) — `status`, `logs`, and speaking in with `tell --as`
- [Org design](r4t-org.md) — cells and leads, `MISSION.md`, portable orgs
- [Idle pass](r4t-idle.md) — drain, dream, heartbeat, flush
- [Knowledge](r4t-knowledge.md) — a member's private k7e memory (experimental)
- [Verification](r4t-verification.md) — `r4t check`, checklists, the post-hoc judge, reading a run back
- [Governance](r4t-governance.md) — why each layer exists, with prior art
- [Security model](r4t-security.md) — what a repo edit can never change
- [Isolation](r4t-isolation.md) — run an org behind a Unix user or a container
- [Development](r4t-development.md) — sandbox testing, module layout
- Harness notes: [agy](r4t-harness-agy.md) ·
  [ollama launch](r4t-harness-ollama-launch.md) ·
  [landscape](r4t-harness-landscape.md)
