"""tells — wait for the next inbound message to this node.

Receive-side complement of `tell`. The node is resolved from `TELL_OUTBOX_DIR`
exactly as `tell` resolves the sender: the file-proxy inbox is `.inbox` beside
the outbox. By default `tells` snapshots what is already there, then blocks up to
`--timeout` seconds (default 5) for new envelopes to land, prints each
(sender + body) to stdout, and exits 0. Nothing new within the timeout prints
one line to stderr and exits 1.

With `-f` / `--follow` or `--timeout 0`, poll the inbox continuously and print
each new message as it arrives until interrupted (Ctrl+C). An explicit
`--timeout` greater than zero follows for that many seconds; `-f` cannot be
combined with a positive `--timeout`.

`--glow [theme]` and `--heading-out` / `--heading-in` reuse convo's markdown
formatting (and optional GlowStream rendering). Plain `sender: body` remains
the default when those options are omitted.

On the plain path, a printed line over `--line-max` bytes (default 400 —
Claude Code's Monitor notification clips each stdout line at 500) is
soft-wrapped at the last space before the limit, with a two-space indent on
continuation lines; `0` disables it, and a positive value under
`MIN_LINE_MAX_BYTES` is refused because the indent could not fit inside it. Wrapping or a body over `--body-max`
both put the `tells --recover <token>` recovery command in the header, ahead
of the body, so a host that clips mid-message still shows where the whole
thing is. `--glow` / `--heading-out` / `--heading-in` keep convo's own
rendering unwrapped.

Non-destructive: it observes new arrivals without consuming them, so it never
races a competing reader for `.inbox` files and repeated runs each wait from
their own baseline. Existing filenames are keyed by mtime/size so a handler
that overwrites a prior ULID (common when ``tells -f`` starts before ``a8s
start``) still surfaces the new envelope. Stdout is line-buffered and flushed
so background watches show output promptly.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path


from core import harden_stdio, version_line as _version_line
from tell import agent_root_from_outbox, find_outbox

DEFAULT_TIMEOUT_SEC = 5.0
POLL_INTERVAL_SEC = 0.1
INBOX_DIRNAME = ".inbox"
# Display cap so host monitors (which often clip mid-body with a bare
# "(truncated)") see our recovery footer. 0 = unlimited. Override with
# TELLS_BODY_MAX or --body-max.
DEFAULT_BODY_MAX_CHARS = 16_000
TELLS_BODY_MAX_ENV = "TELLS_BODY_MAX"
# Claude Code's Monitor notification clips each stdout LINE at 500 bytes and
# appends "(truncated)" — measured against six real tells on 2026-09-02, all
# cut at exactly 500 (#245). 400 leaves headroom for the "SENDER: " prefix
# and whatever a different harness's own clip turns out to be. 0 = no wrap.
# Override with TELLS_LINE_MAX or --line-max.
DEFAULT_LINE_MAX_BYTES = 400
TELLS_LINE_MAX_ENV = "TELLS_LINE_MAX"
CONTINUATION_INDENT = "  "
# Every accepted --line-max has to bound the lines it emits, and the wrap
# reserves CONTINUATION_INDENT on continuation lines and never splits a
# character. Two bytes of indent plus one four-byte character plus margin is
# the smallest limit that can be honored, so smaller ones are refused rather
# than over-run. 0 (no wrap) is unaffected.
MIN_LINE_MAX_BYTES = 16


class TellsUsageError(Exception):
    pass


class TellsHelp(Exception):
    pass


class TellsVersion(Exception):
    """`--version` reached the parser."""


_USAGE = (
    "usage: tells [-f|--follow] [--timeout SEC] [--body-max N] [--line-max N] "
    "[--glow [THEME]] "
    "[--show PATH | --recover TOKEN] [--sent [--since D]] "
    "[--heading-out LINE ...] [--heading-in LINE ...]"
)


def _print_usage() -> None:
    from convo import convo_help_epilog

    print(_USAGE, file=sys.stderr)
    print("       default: wait up to 5s for the next message burst, then exit", file=sys.stderr)
    print("       --timeout SEC: follow the inbox for SEC seconds (0 = until Ctrl+C)", file=sys.stderr)
    print("       -f: same as --timeout 0 (cannot combine with positive --timeout)", file=sys.stderr)
    print(
        f"       --body-max N: truncate printed body at N chars "
        f"(default {DEFAULT_BODY_MAX_CHARS}; 0 = unlimited; env {TELLS_BODY_MAX_ENV})",
        file=sys.stderr,
    )
    print(
        f"       --line-max N: soft-wrap printed lines at N bytes, under a host's own "
        f"clip (default {DEFAULT_LINE_MAX_BYTES}; 0 = no wrap; minimum "
        f"{MIN_LINE_MAX_BYTES}; env {TELLS_LINE_MAX_ENV})",
        file=sys.stderr,
    )
    print("       --glow [THEME]: render markdown via glow (default theme from A8S_GLOW)", file=sys.stderr)
    print("       --sent [--since 2h]: this seat's outbound messages and their delivery state", file=sys.stderr)
    print(convo_help_epilog(), file=sys.stderr)


def _argv_looks_like_option(arg: str) -> bool:
    return arg.startswith("-") and arg != "-"


@dataclass(frozen=True)
class TellsOptions:
    timeout: float
    follow: bool = False
    timeout_explicit: bool = False
    body_max: int = DEFAULT_BODY_MAX_CHARS
    line_max: int = DEFAULT_LINE_MAX_BYTES
    glow_theme: str | None = None
    heading_out: str | None = None
    heading_in: str | None = None
    show: str | None = None
    sent: bool = False
    since: float | None = None

    @property
    def follow_forever(self) -> bool:
        return self.follow or (self.timeout_explicit and self.timeout == 0)

    @property
    def markdown(self) -> bool:
        return (
            self.glow_theme is not None
            or self.heading_out is not None
            or self.heading_in is not None
        )


def resolve_body_max(raw: str | None = None) -> int:
    """Parse body-max chars. Empty → default; 0 → unlimited."""
    text = (raw if raw is not None else os.environ.get(TELLS_BODY_MAX_ENV, "")).strip()
    if not text:
        return DEFAULT_BODY_MAX_CHARS
    try:
        n = int(text, 10)
    except ValueError as e:
        raise TellsUsageError(f"--body-max: {e}") from e
    if n < 0:
        raise TellsUsageError("--body-max must be zero or positive")
    return n


def resolve_line_max(raw: str | None = None) -> int:
    """Parse line-max bytes. Empty → default; 0 → no wrap."""
    text = (raw if raw is not None else os.environ.get(TELLS_LINE_MAX_ENV, "")).strip()
    if not text:
        return DEFAULT_LINE_MAX_BYTES
    try:
        n = int(text, 10)
    except ValueError as e:
        raise TellsUsageError(f"--line-max: {e}") from e
    if n < 0:
        raise TellsUsageError("--line-max must be zero or positive")
    if 0 < n < MIN_LINE_MAX_BYTES:
        raise TellsUsageError(
            f"--line-max must be 0 (no wrap) or at least {MIN_LINE_MAX_BYTES} bytes"
        )
    return n


def format_displayed_content(content: str, envelope_path: Path, body_max: int) -> str:
    """Return content for stdout; append a full-message recovery command when clipped."""
    text = content or ""
    if body_max <= 0 or len(text) <= body_max:
        return text
    path = str(envelope_path.resolve())
    # The printed command is meant to be PASTED, so its argument has to be inert
    # in whatever shell receives it — and no quoting achieves that. A quoted
    # program path is an expression in PowerShell and needs `&`; an apostrophe
    # in the path breaks bash; and double quotes are worse than they look,
    # because bash and PowerShell both expand `$name` and `$(...)` inside them
    # and cmd expands `%NAME%`. A path holding `$HOME` came back rewritten, and
    # one holding `$(...)` was executed. All measured.
    #
    # So the argument is not quoted. It is encoded: base64url with the padding
    # stripped is `A-Za-z0-9-_` and nothing else, which no shell interprets.
    # `--show` still takes a real path for someone who has one and can quote it
    # themselves; `--recover` is the form that survives a paste.
    cmd = f"tells --recover {encode_envelope_path(path)}"
    return (
        f"{text[:body_max]}\n"
        f"… (truncated at {body_max} chars; full message:\n{cmd})"
    )


def encode_envelope_path(path_str: str) -> str:
    """An absolute path as `A-Za-z0-9-_`, which every shell leaves alone."""
    import base64

    raw = base64.urlsafe_b64encode(path_str.encode("utf-8"))
    return raw.decode("ascii").rstrip("=")


def decode_envelope_path(token: str) -> str:
    import base64

    padded = token + "=" * (-len(token) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")


def _take_bytes(text: str, limit: int) -> str:
    """Longest prefix of `text` whose UTF-8 encoding is at most `limit` bytes,
    never splitting a multibyte character."""
    end = len(text)
    while end > 0 and len(text[:end].encode("utf-8")) > limit:
        end -= 1
    return text[: max(end, 1)]


def _wrap_line_bytes(line: str, limit: int) -> list[str]:
    """Soft-wrap one line at `limit` UTF-8 bytes, breaking at the last space
    before the limit. A single token that alone exceeds the limit is
    hard-broken at a byte-safe boundary. Continuation lines reserve room for
    `CONTINUATION_INDENT`, which the caller prepends.

    Splitting on plain " " (not general whitespace) keeps this reversible:
    `" ".join(line.split(" "))` always reconstructs `line`, including runs of
    repeated spaces, so joining the wrapped pieces the same way recovers the
    original whenever no token was hard-broken. A break therefore consumes
    exactly one space; a run of spaces straddling the limit keeps the rest of
    the run on one side of the break or the other. `current` is None until the
    piece is opened so that an empty token — what `split(" ")` yields for a
    leading, trailing or repeated space — is distinguishable from an empty
    buffer, which is what used to eat those spaces.
    """
    if len(line.encode("utf-8")) <= limit:
        return [line]
    indent_bytes = len(CONTINUATION_INDENT.encode("utf-8"))
    out: list[str] = []
    current: str | None = None
    current_bytes = 0

    def budget() -> int:
        return limit if not out else max(limit - indent_bytes, 1)

    def flush() -> None:
        nonlocal current, current_bytes
        out.append(current or "")
        current = None
        current_bytes = 0

    for token in line.split(" "):
        piece = token if current is None else " " + token
        piece_bytes = len(piece.encode("utf-8"))
        if current_bytes + piece_bytes <= budget():
            current = (current or "") + piece
            current_bytes += piece_bytes
            continue
        if current is not None:
            flush()
        remaining = token
        while True:
            b = budget()
            if len(remaining.encode("utf-8")) <= b:
                current = remaining
                current_bytes = len(remaining.encode("utf-8"))
                break
            chunk = _take_bytes(remaining, b)
            remaining = remaining[len(chunk):]
            out.append(chunk)
    if current is not None or not out:
        flush()
    return out


def wrap_body_text(text: str, line_max: int) -> tuple[str, bool]:
    """Soft-wrap every line of `text` under `line_max` UTF-8 bytes.

    Blank lines and existing newlines pass through untouched; only a line
    that itself exceeds the limit gets split, with continuation pieces
    prefixed by `CONTINUATION_INDENT` so a reader can see the wrap. Returns
    `(wrapped_text, any_line_was_wrapped)`; `line_max <= 0` disables wrapping.
    """
    if line_max <= 0 or not text:
        return text, False
    out_lines: list[str] = []
    wrapped_any = False
    for line in text.split("\n"):
        pieces = _wrap_line_bytes(line, line_max)
        if len(pieces) > 1:
            wrapped_any = True
            out_lines.append(pieces[0])
            out_lines.extend(CONTINUATION_INDENT + p for p in pieces[1:])
        else:
            out_lines.append(pieces[0])
    return "\n".join(out_lines), wrapped_any


def show_envelope_body(path_str: str) -> int:
    """Print one stored envelope's `content` in full. The other half of the
    recovery hint: `tells` clipped it, so `tells` prints it back."""
    import json

    path = Path(path_str)
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"tells: no such message: {path}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as e:
        print(f"tells: cannot read {path}: {e}", file=sys.stderr)
        return 1
    _configure_stdout()
    print(envelope.get("content", ""))
    return 0


def _print_sent(since_s: float | None) -> int:
    """List this seat's own outbound ULIDs and their latest known state.

    Reads the receipt files in the seat's own outbox — no registry, no
    machine-wide transaction log. A seat has to be able to answer "did that
    land?" from where it stands."""
    import receipts

    outbox = find_outbox()
    if outbox is None:
        print("tells: cannot find this agent's outbox", file=sys.stderr)
        return 1
    records = receipts.list_recent(outbox, since_s)
    if not records:
        print("tells: no sent messages on record", file=sys.stderr)
        return 1
    _configure_stdout()
    for record in records:
        print(receipts.summary_line(record), flush=True)
    return 0


