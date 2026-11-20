"""a8s wake handler and message option parser for tool-speak."""
from __future__ import annotations

from collections.abc import Sequence
import math
from pathlib import Path
import sys
import tempfile

import _runtime as rt

from .engine import DEFAULT_FORMAT, DEFAULT_TIMEOUT, SpeakError, synthesize_to_file
from .secretstore import SecretStoreError, read_secrets, scrub_text
from .store import SpeakConfigError, validate_engine_choice, validate_format, validate_speed

README = Path(__file__).resolve().parent / "README.md"


class SpeakMessageError(ValueError):
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
        raise SpeakMessageError(f"invalid {name}: {raw!r}") from None
    if not math.isfinite(val) or val <= 0:
        raise SpeakMessageError(f"{name} must be a positive number")
    return val


def _scan_shell_token(line: str, start: int) -> tuple[str, int]:
    """Scan one POSIX-quoted token starting at `start` (after skipping spaces/tabs)."""
    n = len(line)
    i = start
    while i < n and line[i] in (" ", "\t"):
        i += 1
    if i >= n:
        raise SpeakMessageError("unexpected end of option line")
    buf: list[str] = []
    quote: str | None = None
    while i < n:
        ch = line[i]
        if quote is None:
            if ch in (" ", "\t"):
                break
            if ch in ("'", '"'):
                quote = ch
                i += 1
                continue
            if ch == "\\":
                if i + 1 >= n:
                    raise SpeakMessageError("cannot parse message options: No escaped character")
                buf.append(line[i + 1])
                i += 2
                continue
            buf.append(ch)
            i += 1
        elif quote == "'":
            if ch == "'":
                quote = None
            else:
                buf.append(ch)
            i += 1
        else:
            if ch == '"':
                quote = None
                i += 1
                continue
            if ch == "\\" and i + 1 < n and line[i + 1] in ('"', "\\", "$", "`"):
                buf.append(line[i + 1])
                i += 2
                continue
            if ch == "\\" and i + 1 >= n:
                raise SpeakMessageError("cannot parse message options: No closing quotation")
            buf.append(ch)
            i += 1
    if quote is not None:
        raise SpeakMessageError("cannot parse message options: No closing quotation")
    return "".join(buf), i


def _is_non_option_dash_token(tail: str) -> bool:
    """Return True when `tail` (starting with `-`) is `-`, `- item`, or `---` thematic break."""
    if tail == "-" or tail.startswith(("- ", "-\t")):
        return True
    if tail.startswith("---"):
        j = 3
        while j < len(tail) and tail[j] == "-":
            j += 1
        if j == len(tail) or tail[j] in (" ", "\t"):
            return True
    return False


