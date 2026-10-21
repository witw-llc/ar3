"""Everything that knows what Gemini's web interface looks like.

**This is the file that breaks when Google changes the interface.** That is not
a defect in this driver, it is the standing cost of driving a web app that
publishes no API, and patching this module is expected maintenance rather than
a bug report. Every selector, label, heading and URL shape the program relies
on is here, so a break is one file to read and one file to fix.
`docs/gemini-ui.md` lists each one and how to re-derive it from a snapshot.

Two sources, both named in that doc:

*Observed on the live interface, 2026-09-23, through `a8s-browser snap`* — the
prompt box's name, that a new chat has no id until the first message is sent,
the `You said` / `Gemini said` headings that separate the turns, the button
cluster that marks a finished reply, and the mode picker's name. On 2026-09-24:
the upload menu and its chip, a generated image and its download control. The
interface changes without notice, so these are current readings and not
constants.

*From b3t* (`apps/b3t/gemini.py` in `neilobremski/bin`, which drives Gemini Web
through playwright-cli today) — the app URL, the sign-in check, finding the
prompt box as the snapshot's `textbox`, sending with fill-then-Enter, and
polling snapshots for a completion signal rather than guessing a wait. b3t
needs the same fixes whenever this module does.

Unverified and best effort: naming a conversation after its correspondent, and
selecting a model. Both are non-fatal by design — a turn that cannot rename or
cannot switch models still delivers its reply.
"""
import json
import os
import re
import shlex
import time

from .browser_seat import BrowserError

APP_URL = "https://gemini.google.com/app"
SIGN_IN_HOST = "accounts.google.com"

# How long a page is given to settle after a navigation before it is read.
PAGE_LOAD_SECONDS = 3

# A finished reply is announced by the page (see `turn_complete`). The settle
# window is the backstop for when that announcement changes shape: text that
# has not moved for this long counts as done. The timeout bounds the whole wait
# and returns whatever arrived.
POLL_SECONDS = 2.5
SETTLE_SECONDS = 6.0
TURN_TIMEOUT_SECONDS = 180.0

# How long the URL is given to acquire a conversation id after the first
# message. Gemini rewrites /app to /app/<id> once the turn is under way.
URL_WAIT_SECONDS = 15.0

# How long the prompt box is given to appear. Gemini is a single-page app: a
# navigation resolves long before the composer exists, and a snapshot taken in
# that window has no textbox in it at all. Failing there would throw away a
# message because a page was slow, which cost a live turn on 2026-09-23.
PROMPT_WAIT_SECONDS = 20.0

# The prompt box is a textbox in the snapshot, preferring one whose name says
# what it is for. It is a Quill editor rather than a real textarea, so it is
# reached by its accessible name and never by textarea semantics.
PROMPT_HINTS = ("prompt", "gemini", "ask")
PROMPT_FALLBACK_LABEL = "Enter a prompt for Gemini"

# Each turn is announced by a heading: the person's at level 5, Gemini's at
# level 6. The model heading is what anchors reply extraction.
MODEL_TURN_HEADING = "Gemini said"
USER_TURN_HEADING = "You said"

# A finished model turn grows a button cluster. Its presence after the last
# `Gemini said` heading is the completion signal, in place of guessing from
# text alone. Every label here is distinctive enough that one is enough.
COMPLETION_BUTTONS = ("Good response", "Bad response", "Redo", "Show more options")

# Snapshot roles that carry reply text. Buttons, headings and the rest of the
# page's furniture are not part of what Gemini said.
#
# A block role starts a line of the reply and ends it. An inline role is a run
# inside the current line: one sentence with inline code, a link or a citation
# marker in it arrives as several sibling runs of one paragraph, and each run is
# trimmed, so the space between two runs is not in the snapshot at all (observed
# 2026-09-24). Bold and italic are not runs; the snapshot merges them into the
# surrounding text. `strong`, `emphasis` and `subscript` are the roles a snapshot
# gives other inline markup, and are a reading rather than an observation.
BLOCK_ROLES = ("paragraph", "listitem", "blockquote", "cell", "columnheader", "rowheader")
INLINE_ROLES = ("text", "code", "link", "superscript", "subscript", "strong", "emphasis")
TEXT_ROLES = (*BLOCK_ROLES, *INLINE_ROLES)
# A code block sits beside its own header, which carries the language's name and
# this button; inline code never has one (observed 2026-09-24). Inside a list
# item, it is how the wrapper holding a code block is told from an inline span.
CODE_BLOCK_BUTTON = "Copy code"
# A table row is one line of the reply, its cells joined by this. The header
# row is a row like any other. An empty cell is an empty field between two
# separators, and a cell holding several lines keeps them in its one field.
CELL_ROLES = ("cell", "columnheader", "rowheader")
CELL_SEPARATOR = " | "
CELL_LINE_SEPARATOR = " / "
# Punctuation that closes onto the run before it and opens onto the run after
# it. Only used when the page's rendered text cannot settle the spacing.
CLOSES_ONTO = ".,;:!?)]}%\u2019\u201d\u2026"
OPENS_ONTO = "([{\u2018\u201c"
# How much of each run is looked for in the rendered text to settle the space
# between two runs: long enough not to match somewhere else by chance.
JOINT_CONTEXT = 24
# Roles that end a turn. A button does not: a reply with a code block in it
# carries a copy button of its own, and stopping there would cut the reply in
# half. The prompt box is what actually sits below the last turn, which is also
# what keeps the page's own disclaimer out of a reply that is still streaming.
#
# Neither does a heading, on its own. Gemini writes structured answers, and
# their section headings are the reply — stopping at the first one returns the
# introductory sentence and throws away everything the sender asked for. Only a
# *turn* heading ends a turn, which is the pair below.
STOP_ROLES = ("textbox", "contentinfo", "form", "navigation", "banner")

# A file goes in through Gemini's own upload flow: the composer's `Upload &
# tools` button opens a menu, its `Upload files` item opens the operating
# system's file chooser, and a8s-browser's `upload` answers the chooser with one
# path. One file per chooser, so the menu is opened once per file. The button is
# addressed by CSS because a selector click is a real Playwright click, which
# this Angular menu answers; the item is addressed by its visible text.
#
# A synthetic `drop` does not work here. On the live page (2026-09-24) a drop
# onto the Quill editor and a drop onto Gemini's own dropzone container both
# ran without error and attached nothing: no chip, no filename, no progress.
UPLOAD_MENU_SELECTOR = 'button[aria-label="Upload & tools"]'
UPLOAD_MENU_ITEM = "Upload files"

# What an uploaded file looks like in the composer, which is the `group` that
# holds the prompt box. A document gets a chip with its name in it (see
# `chip_label`). An image gets a thumbnail, `img "attachment"`, with no name
# anywhere (observed 2026-09-24). The conversation above the composer carries
# images too, so chips are only ever counted inside the composer.
IMAGE_CHIP_NAME = "attachment"
IMAGE_UPLOAD_TYPES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
# Types that may be shown either way. Nobody has watched one land, so either
# chip counts, as long as the total rises by one for each of them.
EITHER_CHIP_TYPES = (".svg", ".heic", ".heif", ".avif", ".bmp", ".tif", ".tiff", ".ico")

