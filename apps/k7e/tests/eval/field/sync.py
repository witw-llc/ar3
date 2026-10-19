#!/usr/bin/env python3
"""Read-only field sync for k7e's memory evaluation.

usage: sync.py --dest DIR --host HOST [--agent NAME ...] [--a8s PATH]
               [--r4t-home PATH] [--ssh CMD] [--rsync CMD] [--timeout S]
               [--dry-run] [--json]

Copies each agent's a8s conversation archive and r4t engine-memory turn
captures from a field machine into a local corpus. Only read commands run on
the far end.
"""
import argparse
import datetime
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

WT = Path(__file__).resolve().parents[5]

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
MEMORY_VALUES = ("on", "off", "small", "medium", "large")
FIRST_PULL_CURSOR = "1970-01-01"


class Refused(Exception):
    pass


class FarError(Exception):
    pass


def host_label(host):
    label = re.sub(r"[@:/]", "-", host)
    if not NAME.match(label):
        raise Refused(f"host {host!r} does not make a plain directory name")
    return label


def agent_dir(dest, host, agent):
    """The directory one agent owns, checked before anything is created: a
    plain agent name, a corpus outside the repo, and a destination that
    resolves (through any symlink) to a direct child of the host directory."""
    if not NAME.match(agent):
        raise Refused(f"agent name must be a plain name, not {agent!r}")
    repo = WT.resolve()
    base = (Path(dest) / host_label(host)).resolve()
    if base == repo or repo in base.parents:
        raise Refused(f"dest {dest} is inside the repo {repo}")
    path = (base / agent).resolve()
    if path.parent != base or path == repo or repo in path.parents:
        raise Refused(f"agent {agent!r} resolves to {path}, outside {base}")
    return path


def far_path(path):
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