def parse_message_body(body: str) -> dict:
    """Parse optional leading speech flags on the first line (or before `--`) and return spoken text.

    Plain text or markdown with no leading option is returned verbatim with all
    punctuation, quotes, backslashes, and newlines preserved. When the body starts
    with option tokens, only the leading option prefix is shell-parsed; everything
    after `--` or the first non-option word is kept verbatim.
    """
    opts: dict = {
        "engine": None,
        "voice": None,
        "model": None,
        "format": None,
        "speed": None,
        "style": None,
        "raw": False,
        "flat": False,
        "emphasis": True,
        "text": "",
    }
    if not body or not body.strip():
        return opts

    lstripped = body.lstrip()
    if not lstripped.startswith("-"):
        opts["text"] = body
        return opts

    lines = lstripped.split("\n", 1)
    first_line = lines[0]
    rest_lines = lines[1] if len(lines) > 1 else None
    if _is_non_option_dash_token(first_line):
        opts["text"] = body
        return opts

    value_flags = {
        "--engine": "engine",
        "--vendor": "engine",
        "--voice": "voice",
        "-v": "voice",
        "--model": "model",
        "--format": "format",
        "--speed": "speed",
        "-s": "speed",
        "--style": "style",
        "--instructions": "style",
    }
    bool_true = {
        "--raw": "raw",
        "--flat": "flat",
    }

    pos = 0
    n = len(first_line)
    consumed = 0
    remaining_first = ""
    while True:
        while pos < n and first_line[pos] in (" ", "\t"):
            pos += 1
        if pos >= n:
            remaining_first = ""
            break
        tail = first_line[pos:]
        if not tail.startswith("-"):
            remaining_first = tail
            break
        if _is_non_option_dash_token(tail):
            if consumed == 0:
                opts["text"] = body
                return opts
            remaining_first = tail
            break
        if tail == "--" or tail.startswith(("-- ", "--\t")):
            after = pos + 2
            if after < n and first_line[after] in (" ", "\t"):
                after += 1
            remaining_first = first_line[after:]
            consumed += 1
            break

        tok, next_pos = _scan_shell_token(first_line, pos)
        if "=" in tok:
            flag, val = tok.split("=", 1)
            if flag in bool_true or flag == "--no-emphasis":
                raise SpeakMessageError(f"{flag} takes no value")
            dest = value_flags.get(flag)
            if dest is None:
                raise SpeakMessageError(
                    f"unknown option {flag!r} (use `--` before literal text starting with `-`)"
                )
            opts[dest] = val
            pos = next_pos
            consumed += 1
            continue
        if tok in bool_true:
            opts[bool_true[tok]] = True
            pos = next_pos
            consumed += 1
            continue
        if tok == "--no-emphasis":
            opts["emphasis"] = False
            pos = next_pos
            consumed += 1
            continue
        dest = value_flags.get(tok)
        if dest is None:
            raise SpeakMessageError(
                f"unknown option {tok!r} (use `--` before literal text starting with `-`)"
            )
        probe = next_pos
        while probe < n and first_line[probe] in (" ", "\t"):
            probe += 1
        if probe >= n:
            raise SpeakMessageError(f"missing value for {tok}")
        val_tok, next_pos = _scan_shell_token(first_line, probe)
        opts[dest] = val_tok
        pos = next_pos
        consumed += 1

    if opts["engine"] is not None:
        opts["engine"] = validate_engine_choice(opts["engine"])
    if opts["format"] is not None:
        opts["format"] = validate_format(opts["format"])
    if opts["speed"] is not None:
        opts["speed"] = validate_speed(opts["speed"])

    if remaining_first and rest_lines is not None:
        opts["text"] = f"{remaining_first}\n{rest_lines}"
    elif remaining_first:
        opts["text"] = remaining_first
    else:
        opts["text"] = rest_lines or ""
    return opts


def _collect_sensitive(workdir: Path) -> list[str]:
    vals: list[str] = []
    try:
        for v in read_secrets(workdir).values():
            if len(v) >= 4 and v not in vals:
                vals.append(v)
    except SecretStoreError:
        pass
    import os

    for env_key in ("OPENAI_API_KEY", "ELEVENLABS_API_KEY", "CARTESIA_API_KEY"):
        ev = (os.environ.get(env_key) or "").strip()
        if len(ev) >= 4 and ev not in vals:
            vals.append(ev)
    return vals


