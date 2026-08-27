"""a8s remote routing — config, publish-with-backoff, receive loop, dedup.

a8s only crosses cluster boundaries on outbound `tell` messages — every
message has a force-stamped agent `from`, no senderless channel exists.
State queries (`logs`, `ls`, `agents`) are strictly local.
This module wires the message side: `~/.config/a8s/network.json` (dict-shaped:
name → {transport, broker, topic, ...}) becomes a list of Transport
instances. The routing pass uses `publish_with_backoff` as its
`route_outboxes(publish_remotes=...)` hook; each running attached_loop
spawns one subscriber thread per remote that calls into
`receive_envelope`. Cluster-wide dedup lives in the seen-ids ring file
at `~/.config/a8s/seen-ids`.

Transport modules are imported lazily. `load_remotes()` only pulls in
e.g. `transports.mqtt` when it sees a `transport: mqtt` entry in the
config; an a8s install with no remotes never imports paho-mqtt or any
other transport library.

`_build_transport` forwards every key past `transport`/`broker`/`topic`/
`path` to the transport constructor as `**opts`, so adding a new transport
option doesn't require touching this dispatcher — only the transport's
own option-bag handling.
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from core import (
    Participant,
    _preview,
    inbox_dir,
    inbox_tmp_dir,
    network_config_path,
    out,
    out_agent,
    secrets_config_path,
    seen_ids_path,
)
from delivery_receipt import (
    build_delivery_receipt,
    is_control_envelope,
    parse_delivery_receipt,
)
from registry import resolve_name
from services import StorageService
from transports import OnMessage, Transport, TransportError
import txlog
from ark.fsio import atomic_write_text
from ark.ulid import is_ulid


# Process-local lock guarding the seen-ids ring rotation. Multiple subscriber
# threads (one per remote) call seen_id_append concurrently; the append
# itself is atomic per POSIX, but the truncate-after-rotate is not.
_SEEN_IDS_LOCK = threading.Lock()
_REMOTE_DIAGNOSTIC_LOCK = threading.Lock()
_REMOTE_DIAGNOSTIC_LAST: dict[tuple[str, str, str], float] = {}
_REMOTE_DIAGNOSTIC_INTERVAL_S = 300.0
_REMOTE_DIAGNOSTIC_MAX_KEYS = 256


def _remote_receive_diagnostic(
    msg_id: str,
    recipient: str,
    reason: str,
    remote_id: str,
    *,
    event: txlog.Event,
    prefix: str,
) -> None:
    """Rate-limited shared-topic receive diagnostic; never includes content.

    NOT_LOCAL: the envelope resolved to no local agent on this node — a
    normal outcome on a shared topic, since some other node owns delivery.
    DROPPED is reserved for terminal paths where the message will not be
    delivered by anyone; this helper never logs that event.
    """
    key = (remote_id, reason, recipient.lower())
    now = time.monotonic()
    with _REMOTE_DIAGNOSTIC_LOCK:
        last = _REMOTE_DIAGNOSTIC_LAST.get(key)
        if last is not None and now - last < _REMOTE_DIAGNOSTIC_INTERVAL_S:
            return
        _REMOTE_DIAGNOSTIC_LAST[key] = now
        if len(_REMOTE_DIAGNOSTIC_LAST) > _REMOTE_DIAGNOSTIC_MAX_KEYS:
            oldest = min(_REMOTE_DIAGNOSTIC_LAST, key=_REMOTE_DIAGNOSTIC_LAST.get)
            del _REMOTE_DIAGNOSTIC_LAST[oldest]
    out(f"{prefix} id={msg_id} to={recipient!r} reason={reason}")
    txlog.log(
        event,
        msg_id=msg_id,
        recipient=recipient,
        remote=remote_id,
        detail=reason,
    )


def _remote_not_local(
    msg_id: str, recipient: str, reason: str, remote_id: str = "remote",
) -> None:
    """Envelope observed on a shared remote that resolves to no local agent
    here. Not a failure — some other node on the same topic owns delivery."""
    _remote_receive_diagnostic(
        msg_id, recipient, reason, remote_id, event="NOT_LOCAL", prefix="REMOTE_SKIP",
    )


def _remote_discarded(
    msg_id: str, recipient: str, reason: str, remote_id: str = "remote",
) -> None:
    """The received envelope itself is unusable (malformed/unsupported).
    Nothing will retry it from this observation."""
    _remote_receive_diagnostic(
        msg_id, recipient, reason, remote_id, event="DISCARDED", prefix="REMOTE_DROP",
    )


# ---------- network.json + secrets.json ----------

# Keys that belong in secrets.json, never network.json. Transport-agnostic
# (mqtt user/pass today; any future transport can reuse the same names).
SECRET_SPEC_KEYS = frozenset({"pass", "password"})


def load_network_config() -> dict:
    p = network_config_path()
    if not p.is_file():
        return {"remotes": {}, "services": {}}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        out(f"WARN: network.json malformed ({e}); treating as empty")
        return {"remotes": {}, "services": {}}
    if not isinstance(data, dict):
        return {"remotes": {}, "services": {}}
    data.setdefault("remotes", {})
    if not isinstance(data["remotes"], dict):
        data["remotes"] = {}
    data.setdefault("services", {})
    if not isinstance(data["services"], dict):
        data["services"] = {}
    return data


def save_network_config(cfg: dict) -> None:
    p = network_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def load_secrets_config() -> dict:
    p = secrets_config_path()
    if not p.is_file():
        return {"remotes": {}, "services": {}}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        out(f"WARN: secrets.json malformed ({e}); treating as empty")
        return {"remotes": {}, "services": {}}
    if not isinstance(data, dict):
        return {"remotes": {}, "services": {}}
    data.setdefault("remotes", {})
    if not isinstance(data["remotes"], dict):
        data["remotes"] = {}
    data.setdefault("services", {})
    if not isinstance(data["services"], dict):
        data["services"] = {}
    return data


def save_secrets_config(cfg: dict) -> None:
    """Atomic write of secrets.json with mode 0600."""
    payload = json.dumps(cfg, indent=2) + "\n"
    atomic_write_text(secrets_config_path(), payload, fsync=True, mode=0o600)


def split_secret_keys(spec: dict) -> tuple[dict, dict]:
    """Split a remote/service spec into (public, secrets) dicts."""
    public: dict = {}
    secrets: dict = {}
    for key, value in spec.items():
        if key in SECRET_SPEC_KEYS:
            secrets[key] = value
        else:
            public[key] = value
    return public, secrets


def merge_spec_secrets(section: str, name: str, spec: dict) -> dict:
    """Overlay secrets.json (and any legacy inline secrets) onto a spec.

    secrets.json wins for secret keys. Inline values still in network.json
    remain effective until the next `a8s remote` / `a8s storage` rewrite
    strips them. `section` is "remotes" or "services".
    """
    merged = dict(spec)
    stored = (load_secrets_config().get(section) or {}).get(name)
    if isinstance(stored, dict):
        for key, value in stored.items():
            if key in SECRET_SPEC_KEYS:
                merged[key] = value
    return merged


def put_spec_secrets(section: str, name: str, secrets: dict) -> None:
    """Merge ``secrets`` into secrets.json for ``name`` (no-op if empty)."""
    if not secrets:
        return
    cfg = load_secrets_config()
    prev = cfg[section].get(name)
    merged = dict(prev) if isinstance(prev, dict) else {}
    merged.update(secrets)
    cfg[section][name] = merged
    save_secrets_config(cfg)


def delete_spec_secrets(section: str, name: str) -> None:
    cfg = load_secrets_config()
    if name not in cfg[section]:
        return
    del cfg[section][name]
    save_secrets_config(cfg)


def merge_remote_secrets(name: str, spec: dict) -> dict:
    return merge_spec_secrets("remotes", name, spec)


def put_remote_secrets(name: str, secrets: dict) -> None:
    put_spec_secrets("remotes", name, secrets)


def delete_remote_secrets(name: str) -> None:
    delete_spec_secrets("remotes", name)


# Top-level keys in a network.json entry that are not transport options
# (they're consumed by the dispatcher itself before forwarding the rest).
_RESERVED_SPEC_KEYS = {"transport", "broker", "topic", "path"}


def _build_transport(name: str, spec: dict) -> Transport:
    """Instantiate one Transport from a network.json entry. Forwards every
    key past `transport` / `broker` / `topic` / `path` as `**opts` to the
    transport constructor — each transport handles its own option vocabulary,
    aliases (e.g. `user` → `username`), and rejects unknowns.

    Each kind states its own required fields: a broker and a topic name a
    server, a path names a folder, and neither is a requirement of the other."""
    kind = (spec.get("transport") or "").strip().lower()
    opts = {k: v for k, v in spec.items() if k not in _RESERVED_SPEC_KEYS}
    if kind == "mqtt":
        broker = spec.get("broker")
        topic = spec.get("topic")
        if not broker or not topic:
            raise ValueError(f"remote {name!r}: an mqtt transport requires `broker` and `topic`")
        # Lazy import — keeps paho out of the import graph for users with no
        # remotes configured.
        from transports.mqtt import MqttTransport

        return MqttTransport(remote_id=name, broker=broker, topic=topic, **opts)
    if kind == "folder":
        path = spec.get("path")
        if not path:
            raise ValueError(f"remote {name!r}: a folder transport requires `path`")
        from transports.folder import FolderTransport

        return FolderTransport(remote_id=name, path=path, **opts)
    raise ValueError(f"remote {name!r}: unsupported transport {kind!r}")


def load_remotes(node: str | None = None, overrides: dict | None = None) -> list[Transport]:
    """Return Transport instances for every configured remote.

    Merges ``secrets.json`` secret keys into each network.json spec before
    building the transport. Failures are logged and skipped — never block
    a8s startup.

    ``node`` names the caller's node identity (the attached agent set) and
    becomes part of each transport's default client id, so two nodes on one
    host never contend for the same broker session. ``overrides`` is merged
    into every spec last; a short-lived probe uses it to take a throwaway
    identity instead of a node's durable one.
    """
    cfg = load_network_config()
    out_list: list[Transport] = []
    for name, spec in cfg["remotes"].items():
        if not isinstance(spec, dict):
            out(f"WARN: remote {name!r} config is not an object; skipping")
            continue
        try:
            merged = merge_remote_secrets(name, spec)
            if node:
                merged = {**merged, "node_tag": node}
            if overrides:
                merged = {**merged, **overrides}
            out_list.append(_build_transport(name, merged))
        except Exception as e:
            out(f"WARN: remote {name!r} skipped: {e}")
    return out_list


def configured_remote_ids() -> list[str]:
    """Just the ordered list of remote IDs from network.json. Used by the
    routing pass to know which remotes to wait on without paying the cost
    of building the full Transport instances."""
    return list(load_network_config()["remotes"].keys())


# ---------- storage services ----------

# Provenance written by `a8s remote`: the name of the folder remote whose
# registration created this service. It is config-layer bookkeeping rather than
# a service option — nothing a StorageService can act on — so it is reserved
# here and stripped before the constructor sees the spec.
PAIRED_KEY = "paired"

# Top-level keys in a network.json `services` entry that the dispatcher
# consumes itself before forwarding the rest to the StorageService constructor.
_RESERVED_SERVICE_SPEC_KEYS = {"service", "url", PAIRED_KEY}


def _normalize_opt_key(key: str) -> str:
    """`--base-url` and `--base_url` name the same option. Services declare
    their vocabulary in Python identifiers, so dashes fold to underscores."""
    return key.replace("-", "_")


def _build_service(name: str, spec: dict) -> StorageService:
    """Instantiate one StorageService from a network.json `services` entry.

    The persisted `service` field is the canonical kind name (e.g.
    `tempfile_org`). The dispatcher imports each known service class
    lazily so the import graph stays empty for installs without storage
    configured. Any keys past `service` and `url` are forwarded as
    `**opts`; each service class handles its own option vocabulary
    and rejects unknowns at construction time."""
    kind = (spec.get("service") or "").strip().lower()
    url = spec.get("url")
    if not url:
        raise ValueError(f"storage {name!r}: every service requires `url`")
    opts = {
        _normalize_opt_key(k): v
        for k, v in spec.items()
        if k not in _RESERVED_SERVICE_SPEC_KEYS
    }
    if kind == "tempfile_org":
        # Lazy import — keeps the storage modules out of the import graph
        # for users without storage configured.
        from services.tempfile_org import TempFileOrgService

        return TempFileOrgService(name, url=url, **opts)
    if kind == "s3":
        from services.s3 import S3Service

        return S3Service(name, url=url, **opts)
    if kind == "file_sync":
        from services.file_sync import FileSyncService

        return FileSyncService(name, url=url, **opts)
    if kind == "webdav":
        from services.webdav import WebdavService

        return WebdavService(name, url=url, **opts)
    if kind == "rclone":
        from services.rclone import RcloneService

        return RcloneService(name, url=url, **opts)
    if kind == "sync_folder":
        from services.sync_folder import SyncFolderService

        return SyncFolderService(name, url=url, **opts)
    raise ValueError(f"storage {name!r}: unsupported service kind {kind!r}")


#: Built services, and the config stamp they were built from. A daemon runs
#: for days, so a service configured after it started must still become usable
#: — the same reason the receive path re-reads the participant list. Rebuilding
#: is keyed on the config file's identity rather than done every pass, because
#: a service constructor can read secrets and touch the filesystem.
_SERVICE_CACHE: tuple[tuple, list[StorageService]] | None = None


def _service_config_stamp() -> tuple:
    """What has to change before the built services are stale."""
    marks = []
    for path in (network_config_path(), secrets_config_path()):
        # The path itself is part of the stamp: a8s tests relocate the config
        # home, and two homes that both lack the file would otherwise look
        # identical and share a cached answer.
        try:
            st = path.stat()
            marks.append((str(path), st.st_mtime_ns, st.st_size))
        except OSError:
            marks.append((str(path), None, None))
    return tuple(marks)


def load_services() -> list[StorageService]:
    """Return StorageService instances for every entry in
    `network.json`'s `services` map. Failures (bad config, missing
    module) are logged and skipped — never block a8s startup.

    Cheap to call repeatedly: the built list is reused until the config or
    the secrets file changes underneath it."""
    global _SERVICE_CACHE
    stamp = _service_config_stamp()
    if _SERVICE_CACHE is not None and _SERVICE_CACHE[0] == stamp:
        return _SERVICE_CACHE[1]
    cfg = load_network_config()
    out_list: list[StorageService] = []
    for name, spec in cfg["services"].items():
        if not isinstance(spec, dict):
            out(f"WARN: storage {name!r} config is not an object; skipping")
            continue
        try:
            out_list.append(
                _build_service(name, merge_spec_secrets("services", name, spec))
            )
        except Exception as e:
            out(f"WARN: storage {name!r} skipped: {e}")
    _SERVICE_CACHE = (stamp, out_list)
    return out_list


def configured_service_ids() -> list[str]:
    """Just the ordered list of service IDs from network.json. Used by the
    routing pass to know which services need uploads before remote publish
    can finalize."""
    return list(load_network_config()["services"].keys())


def detect_service_kind(url: str) -> str | None:
    """Find the canonical service kind for an operator-typed URL by asking
    each known StorageService subclass `supports_config_url`. Returns the
    canonical kind string (e.g. `tempfile_org`) or None if no service
    accepted the URL. Used by the `a8s storage` CLI to persist the right
    `service` field at config-write time."""
    # Lazy imports keep the storage modules out of the import graph for
    # installs without storage configured.
    from services.file_sync import FileSyncService
    from services.rclone import RcloneService
    from services.s3 import S3Service
    from services.sync_folder import SyncFolderService
    from services.tempfile_org import TempFileOrgService
    from services.webdav import WebdavService

    for kind, cls in (
        ("tempfile_org", TempFileOrgService),
        ("s3", S3Service),
        ("file_sync", FileSyncService),
        ("webdav", WebdavService),
        ("rclone", RcloneService),
        ("sync_folder", SyncFolderService),
    ):
        try:
            if cls.supports_config_url(url):
                return kind
        except Exception:
            continue
    return None


# ---------- seen-ids ring ----------

# How long a claim may sit before another receiver may take it over. The claim
# covers one delivery attempt — a single download pass plus the inbox writes —
# so this only has to outlast a network timeout, not the deferred retry window.
# A process killed mid-delivery leaves a claim behind; past this it is assumed
# dead and the message becomes deliverable again rather than lost.
CLAIM_STALE_SECONDS = 300


def _claims_dir() -> Path:
    return seen_ids_path().parent / "claims"


def claim_message(ulid: str) -> bool:
    """Take exclusive responsibility for delivering `ulid`, cluster-wide.

    Every daemon on a machine runs its own subscriber and resolves recipients
    from the shared registry, so one envelope arrives at every one of them and
    any of them will deliver it. The seen-ids ring alone cannot arbitrate that:
    it is read on entry and written after delivery, and everything in between —
    a download attempt above all — is time in which a second receiver reads a
    ring that does not mention this message yet and starts delivering it too.

    So the claim is taken up front and atomically. `O_CREAT | O_EXCL` is one
    filesystem operation with one winner, which is what the process-local lock
    around the ring cannot be.

    False means somebody else has it; the caller drops the envelope silently,
    exactly as it would for a duplicate.
    """
    d = _claims_dir()
    path = d / ulid
    try:
        d.mkdir(parents=True, exist_ok=True)
        os.close(os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        return True
    except FileExistsError:
        pass
    except OSError:
        # Claiming is an optimisation over the ring, never a gate on delivery.
        # If the directory cannot be written, deliver and accept the duplicate.
        return True
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return True  # vanished between the two calls: the holder finished
    if age <= CLAIM_STALE_SECONDS:
        return False
    # The holder is gone. Re-stamp before taking over so that two processes
    # racing an expiry do not both conclude they won.
    try:
        prior = path.stat().st_mtime
        os.utime(path, None)
        return path.stat().st_mtime != prior
    except OSError:
        return False


def release_claim(ulid: str) -> None:
    """Give up a claim. Called once the message is durably recorded in the
    ring, and on every path that ends without delivering."""
    try:
        (_claims_dir() / ulid).unlink(missing_ok=True)
    except OSError:
        pass


def sweep_stale_claims() -> None:
    """Drop claim files left behind by processes that died mid-delivery."""
    cutoff = time.time() - CLAIM_STALE_SECONDS
    try:
        entries = list(_claims_dir().iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
        except OSError:
            continue


def seen_id_contains(ulid: str) -> bool:
    p = seen_ids_path()
    if not p.is_file():
        return False
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip() == ulid:
                    return True
    except OSError:
        pass
    return False


def _max_seen_ids() -> int:
    from settings import get_int

    return get_int("max_seen_ids")


def seen_id_append(ulid: str) -> None:
    """Append a ULID to the ring, rotating to the last max_seen_ids entries
    when the file grows past the cap. Best-effort — disk failures don't
    propagate (a missed append just means we might re-deliver a duplicate)."""
    with _SEEN_IDS_LOCK:
        p = seen_ids_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(ulid + "\n")
        except OSError:
            return
        # Rotation check.
        try:
            with p.open("r", encoding="utf-8") as f:
                lines = [ln.rstrip("\n") for ln in f if ln.strip()]
        except OSError:
            return
        if len(lines) > _max_seen_ids():
            tmp = p.with_suffix(p.suffix + ".tmp")
            try:
                with tmp.open("w", encoding="utf-8") as out_f:
                    for u in lines[-_max_seen_ids():]:
                        out_f.write(u + "\n")
                os.replace(str(tmp), str(p))
            except OSError:
                pass


# ---------- send (publish_with_backoff) ----------

def make_publish_remotes(remotes: list[Transport]) -> Callable:
    """Build the `publish_remotes` callable that `route_outboxes` invokes.
    For each not-yet-succeeded remote, attempts a publish; on success logs to
    the sender's per-agent log and the global log (so diagnosing an outage
    doesn't require reading every agent's log to rule out a quiet success);
    on failure logs a warning to the per-agent log and leaves the remote in
    the `pending_remotes` set for the next pass. Returns the updated
    `succeeded_remotes` list."""

    def publish_with_backoff(
        msg: dict,
        sender_name: str,
        succeeded_so_far: list[str],
        attempt_count: int,
    ) -> list[str]:
        envelope = json.dumps(msg).encode("utf-8")
        recipient = (msg.get("to") or "").strip() or "?"
        preview = _preview(msg.get("content", ""))
        succeeded = list(succeeded_so_far)
        for remote in remotes:
            if remote.id in succeeded:
                continue
            try:
                remote.publish(envelope)
                succeeded.append(remote.id)
                out_agent(
                    sender_name,
                    f"remote {remote.id}: published -> {recipient}: {preview}",
                )
                out(f"{sender_name}: remote {remote.id}: published -> {recipient}")
            except TransportError as e:
                out_agent(
                    sender_name,
                    f"WARN remote {remote.id} publish failed (attempt {attempt_count + 1}): {e}",
                )
            except Exception as e:
                out_agent(
                    sender_name,
                    f"WARN remote {remote.id} publish raised (attempt {attempt_count + 1}): {e}",
                )
        return succeeded

    return publish_with_backoff


# ---------- receive ----------

def receive_envelope(
    envelope: bytes,
    all_agents: list[Participant],
    services: list[StorageService] | None = None,
    publish_control: Callable[[bytes], None] | None = None,
    remote_id: str = "remote",
) -> None:
    """Decode an incoming envelope, dedupe, filter against the local
    registry, and atomically write into each matched local recipient's
    inbox. Unknown local destinations emit bounded, rate-limited diagnostics;
    malformed or duplicate envelopes drop silently. Nothing should crash the
    subscriber thread.

    `services`: configured storage services. When set and the
    envelope's `files[i].storage` URLs point at a service we know, the
    helper downloads each file into the recipient's `<root>/.files/` and
    rewrites the entry to local `{filename, path}` shape. None / empty
    falls back to the v1 limitation (strip files; log warning).

    `publish_control` is an optional same-transport publisher for internal
    receipt envelopes. Omitting it preserves receive-only behavior."""
    try:
        msg = json.loads(envelope)
        if not isinstance(msg, dict):
            raise ValueError("envelope is not a JSON object")
    except (ValueError, UnicodeDecodeError) as e:
        out(f"WARN: dropped malformed envelope ({e})")
        return
    msg_id = msg.get("id", "")
    if not isinstance(msg_id, str) or not is_ulid(msg_id):
        out(f"WARN: envelope without valid id; dropping (id={msg_id!r})")
        return
    if seen_id_contains(msg_id):
        return  # already delivered — silent dedup
    if not claim_message(msg_id):
        return  # a sibling receiver has it in flight — same silent dedup
    try:
        _deliver_claimed_envelope(
            msg, msg_id, all_agents, services, publish_control, remote_id
        )
    except BaseException:
        # Nothing was recorded, so the message must stay deliverable — by this
        # receiver's transport redelivery or by a sibling.
        release_claim(msg_id)
        raise


def _deliver_claimed_envelope(
    msg: dict,
    msg_id: str,
    all_agents: list[Participant],
    services: list[StorageService] | None,
    publish_control: Callable[[bytes], None] | None,
    remote_id: str,
) -> None:
    """The body of `receive_envelope`, run while holding the claim on `msg_id`.

    Every path out of here either records the message in the seen-ids ring and
    releases the claim, or releases the claim so somebody can try again.
    """
    if is_control_envelope(msg):
        _receive_control_envelope(msg, all_agents, remote_id)
        seen_id_append(msg_id)
        release_claim(msg_id)
        return
    recipient_name = (msg.get("to") or "").strip()
    if not recipient_name:
        release_claim(msg_id)
        return  # malformed; nothing to filter on
    by_name = {p.name.lower(): p for p in all_agents}
    try:
        kind, member_names = resolve_name(recipient_name)
    except (KeyError, ValueError):
        _remote_not_local(msg_id, recipient_name, "not in local registry", remote_id)
        release_claim(msg_id)
        return
    recipients: list[Participant] = []
    for m in member_names:
        rp = by_name.get(m.lower())
        if rp is not None:
            # No sender exclusion here: the sender lives on a different
            # cluster and its name (if it happens to also be a local agent)
            # is the dual-name foot-gun. Per the design, deliver locally.
            recipients.append(rp)
    if not recipients:
        _remote_not_local(
            msg_id,
            recipient_name,
            f"{kind} resolved to zero local recipients",
            remote_id,
        )
        release_claim(msg_id)
        return
    txlog.log(
        "RESOLVED_REMOTE",
        msg_id=msg_id,
        sender=msg.get("from") or "?",
        recipient=",".join(recipient.name for recipient in recipients),
        remote=remote_id,
        detail=f"{kind} resolved to {len(recipients)} local recipient(s)",
    )
    # File payloads: when the envelope carries `files[i].storage` URLs,
    # download into the recipient's `.files/` and rewrite to local-path shape.
    # Configured storage services are tried first; http(s) URLs then fall back
    # to a plain GET so presigned links need no receiver-side credentials.
    # Legacy envelopes with filename-only entries (no `storage`) are stripped.
    raw_files = msg.get("files") or []
    files_have_storage = any((isinstance(e, dict) and e.get("storage")) for e in raw_files)
    if raw_files and not files_have_storage:
        out(f"WARN: stripped FILE: payloads from incoming envelope id={msg_id}")
        msg = dict(msg)
        msg["files"] = []
    sender_label = msg.get("from") or "?"
    preview = _preview(msg.get("content", ""))
    delivered_names: list[str] = []
    delivered_envelopes: list[dict] = []
    deferred: list[Participant] = []
    for recipient in recipients:
        # Per-recipient download: each recipient has its own `.files/`, so
        # the bytes land in the right place even on alias fan-out. Imported
        # lazily — `mailbox` imports `network`, so a top-level import here
        # would form an import cycle.
        msg_for_recipient = msg
        if files_have_storage:
            from mailbox import _download_files_to_recipient

            # One attempt only. Everything here runs on the transport's single
            # subscriber worker, so a retry loop that sleeps would hold every
            # later message — including plain text from another sender —
            # behind one unreachable URL.
            wait_s = _receive_wait_seconds()
            msg_for_recipient = _download_files_to_recipient(
                msg, recipient, services or [],
                wait_s=0,
                announce_failures=wait_s <= 0,
            )
            if _attachments_missing(msg_for_recipient) and wait_s > 0:
                deferred.append(recipient)
                continue
        if _write_to_inbox(
            msg_for_recipient, recipient, msg_id, sender_label, preview, remote_id
        ):
            delivered_names.append(recipient.name)
            delivered_envelopes.append(msg_for_recipient)
    if delivered_names:
        import convo

        # The archive must see what the recipients actually got. A failed
        # download rewrites its own file entries with `error`/`detail`, and
        # recording `msg` here would file a lost attachment under the same
        # line a delivered one produces. One call, because one message id is
        # one row: splitting it dropped every group after the first.
        convo.record(
            _worst_attachment_outcome(delivered_envelopes),
            recipients=delivered_names,
        )
    seen_id_append(msg_id)
    release_claim(msg_id)
    if delivered_names and publish_control is not None:
        _publish_delivery_receipt(msg, delivered_names, publish_control, remote_id)
    for recipient in deferred:
        _submit_deferred_delivery(
            msg, recipient, services or [], msg_id, sender_label, preview,
            remote_id, publish_control,
        )


def _receive_wait_seconds() -> int:
    from settings import get_setting

    try:
        return max(0, int(get_setting("storage_receive_wait_seconds")))
    except (TypeError, ValueError):
        return 0


def _attachments_missing(msg: dict) -> bool:
    return any(e.get("error") for e in (msg.get("files") or []))


def _worst_attachment_outcome(envelopes: list[dict]) -> dict:
    """One envelope reporting the least fortunate copy of each file.

    The download runs per recipient, so an alias fan-out can end with one
    recipient holding bytes and another holding an error. The archive keeps
    one row per message id and cannot hold both truths, so the row reports a
    file as lost when any recipient's copy was lost. That direction is chosen
    deliberately: a lost file described as delivered sends a reader hunting
    for something that was never written, which is the whole of #222, while
    the reverse only sends them to check a file they already have.

    Copies pair by filename, never by position. `_download_files_to_recipient`
    appends each success as it lands and every failure afterwards, so two
    recipients list the same files in different orders precisely when their
    outcomes differ — which is the only case this function is called to
    resolve. Pairing by index there overwrites one recipient's delivered file
    with another's lost one, dropping a name out of the archive entirely.

    Per-recipient outcomes are the real model and are #225.
    """
    base = dict(envelopes[0])
    files = [dict(e) if isinstance(e, dict) else e for e in (base.get("files") or [])]
    at = {
        e["filename"]: i
        for i, e in enumerate(files)
        if isinstance(e, dict) and e.get("filename")
    }
    for env in envelopes[1:]:
        for other in env.get("files") or []:
            if not isinstance(other, dict) or not other.get("error"):
                continue
            name = other.get("filename")
            if not name:
                continue
            i = at.get(name)
            if i is None:
                at[name] = len(files)
                files.append(dict(other))
            elif isinstance(files[i], dict) and not files[i].get("error"):
                files[i] = dict(other)
    base["files"] = files
    return base


def _write_to_inbox(
    msg_for_recipient: dict,
    recipient: Participant,
    msg_id: str,
    sender_label: str,
    preview: str,
    remote_id: str,
) -> bool:
    """Atomically land one envelope in one recipient's inbox. True when the
    message is there (already present counts)."""
    # ensure_mailboxes lives in mailbox.py; importing it here would form
    # a cycle. Just create dirs.
    inbox_dir(recipient.name).mkdir(parents=True, exist_ok=True)
    inbox_tmp_dir(recipient.name).mkdir(parents=True, exist_ok=True)
    final = inbox_dir(recipient.name) / f"{msg_id}.json"
    if final.is_file():
        txlog.log(
            "RECEIVED_REMOTE",
            msg_id=msg_id,
            sender=sender_label,
            recipient=recipient.name,
            remote=remote_id,
            detail="inbox already contained envelope",
        )
        return True
    staging = inbox_tmp_dir(recipient.name) / f"{msg_id}.json"
    try:
        with staging.open("w", encoding="utf-8") as f:
            json.dump(msg_for_recipient, f, indent=2)
        os.replace(str(staging), str(final))
    except OSError as e:
        out_agent(recipient.name, f"WARN failed to write incoming envelope id={msg_id}: {e}")
        return False
    out_agent(recipient.name, f"received from {sender_label} (via remote): {preview}")
    file_names = [
        e.get("filename", "")
        for e in (msg_for_recipient.get("files") or [])
        if e.get("filename")
    ]
    txlog.log(
        "RECEIVED_REMOTE",
        msg_id=msg_id,
        sender=sender_label,
        recipient=recipient.name,
        files=file_names or None,
        remote=remote_id,
        detail="inbox write complete",
    )
    return True


# Attachment retries run here instead of on the subscriber worker. Bounded so a
# burst of undeliverable attachments cannot spawn a thread apiece; queued work
# still runs, just later. Daemon threads — a retry must never hold up exit.
_ATTACHMENT_RETRY_WORKERS = 4
_attachment_pool: "ThreadPoolExecutor | None" = None
_attachment_pool_lock = threading.Lock()
_attachment_futures: list = []


def _get_attachment_pool() -> ThreadPoolExecutor:
    global _attachment_pool
    with _attachment_pool_lock:
        if _attachment_pool is None:
            _attachment_pool = ThreadPoolExecutor(
                max_workers=_ATTACHMENT_RETRY_WORKERS,
                thread_name_prefix="a8s-attach",
            )
        return _attachment_pool


def drain_attachment_retries(timeout_s: float | None = None) -> bool:
    """Block until every deferred attachment delivery has finished.

    Returns False if `timeout_s` expired first. Deliveries are durable either
    way — this is for callers that need the inbox settled before looking at
    it (tests, and a shutdown that would rather not abandon a live download)."""
    deadline = None if timeout_s is None else time.monotonic() + timeout_s
    with _attachment_pool_lock:
        pending = [f for f in _attachment_futures if not f.done()]
    for future in pending:
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            return False
        try:
            future.result(timeout=remaining)
        except TimeoutError:
            return False
        except Exception:
            pass
    return True


def _submit_deferred_delivery(
    msg: dict,
    recipient: Participant,
    services: list[StorageService],
    msg_id: str,
    sender_label: str,
    preview: str,
    remote_id: str,
    publish_control: Callable[[bytes], None] | None,
) -> None:
    """Finish a delivery whose attachments were not ready on the first try.

    The message is held out of the inbox until its files resolve — an agent
    woken for a file it cannot open burns tokens hunting for it — but the
    waiting happens off the subscriber worker so unrelated mail flows."""
    out_agent(
        recipient.name,
        f"attachment(s) for id={msg_id} not ready; retrying in the background",
    )

    def finish() -> None:
        from mailbox import _download_files_to_recipient

        try:
            resolved = _download_files_to_recipient(msg, recipient, services)
            if not _write_to_inbox(
                resolved, recipient, msg_id, sender_label, preview, remote_id
            ):
                return
            import convo

            # `resolved`, not `msg`: this path exists because the first
            # download failed, so it is the one most likely to be recording a
            # file that never arrived.
            convo.record(resolved, recipients=[recipient.name])
            if publish_control is not None:
                _publish_delivery_receipt(
                    msg, [recipient.name], publish_control, remote_id
                )
        except Exception as e:
            out_agent(
                recipient.name,
                f"WARN deferred attachment delivery failed for id={msg_id}: {e}",
            )

    future = _get_attachment_pool().submit(finish)
    with _attachment_pool_lock:
        _attachment_futures[:] = [f for f in _attachment_futures if not f.done()]
        _attachment_futures.append(future)


def _receipt_sender(sender: str, all_agents: list[Participant]) -> Participant | None:
    """The local agent a receipt belongs to. A node that owns a namespace sends
    under the bare prefix, so the name on the wire is an address, not an agent —
    resolve it through the binding before giving up on the confirmation."""
    by_name = {agent.name.lower(): agent for agent in all_agents}
    local = by_name.get(sender.lower())
    if local is not None:
        return local
    try:
        kind, bound = resolve_name(sender)
    except (KeyError, ValueError):
        return None
    return by_name.get(bound[0].lower()) if kind == "namespace" else None


def _receive_control_envelope(
    message: dict,
    all_agents: list[Participant],
    remote_id: str,
) -> None:
    receipt = parse_delivery_receipt(message)
    if receipt is None:
        _remote_discarded(
            str(message.get("id", "?")),
            str(message.get("to", "?")),
            "unsupported or malformed a8s control envelope",
            remote_id,
        )
        return
    local_sender = _receipt_sender(receipt.sender, all_agents)
    if local_sender is None:
        return
    recipients = ",".join(receipt.recipients)
    out_agent(
        local_sender.name,
        f"delivery confirmed id={receipt.for_id} -> {recipients} ({receipt.stage})",
    )
    txlog.log(
        "DELIVERY_RECEIPT",
        msg_id=receipt.for_id,
        sender=local_sender.name,
        recipient=recipients,
        remote=remote_id,
        detail=f"{receipt.stage}; receipt_id={receipt.receipt_id}",
    )


def _publish_delivery_receipt(
    original: dict,
    delivered_names: list[str],
    publish_control: Callable[[bytes], None],
    remote_id: str,
) -> None:
    receipt = build_delivery_receipt(original, delivered_names)
    if receipt is None:
        return
    try:
        publish_control(json.dumps(receipt).encode("utf-8"))
        txlog.log(
            "RECEIPT_PUBLISHED",
            msg_id=original["id"],
            sender=original.get("from") or "?",
            recipient=",".join(delivered_names),
            remote=remote_id,
            detail=f"receipt_id={receipt['id']}",
        )
    except Exception as e:
        out(
            f"WARN remote {remote_id} delivery receipt publish failed "
            f"(fire-and-forget, not retried) id={original.get('id', '?')}: {e}"
        )


def make_receive_callback(
    get_participants: Callable[[], list[Participant]],
    services: list[StorageService] | None = None,
    publish_control: Callable[[bytes], None] | None = None,
    remote_id: str = "remote",
) -> OnMessage:
    """Wrap `receive_envelope` so the subscriber thread always passes the
    CURRENT participant list — agents added via `a8s add` after the
    subscriber started are picked up without restarting the loop. Storage
    services are resolved the same way and for the same reason: a
    daemon that has been up for days must be able to download an attachment
    through a service configured this morning. Passing an explicit list
    pins it instead, which is what the tests want. `publish_control` enables
    content-free delivery receipts on that same transport."""

    def callback(envelope: bytes) -> None:
        try:
            receive_envelope(
                envelope,
                get_participants(),
                services=load_services() if services is None else services,
                publish_control=publish_control,
                remote_id=remote_id,
            )
        except Exception as e:
            out(f"WARN: receive_envelope raised: {e}")

    return callback


# ---------- lifecycle ----------

def start_remotes(
    remotes: list[Transport],
    get_participants: Callable[[], list[Participant]],
    services: list[StorageService] | None = None,
) -> list[Transport]:
    """Start every remote's subscriber loop. A failure to start one remote
    logs a warning and continues with the others — no remote is allowed to
    block a8s startup. Returns the list of successfully-started remotes.

    `services` is passed through to the receive callback so cross-cluster
    `FILE:` payloads can be downloaded into each recipient's
    `.files/` as envelopes arrive. None / empty strips incoming files and
    warns instead."""
    started: list[Transport] = []
    for r in remotes:
        try:
            cb = make_receive_callback(
                get_participants,
                services=services,
                publish_control=r.publish,
                remote_id=r.id,
            )
            r.start(cb)
            started.append(r)
            out(f"remote {r.id}: subscriber started")
        except Exception as e:
            out(f"WARN: remote {r.id} failed to start: {e}")
    return started


def stop_remotes(remotes: list[Transport]) -> None:
    for r in remotes:
        try:
            r.stop()
        except Exception as e:
            out(f"WARN: remote {r.id} stop raised: {e}")
