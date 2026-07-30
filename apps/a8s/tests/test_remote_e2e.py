"""End-to-end remote-routing test (issue #63).

Spawns a real `mosquitto` broker, configures one cluster to send and
another to receive, and verifies a `tell` reaches the receiver's inbox via
the broker. The send side runs through `attached_loop` to exercise the
daemon's wiring of `publish_remotes`. The receive side uses `start_remotes`
directly so we can wait deterministically for the network thread to
deliver before tearing down — it would still go through `receive_envelope`
the same way `attached_loop` does, just without the timing complexity of
two daemons swapping HOMEs in one process.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from mqtt_cluster import write_network_json

pytest.importorskip("paho.mqtt.client")


def test_remote_round_trip(tmp_path, mqtt_broker, monkeypatch):
    """Sender publishes via attached_loop's remote-routing wiring; a
    receiver running start_remotes on the same broker writes the envelope
    into a local agent's inbox."""
    topic = f"a8s/test-{os.getpid()}-{int(time.time() * 1000)}"
    cluster_a_home = tmp_path / "clusterA"
    cluster_a_home.mkdir()
    cluster_b_home = tmp_path / "clusterB"
    cluster_b_home.mkdir()
    write_network_json(cluster_a_home / ".a8s", mqtt_broker, topic, "a8s-test-clusterA")
    write_network_json(cluster_b_home / ".a8s", mqtt_broker, topic, "a8s-test-clusterB")

    import core
    core.PRINT_LOCK = None

    # The two clusters run in one process here, so HOME has to flip between
    # them. We can't have a live receive loop while HOME is set to the
    # sender's value (resolve_name in the receive callback reads HOME's
    # registry). The fix is to pre-register cluster B's persistent session
    # (clean_session=False + QoS 1 → broker holds messages for that
    # client_id), publish from A while B's subscriber is offline, then
    # bring B's subscriber back up and let the broker replay.

    monkeypatch.setenv("HOME", str(cluster_b_home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    target_root = cluster_b_home / "target"
    target_root.mkdir()
    from registry import save_registry
    from mailbox import ensure_mailboxes
    from core import Participant, inbox_dir
    save_registry({"TARGET": {"root": str(target_root)}})
    target_p = Participant("TARGET", target_root)
    ensure_mailboxes(target_p)

    # Step 1: warm-up — connect, register the persistent session, disconnect.
    from network import load_remotes, start_remotes, stop_remotes
    warmup = start_remotes(load_remotes(), lambda: [target_p])
    stop_remotes(warmup)

    # Step 2: cluster A publishes via attached_loop.
    monkeypatch.setenv("HOME", str(cluster_a_home))
    core.PRINT_LOCK = None
    sender_root = cluster_a_home / "sender"
    sender_root.mkdir()
    save_registry({"SENDER": {"root": str(sender_root)}})
    sender_p = Participant("SENDER", sender_root)
    ensure_mailboxes(sender_p)
    from mailbox import _write_outbox
    out_path = _write_outbox("SENDER", sender_root, "TARGET", "ping from A", [])
    msg_id = out_path.stem
    from daemon import attached_loop
    rc = attached_loop(["SENDER"], 0.2, single_pass=True)
    assert rc == 0

    # Step 3: cluster B reconnects with the same client_id; broker replays.
    monkeypatch.setenv("HOME", str(cluster_b_home))
    core.PRINT_LOCK = None
    rx_remotes = start_remotes(load_remotes(), lambda: [target_p])
    try:
        deadline = time.time() + 5.0
        files: list[Path] = []
        while time.time() < deadline:
            files = list(inbox_dir("TARGET").iterdir())
            if files:
                break
            time.sleep(0.1)
        assert files, "envelope did not arrive at TARGET via the remote"
        body = json.loads(files[0].read_text())
        assert body["from"] == "SENDER"
        assert body["content"] == "ping from A"
        assert body["to"] == "TARGET"
    finally:
        stop_remotes(rx_remotes)

    # Step 4: cluster A reconnects its persistent subscriber and consumes
    # the internal receipt emitted by cluster B after the inbox write.
    monkeypatch.setenv("HOME", str(cluster_a_home))
    core.PRINT_LOCK = None
    receipt_remotes = start_remotes(load_remotes(), lambda: [sender_p])
    try:
        from txlog import read_events

        deadline = time.time() + 5.0
        receipt_events = []
        while time.time() < deadline:
            receipt_events = [
                event for event in read_events(msg_id)
                if event["event"] == "DELIVERY_RECEIPT"
            ]
            if receipt_events:
                break
            time.sleep(0.1)
        assert len(receipt_events) == 1
        assert receipt_events[0]["from"] == "SENDER"
        assert receipt_events[0]["to"] == "TARGET"
        assert "inbox_write" in receipt_events[0]["detail"]
        assert "ping from A" not in receipt_events[0]["detail"]
    finally:
        stop_remotes(receipt_remotes)


def test_remote_round_trip_with_file_via_storage(tmp_path, mqtt_broker, monkeypatch):
    """Two-cluster end-to-end for issue #90: cluster A writes a `FILE:`
    outbox, attached_loop uploads to the configured storage service,
    publishes the envelope (with `files[i].storage` URLs in place of
    sender-local paths), broker holds for B's persistent session, B's
    subscriber receives, downloads via the same storage service into
    TARGET's `.files/`, and the inbox JSON has the local-path shape.

    Both clusters point at the SAME fake tempfile.org-shaped HTTP server
    (a stand-in for a real shared backend). The MQTT round-trip uses the
    persistent-session warmup pattern from the no-files test above so we
    don't need two HOMEs running subscribers concurrently."""
    from _fake_storage import start_fake_tempfile_server

    storage_server, storage_url = start_fake_tempfile_server()
    try:
        topic = f"a8s/test-storage-{os.getpid()}-{int(time.time() * 1000)}"
        cluster_a_home = tmp_path / "clusterA"; cluster_a_home.mkdir()
        cluster_b_home = tmp_path / "clusterB"; cluster_b_home.mkdir()
        write_network_json(
            cluster_a_home / ".a8s",
            mqtt_broker,
            topic,
            "a8s-test-storage-A",
            storage_url=storage_url,
        )
        write_network_json(
            cluster_b_home / ".a8s",
            mqtt_broker,
            topic,
            "a8s-test-storage-B",
            storage_url=storage_url,
        )

        import core
        core.PRINT_LOCK = None

        # Pre-register cluster B's persistent session (matches the no-files
        # test's pattern) and create TARGET's mailbox.
        monkeypatch.setenv("HOME", str(cluster_b_home))
        monkeypatch.delenv("USERPROFILE", raising=False)
        target_root = cluster_b_home / "target"
        target_root.mkdir()
        from core import Participant, files_dir, inbox_dir
        from mailbox import ensure_mailboxes
        from registry import save_registry
        save_registry({"TARGET": {"root": str(target_root)}})
        target_p = Participant("TARGET", target_root)
        ensure_mailboxes(target_p)

        from network import (
            load_remotes, load_services, start_remotes, stop_remotes,
        )
        b_services = load_services()
        warmup = start_remotes(load_remotes(), lambda: [target_p], services=b_services)
        stop_remotes(warmup)

        # Cluster A: write a FILE: outbox, run attached_loop to publish.
        monkeypatch.setenv("HOME", str(cluster_a_home))
        core.PRINT_LOCK = None
        sender_root = cluster_a_home / "sender"
        sender_root.mkdir()
        save_registry({"SENDER": {"root": str(sender_root)}})
        sender_p = Participant("SENDER", sender_root)
        ensure_mailboxes(sender_p)
        # The payload lives inside the sender's root (sandbox check enforces this).
        payload = sender_root / "report.txt"
        payload.write_text("hello from cluster A\n")
        from mailbox import _write_outbox

        out_path = _write_outbox("SENDER", sender_root, "TARGET", "see attached", [], attachment_sources=[payload])
        msg_id = out_path.stem
        from daemon import attached_loop
        rc = attached_loop(["SENDER"], 0.2, single_pass=True)
        assert rc == 0
        # Sender uploaded to the fake server.
        assert len(storage_server.files) == 1, "sender did not upload to storage"

        # Cluster B reconnects with the same client_id; broker replays.
        monkeypatch.setenv("HOME", str(cluster_b_home))
        core.PRINT_LOCK = None
        b_services = load_services()
        rx_remotes = start_remotes(load_remotes(), lambda: [target_p], services=b_services)
        try:
            deadline = time.time() + 5.0
            files: list[Path] = []
            while time.time() < deadline:
                files = list(inbox_dir("TARGET").iterdir())
                if files:
                    break
                time.sleep(0.1)
            assert files, "envelope did not arrive at TARGET via the remote"
            body = json.loads(files[0].read_text())
            assert body["from"] == "SENDER"
            assert body["content"] == "see attached"
            assert body["to"] == "TARGET"
            # File materialized into TARGET's .files/, path rewritten to local.
            assert len(body["files"]) == 1
            assert body["files"][0]["filename"] == "report.txt"
            local_files = files_dir(target_root) / msg_id
            assert (local_files / "report.txt").read_text() == "hello from cluster A\n"
        finally:
            stop_remotes(rx_remotes)
    finally:
        storage_server.shutdown()
        storage_server.server_close()
