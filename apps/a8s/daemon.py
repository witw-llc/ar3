"""a8s daemon — wake subprocess execution + per-agent attachment + signal handling.

This module owns the runtime engine:
- `acquire`/`release` manage exclusive pid-file attachment per agent.
- `run_with_prefix` spawns the wake subprocess in its own session group so
  SIGKILL can target the whole tree.
- `wake_once` processes one inbox message (with read-time wipe for CLEAR).
- `_settle_wake` acks (exit 0) or requeues-with-backoff every other outcome.
- `attached_loop` is the daemon body — handles 1+ agents in one process.

Module-level mutable state used by signal handlers:
  _STOP_EVENT          — set on 1st signal; checked in the loop body
  _SIGNAL_COUNT        — incremented per signal; 2 triggers force-kill
  _CURRENT_WAKE_PROC   — the currently-running wake subprocess (or None)
  _WAKE_*              — in-flight wake timing / completion callback
  _WAKE_STDOUT_*       — the reader thread + queue draining that wake's stdout
  _STUCK_WAKE_READERS  — reader threads a drain-timeout kill failed to free;
                         swept before every new spawn
  _LOOP_BEAT_*         — stamped at each `attached_loop` iteration top; read by
                         the watchdog thread to detect a wedged loop

A wake's stdout is read by a dedicated daemon thread
(`_wake_stdout_reader`) rather than the main loop, so a grandchild that
inherits the pipe's write end and never closes it (the alive-but-deaf
incident) blocks that thread forever instead of the runner. The main loop
only ever drains the thread's queue with a bounded deadline
(`_pump_wake_stdout_once` while running, `_drain_wake_stdout` after exit).
When the bounded drain times out, `_drain_wake_stdout_after_exit` terminates
the exited wake's whole process group — the grandchild holding the pipe dies
with it, on POSIX — and hands the reader thread to `_STUCK_WAKE_READERS` as a
backstop for the platforms/races where that doesn't free it.

`attached_loop` also sets `core.PRINT_LOCK` to a fresh Lock so `core.out` /
`core.out_agent` serialize log writes across threads.
"""
from __future__ import annotations

import json
import os
import queue
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time as _time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import core
from ar3 import clock
from ar3.proc import terminate_group
from settings import get_float, get_setting
from core import (
    MAX_WAKE_ATTEMPTS,
    Participant,
    TELL_OUTBOX_DIR_ENV,
    WAKE_RETRY_SCHEDULE,
    _pid_alive,
    _preview,
    agent_dir,
    detach_request_path,
    inbox_dir,
    kill_request_path,
    out_agent,
    pid_path,
    clear_inbox_waiting_since,
    clear_wake_retry,
    read_inbox_waiting_since,
    read_last_active,
    read_wake_retry,
    touch_inbox_waiting_since,
    touch_last_active,
    trash_dir,
    unique_path,
    write_wake_retry,
)
from definitions import (
    BatchEntry,
    batch_limit,
    build_batch_command,
    build_command,
    build_idle_command,
    files_ttl_seconds,
    has_batch_invoke,
    idle_timeout_seconds,
    is_file_proxy,
    load_agent_vars,
    load_definition,
    resolve_definition_path,
    max_wake_seconds,
    pause_seconds,
    wake_env,
    wrap_wake_argv,
)
from mailbox import (
    ensure_mailboxes,
    newest_inbox_mtime,
    next_inbox_message,
    peek_inbox_messages,
    route_outboxes,
)
from network import (
    load_remotes,
    load_services,
    make_publish_remotes,
    start_remotes,
    sweep_stale_claims,
    stop_remotes,
)
from registry import participants_from_registry, unresolved_mailboxes
import txlog


# ---------- subprocess execution ----------

# Set by wake subprocess helpers; read by _kill_wake_subprocess_group via the
# signal handler. _CURRENT_WAKE_NAME pairs with _CURRENT_WAKE_PROC so the
# SIGUSR1 kill-request handler can decide whether the in-flight wake is the one
# being killed.
_CURRENT_WAKE_PROC: subprocess.Popen | None = None
_CURRENT_WAKE_NAME: str | None = None
_WAKE_STARTED_MONO: float | None = None
_WAKE_MAX_SECONDS: float | None = None
_WAKE_ON_COMPLETE: Callable[[int | None], None] | None = None

# The reader thread + queue for the in-flight wake's stdout. One pair per
# wake, created in `_start_wake_subprocess` and cleared in `_clear_wake_state`.
_WAKE_STDOUT_THREAD: threading.Thread | None = None
_WAKE_STDOUT_QUEUE: "queue.Queue[str | object] | None" = None
# Sentinel the reader thread puts after its `for line in proc.stdout:` loop
# ends (clean EOF, or the ValueError/OSError from a stdout we closed on it).
_WAKE_STDOUT_DONE = object()

# Reader threads `_drain_wake_stdout_after_exit` couldn't free by terminating
# the exited wake's process group — Windows, where there is no group to
# target, or a POSIX straggler that survived the SIGKILL-window race.
# Appended there; swept in `_start_wake_subprocess` before every new spawn so
# the list only ever shrinks between spawns — a live entry names an actual
# stuck reader, never an accumulation of the list's own making.
_STUCK_WAKE_READERS: list[tuple[threading.Thread, str, float]] = []


def _wake_in_flight() -> bool:
    proc = _CURRENT_WAKE_PROC
    return proc is not None and proc.poll() is None


def _heartbeat_detail() -> str:
    if not _wake_in_flight():
        return "idle"
    started = _WAKE_STARTED_MONO
    elapsed = _time.monotonic() - started if started is not None else 0.0
    return f"wake in flight for {elapsed:.0f}s"


def _clear_wake_state(rc: int | None = None) -> None:
    global _CURRENT_WAKE_PROC, _CURRENT_WAKE_NAME
    global _WAKE_STARTED_MONO, _WAKE_MAX_SECONDS, _WAKE_ON_COMPLETE
    global _WAKE_STDOUT_THREAD, _WAKE_STDOUT_QUEUE
    _CURRENT_WAKE_PROC = None
    _CURRENT_WAKE_NAME = None
    _WAKE_STARTED_MONO = None
    _WAKE_MAX_SECONDS = None
    _WAKE_STDOUT_THREAD = None
    _WAKE_STDOUT_QUEUE = None
    on_complete = _WAKE_ON_COMPLETE
    _WAKE_ON_COMPLETE = None
    if on_complete is not None:
        on_complete(rc)


def _log_wake_line(name: str, line: str) -> None:
    ts = clock.time_stamp()
    out_agent(name, f"{name}> [{ts}] {line.rstrip(chr(10))}")


def _wake_stdout_reader(proc: subprocess.Popen, q: "queue.Queue[str | object]") -> None:
    """Runs for the lifetime of one wake, in its own daemon thread.

    Reading to EOF happens HERE, never in the main loop — a grandchild that
    inherits the pipe's write end and holds it open blocks this thread
    forever, but the main loop only ever drains `q` with a bounded deadline
    (`_pump_wake_stdout_once`, `_drain_wake_stdout`), so it can't wedge the
    runner. `proc.stdout.close()` from another thread (the bounded drain's
    timeout path, or the watchdog) surfaces here as ValueError or OSError."""
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            q.put(line)
    except (ValueError, OSError):
        pass
    finally:
        q.put(_WAKE_STDOUT_DONE)


