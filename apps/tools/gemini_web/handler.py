"""a8s wake entry point: a tell in, Gemini's answer back out.

Every exit is 0. a8s acks an envelope on 0 and dead-letters it on anything
else, once, with no retry: a non-zero exit would lose the message without a
word. So a turn that could not be run at all (the seat is busy, the profile is
not signed in, the browser is unreachable) answers the sender with a reply that
says the message was not sent and must be sent again. A sender who is not on
the allowlist gets no reply.

Two cases sit between those and are handled rather than guessed at. A send that
fails on the keystroke that submits is *not known* to have failed, so the page
is asked before the message is ever sent again. And a reply that is produced
but cannot be handed to `tell` is held on disk, because the turn that produced
it has already been spent and re-running it would ask Gemini the same question
twice.
"""
import os
import shutil
import subprocess
import sys

import _runtime as rt

from . import attachments
from . import browser_seat as browser
from . import gemini
from . import identity
from . import store as store_module
from .outbox import Outbox
from .store import SessionStore, StoreError

MAX_BODY = 4000

# A reply too long for the body is sent as a file instead of being cut. The cap
# exists so one answer cannot flood a mailbox; an attachment does not sit in the
# message text, so it is not what the cap is protecting against.
LONG_REPLY_NAME = "gemini-reply.md"

# Where a turn's generated images are copied before they are attached, inside
# the turn's own work directory.
OUTBOUND_DIR = "out"

# Body room given back when the whole reply travels as a file. `_compose` caps
# the body and its notes together, so an excerpt that fills the cap exactly
# would push out the note saying where the rest of the answer went.
LONG_REPLY_RESERVE = 600

# How long a wake waits for another run of this seat to finish its turn. A turn
# is bounded by gemini.TURN_TIMEOUT_SECONDS, so a longer wait here would spend
# this wake queueing; requeueing the message costs nothing and frees the seat.
TURN_LOCK_SECONDS = 30.0

UNCERTAIN_NOTE = (
    "this seat cannot tell whether Gemini received your message. It was NOT sent "
    "again: a duplicate question in a live conversation is worse than one you can "
    "repeat yourself. Check the conversation, and send it again if it is not there."
)


UNSUPPORTED_NOTE = "This seat runs on Linux and macOS only; your message was not sent."

NOT_SENT_NOTE = "Your message was not sent. Send it again."


CUT_MARK = "\n… (cut at this seat's reply cap)"


def _capped(body):
    if len(body) <= MAX_BODY:
        return body
    return body[: MAX_BODY - 40].rstrip() + CUT_MARK


def _notes_block(notes):
    kept = [note for note in notes if note]
    return "--\n" + "\n".join(kept) if kept else ""


def _compose(reply, notes):
    """The body: the reply, then the notes — and the notes always survive the cap.

    The notes are what the seat says about delivery: an image that did not
    download, a file that was not held, where the rest of a long answer went.
    Cutting from the end would cut exactly those, so when the two do not fit
    together it is the reply that is shortened.
    """
    head = reply.strip()
    tail = _notes_block(notes)
    body = "\n\n".join(part for part in (head, tail) if part)
    if len(body) <= MAX_BODY:
        return body
    room = MAX_BODY - len(tail) - len(CUT_MARK) - 2
    if not head or room <= 0:
        return _capped(tail or head)
    return head[:room].rstrip() + CUT_MARK + "\n\n" + tail


def _status(send, recipient, body, files=()):
    """One delivery attempt as a status, however it went wrong.

    A sender that answers with nothing counts as delivered, which is what an
    in-process caller means. One that cannot start a process at all counts as
    a failure, not as an exception for somebody else to handle.
    """
    try:
        return int(send(recipient, body, files) or 0)
    except OSError as exc:
        print(f"a8s-gemini-web: delivery to {recipient} could not start ({exc})", file=sys.stderr)
        return 127


