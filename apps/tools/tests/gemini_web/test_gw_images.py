"""Files out: a generated image comes back as an attachment, not as furniture.

The fixtures are the live page's shape, read on 2026-09-24 from a turn that
generated one image, with the prompt replaced by invented text.
"""
import os

from gw_support import FakeGeminiSeat, ScriptedSeat
from test_gw_gemini import transcript
from test_gw_handler import answer

from gemini_web import gemini
from gemini_web import handler

PROMPT = "Create an image of a tile map with a few biomes"

# Two exchanges of the same request. Only the newest carries `Redo`, and an image
# turn's cluster has no `Copy` — a text turn's does.
IMAGE_TURN = f"""\
          - generic [ref=e386]:
            - generic [ref=e390]:
              - generic [ref=e394]:
                - heading "You said {PROMPT}" [level=5] [ref=e395]
                - paragraph [ref=e396]: {PROMPT}
              - generic [ref=e397]:
                - button "Copy prompt" [ref=e399] [cursor=pointer]:
                  - img [ref=e401]: copy
                - button "Edit" [ref=e404] [cursor=pointer]:
                  - img [ref=e406]: edit
            - generic [ref=e411]:
              - generic [ref=e414]:
                - heading "Gemini said" [level=6] [ref=e415]
                - generic [ref=e427]:
                  - button [ref=e428] [cursor=pointer]:
                    - img [ref=e429]
                  - generic [ref=e430]:
                    - button "Share image" [ref=e433] [cursor=pointer]:
                      - img [ref=e435]: share_1
                    - button "Copy image" [ref=e439] [cursor=pointer]:
                      - img [ref=e441]: content_copy
                    - button "Download full size image" [ref=e445] [cursor=pointer]:
                      - img [ref=e447]: download
              - generic [ref=e452]:
                - button "Good response" [ref=e455] [cursor=pointer]:
                  - img [ref=e457]: thumb_up
                - button "Bad response" [ref=e461] [cursor=pointer]:
                  - img [ref=e463]: thumb_down
                - button "Share image" [ref=e467] [cursor=pointer]:
                  - img [ref=e469]: share_1
                - button "Show more options" [ref=e475] [cursor=pointer]:
                  - img [ref=e477]: more_horiz
          - generic [ref=e479]:
            - generic [ref=e483]:
              - generic [ref=e487]:
                - heading "You said {PROMPT}" [level=5] [ref=e488]
                - paragraph [ref=e489]: {PROMPT}
              - generic [ref=e490]:
                - button "Copy prompt" [ref=e492] [cursor=pointer]:
                  - img [ref=e494]: copy
                - button "Edit" [ref=e497] [cursor=pointer]:
                  - img [ref=e499]: edit
            - generic [ref=e504]:
              - generic [ref=e507]:
                - heading "Gemini said" [level=6] [ref=e508]
                - generic [ref=e520]:
                  - button [ref=e521] [cursor=pointer]:
                    - img [ref=e522]
                  - generic [ref=e523]:
                    - button "Share image" [ref=e526] [cursor=pointer]:
                      - img [ref=e528]: share_1
                    - button "Copy image" [ref=e532] [cursor=pointer]:
                      - img [ref=e534]: content_copy
                    - button "Download full size image" [ref=e538] [cursor=pointer]:
                      - img [ref=e540]: download
              - generic [ref=e545]:
                - button "Good response" [ref=e548] [cursor=pointer]:
                  - img [ref=e550]: thumb_up
                - button "Bad response" [ref=e554] [cursor=pointer]:
                  - img [ref=e556]: thumb_down
                - button "Redo" [ref=e561] [cursor=pointer]:
                  - img [ref=e563]: refresh
                - button "Share image" [ref=e567] [cursor=pointer]:
                  - img [ref=e569]: share_1
                - button "Show more options" [ref=e575] [cursor=pointer]:
                  - img [ref=e577]: more_horiz
      - generic [ref=e579]:
        - group [ref=e580]:
          - generic [ref=e583]:
            - textbox "Enter a prompt for Gemini" [ref=e589]:
              - text: Ask Gemini
              - paragraph [ref=e590]
            - button "Upload & tools" [ref=e596] [cursor=pointer]:
              - img [ref=e598]: plus
            - generic [ref=e600]:
              - group [ref=e603]:
                - button "Open mode picker, currently Flash" [ref=e604]:
                  - generic [ref=e606]:
                    - generic [ref=e608]: Flash
                    - img [ref=e610]: keyboard_arrow_down
        - paragraph [ref=e624]: Gemini is AI and can make mistakes.
"""