def _pump_wake_stdout_once() -> None:
    """Non-blocking drain of whatever the reader thread has queued so far.
    Works identically on every platform — no `select` on a pipe, which raises
    OSError on Windows and silently produced no live output there."""
    name = _CURRENT_WAKE_NAME
    q = _WAKE_STDOUT_QUEUE
    if name is None or q is None:
        return
    while True:
        try:
            item = q.get_nowait()
        except queue.Empty:
            return
        if item is _WAKE_STDOUT_DONE:
            return
        _log_wake_line(name, item)


def _drain_wake_stdout(grace_seconds: float) -> bool:
    """Drain the queue until the reader thread reaches EOF or `grace_seconds`
    elapses. Returns True iff EOF was reached in time.

    EOF is detected by the reader thread's liveness, not by catching its
    sentinel in this function specifically — `_pump_wake_stdout_once` may
    already have consumed it moments earlier (it also drains the queue, and
    runs first on every iteration of the caller's poll loop), and a sentinel
    only ever gets put once. `thread.is_alive()` stays correct no matter
    which of the two consumed it.

    Called only after the wake process itself has exited — the case this
    guards against is a grandchild that inherited stdout's write end and
    kept it open, which leaves the reader thread blocked on a read that will
    never return. Polling with a small timeout against a monotonic deadline
    is what keeps this bounded no matter what the subprocess tree does with
    the pipe."""
    name = _CURRENT_WAKE_NAME
    q = _WAKE_STDOUT_QUEUE
    thread = _WAKE_STDOUT_THREAD
    if name is None or q is None:
        return True
    deadline = _time.monotonic() + grace_seconds
    while True:
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            return thread is None or not thread.is_alive()
        try:
            item = q.get(timeout=min(remaining, 0.1))
        except queue.Empty:
            item = None
        if item is not None and item is not _WAKE_STDOUT_DONE:
            _log_wake_line(name, item)
            continue
        if item is _WAKE_STDOUT_DONE:
            return True
        if thread is None or not thread.is_alive():
            return True


def _check_wake_timeout() -> None:
    if not _wake_in_flight():
        return
    if _WAKE_MAX_SECONDS is None or _WAKE_STARTED_MONO is None:
        return
    if _time.monotonic() - _WAKE_STARTED_MONO < _WAKE_MAX_SECONDS:
        return
    name = _CURRENT_WAKE_NAME or "?"
    ts = clock.time_stamp()
    out_agent(
        name,
        f"{name}> [{ts}] max wake time ({_WAKE_MAX_SECONDS:g}s) exceeded — killing",
    )
    _kill_wake_subprocess_group()


def _diagnose_stuck_wake_stdout(name: str) -> None:
    ts = clock.time_stamp()
    out_agent(
        name,
        f"{name}> [{ts}] wake exited but its stdout stayed open — suspected "
        "inherited handle (a grandchild holding the write end); terminating "
        "its process group",
    )


def _close_wake_stdout_in_background(proc: subprocess.Popen) -> None:
    """Fire-and-forget close of `proc.stdout`, off the calling thread.

    `TextIOWrapper.close()` shares an internal lock with whatever thread is
    mid-`read()` on the same object — calling it synchronously from the
    runner while the reader thread is blocked inside that read (exactly the
    inherited-pipe case this exists for) makes `close()` itself block until
    the read eventually returns, which reintroduces the unbounded wait this
    whole mechanism is meant to remove. A background daemon thread absorbs
    that wait instead of the runner."""
    def close() -> None:
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except (OSError, ValueError):
            pass

    threading.Thread(target=close, daemon=True).start()


def _drain_wake_stdout_after_exit(name: str) -> None:
    """Bounded post-exit drain, called once the wake process itself has
    exited. If the reader thread's EOF sentinel doesn't show up within
    `wake_drain_grace_seconds`, something other than our own wake is still
    holding the write end open — and since the wake itself already exited,
    anything still alive in its process group is turn litter, not a live
    turn. Terminate the group the same way the max-wake-time kill does at
    turn boundaries: on POSIX that closes the write end, the reader thread
    hits EOF, and the pipe closes with it. `terminate_group` handles an
    already-exited leader by design (falls back to pgid=pid), and a
    properly-detached child (its own session — the p0o supervisor design) is
    outside the group by construction and untouched.

    That kill is not a guarantee — Windows has no group to target, and a
    POSIX straggler can still survive the SIGKILL-window race — so the
    runner's own end is also closed in the background as before (belt, not a
    wait), and a reader thread still alive after both is handed to
    `_STUCK_WAKE_READERS` for the retention sweep to keep reporting until it
    dies on its own."""
    if _drain_wake_stdout(get_float("wake_drain_grace_seconds")):
        return
    _diagnose_stuck_wake_stdout(name)
    proc = _CURRENT_WAKE_PROC
    if proc is not None:
        terminate_group(proc)
        _close_wake_stdout_in_background(proc)
    thread = _WAKE_STDOUT_THREAD
    if thread is not None and thread.is_alive():
        _STUCK_WAKE_READERS.append((thread, name, _time.monotonic()))


def _finish_wake_if_done() -> None:
    proc = _CURRENT_WAKE_PROC
    name = _CURRENT_WAKE_NAME
    if proc is None or name is None:
        return
    _pump_wake_stdout_once()
    rc = proc.poll()
    if rc is None:
        return
    _drain_wake_stdout_after_exit(name)
    if rc != 0:
        ts = clock.time_stamp()
        out_agent(name, f"{name}> [{ts}] (exit {rc})")
    try:
        proc.wait(timeout=0)
    except subprocess.TimeoutExpired:
        pass
    _clear_wake_state(rc)


def _service_in_flight_wake() -> None:
    if _CURRENT_WAKE_PROC is None:
        return
    _pump_wake_stdout_once()
    _check_wake_timeout()
    _finish_wake_if_done()


def _sweep_stuck_wake_readers() -> None:
    """Drop reader threads that have since died — their grandchild finally
    exited, or a delayed kill landed — and report whatever remains. Runs
    once per new wake spawn rather than on a timer: the backstop only needs
    to notice before entries pile up, not in real time, and a spawn is
    already the moment a fresh reader thread and pipe are about to exist
    alongside whatever is still stuck from a prior one."""
    global _STUCK_WAKE_READERS
    alive = [entry for entry in _STUCK_WAKE_READERS if entry[0].is_alive()]
    if alive:
        oldest = min(ts for _, _, ts in alive)
        core.out(
            f"[a8s] {len(alive)} stuck wake stdout reader(s) from a prior "
            f"turn (oldest {_time.monotonic() - oldest:.0f}s)"
        )
    _STUCK_WAKE_READERS = alive


def _start_wake_subprocess(
    name: str,
    cmd: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
    max_seconds: float | None = None,
    on_complete: Callable[[int | None], None] | None = None,
) -> bool:
    """Start a wake subprocess. Returns True iff the process was spawned.

    `on_complete` fires from `_clear_wake_state` with the subprocess exit code
    once the wake finishes. It does NOT fire when the spawn itself fails —
    callers see that as a False return and settle the delivery themselves."""
    global _CURRENT_WAKE_PROC, _CURRENT_WAKE_NAME
    global _WAKE_STARTED_MONO, _WAKE_MAX_SECONDS, _WAKE_ON_COMPLETE
    global _WAKE_STDOUT_THREAD, _WAKE_STDOUT_QUEUE
    if _wake_in_flight():
        return False
    _sweep_stuck_wake_readers()
    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=proc_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
    except FileNotFoundError:
        ts = clock.time_stamp()
        out_agent(name, f"{name}> [{ts}] command not found: {cmd[0]}")
        return False
    stdout_queue: "queue.Queue[str | object]" = queue.Queue()
    stdout_thread = threading.Thread(
        target=_wake_stdout_reader, args=(proc, stdout_queue), daemon=True
    )
    stdout_thread.start()
    _CURRENT_WAKE_PROC = proc
    _CURRENT_WAKE_NAME = name
    _WAKE_STARTED_MONO = _time.monotonic()
    _WAKE_MAX_SECONDS = max_seconds
    _WAKE_ON_COMPLETE = on_complete
    _WAKE_STDOUT_THREAD = stdout_thread
    _WAKE_STDOUT_QUEUE = stdout_queue
    return True


