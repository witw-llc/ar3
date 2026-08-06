"""tell — drop a JSON envelope in the outbox directory.

Requires `TELL_OUTBOX_DIR` when set (a8s injects it on agent wake). If unset
and `~/.a8s` is readable, `tell` may resolve a unique configured outbox from
CWD — see `docs/a8s-filedrop.md`. System installs for agent users typically
have no registry access and always need the env var.

`~/.a8s` reachable and CWD inside a registered agent stamps `from` and logs to
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
    out_agent,
    outbox_bundle_dir,
    version_line as _version_line,
    TELL_FILE_MAX_ENV,
    TELL_OUTBOX_DIR_ENV,
)
from mailbox import _split_content_and_files
from ulid import new as new_ulid

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
    `max_file_bytes` from settings when `~/.a8s` is reachable. Else 50 MiB.
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


def resolve_message_body(message_argv: list[str]) -> str | None:
    if message_argv == ["-"]:
        if sys.stdin.isatty():
            return None
        return sys.stdin.read()
    if message_argv:
        return join_args(message_argv)
    if sys.stdin.isatty():
        return None
    data = sys.stdin.read()
    if not data:
        return None
    return data


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
    tmp = outbox / f".{envelope_id}.tmp"
    tmp.write_text(json.dumps(msg, indent=2), encoding="utf-8")
    os.replace(tmp, dest)
    return msg


class TellUsageError(Exception):
    pass


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
    print("       message may be `-` to read stdin; stdin is used when piped", file=sys.stderr)


def _optional_sender() -> tuple[str, dict] | None:
    try:
        from registry import sender_from_cwd

        return sender_from_cwd()
    except OSError:
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
    from registry import participants_from_registry

    try:
        for p in participants_from_registry():
            try:
                registered = p.outbox_path().resolve()
            except (OSError, RuntimeError):
                continue
            try:
                if os.path.samefile(registered, outbox):
                    return True
            except OSError:
                # Registered outbox (or the resolved outbox) doesn't exist yet
                # on disk — samefile can't stat it, so fall back to the path
                # comparison it would otherwise replace.
                if registered == outbox:
                    return True
    except OSError:
        return False
    return False


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
            print("tell: --check does not accept a message, attachments, or --split", file=sys.stderr)
            return 2
        return run_check(recipient)

    if recipient is None:
        _print_usage()
        return 2

    body = resolve_message_body(message_argv)
    if body is None:
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

    sender = _optional_sender()
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
