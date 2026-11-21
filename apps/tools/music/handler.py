"""a8s wake handler and message option parser for tool-music."""
from __future__ import annotations

import io
from pathlib import Path
import shlex
import sys
import tempfile
import time

import _runtime as rt

from .engine import DEFAULT_TIMEOUT, MusicError, generate_file, resolve_format
from .pins import MODELS
from .store import (
    DEFAULT_DURATION,
    MusicConfigError,
    read_config,
    validate_bpm,
    validate_caption,
    validate_duration,
    validate_engine,
    validate_format,
    validate_key,
    validate_model,
    validate_seed,
    validate_steps,
)

README = Path(__file__).resolve().parent / "README.md"

VALUE_FLAGS = {
    "--duration": "duration",
    "-d": "duration",
    "--bpm": "bpm",
    "--key": "key",
    "--seed": "seed",
    "--steps": "steps",
    "--format": "format",
    "--model": "model",
    "--engine": "engine",
}
VALIDATORS = {
    "duration": validate_duration,
    "bpm": validate_bpm,
    "key": validate_key,
    "seed": validate_seed,
    "steps": validate_steps,
    "format": validate_format,
    "model": validate_model,
    "engine": validate_engine,
}


class MusicMessageError(ValueError):
    """Raised when message options are malformed."""


def _exit_code(sent: int, *, ok: bool) -> int:
    if not rt.print_mode():
        return 0
    return 1 if sent != 0 or not ok else 0


def parse_message_body(body: str) -> dict:
    """Parse leading `--flag value` options and return them with the description.

    Only a leading run of options is shell-parsed. Parsing stops at `--` or at
    the first word that does not start with `-`; the rest of the body, with
    its apostrophes and quotes, is the description.
    """
    opts: dict = {name: None for name in VALIDATORS}
    text = body or ""
    stripped = text.lstrip()
    if not stripped.startswith("-"):
        opts["text"] = text
        return opts
    first, newline, rest = stripped.partition("\n")
    lexer = shlex.shlex(io.StringIO(first), posix=True, punctuation_chars=False)
    lexer.whitespace_split = True
    lexer.commenters = ""
    stream: io.StringIO = lexer.instream
    remainder = ""
    while True:
        pos = stream.tell()
        tail = first[pos:].lstrip(" \t")
        if not tail:
            break
        if not tail.startswith("-") or tail in ("-",) or tail.startswith(("- ", "-\t")):
            remainder = tail
            break
        try:
            token = lexer.get_token()
        except ValueError as exc:
            raise MusicMessageError(f"cannot parse message options: {exc}") from None
        if token == "--":
            remainder = first[stream.tell():].lstrip(" \t")
            break
        flag, eq, inline = token.partition("=")
        dest = VALUE_FLAGS.get(flag)
        if dest is None:
            raise MusicMessageError(f"unknown option {flag!r} (use -- before a description that starts with -)")
        if eq:
            value = inline
        else:
            try:
                value = lexer.get_token()
            except ValueError as exc:
                raise MusicMessageError(f"cannot parse message options: {exc}") from None
            if value is None:
                raise MusicMessageError(f"missing value for {flag}")
        try:
            opts[dest] = VALIDATORS[dest](value)
        except MusicConfigError as exc:
            raise MusicMessageError(str(exc)) from None
    opts["text"] = f"{remainder}\n{rest}" if remainder and newline else (remainder or rest)
    return opts


def build_request(opts: dict, node_defaults: dict, saved: dict, *, which=None) -> dict:
    """Merge message options over node vars over `music.json` over built-in defaults."""

    def pick(key: str):
        for source in (opts, node_defaults, saved):
            if source.get(key) is not None:
                return source[key]
        return None

    model = pick("model") or "turbo"
    fmt = pick("format")
    request = {
        "engine": pick("engine") or "ace",
        "model": model,
        "caption": validate_caption(opts.get("text") or ""),
        "duration": pick("duration") or DEFAULT_DURATION,
        "steps": pick("steps") or MODELS[model].default_steps,
        "format": resolve_format(fmt) if which is None else resolve_format(fmt, which),
        "bpm": opts.get("bpm"),
        "key": opts.get("key"),
        "seed": opts.get("seed"),
    }
    return request


def summary_line(path: Path, request: dict, meta: dict, elapsed: float) -> str:
    seconds = meta.get("seconds", request["duration"])
    return (
        f"Generated {path.name}: {seconds:.0f}s instrumental "
        f"(model={meta.get('model', request['model'])}, seed={meta.get('seed')}, "
        f"device={meta.get('device', '?')}, took {elapsed:.0f}s)"
    )


def handle_wake(
    *,
    seat: str,
    sender: str,
    allow: str,
    message: str,
    workdir: Path | None = None,
    defaults: dict | None = None,
    timeout_raw: str | None = None,
) -> int:
    """Process one a8s wake or `ar3 tool music` invocation."""
    if not sender:
        print("tool-music: senderless wake refused", file=sys.stderr)
        return 0
    if not rt.allowed(sender, allow or ""):
        print(f"tool-music: {sender!r} is not in ALLOW — refused", file=sys.stderr)
        return 0
    try:
        body, attachments = rt.split_verified_attachments(message or "")
    except rt.AttachmentProvenanceError as exc:
        return _exit_code(rt.send_reply(sender, f"tool-music: {exc}"), ok=False)
    if rt.is_help(body):
        return rt.send_reply(sender, rt.help_reply("tool-music", seat, README, "apps/tools/music/README.md"))

    wd = Path(workdir or Path.cwd()).resolve()
    try:
        timeout = DEFAULT_TIMEOUT
        if timeout_raw is not None and str(timeout_raw).strip():
            try:
                timeout = float(str(timeout_raw).strip())
            except ValueError:
                raise MusicMessageError(f"invalid TIMEOUT {timeout_raw!r}") from None
            if not timeout > 0:
                raise MusicMessageError("TIMEOUT must be a positive number of seconds")
        node_defaults = {
            key: VALIDATORS[key](value)
            for key, value in (defaults or {}).items()
            if value is not None and str(value).strip()
        }
        opts = parse_message_body(body)
        request = build_request(opts, node_defaults, read_config(wd))
    except (MusicMessageError, MusicConfigError, MusicError) as exc:
        return _exit_code(rt.send_reply(sender, f"tool-music: {exc}"), ok=False)
    note = " (attachments are ignored)" if attachments else ""

    with tempfile.TemporaryDirectory(prefix="tool-music-reply-") as out_dir:
        started = time.monotonic()
        try:
            path, meta = generate_file(request, Path(out_dir), timeout=timeout)
        except (MusicError, OSError) as exc:
            return _exit_code(rt.send_reply(sender, f"tool-music: {exc}"), ok=False)
        line = summary_line(path, request, meta, time.monotonic() - started) + note
        return _exit_code(rt.send_reply(sender, line, (str(path),)), ok=True)