NEWEST_DOWNLOAD = (
    '                    - button "Download full size image" [ref=e538] [cursor=pointer]:\n'
)
NEWEST_DOWNLOAD_ICON = "                      - img [ref=e540]: download\n"

# The same page with the newest image drawn and its download control not there
# yet, while the rating cluster already is. Whether Gemini ever renders this is
# not known; it is the case completion has to survive if it does.
STILL_RENDERING = IMAGE_TURN.replace(NEWEST_DOWNLOAD + NEWEST_DOWNLOAD_ICON, "")

# The rendered text of that page after the message: headings and control
# labels, no answer. a8s-browser 0.3.1 hands it over decoded.
FURNITURE_HISTORY = f"{PROMPT}\n\nGemini said\n\n\n\n\nFlash\n\n"


def _mixed(snapshot):
    """The newest image turn with a paragraph of text above its image."""
    anchor = '                - heading "Gemini said" [level=6] [ref=e508]\n'
    return snapshot.replace(
        anchor, anchor + "                - paragraph [ref=e509]: Here is your tile map.\n"
    )


def _two_images(snapshot):
    tile = (
        "                - generic [ref=e520]:\n"
        "                  - button [ref=e521] [cursor=pointer]:\n"
        "                    - img [ref=e522]\n"
    )
    second = tile.replace("e520", "e620").replace("e521", "e621").replace("e522", "e622")
    controls_start = snapshot.index(tile)
    controls_end = snapshot.index("              - generic [ref=e545]:")
    block = snapshot[controls_start:controls_end]
    return snapshot[:controls_end] + block.replace(tile, second) + snapshot[controls_end:]


class TestReadiness:
    def test_an_image_turn_is_ready_when_its_image_has_a_download_control(self):
        assert gemini.image_readiness(IMAGE_TURN) is True
        assert gemini.image_downloads(IMAGE_TURN) == 1

    def test_an_image_without_its_download_control_is_not_ready_even_with_the_cluster(self):
        assert gemini.turn_complete(STILL_RENDERING)  # the cluster is there
        assert gemini.image_readiness(STILL_RENDERING) is False

    def test_a_text_turn_shows_no_image_evidence(self):
        from test_gw_gemini import FINISHED

        assert gemini.image_readiness(FINISHED) is None
        assert gemini.image_downloads(FINISHED) == 0

    def test_only_the_newest_turn_counts(self):
        """The older exchange's download button says nothing about this turn."""
        assert gemini.image_downloads(STILL_RENDERING) == 0

    def test_two_images_need_two_download_controls(self):
        both = _two_images(IMAGE_TURN)
        assert gemini.image_readiness(both) is True
        assert gemini.image_downloads(both) == 2
        one_missing = both.replace(NEWEST_DOWNLOAD + NEWEST_DOWNLOAD_ICON, "", 1)
        assert gemini.image_readiness(one_missing) is False

    def test_a_picture_with_alt_text_is_still_a_picture(self):
        """Live, 2026-09-24: a second generation named its img `", AI generated"`."""
        named = STILL_RENDERING.replace(
            "                    - img [ref=e522]\n",
            '                    - img ", AI generated" [ref=e522]\n',
        )
        assert named != STILL_RENDERING
        assert gemini.image_readiness(named) is False
        assert gemini._image_tiles(gemini._newest_turn(named)) == 1

    def test_an_icon_is_not_a_picture(self):
        """Every control carries an img too; only an unnamed, valueless one is an image."""
        lines = STILL_RENDERING.splitlines()
        assert gemini._image_tiles(lines) == 2  # one per exchange, icons excluded