def parse_tells_argv(argv: list[str]) -> TellsOptions:
    from convo import extract_heading_templates

    try:
        rest, heading_out, heading_in = extract_heading_templates(argv)
    except ValueError as e:
        raise TellsUsageError(str(e)) from e

    timeout = DEFAULT_TIMEOUT_SEC
    follow = False
    timeout_explicit = False
    body_max = resolve_body_max()
    line_max = resolve_line_max()
    show: str | None = None
    sent = False
    since: float | None = None
    default_glow = os.environ.get("A8S_GLOW", "").strip() or None
    glow_theme = default_glow
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in ("-f", "--follow"):
            follow = True
            i += 1
            continue
        if arg == "--sent":
            sent = True
            i += 1
            continue
        if arg == "--since":
            i += 1
            if i >= len(rest):
                raise TellsUsageError("--since requires a duration (30m, 2h, 7d)")
            from receipts import parse_duration

            try:
                since = parse_duration(rest[i])
            except ValueError as e:
                raise TellsUsageError(f"--since: {e}") from e
            i += 1
            continue
        if arg == "--timeout":
            i += 1
            if i >= len(rest):
                raise TellsUsageError("--timeout requires seconds")
            try:
                timeout = float(rest[i])
            except ValueError as e:
                raise TellsUsageError(f"--timeout: {e}") from e
            if timeout < 0:
                raise TellsUsageError("--timeout must be zero or positive")
            timeout_explicit = True
        elif arg == "--body-max":
            i += 1
            if i >= len(rest):
                raise TellsUsageError("--body-max requires a character count")
            body_max = resolve_body_max(rest[i])
        elif arg == "--line-max":
            i += 1
            if i >= len(rest):
                raise TellsUsageError("--line-max requires a byte count")
            line_max = resolve_line_max(rest[i])
        elif arg == "--show":
            i += 1
            if i >= len(rest):
                raise TellsUsageError("--show requires the path to a message")
            show = rest[i]
        elif arg == "--recover":
            i += 1
            if i >= len(rest):
                raise TellsUsageError("--recover requires the token tells printed")
            try:
                show = decode_envelope_path(rest[i])
            except Exception as e:
                raise TellsUsageError(f"--recover: not a token tells printed: {e}") from e
        elif arg == "--glow":
            if i + 1 < len(rest) and not _argv_looks_like_option(rest[i + 1]):
                i += 1
                glow_theme = rest[i]
            else:
                glow_theme = "auto"
        elif arg in ("-h", "--help"):
            raise TellsHelp()
        elif arg == "--version":
            raise TellsVersion()
        else:
            raise TellsUsageError(f"unexpected argument: {arg!r}")
        i += 1
    opts = TellsOptions(
        timeout=timeout,
        follow=follow,
        timeout_explicit=timeout_explicit,
        body_max=body_max,
        line_max=line_max,
        glow_theme=glow_theme,
        heading_out=heading_out,
        heading_in=heading_in,
        show=show,
        sent=sent,
        since=since,
    )
    if follow and timeout_explicit and timeout != 0:
        raise TellsUsageError("cannot use -f/--follow with a positive --timeout")
    if since is not None and not sent:
        raise TellsUsageError("--since applies to --sent")
    if sent and follow:
        raise TellsUsageError("--sent lists what was sent; it does not follow")
    return opts


