"""Every exit is 0: held replies in print mode, and disk failures at the boundary."""
import os

import pytest

from gemini_web import handler
from gemini_web import outbox as outbox_module
from gemini_web.store import SessionStore

NOT_SENT = "Your message was not sent. Send it again."


def run(seat, sender, outbox, tmp_path, **kwargs):
    return handler.handle(
        "gemini", sender, "hi", browser_seat="profile", allow="alice, bob",
        runner=kwargs.pop("runner", None) or seat, send=outbox, deliver=outbox, **kwargs,
    )


def test_print_mode_leaves_held_replies_queued(seat, outbox, clock, state_home, monkeypatch, capsys, tmp_path):
    held = outbox_module.Outbox("gemini", SessionStore("gemini").root)
    held.keep("alice", "held answer", [])
    monkeypatch.setenv("AR3_TOOL_REPLY_DIR", str(tmp_path / "replies"))
    printed = []

    def display(to, body, files=()):
        printed.append(to)
        print(f"to {to}:\n{body}")
        return 0

    code = handler.handle(
        "gemini", "bob", "hi", browser_seat="profile", allow="alice, bob",
        runner=seat, send=display, deliver=display,
    )
    out = capsys.readouterr()
    assert code == 0
    assert printed == ["bob"]
    assert "held answer" not in out.out
    assert "1 held reply(ies) left queued (print mode)" in out.err
    assert [w[1] for w in held.waiting()] == ["alice"]

    # A later normal wake delivers it.
    monkeypatch.delenv("AR3_TOOL_REPLY_DIR")
    run(seat, "bob", outbox, tmp_path)
    assert ("alice", "held answer", []) in outbox.sent
    assert held.waiting() == []


def test_a_stale_file_at_the_work_path_is_replaced(seat, outbox, clock, state_home, capsys, tmp_path):
    root = SessionStore("gemini").root
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, ".gemini.work"), "w") as handle:
        handle.write("stale")
    assert run(seat, "alice", outbox, tmp_path) == 0
    assert outbox.sent and outbox.last.startswith("ack:")
    assert os.path.isdir(os.path.join(root, ".gemini.work"))
    assert "replaced a stale file" in capsys.readouterr().err


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_an_unwritable_work_dir_asks_for_the_message_again(seat, outbox, clock, state_home, tmp_path):
    root = SessionStore("gemini").root
    os.makedirs(root, exist_ok=True)
    os.chmod(root, 0o500)
    try:
        assert run(seat, "alice", outbox, tmp_path) == 0
    finally:
        os.chmod(root, 0o700)
    assert outbox.sent
    assert NOT_SENT in outbox.last
    assert seat.history == []


def test_a_keep_failure_still_delivers_the_answer(seat, clock, state_home, monkeypatch, tmp_path):
    sent = []

    def flaky(to, body, files=()):
        sent.append(body)
        return 1 if len(sent) == 1 else 0

    def broken_keep(self, recipient, body, files=()):
        raise OSError("disk full")

    monkeypatch.setattr(outbox_module.Outbox, "keep", broken_keep)
    code = handler.handle(
        "gemini", "alice", "hi", browser_seat="profile", allow="alice",
        runner=seat, send=flaky, deliver=flaky,
    )
    assert code == 0
    assert len(sent) == 2
    assert "ack: hi" in sent[1]
    assert "could not save state" in sent[1]


def test_a_flush_failure_keeps_the_item_and_the_turn_runs(seat, outbox, clock, state_home, monkeypatch, tmp_path):
    held = outbox_module.Outbox("gemini", SessionStore("gemini").root)
    held.keep("bob", "waiting", [])

    def broken(self):
        raise OSError("disk error")

    monkeypatch.setattr(outbox_module.Outbox, "_recover", broken)
    assert run(seat, "alice", outbox, tmp_path) == 0
    assert outbox.last.startswith("ack:")
    assert [w[1] for w in held.waiting()] == ["bob"]


def test_without_fcntl_the_sender_is_told_and_the_exit_is_zero(seat, outbox, state_home, monkeypatch):
    from gemini_web import store as store_module

    monkeypatch.setattr(store_module, "fcntl", None)
    assert run(seat, "alice", outbox, None) == 0
    assert outbox.sent == [("alice", handler.UNSUPPORTED_NOTE, [])]
    assert seat.history == []