def run_with_prefix(
    name: str,
    cmd: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
    max_seconds: float | None = None,
    on_complete: Callable[[int | None], None] | None = None,
    on_start: Callable[[int], None] | None = None,
) -> int:
    """Run the wake subprocess in its own session so SIGKILL can target the
    whole process group (LLM CLI + any helpers it spawns). Tracks the live
    process in `_CURRENT_WAKE_PROC` and the agent in `_CURRENT_WAKE_NAME` so
    signal handlers can identify which agent's wake is in-flight.

    `on_start` fires once, right after the subprocess is confirmed spawned,
    with its pid — the sync wake path's only way to log the pid, since it has
    no access to `_CURRENT_WAKE_PROC` before calling in."""
    if not _start_wake_subprocess(
        name, cmd, cwd, env=env, max_seconds=max_seconds, on_complete=on_complete
    ):
        if on_complete is not None:
            on_complete(None)
        return 127
    proc = _CURRENT_WAKE_PROC
    assert proc is not None and proc.stdout is not None
    if on_start is not None:
        on_start(proc.pid)
    started = _WAKE_STARTED_MONO or _time.monotonic()
    exited: int | None = None
    try:
        while True:
            if max_seconds is not None and _time.monotonic() - started >= max_seconds:
                ts = clock.time_stamp()
                out_agent(
                    name,
                    f"{name}> [{ts}] max wake time ({max_seconds:g}s) exceeded — killing",
                )
                _kill_wake_subprocess_group()
            _pump_wake_stdout_once()
            rc = proc.poll()
            if rc is not None:
                _drain_wake_stdout_after_exit(name)
                proc.wait()
                if rc != 0:
                    ts = clock.time_stamp()
                    out_agent(name, f"{name}> [{ts}] (exit {rc})")
                exited = rc
                return rc
            _time.sleep(0.05)
    finally:
        if _CURRENT_WAKE_PROC is not None:
            _clear_wake_state(exited)


def _tell_outbox_env(p: Participant) -> dict[str, str]:
    from core import TELL_FILE_MAX_ENV
    from settings import get_int

    return {
        TELL_OUTBOX_DIR_ENV: str(p.outbox_path()),
        TELL_FILE_MAX_ENV: str(get_int("max_file_bytes")),
    }


def _wake_env(p: Participant, definition: dict) -> dict[str, str]:
    """The layer `_start_wake_subprocess` puts over its own environment.

    Declared node env underneath, routing variables on top: an operator who
    writes `TELL_OUTBOX_DIR` into `definition.env` still gets the outbox a8s
    routes to, because a node that answers into someone else's outbox is worse
    than a node that does not answer.
    """
    return {**wake_env(definition), **_tell_outbox_env(p)}


def _deliver_file_proxy(p: Participant) -> None:
    """Move ALL inbox files to the agent's file-proxy inbox dir."""
    dest = p.inbox_path()
    dest.mkdir(parents=True, exist_ok=True)
    src = inbox_dir(p.name)
    if not src.is_dir():
        return
    for f in sorted(src.iterdir()):
        if not (f.is_file() and f.name.endswith(".json")):
            continue
        target = dest / f.name
        shutil.move(str(f), str(target))
        out_agent(p.name, f"[{p.name}] proxy: delivered {f.name}")
        try:
            envelope = json.loads(target.read_text(encoding="utf-8"))
            file_names = [e.get("filename", "") for e in (envelope.get("files") or []) if e.get("filename")]
            txlog.log("PROXY_DELIVERED", msg_id=envelope.get("id", f.stem), sender=envelope.get("from", ""), recipient=p.name, files=file_names or None, detail=_preview(envelope.get("content", "")))
        except (json.JSONDecodeError, OSError):
            txlog.log("PROXY_DELIVERED", msg_id=f.stem, recipient=p.name)


def _pause_ready_for_wake(
    p: Participant,
    definition: dict,
    *,
    now: datetime | None = None,
) -> bool:
    """Trailing-edge debounce: ready when the inbox has been quiet for `pause`
    seconds, or when depth reaches `batch_limit` (escape hatch). Zero pause
    means immediate readiness. `inbox_waiting_since` is only a once-per-burst
    log marker — readiness comes from newest inbox mtime."""
    pause = pause_seconds(definition)
    if pause <= 0:
        return True
    if now is None:
        now = datetime.now(timezone.utc)
    limit = batch_limit(definition)
    depth = len(peek_inbox_messages(p, limit))
    if depth >= limit:
        clear_inbox_waiting_since(p.name)
        out_agent(p.name, f"[{p.name}] {depth} waiting at the limit, waking now")
        return True
    newest = newest_inbox_mtime(p)
    if newest is None:
        clear_inbox_waiting_since(p.name)
        return True
    if (now - newest).total_seconds() >= pause:
        clear_inbox_waiting_since(p.name)
        return True
    if read_inbox_waiting_since(p.name) is None:
        touch_inbox_waiting_since(p.name, now)
        out_agent(p.name, f"[{p.name}] pause {pause:g}s before wake")
    return False


def _settle_wake(
    p: Participant,
    envelopes: list[Path],
    rc: int | None,
    *,
    reason: str | None = None,
) -> None:
    """Ack or requeue the envelopes a wake consumed.

    Exit 0 is the only ack: the envelopes stay in trash and the agent's retry
    record clears. Any other outcome — nonzero exit, timeout kill, failed spawn,
    unexpanded vars — moves them back into the inbox and arms a per-agent
    backoff so the next attempt waits instead of hot-looping. Once
    MAX_WAKE_ATTEMPTS attempts have failed they stay in trash as dead letters,
    logged and recorded in the transaction log, so a poison envelope can't block
    the inbox forever."""
    return_detail = f"exit {rc}" if rc is not None else (reason or "spawn failed")
    for f in envelopes:
        txlog.log("WAKE_RETURN", msg_id=f.stem, recipient=p.name, detail=return_detail)
    if rc == 0:
        clear_wake_retry(p.name)
        return
    if reason is None:
        reason = "spawn failed" if rc is None else f"exit {rc}"
    unit = sorted(f.name for f in envelopes)
    record = read_wake_retry(p.name) or {}
    attempts = (record.get("attempts", 0) if record.get("unit") == unit else 0) + 1

    if attempts >= MAX_WAKE_ATTEMPTS:
        clear_wake_retry(p.name)
        for f in envelopes:
            out_agent(
                p.name,
                f"[{p.name}] dead letter after {attempts} failed wakes "
                f"({reason}): {f.name} left in trash",
            )
            txlog.log(
                "DROPPED",
                msg_id=f.stem,
                recipient=p.name,
                detail=f"wake failed {attempts}x ({reason}); envelope left in trash",
            )
        return

    inbox = inbox_dir(p.name)
    inbox.mkdir(parents=True, exist_ok=True)
    requeued = 0
    for f in envelopes:
        try:
            f.rename(inbox / f.name)
        except OSError as e:
            out_agent(p.name, f"[{p.name}] requeue failed for {f.name}: {e}")
            continue
        requeued += 1
    if not requeued:
        return
    delay = WAKE_RETRY_SCHEDULE[attempts - 1]
    write_wake_retry(
        p.name, unit, attempts, datetime.now(timezone.utc) + timedelta(seconds=delay)
    )
    out_agent(
        p.name,
        f"[{p.name}] wake failed ({reason}) — requeued {requeued}; "
        f"retry in {delay}s (attempt {attempts + 1}/{MAX_WAKE_ATTEMPTS})",
    )


