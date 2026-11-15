"""a8s wake handler and message option parser for tool-transcribe."""
from __future__ import annotations

import math
import shlex
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

import _runtime as rt

from .engine import DEFAULT_TIMEOUT, TranscribeError, transcribe_file
from .store import (
    TranscribeConfigError,
    add_hints,
    parse_cli_replacements,
    save_replacements,
    split_hints,
)

README = Path(__file__).resolve().parent / "README.md"
DEFAULT_INLINE_MAX = 16384


class TranscribeMessageError(ValueError):
    """Raised when message flags or inputs are invalid."""


def _exit_code(sent: int, *, ok: bool) -> int:
    if not rt.print_mode():
        return 0
    if sent != 0 or not ok:
        return 1
    return 0


def _parse_positive_float(raw: str | None, default: float, name: str) -> float:
    if raw is None or not str(raw).strip():
        return default
    try:
        val = float(str(raw).strip())
    except ValueError:
        raise TranscribeMessageError(f"invalid {name}: {raw!r}") from None
    if not math.isfinite(val) or val <= 0:
        raise TranscribeMessageError(f"{name} must be a positive number")
    return val


def _parse_positive_int(raw: str | None, default: int, name: str) -> int:
    if raw is None or not str(raw).strip():
        return default
    try:
        val = int(str(raw).strip())
    except ValueError:
        raise TranscribeMessageError(f"invalid {name}: {raw!r}") from None
    if val <= 0:
        raise TranscribeMessageError(f"{name} must be a positive integer")
    return val


def parse_message_options(body: str) -> dict:
    """Parse optional transcription flags from the message body; refuse bare paths."""
    opts: dict = {
        "hints": [],
        "replaces": [],
        "save": False,
        "flavor": None,
        "engine": None,
        "model": None,
        "language": None,
        "vision_model": None,
        "condition_on_previous": False,
    }
    stripped = (body or "").strip()
    if not stripped:
        return opts
    try:
        tokens = shlex.split(stripped)
    except ValueError as exc:
        raise TranscribeMessageError(f"cannot parse message options: {exc}") from None

    value_flags = {
        "--hint": "hints",
        "--hints": "hints",
        "--replace": "replaces",
        "--flavor": "flavor",
        "--engine": "engine",
        "--model": "model",
        "--language": "language",
        "--vision-model": "vision_model",
    }
    bool_flags = {
        "--save": "save",
        "--condition-on-previous": "condition_on_previous",
    }

    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if not tok.startswith("-"):
            raise TranscribeMessageError(
                "bare file paths are not accepted; attach files with --attach"
            )
        if "=" in tok:
            flag, val = tok.split("=", 1)
            if flag in bool_flags:
                raise TranscribeMessageError(f"{flag} takes no value")
            dest = value_flags.get(flag)
            if dest is None:
                raise TranscribeMessageError(f"unknown option {flag!r}")
            if dest in ("hints", "replaces"):
                opts[dest].append(val)
            else:
                opts[dest] = val
            idx += 1
            continue
        if tok in bool_flags:
            opts[bool_flags[tok]] = True
            idx += 1
            continue
        dest = value_flags.get(tok)
        if dest is None:
            raise TranscribeMessageError(f"unknown option {tok!r}")
        if idx + 1 >= len(tokens):
            raise TranscribeMessageError(f"missing value for {tok}")
        val = tokens[idx + 1]
        if dest in ("hints", "replaces"):
            opts[dest].append(val)
        else:
            opts[dest] = val
        idx += 2
    return opts


