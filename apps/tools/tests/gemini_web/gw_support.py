import os
import re
import shlex
import sys

from gemini_web import browser_seat as browser
from gemini_web import gemini

# a8s-browser's own script rules, mirrored so a script this driver generates is
# parsed here by the rules the real seat parses it by. Kept honest by
# `tests/test_browser_parser.py`, which runs the real parser when a checkout of
# a8s-browser is reachable and compares it against this one case by case.
#
# Verbs that take the rest of the line verbatim instead of shlex words:
RAW_ARGS = {"eval", "run-code", "type", "find", "assert-text", "video-chapter", "dialog-accept"}

# `<<MARKER` at the end of a line opens a block; the marker is the last word.
HEREDOC = re.compile(r"^(?P<command>.*?)\s*<<\s*(?P<marker>\S+)$")


class ScriptError(Exception):
    """A script the seat refuses to parse — no step of it runs."""


def script_commands(body):
    """Commands of a script, each with the block it opened or None.

    Every line is stripped before it is read, which is exactly why a message
    cannot travel as an inline argument. A block's lines are taken verbatim:
    no stripping, no comment rules, indentation kept.
    """
    parsed = []
    lines = body.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line or line.startswith("#"):
            continue
        if line.startswith(("ATTACHED FILE:", "ATTACHMENT UNAVAILABLE:")):
            continue
        opener = HEREDOC.match(line)
        command = opener.group("command").strip() if opener else ""
        if not command:
            parsed.append((line, None))
            continue
        if len(command.split()) > 1:
            raise ScriptError(
                "a line that opens a block cannot also carry an inline argument: " + line
            )
        marker = opener.group("marker")
        block = []
        for raw in lines[index:]:
            index += 1
            if raw.strip() == marker:
                break
            block.append(raw)
        else:
            raise ScriptError(f"unterminated <<{marker}: no line reads {marker}")
        parsed.append((command, "\n".join(block)))
    return parsed


def parse_script(script):
    """One script as (verb, args, line) steps, the way a8s-browser runs it."""
    parsed = []
    for line, block in script_commands(script):
        verb = line.split(None, 1)[0]
        if verb in RAW_ARGS:
            args = line.split(None, 1)[1:]
        else:
            words = shlex.split(line)
            verb, args = words[0], words[1:]
        if block is not None:
            args = [block]
        parsed.append((verb, args, line))
    return parsed


class StepFailed(Exception):
    """What a8s-browser reports for a step that matched nothing."""


class FakeClock:
    """Virtual time: sleeping advances it, so a settling test costs nothing."""

    def __init__(self):
        self.seconds = 0.0

    def now(self):
        return self.seconds

    def sleep(self, seconds):
        self.seconds += seconds


