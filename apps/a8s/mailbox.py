"""a8s mailbox — inbox/outbox/trash routing and queue helpers.

Mailbox routing is process-agnostic: a per-agent daemon may write into any
other agent's inbox even though it isn't handling them. Only `wake_once` (in
daemon.py) requires the handler attachment.

Routing runs in two phases per pass (issue #63 prep):

1. INGEST — atomically move `<root>/.outbox/<f>.json` into
   `~/.a8s/agents/<sender>/pending/<f>.json`. This is the only thing a8s
   ever does to a file in `<root>/.outbox/` — the agent's directory is
   one-way (agent writes, a8s renames out, never read-modify-write). After
   this phase the agent's outbox dir is empty for the duration of the pass.

2. PROCESS — for each pending file, load (or initialize) a `<f>.json.retry`
   sidecar that tracks attempts, next-attempt time, which configured remotes
   already accepted the publish, and whether local delivery has happened.
   Local delivery uses the existing maildir-style staging (`inbox.tmp/` →
   `inbox/` via `os.replace`). Remote publishing is the chunk-7 hook —
   the no-remote path stays semantically identical to the pre-#63 code:
   deliver locally, unlink.

Backoff / retry (issue #63): when some configured remote hasn't yet
accepted, attempts increment and the sidecar's `next_attempt` is bumped
according to `BACKOFF_SCHEDULE`. After `MAX_ATTEMPTS` failures the
message is moved to trash with a "discarded after backoff exhausted"
log.

Atomic alias fan-out (issue #67): each routed copy is written into
`<recipient>/inbox.tmp/<source-fname>` first, then renamed into
`<recipient>/inbox/` only after every recipient has staged. A crash mid-
fan-out leaves no partial state. Source filename (a ULID) is preserved
across staging so a recipient whose final inbox already has the file is
skipped — retries are idempotent.

FILE: payload transfer (issue #62): tell stages outgoing attachments in
`.outbox/<msg_id>/` (basename only in the JSON). Ingest moves each bundle
with its envelope into pending; routing copies into each recipient's
`.files/<msg_id>/`. Envelope `path` fields are invalid.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from core import (
    BACKOFF_SCHEDULE,
    MAX_ATTEMPTS,
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
    retry_sidecar_path,
    trash_dir,
    unique_path,
)
from network import seen_id_append
from registry import load_namespaces, resolve_name, opaque_prefixes
from services import StorageError, StorageService
import txlog
from ulid import new as new_ulid


def _max_file_bytes() -> int:
    from settings import get_int

    return get_int("max_file_bytes")

# A function that publishes one routed-and-from-stamped message envelope to
# every configured remote that hasn't yet accepted it. Returns the updated
# `succeeded_remotes` list. Daemon wires this up at startup; passing None
# keeps the path purely local (no remotes configured).
PublishRemotes = Callable[[dict, str, list[str], int], list[str]]


# ---------- mailboxes ----------

def ensure_mailboxes(p: Participant) -> None:
    """Create mailbox dirs for `p`. Inbox, inbox.tmp, pending, and trash live
    under ~/.a8s/ (hidden from the agent); outbox lives at the resolved
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
    if filename != Path(filename).name:
        return None, f"filename {filename!r} is not a basename"
    bundle = pending_bundle_dir(sender_name, msg_id).resolve()
    try:
        src = (bundle / filename).resolve()
        src.relative_to(bundle)
    except ValueError:
        return None, f"path escapes pending bundle {bundle}"
    except (OSError, RuntimeError) as e:
        return None, f"cannot resolve {bundle / filename}: {e}"
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

    Strict opacity (#69, #70): the `to` field is left at whatever the sender
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


def _commit_staged_inboxes(staged: list[tuple[Path, Path]]) -> None:
    """Atomically promote each `(staging, final)` pair from `inbox.tmp/` into
    `inbox/`. Uses `os.replace` so the rename is atomic on POSIX even if the
    final already exists (re-route after partial crash). Skips entries whose
    staging file is already gone (already promoted by a prior partial commit)."""
    for staging, final in staged:
        if not staging.is_file():
            continue
        os.replace(str(staging), str(final))


# ---------- routing ----------

def _ingest_outboxes(senders: list[Participant]) -> None:
    """Phase 1: atomically move every `<root>/.outbox/<id>.json` and its
    `<root>/.outbox/<id>/` attachment bundle into pending."""
    for sender in senders:
        ensure_mailboxes(sender)
        outbox = sender.outbox_path()
        if not outbox.is_dir():
            continue
        dest_dir = pending_dir(sender.name)
        for f in sorted(outbox.iterdir()):
            if not (f.is_file() and f.name.endswith(".json")):
                continue
            msg_id = _msg_id_from_pending_json(f.name)
            dest = dest_dir / f.name
            try:
                os.rename(str(f), str(dest))
            except OSError:
                try:
                    shutil.copy2(str(f), str(dest))
                    f.unlink()
                except OSError as e:
                    out_agent(sender.name, f"ingest copy failed on {f.name}: {e}")
                    continue
            bundle_src = outbox_bundle_dir(outbox, msg_id)
            bundle_dest = pending_bundle_dir(sender.name, msg_id)
            try:
                _move_dir(bundle_src, bundle_dest)
            except OSError as e:
                out_agent(sender.name, f"ingest bundle move failed for {msg_id}: {e}")


def _load_or_init_sidecar(pending_file: Path) -> dict:
    p = retry_sidecar_path(pending_file)
    if p.is_file():
        try:
            with p.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
            if isinstance(data, dict):
                data.setdefault("attempts", 0)
                data.setdefault("next_attempt", "")
                data.setdefault("succeeded_remotes", [])
                data.setdefault("local_delivered", False)
                data.setdefault("uploaded", {})
                return data
        except (OSError, json.JSONDecodeError):
            pass  # corrupt sidecar — start fresh
    return {
        "attempts": 0,
        "next_attempt": "",
        "succeeded_remotes": [],
        "local_delivered": False,
        # Per-file × per-service upload cache for cross-cluster `FILE:`
        # payloads. Shape: {"<filename>": {"<service_id>": "<download_url>"}}.
        # A backoff retry only re-uploads to services still missing.
        "uploaded": {},
    }


def _save_sidecar(pending_file: Path, sidecar: dict) -> None:
    p = retry_sidecar_path(pending_file)
    try:
        with p.open("w", encoding="utf-8") as fp:
            json.dump(sidecar, fp, indent=2)
    except OSError:
        pass  # best-effort — bad write means we'll retry on the next pass


def _drop_sidecar(pending_file: Path) -> None:
    p = retry_sidecar_path(pending_file)
    try:
        p.unlink()
    except OSError:
        pass


def _trash_pending(sender: Participant, pending_file: Path) -> None:
    """Retire a dead pending message into the agent's trash — envelope and
    attachment bundle both. Attachment bytes are the sender's only copy once
    ingest has moved them out of the outbox, so a failed delivery parks them
    for recovery instead of destroying them."""
    trash = trash_dir(sender.name)
    trash.mkdir(parents=True, exist_ok=True)
    bad = unique_path(trash / pending_file.name)
    try:
        pending_file.rename(bad)
    except OSError:
        pass
    msg_id = _msg_id_from_pending_json(pending_file.name)
    bundle = pending_bundle_dir(sender.name, msg_id)
    if bundle.is_dir():
        _move_dir(bundle, unique_path(trash / msg_id))


def _finalize_pending(sender: Participant, pending_file: Path) -> None:
    msg_id = _msg_id_from_pending_json(pending_file.name)
    try:
        pending_file.unlink()
    except OSError:
        pass
    _drop_sidecar(pending_file)
    _remove_dir(pending_bundle_dir(sender.name, msg_id))


def _schedule_retry(
    pending_file: Path,
    sidecar: dict,
    sender: Participant,
    *,
    msg_id: str,
    recipient: str,
    files: list[str],
    reason: str,
) -> None:
    """Increment attempts and either set the next-attempt time per
    BACKOFF_SCHEDULE or trash the message if the schedule is exhausted.

    Exhaustion is the end of a message's life, so it is recorded where a
    sender can find it after the fact: the agent log, and a `DISCARDED` row
    naming the last failure so `a8s trace <ULID>` explains the death."""
    sidecar["attempts"] += 1
    if sidecar["attempts"] > MAX_ATTEMPTS:
        out_agent(
            sender.name,
            f"discarded {pending_file.name} after backoff exhausted: {reason}",
        )
        txlog.log(
            "DISCARDED",
            msg_id=msg_id,
            sender=sender.name,
            recipient=recipient,
            files=files or None,
            detail=f"backoff exhausted after {MAX_ATTEMPTS} attempts; last failure: {reason}",
        )
        _trash_pending(sender, pending_file)
        _drop_sidecar(pending_file)
        return
    delay_idx = min(sidecar["attempts"] - 1, len(BACKOFF_SCHEDULE) - 1)
    next_dt = datetime.now(timezone.utc) + timedelta(seconds=BACKOFF_SCHEDULE[delay_idx])
    sidecar["next_attempt"] = next_dt.isoformat().replace("+00:00", "Z")
    _save_sidecar(pending_file, sidecar)


def _resolve_file_source(sender_name: str, msg_id: str, entry: dict) -> Path | None:
    return _resolve_pending_attachment(sender_name, msg_id, entry)


def _record_upload_failure(
    msg: dict, sender: Participant, sidecar: dict, filename: str, detail: str
) -> None:
    """One attachment-upload failure, on all three surfaces: the agent log for
    a tailing operator, a `FILE_UPLOAD_FAILED` row so `a8s trace` sees it, and
    the sidecar so an eventual discard can name what kept killing the send.
    Fires once per attempt per service — the attempt number is in the detail,
    so a repeated network fault reads as a sequence rather than a duplicate."""
    out_agent(sender.name, f"FILE: upload failed for {filename!r}: {detail}; will retry")
    txlog.log(
        "FILE_UPLOAD_FAILED",
        msg_id=msg.get("id", ""),
        sender=sender.name,
        recipient=str(msg.get("to") or "").strip(),
        files=[filename],
        detail=detail,
    )
    sidecar["last_error"] = detail


def _upload_files_for_remote(
    msg: dict,
    sender: Participant,
    services: list[StorageService],
    sidecar: dict,
) -> bool:
    """Upload every file in `msg["files"]` to every configured storage
    service that hasn't yet accepted it. Caches results in
    `sidecar["uploaded"][filename][service_id] = url` so a backoff retry
    only re-uploads to services still missing.

    Side-effects:
      - Mutates `sidecar["uploaded"]` with each successful upload.
      - On full success (every file × every service covered), rewrites
        `msg["files"]` in-place to the wire shape:
        `[{"filename": ..., "storage": [url1, url2, ...]}]` (drops `path`).

    Returns True if every file is now covered by every configured service
    (caller proceeds to publish). Returns False if any upload failed
    (caller schedules retry; msg["files"] stays in its on-disk shape)."""
    files = msg.get("files") or []
    if not files or not services:
        return True  # no work to do

    uploaded: dict = sidecar.setdefault("uploaded", {})
    all_done = True
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
        if src is None and any(s.id not in per_file for s in services):
            _record_upload_failure(msg, sender, sidecar, filename, src_reason)
            all_done = False
            continue
        attempt = sidecar.get("attempts", 0) + 1
        for service in services:
            if service.id in per_file:
                continue
            try:
                url = service.store(src)  # type: ignore[arg-type]
            except StorageError as e:
                _record_upload_failure(
                    msg, sender, sidecar, filename,
                    f"storage {service.id} upload failed (attempt {attempt}): {e}",
                )
                all_done = False
                continue
            except Exception as e:
                _record_upload_failure(
                    msg, sender, sidecar, filename,
                    f"storage {service.id} upload raised (attempt {attempt}): {e}",
                )
                all_done = False
                continue
            per_file[service.id] = url

    if not all_done:
        return False
    sidecar.pop("last_error", None)

    # Every file is covered by every service — rewrite to the wire shape.
    new_files: list[dict] = []
    for entry in files:
        filename = entry.get("filename") or ""
        urls = list(uploaded.get(filename, {}).values())
        new_files.append({"filename": filename, "storage": urls})
    msg["files"] = new_files
    return True


def _download_files_to_recipient(
    msg: dict,
    recipient: Participant,
    services: list[StorageService],
) -> dict:
    """Pull `msg["files"][i]["storage"]` URLs into `<root>/.files/<msg_id>/`.
    Returns a NEW dict with filename-only `files` entries."""
    out_msg = dict(msg)
    msg_id = (msg.get("id") or "").strip()
    src_files = msg.get("files") or []
    new_files: list[dict] = []
    if not msg_id:
        out_msg["files"] = []
        return out_msg
    dest_root = recipient.files_bundle_dir(msg_id)
    dest_root.mkdir(parents=True, exist_ok=True)
    for entry in src_files:
        filename = entry.get("filename") or ""
        urls = entry.get("storage") or []
        if not filename or not urls:
            continue
        dest = dest_root / filename
        delivered = False
        for url in urls:
            for service in services:
                try:
                    if service.retrieve(url, dest):
                        delivered = True
                        break
                except StorageError as e:
                    out_agent(
                        recipient.name,
                        f"WARN storage {service.id} download failed for {filename!r}: {e}",
                    )
                    # Real failure on a matched URL — try next URL/service.
            if delivered:
                break
        if delivered:
            new_files.append({"filename": filename})
        else:
            out_agent(
                recipient.name,
                f"WARN no configured storage service could download {filename!r} (urls={urls})",
            )
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
) -> int:
    """Phase 2: iterate `~/.a8s/agents/<sender>/pending/`, deliver each
    pending file locally and/or publish to not-yet-succeeded remotes. The
    sidecar tracks per-message progress so a partial pass can be resumed
    cheaply on the next routing iteration. Returns the count of completed
    local deliveries this pass (matches the legacy `route_outboxes` count
    for the local-only path)."""
    routed = 0
    pending = pending_dir(sender.name)
    if not pending.is_dir():
        return 0
    owned_prefixes = {
        p.lower(): p for p, a in load_namespaces().items()
        if a.lower() == sender.name.lower()
    }
    opaque = opaque_prefixes()
    now = datetime.now(timezone.utc)
    files = sorted(
        f for f in pending.iterdir()
        if f.is_file() and f.name.endswith(".json")
    )
    for f in files:
        sidecar = _load_or_init_sidecar(f)
        # Backoff gate. A sidecar is operator-editable, so `next_attempt` is a
        # boundary: anything unparseable — wrong type included — means due now,
        # never an exception that would abort the whole pass and strand every
        # message behind this one, inbound proxy delivery included.
        raw_next_attempt = sidecar["next_attempt"]
        if raw_next_attempt:
            try:
                next_dt = datetime.fromisoformat(str(raw_next_attempt).replace("Z", "+00:00"))
                if now < next_dt:
                    continue
            except (TypeError, ValueError):
                out_agent(
                    sender.name,
                    f"unparseable next_attempt {raw_next_attempt!r} in {f.name} sidecar; treating as due",
                )
        try:
            with f.open("r", encoding="utf-8") as fp:
                msg = json.load(fp)
        except (OSError, json.JSONDecodeError) as e:
            out_agent(sender.name, f"pending parse error on {f.name}: {e}; trashing")
            _trash_pending(sender, f)
            _drop_sidecar(f)
            continue
        recipient_name = (msg.get("to") or "").strip()
        msg["from"] = _stamp_from(
            sender.name, str(msg.get("from") or "").strip(),
            recipient_name, owned_prefixes, opaque,
        )
        preview = _preview(msg.get("content", ""))
        msg_files = [e.get("filename", "") for e in (msg.get("files") or []) if e.get("filename")]
        if not recipient_name:
            out_agent(sender.name, f"empty 'to' in {f.name}; rejecting")
            _trash_pending(sender, f)
            _drop_sidecar(f)
            continue
        # ----- LOCAL ROUTING -----
        local_target_known = False
        if not sidecar["local_delivered"]:
            kind = None
            member_names: list[str] = []
            try:
                kind, member_names = resolve_name(recipient_name)
                local_target_known = True
            except KeyError:
                pass  # unknown locally — remotes (if any) might still deliver
            except ValueError as e:
                out_agent(sender.name, f"{e} in {f.name}; trashing")
                _trash_pending(sender, f)
                _drop_sidecar(f)
                continue
            if local_target_known:
                recipients: list[Participant] = []
                for m in member_names:
                    rp = by_name.get(m.lower())
                    if rp is not None and rp.name != sender.name:
                        recipients.append(rp)
                if recipients:
                    staged: list[tuple[Path, Path]] = []
                    stage_failed = False
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
                            staged.append((staging, final))
                    except OSError as e:
                        out_agent(sender.name, f"stage failed on {f.name}: {e}")
                        for staging, _ in staged:
                            try:
                                staging.unlink()
                            except OSError:
                                pass
                        stage_failed = True
                    if not stage_failed:
                        try:
                            if staged:
                                _commit_staged_inboxes(staged)
                            if staged:
                                sidecar["local_delivered"] = True
                                local_msg_id = msg.get("id", "")
                                if local_msg_id:
                                    seen_id_append(local_msg_id)
                                msg_id = msg.get("id", "")
                                delivered = len(staged)
                                if kind == "alias":
                                    out_agent(sender.name, f"routed: {sender.name} -> {recipient_name} (alias of {len(recipients)}): {preview}")
                                    for recipient in recipients:
                                        out_agent(recipient.name, f"received from {sender.name} (via {recipient_name} alias): {preview}")
                                        txlog.log("ROUTED", msg_id=msg_id, sender=sender.name, recipient=recipient.name, files=msg_files or None, detail=preview)
                                    routed += delivered
                                elif kind == "namespace":
                                    recipient = recipients[0]
                                    out_agent(sender.name, f"routed: {sender.name} -> {recipient_name} (namespace via {recipient.name}): {preview}")
                                    if staged:
                                        out_agent(recipient.name, f"received from {sender.name} (to {recipient_name}): {preview}")
                                    txlog.log("ROUTED", msg_id=msg_id, sender=sender.name, recipient=recipient.name, files=msg_files or None, detail=preview)
                                    routed += delivered
                                else:
                                    recipient = recipients[0]
                                    out_agent(sender.name, f"routed: {sender.name} -> {recipient.name}: {preview}")
                                    if staged:
                                        out_agent(recipient.name, f"received from {sender.name}: {preview}")
                                    txlog.log("ROUTED", msg_id=msg_id, sender=sender.name, recipient=recipient.name, files=msg_files or None, detail=preview)
                                    routed += delivered
                                import convo

                                delivered_names = [
                                    r.name for r in recipients
                                    if (inbox_dir(r.name) / f.name).is_file()
                                ]
                                convo.record(msg, recipients=delivered_names)
                        except OSError as e:
                            out_agent(sender.name, f"commit failed on {f.name}: {e}")
                            # leave for retry
                else:
                    # Local target resolved but no recipients (alias with only
                    # the sender as a member, or the sender targeting themselves).
                    # Nothing to deliver locally; mark done so we don't loop.
                    out_agent(sender.name, f"{recipient_name!r} has no local recipients (excluding self)")
                    sidecar["local_delivered"] = True
        # ----- REMOTE ROUTING -----
        has_files = bool(msg.get("files"))
        if publish_remotes is not None and configured_remote_ids:
            services_list = services or []
            if has_files and not services_list:
                # No storage services configured — file payloads can't make
                # the trip. Mark all remotes "succeeded" so the message
                # finalizes after local delivery instead of looping on
                # retries (#62 v1 fallback: local-only with files when no
                # services, full pipeline when services are configured).
                if sidecar["attempts"] == 0:
                    out_agent(sender.name, f"FILE: payloads in {f.name} not published to remotes (no storage configured)")
                sidecar["succeeded_remotes"] = list(configured_remote_ids)
            else:
                # Upload any files first (per-file × per-service cache lives
                # in the sidecar). On full success the in-memory msg gets
                # rewritten to the wire shape; on partial failure we fall
                # through to schedule a retry without publishing this pass.
                publish_msg = dict(msg)
                upload_ok = True
                if has_files:
                    publish_msg["files"] = list(msg.get("files") or [])
                    upload_ok = _upload_files_for_remote(
                        publish_msg, sender, services_list, sidecar,
                    )
                if upload_ok:
                    prev_remotes = list(sidecar["succeeded_remotes"])
                    sidecar["succeeded_remotes"] = publish_remotes(
                        publish_msg, sender.name, prev_remotes, sidecar["attempts"]
                    )
                    newly_published = [r for r in sidecar["succeeded_remotes"] if r not in prev_remotes]
                    for rid in newly_published:
                        txlog.log("PUBLISHED", msg_id=msg.get("id", ""), sender=sender.name, recipient=recipient_name, remote=rid, files=msg_files if has_files else None, detail=preview)
                # else: leave succeeded_remotes alone; backoff retry will
                # finish the uploads and try the publish next pass.
        # ----- OUTCOME -----
        remaining_remotes = [
            rid for rid in configured_remote_ids
            if rid not in sidecar["succeeded_remotes"]
        ]
        no_path_at_all = (
            not local_target_known
            and not sidecar["local_delivered"]
            and not configured_remote_ids
        )
        if no_path_at_all:
            out_agent(sender.name, f"unknown recipient {recipient_name!r} in {f.name}; trashing")
            txlog.log("DROPPED", msg_id=msg.get("id", ""), sender=sender.name, recipient=recipient_name, detail="unknown recipient")
            _trash_pending(sender, f)
            _drop_sidecar(f)
            continue
        all_done = (
            not remaining_remotes
            and (not local_target_known or sidecar["local_delivered"])
        )
        if all_done:
            remote_published = bool(sidecar["succeeded_remotes"])
            if has_files and not (services or []):
                # v1 fallback: remotes marked succeeded without a wire publish.
                remote_published = False
            if remote_published:
                import convo

                convo.record(msg, recipients=[recipient_name])
            _finalize_pending(sender, f)
            continue
        # Still pending — schedule a retry with backoff.
        if remaining_remotes:
            stalled = f"remotes did not accept: {', '.join(remaining_remotes)}"
        else:
            stalled = "local delivery incomplete"
        _schedule_retry(
            f, sidecar, sender,
            msg_id=msg.get("id", ""),
            recipient=recipient_name,
            files=msg_files,
            reason=sidecar.get("last_error") or stalled,
        )
    return routed


