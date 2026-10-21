"""Where a reply breaks into lines.

Every snapshot here is a model turn copied from the live page on 2026-09-24,
with the file names in it replaced. Each history is that page's rendered text
(`text main`) for the same turn. What the snapshot does with each piece of
markup, as observed:

- bold and italic (`<b>`, `<i>`) are merged into the surrounding `text` node;
- inline code is a `code` child of the paragraph, and a link a `link` child
  named for its text;
- a citation marker is an empty `superscript` between two `text` children,
  followed by a single-quoted `button` for the source chip;
- every run is trimmed, so the space between two runs is not in the snapshot;
- a code block is a `code` node beside the block's own `Copy code` controls,
  and the snapshot collapses its newlines and indentation. Only the rendered
  text still has them.
"""
from gw_support import ScriptedSeat
from test_gw_gemini import transcript

from gemini_web import gemini


def turn(*body):
    """One finished model turn holding `body`, indented the way the page nests it."""
    return "\n".join(
        [
            '- heading "You said ask" [level=5] [ref=e1]',
            "- paragraph [ref=e2]: ask",
            "- generic [ref=e3]:",
            '  - heading "Gemini said" [level=6] [ref=e4]',
            "  - generic [ref=e5]:",
            *(f"    {line}" for line in body),
            "- generic [ref=e80]:",
            '  - button "Good response" [ref=e81] [cursor=pointer]:',
            "    - img [ref=e82]: thumb_up",
            '  - button "Show more options" [ref=e83] [cursor=pointer]:',
            "    - img [ref=e84]: more_horiz",
            '- textbox "Enter a prompt for Gemini" [ref=e90]:',
            "  - text: Ask Gemini",
            "- paragraph [ref=e91]: Gemini is AI and can make mistakes.",
        ]
    ) + "\n"


CITED = turn(
    "- paragraph [ref=e10]:",
    "  - text: The secret word is MARIGOLD-9",
    "  - superscript [ref=e11]",
    '  - text: ", and the shapes in the picture are a star and a triangle"',
    "  - superscript [ref=e12]",
    "  - text: .",
    "  - 'button \"View source details for citations from TXT: notes.txt and JPEG: "
    'picture.jpeg. Press Enter to open sources dialog." [ref=e13] [cursor=pointer]\':',
    "    - generic [ref=e14]:",
    "      - generic [ref=e15]: TXT",
    "      - generic [ref=e16]: + 1",
)
CITED_HISTORY = (
    "Gemini said\n\nThe secret word is MARIGOLD-9, and the shapes in the picture are "
    "a star and a triangle.\nTXT\n+ 1\n\n\nFlash\n\nGemini is AI and can make mistakes."
)

FORMATTED = turn(
    '- heading "Status Report" [level=2] [ref=e20]',
    "- paragraph [ref=e21]:",
    "  - text: The relay is active, operating normally via",
    "  - code [ref=e22]: protocol",
    "  - text: .",
    "- paragraph [ref=e23]:",
    "  - text: Visit",
    '  - link "Example" [ref=e24] [cursor=pointer]:',
    "    - /url: https://example.com?utm_source=gemini",
    "  - text: for external connection tests.",
    "- list [ref=e25]:",
    "  - listitem [ref=e26]:",
    "    - paragraph [ref=e27]: Primary channel confirmed",
    "  - listitem [ref=e28]:",
    "    - paragraph [ref=e29]: Secondary listener disabled",
    "- generic [ref=e30]:",
    "  - generic [ref=e31]:",
    "    - generic [ref=e32]: Python",
    "    - generic [ref=e33]:",
    '      - button "Download code" [ref=e34] [cursor=pointer]:',
    "        - img [ref=e35]: arrow_circle_down",
    '      - button "Copy code" [ref=e36] [cursor=pointer]:',
    "        - img [ref=e37]: copy",
    "  - code [ref=e38]:",
    '    - generic [ref=e39]: "def check_status():"',
    "    - text: return True",
    "- paragraph [ref=e40]: Transmission concluded successfully.",
)
FORMATTED_HISTORY = (
    "Gemini said\nStatus Report\n\nThe relay is active, operating normally via protocol.\n\n"
    "Visit Example for external connection tests.\n\nPrimary channel confirmed\n\n"
    "Secondary listener disabled\n\nPython\ndef check_status():\n    return True\n\n\n"
    "Transmission concluded successfully.\n\n\n\n\nFlash\n\nGemini is AI and can make mistakes."
)

