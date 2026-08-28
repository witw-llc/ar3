"""Antigravity quota — the local language-server API.

A running Antigravity (IDE or a persistent `agy --continue` session) exposes
a localhost Connect-RPC API; `GetUserStatus` carries one `quotaInfo` per
model. The pools mirror how `agy /usage` presents them: Gemini models burn
the weekly limit, Claude/GPT models the five-hour one.

Antigravity meters four pools and the local API carries two. The other two —
Gemini five-hour and Claude/GPT weekly — are held by the vendor's cloud, which
answers only to gemini-cli's OAuth client pair. Reading them would mean
shipping that pair, so this reports what the local API knows and says which
two it is. A `--print` run exits too fast to serve the API; only persistent
sessions answer.
"""
from __future__ import annotations

import json
import re
import shutil
import ssl
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from engines.base import QuotaError

TIMEOUT_S = 8
USER_STATUS = "/exa.language_server_pb.LanguageServerService/GetUserStatus"

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def quota() -> dict:
    pid, csrf = _find_language_server()
    ports = _listen_ports(pid)
    if not ports:
        raise QuotaError(
            f"antigravity pid {pid} has no local API listeners — a --print run "
            "exits too fast; use the IDE or a persistent agy --continue session"
        )
    payload = _post_user_status(ports, csrf)
    return parse_user_status(payload)


def _find_language_server() -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["ps", "-ax", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QuotaError(f"cannot list processes: {exc}") from exc
    best: tuple[int, int, str] | None = None
    for line in proc.stdout.splitlines():
        match = re.match(r"^\s*(\d+)\s+(.+)$", line)
        if not match:
            continue
        command = match.group(2)
        lower = command.lower()
        if "language_server" not in lower or "antigravity" not in lower:
            continue
        csrf = re.search(r"--csrf_token(?:=|\s+)(\S+)", command)
        score = 1 + (50 if csrf else 0)
        if best is None or score > best[0]:
            best = (score, int(match.group(1)), csrf.group(1) if csrf else "")
    if best is None:
        raise QuotaError(
            "antigravity language server is not running — start the IDE or a "
            "persistent agy --continue session"
        )
    return best[1], best[2]


def _listen_ports(pid: int) -> list[int]:
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        proc = subprocess.run(
            [lsof, "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QuotaError(f"lsof failed discovering the local API port: {exc}") from exc
    return sorted(
        {int(m.group(1)) for m in re.finditer(r":(\d+)\s+\(LISTEN\)", proc.stdout)}
    )


def _post_user_status(ports: list[int], csrf: str) -> dict:
    body = json.dumps(
        {"metadata": {"ideName": "antigravity", "extensionName": "r4t", "locale": "en"}}
    ).encode("utf-8")
    last: Exception | None = None
    for port in ports:
        for scheme, ctx in (("https", _SSL_CTX), ("http", None)):
            request = urllib.request.Request(
                f"{scheme}://127.0.0.1:{port}{USER_STATUS}",
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Connect-Protocol-Version": "1",
                    "X-Codeium-Csrf-Token": csrf,
                },
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=TIMEOUT_S, context=ctx
                ) as response:
                    return json.loads(response.read().decode("utf-8"))
            except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
                last = exc
    raise QuotaError(f"local API did not answer GetUserStatus: {last}")


def parse_user_status(payload: dict) -> dict:
    status = payload.get("userStatus", payload)
    models = _models_with_quota(status)
    if not models:
        raise QuotaError("GetUserStatus answered without per-model quota")
    pools: dict[str, dict] = {}
    for label, info in models:
        lower = label.lower()
        if "gemini" in lower:
            pool, bucket_label = "gemini", "Weekly Limit"
        elif "claude" in lower or "gpt" in lower:
            pool, bucket_label = "claude-gpt", "Five Hour Limit"
        else:
            continue
        fraction = info.get("remainingFraction")
        if not isinstance(fraction, (int, float)):
            continue
        current = pools.get(pool)
        if current is None or fraction < current["remaining_fraction"]:
            pools[pool] = {
                "label": f"{'Gemini' if pool == 'gemini' else 'Claude/GPT'} {bucket_label}",
                "remaining_fraction": float(fraction),
                "reset_time": info.get("resetTime"),
            }
    if not pools:
        raise QuotaError("GetUserStatus quota carried no recognizable models")
    plan_info = (status.get("planStatus") or {}).get("planInfo") or {}
    return {
        "origin": "live",
        "plan": plan_info.get("planDisplayName") or plan_info.get("planName"),
        "buckets": list(pools.values()),
        # Names the two pools that are missing rather than pointing at another
        # tool to read them. A note that sends the reader somewhere else is
        # only useful if they can go there.
        "note": (
            "two of four pools — the Gemini five-hour and Claude/GPT weekly "
            "limits are held by the vendor's cloud and are not readable locally"
        ),
    }


def _models_with_quota(node, out=None) -> list[tuple[str, dict]]:
    if out is None:
        out = []
    if isinstance(node, dict):
        quota_info = node.get("quotaInfo")
        if isinstance(quota_info, dict):
            label = (
                node.get("label")
                or node.get("displayName")
                or (node.get("modelOrAlias") or {}).get("model")
                or node.get("model")
                or ""
            )
            if label:
                out.append((str(label), quota_info))
        for child in node.values():
            _models_with_quota(child, out)
    elif isinstance(node, list):
        for child in node:
            _models_with_quota(child, out)
    return out
