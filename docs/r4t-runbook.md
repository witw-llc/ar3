# Runbook options

Every key `r4t.md` accepts, with its allowed values, its default, and a line
you can paste. For what a runbook is and how to grow one, read
[r4t.md](r4t.md#the-runbook--one-file-that-says-what-the-team-is).

A runbook is YAML frontmatter, then a closed set of six `##` sections. Under
the four collection sections, each `###` block is a leading run of
`- **Key:** value` bullets followed by prose.

```markdown
---
name: "acme"
extends: "triforce"
workdir: "."
---

# Acme

Orientation prose. Ignored by the parser.

## Mission
## Charter

## Roster

### Lead
- **Engine:** claude --model opus
- **Leader:** yes
- **Role:** Talks to the owner

Prose here reaches the model verbatim.

## Cells

### build
- **Lead:** Lead

## Rigs

### worker
- **Engine:** claude --model sonnet
- **Rig budget:** 20 per hour, max 20

## Rituals

### standup
- **When:** weekdays 09:00
- **To:** Lead

The prompt the ritual carries.
```

## Frontmatter

Keys are lowercased. Values parse as `true`/`yes` and `false`/`no` to booleans,
`[a, b]` to a list, anything else to a quote-stripped string. Frontmatter is
never interpolated and merges **per key** along the `extends:` chain. A key
spelled wrong warns, naming the key and the accepted set, rather than erroring
— frontmatter is the org seam, and a hard error here would break forward
compatibility for zero gain.

| Key | Values | Default | Sample | What it sets |
|---|---|---|---|---|
| `name` | string | the node directory's own name | `name: "acme"` | The roster's name. |
| `extends` | a built-in name or a path | none | `extends: "triforce"` | The base runbook this one inherits from. |
| `workdir` | path relative to the node dir, or `"."` | `"."` — the node dir is the workplace | `workdir: "../.."` | The workplace repo a member's turns run in. |
| `comms` | `"open"` / `"closed"` | `"open"` | `comms: "closed"` | `open` delivers a tell to any valid member; `closed` reroutes a tell outside the sender's tree adjacency to its lead. |
| `egress` | boolean | `true` | `egress: false` | `true` lets the topmost leader alone originate mail outside the roster; `false` lets no member do it. |
| `leader_sees_lateral` | boolean | `false` | `leader_sees_lateral: true` | Lands a read-only copy of a lateral delivery on the lead. |
| `priority_senders` | list of glob strings | `[]` | `priority_senders: ["boss*", "oncall"]` | Tier-1 senders; the member holding their mail goes next in the rotation. Empty by default — no priority sender ships. |
| `run_as` | non-empty username string | unset | `run_as: "r4t-worker"` | Runs every member turn as this Unix user. Excludes `container`. |
| `container` | non-empty image string | unset | `container: "python:3.12"` | Runs every member turn in this container image. Excludes `run_as`. |
| `container_args` | list of strings | `[]` | `container_args: ["--network", "none"]` | Extra arguments for the container runtime. Needs `container`. |

## Sections

Six `##` headings and no others. An unknown or repeated one is a hard error
naming the set. Heading text is matched case-insensitively.

| Section | Kind | Required | What it holds |
|---|---|---|---|
| `## Mission` | prose | no | The north star. A node var named `MISSION` replaces it outright. |
| `## Charter` | prose | no | How the team operates whatever it works on. |
| `## Roster` | collection | yes | One `###` per member. A runbook with no leader fails to load. |
| `## Cells` | collection | no | One `###` per cell. |
| `## Rigs` | collection | no | One `###` per rig. Shadows a machine rig of the same name, whole-block. |
| `## Rituals` | collection | no | One `###` per ritual. |

## Block grammar

- Fields are the **leading run** of `- Key: value` bullets. The first
  non-bullet line ends them; everything from there on is prose.
- The bold is optional: `- **Rig:** big` and `- Rig: big` parse identically.
- Keys ignore case, spaces and underscores — `Allowed tools`, `allowed_tools`
  and `allowedtools` are one key.
- Values are stripped of surrounding backticks and asterisks.
- A key set twice is an error. `Env:` is the one key that repeats.
- Prose before the first `###` in a collection section is orientation text and
  is ignored.
- A member block's whole text — its bullets and its prose together — becomes
  that member's persona in the turn prompt.
- Block names take letters, digits, underscore and hyphen, and must start with
  a letter or a digit. A colon can never appear — it separates a node from a
  member in `tell node:member`.

## Member fields

`## Roster`, one `###` per member. A member is complete with one field: either
`Engine:` or `Rig:`, never both.

| Key | Values | Default | Sample | What it sets |
|---|---|---|---|---|
| `Engine:` | an engine line (below) | — | `- **Engine:** claude --model opus` | An inline rig for this member alone. |
| `Rig:` | a rig name — letters, digits, `_`, `-` | — | `- **Rig:** ark-lead` | The rig class, resolved in `## Rigs` first, then the machine's `rigs.json`. |
| `Leader:` | boolean — exactly one member per runbook | `no` | `- **Leader:** yes` | Mail addressed to the node itself lands here. A value outside the boolean vocabulary is a field-level error, not a silent `no`. |
| `Ingress:` | boolean | `off`; `on` for the leader | `- **Ingress:** on` | Whether `tell node:member` from outside the roster delivers here. |
| `Cell:` | a cell name declared in `## Cells` | unset | `- **Cell:** build` | The cell this member belongs to. |
| `Lead:` | another member's name | unset | `- **Lead:** Mira` | The member this one reports to. Any `Lead:` line makes the tree structural. |
| `Role:` | one line of prose | empty | `- **Role:** Writes the code and the tests` | The one-line job title carried into the prompt. |
| `Workdir:` | a path; relative resolves against **the node dir** | the workplace | `- **Workdir:** services/api` | The directory this member's turns run from. |
| `Continue:` | `on` / `off` / an idle window | `off` | `- **Continue:** 15m` | Resume the CLI conversation between turns, dropping it after the window. |
| `Knowledge:` | `on` / `off` / a size / a rig name / `<size> <rig>` | `off` | `- **Knowledge:** medium` | The member's k7e store, its inject budget, and the rig that distills it. |
| `ProseReply:` | boolean | `on` | `- **ProseReply:** off` | Whether a turn's prose is staged as a reply when the member sent nothing. |
| `Framing:` | `default` / `off` / a double-quoted string | `default` | `- **Framing:** "Notes from your own past turns:"` | The line above injected knowledge. |
| `Reinforce:` | one line of prose | empty | `- **Reinforce:** Never push to main.` | A standing instruction repeated late in every prompt. Over 200 characters warns. |

A member takes no `Env:` line — environment rides the rig.

## Cell fields

`## Cells`, one `###` per cell.

| Key | Values | Default | Sample | What it sets |
|---|---|---|---|---|
| `Lead:` | a member's name | unset | `- **Lead:** Mira` | The member who leads this cell. |
| `Ingress:` | boolean | `off` | `- **Ingress:** on` | Marks the cell address as a door from outside the roster. |

## Rig fields

`## Rigs`, one `###` per rig. The block name is the rig name, lowercased. A rig
here shadows a machine rig of the same name **whole** — never field-merged.

| Key | Values | Default | Sample | What it sets |
|---|---|---|---|---|
| `Engine:` | an engine line (below) — required | — | `- **Engine:** claude --model sonnet` | The CLI this rig drives, and its model, stance, allowlist and timeout. |
| `Allowed tools:` | the engine's own allowlist string | the preset's own | `- **Allowed tools:** Bash Read Edit Write Glob Grep` | Replaces the preset's tool allowlist for every turn. Conflicts with `Engine: --allowed-tools`. |
| `Rig budget:` | `<n> per hour, max <n>` | unset — no rig-wide gate | `- **Rig budget:** 20 per hour, max 20` | The machine-global bucket every member on this rig shares. |
| `Member budget:` | `<n> per hour, max <n>` | `4 per hour, max 8` | `- **Member budget:** 8 per hour, max 16` | The per-member spend bucket. One turn costs one unit. |
| `Env:` | `NAME=value` — the one repeatable key | none | `- **Env:** GH_TOKEN=${GH_TOKEN}` | One environment variable for the turn. Repeat the line per variable. |
| `MCP:` | boolean | by preset | `- **MCP:** off` | Members send with the `a8s_tell` tool instead of the `tell` shell command. |
| `Echo:` | boolean | `off` | `- **Echo:** on` | Stage cleaned stdout as the one reply and drop the messaging scaffolding. |
| `Echo max:` | a number of characters | `1500` | `- **Echo max:** 4000` | Where an echoed body is truncated and attached instead. |
| `Max sends:` | a number | `6` | `- **Max sends:** 3` | Envelopes released per turn; the excess dead-letters. |
| `History:` | a number of bytes | by preset tier — 50000 for `claude`/`codex`/`agy`, 25000 for `cursor`/`opencode`/`copilot`, 8192 for the `ollama` variants | `- **History:** 25000` | The rolling history budget in the turn prompt. |

Numbers are read as integers, so `1500.0` and `1500` are the same value.

A rig block sets its permission stance and its timeout through `Engine:` flags,
not through keys of their own, and it takes no `Framing:` — that default lives
in `rigs.json` (see [Rig configuration](r4t-rigs.md)).

## Ritual fields

`## Rituals`, one `###` per ritual. The block's prose is the ritual's prompt
payload. This release parses and validates rituals and does not run them —
`r4t runbook check` says so per file — and the idle mission review is
scheduler behavior with a built-in prompt, not a ritual block. Firing is #137.

| Key | Values | Default | Sample | What it sets |
|---|---|---|---|---|
| `When:` | a schedule (below) — required | — | `- **When:** weekdays 09:00` | The cadence this ritual declares, in machine-local time. |
| `To:` | a member or cell name — required | — | `- **To:** Mira` | Who the prompt is addressed to. |
| `Budget:` | `charge` / `free` | `charge` | `- **Budget:** free` | Whether the turn spends from the member's bucket. |

## The `Engine:` line

`<engine> [flags]` — an `r4t engine <id> run` invocation minus the prompt, so a
member that misbehaves is debugged by copying its own line out of the file. The
flag set is closed.

```markdown
- **Engine:** claude --model opus --permissions auto --timeout 1800
```

| Flag | Values | Default | Sample | What it sets |
|---|---|---|---|---|
| `--model` | an engine-specific model id | the preset's own | `--model sonnet` | The model this rig runs. |
| `--permissions` | `ask` / `auto` / `bypass` | the preset's own flags | `--permissions auto` | The permission stance, translated into the engine's flags. Capped by the machine ceiling. |
| `--allowed-tools` | the engine's allowlist string | the preset's own | `--allowed-tools "Bash Read Edit"` | The tool allowlist. Conflicts with a `Allowed tools:` key on the same rig. |
| `--timeout` | seconds, a number | `900` | `--timeout 1800` | How long one turn may run. |

Engines: `agy`, `claude`, `codex`, `copilot`, `cursor`, `ollama`,
`ollama-claude`, `ollama-codex`, `ollama-copilot`, `ollama-opencode`,
`opencode`. `--continue` is refused by name — continuation is per member.

## Value grammars

| Grammar | Accepted forms | Used by |
|---|---|---|
| boolean | `yes` / `true` / `y` / `1` / `on`, and `no` / `false` / `n` / `0` / `off` — case-insensitive, anything else errors | `Leader:`, `ProseReply:`, `Ingress:` on a member or a cell, `MCP:`, `Echo:` |
| duration | a number of seconds, or a number with an `s` / `m` / `h` / `d` suffix — `on` and `off` also read as the boolean words above (`Continue:` is time-valued, not boolean, so any other value is tried as a duration rather than erroring) | `Continue:` |
| budget | `<n> per hour, max <n>` — both numbers, decimals allowed | `Rig budget:`, `Member budget:` |
| schedule | `every 30m` and `every 4h` · `daily 09:00` · `weekdays 09:00` · `weekly mon 09:00` · `monthly 1 09:00` · `on idle` — machine-local, and there is no cron form | `When:` |
| knowledge | `on` / `off` · a T-shirt size `small` (4096) / `medium` (8192) / `large` (32768) · an exact count like `4k` or `4096` · a rig name · `<size> <rig>` | `Knowledge:` |
| framing | `default` · `off` · a double-quoted string | `Framing:` |

## Variables

`${VAR}` resolves from `A8S_VAR_<KEY>` in the environment first, then from the
node's a8s vars (`a8s vars <node> set KEY value`). Substitution runs **before**
merging, so a resolved runbook carries none of it.

| Form | Unset | Set but empty | Sample |
|---|---|---|---|
| `${VAR}` | hard error naming the remedy | empty string | `- **Workdir:** ${SERVICE_DIR}` |
| `${VAR:-default}` | the default | the default | `- **Rig:** ${TIER:-worker}` |
| `${VAR:?message}` | hard error carrying your message | empty string | `- **Env:** TOKEN=${GH_TOKEN:?set a token first}` |

- Reaches **field values and prose only** — never a heading, never frontmatter.
- A node var named `MISSION` becomes the `## Mission` section at the highest
  precedence layer, above every file in the chain.

## `extends:`

| Rule | Value |
|---|---|
| Built-in bases | `ark-suite`, `triforce` — resolved by name, never copied |
| Path bases | anything starting `./` `../` `/` `~`, containing a slash, or ending `.md`; relative to the file that names it |
| Frontmatter merge | per key — the deriving file's key wins |
| Section merge | an `##` section **replaces** the base's whole; it never blends |
| Depth | five hops, six files |
| Cycle | hard error naming the loop |

```yaml
extends: "triforce"          # a built-in
extends: "./parts/rigs.md"   # a path, relative to this file
```

A file that names only its own sections adds them to the chain instead of
overriding — that is how a runbook splits across files.

## Validation errors

Every error names the file, the line, the offending token, and the closed set
it should have come from. `r4t runbook check` prints them all at once.

### Document

| Message | Cause | Fix |
|---|---|---|
| `unknown section` … `a runbook has exactly six` | an `##` heading outside the set | rename it to one of the six, or demote it to `###` |
| `appears twice — which one wins must never be a question` | a repeated `##` heading | merge the two into one |
| `frontmatter line is not `key: value`` | a bare line between the `---` markers | give it a key, or delete it |
| `frontmatter opened with `---` and never closed` | a missing second `---` | close the block |
| `names no built-in runbook — built-ins are:` | `extends:` naming an unknown built-in | use `triforce` or `ark-suite`, or write a path |
| `does not resolve — no file at` | `extends:` naming a missing file | fix the path; it resolves against this file's directory |
| `extends: forms a cycle` | two files naming each other | break the loop |
| `extends: chain is deeper than 5` | more than five hops | flatten the chain |
| `is not set — set it with `a8s vars` | `${VAR}` with no value and no default | set the var, or write `${VAR:-...}` |
| `runbook not found:` | no `r4t.md` at the node directory | `r4t init` |

### Fields and values

| Message | Cause | Fix |
|---|---|---|
| `unknown member field` / `cell field` / `rig field` / `ritual field` | a key outside that block's set | the message lists the whole accepted set |
| `is set 2 times; only Env: repeats` | a duplicated key in one block | delete one |
| `carries both Engine: and Rig: — delete one` | a member mid-promotion | keep the one that is live |
| `names neither Engine: nor Rig: — there is nothing to run` | an empty member block | give it one of the two |
| `a rig block needs an Engine: line` | a rig with no engine | add one |
| `the rig sets Allowed tools: and Engine: --allowed-tools — delete one` | the allowlist written twice | keep either spelling |
| `is not an engine — choose one of:` | an unknown engine id | the message lists all eleven |
| `Engine: unknown flag` | a flag outside the closed four | use `--model`, `--permissions`, `--allowed-tools`, `--timeout` |
| `Engine: takes no --continue` | continuation written on the rig | put `Continue:` on the member |
| `is not a stance — one of: ask, auto, bypass` | a bad `--permissions` value | pick one of the three |
| `is not a number of seconds` | a bad `--timeout` value | give a number |
| `must read like `8 per hour, max 16`` | a bad budget line | match the grammar exactly |
| `must be a number (got` | a bad `Max sends:`, `History:` or `Echo max:` | give a number |
| `must be yes/no/true/false/y/n/1/0/on/off (got` | a bad `Leader:`, `Ingress:` (member or cell), `ProseReply:`, `MCP:` or `Echo:` value | pick one of the accepted words — the message opens with the field name |
| `Env: must read like NAME=value` | an `Env:` line with no `=` | add the name |
| `Rig must be a symbolic rig name, not a command` | a command line written after `Rig:` | move it to `Engine:` |
| `Continue must be on, off, or an idle window like 15m` | a bad `Continue:` value | `on`, `off`, or `15m` |
| `Knowledge must be on, off, a T-shirt size` … | a bad `Knowledge:` value | see the knowledge grammar above |
| `Framing must be default, off, or a double-quoted custom string` | unquoted custom framing | wrap it in double quotes |
| `contains a colon` | a colon in a block name | the colon separates node from member |
| `is not a valid address — letters, digits, underscore and hyphen only` | any other bad block name | rename it |

### Refused by name

Keys the parser knows and turns down, so a reader hears why rather than
"unknown key".

| Key | What the parser says |
|---|---|
| `Human:` | gone — the node is the apex; the owner is an ordinary a8s agent reached with `tell` |
| `Address:` | gone — mail crossing the wall is a8s's job |
| `Status:` | gone — members carry no marker |
| `Flush:` | not a field — the idle window rides `Continue:` |
| `Fallback:` | gone — the knob is `ProseReply:` |
| `Mandate:` | gone — the one-line job title is `Role:` |
| `Remove:` | deferred — no tombstones and no `###`-level merge; a section replaces whole |
| `Budget:` on a cell | deferred — the cell bucket is roster-wide, in `rigs.json` |
| `Concurrency:` | gone — one live turn per node is the contract |

### Cross-checks and the ceiling

| Message | Cause | Fix |
|---|---|---|
| `marks no leader` / `marks 2 leaders` | not exactly one `Leader:` | mark exactly one member |
| `duplicate roster entry` | two `###` blocks with one name | rename one |
| `names both a member and a cell` | one name in two namespaces | rename one; `tell node:name` has to mean one thing |
| `Lead 'x' is not a member of this roster` | a `Lead:` naming nobody | fix the spelling |
| `Cell 'x' is not declared in `## Cells`` | a `Cell:` naming no block | declare it, or fix the spelling |
| `To 'x' names neither a member nor a cell` | a ritual addressed to nobody | fix the spelling |
| `a ritual needs a When: line` / `a To: line` | a ritual missing a required key | add it |
| `When must be one of:` | a bad schedule | see the schedule grammar above |
| `Budget must be charge or free` | a bad ritual budget | `charge` or `free` |
| `is above the trust ceiling` | a rig naming a stance the machine caps | `r4t add <dir> --trust` raises the node's ceiling to `bypass` |
| `names a cell, and one post forked to a whole cell is deferred (#183)` | mail sent to `node:cell` | address a member, or send to the node to reach the leader |

The ceiling is `auto` until `r4t add --trust` raises it, it is stored outside
the repo, and it is re-checked every turn — editing `r4t.md` after an untrusted
`add` stops that member at the next wake.

## Warnings

Printed by `r4t runbook check`; none of them block a turn.

| Warning | Means |
|---|---|
| `rig 'x' is declared and no member names it` | a `## Rigs` block nothing uses |
| `cell 'x' is declared and no member joins it` | a `## Cells` block nothing joins |
| `has Ingress: on and is not the leader` | a second door into the roster |
| `Reinforce is N characters` | over 200 — a paragraph is a mission, not a reinforcement |
| `is the roster;` … `are ignored` | `ROSTER.md`, `MISSION.md`, `CHARTER.md` or `r4t-org.json` sitting beside the runbook |
| `Knowledge is on with rig` … | a small-model class distilling notes |
| `frontmatter key 'x' is not recognized — accepted:` … | a misspelled or unknown frontmatter key — the message lists the whole accepted set |

A malformed `comms:` / `egress:` / other org setting also warns wherever a
command dispatches on it — `r4t status`, `dispatch`, `tell`, and the rest —
printed to stderr, and the turn still runs on the default. `r4t roster check`
/ `runbook check` report the same problem as blocking instead (`org: …`),
which is why it is not one of the rows above.

## Commands

| Command | Flags | Does |
|---|---|---|
| `r4t init` | `--root` (default: cwd) | Writes a starter `r4t.md` that extends `triforce`. Leaves an existing file unchanged. |
| `r4t add <dir> [runbook]` | `--name`, `--trust`, `--rig-config` | Validates the runbook, then binds the a8s agent, the namespace and the address under one name. The optional positional is a built-in name or a path, omitted when the directory carries its own `r4t.md`. |
| `r4t runbook show` | `--root`, `--node`, `--resolved`, `--sources` | Prints the file, or the merged and interpolated truth. `--sources` implies `--resolved`. |
| `r4t runbook check` | `--root`, `--roster`, `--rig-config`, `--definition`, `--node` | Lints the resolved runbook — the same checks as `r4t roster check`. |

```bash
r4t init                                # a starter r4t.md here
r4t runbook check                       # every problem at once
r4t runbook show --resolved --sources   # which layer each section came from
r4t add ~/your-repo triforce            # register with a shipped runbook
r4t add ~/their-repo --trust            # this runbook may name --permissions bypass
```

With inheritance the file you read is not the file that runs. `--sources`
closes the gap:

```
$ r4t runbook show --resolved --sources | head -4
## Mission                                    [node var MISSION]
Get the 0.4 release out. Nothing else matters until it ships.
## Charter                                    [triforce]
```