def inbox_from_env() -> Path | None:
    outbox = find_outbox()
    if outbox is None:
        return None
    agent = _agent_name_for_outbox(outbox)
    if agent is not None:
        try:
            from registry import find_participant, participants_from_registry

            participant = find_participant(participants_from_registry(), agent)
            if participant is not None:
                return participant.inbox_path()
        except OSError:
            pass
    return agent_root_from_outbox(outbox) / INBOX_DIRNAME


def _agent_name_for_outbox(outbox: Path) -> str | None:
    try:
        from registry import participants_from_registry

        target = outbox.resolve()
        for p in participants_from_registry():
            try:
                if p.outbox_path().resolve() == target:
                    return p.name
            except (OSError, RuntimeError):
                continue
    except OSError:
        return None
    return None


def _inbox_fingerprints(inbox: Path) -> dict[str, tuple[int, int]]:
    """Map ``name -> (mtime_ns, size)`` for ``*.json`` envelopes.

    Fingerprints (not bare names) so a proxy overwrite of an existing ULID
    file is treated as new — important when ``tells -f`` starts before the
    handler and a prior ``.inbox`` entry is replaced on first delivery.
    """
    if not inbox.is_dir():
        return {}
    found: dict[str, tuple[int, int]] = {}
    try:
        entries = list(inbox.iterdir())
    except OSError:
        return {}
    for path in entries:
        if not (path.is_file() and path.name.endswith(".json")):
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        found[path.name] = (st.st_mtime_ns, st.st_size)
    return found


