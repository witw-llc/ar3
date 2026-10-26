"""The secrets a browser seat may type into a page, and the scrub that keeps them out of replies.

A seat's secrets live in one file beside its profile, `secrets.json`, mode 0600.
The operator writes it with `browser secret set`. Each secret carries the origins
it may be typed on. A script names a secret (`fill-secret e9 PASSWORD`) and the
tool types the value there, so a sender can use a secret without ever reading
it. No script verb sets, lists or reads one.

The module is named for what it holds: `secrets` would shadow the standard
library module of that name.
"""
import bisect
import functools
import heapq
import html
import ipaddress
import json
import os
import re
import stat
import threading
from array import array
from html.entities import html5 as _HTML5_ENTITIES
from urllib.parse import quote, quote_plus, urlsplit

from . import session

FILE = "secrets.json"
VERSION = 1
NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
MIN_VALUE_LENGTH = 4
DEFAULT_PORTS = {"http": "80", "https": "443"}
HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class SecretStoreError(Exception):
    """The store cannot be read or written. A message never holds a secret value."""


def path(seat):
    return os.path.join(session.seat_home(seat), FILE)


def check_name(name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise SecretStoreError(
            "a secret name starts with a capital letter and holds only A-Z, 0-9 and _, "
            "64 characters at most"
        )


def check_value(value):
    if not isinstance(value, str) or len(value) < MIN_VALUE_LENGTH:
        raise SecretStoreError(
            f"a secret value is at least {MIN_VALUE_LENGTH} characters: "
            "a shorter one cannot be scrubbed from replies safely"
        )
    if "\0" in value:
        raise SecretStoreError("a secret value cannot hold a NUL character")


def _origin_parts(text):
    """(scheme, host, port, wildcard) of an origin as the operator wrote it: the
    scheme and host lower case, `port` empty for the scheme's default, and
    `wildcard` set when the host began with `*.` (then `host` is the domain, with
    two labels at least: a wildcard on a top-level domain would cover every site
    under it, and only `*.localhost` is let through).
    Anything that is not scheme://host[:port] is an error that does not repeat
    the text, which could be a value typed in the wrong place."""
    if not isinstance(text, str) or not text.isprintable() or " " in text:
        raise SecretStoreError("an origin is text such as https://app.example.com")
    scheme, separator, rest = text.partition("://")
    scheme = scheme.lower()
    if not separator or scheme not in DEFAULT_PORTS:
        raise SecretStoreError("an origin starts with http:// or https://")
    if any(char in rest for char in "/?#@\\"):
        raise SecretStoreError("an origin has no path, query, fragment or user name: scheme://host[:port]")
    if rest.startswith("["):
        closing = rest.find("]")
        if closing < 0:
            raise SecretStoreError("an origin host is an ASCII domain name or an IP address")
        cut = closing + 1
    else:
        colon = rest.find(":")
        cut = colon if colon >= 0 else len(rest)
    host, tail = rest[:cut].lower(), rest[cut:]
    port = ""
    if tail:
        digits = tail[1:]
        if not tail.startswith(":") or not digits.isascii() or not digits.isdigit():
            raise SecretStoreError("an origin is scheme://host[:port], and the port is a number")
        if not 1 <= int(digits) <= 65535:
            raise SecretStoreError("an origin port is a number from 1 to 65535")
        port = str(int(digits))
    wildcard = host.startswith("*.")
    if wildcard:
        host = host[2:]
    if "*" in host:
        raise SecretStoreError("a wildcard is a leading *. before a domain name, as in https://*.example.org")
    if host.startswith("["):
        try:
            host = f"[{ipaddress.IPv6Address(host[1:-1]).compressed}]"
        except ValueError:
            raise SecretStoreError("an origin host is an ASCII domain name or an IP address") from None
    elif not host.isascii() or len(host) > 253 or not all(HOST_LABEL.fullmatch(label) for label in host.split(".")):
        raise SecretStoreError("an origin host is an ASCII domain name or an IP address")
    if wildcard and (host.startswith("[") or re.fullmatch(r"[0-9.]+", host)):
        raise SecretStoreError("a wildcard (*.) goes before a domain name, not an IP address")
    if wildcard and host != "localhost" and len(host.split(".")) < 2:
        raise SecretStoreError(
            "a wildcard needs at least two labels after *., as in https://*.example.com or "
            "https://*.co.uk: a wildcard on one label, as in https://*.com, would match every site "
            "under it. https://*.localhost is the one exception"
        )
    return scheme, host, "" if port == DEFAULT_PORTS[scheme] else port, wildcard


def parse_origin(text):
    """The normalized text of an origin: scheme://host[:port], where a leading `*.`
    in the host matches any subdomain but not the bare domain."""
    scheme, host, port, wildcard = _origin_parts(text)
    return f"{scheme}://{'*.' if wildcard else ''}{host}{':' + port if port else ''}"


def origin_parts(origin):
    """The parts of a normalized origin, in the shape the in-page check compares."""
    scheme, host, port, wildcard = _origin_parts(origin)
    return {"scheme": scheme, "host": host, "port": port, "wildcard": wildcard}


def check_origins(origins):
    """The normalized origins, each once, in the order given. A secret needs one at least."""
    if not isinstance(origins, (list, tuple)) or not origins:
        raise SecretStoreError(
            "a secret needs at least one origin it may be typed on, such as --origin https://app.example.com"
        )
    normalized = []
    for origin in origins:
        text = parse_origin(origin)
        if text not in normalized:
            normalized.append(text)
    return normalized


def _url_origin(url):
    """(scheme, host, port) of an http(s) URL, written the way `origin_parts` writes an origin
    (`port` empty for the scheme's default), or None for anything else. A URL that a browser and
    Python could read two ways is None, so a check made here holds for the request the browser
    makes. Those are a backslash, a space or other control or non-ASCII character, and user
    information."""
    if not isinstance(url, str) or not url.isascii() or not url.isprintable() or " " in url or "\\" in url:
        return None
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if scheme not in DEFAULT_PORTS or not host or "@" in parts.netloc:
        return None
    if ":" in host:
        try:
            host = f"[{ipaddress.IPv6Address(host).compressed}]"
        except ValueError:
            return None
    elif not all(HOST_LABEL.fullmatch(label) for label in host.split(".")):
        return None
    return scheme, host, "" if port is None or str(port) == DEFAULT_PORTS[scheme] else str(port)


def url_on_origins(url, origins):
    """True when `url` is an http(s) URL on one of `origins`, given as `origin_parts` makes them.
    The rule is the one `plc.fill_secret_code` applies in the page: scheme and port equal, host
    equal, or for a wildcard any subdomain of the domain and not the domain itself."""
    found = _url_origin(url)
    if found is None:
        return False
    scheme, host, port = found
    return any(
        origin["scheme"] == scheme and origin["port"] == port
        and (host.endswith("." + origin["host"]) if origin["wildcard"] else host == origin["host"])
        for origin in origins
    )


def read(seat):
    """The seat's {NAME: {"value": ..., "origins": [...]}}. A missing file is an
    empty store; a file open to group or other users, or one that is not a
    version 1 store, is an error."""
    target = path(seat)
    try:
        descriptor = os.open(target, os.O_RDONLY)
    except (FileNotFoundError, NotADirectoryError):
        return {}
    except OSError as exc:
        raise SecretStoreError(f"secret store {target} cannot be read: {_why(exc)}") from None
    try:
        with os.fdopen(descriptor, "rb") as handle:
            mode = stat.S_IMODE(os.fstat(handle.fileno()).st_mode)
            if mode & 0o077:
                raise SecretStoreError(
                    f"secret store {target} is open to group or other users (mode {mode:04o}); "
                    f"fix it with: chmod 600 {target}"
                )
            raw = handle.read()
    except OSError as exc:
        raise SecretStoreError(f"secret store {target} cannot be read: {_why(exc)}") from None
    return _parse(target, raw)


def _why(exc):
    return exc.strerror or type(exc).__name__


def _parse(target, raw):
    corrupt = SecretStoreError(
        f"secret store {target} is not a valid secret store; repair it or remove the file"
    )
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise corrupt from None
    if not isinstance(data, dict) or not isinstance(data.get("secrets"), dict):
        raise corrupt
    version = data.get("version")
    if type(version) is not int:
        raise corrupt
    if version > VERSION:
        raise SecretStoreError(
            f"secret store {target} is version {version}; this tool reads version {VERSION}"
        )
    if version != VERSION:
        raise corrupt
    store = {}
    try:
        for name, entry in data["secrets"].items():
            check_name(name)
            if not isinstance(entry, dict):
                raise corrupt
            check_value(entry.get("value"))
            store[name] = {"value": entry["value"], "origins": check_origins(entry.get("origins"))}
    except SecretStoreError:
        raise corrupt from None
    return store


def _discard(target):
    """Remove `target`, or empty it when it cannot be removed. True when it no longer holds its text."""
    try:
        os.unlink(target)
        return True
    except OSError:
        pass
    try:
        os.truncate(target, 0)
        return True
    except OSError:
        return False


def _create_aside(aside):
    """A new 0600 file at `aside`, the name only this process and thread use. A
    leftover of a run that died with the same ids is replaced."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        return os.open(aside, flags, 0o600)
    except FileExistsError:
        os.unlink(aside)
        return os.open(aside, flags, 0o600)


def save(seat, store):
    """Write the whole store: a 0600 file made beside the target, then renamed over it."""
    target = path(seat)
    directory = os.path.dirname(target)
    session.private_dir(directory)
    body = json.dumps({"version": VERSION, "secrets": dict(sorted(store.items()))}, indent=2)
    aside = os.path.join(directory, f".{FILE}.{os.getpid()}.{threading.get_ident()}.tmp")
    descriptor = _create_aside(aside)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(aside, target)
    except BaseException:
        try:
            os.unlink(aside)
        except OSError:
            pass
        raise


def set_secret(seat, name, value, origins):
    """Store `value` under `name`, to be typed only on `origins`. A later set replaces both."""
    check_name(name)
    check_value(value)
    origins = check_origins(origins)
    store = read(seat)
    store[name] = {"value": value, "origins": origins}
    save(seat, store)


def remove(seat, name):
    """Drop `name`. False when the store never held it."""
    check_name(name)
    store = read(seat)
    if name not in store:
        return False
    del store[name]
    save(seat, store)
    return True


_YAML_ESCAPES = {
    "\\": "\\\\", '"': '\\"', "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t",
}
_YAML_SPECIAL = re.compile(r'["\\\x00-\x1f\x7f-\x9f]')
# What JavaScript's \s matches. Playwright tidies a field's value with it for a snapshot.
_JS_WHITESPACE = re.compile("[\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+")


def _yaml_quoted(value):
    """The inside of the double-quoted scalar a Playwright snapshot prints for a
    value that needs quotes."""
    return _YAML_SPECIAL.sub(
        lambda match: _YAML_ESCAPES.get(match.group()) or "\\x%02x" % ord(match.group()), value
    )


def _tidied(value):
    """A value as a snapshot shows it: no zero-width spaces, each run of whitespace
    one space, none at either end."""
    return _JS_WHITESPACE.sub(" ", value.replace("\u200b", "")).strip(" ")


# The whitespace that a JSON or YAML string leaves as it is: the others (`\t \n \v \f \r`) are escapes.
_RAW_WHITESPACE = re.compile("[ \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+")


def _tidied_in_a_string(value):
    """A value as a snapshot shows it when a page printed it in a string, JSON or YAML: each run of the whitespace that the
    string leaves raw is one space, the ones it escaped are as they were, and the ends are not touched."""
    return _RAW_WHITESPACE.sub(" ", value.replace("\u200b", ""))


def _css_escaped(text):
    """`CSS.escape(text)` as a browser computes it (CSSOM, "serialize an identifier"). A page that makes an id
    or a class from a value, and a tool that names the element by it, print the value in this form."""
    out = []
    for index, char in enumerate(text):
        code = ord(char)
        digit = 0x30 <= code <= 0x39
        if code == 0:
            out.append("\ufffd")
        elif code <= 0x1F or code == 0x7F or (index == 0 and digit) or (index == 1 and digit and text[0] == "-"):
            out.append(f"\\{code:x} ")
        elif index == 0 and char == "-" and len(text) == 1:
            out.append("\\-")
        elif code >= 0x80 or char in "-_" or digit or 0x41 <= code <= 0x5A or 0x61 <= code <= 0x7A:
            out.append(char)
        else:
            out.append("\\" + char)
    return "".join(out)


def _one_line(text):
    """`text` as the preview of an element in the call log of Playwright shows it (`oneLine`): a line feed as U+21B5 and a
    tab as U+21C6, the only two it changes."""
    return text.replace("\n", "\u21b5").replace("\t", "\u21c6")


def _forms(value):
    """The shapes `value` takes in text a page or Playwright prints, one for each way to write the whole value: as
    typed, as a snapshot tidies it, as a preview of an element shows it, and escaped for JSON, YAML, CSS, a URL or
    markup. A value too long to match one character at a time (`_pieces_of`) is matched by these."""
    forms = set()
    for text in {value, _tidied(value), _tidied_in_a_string(value)}:
        data = text.encode("utf-8", "surrogatepass")  # a page can have a lone surrogate in a string, and `quote` would raise
        forms.update((
            text,
            _one_line(text),
            json.dumps(text)[1:-1],
            json.dumps(text, ensure_ascii=False)[1:-1],
            _yaml_quoted(text),
            _css_escaped(text),
            quote(data, safe=""),
            quote_plus(data),
            html.escape(text),
        ))
    return {form for form in forms if len(form) >= MIN_VALUE_LENGTH}


def _hex(code, width, prefix, suffix=""):
    """`prefix` and `code` in hex, in both cases: a hex escape is written in either."""
    return {f"{prefix}{code:0{width}x}{suffix}", f"{prefix}{code:0{width}X}{suffix}"}


# The escapes of one letter that YAML and Python write for a character besides the ones of JSON (`\\b \\f \\n \\r \\t`), and
# the one that PHP writes for a slash.
_LETTER_ESCAPES = {
    "\0": "\\0", "\a": "\\a", "\v": "\\v", "\x1b": "\\e", "\x85": "\\N", "\xa0": "\\_", "\u2028": "\\L", "\u2029": "\\P",
    "/": "\\/",
}


# The names of markup that stand for one character other than a letter or a digit: `&excl;`, `&eacute;`.
_NAMED = {}
for _name, _chars in _HTML5_ENTITIES.items():
    if _name.endswith(";") and len(_chars) == 1 and not (_chars.isascii() and _chars.isalnum()):
        _NAMED.setdefault(_chars, []).append(_name)


@functools.lru_cache(maxsize=8192)
def _escapes_of(char):
    """Every text that can stand for `char` in text a page, a browser or Playwright prints: itself, and each way a
    string (JSON, JavaScript, YAML, Python), a selector (CSS), a URL (the percent escape of its UTF-8 bytes, and `+`
    for a space) or markup (an entity) writes it. A letter or a digit is only itself (a digit has a CSS hex escape
    too: `CSS.escape` writes one at the start of an identifier). A text is a mix of these: a URL keeps `@` and
    `!` and escapes a space; an id made of a prefix and a value escapes the value's `!` and not its digits."""
    code = ord(char)
    found = {char}
    if char.isascii() and char.isalnum():
        if char.isdigit():
            found.update(_hex(code, 1, "\\", " "))
        return frozenset(found)
    found.update((
        json.dumps(char)[1:-1], json.dumps(char, ensure_ascii=False)[1:-1], _yaml_quoted(char), html.escape(char),
        f"&#{code};",
    ))
    found.update(f"&{name}" for name in _NAMED.get(char, ()))
    found.update(_hex(code, 1, "&#x", ";"))
    found.update(_hex(code, 1, "\\", " "))  # CSS: a hex escape, ended by a space
    if char.isascii() and char.isprintable():
        found.add("\\" + char)  # CSS: a backslash before the character
    if char in _LETTER_ESCAPES:
        found.add(_LETTER_ESCAPES[char])
    if code < 0x100:
        found.update(_hex(code, 2, "\\x"))  # JavaScript, YAML, Python
    if code < 0x10000:
        found.update(_hex(code, 4, "\\u"))
    else:
        found.update(_hex(code, 8, "\\U"))
        found.update(_hex(code, 1, "\\u{", "}"))
    try:
        encoded = char.encode("utf-8")
    except UnicodeEncodeError:
        encoded = b""
    if encoded:
        found.add("".join(f"%{byte:02X}" for byte in encoded))
        found.add("".join(f"%{byte:02x}" for byte in encoded))
    if 0x80 <= code < 0x100:
        found.update(_hex(code, 2, "%"))  # JavaScript's `escape`: the Latin-1 byte
    elif 0x100 <= code < 0x10000:
        found.update(_hex(code, 4, "%u"))  # and the UTF-16 unit
    elif code >= 0x10000:
        high, low = 0xD800 + ((code - 0x10000) >> 10), 0xDC00 + ((code - 0x10000) & 0x3FF)
        found.update(first + second for first in _hex(high, 4, "%u") for second in _hex(low, 4, "%u"))
    if char == " ":
        found.add("+")
    if char == "\u00a0":
        found.add("&nbsp;")
    if char == "\n":
        found.add("\u21b5")  # the preview of an element in the call log of Playwright (`oneLine`)
    if char == "\t":
        found.add("\u21c6")  # and a tab, in the same
    if char == "'":
        found.update(("\\'", "&#39;", "&apos;", "''"))  # the last one is how YAML doubles it in single quotes
    if code == 0:
        found.add("\ufffd")  # what CSS.escape makes of a NUL
    return frozenset(found)


# A value longer than this is matched by its whole forms (`_forms`) and by patterns a character at a time of pieces of it that
# are no longer than this: one pattern for all of it would be huge.
MAX_PATTERN_VALUE = 4096
# A value longer than this has its whole forms and no more (a pattern of a piece costs a tenth of a second for each thousand characters).
MAX_PIECED_VALUE = 16384
# A value a script hides is a passcode or a token, and not text of a page: it is made into patterns at once, and every hide makes them all again.
MAX_HIDDEN_VALUE = 4096
# How far before an ellipsis the start of a cut value can begin, for a value of 43 characters or fewer. Playwright cuts the text
# of an element in the call log of an error at 50 characters, and the attributes of an element at 500, and ends each with
# an ellipsis; 20 characters more are for the escapes in them. A cut of a longer value can show as much of it as the tool
# shows, so its window is MAX_CUT_RATIO characters for each character of the value, at least this.
MAX_CUT_WINDOW = 520
# The most characters of a text that one character of a value takes in one layer, near enough: an emoji is `\ud83d\ude00` or
# `%F0%9F%98%80` (a name of markup, up to 40, is rare and not counted; a longer one cannot be in a window this short).
MAX_CUT_RATIO = 12
ELLIPSES = re.compile("\u2026|\\.{3,}")
_DOTS = re.compile(r"\.+")
# The most dots in a row of a value that a cut value is looked for with: in a run of dots, each place that the ellipsis can begin at
# costs a scan, and a text of runs of dots is not to cost a scan for every dot of them.
MAX_DOT_RUN = 64
# With more ellipses than this in a text, the starts of a cut value are found in one pass over it.
MANY_ELLIPSES = 64
# The longest way to write one character: a name of markup (`&CounterClockwiseContourIntegral;`) is the longest there is.
MAX_ESCAPE_LENGTH = 40
# What the search for the cut values of one text may cost, in steps (about two microseconds each): a character walked, or a hundred
# characters of a window looked at for the places where a value begins. A text that repeats the first characters of a value, and a
# value that repeats itself, have each place a value can begin at walked to its end. Past it the window before each ellipsis that
# is left is scrubbed whole.
CUT_WORK_LIMIT = 1_500_000


# How a line break of a value is written in lines of text: a writer breaks a line with its own break (`\n`, `\r\n`, or `\r`), and
# YAML 1.1 (PyYAML) also has U+0085, U+2028 and U+2029 for one, kept as they are.
_BREAKS = {"\n": r"(?:\r\n|\n|\r(?!\n))", "\x85": "\x85", "\u2028": "\u2028", "\u2029": "\u2029"}


def _pieces_of(text, more=False):
    """One pattern for each character of `text`: any text that can stand for it (`_escapes_of`). A line break that has another
    character after it also stands for itself followed by spaces: the lines of a YAML string and of a block (`|`) are indented,
    and a value whose own lines begin with spaces has the spaces of the block before them (the pattern gives the extra back
    one by one). With `more`, `text` is the start of a longer one, and its last character has another after it."""
    pieces, last = [], len(text) if more else len(text) - 1
    for index, char in enumerate(text):
        found = sorted(_escapes_of(char), key=lambda alternative: (-len(alternative), alternative))
        alternatives = [re.escape(each) for each in found]
        if char in _BREAKS:
            alternatives = [_BREAKS[char] + (" *" if index < last else "")] + [each for each in alternatives if each != re.escape(char)]
        pieces.append(alternatives[0] if len(alternatives) == 1 else "(?:" + "|".join(alternatives) + ")")
    return pieces


@functools.lru_cache(maxsize=1024)
def _prefixes_of(char):
    """Every start of a way to write `char` that is not all of it. A character over U+FFFF is two UTF-16 units to a tool that
    cuts a text by the length of a JavaScript string: the first of them, alone, is also a start (and a decoder makes U+FFFD of it)."""
    found = {each[:size] for each in _escapes_of(char) for size in range(1, len(each))}
    if ord(char) > 0xFFFF:
        found.update((chr(0xD800 + ((ord(char) - 0x10000) >> 10)), "\ufffd"))
    return frozenset(found)


def _between(positions, low, high):
    """The positions in the sorted list `positions` that are in [low, high), without a copy of the list: a text of many
    ellipses asks for them once for each."""
    index = bisect.bisect_left(positions, low)
    while index < len(positions) and positions[index] < high:
        yield positions[index]
        index += 1


class _OutOfWork(Exception):
    """The walk of the cut values of a text has used what it may."""


@functools.lru_cache(maxsize=1024)
def _by_first(char):
    """The ways to write `char`, by the character each begins with."""
    table = {}
    for each in _escapes_of(char):
        table.setdefault(each[0], []).append(each)
    return table


class _Cut:
    """Finds the start of a value in text that a tool cut: the first `MIN_VALUE_LENGTH` characters or more, in any of
    the ways `_escapes_of` writes them, right before an ellipsis (`starts` is the lookahead that needs the first
    `MIN_VALUE_LENGTH`). A value of any length can be followed as far as the tool showed it: the text is walked with the
    set of places the characters so far can have ended at, so that a character that has two ways to be written, one of
    them two characters of the value (`\\\\` is a backslash, or two), is read the way that fits."""

    def __init__(self, pieces, chars, more=False):
        self.starts = re.compile("(?=" + "".join(pieces[:MIN_VALUE_LENGTH]) + ")")
        self.chars = chars
        self.more = more  # `chars` are the start of a longer value: the last has another after it
        self.window = max(MAX_CUT_WINDOW, MAX_CUT_RATIO * len(chars))  # how far before an ellipsis a cut of it can begin

    def _ends(self, index, text, position, ellipsis):
        """Where a way to write the character `index` can end when it begins at `position`, within the text up to `ellipsis`. A
        line break of the value that has another character after it can be followed by spaces (`_pieces_of`)."""
        char = self.chars[index]
        if char in _BREAKS:
            flexible = self.more or index < len(self.chars) - 1
            for form in (("\r\n", "\n", "\r") if char == "\n" else (char,)):
                if text.startswith(form, position, ellipsis):
                    end = position + len(form)
                    if form == "\r" and text.startswith("\n", end):
                        continue  # a carriage return that is part of a CRLF is not a break of its own
                    yield end
                    while flexible and end < ellipsis and text[end] == " ":
                        end += 1
                        yield end
        for each in _by_first(char).get(text[position], ()):
            if (char not in _BREAKS or each != char) and text.startswith(each, position, ellipsis):
                yield position + len(each)

    def _reaches(self, text, start, ellipsis, work=None):
        """True when the characters of the value, from `start`, are the text up to `ellipsis`, or all but the start of the way
        to write the next one, and there are MIN_VALUE_LENGTH of them at least: the value is cut there."""
        positions = {start}
        for index, char in enumerate(self.chars):
            if ellipsis in positions and index >= MIN_VALUE_LENGTH:
                return True
            following = set()
            if work is not None:
                work[0] -= len(positions)
                if work[0] < 0:
                    raise _OutOfWork
            for position in positions:
                if position >= ellipsis:
                    continue
                if index >= MIN_VALUE_LENGTH and ellipsis - position < MAX_ESCAPE_LENGTH and (
                    text[position:ellipsis] in _prefixes_of(char)
                ):
                    return True  # the cut is in the middle of an escape of this character
                following.update(self._ends(index, text, position, ellipsis))
            positions = following
            if not positions:
                return False
        return False  # all of the value is there: it is not cut, and the pattern for the whole value finds it

    def candidates(self, text, high, starts=None, floor=0, work=None):
        """The places in `text` where a value can begin that a tool cut before `high`, in order: the places where its first
        MIN_VALUE_LENGTH characters are, in the `self.window` characters before `high` and not before `floor`. `starts` are the
        places where the value can begin in all of `text` (the places of `self.starts`), when they were found in one pass.
        `work` is a list of one number, what may still be spent: when it is spent, `_OutOfWork` is raised."""
        low = max(0, high - self.window, floor)
        if work is not None:
            work[0] -= 1 if starts is not None else 1 + (high - low) // 100  # a look up costs a step, a scan of the window more
            if work[0] < 0:
                raise _OutOfWork
        if starts is None:
            return [found.start() for found in self.starts.finditer(text, low, high)]
        return list(_between(starts, low, high))

    def start_before(self, text, ellipsis, candidates, work=None):
        """Where in `text` a cut value begins that ends at `ellipsis` (the place of the ellipsis), or None. A tool cuts a text by
        its length, so the cut can be in the middle of the escape of a character: what is left of it is the start of a way to
        write the character. Of the `candidates` the first is tried first (the longest cut)."""
        for start in candidates:
            if start >= ellipsis:
                break
            if self._reaches(text, start, ellipsis, work):
                return start
        return None


def _alternation(sources):
    """One pattern for all of `sources`, [(pattern, marker)], each in a group named for its place in the list, so that a
    match says which one it was."""
    return re.compile("|".join(f"(?P<m{index}>{source})" for index, (source, _marker) in enumerate(sources)))


# --- a value written in layers ------------------------------------------------------------------------------------
#
# A page can print a value inside a string that is escaped in its turn: `JSON.stringify({user: value})` in an element,
# and Playwright quoting that text in a snapshot, make `"{\"user\":\"Synthetic\\\"User\\\\A7492\"}"`. `_escapes_of` knows
# one layer. The others are found the other way round. The text around a place where the letters and digits of a value
# show is decoded, one family of escapes after another, up to MAX_LAYERS times, and the pattern of the value searches
# each result like any other text. A decoded character remembers the stretch of the text it was made from (`_Layer`),
# so that stretch, and nothing else, is what gets scrubbed.

# How many families of escapes are undone, one after the other, before the pattern of the value looks at the text. The
# pattern knows one layer more: a value is found under MAX_LAYERS + 1 layers.
MAX_LAYERS = 3
# The most characters of a text that one character of a value takes, in all the layers. The longest real ones: an emoji
# under a JSON escape and two URL escapes (`%25255Cud83d...`, 36 characters), and a `%` under three layers of numeric
# character references (about 70), or an emoji under a URL escape and three of those (about 270). A value with a run
# of letters or digits is looked for near that run, so the number only sets how far from it the rest of the value can be.
REACH = 512
# Places whose windows are this close are decoded as one window: the text between costs less than a decoding of its own.
MERGE_GAP = 256
# The most characters of a text that are decoded as one window. Places that are closer than that are decoded together; a
# text that has the value all over it is decoded in windows of this size, which overlap, so that what is held at once
# (each decoded text with the stretch of the first for each of its characters, 16 bytes) stays small.
MAX_WINDOW = 65536
# The most that the decoding of one text may cost in all, over its layers, in characters of text: one is a pass of C over a
# character to undo, search and compare it, about 50 nanoseconds, so this is two or three seconds. The places of a text that come after
# that are scrubbed whole, from the first run of the value to its last: a value that begins and ends with a letter or a digit
# is exactly that, and one that does not has what it can take around it.
WORK_LIMIT = 48_000_000
# What undoing one escape costs, in characters of text: a call of Python for each, about two microseconds.
TOKEN_COST = 40
# A value is looked for by its longest runs of letters and digits: this many of them.
MAX_TRACE_RUNS = 3
# A first or last run of a value that is shorter than this is not looked for for its own sake: it is in too many places of a text.
MIN_ANCHOR_RUN = 3
# How many places of a run the search for where a value ends looks at before it gives up and takes the longest reach. A
# place in a stretch that another place took already (a text full of the runs) looks at fewer, so that the cost of all the
# places stays near what the text costs.
MAX_CHAIN_CANDIDATES = 256
MAX_CHAIN_CANDIDATES_IN_A_CLUSTER = 16
# How far before a place the start of its window can move, to just after white space. A window that begins in the middle of
# a run of escaped backslashes does not know how many came before it, and the pairs of the escapes in it are counted
# from the wrong end half of the time (`\b`, a backspace, is the first letter of a value that was `b`).
MAX_ALIGNMENT = 1024

_ALNUM_RUN = re.compile(r"[A-Za-z0-9]+")
# What an escape needs to be in the text: a backslash, a percent, an ampersand, a plus, or two apostrophes. A value whose
# text has none of these near it is the value as it is, and the pattern of the value finds that.
_ESCAPE_HINT = re.compile(r"[\\%&+]|''")
_LINE_END = re.compile("[\r\n]")
# What a string (JSON, JavaScript, YAML, Python) makes of a backslash and one letter. A letter that no string uses
# stands for itself, as it does in JavaScript and CSS.
_STRING_ESCAPES = {
    "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "0": "\0", "a": "\a", "e": "\x1b",
    "N": "\x85", "_": "\xa0", "L": "\u2028", "P": "\u2029",
}
_LINE_ENDS = ("\n", "\r", "\r\n", "\u2028", "\u2029")
_STRING_ESCAPE = re.compile(
    r"\\(?:"
    r"(?P<pair>u[dD][89abAB][0-9A-Fa-f]{2}\\u[dD][c-fC-F][0-9A-Fa-f]{2})"
    r"|u\{(?P<braced>[0-9A-Fa-f]{1,6})\}"
    r"|u(?P<unit>[0-9A-Fa-f]{4})"
    r"|x(?P<byte>[0-9A-Fa-f]{2})"
    r"|U(?P<wide>[0-9A-Fa-f]{8})"
    r"|(?P<fold>(?:\r\n|[\n\r\u2028\u2029])[ \t]*)"
    r"|(?P<other>[\s\S])"
    r")"
)
_CSS_ESCAPE = re.compile(r"\\(?:(?P<hex>[0-9A-Fa-f]{1,6})(?:\r\n|[ \t\r\n\f])?|(?P<other>\r\n|[\s\S]))")
_APOSTROPHES = re.compile(r"'{2,}")
_PERCENT = re.compile(r"(?:%u[0-9A-Fa-f]{4}|%[0-9A-Fa-f]{2})+")
_PERCENT_OR_PLUS = re.compile(r"(?:%u[0-9A-Fa-f]{4}|%[0-9A-Fa-f]{2})+|\+")
_PERCENT_TOKEN = re.compile(r"%u([0-9A-Fa-f]{4})|%([0-9A-Fa-f]{2})")
_ENTITY = re.compile(r"&(?:#[xX](?P<hex>[0-9A-Fa-f]+)|#(?P<decimal>[0-9]+)|(?P<name>[A-Za-z][A-Za-z0-9]*));?")
# An escape of a byte over 127.
_HIGH_BYTE = re.compile(r"%[89A-Fa-f][0-9A-Fa-f]")
# A numeric reference to a control character (U+0080 to U+009F): decimal 128 to 159, or hex 80 to 9F.
_C1_REFERENCE = re.compile(r"&#(?:0*1(?:2[89]|[345][0-9])(?![0-9])|[xX]0*[89][0-9A-Fa-f](?![0-9A-Fa-f]))")
# A line break of a YAML string with the empty lines after it and the indent of the line that goes on.
_BREAK = re.compile(r"\r\n|\n|\r")
_FOLD = re.compile(r"(?:\r\n|\n|\r)(?:[ \t]*(?:\r\n|\n|\r))*[ \t]*")
_FOLD_OR_APOSTROPHES = re.compile(r"(?P<fold>(?:\r\n|\n|\r)(?:[ \t]*(?:\r\n|\n|\r))*[ \t]*)|(?P<quotes>'{2,})")
_LINE_BREAK = re.compile(r"[\r\n]")


def _character(code):
    """The character a code point stands for, as a string does: U+FFFD for one that is none."""
    return chr(code) if 0 < code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else "\ufffd"


def _string_piece(match):
    """What a backslash escape of a string (JSON, JavaScript, YAML, Python) stands for."""
    token, kind = match.group(), match.lastgroup
    if kind == "fold":
        chars = ""  # an escaped line break is nothing, and a YAML string drops the indent of the line after it
    elif kind == "other":
        chars = _STRING_ESCAPES.get(token[1:], token[1:])
    elif kind == "pair":
        high, low = int(token[2:6], 16), int(token[8:12], 16)
        chars = chr(0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00))
    else:
        code = int(match.group(kind), 16)
        chars = "\0" if code == 0 else _character(code)
    return [(chars, 0, len(token))]


