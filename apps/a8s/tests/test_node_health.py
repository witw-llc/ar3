"""`a8s health [<node>]` through the public CLI.

r4t is a sibling process, so these tests stand a fake `r4t.py` in its place
(`node_health.R4T_PY`). It records every argv, working directory and `HX_*`
environment variable it is given and answers from a scenario file, which lets
the argument contract, the composed environment and the timeouts be checked
without any provider. Real-r4t integration is the parent's to combine."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import cli
import node_health
import txlog
from core import pid_path, transactions_path, write_wake_pid
from registry import save_registry

SENTINEL = "sk-sentinel-4f9a1c7e2b"

FAKE_R4T = '''\
import json, os, subprocess, sys, time

here = os.path.dirname(os.path.abspath(__file__))
scenario = json.load(open(os.path.join(here, "scenario.json"), encoding="utf-8"))
argv = sys.argv[1:]
record = {
    "argv": argv,
    "cwd": os.getcwd(),
    "env": {k: v for k, v in os.environ.items() if k.startswith("HX_") or k == "MUSE_NO_AUTO_UPDATE"},
}
with open(os.path.join(here, "calls.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps(record) + "\\n")
spec = scenario.get(argv[2], {})
if spec.get("hang"):
    kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(os.path.join(here, "kid.pid"), "w", encoding="utf-8") as f:
        f.write(str(kid.pid))
    time.sleep(60)
if "stdout" in spec:
    print(json.dumps(spec["stdout"]))
else:
    print(spec.get("raw", ""))
sys.exit(spec.get("rc", 0))
'''

LIVE = {
    "engine": "claude",
    "origin": "live",
    "plan": "Max",
    "buckets": [
        {"label": "5-hour", "remaining_fraction": 0.62, "reset_time": "2026-11-04T23:00:00Z"},
        {"label": "Weekly", "remaining_fraction": 0.4, "reset_time": "2026-11-09T00:00:00Z"},
    ],
}
CHECK_OK = [{"engine": "claude", "binary": "claude", "installed": True, "runnable": True, "detail": ""}]


@pytest.fixture
def r4t(tmp_path, monkeypatch):
    folder = tmp_path / "fake-r4t"
    folder.mkdir()
    script = folder / "r4t.py"
    script.write_text(FAKE_R4T, encoding="utf-8")
    monkeypatch.setattr(node_health, "R4T_PY", script)

    class Fake:
        def scenario(self, **verbs):
            (folder / "scenario.json").write_text(json.dumps(verbs), encoding="utf-8")

        def calls(self):
            path = folder / "calls.jsonl"
            if not path.exists():
                return []
            return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        kid = folder / "kid.pid"

    fake = Fake()
    fake.scenario(check={"stdout": CHECK_OK}, quota={"stdout": LIVE})
    return fake


ENGINE_ARGV = [
    "$PYTHON", "$A8S_DIR/../r4t/r4t.py", "engine", "claude", "run",
    "--effort=$EFFORT?", "--model=$MODEL?", "--agent", "$RECIPIENT",
]


@pytest.fixture
def make_node(fake_home, tmp_path):
    def make(name="alpha", definition=None, vars=None, registered=True):
        root = tmp_path / f"root-{name}"
        root.mkdir(exist_ok=True)
        definition = {"invoke": ENGINE_ARGV} if definition is None else definition
        path = tmp_path / f"{name}-def.json"
        path.write_text(json.dumps(definition), encoding="utf-8")
        info = {"root": str(root), "definition": str(path)}
        if vars:
            info["vars"] = vars
        try:
            from registry import load_registry
            agents = dict(load_registry())
        except Exception:
            agents = {}
        agents[name] = info
        save_registry(agents)
        if not transactions_path().exists():
            txlog.log("ROUTED", msg_id="seed", sender="x", recipient="someone-else")
        return root

    return make


def health(capsys, *args):
    code = cli.main(["health", *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def tree(base: Path) -> dict:
    """Every path and content under `base`, less what the existing strict
    readers leave by design (the empty `pid.lock`, sqlite's `-wal`/`-shm`
    beside a read-only open) and this file's fake r4t."""
    snap = {}
    for p in sorted(base.rglob("*")):
        rel = str(p.relative_to(base))
        if rel.startswith("fake-r4t") or rel.endswith(("pid.lock", "-wal", "-shm")):
            continue
        snap[rel] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir"
    return snap


class TestSurface:
    def test_bare_health_is_unchanged(self, fake_home, capsys):
        code, out, err = health(capsys)
        assert (code, err) == (0, "")
        assert out == "remotes: (none configured)\nstorage: (none configured)\nagents: 0 registered\n"

    def test_help_exits_zero_and_describes_the_operand(self, fake_home, capsys):
        for flag in ("--help", "-h"):
            code, out, err = health(capsys, flag)
            assert code == 0 and err == ""
            assert "usage: a8s health" in out and "node" in out
            assert "agents:" not in out

    @pytest.mark.parametrize("args", [["a", "b"], ["--bogus"], ["alpha", "--bogus"]])
    def test_extra_or_unknown_operands_are_a_usage_error(self, fake_home, capsys, args):
        code, out, err = health(capsys, *args)
        assert code == 2
        assert out == ""
        assert "a8s health" in err

    def test_unknown_name_fails_clearly(self, make_node, capsys):
        make_node()
        code, out, err = health(capsys, "nobody")
        assert (code, out) == (1, "")
        assert "a8s: health: no node named 'nobody'" in err

    def test_alias_is_refused(self, make_node, capsys):
        make_node()
        from registry import save_aliases
        save_aliases({"team": ["alpha"]})
        code, out, err = health(capsys, "team")
        assert (code, out) == (1, "")
        assert "alias" in err and "alpha" in err

    def test_remote_only_name_shows_only_what_is_known(self, make_node, r4t, monkeypatch, capsys):
        make_node()
        monkeypatch.setattr(node_health, "last_heard", lambda: {"Robin": "2026-11-03T12:00:00Z"})
        code, out, err = health(capsys, "robin")
        assert code == 1 and err == ""
        assert "node: Robin (remote, remembered)" in out
        assert "(observation, not health)" in out
        for what in ("runtime", "engine", "quota"):
            assert f"{what}: unavailable (remote node; not known here)" in out
        assert r4t.calls() == []

    def test_local_name_wins_over_a_remote_of_the_same_name(self, make_node, r4t, monkeypatch, capsys):
        make_node()
        monkeypatch.setattr(node_health, "last_heard", lambda: {"ALPHA": "2026-11-03T12:00:00Z"})
        _code, out, _err = health(capsys, "Alpha")
        assert "node: alpha (local)" in out