def _wake_retry_ready(name: str, *, now: datetime | None = None) -> bool:
    """False while a failed wake's backoff is still running. An unreadable or
    unparseable record reads as ready — a corrupt sidecar must not wedge an
    agent's inbox shut."""
    record = read_wake_retry(name)
    if not record:
        return True
    raw = record.get("next_at")
    if not isinstance(raw, str):
        return True
    try:
        next_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (now or datetime.now(timezone.utc)) >= next_at


def _wake_completion(
    p: Participant, envelopes: list[Path]
) -> Callable[[int | None], None]:
    def complete(rc: int | None) -> None:
        touch_last_active(p.name)
        _settle_wake(p, envelopes, rc)

    return complete


def wake_once(p: Participant, msg_path: Path, *, async_wake: bool = False) -> bool:
    # Mark activity before any work — covers the parse-error / load-error
    # exits below too. Without this, a bad inbox file in the only handled
    # agent could let an idle invoke fire on the same iteration.
    touch_last_active(p.name)
    try:
        with msg_path.open("r", encoding="utf-8") as f:
            msg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        out_agent(p.name, f"[{p.name}] inbox parse error on {msg_path.name}: {e}")
        bad = unique_path(trash_dir(p.name) / msg_path.name)
        msg_path.rename(bad)
        return False

    try:
        definition = load_definition(p.name)
    except (FileNotFoundError, RuntimeError) as e:
        out_agent(p.name, f"[{p.name}] {e}")
        bad = unique_path(trash_dir(p.name) / msg_path.name)
        msg_path.rename(bad)
        return False

    if is_file_proxy(definition):
        _deliver_file_proxy(p)
        touch_last_active(p.name)
        return False

    trashed = unique_path(trash_dir(p.name) / msg_path.name)
    msg_path.rename(trashed)
    out_agent(p.name, f"[{p.name}] waking from {trashed.name}: {_preview(msg.get('content', ''))}")
    p.files_path().mkdir(parents=True, exist_ok=True)
    try:
        cmd = wrap_wake_argv(definition, build_command(
            definition,
            msg,
            p.files_path(),
            resolve_definition_path(p.name),
            vars=load_agent_vars(p.name),
        ))
        spawn_env = _wake_env(p, definition)
    except ValueError as e:
        out_agent(p.name, f"[{p.name}] wake aborted: {e}")
        _settle_wake(p, [trashed], None, reason=str(e))
        return False
    out_agent(p.name, f"[{p.name}] exec: {shlex.join(cmd)}")
    max_sec = max_wake_seconds(definition)
    complete = _wake_completion(p, [trashed])
    if async_wake:
        started = _start_wake_subprocess(
            p.name,
            cmd,
            p.root,
            env=spawn_env,
            max_seconds=max_sec,
            on_complete=complete,
        )
        if started and _CURRENT_WAKE_PROC is not None:
            txlog.log(
                "WAKE_START",
                msg_id=trashed.stem,
                recipient=p.name,
                detail=f"wake pid={_CURRENT_WAKE_PROC.pid}",
            )
        if not started:
            complete(None)
        return started
    run_with_prefix(
        p.name,
        cmd,
        p.root,
        env=spawn_env,
        max_seconds=max_sec,
        on_complete=complete,
        on_start=lambda pid: txlog.log(
            "WAKE_START", msg_id=trashed.stem, recipient=p.name, detail=f"wake pid={pid}"
        ),
    )
    return False


def wake_batch(
    p: Participant,
    msg_paths: list[Path],
    definition: dict,
    *,
    async_wake: bool = False,
) -> bool:
    touch_last_active(p.name)
    if is_file_proxy(definition):
        _deliver_file_proxy(p)
        touch_last_active(p.name)
        return False

    trashed: list[Path] = []
    previews: list[str] = []
    entries: list[BatchEntry] = []
    for msg_path in msg_paths:
        try:
            with msg_path.open("r", encoding="utf-8") as f:
                msg = json.load(f)
            previews.append(_preview(msg.get("content", "")))
            entries.append(BatchEntry(msg, msg_path.name))
        except (OSError, json.JSONDecodeError) as e:
            previews.append(msg_path.name)
            entries.append(BatchEntry(None, msg_path.name, str(e)))
        dest = unique_path(trash_dir(p.name) / msg_path.name)
        msg_path.rename(dest)
        trashed.append(dest)

    summary = "; ".join(previews[:3])
    if len(previews) > 3:
        summary += f"; +{len(previews) - 3} more"
    out_agent(
        p.name,
        f"[{p.name}] batch waking ({len(trashed)}): {summary}",
    )
    p.files_path().mkdir(parents=True, exist_ok=True)
    try:
        cmd = wrap_wake_argv(definition, build_batch_command(
            definition,
            p.name,
            entries,
            resolve_definition_path(p.name),
            vars=load_agent_vars(p.name),
        ))
        spawn_env = _wake_env(p, definition)
    except ValueError as e:
        out_agent(p.name, f"[{p.name}] batch wake aborted: {e}")
        _settle_wake(p, trashed, None, reason=str(e))
        return False
    out_agent(p.name, f"[{p.name}] batch exec: {shlex.join(cmd)}")
    max_sec = max_wake_seconds(definition)
    complete = _wake_completion(p, trashed)

    def log_batch_start(pid: int) -> None:
        for f in trashed:
            txlog.log(
                "WAKE_START",
                msg_id=f.stem,
                recipient=p.name,
                detail=f"wake pid={pid} batch={len(trashed)}",
            )

    if async_wake:
        started = _start_wake_subprocess(
            p.name,
            cmd,
            p.root,
            env=spawn_env,
            max_seconds=max_sec,
            on_complete=complete,
        )
        if started and _CURRENT_WAKE_PROC is not None:
            log_batch_start(_CURRENT_WAKE_PROC.pid)
        if not started:
            complete(None)
        return started
    run_with_prefix(
        p.name,
        cmd,
        p.root,
        env=spawn_env,
        max_seconds=max_sec,
        on_complete=complete,
        on_start=log_batch_start,
    )
    return False


def _file_proxy_ttl_cleanup(p: Participant, definition: dict) -> None:
    """Delete files in the agent files dir older than files_ttl_hours."""
    ttl = files_ttl_seconds(definition)
    files_path = p.files_path()
    if not files_path.is_dir():
        return
    cutoff = _time.time() - ttl
    removed = 0
    for entry in files_path.iterdir():
        try:
            mtime = os.path.getmtime(entry)
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        try:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            elif entry.is_file():
                entry.unlink()
            removed += 1
        except OSError:
            pass
    if removed:
        out_agent(p.name, f"[{p.name}] proxy: TTL cleanup removed {removed} file(s)")


