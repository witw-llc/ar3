import os

from gemini_web import browser_seat as browser
from gemini_web import gemini
from gemini_web import handler
from gemini_web.store import SessionStore


class RefusedSeat:
    """A seat that fails the test if the browser is touched at all."""

    seat = "profile"

    def run(self, script):
        raise AssertionError(f"the browser was driven for a refused sender:\n{script}")


def answer(node, sender, message, outbox, runner, **kwargs):
    kwargs.setdefault("browser_seat", "profile")
    kwargs.setdefault("allow", "example-sender")
    return handler.handle(node, sender, message, runner=runner, send=outbox, **kwargs)


def test_an_empty_allowlist_accepts_nobody(outbox, state_home):
    code = handler.handle(
        "gemini", "example-sender", "hello", browser_seat="profile", allow="",
        runner=RefusedSeat(), send=outbox,
    )
    assert code == 0
    assert outbox.sent == []


def test_the_allowlist_is_case_insensitive(seat, outbox, clock, state_home):
    code = answer("gemini", "OTHER", "hi", outbox, seat, allow="Example-Sender, other")
    assert code == 0
    assert seat.history


def test_a_stranger_gets_no_reply_and_the_browser_is_never_opened(outbox, state_home, capsys):
    code = handler.handle(
        "gemini",
        "stranger",
        "hello",
        browser_seat="profile",
        allow="example-sender",
        runner=RefusedSeat(),
        send=outbox,
    )
    assert code == 0  # a refusal is final
    assert outbox.sent == []
    assert "refused" in capsys.readouterr().err


def test_a_node_with_no_browser_seat_says_which_var_is_missing(outbox, state_home):
    code = handler.handle(
        "gemini", "example-sender", "hello", allow="example-sender",
        runner=RefusedSeat(), send=outbox,
    )
    assert code == 0  # every exit is 0: a non-zero exit would dead-letter the tell
    assert "BROWSER_SEAT" in outbox.last


def test_a_new_correspondent_gets_the_preamble_first_and_a_conversation_of_their_own(
    seat, outbox, clock, state_home
):
    assert answer("gemini", "example-sender", "what is the status", outbox, seat) == 0

    prompts = [text for role, text in seat.history if role == "user"]
    assert "example-sender" in prompts[0]
    assert "relay" in prompts[0]
    assert prompts[1] == "what is the status"
    assert outbox.sent[0][0] == "example-sender"
    assert "ack: what is the status" in outbox.last

    stored = SessionStore("gemini").get("example-sender")
    assert gemini.conversation_id(stored["url"])


def test_the_second_message_goes_into_the_same_conversation(seat, outbox, clock, state_home):
    answer("gemini", "example-sender", "first", outbox, seat)
    first_url = SessionStore("gemini").get("example-sender")["url"]
    answer("gemini", "example-sender", "second", outbox, seat)

    assert seat.conversations == 1
    assert SessionStore("gemini").get("example-sender")["url"] == first_url
    prompts = [text for role, text in seat.history if role == "user"]
    assert len(prompts) == 3  # the preamble, then both messages
    assert prompts[1:] == ["first", "second"]
    assert "ack: second" in outbox.last


def test_two_correspondents_get_two_conversations(seat, outbox, clock, state_home):
    both = "example-sender,other"
    answer("gemini", "example-sender", "hello", outbox, seat, allow=both)
    answer("gemini", "other", "hello", outbox, seat, allow=both)

    sessions = SessionStore("gemini").all()
    assert seat.conversations == 2
    assert sessions["example-sender"]["url"] != sessions["other"]["url"]


def test_a_conversation_that_does_not_open_is_replaced_and_said_out_loud(
    seat, outbox, clock, state_home
):
    SessionStore("gemini").remember("example-sender", "https://gemini.google.com/app/gone")
    assert answer("gemini", "example-sender", "hello", outbox, seat) == 0

    assert "not usable" in outbox.last
    # Landing somewhere else is not proof the conversation was deleted.
    assert "could not be opened" in outbox.last
    assert "deleted" not in outbox.last
    assert gemini.conversation_id(SessionStore("gemini").get("example-sender")["url"]) != "gone"
    assert "ack: hello" in outbox.last


def test_a_corrupt_store_costs_a_note_and_not_the_turn(seat, outbox, clock, state_home):
    broken = SessionStore("gemini")
    os.makedirs(broken.root, exist_ok=True)
    with open(broken.path, "w") as handle:
        handle.write("{ half a file")

    assert answer("gemini", "example-sender", "hello", outbox, seat) == 0
    assert "could not be read" in outbox.last
    assert "ack: hello" in outbox.last
    assert SessionStore("gemini").get("example-sender")


