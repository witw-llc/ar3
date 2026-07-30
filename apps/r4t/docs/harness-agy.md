# The agy harness: capabilities, sandbox, and `tell`

Findings dated 2026-07-14. agy version 1.1.2 on macOS (`sandbox-exec`).

`agy` is Antigravity, Google's headless coding-agent CLI. r4t drives it through
the `agy` preset in [`rig.py`](../rig.py). This note maps what agy can and
cannot do under its flags — chiefly `--sandbox` — because the sandbox silently
breaks `tell`, and that surprised a live org before it was understood.

## How r4t invokes agy

Every turn, `dispatch.py` runs the rig's argv with:

- **CWD** = the workplace repo (`ctx.workplace`), the tree the member edits and
  commits in.
- **`TELL_OUTBOX_DIR`** = a per-turn staging dir under the a8s node root,
  `~/.config/r4t/teams/<node>/agents/<member>/staging/`. `tell` writes its
  envelope JSON there; dispatch reads it back, applies quota and thread rules,
  then releases it to the real outbox. The staging dir is **outside the
  workplace repo**.

`tell` (via `apps/a8s/tell.py`) finds its outbox **only** from
`$TELL_OUTBOX_DIR` — it no longer walks up from CWD. On each send it does
`mkdir -p $TELL_OUTBOX_DIR` and writes a probe file there; if that write
fails it prints `cannot send from this directory` /
`TELL_OUTBOX_DIR is set but outbox is unavailable` and exits 1.

## What `--sandbox` does

Marketing calls it "terminal restrictions." Empirically, on macOS it wraps the
agent's **child processes** (the `run_command` terminal tool) in a
`sandbox-exec` profile. It does **not** sandbox agy's own process, so agy still
reaches its model API. The confinement is filesystem-write only, and it is
**soft**: the terminal tool exposes a `BypassSandbox` parameter the model can
set to escape it.

Writable set under `--sandbox` = the workspace (CWD) + any `--add-dir` path +
essential system locations. Everything else is read-only.

## Capability matrix

Cells verified with `agy --model "Gemini 3.5 Flash (Low)" ... --print`,
`--mode accept-edits`, headless. "workspace" = CWD or an `--add-dir` path.

| Capability | `--sandbox` | no `--sandbox` | Notes |
|---|---|---|---|
| Run an arbitrary CLI (exec) | allowed | allowed | Exec itself is never blocked; only the child's writes are. |
| Write inside workspace (absolute path) | allowed, persists to real FS | allowed | |
| Write outside the writable set | **blocked** — `operation not permitted` | allowed (normal OS perms) | |
| Relative-path write | lands in agy's own scratch overlay, not CWD | same | The terminal tool does not run in the `--print` CWD; **use absolute paths**. |
| Network egress (HTTPS `curl`) | **allowed** | allowed | Contradicts the "network isolation" marketing; external egress works on macOS. |
| Env vars visible to child | yes | yes | `$TELL_OUTBOX_DIR` passes through intact. |
| `tell` to `$TELL_OUTBOX_DIR` outside workspace | **blocked** (`outbox is unavailable`) | allowed | Root cause of the incident. |
| `tell` with `--add-dir <staging>` | allowed | n/a | Surgical fix; requires the runtime staging path. |
| `BypassSandbox: true` (tool param) | honored in headless `--mode accept-edits` | n/a | This is what "bypass the sandbox" means (see case study). |

## The `tell` interaction

Under `--sandbox`, `tell`'s outbox is `$TELL_OUTBOX_DIR` — the staging dir
outside the workplace repo. `tell`'s probe write into it is a write outside the
writable set, so the sandbox denies it and `tell` reports the outbox
unavailable. Env passthrough is fine; the block is purely the filesystem write
target being outside CWD.

Two things make it work:

1. **Drop `--sandbox`** (what r4t now does). agy runs with normal OS
   permissions, like every other r4t preset (`claude`, `codex`, `cursor`,
   `opencode`, `copilot` carry no sandbox). `tell` writes the staging dir
   freely.
2. **Keep `--sandbox`, add `--add-dir <staging>`.** Preserves confinement but
   requires dispatch to inject the per-turn staging path into the argv, since
   the path is per-node/per-member. Not currently wired.

## Update: headless command permissions (2026-07-16, agy 1.1.3)

agy 1.1.3 introduced a new permission model, `toolPermission=request-review`.
In headless `--print` runs any tool needing the **"command"** permission is
**auto-denied** — headless mode has no way to surface the review prompt, so the
turn produces only the denial text (`a tool required the "command" permission
that headless mode cannot prompt for, so it was auto-denied`). Crucially,
`--mode accept-edits` covers *file edits* but **not commands**: a member that
runs `tell` or `git` now stalls even though it never touches `--sandbox`.

The r4t `agy` preset therefore now passes **`--dangerously-skip-permissions`**,
matching what `apps/a8s/definitions/agy.json` already does. This is consistent
with r4t doctrine (ISOLATE-SPEC): the security line is the **OS isolation
boundary**, not the harness's own sandbox/permission layer, which r4t already
trusts for its peers.

The narrow alternative is per-target allow rules —
`command(<target>)` entries under `permissions.allow` in
`~/.gemini/antigravity-cli/settings.json`. That works for a rig that only ever
runs a fixed, known command set, but it is impractical for roster members that
need broad shell access (arbitrary `git`, `tell`, build tooling), so r4t skips
permissions wholesale instead.

## Recommended invocations per role

- **A rig that must `tell`** (any r4t member): no `--sandbox`. Keep
  `--mode accept-edits` (so headless file edits don't stall on the review
  prompt) and `--print`.
- **A rig that only edits a repo and never messages**: `--sandbox --mode
  accept-edits --print` is safe, but note the sandbox is soft (`BypassSandbox`)
  and does not block network. It buys little over trusting the rig, which r4t
  already does for its peers.

## Quota notes

agy shares the owner's real subscription across **two quotas** — a larger
Gemini quota and a smaller Anthropic (Claude models) quota. The model list is
**live**: `agy models` display names drift across releases, so r4t stores the
friendly `--model` string and re-resolves it against `agy models` before every
turn (`resolve_agy_model` in `rig.py`). Prefer Gemini models for routine work;
reserve Claude models for turns that need them.

## Incident case study (2026-07-14, org d5n, milestone M4)

Members Vela and Faye (both on the `agy` preset, then `--sandbox --mode
accept-edits --print`) could not `tell`. Both hit `cannot send from this
directory` / `TELL_OUTBOX_DIR is set but outbox is unavailable`. The org
survived because dispatch's **STDOUT-REPLY fallback** stages a member's cleaned
stdout as one reply when the turn releases no envelope — so their prose reports
still reached their recipients, just downgraded to a single fallback message.

Vela then reported she "bypassed the sandbox to write to the outbox." What
actually happened: agy's terminal tool, on the first denied write, hints the
model to retry with `BypassSandbox: true`; in headless `--mode accept-edits`
that retry is honored (no `--dangerously-skip-permissions` required). So the
"bypass" was a real agy tool feature, not a workaround she invented — but it is
model-discretionary and unreliable as a transport. The fix is to not sandbox a
rig that needs `tell` in the first place.

Related: the a8s definition `apps/a8s/definitions/agy.json` invokes `--sandbox
--dangerously-skip-permissions`. That combination is a documented agy issue
(the skip-permissions flag auto-approves the sandbox bypass, defeating it); it
happens to let `tell` through but relies on the very bypass this note advises
against depending on. Worth revisiting separately.
