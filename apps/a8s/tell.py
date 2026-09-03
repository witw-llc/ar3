"""tell — drop a JSON envelope in the outbox directory.

Requires `TELL_OUTBOX_DIR` when set (a8s injects it on agent wake). If unset
and `~/.config/a8s` is readable, `tell` may resolve a unique configured outbox from
CWD — see `docs/a8s-filedrop.md`. System installs for agent users typically
have no registry access and always need the env var.

`~/.config/a8s` reachable and CWD inside a registered agent stamps `from` and logs to
the agent log. Recipient validation is narrower: it runs only when the resolved
outbox is some registered agent's own outbox. Any other outbox makes tell a
staging writer for another router (r4t points a caged member's
`TELL_OUTBOX_DIR` at a per-turn staging dir), and that router resolves the
recipient against its own roster.

Attachments: any path tell can read is copied into `.outbox/<msg_id>/` before
the envelope is written. The JSON `files` array carries basename only (no
`path` field). Ingest moves the bundle with the JSON; routing delivers into
`.files/<msg_id>/` on each recipient.

Waiting for a reply is the receive-side complement `tells` (see `tells.py`).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


from core import (
    _preview,
    harden_stdio,
    out_agent,
    outbox_bundle_dir,
    version_line as _version_line,
    TELL_FILE_MAX_ENV,
    TELL_OUTBOX_DIR_ENV,
)
from mailbox import _split_content_and_files
from ar3.fsio import atomic_write_text
from ar3.ulid import new as new_ulid

DEFAULT_FILE_MAX_BYTES = 50 * 1024 * 1024


def _probe_outbox_writable(outbox: Path) -> str | None:
    probe = outbox / f".tell-check-{os.getpid()}.tmp"
    try:
        probe.write_text("{}", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return str(e)
    return None


def _outbox_from_env() -> Path | None:
    raw = os.environ.get(TELL_OUTBOX_DIR_ENV, "").strip()
    if not raw:
        return None
    try:
        outbox = Path(raw).expanduser().resolve()
        outbox.mkdir(parents=True, exist_ok=True)
        if _probe_outbox_writable(outbox) is not None:
            return None
        return outbox
    except OSError:
        return None


def _cwd_matches_outbox(cwd: Path, agent_root: Path, outbox: Path) -> bool:
    if cwd == outbox:
        return True
    try:
        outbox.relative_to(cwd)
        return True
    except ValueError:
        pass
    try:
        cwd.relative_to(agent_root)
        return True
    except ValueError:
        pass
    return False


def _outboxes_matching_cwd(cwd: Path) -> list[tuple[str, Path]]:
    """Configured (name, outbox) pairs whose seat matches `cwd`.

    Only consulted when `TELL_OUTBOX_DIR` is unset and the registry is
    readable. Matching: CWD is the outbox, CWD contains the outbox, or CWD
    sits inside the agent's registered root.
    """
    from registry import participants_from_registry

    matches: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for p in participants_from_registry():
        try:
            root = p.root.resolve()
            outbox = p.outbox_path().resolve()
        except (OSError, RuntimeError):
            continue
        if not _cwd_matches_outbox(cwd, root, outbox):
            continue
        if outbox in seen:
            continue
        seen.add(outbox)
        matches.append((p.name, outbox))
    return matches


def _outbox_from_registry() -> Path | None:
    """Unique CWD-matched configured outbox, or None if zero/ambiguous/unavailable."""
    try:
        cwd = Path.cwd().resolve()
    except OSError:
        return None
    try:
        matches = _outboxes_matching_cwd(cwd)
    except OSError:
        return None
    if len(matches) != 1:
        return None
    _name, outbox = matches[0]
    try:
        outbox.mkdir(parents=True, exist_ok=True)
        if _probe_outbox_writable(outbox) is not None:
            return None
        return outbox
    except OSError:
        return None


def find_outbox() -> Path | None:
    found = _outbox_from_env()
    if found is not None:
        return found
    if os.environ.get(TELL_OUTBOX_DIR_ENV, "").strip():
        return None
    return _outbox_from_registry()


def _report_outbox_unavailable() -> None:
    print("tell: cannot send from this directory", file=sys.stderr)
    raw = os.environ.get(TELL_OUTBOX_DIR_ENV, "").strip()
    if raw:
        print(f"tell: {TELL_OUTBOX_DIR_ENV} is set but outbox is unavailable", file=sys.stderr)
        return
    try:
        matches = _outboxes_matching_cwd(Path.cwd().resolve())
    except OSError:
        matches = []
    if len(matches) > 1:
        names = ", ".join(name for name, _ in matches)
        print(
            f"tell: multiple filedrops match this directory ({names}); "
            f"set {TELL_OUTBOX_DIR_ENV}",
            file=sys.stderr,
        )
        return
    print(f"tell: {TELL_OUTBOX_DIR_ENV} is not set", file=sys.stderr)


def agent_root_from_outbox(outbox: Path) -> Path:
    return outbox.parent.resolve()


def _absolutize_file_path(path: str) -> str:
    p = Path(path).expanduser()
    if p.is_absolute():
        return str(p.resolve())
    return str((Path.cwd() / p).resolve())


def _normalize_file_entries(entries: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for entry in entries:
        raw = (entry.get("path") or "").strip()
        if not raw:
            normalized.append(dict(entry))
            continue
        abs_path = _absolutize_file_path(raw)
        normalized.append({"filename": Path(abs_path).name, "path": abs_path})
    return normalized


def _argv_looks_like_option(arg: str) -> bool:
    return arg.startswith("-") and arg != "-"


def _argv_is_existing_file(arg: str) -> bool:
    """True if arg names a regular file. OSError (e.g. ENAMETOOLONG) → False."""
    try:
        return Path(arg).expanduser().is_file()
    except OSError:
        return False


def parse_byte_size(raw: str) -> int:
    """Parse a positive byte size: plain int, or with k/kb/m/mb/g/gb suffix."""
    text = raw.strip().lower().replace("_", "")
    if not text:
        raise ValueError("empty size")
    mult = 1
    for suffix, factor in (
        ("kb", 1024),
        ("k", 1024),
        ("mb", 1024**2),
        ("m", 1024**2),
        ("gb", 1024**3),
        ("g", 1024**3),
        ("b", 1),
    ):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
            mult = factor
            break
    value = int(text)
    if value < 0:
        raise ValueError("size must be zero or positive")
    return value * mult


def file_max_bytes() -> int:
    """Effective attachment size cap for tell.

    Prefer `TELL_FILE_MAX` (set by a8s on wake, or by the operator). Else
    `max_file_bytes` from settings when `~/.config/a8s` is reachable. Else 50 MiB.
    """
    raw = os.environ.get(TELL_FILE_MAX_ENV, "").strip()
    if raw:
        try:
            return parse_byte_size(raw)
        except ValueError as e:
            raise ValueError(f"{TELL_FILE_MAX_ENV}={raw!r}: {e}") from e
    try:
        from settings import get_int

        return get_int("max_file_bytes")
    except (OSError, ValueError, ImportError):
        return DEFAULT_FILE_MAX_BYTES


def _format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} bytes"
    if n < 1024**2:
        return f"{n / 1024:.1f} KiB"
    if n < 1024**3:
        return f"{n / (1024**2):.1f} MiB"
    return f"{n / (1024**3):.2f} GiB"


def _split_path_into_parts(src: Path, chunk_size: int, dest_dir: Path) -> list[Path]:
    size = src.stat().st_size
    if size <= chunk_size:
        return [src]
    n_parts = (size + chunk_size - 1) // chunk_size
    width = max(3, len(str(n_parts)))
    parts: list[Path] = []
    dest_dir.mkdir(parents=True, exist_ok=True)
    with src.open("rb") as handle:
        for index in range(1, n_parts + 1):
            name = f"{src.name}.part{index:0{width}d}of{n_parts:0{width}d}"
            part_path = dest_dir / name
            remaining = chunk_size
            with part_path.open("wb") as out:
                while remaining > 0:
                    buf = handle.read(min(65536, remaining))
                    if not buf:
                        break
                    out.write(buf)
                    remaining -= len(buf)
            parts.append(part_path)
    return parts


def _prepare_attachment_entries(
    files: list[dict],
    *,
    split: bool,
    work_dir: Path,
) -> tuple[list[dict], int]:
    """Validate sources, enforce size cap, optionally split oversized files.

    Returns `(entries_with_paths, 0)` on success or `([], exit_code)` on error.
    """
    try:
        limit = file_max_bytes()
    except ValueError as e:
        print(f"tell: {e}", file=sys.stderr)
        return [], 1
    if limit <= 0:
        print(f"tell: file size limit must be positive (got {limit})", file=sys.stderr)
        return [], 1

    prepared: list[dict] = []
    for entry in files:
        raw = (entry.get("path") or "").strip()
        if not raw:
            print("tell: attachment path required", file=sys.stderr)
            return [], 1
        try:
            resolved = Path(raw).resolve()
        except (OSError, RuntimeError) as e:
            print(f"tell: attachment path invalid: {raw}: {e}", file=sys.stderr)
            return [], 1
        if not resolved.is_file():
            print(f"tell: attachment not found: {resolved}", file=sys.stderr)
            return [], 1
        try:
            size = resolved.stat().st_size
        except OSError as e:
            print(f"tell: cannot stat attachment {resolved}: {e}", file=sys.stderr)
            return [], 1
        if size <= limit:
            prepared.append({"filename": resolved.name, "path": str(resolved)})
            continue
        if not split:
            print(
                f"tell: attachment {resolved.name!r} is {_format_bytes(size)} "
                f"(limit {_format_bytes(limit)} via {TELL_FILE_MAX_ENV} / max_file_bytes); "
                f"pass --split to send as parts",
                file=sys.stderr,
            )
            return [], 1
        try:
            parts = _split_path_into_parts(resolved, limit, work_dir)
        except OSError as e:
            print(f"tell: failed to split {resolved}: {e}", file=sys.stderr)
            return [], 1
        print(
            f"tell: splitting {resolved.name!r} ({_format_bytes(size)}) into "
            f"{len(parts)} parts of up to {_format_bytes(limit)}",
            file=sys.stderr,
        )
        for part in parts:
            prepared.append({"filename": part.name, "path": str(part)})
    return prepared, 0


def stage_outbox_attachments(
    outbox: Path,
    msg_id: str,
    entries: list[dict],
) -> list[dict]:
    """Copy sources into `.outbox/<msg_id>/<basename>`; return filename-only
    envelope entries."""
    bundle = outbox_bundle_dir(outbox, msg_id)
    bundle.mkdir(parents=True, exist_ok=True)
    staged: list[dict] = []
    for entry in entries:
        src = Path((entry.get("path") or "").strip()).resolve()
        name = src.name
        dest = bundle / name
        tmp = bundle / f".{name}.tmp"
        shutil.copyfile(src, tmp)
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
        staged.append({"filename": name})
    return staged


def stage_sender_attachment(
    outbox: Path,
    msg_id: str,
    source: Path | str,
) -> dict:
    """Stage one file for tests simulating outbox traffic."""
    src = Path(source).expanduser().resolve()
    return stage_outbox_attachments(outbox, msg_id, [{"path": str(src)}])[0]


def join_args(args: list[str]) -> str:
    parts: list[str] = []
    for a in args:
        if a.lstrip().startswith("FILE:"):
            parts.append("\n" + a.lstrip())
        else:
            if parts:
                parts.append(" ")
            parts.append(a)
    return "".join(parts).strip()


def parse_tell_argv(
    argv: list[str],
) -> tuple[str | None, list[str], list[str], bool, bool]:
    """Return `(recipient, attachments, message_argv, check, split)`."""
    attachments: list[str] = []
    recipient: str | None = None
    message_argv: list[str] = []
    check = False
    split = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg.startswith("--attach=") or arg.startswith("--file="):
            path = arg.split("=", 1)[1]
            if not path.strip():
                raise TellUsageError("--attach requires a path")
            attachments.append(path)
        elif arg in ("--attach", "--file"):
            i += 1
            if i >= len(argv) or _argv_looks_like_option(argv[i]):
                raise TellUsageError("--attach requires a path")
            attachments.append(argv[i])
            while (
                i + 1 < len(argv)
                and not _argv_looks_like_option(argv[i + 1])
                and _argv_is_existing_file(argv[i + 1])
            ):
                i += 1
                attachments.append(argv[i])
        elif arg == "--split":
            split = True
        elif arg == "--check":
            check = True
        elif arg in ("-h", "--help"):
            raise TellHelp()
        elif arg == "--version":
            raise TellVersion()
        elif recipient is None:
            recipient = arg
        else:
            message_argv.append(arg)
        i += 1
    return recipient, attachments, message_argv, check, split


STDIN_WAIT_SEC = 2.0
TELL_STDIN_WAIT_ENV = "TELL_STDIN_WAIT_SEC"


def _stdin_wait_sec() -> float:
    """The pipe deadline, overridable.

    Two seconds is a wall clock, and a producer that is merely slow to start
    races it: on a loaded machine the deadline can expire before a sleeping
    producer says anything, and the refusal is correct but the margin is not
    something a test can depend on. A caller that knows its producer is slow
    raises it; the suite sets it so its slow-producer case measures the
    contract rather than the box's load.
    """
    raw = os.environ.get(TELL_STDIN_WAIT_ENV)
    if not raw:
        return STDIN_WAIT_SEC
    try:
        seconds = float(raw)
    except ValueError:
        return STDIN_WAIT_SEC
    return seconds if seconds > 0 else STDIN_WAIT_SEC


def resolve_message_body(message_argv: list[str]) -> str | None:
    """The body, or None when there is not one.

    Four cases, and the difference between the last two is the whole design.

    `-` reads stdin however long that takes: the caller asked for it, so
    waiting is the instruction rather than a surprise.

    At a TERMINAL with no message, the body is typed and ends at Ctrl-D. That
    is what `mail` and `write` have always done, and a terminal EOF is the
    user's own decision.

    A PIPE with no message is the case that has to be careful. Reading it
    unconditionally is what hung a send for five hours: `isatty()` is false for
    every pipe, including one a harness holds open and never closes, so a
    caller that forgot the body got a process that waited while its sender read
    the silence as delivery. Refusing it outright is the other extreme and
    costs the ordinary `echo hi | tell bob`, which has to be retyped with `-`.

    So a pipe is read with a deadline. A real producer is already writing and
    arrives at once; a pipe with nobody behind it fails in seconds and names
    `-`. No case waits without a bound, and no case loses a body silently — the
    slow producer that misses the deadline gets an error, not a truncation.
    """
    if message_argv == ["-"]:
        if sys.stdin.isatty():
            print("tell: reading the message; end with Ctrl-D", file=sys.stderr)
        return sys.stdin.read()
    if message_argv:
        return join_args(message_argv)
    if sys.stdin.isatty():
        print("tell: reading the message; end with Ctrl-D", file=sys.stderr)
        return sys.stdin.read() or None
    return _read_pipe_before(_stdin_wait_sec())


def _read_pipe_before(deadline_sec: float) -> str | None:
    """Stdin if a producer is there, None if the deadline passes with nothing.

    Only the FIRST character is waited for. Once a producer has spoken it is a
    real one, and the rest of the body is read to EOF with no clock on it — a
    deadline over the whole read would truncate a long message, which is the
    silent failure this exists to avoid.

    A thread rather than `select`. `select` on Windows is WinSock-backed and
    takes sockets only; a pipe raises there, so `echo hi | tell bob` through
    `tell.cmd` would have failed to send on the one platform this batch is
    about. `PeekNamedPipe` through ctypes is the native answer and is a second
    code path that only one machine can run. A daemon thread is one path for
    both, and if the read never returns the thread dies with the process.

    A read that fails after the first character has to be fatal. The first
    character is prefetched out of a decoded buffer, so a body whose invalid
    byte falls past that buffer raises on the SECOND read with a valid prefix
    already in hand — and sending the prefix is exactly the silent truncation
    that reading a pipe at all is supposed to avoid. The exception crosses back
    to the caller and the send does not happen.
    """
    import threading

    if sys.stdin is None:
        return None

    first: list[str] = []
    rest: list[str] = []
    failure: list[BaseException] = []
    spoke = threading.Event()

    def read() -> None:
        try:
            head = sys.stdin.read(1)
            if head:
                first.append(head)
        except BaseException as e:  # re-raised on the calling thread
            failure.append(e)
            spoke.set()
            return
        spoke.set()
        if not head:
            return
        try:
            rest.append(sys.stdin.read())
        except BaseException as e:
            failure.append(e)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    if not spoke.wait(deadline_sec):
        return None
    reader.join()
    if failure:
        raise TellStdinError(f"could not read the piped message: {failure[0]}")
    if not first:
        return None
    return ("".join(first) + "".join(rest)) or None


def write_outbox_envelope(
    outbox: Path,
    to: str,
    content: str,
    files: list[dict],
    *,
    from_name: str | None = None,
    extra: dict | None = None,
    msg_id: str | None = None,
) -> dict:
    envelope_id = msg_id or new_ulid()
    msg: dict = {
        "id": envelope_id,
        "date": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "to": to,
        "content": content,
        "files": files,
    }
    if from_name is not None:
        msg["from"] = from_name
    if extra:
        msg.update(extra)
    dest = outbox / f"{envelope_id}.json"
    atomic_write_text(dest, json.dumps(msg, indent=2))
    return msg


class TellUsageError(Exception):
    pass


class TellStdinError(Exception):
    """Stdin was there and could not be read to the end. Distinct from "no
    message": a partial body must not be sent as if it were the whole one."""


