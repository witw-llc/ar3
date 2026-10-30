"""A value under more than one layer of escapes is scrubbed.

A page can print a value inside a string that is escaped in its turn. `JSON.stringify({user: value})` in an
element, and Playwright quoting that text in a snapshot, make this line of a snapshot for the value
`Synthetic"User\\A7492`:

    - generic [ref=e3]: "{\\"user\\":\\"Synthetic\\\\\\"User\\\\\\\\A7492\\"}"

and a caller decodes it back, YAML and then JSON (round 3, finding 1). One layer is what the pattern of a value
knows (`secretstore._escapes_of`). The others are undone, one family of escapes after another, around each place
where the letters and digits of the value show (`secretstore._deep_spans`).

The proof is a table made by code, with encoders that are written in this file and are not the tool's:

- 18 encoders, which are the ways real code writes a text: JSON and YAML strings (with and without non-ASCII
  characters escaped, in the style of Playwright and of PyYAML, the lines that PyYAML folds in double quotes, in single
  quotes and plain, and YAML in single quotes), URLs
  (and JavaScript's `escape`, which writes `%uXXXX`) (strict, with a plus, as `encodeURIComponent`,
  with lower case hex), markup (the five characters, text only, decimal and hex character references) and CSS;
- each is applied to a value, and the result is put in the syntax of that encoder (a JSON object, a YAML line, a
  query, an element, a selector), which the next encoder escapes in its turn, as a page and a browser do;
- every ordered composition of 1, 2 and 3 encoders, 6,174 of them, over 23 values with quotes, backslashes,
  spaces, controls, non-ASCII text, an emoji, a digit first, a hyphen and a digit first, and values that have no
  letter or digit at all;
- the scrub of each must be the text with exactly the stretch of the value made into its marker, and the value must
  not come back when the text is decoded by decoders that are written here too.

The row of four layers, with one encoder of each kind, passes too. Five do not: the scrub undoes three families
of escapes and its pattern knows the fourth layer. The limits are in the README.
"""
import html as html_module
import itertools
import json
import os
import re
import time
from urllib.parse import quote, quote_plus, unquote, unquote_plus

import pytest

from browser import cli
from browser import commands
from browser import plc
from browser import secretstore
from browser import session
from browser.secretstore import _DECODERS, _Layer, _decodings

from browser.recipe import Recipe, Result

from fake_browser import static_library
from test_commands import FakeCli, ORIGIN, _Completed, _cli_dir, _everything, _with_secret

MARKER = "[secret:USER]"


# --- encoders, written here ------------------------------------------------------------------------------------


def json_ascii(text):
    return json.dumps(text)[1:-1]


def json_utf8(text):
    return json.dumps(text, ensure_ascii=False)[1:-1]


def yaml_playwright(text):
    """What Playwright writes in the double quotes of a snapshot: a backslash and a quote, `\\b \\f \\n \\r \\t`, and `\\xHH`
    for the other C0 characters, DEL and C1."""
    named = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
    out = []
    for char in text:
        if char in named:
            out.append(named[char])
        elif ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F:
            out.append("\\x%02x" % ord(char))
        else:
            out.append(char)
    return "".join(out)


def yaml_pyyaml(text):
    """What PyYAML writes in double quotes with its defaults: every character outside printable ASCII escaped, with the
    letters of YAML for the ones that have one."""
    named = {
        "\0": "0", "\a": "a", "\b": "b", "\t": "t", "\n": "n", "\v": "v", "\f": "f", "\r": "r", "\x1b": "e",
        '"': '"', "\\": "\\", "\x85": "N", "\xa0": "_", "\u2028": "L", "\u2029": "P",
    }
    out = []
    for char in text:
        if char in named:
            out.append("\\" + named[char])
        elif " " <= char <= "~":
            out.append(char)
        elif char <= "\xff":
            out.append("\\x%02X" % ord(char))
        elif char <= "\uffff":
            out.append("\\u%04X" % ord(char))
        else:
            out.append("\\U%08X" % ord(char))
    return "".join(out)


def yaml_single(text):
    """What Playwright writes for the key of a line that needs quotes: single quotes, and an apostrophe doubled."""
    return text.replace("'", "''")


def js_escape(text):
    """JavaScript's `escape`: a letter, a digit and `@*_+-./` as they are, `%XX` for the rest up to U+00FF, and `%uXXXX` for the UTF-16
    units of the others."""
    out = []
    for char in text:
        code = ord(char)
        if char.isascii() and (char.isalnum() or char in "@*_+-./"):
            out.append(char)
        elif code < 0x100:
            out.append("%%%02X" % code)
        else:
            units = char.encode("utf-16-be")
            out.extend("%%u%04X" % int.from_bytes(units[at:at + 2], "big") for at in range(0, len(units), 2))
    return "".join(out)


def yaml_pyyaml_folded(text, width=12, indent=2, column=8):
    """PyYAML's `write_double_quoted` with the folding it does at a narrow width (a port of the emitter of PyYAML 6.0.1): a line
    is broken at a space, or after an escape, with a backslash that ends it, the indent of the next line, and a backslash
    in front of a space that begins it. Returns the text and, for each character of `text`, the stretch of the result that
    stands for it (a character's backslash that protects it from the fold included, a fold itself not). `column` is the column
    of the opening quote."""
    named = {
        "\0": "0", "\a": "a", "\b": "b", "\t": "t", "\n": "n", "\v": "v", "\f": "f", "\r": "r", "\x1b": "e",
        '"': '"', "\\": "\\", "\x85": "N", "\xa0": "_", " ": "L", " ": "P",
    }
    out, spans, state = [], [None] * len(text), {"column": column + 1, "pos": 0, "protect": None}

    def put(data):
        out.append(data)
        state["column"] += len(data)
        state["pos"] += len(data)

    def chunk(first, last):
        protect = state["protect"]
        for index in range(first, last):
            spans[index] = (state["pos"] + (index - first), state["pos"] + (index - first) + 1)
        if protect is not None and first < last:
            spans[first] = (protect, spans[first][1])
            state["protect"] = None
        put(text[first:last])

    start = end = 0
    while end <= len(text):
        char = text[end] if end < len(text) else None
        if char is None or char in '"\\\x85  ﻿' or not "\x20" <= char <= "\x7e":
            if start < end:
                chunk(start, end)
                start = end
            if char is not None:
                data = "\\" + named[char] if char in named else (
                    "\\x%02X" % ord(char) if char <= "\xff" else "\\u%04X" % ord(char) if char <= "￿" else "\\U%08X" % ord(char)
                )
                spans[end] = (state["pos"], state["pos"] + len(data))
                put(data)
                start = end + 1
        if 0 < end < len(text) - 1 and (char == " " or start >= end) and state["column"] + (end - start) > width:
            if start < end:
                chunk(start, end)
                start = end
            put("\\")
            put("\n" + " " * indent)
            state["column"] = indent
            if text[start] == " ":
                state["protect"] = state["pos"]
                put("\\")
        end += 1
    return "".join(out), spans


class Unrepresentable(Exception):
    """A text that a YAML writer does not write in lines: it holds white space next to a line break, which a reader of lines drops,
    or a tab or another control character, or a line break that is none for the reader of the scrub (U+0085, U+2028, U+2029). A
    writer (PyYAML) writes such a text in double quotes with escapes."""


def yaml_lines(text, style, width=12, indent=2, column=8):
    """`text` written as PyYAML writes a scalar in plain style (`style` "plain") or in single quotes ("single"), with the folding it
    does at a narrow width (a port of `write_plain` and `write_single_quoted` of PyYAML 6.0.1, for a value that PyYAML would not choose
    that style for as well: the style is forced). A line is broken at a single space once it is longer than `width`, and the
    indent of the scalar begins the next; a line break of the value is written twice, and the indent follows (the break and
    the empty line that a reader folds back into one). Returns the text and, for each character of `text`, the stretch of the result
    that stands for it: the fold for a space that was broken, the whole run of line breaks and the indent for each of them."""
    if re.search(r"[ \t]\r?\n|\r?\n[ \t]|[\x00-\x09\x0b-\x1f\x7f-\x9f\u2028\u2029\ufeff]", text):
        raise Unrepresentable(text)
    out, spans, state = [], [None] * len(text), {"column": column, "pos": 0}
    quoted = style == "single"

    def put(data):
        out.append(data)
        state["column"] += len(data)
        state["pos"] += len(data)

    def break_and_indent():
        out.append("\n" + " " * indent)
        state["pos"] += 1 + indent
        state["column"] = indent

    spaces = breaks = False
    start = end = 0
    while end <= len(text):
        char = text[end] if end < len(text) else None
        if spaces:
            if char != " ":
                folds = start + 1 == end and state["column"] > width and (not quoted or (start != 0 and end != len(text)))
                if folds:
                    spans[start] = (state["pos"], state["pos"] + 1 + indent)
                    break_and_indent()
                else:
                    for index in range(start, end):
                        spans[index] = (state["pos"] + index - start, state["pos"] + index - start + 1)
                    put(text[start:end])
                start = end
        elif breaks:
            if char is None or char != "\n":
                where = state["pos"]
                data = ("\n" if text[start] == "\n" else "") + "\n" * (end - start) + " " * indent
                for index in range(start, end):
                    spans[index] = (where, where + len(data))
                out.append(data)
                state["pos"] += len(data)
                state["column"] = indent
                start = end
        elif char is None or char in " \n" or (quoted and char == "'"):
            if start < end:
                for index in range(start, end):
                    spans[index] = (state["pos"] + index - start, state["pos"] + index - start + 1)
                put(text[start:end])
                start = end
        if quoted and char == "'":
            spans[end] = (state["pos"], state["pos"] + 2)
            put("''")
            start = end + 1
        if char is not None:
            spaces, breaks = char == " ", char == "\n"
        end += 1
    return "".join(out), spans


def url_strict(text):
    return quote(text, safe="")


def url_plus(text):
    return quote_plus(text, safe="")


def url_component(text):
    """`encodeURIComponent`: it leaves `-_.!~*'()` as they are."""
    return quote(text, safe="-_.!~*'()")


def url_lower(text):
    return re.sub(r"%[0-9A-F]{2}", lambda found: found.group().lower(), quote(text, safe=""))


def html_basic(text):
    return html_module.escape(text)


def html_text(text):
    """What a browser writes for the text of an element: `&`, `<`, `>` and a no-break space."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\xa0", "&nbsp;")


def html_decimal(text):
    return "".join(c if (c.isascii() and (c.isalnum() or c == " ")) else "&#%d;" % ord(c) for c in text)


def html_hex(text):
    return "".join(c if (c.isascii() and (c.isalnum() or c == " ")) else "&#x%X;" % ord(c) for c in text)


def css_escape(text):
    """`CSS.escape`, from the CSSOM: serialize an identifier. (A live test checks the tool's copy against Chrome's.)"""
    out = []
    for index, char in enumerate(text):
        code = ord(char)
        if code == 0:
            out.append("\ufffd")
        elif code <= 0x1F or code == 0x7F or (index == 0 and "0" <= char <= "9") or (
            index == 1 and "0" <= char <= "9" and text[0] == "-"
        ):
            out.append("\\%x " % code)
        elif index == 0 and char == "-" and len(text) == 1:
            out.append("\\-")
        elif code >= 0x80 or char in "-_" or "0" <= char <= "9" or "A" <= char <= "Z" or "a" <= char <= "z":
            out.append(char)
        else:
            out.append("\\" + char)
    return "".join(out)


# The encoder, and the syntax its result is put in: before and after. A JSON string is in an object, a YAML one on a
# line of a snapshot, a URL escape in a query, markup in an element, a CSS escape in a selector.
ENCODERS = {
    "json": (json_ascii, '{"user":"', '"}'),
    "json_utf8": (json_utf8, '{"user":"', '"}'),
    "yaml": (yaml_playwright, '- generic [ref=e3]: "', '"'),
    "yaml_py": (yaml_pyyaml, '- text: "', '"'),
    "yaml_single": (yaml_single, "- '", "' [ref=e5]"),
    "yaml_py_folded": (lambda text: yaml_pyyaml_folded(text)[0], "- text: \"", "\""),
    "yaml_plain_folded": (lambda text: yaml_lines(text, "plain")[0], "- text: ", ""),
    "yaml_single_folded": (lambda text: yaml_lines(text, "single", column=9)[0], "- text: '", "'"),
    "url": (url_strict, "?u=", "&x=1"),
    "url_plus": (url_plus, "?u=", "&x=1"),
    "url_component": (url_component, "?u=", "&x=1"),
    "js_escape": (js_escape, "?q=", "&x=1"),
    "url_lower": (url_lower, "?u=", "&x=1"),
    "html": (html_basic, "<pre>", "</pre>"),
    "html_text": (html_text, "<pre>", "</pre>"),
    "html_decimal": (html_decimal, "<pre>", "</pre>"),
    "html_hex": (html_hex, "<pre>", "</pre>"),
    "css": (css_escape, "#pw-", " > b"),
}
FAMILY = {
    "json": "json", "json_utf8": "json", "yaml": "yaml", "yaml_py": "yaml", "yaml_py_folded": "yaml", "yaml_single": "yaml", "url": "url", "url_plus": "url",
    "url_component": "url", "js_escape": "url", "url_lower": "url", "html": "html", "html_text": "html", "html_decimal": "html",
    "html_hex": "html", "css": "css", "yaml_plain_folded": "yaml", "yaml_single_folded": "yaml",
}
# The encoders that fold a long line, and give back for each character the stretch of the result it stands for. The pattern of a
# value does not read a fold, so a fold is the first of four layers that cannot be undone: three families are, and the pattern reads the fourth.
SPANNED = {
    "yaml_py_folded": yaml_pyyaml_folded,
    "yaml_plain_folded": lambda text: yaml_lines(text, "plain"),
    "yaml_single_folded": lambda text: yaml_lines(text, "single", column=9),
}
ONE_OF_EACH = ["json", "yaml", "yaml_single", "url_plus", "html", "css"]


# --- decoders, written here ------------------------------------------------------------------------------------

_BACKSLASH = re.compile(r"\\(?:u([0-9a-fA-F]{4})|U([0-9a-fA-F]{8})|x([0-9a-fA-F]{2})|(\n[ \t]*)|(.))", re.S)
_LETTERS = {
    "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0", "a": "\a", "e": "\x1b", "N": "\x85",
    "_": "\xa0", "L": "\u2028", "P": "\u2029",
}


def un_string(text):
    def one(found):
        unit, wide, byte, fold, other = found.groups()
        for digits in (unit, wide, byte):
            if digits:
                return chr(int(digits, 16))
        return "" if fold else _LETTERS.get(other, other)

    return _BACKSLASH.sub(one, text).encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def un_css(text):
    return re.sub(
        r"\\(?:([0-9a-fA-F]{1,6}) ?|(.))", lambda found: chr(int(found.group(1), 16)) if found.group(1) else found.group(2),
        text, flags=re.S,
    )


def un_html(text):
    text = re.sub(
        r"&#(?:[xX]([0-9a-fA-F]+)|([0-9]+));?", lambda found: chr(int(found.group(1) or found.group(2), 16 if found.group(1) else 10)),
        text,
    )
    return html_module.unescape(text)


def un_js_escape(text):
    def one(found):
        return chr(int(found.group(1) or found.group(2), 16))

    return re.sub(r"%u([0-9a-fA-F]{4})|%([0-9a-fA-F]{2})", one, text).encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def un_fold(text):
    """A YAML reader's line folding: a line break between two lines is a space, and each empty line after it is a break."""
    return re.sub(
        r"\r?\n(?:[ \t]*\r?\n)*[ \t]*", lambda found: " " if found.group().count("\n") == 1 else "\n" * (found.group().count("\n") - 1),
        text,
    )


INVERSE = {
    "yaml_plain_folded": un_fold, "yaml_single_folded": lambda text: un_fold(text).replace("''", "'"),
    "json": un_string, "json_utf8": un_string, "yaml": un_string, "yaml_py": un_string, "yaml_py_folded": un_string,
    "js_escape": un_js_escape, "yaml_single": lambda t: t.replace("''", "'"), "url": unquote,
    "url_plus": unquote_plus, "url_component": unquote, "url_lower": unquote, "html": un_html, "html_text": un_html,
    "html_decimal": un_html, "html_hex": un_html, "css": un_css,
}

# --- values and texts ------------------------------------------------------------------------------------------

VALUES = [
    'Synthetic"User\\A7492',  # the value of the review
    "p@ss w0rd!#1",
    "caf\u00e9 cr\u00e8me 2026",
    "\u65e5\u672c\u8a9e-pass",
    "emoji \U0001F600 pin 4242",
    "a\\b\\\\c-1234",
    "it's \"quoted\" & <tagged>",
    "2026",  # a digit first: CSS.escape writes it as a hex escape
    "-5x7y",  # a hyphen and a digit: so is the digit
    "tab\there x",
    "a+b=c%d&e~f",
    "x;y:z,w/v?u#t[s]r",
    "pass word  with   spaces",
    "line1\nline2 end",
    "A_b-c.d~e",
    "'lead7492",  # an apostrophe right after the quote of a YAML key
    "trail7492'",  # and right before the closing quote
    "100%41&amp;x",  # a value that looks like escapes
    "\u00e9\u00e8\u00ea\u00eb",  # no letter or digit of ASCII
    "!@#$%^&*()_+",  # nothing but punctuation
    "~\n{?}=\t/\xa0,\U0001F600&(",  # no letter or digit, and a line end that markup leaves as it is
    "c1\x85\x96\x9f-7492",  # control characters of the C1 block: a browser reads their numbers as Windows-1252
    "\u00c3\u00a9-7492 \u00df\u00a0",  # Latin-1 characters that are, as bytes, UTF-8 (`%C3%A9`, `%DF%A0`)
]


def places_of(trace, text):
    """The places of a trace in a text, with the positions of the characters that escapes need."""
    from array import array

    return trace.places(text, array("q", (found.start() for found in secretstore._ESCAPE_HINT.finditer(text))))


def scrubber_for(value, name="USER"):
    return secretstore.Scrubber({name: {"value": value, "origins": [ORIGIN]}})


def build(value, sequence):
    """(text, start, end): `value` under the encoders in order, each result put in its syntax, and the stretch of the
    text that is the value."""
    text, start, end = value, 0, len(value)
    for name in sequence:
        encode, before, after = ENCODERS[name]
        if name in SPANNED:
            whole, spans = SPANNED[name](text)
            low, high = spans[start][0], spans[end - 1][1]
        else:
            low, high = len(encode(text[:start])), len(encode(text[:end]))
            whole = encode(text)
            assert whole.startswith(encode(text[:end])), (name, text, whole)
        text = before + whole + after
        start, end = len(before) + low, len(before) + high
    return text, start, end


def document(value, sequence):
    """(text, expected): a text with the layered value between two lines, and the text with the value made its marker."""
    text, start, end = build(value, sequence)
    shift = len("first line\n")
    text = "first line\n" + text + "\nlast line"
    return text, text[: shift + start] + MARKER + text[shift + end:]


def never_more_than_the_exact_answer_leaves(got, expected):
    """True when `got` is the marker with some of the text before it and some of the text after it that `expected` has:
    the value is gone, and the scrub took at most a neighbour or two with it. A value that ends in a backslash can cost
    the next escape its first character."""
    left, right = expected.split(MARKER, 1)
    kept_left, _marker, kept_right = got.partition(MARKER)
    return got.count(MARKER) == 1 and left.startswith(kept_left) and right.endswith(kept_right)


