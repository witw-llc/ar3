"""tool-command-shell: argv building, execution limits, and the reply the
wake produces end to end through `a8s tell`."""
from __future__ import annotations

import json
import os
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

    @pytest.mark.parametrize("command", [
        "echo $$ > {pidfile}; exec sleep 30",
        "sleep 30 & echo $! > {pidfile}; exit 0",
    ], ids=["leader-alive", "leader-exited-child-holds-pipes"])
    def test_stop_signal_to_the_tool_kills_the_command(self, tmp_path, command):
        """a8s ends a wake with `terminate_group` on the wake's own group (max
        wake time, node stop). The command runs in its own session, outside
        that group, so the tool itself must take it down on the way out —
        also while it is only draining a child the shell left behind."""
        pidfile = tmp_path / "pid"
        command = command.format(pidfile=pidfile)
        script = (
            "import command_shell as cs\n"
            f"cs.run_command({command!r}, timeout=60)\n"
        )
        tool = spawn(
            [sys.executable, "-c", script], cwd=Path(cs.__file__).parent,
            env={**os.environ, "PYTHONPATH": Path(cs.__file__).parent.parent.parent / "lib"},
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
            assert not pid_alive(command_pid), "the command outlived the tool"
        finally:
            if tool.poll() is None:
                terminate_group(tool)
            try:
                os.kill(int(pidfile.read_text() or 0), 9)
            except (OSError, ValueError):
                pass


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
