"""a8s remote routing — config, one-try publish, receive loop, dedup.

a8s only crosses cluster boundaries on outbound `tell` messages — every
message has a force-stamped agent `from`, no senderless channel exists.
State queries (`logs`, `ls`, `agents`) are strictly local.
This module wires the message side: `~/.config/a8s/network.json` (dict-shaped:
name → {transport, broker, topic, ...}) becomes a list of Transport
instances. The routing pass uses `make_publish_remotes` as its
`route_outboxes(publish_remotes=...)` hook; each running attached_loop
spawns one subscriber thread per remote that calls into
`receive_envelope`. Cluster-wide dedup lives in the `seen` table of the
transactions store.

Transport modules are imported lazily. `load_remotes()` only pulls in
e.g. `transports.mqtt` when it sees a `transport: mqtt` entry in the
config; an a8s install with no remotes never imports paho-mqtt or any
other transport library.

`_build_transport` forwards every key past `transport`/`broker`/`topic`/
`path`/`bucket` to the transport constructor as `**opts`, so adding a new
transport option doesn't require touching this dispatcher — only the
transport's own option-bag handling.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
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
    out_err,
    secrets_config_path,
)
from delivery_receipt import (
    build_delivery_receipt,
    local_node,
    is_control_envelope,
    is_other_stage_receipt,
    parse_delivery_receipt,
)
from registry import resolve_name
from services import StorageService
from transports import OnMessage, PublishAmbiguous, Transport, TransportError
import txlog
from ar3.fsio import atomic_write_text
from ar3.ulid import is_ulid, parse as parse_ulid


RECEIPT_SEEN_DAYS = 1
# Admission reads the sender's clock (ULID mint time); the memory reads this
# machine's. The memory outlasts admission by this much, so a sender running
# ahead by less than this cannot slip a replay through the window's edge; a
# larger skew can.
CLOCK_SKEW_DAYS = 1
_SEEN_EXPIRY_INTERVAL_S = 600.0
_SEEN_EXPIRY_LOCK = threading.Lock()
_seen_expiry_last: float | None = None
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
    msg_id: str,
    recipient: str,
    reason: str,
    remote_id: str = "remote",
) -> None:
    """Envelope observed on a shared remote that resolves to no local agent
    here. Not a failure — some other node on the same topic may own delivery,
    or none does. a8s is best effort, so nothing goes back to the sender either
    way: the miss is a line in this node's own log and nothing more. The
    receiver recorded the id in the seen table before it got here, so the
    machine logs the miss once."""
    _remote_receive_diagnostic(
        msg_id, recipient, reason, remote_id, event="NOT_LOCAL", prefix="REMOTE_SKIP",
    )


def _remote_discarded(
    msg_id: str, recipient: str, reason: str, remote_id: str = "remote",
) -> None:
    """The received envelope itself is unusable (malformed/unsupported).
    Nothing tries it again."""
    _remote_receive_diagnostic(
        msg_id, recipient, reason, remote_id, event="DISCARDED", prefix="REMOTE_DROP",
    )


# ---------- network.json + secrets.json ----------

# Keys that belong in secrets.json, never network.json. Transport-agnostic
# (mqtt user/pass today; any future transport can reuse the same names).
SECRET_SPEC_KEYS = frozenset({"pass", "password"})


def _parse_sections(raw: bytes | None, label: str) -> dict:
    empty = {"remotes": {}, "services": {}}
    if raw is None:
        return empty
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as e:
        out_err(f"WARN: {label} malformed ({e}); treating as empty")
        return empty
    if not isinstance(data, dict):
        return empty
    data.setdefault("remotes", {})
    if not isinstance(data["remotes"], dict):
        data["remotes"] = {}
    data.setdefault("services", {})
    if not isinstance(data["services"], dict):
        data["services"] = {}
    return data


def _load_sections(p, label: str) -> dict:
    try:
        raw = p.read_bytes() if p.is_file() else None
    except OSError as e:
        out_err(f"WARN: {label} malformed ({e}); treating as empty")
        return {"remotes": {}, "services": {}}
    return _parse_sections(raw, label)


def parse_network_config(raw: bytes | None) -> dict:
    return _parse_sections(raw, "network.json")


def parse_secrets_config(raw: bytes | None) -> dict:
    return _parse_sections(raw, "secrets.json")


def load_network_config() -> dict:
    return _load_sections(network_config_path(), "network.json")


def save_network_config(cfg: dict) -> None:
    atomic_write_text(network_config_path(), json.dumps(cfg, indent=2))


def load_secrets_config() -> dict:
    return _load_sections(secrets_config_path(), "secrets.json")


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


#: Key in a secrets.json entry that holds the binding digest. Not a secret
#: key, so the overlay never copies it onto a spec.
SECRET_BINDING_KEY = "bind"

_SECTION_NOUN = {"remotes": "remote", "services": "storage"}


def spec_binding(spec: dict) -> str:
    """Digest of a spec's non-secret fields: the destination a secret was
    stored for. A secret travels with this digest and is used only with a spec
    that hashes to it."""
    public, _ = split_secret_keys(spec)
    blob = json.dumps(public, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def merge_spec_secrets(section: str, name: str, spec: dict, secrets: dict | None = None) -> dict:
    """Overlay secrets.json (and any legacy inline secrets) onto a spec.

    A stored secret applies only when its binding matches ``spec``'s
    destination. network.json and secrets.json are two files, so a reader that
    loaded the spec before a writer replaced both would otherwise pair the old
    destination with the new password. A secret with no binding or a different
    one is dropped and reported; the operator re-enters it. Inline values still
    in network.json remain effective until the next `a8s remote` /
    `a8s storage` rewrite strips them. `section` is "remotes" or "services".
    `secrets` is an already parsed secrets.json; None reads the file.
    """
    merged = dict(spec)
    if secrets is None:
        secrets = load_secrets_config()
    stored = (secrets.get(section) or {}).get(name)
    if isinstance(stored, dict) and any(k in SECRET_SPEC_KEYS for k in stored):
        if stored.get(SECRET_BINDING_KEY) == spec_binding(spec):
            for key, value in stored.items():
                if key in SECRET_SPEC_KEYS:
                    merged[key] = value
        else:
            out_err(
                f"WARN: the stored secret for {_SECTION_NOUN[section]} {name!r} does not "
                "match its destination and is not used; enter it again"
            )
    return merged


def put_spec_secrets(
    section: str, name: str, spec: dict, secrets: dict, prior: dict | None = None
) -> None:
    """Store ``secrets`` for ``name``, bound to the destination in ``spec`` (the
    non-secret fields just written to network.json).

    With no new password, the stored one is kept and rebound to ``spec`` only
    when its binding matches ``prior``, the entry as it stood before this
    rewrite. An unbound or mismatched password is dropped and reported: the
    reader already treats it as missing, and rebinding it would hand it to a
    destination it was never typed for.
    """
    cfg = load_secrets_config()
    prev = cfg[section].get(name)
    merged: dict = {}
    if isinstance(prev, dict):
        kept = {k: v for k, v in prev.items() if k in SECRET_SPEC_KEYS}
        if kept and not secrets:
            if isinstance(prior, dict) and prev.get(SECRET_BINDING_KEY) == spec_binding(prior):
                merged = kept
            else:
                out_err(
                    f"stored password for {_SECTION_NOUN[section]} {name!r} dropped: it is "
                    "not bound to the entry's previous destination; enter it again"
                )
                del cfg[section][name]
                save_secrets_config(cfg)
                return
    merged.update(secrets)
    if not merged:
        return
    merged[SECRET_BINDING_KEY] = spec_binding(spec)
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


def put_remote_secrets(
    name: str, spec: dict, secrets: dict, prior: dict | None = None
) -> None:
    put_spec_secrets("remotes", name, spec, secrets, prior)


def delete_remote_secrets(name: str) -> None:
    delete_spec_secrets("remotes", name)


# Top-level keys in a network.json entry that are not transport options
# (they're consumed by the dispatcher itself before forwarding the rest).
_RESERVED_SPEC_KEYS = {"transport", "broker", "topic", "path", "bucket"}

# Transport kind -> the tier-2 `ar3 deps` group its module needs, so `a8s
# remote` can install it at the verb instead of leaving the first real use to
# fail. A kind absent here needs nothing beyond stdlib and the vendored tier.
# Separate from the storage map of the same shape: the two dispatchers have
# different kind vocabularies, and `s3` naming the same group in both is a
# coincidence of the package, not a shared rule.
_TRANSPORT_DEPS_GROUP = {
    "s3": "a8s-s3",
}


def transport_deps_group_for(kind: str) -> str | None:
    """The `ar3 deps` group `kind`'s transport module needs, or `None`."""
    return _TRANSPORT_DEPS_GROUP.get(kind)