class Far:
    def __init__(self, ssh, host, timeout):
        self.ssh = shlex.split(ssh)
        self.host = host
        self.timeout = timeout

    def run(self, command):
        try:
            done = subprocess.run(
                [*self.ssh, self.host, command],
                capture_output=True, text=True, timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            raise FarError(f"timed out after {self.timeout}s: {command}")
        except OSError as e:
            raise FarError(f"cannot run {self.ssh[0]}: {e}")
        if done.returncode == 255:
            raise FarError(f"host {self.host} unreachable: {done.stderr.strip()}")
        return done


def memory_value(far, a8s, agent):
    done = far.run(f"{a8s} vars {shlex.quote(agent)}")
    if done.returncode != 0:
        raise FarError(f"a8s vars {agent} failed: {done.stderr.strip()}")
    for line in done.stdout.splitlines():
        parts = line.split(None, 1)
        if parts and parts[0].upper() == "MEMORY":
            return (parts[1].strip().lower() if len(parts) > 1 else "off")
    return "off"


def convo_command(a8s, agent, cursor):
    return f"{a8s} convo {shlex.quote(agent)} --json --since {shlex.quote(cursor or FIRST_PULL_CURSOR)}"


def fetch_rows(far, a8s, agent, cursor):
    done = far.run(convo_command(a8s, agent, cursor))
    if done.returncode != 0:
        raise FarError(f"a8s convo {agent} failed: {done.stderr.strip()}")
    rows = []
    for line in done.stdout.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            raise FarError(f"a8s convo {agent} printed a line that is not JSON")
        if not isinstance(row, dict) or not isinstance(row.get("ulid"), str) or not row["ulid"]:
            raise FarError(f"a8s convo {agent} printed a row without a ulid")
        rows.append(row)
    return rows


def count_rows(far, a8s, agent, cursor):
    # The producer's own exit status is the check; a pipe into `wc` would
    # report a failed read as zero rows.
    done = far.run(convo_command(a8s, agent, cursor))
    if done.returncode != 0:
        raise FarError(f"a8s convo {agent} count read failed: {done.stderr.strip()}")
    return sum(1 for line in done.stdout.splitlines() if line.strip())


def memory_homes(far, r4t_home, agent):
    label = re.sub(r"[^a-z0-9_.-]", "-", agent.lower())[:48]
    done = far.run(f"ls -d {far_path(r4t_home)}/engine-memory/{label}-*")
    if done.returncode != 0:
        return []
    pattern = re.compile(re.escape(label) + r"-[0-9a-f]{20}\Z")
    return [p for p in done.stdout.splitlines() if pattern.match(os.path.basename(p))]


def count_far_turns(far, home):
    done = far.run(f"find {shlex.quote(home)}/turns -maxdepth 1 -name '*.json'")
    if done.returncode != 0:
        raise FarError(f"could not count far-end turns: {done.stderr.strip()}")
    return sum(1 for line in done.stdout.splitlines() if line.strip())


def rsync_flavor(rsync):
    try:
        done = subprocess.run([rsync, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return "rsync"
    return "openrsync" if "openrsync" in done.stdout.split("\n", 1)[0] else "rsync"


def rsync_command(args, home, dest):
    # openrsync (macOS) hands the source path to the far end's shell, so
    # it carries shell quoting. rsync 3 sends the path protected (-s), where
    # a quote is a character of the path.
    if args.rsync_flavor == "openrsync":
        protect, source = [], f"{args.host}:{shlex.quote(home)}/turns/"
    else:
        protect, source = ["-s"], f"{args.host}:{home}/turns/"
    return [args.rsync, "-a", "--ignore-existing", *protect, "-e", args.ssh, source, f"{dest}/"]


def local_turns(path):
    turns = path / "turns"
    return len(list(turns.glob("*.json"))) if turns.is_dir() else 0


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def stored_ulids(path):
    seen, last = set(), None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return seen, last
    for line in text.splitlines():
        try:
            ulid = json.loads(line).get("ulid")
        except (ValueError, AttributeError):
            continue
        if ulid:
            seen.add(ulid)
            last = ulid
    return seen, last


def write_atomic(path, text):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def pull(args, far, agent, path):
    memory = memory_value(far, args.a8s, agent)
    if memory not in MEMORY_VALUES:
        raise FarError(f"unknown MEMORY value {memory!r}")
    seen, cursor = stored_ulids(path / "convo.jsonl")
    manifest = read_json(path / "sync.json", {})
    pulls = manifest.get("pulls", []) if isinstance(manifest.get("pulls"), list) else []

    rows = fetch_rows(far, args.a8s, agent, cursor)
    new_rows = [r for r in rows if r["ulid"] not in seen]
    far_rows = count_rows(far, args.a8s, agent, cursor)

    home = None
    far_turns = None
    notes = []
    if memory == "off":
        notes.append("memory off: turns skipped")
    else:
        homes = memory_homes(far, args.r4t_home, agent)
        if len(homes) > 1:
            raise FarError(f"more than one memory home for {agent}: {' '.join(homes)}")
        if homes:
            home = homes[0]
            far_turns = count_far_turns(far, home)
        else:
            notes.append("memory on but no memory home on the far end")

    path.mkdir(parents=True, exist_ok=True)
    if new_rows:
        with open(path / "convo.jsonl", "a", encoding="utf-8") as f:
            for row in new_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    cursor_after = rows[-1]["ulid"] if rows else cursor

    turns_before = local_turns(path)
    if home is not None:
        (path / "turns").mkdir(exist_ok=True)
        done = subprocess.run(rsync_command(args, home, path / "turns"),
                              capture_output=True, text=True, timeout=args.timeout)
        if done.returncode != 0:
            notes.append(f"rsync failed ({done.returncode}): {done.stderr.strip()}")
            far_turns = None
    turns_total = local_turns(path)

    verified = len(new_rows) == far_rows
    if not verified:
        notes.append(f"rows: far end {far_rows}, appended {len(new_rows)}")
    if memory != "off":
        if far_turns is None or turns_total < far_turns:
            verified = False
            notes.append(f"turns: far end {far_turns}, local {turns_total}")

    _, last = stored_ulids(path / "convo.jsonl")
    record = {
        "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "memory": memory,
        "cursor_before": cursor,
        "cursor_after": cursor_after,
        "rows_added": len(new_rows),
        "rows_total": len(seen) + len(new_rows),
        "turns_added": turns_total - turns_before,
        "turns_total": turns_total,
        "verified": verified,
        "note": "; ".join(notes),
    }
    write_atomic(path / "sync.json", json.dumps(
        {"agent": agent, "host": args.host, "cursor": last, "pulls": pulls + [record]},
        indent=2) + "\n")
    return {"agent": agent, **record, "far_rows": far_rows, "far_turns": far_turns}


def human(result):
    if "error" in result:
        return f"{result['agent']}: FAILED {result['error']}"
    if result["memory"] == "off":
        turns = "turns skipped"
    else:
        turns = f"+{result['turns_added']} turns ({result['turns_total']})"
    state = "verified" if result["verified"] else f"NOT VERIFIED ({result['note']})"
    return (f"{result['agent']}: memory {result['memory']}, "
            f"+{result['rows_added']} rows ({result['rows_total']}), {turns}, {state}")


def dry_run(args, agents):
    if not agents:
        print(f"would run on the far end: {args.a8s} ls -q")
    for agent in agents or ["<agent>"]:
        print(f"{agent}: would run on the far end: {args.a8s} vars {agent}")
        print(f"{agent}: would run on the far end: {convo_command(args.a8s, agent, '<cursor>')}")
        print(f"{agent}: unless memory is off, would run on the far end: "
              f"ls -d {args.r4t_home}/engine-memory/{agent.lower()}-*")
        command = rsync_command(args, "<memory home>", Path(args.dest) / host_label(args.host) / agent / "turns")
        print(f"{agent}: unless memory is off, would run: {shlex.join(command)}")


def main(argv=None):
    p = argparse.ArgumentParser(description="Read-only field sync for k7e's memory evaluation.")
    p.add_argument("--dest", required=True, help="corpus root, outside the repo")
    p.add_argument("--host", required=True, help="ssh target, for example user@box")
    p.add_argument("--agent", action="append", default=[], metavar="NAME",
                   help="agent to pull (repeat; default: every node `a8s ls -q` lists)")
    p.add_argument("--a8s", default="~/.ar3/a8s", help="a8s launcher on the far end")
    p.add_argument("--r4t-home", default="~/.config/r4t", help="r4t home on the far end")
    p.add_argument("--ssh", default="ssh -o BatchMode=yes", help="command prefix that runs a far-end command")
    p.add_argument("--rsync", default="rsync", help="rsync command")
    p.add_argument("--timeout", type=int, default=60, help="seconds allowed per far-end command")
    p.add_argument("--dry-run", action="store_true", help="print what would run and pull nothing")
    p.add_argument("--json", action="store_true", help="one JSON summary per agent")
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    if args.host.startswith("-"):
        print("sync: --host must not start with '-'", file=sys.stderr)
        return 2
    a8s = far_path(args.a8s)
    args.a8s = a8s
    args.rsync_flavor = rsync_flavor(args.rsync)
    try:
        for agent in args.agent:
            agent_dir(args.dest, args.host, agent)
        host_label(args.host)
    except Refused as e:
        print(f"sync: {e}", file=sys.stderr)
        return 2

    if args.dry_run:
        dry_run(args, args.agent)
        return 0

    far = Far(args.ssh, args.host, args.timeout)
    agents = args.agent
    results = []
    if not agents:
        try:
            done = far.run(f"{a8s} ls -q")
            if done.returncode != 0:
                raise FarError(f"a8s ls failed: {done.stderr.strip()}")
        except FarError as e:
            results.append({"agent": "*", "error": str(e)})
        else:
            agents = done.stdout.split()
    for agent in agents:
        try:
            results.append(pull(args, far, agent, agent_dir(args.dest, args.host, agent)))
        except (FarError, Refused) as e:
            results.append({"agent": agent, "error": str(e)})
        except subprocess.TimeoutExpired:
            results.append({"agent": agent, "error": f"rsync timed out after {args.timeout}s"})
    if not results:
        print("sync: the far end lists no agents", file=sys.stderr)
    for result in results:
        print(json.dumps(result) if args.json else human(result))
    return 0 if results and all(r.get("verified") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
