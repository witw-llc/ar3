"""Argument parsing shared by node listings."""
from __future__ import annotations

import argparse
import math
import time

from ar3 import clock
from convo import parse_duration


def arguments(command: str, args: list[str]):
    parser = argparse.ArgumentParser(
        prog=f"a8s {command}", allow_abbrev=False,
        description=("List registered local nodes and remembered remote names." if command == "ls"
                     else "List running local node processes; remote process status is unknown."),
    )
    parser.add_argument("-q", action="store_true", help="Print names only.")
    if command == "ps":
        parser.add_argument(
            "--json", action="store_true",
            help="One JSON object per running node: name, pid, and busy (busy, idle or unknown).",
        )
    if command == "ls":
        parser.add_argument("--autostart", action="store_true", help="Show local startup preferences and OS entries.")
        source = parser.add_mutually_exclusive_group()
        source.add_argument("--local", action="store_true", help="Show registered local nodes only.")
        source.add_argument("--remote", action="store_true", help="Show remembered remote names only.")
        parser.add_argument(
            "--since", metavar="CUTOFF",
            help="Keep remotes heard at or after a duration ago (24h, 7d) or an ISO timestamp; locals stay visible. Naive dates use UTC.",
        )
    try:
        parsed = parser.parse_args(args)
        if command == "ls":
            if parsed.autostart and (parsed.remote or parsed.since is not None):
                parser.error("--autostart shows local startup state; it cannot be combined with --remote or --since")
            parsed.cutoff = None
            if parsed.since is not None:
                try:
                    seconds = parse_duration(parsed.since)
                except ValueError:
                    stamp = clock.to_local(parsed.since)
                    if stamp is None:
                        parser.error("--since needs a duration such as 24h or an ISO timestamp")
                    parsed.cutoff = stamp.timestamp()
                else:
                    if not math.isfinite(seconds):
                        parser.error("--since needs a finite duration")
                    parsed.cutoff = time.time() - seconds
        return parsed
    except SystemExit as exc:
        return int(exc.code)
