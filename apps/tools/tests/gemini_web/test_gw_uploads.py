"""A tell that carries files, from the message body into the conversation."""
import os
import shlex

from test_gw_handler import answer

from gemini_web import attachments
from gemini_web import gemini
from gemini_web import handler


def _attached(tmp_path, name, data=b"payload"):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def _with_files(prose, *paths):
    lines = [prose, ""] + [f"{attachments.ATTACHED_PREFIX}{path}" for path in paths]
    return "\n".join(lines)


def _typed(seat):
    return [text for role, text in seat.history if role == "user"]


def _asked(seat):
    """What a correspondent's own messages were, without the seat's preamble.

    A new correspondent gets the preamble typed first, so "nothing was typed"
    has to mean "the sender's message was not typed".
    """
    return [text for text in _typed(seat) if not text.startswith("You are ")]


class TestInbound:
    def test_the_file_is_uploaded_and_only_the_prose_is_typed(
        self, tmp_path, seat, outbox, clock, state_home
    ):
        path = _attached(tmp_path, "report.pdf")
        code = answer(
            "gemini", "example-sender", _with_files("summarise this", path), outbox, seat
        )
        assert code == 0
        assert seat.attached == ["report.pdf"]
        # The path is the thing that must not reach the page as text. It
        # belongs on the line that answers the file chooser and nowhere else.
        assert _asked(seat)[-1] == "summarise this"
        carrying = [
            line
            for script in seat.scripts
            for line in script.splitlines()
            if path in line
        ]
        assert carrying and all(line.startswith("upload ") for line in carrying)
        assert "report.pdf" in outbox.last

    def test_the_upload_happens_before_the_keystroke_that_sends(
        self, tmp_path, seat, outbox, clock, state_home
    ):
        path = _attached(tmp_path, "chart.png")
        answer("gemini", "example-sender", _with_files("what is this", path), outbox, seat)
        joined = "\n".join(seat.scripts)
        # A file that lands after the send is a question about nothing.
        assert joined.index("upload ") < joined.rindex(gemini.SUBMIT_STEP)

    def test_several_files_go_in_together(
        self, tmp_path, seat, outbox, clock, state_home
    ):
        one = _attached(tmp_path, "a.png")
        two = _attached(tmp_path, "b.png")
        answer("gemini", "example-sender", _with_files("compare", one, two), outbox, seat)
        assert seat.attached == ["a.png", "b.png"]

    def test_an_upload_that_takes_a_moment_is_waited_for(
        self, tmp_path, seat, outbox, clock, state_home
    ):
        seat.upload_polls = 3
        path = _attached(tmp_path, "slow.pdf")
        code = answer("gemini", "example-sender", _with_files("read it", path), outbox, seat)
        assert code == 0
        assert seat.attached == ["slow.pdf"]

    def test_an_upload_that_never_appears_keeps_the_message(
        self, tmp_path, seat, outbox, clock, state_home
    ):
        seat.upload_polls = 10_000
        path = _attached(tmp_path, "stuck.pdf")
        code = answer("gemini", "example-sender", _with_files("read it", path), outbox, seat)
        # Nothing was submitted, so a8s may hand this back — and must, rather
        # than let the sender believe Gemini saw the file.
        assert code == 0
        assert "Send it again" in outbox.last
        assert _asked(seat) == []
        assert "stuck.pdf" in outbox.last
        assert "was not sent" in outbox.last

    def test_a_missing_file_is_a_non_send_not_a_wrong_answer(
        self, tmp_path, seat, outbox, clock, state_home
    ):
        code = answer(
            "gemini",
            "example-sender",
            _with_files("read it", str(tmp_path / "gone.pdf")),
            outbox,
            seat,
        )
        assert code == 0
        assert "Send it again" in outbox.last
        assert _asked(seat) == []

    def test_a_lost_attachment_is_named_in_the_reply(
        self, tmp_path, seat, outbox, clock, state_home
    ):
        message = (
            "look at this\n\n"
            f"{attachments.UNAVAILABLE_PREFIX}chart.png: could not download after 900s"
        )
        code = answer("gemini", "example-sender", message, outbox, seat)
        assert code == 0
        # The question still goes to Gemini; the sender is told what it went without.
        assert _asked(seat)[-1] == "look at this"
        assert "chart.png" in outbox.last


