"""a8s mailbox — inbox/outbox/trash routing and queue helpers.

Mailbox routing is process-agnostic: a per-agent daemon may write into any
other agent's inbox even though it isn't handling them. Only `wake_once` (in
daemon.py) requires the handler attachment.

Routing runs in two phases per pass:

1. INGEST — atomically move `<root>/.outbox/<f>.json` into
   `~/.config/a8s/agents/<sender>/pending/<f>.json`. This is the only thing a8s
   ever does to a file in `<root>/.outbox/` — the agent's directory is
   one-way (agent writes, a8s renames out, never read-modify-write). After
   this phase the agent's outbox dir is empty for the duration of the pass.

2. PROCESS — each pending file gets one try per hop: local delivery uses the
   maildir-style staging (`inbox.tmp/` → `inbox/` via `os.replace`), and
   every configured remote gets one publish. If any path took the message,
   it is sent; a path that failed gets one transaction row. If no path took
   it, the message drops: it moves to trash with one `DISCARDED` row (or
   `DROPPED` when it had no path at all), and a8s never sends it again.

Atomic alias fan-out: each routed copy is written into
`<recipient>/inbox.tmp/<source-fname>` first, then renamed into
`<recipient>/inbox/` only after every recipient has staged. A crash mid-
fan-out leaves no partial state. Source filename (a ULID) is preserved
across staging so a recipient whose final inbox already has the file is
skipped — the write is idempotent.

FILE: payload transfer: tell stages outgoing attachments in
`.outbox/<msg_id>/` (basename only in the JSON). Ingest moves each bundle
with its envelope into pending; routing copies into each recipient's
`.files/<msg_id>/`. Envelope `path` fields are invalid.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from core import (
    Participant,
    _preview,
    inbound_bundle_dir,
    inbox_dir,
    inbox_tmp_dir,
    outbox_bundle_dir,
    outbox_dir,
    out_agent,
    pending_bundle_dir,
    pending_dir,
    sending_dir,
    trash_dir,
    unique_path,
)
from network import AMBIGUOUS, SENT, seen_id_append
from ar3.fsio import atomic_write_text
from registry import load_namespaces, resolve_name, opaque_prefixes
from services import StorageError, StorageService
from services.attachment_errors import ATTACHMENT_UNAVAILABLE
from services.attachment_path import bundle_file_path
import txlog
from ar3.ulid import new as new_ulid


def _max_file_bytes() -> int:
    from settings import get_int

    return get_int("max_file_bytes")


def _storage_receive_wait_seconds() -> int:
    from settings import get_setting

    try:
        return max(0, int(get_setting("storage_receive_wait_seconds")))
    except (TypeError, ValueError):
        return 0


def _storage_fetch_poll_seconds() -> float:
    from settings import get_setting

    try:
        return float(get_setting("storage_fetch_poll_seconds"))
    except (TypeError, ValueError):
        return 5.0


# A function that publishes one routed-and-from-stamped message envelope once
# to every configured remote. Takes the envelope, the sender name and the
# pass's set of remotes already found down, and answers
# `{remote_id: (outcome, detail)}`. Daemon wires this up at startup; passing
# None keeps the path purely local (no remotes configured).
PublishRemotes = Callable[[dict, str, set[str]], dict[str, tuple[str, str]]]


# ---------- mailboxes ----------

def ensure_mailboxes(p: Participant) -> None:
    """Create mailbox dirs for `p`. Inbox, inbox.tmp, pending, and trash live
    under ~/.config/a8s/ (hidden from the agent); outbox lives at the resolved
    `outbox_dir` (default `.outbox` under agent root). Incoming attachments
    use `files_dir` (default `.files`); that directory is created on wake."""
    for d in (
        inbox_dir(p.name),
        inbox_tmp_dir(p.name),
        pending_dir(p.name),
        trash_dir(p.name),
    ):
        d.mkdir(parents=True, exist_ok=True)
    p.outbox_path().mkdir(parents=True, exist_ok=True)


def _move_dir(src: Path, dest: Path) -> None:
    if not src.is_dir():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(str(src), str(dest))
    except OSError:
        shutil.copytree(src, dest, dirs_exist_ok=True)
        shutil.rmtree(src, ignore_errors=True)


def _remove_dir(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _msg_id_from_pending_json(name: str) -> str:
    return Path(name).stem


def _pending_attachment_status(
    sender_name: str, msg_id: str, entry: dict
) -> tuple[Path | None, str]:
    """Resolve a pending-bundle attachment for upload or local delivery.

    Returns `(path, "")` on success or `(None, reason)` with a specific
    diagnostic when the bytes cannot be read."""
    if not (msg_id or "").strip():
        return None, "missing message id"
    if (entry.get("path") or "").strip():
        return None, "files entry has path field (expected filename-only pending shape)"
    filename = (entry.get("filename") or "").strip()
    if not filename:
        return None, "missing filename in files entry"
    bundle = pending_bundle_dir(sender_name, msg_id)
    src, err = bundle_file_path(bundle, filename)
    if src is None:
        return None, err
    if not src.exists():
        return None, f"not found: {src}"
    if src.is_dir():
        return None, f"is a directory, not a file: {src}"
    if not src.is_file():
        return None, f"not a regular file: {src}"
    try:
        size = src.stat().st_size
    except OSError as e:
        return None, f"cannot stat {src}: {e}"
    max_bytes = _max_file_bytes()
    if size > max_bytes:
        return None, f"size {size} bytes exceeds max_file_bytes ({max_bytes})"
    return src, ""


def _resolve_pending_attachment(sender_name: str, msg_id: str, entry: dict) -> Path | None:
    path, _ = _pending_attachment_status(sender_name, msg_id, entry)
    return path


def _transfer_file_to_recipient(
    sender_name: str,
    msg_id: str,
    recipient: Participant,
    entry: dict,
) -> dict | None:
    """Copy one attachment from pending into the recipient's files bundle."""
    if (entry.get("path") or "").strip():
        out_agent(
            sender_name,
            "FILE: rejected — envelope path field invalid (filename-only entries)",
        )
        return None
    filename = (entry.get("filename") or "").strip()
    if not filename:
        return None
    src_resolved, reason = _pending_attachment_status(sender_name, msg_id, entry)
    if src_resolved is None:
        out_agent(
            sender_name,
            f"FILE: staged attachment missing or invalid: {filename!r} ({reason})",
        )
        return None
    dest_dir = recipient.files_bundle_dir(msg_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    try:
        shutil.copyfile(src_resolved, dest)
        os.chmod(dest, 0o644)
    except OSError as e:
        out_agent(sender_name, f"FILE: copy failed {src_resolved} -> {dest}: {e}")
        return None
    out_agent(sender_name, f"FILE: delivered {filename} -> {dest_dir}")
    txlog.log("FILE_DELIVERED", sender=sender_name, files=[filename], detail=str(dest_dir))
    return {"filename": filename}


def _build_routed_message(
    base_msg: dict,
    sender: Participant,
    recipient: Participant,
) -> dict:
    """Construct the routed copy of `base_msg` for `recipient` — copies any
    `FILE:` payloads into the recipient's `.files/` and rewrites the file
    paths in the returned message. Files that fail validation are dropped
    (logged); the message is delivered with the surviving files.

    Strict opacity: the `to` field is left at whatever the sender
    wrote — alias for fanned messages, agent name for direct ones — same as
    a public mailing list's `To:` header."""
    routed = dict(base_msg)
    msg_id = (base_msg.get("id") or "").strip()
    src_files = base_msg.get("files") or []
    if src_files and msg_id:
        new_files: list[dict] = []
        for entry in src_files:
            rewritten = _transfer_file_to_recipient(
                sender.name, msg_id, recipient, entry
            )
            if rewritten is not None:
                new_files.append(rewritten)
        routed["files"] = new_files
    return routed


def _commit_staged_inboxes(
    staged: list[tuple[Participant, Path, Path]],
) -> dict[str, str]:
    """Atomically promote each staged copy from `inbox.tmp/` into `inbox/`.
    Returns the recipients whose copy did not move, each with its error; their
    staged copy is removed."""
    failed: dict[str, str] = {}
    for recipient, staging, final in staged:
        try:
            os.replace(str(staging), str(final))
        except OSError as e:
            failed[recipient.name] = str(e)
            try:
                staging.unlink()
            except OSError:
                pass
    return failed


# ---------- routing ----------

def _claimed_from_name(envelope: Path) -> str:
    """Bare `from` claim on an outbox envelope, or "" when unreadable / absent.

    Used only to attribute a shared-outbox file to a co-registered owner —
    corrupt JSON falls through to the default owner and is trashed later."""
    try:
        with envelope.open("r", encoding="utf-8") as fp:
            msg = json.load(fp)
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(msg, dict):
        return ""
    return str(msg.get("from") or "").strip()


def _ingest_owner(
    envelope: Path,
    owners: list[Participant],
    owners_by_name: dict[str, Participant],
    default: Participant,
) -> Participant:
    """Who owns an outbox envelope on a (possibly shared) mount.

    A single owner always claims every file. On a shared outbox, a claimed
    `from` that names a co-registered peer attributes the file to that peer;
    anything else keeps `default` (the handled agent that scanned this path)."""
    if len(owners) == 1:
        return owners[0]
    claimed = _claimed_from_name(envelope)
    if claimed:
        peer = owners_by_name.get(claimed.lower())
        if peer is not None:
            return peer
    return default


def _log_enqueued(owner: Participant, pending_file: Path) -> None:
    try:
        msg = json.loads(pending_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(msg, dict):
        return
    txlog.log(
        "ENQUEUED",
        msg_id=str(msg.get("id") or pending_file.stem),
        sender=owner.name,
        recipient=str(msg.get("to") or ""),
        detail=_preview(str(msg.get("content") or "")),
    )


def _ingest_outboxes(
    senders: list[Participant],
    all_agents: list[Participant],
) -> list[Participant]:
    """Phase 1: atomically move every handled `<root>/.outbox/<id>.json` and
    its attachment bundle into pending.

    Each physical outbox directory is scanned once (via handled senders only).
    Co-owners are taken from `all_agents` so a daemon that handles one of
    several names on a shared mount still attributes a claimed peer correctly.
    Returns every participant whose pending received a file this pass — peers
    included — so phase 2 can deliver without waiting for their handler."""
    owners_by_outbox: dict[Path, list[Participant]] = {}
    for agent in all_agents:
        outbox = agent.outbox_path()
        if not outbox.is_dir():
            continue
        owners_by_outbox.setdefault(outbox.resolve(), []).append(agent)

    pending_owners: dict[str, Participant] = {}
    scanned: set[Path] = set()
    for sender in senders:
        ensure_mailboxes(sender)
        outbox = sender.outbox_path()
        if not outbox.is_dir():
            continue
        key = outbox.resolve()
        if key in scanned:
            continue
        scanned.add(key)
        owners = owners_by_outbox.get(key, [sender])
        for peer in owners:
            ensure_mailboxes(peer)
        owners_by_name = {o.name.lower(): o for o in owners}
        for f in sorted(outbox.iterdir()):
            if not (f.is_file() and f.name.endswith(".json")):
                continue
            owner = _ingest_owner(f, owners, owners_by_name, sender)
            msg_id = _msg_id_from_pending_json(f.name)
            dest = pending_dir(owner.name) / f.name
            try:
                os.rename(str(f), str(dest))
            except OSError:
                try:
                    shutil.copy2(str(f), str(dest))
                    f.unlink()
                except OSError as e:
                    out_agent(owner.name, f"ingest copy failed on {f.name}: {e}")
                    continue
            pending_owners[owner.name.lower()] = owner
            _log_enqueued(owner, dest)
            bundle_src = outbox_bundle_dir(outbox, msg_id)
            bundle_dest = pending_bundle_dir(owner.name, msg_id)
            try:
                _move_dir(bundle_src, bundle_dest)
            except OSError as e:
                out_agent(owner.name, f"ingest bundle move failed for {msg_id}: {e}")
    return list(pending_owners.values())


def _trash_pending(sender: Participant, pending_file: Path) -> None:
    """Retire a message that did not send into the agent's trash — envelope
    and attachment bundle both. The copy is a local record for the operator;
    a8s never sends it again."""
    trash = trash_dir(sender.name)
    trash.mkdir(parents=True, exist_ok=True)
    bad = unique_path(trash / pending_file.name)
    try:
        pending_file.rename(bad)
    except OSError as e:
        out_agent(sender.name, f"could not move {pending_file.name} to trash: {e}")
    msg_id = _msg_id_from_pending_json(pending_file.name)
    bundle = pending_bundle_dir(sender.name, msg_id)
    if bundle.is_dir():
        _move_dir(bundle, unique_path(trash / msg_id))


_unclaimable: set[Path] = set()


def _claim_pending(sender: Participant, queued: Path, claimed_dir: Path) -> Path | None:
    """Move a pending file out of the scanned directory before it is sent.
    A file that will not move is not sent; another daemon that claimed it
    first leaves nothing to move."""
    claimed = claimed_dir / queued.name
    try:
        claimed_dir.mkdir(parents=True, exist_ok=True)
        os.rename(str(queued), str(claimed))
    except FileNotFoundError:
        return None
    except OSError as e:
        if queued not in _unclaimable:
            _unclaimable.add(queued)
            out_agent(sender.name, f"could not claim {queued.name} for sending: {e}")
        return None
    _unclaimable.discard(queued)
    return claimed


def _finalize_pending(sender: Participant, pending_file: Path) -> None:
    msg_id = _msg_id_from_pending_json(pending_file.name)
    try:
        pending_file.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        out_agent(sender.name, f"could not remove sent {pending_file.name}: {e}")
    _remove_dir(pending_bundle_dir(sender.name, msg_id))


def _shape_error(msg: dict) -> str:
    """Why a pending message cannot be routed as written, or '' when it can."""
    for key in ("to", "from", "id", "content"):
        if not isinstance(msg.get(key) or "", str):
            return f"'{key}' is not a string"
    files = msg.get("files") or []
    if not isinstance(files, list) or not all(isinstance(e, dict) for e in files):
        return "'files' is not a list of objects"
    return ""


def _drop_pending(
    sender: Participant,
    pending_file: Path,
    event: txlog.Event,
    *,
    msg_id: str,
    recipient: str,
    files: list[str],
    reason: str,
    remote: str = "",
) -> None:
    """A message no path took: the agent log, one transaction row, and the
    trash. `DROPPED` is a message that had no path; `DISCARDED` is one whose
    every path failed."""
    out_agent(sender.name, f"dropped {pending_file.name}: {reason}")
    txlog.log(
        event,
        msg_id=msg_id,
        sender=sender.name,
        recipient=recipient,
        files=files or None,
        remote=remote,
        detail=reason,
    )
    _trash_pending(sender, pending_file)


def _resolve_file_source(sender_name: str, msg_id: str, entry: dict) -> Path | None:
    return _resolve_pending_attachment(sender_name, msg_id, entry)


def _record_upload_failure(
    msg: dict,
    sender: Participant,
    filename: str,
    detail: str,
    *,
    blocking: bool = True,
) -> None:
    """One attachment-upload failure, on the agent log for a tailing operator
    and as a `FILE_UPLOAD_FAILED` row so `a8s trace` sees it.

    `blocking` is False when another service already carries this file."""
    tail = "the message is not published" if blocking else "another service carries this file"
    out_agent(sender.name, f"FILE: upload failed for {filename!r}: {detail}; {tail}")
    txlog.log(
        "FILE_UPLOAD_FAILED",
        msg_id=msg.get("id", ""),
        sender=sender.name,
        recipient=str(msg.get("to") or "").strip(),
        files=[filename],
        detail=detail,
    )


def _upload_files_for_remote(
    msg: dict,
    sender: Participant,
    services: list[StorageService],
) -> str:
    """Store every file in every configured storage service, once each, and
    answer why the message cannot be published, or "" when it can.

    Redundancy is any-of, not all-of: every service is tried, so the good case
    is a copy in all of them, but one dead service does not stop the others.
    The floor is one: a file no service took fails the whole publish, because
    an envelope whose attachment nobody can fetch is a silent loss.

    On success, rewrites `msg["files"]` in place to the wire shape
    `[{"filename": ..., "storage": [url1, url2, ...]}]` (drops `path`). Only
    services that took the file contribute a URL."""
    files = msg.get("files") or []
    if not files or not services:
        return ""

    uploaded: dict[str, dict[str, str]] = {}
    blocked = ""
    msg_id = (msg.get("id") or "").strip()
    for entry in files:
        filename = entry.get("filename") or ""
        if not filename:
            continue
        per_file = uploaded.setdefault(filename, {})
        src, src_reason = (
            _pending_attachment_status(sender.name, msg_id, entry)
            if msg_id
            else (None, "missing message id")
        )
        failures: list[str] = []
        if src is None:
            failures.append(src_reason)
        else:
            for service in services:
                try:
                    per_file[service.id] = service.store(src, msg_id=msg_id)
                except StorageError as e:
                    failures.append(f"storage {service.id} upload failed: {e}")
                except Exception as e:
                    failures.append(f"storage {service.id} upload raised: {e}")

        blocking = not per_file
        for detail in failures:
            _record_upload_failure(msg, sender, filename, detail, blocking=blocking)
        if blocking and not blocked:
            blocked = f"no storage service took {filename!r}: {failures[-1] if failures else 'no copy'}"

    if blocked:
        return blocked

    msg["files"] = [
        {"filename": entry.get("filename") or "",
         "storage": list(uploaded.get(entry.get("filename") or "", {}).values())}
        for entry in files
    ]
    return ""


def _attempt_download_file(
    recipient: Participant,
    dest: Path,
    filename: str,
    urls: list,
    services: list[StorageService],
    max_bytes: int,
    announce: bool = True,
) -> bool:
    """One pass over every URL/service for one file. `announce` is False on
    retry rounds — a 900s wait polls ~180 times and each failure would
    otherwise repeat the same warning into the agent's log."""
    delivered = False
    for url in urls:
        for service in services:
            try:
                if service.retrieve(url, dest):
                    delivered = True
                    break
            except StorageError as e:
                if announce:
                    out_agent(
                        recipient.name,
                        f"WARN storage {service.id} download failed for {filename!r}: {e}",
                    )
        if not delivered:
            try:
                from services.http_get import http_get_url_to_path

                if http_get_url_to_path(url, dest, max_bytes=max_bytes):
                    delivered = True
            except StorageError as e:
                if announce:
                    out_agent(
                        recipient.name,
                        f"WARN http download failed for {filename!r}: {e}",
                    )
        if delivered:
            break
    if not delivered:
        return False
    try:
        size = dest.stat().st_size
    except OSError as e:
        out_agent(
            recipient.name,
            f"WARN cannot stat downloaded {filename!r}: {e}",
        )
        return False
    if size > max_bytes:
        dest.unlink(missing_ok=True)
        out_agent(
            recipient.name,
            f"WARN dropped {filename!r}: size {size} exceeds max_file_bytes ({max_bytes})",
        )
        return False
    return True


def _download_files_to_recipient(
    msg: dict,
    recipient: Participant,
    services: list[StorageService],
    *,
    wait_s: int | None = None,
    announce_failures: bool = True,
) -> dict:
    """Pull `msg["files"][i]["storage"]` URLs into `<root>/.files/<msg_id>/`.

    Retries until each file is delivered or the wait expires. `wait_s`
    overrides `storage_receive_wait_seconds`; 0 means one attempt. Failures
    become `error: ATTACHMENT_UNAVAILABLE` entries so wakes do not imply bytes
    exist. Returns a NEW dict with filename-only or error-shaped `files`
    entries. `announce_failures` is False when the caller is only probing and
    will retry — the give-up warning belongs to whoever actually gives up."""
    out_msg = dict(msg)
    msg_id = (msg.get("id") or "").strip()
    src_files = msg.get("files") or []
    new_files: list[dict] = []
    if not msg_id:
        out_msg["files"] = []
        return out_msg
    dest_root = recipient.files_bundle_dir(msg_id)
    dest_root.mkdir(parents=True, exist_ok=True)
    max_bytes = _max_file_bytes()
    if wait_s is None:
        wait_s = _storage_receive_wait_seconds()
    poll_s = _storage_fetch_poll_seconds()
    pending: list[dict] = []
    for entry in src_files:
        filename = entry.get("filename") or ""
        urls = entry.get("storage") or []
        if not filename:
            continue
        if not urls:
            # A sender that could not upload publishes the entry precisely to
            # say the file existed and was lost. Dropping it here repeats the
            # silence it was written to break, and restating it as a download
            # failure would replace the sender's reason with our own.
            if entry.get("error"):
                carried = {"filename": filename, "error": entry["error"]}
                if entry.get("detail"):
                    carried["detail"] = entry["detail"]
                new_files.append(carried)
            continue
        dest, err = bundle_file_path(dest_root, filename)
        if dest is None:
            out_agent(
                recipient.name,
                f"WARN rejected inbound attachment filename {filename!r}: {err}",
            )
            new_files.append({
                "filename": filename,
                "error": ATTACHMENT_UNAVAILABLE,
                "detail": err,
            })
            continue
        pending.append({"filename": filename, "urls": urls, "dest": dest})

    deadline = time.monotonic() + wait_s if wait_s > 0 else time.monotonic()
    first_pass = True
    while pending:
        still: list[dict] = []
        for item in pending:
            if _attempt_download_file(
                recipient,
                item["dest"],
                item["filename"],
                item["urls"],
                services,
                max_bytes,
                announce=first_pass,
            ):
                new_files.append({"filename": item["filename"]})
            else:
                still.append(item)
        pending = still
        first_pass = False
        if not pending:
            break
        if wait_s <= 0:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_s)

    for item in pending:
        detail = (
            f"could not download after {wait_s}s; contact an administrator"
            if wait_s > 0
            else "could not download; contact an administrator"
        )
        if announce_failures:
            out_agent(
                recipient.name,
                f"WARN attachment {item['filename']!r} unavailable after retries ({detail})",
            )
        new_files.append({
            "filename": item["filename"],
            "error": ATTACHMENT_UNAVAILABLE,
            "detail": detail,
        })
    out_msg["files"] = new_files
    return out_msg


def _stamp_from(
    sender_name: str,
    claimed: str,
    recipient: str,
    owned_prefixes: dict[str, str],
    opaque: set[str],
) -> str:
    """The `from` a routed message carries, keyed by the namespace prefixes
    bound to the sender (lowercase key -> registry spelling) and their opacity.

    Ownership is settled by the filesystem — a claim the sender's own
    namespaces don't back is discarded. By default a namespace changes nothing
    about presentation: a sub-sender claim under the node's own prefix stands
    to any recipient (`acme:gerry` arrives as `acme:gerry`), because
    attribution is the default and only the bound node writes this outbox.

    A binding made with `--opaque` conceals instead: mail leaving that prefix
    presents as the bare prefix — one address outside, whatever it fronts —
    while mail addressed inside the prefix keeps the sub-sender. A node-name
    or unbacked claim presents as the node's single opaque prefix when it has
    exactly one; anything else falls back to the agent name."""
    prefix = owned_prefixes.get(claimed.partition(":")[0].strip().lower())
    if prefix is not None and prefix.lower() not in opaque:
        return claimed if ":" in claimed else sender_name
    if prefix is None:
        concealed = [p for p in owned_prefixes.values() if p.lower() in opaque]
        return concealed[0] if len(concealed) == 1 else sender_name
    if recipient.partition(":")[0].strip().lower() == prefix.lower():
        return claimed if ":" in claimed else prefix
    return prefix


def _process_pending(
    sender: Participant,
    by_name: dict[str, Participant],
    publish_remotes: Optional[PublishRemotes],
    configured_remote_ids: list[str],
    services: Optional[list[StorageService]] = None,
    unstarted_remote_ids: Optional[list[str]] = None,
    down: Optional[set[str]] = None,
) -> int:
    """Phase 2: give each file in `~/.config/a8s/agents/<sender>/pending/` one
    try per hop — local delivery and one publish to each configured remote —
    and settle it in this pass. If any path took it, it is sent and leaves
    pending; a path that failed gets one row. If none took it, it drops to
    trash. Returns the count of local deliveries this pass.

    `down` collects the remotes this pass found without a link, so a later
    message in the same pass does not wait for that link again."""
    routed = 0
    pending = pending_dir(sender.name)
    if not pending.is_dir():
        return 0
    if down is None:
        down = set()
    owned_prefixes = {
        p.lower(): p for p, a in load_namespaces().items()
        if a.lower() == sender.name.lower()
    }
    opaque = opaque_prefixes()
    files = sorted(
        f for f in pending.iterdir()
        if f.is_file() and f.name.endswith(".json")
    )
    claimed_dir = sending_dir(sender.name)
    for queued in files:
        f = _claim_pending(sender, queued, claimed_dir)
        if f is None:
            continue
        try:
            with f.open("r", encoding="utf-8") as fp:
                msg = json.load(fp)
        except (OSError, json.JSONDecodeError) as e:
            _drop_pending(
                sender, f, "DROPPED",
                msg_id=_msg_id_from_pending_json(f.name), recipient="", files=[],
                reason=f"unreadable message: {e}",
            )
            continue
        if not isinstance(msg, dict):
            _drop_pending(
                sender, f, "DROPPED",
                msg_id=_msg_id_from_pending_json(f.name), recipient="", files=[],
                reason="unreadable message: not a JSON object",
            )
            continue
        shape_error = _shape_error(msg)
        if shape_error:
            msg_id = msg.get("id")
            _drop_pending(
                sender, f, "DROPPED",
                msg_id=msg_id if isinstance(msg_id, str) and msg_id else _msg_id_from_pending_json(f.name),
                recipient="", files=[],
                reason=f"malformed message: {shape_error}",
            )
            continue
        recipient_name = (msg.get("to") or "").strip()
        msg["from"] = _stamp_from(
            sender.name, str(msg.get("from") or "").strip(),
            recipient_name, owned_prefixes, opaque,
        )
        msg_id = msg.get("id", "")
        preview = _preview(msg.get("content", ""))
        msg_files = [e.get("filename", "") for e in (msg.get("files") or []) if e.get("filename")]
        if not recipient_name:
            _drop_pending(
                sender, f, "DROPPED",
                msg_id=msg_id, recipient="", files=msg_files,
                reason="empty 'to'",
            )
            continue
        # ----- LOCAL ROUTING -----
        local_target_known = False
        local_delivered = False
        local_failure = ""
        kind = None
        member_names: list[str] = []
        try:
            kind, member_names = resolve_name(recipient_name)
            local_target_known = True
        except KeyError:
            pass  # unknown locally — remotes (if any) might still deliver
        except ValueError as e:
            _drop_pending(
                sender, f, "DROPPED",
                msg_id=msg_id, recipient=recipient_name, files=msg_files,
                reason=str(e),
            )
            continue
        if local_target_known:
            recipients: list[Participant] = []
            for m in member_names:
                rp = by_name.get(m.lower())
                if rp is not None and rp.name != sender.name:
                    recipients.append(rp)
            if recipients:
                staged: list[tuple[Participant, Path, Path]] = []
                try:
                    for recipient in recipients:
                        ensure_mailboxes(recipient)
                        final = inbox_dir(recipient.name) / f.name
                        if final.is_file():
                            continue
                        routed_msg = _build_routed_message(msg, sender, recipient)
                        staging = inbox_tmp_dir(recipient.name) / f.name
                        with staging.open("w", encoding="utf-8") as out_f:
                            json.dump(routed_msg, out_f, indent=2)
                        staged.append((recipient, staging, final))
                except OSError as e:
                    local_failure = f"stage failed: {e}"
                    out_agent(sender.name, f"stage failed on {f.name}: {e}")
                    for _, staging, _ in staged:
                        try:
                            staging.unlink()
                        except OSError:
                            pass
                if not local_failure:
                    failed = _commit_staged_inboxes(staged)
                    committed = [r for r, _, _ in staged if r.name not in failed]
                    if staged and not committed:
                        local_failure = "commit failed: " + "; ".join(failed.values())
                        out_agent(sender.name, f"commit failed on {f.name}: {local_failure}")
                    else:
                        local_delivered = True
                        for name, err in failed.items():
                            out_agent(sender.name, f"commit failed on {f.name} for {name}: {err}")
                            txlog.log("DROPPED", msg_id=msg_id, sender=sender.name, recipient=name, files=msg_files or None, detail=f"local delivery failed: commit failed: {err}")
                    if committed:
                        if msg_id:
                            seen_id_append(msg_id)
                        if kind == "alias":
                            out_agent(sender.name, f"routed: {sender.name} -> {recipient_name} (alias of {len(recipients)}): {preview}")
                            for recipient in committed:
                                out_agent(recipient.name, f"received from {sender.name} (via {recipient_name} alias): {preview}")
                                txlog.log("ROUTED", msg_id=msg_id, sender=sender.name, recipient=recipient.name, files=msg_files or None, detail=preview)
                        elif kind == "namespace":
                            recipient = committed[0]
                            out_agent(sender.name, f"routed: {sender.name} -> {recipient_name} (namespace via {recipient.name}): {preview}")
                            out_agent(recipient.name, f"received from {sender.name} (to {recipient_name}): {preview}")
                            txlog.log("ROUTED", msg_id=msg_id, sender=sender.name, recipient=recipient.name, files=msg_files or None, detail=preview)
                        else:
                            recipient = committed[0]
                            out_agent(sender.name, f"routed: {sender.name} -> {recipient.name}: {preview}")
                            out_agent(recipient.name, f"received from {sender.name}: {preview}")
                            txlog.log("ROUTED", msg_id=msg_id, sender=sender.name, recipient=recipient.name, files=msg_files or None, detail=preview)
                        routed += len(committed)
                        import convo

                        delivered_names = [
                            r.name for r in recipients
                            if (inbox_dir(r.name) / f.name).is_file()
                        ]
                        convo.record(msg, recipients=delivered_names)
            else:
                # An alias whose only member is the sender, or the sender
                # addressing itself: nothing to deliver here.
                out_agent(sender.name, f"{recipient_name!r} has no local recipients (excluding self)")
                local_delivered = True
        # ----- REMOTE ROUTING -----
        has_files = bool(msg.get("files"))
        took_remotes: list[str] = []
        remote_failures: dict[str, str] = {}
        files_stay_local = False
        if publish_remotes is not None and configured_remote_ids:
            services_list = services or []
            if has_files and not services_list:
                # No storage service: file payloads cannot make the trip, so
                # the message stays local-only and the remotes are skipped.
                out_agent(sender.name, f"FILE: payloads in {f.name} not published to remotes (no storage configured)")
                files_stay_local = True
            else:
                publish_msg = dict(msg)
                blocked = ""
                if has_files:
                    publish_msg["files"] = list(msg.get("files") or [])
                    blocked = _upload_files_for_remote(publish_msg, sender, services_list)
                if blocked:
                    remote_failures = {rid: blocked for rid in configured_remote_ids}
                else:
                    outcomes = publish_remotes(publish_msg, sender.name, down)
                    for rid, (outcome, detail) in outcomes.items():
                        if outcome == SENT:
                            txlog.log("PUBLISHED", msg_id=msg_id, sender=sender.name, recipient=recipient_name, remote=rid, files=msg_files if has_files else None, detail=preview)
                        elif outcome == AMBIGUOUS:
                            txlog.log("PUBLISH_AMBIGUOUS", msg_id=msg_id, sender=sender.name, recipient=recipient_name, remote=rid, files=msg_files if has_files else None, detail=detail)
                        else:
                            remote_failures[rid] = detail
                            continue
                        took_remotes.append(rid)
        # ----- OUTCOME -----
        if not local_target_known and not configured_remote_ids:
            if unstarted_remote_ids:
                down_ids = ", ".join(unstarted_remote_ids)
                _drop_pending(
                    sender, f, "DROPPED",
                    msg_id=msg_id, recipient=recipient_name, files=msg_files,
                    reason=f"{recipient_name!r} is not local and remote {down_ids} did not start",
                    remote=down_ids,
                )
            else:
                _drop_pending(
                    sender, f, "DROPPED",
                    msg_id=msg_id, recipient=recipient_name, files=msg_files,
                    reason=f"unknown recipient {recipient_name!r}",
                )
            continue
        if local_delivered or took_remotes:
            if took_remotes:
                import convo

                convo.record(msg, recipients=[recipient_name])
            if local_failure:
                txlog.log("DROPPED", msg_id=msg_id, sender=sender.name, recipient=recipient_name, files=msg_files or None, detail=f"local delivery failed: {local_failure}")
            for rid, reason in remote_failures.items():
                txlog.log("DROPPED", msg_id=msg_id, sender=sender.name, recipient=recipient_name, remote=rid, files=msg_files or None, detail=reason)
            _finalize_pending(sender, f)
            continue
        reasons = [f"local delivery failed: {local_failure}"] if local_failure else []
        if files_stay_local:
            reasons.append("its files cannot reach a remote: no storage service configured")
        reasons += [f"remote {rid}: {reason}" for rid, reason in remote_failures.items()]
        _drop_pending(
            sender, f, "DISCARDED",
            msg_id=msg_id, recipient=recipient_name, files=msg_files,
            reason="; ".join(reasons) or "no path took it",
        )
    return routed


def route_outboxes(
    senders: list[Participant],
    all_agents: list[Participant] | None = None,
    publish_remotes: Optional[PublishRemotes] = None,
    configured_remote_ids: Optional[list[str]] = None,
    services: Optional[list[StorageService]] = None,
    unstarted_remote_ids: Optional[list[str]] = None,
) -> int:
    """Two-phase routing pass:

      1. Ingest: move new outbox files out of every handled sender's
         `<root>/.outbox/` into pending. Co-owners on a shared mount come from
         `all_agents`, so a single-name handler still attributes a claimed peer.
         The agent's directory is touched only by the rename — never
         read-modified-rewritten.
      2. Process: give each pending message one try at local delivery (via
         `inbox.tmp/` → `inbox/` atomic stage→commit) and one publish to each
         configured remote, then finalize or trash it. Phase 2 covers handled
         senders plus any co-registered peer that received pending this pass.

    `publish_remotes` and `configured_remote_ids` are the daemon-wired
    hooks for cross-cluster routing; both default to None / [] so an
    install with no remotes configured routes locally only.

    `services` is the storage-service hook for cross-cluster `FILE:`
    payloads. When set and a message has files, each service uploads its
    bytes and the wire envelope carries `files[i].storage = [...]`. None /
    empty keeps files local-only and skips the remotes for that message.

    `unstarted_remote_ids` names configured remotes that failed to start. With
    none started, mail for a name this node does not hold drops with a
    `DROPPED` row that names the remote that did not start."""
    if all_agents is None:
        all_agents = senders
    by_name = {p.name.lower(): p for p in all_agents}
    if configured_remote_ids is None:
        configured_remote_ids = []
    pending_owners = _ingest_outboxes(senders, all_agents)
    process: dict[str, Participant] = {p.name.lower(): p for p in senders}
    for owner in pending_owners:
        process.setdefault(owner.name.lower(), owner)
    down: set[str] = set()
    routed = 0
    for sender in process.values():
        routed += _process_pending(
            sender, by_name, publish_remotes, configured_remote_ids, services,
            unstarted_remote_ids or [], down,
        )
    return routed


def _inbox_json_files(p: Participant) -> list[Path]:
    inbox = inbox_dir(p.name)
    if not inbox.is_dir():
        return []
    return sorted(f for f in inbox.iterdir() if f.is_file() and f.name.endswith(".json"))


def next_inbox_message(p: Participant) -> Path | None:
    files = _inbox_json_files(p)
    return files[0] if files else None


def peek_inbox_messages(p: Participant, limit: int) -> list[Path]:
    return _inbox_json_files(p)[:limit]


def newest_inbox_mtime(p: Participant) -> datetime | None:
    """Arrival time of the most recent inbox message — what the wake debounce
    measures quiet against. A file listed and then trashed by a wake settling
    concurrently is skipped rather than raising: the daemon loop calls this
    every tick and must not die of a race it can simply ignore."""
    mtimes = []
    for f in _inbox_json_files(p):
        try:
            mtimes.append(f.stat().st_mtime)
        except OSError:
            continue
    if not mtimes:
        return None
    return datetime.fromtimestamp(max(mtimes), tz=timezone.utc)


# ---------- queue helpers (used by cmd_tell, cmd_prompt, cmd_clear) ----------

def _split_content_and_files(raw: str) -> tuple[str, list[dict]]:
    lines = raw.splitlines()
    files: list[dict] = []
    while lines and lines[-1].strip().startswith("FILE:"):
        path = lines.pop().strip()[len("FILE:"):].strip()
        if path:
            files.insert(0, {"filename": Path(path).name, "path": path})
    return "\n".join(lines).rstrip(), files


def _write_outbox(
    sender_name: str,
    sender_root: Path,
    to: str,
    content: str,
    files: list[dict],
    *,
    attachment_sources: list[Path] | None = None,
) -> Path:
    outbox = outbox_dir(sender_root)
    outbox.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    msg_id = new_ulid()
    if attachment_sources:
        from tell import stage_outbox_attachments

        entries = [{"path": str(p)} for p in attachment_sources]
        files = stage_outbox_attachments(outbox, msg_id, entries)
    msg = {
        "id": msg_id,
        "date": now.isoformat().replace("+00:00", "Z"),
        "from": sender_name,
        "to": to,
        "content": content,
        "files": files,
    }
    dest = outbox / f"{msg_id}.json"
    with dest.open("w", encoding="utf-8") as f:
        json.dump(msg, f, indent=2)
    return dest