def _build_transport(name: str, spec: dict) -> Transport:
    """Instantiate one Transport from a network.json entry. Forwards every
    key past `transport` / `broker` / `topic` / `path` as `**opts` to the
    transport constructor — each transport handles its own option vocabulary,
    aliases (e.g. `user` → `username`), and rejects unknowns.

    Each kind states its own required fields: a broker and a topic name a
    server, a path names a folder, a bucket and a prefix name the keys an s3
    remote owns, and none is a requirement of any other."""
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
    if kind == "s3":
        bucket = spec.get("bucket")
        if not bucket or not spec.get("prefix"):
            raise ValueError(
                f"remote {name!r}: an s3 transport requires `bucket` and `prefix`"
            )
        from transports.s3 import S3Transport

        return S3Transport(remote_id=name, bucket=bucket, **opts)
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

# Kind -> the tier-2 `ar3 deps` group its service module needs. A kind absent
# here needs nothing beyond stdlib and the vendored tier — the one place a
# new kind that imports a heavy package has to name its group, so `a8s
# storage` can install it instead of failing at first real use.
_KIND_DEPS_GROUP = {
    "s3": "a8s-s3",
}


def deps_group_for(kind: str) -> str | None:
    """The `ar3 deps` group `kind`'s service module needs, or `None` when it
    needs nothing beyond stdlib/vendored deps."""
    return _KIND_DEPS_GROUP.get(kind)


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


