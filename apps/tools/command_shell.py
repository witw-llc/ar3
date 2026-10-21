#!/usr/bin/env python3
"""tool-command-shell — run a shell command sent as an a8s message.

A poor man's SSH: register a node on any machine, allowlist the senders who may
drive it, and `tell <node> <command>` runs that command there and sends the
output back. Each message is one command in its own process; nothing persists
between them (no session, no working-directory memory). The node's directory is
the working directory, set by a8s on wake.

Only a sender named in the `$ALLOW` var may run anything. `$ALLOW` is required,
so for a node registered without it a8s aborts every wake before this program
runs and dead-letters the message — fail closed. A refused sender gets no
reply; the refusal is logged on the node.

Limits, each a per-node a8s var with a default:
  ALLOW         required — comma/space-separated sender names
  SHELL         sh (POSIX default) / cmd (Windows default); also bash, zsh,
                powershell, pwsh, or any interpreter taking `-c`
  TIMEOUT       seconds before the command's process group is killed (60)
  INLINE_MAX    bytes of output carried inline in the reply (4096); beyond it,
                output is attached as stdout.txt / stderr.txt
  OUTPUT_MAX    bytes kept per stream (10 MiB); the rest is read and dropped

The command runs in its own session, and nothing it starts outlives it: a
background child it leaves in that session is killed when the command ends, by
exit, by the timeout, or by a stop signal to the tool (a8s ending the wake),
which kills the tree first and then takes effect. On Windows the tree is killed through `taskkill /T`, and a stop from
a8s is a plain TerminateProcess that no handler sees, so the command is on its
own there; the drain after a kill is bounded either way.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "lib"))

from ar3.proc import terminate_group  # noqa: E402

import _runtime as rt  # noqa: E402

DEFAULT_TIMEOUT = 60.0
DEFAULT_INLINE_MAX = 4096
DEFAULT_OUTPUT_MAX = 10 * 1024 * 1024


def build_argv(shell: str, command: str) -> list[str]:
    """The child argv for `command` under `shell`. An empty shell is the
    platform default: cmd on Windows, sh elsewhere."""
    s = (shell or "").strip().lower()
    if not s:
        s = "cmd" if os.name == "nt" else "sh"
    if s == "cmd":
        return ["cmd.exe", "/c", command]
    if s in ("powershell", "pwsh"):
        return [s, "-NoProfile", "-Command", command]
    # sh, bash, zsh, or any other interpreter that takes `-c`.
    return [s, "-c", command]


_CHUNK = 64 * 1024


class _Capture(threading.Thread):
    """Drains one pipe to EOF, keeping the first `limit` bytes. Past the limit
    the stream is read and dropped, so a command that writes more than the cap
    neither blocks on a full pipe nor fills memory with what is thrown away."""

    def __init__(self, stream, limit: int):
        super().__init__(daemon=True)
        self.fd = stream.fileno()
        self.limit = limit
        self.kept = bytearray()
        self.truncated = False
        self.start()

    def run(self) -> None:
        while True:
            try:
                chunk = os.read(self.fd, _CHUNK)
            except OSError:
                return
            if not chunk:
                return
            if self.truncated:
                continue
            room = self.limit - len(self.kept)
            if len(chunk) > room:
                self.kept += chunk[:room]
                self.truncated = True
            else:
                self.kept += chunk


def _group_alive(proc: subprocess.Popen) -> bool:
    """Whether anything is left in the command's process group. The command
    leads its own session, so its pgid is its pid by construction, and that
    holds after the leader has exited. Windows has no group: the answer is
    whether the shell itself is still running, so a child it left behind
    after exiting is not seen there (the doc says so)."""
    if os.name == "nt":
        return proc.poll() is None
    try:
        os.killpg(proc.pid, 0)
    except OSError:
        return False
    return True


def _kill_tree(proc: subprocess.Popen, *, grace_seconds: float = 0.5) -> None:
    """The command and everything it started. POSIX: the foundation's group
    kill, which reaches the whole session the command leads. Windows: the
    foundation stops at the shell process (there is no group), so `taskkill
    /T` takes the tree, as a command runner is exactly the case of a shell
    that starts children."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if proc.poll() is None:
            proc.kill()
        return
    terminate_group(proc, grace_seconds=grace_seconds)


