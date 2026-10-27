"""The resident session on codex: `codex app-server` behind the bridge (#344).

The fake server replays frames recorded from codex-cli 0.163.0
(fixtures/codex-app-server/, redacted). Unit tests feed the same frames to
`CodexSession` directly."""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from engines import check as engine_check
from engines import resident as resident_mod
from engines import run as engine_run
from engines.run import RunError, _build_argv_template
from ar3.proc import terminate_group
from r4t import main as r4t_main

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "codex-app-server"
FAKE_SERVER = FIXTURES / "fake_app_server.py"


def _frames(name: str) -> list[dict]:
    out = []
    for line in (FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if isinstance(record.get("line"), dict):
            out.append(record)
    return out


def _out(name: str, pred) -> dict:
    return next(r["line"] for r in _frames(name) if r["dir"] == "out" and pred(r["line"]))


def _template(permissions=None, model=None, effort=None) -> list[str]:
    template, _ = _build_argv_template(
        "codex", model=model, effort=effort, timeout=900, workdir=Path("/tmp"),
        permissions=permissions, resident=True,
    )
    return template


def _rpc(line: str) -> dict:
    return json.loads(line)


class TestComposeCodexArgv:
    def test_restricted_template_becomes_app_server_with_config_keys(self):
        argv = resident_mod.compose_resident_argv(
            _template(model="gpt-x", effort="low"), {"TELL_OUTBOX_DIR": "/tmp/out"},
            engine="codex",
        )
        assert argv[:2] == ["codex", "app-server"]
        assert "model_reasoning_effort=low" in argv
        assert 'model="gpt-x"' in argv
        assert 'sandbox_mode="workspace-write"' in argv
        assert 'approval_policy="never"' in argv
        override = next(a for a in argv if a.startswith("mcp_servers.a8s="))
        assert 'default_tools_approval_mode = "approve"' in override
        assert 'TELL_OUTBOX_DIR = "/tmp/out"' in override
        # app-server parses these and then ignores them: none may survive.
        for ignored in ("exec", "-m", "-s", "--sandbox", "-a",
                        resident_mod.CODEX_BYPASS_FLAG, "--skip-git-repo-check", "{prompt}"):
            assert ignored not in argv

    def test_bypass_is_danger_full_access_with_approvals_never(self):
        argv = resident_mod.compose_resident_argv(_template("bypass"), {}, engine="codex")
        assert 'sandbox_mode="danger-full-access"' in argv
        assert 'approval_policy="never"' in argv
        assert resident_mod.CODEX_BYPASS_FLAG not in argv
        assert resident_mod.codex_sandbox_intent(argv) == "danger-full-access"

    def test_mcp_log_rides_the_server_env(self, tmp_path):
        argv = resident_mod.compose_resident_argv(
            _template(), {}, engine="codex", mcp_log_path=tmp_path / "mcp.jsonl"
        )
        assert f'A8S_MCP_LOG = "{tmp_path / "mcp.jsonl"}"' in argv[-1]

    def test_a_token_app_server_cannot_carry_is_refused(self):
        with pytest.raises(RunError, match="--json"):
            resident_mod.compose_resident_argv(
                ["codex", "exec", "--json", "{prompt}"], {}, engine="codex"
            )

    def test_codex_is_a_resident_engine_with_the_mcp_tell_step(self):
        assert "codex" in resident_mod.RESIDENT_ENGINES
        adapter = resident_mod.RESIDENT_ADAPTERS["codex"]
        assert adapter.tell_step == engine_run.CODEX_MCP_TELL_STEP
        assert "`mcp__a8s__tell`" in adapter.tell_step
        assert adapter.merge_stderr is False

    def test_resident_with_continue_is_still_refused(self, tmp_path):
        with pytest.raises(RunError, match="--resident cannot be combined"):
            _build_argv_template(
                "codex", model=None, timeout=900, workdir=tmp_path,
                continue_conversation=True, resident=True,
            )


class TestCodexSession:
    """The session against the recorded frames, no process."""

    def _ready(self, tmp_path, sandbox="workspace-write"):
        argv = ["codex", "app-server", "-c", f'sandbox_mode="{sandbox}"']
        sess = resident_mod.CodexSession(argv, tmp_path)
        opening = [_rpc(x) for x in sess.opening_lines()]
        assert [m.get("method") for m in opening] == ["initialize", "initialized", "thread/start"]
        assert opening[2]["params"]["cwd"] == str(tmp_path)
        sess.on_frame(_out("pingpong", lambda m: m.get("id") == 1))
        return sess

    def _thread_answer(self, policy="never", sandbox="workspaceWrite"):
        answer = json.loads(json.dumps(_out("pingpong", lambda m: m.get("id") == 2)))
        answer["result"]["approvalPolicy"] = policy
        answer["result"]["sandbox"] = {"type": sandbox}
        return answer

    def test_initialize_answer_carries_the_version(self, tmp_path):
        sess = self._ready(tmp_path)
        parsed = sess.on_frame(_out("pingpong", lambda m: m.get("id") == 1))
        assert parsed == resident_mod.ParsedFrame()
        assert sess.version == "0.163.0"
        assert resident_mod.codex_version("r4t/0.170.2 (Linux)") == "0.170.2"
        assert resident_mod.codex_version("garbage") is None

    def test_a_turn_before_the_thread_is_held_then_sent(self, tmp_path):
        sess = self._ready(tmp_path)
        assert sess.turn_lines("first") == []
        answer = self._thread_answer()
        parsed = sess.on_frame(answer)
        assert not parsed.fatal
        (line,) = parsed.replies
        turn = _rpc(line)
        assert turn["method"] == "turn/start"
        assert turn["params"]["threadId"] == answer["result"]["thread"]["id"]
        assert turn["params"]["input"] == [{"type": "text", "text": "first"}]

    def test_two_turns_one_thread_one_on_the_wire(self, tmp_path):
        sess = self._ready(tmp_path)
        answer = self._thread_answer()
        tid = answer["result"]["thread"]["id"]
        sess.on_frame(answer)
        (first,) = sess.turn_lines("one")
        # Mid-turn: held, never a second turn/start the server would merge.
        assert sess.turn_lines("two") == []
        item = json.loads(json.dumps(_out("pingpong", lambda m: m.get("method") == "item/completed"
                                          and m["params"]["item"]["type"] == "agentMessage")))
        item["params"]["threadId"] = tid
        assert sess.on_frame(item).assistant_text == "PONG"
        done = json.loads(json.dumps(_out("pingpong", lambda m: m.get("method") == "turn/completed")))
        done["params"]["threadId"] = tid
        parsed = sess.on_frame(done)
        assert parsed.turn_completed and parsed.result_text == "PONG"
        (second,) = parsed.replies
        assert _rpc(second)["params"]["input"][0]["text"] == "two"
        assert _rpc(second)["id"] != _rpc(first)["id"]

    def test_frames_of_another_thread_are_ignored(self, tmp_path):
        sess = self._ready(tmp_path)
        answer = self._thread_answer()
        answer["result"]["thread"]["id"] = "another-thread"
        sess.on_frame(answer)
        done = _out("pingpong", lambda m: m.get("method") == "turn/completed")
        assert sess.on_frame(done) == resident_mod.ParsedFrame()

    @pytest.mark.parametrize(
        ("policy", "sandbox"),
        (("on-request", "workspaceWrite"), ("never", "readOnly")),
    )
    def test_a_policy_other_than_the_composed_one_is_fatal(self, tmp_path, policy, sandbox):
        sess = self._ready(tmp_path)
        parsed = sess.on_frame(self._thread_answer(policy=policy, sandbox=sandbox))
        assert parsed.fatal
        assert "thread/start reports" in parsed.error_text
        assert sess.thread_id is None

    def test_a_failed_handshake_is_fatal(self, tmp_path):
        sess = self._ready(tmp_path)
        assert sess.on_frame({"id": 1, "error": {"code": -1, "message": "no"}}).fatal
        assert sess.on_frame({"id": 2, "error": {"code": -1, "message": "no"}}).fatal

    @pytest.mark.parametrize("first", [1, 2])
    @pytest.mark.parametrize("poll_first", [False, True])
    def test_handshake_requires_both_replies_with_one_total_deadline(
        self, monkeypatch, tmp_path, first, poll_first,
    ):
        clock = [100.0]
        monkeypatch.setattr(resident_mod.time, "monotonic", lambda: clock[0])
        sess = resident_mod.CodexSession(
            ["codex", "app-server", "-c", 'sandbox_mode="workspace-write"'], tmp_path,
        )
        replies = {1: _out("pingpong", lambda m: m.get("id") == 1),
                   2: self._thread_answer()}
        assert sess.turn_lines("held") == []
        clock[0] = 119.0
        assert sess.on_frame(replies[first]).replies == ()
        assert not sess.handshake_done and sess.deadline_error() is None
        clock[0] = 120.0
        if poll_first:
            assert sess.deadline_error() == "codex handshake was not ready after 20s"
        assert list(sess.held) == ["held"]
        parsed = sess.on_frame(replies[3 - first])
        assert parsed == resident_mod.ParsedFrame()
        assert not sess.handshake_done and sess.handshake_expired
        assert sess.turn_lines("late") == []
        assert sess._release() == ()
        assert list(sess.held) == ["held", "late"]
        clock[0] = 1000.0
        assert sess.deadline_error() == "codex handshake was not ready after 20s"

    def test_completed_handshake_disarms_deadline(self, monkeypatch, tmp_path):
        sess = self._ready(tmp_path)
        sess.on_frame(self._thread_answer())
        assert sess.handshake_done
        monkeypatch.setattr(resident_mod.time, "monotonic", lambda: sess.started + 1000)
        assert sess.deadline_error() is None
        assert len(sess.turn_lines("later turn")) == 1

    def test_an_approval_request_is_declined_with_its_own_id(self, tmp_path):
        sess = self._ready(tmp_path)
        ask = _out("approval", lambda m: m.get("method") == "item/commandExecution/requestApproval")
        parsed = sess.on_frame(ask)
        (reply,) = parsed.replies
        assert _rpc(reply) == {"jsonrpc": "2.0", "id": ask["id"], "result": {"decision": "decline"}}
        assert "refused" in parsed.error_text

    def test_an_unknown_server_request_gets_a_jsonrpc_error(self, tmp_path):
        sess = self._ready(tmp_path)
        (reply,) = sess.on_frame({"id": 7, "method": "attestation/generate", "params": {}}).replies
        assert _rpc(reply)["error"]["code"] == -32601

    def test_a_failed_turn_start_completes_the_turn_with_its_error(self, tmp_path):
        sess = self._ready(tmp_path)
        sess.on_frame(self._thread_answer())
        (line,) = sess.turn_lines("one")
        sess.turn_lines("two")
        error = dict(_out("turn-error", lambda m: "error" in m))
        error["id"] = _rpc(line)["id"]
        parsed = sess.on_frame(error)
        assert parsed.turn_completed
        assert "thread not found" in parsed.error_text
        assert _rpc(parsed.replies[0])["params"]["input"][0]["text"] == "two"

    def test_an_interrupted_turn_reports_its_status(self, tmp_path):
        sess = self._ready(tmp_path)
        answer = self._thread_answer()
        sess.on_frame(answer)
        done = json.loads(json.dumps(_out("stop", lambda m: m.get("method") == "turn/completed")))
        done["params"]["threadId"] = answer["result"]["thread"]["id"]
        parsed = sess.on_frame(done)
        assert parsed.turn_completed
        assert parsed.error_text == "turn interrupted"


def _fake_codex(tmp_path: Path) -> Path:
    path = tmp_path / "fake-codex"
    path.write_text(
        f"#!{sys.executable}\nimport runpy, sys\n"
        f"sys.argv[0] = {str(FAKE_SERVER)!r}\n"
        f"runpy.run_path({str(FAKE_SERVER)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _patch_codex(monkeypatch, tmp_path) -> Path:
    fake = _fake_codex(tmp_path)
    preset = engine_run.HARNESS_PRESETS["codex"]
    monkeypatch.setitem(
        engine_run.HARNESS_PRESETS, "codex",
        {**preset, "invoke": [str(fake), *preset["invoke"][1:]]},
    )
    record = tmp_path / "codex-record.json"
    monkeypatch.setenv("FAKE_CODEX_RECORD", str(record))
    return record


def _rec(record: Path) -> dict:
    if not record.is_file():
        return {}
    try:
        return json.loads(record.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def _wait_until(predicate, timeout: float = 10.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met before timeout")


def _stop(workdir: Path, agent: str) -> None:
    pid = resident_mod.read_live_pid(resident_mod.resident_home(workdir, agent))
    if pid is not None:
        terminate_group(pid)


def _run(workdir: Path, message: str, *extra: str) -> int:
    return r4t_main([
        "engine", "codex", "run", "--resident", "--dir", str(workdir),
        "--agent", "ares", *extra, message,
    ])


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
class TestCodexResidentLifecycle:
    def test_two_messages_one_process_one_thread_then_wrap_up(self, monkeypatch, tmp_path):
        record = _patch_codex(monkeypatch, tmp_path)
        monkeypatch.setenv("FAKE_CODEX_TURN_SECONDS", "0.5")
        workdir = tmp_path / "seat"
        workdir.mkdir()
        home = resident_mod.resident_home(workdir, "ares")
        try:
            assert _run(workdir, "first from alice", "--timeout", "2") == 0
            _wait_until(lambda: len(_rec(record).get("turns", [])) >= 1)
            assert _run(workdir, "second from bob", "--timeout", "2") == 0
            _wait_until(
                lambda: resident_mod.read_live_pid(home) is None
                and len(_rec(record).get("turns", [])) >= 3,
                timeout=15.0,
            )
            rec = _rec(record)
            assert rec["argv"][0] == "app-server"
            assert 'approval_policy="never"' in rec["argv"]
            methods = [m.get("method") for m in rec["in"]]
            assert methods[:3] == ["initialize", "initialized", "thread/start"]
            assert methods.count("thread/start") == 1
            turns = rec["turns"]
            assert len(turns) == 3
            assert "first from alice" in turns[0] and "mcp__a8s__tell" in turns[0]
            assert "second from bob" in turns[1]
            assert "wrap up and exit" in turns[2]
            # One turn on the wire at a time: the server never had to merge.
            assert rec["steered"] == 0
            assert "Wrapped up cleanly." in (workdir / "STATUS.md").read_text(encoding="utf-8")
            log = (home / "resident.log").read_text(encoding="utf-8")
            assert "codex app-server 0.163.0" in log
        finally:
            _stop(workdir, "ares")

    def test_messages_mid_turn_wait_and_the_count_returns_to_zero(self, monkeypatch, tmp_path):
        record = _patch_codex(monkeypatch, tmp_path)
        monkeypatch.setenv("FAKE_CODEX_TURN_SECONDS", "1.5")
        workdir = tmp_path / "seat"
        workdir.mkdir()
        home = resident_mod.resident_home(workdir, "ares")
        try:
            assert _run(workdir, "one", "--timeout", "30") == 0
            _wait_until(lambda: len(_rec(record).get("turns", [])) >= 1)
            assert _run(workdir, "two", "--timeout", "30") == 0
            _wait_until(lambda: (resident_mod.read_state(home) or {}).get("turns") == 2)
            assert resident_mod.status_resident(workdir, "ares") == "busy"
            _wait_until(lambda: (resident_mod.read_state(home) or {}).get("turns") == 0,
                        timeout=15.0)
            assert resident_mod.status_resident(workdir, "ares") == "resident"
            rec = _rec(record)
            assert rec["steered"] == 0
            assert [m.get("method") for m in rec["in"]].count("turn/start") == 2
        finally:
            _stop(workdir, "ares")

    def test_a_wrong_effective_policy_ends_the_session_before_any_turn(
        self, monkeypatch, tmp_path
    ):
        record = _patch_codex(monkeypatch, tmp_path)
        monkeypatch.setenv("FAKE_CODEX_POLICY", "on-request")
        workdir = tmp_path / "seat"
        workdir.mkdir()
        home = resident_mod.resident_home(workdir, "ares")
        try:
            assert _run(workdir, "hello", "--timeout", "30") == 0
            _wait_until(lambda: resident_mod.read_live_pid(home) is None, timeout=15.0)
            assert _rec(record).get("turns") == []
            log = (home / "resident.log").read_text(encoding="utf-8")
            assert "approvalPolicy 'on-request', not 'never'" in log
        finally:
            _stop(workdir, "ares")

    def test_an_approval_request_is_declined_over_the_pipe(self, monkeypatch, tmp_path):
        record = _patch_codex(monkeypatch, tmp_path)
        monkeypatch.setenv("FAKE_CODEX_APPROVAL", "1")
        workdir = tmp_path / "seat"
        workdir.mkdir()
        try:
            assert _run(workdir, "hello", "--timeout", "30") == 0
            _wait_until(lambda: _rec(record).get("replies"))
            (reply,) = _rec(record)["replies"]
            assert reply["result"] == {"decision": "decline"}
            assert reply["id"] == 0
        finally:
            _stop(workdir, "ares")

    def test_bypass_prints_the_resident_spelling_not_the_exec_flag(
        self, monkeypatch, tmp_path, capsys
    ):
        _patch_codex(monkeypatch, tmp_path)
        workdir = tmp_path / "seat"
        workdir.mkdir()
        try:
            assert _run(workdir, "hello", "--timeout", "30", "--permissions", "bypass") == 0
            err = capsys.readouterr().err
            assert 'sandbox_mode="danger-full-access"' in err
            assert 'approval_policy="never"' in err
            assert resident_mod.CODEX_BYPASS_FLAG not in err
        finally:
            _stop(workdir, "ares")

    def test_stop_ends_the_session(self, monkeypatch, tmp_path, capsys):
        record = _patch_codex(monkeypatch, tmp_path)
        monkeypatch.setenv("FAKE_CODEX_TURN_SECONDS", "0.05")
        workdir = tmp_path / "seat"
        workdir.mkdir()
        home = resident_mod.resident_home(workdir, "ares")
        try:
            assert _run(workdir, "hello", "--timeout", "60") == 0
            _wait_until(lambda: (resident_mod.read_state(home) or {}).get("turns") == 0
                        and _rec(record).get("turns"))
            engine_pid = _rec(record)["pid"]
            assert r4t_main(["engine", "codex", "stop", "--dir", str(workdir),
                             "--agent", "ares"]) == 0
            assert "stopped" in capsys.readouterr().out
            assert resident_mod.read_live_pid(home) is None
            _wait_until(lambda: not resident_mod.pid_alive(engine_pid))
        finally:
            _stop(workdir, "ares")


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
class TestCodexResidentCheck:
    def _check(self, monkeypatch, tmp_path, **env):
        _patch_codex(monkeypatch, tmp_path)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return engine_check.check_engine("codex", resident=True, workdir=tmp_path)

    def test_the_handshake_reports_the_proven_version(self, monkeypatch, tmp_path):
        report = self._check(monkeypatch, tmp_path)
        assert report.verdict == engine_check.ACCEPTED
        assert report.method == "app-server handshake"
        assert report.version == "app-server 0.163.0"
        assert report.detail == ""

    def test_another_version_is_accepted_with_a_note(self, monkeypatch, tmp_path):
        report = self._check(monkeypatch, tmp_path, FAKE_CODEX_VERSION="0.170.0")
        assert report.verdict == engine_check.ACCEPTED
        assert "0.170.0 is not the proven 0.163.0" in report.detail

    def test_a_wrong_effective_policy_is_rejected(self, monkeypatch, tmp_path):
        report = self._check(monkeypatch, tmp_path, FAKE_CODEX_POLICY="on-request")
        assert report.verdict == engine_check.REJECTED
        assert "approvalPolicy 'on-request'" in report.detail

    def test_the_handshake_is_ephemeral_and_strict(self, monkeypatch, tmp_path):
        record = tmp_path / "codex-record.json"
        self._check(monkeypatch, tmp_path)
        rec = _rec(record)
        assert "--strict-config" in rec["argv"]
        thread = next(m for m in rec["in"] if m.get("method") == "thread/start")
        assert thread["params"]["ephemeral"] is True
        assert not any(m.get("method") == "turn/start" for m in rec["in"])

    def test_a_non_resident_check_keeps_the_parse_probe(self, monkeypatch, tmp_path):
        _patch_codex(monkeypatch, tmp_path)
        report = engine_check.check_engine("codex", workdir=tmp_path)
        assert report.method == "parse probe"


@pytest.mark.skipif(os.name != "posix", reason="process signals are POSIX")
def test_a_stop_request_signals_a_busy_codex_at_once(monkeypatch, tmp_path):
    import time

    record = _patch_codex(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_CODEX_TURN_SECONDS", "30")
    monkeypatch.setenv("FAKE_CODEX_HOLD_AFTER_EOF", "30")
    monkeypatch.setenv("FAKE_CODEX_RECORD_SIGTERM", "1")
    workdir = tmp_path / "seat"
    workdir.mkdir()
    home = resident_mod.resident_home(workdir, "ares")
    try:
        assert _run(workdir, "hello", "--timeout", "60") == 0
        _wait_until(lambda: _rec(record).get("turns"))
        started = time.time()
        assert resident_mod.stop_resident(workdir, "ares") is True
        _wait_until(lambda: _rec(record).get("sigterm_at"), timeout=3)
        rec = _rec(record)
        assert rec["sigterm_at"] - started < 1.0
    finally:
        _stop(workdir, "ares")


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_an_exit_after_eof_records_a_nonzero_memory_exit(monkeypatch, tmp_path):
    import threading

    record = _patch_codex(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_CODEX_TURN_SECONDS", "0.05")
    monkeypatch.setenv("FAKE_CODEX_EXIT_CODE", "3")
    workdir = tmp_path / "seat"
    workdir.mkdir()
    mem = tmp_path / "mem"
    home = resident_mod.resident_home(workdir, "ares")
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "hello")
    spec = {
        "engine": "codex",
        "dir_path": str(workdir),
        "template": _template(),
        "agent": "ares",
        "timeout": 1,
        "scaffold": False,
        "memory": "on",
        "memory_home": str(mem),
    }
    resident_mod._run_session(home, spec, threading.Event(), {})
    turns = list(mem.rglob("turns/*.json"))
    assert turns
    assert json.loads(turns[0].read_text(encoding="utf-8")).get("exit") not in (None, 0)
    assert _rec(record).get("turns")


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_an_outside_sigkill_after_eof_records_a_nonzero_memory_exit(
    monkeypatch, tmp_path,
):
    """A negative code counts when the bridge sent no signal. An outside
    SIGKILL after EOF is the engine's own failure, the way exit 3 is."""
    import signal
    import threading

    record = _patch_codex(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_CODEX_TURN_SECONDS", "0.05")
    monkeypatch.setenv("FAKE_CODEX_HOLD_AFTER_EOF", "30")
    workdir = tmp_path / "seat"
    workdir.mkdir()
    mem = tmp_path / "mem"
    home = resident_mod.resident_home(workdir, "ares")
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "hello")
    spec = {
        "engine": "codex",
        "dir_path": str(workdir),
        "template": _template(),
        "agent": "ares",
        "timeout": 1,
        "scaffold": False,
        "memory": "on",
        "memory_home": str(mem),
    }
    done = threading.Thread(
        target=resident_mod._run_session,
        args=(home, spec, threading.Event(), {}),
        daemon=True,
    )
    done.start()
    try:
        _wait_until(lambda: _rec(record).get("eof_at"), timeout=15)
        engine_pid = (resident_mod.read_state(home) or {}).get("engine_pid")
        assert isinstance(engine_pid, int)
        os.kill(engine_pid, signal.SIGKILL)
        done.join(10)
    finally:
        if done.is_alive():
            state = resident_mod.read_state(home) or {}
            pid = state.get("engine_pid")
            if isinstance(pid, int):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            done.join(3)
    assert not done.is_alive()
    turns = list(mem.rglob("turns/*.json"))
    assert turns
    assert json.loads(turns[0].read_text(encoding="utf-8")).get("exit") not in (None, 0)


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_a_quiet_expiry_waits_out_the_eof_grace(monkeypatch, tmp_path, capsys):
    """codex keeps the 10 s EOF grace when the bridge ends the session.
    An engine that exits 5 s after EOF is not signaled."""
    import threading
    import time

    record = _patch_codex(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_CODEX_TURN_SECONDS", "0.05")
    monkeypatch.setenv("FAKE_CODEX_HOLD_AFTER_EOF", "5")
    monkeypatch.setenv("FAKE_CODEX_RECORD_SIGTERM", "1")
    workdir = tmp_path / "seat"
    workdir.mkdir()
    home = resident_mod.resident_home(workdir, "ares")
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "hello")
    spec = {
        "engine": "codex",
        "dir_path": str(workdir),
        "template": _template(),
        "agent": "ares",
        "timeout": 1,
        "scaffold": False,
        "memory": "off",
    }
    started = time.monotonic()
    resident_mod._run_session(home, spec, threading.Event(), {})
    elapsed = time.monotonic() - started
    rec = _rec(record)
    out = capsys.readouterr().out
    assert rec.get("eof_at")
    assert "sigterm_at" not in rec
    assert "resident session exited (code 0)" in out
    assert 5.5 < elapsed < 12


@pytest.mark.skipif(os.name != "posix", reason="process signals are POSIX")
def test_a_stop_during_the_eof_grace_signals_at_once(monkeypatch, tmp_path):
    """A stop that arrives while codex is in its quiet EOF grace ends the
    grace. SIGTERM follows at once, the way a stop that was already set does."""
    import time

    record = _patch_codex(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_CODEX_TURN_SECONDS", "0.05")
    monkeypatch.setenv("FAKE_CODEX_HOLD_AFTER_EOF", "30")
    monkeypatch.setenv("FAKE_CODEX_RECORD_SIGTERM", "1")
    workdir = tmp_path / "seat"
    workdir.mkdir()
    try:
        assert resident_mod.enqueue_or_start(
            "codex", "hello", dir_path=workdir, template=_template(),
            model=None, effort=None, agent="ares", timeout=1,
            scaffold=False,
        ) == 0
        _wait_until(lambda: _rec(record).get("eof_at"), timeout=15)
        started = time.time()
        assert resident_mod.stop_resident(workdir, "ares") is True
        _wait_until(lambda: _rec(record).get("sigterm_at"), timeout=3)
        rec = _rec(record)
        assert rec["sigterm_at"] - started < 1.0
    finally:
        _stop(workdir, "ares")


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_an_unfinished_wrap_up_drops_the_turn(monkeypatch, tmp_path, capsys):
    import threading

    _patch_codex(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_CODEX_TURN_SECONDS", "0.05")
    monkeypatch.setenv("FAKE_CODEX_HANG_WRAPUP", "1")
    monkeypatch.setattr(resident_mod, "WRAPUP_TURN_SECONDS", 0.4)
    workdir = tmp_path / "seat"
    workdir.mkdir()
    mem = tmp_path / "mem"
    home = resident_mod.resident_home(workdir, "ares")
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "hello")
    spec = {
        "engine": "codex",
        "dir_path": str(workdir),
        "template": _template(),
        "agent": "ares",
        "timeout": 1,
        "scaffold": True,
        "memory": "on",
        "memory_home": str(mem),
    }
    resident_mod._run_session(home, spec, threading.Event(), {})
    out = capsys.readouterr().out
    turns = list(mem.rglob("turns/*.json"))
    assert turns
    assert json.loads(turns[0].read_text(encoding="utf-8")).get("exit") not in (None, 0)
    assert (resident_mod.read_state(home) or {}).get("turns", 0) == 0
    assert out.count("dropped ") == 1
    assert "dropped 1 held message(s): deadline" in out


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_a_missed_wrap_up_deadline_signals_at_once(monkeypatch, tmp_path, capsys):
    """A wrap-up whose clock runs out is SIGTERM at once. The 10 s EOF grace
    does not apply. EOF_GRACE_SECONDS is left at 10 so a grace would fail.
    The fake stays up after EOF, so the signal hits the handler rather than
    interpreter shutdown."""
    import threading
    import time

    record = _patch_codex(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_CODEX_TURN_SECONDS", "0.05")
    monkeypatch.setenv("FAKE_CODEX_HANG_WRAPUP", "1")
    monkeypatch.setenv("FAKE_CODEX_RECORD_SIGTERM", "1")
    monkeypatch.setenv("FAKE_CODEX_HOLD_AFTER_EOF", "30")
    monkeypatch.setattr(resident_mod, "WRAPUP_TURN_SECONDS", 0.3)
    workdir = tmp_path / "seat"
    workdir.mkdir()
    home = resident_mod.resident_home(workdir, "ares")
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "hello")
    spec = {
        "engine": "codex",
        "dir_path": str(workdir),
        "template": _template(),
        "agent": "ares",
        "timeout": 1,
        "scaffold": True,
        "memory": "off",
    }
    started = time.monotonic()
    resident_mod._run_session(home, spec, threading.Event(), {})
    elapsed = time.monotonic() - started
    rec = _rec(record)
    assert rec.get("sigterm_at")
    assert "dropped 1 held message(s): deadline" in capsys.readouterr().out
    # Quiet window 1 s, wrap-up deadline 0.3 s. A 10 s grace lands near 11 s.
    assert elapsed < 4


def _wrap_up_session(monkeypatch, tmp_path, flag: str):
    import threading

    _patch_codex(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_CODEX_TURN_SECONDS", "0.05")
    monkeypatch.setenv(flag, "1")
    monkeypatch.setattr(resident_mod, "EOF_GRACE_SECONDS", 0.2)
    workdir = tmp_path / "seat"
    workdir.mkdir()
    mem = tmp_path / "mem"
    home = resident_mod.resident_home(workdir, "ares")
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "hello")
    spec = {
        "engine": "codex",
        "dir_path": str(workdir),
        "template": _template(),
        "agent": "ares",
        "timeout": 1,
        "scaffold": True,
        "memory": "on",
        "memory_home": str(mem),
    }
    resident_mod._run_session(home, spec, threading.Event(), {})
    return home, mem


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_a_fatal_during_the_wrap_up_names_fatal(monkeypatch, tmp_path, capsys):
    home, mem = _wrap_up_session(monkeypatch, tmp_path, "FAKE_CODEX_FATAL_WRAPUP")
    out = capsys.readouterr().out
    assert "dropped 1 held message(s): fatal" in out
    assert "deadline" not in out
    assert (resident_mod.read_state(home) or {}).get("turns", 0) == 0
    turns = list(mem.rglob("turns/*.json"))
    assert turns
    assert json.loads(turns[0].read_text(encoding="utf-8")).get("exit") not in (None, 0)


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_an_engine_exit_during_the_wrap_up_names_engine_exit(
    monkeypatch, tmp_path, capsys,
):
    home, mem = _wrap_up_session(monkeypatch, tmp_path, "FAKE_CODEX_EXIT_WRAPUP")
    out = capsys.readouterr().out
    assert "dropped 1 held message(s): engine exit" in out
    assert "deadline" not in out
    assert (resident_mod.read_state(home) or {}).get("turns", 0) == 0
    turns = list(mem.rglob("turns/*.json"))
    assert turns
    assert json.loads(turns[0].read_text(encoding="utf-8")).get("exit") not in (None, 0)


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_a_bare_json_value_on_codex_stdout_is_fatal(monkeypatch, tmp_path, capsys):
    import threading

    _patch_codex(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_CODEX_TURN_SECONDS", "0.05")
    monkeypatch.setenv("FAKE_CODEX_BARE_JSON", "1")
    monkeypatch.setattr(resident_mod, "EOF_GRACE_SECONDS", 0.2)
    workdir = tmp_path / "seat"
    workdir.mkdir()
    home = resident_mod.resident_home(workdir, "ares")
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "hello")
    spec = {
        "engine": "codex",
        "dir_path": str(workdir),
        "template": _template(),
        "agent": "ares",
        "timeout": 1,
        "scaffold": False,
        "memory": "off",
    }
    resident_mod._run_session(home, spec, threading.Event(), {})
    out = capsys.readouterr().out
    assert "resident engine sent a frame that was not an object; ending it" in out
    assert "dropped 1 held message(s): fatal" in out
    assert (resident_mod.read_state(home) or {}).get("turns", 0) == 0


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_a_codex_fatal_signals_without_the_eof_grace(monkeypatch, tmp_path):
    """The fake stays up after EOF. A fatal must SIGTERM at once, not after
    the 10 s grace."""
    import threading
    import time

    record = _patch_codex(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_CODEX_TURN_SECONDS", "0.05")
    monkeypatch.setenv("FAKE_CODEX_BARE_JSON", "1")
    monkeypatch.setenv("FAKE_CODEX_HOLD_AFTER_EOF", "30")
    monkeypatch.setenv("FAKE_CODEX_RECORD_SIGTERM", "1")
    workdir = tmp_path / "seat"
    workdir.mkdir()
    home = resident_mod.resident_home(workdir, "ares")
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "hello")
    spec = {
        "engine": "codex",
        "dir_path": str(workdir),
        "template": _template(),
        "agent": "ares",
        "timeout": 1,
        "scaffold": False,
        "memory": "off",
    }
    started = time.monotonic()
    resident_mod._run_session(home, spec, threading.Event(), {})
    assert time.monotonic() - started < 3
    assert _rec(record).get("sigterm_at")


def _fatal_during_the_grace(monkeypatch, tmp_path, *, windows: bool) -> dict:
    import threading

    if windows:
        monkeypatch.setattr(resident_mod, "_use_windows_stop", lambda: True)
    record = _patch_codex(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_CODEX_TURN_SECONDS", "0.05")
    monkeypatch.setenv("FAKE_CODEX_FATAL_AFTER_EOF", "1")
    monkeypatch.setenv("FAKE_CODEX_HOLD_AFTER_EOF", "30")
    monkeypatch.setenv("FAKE_CODEX_RECORD_SIGTERM", "1")
    workdir = tmp_path / "seat"
    workdir.mkdir()
    home = resident_mod.resident_home(workdir, "ares")
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "hello")
    spec = {
        "engine": "codex",
        "dir_path": str(workdir),
        "template": _template(),
        "agent": "ares",
        "timeout": 1,
        "scaffold": False,
        "memory": "off",
    }
    resident_mod._run_session(home, spec, threading.Event(), {})
    return _rec(record)


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_a_fatal_during_the_eof_grace_signals_at_once(monkeypatch, tmp_path):
    """Quiet expiry starts the grace. A fatal during it must not wait the
    rest of the 10 s out."""
    rec = _fatal_during_the_grace(monkeypatch, tmp_path, windows=False)
    assert rec.get("fatal_at") and rec.get("sigterm_at")
    assert rec["sigterm_at"] - rec["fatal_at"] < 1


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_a_windows_fatal_during_the_eof_grace_signals_at_once(monkeypatch, tmp_path):
    rec = _fatal_during_the_grace(monkeypatch, tmp_path, windows=True)
    assert rec.get("fatal_at") and rec.get("sigterm_at")
    assert rec["sigterm_at"] - rec["fatal_at"] < 1


def test_the_wrap_up_turn_asks_for_reply_text_and_no_message(tmp_path):
    """A wrap-up that says "reply" to a session taught to reply only by
    message sent its sender a "Done." tell at every close (live, 2026-10-22)."""
    text = resident_mod.wrapup_prompt(tmp_path, 2700)
    assert (
        "4. When done, say so in your reply text only; send no message. "
        "The session closes right after."
    ) in text
    assert "Reply briefly" not in text


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
@pytest.mark.parametrize("missing", ["initialize", "thread/start"])
@pytest.mark.parametrize("reply_after_deadline", [False, True])
def test_missing_codex_handshake_reply_drops_origins_within_stop_budget(
    monkeypatch, tmp_path, capsys, missing, reply_after_deadline,
):
    import threading
    import time

    from ar3.wake_outcome import origin_from_env, take_failures

    init = _out("pingpong", lambda m: m.get("id") == 1)
    thread = TestCodexSession()._thread_answer()
    record = tmp_path / "received.jsonl"
    release = tmp_path / "release-late-reply"
    fake = tmp_path / "silent_handshake.py"
    fake.write_text(
        "import json, sys, time, threading\nfrom pathlib import Path\n"
        f"answers = {{'initialize': {init!r}, 'thread/start': {thread!r}}}\n"
        "def late_reply(method):\n"
        f"    while not Path({str(release)!r}).exists(): time.sleep(0.005)\n"
        "    print(json.dumps(answers[method]), flush=True)\n"
        "for line in sys.stdin:\n"
        f"    with open({str(record)!r}, 'a', encoding='utf-8') as out: out.write(line)\n"
        "    request = json.loads(line)\n"
        "    method = request.get('method')\n"
        f"    if method in answers and method != {missing!r}:\n"
        "        print(json.dumps(answers[method]), flush=True)\n"
        f"    elif method == {missing!r} and {reply_after_deadline!r}:\n"
        "        threading.Thread(target=late_reply, args=(method,), daemon=True).start()\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(resident_mod, "CODEX_HANDSHAKE_DEADLINE_SECONDS", 0.3)
    monkeypatch.setattr(resident_mod, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(engine_run, "resolve_argv0", lambda argv: [sys.executable, str(fake)])
    late_processed = threading.Event()
    on_frame = resident_mod.CodexSession.on_frame
    def observed_frame(session, frame):
        parsed = on_frame(session, frame)
        if release.exists() and frame.get("id") == (1 if missing == "initialize" else 2):
            late_processed.set()
        return parsed
    monkeypatch.setattr(resident_mod.CodexSession, "on_frame", observed_frame)
    log = resident_mod._log
    def release_during_deadline_log(message):
        if reply_after_deadline and message.startswith("codex handshake was not ready"):
            release.touch()
            assert late_processed.wait(2), "fake app-server did not deliver late reply"
        log(message)
    monkeypatch.setattr(resident_mod, "_log", release_during_deadline_log)
    home = tmp_path / "resident"
    (home / "inbox").mkdir(parents=True)
    failure_dir = tmp_path / "failures"
    for name in ("first", "second"):
        origin = origin_from_env({
            "A8S_TURN_RECIPIENT": "probe",
            "A8S_TURN_ENVELOPES": json.dumps([name + ".json"]),
            "A8S_TURN_OUTCOME_DIR": str(failure_dir),
        }, "probe")
        resident_mod._enqueue_message(home, name, origin)
    reports = []
    report = resident_mod.report_failure
    monkeypatch.setattr(resident_mod, "report_failure", lambda *a, **kw: (
        reports.append((a, kw)), report(*a, **kw))[1])
    stop = threading.Event()
    watchdog = threading.Timer(5, stop.set)
    watchdog.daemon = True
    watchdog.start()
    started = time.monotonic()
    try:
        code = resident_mod._run_session(home, {
            "engine": "codex", "dir_path": str(tmp_path), "template": _template(),
            "agent": "probe", "timeout": 30, "scaffold": True,
        }, stop, {})
    finally:
        watchdog.cancel()
    elapsed = time.monotonic() - started
    assert not stop.is_set(), "missing handshake left bridge alive"
    assert late_processed.is_set() == reply_after_deadline
    assert code == 1 and elapsed < 0.3 + resident_mod.ACP_STOP_TERM_SECONDS + 2
    state = resident_mod.read_state(home)
    assert state["turns"] == 0 and state["status"] != "closing"
    assert not resident_mod.pid_alive(state["engine_pid"])
    requests = [json.loads(line) for line in record.read_text().splitlines()]
    assert [r["method"] for r in requests] == ["initialize", "initialized", "thread/start"]
    output = capsys.readouterr().out
    assert "codex handshake was not ready after 0.3s" in output
    assert "dropped 1 held message(s): deadline" in output
    assert "sending wrap-up" not in output
    assert len(reports) == 2 and all(args[1] == "deadline" for args, _ in reports)
    failures = list(take_failures(failure_dir, "probe"))
    assert len(failures) == 2
    assert sorted(f["envelopes"] for f in failures) == [["first.json"], ["second.json"]]
    assert list((home / "inbox").glob("*.json")) == []


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
@pytest.mark.parametrize("case", [
    "missing_pid", "bad_pid", "missing_token", "empty_token", "mismatched_pid",
    "old_boot", "unknown_boot", "reused_leader", "unknown_leader_token",
    "wrong_group", "unreadable_group", "dead_same_boot", "live_matching",
])
def test_published_bridge_group_requires_matching_identity(monkeypatch, tmp_path, case):
    pid = 12000
    home = tmp_path / "resident"
    home.mkdir()
    (home / "pid").write_text(str(pid))
    (home / "pid.start").write_text("this-boot 123")
    state = {"pid": pid}
    if case == "missing_pid":
        (home / "pid").unlink()
    elif case == "bad_pid":
        (home / "pid").write_text("invalid")
    elif case == "missing_token":
        (home / "pid.start").unlink()
    elif case == "empty_token":
        (home / "pid.start").write_text("")
    elif case == "mismatched_pid":
        state["pid"] += 1
    live = case in {"reused_leader", "unknown_leader_token", "wrong_group",
                    "unreadable_group", "live_matching"}
    monkeypatch.setattr(resident_mod, "pid_alive", lambda p: live)
    token = {"reused_leader": "this-boot 456", "unknown_leader_token": None}.get(
        case, "this-boot 123")
    monkeypatch.setattr(resident_mod, "process_start_token", lambda p: token)
    def getpgid(p):
        if case == "unreadable_group":
            raise ProcessLookupError(p)
        return p + 1 if case == "wrong_group" else p
    monkeypatch.setattr(resident_mod.os, "getpgid", getpgid)
    monkeypatch.setattr(resident_mod, "_published_group_from_this_boot",
                        lambda s: case not in {"old_boot", "unknown_boot"})
    swept = []
    monkeypatch.setattr(resident_mod, "_sweep_group", swept.append)
    resident_mod._end_published_bridge(home, state)
    assert swept == ([pid] if case in {"dead_same_boot", "live_matching"} else [])


def _abandoned_codex_child(tmp_path):
    import signal
    import subprocess

    record = tmp_path / "orphan.json"
    ready = tmp_path / "child-ready"
    child_code = (
        "import signal, time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(ready)!r}).write_text('ready')\n"
        "time.sleep(60)\n"
    )
    engine_code = (
        "import json, os, subprocess, sys, time\nfrom pathlib import Path\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"Path({str(record)!r}).write_text(json.dumps({{'engine': os.getpid(), 'child': child.pid}}))\n"
        "for line in sys.stdin: pass\n"
    )
    bridge_code = (
        "import subprocess, sys, time\n"
        f"engine = subprocess.Popen([sys.executable, '-c', {engine_code!r}], stdin=subprocess.PIPE)\n"
        "time.sleep(60)\n"
    )
    bridge = subprocess.Popen([sys.executable, "-c", bridge_code], start_new_session=True)
    try:
        _wait_until(ready.exists, timeout=5)
        ids = json.loads(record.read_text())
        home = resident_mod.resident_home(tmp_path, "probe")
        resident_mod._write_pid_files(home, bridge.pid)
        resident_mod._write_state(home, pid=bridge.pid, engine_pid=ids["engine"], status="running")
        os.kill(bridge.pid, signal.SIGKILL)
        bridge.wait(timeout=3)
        _wait_until(lambda: not resident_mod.pid_alive(ids["engine"]), timeout=5)
        assert resident_mod.pid_alive(ids["child"])
        assert os.getpgid(ids["child"]) == bridge.pid
        return bridge, ids["child"], home
    except BaseException:
        try:
            os.killpg(bridge.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        bridge.wait(timeout=3)
        raise


@pytest.mark.skipif(not Path("/proc/sys/kernel/random/boot_id").is_file(),
                    reason="leaderless bridge recovery requires Linux boot identity")
@pytest.mark.parametrize("operation", ["stop", "enqueue"])
def test_dead_bridge_and_exited_codex_leave_no_child_after_recovery(
    monkeypatch, tmp_path, operation,
):
    import signal

    bridge, child, home = _abandoned_codex_child(tmp_path)
    try:
        if operation == "stop":
            assert resident_mod.stop_resident(tmp_path, "probe") is False
            assert not (home / "pid").exists()
        else:
            _patch_codex(monkeypatch, tmp_path)
            write_state = resident_mod._write_state
            replacements = []
            def checked_write(*args, **kwargs):
                if kwargs["pid"] != bridge.pid:
                    assert not resident_mod.pid_alive(child)
                    replacements.append(kwargs["pid"])
                return write_state(*args, **kwargs)
            monkeypatch.setattr(resident_mod, "_write_state", checked_write)
            assert resident_mod.enqueue_or_start(
                "codex", "hello", dir_path=tmp_path, template=_template(), model=None,
                effort=None, agent="probe", timeout=30, scaffold=False,
            ) == 0
            assert replacements
        _wait_until(lambda: not resident_mod.pid_alive(child), timeout=3)
    finally:
        resident_mod.stop_resident(tmp_path, "probe", grace_seconds=2)
        try:
            os.killpg(bridge.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
