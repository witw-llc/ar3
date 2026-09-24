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

**A mailbox has one owner, and the owner is a node.** The node whose registry
holds a name is the node that polls `<prefix>/<name>/`, and it is the only one:
two nodes polling one prefix would race to delete each other's mail. That is
also why a publish to a name this node already answers for writes nothing — the
routing pass delivers it locally, and putting it in the bucket would only hand
this node its own message back. The owner set is every agent, alias and
namespace prefix in the local registry, read fresh on every poll so `a8s add`
needs no restart.

A node is not a process. Every daemon on the machine runs its own subscriber
over the same registry, so `a8s run alice` and `a8s run bob` each poll every
mailbox this node owns. That fan-out is the model, not a mistake, and
`network.claim_message` is what arbitrates it.

**Delete is the acknowledgement, and only a delivery earns one.** An envelope
is deleted once the receive path reports it consumed — delivered here, or
already in the seen-ids ring. When the answer is no, the object stays: a
sibling daemon holding the claim is mid-delivery, and a released claim exists
so somebody can try again. Both would be destroyed by a delete keyed on
nothing more than the callback returning. Leaving it makes the wire
at-least-once — a node that dies between the delivery and the delete sees the
message again — which is what `claim_message` and the seen-ids ring already
collapse for every other transport. The transport owns its own key prefix,
separate from any `a8s storage` prefix in the same bucket, so a lifecycle rule
pointed at one cannot eat the other.

Retention is the bucket's job: a lifecycle rule survives a node that never
comes back, which nothing in this process does. `retain_days` covers the
narrower case the rule cannot see — an object under this node's own prefix that
is not this transport's mail at all, because the key is not an envelope or the
envelope is not the one its name claims. A well-formed envelope addressed to
this node is never swept, however long it has failed to land: it is mail, and a
node that cannot take it today may take it tomorrow. What is swept goes only
when both the ULID mint time and the object's `LastModified` clear the window,
the same two clocks the folder transport requires, so a delayed send never
sweeps itself.

`services/s3.py` deliberately never deletes: a storage service that reaches
back into a bucket to remove objects is a foot nuke, and S3 has lifecycle
rules for expiry. A transport is the opposite case — an unacknowledged
envelope is not archived, it is redelivered — so this one deletes, and the
asymmetry is why the two must not share a prefix.

boto3 is lazy and tier-2 (`requirements/a8s-s3.txt`), exactly as the storage
service has it: `a8s remote` installs the group the moment an `s3` remote is
registered, and a remote built by hand installs it here on first use. The
standard credential chain applies — env vars, shared config, SSO, instance and
container roles — so a machine granted an IAM role needs no a8s-side secret.
a8s never reads, stores, or logs a credential.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional

from core import canonical_name, out
from delivery_receipt import parse_delivery_receipt
from registry import load_aliases, load_namespaces, load_registry
from transports import OnMessage, Transport, TransportError
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

# One page per mailbox per poll. The poll deletes what it consumes, so a
# backlog drains over successive polls instead of turning one interval into an
# unbounded run of GETs on a network that is slow by assumption.
PAGE_SIZE = 1000


