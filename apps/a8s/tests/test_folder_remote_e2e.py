"""End-to-end folder routing: two machines, one shared directory, no broker.

Modelled on `test_remote_e2e.py`, minus the mosquitto. Both machines run in
one process, so HOME flips between them — the receive callback resolves
recipients out of HOME's registry, so a live subscriber cannot be running
under the wrong one. Unlike the broker version there is nothing to warm up:
the folder holds the envelope until somebody polls it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from conftest import set_home


def _machine(home: Path, shared: Path) -> None:
    """Write one machine's a8s state root, pointed at the shared folder."""
    a8s = home / ".a8s"
    a8s.mkdir(parents=True, exist_ok=True)
    (a8s / "network.json").write_text(
        json.dumps(
            {
                "remotes": {
                    "box": {
                        "transport": "folder",
                        "path": str(shared),
                        "poll_seconds": 1,
                    }
                },
                "services": {"box": {"service": "sync_folder", "url": str(shared)}},
            }
        )
    )
    (a8s / "settings.json").write_text(
        json.dumps({"storage_receive_wait_seconds": 0}) + "\n"
    )


def _wait_for_inbox(agent: str, timeout: float = 5.0) -> list[Path]:
    from core import inbox_dir

    deadline = time.time() + timeout
    while time.time() < deadline:
        files = sorted(inbox_dir(agent).glob("*.json"))
        if files:
            return files
        time.sleep(0.05)
    return []


def test_folder_round_trip(tmp_path, monkeypatch):
    """A publishes through `attached_loop`'s remote wiring into the shared
    folder; B's subscriber picks the envelope up and writes TARGET's inbox."""
    shared = tmp_path / "Dropbox" / "A8S"
    shared.mkdir(parents=True)
    home_a = tmp_path / "machineA"
    home_a.mkdir()
    home_b = tmp_path / "machineB"
    home_b.mkdir()
    _machine(home_a, shared)
    _machine(home_b, shared)

    import core

    core.PRINT_LOCK = None

    from core import Participant, inbox_dir
    from daemon import attached_loop
    from mailbox import _write_outbox, ensure_mailboxes
    from network import load_remotes, load_services, start_remotes, stop_remotes
    from registry import save_registry

    set_home(monkeypatch, home_b)
    target_root = home_b / "target"
    target_root.mkdir()
    save_registry({"TARGET": {"root": str(target_root)}})
    target_p = Participant("TARGET", target_root)
    ensure_mailboxes(target_p)

    set_home(monkeypatch, home_a)
    core.PRINT_LOCK = None
    sender_root = home_a / "sender"
    sender_root.mkdir()
    save_registry({"SENDER": {"root": str(sender_root)}})
    sender_p = Participant("SENDER", sender_root)
    ensure_mailboxes(sender_p)
    out_path = _write_outbox("SENDER", sender_root, "TARGET", "ping from A", [])
    msg_id = out_path.stem
    assert attached_loop(["SENDER"], 0.2, single_pass=True) == 0

    envelope = shared / f"{msg_id}.json"
    assert envelope.is_file(), "publish left no envelope in the shared folder"
    assert json.loads(envelope.read_text())["content"] == "ping from A"

    set_home(monkeypatch, home_b)
    core.PRINT_LOCK = None
    rx = start_remotes(load_remotes(), lambda: [target_p], services=load_services())
    try:
        files = _wait_for_inbox("TARGET")
        assert files, "envelope did not reach TARGET through the folder"
        body = json.loads(files[0].read_text())
        assert body["from"] == "SENDER"
        assert body["to"] == "TARGET"
        assert body["content"] == "ping from A"
        # Nobody deletes on receive — the folder is shared, and a second
        # machine still has to read it.
        assert envelope.is_file()
        # A second pass over the same folder must not deliver it again.
        rx[0]._poll_once()
        assert len(list(inbox_dir("TARGET").glob("*.json"))) == 1
    finally:
        stop_remotes(rx)

    # The receipt rides the same folder back.
    set_home(monkeypatch, home_a)
    core.PRINT_LOCK = None
    receipt_rx = start_remotes(load_remotes(), lambda: [sender_p])
    try:
        from txlog import read_events

        deadline = time.time() + 5.0
        receipts: list[dict] = []
        while time.time() < deadline:
            receipts = [
                e for e in read_events(msg_id) if e["event"] == "DELIVERY_RECEIPT"
            ]
            if receipts:
                break
            time.sleep(0.05)
        assert len(receipts) == 1
        assert receipts[0]["from"] == "SENDER"
        assert receipts[0]["to"] == "TARGET"
    finally:
        stop_remotes(receipt_rx)


def test_folder_round_trip_with_attachment(tmp_path, monkeypatch):
    """One folder carries both halves of a message: `<ULID>.json` beside the
    `<ULID>/` bundle the storage service writes for the same message."""
    shared = tmp_path / "Drive" / "A8S"
    shared.mkdir(parents=True)
    home_a = tmp_path / "machineA"
    home_a.mkdir()
    home_b = tmp_path / "machineB"
    home_b.mkdir()
    _machine(home_a, shared)
    _machine(home_b, shared)

    import core

    core.PRINT_LOCK = None

    from core import Participant, files_dir
    from daemon import attached_loop
    from mailbox import _write_outbox, ensure_mailboxes
    from network import load_remotes, load_services, start_remotes, stop_remotes
    from registry import save_registry

    set_home(monkeypatch, home_b)
    target_root = home_b / "target"
    target_root.mkdir()
    save_registry({"TARGET": {"root": str(target_root)}})
    target_p = Participant("TARGET", target_root)
    ensure_mailboxes(target_p)

    set_home(monkeypatch, home_a)
    core.PRINT_LOCK = None
    sender_root = home_a / "sender"
    sender_root.mkdir()
    save_registry({"SENDER": {"root": str(sender_root)}})
    sender_p = Participant("SENDER", sender_root)
    ensure_mailboxes(sender_p)
    payload = sender_root / "report.txt"
    payload.write_text("hello from machine A\n")
    out_path = _write_outbox(
        "SENDER", sender_root, "TARGET", "see attached", [],
        attachment_sources=[payload],
    )
    msg_id = out_path.stem
    assert attached_loop(["SENDER"], 0.2, single_pass=True) == 0

    assert (shared / f"{msg_id}.json").is_file()
    assert (shared / msg_id / "report.txt").read_text() == "hello from machine A\n"

    set_home(monkeypatch, home_b)
    core.PRINT_LOCK = None
    rx = start_remotes(load_remotes(), lambda: [target_p], services=load_services())
    try:
        files = _wait_for_inbox("TARGET")
        assert files, "envelope did not reach TARGET through the folder"
        body = json.loads(files[0].read_text())
        assert body["content"] == "see attached"
        assert len(body["files"]) == 1
        assert body["files"][0]["filename"] == "report.txt"
        landed = files_dir(target_root) / msg_id / "report.txt"
        assert landed.read_text() == "hello from machine A\n"
    finally:
        stop_remotes(rx)
