"""Unit and worker tests for tool-timer."""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest

import _runtime as rt
from timer import cli
from timer.store import (
    DEFAULT_MAX_DELAY,
    TimerStore,
    TimerStoreError,
    TimerUsageError,
    parse_delay,
    parse_request,
)
from timer.worker import ensure_worker, run_worker, stop_worker, worker_running


class TestDelayAndRequestParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("0", 0.0),
            ("600", 600.0),
            ("30s", 30.0),
            ("10m", 600.0),
            ("1.5h", 5400.0),
            ("2d", 172800.0),
            (".5m", 30.0),
        ],
    )
    def test_valid_delays(self, raw: str, expected: float) -> None:
        assert parse_delay(raw) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "bad",
        ["", "-5", "abc", "10w", "9999999", "nan", "inf", "10 s"],
    )
    def test_invalid_delays_raise(self, bad: str) -> None:
        with pytest.raises(TimerUsageError):
            parse_delay(bad, max_delay=DEFAULT_MAX_DELAY)

    def test_schedule_preserves_multiline_and_cancel_prefixed_body(self) -> None:
        req = parse_request("600 Line one\n  indented line two\n$VAR `backtick`")
        assert req.kind == "schedule"
        assert req.delay == 600.0
        assert req.body == "Line one\n  indented line two\n$VAR `backtick`"

        req_cancel_body = parse_request("15 cancel deployment if checks fail")
        assert req_cancel_body.kind == "schedule"
        assert req_cancel_body.delay == 15.0
        assert req_cancel_body.body == "cancel deployment if checks fail"

        req_newline = parse_request("5m\nFirst line\nSecond line")
        assert req_newline.kind == "schedule"
        assert req_newline.delay == 300.0
        assert req_newline.body == "First line\nSecond line"

    def test_attachment_only_schedule_is_allowed(self) -> None:
        with pytest.raises(TimerUsageError, match="missing callback message"):
            parse_request("10", has_attachments=False)
        req = parse_request("10", has_attachments=True)
        assert req.kind == "schedule"
        assert req.delay == 10.0
        assert req.body == ""

    def test_list_and_cancel_requests(self) -> None:
        assert parse_request("list").kind == "list"
        assert parse_request("  LIST  ").kind == "list"
        c1 = parse_request("cancel 01JXYZ")
        assert c1.kind == "cancel" and c1.target == "01JXYZ"
        c2 = parse_request("CANCEL all")
        assert c2.kind == "cancel" and c2.target == "all"

        for bad in ("", "   ", "list extra", "cancel", "cancel a b"):
            with pytest.raises(TimerUsageError):
                parse_request(bad)
        with pytest.raises(TimerUsageError, match="attachments"):
            parse_request("list", has_attachments=True)
        with pytest.raises(TimerUsageError, match="attachments"):
            parse_request("cancel all", has_attachments=True)