@contextlib.contextmanager
def _command_dies_with_us(proc: subprocess.Popen):
    """While the command runs, a stop signal to this process kills the
    command's group first, then lands as it would have. The command is in its
    own session so the tool's timeout can kill the whole tree without killing
    itself, which also puts it outside the wake's group — a8s ending the wake
    (max wake time, node stop) with `terminate_group` would otherwise leave
    the command running. No grace: the caller already chose to stop us."""
    if os.name != "posix" or threading.current_thread() is not threading.main_thread():
        yield
        return

    def forward(signum, _frame):
        _kill_tree(proc, grace_seconds=0)
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    stop_signals = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    previous = {sig: signal.signal(sig, forward) for sig in stop_signals}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def run_command(
    command: str,
    *,
    shell: str = "",
    timeout: float = DEFAULT_TIMEOUT,
    output_max: int = DEFAULT_OUTPUT_MAX,
    cwd: str | None = None,
) -> dict:
    """Run `command` and capture it. Returns a dict with `code` (None when the
    shell itself was killed at the deadline), `elapsed`, `timed_out` (the
    deadline passed with the shell or a child it left still running),
    `stdout`, `stderr` (decoded, each the first `output_max` bytes of its
    stream), and `stdout_trunc` / `stderr_trunc` flags. Nothing the command
    started outlives it: its whole process group ends when it finishes."""
    argv = build_argv(shell, command)
    start = time.monotonic()
    proc = subprocess.Popen(
        argv,
        cwd=cwd or os.getcwd(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=(os.name == "posix"),
    )
    out = _Capture(proc.stdout, output_max)
    err = _Capture(proc.stderr, output_max)
    deadline = start + timeout
    timed_out = False
    code = None
    # The shell exiting is not the command ending: a child it left in the
    # group may still hold the pipes and still be working. The command ends
    # when both pipes close or the deadline passes, whichever is first, and
    # the stop-signal forward covers that whole span.
    with _command_dies_with_us(proc):
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        for reader in (out, err):
            reader.join(timeout=max(0.0, deadline - time.monotonic()))
            if reader.is_alive():
                timed_out = True
        # One message is one process tree: whatever the command leaves
        # behind in its group ends with it.
        if timed_out or _group_alive(proc):
            _kill_tree(proc)
        if code is None:
            proc.wait()
    # A descendant in another session can hold a pipe open after the group is
    # gone; the readers are daemons, so give them a moment and take what they
    # have rather than wait on it.
    out.join(timeout=5)
    err.join(timeout=5)
    proc.stdout.close()
    proc.stderr.close()
    elapsed = time.monotonic() - start
    return {
        "code": code,
        "elapsed": elapsed,
        "timed_out": timed_out,
        "stdout": bytes(out.kept).decode("utf-8", "replace"),
        "stderr": bytes(err.kept).decode("utf-8", "replace"),
        "stdout_trunc": out.truncated,
        "stderr_trunc": err.truncated,
    }


def _status_line(result: dict, timeout: float) -> str:
    if result["timed_out"]:
        return f"timed out after {timeout:.0f}s, process group killed"
    return f"exit {result['code']} in {result['elapsed']:.2f}s"


def compose_reply(
    command: str, result: dict, timeout: float, inline_max: int, work_dir: Path
) -> tuple[str, list[str]]:
    """The reply body and any attachment paths. Output within `inline_max`
    (both streams together) rides in the body; beyond it, each non-empty
    stream is written into `work_dir` and attached, with a short head inline."""
    header = [f"$ {command}", _status_line(result, timeout)]
    out, err = result["stdout"], result["stderr"]
    for name, trunc in (("stdout", result["stdout_trunc"]), ("stderr", result["stderr_trunc"])):
        if trunc:
            header.append(f"({name} truncated at the capture limit)")

    if len(out) + len(err) <= inline_max:
        lines = list(header)
        if out:
            lines += ["", "stdout:", out.rstrip("\n")] if err else ["", out.rstrip("\n")]
        if err:
            lines += ["", "stderr:", err.rstrip("\n")]
        if not out and not err:
            lines.append("(no output)")
        return "\n".join(lines), []

    attachments: list[str] = []
    lines = list(header)
    lines.append(f"(output over the {inline_max}-byte inline limit — attached)")
    for name, text in (("stdout", out), ("stderr", err)):
        if not text:
            continue
        path = work_dir / f"{name}.txt"
        path.write_text(text, encoding="utf-8")
        attachments.append(str(path))
        head = text[:inline_max]
        lines += ["", f"{name} (first {len(head)} bytes):", head.rstrip("\n")]
    return "\n".join(lines), attachments


def _int_arg(value: str | None, default: int) -> int:
    if value is None or not value.strip():
        return default
    return int(value)


def _float_arg(value: str | None, default: float) -> float:
    if value is None or not value.strip():
        return default
    return float(value)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="tool-command-shell")
    parser.add_argument("--from", dest="sender", default="")
    parser.add_argument("--allow", default="")
    parser.add_argument("--shell", default="")
    parser.add_argument("--timeout", default=None)
    parser.add_argument("--inline-max", dest="inline_max", default=None)
    parser.add_argument("--output-max", dest="output_max", default=None)
    parser.add_argument("message", nargs="?", default="")
    args = parser.parse_args(argv)

    sender = args.sender
    if not sender:
        print("tool-command-shell: senderless wake refused", file=sys.stderr)
        return 0
    if not rt.allowed(sender, args.allow):
        print(f"tool-command-shell: {sender!r} is not in ALLOW — refused", file=sys.stderr)
        return 0

    command, _attachments = rt.split_attachments(args.message)
    command = command.strip()
    if not command:
        return rt.send_reply(sender, "tool-command-shell: no command given")

    timeout = _float_arg(args.timeout, DEFAULT_TIMEOUT)
    inline_max = _int_arg(args.inline_max, DEFAULT_INLINE_MAX)
    output_max = _int_arg(args.output_max, DEFAULT_OUTPUT_MAX)

    result = run_command(command, shell=args.shell, timeout=timeout, output_max=output_max)
    print(f"tool-command-shell: {sender} ran {command!r} -> {_status_line(result, timeout)}")
    # tell stages its own copy of each attachment before it returns, so the
    # capture files live only for the send.
    with tempfile.TemporaryDirectory(prefix="tool-command-shell-") as work_dir:
        body, attachments = compose_reply(command, result, timeout, inline_max, Path(work_dir))
        return rt.send_reply(sender, body, tuple(attachments))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
