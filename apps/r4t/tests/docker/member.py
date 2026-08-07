#!/usr/bin/env python3
"""Scripted r4t member for the real run_as isolation test (tests/docker).

This runs as the *agent* user, wrapped by `sudo -u agent bash --login -c ...`
(dispatch.run_harness -> isolate.wrap_run_as). It makes no LLM call: it probes
the boundary from the inside with functional checks — actual writes, real
`sudo` attempts, real reads — and records the outcome to `<cwd>/agent-results.json`
(cwd is the workplace, /work). inside.sh asserts on that report.

It deliberately does NOT emit a tell; dispatch's stdout fallback handles the
empty staging. The point is the report, not the reply.
"""
from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

ATTACHED_FILE_PREFIX = "ATTACHED FILE: "  # twin of apps/a8s/definitions.py
R4T_HOME = "/var/lib/r4t"  # provisioned in inside.sh
MEMBER_STORE = f"{R4T_HOME}/rosters/acme/agents/worker/k7e"  # knowledge.store_home


def _effective_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return f"uid:{os.getuid()}"


def _outbox_checks() -> dict:
    """Prove TELL_OUTBOX_DIR survived env_reset and that the agent can write
    there, and that a created file inherits the staging group (setgid 2770)."""
    outbox = os.environ.get("TELL_OUTBOX_DIR", "")
    result = {
        "tell_outbox_dir": outbox,
        "outbox_writable": False,
        "outbox_file_group_matches_dir": False,
    }
    if not outbox:
        return result
    d = Path(outbox)
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / "_boundary-probe.txt"
        probe.write_text("agent was here\n", encoding="utf-8")
        result["outbox_writable"] = True
        # Functional setgid evidence: the file's group == the dir's group,
        # which r4t chgrp'd to the agent's own group before the turn.
        result["outbox_file_group_matches_dir"] = (
            probe.stat().st_gid == d.stat().st_gid
        )
        probe.unlink()
    except OSError:
        pass
    return result


def _agent_can_sudo() -> bool:
    """The sandbox must be sudo-LESS. `sudo -n true` must be refused."""
    try:
        proc = subprocess.run(
            ["sudo", "-n", "true"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False  # cannot even run sudo -> certainly cannot escalate
    return proc.returncode == 0


def _can_read_router_home() -> bool:
    try:
        os.listdir("/home/router")
        return True
    except OSError:
        return False


def _can_write_router_home() -> bool:
    target = Path("/home/router/_agent-escape.txt")
    try:
        target.write_text("x", encoding="utf-8")
        target.unlink()
        return True
    except OSError:
        return False


def _can_list_r4t_home() -> bool:
    try:
        os.listdir(R4T_HOME)
        return True
    except OSError:
        return False


def _can_read_member_store() -> bool:
    """The knowledge store is r4t state the turn is never handed. Its files
    carry the operator's umask, so the boundary is the store dir's own mode."""
    try:
        return bool(Path(MEMBER_STORE, "notes.md").read_text(encoding="utf-8"))
    except OSError:
        return False


def _delivered_checks(prompt: str) -> dict:
    """The prompt's ATTACHED FILE line must point at a delivered copy the agent
    can read but not write — dispatch marshals it across the boundary because
    the a8s original sits in the router's sealed home."""
    result = {
        "delivered_path": "",
        "delivered_readable": False,
        "delivered_writable": False,
    }
    for line in prompt.splitlines():
        if not line.startswith(ATTACHED_FILE_PREFIX):
            continue
        path = Path(line[len(ATTACHED_FILE_PREFIX):].strip())
        result["delivered_path"] = str(path)
        try:
            result["delivered_readable"] = bool(path.read_text(encoding="utf-8"))
        except OSError:
            pass
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write("tamper")
            result["delivered_writable"] = True
        except OSError:
            pass
        break
    return result


def main() -> int:
    report = {
        "effective_user": _effective_user(),
        "agent_can_sudo": _agent_can_sudo(),
        "can_read_router_home": _can_read_router_home(),
        "can_write_router_home": _can_write_router_home(),
        "can_list_r4t_home": _can_list_r4t_home(),
        "can_read_member_store": _can_read_member_store(),
        **_outbox_checks(),
        **_delivered_checks(sys.argv[1] if len(sys.argv) > 1 else ""),
    }
    # cwd is the workplace (/work); dispatch cd'd here via the bootstrap's `cd "$2"`.
    Path("agent-results.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print("member: boundary probe complete for user", report["effective_user"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