class TestLocalNode:
    def test_engine_node_shows_everything_in_one_view(self, make_node, r4t, capsys):
        make_node(vars={"MODEL": "opus", "EFFORT": "high", "TIMEOUT": "300"})
        code, out, err = health(capsys, "alpha")
        assert code == 0 and err == ""
        for line in (
            "node: alpha (local)",
            "definition: alpha-def (registry)",
            "engine: claude",
            "model: opus",
            "effort: high",
            "vars: MODEL=opus EFFORT=high TIMEOUT=300",
            "runtime: stopped",
            "activity: idle (no status probe; no wake in flight)",
            "last wake: none recorded",
            "readiness: ready (claude installed",
            "transport: remotes are machine-wide",
            "remotes: (none configured)",
            "quota: live — Max",
            "  5-hour: 62% remaining · resets 2026-11-04T23:00:00Z",
            "  Weekly: 40% remaining · resets 2026-11-09T00:00:00Z",
            "findings: none",
        ):
            assert line in out, line

    def test_unset_model_and_effort_read_engine_default(self, make_node, r4t, capsys):
        make_node()
        _code, out, _err = health(capsys, "alpha")
        assert "model: engine default" in out and "effort: engine default" in out

    def test_running_node_shows_pid_and_uptime(self, make_node, r4t, capsys):
        make_node()
        pid_path("alpha").parent.mkdir(parents=True, exist_ok=True)
        pid_path("alpha").write_text(str(os.getpid()), encoding="utf-8")
        _code, out, _err = health(capsys, "alpha")
        assert f"runtime: running pid {os.getpid()}, up " in out

    def test_unreadable_pid_file_is_unknown_not_stopped(self, make_node, r4t, capsys):
        make_node()
        pid_path("alpha").parent.mkdir(parents=True, exist_ok=True)
        pid_path("alpha").write_text("garbage", encoding="utf-8")
        code, out, _err = health(capsys, "alpha")
        assert code == 1
        assert "runtime: unknown" in out and "runtime: stopped" not in out
        assert pid_path("alpha").read_text(encoding="utf-8") == "garbage"

    def test_dead_pid_is_not_reaped(self, make_node, r4t, capsys):
        make_node()
        pid_path("alpha").parent.mkdir(parents=True, exist_ok=True)
        pid_path("alpha").write_text("2147483646", encoding="utf-8")
        _code, out, _err = health(capsys, "alpha")
        assert "runtime: stopped" in out
        assert pid_path("alpha").exists()

    def test_wake_in_flight_is_busy(self, make_node, r4t, capsys):
        make_node()
        write_wake_pid("alpha", os.getpid(), ["a.json", "b.json"])
        _code, out, _err = health(capsys, "alpha")
        assert f"activity: busy (wake in flight, pid {os.getpid()}; 2 envelope(s))" in out

    def test_resident_session_runs_one_status_probe(self, make_node, r4t, tmp_path, capsys):
        probe = tmp_path / "probe.py"
        count = tmp_path / "probe-count"
        probe.write_text(
            f"open({str(count)!r}, 'a', encoding='utf-8').write('x')\nprint('resident')\n",
            encoding="utf-8",
        )
        make_node(definition={
            "invoke": ENGINE_ARGV,
            "status": {"invoke": [sys.executable, str(probe)]},
        })
        _code, out, _err = health(capsys, "alpha")
        assert "activity: resident idle" in out
        assert count.read_text(encoding="utf-8") == "x"

    def test_failed_status_probe_is_unknown_not_idle(self, make_node, r4t, tmp_path, capsys):
        probe = tmp_path / "bad.py"
        probe.write_text("import sys; sys.exit(3)\n", encoding="utf-8")
        make_node(definition={
            "invoke": ENGINE_ARGV,
            "status": {"invoke": [sys.executable, str(probe)]},
        })
        code, out, _err = health(capsys, "alpha")
        assert code == 1
        assert "activity: unknown (status probe failed" in out
        assert "activity: idle" not in out

    def test_unresolved_definition_keeps_process_facts(self, make_node, r4t, capsys):
        root = make_node()
        save_registry({"alpha": {"root": str(root), "definition": str(root / "missing.json")}})
        code, out, _err = health(capsys, "alpha")
        assert code == 1
        assert "unresolved: definition file missing" in out
        assert "runtime: stopped" in out
        assert "engine: unavailable (definition unresolved)" in out
        assert "quota: unavailable (definition unresolved)" in out
        assert r4t.calls() == []

    def test_unresolved_mailbox_var_is_not_routing(self, make_node, r4t, capsys):
        make_node(definition={"invoke": ENGINE_ARGV, "inbox_dir": ".in-$SEAT"})
        code, out, _err = health(capsys, "alpha")
        assert code == 1
        assert "routing: not routing" in out and "$SEAT" in out

    def test_stats_use_the_24_hour_window(self, make_node, r4t, capsys):
        make_node()
        for age in (1, 5, 25):
            txlog.log("WAKE_FAILED", msg_id="", sender="x", recipient="alpha")
            stamp = (datetime.now(timezone.utc) - timedelta(hours=age)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            with sqlite3.connect(transactions_path()) as conn:
                conn.execute(
                    "UPDATE transactions SET timestamp = ? WHERE seq = (SELECT MAX(seq) FROM transactions)",
                    (stamp,),
                )
        code, out, _err = health(capsys, "alpha")
        assert code == 1
        assert "failed wakes (24 h): 2" in out
        assert "2 failed wake(s) in 24 h" in out

    def test_unreadable_transaction_log_is_unknown_not_zero(self, make_node, r4t, capsys):
        make_node()
        transactions_path().unlink()
        code, out, _err = health(capsys, "alpha")
        assert code == 1
        assert "failed wakes (24 h): unknown" in out
        assert "failed wakes (24 h): 0" not in out
        assert "last sent: unknown" in out

    def test_non_engine_node_has_quota_not_applicable_and_spawns_nothing(self, make_node, r4t, capsys):
        make_node(definition={"invoke": ["$PYTHON", "$A8S_DIR/echo_agent.py", "$SENDER", "$MESSAGE"]})
        code, out, _err = health(capsys, "alpha")
        assert "engine: none" in out
        assert "quota: not applicable (no engine)" in out
        assert "readiness: not applicable" in out
        assert r4t.calls() == []

    def test_roster_node_does_not_invent_one_engine_quota(self, make_node, r4t, capsys):
        make_node(definition={
            "invoke": ["$PYTHON", "$A8S_DIR/../r4t/r4t.py", "dispatch", "--root", "."],
            "idle": {"invoke": ["$PYTHON", "$A8S_DIR/../r4t/r4t.py", "idle", "--root", "."]},
        })
        _code, out, _err = health(capsys, "alpha")
        assert "engine: none (roster node" in out
        assert "quota: not applicable (roster node" in out
        assert r4t.calls() == []

    def test_engine_is_read_from_idle_and_batch_lines_too(self, make_node, r4t, capsys):
        codex = [a if a != "claude" else "codex" for a in ENGINE_ARGV]
        make_node(definition={"invoke": ENGINE_ARGV, "batch": {"invoke": codex}})
        _code, out, _err = health(capsys, "alpha")
        assert "engine: ambiguous (lines name different engines" in out
        assert "quota: unavailable (lines name different engines" in out
        assert r4t.calls() == []

    def test_native_engine_is_found_from_the_program(self, make_node, r4t, capsys):
        make_node(definition={"invoke": ["claude", "-p", "$MESSAGE"]})
        _code, out, _err = health(capsys, "alpha")
        assert "engine: claude" in out
        assert [c["argv"][:3] for c in r4t.calls()] == [["engine", "claude", "check"], ["engine", "claude", "quota"]]

    def test_absolute_native_binary_that_path_would_not_pick_is_unpinned(
        self, make_node, r4t, tmp_path, capsys
    ):
        other = tmp_path / "custom" / "claude"
        other.parent.mkdir()
        other.write_text("#!/bin/sh\n", encoding="utf-8")
        other.chmod(0o755)
        make_node(definition={"invoke": [str(other), "-p", "$MESSAGE"]})
        code, out, _err = health(capsys, "alpha")
        assert code == 1
        assert "not pinned" in out and "quota: unavailable" in out
        assert r4t.calls() == []

    def test_absolute_native_binary_that_path_resolves_to_is_queried(
        self, make_node, r4t, tmp_path, monkeypatch, capsys
    ):
        binary = tmp_path / "bin" / "claude"
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o755)
        make_node(
            definition={"invoke": [str(binary), "-p", "$MESSAGE"]},
            vars={"env.PATH": f"{binary.parent}{os.pathsep}/usr/bin"},
        )
        _code, out, _err = health(capsys, "alpha")
        assert "engine: claude" in out and "not pinned" not in out
        assert len(r4t.calls()) == 2