def handle_wake(
    *,
    seat: str,
    sender: str,
    allow: str,
    message: str,
    workdir: Path | None = None,
    default_engine: str | None = None,
    default_voice: str | None = None,
    default_model: str | None = None,
    default_format: str | None = None,
    default_speed: str | None = None,
    default_style: str | None = None,
    timeout_raw: str | None = None,
    attachments: Sequence[str | Path] | None = None,
) -> int:
    """Process one a8s wake or `ar3 tool speak` invocation."""
    if not sender:
        print("tool-speak: senderless wake refused", file=sys.stderr)
        return 0
    if not rt.allowed(sender, allow or ""):
        print(f"tool-speak: {sender!r} is not in ALLOW — refused", file=sys.stderr)
        return 0

    try:
        body, verified_attachments = rt.split_verified_attachments(
            message or "", verified=attachments
        )
    except rt.AttachmentProvenanceError as exc:
        sent = rt.send_reply(sender, f"tool-speak: {exc}")
        return _exit_code(sent, ok=False)

    if rt.is_help(body) and not verified_attachments:
        return rt.send_reply(
            sender,
            rt.help_reply(
                "tool-speak",
                seat,
                README,
                "apps/tools/speak/README.md",
            ),
        )

    wd = Path(workdir or Path.cwd()).resolve()
    sensitive = _collect_sensitive(wd)

    try:
        timeout = _parse_positive_float(timeout_raw, DEFAULT_TIMEOUT, "TIMEOUT")
        if default_engine is not None and str(default_engine).strip():
            default_engine = validate_engine_choice(default_engine)
        else:
            default_engine = None
        if default_format is not None and str(default_format).strip():
            default_format = validate_format(default_format)
        else:
            default_format = None
        parsed_default_speed = (
            validate_speed(default_speed)
            if (default_speed is not None and str(default_speed).strip())
            else None
        )
        opts = parse_message_body(body)
    except (SpeakMessageError, SpeakConfigError) as exc:
        sent = rt.send_reply(sender, scrub_text(f"tool-speak: {exc}", sensitive))
        return _exit_code(sent, ok=False)

    parts_to_speak: list[str] = []
    if opts["text"] and opts["text"].strip():
        parts_to_speak.append(opts["text"])
    for att in verified_attachments:
        att_path = Path(att)
        try:
            parts_to_speak.append(att_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            sent = rt.send_reply(
                sender,
                scrub_text(f"tool-speak: cannot read attached text file {att_path.name}: {exc}", sensitive),
            )
            return _exit_code(sent, ok=False)

    spoken_source = "\n\n".join(p for p in parts_to_speak if p.strip())
    if not spoken_source.strip():
        sent = rt.send_reply(
            sender,
            "tool-speak: provide text or markdown in the message body, or attach a text/markdown file with --attach",
        )
        return _exit_code(sent, ok=False)

    eff_engine = opts["engine"] or default_engine
    eff_voice = opts["voice"] if opts["voice"] is not None else (default_voice or None)
    eff_model = opts["model"] if opts["model"] is not None else (default_model or None)
    eff_format = opts["format"] or default_format
    eff_speed = opts["speed"] if opts["speed"] is not None else parsed_default_speed
    eff_style = opts["style"] if opts["style"] is not None else (default_style or None)

    with tempfile.TemporaryDirectory(prefix="tool-speak-reply-") as work_dir:
        fmt_ext = eff_format or DEFAULT_FORMAT
        try:
            # Determine actual extension from saved config when not overridden.
            if eff_format is None:
                from .store import read_config

                fmt_ext = read_config(wd).get("format") or DEFAULT_FORMAT
            out_file = Path(work_dir) / f"speech.{fmt_ext}"
            meta = synthesize_to_file(
                spoken_source,
                out_file,
                workdir=wd,
                engine=eff_engine,
                voice=eff_voice,
                model=eff_model,
                audio_format=eff_format,
                speed=eff_speed,
                style=eff_style,
                raw=bool(opts["raw"]),
                flat=bool(opts["flat"]),
                emphasis=bool(opts["emphasis"]),
                timeout=timeout,
            )
        except (SpeakError, SpeakConfigError, SecretStoreError, OSError) as exc:
            sent = rt.send_reply(sender, scrub_text(f"tool-speak: {exc}", sensitive))
            return _exit_code(sent, ok=False)

        model_part = f", model={meta['model']}" if meta.get("model") else ""
        summary = (
            f"Synthesized {out_file.name} "
            f"(engine={meta['engine']}, voice={meta['voice']}{model_part})"
        )
        sent = rt.send_reply(sender, summary, (str(out_file),))
        return _exit_code(sent, ok=True)