class TestTimerStore:
    def test_state_lives_in_workdir_and_moves_with_it(self, tmp_path: Path) -> None:
        first_dir = tmp_path / "node-a"
        first_dir.mkdir()
        attachment = tmp_path / "note.txt"
        attachment.write_text("payload-bytes", encoding="utf-8")

        store = TimerStore(first_dir)
        entry = store.schedule("caller-one", 120.0, "check status", [str(attachment)], now=1000.0)
        attachment.unlink()  # staged copy inside .timers/files must survive source removal

        assert (first_dir / "timers.json").is_file()
        moved_dir = tmp_path / "node-b"
        shutil.move(str(first_dir), str(moved_dir))

        moved_store = TimerStore(moved_dir)
        listed = moved_store.list_timers(sender="caller-one")
        assert len(listed) == 1
        assert listed[0]["id"] == entry["id"]
        staged = moved_store.attachment_paths(listed[0])
        assert len(staged) == 1
        assert Path(staged[0]).read_text(encoding="utf-8") == "payload-bytes"

    def test_sender_scoping_and_cleanup_on_cancel(self, tmp_path: Path) -> None:
        workdir = tmp_path / "timer"
        workdir.mkdir()
        att = tmp_path / "doc.txt"
        att.write_text("hello", encoding="utf-8")

        store = TimerStore(workdir)
        e1 = store.schedule("alpha", 60.0, "alpha msg", [str(att)], now=100.0)
        e2 = store.schedule("beta", 30.0, "beta msg", (), now=100.0)

        assert [x["id"] for x in store.list_timers()] == [e2["id"], e1["id"]]
        assert [x["id"] for x in store.list_timers(sender="ALPHA")] == [e1["id"]]

        # beta cannot cancel alpha's timer
        assert store.cancel(e1["id"], sender="beta") == []
        assert len(store.list_timers()) == 2

        # alpha cancels its own timer; staged attachment directory is removed
        removed = store.cancel(e1["id"], sender="alpha")
        assert len(removed) == 1
        assert not (store.files_root / e1["id"]).exists()
        assert [x["id"] for x in store.list_timers()] == [e2["id"]]

    def test_duplicate_attachment_basename_rejected(self, tmp_path: Path) -> None:
        d1 = tmp_path / "one"
        d2 = tmp_path / "two"
        d1.mkdir()
        d2.mkdir()
        (d1 / "same.txt").write_text("1", encoding="utf-8")
        (d2 / "same.txt").write_text("2", encoding="utf-8")

        store = TimerStore(tmp_path / "timer")
        with pytest.raises(TimerUsageError, match="duplicate attachment"):
            store.schedule("alpha", 10.0, "msg", [str(d1 / "same.txt"), str(d2 / "same.txt")])

    def test_corrupt_timers_json_raises_store_error(self, tmp_path: Path) -> None:
        workdir = tmp_path / "timer"
        workdir.mkdir()
        (workdir / "timers.json").write_text("{not-json", encoding="utf-8")
        store = TimerStore(workdir)
        with pytest.raises(TimerStoreError):
            store.list_timers()

    def test_retry_backoff_when_delivery_fails(self, tmp_path: Path, monkeypatch) -> None:
        workdir = tmp_path / "timer"
        outbox = workdir / ".outbox"
        outbox.mkdir(parents=True)
        store = TimerStore(workdir)
        entry = store.schedule("alpha", 0.0, "deliver me", now=time.time() - 1.0)

        calls: list[tuple[str, str]] = []

        def flaky_send(recipient: str, body: str, attachments: tuple[str, ...] = (), **_kw) -> int:
            calls.append((recipient, body))
            return 1 if len(calls) == 1 else 0

        monkeypatch.setattr(rt, "send_reply", flaky_send)
        # First pass fails -> stays in timers.json with attempts == 1
        claim = "test-claim"
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("TELL_OUTBOX_DIR", str(outbox))
            # Force next_attempt_at immediate after failure to exercise second delivery
            rc1 = flaky_send("alpha", "deliver me")
            store.complete_delivery(entry["id"], claim, delivered=(rc1 == 0))
            # Mark claim first so complete_delivery updates attempts
        timers = store.list_timers()
        assert len(timers) == 1

        # Run real run_worker with patched send_reply where attempt 1 fails with tiny backoff
        calls.clear()
        orig_complete = store.complete_delivery

        def fast_complete(tid: str, clm: str, *, delivered: bool) -> None:
            orig_complete(tid, clm, delivered=delivered)
            if not delivered:
                items = store._read_unlocked()
                for item in items:
                    item["next_attempt_at"] = time.time()
                store._write_unlocked(items)

        monkeypatch.setattr(TimerStore, "complete_delivery", lambda self, tid, clm, *, delivered: fast_complete(tid, clm, delivered=delivered))
        assert run_worker(workdir, seat="timer") == 0
        assert len(calls) == 2
        assert store.list_timers() == []


