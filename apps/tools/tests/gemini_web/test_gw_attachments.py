"""The a8s file seam: what arrives in the message body, and what goes back."""
import os

from gemini_web import attachments


def _wrote(tmp_path, name, data=b"body"):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


class TestReadingTheMessage:
    def test_a_message_with_no_attachments_is_left_alone(self, tmp_path):
        incoming = attachments.read("just a question", str(tmp_path))
        assert incoming.prose == "just a question"
        assert incoming.paths == []
        assert incoming.notes == []

    def test_an_attached_path_is_taken_out_of_the_prose(self, tmp_path):
        one = _wrote(tmp_path, "report.pdf")
        message = f"summarise this\n\n{attachments.ATTACHED_PREFIX}{one}"
        incoming = attachments.read(message, str(tmp_path / "work"))
        # The path must not reach Gemini: it means nothing there and it says
        # where this machine keeps its mail.
        assert incoming.prose == "summarise this"
        assert one not in incoming.prose
        assert incoming.paths == [one]

    def test_every_attached_path_is_collected_in_order(self, tmp_path):
        one = _wrote(tmp_path, "a.png")
        two = _wrote(tmp_path, "b.png")
        message = (
            f"compare these\n\n{attachments.ATTACHED_PREFIX}{one}\n"
            f"{attachments.ATTACHED_PREFIX}{two}"
        )
        incoming = attachments.read(message, str(tmp_path / "work"))
        assert incoming.paths == [one, two]
        assert incoming.prose == "compare these"

    def test_a_lost_attachment_becomes_a_note_and_not_a_path(self, tmp_path):
        message = (
            f"look at this\n\n{attachments.UNAVAILABLE_PREFIX}chart.png: could not download"
        )
        incoming = attachments.read(message, str(tmp_path / "work"))
        assert incoming.paths == []
        assert incoming.prose == "look at this"
        assert len(incoming.notes) == 1
        # The sender believes they sent a file; saying nothing reads as an answer
        # about it.
        assert "chart.png" in incoming.notes[0]
        assert "never received" in incoming.notes[0]

    def test_prose_keeps_its_own_blank_lines_and_indentation(self, tmp_path):
        one = _wrote(tmp_path, "x.txt")
        message = f"line one\n\n    indented\n{attachments.ATTACHED_PREFIX}{one}"
        incoming = attachments.read(message, str(tmp_path / "work"))
        assert incoming.prose == "line one\n\n    indented"


class TestSplitFiles:
    def test_a_complete_part_set_is_joined_back_into_one_file(self, tmp_path):
        one = _wrote(tmp_path, "big.bin.part1of3", b"aaa")
        two = _wrote(tmp_path, "big.bin.part2of3", b"bbb")
        three = _wrote(tmp_path, "big.bin.part3of3", b"ccc")
        message = "".join(
            f"{attachments.ATTACHED_PREFIX}{path}\n" for path in (one, two, three)
        )
        incoming = attachments.read("here it is\n" + message, str(tmp_path / "work"))
        assert len(incoming.paths) == 1
        assert os.path.basename(incoming.paths[0]) == "big.bin"
        assert open(incoming.paths[0], "rb").read() == b"aaabbbccc"
        assert incoming.notes == []

    def test_parts_are_joined_in_index_order_whatever_order_they_arrive_in(self, tmp_path):
        three = _wrote(tmp_path, "d.bin.part3of3", b"C")
        one = _wrote(tmp_path, "d.bin.part1of3", b"A")
        two = _wrote(tmp_path, "d.bin.part2of3", b"B")
        message = "".join(
            f"{attachments.ATTACHED_PREFIX}{path}\n" for path in (three, one, two)
        )
        incoming = attachments.read(message, str(tmp_path / "work"))
        assert open(incoming.paths[0], "rb").read() == b"ABC"

    def test_an_incomplete_part_set_is_not_uploaded_at_all(self, tmp_path):
        one = _wrote(tmp_path, "big.bin.part1of3", b"aaa")
        three = _wrote(tmp_path, "big.bin.part3of3", b"ccc")
        message = "".join(
            f"{attachments.ATTACHED_PREFIX}{path}\n" for path in (one, three)
        )
        incoming = attachments.read(message, str(tmp_path / "work"))
        # Joining what arrived would hand Gemini a truncated file and call it
        # the sender's document.
        assert incoming.paths == []
        assert len(incoming.notes) == 1
        assert "missing 2" in incoming.notes[0]
        assert "big.bin" in incoming.notes[0]

    def test_a_split_set_and_a_whole_file_both_come_through(self, tmp_path):
        whole = _wrote(tmp_path, "notes.txt", b"n")
        one = _wrote(tmp_path, "s.bin.part1of2", b"1")
        two = _wrote(tmp_path, "s.bin.part2of2", b"2")
        message = "".join(
            f"{attachments.ATTACHED_PREFIX}{path}\n" for path in (whole, one, two)
        )
        incoming = attachments.read(message, str(tmp_path / "work"))
        assert sorted(os.path.basename(p) for p in incoming.paths) == ["notes.txt", "s.bin"]


class TestSafeName:
    def test_a_plain_name_is_kept(self):
        assert attachments.safe_name("chart.png") == "chart.png"

    def test_a_traversing_name_cannot_escape_its_directory(self):
        assert attachments.safe_name("../../etc/passwd") == "passwd"
        assert attachments.safe_name("..") == attachments.FALLBACK_NAME

    def test_an_absolute_path_is_reduced_to_its_basename(self):
        assert attachments.safe_name("/etc/shadow") == "shadow"

    def test_a_windows_separator_is_a_separator_too(self):
        assert attachments.safe_name(r"C:\\Windows\\win.ini") == "win.ini"

    def test_an_empty_or_dotted_name_gets_the_fallback(self):
        assert attachments.safe_name("") == attachments.FALLBACK_NAME
        assert attachments.safe_name("   ") == attachments.FALLBACK_NAME
        assert attachments.safe_name(".") == attachments.FALLBACK_NAME
        assert attachments.safe_name(None) == attachments.FALLBACK_NAME

    def test_a_null_byte_cannot_survive_into_a_filename(self):
        assert "\0" not in attachments.safe_name("bad\0name.txt")