class TellHelp(Exception):
    pass


class TellVersion(Exception):
    """`--version` reached the parser. Without this branch the flag becomes
    the recipient name, which fails later and for the wrong reason."""


_USAGE = "usage: tell [--attach PATH ...] [--split] <name> [<message...>|-]"


def _print_usage() -> None:
    print(_USAGE, file=sys.stderr)
    print("       --attach/--file may repeat; multiple paths after one flag OK if they exist", file=sys.stderr)
    print("       --split: chunk attachments over the size limit into .partNNNofMMM files", file=sys.stderr)
    print(f"       size limit: {TELL_FILE_MAX_ENV} (bytes or 50m), else max_file_bytes / 50MiB", file=sys.stderr)
    print("       body on stdin keeps the shell out of it: - <<'EOF' … EOF, or - < body.md", file=sys.stderr)
    print("       message may be `-` to read stdin, however long that takes", file=sys.stderr)
    print("       a pipe with no message is read too, if it speaks within seconds", file=sys.stderr)


def _optional_sender(outbox: Path | None = None) -> tuple[str, dict] | None:
    """The registry entry this send is attributed to locally.

    The outbox decides when it is some node's own: two nodes rooted at one repo
    both match `sender_from_cwd`, which returns whichever the registry happens
    to list first. The wire is unaffected either way — `_process_pending`
    force-overwrites `from` from the node owning the ingested directory — but
    the local log line and the envelope's initial stamp should name the node
    that actually wrote it."""
    try:
        from registry import load_registry, sender_from_cwd

        if outbox is not None:
            owner = _registered_owner(outbox)
            if owner is not None:
                return owner.name, load_registry().get(owner.name, {})
        return sender_from_cwd()
    except OSError:
        return None


