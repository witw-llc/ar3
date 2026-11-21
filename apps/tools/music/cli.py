"""CLI for tool-music: serve a8s wakes, check readiness, prefetch weights, and manage `music.json`."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .engine import DEFAULT_TIMEOUT, MusicError, doctor_report, run_ace
from .handler import handle_wake
from .pins import MODEL_NAMES
from .store import CONFIG_KEYS, MusicConfigError, clear_config, config_path, read_config, update_config, validate_model


def _cmd_config(args: argparse.Namespace) -> int:
    workdir = Path.cwd().resolve()
    sub = args.config_command or "show"
    try:
        if sub == "show":
            cfg = read_config(workdir)
            if not cfg:
                print(f"{args.seat}: default configuration (no {config_path(workdir).name})")
            else:
                print(json.dumps(cfg, indent=2, sort_keys=True))
            return 0
        if sub == "clear":
            existed = clear_config(workdir)
            print(f"{args.seat}: {'cleared' if existed else 'already empty'} ({config_path(workdir)})")
            return 0
        if sub == "unset":
            updated = update_config(workdir, {}, args.keys)
            print(f"{args.seat}: updated {config_path(workdir)} ({len(updated)} setting(s) remaining)")
            return 0
        values = {key: getattr(args, f"cfg_{key}") for key in CONFIG_KEYS}
        if all(value is None for value in values.values()):
            print(f"{args.seat}: pass at least one of " + ", ".join(f"--{k}" for k in CONFIG_KEYS), file=sys.stderr)
            return 2
        updated = update_config(workdir, values)
        print(f"{args.seat}: saved {config_path(workdir)}: {json.dumps(updated, sort_keys=True)}")
        return 0
    except (MusicConfigError, OSError) as exc:
        print(f"{args.seat}: {exc}", file=sys.stderr)
        return 1


def _cmd_prepare(args: argparse.Namespace) -> int:
    try:
        model = validate_model(args.model or read_config(Path.cwd()).get("model") or "turbo")
        meta = run_ace({"model": model}, None, timeout=args.timeout, prepare_only=True)
    except (MusicConfigError, MusicError) as exc:
        print(f"{args.seat}: {exc}", file=sys.stderr)
        return 1
    print(f"{args.seat}: {model} ready in {meta['checkpoints']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    in_ar3_tool = bool(os.environ.get("AR3_TOOL_PROG"))
    seat_help = argparse.SUPPRESS if in_ar3_tool else "a8s node name (default: music)"
    parser = argparse.ArgumentParser(
        prog=os.environ.get("AR3_TOOL_PROG", "music"),
        description="Generate instrumental music from a text description with local models.",
    )
    parser.add_argument("--seat", default="music", help=seat_help)
    sub = parser.add_subparsers(dest="command")
    seat_only = argparse.ArgumentParser(add_help=False)
    seat_only.add_argument("--seat", default=argparse.SUPPRESS, help=seat_help)

    p_handle = sub.add_parser("handle", parents=[seat_only], help="a8s wake entry point")
    p_handle.add_argument("--from", dest="sender", default="")
    p_handle.add_argument("--allow", default="")
    for key in CONFIG_KEYS:
        p_handle.add_argument(f"--{key}", default=None)
    p_handle.add_argument("--timeout", default=None)
    p_handle.add_argument("message", nargs="?", default="")

    p_doc = sub.add_parser("doctor", parents=[seat_only], help="check the dependency group, encoder, source, and weights")
    p_doc.add_argument("--model", default=None, choices=MODEL_NAMES)

    p_prep = sub.add_parser("prepare", parents=[seat_only], help="install dependencies and download verified weights now")
    p_prep.add_argument("--model", default=None, choices=MODEL_NAMES)
    p_prep.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT * 4)

    p_cfg = sub.add_parser("config", parents=[seat_only], help="view or update defaults in music.json")
    cfg_sub = p_cfg.add_subparsers(dest="config_command")
    cfg_sub.add_parser("show", parents=[seat_only], help="print music.json settings")
    p_set = cfg_sub.add_parser("set", parents=[seat_only], help="save default settings in music.json")
    for key in CONFIG_KEYS:
        p_set.add_argument(f"--{key}", dest=f"cfg_{key}", default=None)
    p_unset = cfg_sub.add_parser("unset", parents=[seat_only], help="remove keys from music.json")
    p_unset.add_argument("keys", nargs="+", metavar="KEY")
    cfg_sub.add_parser("clear", parents=[seat_only], help="remove music.json")

    args = parser.parse_args(argv)
    if args.command == "handle":
        return handle_wake(
            seat=args.seat,
            sender=args.sender,
            allow=args.allow,
            message=args.message,
            defaults={key: getattr(args, key) for key in CONFIG_KEYS},
            timeout_raw=args.timeout,
        )
    if args.command == "doctor":
        try:
            model = validate_model(args.model or read_config(Path.cwd()).get("model") or "turbo")
        except MusicConfigError as exc:
            print(f"{args.seat}: {exc}", file=sys.stderr)
            return 1
        return doctor_report(Path.cwd(), model=model)
    if args.command == "prepare":
        return _cmd_prepare(args)
    if args.command == "config":
        return _cmd_config(args)
    parser.print_help()
    return 0