class TestTheUploadMenu:
    """Files go in through Gemini's own menu and chooser (live, 2026-09-24)."""

    def test_each_file_gets_its_own_menu_and_chooser(
        self, tmp_path, seat, outbox, clock, state_home
    ):
        one = _attached(tmp_path, "a.png")
        two = _attached(tmp_path, "b.png")
        answer("gemini", "example-sender", _with_files("compare", one, two), outbox, seat)
        joined = "\n".join(seat.scripts)
        # A chooser takes one file and closes, so the menu opens once per file.
        assert joined.count(f"click {shlex.quote(gemini.UPLOAD_MENU_SELECTOR)}") == 2
        assert joined.count(f"upload {one}") == 1
        assert joined.count(f"upload {two}") == 1

    def test_a_synthetic_drop_is_never_used(self, tmp_path, seat, outbox, clock, state_home):
        path = _attached(tmp_path, "report.pdf")
        answer("gemini", "example-sender", _with_files("read it", path), outbox, seat)
        assert not any(
            line.startswith("drop ") for script in seat.scripts for line in script.splitlines()
        )

    def test_a_chooser_that_never_opens_is_a_non_send_and_the_menu_is_closed(
        self, tmp_path, seat, outbox, clock, state_home
    ):
        seat.clickable.discard(gemini.UPLOAD_MENU_SELECTOR)  # the page changed shape
        path = _attached(tmp_path, "report.pdf")
        code = answer("gemini", "example-sender", _with_files("read it", path), outbox, seat)
        assert code == 0
        assert "Send it again" in outbox.last
        assert _asked(seat) == []
        assert "report.pdf" in outbox.last
        # An open menu swallows the keystrokes that follow it.
        assert "press Escape" in seat.scripts
        assert not seat.menu_open

    def test_the_chip_shows_the_name_without_its_extension(self):
        assert gemini.chip_label("probe-note.txt") == "probe-note"
        assert gemini.chip_label("archive.tar.gz") == "archive.tar"
        assert gemini.chip_label("README") == "README"
        # Read off the live chip for a 47-character name.
        long_name = "a-rather-long-file-name-for-the-chip-check-2026.txt"
        assert gemini.chip_label(long_name) == "a-rather-l...check-2026"
        composer = (
            "- group [ref=e0]:\n"
            "  - generic [ref=e1] [cursor=pointer]:\n"
            "    - generic [ref=e2]: TXT\n"
            "    - generic [ref=e3]: probe-note\n"
            '  - textbox "Enter a prompt for Gemini" [ref=e4]\n'
        )
        assert gemini.chips(composer, ["probe-note.txt"]) == ((1,), 0)


class TestConsent:
    def test_the_first_upload_disclaimer_keeps_the_message_and_names_the_fix(
        self, tmp_path, seat, outbox, clock, state_home
    ):
        seat.consent_needed = True
        path = _attached(tmp_path, "report.pdf")
        code = answer(
            "gemini", "example-sender", _with_files("summarise", path), outbox, seat
        )
        # Nothing was typed, so the message is worth keeping until a person
        # accepts the disclaimer once.
        assert code == 0
        assert "Send it again" in outbox.last
        assert _asked(seat) == []
        assert "Agree" in outbox.last
        assert "by hand" in outbox.last

    def test_the_driver_never_presses_agree_itself(
        self, tmp_path, seat, outbox, clock, state_home
    ):
        seat.consent_needed = True
        path = _attached(tmp_path, "report.pdf")
        answer("gemini", "example-sender", _with_files("summarise", path), outbox, seat)
        # Accepting terms on somebody's account is not this driver's to do.
        assert "Agree" not in "\n".join(seat.scripts)

    def test_consent_is_recognised_from_the_page_text(self):
        assert gemini.consent_pending(f'- heading "{gemini.CONSENT_HEADING}" [level=1]')
        assert not gemini.consent_pending("- textbox \"Enter a prompt for Gemini\"")
        assert not gemini.consent_pending("")


