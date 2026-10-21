"""Fixtures here are invented content in the shape the live page produces."""
import pytest
from gw_support import ScriptedSeat

from gemini_web import browser_seat as browser
from gemini_web import gemini

FINISHED = """\
- generic [ref=e1]:
  - button "Open mode picker, currently Flash" [ref=e2]
  - heading "You said what is the status" [level=5] [ref=e10]
  - paragraph: what is the status
  - button "Copy prompt" [ref=e11]
  - button "Edit" [ref=e12]
  - heading "Gemini said" [level=6] [ref=e20]
  - paragraph: The batch is green.
  - list [ref=e21]:
    - listitem: lint is clean
    - listitem: the suite passes
  - button "Good response" [ref=e22]
  - button "Bad response" [ref=e23]
  - button "Redo" [ref=e24]
  - button "Copy" [ref=e25]
  - button "Show more options" [ref=e26]
  - textbox "Enter a prompt for Gemini" [ref=e30]
  - paragraph: Gemini is AI and can make mistakes.
"""

STREAMING = """\
- generic [ref=e1]:
  - button "Open mode picker, currently Flash" [ref=e2]
  - heading "You said what is the status" [level=5] [ref=e10]
  - paragraph: what is the status
  - heading "Gemini said" [level=6] [ref=e20]
  - paragraph: The batch is
  - textbox "Enter a prompt for Gemini" [ref=e30]
  - paragraph: Gemini is AI and can make mistakes.
"""

EMPTY_TURN = """\
- generic [ref=e1]:
  - heading "You said what is the status" [level=5] [ref=e10]
  - paragraph: what is the status
  - heading "Gemini said" [level=6] [ref=e20]
  - textbox "Enter a prompt for Gemini" [ref=e30]
  - paragraph: Gemini is AI and can make mistakes.
"""

NO_CONVERSATION = """\
- generic [ref=e1]:
  - button "Open mode picker, currently Pro" [ref=e2]
  - heading "Meet Gemini" [level=1] [ref=e3]
  - textbox "Enter a prompt for Gemini" [ref=e30]
"""


def transcript(tmp_path, snapshot, history="", index=0):
    """One poll's answer: a snapshot on disk, and the page's rendered text."""
    path = tmp_path / f"snap-{index}.txt"
    path.write_text(snapshot)
    return browser.Transcript(
        True,
        [
            {"command": "snap", "ok": True, "output": str(path)},
            {"command": "text main", "ok": True, "output": history},
        ],
        [str(path)],
    )


def test_a_snapshot_line_parses_into_role_name_and_value():
    assert gemini.node('  - heading "Gemini said" [level=6] [ref=e20]') == (
        "heading",
        "Gemini said",
        "",
    )
    assert gemini.node("  - paragraph: The batch is green.") == (
        "paragraph",
        "",
        "The batch is green.",
    )
    assert gemini.node("  - list [ref=e21]:") == ("list", "", "")
    assert gemini.node("not a node") == ("", "", "")


def test_a_quoted_yaml_value_is_read_as_the_text_it_quotes():
    """Live, 2026-09-24: a cited answer split into text nodes, one of them quoted."""
    assert gemini.node('- text: ", HERON-3"') == ("text", "", ", HERON-3")
    assert gemini.node("- text: 'it''s here: now'") == ("text", "", "it's here: now")
    assert gemini.node('- paragraph: "unbalanced') == ("paragraph", "", '"unbalanced')


def test_the_prompt_box_is_the_textbox_that_says_what_it_is_for():
    snapshot = (
        '- textbox "Search" [ref=e1]\n'
        '- textbox "Enter a prompt for Gemini" [ref=e2]\n'
    )
    assert gemini.textbox_label(snapshot) == "Enter a prompt for Gemini"


def test_any_textbox_will_do_when_none_of_them_say_it():
    assert gemini.textbox_label('- textbox "Search" [ref=e1]') == "Search"
    assert gemini.textbox_label("- button \"Send\" [ref=e1]") is None