class TestQuota:
    @pytest.fixture(autouse=True)
    def _default_node(self, make_node):
        make_node()

    def test_child_contract_env_and_cwd(self, make_node, r4t, monkeypatch, capsys):
        monkeypatch.setenv("HX_A", "operator")
        monkeypatch.setenv("HX_C", "operator")
        root = make_node(
            definition={"invoke": ENGINE_ARGV, "env": {"HX_A": "definition", "HX_B": "definition"}},
            vars={"env.HX_B": "node"},
        )
        health(capsys, "alpha")
        quota = [c for c in r4t.calls() if c["argv"][2] == "quota"]
        assert len(quota) == 1
        call = quota[0]
        assert call["argv"] == ["engine", "claude", "quota", "--live", "--json"]
        assert call["env"] == {"HX_A": "definition", "HX_B": "node", "HX_C": "operator"}
        assert os.path.realpath(call["cwd"]) == os.path.realpath(root)

    def test_check_gets_the_same_environment(self, make_node, r4t, capsys):
        make_node(definition={"invoke": ENGINE_ARGV, "env": {"HX_A": "definition"}})
        health(capsys, "alpha")
        check = [c for c in r4t.calls() if c["argv"][2] == "check"]
        assert check[0]["argv"][-2:] == ["check", "--json"]
        assert check[0]["env"] == {"HX_A": "definition"}

    def test_exhausted_bucket_is_a_finding_without_an_invented_aggregate(self, make_node, r4t, capsys):
        payload = dict(LIVE, buckets=[
            {"label": "5-hour", "remaining_fraction": 0.0, "reset_time": "2026-11-04T23:00:00Z"},
            {"label": "Weekly", "remaining_fraction": 0.4, "reset_time": None},
        ])
        r4t.scenario(check={"stdout": CHECK_OK}, quota={"stdout": payload})
        code, out, _err = health(capsys, "alpha")
        assert code == 1
        assert "5-hour: 0% remaining (exhausted) · resets 2026-11-04T23:00:00Z" in out
        assert "Weekly: 40% remaining" in out
        assert "5-hour exhausted until 2026-11-04T23:00:00Z" in out
        assert "overall" not in out.lower() and "combined" not in out.lower()

    def test_unavailable_shows_the_reason_and_exits_one(self, make_node, r4t, capsys):
        r4t.scenario(check={"stdout": CHECK_OK}, quota={
            "rc": 1,
            "stdout": {"origin": "unavailable", "engine": "claude", "applicable": True, "reason": "claude is signed out"},
        })
        code, out, _err = health(capsys, "alpha")
        assert code == 1
        assert "quota: unavailable (r4t could not take a live reading; run `r4t engine claude quota --live`)" in out
        assert "signed out" not in out
        assert "% remaining" not in out

    def test_a_known_code_from_r4t_has_a_fixed_hint(self, make_node, r4t, capsys):
        r4t.scenario(check={"stdout": CHECK_OK}, quota={
            "rc": 1,
            "stdout": {"origin": "unavailable", "engine": "claude", "applicable": True,
                       "reason": f"gh said {SENTINEL}", "code": "account_mismatch"},
        })
        _code, out, err = health(capsys, "alpha")
        assert "account mismatch: the quota adapter cannot use this node's account settings" in out
        assert "sentinel" not in (out + err).lower()

    def test_unknown_code_is_not_forwarded(self, make_node, r4t, capsys):
        r4t.scenario(check={"stdout": CHECK_OK}, quota={
            "rc": 1,
            "stdout": {"origin": "unavailable", "engine": "claude", "applicable": True,
                       "reason": "account mismatch: CLAUDE_CONFIG_DIR differs from the signed-in home",
                       "code": "account-mismatch"},
        })
        code, out, _err = health(capsys, "alpha")
        assert code == 1
        assert "quota: unavailable (r4t could not take a live reading;" in out
        assert "code account-mismatch" not in out
        assert "CLAUDE_CONFIG_DIR" not in out

    @pytest.mark.parametrize("rc", [0, 3])
    def test_snapshot_is_refused_even_on_exit_zero(self, make_node, r4t, capsys, rc):
        snapshot = dict(LIVE, origin="snapshot", age_seconds=600)
        r4t.scenario(check={"stdout": CHECK_OK}, quota={"rc": rc, "stdout": snapshot})
        code, out, _err = health(capsys, "alpha")
        assert code == 1
        assert "quota: unavailable" in out
        assert "% remaining" not in out

    def test_stale_live_label_is_refused(self, make_node, r4t, capsys):
        r4t.scenario(check={"stdout": CHECK_OK}, quota={"stdout": dict(LIVE, age_seconds=4000)})
        code, out, _err = health(capsys, "alpha")
        assert code == 1 and "refused as stale" in out and "% remaining" not in out

    @pytest.mark.parametrize("scenario", [
        {"raw": "not json"},
        {"stdout": ["a list"]},
        {"stdout": dict(LIVE, buckets=[])},
        {"stdout": dict(LIVE, buckets="x")},
        {"rc": 1, "raw": ""},
        {"rc": 7, "stdout": {"origin": "live"}},
    ])
    def test_malformed_answers_are_unavailable_not_healthy(self, make_node, r4t, capsys, scenario):
        r4t.scenario(check={"stdout": CHECK_OK}, quota=scenario)
        code, out, _err = health(capsys, "alpha")
        assert code == 1
        assert "quota: unavailable" in out

    def test_unknown_engine_reported_by_r4t_is_not_applicable(self, make_node, r4t, capsys):
        r4t.scenario(check={"stdout": CHECK_OK}, quota={
            "rc": 1,
            "stdout": {"origin": "unavailable", "engine": None, "applicable": False, "reason": "unknown engine"},
        })
        code, out, _err = health(capsys, "alpha")
        assert "quota: not applicable (r4t reports this engine has no quota to read)" in out

    def test_unrunnable_engine_is_a_readiness_failure(self, make_node, r4t, capsys):
        r4t.scenario(
            check={"stdout": [{"binary": "claude", "installed": False, "runnable": False,
                               "detail": "claude is not on PATH"}]},
            quota={"stdout": LIVE},
        )
        code, out, _err = health(capsys, "alpha")
        assert code == 1
        assert "readiness: FAIL (claude is not installed; run `r4t engine claude check`)" in out
        assert "is not on PATH" not in out

    def test_malformed_check_report_is_unavailable(self, make_node, r4t, capsys):
        r4t.scenario(check={"raw": "[]"}, quota={"stdout": LIVE})
        code, out, _err = health(capsys, "alpha")
        assert code == 1
        assert "readiness: unavailable (check gave a malformed report; run `r4t engine claude check`)" in out

    def test_timeout_ends_the_whole_group_and_returns(self, make_node, r4t, monkeypatch, capsys):
        monkeypatch.setattr(node_health, "QUOTA_TIMEOUT_SECONDS", 1)
        r4t.scenario(check={"stdout": CHECK_OK}, quota={"hang": True})
        started = time.monotonic()
        code, out, _err = health(capsys, "alpha")
        assert time.monotonic() - started < 15
        assert code == 1
        assert "quota: unavailable (timed out after 1s)" in out
        assert 'a8s: health' not in out
        kid = int(r4t.kid.read_text(encoding="utf-8"))
        for _ in range(50):
            try:
                os.kill(kid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(kid, 9)
            pytest.fail("a descendant of the timed-out child survived")

    def test_login_shell_node_does_not_guess_its_environment(self, make_node, r4t, capsys):
        make_node(definition={"invoke": ENGINE_ARGV, "wake_shell": "login"})
        code, out, _err = health(capsys, "alpha")
        assert code == 1
        assert "readiness: unavailable (the node wakes through a login shell" in out
        assert "quota: unavailable (the node wakes through a login shell" in out
        assert r4t.calls() == []

    def test_account_variable_from_the_shell_only_is_labelled(self, make_node, r4t, monkeypatch, capsys):
        monkeypatch.setenv("CODEX_HOME", "/somewhere")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/elsewhere")
        make_node(vars={"env.CLAUDE_CONFIG_DIR": "/node/claude"})
        _code, out, _err = health(capsys, "alpha")
        assert "inherited only from this shell:" in out and "CODEX_HOME" in out
        assert "CLAUDE_CONFIG_DIR" not in out.split("note:")[1].splitlines()[0]


class TestSecretsAndMutation:
    def test_no_secret_value_reaches_stdout_or_stderr(self, make_node, r4t, capsys):
        r4t.scenario(check={"stdout": CHECK_OK}, quota={
            "rc": 1,
            "stdout": {"origin": "unavailable", "applicable": True,
                       "reason": f"token {SENTINEL} rejected\nsecond line"},
        })
        make_node(
            definition={"invoke": ENGINE_ARGV, "env": {"DEF_TOKEN": SENTINEL + "-def"}},
            vars={"env.API_TOKEN": SENTINEL, "NOTE": SENTINEL + "-var", "MODEL": "opus"},
        )
        code, out, err = health(capsys, "alpha")
        assert code == 1
        assert "sentinel" not in (out + err).lower()
        assert "env (values hidden): API_TOKEN" in out
        assert "vars hidden: NOTE" in out
        assert "definition env (values hidden): DEF_TOKEN" in out
        assert "rejected" not in out
        assert "model: opus" in out

    def test_expanded_argv_is_never_printed(self, make_node, r4t, capsys):
        make_node(vars={"MODEL": "opus"})
        _code, out, err = health(capsys, "alpha")
        assert "--agent" not in out + err and "$RECIPIENT" not in out + err

    def test_inspection_changes_nothing_on_disk(self, make_node, r4t, fake_home, capsys):
        make_node()
        pid_path("alpha").parent.mkdir(parents=True, exist_ok=True)
        pid_path("alpha").write_text("2147483646", encoding="utf-8")
        inbox = fake_home / "unrelated"
        inbox.mkdir()
        (inbox / "keep.json").write_text("{}", encoding="utf-8")
        before = tree(fake_home)
        health(capsys, "alpha")
        health(capsys, "nobody")
        assert tree(fake_home) == before


# The live payload `r4t engine copilot quota --live` returns for the
# token-billing Business seat in r4t's own USER_SAMPLE (test_engine_copilot):
# remaining_fraction is None, credits_used is the one measured signal.
COPILOT_TOKEN_BILLING = {
    "engine": "copilot",
    "origin": "live",
    "plan": "business",
    "buckets": [
        {"label": "Chat", "remaining_fraction": None, "credits_used": 0,
         "reset_time": "2026-10-01T00:00:00.000Z"},
        {"label": "Completions", "remaining_fraction": None, "credits_used": 0,
         "reset_time": "2026-10-01T00:00:00.000Z"},
        {"label": "Premium Requests", "remaining_fraction": None, "credits_used": 395,
         "reset_time": "2026-10-01T00:00:00.000Z"},
    ],
    "note": "premium_interactions: 395 credits used this cycle",
}


def bucket(**fields):
    base = {"label": "5-hour", "remaining_fraction": 0.5, "reset_time": "2026-11-04T23:00:00Z"}
    return dict(LIVE, buckets=[{**base, **fields}])


def without(key, payload):
    return {k: v for k, v in payload.items() if k != key}


class TestBucketValidation:
    @pytest.fixture(autouse=True)
    def _node(self, make_node):
        make_node()

    @pytest.mark.parametrize("payload", [
        bucket(remaining_fraction=True),
        bucket(remaining_fraction=False),
        bucket(remaining_fraction=float("nan")),
        bucket(remaining_fraction=float("inf")),
        bucket(remaining_fraction=1.5),
        bucket(remaining_fraction=-0.1),
        bucket(remaining_fraction="0.5"),
        bucket(remaining_fraction=None, credits_used=-1),
        bucket(remaining_fraction=None, credits_used=True),
        bucket(remaining_fraction=None, credits_used="395"),
        bucket(remaining_fraction=None, credits_used=float("nan")),
        bucket(remaining_fraction=None, credits_used=float("inf")),
        bucket(credits_used=-3),
        bucket(label=None),
        bucket(label=""),
        bucket(label="x" * 200),
        bucket(label="k" * 40),
        bucket(reset_time=5),
        bucket(reset_time="tomorrow-" + "z" * 40),
        dict(LIVE, buckets=[{"label": "5-hour", "reset_time": None}]),
        dict(LIVE, buckets=["x"]),
        dict(LIVE, age_seconds="12"),
        dict(LIVE, age_seconds=True),
        dict(LIVE, age_seconds=float("nan")),
        dict(LIVE, age_seconds=float("inf")),
        dict(LIVE, age_seconds=-4000),
        dict(LIVE, engine="codex"),
        dict(LIVE, engine=None),
        without("engine", LIVE),
    ])
    def test_malformed_live_reading_is_unavailable_never_healthy(self, r4t, capsys, payload):
        r4t.scenario(check={"stdout": CHECK_OK}, quota={"stdout": payload})
        code, out, err = health(capsys, "alpha")
        assert code == 1
        assert "quota: unavailable (" in out
        assert "% remaining" not in out and "remaining unknown" not in out
        assert "\n  5-hour:" not in out and "\n  Weekly:" not in out
        assert "findings: none" not in out
        assert "k" * 40 not in out + err

    def test_a_good_bucket_beside_a_bad_one_refuses_the_whole_reading(self, r4t, capsys):
        payload = dict(LIVE, buckets=[LIVE["buckets"][0], {"label": "Weekly", "remaining_fraction": True}])
        r4t.scenario(check={"stdout": CHECK_OK}, quota={"stdout": payload})
        code, out, _err = health(capsys, "alpha")
        assert code == 1 and "% remaining" not in out

    def test_the_engine_is_matched_case_insensitively_against_the_real_one(self, r4t, capsys):
        r4t.scenario(check={"stdout": CHECK_OK}, quota={"stdout": dict(LIVE, engine="Claude")})
        code, out, _err = health(capsys, "alpha")
        assert code == 0 and "62% remaining" in out

    def test_copilot_token_billing_shows_credits_and_unknown_remaining(self, make_node, r4t, capsys):
        make_node(definition={"invoke": ["copilot", "-s", "-p", "$MESSAGE"]})
        r4t.scenario(check={"stdout": CHECK_OK}, quota={"stdout": COPILOT_TOKEN_BILLING})
        code, out, _err = health(capsys, "alpha")
        assert code == 0
        assert "quota: live — business" in out
        assert "  Chat: remaining unknown · 0 credits used · resets 2026-10-01T00:00:00.000Z" in out
        assert "  Premium Requests: remaining unknown · 395 credits used · resets 2026-10-01T00:00:00.000Z" in out
        assert "findings: none" in out
        low = out.lower()
        assert "overall" not in low and "combined" not in low and "%" not in out and "limit" not in low

    def test_credits_beside_a_real_fraction_are_both_shown(self, r4t, capsys):
        r4t.scenario(check={"stdout": CHECK_OK}, quota={"stdout": bucket(credits_used=12.5)})
        _code, out, _err = health(capsys, "alpha")
        assert "5-hour: 50% remaining · 12.5 credits used · resets" in out

    def test_a_fraction_with_null_credits_is_still_a_measured_bucket(self, r4t, capsys):
        r4t.scenario(check={"stdout": CHECK_OK}, quota={"stdout": bucket(credits_used=None)})
        code, out, _err = health(capsys, "alpha")
        assert code == 0 and "5-hour: 50% remaining" in out and "credits" not in out

    def test_provider_prose_in_the_payload_is_not_printed(self, r4t, capsys):
        payload = dict(LIVE, note=f"token {SENTINEL} echoed", plan=f"plan {SENTINEL}" + "q" * 30)
        r4t.scenario(check={"stdout": CHECK_OK}, quota={"stdout": payload})
        _code, out, err = health(capsys, "alpha")
        assert "sentinel" not in (out + err).lower()
        assert "quota: live\n" in out


class TestEngineReadiness:
    @pytest.fixture(autouse=True)
    def _node(self, make_node):
        make_node()

    def test_pass_claims_only_what_the_default_check_proved(self, r4t, capsys):
        _code, out, _err = health(capsys, "alpha")
        assert "accepts r4t's default engine argv" in out
        assert "the node's own model, effort and flags are not tried" in out
        assert "accepts the node's argv" not in out
        assert r4t.calls()[0]["argv"] == ["engine", "claude", "check", "--json"]

    @pytest.mark.parametrize("report", [
        {"installed": True},
        {"installed": True, "runnable": "yes"},
        {"installed": True, "runnable": None},
        {"installed": "yes", "runnable": True},
    ])
    def test_a_missing_or_non_boolean_runnable_is_unavailable(self, r4t, capsys, report):
        r4t.scenario(check={"stdout": [report]}, quota={"stdout": LIVE})
        code, out, _err = health(capsys, "alpha")
        assert code == 1
        assert "readiness: unavailable (check gave a malformed report; run `r4t engine claude check`)" in out

    def test_installed_but_rejected_argv_is_a_failure_with_the_repair_command(self, r4t, capsys):
        r4t.scenario(
            check={"stdout": [{"binary": "claude", "installed": True, "runnable": False,
                               "detail": f"unknown flag --x, token {SENTINEL}"}]},
            quota={"stdout": LIVE},
        )
        code, out, err = health(capsys, "alpha")
        assert code == 1
        assert "readiness: FAIL (claude is installed but r4t's engine argv was not accepted; run `r4t engine claude check`)" in out
        assert "unknown flag" not in out and "sentinel" not in (out + err).lower()


class TestMuseProbeEnvironment:
    def test_both_muse_probes_disable_the_launcher_update(self, make_node, r4t, capsys):
        root = make_node(definition={"invoke": ["muse", "exec", "--approval-mode", "never", "$MESSAGE"]})
        before = (root.parent / "alpha-def.json").read_bytes()
        r4t.scenario(check={"stdout": CHECK_OK}, quota={"stdout": dict(LIVE, engine="muse")})
        code, out, _err = health(capsys, "alpha")
        assert code == 0 and "engine: muse" in out
        calls = r4t.calls()
        assert [c["argv"][2] for c in calls] == ["check", "quota"]
        assert all(c["env"] == {"MUSE_NO_AUTO_UPDATE": "1"} for c in calls)
        assert (root.parent / "alpha-def.json").read_bytes() == before
        from registry import load_registry
        assert "MUSE_NO_AUTO_UPDATE" not in json.dumps(load_registry())

    def test_other_engines_do_not_get_it(self, make_node, r4t, capsys):
        make_node()
        health(capsys, "alpha")
        assert all("MUSE_NO_AUTO_UPDATE" not in c["env"] for c in r4t.calls())

    def test_the_inherited_value_is_overridden_not_the_node_setting(self, make_node, r4t, monkeypatch, capsys):
        monkeypatch.setenv("MUSE_NO_AUTO_UPDATE", "0")
        make_node(definition={"invoke": ["muse", "exec", "$MESSAGE"]})
        r4t.scenario(check={"stdout": CHECK_OK}, quota={"stdout": dict(LIVE, engine="muse")})
        health(capsys, "alpha")
        assert {c["env"]["MUSE_NO_AUTO_UPDATE"] for c in r4t.calls()} == {"1"}


BUNDLED = Path(node_health.SCRIPT_DIR) / "definitions"


class TestNativeEngineNames:
    @pytest.fixture
    def agent_on_path(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        agent = bin_dir / "agent"
        agent.write_text("#!/bin/sh\n", encoding="utf-8")
        agent.chmod(0o755)
        return bin_dir

    def test_bundled_cursor_definition_is_the_cursor_engine(self, make_node, r4t, agent_on_path, capsys):
        definition = json.loads((BUNDLED / "cursor.json").read_text(encoding="utf-8"))
        make_node(definition=definition, vars={"env.PATH": f"{agent_on_path}{os.pathsep}/usr/bin"})
        r4t.scenario(check={"stdout": CHECK_OK}, quota={
            "rc": 1,
            "stdout": {"origin": "unavailable", "engine": "cursor", "applicable": True,
                       "reason": "cursor has no live quota"},
        })
        code, out, _err = health(capsys, "alpha")
        assert code == 1
        assert "engine: cursor\n" in out and "engine: none" not in out
        assert [c["argv"][:3] for c in r4t.calls()] == [["engine", "cursor", "check"], ["engine", "cursor", "quota"]]
        assert "quota: unavailable (r4t could not take a live reading" in out
        assert "not applicable" not in out.split("quota:")[1]

    def test_cursor_agent_is_queried_only_if_it_is_the_file_r4t_would_start(
        self, make_node, r4t, agent_on_path, capsys
    ):
        (agent_on_path / "cursor-agent").write_text("#!/bin/sh\n", encoding="utf-8")
        (agent_on_path / "cursor-agent").chmod(0o755)
        make_node(
            definition={"invoke": ["cursor-agent", "-p", "$MESSAGE"]},
            vars={"env.PATH": f"{agent_on_path}{os.pathsep}/usr/bin"},
        )
        code, out, _err = health(capsys, "alpha")
        assert code == 1
        assert "engine: cursor (not pinned" in out and "r4t would query 'agent'" in out
        assert r4t.calls() == []

    def test_cursor_agent_linked_to_agent_is_pinned(self, make_node, r4t, agent_on_path, capsys):
        (agent_on_path / "cursor-agent").symlink_to(agent_on_path / "agent")
        make_node(
            definition={"invoke": ["cursor-agent", "-p", "$MESSAGE"]},
            vars={"env.PATH": f"{agent_on_path}{os.pathsep}/usr/bin"},
        )
        r4t.scenario(check={"stdout": CHECK_OK}, quota={"stdout": dict(LIVE, engine="cursor")})
        code, out, _err = health(capsys, "alpha")
        assert code == 0 and "engine: cursor\n" in out

    def test_ollama_launch_is_the_ollama_engine_and_quota_is_not_applicable(self, make_node, r4t, capsys):
        definition = json.loads((BUNDLED / "ollama-opencode.json").read_text(encoding="utf-8"))
        make_node(definition=definition, vars={"MODEL": "qwen"})
        r4t.scenario(check={"stdout": CHECK_OK}, quota={
            "rc": 1,
            "stdout": {"origin": "unavailable", "engine": "ollama", "applicable": False, "reason": "local models"},
        })
        _code, out, _err = health(capsys, "alpha")
        assert "engine: ollama-opencode" in out and "model: qwen" in out
        assert "quota: not applicable" in out
        assert [c["argv"][1] for c in r4t.calls()] == ["ollama-opencode", "ollama-opencode"]

    def test_every_bundled_definition_names_its_engine(self, tmp_path):
        import re
        expected_native = {
            "agy": "agy", "claude": "claude", "codex": "codex", "copilot": "copilot",
            "cursor": "cursor", "devin": "devin", "muse": "muse", "opencode": "opencode",
            "ollama-opencode": "ollama-opencode",
        }
        for path in sorted(BUNDLED.glob("*.json")):
            definition = json.loads(path.read_text(encoding="utf-8"))
            found = node_health._detect_engine(definition, {"PATH": os.defpath}, tmp_path)
            stem = path.stem
            if stem.startswith("engine-"):
                want = re.sub(r"-(resident|unrestricted)", "", stem[len("engine-"):])
                assert (found.kind, found.engine) == ("engine", want), stem
            elif stem in expected_native:
                assert (found.kind, found.engine) == ("engine", expected_native[stem]), stem
            elif stem == "r4t":
                assert found.kind == "roster", stem
            else:
                assert found.kind == "none", stem


class TestLaunchSettings:
    def engine_line(self, *extra, engine="claude"):
        return ["$PYTHON", "$A8S_DIR/../r4t/r4t.py", "engine", engine, "run", *extra]

    def test_native_short_model_flag_and_codex_effort_override(self, make_node, r4t, capsys):
        make_node(
            definition={"invoke": ["codex", "exec", "-m", "$MODEL", "-c", "model_reasoning_effort=$EFFORT", "$MESSAGE"]},
            vars={"MODEL": "gpt-x", "EFFORT": "high"},
        )
        r4t.scenario(check={"stdout": CHECK_OK}, quota={"stdout": dict(LIVE, engine="codex")})
        _code, out, _err = health(capsys, "alpha")
        assert "model: gpt-x" in out and "effort: high" in out

    def test_short_model_flag_with_equals_form_and_reasoning_effort(self, make_node, r4t, capsys):
        make_node(definition={"invoke": ["copilot", "--model=sonnet", "--reasoning-effort", "low", "$MESSAGE"]})
        r4t.scenario(check={"stdout": CHECK_OK}, quota={"stdout": dict(LIVE, engine="copilot")})
        _code, out, _err = health(capsys, "alpha")
        assert "model: sonnet" in out and "effort: low" in out

    def test_an_unsupported_short_effort_flag_is_not_guessed_at(self, make_node, r4t, capsys):
        make_node(definition={"invoke": ["claude", "-e", "high", "$MESSAGE"]})
        _code, out, _err = health(capsys, "alpha")
        assert "effort: engine default" in out

    def test_lines_that_agree_show_one_value(self, make_node, r4t, capsys):
        line = self.engine_line("--model", "$MODEL", "--effort", "$EFFORT")
        make_node(definition={"invoke": line, "idle": {"invoke": line}, "batch": {"invoke": line}},
                  vars={"MODEL": "opus", "EFFORT": "high"})
        _code, out, _err = health(capsys, "alpha")
        assert "model: opus" in out and "effort: high" in out

    def test_lines_that_disagree_are_conflicting_not_a_single_value(self, make_node, r4t, capsys):
        make_node(definition={
            "invoke": self.engine_line("--model", "opus", "--effort", "high"),
            "idle": {"invoke": self.engine_line("--model", "sonnet", "--effort", "high")},
            "batch": {"invoke": self.engine_line("--effort", "low")},
        })
        _code, out, _err = health(capsys, "alpha")
        model = next(l for l in out.splitlines() if l.startswith("model:"))
        effort = next(l for l in out.splitlines() if l.startswith("effort:"))
        assert model.startswith("model: conflicting (") and "invoke: opus" in model
        assert "idle.invoke: sonnet" in model and "batch.invoke: engine default" in model
        assert effort.startswith("effort: conflicting (") and "batch.invoke: low" in effort

    def test_a_line_that_sets_and_a_line_that_does_not_conflict(self, make_node, r4t, capsys):
        make_node(definition={
            "invoke": self.engine_line("--model", "opus"),
            "batch": {"invoke": self.engine_line()},
        })
        _code, out, _err = health(capsys, "alpha")
        assert "model: conflicting (" in out and "effort: engine default" in out

    def test_a_flag_repeated_with_different_values_conflicts(self, make_node, r4t, capsys):
        make_node(definition={"invoke": self.engine_line("--model", "opus", "-m", "sonnet")})
        _code, out, _err = health(capsys, "alpha")
        assert "model: conflicting (invoke: opus / sonnet)" in out


class TestRuntimeStateIsNotAFinding:
    def test_a_stopped_node_is_not_unhealthy(self, make_node, r4t, capsys):
        make_node()
        code, out, _err = health(capsys, "alpha")
        assert code == 0
        assert "runtime: stopped" in out and "findings: none" in out

    def test_a_running_node_is_not_a_finding(self, make_node, r4t, capsys):
        make_node()
        pid_path("alpha").parent.mkdir(parents=True, exist_ok=True)
        pid_path("alpha").write_text(str(os.getpid()), encoding="utf-8")
        code, out, _err = health(capsys, "alpha")
        assert code == 0 and "findings: none" in out


# ---------- group cleanup ----------

LEAVES_A_DESCENDANT = '''\
import subprocess, sys
kid = subprocess.Popen([sys.executable, "-c", """
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(60)
"""])
open(sys.argv[1], "w", encoding="utf-8").write(str(kid.pid))
print("done")
'''

CLEANS_UP_ON_TERM = '''\
import signal, sys, time
marker = sys.argv[1]
def bye(*_):
    time.sleep(0.5)
    open(marker, "w", encoding="utf-8").write("cleaned")
    sys.exit(0)
signal.signal(signal.SIGTERM, bye)
open(marker + ".ready", "w", encoding="utf-8").write("up")
time.sleep(60)
'''


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def gone_soon(pid: int) -> bool:
    for _ in range(50):
        if not alive(pid):
            return True
        time.sleep(0.1)
    return False


posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")


@posix_only
class TestGroupCleanup:
    def run(self, tmp_path, source, *args, timeout=10, grace=0.5):
        script = tmp_path / "child.py"
        script.write_text(source, encoding="utf-8")
        return node_health._run_bounded(
            [sys.executable, str(script), *args], dict(os.environ), tmp_path, timeout, grace
        )

    def test_normal_exit_signals_only_its_own_group_never_a_bare_pid(self, tmp_path, monkeypatch):
        killed, pgids = [], []
        real_killpg = os.killpg
        monkeypatch.setattr(os, "kill", lambda *a: killed.append(a))
        monkeypatch.setattr(os, "killpg", lambda g, s: (pgids.append((g, s)), real_killpg(g, s))[1])
        spawned = []
        real_popen = node_health.subprocess.Popen

        def spy(*a, **k):
            p = real_popen(*a, **k)
            spawned.append(p.pid)
            return p

        monkeypatch.setattr(node_health.subprocess, "Popen", spy)
        rc, out, problem = self.run(tmp_path, "print('hi')")
        assert (rc, out.strip(), problem) == (0, "hi", None)
        assert killed == []
        assert pgids and {g for g, _ in pgids} == set(spawned)

    def test_an_unrelated_process_with_a_reaped_looking_pid_is_never_signalled(self, tmp_path, monkeypatch):
        bystander = node_health.subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.run(tmp_path, "pass")
            assert bystander.poll() is None
        finally:
            bystander.kill()
            bystander.wait()

    def test_descendant_left_after_a_normal_exit_is_ended_even_if_it_ignores_term(self, tmp_path):
        pidfile = tmp_path / "kid.pid"
        started = time.monotonic()
        rc, out, problem = self.run(tmp_path, LEAVES_A_DESCENDANT, str(pidfile))
        assert (rc, problem) == (0, None) and out.strip() == "done"
        assert time.monotonic() - started < 5
        assert gone_soon(int(pidfile.read_text(encoding="utf-8")))

    def test_timeout_ends_a_hung_child_within_the_bound(self, tmp_path):
        started = time.monotonic()
        rc, _out, problem = self.run(tmp_path, "import time; time.sleep(60)", timeout=0.5, grace=0.5)
        assert rc is None and problem == "timed out after 0.5s"
        assert time.monotonic() - started < 5

    def test_timeout_grace_lets_the_child_clean_up_then_returns_early(self, tmp_path):
        marker = tmp_path / "marker"
        started = time.monotonic()
        rc, _out, problem = self.run(tmp_path, CLEANS_UP_ON_TERM, str(marker), timeout=1.0, grace=3.0)
        elapsed = time.monotonic() - started
        assert rc is None and problem == "timed out after 1s"
        assert marker.read_text(encoding="utf-8") == "cleaned"
        assert elapsed < 3.0 + 1.0

    def test_a_child_that_ignores_term_is_killed_when_the_grace_ends(self, tmp_path):
        source = "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n"
        started = time.monotonic()
        rc, _out, problem = self.run(tmp_path, source, timeout=0.5, grace=0.5)
        assert rc is None and problem is not None
        assert time.monotonic() - started < 5

    def test_quota_waits_the_longer_grace_and_check_the_shorter(self, make_node, r4t, monkeypatch, capsys):
        make_node()
        graces = []
        real = node_health._run_bounded

        def spy(argv, env, cwd, timeout, grace=node_health.CHECK_TEARDOWN_GRACE_SECONDS):
            graces.append((argv[4], grace))
            return real(argv, env, cwd, timeout, grace)

        monkeypatch.setattr(node_health, "_run_bounded", spy)
        health(capsys, "alpha")
        assert graces == [("check", node_health.CHECK_TEARDOWN_GRACE_SECONDS),
                          ("quota", node_health.QUOTA_TEARDOWN_GRACE_SECONDS)]
        assert node_health.QUOTA_TEARDOWN_GRACE_SECONDS == 3.0
        assert node_health.QUOTA_TEARDOWN_GRACE_SECONDS + node_health.SETTLE_SECONDS < node_health.QUOTA_TIMEOUT_SECONDS


class FakeLeader:
    pid = 2 ** 22 + 12345  # not a child of this process
    returncode = 0

    def poll(self):
        return 0


class TestWindowsPolicy:
    def test_no_tree_kill_after_a_normal_exit(self, monkeypatch):
        calls = []
        monkeypatch.setattr(node_health, "terminate_group", lambda *a, **k: calls.append(a))
        monkeypatch.setattr(node_health.os, "name", "nt")
        monkeypatch.setattr(node_health, "_leader_exited", lambda proc: True)
        node_health._end_group(FakeLeader(), grace=3.0)
        assert calls == []

    def test_tree_kill_on_timeout(self, monkeypatch):
        calls = []
        monkeypatch.setattr(node_health, "terminate_group", lambda *a, **k: calls.append(a))
        monkeypatch.setattr(node_health.os, "name", "nt")
        monkeypatch.setattr(node_health, "_leader_exited", lambda proc: False)
        node_health._end_group(FakeLeader(), grace=3.0)
        assert len(calls) == 1

class TestForcedExitObservation:
    @staticmethod
    def fake_kqueue(monkeypatch, *, register_error=None, events=()):
        import select
        import types

        log = {"control": [], "closed": 0}

        class Queue:
            def control(self, changes, n, timeout=None):
                log["control"].append((changes, n, timeout))
                if changes and register_error:
                    raise register_error
                return [] if changes else list(events)

            def close(self):
                log["closed"] += 1

        monkeypatch.delattr(os, "waitid", raising=False)
        for name, val in (("KQ_FILTER_PROC", -5), ("KQ_EV_ADD", 1), ("KQ_EV_ONESHOT", 16),
                          ("KQ_NOTE_EXIT", 0x80000000), ("KQ_EV_ERROR", 0x4000)):
            monkeypatch.setattr(select, name, val, raising=False)
        monkeypatch.setattr(select, "kevent", lambda ident, **k: types.SimpleNamespace(ident=ident, **k), raising=False)
        monkeypatch.setattr(select, "kqueue", Queue, raising=False)
        return log

    @staticmethod
    def spawn(code="pass"):
        import subprocess
        return subprocess.Popen([sys.executable, "-c", code], start_new_session=True)

    def test_kqueue_normal_event_observes_without_reaping(self, monkeypatch):
        import types
        proc = self.spawn()
        log = self.fake_kqueue(monkeypatch, events=[
            types.SimpleNamespace(ident=proc.pid, fflags=0x80000000, flags=0)])
        assert node_health._observe_leader(proc, 5) == "exited"
        assert proc.returncode is None and log["closed"] == 1
        proc.wait()

    def test_kqueue_esrch_before_registration_means_exited(self, monkeypatch):
        proc = self.spawn()
        log = self.fake_kqueue(monkeypatch, register_error=ProcessLookupError())
        assert node_health._observe_leader(proc, 5) == "exited"
        assert proc.returncode is None and log["closed"] == 1
        proc.wait()

    def test_kqueue_timeout_is_bounded_and_registers_again(self, monkeypatch):
        proc = self.spawn("import time; time.sleep(30)")
        try:
            log = self.fake_kqueue(monkeypatch)
            monkeypatch.setattr(node_health, "KQUEUE_SLICE_SECONDS", 0.01)
            started = time.monotonic()
            assert node_health._observe_leader(proc, 0.05) == "running"
            assert time.monotonic() - started < 2
            assert len([c for c in log["control"] if c[0]]) >= 2 and log["closed"] == 1
        finally:
            proc.kill()
            proc.wait()

    def test_kqueue_exit_then_end_group_signals_the_unreaped_group(self, monkeypatch):
        proc = self.spawn()
        self.fake_kqueue(monkeypatch, register_error=ProcessLookupError())
        sent = []
        monkeypatch.setattr(node_health, "_signal_group", lambda g, s: sent.append((g, s)))
        monkeypatch.setattr(node_health, "SETTLE_SECONDS", 0)
        node_health._end_group(proc, grace=0.1)
        assert [s for _g, s in sent] == [node_health.signal.SIGTERM, node_health.signal.SIGKILL]
        assert all(g == proc.pid for g, _s in sent)
        proc.wait()

    def test_neither_api_reaped_exit_sends_no_signal(self, monkeypatch):
        import select
        monkeypatch.delattr(os, "waitid", raising=False)
        monkeypatch.delattr(select, "kqueue", raising=False)
        proc = self.spawn()
        assert node_health._observe_leader(proc, 5) == "reaped"
        monkeypatch.setattr(node_health.os, "killpg", lambda *a: pytest.fail("killpg"))
        monkeypatch.setattr(node_health.os, "kill", lambda *a: pytest.fail("kill"))
        node_health._end_group(proc, grace=0.1)

    def test_neither_api_timeout_still_ends_the_running_group(self, monkeypatch):
        import select
        monkeypatch.delattr(os, "waitid", raising=False)
        monkeypatch.delattr(select, "kqueue", raising=False)
        proc = self.spawn("import time; time.sleep(30)")
        try:
            assert node_health._observe_leader(proc, 0.1) == "running"
            node_health._end_group(proc, grace=0.1)
            assert proc.wait(timeout=5) is not None
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_foreign_reap_on_the_waitid_path_sends_no_signal(self, monkeypatch):
        if not hasattr(os, "waitid"):
            pytest.skip("no waitid")
        proc = self.spawn()
        proc.wait()
        proc.returncode = None
        monkeypatch.setattr(node_health.os, "killpg", lambda *a: pytest.fail("killpg"))
        assert node_health._observe_leader(proc, 1) == "reaped"
        proc.returncode = None
        node_health._end_group(proc, grace=0.1)


class TestStrictReadFailures:
    def test_unreadable_last_wake_is_unknown(self, make_node, r4t, monkeypatch, capsys):
        from types import SimpleNamespace
        make_node()
        def denied(**kwargs):
            raise PermissionError("blocked")
        monkeypatch.setattr(node_health, "last_wake_path", lambda _: SimpleNamespace(read_text=denied))
        code, out, _ = health(capsys, "alpha")
        assert code == 1
        assert "last wake: unknown (record unreadable)" in out
        assert "last wake: none recorded" not in out

    def test_unreadable_pid_parent_is_unknown(self, make_node, r4t, monkeypatch, capsys):
        from types import SimpleNamespace
        make_node()
        def denied():
            raise PermissionError("blocked")
        monkeypatch.setattr(node_health, "pid_path", lambda _: SimpleNamespace(parent=SimpleNamespace(stat=denied)))
        monkeypatch.setattr(node_health, "read_handler_pid_strict", lambda _: pytest.fail("must not infer stopped"))
        code, out, _ = health(capsys, "alpha")
        assert code == 1 and "runtime: unknown" in out
        assert "runtime: stopped" not in out

    def test_unreadable_dead_letter_metadata_is_unknown(self, make_node, r4t, monkeypatch, capsys):
        from types import SimpleNamespace
        make_node()
        def denied():
            raise PermissionError("blocked")
        marker = SimpleNamespace(stat=denied)
        monkeypatch.setattr(node_health, "dead_letters_dir", lambda _: SimpleNamespace(iterdir=lambda: iter([marker])))
        code, out, _ = health(capsys, "alpha")
        assert code == 1 and "dead letters (24 h): unknown" in out
        assert "dead letters (24 h): 0" not in out

    def test_account_mismatch_code_has_fixed_hint_not_provider_text(self, make_node, r4t, capsys):
        make_node()
        r4t.scenario(check={"stdout": CHECK_OK}, quota={"rc": 1, "stdout": {
            "origin": "unavailable", "engine": "claude", "applicable": True,
            "code": "account_mismatch", "reason": SENTINEL,
        }})
        code, out, err = health(capsys, "alpha")
        assert code == 1 and "account mismatch" in out
        assert SENTINEL not in out + err


class TestUnknownRemainingContract:
    def test_unknown_bucket_keeps_other_live_measurements_visible(self, make_node, r4t, capsys):
        make_node()
        payload = dict(LIVE, buckets=[
            {"label": "Chat", "remaining_fraction": None, "credits_used": None, "reset_time": None},
            {"label": "Premium Requests", "remaining_fraction": 0.42, "reset_time": None},
        ])
        r4t.scenario(check={"stdout": CHECK_OK}, quota={"stdout": payload})
        code, out, _ = health(capsys, "alpha")
        assert code == 1
        assert "Chat: remaining unknown" in out
        assert "Premium Requests: 42% remaining" in out
        assert "Chat remaining unavailable" in out
        assert "findings: none" not in out


# ---------- launch wrappers that change the environment ----------

R4T_TOKEN = "$A8S_DIR/../r4t/r4t.py"


def r4t_line(engine, *prefix):
    return [*prefix, "$PYTHON", R4T_TOKEN, "engine", engine, "run", "--agent", "$RECIPIENT"]


def native_line(*prefix, program="codex"):
    return [*prefix, program, "exec", "--model=$MODEL?"]


ACCOUNT = f"CODEX_HOME=/scratch/{SENTINEL}"
QUOTA_FOR_CODEX = dict(LIVE, engine="codex")


class TestWrappedLaunchContext:
    """A health child gets the node's composed environment. A line that changes
    it on the way to the engine cannot be reproduced, so the node is refused
    before any readiness or quota child, and no operand is ever shown."""

    REFUSED = {
        "adjacent-launcher-r4t": ({"invoke": ["firejail", "--env=" + ACCOUNT, R4T_TOKEN, "engine", "codex", "run"]}, "prefix"),
        "adjacent-wrapper-r4t": ({"invoke": ["withacct", "--", R4T_TOKEN, "engine", "codex", "run"]}, "prefix"),
        "python-command-r4t": ({"invoke": ["python3", "-c", R4T_TOKEN, "engine", "codex", "run"]}, "prefix"),
        "mixed-shell-native": ({"invoke": native_line(), "idle": {"invoke": ["sh", "-c", ACCOUNT + " codex exec"]}}, "idle.invoke"),
        "mixed-env-split": ({"invoke": native_line(), "batch": {"limit": 3, "invoke": ["env", "-S", ACCOUNT + " codex exec"]}}, "batch.invoke"),
        "mixed-env-shell": ({"invoke": native_line(), "idle": {"invoke": ["env", ACCOUNT, "sh", "-c", "codex exec"]}}, "idle.invoke"),
        "mixed-unknown-launcher": ({"invoke": native_line(), "idle": {"invoke": ["firejail", "--env=" + ACCOUNT, "codex", "exec"]}}, "idle.invoke"),
        "mixed-r4t-shell": ({"invoke": r4t_line("codex"), "idle": {"invoke": ["sh", "-c", ACCOUNT + " python3 r4t.py engine codex run"]}}, "idle.invoke"),
        "stdbuf": ({"invoke": native_line("stdbuf", "-oL")}, "stdbuf"),
        "opaque-first": ({"invoke": ["account-wrapper", "codex", "exec"], "idle": {"invoke": native_line()}}, "invoke"),
        "env-native": ({"invoke": native_line("env", ACCOUNT)}, "env"),
        "env-r4t": ({"invoke": r4t_line("codex", "env", ACCOUNT)}, "env"),
        "env-unset-native": ({"invoke": native_line("env", "-u", "CODEX_HOME")}, "env"),
        "env-unset-r4t": ({"invoke": r4t_line("codex", "env", "-u", "CODEX_HOME")}, "env"),
        "env-ignore-native": ({"invoke": native_line("env", "-i")}, "env"),
        "env-ignore-r4t": ({"invoke": r4t_line("codex", "env", "-i")}, "env"),
        "env-chdir-native": ({"invoke": native_line("env", "-C", f"/scratch/{SENTINEL}")}, "env"),
        "env-chdir-r4t": ({"invoke": r4t_line("codex", "env", "-C", f"/scratch/{SENTINEL}")}, "env"),
        "nested-native": ({"invoke": native_line("nohup", "timeout", "5", "env", ACCOUNT)}, "env"),
        "nested-r4t": ({"invoke": r4t_line("codex", "nohup", "nice", "-n", "5", "env", ACCOUNT)}, "env"),
        "sudo-native": ({"invoke": native_line("sudo", "-u", "other")}, "sudo"),
        "shell-r4t": ({"invoke": ["bash", "$PYTHON", R4T_TOKEN, "engine", "codex", "run", ACCOUNT]}, "bash"),
        "unknown-prefix-r4t": ({"invoke": r4t_line("codex", "firejail")}, "r4t"),
        "too-deep": ({"invoke": r4t_line("codex", "nohup", "nohup", "nohup", "nohup", "nohup")}, "nested wrappers"),
        "idle-wrapped": ({"invoke": r4t_line("codex"), "idle": {"invoke": r4t_line("codex", "env", ACCOUNT)}}, "idle.invoke"),
        "batch-wrapped-native": ({"invoke": native_line(), "batch": {"limit": 3, "invoke": native_line("env", ACCOUNT)}}, "batch.invoke"),
        "batch-wrapped-r4t": ({"invoke": r4t_line("codex"), "batch": {"limit": 3, "invoke": r4t_line("codex", "env", ACCOUNT)}}, "batch.invoke"),
        "direct-first-wrapped-later": ({"invoke": r4t_line("codex"), "idle": {"invoke": native_line("env", ACCOUNT)}}, "idle.invoke"),
        "wrapped-first-direct-later": ({"invoke": native_line("env", ACCOUNT), "batch": {"limit": 3, "invoke": r4t_line("codex")}}, "invoke"),
        "mixed-engines-one-wrapped": ({"invoke": r4t_line("codex"), "batch": {"limit": 3, "invoke": r4t_line("claude", "env", ACCOUNT)}}, "batch.invoke"),
    }

    @pytest.mark.parametrize("case", sorted(REFUSED))
    def test_refused_without_any_child_or_secret(self, make_node, r4t, capsys, case):
        definition, wrapper = self.REFUSED[case]
        make_node(definition=definition, vars={"env.OTHER": SENTINEL, "MODEL": "m"})
        r4t.scenario(check={"stdout": CHECK_OK}, quota={"stdout": QUOTA_FOR_CODEX})
        code, out, err = health(capsys, "alpha")
        assert code == 1
        assert r4t.calls() == []
        assert "launch context not provable" in out
        assert wrapper in out
        assert "quota: unavailable (" in out and "quota: live" not in out
        assert "readiness: unavailable" in out
        assert "engine launch context unproven" in out
        assert SENTINEL not in out + err and "/scratch/" not in out + err

    def test_detection_kind_names_the_refusal_not_a_pinned_engine(self, tmp_path):
        found = node_health._detect_engine({"invoke": r4t_line("codex", "env", ACCOUNT)}, {}, tmp_path)
        assert (found.kind, found.engine) == ("unproven", "codex")
        assert SENTINEL not in found.detail

    ALLOWED = {
        "direct-native": {"invoke": native_line(program="claude")},
        "direct-r4t": {"invoke": r4t_line("claude")},
        "neutral-native": {"invoke": native_line("timeout", "60", "nice", "-n", "5", program="claude")},
        "neutral-r4t": {"invoke": r4t_line("claude", "nohup", "setsid")},
        "interpreter-flag": {"invoke": ["python3", "-u", R4T_TOKEN, "engine", "claude", "run", "--agent", "$RECIPIENT"]},
        "all-lines-direct": {
            "invoke": r4t_line("claude"),
            "idle": {"invoke": r4t_line("claude")},
            "batch": {"limit": 3, "invoke": r4t_line("claude")},
        },
        "env-as-an-argument-only": {"invoke": r4t_line("claude") + ["--note", "env", "A=B"]},
    }

    @pytest.mark.parametrize("case", sorted(ALLOWED))
    def test_ordinary_definitions_still_query_live(self, make_node, r4t, capsys, case):
        make_node(definition=self.ALLOWED[case], vars={"MODEL": "m"})
        code, out, err = health(capsys, "alpha")
        assert (code, err) == (0, "")
        assert "quota: live" in out and "readiness: ready" in out
        assert "not provable" not in out
        assert [c["argv"][2] for c in r4t.calls()] == ["check", "quota"]

    def test_codex_direct_positive_control(self, make_node, r4t, capsys):
        make_node(definition={"invoke": native_line()}, vars={"MODEL": "m"})
        r4t.scenario(check={"stdout": CHECK_OK}, quota={"stdout": QUOTA_FOR_CODEX})
        code, out, _err = health(capsys, "alpha")
        assert code == 0 and "engine: codex\n" in out and "quota: live" in out
        assert [c["argv"][1] for c in r4t.calls()] == ["codex", "codex"]

    def test_bundled_definitions_are_not_refused(self, tmp_path):
        for path in sorted(BUNDLED.glob("*.json")):
            definition = json.loads(path.read_text(encoding="utf-8"))
            found = node_health._detect_engine(definition, {"PATH": os.defpath}, tmp_path)
            assert found.kind != "unproven", path.stem


# ---------- interrupted waits ----------

WRITES_PIDS = '''\
import os, signal, subprocess, sys, time
kid = subprocess.Popen([sys.executable, "-c", "import signal, time\\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\\ntime.sleep(120)"])
tmp = sys.argv[1] + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    f.write(f"{os.getpid()} {kid.pid}")
os.replace(tmp, sys.argv[1])
time.sleep(120)
'''

OUTER = '''\
import os, sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from pathlib import Path
import node_health
try:
    node_health._run_bounded(
        [sys.executable, sys.argv[3], sys.argv[4]], dict(os.environ), Path(sys.argv[5]), 60, 0.5
    )
except KeyboardInterrupt:
    sys.exit(42)
sys.exit(0)
'''

A8S_DIR = Path(__file__).resolve().parent.parent


def wait_for_pids(path: Path, outer=None):
    for _ in range(200):
        if path.exists():
            leader, kid = (int(x) for x in path.read_text(encoding="utf-8").split())
            return leader, kid
        if outer is not None and outer.poll() is not None:
            break
        time.sleep(0.05)
    raise AssertionError("child never reported ready")


def rescue(*pgids):
    """Only for groups this test started, and only after the assertions."""
    import signal
    for pgid in pgids:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


@posix_only
class TestInterruptedWait:
    def test_sigint_to_the_outer_process_ends_child_and_descendant(self, tmp_path):
        import signal
        import subprocess
        child = tmp_path / "child.py"
        child.write_text(WRITES_PIDS, encoding="utf-8")
        outer_py = tmp_path / "outer.py"
        outer_py.write_text(OUTER, encoding="utf-8")
        pidfile = tmp_path / "pids"
        outer = subprocess.Popen(
            [sys.executable, str(outer_py), str(A8S_DIR), str(A8S_DIR.parent.parent / "lib"),
             str(child), str(pidfile), str(tmp_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        leader = kid = None
        try:
            leader, kid = wait_for_pids(pidfile, outer)
            assert alive(leader) and alive(kid)
            assert outer.poll() is None
            os.kill(outer.pid, signal.SIGINT)
            code = outer.wait(timeout=20)
            stderr = outer.stderr.read().decode("utf-8", errors="replace")
            assert code == 42, stderr
            assert gone_soon(leader) and gone_soon(kid)
        finally:
            if outer.poll() is None:
                outer.kill()
                outer.wait()
            outer.stderr.close()
            rescue(*(p for p in (leader,) if p))

    @pytest.mark.parametrize("raised", [KeyboardInterrupt, SystemExit, RuntimeError])
    def test_every_exceptional_wait_tears_down_and_keeps_its_outcome(self, tmp_path, monkeypatch, raised):
        child = tmp_path / "child.py"
        child.write_text(WRITES_PIDS, encoding="utf-8")
        pidfile = tmp_path / "pids"
        seen = {}

        def interrupted(proc, seconds):
            seen["pids"] = wait_for_pids(pidfile)
            seen["alive_before"] = alive(seen["pids"][0]) and alive(seen["pids"][1])
            raise raised("stop")

        monkeypatch.setattr(node_health, "_await_leader", interrupted)
        try:
            with pytest.raises(raised, match="stop"):
                node_health._run_bounded([sys.executable, str(child), str(pidfile)],
                                         dict(os.environ), tmp_path, 60, 0.5)
            leader, kid = seen["pids"]
            assert seen["alive_before"]
            assert gone_soon(leader) and gone_soon(kid)
        finally:
            leader, kid = seen.get("pids", (None, None))
            rescue(*(p for p in (leader,) if p))

    def test_a_failing_teardown_does_not_replace_the_interrupt_and_still_kills(self, tmp_path, monkeypatch):
        child = tmp_path / "child.py"
        child.write_text(WRITES_PIDS, encoding="utf-8")
        pidfile = tmp_path / "pids"
        seen = {}

        def interrupted(proc, seconds):
            seen["pids"] = wait_for_pids(pidfile)
            raise KeyboardInterrupt

        def broken(proc, *, grace):
            raise OSError("teardown failed")

        monkeypatch.setattr(node_health, "_await_leader", interrupted)
        monkeypatch.setattr(node_health, "_end_group", broken)
        try:
            with pytest.raises(KeyboardInterrupt):
                node_health._run_bounded([sys.executable, str(child), str(pidfile)],
                                         dict(os.environ), tmp_path, 60, 0.5)
            leader, kid = seen["pids"]
            assert gone_soon(leader) and gone_soon(kid)
        finally:
            leader, kid = seen.get("pids", (None, None))
            rescue(*(p for p in (leader,) if p))

    def test_an_interrupt_after_the_leader_was_reaped_signals_nothing(self, tmp_path, monkeypatch):
        killed = []
        monkeypatch.setattr(os, "killpg", lambda *a: killed.append(a))
        monkeypatch.setattr(os, "kill", lambda *a: killed.append(a))

        def interrupted(proc, seconds):
            proc.wait()
            raise KeyboardInterrupt

        monkeypatch.setattr(node_health, "_await_leader", interrupted)
        with pytest.raises(KeyboardInterrupt):
            node_health._run_bounded([sys.executable, "-c", "pass"], dict(os.environ), tmp_path, 60, 0.5)
        assert killed == []


class FakeProc:
    pid = 2 ** 22 + 54321
    returncode = None

    def __init__(self, error):
        self.error = error

    def wait(self, timeout=None):
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        return 0

    def poll(self):
        return None


class TestInterruptedWaitPortability:
    @pytest.mark.parametrize("exited", [True, False])
    def test_without_process_groups_only_a_running_leader_is_ended(self, tmp_path, monkeypatch, exited):
        calls = []
        monkeypatch.setattr(node_health, "terminate_group", lambda *a, **k: calls.append(a))
        monkeypatch.setattr(node_health, "launch_options", lambda **k: {})
        monkeypatch.setattr(node_health.subprocess, "Popen", lambda *a, **k: FakeProc(KeyboardInterrupt()))
        monkeypatch.setattr(node_health, "_leader_exited", lambda proc: exited)
        monkeypatch.setattr(node_health.os, "name", "nt")
        with pytest.raises(KeyboardInterrupt):
            node_health._run_bounded(["x"], {}, tmp_path, 5, 0.5)
        assert len(calls) == (0 if exited else 1)