def maybe_run_idle(p: Participant, *, async_wake: bool = False) -> bool:
    """If the agent has `definition.idle.invoke` configured AND has been
    idle for at least `definition.idle.timeout` seconds, run the configured
    argv via the wake subprocess machinery and refresh `last-active`. Returns
    True iff an idle invoke fired this call. Errors loading the definition are
    logged and swallowed — idle never crashes the loop."""
    try:
        definition = load_definition(p.name)
    except (FileNotFoundError, RuntimeError):
        return False
    timeout = idle_timeout_seconds(definition)
    if timeout is None:
        return False

    if is_file_proxy(definition):
        last = read_last_active(p.name)
        if last is None:
            touch_last_active(p.name)
            return False
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        if elapsed < timeout:
            return False
        _deliver_file_proxy(p)
        _file_proxy_ttl_cleanup(p, definition)
        touch_last_active(p.name)
        return True

    last = read_last_active(p.name)
    if last is None:
        # No prior activity recorded — initialize and let the next iteration
        # start the clock fresh.
        touch_last_active(p.name)
        return False
    elapsed = (datetime.now(timezone.utc) - last).total_seconds()
    if elapsed < timeout:
        return False
    try:
        cmd = build_idle_command(
            definition,
            p.name,
            resolve_definition_path(p.name),
            vars=load_agent_vars(p.name),
        )
        if cmd is None:
            return False
        cmd = wrap_wake_argv(definition, cmd)
        spawn_env = _wake_env(p, definition)
    except ValueError as e:
        out_agent(p.name, f"[{p.name}] idle aborted: {e}")
        return False
    out_agent(
        p.name,
        f"[{p.name}] idle {int(elapsed)}s ≥ {int(timeout)}s — firing idle invoke",
    )
    out_agent(p.name, f"[{p.name}] idle exec: {shlex.join(cmd)}")
    max_sec = max_wake_seconds(definition)
    try:
        if async_wake:
            return _start_wake_subprocess(
                p.name,
                cmd,
                p.root,
                env=spawn_env,
                max_seconds=max_sec,
                on_complete=lambda _rc: touch_last_active(p.name),
            )
        run_with_prefix(
            p.name, cmd, p.root, env=spawn_env, max_seconds=max_sec
        )
    finally:
        if not async_wake:
            touch_last_active(p.name)
    return True


# ---------- per-agent attachment ----------

def _read_handler_pid(name: str) -> int | None:
    """Return the live PID currently handling <name>, or None. Cleans up stale
    pid files. Treats empty / non-int / non-positive contents as stale (the
    O_CREAT|O_EXCL window allows a partial-write to leave an empty pid file
    if the writer dies before `os.write`; non-positive values don't refer to
    any real process — `os.kill(0, ...)` would target the whole process group)."""
    p = pid_path(name)
    if not p.is_file():
        return None
    try:
        pid = int(p.read_text().strip())
        if pid <= 0:
            raise ValueError("non-positive pid")
    except (OSError, ValueError):
        try:
            p.unlink()
        except OSError:
            pass
        return None
    if _pid_alive(pid):
        return pid
    try:
        p.unlink()
    except OSError:
        pass
    return None


def _try_atomic_claim(name: str, pid: int) -> bool:
    """Attempt to write `pid` into `pid_path(name)` using O_CREAT|O_EXCL.
    Returns True iff this process now holds the handler attachment.

    `os.fsync` after the write makes the pid bytes durable before the fd
    closes — without it, a kernel-level crash window between create and write
    could leave readers parsing an empty file (which `_read_handler_pid` now
    treats as stale and cleans up)."""
    p = pid_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    try:
        os.write(fd, str(pid).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


# How long to wait for the holder to honor a detach-request before giving up.
# Long enough to cover an in-flight LLM wake (the holder only checks the
# request between iterations, so an active subprocess delays response).
DETACH_TIMEOUT_S = 60.0
DETACH_POLL_S = 0.2


def _write_detach_request(name: str, requester_pid: int) -> None:
    """Write `requester_pid` into the detach-request file for `name` (overwrites
    any prior request — last writer wins, which is fine since whichever
    requester is the most recent will get the agent next)."""
    p = detach_request_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(requester_pid))


def _read_detach_request(name: str) -> int | None:
    """Return the requester pid in the detach-request file for `name`, or None.
    Reaps malformed contents (empty / non-int / non-positive) and stale
    requests from dead requesters — without the liveness check, an
    `acquire()` caller that crashes after writing the request would cause
    the holder's next iteration to release the agent to nobody."""
    p = detach_request_path(name)
    if not p.is_file():
        return None
    try:
        pid = int(p.read_text().strip())
        if pid <= 0:
            raise ValueError("non-positive pid")
    except (OSError, ValueError):
        try:
            p.unlink()
        except OSError:
            pass
        return None
    if _pid_alive(pid):
        return pid
    try:
        p.unlink()
    except OSError:
        pass
    return None


def _clear_detach_request(name: str) -> None:
    """Best-effort unlink of the detach-request file."""
    try:
        detach_request_path(name).unlink()
    except OSError:
        pass


def _write_kill_request(name: str, requester_pid: int) -> None:
    p = kill_request_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(requester_pid))


def _read_kill_request(name: str) -> int | None:
    """Same parse-and-reap discipline as `_read_detach_request`, including
    the dead-requester reap."""
    p = kill_request_path(name)
    if not p.is_file():
        return None
    try:
        pid = int(p.read_text().strip())
        if pid <= 0:
            raise ValueError("non-positive pid")
    except (OSError, ValueError):
        try:
            p.unlink()
        except OSError:
            pass
        return None
    if _pid_alive(pid):
        return pid
    try:
        p.unlink()
    except OSError:
        pass
    return None


def _clear_kill_request(name: str) -> None:
    try:
        kill_request_path(name).unlink()
    except OSError:
        pass


def acquire(name: str) -> None:
    """Attach this process as the handler of <name>.

    If another live process holds <name>, write a detach-request file and
    poll for it to release. The holder's `attached_loop` checks the request
    at the top of each iteration and releases just <name> (not its other
    handled agents) — so a multi-agent handler losing one member keeps
    serving the rest. Raises `TimeoutError` if the holder doesn't honor
    the request within `DETACH_TIMEOUT_S` (typically because an in-flight
    LLM wake is taking a long time; `a8s kill <name>` breaks the deadlock).

    Stale pid files (writer dead) are reaped by `_read_handler_pid` and
    the claim retried."""
    me = os.getpid()
    requested = False
    deadline: float | None = None
    while True:
        if _try_atomic_claim(name, me):
            # If the pending request was OURS (we placed it earlier in this
            # call), clear it — it's been satisfied. Leave foreign requests
            # alone: those belong to whichever process placed them, and our
            # next iteration as the new holder will honor them.
            if _read_detach_request(name) == me:
                _clear_detach_request(name)
            return
        existing = _read_handler_pid(name)
        if existing is None:
            continue  # stale; retry the claim
        if existing == me:
            return
        if not requested:
            _write_detach_request(name, me)
            sys.stderr.write(
                f"[a8s] {name}: requesting release from PID {existing}...\n"
            )
            sys.stderr.flush()
            requested = True
            deadline = _time.time() + DETACH_TIMEOUT_S
        if deadline is not None and _time.time() >= deadline:
            _clear_detach_request(name)
            raise TimeoutError(
                f"PID {existing} did not release {name} within {DETACH_TIMEOUT_S}s — "
                f"try `a8s kill {name}`"
            )
        _time.sleep(DETACH_POLL_S)


def release(name: str) -> None:
    """Unlink the pid file iff it points at our pid. Safe to call repeatedly.
    Also clears any pending detach-request for `name` since the request was
    aimed at the now-released attachment."""
    p = pid_path(name)
    try:
        if p.is_file():
            pid = int(p.read_text().strip())
            if pid == os.getpid():
                p.unlink()
    except (OSError, ValueError):
        pass
    _clear_detach_request(name)


# ---------- attached loop (daemon body for 1+ agents) ----------

# Set when an attached loop is running. The signal handler closes over them.
_STOP_EVENT: threading.Event | None = None
_SIGNAL_COUNT = 0


