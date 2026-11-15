#!/usr/bin/env python3
"""Detached per-directory timer delivery worker for tool-timer."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent.parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
_LIB_DIR = _TOOLS_DIR.parent.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from ar3.fsio import atomic_write_text, read_text_if_present  # noqa: E402
from ar3.locking import file_lock  # noqa: E402
from ar3.proc import (  # noqa: E402
    ERROR_ACCESS_DENIED,
    launch_options,
    pid_alive,
    process_start_token,
    terminate_verified_group,
    valid_start_token,
    verified_process_alive,
)
from ar3.ulid import new as new_ulid  # noqa: E402

import _runtime as rt  # noqa: E402
from timer.store import TimerStore, TimerStoreError  # noqa: E402

POLL_TICK_SECONDS = 0.2
SPAWN_CONFIRM_SECONDS = 1.5


def worker_running(store: TimerStore) -> bool:
    """True when `worker.lock` is held by a live worker process."""
    try:
        with file_lock(store.worker_lock_path, blocking=False):
            return False
    except BlockingIOError:
        return True
    except OSError:
        return False


def ensure_worker(
    store: TimerStore,
    seat: str,
    outbox_dir: Path | str | None = None,
) -> bool:
    """Ensure a background worker is running for `store.workdir`."""
    resolved_outbox = store.record_outbox(outbox_dir)
    if worker_running(store):
        return True
    if not store.has_pending():
        return False

    env = {
        key: val
        for key, val in os.environ.items()
        if key not in (rt.REPLY_DIR_ENV, rt.TURN_FILES_ENV)
    }
    env["PYTHONUTF8"] = "1"
    if resolved_outbox is not None:
        env["TELL_OUTBOX_DIR"] = str(resolved_outbox)

    argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--workdir",
        str(store.workdir),
        "--seat",
        seat,
    ]
    try:
        subprocess.Popen(
            argv,
            cwd=str(store.workdir),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **launch_options(new_session=True, breakaway=True),
        )
    except OSError as exc:
        if getattr(exc, "winerror", None) != ERROR_ACCESS_DENIED:
            raise
        subprocess.Popen(
            argv,
            cwd=str(store.workdir),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **launch_options(new_session=True),
        )

    deadline = time.monotonic() + SPAWN_CONFIRM_SECONDS
    while time.monotonic() < deadline:
        if worker_running(store) or not store.has_pending():
            return True
        time.sleep(0.01)
    return worker_running(store)


def stop_worker(store: TimerStore) -> bool:
    """Stop the background worker for `store.workdir`, keeping queued timers on disk.

    Signals `pid` only when `worker.lock` is held and `pid` positively matches a
    valid `start_token` in `worker.json` (re-verified before escalation).
    """
    raw = None
    try:
        raw = read_text_if_present(store.worker_info_path)
    except (OSError, UnicodeDecodeError):
        raw = None

    pid = 0
    stamped_token = None
    if raw:
        try:
            info = json.loads(raw)
            pid = int(info.get("pid", 0))
            stamped_token = info.get("start_token")
        except (ValueError, TypeError, AttributeError):
            pid = 0

    stop_marker = store.meta_dir / "worker.stop"
    was_running = worker_running(store)
    if was_running:
        try:
            store.meta_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(stop_marker, "stop\n")
        except OSError:
            pass
        if verified_process_alive(
            pid,
            stamped_token,
            liveness=pid_alive,
            token_reader=process_start_token,
        ):
            terminate_verified_group(
                pid,
                stamped_token,
                grace_seconds=0.3,
                liveness=pid_alive,
                token_reader=process_start_token,
            )

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and worker_running(store):
        time.sleep(0.02)

    if not worker_running(store):
        for stale in (store.worker_info_path, stop_marker):
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                pass
    return was_running


def _claim_next_due(
    store: TimerStore, now: float
) -> tuple[dict | None, str | None, float | None]:
    """Under `queue.lock`, claim one due timer or return `(None, None, next_due)`.

    When the queue is completely empty, returns `(None, None, None)` so the
    caller knows it can release `worker.lock` while still holding `queue.lock`.
    """
    timers = store._read_unlocked()
    if not timers:
        return None, None, None

    soonest: float | None = None
    for item in timers:
        if item.get("claiming") is not None:
            continue
        ready_at = max(
            float(item.get("due_at", 0.0)),
            float(item.get("next_attempt_at", 0.0)),
        )
        if ready_at <= now:
            claim = f"{now:.3f}-{os.getpid()}-{new_ulid()}"
            item["claiming"] = claim
            store._write_unlocked(timers)
            return dict(item), claim, 0.0
        if soonest is None or ready_at < soonest:
            soonest = ready_at
    return None, None, soonest


def run_worker(workdir: Path, seat: str = "timer") -> int:
    """Drain due timers in `workdir` until none remain or a stop signal arrives."""
    store = TimerStore(workdir)
    stop_flag = False

    def _on_stop(_signum: int, _frame: object) -> None:
        nonlocal stop_flag
        stop_flag = True

    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _on_stop)
            except (OSError, ValueError):
                pass

    wctx = file_lock(store.worker_lock_path, blocking=False)
    try:
        wctx.__enter__()
    except BlockingIOError:
        return 0
    wlocked = True

    stop_marker = store.meta_dir / "worker.stop"
    try:
        try:
            stop_marker.unlink(missing_ok=True)
        except OSError:
            pass
        own_token = process_start_token(os.getpid())
        if not valid_start_token(own_token):
            return 1
        info = {
            "pid": os.getpid(),
            "seat": seat,
            "start_token": own_token,
        }
        try:
            atomic_write_text(store.worker_info_path, json.dumps(info, indent=2) + "\n")
        except OSError:
            return 1

        try:
            store.clear_stale_claims()
        except TimerStoreError:
            return 1

        while not stop_flag and not stop_marker.exists():
            resolved_outbox = store.record_outbox(os.environ.get("TELL_OUTBOX_DIR"))
            if resolved_outbox is not None:
                os.environ["TELL_OUTBOX_DIR"] = str(resolved_outbox)
            os.environ.pop(rt.REPLY_DIR_ENV, None)

            qctx = file_lock(store.queue_lock_path)
            qctx.__enter__()
            try:
                now = time.time()
                entry, claim, next_ready = _claim_next_due(store, now)
                if entry is None and next_ready is None:
                    # Queue is empty: release worker.lock BEFORE queue.lock so a
                    # concurrent wake that writes under queue.lock always sees
                    # worker.lock free and starts a fresh worker.
                    try:
                        store.worker_info_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    wctx.__exit__(None, None, None)
                    wlocked = False
                    return 0
            finally:
                qctx.__exit__(None, None, None)

            if entry is not None and claim is not None:
                recipient = str(entry.get("sender", ""))
                body = str(entry.get("body", ""))
                attachments = store.attachment_paths(entry)
                rc = rt.send_reply(recipient, body, attachments)
                store.complete_delivery(str(entry["id"]), claim, delivered=(rc == 0))
                continue

            wait_for = POLL_TICK_SECONDS
            if next_ready is not None:
                wait_for = min(POLL_TICK_SECONDS, max(0.01, next_ready - time.time()))
            time.sleep(wait_for)
        return 0
    finally:
        if wlocked:
            try:
                store.worker_info_path.unlink(missing_ok=True)
            except OSError:
                pass
            wctx.__exit__(None, None, None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="timer-worker")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--seat", default="timer")
    args = parser.parse_args(argv)
    return run_worker(Path(args.workdir), seat=args.seat)


if __name__ == "__main__":
    sys.exit(main())