def _read_envelope(path: Path) -> dict | None:
    try:
        msg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return msg if isinstance(msg, dict) else None


# A delivery this far behind its `date` is replay, not live traffic. The gap
# is the envelope's own: `date` is when the sender queued it, `delivered_at`
# is when the receiving node wrote it to the inbox.
LATE_THRESHOLD_SEC = 600.0


def late_prefix(msg: dict, threshold_s: float = LATE_THRESHOLD_SEC) -> str:
    """`[late 32h] ` when this message sat somewhere on the way, else ``."""
    from receipts import duration_text, parse_stamp

    queued = parse_stamp(str(msg.get("date") or ""))
    delivered = parse_stamp(str(msg.get("delivered_at") or ""))
    if queued is None or delivered is None:
        return ""
    gap = (delivered - queued).total_seconds()
    if gap <= threshold_s:
        return ""
    return f"[late {duration_text(gap)}] "


def _print_plain(msg: dict, envelope_path: Path, body_max: int, line_max: int) -> None:
    sender = f"{late_prefix(msg)}{msg.get('from') or '?'}"
    raw_content = msg.get("content", "") or ""
    displayed = format_displayed_content(raw_content, envelope_path, body_max)
    wrapped, was_wrapped = wrap_body_text(displayed, line_max)
    body_max_exceeded = body_max > 0 and len(raw_content) > body_max
    if was_wrapped or body_max_exceeded:
        # The pointer goes in the header, before the body, so a host that
        # clips mid-body still lands the reader on the recovery command.
        # The body-max footer keeps its own copy of the same command
        # (below the clip point it exists to survive) — same token, so
        # nothing about it can go stale on its own. The header line itself
        # is not wrapped: it names a fixed command, and an --line-max small
        # enough to force-wrap it would make the command unpasteable.
        token = encode_envelope_path(str(envelope_path.resolve()))
        print(f"{sender}: full message: tells --recover {token}", flush=True)
        print(wrapped, flush=True)
    else:
        print(f"{sender}: {wrapped}", flush=True)