# The composer's own send control, and the only positive readiness evidence this
# driver has. It does not exist while the prompt box is empty, and a snapshot
# renders a disabled node with a trailing `[disabled]`. So "the composer will
# send this now" is a thing the page can be asked, as long as the text is typed
# before the files go in.
SEND_BUTTON_NAME = "Send message"

# Gemini raises a disclaimer the first time a profile attaches anything, and the
# file does not land until a person accepts it. Accepting terms on somebody's
# account is not this driver's to do — it is the same kind of one-time, by-hand
# step as signing the profile in.
CONSENT_HEADING = "Creating content from images and files"

# How long an upload is given to show up in the composer before the send is
# abandoned. Nothing is submitted in that window, so running out is a message
# kept rather than a message sent without its files.
UPLOAD_WAIT_SECONDS = 30.0

# a8s-browser's heredoc marker for the message text. It is this driver's choice
# rather than the page's: any word works, and `_block_marker` lengthens it if a
# line of the message happens to read exactly like it.
BLOCK_MARKER = "A8SGW"

# The whole conversation as rendered text, used when the headings stop matching
# — the reply is then whatever follows the message just sent.
HISTORY_SELECTOR = "main"
# Page furniture that rendered text picks up after the last reply.
TRAILING_NOISE = ("Gemini is AI and can make mistakes", *COMPLETION_BUTTONS)

# A generated image, as the live page renders it inside a model turn: an
# unnamed button wrapping an unnamed img, then that image's own controls. The
# download control is the one that matters. It is per image, and an image turn
# counts as finished only when every image has one — the rating cluster alone
# is not trusted to wait for the pixels.
IMAGE_DOWNLOAD_NAME = "Download full size image"
IMAGE_CONTROL_NAMES = ("Share image", "Copy image", IMAGE_DOWNLOAD_NAME)

# The same control as an element, for a8s-browser's `download`, which takes a
# CSS selector and acts on the first visible match. The page keeps every older
# turn, and older turns carry download buttons too, so the selector is scoped
# to the newest exchange: the conversation container with no container after
# it. Within the turn, images are counted among the siblings that hold one.
NEWEST_EXCHANGE_SELECTOR = "div.conversation-container:not(:has(~ div.conversation-container))"
IMAGE_DOWNLOAD_SELECTOR = f'button[aria-label="{IMAGE_DOWNLOAD_NAME}"]'
IMAGE_GROUP_SELECTOR = "div.generated-images"

# How long one image's download is given, in a8s-browser's own units.
DOWNLOAD_WAIT_SECONDS = 60

# Gemini answers a burst with a quota refusal of its own, which arrives as a
# reply like any other and has to be recognised rather than relayed as an
# answer. The window keeps a long answer *about* rate limiting from reading as
# one.
RATE_LIMIT_PHRASES = (
    "you've reached your limit",
    "you have reached your limit",
    "you've hit your limit",
    "reached your daily limit",
    "too many requests",
    "rate limit",
    "try again later",
)
RATE_LIMIT_SCAN_CHARS = 240
RATE_LIMIT_MAX_CHARS = 600

# b3t's wording for a turn Gemini could not finish.
TROUBLE_PHRASES = ("couldn't generate", "something went wrong")

# The model switcher names the model it is on, so the current model is readable
# with no clicking at all. Changing it means opening an Angular menu, which is
# the part that may not answer a selector click.
MODE_PICKER_PREFIX = "Open mode picker, currently"
MODEL_BUTTON_SELECTORS = (
    'button[aria-label^="Open mode picker"]',
    'button[data-test-id="bard-mode-menu-button"]',
    ".gds-mode-switch-button",
)

# Gemini has no rename control this driver can reach safely. The obvious
# candidate, `button[aria-label^="Show more options"]`, opens the *response
# actions* menu - "Branch in new chat", "Listen", "Export to Docs" - whose first
# item is preselected, so an open menu swallows the next keystrokes and can
# branch the conversation instead of typing into it. Naming is cosmetic anyway:
# the stored URL identifies a conversation, and Gemini titles a chat from its
# first message, which here is the preamble.


# `- role "name" [ref=e1] [level=6]: value` — one node of a snapshot.
NODE = re.compile(
    r'^-\s+(?P<role>[A-Za-z][\w-]*)'
    r'(?:\s+"(?P<name>[^"]*)")?'
    r'(?P<attrs>(?:\s*\[[^\]]*\])*)'
    r'(?:\s*:\s*(?P<value>.*))?$'
)


class GeminiError(Exception):
    """The page did not look the way this module expects."""


class SendUncertain(GeminiError):
    """The send failed at or after the keystroke that submits the message.

    The difference matters more than it looks. A failure *before* Enter means
    the message is definitely not in the conversation and the caller should
    keep it and try again. A failure at Enter, or a seat that stopped answering
    altogether, means nobody here knows — and retrying on that guess asks
    Gemini the same question twice, in the same chat, as though the sender had
    said it twice. The caller reconciles against the page instead of guessing.
    """


class Reading:
    """One look at the conversation.

    `counts` is (user turns, model turns) as the page's own headings report
    them, or None when no heading matched — that is what tells a new reply from
    the one that was already there, without comparing text.

    `images` is `image_readiness` for the newest model turn, and `image_count`
    how many of its images can be downloaded now.
    """

    def __init__(self, text, complete, counts=None, images=None, image_count=0):
        self.text = text
        self.complete = complete
        self.counts = counts
        self.images = images
        self.image_count = image_count


class Turn:
    """The outcome of one message: the reply, and whether it finished.

    `images` is how many generated images the turn holds that are ready to be
    downloaded. It can be nonzero with an empty `reply`: an image-only answer
    is an answer.
    """

    def __init__(self, reply, complete, seconds, note="", counts=None, images=0):
        self.reply = reply
        self.complete = complete
        self.seconds = seconds
        self.note = note
        self.counts = counts
        self.images = images


def _now():
    return time.monotonic()


def _sleep(seconds):
    time.sleep(seconds)


def _unquote(value):
    """A snapshot value as text. The snapshot is YAML, and a value that starts
    with a quote or holds a colon is written as a quoted scalar — `- text: ",
    HERON-3"` — so the quotes are syntax, not part of what the page says."""
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
        except ValueError:
            return value
        return decoded if isinstance(decoded, str) else value
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def _unquote_key(line):
    """`- 'role "name: x" [ref=e1]': value` as `- role "name: x" [ref=e1]: value`.

    YAML single-quotes a whole key that holds `: `, as a citation's source chip
    does (observed 2026-09-24), and the node inside is as readable as any other.
    """
    if not line.startswith("- '"):
        return line
    index = 3
    while True:
        end = line.find("'", index)
        if end < 0:
            return line
        if line[end + 1:end + 2] == "'":
            index = end + 2
            continue
        return "- " + line[3:end].replace("''", "'") + line[end + 1:]


def node(line):
    """One snapshot line as (role, name, value); ("", "", "") for anything else."""
    match = NODE.match(_unquote_key(line.strip()))
    if not match:
        return "", "", ""
    return (
        match.group("role"),
        (match.group("name") or "").strip(),
        _unquote((match.group("value") or "").strip()),
    )