class TestTheReplyText:
    def test_an_image_only_turn_has_no_text_and_is_not_filled_in_from_the_page(self):
        assert gemini.extract_reply(IMAGE_TURN, FURNITURE_HISTORY, PROMPT) == ""

    def test_an_image_only_turn_is_never_filled_in_from_the_rendered_text(self):
        """The turn heading is on the page, so its turn is the whole answer."""
        elsewhere = f"{PROMPT}\nA sentence from somewhere else on the page."
        assert gemini.extract_reply(IMAGE_TURN, elsewhere, PROMPT) == ""

    def test_text_beside_an_image_is_the_reply(self):
        assert gemini.extract_reply(_mixed(IMAGE_TURN), "", PROMPT) == "Here is your tile map."

    def test_rendered_text_is_taken_as_decoded(self):
        """a8s-browser 0.3.1 decodes `text`; decoding again would turn a literal
        backslash-n that is really on the page into a line break."""
        history = f"{PROMPT}\nThe batch is green.\nprint('a\\nb')\n"
        assert gemini.extract_reply("- generic [ref=e1]", history, PROMPT) == (
            "The batch is green.\nprint('a\\nb')"
        )

    def test_furniture_is_never_a_reply(self):
        """The fallback, given the page that produced the defect, finds nothing to say."""
        no_headings = IMAGE_TURN.replace('heading "Gemini said"', 'heading "Model"')
        no_headings = no_headings.replace('heading "You said', 'heading "Asked')
        assert gemini.extract_reply(no_headings, FURNITURE_HISTORY, PROMPT) == ""


def _polls(tmp_path, *snapshots):
    return [transcript(tmp_path, snap, index=n) for n, snap in enumerate(snapshots)]


class TestCompletion:
    BEFORE = (1, 1)

    def test_an_image_only_turn_completes_with_its_image(self, tmp_path, clock):
        seat = ScriptedSeat(_polls(tmp_path, IMAGE_TURN))
        turn = gemini.await_reply(seat, PROMPT, baseline_counts=self.BEFORE)
        assert turn.complete
        assert turn.reply == ""
        assert turn.images == 1

    def test_a_turn_still_rendering_its_image_is_waited_for(self, tmp_path, clock):
        seat = ScriptedSeat(_polls(tmp_path, STILL_RENDERING, STILL_RENDERING, IMAGE_TURN))
        turn = gemini.await_reply(seat, PROMPT, baseline_counts=self.BEFORE)
        assert turn.complete and turn.images == 1
        assert not seat.transcripts  # all three looks were needed

    def test_a_pending_image_never_settles(self, tmp_path, clock):
        """Text beside a pending image stops changing; that is not completion."""
        pending = _mixed(STILL_RENDERING)
        polls = int(gemini.TURN_TIMEOUT_SECONDS / gemini.POLL_SECONDS) + 2
        seat = ScriptedSeat(_polls(tmp_path, *([pending] * polls)))
        turn = gemini.await_reply(seat, PROMPT, baseline_counts=self.BEFORE)
        assert not turn.complete
        assert turn.reply == "Here is your tile map."
        assert turn.images == 0
        assert "image" in turn.note

    def test_a_text_turn_completes_as_it_always_did(self, tmp_path, clock):
        from test_gw_gemini import FINISHED

        seat = ScriptedSeat(_polls(tmp_path, FINISHED))
        turn = gemini.await_reply(seat, "what is the status", baseline_counts=(1, 0))
        assert turn.complete and turn.images == 0
        assert turn.reply.startswith("The batch is green.")


def _image_seat(tmp_path, images, reply=""):
    seat = FakeGeminiSeat(tmp_path, reply=lambda prompt: reply if prompt == PROMPT else "ack")

    original = seat._do_press

    def press(args):
        # Only the sender's own message makes pictures, not the preamble.
        if args[0] == "Enter" and seat.draft == PROMPT:
            seat.next_images = list(images)
        return original(args)

    seat._do_press = press
    return seat


PNG = b"\x89PNG\r\n\x1a\n" + b"red"


