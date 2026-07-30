"""Two-cluster MQTT integration — sender and recipient on opposite A8S_HOME
trees with real mosquitto, exercising publish/receive, conversations.sqlite3,
and `a8s convo` end to end."""
from __future__ import annotations

import os
import time

import pytest

pytest.importorskip("paho.mqtt.client")

from commands import cmd_convo
from convo import load_entries
from core import inbox_dir
from mqtt_cluster import (
    ensure_agent_mailboxes,
    setup_cluster,
    start_attached_loop,
    stop_attached_loop,
    using_a8s_home,
    wait_agent_log,
    wait_convo,
    write_outbox,
)


def _unique_topic(prefix: str) -> str:
    return f"a8s/{prefix}-{os.getpid()}-{int(time.time() * 1000)}"


class TestDualClusterRemote:
    def test_bidirectional_delivery_convo_and_cmd(self, tmp_path, mqtt_broker, capsys):
        """Two handlers on separate A8S_HOME dirs share one broker topic.
        Messages published from each side arrive at the other; both
        conversations.sqlite3 archives and `a8s convo` show inbound/outbound."""
        topic = _unique_topic("dual")
        alice_a8s = tmp_path / "alice_a8s"
        bob_a8s = tmp_path / "bob_a8s"
        alice_root = tmp_path / "alice_root"
        bob_root = tmp_path / "bob_root"
        alice_root.mkdir(parents=True)
        bob_root.mkdir(parents=True)

        setup_cluster(
            alice_a8s,
            port=mqtt_broker,
            topic=topic,
            client_id=f"a8s-test-alice-{os.getpid()}",
            agents={"ALICE": alice_root},
        )
        setup_cluster(
            bob_a8s,
            port=mqtt_broker,
            topic=topic,
            client_id=f"a8s-test-bob-{os.getpid()}",
            agents={"BOB": bob_root},
        )
        ensure_agent_mailboxes(alice_a8s, {"ALICE": alice_root})
        ensure_agent_mailboxes(bob_a8s, {"BOB": bob_root})

        alice_proc = start_attached_loop(alice_a8s, "ALICE")
        bob_proc = start_attached_loop(bob_a8s, "BOB")
        try:
            time.sleep(0.8)

            write_outbox(alice_a8s, "ALICE", alice_root, "BOB", "hello from alice")
            wait_agent_log(alice_a8s, "ALICE", "remote hub: published -> BOB: hello from alice")
            wait_agent_log(bob_a8s, "BOB", "received from ALICE (via remote): hello from alice")

            wait_convo(
                alice_a8s,
                predicate=lambda rows: any(
                    r.get("from") == "ALICE"
                    and r.get("to") == "BOB"
                    and r.get("content") == "hello from alice"
                    for r in rows
                ),
            )
            wait_convo(
                bob_a8s,
                predicate=lambda rows: any(
                    r.get("from") == "ALICE"
                    and r.get("to") == "BOB"
                    and "BOB" in (r.get("recipients") or [])
                    for r in rows
                ),
            )

            write_outbox(bob_a8s, "BOB", bob_root, "ALICE", "hello from bob")
            wait_agent_log(bob_a8s, "BOB", "remote hub: published -> ALICE: hello from bob")
            wait_agent_log(alice_a8s, "ALICE", "received from BOB (via remote): hello from bob")

            wait_convo(
                bob_a8s,
                predicate=lambda rows: any(
                    r.get("from") == "BOB"
                    and r.get("to") == "ALICE"
                    and r.get("content") == "hello from bob"
                    for r in rows
                ),
            )

            with using_a8s_home(alice_a8s):
                assert cmd_convo(["ALICE", "--limit", "10"]) == 0
            alice_out = capsys.readouterr().out
            assert "## from ALICE to BOB" in alice_out
            assert "### from BOB to ALICE" in alice_out
            assert "hello from alice" in alice_out
            assert "hello from bob" in alice_out

            with using_a8s_home(bob_a8s):
                assert cmd_convo(["BOB", "--limit", "10"]) == 0
            bob_out = capsys.readouterr().out
            assert "### from ALICE to BOB" in bob_out
            assert "## from BOB to ALICE" in bob_out

            wait_agent_log(alice_a8s, "ALICE", "remote hub: published -> BOB: hello from alice")
            wait_agent_log(bob_a8s, "BOB", "remote hub: published -> ALICE: hello from bob")
        finally:
            stop_attached_loop(alice_proc)
            stop_attached_loop(bob_proc)

    def test_mismatched_topics_do_not_cross_deliver(self, tmp_path, mqtt_broker):
        """Subscribe and publish both use the configured topic — a typo on
        one cluster means MQTT connects fine but envelopes never arrive."""
        topic_a = _unique_topic("topic-a")
        topic_b = _unique_topic("topic-b")
        alice_a8s = tmp_path / "alice_a8s"
        bob_a8s = tmp_path / "bob_a8s"
        alice_root = tmp_path / "alice_root"
        bob_root = tmp_path / "bob_root"
        alice_root.mkdir(parents=True)
        bob_root.mkdir(parents=True)

        setup_cluster(
            alice_a8s,
            port=mqtt_broker,
            topic=topic_a,
            client_id=f"a8s-test-alice-mismatch-{os.getpid()}",
            agents={"ALICE": alice_root},
        )
        setup_cluster(
            bob_a8s,
            port=mqtt_broker,
            topic=topic_b,
            client_id=f"a8s-test-bob-mismatch-{os.getpid()}",
            agents={"BOB": bob_root},
        )
        ensure_agent_mailboxes(alice_a8s, {"ALICE": alice_root})
        ensure_agent_mailboxes(bob_a8s, {"BOB": bob_root})

        alice_proc = start_attached_loop(alice_a8s, "ALICE")
        bob_proc = start_attached_loop(bob_a8s, "BOB")
        try:
            time.sleep(0.8)
            write_outbox(alice_a8s, "ALICE", alice_root, "BOB", "wrong topic")

            with using_a8s_home(bob_a8s):
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    if list(inbox_dir("BOB").glob("*.json")):
                        pytest.fail("BOB received message despite topic mismatch")
                    time.sleep(0.05)

            with using_a8s_home(alice_a8s):
                rows = load_entries()
                assert any(r.get("content") == "wrong topic" for r in rows), (
                    "sender should still archive outbound remote publish"
                )
        finally:
            stop_attached_loop(alice_proc)
            stop_attached_loop(bob_proc)
