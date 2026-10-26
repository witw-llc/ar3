"""tool-command-shell: argv building, execution limits, and the reply the
wake produces end to end through `a8s tell`."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path

import pytest

from ar3.proc import pid_alive, spawn, terminate_group

import command_shell as cs

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="the POSIX shell paths here do not run on Windows"
)


class TestBuildArgv:
    def test_default_is_sh_c_on_posix(self):
        assert cs.build_argv("", "echo hi") == ["sh", "-c", "echo hi"]

    def test_named_shell_takes_dash_c(self):
        assert cs.build_argv("bash", "echo hi") == ["bash", "-c", "echo hi"]

    def test_cmd_maps_to_slash_c(self):
        assert cs.build_argv("cmd", "dir") == ["cmd.exe", "/c", "dir"]

    def test_powershell_maps_to_command(self):
        assert cs.build_argv("pwsh", "Get-Date") == ["pwsh", "-NoProfile", "-Command", "Get-Date"]


class TestRunCommand:
    def test_captures_stdout_and_exit_code(self):
        r = cs.run_command("echo out; echo err >&2; exit 3")
        assert r["code"] == 3
        assert r["stdout"].strip() == "out"
        assert r["stderr"].strip() == "err"
        assert not r["timed_out"]

    def test_timeout_kills_and_flags(self):
        r = cs.run_command("sleep 10", timeout=0.5)
        assert r["timed_out"]
        assert r["code"] is None

    def test_output_cap_truncates(self):
        r = cs.run_command("printf '%0.sx' $(seq 1 1000)", output_max=100)
        assert len(r["stdout"].encode()) <= 100
        assert r["stdout_trunc"]

    def test_output_past_the_cap_is_drained_not_buffered(self):
        """8 MiB of output against a 1000-byte cap: the command runs to its
        own exit (the pipe is drained past the cap, so it never blocks) and
        the capture holds the cap, not the whole stream — the peak traced
        allocation stays far under the output's size."""
        tracemalloc.start()
        try:
            r = cs.run_command(
                "head -c 8388608 /dev/zero | tr '\\0' x; echo done >&2",
                output_max=1000, timeout=30,
            )
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert r["code"] == 0
        assert not r["timed_out"]
        assert len(r["stdout"]) == 1000 and r["stdout_trunc"]
        assert r["stderr"].strip() == "done" and not r["stderr_trunc"]
        assert peak < 2 * 1024 * 1024

    def test_background_child_past_the_deadline_is_killed(self, tmp_path):
        """The shell exiting is not the command ending: a child it left in
        the group still holds the pipes. The deadline covers that drain, and
        the child dies at it — it must not write its marker afterwards."""
        marker = tmp_path / "marker"
        r = cs.run_command(
            f"(sleep 1; echo late > {marker}) & exit 0", timeout=0.3,
        )
        assert r["timed_out"]
        assert r["code"] == 0
        assert r["elapsed"] < 3
        time.sleep(1.3)
        assert not marker.exists(), "the background child outlived the command"

    def test_nothing_outlives_a_finished_command(self, tmp_path):
        """A child that let go of the pipes does not make the command wait,
        and is still ended with the command: one message, one process tree."""
        pidfile = tmp_path / "pid"
        r = cs.run_command(f"sleep 30 >/dev/null 2>&1 & echo $! > {pidfile}; exit 0")
        assert r["code"] == 0 and not r["timed_out"]
        assert r["elapsed"] < 3
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 2
        while pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not pid_alive(pid)

    STOP_COMMANDS = [
        "echo $$ > {pidfile}; exec sleep 30",
        "sleep 30 & echo $! > {pidfile}; exit 0",
    ]
    STOP_IDS = ["leader-alive", "leader-exited-child-holds-pipes"]

    @staticmethod
    def _stop_the_tool(tmp_path, command, *, start_delay=0.0):
        """Run `command` in a tool process, wait until it has written its pid, stop the tool the way a8s stops a wake
        (`terminate_group`: SIGTERM, half a second, SIGKILL), and return (the tool, the pid of the command, whether the
        command was gone within two seconds). `start_delay` makes the tool sleep that long right after the command is
        started, before anything else: a busy machine, made certain."""
        pidfile = tmp_path / "pid"
        command = command.format(pidfile=pidfile)
        slow = (
            "import subprocess, time\n"
            "_Popen = subprocess.Popen\n"
            "class SlowPopen(_Popen):\n"
            "    def __init__(self, *args, **kwargs):\n"
            "        super().__init__(*args, **kwargs)\n"
            f"        time.sleep({start_delay})\n"
            "subprocess.Popen = SlowPopen\n"
        ) if start_delay else ""
        script = (
            "import command_shell as cs\n"
            + slow
            + f"cs.run_command({command!r}, timeout=60)\n"
        )
        tool = spawn(
            [sys.executable, "-c", script], cwd=Path(cs.__file__).parent.parent,
            env={**os.environ, "PYTHONPATH": Path(cs.__file__).parents[3] / "lib"},
        )
        try:
            deadline = time.monotonic() + 10
            while not pidfile.exists() or not pidfile.read_text().strip():
                assert time.monotonic() < deadline, "the command never started"
                assert tool.poll() is None, "the tool exited before the command ran"
                time.sleep(0.05)
            command_pid = int(pidfile.read_text())
            assert pid_alive(command_pid)
            terminate_group(tool)
            tool.wait(timeout=5)
            deadline = time.monotonic() + 2
            while pid_alive(command_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            return tool, command_pid, not pid_alive(command_pid)
        finally:
            if tool.poll() is None:
                terminate_group(tool)
            try:
                os.kill(int(pidfile.read_text() or 0), 9)
            except (OSError, ValueError):
                pass

    @pytest.mark.parametrize("command", STOP_COMMANDS, ids=STOP_IDS)
    def test_stop_signal_to_the_tool_kills_the_command(self, tmp_path, command):
        """a8s ends a wake with `terminate_group` on the wake's own group (max
        wake time, node stop). The command runs in its own session, outside
        that group, so the tool itself must take it down on the way out —
        also while it is only draining a child the shell left behind."""
        _tool, _pid, gone = self._stop_the_tool(tmp_path, command)
        assert gone, "the command outlived the tool"

    @pytest.mark.parametrize("command", STOP_COMMANDS, ids=STOP_IDS)
    def test_a_stop_signal_that_comes_while_the_command_is_starting_takes_it_down_and_lands(self, tmp_path, command):
        """The race of the test above, made certain. The command has written its pid, and the tool, which started it, has
        not got further: the tool sleeps 0.3 s right after the start, as a busy machine would make it (it has two
        threads to start before the old code put its handler in place, and a8s's stop came in that gap: the tool died
        of the signal and the command, in a session of its own, lived on for its 30 s). The handler is in place before
        the command now; the stop is kept until the command is known, then it takes the command down and lands, so the
        tool ends of the signal as it would have."""
        tool, _pid, gone = self._stop_the_tool(tmp_path, command, start_delay=0.3)
        assert gone, "the command outlived the tool"
        assert tool.returncode == -signal.SIGTERM, "the signal landed on the tool after it took the command down"

    @staticmethod
    def _tool_with(tmp_path, preamble, command="echo started > {marker}; exec sleep 30"):
        """A tool process that runs `command` after `preamble` (code of the test). It touches `ready` when the preamble says
        so: the test sends its stop then, and `started` is touched by whatever starts the command. Returns (tool, marker, ready)."""
        marker, ready = tmp_path / "marker", tmp_path / "ready"
        script = (
            "import command_shell as cs\n"
            + preamble.format(ready=str(ready), started=str(tmp_path / "popen"))
            + f"cs.run_command({command.format(marker=marker)!r}, timeout=60)\n"
        )
        tool = spawn(
            [sys.executable, "-c", script], cwd=Path(cs.__file__).parent.parent,
            env={**os.environ, "PYTHONPATH": Path(cs.__file__).parents[3] / "lib"},
        )
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert time.monotonic() < deadline and tool.poll() is None, "the tool never got ready"
            time.sleep(0.02)
        return tool, marker, ready

    def test_a_stop_that_comes_before_the_command_is_started_means_it_is_never_started(self, tmp_path):
        """The handler is in place and the command is not started yet (the tool is held right after the handlers, as a busy
        machine would hold it): the stop is kept, and the tool ends of it without starting the command."""
        tool, marker, _ready = self._tool_with(tmp_path, (
            "import signal, subprocess, time\n"
            "_Popen = subprocess.Popen\n"
            "class Marked(_Popen):\n"
            "    def __init__(self, *args, **kwargs):\n"
            "        open({started!r}, 'w').close()\n"
            "        super().__init__(*args, **kwargs)\n"
            "subprocess.Popen = Marked\n"
            "_signal = signal.signal\n"
            "def slow(signum, handler):\n"
            "    previous = _signal(signum, handler)\n"
            "    if signum == signal.SIGHUP and callable(handler):\n"   # the last of the three: all are in place
            "        open({ready!r}, 'w').close()\n"
            "        time.sleep(0.4)\n"
            "    return previous\n"
            "signal.signal = slow\n"
        ))
        try:
            terminate_group(tool)
            tool.wait(timeout=5)
            assert tool.returncode == -signal.SIGTERM
            assert not (tmp_path / "popen").exists() and not marker.exists(), "the command was started after the stop"
        finally:
            if tool.poll() is None:
                terminate_group(tool)

    def test_a_stop_that_comes_while_the_command_fails_to_start_lands_on_the_tool(self, tmp_path):
        """The shell is not there: starting the command raises. A stop that came meanwhile has no command to take down, and
        it lands when the block ends, so the tool ends of the signal and not of the error."""
        tool, _marker, _ready = self._tool_with(tmp_path, (
            "import subprocess, time\n"
            "class Missing(subprocess.Popen):\n"
            "    def __init__(self, *args, **kwargs):\n"
            "        open({ready!r}, 'w').close()\n"
            "        time.sleep(0.4)\n"
            "        raise FileNotFoundError('no such shell')\n"
            "subprocess.Popen = Missing\n"
        ))
        try:
            terminate_group(tool)
            tool.wait(timeout=5)
            assert tool.returncode == -signal.SIGTERM
        finally:
            if tool.poll() is None:
                terminate_group(tool)


class TestComposeReply:
    def test_small_output_is_inline(self, tmp_path):
        r = cs.run_command("echo hello")
        body, attachments = cs.compose_reply("echo hello", r, 60, 4096, tmp_path)
        assert "hello" in body
        assert attachments == []

    def test_large_output_is_attached(self, tmp_path):
        r = cs.run_command("seq 1 50")
        body, attachments = cs.compose_reply("seq 1 50", r, 60, 20, tmp_path)
        assert len(attachments) == 1
        assert attachments[0].endswith("stdout.txt")
        assert "attached" in body


def _reply(outbox):
    return json.loads(next(outbox.glob("*.json")).read_text())


class TestMainWake:
    def _outbox(self, tmp_path, monkeypatch):
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        monkeypatch.setenv("TELL_OUTBOX_DIR", str(outbox))
        return outbox

    def test_allowed_sender_runs_and_replies(self, tmp_path, monkeypatch):
        outbox = self._outbox(tmp_path, monkeypatch)
        rc = cs.main(["--from", "alice", "--allow", "alice,bob", "--", "echo wake-hi"])
        assert rc == 0
        env = _reply(outbox)
        assert env["to"] == "alice"
        assert "wake-hi" in env["content"]
        assert env["content"].startswith("$ echo wake-hi")

    def test_refused_sender_sends_no_reply(self, tmp_path, monkeypatch, capsys):
        outbox = self._outbox(tmp_path, monkeypatch)
        rc = cs.main(["--from", "mallory", "--allow", "alice,bob", "--", "echo nope"])
        assert rc == 0
        assert list(outbox.glob("*.json")) == []
        assert "not in ALLOW" in capsys.readouterr().err

    def test_senderless_wake_refused(self, tmp_path, monkeypatch):
        outbox = self._outbox(tmp_path, monkeypatch)
        rc = cs.main(["--from", "", "--allow", "alice", "--", "echo nope"])
        assert rc == 0
        assert list(outbox.glob("*.json")) == []

    def test_empty_command_reports_back(self, tmp_path, monkeypatch):
        outbox = self._outbox(tmp_path, monkeypatch)
        rc = cs.main(["--from", "alice", "--allow", "alice", "--", ""])
        assert rc == 0
        assert "no command" in _reply(outbox)["content"]

    def test_attachment_lines_are_stripped_from_command(self, tmp_path, monkeypatch):
        outbox = self._outbox(tmp_path, monkeypatch)
        msg = "echo keep\n\nATTACHED FILE: /tmp/ignored.bin"
        rc = cs.main(["--from", "alice", "--allow", "alice", "--", msg])
        assert rc == 0
        content = _reply(outbox)["content"]
        assert content.startswith("$ echo keep")
        assert "keep" in content
        assert "ignored.bin" not in content

    def test_timeout_var_is_honored(self, tmp_path, monkeypatch):
        outbox = self._outbox(tmp_path, monkeypatch)
        rc = cs.main(["--from", "alice", "--allow", "alice", "--timeout=0.5", "--", "sleep 10"])
        assert rc == 0
        assert "timed out" in _reply(outbox)["content"]


class TestHelp:
    @pytest.mark.parametrize("spelling", ["help", "/help", "--help", "/?", "HELP"])
    def test_help_answers_the_usage_and_runs_nothing(self, tmp_path, monkeypatch, spelling):
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        monkeypatch.setenv("TELL_OUTBOX_DIR", str(outbox))
        usage = cs.shell.rt.usage_from_readme(cs.shell.README)
        assert usage
        marker = tmp_path / "ran"
        monkeypatch.setattr(
            cs.shell, "run_command", lambda *a, **k: marker.write_text("ran")
        )
        rc = cs.main(["--seat", "box", "--from", "alice", "--allow", "alice", "--", spelling])
        assert rc == 0
        assert not marker.exists()
        content = _reply(outbox)["content"]
        assert content.startswith("box: tool-command-shell — usage")
        assert usage in content
        assert content.endswith("README: apps/tools/command_shell/README.md")

    @pytest.mark.parametrize("message", ["help me open a page", "echo a\nhelp"])
    def test_other_messages_still_run(self, tmp_path, monkeypatch, message):
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        monkeypatch.setenv("TELL_OUTBOX_DIR", str(outbox))
        seen = []

        def fake(command, **kwargs):
            seen.append(command)
            return {"code": 0, "elapsed": 0.0, "timed_out": False, "stdout": "",
                    "stderr": "", "stdout_trunc": False, "stderr_trunc": False}

        monkeypatch.setattr(cs.shell, "run_command", fake)
        assert cs.main(["--from", "alice", "--allow", "alice", "--", message]) == 0
        assert seen == [message]

    def test_a_refused_sender_gets_no_help(self, tmp_path, monkeypatch):
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        monkeypatch.setenv("TELL_OUTBOX_DIR", str(outbox))
        assert cs.main(["--from", "mallory", "--allow", "alice", "--", "help"]) == 0
        assert list(outbox.glob("*.json")) == []

    def test_help_in_print_mode_prints_the_usage(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("AR3_TOOL_REPLY_DIR", str(tmp_path / "out"))
        assert cs.main(["--seat", "box", "--from", "operator", "--allow", "operator", "--", "/?"]) == 0
        out = capsys.readouterr().out
        assert out.startswith("box: tool-command-shell")
        assert cs.shell.rt.usage_from_readme(cs.shell.README) in out

    def test_help_without_a_usage_section_says_so(self, tmp_path, monkeypatch, capsys):
        bare = tmp_path / "README.md"
        bare.write_text("# tool-command-shell\n\n## State\n\nnone\n")
        monkeypatch.setattr(cs.shell, "README", bare)
        monkeypatch.setenv("AR3_TOOL_REPLY_DIR", str(tmp_path / "out"))
        assert cs.main(["--seat", "box", "--from", "operator", "--allow", "operator", "--", "help"]) == 0
        assert 'no "## Usage" section in apps/tools/command_shell/README.md' in capsys.readouterr().out


def test_the_tool_imports_where_the_platform_has_no_sighup():
    """Windows has no SIGHUP. The stop forwarder is installed on POSIX only, but the module must import
    everywhere: a child with SIGHUP deleted imports it and keeps the two stop signals it has."""
    code = (
        "import signal\n"
        "del signal.SIGHUP\n"
        "import command_shell.shell as shell\n"
        "assert shell._STOP_SIGNALS == (signal.SIGTERM, signal.SIGINT), shell._STOP_SIGNALS\n"
    )
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
