# Engine memory validation — 2026-09-18

## Retrieval comparison

The fresh comparison uses the frozen K3 store (31 notes, 30 queries) and K6
LongMemEval subset (50 questions, 48 with scored evidence). It compares
candidate depths 8/16/32, message-only and expanded queries, FTS-only and
hybrid search, and budgets of 4096/8192/32768 bytes: 2808 scored rows.
Hybrid uses local `nomic-embed-text:latest`; all measured query embeddings
succeeded. No generative reranker or answer model runs.

K6 message-only results below count questions whose **entire scored evidence**
survives packing. Retrieved means all evidence entries reached the candidate
pool, before packing. Each denominator is 48.

| Track | Depth | Retrieved | 4 KiB covered | 8 KiB covered | 32 KiB covered | Median search ms | Mean 32 KiB bytes |
|---|---:|---:|---:|---:|---:|---:|---:|
| FTS | 8 | 41 | 27 | 39 | 41 | 4.61 | 18,260 |
| FTS | 16 | 45 | 24 | 38 | 45 | 4.96 | 30,127 |
| FTS | 32 | 46 | 22 | 35 | 45 | 5.59 | 29,899 |
| Hybrid | 8 | 41 | 25 | 39 | 41 | 27.80 | 18,246 |
| Hybrid | 16 | 45 | 24 | 35 | 45 | 27.45 | 31,052 |
| Hybrid | 32 | 47 | 24 | 34 | 46 | 27.38 | 31,798 |

Depth eight protects small budgets from dividing their bytes among too many
notes. Depth 32 makes the large tier useful: hybrid covers 46/48 rather than
41/48. Within K6's 12 multi-session questions, hybrid large coverage rises
from 6 to 10; all-evidence retrieval rises from 6 to 11.

The expanded query prepends a fixed identity/mission-shaped prefix:
`wren research assistant Answer questions from prior conversations.` It is
a representative expansion, not a replay of every production mission.
At the chosen depths, K6 hybrid message-only covers 25/39/46 questions across
the three budgets; expanded covers 21/35/47. K3 hybrid message-only covers
28/28/29 of 30, versus expanded 26/27/28. FTS K3 covers 18/18/19 either way.
The extra large-budget K6 hit does not justify the expansion's losses at the
smaller tiers and on K3.

Defaults therefore use the newest message, depth eight through 8 KiB and
depth 32 from 32 KiB. Exact roster budgets between those sizes use depth 16;
that interpolation is a policy choice, not a separately measured optimum.

The experiment includes framing bytes. It measures evidence coverage rather
than answer accuracy and seeds raw turn-pair notes using the existing packing
fixture builder, bypassing distillation. Notes are freshly dated. Search
latency is an in-process measurement including query embedding, excluding CLI
startup, fetching and packing. Production diagnostics separately report
whole retrieval latency. The frozen subset is small and has no claim to
represent every agent workload. These are new measurements; the wiki's K3,
packing, age and poisoning experiments remain historical supporting evidence.

Reproduce from the repository root with Ollama and the fixture dataset
available:

```bash
python3 apps/r4t/experiments/k-retrieval-depth/evaluate.py \
  --dataset /path/to/longmemeval_s_cleaned.json --out /tmp/k-depth
```

The committed `apps/r4t/experiments/k-retrieval-depth/results.json` contains
the measured rows and model metadata without conversation text. The runner
writes `rows.json` and grouped `summary.json`.

## Live engine proof

Fresh standalone invocations used a disposable store with embeddings disabled
and automatic background writers. Claude learned a synthetic staging decision
(`cedar-742`, region `west-2`). AGY then recalled it with the same named agent.
An operator correction through Claude changed the target to `amber-319`.
The next Claude invocation recalled the replacement. A second named agent
on the same working directory had an empty store and answered `UNKNOWN`.

Verification inspected the capture IDs, echoed injected prompts and Markdown:
the initial recall and correction carried entry 1; the final recall carried
only entry 2. Entry 1 was `superseded` with its replacement pointer to entry 2.
The replacement describes the old target as retired, so the old token can
occur as history without injecting the retired entry as current evidence.

An earlier AGY-first trial returned a valid empty extraction for the teaching
turn. Its later correction became the first stored fact, so that trial does
not prove supersession. Writer recall is probabilistic; explicitly taught
facts can be omitted. A stronger writer override is available, and verification
must inspect stored state. A live Codex attempt failed before model execution
because the installed CLI did not support its configured model; no Codex live
success is claimed. Its failed capture was not queued.

## Deterministic coverage

Process tests exercise real K7E storage and the worker with scripted engine
outputs: automatic formation, fresh CLI recall across engine names, correction,
separate agents, directory changes and copied homes, failed turns, retry,
worker death while its distillation child remains alive, and idempotent replay.
Additional tests cover simultaneous storage writers, a crash between Markdown
and index updates, immutable job IDs, streaming/timeout behavior, source-rig
environment, routed batch authority and idle processing behind a latched turn.
All bundled engine definitions pass memory options on message/batch/idle wakes.

Native Windows CI runs the process and integrity tests using generated native
test executables. A `.cmd` fixture exposed the existing #230 multiline
truncation: the first header arrived and the memory and message disappeared.
The memory execution/writer paths refuse that unsafe launcher combination;
no executable launcher ships with this release. The broader roster
OS-isolation boundary remains a separate Linux integration check. Logical
store separation does not provide filesystem access control.
