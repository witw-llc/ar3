"""CLI and a8s wake handler for tool-timer."""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import _runtime as rt

from .store import (
    DEFAULT_MAX_DELAY,
    TimerStore,
    TimerStoreError,
    TimerUsageError,
    format_duration,
    format_timer_line,
    parse_request,
)
from .worker import ensure_worker, stop_worker

README = Path(__file__).resolve().parent / "README.md"


def _exit_code(sent: int, *, ok: bool) -> int:
    if not rt.print_mode():
        return 0
    if sent != 0 or not ok:
        return 1
    return 0


def _parse_max_delay(raw: str | None) -> float:
    if raw is None or not str(raw).strip():
        return DEFAULT_MAX_DELAY
    val = float(str(raw).strip())
    if not math.isfinite(val) or val <= 0:
        raise ValueError("MAX_DELAY must be a positive number")
    return val


def handle_wake(
    *,
    seat: str,
    sender: str,
    allow: str,
    message: str,
    max_delay_raw: str | None = None,
    workdir: Path | None = None,
    attachments: Sequence[str | Path] | None = None,
) -> int:
    """Process one a8s wake or `ar3 tool timer` message."""
    if not sender:
        print("tool-timer: senderless wake refused", file=sys.stderr)
        return 0
    if not rt.allowed(sender, allow or ""):
        print(f"tool-timer: {sender!r} is not in ALLOW — refused", file=sys.stderr)
        return 0

    try:
        body, verified_attachments = rt.split_verified_attachments(
            message or "", verified=attachments
        )
    except rt.AttachmentProvenanceError as exc:
        sent = rt.send_reply(sender, f"tool-timer: {exc}")
        return _exit_code(sent, ok=False)

    if rt.is_help(body):
        return rt.send_reply(
            sender,
            rt.help_reply("tool-timer", seat, README, "apps/tools/timer/README.md"),
        )

    try:
        max_delay = _parse_max_delay(max_delay_raw)
    except ValueError:
        sent = rt.send_reply(sender, f"tool-timer: invalid MAX_DELAY setting {max_delay_raw!r}")
        return _exit_code(sent, ok=False)

    try:
        req = parse_request(
            body,
            has_attachments=bool(verified_attachments),
            max_delay=max_delay,
        )
    except TimerUsageError as exc:
        sent = rt.send_reply(sender, str(exc))
        return _exit_code(sent, ok=False)

    store = TimerStore(workdir or Path.cwd())

    try:
        if req.kind == "list":
            entries = store.list_timers(sender=sender)
            if not entries:
                reply = "tool-timer: no pending timers"
            else:
                now = time.time()
                reply = "\n".join(format_timer_line(item, now=now) for item in entries)
            return _exit_code(rt.send_reply(sender, reply), ok=True)

        if req.kind == "cancel":
            removed = store.cancel(req.target, sender=sender)
            if req.target.strip().lower() == "all":
                reply = f"tool-timer: cancelled {len(removed)} timer(s)"
                return _exit_code(rt.send_reply(sender, reply), ok=True)
            if removed:
                reply = f"tool-timer: cancelled {removed[0]['id']}"
                return _exit_code(rt.send_reply(sender, reply), ok=True)
            reply = f"tool-timer: no pending timer {req.target!r}"
            return _exit_code(rt.send_reply(sender, reply), ok=False)

        outbox = store.record_outbox(os.environ.get("TELL_OUTBOX_DIR"))
        if rt.print_mode() and outbox is None:
            deadline = time.monotonic() + req.delay
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.2, remaining))
            sent = rt.send_reply(sender, req.body, tuple(verified_attachments))
            return _exit_code(sent, ok=True)

        if outbox is None:
            print("tool-timer: no outbox configured for delayed delivery", file=sys.stderr)
            sent = rt.send_reply(sender, "tool-timer: no outbox configured")
            return _exit_code(sent, ok=False)

        entry = store.schedule(sender, req.delay, req.body, verified_attachments)
        ensure_worker(store, seat, outbox_dir=outbox)
        print(
            f"tool-timer: scheduled {entry['id']} for {sender} in {format_duration(req.delay)}"
        )
        return 0
    except (TimerUsageError, TimerStoreError, OSError) as exc:
        sent = rt.send_reply(sender, f"tool-timer: {exc}")
        return _exit_code(sent, ok=False)


