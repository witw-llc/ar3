"""CLI for the browser tool: drive a seat's browser by hand, or serve a8s wakes."""
import argparse
import contextlib
import getpass
import json
import math
import os
import sys
import time

from . import commands
from . import doctor
from . import handler
from . import plc
from . import recipe
from . import seatlock
from . import secretstore
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
            run = commands.run_script(
                seat, body, allow_eval=_allow_eval(), epoch=epoch,
                recipes=recipe.Library(dirs=args.recipes),
            )
    except seatlock.SeatBusy as busy:
        return _busy(seat, busy, args.json)
    if args.json:
        print(json.dumps({"ok": run.ok, "steps": run.steps, "files": run.files}))
    else:
        print(run.transcript())
        for path in run.files:
            print(f"artifact: {path}")
    return 0 if run.ok else 1


def _positive_seconds(text):
    try:
        value = float(text)
    except ValueError:
        value = 0.0
    if not (value > 0 and math.isfinite(value)):
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of seconds above 0")
    return value


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
    parser_idle = sub.add_parser(
        "idle", parents=[common],
        help="quit Chrome when no script has used the seat for --timeout seconds",
    )
    parser_idle.add_argument(
        "--timeout", required=True, type=_positive_seconds,
        help="seconds without a script before Chrome quits; a8s passes it as the node's TIMEOUT var",
    )
    sub.add_parser("status", parents=[common], help="report whether the session is attached, and where it is")
    sub.add_parser("snap", parents=[common], help="print the page accessibility snapshot")
    sub.add_parser("console", parents=[common], help="print the page console log")
    sub.add_parser("doctor", parents=[common], help="check that this box has what a seat needs")

    parser_secret = sub.add_parser(
        "secret", help="keep the secrets this seat's scripts may type into pages"
    )
    secret_sub = parser_secret.add_subparsers(dest="secret_command", required=True)
    seat_only = argparse.ArgumentParser(add_help=False)
    seat_only.add_argument("--seat", default=argparse.SUPPRESS)
    parser_set = secret_sub.add_parser(
        "set", parents=[seat_only], help="store a secret; the value comes from stdin or a hidden prompt"
    )
    parser_set.add_argument("name", metavar="NAME")
    parser_set.add_argument(
        "--origin", action="append", required=True, dest="origins", metavar="ORIGIN",
        help="an origin the secret may be typed on, such as https://app.example.com; repeat for more",
    )
    secret_sub.add_parser("list", parents=[seat_only], help="print each secret name with its origins")
    parser_rm = secret_sub.add_parser("rm", parents=[seat_only], help="remove a secret")
    parser_rm.add_argument("name", metavar="NAME")

    parser_go = sub.add_parser("go", parents=[common], help="navigate to a URL")
    parser_go.add_argument("url")

    parser_do = sub.add_parser("do", parents=[common], help="run a command script (file, or '-' for stdin)")
    parser_do.add_argument("script")
    parser_do.add_argument("--json", action="store_true", help="emit the run as JSON")
    parser_do.add_argument(
        "--recipes", action="append", default=[], metavar="DIR",
        help="also load site recipes from DIR (repeatable); a recipe there replaces a bundled one of its name",
    )

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
    if args.command == "secret":
        return _secret(seat, args)
    if args.command == "idle":
        return _idle(seat, args.timeout)
    # A relative script path must resolve before the seat's scratch dir becomes
    # the cwd; everything playwright-cli drops then lands under the seat.
    if args.command == "do" and args.script != "-":
        args.script = os.path.abspath(args.script)
    if args.command == "do":
        args.recipes = [os.path.abspath(directory) for directory in args.recipes]
        for directory in args.recipes:
            if not os.path.isdir(directory):
                print(f"{seat}: --recipes {directory} is not a directory", file=sys.stderr)
                return 2
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
        return _status(seat)
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
        with seatlock.hold(seat, "a8s stop", verb="close", wait=0, activity=False):
            return _closed_under_launch_lock(seat)
    except seatlock.SeatBusy as busy:
        print(f"{seat}: closing Chrome. The seat is held by {busy.who}.", file=sys.stderr)
        return _closed_under_launch_lock(seat)


def _take_launch_lock(seat, stack):
    """Take the launch lock onto `stack`, polling for a short while. True when
    held; False when a launch or a stop kept it past the wait."""
    deadline = time.monotonic() + LAUNCH_LOCK_WAIT_SECONDS
    while True:
        try:
            stack.enter_context(session.launch_lock(seat, blocking=False))
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(LAUNCH_POLL_SECONDS)


def _closed_under_launch_lock(seat):
    with contextlib.ExitStack() as stack:
        if not _take_launch_lock(seat, stack):
            print(f"{seat}: launch lock held past 3 s; closing anyway.", file=sys.stderr)
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