def _deliver(send, outbox, recipient, body, files=()):
    """Send a reply, and keep it if that fails.

    A reply that cannot be handed over is held on disk and delivered by the
    next run of this seat, without asking Gemini anything a second time. Its
    attachments are copied into the queue with it, because the artifacts they
    point at do not outlive the seat's own housekeeping.
    """
    if _status(send, recipient, body, files) == 0:
        return True
    try:
        path = outbox.keep(recipient, body, files)
    except OSError as exc:
        # Nothing can be held, so the answer is not allowed to die with the
        # failed send. It goes out once more, with a note about the state.
        print(f"a8s-gemini-web: could not hold the reply for {recipient} ({exc})", file=sys.stderr)
        note = f"--\nthis seat could not save state ({exc}), so this answer is sent now."
        if _status(send, recipient, body.rstrip() + "\n\n" + note, files) != 0:
            print(f"a8s-gemini-web: the reply for {recipient} was lost:\n{body}", file=sys.stderr)
        return False
    print(
        f"a8s-gemini-web: tell to {recipient} failed; the reply is held at {path} "
        "and the next run of this seat will try again",
        file=sys.stderr,
    )
    return False


def _reconcile(runner, before, notes, state, exc):
    """Settle whether an uncertain send actually reached the conversation.

    The page is watched for a turn from this side. Seeing one is proof the
    message went in, and this turn carries on to wait for the answer.

    **Not seeing one proves nothing.** Gemini renders when it renders, and a
    snapshot taken a moment too early shows the state before the send. Reading
    that as "never sent" is what asked Gemini the same question twice, so the
    only thing that is ever treated as a definite non-send is a named failure
    on a step before the submitting keystroke — which never reaches here.
    Everything else stays uncertain, and uncertain means the message is kept
    out of the queue and the sender is told exactly that.
    """
    if gemini.await_turn_taken(runner, before):
        notes.append(
            "the browser lost its answer while sending, but the message is in the "
            "conversation, so it was not sent again."
        )
        state["typed"] = True
        return
    state["typed"] = True
    state["uncertain"] = True
    raise exc from None


def _round_trip(seat, sender, message, runner, store, model, notes, state, paths=()):
    """One correspondent's conversation, resumed or created, and one turn in it.

    `state["typed"]` records the moment the message itself reaches Gemini,
    because that is what decides whether a failure after it should be retried.
    """
    entry = store.get(sender) or {}
    label = ""
    baseline = ""
    counts = None
    if entry.get("url"):
        try:
            gemini.open_conversation(runner, entry["url"])
            label, _ = gemini.await_prompt(runner)
            before = gemini.read(runner)
            baseline, counts = before.text, before.counts
        except gemini.GeminiError as exc:
            notes.append(f"the stored conversation was not usable ({exc}); starting a new one")
            entry = {}

    if not entry.get("url"):
        gemini.open_app(runner)
        label, snapshot = gemini.await_prompt(runner)
        notes.append(gemini.choose_model(runner, model, snapshot))
        opening = gemini.send_turn(runner, label, identity.preamble(seat, sender))
        try:
            store.remember(sender, gemini.conversation_url(runner))
        except StoreError as exc:
            # The conversation exists and holds the preamble, but nothing can
            # come back to it. Your message has not been typed yet, so it is
            # kept: fixing the store and retrying costs one stray empty chat.
            raise gemini.GeminiError(
                f"{exc}. Your message was not sent — fix that and it will be retried."
            ) from exc
        if gemini.rate_limited(opening.reply):
            return opening
        baseline, counts = opening.reply, opening.counts

    try:
        if paths:
            # Type, attach, then send — three steps, because the composer has to
            # be asked whether it will send before the keystroke. Everything
            # before that keystroke is a definite non-send, so a refusal here is
            # a message a8s can safely hand back.
            attached = gemini.send_with_files(runner, label, message, paths)
            notes.append("attached to this turn: " + ", ".join(attached))
        else:
            gemini.send(runner, label, message)
    except gemini.SendUncertain as exc:
        _reconcile(runner, counts, notes, state, exc)
    else:
        state["typed"] = True
    return gemini.await_reply(runner, message, baseline, counts)