class S3Transport(Transport):
    """One configured S3 remote.

    Args:
        remote_id: stable name from `network.json`.
        bucket: the bucket both nodes address.
        **opts: per-remote options forwarded from `network.json`. Recognized:
            prefix (required — the key prefix this transport owns, which must
            not be the one an `a8s storage` service writes under), poll_seconds
            (default 10, floored at 1), retain_days (default 3, `0` keeps
            forever), region / profile / endpoint_url / timeout_s (the
            `services/s3.py` vocabulary, same meanings), probe (a reachability
            check instead of a poll thread; see `start`).
    """

    def __init__(self, remote_id: str, *, bucket: str, **opts: Any) -> None:
        unknown = set(opts) - _KNOWN_OPTS
        if unknown:
            raise ValueError(
                f"remote {remote_id!r}: unknown option(s) {sorted(unknown)} "
                f"(known: {sorted(_KNOWN_OPTS)})"
            )
        name = (bucket or "").strip()
        if not name:
            raise ValueError(f"remote {remote_id!r}: bucket is required")
        prefix = str(opts.get("prefix") or "").strip().strip("/")
        if not prefix:
            raise ValueError(
                f"remote {remote_id!r}: an s3 remote requires a key prefix it "
                f"owns alone, e.g. s3://{name}/a8s-mail"
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
        self._client_cache: Any = None
        # Where the next LIST of each mailbox resumes. Without it a poll that
        # cannot consume its first page attempts that same page forever, and
        # every envelope behind it is unreachable until something external
        # removes the blockage.
        self._cursor: dict[str, str] = {}
        self._client_error: str | None = None
        self._client_error_at = 0.0
        self._warned: set[str] = set()
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
        """The names this node answers for, and therefore the prefixes it owns.

        Every agent, alias and namespace prefix in the local registry: each one
        is a recipient a sender on another cluster may address, and each one
        resolves here and nowhere else. Read on every poll rather than captured
        at start, for the reason `make_receive_callback` re-reads participants —
        a daemon runs for days, and an agent added this morning must become
        reachable without a restart.
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
        if mailbox in self._mailboxes():
            # This node owns that prefix and polls it. Publishing here would
            # put the message in the bucket for this node to hand back to
            # itself, after the routing pass has already delivered it locally.
            self._warn_once(
                f"local:{mailbox}",
                f"remote {self._remote_id}: {mailbox} is registered on this "
                f"node — envelopes for it are delivered locally and never "
                f"published to s3",
            )
            return
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
        """One LIST page against one mailbox, then deliver in ULID order and
        delete what the receive path says it consumed.

        The page is where the last one stopped. A poll is bounded to one page
        so a large mailbox cannot monopolise the worker, but the bound has to
        move: anything this transport leaves behind — a delivery that failed,
        an object it could not fetch, junk inside its retention window, and on
        AWS a `CommonPrefixes` entry, which spends the key budget the same as
        an object does — otherwise holds the front of the listing and hides
        every envelope after it. At the end of the listing the cursor is
        dropped, so the next poll starts over and revisits both the retries
        and whatever arrived meanwhile."""
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
            if not is_ulid(stem):
                self._reap(key, stem, item, cutoff)
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
                self._reap(key, stem, item, cutoff)
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
                # Deliberately left where it is. A sibling daemon on this
                # machine polls the same mailbox and may hold the claim right
                # now; deleting here would destroy the only copy of a message
                # it is still delivering. The wire is at-least-once by design,
                # and `claim_message` plus the seen-ids ring collapse the
                # redelivery on the next poll. Not a `_reap` candidate either:
                # this is a well-formed envelope addressed to this node, which
                # is mail, and only the bucket's lifecycle rule removes mail.
                continue
            try:
                client.delete_object(Bucket=self._bucket, Key=key)
            except Exception as e:
                # The message is delivered and the object is still there, so it
                # is delivered again on the next poll. `claim_message` and the
                # seen-ids ring absorb that; a delete that never succeeds does
                # it forever, which is worth a line.
                self._warn_once(
                    f"delete:{type(e).__name__}",
                    f"WARN: remote {self._remote_id}: deleting {key} failed "
                    f"({type(e).__name__}: {e}); it will be redelivered",
                )

    def _reap(self, key: str, stem: str, item: dict, cutoff: float) -> None:
        """Drop an object that is not this transport's mail, once it is old.

        Everything reachable here is either not an envelope at all or not the
        envelope its own name claims, so nothing else will ever remove it from
        a prefix only this node reads. An envelope that is well-formed and
        addressed here never arrives: that is mail, and a node that cannot take
        it today may take it tomorrow, so the bucket's lifecycle rule owns that
        case and this does not. Both clocks must agree, exactly as the folder
        transport requires: `LastModified` alone can be rewritten forward by a
        rewrite, and the ULID's mint time alone would call a delayed send
        expired the moment it landed. A key with no ULID in it has only the one
        clock to offer.
        """
        if not cutoff:
            return
        stamped = item.get("LastModified")
        if stamped is None or stamped.timestamp() >= cutoff:
            return
        if is_ulid(stem) and parse_ulid(stem)[0] >= cutoff * 1000:
            return
        try:
            self._client().delete_object(Bucket=self._bucket, Key=key)
        except Exception:
            return
        out(f"remote {self._remote_id}: dropped {key} past retain_days")

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