class TestConfirmationIsNotJustTheName:
    """Carlos R1 on PR3: the page carries the whole conversation."""

    def test_a_name_already_in_the_history_does_not_confirm_a_new_upload(
        self, tmp_path, seat, outbox, clock, state_home
    ):
        # A real earlier turn, so the conversation is resumed rather than
        # created — a fresh conversation would clear the history and the
        # scenario with it.
        answer("gemini", "example-sender", "I will send you report.pdf shortly", outbox, seat)
        assert any("report.pdf" in text for _, text in seat.history)

        seat.upload_polls = 10_000  # this upload never lands
        path = _attached(tmp_path, "report.pdf")

        code = answer(
            "gemini", "example-sender", _with_files("and this one?", path), outbox, seat
        )

        # Before the repair this returned 0, submitted the prompt without the
        # file, and told the sender it was attached.
        assert code == 0
        assert "Send it again" in outbox.last
        assert "and this one?" not in _asked(seat)
        assert "was not sent" in outbox.last
        assert "attached to this turn" not in outbox.last

    def test_a_chip_beside_a_disabled_send_never_confirms(
        self, tmp_path, seat, outbox, clock, state_home
    ):
        """Carlos R1 on the repair: the pending page is STATIC.

        The chip is there and the indicator never moves, so two identical
        snapshots are the normal case for an upload that has not finished.
        """
        seat.upload_pending_polls = 10_000
        path = _attached(tmp_path, "slow.pdf")

        code = answer("gemini", "example-sender", _with_files("read it", path), outbox, seat)

        assert code == 0
        assert "Send it again" in outbox.last
        assert "read it" not in _asked(seat)
        assert "did not finish uploading" in outbox.last

    def test_an_upload_that_becomes_ready_is_confirmed(
        self, tmp_path, seat, outbox, clock, state_home
    ):
        # Disabled for a while, then the control enables: the transition is the
        # only thing that counts as done.
        seat.upload_pending_polls = 3
        path = _attached(tmp_path, "fine.pdf")

        code = answer("gemini", "example-sender", _with_files("read it", path), outbox, seat)

        assert code == 0
        assert _asked(seat)[-1] == "read it"
        assert seat.attached == ["fine.pdf"]

    def test_no_send_control_is_unknown_and_unknown_is_not_ready(
        self, tmp_path, seat, outbox, clock, state_home
    ):
        # The page changed shape. Absence of evidence must not read as ready.
        seat.send_button = False
        path = _attached(tmp_path, "report.pdf")

        code = answer("gemini", "example-sender", _with_files("read it", path), outbox, seat)

        assert code == 0
        assert "Send it again" in outbox.last
        assert "read it" not in _asked(seat)
        assert "cannot tell" in outbox.last
        assert gemini.SEND_BUTTON_NAME in outbox.last
        assert "docs/gemini-ui.md" in outbox.last

    def test_the_text_is_typed_before_the_files_go_in(
        self, tmp_path, seat, outbox, clock, state_home
    ):
        # The send control does not exist on an empty composer, so an upload
        # that happened first would leave nothing to read.
        path = _attached(tmp_path, "order.pdf")
        answer("gemini", "example-sender", _with_files("read it", path), outbox, seat)
        joined = "\n".join(seat.scripts)
        assert joined.index("type <<") < joined.rindex("upload ")
        assert joined.rindex("upload ") < joined.rindex(gemini.SUBMIT_STEP)

    def test_send_ready_reads_the_control_three_ways(self):
        enabled = f'- button "{gemini.SEND_BUTTON_NAME}" [ref=e1] [cursor=pointer]'
        disabled = f'- button "{gemini.SEND_BUTTON_NAME}" [ref=e1] [disabled]'
        assert gemini.send_ready(enabled) is True
        assert gemini.send_ready(disabled) is False
        assert gemini.send_ready('- textbox "Enter a prompt for Gemini" [ref=e1]') is None

    def test_a_repeat_of_the_same_filename_is_still_confirmed(
        self, tmp_path, seat, outbox, clock, state_home
    ):
        # Sending the same name twice must work: the test is one MORE
        # occurrence, not a first one.
        path = _attached(tmp_path, "again.pdf")
        answer("gemini", "example-sender", _with_files("first", path), outbox, seat)
        seat.attached = []  # Gemini clears the composer after a send
        code = answer("gemini", "example-sender", _with_files("second", path), outbox, seat)
        assert code == 0
        assert _asked(seat)[-1] == "second"