def poll_script():
    """One look at the page: its structure, and its text.

    The snapshot answers "is the turn done" and carries the reply; the rendered
    text is the fallback that needs no headings at all.
    """
    return f"snap\ntext {shlex.quote(HISTORY_SELECTOR)}"


def _block_marker(lines):
    """A heredoc marker no line of the message can be mistaken for."""
    marker = BLOCK_MARKER
    taken = {line.strip() for line in lines}
    while marker in taken:
        marker += "X"
    return marker


def compose_script(label, message):
    """`send_script` without the keystroke that sends.

    Used when files are going in. The text has to be in the box *first*: the
    send control does not render at all while the prompt is empty, and that
    control is the only evidence this driver has that an upload has finished.
    """
    script = send_script(label, message).splitlines()
    assert script[-1] == SUBMIT_STEP
    return "\n".join(script[:-1])


def send_script(label, message):
    """The script that types one message and sends it.

    **The message never appears as a command-line argument.** a8s-browser
    strips every script line before it parses it, so text passed inline loses
    its indentation — an indented code example arrives at column zero, which is
    a different program from the one the sender asked about. Worse, any line
    ending in `<<WORD` reads as a block opener and the whole script is refused,
    so a message that merely mentions a heredoc could not be sent at all.

    Each line therefore travels as a `<<MARKER` block, whose content a8s-browser
    takes verbatim: no stripping, no quoting, no comment rules. One block per
    line, because a `type` carrying a newline would press Enter and submit the
    message half-written; the line break is `Shift+Enter`, which Gemini treats
    as a new line inside the prompt.

    The box is emptied first, which also focuses it — a leftover draft in the
    prompt box would otherwise be sent along with the message. Sending is Enter
    straight after, which is b3t's proven move.

    One known difference from byte-for-byte: `splitlines` drops a trailing
    newline, so a message that ends in one arrives without it. Interior blank
    lines, tabs, quotes and indentation all survive.
    """
    if "<<" in label:
        raise GeminiError(
            f"the prompt box's name ({label!r}) contains '<<', which a8s-browser reads "
            "as a block opener. Give this seat a CSS selector for the prompt box instead."
        )
    lines = message.splitlines() or [""]
    marker = _block_marker(lines)
    script = [f"fill {shlex.quote(label)} ''"]
    for index, line in enumerate(lines):
        if index:
            script.append("press Shift+Enter")
        if line:
            script.append(f"type <<{marker}\n{line}\n{marker}")
    script.append("press Enter")
    return "\n".join(script)


def textbox_label(snapshot):
    """The prompt box's accessible name, read from a snapshot (b3t's rule).

    a8s-browser fills by label or selector rather than by ref, so what this
    takes from the snapshot is the *name* — which is the aria-label the page's
    own markup carries, and what `fill` matches on.
    """
    fallback = None
    for line in snapshot.splitlines():
        lowered = line.lower()
        if "textbox" not in lowered:
            continue
        match = re.search(r'"([^"]+)"', line)
        if not match:
            continue
        name = match.group(1)
        if any(hint in lowered for hint in PROMPT_HINTS):
            return name
        if fallback is None:
            fallback = name
    return fallback


def current_model(snapshot):
    """The model Gemini is on, read off the mode picker without opening it."""
    for line in snapshot.splitlines():
        role, name, _ = node(line)
        if role == "button" and name.startswith(MODE_PICKER_PREFIX):
            return name[len(MODE_PICKER_PREFIX):].strip(" :,")
    return ""


def turn_heading(role, name):
    """Which turn a heading opens: "model", "user", or "" for a content heading.

    The distinction is the whole of R1: a level-2 `Critical findings` inside an
    answer is text, and only `Gemini said` / `You said` divide one turn from
    the next.
    """
    if role != "heading":
        return ""
    if name == MODEL_TURN_HEADING:
        return "model"
    if name.startswith(USER_TURN_HEADING):
        return "user"
    return ""


def turn_counts(snapshot):
    """How many turns each side has taken, or None when no heading matches.

    This is what identifies a turn. Text cannot: an agent relay answers `OK`
    all day, and a second `OK` is a new reply rather than the old one still on
    screen. None means the headings have changed shape and the caller has to
    fall back to comparing text, which is the weaker rule and known to be.
    """
    asked = answered = 0
    for line in snapshot.splitlines():
        role, name, _ = node(line)
        which = turn_heading(role, name)
        if which == "model":
            answered += 1
        elif which == "user":
            asked += 1
    if not asked and not answered:
        return None
    return asked, answered


def _last_model_turn(lines):
    found = -1
    for index, line in enumerate(lines):
        role, name, _ = node(line)
        if turn_heading(role, name) == "model":
            found = index
    return found


def _turn_nodes(snapshot):
    """The newest model turn as (indent, role, name, value), up to what ends it."""
    lines = snapshot.splitlines()
    start = _last_model_turn(lines)
    if start < 0:
        return []
    nodes = []
    for line in lines[start + 1:]:
        role, name, value = node(line)
        if turn_heading(role, name) or role in STOP_ROLES:
            break
        if role == "button" and name in COMPLETION_BUTTONS:
            break
        nodes.append((_indent(line), role, name, value))
    return nodes


def _subtree_end(nodes, index):
    """The index just past `nodes[index]` and everything nested under it."""
    indent = nodes[index][0]
    end = index + 1
    while end < len(nodes) and nodes[end][0] > indent:
        end += 1
    return end


def _holds_blocks(nodes, start, stop):
    """Whether these nodes hold a line of their own: a block, or a code block's header."""
    for _, role, name, _ in nodes[start:stop]:
        if role in BLOCK_ROLES or role in ("heading", "list", "table"):
            return True
        if role == "button" and name == CODE_BLOCK_BUTTON:
            return True
    return False


def _joint(left, right, flat_history):
    """What goes between two runs of one line: a space, or nothing.

    The page's rendered text is asked first, because it has the space the
    snapshot trimmed away: `len`(x) and `str`s are glued on the page. When it
    cannot tell, the runs are spaced, except onto punctuation that closes or
    opens onto its neighbour.
    """
    if flat_history:
        tail, head = left[-JOINT_CONTEXT:], right[:JOINT_CONTEXT]
        glued = tail + head in flat_history
        spaced = f"{tail} {head}" in flat_history
        if glued != spaced:
            return "" if glued else " "
    if right[0] in CLOSES_ONTO or left[-1] in OPENS_ONTO:
        return ""
    return " "


def _reply_region(history):
    """The rendered text after the last `Gemini said`: the newest reply, and not
    the prompt above it. "" when the rendered text has no such heading."""
    index = (history or "").rfind(MODEL_TURN_HEADING)
    if index < 0:
        return ""
    return history[index + len(MODEL_TURN_HEADING):]