STRUCTURED = turn(
    "- list [ref=e20]:",
    "  - listitem [ref=e21]:",
    "    - paragraph [ref=e22]: Initialize relay",
    "  - listitem [ref=e23]:",
    "    - paragraph [ref=e24]: Verify connection",
    "    - list [ref=e25]:",
    "      - listitem [ref=e26]:",
    "        - paragraph [ref=e27]: Confirmed",
    "- blockquote [ref=e28]:",
    "  - paragraph [ref=e29]: Signal integrity verified.",
    "- generic [ref=e30]:",
    "  - table [ref=e31]:",
    "    - rowgroup [ref=e32]:",
    "      - row [ref=e33]:",
    '        - columnheader "Key" [ref=e34]',
    '        - columnheader "Value" [ref=e35]',
    "    - rowgroup [ref=e36]:",
    "      - row [ref=e37]:",
    '        - cell "Status" [ref=e38]',
    '        - cell "Active" [ref=e39]',
    "      - row [ref=e40]:",
    '        - cell "Mode" [ref=e41]',
    '        - cell "Stream" [ref=e42]',
    '  - button "More options" [ref=e43] [cursor=pointer]:',
    "    - img [ref=e44]: more_horiz",
    "- paragraph [ref=e45]:",
    "  - text: Call",
    "  - code [ref=e46]: len",
    "  - text: (x) on a list of",
    "  - code [ref=e47]: str",
    "  - text: s, then see",
    '  - link "the docs" [ref=e48] [cursor=pointer]:',
    "    - /url: https://example.com/docs?utm_source=gemini",
    "  - text: .",
    "- generic [ref=e50]:",
    "  - generic [ref=e51]:",
    "    - generic [ref=e52]: JSON",
    "    - generic [ref=e53]:",
    '      - button "Copy code" [ref=e54] [cursor=pointer]:',
    "        - img [ref=e55]: copy",
    '  - code [ref=e56]: "{ \\"status\\": \\"ok\\" }"',
)
STRUCTURED_HISTORY = (
    "Gemini said\n\nInitialize relay\n\nVerify connection\n\nConfirmed\n\n"
    "Signal integrity verified.\n\nKey\tValue\nStatus\tActive\nMode\tStream\n\n"
    "Call len(x) on a list of strs, then see the docs.\n\n"
    'JSON\n{\n  "status": "ok"\n}\n\n\n\n\n\nFlash\n\nGemini is AI and can make mistakes.'
)

NESTED_CODE = turn(
    "- list [ref=e20]:",
    "  - listitem [ref=e21]:",
    "    - paragraph [ref=e22]: Execute the initial setup command.",
    "    - generic [ref=e23]:",
    "      - generic [ref=e24]:",
    "        - generic [ref=e25]: Bash",
    "        - generic [ref=e26]:",
    '          - button "Copy code" [ref=e27] [cursor=pointer]:',
    "            - img [ref=e28]: copy",
    "      - code [ref=e29]: run_probe --verify",
    "  - listitem [ref=e30]:",
    "    - paragraph [ref=e31]:",
    "      - text: Verify the",
    "      - code [ref=e32]: status",
    "      - text: output.",
    "- paragraph [ref=e33]: done",
)
NESTED_CODE_HISTORY = (
    "Gemini said\n\nExecute the initial setup command.\n\nBash\nrun_probe\n    --verify\n\n\n"
    "Verify the status output.\n\ndone\n\n\n\n\nFlash\n\nGemini is AI and can make mistakes."
)


def lines(snapshot, history=""):
    return gemini.reply_block(snapshot, history).splitlines()


def test_a_cited_sentence_is_one_line():
    """The live defect: the citation markers split one sentence into three lines."""
    expected = (
        "The secret word is MARIGOLD-9, and the shapes in the picture are a star and a triangle."
    )
    assert gemini.reply_block(CITED, CITED_HISTORY) == expected
    assert gemini.reply_block(CITED) == expected


def test_a_bold_word_and_inline_code_stay_in_their_sentence():
    assert "The relay is active, operating normally via protocol." in lines(FORMATTED)
    assert "The relay is active, operating normally via protocol." in lines(
        FORMATTED, FORMATTED_HISTORY
    )


