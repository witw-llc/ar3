"""CLI for tool-speak: serve a8s wakes and manage local speech configuration and secrets."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys

from .engine import doctor_report, list_voices_report
from .handler import handle_wake
from .secretstore import (
    SecretStoreError,
    check_name,
    read_secrets,
    remove_secret,
    set_secret,
    store_path,
)
from .store import (
    SpeakConfigError,
    clear_config,
    clear_pronunciations,
    clear_replacements,
    config_path,
    parse_pair_spec,
    pronunciations_path,
    read_config,
    read_pronunciations,
    read_replacements,
    remove_pronunciation,
    remove_replacement,
    replacements_path,
    set_pronunciation,
    set_replacement,
    update_config,
)

MAX_SECRET_BYTES = 65536


class _Parser(argparse.ArgumentParser):
    """Suppress argument echoing on `secret` parse errors so mistyped secrets never print."""

    def error(self, message: str) -> None:
        if "secret" not in sys.argv[1:]:
            super().error(message)
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: the secret arguments are not valid\n")


def _read_secret_value(name: str) -> str:
    if sys.stdin.isatty():
        return getpass.getpass(f"Value for {name}: ")
    raw = sys.stdin.buffer.read(MAX_SECRET_BYTES + 1)
    if len(raw) > MAX_SECRET_BYTES:
        raise SecretStoreError(f"a secret value is at most {MAX_SECRET_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SecretStoreError("a secret value must be UTF-8 text") from None
    return text[:-1] if text.endswith("\n") else text


def _cmd_secret(args: argparse.Namespace) -> int:
    workdir = Path.cwd().resolve()
    try:
        if args.secret_command == "list":
            for name in sorted(read_secrets(workdir)):
                print(name)
            return 0
        if args.secret_command == "rm":
            if not remove_secret(workdir, args.name):
                print(f"{args.seat}: secret {args.name} is not set", file=sys.stderr)
                return 1
            print(f"{args.seat}: removed {args.name}")
            return 0
        if args.secret_command == "set":
            check_name(args.name)
            if args.source not in (None, "-"):
                raise SecretStoreError(
                    "the value is never an argument: pipe it in and pass - after the name"
                )
            set_secret(workdir, args.name, _read_secret_value(args.name))
            print(f"{args.seat}: set {args.name} in {store_path(workdir)}")
            return 0
    except SecretStoreError as exc:
        print(f"{args.seat}: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(
            f"{args.seat}: cannot write {store_path(workdir)}: {exc.strerror or exc}",
            file=sys.stderr,
        )
        return 1
    return 2


def _cmd_config(args: argparse.Namespace) -> int:
    workdir = Path.cwd().resolve()
    sub = args.config_command or "show"
    try:
        if sub == "show":
            cfg = read_config(workdir)
            if not cfg:
                print(f"{args.seat}: default configuration (no {config_path(workdir).name})")
                return 0
            print(json.dumps(cfg, indent=2, sort_keys=True))
            return 0
        if sub == "clear":
            existed = clear_config(workdir)
            note = "cleared" if existed else "already empty"
            print(f"{args.seat}: {note} ({config_path(workdir)})")
            return 0
        if sub == "unset":
            updated = update_config(workdir, unset_keys=args.keys)
            print(
                f"{args.seat}: updated {config_path(workdir)} "
                f"({len(updated)} setting(s) remaining)"
            )
            return 0
        if sub == "set":
            if all(
                getattr(args, field) is None
                for field in ("cfg_engine", "cfg_voice", "cfg_model", "cfg_format", "cfg_speed", "cfg_style")
            ):
                print(
                    f"{args.seat}: pass at least one of --engine, --voice, --model, --format, --speed, --style",
                    file=sys.stderr,
                )
                return 2
            updated = update_config(
                workdir,
                engine=args.cfg_engine,
                voice=args.cfg_voice,
                model=args.cfg_model,
                audio_format=args.cfg_format,
                speed=args.cfg_speed,
                style=args.cfg_style,
            )
            print(f"{args.seat}: saved {config_path(workdir)}: {json.dumps(updated, sort_keys=True)}")
            return 0
    except (SpeakConfigError, OSError) as exc:
        print(f"{args.seat}: {exc}", file=sys.stderr)
        return 1
    return 2


def _cmd_pair_table(
    args: argparse.Namespace,
    *,
    sub_attr: str,
    kind: str,
    path_fn,
    read_fn,
    set_fn,
    rm_fn,
    clear_fn,
) -> int:
    workdir = Path.cwd().resolve()
    sub = getattr(args, sub_attr)
    try:
        if sub == "list":
            pairs = read_fn(workdir)
            if not pairs:
                print(f"{args.seat}: no {kind}s ({path_fn(workdir)})")
                return 0
            for pat, rhs in pairs:
                print(f"{pat} => {rhs}")
            return 0
        if sub == "set":
            if args.target_val is None:
                pat, rhs = parse_pair_spec(args.pattern, kind)
            else:
                pat, rhs = args.pattern, args.target_val
            outcome = set_fn(workdir, pat, rhs)
            print(f"{args.seat}: {outcome} {kind} {pat} => {rhs} in {path_fn(workdir)}")
            return 0
        if sub == "rm":
            if not rm_fn(workdir, args.pattern):
                print(f"{args.seat}: {kind} {args.pattern!r} not found", file=sys.stderr)
                return 1
            print(f"{args.seat}: removed {kind} {args.pattern!r}")
            return 0
        if sub == "clear":
            count = clear_fn(workdir)
            print(f"{args.seat}: cleared {count} {kind}(s)")
            return 0
    except (SpeakConfigError, OSError) as exc:
        print(f"{args.seat}: {exc}", file=sys.stderr)
        return 1
    return 2


def main(argv: list[str] | None = None) -> int:
    in_ar3_tool = bool(os.environ.get("AR3_TOOL_PROG"))
    parser = _Parser(
        prog=os.environ.get("AR3_TOOL_PROG", "speak"),
        description="Synthesize speech from text or markdown across local and cloud TTS engines.",
    )
    parser.add_argument(
        "--seat",
        default="speak",
        help=argparse.SUPPRESS if in_ar3_tool else "a8s node name (default: speak)",
    )
    sub = parser.add_subparsers(dest="command")

    seat_only = argparse.ArgumentParser(add_help=False)
    seat_only.add_argument(
        "--seat",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS if in_ar3_tool else "a8s node name (default: speak)",
    )

    p_handle = sub.add_parser("handle", parents=[seat_only], help="a8s wake entry point")
    p_handle.add_argument("--from", dest="sender", default="")
    p_handle.add_argument("--allow", default="")
    p_handle.add_argument("--engine", default=None)
    p_handle.add_argument("--voice", default=None)
    p_handle.add_argument("--model", default=None)
    p_handle.add_argument("--format", dest="audio_format", default=None)
    p_handle.add_argument("--speed", default=None)
    p_handle.add_argument("--style", default=None)
    p_handle.add_argument("--timeout", default=None)
    p_handle.add_argument("message", nargs="?", default="")

    p_doc = sub.add_parser(
        "doctor",
        parents=[seat_only],
        help="check local binaries, engines, secrets, and working-directory files",
    )
    p_doc.add_argument("--engine", default=None, help="engine to check")
    p_doc.add_argument("--voice", default=None, help="voice preference to check")

    p_voices = sub.add_parser(
        "voices",
        parents=[seat_only],
        help="list built-in and installed voices per engine",
    )
    p_voices.add_argument("--engine", default=None, help="filter by engine name")

    p_cfg = sub.add_parser(
        "config",
        parents=[seat_only],
        help="view or update saved voice/engine defaults in speak.json",
    )
    cfg_sub = p_cfg.add_subparsers(dest="config_command")
    cfg_sub.add_parser("show", parents=[seat_only], help="print speak.json settings")
    p_cfg_set = cfg_sub.add_parser("set", parents=[seat_only], help="save default settings in speak.json")
    p_cfg_set.add_argument("--engine", "--vendor", dest="cfg_engine", default=None)
    p_cfg_set.add_argument("--voice", "-v", dest="cfg_voice", default=None)
    p_cfg_set.add_argument("--model", dest="cfg_model", default=None)
    p_cfg_set.add_argument("--format", dest="cfg_format", default=None)
    p_cfg_set.add_argument("--speed", dest="cfg_speed", default=None)
    p_cfg_set.add_argument("--style", dest="cfg_style", default=None)
    p_cfg_unset = cfg_sub.add_parser("unset", parents=[seat_only], help="remove one or more keys from speak.json")
    p_cfg_unset.add_argument("keys", nargs="+", metavar="KEY")
    cfg_sub.add_parser("clear", parents=[seat_only], help="remove speak.json")

    p_rep = sub.add_parser("replace", parents=[seat_only], help="manage spoken regex replacements in replacements.txt")
    rep_sub = p_rep.add_subparsers(dest="replace_command", required=True)
    rep_sub.add_parser("list", parents=[seat_only], help="list rules in replacements.txt")
    p_rep_set = rep_sub.add_parser("set", parents=[seat_only], help="add or update a replacement rule")
    p_rep_set.add_argument("pattern", metavar="PATTERN", help="Python regex (or 'pattern => spoken')")
    p_rep_set.add_argument("target_val", nargs="?", default=None, metavar="SPOKEN", help="spoken replacement text")
    p_rep_rm = rep_sub.add_parser("rm", parents=[seat_only], help="remove a replacement rule")
    p_rep_rm.add_argument("pattern", metavar="PATTERN")
    rep_sub.add_parser("clear", parents=[seat_only], help="remove all replacement rules")

    p_pro = sub.add_parser("pronounce", parents=[seat_only], help="manage Kokoro IPA pronunciations in pronunciations.txt")
    pro_sub = p_pro.add_subparsers(dest="pronounce_command", required=True)
    pro_sub.add_parser("list", parents=[seat_only], help="list rules in pronunciations.txt")
    p_pro_set = pro_sub.add_parser("set", parents=[seat_only], help="add or update an IPA pronunciation rule")
    p_pro_set.add_argument("pattern", metavar="PATTERN", help="Python regex (or 'pattern => ipa')")
    p_pro_set.add_argument("target_val", nargs="?", default=None, metavar="IPA", help="IPA string without slashes")
    p_pro_rm = pro_sub.add_parser("rm", parents=[seat_only], help="remove a pronunciation rule")
    p_pro_rm.add_argument("pattern", metavar="PATTERN")
    pro_sub.add_parser("clear", parents=[seat_only], help="remove all pronunciation rules")

    p_sec = sub.add_parser(
        "secret",
        parents=[seat_only],
        help="manage API keys in secrets.json (mode 0600)",
    )
    sec_sub = p_sec.add_subparsers(dest="secret_command", required=True)
    sec_sub.add_parser("list", parents=[seat_only], help="print stored secret names")
    p_sec_set = sec_sub.add_parser(
        "set",
        parents=[seat_only],
        help="store a secret from stdin (-) or hidden prompt",
    )
    p_sec_set.add_argument("name", metavar="NAME")
    p_sec_set.add_argument("source", nargs="?", default=None, metavar="-")
    p_sec_rm = sec_sub.add_parser("rm", parents=[seat_only], help="remove a secret")
    p_sec_rm.add_argument("name", metavar="NAME")

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
            default_voice=args.voice,
            default_model=args.model,
            default_format=args.audio_format,
            default_speed=args.speed,
            default_style=args.style,
            timeout_raw=args.timeout,
        )
    if args.command == "doctor":
        return doctor_report(Path.cwd(), engine=args.engine, voice=args.voice)
    if args.command == "voices":
        return list_voices_report(Path.cwd(), engine=args.engine)
    if args.command == "config":
        return _cmd_config(args)
    if args.command == "replace":
        return _cmd_pair_table(
            args,
            sub_attr="replace_command",
            kind="replacement",
            path_fn=replacements_path,
            read_fn=read_replacements,
            set_fn=set_replacement,
            rm_fn=remove_replacement,
            clear_fn=clear_replacements,
        )
    if args.command == "pronounce":
        return _cmd_pair_table(
            args,
            sub_attr="pronounce_command",
            kind="pronunciation",
            path_fn=pronunciations_path,
            read_fn=read_pronunciations,
            set_fn=set_pronunciation,
            rm_fn=remove_pronunciation,
            clear_fn=clear_pronunciations,
        )
    if args.command == "secret":
        return _cmd_secret(args)
    return 2