def _code_block(pieces, region, cursor):
    """A code block's text, newlines and indentation included, and where the
    search for the next block starts.

    The snapshot collapses a code block's whitespace: a run of it inside one
    piece becomes one space, and the pieces themselves are trimmed. So the
    rendered block must agree with the snapshot token for token, whitespace
    wherever the snapshot has a space and none where it has none, with only the
    whitespace between pieces left open. `print("a b")` never matches
    `print("ab")`.

    The block is looked for in the newest reply's rendered text only, on lines
    of its own, and from where the previous block was found: blocks are matched
    in order and none is used twice, so two blocks with the same characters
    each keep their own indentation. When no rendered block agrees, the
    snapshot's own text is returned, one piece per line — never another
    block's, and never the prompt's.
    """
    patterns = [r"\s+".join(map(re.escape, piece.split())) for piece in pieces if piece.split()]
    if not patterns:
        return "", cursor
    found = None
    if region:
        body = r"\s*".join(patterns)
        found = re.compile(rf"(?m)^([ \t]*{body})[ \t]*$").search(region, cursor)
    if found:
        return found.group(1), found.end()
    return "\n".join(piece.strip() for piece in pieces if piece.strip()), cursor


def reply_block(snapshot, history=""):
    """The newest model turn's text, taken from the snapshot's own structure.

    Anchored on the last `Gemini said` heading and stopped at whatever ends the
    turn. Inside it, a block (paragraph, list item, heading, table row,
    blockquote, code block) is a line of its own and an inline run joins the
    line it sits in, so one sentence with a citation, a link or inline code in
    it stays one sentence. `history` is the page's rendered text from the same
    look; it settles the spacing between runs and gives a code block back its
    lines. Without it the reply is still one line per block.
    """
    nodes = _turn_nodes(snapshot)
    region = _reply_region(history)
    # Spacing is read from the newest reply when it can be found, and from the
    # whole rendered text otherwise; a code block only ever from the reply.
    flat_history = " ".join((region or history or "").split())
    finished = []
    runs = []
    cursor = 0

    def end_line():
        text = ""
        for run in runs:
            run = run.strip()
            if run:
                text += (_joint(text, run, flat_history) if text else "") + run
        runs.clear()
        if text:
            finished.append(text)

    def walk(index, stop, inline):
        nonlocal cursor
        while index < stop:
            _, role, name, value = nodes[index]
            end = _subtree_end(nodes, index)
            if role == "heading":
                # A section heading inside the answer. It is part of what Gemini
                # wrote, and dropping it would run two sections together.
                end_line()
                runs.append(name or value)
                end_line()
            elif role in BLOCK_ROLES:
                end_line()
                runs.append(value or name)
                walk(index + 1, end, True)
                end_line()
            elif role == "code" and not inline:
                end_line()
                pieces = [value] + [child[3] or child[2] for child in nodes[index + 1:end]]
                block, cursor = _code_block(pieces, region, cursor)
                if block:
                    finished.append(block)
            elif role == "link":
                runs.append(name or value)
            elif role in INLINE_ROLES:
                runs.append(value or name)
                walk(index + 1, end, True)
            elif role == "row":
                # One line per row, one field per cell, as the page renders it.
                # An empty cell is an empty field, so every column stays where
                # its header is.
                end_line()
                fields = []
                child = index + 1
                while child < end:
                    child_end = _subtree_end(nodes, child)
                    first = len(finished)
                    walk(child, child_end, False)
                    end_line()
                    lines = "\n".join(finished[first:]).splitlines()
                    del finished[first:]
                    field = CELL_LINE_SEPARATOR.join(line.strip() for line in lines if line.strip())
                    if field or nodes[child][1] in CELL_ROLES:
                        fields.append(field)
                    child = child_end
                if fields:
                    finished.append(CELL_SEPARATOR.join(fields))
            elif role in ("button", "img", ""):
                pass
            elif inline and not _holds_blocks(nodes, index + 1, end):
                if role == "generic" and value:
                    runs.append(value)
                walk(index + 1, end, True)
            else:
                # A container of blocks — a list, a table row, the page's own
                # wrappers. What it holds is walked; its edges end a line.
                end_line()
                walk(index + 1, end, False)
                end_line()
            index = end

    walk(0, len(nodes), False)
    end_line()
    return "\n".join(finished).strip("\n")


def turn_complete(snapshot):
    """Whether the page says the newest model turn has finished.

    A finished turn carries the rate-it-and-redo-it buttons; one that is still
    streaming does not. This is the primary completion signal — settling on
    unchanged text is only the backstop for when these labels change.
    """
    lines = snapshot.splitlines()
    start = _last_model_turn(lines)
    if start < 0:
        return False
    for line in lines[start + 1:]:
        role, name, _ = node(line)
        if role == "button" and name in COMPLETION_BUTTONS:
            return True
    return False


def _newest_turn(snapshot):
    """The newest model turn's lines, controls included, or [] when there is none.

    Unlike `reply_block` this does not stop at the rating cluster: an image's own
    controls sit inside the turn, and the cluster carries a `Share image` of its
    own on an image turn.
    """
    lines = (snapshot or "").splitlines()
    start = _last_model_turn(lines)
    if start < 0:
        return []
    # The composer is never part of a turn: an image put in for the next message
    # is a thumbnail there, not a picture Gemini drew.
    span = _composer_span(lines)
    stop = span[0] if span and span[0] > start else len(lines)
    turn = []
    for line in lines[start + 1:stop]:
        role, name, _ = node(line)
        if turn_heading(role, name) or role in STOP_ROLES:
            break
        turn.append(line)
    return turn


def _indent(line):
    return len(line) - len(line.lstrip())


def _image_tiles(lines):
    """Generated images drawn in these lines: an unnamed button wrapping an img.

    An icon is an img too, but it carries its glyph name as a value (`img: download`),
    so an img with no value is a picture. Its name varies: unnamed on one live
    page, `", AI generated"` on another (2026-09-24), so the name is not read.
    """
    tiles = 0
    for index, line in enumerate(lines[:-1]):
        role, name, _ = node(line)
        if role != "button" or name:
            continue
        inner = lines[index + 1]
        inner_role, _, inner_value = node(inner)
        if inner_role == "img" and not inner_value:
            if _indent(inner) > _indent(line):
                tiles += 1
    return tiles


def image_downloads(snapshot):
    """How many images in the newest model turn have their download control."""
    count = 0
    for line in _newest_turn(snapshot):
        role, name, _ = node(line)
        if role == "button" and name == IMAGE_DOWNLOAD_NAME:
            count += 1
    return count


def image_readiness(snapshot):
    """Whether the newest model turn's images can all be downloaded.

    `True` every image drawn in the turn has its download control. `False` the
    turn shows an image, or an image's controls, and at least one image has no
    download control yet. **`None` the turn shows no image evidence at all** —
    as far as the page says it is a text turn, and text-turn completion rules
    apply. None never makes a turn ready on its own: an image-only turn has no
    text, so without `True` it has nothing to report.

    Nothing here trusts the rating cluster. Whether it can appear before an
    image finishes rendering cannot be read off a finished page, so the safe
    reading is that it can, and the per-image control is the evidence instead.
    """
    turn = _newest_turn(snapshot)
    if not turn:
        return None
    tiles = _image_tiles(turn)
    downloads = 0
    controls = False
    for line in turn:
        role, name, _ = node(line)
        if role != "button":
            continue
        if name == IMAGE_DOWNLOAD_NAME:
            downloads += 1
        if name in IMAGE_CONTROL_NAMES:
            controls = True
    if not (tiles or downloads or controls):
        return None
    return bool(downloads) and downloads >= tiles


