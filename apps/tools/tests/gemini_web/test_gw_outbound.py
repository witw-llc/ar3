"""Files going the other way: held replies, and naming the page's snapshot."""
import os

import pytest

from gemini_web import browser_seat as browser
from gemini_web import gemini
from gemini_web import outbox as outbox_module


class TestHeldReplies:
    def test_a_held_reply_keeps_a_copy_of_its_attachments(self, tmp_path):
        source = tmp_path / "chart.png"
        source.write_bytes(b"pixels")
        held = outbox_module.Outbox("gemini", str(tmp_path / "state"))

        path = held.keep("example-sender", "here it is", [str(source)])

        # Copied, not referenced: the artifact it came from is swept.
        source.unlink()
        waiting = held.waiting()
        assert len(waiting) == 1
        _, recipient, body, files = waiting[0]
        assert recipient == "example-sender"
        assert body == "here it is"
        assert [os.path.basename(f) for f in files] == ["chart.png"]
        assert open(files[0], "rb").read() == b"pixels"
        assert path.endswith(".json")

    def test_a_flush_hands_the_files_to_the_sender(self, tmp_path):
        source = tmp_path / "out.py"
        source.write_text("print(1)\n")
        held = outbox_module.Outbox("gemini", str(tmp_path / "state"))
        held.keep("example-sender", "your script", [str(source)])

        sent = []

        def send(to, body, files):
            # The bytes are the sender's to consume during the call: a
            # delivered reply's copies go with it, and `tell` copies them into
            # a8s synchronously.
            sent.append((to, body, [(os.path.basename(f), open(f).read()) for f in files]))
            return 0

        delivered, waiting = held.flush(send)
        assert (delivered, waiting) == (1, 0)
        assert sent == [("example-sender", "your script", [("out.py", "print(1)\n")])]

    def test_a_delivered_reply_takes_its_copies_with_it(self, tmp_path):
        source = tmp_path / "gone.txt"
        source.write_text("x")
        held = outbox_module.Outbox("gemini", str(tmp_path / "state"))
        path = held.keep("example-sender", "body", [str(source)])
        copies = outbox_module._files_dir(path)
        assert os.path.isdir(copies)

        held.flush(lambda to, body, files: 0)
        assert not os.path.exists(copies)
        assert held.waiting() == []

    def test_a_reply_put_back_still_has_its_files(self, tmp_path):
        source = tmp_path / "keep.txt"
        source.write_text("still here")
        held = outbox_module.Outbox("gemini", str(tmp_path / "state"))
        held.keep("example-sender", "body", [str(source)])

        held.flush(lambda to, body, files: 1)  # refused

        _, _, _, files = held.waiting()[0]
        assert open(files[0]).read() == "still here"

    def test_an_attachment_whose_bytes_vanished_is_dropped_not_raised(self, tmp_path):
        source = tmp_path / "vanishes.txt"
        source.write_text("x")
        held = outbox_module.Outbox("gemini", str(tmp_path / "state"))
        path = held.keep("example-sender", "body", [str(source)])
        os.unlink(os.path.join(outbox_module._files_dir(path), "vanishes.txt"))

        # One missing file must not cost the answer: `tell` would refuse the
        # whole reply over it.
        _, _, body, files = held.waiting()[0]
        assert files == []
        assert body == "body"

    def test_a_file_that_cannot_be_copied_is_named_in_the_body(self, tmp_path):
        held = outbox_module.Outbox("gemini", str(tmp_path / "state"))
        held.keep("example-sender", "the answer", [str(tmp_path / "never-existed.png")])
        _, _, body, files = held.waiting()[0]
        assert files == []
        assert "never-existed.png" in body
        assert "the answer" in body

    def test_a_reply_with_no_files_writes_no_directory(self, tmp_path):
        held = outbox_module.Outbox("gemini", str(tmp_path / "state"))
        path = held.keep("example-sender", "just text")
        assert not os.path.exists(outbox_module._files_dir(path))
        assert held.waiting()[0][3] == []

    def test_the_files_dir_is_found_through_a_claim(self, tmp_path):
        held = outbox_module.Outbox("gemini", str(tmp_path / "state"))
        path = held.keep("example-sender", "body")
        claimed = f"{path}{outbox_module.CLAIM}1234.5-9-abcd"
        # A claim renames the reply; its files must still be found.
        assert outbox_module._files_dir(claimed) == outbox_module._files_dir(path)


class TestNamingTheSnapshot:
    def _transcript(self, steps, files):
        return browser.Transcript(True, steps, files)

    def test_the_snap_step_names_the_snapshot_it_wrote(self, tmp_path):
        page = tmp_path / "page.txt"
        page.write_text("- textbox \"Enter a prompt for Gemini\"")
        download = tmp_path / "answer.txt"
        download.write_text("not the page")
        transcript = self._transcript(
            # The real order: look at the page, then download what it offered.
            # Artifacts are attached in script order, so the download is last
            # and neither "first .txt" nor "last artifact" finds the page.
            [
                {"command": "snap", "ok": True, "output": str(page)},
                {"command": "download button.save", "ok": True, "output": str(download)},
            ],
            [str(page), str(download)],
        )
        seat = type("S", (), {"seat": "profile"})()
        assert "Enter a prompt" in gemini._snapshot_of(transcript, seat)

    def test_the_newest_snap_wins_when_a_script_looks_twice(self, tmp_path):
        first, second = tmp_path / "one.txt", tmp_path / "two.txt"
        first.write_text("before")
        second.write_text("after")
        transcript = self._transcript(
            [
                {"command": "snap", "ok": True, "output": str(first)},
                {"command": "snap", "ok": True, "output": str(second)},
            ],
            [str(first), str(second)],
        )
        seat = type("S", (), {"seat": "profile"})()
        assert gemini._snapshot_of(transcript, seat) == "after"

    def test_a_failed_run_still_falls_back_to_its_attached_snapshot(self, tmp_path):
        page = tmp_path / "died-on.txt"
        page.write_text("- button \"Sign in\"")
        # a8s-browser attaches a snapshot to a failure without a `snap` step.
        transcript = browser.Transcript(False, [{"command": "click x", "ok": False}], [str(page)])
        seat = type("S", (), {"seat": "profile"})()
        assert "Sign in" in gemini._snapshot_of(transcript, seat)

    def test_a_binary_artifact_is_a_reply_and_not_a_crash(self, tmp_path):
        blob = tmp_path / "image.png"
        blob.write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe undecodable")
        transcript = browser.Transcript(False, [{"command": "click x", "ok": False}], [str(blob)])
        seat = type("S", (), {"seat": "profile"})()
        # A UnicodeDecodeError here would escape as a crash rather than reach
        # the sender as a reply.
        gemini._snapshot_of(transcript, seat)

    def test_no_artifact_at_all_says_the_page_cannot_be_read(self):
        transcript = browser.Transcript(True, [], [])
        seat = type("S", (), {"seat": "profile"})()
        with pytest.raises(gemini.GeminiError, match="attached no snapshot"):
            gemini._snapshot_of(transcript, seat)
