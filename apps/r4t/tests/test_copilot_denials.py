"""Copilot denial telemetry (#382 part B).

A headless copilot turn denied a path, URL or tool carries on; the denial sits
in the session's `events.jsonl`. These tests pin what r4t reads back: bounded
metadata only, and "complete" only when every coverage condition holds.

Fixture provenance (tests/fixtures/copilot-events/):
  - the `data` payloads of the denial records are REAL captures from a live
    copilot 1.0.105 headless session (2026-11-02), sanitized to generic names
    and paths (`fallback-both-families`, `deny-rule-tool-only`,
    `allowed-clean`);
  - the line ENVELOPE (`type`, `data`, `id`, `timestamp`) is INFERRED: the
    capture kept only record types and payloads;
  - `fallback-permission-only` is INFERRED: the real fallback always writes
    the failed tool record too, this is the fixture without it.
No test here starts a real CLI: a stub `copilot` is the only one on PATH.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

import dispatch
import engines.run as engine_run
from conftest import launch_for, write_path_executable
from engines import copilot
from engines.copilot import DenialProbe, format_denials, parse_events
from rig import HARNESS_PRESETS

FIXTURES = Path(__file__).parent / "fixtures" / "copilot-events"
# Strings that appear in the fixtures' raw records and must never reach a summary.
SECRETS = (
    "/srv/outside", "inventory.txt", "/srv/work", "marker", "touch /srv",
    "Permission denied because", "interactive user", "TKfixture", "intention",
)


FIXTURE_SESSION = b"11111111-1111-4111-8111-111111111111"
OWNER = FIXTURE_SESSION.decode()


def fixture(name: str) -> bytes:
    return (FIXTURES / f"{name}.jsonl").read_bytes()


def for_session(data: bytes, session: str) -> bytes:
    """The fixtures carry a placeholder session id; a probe test runs under
    its own, and the records that name a session must name that one."""
    return data.replace(FIXTURE_SESSION, session.encode())


def event(kind: str, data: dict, ident: str | None = None) -> bytes:
    body = {"type": kind, "data": data}
    if ident is not None:
        body["id"] = ident
    return json.dumps(body).encode() + b"\n"


def start(*calls: str) -> bytes:
    """The `tool.execution_start` records every real tool outcome follows."""
    return b"".join(
        event("tool.execution_start", {"toolCallId": c, "toolName": "bash"}) for c in calls
    )


SHUTDOWN = event("session.shutdown", {"shutdownType": "routine"})


def fake_binary_source(version: str, events: str = "") -> str:
    """A stub `copilot`: `--version` answers; a turn appends the file named by
    FAKE_EVENTS (or nothing) to the events file of the session it is given."""
    return (
        "import os, sys\n"
        "if '--version' in sys.argv:\n"
        f"    print({version!r})\n"
        "    sys.exit(0)\n"
        "sid = None\n"
        "for i, a in enumerate(sys.argv):\n"
        "    if a == '--session-id':\n"
        "        sid = sys.argv[i + 1]\n"
        "    elif a.startswith('--resume='):\n"
        "        sid = a.split('=', 1)[1]\n"
        "src = os.environ.get('FAKE_EVENTS')\n"
        "home = os.environ.get('COPILOT_HOME') or os.path.join(os.environ['HOME'], '.copilot')\n"
        "if sid and src:\n"
        "    folder = os.path.join(home, 'session-state', sid)\n"
        "    os.makedirs(folder, exist_ok=True)\n"
        "    with open(os.path.join(folder, 'events.jsonl'), 'ab') as out:\n"
        "        out.write(open(src, 'rb').read().replace(b'11111111-1111-4111-8111-111111111111', sid.encode()))\n"
        "sys.exit(int(os.environ.get('FAKE_EXIT', '0')))\n"
    )


@pytest.fixture
def stub_bin(tmp_path_factory, monkeypatch):
    """The only `copilot` (and `claude`) on PATH: reports the verified build."""
    bin_dir = tmp_path_factory.mktemp("stub-bin")
    write_path_executable(bin_dir, "copilot", fake_binary_source("GitHub Copilot CLI 1.0.105."))
    write_path_executable(bin_dir, "claude", "print('2.1.316')\n")
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    return bin_dir


@pytest.fixture
def home(tmp_path):
    root = tmp_path / "home"
    root.mkdir()
    return root


def child_env(home: Path, **extra) -> dict:
    return {"HOME": str(home), "PATH": os.environ["PATH"], **extra}


def events_file(home: Path, session: str, *, copilot_home: Path | None = None) -> Path:
    base = copilot_home or home / ".copilot"
    return base / "session-state" / session / "events.jsonl"


def run_probe(home, stub_bin, *, before=None, after=b"", minted=True, lock_held=False,
              env=None, isolated=False, mutate=None):
    """bind, let `mutate`/`after` change the file, collect."""
    session = str(uuid.uuid4())
    probe = DenialProbe(session=session, minted=minted, lock_held=lock_held)
    path = events_file(home, session)
    if before is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(before)
    bind(probe, env if env is not None else child_env(home), isolated=isolated)
    if mutate is not None:
        mutate(path)
    elif after:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            handle.write(for_session(after, session))
    return probe.collect(), session


def bind(probe, env, *, isolated: bool = False, binary: str = "copilot") -> None:
    """Bind a probe the way the callers do: on the one resolved launch."""
    probe.bind(launch_for(binary, env, isolated=isolated))


def head() -> bytes:
    return event("session.start", {"sessionId": "x"}) + event("user.message", {})


# --- the parser ----------------------------------------------------------------


class TestParser:
    def test_a_fallback_denial_writes_both_record_families_and_counts_once(self):
        parsed = parse_events(fixture("fallback-both-families"))
        assert parsed["count"] == 1
        assert parsed["by_class"] == {"denied-no-approval-rule-and-could-not-request-from-user": 1}
        assert parsed["by_tool"] == {"read": 1}
        assert parsed["shutdown"] and not parsed["unknown_shape"]

    def test_a_deny_rule_writes_only_the_tool_record_and_still_counts(self):
        parsed = parse_events(fixture("deny-rule-tool-only"))
        assert parsed["count"] == 1
        assert parsed["by_class"] == {"denied-by-rule": 1}
        assert parsed["by_tool"] == {"shell": 1}
        assert parsed["rule_tools"] == ["shell"] and parsed["rules_omitted"] == 1
        assert "touch" not in json.dumps(parsed)

    def test_a_permission_only_denial_counts(self):
        parsed = parse_events(fixture("fallback-permission-only"))
        assert parsed["count"] == 1
        assert parsed["by_tool"] == {"read": 1}

    def test_an_allowed_call_is_not_a_denial(self):
        parsed = parse_events(fixture("allowed-clean"))
        assert parsed["count"] == 0 and parsed["shutdown"]

    def test_deduplication_is_by_tool_call_not_by_event_id(self):
        # The same ids on two different calls count twice; different ids on
        # the same call count once.
        a = event("tool.execution_complete", {"toolCallId": "call_a", "success": False,
                                              "error": {"code": "denied", "message": "m"}}, "same")
        b = event("tool.execution_complete", {"toolCallId": "call_b", "success": False,
                                              "error": {"code": "denied", "message": "m"}}, "same")
        assert parse_events(start("call_a", "call_b") + a + b)["count"] == 2
        c = event("tool.execution_complete", {"toolCallId": "call_a", "success": False,
                                              "error": {"code": "denied", "message": "m"}}, "other")
        assert parse_events(start("call_a") + a + c)["count"] == 1

    def test_event_ids_are_validated_capped_and_never_raw(self):
        lines = start(*[f"call_{i}" for i in range(14)], "call_bad")
        for i in range(14):
            lines += event(
                "tool.execution_complete",
                {"toolCallId": f"call_{i}", "success": False, "error": {"code": "denied", "message": ""}},
                f"id-{i}",
            )
        lines += event(
            "tool.execution_complete",
            {"toolCallId": "call_bad", "success": False, "error": {"code": "denied", "message": ""}},
            "../../etc/passwd",
        )
        parsed = parse_events(lines)
        assert parsed["count"] == 15
        assert len(parsed["ids"]) == 10
        assert all(i.startswith("id-") for i in parsed["ids"])
        assert "../../etc/passwd" not in json.dumps(parsed)

    def test_the_denial_class_is_a_slug_or_other(self):
        raw = event("permission.completed", {"requestId": "r1", "toolCallId": "call_1",
                                             "result": {"kind": "denied-" + "x" * 80}})
        assert parse_events(raw)["by_class"] == {"other": 1}
        raw = event("permission.completed", {"requestId": "r1", "toolCallId": "call_1",
                                             "result": {"kind": "denied-by-rules"}})
        assert parse_events(raw)["by_class"] == {"denied-by-rules": 1}

    def test_classes_are_capped(self):
        raw = start(*[f"call_{i}" for i in range(25)]) + b"".join(
            event("permission.completed", {"requestId": f"r{i}", "toolCallId": f"call_{i}",
                                           "result": {"kind": f"denied-k{i}"}})
            for i in range(25)
        )
        by_class = parse_events(raw)["by_class"]
        assert len(by_class) <= copilot.MAX_CLASSES + 1
        assert sum(by_class.values()) == 25

    @pytest.mark.parametrize(
        "line",
        [
            b"not json\n",
            b"[1, 2]\n",
            b'{"data": {}}\n',
            event("permission.requested", {}),
            event("permission.completed", {"requestId": "r1", "result": {}}),
            event("permission.completed", {"requestId": "r1", "toolCallId": "call_1", "result": {"kind": "maybe"}}),
            event("permission.mystery", {"requestId": "r1"}),
            event("tool.execution_complete", {"toolCallId": "call_1"}),
            event("tool.execution_complete", {"toolCallId": "call_1", "success": False}),
        ],
    )
    def test_an_unknown_shape_is_flagged(self, line):
        assert parse_events(line)["unknown_shape"]

    def test_unrelated_kinds_are_ignored(self):
        parsed = parse_events(event("assistant.message", {"content": "x"}) + SHUTDOWN)
        assert not parsed["unknown_shape"] and parsed["count"] == 0

    def test_a_successful_tool_call_is_not_unknown(self):
        raw = start("call_1") + event("tool.execution_complete", {"toolCallId": "call_1", "success": True})
        assert not parse_events(raw)["unknown_shape"]

    def test_no_summary_field_carries_raw_text(self):
        for name in ("fallback-both-families", "deny-rule-tool-only", "fallback-permission-only"):
            blob = json.dumps(parse_events(fixture(name)))
            for secret in SECRETS:
                assert secret not in blob, (name, secret)


# --- the display ---------------------------------------------------------------


class TestDisplay:
    def test_complete_zero(self):
        assert format_denials({"observed": 0, "complete": True, "reason": None}) == "denied: 0 (complete)"

    def test_incomplete_with_a_denial_says_at_least(self):
        line = format_denials({"observed": 2, "complete": False, "reason": "no-shutdown"})
        assert line == "denied: at least 2 (incomplete: no-shutdown)"

    def test_incomplete_never_prints_zero(self):
        assert "0" not in format_denials({"observed": 0, "complete": False, "reason": "no-shutdown"})

    def test_unreadable_is_unknown(self):
        assert format_denials({"observed": None, "complete": False, "reason": "home-unknown"}) == (
            "denied: unknown (home-unknown)"
        )


# --- the probe: coverage -------------------------------------------------------


class TestCoverage:
    def test_a_cold_session_that_appears_with_a_full_slice_is_complete(self, home, stub_bin):
        summary, session = run_probe(home, stub_bin, after=fixture("fallback-both-families"))
        assert summary["reason"] is None, summary
        assert summary["complete"] is True
        assert summary["observed"] == 1
        assert summary["session"] == session
        assert summary["by_tool"] == {"read": 1}
        assert format_denials(summary) == "denied: 1 (complete)"

    def test_a_clean_complete_turn_says_zero(self, home, stub_bin):
        summary, _ = run_probe(home, stub_bin, after=fixture("allowed-clean"))
        assert (summary["observed"], summary["complete"]) == (0, True)

    def test_a_deny_rule_turn_is_never_zero_complete(self, home, stub_bin):
        summary, _ = run_probe(home, stub_bin, after=fixture("deny-rule-tool-only"))
        assert summary["observed"] == 1 and summary["complete"] is True
        assert summary["rule_tools"] == ["shell"] and summary["rules_omitted"] == 1
        assert "touch" not in json.dumps(summary)

    def test_a_missing_shutdown_is_at_least_n(self, home, stub_bin):
        body = fixture("fallback-both-families").replace(
            b'{"type": "session.shutdown"', b'{"type": "assistant.message"'
        )
        summary, _ = run_probe(home, stub_bin, after=body)
        assert summary["complete"] is False and summary["reason"] == "no-shutdown"
        assert summary["observed"] == 1
        assert format_denials(summary) == "denied: at least 1 (incomplete: no-shutdown)"

    def test_a_partial_last_line_is_incomplete_and_keeps_the_denial(self, home, stub_bin):
        body = fixture("fallback-both-families") + b'{"type": "session.shut'
        summary, _ = run_probe(home, stub_bin, after=body)
        assert summary["reason"] == "partial-line" and summary["observed"] == 1

    def test_an_oversize_slice_is_incomplete(self, home, stub_bin, monkeypatch):
        monkeypatch.setattr(copilot, "MAX_SLICE_BYTES", 400)
        body = fixture("fallback-both-families")
        assert len(body) > 400
        summary, _ = run_probe(home, stub_bin, after=body)
        assert summary["complete"] is False and summary["reason"] == "oversize"

    def test_an_unknown_permission_shape_is_incomplete(self, home, stub_bin):
        body = fixture("fallback-both-families") + event("permission.mystery", {"requestId": "r"})
        summary, _ = run_probe(home, stub_bin, after=body)
        assert summary["reason"] == "unknown-shape" and summary["observed"] == 1

    def test_a_malformed_complete_line_is_incomplete(self, home, stub_bin):
        body = b"garbage\n" + fixture("allowed-clean")
        summary, _ = run_probe(home, stub_bin, after=body)
        assert summary["reason"] == "unknown-shape" and summary["complete"] is False

    def test_a_truncated_file_is_incomplete(self, home, stub_bin):
        def shrink(path):
            path.write_bytes(fixture("allowed-clean")[:50])
        summary, _ = run_probe(home, stub_bin, before=fixture("allowed-clean") + head(),
                               mutate=shrink, minted=False, lock_held=True)
        assert summary["reason"] == "truncated" and summary["complete"] is False

    def test_a_replaced_file_is_incomplete_and_keeps_what_it_saw(self, home, stub_bin):
        def replace(path):
            # Written beside it first, so the new file cannot reuse the inode.
            other = path.with_name("new.jsonl")
            other.write_bytes(for_session(fixture("fallback-both-families"), path.parent.name))
            os.replace(other, path)
        summary, _ = run_probe(home, stub_bin, before=head(), mutate=replace,
                               minted=False, lock_held=True)
        assert summary["reason"] == "file-replaced" and summary["complete"] is False
        assert summary["observed"] == 1  # an observed denial survives the failure

    def test_an_unverified_newer_binary_is_never_complete(self, home, tmp_path):
        bin_dir = tmp_path / "newbin"
        write_path_executable(bin_dir, "copilot", fake_binary_source("GitHub Copilot CLI 1.0.106."))
        env = child_env(home, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"])
        summary, _ = run_probe(home, None, after=fixture("allowed-clean"), env=env)
        assert summary["reason"] == "unverified-version" and summary["complete"] is False

    def test_an_unreadable_version_is_unverified(self, home, tmp_path):
        bin_dir = tmp_path / "nobin"
        write_path_executable(bin_dir, "copilot", "print('no digits here')\n")
        env = child_env(home, PATH=str(bin_dir))
        summary, _ = run_probe(home, None, after=fixture("allowed-clean"), env=env)
        assert summary["reason"] == "unverified-version"

    def test_a_resumed_pin_is_shared_pin_even_with_the_lock(self, home, stub_bin):
        summary, _ = run_probe(home, stub_bin, before=head(), after=fixture("allowed-clean"),
                               minted=False, lock_held=True)
        assert summary["reason"] == "shared-pin" and summary["complete"] is False

    def test_a_founded_pin_without_the_lock_is_shared_pin(self, home, stub_bin):
        summary, _ = run_probe(home, stub_bin, after=fixture("allowed-clean"),
                               minted=False, lock_held=False)
        assert summary["reason"] == "shared-pin"

    def test_a_founded_pin_under_the_lock_can_be_complete(self, home, stub_bin):
        summary, _ = run_probe(home, stub_bin, after=fixture("fallback-both-families"),
                               minted=False, lock_held=True)
        assert summary["complete"] is True and summary["observed"] == 1

    def test_a_resumed_pin_counts_only_the_new_slice(self, home, stub_bin):
        before = fixture("fallback-both-families")
        summary, _ = run_probe(home, stub_bin, before=before, after=fixture("allowed-clean"),
                               minted=False, lock_held=True)
        assert summary["observed"] == 0 and summary["reason"] == "shared-pin"


class TestUnknown:
    def test_a_missing_file_is_unknown(self, home, stub_bin):
        summary, _ = run_probe(home, stub_bin)
        assert summary["observed"] is None and summary["reason"] == "session-unknown"
        assert format_denials(summary) == "denied: unknown (session-unknown)"

    def test_an_unknown_home_is_never_the_hosts(self, home, stub_bin, monkeypatch):
        host = home / "hosthome"
        monkeypatch.setenv("HOME", str(host))
        session = str(uuid.uuid4())
        events_file(host, session).parent.mkdir(parents=True)
        events_file(host, session).write_bytes(fixture("fallback-both-families"))
        probe = DenialProbe(session=session, minted=True)
        bind(probe, {"PATH": os.environ["PATH"]})
        summary = probe.collect()
        assert summary["observed"] is None and summary["reason"] == "home-unknown"

    @pytest.mark.parametrize("isolated", [True])
    def test_an_isolated_child_is_unknown(self, home, stub_bin, isolated):
        summary, _ = run_probe(home, stub_bin, after=fixture("allowed-clean"), isolated=isolated)
        assert summary["reason"] == "home-unknown" and summary["observed"] is None

    def test_a_relative_copilot_home_is_unknown(self, home, stub_bin):
        summary, _ = run_probe(home, stub_bin, env=child_env(home, COPILOT_HOME="rel/dir"))
        assert summary["reason"] == "home-unknown"

    def test_the_events_file_follows_copilot_home(self, home, stub_bin, tmp_path):
        other = tmp_path / "elsewhere"
        session = str(uuid.uuid4())
        probe = DenialProbe(session=session, minted=True)
        bind(probe, child_env(home, COPILOT_HOME=str(other)))
        target = events_file(home, session, copilot_home=other)
        target.parent.mkdir(parents=True)
        target.write_bytes(for_session(fixture("allowed-clean"), session))
        assert probe.collect()["complete"] is True

    def test_a_session_that_is_not_a_slug_is_unknown(self, home, stub_bin):
        probe = DenialProbe(session="../escape", minted=False, lock_held=True)
        bind(probe, child_env(home))
        assert probe.collect()["reason"] == "session-unknown"

    def test_a_reader_exception_is_reader_error(self, home, stub_bin, monkeypatch):
        def boom(_):
            raise RuntimeError("secret /srv/outside")
        monkeypatch.setattr(copilot, "parse_events", boom)
        summary, _ = run_probe(home, stub_bin, after=fixture("allowed-clean"))
        assert summary["reason"] == "reader-error" and summary["observed"] is None
        assert "secret" not in json.dumps(summary)

    def test_an_unbound_probe_reports_nothing(self):
        assert DenialProbe(session="s", minted=True).collect() is None

    def test_the_reason_is_always_from_the_fixed_set(self, home, stub_bin):
        summary, _ = run_probe(home, stub_bin, after=b"x\n")
        assert summary["reason"] in copilot.REASONS


# --- through a real turn -------------------------------------------------------


@pytest.fixture
def turn_home(tmp_path, monkeypatch, stub_bin):
    root = tmp_path / "turnhome"
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.delenv("COPILOT_HOME", raising=False)
    monkeypatch.setattr(copilot, "supports_usage_file", lambda *a: False)
    return root


def run_turn(tmp_path, turn_home, name, *, exit_code="0", session=None, extra_env=None):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    env = {**os.environ, "FAKE_EVENTS": str(FIXTURES / f"{name}.jsonl"),
           "FAKE_EXIT": exit_code, "HOME": str(turn_home), **(extra_env or {})}
    record: dict = {}
    # The roster's argv is not under test; the stub only reads --session-id.
    code = engine_run.execute(
        "copilot", "hi", dir_path=work, model=None, agent=None, timeout=30,
        scaffold=False, record=record, env=env, session=session,
    )
    return code, record


class TestEngineRun:
    def test_a_turn_mints_a_session_id_and_reports_the_denial(
        self, tmp_path, turn_home, monkeypatch, capsys
    ):
        monkeypatch.setattr(copilot, "supports_usage_file", lambda *a: True)
        code, record = run_turn(tmp_path, turn_home, "deny-rule-tool-only")
        assert code == 0
        denials = record["denials"]
        assert denials["observed"] == 1 and denials["complete"] is True
        uuid.UUID(denials["session"])
        assert "denied: 1 (complete)" in capsys.readouterr().err

    def test_the_exit_code_is_kept_whatever_the_denials_say(
        self, tmp_path, turn_home, monkeypatch
    ):
        monkeypatch.setattr(copilot, "supports_usage_file", lambda *a: True)
        code, record = run_turn(tmp_path, turn_home, "fallback-both-families", exit_code="7")
        assert code == 7 and record["denials"]["observed"] == 1

    def test_a_reader_failure_keeps_the_exit_code(self, tmp_path, turn_home, monkeypatch, capsys):
        monkeypatch.setattr(copilot, "supports_usage_file", lambda *a: True)
        monkeypatch.setattr(copilot, "parse_events", lambda _: 1 / 0)
        code, record = run_turn(tmp_path, turn_home, "fallback-both-families", exit_code="3")
        assert code == 3
        assert record["denials"]["reason"] == "reader-error"
        assert "denied: unknown (reader-error)" in capsys.readouterr().err

    def test_a_pinned_turn_passes_no_second_session_flag(self, tmp_path, turn_home, monkeypatch):
        monkeypatch.setattr(copilot, "supports_usage_file", lambda *a: True)
        pin = str(uuid.uuid4())
        _, record = run_turn(tmp_path, turn_home, "allowed-clean", session=pin)
        assert record["denials"]["session"] == pin
        # a bare `engine run --session` holds no lock: never complete
        assert record["denials"]["reason"] == "shared-pin"

    def test_an_old_binary_gets_no_minted_flag(self, tmp_path, turn_home):
        # supports_usage_file is False (the fixture): no flag it may reject.
        _, record = run_turn(tmp_path, turn_home, "allowed-clean")
        assert "denials" not in record

    def test_a_turn_that_writes_no_events_is_unknown_not_zero(
        self, tmp_path, turn_home, monkeypatch, capsys
    ):
        monkeypatch.setattr(copilot, "supports_usage_file", lambda *a: True)
        work = tmp_path / "work"
        work.mkdir()
        record: dict = {}
        env = {**os.environ, "HOME": str(turn_home)}
        env.pop("FAKE_EVENTS", None)
        engine_run.execute("copilot", "hi", dir_path=work, model=None, agent=None,
                           timeout=30, scaffold=False, record=record, env=env)
        assert record["denials"]["observed"] is None
        assert "denied: unknown (session-unknown)" in capsys.readouterr().err

    def test_nothing_is_told_to_the_owner(self, tmp_path, turn_home, monkeypatch):
        monkeypatch.setattr(copilot, "supports_usage_file", lambda *a: True)
        sent: list = []
        import subprocess as sp
        real = sp.run
        monkeypatch.setattr(sp, "run", lambda argv, *a, **k: (
            sent.append(argv) if "tell" in str(argv[0]) else None, real(argv, *a, **k))[1])
        run_turn(tmp_path, turn_home, "fallback-both-families")
        assert sent == []

    def test_other_engines_report_no_denials(self, tmp_path, turn_home):
        with copilot.turn_instruments("claude", scratch=tmp_path) as instruments:
            assert instruments.denials is None
            assert instruments.flags() == []


class TestFlags:
    def test_a_minted_probe_adds_session_id_and_a_pinned_one_does_not(self, monkeypatch, tmp_path):
        monkeypatch.setattr(copilot, "supports_usage_file", lambda *a: True)
        with copilot.turn_instruments("copilot", scratch=tmp_path) as minted:
            assert minted.flags()[-2:] == ["--session-id", minted.denials.session]
        with copilot.turn_instruments("copilot", scratch=tmp_path, session="pin") as pinned:
            assert "--session-id" not in pinned.flags()
            assert pinned.denials.session == "pin"


# --- the roster ----------------------------------------------------------------


class TestRoster:
    def test_a_roster_turn_logs_the_denial_and_keeps_it_out_of_spend(
        self, tmp_path, monkeypatch, r4t_home
    ):
        monkeypatch.setattr(copilot, "supports_usage_file", lambda *a: True)
        logged: list[str] = []
        meta: dict = {}
        monkeypatch.setattr(dispatch.state, "append_log", lambda node, line: logged.append(line))
        monkeypatch.setattr(dispatch.state, "update_meta",
                            lambda node, member, **kw: meta.update(kw))
        probe = DenialProbe(session=str(uuid.uuid4()), minted=True)
        probe.bound = True
        probe._early = "home-unknown"
        instruments = copilot.TurnInstruments(denials=probe)
        dispatch._record_measurement({"R4T_NODE": "n", "R4T_MEMBER": "m"}, instruments)
        assert logged == ["r4t: denied: unknown (home-unknown)"]
        assert meta["last_denials"]["reason"] == "home-unknown"
        assert "last_spend" not in meta


class TestVersionProbe:
    def test_a_stand_in_binary_is_never_run_for_its_version(self, home, tmp_path):
        log = tmp_path / "ran"
        bin_dir = tmp_path / "wrap"
        write_path_executable(
            bin_dir, "my-wrapper",
            f"import pathlib\npathlib.Path({str(log)!r}).write_bytes(b'x')\nprint('1.0.105')\n",
        )
        session = str(uuid.uuid4())
        probe = DenialProbe(session=session, minted=True)
        bind(probe, child_env(home, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"]),
             binary=str(bin_dir / "my-wrapper"))
        target = events_file(home, session)
        target.parent.mkdir(parents=True)
        target.write_bytes(fixture("allowed-clean"))
        assert not log.exists()
        assert probe.collect()["reason"] == "unverified-version"

    @pytest.mark.parametrize("name", ["copilot.exe", "Copilot.EXE", "copilot.cmd"])
    def test_the_cli_by_any_windows_suffix_is_asked_its_version(self, home, tmp_path, name):
        bin_dir = tmp_path / "winbin"
        write_path_executable(bin_dir, name, fake_binary_source("GitHub Copilot CLI 1.0.105."))
        session = str(uuid.uuid4())
        probe = DenialProbe(session=session, minted=True)
        bind(probe, child_env(home, PATH=str(bin_dir)), binary=str(bin_dir / name))
        target = events_file(home, session)
        target.parent.mkdir(parents=True)
        target.write_bytes(for_session(fixture("allowed-clean"), session))
        assert probe.collect()["complete"] is True

    @pytest.mark.parametrize("value", ["rel/dir", "~/copilot", "./x"])
    def test_a_home_upstream_would_refuse_is_an_unknown_home(self, home, stub_bin, value):
        # `engine_home` refuses a relative or `~` state home; a turn never
        # spawns with one, and a probe asked about one cannot name it.
        probe = DenialProbe(session=str(uuid.uuid4()), minted=True)
        bind(probe, child_env(home, COPILOT_HOME=value))
        summary = probe.collect()
        assert summary["reason"] == "home-unknown" and summary["observed"] is None


# --- review round 1 (#382 part B) ---------------------------------------------
#
# Four findings and the gaps the audit found beside them. Every test here fails
# on 590d8fe.


def denied_exec(call=None, ident=None, message="denied"):
    data = {"success": False, "error": {"code": "denied", "message": message}}
    if call is not None:
        data["toolCallId"] = call
    return event("tool.execution_complete", data, ident)


def denied_perm(request="r1", call=None, ident=None, kind="denied-by-rules"):
    data = {"result": {"kind": kind}}
    if request is not None:
        data["requestId"] = request
    if call is not None:
        data["toolCallId"] = call
    return event("permission.completed", data, ident)


RULE_MESSAGE = "Permission to run this tool was denied due to the following rules: "


class TestRuleText:
    """P1: no rule text, command, argument, filename or host is ever kept."""

    LEAKS = (
        "shell(echo SYNTHETIC_SECRET)", "write(private-notes.txt)",
        "url(private.example.test)", "shell(touch)", "shell(git push)",
        "write(notes)", "shell(synthetic_secret)", "url(localhost)",
        "SYNTHETIC_SECRET(tool)",
    )

    def message(self, *rules):
        return RULE_MESSAGE + " ".join(f"`{r}`" for r in rules)

    def test_no_rule_body_reaches_the_parse(self):
        raw = denied_exec("call_1", message=self.message(*self.LEAKS))
        blob = json.dumps(parse_events(raw))
        for text in ("SYNTHETIC", "private", "notes", "touch", "git", "echo", "localhost", "example"):
            assert text not in blob, text

    def test_only_the_fixed_tool_and_a_count_remain(self):
        raw = denied_exec("call_1", message=self.message(
            "shell(echo SYNTHETIC_SECRET)", "write(private-notes.txt)",
            "url(private.example.test)", "SYNTHETIC_SECRET(tool)"))
        parsed = parse_events(raw)
        assert parsed["rule_tools"] == ["shell", "url", "write"]
        assert parsed["rules_omitted"] == 4
        assert "rules" not in parsed

    def test_an_unknown_head_is_counted_and_never_named(self):
        parsed = parse_events(denied_exec("call_1", message=self.message("my-server(do_thing)")))
        assert parsed["rule_tools"] == [] and parsed["rules_omitted"] == 1

    def test_the_summary_through_a_real_probe_holds_no_rule_text(self, home, stub_bin):
        body = start("call_1") + denied_exec("call_1", message=self.message(*self.LEAKS)) + SHUTDOWN
        summary, _ = run_probe(home, stub_bin, after=body)
        assert summary["observed"] == 1 and summary["complete"] is True
        blob = json.dumps(summary)
        for text in ("SYNTHETIC", "private", "notes", "touch", "git", "echo", "localhost", "example"):
            assert text not in blob, text
        assert "rules" not in summary

    def test_through_the_engine_record_and_stderr(self, tmp_path, turn_home, monkeypatch, capsys):
        monkeypatch.setattr(copilot, "supports_usage_file", lambda *a: True)
        source = tmp_path / "events.jsonl"
        source.write_bytes(
            denied_exec("call_1", message=self.message(*self.LEAKS)) + SHUTDOWN)
        work = tmp_path / "work"
        work.mkdir()
        record: dict = {}
        env = {**os.environ, "FAKE_EVENTS": str(source), "HOME": str(turn_home)}
        engine_run.execute("copilot", "hi", dir_path=work, model=None, agent=None, timeout=30,
                           scaffold=False, record=record, env=env)
        blob = json.dumps(record) + capsys.readouterr().err
        for text in ("SYNTHETIC", "private", "notes", "touch", "git push", "localhost", "example"):
            assert text not in blob, text
        assert record["denials"]["rule_tools"] == ["shell", "url", "write"]

    def test_through_the_roster_sinks(self, tmp_path, turn_home, monkeypatch, r4t_home):
        import rig as rig_module
        monkeypatch.setattr(copilot, "supports_usage_file", lambda *a: True)
        source = tmp_path / "events.jsonl"
        source.write_bytes(denied_exec("call_1", message=self.message(*self.LEAKS)) + SHUTDOWN)
        metas: list[dict] = []
        logs: list[str] = []
        monkeypatch.setattr(dispatch.state, "update_meta", lambda n, m, **kw: metas.append(kw))
        monkeypatch.setattr(dispatch.state, "append_log", lambda n, line: logs.append(line))
        work = tmp_path / "work"
        work.mkdir()
        selected = rig_module.rig_from_spec(
            "t", {"preset": "copilot", "invoke": rig_module.build_preset_invoke("copilot"), "mcp": False})
        env = {**os.environ, "FAKE_EVENTS": str(source), "HOME": str(turn_home),
               "R4T_NODE": "n", "R4T_MEMBER": "m"}
        dispatch.run_harness(selected, "hi", work, env=env)
        blob = json.dumps(metas) + "\n".join(logs)
        for text in ("SYNTHETIC", "private", "notes", "touch", "git push", "localhost", "example"):
            assert text not in blob, text
        assert any("last_denials" in m for m in metas)

    def test_the_seal_refuses_a_summary_that_holds_rule_text(self):
        bad = copilot._empty_summary("s", "no-shutdown")
        bad["rule_tools"] = ["shell(echo SYNTHETIC_SECRET)"]
        assert copilot._seal(bad)["reason"] == "reader-error"
        bad = copilot._empty_summary("s", "no-shutdown")
        bad["rules"] = ["shell(touch)"]
        assert copilot._seal(bad)["reason"] == "reader-error"


class TestCorrelation:
    """P2: a denial without a usable id is still a denial."""

    def test_a_tool_denial_without_a_call_id_counts_and_is_incomplete(self, home, stub_bin):
        summary, _ = run_probe(home, stub_bin, after=denied_exec() + SHUTDOWN)
        assert summary["observed"] == 1 and summary["complete"] is False
        assert summary["reason"] == "unknown-shape"
        assert format_denials(summary) == "denied: at least 1 (incomplete: unknown-shape)"

    def test_a_malformed_call_id_counts(self):
        assert parse_events(denied_exec("bad id!"))["count"] == 1

    @pytest.mark.parametrize("record", [
        denied_perm(call=None),                       # no call id anywhere
        denied_perm(request=None, call="call_1"),     # no request id
        denied_perm(request=None, call=None),
        denied_perm(request="bad id!", call="call_1"),
    ])
    def test_a_permission_denial_missing_an_id_counts_and_is_incomplete(self, record, home, stub_bin):
        parsed = parse_events(record + SHUTDOWN)
        assert parsed["count"] == 1 and parsed["unknown_shape"]
        summary, _ = run_probe(home, stub_bin, after=record + SHUTDOWN)
        assert summary["observed"] == 1 and summary["complete"] is False

    def test_a_request_can_supply_the_call_but_the_gap_still_lowers_coverage(self):
        request = event("permission.requested", {"requestId": "r1", "ownerSessionId": OWNER, "permissionRequest": {
            "kind": "read", "toolCallId": "call_1"}})
        parsed = parse_events(request + denied_perm(call=None) + denied_exec("call_1"))
        assert parsed["count"] == 1 and parsed["unknown_shape"]

    # (a) the cross-family case: possibly one operation, never 2
    def test_an_uncorrelated_permission_denial_with_a_correlated_tool_denial_is_one(self):
        parsed = parse_events(denied_perm(call=None) + denied_exec("call1") + SHUTDOWN)
        assert parsed["count"] == 1 and parsed["unknown_shape"]

    def test_and_the_other_way_round(self):
        parsed = parse_events(denied_exec() + denied_perm(call="call1") + SHUTDOWN)
        assert parsed["count"] == 1 and parsed["unknown_shape"]

    # (b) duplicates and replays
    def test_replayed_uncorrelated_denials_are_at_least_one_not_n(self):
        raw = denied_exec(ident="e1") * 3 + denied_perm(ident="p1") * 2
        parsed = parse_events(raw)
        assert parsed["count"] == 1 and parsed["unknown_shape"]

    # (c) only uncorrelated
    def test_only_uncorrelated_denials_are_at_least_one(self, home, stub_bin):
        summary, _ = run_probe(home, stub_bin, after=denied_exec() + denied_perm(call=None) + SHUTDOWN)
        assert summary["observed"] == 1 and summary["complete"] is False
        assert summary["by_tool"] and sum(summary["by_tool"].values()) == 1
        assert sum(summary["by_class"].values()) == 1

    # (d) correlated only: exact
    def test_correlated_denials_stay_exact_and_complete(self, home, stub_bin):
        asked = event("permission.requested", {"requestId": "r1", "ownerSessionId": OWNER,
                                               "permissionRequest": {"kind": "shell", "toolCallId": "call_b"}})
        raw = (start("call_a", "call_b") + asked + denied_exec("call_a") + denied_exec("call_b")
               + denied_perm(call="call_b") + SHUTDOWN)
        summary, _ = run_probe(home, stub_bin, after=raw)
        assert summary["observed"] == 2 and summary["complete"] is True

    def test_uncorrelated_denials_never_add_to_the_correlated_count(self):
        parsed = parse_events(start("call_a", "call_b") + denied_exec("call_a") + denied_exec("call_b") + denied_exec())
        assert parsed["count"] == 2

    def test_an_uncorrelated_denial_is_never_zero_anywhere(self):
        for record in (denied_exec(), denied_perm(call=None), denied_perm(request=None)):
            assert parse_events(record)["count"] >= 1

    def test_event_ids_count_once(self):
        raw = denied_exec("call_a", "same") + denied_exec("call_b", "same")
        assert parse_events(raw)["ids"] == ["same"]


class TestUnknownShapes:
    """P2: a malformed record in a counted family defeats "complete"."""

    def test_a_request_with_only_an_id_is_not_complete(self, home, stub_bin):
        body = event("permission.requested", {"requestId": "r1"}) + SHUTDOWN
        summary, _ = run_probe(home, stub_bin, after=body)
        assert summary["complete"] is False and summary["reason"] == "unknown-shape"

    @pytest.mark.parametrize("data", [None, "x", [], {}, {"shutdownType": 3}, {"shutdownType": "A B"}, {"shutdownType": "other"}])
    def test_a_shutdown_that_is_not_well_formed_is_no_evidence(self, data, home, stub_bin):
        summary, _ = run_probe(home, stub_bin, after=event("session.shutdown", data))
        assert summary["complete"] is False
        assert summary["reason"] in ("unknown-shape", "no-shutdown")

    def test_a_null_shutdown_is_not_a_shutdown(self):
        parsed = parse_events(event("session.shutdown", None))
        assert not parsed["shutdown"] and parsed["unknown_shape"]

    @pytest.mark.parametrize("asked", [
        {"requestId": "r1", "permissionRequest": "x"},
        {"requestId": "r1", "permissionRequest": {}},
        {"requestId": "r1", "permissionRequest": {"kind": "read", "toolCallId": "bad id!"}},
    ])
    def test_a_request_without_its_minimum_schema(self, asked):
        assert parse_events(event("permission.requested", asked))["unknown_shape"]

    def test_a_non_object_body_in_a_permission_record(self):
        assert parse_events(event("permission.completed", None))["unknown_shape"]

    def test_a_request_nobody_answered_is_not_proof_of_nothing(self):
        raw = event("permission.requested", {"requestId": "r1", "ownerSessionId": OWNER, "permissionRequest": {"kind": "read"}})
        assert parse_events(raw)["unknown_shape"]

    def test_a_request_id_used_twice_is_ambiguous(self):
        one = event("permission.requested", {"requestId": "r1", "ownerSessionId": OWNER, "permissionRequest": {"kind": "read"}})
        done = event("permission.completed", {"requestId": "r1", "result": {"kind": "approved"}})
        assert not parse_events(one + done)["unknown_shape"]
        assert parse_events(one + one + done)["unknown_shape"]

    def test_an_answered_request_is_fine(self):
        raw = (event("permission.requested", {"requestId": "r1", "ownerSessionId": OWNER, "permissionRequest": {"kind": "read"}})
               + event("permission.completed", {"requestId": "r1", "result": {"kind": "approved"}}))
        assert not parse_events(raw)["unknown_shape"]

    def test_a_request_owned_by_another_session_is_not_attributed(self):
        raw = fixture("fallback-both-families")
        assert not parse_events(raw)["unknown_shape"]
        assert not parse_events(raw, FIXTURE_SESSION.decode())["unknown_shape"]
        assert parse_events(raw, "22222222-2222-4222-8222-222222222222")["unknown_shape"]

    def test_a_start_record_for_another_session(self):
        raw = event("session.start", {"sessionId": "other"}) + SHUTDOWN
        assert parse_events(raw, "mine")["unknown_shape"]
        assert not parse_events(raw)["unknown_shape"]

    def test_a_foreign_session_through_a_probe(self, home, stub_bin):
        summary, _ = run_probe(home, stub_bin, after=event("session.start", {"sessionId": "other"}) + SHUTDOWN)
        assert summary["complete"] is False and summary["reason"] == "unknown-shape"

    def test_counts_survive_the_malformed_neighbours(self, home, stub_bin):
        body = fixture("fallback-both-families").replace(
            b'"type": "session.shutdown", "data": {"shutdownType": "routine"}',
            b'"type": "session.shutdown", "data": null')
        summary, _ = run_probe(home, stub_bin, after=body)
        assert summary["observed"] == 1 and summary["complete"] is False


class TestSummaryGate:
    """Audit: every persisted field passes one gate."""

    def test_a_session_that_is_not_a_slug_is_never_stored(self, home, stub_bin):
        probe = DenialProbe(session="../escape/secret", minted=False, lock_held=True)
        bind(probe, child_env(home), isolated=True)
        assert probe.collect()["session"] is None
        assert copilot._empty_summary("a b", "home-unknown")["session"] is None

    def test_an_events_file_that_is_not_a_regular_file_does_not_hang(self, home, stub_bin):
        session = str(uuid.uuid4())
        probe = DenialProbe(session=session, minted=True)
        bind(probe, child_env(home))
        path = events_file(home, session)
        path.parent.mkdir(parents=True)
        os.mkfifo(path)
        assert probe.collect()["reason"] == "reader-error"

    def test_the_gate_accepts_a_real_summary(self, home, stub_bin):
        summary, _ = run_probe(home, stub_bin, after=fixture("fallback-both-families"))
        assert copilot._seal(summary) == summary

    @pytest.mark.parametrize("field,value", [
        ("by_tool", {"cat /etc/passwd": 1}),
        ("by_tool", {"shell": -1}),
        ("by_class", {"Has Space": 1}),
        ("event_ids", ["../x"]),
        ("event_ids", ["a", "a"]),
        ("rules_omitted", "5"),
        ("rules_omitted", True),
        ("observed", 10 ** 6),
        ("reason", "free text"),
        ("session", "a/b"),
        ("extra", 1),
    ])
    def test_the_gate_refuses_what_the_vocabulary_does_not_allow(self, field, value, home, stub_bin):
        summary, _ = run_probe(home, stub_bin, after=fixture("fallback-both-families"))
        summary = {**summary, field: value}
        assert copilot._seal(summary)["reason"] == "reader-error"

    def test_complete_with_a_reason_is_refused(self, home, stub_bin):
        summary, _ = run_probe(home, stub_bin, after=fixture("allowed-clean"))
        assert copilot._seal({**summary, "reason": "no-shutdown"})["reason"] == "reader-error"
        assert copilot._seal({**summary, "observed": None})["reason"] == "reader-error"


# --- finding 4: every exit path -----------------------------------------------


def _boom(*args, **kwargs):
    raise RuntimeError("sink down")


def _boom_measure(self, **kwargs):
    raise RuntimeError("collector down")


def write_denial(home: Path, session: str) -> None:
    path = events_file(home, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(denied_exec("call_1") )


class TestEveryExitPath:
    def run(self, tmp_path, turn_home, monkeypatch, spawn, **kw):
        monkeypatch.setattr(copilot, "supports_usage_file", lambda *a: True)
        monkeypatch.setattr(engine_run, "_spawn", spawn)
        work = tmp_path / "work"
        work.mkdir(exist_ok=True)
        record: dict = {}
        env = {**os.environ, "HOME": str(turn_home)}
        return record, lambda: engine_run.execute(
            "copilot", "hi", dir_path=work, model=None, agent=None, timeout=30,
            scaffold=False, record=record, env=env, **kw)

    def session_of(self, argv):
        return argv[argv.index("--session-id") + 1]

    def test_a_spawn_failure_is_collected_as_unknown(self, tmp_path, turn_home, monkeypatch, capsys):
        # A binary that is not there: the real _spawn raises RunError.
        monkeypatch.setattr(copilot, "supports_usage_file", lambda *a: True)
        monkeypatch.setattr(
            engine_run, "_build_argv_template",
            lambda *a, **k: (["/nonexistent/copilot", *k["instruments"].flags()], None))
        work = tmp_path / "work"
        work.mkdir()
        record: dict = {}
        with pytest.raises(engine_run.RunError):
            engine_run.execute("copilot", "hi", dir_path=work, model=None, agent=None, timeout=30,
                               scaffold=False, record=record,
                               env={**os.environ, "HOME": str(turn_home)})
        assert record["denials"]["observed"] is None
        assert record["denials"]["reason"] in copilot.REASONS
        assert "denied: unknown (" in capsys.readouterr().err

    def test_an_interrupt_after_a_written_denial_is_recorded_and_still_raised(
        self, tmp_path, turn_home, monkeypatch
    ):
        def spawn(launch, argv, timeout):
            write_denial(turn_home, self.session_of(argv))
            raise KeyboardInterrupt
        record, call = self.run(tmp_path, turn_home, monkeypatch, spawn)
        with pytest.raises(KeyboardInterrupt):
            call()
        assert record["denials"]["observed"] == 1 and record["denials"]["complete"] is False

    def test_an_exception_keeps_its_type_and_message(self, tmp_path, turn_home, monkeypatch):
        def spawn(launch, argv, timeout):
            write_denial(turn_home, self.session_of(argv))
            raise engine_run.RunError("original failure")
        record, call = self.run(tmp_path, turn_home, monkeypatch, spawn)
        with pytest.raises(engine_run.RunError, match="original failure"):
            call()
        assert record["denials"]["observed"] == 1

    def test_a_failing_collector_changes_nothing(self, tmp_path, turn_home, monkeypatch):
        def spawn(launch, argv, timeout):
            raise engine_run.RunError("original failure")
        record, call = self.run(tmp_path, turn_home, monkeypatch, spawn)
        monkeypatch.setattr(copilot.TurnInstruments, "measure",
                            _boom_measure)
        with pytest.raises(engine_run.RunError, match="original failure"):
            call()

    def test_a_failing_collector_keeps_the_exit_code(self, tmp_path, turn_home, monkeypatch):
        record, call = self.run(tmp_path, turn_home, monkeypatch, lambda *a: 9)
        monkeypatch.setattr(copilot.TurnInstruments, "measure", _boom_measure)
        assert call() == 9

    def test_a_non_zero_exit_is_still_measured_once(self, tmp_path, turn_home, monkeypatch):
        collected: list[int] = []
        real = copilot.DenialProbe.collect
        monkeypatch.setattr(copilot.DenialProbe, "collect",
                            lambda self, expect=False: (collected.append(1), real(self, expect))[1])
        def spawn(launch, argv, timeout):
            write_denial(turn_home, self.session_of(argv))
            return 5
        record, call = self.run(tmp_path, turn_home, monkeypatch, spawn)
        assert call() == 5
        assert len(collected) == 1 and record["denials"]["observed"] == 1

    def test_a_timeout_return_is_measured_once(self, tmp_path, turn_home, monkeypatch):
        collected: list[int] = []
        real = copilot.DenialProbe.collect
        monkeypatch.setattr(copilot.DenialProbe, "collect",
                            lambda self, expect=False: (collected.append(1), real(self, expect))[1])
        record, call = self.run(tmp_path, turn_home, monkeypatch, lambda *a: 124)
        assert call() == 124 and len(collected) == 1

    def test_an_unfinished_turn_reads_no_spend_or_spans(self, tmp_path, turn_home, monkeypatch):
        def spawn(launch, argv, timeout):
            raise engine_run.RunError("x")
        record, call = self.run(tmp_path, turn_home, monkeypatch, spawn)
        with pytest.raises(engine_run.RunError):
            call()
        assert "otel" not in record and "spend" not in record

    def test_a_turn_that_stopped_before_the_bind_records_nothing_on_the_engine_path(
        self, tmp_path, turn_home, monkeypatch
    ):
        monkeypatch.setattr(copilot, "supports_usage_file", lambda *a: True)
        def refuse():
            raise RuntimeError("fuel")
        record: dict = {}
        with pytest.raises(RuntimeError, match="fuel"):
            engine_run.execute("copilot", "hi", dir_path=tmp_path, model=None, agent=None,
                               timeout=30, scaffold=False, record=record, charge_hook=refuse,
                               env={**os.environ, "HOME": str(turn_home)})
        assert "denials" not in record


class TestRosterEveryExitPath:
    def harness(self, tmp_path, turn_home, monkeypatch, turn):
        import rig as rig_module
        monkeypatch.setattr(copilot, "supports_usage_file", lambda *a: True)
        metas: list[dict] = []
        logs: list[str] = []
        monkeypatch.setattr(dispatch.state, "update_meta", lambda n, m, **kw: metas.append(kw))
        monkeypatch.setattr(dispatch.state, "append_log", lambda n, line: logs.append(line))
        monkeypatch.setattr(dispatch, "_run_harness_turn", turn)
        work = tmp_path / "work"
        work.mkdir(exist_ok=True)
        selected = rig_module.rig_from_spec(
            "t", {"preset": "copilot", "invoke": rig_module.build_preset_invoke("copilot"), "mcp": False})
        env = {**os.environ, "HOME": str(turn_home), "R4T_NODE": "n", "R4T_MEMBER": "m"}
        return metas, logs, lambda: dispatch.run_harness(selected, "hi", work, env=env)

    def test_an_interrupt_is_recorded_and_re_raised(self, tmp_path, turn_home, monkeypatch, r4t_home):
        def turn(rig, prompt, *, launch, variant, instruments, turn_context):
            instruments.denials.bind(launch)
            write_denial(turn_home, instruments.denials.session)
            raise KeyboardInterrupt
        metas, logs, call = self.harness(tmp_path, turn_home, monkeypatch, turn)
        with pytest.raises(KeyboardInterrupt):
            call()
        recorded = [m["last_denials"] for m in metas if "last_denials" in m]
        assert len(recorded) == 1 and recorded[0]["observed"] == 1
        assert not any("last_spend" in m for m in metas)

    def test_an_exception_keeps_its_type(self, tmp_path, turn_home, monkeypatch, r4t_home):
        def turn(rig, prompt, *, launch, variant, instruments, turn_context):
            raise OSError("original")
        metas, logs, call = self.harness(tmp_path, turn_home, monkeypatch, turn)
        with pytest.raises(OSError, match="original"):
            call()

    def test_a_failing_sink_changes_nothing(self, tmp_path, turn_home, monkeypatch, r4t_home):
        def turn(rig, prompt, *, launch, variant, instruments, turn_context):
            return 4, "out", 0.0, False
        metas, logs, call = self.harness(tmp_path, turn_home, monkeypatch, turn)
        monkeypatch.setattr(dispatch.state, "update_meta", _boom)
        monkeypatch.setattr(dispatch.state, "append_log", _boom)
        assert call() == (4, "out", 0.0, False)

    def test_a_turn_refused_before_the_spawn_replaces_the_previous_record(
        self, tmp_path, turn_home, monkeypatch, r4t_home
    ):
        def turn(rig, prompt, *, launch, variant, instruments, turn_context):
            return 126, "refused", 0.0, False
        metas, logs, call = self.harness(tmp_path, turn_home, monkeypatch, turn)
        assert call()[0] == 126
        recorded = [m["last_denials"] for m in metas if "last_denials" in m]
        assert recorded[0]["observed"] is None and recorded[0]["reason"] == "not-started"
        assert "r4t: denied: unknown (not-started)" in logs


# --- round 2: proof, descriptor, correlation, overflow --------------------------


class TestReadsTheDescriptor:
    """Everything is checked on the file descriptor that was read."""

    def bound(self, home, stub_bin, payload, **kw):
        session = str(uuid.uuid4())
        probe = DenialProbe(session=session, minted=kw.get("minted", True), lock_held=kw.get("lock", False))
        before = kw.get("before")
        path = events_file(home, session)
        if before is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(before)
        bind(probe, child_env(home))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            handle.write(for_session(payload, session))
        return probe, path

    def test_a_path_replaced_after_the_open_is_file_replaced_and_keeps_the_count(
        self, home, stub_bin, monkeypatch
    ):
        probe, path = self.bound(home, stub_bin, start("call1") + denied_exec("call1") + SHUTDOWN)
        real_open = os.open
        def open_then_replace(target, *args, **kwargs):
            fd = real_open(target, *args, **kwargs)
            if Path(target) == path:
                other = path.with_name("other.jsonl")
                other.write_bytes(SHUTDOWN)
                os.replace(other, path)
            return fd
        monkeypatch.setattr(os, "open", open_then_replace)
        summary = probe.collect()
        assert summary["complete"] is False and summary["reason"] == "file-replaced"
        assert summary["observed"] == 1  # the descriptor's file is what was read

    def test_a_path_replaced_before_the_open_is_not_the_pinned_file(self, home, stub_bin, monkeypatch):
        probe, path = self.bound(home, stub_bin, SHUTDOWN, before=head(), minted=False, lock=True)
        other = path.with_name("other.jsonl")
        other.write_bytes(SHUTDOWN)
        os.replace(other, path)
        assert probe.collect()["reason"] in ("file-replaced", "shared-pin")
        assert probe.collect()["complete"] is False

    def test_a_short_read_is_not_complete(self, home, stub_bin, monkeypatch):
        probe, path = self.bound(home, stub_bin, start("c") + denied_exec("c") + SHUTDOWN)
        real_read = os.read
        monkeypatch.setattr(os, "read", lambda fd, n: real_read(fd, n)[:10] if n > 10 else real_read(fd, n))
        # a reader that stops early (here: 10 bytes then EOF)
        calls = {"n": 0}
        def one_short(fd, n):
            calls["n"] += 1
            return real_read(fd, 10) if calls["n"] == 1 else b""
        monkeypatch.setattr(os, "read", one_short)
        summary = probe.collect()
        assert summary["complete"] is False and summary["reason"] == "truncated"

    def grow_during_read(self, monkeypatch, path, extra):
        """The first read returns what the budget allows; before returning,
        the writer appends to the same inode."""
        real_read = os.read
        done = {"n": 0}
        def read(fd, n):
            piece = real_read(fd, n)
            if not done["n"]:
                done["n"] = 1
                with path.open("ab") as handle:
                    handle.write(extra)
            return piece
        monkeypatch.setattr(os, "read", read)

    LATE = start("call_late") + denied_exec("call_late")

    def test_growth_from_zero_is_at_least_zero_shown_as_unknown(self, home, stub_bin, monkeypatch):
        probe, path = self.bound(home, stub_bin, SHUTDOWN)
        self.grow_during_read(monkeypatch, path, self.LATE)
        summary = probe.collect()
        assert summary["observed"] == 0 and summary["complete"] is False
        assert summary["reason"] == "file-grew"
        assert format_denials(summary) == "denied: unknown (incomplete: file-grew)"

    def test_growth_with_a_denial_already_read_is_at_least_n(self, home, stub_bin, monkeypatch):
        first = start("call_1") + denied_exec("call_1") + SHUTDOWN
        probe, path = self.bound(home, stub_bin, first)
        self.grow_during_read(monkeypatch, path, self.LATE)
        summary = probe.collect()
        assert summary["observed"] == 1 and summary["complete"] is False
        assert summary["reason"] == "file-grew"
        assert format_denials(summary) == "denied: at least 1 (incomplete: file-grew)"

    def test_growth_is_not_hidden_by_shared_pin(self, home, stub_bin, monkeypatch):
        # A resumed pin is also `shared-pin`; the growth is the more specific
        # reason and must be the one reported.
        probe, path = self.bound(home, stub_bin, SHUTDOWN, before=head(), minted=False, lock=True)
        self.grow_during_read(monkeypatch, path, self.LATE)
        summary = probe.collect()
        assert summary["complete"] is False and summary["reason"] == "file-grew"
        assert copilot.REASONS.index("file-grew") < copilot.REASONS.index("shared-pin")

    def test_the_gate_knows_file_grew(self, home, stub_bin):
        summary = copilot._empty_summary("s", "file-grew", 0)
        assert copilot._seal(summary)["reason"] == "file-grew"
        assert "file-grew" in copilot.PROOF

    def test_the_same_bytes_in_a_stable_file_are_complete_and_exact(self, home, stub_bin):
        full = start("call_1") + denied_exec("call_1") + SHUTDOWN + self.LATE
        probe, _ = self.bound(home, stub_bin, full)
        summary = probe.collect()
        assert summary["complete"] is True and summary["observed"] == 2

    def test_a_stable_file_read_twice_does_not_grow(self, home, stub_bin):
        probe, _ = self.bound(home, stub_bin, start("call_1") + denied_exec("call_1") + SHUTDOWN)
        assert probe.collect()["complete"] is True

    def test_a_symlink_is_not_followed(self, home, stub_bin, tmp_path):
        session = str(uuid.uuid4())
        probe = DenialProbe(session=session, minted=True)
        bind(probe, child_env(home))
        target = tmp_path / "elsewhere.jsonl"
        target.write_bytes(SHUTDOWN)
        path = events_file(home, session)
        path.parent.mkdir(parents=True)
        path.symlink_to(target)
        summary = probe.collect()
        assert summary["complete"] is False and summary["reason"] == "reader-error"

    FIFO_CHILD = """
