"""S3 Transport — envelopes through a bucket, for a machine that only has 443.

Some machines reach HTTPS and nothing else. An egress proxy allows `CONNECT`
on 443 to a list of domains and refuses every other port, which rules out both
of the other wires: mqtt wants a broker on 1883 or 8883, and a folder wants a
filesystem two machines share. An S3 bucket needs one allowed domain on 443,
which is the whole ask, and the machine pulls rather than listening — nothing
inbound has to reach it:

  a8s remote hub s3://my-bucket/a8s-mail --region us-west-2

The wire is one object per message, named for the message's own ULID and
holding the same envelope bytes MQTT would carry, under a key prefix per
recipient:

  <prefix>/<recipient>/<ULID>.json     one envelope
  <prefix>/<recipient>/<ULID>/<file>   that message's attachments

A poll is therefore one `list_objects_v2` per mailbox with a `/` delimiter —
no filtering, and no attachment object in the answer — and ULIDs sort
lexically, so the listing already comes back in send order.

**Every machine that holds a name gets a copy, as MQTT gives every
subscriber one.** A name may live on several machines. Each machine whose
registry holds a name polls `<prefix>/<name>/`, and none of them deletes what it
reads: the first reader cannot know how many others are still offline. So each
machine keeps its own ledger of consumed ULIDs under its config home
(`transports/ledger.py`, shared with the folder transport), a poll skips every
ULID the ledger names without fetching it, and the object stays on the wire
for everyone else. The mailbox set is every agent, alias and namespace prefix
in the local registry, read fresh on every poll so `a8s add` needs no restart.

A publish goes on the wire whatever this node holds. The routing pass has
already delivered to a local recipient and recorded the ULID in the seen-ids
ring, exactly as it has before an MQTT publish; this node's own poll then
meets the object the way an MQTT client meets the broker's echo, the receive
path answers from the ring, and the ledger takes the ULID.

A node is not a process. Every daemon on the machine runs its own subscriber
over the same registry, so `a8s run alice` and `a8s run bob` each poll every
mailbox this node holds, and `network.claim_message` arbitrates which of them
delivers. Only the receive path's answer stamps the ledger: a sibling daemon
holding the claim, or a released claim after a failed delivery, answers False,
and the envelope is offered again on the next poll. The wire is at-least-once,
and `claim_message` plus the seen-ids ring collapse the repeats.

A machine that joins is owed the mail sent after it joined, as a new MQTT
session is owed nothing published before it existed. `a8s remote` stamps a
`joined` ULID into the spec, and an envelope minted more than
`JOIN_SKEW_GRACE_MS` below it is somebody else's history — the same cutoff,
and the same clock allowance, as the folder transport's.

**Mail leaves the wire by time, never because somebody read it.** The reap
sweeps every object in a mailbox this node polls — a consumed envelope, one
nobody took, junk — once both the ULID mint time and the object's
`LastModified` clear `retain_days` (default 3; `0` keeps forever). Both clocks,
as the folder transport requires: `LastModified` alone can be pushed forward by
a rewrite, and mint time alone would call a delayed send expired the moment a
backoff retry republished it. A key carrying no ULID has only the object clock
to offer. A bucket lifecycle rule on the prefix is the durable backstop: it
survives every node going away, which nothing in this process does, and it
covers a mailbox no node polls any more. The transport owns its own key
prefix, separate from any `a8s storage` prefix in the same bucket, because the
reap deletes under it.

`services/s3.py` deliberately never deletes: a storage service that reaches
back into a bucket to remove objects is a foot nuke, and S3 has lifecycle
rules for expiry. The transport's reap is the one delete in this module, and it
is why the two must not share a prefix.

boto3 is lazy and tier-2 (`requirements/a8s-s3.txt`), exactly as the storage
service has it: `a8s remote` installs the group the moment an `s3` remote is
registered, and a remote built by hand installs it here on first use. The
standard credential chain applies — env vars, shared config, SSO, instance and
container roles — so a machine granted an IAM role needs no a8s-side secret.
a8s never reads, stores, or logs a credential.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from core import canonical_name, out, s3_ledger_path
from delivery_receipt import parse_delivery_receipt
from registry import load_aliases, load_namespaces, load_registry
from transports import OnMessage, Transport, TransportError
from transports.folder import JOIN_SKEW_GRACE_MS
from transports.ledger import ConsumedLedger
from ar3 import clock
from ar3.ulid import is_ulid, new as new_ulid, parse as parse_ulid


# Recognized option keys. `node_tag`, `client_id` and `clean_session` are the
# loader's broker-session vocabulary and a bucket has no session to name; they
# are accepted and ignored so `load_remotes` can hand every transport the same
# option bag.
_KNOWN_OPTS: set[str] = {
    "prefix",
    "poll_seconds",
    "retain_days",
    "region",
    "profile",
    "endpoint_url",
    "timeout_s",
    "probe",
    "joined",
    "node_tag",
    "client_id",
    "clean_session",
}

# A LIST is the poll and a LIST is what it costs — once per mailbox per polling
# process, because every daemon on the node runs its own subscriber over the
# same registry. At 10s each of those pairs is ~260k requests a month, around
# USD 1.30 at today's list price, so a node running three daemons for four
# mailboxes pays twelve times that. Tighter buys latency at a price the
# operator can read off the interval and the daemon count.
DEFAULT_POLL_SECONDS = 10.0
MIN_POLL_SECONDS = 1.0

DEFAULT_RETAIN_DAYS = 3
DEFAULT_TIMEOUT_S = 60

# How long a failed attempt to build the client is remembered before another is
# made. Building it may install the `a8s-s3` group, which is a pip subprocess:
# a poll loop that reruns one every `poll_seconds` for as long as the machine is
# offline spends far more than the mail it is failing to fetch is worth.
CLIENT_RETRY_SECONDS = 600.0

# One page per mailbox per poll, and the page moves: the next poll resumes where
# this one stopped, and the end of the listing starts the mailbox over. A
# consumed envelope costs its share of the LIST and no GET, so a mailbox holding
# N retained objects shows new mail within ceil((N + 1) / PAGE_SIZE) polls, at
# one LIST per poll whatever N is.
PAGE_SIZE = 1000

# How often compaction may list every mailbox to learn which consumed ULIDs
# still have an object. With `retain_days 0` nothing ever leaves, the ledger
# stays over its cap, and every consumption would otherwise walk the bucket.
COMPACT_INTERVAL_SECONDS = 3600.0


class S3Transport(Transport):
    """One configured S3 remote.

    Args:
        remote_id: stable name from `network.json`.
        bucket: the bucket both nodes address.
        **opts: per-remote options forwarded from `network.json`. Recognized:
            prefix (required — the key prefix this transport reaps under,
            which must not be the one an `a8s storage` service writes under),
            poll_seconds (default 10, floored at 1), retain_days (default 3,
            `0` keeps forever), region / profile / endpoint_url / timeout_s
            (the `services/s3.py` vocabulary, same meanings), probe (a
            reachability check instead of a poll thread; see `start`), joined
            (the ULID `a8s remote` stamped at registration — envelopes minted
            before it are somebody else's history; absent means consume
            whatever is there).
    """

    def __init__(self, remote_id: str, *, bucket: str, **opts: Any) -> None:
        unknown = set(opts) - _KNOWN_OPTS
        if unknown:
            raise ValueError(
                f"remote {remote_id!r}: unknown option(s) {sorted(unknown)} "
                f"(known: {sorted(_KNOWN_OPTS)})"
            )
        seps = {os.sep, os.altsep or os.sep, "/"}
        if any(sep in remote_id for sep in seps):
            raise ValueError(
                f"remote {remote_id!r}: name cannot contain a path separator"
            )
        name = (bucket or "").strip()
        if not name:
            raise ValueError(f"remote {remote_id!r}: bucket is required")
        prefix = str(opts.get("prefix") or "").strip().strip("/")
        if not prefix:
            raise ValueError(
                f"remote {remote_id!r}: an s3 remote requires a key prefix "
                f"nothing else writes under, e.g. s3://{name}/a8s-mail"
            )

        self._remote_id = remote_id
        self._bucket = name
        self._prefix = prefix
        self._poll_seconds = self._resolve_poll_seconds(remote_id, opts)
        self._retain_days = self._resolve_retain_days(remote_id, opts)
        self._region = str(opts.get("region") or "").strip() or None
        self._profile = str(opts.get("profile") or "").strip() or None
        self._endpoint_url = str(opts.get("endpoint_url") or "").strip() or None
        raw_timeout = opts.get("timeout_s")
        self._timeout_s = int(DEFAULT_TIMEOUT_S if raw_timeout is None else raw_timeout)
        if self._timeout_s < 1:
            raise ValueError(f"remote {remote_id!r}: timeout_s must be positive")
        self._probe = bool(opts.get("probe", False))
        self._joined = self._resolve_joined(remote_id, opts)
        self._cutoff_ms = (
            parse_ulid(self._joined)[0] - JOIN_SKEW_GRACE_MS if self._joined else 0
        )
        self._client_cache: Any = None
        # Where the next LIST of each mailbox resumes. Without it a poll that
        # cannot consume its first page attempts that same page forever, and
        # every envelope behind it is unreachable until something external
        # removes the blockage.
        self._cursor: dict[str, str] = {}
        self._client_error: str | None = None
        self._client_error_at = 0.0
        self._warned: set[str] = set()
        self._last_compact = 0.0
        self._ledger = ConsumedLedger(
            s3_ledger_path(remote_id),
            remote_id=remote_id,
            warn=self._warn_once,
            present=self._present_ids,
        )
        self._on_message: Optional[OnMessage] = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False
        self._reachable = False

    @staticmethod
    def _resolve_poll_seconds(remote_id: str, opts: dict) -> float:
        raw = opts.get("poll_seconds")
        if raw is None or str(raw).strip() == "":
            return DEFAULT_POLL_SECONDS
        try:
            value = float(str(raw).strip())
        except ValueError:
            raise ValueError(f"remote {remote_id!r}: poll_seconds must be a number")
        return max(value, MIN_POLL_SECONDS)

    @staticmethod
    def _resolve_retain_days(remote_id: str, opts: dict) -> int:
        raw = opts.get("retain_days")
        if raw is None or str(raw).strip() == "":
            return DEFAULT_RETAIN_DAYS
        try:
            days = int(str(raw).strip())
        except ValueError:
            raise ValueError(f"remote {remote_id!r}: retain_days must be a whole number")
        if days < 0:
            raise ValueError(f"remote {remote_id!r}: retain_days cannot be negative")
        return days

    @staticmethod
    def _resolve_joined(remote_id: str, opts: dict) -> str:
        raw = opts.get("joined")
        if raw is None or str(raw).strip() == "":
            return ""
        value = str(raw).strip().upper()
        if not is_ulid(value):
            raise ValueError(f"remote {remote_id!r}: joined must be a ULID")
        return value

    @property
    def id(self) -> str:
        return self._remote_id

    # ---------- bucket ----------

    def _client(self) -> Any:
        if self._client_cache is not None:
            return self._client_cache
        if (
            self._client_error is not None
            and time.monotonic() - self._client_error_at < CLIENT_RETRY_SECONDS
        ):
            raise TransportError(self._client_error)
        try:
            return self._build_client()
        except TransportError as e:
            self._client_error = str(e)
            self._client_error_at = time.monotonic()
            raise

    def _build_client(self) -> Any:
        from ar3.deps import require_group

        try:
            require_group("a8s-s3", reason=f"s3 remote {self._remote_id!r}")
        except Exception as e:
            raise TransportError(
                f"{self._remote_id}: installing a8s-s3 (boto3) failed: {e}"
            ) from e
        try:
            import boto3
            from botocore.config import Config
        except ImportError as e:
            raise TransportError(
                f"{self._remote_id}: boto3 still missing after installing a8s-s3: {e}"
            ) from e
        session_args = {"profile_name": self._profile} if self._profile else {}
        try:
            session = boto3.session.Session(**session_args)
            self._client_cache = session.client(
                "s3",
                region_name=self._region,
                endpoint_url=self._endpoint_url,
                config=Config(
                    connect_timeout=self._timeout_s,
                    read_timeout=self._timeout_s,
                    retries={"max_attempts": 2},
                ),
            )
        except Exception as e:
            raise TransportError(f"{self._remote_id}: s3 client init failed: {e}") from e
        return self._client_cache

    def _mailboxes(self) -> list[str]:
        """The names this node answers for, and therefore the mailboxes it reads.

        Every agent, alias and namespace prefix in the local registry: each one
        is a recipient a sender on another cluster may address, and each one
        resolves here, whatever other machine also holds it. Read on every poll
        rather than captured at start, for the reason `make_receive_callback`
        re-reads participants — a daemon runs for days, and an agent added this
        morning must become reachable without a restart.
        """
        names: set[str] = set()
        for source in (load_registry(), load_aliases(), load_namespaces()):
            for raw in source:
                try:
                    names.add(canonical_name(raw))
                except ValueError:
                    continue
        return sorted(names)

    def _check_reachable(self) -> None:
        """Answer whether this machine can work the bucket right now.

        Every verb the transport uses, against the prefix it actually uses: an
        IAM policy that grants three of the four is the failure this is for,
        and it is invisible until the one missing verb is the one a message
        needs. The probe object is deleted on the way out — including after a
        failed put, which may have written the object and lost the answer, and
        including after a failed read, which is the case a cleanup that could
        itself raise would report over the top of.

        The first failure is the one reported, because it is the one that
        names the missing verb. A delete that fails is a missing verb too, and
        says so whenever nothing has gone wrong before it.
        """
        client = self._client()
        key = f"{self._prefix}/.a8s-health-{new_ulid()}"
        failure: tuple[str, Exception] | None = None
        try:
            client.put_object(Bucket=self._bucket, Key=key, Body=b"")
        except Exception as e:
            failure = ("s3 put failed", e)
        if failure is None:
            try:
                client.get_object(Bucket=self._bucket, Key=key)
                client.list_objects_v2(Bucket=self._bucket, Prefix=key, MaxKeys=1)
            except Exception as e:
                failure = ("s3 read failed", e)
        try:
            client.delete_object(Bucket=self._bucket, Key=key)
        except Exception as e:
            if failure is None:
                failure = ("s3 delete failed", e)
        if failure is not None:
            what, cause = failure
            raise TransportError(f"{self._remote_id}: {what}: {cause}") from cause

    # ---------- Transport ----------

    def start(self, on_message: OnMessage) -> None:
        if self._started:
            raise TransportError(f"{self._remote_id}: already started")
        self._on_message = on_message
        if self._probe:
            self._check_reachable()
            self._reachable = True
            self._started = True
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name=f"a8s-s3-{self._remote_id}",
            daemon=True,
        )
        self._thread.start()
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
            self._thread = None
        self._started = False

    def is_connected(self) -> bool:
        return self._reachable

    def publish(self, envelope: bytes) -> None:
        msg = _decode(envelope)
        if msg is None:
            raise TransportError(
                f"{self._remote_id}: envelope is not a JSON object with a ULID id"
            )
        mailbox = _mailbox_for(msg)
        if mailbox is None:
            raise TransportError(
                f"{self._remote_id}: envelope names no addressable recipient"
            )
        key = f"{self._prefix}/{mailbox}/{msg['id']}.json"
        try:
            self._client().put_object(
                Bucket=self._bucket, Key=key, Body=envelope
            )
        except TransportError:
            raise
        except Exception as e:
            raise TransportError(f"{self._remote_id}: s3 put failed: {e}") from e

    # ---------- poll ----------

    def _poll_loop(self) -> None:
        while True:
            try:
                self._poll_once()
            except Exception as e:
                # The bucket is the far side of a network and an unhandled
                # answer from it would otherwise end this thread in silence:
                # the remote stays "started" and no mail ever arrives again.
                self._warn_once(
                    f"poll:{type(e).__name__}",
                    f"WARN: remote {self._remote_id}: poll failed "
                    f"({type(e).__name__}: {e}); retrying",
                )
            if self._stop.wait(self._poll_seconds):
                return

    def _poll_once(self) -> None:
        mailboxes = self._mailboxes()
        for stale in set(self._cursor) - set(mailboxes):
            del self._cursor[stale]
        for mailbox in mailboxes:
            self._drain(mailbox)

    def _drain(self, mailbox: str) -> None:
        """One LIST page against one mailbox: reap what has expired, skip what
        this machine already consumed, and deliver the rest in ULID order.

        The page is where the last one stopped. A poll is bounded to one page
        so a large mailbox cannot monopolise the worker, but the bound has to
        move: nothing leaves the mailbox until it expires, so its front holds
        every envelope this machine already read, a delivery that failed, an
        object it could not fetch, junk inside the retention window, and on
        AWS a `CommonPrefixes` entry, which spends the key budget the same as
        an object does. A cursor that did not move would stop at the first
        page of old mail and hide every envelope after it. At the end of the
        listing the cursor is dropped, so the next poll starts over and
        revisits both the retries and whatever arrived meanwhile."""
        client = self._client()
        prefix = f"{self._prefix}/{mailbox}/"
        token = self._cursor.get(mailbox)
        params: dict[str, Any] = {
            "Bucket": self._bucket,
            "Prefix": prefix,
            "Delimiter": "/",
            "MaxKeys": PAGE_SIZE,
        }
        if token:
            params["ContinuationToken"] = token
        try:
            page = client.list_objects_v2(**params)
        except Exception as e:
            self._reachable = False
            # A token S3 will not take is worse than no token: it fails every
            # poll from here on. Start the mailbox over instead.
            self._cursor.pop(mailbox, None)
            self._warn_once(
                f"list:{type(e).__name__}",
                f"WARN: remote {self._remote_id}: listing {prefix} failed "
                f"({type(e).__name__}: {e}); retrying",
            )
            return
        self._reachable = True
        nxt = page.get("NextContinuationToken") if page.get("IsTruncated") else None
        if nxt:
            self._cursor[mailbox] = str(nxt)
        else:
            self._cursor.pop(mailbox, None)
        cutoff = time.time() - self._retain_days * 86400 if self._retain_days else 0.0
        for item in page.get("Contents") or []:
            key = str(item.get("Key") or "")
            name = key[len(prefix):]
            stem = name[:-5] if name.endswith(".json") else ""
            if self._expired(stem, item, cutoff):
                self._drop(key)
                continue
            if not is_ulid(stem):
                continue
            if stem in self._ledger:
                continue
            if self._cutoff_ms and parse_ulid(stem)[0] < self._cutoff_ms:
                self._warn_backlog()
                continue
            try:
                body = client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
            except Exception as e:
                self._warn_once(
                    f"get:{type(e).__name__}",
                    f"WARN: remote {self._remote_id}: fetching {key} failed "
                    f"({type(e).__name__}: {e}); retrying",
                )
                continue
            msg = _decode(body)
            # A PUT is atomic, so an object whose name and envelope disagree is
            # not half-written — it is somebody else's object under our prefix,
            # and only the retention window may remove it.
            if msg is None or msg["id"].upper() != stem.upper():
                self._warn_once(
                    f"foreign:{key}",
                    f"WARN: remote {self._remote_id}: {key} is not the envelope "
                    f"its name claims; leaving it",
                )
                continue
            cb = self._on_message
            if cb is None:
                continue
            try:
                consumed = cb(body) is not False
            except Exception as e:
                # The receive path answers rather than raises, so this is the
                # guard for any other callback a transport may be handed — and
                # a raise says the same thing an answer of False does.
                consumed = False
                self._warn_once(
                    f"deliver:{type(e).__name__}",
                    f"WARN: remote {self._remote_id}: delivery failed "
                    f"({type(e).__name__}: {e}); retrying",
                )
            if not consumed:
                # Not recorded. A sibling daemon on this machine polls the same
                # mailbox and may hold the claim right now, and a ledger entry
                # would skip this object on every later poll: if that sibling
                # dies mid-delivery, nobody on this machine reads it again.
                continue
            self._ledger.record(stem)

    def _expired(self, stem: str, item: dict, cutoff: float) -> bool:
        """Whether an object has outlived `retain_days` on both clocks.

        `LastModified` alone can be pushed forward by a rewrite, and the ULID's
        mint time alone would call a delayed send expired the moment a backoff
        retry republished it. A key with no ULID in it has only the one clock
        to offer.
        """
        if not cutoff:
            return False
        stamped = item.get("LastModified")
        if stamped is None or stamped.timestamp() >= cutoff:
            return False
        if is_ulid(stem) and parse_ulid(stem)[0] >= cutoff * 1000:
            return False
        return True

    def _drop(self, key: str) -> None:
        """Delete an expired object. A refusal is left for the next pass over
        this key, which asks again; every other machine holding the mailbox
        asks too, and a delete of a key already gone succeeds."""
        try:
            self._client().delete_object(Bucket=self._bucket, Key=key)
        except Exception:
            return
        out(f"remote {self._remote_id}: dropped {key} past retain_days")

    def _present_ids(self) -> set[str] | None:
        """Every envelope ULID still in a mailbox this node reads, for the
        ledger's compaction; None when the bucket cannot answer or was asked
        within the last `COMPACT_INTERVAL_SECONDS`.

        A ULID in a mailbox this node no longer holds is not listed, and may
        be forgotten: nothing here polls that mailbox to redeliver it.
        """
        now = time.time()
        if now - self._last_compact < COMPACT_INTERVAL_SECONDS:
            return None
        self._last_compact = now
        present: set[str] = set()
        try:
            client = self._client()
            for mailbox in self._mailboxes():
                prefix = f"{self._prefix}/{mailbox}/"
                params: dict[str, Any] = {
                    "Bucket": self._bucket,
                    "Prefix": prefix,
                    "Delimiter": "/",
                    "MaxKeys": PAGE_SIZE,
                }
                while True:
                    page = client.list_objects_v2(**params)
                    for item in page.get("Contents") or []:
                        name = str(item.get("Key") or "")[len(prefix):]
                        if name.endswith(".json") and is_ulid(name[:-5]):
                            present.add(name[:-5])
                    token = page.get("NextContinuationToken")
                    if not (page.get("IsTruncated") and token):
                        break
                    params["ContinuationToken"] = token
        except Exception:
            return None
        return present

    def _warn_backlog(self) -> None:
        """Say once that a cutoff is hiding mail.

        A clock that ran fast at registration stamps a `joined` no peer can
        reach, and every envelope is then skipped as history. This line is the
        one thing in `a8s logs` that shows it.
        """
        joined_local = clock.stamp(
            datetime.fromtimestamp(parse_ulid(self._joined)[0] / 1000, tz=timezone.utc),
            seconds=True,
        )
        self._warn_once(
            "backlog",
            f"WARN: remote {self._remote_id}: ignoring envelopes minted before "
            f"this machine joined ({self._joined} = {joined_local}) as backlog — "
            f"if that time is in the future, the clock was ahead at "
            f"registration; a8s unremote + re-add re-joins at now",
        )

    def _warn_once(self, key: str, message: str) -> None:
        """Say it the first time and never again for the same `key`.

        This transport's failures all look like nothing arriving, so every one
        of them owes the operator a line in `a8s logs` — and a poll loop owes
        it exactly once, not every ten seconds.
        """
        if key in self._warned:
            return
        self._warned.add(key)
        out(message)


def _decode(envelope: bytes) -> dict | None:
    """The envelope as a dict, or None when it is not one with a ULID id."""
    try:
        msg = json.loads(envelope)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(msg, dict):
        return None
    msg_id = msg.get("id")
    if not isinstance(msg_id, str) or not is_ulid(msg_id):
        return None
    return msg


def _mailbox_for(msg: dict) -> str | None:
    """The prefix segment this envelope belongs under, or None if it has none.

    Normally the recipient. A delivery receipt is addressed to a reserved
    destination that is deliberately not a participant, so on a wire where the
    address picks the reader it would reach nobody: it is filed under the
    original sender instead, which is the one node that can act on it.

    A namespace address (`acme:ops:phil`) is filed under its prefix alone. The
    prefix is what a node binds and therefore what it can poll; the full
    address rides inside the envelope, where `resolve_name` reads it.
    """
    receipt = parse_delivery_receipt(msg)
    target = receipt.sender if receipt is not None else str(msg.get("to") or "")
    try:
        return canonical_name(target.partition(":")[0])
    except ValueError:
        return None
