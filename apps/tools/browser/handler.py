"""a8s wake entry point: a tell in, a transcript and its artifacts back."""
import os
import sys
from pathlib import Path

import _runtime as rt
from ar3 import nodes

from . import commands
from . import recipe
from . import seatlock
from . import session

README = Path(__file__).resolve().parent / "README.md"
MAX_BODY = 4000


def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes")


def node_recipe_dirs(seat):
    """The directories, other than the bundled one, that site recipes load from for this seat: `recipes/`
    in the root of the registered node that `seat` names, when that node runs the tool-browser definition.
    A seat that names no such node has none. The working directory and the environment are not asked, so
    a run cannot take a directory for a node's by standing in it."""
    root = nodes.node_root(seat, recipe.NODE_DEFINITION)
    return [os.path.join(root, recipe.NODE_DIRNAME)] if root else []


def handle(seat, sender, message, allow="", allow_eval=None):
    """`allow` is the node's required ALLOW var; a sender outside it gets no
    reply (the refusal is logged). `allow_eval` is the ALLOW_EVAL var; unset,
    it falls back to AR3_BROWSER_ALLOW_EVAL so a hand-run `handle` behaves the
    same. The site recipes of the registered node called `seat` load beside
    the bundled ones (see `node_recipe_dirs`).

    A wake exits 0 whatever the script did: a8s dead-letters a wake on any
    other code, with no retry, so a failed send is logged and the message is
    not lost twice. `ar3 tool` prints the reply and exits 1 when the script
    failed or the seat was busy, so a shell script can test it."""
    if not rt.allowed(sender, allow or ""):
        print(f"browser: {sender!r} is not in ALLOW — refused", file=sys.stderr)
        return 0

    if rt.is_help(message):
        body = rt.help_reply("tool-browser", seat, README, "apps/tools/browser/README.md")
        return rt.send_reply(sender, body)

    eval_flag = (
        allow_eval
        if allow_eval is not None
        else os.environ.get("AR3_BROWSER_ALLOW_EVAL", "")
    )
    script, _attachments = rt.split_attachments(message)
    # Read the stop epoch before any wait for the seat: a request that began
    # before a stop must not open Chrome after it.
    epoch = session.stop_epoch(seat)
    kind = "ar3 tool" if rt.print_mode() else "a8s wake"
    try:
        with seatlock.hold(
            seat, kind, sender=sender, verb=script.strip().split("\n", 1)[0][:40],
            on_wait=lambda who: print(f"{seat}: waiting: seat held by {who}.", file=sys.stderr),
        ):
            run = commands.run_script(
                seat, script, allow_eval=_truthy(eval_flag), epoch=epoch,
                recipes=recipe.Library(dirs=node_recipe_dirs(seat)),
            )
    except seatlock.SeatBusy as busy:
        return _exit_code(rt.send_reply(sender, f"{seat}: busy: {busy}. Nothing ran."), ran=False)

    verdict = "ok" if run.ok else f"failed: {run.error}"
    body = run.clean(f"{seat}: {verdict}\n\n{run.transcript()}")
    if not rt.print_mode():
        body = body[:MAX_BODY]
    return _exit_code(rt.send_reply(sender, body, tuple(run.files)), ran=run.ok)


def _exit_code(sent, ran):
    """The reply's own exit code, or 1 for a printed reply whose script did not run to the end."""
    if sent == 0 and rt.print_mode() and not ran:
        return 1
    return sent
