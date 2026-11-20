# tool-speak

Synthesize speech from text or markdown across local engines (`say`, `pocket`,
`kokoro`) and secret-gated cloud vendors (`openai` GA Realtime, `elevenlabs`,
`cartesia`), replying to the sender with an `.m4a`, `.mp3`, or `.wav` audio
attachment.

Use `ar3 tool speak` from a terminal. Use `tell` from another node.
See [Tool nodes](../../../docs/a8s-tools.md).

## Needs

Install AR3. Local engines install on first use into `~/.local/share/ar3/deps`
through `ar3 deps`:

- `say`: built into macOS (no extra Python packages).
- `speak-pocket` (`pocket-tts==4.0.1`): cross-platform CPU neural TTS with SHA-256 verified weights.
- `speak-kokoro` (`kokoro`, `soundfile`): optional Kokoro ONNX neural TTS.

Cloud engines require the corresponding API key via
`a8s vars <node> set secret.<NAME> -` (or `ar3 tool speak --node <node> secret set <NAME> -`):

- `openai`: `OPENAI_API_KEY` (uses GA Realtime WebSocket `gpt-realtime-2.1-mini`).
- `elevenlabs`: `ELEVENLABS_API_KEY`.
- `cartesia`: `CARTESIA_API_KEY`.

For `.m4a` output (the default), macOS uses built-in `afconvert` (or `say`);
Linux and Windows use `ffmpeg`. Pass `--format wav` when neither encoder is
installed.

## Register

```bash
a8s add speak ~/nodes/speak tool-speak --ALLOW=operator,assistant
printf '%s\n' "$OPENAI_API_KEY" | a8s vars speak set secret.OPENAI_API_KEY -
ar3 tool speak --node speak config set --engine openai --voice marin
a8s start speak
```

`ALLOW` is required. If `ALLOW` is missing, a8s stops the wake before the tool
runs and drops the message. A sender outside `ALLOW` gets no reply.

## Usage

Send plain text or markdown in the message body, or attach `.md` / `.txt` files
with `tell --attach`. Markdown headings and paragraphs add automatic section
pauses, `**bold**` / `*italic*` add emphasis, fenced code blocks and tables are
skipped, and `<!-- speak: voice=... style="..." speed=... pause=... -->`
HTML comments tailor delivery per section without cluttering rendered markdown.

From another node:

```bash
tell speak "Deployment finished with zero errors."
tell speak - <<'EOF'
# Daily Summary

<!-- speak: style="warm, conversational" -->
All three test suites passed.

<!-- speak: pause=1.0 style="crisp, direct" -->
**Next step:** review the release notes.
EOF
tell -- speak --voice cedar --speed 1.05 "Quick status check."
tell --attach briefing.md speak ""
```

On the node's machine:

```bash
ar3 tool speak --node speak doctor
ar3 tool speak --node speak voices --engine openai
ar3 tool speak --node speak config set --engine openai --voice "openai:marin, pocket:alba"
ar3 tool speak --node speak config show
ar3 tool speak --node speak replace set "\\br4t\\b" "are four tee"
ar3 tool speak --node speak pronounce set "\\ba8s\\b" "eɪ ɪnfɪnɪti"
printf '%s\n' "$OPENAI_API_KEY" | ar3 tool speak --node speak secret set OPENAI_API_KEY -
ar3 tool speak --node speak -- "Hello from tool-speak."
```

### Commands

| Message | Result |
|---|---|
| `<text or markdown>` | Synthesizes speech using the node's configured engine and voice and replies with `speech.m4a` attached. |
| `[flags] [--] <text>` | Overrides `--engine`, `--voice`, `--model`, `--format`, `--speed`, `--style`, `--raw`, `--flat`, or `--no-emphasis` for this message only. |
| `[flags]` (with `--attach`) | Synthesizes attached UTF-8 text or markdown files. Bare file paths in message text are never opened from disk. |
| `help` | Replies with this usage text. Also accepts `/help`, `--help`, and `/?` on a single line. |

Message flags (when placed on the first line before the text or `--`):