def inverse_chain(text, sequence):
    for name in reversed(sequence):
        text = INVERSE[name](text)
    return text


def cannot_be_told_from_its_quote(value, sequence):
    """True when the value begins or ends with an apostrophe that a YAML string in single quotes doubles: the quote of the
    string is then the same character, next to it, and the text does not say which of the three is the quote. The
    scrub may take the quote with the value."""
    return ("yaml_single" in sequence or "yaml_single_folded" in sequence) and (value.startswith("'") or value.endswith("'"))


SKIPPED = [0]  # the compositions that `wrong` did not try: a text that no writer writes in lines


def wrong(values, sequences, stop=40):
    """[(value, sequence, text, got, expected)]: the cases in which the scrub is not the exact text, or the value
    comes back when the scrubbed text is decoded the way it was encoded. The search stops at `stop` of them."""
    found = []
    for value in values:
        scrubber = scrubber_for(value)
        for sequence in sequences:
            if len(found) >= stop:
                return found
            try:
                text, expected = document(value, sequence)
            except Unrepresentable:
                SKIPPED[0] += 1
                continue  # no writer writes this text in lines
            got = scrubber.scrub(text)
            if cannot_be_told_from_its_quote(value, sequence):
                exact = never_more_than_the_exact_answer_leaves(got, expected)
            else:
                exact = got == expected
            if not exact or value in inverse_chain(got, sequence) or scrubber.holds(got):
                found.append((value, sequence, text, got, expected))
    return found


def report(cases):
    lines = [f"{len(cases)} cases are wrong (or more: the search stops early); the first:"]
    for value, sequence, text, got, expected in cases[:3]:
        lines += [f"  value {value!r} under {sequence}", f"    text     {text!r}", f"    got      {got!r}", f"    expected {expected!r}"]
    return "\n".join(lines)


# --- the review's own line -----------------------------------------------------------------------------------

REVIEW_VALUE = 'Synthetic"User\\A7492'
REVIEW_LINE = '  - generic [ref=e3]: "{\\"user\\":\\"Synthetic\\\\\\"User\\\\\\\\A7492\\"}"'
REVIEW_SNAPSHOT = (
    "### Page\n- Page URL: http://127.0.0.1:32969/\n### Snapshot\n```yaml\n- generic [ref=e1]:\n"
    "  - textbox [active] [ref=e2]: Synthetic\"User\\A7492\n" + REVIEW_LINE + "\n```\n"
)


def test_the_line_of_the_review_is_what_the_encoders_make():
    text, _start, _end = build(REVIEW_VALUE, ["json", "yaml"])
    assert text == REVIEW_LINE.strip(), "the encoders of this file make the line the review captured"


def test_the_line_of_the_review_is_scrubbed_and_does_not_decode_back():
    scrubber = scrubber_for(REVIEW_VALUE)
    assert scrubber.holds(REVIEW_LINE)
    line = scrubber.scrub(REVIEW_LINE)
    assert line == '  - generic [ref=e3]: "{\\"user\\":\\"[secret:USER]\\"}"'
    recovered = json.loads(json.loads(line.split(": ", 1)[1]))["user"]  # the decoding of the review
    assert recovered == MARKER and recovered != REVIEW_VALUE
    snapshot = scrubber.scrub(REVIEW_SNAPSHOT)
    assert "Synthetic" not in snapshot and "A7492" not in snapshot and snapshot.count(MARKER) == 2


# --- the table ---------------------------------------------------------------------------------------------------


def test_every_ordered_composition_of_one_two_and_three_encoders_is_scrubbed_exactly():
    sequences = [seq for size in (1, 2, 3) for seq in itertools.product(ENCODERS, repeat=size)]
    assert len(sequences) == 18 + 18**2 + 18**3 == 6174
    SKIPPED[0] = 0
    bad = wrong(VALUES, sequences)
    assert not bad, report(bad)
    assert SKIPPED[0] < 0.05 * len(sequences) * len(VALUES), "the table is not hollow: most compositions were tried"


def test_four_layers_with_one_encoder_of_each_kind_are_scrubbed_exactly():
    sequences = [seq for size in (1, 2, 3, 4) for seq in itertools.product(ONE_OF_EACH, repeat=size)]
    bad = wrong(VALUES, sequences)
    assert not bad, report(bad)


def test_the_table_covers_each_family_of_escapes_the_scrub_undoes():
    """A family added to the scrub with no encoder here has no proof."""
    assert len(_DECODERS) == 7 and sum(len(family) for family in secretstore._EXTRA_DECODERS.values()) == 6
    assert set(FAMILY.values()) == {"json", "yaml", "url", "html", "css"}
    assert {name for name in ENCODERS if FAMILY[name] == "url"} >= {"url", "url_plus"}, "percent and plus"


def test_five_layers_are_beyond_what_the_scrub_undoes_and_a_higher_limit_reaches_them(monkeypatch):
    sequence = ("json",) * 5
    text, expected = document(REVIEW_VALUE, sequence)
    assert scrubber_for(REVIEW_VALUE).scrub(text) == text, "five layers of JSON: the fourth decoder is not there"
    monkeypatch.setattr(secretstore, "MAX_LAYERS", 4)
    assert scrubber_for(REVIEW_VALUE).scrub(text) == expected
    monkeypatch.setattr(secretstore, "MAX_LAYERS", 1)
    two, expected_two = document(REVIEW_VALUE, ("json", "yaml", "json"))
    assert scrubber_for(REVIEW_VALUE).scrub(two) == two, "one decoder and the pattern know two layers, not three"


@pytest.mark.parametrize("seed", range(4))
def test_random_values_under_random_layers_are_never_left_in_the_text(seed):
    """The same check with values made at random from the characters that escapes are made for, up to four layers.
    The scrub must never leave more of the text than the exact answer does: a value that ends in a backslash may
    cost a neighbour one."""
    import random

    rng = random.Random(7000 + seed)
    alphabet = list("abcXYZ0129" * 3) + list("\"\\' %&+=;:,/?#[]{}()<>!@$^*_-.~|`") + [
        "\u00e9", "\u00fc", "\u65e5", "\U0001F600", "\t", "\n", "\u00a0", "\u2028", "\a", "\x7f",
        "\u0418", "\u20ac", "\x85", "\u00df", "\u0301", "\u0432",
    ]
    leaks = []
    for _ in range(150):
        value = "".join(rng.choice(alphabet) for _ in range(rng.randint(4, 14)))
        if not any(c.isascii() and c.isalnum() for c in value) and rng.random() < 0.7:
            value += rng.choice(["x", "7", "Zq"])
        scrubber = scrubber_for(value)
        for _ in range(12):
            sequence = tuple(rng.choice(list(ENCODERS)) for _ in range(rng.randint(1, 4)))
            if len(sequence) == 4 and sequence[0] in SPANNED:
                continue  # three layers are undone; the pattern of the value reads the fourth, and it does not read folded lines
            try:
                text, expected = document(value, sequence)
            except (AssertionError, Unrepresentable):
                continue  # the encoder does not give the same prefix for this value: the table is not made for it
            got = scrubber.scrub(text)
            if not never_more_than_the_exact_answer_leaves(got, expected) or scrubber.holds(got):
                leaks.append((value, sequence, text, got, expected))
    assert not leaks, report(leaks)


# --- the stretch each decoded character stands for ---------------------------------------------------------------

STRING, CSS, PERCENT, PLUS, ENTITY, QUOTE_LEFT, QUOTE_RIGHT = _DECODERS
PERCENT_LATIN_1, PLUS_LATIN_1 = secretstore._EXTRA_DECODERS[secretstore._LATIN_1]
(ENTITY_AS_WRITTEN,) = secretstore._EXTRA_DECODERS[secretstore._CONTROLS]


def test_the_decoders_are_the_families_in_the_order_the_tests_use():
    assert [decoder[0] for decoder in _DECODERS] == [("\\",), ("\\",), ("%",), ("+",), ("&",), ("''",), ("''",)]
    assert ENTITY_AS_WRITTEN[1] is ENTITY[1], "two readings of a reference to a control character"
    assert PERCENT_LATIN_1[1] is PERCENT[1] and PLUS_LATIN_1[1] is PLUS[1], "two readings of the bytes of a URL"


def test_a_run_of_percent_escapes_is_decoded_a_character_at_a_time_and_each_knows_its_own_bytes():
    layer = _Layer.whole("a%C3%A9%F0%9F%98%80b%E9z", 100)
    made = layer.decoded(PERCENT)
    assert made.text == "a\u00e9\U0001F600b\u00e9z", "UTF-8 where it is UTF-8, Latin-1 for a byte that is not"
    assert [(made.starts[i], made.ends[i]) for i in range(len(made.text))] == [
        (100, 101), (101, 107), (107, 119), (119, 120), (120, 123), (123, 124),
    ]


def test_a_plus_is_a_space_only_for_the_decoder_that_reads_one():
    layer = _Layer.whole("a+b%2Bc", 0)
    assert layer.decoded(PERCENT).text == "a+b+c"
    assert layer.decoded(PLUS).text == "a b+c"
    assert _Layer.whole("a+b", 0).decoded(PERCENT) is None, "no escape: nothing changed"


def test_two_escapes_of_a_surrogate_pair_are_one_character_that_knows_both():
    made = _Layer.whole('x\\ud83d\\ude00y\\u{1F600}z\\U0001F600w', 10).decoded(STRING)
    assert made.text == "x\U0001F600y\U0001F600z\U0001F600w"
    assert (made.starts[1], made.ends[1]) == (11, 23)
    assert (made.starts[3], made.ends[3]) == (24, 33)
    assert (made.starts[5], made.ends[5]) == (34, 44)


@pytest.mark.parametrize("source, decoded", [
    ('a\\"b\\\\c\\/d', 'a"b\\c/d'),
    ("\\n\\t\\r\\b\\f\\v\\0\\a\\e", "\n\t\r\b\f\v\0\a\x1b"),
    ("\\N\\_\\L\\P", "\x85\xa0\u2028\u2029"),
    ("\\x41\\u00e9\\u{e9}\\U000000E9", "A\u00e9\u00e9\u00e9"),
    ("\\q\\@", "q@"),
    ("a\\\nb", "ab"),
    ("a\\\r\nb", "ab"),
    ("\\u12", "u12"),
])
def test_the_escapes_of_a_string_are_what_json_javascript_yaml_and_python_say(source, decoded):
    made = _Layer.whole(source, 0).decoded(STRING)
    assert made is not None and made.text == decoded


def test_a_backslash_at_the_end_of_a_text_is_no_escape():
    assert _Layer.whole("trailing\\", 0).decoded(STRING) is None
    assert _Layer.whole("trailing\\", 0).decoded(CSS) is None


@pytest.mark.parametrize("source, decoded", [
    ("\\32 026", "2026"),
    ("\\32\t026", "2026"),
    ("\\000032 026", "2026"),
    ("\\3a b", ":b"),
    ("\\@\\.", "@."),
    ("\\\nx", "x"),
    ("\\0 x", "\ufffdx"),
    ("\\110000 x", "\ufffdx"),
])
def test_the_escapes_of_css_are_a_code_point_in_hex_or_the_character_after_the_backslash(source, decoded):
    made = _Layer.whole(source, 0).decoded(CSS)
    assert made is not None and made.text == decoded


def test_a_hex_escape_of_css_stands_for_its_space_too():
    made = _Layer.whole("a\\32 026", 0).decoded(CSS)
    assert made.text == "a2026"
    assert [(made.starts[i], made.ends[i]) for i in range(5)] == [(0, 1), (1, 5), (5, 6), (6, 7), (7, 8)]


@pytest.mark.parametrize("source, decoded", [
    ("&notanentity;", "\u00acanentity;"),  # as a browser reads it: `&not` is a name that needs no semicolon
    ("&amp;&lt;&gt;&quot;&#39;&apos;", "&<>\"''"),
    ("&#x41;&#X41;&#65;&#65", "AAAA"),
    ("&#7;&#127;&#31;", "\x07\x7f\x1f"),
    ("&#150;&#128;", "\u2013\u20ac"),
    ("&#x1F600;", "\U0001F600"),
    ("&#xD800;&#1114112;&#0;", "\ufffd\ufffd\ufffd"),
    ("&nbsp;&copy;", "\xa0\u00a9"),
    ("&amp", "&"),
])
def test_the_character_references_of_markup_keep_a_control_character_that_html_unescape_drops(source, decoded):
    made = _Layer.whole(source, 0).decoded(ENTITY)
    assert made is not None and made.text == decoded


@pytest.mark.parametrize("decoder", [QUOTE_LEFT, QUOTE_RIGHT])
@pytest.mark.parametrize("source, decoded", [("it''s", "it's"), ("''", "'"), ("'''", "''"), ("a''''b", "a''b"), ("'''''", "'''")])
def test_two_apostrophes_are_one_in_a_yaml_string_in_single_quotes(decoder, source, decoded):
    made = _Layer.whole(source, 0).decoded(decoder)
    assert made is not None and made.text == decoded
    assert _Layer.whole("it's", 0).decoded(decoder) is None


def test_a_run_of_three_apostrophes_is_read_from_each_end_and_each_character_knows_its_own():
    """The opening quote is one of the three when the value begins with an apostrophe; the closing quote is one when it
    ends with one. Only the reader from the other end has the pair that is the value's."""
    left, right = _Layer.whole("'''", 10).decoded(QUOTE_LEFT), _Layer.whole("'''", 10).decoded(QUOTE_RIGHT)
    assert [(left.starts[i], left.ends[i]) for i in range(2)] == [(10, 12), (12, 13)]
    assert [(right.starts[i], right.ends[i]) for i in range(2)] == [(10, 11), (11, 13)]


@pytest.mark.parametrize("source", ["a & b", "&;", "&#;", "&zzzzzz;", "AT&T"])
def test_text_that_is_no_character_reference_is_left_as_it_is(source):
    assert _Layer.whole(source, 0).decoded(ENTITY) is None


def test_each_text_is_made_once_with_the_fewest_layers_first():
    layer = _Layer.whole('{\\"a\\":\\"b%20c&amp;d\\"}', 0)
    made = [item.text for item in _decodings(layer)]
    assert len(made) == len(set(made)) and layer.text not in made
    assert made[0] != made[-1]
    assert '{"a":"b c&d"}' in made


# --- the findings of the second reading ----------------------------------------------------------------------------


def test_the_units_of_javascripts_escape_are_characters_and_a_pair_of_them_is_one():
    made = _Layer.whole("a%u65E5%uD83D%uDE00b%E9%u00e9z%C3%A9", 0).decoded(PERCENT)
    assert made.text == "a日\U0001F600béézé"
    assert [(made.starts[i], made.ends[i]) for i in range(len(made.text))] == [
        (0, 1), (1, 7), (7, 19), (19, 20), (20, 23), (23, 29), (29, 30), (30, 36),
    ]
    assert _Layer.whole("%uD83Dx%uDE00", 0).decoded(PERCENT).text == "\ud83dx\ude00", "a lone half of a pair is what it is"


@pytest.mark.parametrize("value", ["café crème 2026", "日本語-pass99", "emoji \U0001F600 pin 4242", "tab x y"])
def test_the_pattern_of_a_value_knows_what_javascripts_escape_makes_of_it_with_no_layer_undone(monkeypatch, value):
    monkeypatch.setattr(secretstore, "MAX_LAYERS", 0)
    scrubber = scrubber_for(value)
    assert scrubber.holds(js_escape(value)), js_escape(value)
    assert scrubber.scrub(f"q={js_escape(value)}&x=1") == f"q={MARKER}&x=1"


@pytest.mark.parametrize("source, decoded", [
    ("a\\\n    b", "ab"),
    ("a\\\r\n  b", "ab"),
    ("a\\\n\tb c", "ab c"),
    ("a\\\n  \\ b", "a b"),
    ("a\\\n", "a"),
])
def test_an_escaped_line_break_of_a_yaml_string_is_nothing_and_so_is_the_indent_after_it(source, decoded):
    made = _Layer.whole(source, 0).decoded(STRING)
    assert made is not None and made.text == decoded


def test_pyyaml_folds_a_long_string_at_a_space_and_the_value_in_it_is_found():
    value = 'Synthetic "User\\A7492 café with some more words to fold'
    folded, spans = yaml_pyyaml_folded(value, width=20)
    assert "\\\n" in folded and folded.count("\n") >= 2, "the control: the writer folded it"
    assert [folded[a:b] for a, b in spans[:3]] == ["S", "y", "n"]
    for text, expected in (
        (f'- text: "{folded}"', f'- text: "{MARKER}"'),
        (json.dumps({"k": f'"{folded}"'}), json.dumps({"k": f'"{MARKER}"'})),
        (f'"{folded}"', f'"{MARKER}"'),
    ):
        assert scrubber_for(value).scrub(text) == expected, text


@pytest.mark.parametrize("value", ["line1\n  line2", "pass  word\t1234", "Ab12  cd34\nef56", "a  b c d 99"])
def test_the_value_with_the_raw_whitespace_of_a_string_folded_by_a_snapshot_is_found_in_layers(value):
    """The page prints JSON, which leaves a space as it is and escapes a tab and a line end; the snapshot shows the text of
    the element with each run of white space one space. What is left is the value with only its raw spaces folded."""
    shown = secretstore._tidied_in_a_string(json_ascii(value)) if False else re.sub(
        "[   ]+", " ", '{"user":"' + json_utf8(value) + '"}'
    )
    line = '- generic [ref=e3]: "' + yaml_playwright(shown) + '"'
    scrubber = scrubber_for(value)
    assert scrubber.scrub(line) == '- generic [ref=e3]: "{\\"user\\":\\"[secret:USER]\\"}"', line
    assert scrubber.scrub(shown) == '{"user":"[secret:USER]"}'


@pytest.mark.parametrize("digits", [10, 11, 4300, 4301, 20_000])
@pytest.mark.parametrize("kind", ["decimal", "hex"])
def test_a_character_reference_with_more_digits_than_a_character_has_is_no_error(digits, kind):
    """`int` of more than 4300 digits raises in Python 3.11 and later, and a page can write them: the scrub of its text
    must not raise, and nothing may leak."""
    reference = "&#" + "9" * digits + ";" if kind == "decimal" else "&#x" + "F" * digits + ";"
    for value in ("482916", "Жук-parol", "!@#$%^&*()_+", "Synthetic\"User\\A7492"):
        scrubber = scrubber_for(value)
        text = f"before {reference} after {value} end"
        assert scrubber.scrub(text) == f"before {reference} after {MARKER} end"
        assert scrubber.holds(text) and not scrubber.holds(f"before {reference} after")
    assert _Layer.whole(reference, 0).decoded(ENTITY).text == "�"


def test_a_file_that_the_scrub_cannot_read_is_removed_when_it_is_asked_and_left_otherwise(tmp_path, monkeypatch):
    scrubber = scrubber_for("482916")
    target = tmp_path / "console.log"
    target.write_text("an 482916 line\n")
    monkeypatch.setattr(secretstore.Scrubber, "scrub", lambda self, text: (_ for _ in ()).throw(MemoryError("a text too big")))
    assert scrubber.scrub_file(str(target)) is False and target.exists()
    assert scrubber.scrub_file(str(target), remove_on_failure=True) is True and not target.exists()


