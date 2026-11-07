"""Muse is wired, not merely declared.

The suite's recurring defect is configuration that parses, validates and
documents cleanly with nothing behind it. These tests are the other half of
adding an engine: each one fails if a specific piece of the wiring is removed,
so `muse` cannot decay into a name the tables know and nothing honours.
"""
from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import detect
import engines
from conftest import write_path_executable
from engines import check as engine_check
from engines import muse as muse_engine
from engines import run as engine_run
from engines.base import QuotaError
from rig import (
    HARNESS_PRESETS,
    PERMISSION_TRANSLATION,
    allowed_tools_unsupported_reason,
    continue_unsupported_reason,
    mcp_unsupported_reason,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFINITIONS = REPO_ROOT / "apps" / "a8s" / "definitions"


def argv_for(**kwargs):
    base = dict(model=None, timeout=900, workdir=Path("/tmp"))
    base.update(kwargs)
    return engine_run.build_argv("muse", prompt="PROMPT", **base)


class TestPreset:
    def test_muse_is_a_preset_and_a_run_engine(self):
        assert "muse" in HARNESS_PRESETS
        assert "muse" in engine_run.RUN_ENGINES

    def test_capabilities_include_quota(self):
        # muse's quota verb rides `muse serve`'s MSP usage/read — verified
        # against 1.3.0 — so the registry advertises it beside run and check.
        from engines import muse as muse_engine

        assert engines.capabilities("muse") == ["quota", "run", "check"]
        assert engines.capability("muse", "quota") is muse_engine.quota

    def test_quota_dispatches_to_the_module(self, monkeypatch):
        # engines.quota() calls MODULES[engine].quota() directly; a stub proves
        # the wiring without spawning a real `muse serve` in the suite. The
        # origin is not "live" on purpose: a live answer would persist a
        # snapshot under the real r4t home.
        from engines import muse as muse_engine

        monkeypatch.setattr(
            muse_engine,
            "quota",
            lambda: {"origin": "stub", "plan": None, "buckets": [], "note": None},
        )
        # spend=True because muse's live read mints a turn — the dispatch
        # under test is the same either way, and the gate itself is covered
        # by TestSpendGate below.
        payload = engines.quota("muse", spend=True)
        assert payload["engine"] == "muse"

    def test_headless_invocation_is_exec_with_a_positional_prompt(self):
        assert argv_for() == [
            "muse", "exec",
            "--approval-mode", "never",
            "--user-input-auto-resolve",
            "PROMPT",
        ]

    def test_model_is_spliced_at_the_exec_anchor(self):
        argv = argv_for(model="muse-spark-1.2")
        assert argv[:4] == ["muse", "exec", "--model", "muse-spark-1.2"]


class TestPermissionTranslation:
    def test_muse_is_registered_in_the_translation_table(self):
        assert PERMISSION_TRANSLATION["muse"].anchor == "exec"

    def test_ask_returns_muse_to_its_own_default(self):
        # Both flags go: --approval-mode never AND the auto-resolve flag that
        # exists only to keep an unattended turn from blocking on a prompt.
        assert argv_for(permissions="ask") == ["muse", "exec", "PROMPT"]

    def test_auto_is_what_the_preset_already_carries(self):
        assert argv_for(permissions="auto") == argv_for()

    def test_bypass_is_yolo_and_drops_the_approval_flag(self):
        argv = argv_for(permissions="bypass")
        assert "--yolo" in argv
        assert "--approval-mode" not in argv
        # --yolo disables approvals and the sandbox; the auto-resolve flag is
        # about unattendedness, not permission, so it stays.
        assert "--user-input-auto-resolve" in argv


class TestRefusalsNameTheirReason:
    def test_continue_is_refused_because_resume_is_interactive(self):
        with pytest.raises(engine_run.RunError) as excinfo:
            argv_for(continue_conversation=True)
        message = str(excinfo.value)
        assert "muse cannot continue" in message
        assert "session picker" in message

    def test_continue_reason_is_recorded_for_muse(self):
        assert "picker" in continue_unsupported_reason("muse")

    def test_allowed_tools_is_refused_with_a_reason(self):
        with pytest.raises(Exception) as excinfo:
            argv_for(allowed_tools="Read Write")
        assert "muse" in str(excinfo.value)
        assert "permission profile" in allowed_tools_unsupported_reason("muse")

    def test_mcp_reason_says_muse_serves_msp_rather_than_consuming_mcp(self):
        reason = mcp_unsupported_reason("muse")
        assert "MSP" in reason
        assert HARNESS_PRESETS["muse"].get("mcp") is None


class TestCheckProbe:
    def test_a_run_engine_has_a_check_probe(self):
        # Without this, `r4t engine muse check` raises KeyError instead of
        # reporting — a verb the registry advertises and cannot perform.
        from engines import check as engine_check

        assert "muse" in engine_check.PROBES
        probe = engine_check.PROBES["muse"]
        assert probe.help_binary == "muse"
        assert probe.help_argv == ("exec", "--help")
        # muse's --help short-circuits whatever else is on the line, so it
        # cannot be handed the composed argv the way codex can.
        assert probe.strict is False

    def test_every_run_engine_has_a_probe(self):
        from engines import check as engine_check

        assert set(engine_run.RUN_ENGINES) <= set(engine_check.PROBES)


class TestA8sDefinitions:
    @pytest.mark.parametrize(
        "name", ["muse.json", "engine-muse.json", "engine-muse-unrestricted.json"]
    )
    def test_definition_exists_and_is_valid_json(self, name):
        data = json.loads((DEFINITIONS / name).read_text(encoding="utf-8"))
        assert data["description"]
        assert data["invoke"]

    def test_the_preset_points_at_a_definition_that_exists(self):
        named = HARNESS_PRESETS["muse"]["a8s_definition"]
        assert (DEFINITIONS / named).is_file()

    def test_direct_definition_invokes_muse_headlessly(self):
        data = json.loads((DEFINITIONS / "muse.json").read_text(encoding="utf-8"))
        assert data["invoke"][:2] == ["muse", "exec"]
        assert "--user-input-auto-resolve" in data["invoke"]

    def test_engine_definition_routes_through_r4t_engine_run(self):
        data = json.loads((DEFINITIONS / "engine-muse.json").read_text(encoding="utf-8"))
        for block in (data, data["batch"], data["idle"]):
            argv = block["invoke"]
            assert argv[2:5] == ["engine", "muse", "run"]

    def test_unrestricted_definition_passes_permissions_bypass(self):
        data = json.loads(
            (DEFINITIONS / "engine-muse-unrestricted.json").read_text(encoding="utf-8")
        )
        for block in (data, data["batch"], data["idle"]):
            argv = block["invoke"]
            i = argv.index("--permissions", argv.index("run"))
            assert argv[i + 1] == "bypass"
        assert "--yolo" in data["description"]


FAKE_USAGE = {
    "window": {"usedPercent": 40, "windowDurationMins": 300, "resetsAtMs": 1790286036000},
    "weekly": {"usedPercent": 16, "resetsAtMs": 1790553600000},
    "tier": "fake-tier",
    "observedAtMs": 1790273739931,
}

FAKE_SERVE = textwrap.dedent(
    """\
    import json, os, sys, threading, time

    USAGE = {usage!r}
    LOG = os.environ.get("FAKE_MUSE_LOG")
    PIDFILE = os.environ.get("FAKE_MUSE_PIDFILE")
    DELAY = float(os.environ.get("FAKE_MUSE_DELAY", "0.2"))
    NEVER = os.environ.get("FAKE_MUSE_NEVER")

    if PIDFILE:
        with open(PIDFILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))

    def emit(frame):
        sys.stdout.write(json.dumps(frame) + "\\n")
        sys.stdout.flush()

    def mint():
        time.sleep(DELAY)
        completion = os.environ.get("FAKE_MUSE_COMPLETION")
        if completion:
            terminals = json.loads(completion)
            if isinstance(terminals, dict):
                terminals = [terminals]
            for terminal in terminals:
                emit({{"jsonrpc": "2.0", "method": "turn/completed",
                      "params": terminal}})
            return
        if os.environ.get("FAKE_MUSE_UNRELATED_COMPLETION"):
            for session, turn in (("other-session", "t1"), ("s1", "other-turn")):
                emit({{"jsonrpc": "2.0", "method": "turn/completed",
                      "params": {{"sessionId": session, "turnId": turn,
                                 "terminal": "failed", "error": {{
                                     "kind": "modelError", "quota": {{
                                         "limitKind": "tierLimit",
                                         "resetsAtMs": int(time.time() * 1000) + 3600000
                                     }}
                                 }} }} }})
                emit({{"jsonrpc": "2.0", "method": "turn/completed",
                      "params": {{"sessionId": session, "turnId": turn,
                                 "terminal": "completed"}} }})
            time.sleep(0.15)
        if not NEVER:
            # usage/changed deliberately arrives WITHOUT turn/completed —
            # the client must take the observation and cancel, not hold out
            # for the turn to end.
            emit({{"jsonrpc": "2.0", "method": "usage/changed", "params": USAGE}})

    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if LOG:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg) + "\\n")
        if "id" not in msg:
            continue
        rid, method = msg["id"], msg.get("method")
        if method == "initialize":
            emit({{"jsonrpc": "2.0", "id": rid, "result": {{"serverInfo": {{"name": "muse"}}}}}})
        elif method == "usage/read":
            emit({{"jsonrpc": "2.0", "id": rid, "result": {{}}}})
        elif method == "session/start":
            emit({{"jsonrpc": "2.0", "id": rid,
                  "result": {{"session": {{"sessionId": "s1"}}, "viewCursor": "v:1"}}}})
        elif method == "turn/start":
            if os.environ.get("FAKE_MUSE_COMPLETION_BEFORE_RECEIPT"):
                mint()
            emit({{"jsonrpc": "2.0", "id": rid,
                  "result": {{"commandId": msg["params"]["commandId"],
                              "status": "accepted", "turnId": "t1",
                              "disposition": "started", "startedNewTurn": True}}}})
            if not os.environ.get("FAKE_MUSE_COMPLETION_BEFORE_RECEIPT"):
                threading.Thread(target=mint, daemon=True).start()
        elif method == "turn/cancel":
            emit({{"jsonrpc": "2.0", "id": rid,
                  "result": {{"commandId": msg["params"]["commandId"],
                              "status": "accepted", "turnId": "t1"}}}})
            late_completion = os.environ.get("FAKE_MUSE_CANCEL_COMPLETION")
            emit({{"jsonrpc": "2.0", "method": "turn/completed",
                  "params": json.loads(late_completion) if late_completion else
                            {{"sessionId": "s1", "turnId": "t1"}}}})
    """
).format(usage=FAKE_USAGE)


@pytest.fixture
def fake_serve(tmp_path, monkeypatch):
    """A `muse serve` stand-in on a tmp PATH: speaks the MSP handshake, mints
    a usage observation mid-turn, logs every frame it receives."""
    bin_dir = tmp_path / "bin"
    write_path_executable(bin_dir, "muse", FAKE_SERVE)
    monkeypatch.setenv("PATH", str(bin_dir))
    log = tmp_path / "frames.jsonl"
    monkeypatch.setenv("FAKE_MUSE_LOG", str(log))
    monkeypatch.setenv("FAKE_MUSE_PIDFILE", str(tmp_path / "pid"))
    return log


def frames_of(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()]


class TestRpcUsage:
    """The live path end to end: `_rpc_usage` against the fake host proves the
    handshake, the mint, the early return on `usage/changed`, and that the
    host process does not outlive the call — the exact lifecycle the silent
    terminal regression was about."""

    def test_mint_path_answers_and_cancels_the_turn(self, fake_serve):
        payload = muse_engine.quota()
        assert payload["origin"] == "live"
        assert payload["plan"] == "fake-tier"
        assert payload["buckets"][0]["label"] == "Five Hour Limit"

        frames = frames_of(fake_serve)
        # `initialized` is a notification — an id would make it a request and
        # strand every later call at notInitialized.
        initialized = [f for f in frames if f.get("method") == "initialized"]
        assert initialized and "id" not in initialized[0]
        # session/start and turn/start commandIds are checked UUIDv7.
        for method in ("session/start", "turn/start"):
            command_id = next(
                f["params"]["commandId"]
                for f in frames
                if f.get("method") == method
            )
            assert command_id.split("-")[2][0] == "7"
        # The mint turn asks for no reasoning — the cheapest provider call.
        turn = next(f for f in frames if f.get("method") == "turn/start")
        assert turn["params"]["reasoningEffort"] == "none"
        # usage/changed landed mid-turn; the still-running turn is cancelled
        # rather than waited out or orphaned by the kill.
        assert any(f.get("method") == "turn/cancel" for f in frames)

    def test_the_serve_host_does_not_outlive_the_call(
        self, fake_serve, tmp_path
    ):
        if os.name == "nt":
            pytest.skip("pid liveness is a POSIX check")
        muse_engine.quota()
        pid = int((tmp_path / "pid").read_text())
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return  # already reaped — dead
            if done == pid:
                return
            time.sleep(0.05)
        pytest.fail("muse serve still running after quota() returned")

    def test_a_turn_that_never_reports_raises_instead_of_hanging(
        self, fake_serve, monkeypatch
    ):
        monkeypatch.setenv("FAKE_MUSE_NEVER", "1")
        monkeypatch.setattr(muse_engine, "MINT_TIMEOUT_S", 0.5)
        with pytest.raises(QuotaError, match="no usage observation"):
            muse_engine.quota()

    def test_a_tty_sees_progress_while_the_turn_runs(
        self, fake_serve, monkeypatch
    ):
        class Tty:
            def __init__(self):
                self.buffer = []

            def isatty(self):
                return True

            def write(self, text):
                self.buffer.append(text)

            def flush(self):
                pass

        tty = Tty()
        monkeypatch.setattr("sys.stderr", tty)
        monkeypatch.setattr(muse_engine, "HEARTBEAT_S", 0.3)
        monkeypatch.setenv("FAKE_MUSE_DELAY", "0.8")
        muse_engine.quota()
        lines = "".join(tty.buffer).splitlines()
        assert any("cold host" in line for line in lines)
        assert any("waiting on the probe turn" in line for line in lines)


FRESH_SNAPSHOT = {
    "origin": "live",
    "plan": "fake-tier",
    "buckets": [
        {
            "label": "Weekly Limit",
            "remaining_fraction": 0.5,
            "reset_time": "2099-01-01T00:00:00+00:00",
        }
    ],
    "note": None,
}


class TestProbeQuotaExhaustion:
    def _completion(self, monkeypatch, *, detail=None, error=None):
        if error is None:
            error = {
                "kind": "modelError",
                "message": "private-provider-text-secret@example.invalid",
                "quota": detail if detail is not None else {
                    "limitKind": "tierLimit",
                    "resetsAtMs": int(time.time() * 1000) + 3600000,
                },
            }
        monkeypatch.setenv("FAKE_MUSE_COMPLETION", json.dumps({
            "sessionId": "s1", "turnId": "t1", "terminal": "failed",
            "error": error, "retryable": False,
        }))

    @pytest.mark.parametrize("as_json", [False, True])
    @pytest.mark.parametrize("expired", [False, True])
    def test_public_quota_replaces_positive_snapshot_with_zero(
        self, fake_serve, r4t_home, monkeypatch, capsys, as_json, expired
    ):
        import r4t as r4t_mod

        self._completion(monkeypatch)
        snapshot = {**FRESH_SNAPSHOT, "buckets": [{
            **FRESH_SNAPSHOT["buckets"][0],
            "reset_time": "2000-01-01T00:00:00+00:00" if expired else
                          "2099-01-01T00:00:00+00:00",
        }]}
        engines.save_snapshot("muse", snapshot)
        assert r4t_mod.main(["engine", "muse", "quota"] +
                            (["--json"] if as_json else [])) == 0
        output = capsys.readouterr()
        assert not output.err
        assert "private-provider-text" not in output.out
        assert "Subscription Limit" in output.out
        if as_json:
            payload = json.loads(output.out)
            assert payload["origin"] == "live"
            assert payload["buckets"] == [{
                "label": "Subscription Limit", "remaining_fraction": 0.0,
                "reset_time": payload["buckets"][0]["reset_time"],
            }]
            assert payload["buckets"][0]["reset_time"]
        else:
            assert "0%" in output.out
        saved = engines.load_snapshot("muse")
        assert saved["buckets"][0]["remaining_fraction"] == 0.0
        report = engines.fuel("muse", model="muse-spark-1.2")
        assert report["fuel"] == 0.0 and report["state"] == "gauged"
        assert report["origin"] == "snapshot"
        assert len([f for f in frames_of(fake_serve)
                    if f.get("method") == "turn/start"]) == 1

    @pytest.mark.parametrize("timing", ["before_receipt", "after_usage"])
    def test_matching_exhaustion_survives_notification_order(
        self, fake_serve, monkeypatch, timing
    ):
        self._completion(monkeypatch)
        if timing == "before_receipt":
            monkeypatch.setenv("FAKE_MUSE_COMPLETION_BEFORE_RECEIPT", "1")
        else:
            monkeypatch.setenv("FAKE_MUSE_CANCEL_COMPLETION",
                               os.environ["FAKE_MUSE_COMPLETION"])
            monkeypatch.delenv("FAKE_MUSE_COMPLETION")
        payload = muse_engine.quota()
        assert payload["buckets"][0]["label"] == "Subscription Limit"
        assert payload["buckets"][0]["remaining_fraction"] == 0.0

    @pytest.mark.parametrize("before_receipt", [False, True])
    @pytest.mark.parametrize("failure_first", [False, True])
    def test_matching_exhaustion_survives_other_turns_and_conflicting_replays(
        self, fake_serve, r4t_home, monkeypatch, before_receipt, failure_first
    ):
        self._completion(monkeypatch)
        exhausted = json.loads(os.environ["FAKE_MUSE_COMPLETION"])
        success = {"sessionId": "s1", "turnId": "t1", "terminal": "completed"}
        others = [{"sessionId": "s1", "turnId": f"other-{i}",
                   "terminal": "completed"} for i in range(3)]
        terminals = ([exhausted, exhausted, success] + others if failure_first
                     else others + [success, exhausted, exhausted])
        monkeypatch.setenv("FAKE_MUSE_COMPLETION", json.dumps(terminals))
        if before_receipt:
            monkeypatch.setenv("FAKE_MUSE_COMPLETION_BEFORE_RECEIPT", "1")
        monkeypatch.setattr(muse_engine, "MINT_TIMEOUT_S", 0.5)
        engines.save_snapshot("muse", dict(FRESH_SNAPSHOT))
        payload = engines.quota("muse", spend=True)
        assert payload["origin"] == "live"
        assert payload["buckets"][0]["remaining_fraction"] == 0.0

    def test_pending_completion_overflow_invalidates_snapshot_without_guessing(
        self, fake_serve, r4t_home, monkeypatch
    ):
        self._completion(monkeypatch)
        terminals = [json.loads(os.environ["FAKE_MUSE_COMPLETION"])] + [
            {"sessionId": "s1", "turnId": f"other-{i}", "terminal": "completed"}
            for i in range(3)
        ]
        monkeypatch.setenv("FAKE_MUSE_COMPLETION", json.dumps(terminals))
        monkeypatch.setenv("FAKE_MUSE_COMPLETION_BEFORE_RECEIPT", "1")
        monkeypatch.setattr(muse_engine, "PENDING_COMPLETIONS_LIMIT", 3)
        monkeypatch.setattr(muse_engine, "MINT_TIMEOUT_S", 0.5)
        engines.save_snapshot("muse", dict(FRESH_SNAPSHOT))
        with pytest.raises(engines.QuotaInvalidated, match="completion limit exceeded"):
            engines.quota("muse", spend=True)
        with pytest.raises(QuotaError, match="spends a provider turn"):
            engines.fuel("muse", model="muse-spark-1.2")
        assert engines.load_snapshot("muse") is None
        assert len([f for f in frames_of(fake_serve)
                    if f.get("method") == "turn/start"]) == 1

    @pytest.mark.parametrize("detail", [
        {"limitKind": "modelLimit", "resetsAtMs": 2000000000000},
        {"limitKind": "tierLimit"},
        {"limitKind": "tierLimit", "resetsAtMs": None},
        {"limitKind": "tierLimit", "resetsAtMs": True},
        {"limitKind": "tierLimit", "resetsAtMs": "2000000000000"},
        {"limitKind": "tierLimit", "resetsAtMs": 1},
        {"limitKind": "tierLimit", "resetsAtMs": 999999999999999999999},
    ])
    def test_ungaugeable_quota_refusal_cannot_resurrect_positive_snapshot(
        self, fake_serve, r4t_home, monkeypatch, detail
    ):
        self._completion(monkeypatch, detail=detail)
        engines.save_snapshot("muse", dict(FRESH_SNAPSHOT))
        with pytest.raises(engines.QuotaExhausted) as exc:
            engines.quota("muse", spend=True)
        assert "private-provider-text" not in str(exc.value)
        assert "snapshot" not in str(exc.value)
        with pytest.raises(QuotaError, match="spends a provider turn"):
            engines.fuel("muse", model="muse-spark-1.2")
        assert engines.load_snapshot("muse") is None
        assert len([f for f in frames_of(fake_serve)
                    if f.get("method") == "turn/start"]) == 1

    def test_positive_reading_recovers_after_quota_invalidation(
        self, fake_serve, r4t_home, monkeypatch
    ):
        self._completion(monkeypatch, detail={"limitKind": "tierLimit"})
        engines.save_snapshot("muse", dict(FRESH_SNAPSHOT))
        with pytest.raises(engines.QuotaExhausted):
            engines.quota("muse", spend=True)
        monkeypatch.delenv("FAKE_MUSE_COMPLETION")
        payload = engines.quota("muse", spend=True)
        assert payload["origin"] == "live"
        assert payload["buckets"][0]["remaining_fraction"] == 0.6
        assert engines.load_snapshot("muse")["buckets"] == payload["buckets"]

    def test_snapshot_invalidation_failure_is_explicit_and_safe(
        self, fake_serve, r4t_home, monkeypatch
    ):
        self._completion(monkeypatch, detail={"limitKind": "tierLimit"})
        engines.save_snapshot("muse", dict(FRESH_SNAPSHOT))
        real_unlink = Path.unlink
        snapshot = engines.snapshot_path("muse")

        def refuse_snapshot(path, *args, **kwargs):
            if path == snapshot:
                raise PermissionError("private-filesystem-detail")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", refuse_snapshot)
        with pytest.raises(engines.QuotaInvalidated, match="saved quota invalidation failed") as exc:
            engines.quota("muse", spend=True)
        assert "private-filesystem-detail" not in str(exc.value)

    def test_public_json_refuses_quota_with_unknown_reset_despite_snapshot(
        self, fake_serve, r4t_home, monkeypatch, capsys
    ):
        import r4t as r4t_mod

        self._completion(monkeypatch, detail={"limitKind": "tierLimit"})
        engines.save_snapshot("muse", dict(FRESH_SNAPSHOT))
        assert r4t_mod.main(["engine", "muse", "quota", "--json"]) == 1
        output = capsys.readouterr()
        assert not output.out
        assert "subscription quota exhausted; reset time unavailable" in output.err
        assert "private-provider-text" not in output.err

    @pytest.mark.parametrize("error", [
        {"kind": "modelError", "message": "429 quota exhausted private-token"},
        {"kind": "modelError", "message": "private-token", "quota": None},
        {"kind": "modelError", "message": "private-token", "quota": []},
    ])
    def test_untyped_failure_is_not_zero_and_does_not_publish_provider_text(
        self, fake_serve, r4t_home, monkeypatch, error
    ):
        self._completion(monkeypatch, error=error)
        with pytest.raises(QuotaError, match="probe turn failed without a quota observation") as exc:
            engines.quota("muse", spend=True)
        assert "private-token" not in str(exc.value)
        assert engines.load_snapshot("muse") is None

    @pytest.mark.parametrize("before_receipt", [False, True])
    def test_other_session_or_turn_failure_does_not_override_probe_usage(
        self, fake_serve, monkeypatch, before_receipt
    ):
        monkeypatch.setenv("FAKE_MUSE_UNRELATED_COMPLETION", "1")
        if before_receipt:
            monkeypatch.setenv("FAKE_MUSE_COMPLETION_BEFORE_RECEIPT", "1")
        payload = muse_engine.quota()
        assert payload["buckets"][0]["remaining_fraction"] == 0.6
        assert payload["buckets"][0]["label"] == "Five Hour Limit"


class TestSpendGate:
    """Muse declares `QUOTA_SPENDS_TURN`, so `engines.quota`/`fuel` must not
    invoke its checker unless the caller opted in. The case that bit:
    `rig detect` -> `engines.fuel` -> `engines.quota` minted a provider turn
    on a command whose contract is "nothing is spent" — snapshot or refusal,
    never a spawned host."""

    def test_muse_declares_its_live_read_spends(self):
        assert muse_engine.QUOTA_SPENDS_TURN is True

    def test_no_spend_serves_a_snapshot_without_calling_the_checker(
        self, r4t_home, monkeypatch
    ):
        engines.save_snapshot("muse", dict(FRESH_SNAPSHOT))
        called = []
        monkeypatch.setattr(
            muse_engine, "quota", lambda: called.append(True) or {}
        )
        payload = engines.quota("muse")
        assert payload["origin"] == "snapshot"
        assert called == []

    def test_no_spend_without_a_snapshot_refuses_without_calling(
        self, r4t_home, monkeypatch
    ):
        called = []
        monkeypatch.setattr(
            muse_engine, "quota", lambda: called.append(True) or {}
        )
        with pytest.raises(QuotaError, match="spends a provider turn"):
            engines.quota("muse")
        assert called == []

    def test_spend_calls_the_checker(self, r4t_home, monkeypatch):
        monkeypatch.setattr(
            muse_engine,
            "quota",
            lambda: {"origin": "live", "plan": None, "buckets": [], "note": None},
        )
        assert engines.quota("muse", spend=True)["origin"] == "live"

    def _detect_with_only_muse_installed(self, tmp_path):
        def check_engine(preset, **_kwargs):
            installed = preset == "muse"
            return engine_check.EngineReport(
                engine=preset,
                binary=preset,
                installed=installed,
                version="1.3.0" if installed else None,
                verdict=(
                    engine_check.ACCEPTED
                    if installed
                    else engine_check.UNVERIFIABLE
                ),
                detail="" if installed else "not on PATH",
            )

        rows = detect.detect(check_fn=check_engine, workdir=tmp_path)
        return next(r for r in rows if r.preset == "muse")

    def test_detection_never_spawns_a_host(self, fake_serve, r4t_home, tmp_path):
        row = self._detect_with_only_muse_installed(tmp_path)
        assert row.detected
        assert row.fuel is None
        assert "spends a provider turn" in (row.fuel_note or "")
        # The strongest statement: the fake binary never ran at all.
        assert not fake_serve.exists()
        assert not (tmp_path / "pid").exists()

    def test_detection_serves_a_snapshot_without_spawning(
        self, fake_serve, r4t_home, tmp_path
    ):
        engines.save_snapshot("muse", dict(FRESH_SNAPSHOT))
        row = self._detect_with_only_muse_installed(tmp_path)
        assert row.detected
        assert row.fuel == pytest.approx(0.5)
        assert row.origin == "snapshot"
        assert not fake_serve.exists()
        assert not (tmp_path / "pid").exists()


FAKE_USAGE_CLI = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, subprocess, sys, time

    mode = os.environ.get("FAKE_USAGE_MODE", "fresh")
    with open(os.environ["FAKE_USAGE_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "stdin_tty": sys.stdin.isatty(),
                            "no_update": os.environ.get("MUSE_NO_AUTO_UPDATE")}) + "\\n")
    if sys.argv[1:] == ["--version"]:
        print("muse 1.4.12" if mode == "old" else "muse 1.4.14")
        sys.exit(0)
    if mode == "old":
        print("TURN STARTED")
        sys.exit(0)
    if mode in ("fresh", "timeout"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        with open(os.environ["FAKE_USAGE_CHILD"], "w", encoding="utf-8") as f:
            f.write(str(child.pid))
    if mode == "timeout":
        time.sleep(60)
    if mode == "unsupported":
        print("error: unrecognized subcommand 'usage'", file=sys.stderr)
        sys.exit(2)
    if mode == "nonzero":
        print("boom", file=sys.stderr)
        sys.exit(3)
    if mode == "badjson":
        print("not json")
        sys.exit(0)
    now = int(time.time() * 1000)
    as_of = {"stale": now - 600000, "future": now + 600000}.get(mode, now)
    allowance = {
        "source": "cache" if mode == "cache" else "mint",
        "asOfMs": as_of, "tier": "fake-tier",
        "window": {"usedPercent": 100 if mode == "exhausted" else 125 if mode == "overlimit" else 25,
                   "resetsAtMs": now + 3600000},
        "weekly": {"usedPercent": -1 if mode == "badpercent" else 40,
                   "resetsAtMs": now + 86400000},
    }
    if mode == "null":
        allowance = None
    print(json.dumps({"schemaVersion": 1, "allowance": allowance,
                      "reason": "no observation" if mode == "null" else None,
                      "hint": None}))
    """
)


@pytest.fixture
def fake_usage(tmp_path, monkeypatch):
    write_path_executable(tmp_path / "bin", "muse", FAKE_USAGE_CLI)
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_USAGE_LOG", str(tmp_path / "call.json"))
    monkeypatch.setenv("FAKE_USAGE_CHILD", str(tmp_path / "child.pid"))

    def mode(name):
        monkeypatch.setenv("FAKE_USAGE_MODE", name)
    mode("fresh")
    fake_usage_state = type("S", (), {"mode": staticmethod(mode), "tmp": tmp_path})
    return fake_usage_state


def _gone(pid: int) -> bool:
    deadline = time.time() + 3
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        try:
            state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split(")")[-1].split()[0]
            if state == "Z":
                return True
        except OSError:
            pass
        time.sleep(0.05)
    return False


class TestLiveUsage:
    """`muse usage --json` is the free live read: fresh or unavailable, with
    no turn, no session mint and no snapshot behind it."""

    def test_fresh_allowance_is_a_live_answer_and_leaves_no_child(self, fake_usage):
        payload = engines.quota("muse", live=True)
        assert payload["origin"] == "live" and payload["engine"] == "muse"
        assert payload["plan"] == "fake-tier"
        assert [b["label"] for b in payload["buckets"]] == ["Window Limit", "Weekly Limit"]
        assert payload["buckets"][0]["remaining_fraction"] == 0.75
        assert payload["buckets"][1]["remaining_fraction"] == 0.6
        assert payload["requested_at"] and "requested at" in payload["note"] and "no model turn" in payload["note"]
        calls = [json.loads(l) for l in
                 (fake_usage.tmp / "call.json").read_text(encoding="utf-8").splitlines()]
        assert [c["argv"] for c in calls] == [["--version"], ["usage", "--json"]]
        assert all(c["stdin_tty"] is False and c["no_update"] == "1" for c in calls)
        pid = int((fake_usage.tmp / "child.pid").read_text(encoding="utf-8"))
        assert _gone(pid)

    @pytest.mark.parametrize("mode", ["exhausted", "overlimit"])
    def test_exhausted_reads_as_zero_remaining(self, fake_usage, mode):
        fake_usage.mode(mode)
        assert engines.quota("muse", live=True)["buckets"][0]["remaining_fraction"] == 0.0

    @pytest.mark.parametrize("mode, text", [
        ("stale", "predates"),
        ("future", "future"),
        ("cache", "not a fresh"),
        ("null", "no allowance"),
        ("badjson", "non-JSON"),
        ("badpercent", "usedPercent"),
        ("nonzero", "exited 3"),
        ("unsupported", "upgrade muse"),
        ("old", "upgrade muse"),
    ])
    def test_anything_short_of_a_fresh_mint_is_unavailable(self, fake_usage, mode, text):
        fake_usage.mode(mode)
        with pytest.raises(QuotaError, match=text):
            engines.quota("muse", live=True)

    def test_timeout_is_bounded_and_the_process_group_is_ended(
        self, fake_usage, monkeypatch
    ):
        monkeypatch.setattr(muse_engine, "LIVE_TIMEOUT_S", 1)
        fake_usage.mode("timeout")
        started = time.time()
        with pytest.raises(QuotaError, match="did not answer in 1s"):
            engines.quota("muse", live=True)
        assert time.time() - started < 8
        pid = int((fake_usage.tmp / "child.pid").read_text(encoding="utf-8"))
        assert _gone(pid)

    def test_sigterm_during_a_live_read_unwinds_and_ends_the_child_group(
        self, fake_usage, monkeypatch
    ):
        import threading

        import r4t as r4t_mod

        fake_usage.mode("timeout")
        before = signal.getsignal(signal.SIGTERM)
        timer = threading.Timer(1.0, os.kill, (os.getpid(), signal.SIGTERM))
        timer.start()
        started = time.time()
        try:
            with pytest.raises(SystemExit) as caught:
                r4t_mod.main(["engine", "muse", "quota", "--live", "--json"])
        finally:
            timer.cancel()
        assert caught.value.code == 128 + signal.SIGTERM
        assert time.time() - started < 8
        assert signal.getsignal(signal.SIGTERM) == before
        pid = int((fake_usage.tmp / "child.pid").read_text(encoding="utf-8"))
        assert _gone(pid)

    @pytest.mark.parametrize("patch", [
        lambda a: a.update(schemaVersion=2),
        lambda a: a["allowance"].update(asOfMs=True),
        lambda a: a["allowance"]["window"].update(resetsAtMs=0),
        lambda a: a["allowance"]["window"].update(usedPercent=float("nan")),
        lambda a: a["allowance"]["weekly"].update(usedPercent=-1),
        lambda a: a["allowance"].update(window=None, weekly=None),
    ])
    def test_parser_rejects_malformed_observations(self, patch):
        now = int(time.time() * 1000)
        data = {"schemaVersion": 1, "allowance": {
            "source": "mint", "asOfMs": now, "tier": "t",
            "window": {"usedPercent": 1, "resetsAtMs": now + 1000},
            "weekly": {"usedPercent": 2, "resetsAtMs": now + 2000}}}
        patch(data)
        with pytest.raises(QuotaError):
            muse_engine.parse_live_usage(json.dumps(data), now, now)

    def test_old_binary_is_refused_without_calling_usage(self, fake_usage):
        fake_usage.mode("old")
        with pytest.raises(QuotaError, match="1.4.13"):
            engines.quota("muse", live=True)
        calls = (fake_usage.tmp / "call.json").read_text(encoding="utf-8").splitlines()
        assert len(calls) == 1 and "--version" in calls[0]

    def test_provider_stderr_and_odd_reasons_are_not_relayed(self, fake_usage):
        fake_usage.mode("nonzero")
        with pytest.raises(QuotaError) as exc:
            engines.quota("muse", live=True)
        assert "boom" not in str(exc.value)
        data = {"schemaVersion": 1, "allowance": None, "reason": "no_allowance",
                "hint": "sentinel-secret-7d41"}
        with pytest.raises(QuotaError) as exc:
            muse_engine.parse_live_usage(json.dumps(data), 0, 0)
        assert "no_allowance" in str(exc.value) and "sentinel" not in str(exc.value)
        data.update(reason="sentinel_reason", hint="login_again")
        with pytest.raises(QuotaError) as exc:
            muse_engine.parse_live_usage(json.dumps(data), 0, 0)
        assert str(exc.value) == "muse usage reported no allowance"

    def test_provider_tier_text_is_dropped_unless_plain(self):
        now = int(time.time() * 1000)
        data = {"schemaVersion": 1, "allowance": {
            "source": "mint", "asOfMs": now, "tier": "bad\ntoken=sentinel/x",
            "window": {"usedPercent": 1, "resetsAtMs": now + 1000}}}
        assert muse_engine.parse_live_usage(json.dumps(data), now, now)["plan"] is None

    def test_wait_keeps_the_leader_unreaped_until_the_group_is_ended(self, monkeypatch):
        if not hasattr(os, "waitid"):
            pytest.skip("no waitid")
        proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        seen = []
        real = muse_engine.terminate_group
        monkeypatch.setattr(muse_engine, "terminate_group", lambda p, **k: (
            seen.append(proc.returncode), real(p, **k)))
        muse_engine._wait_exit(proc, 5)
        assert muse_engine._end_group(proc) == 0
        assert seen == [None]

    def test_waitid_path_ends_the_group_with_grace_before_a_bounded_reap(
        self, monkeypatch
    ):
        if not hasattr(os, "waitid"):
            pytest.skip("no waitid")
        proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        graces, waits = [], []
        real_terminate, real_wait = muse_engine.terminate_group, proc.wait
        monkeypatch.setattr(muse_engine, "terminate_group", lambda p, **k: (
            graces.append(k.get("grace_seconds")), real_terminate(p, **k)))
        monkeypatch.setattr(proc, "wait", lambda timeout=None: (
            waits.append(timeout), real_wait(timeout))[1])
        muse_engine._wait_exit(proc, 5)
        muse_engine._end_group(proc)
        assert graces == [0.5] and waits == [2]

    def test_a_leader_that_will_not_be_reaped_is_bounded_not_waited_forever(
        self, monkeypatch
    ):
        class Stuck:
            pid, returncode = 1234567, None

            def wait(self, timeout=None):
                assert timeout == 2
                raise subprocess.TimeoutExpired("muse", timeout)

        monkeypatch.setattr(muse_engine, "terminate_group", lambda p, **k: None)
        with pytest.raises(QuotaError, match="did not exit after teardown"):
            muse_engine._end_group(Stuck())

    @staticmethod
    def _fake_kqueue(monkeypatch, *, register_error=None, events=()):
        import types

        log = {"control": [], "closed": 0}

        class Queue:
            def control(self, changes, n, timeout=None):
                log["control"].append((changes, n, timeout))
                if changes and register_error:
                    raise register_error
                return list(events) if not changes else []

            def close(self):
                log["closed"] += 1

        monkeypatch.delattr(os, "waitid", raising=False)
        for name, val in (("KQ_FILTER_PROC", -5), ("KQ_EV_ADD", 1), ("KQ_EV_ONESHOT", 16),
                          ("KQ_NOTE_EXIT", 0x80000000), ("KQ_EV_ERROR", 0x4000)):
            monkeypatch.setattr(select, name, val, raising=False)
        monkeypatch.setattr(select, "kevent", lambda ident, **k: types.SimpleNamespace(ident=ident, **k), raising=False)
        monkeypatch.setattr(select, "kqueue", Queue, raising=False)
        return log

    def test_forced_kqueue_normal_event_returns_without_reaping(self, monkeypatch):
        import types

        proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        log = self._fake_kqueue(monkeypatch, events=[
            types.SimpleNamespace(ident=proc.pid, fflags=0x80000000, flags=0)])
        muse_engine._wait_exit(proc, 5)
        assert proc.returncode is None and log["closed"] == 1
        proc.wait()

    def test_forced_kqueue_esrch_at_registration_means_exited(self, monkeypatch):
        proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        log = self._fake_kqueue(monkeypatch, register_error=ProcessLookupError())
        muse_engine._wait_exit(proc, 5)
        assert proc.returncode is None and log["closed"] == 1
        proc.wait()

    def test_forced_kqueue_timeout_is_bounded_and_reregisters(self, monkeypatch):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                start_new_session=True)
        try:
            log = self._fake_kqueue(monkeypatch)
            monkeypatch.setattr(muse_engine, "KQUEUE_SLICE_S", 0.01)
            started = time.monotonic()
            with pytest.raises(subprocess.TimeoutExpired):
                muse_engine._wait_exit(proc, 0.05)
            assert time.monotonic() - started < 2
            registers = [c for c in log["control"] if c[0]]
            assert len(registers) >= 2 and log["closed"] == 1
        finally:
            proc.kill()
            proc.wait()

    def test_forced_neither_api_reaped_exit_signals_nothing(self, monkeypatch):
        monkeypatch.delattr(os, "waitid", raising=False)
        monkeypatch.delattr(select, "kqueue", raising=False)
        proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        muse_engine._wait_exit(proc, 5)
        assert proc.returncode == 0
        monkeypatch.setattr(muse_engine.os, "killpg", lambda *a: pytest.fail("killpg"))
        monkeypatch.setattr(muse_engine.os, "kill", lambda *a: pytest.fail("kill(pid)"))
        monkeypatch.setattr(muse_engine.os, "getpgid", lambda *a: pytest.fail("getpgid"))
        monkeypatch.setattr(
            muse_engine, "terminate_group", lambda *a, **k: pytest.fail("terminate_group")
        )
        assert muse_engine._end_group(proc) == 0

    def test_foreign_reap_on_the_waitid_path_signals_nothing(self, monkeypatch):
        if not hasattr(os, "waitid"):
            pytest.skip("no waitid")
        proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        proc.wait()  # reaped elsewhere: waitid now raises ChildProcessError
        proc.returncode = None
        monkeypatch.setattr(
            muse_engine, "terminate_group", lambda *a, **k: pytest.fail("terminate_group")
        )
        muse_engine._wait_exit(proc, 5)
        assert muse_engine._end_group(proc) is not None

    def test_forced_windows_reaped_exit_runs_no_taskkill(self, monkeypatch):
        import types

        fake_os = types.SimpleNamespace(name="nt")
        monkeypatch.setattr(muse_engine, "os", fake_os)
        monkeypatch.setattr(
            muse_engine, "terminate_group", lambda *a, **k: pytest.fail("taskkill")
        )
        monkeypatch.setattr(
            muse_engine.subprocess, "run", lambda *a, **k: pytest.fail("taskkill")
        )
        reaped = types.SimpleNamespace(pid=4242, returncode=0)
        assert muse_engine._end_group(reaped) == 0

    @pytest.mark.parametrize("name, value", [
        ("META_API_KEY", "sekret-9f3"), ("TBH_CREDENTIAL_BACKEND", "zz-backend-7"),
        ("MUSE_AUTH_PATH", "/elsewhere/auth.json"),
    ])
    def test_unhonorable_account_context_makes_no_call(
        self, fake_usage, monkeypatch, name, value
    ):
        monkeypatch.setenv(name, value)
        with pytest.raises(QuotaError, match="account mismatch") as exc:
            engines.quota("muse", live=True)
        assert value not in str(exc.value)
        assert not (fake_usage.tmp / "call.json").exists()

    def test_auth_path_equal_to_the_binary_default_is_allowed(self, fake_usage, monkeypatch):
        monkeypatch.setenv("HOME", str(fake_usage.tmp))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("MUSE_AUTH_PATH", str(fake_usage.tmp / ".config/muse/auth.json"))
        monkeypatch.setattr(muse_engine.sys, "platform", "linux")
        assert muse_engine.live_account_problem() is None

    def test_default_quota_keeps_the_old_mint_path(self, fake_serve):
        assert muse_engine.quota()["plan"] == "fake-tier"
        assert any(f.get("method") == "turn/start" for f in frames_of(fake_serve))

    def test_no_session_or_turn_is_ever_started(self, fake_usage, monkeypatch):
        monkeypatch.setattr(muse_engine, "_rpc_usage", lambda: pytest.fail("served"))
        engines.quota("muse", live=True)

    def test_missing_binary_is_unavailable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PATH", str(tmp_path))
        with pytest.raises(QuotaError, match="not on PATH"):
            engines.quota("muse", live=True)