def _registered_owner(outbox: Path):
    """The registered participant whose own outbox this is, or None."""
    from registry import participants_from_registry

    try:
        for p in participants_from_registry():
            try:
                registered = p.outbox_path().resolve()
            except (OSError, RuntimeError):
                continue
            try:
                if os.path.samefile(registered, outbox):
                    return p
            except OSError:
                # Registered outbox (or the resolved outbox) doesn't exist yet
                # on disk — samefile can't stat it, so fall back to the path
                # comparison it would otherwise replace.
                if registered == outbox:
                    return p
    except OSError:
        return None
    return None


def _outbox_is_registered(outbox: Path) -> bool:
    """True when `outbox` is some registered agent's own outbox.

    Recipient validation belongs to whoever routes the outbox. Writing into a
    registered a8s outbox, tell feeds the a8s router, so the registry is the
    authority on who may be addressed. Writing anywhere else, tell is a staging
    writer for another router — r4t points a caged member's `TELL_OUTBOX_DIR` at
    a per-turn staging dir it drains itself — and that router resolves the
    recipient against its own roster. Roster members are not a8s agents, so
    validating them here would reject every intra-roster delegation.
    """
    return _registered_owner(outbox) is not None


def _hijack_note(outbox: Path) -> str | None:
    """The one shape a leaked `TELL_OUTBOX_DIR` makes and nothing else does.

    Two registered identities competing is the only case where the env var's
    outbox is misleading: the CWD sits inside (or is) a DIFFERENT registered
    agent's root/outbox, so a reader there would expect mail to leave under
    that other agent's name while it actually leaves under the outbox
    owner's. A CWD that matches no registered agent at all has no competing
    identity to lose to — the env var is explicit configuration (a seat that
    deliberately exports its own TELL_OUTBOX_DIR and sends from an unrelated
    worktree, or a staging outbox like r4t's per-turn dir), so it stays
    silent even though the outbox is a registered agent's own. A CWD inside
    the owner's own root is checked first and always silent: two nodes may
    share one repo root, and the sibling must not read as a hijacker there.
    """
    if not os.environ.get(TELL_OUTBOX_DIR_ENV, "").strip():
        return None
    owner = _registered_owner(outbox)
    if owner is None:
        return None
    try:
        cwd = Path.cwd().resolve()
        if _cwd_matches_outbox(cwd, owner.root.resolve(), outbox):
            return None
    except (OSError, RuntimeError):
        return None
    from registry import participants_from_registry

    try:
        others = participants_from_registry()
    except OSError:
        return None
    for other in others:
        if other.name == owner.name:
            continue
        try:
            other_root = other.root.resolve()
            other_outbox = other.outbox_path().resolve()
        except (OSError, RuntimeError):
            continue
        if not _cwd_matches_outbox(cwd, other_root, other_outbox):
            continue
        return (
            f"{TELL_OUTBOX_DIR_ENV} points at {owner.name}'s outbox, but this "
            f"directory is {other.name}'s ({cwd}). Mail sent from here leaves "
            f"as {owner.name}. If that is not what you meant, the variable was "
            f"inherited from another shell — unset {TELL_OUTBOX_DIR_ENV}."
        )
    return None