def _kill_wake_subprocess_group() -> None:
    """SIGTERM-then-SIGKILL the current wake's subprocess group. Targets the
    whole process tree on POSIX so the LLM CLI dies along with our wake
    wrapper; on Windows, where there is no process group to target,
    `ar3.proc.terminate_group` falls back to a plain SIGTERM of the wake
    process itself. Delegated to the foundation rather than reimplemented
    here — `os.getpgid`/`os.killpg` don't exist on `nt`, and this helper now
    runs from the iteration-top kill-request branch on every platform, not
    only from the POSIX-only SIGUSR1 handler."""
    proc = _CURRENT_WAKE_PROC
    if proc is None or proc.poll() is not None:
        return
    terminate_group(proc)


# ---------- alive-but-deaf watchdog ----------

# Stamped at the top of every `attached_loop` iteration. `_LOOP_BEAT_MONO`
# drives the stale check; `_LOOP_BEAT_WALL` is only for the human-readable
# alert if recovery fails.
_LOOP_BEAT_MONO: float | None = None
_LOOP_BEAT_WALL: datetime | None = None


def _oldest_inbox_age(names: list[str]) -> float | None:
    """Seconds since the oldest file in any of `names`' inboxes was last
    modified, or None if every inbox is empty. Scans defensively — a file
    disappearing mid-scan (routed, drained, or picked up by a wake) is not
    an error here."""
    now = _time.time()
    oldest_mtime: float | None = None
    for name in list(names):
        try:
            d = inbox_dir(name)
            if not d.is_dir():
                continue
            entries = list(d.iterdir())
        except OSError:
            continue
        for f in entries:
            try:
                if not f.is_file():
                    continue
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if oldest_mtime is None or mtime < oldest_mtime:
                oldest_mtime = mtime
    if oldest_mtime is None:
        return None
    return now - oldest_mtime


def _close_wake_stdout_for_watchdog() -> None:
    proc = _CURRENT_WAKE_PROC
    if proc is None:
        return
    _close_wake_stdout_in_background(proc)


def _attempt_wedge_recovery(
    label: str, last_beat_mono: float, oldest_age: float, wedge_seconds: float
) -> None:
    """Unblock a runner whose loop has stopped beating while mail is waiting.

    A stale loop beat is not by itself a wedge: `route_outboxes` does
    synchronous storage uploads / remote publishes before servicing the
    in-flight wake, so one slow upload can hold an iteration past
    `wedge_seconds` with a perfectly healthy LLM turn running underneath it.
    Detection snapshots the wake state once, up front, and branches on it
    before touching anything:

      - wake alive, still inside its budget (`max_wake_seconds`, or no budget
        set at all): an active turn. Nothing is closed or killed — only
        logged — because closing stdout under a live turn is exactly the
        failure this function must never cause.
      - wake alive, past its budget: the loop that would normally enforce
        `max_wake_seconds` (`_check_wake_timeout`, which only ever runs from
        inside the loop) is itself the thing that's stale, so recovery kills
        the wake's process group in its place.
      - wake exited but its state was never cleared (this beat never reached
        `_finish_wake_if_done`, so the post-exit drain never ran): close the
        runner's own end of stdout and terminate the process group, same as
        the drain-timeout path in `_drain_wake_stdout_after_exit`.
      - no wake in flight at all: nothing to close or kill; alert-only.

    Scope is exactly the current wake's own process group — nothing else on
    the machine is ever touched, and a wake that used its own detached session
    (outside `run_with_prefix`'s tracked subprocess — the p0o supervisor
    design) is out of bounds by construction: there is nothing here to reach
    it with."""
    proc = _CURRENT_WAKE_PROC
    started_mono = _WAKE_STARTED_MONO
    max_seconds = _WAKE_MAX_SECONDS
    ts = clock.time_stamp()

    if proc is not None and proc.poll() is None:
        elapsed = _time.monotonic() - started_mono if started_mono is not None else 0.0
        if max_seconds is None or elapsed < max_seconds:
            detail = (
                f"loop beat stale ({wedge_seconds:g}s+) during active turn "
                f"({elapsed:.0f}s in flight); not intervening"
            )
            txlog.log("WEDGE", sender=label, detail=detail)
            out_agent(label, f"{label}> [{ts}] watchdog: {detail}")
            return
        out_agent(
            label,
            f"{label}> [{ts}] watchdog: loop beat stale ({wedge_seconds:g}s+) and "
            f"oldest queued message is {oldest_age:.0f}s old — wake has run "
            f"{elapsed:.0f}s, past its {max_seconds:g}s budget with no loop "
            "left to enforce it — killing",
        )
        _kill_wake_subprocess_group()
    elif proc is not None:
        out_agent(
            label,
            f"{label}> [{ts}] watchdog: loop beat stale ({wedge_seconds:g}s+) and "
            f"oldest queued message is {oldest_age:.0f}s old — wake already "
            "exited but its stdout stayed open; closing it and terminating "
            "its process group",
        )
        _close_wake_stdout_for_watchdog()
        try:
            terminate_group(proc)
        except (OSError, ValueError):
            pass
    else:
        out_agent(
            label,
            f"{label}> [{ts}] watchdog: loop beat stale ({wedge_seconds:g}s+) and "
            f"oldest queued message is {oldest_age:.0f}s old — no wake in "
            "flight; attempting recovery",
        )

    deadline = _time.monotonic() + 30.0
    while _time.monotonic() < deadline:
        if _STOP_EVENT is not None and _STOP_EVENT.is_set():
            return
        if _LOOP_BEAT_MONO is not None and _LOOP_BEAT_MONO != last_beat_mono:
            txlog.log(
                "WEDGE",
                sender=label,
                detail=(
                    f"recovered: loop beat resumed within 30s "
                    f"(oldest queued message had aged {oldest_age:.0f}s)"
                ),
            )
            out_agent(label, f"{label}> [{clock.time_stamp()}] watchdog: recovered")
            return
        _time.sleep(0.2)

    last_beat_str = _LOOP_BEAT_WALL.isoformat() if _LOOP_BEAT_WALL is not None else "unknown"
    detail = (
        f"recovery failed: {label} still unresponsive; oldest queued message "
        f"{oldest_age:.0f}s old; last beat {last_beat_str}"
    )
    txlog.log("WEDGE", sender=label, detail=detail)
    out_agent(
        label,
        f"{label}> [{clock.time_stamp()}] watchdog ALERT: {label} unresponsive — "
        f"oldest queued message {oldest_age:.0f}s old, last beat {last_beat_str}",
    )


def _watchdog_loop(names: list[str], label: str) -> None:
    """Runs in its own daemon thread for the life of one resident
    `attached_loop`. Watches three clocks: the loop's own beat
    (`_LOOP_BEAT_MONO`), the wake state, and the oldest undispatched inbox
    message. Wedged means the loop beat is stale AND an addressed inbox
    message is at least as stale — a quiet agent with no mail waiting is not
    wedged, just idle.

    Polls every ~10s in production; scales down with a small
    `watchdog_wedge_seconds` so a test doesn't need real minutes to exercise
    this. After a detection, suppresses re-checking for `wedge_seconds` more
    so one wedge doesn't produce a WEDGE row every poll."""
    stop = _STOP_EVENT
    if stop is None:
        return
    suppressed_until = 0.0
    while True:
        wedge_seconds = float(get_setting("watchdog_wedge_seconds"))
        if wedge_seconds <= 0:
            return
        poll_interval = min(10.0, max(wedge_seconds / 4, 0.05))
        if stop.wait(poll_interval):
            return
        now = _time.monotonic()
        if now < suppressed_until:
            continue
        beat = _LOOP_BEAT_MONO
        if beat is None or now - beat < wedge_seconds:
            continue
        oldest = _oldest_inbox_age(names)
        if oldest is None or oldest < wedge_seconds:
            continue
        _attempt_wedge_recovery(label, beat, oldest, wedge_seconds)
        suppressed_until = _time.monotonic() + wedge_seconds


