"""One test per finding from the 2026-09-23 review of pull request #1.

Each of these fails against the code as it was reviewed. They are kept together
and named for what goes wrong rather than for the module they touch, because
what makes them worth keeping is the failure they describe.
"""
import json
import os
import stat
import time

import pytest
from gw_support import FakeGeminiSeat

from gemini_web import browser_seat as browser
from gemini_web import gemini
from gemini_web import handler
from gemini_web import outbox as outbox_module
from gemini_web import store
from gemini_web.store import FileLock


def snapshot(*lines):
    return "\n".join(lines) + "\n"


def finished(*body):
    """A snapshot of one finished model turn carrying `body`."""
    return snapshot(
        '- heading "You said ask" [level=5] [ref=e1]',
        "- paragraph: ask",
        f'- heading "{gemini.MODEL_TURN_HEADING}" [level=6] [ref=e2]',
        *body,
        *[f'- button "{label}" [ref=e9]' for label in gemini.COMPLETION_BUTTONS],
        f'- textbox "{gemini.PROMPT_FALLBACK_LABEL}" [ref=e90]',
    )


# --- R1: a heading inside an answer is not the end of the answer -------------


def test_a_structured_answer_survives_its_own_headings():
    page = finished(
        "- paragraph: Here is what I found.",
        '- heading "Critical findings" [level=2] [ref=e3]',
        "- paragraph: The lock is missing.",
        '- heading "Minor findings" [level=2] [ref=e4]',
        "- listitem: A typo in the docstring.",
    )
    reply = gemini.reply_block(page)
    assert "Here is what I found." in reply
    assert "Critical findings" in reply
    assert "The lock is missing." in reply
    assert "A typo in the docstring." in reply


def test_the_next_turn_still_ends_the_reply():
    page = snapshot(
        f'- heading "{gemini.MODEL_TURN_HEADING}" [level=6] [ref=e2]',
        "- paragraph: first answer",
        '- heading "You said second question" [level=5] [ref=e3]',
        "- paragraph: second question",
        f'- heading "{gemini.MODEL_TURN_HEADING}" [level=6] [ref=e4]',
        "- paragraph: second answer",
        f'- textbox "{gemini.PROMPT_FALLBACK_LABEL}" [ref=e90]',
    )
    assert gemini.reply_block(page) == "second answer"


# --- R2: a send that may or may not have gone in is not guessed at -----------


def transcript(*steps):
    return browser.Transcript(False, list(steps), [])


def test_a_failure_before_the_keystroke_is_a_definite_non_send(seat, tmp_path):
    class Seat:
        seat = "profile"

        def run(self, script):
            return transcript(
                {"command": "fill 'box' ''", "ok": False, "error": "nothing visible"},
            )

    with pytest.raises(gemini.GeminiError) as caught:
        gemini.send(Seat(), "box", "hello")
    assert not isinstance(caught.value, gemini.SendUncertain)


def test_a_failure_on_the_keystroke_that_sends_is_uncertain():
    class Seat:
        seat = "profile"

        def run(self, script):
            return transcript(
                {"command": "fill 'box' ''", "ok": True, "output": "box"},
                {"command": "press Enter", "ok": False, "error": "the tab went away"},
            )

    with pytest.raises(gemini.SendUncertain):
        gemini.send(Seat(), "box", "hello")


def test_a_seat_that_never_answered_is_uncertain():
    class Seat:
        seat = "profile"

        def run(self, script):
            raise browser.BrowserError("a8s-browser did not finish within 300s")

    with pytest.raises(gemini.SendUncertain):
        gemini.send(Seat(), "box", "hello")