def _print_markdown(
    msg: dict,
    *,
    envelope_path: Path,
    body_max: int,
    agent: str,
    glow_stream: object | None,
    heading_out: str,
    heading_in: str,
) -> None:
    from convo import entry_from_message, print_entries

    entry = entry_from_message(msg, recipients=[agent])
    entry["content"] = late_prefix(msg) + format_displayed_content(
        entry.get("content", ""), envelope_path, body_max
    )
    print_entries(
        agent,
        [entry],
        glow_stream=glow_stream,
        heading_out=heading_out,
        heading_in=heading_in,
    )


def _poll_new_messages(
    inbox: Path,
    seen: dict[str, tuple[int, int]],
    *,
    agent: str,
    body_max: int,
    line_max: int,
    markdown: bool,
    glow_stream: object | None,
    heading_out: str,
    heading_in: str,
) -> int:
    printed = 0
    current = _inbox_fingerprints(inbox)
    for name in sorted(current):
        fingerprint = current[name]
        if seen.get(name) == fingerprint:
            continue
        path = inbox / name
        msg = _read_envelope(path)
        if msg is None:
            # Incomplete rename / mid-write — retry next poll without locking seen.
            continue
        if markdown:
            local = agent or (msg.get("to") or "").strip() or "me"
            _print_markdown(
                msg,
                envelope_path=path,
                body_max=body_max,
                agent=local,
                glow_stream=glow_stream,
                heading_out=heading_out,
                heading_in=heading_in,
            )
        else:
            _print_plain(msg, path, body_max, line_max)
        seen[name] = fingerprint
        printed += 1
    # Drop fingerprints for names that disappeared so a later recreate is fresh.
    for name in list(seen):
        if name not in current:
            del seen[name]
    return printed