def _make_signal_handler(label: str):
    def handle(signum, _frame):
        global _SIGNAL_COUNT
        _SIGNAL_COUNT += 1
        if _SIGNAL_COUNT == 1:
            sys.stderr.write(
                f"[a8s] {label}: received signal {signum}; detaching after current wake\n"
            )
            sys.stderr.flush()
            if _STOP_EVENT is not None:
                _STOP_EVENT.set()
        else:
            sys.stderr.write(
                f"[a8s] {label}: second signal — killing wake subprocess group\n"
            )
            sys.stderr.flush()
            _kill_wake_subprocess_group()
    return handle


def _on_kill_signal(_signum, _frame):
    """SIGUSR1 from `cmd_kill`. If the in-flight wake's target agent has a
    foreign kill-request, kill the subprocess group so `run_with_prefix`'s
    `wait()` returns immediately. The actual release of the agent (and any
    others with a kill-request, even when no wake is in flight) happens at
    the next iteration top via `_read_kill_request`."""
    name = _CURRENT_WAKE_NAME
    if name is None:
        return
    req = _read_kill_request(name)
    if req is None or req == os.getpid():
        return
    _kill_wake_subprocess_group()


def _drain_one(p: Participant, msg_path: Path) -> None:
    """Trash a single inbox message without invoking, with summary output."""
    try:
        data = json.loads(msg_path.read_text())
        sender = data.get("from", "?")
        content = data.get("content", "")
        preview = content.replace("\n", " ")[:80]
        out_agent(p.name, f"[drain] {sender}: {preview}")
    except Exception:
        out_agent(p.name, f"[drain] (unreadable: {msg_path.name})")
    dest = unique_path(trash_dir(p.name) / msg_path.name)
    msg_path.rename(dest)


def _dispatch_agent(p: Participant, definition: dict, *, async_wake: bool) -> bool:
    """Try to process one inbox unit for `p`. Returns True if a subprocess wake
    was started (attached_loop should not start another until it finishes)."""
    if not peek_inbox_messages(p, 1):
        clear_inbox_waiting_since(p.name)
        return False

    if is_file_proxy(definition):
        msg = next_inbox_message(p)
        if msg is None:
            clear_inbox_waiting_since(p.name)
            return False
        wake_once(p, msg, async_wake=False)
        return False

    if not _wake_retry_ready(p.name):
        return False

    if not _pause_ready_for_wake(p, definition):
        return False

    if has_batch_invoke(definition):
        limit = batch_limit(definition)
        batch_paths = peek_inbox_messages(p, limit)
        if len(batch_paths) >= 2:
            return wake_batch(p, batch_paths, definition, async_wake=async_wake)

    msg = next_inbox_message(p)
    if msg is None:
        clear_inbox_waiting_since(p.name)
        return False
    return wake_once(p, msg, async_wake=async_wake)