def image_download_selector(index):
    """A CSS selector for the download control of image `index` (1-based) in the newest turn."""
    return (
        f"{NEWEST_EXCHANGE_SELECTOR} {IMAGE_GROUP_SELECTOR} > "
        f":nth-child({index} of :has({IMAGE_DOWNLOAD_SELECTOR})) {IMAGE_DOWNLOAD_SELECTOR}"
    )


def conversation_id(url):
    """The id in `https://gemini.google.com/app/<id>`, or "" for a fresh app."""
    marker = "/app/"
    if marker not in url:
        return ""
    return url.split(marker, 1)[1].split("?", 1)[0].split("#", 1)[0].strip("/")


def rate_limited(text):
    """Gemini's own quota refusal, which arrives as a reply.

    Matched only near the front of a short answer: a long reply that explains
    HTTP rate limiting is an answer, not a refusal, and withholding it would be
    the worse mistake.
    """
    if not text or len(text) > RATE_LIMIT_MAX_CHARS:
        return False
    window = text[:RATE_LIMIT_SCAN_CHARS].lower()
    return any(phrase in window for phrase in RATE_LIMIT_PHRASES)


def trouble(text):
    """Gemini's wording for a turn it could not finish, or ""."""
    lowered = (text or "").lower()
    for phrase in TROUBLE_PHRASES:
        if phrase in lowered:
            return phrase
    return ""


def furniture(snapshot):
    """Labels of the page's own controls, which rendered text picks up as lines.

    Button names, icon glyph names, the mode picker's model name, the turn
    headings: none of them is something Gemini said. A reply built from the
    rendered text drops any line that is exactly one of these.
    """
    labels = {MODEL_TURN_HEADING, USER_TURN_HEADING, *TRAILING_NOISE}
    model = current_model(snapshot)
    if model:
        labels.add(model)
    for line in (snapshot or "").splitlines():
        role, name, value = node(line)
        if role in TEXT_ROLES or role == "heading":
            continue
        for label in (name, value):
            if label:
                labels.add(label)
    return {label.strip().lower() for label in labels if label.strip()}


def _tail_after(history, sent, snapshot=""):
    """What the rendered conversation says after the message just sent.

    Only lines that are not page furniture count. A tail made of nothing but
    headings and control labels is not an answer, and it is returned as "".
    """
    if not history or not sent:
        return ""
    needle = sent.strip()
    index = history.rfind(needle)
    if index < 0:
        first = needle.splitlines()[0].strip()
        index = history.rfind(first) if first else -1
        if index < 0:
            return ""
        needle = first
    tail = history[index + len(needle):]
    for noise in TRAILING_NOISE:
        tail = tail.split(noise, 1)[0]
    skip = furniture(snapshot)
    kept = [line for line in tail.splitlines() if line.strip().lower() not in skip]
    return "\n".join(kept).strip()


def extract_reply(snapshot, history, sent):
    """The newest reply: the snapshot's own turn structure, else the text.

    The structure comes first because it is unambiguous — the last `Gemini
    said` heading is the turn that just ran. When that heading is on the page,
    its turn is the whole answer, **including when it holds no text**: an image
    turn has none, and reading the rendered text instead returns the page's
    furniture as though Gemini had written it. The rendered text is the fallback
    only for a page whose headings no longer match, and it is anchored on the
    message that was just sent, which is text this driver already knows.
    """
    if _last_model_turn((snapshot or "").splitlines()) >= 0:
        return reply_block(snapshot, history)
    return _tail_after(history, sent, snapshot)


def _run(browser, script, doing):
    try:
        transcript = browser.run(script)
    except BrowserError as exc:
        raise GeminiError(f"{doing}: {exc}") from exc
    if not transcript.ok:
        raise GeminiError(f"{doing}: {transcript.error}")
    return transcript


def _first_output(transcript, verb):
    outputs = transcript.outputs(verb)
    return outputs[0] if outputs else ""


def _snapshot_of(transcript, browser):
    """The snapshot a `snap` step attached, read off disk.

    A `snap` step reports the file it wrote, so the snapshot is named rather
    than guessed at. Taking the first `.txt` out of the run's artifacts is what
    this used to do, and it stops being right the moment a script also
    downloads something: artifacts are attached in script order, so a
    downloaded `.txt` attached before the snap would be read as the page. The
    artifact list is still the fallback, because a step that *fails* has a
    snapshot attached to it without a `snap` command to report one.

    The newest is the one that counts — a script may look twice.
    """
    named = [path for path in transcript.outputs("snap") if path.strip()]
    paths = named or transcript.files
    if not paths:
        raise GeminiError(
            f"a8s-browser attached no snapshot for seat {browser.seat!r}, so the page "
            "cannot be read"
        )
    path = paths[-1]
    try:
        # `errors="replace"` because the fallback can land on a binary artifact,
        # and a UnicodeDecodeError here would escape as a crash rather than as
        # a reply telling the sender what went wrong.
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError as exc:
        raise GeminiError(f"the page snapshot at {path} could not be read: {exc}") from exc


def current_url(browser):
    return _first_output(_run(browser, "url", "reading the browser's URL"), "url").strip()


def _require_signed_in(browser, url):
    if SIGN_IN_HOST in url:
        raise GeminiError(
            f"browser seat {browser.seat!r} is not signed in to Gemini — the page is on "
            f"{SIGN_IN_HOST}. Sign it in by hand once (`ar3 tool browser --as {browser.seat} -- open`, "
            "then log in in the window that opens); this driver never signs in to anything."
        )


def open_app(browser):
    """A fresh Gemini chat."""
    script = f"go {APP_URL}\nwait {PAGE_LOAD_SECONDS}\nurl"
    url = _first_output(_run(browser, script, "opening Gemini"), "url").strip()
    _require_signed_in(browser, url)
    return url


def open_conversation(browser, url):
    """An existing conversation, by the URL this correspondent was given."""
    wanted = conversation_id(url)
    script = f"go {url}\nwait {PAGE_LOAD_SECONDS}\nurl"
    landed = _first_output(
        _run(browser, script, "opening the stored conversation"), "url"
    ).strip()
    _require_signed_in(browser, landed)
    if wanted and conversation_id(landed) != wanted:
        # Where the browser landed is not evidence of why. A page lost to a
        # browser restart lands on about:blank, and nothing here has seen what
        # Gemini shows for a conversation that was really deleted.
        raise GeminiError(
            f"the stored conversation {wanted} could not be opened — the browser "
            f"landed on {landed}"
        )
    return landed


def page_snapshot(browser):
    return _snapshot_of(_run(browser, "snap", "snapshotting the page"), browser)


def prompt_label(snapshot, seat=""):
    """The prompt box's label, or a failure that says where to look."""
    label = textbox_label(snapshot)
    if not label:
        raise GeminiError(
            "no textbox in the page snapshot — Gemini's prompt box was not found "
            f"(it is normally named {PROMPT_FALLBACK_LABEL!r}). "
            f"`ar3 tool browser --as {seat or '<seat>'} -- snap` shows what the page looks like now."
        )
    return label


BLANK_PAGE = "about:blank"


