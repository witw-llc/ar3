"""Tests for the attachment receive wait."""
from __future__ import annotations

import json
import time

from network import drain_attachment_retries
from registry import save_registry
from ulid import new as new_ulid

from test_mailbox import Participant, _StubStorage, inbox_dir


class TestReceiveWait:
    def test_retries_until_download_succeeds(self, fake_home, tmp_path):
        import json
        from core import settings_path

        settings_path().write_text(
            json.dumps(
                {
                    "storage_receive_wait_seconds": 3,
                    "storage_fetch_poll_seconds": 1,
                }
            )
            + "\n",
            encoding="utf-8",
        )

        from network import receive_envelope

        b_root = tmp_path / "B"
        b_root.mkdir()
        save_registry({"B": {"root": str(b_root)}})
        b = Participant("B", b_root)

        class DelayedStorage(_StubStorage):
            def __init__(self):
                super().__init__("slow")
                self._ready_at = time.monotonic() + 1.5

            def retrieve(self, url: str, dest):
                if time.monotonic() < self._ready_at:
                    return False
                return super().retrieve(url, dest)

        svc = DelayedStorage()
        svc.bytes_for["stub://slow/1"] = b"late-bytes"
        msg_id = new_ulid()
        envelope = json.dumps({
            "id": msg_id,
            "from": "X",
            "to": "B",
            "content": "wait for it",
            "files": [{"filename": "doc.txt", "storage": ["stub://slow/1"]}],
        }).encode()
        receive_envelope(envelope, [b], services=[svc])
        # The retry runs off the subscriber worker; the message is still held
        # out of the inbox until its bytes land.
        assert not (inbox_dir("B") / f"{msg_id}.json").exists()
        assert drain_attachment_retries(timeout_s=30) is True
        assert (b.files_bundle_dir(msg_id) / "doc.txt").read_bytes() == b"late-bytes"
        body = json.loads(next(inbox_dir("B").iterdir()).read_text())
        assert body["files"] == [{"filename": "doc.txt"}]

    def test_a_stalled_attachment_does_not_hold_up_other_mail(
        self, fake_home, tmp_path
    ):
        """The regression that motivated deferring: everything below runs on
        the transport's single subscriber worker, so an inline retry loop made
        one unreachable URL stall every message behind it."""
        import json as _json
        import queue
        import threading
        from core import settings_path

        settings_path().write_text(
            _json.dumps({
                "storage_receive_wait_seconds": 10,
                "storage_fetch_poll_seconds": 1,
            }) + "\n",
            encoding="utf-8",
        )

        from network import receive_envelope

        b_root = tmp_path / "B"
        b_root.mkdir()
        save_registry({"B": {"root": str(b_root)}})
        b = Participant("B", b_root)

        def envelope(content, files=None):
            body = {"id": new_ulid(), "from": "X", "to": "B", "content": content}
            if files:
                body["files"] = files
            return _json.dumps(body).encode()

        blocked = envelope(
            "has an unreachable attachment",
            [{"filename": "doc.txt", "storage": ["https://127.0.0.1:9/never"]}],
        )
        plain = envelope("plain text, no attachment")

        # Mirrors transports/mqtt.py `_worker_loop`: one thread, strictly serial.
        work: queue.Queue = queue.Queue()
        finished: dict = {}

        def worker():
            while True:
                item = work.get()
                if item is None:
                    return
                label, payload = item
                receive_envelope(payload, [b], services=[])
                finished[label] = time.monotonic()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        started = time.monotonic()
        work.put(("blocked", blocked))
        work.put(("plain", plain))
        while len(finished) < 2 and time.monotonic() - started < 30:
            time.sleep(0.05)
        work.put(None)
        thread.join(timeout=5)

        assert len(finished) == 2, "subscriber worker never drained"
        assert finished["plain"] - started < 2.0, (
            f"plain mail waited {finished['plain'] - started:.1f}s behind a "
            "stalled attachment"
        )