import os, sys, tempfile, uuid
sys.path[:0] = {roots!r}
from engines import copilot
from rig import merge_env, resolve_launch
with tempfile.TemporaryDirectory() as td:
    session = str(uuid.uuid4())
    probe = copilot.DenialProbe(session=session, minted=True)
    env = merge_env({{"HOME": td}}, {{"PATH": os.environ.get("PATH", "")}})
    probe.bind(resolve_launch("copilot", env, cwd=td))
    path = os.path.join(td, ".copilot", "session-state", session, "events.jsonl")
    os.makedirs(os.path.dirname(path))
    os.mkfifo(path)
    print(probe.collect()["reason"])
"""

    def run_fifo_child(self, tmp_path, patch_source=None):
        import subprocess
        lib = Path(dispatch.__file__).resolve().parents[2] / "lib"
        r4t = Path(dispatch.__file__).resolve().parent
        code = self.FIFO_CHILD.format(roots=[str(lib), str(r4t)])
        if patch_source:
            code = patch_source + code
        try:
            done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True,
                                  text=True, timeout=20, cwd=tmp_path)
        except subprocess.TimeoutExpired:
            return "HUNG"
        return done.stdout.strip() or done.stderr[-200:]

    def test_a_fifo_in_place_of_the_file_never_blocks(self, tmp_path):
        # A bounded child: a mutation that makes the open blocking times out
        # here instead of hanging the suite.
        assert self.run_fifo_child(tmp_path) == "reader-error"

    def test_the_fifo_control_detects_a_blocking_open(self, tmp_path):
        patch = (
            "import os\n_o = os.open\n"
            "os.open = lambda p, f, *a, **k: _o(p, f & ~getattr(os, 'O_NONBLOCK', 0), *a, **k)\n"
        )
        assert self.run_fifo_child(tmp_path, patch) == "HUNG"


class TestStrictShapes:
    """Every recognized record is validated before any fast path."""

    @pytest.mark.parametrize("record", [
        event("session.start", None),
        event("session.start", {}),
        event("session.start", {"sessionId": 5}),
        event("tool.execution_complete", {"success": True}),
        event("tool.execution_complete", {"toolCallId": "bad id!", "success": True}),
        event("tool.execution_complete", {"toolCallId": "call_1", "success": "yes"}),
        event("tool.execution_complete", {"toolCallId": "call_1", "success": False}),
        event("tool.execution_start", {"toolCallId": "call_1"}),
        event("tool.execution_start", None),
        event("totally.unknown.kind", {}),
    ])
    def test_a_malformed_known_record_with_a_valid_shutdown_is_not_complete(self, record, home, stub_bin):
        summary, _ = run_probe(home, stub_bin, after=record + SHUTDOWN)
        assert summary["complete"] is False, record
        assert summary["reason"] == "unknown-shape"

    def test_an_explicitly_ignorable_kind_is_fine(self, home, stub_bin):
        summary, _ = run_probe(home, stub_bin, after=event("session.usage_record", {"x": 1}) + SHUTDOWN)
        assert summary["complete"] is True and summary["observed"] == 0

    def test_a_tool_call_that_never_finished_is_not_proof_of_nothing(self, home, stub_bin):
        summary, _ = run_probe(home, stub_bin, after=start("call_1") + SHUTDOWN)
        assert summary["complete"] is False

    def test_a_second_start_or_shutdown_is_not_complete(self, home, stub_bin):
        for extra in (event("session.start", {"sessionId": FIXTURE_SESSION.decode()}), SHUTDOWN):
            body = for_session(fixture("allowed-clean"), "x")  # placeholder, replaced by run_probe
            summary, _ = run_probe(home, stub_bin, after=fixture("allowed-clean") + extra)
            assert summary["complete"] is False

    def test_a_successful_outcome_for_a_call_nobody_started(self, home, stub_bin):
        raw = event("tool.execution_complete", {"toolCallId": "call_9", "success": True}) + SHUTDOWN
        summary, _ = run_probe(home, stub_bin, after=raw)
        assert summary["complete"] is False


class TestContradictoryCorrelation:
    REQUEST = event("permission.requested", {"requestId": "r1", "ownerSessionId": OWNER, "permissionRequest": {
        "kind": "shell", "toolCallId": "callA"}})

    def completed(self, call, verdict="denied-by-rules", request="r1"):
        return event("permission.completed", {"requestId": request, "toolCallId": call,
                                              "result": {"kind": verdict}})

    def test_the_review_case_is_one_not_two_and_not_complete(self, home, stub_bin):
        raw = (start("callA", "callB") + self.REQUEST + self.completed("callB")
               + denied_exec("callA") + denied_exec("callB") * 0 + SHUTDOWN)
        summary, _ = run_probe(home, stub_bin, after=raw)
        assert summary["observed"] == 1 and summary["complete"] is False
        assert summary["reason"] == "unknown-shape"

    def test_the_exact_review_shape_with_one_start(self, home, stub_bin):
        raw = start("callA") + self.REQUEST + self.completed("callB") + denied_exec("callA") + SHUTDOWN
        summary, _ = run_probe(home, stub_bin, after=raw)
        assert summary["observed"] == 1 and summary["complete"] is False

    def test_repeated_completions_that_name_different_calls(self, home, stub_bin):
        raw = (start("callA", "callB") + self.REQUEST + self.completed("callA") + self.completed("callB")
               + denied_exec("callA") + denied_exec("callB") + SHUTDOWN)
        summary, _ = run_probe(home, stub_bin, after=raw)
        assert summary["complete"] is False and 1 <= summary["observed"] <= 2

    def test_a_request_with_no_call_and_completions_naming_two(self, home, stub_bin):
        bare = event("permission.requested", {"requestId": "r1", "ownerSessionId": OWNER,
                                              "permissionRequest": {"kind": "shell"}})
        raw = (start("callA", "callB") + bare + self.completed("callA") + self.completed("callB")
               + denied_exec("callA") + denied_exec("callB") + SHUTDOWN)
        summary, _ = run_probe(home, stub_bin, after=raw)
        assert summary["complete"] is False and 1 <= summary["observed"] <= 2

    def test_completions_that_disagree_about_the_verdict(self, home, stub_bin):
        raw = (start("callA") + self.REQUEST + self.completed("callA") + self.completed("callA", "approved")
               + denied_exec("callA") + SHUTDOWN)
        summary, _ = run_probe(home, stub_bin, after=raw)
        assert summary["complete"] is False and summary["observed"] >= 1

    def test_a_denied_and_a_successful_outcome_for_one_call(self, home, stub_bin):
        raw = (start("call_1") + denied_exec("call_1")
               + event("tool.execution_complete", {"toolCallId": "call_1", "success": True}) + SHUTDOWN)
        summary, _ = run_probe(home, stub_bin, after=raw)
        assert summary["complete"] is False and summary["observed"] == 1

    def test_a_call_with_no_start_is_not_a_distinct_proven_operation(self, home, stub_bin):
        raw = start("call_a") + denied_exec("call_a") + denied_exec("call_ghost") + SHUTDOWN
        summary, _ = run_probe(home, stub_bin, after=raw)
        assert summary["observed"] == 1 and summary["complete"] is False

    def test_two_consistent_operations_stay_exact_and_complete(self, home, stub_bin):
        second = event("permission.requested", {"requestId": "r2", "ownerSessionId": OWNER, "permissionRequest": {
            "kind": "shell", "toolCallId": "callB"}})
        raw = (start("callA", "callB") + self.REQUEST + self.completed("callA") + second
               + self.completed("callB", request="r2") + denied_exec("callA") + denied_exec("callB") + SHUTDOWN)
        summary, _ = run_probe(home, stub_bin, after=raw)
        assert summary["observed"] == 2 and summary["complete"] is True


class TestOverflow:
    def many(self, n):
        calls = [f"call{i}" for i in range(n)]
        return start(*calls) + b"".join(denied_exec(c) for c in calls) + SHUTDOWN

    def test_the_cap_itself_is_exact_and_complete(self, home, stub_bin):
        summary, _ = run_probe(home, stub_bin, after=self.many(copilot.MAX_COUNT))
        assert summary["observed"] == copilot.MAX_COUNT and summary["complete"] is True

    @pytest.mark.parametrize("n", [1001, 1500])
    def test_past_the_cap_keeps_a_bounded_lower_bound(self, n, home, stub_bin):
        summary, _ = run_probe(home, stub_bin, after=self.many(n))
        assert summary["observed"] == copilot.MAX_COUNT
        assert summary["complete"] is False and summary["reason"] == "oversize"
        assert sum(summary["by_tool"].values()) == copilot.MAX_COUNT
        assert sum(summary["by_class"].values()) == copilot.MAX_COUNT
        assert format_denials(summary) == f"denied: at least {copilot.MAX_COUNT} (incomplete: oversize)"

    def test_the_cap_holds_over_mixed_distributions(self, home, stub_bin):
        calls = [f"call{i}" for i in range(1100)]
        raw = start(*calls)
        for i, c in enumerate(calls):
            if i % 3 == 0:
                raw += denied_exec(c, message=RULE_MESSAGE + "`shell(x)`")
            elif i % 3 == 1:
                raw += denied_exec(c)
            else:
                raw += (event("permission.requested", {"requestId": f"r{i}", "ownerSessionId": OWNER, "permissionRequest": {
                    "kind": "read", "toolCallId": c}})
                    + event("permission.completed", {"requestId": f"r{i}", "toolCallId": c,
                                                     "result": {"kind": f"denied-k{i % 30}"}})
                    + denied_exec(c))
        summary, _ = run_probe(home, stub_bin, after=raw + SHUTDOWN)
        assert summary["observed"] == copilot.MAX_COUNT and summary["reason"] == "oversize"
        assert sum(summary["by_class"].values()) == sum(summary["by_tool"].values()) == copilot.MAX_COUNT
        assert len(summary["by_class"]) <= copilot.MAX_CLASSES + 1

    def test_the_gate_checks_that_breakdowns_fit_the_count(self, home, stub_bin):
        summary, _ = run_probe(home, stub_bin, after=fixture("fallback-both-families"))
        broken = {**summary, "by_tool": {"read": 5}, "by_class": {"x": 5}}
        assert copilot._seal(broken)["reason"] == "reader-error"


class TestProofChecklist:
    def test_a_fact_that_is_missing_is_not_proven(self):
        full = {name: True for name in copilot.PROOF}
        assert copilot._unproven(full) == []
        for name in copilot.PROOF:
            assert copilot._unproven({k: v for k, v in full.items() if k != name}) == [name]
            assert copilot._unproven({**full, name: False}) == [name]
            assert copilot._unproven({**full, name: None}) == [name]
        assert copilot._unproven({}) == sorted(copilot.PROOF, key=copilot.REASONS.index)

    def test_every_proof_name_is_a_reportable_reason(self):
        assert set(copilot.PROOF) <= set(copilot.REASONS)


# --- the mutation fuzz ----------------------------------------------------------
#
# Oracle independence. The expected distinct-operation count is TRUTH below: it
# comes from how each fixture was produced (one tool call, denied or allowed),
# not from any id the code under test reconstructs. "Harmless" is a static table
# of fields the reader is specified never to read. The denial-line predicate is
# written here, from the raw JSON, without the reader.

import copy
import random

# name -> operations in the capture (provenance in the module docstring)
TRUTH = {
    "allowed-clean": 0,
    "deny-rule-tool-only": 1,
    "fallback-both-families": 1,
    "fallback-permission-only": 1,  # inferred: no failed tool record, so never complete
}
COMPLETE_BASE = ("allowed-clean", "deny-rule-tool-only", "fallback-both-families")

# Fields no coverage decision uses, by record kind (path under the record).
INERT = {
    "tool.execution_start": [("data", "arguments"), ("data", "turnId"), ("data", "toolTitle"),
                             ("data", "shellToolInfo"), ("data", "toolName")],
    "tool.execution_complete": [("data", "turnId"), ("data", "rte"), ("data", "result"),
                                ("data", "toolTelemetry"), ("data", "error", "message")],
    "permission.requested": [("data", "promptRequest"), ("data", "agentMode"), ("data", "permissionMode"),
                             ("data", "permissionRequest", "intention"), ("data", "permissionRequest", "path"),
                             ("data", "permissionRequest", "kind")],
    "permission.completed": [("data", "decisionSource")],
    "session.start": [("data", "copilotVersion")],
}
EVERYWHERE_INERT = [("timestamp",), ("parentId",), ("id",)]
RESTATES = {"tool.execution_start", "tool.execution_complete", "permission.completed"}
VALUES = [None, 123, "zz-str", [], {}]


def fixture_lines(name):
    return [json.loads(line) for line in fixture(name).splitlines() if line.strip()]


def to_bytes(lines):
    return b"".join((json.dumps(line) if not isinstance(line, bytes) else line.decode()).encode() + b"\n"
                    for line in lines)


def key_paths(obj, prefix=()):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield prefix + (key,)
            yield from key_paths(value, prefix + (key,))


def set_path(obj, path, value, drop=False):
    for key in path[:-1]:
        obj = obj[key]
    if drop:
        del obj[path[-1]]
    else:
        obj[path[-1]] = value


def line_kind(line):
    return line.get("type") if isinstance(line, dict) else None


def ignorable(kind):
    return isinstance(kind, str) and kind in copilot.IGNORABLE_KINDS


# Optional, and the completion carries its own copy: absence changes nothing,
# any other value is a claim that can contradict it.
OPTIONAL = {("permission.requested", ("data", "permissionRequest", "toolCallId"))}


def is_inert(kind, path, absent=False):
    if not isinstance(kind, str):
        return False
    if absent and (kind, path) in OPTIONAL:
        return True
    for prefix in EVERYWHERE_INERT + INERT.get(kind, []):
        if path[:len(prefix)] == prefix:
            return True
    return ignorable(kind) and path[:1] == ("data",)


def denial_lines(lines):
    """The independent predicate: a line that states a denial on its own fields."""
    n = 0
    for line in lines:
        if not isinstance(line, dict) or not isinstance(line.get("data"), dict):
            continue
        data = line["data"]
        if line.get("type") == "tool.execution_complete":
            error = data.get("error")
            if data.get("success") is False and isinstance(error, dict) and \
                    isinstance(error.get("code"), str) and error["code"].startswith("denied"):
                n += 1
        elif line.get("type") == "permission.completed":
            result = data.get("result")
            if isinstance(result, dict) and isinstance(result.get("kind"), str) and \
                    result["kind"].startswith("denied"):
                n += 1
    return n


def single_mutations(name, base=None):
    """(lines, harmless, label, touched): every field operation, line deletion and
    duplication, id swaps, line truncation. Exhaustive, no randomness. `base`
    lets a second mutation be applied on top of a first."""
    base = fixture_lines(name) if base is None else base
    for i, line in enumerate(base):
        if not isinstance(line, dict):
            continue
        kind = line_kind(line)
        for path in key_paths(line):
            for value in VALUES:
                mutated = copy.deepcopy(base)
                set_path(mutated[i], path, value)
                yield mutated, is_inert(kind, path, value is None), f"{name}[{i}] {kind} set {path}={value!r}", (i,)
            mutated = copy.deepcopy(base)
            set_path(mutated[i], path, None, drop=True)
            yield mutated, is_inert(kind, path, True), f"{name}[{i}] {kind} drop {path}", (i,)
        mutated = copy.deepcopy(base)
        del mutated[i]
        yield mutated, ignorable(kind) or kind == "session.start", f"{name} delete [{i}] {kind}", None
        mutated = copy.deepcopy(base)
        mutated.insert(i, copy.deepcopy(line))
        yield mutated, ignorable(kind) or (isinstance(kind, str) and kind in RESTATES), f"{name} dup [{i}] {kind}", None
        cut = json.dumps(line).encode()
        mutated = copy.deepcopy(base)
        mutated[i] = cut[: len(cut) // 2]
        yield mutated, False, f"{name} truncate line [{i}] {kind}", (i,)
    holders = [(i, p) for i, line in enumerate(base) if isinstance(line, dict) for p in key_paths(line)
               if p[-1] in ("toolCallId", "requestId")]
    for a in range(len(holders)):
        for b in range(a + 1, len(holders)):
            (i, pi), (j, pj) = holders[a], holders[b]
            mutated = copy.deepcopy(base)
            x = mutated[i]
            y = mutated[j]
            for obj, path in ((x, pi), (y, pj)):
                pass
            vi = base[i]
            for key in pi[:-1]:
                vi = vi[key]
            vi = vi[pi[-1]]
            vj = base[j]
            for key in pj[:-1]:
                vj = vj[key]
            vj = vj[pj[-1]]
            set_path(mutated[i], pi, vj)
            set_path(mutated[j], pj, vi)
            yield mutated, vi == vj, f"{name} swap ids [{i}]{pi} <-> [{j}]{pj}", (i, j)


def end_truncations(name):
    raw = fixture(name)
    for cut in (1, 3, 40, 200, len(raw.splitlines(True)[-1]), len(b"".join(raw.splitlines(True)[-2:]))):
        yield raw[:-cut], False, f"{name} cut last {cut} bytes"


class TestMutationFuzz:
    def check(self, home, stub_bin, name, raw, harmless, label, lines=None):
        summary, _ = run_probe(home, stub_bin, after=raw)
        truth = TRUTH[name]
        observed = summary["observed"]
        if summary["complete"]:
            assert harmless, f"complete survived a harmful mutation: {label}"
            assert observed == truth, f"complete with a wrong count: {label} -> {observed}"
        if observed is not None:
            assert observed <= truth, f"observed {observed} exceeds the true count {truth}: {label}"
        if lines is not None and denial_lines(lines) > 0:
            assert observed is not None and observed >= 1, f"a surviving denial line read as 0: {label}"
            assert observed <= denial_lines(lines), label
        return summary

    def test_the_unmutated_fixtures(self, home, stub_bin):
        for name in COMPLETE_BASE:
            summary, _ = run_probe(home, stub_bin, after=fixture(name))
            assert summary["complete"] is True and summary["observed"] == TRUTH[name], name
        summary, _ = run_probe(home, stub_bin, after=fixture("fallback-permission-only"))
        assert summary["complete"] is False and summary["observed"] == 1

    def test_every_single_mutation(self, home, stub_bin):
        seen = survivors = 0
        for name in TRUTH:
            for lines, harmless, label, _ in single_mutations(name):
                seen += 1
                raw = to_bytes(lines)
                summary = self.check(home, stub_bin, name, raw, harmless, label, lines)
                survivors += bool(summary["complete"])
        for name in TRUTH:
            for raw, harmless, label in end_truncations(name):
                seen += 1
                self.check(home, stub_bin, name, raw, harmless, label)
        # the generator is not vacuous, and harmless mutations do survive
        assert seen > 1500
        assert survivors > 100

    def test_seeded_pairs_of_mutations(self, home, stub_bin):
        # Two mutations on different lines. The first keeps the line positions
        # (a field change or an id swap), so the second cannot erase it.
        rng = random.Random(20261102)
        for name in COMPLETE_BASE:
            pool = [m for m in single_mutations(name) if m[3] is not None]
            for _ in range(100):
                first_lines, h1, t1, touched = rng.choice(pool)
                second = [m for m in single_mutations(name, copy.deepcopy(first_lines))
                          if m[3] is None or not set(m[3]) & set(touched)]
                lines, h2, t2, _ = rng.choice(second)
                self.check(home, stub_bin, name, to_bytes(lines), h1 and h2, f"{t1} + {t2}", lines)

    def test_harmless_controls_stay_complete_with_the_exact_count(self, home, stub_bin):
        for name in COMPLETE_BASE:
            truth = TRUTH[name]
            lines = fixture_lines(name)
            variants = {
                "reordered fields": to_bytes([dict(reversed(list(l.items()))) for l in lines]),
                "extra whitespace": b"".join(
                    b"  " + json.dumps(l, indent=1).replace("\n", " ").encode() + b"   \n\n" for l in lines),
                "crlf": b"".join(json.dumps(l).encode() + b"\r\n" for l in lines),
                "an ignorable unknown-to-us event": to_bytes(
                    lines[:-1] + [{"type": "session.usage_record", "data": {"x": 1}}] + lines[-1:]),
                "inert fields changed": to_bytes([
                    {**l, "timestamp": "2030-01-01T00:00:00Z", "parentId": None} for l in lines]),
                "a duplicated ignorable line": to_bytes(lines[:2] + lines[1:2] + lines[2:]),
            }
            for label, raw in variants.items():
                summary, _ = run_probe(home, stub_bin, after=raw)
                assert summary["complete"] is True and summary["observed"] == truth, (name, label, summary)

    def test_the_generator_covers_every_mutation_kind(self):
        labels = [t for name in TRUTH for _, _, t, _ in single_mutations(name)]
        for needle in (" set ", " drop ", " delete ", " dup ", " truncate line ", " swap ids "):
            assert any(needle in t for t in labels), needle


# --- every way out of run_harness records the turn ----------------------------
#
# A refused or failed roster turn must replace the previous turn's
# `last_denials` with `unknown (not-started)` instead of leaving it to read as
# this turn's. One try/finally in `run_harness` covers every return and every
# exception of the body; these tests pin each case, and a source test fails
# when the structure is broken.

import ast
import re

OLD_RECORD = {"observed": 0, "complete": True, "reason": None}


class TestNotStartedOnEveryExit:
    def setup(self, tmp_path, turn_home, monkeypatch, *, rig_env=None, env_extra=None, turn=None):
        import rig as rig_module
        monkeypatch.setattr(copilot, "supports_usage_file", lambda *a: True)
        metas: list[dict] = []
        logs: list[str] = []
        monkeypatch.setattr(dispatch.state, "update_meta", lambda n, m, **kw: metas.append(kw))
        monkeypatch.setattr(dispatch.state, "append_log", lambda n, line: logs.append(line))
        if turn is not None:
            monkeypatch.setattr(dispatch, "_run_harness_turn", turn)
        work = tmp_path / "work"
        work.mkdir(exist_ok=True)
        spec = {"preset": "copilot", "invoke": rig_module.build_preset_invoke("copilot"), "mcp": False}
        selected = rig_module.rig_from_spec("t", spec)
        if rig_env:
            import dataclasses
            selected = dataclasses.replace(selected, env=dict(rig_env))
        env = {**os.environ, "HOME": str(turn_home), "R4T_NODE": "n", "R4T_MEMBER": "m",
               **(env_extra or {})}
        return metas, logs, lambda: dispatch.run_harness(selected, "hi", work, env=env)

    def assert_replaced(self, metas, logs, count=1):
        recorded = [m["last_denials"] for m in metas if "last_denials" in m]
        assert len(recorded) == count, recorded
        assert recorded[-1]["observed"] is None and recorded[-1]["reason"] == "not-started"
        assert logs.count("r4t: denied: unknown (not-started)") == count
        assert recorded[-1] != OLD_RECORD

    @pytest.mark.parametrize("value", ["rel/dir", "~/copilot"])
    def test_a_bad_state_home_refusal_records_not_started(
        self, value, tmp_path, turn_home, monkeypatch, r4t_home
    ):
        metas, logs, call = self.setup(tmp_path, turn_home, monkeypatch,
                                       env_extra={"COPILOT_HOME": value})
        result = call()
        assert result[0] == 126 and "COPILOT_HOME" in result[1]
        self.assert_replaced(metas, logs)

    def test_a_bad_home_set_by_the_rig_records_not_started(
        self, tmp_path, turn_home, monkeypatch, r4t_home
    ):
        metas, logs, call = self.setup(tmp_path, turn_home, monkeypatch,
                                       rig_env={"COPILOT_HOME": "rel/dir"})
        assert call()[0] == 126
        self.assert_replaced(metas, logs)

    def test_an_exception_while_the_launch_is_built_records_and_re_raises(
        self, tmp_path, turn_home, monkeypatch, r4t_home
    ):
        metas, logs, call = self.setup(tmp_path, turn_home, monkeypatch)
        def boom(*a, **k):
            raise RuntimeError("original")
        monkeypatch.setattr(dispatch, "resolve_launch", boom)
        with pytest.raises(RuntimeError, match="original"):
            call()
        self.assert_replaced(metas, logs)

    def test_the_instruments_failing_to_arm_records_and_re_raises(
        self, tmp_path, turn_home, monkeypatch, r4t_home
    ):
        metas, logs, call = self.setup(tmp_path, turn_home, monkeypatch)
        class Boom:
            def __enter__(self):
                raise RuntimeError("arming failed")
            def __exit__(self, *a):
                return False
        monkeypatch.setattr(copilot, "turn_instruments", lambda *a, **k: Boom())
        with pytest.raises(RuntimeError, match="arming failed"):
            call()
        self.assert_replaced(metas, logs)

    def test_an_interrupt_before_the_launch_records_and_re_raises(
        self, tmp_path, turn_home, monkeypatch, r4t_home
    ):
        metas, logs, call = self.setup(tmp_path, turn_home, monkeypatch)
        def stop(*a, **k):
            raise KeyboardInterrupt
        monkeypatch.setattr(dispatch, "resolve_launch", stop)
        with pytest.raises(KeyboardInterrupt):
            call()
        self.assert_replaced(metas, logs)

    # Refusals inside the turn body are measured by the body's own finally and
    # the outer one adds nothing: exactly one record each.
    def test_an_access_refusal_records_once(self, tmp_path, turn_home, monkeypatch, r4t_home):
        metas, logs, call = self.setup(tmp_path, turn_home, monkeypatch)
        monkeypatch.setattr(dispatch, "_access_for_turn",
                            lambda *a, **k: (None, None, "refused: access"))
        assert call()[0] == 126
        self.assert_replaced(metas, logs)

    def test_a_container_wrapper_refusal_records_once(self, tmp_path, turn_home, monkeypatch, r4t_home):
        metas, logs, call = self.setup(tmp_path, turn_home, monkeypatch,
                                       env_extra={"R4T_CONTAINER": "img"})
        def refuse(*a, **k):
            raise dispatch.isolate.IsolationError("no trusted docker")
        monkeypatch.setattr(dispatch.isolate, "build_container_argv", refuse)
        result = call()
        assert result[0] == 126 and "no trusted docker" in result[1]
        self.assert_replaced(metas, logs)

    def test_a_run_as_wrapper_refusal_records_once(self, tmp_path, turn_home, monkeypatch, r4t_home):
        metas, logs, call = self.setup(tmp_path, turn_home, monkeypatch,
                                       env_extra={"R4T_RUN_AS": "somebody"})
        monkeypatch.setattr(dispatch.isolate, "probe_run_as", lambda *a, **k: None)
        monkeypatch.setattr(dispatch.isolate, "probe_readable_as", lambda *a, **k: None)
        def refuse(*a, **k):
            raise dispatch.isolate.IsolationError("no trusted sudo")
        monkeypatch.setattr(dispatch.isolate, "wrap_run_as", refuse)
        result = call()
        assert result[0] == 126 and "no trusted sudo" in result[1]
        self.assert_replaced(metas, logs)

    def test_a_turn_that_reached_its_measurement_is_recorded_once_not_twice(
        self, tmp_path, turn_home, monkeypatch, r4t_home
    ):
        def turn(rig, prompt, *, launch, variant, instruments, turn_context):
            instruments.denials.bind(launch)
            return 0, "", 0.0, False
        metas, logs, call = self.setup(tmp_path, turn_home, monkeypatch, turn=turn)
        assert call()[0] == 0
        recorded = [m["last_denials"] for m in metas if "last_denials" in m]
        assert [r["reason"] for r in recorded] == ["session-unknown"]
        assert "r4t: denied: unknown (not-started)" not in logs

    def test_a_rig_with_no_instruments_and_a_plain_engine_record_nothing(
        self, tmp_path, turn_home, monkeypatch, r4t_home
    ):
        import rig as rig_module
        metas: list[dict] = []
        monkeypatch.setattr(dispatch.state, "update_meta", lambda n, m, **kw: metas.append(kw))
        monkeypatch.setattr(dispatch.state, "append_log", lambda n, line: None)
        work = tmp_path / "work"
        work.mkdir()
        plain = rig_module.rig_from_spec("p", {"preset": "claude",
                                               "invoke": rig_module.build_preset_invoke("claude")})
        env = {**os.environ, "HOME": str(turn_home), "R4T_NODE": "n", "R4T_MEMBER": "m",
               "COPILOT_HOME": "rel/dir"}
        assert dispatch.run_harness(plain, "hi", work, env=env)[0] == 126
        assert not any("last_denials" in m for m in metas)

    def test_no_context_no_record(self, tmp_path, turn_home, monkeypatch, r4t_home):
        metas, logs, call = self.setup(tmp_path, turn_home, monkeypatch,
                                       env_extra={"COPILOT_HOME": "rel/dir", "R4T_NODE": "",
                                                  "R4T_MEMBER": ""})
        assert call()[0] == 126
        assert metas == [] and logs == []

    def test_a_failing_sink_changes_neither_the_result_nor_the_exception(
        self, tmp_path, turn_home, monkeypatch, r4t_home
    ):
        metas, logs, call = self.setup(tmp_path, turn_home, monkeypatch,
                                       env_extra={"COPILOT_HOME": "rel/dir"})
        monkeypatch.setattr(dispatch.state, "update_meta", _boom)
        monkeypatch.setattr(dispatch.state, "append_log", _boom)
        assert call()[0] == 126


class TestRunHarnessStructure:
    """A source test: the structure, not each return, is what is pinned."""

    def tree(self):
        path = Path(dispatch.__file__)
        return ast.parse(path.read_text(encoding="utf-8"))

    def function(self, name):
        for node in ast.walk(self.tree()):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        raise AssertionError(name)

    def test_run_harness_is_one_try_finally_that_records_a_turn_that_never_measured(self):
        function = self.function("run_harness")
        body = [n for n in function.body
                if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
        # the whole body is the measured list and a single try/finally
        assert [type(n).__name__ for n in body] == ["AnnAssign", "Try"] or \
            [type(n).__name__ for n in body] == ["Assign", "Try"]
        try_node = body[-1]
        assert try_node.finalbody and not try_node.handlers
        assert "_record_fallback" in ast.unparse(try_node.finalbody)
        # no return or raise outside the try
        outside = [n for n in body[:-1] if any(isinstance(s, (ast.Return, ast.Raise)) for s in ast.walk(n))]
        assert not outside

    def test_the_body_measures_inside_a_finally_and_says_so(self):
        function = self.function("_run_instrumented")
        text = ast.unparse(function)
        assert "measured.append(" in text
        finals = [n for n in ast.walk(function) if isinstance(n, ast.Try) and n.finalbody]
        assert any("_record_measurement" in ast.unparse(t.finalbody)
                   and "measured.append" in ast.unparse(t.finalbody) for t in finals)

    def test_nothing_else_calls_the_body_directly(self):
        callers = [n for n in ast.walk(self.tree())
                   if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_run_instrumented"]
        assert len(callers) == 1


REAL_SUPPORTS_USAGE_FILE = copilot.supports_usage_file  # before any fixture patches it


class TestEveryCopilotTurnLeavesOneRecord:
    """The rule: every copilot turn of a node and member writes exactly one
    `last_denials` record, so meta never shows an earlier turn's result. Real
    state, not a capture: a stale record is replaced on disk."""

    STALE = {"observed": 0, "complete": True, "reason": None, "by_tool": {}, "by_class": {},
             "event_ids": [], "rule_tools": [], "rules_omitted": 0, "session": None}

    def run(self, tmp_path, turn_home, monkeypatch, *, armed, turn=None, env_extra=None):
        import rig as rig_module
        from engines import copilot as copilot_module
        monkeypatch.setattr(copilot_module, "supports_usage_file", lambda *a: armed)
        if turn is not None:
            monkeypatch.setattr(dispatch, "_run_harness_turn", turn)
        dispatch.state.update_meta("n", "m", last_denials=self.STALE)
        logs: list[str] = []
        real_log = dispatch.state.append_log
        monkeypatch.setattr(dispatch.state, "append_log",
                            lambda node, line: (logs.append(line), real_log(node, line))[1])
        work = tmp_path / "work"
        work.mkdir(exist_ok=True)
        selected = rig_module.rig_from_spec(
            "t", {"preset": "copilot", "invoke": rig_module.build_preset_invoke("copilot"), "mcp": False})
        env = {**os.environ, "HOME": str(turn_home), "R4T_NODE": "n", "R4T_MEMBER": "m",
               **(env_extra or {})}
        result = dispatch.run_harness(selected, "hi", work, env=env)
        return result, dispatch.state.read_meta("n", "m")["last_denials"], logs

    def ran(self):
        def turn(rig, prompt, *, launch, variant, instruments, turn_context):
            return 0, "out", 0.0, False
        return turn

    def test_an_unarmed_turn_that_ran_writes_not_armed(self, tmp_path, turn_home, monkeypatch, r4t_home):
        result, record, logs = self.run(tmp_path, turn_home, monkeypatch, armed=False, turn=self.ran())
        assert result[0] == 0
        assert record["observed"] is None and record["reason"] == "not-armed"
        assert logs.count("r4t: denied: unknown (not-armed)") == 1

    def test_an_unarmed_turn_that_was_refused_writes_not_armed(self, tmp_path, turn_home, monkeypatch, r4t_home):
        monkeypatch.setattr(dispatch, "_access_for_turn", lambda *a, **k: (None, None, "refused: access"))
        result, record, logs = self.run(tmp_path, turn_home, monkeypatch, armed=False)
        assert result[0] == 126
        assert record["reason"] == "not-armed"
        assert logs.count("r4t: denied: unknown (not-armed)") == 1
        assert not any("not-started" in line for line in logs)

    def test_a_stale_record_from_the_turn_before_is_replaced(self, tmp_path, turn_home, monkeypatch, r4t_home):
        _, record, _ = self.run(tmp_path, turn_home, monkeypatch, armed=False, turn=self.ran())
        assert record != self.STALE

    def test_an_unresolvable_binary_is_not_armed(self, tmp_path, turn_home, monkeypatch, r4t_home):
        # No copilot on the child's PATH: the REAL usage probe finds nothing.
        monkeypatch.setattr(copilot, "supports_usage_file", REAL_SUPPORTS_USAGE_FILE)
        import rig as rig_module
        monkeypatch.setattr(dispatch.state, "append_log", lambda *a, **k: None)
        dispatch.state.update_meta("n", "m", last_denials=self.STALE)
        work = tmp_path / "work"
        work.mkdir(exist_ok=True)
        selected = rig_module.rig_from_spec(
            "t", {"preset": "copilot", "invoke": rig_module.build_preset_invoke("copilot"), "mcp": False})
        env = {"HOME": str(turn_home), "PATH": str(tmp_path / "nowhere"),
               "R4T_NODE": "n", "R4T_MEMBER": "m"}
        dispatch.run_harness(selected, "hi", work, env=env)
        assert dispatch.state.read_meta("n", "m")["last_denials"]["reason"] == "not-armed"

    def test_an_armed_turn_that_ran_still_writes_exactly_one_record(
        self, tmp_path, turn_home, monkeypatch, r4t_home
    ):
        def turn(rig, prompt, *, launch, variant, instruments, turn_context):
            instruments.denials.bind(launch)
            return 0, "", 0.0, False
        _, record, logs = self.run(tmp_path, turn_home, monkeypatch, armed=True, turn=turn)
        assert record["reason"] == "session-unknown"
        assert [l for l in logs if l.startswith("r4t: denied:")] == ["r4t: denied: unknown (session-unknown)"]

    def test_an_armed_turn_refused_before_its_instruments_is_not_started(
        self, tmp_path, turn_home, monkeypatch, r4t_home
    ):
        _, record, logs = self.run(tmp_path, turn_home, monkeypatch, armed=True,
                                   env_extra={"COPILOT_HOME": "rel/dir"})
        assert record["reason"] == "not-started"
        assert [l for l in logs if l.startswith("r4t: denied:")] == ["r4t: denied: unknown (not-started)"]

    def test_the_new_reason_is_in_the_enum_the_gate_and_the_ranking(self):
        assert "not-armed" in copilot.REASONS
        assert copilot._seal(copilot.unknown_summary("not-armed"))["reason"] == "not-armed"
        assert copilot.REASONS.index("not-armed") < copilot.REASONS.index("home-unknown")
        assert format_denials(copilot.unknown_summary("not-armed")) == "denied: unknown (not-armed)"


class TestWindowsEnvNames:
    """Issue 382 Part A made one canonical key per environment variable: on
    Windows a name is case-insensitive. The probe's home lookup must read the
    same variable the child gets. Windows is simulated by patching
    `sys.platform`; nothing here starts a process."""

    def bound(self, home, tmp_path, env_extra):
        account = tmp_path / "account"
        session = str(uuid.uuid4())
        events = account / "session-state" / session / "events.jsonl"
        events.parent.mkdir(parents=True)
        probe = DenialProbe(session=session, minted=True)
        probe.bind(launch_for("copilot", child_env(home, **env_extra(account))))
        events.write_bytes(for_session(
            start("call_1") + denied_exec("call_1") + SHUTDOWN, session))
        return probe

    def test_a_lowercase_alias_binds_the_accounts_events_home_on_windows(
        self, home, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(sys, "platform", "win32")
        probe = self.bound(home, tmp_path, lambda account: {"copilot_home": str(account)})
        summary = probe.collect()
        # the account's events file was found and read (the build may be
        # unverified in this simulation; the home is what is under test)
        assert summary["observed"] == 1
        assert summary["reason"] not in ("home-unknown", "session-unknown")

    def test_the_same_alias_is_a_different_variable_on_posix(self, home, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        probe = self.bound(home, tmp_path, lambda account: {"copilot_home": str(account)})
        # `copilot_home` is not COPILOT_HOME here: the default home has no session
        assert probe.collect()["reason"] == "session-unknown"

    def test_the_exact_name_binds_the_same_home_everywhere(self, home, tmp_path, monkeypatch):
        for platform in ("win32", "linux"):
            monkeypatch.setattr(sys, "platform", platform)
            probe = self.bound(home, tmp_path, lambda account: {"COPILOT_HOME": str(account)})
            assert probe.collect()["observed"] == 1

    def test_the_probe_reads_a_launch_whose_env_is_not_canonical(self, home, tmp_path, monkeypatch):
        # A launch made from a caller's dict: the probe canonicalizes it itself.
        monkeypatch.setattr(sys, "platform", "win32")
        account = tmp_path / "acct2"
        session = str(uuid.uuid4())
        probe = DenialProbe(session=session, minted=True)
        launch = launch_for("copilot", {"HOME": str(home), "PATH": os.environ["PATH"],
                                        "Copilot_Home": str(account)})
        probe.bind(launch)
        assert probe._path == account / "session-state" / session / "events.jsonl"

    def test_an_alias_home_with_no_home_variable_still_binds_on_windows(self, tmp_path, monkeypatch):
        # Only the alias names the home: reading the raw dict would call it unknown.
        monkeypatch.setattr(sys, "platform", "win32")
        account = tmp_path / "acct3"
        probe = DenialProbe(session=str(uuid.uuid4()), minted=True)
        probe.bind(launch_for("copilot", {"PATH": os.environ["PATH"], "copilot_home": str(account)}))
        assert probe._early is None and probe._path.is_relative_to(account)

    def test_a_launch_carrying_a_raw_alias_is_canonicalized_by_the_probe(self, tmp_path, monkeypatch):
        # Defence in depth: `resolve_launch` canonicalizes, but a launch whose env
        # was swapped afterwards (as a test can) must not make the probe read a
        # different variable than the child would.
        import dataclasses
        monkeypatch.setattr(sys, "platform", "win32")
        account = tmp_path / "acct4"
        launch = dataclasses.replace(
            launch_for("copilot", {"PATH": os.environ["PATH"]}),
            env={"PATH": os.environ["PATH"], "copilot_home": str(account)},
        )
        probe = DenialProbe(session=str(uuid.uuid4()), minted=True)
        probe.bind(launch)
        assert probe._early is None and probe._path.is_relative_to(account)


# --- the turn's routing names are read in one place, from the canonical env ----


def names_for(style):
    """The three routing names written in an environment's case style."""
    names = {"upper": ("R4T_NODE", "R4T_MEMBER", "R4T_SESSION"),
             "lower": ("r4t_node", "r4t_member", "r4t_session"),
             "mixed": ("R4t_Node", "r4T_member", "R4t_SESSION")}
    return names[style]