def _utf8_prefix(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def compose_transcript_reply(
    text: str,
    *,
    inline_max: int,
    work_dir: Path,
) -> tuple[str, tuple[str, ...]]:
    clean = text.strip() or "(empty transcript)"
    if len(clean.encode("utf-8")) <= inline_max:
        return clean, ()
    out_file = work_dir / "transcript.txt"
    out_file.write_text(clean + "\n", encoding="utf-8")
    head = _utf8_prefix(clean, inline_max).rstrip("\n")
    body = (
        f"(transcript over the {inline_max}-byte inline limit — full text attached as transcript.txt)\n\n"
        f"{head}"
    )
    return body, (str(out_file),)


def handle_wake(
    *,
    seat: str,
    sender: str,
    allow: str,
    message: str,
    workdir: Path | None = None,
    default_engine: str | None = None,
    default_model: str | None = None,
    default_language: str | None = None,
    default_flavor: str | None = None,
    default_vision_model: str | None = None,
    ollama_url: str | None = None,
    default_hints: str | None = None,
    timeout_raw: str | None = None,
    inline_max_raw: str | None = None,
    attachments: Sequence[str | Path] | None = None,
) -> int:
    """Process one a8s wake or `ar3 tool transcribe` invocation."""
    if not sender:
        print("tool-transcribe: senderless wake refused", file=sys.stderr)
        return 0
    if not rt.allowed(sender, allow or ""):
        print(f"tool-transcribe: {sender!r} is not in ALLOW — refused", file=sys.stderr)
        return 0

    try:
        body, verified_attachments = rt.split_verified_attachments(
            message or "", verified=attachments
        )
    except rt.AttachmentProvenanceError as exc:
        sent = rt.send_reply(sender, f"tool-transcribe: {exc}")
        return _exit_code(sent, ok=False)

    if rt.is_help(body):
        return rt.send_reply(
            sender,
            rt.help_reply(
                "tool-transcribe",
                seat,
                README,
                "apps/tools/transcribe/README.md",
            ),
        )

    wd = Path(workdir or Path.cwd()).resolve()

    try:
        timeout = _parse_positive_float(timeout_raw, DEFAULT_TIMEOUT, "TIMEOUT")
        inline_max = _parse_positive_int(inline_max_raw, DEFAULT_INLINE_MAX, "INLINE_MAX")
        opts = parse_message_options(body)
        cli_hints = split_hints(opts["hints"])
        cli_replaces = parse_cli_replacements(opts["replaces"])
    except (TranscribeMessageError, TranscribeConfigError) as exc:
        sent = rt.send_reply(sender, f"tool-transcribe: {exc}")
        return _exit_code(sent, ok=False)

    saved_notes: list[str] = []
    if opts["save"]:
        if not cli_hints and not cli_replaces:
            sent = rt.send_reply(
                sender,
                "tool-transcribe: --save requires at least one --hint or --replace",
            )
            return _exit_code(sent, ok=False)
        try:
            if cli_hints:
                added_h, skipped_h = add_hints(wd, cli_hints)
                saved_notes.append(f"saved {added_h} hint(s) ({skipped_h} existing)")
            if cli_replaces:
                added_r, updated_r = save_replacements(wd, cli_replaces)
                saved_notes.append(
                    f"saved {added_r} replacement(s) ({updated_r} updated)"
                )
        except (TranscribeConfigError, OSError) as exc:
            sent = rt.send_reply(sender, f"tool-transcribe: {exc}")
            return _exit_code(sent, ok=False)
        if not verified_attachments:
            sent = rt.send_reply(sender, "tool-transcribe: " + "; ".join(saved_notes))
            return _exit_code(sent, ok=True)

    if not verified_attachments:
        sent = rt.send_reply(
            sender,
            "tool-transcribe: attach an audio or video file with --attach",
        )
        return _exit_code(sent, ok=False)

    node_hints = split_hints([default_hints]) if default_hints else []
    combined_extra_hints = node_hints + ([] if opts["save"] else cli_hints)
    combined_extra_replaces = [] if opts["save"] else cli_replaces

    sections: list[str] = []
    try:
        for att in verified_attachments:
            att_path = Path(att)
            result = transcribe_file(
                att_path,
                workdir=wd,
                engine=opts["engine"] or default_engine,
                model=opts["model"] or default_model,
                language=opts["language"] or default_language,
                flavor=opts["flavor"] or default_flavor,
                vision_model=opts["vision_model"] or default_vision_model,
                ollama_url=ollama_url,
                extra_hints=combined_extra_hints,
                extra_replacements=combined_extra_replaces,
                condition_on_previous=bool(opts["condition_on_previous"]),
                timeout=timeout,
            )
            transcript = str(result["text"] or "").strip() or "(empty transcript)"
            if len(verified_attachments) > 1:
                sections.append(f"### {att_path.name}\n\n{transcript}")
            else:
                sections.append(transcript)
    except (TranscribeError, TranscribeConfigError, OSError) as exc:
        sent = rt.send_reply(sender, f"tool-transcribe: {exc}")
        return _exit_code(sent, ok=False)

    final_text = "\n\n".join(sections)
    if saved_notes:
        final_text = f"({'; '.join(saved_notes)})\n\n{final_text}"

    with tempfile.TemporaryDirectory(prefix="tool-transcribe-reply-") as work_dir:
        reply_body, reply_files = compose_transcript_reply(
            final_text,
            inline_max=inline_max,
            work_dir=Path(work_dir),
        )
        sent = rt.send_reply(sender, reply_body, reply_files)
        return _exit_code(sent, ok=True)