class ScriptedSeat:
    """A seam that answers each run with the next transcript it was given."""

    def __init__(self, transcripts, seat="profile"):
        self.seat = seat
        self.transcripts = list(transcripts)
        self.scripts = []

    def run(self, script):
        self.scripts.append(script)
        if not self.transcripts:
            raise AssertionError(f"the seat was run more times than scripted:\n{script}")
        answer = self.transcripts.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeGeminiSeat:
    """A signed-in Gemini seat, as far as the command vocabulary can tell.

    It parses each script the way a8s-browser does and keeps the state a real
    page would: what is in the prompt box, what has been said, where the
    browser is. Its snapshots are in the shape the live page was observed to
    produce, with invented content. Everything the driver believes about Gemini
    is exercised against it — quoting, multi-line typing, reply extraction,
    completion — with no browser and no network.
    """

    def __init__(self, tmp_path, seat="profile", reply=None, stream_polls=0):
        self.seat = seat
        self.tmp_path = tmp_path
        self.reply = reply or (
            lambda prompt: "ack: " + (prompt.strip().splitlines() or [""])[0][:40]
        )
        self.stream_polls = stream_polls
        self.label = gemini.PROMPT_FALLBACK_LABEL
        self.model = "Flash"
        self.url = gemini.APP_URL
        self.history = []
        self.scripts = []
        self.draft = ""
        self.signed_in = True
        self.conversations = 0
        self.ids = set()
        self.clickable = {
            gemini.MODEL_BUTTON_SELECTORS[0],
            gemini.UPLOAD_MENU_SELECTOR,
        }
        # Gemini's upload menu and the file chooser its `Upload files` opens.
        self.menu_open = False
        self.chooser_open = False
        # Files in the composer, as chips the page would render.
        self.attached = []
        # An upload that raises Gemini's first-upload disclaimer instead of
        # opening the chooser, which is what a fresh profile does.
        self.consent_needed = False
        self.consent_open = False
        # Snaps an upload takes to show up, so the wait itself is exercised.
        self.upload_polls = 0
        # Snaps the upload stays PENDING for after its chip appears — a real
        # upload renders its filename while it is still running, and the send
        # control stays disabled until it finishes. A static indicator is the
        # point: nothing about the page needs to change while it is pending.
        self.upload_pending_polls = 0
        # A page that no longer carries the send control at all, so readiness
        # cannot be established either way.
        self.send_button = True
        self._pending_uploads = []
        # Images the next model turn generates, as (filename, bytes).
        self.next_images = []
        # Images per model turn, keyed by that turn's index in `history`.
        self.turn_images = {}
        # Snaps an image stays drawn without its download control, with the
        # rating cluster already showing — the worst case the page could have.
        self.image_pending_polls = 0
        # 1-based image numbers whose download fails in the browser.
        self.download_fails = set()

    def run(self, script):
        self.scripts.append(script)
        steps = []
        files = []
        for verb, args, line in parse_script(script):
            try:
                output = self._step(verb, args)
            except StepFailed as exc:
                steps.append({"command": line, "ok": False, "error": str(exc)})
                return browser.Transcript(False, steps, files)
            if verb == "snap":
                files.append(output)
            steps.append({"command": line, "ok": True, "output": str(output or "")})
        return browser.Transcript(True, steps, files)

    def _step(self, verb, args):
        step = getattr(self, f"_do_{verb.replace('-', '_')}", None)
        if step is None:
            raise StepFailed(f"unknown command: {verb}")
        return step(args)

    def _visible_reply(self, text):
        """What is on screen of a reply that is still being written."""
        if self.stream_polls <= 0:
            return text, True
        self.stream_polls -= 1
        shown = max(1, len(text) // (self.stream_polls + 2))
        return text[:shown], False

    def _snapshot(self):
        if self._pending_uploads:
            if self.upload_polls > 0:
                self.upload_polls -= 1
            else:
                self.attached += self._pending_uploads
                self._pending_uploads = []
        lines = [
            "- generic [ref=e1]:",
            f'  - button "{gemini.MODE_PICKER_PREFIX} {self.model}" [ref=e2]',
        ]
        if self.consent_open:
            lines.append(f'  - heading "{gemini.CONSENT_HEADING}" [level=1] [ref=e3]')
            lines.append(
                '  - button "Cancel (Closes dialog box and does not enable MMGen)" [ref=e4]'
            )
            lines.append('  - button "Agree (Closes dialog box and gives disclaimer)" [ref=e5]')
        pending = False
        # The composer: the group holding the prompt box, its chips and its send
        # control, in the shape the live page has (2026-09-24).
        composer = []
        for name in self.attached:
            ext = os.path.splitext(name)[1]
            if ext.lower() in gemini.IMAGE_UPLOAD_TYPES:
                # An image is a thumbnail with no name anywhere.
                composer.append(f'      - img "{gemini.IMAGE_CHIP_NAME}" [ref=e6]')
                continue
            # A document: the extension in capitals, then the name without it.
            composer += [
                "      - generic [ref=e6] [cursor=pointer]:",
                f"        - generic [ref=e6a]: {ext.lstrip('.').upper()}",
                f"        - generic [ref=e6b]: {gemini.chip_label(name)}",
            ]
        if self.attached and self.upload_pending_polls > 0:
            # The chip is there and the upload is not done. Deliberately static:
            # two identical snapshots must not read as a finished upload.
            self.upload_pending_polls -= 1
            pending = True
            composer.append('      - progressbar "Uploading" [ref=e7]')
        for index, (role, text) in enumerate(self.history):
            ref = f"e{10 + index * 4}"
            last = index == len(self.history) - 1
            if role == "user":
                first = text.splitlines()[0] if text.splitlines() else ""
                lines.append(
                    f'  - heading "{gemini.USER_TURN_HEADING} {first}" [level=5] [ref={ref}]'
                )
                lines += [f"  - paragraph: {line}" for line in text.splitlines() if line.strip()]
                lines.append(f'  - button "Copy prompt" [ref={ref}b]')
                continue
            shown, done = (text, True) if not last else self._visible_reply(text)
            lines.append(f'  - heading "{gemini.MODEL_TURN_HEADING}" [level=6] [ref={ref}]')
            lines += [f"  - paragraph: {line}" for line in shown.splitlines() if line.strip()]
            images = self.turn_images.get(index, [])
            drawing = bool(last and images and self.image_pending_polls > 0)
            if drawing:
                self.image_pending_polls -= 1
            lines += self._image_lines(images, ref, drawing)
            if done:
                lines += [
                    f'  - button "{label}" [ref={ref}{n}]'
                    for n, label in enumerate(gemini.COMPLETION_BUTTONS)
                ]
        composer.append(f'      - textbox "{self.label}" [ref=e90]')
        if self.draft and self.send_button:
            # The live control only exists once the prompt box has text, and a
            # snapshot marks a disabled node with a trailing `[disabled]`.
            flag = " [disabled]" if pending else ""
            composer.append(f'      - button "{gemini.SEND_BUTTON_NAME}" [ref=e91]{flag}')
        lines += ["  - group [ref=e88]:", "    - generic [ref=e89]:", *composer]
        lines.append("  - paragraph: Gemini is AI and can make mistakes.")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _image_lines(images, ref, pending):
        """Generated images in the shape the live page draws them (2026-09-24)."""
        lines = []
        for number, _ in enumerate(images, 1):
            lines += [
                f"  - generic [ref={ref}i{number}]:",
                f"    - button [ref={ref}t{number}] [cursor=pointer]:",
                f"      - img [ref={ref}p{number}]",
            ]
            if pending:
                continue
            lines += [
                f"    - generic [ref={ref}c{number}]:",
                f'      - button "Share image" [ref={ref}s{number}] [cursor=pointer]:',
                f"        - img [ref={ref}s{number}i]: share_1",
                f'      - button "Copy image" [ref={ref}y{number}] [cursor=pointer]:',
                f"        - img [ref={ref}y{number}i]: content_copy",
                f'      - button "{gemini.IMAGE_DOWNLOAD_NAME}" [ref={ref}d{number}]'
                " [cursor=pointer]:",
                f"        - img [ref={ref}d{number}i]: download",
            ]
        return lines

    def _do_download(self, args):
        """a8s-browser's `download`: click the target, save what came back as an artifact."""
        match = re.search(r":nth-child\((\d+) of ", args[0])
        newest = max(self.turn_images, default=None)
        images = self.turn_images.get(newest, []) if newest == len(self.history) - 1 else []
        if not match or not args[0].startswith(gemini.NEWEST_EXCHANGE_SELECTOR):
            raise StepFailed(f"click: nothing visible matching {args[0]!r}")
        number = int(match.group(1))
        if number > len(images):
            raise StepFailed(f"click: selector {args[0]!r} matched nothing visible and enabled")
        if number in self.download_fails:
            raise StepFailed(f"download: {args[0]} produced no download within 60s")
        name, data = images[number - 1]
        folder = os.path.join(str(self.tmp_path), "artifacts")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"20260924T074500-{name}")
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def _do_upload(self, args):
        """a8s-browser's `upload`: answer an open file chooser with one path."""
        if len(args) != 1:
            raise StepFailed("upload takes one file")
        if not self.chooser_open:
            raise StepFailed("upload: no file chooser is open")
        if not os.path.isfile(args[0]):
            raise StepFailed(f"upload: no such file: {args[0]}")
        self.chooser_open = False
        self._pending_uploads.append(os.path.basename(args[0]))
        return os.path.basename(args[0])

    def _do_go(self, args):
        if not self.signed_in:
            self.url = "https://accounts.google.com/signin/v2/identifier"
            return self.url
        wanted = gemini.conversation_id(args[0])
        # A conversation that cannot be opened lands back on the bare app.
        self.url = args[0] if (not wanted or wanted in self.ids) else gemini.APP_URL
        if not gemini.conversation_id(self.url):
            self.history = []
            return self.url

    def _do_url(self, args):
        return self.url

    def _do_wait(self, args):
        return f"{args[0]}s"

    def _do_snap(self, args):
        path = os.path.join(str(self.tmp_path), f"snapshot-{len(self.scripts)}.txt")
        with open(path, "w") as handle:
            handle.write(self._snapshot())
        return path

    def _do_fill(self, args):
        target, text = args[0], " ".join(args[1:])
        if target == self.label:
            self.draft = text
            return target
        raise StepFailed(f"fill: nothing visible matching {target!r}")

    def _do_type(self, args):
        self.draft += args[0]
        return "typed"

    def _do_press(self, args):
        key = args[0]
        if key == "Shift+Enter":
            self.draft += "\n"
            return key
        if key == "Escape":
            self.menu_open = False
            self.clickable.discard(gemini.UPLOAD_MENU_ITEM)
            return key
        if key != "Enter":
            return key
        prompt, self.draft = self.draft, ""
        self.history.append(("user", prompt))
        self.history.append(("model", self.reply(prompt)))
        if self.next_images:
            self.turn_images[len(self.history) - 1] = self.next_images
            self.next_images = []
        if not gemini.conversation_id(self.url):
            self.conversations += 1
            self.url = f"{gemini.APP_URL}/c{self.conversations:04d}ab"
            self.ids.add(gemini.conversation_id(self.url))
        return key

    def _do_click(self, args):
        target = args[0]
        if target not in self.clickable:
            raise StepFailed(f"click: nothing visible matching {target!r}")
        if target == gemini.UPLOAD_MENU_SELECTOR:
            self.menu_open = True
            self.clickable.add(gemini.UPLOAD_MENU_ITEM)
        elif target == gemini.UPLOAD_MENU_ITEM:
            self.menu_open = False
            self.clickable.discard(gemini.UPLOAD_MENU_ITEM)
            if self.consent_needed:
                # The page asks a person to accept its disclaimer; no chooser.
                self.consent_open = True
            else:
                self.chooser_open = True
        elif target in gemini.MODEL_BUTTON_SELECTORS:
            self.clickable |= {"Pro", "Flash"}
        elif target in ("Pro", "Flash"):
            self.model = target
        return target

    def _do_text(self, args):
        if args[0] != gemini.HISTORY_SELECTOR:
            return ""
        body = "\n".join(text for _, text in self.history)
        return f"{body}\nGemini is AI and can make mistakes."


class Outbox:
    """Every tell this seat sends, instead of the `tell` CLI."""

    def __init__(self):
        self.sent = []

    def __call__(self, recipient, body, files=()):
        self.sent.append((recipient, body, list(files or ())))

    @property
    def last(self):
        return self.sent[-1][1]

    @property
    def last_files(self):
        """Paths attached to the newest reply, as `tell --attach` would get."""
        return self.sent[-1][2]

    @property
    def last_names(self):
        return [os.path.basename(path) for path in self.last_files]