def test_a_link_stays_in_its_sentence_and_its_url_stays_out():
    assert "Visit Example for external connection tests." in lines(FORMATTED)
    assert "utm_source" not in gemini.reply_block(FORMATTED)


def test_the_page_decides_the_spacing_around_inline_code():
    """`len`(x) and `str`s have no space on the page; a guess would add one."""
    assert "Call len(x) on a list of strs, then see the docs." in lines(
        STRUCTURED, STRUCTURED_HISTORY
    )


def test_without_the_rendered_text_runs_are_spaced_but_never_before_punctuation():
    reply = gemini.reply_block(STRUCTURED)
    assert "then see the docs." in reply
    assert "(x) on a list of" in reply
    assert " ." not in reply
    assert " ," not in reply


def test_two_paragraphs_keep_their_break():
    reply = gemini.reply_block(FORMATTED, FORMATTED_HISTORY)
    assert (
        "The relay is active, operating normally via protocol.\n"
        "Visit Example for external connection tests."
    ) in reply


def test_list_items_are_one_per_line_including_a_nested_one():
    assert lines(STRUCTURED, STRUCTURED_HISTORY)[:3] == [
        "Initialize relay",
        "Verify connection",
        "Confirmed",
    ]


def test_a_heading_inside_the_answer_is_its_own_line():
    assert lines(FORMATTED, FORMATTED_HISTORY)[0] == "Status Report"


def test_a_blockquote_is_its_own_line_and_a_table_is_one_line_per_row():
    """The header row too. The page renders a row as one line, and a cell per
    line would make a table unreadable in a text reply."""
    got = lines(STRUCTURED, STRUCTURED_HISTORY)
    assert got[3:8] == [
        "Signal integrity verified.",
        "Key | Value",
        "Status | Active",
        "Mode | Stream",
        "Call len(x) on a list of strs, then see the docs.",
    ]
    assert lines(STRUCTURED)[4:7] == ["Key | Value", "Status | Active", "Mode | Stream"]


def test_a_code_block_keeps_its_newlines_and_indentation():
    reply = gemini.reply_block(FORMATTED, FORMATTED_HISTORY)
    assert "Secondary listener disabled\ndef check_status():\n    return True\n" in reply
    assert reply.endswith("return True\nTransmission concluded successfully.")


def test_a_code_block_the_snapshot_flattened_to_one_line_is_read_from_the_page():
    reply = gemini.reply_block(STRUCTURED, STRUCTURED_HISTORY)
    assert reply.endswith('the docs.\n{\n  "status": "ok"\n}')


def test_without_the_rendered_text_a_code_block_keeps_the_lines_the_snapshot_has():
    reply = gemini.reply_block(FORMATTED)
    assert "def check_status():\nreturn True" in reply
    assert "Python" not in reply
    assert "Copy code" not in reply


def test_a_code_block_inside_a_list_item_is_a_block_and_not_a_run():
    assert gemini.reply_block(NESTED_CODE, NESTED_CODE_HISTORY) == (
        "Execute the initial setup command.\n"
        "run_probe\n    --verify\n"
        "Verify the status output.\n"
        "done"
    )


def test_the_citation_chip_is_not_part_of_the_answer():
    reply = gemini.reply_block(CITED, CITED_HISTORY)
    assert "TXT" not in reply
    assert "notes.txt" not in reply


def test_a_single_quoted_node_is_read_as_the_node_it_quotes():
    """YAML quotes a whole key that holds `: `, as the citation chip's does."""
    line = "  - 'link \"Source: example\" [ref=e1] [cursor=pointer]':"
    assert gemini.node(line) == ("link", "Source: example", "")


def test_an_earlier_identical_code_block_does_not_stand_in_for_the_newest():
    history = (
        "Gemini said\ndef f():\n  pass\n\nYou said again\n\n"
        "Gemini said\nPython\ndef f():\n        pass\n\nFlash"
    )
    page = turn(
        "- code [ref=e1]:",
        '  - generic [ref=e2]: "def f():"',
        "  - text: pass",
    )
    assert gemini.reply_block(page, history) == "def f():\n        pass"


def test_a_code_block_whose_first_line_is_indented_keeps_that_indentation():
    history = "Gemini said\nPython\n    indented = True\nflush = False\n\nFlash"
    page = turn("- code [ref=e1]: indented = True flush = False")
    assert gemini.reply_block(page, history) == "    indented = True\nflush = False"


