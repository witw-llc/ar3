# Private memory for engine agents

Enable K7E for a named agent without a roster:

```bash
r4t engine claude run --agent wren --dir ~/work/project --memory on "Do the work"
```

For a bundled a8s engine node:

```bash
a8s vars wren set MEMORY on
```

The setting reaches message, batch, and idle wakes. `r4t rig run` accepts the
same memory options. Unset or `off` leaves memory disabled. `on` uses the
engine's existing knowledge tier; `small`, `medium`, and `large` select 4096,
8192, and 32768 bytes. These are byte budgets, not token counts.

## An agent owns its knowledge

Each named `--agent` has a stable store under
`$R4T_HOME/engine-memory/<name>-<identity hash>/`. Names are case-insensitive.
Changing engines, models, or working directories keeps the same store.
Different names have different stores. Without `--agent`, the canonical
working directory determines the store instead. On an a8s wake the node's
own name is the identity, so a message addressed to an alias or to
`node:member` reaches the same store as the node's batch and idle wakes.

The diagnostic on stderr prints the actual home. Use that path with
`K7E_HOME=<home> k7e list --limit 10`, `k7e status`, or `k7e check` to inspect it.

Copy the whole store directory to carry an agent's knowledge to another
machine, then select it explicitly:

```bash
r4t engine codex run --agent wren --memory on --memory-home ~/memory/wren "Continue"
```

`MEMORY_HOME` supplies the same override to an a8s node. The directory's
`identity.json` binds it to its agent; another named agent cannot adopt it
silently. Copy only while the worker is stopped and no turn is writing.
Pending jobs retain their writer selection, so that writer and any named rig
configuration must be available at the destination too.

Store separation limits what r4t retrieves. It is not an access boundary
between processes using the same OS account. OS isolation remains necessary
where agents must be prevented from reading each other's files.

## What happens around a turn

Before execution, r4t searches with the newest message and packs active,
age-stamped recollections. Retrieval uses keyword search plus local embeddings
when available, without LLM synthesis or reranking. Small and medium budgets
retrieve eight candidates; large retrieves 32. Exact intermediate byte
budgets used by roster members retrieve 16 above 8 KiB and below 32 KiB.
The framing and snippets together fit the budget. Only packed entries count
as uses.
The [evaluation record](r4t-memory-evaluation.md) documents the depth/query
comparison, live engine proof, and writer limitations.

After execution, a capture records the original input, output, recalled IDs,
sender attribution, and exit code. Successful turns enter a durable queue.
Failed and timed-out turns remain diagnostic captures and are not distilled.
Memory text inserted into the prompt is not part of the engine capture's
extraction input. STATUS.md and LESSONS.md retain their working-state roles.
`--no-scaffold` disables that scaffold independently of memory.

A bounded background worker drains the queue without holding the foreground
turn open. It defaults to the selected engine/model/effort, without the
agent's session continuation or memory hooks. This spends additional model
calls asynchronously. For a stronger writer, select a configured rig:

```bash
r4t engine opencode run --agent wren --memory small --memory-writer curator "Do the work"
a8s vars wren set MEMORY_WRITER curator
```

The worker journals extraction decisions and individual writes so interruption
can resume without appending completed changes twice. It runs at most five
captures or four minutes per pass, with a three-minute cap per capture, then
hands remaining work to another bounded worker. One worker per store drains
the queue; a store write lock also coordinates ordinary K7E writers.

Failure leaves the capture pending. A later invocation or a8s idle wake
retries it, even when the engine's idle latch suppresses a model turn.
After three failed attempts its queue marker moves to `queue/failed/`, the
capture stays in `turns/`, and the worker continues with the next capture.
`failure.json` and `worker.log` explain failures; the next turn reports a
pending failure on stderr. An unavailable memory service does not reverse a
completed engine turn's success. Engine stdout remains the engine's output.

## Conversation corrects recalled facts

Direct CLI input is operator input. For routed a8s traffic, explicitly name
the senders whose corrections can retire recalled facts:

```bash
a8s vars wren set MEMORY_PEOPLE operator-phone,operator-email
```

`--memory-people` is the CLI equivalent. Without that setting, routed turns
still learn ordinary facts but do not grant correction authority to any
sender. a8s passes the actual envelope paths separately from prompt prose;
batched messages retain individual senders. A quoted sender name or forged
human-message heading inside message text grants no authority, and neither
does automated traffic (`meta.class` of `auto`) from a named sender. This uses
a8s's sender attribution, not cryptographic sender authentication.

Correction is asynchronous and covers the memories recalled into that turn.
Once its capture is distilled, the replacement is active and the old entry
is retired. This is not a scan for every contradiction elsewhere in the store.
For immediate manual correction, use `k7e supersede`. Verify the stored
status and replacement rather than relying on an agent's acknowledgment.

## Embeddings and inspection

On Windows, use a native engine executable. Memory-enabled execution and its
writer refuse multiline arguments through `.cmd`/`.bat` launchers, which
`cmd.exe` truncates at the first newline. npm-installed engine shims therefore
need a native invocation before they can carry memory safely. This is the
existing launcher boundary tracked in #230; memory-off execution is unchanged.

Keyword retrieval works without Ollama. To add semantic retrieval, run
`ollama pull nomic-embed-text` with Ollama available. To choose keyword-only
operation deliberately, set `K7E_HOME` to the agent's store and run
`k7e config embeddings off`. `k7e status` explains missing capabilities.

The queue, captures and logs remain local. Distillation sends the selected
capture to the configured writer, just as an engine turn sends its prompt to
that engine. Memory tools/MCP and shared memory tiers are separate features.