def attached_loop(names: list[str], interval: float, *, single_pass: bool = False, drain_seconds: float = 0) -> int:
    """Body of `a8s run` / `a8s start` / `a8s step`. ONE process handles every
    name in `names`; multi-agent handlers share a PID across each member's
    pid file.

    Per iteration:
      - honor any detach-requests for our handled agents (per-agent
        take-over): release just the requested agent and keep serving the rest
      - reload registry (so newly-added agents become routable recipients)
      - drop any agent whose pid file no longer points at us (defense)
      - route each handled agent's outbox; deliver every file proxy's inbox;
        then wake one non-proxy agent if the wake slot is free

    On 1st signal: detach all currently-handled agents (graceful — finish the
    in-flight wake first). On 2nd signal: SIGTERM-then-SIGKILL the wake
    subprocess group. The whole-process detach is the path for explicit
    `a8s stop` / `a8s kill`; per-agent take-over for `a8s start`/`run`/`step`
    against an already-attached agent goes through the detach-request file
    instead, leaving siblings handled — no orphans."""
    global _STOP_EVENT, _SIGNAL_COUNT, _LOOP_BEAT_MONO, _LOOP_BEAT_WALL
    core.PRINT_LOCK = threading.Lock()
    _STOP_EVENT = threading.Event()
    _SIGNAL_COUNT = 0

    if not names:
        print("attached_loop: empty names list", file=sys.stderr)
        return 2

    # Acquire each pid file. If any fails (holder didn't honor the
    # detach-request in time), release whatever we got.
    acquired: list[str] = []
    try:
        for name in names:
            acquire(name)
            acquired.append(name)
    except TimeoutError as e:
        print(str(e), file=sys.stderr)
        for n in acquired:
            release(n)
        return 1

    label = names[0] if len(names) == 1 else f"[{', '.join(names)}]"
    handler = _make_signal_handler(label)
    prev_sigterm = signal.signal(signal.SIGTERM, handler)
    prev_sigint = signal.signal(signal.SIGINT, handler)
    prev_sigusr1 = (
        signal.signal(signal.SIGUSR1, _on_kill_signal)
        if hasattr(signal, "SIGUSR1")
        else None
    )

    pid = os.getpid()
    for n in names:
        out_agent(n, f"[a8s] {n}: attached (PID {pid}{', shared' if len(names) > 1 else ''})")

    # Load configured remotes and start one subscriber loop per
    # remote. The receive callback always asks the registry for the current
    # participant list so agents added after startup become routable without
    # restarting the daemon.
    # Storage services — stateless, no start/stop, and deliberately NOT
    # captured here. A daemon runs for days; one started before a service was
    # configured would never see it, and would fail every attachment while
    # `a8s storage` cheerfully listed the service it was ignoring. Both sides
    # resolve at use time instead — `load_services` rebuilds only when the
    # config changes.
    # A receiver killed mid-delivery leaves its claim behind. Startup is when
    # that is most likely to be true, and the only moment nothing is in flight.
    sweep_stale_claims()
    started_remotes = start_remotes(
        load_remotes(node=",".join(sorted(names))), participants_from_registry
    )
    publish_remotes = make_publish_remotes(started_remotes) if started_remotes else None
    configured_remote_ids = [r.id for r in started_remotes]
    deadline = _time.monotonic() + drain_seconds if drain_seconds > 0 else 0
    async_wake = not single_pass
    # Round-robin wake start across attached-loop iterations so a busy early
    # agent cannot starve siblings on the same handler. Scoped to
    # async_wake only — `a8s step` (single_pass) stays index-0 ordered. The
    # idle pass below has the same starve shape and its own counter.
    wake_rr = 0
    idle_rr = 0
    mode = "drain" if drain_seconds > 0 else ("step" if single_pass else "run")
    stop_reason: str | None = None
    watchdog_thread: threading.Thread | None = None
    if not single_pass and drain_seconds == 0 and float(get_setting("watchdog_wedge_seconds")) > 0:
        watchdog_thread = threading.Thread(
            target=_watchdog_loop, args=(names, label), daemon=True
        )
        watchdog_thread.start()
    last_heartbeat_mono = _time.monotonic()
    try:
        txlog.log(
            "RUN_START",
            sender=label,
            detail=f"agents={','.join(names)} pid={pid} interval={interval:g} mode={mode}",
        )
        while True:
            _LOOP_BEAT_MONO = _time.monotonic()
            _LOOP_BEAT_WALL = datetime.now(timezone.utc)
            hb_seconds = float(get_setting("txlog_heartbeat_seconds"))
            if hb_seconds > 0 and _LOOP_BEAT_MONO - last_heartbeat_mono >= hb_seconds:
                last_heartbeat_mono = _LOOP_BEAT_MONO
                txlog.log("HEARTBEAT", sender=label, detail=_heartbeat_detail())
            if _STOP_EVENT.is_set() and not _wake_in_flight():
                stop_reason = "stop-signal"
                break
            if deadline and _time.monotonic() >= deadline:
                _STOP_EVENT.set()
                stop_reason = "deadline"
                break
            try:
                # Honor kill-requests and detach-requests at the iteration
                # top. This is the mechanism on every platform: SIGUSR1
                # (POSIX only — Windows has no user-definable signal, and no
                # console-control substitute that can target a background
                # process, see docs/ar3-foundation.md's process doctrine) is a latency
                # optimisation on top, killing the subprocess group early
                # via `_on_kill_signal`. The group kill also happens here so
                # a Windows `a8s kill` — or a POSIX one that raced the signal
                # — still reaps the in-flight wake instead of orphaning it.
                for name in list(names):
                    kill_req = _read_kill_request(name)
                    if kill_req is not None and kill_req != pid:
                        if name == _CURRENT_WAKE_NAME:
                            _kill_wake_subprocess_group()
                        out_agent(name, f"[a8s] {name}: killed by PID {kill_req}")
                        release(name)
                        _clear_kill_request(name)
                        names.remove(name)
                        continue
                    requester = _read_detach_request(name)
                    if requester is not None and requester != pid:
                        out_agent(name, f"[a8s] {name}: releasing to PID {requester}")
                        release(name)
                        names.remove(name)

                all_agents = participants_from_registry()
                handled: list[Participant] = []
                for name in list(names):
                    p = next((q for q in all_agents if q.name == name), None)
                    if p is None:
                        # Absent from the participant list for one of two
                        # reasons: gone from the registry, or still registered
                        # with a mailbox path field that no longer resolves.
                        # Naming the second here is the one line an operator
                        # gets, and it fires once — the name leaves `names`.
                        reason = unresolved_mailboxes().get(name, "removed from registry")
                        out_agent(name, f"[a8s] {name}: {reason}; dropping")
                        names.remove(name)
                        continue
                    holder = _read_handler_pid(name)
                    if holder is not None and holder != pid:
                        # Defense: someone manually overwrote the pid file
                        # outside of the detach-request handshake.
                        out_agent(name, f"[a8s] {name}: pid file diverged (now PID {holder}); dropping")
                        names.remove(name)
                        continue
                    handled.append(p)
                if not handled:
                    out_agent(label, f"[a8s] {label}: nothing left to handle; exiting")
                    stop_reason = "nothing left to handle"
                    break
                for p in handled:
                    ensure_mailboxes(p)
                if drain_seconds == 0:
                    route_outboxes(
                        handled,
                        all_agents=all_agents,
                        publish_remotes=publish_remotes,
                        configured_remote_ids=configured_remote_ids,
                        services=load_services(),
                    )
                _service_in_flight_wake()
                if drain_seconds > 0:
                    for p in handled:
                        while not _STOP_EVENT.is_set():
                            msg = next_inbox_message(p)
                            if msg is None:
                                clear_inbox_waiting_since(p.name)
                                break
                            _drain_one(p, msg)
                else:
                    defined: list[tuple[Participant, dict]] = []
                    for p in handled:
                        try:
                            defined.append((p, load_definition(p.name)))
                        except (FileNotFoundError, RuntimeError) as e:
                            out_agent(p.name, f"[{p.name}] {e}")
                    # A file proxy's delivery is a file move that spawns
                    # nothing, so it has no reason to queue behind the single
                    # wake slot. Gating it froze proxy inboxes —
                    # and `tells`, which watches them — for as long as some
                    # other handled agent's turn ran, up to max_wake_seconds.
                    for p, definition in defined:
                        if is_file_proxy(definition):
                            _dispatch_agent(p, definition, async_wake=False)
                    if not _wake_in_flight():
                        n = len(defined)
                        start = (wake_rr % n) if async_wake and n else 0
                        woke_index: int | None = None
                        for i in range(n):
                            p, definition = defined[(start + i) % n]
                            if _wake_in_flight():
                                break
                            if is_file_proxy(definition):
                                continue
                            while not _STOP_EVENT.is_set():
                                if _wake_in_flight():
                                    break
                                started = _dispatch_agent(
                                    p, definition, async_wake=async_wake
                                )
                                if started:
                                    woke_index = i
                                    break
                                if async_wake:
                                    break
                                if not peek_inbox_messages(p, 1):
                                    break
                                if not _wake_retry_ready(p.name):
                                    break
                                if not _pause_ready_for_wake(p, definition):
                                    break
                            if async_wake and woke_index is not None:
                                break
                        # Rotation means "next after the one that woke" — the
                        # position that actually started, not start+1. A +1
                        # advance past a skipped agent parks the counter ON the
                        # agent that just woke, handing it a double turn (the
                        # idle rotation below has always advanced by position).
                        # And a pass where nobody had mail must not move it:
                        # advancing unconditionally spins the counter through
                        # every idle iteration, and then which of two
                        # simultaneously-mailed agents goes first depends on how
                        # long the quiet period happened to be — fair on
                        # average, unreproducible in the particular.
                        if async_wake and n and woke_index is not None:
                            wake_rr = start + woke_index + 1
                if (
                    not _STOP_EVENT.is_set()
                    and drain_seconds == 0
                    and not _wake_in_flight()
                ):
                    # Same shape as the wake loop above, and the same fix: it
                    # breaks on the first started invoke, so from a fixed
                    # index-0 start an agent with a much shorter idle.timeout
                    # than its siblings takes every idle slot it is ready for.
                    # last-active limits that but does not divide it fairly.
                    m = len(handled)
                    idle_start = (idle_rr % m) if async_wake and m else 0
                    for j in range(m):
                        p = handled[(idle_start + j) % m]
                        if _wake_in_flight():
                            break
                        try:
                            if maybe_run_idle(p, async_wake=async_wake):
                                if async_wake:
                                    idle_rr = idle_start + j + 1
                                break
                        except Exception as e:
                            out_agent(p.name, f"[{p.name}] idle check error: {e}")
            except Exception as e:
                out_agent(label, f"[a8s] {label}: iteration error: {e}")
            if single_pass and not _wake_in_flight():
                stop_reason = "single pass complete"
                break
            _STOP_EVENT.wait(interval)
    finally:
        # Stop subscriber threads first so paho's network loop unwinds before
        # we release pid files (otherwise an in-flight envelope arriving
        # during shutdown could try to write into a directory we're about to
        # forget).
        stop_remotes(started_remotes)
        while _wake_in_flight():
            _service_in_flight_wake()
            _time.sleep(0.05)
        txlog.log("RUN_STOP", sender=label, detail=stop_reason or "loop exited")
        if watchdog_thread is not None:
            watchdog_thread.join(timeout=1.0)
        # Release every pid file we still hold.
        for n in acquired:
            holder = _read_handler_pid(n)
            if holder is None or holder == pid:
                release(n)
                out_agent(n, f"[a8s] {n}: detached")
        signal.signal(signal.SIGTERM, prev_sigterm)
        signal.signal(signal.SIGINT, prev_sigint)
        if hasattr(signal, "SIGUSR1"):
            signal.signal(signal.SIGUSR1, prev_sigusr1)
        _STOP_EVENT = None
    return 0
