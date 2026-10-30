"""Tool launches use the foundation policy at the actual subprocess boundary."""
from types import SimpleNamespace

import pytest

from ar3.proc import launch_options
import _runtime
from browser import plc, session
from command_shell import shell
from gemini_web import browser_seat
from vault import client, crypto


class LaunchReached(BaseException):
    pass


@pytest.mark.parametrize("module,invoke,group", [
    (_runtime, lambda: _runtime.send_reply("test-seat", "body"), False),
    (crypto, lambda: crypto._run(["version"]), False),
    (crypto, lambda: crypto.verify(b"public", b"data", b"signature"), False),
    (client, lambda: client._tell("test-seat", "body"), False),
    (browser_seat, lambda: browser_seat.BrowserSeat("test-seat").run("url"), False),
    (plc, lambda: plc.run("test-seat", "snapshot"), False),
    (plc, plc.sessions, False),
    (session, lambda: session._profile_pids("test-seat"), False),
    (session, lambda: session._main_pids("test-seat"), False),
    (session, lambda: session._daemon_pids("test-seat"), False),
    (shell, lambda: shell.run_command("echo ignored", shell="sh"), True),
])
def test_windows_launches_are_hidden_without_changing_io(monkeypatch, module, invoke, group):
    calls = []
    def intercept(argv, **kwargs):
        calls.append((argv, kwargs))
        raise LaunchReached
    monkeypatch.setattr(module, "launch_options", lambda **kw: launch_options(windows=True, **kw))
    monkeypatch.setattr(module.subprocess, "run", intercept)
    monkeypatch.setattr(module.subprocess, "Popen", intercept)
    monkeypatch.delenv("AR3_TOOL_REPLY_DIR", raising=False)
    monkeypatch.setattr(plc, "require", lambda: "fake-playwright")
    monkeypatch.setattr(plc.shutil, "which", lambda name: name)
    monkeypatch.setattr(session.platform, "system", lambda: "Linux")
    with pytest.raises(LaunchReached):
        invoke()
    assert len(calls) == 1
    argv, options = calls[0]
    assert options["creationflags"] == (0x08000200 if group else 0x08000000)
    assert not options.get("start_new_session", False)
    if module is _runtime:
        assert options["input"] == "body"
        assert options["capture_output"] is True
        assert options["env"]["PYTHONUTF8"] == "1"
    if module is shell:
        assert options["stdin"] == shell.subprocess.DEVNULL
        assert options["stdout"] == shell.subprocess.PIPE
        assert options["stderr"] == shell.subprocess.PIPE
    if module is plc:
        assert options["env"] == plc.cli_env()


def test_tree_kill_is_the_foundation_group_kill(monkeypatch):
    """The Windows `taskkill /T` arm and its hidden console are the
    foundation's (apps/ar3/tests/test_ar3_proc.py); the tool keeps no copy."""
    calls = []
    monkeypatch.setattr(shell, "terminate_group", lambda proc, **kw: calls.append((proc.pid, kw)))
    shell._kill_tree(SimpleNamespace(pid=123, poll=lambda: 0))
    assert calls == [(123, {"grace_seconds": 0.5})]


@pytest.mark.parametrize("interactive", [True, False])
def test_installer_inherits_console_only_after_terminal_prompt(monkeypatch, interactive):
    calls = []
    monkeypatch.setattr(plc, "_asked", False)
    monkeypatch.setattr(plc.shutil, "which", lambda name: name)
    monkeypatch.setattr(plc, "launch_options", lambda **kw: launch_options(windows=True, **kw))
    monkeypatch.setattr(plc.subprocess, "run", lambda argv, **kw: calls.append(kw))
    if interactive:
        monkeypatch.delenv("AR3_BROWSER_INSTALL_PLAYWRIGHT", raising=False)
        class Terminal:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def write(self, text): pass
            def flush(self): pass
            def readline(self): return "yes"
        monkeypatch.setattr(plc, "open", lambda *args: Terminal(), raising=False)
    else:
        monkeypatch.setenv("AR3_BROWSER_INSTALL_PLAYWRIGHT", "1")
    assert plc._offer_install()
    assert calls[0].get("creationflags", 0) == (0 if interactive else 0x08000000)
