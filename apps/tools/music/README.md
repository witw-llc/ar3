# tool-music

Generate instrumental music from a free-form text description with the local
ACE-Step 1.5 model, and reply to the sender with a `.wav`, `.flac`, or `.m4a`
audio attachment. The output never has vocals or lyrics.

Use `ar3 tool music` from a terminal. Use `tell` from another node.
See [Tool nodes](../../../docs/a8s-tools.md).

## Needs

Install AR3 on Python 3.11 or 3.12. The first job installs the `music-ace`
dependency group (`ar3 deps music-ace`, PyTorch and its libraries, about 7.5 GB
on Linux) into `~/.local/share/ar3/deps`. The first job also downloads:

- The ACE-Step 1.5 source at a pinned commit, checked against a pinned tree digest.
- The pinned model weights from Hugging Face, each checked against its SHA-256:
  about 6.3 GB for `turbo` (the default), 6.3 GB for `sft`, and 21.5 GB for `xl-turbo`.

The weights go to `~/.cache/ar3/music` (or `$XDG_CACHE_HOME/ar3/music`). Set
`AR3_MUSIC_CACHE` to use another directory, for example a mounted drive on a
GPU machine. Run `prepare` once to do all of this before the first message.

The engine uses a CUDA GPU when PyTorch finds one, then Apple Silicon (MPS and
MLX), then the CPU. On a 12-core CPU, a 30-second track takes about two minutes.

For `.m4a` output, macOS uses built-in `afconvert`; Linux uses `ffmpeg`. Without
an encoder, the default format is `.wav`.

## Register

```bash
a8s add music ~/nodes/music tool-music --ALLOW=operator,assistant
ar3 tool music --node music prepare
a8s start music
```

`ALLOW` is required. A sender outside `ALLOW` gets no reply. Optional node
vars: `ENGINE` (`ace`), `MODEL` (`turbo`, `sft`, `xl-turbo`), `DURATION`
(seconds), `FORMAT` (`wav`, `flac`, `m4a`), `STEPS`, and `TIMEOUT` (seconds,
default 3000).

## Usage

Describe the music in the message body: style, mood, theme, instruments, and
structure. Put options first. Text after the options (or after `--`) is the
description, at most 512 characters.

| Option | Values | Default |
|---|---|---|
| `--duration`, `-d` | 10 to 600 seconds | 60 |
| `--bpm` | 30 to 300 | chosen by the model |
| `--key` | `C major`, `F# minor`, `Am` | chosen by the model |
| `--seed` | 0 to 4294967295 | random |
| `--steps` | 1 to 200 | 8 (`turbo`, `xl-turbo`), 50 (`sft`) |
| `--format` | `wav`, `flac`, `m4a` | `m4a` with an encoder, else `wav` |
| `--model` | `turbo`, `sft`, `xl-turbo` | `turbo` |
| `--engine` | `ace` | `ace` |

The reply gives the length, model, seed, device, and time taken. Send the same
seed and description again to get the same track. The tool cuts trailing
silence, so a track can be a few seconds shorter than `--duration`.

```bash
tell music "calm lo-fi hip hop, warm Rhodes piano, vinyl crackle, mellow drums, upright bass"
tell -- music --duration 120 --bpm 96 --key "D minor" "cinematic orchestral theme, soaring strings, French horns, timpani, builds to a triumphant climax"
tell -- music --format flac --seed 42 -- "-ambient drone, slow evolving pads"
```

On the node's machine:

```bash
ar3 tool music --node music doctor
ar3 tool music --node music prepare --model sft
ar3 tool music --node music config set --duration 90 --format flac
ar3 tool music --node music config show
ar3 tool music --node music -- "--duration 30 upbeat synthwave, gated drums, arpeggiated bass"
```

## Settings

`music.json` in the node's working directory stores defaults for `engine`,
`model`, `duration`, `format`, and `steps`. A message option overrides a node
var, and a node var overrides `music.json`.

## Notes

- ACE-Step 1.5 code and weights are MIT-licensed.
- The tool never loads ACE-Step's 5 Hz language model, so it never writes lyrics
  or rewrites the description.
- Attachments sent to the node are ignored.