def _validate_recipient(target_query: str) -> tuple[int, str | None, str | None]:
    from network import configured_remote_ids
    from registry import load_aliases, load_namespaces, resolve_name, split_namespace_address

    try:
        kind, members = resolve_name(target_query)
    except KeyError:
        if not configured_remote_ids():
            if ":" in target_query:
                print(f"tell: no namespace bound for {target_query!r}", file=sys.stderr)
            else:
                print(f"tell: no agent or alias named {target_query!r}", file=sys.stderr)
            return 1, None, None
        return 0, target_query, None
    except ValueError as e:
        print(f"tell: {e}", file=sys.stderr)
        return 1, None, None
    if not members:
        print(f"tell: {target_query!r} resolves to no agents", file=sys.stderr)
        return 1, None, None
    if kind == "agent":
        canonical = members[0]
    elif kind == "namespace":
        split = split_namespace_address(target_query)
        if split is not None:
            prefix, sub = split
            canonical = f"{prefix}:{sub}"
        else:
            canonical = next(
                k for k in load_namespaces()
                if k.lower() == target_query.strip().lower()
            )
    else:
        aliases = load_aliases()
        canonical = next(
            (k for k in aliases if k.lower() == target_query.lower()),
            target_query,
        )
    return 0, canonical, kind