def _no_prompt(browser, snapshot):
    """Why the composer is missing, with the likely cause named.

    A blank page straight after a navigation that reported the app's own URL
    means the page did not survive to the next command. With a8s-browser 0.3.1
    or later, a covered or minimised window keeps its page, so that leaves two
    causes: Chrome was restarted with no window to return to — a8s-browser names
    the URL it lost in its own error when that happens — or somebody closed the
    seat's window by hand.
    """
    seat = browser.seat or "<seat>"
    if not snapshot.strip() or BLANK_PAGE in snapshot:
        return (
            f"the seat landed on {BLANK_PAGE} straight after opening Gemini, so the page "
            "did not survive to the next command. Either a8s-browser restarted Chrome "
            "and could not return to the page (its own error names the URL it lost), "
            f"or seat {seat!r}'s Chrome window was closed by hand. "
            f"`ar3 tool browser --as {seat} -- open` brings the seat back."
        )
    return (
        f"no textbox in the page snapshot after {PROMPT_WAIT_SECONDS:.0f}s — Gemini's "
        f"prompt box never appeared (it is normally named {PROMPT_FALLBACK_LABEL!r}). "
        f"`ar3 tool browser --as {seat} -- snap` shows the page now."
    )


# How long the page is watched for the turn an uncertain send may have made.
# It confirms a send; its expiry never disproves one.
SUBMIT_CONFIRM_SECONDS = 20.0


def await_turn_taken(browser, before, now=None, sleep=None):
    """Whether a turn from this side appears on the page within the window.

    True is proof the message went in. **False is not proof that it did not.**
    A page renders when it renders, and a snapshot taken a moment too early
    shows the previous state — which is exactly the reading that, treated as
    proof, made the driver ask Gemini the same question twice.
    """
    now, sleep = now or _now, sleep or _sleep
    deadline = now() + SUBMIT_CONFIRM_SECONDS
    while True:
        try:
            after = read(browser)
        except (GeminiError, BrowserError):
            return False
        if before and after.counts and after.counts[0] > before[0]:
            return True
        if now() >= deadline:
            return False
        sleep(POLL_SECONDS)


def await_prompt(browser, now=None, sleep=None):
    """The prompt box's label and the snapshot it was read from, waited for.

    Returns as soon as the composer exists. If it never does, this raises the
    same sentence `prompt_label` would, naming the seat to snapshot by hand.
    """
    now, sleep = now or _now, sleep or _sleep
    deadline = now() + PROMPT_WAIT_SECONDS
    while True:
        snapshot = page_snapshot(browser)
        label = textbox_label(snapshot)
        if label:
            return label, snapshot
        if now() >= deadline:
            raise GeminiError(_no_prompt(browser, snapshot))
        sleep(POLL_SECONDS)


def conversation_url(browser, now=None, sleep=None):
    """The conversation's own URL, waited for until Gemini assigns one."""
    now, sleep = now or _now, sleep or _sleep
    deadline = now() + URL_WAIT_SECONDS
    url = current_url(browser)
    while not conversation_id(url):
        if now() >= deadline:
            raise GeminiError(
                f"Gemini stayed on {url} instead of a conversation URL, so this "
                "correspondent has no conversation to come back to"
            )
        sleep(POLL_SECONDS)
        url = current_url(browser)
    return url


def read(browser, sent=""):
    """One look at the page: the newest reply, and whether it is finished."""
    transcript = _run(browser, poll_script(), "reading the conversation")
    snapshot = _snapshot_of(transcript, browser)
    history = _first_output(transcript, "text")
    return Reading(
        extract_reply(snapshot, history, sent),
        turn_complete(snapshot),
        turn_counts(snapshot),
        image_readiness(snapshot),
        image_downloads(snapshot),
    )


def is_new(reading, baseline="", baseline_counts=None):
    """Whether this reading shows a reply that was not there before the send.

    Counting the page's turn headings is the strong answer, and it is right
    even when the new reply reads exactly like the old one — `OK` after `OK` is
    two turns, not one. Comparing text is the fallback for a page whose
    headings changed shape, and it is wrong in precisely that case, which is
    why it is second.
    """
    if reading.counts and baseline_counts:
        return reading.counts[1] > baseline_counts[1]
    return bool(reading.text) and reading.text != baseline


def await_reply(browser, sent, baseline="", baseline_counts=None, now=None, sleep=None):
    """Wait for the turn Gemini is writing to finish, and return it.

    Finished means the page says so, or — if those labels have changed — that
    the text has not moved for SETTLE_SECONDS. What counts as *this* turn's
    reply is `is_new`: the page's turn count moved, or, failing that, the text
    is no longer the `baseline` that was on screen before the message went in.

    An image turn adds one condition and removes one shortcut. It is finished
    only when `image_readiness` says every image has its download control — the
    rating cluster is not trusted to wait for the pixels — and a turn with an
    image still pending never settles, because a pending image is a static page
    and a static page proves only that nothing moved. A text turn, where the
    page shows no image evidence, is judged exactly as before.
    """
    now, sleep = now or _now, sleep or _sleep
    started = now()
    last = ""
    last_seen = None
    images = None
    ready = 0
    counts = baseline_counts
    stable_since = None
    while True:
        reading = read(browser, sent)
        counts = reading.counts or counts
        fresh = is_new(reading, baseline, baseline_counts)
        text = reading.text if fresh else ""
        images = reading.images if fresh else None
        ready = reading.image_count if images is not None else 0
        has_answer = bool(text) or images is True
        seen = (text, ready)
        if has_answer and seen == last_seen:
            if stable_since is None:
                stable_since = now()
        else:
            stable_since = None
            last_seen = seen
        last = text
        if has_answer and images is not False and reading.complete:
            return Turn(text, True, now() - started, counts=counts, images=ready)
        settled = stable_since is not None and now() - stable_since >= SETTLE_SECONDS
        if has_answer and images is not False and settled:
            return Turn(
                text,
                True,
                now() - started,
                "the page never marked this turn finished; it is being reported because "
                "the text stopped changing.",
                counts=counts,
                images=ready,
            )
        elapsed = now() - started
        if elapsed >= TURN_TIMEOUT_SECONDS:
            if images is False:
                note = (
                    f"cut short: Gemini was still making an image after {elapsed:.0f}s, "
                    "so an image that was still being made is not attached."
                )
            elif last:
                note = (
                    f"cut short: Gemini was still writing after {elapsed:.0f}s, "
                    "so this reply may be unfinished."
                )
            else:
                note = (
                    f"no reply appeared within {elapsed:.0f}s. The message may not have "
                    "reached Gemini's prompt box, or the turn headings in gemini.py no "
                    "longer match the page."
                )
            return Turn(last, False, elapsed, note, counts=counts, images=ready)
        sleep(POLL_SECONDS)