class LosesTheAnswer(FakeGeminiSeat):
    """A seat that does the work and then loses its reply on the way back.

    This is the shape of the defect: the message really is submitted, and the
    caller is told nothing except that something went wrong.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.swallow_next_send = True

    def run(self, script):
        if self.swallow_next_send and "press Enter" in script.splitlines():
            self.swallow_next_send = False
            super().run(script)
            raise browser.BrowserError("the seat stopped answering")
        return super().run(script)


def test_an_uncertain_send_that_did_reach_gemini_is_not_sent_twice(
    tmp_path, outbox, clock, state_home
):
    seat = LosesTheAnswer(tmp_path)
    # The conversation already exists, so the message under test is the first
    # thing typed into it and the count is unambiguous.
    seat.history = [("user", "earlier"), ("model", "earlier answer")]
    seat.url = f"{gemini.APP_URL}/c0001ab"
    seat.ids.add(gemini.conversation_id(seat.url))
    store.SessionStore("gemini").remember("example-sender", seat.url)

    code = handler.handle(
        "gemini", "example-sender", "only once please",
        browser_seat="profile", allow="example-sender", runner=seat, send=outbox,
    )
    assert code == 0
    asked = [prompt for role, prompt in seat.history if role == "user"]
    assert asked.count("only once please") == 1
    assert "is in the conversation" in outbox.last


def test_an_uncertain_send_on_an_unreadable_page_is_never_repeated(
    tmp_path, outbox, clock, state_home
):
    class Blind(LosesTheAnswer):
        def run(self, script):
            if self.swallow_next_send and "press Enter" in script.splitlines():
                self.swallow_next_send = False
                FakeGeminiSeat.run(self, script)
                raise browser.BrowserError("the seat stopped answering")
            if "snap" in script and not self.swallow_next_send:
                raise browser.BrowserError("the seat is gone")
            return FakeGeminiSeat.run(self, script)

    seat = Blind(tmp_path)
    seat.history = [("user", "earlier"), ("model", "earlier answer")]
    seat.url = f"{gemini.APP_URL}/c0001ab"
    seat.ids.add(gemini.conversation_id(seat.url))
    store.SessionStore("gemini").remember("example-sender", seat.url)

    code = handler.handle(
        "gemini", "example-sender", "only once please",
        browser_seat="profile", allow="example-sender", runner=seat, send=outbox,
    )
    # Exit 0: a8s must not requeue a message that may already be in the chat.
    assert code == 0
    assert "NOT sent again" in outbox.last


# --- R3: a reply that cannot be delivered is kept, not lost ------------------


class Refuses:
    """A `tell` that fails, then succeeds."""

    def __init__(self, failures=1):
        self.failures = failures
        self.sent = []

    def __call__(self, recipient, body, files=()):
        if self.failures > 0:
            self.failures -= 1
            return 1
        self.sent.append((recipient, body))
        return 0


def test_an_undeliverable_reply_is_held_and_sent_by_the_next_run(
    tmp_path, clock, state_home
):
    send = Refuses(failures=1)
    seat = FakeGeminiSeat(tmp_path)
    code = handler.handle(
        "gemini", "example-sender", "what is the capital of France",
        browser_seat="profile", allow="example-sender", runner=seat,
        send=send, deliver=send,
    )
    # The turn was spent, so the message is acked rather than asked again.
    assert code == 0
    assert send.sent == []
    held = os.path.join(str(state_home), "ar3", "gemini-web", "gemini", "gemini.pending")
    assert len(os.listdir(held)) == 1

    # The next run delivers it before it drives the browser for anything else.
    handler.handle(
        "gemini", "example-sender", "and of Spain",
        browser_seat="profile", allow="example-sender", runner=seat,
        send=send, deliver=send,
    )
    first = send.sent[0][1]
    assert "capital of France" in first or "ack" in first
    assert os.listdir(held) == []


# --- R4: two runs cannot erase each other's conversations --------------------


def test_two_overlapping_runs_both_keep_their_conversation(state_home):
    first = store.SessionStore("gemini")
    second = store.SessionStore("gemini")
    # Both read the empty store before either writes — the interleaving that
    # loses a conversation when the write is not a locked read-modify-write.
    assert first.all() == {}
    assert second.all() == {}

    first.remember("example-sender", "https://gemini.google.com/app/aaa")
    second.remember("other-sender", "https://gemini.google.com/app/bbb")

    reader = store.SessionStore("gemini")
    assert sorted(reader.all()) == ["example-sender", "other-sender"]


def test_a_turn_holds_the_seat_against_another_run(state_home, tmp_path, outbox, monkeypatch):
    monkeypatch.setattr(handler, "TURN_LOCK_SECONDS", 0.0)
    root = os.path.join(str(state_home), "ar3", "gemini-web", "gemini")
    with store.turn_lock("gemini", root):
        code = handler.handle(
            "gemini", "example-sender", "hello",
            browser_seat="profile", allow="example-sender",
            runner=FakeGeminiSeat(tmp_path), send=outbox,
        )
    # Busy: the sender is told to send again; nothing interleaves in the page.
    assert code == 0
    assert "busy" in outbox.last
    assert "Send it again" in outbox.last


def test_the_turn_lock_is_released_when_the_turn_ends(state_home, tmp_path, outbox, clock):
    root = os.path.join(str(state_home), "ar3", "gemini-web", "gemini")
    handler.handle(
        "gemini", "example-sender", "hello",
        browser_seat="profile", allow="example-sender",
        runner=FakeGeminiSeat(tmp_path), send=outbox,
    )
    with store.turn_lock("gemini", root):
        pass


# --- R5: a store that cannot be written is reported, not raised --------------


def test_a_store_that_cannot_be_written_answers_the_sender(
    tmp_path, outbox, clock, state_home
):
    root = state_home / "ar3" / "gemini-web" / "gemini"
    root.mkdir(parents=True)
    os.chmod(root, stat.S_IRUSR | stat.S_IXUSR)
    try:
        code = handler.handle(
            "gemini", "example-sender", "hello",
            browser_seat="profile", allow="example-sender",
            runner=FakeGeminiSeat(tmp_path), send=outbox,
        )
    finally:
        os.chmod(root, stat.S_IRWXU)
    assert code == 0
    assert "Send it again" in outbox.last
    assert "could not be" in outbox.last
    assert "Permission denied" in outbox.last


def test_an_entry_whose_url_is_not_text_is_ignored(state_home):
    root = state_home / "ar3" / "gemini-web" / "gemini"
    root.mkdir(parents=True)
    with open(root / "gemini.json", "w") as handle:
        json.dump({"sessions": {"example-sender": {"url": 17}}}, handle)
    assert store.SessionStore("gemini").get("example-sender") is None


# --- R6: the message reaches Gemini exactly as it was written ----------------


def test_indentation_survives_the_trip(tmp_path, clock, state_home):
    seat = FakeGeminiSeat(tmp_path)
    message = 'def f():\n    if True:\n        print("hi")'
    gemini.send(seat, seat.label, message)
    assert seat.history[0] == ("user", message)


def test_a_message_that_mentions_a_heredoc_is_still_sendable(tmp_path, clock, state_home):
    seat = FakeGeminiSeat(tmp_path)
    message = "Explain this shell line: cat <<EOF\nand this one: run <<END"
    gemini.send(seat, seat.label, message)
    assert seat.history[0] == ("user", message)


def test_a_message_line_that_reads_like_the_marker_is_still_sendable(
    tmp_path, clock, state_home
):
    seat = FakeGeminiSeat(tmp_path)
    message = f"before\n{gemini.BLOCK_MARKER}\nafter"
    gemini.send(seat, seat.label, message)
    assert seat.history[0] == ("user", message)


def test_the_message_never_travels_as_a_line_argument():
    script = gemini.send_script("Enter a prompt for Gemini", "    indented")
    assert "type <<" in script
    assert "type     indented" not in script


def test_a_prompt_box_name_that_would_open_a_block_is_refused():
    with pytest.raises(gemini.GeminiError) as caught:
        gemini.send_script("odd <<name", "hello")
    assert "block opener" in str(caught.value)


def test_the_box_is_emptied_before_the_message_is_typed(tmp_path, clock, state_home):
    seat = FakeGeminiSeat(tmp_path)
    seat.draft = "half a sentence somebody left behind"
    gemini.send(seat, seat.label, "the real message")
    assert seat.history[0] == ("user", "the real message")


# --- R7: an answer that repeats itself is still a new answer -----------------


def test_the_same_answer_twice_is_two_answers(tmp_path, clock, state_home, outbox):
    seat = FakeGeminiSeat(tmp_path, reply=lambda prompt: "OK")
    for message in ("first", "second"):
        code = handler.handle(
            "gemini", "example-sender", message,
            browser_seat="profile", allow="example-sender", runner=seat, send=outbox,
        )
        assert code == 0
    assert outbox.last.startswith("OK")
    assert "no reply appeared" not in outbox.last


def test_turn_counts_read_both_sides():
    page = snapshot(
        '- heading "You said one" [level=5] [ref=e1]',
        f'- heading "{gemini.MODEL_TURN_HEADING}" [level=6] [ref=e2]',
        '- heading "You said two" [level=5] [ref=e3]',
        f'- heading "{gemini.MODEL_TURN_HEADING}" [level=6] [ref=e4]',
    )
    assert gemini.turn_counts(page) == (2, 2)


def test_a_page_with_no_turn_headings_has_no_counts():
    assert gemini.turn_counts("- paragraph: nothing here\n") is None


def test_identical_text_is_new_when_the_turn_count_moved():
    reading = gemini.Reading("OK", True, (2, 2))
    assert gemini.is_new(reading, baseline="OK", baseline_counts=(1, 1))
    assert not gemini.is_new(reading, baseline="OK", baseline_counts=(2, 2))


def test_text_is_the_fallback_when_the_headings_stop_matching():
    reading = gemini.Reading("new answer", True, None)
    assert gemini.is_new(reading, baseline="old answer")
    assert not gemini.is_new(gemini.Reading("old answer", True, None), baseline="old answer")


# --- the fake seat is only worth anything if it refuses what the real one does


def test_the_fake_seat_refuses_a_block_opener_with_an_inline_argument(tmp_path):
    seat = FakeGeminiSeat(tmp_path)
    with pytest.raises(Exception) as caught:
        seat.run("type Dear team <<TEXT\nbody\nTEXT")
    assert "inline argument" in str(caught.value)


def test_the_fake_seat_strips_a_line_argument_the_way_the_real_one_does(tmp_path):
    seat = FakeGeminiSeat(tmp_path)
    seat.run("type     indented")
    assert seat.draft == "indented"


def test_an_unknown_command_still_fails_the_step(tmp_path):
    seat = FakeGeminiSeat(tmp_path)
    run = seat.run("nonsense thing")
    assert not run.ok
    assert "unknown command" in run.error


# --- found while verifying the repair: a slow first paint cost a live turn ----


class SlowToPaint(FakeGeminiSeat):
    """Gemini's composer appears some polls after the navigation resolves."""

    def __init__(self, *args, blank_snaps=2, **kwargs):
        super().__init__(*args, **kwargs)
        self.blank_snaps = blank_snaps

    def _snapshot(self):
        if self.blank_snaps > 0:
            self.blank_snaps -= 1
            return "- generic [ref=e1]\n"
        return super()._snapshot()