def test_two_code_blocks_with_the_same_characters_each_keep_their_own_indentation():
    """The first block runs `log()` whatever `ready` is; the second only when it is
    true. Handing both the second block's indentation changes what the code does."""
    flat = "if ready: emit() log()"
    page = turn(
        "- paragraph: First:", f"- code: {flat}", "- paragraph: Second:", f"- code: {flat}"
    )
    first = "if ready:\n    emit()\nlog()"
    second = "if ready:\n    emit()\n    log()"
    history = f"Gemini said\nFirst:\nPython\n{first}\nSecond:\nPython\n{second}\n"
    assert gemini.reply_block(page, history) == f"First:\n{first}\nSecond:\n{second}"


def test_a_space_inside_a_code_block_is_never_matched_away():
    page = turn('- code: print("a b")', "- paragraph: Then:", '- code: print("ab")')
    history = 'Gemini said\nprint("a b")\nThen:\nprint("ab")'
    assert gemini.reply_block(page, history) == 'print("a b")\nThen:\nprint("ab")'


def test_a_rendered_block_that_disagrees_on_a_space_is_not_used():
    """Only `print("ab")` is on the page; the snapshot says `print("a b")`. The
    correspondence is not established, so the snapshot's text stands."""
    page = turn('- code: print("a b")')
    assert gemini.reply_block(page, 'Gemini said\nprint("ab")') == 'print("a b")'


def test_rendered_text_with_no_turn_heading_still_settles_the_spacing():
    page = turn("- paragraph:", "  - text: Use", "  - code: len", "  - text: (items).")
    assert gemini.reply_block(page, "Use len(items).") == "Use len(items)."


def test_a_code_block_is_never_read_from_the_prompt():
    """The rendered reply does not hold the block yet; the prompt above it does,
    indented differently. The snapshot's own text is the answer, not the prompt's."""
    page = turn("- code: def run(): work()")
    history = "You said\ndef run():\n        work()\nGemini said\nOutput not rendered yet"
    assert gemini.reply_block(page, history) == "def run(): work()"


def test_a_code_block_is_not_read_from_a_sentence_that_quotes_it():
    page = turn(
        "- paragraph:",
        "  - text: Set",
        "  - code: a = 1 b = 2",
        "  - text: first.",
        "- code: a = 1 b = 2",
    )
    history = "Gemini said\nSet a = 1 b = 2 first.\n\nPython\na = 1\nb = 2\n"
    assert gemini.reply_block(page, history) == "Set a = 1 b = 2 first.\na = 1\nb = 2"


def test_an_empty_table_cell_keeps_its_column():
    page = turn(
        "- table:",
        "  - row:",
        '    - columnheader "Name"',
        '    - columnheader "Amount"',
        '    - columnheader "Status"',
        "  - row:",
        '    - cell "Example"',
        "    - cell",
        '    - cell "Pending"',
    )
    history = "Gemini said\nName\tAmount\tStatus\nExample\t\tPending"
    expected = "Name | Amount | Status\nExample |  | Pending"
    assert gemini.reply_block(page, history) == expected
    assert gemini.reply_block(page) == expected


def test_a_cell_holding_two_paragraphs_is_still_one_column():
    page = turn(
        "- table:",
        "  - row:",
        "    - cell:",
        "      - paragraph: Line one",
        "      - paragraph: Line two",
        '    - cell "Done"',
    )
    assert gemini.reply_block(page) == "Line one / Line two | Done"


def test_a_poll_threads_the_rendered_text_through_to_the_reply(tmp_path):
    seat = ScriptedSeat([transcript(tmp_path, STRUCTURED, STRUCTURED_HISTORY)])
    reading = gemini.read(seat, "ask")
    assert "Call len(x) on a list of strs, then see the docs." in reading.text.splitlines()


def test_the_rendered_text_fallback_keeps_a_sentence_and_a_code_block_whole():
    """No turn headings at all: the reply is what the page rendered after the prompt."""
    history = "ask\n\n" + FORMATTED_HISTORY.split("Gemini said\n", 1)[1]
    reply = gemini.extract_reply("- generic [ref=e1]", history, "ask")
    assert "The relay is active, operating normally via protocol." in reply.splitlines()
    assert "def check_status():\n    return True" in reply