def _registry_readable() -> bool:
    from registry import participants_from_registry

    try:
        participants_from_registry()
        return True
    except OSError:
        return False


def run_check(recipient: str | None) -> int:
    outbox = find_outbox()
    if outbox is None:
        _report_outbox_unavailable()
        return 1

    lines = ["tell: ok", f"  outbox: {outbox}"]

    note = _hijack_note(outbox)
    if note is not None:
        lines.append(f"  warning: {note}")

    if recipient is not None:
        if not _outbox_is_registered(outbox):
            if not _registry_readable():
                lines.append(
                    f"  recipient {recipient!r}: not checked (no readable registry)"
                )
            else:
                lines.append(
                    f"  recipient {recipient!r}: not checked "
                    "(staging outbox — its consumer resolves recipients)"
                )
        else:
            rc, canonical, kind = _validate_recipient(recipient)
            if rc != 0:
                return rc
            assert canonical is not None
            if kind == "alias":
                lines.append(f"  recipient {recipient!r}: ok (alias -> {canonical})")
            elif kind == "namespace":
                from registry import resolve_name

                _, members = resolve_name(recipient)
                lines.append(
                    f"  recipient {recipient!r}: ok (namespace -> {members[0]})"
                )
            else:
                lines.append(f"  recipient {recipient!r}: ok")

    for line in lines:
        print(line)
    return 0


