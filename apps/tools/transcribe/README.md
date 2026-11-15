# tool-transcribe

Transcribe attached audio and video files with local speech-to-text models and
optional local Ollama vision scene analysis. The tool accepts attached files
only and refuses bare file paths in messages.

Use `ar3 tool transcribe` from a terminal. Use `tell --attach` from another
node. See [Tool nodes](../../../docs/a8s-tools.md).

## Needs

Install AR3. On first run, the tool installs the required Python speech package
into `~/.local/share/ar3/deps` through `ar3 deps`:

- `transcribe-fast` (`faster-whisper`): default on Linux and Windows (no PyTorch).
- `transcribe-mlx` (`mlx-whisper`, `parakeet-mlx`): default on Apple Silicon macOS.
- `transcribe-whisper` (`openai-whisper`): optional PyTorch Whisper engine.

Install `ffmpeg` and `ffprobe` for non-`faster-whisper` engines and video
inspection. For `--flavor fancy`, run Ollama (`ollama serve`) with a vision
model such as `qwen3.6`.

## Register

```bash
a8s add scribe ~/nodes/scribe tool-transcribe --ALLOW=operator,assistant
a8s start scribe
```

`ALLOW` is required. If `ALLOW` is missing, a8s stops the wake before the tool
runs and drops the message. A sender outside `ALLOW` gets no reply.

## Usage

Attach one or more audio or video files with `--attach`. Pass optional flags in
the message body to override engine, model, flavor, hints, or replacements.

From another node:

```bash
tell --attach memo.m4a scribe ""
tell --attach clip.mp4 -- scribe --flavor plain
tell --attach clip.mp4 -- scribe --flavor fancy --vision-model qwen3.6
tell --attach memo.m4a scribe - <<'EOF'
--language en --hint "r4t, a8s"
EOF
tell scribe - <<'EOF'
--save --hint "k7e, a8s" --replace "Gerry => Jerry"
EOF
```

On the node's machine:

```bash
ar3 tool transcribe --node scribe doctor
ar3 tool transcribe --node scribe hint add "k7e, a8s"
ar3 tool transcribe --node scribe hint list
ar3 tool transcribe --node scribe replace set "\\bJerry\\b" "Gerry"
ar3 tool transcribe --node scribe replace list
ar3 tool transcribe --node scribe --attach memo.m4a
ar3 tool transcribe --node scribe --attach memo.m4a -- --language en
```

### Commands

| Message | Result |
|---|---|
| `[flags]` (with `--attach`) | Transcribes each attached audio or video file. Bare file paths in the message text are refused. |
| `--save --hint ...` / `--save --replace ...` | Saves hints to `hints.txt` and replacement rules to `replacements.txt` in the node's working directory. |
| `help` | Returns this usage text. Also accepts `/help`, `--help`, and `/?` on a single line. |

Message flags:

- `--engine auto|mlx-whisper|faster-whisper|parakeet-mlx|whisper`
- `--model <name>` (`tiny`, `base`, `small`, `medium`, `large`, `large-v2`, `large-v3`, `turbo`, or `org/repo`)
- `--language <code>` (skip language auto-detection on Whisper engines)
- `--flavor auto|plain|fancy` (`auto` uses `fancy` when `ffprobe` finds a video stream, otherwise `plain`)
- `--vision-model <name>` (Ollama vision model for `fancy`, default `qwen3.6`)
- `--hint "<terms>"` (repeatable; biases Whisper `initial_prompt`)
- `--replace "<regex> => <correction>"` (repeatable; annotates matches in place)
- `--save` (persists `--hint` and `--replace` entries to the working directory)
- `--condition-on-previous` (enables Whisper cross-window text conditioning)

### Reply

The reply contains the transcript text (or the multi-scene narrative when
`fancy` flavor runs). Replacement matches appear inline as
`<match> (possible transcribe error, might be '<correction>')` so downstream
readers see both the raw hearing and the suggested term.

Transcripts larger than `INLINE_MAX` bytes attach `transcript.txt` and show the
opening text inline.

### Options

Set an option at registration with `a8s add ... --KEY=VALUE`. Change a
registered option with `a8s vars <node> set KEY VALUE`. Pass `--var KEY=VALUE`
before `--` for one local run.

| Variable | Default | Function |
|---|---|---|
| `ALLOW` | Required | Lists permitted sender names separated by commas or spaces. |
| `ENGINE` | `auto` | Selects `auto`, `mlx-whisper`, `faster-whisper`, `parakeet-mlx`, or `whisper`. |
| `MODEL` | `turbo` | Sets the default speech model name or Hugging Face repo ID. |
| `LANGUAGE` | Auto-detect | Sets a fixed language code such as `en`. |
| `FLAVOR` | `auto` | Selects `auto`, `plain`, or `fancy`. |
| `VISION_MODEL` | `qwen3.6` | Selects the Ollama vision model for `fancy` video transcription. |
| `OLLAMA_URL` | `http://localhost:11434` | Sets the Ollama server URL. |
| `HINTS` | Empty | Adds comma-separated hint terms to every run alongside `hints.txt`. |
| `TIMEOUT` | `1200` | Sets the per-file timeout in seconds. |
| `INLINE_MAX` | `16384` | Sets the inline byte limit before attaching `transcript.txt`. |

```bash
a8s vars scribe set ENGINE faster-whisper
a8s vars scribe set LANGUAGE en
```

## State

All tool configuration lives directly in the node's working directory and moves
with that directory:

- `hints.txt`: one hint phrase per line (`#` comments allowed).
- `replacements.txt`: one `<regex> => <correction>` rule per line (`#` comments allowed).

Speech packages install under `~/.local/share/ar3/deps`.

## Platforms

The tool runs on macOS, Linux, and Windows. `auto` selects `mlx-whisper` on
Apple Silicon macOS and `faster-whisper` on other platforms. Subprocesses run
in isolated process groups and stop on timeout or `a8s stop`.

## Try it by hand

```bash
ar3 tool transcribe doctor
ar3 tool transcribe --attach memo.m4a -- --flavor plain
ar3 tool transcribe -- help
ar3 tool transcribe --help
```

## Errors

| Condition | Action |
|---|---|
| Bare path in message | Attach the file with `tell --attach <path>` or `ar3 tool transcribe --attach <path>`. |
| `ffmpeg` or `ffprobe` missing | Install `ffmpeg` or pass `--flavor plain --engine faster-whisper`. |
| Ollama not reachable | Start `ollama serve` and pull a vision model (`ollama pull qwen3.6`), or pass `--flavor plain`. |

## Trust

The node reads only files delivered as `a8s` attachments and writes only
`hints.txt` and `replacements.txt` in its working directory. Permitted senders
can save hints and replacement rules when they pass `--save`.