def _css_piece(match):
    """What a backslash escape of CSS stands for: a code point in hex, or the character after the backslash."""
    token = match.group()
    if match.lastgroup == "hex":
        return [(_character(int(match.group("hex"), 16)), 0, len(token))]
    char = token[1:]
    return [("" if char in _LINE_ENDS else char, 0, len(token))]


def _quotes_from_the_left(match):
    """What a run of apostrophes stands for in a YAML string in single quotes, where two are one: Playwright writes the key
    of a line of a snapshot that way when the key needs quotes, as for an element whose name has a colon and a space
    in it. The opening quote is one of the run when the value begins with an apostrophe, and the closing quote when
    it ends with one, so an odd run is read from each end (this is the left one)."""
    size = len(match.group())
    pieces = [("'", 2 * index, 2 * index + 2) for index in range(size // 2)]
    return pieces + [("'", size - 1, size)] if size % 2 else pieces


def _quotes_from_the_right(match):
    """The same, from the right end of the run."""
    size = len(match.group())
    lead = size % 2
    return [("'", 0, 1)] * lead + [("'", lead + 2 * index, lead + 2 * index + 2) for index in range(size // 2)]


def _utf8_length(byte):
    """How many bytes the UTF-8 sequence that starts with `byte` has, or 0 for a byte that starts none."""
    if byte < 0x80:
        return 1
    if 0xC2 <= byte <= 0xDF:
        return 2
    if 0xE0 <= byte <= 0xEF:
        return 3
    if 0xF0 <= byte <= 0xF4:
        return 4
    return 0


def _flush_bytes(pieces, data, at):
    """Add to `pieces` the characters of the UTF-8 bytes in `data`, which are `%XX` escapes from `at` on, each with the
    escapes it is made of. A byte that starts no UTF-8 sequence, or a sequence that is cut, is the Latin-1 character."""
    index = 0
    while index < len(data):
        size, chars = _utf8_length(data[index]), None
        if size:
            try:
                chars = bytes(data[index:index + size]).decode("utf-8")
            except UnicodeDecodeError:
                chars = None
        if chars is None:
            size, chars = 1, chr(data[index])
        pieces.append((chars, at + index * 3, at + (index + size) * 3))
        index += size


def _flush_bytes_as_latin_1(pieces, data, at):
    """The same for a text that was written in ISO-8859-1 (JavaScript's `escape`, a form of a page in that charset): every
    byte is the character of its value, also where the bytes would be a UTF-8 sequence (`%C3%A9` is two characters)."""
    for index, byte in enumerate(data):
        pieces.append((chr(byte), at + index * 3, at + (index + 1) * 3))


def _percent_piece(match, flush=_flush_bytes):
    """What a run of percent escapes stands for: the characters of the UTF-8 bytes it holds (a byte that is no UTF-8 is the
    Latin-1 character of its value), and the UTF-16 units of `%uXXXX` that JavaScript's `escape` writes for a character
    over U+00FF, a pair of them for one over U+FFFF, each with the escapes it is made of. A `+` is a space. With `flush`,
    the bytes are read another way."""
    token = match.group()
    if token == "+":
        return [(" ", 0, 1)]
    pieces, data, data_at = [], bytearray(), 0
    found = list(_PERCENT_TOKEN.finditer(token))
    index = 0
    while index < len(found):
        current = found[index]
        index += 1
        if current.group(1) is None:
            if not data:
                data_at = current.start()
            data.append(int(current.group(2), 16))
            continue
        flush(pieces, data, data_at)
        data = bytearray()
        unit, end = int(current.group(1), 16), current.end()
        following = found[index] if index < len(found) else None
        if 0xD800 <= unit <= 0xDBFF and following is not None and following.group(1) is not None and (
            0xDC00 <= int(following.group(1), 16) <= 0xDFFF
        ):
            chars = chr(0x10000 + ((unit - 0xD800) << 10) + (int(following.group(1), 16) - 0xDC00))
            end = following.end()
            index += 1
        else:
            chars = chr(unit)
        pieces.append((chars, current.start(), end))
    flush(pieces, data, data_at)
    return pieces


def _percent_piece_as_latin_1(match):
    return _percent_piece(match, _flush_bytes_as_latin_1)


def _entity_piece(match):
    """What a character reference of markup stands for. None when the text is no reference. A number is the code point
    it says, a control character included: `html.unescape` drops those, and a page that prints a value with one in it
    has its DOM, and its encoders, keep it. A name is looked up as `html.unescape` does, the names that are written
    without a semicolon included."""
    token = match.group()
    if match.lastgroup == "name":
        chars = html.unescape(token)
        return None if chars == token else [(chars, 0, len(token))]
    digits = match.group(match.lastgroup)
    # A number of more digits than a code point can have is none (U+FFFD); `int` of a few thousand digits raises.
    code = (int(digits, 16) if match.lastgroup == "hex" else int(digits)) if len(digits) <= 10 else 0x110000
    if 0x80 <= code <= 0x9F:
        try:
            chars = bytes([code]).decode("cp1252")  # as a browser reads it: the C1 numbers are Windows-1252 text
        except UnicodeDecodeError:
            chars = chr(code)
    else:
        chars = _character(code)
    return [(chars, 0, len(token))]


def _entity_piece_as_written(match):
    """`_entity_piece`, except that a numeric reference to a control character stands for the control character, as it does
    in the code that wrote it. (A browser reads it as the character of Windows-1252, `&#133;` as an ellipsis, and a writer that
    escapes every character above U+007E with its number writes `&#133;` for U+0085.) It is tried on a text that has such a
    reference, and decodes all of the references of the text: one layer has one writer."""
    if match.lastgroup != "name":
        digits = match.group(match.lastgroup)
        code = (int(digits, 16) if match.lastgroup == "hex" else int(digits)) if len(digits) <= 10 else 0x110000
        if 0x80 <= code <= 0x9F:
            return [(chr(code), 0, len(match.group()))]
    return _entity_piece(match)


def _fold_piece(match):
    """What the line break of a YAML string that is written in lines stands for (plain, in single or double quotes, or
    folded with `>`), with the empty lines and the indent after it. A break between two lines is one space, which is
    where a writer folds a long line, and each empty line after the first break is a line break."""
    token = match.group()
    breaks = len(_BREAK.findall(token))
    return [(" " if breaks == 1 else "\n" * (breaks - 1), 0, len(token))]


def _fold_or_quotes_from_the_left(match):
    return _fold_piece(match) if match.lastgroup == "fold" else _quotes_from_the_left(match)


def _fold_or_quotes_from_the_right(match):
    return _fold_piece(match) if match.lastgroup == "fold" else _quotes_from_the_right(match)


# The families of escapes that are undone: what one of them needs to be in the text (all of these: a string, or a pattern
# that finds it), how they are found, and what they stand for, and whether they can be asked to read the first backslash as
# the second half of a pair (see `_Layer.decoded`). Two read a URL, with a plus for a space and without: a plus is a plus in a
# path. Two read the apostrophes of a YAML string in single quotes, from each end of a run.
_DECODERS = (
    (("\\",), _STRING_ESCAPE, _string_piece, True),
    (("\\",), _CSS_ESCAPE, _css_piece, True),
    (("%",), _PERCENT, _percent_piece, False),
    (("+",), _PERCENT_OR_PLUS, _percent_piece, False),
    (("&",), _ENTITY, _entity_piece, False),
    (("''",), _APOSTROPHES, _quotes_from_the_left, False),
    (("''",), _APOSTROPHES, _quotes_from_the_right, False),
)
# More families, for the values that need them and for no other: a window has the ones that the values in it need (the bits
# that a value, `_Trace.extras`, and a place carry). A writer (PyYAML) breaks a long line of a string at a space and puts the
# indent of the string on the next line: that is for a value that has a space, or a line break, or a character that CSS escapes
# with a hex number (which ends with a space) in it, which is where a writer can have broken it. The bytes of a URL are UTF-8 for a reader of UTF-8 and ISO-8859-1 for another
# (`%C3%A9` is one character or two: JavaScript's `escape` writes the second, and so does a page in that charset): the second is
# for a value with a character in U+0080 to U+00FF. A numeric reference to a control character is read as a browser reads it
# (`&#133;` is an ellipsis) and as the code that wrote it meant it (U+0085): the second is for a value with a character in
# U+0080 to U+009F.
_FOLDING, _LATIN_1, _CONTROLS = 1, 2, 4
_EXTRA_DECODERS = {
    # A string in single quotes is folded and has its apostrophes doubled: one layer, so one step.
    _FOLDING: (
        ((_LINE_BREAK,), _FOLD, _fold_piece, False),
        ((_LINE_BREAK, "''"), _FOLD_OR_APOSTROPHES, _fold_or_quotes_from_the_left, False),
        ((_LINE_BREAK, "''"), _FOLD_OR_APOSTROPHES, _fold_or_quotes_from_the_right, False),
    ),
    _LATIN_1: (
        ((_HIGH_BYTE,), _PERCENT, _percent_piece_as_latin_1, False),
        ((_HIGH_BYTE, "+"), _PERCENT_OR_PLUS, _percent_piece_as_latin_1, False),
    ),
    _CONTROLS: (((_C1_REFERENCE,), _ENTITY, _entity_piece_as_written, False),),
}


def _decoders_for(extras):
    """The families of `_DECODERS` and the extra ones for the bits in `extras`."""
    return _DECODERS + tuple(decoder for bit, family in _EXTRA_DECODERS.items() if extras & bit for decoder in family)


def _extras_of(text):
    """The bits of the extra families that a value needs (`_EXTRA_DECODERS`)."""
    return (
        (_FOLDING if " " in _css_escaped(text) or "\n" in text else 0)
        | (_LATIN_1 if any("\x80" <= char <= "\xff" for char in text) else 0)
        | (_CONTROLS if any("\x80" <= char <= "\x9f" for char in text) else 0)
    )


class _Layer:
    """A text that was made by undoing escapes in another, and for each of its characters the stretch of the first
    text it was made from: `starts[i]` to `ends[i]`. A character that came from an escape stands for all of it."""

    __slots__ = ("text", "starts", "ends", "work")

    def __init__(self, text, starts, ends, work=0):
        self.text, self.starts, self.ends = text, starts, ends
        self.work = work  # what it cost to make this text from the one before it, in characters (see TOKEN_COST)

    @classmethod
    def whole(cls, text, offset):
        """`text`, which begins at `offset` in the first text, with every character standing for itself."""
        size = len(text)
        return cls(text, array("q", range(offset, offset + size)), array("q", range(offset + 1, offset + size + 1)))

    def identity(self):
        """What makes this text and the stretches its characters stand for different from another's: the hashes of the
        three (a hash of a str or bytes is keyed per process, so that a text cannot be made to collide with another)."""
        return hash(self.text), hash(self.starts.tobytes()), hash(self.ends.tobytes())

    def decoded(self, decoder, shifted=False, spent=None):
        """This text with the escapes of one family undone, or None when it has none. With `shifted`, the first backslash
        is read as a backslash and the second half of a pair, as it is when the text begins in the middle of a run of
        them: the escapes after it are counted from the next one. `spent[0]` is what the pass cost, in the units of
        WORK_LIMIT: a pass over the text, and a call for each match, whether it stood for anything or not."""
        needs, pattern, convert, _shiftable = decoder
        text = self.text
        if not all((each in text) if isinstance(each, str) else each.search(text) for each in needs):
            return None
        out, starts, ends, last = [], array("q"), array("q"), 0
        begin = text.find("\\") + 1 if shifted else 0
        if shifted and not begin:
            return None
        tokens = 0
        for match in pattern.finditer(text, begin):
            tokens += 1 + len(match.group()) // 3  # a run of percent escapes, or of apostrophes, is a call for each
            pieces = convert(match)
            if pieces is None:
                continue
            begin = match.start()
            out.append(text[last:begin])
            starts.extend(self.starts[last:begin])
            ends.extend(self.ends[last:begin])
            for chars, low, high in pieces:
                out.append(chars)
                starts.extend([self.starts[begin + low]] * len(chars))
                ends.extend([self.ends[begin + high - 1]] * len(chars))
            last = match.end()
        if spent is not None:
            spent[0] = len(text) + TOKEN_COST * tokens
        if not out:
            return None
        out.append(text[last:])
        starts.extend(self.starts[last:])
        ends.extend(self.ends[last:])
        return _Layer("".join(out), starts, ends, len(text) + TOKEN_COST * tokens)


class _Budget:
    """What may still be spent on decoding, in the units of WORK_LIMIT. `take` is False, and `out` stays True, when a cost is
    more than what is left."""

    __slots__ = ("left", "out")

    def __init__(self, left):
        self.left, self.out = left, False

    def take(self, cost):
        if cost > self.left:
            self.out = True
            return False
        self.left -= cost
        return True


def _decodings(layer, edge=False, decoders=_DECODERS, budget=None):
    """Each text that undoing the escapes of up to MAX_LAYERS families (`decoders`), in any order, makes of `layer`, the
    fewest layers first. A text is made once, whatever the way to it, unless the ways differ in the stretches that its
    characters stand for: a run of three apostrophes is a pair and a single one from the left, and a single one and a pair
    from the right. With `edge`, the text begins in the middle of something, and the escapes of a string and of CSS are
    counted from the first backslash and from the second. With a `budget`, the generator stops, and the budget says so,
    when what is left cannot pay for a pass over the text it is about to decode (that is checked first, so that a text is
    not copied for nothing), or for the decoding that was made."""
    seen = {layer.identity()}
    row = [layer]
    for depth in range(MAX_LAYERS):
        following = []
        for current in row:
            for decoder in decoders:
                for shifted in (False, True) if edge and decoder[3] else (False,):
                    if budget is not None and budget.left < len(current.text):
                        budget.out = True  # not enough for a pass over this text: it is not decoded, and not copied
                        return
                    spent = [0]
                    made = current.decoded(decoder, shifted, spent)
                    if budget is not None and not budget.take(spent[0]):
                        return  # a pass that found nothing to undo cost its time, too
                    if made is None:
                        continue
                    identity = made.identity()
                    if identity in seen:
                        continue
                    seen.add(identity)
                    if depth + 1 < MAX_LAYERS:
                        following.append(made)  # the last layer is searched and let go: it has none after it
                    yield made
        row = following


def _aligned_start(text, index):
    """(start, edge) for a window that would begin at `index`. A line end that is not escaped ends every escape and every
    run of them, in every layer, so the window begins just after the last one before `index`, within MAX_ALIGNMENT
    characters, or at the start of the text: its escapes are then counted from where they begin. A line end after an odd
    number of backslashes is an escape, or the fold of a YAML string, which is nothing when it is undone: the text on the
    two sides is one, and so are the spaces of the indent. White space is no boundary for that reason. When there is none
    (a minified page, a long run of percent escapes) the window begins at `index` and is an `edge`: what it begins in the
    middle of is not known."""
    if index <= 0:
        return 0, False
    low, position = max(0, index - MAX_ALIGNMENT), index
    while True:
        found = text.rfind("\n", low, position)
        if found < 0:
            break
        end = found - 1 if found > 0 and text[found - 1] == "\r" else found
        run = 0
        while end - run > 0 and text[end - run - 1] == "\\":
            run += 1
        if run % 2 == 0:
            return found + 1, False
        position = found
    return (0, False) if low == 0 else (index, True)


def _is_digit(char):
    return "0" <= char <= "9"


def _trace_texts(text):
    """The texts to look for a value by, under layers of escapes. A value of ASCII letters and digits alone is the same text
    in every layer but one: `CSS.escape` writes a first digit as a hex escape (`\\32 026`). So one that begins with a letter
    has nothing to look for, the pattern of the value finds it as it is, and one that begins with a digit is looked for
    by its CSS form."""
    if text.isascii() and text.isalnum():
        return [_css_escaped(text)] if _is_digit(text[0]) else []
    return [text]


# The characters over U+007F that a letter stands for in a string (`\\N`, `\\_`, `\\L`, `\\P`), and the one that the code of a
# character that is none stands for: the text of an escape of them does not have their code in it.
_NO_SIGNATURE = frozenset(["\ufffd", *(char for char in _STRING_ESCAPES.values() if not char.isascii())])
# The characters that a reference to a control character stands for in markup (`&#128;` is the euro sign), and the codes: the
# number in the text is not the code of the character.
_CP1252_CODES = {}
for _code in range(0x80, 0xA0):
    try:
        _CP1252_CODES.setdefault(bytes([_code]).decode("cp1252"), []).append(_code)
    except UnicodeDecodeError:
        pass
_ENTITY_NAMES = {}  # {character over U+007F: the names that stand for it}
for _name, _chars in _HTML5_ENTITIES.items():
    for _char in set(_chars):
        if not _char.isascii():
            _ENTITY_NAMES.setdefault(_char, set()).add(_name.rstrip(";"))
# How many characters of a value the check of what a window holds looks at: the ones with the longest numbers.
MAX_SIGNATURES = 6
# How far apart two bytes of a character can be in a text, one after the other: a `%` between them, escaped in the layers there are.
BYTE_REACH = 300


class _Evidence:
    """What a text has for a character over U+007F under any escapes there are (`_evidence`): `starts(text)` are the places in
    it where that starts. The text of a number, of a name or of the character itself is found by a pattern, at every place it
    starts. The first byte of its UTF-8 and the last, as far apart as the bytes between them can make them, are found one by
    one, and put together by their places, so that a text built of first bytes costs a pass for each of them, and no more."""

    __slots__ = ("strength", "_simple", "_first", "_last", "_gap")

    def __init__(self, strength, simple, first, last, gap):
        self.strength, self._simple, self._first, self._last, self._gap = strength, simple, first, last, gap

    def starts(self, text):
        found = [match.start() for match in self._simple.finditer(text)]
        lows = [match.start() for match in self._first.finditer(text)]
        if lows:
            highs = [match.start() for match in self._last.finditer(text)]
            for low in lows:
                index = bisect.bisect_left(highs, low + 2)  # the last byte starts after the first has ended
                if index < len(highs) and highs[index] - (low + 2) <= self._gap:
                    found.append(low)
        return sorted(set(found)) if lows else found


@functools.lru_cache(maxsize=1024)
def _evidence(char):
    """An `_Evidence` for a character over U+007F, or None for a character that is not looked for this way. The escapes of a
    letter and a digit are never made (`_escapes_of`), and every other escape of a character has its number in it, whatever the
    layers that came after: its hex digits (`\\u0418`, `\\u{418}`, `&#x418;`, `\\418 `, `%u0418`), or its decimal ones (`&#1048;`),
    the first half of its pair of UTF-16 units (`\\ud83d\\ude00`), the code that Windows-1252 gives it for a reference to a control
    character, the first and the last byte of its UTF-8 (`%D0%98`), or its name in markup (`&Icy;`). A text that has none of
    these has the character itself, or does not have it. The numbers and the bytes are matched in either case, the character
    and the names as they are. `strength` is the length of the longest number."""
    code = ord(char)
    if char.isascii() or char in _NO_SIGNATURE or 0xD800 <= code <= 0xDFFF:
        return None
    codes = [code, *_CP1252_CODES.get(char, ())]
    numbers = {f"{each:x}" for each in codes} | {str(each) for each in codes}
    if code >= 0x10000:
        numbers.add(f"{0xD800 + ((code - 0x10000) >> 10):x}")
    pairs = [f"{byte:02x}" for byte in char.encode("utf-8")]
    exact = [re.escape(char), *(re.escape(name) for name in sorted(_ENTITY_NAMES.get(char, ())))]
    simple = re.compile("(?=%s|(?i:%s))" % ("|".join(exact), "|".join(re.escape(each) for each in sorted(numbers))), re.S)
    return _Evidence(
        max(len(each) for each in numbers), simple, re.compile(pairs[0], re.I), re.compile(pairs[-1], re.I),
        BYTE_REACH * (len(pairs) - 1),
    )


def _weights(text):
    """For each character of `text`, the most characters of a text that it can take: one for a letter or a digit, which no
    encoder escapes (`_escapes_of`), and REACH for any other, and for a digit at the start of an identifier, which CSS writes as a
    hex escape (`\\32 `). A value of a thousand characters, most of them letters, takes a text a thousand characters long
    and not half a million."""
    weights = [1 if char.isascii() and char.isalnum() else REACH for char in text]
    if _is_digit(text[:1]):
        weights[0] = REACH
    if text[:1] == "-" and _is_digit(text[1:2]):
        weights[1] = REACH
    return weights


def _chain_of(text):
    """[(gap, evidence, index)]: for the characters of `text` over U+007F that have the longest numbers (MAX_SIGNATURES of
    them, in the order they are in), what a text has for each (`_evidence`), how far the start of
    what the text has for it can be from the start of what it has for the one before (0 for the first: the stretch of
    every character of `text` from the one before up to this one, and this one, takes at most what `_weights` says), and
    where the character is in `text`."""
    found = [(index, _evidence(char)) for index, char in enumerate(text)]
    found = [(index, evidence) for index, evidence in found if evidence is not None]
    chosen = sorted(sorted(found, key=lambda item: -item[1].strength)[:MAX_SIGNATURES])
    weights = _weights(text)
    return [
        (sum(weights[chosen[position - 1][0]:index + 1]) if position else 0, evidence, index)
        for position, (index, evidence) in enumerate(chosen)
    ]


class _Trace:
    """Where a value can be in a text, whatever escapes are on it. A letter or a digit is never escaped (`_escapes_of`),
    so the longest runs of them in the value show in the text, in order, and a character of the value takes at most
    REACH characters of the text. A value with no such run is looked for around what a text has for its characters
    over U+007F (`_chain_of`), or, with none of those, around every escape. A place is not one when the text in it does
    not have what each of those characters is written as, in order."""

    def __init__(self, text, marker):
        self.marker, self.text = marker, text
        self.chain = _chain_of(text)
        self.extras = _extras_of(text)  # the families of escapes that only this value needs (`_EXTRA_DECODERS`)
        runs = [list(found.span()) for found in _ALNUM_RUN.finditer(text)]
        # `CSS.escape` writes a digit at the start of an identifier, and one after a first hyphen, as a hex escape.
        escaped = 0 if _is_digit(text[:1]) else (1 if text[:1] == "-" and _is_digit(text[1:2]) else None)
        for run in runs:
            if run[0] == escaped:
                run[0] += 1
        runs = [run for run in runs if run[1] > run[0]]
        # A value is looked for from its first run and to its last, so that what a window takes before the first and after the
        # last is a few characters and not the half of a key file: a value that begins with a run is looked for from it, and one
        # that ends with a run to it (nothing of the value is before the first or after the last, and a text that is scrubbed
        # whole for lack of time takes exactly the value); otherwise the first run of MIN_ANCHOR_RUN characters or more, and the last.
        first = runs[0] if runs and runs[0][0] == 0 else next((run for run in runs if run[1] - run[0] >= MIN_ANCHOR_RUN), None)
        last = runs[-1] if runs and runs[-1][1] == len(text) else next(
            (run for run in reversed(runs) if run[1] - run[0] >= MIN_ANCHOR_RUN), None
        )
        forced = [run for run in ((first,) if first is last else (first, last)) if run is not None]
        picked = forced + [run for run in sorted(runs, key=lambda run: (run[0] - run[1], run[0])) if not any(run is each for each in forced)]
        chosen = sorted(picked[:max(MAX_TRACE_RUNS, len(forced))])
        self.runs = [text[begin:end] for begin, end in chosen]
        self.length = len(text)
        if chosen:
            # How many characters of the value come before the first run, from the first run on, and after the last
            # run, how many characters of a text they take at most, and how far apart each run can be from the one before.
            weights = _weights(text)
            self.lead, self.rest, self.tail = chosen[0][0], len(text) - chosen[0][0], len(text) - chosen[-1][1]
            self.reach_before, self.reach_rest = sum(weights[:chosen[0][0]]), sum(weights[chosen[0][0]:])
            self.reach_tail = sum(weights[chosen[-1][1]:])
            self.gaps = [sum(weights[previous_end:begin]) for (_b, previous_end), (begin, _e) in zip(chosen, chosen[1:])]

    def places(self, text, hints):
        """[(start, end, marker, exact, extras)]: the stretch of `text` that the value can fill, for each place where it can
        be: each place of the first run that has the other runs after it, in order, within their reach, and an escape in the
        stretch (`hints` are the positions of the characters that escapes need, and of the line ends for a value that a
        fold can be in). `exact` says that the stretch begins where the value does: a value that begins with its first run
        has nothing before it, if the run is not cut out of an escape. `extras` are the bits of the families
        of escapes that only the value needs. When the value has characters over U+007F, the stretch must have what a text has for them (`_chain_of`) in
        order, or it is no place."""
        if self.runs and any(run not in text for run in self.runs):
            return []
        survivors = None
        if self.chain:
            survivors = self._survivors(text)
            if not survivors:
                return []
        if not self.runs and survivors is not None:
            return self._chain_places(text, hints, survivors)
        if not self.runs:
            # No letter or digit to look for: the reach of the value on either side of each escape is a place. (A line is
            # not: a layer that folds a long string puts line ends in the middle of it.) The places of escapes that are close are
            # one, up to the size of a window: a text full of escapes does not make a place of the size of the text.
            reach, found = sum(_weights(self.text)), []
            cap = max(MAX_WINDOW, 2 * reach + 1)
            for position in hints:
                start, stop = max(0, position - reach), min(len(text), position + reach + 1)
                if found and start <= found[-1][1] and stop - found[-1][0] <= cap:
                    found[-1] = (found[-1][0], stop)
                else:
                    found.append((start, stop))
            return [(start, stop, self.marker, False, self.extras) for start, stop in found]
        first, size, found, taken = self.runs[0], len(text), [], -1
        place = text.find(first)
        while place >= 0:
            start = max(0, place - self.reach_before)
            if _hint_between(hints, start, place + self.reach_rest):
                end = self._end_from(
                    text, place, MAX_CHAIN_CANDIDATES_IN_A_CLUSTER if place < taken else MAX_CHAIN_CANDIDATES
                )
                if end is not None and _hint_between(hints, start, end + self.reach_tail):
                    stop = min(size, end + self.reach_tail)
                    if survivors is None or _hint_between(survivors, start, stop):
                        found.append((start, stop, self.marker, not self.lead and _begins_a_token(text, place), self.extras))
                    taken = max(taken, stop)
            place = text.find(first, place + 1)
        return found

    def _survivors(self, text):
        """The sorted places in `text` where what a text has for the last character of the chain starts, and what it has for
        each of the others before it, in order and not farther apart than they can be (`_chain_of`): [] when there is
        none, and then the value is not in the text, in any form."""
        frontier = None
        for gap, evidence, _index in self.chain:
            starts = evidence.starts(text)
            if frontier is not None:
                starts = [at for at in starts if _within(frontier, at, gap)]
            if not starts:
                return []
            frontier = starts
        return frontier

    def _chain_places(self, text, hints, survivors):
        """The places of a value with no letter or digit and a character over U+007F: around each survivor. The value is at
        most as long before it as its characters before it can be, and after it as the characters after it."""
        last, weights = self.chain[-1][2], _weights(self.text)
        before, after, found = sum(weights[:last + 1]), sum(weights[last:]), []
        cap = max(MAX_WINDOW, before + after)
        for at in survivors:
            start, stop = max(0, at - before), min(len(text), at + after)
            if not _hint_between(hints, start, stop):
                continue
            if found and start <= found[-1][1] and stop - found[-1][0] <= cap:
                found[-1] = (found[-1][0], stop)
            else:
                found.append((start, stop))
        return [(start, stop, self.marker, False, self.extras) for start, stop in found]

    def _end_from(self, text, place, cap):
        """Where the last run can end, the farthest, when the first starts at `place`: each run starts after the end of the
        one before, within its reach. None when they are not all there. A run that is in too many places to tell
        has the reach of the whole value."""
        ends = [place + len(self.runs[0])]
        for run, reach in zip(self.runs[1:], self.gaps):
            high = ends[-1] + reach + len(run)
            starts, at = [], text.find(run, ends[0], high)
            while at >= 0:
                if len(starts) >= cap:
                    return min(len(text), place + self.reach_rest)
                starts.append(at)
                at = text.find(run, at + 1, high)
            following = []
            for at in starts:
                before = bisect.bisect_right(ends, at)  # the ends at or before this start, and the nearest is the last
                if before and at - ends[before - 1] <= reach:
                    following.append(at + len(run))
            if not following:
                return None
            ends = following
        return ends[-1]


# What can be inside a token of an escape or of a run of them, or end one whose character is one of these: a letter or a digit
# (`%5C`, `\x41`, `&amp;`), and the characters that escapes are made of.
_INSIDE_AN_ESCAPE = frozenset("\\%&#;'+{")


def _begins_a_token(text, place):
    """True when `place` can be the start of the text of a value, whatever escapes are around it: it is at the start of the text, or
    the character before it is not one that an escape is made of, so no escape or run of them is cut there."""
    if place <= 0:
        return True
    before = text[place - 1]
    return not (before.isalnum() or before in _INSIDE_AN_ESCAPE)


def _within(starts, at, gap):
    """True when one of the sorted positions in `starts` is at `at` or before it, and not more than `gap` before it. (A name of
    markup can stand for two characters, and then what a text has for them starts at the same place.)"""
    before = bisect.bisect_right(starts, at) - 1
    return before >= 0 and at - starts[before] <= gap


def _hint_between(hints, start, end):
    """True when one of the sorted positions in `hints` is in [start, end)."""
    index = bisect.bisect_left(hints, start)
    return index < len(hints) and hints[index] < end


def _merged(spans):
    """The stretches in `spans`, [(start, end, marker)], with those that overlap made into one (the first marker)."""
    merged = []
    for start, end, marker in sorted(spans, key=lambda span: (span[0], -span[1])):
        if merged and start < merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end, merged[-1][2])
        else:
            merged.append((start, end, marker))
    return merged


class Scrubber:
    """Replaces every stored value in a text with `[secret:NAME]`, longest first,
    in one pass, so a marker is never scanned again. A value is found whole, in any
    of the ways a page, a browser or Playwright writes it, one character at a time
    (`_escapes_of`), under more layers of escapes than one (`_deep_spans`), and as a
    prefix of 4 characters or more that a tool cut and ended with an ellipsis. A
    script can add a value it read at run time (`hide`), such as a one-time
    passcode, which is scrubbed the same way from then on."""

    def __init__(self, store):
        self.secrets = dict(store)
        self._texts = {}  # the text of a value, as typed or tidied, and the name it goes by
        for name, entry in sorted(self.secrets.items()):
            self._add(name, entry["value"])
        self._whole = self._exact = None
        self._entries, self._traces, self._wholes, self._markers, self._starts, self._lines = [], [], [], [], None, False
        self._dot_run, self._value_ellipses = 0, 0
        try:
            self._compile()
        except Exception:
            raise SecretStoreError("a stored value cannot be searched for in a text") from None

    def _add(self, name, value):
        for text in (value, _tidied(value), _tidied_in_a_string(value)):
            if len(text) >= MIN_VALUE_LENGTH:
                self._texts.setdefault(text, name)

    def _compile(self):
        """Make the patterns of the values. Nothing of the scrubber changes unless all of them are made: a scrubber that raised
        half way would have none, and a text would go through it unscrubbed."""
        if not self._texts:
            self._whole = self._exact = None
            self._entries, self._traces, self._wholes, self._markers, self._starts, self._lines = [], [], [], [], None, False
            self._dot_run, self._value_ellipses = 0, 0
            return
        wholes, parts, entries = [], [], []  # the patterns, the numbers of the ones that are a piece of a value, the cut ones
        for text in sorted(self._texts, key=len, reverse=True):
            marker = f"[secret:{self._texts[text]}]"
            if len(text) <= MAX_PATTERN_VALUE:
                pieces = _pieces_of(text)
                wholes.append(("".join(pieces), marker))
                entries.append((_Cut(pieces, text), marker))
            else:
                forms = sorted(_forms(text), key=len, reverse=True)
                wholes.append(("|".join(re.escape(form) for form in forms), marker))
                # A pattern of every character is too large for a value this long: it is made of pieces, each as long as
                # a pattern can be, one after the other. A value in any mix of the ways a text is written is then found
                # as the pieces, side by side, each with a marker of its own.
                count = -(-len(text) // MAX_PATTERN_VALUE)
                size = -(-len(text) // count)
                for start in range(0, len(text) if len(text) <= MAX_PIECED_VALUE else 0, size):
                    pieces = _pieces_of(text[start:start + size], more=start + size < len(text))
                    parts.append(len(wholes))
                    wholes.append(("".join(pieces), marker))
                    # a text cut in the second piece shows the first whole, and the start of the second
                    entries.append((_Cut(pieces, text[start:start + size], more=start + size < len(text)), marker))
        traces = [
            _Trace(trace_text, f"[secret:{name}]") for text, name in self._texts.items() for trace_text in _trace_texts(text)
        ]
        whole = _alternation(wholes)
        # `is_value` is about the whole of a value: a piece of a long one is not
        exact = _alternation([each for number, each in enumerate(wholes) if number not in parts]) if parts else whole
        # all of it was made: the scrubber changes now
        # How many dots in a row a value has in it, at most: a cut value can end in that many before the ellipsis.
        self._dot_run = min(MAX_DOT_RUN, max((len(each.group()) for text in self._texts for each in _DOTS.finditer(text)), default=0))
        # and how many ellipses (a run of three dots or more, or one \u2026), at most: a cut value has no more of them in it
        self._value_ellipses = max((len(ELLIPSES.findall(text)) for text in self._texts), default=0)
        self._entries = entries  # the values that have a pattern of their own, longest first, for the cut text
        self._traces = traces  # where each value can be in a text that has it under layers of escapes
        self._lines = any(trace.extras & _FOLDING for trace in traces)  # a value that a fold can be in: line ends are hints too
        self._wholes, self._markers = wholes, [marker for _source, marker in wholes]
        self._starts = None  # the pattern that finds the value at every place it starts, made when it is first needed
        self._whole, self._exact = whole, exact

    def hide(self, name, value):
        """Scrub `value` from now on, as `[secret:NAME]`. It is not a stored secret: `secrets` does not
        hold it, so no script can type it with `fill-secret`. Raises SecretStoreError, which never holds
        the value, when the name is not a secret name or the value is too short to scrub safely."""
        check_name(name)
        check_value(value)
        if len(value) > MAX_HIDDEN_VALUE:
            raise SecretStoreError(f"a hidden value is at most {MAX_HIDDEN_VALUE} characters")
        before = dict(self._texts)
        self._add(name, value)
        try:
            self._compile()
        except Exception:
            self._texts = before  # nothing was changed by the compile that failed: the scrubber is as it was
            raise SecretStoreError("this value cannot be searched for in a text, so it cannot be hidden") from None

    def __bool__(self):
        return self._whole is not None

    def holds(self, text):
        """True when `text` holds a protected value, whole, in any form the scrubber knows, under any layers of escapes
        that it undoes. A text that is not a str holds none."""
        if self._whole is None or not isinstance(text, str):
            return False
        return self._whole.search(text) is not None or bool(self._deep_spans(text, first=True))

    def is_value(self, text):
        """True when `text` is, whole, a protected value in one of the forms the scrubber knows."""
        return self._whole is not None and isinstance(text, str) and self._exact.fullmatch(text) is not None

    def scrub(self, text):
        if self._whole is None or not isinstance(text, str) or not text:
            return text
        # The stretches of the layered search, of the plain pattern and of the cut values, all found in the text as it is and
        # made one: a value found in one way may overlap a value found in another, a value may begin with another value
        # (a password that begins with the user name), and what is left of it must not stay.
        spans = self._deep_spans(text) + self._plain_spans(text)
        if "\u2026" in text or "..." in text:
            spans += self._cut_spans(text)
        if not spans:
            return text
        out, last = [], 0
        for start, end, marker in _merged(spans):
            out.append(text[last:start])
            out.append(marker)
            last = end
        out.append(text[last:])
        return "".join(out)

    def _start_pattern(self):
        """The pattern that finds a value at every place it starts, also where it starts inside another value or inside
        itself (`finditer` of the pattern of the values goes on after the end of a match): `end(lastgroup)` is where it ends."""
        if self._starts is None:
            self._starts = re.compile(
                "(?=" + "|".join(f"(?P<m{index}>{source})" for index, (source, _marker) in enumerate(self._wholes)) + ")"
            )
        return self._starts

    def _plain_spans(self, text):
        """[(start, end, marker)] for each value in `text`, in one layer of escapes at most. A value that starts inside
        another, or inside itself, is found too, and what they cover is one stretch: the end of one value must not stay
        because the start of the next was found first."""
        return _merged([
            (match.start(), match.end(match.lastgroup), self._markers[int(match.lastgroup[1:])])
            for match in self._start_pattern().finditer(text)
        ])

    def _deep_spans(self, text, first=False):
        """[(start, end, marker)] for each stretch of `text` that holds a value under escapes that the pattern of the value
        does not know: the text around each place where a value can be (`_Trace`) is decoded (`_decodings`), and what
        the pattern finds in each result, at every place a value starts in it, is mapped back to the stretch it was made
        from. The stretches do not overlap. With `first`, the list stops at the first one. When the decoding of a window
        costs more than is left of WORK_LIMIT, the places in it are scrubbed whole."""
        if not self._traces:
            return []
        if not any(not trace.runs or all(run in text for run in trace.runs) for trace in self._traces):
            return []  # none of the values has its words in the text
        hints = array("q", (match.start() for match in _ESCAPE_HINT.finditer(text)))
        if not hints and not self._lines:
            return []  # no escape in the text: a value in it is as it is, and the pattern of the value finds that
        lined = None  # the hints and the line ends: for a value that a fold can be in, a line end is where a space was
        places = []
        for trace in self._traces:
            at = hints
            if trace.extras & _FOLDING:
                if lined is None:
                    lined = array("q", heapq.merge(hints, (match.start() for match in _LINE_END.finditer(text))))
                at = lined
            places.extend(trace.places(text, at))
        if not places:
            return []
        groups = []  # [start, end, [place, ...], edge, extras]: the text to decode, the places in it, whether it begins in the middle of something, the extra families
        for place in sorted(places):
            low, edge = (place[0], False) if place[3] else _aligned_start(text, place[0])
            high = place[1]
            if groups and low <= groups[-1][1] + MERGE_GAP and high - groups[-1][0] <= MAX_WINDOW:
                if low < groups[-1][0]:
                    groups[-1][0], groups[-1][3] = low, edge
                groups[-1][1] = max(groups[-1][1], high)
                groups[-1][2].append(place)
                groups[-1][4] |= place[4]
            else:
                groups.append([low, high, [place], edge, place[4]])
        starts, budget, spans = self._start_pattern(), _Budget(WORK_LIMIT), []
        search_cost = len(self._wholes) * 3  # per character and ten, in the units of the budget: 12 ns for a pattern, 50 per unit
        # The small windows first: with what is left after them the large one is decoded, or its places are scrubbed whole, and
        # the fewer windows are lost to that, the less of the text goes.
        for low, high, group, edge, extras in sorted(groups, key=lambda each: each[1] - each[0]):
            budget.out = False
            hits, done = [], budget.take(high - low)
            if done:
                decoders = _decoders_for(extras)
                for made in _decodings(_Layer.whole(text[low:high], low), edge, decoders, budget):
                    if not budget.take(-(-len(made.text) * search_cost // 10)):
                        break  # searching the text costs a pass for each value, and more for a value that is long
                    for hit in starts.finditer(made.text):
                        marker = self._markers[int(hit.lastgroup[1:])]
                        hits.append((made.starts[hit.start()], made.ends[hit.end(hit.lastgroup) - 1], marker))
                        if first:
                            return hits
                done = not budget.out
            spans.extend(hits if done else [place[:3] for place in group])
            if first and spans:
                return spans
        return _merged(spans)

    def _floor(self, text, runs, number):
        """Where a value that is cut at the ellipsis `number` of `runs` does not begin before. What the tool shows of a value has
        in it the ellipses of the value and no others, so it holds at most `_value_ellipses` of the runs before this one whole, and
        a run it begins in is one that ends in the dots the value begins with."""
        back = number - self._value_ellipses - 1
        if back < 0:
            return 0
        run = runs[back]
        return run.end() if text[run.start()] == "\u2026" else max(run.start(), run.end() - self._dot_run)

    def _cut_spans(self, text):
        """[(start, end, marker)] for the start of each value that a tool cut and ended with an ellipsis, and the ellipsis. An
        ellipsis is one character, or three dots of a run of them: the value may have dots of its own at its end, before it
        (as many as it has in a row at most), and the text may go on with a dot after it. When there are many ellipses, the
        places where a value can begin are found in one pass over the text for each value, and each ellipsis looks them up. When
        the search costs more than CUT_WORK_LIMIT, the window before each ellipsis that is left is scrubbed whole."""
        runs = list(ELLIPSES.finditer(text))
        if not runs:
            return []
        if len(runs) > MANY_ELLIPSES:
            starts = [[found.start() for found in cut.starts.finditer(text)] for cut, _marker in self._entries]
            active = [index for index, found in enumerate(starts) if found]  # a value whose start is nowhere in the text is not looked for
        else:
            starts, active = None, range(len(self._entries))
        spans, work = [], [CUT_WORK_LIMIT]
        for number, run in enumerate(runs):
            single = text[run.start()] == "\u2026"
            last = run.start() if single else min(run.end() - 3, run.start() + self._dot_run)
            floor = self._floor(text, runs, number)
            try:
                candidates = []
                for index in active:
                    found = self._entries[index][0].candidates(text, last, None if starts is None else starts[index], floor, work)
                    if found:
                        candidates.append((index, found))
                for place in range(last, run.start() - 1, -1):  # the last place first: a value with dots in it ends in them
                    found = self._cut_before(text, place, candidates, work)
                    if found:
                        spans.extend((start, place + (1 if single else 3), marker) for start, marker in found)
                        break
            except _OutOfWork:
                marker = self._entries[0][1]
                window = max(cut.window for cut, _marker in self._entries)
                spans.extend(
                    (max(0, self._floor(text, runs, each), runs[each].start() - window), runs[each].end(), marker)
                    for each in range(number, len(runs))
                )
                break
        return spans

    def _cut_before(self, text, place, candidates, work=None):
        """[(start, marker)] of the values whose start ends at `place` in `text`. `candidates` are [(number of the value, its
        candidates)] (`_Cut.candidates`) for the values that have any."""
        found = []
        for index, each in candidates:
            cut, marker = self._entries[index]
            start = cut.start_before(text, place, each, work)
            if start is not None:
                found.append((start, marker))
        return found

    def scrub_file(self, target, remove_on_failure=False):
        """Scrub a text file in place, through a file made beside it and renamed over
        it. A file that is not UTF-8 text, or holds no secret, is left alone. True
        when the file changed. With `remove_on_failure`, a file that holds a value and
        cannot be rewritten is removed, or emptied when it cannot be removed, and that
        counts as a change: a log of the driver is not worth keeping with a value in it. That
        holds for a file with bytes in it that are not UTF-8, too: it is read as text with those
        bytes kept apart, and removed when the value is in it."""
        try:
            with open(target, "rb") as handle:
                raw = handle.read()
        except OSError:
            return False
        try:
            original = raw.decode("utf-8")
        except UnicodeDecodeError:
            if remove_on_failure and self.holds(raw.decode("utf-8", "surrogateescape")):
                return _discard(target)
            return False
        try:
            clean = self.scrub(original)
        except Exception:  # a text the scrub cannot read is not one to keep, when the file is not worth keeping
            return remove_on_failure and _discard(target)
        if clean == original:
            return False
        aside = f"{target}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with os.fdopen(_create_aside(aside), "w", encoding="utf-8", newline="") as handle:
                handle.write(clean)
            os.replace(aside, target)
        except OSError:
            try:
                os.unlink(aside)
            except OSError:
                pass
            return remove_on_failure and _discard(target)
        return True