def test_the_sweep_goes_on_over_a_file_with_a_long_character_reference_and_moves_its_mark(monkeypatch, tmp_path):
    _with_secret(monkeypatch, tmp_path)
    page = os.path.join(_cli_dir("seat"), "page-1.yml")
    with open(page, "w") as handle:
        handle.write("- generic: &#" + "9" * 5000 + "; and " + "hunter2-value" + "\n")
    before = session.sweep_mark("seat")
    assert commands.run_script("seat", "url").ok
    assert "hunter2-value" not in open(page).read() and session.sweep_mark("seat") != before


@pytest.mark.parametrize("sequence", [(), ("json", "yaml"), ("url_plus",), ("html", "css", "json")])
def test_two_values_one_ending_where_the_other_begins_are_both_gone_in_every_layer(sequence):
    scrubber = secretstore.Scrubber({
        "A": {"value": "abcd1234", "origins": [ORIGIN]}, "B": {"value": "1234wxyz", "origins": [ORIGIN]},
    })
    text, _start, _end = build("abcd1234wxyz", sequence) if sequence else ("abcd1234wxyz", 0, 12)
    out = scrubber.scrub(text)
    assert "wxyz" not in out and "abcd" not in out and "1234" not in out, out
    assert not scrubber.holds(out)


def test_a_value_that_overlaps_itself_is_one_stretch():
    scrubber = scrubber_for("abababab")
    assert scrubber.scrub("x abababababab y") == f"x {MARKER} y"
    assert scrubber.scrub("x ababababab y") == f"x {MARKER} y"


def test_a_text_with_no_character_that_an_escape_needs_is_not_decoded_at_all(monkeypatch):
    """No escape, no layer: the value in it is as it is, and the pattern of the value finds that. Nothing is searched."""
    calls = []
    real = secretstore._Trace.places
    monkeypatch.setattr(secretstore._Trace, "places", lambda self, text, hints: calls.append(1) or real(self, text, hints))
    scrubber = scrubber_for("a!b@c#d$")
    text = "abc" * 300_000
    started = time.perf_counter()
    assert scrubber.scrub(text) == text and not scrubber.holds(text)
    assert time.perf_counter() - started < 1.0 and calls == []
    assert scrubber.scrub("a!b@c#d$ and a%21b%40c%23d%24") == f"{MARKER} and {MARKER}"
    assert calls, "with an escape in the text the places are looked for"


def test_a_place_with_no_escape_near_it_is_left_alone():
    scrubber = scrubber_for(REVIEW_VALUE)
    far = "Synthetic.User.A7492 " * 50 + "\\n" + " Synthetic.User.A7492" * 50 + " %20"
    assert scrubber.scrub(far) == far


def test_a_weak_value_in_a_text_full_of_escapes_costs_a_bounded_time_and_leaves_none_of_itself():
    scrubber = scrubber_for("a!b@c#d$")
    text = "ab\\c%+& " * 100_000
    started = time.perf_counter()
    out = scrubber.scrub(text)
    assert time.perf_counter() - started < 15.0
    assert not scrubber.holds(out)
    value_in_two_layers = yaml_playwright(json_ascii("a!b@c#d$")) + " " + "%21".join(["a", "b@c#d$"])
    assert MARKER in scrubber.scrub("x \\n " + value_in_two_layers + " y")


# --- where the value is, and how much text is decoded --------------------------------------------------------------


def encoded_through(text, sequence):
    """`text` under the encoders in order, with no syntax put around the result."""
    for name in sequence:
        text = ENCODERS[name][0](text)
    return text


@pytest.mark.parametrize("value, sequence", [
    ("\nZq9", ("css", "json", "css")),
    ("\nZq9", ("css", "yaml_single", "css")),
    ('"Syn7492x', ("json", "yaml")),
    ("bravo-7492", ("css", "json", "url")),  # the first letter is the letter of an escape of a string: `\\b`
    ("bravo-7492", ("url", "html_hex", "css")),
    ("~a\u65e59./", ("css", "css", "json", "url_component")),
])
@pytest.mark.parametrize("alignment", [1024, 64, 8])
@pytest.mark.parametrize("run", [40, 41, 120, 121])
def test_a_window_that_begins_inside_a_run_of_escaped_backslashes_still_finds_the_value(
    monkeypatch, value, sequence, alignment, run,
):
    """The text before the value is a run of backslashes, which the outer layers write as `\\\\`, `%5C` or `&#x5C;` in
    runs of their own. The window of the value begins inside that run, at either parity, and the pairs of the escapes in
    it are not counted from where they began: the first backslash of the run may be the second half of a pair. A stray
    one before the first letter of the value is an escape of it (`\\b`), and the value is not found. A value that begins
    with a letter or a digit is looked for from that letter, and nothing of the text before it is decoded. One that begins
    with a quote or a backslash has the escapes of it in the window: it begins just after white space, so that they are
    counted from the right place, or, when there is none in reach, it is read both ways."""
    monkeypatch.setattr(secretstore, "MAX_ALIGNMENT", alignment)
    before = "\\" * run
    text, start, end = build(before + value, sequence)
    doc = "X" + text + "Y"
    first = 1 + start + len(encoded_through(before, sequence))
    scrubber = scrubber_for(value)
    got = scrubber.scrub(doc)
    assert never_more_than_the_exact_answer_leaves(got, doc[:first] + MARKER + doc[1 + end:]), (value, sequence, alignment, run)
    assert not scrubber.holds(got) and value not in inverse_chain(got, sequence)


def test_a_window_begins_just_after_the_last_line_end_that_is_not_escaped_and_says_when_there_is_none():
    assert secretstore._aligned_start("aa\nbb\\cc dd", 8) == (3, False)
    assert secretstore._aligned_start("aa\r\nbb", 5) == (4, False)
    assert secretstore._aligned_start("aa\nbb", 3) == (3, False), "right after the line end already"
    assert secretstore._aligned_start("anything", 0) == (0, False)
    assert secretstore._aligned_start("x" * 500, 400) == (0, False), "within reach of the start of the text"
    assert secretstore._aligned_start("x" * 3000, 2000) == (2000, True), "no line end in reach: an edge"
    assert secretstore._aligned_start("a\n" + "x" * 3000, 2000) == (2000, True), "a line end beyond the reach is not used"
    assert secretstore._aligned_start("aa bb\tcc dd" + "x" * 2000, 1500) == (1500, True), "white space is no boundary"


def test_a_line_end_that_is_the_fold_of_a_yaml_string_or_a_continuation_is_no_boundary():
    """`\\` and a line end is nothing when it is undone: the text on both sides is one, and a run of backslashes goes across."""
    assert secretstore._aligned_start("a\n" + "x" * 40 + "\\\n  yy", 47) == (2, False), "the fold is passed over, the line end before it is used"
    assert secretstore._aligned_start("ab\\\\\nyy", 6) == (5, False), "two backslashes are one backslash: the line end is a line end"
    assert secretstore._aligned_start("ab\\\\\\\nyy", 7) == (0, False), "three: it is the fold"
    assert secretstore._aligned_start("ab\\\r\nyy", 6) == (0, False), "and so is a fold with a carriage return"


def test_a_text_that_begins_in_the_middle_of_a_run_of_backslashes_is_read_from_the_first_and_from_the_second():
    layer = _Layer.whole("\\\\\\bz", 10)  # three backslashes, b, z
    plain, shifted = layer.decoded(STRING), layer.decoded(STRING, True)
    assert plain.text == "\\\bz", "a pair, and then `\\b`: a backspace"
    assert shifted.text == "\\\\bz", "the first one alone, a pair, and the letter as it is"
    assert [(shifted.starts[i], shifted.ends[i]) for i in range(4)] == [(10, 11), (11, 13), (13, 14), (14, 15)]
    assert layer.decoded(CSS, True).text == "\\\\bz" and _Layer.whole("abc", 0).decoded(STRING, True) is None
    assert [decoder[3] for decoder in _DECODERS] == [True, True] + [False] * 5, "only backslashes"


def test_two_readings_that_make_the_same_text_from_different_stretches_are_both_searched():
    """A run of three apostrophes is the same two characters whichever end the pair is read from, and the characters stand
    for different stretches of the text: only one of them has the stretch that is the value's."""
    made = [item for item in _decodings(_Layer.whole("a'''b", 0)) if item.text == "a''b"]
    assert len(made) == 2
    assert sorted(tuple(zip(item.starts, item.ends)) for item in made) == [
        ((0, 1), (1, 2), (2, 4), (4, 5)),
        ((0, 1), (1, 3), (3, 4), (4, 5)),
    ]


def test_a_value_that_begins_with_a_letter_or_a_digit_is_looked_for_from_it_and_has_no_text_before_it_to_decode():
    for value in ("Synthetic\"User\\A7492", "a-bb-cc-dd", "hunter2!", "x" + "é" * 3):
        trace = secretstore._Trace(value, MARKER)
        assert trace.lead == 0 and places_of(trace, "zzz yyyy no") == []
    text = "head " + yaml_playwright(json_ascii(REVIEW_VALUE)) + " tail"
    (place,) = places_of(secretstore._Trace(REVIEW_VALUE, MARKER), text)
    assert place[3] is True and text[place[0]:place[1]] == "Synthetic\\\\\\\"User\\\\\\\\A7492"


def test_a_value_of_letters_and_digits_alone_is_looked_for_in_layers_only_by_the_css_form_of_a_first_digit():
    """Nothing escapes a letter or a digit but `CSS.escape`, and only a digit at the start of an identifier: a value of
    letters and digits has the same text in every layer. One that begins with a letter has nothing to look for under
    layers (the pattern of the value finds it), and one that begins with a digit is looked for by its CSS form."""
    assert secretstore._trace_texts("Summer2026") == [] and secretstore._trace_texts("abcd") == []
    assert secretstore._trace_texts("2026") == ["\\32 026"] and secretstore._trace_texts("7x7x") == ["\\37 x7x"]
    assert secretstore._trace_texts("a-b-c") == ["a-b-c"] and secretstore._trace_texts("éabc") == ["éabc"]
    assert secretstore._trace_texts("1_000") == ["1_000"], "a digit first, and an underscore: a value with a character of its own"
    assert scrubber_for("Summer2026")._traces == [] and len(scrubber_for("2026")._traces) == 1
    # and what is not found by a trace is found as it is, in every layer there is
    summer = scrubber_for("Summer2026")
    for sequence in itertools.product(ONE_OF_EACH, repeat=2):
        text, expected = document("Summer2026", sequence)
        assert summer.scrub(text) == expected, sequence


def test_a_value_that_begins_with_a_quote_or_a_backslash_has_the_escapes_of_it_in_its_window():
    for value in ('"quoted7492', "\\path7492", "\népass7492", "+2026x"):
        trace = secretstore._Trace(value, MARKER)
        assert trace.lead > 0, value
        text = "head " + yaml_playwright(json_ascii(value)) + " tail"
        (place,) = places_of(trace, text)
        assert place[3] is False and place[0] < text.index(trace.runs[0])


def test_a_window_is_an_edge_unless_it_begins_where_it_cannot_be_inside_an_escape(monkeypatch):
    """A value that begins with its first run is decoded from the run when the character before it is not one that an escape is
    made of: nothing before it is decoded. After a letter, a backslash or an ampersand the run may be in the middle of an
    escape, and the window is read from a line end, or, with none, both ways."""
    seen = []
    real = secretstore._decodings
    monkeypatch.setattr(secretstore, "_decodings", lambda layer, edge=False, *rest: seen.append(edge) or real(layer, edge, *rest))
    monkeypatch.setattr(secretstore, "MAX_ALIGNMENT", 8)  # so that the start of this short text is not in reach
    for value, before, expected in (
        (REVIEW_VALUE, "", False),
        (REVIEW_VALUE, " ", False),
        (REVIEW_VALUE, "=", False),
        (REVIEW_VALUE, "\\" * 500, True),
        (REVIEW_VALUE, "&amp;", True),
        (REVIEW_VALUE, "x", True),
        ('"Syn7492x', "", True),
        ('"Syn7492x', " " * 3000, True),
    ):
        seen.clear()
        text, _start, _end = build(before + value, ["json", "yaml"])
        scrubber_for(value).scrub("z" * 2000 + text)
        assert seen and set(seen) == {expected}, (value, before[:8], seen)
    seen.clear()
    monkeypatch.setattr(secretstore, "MAX_ALIGNMENT", 1024)
    scrubber_for('"Syn7492x').scrub("z" * 100 + "\n" + "q" * 600 + build('"Syn7492x', ["json", "yaml"])[0])
    assert seen and set(seen) == {False}, "after a line end it is aligned"


def test_a_text_with_the_value_all_over_it_is_decoded_in_windows_of_bounded_size(monkeypatch):
    sizes = []
    real = secretstore._Layer.whole.__func__
    monkeypatch.setattr(secretstore._Layer, "whole", classmethod(lambda cls, text, offset: sizes.append(len(text)) or real(cls, text, offset)))
    monkeypatch.setattr(secretstore, "MAX_WINDOW", 20_000)
    one = yaml_playwright(json_ascii(REVIEW_VALUE))
    text = f"[{one}]" * 6000
    assert scrubber_for(REVIEW_VALUE).scrub(text) == f"[{MARKER}]" * 6000
    assert len(sizes) > 5 and max(sizes) <= 2 * 20_000, sizes


def test_undoing_an_escape_is_charged_against_the_work_limit_as_more_than_a_character():
    layer = _Layer.whole("\\n" * 100 + "x" * 50, 0)
    made = layer.decoded(STRING)
    assert made.work == len(layer.text) + secretstore.TOKEN_COST * 100
    assert secretstore.TOKEN_COST >= 4, "undoing an escape costs a call of Python, which is more than a few characters of text"
    assert made.work >= len(layer.text) + 4 * 100
    assert _Layer.whole("plain text", 0).decoded(STRING) is None


def test_a_place_needs_an_escape_near_it_to_be_one():
    trace = secretstore._Trace(REVIEW_VALUE, MARKER)
    far = "Synthetic.User.A7492 " * 50 + "x" * (secretstore.REACH * trace.rest + 100) + " %20"
    assert places_of(trace, far) == [], "the only escape is farther than the reach of the value"
    assert scrubber_for(REVIEW_VALUE).scrub(far) == far
    near = yaml_playwright(json_ascii(REVIEW_VALUE)) + " and a plain Synthetic.User.A7492"
    assert len(places_of(trace, near)) == 1


def test_only_a_window_that_is_an_edge_is_read_from_the_second_backslash_as_well():
    layer = _Layer.whole("\\\\\\bz", 0)
    plain = [made.text for made in _decodings(layer)]
    edge = [made.text for made in _decodings(layer, True)]
    assert set(plain) < set(edge) and "\\\\bz" in edge and "\\\\bz" not in plain


def test_a_value_at_the_very_start_and_the_very_end_of_a_text_is_scrubbed():
    scrubber = scrubber_for(REVIEW_VALUE)
    layered = json_ascii(REVIEW_VALUE)
    assert scrubber.scrub(layered) == MARKER
    assert scrubber.scrub("x" + layered) == "x" + MARKER
    assert scrubber.scrub(layered + "x") == MARKER + "x"
    doubled = yaml_playwright(json_ascii(REVIEW_VALUE))
    assert scrubber.scrub(doubled) == MARKER


def test_the_value_raw_and_in_two_layers_in_one_text_is_scrubbed_everywhere():
    scrubber = scrubber_for(REVIEW_VALUE)
    text = f"{REVIEW_VALUE} then {REVIEW_LINE} then {json_ascii(REVIEW_VALUE)} then {REVIEW_VALUE}"
    out = scrubber.scrub(text)
    assert out.count(MARKER) == 4
    assert "Synthetic" not in out and "A7492" not in out


def test_values_next_to_each_other_each_get_a_marker_and_do_not_swallow_the_text_between():
    scrubber = scrubber_for(REVIEW_VALUE)
    one = yaml_playwright(json_ascii(REVIEW_VALUE))
    assert scrubber.scrub(f"{one}-{one}") == f"{MARKER}-{MARKER}"
    assert scrubber.scrub(f"{one}{one}") == f"{MARKER}{MARKER}"


def test_a_value_that_begins_with_the_whole_of_another_is_the_longer_one_in_every_layer():
    scrubber = secretstore.Scrubber({
        "SHORT": {"value": 'abcd1234"x', "origins": [ORIGIN]},
        "LONG": {"value": 'abcd1234"x-extra\\y', "origins": [ORIGIN]},
    })
    long_text, _start, _end = build('abcd1234"x-extra\\y', ["json", "yaml"])
    assert scrubber.scrub(long_text) == '- generic [ref=e3]: "{\\"user\\":\\"[secret:LONG]\\"}"'
    short_text, _start, _end = build('abcd1234"x', ["json", "yaml"])
    assert scrubber.scrub(short_text) == '- generic [ref=e3]: "{\\"user\\":\\"[secret:SHORT]\\"}"'


def test_a_value_that_is_part_of_the_encoding_of_a_longer_value_does_not_hide_the_longer_one():
    """The short value is raw inside the layered text of the long one: the long one is found, and none of it stays."""
    scrubber = secretstore.Scrubber({
        "SHORT": {"value": "abcd1234", "origins": [ORIGIN]},
        "LONG": {"value": "abcd1234 efgh", "origins": [ORIGIN]},
    })
    text, start, end = build("abcd1234 efgh", ["url_plus", "url", "json"])
    out = scrubber.scrub(f"<{text}>")
    assert "efgh" not in out and "abcd1234" not in out and "[secret:" in out


def test_the_value_with_its_whitespace_tidied_by_a_snapshot_is_found_under_layers_too():
    """Playwright shows the text of an element with each run of whitespace one space; the page printed JSON."""
    value = "pass  word   1234"
    scrubber = scrubber_for(value)
    tidied = json_ascii(value).replace("  ", " ").replace("  ", " ")
    line = '- generic [ref=e3]: "' + yaml_playwright('{"user":"' + tidied + '"}') + '"'
    assert scrubber.scrub(line) == '- generic [ref=e3]: "{\\"user\\":\\"[secret:USER]\\"}"'


def test_a_value_that_a_script_hid_is_found_under_layers_from_then_on():
    scrubber = secretstore.Scrubber({})
    scrubber.hide("PASSCODE", 'Synthetic"User\\A7492')
    assert scrubber.scrub(REVIEW_LINE) == '  - generic [ref=e3]: "{\\"user\\":\\"[secret:PASSCODE]\\"}"'
    assert scrubber.holds(REVIEW_LINE)


def test_a_layered_stretch_that_overlaps_a_plain_match_takes_the_whole_of_both(monkeypatch):
    """A value found one way may overlap a value found the other way, and what is left of the first must not stay."""
    scrubber = secretstore.Scrubber({"A": {"value": "abcd1234", "origins": [ORIGIN]}})
    monkeypatch.setattr(
        scrubber, "_deep_spans", lambda text, first=False: [(text.index("1234"), text.index("1234") + 8, "[secret:B]")]
    )
    assert scrubber.scrub("abcd1234wxyz!") == "[secret:A]!"
    assert scrubber.scrub("!abcd1234wxyz!") == "![secret:A]!"


def test_a_text_holds_a_value_under_layers_for_the_gate_on_a_command_line():
    scrubber = scrubber_for(REVIEW_VALUE)
    for sequence in itertools.product(ONE_OF_EACH, repeat=2):
        text, _expected = document(REVIEW_VALUE, sequence)
        assert scrubber.holds(text), sequence
    assert not scrubber.holds("Synthetic and User and A7492, with \\\"quotes\\\" and %20 and &amp;")
    assert not scrubber.holds(json_ascii(REVIEW_VALUE)[:-3])