def test_a_rate_limited_seat_says_so_and_does_not_retry(seat, outbox, clock, state_home):
    seat.reply = lambda prompt: "You've reached your limit on messages. Try again later."
    assert answer("gemini", "example-sender", "hello", outbox, seat) == 0

    assert "rate-limiting" in outbox.last
    assert "Retry later" in outbox.last
    # the refusal answered the preamble, so the message was never typed in
    prompts = [text for role, text in seat.history if role == "user"]
    assert len(prompts) == 1
    assert "hello" not in prompts[0]


def test_a_reply_cut_short_by_the_timeout_says_so(seat, outbox, clock, state_home, monkeypatch):
    monkeypatch.setattr(gemini, "TURN_TIMEOUT_SECONDS", 5.0)
    seat.stream_polls = 99
    assert answer("gemini", "example-sender", "hello", outbox, seat) == 0
    assert "cut short" in outbox.last or "no reply appeared" in outbox.last


def test_a_browser_that_cannot_be_driven_asks_for_the_message_again(seat, outbox, clock, state_home):
    seat.signed_in = False
    assert answer("gemini", "example-sender", "hello", outbox, seat) == 0
    assert "not signed in" in outbox.last
    assert "Send it again" in outbox.last


class FlakySeat:
    """A seat that stops answering right after the message is typed in."""

    def __init__(self, inner):
        self.inner = inner
        self.seat = inner.seat
        self.sends = 0
        self.dead = False

    def run(self, script):
        if self.dead:
            raise browser.BrowserError("the browser went away")
        answer = self.inner.run(script)
        if self.inner.label in script.splitlines()[0]:
            self.sends += 1
            self.dead = self.sends == 2  # the preamble, then the message itself
        return answer


def test_a_failure_after_the_message_is_typed_does_not_ask_gemini_twice(
    seat, outbox, clock, state_home
):
    code = answer("gemini", "example-sender", "hello", outbox, FlakySeat(seat))
    assert code == 0  # a requeue here would type the same message in again
    assert "went away" in outbox.last
    assert [text for role, text in seat.history if role == "user"][-1] == "hello"


def test_a_long_reply_travels_whole_as_a_file_instead_of_being_cut(
    seat, outbox, clock, state_home
):
    seat.reply = lambda prompt: "x" * 9000 if "hello" in prompt else "ready"
    answer("gemini", "example-sender", "hello", outbox, seat)
    assert len(outbox.last) <= handler.MAX_BODY
    assert outbox.last_names == [handler.LONG_REPLY_NAME]
    # The whole answer, not the excerpt the body carries.
    assert open(outbox.last_files[0]).read().strip() == "x" * 9000
    assert "attached as gemini-reply.md" in outbox.last


def test_a_reply_inside_the_cap_carries_no_attachment(seat, outbox, clock, state_home):
    answer("gemini", "example-sender", "hello", outbox, seat)
    assert outbox.last_files == []


def test_geminis_own_trouble_wording_is_passed_on_as_a_note(seat, outbox, clock, state_home):
    seat.reply = (
        lambda prompt: "Sorry, I couldn't generate a response." if "hello" in prompt else "ready"
    )
    answer("gemini", "example-sender", "hello", outbox, seat)
    assert "couldn't generate" in outbox.last
    assert "trouble wording" in outbox.last


def test_help_answers_the_usage_and_asks_gemini_nothing(outbox, state_home):
    usage = handler.rt.usage_from_readme(handler.README)
    assert usage

    for spelling in ["help", "/help", "--help", "/?", "Help"]:
        code = handler.handle(
            "gemini", "example-sender", spelling, browser_seat="profile",
            allow="example-sender", runner=RefusedSeat(), send=outbox,
        )
        assert code == 0
    assert len(outbox.sent) == 5
    assert outbox.last.startswith("gemini: tool-gemini-web — usage")
    assert usage in outbox.last
    assert outbox.last.endswith("README: apps/tools/gemini_web/README.md")


def test_help_works_on_a_node_with_no_browser_seat(outbox, state_home):
    handler.handle("gemini", "example-sender", "help", allow="example-sender",
                   runner=RefusedSeat(), send=outbox)
    assert "usage" in outbox.last


def test_help_me_open_a_page_is_a_prompt_not_help(seat, outbox, clock, state_home):
    answer("gemini", "example-sender", "help me open a page", outbox, seat)
    assert seat.history


def test_a_stranger_sending_help_gets_no_reply(outbox, state_home):
    code = handler.handle(
        "gemini", "stranger", "help", browser_seat="profile",
        allow="example-sender", runner=RefusedSeat(), send=outbox,
    )
    assert code == 0
    assert outbox.sent == []


def test_help_without_a_usage_section_says_so(outbox, state_home, monkeypatch, tmp_path):
    bare = tmp_path / "README.md"
    bare.write_text("# tool-gemini-web\n\n## State\n\nnone\n")
    monkeypatch.setattr(handler, "README", bare)
    handler.handle("gemini", "example-sender", "help", allow="example-sender",
                   runner=RefusedSeat(), send=outbox)
    assert 'no "## Usage" section in apps/tools/gemini_web/README.md' in outbox.last