def route_outboxes(
    senders: list[Participant],
    all_agents: list[Participant] | None = None,
    publish_remotes: Optional[PublishRemotes] = None,
    configured_remote_ids: Optional[list[str]] = None,
    services: Optional[list[StorageService]] = None,
) -> int:
    """Two-phase routing pass:

      1. Ingest: move new outbox files out of every sender's `<root>/.outbox/`
         and into `~/.a8s/agents/<sender>/pending/`. The agent's directory is
         touched only by the rename — never read-modified-rewritten.
      2. Process: deliver each pending message to local recipients (via
         `inbox.tmp/` → `inbox/` atomic stage→commit) and/or publish to any
         configured remote that hasn't yet accepted it. Per-message retry
         state lives in `<f>.json.retry` alongside the pending file.

    `publish_remotes` and `configured_remote_ids` are the daemon-wired
    hooks for cross-cluster routing; both default to None / [] so an
    install with no remotes configured behaves identically to pre-#63
    except for the on-disk location of in-flight messages.

    `services` is the storage-service hook (#90) for cross-cluster `FILE:`
    payloads. When set and a message has files, each service uploads its
    bytes and the wire envelope carries `files[i].storage = [...]`. None /
    empty falls back to the v1 limitation (files local-only, remote skip)."""
    if all_agents is None:
        all_agents = senders
    by_name = {p.name.lower(): p for p in all_agents}
    if configured_remote_ids is None:
        configured_remote_ids = []
    _ingest_outboxes(senders)
    routed = 0
    for sender in senders:
        routed += _process_pending(
            sender, by_name, publish_remotes, configured_remote_ids, services,
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