def tell_main(argv: list[str]) -> int:
    harden_stdio()
    try:
        recipient, attachments, message_argv, check, split = parse_tell_argv(argv)
    except TellHelp:
        _print_usage()
        return 0
    except TellVersion:
        print(_version_line("tell"))
        return 0
    except TellUsageError as e:
        print(f"tell: {e}", file=sys.stderr)
        _print_usage()
        return 2

    if check:
        if attachments or message_argv or split:
            print(
                "tell: --check does not accept a message, attachments, or --split",
                file=sys.stderr,
            )
            return 2
        return run_check(recipient)

    if recipient is None:
        _print_usage()
        return 2

    try:
        body = resolve_message_body(message_argv)
    except TellStdinError as e:
        print(f"tell: {e}", file=sys.stderr)
        return 1
    if body is None:
        print(
            "tell: no message — pass one as arguments, pipe one in, or `-` to "
            "wait on stdin for as long as it takes",
            file=sys.stderr,
        )
        _print_usage()
        return 2

    content, files = _split_content_and_files(body)
    for path in attachments:
        files.append({"filename": Path(path).name, "path": path})
    files = _normalize_file_entries(files)

    outbox = find_outbox()
    if outbox is None:
        _report_outbox_unavailable()
        return 1

    note = _hijack_note(outbox)
    if note is not None:
        print(f"tell: warning: {note}", file=sys.stderr)

    sender = _optional_sender(outbox)
    to = recipient
    kind: str | None = None
    if _outbox_is_registered(outbox):
        rc, canonical, kind = _validate_recipient(recipient)
        if rc != 0:
            return rc
        assert canonical is not None
        to = canonical

    msg_id = new_ulid()
    split_dir = outbox / f".{msg_id}.parts"
    try:
        prepared, prep_rc = _prepare_attachment_entries(
            files, split=split, work_dir=split_dir
        )
        if prep_rc != 0:
            return prep_rc
        committed = False
        try:
            try:
                staged_files = (
                    stage_outbox_attachments(outbox, msg_id, prepared)
                    if prepared
                    else []
                )
            except OSError as e:
                print(f"tell: attachment staging failed: {e}", file=sys.stderr)
                return 1
            write_outbox_envelope(
                outbox,
                to,
                content,
                staged_files,
                from_name=sender[0] if sender is not None else None,
                msg_id=msg_id,
            )
            committed = True
        finally:
            if not committed:
                shutil.rmtree(outbox_bundle_dir(outbox, msg_id), ignore_errors=True)
    finally:
        if split_dir.is_dir():
            shutil.rmtree(split_dir, ignore_errors=True)

    preview = _preview(content)
    line = f"tell -> {to}: {preview}"
    if sender is not None:
        sender_name, _ = sender
        if kind == "alias":
            from registry import resolve_name

            _, members = resolve_name(recipient)
            out_agent(
                sender_name,
                f"tell -> {to} (alias of {len(members)}): {preview}",
            )
        else:
            out_agent(sender_name, line)
    else:
        print(line)

    return 0
