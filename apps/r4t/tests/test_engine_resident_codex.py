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


def test_the_wrap_up_turn_asks_for_reply_text_and_no_message(tmp_path):
    """A wrap-up that says "reply" to a session taught to reply only by
    message sent its sender a "Done." tell at every close (live, 2026-10-22)."""
    text = resident_mod.wrapup_prompt(tmp_path, 2700)
    assert (
        "4. When done, say so in your reply text only; send no message. "
        "The session closes right after."
    ) in text
    assert "Reply briefly" not in text
