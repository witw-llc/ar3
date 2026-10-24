"""CLI for the browser tool: drive a seat's browser by hand, or serve a8s wakes."""
import argparse
import contextlib
import json
import os
import sys
import time

from . import commands
from . import doctor
from . import handler
from . import plc
from . import seatlock
from . import session


def _seat(args):
    return args.seat or os.environ.get("AR3_BROWSER_SEAT") or "browser"


def _allow_eval():
    return os.environ.get("AR3_BROWSER_ALLOW_EVAL", "").lower() in ("1", "true", "yes")


def _waiting(seat):
    def say(who):
        print(f"{seat}: waiting: seat held by {who}.", file=sys.stderr)

    return say


def _hold(seat, kind, verb):
    return seatlock.hold(seat, kind, verb=verb, on_wait=_waiting(seat))


def _busy(seat, busy, as_json):
    if as_json:
        step = {"command": "", "ok": False, "error": f"busy: {busy}"}
        print(json.dumps({"ok": False, "steps": [step], "files": []}))
    else:
        print(f"{seat}: busy: {busy}", file=sys.stderr)
    return 1


def _do(seat, args, epoch):
    if args.script == "-":
        body = sys.stdin.read()
    else:
        with open(args.script) as handle:
            body = handle.read()

    try:
        with _hold(seat, "browser do", "do"):
            run = commands.run_script(seat, body, allow_eval=_allow_eval(), epoch=epoch)
    except seatlock.SeatBusy as busy:
        return _busy(seat, busy, args.json)
    if args.json:
        print(json.dumps({"ok": run.ok, "steps": run.steps, "files": run.files}))
    else:
        print(run.transcript())
        for path in run.files:
            print(f"artifact: {path}")
    return 0 if run.ok else 1


def main():
    parser = argparse.ArgumentParser(
        prog="browser",
        description="A persistent browser session addressable as an a8s seat.",
    )
    parser.add_argument("-s", "--seat", help="seat name (default: $AR3_BROWSER_SEAT or 'browser')")
    parser.add_argument("--chrome", help="Chrome binary (default: found on PATH)")
    parser.add_argument("--port", help="CDP port (default: Chrome picks a free port and records it in the seat profile)")
    sub = parser.add_subparsers(dest="command")
    # The same options after the verb, as a8s's definition passes them there.
    # SUPPRESS keeps a subparser from overwriting a value given before the verb.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--seat", default=argparse.SUPPRESS)
    common.add_argument("--chrome", default=argparse.SUPPRESS)
    common.add_argument("--port", default=argparse.SUPPRESS)

    sub.add_parser("open", parents=[common], help="launch Chrome on this seat's profile and attach")
    sub.add_parser("close", parents=[common], help="save state and quit Chrome")
    sub.add_parser("status", parents=[common], help="report whether the session is attached, and where it is")
    sub.add_parser("snap", parents=[common], help="print the page accessibility snapshot")
    sub.add_parser("console", parents=[common], help="print the page console log")
    sub.add_parser("doctor", parents=[common], help="check that this box has what a seat needs")

    parser_go = sub.add_parser("go", parents=[common], help="navigate to a URL")
    parser_go.add_argument("url")

    parser_do = sub.add_parser("do", parents=[common], help="run a command script (file, or '-' for stdin)")
    parser_do.add_argument("script")
    parser_do.add_argument("--json", action="store_true", help="emit the run as JSON")

    parser_handle = sub.add_parser("handle", parents=[common], help="a8s wake entry point")
    parser_handle.add_argument("--from", dest="sender", required=True)
    parser_handle.add_argument("message", nargs="?", default="")
    parser_handle.add_argument(
        "--allow",
        required=True,
        help="comma-separated senders this seat answers; a8s passes it as the node's ALLOW var",
    )
    parser_handle.add_argument(
        "--allow-eval",
        nargs="?",
        const="1",
        help="permit `eval` and `run-code` on this seat; a8s passes it as the node's ALLOW_EVAL var",
    )

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 0

    if args.command == "doctor":
        return doctor.report()

    if args.chrome:
        os.environ["AR3_BROWSER_CHROME"] = args.chrome
    if args.port:
        os.environ["AR3_BROWSER_PORT"] = args.port
    seat = _seat(args)
    # A relative script path must resolve before the seat's scratch dir becomes
    # the cwd; everything playwright-cli drops then lands under the seat.
    if args.command == "do" and args.script != "-":
        args.script = os.path.abspath(args.script)
    os.chdir(session.scratch_dir(seat))
    # Read the stop epoch now, before any wait for the seat: an operation that
    # began before a stop must not open Chrome after it.
    epoch = session.stop_epoch(seat)
    if args.command == "handle":
        return handler.handle(
            seat, args.sender, args.message,
            allow=args.allow, allow_eval=args.allow_eval,
        )
    if args.command == "do":
        return _do(seat, args, epoch)

    if args.command == "close":
        return _close(seat)
    if args.command == "status":
        print(session.status(seat))
        return 0
    try:
        with _hold(seat, "browser cli", args.command):
            return _drive(seat, args, epoch)
    except seatlock.SeatBusy as busy:
        return _busy(seat, busy, False)


LAUNCH_LOCK_WAIT_SECONDS = 3.0
LAUNCH_POLL_SECONDS = 0.1


def _close(seat):
    """The stop hook. It never waits for the seat: the epoch it writes ends the
    holder's script. It takes the launch lock for the epoch and the kill, but
    only for a short poll, because a8s kills the hook after 30 s."""
    try:
        with seatlock.hold(seat, "a8s stop", verb="close", wait=0):
            return _closed_under_launch_lock(seat)
    except seatlock.SeatBusy as busy:
        print(f"{seat}: closing Chrome. The seat is held by {busy.who}.", file=sys.stderr)
        return _closed_under_launch_lock(seat)


def _closed_under_launch_lock(seat):
    deadline = time.monotonic() + LAUNCH_LOCK_WAIT_SECONDS
    with contextlib.ExitStack() as stack:
        while True:
            try:
                stack.enter_context(session.launch_lock(seat, blocking=False))
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    print(f"{seat}: launch lock held past 3 s; closing anyway.", file=sys.stderr)
                    break
                time.sleep(LAUNCH_POLL_SECONDS)
        return _closed(seat)


def _closed(seat):
    # The epoch goes first: a script that holds the seat must see the stop
    # before it can see the closed Chrome.
    session.bump_stop_epoch(seat)
    try:
        session.close_browser(seat)
    except plc.BrowserError as exc:
        print(f"{seat}: {exc}", file=sys.stderr)
        return 1
    print(f"{seat}: closed")
    return 0


def _drive(seat, args, epoch):
    try:
        if args.command == "open":
            session.open_if_down(seat, epoch)
            print(session.status(seat))
        elif args.command == "snap":
            session.ensure_running(seat, script_epoch=epoch)
            print(session.snapshot(seat))
        elif args.command == "console":
            session.ensure_running(seat, script_epoch=epoch)
            print(plc.result_or(plc.run(seat, "console")))
        elif args.command == "go":
            session.ensure_running(seat, script_epoch=epoch)
            plc.run(seat, "goto", args.url, timeout=60)
            print(session.current_url(seat))
    except plc.BrowserError as exc:
        print(f"{seat}: {exc}", file=sys.stderr)
        return 1
    return 0
