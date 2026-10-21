"""a8s wake entry point: a tell in, a transcript and its artifacts back."""
import os
import sys

import _runtime as rt

from . import commands

MAX_BODY = 4000


def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes")


def handle(seat, sender, message, allow="", allow_eval=None):
    """`allow` is the node's required ALLOW var; a sender outside it gets no
    reply (the refusal is logged). `allow_eval` is the ALLOW_EVAL var; unset,
    it falls back to AR3_BROWSER_ALLOW_EVAL so a hand-run `handle` behaves the
    same. Every exit is 0: a8s dead-letters a wake on any other code, with no
    retry, so a failed send is logged and the message is not lost twice."""
    if not rt.allowed(sender, allow or ""):
        print(f"browser: {sender!r} is not in ALLOW — refused", file=sys.stderr)
        return 0

    eval_flag = (
        allow_eval
        if allow_eval is not None
        else os.environ.get("AR3_BROWSER_ALLOW_EVAL", "")
    )
    script, _attachments = rt.split_attachments(message)
    run = commands.run_script(seat, script, allow_eval=_truthy(eval_flag))

    verdict = "ok" if run.ok else f"failed: {run.error}"
    body = f"{seat}: {verdict}\n\n{run.transcript()}"
    return rt.send_reply(sender, body[:MAX_BODY], tuple(run.files))