def test_a_slow_first_paint_does_not_throw_the_message_away(
    tmp_path, outbox, clock, state_home
):
    code = handler.handle(
        "gemini", "example-sender", "hello",
        browser_seat="profile", allow="example-sender",
        runner=SlowToPaint(tmp_path), send=outbox,
    )
    assert code == 0
    assert "ack: hello" in outbox.last


def test_a_composer_that_never_appears_is_still_reported(tmp_path, outbox, clock, state_home):
    code = handler.handle(
        "gemini", "example-sender", "hello",
        browser_seat="profile", allow="example-sender",
        runner=SlowToPaint(tmp_path, blank_snaps=10_000), send=outbox,
    )
    assert code == 0
    assert "Send it again" in outbox.last
    assert "never appeared" in outbox.last


def test_a_page_that_does_not_survive_between_commands_says_so(
    tmp_path, outbox, clock, state_home
):
    class Cycled(FakeGeminiSeat):
        """A seat whose Chrome is restarted between commands."""

        def _snapshot(self):
            return "- Page URL: about:blank\n"

    code = handler.handle(
        "gemini", "example-sender", "hello",
        browser_seat="profile", allow="example-sender",
        runner=Cycled(tmp_path), send=outbox,
    )
    assert code == 0
    assert "Send it again" in outbox.last
    assert "did not survive to the next command" in outbox.last
    # A covered window keeps its page since a8s-browser 0.3.1, so the seat must
    # not send anyone off to rearrange windows.
    assert "front" not in outbox.last
    assert "closed by hand" in outbox.last


