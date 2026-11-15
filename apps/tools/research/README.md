# tool-research

Run deep web research through the OpenAI Responses API (`gpt-5.6-sol` for
research and composition, `gpt-5.6-luna` for Stage 0 decomposition and citation
checks) using Python's standard library only.

Use `ar3 tool research` from a terminal. Use `tell` from another node.
See [Tool nodes](../../../docs/a8s-tools.md).

## Needs

Install AR3 and provide an `OPENAI_API_KEY` stored in the node's `secrets.json`
(or set in `OPENAI_API_KEY` in the node's environment). No third-party Python
packages are required.

## Register

```bash
a8s add scholar ~/nodes/scholar tool-research --ALLOW=operator,assistant
printf '%s\n' "$OPENAI_API_KEY" | ar3 tool research --node scholar secret set OPENAI_API_KEY -
a8s start scholar
```

`ALLOW` is required. If `ALLOW` is missing, a8s stops the wake before the tool
runs and drops the message. A sender outside `ALLOW` gets no reply.

## Usage

Send a research question to run a single-shot background job, or prefix the
first line with `--fanout` to run multi-angle parallel research and three-pass
synthesis.

From another node:

```bash
tell scholar Compare SQLite WAL and Litestream replication trade-offs
tell -- scholar --fanout Evaluate eBPF observability overhead on Linux 6.x kernels
tell -- scholar --fanout 4 --plan-only Compare PostgreSQL logical replication failure modes
tell -- scholar --resume --fanout 4 Compare PostgreSQL logical replication failure modes
tell --attach brief.md -- scholar --fanout 3 Verify the claims in the attached brief
```

On the node's machine:

```bash
printf '%s\n' "$OPENAI_API_KEY" | ar3 tool research --node scholar secret set OPENAI_API_KEY -
ar3 tool research --node scholar secret list
ar3 tool research --node scholar secret rm OPENAI_API_KEY
ar3 tool research --node scholar resume --list
ar3 tool research --node scholar resume <checkpoint-id>
ar3 tool research --node scholar idle
ar3 tool research --node scholar -- --fanout 2 Compare Raft and Multi-Paxos
```

### Commands

| Message | Result |
|---|---|
| `<question>` | Runs one background web-search research job and replies with a cited markdown report. |
| `--fanout <question>` | Runs Stage 0 (`gpt-5.6-luna`) to produce at least 12 distinct angles plus a reserved adversarial slot, picks `N` (`1`..`8`), selects `N - 1` angles by MMR (`lambda=0`), appends the adversarial angle, runs parallel jobs, and merges them in three passes. |
| `--fanout N <question>` | Runs fanout width `N` (`1`..`8`). When a straggler times out after `ceil(0.67 * N)` jobs succeed, merges completed reports and lists missing angles under `## Missing sub-questions`. |
| `--fanout [N] --plan-only <question>` | Runs Stage 0 decomposition and MMR selection and returns the markdown plan without submitting research jobs. |
| `--resume [--fanout [N]] <question>` | Resumes an interrupted single-shot or fanout checkpoint in `.research/` (or pass `--resume=<checkpoint-id>`). Fails closed if no saved checkpoint matches. |
| `help` | Replies with this usage text. Also accepts `/help`, `--help`, and `/?` on a single line. |

Additional first-line flags:

- `--no-cache` — Ignores cached `.research/` artifacts for the question and starts a fresh run.
- `--resume` / `--resume=<checkpoint-id>` — Resumes a saved checkpoint in `.research/` without starting an unplanned fresh run.
- `--json` — Replies with structured JSON instead of markdown prose.

### Reply

Replies return markdown inline up to `INLINE_MAX` bytes. Larger reports show the
opening text inline and attach `report.md` (plus `report-01.md` .. `report-NN.md`
on fanout runs). Any secret stored in `secrets.json` (or `OPENAI_API_KEY` in the
environment) is scrubbed before delivery.

### Options

Set an option at registration with `a8s add ... --KEY=VALUE`. Change a
registered option with `a8s vars <node> set KEY VALUE`. Pass `--var KEY=VALUE`
before `--` for one local run.

| Variable | Default | Function |
|---|---|---|
| `ALLOW` | Required | Lists permitted sender names separated by commas or spaces. Names are not case-sensitive. |
| `MODEL` | `gpt-5.6-sol` | Selects the research and composition model. |
| `PLANNER_MODEL` | `gpt-5.6-luna` | Selects the Stage 0 decomposition and citation-check model. |
| `FANOUT` | Single-shot | Sets the default fanout (`1`..`8`, or `auto`). |
| `JOB_TIMEOUT` | `1200` | Sets the per-job background timeout in seconds. |
| `INLINE_MAX` | `16384` | Sets the inline byte limit before attaching `report.md`. |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Sets the OpenAI Responses API base URL. |

## State

All tool state lives in the node's working directory and moves with that
directory:

- `secrets.json` (`0600` on POSIX): stores API keys such as `OPENAI_API_KEY`.
- `.research/`: stores per-run checkpoints (`manifest.json`, `job-<i>.json`, `job-<i>-result.json`, `report-NN.md`, `merged.md`) written immediately as each background job finishes.
- `.research/pending/`: holds finished replies and attachment copies when `tell` fails transiently, retrying on the next wake or `idle` hook.

## Platforms

The tool runs on macOS, Linux, and Windows with Python's standard library. On
POSIX hosts, `secrets.json` is written with mode `0600` and refused if readable
by group or other users.

## Try it by hand

```bash
ar3 tool research -- help
ar3 tool research -- --fanout --plan-only Compare SQLite WAL and rollback journal
ar3 tool research --help
```

## Errors

| Condition | Action |
|---|---|
| `OPENAI_API_KEY is not set` | Run `ar3 tool research --node scholar secret set OPENAI_API_KEY -` and pipe the key on stdin. |
| `secrets.json` open to group or other users | Run `chmod 600 <workdir>/secrets.json`. |
| Quorum not reached | Run `--resume` with the same message (or `ar3 tool research --node scholar resume <checkpoint-id>`) to resume completed sub-jobs from `.research/`. |

## Trust

Permitted senders spend OpenAI API quota configured for the node and write
cached research artifacts under `.research/`. Keep `ALLOW` restricted to trusted
callers. Senders never read `secrets.json`; secret management is local-only via
`ar3 tool research ... secret`.