def handle(
    seat,
    sender,
    message,
    browser_seat=None,
    allow=None,
    model=None,
    runner=None,
    send=None,
    deliver=None,
):
    """`browser_seat`, `allow` and `model` come from the definition's argv
    (the node's BROWSER_SEAT, ALLOW and MODEL vars).

    `send` is what this call does with its own reply — `tell` on a wake, and
    the terminal for a hand-run `ask`. `deliver` is how *held* replies reach
    their correspondents, and it is `tell` whatever `send` is. They are two
    arguments because collapsing them means a hand-run `ask` prints somebody
    else's queued answer to the operator and deletes it unsent.
    """
    send = send or rt.send_reply
    deliver = deliver or rt.send_reply
    spec = allow or ""
    if not rt.allowed(sender, spec):
        print(f"a8s-gemini-web: {sender!r} is not in ALLOW — refused", file=sys.stderr)
        return 0

    if store_module.fcntl is None:
        # No file locks on this platform, so no store work is safe. Answer first.
        _status(send, sender, UNSUPPORTED_NOTE)
        return 0

    store = SessionStore(seat)
    outbox = Outbox(seat, store.root)

    # Held replies go out before this turn runs, and never through `send`.
    # In print mode (a hand-run wake) `deliver` only prints and returns 0, so a
    # flush would delete held replies that nobody was sent. They stay queued.
    if rt.print_mode():
        held = len(outbox.waiting())
        if held:
            print(f"gemini-web: {held} held reply(ies) left queued (print mode)", file=sys.stderr)
    else:
        try:
            outbox.flush(lambda to, body, files: _status(deliver, to, body, files))
        except (OSError, StoreError) as exc:
            print(f"a8s-gemini-web: held replies could not be flushed ({exc})", file=sys.stderr)

    profile = browser_seat or ""
    if not profile:
        _deliver(
            send,
            outbox,
            sender,
            f"{seat}: not configured — this node has no browser seat. Set "
            "BROWSER_SEAT to the browser seat that is signed in to Gemini.",
        )
        return 0

    model = model or ""
    runner = runner or browser.BrowserSeat(profile)

    try:
        with store_module.turn_lock(seat, store.root, timeout=TURN_LOCK_SECONDS):
            return _turn(seat, sender, message, runner, store, model, send, outbox)
    except store_module.Busy:
        # One browser window, one conversation at a time. The message is kept
        # rather than answered, so nothing interleaves in the page.
        print(f"a8s-gemini-web: seat {seat!r} is busy with another turn", file=sys.stderr)
        _deliver(
            send, outbox, sender,
            f"{seat}: busy with another turn. {NOT_SENT_NOTE}",
        )
        return 0
    except (StoreError, OSError) as exc:
        _deliver(send, outbox, sender, f"{seat}: {exc}\n\n{NOT_SENT_NOTE}")
        return 0


def _work_dir(seat, root):
    """A fresh scratch dir for files this turn has to build.

    Rebuilt per turn rather than accumulated: everything in it is either
    already in the conversation or already copied into the outbox by the time
    the turn ends, and one stale reply file read as this turn's is a wrong
    answer sent confidently.
    """
    path = os.path.join(root, f".{seat}.work")
    if os.path.islink(path) or (os.path.exists(path) and not os.path.isdir(path)):
        # Nothing of ours is a plain file here, so it is a stale leftover.
        print(f"a8s-gemini-web: replaced a stale file at {path}", file=sys.stderr)
        os.unlink(path)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    return path


def _as_body_and_files(reply, notes, work_dir):
    """The reply as a body plus attachments, sending a long one as a file.

    Cutting a long answer throws away the part the sender most likely wanted —
    the end of the program, the rest of the list. The cap keeps one answer from
    flooding a mailbox, and a file does not sit in the message text, so sending
    the whole thing as one costs the cap nothing.
    """
    tail = _notes_block(notes)
    if len(reply) + (len(tail) + 2 if tail else 0) <= MAX_BODY:
        # Decided on the combined size: an answer that fits alone but not with
        # its notes beside it would otherwise lose its end to the cap.
        return reply, []
    path = os.path.join(work_dir, LONG_REPLY_NAME)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(reply if reply.endswith("\n") else reply + "\n")
    except OSError as exc:
        notes.append(
            "this reply is too long for a message and could not be written to a "
            f"file ({exc}), so it is cut."
        )
        return reply, []
    notes.append(
        f"Gemini's answer is {len(reply)} characters, which with this seat's notes is "
        f"past its {MAX_BODY}-character reply cap, so the whole of it is attached as "
        f"{LONG_REPLY_NAME} rather than cut."
    )
    return reply[: MAX_BODY - LONG_REPLY_RESERVE].rstrip(), [path]


