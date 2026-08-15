# r4t harness CLI landscape

Tracking page for AI coding-agent CLIs that could become r4t rig
presets. Not a commitment to support any of them. Preset inventory
below is taken from `HARNESS_PRESETS` in
[`apps/r4t/rig.py`](../apps/r4t/rig.py). Candidate facts were verified
against vendor docs and public CLI references on **2026-07-31**
(starting from the 2026-07-28 survey in issue #18). Where a claim
could not be confirmed, the page says so.

## Viability criteria

A harness is a viable r4t rig only if it can do all of:

1. **Subprocess-per-turn** — prompt in argv (or equivalent one-shot
   flag), assistant output on stdout, process exits when the turn
   ends.
2. **Unattended permissions** — a flag or env that auto-approves tool
   use so the turn never blocks on a TTY prompt.
3. **Model selection** — a CLI flag, env, or documented product mode
   that pins which model runs the turn.
4. **Sane continuation** — if the roster enables `- **Continue:**`,
   resume must be pinnable per member. Per-directory or per-project
   session stores are fine. Machine-global or cloud-synced
   continuation that crosses directories is nuke-class: it disqualifies
   continue support until a session id (or equivalent) can be pinned
   cleanly (the lesson from bin#256 / Copilot).

Judge a new harness against these four. Capability breadth alone is
not enough.

## Adoption trigger

A candidate graduates from this list to a preset PR when a **concrete
seat** wants it — a roster member that needs that CLI. Each adoption
runs the live verification matrix: plant/resume codeword, cold
behavior, cross-directory scope probe.

## Presets r4t supports today

| Preset | Headless shape | Continue | MCP idiom | Notes |
|---|---|---|---|---|
| `claude` | `claude … -p {prompt}` with `--permission-mode dontAsk` | `--continue` | `claude-flag` | |
| `codex` | `codex exec --sandbox workspace-write … {prompt}` | `resume --last` after `exec` | `codex-config` | Optional `[SESSION_ID]` is the bin#256 pin path |
| `cursor` | `agent -p --trust --force --approve-mcps {prompt}` | `--continue` | `cursor-file` (opt-in) | Default model pinned to `auto` |
| `opencode` | `opencode run --auto --dir {workdir} {prompt}` | `--continue` | `opencode-env` | `{workdir}` is absolute (bin#273) |
| `ollama-opencode` | `ollama launch opencode --model … -- run --auto --dir {workdir}` | `--continue` | `opencode-env` | Requires `--model` |
| `ollama-claude` | `ollama launch claude --model … -y -- … -p` | no | `claude-flag` | Requires `--model` |
| `ollama-codex` | `ollama launch codex --model … -y -- exec --sandbox workspace-write` | no | `codex-config` | Requires `--model` |
| `copilot` | `copilot --allow-all-tools -p {prompt}` | **no** | `copilot-flag` | `--continue` is machine-global; `--session-id <id>` at creation and `-r, --resume=<id>` under `-p` are the pin path (#17) |
| `ollama-copilot` | `ollama launch copilot --model … -y -- … -p` | no | `copilot-flag` | Requires `--model` |
| `agy` | `agy --dangerously-skip-permissions --mode accept-edits --print` | `--continue` | none | MCP only from `~/.gemini`; no `--sandbox` — see [r4t-harness-agy.md](r4t-harness-agy.md) |
| `ollama` | `ollama run {model} {prompt}` | no | none | No tools; stdout-fallback replies |

`r4t rig presets` marks which of these declare `continue_argv`. Deep
notes for agy and the `ollama launch` wrappers:
[r4t-harness-agy.md](r4t-harness-agy.md) ·
[r4t-harness-ollama-launch.md](r4t-harness-ollama-launch.md).

## Notable absent harnesses

### Closest to viable

| Harness | Headless invocation (verified) | Continue / scope | Model | Caveats |
|---|---|---|---|---|
| **Gemini CLI** | `gemini -p "…" --yolo --output-format json` (also `stream-json`) | `--resume` / `-r` [session]; chats under `~/.gemini/tmp/<project_hash>/` — project-scoped | `-m` / `--model` | Same family as Google’s Antigravity CLI (agy), already a preset. MCP via `mcpServers` in `~/.gemini/settings.json` or `.gemini/settings.json` |
| **Cline CLI** | `cline --yolo "…"` or `cline --json "…"` | `--continue` resumes most recent task for the **current directory**; avoid `--zen` (hub daemon path) | `-m` / `--model` | MCP in `~/.cline/data/settings/cline_mcp_settings.json` (and related `cline mcp` / project `.cline/` layouts — confirm against installed major) |
| **Qwen Code** | `qwen -p "…" --yolo --output-format stream-json` | `--continue` / `--resume <id>` — **project-scoped** JSONL under `~/.qwen/projects/<project_hash>/chats` | documented model flags in CLI help | Strongest documented continue contract in this tier. MCP: `mcpServers` in `~/.qwen/settings.json` or `.qwen/settings.json` |
| **Aider** | `aider --message "…" --yes --no-pretty --no-stream` (also `--yes-always`) | No `--continue` flag; history file `.aider.chat.history.md` (overridable via `--chat-history-file`) is naturally per-repo/cwd, and replaying it into the next turn also needs `--restore-chat-history` | `--model` | Noisy text stdout. MCP: config/`--mcp-servers` paths exist in the ecosystem — **confirm against the installed Aider version** before treating as first-class |
| **Goose** (AAIF / Linux Foundation) | `GOOSE_MODE=auto goose run -t "…"` | Named sessions: `goose run -n <name> -r -t "…"` (also `goose session --resume --name`). No uniform bare `--continue`. Session store location unverified — confirm on the installed build | `--model` / `--provider`, or `GOOSE_MODEL` / `GOOSE_PROVIDER` | Unattended mode comes from env `GOOSE_MODE=auto`; there is no `--mode` flag. MCP as “extensions” (`--with-extension`, config). Feature-detect per version — surface shifts |

### Next tier

| Harness | Notes |
|---|---|
| **Kiro CLI v2** (AWS) | `kiro-cli chat --no-interactive --trust-all-tools "…"`. Sessions **per-directory** in `~/.kiro/`; resume with `--resume` / `--resume-id`. **Pin v2 for headless:** CLI 3.0 EA drops classic/non-TUI mode and breaks the session format. Model via `/model` interactively; headless model pin — confirm `--list-models` / agent config for the pinned v2 build |
| **Amp** (Sourcegraph) | `amp --execute "…" --stream-json` (`-x`). Product modes instead of a conventional `--model` (reported as `low` / `medium` / `high` / `ultra` — **unverified; confirm the mode names on the installed build**). Threads sync to ampcode.com and are globally addressable — **continuation needs the bin#256 treatment before enabling**. MCP in settings JSON |
| **Kimi Code CLI** (Moonshot) | Unattended print path is `--print`, which implies `--afk`; `-p` alone is not AFK. Example: `kimi --print --final-message-only -p "…"`. `--continue` / `-C` is **cwd-scoped**. `-m` / `--model`. `--yolo` is orthogonal (user still reachable for questions). Print mode waits on background work up to a long ceiling before exiting (a `print_wait_ceiling_s` setting, default reported as 3600s — **unverified**) — use an external timeout. MCP: `--mcp-config` / `--mcp-config-file`, default `~/.kimi/mcp.json` |

### Watch

| Harness | Notes |
|---|---|
| **Grok Build CLI** (xAI) | `grok --no-auto-update -p "…" --always-approve --output-format json`. `--continue` is **cwd-scoped**; sessions in `~/.grok/sessions`. `-m` / `--model`. Young surface — treat as unstable |
| **Factory Droid** | `droid exec --auto high --output-format stream-json "…"`. Strong headless contract; `-s` / `--session-id` to continue; `-m` / `--model`. Enterprise-shaped — prioritize on demand |
| **Mistral Vibe** | `vibe --trust --prompt "…" --yolo --output json`. `--continue` / `-c` resumes recent session; docs say directory-scoped (skill text also mentions TTY-scoped with cwd fallback — **verify on the installed build**). Rapid release cadence |
| **Auggie CLI** (Augment) | `auggie --print --quiet "…"`. `--continue` / `-c`; `auggie session list` is workspace-aware. `--model`. Permissions via `--permission` / settings. Clean contract; smaller likely demand. MCP in `.augment` / `~/.augment` settings |
| **Pi** | `pi -p "…"` (print mode). Sessions per-cwd JSONL under `~/.pi/agent/sessions/`; `-c` / `--continue`. `--model` / `--provider`. Unattended tool policy is permission-mode / extension territory (`--approve` is **project trust**, not YOLO) — confirm a non-interactive allow-all path on the installed build |

### Poor fits (do not build)

| Harness | Why |
|---|---|
| **Charm Crush** | `crush run` is the documented one-shot path. The product is client/server-shaped. Revisit only if the print contract stays a plain local subprocess |
| **Warp Agent** | Embedded in Warp / Oz cloud harness plumbing — not a standalone argv→stdout coding CLI for r4t to spawn |
| **Kilo Code CLI** | `kilo run --auto "…"` works, but the product is server/daemon-shaped (`kilo serve`, `kilo daemon`, attach). Engine-redundant with OpenCode, which r4t already supports |
| **Devin** | Cloud delegation, not a local harness |
| **OpenHands / SWE-agent** | Container / job-shaped runners |
| **Roo Code** | Editor-first |
| **Amazon Q CLI** | Migrating to Kiro |

## Continuation support and scope

Cross-cutting view for roster `- **Continue:**` decisions. “Pin path”
means how r4t would keep two members on the same CLI from sharing one
conversation.

| CLI | Continue mechanism | Scope | Pin path | Roster continue? |
|---|---|---|---|---|
| claude (preset) | `--continue` | per-directory (CLI convention) | distinct `Workdir:` / CLI | yes |
| codex (preset) | `exec resume --last` | last **interactive** session in this cwd — cwd-filtered, and excludes `codex exec` sessions unless `--include-non-interactive` is passed | `resume <SESSION_ID>` (bin#256) | yes (`--last`) |
| cursor (preset) | `--continue` | per-directory | distinct workdirs | yes |
| opencode / ollama-opencode | `--continue` | per-directory store | distinct workdirs | yes |
| agy (preset) | `--continue` | project-associated (agy/gemini family) | distinct workdirs | yes |
| copilot (preset) | `--continue` exists | **machine-global** | `--session-id <id>` at creation, `-r, --resume=<id>` under `-p` (#17) | **no** until the pin path replaces `--continue` in the preset |
| Gemini CLI | `--resume` / `-r` | project hash under `~/.gemini/tmp/` | session id | candidate |
| Cline | `--continue` | current directory’s latest task | `--taskId` (unverified) / workdirs | candidate |
| Qwen Code | `--continue` / `--resume` | project hash under `~/.qwen/projects/` | session id | candidate (best contract) |
| Aider | history file | per-cwd / repo file | `--chat-history-file` + `--restore-chat-history` | candidate (file-based) |
| Goose | `--name` + `--resume` | named session files | required name/id | candidate (feature-detect) |
| Kiro v2 | `--resume` / `--resume-id` | per-directory DB in `~/.kiro/` | session UUID | candidate (v2 only) |
| Amp | `amp threads continue [id]` | **cloud-synced / global** | thread id + bin#256 rules | **continue off** until pinned |
| Kimi | `--continue` / `-C` | cwd | `--session` id | candidate |
| Grok Build | `--continue` / `-c` | cwd; `~/.grok/sessions` | `--session-id` / `--resume` | watch |
| Factory Droid | `--session-id` | session id (no bare `--continue`) | required id | watch |
| Mistral Vibe | `--continue` / `-c` | directory (verify TTY nuance) | `--resume` id | watch |
| Auggie | `--continue` / `-c` | workspace-aware session list | `--resume` id | watch |
| Pi | `-c` / `--continue` | per-cwd under `~/.pi/agent/sessions/` | `--session` | watch |

## MCP configuration idiom

How each CLI takes a stdio MCP server — relevant when a future preset
should expose `a8s` tell the way current MCP presets do.

| CLI / preset | Idiom | Per-invocation? | Notes |
|---|---|---|---|
| claude / ollama-claude | CLI flag (`claude-flag`) | yes | Default-on in r4t |
| codex / ollama-codex | config file (`codex-config`) | yes | Default-on |
| copilot / ollama-copilot | CLI flag (`copilot-flag`) | yes | Default-on |
| opencode / ollama-opencode | env / config (`opencode-env`) | yes | Default-on |
| cursor | `.cursor/mcp.json` in worktree (`cursor-file`) | yes | Opt-in (writes into the repo) |
| agy | `~/.gemini` settings only | **no** | Preset refuses MCP knob |
| ollama (bare) | none | no | No tools |
| Gemini CLI | `mcpServers` in user/project `settings.json`; `gemini mcp add` | file-scoped | Same family as agy’s constraint |
| Cline | `cline_mcp_settings.json` / `cline mcp` | global (isolatable via `--data-dir` / `CLINE_DIR`) | Confirm path for installed major |
| Qwen Code | `mcpServers` in `~/.qwen` or `.qwen` settings; `qwen mcp` | file-scoped | Closest to Gemini/Qwen-family |
| Aider | `mcp-servers` / related config flags | version-dependent | Verify installed release |
| Goose | extensions (`--with-extension`, config.yaml) | per-run flags available | MCP-shaped extensions |
| Kiro | MCP config + `--require-mcp-startup` | file / agent config | v3 moves more to MCP; still pin v2 for headless |
| Amp | settings `mcpServers` | file-scoped | |
| Kimi | `--mcp-config` / `--mcp-config-file`; `~/.kimi/mcp.json` | **yes** (flags) | Strong per-invocation story |
| Grok Build | not fully verified here | — | Check current docs before designing an idiom |
| Factory Droid | not fully verified here | — | |
| Mistral Vibe | project/user config (rapid churn) | — | Verify on install |
| Auggie | `.augment` / `~/.augment` settings; also `--mcp` server mode | file-scoped | |
| Pi | extension / config ecosystem | — | Confirm before promising an idiom |