class TestTheReply:
    def test_an_image_only_answer_arrives_as_an_attachment(
        self, tmp_path, outbox, clock, state_home
    ):
        seat = _image_seat(tmp_path, [("Gemini_Generated_Image_a1.png", PNG)])
        assert answer("gemini", "example-sender", PROMPT, outbox, seat) == 0
        assert outbox.last == "Generated 1 image."
        assert outbox.last_names == ["Gemini_Generated_Image_a1.png"]
        path = outbox.last_files[0]
        with open(path, "rb") as handle:
            assert handle.read() == PNG
        # Copied into this driver's own turn directory, not handed over from
        # the browser's artifacts.
        assert "artifacts" not in path
        assert os.sep + "out" + os.sep in path

    def test_text_and_image_arrive_together(self, tmp_path, outbox, clock, state_home):
        seat = _image_seat(tmp_path, [("map.png", PNG)], reply="Here is your tile map.")
        answer("gemini", "example-sender", PROMPT, outbox, seat)
        assert outbox.last == "Here is your tile map."
        assert outbox.last_names == ["map.png"]

    def test_two_images_are_two_files_even_when_named_alike(
        self, tmp_path, outbox, clock, state_home
    ):
        # The fake saves both downloads to one artifact path, as a browser that
        # names artifacts by the second can. Each must be kept before the next.
        first, second = PNG + b"FIRST", PNG + b"SECOND"
        seat = _image_seat(tmp_path, [("map.png", first), ("map.png", second)])
        answer("gemini", "example-sender", PROMPT, outbox, seat)
        assert outbox.last == "Generated 2 images."
        assert outbox.last_names == ["map.png", "map-2.png"]
        contents = [open(path, "rb").read() for path in outbox.last_files]
        assert contents == [first, second]
        downloads = [s for s in seat.scripts if s.startswith("download ")]
        assert [":nth-child(1 of" in downloads[0], ":nth-child(2 of" in downloads[1]] == [
            True,
            True,
        ]

    def test_a_download_that_fails_is_named_in_the_reply(
        self, tmp_path, outbox, clock, state_home
    ):
        seat = _image_seat(tmp_path, [("one.png", PNG), ("two.png", PNG + b"2")])
        seat.download_fails = {2}
        answer("gemini", "example-sender", PROMPT, outbox, seat)
        assert outbox.last_names == ["one.png"]
        assert outbox.last.startswith("Gemini generated 2 images; 1 attached.")
        assert "image 2 of 2 could not be downloaded" in outbox.last

    def test_the_same_picture_twice_is_a_failure_not_a_duplicate(
        self, tmp_path, outbox, clock, state_home
    ):
        seat = _image_seat(tmp_path, [("one.png", PNG), ("two.png", PNG)])
        answer("gemini", "example-sender", PROMPT, outbox, seat)
        assert outbox.last_names == ["one.png"]
        assert "image 2 of 2 could not be downloaded" in outbox.last

    def test_a_name_from_the_page_cannot_leave_the_directory(
        self, tmp_path, outbox, clock, state_home
    ):
        seat = _image_seat(tmp_path, [("..", PNG)])
        answer("gemini", "example-sender", PROMPT, outbox, seat)
        assert outbox.last_names == ["image-1"]

    def test_the_image_waits_for_its_download_control(
        self, tmp_path, outbox, clock, state_home
    ):
        seat = _image_seat(tmp_path, [("map.png", PNG)])
        seat.image_pending_polls = 3
        answer("gemini", "example-sender", PROMPT, outbox, seat)
        assert outbox.last_names == ["map.png"]
        assert seat.image_pending_polls == 0

    def test_a_text_reply_downloads_nothing(self, seat, outbox, clock, state_home):
        answer("gemini", "example-sender", "what is the status", outbox, seat)
        assert not any(script.startswith("download") for script in seat.scripts)
        assert outbox.last_files == []


class TestStatusSurvivesTheCap:
    """A long answer must not push the seat's own delivery notes out of the body."""

    def test_a_failed_image_is_named_beside_a_full_length_answer(
        self, tmp_path, outbox, clock, state_home
    ):
        answer_text = "x" * handler.MAX_BODY
        seat = _image_seat(tmp_path, [("lost.png", PNG)], reply=answer_text)
        seat.download_fails = {1}
        assert answer("gemini", "example-sender", PROMPT, outbox, seat) == 0
        assert len(outbox.last) <= handler.MAX_BODY
        assert "image 1 of 1 could not be downloaded" in outbox.last
        # The whole answer still travels, as the reply file.
        assert outbox.last_names == [handler.LONG_REPLY_NAME]
        with open(outbox.last_files[0]) as handle:
            assert handle.read().strip() == answer_text

    def test_an_answer_that_fits_alone_but_not_with_its_notes_goes_as_a_file(
        self, tmp_path, outbox, clock, state_home
    ):
        answer_text = "y" * (handler.MAX_BODY - 20)
        seat = _image_seat(tmp_path, [("one.png", PNG), ("two.png", PNG + b"2")],
                           reply=answer_text)
        seat.download_fails = {2}
        answer("gemini", "example-sender", PROMPT, outbox, seat)
        assert "image 2 of 2 could not be downloaded" in outbox.last
        assert outbox.last_names == [handler.LONG_REPLY_NAME, "one.png"]

    def test_compose_shortens_the_reply_never_the_notes(self):
        body = handler._compose("z" * 5000, ["the note that must survive"])
        assert len(body) <= handler.MAX_BODY
        assert body.endswith("--\nthe note that must survive")
        assert handler.CUT_MARK in body