def _turn(seat, sender, message, runner, store, model, send, outbox):
    notes = []
    state = {}
    try:
        work_dir = _work_dir(seat, store.root)
        incoming = attachments.read(message, work_dir)
    except OSError as exc:
        # Before Gemini is asked, so the sender can safely send it again.
        _deliver(
            send, outbox, sender,
            f"{seat}: this seat cannot prepare its work folder ({exc}).\n\n{NOT_SENT_NOTE}",
        )
        return 0
    notes.extend(incoming.notes)

    try:
        turn = _round_trip(
            seat, sender, incoming.prose, runner, store, model, notes, state,
            paths=incoming.paths,
        )
    except (gemini.GeminiError, browser.BrowserError, OSError) as exc:
        notes.insert(0, store.warning)
        if state.get("uncertain"):
            notes.append(UNCERTAIN_NOTE)
        if not state.get("typed"):
            # Not yet typed in: the sender can send it again without asking
            # Gemini the same question twice.
            notes.append(NOT_SENT_NOTE)
        _deliver(send, outbox, sender, _compose(f"{seat}: {exc}", notes))
        return 0
    notes.insert(0, store.warning)

    if gemini.rate_limited(turn.reply):
        _deliver(
            send,
            outbox,
            sender,
            _capped(
                f"{seat}: Gemini is rate-limiting this seat, so your message was not "
                f"answered. Retry later.\n\nGemini said: {turn.reply.strip()}"
            ),
        )
        return 0

    notes.append(turn.note)
    hit = gemini.trouble(turn.reply)
    if hit:
        notes.append(f"Gemini's own trouble wording is in this reply ({hit!r}).")
    try:
        images, lost = _fetch_images(runner, turn.images, work_dir)
        notes.extend(lost)
        reply = turn.reply.strip() or _images_line(turn.images, images) or (
            f"{seat}: nothing came back from Gemini for this message."
        )
        body, files = _as_body_and_files(reply, notes, work_dir)
    except OSError as exc:
        # The answer exists, so it is sent as text with a note: asking again
        # would spend a second turn.
        print(f"a8s-gemini-web: could not save state after the answer ({exc})", file=sys.stderr)
        notes.append(f"this seat could not save state ({exc}); attachments are not included.")
        body, files, images = turn.reply.strip() or f"{seat}: nothing came back.", [], []
    _deliver(send, outbox, sender, _compose(body, notes), files + images)
    return 0


def _fetch_images(runner, count, work_dir):
    """Download a turn's generated images into this turn's own directory.

    Returns `(paths, notes)`, a note naming each image that did not come back.

    Each image is copied out of a8s-browser's artifacts **before the next one is
    asked for**, and the copy is checked against the bytes the download
    produced. An artifact path is the browser's to reuse: two quick downloads
    with one name can land on the same path, and a copy made afterwards would
    send the second picture twice under two names. A picture byte-identical to
    one already kept is a failure too, because it means the selector no longer
    tells this turn's images apart.
    """
    if not count:
        return [], []
    directory = os.path.join(work_dir, OUTBOUND_DIR)
    taken = {LONG_REPLY_NAME}
    seen = {}
    kept = []
    notes = []
    for index in range(1, count + 1):
        which = f"image {index} of {count}"
        source, failure = gemini.download_image(runner, index, count)
        if failure:
            notes.append(failure)
            continue
        try:
            fetched = attachments.digest(source)
        except OSError as exc:
            notes.append(f"{which} could not be downloaded: {exc}")
            continue
        if fetched in seen:
            notes.append(
                f"{which} could not be downloaded: the page handed back image "
                f"{seen[fetched]} again, so this seat cannot reach the others"
            )
            continue
        path, note = attachments.keep(source, directory, taken, f"image-{index}")
        if note:
            notes.append(note)
            continue
        if attachments.digest(path) != fetched:
            os.unlink(path)
            notes.append(f"{which} changed while it was being kept, so it is not attached.")
            continue
        seen[fetched] = index
        kept.append(path)
    return kept, notes


def _images_line(made, attached):
    """What an image-only answer says in words, since Gemini wrote none."""
    if not made:
        return ""
    noun = "image" if made == 1 else "images"
    if len(attached) == made:
        return f"Generated {made} {noun}."
    return f"Gemini generated {made} {noun}; {len(attached)} attached."