# ---------- seen table ----------

def seen_id_contains(ulid: str) -> bool:
    return txlog.seen_contains(ulid)


def _dedup_days() -> int:
    from settings import get_int

    return get_int("dedup_days")


def _expire_seen(now: float) -> None:
    txlog.seen_expire(
        now - (_dedup_days() + CLOCK_SKEW_DAYS) * 86400.0,
        now - (RECEIPT_SEEN_DAYS + CLOCK_SKEW_DAYS) * 86400.0,
    )


def seen_id_append(ulid: str, kind: str = "message") -> bool:
    """Remember a ULID, and at most once per ten minutes drop the ids that
    have aged out. True when this call added the row, and on a store that
    cannot be written; False when the id was already there.

    The insert is the one cross-process winner on a machine: every daemon's
    subscriber meets the same envelope, and only the one whose insert adds
    the row goes on to deliver it."""
    global _seen_expiry_last
    added = txlog.seen_insert(ulid, kind)
    with _SEEN_EXPIRY_LOCK:
        mono = time.monotonic()
        if (
            _seen_expiry_last is not None
            and mono - _seen_expiry_last < _SEEN_EXPIRY_INTERVAL_S
        ):
            return added
        _seen_expiry_last = mono
    _expire_seen(time.time())
    return added


# ---------- send ----------

SENT = "sent"
AMBIGUOUS = "ambiguous"
FAILED = "failed"