def _idle(seat, timeout):
    """The idle hook. It closes Chrome when no script has used the seat for
    `timeout` seconds, and in every other case leaves Chrome as it is. It
    never starts one, never waits for the seat, and never waits for a launch:
    a seat in use or a launch in progress is not idle. It does not bump the
    stop epoch, because an idle close is not a stop: a script that already
    read the epoch may still open Chrome afterwards."""
    if not session.chrome_running(seat):
        return 0
    try:
        with seatlock.hold(seat, "idle close", verb="idle", wait=0, activity=False):
            return _idle_under_seat(seat, timeout)
    except seatlock.SeatBusy:
        print(f"{seat}: busy, not idle")
        return 0


def _idle_under_seat(seat, timeout):
    stamp = session.last_activity(seat)
    age = None if stamp is None else max(0.0, time.time() - stamp)
    # A missing stamp counts as idle: a8s ran this hook after `timeout` seconds
    # of quiet on the node.
    if age is not None and age < timeout:
        print(f"{seat}: used {int(age)}s ago, not idle")
        return 0
    os.chdir(session.scratch_dir(seat))
    with contextlib.ExitStack() as stack:
        if not _take_launch_lock(seat, stack):
            print(f"{seat}: a launch is in progress, not idle")
            return 0
        try:
            session.close_browser(seat)
        except plc.BrowserError as exc:
            print(f"{seat}: {exc}", file=sys.stderr)
            return 1
    if age is None:
        print(f"{seat}: closed, idle with no activity record")
    else:
        print(f"{seat}: closed after {int(age)}s idle")
    return 0


def _scrubber(seat):
    """The seat's scrubber, or None after the reason has been printed. Text from a
    page prints only through it, so a store that cannot be read stops the verb."""
    try:
        return secretstore.Scrubber(secretstore.read(seat))
    except secretstore.SecretStoreError as exc:
        print(f"{seat}: {exc}", file=sys.stderr)
        return None


def _status(seat):
    scrubber = _scrubber(seat)
    if scrubber is None:
        return 1
    print(scrubber.scrub(session.status(seat)))
    return 0


def _drive(seat, args, epoch):
    scrubber = _scrubber(seat)
    if scrubber is None:
        return 1
    try:
        with plc.protecting(seat, scrubber), commands.scrubbed_outputs(seat, scrubber):
            commands.sweep_outputs(seat, scrubber)
            if args.command == "open":
                session.open_if_down(seat, epoch)
                print(scrubber.scrub(session.status(seat)))
            elif args.command == "snap":
                session.ensure_running(seat, script_epoch=epoch)
                commands.refuse_a_local_page(seat, None, "snap")
                print(scrubber.scrub(session.snapshot(seat)))
            elif args.command == "console":
                session.ensure_running(seat, script_epoch=epoch)
                commands.refuse_a_local_page(seat, None, "console")
                print(scrubber.scrub(plc.result_or(plc.run(seat, "console"))))
            elif args.command == "go":
                commands.refuse_local_url(args.url, "go")
                session.ensure_running(seat, script_epoch=epoch)
                plc.run(seat, "goto", args.url, timeout=60)
                commands.refuse_a_local_page(seat, None, "go")
                print(scrubber.scrub(session.current_url(seat)))
    except plc.BrowserError as exc:
        print(f"{seat}: {scrubber.scrub(str(exc))}", file=sys.stderr)
        return 1
    return 0


def _secret_value(name):
    """The value from stdin when stdin is not a terminal (one trailing newline
    stripped), else from a prompt that does not echo."""
    if sys.stdin.isatty():
        return getpass.getpass(f"Value for {name}: ")
    text = sys.stdin.read()
    return text[:-1] if text.endswith("\n") else text


def _secret(seat, args):
    """The operator's verbs on the seat's secret store. They start no Chrome and
    take no seat lock, and no path through them prints a value."""
    try:
        if args.secret_command == "list":
            for name, entry in sorted(secretstore.read(seat).items()):
                print(name, *entry["origins"])
        elif args.secret_command == "rm":
            if not secretstore.remove(seat, args.name):
                print(f"{seat}: secret {args.name} is not set", file=sys.stderr)
                return 1
            print(f"{seat}: removed {args.name}")
        else:
            secretstore.check_name(args.name)
            origins = secretstore.check_origins(args.origins)
            secretstore.set_secret(seat, args.name, _secret_value(args.name), origins)
            print(f"{seat}: set {args.name}")
    except secretstore.SecretStoreError as exc:
        print(f"{seat}: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"{seat}: cannot write the secret store {secretstore.path(seat)}: {exc.strerror}", file=sys.stderr)
        return 1
    return 0