# The composer as the live page drew it at 08:52 PDT on 2026-09-24, after an
# image went in through the chooser: a thumbnail, and no filename anywhere.
IMAGE_IN_COMPOSER = """\
      - generic [ref=f1e700]:
        - heading "Gemini said" [level=6] [ref=f1e701]
        - generic [ref=f1e702]:
          - button [ref=f1e703] [cursor=pointer]:
            - img [ref=f1e704]
      - generic [ref=f1e579]:
        - group [ref=f1e580]:
          - generic [ref=f1e583]:
            - img "attachment" [ref=f1e584]
            - textbox "Enter a prompt for Gemini" [active] [ref=f1e589]:
              - paragraph [ref=f1e590]: what is in this picture?
            - button "Upload & tools" [ref=f1e596] [cursor=pointer]:
              - img [ref=f1e598]: plus
            - generic [ref=f1e600]:
              - button "Send message" [ref=f1e601] [cursor=pointer]:
                - img [ref=f1e602]: arrow_upward
        - paragraph [ref=f1e624]: Gemini is AI and can make mistakes.
"""
EMPTY_COMPOSER = IMAGE_IN_COMPOSER.replace('            - img "attachment" [ref=f1e584]\n', "")
PICTURE = "Gemini_Generated_Image_abc123.jpeg"


class TestImageUploads:
    """An image's chip is a thumbnail: `img "attachment"`, no name (live, 2026-09-24)."""

    def test_an_image_thumbnail_in_the_composer_confirms_the_upload(self):
        before = gemini.chips(EMPTY_COMPOSER, [PICTURE])
        after = gemini.chips(IMAGE_IN_COMPOSER, [PICTURE])
        assert before == ((0,), 0)
        assert after == ((0,), 1)
        assert gemini.uploads_shown(before, after, [PICTURE])

    def test_only_the_composer_is_counted(self):
        """The conversation carries pictures too, and one there is not an upload."""
        in_history = EMPTY_COMPOSER.replace(
            "            - img [ref=f1e704]\n",
            '            - img "attachment" [ref=f1e704]\n',
        )
        assert gemini.chips(in_history, [PICTURE]) == ((0,), 0)

    def test_a_thumbnail_in_the_composer_is_not_a_picture_gemini_drew(self):
        assert gemini._image_tiles(gemini._newest_turn(IMAGE_IN_COMPOSER)) == 1
        assert not any("attachment" in line for line in gemini._newest_turn(IMAGE_IN_COMPOSER))

    def test_two_images_need_two_thumbnails(self):
        names = ["a.png", "b.jpg"]
        before = gemini.chips(EMPTY_COMPOSER, names)
        one = gemini.chips(IMAGE_IN_COMPOSER, names)
        assert not gemini.uploads_shown(before, one, names)
        two = IMAGE_IN_COMPOSER.replace(
            '            - img "attachment" [ref=f1e584]\n',
            '            - img "attachment" [ref=f1e584]\n'
            '            - img "attachment" [ref=f1e585]\n',
        )
        assert gemini.uploads_shown(before, gemini.chips(two, names), names)

    def test_a_type_that_could_be_either_counts_either_chip_but_only_once(self):
        names = ["diagram.svg", "chart.svg"]
        before = gemini.chips(EMPTY_COMPOSER, names)
        one_thumb = gemini.chips(IMAGE_IN_COMPOSER, names)
        assert not gemini.uploads_shown(before, one_thumb, names)
        named = IMAGE_IN_COMPOSER.replace(
            '            - img "attachment" [ref=f1e584]\n',
            '            - img "attachment" [ref=f1e584]\n'
            "            - generic [ref=f1e585] [cursor=pointer]:\n"
            "              - generic [ref=f1e586]: SVG\n"
            "              - generic [ref=f1e587]: chart\n",
        )
        assert gemini.uploads_shown(before, gemini.chips(named, names), names)

    def test_a_page_with_no_composer_group_has_no_chips_to_count(self):
        flat = '- textbox "Enter a prompt for Gemini" [ref=e1]\n- img "attachment" [ref=e2]\n'
        assert gemini.chips(flat, [PICTURE]) is None

    def test_an_image_upload_is_confirmed_end_to_end(
        self, tmp_path, seat, outbox, clock, state_home
    ):
        path = _attached(tmp_path, PICTURE, b"\xff\xd8\xff\xe0jpeg")
        code = answer("gemini", "example-sender", _with_files("what is this?", path), outbox, seat)
        assert code == 0
        assert _asked(seat)[-1] == "what is this?"
        assert seat.attached == [PICTURE]

    def test_a_document_and_an_image_together(self, tmp_path, seat, outbox, clock, state_home):
        doc = _attached(tmp_path, "notes.txt")
        pic = _attached(tmp_path, "photo.png", b"\x89PNG")
        code = answer("gemini", "example-sender", _with_files("compare", doc, pic), outbox, seat)
        assert code == 0
        assert seat.attached == ["notes.txt", "photo.png"]

    def test_an_image_that_never_shows_keeps_the_message(
        self, tmp_path, seat, outbox, clock, state_home
    ):
        seat.upload_polls = 10_000
        path = _attached(tmp_path, PICTURE, b"\xff\xd8")
        code = answer("gemini", "example-sender", _with_files("what is this?", path), outbox, seat)
        assert code == 0
        assert "Send it again" in outbox.last
        assert _asked(seat) == []
        assert "did not finish uploading" in outbox.last