def make_publish_remotes(remotes: list[Transport]) -> Callable:
    """Build the `publish_remotes` callable that `route_outboxes` invokes.

    Each call gives every remote one publish and answers
    `{remote_id: (outcome, detail)}`. `SENT` is a remote that took the
    envelope. `AMBIGUOUS` is one whose outcome is unknown (no PUBACK, a link
    that dropped after the send); it counts as sent and is never retried.
    `FAILED` is a definite failure. A remote found without a link goes into
    the pass's `down` set, and a later message in the same pass fails at once
    instead of waiting for that link again."""

    def publish_once(
        msg: dict,
        sender_name: str,
        down: set[str],
    ) -> dict[str, tuple[str, str]]:
        envelope = json.dumps(msg).encode("utf-8")
        recipient = (msg.get("to") or "").strip() or "?"
        preview = _preview(msg.get("content", ""))
        outcomes: dict[str, tuple[str, str]] = {}
        for remote in remotes:
            if remote.id in down and not remote.is_connected():
                outcomes[remote.id] = (FAILED, f"{remote.id}: broker not connected")
                continue
            try:
                remote.publish(envelope)
            except PublishAmbiguous as e:
                outcomes[remote.id] = (AMBIGUOUS, str(e))
                out_agent(
                    sender_name,
                    f"WARN remote {remote.id} publish outcome unknown, counted as sent: {e}",
                )
                continue
            except Exception as e:
                if not remote.is_connected():
                    down.add(remote.id)
                outcomes[remote.id] = (FAILED, str(e))
                kind = "failed" if isinstance(e, TransportError) else "raised"
                out_agent(sender_name, f"WARN remote {remote.id} publish {kind}: {e}")
                continue
            outcomes[remote.id] = (SENT, "")
            out_agent(
                sender_name,
                f"remote {remote.id}: published -> {recipient}: {preview}",
            )
            out(f"{sender_name}: remote {remote.id}: published -> {recipient}")
        return outcomes

    return publish_once


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

    Every daemon on a machine runs its own subscriber over the same registry,
    so one envelope reaches each of them. The seen-table insert names the one
    that handles it: the insert that adds the row wins, and every other
    sighting drops. The row goes in before delivery, so the transport
    consumes the envelope whatever happens next, and a delivery that fails is
    logged and lost.

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
    if _refuse_aged(msg, msg_id, all_agents, remote_id):
        return
    control = is_control_envelope(msg)
    if not seen_id_append(msg_id, kind="receipt" if control else "message"):
        return
    if control:
        _receive_control_envelope(msg, all_agents, remote_id)
        return
    _deliver_envelope(msg, msg_id, all_agents, services, publish_control, remote_id)


def _holds_recipient(msg: dict, all_agents: list[Participant]) -> bool:
    recipient_name = (msg.get("to") or "").strip()
    try:
        _, member_names = resolve_name(recipient_name)
    except (KeyError, ValueError):
        return False
    local = {p.name.lower() for p in all_agents}
    return any(m.lower() in local for m in member_names)


def _refuse_aged(
    msg: dict, msg_id: str, all_agents: list[Participant], remote_id: str
) -> bool:
    """Whether the envelope is older than the dedup memory. The memory has to
    outlast anything a wire can replay, so what it no longer remembers is
    refused rather than delivered a second time. Only mail for an agent this
    node holds leaves a row; a dead envelope for somebody else is nobody's
    business here."""
    age_days = (time.time() - parse_ulid(msg_id)[0] / 1000.0) / 86400.0
    if is_control_envelope(msg):
        return age_days > RECEIPT_SEEN_DAYS
    limit = _dedup_days()
    if age_days <= limit:
        return False
    if not _holds_recipient(msg, all_agents):
        return True
    txlog.log(
        "EXPIRED",
        msg_id=msg_id,
        sender=msg.get("from") or "?",
        recipient=str(msg.get("to") or ""),
        remote=remote_id,
        detail=f"older than dedup_days ({limit} days)",
    )
    return True


