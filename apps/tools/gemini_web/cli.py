"""CLI for the gemini-web tool: serve a8s wakes, or drive one turn by hand."""
import argparse
import sys

from . import handler
from .store import SessionStore


def _print_reply(recipient, body, files=()):
    print(f"--- reply to {recipient} ---")
    print(body)
    for path in files or ():
        print(f"--- attached: {path}")
    return 0


def _sessions(seat):
    store = SessionStore(seat)
    sessions = store.all()
    if store.warning:
        print(store.warning, file=sys.stderr)
    if not sessions:
        print(f"{seat}: no conversations yet ({store.path})")
        return 0
    for name in sorted(sessions):
        entry = sessions[name]
        print(f"{name}\t{entry.get('url', '')}\t{entry.get('created', '')}")
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="gemini_web",
        description="Gemini Web as an a8s seat: a tell in, Gemini's answer back.",
    )
    sub = parser.add_subparsers(dest="command")
    # The same options after the verb, as a8s's definition passes them there.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--seat", default="gemini", help="a8s node name (default: gemini)")

    def node_options(p, allow_required):
        p.add_argument("--browser-seat", required=True,
                       help="browser seat holding the signed-in profile")
        p.add_argument("--allow", required=allow_required,
                       help="comma-separated senders this seat answers")
        p.add_argument("--model", help="model to pick when a conversation is created")

    parser_handle = sub.add_parser("handle", parents=[common], help="a8s wake entry point")
    parser_handle.add_argument("--from", dest="sender", required=True)
    node_options(parser_handle, True)
    parser_handle.add_argument("message", nargs="?", default="")

    parser_ask = sub.add_parser(
        "ask", parents=[common],
        help="run one turn by hand and print the reply instead of telling it",
    )
    parser_ask.add_argument("--from", dest="sender", default="operator")
    node_options(parser_ask, False)
    parser_ask.add_argument("message")

    sub.add_parser("sessions", parents=[common], help="list this seat's conversations")

    parser_forget = sub.add_parser(
        "forget", parents=[common],
        help="drop a correspondent's conversation; the next tell starts a new one",
    )
    parser_forget.add_argument("sender")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 0

    seat = args.seat
    if args.command == "sessions":
        return _sessions(seat)
    if args.command == "forget":
        dropped = SessionStore(seat).forget(args.sender)
        print(f"{seat}: {'forgot' if dropped else 'had no conversation for'} {args.sender}")
        return 0

    handler.handle(
        seat,
        args.sender,
        args.message,
        browser_seat=args.browser_seat,
        allow=args.allow or args.sender,
        model=args.model,
        send=None if args.command == "handle" else _print_reply,
        # `deliver` is deliberately left alone: a hand-run `ask` prints its own
        # reply, and still hands held replies to their correspondents.
    )
    return 0