class TestEachChipCountsOnce:
    """Evidence is chip nodes, and each new chip is given to one file at most."""

    def test_a_thumbnail_never_confirms_a_document_named_attachment(
        self, tmp_path, seat, outbox, clock, state_home
    ):
        class LosesTheDocument(type(seat)):
            def _do_upload(self, args):
                if os.path.basename(args[0]) == "attachment.pdf":
                    self.chooser_open = False  # the chooser took it; the page never shows it
                    return "attachment.pdf"
                return super()._do_upload(args)

        seat = LosesTheDocument(tmp_path)
        doc = _attached(tmp_path, "attachment.pdf", b"%PDF")
        pic = _attached(tmp_path, "photo.jpg", b"\xff\xd8")
        code = answer("gemini", "example-sender", _with_files("compare", doc, pic), outbox, seat)
        assert seat.attached == ["photo.jpg"]
        assert code == 0
        assert "Send it again" in outbox.last
        assert _asked(seat) == []
        assert "attached to this turn" not in outbox.last

    def test_one_thumbnail_is_not_two_files_of_either_kind(self):
        names = ["attachment.svg", "chart.svg"]
        before = gemini.chips(EMPTY_COMPOSER, names)
        after = gemini.chips(IMAGE_IN_COMPOSER, names)
        assert after == ((0, 0), 1)
        assert not gemini.uploads_shown(before, after, names)

    def test_two_documents_with_one_label_need_two_chips(self):
        names = ["report.pdf", "report.docx"]
        chip = (
            "            - generic [ref=f1e590] [cursor=pointer]:\n"
            "              - generic [ref=f1e591]: PDF\n"
            "              - generic [ref=f1e592]: report\n"
        )
        anchor = '            - img "attachment" [ref=f1e584]\n'
        one = IMAGE_IN_COMPOSER.replace(anchor, chip)
        two = IMAGE_IN_COMPOSER.replace(anchor, chip + chip)
        before = gemini.chips(EMPTY_COMPOSER, names)
        assert not gemini.uploads_shown(before, gemini.chips(one, names), names)
        assert gemini.uploads_shown(before, gemini.chips(two, names), names)

    def test_the_prompt_and_the_page_furniture_are_not_chips(self):
        """A short name matches plenty of text; only a chip's own value counts."""
        names = ["what.txt", "arrow_upward.txt", "plus.txt"]
        before = gemini.chips(EMPTY_COMPOSER, names)
        assert before == ((0, 0, 0), 0)
        assert not gemini.uploads_shown(before, gemini.chips(IMAGE_IN_COMPOSER, names), names)
