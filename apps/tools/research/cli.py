"""CLI for tool-research: serve a8s wakes, resume checkpoints, manage `secrets.json`, and flush pending replies."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import tempfile
from pathlib import Path

import _runtime as rt

from .engine import is_checkpoint_id, list_checkpoints
from .handler import flush_pending, handle_wake
from .secretstore import (
    SecretStoreError,
    check_name,
    read_secrets,
    remove_secret,
    set_secret,
    store_path,
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
            names = sorted(read_secrets(workdir))
            for name in names:
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


def _cmd_resume(args: argparse.Namespace) -> int:
    workdir = Path.cwd().resolve()
    if args.list_only:
        items = list_checkpoints(workdir)
        if args.as_json:
            print(json.dumps({"checkpoints": items}, indent=2, ensure_ascii=True))
            return 0
        if not items:
            print(f"{args.seat}: no saved checkpoints")
            return 0
        for item in items:
            preview = " ".join(str(item.get("prompt") or "").split())
            if len(preview) > 72:
                preview = preview[:69] + "..."
            print(f"{item['id']}  {item['state']:<16}  {preview}")
        return 0

    target_text = " ".join(args.target).strip()
    if not target_text:
        print(
            f"{args.seat}: pass --list or a checkpoint ID / research question to resume",
            file=sys.stderr,
        )
        return 2

    flags: list[str] = []
    if is_checkpoint_id(target_text) and (workdir / ".research" / target_text).is_dir():
        flags.append(f"--resume={target_text}")
        body_tail = ""
    else:
        flags.append("--resume")
        body_tail = target_text
    if args.resume_fanout is not None:
        flags.append(f"--fanout={args.resume_fanout}")
    if args.as_json:
        flags.append("--json")
    message = " ".join(flags) + (f"\n{body_tail}" if body_tail else "")

    prev_reply_dir = os.environ.get(rt.REPLY_DIR_ENV)
    created_dir: str | None = None
    if not prev_reply_dir:
        created_dir = tempfile.mkdtemp(prefix="ar3-research-resume-")
        os.environ[rt.REPLY_DIR_ENV] = created_dir
    try:
        return handle_wake(
            seat=args.seat,
            sender="operator",
            allow="operator",
            message=message,
            workdir=workdir,
            default_model=args.model,
            default_planner_model=args.planner_model,
            job_timeout_raw=args.job_timeout,
            inline_max_raw=args.inline_max,
            api_url=args.api_url,
            attachments=(),
            force_resume=True,
        )
    finally:
        if created_dir is not None:
            if prev_reply_dir is None:
                os.environ.pop(rt.REPLY_DIR_ENV, None)
            else:
                os.environ[rt.REPLY_DIR_ENV] = prev_reply_dir


def main(argv: list[str] | None = None) -> int:
    in_ar3_tool = bool(os.environ.get("AR3_TOOL_PROG"))
    parser = _Parser(
        prog=os.environ.get("AR3_TOOL_PROG", "research"),
        description="Run deep web research via the OpenAI Responses API.",
    )
    parser.add_argument(
        "--seat",
        default="research",
        help=argparse.SUPPRESS if in_ar3_tool else "a8s node name (default: research)",
    )
    sub = parser.add_subparsers(dest="command")

    seat_only = argparse.ArgumentParser(add_help=False)
    seat_only.add_argument(
        "--seat",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS if in_ar3_tool else "a8s node name (default: research)",
    )

    p_handle = sub.add_parser("handle", parents=[seat_only], help="a8s wake entry point")
    p_handle.add_argument("--from", dest="sender", default="")
    p_handle.add_argument("--allow", default="")
    p_handle.add_argument("--model", default=None)
    p_handle.add_argument("--planner-model", dest="planner_model", default=None)
    p_handle.add_argument("--fanout", default=None)
    p_handle.add_argument("--job-timeout", dest="job_timeout", default=None)
    p_handle.add_argument("--inline-max", dest="inline_max", default=None)
    p_handle.add_argument("--api-url", dest="api_url", default=None)
    p_handle.add_argument("--resume", action="store_true", default=False)
    p_handle.add_argument("message", nargs="?", default="")

    p_resume = sub.add_parser(
        "resume",
        parents=[seat_only],
        help="list or resume saved research checkpoints in .research/",
    )
    p_resume.add_argument(
        "--list",
        dest="list_only",
        action="store_true",
        help="list saved checkpoints in .research/",
    )
    p_resume.add_argument(
        "--fanout",
        dest="resume_fanout",
        nargs="?",
        const="auto",
        default=None,
        help="fanout count (1..8) or auto",
    )
    p_resume.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit JSON output",
    )
    p_resume.add_argument("--model", default=None)
    p_resume.add_argument("--planner-model", dest="planner_model", default=None)
    p_resume.add_argument("--job-timeout", dest="job_timeout", default=None)
    p_resume.add_argument("--inline-max", dest="inline_max", default=None)
    p_resume.add_argument("--api-url", dest="api_url", default=None)
    p_resume.add_argument(
        "target",
        nargs="*",
        metavar="CHECKPOINT_OR_QUESTION",
        help="checkpoint ID or research question to resume",
    )

    sub.add_parser(
        "idle",
        parents=[seat_only],
        help="flush held replies from .research/pending/",
    )

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
    p_sec_set.add_argument(
        "source",
        nargs="?",
        default=None,
        metavar="-",
        help="pass - to read value from stdin",
    )
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
            default_model=args.model,
            default_planner_model=args.planner_model,
            default_fanout_raw=args.fanout,
            job_timeout_raw=args.job_timeout,
            inline_max_raw=args.inline_max,
            api_url=args.api_url,
            force_resume=bool(args.resume),
        )
    if args.command == "resume":
        return _cmd_resume(args)
    if args.command == "idle":
        return flush_pending(Path.cwd())
    if args.command == "secret":
        return _cmd_secret(args)
    return 2
