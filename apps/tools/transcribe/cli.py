"""CLI for tool-transcribe: serve a8s wakes and manage hints/replacements."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .engine import doctor_report
from .handler import handle_wake
from .store import (
    TranscribeConfigError,
    add_hints,
    clear_hints,
    clear_replacements,
    hints_path,
    parse_replacement_spec,
    read_hints,
    read_replacements,
    remove_hint,
    remove_replacement,
    replacements_path,
    set_replacement,
)


def _cmd_hint(args: argparse.Namespace) -> int:
    workdir = Path.cwd().resolve()
    try:
        if args.hint_command == "list":
            items = read_hints(workdir)
            if not items:
                print(f"{args.seat}: no hints ({hints_path(workdir)})")
                return 0
            for item in items:
                print(item)
            return 0
        if args.hint_command == "add":
            added, skipped = add_hints(workdir, args.terms)
            suffix = f" ({skipped} already present)" if skipped else ""
            print(f"{args.seat}: added {added} hint(s) to {hints_path(workdir)}{suffix}")
            return 0
        if args.hint_command == "rm":
            removed = remove_hint(workdir, args.term)
            if not removed:
                print(f"{args.seat}: hint {args.term!r} not found", file=sys.stderr)
                return 1
            print(f"{args.seat}: removed hint {args.term!r}")
            return 0
        if args.hint_command == "clear":
            count = clear_hints(workdir)
            print(f"{args.seat}: cleared {count} hint(s)")
            return 0
    except (TranscribeConfigError, OSError) as exc:
        print(f"{args.seat}: {exc}", file=sys.stderr)
        return 1
    return 2


def _cmd_replace(args: argparse.Namespace) -> int:
    workdir = Path.cwd().resolve()
    try:
        if args.replace_command == "list":
            pairs = read_replacements(workdir)
            if not pairs:
                print(f"{args.seat}: no replacements ({replacements_path(workdir)})")
                return 0
            for pat, corr in pairs:
                print(f"{pat} => {corr}")
            return 0
        if args.replace_command == "set":
            if args.correction is None:
                pat, corr = parse_replacement_spec(args.pattern)
            else:
                pat, corr = args.pattern, args.correction
            outcome = set_replacement(workdir, pat, corr)
            print(
                f"{args.seat}: {outcome} replacement {pat} => {corr} in {replacements_path(workdir)}"
            )
            return 0
        if args.replace_command == "rm":
            removed = remove_replacement(workdir, args.pattern)
            if not removed:
                print(
                    f"{args.seat}: replacement {args.pattern!r} not found",
                    file=sys.stderr,
                )
                return 1
            print(f"{args.seat}: removed replacement {args.pattern!r}")
            return 0
        if args.replace_command == "clear":
            count = clear_replacements(workdir)
            print(f"{args.seat}: cleared {count} replacement(s)")
            return 0
    except (TranscribeConfigError, OSError) as exc:
        print(f"{args.seat}: {exc}", file=sys.stderr)
        return 1
    return 2


def main(argv: list[str] | None = None) -> int:
    in_ar3_tool = bool(os.environ.get("AR3_TOOL_PROG"))
    parser = argparse.ArgumentParser(
        prog=os.environ.get("AR3_TOOL_PROG", "transcribe"),
        description="Transcribe attached audio and video files with local models.",
    )
    parser.add_argument(
        "--seat",
        default="transcribe",
        help=argparse.SUPPRESS if in_ar3_tool else "a8s node name (default: transcribe)",
    )
    sub = parser.add_subparsers(dest="command")

    seat_only = argparse.ArgumentParser(add_help=False)
    seat_only.add_argument(
        "--seat",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS if in_ar3_tool else "a8s node name (default: transcribe)",
    )

    p_handle = sub.add_parser("handle", parents=[seat_only], help="a8s wake entry point")
    p_handle.add_argument("--from", dest="sender", default="")
    p_handle.add_argument("--allow", default="")
    p_handle.add_argument("--engine", default=None)
    p_handle.add_argument("--model", default=None)
    p_handle.add_argument("--language", default=None)
    p_handle.add_argument("--flavor", default=None)
    p_handle.add_argument("--vision-model", dest="vision_model", default=None)
    p_handle.add_argument("--ollama-url", dest="ollama_url", default=None)
    p_handle.add_argument("--hints", default=None)
    p_handle.add_argument("--timeout", default=None)
    p_handle.add_argument("--inline-max", dest="inline_max", default=None)
    p_handle.add_argument("message", nargs="?", default="")

    p_doc = sub.add_parser(
        "doctor",
        parents=[seat_only],
        help="check local binaries, engines, and working-directory files",
    )
    p_doc.add_argument("--engine", default=None, help="engine to check (default: auto)")
    p_doc.add_argument("--vision-model", dest="vision_model", default=None, help="vision model name")
    p_doc.add_argument("--ollama-url", dest="ollama_url", default=None, help="Ollama base URL")

    p_hint = sub.add_parser("hint", parents=[seat_only], help="manage vocabulary hints in hints.txt")
    hint_sub = p_hint.add_subparsers(dest="hint_command", required=True)
    hint_sub.add_parser("list", parents=[seat_only], help="list hints in hints.txt")
    p_hint_add = hint_sub.add_parser("add", parents=[seat_only], help="add one or more hints")
    p_hint_add.add_argument("terms", nargs="+", metavar="HINT", help="hint phrase or comma-separated list")
    p_hint_rm = hint_sub.add_parser("rm", parents=[seat_only], help="remove a hint")
    p_hint_rm.add_argument("term", metavar="HINT", help="hint phrase to remove")
    hint_sub.add_parser("clear", parents=[seat_only], help="remove all hints")

    p_rep = sub.add_parser("replace", parents=[seat_only], help="manage regex replacements in replacements.txt")
    rep_sub = p_rep.add_subparsers(dest="replace_command", required=True)
    rep_sub.add_parser("list", parents=[seat_only], help="list rules in replacements.txt")
    p_rep_set = rep_sub.add_parser("set", parents=[seat_only], help="add or update a replacement rule")
    p_rep_set.add_argument("pattern", metavar="PATTERN", help="Python regex (or 'pattern => correction')")
    p_rep_set.add_argument("correction", nargs="?", default=None, metavar="CORRECTION", help="likely replacement text")
    p_rep_rm = rep_sub.add_parser("rm", parents=[seat_only], help="remove a replacement rule")
    p_rep_rm.add_argument("pattern", metavar="PATTERN", help="exact pattern string to remove")
    rep_sub.add_parser("clear", parents=[seat_only], help="remove all replacement rules")

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
            default_engine=args.engine,
            default_model=args.model,
            default_language=args.language,
            default_flavor=args.flavor,
            default_vision_model=args.vision_model,
            ollama_url=args.ollama_url,
            default_hints=args.hints,
            timeout_raw=args.timeout,
            inline_max_raw=args.inline_max,
        )
    if args.command == "doctor":
        return doctor_report(
            Path.cwd(),
            engine=args.engine,
            vision_model=args.vision_model,
            ollama_url=args.ollama_url,
        )
    if args.command == "hint":
        return _cmd_hint(args)
    if args.command == "replace":
        return _cmd_replace(args)
    return 2