class TestRosterContextIsCanonical:
    """Issue 382 Part B review: the roster telemetry read the raw caller env for
    the node, member and pin, which on Windows (names are case-insensitive)
    disagrees with the Launch. Windows is simulated by patching `sys.platform`."""

    def setup(self, tmp_path, turn_home, monkeypatch, style, *, armed, platform="win32",
              env_extra=None, turn=None, pin=None):
        import rig as rig_module
        monkeypatch.setattr(sys, "platform", platform)
        monkeypatch.setattr(copilot, "supports_usage_file", lambda *a: armed)
        writes: list[dict] = []
        logs: list[str] = []
        monkeypatch.setattr(dispatch.state, "update_meta",
                            lambda n, m, **kw: writes.append({"node": n, "member": m, **kw}))
        monkeypatch.setattr(dispatch.state, "append_log", lambda n, line: logs.append((n, line)))
        if turn is not None:
            monkeypatch.setattr(dispatch, "_run_harness_turn", turn)
        work = tmp_path / "work"
        work.mkdir(exist_ok=True)
        selected = rig_module.rig_from_spec(
            "t", {"preset": "copilot", "invoke": rig_module.build_preset_invoke("copilot"), "mcp": False})
        node, member, session = names_for(style)
        env = {"HOME": str(turn_home), "PATH": os.environ["PATH"], node: "n", member: "m",
               **(env_extra or {})}
        if pin:
            env[session] = pin
        return writes, logs, lambda: dispatch.run_harness(selected, "hi", work, env=env)

    def denial_writes(self, writes):
        return [w for w in writes if "last_denials" in w]

    def fake_turn(self, bind):
        def turn(rig, prompt, *, launch, variant, instruments, turn_context):
            if bind:
                instruments.denials.bind(launch)
            return 0, "", 0.0, False
        return turn

    @pytest.mark.parametrize("style", ["upper", "lower", "mixed"])
    def test_an_unarmed_turn_writes_one_record_in_any_case(
        self, style, tmp_path, turn_home, monkeypatch, r4t_home
    ):
        writes, logs, call = self.setup(tmp_path, turn_home, monkeypatch, style, armed=False,
                                        turn=self.fake_turn(False))
        assert call()[0] == 0
        records = self.denial_writes(writes)
        assert [r["last_denials"]["reason"] for r in records] == ["not-armed"]
        assert (records[0]["node"], records[0]["member"]) == ("n", "m")
        assert len([1 for _, line in logs if line.startswith("r4t: denied:")]) == 1

    @pytest.mark.parametrize("style", ["upper", "lower", "mixed"])
    def test_an_armed_turn_writes_one_record_in_any_case(
        self, style, tmp_path, turn_home, monkeypatch, r4t_home
    ):
        writes, logs, call = self.setup(tmp_path, turn_home, monkeypatch, style, armed=True,
                                        turn=self.fake_turn(True))
        assert call()[0] == 0
        records = self.denial_writes(writes)
        assert [r["last_denials"]["reason"] for r in records] == ["session-unknown"]
        assert (records[0]["node"], records[0]["member"]) == ("n", "m")

    @pytest.mark.parametrize("style", ["upper", "lower", "mixed"])
    def test_a_refusal_before_the_launch_writes_one_record_in_any_case(
        self, style, tmp_path, turn_home, monkeypatch, r4t_home
    ):
        writes, logs, call = self.setup(tmp_path, turn_home, monkeypatch, style, armed=True,
                                        env_extra={"COPILOT_HOME": "rel/dir"})
        assert call()[0] == 126
        records = self.denial_writes(writes)
        assert [r["last_denials"]["reason"] for r in records] == ["not-started"]
        assert (records[0]["node"], records[0]["member"]) == ("n", "m")

    @pytest.mark.parametrize("style", ["upper", "lower", "mixed"])
    def test_a_refusal_inside_the_turn_body_writes_one_record_in_any_case(
        self, style, tmp_path, turn_home, monkeypatch, r4t_home
    ):
        writes, logs, call = self.setup(tmp_path, turn_home, monkeypatch, style, armed=False)
        monkeypatch.setattr(dispatch, "_access_for_turn", lambda *a, **k: (None, None, "refused"))
        assert call()[0] == 126
        assert [r["last_denials"]["reason"] for r in self.denial_writes(writes)] == ["not-armed"]

    @pytest.mark.parametrize("style", ["upper", "lower", "mixed"])
    def test_the_probe_session_is_the_launchs_pin_in_any_case(
        self, style, tmp_path, turn_home, monkeypatch, r4t_home
    ):
        pin = str(uuid.uuid4())
        seen = {}
        def turn(rig, prompt, *, launch, variant, instruments, turn_context):
            seen["probe"] = instruments.denials.session
            seen["minted"] = instruments.denials.minted
            seen["pin"] = launch.env.get("R4T_SESSION")
            return 0, "", 0.0, False
        writes, logs, call = self.setup(tmp_path, turn_home, monkeypatch, style, armed=True,
                                        turn=turn, pin=pin)
        call()
        assert seen == {"probe": pin, "minted": False, "pin": pin}

    # POSIX: names are exact, a lowercase name is a different variable.
    def test_posix_lowercase_names_are_other_variables_and_nothing_is_written(
        self, tmp_path, turn_home, monkeypatch, r4t_home
    ):
        writes, logs, call = self.setup(tmp_path, turn_home, monkeypatch, "lower", armed=False,
                                        platform="linux", turn=self.fake_turn(False))
        assert call()[0] == 0
        assert self.denial_writes(writes) == [] and logs == []

    def test_posix_a_lowercase_pin_is_not_the_pin(self, tmp_path, turn_home, monkeypatch, r4t_home):
        pin = str(uuid.uuid4())
        seen = {}
        def turn(rig, prompt, *, launch, variant, instruments, turn_context):
            seen["probe"], seen["minted"] = instruments.denials.session, instruments.denials.minted
            return 0, "", 0.0, False
        writes, logs, call = self.setup(tmp_path, turn_home, monkeypatch, "lower", armed=True,
                                        platform="linux", turn=turn, pin=pin)
        call()
        assert seen["minted"] is True and seen["probe"] != pin

    @pytest.mark.parametrize("platform", ["linux", "win32"])
    def test_the_exact_names_behave_the_same_everywhere(
        self, platform, tmp_path, turn_home, monkeypatch, r4t_home
    ):
        writes, logs, call = self.setup(tmp_path, turn_home, monkeypatch, "upper", armed=False,
                                        platform=platform, turn=self.fake_turn(False))
        call()
        assert [r["last_denials"]["reason"] for r in self.denial_writes(writes)] == ["not-armed"]

    # The helper failing must change neither the result nor the exception.
    def raising_after_the_first_call(self, monkeypatch):
        real = dispatch.roster_context
        calls = {"n": 0}
        def flaky(env):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("helper down")
            return real(env)
        monkeypatch.setattr(dispatch, "roster_context", flaky)
        return calls

    @pytest.mark.parametrize("armed", [False, True])
    def test_a_helper_that_raises_in_the_record_step_keeps_the_return_value(
        self, armed, tmp_path, turn_home, monkeypatch, r4t_home
    ):
        writes, logs, call = self.setup(tmp_path, turn_home, monkeypatch, "upper", armed=armed,
                                        turn=self.fake_turn(armed))
        calls = self.raising_after_the_first_call(monkeypatch)
        assert call() == (0, "", 0.0, False)
        assert calls["n"] > 1 and self.denial_writes(writes) == []

    def test_a_helper_that_raises_in_the_record_step_keeps_the_exception(
        self, tmp_path, turn_home, monkeypatch, r4t_home
    ):
        def turn(rig, prompt, *, launch, variant, instruments, turn_context):
            raise OSError("original failure")
        writes, logs, call = self.setup(tmp_path, turn_home, monkeypatch, "upper", armed=False,
                                        turn=turn)
        self.raising_after_the_first_call(monkeypatch)
        with pytest.raises(OSError, match="original failure"):
            call()

    def test_a_helper_that_raises_keeps_a_refusal_before_the_launch(
        self, tmp_path, turn_home, monkeypatch, r4t_home
    ):
        writes, logs, call = self.setup(tmp_path, turn_home, monkeypatch, "upper", armed=True,
                                        env_extra={"COPILOT_HOME": "rel/dir"})
        # the refusal comes before the main path reads the helper, so every
        # call is the record step's
        def down(env):
            raise RuntimeError("helper down")
        monkeypatch.setattr(dispatch, "roster_context", down)
        assert call()[0] == 126 and self.denial_writes(writes) == []

    def test_a_helper_that_raises_keeps_an_interrupt(self, tmp_path, turn_home, monkeypatch, r4t_home):
        def turn(rig, prompt, *, launch, variant, instruments, turn_context):
            raise KeyboardInterrupt
        writes, logs, call = self.setup(tmp_path, turn_home, monkeypatch, "upper", armed=True,
                                        turn=turn)
        self.raising_after_the_first_call(monkeypatch)
        with pytest.raises(KeyboardInterrupt):
            call()


