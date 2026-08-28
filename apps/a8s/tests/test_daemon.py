"""Tests for daemon.py — pid-file lifecycle and end-to-end wake_once with the
mock CLI.

The mock CLI lives at tests/fixtures/mock_cli.py. tests/fixtures/mock.json
defines an agent that routes every verb through it with deterministic
templates. Tests assert on the per-agent log to verify what argv the wake
subprocess actually received.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core import (
    Participant,
    TELL_OUTBOX_DIR_ENV,
    agent_log_path,
    clear_inbox_waiting_since,
    detach_request_path,
    files_dir,
    inbox_dir,
    kill_request_path,
    pid_path,
    trash_dir,
    unique_path,
)
from daemon import (
    _pause_ready_for_wake,
    _clear_detach_request,
    _clear_kill_request,
    _read_detach_request,
    _read_handler_pid,
    _read_kill_request,
    _try_atomic_claim,
    _write_detach_request,
    _write_kill_request,
    acquire,
    attached_loop,
    maybe_run_idle,
    release,
)
from mailbox import _write_outbox, ensure_mailboxes, route_outboxes
from registry import save_aliases, save_registry


def _main_thread_only(patched):
    """Scope an Event.wait monkeypatch to the loop under test.

    These tests replace `threading.Event.wait` process-wide to script the
    attached loop's iteration clock, but the runner's watchdog thread waits
    on the same event class — an unscoped patch runs the script from that
    thread too, racing the loop it is scripting (double refills, early
    stops). Any thread but the main one falls through to the real wait."""
    real_wait = threading.Event.wait

    def gated(self, timeout=None):
        if threading.current_thread() is not threading.main_thread():
            return real_wait(self, timeout)
        return patched(self, timeout)

    return gated


# ---------- pid-file lifecycle ----------

class TestAtomicClaim:
    def test_first_claim_succeeds(self, fake_home):
        assert _try_atomic_claim("X", 12345) is True
        assert pid_path("X").read_text() == "12345"

    def test_second_claim_fails(self, fake_home):
        assert _try_atomic_claim("X", 1) is True
        assert _try_atomic_claim("X", 2) is False
        # Original pid still there.
        assert pid_path("X").read_text() == "1"


class TestReadHandlerPid:
    def test_no_pid_file(self, fake_home):
        assert _read_handler_pid("X") is None

    def test_dead_pid_is_cleaned_up(self, fake_home):
        # Use a pid that's almost certainly dead (max signed int).
        agent_pid = 2**31 - 1
        pid_path("X").parent.mkdir(parents=True, exist_ok=True)
        pid_path("X").write_text(str(agent_pid))
        assert _read_handler_pid("X") is None
        assert not pid_path("X").is_file()

    def test_live_pid(self, fake_home):
        # Our own pid is live.
        pid_path("X").parent.mkdir(parents=True, exist_ok=True)
        pid_path("X").write_text(str(os.getpid()))
        assert _read_handler_pid("X") == os.getpid()

    def test_empty_pid_file_is_cleaned_up(self, fake_home):
        # Issue #66: a partial-write window between O_CREAT|O_EXCL and os.write
        # can leave an empty pid file. Treat as stale and unlink.
        pid_path("X").parent.mkdir(parents=True, exist_ok=True)
        pid_path("X").write_text("")
        assert _read_handler_pid("X") is None
        assert not pid_path("X").is_file()

    def test_negative_pid_is_cleaned_up(self, fake_home):
        # Issue #66: a non-positive pid doesn't refer to any real process —
        # pid 0 / negative pids would target the whole process group via
        # os.kill, which is unsafe.
        pid_path("X").parent.mkdir(parents=True, exist_ok=True)
        pid_path("X").write_text("-1")
        assert _read_handler_pid("X") is None
        assert not pid_path("X").is_file()

    def test_zero_pid_is_cleaned_up(self, fake_home):
        pid_path("X").parent.mkdir(parents=True, exist_ok=True)
        pid_path("X").write_text("0")
        assert _read_handler_pid("X") is None
        assert not pid_path("X").is_file()

    def test_garbage_pid_is_cleaned_up(self, fake_home):
        pid_path("X").parent.mkdir(parents=True, exist_ok=True)
        pid_path("X").write_text("not-an-int")
        assert _read_handler_pid("X") is None
        assert not pid_path("X").is_file()


class TestRequestFileLiveness:
    """Issue #71: stale rendezvous files from dead requesters must not be
    honored. Without this reap, an `acquire()` caller (or `cmd_kill`) that
    crashes after writing the request would cause the holder's next
    iteration to release the agent to nobody."""

    def test_detach_request_dead_requester_reaped(self, fake_home):
        dead_pid = 2**31 - 1
        detach_request_path("X").parent.mkdir(parents=True, exist_ok=True)
        detach_request_path("X").write_text(str(dead_pid))
        assert _read_detach_request("X") is None
        assert not detach_request_path("X").is_file()

    def test_detach_request_live_requester_returned(self, fake_home):
        detach_request_path("X").parent.mkdir(parents=True, exist_ok=True)
        detach_request_path("X").write_text(str(os.getpid()))
        assert _read_detach_request("X") == os.getpid()
        assert detach_request_path("X").is_file()

    def test_kill_request_dead_requester_reaped(self, fake_home):
        dead_pid = 2**31 - 1
        kill_request_path("X").parent.mkdir(parents=True, exist_ok=True)
        kill_request_path("X").write_text(str(dead_pid))
        assert _read_kill_request("X") is None
        assert not kill_request_path("X").is_file()

    def test_kill_request_live_requester_returned(self, fake_home):
        kill_request_path("X").parent.mkdir(parents=True, exist_ok=True)
        kill_request_path("X").write_text(str(os.getpid()))
        assert _read_kill_request("X") == os.getpid()
        assert kill_request_path("X").is_file()

    def test_attached_loop_ignores_dead_requester_detach(self, fake_home, tmp_path, fixtures_dir):
        # Without the liveness check, this dead-pid request would cause the
        # iteration top to spuriously release X.
        from registry import save_registry
        d = tmp_path / "x"; d.mkdir()
        save_registry({
            "X": {"root": str(d), "definition": str(fixtures_dir / "mock.json")},
        })
        ensure_mailboxes(Participant("X", d))
        detach_request_path("X").parent.mkdir(parents=True, exist_ok=True)
        detach_request_path("X").write_text(str(2**31 - 1))

        rc = attached_loop(["X"], 0.1, single_pass=True)
        assert rc == 0
        assert "releasing to PID" not in _read_log("X")
        # Stale request file reaped.
        assert not detach_request_path("X").is_file()

    def test_attached_loop_ignores_dead_requester_kill(self, fake_home, tmp_path, fixtures_dir):
        from registry import save_registry
        d = tmp_path / "x"; d.mkdir()
        save_registry({
            "X": {"root": str(d), "definition": str(fixtures_dir / "mock.json")},
        })
        ensure_mailboxes(Participant("X", d))
        kill_request_path("X").parent.mkdir(parents=True, exist_ok=True)
        kill_request_path("X").write_text(str(2**31 - 1))

        rc = attached_loop(["X"], 0.1, single_pass=True)
        assert rc == 0
        assert "killed by" not in _read_log("X")
        assert not kill_request_path("X").is_file()


class TestAtomicClaimDurability:
    def test_claim_after_partial_write_cleanup(self, fake_home):
        # Issue #66: if a prior writer died after O_CREAT but before the byte
        # write, the file exists but is empty. _read_handler_pid cleans it up;
        # the next _try_atomic_claim must then succeed.
        pid_path("X").parent.mkdir(parents=True, exist_ok=True)
        pid_path("X").write_text("")  # simulate partial-write death
        # Direct re-claim fails because the file still exists.
        assert _try_atomic_claim("X", os.getpid()) is False
        # _read_handler_pid reaps the empty file.
        assert _read_handler_pid("X") is None
        # Now _try_atomic_claim succeeds.
        assert _try_atomic_claim("X", os.getpid()) is True
        assert pid_path("X").read_text() == str(os.getpid())


class TestAcquireRelease:
    def test_acquire_when_free_then_release(self, fake_home):
        acquire("X")
        assert pid_path("X").read_text() == str(os.getpid())
        release("X")
        assert not pid_path("X").is_file()

    def test_acquire_reaps_stale_pid_and_succeeds(self, fake_home):
        # Pid file points at a dead pid → _read_handler_pid unlinks it →
        # acquire's loop retries the claim and succeeds.
        dead_pid = 2**31 - 1
        pid_path("X").parent.mkdir(parents=True, exist_ok=True)
        pid_path("X").write_text(str(dead_pid))
        acquire("X")
        assert pid_path("X").read_text() == str(os.getpid())
        release("X")

    def test_acquire_against_live_holder_times_out(self, fake_home, monkeypatch):
        # Issue #68: acquire writes a detach-request and polls; if the holder
        # never honors it, raise TimeoutError. Use a tiny timeout so the test
        # finishes quickly.
        monkeypatch.setattr("daemon.DETACH_TIMEOUT_S", 0.5)
        monkeypatch.setattr("daemon.DETACH_POLL_S", 0.05)
        # Hold the pid file with the parent shell's pid (live, foreign).
        pid_path("X").parent.mkdir(parents=True, exist_ok=True)
        pid_path("X").write_text(str(os.getppid()))
        with pytest.raises(TimeoutError, match="did not release X"):
            acquire("X")
        # Holder pid file untouched.
        assert pid_path("X").read_text() == str(os.getppid())
        # Detach-request cleared on timeout.
        assert not detach_request_path("X").is_file()

    def test_acquire_writes_detach_request_for_live_holder(self, fake_home, monkeypatch):
        # Verify the request file is written before the timeout fires.
        monkeypatch.setattr("daemon.DETACH_TIMEOUT_S", 0.3)
        monkeypatch.setattr("daemon.DETACH_POLL_S", 0.05)
        pid_path("X").parent.mkdir(parents=True, exist_ok=True)
        pid_path("X").write_text(str(os.getppid()))
        # During acquire's poll loop, the request file should be present.
        # Easiest assertion: after timeout, the file is gone (cleared on
        # timeout) — but during the polling window it WAS written. Use a
        # spy on _write_detach_request.
        called = {}
        orig = _write_detach_request
        def spy(name, pid):
            called["name"] = name
            called["pid"] = pid
            orig(name, pid)
        monkeypatch.setattr("daemon._write_detach_request", spy)
        with pytest.raises(TimeoutError):
            acquire("X")
        assert called == {"name": "X", "pid": os.getpid()}

    def test_release_clears_detach_request(self, fake_home):
        # release(name) also clears any pending detach-request — once we
        # release, there's nothing to ask for.
        acquire("X")
        _write_detach_request("X", os.getppid())
        assert detach_request_path("X").is_file()
        release("X")
        assert not pid_path("X").is_file()
        assert not detach_request_path("X").is_file()

    def test_release_other_pid_is_noop(self, fake_home):
        # Another (dead) pid in the file — release shouldn't unlink it because
        # it doesn't belong to us. _read_handler_pid will clean dead ones, but
        # release is intentionally guarded.
        pid_path("X").parent.mkdir(parents=True, exist_ok=True)
        pid_path("X").write_text("12345")
        # Use a live pid so the cleanup doesn't fire.
        # Actually we can't easily test "not ours but live" without spawning.
        # Check the simpler case: garbage in the file shouldn't crash release.
        release("X")  # garbage int parses, just doesn't match our pid
        # File should still be there (we didn't write it).
        # Actually: the file might or might not exist depending on whether
        # _read_handler_pid was called; release itself just guards the unlink.
        # Just assert no exception was raised.


# ---------- attached_loop end-to-end with mock CLI ----------

@pytest.fixture
def mock_agent(fake_home, tmp_path, fixtures_dir):
    """Register an agent named MOCK that uses the mock CLI definition."""
    agent_root = tmp_path / "mock-agent"
    agent_root.mkdir()
    save_registry({
        "MOCK": {
            "root": str(agent_root),
            "definition": str(fixtures_dir / "mock.json"),
        },
    })
    p = Participant("MOCK", agent_root)
    ensure_mailboxes(p)
    return p


def _read_log(name: str) -> str:
    return agent_log_path(name).read_text() if agent_log_path(name).is_file() else ""


class TestTellOutboxEnv:
    def test_run_with_prefix_sets_tell_outbox_dir(self, tmp_path, monkeypatch):
        from core import TELL_OUTBOX_DIR_ENV

        agent_root = tmp_path / "a"
        agent_root.mkdir()
        external = tmp_path / "out"
        external.mkdir()
        p = Participant("X", agent_root, outbox=external)
        captured: dict = {}

        class FakeProc:
            stdout = iter([])
            returncode = 0

            def wait(self):
                return 0

            def poll(self):
                return 0

        def fake_popen(cmd, **kwargs):
            captured["env"] = kwargs["env"]
            return FakeProc()

        monkeypatch.setattr("daemon.subprocess.Popen", fake_popen)
        from daemon import _tell_outbox_env, run_with_prefix

        run_with_prefix("X", ["true"], agent_root, env=_tell_outbox_env(p))
        assert captured["env"][TELL_OUTBOX_DIR_ENV] == str(external.resolve())

    def test_wake_env_matches_participant_outbox_path(self, tmp_path):
        from core import TELL_FILE_MAX_ENV, TELL_OUTBOX_DIR_ENV
        from daemon import _tell_outbox_env
        from settings import get_int

        agent_root = tmp_path / "agent"
        agent_root.mkdir()
        external = tmp_path / "mail" / ".outbox"
        p = Participant("X", agent_root, outbox=external)
        assert _tell_outbox_env(p) == {
            TELL_OUTBOX_DIR_ENV: str(external.resolve()),
            TELL_FILE_MAX_ENV: str(get_int("max_file_bytes")),
        }


class TestWakeOnce:
    """End-to-end wake_once exercise via the mock CLI. With the single-`invoke`
    verb every wake produces the same argv shape — the wake line surfaces
    `$SENDER`/`$RECIPIENT`/`$TIMESTAMP`/`$AGE`/`$MESSAGE` for both direct
    sends and alias fan-out. Asserts on lines the mock CLI echoes into the
    per-agent log."""

    def test_routed_message(self, fake_home, tmp_path, fixtures_dir):
        for n in ("A", "B"):
            (tmp_path / n).mkdir()
        save_registry({
            "A": {"root": str(tmp_path / "a"), "definition": str(fixtures_dir / "mock.json")},
            "B": {"root": str(tmp_path / "b"), "definition": str(fixtures_dir / "mock.json")},
        })
        a = Participant("A", tmp_path / "a")
        b = Participant("B", tmp_path / "b")
        ensure_mailboxes(a)
        ensure_mailboxes(b)
        _write_outbox("A", a.root, "B", "design review", [])

        rc = attached_loop(["A", "B"], 0.1, single_pass=True)
        assert rc == 0
        log_b = _read_log("B")
        # The argv was logged via shlex.join before invocation so operators
        # can see the actual prompt that reached the wake subprocess.
        assert "[B] exec: " in log_b
        assert "FROM:A|TO:B|TS:" in log_b
        assert "|MSG:design review" in log_b
        assert "AGE:0 seconds ago" in log_b or "AGE:1 seconds ago" in log_b

    def test_alias_routed_message(self, fake_home, tmp_path, fixtures_dir):
        # Strict opacity (#69, #70): alias-routed messages produce the same
        # shape as direct ones — only `$RECIPIENT` differs (alias name).
        agents = {}
        for n in ("A", "B", "C"):
            d = tmp_path / n; d.mkdir()
            agents[n] = Participant(n, d)
        save_registry({
            n: {"root": str(p.root), "definition": str(fixtures_dir / "mock.json")}
            for n, p in agents.items()
        })
        save_aliases({"devs": ["A", "B", "C"]})
        for p in agents.values():
            ensure_mailboxes(p)

        _write_outbox("A", agents["A"].root, "devs", "all-hands", [])
        rc = attached_loop(["A", "B", "C"], 0.1, single_pass=True)
        assert rc == 0
        for n in ("B", "C"):
            log = _read_log(n)
            assert f"FROM:A|TO:devs|TS:" in log
            assert "|MSG:all-hands" in log
            assert "OTHERS:" not in log
            assert "ALIAS:" not in log


class TestDeclaredWakeEnv:
    """`definition.env` and `wake_path` sit between the handler's own
    environment and the routing variables a8s injects (#121). Both edges
    matter: a node can override what the start shell happened to carry, and
    nothing a node declares can move its own outbox."""

    def _participant(self, tmp_path):
        agent_root = tmp_path / "agent"
        agent_root.mkdir()
        return Participant("X", agent_root, outbox=tmp_path / "mail" / ".outbox")

    def test_declared_env_reaches_the_wake(self, fake_home, tmp_path):
        from daemon import _wake_env

        p = self._participant(tmp_path)
        env = _wake_env(p, {"env": {"PATH": "/node/bin", "LANG": "C"}})
        assert env["PATH"] == "/node/bin"
        assert env["LANG"] == "C"

    def test_routing_env_wins_over_a_declared_override(self, fake_home, tmp_path):
        from daemon import _wake_env

        p = self._participant(tmp_path)
        env = _wake_env(p, {"env": {TELL_OUTBOX_DIR_ENV: "/somewhere/else"}})
        assert env[TELL_OUTBOX_DIR_ENV] == str(p.outbox_path().resolve())

    def test_wake_path_is_the_fallback_when_no_node_declares_one(
        self, fake_home, tmp_path, monkeypatch
    ):
        from daemon import _wake_env

        monkeypatch.setenv("A8S_WAKE_PATH", "/machine/bin")
        p = self._participant(tmp_path)
        assert _wake_env(p, {"invoke": ["x"]})["PATH"] == "/machine/bin"

    def test_the_spawned_process_gets_the_declared_path(self, tmp_path, monkeypatch):
        from daemon import _wake_env, run_with_prefix

        captured: dict = {}

        class FakeProc:
            stdout = iter([])
            returncode = 0

            def wait(self):
                return 0

            def poll(self):
                return 0

        monkeypatch.setattr(
            "daemon.subprocess.Popen",
            lambda cmd, **kwargs: (captured.update(env=kwargs["env"]), FakeProc())[1],
        )
        p = self._participant(tmp_path)
        run_with_prefix("X", ["true"], p.root, env=_wake_env(p, {"env": {"PATH": "/node/bin"}}))
        assert captured["env"]["PATH"] == "/node/bin"
        assert captured["env"][TELL_OUTBOX_DIR_ENV] == str(p.outbox_path().resolve())

    def test_a_malformed_env_aborts_the_wake_instead_of_crashing(
        self, fake_home, tmp_path, fixtures_dir
    ):
        for n in ("A", "B"):
            (tmp_path / n).mkdir()
        bad = tmp_path / "bad-env.json"
        bad.write_text(json.dumps({
            "invoke": ["$PYTHON", "$A8S_DIR/tests/fixtures/mock_cli.py", "MSG:$MESSAGE"],
            "env": "PATH=/usr/bin",
        }))
        save_registry({
            "A": {"root": str(tmp_path / "a"), "definition": str(fixtures_dir / "mock.json")},
            "B": {"root": str(tmp_path / "b"), "definition": str(bad)},
        })
        a = Participant("A", tmp_path / "a")
        b = Participant("B", tmp_path / "b")
        ensure_mailboxes(a)
        ensure_mailboxes(b)
        _write_outbox("A", a.root, "B", "hi", [])

        assert attached_loop(["A", "B"], 0.1, single_pass=True) == 0
        assert "wake aborted" in _read_log("B")
        # Requeued, never dropped: the operator fixes the knob and it delivers.
        assert list(inbox_dir("B").glob("*.json"))


class TestFilesDirContract:
    """PR #137 checklist — wake prompts and files_dir bootstrap."""

    def _mock_def(self, tmp_path: Path, fixtures_dir: Path, *, files_dir: str | None = None) -> Path:
        invoke = [
            "$PYTHON", "$A8S_DIR/tests/fixtures/mock_cli.py",
            "FROM:$SENDER|TO:$RECIPIENT|TS:$TIMESTAMP|AGE:$AGE|MSG:$MESSAGE",
        ]
        body: dict = {"invoke": invoke}
        if files_dir is not None:
            body["files_dir"] = files_dir
        path = tmp_path / "mock-def.json"
        path.write_text(json.dumps(body))
        return path

    def test_wake_prompt_includes_absolute_attached_file_path(
        self, fake_home, tmp_path, fixtures_dir
    ):
        a_root = tmp_path / "a"
        b_root = tmp_path / "b"
        a_root.mkdir()
        b_root.mkdir()
        defn = self._mock_def(tmp_path, fixtures_dir)
        save_registry({
            "A": {"root": str(a_root), "definition": str(defn)},
            "B": {"root": str(b_root), "definition": str(defn)},
        })
        a = Participant("A", a_root)
        b = Participant("B", b_root)
        ensure_mailboxes(a)
        ensure_mailboxes(b)
        payload = a_root / "avatar.jpg"
        payload.write_text("bytes")
        out_path = _write_outbox(
            "A", a_root, "B", "see attached", [],
            attachment_sources=[payload],
        )
        msg_id = out_path.stem
        rc = attached_loop(["A", "B"], 0.1, single_pass=True)
        assert rc == 0
        expected = (b_root / ".files" / msg_id / "avatar.jpg").resolve()
        log_b = _read_log("B")
        assert f"ATTACHED FILE: {expected}" in log_b
        assert "ATTACHED FILE: ./.files" not in log_b

    def test_wake_custom_files_dir_in_attached_file_path(
        self, fake_home, tmp_path, fixtures_dir
    ):
        a_root = tmp_path / "a"
        b_root = tmp_path / "b"
        external = tmp_path / "var" / "attachments" / "bob"
        a_root.mkdir()
        b_root.mkdir()
        defn = self._mock_def(tmp_path, fixtures_dir, files_dir=str(external))
        save_registry({
            "A": {"root": str(a_root), "definition": str(defn)},
            "B": {"root": str(b_root), "definition": str(defn)},
        })
        ensure_mailboxes(Participant("A", a_root))
        ensure_mailboxes(Participant("B", b_root))
        payload = a_root / "avatar.jpg"
        payload.write_text("bytes")
        out_path = _write_outbox(
            "A", a_root, "B", "see attached", [],
            attachment_sources=[payload],
        )
        msg_id = out_path.stem
        rc = attached_loop(["A", "B"], 0.1, single_pass=True)
        assert rc == 0
        expected = (external / msg_id / "avatar.jpg").resolve()
        log_b = _read_log("B")
        assert f"ATTACHED FILE: {expected}" in log_b
        assert (external / msg_id / "avatar.jpg").is_file()

    def test_wake_creates_files_dir_when_missing(
        self, fake_home, tmp_path, fixtures_dir
    ):
        a_root = tmp_path / "a"
        b_root = tmp_path / "b"
        a_root.mkdir()
        b_root.mkdir()
        defn = self._mock_def(tmp_path, fixtures_dir)
        save_registry({
            "A": {"root": str(a_root), "definition": str(defn)},
            "B": {"root": str(b_root), "definition": str(defn)},
        })
        ensure_mailboxes(Participant("A", a_root))
        ensure_mailboxes(Participant("B", b_root))
        _write_outbox("A", a_root, "B", "text only", [])
        assert not files_dir(b_root).exists()
        rc = attached_loop(["A", "B"], 0.1, single_pass=True)
        assert rc == 0
        assert files_dir(b_root).is_dir()


class TestAttachedLoopLifecycle:
    def test_attaches_and_detaches(self, mock_agent):
        # No messages — single_pass attaches, sees nothing, detaches.
        rc = attached_loop(["MOCK"], 0.1, single_pass=True)
        assert rc == 0
        log = _read_log("MOCK")
        assert f"[a8s] MOCK: attached (PID {os.getpid()})" in log
        assert "[a8s] MOCK: detached" in log
        # Pid file released.
        assert not pid_path("MOCK").is_file()


class TestAttachedLoopWithoutSigusr1:
    """Windows has no SIGUSR1. attached_loop must not crash registering or
    restoring a handler for a signal that doesn't exist on the platform —
    simulate that by deleting the attribute, the same way Windows lacks it."""

    def test_attaches_and_detaches_without_sigusr1(self, mock_agent, monkeypatch):
        monkeypatch.delattr(signal, "SIGUSR1", raising=False)
        rc = attached_loop(["MOCK"], 0.1, single_pass=True)
        assert rc == 0
        log = _read_log("MOCK")
        assert f"[a8s] MOCK: attached (PID {os.getpid()})" in log
        assert "[a8s] MOCK: detached" in log
        assert not pid_path("MOCK").is_file()


class TestAttachedLoopDetachRequest:
    """Issue #68 — per-agent take-over. A detach-request file under one of
    our handled agents causes that agent (and only that agent) to be
    released; siblings keep running."""

    def test_releases_only_requested_agent(self, fake_home, tmp_path, fixtures_dir):
        # Two agents A, B. We acquire both, then drop a detach-request for
        # A (from a foreign pid), run one iteration, and verify A is gone
        # while B is still ours.
        for n in ("A", "B"):
            (tmp_path / n).mkdir()
        save_registry({
            "A": {"root": str(tmp_path / "a"), "definition": str(fixtures_dir / "mock.json")},
            "B": {"root": str(tmp_path / "b"), "definition": str(fixtures_dir / "mock.json")},
        })
        for n in ("A", "B"):
            ensure_mailboxes(Participant(n, tmp_path / n))

        # Place the detach-request BEFORE running attached_loop. The first
        # iteration will pick it up, release A, and continue with B.
        detach_request_path("A").parent.mkdir(parents=True, exist_ok=True)
        detach_request_path("A").write_text(str(os.getppid()))

        rc = attached_loop(["A", "B"], 0.1, single_pass=True)
        assert rc == 0
        # A's log captured the release notice.
        assert f"releasing to PID {os.getppid()}" in _read_log("A")
        # B's log shows attached + detached normally.
        assert f"[a8s] B: attached (PID {os.getpid()}" in _read_log("B")
        assert "[a8s] B: detached" in _read_log("B")
        # Both pid files cleaned up at end (B by the finally block, A by the
        # detach-request handling mid-iteration).
        assert not pid_path("A").is_file()
        assert not pid_path("B").is_file()
        # Detach-request file removed too.
        assert not detach_request_path("A").is_file()

    def test_self_request_is_ignored(self, fake_home, tmp_path, fixtures_dir):
        # If our OWN pid is in the detach-request (shouldn't happen, but
        # defense), we don't release ourselves.
        d = tmp_path / "x"; d.mkdir()
        save_registry({
            "X": {"root": str(d), "definition": str(fixtures_dir / "mock.json")},
        })
        ensure_mailboxes(Participant("X", d))

        detach_request_path("X").parent.mkdir(parents=True, exist_ok=True)
        detach_request_path("X").write_text(str(os.getpid()))

        rc = attached_loop(["X"], 0.1, single_pass=True)
        assert rc == 0
        # Did NOT log a release.
        assert "releasing to PID" not in _read_log("X")
        # Normal attached + detached.
        assert "attached (PID" in _read_log("X")
        assert "detached" in _read_log("X")


class TestAttachedLoopKillRequest:
    """Per-agent kill via kill-request file. Same shape as detach-request,
    but logs as 'killed by' and the SIGUSR1 handler interrupts an in-flight
    wake whose target matches."""

    def test_releases_only_killed_agent(self, fake_home, tmp_path, fixtures_dir):
        for n in ("A", "B"):
            (tmp_path / n).mkdir()
        save_registry({
            "A": {"root": str(tmp_path / "a"), "definition": str(fixtures_dir / "mock.json")},
            "B": {"root": str(tmp_path / "b"), "definition": str(fixtures_dir / "mock.json")},
        })
        for n in ("A", "B"):
            ensure_mailboxes(Participant(n, tmp_path / n))

        # Pre-place kill-request for A from a foreign pid.
        _write_kill_request("A", os.getppid())
        rc = attached_loop(["A", "B"], 0.1, single_pass=True)
        assert rc == 0
        # A's log shows 'killed by'; B's does not.
        assert f"killed by PID {os.getppid()}" in _read_log("A")
        assert "killed by" not in _read_log("B")
        # B attached normally.
        assert "B: attached" in _read_log("B")
        # Kill-request file was cleared.
        assert not kill_request_path("A").is_file()

    def test_kill_takes_precedence_over_detach(self, fake_home, tmp_path, fixtures_dir):
        # If both files exist for the same agent, kill wins.
        d = tmp_path / "x"; d.mkdir()
        save_registry({
            "X": {"root": str(d), "definition": str(fixtures_dir / "mock.json")},
        })
        ensure_mailboxes(Participant("X", d))

        _write_detach_request("X", os.getppid())
        _write_kill_request("X", os.getppid())

        rc = attached_loop(["X"], 0.1, single_pass=True)
        assert rc == 0
        log = _read_log("X")
        assert "killed by" in log
        assert "releasing to PID" not in log

    def test_self_kill_request_is_ignored(self, fake_home, tmp_path, fixtures_dir):
        d = tmp_path / "x"; d.mkdir()
        save_registry({
            "X": {"root": str(d), "definition": str(fixtures_dir / "mock.json")},
        })
        ensure_mailboxes(Participant("X", d))

        _write_kill_request("X", os.getpid())
        rc = attached_loop(["X"], 0.1, single_pass=True)
        assert rc == 0
        assert "killed by" not in _read_log("X")

    def test_polled_branch_kills_inflight_wake_without_sigusr1(
        self, fake_home, tmp_path, fixtures_dir, monkeypatch
    ):
        """#2/amendment 2: the group kill must happen from the iteration-top
        kill-request branch itself, not only from the SIGUSR1 handler — that
        is the only path Windows has, since it has no SIGUSR1 to nudge with.
        No signal is sent here (monkeypatching `threading.Event.wait`, same
        as `test_max_wake_seconds_kills_hung_subprocess` above, only ever
        writes the kill-request *file*), so a subprocess that dies proves
        the polled branch performs the kill on its own."""
        import daemon as daemon_mod

        monkeypatch.setenv("MOCK_SLEEP", "5")
        d = tmp_path / "a"
        d.mkdir()
        save_registry({"A": {"root": str(d), "definition": str(fixtures_dir / "mock-slow.json")}})
        ensure_mailboxes(Participant("A", d))

        from ark.ulid import new as new_ulid

        msg_id = new_ulid()
        (inbox_dir("A") / f"{msg_id}.json").write_text(
            json.dumps({
                "id": msg_id,
                "date": "2026-04-29T12:00:00Z",
                "from": "Y",
                "to": "A",
                "content": "hang",
                "files": [],
            })
        )

        foreign_pid = os.getppid()
        captured: dict[str, int] = {}
        written = False

        def write_kill_request_once(self, timeout=None):
            nonlocal written
            if not written and daemon_mod._CURRENT_WAKE_PROC is not None:
                written = True
                captured["proc"] = daemon_mod._CURRENT_WAKE_PROC
                _write_kill_request("A", foreign_pid)
            return False

        monkeypatch.setattr(threading.Event, "wait", _main_thread_only(write_kill_request_once))

        started = time.monotonic()
        rc = attached_loop(["A"], 0.05, single_pass=False)
        elapsed = time.monotonic() - started

        assert rc == 0
        assert "proc" in captured
        assert f"killed by PID {foreign_pid}" in _read_log("A")
        # The kill actually reaped the subprocess rather than merely releasing
        # the pid file. Asked of the Popen itself rather than by probing the
        # pid: `os.kill(pid, 0)` raising ProcessLookupError is POSIX-shaped —
        # Windows' os.kill routes through TerminateProcess and does not answer
        # that question — and a returncode is the stronger claim anyway, since
        # it can only be set once the process exited AND was waited on.
        assert captured["proc"].poll() is not None
        # And it happened well inside the 5s MOCK_SLEEP, not because the
        # mock CLI ran to completion on its own.
        assert elapsed < 3.0

    def test_multi_agent_share_one_pid(self, fake_home, tmp_path, fixtures_dir):
        # Two agents, one process — both pid files point at this pytest process.
        for n in ("A", "B"):
            (tmp_path / n).mkdir()
        save_registry({
            n: {"root": str(tmp_path / n.lower()), "definition": str(fixtures_dir / "mock.json")}
            for n in ("A", "B")
        })
        # Use different roots for A vs B.
        save_registry({
            "A": {"root": str(tmp_path / "A"), "definition": str(fixtures_dir / "mock.json")},
            "B": {"root": str(tmp_path / "B"), "definition": str(fixtures_dir / "mock.json")},
        })
        for n in ("A", "B"):
            ensure_mailboxes(Participant(n, tmp_path / n))

        rc = attached_loop(["A", "B"], 0.1, single_pass=True)
        assert rc == 0
        # Both agents attached + detached from the same pytest pid.
        assert "shared" in _read_log("A")
        assert "shared" in _read_log("B")
        assert not pid_path("A").is_file()
        assert not pid_path("B").is_file()


# ---------- idle invoke ----------

def _write_idle_def(path: Path, fixtures_dir: Path, timeout: int) -> None:
    """Write a definition that wakes via mock_cli.py on tells AND has an
    idle.invoke that prints a distinguishable string. The idle command's
    argv echoes 'IDLE-FIRED-FOR:$RECIPIENT' so we can grep the per-agent
    log to assert it ran."""
    path.write_text(json.dumps({
        "invoke": [
            sys.executable,
            f"{fixtures_dir}/mock_cli.py",
            "FROM:$SENDER|TO:$RECIPIENT|TS:$TIMESTAMP|AGE:$AGE|MSG:$MESSAGE",
        ],
        "idle": {
            "timeout": timeout,
            "invoke": [
                sys.executable,
            f"{fixtures_dir}/mock_cli.py",
                "IDLE-FIRED-FOR:$RECIPIENT",
            ],
        },
    }))


class TestMaybeRunIdle:
    """`maybe_run_idle` is the per-iteration check `attached_loop` calls
    for each handled agent after draining the inbox. It reads
    `last-active`, computes elapsed, and fires `idle.invoke` iff the
    agent has been quiet long enough."""

    def test_returns_false_when_no_idle_config(self, fake_home, tmp_path, fixtures_dir):
        d = tmp_path / "X"; d.mkdir()
        save_registry({"X": {"root": str(d), "definition": str(fixtures_dir / "mock.json")}})
        ensure_mailboxes(Participant("X", d))
        # mock.json has no `idle` block.
        assert maybe_run_idle(Participant("X", d)) is False

    def test_initializes_last_active_when_missing(self, fake_home, tmp_path, fixtures_dir):
        from core import last_active_path, read_last_active
        d = tmp_path / "X"; d.mkdir()
        defp = tmp_path / "idle.json"
        _write_idle_def(defp, fixtures_dir, timeout=60)
        save_registry({"X": {"root": str(d), "definition": str(defp)}})
        ensure_mailboxes(Participant("X", d))
        # No last-active file yet — first call seeds it and does NOT fire.
        assert not last_active_path("X").is_file()
        fired = maybe_run_idle(Participant("X", d))
        assert fired is False
        assert read_last_active("X") is not None

    def test_skips_when_not_yet_idle_long_enough(self, fake_home, tmp_path, fixtures_dir):
        from core import touch_last_active
        from datetime import datetime, timezone, timedelta
        d = tmp_path / "X"; d.mkdir()
        defp = tmp_path / "idle.json"
        _write_idle_def(defp, fixtures_dir, timeout=300)
        save_registry({"X": {"root": str(d), "definition": str(defp)}})
        ensure_mailboxes(Participant("X", d))
        # Last active 10 seconds ago; timeout is 300.
        touch_last_active("X", datetime.now(timezone.utc) - timedelta(seconds=10))
        assert maybe_run_idle(Participant("X", d)) is False

    def test_fires_when_elapsed_exceeds_timeout(self, fake_home, tmp_path, fixtures_dir):
        from core import touch_last_active, read_last_active
        from datetime import datetime, timezone, timedelta
        d = tmp_path / "X"; d.mkdir()
        defp = tmp_path / "idle.json"
        _write_idle_def(defp, fixtures_dir, timeout=1)
        save_registry({"X": {"root": str(d), "definition": str(defp)}})
        ensure_mailboxes(Participant("X", d))
        before = datetime.now(timezone.utc)
        touch_last_active("X", before - timedelta(seconds=60))
        fired = maybe_run_idle(Participant("X", d))
        assert fired is True
        # Log must show the idle invoke ran.
        log = _read_log("X")
        assert "idle exec:" in log
        assert "IDLE-FIRED-FOR:X" in log
        # last-active was refreshed to ~now after the run.
        got = read_last_active("X")
        assert got is not None
        assert got >= before

    def test_zero_timeout_disables_idle(self, fake_home, tmp_path, fixtures_dir):
        d = tmp_path / "X"; d.mkdir()
        defp = tmp_path / "idle.json"
        _write_idle_def(defp, fixtures_dir, timeout=0)
        save_registry({"X": {"root": str(d), "definition": str(defp)}})
        ensure_mailboxes(Participant("X", d))
        # Even with no last-active, timeout<=0 means idle is off.
        assert maybe_run_idle(Participant("X", d)) is False


class TestAttachedLoopIdleIntegration:
    """End-to-end: attached_loop's iteration must call maybe_run_idle for
    every handled agent after the inbox drain. With single_pass=True we
    can prep last-active to look "stale" and verify the idle invoke fires
    on the very first iteration."""

    def test_idle_fires_after_drain(self, fake_home, tmp_path, fixtures_dir):
        from core import touch_last_active
        from datetime import datetime, timezone, timedelta
        d = tmp_path / "X"; d.mkdir()
        defp = tmp_path / "idle.json"
        _write_idle_def(defp, fixtures_dir, timeout=1)
        save_registry({"X": {"root": str(d), "definition": str(defp)}})
        ensure_mailboxes(Participant("X", d))
        # Stale last-active so idle should fire this pass.
        touch_last_active("X", datetime.now(timezone.utc) - timedelta(seconds=60))

        rc = attached_loop(["X"], 0.1, single_pass=True)
        assert rc == 0
        log = _read_log("X")
        assert "idle exec:" in log
        assert "IDLE-FIRED-FOR:X" in log

    def test_wake_refreshes_last_active_so_idle_doesnt_fire(self, fake_home, tmp_path, fixtures_dir):
        # If a real wake happened this iteration, last-active was just
        # touched at wake_once time — idle should NOT fire.
        d = tmp_path / "X"; d.mkdir()
        defp = tmp_path / "idle.json"
        _write_idle_def(defp, fixtures_dir, timeout=1)
        save_registry({"X": {"root": str(d), "definition": str(defp)}})
        ensure_mailboxes(Participant("X", d))
        # Drop a self-tell so there's an inbox message to drain. We can't
        # tell ourselves through routing (sender exclusion), so write the
        # routed message directly into the inbox.
        from ark.ulid import new as new_ulid
        msg_id = new_ulid()
        (inbox_dir("X") / f"{msg_id}.json").write_text(json.dumps({
            "id": msg_id,
            "date": "2026-04-29T12:00:00Z",
            "from": "Y",
            "to": "X",
            "content": "wake-test",
            "files": [],
        }))

        rc = attached_loop(["X"], 0.1, single_pass=True)
        assert rc == 0
        log = _read_log("X")
        # Wake fired (mock_cli.py received the message).
        assert "MSG:wake-test" in log
        # Idle did NOT fire — wake_once just touched last-active.
        assert "idle exec:" not in log
        assert "IDLE-FIRED-FOR" not in log


class TestIdleFairness:
    """The idle pass breaks on the first started invoke, so from a fixed
    index-0 start an agent that is always ready takes every idle slot and
    its siblings never get checked. Equal timeouts are self-limiting —
    firing refreshes last-active — but an agent whose clock keeps expiring
    first is not."""

    def test_an_always_ready_agent_does_not_own_every_idle_slot(
        self, fake_home, tmp_path, fixtures_dir, monkeypatch
    ):
        from core import touch_last_active
        from datetime import datetime, timezone, timedelta
        import daemon as daemon_mod

        reg = {}
        for name in ("A", "B"):
            root = tmp_path / name.lower()
            root.mkdir()
            defp = tmp_path / f"idle-{name}.json"
            _write_idle_def(defp, fixtures_dir, timeout=1)
            reg[name] = {"root": str(root), "definition": str(defp)}
        save_registry(reg)
        for name in ("A", "B"):
            ensure_mailboxes(Participant(name, tmp_path / name.lower()))

        touch_last_active("A", datetime.now(timezone.utc) - timedelta(seconds=600))
        touch_last_active("B", datetime.now(timezone.utc) - timedelta(seconds=600))

        # A's clock always reads expired — the shape a much shorter
        # idle.timeout produces against slower siblings. Patched at the read
        # rather than the file, so firing cannot quietly make A unready and
        # hand B the slot for a reason other than rotation.
        real_read = daemon_mod.read_last_active

        def a_is_always_overdue(name):
            if name == "A":
                return datetime.now(timezone.utc) - timedelta(seconds=600)
            return real_read(name)

        monkeypatch.setattr(daemon_mod, "read_last_active", a_is_always_overdue)

        fired: list[str] = []
        orig = daemon_mod.maybe_run_idle

        def track_idle(p, *, async_wake=False):
            ran = orig(p, async_wake=async_wake)
            if ran:
                fired.append(p.name)
            return ran

        monkeypatch.setattr(daemon_mod, "maybe_run_idle", track_idle)

        passes = 0

        def keep_a_always_ready(_event, timeout=None):
            nonlocal passes
            passes += 1
            if "B" in fired or passes > 12:
                daemon_mod._STOP_EVENT.set()
            return True

        monkeypatch.setattr(threading.Event, "wait", _main_thread_only(keep_a_always_ready))
        attached_loop(["A", "B"], 0.01, single_pass=False)

        assert "B" in fired, f"A took every idle slot; fired={fired}"


class TestBatchWake:
    def _queue_inbox(self, recipient: str, n: int, *, prefix: str = "msg") -> list[Path]:
        paths: list[Path] = []
        for i in range(n):
            msg = {
                "id": f"{prefix}{i}",
                "date": "2026-04-28T14:30:00.000000Z",
                "from": "A",
                "to": recipient,
                "content": f"{prefix}-{i}",
                "files": [],
            }
            p = inbox_dir(recipient) / f"{prefix}{i}.json"
            p.write_text(json.dumps(msg))
            paths.append(p)
        return paths

    def test_three_messages_batch_wake(self, fake_home, tmp_path, fixtures_dir):
        d = tmp_path / "b"
        d.mkdir()
        save_registry({
            "B": {"root": str(d), "definition": str(fixtures_dir / "mock-batch.json")},
        })
        ensure_mailboxes(Participant("B", d))
        self._queue_inbox("B", 3)

        rc = attached_loop(["B"], 0.1, single_pass=True)
        assert rc == 0
        log = _read_log("B")
        assert log.count("batch exec:") == 1
        assert "BATCH|TO:B" in log
        assert log.count("MOCK-CLI: BATCH|TO:B") == 1
        assert log.count("SINGLE|") == 0
        # The daemon composes one prompt from all 3 envelopes (not raw file
        # paths) — every message body shows up, plus the shared header.
        assert "receiving messages as 'B'" in log
        for i in range(3):
            assert f"msg-{i}" in log

    def test_unreadable_envelope_gets_visible_placeholder(self, fake_home, tmp_path, fixtures_dir):
        # One malformed file among otherwise-good ones must never be silently
        # dropped — it shows up as a placeholder block in the composed prompt
        # instead of vanishing (or, pre-fix, poisoning the whole batch).
        d = tmp_path / "b"
        d.mkdir()
        save_registry({
            "B": {"root": str(d), "definition": str(fixtures_dir / "mock-batch.json")},
        })
        ensure_mailboxes(Participant("B", d))
        self._queue_inbox("B", 2)
        (inbox_dir("B") / "corrupt.json").write_text("{not json")

        rc = attached_loop(["B"], 0.1, single_pass=True)
        assert rc == 0
        log = _read_log("B")
        assert log.count("batch exec:") == 1
        assert "msg-0" in log and "msg-1" in log
        assert "unreadable message file corrupt.json" in log

    def test_single_message_uses_normal_invoke(self, fake_home, tmp_path, fixtures_dir):
        d = tmp_path / "b"
        d.mkdir()
        save_registry({
            "B": {"root": str(d), "definition": str(fixtures_dir / "mock-batch.json")},
        })
        ensure_mailboxes(Participant("B", d))
        self._queue_inbox("B", 1)

        attached_loop(["B"], 0.1, single_pass=True)
        log = _read_log("B")
        assert "batch exec:" not in log
        assert "SINGLE|FROM:A|TO:B|MSG:msg-0" in log

    def test_without_batch_block_one_wake_per_message(self, fake_home, tmp_path, fixtures_dir):
        d = tmp_path / "b"
        d.mkdir()
        save_registry({
            "B": {"root": str(d), "definition": str(fixtures_dir / "mock.json")},
        })
        ensure_mailboxes(Participant("B", d))
        self._queue_inbox("B", 3, prefix="solo")

        attached_loop(["B"], 0.1, single_pass=True)
        log = _read_log("B")
        assert "batch exec:" not in log
        assert log.count("[B] exec: ") == 3

    def test_limit_caps_batch_then_drains_remainder(self, fake_home, tmp_path, fixtures_dir):
        d = tmp_path / "b"
        d.mkdir()
        defn = {
            "pause": 0,
            "invoke": ["$PYTHON", "$A8S_DIR/tests/fixtures/mock_cli.py", "SINGLE"],
            "batch": {
                "invoke": ["$PYTHON", "$A8S_DIR/tests/fixtures/mock_cli.py", "BATCH"],
                "limit": 5,
            },
        }
        defp = tmp_path / "batch5.json"
        defp.write_text(json.dumps(defn))
        save_registry({"B": {"root": str(d), "definition": str(defp)}})
        ensure_mailboxes(Participant("B", d))
        self._queue_inbox("B", 7, prefix="q")

        attached_loop(["B"], 0.1, single_pass=True)
        log = _read_log("B")
        assert log.count("batch exec:") == 2
        # First batch: files are queued/consumed in name order (q0..q6), so
        # the 5-cap takes q-0..q-4 and the drained remainder is q-5/q-6.
        first_batch = log.split("batch exec:")[1].split("batch exec:")[0]
        for i in range(5):
            assert f"q-{i}" in first_batch
        second_batch = log.split("batch exec:")[2]
        for i in (5, 6):
            assert f"q-{i}" in second_batch


class TestPauseBeforeWake:
    T0 = datetime(2026, 4, 28, 14, 30, 0, tzinfo=timezone.utc)

    def _defn(self, *, pause: float = 5, limit: int = 5) -> dict:
        return {
            "pause": pause,
            "invoke": ["x"],
            "batch": {"invoke": ["y"], "limit": limit},
        }

    def _agent(self, tmp_path, name: str = "X") -> Participant:
        d = tmp_path / name.lower()
        d.mkdir()
        p = Participant(name, d)
        ensure_mailboxes(p)
        clear_inbox_waiting_since(name)
        return p

    def _drop(self, name: str, msg_id: str, *, mtime: datetime | None = None) -> Path:
        path = inbox_dir(name) / f"{msg_id}.json"
        path.write_text(json.dumps({
            "id": msg_id, "date": "2026-04-28T14:30:00Z",
            "from": "A", "to": name, "content": msg_id, "files": [],
        }))
        if mtime is not None:
            ts = mtime.timestamp()
            os.utime(path, (ts, ts))
        return path

    def _batch_pause_def(
        self, tmp_path, fixtures_dir, pause: float, *, limit: int = 5
    ) -> Path:
        defn = {
            "pause": pause,
            "invoke": ["$PYTHON", "$A8S_DIR/tests/fixtures/mock_cli.py", "SINGLE"],
            "batch": {
                "invoke": ["$PYTHON", "$A8S_DIR/tests/fixtures/mock_cli.py", "BATCH|TO:$RECIPIENT"],
                "limit": limit,
            },
        }
        defp = tmp_path / "pause-batch.json"
        defp.write_text(json.dumps(defn))
        return defp

    def test_pause_ready_immediate_when_zero(self, fake_home, tmp_path):
        p = self._agent(tmp_path)
        assert _pause_ready_for_wake(p, self._defn(pause=0), now=self.T0) is True

    def test_second_message_resets_quiet_window(self, fake_home, tmp_path):
        p = self._agent(tmp_path)
        self._drop("X", "m0", mtime=self.T0)
        # First message is already older than pause, but a second lands later.
        self._drop("X", "m1", mtime=self.T0 + timedelta(seconds=4))
        # More than pause since FIRST, but not since newest → not ready.
        assert _pause_ready_for_wake(
            p, self._defn(pause=5), now=self.T0 + timedelta(seconds=6)
        ) is False
        # Quiet since newest → ready.
        assert _pause_ready_for_wake(
            p, self._defn(pause=5), now=self.T0 + timedelta(seconds=9)
        ) is True

    def test_quiet_elapsed_since_newest_is_ready(self, fake_home, tmp_path):
        p = self._agent(tmp_path)
        self._drop("X", "m0", mtime=self.T0)
        assert _pause_ready_for_wake(
            p, self._defn(pause=5), now=self.T0 + timedelta(seconds=5)
        ) is True

    def test_batch_limit_escapes_pause(self, fake_home, tmp_path):
        p = self._agent(tmp_path)
        just_now = self.T0
        for i in range(5):
            self._drop("X", f"m{i}", mtime=just_now)
        assert _pause_ready_for_wake(
            p, self._defn(pause=60, limit=5), now=just_now
        ) is True
        assert "5 waiting at the limit, waking now" in _read_log("X")

    def test_empty_inbox_is_ready(self, fake_home, tmp_path):
        p = self._agent(tmp_path)
        assert _pause_ready_for_wake(p, self._defn(pause=5), now=self.T0) is True

    def test_waiting_log_emitted_once_across_polls(self, fake_home, tmp_path):
        p = self._agent(tmp_path)
        self._drop("X", "m0", mtime=self.T0)
        defn = self._defn(pause=5)
        assert _pause_ready_for_wake(p, defn, now=self.T0) is False
        assert _pause_ready_for_wake(
            p, defn, now=self.T0 + timedelta(seconds=1)
        ) is False
        assert _pause_ready_for_wake(
            p, defn, now=self.T0 + timedelta(seconds=2)
        ) is False
        assert _read_log("X").count("pause 5s before wake") == 1

    def test_pause_defers_wake_until_quiet(self, fake_home, tmp_path, fixtures_dir):
        d = tmp_path / "b"
        d.mkdir()
        defp = self._batch_pause_def(tmp_path, fixtures_dir, pause=60)
        save_registry({"B": {"root": str(d), "definition": str(defp)}})
        ensure_mailboxes(Participant("B", d))
        clear_inbox_waiting_since("B")
        self._drop("B", "m0")

        attached_loop(["B"], 0.1, single_pass=True)
        log = _read_log("B")
        assert "exec:" not in log
        assert "batch exec:" not in log
        assert "pause 60s before wake" in log

    def test_pause_after_quiet_batches_all(self, fake_home, tmp_path, fixtures_dir):
        d = tmp_path / "b"
        d.mkdir()
        defp = self._batch_pause_def(tmp_path, fixtures_dir, pause=2)
        save_registry({"B": {"root": str(d), "definition": str(defp)}})
        ensure_mailboxes(Participant("B", d))
        clear_inbox_waiting_since("B")
        old = datetime.now(timezone.utc) - timedelta(seconds=10)
        for i in range(3):
            self._drop("B", f"late{i}", mtime=old)

        attached_loop(["B"], 0.1, single_pass=True)
        log = _read_log("B")
        assert log.count("batch exec:") == 1
        assert "SINGLE" not in log


class TestAsyncAttachedLoop:
    def test_route_outboxes_runs_during_in_flight_wake(
        self, fake_home, tmp_path, fixtures_dir, monkeypatch
    ):
        import daemon as daemon_mod

        for sub in ("a", "b"):
            (tmp_path / sub).mkdir()
        save_registry({
            "A": {
                "root": str(tmp_path / "a"),
                "definition": str(fixtures_dir / "mock-slow.json"),
            },
            "B": {
                "root": str(tmp_path / "b"),
                "definition": str(fixtures_dir / "mock.json"),
            },
        })
        a = Participant("A", tmp_path / "a")
        b = Participant("B", tmp_path / "b")
        ensure_mailboxes(a)
        ensure_mailboxes(b)

        from ark.ulid import new as new_ulid

        msg_id = new_ulid()
        (inbox_dir("A") / f"{msg_id}.json").write_text(
            json.dumps({
                "id": msg_id,
                "date": "2026-04-29T12:00:00Z",
                "from": "Y",
                "to": "A",
                "content": "slow-wake",
                "files": [],
            })
        )

        route_counts: list[float] = []
        orig_route = route_outboxes

        def counting_route(*args, **kwargs):
            route_counts.append(time.monotonic())
            return orig_route(*args, **kwargs)

        monkeypatch.setattr("daemon.route_outboxes", counting_route)

        wait_calls = 0

        def stop_after_wake_started(self, timeout=None):
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls >= 2:
                _write_outbox("A", a.root, "B", "during-wake", [])
            if wait_calls >= 8 or any(inbox_dir("B").glob("*.json")):
                if daemon_mod._STOP_EVENT is not None:
                    daemon_mod._STOP_EVENT.set()
            return True

        monkeypatch.setattr(threading.Event, "wait", _main_thread_only(stop_after_wake_started))

        attached_loop(["A", "B"], 0.05, single_pass=False)

        assert len(route_counts) >= 2
        assert any(inbox_dir("B").glob("*.json"))

    def test_max_wake_seconds_kills_hung_subprocess(
        self, fake_home, tmp_path, fixtures_dir, monkeypatch
    ):
        import daemon as daemon_mod

        monkeypatch.setenv("MOCK_SLEEP", "5")
        d = tmp_path / "a"
        d.mkdir()
        defp = tmp_path / "slow-max.json"
        defp.write_text(json.dumps({
            "invoke": [sys.executable, str(fixtures_dir / "mock_slow_cli.py"), "MSG:$MESSAGE"],
            "max_wake_seconds": 0.25,
        }))
        save_registry({"A": {"root": str(d), "definition": str(defp)}})
        ensure_mailboxes(Participant("A", d))

        from ark.ulid import new as new_ulid

        msg_id = new_ulid()
        (inbox_dir("A") / f"{msg_id}.json").write_text(
            json.dumps({
                "id": msg_id,
                "date": "2026-04-29T12:00:00Z",
                "from": "Y",
                "to": "A",
                "content": "hang",
                "files": [],
            })
        )

        wait_calls = 0

        def stop_when_killed(self, timeout=None):
            nonlocal wait_calls
            wait_calls += 1
            log = _read_log("A")
            if "max wake time" in log or wait_calls >= 30:
                if daemon_mod._STOP_EVENT is not None:
                    daemon_mod._STOP_EVENT.set()
            return True

        monkeypatch.setattr(threading.Event, "wait", _main_thread_only(stop_when_killed))

        attached_loop(["A"], 0.05, single_pass=False)

        log = _read_log("A")
        assert "max wake time" in log
        assert "0.25" in log


class TestSharedHandlerStarvation:
    """Issue #163 — one agent's in-flight wake must not freeze delivery to the
    other agents a shared handler serves."""

    def _queue(self, name: str, content: str) -> str:
        from ark.ulid import new as new_ulid

        msg_id = new_ulid()
        (inbox_dir(name) / f"{msg_id}.json").write_text(
            json.dumps({
                "id": msg_id,
                "date": "2026-04-29T12:00:00Z",
                "from": "Y",
                "to": name,
                "content": content,
                "files": [],
            })
        )
        return msg_id

    def _setup(self, tmp_path, fixtures_dir, sibling_def: Path) -> Path:
        for sub in ("a", "b"):
            (tmp_path / sub).mkdir()
        save_registry({
            "A": {
                "root": str(tmp_path / "a"),
                "definition": str(fixtures_dir / "mock-slow.json"),
            },
            "B": {
                "root": str(tmp_path / "b"),
                "definition": str(sibling_def),
            },
        })
        ensure_mailboxes(Participant("A", tmp_path / "a"))
        ensure_mailboxes(Participant("B", tmp_path / "b"))
        self._queue("A", "slow-wake")
        return tmp_path / "b" / ".inbox"

    def test_file_proxy_delivery_runs_during_in_flight_wake(
        self, fake_home, tmp_path, fixtures_dir, monkeypatch
    ):
        import daemon as daemon_mod

        monkeypatch.setenv("MOCK_SLEEP", "3")
        defp = tmp_path / "proxy.json"
        defp.write_text(json.dumps({"proxy": "file", "idle": {"timeout": 30}}))
        proxy_inbox = self._setup(tmp_path, fixtures_dir, defp)

        in_flight_at_delivery: list[bool] = []
        sent = False
        wait_calls = 0

        def stop_when_delivered(self, timeout=None):
            nonlocal sent, wait_calls
            wait_calls += 1
            if not sent:
                if daemon_mod._wake_in_flight():
                    _write_outbox("A", tmp_path / "a", "B", "mid-wake", [])
                    sent = True
            elif not in_flight_at_delivery and list(proxy_inbox.glob("*.json")):
                in_flight_at_delivery.append(daemon_mod._wake_in_flight())
                daemon_mod._STOP_EVENT.set()
            if wait_calls >= 500:
                daemon_mod._STOP_EVENT.set()
            return True

        monkeypatch.setattr(threading.Event, "wait", _main_thread_only(stop_when_delivered))
        attached_loop(["A", "B"], 0.05, single_pass=False)

        assert "exec:" in _read_log("A")
        assert in_flight_at_delivery == [True]
        bodies = [json.loads(f.read_text())["content"] for f in proxy_inbox.glob("*.json")]
        assert bodies == ["mid-wake"]

    def test_sibling_wake_still_waits_for_the_single_wake_slot(
        self, fake_home, tmp_path, fixtures_dir, monkeypatch
    ):
        import daemon as daemon_mod

        monkeypatch.setenv("MOCK_SLEEP", "3")
        self._setup(tmp_path, fixtures_dir, fixtures_dir / "mock.json")
        self._queue("B", "for-sibling")

        saw_in_flight = False
        wait_calls = 0

        def stop_once_wake_started(self, timeout=None):
            nonlocal saw_in_flight, wait_calls
            wait_calls += 1
            if daemon_mod._wake_in_flight():
                saw_in_flight = True
            if (saw_in_flight and wait_calls >= 5) or wait_calls >= 60:
                daemon_mod._STOP_EVENT.set()
            return True

        monkeypatch.setattr(threading.Event, "wait", _main_thread_only(stop_once_wake_started))
        attached_loop(["A", "B"], 0.05, single_pass=False)

        assert saw_in_flight
        assert "exec:" not in _read_log("B")


class TestSharedHandlerWakeFairness:
    """Issue #20 — shared-handler wake start rotates across attached-loop
    iterations so a busy early agent cannot starve siblings. `a8s step`
    (single_pass) stays index-0 ordered."""

    def _queue(self, name: str, content: str) -> None:
        from ark.ulid import new as new_ulid

        msg_id = new_ulid()
        (inbox_dir(name) / f"{msg_id}.json").write_text(
            json.dumps({
                "id": msg_id,
                "date": "2026-04-29T12:00:00Z",
                "from": "Y",
                "to": name,
                "content": content,
                "files": [],
            })
        )

    def _register(self, tmp_path, fixtures_dir, names: tuple[str, ...]) -> dict[str, Participant]:
        reg = {}
        agents = {}
        for name in names:
            root = tmp_path / name.lower()
            root.mkdir()
            reg[name] = {
                "root": str(root),
                "definition": str(fixtures_dir / "mock.json"),
            }
            agents[name] = Participant(name, root)
        save_registry(reg)
        for p in agents.values():
            ensure_mailboxes(p)
        return agents

    def test_busy_early_agent_does_not_starve_sibling(
        self, fake_home, tmp_path, fixtures_dir, monkeypatch
    ):
        import daemon as daemon_mod
        from mailbox import peek_inbox_messages

        agents = self._register(tmp_path, fixtures_dir, ("A", "B", "C"))
        queue = self._queue
        queue("A", "busy-0")
        queue("C", "for-late-sibling")

        woke: list[str] = []
        orig = daemon_mod.wake_once

        def track_wake(p, msg_path, *, async_wake=False):
            woke.append(p.name)
            return orig(p, msg_path, async_wake=async_wake)

        monkeypatch.setattr(daemon_mod, "wake_once", track_wake)

        wait_calls = 0
        # Without rotation A claims every free slot forever. With round-robin
        # start, C must appear in `woke` within len(handled) free-slot turns.
        max_waits = 200

        def keep_a_busy_stop_when_c_wakes(self, timeout=None):
            nonlocal wait_calls
            wait_calls += 1
            if not peek_inbox_messages(agents["A"], 1):
                queue("A", f"busy-{wait_calls}")
            if "C" in woke or wait_calls >= max_waits:
                daemon_mod._STOP_EVENT.set()
            return True

        monkeypatch.setattr(threading.Event, "wait", _main_thread_only(keep_a_busy_stop_when_c_wakes))
        attached_loop(["A", "B", "C"], 0.01, single_pass=False)

        assert "C" in woke
        # C is index 2; after A's first wake the start offset is 1, so the
        # next free slot tries B (empty) then C — at most a handful of A
        # wakes before C, never an unbounded run of A-only.
        assert woke.index("C") <= 3

    def test_a_quiet_period_does_not_spin_the_rotation(
        self, fake_home, tmp_path, fixtures_dir, monkeypatch
    ):
        """Rotation is "next after the one that woke". An idle pass wakes
        nobody, so it must not advance — otherwise the counter spins through
        every quiet iteration and which of two agents mailed at the same
        moment goes first is decided by how long the lull happened to be."""
        import daemon as daemon_mod

        self._register(tmp_path, fixtures_dir, ("A", "B"))
        queue = self._queue
        woke: list[str] = []
        orig = daemon_mod.wake_once

        def track_wake(p, msg_path, *, async_wake=False):
            woke.append(p.name)
            return orig(p, msg_path, async_wake=async_wake)

        monkeypatch.setattr(daemon_mod, "wake_once", track_wake)

        # An odd number of quiet passes, so a counter that spun would hand the
        # slot to B and an unspun one still starts at A.
        quiet_passes = 3
        calls = 0

        def mail_both_after_a_lull(_event, timeout=None):
            nonlocal calls
            calls += 1
            if calls == quiet_passes:
                queue("A", "same moment")
                queue("B", "same moment")
            if woke or calls > quiet_passes + 5:
                daemon_mod._STOP_EVENT.set()
            return True

        monkeypatch.setattr(threading.Event, "wait", _main_thread_only(mail_both_after_a_lull))
        attached_loop(["A", "B"], 0.01, single_pass=False)

        assert woke[:1] == ["A"], (
            f"three idle passes moved the rotation; first wake was {woke[:1]}"
        )

    def test_step_wakes_in_handler_index_order(
        self, fake_home, tmp_path, fixtures_dir, monkeypatch
    ):
        import daemon as daemon_mod

        self._register(tmp_path, fixtures_dir, ("A", "B", "C"))
        for name in ("A", "B", "C"):
            self._queue(name, f"for-{name}")

        woke: list[str] = []
        orig = daemon_mod.wake_once

        def track(p, msg_path, *, async_wake=False):
            woke.append(p.name)
            return orig(p, msg_path, async_wake=async_wake)

        monkeypatch.setattr(daemon_mod, "wake_once", track)
        rc = attached_loop(["A", "B", "C"], 0.1, single_pass=True)
        assert rc == 0
        assert woke == ["A", "B", "C"]

    def test_attached_loop_rotates_wake_start_across_iterations(
        self, fake_home, tmp_path, fixtures_dir, monkeypatch
    ):
        import daemon as daemon_mod
        from mailbox import peek_inbox_messages

        agents = self._register(tmp_path, fixtures_dir, ("A", "B"))
        queue = self._queue
        queue("A", "a0")
        queue("B", "b0")

        woke: list[str] = []

        def instant_wake(p, msg_path, *, async_wake=False):
            # A real subprocess makes strict alternation a race: on a loaded
            # runner a wake's completion can slip a pass, an agent can be
            # transiently unready at its turn, and the sibling legitimately
            # goes twice. That fairness-under-timing property has its own
            # test (the starvation bound below); THIS test asserts the
            # rotation arithmetic, so the wake is a no-op consume — every
            # pass dispatches exactly one agent, deterministically.
            woke.append(p.name)
            msg_path.rename(unique_path(trash_dir(p.name) / msg_path.name))
            return True

        monkeypatch.setattr(daemon_mod, "wake_once", instant_wake)

        wait_calls = 0

        def refill_and_stop(self, timeout=None):
            nonlocal wait_calls
            wait_calls += 1
            for name, p in agents.items():
                if not peek_inbox_messages(p, 1):
                    queue(name, f"more-{name}-{wait_calls}")
            if len(woke) >= 4 or wait_calls >= 200:
                daemon_mod._STOP_EVENT.set()
            return True

        monkeypatch.setattr(threading.Event, "wait", _main_thread_only(refill_and_stop))
        attached_loop(["A", "B"], 0.01, single_pass=False)

        assert len(woke) >= 4
        assert woke[:4] == ["A", "B", "A", "B"]

    def test_a_skip_advances_past_the_woken_agent_not_the_skipped_one(
        self, fake_home, tmp_path, fixtures_dir, monkeypatch
    ):
        """The transient-skip double turn. B is unready exactly once, at its
        own rotation slot, so A takes it. The counter must land one past the
        WOKEN position — recovered B goes next — not one past the skipped
        slot, which parks it on A and hands A a third consecutive turn while
        B sits ready with mail. The strict-alternation test above never
        exercises this: its inboxes stay full, the start agent always wakes,
        and both arithmetics agree at woke_index zero."""
        import daemon as daemon_mod
        from mailbox import peek_inbox_messages

        agents = self._register(tmp_path, fixtures_dir, ("A", "B"))
        queue = self._queue
        queue("A", "a0")
        queue("B", "b0")

        woke: list[str] = []

        def instant_wake(p, msg_path, *, async_wake=False):
            woke.append(p.name)
            msg_path.rename(unique_path(trash_dir(p.name) / msg_path.name))
            return True

        monkeypatch.setattr(daemon_mod, "wake_once", instant_wake)

        real_ready = daemon_mod._wake_retry_ready
        armed = True

        def skip_b_once(name, **kw):
            nonlocal armed
            if armed and name == "B" and len(woke) == 1:
                armed = False
                return False
            return real_ready(name, **kw)

        monkeypatch.setattr(daemon_mod, "_wake_retry_ready", skip_b_once)

        wait_calls = 0

        def refill_and_stop(self, timeout=None):
            nonlocal wait_calls
            wait_calls += 1
            for name, p in agents.items():
                if not peek_inbox_messages(p, 1):
                    queue(name, f"more-{name}-{wait_calls}")
            if len(woke) >= 4 or wait_calls >= 200:
                daemon_mod._STOP_EVENT.set()
            return True

        monkeypatch.setattr(threading.Event, "wait", _main_thread_only(refill_and_stop))
        attached_loop(["A", "B"], 0.01, single_pass=False)

        assert len(woke) >= 4
        assert woke[:4] == ["A", "A", "B", "A"]
