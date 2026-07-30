"""Unit tests for sandbox/live-agent.py harness selection."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

LIVE_AGENT = Path(__file__).resolve().parent.parent / "sandbox" / "live-agent.py"


def _load_live_agent():
    spec = importlib.util.spec_from_file_location("live_agent", LIVE_AGENT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_harness_invoke_defaults_to_opencode(monkeypatch):
    monkeypatch.delenv("R4T_SANDBOX_INVOKE", raising=False)
    mod = _load_live_agent()
    assert mod.harness_invoke()[0] == "opencode"


def test_harness_invoke_reads_env_json(monkeypatch):
    argv = ["ollama", "launch", "opencode", "--model", "llama3", "--", "run", "{prompt}"]
    monkeypatch.setenv("R4T_SANDBOX_INVOKE", json.dumps(argv))
    mod = _load_live_agent()
    assert mod.harness_invoke() == argv


def test_harness_invoke_rejects_bad_json(monkeypatch):
    monkeypatch.setenv("R4T_SANDBOX_INVOKE", "not-json")
    mod = _load_live_agent()
    with pytest.raises(SystemExit, match="R4T_SANDBOX_INVOKE"):
        mod.harness_invoke()


def test_protocol_only_skips_llm_for_tester_and_verified_lead():
    mod = _load_live_agent()
    assert mod.protocol_only("tester", "dev", "ready")
    assert mod.protocol_only("lead", "tester", "VERIFIED: battleship.py runs")
    assert not mod.protocol_only("lead", "human", "build this")
    assert not mod.protocol_only("dev", "lead", "build it")
