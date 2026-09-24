"""Muse is wired, not merely declared.

The suite's recurring defect is configuration that parses, validates and
documents cleanly with nothing behind it. These tests are the other half of
adding an engine: each one fails if a specific piece of the wiring is removed,
so `muse` cannot decay into a name the tables know and nothing honours.
"""
from __future__ import annotations

import json
import os
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
            emit({{"jsonrpc": "2.0", "id": rid,
                  "result": {{"commandId": msg["params"]["commandId"],
                              "status": "accepted", "turnId": "t1",
                              "disposition": "started", "startedNewTurn": True}}}})
            threading.Thread(target=mint, daemon=True).start()
        elif method == "turn/cancel":
            emit({{"jsonrpc": "2.0", "id": rid,
                  "result": {{"commandId": msg["params"]["commandId"],
                              "status": "accepted", "turnId": "t1"}}}})
            emit({{"jsonrpc": "2.0", "method": "turn/completed",
                  "params": {{"sessionId": "s1", "turnId": "t1"}}}})
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