# --- second review: four more, all in what happens around a delivery ---------


class StaleThenCurrent(FakeGeminiSeat):
    """A seat that accepts the message, loses its answer, and renders late.

    This is the page nobody can tell apart from one that never got the
    message — which is the whole point of R2.
    """

    def __init__(self, *args, stale_snaps=1, **kwargs):
        super().__init__(*args, **kwargs)
        self.swallow_next_send = True
        self.stale_snaps = stale_snaps
        self._frozen = None

    def run(self, script):
        if self.swallow_next_send and "press Enter" in script.splitlines():
            self.swallow_next_send = False
            self._frozen = FakeGeminiSeat._snapshot(self)
            super().run(script)
            raise browser.BrowserError("the seat stopped answering")
        return super().run(script)

    def _snapshot(self):
        if self._frozen is not None and self.stale_snaps > 0:
            self.stale_snaps -= 1
            return self._frozen
        return super()._snapshot()


def resumed(seat, sender="example-sender"):
    """Give a seat a conversation already in progress, and remember it."""
    seat.history = [("user", "earlier"), ("model", "earlier answer")]
    seat.url = f"{gemini.APP_URL}/c0001ab"
    seat.ids.add(gemini.conversation_id(seat.url))
    store.SessionStore("gemini").remember(sender, seat.url)
    return seat