def _deliver_envelope(
    msg: dict,
    msg_id: str,
    all_agents: list[Participant],
    services: list[StorageService] | None,
    publish_control: Callable[[bytes], None] | None,
    remote_id: str,
) -> None:
    """The body of `receive_envelope`, run by the one receiver whose insert
    recorded `msg_id`. Each local recipient gets one inbox write."""
    recipient_name = (msg.get("to") or "").strip()
    if not recipient_name:
        return
    by_name = {p.name.lower(): p for p in all_agents}
    try:
        kind, member_names = resolve_name(recipient_name)
    except (KeyError, ValueError):
        _remote_not_local(msg_id, recipient_name, "not in local registry", remote_id)
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
            msg_id, recipient_name, f"{kind} resolved to zero local recipients", remote_id,
        )
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
    # An entry carrying `error` is not one of those: a sender that could not
    # upload publishes it to say the file existed and was lost, and emptying
    # the array around it recreates the silence it was written to break.
    raw_files = msg.get("files") or []
    files_have_storage = any((isinstance(e, dict) and e.get("storage")) for e in raw_files)
    kept = [
        e
        for e in raw_files
        if isinstance(e, dict) and (e.get("storage") or e.get("error"))
    ]
    if len(kept) != len(raw_files):
        out(
            f"WARN: stripped {len(raw_files) - len(kept)} legacy FILE: "
            f"payload(s) from incoming envelope id={msg_id}"
        )
        msg = dict(msg)
        msg["files"] = kept
    sender_label = msg.get("from") or "?"
    preview = _preview(msg.get("content", ""))
    delivered_names: list[str] = []
    delivered_envelopes: list[dict] = []
    for recipient in recipients:
        # Per-recipient download: each recipient has its own `.files/`, so
        # the bytes land in the right place even on alias fan-out. Imported
        # lazily — `mailbox` imports `network`, so a top-level import here
        # would form an import cycle.
        msg_for_recipient = msg
        if files_have_storage:
            from mailbox import _download_files_to_recipient

            # The first fetch does not wait. Everything here runs on the
            # transport's single subscriber worker, so a wait here would hold
            # every later message — including plain text from another sender —
            # behind one unreachable URL. The bounded wait runs off the worker.
            wait_s = _receive_wait_seconds()
            msg_for_recipient = _download_files_to_recipient(
                msg, recipient, services or [],
                wait_s=0,
                announce_failures=wait_s <= 0,
            )
            if _attachments_missing(msg_for_recipient, msg) and wait_s > 0:
                _wait_for_attachments(
                    msg, recipient, services or [], msg_id, sender_label, preview,
                    remote_id, publish_control,
                )
                continue
        if _write_to_inbox(
            msg_for_recipient, recipient, msg_id, sender_label, preview, remote_id
        ):
            delivered_names.append(recipient.name)
            delivered_envelopes.append(msg_for_recipient)
            _report_attachment_outcome(
                msg, msg_for_recipient, recipient, publish_control, remote_id
            )
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
    if delivered_names and publish_control is not None:
        _publish_delivery_receipt(msg, delivered_names, publish_control, remote_id)


def _attachment_failures(delivered: dict) -> list[tuple[str, str]]:
    """The (name, reason) of each file a delivered envelope does not hold. The
    download rewrites an entry it could not fetch with `error`, so the
    envelope the recipient got is the record of what it got."""
    failed: list[tuple[str, str]] = []
    for entry in delivered.get("files") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("filename") or "")
        if name and entry.get("error"):
            failed.append((name, str(entry.get("detail") or entry.get("error"))))
    return failed


def _report_attachment_outcome(
    original: dict,
    delivered: dict,
    recipient: Participant,
    publish_control: Callable[[bytes], None] | None,
    remote_id: str,
) -> None:
    """Record each file this recipient will never hold, and tell the sender's
    node.

    Per recipient, because the download is per recipient: on an alias fan-out
    one seat can hold the bytes while another holds the reason it does not."""
    if not (original.get("files") or []):
        return
    msg_id = str(original.get("id") or "")
    failed = _attachment_failures(delivered)
    for name, reason in failed:
        txlog.log(
            "ATTACHMENT_FAILED",
            msg_id=msg_id,
            sender=original.get("from") or "?",
            recipient=recipient.name,
            files=[name],
            remote=remote_id,
            detail=reason,
        )
    if failed and publish_control is not None:
        _publish_delivery_receipt(
            original, [recipient.name], publish_control, remote_id,
            stage="attachment_failed",
            files=[name for name, _ in failed],
            detail="; ".join(reason for _, reason in failed),
        )