def test_a_text_with_the_words_of_the_value_and_no_value_is_left_alone():
    scrubber = scrubber_for(REVIEW_VALUE)
    text = 'Synthetic\\"User\\\\A7493 and Synthetic "User" A7492 and \\"Synthetic\\\\\\"User\\\\\\\\A749\\"'
    assert scrubber.scrub(text) == text


def test_a_value_with_only_punctuation_or_only_non_ascii_text_is_found_in_the_lines_that_have_an_escape():
    for value in ("!@#$%^&*()_+", "\u65e5\u672c\u8a9e\u30d1\u30b9\u30ef\u30fc\u30c9"):
        scrubber = scrubber_for(value)
        text, expected = document(value, ("json", "yaml"))
        assert scrubber.scrub(text + "\nunrelated \\n line") == expected + "\nunrelated \\n line"


# --- the work limit ----------------------------------------------------------------------------------------------


def test_text_that_costs_more_than_the_limit_to_decode_is_scrubbed_whole_from_the_first_run_to_the_last(monkeypatch):
    """Copies of a value that are far enough apart (here 800 characters, and a run can be 256 from the one before)
    are each scrubbed to the character, because a value that begins and ends with a letter or a digit is its own core."""
    scrubber = scrubber_for(REVIEW_VALUE)
    filler = "x" * 800
    lines = [f"line {number} {yaml_playwright(json_ascii(REVIEW_VALUE))} {filler}" for number in range(40)]
    text = "\n".join(lines)
    exact = "\n".join(f"line {n} {MARKER} {filler}" for n in range(40))
    assert scrubber.scrub(text) == exact
    monkeypatch.setattr(secretstore, "WORK_LIMIT", 3000)
    assert scrubber.scrub(text) == exact


def test_copies_close_together_are_scrubbed_as_one_stretch_when_the_limit_is_hit_and_none_stays(monkeypatch):
    scrubber = scrubber_for(REVIEW_VALUE)
    text = "\n".join(f"line {n} {yaml_playwright(json_ascii(REVIEW_VALUE))} end" for n in range(300))
    monkeypatch.setattr(secretstore, "WORK_LIMIT", 3000)
    out = scrubber.scrub(text)
    assert "Synthetic" not in out and "A7492" not in out and out.startswith("line 0 " + MARKER)
    assert not scrubber.holds(out)


def test_a_value_that_begins_with_punctuation_is_scrubbed_with_what_it_can_take_around_it_when_the_limit_is_hit(monkeypatch):
    value = '"pass1234"'
    scrubber = scrubber_for(value)
    text = "\n".join(f"line {n} " + yaml_playwright(json_ascii(value)) + " end" for n in range(100))
    monkeypatch.setattr(secretstore, "WORK_LIMIT", 2000)
    out = scrubber.scrub(text)
    assert "pass1234" not in out and MARKER in out
    assert not scrubber.holds(out)


def test_a_value_with_no_letter_or_digit_is_scrubbed_around_each_escape_when_the_limit_is_hit(monkeypatch):
    scrubber = scrubber_for("!@#$%^&*()_+")
    text = "\n".join(f"line {n} {yaml_playwright(json_ascii('!@#$%^&*()_+'))}" if n % 10 == 0 else f"line {n} a \\n b" for n in range(100))
    monkeypatch.setattr(secretstore, "WORK_LIMIT", 500)
    out = scrubber.scrub(text)
    assert MARKER in out and not scrubber.holds(out) and "!@#$" not in out


def test_the_small_windows_are_decoded_before_the_large_ones_when_the_limit_is_short(monkeypatch):
    """A value that begins with punctuation has, past the limit, what it can take around it scrubbed with it: 512 characters
    for each character before it. A large region of copies at the start of the text would use the limit for itself, if the
    windows were decoded in the order of the text, and the sixty small copies after it would be scrubbed that way. The small
    windows go first."""
    value = '"pass1234"'
    layered = yaml_playwright(json_ascii(value))
    scrubber = scrubber_for(value)
    dense = "".join(f"[{layered}] {number} " for number in range(1500))  # copies close together: windows of 64 KB
    far = "\n" + "z" * 3000 + "\n"
    small = "words before " + layered + " words after"
    text = dense + "".join(far + small for _ in range(60))
    monkeypatch.setattr(secretstore, "WORK_LIMIT", 1_500_000)
    out = scrubber.scrub(text)
    exact = out.count("words before " + MARKER + " words after")
    assert exact >= 50, exact  # in the order of the text, 22 of them
    assert "pass1234" not in out and not scrubber.holds(out)
    monkeypatch.setattr(secretstore, "WORK_LIMIT", 10**12)
    assert scrubber.scrub(text).count("words before " + MARKER + " words after") == 60


def test_a_window_that_the_limit_was_too_short_for_does_not_make_the_window_after_it_too_short(monkeypatch):
    """The window of the first copy is full of matches that stand for nothing (`&a`), and the limit is spent in it. What is
    left is enough for the window of the second copy, which is decoded, and the text around its copy stays."""
    value = '"pass1234"'
    layered = yaml_playwright(json_ascii(value))
    scrubber = scrubber_for(value)
    first = "first words " + "&a" * 400 + layered + "&a" * 400 + " last words of b"
    second = "c" * 1500 + layered + "c" * 1500
    text = first + "\n" + "z" * 30_000 + "\n" + second
    for limit in (60_000, 80_000):
        monkeypatch.setattr(secretstore, "WORK_LIMIT", limit)
        out = scrubber.scrub(text)
        around = "c" * 1400 in out
        assert around and "pass1234" not in out and not scrubber.holds(out), limit
        assert out.count("&a") < 800, "the window that was not decoded had its copy scrubbed with what it can take around it"


def test_the_default_limit_is_a_few_seconds_of_work_at_most():
    assert 1_000_000 <= secretstore.WORK_LIMIT <= 80_000_000


# --- the lines of YAML ----------------------------------------------------------------------------------------------


def test_the_ports_of_the_yaml_writers_are_what_pyyaml_writes():
    """The encoders of this file that fold are ports of the emitter of PyYAML 6.0.1. Where PyYAML is installed (the venv of
    the suite has none), they are compared with it, at four widths, over random values."""
    import random

    yaml = pytest.importorskip("yaml")
    rng = random.Random(11)
    words = ["alpha", "be", "gamma-7", "d", "epsilon9", "zeta", "x"]
    alphabet = list("abcdef ghij klm") * 3 + list("\"\\'\t\n") + ["\u00e9", "\u65e5", "\U0001F600", "\xa0", "\u2028", "\x00", "\x7f", "\x1b"]
    for width in (8, 12, 20, 80):
        for _ in range(150):
            value = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 50)))
            written = yaml.dump({"text": value}, default_style='"', width=width)
            assert written.startswith('"text": "')
            assert yaml_pyyaml_folded(value, width=width, column=8)[0] + '"' == written[len('"text": "'):].rstrip("\n"), (width, value)
            plain = " ".join(rng.choice(words) for _ in range(rng.randint(1, 14)))
            for style, key, mine in (("'", "'text': ", "single"), (None, "text: ", "plain")):
                written = yaml.dump({"text": plain}, default_style=style, width=width)
                if style is None and written[len(key):].startswith(("'", '"')):
                    continue
                inner = written[len(key):].rstrip("\n")
                inner = inner[1:-1] if style else inner
                assert yaml_lines(plain, mine, width=width, column=len(key) + (1 if style else 0))[0] == inner, (mine, width, plain)
            quoted = "".join(rng.choice(["it's", " ", "a", "\n", "b", "''"]) for _ in range(rng.randint(1, 20))).strip(" \n") or "w"
            if not re.search(r"[ \t]\r?\n|\r?\n[ \t]|\r", quoted):
                written = yaml.dump({"text": quoted}, default_style="'", width=width)
                assert yaml_lines(quoted, "single", width=width, column=len("'text': '"))[0] == written[len("'text': '"):].rstrip("\n")[:-1], (width, quoted)


@pytest.mark.parametrize("style", ["plain", "single"])
def test_a_value_that_a_yaml_writer_broke_at_a_space_is_scrubbed_wherever_the_break_falls(style):
    """PyYAML breaks a long line of a string in plain style, or in single quotes, at a space, and a reader folds the break and
    the indent back into the space (the second review). The value has three spaces, and the break falls in each of them as the
    text before it gets longer, and in none, and in all of them at a narrow width."""
    value = "correct horse battery staple"
    scrubber = scrubber_for(value)
    inside = set()
    for before in range(1, 40):
        for width in (4, 8, 12, 30):
            prefix = "x" * before + " "
            text = prefix + value + " and then more words"
            whole, spans = yaml_lines(text, style, width=width)
            low, high = spans[len(prefix)][0], spans[len(prefix) + len(value) - 1][1]
            inside.add(whole[low:high].count("\n"))
            assert scrubber.scrub(whole) == whole[:low] + MARKER + whole[high:], (style, before, width, whole)
            assert scrubber.holds(whole)
    assert {0, 1, 3} <= inside, inside


def test_the_text_of_the_review_that_pyyaml_folded_is_scrubbed():
    value = "correct horse battery staple"
    scrubber = scrubber_for(value)
    plain = "note: " + "x" * 60 + " correct horse battery\n  staple and then more words\n"
    single = "note: 'Notes: " + "y" * 60 + " correct\n  horse battery staple'\n"
    assert scrubber.scrub(plain) == "note: " + "x" * 60 + f" {MARKER} and then more words\n"
    assert scrubber.scrub(single) == "note: 'Notes: " + "y" * 60 + f" {MARKER}'\n"
    assert scrubber.holds(plain) and scrubber.holds(single)
    assert not scrubber.holds("note: correct horse\n  battery and then staple\n")


def test_empty_lines_and_tabs_in_the_indent_of_a_fold_are_read_as_a_yaml_reader_reads_them():
    value = "alpha beta\ngamma delta"
    scrubber = scrubber_for(value)
    for text, expected in (
        ("k: 'alpha\n  beta\n\n  gamma\n  delta'", f"k: '{MARKER}'"),
        ("k: alpha\n\tbeta\n\n\tgamma delta", f"k: {MARKER}"),
        ("k: alpha\r\n  beta\r\n\r\n  gamma\r\n  delta", f"k: {MARKER}"),
        ("k: 'alpha beta-gamma delta'", "k: 'alpha beta-gamma delta'"),
        ("k: 'alpha beta\n\n\n\n\n\n\n  gamma delta'", "k: 'alpha beta\n\n\n\n\n\n\n  gamma delta'"),  # six empty lines: more folds than layers
    ):
        assert scrubber.scrub(text) == expected, text


@pytest.mark.parametrize("indent", [2, 4, 8])
def test_a_value_with_lines_in_a_yaml_block_is_scrubbed_with_the_indent_of_the_lines_inside_it(indent):
    """A block (`|-`) keeps each line break and indents each line. The value is a key of the kind a person stores."""
    value = "-----BEGIN KEY-----\nMIIBabcDEF123456\nxyz789QRS-----END KEY-----"
    scrubber = scrubber_for(value)
    body = "\n".join(" " * indent + line for line in value.split("\n"))
    block = "key: |-\n" + body
    start = len("key: |-\n") + indent
    assert scrubber.scrub(block + "\nnext: 1\n") == block[:start] + MARKER + "\nnext: 1\n"
    # the block inside a string that JSON writes, and inside one that a snapshot writes in its turn
    for encode in (json_ascii, lambda text: yaml_playwright(json_ascii(text))):
        low, high = len(encode(block[:start])), len(encode(block))
        text = "<" + encode(block) + ">"
        assert scrubber.scrub(text) == "<" + encode(block)[:low] + MARKER + ">", encode
    assert not scrubber.holds("key: |-\n  -----BEGIN KEY-----\n  MIIBabcDEF123456\n  something else\n")


@pytest.mark.parametrize("value", [
    'line1\n  indented-7492',
    '{\n  "key": "Zq9-secret-7492"\n}',
    "Host db7\n  User  admin-7492\n    deeper",
    "a1\n\n  b2-7492",
])
@pytest.mark.parametrize("indent", [2, 6])
def test_a_value_whose_own_lines_begin_with_spaces_is_found_in_a_block_that_indents_every_line(value, indent):
    """The lines of a `|` block have the indent of the block before the spaces of the value, whatever the indent is; PyYAML
    writes a block like this for a value with such lines when it is asked to (`default_style="|"`)."""
    scrubber = scrubber_for(value)
    body = "\n".join(" " * indent + line if line else "" for line in value.split("\n"))
    block = "key: |-\n" + body
    start = len("key: |-\n") + indent
    assert scrubber.scrub(block + "\nnext: 1\n") == block[:start] + MARKER + "\nnext: 1\n"
    snapshot = '- generic [ref=e3]: "' + yaml_playwright(block) + '"'
    assert scrubber.scrub(snapshot) == '- generic [ref=e3]: "' + yaml_playwright(block[:start]) + MARKER + '"'
    assert scrubber.holds(block) and scrubber.holds(snapshot)


def test_a_value_with_a_space_is_looked_for_around_the_line_ends_of_a_text_and_one_without_is_not(monkeypatch):
    seen = []
    real = secretstore._Trace.places
    monkeypatch.setattr(secretstore._Trace, "places", lambda self, text, hints: seen.append(len(hints)) or real(self, text, hints))
    text = "line one\nline two\n" * 100 + "Synthetic words 7492"  # no character that an escape is made of
    assert scrubber_for('Synthetic"7492').scrub(text) == text
    assert seen == [], "no escape, and a value that a fold cannot be in: nothing is looked for"
    assert scrubber_for("two words 7492").scrub(text) == text
    assert seen and seen[0] == 200, "the line ends are where a space can have been"


def test_a_value_that_css_escapes_with_a_hex_number_can_be_folded_at_the_space_that_ends_it():
    """`CSS.escape("-5x7y")` is `-\\35 x7y`, and a YAML writer that folds the line at that space breaks it in two."""
    for value, lines in (("-5x7y", True), ("2026", True), ("a\tb-7492", True), ("plain\u00e9-7492", False), ("two words-7492", True)):
        assert bool(secretstore._Trace(value, MARKER).extras & secretstore._FOLDING) is lines, value
    assert secretstore._Trace("a\nb-7492", MARKER).extras == secretstore._FOLDING
    for style in ("plain", "single"):
        for width in (4, 8, 12):
            text, expected = document("-5x7y", ("css", "yaml_plain_folded" if style == "plain" else "yaml_single_folded"))
            assert scrubber_for("-5x7y").scrub(text) == expected


def test_two_values_that_overlap_are_both_scrubbed_under_layers_and_so_is_one_that_overlaps_itself():
    """`finditer` of the pattern of the values goes on after a match, so the start of the second value was lost: what
    stayed was the end of it, after the end of the first."""
    both = secretstore.Scrubber({"A": {"value": 'ab"cd1234', "origins": [ORIGIN]}, "B": {"value": '1234wx"yz', "origins": [ORIGIN]}})
    inner = json.dumps({"u": 'ab"cd1234wx"yz'})
    line = '- generic [ref=e3]: "' + yaml_playwright(inner) + '"'
    assert both.scrub(line) == '- generic [ref=e3]: "{\\"u\\": \\"[secret:A]\\"}"'
    assert both.scrub(inner) == '{"u": "[secret:A]"}'
    assert both.holds(line) and not both.holds(both.scrub(line))
    flipped = secretstore.Scrubber({"A": {"value": '1234wx"yz', "origins": [ORIGIN]}, "B": {"value": 'ab"cd1234', "origins": [ORIGIN]}})
    out = flipped.scrub(line)
    assert "wx" not in out and "ab" not in out.replace("[secret", "") and out.count("[secret:") == 1
    twice = scrubber_for('q"z!q"z')
    three = yaml_playwright(json.dumps({"u": 'q"z!q"z!q"z'}))
    assert twice.scrub("- text: \"" + three + "\"") == '- text: "{\\"u\\": \\"[secret:USER]\\"}"'
    assert twice.scrub("q\"z!q\"z!q\"z") == MARKER


# --- the evidence of a character over U+007F --------------------------------------------------------------------------

EVIDENCE = {
    "\u0418": [
        "\u0418", "\\u0418", "\\u{418}", "\\U00000418", "&#1048;", "&#x418;", "&Icy;", "\\418 ", "%D0%98", "%d0%98", "%u0418",
        "%25D0%2598", "%255Cu0418", "&#38;#1048;", "\\\\u0418",
    ],
    "\U0001F600": [
        "\U0001F600", "\\ud83d\\ude00", "\\ud83D\\uDE00", "\\u{1F600}", "\\U0001F600", "&#128512;", "&#x1F600;", "\\1f600 ",
        "%F0%9F%98%80", "%uD83D%uDE00",
    ],
    "\u20ac": ["\u20ac", "&#128;", "&#x80;", "&#8364;", "&euro;", "\\u20AC", "%E2%82%AC", "%u20AC"],
    "\u00e9": ["\u00e9", "&eacute;", "\\xe9", "\\u00E9", "%E9", "%C3%A9", "&#233;", "&#xE9;", "\\e9 "],
}


@pytest.mark.parametrize("char", list(EVIDENCE))
def test_every_escape_of_a_character_over_ascii_has_the_evidence_of_it_in_it(char):
    evidence = secretstore._evidence(char)
    assert evidence.strength >= 2
    for form in EVIDENCE[char]:
        assert evidence.starts(form), (char, form)
    assert evidence.starts("plain words 12 34 and 56 with d0 and 99 apart") == []
    assert evidence.starts("%D0 ... %42") == [], "one byte of two"


def test_the_first_and_the_last_byte_of_a_character_are_put_together_by_their_places():
    evidence = secretstore._evidence("\u0418")  # %D0%98
    assert evidence.starts("%D0y%98") == [1] and evidence.starts("%D0y%98 %d0%98") == [1, 9]
    assert evidence.starts("x" * 2000 + "%D0" + "y" * 400 + "%98") == [], "the last byte is farther away than the bytes between can make it"
    assert evidence.starts("x" * 2000 + "%D0" + "y" * 290 + "%98") != []
    assert evidence.starts("%98 %D0") == [], "the last byte is before the first"
    emoji = secretstore._evidence("\U0001F600")  # f0 9f 98 80: the gap is that of three bytes between
    assert emoji.starts("%F0%9F%98%80") == [1]
    assert emoji.starts("%F0" + "y" * 800 + "%80") != [] and emoji.starts("%F0" + "y" * 900 + "%80") == []
    assert emoji.starts("f0" * 2000 + "80") != [] and emoji.starts("f0" * 2000) == []


def test_a_character_that_a_letter_or_no_code_stands_for_has_no_evidence_and_is_not_looked_for():
    """`\\L` is U+2028 in YAML and `\\_` a no-break space: the text of the escape has no number in it. The same for the character
    that a number that is none stands for."""
    for char in ("\u2028", "\u2029", "\u00a0", "\x85", "\ufffd", "a", "!", "7", " ", "\ud800"):
        assert secretstore._evidence(char) is None, repr(char)
    assert secretstore._chain_of("wide\u2028line-7492") == []
    value = "wide\u2028line-7492"
    for sequence in (("yaml_py",), ("yaml_py", "json"), ("yaml_py", "url")):
        text, expected = document(value, sequence)
        assert scrubber_for(value).scrub(text) == expected, sequence
    assert scrubber_for("no\u00a0break-7492").scrub('- text: "no\\_break-7492"') == f'- text: "{MARKER}"'