def download_image(browser, index, count):
    """Fetch image `index` of `count` in the newest turn through a8s-browser's `download`.

    Returns `(path, failure)`: the file a8s-browser saved, or "" and one
    sentence saying why not. The path is a8s-browser's own artifact and may be
    reused by the next download, so the caller copies it before asking for
    another.
    """
    selector = image_download_selector(index)
    script = f"download {shlex.quote(selector)} {DOWNLOAD_WAIT_SECONDS}"
    which = f"image {index} of {count}"
    try:
        transcript = browser.run(script)
    except BrowserError as exc:
        return "", f"{which} could not be downloaded: {exc}"
    if not transcript.ok:
        return "", f"{which} could not be downloaded: {transcript.error}"
    path = _first_output(transcript, "download").strip()
    if not path or not os.path.isfile(path):
        return "", (
            f"{which} could not be downloaded: a8s-browser named {path or 'no file'}, "
            "which is not there"
        )
    return path, ""


SUBMIT_STEP = "press Enter"


def _failed_step(transcript):
    for step in transcript.steps:
        if not step.get("ok"):
            return step
    return None


def consent_pending(snapshot):
    """Whether Gemini is holding a file behind its first-upload disclaimer."""
    return CONSENT_HEADING.lower() in (snapshot or "").lower()


def _consent_error(browser, names):
    return GeminiError(
        f"Gemini is asking this profile to accept its disclaimer about uploaded files "
        f"before it will take one, so {', '.join(names)} was not sent and neither was "
        f"your message. Someone has to accept it by hand, once: open the seat "
        f"(`ar3 tool browser --as {browser.seat} -- open`), attach any file to a chat, and press "
        f"Agree in the dialog that appears. This driver does not accept terms on an "
        f"account it drives."
    )


def send_ready(snapshot):
    """Whether the composer will send what it is holding.

    `True` ready, `False` not yet, and **`None` for no evidence either way** —
    which is a different answer and is never treated as ready. The control is
    absent from an empty composer, so `None` after the text is typed means the
    page changed shape and this driver can no longer tell a finished upload from
    a running one.
    """
    for line in (snapshot or "").splitlines():
        role, name, _ = node(line)
        if role == "button" and SEND_BUTTON_NAME.lower() in (name or "").lower():
            return "[disabled]" not in line
    return None


# A long name is shortened in the chip to its first and last few characters
# around three dots: `a-rather-l...check-2026` for a 47-character name.
CHIP_KEEP = 10


def chip_label(name):
    """What Gemini's attachment chip shows for a file.

    The live chip (2026-09-24) is a clickable `generic` holding two more: the
    extension in capitals (`TXT`) and the name without it (`probe-note`). A
    long name is cut to its first and last `CHIP_KEEP` characters around
    `...`. The full filename is not in the chip at all, so it is not what is
    counted.
    """
    stem, _ = os.path.splitext(name)
    stem = stem or name
    if len(stem) > 2 * CHIP_KEEP + 3:
        return f"{stem[:CHIP_KEEP]}...{stem[-CHIP_KEEP:]}"
    return stem


def _prompt_box_index(lines):
    """The line of the prompt box, preferring a textbox that says what it is for."""
    fallback = -1
    for index, line in enumerate(lines):
        role, name, _ = node(line)
        if role != "textbox":
            continue
        if any(hint in name.lower() for hint in PROMPT_HINTS):
            return index
        if fallback < 0:
            fallback = index
    return fallback


def _composer_span(lines):
    """`(start, end)` of the composer — the `group` that holds the prompt box —
    with `start` the group's own line, or None when there is no such group."""
    box = _prompt_box_index(lines)
    if box < 0:
        return None
    depth = _indent(lines[box])
    start = -1
    for index in range(box - 1, -1, -1):
        indent = _indent(lines[index])
        if indent >= depth:
            continue
        depth = indent
        if node(lines[index])[0] == "group":
            start = index
            break
    if start < 0:
        return None
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or _indent(lines[end]) > depth):
        end += 1
    return start, end


def _composer(lines):
    """The composer's lines, or None.

    None is "no composer found", and it is an answer of its own: nothing inside
    it can be counted, so no upload can be confirmed from it.
    """
    span = _composer_span(lines)
    if span is None:
        return None
    return lines[span[0] + 1:span[1]]


def chips(snapshot, names):
    """What the composer shows for these files, or None when there is no composer.

    Returns `(labels, images)`. `labels` has one entry per file: how many
    document-chip labels in the composer read exactly that file's `chip_label`.
    `images` is how many image thumbnails the composer holds.

    Only chip nodes are read. A document chip's label is the value of a
    `generic` node (`- generic: probe-note`); a thumbnail is an `img
    "attachment"` node. Nothing else in the composer is evidence — not the
    thumbnail's own name, not the prompt the sender typed, not the snapshot's
    YAML — so a file called `attachment.pdf` is never confirmed by a picture,
    and a short name is never confirmed by the text around it.
    """
    composer = _composer((snapshot or "").splitlines())
    if composer is None:
        return None
    found = {}
    images = 0
    for line in composer:
        role, name, value = node(line)
        if role == "img" and name == IMAGE_CHIP_NAME:
            images += 1
        elif role == "generic" and not name and value:
            found[value] = found.get(value, 0) + 1
    return tuple(found.get(chip_label(name), 0) for name in names), images


def _chip_kind(name):
    ext = os.path.splitext(name)[1].lower()
    if ext in IMAGE_UPLOAD_TYPES:
        return "image"
    if ext in EITHER_CHIP_TYPES:
        return "either"
    return "named"


def uploads_shown(before, after, names):
    """Whether the composer shows every file of this upload, each by a chip of its own.

    Evidence is what is new since the baseline, and each new chip or thumbnail
    is given to **one** file at most — a matching, not a count per file. Two
    files whose labels read the same need two new chips with that label.

    - A document takes one new chip carrying its label.
    - An image takes one new thumbnail.
    - A type nobody has watched land takes one of whichever is left: a new chip
      with its own label first, since only it can use that, then a thumbnail.
    """
    (was_labels, was_images), (labels, images) = before, after
    kinds = [_chip_kind(name) for name in names]
    new_labels = {}
    for name, now, was in zip(names, labels, was_labels, strict=True):
        new_labels[chip_label(name)] = now - was
    for name, kind in zip(names, kinds, strict=True):
        if kind == "named":
            new_labels[chip_label(name)] -= 1
    if any(left < 0 for left in new_labels.values()):
        return False
    thumbnails = images - was_images - kinds.count("image")
    if thumbnails < 0:
        return False
    for name, kind in zip(names, kinds, strict=True):
        if kind != "either":
            continue
        label = chip_label(name)
        if new_labels[label] > 0:
            new_labels[label] -= 1
        elif thumbnails > 0:
            thumbnails -= 1
        else:
            return False
    return True


def upload_script(path):
    """The steps that put one file into the composer through Gemini's own menu."""
    return "\n".join([
        f"click {shlex.quote(UPLOAD_MENU_SELECTOR)}",
        "wait 1",
        f"click {shlex.quote(UPLOAD_MENU_ITEM)}",
        "wait 1",
        f"upload {shlex.quote(path)}",
    ])