def test_a_stale_snapshot_never_counts_as_proof_the_message_was_not_sent(
    tmp_path, outbox, clock, state_home
):
    seat = resumed(StaleThenCurrent(tmp_path))
    code = handler.handle(
        "gemini", "example-sender", "only once please",
        browser_seat="profile", allow="example-sender", runner=seat, send=outbox,
    )
    # Whatever this turn decides, a8s must not be told to send it again.
    assert code == 0
    asked = [prompt for role, prompt in seat.history if role == "user"]
    assert asked.count("only once please") == 1


def test_a_page_that_never_shows_the_turn_is_uncertain_not_unsent(
    tmp_path, outbox, clock, state_home
):
    seat = resumed(StaleThenCurrent(tmp_path, stale_snaps=10_000))
    code = handler.handle(
        "gemini", "example-sender", "only once please",
        browser_seat="profile", allow="example-sender", runner=seat, send=outbox,
    )
    assert code == 0
    assert "NOT sent again" in outbox.last
    assert "was not sent" not in outbox.last


def test_a_confirmed_turn_carries_on_to_the_answer(tmp_path, outbox, clock, state_home):
    seat = resumed(StaleThenCurrent(tmp_path, stale_snaps=0))
    code = handler.handle(
        "gemini", "example-sender", "only once please",
        browser_seat="profile", allow="example-sender", runner=seat, send=outbox,
    )
    assert code == 0
    assert "is in the conversation" in outbox.last


# R3a: a hand-run ask must not eat the network queue


def test_a_hand_run_ask_never_delivers_a_held_reply_to_the_terminal(
    tmp_path, clock, state_home
):
    root = os.path.join(str(state_home), "ar3", "gemini-web", "gemini")
    held = outbox_module.Outbox("gemini", root)
    held.keep("someone-else", "an answer that belongs to someone else")

    printed = []
    network = []

    def display(recipient, body, files=()):
        printed.append((recipient, body))

    def tell(recipient, body, files=()):
        network.append((recipient, body))
        return 0

    handler.handle(
        "gemini", "example-sender", "hello",
        browser_seat="profile", allow="example-sender",
        runner=FakeGeminiSeat(tmp_path), send=display, deliver=tell,
    )
    assert ("someone-else", "an answer that belongs to someone else") in network
    assert [to for to, _ in printed] == ["example-sender"]
    assert held.waiting() == []


def test_the_cli_leaves_the_network_queue_to_tell():
    import inspect

    from gemini_web import cli

    source = inspect.getsource(cli)
    # `ask` overrides `send` only. A `deliver` override would route held
    # replies to the terminal, which is the defect this guards.
    assert "send=None if" in source
    assert "deliver=" not in source.replace("# `deliver`", "")


# R3b: two flushers cannot deliver one held reply twice


def test_two_flushers_cannot_send_the_same_held_reply_twice(state_home):
    import threading

    root = os.path.join(str(state_home), "ar3", "gemini-web", "gemini")
    held = outbox_module.Outbox("gemini", root)
    held.keep("someone", "the only copy")

    at_the_gate = threading.Barrier(2)
    sent = []
    guard = threading.Lock()

    def send(recipient, body, files=()):
        at_the_gate.wait(timeout=5)
        with guard:
            sent.append((recipient, body))
        return 0

    workers = [threading.Thread(target=held.flush, args=(send,)) for _ in range(2)]
    for worker in workers:
        worker.start()
    at_the_gate.wait(timeout=5)
    for worker in workers:
        worker.join(timeout=5)

    assert len(sent) == 1
    assert held.waiting() == []


def test_a_failed_send_puts_the_held_reply_back(state_home):
    root = os.path.join(str(state_home), "ar3", "gemini-web", "gemini")
    held = outbox_module.Outbox("gemini", root)
    held.keep("someone", "still waiting")
    delivered, waiting = held.flush(lambda to, body, files: 1)
    assert (delivered, waiting) == (0, 1)
    assert held.waiting()[0][2] == "still waiting"


def test_a_claim_left_by_a_dead_run_comes_back(state_home, monkeypatch):
    root = os.path.join(str(state_home), "ar3", "gemini-web", "gemini")
    held = outbox_module.Outbox("gemini", root)
    path = held.keep("someone", "abandoned")
    stranded = held._claim(path)
    # A claim is invisible until it is old enough to be recovered, and only a
    # flush recovers it — listing the queue has no side effects.
    assert held.waiting() == []
    sent = []
    assert held.flush(lambda to, body, files: sent.append((to, body)) or 0) == (0, 0)

    monkeypatch.setattr(outbox_module, "RECLAIM_SECONDS", -1.0)
    delivered, remaining = held.flush(lambda to, body, files: sent.append((to, body)) or 0)
    assert (delivered, remaining) == (1, 0)
    assert sent == [("someone", "abandoned")]
    assert not os.path.exists(stranded)