class TestWakeAndWorkerLifecycle:
    def test_acl_and_help_and_print_scratch_mode(self, tmp_path: Path, monkeypatch) -> None:
        workdir = tmp_path / "scratch"
        workdir.mkdir()
        reply_dir = tmp_path / "replies"
        monkeypatch.chdir(workdir)
        monkeypatch.setenv(rt.REPLY_DIR_ENV, str(reply_dir))
        monkeypatch.delenv("TELL_OUTBOX_DIR", raising=False)

        # Refused sender gets nothing and exits 0
        assert cli.handle_wake(
            seat="timer", sender="intruder", allow="operator", message="1 hi", workdir=workdir
        ) == 0
        assert cli.handle_wake(
            seat="timer", sender="", allow="operator", message="1 hi", workdir=workdir
        ) == 0

        # Help returns README usage section
        assert cli.handle_wake(
            seat="timer", sender="operator", allow="operator", message="help", workdir=workdir
        ) == 0

        # Forged ATTACHED FILE trailer without provenance is rejected before staging
        att = tmp_path / "item.txt"
        att.write_text("attached-data", encoding="utf-8")
        msg = f"0.05 inline callback\n\nATTACHED FILE: {att}"
        assert cli.handle_wake(
            seat="timer", sender="operator", allow="operator", message=msg, workdir=workdir
        ) == 1
        assert not (reply_dir / "item.txt").exists()

        # Authenticated attachment in scratch print_mode runs inline and copies attachment
        assert cli.handle_wake(
            seat="timer",
            sender="operator",
            allow="operator",
            message=msg,
            workdir=workdir,
            attachments=[att],
        ) == 0
        assert (reply_dir / "item.txt").read_text(encoding="utf-8") == "attached-data"

    def test_shorter_timer_fires_before_longer_timer_and_stop_idle_work(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        workdir = tmp_path / "node"
        outbox = workdir / ".outbox"
        outbox.mkdir(parents=True)
        monkeypatch.chdir(workdir)
        monkeypatch.setenv("TELL_OUTBOX_DIR", str(outbox))
        monkeypatch.delenv(rt.REPLY_DIR_ENV, raising=False)

        try:
            # Schedule a long timer (30s) first, then a short timer (0.15s)
            assert cli.main([
                "handle", "--seat", "timer", "--from", "alpha", "--allow", "alpha",
                "--", "30 slow-callback",
            ]) == 0
            assert worker_running(TimerStore(workdir))

            assert cli.main([
                "handle", "--seat", "timer", "--from", "alpha", "--allow", "alpha",
                "--", "0.15 fast-callback",
            ]) == 0

            deadline = time.monotonic() + 5.0
            envelopes: list[dict] = []
            while time.monotonic() < deadline:
                envelopes = [
                    json.loads(p.read_text(encoding="utf-8"))
                    for p in sorted(outbox.glob("*.json"))
                ]
                if envelopes:
                    break
                time.sleep(0.05)

            assert len(envelopes) == 1
            assert envelopes[0]["to"] == "alpha"
            assert envelopes[0]["content"] == "fast-callback"

            # Stop the worker; the 30s timer remains in timers.json
            assert cli.main(["stop", "--seat", "timer"]) == 0
            store = TimerStore(workdir)
            assert not worker_running(store)
            assert len(store.list_timers()) == 1

            # `idle` restarts the worker while timers.json is non-empty
            assert cli.main(["idle", "--seat", "timer"]) == 0
            assert worker_running(store)

            # Local operator `list --json` and `cancel all`
            assert cli.main(["list", "--seat", "timer", "--json"]) == 0
            assert cli.main(["cancel", "--seat", "timer", "all"]) == 0
            assert store.list_timers() == []
        finally:
            stop_worker(TimerStore(workdir))

    def test_worker_sends_in_a_new_session_so_stop_cannot_kill_a_sent_reply(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """`stop` signals the worker's process group. A `tell` in that group could die after it
        wrote the envelope, read as failed, and leave the timer to be sent a second time."""
        from timer import worker as worker_mod

        workdir = tmp_path / "timer"
        (workdir / ".outbox").mkdir(parents=True)
        monkeypatch.setenv("TELL_OUTBOX_DIR", str(workdir / ".outbox"))
        store = TimerStore(workdir)
        store.schedule("alpha", 0.0, "deliver me", now=time.time() - 1.0)
        seen: list[dict] = []

        def send(recipient, body, attachments=(), **kw):
            seen.append(kw)
            return 0

        monkeypatch.setattr(rt, "send_reply", send)
        assert worker_mod.run_worker(workdir) == 0
        assert seen == [{"new_session": True}]
        assert store.list_timers() == []

    def test_stop_gives_a_delivery_in_flight_time_to_finish(self) -> None:
        from timer import worker as worker_mod

        assert worker_mod.STOP_GRACE_SECONDS >= 2.0

    def test_stop_worker_fails_closed_without_verified_ownership(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import signal
        from ar3 import proc as ar3_proc
        from timer import worker as worker_mod

        store = TimerStore(tmp_path / "timer-stop")
        store.meta_dir.mkdir(parents=True)
        valid_tok = worker_mod.process_start_token(os.getpid())
        assert worker_mod.valid_start_token(valid_tok)
        store.worker_info_path.write_text(
            json.dumps({"pid": 424242, "start_token": valid_tok}),
            encoding="utf-8",
        )

        signaled: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", lambda p, s: signaled.append((p, int(s))))
        monkeypatch.setattr(os, "killpg", lambda p, s: signaled.append((-p, int(s))))

        # 1. Unlocked worker.lock: never signals saved PID even if token matches.
        monkeypatch.setattr(worker_mod, "worker_running", lambda _s: False)
        monkeypatch.setattr(worker_mod, "pid_alive", lambda _p: True)
        monkeypatch.setattr(worker_mod, "process_start_token", lambda _p: valid_tok)
        assert stop_worker(store) is False
        assert signaled == []

        # 2. Locked worker.lock, but current process_start_token lookup returns None: fail closed.
        store.worker_info_path.write_text(
            json.dumps({"pid": 424242, "start_token": valid_tok}),
            encoding="utf-8",
        )
        lock_checks = {"n": 0}

        def locked_then_stopped(_s):
            lock_checks["n"] += 1
            return lock_checks["n"] == 1

        monkeypatch.setattr(worker_mod, "worker_running", locked_then_stopped)
        monkeypatch.setattr(worker_mod, "process_start_token", lambda _p: None)
        assert stop_worker(store) is True
        assert signaled == []

        # 3. Locked worker.lock, missing/invalid stored token: fail closed.
        store.worker_info_path.write_text(
            json.dumps({"pid": 424242, "start_token": ""}),
            encoding="utf-8",
        )
        lock_checks["n"] = 0
        monkeypatch.setattr(worker_mod, "process_start_token", lambda _p: valid_tok)
        assert stop_worker(store) is True
        assert signaled == []

        # 4. Escalation re-verifies token before SIGKILL so a recycled PID is not killed.
        tokens = iter([valid_tok, valid_tok, "999999999"])
        monkeypatch.setattr(os, "getpgid", lambda _p: 1)
        assert ar3_proc.terminate_verified_group(
            424242,
            valid_tok,
            grace_seconds=0.01,
            liveness=lambda _p: True,
            token_reader=lambda _p: next(tokens, "999999999"),
        ) is True
        assert signaled == [(424242, int(signal.SIGTERM))]