class TestRoutingNamesHaveOneReader:
    NAMES = re.compile(r"""\.get\(\s*["']R4T_(SESSION|NODE|MEMBER)["']""")

    def test_no_raw_read_of_the_routing_names_remains_outside_the_helper(self):
        import re as _re
        root = Path(dispatch.__file__).resolve().parent
        offenders = []
        for relative in ("dispatch.py", "engines/copilot.py", "engines/run.py"):
            text = (root / relative).read_text(encoding="utf-8")
            tree = ast.parse(text)
            helper_lines = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "roster_context":
                    helper_lines = set(range(node.lineno, node.end_lineno + 1))
            for number, line in enumerate(text.splitlines(), 1):
                code = line.split("#", 1)[0]
                if self.NAMES.search(code) and number not in helper_lines:
                    offenders.append(f"{relative}:{number}: {line.strip()}")
        assert not offenders, "read the routing names with dispatch.roster_context:\n" + "\n".join(offenders)

    def test_the_helper_reads_the_canonical_view(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        context = dispatch.roster_context({"r4t_node": "n", "R4t_Member": "m", "r4t_session": "s"})
        assert tuple(context) == ("n", "m", "s")
        monkeypatch.setattr(sys, "platform", "linux")
        assert tuple(dispatch.roster_context({"r4t_node": "n"})) == ("", "", None)
        assert tuple(dispatch.roster_context(None)) == ("", "", None)