def _cmd_list(args: argparse.Namespace) -> int:
    store = TimerStore(Path.cwd())
    try:
        entries = store.list_timers(sender=args.sender)
    except TimerStoreError as exc:
        print(f"{args.seat}: {exc}", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps({"timers": entries}, indent=2, ensure_ascii=True))
        return 0
    if not entries:
        print(f"{args.seat}: no pending timers")
        return 0
    now = time.time()
    for item in entries:
        print(format_timer_line(item, now=now, include_sender=(args.sender is None)))
    return 0


def _cmd_cancel(args: argparse.Namespace) -> int:
    store = TimerStore(Path.cwd())
    try:
        removed = store.cancel(args.target, sender=args.sender)
    except TimerStoreError as exc:
        print(f"{args.seat}: {exc}", file=sys.stderr)
        return 1
    if args.target.strip().lower() == "all":
        print(f"{args.seat}: cancelled {len(removed)} timer(s)")
        return 0
    if not removed:
        print(f"{args.seat}: no pending timer {args.target!r}", file=sys.stderr)
        return 1
    print(f"{args.seat}: cancelled {removed[0]['id']}")
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    store = TimerStore(Path.cwd())
    stopped = stop_worker(store)
    print(f"{args.seat}: {'stopped worker' if stopped else 'no worker running'}")
    return 0


def _cmd_idle(args: argparse.Namespace) -> int:
    store = TimerStore(Path.cwd())
    try:
        if not store.has_pending():
            return 0
    except TimerStoreError as exc:
        print(f"{args.seat}: {exc}", file=sys.stderr)
        return 1
    outbox = store.record_outbox(os.environ.get("TELL_OUTBOX_DIR"))
    if outbox is None:
        return 0
    ensure_worker(store, args.seat, outbox_dir=outbox)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=os.environ.get("AR3_TOOL_PROG", "timer"),
        description="Schedule delayed callback messages back to their senders.",
    )
    sub = parser.add_subparsers(dest="command")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--seat",
        default="timer",
        help=argparse.SUPPRESS
        if os.environ.get("AR3_TOOL_PROG")
        else "a8s node name (default: timer)",
    )

    p_handle = sub.add_parser("handle", parents=[common], help="a8s wake entry point")
    p_handle.add_argument("--from", dest="sender", default="")
    p_handle.add_argument("--allow", default="")
    p_handle.add_argument("--max-delay", dest="max_delay", default=None)
    p_handle.add_argument("message", nargs="?", default="")

    p_list = sub.add_parser("list", parents=[common], help="list pending timers in this directory")
    p_list.add_argument("--from", dest="sender", default=None, help="filter by sender name")
    p_list.add_argument("--json", dest="as_json", action="store_true", help="emit pending timers as JSON")

    p_cancel = sub.add_parser("cancel", parents=[common], help="cancel one timer or all pending timers")
    p_cancel.add_argument("target", metavar="ID_OR_ALL", help="timer ID or 'all'")
    p_cancel.add_argument("--from", dest="sender", default=None, help="restrict cancellation to one sender")

    sub.add_parser("stop", parents=[common], help="stop the background worker process")
    sub.add_parser("idle", parents=[common], help="start the worker if pending timers exist")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    if args.command == "handle":
        return handle_wake(
            seat=args.seat,
            sender=args.sender,
            allow=args.allow,
            message=args.message,
            max_delay_raw=args.max_delay,
        )
    if args.command == "list":
        return _cmd_list(args)
    if args.command == "cancel":
        return _cmd_cancel(args)
    if args.command == "stop":
        return _cmd_stop(args)
    if args.command == "idle":
        return _cmd_idle(args)
    return 2
