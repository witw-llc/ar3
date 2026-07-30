"""Gmail connector — inbound side.

Polls the GAS Bridge for unread mail FROM `--from <address>` (the human's
address — replies to the connector come back from there). For each
unread thread: strips `Re:` / `Fwd:` repeats from the subject and shells
`tell <stripped-subject> <body>` from cwd, then marks the thread read
iff the tell exited 0.

Intended invocation: as an `idle.invoke` in the agent's a8s definition.
a8s fires idle with cwd = the agent's own root, so the `tell` shell-out
inherits the right cwd and a8s force-stamps `from` to the connector's
participant name. No registry / definition reading happens here — the
`--from` address is dependency-injected via the definition's argv.

stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


TIMEOUT_S = 30

# A cron host has no guarantee that the shims are on PATH, so the connector
# resolves the a8s entry point relative to its own file, the same way
# apps/r4t/notify.py does.
A8S_PY = Path(__file__).resolve().parent.parent.parent / "a8s.py"


# ---------- subject parse ----------

_PREFIXES = ("re:", "fwd:", "fw:")


def parse_subject_to_name(subject: str) -> str:
    """Strip leading `re:` / `fwd:` / `fw:` prefixes (case-insensitive,
    repeated, optional whitespace after the colon) until none remain.

    Examples:
      'Re: NEIL'        -> 'NEIL'
      'Re: Re: NEIL'    -> 'NEIL'
      'RE:NEIL'         -> 'NEIL'
      'Fwd: NEIL'       -> 'NEIL'
      're: fwd: NEIL'   -> 'NEIL'
      'NEIL urgent'     -> 'NEIL urgent'   (no prefix; tell will reject)
      ''                -> ''              (graceful)
    """
    s = (subject or "").strip()
    while True:
        lower = s.lower()
        matched = False
        for pref in _PREFIXES:
            if lower.startswith(pref):
                s = s[len(pref):].lstrip()
                matched = True
                break
        if not matched:
            break
    return s


# ---------- bridge HTTP ----------

def _bridge_post(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"error": f"non-JSON response: {body[:200]}"}


# ---------- main flow ----------

def _last_message(thread: dict) -> dict | None:
    msgs = thread.get("messages") or []
    return msgs[-1] if msgs else None


def _body_of(msg: dict) -> str:
    return (msg.get("plain") or msg.get("body") or "").strip()


def _process_thread(
    thread_summary: dict,
    *,
    bridge_url: str,
    bridge_key: str,
) -> tuple[bool, bool]:
    """Process a single thread. Returns (attempted, succeeded).

    `attempted` is True if we ran a `tell` (i.e. parsed a non-empty
    target name). `succeeded` is True iff the tell exited 0 AND the
    subsequent gmail.read succeeded.
    """
    thread_id = thread_summary.get("id")
    if not thread_id:
        return False, False

    try:
        full = _bridge_post(bridge_url, {
            "action": "gmail.get",
            "key": bridge_key,
            "thread_id": thread_id,
        })
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        print(f"[gmail-cron] gmail.get failed for {thread_id}: {e}", file=sys.stderr)
        return False, False
    if isinstance(full, dict) and full.get("error"):
        print(f"[gmail-cron] gmail.get error: {full['error']}", file=sys.stderr)
        return False, False

    msg = _last_message(full)
    if msg is None:
        print(f"[gmail-cron] thread {thread_id} has no messages", file=sys.stderr)
        return False, False

    subject = msg.get("subject") or ""
    body = _body_of(msg)
    target = parse_subject_to_name(subject)
    if not target:
        print(
            f"[gmail-cron] empty subject on thread {thread_id}; left unread",
            file=sys.stderr,
        )
        return False, False

    try:
        # No explicit cwd: a8s fired idle with cwd = the connector's
        # registered root, which is what tell needs for force-stamping.
        result = subprocess.run(
            [sys.executable, str(A8S_PY), "tell", target, body], check=False
        )
    except (OSError, FileNotFoundError) as e:
        print(f"[gmail-cron] tell shell-out failed: {e}", file=sys.stderr)
        return True, False
    if result.returncode != 0:
        # tell rejects unknown recipients (registry / canonical-name
        # validation). Leave unread so the operator can register the
        # agent and the next tick picks it up.
        print(
            f"[gmail-cron] tell {target!r} returned {result.returncode}; left unread",
            file=sys.stderr,
        )
        return True, False

    try:
        read = _bridge_post(bridge_url, {
            "action": "gmail.read",
            "key": bridge_key,
            "thread_id": thread_id,
        })
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        print(f"[gmail-cron] gmail.read failed for {thread_id}: {e}", file=sys.stderr)
        return True, False
    if isinstance(read, dict) and read.get("error"):
        print(f"[gmail-cron] gmail.read error: {read['error']}", file=sys.stderr)
        return True, False
    return True, True


def run(from_address: str) -> int:
    """Run one cron pass against the bridge, filtering on
    `is:unread from:<from_address>`. Env: GAS_BRIDGE_URL / GAS_BRIDGE_KEY."""
    bridge_url = os.environ.get("GAS_BRIDGE_URL", "").strip()
    bridge_key = os.environ.get("GAS_BRIDGE_KEY", "").strip()
    if not bridge_url or not bridge_key:
        print(
            "gmail-cron: GAS_BRIDGE_URL/KEY env vars must be set",
            file=sys.stderr,
        )
        return 2

    if not from_address:
        print("gmail-cron: --from must be a non-empty email address", file=sys.stderr)
        return 2

    try:
        search = _bridge_post(bridge_url, {
            "action": "gmail.search",
            "key": bridge_key,
            "query": f"is:unread from:{from_address}",
            "count": 20,
        })
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        print(f"gmail-cron: gmail.search failed: {e}", file=sys.stderr)
        return 1
    if isinstance(search, dict) and search.get("error"):
        print(f"gmail-cron: bridge error: {search['error']}", file=sys.stderr)
        return 1

    threads = (search or {}).get("messages") or []
    if not threads:
        return 0

    attempts = 0
    successes = 0
    for t in threads:
        attempted, ok = _process_thread(
            t,
            bridge_url=bridge_url,
            bridge_key=bridge_key,
        )
        if attempted:
            attempts += 1
            if ok:
                successes += 1

    # Exit 1 only if we tried at least once and every attempt failed.
    if attempts > 0 and successes == 0:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="gmail-cron",
        description=(
            "Inbound side of the a8s gmail connector. Polls the bridge for "
            "unread replies and shells `tell <name> <body>` for each. "
            "Intended to be invoked by a8s as the connector definition's "
            "`idle.invoke` so cwd is set correctly for force-stamping."
        ),
    )
    p.add_argument(
        "--from",
        dest="from_address",
        required=True,
        help="Email address whose replies should be routed back into a8s",
    )
    args = p.parse_args(argv)
    return run(args.from_address)


if __name__ == "__main__":
    raise SystemExit(main())
