# `ollama launch` harnesses: local models under the big-name CLIs

Findings dated 2026-07-14. ollama 0.32.0 on macOS; validated against
qwen3.6:latest (23 GB) unless noted.

`ollama launch <integration>` starts a coding-agent CLI pointed at the local
ollama server instead of its cloud backend — no env vars or config files to
hand-edit. r4t wraps it in the `claude-ollama`, `codex-ollama`, and
`copilot-ollama` presets in [`rig.py`](../rig.py), alongside the older
`opencode-ollama`. Local models mean no cloud quota, which is what makes
whole-org experiments affordable.

## How the launcher works

```
ollama launch <integration> --model <tag> -y -- <the harness's own headless argv>
```

- `--model` takes an ollama tag verbatim (`qwen3.6:latest`); the launcher
  owns model selection for the child.
- Everything after `--` is passed through to the integration untouched. Each
  r4t preset's passthrough is the parent preset's headless argv, verbatim —
  all three parents' argv worked under the launcher with no flag changes.
- `-y` auto-answers the launcher's own confirmation prompts. Required for
  unattended dispatch: a first-run or install confirmation would otherwise
  hang the turn.
- The launcher execs the integration in its own working directory, so the cwd
  r4t sets for a member's turn reaches the harness. Measured on ollama 0.32.5
  by spawning each wrapped integration with a cwd of its own and reading the
  OS-reported cwd of every descendant: launcher, harness, and the node/codex
  helpers underneath all sat in the spawn directory. That is what makes
  `Workdir:` work on the `*-ollama` presets (issue #289). A harness that reads
  something other than its cwd needs more than the hop: opencode resolves
  `--dir` against `$PWD`, so r4t pins `PWD` to the turn directory and the
  opencode presets pass the workdir absolutely (issue #273).

## What the launcher persists, per integration

Verified by checksumming every config surface before and after each launch.

| Integration | Mechanism | Persisted to the real config |
|---|---|---|
| claude | env injection (`ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`, `ANTHROPIC_DEFAULT_*_MODEL` on the child) | **Nothing.** `~/.claude/settings.json` and `settings.local.json` byte-identical; `~/.claude.json` shows only Claude Code's ordinary startup churn |
| codex | overlay file: `~/.codex/ollama-launch.config.toml` (`model_provider = ollama-launch`, `base_url = http://127.0.0.1:11434/v1/`, `wire_api = responses`) plus a `~/.codex/model.json` catalog | **Real `~/.codex/config.toml` untouched.** Trust entries codex adds under `[projects]` are codex's own `--full-auto` behavior, identical to an unwrapped `codex exec` |
| copilot | env injection (`COPILOT_MODEL` et al.) | **Nothing.** `~/.copilot/config.json` byte-identical |

`--restore` exists for integrations whose real profile the launcher rewrites —
chiefly `codex-app` (the desktop app), which gets its config regenerated and a
`~/.codex/ollama-codex-app-restore.json` restore-state saved. The three CLI
integrations above persist nothing, so there is nothing to restore
(`ollama launch <x> --restore` reports unsupported where no state exists).
Safe to run against a working install.

## Validated presets

Both turns run in a scratch git repo: a one-shot reply
("Reply with exactly the word: pong") and a tool-use turn (create `proof.txt`
with a given word, verified on disk).

| Preset | One-shot | Tool-use | Wall time per turn |
|---|---|---|---|
| `claude-ollama` | pass | pass | 92–100 s |
| `codex-ollama` | pass | pass | 10–24 s |
| `copilot-ollama` | pass | pass | 18–25 s (qwen3:1.7b) |

Oddities worth knowing:

- **codex: never pass `-m`/`--model` after the `--`.** The launcher manages
  the model and rejects the conflict
  (`conflicting extra argument: ollama launch codex manages --model`). Same
  for kimi. The preset takes `--model` on the launcher side only.
- **Claude Code is the slow one.** Its system prompt is enormous relative to a
  local model's throughput, so ~90–100 s/turn where codex takes 10–24 s on the
  same model. Budget rig timeouts accordingly.
- **Model quality is the real gate, not the plumbing.** qwen3:1.7b under
  codex emitted junk tool calls instead of "pong"; under Claude Code,
  qwen3.6 once hallucinated a mangled absolute path for `Write` (the tool ran
  fine — at the wrong path). Pick a qwen3.6-class model and keep prompts
  small: all three presets sit in the `small` text tier.
- ollama recommends raising the server context length to at least 64k tokens
  for coding tools.

## Not-installed integrations (future candidates)

From `ollama launch --help` plus web docs only — none installed, none tested.

| Integration | Headless story | Notes |
|---|---|---|
| qwen | `--prompt` non-interactive mode, stdin piping | gemini-cli lineage; closest analog to the validated three |
| kimi | `-p/--prompt`, `--print`, `--quiet` | launcher manages `--model` and `--config` (same conflict rule as codex) |
| droid | `droid exec` one-shot with tiered autonomy flags | default is read-only; needs an autonomy flag for edits |
| pool | `pool exec` non-interactive | Poolside's agent; ships its own sandbox |
| cline | `-y` yolo mode, stdin/stdout piping, `--json` | CLI 2.0 |
| pi | print mode reads stdin; RPC mode (JSON over stdio) | minimal agent by Mario Zechner |
| omp | pi derivative ("oh-my-pi"), same print/RPC surface | batteries-included pi fork |
| hermes | gateway/daemon architecture, not a one-shot CLI | Nous Research; poor fit for r4t's turn model |
| openclaw | daemon + chat-platform gateway, not a one-shot CLI | same fit problem as hermes |

`codex-app`, `hermes-desktop`, and `vscode` are GUI targets — not rig
material. The daemon-shaped ones (hermes, openclaw) would need an adapter to
fit r4t's spawn-per-turn dispatch; the exec-shaped ones (qwen, kimi, droid,
pool, cline, pi, omp) look preset-sized once installed and validated the same
way as the three above.