def _configure_stdout() -> None:
    """Floor stdout's error handler, then prefer line buffering so background
    ``tells -f`` shows output promptly.

    Two separate `reconfigure` calls, each touching only its own keyword —
    `TextIOWrapper.reconfigure` leaves every other current setting alone, so
    flooring errors first and buffering second cannot clobber either one.
    """
    harden_stdio()
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        pass


def tells_main(argv: list[str]) -> int:
    try:
        opts = parse_tells_argv(argv)
    except TellsHelp:
        _print_usage()
        return 0
    except TellsVersion:
        print(_version_line("tells"))
        return 0
    except TellsUsageError as e:
        print(f"tells: {e}", file=sys.stderr)
        _print_usage()
        return 2

    if opts.sent:
        return _print_sent(opts.since)

    if opts.show is not None:
        # Addressed by path, so it needs no outbox and no registry — a clipped
        # message has to be recoverable from wherever the reader is standing.
        return show_envelope_body(opts.show)

    from convo import DEFAULT_HEADING_IN, DEFAULT_HEADING_OUT, open_glow_stdout

    _configure_stdout()

    inbox = inbox_from_env()
    if inbox is None:
        print("tells: cannot receive from this directory", file=sys.stderr)
        return 1

    outbox = find_outbox()
    agent = _agent_name_for_outbox(outbox) if outbox is not None else None
    heading_out = opts.heading_out if opts.heading_out is not None else DEFAULT_HEADING_OUT
    heading_in = opts.heading_in if opts.heading_in is not None else DEFAULT_HEADING_IN
    glow_stream = None
    if opts.glow_theme is not None:
        try:
            glow_stream = open_glow_stdout(opts.glow_theme)
        except FileNotFoundError:
            print("tells: glow not found on PATH", file=sys.stderr)

    poll_kwargs = {
        "agent": agent or "",
        "body_max": opts.body_max,
        "line_max": opts.line_max,
        "markdown": opts.markdown,
        "glow_stream": glow_stream,
        "heading_out": heading_out,
        "heading_in": heading_in,
    }

    try:
        # Ensure the watch target exists so a handler that starts later can
        # drop into the same directory this process is already polling.
        try:
            inbox.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        seen = _inbox_fingerprints(inbox)
        if opts.follow_forever:
            try:
                while True:
                    _poll_new_messages(inbox, seen, **poll_kwargs)
                    time.sleep(POLL_INTERVAL_SEC)
            except KeyboardInterrupt:
                return 0

        if opts.timeout_explicit:
            deadline = time.monotonic() + opts.timeout
            printed_any = False
            while time.monotonic() < deadline:
                if _poll_new_messages(inbox, seen, **poll_kwargs):
                    printed_any = True
                time.sleep(POLL_INTERVAL_SEC)
            if printed_any:
                return 0
            print(f"tells: no message within {opts.timeout:g}s", file=sys.stderr)
            return 1

        deadline = time.monotonic() + opts.timeout
        printed_any = False
        while True:
            printed = _poll_new_messages(inbox, seen, **poll_kwargs)
            if printed:
                printed_any = True
            elif printed_any:
                return 0
            if not printed_any and time.monotonic() >= deadline:
                print(f"tells: no message within {opts.timeout:g}s", file=sys.stderr)
                return 1
            time.sleep(POLL_INTERVAL_SEC)
    finally:
        if glow_stream is not None:
            glow_stream.close()