- `--engine` / `--vendor` `auto|say|pocket|kokoro|openai|elevenlabs|cartesia`
- `--voice` / `-v` `<spec>` (voice ID, display name, `Male`/`Female`, or fallback stack such as `"openai:marin, pocket:alba, Samantha"`)
- `--model <name>` (`gpt-realtime-2.1-mini` for `openai`, `eleven_flash_v2_5` for `elevenlabs`, `sonic-3.6` for `cartesia`)
- `--format m4a|mp3|wav` (default `m4a`)
- `--speed` / `-s` `<float>` (default `1.0`)
- `--style` / `--instructions` `"<tone>"` (delivery tone for `openai` Realtime)
- `--raw` (speak literal characters without markdown cleanup)
- `--flat` (strip markdown syntax without section pauses or emphasis)
- `--no-emphasis` (keep section pauses while disabling inline emphasis)

### Reply

Each successful wake replies with a one-line summary naming the engine and voice
and attaches `speech.m4a` (or `speech.mp3` / `speech.wav`). Any stored API keys
are scrubbed from error messages before delivery.

### Options

Set an option at registration with `a8s add ... --KEY=VALUE`. Change a
registered option with `a8s vars <node> set KEY VALUE`, or store defaults in
`speak.json` with `ar3 tool speak --node <node> config set ...`. Pass
`--var KEY=VALUE` before `--` for one local run.

| Variable | Default | Function |
|---|---|---|
| `ALLOW` | Required | Lists permitted sender names separated by commas or spaces. |
| `ENGINE` | `auto` | Selects `auto`, `say`, `pocket`, `kokoro`, `openai`, `elevenlabs`, or `cartesia`. |
| `VOICE` | Engine default | Sets a voice ID, name, or comma-separated fallback stack (`openai:marin, pocket:alba`). |
| `MODEL` | Vendor default | Overrides the cloud vendor model (`gpt-realtime-2.1-mini` on `openai`). |
| `FORMAT` | `m4a` | Selects `m4a`, `mp3`, or `wav` attachment output. |
| `SPEED` | `1.0` | Sets the default speech rate multiplier. |
| `STYLE` | Empty | Sets default delivery instructions for `openai` Realtime. |
| `TIMEOUT` | `300` | Sets the per-wake synthesis timeout in seconds. |

## State

All tool configuration lives in the node's working directory and moves with that
directory:

- `speak.json`: saved operator defaults (`engine`, `voice`, `model`, `format`, `speed`, `style`).
- `replacements.txt`: `<regex> => <spoken>` text substitutions (`#` comments allowed).
- `pronunciations.txt`: `<regex> => <IPA>` Kokoro phoneme rules (`#` comments allowed).
- `secrets.json` (`0600` on POSIX): optional working-directory API keys alongside `a8s vars <node> set secret.<NAME>`.

Local Python speech packages install under `~/.local/share/ar3/deps`.

## Platforms

The tool runs on macOS, Linux, and Windows. When `ENGINE` is `auto` and no
vendor-qualified voice stack selects a cloud vendor, `auto` uses `say` on macOS
and `pocket` (or an already-installed `kokoro` group) on Linux and Windows.

## Try it by hand

```bash
ar3 tool speak doctor
ar3 tool speak voices --engine pocket
ar3 tool speak --var FORMAT=wav -- "Hello world."
ar3 tool speak -- help
```

## Errors

| Condition | Action |
|---|---|
| `OPENAI_API_KEY is not set` | Run `a8s vars <node> set secret.OPENAI_API_KEY -` or `ar3 tool speak --node <node> secret set OPENAI_API_KEY -`. |
| `m4a output requires afconvert or ffmpeg` | Install `ffmpeg` or set `FORMAT=wav` (`--format wav`). |
| Deprecated OpenAI TTS model (`tts-1`, `gpt-4o-mini-tts`) | Omit `MODEL` or use `gpt-realtime-2.1-mini`. |

## Trust

Permitted senders generate audio using the node's configured local or cloud TTS
engine (spending cloud API quota when a cloud engine is configured). Message
bodies are never interpreted as local filesystem paths, markdown `<!-- speak: ... -->`
directives cannot switch vendors, and senders cannot read `secrets.json` or
modify `speak.json`.