def _receive_wait_seconds() -> int:
    from settings import get_setting

    try:
        return max(0, int(get_setting("storage_receive_wait_seconds")))
    except (TypeError, ValueError):
        return 0


def _attachments_missing(msg: dict, source: dict | None = None) -> bool:
    """Whether anything worth waiting for is still missing.

    A sender that could not upload publishes an entry carrying `error` and no
    `storage`. That loss is already final — there is no URL to fetch — so
    waiting on it would hold the message for the whole fetch wait and then
    deliver exactly what was available at the start. Only an entry the sender
    gave us somewhere to fetch from counts as worth the wait; pass `source` to
    make that distinction, omit it to ask the flat question.
    """
    fetchable: set | None = None
    if source is not None:
        fetchable = {
            e.get("filename")
            for e in (source.get("files") or [])
            if isinstance(e, dict) and e.get("storage")
        }
    return any(
        e.get("error") and (fetchable is None or e.get("filename") in fetchable)
        for e in (msg.get("files") or [])
    )


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
    message is there (already present counts); False when the write failed
    and the message was dropped for this recipient."""
    # ensure_mailboxes lives in mailbox.py; importing it here would form
    # a cycle. Just create dirs.
    try:
        inbox_dir(recipient.name).mkdir(parents=True, exist_ok=True)
        inbox_tmp_dir(recipient.name).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _dropped(msg_id, sender_label, recipient, remote_id, f"inbox write failed: {e}")
        return False
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
    # `date` is when the sender queued it; `delivered_at` is when it landed.
    # Without both, a replayed message and live traffic look identical to
    # anything reading the inbox.
    msg_for_recipient = dict(msg_for_recipient)
    msg_for_recipient["delivered_at"] = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    try:
        with staging.open("w", encoding="utf-8") as f:
            json.dump(msg_for_recipient, f, indent=2)
        os.replace(str(staging), str(final))
    except OSError as e:
        _dropped(msg_id, sender_label, recipient, remote_id, f"inbox write failed: {e}")
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


def _dropped(
    msg_id: str, sender_label: str, recipient: Participant, remote_id: str, reason: str,
) -> None:
    """A received message that will not reach this recipient. The seen row is
    already written, so nothing here tries it again."""
    out_agent(recipient.name, f"WARN dropped incoming envelope id={msg_id}: {reason}")
    txlog.log(
        "DROPPED",
        msg_id=msg_id,
        sender=sender_label,
        recipient=recipient.name,
        remote=remote_id,
        detail=reason,
    )


# The bounded attachment wait runs here instead of on the subscriber worker.
# Bounded so a burst of slow attachments cannot spawn a thread apiece; queued
# waits still run, just later.
_ATTACHMENT_WAIT_WORKERS = 4
_attachment_pool: "ThreadPoolExecutor | None" = None
_attachment_pool_lock = threading.Lock()
_attachment_futures: list = []


def _get_attachment_pool() -> ThreadPoolExecutor:
    global _attachment_pool
    with _attachment_pool_lock:
        if _attachment_pool is None:
            _attachment_pool = ThreadPoolExecutor(
                max_workers=_ATTACHMENT_WAIT_WORKERS,
                thread_name_prefix="a8s-attach",
            )
        return _attachment_pool


def drain_attachment_waits(timeout_s: float | None = None) -> bool:
    """Block until every attachment wait has finished.

    Returns False if `timeout_s` expired first. For callers that need the
    inbox settled before looking at it: tests, and a shutdown that would
    rather not abandon a live download."""
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


def _wait_for_attachments(
    msg: dict,
    recipient: Participant,
    services: list[StorageService],
    msg_id: str,
    sender_label: str,
    preview: str,
    remote_id: str,
    publish_control: Callable[[bytes], None] | None,
) -> None:
    """Deliver once the attachments resolve or the bounded fetch wait ends.

    The message stays out of the inbox until then — an agent woken for a file
    it cannot open burns tokens hunting for it — and the wait runs off the
    subscriber worker so unrelated mail flows. The seen row is already written
    and the transport consumes the envelope as soon as the worker returns, so
    a restart during the wait loses this message."""
    out_agent(
        recipient.name,
        f"attachment(s) for id={msg_id} not ready; waiting in the background",
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
            _report_attachment_outcome(
                msg, resolved, recipient, publish_control, remote_id
            )
        except Exception as e:
            _dropped(msg_id, sender_label, recipient, remote_id, f"attachment wait failed: {e}")

    try:
        future = _get_attachment_pool().submit(finish)
    except RuntimeError as e:
        _dropped(msg_id, sender_label, recipient, remote_id, f"attachment wait not started: {e}")
        return
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


_RECEIPT_LOG_LINE = {
    "inbox_write": "delivered",
    "attachment_failed": "attachment delivery failed",
}


def _receive_control_envelope(
    message: dict,
    all_agents: list[Participant],
    remote_id: str,
) -> None:
    receipt = parse_delivery_receipt(message)
    if receipt is None:
        if is_other_stage_receipt(message):
            return
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
    line = _RECEIPT_LOG_LINE.get(receipt.stage)
    if line is not None:
        out_agent(
            local_sender.name,
            f"{line} id={receipt.for_id} -> {recipients}",
        )
    node = f"; node={receipt.node}" if receipt.node else ""
    txlog.log(
        "DELIVERY_RECEIPT",
        msg_id=receipt.for_id,
        sender=local_sender.name,
        recipient=recipients,
        files=list(receipt.files) or None,
        remote=remote_id,
        detail=f"{receipt.stage}{node}; receipt_id={receipt.receipt_id}",
    )
    if receipt.stage == "attachment_failed":
        txlog.log(
            "ATTACHMENT_FAILED",
            msg_id=receipt.for_id,
            sender=local_sender.name,
            recipient=recipients,
            files=list(receipt.files) or None,
            remote=remote_id,
            detail=receipt.detail or receipt.stage,
        )


def _publish_delivery_receipt(
    original: dict,
    delivered_names: list[str],
    publish_control: Callable[[bytes], None],
    remote_id: str,
    *,
    stage: str = "inbox_write",
    files: list[str] | None = None,
    detail: str = "",
) -> None:
    receipt = build_delivery_receipt(
        original, delivered_names, stage, files=files, detail=detail,
        node=local_node(),
    )
    if receipt is None:
        return
    try:
        publish_control(json.dumps(receipt).encode("utf-8"))
    except Exception as e:
        out(
            f"WARN remote {remote_id} delivery receipt publish failed; "
            f"dropped 1 receipt id={original.get('id', '?')}: {e}"
        )
        return
    txlog.log(
        "RECEIPT_PUBLISHED",
        msg_id=original["id"],
        sender=original.get("from") or "?",
        recipient=",".join(delivered_names),
        remote=remote_id,
        files=files or None,
        detail=f"{stage}; receipt_id={receipt['id']}",
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
    content-free delivery receipts on that same transport.

    The transport consumes every envelope it hands this callback, whatever
    the callback does with it."""

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
                publish_control=r.publish_control,
                remote_id=r.id,
            )
            r.start(cb)
            started.append(r)
            if r.is_connected():
                out(f"remote {r.id}: subscriber started")
            else:
                out(f"remote {r.id}: subscriber started; link not up yet")
        except Exception as e:
            out(f"WARN: remote {r.id} failed to start: {e}")
    return started


def stop_remotes(remotes: list[Transport]) -> None:
    for r in remotes:
        try:
            r.stop()
        except Exception as e:
            out(f"WARN: remote {r.id} stop raised: {e}")