def test_the_chain_of_a_value_has_its_characters_in_the_order_they_are_in_and_the_gaps_they_can_have():
    chain = secretstore._chain_of("\u0418\u0432\u0430\u043d \u041f\u0435\u0442\u0440\u043e\u0432")
    assert len(chain) == secretstore.MAX_SIGNATURES == 6
    assert [gap for gap, _pattern, _index in chain] == [0] + [secretstore.REACH * step for step in (2, 2, 2, 3, 2)]
    assert [index for _gap, _pattern, index in chain] == [0, 1, 2, 3, 5, 6]
    assert secretstore._chain_of("only ascii 1234") == [] and secretstore._chain_of("") == []
    strongest = secretstore._chain_of("\u00e9\u00e8abc\U0001F600\u65e5x")  # the longest numbers: the emoji, the kanji, then the rest
    assert len(strongest) == 4 and strongest[0][0] == 0


def test_a_reference_to_a_control_character_is_the_character_of_windows_1252_and_is_found():
    """A browser reads `&#128;` as the euro sign. The number in the text is not the code of the character, so the evidence of
    the euro sign has 128 and 80 in it."""
    scrubber = scrubber_for("\u20acuro-pass-1234")
    for text in ("<p>&#128;uro-pass-1234</p>", "<p>&#x80;uro-pass-1234</p>", "<p>&#0128;uro&#45;pass-1234</p>"):
        assert scrubber.scrub(text) == f"<p>{MARKER}</p>", text
    assert scrubber.scrub("<p>&#129;uro-pass-1234</p>") == "<p>&#129;uro-pass-1234</p>", "the next control character is no euro sign"


def test_a_reference_to_a_control_character_is_read_as_a_browser_reads_it_and_as_the_code_that_wrote_it_meant_it():
    """`&#133;` is an ellipsis to a browser (Windows-1252), and U+0085 to a writer that escapes every character above U+007E by
    its number. The first is how the DOM has it, the second what such a page prints, and the scrub undoes both."""
    assert _Layer.whole("a&#133;b&#x85;c&#60;", 0).decoded(ENTITY).text == "a\u2026b\u2026c<"
    made = _Layer.whole("a&#133;b&#x85;c&#60;&amp;", 0).decoded(ENTITY_AS_WRITTEN)
    assert made.text == "a\x85b\x85c<&", "every reference of the text is read the same way"
    assert [(made.starts[i], made.ends[i]) for i in range(3)] == [(0, 1), (1, 7), (7, 8)]
    assert _Layer.whole("a&#60;b&#x2026;c&#1330;", 0).decoded(ENTITY_AS_WRITTEN) is None, "no control character: the first reading is the only one"
    assert _Layer.whole("a&#1330;&#x185;&#0000133;", 0).decoded(ENTITY_AS_WRITTEN).text == "a\u0532\u0185\x85"
    for value in ("ctl\x85\x96end-7492", "\x85\x86\x87\x88-7492"):
        scrubber = scrubber_for(value)
        for sequence in (("html_decimal",), ("html", "html_decimal"), ("html_hex", "url"), ("json", "html_hex"), ("css", "html_decimal", "url_lower")):
            text, expected = document(value, sequence)
            assert scrubber.scrub(text) == expected, (value, sequence)


def test_the_bytes_of_a_url_are_read_as_utf_8_and_as_latin_1_and_a_value_is_found_either_way():
    """`escape("\u00df\u00a0")` is `%DF%A0`, which is also the UTF-8 of U+07E0. A page in ISO-8859-1 writes its form the same
    way, with a plus for a space. Which it is, only the writer knows, so the scrub reads both."""
    assert _Layer.whole("a%DF%A0b%C3%A9", 0).decoded(PERCENT).text == "a\u07e0b\u00e9"
    made = _Layer.whole("a%DF%A0b%C3%A9", 100).decoded(PERCENT_LATIN_1)
    assert made.text == "a\u00df\u00a0b\u00c3\u00a9"
    assert [(made.starts[i], made.ends[i]) for i in range(6)] == [(100, 101), (101, 104), (104, 107), (107, 108), (108, 111), (111, 114)]
    assert _Layer.whole("a%20b%7E", 0).decoded(PERCENT_LATIN_1) is None, "no byte over 127: the first reading is the only one"
    assert _Layer.whole("a+b%DF", 0).decoded(PLUS_LATIN_1).text == "a b\u00df"
    assert _Layer.whole("a+b%20", 0).decoded(PLUS_LATIN_1) is None
    for value in ("\u00df\u00a0-7492", "x\u00c3\u00a9y-7492", "\u00ea\u00a9\u00a9-pass7"):
        scrubber = scrubber_for(value)
        for sequence in (("js_escape",), ("js_escape", "json"), ("js_escape", "html_decimal"), ("json", "js_escape", "url_plus")):
            text, expected = document(value, sequence)
            assert scrubber.scrub(text) == expected, (value, sequence)
    plus = scrubber_for("m\u00fcnchen \u00df-pass7")
    assert plus.scrub("q=m%FCnchen+%DF-pass7&x=1") == f"q={MARKER}&x=1"
    assert plus.scrub("q=m%C3%BCnchen+%C3%9F-pass7&x=1") == f"q={MARKER}&x=1"


def test_the_extra_families_are_for_the_values_that_need_them_and_for_no_other(monkeypatch):
    folding, latin_1, controls = secretstore._FOLDING, secretstore._LATIN_1, secretstore._CONTROLS
    assert secretstore._extras_of("plain-7492") == 0 and secretstore._extras_of("\u0418\u043d-7492") == 0
    assert secretstore._extras_of("two words-7492") == folding and secretstore._extras_of("-5x7y") == folding
    assert secretstore._extras_of("a\nb-7492") == folding
    assert secretstore._extras_of("caf\u00e9-7492") == latin_1 and secretstore._extras_of("c1\x85-7492") == latin_1 | controls
    assert secretstore._decoders_for(0) == _DECODERS and len(secretstore._decoders_for(folding | latin_1 | controls)) == 13
    used = []
    real = secretstore._decodings
    monkeypatch.setattr(secretstore, "_decodings", lambda layer, edge=False, decoders=_DECODERS, budget=None: used.append(len(decoders)) or real(layer, edge, decoders, budget))
    for value, size in (('Synthetic"User\\A7492', 7), ('caf\u00e9"User\\A7492', 9), ('two words"User\\A7492', 10), ('c1\x85 two"User\\A7492', 7 + 3 + 2 + 1)):
        used.clear()
        text, expected = document(value, ("json", "yaml"))
        assert scrubber_for(value).scrub(text) == expected
        assert used and set(used) == {size}, (value, used)


def test_a_value_longer_than_a_pattern_can_be_is_found_in_any_mix_of_the_ways_a_text_is_written():
    """Over MAX_PATTERN_VALUE a value is matched by its whole forms, and by patterns a character at a time of pieces of it,
    which are side by side with a marker each: JavaScript's `escape`, `encodeURIComponent`, lower case hex and markup by numbers
    are not whole forms (the third review)."""
    import random

    rng = random.Random(5)
    alphabet = list("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") + list("+/=-_.:\"' \\&%<>") + ["\n", "\u00e9"]
    for size in (4097, 5000, 9000):
        value = "".join(rng.choice(alphabet) for _ in range(size))
        scrubber = scrubber_for(value)
        assert len(scrubber._wholes) > 3
        for sequence in (
            ("js_escape",), ("html_decimal",), ("url_component",), ("url_lower",), ("yaml_py",), ("js_escape", "json"),
            ("url_lower", "json", "yaml"), ("html_decimal", "url", "json"), ("js_escape", "url", "url", "url"),
            ("json", "url", "html", "css"),
        ):
            text, expected = document(value, sequence)
            got = scrubber.scrub(text)
            assert re.sub(r"(?:\[secret:USER\])+", MARKER, got) == expected, (size, sequence)
            assert scrubber.holds(text) and not scrubber.holds(got)


def test_the_start_of_a_long_value_that_a_tool_cut_is_found_and_the_value_is_the_whole_of_itself():
    import random

    rng = random.Random(9)
    value = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789 -_.") for _ in range(6000))
    scrubber = scrubber_for(value)
    assert scrubber.scrub("shown: " + value[:300] + "\u2026 end") == f"shown: {MARKER} end"
    assert scrubber.is_value(value) and scrubber.is_value(json_ascii(value)) and scrubber.holds(value)
    assert not scrubber.is_value(value[:3000]) and not scrubber.is_value(value[3000:]), "a piece of a value is not the value"
    assert scrubber.scrub("<" + value + ">") == f"<{MARKER}>"