def test_the_model_is_readable_without_opening_the_picker():
    assert gemini.current_model(FINISHED) == "Flash"
    assert gemini.current_model(NO_CONVERSATION) == "Pro"
    assert gemini.current_model("- generic [ref=e1]") == ""


def test_the_reply_is_the_newest_model_turn_and_not_the_page_around_it():
    assert gemini.reply_block(FINISHED) == (
        "The batch is green.\nlint is clean\nthe suite passes"
    )


def test_the_disclaimer_under_the_prompt_box_is_not_part_of_a_reply():
    """A turn still streaming has no buttons after it, so only the prompt box
    keeps the page's own footer out of the reply."""
    assert gemini.reply_block(STREAMING) == "The batch is"
    assert gemini.reply_block(EMPTY_TURN) == ""


def test_an_older_turn_is_never_the_answer_to_this_one():
    older = FINISHED + (
        '  - heading "Gemini said" [level=6] [ref=e40]\n'
        "  - paragraph: The second answer.\n"
        '  - button "Good response" [ref=e41]\n'
        '  - textbox "Enter a prompt for Gemini" [ref=e50]\n'
    )
    assert gemini.reply_block(older) == "The second answer."


def test_a_finished_turn_is_the_one_carrying_the_button_cluster():
    assert gemini.turn_complete(FINISHED)
    assert not gemini.turn_complete(STREAMING)
    assert not gemini.turn_complete(NO_CONVERSATION)


def test_the_conversation_id_is_read_off_the_url():
    assert gemini.conversation_id("https://gemini.google.com/app/ceb94c08b1ef") == "ceb94c08b1ef"
    assert gemini.conversation_id("https://gemini.google.com/app/abc?hl=en") == "abc"
    assert gemini.conversation_id("https://gemini.google.com/app") == ""


def test_the_rendered_text_answers_when_the_headings_stop_matching():
    history = "what is the status\nThe batch is green.\nGemini is AI and can make mistakes."
    assert gemini.extract_reply("- generic [ref=e1]", history, "what is the status") == (
        "The batch is green."
    )


def test_the_text_fallback_gives_up_rather_than_guessing():
    assert gemini.extract_reply("", "a conversation about something else", "what now") == ""
    assert gemini.extract_reply("", "", "what now") == ""


def test_a_quota_refusal_is_recognised_but_an_answer_about_quotas_is_not():
    assert gemini.rate_limited("You've reached your limit on messages. Try again later.")
    assert gemini.rate_limited("Too many requests — please wait a while.")
    essay = (
        "Rate limiting is how a service protects itself from a burst of traffic. "
        "A token bucket refills at a fixed rate, and a request that finds the bucket "
        "empty is rejected with 429. " * 4
    )
    assert not gemini.rate_limited(essay)
    assert not gemini.rate_limited("")


def test_geminis_own_trouble_wording_is_reported_as_such():
    assert gemini.trouble("Sorry, I couldn't generate a response.") == "couldn't generate"
    assert gemini.trouble("The batch is green.") == ""


def test_a_message_survives_the_command_script_intact(seat):
    """Quotes, apostrophes, a leading hash and several lines all reach the box:
    a8s-browser splits a script by line and shlex-splits most verbs, so this is
    the whole reason `send_script` looks the way it does."""
    message = "don't \"quote\" me\n\n# not a comment\nlast line"
    gemini.send(seat, seat.label, message)
    assert seat.history[0] == ("user", message)


def test_an_empty_message_still_sends(seat):
    gemini.send(seat, seat.label, "")
    assert seat.history[0] == ("user", "")


def test_a_reply_comes_back_when_the_page_says_the_turn_is_done(seat, clock):
    turn = gemini.send_turn(seat, seat.label, "what is the status")
    assert turn.complete
    assert turn.reply == "ack: what is the status"
    assert turn.note == ""


def test_a_streaming_reply_is_waited_out(tmp_path, clock, monkeypatch):
    seat = ScriptedSeat(
        [
            transcript(tmp_path, EMPTY_TURN, index=0),
            transcript(tmp_path, STREAMING, index=1),
            transcript(tmp_path, FINISHED, index=2),
        ]
    )
    turn = gemini.await_reply(seat, "what is the status")
    assert turn.complete
    assert turn.reply.startswith("The batch is green.")
    assert turn.seconds == pytest.approx(2 * gemini.POLL_SECONDS)


