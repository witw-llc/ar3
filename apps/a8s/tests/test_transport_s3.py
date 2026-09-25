"""Tests for the S3 transport.

No bucket, no credentials and no network: every test drives an in-memory fake
injected into the transport's client cache, the way the S3 storage service's
tests do. That keeps the suite about our own logic — key layout, fan-out,
the consumed ledger, retention — without pretending to test AWS.
"""
from __future__ import annotations

import io
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from ar3.ulid import ALPHABET, new as new_ulid
from delivery_receipt import build_delivery_receipt
from registry import (
    save_aliases,
    save_namespace_options,
    save_namespaces,
    save_registry,
)
from transports import TransportError
from transports.s3 import PAGE_SIZE, S3Transport

BUCKET = "my-bucket"
PREFIX = "a8s-mail"


class FakeBucket:
    """An in-memory bucket with the four verbs the transport uses."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, datetime]] = {}
        self.fail: set[str] = set()
        self.calls: list[tuple[str, str]] = []

    def _verb(self, name: str, key: str) -> None:
        self.calls.append((name, key))
        if name in self.fail:
            raise RuntimeError(f"{name} refused")

    def put_object(self, *, Bucket, Key, Body):
        self._verb("put", Key)
        self.objects[Key] = (Body, datetime.now(timezone.utc))

    def get_object(self, *, Bucket, Key):
        self._verb("get", Key)
        return {"Body": io.BytesIO(self.objects[Key][0])}

    def delete_object(self, *, Bucket, Key):
        self._verb("delete", Key)
        self.objects.pop(Key, None)

    def list_objects_v2(
        self, *, Bucket, Prefix, Delimiter=None, MaxKeys=1000, ContinuationToken=None
    ):
        """One ordered walk of the key space, as S3 does it.

        A key under `Delimiter` becomes a CommonPrefix, and a CommonPrefix
        spends one unit of `MaxKeys` exactly as a key does. Filtering the
        nested keys out before the limit — which is the easy way to write this
        — gives a page more capacity than the real thing ever has, and hides
        the case where a page carries nothing but prefixes."""
        self._verb("list", Prefix)
        entries: list[tuple[str, bool]] = []
        groups: set[str] = set()
        for k in sorted(k for k in self.objects if k.startswith(Prefix)):
            rest = k[len(Prefix):]
            if Delimiter and Delimiter in rest:
                group = Prefix + rest.split(Delimiter)[0] + Delimiter
                if group not in groups:
                    groups.add(group)
                    entries.append((group, True))
            else:
                entries.append((k, False))
        entries.sort()
        start = 0
        if ContinuationToken is not None:
            start = next(
                (i for i, e in enumerate(entries) if e[0] > ContinuationToken),
                len(entries),
            )
        page = entries[start:start + MaxKeys]
        truncated = start + MaxKeys < len(entries)
        out: dict = {
            "Contents": [
                {"Key": n, "LastModified": self.objects[n][1]}
                for n, is_prefix in page
                if not is_prefix
            ],
            "CommonPrefixes": [{"Prefix": n} for n, is_prefix in page if is_prefix],
            "IsTruncated": truncated,
        }
        if truncated:
            out["NextContinuationToken"] = page[-1][0]
        return out

    def age(self, key: str, days: float) -> None:
        body, _ = self.objects[key]
        self.objects[key] = (body, datetime.now(timezone.utc) - timedelta(days=days))

    def keys(self) -> list[str]:
        return sorted(self.objects)


def _transport(**opts) -> tuple[S3Transport, FakeBucket]:
    opts.setdefault("prefix", PREFIX)
    t = S3Transport(remote_id="hub", bucket=BUCKET, **opts)
    bucket = FakeBucket()
    t._client_cache = bucket
    return t, bucket


def _envelope(msg_id: str, to: str = "target", content: str = "hi") -> bytes:
    return json.dumps(
        {"id": msg_id, "from": "example-sender", "to": to, "content": content}
    ).encode("utf-8")


def _ulid_at_ms(ms: int) -> str:
    """A ULID carrying an exact millisecond, with a zeroed random half."""
    chars = []
    n = ms
    for _ in range(10):
        chars.append(ALPHABET[n & 0x1F])
        n >>= 5
    return "".join(reversed(chars)) + "0" * 16


def _register(*names: str) -> None:
    save_registry({name: {"root": f"/nowhere/{name}"} for name in names})


def _arm(t: S3Transport, on_message) -> None:
    """Wire the receive callback without the poll thread.

    `start` polls immediately and then every interval, which is right in
    production and useless in a test that counts what one poll did.
    `TestLifecycle` covers the thread itself.
    """
    t._on_message = on_message


def _wait_for(done, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if done():
            return
        time.sleep(0.01)
    raise AssertionError("the poll thread never got there")


class TestConfig:
    def test_prefix_is_required(self):
        with pytest.raises(ValueError, match="key prefix"):
            S3Transport(remote_id="hub", bucket=BUCKET)

    def test_bucket_is_required(self):
        with pytest.raises(ValueError, match="bucket is required"):
            S3Transport(remote_id="hub", bucket="  ", prefix=PREFIX)

    def test_unknown_option_is_rejected(self):
        with pytest.raises(ValueError, match="unknown option"):
            S3Transport(remote_id="hub", bucket=BUCKET, prefix=PREFIX, regionn="x")

    def test_broker_session_options_are_accepted_and_ignored(self):
        t = S3Transport(
            remote_id="hub",
            bucket=BUCKET,
            prefix=PREFIX,
            node_tag="node-a",
            client_id="a8s-health-1",
            clean_session=True,
        )
        assert t.id == "hub"

    def test_poll_seconds_defaults_and_floors(self):
        assert _transport()[0]._poll_seconds == 10.0
        assert _transport(poll_seconds="0.1")[0]._poll_seconds == 1.0
        assert _transport(poll_seconds="30")[0]._poll_seconds == 30.0
        with pytest.raises(ValueError, match="poll_seconds"):
            _transport(poll_seconds="soon")

    def test_retain_days_defaults_and_validates(self):
        assert _transport()[0]._retain_days == 3
        assert _transport(retain_days="0")[0]._retain_days == 0
        with pytest.raises(ValueError, match="cannot be negative"):
            _transport(retain_days="-1")
        with pytest.raises(ValueError, match="whole number"):
            _transport(retain_days="three")

    def test_timeout_must_be_positive(self):
        with pytest.raises(ValueError, match="timeout_s"):
            _transport(timeout_s=0)

    def test_prefix_slashes_are_normalized(self):
        assert _transport(prefix="/mail/")[0]._prefix == "mail"


class TestPublish:
    def test_writes_one_object_under_the_recipient(self, fake_home):
        _register("local")
        t, bucket = _transport()
        msg_id = new_ulid()
        raw = _envelope(msg_id, to="target")
        t.publish(raw)
        assert bucket.keys() == [f"{PREFIX}/target/{msg_id}.json"]
        assert bucket.objects[f"{PREFIX}/target/{msg_id}.json"][0] == raw

    def test_a_name_this_node_answers_for_is_published_too(self, fake_home, capsys):
        """The same name may live on another machine, and it is owed a copy.

        MQTT publishes to the shared topic whatever the routing pass already
        delivered here, and the broker's echo is collapsed by the seen-ids
        ring. The bucket does the same: the object goes on the wire, and this
        node's own poll is the echo.
        """
        _register("target")
        t, bucket = _transport()
        msg_id = new_ulid()
        t.publish(_envelope(msg_id, to="TARGET"))
        assert bucket.keys() == [f"{PREFIX}/target/{msg_id}.json"]
        assert "delivered locally" not in capsys.readouterr().out

    def test_an_alias_this_node_holds_is_published_too(self, fake_home):
        _register("target")
        save_aliases({"team": ["target"]})
        t, bucket = _transport()
        msg_id = new_ulid()
        t.publish(_envelope(msg_id, to="team"))
        assert bucket.keys() == [f"{PREFIX}/team/{msg_id}.json"]

    def test_a_namespace_address_is_filed_under_its_prefix(self, fake_home):
        _register("local")
        t, bucket = _transport()
        msg_id = new_ulid()
        t.publish(_envelope(msg_id, to="acme:ops:example-agent"))
        assert bucket.keys() == [f"{PREFIX}/acme/{msg_id}.json"]

    def test_a_bound_namespace_prefix_is_published_too(self, fake_home):
        _register("local")
        save_namespaces({"acme": "local"})
        t, bucket = _transport()
        msg_id = new_ulid()
        t.publish(_envelope(msg_id, to="acme:ops:example-agent"))
        assert bucket.keys() == [f"{PREFIX}/acme/{msg_id}.json"]

    def test_a_receipt_is_filed_under_the_sender_it_reports_to(self, fake_home):
        _register("local")
        t, bucket = _transport()
        original = json.loads(_envelope(new_ulid(), to="target"))
        receipt = build_delivery_receipt(original, ["target"])
        t.publish(json.dumps(receipt).encode("utf-8"))
        assert bucket.keys() == [f"{PREFIX}/example-sender/{receipt['id']}.json"]

    def test_invalid_envelope_raises_and_writes_nothing(self, fake_home):
        t, bucket = _transport()
        for bad in (b"not json", b'["a","list"]', b'{"id":"nope"}', b"{}"):
            with pytest.raises(TransportError):
                t.publish(bad)
        assert bucket.keys() == []

    def test_an_unaddressable_recipient_raises(self, fake_home):
        t, bucket = _transport()
        with pytest.raises(TransportError, match="no addressable recipient"):
            t.publish(_envelope(new_ulid(), to=""))
        with pytest.raises(TransportError, match="no addressable recipient"):
            t.publish(_envelope(new_ulid(), to="not a name"))
        assert bucket.keys() == []

    def test_a_refused_put_raises(self, fake_home):
        _register("local")
        t, bucket = _transport()
        bucket.fail.add("put")
        with pytest.raises(TransportError, match="s3 put failed"):
            t.publish(_envelope(new_ulid()))


class TestPoll:
    def test_delivers_this_node_s_mailboxes_in_ulid_order(self, fake_home):
        _register("target")
        t, bucket = _transport()
        first, second = sorted([new_ulid(), new_ulid()])
        for msg_id in (second, first):
            bucket.put_object(
                Bucket=BUCKET,
                Key=f"{PREFIX}/target/{msg_id}.json",
                Body=_envelope(msg_id),
            )
        seen: list[bytes] = []
        _arm(t, seen.append)
        t._poll_once()
        assert [json.loads(raw)["id"] for raw in seen] == [first, second]

    def test_delivery_leaves_the_object_for_every_other_machine(self, fake_home):
        _register("target")
        t, bucket = _transport()
        msg_id = new_ulid()
        key = f"{PREFIX}/target/{msg_id}.json"
        bucket.put_object(Bucket=BUCKET, Key=key, Body=_envelope(msg_id))
        _arm(t, lambda _raw: True)
        t._poll_once()
        assert bucket.keys() == [key]
        assert not any(verb == "delete" for verb, _key in bucket.calls)
        assert msg_id in t._ledger

    def test_a_consumed_envelope_is_not_offered_again_on_this_machine(
        self, fake_home
    ):
        _register("target")
        t, bucket = _transport()
        msg_id = new_ulid()
        key = f"{PREFIX}/target/{msg_id}.json"
        bucket.put_object(Bucket=BUCKET, Key=key, Body=_envelope(msg_id))
        seen: list[bytes] = []
        _arm(t, lambda raw: seen.append(raw) or True)
        t._poll_once()
        bucket.calls.clear()
        t._poll_once()
        assert len(seen) == 1
        assert ("get", key) not in bucket.calls, "the ledger answers without a GET"

    def test_the_ledger_survives_a_restart(self, fake_home):
        _register("target")
        t, bucket = _transport()
        msg_id = new_ulid()
        key = f"{PREFIX}/target/{msg_id}.json"
        bucket.put_object(Bucket=BUCKET, Key=key, Body=_envelope(msg_id))
        _arm(t, lambda _raw: True)
        t._poll_once()

        restarted = S3Transport(remote_id="hub", bucket=BUCKET, prefix=PREFIX)
        restarted._client_cache = bucket
        seen: list[bytes] = []
        _arm(restarted, seen.append)
        restarted._poll_once()
        assert seen == []
        assert bucket.keys() == [key]

    def test_a_receive_path_with_no_answer_is_still_a_consumption(
        self, fake_home
    ):
        # A callback that reports nothing is a receive path this transport
        # cannot interrogate. It gets the plain at-least-once contract.
        _register("target")
        t, bucket = _transport()
        msg_id = new_ulid()
        bucket.put_object(
            Bucket=BUCKET, Key=f"{PREFIX}/target/{msg_id}.json", Body=_envelope(msg_id)
        )
        _arm(t, lambda _raw: None)
        t._poll_once()
        assert msg_id in t._ledger

    def test_the_echo_of_a_local_delivery_is_collapsed_by_the_ring(
        self, fake_home, tmp_path
    ):
        """What MQTT does with the broker's echo of its own publish.

        The routing pass delivered the message here and recorded it in the
        seen-ids ring before publishing it. This node's poll then meets its
        own object: the real receive callback answers from the ring, nothing
        is written twice, and the ledger takes the ULID so the next poll does
        not fetch it again.
        """
        from core import Participant, inbox_dir
        from network import make_receive_callback, seen_id_append

        root = tmp_path / "target"
        root.mkdir()
        save_registry({"target": {"root": str(root)}})
        t, bucket = _transport()
        msg_id = new_ulid()
        seen_id_append(msg_id)
        t.publish(_envelope(msg_id, to="target"))
        participants = [Participant("target", root)]
        _arm(t, make_receive_callback(lambda: participants, services=[]))
        t._poll_once()
        assert not inbox_dir("target").is_dir()
        assert msg_id in t._ledger
        assert bucket.keys() == [f"{PREFIX}/target/{msg_id}.json"]

    def test_only_this_node_s_prefixes_are_listed(self, fake_home):
        _register("target")
        save_aliases({"team": ["target"]})
        save_namespaces({"acme": "target"})
        t, bucket = _transport()
        _arm(t, lambda _raw: None)
        t._poll_once()
        assert [key for verb, key in bucket.calls if verb == "list"] == [
            f"{PREFIX}/acme/",
            f"{PREFIX}/target/",
            f"{PREFIX}/team/",
        ]

    def test_another_node_s_mail_is_left_alone(self, fake_home):
        _register("target")
        t, bucket = _transport()
        msg_id = new_ulid()
        key = f"{PREFIX}/elsewhere/{msg_id}.json"
        bucket.put_object(Bucket=BUCKET, Key=key, Body=_envelope(msg_id, to="elsewhere"))
        seen: list[bytes] = []
        _arm(t, seen.append)
        t._poll_once()
        assert seen == []
        assert bucket.keys() == [key]

    def test_attachment_objects_are_not_read_as_envelopes(self, fake_home):
        _register("target")
        t, bucket = _transport()
        msg_id = new_ulid()
        bucket.put_object(
            Bucket=BUCKET, Key=f"{PREFIX}/target/{msg_id}/report.json", Body=b"{}"
        )
        seen: list[bytes] = []
        _arm(t, seen.append)
        t._poll_once()
        assert seen == []
        assert bucket.keys() == [f"{PREFIX}/target/{msg_id}/report.json"]

    def test_a_failed_delivery_leaves_the_object_for_the_next_poll(
        self, fake_home, capsys
    ):
        _register("target")
        t, bucket = _transport()
        msg_id = new_ulid()
        key = f"{PREFIX}/target/{msg_id}.json"
        bucket.put_object(Bucket=BUCKET, Key=key, Body=_envelope(msg_id))
        attempts: list[bytes] = []

        def explode(raw: bytes) -> None:
            attempts.append(raw)
            raise RuntimeError("inbox is full")

        _arm(t, explode)
        t._poll_once()
        t._poll_once()
        assert len(attempts) == 2
        assert bucket.keys() == [key]
        assert capsys.readouterr().out.count("delivery failed") == 1

    def test_an_envelope_nobody_consumed_is_left_on_the_wire(self, fake_home):
        """The receive path returning is not the same as it consuming.

        A sibling daemon on this machine polls the same mailbox and may hold
        the claim, and `receive_envelope` returns normally when it does.
        Deleting on that answer destroys the only copy of a message somebody
        else is still delivering.
        """
        _register("target")
        t, bucket = _transport()
        msg_id = new_ulid()
        key = f"{PREFIX}/target/{msg_id}.json"
        bucket.put_object(Bucket=BUCKET, Key=key, Body=_envelope(msg_id))
        _arm(t, lambda _raw: False)
        t._poll_once()
        assert bucket.keys() == [key]
        assert not any(verb == "delete" for verb, _key in bucket.calls)
        assert msg_id not in t._ledger

    def test_an_unconsumed_envelope_is_offered_until_somebody_takes_it(
        self, fake_home
    ):
        _register("target")
        t, bucket = _transport()
        msg_id = new_ulid()
        bucket.put_object(
            Bucket=BUCKET, Key=f"{PREFIX}/target/{msg_id}.json", Body=_envelope(msg_id)
        )
        answers = [False, False, True]
        offered: list[bytes] = []

        def cb(raw: bytes) -> bool:
            offered.append(raw)
            return answers.pop(0)

        _arm(t, cb)
        t._poll_once()
        t._poll_once()
        assert msg_id not in t._ledger
        t._poll_once()
        assert msg_id in t._ledger
        t._poll_once()
        assert len(offered) == 3

    def test_a_delivery_that_raises_is_not_recorded(self, fake_home, capsys):
        _register("target")
        t, bucket = _transport()
        msg_id = new_ulid()
        key = f"{PREFIX}/target/{msg_id}.json"
        bucket.put_object(Bucket=BUCKET, Key=key, Body=_envelope(msg_id))

        def explode(_raw: bytes) -> None:
            raise RuntimeError("inbox is full")

        _arm(t, explode)
        t._poll_once()
        assert bucket.keys() == [key]
        assert msg_id not in t._ledger

    def test_an_object_that_is_not_the_envelope_it_names_is_left(
        self, fake_home, capsys
    ):
        _register("target")
        t, bucket = _transport()
        key = f"{PREFIX}/target/{new_ulid()}.json"
        bucket.put_object(Bucket=BUCKET, Key=key, Body=_envelope(new_ulid()))
        seen: list[bytes] = []
        _arm(t, seen.append)
        t._poll_once()
        assert seen == []
        assert bucket.keys() == [key]
        assert "is not the envelope its name claims" in capsys.readouterr().out

    def test_a_listing_failure_is_survivable_and_said_once(self, fake_home, capsys):
        _register("target")
        t, bucket = _transport()
        bucket.fail.add("list")
        _arm(t, lambda _raw: None)
        t._poll_once()
        t._poll_once()
        assert t.is_connected() is False
        assert capsys.readouterr().out.count("listing") == 1

    def test_connected_only_after_a_listing_succeeds(self, fake_home):
        _register("target")
        t, _bucket = _transport()
        _arm(t, lambda _raw: None)
        assert t.is_connected() is False
        t._poll_once()
        assert t.is_connected() is True


class TestLifecycle:
    def test_start_delivers_without_anyone_calling_the_poll(self, fake_home):
        _register("target")
        t, bucket = _transport(poll_seconds=1)
        msg_id = new_ulid()
        bucket.put_object(
            Bucket=BUCKET, Key=f"{PREFIX}/target/{msg_id}.json", Body=_envelope(msg_id)
        )
        arrived: list[bytes] = []
        t.start(arrived.append)
        try:
            _wait_for(lambda: bool(arrived))
        finally:
            t.stop()
        assert [json.loads(raw)["id"] for raw in arrived] == [msg_id]

    def test_stop_ends_the_thread(self, fake_home):
        _register("target")
        t, _bucket = _transport(poll_seconds=1)
        t.start(lambda _raw: None)
        thread = t._thread
        t.stop()
        assert thread is not None and not thread.is_alive()

    def test_stop_before_start_is_a_no_op(self, fake_home):
        t, _bucket = _transport()
        t.stop()

    def test_start_twice_raises(self, fake_home):
        t, _bucket = _transport()
        t.start(lambda _raw: None)
        try:
            with pytest.raises(TransportError, match="already started"):
                t.start(lambda _raw: None)
        finally:
            t.stop()

    def test_the_poll_thread_survives_an_unexpected_answer(self, fake_home, capsys):
        _register("target")
        t, bucket = _transport(poll_seconds=1)
        # An answer the transport has no case for, on the retention path,
        # which is the one place `_drain` does not wrap.
        bucket.objects[f"{PREFIX}/target/{new_ulid()}.json"] = (b"{}", "not a time")
        t.start(lambda _raw: None)
        try:
            _wait_for(lambda: t._warned and t._thread is not None)
            assert t._thread is not None and t._thread.is_alive()
        finally:
            t.stop()
        assert any(k.startswith("poll:") for k in t._warned)


class TestRetention:
    """Mail leaves the wire by time, never because somebody read it.

    Every machine that holds a name reads that mailbox, and none of them can
    know how many others are still offline. So the reap sweeps every object
    once both its ULID mint time and its `LastModified` clear `retain_days`:
    consumed or not, well-formed or junk.
    """

    def _old(self) -> str:
        return _ulid_at_ms(int((time.time() - 10 * 86400) * 1000))

    def test_an_expired_consumed_envelope_is_reaped(self, fake_home, capsys):
        _register("target")
        t, bucket = _transport()
        old = self._old()
        key = f"{PREFIX}/target/{old}.json"
        bucket.put_object(Bucket=BUCKET, Key=key, Body=_envelope(old))
        t._ledger.record(old)
        bucket.age(key, 10)
        _arm(t, lambda _raw: True)
        t._poll_once()
        assert bucket.keys() == []
        assert "past retain_days" in capsys.readouterr().out

    def test_an_expired_envelope_nobody_took_is_reaped_unread(self, fake_home):
        _register("target")
        t, bucket = _transport()
        old = self._old()
        key = f"{PREFIX}/target/{old}.json"
        bucket.put_object(Bucket=BUCKET, Key=key, Body=_envelope(old))
        bucket.age(key, 10)
        seen: list[bytes] = []
        _arm(t, seen.append)
        t._poll_once()
        assert bucket.keys() == []
        assert seen == []
        assert ("get", key) not in bucket.calls

    def test_a_fresh_envelope_is_left(self, fake_home):
        _register("target")
        t, bucket = _transport()
        msg_id = new_ulid()
        key = f"{PREFIX}/target/{msg_id}.json"
        bucket.put_object(Bucket=BUCKET, Key=key, Body=_envelope(msg_id))
        _arm(t, lambda _raw: False)
        t._poll_once()
        assert bucket.keys() == [key]

    def test_a_consumed_but_fresh_envelope_is_left(self, fake_home):
        _register("target")
        t, bucket = _transport()
        msg_id = new_ulid()
        key = f"{PREFIX}/target/{msg_id}.json"
        bucket.put_object(Bucket=BUCKET, Key=key, Body=_envelope(msg_id))
        _arm(t, lambda _raw: True)
        t._poll_once()
        t._poll_once()
        assert msg_id in t._ledger
        assert bucket.keys() == [key]

    def test_a_delayed_send_of_an_old_envelope_is_delivered(self, fake_home):
        """A backoff retry republishes the original envelope, ULID and all, so
        an old mint time on a fresh object is a late message, not an old one."""
        _register("target")
        t, bucket = _transport()
        old = self._old()
        key = f"{PREFIX}/target/{old}.json"
        bucket.put_object(Bucket=BUCKET, Key=key, Body=_envelope(old))
        seen: list[bytes] = []
        _arm(t, lambda raw: seen.append(raw) or True)
        t._poll_once()
        assert len(seen) == 1
        assert bucket.keys() == [key]

    def test_retain_days_zero_keeps_mail_forever(self, fake_home):
        _register("target")
        t, bucket = _transport(retain_days=0)
        old = _ulid_at_ms(int((time.time() - 400 * 86400) * 1000))
        key = f"{PREFIX}/target/{old}.json"
        bucket.put_object(Bucket=BUCKET, Key=key, Body=_envelope(old))
        t._ledger.record(old)
        bucket.age(key, 400)
        _arm(t, lambda _raw: True)
        t._poll_once()
        assert bucket.keys() == [key]

    def test_a_refused_reap_is_tried_again(self, fake_home):
        _register("target")
        t, bucket = _transport()
        old = self._old()
        key = f"{PREFIX}/target/{old}.json"
        bucket.put_object(Bucket=BUCKET, Key=key, Body=_envelope(old))
        bucket.age(key, 10)
        bucket.fail.add("delete")
        _arm(t, lambda _raw: True)
        t._poll_once()
        assert bucket.keys() == [key]
        bucket.fail.discard("delete")
        t._poll_once()
        assert bucket.keys() == []

    def test_an_undeliverable_object_is_dropped_once_both_clocks_agree(
        self, fake_home, capsys
    ):
        _register("target")
        t, bucket = _transport()
        old = _ulid_at_ms(int((time.time() - 10 * 86400) * 1000))
        key = f"{PREFIX}/target/{old}.json"
        bucket.put_object(Bucket=BUCKET, Key=key, Body=b"not an envelope")
        bucket.age(key, 10)
        _arm(t, lambda _raw: None)
        t._poll_once()
        assert bucket.keys() == []
        assert "past retain_days" in capsys.readouterr().out

    def test_a_fresh_write_of_an_old_ulid_survives(self, fake_home):
        _register("target")
        t, bucket = _transport()
        old = _ulid_at_ms(int((time.time() - 10 * 86400) * 1000))
        key = f"{PREFIX}/target/{old}.json"
        bucket.put_object(Bucket=BUCKET, Key=key, Body=b"not an envelope")
        _arm(t, lambda _raw: None)
        t._poll_once()
        assert bucket.keys() == [key]

    def test_an_old_object_with_a_recent_ulid_survives(self, fake_home):
        _register("target")
        t, bucket = _transport()
        key = f"{PREFIX}/target/{new_ulid()}.json"
        bucket.put_object(Bucket=BUCKET, Key=key, Body=b"not an envelope")
        bucket.age(key, 10)
        _arm(t, lambda _raw: None)
        t._poll_once()
        assert bucket.keys() == [key]

    def test_retain_days_zero_keeps_forever(self, fake_home):
        _register("target")
        t, bucket = _transport(retain_days=0)
        old = _ulid_at_ms(int((time.time() - 400 * 86400) * 1000))
        key = f"{PREFIX}/target/{old}.json"
        bucket.put_object(Bucket=BUCKET, Key=key, Body=b"not an envelope")
        bucket.age(key, 400)
        _arm(t, lambda _raw: None)
        t._poll_once()
        assert bucket.keys() == [key]

    def test_junk_with_no_ulid_ages_out_on_the_object_clock_alone(self, fake_home):
        _register("target")
        t, bucket = _transport()
        key = f"{PREFIX}/target/notes.txt"
        bucket.put_object(Bucket=BUCKET, Key=key, Body=b"stray")
        bucket.age(key, 10)
        _arm(t, lambda _raw: None)
        t._poll_once()
        assert bucket.keys() == []


class TestProbe:
    def test_probe_exercises_every_verb_and_leaves_nothing(self, fake_home):
        t, bucket = _transport(probe=True)
        t.start(lambda _raw: None)
        try:
            assert t.is_connected() is True
        finally:
            t.stop()
        assert bucket.keys() == []
        assert [verb for verb, _key in bucket.calls] == [
            "put",
            "get",
            "list",
            "delete",
        ]

    def test_probe_starts_no_thread(self, fake_home):
        t, _bucket = _transport(probe=True)
        t.start(lambda _raw: None)
        try:
            assert t._thread is None
        finally:
            t.stop()

    @pytest.mark.parametrize(
        "verb, message",
        [("put", "s3 put failed"), ("get", "s3 read failed"), ("delete", "s3 delete failed")],
    )
    def test_a_refused_verb_fails_the_probe(self, fake_home, verb, message):
        t, bucket = _transport(probe=True)
        bucket.fail.add(verb)
        with pytest.raises(TransportError, match=message):
            t.start(lambda _raw: None)

    def test_the_missing_verb_is_the_one_reported(self, fake_home):
        # A policy granting neither read nor delete has to name the first
        # thing that failed. Cleaning up afterwards is not a second finding
        # to report over the top of the first.
        t, bucket = _transport(probe=True)
        bucket.fail.update({"get", "delete"})
        with pytest.raises(TransportError, match="s3 read failed"):
            t.start(lambda _raw: None)

    def test_a_failed_put_still_takes_its_probe_object_away(self, fake_home):
        # A put that wrote the object and lost the answer leaves it behind
        # forever otherwise: nothing else in this transport would ever look at
        # a key under its own prefix that is not an envelope, except the reap.
        t, bucket = _transport(probe=True)

        def put_then_fail(*, Bucket, Key, Body):
            bucket.objects[Key] = (Body, datetime.now(timezone.utc))
            raise RuntimeError("the answer never came back")

        bucket.put_object = put_then_fail
        with pytest.raises(TransportError, match="s3 put failed"):
            t.start(lambda _raw: None)
        assert bucket.keys() == []


class TestLazyDependency:
    def test_the_client_installs_the_group_at_first_use(self, fake_home, monkeypatch):
        from ar3 import deps as ar3_deps

        calls: list[str] = []

        def fail(group: str):
            calls.append(group)
            raise RuntimeError("no network in test")

        monkeypatch.setattr(ar3_deps, "ensure_group", lambda g: None)
        monkeypatch.setattr(ar3_deps, "install_group", fail)
        t = S3Transport(remote_id="hub", bucket=BUCKET, prefix=PREFIX)
        with pytest.raises(TransportError, match="installing a8s-s3"):
            t.publish(_envelope(new_ulid()))
        assert calls == ["a8s-s3"]

    def test_a_failed_install_is_not_retried_every_poll(self, fake_home, monkeypatch):
        # Installing is a pip subprocess. A poll loop that reruns one every
        # `poll_seconds` for as long as the machine is offline costs more than
        # the mail it is failing to fetch is worth.
        from ar3 import deps as ar3_deps
        from transports import s3 as s3_module

        calls: list[str] = []

        def fail(group: str):
            calls.append(group)
            raise RuntimeError("no network in test")

        monkeypatch.setattr(ar3_deps, "ensure_group", lambda g: None)
        monkeypatch.setattr(ar3_deps, "install_group", fail)
        _register("target")
        t = S3Transport(remote_id="hub", bucket=BUCKET, prefix=PREFIX, poll_seconds=1)
        _arm(t, lambda _raw: None)
        for _ in range(5):
            with pytest.raises(TransportError, match="installing a8s-s3"):
                t._drain("target")
        assert calls == ["a8s-s3"]

        clock = time.monotonic() + s3_module.CLIENT_RETRY_SECONDS + 1
        monkeypatch.setattr(s3_module.time, "monotonic", lambda: clock)
        with pytest.raises(TransportError, match="installing a8s-s3"):
            t._drain("target")
        assert calls == ["a8s-s3", "a8s-s3"]

    def test_construction_alone_touches_no_dependency(self, fake_home, monkeypatch):
        from ar3 import deps as ar3_deps

        def boom(group: str):
            raise AssertionError("construction must not reach for boto3")

        monkeypatch.setattr(ar3_deps, "ensure_group", boom)
        monkeypatch.setattr(ar3_deps, "install_group", boom)
        S3Transport(remote_id="hub", bucket=BUCKET, prefix=PREFIX)


class TestDispatch:
    def test_build_transport_requires_bucket_and_prefix(self, fake_home):
        from network import _build_transport

        with pytest.raises(ValueError, match="requires `bucket` and `prefix`"):
            _build_transport("hub", {"transport": "s3", "prefix": PREFIX})
        with pytest.raises(ValueError, match="requires `bucket` and `prefix`"):
            _build_transport("hub", {"transport": "s3", "bucket": BUCKET})

    def test_build_transport_forwards_options(self, fake_home):
        from network import _build_transport

        t = _build_transport(
            "hub",
            {
                "transport": "s3",
                "bucket": BUCKET,
                "prefix": PREFIX,
                "region": "us-west-2",
                "poll_seconds": "30",
            },
        )
        assert isinstance(t, S3Transport)
        assert t.id == "hub"
        assert t._region == "us-west-2"
        assert t._poll_seconds == 30.0

    def test_deps_group(self):
        from network import transport_deps_group_for

        assert transport_deps_group_for("s3") == "a8s-s3"
        assert transport_deps_group_for("folder") is None


def test_delivered_mail_crosses_two_nodes(fake_home, monkeypatch, tmp_path):
    """One bucket, two nodes, and the round trip a receipt makes.

    Each node has its own registry, so the fixture swaps the config home
    between turns — the thing under test is that the sending node writes where
    the receiving node looks, and that the receipt comes back the other way.
    """
    from conftest import set_home

    shared = FakeBucket()

    def node(home) -> S3Transport:
        set_home(monkeypatch, home)
        t = S3Transport(remote_id="hub", bucket=BUCKET, prefix=PREFIX)
        t._client_cache = shared
        return t

    sender_home = tmp_path / "node-a"
    receiver_home = tmp_path / "node-b"
    for home in (sender_home, receiver_home):
        (home / ".a8s").mkdir(parents=True)

    set_home(monkeypatch, sender_home)
    _register("example-sender")
    msg_id = new_ulid()
    node(sender_home).publish(_envelope(msg_id, to="target"))

    set_home(monkeypatch, receiver_home)
    _register("target")
    receiver = node(receiver_home)
    arrived: list[bytes] = []
    receiver.start(arrived.append)
    try:
        receiver._poll_once()
    finally:
        receiver.stop()
    assert [json.loads(raw)["id"] for raw in arrived] == [msg_id]
    assert shared.keys() == [f"{PREFIX}/target/{msg_id}.json"]

    receipt = build_delivery_receipt(json.loads(arrived[0]), ["target"])
    node(receiver_home).publish(json.dumps(receipt).encode("utf-8"))

    set_home(monkeypatch, sender_home)
    sender = node(sender_home)
    back: list[bytes] = []
    sender.start(back.append)
    try:
        sender._poll_once()
    finally:
        sender.stop()
    assert [json.loads(raw)["id"] for raw in back] == [receipt["id"]]
    assert f"{PREFIX}/example-sender/{receipt['id']}.json" in shared.keys()


def test_every_address_shape_reaches_the_node_that_holds_it(
    fake_home, monkeypatch, tmp_path
):
    """The sender files under one path segment and the owner lists one path
    segment, so the two have to agree for every shape an address takes: a
    plain name, an alias, a namespace address, and a namespace bound
    `--opaque`.

    `--opaque` is presentation policy, kept in `namespace_options` rather than
    in the routing map, so a concealed prefix is still a prefix this node binds
    and therefore still one it polls.
    """
    from conftest import set_home

    shared = FakeBucket()

    def node(home) -> S3Transport:
        set_home(monkeypatch, home)
        t = S3Transport(remote_id="hub", bucket=BUCKET, prefix=PREFIX)
        t._client_cache = shared
        return t

    sender_home = tmp_path / "node-a"
    receiver_home = tmp_path / "node-b"
    for home in (sender_home, receiver_home):
        (home / ".a8s").mkdir(parents=True)

    shapes = {
        "target": "target",
        "team": "team",
        "acme:ops:example-agent": "acme",
        "hush:ops:example-agent": "hush",
    }
    set_home(monkeypatch, sender_home)
    _register("example-sender")
    sender = node(sender_home)
    sent = {}
    for to in shapes:
        msg_id = new_ulid()
        sent[msg_id] = to
        sender.publish(_envelope(msg_id, to=to))
    assert sorted(shared.keys()) == sorted(
        f"{PREFIX}/{segment}/{msg_id}.json"
        for msg_id, to in sent.items()
        for segment in [shapes[to]]
    )

    set_home(monkeypatch, receiver_home)
    _register("target")
    save_aliases({"team": ["target"]})
    save_namespaces({"acme": "target", "hush": "target"})
    save_namespace_options({"hush": {"opaque": True}})
    receiver = node(receiver_home)
    arrived: list[bytes] = []
    _arm(receiver, lambda raw: arrived.append(raw) or True)
    receiver._poll_once()
    assert sorted(json.loads(raw)["to"] for raw in arrived) == sorted(shapes)
    assert len(shared.keys()) == len(shapes)


def test_a_sibling_daemon_s_claim_does_not_cost_the_message(fake_home, tmp_path):
    """The whole chain rather than a stand-in for it: the real receive
    callback, the real claim file, and the bucket that holds the only copy.

    Every daemon on a machine runs its own subscriber over the same registry,
    so `a8s run alice` and `a8s run bob` both poll this mailbox. One wins the
    claim; the other is handed an envelope it did not consume and must not
    record it, or a sibling that dies mid-delivery costs the message.
    """
    from core import Participant, inbox_dir
    from network import claim_message, make_receive_callback, release_claim

    root = tmp_path / "target"
    root.mkdir()
    save_registry({"target": {"root": str(root)}})
    t, bucket = _transport()
    msg_id = new_ulid()
    key = f"{PREFIX}/target/{msg_id}.json"
    bucket.put_object(Bucket=BUCKET, Key=key, Body=_envelope(msg_id, to="target"))
    participants = [Participant("target", root)]
    _arm(t, make_receive_callback(lambda: participants, services=[]))

    assert claim_message(msg_id) is True  # the sibling got there first
    t._poll_once()
    assert bucket.keys() == [key]
    assert not inbox_dir("target").is_dir()

    # The sibling died mid-delivery: the claim lapses, nothing reached the
    # ring, and this node's next poll is the redelivery the wire promises.
    release_claim(msg_id)
    t._poll_once()
    assert msg_id in t._ledger
    assert [p.name for p in inbox_dir("target").iterdir()] == [f"{msg_id}.json"]


def test_a_remote_with_no_join_cutoff_consumes_what_is_there(fake_home):
    """A spec written by hand carries no `joined`, so nothing is backlog."""
    _register("target")
    t, bucket = _transport()
    old = _ulid_at_ms(int((time.time() - 30 * 86400) * 1000))
    raw = _envelope(old)
    bucket.put_object(Bucket=BUCKET, Key=f"{PREFIX}/target/{old}.json", Body=raw)
    bucket.objects[f"{PREFIX}/target/{old}.json"] = (
        raw,
        datetime.now(timezone.utc) - timedelta(hours=1),
    )
    seen: list[bytes] = []
    _arm(t, seen.append)
    t._poll_once()
    assert seen == [raw]


def test_an_inbox_that_would_not_take_it_leaves_the_object_on_the_wire(
    fake_home, tmp_path, monkeypatch
):
    """The real callback, the real writer, and a disk that says no.

    An envelope this node cannot file is not an envelope this node is done
    with. Stamping the ledger here would skip the object on every later poll:
    repair the disk afterwards and this machine never reads it again.
    """
    import network
    from core import Participant, inbox_dir
    from network import make_receive_callback, seen_id_contains

    root = tmp_path / "target"
    root.mkdir()
    save_registry({"target": {"root": str(root)}})
    t, bucket = _transport()
    msg_id = new_ulid()
    key = f"{PREFIX}/target/{msg_id}.json"
    bucket.put_object(Bucket=BUCKET, Key=key, Body=_envelope(msg_id, to="target"))
    participants = [Participant("target", root)]
    _arm(t, make_receive_callback(lambda: participants, services=[]))

    real_write = network._write_to_inbox
    monkeypatch.setattr(network, "_write_to_inbox", lambda *a, **k: False)
    t._poll_once()
    assert msg_id not in t._ledger, "not consumed, so offered again"
    assert seen_id_contains(msg_id) is False, "a failure is not a receipt"

    monkeypatch.setattr(network, "_write_to_inbox", real_write)
    t._poll_once()
    assert msg_id in t._ledger
    assert [p.name for p in inbox_dir("target").iterdir()] == [f"{msg_id}.json"]


class TestPaging:
    """One poll reads one page, and the page has to move.

    Nothing leaves a mailbox until it expires, so its front holds every
    envelope this machine already consumed, a delivery the receive path
    refused, an object that would not fetch, junk still inside the retention
    window, and on AWS a CommonPrefix, which spends the key budget an object
    would. A poll that always asks for the first page therefore stops at the
    first page of mail it has already read.
    """

    def test_consumed_mail_at_the_front_costs_one_list_and_no_get(self, fake_home):
        _register("target")
        t, bucket = _transport()
        ids = sorted(new_ulid() for _ in range(PAGE_SIZE))
        for msg_id in ids:
            bucket.put_object(
                Bucket=BUCKET,
                Key=f"{PREFIX}/target/{msg_id}.json",
                Body=_envelope(msg_id),
            )
        t._ledger.record(*ids)
        seen: list[str] = []
        _arm(t, lambda raw: seen.append(json.loads(raw)["id"]) or True)
        bucket.calls.clear()
        t._poll_once()
        assert [verb for verb, _key in bucket.calls] == ["list"]

        fresh = new_ulid()
        bucket.put_object(
            Bucket=BUCKET, Key=f"{PREFIX}/target/{fresh}.json", Body=_envelope(fresh)
        )
        bucket.calls.clear()
        t._poll_once()
        t._poll_once()
        assert seen == [fresh], "the second page is where the new mail is"
        assert [verb for verb, _key in bucket.calls].count("get") == 1

    def test_retained_envelopes_do_not_pin_the_mail_behind_them(self, fake_home):
        _register("target")
        t, bucket = _transport()
        ids = sorted(new_ulid() for _ in range(PAGE_SIZE + 1))
        for msg_id in ids:
            bucket.put_object(
                Bucket=BUCKET,
                Key=f"{PREFIX}/target/{msg_id}.json",
                Body=_envelope(msg_id),
            )
        stuck = set(ids[:PAGE_SIZE])
        last = ids[-1]
        attempted: list[str] = []

        def cb(raw: bytes) -> bool:
            msg_id = json.loads(raw)["id"]
            attempted.append(msg_id)
            return msg_id not in stuck

        _arm(t, cb)
        t._poll_once()
        assert last not in attempted, "a full page of mail comes before it"
        t._poll_once()
        assert last in attempted
        assert last in t._ledger

    def test_a_page_of_only_prefixes_does_not_pin_the_listing(self, fake_home):
        _register("target")
        t, bucket = _transport()
        ids = sorted(new_ulid() for _ in range(PAGE_SIZE + 1))
        for folder in ids[:PAGE_SIZE]:
            bucket.put_object(
                Bucket=BUCKET,
                Key=f"{PREFIX}/target/{folder}/attachment.txt",
                Body=b"x",
            )
        msg_id = ids[-1]
        bucket.put_object(
            Bucket=BUCKET, Key=f"{PREFIX}/target/{msg_id}.json", Body=_envelope(msg_id)
        )
        seen: list[str] = []
        _arm(t, lambda raw: seen.append(json.loads(raw)["id"]) or True)
        t._poll_once()
        assert seen == [], "the first page is nothing but attachment folders"
        t._poll_once()
        assert seen == [msg_id]

    def test_one_poll_still_reads_one_page(self, fake_home, monkeypatch):
        monkeypatch.setattr("transports.s3.PAGE_SIZE", 2)
        _register("target")
        t, bucket = _transport()
        ids = sorted(new_ulid() for _ in range(5))
        for msg_id in ids:
            bucket.put_object(
                Bucket=BUCKET,
                Key=f"{PREFIX}/target/{msg_id}.json",
                Body=_envelope(msg_id),
            )
        seen: list[str] = []
        _arm(t, lambda raw: seen.append(json.loads(raw)["id"]) or True)
        t._poll_once()
        assert seen == ids[:2]

    def test_the_cursor_wraps_so_a_refusal_comes_back_around(
        self, fake_home, monkeypatch
    ):
        monkeypatch.setattr("transports.s3.PAGE_SIZE", 1)
        _register("target")
        t, bucket = _transport()
        first, second = sorted([new_ulid(), new_ulid()])
        for msg_id in (first, second):
            bucket.put_object(
                Bucket=BUCKET,
                Key=f"{PREFIX}/target/{msg_id}.json",
                Body=_envelope(msg_id),
            )
        attempted: list[str] = []

        def cb(raw: bytes) -> bool:
            msg_id = json.loads(raw)["id"]
            attempted.append(msg_id)
            return False

        _arm(t, cb)
        for _ in range(3):
            t._poll_once()
        assert attempted == [first, second, first], "the end of the listing starts over"

    def test_a_token_the_bucket_refuses_starts_the_mailbox_over(
        self, fake_home, monkeypatch
    ):
        monkeypatch.setattr("transports.s3.PAGE_SIZE", 1)
        _register("target")
        t, bucket = _transport()
        first, second = sorted([new_ulid(), new_ulid()])
        for msg_id in (first, second):
            bucket.put_object(
                Bucket=BUCKET,
                Key=f"{PREFIX}/target/{msg_id}.json",
                Body=_envelope(msg_id),
            )
        _arm(t, lambda _raw: False)
        t._poll_once()
        assert t._cursor.get("target")
        bucket.fail.add("list")
        t._poll_once()
        assert "target" not in t._cursor
        bucket.fail.discard("list")
        attempted: list[str] = []
        _arm(t, lambda raw: attempted.append(json.loads(raw)["id"]) or False)
        t._poll_once()
        assert attempted == [first]

    def test_a_mailbox_that_goes_away_drops_its_cursor(self, fake_home, monkeypatch):
        monkeypatch.setattr("transports.s3.PAGE_SIZE", 1)
        _register("target")
        t, bucket = _transport()
        for _ in range(2):
            msg_id = new_ulid()
            bucket.put_object(
                Bucket=BUCKET,
                Key=f"{PREFIX}/target/{msg_id}.json",
                Body=_envelope(msg_id),
            )
        _arm(t, lambda _raw: False)
        t._poll_once()
        assert "target" in t._cursor
        _register("other")
        t._poll_once()
        assert "target" not in t._cursor



def test_a_name_on_two_machines_gets_a_copy_on_each(fake_home, monkeypatch, tmp_path):
    """The owner's case: one name, two machines, one bucket.

    MQTT hands every subscriber on the topic a copy, and each node filters by
    its own registry. The bucket has to match: the first machine to read the
    envelope leaves it for the second, and each one records the read in its
    own ledger under its own config home.
    """
    from conftest import set_home

    shared = FakeBucket()
    homes = [tmp_path / "machine-a", tmp_path / "machine-b"]
    for home in homes:
        (home / ".a8s").mkdir(parents=True)
    msg_id = new_ulid()
    key = f"{PREFIX}/target/{msg_id}.json"
    shared.put_object(Bucket=BUCKET, Key=key, Body=_envelope(msg_id, to="target"))

    arrived: dict[str, list[str]] = {}
    for home in homes:
        set_home(monkeypatch, home)
        _register("target")
        t = S3Transport(remote_id="hub", bucket=BUCKET, prefix=PREFIX)
        t._client_cache = shared
        got = arrived.setdefault(home.name, [])
        _arm(t, lambda raw, got=got: got.append(json.loads(raw)["id"]) or True)
        t._poll_once()
        t._poll_once()
    assert arrived == {"machine-a": [msg_id], "machine-b": [msg_id]}
    assert shared.keys() == [key]


class TestJoined:
    """A machine that joins is owed the mail sent after it joined, as MQTT
    gives a new session nothing published before it existed."""

    def test_mail_minted_before_the_join_is_not_delivered(self, fake_home):
        from transports.s3 import JOIN_SKEW_GRACE_MS

        _register("target")
        now_ms = int(time.time() * 1000)
        joined = _ulid_at_ms(now_ms)
        t, bucket = _transport(joined=joined)
        history = _ulid_at_ms(now_ms - JOIN_SKEW_GRACE_MS - 60_000)
        late = _ulid_at_ms(now_ms - JOIN_SKEW_GRACE_MS + 60_000)
        for msg_id in (history, late):
            bucket.put_object(
                Bucket=BUCKET,
                Key=f"{PREFIX}/target/{msg_id}.json",
                Body=_envelope(msg_id),
            )
        seen: list[str] = []
        _arm(t, lambda raw: seen.append(json.loads(raw)["id"]) or True)
        t._poll_once()
        assert seen == [late], "inside the skew grace is current mail"
        assert ("get", f"{PREFIX}/target/{history}.json") not in bucket.calls
        assert len(bucket.keys()) == 2

    def test_joined_must_be_a_ulid(self):
        with pytest.raises(ValueError, match="joined must be a ULID"):
            _transport(joined="yesterday")


class TestLedgerCompaction:
    """The ledger cap compacts to what is still in the bucket."""

    def test_ids_whose_object_is_gone_are_forgotten(self, fake_home, monkeypatch):
        import transports.ledger as ledger_mod

        monkeypatch.setattr(ledger_mod, "MAX_SEEN_IDS", 2)
        _register("target")
        t, bucket = _transport()
        ids = sorted(new_ulid() for _ in range(3))
        for msg_id in ids:
            bucket.put_object(
                Bucket=BUCKET,
                Key=f"{PREFIX}/target/{msg_id}.json",
                Body=_envelope(msg_id),
            )
        _arm(t, lambda _raw: True)
        t._poll_once()
        assert sorted(t._ledger.ids) == ids, "all three are still in the bucket"
        bucket.delete_object(Bucket=BUCKET, Key=f"{PREFIX}/target/{ids[0]}.json")
        t._last_compact = 0.0
        t._ledger.record(new_ulid())
        assert ids[0] not in t._ledger
        assert set(ids[1:]) <= t._ledger.ids

    def test_a_failed_listing_keeps_the_whole_ledger(self, fake_home, monkeypatch):
        import transports.ledger as ledger_mod

        monkeypatch.setattr(ledger_mod, "MAX_SEEN_IDS", 2)
        _register("target")
        t, bucket = _transport()
        ids = sorted(new_ulid() for _ in range(3))
        bucket.fail.add("list")
        t._ledger.record(*ids)
        assert sorted(t._ledger.ids) == ids

    def test_compaction_lists_the_bucket_at_most_once_an_hour(
        self, fake_home, monkeypatch
    ):
        """With `retain_days 0` nothing ever leaves, so the ledger stays over
        the cap and every consumption would otherwise walk every mailbox."""
        import transports.ledger as ledger_mod

        monkeypatch.setattr(ledger_mod, "MAX_SEEN_IDS", 1)
        _register("target")
        t, bucket = _transport()
        t._ledger.record(new_ulid(), new_ulid())
        t._ledger.record(new_ulid())
        t._ledger.record(new_ulid())
        assert [verb for verb, _key in bucket.calls].count("list") == 1


class TestOutboxAcrossARestart:
    """Mail a node could not send while it was down goes out when it starts
    again, with nobody touching it: `a8s start` runs this same resident loop.

    A host restart is the case — the node is told to stop, the network goes
    with it, and whatever sat in the outbox waits for the next start."""

    def _run_until(self, monkeypatch, transport, done, timeout: float = 10.0) -> None:
        import threading

        import daemon as daemon_mod
        from daemon import attached_loop

        monkeypatch.setenv("A8S_WATCHDOG_WEDGE_SECONDS", "0")
        monkeypatch.setattr(daemon_mod, "load_remotes", lambda node=None: [transport])

        def stop_when_done() -> None:
            deadline = time.monotonic() + timeout
            while daemon_mod._STOP_EVENT is None and time.monotonic() < deadline:
                time.sleep(0.01)
            while not done() and time.monotonic() < deadline:
                time.sleep(0.01)
            stop = daemon_mod._STOP_EVENT
            if stop is not None:
                stop.set()

        watcher = threading.Thread(target=stop_when_done, daemon=True)
        watcher.start()
        assert attached_loop(["sender"], 0.01, single_pass=False) == 0
        watcher.join(timeout=1.0)

    def _sender(self, tmp_path):
        root = tmp_path / "sender"
        root.mkdir()
        save_registry({"sender": {"root": str(root)}})
        return root

    def test_mail_written_while_the_node_was_down_publishes_on_start(
        self, fake_home, tmp_path, monkeypatch
    ):
        from mailbox import _write_outbox

        root = self._sender(tmp_path)
        msg_id = _write_outbox("sender", root, "far-agent", "sent while down", []).stem
        key = f"{PREFIX}/far-agent/{msg_id}.json"
        t, bucket = _transport()

        self._run_until(monkeypatch, t, lambda: key in bucket.objects)

        assert key in bucket.objects
        assert json.loads(bucket.objects[key][0])["content"] == "sent while down"

    def test_mail_whose_publish_failed_before_the_stop_publishes_on_start(
        self, fake_home, tmp_path, monkeypatch
    ):
        """The last run tried, the bucket refused, and the node stopped with
        the message parked for a backoff retry. The next start retries it
        once that backoff is due; the first step is 30 seconds."""
        import mailbox as mailbox_mod
        from core import pending_dir
        from daemon import attached_loop
        from mailbox import _write_outbox

        root = self._sender(tmp_path)
        msg_id = _write_outbox("sender", root, "far-agent", "parked", []).stem
        key = f"{PREFIX}/far-agent/{msg_id}.json"

        down, down_bucket = _transport()
        down_bucket.fail.add("put")
        monkeypatch.setattr(mailbox_mod, "BACKOFF_SCHEDULE", [0] * 9)
        import daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "load_remotes", lambda node=None: [down])
        assert attached_loop(["sender"], 0.01, single_pass=True) == 0
        assert key not in down_bucket.objects
        sidecars = list(pending_dir("sender").glob("*.retry"))
        assert len(sidecars) == 1
        assert json.loads(sidecars[0].read_text())["attempts"] == 1

        t, bucket = _transport()
        self._run_until(monkeypatch, t, lambda: key in bucket.objects)

        assert key in bucket.objects
        assert not list(pending_dir("sender").iterdir())