# R3c: a tell that cannot even start is a delivery failure


def test_a_sender_that_cannot_start_still_holds_the_reply(tmp_path, clock, state_home):
    def cannot_start(recipient, body, files=()):
        raise FileNotFoundError(2, "No such file or directory", "tell")

    code = handler.handle(
        "gemini", "example-sender", "what is the capital of France",
        browser_seat="profile", allow="example-sender",
        runner=FakeGeminiSeat(tmp_path), send=cannot_start, deliver=cannot_start,
    )
    assert code == 0
    root = os.path.join(str(state_home), "ar3", "gemini-web", "gemini")
    assert len(outbox_module.Outbox("gemini", root).waiting()) == 1


# --- third review: a claim must carry its own age, not the reply's -----------


def test_an_old_reply_gets_a_claim_that_is_new(state_home):
    """A rename keeps the file's mtime. The claim's age is not the reply's."""
    root = os.path.join(str(state_home), "ar3", "gemini-web", "gemini")
    held = outbox_module.Outbox("gemini", root)
    path = held.keep("someone", "queued a long time ago")
    stale = time.time() - (outbox_module.RECLAIM_SECONDS + 1)
    os.utime(path, (stale, stale))

    claimed = held._claim(path)
    taken = held._claimed_at(os.path.basename(claimed))
    assert taken is not None
    assert time.time() - taken < 5
    # The file it came from is older than the reclaim window; the claim is not.
    assert time.time() - os.path.getmtime(claimed) > outbox_module.RECLAIM_SECONDS


def test_an_aged_reply_is_not_delivered_twice_by_a_second_flusher(state_home):
    """Carlos's case: a reply older than the reclaim window, claimed live.

    With the age read off the file, the live claim was already expired, a
    second flusher recovered it immediately, and the reply went out twice
    while the first send was still running.
    """
    import threading

    root = os.path.join(str(state_home), "ar3", "gemini-web", "gemini")
    held = outbox_module.Outbox("gemini", root)
    path = held.keep("someone", "waited over five minutes")
    stale = time.time() - (outbox_module.RECLAIM_SECONDS + 1)
    os.utime(path, (stale, stale))

    inside = threading.Event()
    release = threading.Event()
    sent = []
    guard = threading.Lock()

    def slow(recipient, body, files=()):
        with guard:
            sent.append((recipient, body))
        inside.set()
        release.wait(timeout=5)
        return 0

    def quick(recipient, body, files=()):
        with guard:
            sent.append((recipient, body))
        return 0

    first = threading.Thread(target=held.flush, args=(slow,))
    first.start()
    assert inside.wait(timeout=5)
    # A second run arrives while the first is mid-send.
    assert held.flush(quick) == (0, 0)
    release.set()
    first.join(timeout=5)

    assert len(sent) == 1
    assert held.waiting() == []


def test_a_second_flusher_leaves_the_queue_to_the_first(state_home):
    root = os.path.join(str(state_home), "ar3", "gemini-web", "gemini")
    held = outbox_module.Outbox("gemini", root)
    held.keep("someone", "only copy")
    with FileLock(held.lock_path):
        assert held.flush(lambda to, body, files: 0) == (0, 1)
    assert held.waiting()[0][2] == "only copy"


def test_a_claim_with_no_readable_stamp_is_recovered(state_home):
    root = os.path.join(str(state_home), "ar3", "gemini-web", "gemini")
    held = outbox_module.Outbox("gemini", root)
    path = held.keep("someone", "hand-edited claim")
    os.rename(path, f"{path}{outbox_module.CLAIM}not-a-time")
    sent = []
    assert held.flush(lambda to, body, files: sent.append((to, body)) or 0) == (1, 0)
    assert sent == [("someone", "hand-edited claim")]


def test_flushing_an_empty_queue_takes_no_lock(state_home):
    root = os.path.join(str(state_home), "ar3", "gemini-web", "gemini")
    held = outbox_module.Outbox("gemini", root)
    assert held.flush(lambda to, body, files: 0) == (0, 0)
    assert not os.path.exists(held.lock_path)