def test_the_previous_reply_is_never_returned_as_this_one(tmp_path, clock, monkeypatch):
    """A message that never reached the prompt box must not be answered with
    what was already on the page."""
    monkeypatch.setattr(gemini, "TURN_TIMEOUT_SECONDS", 5.0)
    seat = ScriptedSeat([transcript(tmp_path, FINISHED, index=i) for i in range(4)])
    turn = gemini.await_reply(
        seat, "what is the status", baseline=gemini.reply_block(FINISHED)
    )
    assert not turn.complete
    assert turn.reply == ""
    assert "no reply appeared" in turn.note


def test_text_that_stops_changing_is_reported_when_the_page_never_says_done(
    tmp_path, clock
):
    """The backstop for the day the button labels change."""
    seat = ScriptedSeat([transcript(tmp_path, STREAMING, index=i) for i in range(6)])
    turn = gemini.await_reply(seat, "what is the status")
    assert turn.complete
    assert turn.reply == "The batch is"
    assert "stopped changing" in turn.note


def test_a_reply_still_growing_at_the_timeout_comes_back_marked_cut_short(
    tmp_path, clock, monkeypatch
):
    monkeypatch.setattr(gemini, "TURN_TIMEOUT_SECONDS", 5.0)
    growing = [
        transcript(tmp_path, STREAMING.replace("The batch is", "The" + "." * i), index=i)
        for i in range(4)
    ]
    turn = gemini.await_reply(ScriptedSeat(growing), "what is the status")
    assert not turn.complete
    assert turn.reply.startswith("The")
    assert "cut short" in turn.note


def test_a_seat_that_is_not_signed_in_says_so_and_never_tries_to_fix_it(seat):
    seat.signed_in = False
    with pytest.raises(gemini.GeminiError) as exc:
        gemini.open_app(seat)
    message = str(exc.value)
    assert "not signed in" in message
    assert seat.seat in message
    assert "never signs in" in message


def test_a_conversation_that_no_longer_opens_is_named_as_such(seat):
    seat.url = "https://gemini.google.com/app"
    with pytest.raises(gemini.GeminiError) as exc:
        gemini.open_conversation(seat, "https://gemini.google.com/app/gone")
    assert "gone" in str(exc.value)


def test_a_conversation_gets_its_url_from_the_browser_after_the_first_message(seat, clock):
    gemini.open_app(seat)
    gemini.send_turn(seat, seat.label, "first message")
    assert gemini.conversation_id(gemini.conversation_url(seat))


def test_a_chat_that_never_becomes_a_conversation_is_an_error(seat, clock):
    with pytest.raises(gemini.GeminiError) as exc:
        gemini.conversation_url(seat)
    assert "conversation URL" in str(exc.value)


def test_the_model_menu_is_left_alone_when_the_seat_is_already_on_that_model(seat):
    assert gemini.choose_model(seat, "Flash", FINISHED) == ""
    assert seat.scripts == []


def test_choosing_a_model_clicks_the_picker_then_the_model(seat):
    assert gemini.choose_model(seat, "Pro", FINISHED) == ""
    assert seat.model == "Pro"


def test_a_model_that_cannot_be_chosen_costs_a_note_and_not_the_turn(seat):
    seat.clickable.discard(gemini.MODEL_BUTTON_SELECTORS[0])
    note = gemini.choose_model(seat, "Pro", FINISHED)
    assert "not selected" in note
    assert gemini.MODEL_BUTTON_SELECTORS[0] in note
    assert "default" in note
    # the turn goes on, and no menu is left open behind the attempt
    assert "press Escape" in seat.scripts[-1]


def test_a_seat_that_cannot_be_driven_is_reported_as_a_gemini_error(tmp_path):
    seat = ScriptedSeat([browser.BrowserError("Chrome would not start")])
    with pytest.raises(gemini.GeminiError) as exc:
        gemini.read(seat)
    assert "Chrome would not start" in str(exc.value)