def _upload_one(browser, path, names):
    """Open the upload menu, pick `Upload files`, and answer the chooser.

    A step that fails leaves the page in whatever state it reached, so the page
    is read before anything else: a consent dialog is the operator's to answer
    and is reported as that. Otherwise the open menu is dismissed, because an
    open menu swallows the keystrokes that follow it.
    """
    name = os.path.basename(path)
    try:
        transcript = browser.run(upload_script(path))
    except BrowserError as exc:
        _dismiss_menu(browser)
        raise GeminiError(f"putting {name} into the conversation: {exc}") from exc
    if transcript.ok:
        return
    try:
        snapshot = page_snapshot(browser)
    except GeminiError:
        snapshot = ""
    if consent_pending(snapshot):
        raise _consent_error(browser, names)
    _dismiss_menu(browser)
    raise GeminiError(f"putting {name} into the conversation: {transcript.error}")


def attach(browser, paths, wait=UPLOAD_WAIT_SECONDS, now=None, sleep=None):
    """Put files into the composer and wait until it will send them.

    Nothing here submits anything, so every failure is a message that was not
    sent — the one case this driver can safely let a8s retry. Giving up is the
    same choice as failing: a question about a document nobody attached reads as
    a model failure, and it costs a turn to discover.

    Confirmation is two things, and "the filename is on the page" is neither.

    **The name has to be new.** The snapshot is the whole page, conversation
    included, so a name mentioned in an earlier turn would confirm an upload
    that has not begun. What is required is one *more* occurrence than a
    baseline taken before the upload — a count, not a presence, so sending the
    same filename twice still works.

    **The composer has to say it will send.** A filename renders while the
    upload is still running, and a page that has stopped changing is not proof
    of anything: a paused progress bar and a disabled button are textually
    identical from one poll to the next. `Send message` is the page's own answer
    to "will you send this now", and it is positive evidence rather than the
    absence of a marker this driver has never been shown.

    That control does not exist while the prompt is empty, which is why the text
    is typed before the files go in. If it cannot be found at all, readiness is
    *unknown*, and unknown is not ready: the message is kept and the reply says
    which prerequisite went missing.
    """
    now, sleep = now or _now, sleep or _sleep
    names = [os.path.basename(path) for path in paths]

    before = _snapshot_of(_run(browser, "snap", "reading the composer"), browser)
    if consent_pending(before):
        raise _consent_error(browser, names)
    baseline = chips(before, names)

    for path in paths:
        _upload_one(browser, path, names)

    deadline = now() + wait
    ready = None
    while True:
        snapshot = _snapshot_of(_run(browser, "snap", "reading the composer"), browser)
        if consent_pending(snapshot):
            raise _consent_error(browser, names)
        shown = chips(snapshot, names)
        # No composer, before or now, is no evidence: the same answer as a
        # missing send control, and never ready.
        ready = send_ready(snapshot) if shown is not None and baseline is not None else None
        if ready and uploads_shown(baseline, shown, names):
            return names
        if now() >= deadline:
            break
        sleep(POLL_SECONDS)

    if ready is None:
        raise GeminiError(
            f"this seat cannot tell whether {', '.join(names)} finished uploading, so "
            f"your message was not sent. Gemini's composer — the group holding the "
            f"prompt box, with its file chips and its {SEND_BUTTON_NAME!r} control — "
            "is not in the page the way this driver reads it to know an upload is "
            "done. The page has changed shape — see "
            "docs/gemini-ui.md, which names the row to fix."
        )
    raise GeminiError(
        f"{', '.join(names)} did not finish uploading in Gemini's composer within "
        f"{wait:.0f}s, so your message was not sent — a question about a file "
        "Gemini never received is worse than one you can send again."
    )


def send_with_files(browser, label, message, paths):
    """Type the message, put the files in, and only then send.

    Three steps rather than one script, because the middle one has to look at
    the page between the other two. The order is what makes the look worth
    anything: the send control does not render on an empty composer, so the text
    goes first, and then a disabled control means "still uploading" rather than
    "nothing to send".

    The keystroke is its own run, which keeps the uncertainty boundary exactly
    where `send` puts it — everything before it is a definite non-send, and only
    the keystroke itself is unknown.
    """
    _run(browser, compose_script(label, message), "typing the message into Gemini")
    attached = attach(browser, paths)
    try:
        transcript = browser.run(SUBMIT_STEP)
    except BrowserError as exc:
        raise SendUncertain(
            f"sending the message: {exc}. The seat never answered, so whether Gemini "
            "received the message cannot be told from here."
        ) from exc
    if not transcript.ok:
        raise SendUncertain(
            f"sending the message: {transcript.error}. That failure is on the keystroke "
            "that sends, so whether Gemini received the message cannot be told from here."
        )
    return attached


def send(browser, label, message):
    """Type the message and submit it, or say which of those is not known.

    a8s-browser stops a script at its first failed step and reports which one,
    and the submitting keystroke is the last step. So a named failure on any
    earlier step is proof the message was never submitted, while a failure on
    the keystroke itself — or a seat that returned no transcript at all — is
    genuinely unknown and is raised as `SendUncertain`.
    """
    script = send_script(label, message)
    try:
        transcript = browser.run(script)
    except BrowserError as exc:
        raise SendUncertain(
            f"typing the message into Gemini: {exc}. The seat never answered, so "
            "whether the message was submitted cannot be told from here."
        ) from exc
    if transcript.ok:
        return
    failed = _failed_step(transcript) or {}
    if str(failed.get("command") or "").strip() == SUBMIT_STEP:
        raise SendUncertain(
            f"typing the message into Gemini: {transcript.error}. That failure is on "
            "the keystroke that sends, so whether Gemini received the message cannot "
            "be told from here."
        )
    raise GeminiError(f"typing the message into Gemini: {transcript.error}")


def send_turn(browser, label, message, baseline="", baseline_counts=None, now=None, sleep=None):
    send(browser, label, message)
    return await_reply(browser, message, baseline, baseline_counts, now=now, sleep=sleep)


def _dismiss_menu(browser):
    """Leave no menu open behind a step that failed part-way through one."""
    try:
        browser.run("press Escape")
    except BrowserError:
        pass


def _click_first(browser, selectors, doing):
    """Click the first selector that matches. Returns "" or why none did."""
    failures = []
    for selector in selectors:
        script = f"click {shlex.quote(selector)}\nwait 1"
        try:
            transcript = browser.run(script)
        except BrowserError as exc:
            failures.append(f"{selector}: {exc}")
            continue
        if transcript.ok:
            return ""
        failures.append(f"{selector}: {transcript.error}")
    return f"{doing} matched nothing: " + "; ".join(failures)


def choose_model(browser, model, snapshot=""):
    """Pick a model from Gemini's switcher. Best effort, and never fatal.

    The mode picker names the model it is already on, so a seat that is on the
    right one changes nothing and never opens the menu. A seat that cannot open
    it, or cannot find the model in it, keeps the turn and runs on Gemini's
    default — the product is meant to need no configuration, so a preference
    that cannot be applied is a note, not a lost message.
    """
    if not model:
        return ""
    on = current_model(snapshot) if snapshot else ""
    if on and model.lower() in on.lower():
        return ""
    failure = _click_first(browser, MODEL_BUTTON_SELECTORS, "Gemini's mode picker")
    if not failure:
        failure = _click_first(browser, (model,), f"the model menu item {model!r}")
    if failure:
        _dismiss_menu(browser)
        return (
            f"model {model!r} not selected — {failure}. "
            f"Gemini's default{f' ({on})' if on else ''} is in use."
        )
    return ""