def test_a_piece_of_a_long_value_that_ends_with_a_line_break_is_read_with_the_indent_of_the_next_line():
    """The pieces are side by side in the value and the lines of a block have an indent: the line break at the end of a piece has
    another character after it (the first of the next piece), so the spaces between are its."""
    import random

    rng = random.Random(3)
    size = 4097
    pieces = -(-size // secretstore.MAX_PATTERN_VALUE)
    boundary = -(-size // pieces) - 1
    head = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789 -_") for _ in range(boundary))
    tail = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789 -_") for _ in range(size - boundary - 1))
    value = head + "\n" + tail
    scrubber = scrubber_for(value)
    block = "k: |-\n" + "\n".join("  " + line for line in value.split("\n"))
    got = scrubber.scrub(block + "\nnext: 1\n")
    assert re.sub(r"(?:\[secret:USER\])+", MARKER, got) == "k: |-\n  " + MARKER + "\nnext: 1\n"
    assert tail[:50] not in got


def test_a_long_value_with_lines_that_begin_with_spaces_is_found_in_a_block():
    value = "".join(f"host{i}:\n  user:  admin{i}\n" for i in range(200))[:5000].rstrip()
    scrubber = scrubber_for(value)
    block = "k: |-\n" + "\n".join("  " + line for line in value.split("\n"))
    start = len("k: |-\n") + 2
    got = scrubber.scrub(block + "\nnext: 1\n")
    assert re.sub(r"(?:\[secret:USER\])+", MARKER, got) == block[:start] + MARKER + "\nnext: 1\n"
    assert re.sub(r"(?:\[secret:USER\])+", MARKER, scrubber.scrub("x" + value)) == "x" + MARKER


def test_a_value_longer_than_the_limit_for_pieces_has_its_whole_forms_and_no_more():
    value = ("pass word 4097 " * 5000)[: secretstore.MAX_PIECED_VALUE + 10]
    scrubber = scrubber_for(value)
    assert len(scrubber._wholes) == 1
    assert scrubber.scrub("<" + json_ascii(value) + ">") == f"<{MARKER}>"


def test_the_breaks_of_yaml_1_1_and_a_carriage_return_are_line_breaks_with_the_indent_of_the_line_after_them():
    """PyYAML (YAML 1.1) writes U+0085, U+2028 and U+2029 as they are and indents the line after them; a writer may break lines
    with a carriage return alone, or with one and a line feed. The indent is not part of the value."""
    for brk in ("\x85", "\u2028", "\u2029"):
        value = f"ab{brk}cd-7492"
        scrubber = scrubber_for(value)
        for text, expected in (
            (f"k: 'ab{brk}   cd-7492' end", f"k: '{MARKER}' end"),
            (f"k: |-\n  ab{brk}  cd-7492\nend", f"k: |-\n  {MARKER}\nend"),
            (f"k: ab{brk}cd-7492", f"k: {MARKER}"),
        ):
            assert scrubber.scrub(text) == expected, (brk, text)
    value = "alpha beta-7492"
    for text, expected in (
        ("k: alpha\r  beta-7492\n", f"k: {MARKER}\n"), ("k: alpha\r\n  beta-7492\n", f"k: {MARKER}\n"),
        ("k: alphabeta-7492\n", "k: alphabeta-7492\n"),
    ):
        assert scrubber_for(value).scrub(text) == expected, text
    lines = "line1\nline2 x-7492\n"
    assert scrubber_for(lines).scrub("k: |\r\n  line1\r\n  line2 x-7492\r\nnext: 1") == f"k: |\r\n  {MARKER}next: 1"
    assert scrubber_for(lines).scrub("k: |\r  line1\r  line2 x-7492\rnext: 1") == f"k: |\r  {MARKER}next: 1"


def _assert_noop_entity_budget():
    layer = _Layer.whole("&a" * 1000, 0)
    limit = len(layer.text) + secretstore.TOKEN_COST * 500
    budget = secretstore._Budget(limit)
    assert list(_decodings(layer, False, (ENTITY,), budget)) == []
    assert budget.out, "no-op entity matches must exhaust the token budget"
    assert budget.left == limit, "a decoding that exceeds the budget is refused whole"


def test_every_match_of_a_family_is_charged_to_the_budget_whether_or_not_it_stood_for_anything():
    """`&a` is the start of a name of markup, and is none: the call to find that out is work (the third review: a text of 6.8 MB
    that is all such matches took 14 seconds, and the limit was never reached)."""
    layer = _Layer.whole("&a" * 1000, 0)
    spent = [0]
    assert layer.decoded(ENTITY, False, spent) is None
    assert spent[0] == len(layer.text) + secretstore.TOKEN_COST * 1000
    run = _Layer.whole("%C3%A9" * 100, 0)
    assert run.decoded(PERCENT, False, spent).text == "\u00e9" * 100 and spent[0] == len(run.text) + secretstore.TOKEN_COST * (1 + len(run.text) // 3), \
        "a run of percent escapes is one match and a call for each three characters of it"
    quotes = _Layer.whole("''" * 300, 0)
    assert quotes.decoded(QUOTE_LEFT, False, spent) is not None and spent[0] == len(quotes.text) + secretstore.TOKEN_COST * 201
    budget = secretstore._Budget(len(layer.text) + secretstore.TOKEN_COST * 500)
    assert list(_decodings(layer, False, secretstore._DECODERS, budget)) == [] and budget.out
    _assert_noop_entity_budget()


def test_noop_entity_budget_contract_rejects_uncharged_matches(monkeypatch):
    real = _Layer.decoded

    def uncharged(self, decoder, shifted=False, spent=None):
        made = real(self, decoder, shifted, spent)
        if decoder == ENTITY and made is None and spent is not None:
            spent[0] = len(self.text)
        return made

    _assert_noop_entity_budget()
    monkeypatch.setattr(_Layer, "decoded", uncharged)
    with pytest.raises(AssertionError, match="no-op entity matches must exhaust the token budget"):
        _assert_noop_entity_budget()


def test_large_noop_entities_exhaust_the_work_budget_and_fail_closed(monkeypatch):
    budgets = []

    class AuditedBudget(secretstore._Budget):
        def __init__(self, limit):
            super().__init__(limit)
            self.initial, self.accepted, self.refused = limit, 0, 0
            budgets.append(self)

        def take(self, cost):
            accepted = super().take(cost)
            if accepted:
                self.accepted += cost
            else:
                self.refused += 1
            return accepted

    def exhausted():
        assert budgets and all(budget.initial == secretstore.WORK_LIMIT for budget in budgets)
        assert 0 < sum(budget.accepted for budget in budgets) <= secretstore.WORK_LIMIT
        assert sum(budget.refused for budget in budgets) > 0, "the real work budget must be exhausted"
        assert all(budget.left >= 0 for budget in budgets)
        budgets.clear()

    monkeypatch.setattr(secretstore, "_Budget", AuditedBudget)
    scrubber = scrubber_for('Synthetic"User\\A7492')
    text = ("&a" * 200 + " Synthetic User A7492 \\n ") * 8000
    out = scrubber.scrub(text)
    exhausted()
    assert len(out) < len(text) and MARKER in out, "the part past the limit is scrubbed whole"
    assert scrubber.holds(text), "a value in a window past the limit cannot be declared absent"
    exhausted()


def test_the_indent_of_the_line_after_a_value_that_ends_with_a_line_break_stays():
    """The line break at the end of a value is not read with the spaces after it: they are the indent of the next key."""
    value = "line1\nline2\n"
    scrubber = scrubber_for(value)
    text = "a:\n  key: |\n    line1\n    line2\n  next: 1\n"
    assert scrubber.scrub(text) == f"a:\n  key: |\n    {MARKER}  next: 1\n"
    inside = "k: |\n  line1\n    line2\n  tail"
    assert scrubber_for("line1\n  line2").scrub(inside) == f"k: |\n  {MARKER}\n  tail"


def test_a_hide_that_cannot_be_made_leaves_the_scrubber_as_it_was(monkeypatch):
    """A scrubber that raised half way through its patterns had none, and every text went through it unscrubbed (the second
    review: a page can have a lone surrogate in a string, and a long value with one raised in `quote`)."""
    scrubber = secretstore.Scrubber({"PASSWORD": {"value": "hunter2-Secret", "origins": [ORIGIN]}})
    lone = "x" * 5000 + "\ud800" + "yz-1"
    stored = secretstore.Scrubber({"LONG": {"value": lone, "origins": [ORIGIN]}})
    assert stored.scrub("v=" + lone + "!") == "v=[secret:LONG]!", "a lone surrogate in a long stored value is not an error"
    scrubber.hide("OTP", "otp\ud800-1")
    assert scrubber.scrub("v=otp\ud800-1!") == "v=[secret:OTP]!" and scrubber.scrub("hunter2-Secret") == "[secret:PASSWORD]"
    with pytest.raises(secretstore.SecretStoreError, match="at most 4096"):
        scrubber.hide("BIG", "y" * 4097)
    assert "y" * 4097 not in scrubber._texts and scrubber.scrub("hunter2-Secret") == "[secret:PASSWORD]"
    real = secretstore._alternation
    monkeypatch.setattr(secretstore, "_alternation", lambda wholes: (_ for _ in ()).throw(RuntimeError("a pattern too large")))
    with pytest.raises(secretstore.SecretStoreError) as raised:
        scrubber.hide("CODE", "another-value-7")
    assert "another-value-7" not in str(raised.value)
    monkeypatch.setattr(secretstore, "_alternation", real)
    assert bool(scrubber) and scrubber.scrub("hunter2-Secret and another-value-7") == "[secret:PASSWORD] and another-value-7"
    assert "another-value-7" not in scrubber._texts and scrubber.holds("hunter2-Secret")
    monkeypatch.setattr(secretstore, "_alternation", lambda wholes: (_ for _ in ()).throw(RuntimeError("a pattern too large")))
    with pytest.raises(secretstore.SecretStoreError, match="stored value"):
        secretstore.Scrubber({"PASSWORD": {"value": "hunter2-Secret", "origins": [ORIGIN]}})


def test_a_letter_or_a_digit_takes_one_character_of_a_text_and_any_other_the_reach():
    weights = secretstore._weights("a-9\u00e9 Z1")
    assert weights == [1, secretstore.REACH, 1, secretstore.REACH, secretstore.REACH, 1, 1]
    assert secretstore._weights("2026")[0] == secretstore.REACH and secretstore._weights("2026")[1:] == [1, 1, 1], "CSS escapes the first digit"
    assert secretstore._weights("-5x")[:2] == [secretstore.REACH, secretstore.REACH], "and the one after a first hyphen"
    assert secretstore._weights("") == []
    trace = secretstore._Trace("key" + "x" * 1000 + "\n" + "y" * 1000, MARKER)
    assert trace.reach_before == 0 and trace.reach_tail == 0 and trace.gaps == [secretstore.REACH]
    assert trace.reach_rest == 2003 + secretstore.REACH, "two thousand letters and a line end: not two thousand times the reach"


def test_a_long_multi_line_key_in_a_page_of_escapes_is_scrubbed_exactly_and_not_the_page_with_it():
    """The second review: the window of a value was 512 characters for each of its characters, so a PEM key of 1,679 characters
    had a window of half a million, and a page of 100 KB with the key once in JSON was scrubbed whole. A letter or a digit takes
    one character, and the lines and the signs of the key a few hundred."""
    import random

    rng = random.Random(11)
    body = "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/") for _ in range(1600))
    pem = "-----BEGIN PRIVATE KEY-----\n" + "\n".join(body[at:at + 64] for at in range(0, len(body), 64)) + "\n-----END PRIVATE KEY-----\n"
    scrubber = scrubber_for(pem)
    row = '- link "Order 1234 &amp; details" [ref=e5]:\n  - /url: /orders?id=1234&tab=items&q=a%20b\n'
    for kilobytes in (60, 120):
        half = row * (kilobytes * 1000 // len(row) // 2)
        layered = json.dumps(pem)[1:-1]
        text = half + '- generic [ref=e9]: {"key": ' + json.dumps(pem) + "}\n" + half
        started = time.perf_counter()
        out = scrubber.scrub(text)
        assert time.perf_counter() - started < 5.0
        assert out == text.replace(layered, MARKER), kilobytes


def test_a_value_that_ends_with_a_run_is_looked_for_to_it_and_what_is_scrubbed_whole_for_lack_of_time_stops_at_its_end(monkeypatch):
    value = "first.last@example.com"
    trace = secretstore._Trace(value, MARKER)
    assert "com" in trace.runs and trace.tail == 0 and trace.reach_tail == 0, "the last run of a value that ends with one is in"
    assert "first" in trace.runs and trace.lead == 0
    scrubber = scrubber_for(value)
    text = "\n".join(f"line {n} {yaml_playwright(json_ascii(value))} and the {n} words after it end" for n in range(300))
    monkeypatch.setattr(secretstore, "WORK_LIMIT", 3000)
    out = scrubber.scrub(text)
    assert out.count(" and the ") == 300 and out.count(" words after it end") == 300, "only the value is taken"
    assert "example" not in out and scrubber.holds(text)


def test_a_value_of_line_feeds_does_not_make_a_text_of_crlf_take_exponential_time():
    """`\r\n` is one break or two (`\r`, `\n`) to a pattern that allows both: a value of 40 line feeds against a text of 40 CRLF
    and no end has 2**40 ways to fail."""
    scrubber = scrubber_for("x" + "\n" * 40 + "y-7492")
    text = "x" + "\r\n" * 40 + "z"
    started = time.perf_counter()
    assert scrubber.scrub(text) == text
    assert time.perf_counter() - started < 2.0
    assert scrubber.scrub("x" + "\r\n" * 40 + "y-7492") == MARKER


def test_a_run_of_dots_does_not_cost_a_scan_for_each_dot_of_it():
    scrubber = scrubber_for("alpha.bravo-7492")
    assert scrubber._dot_run == 1
    assert scrubber.scrub("shown alpha.bra... end") == f"shown {MARKER} end"
    assert scrubber_for("ab..cd-7492")._dot_run == 2
    assert scrubber_for("ab..cd-7492").scrub("shown ab..cd-... end") == f"shown {MARKER} end"
    assert scrubber_for("plain-7492")._dot_run == 0 and scrubber_for("a" + "." * 100 + "bcd")._dot_run == secretstore.MAX_DOT_RUN
    started = time.perf_counter()
    text = ("." * 600 + " alpha and bravo and 7492 \n ") * 4000
    assert scrubber.scrub(text) == text
    assert time.perf_counter() - started < 3.0
    assert scrubber.scrub("." * 200_000) == "." * 200_000


def test_searching_a_decoded_text_is_charged_more_for_a_scrubber_with_more_patterns(monkeypatch):
    charges = []
    real = secretstore._Budget.take
    monkeypatch.setattr(secretstore._Budget, "take", lambda self, cost: charges.append(cost) or real(self, cost))
    text = (yaml_playwright(json_ascii(REVIEW_VALUE)) + " " + "x" * 200 + " ") * 20
    one = scrubber_for(REVIEW_VALUE)
    many = secretstore.Scrubber({f"V{n}": {"value": f"another-value-{n}-{REVIEW_VALUE}", "origins": [ORIGIN]} for n in range(12)} | {"USER": {"value": REVIEW_VALUE, "origins": [ORIGIN]}})
    one.scrub(text)
    cheap = sum(charges)
    charges.clear()
    many.scrub(text)
    assert sum(charges) > cheap * 1.5, (sum(charges), cheap)


# What the tool writes as its ellipsis, and what the text writes after it (the dots of a text go on after the three of the tool).
ELLIPSIS_ENDINGS = [
    ("\u2026", ""), ("...", ""), ("\u2026", "..."), ("...", "\u2026"), ("...", "."), ("...", "...."), ("\u2026", "\u2026"),
]
# What comes right before the value: a run of dots or an ellipsis that is the text's, and is not the value's.
LEADS = ["", "Loading... ", "x\u2026", "wait....", "\u2026", "..."]


def test_every_cut_of_every_value_in_every_encoder_of_one_layer_is_scrubbed_to_the_ellipsis(monkeypatch):
    """A tool cuts a text by its length, and ends it with an ellipsis: after any character of the value, or in the middle of the
    escape of one. The value is cut after 4 characters at the earliest; what is shown of it, and the ellipsis, is the marker.
    The ellipsis is `\u2026` or three dots, and the text can go on with dots or another ellipsis; the cut can follow a run of
    dots of the text; the places of a value are found in the window of each ellipsis and in one pass over the text.
    (The fourth review: a character that is written one way as two of its own, such as a backslash, and one more, was read the
    longer way, and the rest of the cut did not fit. The fifth: the tool's `...` followed by a dot of the text was not found.)"""
    cuts = tried = 0
    for many in (10**9, 0):
        monkeypatch.setattr(secretstore, "MANY_ELLIPSES", many)
        count_of_cuts = 0
        for value in VALUES:
            scrubber = scrubber_for(value)
            for name in ENCODERS:
                if name in SPANNED:
                    continue  # a fold is a layer of its own
                encode, before, after = ENCODERS[name]
                for count in range(secretstore.MIN_VALUE_LENGTH, len(value)):
                    shown_whole, with_next = encode(value[:count]), encode(value[:count + 1])
                    if not with_next.startswith(shown_whole) or not encode(value).startswith(with_next):
                        continue
                    for cut in range(len(shown_whole), len(with_next)):
                        shown = with_next[:cut]
                        tool, own = ELLIPSIS_ENDINGS[count_of_cuts % len(ELLIPSIS_ENDINGS)]
                        lead = LEADS[count_of_cuts // len(ELLIPSIS_ENDINGS) % len(LEADS)]
                        count_of_cuts += 1
                        text = "X" + lead + before + shown + tool + own + after + "Y"
                        out = scrubber.scrub(text)
                        tried += 1
                        expected = "X" + lead + before + MARKER + own + after + "Y"
                        if not never_more_than_the_exact_answer_leaves(out, expected):
                            cuts += 1
                            if cuts <= 3:
                                print(many, repr(value), name, repr(text), repr(out))
    assert tried > 4000 and not cuts, f"{cuts} of {tried} cuts are not scrubbed to the ellipsis"


@pytest.mark.parametrize("dots", range(3, 11))
@pytest.mark.parametrize("many", [10**9, 0])
def test_the_tools_three_dots_are_the_first_three_of_a_run_for_a_value_without_dots_and_the_text_goes_on_with_the_rest(
    monkeypatch, dots, many
):
    """`Tr0ub4dor-pa....` is the value cut, the tool's `...`, and a dot of the text: the value has no dot, so the ellipsis begins
    where the run does (the fifth review: it was looked for at the last three, and the value was left in the text)."""
    monkeypatch.setattr(secretstore, "MANY_ELLIPSES", many)
    scrubber = scrubber_for("Tr0ub4dor-pass-7492")
    assert scrubber.scrub("typed Tr0ub4dor-pa" + "." * dots + " next") == f"typed {MARKER}{'.' * (dots - 3)} next"
    assert scrubber.scrub("typed Tr0ub4dor-pa" + "." * dots) == f"typed {MARKER}{'.' * (dots - 3)}"
    ends_in_dots = scrubber_for("wait for it...")  # the dots of the run are its own, or the tool's: the text keeps the ones that are neither
    assert re.fullmatch(rf"x {re.escape(MARKER)}\.{{0,{dots - 3}}} y", ends_in_dots.scrub("x wait for it" + "." * dots + " y"))


@pytest.mark.parametrize("many", [10**9, 0])
def test_a_value_that_begins_with_dots_is_found_after_a_run_of_dots_that_ends_in_them(monkeypatch, many):
    """`Loading....abcd-12\u2026`: three dots are the text's and the fourth is the first character of the value (the fifth
    review: the run before was a floor that a cut value could not begin before)."""
    monkeypatch.setattr(secretstore, "MANY_ELLIPSES", many)
    assert scrubber_for(".abcd-1234").scrub("Loading....abcd-12\u2026 end") == f"Loading...{MARKER} end"
    assert scrubber_for("..abcdefgh").scrub("Wait.....abcde\u2026 end") == f"Wait...{MARKER} end"
    assert scrubber_for(".abcd-1234").scrub("Loading... .abcd-12\u2026 end") == f"Loading... {MARKER} end"
    assert scrubber_for(".abcd-1234").scrub("Loading.\u2026.abcd-12... end") == f"Loading.\u2026{MARKER} end"
    two = secretstore.Scrubber({
        "A": {"value": ".abcd-1234", "origins": [ORIGIN]}, "B": {"value": "wxyz-5678", "origins": [ORIGIN]},
    })
    assert two.scrub("a....abcd-1\u2026 b wxyz-5\u2026") == "a...[secret:A] b [secret:B]"


@pytest.mark.parametrize("value, shown, expected", [
    ('Tr0ub4dor"x 7492', '{"pw":"Tr0ub4dor\\\u2026', '{"pw":"[secret:U]'),   # cut after the backslash of \"
    ("Tr0ub4dor x 7492", "?pw=Tr0ub4dor%2\u2026", "?pw=[secret:U]"),           # cut inside %20
    ('Tr0ub4dor"x 7492', "#pw-Tr0ub4dor\\\u2026", "#pw-[secret:U]"),          # cut after the backslash of a CSS escape
    ("Tr0ub4dor&x 7492", "<p>Tr0ub4dor&am\u2026", "<p>[secret:U]"),           # cut inside &amp;
    ("Tr0ub4dor'x 7492", "<p>Tr0ub4dor&#3\u2026", "<p>[secret:U]"),           # cut inside &#39;
    ('Tr0ub4dor"x 7492', "x=Tr0ub4dor\\u00\u2026", "x=[secret:U]"),            # cut inside \u0022
])
def test_a_value_that_a_tool_cut_in_the_middle_of_an_escape_is_found(value, shown, expected):
    """Playwright cuts the text of an attribute at 500 characters, and of an element at 50, by length: the cut can fall in the
    middle of the escape of a character, and what is left of it is the start of a way to write the character."""
    scrubber = scrubber_for(value, "U")
    assert scrubber.scrub(shown) == expected
    assert scrubber.scrub(shown.replace("\u2026", "...")) == expected
    assert scrubber.scrub("Tr0ub4dor" + "\u2026") == "[secret:U]", "a cut between two characters, as before"
    assert scrubber.scrub("Tr0ub4dxr\\\u2026") == "Tr0ub4dxr\\\u2026", "and a text that is not the value is left"


def test_two_cuts_of_a_value_with_dots_in_it_are_one_stretch_and_the_text_after_them_stays():
    scrubber = scrubber_for("abcdefghijklmnopqrstu...vwxyz-pass7")
    assert scrubber.scrub("x abcdefghijklmnopqrstu...vw... tail") == "x [secret:USER] tail"
    assert scrubber.scrub("x abcdefghijklmnopqrstu...vwx... tail and more") == "x [secret:USER] tail and more"
    wide = scrubber_for("abcdefghijklmnopqrstu\u2026vwxyz-pass7")
    assert wide.scrub("x abcdefghijklmnopqrstu\u2026vw\u2026 tail and") == "x [secret:USER] tail and"
    assert scrubber.scrub("a ab...cd... b") == "a ab...cd... b"


def test_a_text_of_many_ellipses_is_scrubbed_as_a_text_of_few_is_and_costs_what_it_is_long(monkeypatch):
    scrubber = scrubber_for("alpha.bravo-pass7")
    text = "".join(f"line {n} alpha.bra\u2026 and alpha.bravo-pa... and other... words\u2026\n" for n in range(300))
    expected = scrubber.scrub(text)
    assert expected.count(MARKER) == 600 and "alpha" not in expected
    monkeypatch.setattr(secretstore, "MANY_ELLIPSES", 10**9)
    assert scrubber.scrub(text) == expected
    monkeypatch.setattr(secretstore, "MANY_ELLIPSES", 0)
    assert scrubber.scrub(text) == expected, "the same text, found in one pass"
    monkeypatch.undo()
    started = time.perf_counter()
    assert scrubber_for("alpha.bravo-pass7").scrub("\u2026" * 1_000_000) == "\u2026" * 1_000_000
    assert time.perf_counter() - started < 6.0


@pytest.mark.parametrize("seed", range(3))
def test_the_walk_of_a_cut_is_what_the_regular_expression_of_the_characters_so_far_says(seed):
    """The walk keeps the set of places the characters can have ended at; a regular expression of the characters, backtracking, is
    the same thing in other words. They agree on texts made at random from the ways to write a value, cut or not, and on noise.
    (The last character of a whole value, `\\` or `&`, is also the start of a longer way to write it: that is a cut, and an ellipsis
    after the whole value is taken with it.)"""
    import random

    rng = random.Random(900 + seed)
    alphabet = list("abcXYZ012") * 3 + list("\"\\' &<>+%.-_\n") + ["\u00e9", "\u65e5"]
    noise = list("abc\\'\" &;#%+\n.0129") + ["x", "\u00e9"]
    agreed = disagreed = 0
    for _ in range(300):
        value = "".join(rng.choice(alphabet) for _ in range(rng.randint(5, 12)))
        cut = secretstore._Cut(secretstore._pieces_of(value), value)
        for _ in range(12):
            if rng.random() < 0.6:
                name = rng.choice([each for each in ENCODERS if each not in SPANNED])
                shown = ENCODERS[name][0](value[: rng.randint(2, len(value))])
                shown = shown[: rng.randint(len(shown) - 6, len(shown))] if rng.random() < 0.5 else shown
            else:
                shown = "".join(rng.choice(noise) for _ in range(rng.randint(1, 14)))
            if not shown:
                continue
            got = cut._reaches(shown, 0, len(shown))
            expected = False
            for count in range(secretstore.MIN_VALUE_LENGTH, len(value)):
                pattern = "".join(secretstore._pieces_of(value[:count], more=True))
                if re.fullmatch(pattern, shown):
                    expected = True
                    break
                # or the characters so far, and the start of a way to write the next one
                for start in range(len(shown)):
                    if shown[start:] in secretstore._prefixes_of(value[count]) and re.fullmatch(pattern, shown[:start]):
                        expected = True
                        break
                if expected:
                    break
            agreed += got == expected
            if got != expected:
                disagreed += 1
                print(repr(value), repr(shown), "walk", got, "regex", expected)
    assert agreed > 3000 and not disagreed


def test_a_cut_is_four_characters_of_a_value_at_least_whichever_way_the_places_are_found(monkeypatch):
    """The dots of a value are the dots of an ellipsis: `ab...` is two characters of `ab..cd-7492` and the ellipsis, or the
    start of it and its dots. Fewer than 4 characters is no start of a value, in the window of the ellipsis and in the pass over
    the text (the fourth review: the pass scrubbed `etc...` for the value `etc.4ever!`)."""
    scrubber = scrubber_for("etc.4ever!")
    shown = "x etc... y and ab... and abc... and etc.4e... z"
    expected = f"x etc... y and ab... and abc... and {MARKER} z"
    for many in (10**9, 0):
        monkeypatch.setattr(secretstore, "MANY_ELLIPSES", many)
        assert scrubber.scrub(shown) == expected, many
        assert scrubber_for("ab..cd-7492").scrub("x ab... y") == "x ab... y", many


def test_a_cut_value_is_scrubbed_whole_when_it_begins_with_another_value_and_when_two_values_are_cut_at_one_ellipsis():
    both = secretstore.Scrubber({
        "USER": {"value": "jdoe2026", "origins": [ORIGIN]}, "PASSWORD": {"value": "jdoe2026-Secret!x", "origins": [ORIGIN]},
    })
    for cut in ("jdoe2026-Secr", "jdoe2026-Secret!"):
        assert both.scrub(f'fill "{cut}\u2026"') == 'fill "[secret:PASSWORD]"', cut
    assert both.scrub('fill "jdoe2026-Secret!x" and jdoe2026') == 'fill "[secret:PASSWORD]" and [secret:USER]'
    two = secretstore.Scrubber({
        "A": {"value": "abcd.efgh-1111", "origins": [ORIGIN]}, "B": {"value": "efgh...ijkl-2222", "origins": [ORIGIN]},
    })
    assert two.scrub("x abcd.efgh... y") == "x [secret:A] y"


def test_a_periodic_value_in_a_text_of_ellipses_is_scrubbed_from_its_longest_cut_and_costs_a_bounded_time():
    """Of the places where a value can begin in the window of an ellipsis, the first that fits is the longest cut. A value
    and a text that repeat themselves are scrubbed from it, and not from the nearest 64 places, which left 370 characters of
    each stretch (the fifth review)."""
    scrubber = scrubber_for("ab" * 300 + "-7492")
    text = ("ab" * 250 + "\u2026 ") * 400
    started = time.perf_counter()
    out = scrubber.scrub(text)
    assert time.perf_counter() - started < 6.0
    assert out == f"{MARKER} " * 400


def test_a_search_for_cut_values_that_costs_more_than_its_limit_scrubs_the_window_of_each_ellipsis_that_is_left(monkeypatch):
    """A value that has an ellipsis in it can begin before the one before, and a value and a text that repeat themselves have
    each place walked to its end: 166 ellipses of a periodic value took 20 seconds. Past CUT_WORK_LIMIT the windows left are
    scrubbed whole, and the cost stays at the limit (the fifth review)."""
    scrubber = scrubber_for("ab" * 300 + "...-7492")
    text = ("ab" * 250 + "X\u2026 ") * 166
    started = time.perf_counter()
    scrubber.scrub(text)
    assert time.perf_counter() - started < 8.0
    monkeypatch.setattr(secretstore, "CUT_WORK_LIMIT", 200_000)
    out = scrubber.scrub(text)
    assert out.startswith("abab") and out.endswith(MARKER + " ") and len(out) < 700, "all of the text after the ellipsis it got to"
    monkeypatch.setattr(secretstore, "CUT_WORK_LIMIT", 10**9)
    small = ("ab" * 25 + "X\u2026 ") * 40
    assert scrubber_for("ab" * 30 + "...-7492").scrub(small) == small, "within the limit, a text that is not the value is left"
    monkeypatch.setattr(secretstore, "CUT_WORK_LIMIT", 2000)
    gone = scrubber_for("ab" * 30 + "...-7492").scrub(small)
    assert gone != small and gone.endswith(MARKER + " ") and len(gone) < len(small) // 2


def test_a_text_of_cut_values_costs_what_it_is_long_because_a_cut_holds_no_more_of_the_ellipses_before_it_than_the_value_has():
    """Each ellipsis looked at the 520 characters before it, and in a text that repeats a cut value there are 37 starts of
    the value in them: 150,000 cut values took 35 seconds. A cut value holds as many of the ellipses before it as the value
    has in it, and no more (the fourth review; the fifth: with one in the value, 100,000 copies took 86 seconds)."""
    scrubber = scrubber_for("Tr0ub4dor-pass-7492")
    assert scrubber._value_ellipses == 0 and scrubber_for("Tr0ub4dor...pass-7492")._value_ellipses == 1
    assert scrubber_for("Tr0ub4dor\u2026pass-7492")._value_ellipses == 1
    assert scrubber_for("a...b\u2026c-1234....d")._value_ellipses == 3 and scrubber_for("a..b.c-1234")._value_ellipses == 0
    text = "Tr0ub4dor-pa\u2026 " * 3000
    assert scrubber.scrub(text) == f"{MARKER} " * 3000
    started = time.perf_counter()
    assert scrubber.scrub("Tr0ub4dor-pa\u2026 " * 100_000) == f"{MARKER} " * 100_000
    assert time.perf_counter() - started < 8.0
    for value, copy in (("Tr0ub4dor...pass-7492", "Tr0ub4dor...pa\u2026 "), ("Tr0ub4dor\u2026pass-7492", "Tr0ub4dor\u2026pa\u2026 ")):
        started = time.perf_counter()
        assert scrubber_for(value).scrub(copy * 10_000) == f"{MARKER} " * 10_000, value
        assert time.perf_counter() - started < 6.0, value


def test_a_cut_value_is_also_found_when_the_text_before_it_is_a_run_of_the_values_own_ellipses(monkeypatch):
    """The floor of a cut: a value with two ellipses in it holds two of the runs before the one that ends it."""
    scrubber = scrubber_for("abcd...efgh\u2026ijkl-1234")
    assert scrubber._value_ellipses == 2
    for many in (10**9, 0):
        monkeypatch.setattr(secretstore, "MANY_ELLIPSES", many)
        assert scrubber.scrub("x\u2026 y... abcd...efgh\u2026ijkl-12\u2026 z") == f"x\u2026 y... {MARKER} z"
        assert scrubber.scrub("abcd...efgh\u2026ij... abcd...efgh\u2026ijkl\u2026") == f"{MARKER} {MARKER}"


@pytest.mark.parametrize("seed", range(3))
def test_the_floor_of_a_cut_saves_work_and_never_changes_what_is_scrubbed(seed, monkeypatch):
    """A cut value does not begin before the ellipses that it cannot hold. With no floor at all, a text of ellipses and
    prefixes of values (dots at the start, at the end and in the middle of a value) is scrubbed in the same way, in the window of
    each ellipsis and in a pass over the text."""
    import random

    rng = random.Random(500 + seed)
    alphabet = list("abcdefgh1234-") * 2 + [".", ".", "\u2026"]
    noise = ["x", " ", "..", ".", "...", "....", "\u2026", "\u2026\u2026", " Loading... ", "ab", "-12"]
    differences = checked = 0
    for _ in range(120):
        value = "".join(rng.choice(alphabet) for _ in range(rng.randint(5, 14)))
        if len(value.strip(".\u2026")) < 3:
            continue
        scrubber = scrubber_for(value)
        for _ in range(8):
            parts = []
            for _ in range(rng.randint(3, 14)):
                kind = rng.random()
                if kind < 0.45:
                    parts.append(value[: rng.randint(secretstore.MIN_VALUE_LENGTH, len(value))])
                elif kind < 0.55:
                    parts.append(value)
                else:
                    parts.append(rng.choice(noise))
                if rng.random() < 0.4:
                    parts.append(rng.choice(["\u2026", "...", "....", "\u2026..."]))
            text = "".join(parts)
            for many in (10**9, 0):
                with monkeypatch.context() as patched:
                    patched.setattr(secretstore, "MANY_ELLIPSES", many)
                    with_floor = scrubber.scrub(text)
                    patched.setattr(secretstore.Scrubber, "_floor", lambda self, text, runs, number: 0)
                    without = scrubber.scrub(text)
                checked += 1
                if with_floor != without:
                    differences += 1
                    if differences <= 3:
                        print(repr(value), repr(text), repr(with_floor), repr(without))
    assert checked > 1500 and not differences


@pytest.fixture(scope="module")
def long_values():
    """Values of letters and digits of 600 to 16,000 characters, each with its scrubber (made once: a pattern for a long value
    costs a tenth of a second for each thousand characters)."""
    import random
    import string

    made = {}

    def get(length):
        if length not in made:
            rng = random.Random(length)
            value = "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(length))
            made[length] = (value, scrubber_for(value))
        return made[length]

    return get


def nothing_of(value, text, size=10):
    """True when no stretch of `size` characters of the value is in the text."""
    return not any(value[at: at + size] in text for at in range(0, len(value) - size))


@pytest.mark.parametrize("length, shown", [
    (600, 530), (600, 499), (700, 650), (2000, 1500), (5000, 2400), (5000, 2600), (5000, 3000), (5000, 4090), (5000, 4100),
    (16000, 4090), (16000, 4100), (16000, 4600), (16000, 8100),
])
def test_a_cut_that_shows_more_than_the_window_of_a_short_value_is_found_at_any_length_and_across_the_pieces_of_a_long_value(
    long_values, length, shown
):
    """The window of a cut is as long as the value can be written (MAX_CUT_RATIO characters for each of its characters), and a
    long value is made of pieces, each with a cut of its own: what a tool shows of a value is scrubbed wherever it cuts it (the
    fifth review: 530 of 600 characters, 2,400 of 5,000, and 4,100 of 16,000 stayed)."""
    value, scrubber = long_values(length)
    for ellipsis in ("\u2026", "..."):
        out = scrubber.scrub("<p>" + value[:shown] + ellipsis + "</p>")
        assert nothing_of(value, out), (length, shown, ellipsis, len(out))
        assert out.startswith("<p>" + MARKER) and out.endswith("</p>") and "<p>" + value[:4] not in out


def test_a_cut_of_playwright_size_under_one_layer_of_json_is_found():
    """The tool cuts at 500 characters, and a layer that escapes the quotes of the value makes 750 of them."""
    import random
    import string

    rng = random.Random(1)
    value = "".join(rng.choice(string.ascii_letters + '"') for _ in range(700))
    text = json.dumps({"preview": value[:499] + "\u2026"}, ensure_ascii=False)
    assert len(text) > secretstore.MAX_CUT_WINDOW
    assert scrubber_for(value).scrub(text) == '{"preview": "' + MARKER + '"}'


def test_a_cut_in_the_middle_of_a_character_over_ffff_is_found():
    """A tool that cuts by the length of a JavaScript string cuts an emoji in two: what is left is its first half, which a
    decoder makes into U+FFFD."""
    scrubber = scrubber_for("abcd\U0001F600xyz-2")
    for shown in ("abcd\ud83d\u2026", "abcd\ufffd\u2026", "abcd\ud83d...", "abcd\ufffd..."):
        assert scrubber.scrub(shown) == MARKER, shown
    assert scrubber.scrub("abcd\u2026") == MARKER and scrubber.scrub("abcX\ufffd\u2026") == "abcX\ufffd\u2026"


def test_a_name_of_markup_is_one_way_to_write_a_character_whatever_layer_it_is_in():
    """`htmlentities` of PHP writes `&eacute;` and `&excl;`; as the first of four layers it is read by the pattern, which knew
    the numbers and not the names (the third review)."""
    value = "caf\u00e9\\Secret-7492!"
    scrubber = scrubber_for(value)
    named = "caf&eacute;\\Secret-7492&excl;"
    assert scrubber.scrub(f"<p>{named}</p>") == f"<p>{MARKER}</p>"
    text = "#pw-" + quote(quote(named, safe=""), safe="")
    assert scrubber.scrub(text) == "#pw-" + MARKER
    inner = css_escape(quote(quote(named, safe=""), safe=""))
    assert scrubber.scrub("#pw-" + inner + " > b") == "#pw-" + MARKER + " > b"
    assert "caf&eacute;" in html_module.escape("x") + "caf&eacute;" and secretstore._escapes_of("!") >= {"!", "&excl;"}


def _key_file():
    """A JSON key file of a service account: 2,000 characters, base64 lines in the middle and signs all through it."""
    import random

    rng = random.Random(4)
    key = "-----BEGIN PRIVATE KEY-----\n" + "\n".join(
        "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/") for _ in range(64)) for _ in range(20)
    ) + "\n-----END PRIVATE KEY-----\n"
    return json.dumps({
        "type": "service_account", "project_id": "demo-project-1234", "private_key_id": "0123456789abcdef", "private_key": key,
        "client_email": "robot@demo-project-1234.example.com", "client_id": "123456789012345678901",
        "auth_uri": "https://accounts.example.com/o/oauth2/auth", "token_uri": "https://oauth2.example.com/token",
    }, indent=2) + "\n"


def test_a_key_file_in_a_page_of_escapes_is_scrubbed_exactly_because_its_first_and_last_runs_are_looked_for():
    """The runs of a value were its three longest, in the middle of the base64 of a key; the window ran 37,000 characters before
    the first and 59,000 after the last, and a snapshot of 200 KB with the file once was scrubbed whole (the third review)."""
    keyfile = _key_file()
    trace = secretstore._Trace(keyfile, MARKER)
    assert trace.runs[0] == "type" and trace.runs[-1] == "token" and len(trace.runs) == 3
    assert trace.reach_before <= 5 * secretstore.REACH and trace.reach_tail <= 5 * secretstore.REACH
    scrubber = scrubber_for(keyfile)
    row = '- link "Order 1234 &amp; details" [ref=e5]:\n  - /url: /orders?id=1234&tab=items&q=a%20b\n'
    half = row * (100_000 // len(row))
    layered = json.dumps(keyfile)[1:-1]
    text = half + '- generic [ref=e9]: {"file": ' + json.dumps(keyfile) + "}\n" + half
    started = time.perf_counter()
    out = scrubber.scrub(text)
    assert time.perf_counter() - started < 6.0
    assert out == text.replace(layered, MARKER)


def test_a_value_with_a_short_first_or_last_run_is_looked_for_from_the_first_run_that_is_not_short():
    trace = secretstore._Trace("!a!bc!DEFGH!ij!KLMNO!p!", MARKER)
    assert "DEFGH" in trace.runs and trace.runs[-1] == "KLMNO" and len(trace.runs) == 3, "the first and the last of three letters or more"
    assert secretstore._Trace("!a!bc!d!", MARKER).runs == ["a", "bc", "d"], "nothing of three letters: the longest, as before"
    edge = secretstore._Trace("a!DEFGH!p", MARKER)
    assert edge.runs[0] == "a" and edge.runs[-1] == "p" and edge.lead == 0 and edge.tail == 0, "a value that begins and ends with a run"


def test_a_text_that_has_none_of_the_words_of_a_value_is_not_even_searched_for_escapes(monkeypatch):
    """The hints of a text are the places of its escapes: an array of them for a text of megabytes costs seconds, and a value
    whose words are not in the text cannot be in it, in any layer."""
    calls = []
    real = secretstore._ESCAPE_HINT

    class Spy:
        def finditer(self, text):
            calls.append(len(text))
            return real.finditer(text)

    monkeypatch.setattr(secretstore, "_ESCAPE_HINT", Spy())
    scrubber = scrubber_for(REVIEW_VALUE)
    assert scrubber.scrub("&amp; " * 100_000) == "&amp; " * 100_000 and scrubber.holds("&amp; " * 100_000) is False
    assert calls == []
    assert scrubber.scrub(REVIEW_LINE) == '  - generic [ref=e3]: "{\\"user\\":\\"[secret:USER]\\"}"' and calls
    anchorless = scrubber_for("\u0418\u0432\u0430\u043d \u041f\u0435\u0442\u0440\u043e\u0432")
    calls.clear()
    anchorless.scrub(json_ascii("\u0418\u0432\u0430\u043d \u041f\u0435\u0442\u0440\u043e\u0432"))
    assert calls, "a value with no run has no words to look for in the text"


def test_a_value_hidden_by_a_script_is_at_most_four_thousand_characters():
    scrubber = secretstore.Scrubber({})
    scrubber.hide("TOKEN", "t" * 4096)
    with pytest.raises(secretstore.SecretStoreError, match="at most 4096"):
        scrubber.hide("TOKEN", "t" * 4097)


def test_a_name_of_markup_that_stands_for_two_characters_is_found():
    """`&NotEqualTilde;` is U+2242 and U+0338. What a text has for each of the two starts at the same place."""
    value = "x\u2242\u0338y-7492"
    assert html_module.unescape("&NotEqualTilde;") == "\u2242\u0338"
    scrubber = scrubber_for(value)
    assert scrubber.scrub("<p>x&NotEqualTilde;y-7492</p>") == f"<p>{MARKER}</p>"
    assert scrubber.scrub(yaml_playwright(json_ascii("<p>x&NotEqualTilde;y-7492</p>"))) == yaml_playwright(json_ascii(f"<p>{MARKER}</p>"))
    assert scrubber.scrub("<p>x&NotEqualTilde;y-7493</p>") == "<p>x&NotEqualTilde;y-7493</p>"


def _numbers_page(rows):
    """A snapshot of a page that is all numbers and escapes, with no secret in it: numbers that are the code points of letters
    show everywhere in it."""
    lines = ["### Snapshot", "```yaml", "- generic [ref=e1]:"]
    for n in range(rows):
        lines.append(f'  - link "Shoe {n}" [ref=e{3 * n + 2}]:')
        lines.append(f"    - /url: /item?id={n}&region=eu&q=red%20shoes+size%2042")
        lines.append(f'  - generic [ref=e{3 * n + 3}]: "Size \\"{40 + n % 8}\\" in stock"')
        lines.append(f"  - 'Note: it''s {n}' [ref=e{3 * n + 4}]")
        lines.append(f'  - code [ref=e{3 * n + 5}]: "{{\\"name\\":\\"caf\\\\u00e9\\",\\"path\\":\\"C:\\\\\\\\tmp\\"}}"')
    return "\n".join(lines + ["```"])


@pytest.mark.parametrize("value", ["\u0418\u0432\u0430\u043d \u041f\u0435\u0442\u0440\u043e\u0432", "\u043f\u0430\u0440\u043e\u043b\u044c1", "1\u043f\u0430\u0440\u043e\u043b\u044c", "\u65e5\u672c\u8a9e\u30d1\u30b9\u30ef\u30fc\u30c9"])
def test_a_page_of_numbers_and_escapes_is_not_taken_for_a_value_that_is_all_non_ascii_text(value):
    """The second review: a page with no secret in it, 370,000 characters of it, was replaced whole by the marker for these
    values, because each escape made a place and the work of decoding all of them went past the limit. The numbers of the
    page are in the evidence of a character, but not in the order of the characters of a value."""
    scrubber = scrubber_for(value)
    text = _numbers_page(1500)
    started = time.perf_counter()
    assert scrubber.scrub(text) == text
    assert time.perf_counter() - started < 3.0
    assert not scrubber.holds(text)
    for trace in scrubber._traces:
        assert places_of(trace, text) == []


def test_a_text_built_to_slow_the_search_for_the_bytes_of_a_character_costs_a_bounded_time():
    """The first byte and the last of a character are looked for with one gap between them: nothing nests, so a text that is all
    first bytes, or all of the bytes but the last, costs a pass of the gap for each of them and no more."""
    for value in ("x\U0001F600y-7492", "\u0418\u0432\u0430\u043d \u041f\u0435\u0442\u0440\u043e\u0432", "\u65e5\u672c\u8a9e\u30d1\u30b9\u30ef\u30fc\u30c9"):
        scrubber = scrubber_for(value)
        for hostile in ("f09f98" * 30_000, "%F0%9F%98" * 20_000, "d0" * 50_000 + "98", "e383" * 30_000, "&#x" * 40_000, "f0" * 1_000_000 + " %"):
            started = time.perf_counter()
            assert scrubber.scrub(hostile) == hostile and not scrubber.holds(hostile)
            assert time.perf_counter() - started < 5.0, (value, hostile[:12])


def test_a_value_that_is_all_non_ascii_text_is_found_in_every_form_on_a_page_of_numbers_and_escapes():
    value = "\u0418\u0432\u0430\u043d \u041f\u0435\u0442\u0440\u043e\u0432"
    scrubber = scrubber_for(value)
    page = _numbers_page(300).split("\n")
    forms = [
        value,
        json_ascii(value),
        url_strict(value),
        html_decimal(value),
        html_hex(value),
        css_escape(value),
        yaml_playwright(json_ascii(value)),
        quote_plus(json_ascii(value), safe=""),
        js_escape(value),
    ]
    for index, form in enumerate(forms):
        lines = list(page)
        lines.insert(40 + index * 90, f"  - text: {form} end")
        out = scrubber.scrub("\n".join(lines))
        assert out.count(MARKER) == 1 and "end" in out, (index, form)
        assert out == "\n".join(lines[: 40 + index * 90] + [f"  - text: {MARKER} end"] + lines[41 + index * 90:]) or "\u0418" not in out, (index, form)
        assert "\u0418" not in out and not scrubber.holds(out)


# --- the size of a place and the cost of a window ---------------------------------------------------------------------


def test_the_places_of_a_value_without_a_letter_or_a_digit_are_no_larger_than_a_window_when_escapes_are_everywhere(monkeypatch):
    monkeypatch.setattr(secretstore, "MAX_WINDOW", 5000)
    value = "!@#$"
    trace = secretstore._Trace(value, MARKER)
    text = "a %20 b " * 6000
    places = places_of(trace, text)
    reach = secretstore.REACH * len(value)
    assert len(places) > 5 and max(stop - start for start, stop, *_rest in places) <= max(5000, 2 * reach + 1)
    covered = set()
    for start, stop, *_rest in places:
        covered.update(range(start, stop))
    assert covered == set(range(len(text))), "the places overlap, so that a value across the end of one is inside the next"


def test_the_places_of_a_value_that_is_all_non_ascii_text_are_no_larger_than_a_window_when_its_characters_are_everywhere(monkeypatch):
    monkeypatch.setattr(secretstore, "MAX_WINDOW", 6000)
    value = "\u0418\u0432\u0430\u043d"
    trace = secretstore._Trace(value, MARKER)
    text = "\u0418\u0432\u0430\u043d %20 " * 3000
    places = places_of(trace, text)
    reach = secretstore.REACH * (len(value) + 1)
    assert len(places) > 5 and max(stop - start for start, stop, *_rest in places) <= max(6000, reach)
    scrubber = scrubber_for(value)
    layered = json_ascii(value)
    for at in range(5900, 6300, 11):
        shown = text[:at] + layered + text[at:]
        out = scrubber.scrub(shown)
        assert layered not in out and "\u0418\u0432\u0430\u043d" not in out, at


def test_a_value_across_the_end_of_a_window_of_a_text_full_of_escapes_is_scrubbed_exactly(monkeypatch):
    monkeypatch.setattr(secretstore, "MAX_WINDOW", 5000)
    value = "!@#$"
    scrubber = scrubber_for(value)
    layered = yaml_playwright(json_ascii(value))
    for at in range(4900, 5200, 7):
        text = "a %20 b " * 2000
        text = text[:at] + layered + text[at:]
        assert scrubber.scrub(text) == text[:at] + MARKER + text[at + len(layered):], at


def test_a_window_that_the_budget_cannot_pay_a_pass_for_is_not_decoded(monkeypatch):
    layer = _Layer.whole("\\n" * 100 + "x" * 50, 0)
    budget = secretstore._Budget(100)
    assert list(_decodings(layer, False, secretstore._DECODERS, budget)) == [] and budget.out and budget.left == 100
    budget = secretstore._Budget(10_000)
    assert sorted(made.text for made in _decodings(layer, False, secretstore._DECODERS, budget)) == [
        "\n" * 100 + "x" * 50, "n" * 100 + "x" * 50,
    ], "a string reads a newline, and CSS reads a letter that is no hex digit"
    assert not budget.out and budget.left < 10_000
    budget = secretstore._Budget(len(layer.text) + 20)  # the pass is paid for, and the tokens are not
    assert list(_decodings(layer, False, secretstore._DECODERS, budget)) == [] and budget.out
    passes = []
    real = _Layer.decoded
    monkeypatch.setattr(_Layer, "decoded", lambda self, *args, **kwargs: passes.append(len(self.text)) or real(self, *args, **kwargs))
    assert list(_decodings(layer, False, secretstore._DECODERS, secretstore._Budget(len(layer.text) - 1))) == []
    assert passes == [], "a text that the budget cannot pay a pass over is not decoded at all, which is the copy and the arrays"


def test_a_text_that_is_larger_than_the_limit_is_not_copied_to_be_decoded(monkeypatch):
    sizes = []
    real = secretstore._Layer.whole.__func__
    monkeypatch.setattr(secretstore._Layer, "whole", classmethod(lambda cls, text, offset: sizes.append(len(text)) or real(cls, text, offset)))
    monkeypatch.setattr(secretstore, "WORK_LIMIT", 3000)
    scrubber = scrubber_for("!@#$")
    text = "a %20 b " * 5000 + yaml_playwright(json_ascii("!@#$")) + " tail"
    out = scrubber.scrub(text)
    assert sizes == [] or max(sizes) <= 3000, sizes
    assert "!@#$" not in out and MARKER in out and not scrubber.holds(out)


# --- what it costs --------------------------------------------------------------------------------------------


def test_text_with_escapes_and_no_value_costs_little_to_scrub():
    scrubber = secretstore.Scrubber({
        "A": {"value": REVIEW_VALUE, "origins": [ORIGIN]},
        "B": {"value": "alex.tester@example.com", "origins": [ORIGIN]},
        "C": {"value": "482916", "origins": [ORIGIN]},
    })
    text = '<div class="x">some page text with words 1234, "quoted" \\ and 50% &amp; done</div>\n' * 12_000
    started = time.perf_counter()
    assert scrubber.scrub(text) == text
    assert time.perf_counter() - started < 3.0


def test_a_text_that_repeats_the_words_of_a_value_and_never_the_value_costs_a_bounded_time():
    scrubber = secretstore.Scrubber({"A": {"value": REVIEW_VALUE, "origins": [ORIGIN]}})
    text = "Synthetic User \\\" A7492 &amp; %20 " * 20_000 + "Synthetic\\\"User\\\\A749"
    started = time.perf_counter()
    scrubber.scrub(text)
    assert time.perf_counter() - started < 15.0


def test_many_copies_of_the_value_in_two_layers_cost_a_bounded_time_and_every_one_is_scrubbed():
    scrubber = scrubber_for(REVIEW_VALUE)
    one = yaml_playwright(json_ascii(REVIEW_VALUE))
    text = f"[{one}]" * 4_000
    started = time.perf_counter()
    out = scrubber.scrub(text)
    assert time.perf_counter() - started < 15.0
    assert out == f"[{MARKER}]" * 4_000


def test_so_many_copies_that_the_work_limit_is_reached_leave_none_of_them_and_cost_a_bounded_time():
    """Past the limit the places that were not decoded are scrubbed whole, and copies that touch are one stretch."""
    scrubber = scrubber_for(REVIEW_VALUE)
    one = yaml_playwright(json_ascii(REVIEW_VALUE))
    text = f"[{one}]" * 40_000
    started = time.perf_counter()
    out = scrubber.scrub(text)
    assert time.perf_counter() - started < 15.0
    assert "Synthetic" not in out and "A7492" not in out and not scrubber.holds(out)
    assert out.startswith(f"[{MARKER}]") and out.count(MARKER) >= 1000


# --- every path that returns text ------------------------------------------------------------------------------


def _layered_user():
    """The review's value, and the text a page makes of it: JSON in an element, then a snapshot of the element."""
    return REVIEW_VALUE, REVIEW_LINE, json_ascii(REVIEW_VALUE)


def _store_user(monkeypatch, tmp_path, snapshot=None, says=None):
    _with_secret(monkeypatch, tmp_path)
    secretstore.set_secret("seat", "USER", REVIEW_VALUE, [ORIGIN])
    if snapshot is not None:
        monkeypatch.setattr(commands.session, "snapshot", lambda seat: snapshot)
    return Saying(monkeypatch, says or {})


class Saying(FakeCli):
    """FakeCli that answers the verbs named in `says` with a result of its own."""

    def __init__(self, monkeypatch, says, **kwargs):
        super().__init__(monkeypatch, **kwargs)
        self.says = says

    def run(self, cmd, **kwargs):
        args = cmd[2:]
        if args and args[0] in self.says:
            self.calls.append(list(cmd))
            return _Completed(0, "### Result\n" + self.says[args[0]] + "\n")
        return super().run(cmd, **kwargs)


def _clean(run):
    text = _everything(run)
    assert "Synthetic" not in text and "A7492" not in text, text
    return text


def test_a_snapshot_the_script_asks_for_is_scrubbed_and_the_attachment_does_not_decode_back(monkeypatch, tmp_path):
    """The probe of the review, at the level of the script: the attachment of `snap` is the one that held it."""
    _store_user(monkeypatch, tmp_path, snapshot=REVIEW_SNAPSHOT)
    run = commands.run_script("seat", "snap")
    assert run.ok, run.error
    (path,) = run.files
    with open(path) as handle:
        attached = handle.read()
    line = next(line for line in attached.splitlines() if "generic [ref=e3]:" in line)
    recovered = json.loads(json.loads(line.split(": ", 1)[1]))["user"]
    assert recovered != REVIEW_VALUE and recovered == MARKER
    _clean(run)


def test_the_text_of_an_element_is_scrubbed(monkeypatch, tmp_path):
    cli = _store_user(monkeypatch, tmp_path)
    cli.fill = _Completed(0, json.dumps({"value": '{"user":"' + json_ascii(REVIEW_VALUE) + '"}'}) + "\n")
    run = commands.run_script("seat", "text body")
    assert run.ok, run.error
    assert run.steps[0]["output"] == '{"user":"[secret:USER]"}'
    _clean(run)


@pytest.mark.parametrize("verb, line", [("find", "find word"), ("requests", "requests"), ("console", "console")])
def test_the_output_of_a_verb_that_prints_what_the_driver_says_is_scrubbed(monkeypatch, tmp_path, verb, line):
    said = f'GET http://127.0.0.1/?q={quote(json_ascii(REVIEW_VALUE), safe="")} and {REVIEW_LINE}'
    cli = _store_user(monkeypatch, tmp_path, says={verb: said})
    run = commands.run_script("seat", line)
    assert run.ok, run.error
    assert run.steps[0]["output"].count(MARKER) == 2
    _clean(run)


def test_the_console_log_that_the_script_attaches_is_scrubbed(monkeypatch, tmp_path):
    cli = _store_user(monkeypatch, tmp_path, says={"console": REVIEW_LINE})
    run = commands.run_script("seat", "console")
    assert run.ok, run.error
    (path,) = run.files
    with open(path) as handle:
        assert handle.read() == '- generic [ref=e3]: "{\\"user\\":\\"[secret:USER]\\"}"\n'


def test_the_address_of_the_page_is_scrubbed(monkeypatch, tmp_path):
    _store_user(monkeypatch, tmp_path)
    layered = quote_plus(json_ascii(REVIEW_VALUE), safe="")
    monkeypatch.setattr(commands.session, "current_url", lambda seat: f"https://example.com/?next={layered}&x=1")
    run = commands.run_script("seat", "url")
    assert run.steps[0]["output"] == f"https://example.com/?next={MARKER}&x=1"
    _clean(run)


def test_the_snapshot_a_failure_attaches_is_scrubbed(monkeypatch, tmp_path):
    _store_user(monkeypatch, tmp_path, snapshot=REVIEW_SNAPSHOT)
    run = commands.run_script("seat", "bogus")
    assert not run.ok
    (path,) = run.files
    with open(path) as handle:
        assert "Synthetic" not in handle.read()
    _clean(run)


@pytest.mark.parametrize("name, text", [
    ("page-2026-10-25T05-10-09-626Z.yml", REVIEW_SNAPSHOT),
    ("console-2026-10-25T05-10-09-626Z.log", f"[    4001ms] [LOG] {json_ascii(REVIEW_VALUE)}\n[    4002ms] [LOG] {REVIEW_LINE}\n"),
])
def test_a_file_the_driver_wrote_is_scrubbed_before_the_script_begins(monkeypatch, tmp_path, name, text):
    cli = _store_user(monkeypatch, tmp_path)
    path = os.path.join(_cli_dir("seat"), name)
    with open(path, "w") as handle:
        handle.write(text)
    run = commands.run_script("seat", "url")
    assert run.ok, run.error
    with open(path) as handle:
        after = handle.read()
    assert "Synthetic" not in after and "A7492" not in after and MARKER in after


def test_a_file_is_scrubbed_in_place_when_the_value_is_in_it_under_layers(tmp_path):
    scrubber = scrubber_for(REVIEW_VALUE)
    target = tmp_path / "console.log"
    target.write_text(f"[LOG] {REVIEW_LINE}\n[LOG] ok\n")
    assert scrubber.scrub_file(str(target)) is True
    assert target.read_text() == f'[LOG]   - generic [ref=e3]: "{{\\"user\\":\\"{MARKER}\\"}}"\n[LOG] ok\n'


def test_a_file_that_is_not_utf8_and_has_the_value_under_layers_is_removed_when_asked(tmp_path):
    scrubber = scrubber_for(REVIEW_VALUE)
    target = tmp_path / "console.log"
    target.write_bytes(b"[LOG] \xff\xfe " + REVIEW_LINE.encode() + b"\n")
    assert scrubber.scrub_file(str(target), remove_on_failure=True) is True
    assert not target.exists()


def test_the_cli_prints_a_snapshot_with_the_value_under_layers_scrubbed(monkeypatch, capsys):
    from test_cli import _run

    secretstore.set_secret("s", "USER", REVIEW_VALUE, [ORIGIN])
    monkeypatch.setattr(session, "ensure_running", lambda seat, script_epoch=None: None)
    monkeypatch.setattr(session, "snapshot", lambda seat: REVIEW_SNAPSHOT)
    assert _run(monkeypatch, "snap", "--seat", "s") == 0
    out = capsys.readouterr().out
    assert "Synthetic" not in out and "A7492" not in out and out.count(MARKER) == 2


def test_a_command_line_that_holds_the_value_under_layers_is_refused_and_nothing_runs(monkeypatch, tmp_path):
    from test_command_line import Spawns, guard

    spawns = Spawns(monkeypatch)
    layered = quote_plus(json_ascii(REVIEW_VALUE), safe="")
    with plc.protecting("seat", guard(USER=REVIEW_VALUE)):
        for arg in (REVIEW_LINE, json_ascii(REVIEW_VALUE), f"https://example.com/?u={layered}", f"#{css_escape(json_ascii(REVIEW_VALUE))}"):
            with pytest.raises(plc.BrowserError, match="must not be in one"):
                plc.run("seat", "goto", arg)
    assert spawns.argv == []


def test_a_recipe_that_hides_a_value_it_read_and_returns_it_under_layers_shows_none_of_it(monkeypatch, tmp_path):
    """The text, the data, the artifact and the line of the transcript of a verb: the value is JSON inside a string inside
    JSON, as a verb that returns a debug dump of what it read would make it."""
    _store_user(monkeypatch, tmp_path)
    rec = Recipe("shop", "A shop", origins=[ORIGIN])

    @rec.verb("get", help="Get.")
    def get(page, args):
        page.hide("PASSCODE", 'Zebra"Quilt\\M3317')
        inner = json.dumps({"code": 'Zebra"Quilt\\M3317'})
        page.artifact("dump.txt", "dump " + json.dumps({"text": inner}))
        return Result(text="shown " + inner, data={"debug": inner, "dump": json.dumps({"debug": inner}), "n": 1})

    run = commands.run_script("seat", "recipe shop get", recipes=static_library(rec))
    assert run.ok, run.error
    everything = _everything(run)
    assert "Zebra" not in everything and "M3317" not in everything, everything
    assert everything.count("[secret:PASSCODE]") >= 4
    assert any(path.endswith("-result.json") for path in run.files) and any(path.endswith("dump.txt") for path in run.files)


# --- the preview of an element in the call log of Playwright: a line feed is U+21B5 and a tab is U+21C6 -----------------
#
# `previewNode` ends in `oneLine(s) = s.replace(/\n/g, "↵").replace(/\t/g, "⇆")`. A value with a line feed or a
# tab that a page puts in an attribute is there, in the call log of an error, in the shape of the arrows.

ARROWS = [("\n", "↵"), ("\t", "⇆")]


def one_line(text):
    """What Playwright's `oneLine` makes of a text."""
    return text.replace("\n", "↵").replace("\t", "⇆")


def un_one_line(text):
    return text.replace("↵", "\n").replace("⇆", "\t")


@pytest.mark.parametrize("char, arrow", ARROWS)
def test_a_line_feed_and_a_tab_may_each_be_written_as_the_arrow_of_the_preview_and_no_other_character_may(char, arrow):
    assert arrow in secretstore._escapes_of(char)
    assert char in secretstore._escapes_of(char), "and as itself, as before"
    for other in (" ", "\r", "\x0b", "a", "7", " "):
        assert arrow not in secretstore._escapes_of(other), repr(other)
    assert secretstore._one_line(f"a{char}b") == f"a{arrow}b"
    assert secretstore._one_line("a\rb c") == "a\rb c", "the two it changes, and not the others"


@pytest.mark.parametrize("char, arrow", ARROWS)
def test_the_value_with_the_character_is_scrubbed_where_the_preview_shows_it_as_the_arrow(char, arrow):
    value = f"line1{char}line2-7492"
    scrubber = scrubber_for(value)
    shown = f'<input id="t" data-note="line1{arrow}line2-7492" disabled/>'
    assert scrubber.scrub(shown) == f'<input id="t" data-note="{MARKER}" disabled/>'
    assert scrubber.holds(shown) and scrubber.is_value(f"line1{arrow}line2-7492")
    assert scrubber.scrub(f"<p>{value}</p>") == f"<p>{MARKER}</p>", "as typed, as before"
    # and a mix: the arrow for one, the escape of a string for another
    other = "\t" if char == "\n" else "\n"
    mixed = scrubber_for(f"a{char}b{other}c-7492")
    assert mixed.scrub(f"x a{arrow}b{json.dumps(other)[1:-1]}c-7492 y") == f"x {MARKER} y"
    assert mixed.scrub(f"x a{arrow}b{one_line(other)}c-7492 y") == f"x {MARKER} y"


@pytest.mark.parametrize("char, arrow", ARROWS)
@pytest.mark.parametrize("ellipsis", ["…", "..."])
@pytest.mark.parametrize("many", [10**9, 0])
def test_a_preview_that_cut_the_value_after_the_arrow_is_found_by_its_start(monkeypatch, char, arrow, ellipsis, many):
    """Playwright cuts the attributes of the preview at 500 characters and the text of the element at 50, and ends the
    cut with an ellipsis: the value is there up to the cut, the arrow in it."""
    monkeypatch.setattr(secretstore, "MANY_ELLIPSES", many)
    value = f"line1{char}line2{char}lantern-7492-zephyr"
    scrubber = scrubber_for(value)
    for count in range(secretstore.MIN_VALUE_LENGTH, len(value)):
        shown = one_line(value[:count])
        assert scrubber.scrub(f'<button id="b" data-note="{shown}{ellipsis}</button>') == f'<button id="b" data-note="{MARKER}</button>', count


@pytest.mark.parametrize("char, arrow", ARROWS)
def test_a_long_value_with_the_character_is_found_by_its_preview_whole_and_in_pieces(char, arrow):
    """A value over 4,096 characters is matched by its whole forms and, a character at a time, in pieces: both know the arrow."""
    import random

    rng = random.Random(11)
    value = "".join(rng.choice("abcdefghij0123456789 -_") + (char if rng.random() < 0.02 else "") for _ in range(5000))
    scrubber = scrubber_for(value)
    assert char in value
    assert scrubber.scrub("#text=" + one_line(value) + " end") == f"#text={MARKER} end"
    assert re.sub(r"(?:\[secret:USER\])+", MARKER, scrubber.scrub("#text=" + value[:3000] + one_line(value[3000:]) + " end")) == f"#text={MARKER} end"
    assert "#text=" + one_line(value[:200]) + "…" not in scrubber.scrub("#text=" + one_line(value[:200]) + "…")


@pytest.mark.parametrize("char, arrow", ARROWS)
def test_the_arrow_is_found_under_the_layers_a_reply_puts_around_the_preview(char, arrow):
    """The error of the driver is quoted by what carries it: JSON, a URL, markup, a selector. The arrow is one character
    to all of them."""
    value = f"Marsh{char}Lantern-5047-Zephyr"
    scrubber = scrubber_for(value)
    shown = one_line(value)
    for name in ("json", "json_utf8", "yaml", "yaml_py", "yaml_single", "url", "url_plus", "url_component", "js_escape", "html",
                 "html_decimal", "html_hex", "css"):
        encode, before, after = ENCODERS[name]
        text = "x " + before + encode(shown) + after + " y"
        assert scrubber.scrub(text) == "x " + before + MARKER + after + " y", (name, text)
        assert not scrubber.holds(scrubber.scrub(text))


def test_the_arrows_of_the_preview_are_the_innermost_layer_of_every_composition_of_up_to_three(monkeypatch):
    """Generated, like the table above: the preview is made first (the arrows), and up to two more writers of the 18 put the
    result in their syntax. For each value that has a line feed or a tab in it, the scrub must be the text with exactly the
    stretch of the value made its marker, and the value must not come back when the text is decoded."""
    names = list(ENCODERS)
    monkeypatch.setitem(ENCODERS, "one_line", (one_line, '<input id="b" data-note="', '">'))
    monkeypatch.setitem(INVERSE, "one_line", un_one_line)
    values = [value for value in VALUES if "\n" in value or "\t" in value]
    assert len(values) >= 3
    sequences = [("one_line",)] + [("one_line", a) for a in names] + [("one_line", a, b) for a in names for b in names]
    assert len(sequences) == 1 + 18 + 324
    SKIPPED[0] = 0
    bad = wrong(values, sequences)
    assert not bad, report(bad)
    assert SKIPPED[0] < 0.05 * len(sequences) * len(values)
