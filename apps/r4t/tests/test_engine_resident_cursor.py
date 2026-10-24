"""The resident session on cursor: `agent acp` behind the bridge.

Frame tests drive `CursorSession` with the handshake proven against agent
2026.10.22-27dcf5f. The lifecycle test runs a fake ACP server through the
bridge; that fake is a POSIX script.
"""
from __future__ import annotations

import json
import os
import signal
import stat
import sys
from pathlib import Path

import pytest

from engines import resident as resident_mod
from engines import run as engine_run
from engines.run import RunError, _build_argv_template
from ar3.proc import terminate_group
from r4t import main as r4t_main

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cursor-acp"
FAKE_ACP = FIXTURES / "fake_acp.py"
CATALOG = [
    {"value": "composer-2.5[fast=true]", "name": "composer-2.5"},
    {"value": "grok-4.6[effort=high,fast=true]", "name": "grok-4.6"},
]


def _template(permissions=None, model="composer-2.5") -> list[str]:
    template, _ = _build_argv_template(
        "cursor", model=model, timeout=900, workdir=Path("/tmp"),
        permissions=permissions, resident=True,
    )
    return template


def _rpc(line: str) -> dict:
    return json.loads(line)


def _new_result(current: str = "composer-2.5[fast=true]", options: list | None = None) -> dict:
    return {
        "id": 3,
        "result": {
            "sessionId": "sess-1",
            "configOptions": [{
                "id": "model",
                "currentValue": current,
                "options": options if options is not None else CATALOG,
            }],
        },
    }


class TestComposeCursorArgv:
    def test_default_template_becomes_agent_acp_with_the_model_and_force(self):
        env: dict[str, str] = {}
        argv = resident_mod.compose_resident_argv(_template(), env, engine="cursor")
        assert argv == ["agent", "acp"]
        assert "--model" not in argv
        assert "--force" not in argv
        assert env["R4T_SESSION_MODEL"] == "composer-2.5"

    def test_bypass_keeps_force(self):
        env: dict[str, str] = {}
        argv = resident_mod.compose_resident_argv(
            _template("bypass"), env, engine="cursor",
        )
        assert argv == ["agent", "--force", "acp"]
        assert env["R4T_SESSION_MODEL"] == "composer-2.5"

    def test_ask_drops_force(self):
        env: dict[str, str] = {}
        argv = resident_mod.compose_resident_argv(
            _template("ask"), env, engine="cursor",
        )
        assert argv == ["agent", "acp"]
        assert "--force" not in argv
        assert "--model" not in argv
        assert env["R4T_SESSION_MODEL"] == "composer-2.5"

    def test_print_mode_flags_do_not_survive(self):
        argv = resident_mod.compose_resident_argv(_template(), {}, engine="cursor")
        for dropped in ("-p", "--trust", "--approve-mcps", "{prompt}"):
            assert dropped not in argv

    def test_mcp_log_is_published_for_the_session(self, tmp_path, monkeypatch):
        monkeypatch.delenv("A8S_MCP_LOG", raising=False)
        log = tmp_path / "mcp.jsonl"
        env: dict[str, str] = {}
        resident_mod.compose_resident_argv(
            _template(), env, engine="cursor", mcp_log_path=log,
        )
        assert env["A8S_MCP_LOG"] == str(log)
        assert "A8S_MCP_LOG" not in os.environ

    def test_a_token_acp_cannot_carry_is_refused(self):
        with pytest.raises(RunError, match="--sandbox"):
            resident_mod.compose_resident_argv(
                ["agent", "-p", "--sandbox", "enabled", "{prompt}"], {}, engine="cursor",
            )

    def test_cursor_is_a_resident_engine_with_the_mcp_tell_step(self):
        assert "cursor" in resident_mod.RESIDENT_ENGINES
        adapter = resident_mod.RESIDENT_ADAPTERS["cursor"]
        assert adapter.tell_step == engine_run.MCP_TELL_STEP
        assert "`a8s_tell`" in adapter.tell_step
        assert adapter.merge_stderr is False

    def test_slug_resolves_to_the_catalog_value_and_an_exact_value_stays(self):
        assert resident_mod.cursor_catalog_model("composer-2.5", CATALOG) == (
            "composer-2.5[fast=true]"
        )
        exact = "grok-4.6[effort=high,fast=true]"
        assert resident_mod.cursor_catalog_model(exact, CATALOG) == exact
        assert resident_mod.cursor_catalog_model("missing", CATALOG) is None
        duplicated = [
            {"value": "composer-2.5[fast=true]", "name": "composer-2.5"},
            {"value": "composer-2.5[fast=false]", "name": "composer-2.5"},
        ]
        assert resident_mod.cursor_catalog_model("composer-2.5", duplicated) is None
        assert resident_mod.cursor_catalog_model(
            "composer-2.5[fast=true]", duplicated,
        ) == "composer-2.5[fast=true]"
        assert resident_mod.cursor_catalog_model(
            "grok-4.6[effort=high]", CATALOG,
        ) == "grok-4.6[effort=high,fast=true]"
        two_fast = [
            {"value": "grok-4.6[effort=high,fast=true]", "name": "grok-4.6"},
            {"value": "grok-4.6[effort=high,fast=false]", "name": "grok-4.6"},
        ]
        assert resident_mod.cursor_catalog_model(
            "grok-4.6[effort=high]", two_fast,
        ) is None
        assert resident_mod.cursor_catalog_refusal(
            "grok-4.6[effort=high]", two_fast,
        ) == "ambiguous model name 'grok-4.6[effort=high]'"


class TestCursorSession:
    def _open(self, tmp_path, permissions=None, model="composer-2.5"):
        env: dict[str, str] = {}
        argv = resident_mod.compose_resident_argv(
            _template(permissions, model), env, engine="cursor",
        )
        sess = resident_mod.CursorSession(argv, tmp_path, env)
        opening = [_rpc(line) for line in sess.opening_lines()]
        assert [msg["method"] for msg in opening] == ["initialize"]
        return sess

    def _ready(self, tmp_path, **kwargs):
        sess = self._open(tmp_path, **kwargs)
        init = sess.on_frame({"id": 1, "result": {
            "protocolVersion": 1,
            "authMethods": [{"id": "cursor_login"}],
        }})
        assert json.loads(init.replies[0])["method"] == "authenticate"
        auth = sess.on_frame({"id": 2, "result": {}})
        (line,) = auth.replies
        new = json.loads(line)
        assert new["method"] == "session/new"
        assert new["params"]["cwd"] == str(tmp_path.resolve())
        servers = new["params"]["mcpServers"]
        assert servers[0]["name"] == "a8s"
        assert isinstance(servers[0]["env"], list)
        return sess

    def test_a_slug_already_current_needs_no_pin_and_releases_a_held_turn(self, tmp_path):
        sess = self._ready(tmp_path)
        assert sess.turn_lines("hello") == []
        parsed = sess.on_frame(_new_result())
        assert parsed.fatal is False
        assert sess.ready
        assert sess.model_value == "composer-2.5[fast=true]"
        (line,) = parsed.replies
        prompt = _rpc(line)
        assert prompt["method"] == "session/prompt"
        assert prompt["params"]["prompt"] == [{"type": "text", "text": "hello"}]
        assert prompt["params"]["sessionId"] == "sess-1"

    def test_a_different_model_is_pinned_before_the_turn(self, tmp_path):
        sess = self._ready(tmp_path, model="grok-4.6")
        sess.turn_lines("hello")
        parsed = sess.on_frame(_new_result())
        assert not sess.ready
        (line,) = parsed.replies
        pin = _rpc(line)
        assert pin["method"] == "session/set_config_option"
        assert pin["params"]["configId"] == "model"
        assert pin["params"]["value"] == "grok-4.6[effort=high,fast=true]"
        pinned = sess.on_frame({
            "id": pin["id"],
            "result": {"configOptions": [{
                "id": "model",
                "currentValue": "grok-4.6[effort=high,fast=true]",
            }]},
        })
        assert sess.ready
        (turn,) = pinned.replies
        assert _rpc(turn)["method"] == "session/prompt"

    def test_a_missing_catalog_model_is_fatal(self, tmp_path):
        sess = self._ready(tmp_path, model="no-such-model")
        parsed = sess.on_frame(_new_result())
        assert parsed.fatal
        assert "no-such-model" in parsed.error_text
        assert "has no model" in parsed.error_text
        assert "composer-2.5[fast=true]" in parsed.error_text
        assert sess.session_id is None or not sess.ready

    def test_duplicate_catalog_names_are_ambiguous(self, tmp_path):
        sess = self._ready(tmp_path, model="composer-2.5")
        parsed = sess.on_frame(_new_result(options=[
            {"value": "composer-2.5[fast=true]", "name": "composer-2.5"},
            {"value": "composer-2.5[fast=false]", "name": "composer-2.5"},
        ]))
        assert parsed.fatal
        assert parsed.error_text == "ambiguous model name 'composer-2.5'"
        assert "has no model" not in parsed.error_text

    def test_a_bracketed_slug_matching_two_values_is_ambiguous(self, tmp_path):
        sess = self._ready(tmp_path, model="grok-4.6[effort=high]")
        parsed = sess.on_frame(_new_result(options=[
            {"value": "grok-4.6[effort=high,fast=true]", "name": "grok-4.6"},
            {"value": "grok-4.6[effort=high,fast=false]", "name": "grok-4.6"},
        ]))
        assert parsed.fatal
        assert parsed.error_text == "ambiguous model name 'grok-4.6[effort=high]'"
        assert "has no model" not in parsed.error_text

    def test_two_prompts_one_session_one_on_the_wire(self, tmp_path):
        sess = self._ready(tmp_path)
        sess.on_frame(_new_result())
        (first,) = sess.turn_lines("one")
        assert sess.turn_lines("two") == []
        chunk = {
            "method": "session/update",
            "params": {
                "sessionId": "sess-1",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "PONG"},
                },
            },
        }
        assert sess.on_frame(chunk).assistant_text == ""
        done = sess.on_frame({"id": _rpc(first)["id"], "result": {"stopReason": "end_turn"}})
        assert done.turn_completed
        assert done.assistant_text == "PONG"
        assert done.result_text == "PONG"
        (second,) = done.replies
        assert _rpc(second)["params"]["prompt"][0]["text"] == "two"
        assert _rpc(second)["params"]["sessionId"] == "sess-1"

    def test_a_failed_handshake_is_fatal(self, tmp_path):
        sess = self._open(tmp_path)
        assert sess.on_frame({"id": 1, "error": {"message": "no"}}).fatal
        assert sess.on_frame({"id": 2, "error": {"message": "no"}}).fatal
        assert sess.on_frame({"id": 3, "error": {"message": "no"}}).fatal

    def test_force_allows_a_permission_once_and_ask_rejects_it(self, tmp_path):
        allowed = self._ready(tmp_path, permissions="bypass")
        ask = {
            "id": 9,
            "method": "session/request_permission",
            "params": {"options": [
                {"optionId": "allow-once", "kind": "allow_once"},
                {"optionId": "reject-once", "kind": "reject_once"},
            ]},
        }
        parsed = allowed.on_frame(ask)
        assert _rpc(parsed.replies[0])["result"]["outcome"]["optionId"] == "allow-once"
        denied = self._ready(tmp_path, permissions="ask")
        parsed = denied.on_frame(ask)
        assert _rpc(parsed.replies[0])["result"]["outcome"]["optionId"] == "reject-once"

    def test_a_pin_answer_without_config_options_is_fatal(self, tmp_path):
        sess = self._ready(tmp_path, model="grok-4.6")
        parsed = sess.on_frame(_new_result())
        pin = _rpc(parsed.replies[0])
        failed = sess.on_frame({"id": pin["id"], "result": {}})
        assert failed.fatal
        assert "pin left" in failed.error_text
        assert not sess.ready

    def test_a_rejected_pin_is_fatal(self, tmp_path):
        sess = self._ready(tmp_path, model="grok-4.6")
        parsed = sess.on_frame(_new_result())
        pin = _rpc(parsed.replies[0])
        failed = sess.on_frame({"id": pin["id"], "error": {"message": "no"}})
        assert failed.fatal
        assert not sess.ready
        left = self._ready(tmp_path, model="grok-4.6")
        opened = left.on_frame(_new_result())
        wrong = left.on_frame({
            "id": _rpc(opened.replies[0])["id"],
            "result": {"configOptions": [{
                "id": "model", "currentValue": "composer-2.5[fast=true]",
            }]},
        })
        assert wrong.fatal
        assert not left.ready

    def test_a_question_and_a_plan_do_not_block(self, tmp_path):
        sess = self._ready(tmp_path)
        question = sess.on_frame({"id": 4, "method": "cursor/ask_question", "params": {}})
        plan = sess.on_frame({"id": 5, "method": "cursor/create_plan", "params": {}})
        assert _rpc(question.replies[0])["result"] == {"outcome": "skipped"}
        assert _rpc(plan.replies[0])["result"] == {"outcome": "cancelled"}

    def test_a_refused_turn_completes_with_the_stop_reason(self, tmp_path):
        sess = self._ready(tmp_path)
        sess.on_frame(_new_result())
        (line,) = sess.turn_lines("one")
        parsed = sess.on_frame({"id": _rpc(line)["id"], "result": {"stopReason": "refusal"}})
        assert parsed.turn_completed
        assert parsed.error_text == "acp turn refusal"

    def test_boot_prompt_teaches_the_mcp_tell(self, tmp_path):
        prompt = resident_mod.resident_boot_prompt(
            tmp_path, "hello", agent="bob", engine="cursor",
            resident_seconds=2700, lessons_cap=200, lessons_cap_bytes=35840,
        )
        assert "a8s_tell" in prompt
        assert prompt.startswith("Smart cold boot (resident session):")


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_probe_reaps_a_child_that_ignores_sigterm(monkeypatch, tmp_path):
    child_file = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CURSOR_IGNORE_EOF", "1")
    monkeypatch.setenv("FAKE_CURSOR_CHILD", "1")
    monkeypatch.setenv("FAKE_CURSOR_CHILD_PID", str(child_file))
    argv = [sys.executable, str(FAKE_ACP)]
    result = resident_mod.cursor_acp_probe(
        argv, tmp_path, env={"R4T_SESSION_MODEL": "composer-2.5"}, timeout=5,
    )
    assert result.get("error") is None
    child_pid = int(child_file.read_text(encoding="utf-8"))
    _wait_until(lambda: not resident_mod.pid_alive(child_pid), timeout=2)


@pytest.mark.skipif(os.name != "posix", reason="the probe stop is covered on POSIX")
def test_a_probe_that_never_answers_returns(tmp_path):
    import time

    argv = [sys.executable, "-c", "import time; time.sleep(60)"]
    started = time.monotonic()
    result = resident_mod.cursor_acp_probe(argv, tmp_path, timeout=0.6)
    assert "handshake" in result.get("error", "")
    assert time.monotonic() - started < 8


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_an_authenticate_error_fails_the_probe(monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_CURSOR_AUTH_ERROR", "1")
    result = resident_mod.cursor_acp_probe(
        [sys.executable, str(FAKE_ACP)], tmp_path,
        env={"R4T_SESSION_MODEL": "composer-2.5"}, timeout=5,
    )
    assert "authenticate failed" in result.get("error", "")
    assert "login refused" in result["error"]


def test_check_notes_an_unproven_agent_version(monkeypatch, tmp_path):
    from engines import check as check_mod

    monkeypatch.setattr(
        resident_mod, "cursor_acp_probe",
        lambda *args, **kwargs: {"model": "composer-2.5[fast=true]"},
    )
    report = check_mod.EngineReport(
        engine="cursor", version="0.0.0-test", installed=True,
    )
    out = check_mod._check_cursor_acp(report, ["agent", "acp"], tmp_path, {})
    assert out.verdict == check_mod.ACCEPTED
    assert "0.0.0-test" in out.detail
    assert resident_mod.CURSOR_ACP_PROVEN in out.detail


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_a_missed_handshake_ends_the_bridge(monkeypatch, tmp_path, capsys):
    import threading
    import time

    import engines.acp as acp_mod

    monkeypatch.setattr(acp_mod, "HANDSHAKE_DEADLINE_SECONDS", 0.4)
    hang = tmp_path / "hang-acp"
    hang.write_text(
        f"#!{sys.executable}\nimport time\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    hang.chmod(hang.stat().st_mode | stat.S_IEXEC)
    home = tmp_path / "resident"
    home.mkdir()
    spec = {
        "engine": "cursor",
        "dir_path": str(tmp_path),
        "template": [str(hang)],
        "agent": "ares",
        "timeout": 30,
        "scaffold": False,
    }
    started = time.monotonic()
    code = resident_mod._run_session(home, spec, threading.Event(), {})
    elapsed = time.monotonic() - started
    assert code != 0
    assert elapsed >= 0.4
    assert elapsed < 8
    assert "after 0.4s" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_a_missed_handshake_signals_at_once(monkeypatch, tmp_path, capsys):
    """A missed handshake is SIGTERM at once. The ACP stop grace does not apply."""
    import json
    import threading
    import time

    import engines.acp as acp_mod

    monkeypatch.setattr(acp_mod, "HANDSHAKE_DEADLINE_SECONDS", 0.4)
    record = tmp_path / "sigterm.json"
    hang = tmp_path / "hang-acp"
    hang.write_text(
        f"#!{sys.executable}\n"
        "import json, os, signal, time\n"
        "from pathlib import Path\n"
        f"record = Path({str(record)!r})\n"
        "def on_term(*_a):\n"
        "    ready = json.loads(record.read_text(encoding='utf-8'))\n"
        "    ready['sigterm_at'] = time.time()\n"
        "    record.write_text(json.dumps(ready), encoding='utf-8')\n"
        "    os._exit(0)\n"
        "signal.signal(signal.SIGTERM, on_term)\n"
        "record.write_text(json.dumps({'ready': time.time()}), encoding='utf-8')\n"
        "deadline = time.monotonic() + 3600\n"
        "while time.monotonic() < deadline:\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    hang.chmod(hang.stat().st_mode | stat.S_IEXEC)
    home = tmp_path / "resident"
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "hello")
    spec = {
        "engine": "cursor",
        "dir_path": str(tmp_path),
        "template": [str(hang)],
        "agent": "ares",
        "timeout": 30,
        "scaffold": False,
        "memory": "off",
    }
    resident_mod._run_session(home, spec, threading.Event(), {})
    rec = json.loads(record.read_text(encoding="utf-8"))
    assert rec.get("ready") and rec.get("sigterm_at")
    # The deadline is 0.4 s. The ACP grace is 1.5 s and must not be added.
    assert rec["sigterm_at"] - rec["ready"] < 1.2
    assert "dropped 1 held message(s): deadline" in capsys.readouterr().out


def _force_windows_stop(monkeypatch) -> None:
    monkeypatch.setattr(resident_mod, "_use_windows_stop", lambda: True)

    def killpg(*_args, **_kwargs):
        raise AssertionError("killpg")

    monkeypatch.setattr(os, "killpg", killpg, raising=False)


def test_a_windows_stop_ends_a_live_engine(monkeypatch):
    import subprocess

    _force_windows_stop(monkeypatch)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.PIPE,
    )
    try:
        resident_mod.stop_engine(
            proc, group=proc.pid, grace_seconds=resident_mod.ACP_STOP_GRACE_SECONDS,
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
    assert proc.poll() is not None


def test_a_windows_stop_accepts_an_exited_engine(monkeypatch):
    import subprocess

    _force_windows_stop(monkeypatch)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    resident_mod.stop_engine(
        proc, group=proc.pid, grace_seconds=resident_mod.ACP_STOP_GRACE_SECONDS,
    )
    assert proc.poll() is not None


def test_a_windows_engine_that_exits_after_eof_is_not_terminated(monkeypatch, tmp_path):
    import subprocess

    _force_windows_stop(monkeypatch)
    terminated: list[int] = []
    real = subprocess.Popen.terminate

    def spy(self):
        terminated.append(self.pid)
        return real(self)

    monkeypatch.setattr(subprocess.Popen, "terminate", spy)
    script = tmp_path / "exit-after-eof.py"
    script.write_text("import sys, time\nsys.stdin.read()\ntime.sleep(5)\n", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(script)], stdin=subprocess.PIPE, text=True,
    )
    try:
        resident_mod.stop_engine(proc, grace_seconds=resident_mod.EOF_GRACE_SECONDS)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
    assert terminated == []
    assert proc.returncode == 0


def test_a_windows_engine_that_ignores_eof_is_terminated_after_the_grace(monkeypatch):
    import subprocess
    import time

    _force_windows_stop(monkeypatch)
    terminated: list[float] = []
    real = subprocess.Popen.terminate

    def spy(self):
        terminated.append(time.monotonic())
        return real(self)

    monkeypatch.setattr(subprocess.Popen, "terminate", spy)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.PIPE,
    )
    started = time.monotonic()
    try:
        resident_mod.stop_engine(proc, grace_seconds=1.5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
    assert terminated
    assert terminated[0] - started >= 1.4
    assert proc.poll() is not None


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_a_missed_handshake_drops_a_held_turn(monkeypatch, tmp_path, capsys):
    import threading
    import time

    import engines.acp as acp_mod

    monkeypatch.setattr(acp_mod, "HANDSHAKE_DEADLINE_SECONDS", 0.4)
    hang = tmp_path / "hang-acp"
    hang.write_text(
        f"#!{sys.executable}\nimport time\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    hang.chmod(hang.stat().st_mode | stat.S_IEXEC)
    home = tmp_path / "resident"
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "hello")
    spec = {
        "engine": "cursor",
        "dir_path": str(tmp_path),
        "template": [str(hang)],
        "agent": "ares",
        "timeout": 30,
        "scaffold": False,
    }
    started = time.monotonic()
    resident_mod._run_session(home, spec, threading.Event(), {})
    elapsed = time.monotonic() - started
    budget = (
        0.4
        + resident_mod.ACP_CANCEL_WAIT_SECONDS
        + resident_mod.ACP_STOP_GRACE_SECONDS
        + resident_mod.ACP_STOP_TERM_SECONDS
        + 4
    )
    assert elapsed < budget
    state = resident_mod.read_state(home) or {}
    assert state.get("turns", 0) == 0
    assert state.get("status") != "closing"
    out = capsys.readouterr().out
    assert "sending wrap-up" not in out
    assert "dropped 1 held message(s): deadline" in out


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_session_cancel_is_the_first_step_of_the_stop():
    import subprocess
    import time

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.PIPE,
        text=True,
    )
    seen: list[str] = []
    started = time.monotonic()
    try:
        resident_mod.stop_engine(
            proc,
            cancel_line='{"method": "session/cancel", "params": {"sessionId": "s"}}',
            write_line=lambda text: seen.append(text) or True,
            turn_settled=lambda: False,
            grace_seconds=resident_mod.ACP_STOP_GRACE_SECONDS,
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
    elapsed = time.monotonic() - started
    assert seen and "session/cancel" in seen[0]
    assert elapsed >= resident_mod.ACP_CANCEL_WAIT_SECONDS - 0.2
    assert elapsed < (
        resident_mod.ACP_CANCEL_WAIT_SECONDS
        + resident_mod.ACP_STOP_GRACE_SECONDS
        + resident_mod.ACP_STOP_TERM_SECONDS
        + 2
    )
    assert proc.poll() is not None


def test_probe_accepts_the_proven_handshake_and_rejects_a_wrong_version(
    monkeypatch, tmp_path,
):
    argv = [sys.executable, str(FAKE_ACP)]
    env = {"R4T_SESSION_MODEL": "composer-2.5"}
    accepted = resident_mod.cursor_acp_probe(argv, tmp_path, env=env, timeout=5)
    assert accepted.get("error") is None
    assert accepted.get("model") == "composer-2.5[fast=true]"
    monkeypatch.setenv("FAKE_CURSOR_PROTOCOL", "999")
    rejected = resident_mod.cursor_acp_probe(argv, tmp_path, env=env, timeout=5)
    assert rejected.get("error")
    assert "999" in rejected["error"]


def _fake_agent(tmp_path: Path) -> Path:
    path = tmp_path / "fake-agent"
    path.write_text(
        f"#!{sys.executable}\nimport runpy, sys\n"
        f"sys.argv[0] = {str(FAKE_ACP)!r}\n"
        f"runpy.run_path({str(FAKE_ACP)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _patch_cursor(monkeypatch, tmp_path) -> Path:
    fake = _fake_agent(tmp_path)
    preset = engine_run.HARNESS_PRESETS["cursor"]
    monkeypatch.setitem(
        engine_run.HARNESS_PRESETS, "cursor",
        {**preset, "invoke": [str(fake), *preset["invoke"][1:]]},
    )
    record = tmp_path / "cursor-record.json"
    monkeypatch.setenv("FAKE_CURSOR_RECORD", str(record))
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
    home = resident_mod.resident_home(workdir, agent)
    state = resident_mod.read_state(home) or {}
    group = state.get("engine_group")
    if isinstance(group, int) and group > 0 and os.name == "posix":
        try:
            os.killpg(group, signal.SIGKILL)
        except OSError:
            pass
    pid = resident_mod.read_live_pid(home)
    if pid is not None:
        terminate_group(pid)


def _run(workdir: Path, message: str, *extra: str) -> int:
    return r4t_main([
        "engine", "cursor", "run", "--resident", "--dir", str(workdir),
        "--agent", "ares", *extra, message,
    ])


def _capture_text(frames: list[dict]) -> str:
    parts: list[str] = []
    for frame in frames:
        if frame.get("method") != "session/update":
            continue
        update = (frame.get("params") or {}).get("update") or {}
        if not isinstance(update, dict):
            continue
        if update.get("sessionUpdate") != "agent_message_chunk":
            continue
        content = update.get("content") or {}
        if isinstance(content, dict) and isinstance(content.get("text"), str):
            parts.append(content["text"])
    return "".join(parts)


_CAPTURES = sorted(path.name for path in FIXTURES.glob("*.jsonl"))


@pytest.mark.parametrize("capture", _CAPTURES)
def test_a_capture_replays_through_the_session(capture, tmp_path):
    frames = [
        json.loads(line)
        for line in (FIXTURES / capture).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    forced = capture.endswith("-force.jsonl") and "no-force" not in capture
    argv = ["agent", "--force", "acp"] if forced else ["agent", "acp"]
    model = "grok-4.6" if capture.startswith("pin-") else "composer-2.5"
    sess = resident_mod.CursorSession(
        argv, tmp_path, {"R4T_SESSION_MODEL": model},
    )
    pending: list[str] = []
    prompted = False
    text = ""
    for frame in frames:
        if "_sent" in frame:
            assert pending, frame
            assert json.loads(pending.pop(0)) == frame["_sent"]
            continue
        if not prompted and sess.ready and frame.get("method") == "session/update":
            assert sess.turn_lines("captured turn")
            prompted = True
        parsed = sess.on_frame(frame)
        assert parsed.fatal is False, parsed.error_text
        if frame.get("id") == 1 and frame.get("method") is None:
            assert [json.loads(line)["method"] for line in parsed.replies] == [
                "authenticate",
            ]
        elif frame.get("id") == 2 and frame.get("method") is None:
            assert [json.loads(line)["method"] for line in parsed.replies] == [
                "session/new",
            ]
        else:
            pending.extend(parsed.replies)
        if parsed.assistant_text:
            text = parsed.assistant_text
    assert pending == []
    expect = _capture_text(frames)
    if expect:
        assert prompted
        assert text == expect
    else:
        assert sess.ready


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
class TestCursorResidentLifecycle:
    def test_two_messages_one_process_then_wrap_up(self, monkeypatch, tmp_path):
        record = _patch_cursor(monkeypatch, tmp_path)
        monkeypatch.setenv("FAKE_CURSOR_TURN_SECONDS", "0.8")
        monkeypatch.setenv("FAKE_CURSOR_IGNORE_EOF", "1")
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
            assert rec["argv"][-1] == "acp"
            assert "--model" not in rec["argv"]
            assert "composer-2.5" not in rec["argv"]
            assert "--force" not in rec["argv"]
            assert rec.get("overlap") is not True
            prompts = [e for e in rec["events"] if e.get("method") == "session/prompt"]
            stops = [e for e in rec["events"] if e.get("method") == "stopReason"]
            assert len(prompts) >= 2 and len(stops) >= 1
            assert prompts[1]["t"] >= stops[0]["t"]
            methods = [msg.get("method") for msg in rec["in"]]
            assert methods[:3] == ["initialize", "authenticate", "session/new"]
            assert methods.count("session/new") == 1
            turns = rec["turns"]
            assert len(turns) == 3
            assert "first from alice" in turns[0] and "a8s_tell" in turns[0]
            assert "second from bob" in turns[1]
            assert "wrap up and exit" in turns[2]
        finally:
            _stop(workdir, "ares")

    def test_a_permission_request_is_allowed_once(self, monkeypatch, tmp_path):
        record = _patch_cursor(monkeypatch, tmp_path)
        monkeypatch.setenv("FAKE_CURSOR_PERMISSION", "1")
        workdir = tmp_path / "seat"
        workdir.mkdir()
        try:
            assert _run(workdir, "hello", "--timeout", "2", "--permissions", "bypass") == 0
            _wait_until(
                lambda: any(
                    e.get("method") == "stopReason"
                    for e in _rec(record).get("events", [])
                ),
                timeout=15.0,
            )
            (reply,) = _rec(record)["replies"]
            assert reply["id"] == 90
            assert reply["result"]["outcome"]["optionId"] == "allow-once"
            reply_at = next(
                e["t"] for e in _rec(record)["events"]
                if e.get("id") == 90 and e.get("method") is None
            )
            stop_at = next(
                e["t"] for e in _rec(record)["events"] if e.get("method") == "stopReason"
            )
            assert reply_at <= stop_at
        finally:
            _stop(workdir, "ares")

    def test_a_restricted_permission_is_rejected_once(self, monkeypatch, tmp_path):
        record = _patch_cursor(monkeypatch, tmp_path)
        monkeypatch.setenv("FAKE_CURSOR_PERMISSION", "1")
        workdir = tmp_path / "seat"
        workdir.mkdir()
        try:
            assert _run(workdir, "hello", "--timeout", "2") == 0
            _wait_until(
                lambda: _rec(record).get("optionId") == "reject-once",
                timeout=15.0,
            )
            (reply,) = _rec(record)["replies"]
            assert reply["id"] == 90
            assert reply["result"]["outcome"]["optionId"] == "reject-once"
        finally:
            _stop(workdir, "ares")

    @pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
    def test_a_fatal_frame_during_wrap_up_stops_the_engine(
        self, monkeypatch, tmp_path,
    ):
        import time

        record = _patch_cursor(monkeypatch, tmp_path)
        monkeypatch.setenv("FAKE_CURSOR_MALFORMED", "2")
        monkeypatch.setenv("FAKE_CURSOR_IGNORE_SIGTERM", "1")
        monkeypatch.setenv("FAKE_CURSOR_IGNORE_EOF", "1")
        workdir = tmp_path / "seat"
        workdir.mkdir()
        home = resident_mod.resident_home(workdir, "ares")
        engine_pid = None
        bridge_pid = None
        try:
            assert _run(workdir, "hello", "--timeout", "1") == 0

            def two_prompts():
                prompts = [
                    msg for msg in _rec(record).get("in", [])
                    if msg.get("method") == "session/prompt"
                ]
                return len(prompts) >= 2

            _wait_until(two_prompts, timeout=15)
            marked = time.monotonic()
            window = (
                resident_mod.ACP_CANCEL_WAIT_SECONDS
                + resident_mod.ACP_STOP_GRACE_SECONDS
                + resident_mod.ACP_STOP_TERM_SECONDS
                + 4
            )

            def captured():
                nonlocal engine_pid, bridge_pid
                state = resident_mod.read_state(home) or {}
                if state.get("engine_pid"):
                    engine_pid = state["engine_pid"]
                live = resident_mod.read_live_pid(home)
                if live:
                    bridge_pid = live
                return bool(engine_pid and bridge_pid)

            captured()

            def gone():
                if resident_mod.read_live_pid(home) is not None:
                    return False
                if engine_pid and resident_mod.pid_alive(engine_pid):
                    return False
                if not bridge_pid:
                    return False
                try:
                    waited, _status = os.waitpid(bridge_pid, os.WNOHANG)
                except ChildProcessError:
                    return True
                return waited == bridge_pid

            _wait_until(gone, timeout=window)
            assert time.monotonic() - marked < window
        finally:
            _stop(workdir, "ares")

    @pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
    def test_a_fatal_frame_reaps_an_engine_that_ignores_sigterm(
        self, monkeypatch, tmp_path,
    ):
        import time

        record = _patch_cursor(monkeypatch, tmp_path)
        monkeypatch.setenv("FAKE_CURSOR_MALFORMED", "1")
        monkeypatch.setenv("FAKE_CURSOR_IGNORE_SIGTERM", "1")
        workdir = tmp_path / "seat"
        workdir.mkdir()
        home = resident_mod.resident_home(workdir, "ares")
        engine_pid = None
        bridge_pid = None
        try:
            assert _run(workdir, "hello", "--timeout", "1") == 0

            def captured():
                nonlocal engine_pid, bridge_pid
                state = resident_mod.read_state(home) or {}
                if state.get("engine_pid"):
                    engine_pid = state["engine_pid"]
                live = resident_mod.read_live_pid(home)
                if live:
                    bridge_pid = live
                return bool(engine_pid and bridge_pid)

            _wait_until(captured, timeout=5)
            marked = time.monotonic()
            window = (
                resident_mod.ACP_CANCEL_WAIT_SECONDS
                + resident_mod.ACP_STOP_GRACE_SECONDS
                + resident_mod.ACP_STOP_TERM_SECONDS
                + 4
            )
            def gone():
                if resident_mod.read_live_pid(home) is not None:
                    return False
                if resident_mod.pid_alive(engine_pid):
                    return False
                try:
                    waited, _status = os.waitpid(bridge_pid, os.WNOHANG)
                except ChildProcessError:
                    return True
                return waited == bridge_pid

            _wait_until(gone, timeout=window)
            assert time.monotonic() - marked < window
            methods = [msg.get("method") for msg in _rec(record).get("in", [])]
            assert "session/prompt" in methods
            assert "session/cancel" not in methods
        finally:
            _stop(workdir, "ares")

    @pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
    def test_a_child_dies_after_the_leader_exits(self, monkeypatch, tmp_path):
        _patch_cursor(monkeypatch, tmp_path)
        child_file = tmp_path / "child.pid"
        monkeypatch.setenv("FAKE_CURSOR_CHILD", "1")
        monkeypatch.setenv("FAKE_CURSOR_CHILD_PID", str(child_file))
        monkeypatch.setenv("FAKE_CURSOR_EXIT_AFTER_PROMPT", "1")
        workdir = tmp_path / "seat"
        workdir.mkdir()
        home = resident_mod.resident_home(workdir, "ares")
        try:
            assert _run(workdir, "hello", "--timeout", "30") == 0
            _wait_until(lambda: child_file.is_file(), timeout=5)
            child_pid = int(child_file.read_text(encoding="utf-8"))
            _wait_until(
                lambda: (
                    resident_mod.read_live_pid(home) is None
                    and not resident_mod.pid_alive(child_pid)
                ),
                timeout=20,
            )
        finally:
            _stop(workdir, "ares")
            if child_file.is_file():
                try:
                    os.kill(int(child_file.read_text(encoding="utf-8")), signal.SIGKILL)
                except OSError:
                    pass

    @pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
    def test_a_child_that_ignores_sigterm_dies_when_the_session_ends(
        self, monkeypatch, tmp_path,
    ):
        _patch_cursor(monkeypatch, tmp_path)
        child_file = tmp_path / "child.pid"
        monkeypatch.setenv("FAKE_CURSOR_IGNORE_EOF", "1")
        monkeypatch.setenv("FAKE_CURSOR_CHILD", "1")
        monkeypatch.setenv("FAKE_CURSOR_CHILD_PID", str(child_file))
        workdir = tmp_path / "seat"
        workdir.mkdir()
        home = resident_mod.resident_home(workdir, "ares")
        try:
            assert _run(workdir, "hello", "--timeout", "1") == 0
            _wait_until(lambda: child_file.is_file(), timeout=5)
            child_pid = int(child_file.read_text(encoding="utf-8"))
            _wait_until(
                lambda: (
                    resident_mod.read_live_pid(home) is None
                    and not resident_mod.pid_alive(child_pid)
                ),
                timeout=15,
            )
        finally:
            _stop(workdir, "ares")
            if child_file.is_file():
                try:
                    os.kill(int(child_file.read_text(encoding="utf-8")), signal.SIGKILL)
                except OSError:
                    pass


def _memory_exit(home: Path) -> int | None:
    turns = list(home.rglob("turns/*.json"))
    if not turns:
        return None
    return json.loads(turns[0].read_text(encoding="utf-8")).get("exit")


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_a_child_that_handles_sigterm_exits_before_sigkill(tmp_path):
    import subprocess

    clean = tmp_path / "clean"
    ready = tmp_path / "ready"
    script = tmp_path / "leader.py"
    script.write_text(
        "import os, signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"clean = Path({str(clean)!r})\n"
        "subprocess.Popen([sys.executable, '-c',\n"
        "    'import signal, sys, time\\n'\n"
        "    'from pathlib import Path\\n'\n"
        "    'def on_term(signum, frame):\\n'\n"
        "    '    time.sleep(1)\\n'\n"
        "    '    Path(sys.argv[1]).write_text(\"clean\")\\n'\n"
        "    '    raise SystemExit(0)\\n'\n"
        "    'signal.signal(signal.SIGTERM, on_term)\\n'\n"
        "    'Path(sys.argv[2]).write_text(\"ready\")\\n'\n"
        "    'deadline = time.monotonic() + 3600\\n'\n"
        "    'while time.monotonic() < deadline: time.sleep(0.05)\\n',\n"
        f"    str(clean), {str(ready)!r}])\n"
        "signal.signal(signal.SIGTERM, lambda *_a: os._exit(0))\n"
        "deadline = time.monotonic() + 3600\n"
        "while time.monotonic() < deadline:\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(script)], stdin=subprocess.PIPE,
        start_new_session=True, text=True,
    )
    try:
        _wait_until(lambda: ready.is_file(), timeout=5)
        resident_mod.stop_engine(proc, group=proc.pid, grace_seconds=0.2)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert clean.read_text(encoding="utf-8") == "clean"


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_a_child_that_ignores_sigterm_is_killed_after_two_seconds(tmp_path):
    import subprocess
    import time

    term = tmp_path / "term"
    child_ready = tmp_path / "child.ready"
    leader_ready = tmp_path / "leader.ready"
    script = tmp_path / "leader.py"
    script.write_text(
        "import os, signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c',\n"
        "    'import os, signal, sys, time\\n'\n"
        "    'from pathlib import Path\\n'\n"
        "    'signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n'\n"
        "    'p = Path(sys.argv[1])\\n'\n"
        "    'tmp = p.with_name(p.name + \".\" + str(os.getpid()) + \".tmp\")\\n'\n"
        "    'tmp.write_text(str(os.getpid()))\\n'\n"
        "    'os.replace(tmp, p)\\n'\n"
        "    'deadline = time.monotonic() + 3600\\n'\n"
        "    'while time.monotonic() < deadline: time.sleep(0.05)\\n',\n"
        f"    {str(child_ready)!r}])\n"
        "def on_term(*_a):\n"
        f"    Path({str(term)!r}).write_text(str(time.time()))\n"
        "    os._exit(0)\n"
        "signal.signal(signal.SIGTERM, on_term)\n"
        f"Path({str(leader_ready)!r}).write_text('ready')\n"
        "deadline = time.monotonic() + 3600\n"
        "while time.monotonic() < deadline:\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(script)], stdin=subprocess.PIPE,
        start_new_session=True, text=True,
    )
    child_pid = None
    try:
        _wait_until(
            lambda: child_ready.is_file() and leader_ready.is_file(), timeout=5,
        )
        child_pid = int(child_ready.read_text(encoding="utf-8"))
        resident_mod.stop_engine(proc, group=proc.pid, grace_seconds=0.2)
        _wait_until(lambda: not resident_mod.pid_alive(child_pid), timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
        if child_pid:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except OSError:
                pass
    stamped = float(term.read_text(encoding="utf-8"))
    assert 1.5 <= time.time() - stamped <= 3.5


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_a_zombie_leader_does_not_hold_the_group_wait(tmp_path):
    """A leader that exits on SIGTERM is reaped in the poll, so the stop
    does not wait the full 2 s for a group that only holds a zombie."""
    import subprocess
    import time

    script = tmp_path / "leader.py"
    ready = tmp_path / "ready"
    script.write_text(
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, lambda *_a: os._exit(0))\n"
        f"Path({str(ready)!r}).write_text('ready')\n"
        "deadline = time.monotonic() + 3600\n"
        "while time.monotonic() < deadline:\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(script)], stdin=subprocess.PIPE,
        start_new_session=True, text=True,
    )
    try:
        _wait_until(lambda: ready.is_file(), timeout=5)
        started = time.monotonic()
        resident_mod.stop_engine(proc, group=proc.pid, grace_seconds=0.2)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0
    assert time.monotonic() - started < 1.5


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_a_process_that_exits_after_eof_is_not_signaled(tmp_path):
    import subprocess
    import time

    marker = tmp_path / "signaled"
    script = tmp_path / "exit-after-eof.py"
    script.write_text(
        "import signal, sys, threading, time\n"
        "from pathlib import Path\n"
        "def on_term(*_a):\n"
        f"    Path({str(marker)!r}).write_text('signaled')\n"
        "    raise SystemExit(1)\n"
        "signal.signal(signal.SIGTERM, on_term)\n"
        "done = threading.Event()\n"
        "def _read():\n"
        "    sys.stdin.read()\n"
        "    done.set()\n"
        "threading.Thread(target=_read, daemon=True).start()\n"
        "while not done.is_set():\n"
        "    time.sleep(0.05)\n"
        "deadline = time.monotonic() + 5\n"
        "while time.monotonic() < deadline:\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(script)], stdin=subprocess.PIPE, text=True,
    )
    started = time.monotonic()
    resident_mod.stop_engine(proc, grace_seconds=resident_mod.EOF_GRACE_SECONDS)
    assert not marker.exists()
    assert proc.returncode == 0
    assert time.monotonic() - started < 8


@pytest.mark.skipif(os.name != "posix", reason="process signals are POSIX")
def test_a_reaped_engine_is_not_killed(tmp_path, monkeypatch):
    """An engine that exits on EOF is not passed to os.kill after the reap."""
    import subprocess
    import time

    script = tmp_path / "exit-on-eof.py"
    script.write_text("import sys\nsys.stdin.read()\n", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(script)], stdin=subprocess.PIPE, text=True,
    )
    killed: list[tuple] = []
    real_kill = os.kill

    def spy(pid, sig):
        killed.append((pid, sig, proc.returncode))
        return real_kill(pid, sig)

    monkeypatch.setattr(os, "kill", spy)
    started = time.monotonic()
    try:
        resident_mod.stop_engine(proc, grace_seconds=resident_mod.EOF_GRACE_SECONDS)
    finally:
        if proc.poll() is None:
            real_kill(proc.pid, signal.SIGKILL)
    assert proc.returncode == 0
    assert time.monotonic() - started < 3
    assert not any(sig not in (0, None) for _pid, sig, _rc in killed)
    assert not any(rc is not None and sig for _pid, sig, rc in killed)


def _cursor_spec(tmp_path: Path, fake: Path, mem: Path | None, timeout: int) -> dict:
    spec = {
        "engine": "cursor",
        "dir_path": str(tmp_path),
        "template": [str(fake), "-p", "{prompt}"],
        "agent": "ares",
        "timeout": timeout,
        "scaffold": False,
        "memory": "on",
        "memory_home": str(mem) if mem is not None else None,
    }
    return spec


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_a_quiet_expiry_records_a_zero_memory_exit(monkeypatch, tmp_path):
    import threading

    import engine_memory

    monkeypatch.setattr(engine_memory, "kick", lambda home: None)
    monkeypatch.setenv("FAKE_CURSOR_IGNORE_EOF", "1")
    monkeypatch.setenv("FAKE_CURSOR_TURN_SECONDS", "0.05")
    fake = _fake_agent(tmp_path)
    home = tmp_path / "resident"
    mem = tmp_path / "mem"
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "hello")
    resident_mod._run_session(
        home, _cursor_spec(tmp_path, fake, mem, 1), threading.Event(), {},
    )
    assert _memory_exit(mem) == 0
    assert list(mem.rglob("queue/*.json"))


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_a_quiet_expiry_ignores_the_143_the_bridge_sent(monkeypatch, tmp_path):
    """agent acp exits 143 on SIGTERM. That signal is the bridge's, so the
    quiet expiry still records memory 0."""
    import threading

    import engine_memory

    monkeypatch.setattr(engine_memory, "kick", lambda home: None)
    monkeypatch.setenv("FAKE_CURSOR_IGNORE_EOF", "1")
    monkeypatch.setenv("FAKE_CURSOR_SIGTERM_CODE", "143")
    monkeypatch.setenv("FAKE_CURSOR_TURN_SECONDS", "0.05")
    fake = _fake_agent(tmp_path)
    home = tmp_path / "resident"
    mem = tmp_path / "mem"
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "hello")
    resident_mod._run_session(
        home, _cursor_spec(tmp_path, fake, mem, 1), threading.Event(), {},
    )
    assert _memory_exit(mem) == 0


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_a_leader_that_crashes_and_leaves_a_child_counts_as_its_own_exit(
    monkeypatch, tmp_path,
):
    """The leader is already dead, so the group's signal is not its exit.
    The child it left is still ended."""
    import threading

    child_file = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CURSOR_CHILD", "1")
    monkeypatch.setenv("FAKE_CURSOR_CHILD_PID", str(child_file))
    monkeypatch.setenv("FAKE_CURSOR_CRASH", "1")
    monkeypatch.setenv("FAKE_CURSOR_TURN_SECONDS", "0.05")
    fake = _fake_agent(tmp_path)
    home = tmp_path / "resident"
    mem = tmp_path / "mem"
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "hello")
    child_pid = None
    try:
        resident_mod._run_session(
            home, _cursor_spec(tmp_path, fake, mem, 30), threading.Event(), {},
        )
        assert child_file.is_file()
        child_pid = int(child_file.read_text(encoding="utf-8"))
        _wait_until(lambda: not resident_mod.pid_alive(child_pid), timeout=5)
        assert not resident_mod.pid_alive(child_pid)
        assert _memory_exit(mem) not in (None, 0)
    finally:
        if child_pid and resident_mod.pid_alive(child_pid):
            try:
                os.kill(child_pid, signal.SIGKILL)
            except OSError:
                pass


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_a_fatal_frame_after_eof_records_a_nonzero_memory_exit(monkeypatch, tmp_path):
    import threading

    monkeypatch.setenv("FAKE_CURSOR_IGNORE_EOF", "1")
    monkeypatch.setenv("FAKE_CURSOR_FATAL_AFTER_EOF", "1")
    monkeypatch.setenv("FAKE_CURSOR_TURN_SECONDS", "0.05")
    fake = _fake_agent(tmp_path)
    home = tmp_path / "resident"
    mem = tmp_path / "mem"
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "hello")
    resident_mod._run_session(
        home, _cursor_spec(tmp_path, fake, mem, 1), threading.Event(), {},
    )
    assert _memory_exit(mem) not in (None, 0)
    assert list(mem.rglob("queue/*.json")) == []


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_a_stop_during_a_turn_records_a_nonzero_memory_exit(monkeypatch, tmp_path):
    import threading

    record = tmp_path / "rec.json"
    monkeypatch.setenv("FAKE_CURSOR_IGNORE_EOF", "1")
    monkeypatch.setenv("FAKE_CURSOR_TURN_SECONDS", "30")
    monkeypatch.setenv("FAKE_CURSOR_RECORD", str(record))
    fake = _fake_agent(tmp_path)
    home = tmp_path / "resident"
    mem = tmp_path / "mem"
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "hello")
    stop = threading.Event()

    def end_once_the_prompt_is_recorded() -> None:
        # turns == 1 also counts a message held before the handshake ends.
        # Stop only once the fake has the prompt, so session/cancel is due.
        def prompted() -> bool:
            return any(
                isinstance(msg, dict) and msg.get("method") == "session/prompt"
                for msg in (_rec(record).get("in") or [])
            )

        _wait_until(prompted)
        stop.set()

    threading.Thread(target=end_once_the_prompt_is_recorded, daemon=True).start()
    resident_mod._run_session(
        home, _cursor_spec(tmp_path, fake, mem, 30), stop, {},
    )
    assert _memory_exit(mem) not in (None, 0)
    _wait_until(
        lambda: any(
            event.get("method") == "session/cancel"
            for event in (_rec(record).get("events") or [])
        ),
        timeout=3,
    )


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_a_cancelled_reply_after_session_cancel_is_a_stop(
    monkeypatch, tmp_path, capsys,
):
    """agent acp answers session/cancel with stopReason cancelled. That turn
    is the stop, not a clean completion: non-zero memory, turns 0, one
    dropped line whose cause is stop."""
    import threading

    record = tmp_path / "rec.json"
    monkeypatch.setenv("FAKE_CURSOR_IGNORE_EOF", "1")
    monkeypatch.setenv("FAKE_CURSOR_TURN_SECONDS", "30")
    monkeypatch.setenv("FAKE_CURSOR_CANCEL_STOP", "1")
    monkeypatch.setenv("FAKE_CURSOR_RECORD", str(record))
    fake = _fake_agent(tmp_path)
    home = tmp_path / "resident"
    mem = tmp_path / "mem"
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "hello")
    stop = threading.Event()

    def end_once_the_prompt_is_recorded() -> None:
        def prompted() -> bool:
            return any(
                isinstance(msg, dict) and msg.get("method") == "session/prompt"
                for msg in (_rec(record).get("in") or [])
            )

        _wait_until(prompted)
        stop.set()

    threading.Thread(target=end_once_the_prompt_is_recorded, daemon=True).start()
    resident_mod._run_session(
        home, _cursor_spec(tmp_path, fake, mem, 30), stop, {},
    )
    out = capsys.readouterr().out
    assert _memory_exit(mem) not in (None, 0)
    assert (resident_mod.read_state(home) or {}).get("turns", 0) == 0
    assert out.count("dropped ") == 1
    assert "dropped 1 held message(s): stop" in out

    def cancelled() -> bool:
        events = _rec(record).get("events") or []
        return any(
            event.get("method") == "session/cancel" for event in events
        ) and any(event.get("stopReason") == "cancelled" for event in events)

    _wait_until(cancelled, timeout=3)


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_an_exit_without_a_reply_drops_the_turn(monkeypatch, tmp_path, capsys):
    import threading

    monkeypatch.setenv("FAKE_CURSOR_EXIT_ON_PROMPT", "0")
    fake = _fake_agent(tmp_path)
    home = tmp_path / "resident"
    mem = tmp_path / "mem"
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "hello")
    resident_mod._run_session(
        home, _cursor_spec(tmp_path, fake, mem, 30), threading.Event(), {},
    )
    out = capsys.readouterr().out
    assert _memory_exit(mem) not in (None, 0)
    assert (resident_mod.read_state(home) or {}).get("turns", 0) == 0
    assert out.count("dropped ") == 1
    assert "dropped 1 held message(s): engine exit" in out


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_a_crash_before_the_reply_drops_the_turn(monkeypatch, tmp_path, capsys):
    import threading

    monkeypatch.setenv("FAKE_CURSOR_EXIT_ON_PROMPT", "1")
    fake = _fake_agent(tmp_path)
    home = tmp_path / "resident"
    mem = tmp_path / "mem"
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "hello")
    resident_mod._run_session(
        home, _cursor_spec(tmp_path, fake, mem, 30), threading.Event(), {},
    )
    out = capsys.readouterr().out
    assert _memory_exit(mem) not in (None, 0)
    assert (resident_mod.read_state(home) or {}).get("turns", 0) == 0
    assert out.count("dropped ") == 1
    assert "dropped 1 held message(s): engine exit" in out


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_a_fatal_frame_records_a_nonzero_memory_exit(monkeypatch, tmp_path):
    import threading

    monkeypatch.setenv("FAKE_CURSOR_MALFORMED", "1")
    monkeypatch.setenv("FAKE_CURSOR_IGNORE_SIGTERM", "1")
    fake = _fake_agent(tmp_path)
    home = tmp_path / "resident"
    mem = tmp_path / "mem"
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "hello")
    resident_mod._run_session(
        home, _cursor_spec(tmp_path, fake, mem, 30), threading.Event(), {},
    )
    assert _memory_exit(mem) not in (None, 0)
    assert list(mem.rglob("queue/*.json")) == []


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_a_stop_the_bridge_chose_records_a_zero_memory_exit(monkeypatch, tmp_path):
    import threading

    import engine_memory

    monkeypatch.setattr(engine_memory, "kick", lambda home: None)
    record = tmp_path / "rec.json"
    monkeypatch.setenv("FAKE_CURSOR_IGNORE_EOF", "1")
    monkeypatch.setenv("FAKE_CURSOR_TURN_SECONDS", "0.05")
    monkeypatch.setenv("FAKE_CURSOR_RECORD", str(record))
    fake = _fake_agent(tmp_path)
    home = tmp_path / "resident"
    mem = tmp_path / "mem"
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "hello")
    stop = threading.Event()

    def end_after_the_turn() -> None:
        _wait_until(lambda: _rec(record).get("turns"), timeout=10)
        stop.set()

    threading.Thread(target=end_after_the_turn, daemon=True).start()
    resident_mod._run_session(
        home, _cursor_spec(tmp_path, fake, mem, 30), stop, {},
    )
    assert _memory_exit(mem) == 0


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_a_missed_handshake_records_a_nonzero_memory_exit(monkeypatch, tmp_path):
    import threading

    import engines.acp as acp_mod

    monkeypatch.setattr(acp_mod, "HANDSHAKE_DEADLINE_SECONDS", 0.4)
    hang = tmp_path / "hang-acp"
    hang.write_text(
        f"#!{sys.executable}\nimport time\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    hang.chmod(hang.stat().st_mode | stat.S_IEXEC)
    home = tmp_path / "resident"
    mem = tmp_path / "mem"
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "hello")
    spec = _cursor_spec(tmp_path, hang, mem, 30)
    spec["template"] = [str(hang)]
    resident_mod._run_session(home, spec, threading.Event(), {})
    assert _memory_exit(mem) not in (None, 0)


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_an_engine_crash_records_a_nonzero_memory_exit(monkeypatch, tmp_path):
    import threading

    monkeypatch.setenv("FAKE_CURSOR_CRASH", "1")
    fake = _fake_agent(tmp_path)
    home = tmp_path / "resident"
    mem = tmp_path / "mem"
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "hello")
    resident_mod._run_session(
        home, _cursor_spec(tmp_path, fake, mem, 30), threading.Event(), {},
    )
    assert _memory_exit(mem) not in (None, 0)


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_a_closed_stdin_drops_the_held_turn(monkeypatch, tmp_path, capsys):
    import threading
    import time

    monkeypatch.setenv("FAKE_CURSOR_CLOSE_STDIN", "1")
    monkeypatch.setenv("FAKE_CURSOR_IGNORE_SIGTERM", "1")
    fake = _fake_agent(tmp_path)
    home = tmp_path / "resident"
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "hello")
    spec = _cursor_spec(tmp_path, fake, None, 30)
    spec["memory"] = "off"
    budget = (
        resident_mod.ACP_STOP_GRACE_SECONDS
        + resident_mod.ACP_STOP_TERM_SECONDS
        + 4
    )
    done = threading.Thread(
        target=resident_mod._run_session,
        args=(home, spec, threading.Event(), {}),
    )
    started = time.monotonic()
    done.start()
    done.join(budget)
    if done.is_alive():
        group = (resident_mod.read_state(home) or {}).get("engine_group")
        if isinstance(group, int) and group > 0:
            try:
                os.killpg(group, signal.SIGKILL)
            except OSError:
                pass
        done.join(3)
    out = capsys.readouterr().out
    assert not done.is_alive()
    assert "dropped 1 held message(s)" in out
    assert time.monotonic() - started < budget
    state = resident_mod.read_state(home) or {}
    assert state.get("turns", 0) == 0
    assert "sending wrap-up" not in out


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_stop_reaches_the_engine_group_after_the_bridge_dies(monkeypatch, tmp_path):
    child_file = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CURSOR_IGNORE_EOF", "1")
    monkeypatch.setenv("FAKE_CURSOR_IGNORE_SIGTERM", "1")
    monkeypatch.setenv("FAKE_CURSOR_CHILD", "1")
    monkeypatch.setenv("FAKE_CURSOR_CHILD_PID", str(child_file))
    monkeypatch.setenv("FAKE_CURSOR_TURN_SECONDS", "30")
    _patch_cursor(monkeypatch, tmp_path)
    workdir = tmp_path / "seat"
    workdir.mkdir()
    home = resident_mod.resident_home(workdir, "ares")
    engine_pid = None
    child_pid = None
    try:
        assert resident_mod.enqueue_or_start(
            "cursor", "hello", dir_path=workdir, template=_template(),
            model="composer-2.5", effort=None, agent="ares", timeout=30,
            scaffold=False,
        ) == 0

        def ready():
            state = resident_mod.read_state(home) or {}
            return bool(state.get("engine_group")) and child_file.is_file()

        _wait_until(ready, timeout=10)
        state = resident_mod.read_state(home) or {}
        engine_pid = state["engine_pid"]
        child_pid = int(child_file.read_text(encoding="utf-8"))
        bridge = resident_mod.read_live_pid(home)
        assert bridge
        os.kill(bridge, signal.SIGKILL)
        os.waitpid(bridge, 0)
        _wait_until(lambda: resident_mod.read_live_pid(home) is None, timeout=5)
        resident_mod.stop_resident(workdir, "ares", grace_seconds=0.2)
        _wait_until(
            lambda: (
                not resident_mod.pid_alive(engine_pid)
                and not resident_mod.pid_alive(child_pid)
            ),
            timeout=5,
        )
    finally:
        _stop(workdir, "ares")
        for pid in (engine_pid, child_pid):
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass


def _spawn_dead_leader_group(tmp_path: Path) -> tuple[int, int]:
    """A group whose leader has exited. The member ignores SIGTERM."""
    import subprocess

    member_file = tmp_path / "member.pid"
    child = tmp_path / "member.py"
    child.write_text(
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"p = Path({str(member_file)!r})\n"
        "tmp = p.with_name(p.name + '.' + str(os.getpid()) + '.tmp')\n"
        "tmp.write_text(str(os.getpid()))\n"
        "os.replace(tmp, p)\n"
        "deadline = time.monotonic() + 120\n"
        "while time.monotonic() < deadline:\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    script = tmp_path / "leader.py"
    script.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(child)!r}])\n"
        "time.sleep(0.4)\n",
        encoding="utf-8",
    )
    leader = subprocess.Popen([sys.executable, str(script)], start_new_session=True)
    try:
        _wait_until(lambda: member_file.is_file(), timeout=5)
        member = int(member_file.read_text(encoding="utf-8"))
        leader.wait(timeout=5)
    except Exception:
        try:
            os.killpg(leader.pid, signal.SIGKILL)
        except OSError:
            leader.kill()
        raise
    return leader.pid, member


def _publish_dead_bridge(home: Path, engine_pid: int, token: str, group: int) -> None:
    import subprocess

    home.mkdir(parents=True, exist_ok=True)
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    assert not resident_mod.pid_alive(gone.pid)
    (home / "state.json").write_text(
        json.dumps({
            "pid": gone.pid,
            "engine_pid": engine_pid,
            "engine_start": token,
            "status": "running",
            "turns": 0,
            "engine_group": group,
        }),
        encoding="utf-8",
    )
    (home / "pid").write_text(f"{gone.pid}\n", encoding="utf-8")


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
@pytest.mark.skipif(
    not Path("/proc/sys/kernel/random/boot_id").is_file(),
    reason="the boot id check is Linux",
)
def test_a_dead_leaders_group_from_another_boot_is_left_alone(tmp_path):
    group, member = _spawn_dead_leader_group(tmp_path)
    workdir = tmp_path / "seat"
    home = resident_mod.resident_home(workdir, "ares")
    try:
        assert os.getpgid(member) == group
        _publish_dead_bridge(
            home, group, "00000000-0000-0000-0000-000000000000 12345", group,
        )
        resident_mod.stop_resident(workdir, "ares", grace_seconds=0.2)
        assert resident_mod.pid_alive(member)
    finally:
        if resident_mod.pid_alive(member):
            os.kill(member, signal.SIGKILL)


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
@pytest.mark.skipif(
    not Path("/proc/sys/kernel/random/boot_id").is_file(),
    reason="the boot id check is Linux",
)
def test_a_dead_leaders_group_from_this_boot_is_signaled(tmp_path):
    group, member = _spawn_dead_leader_group(tmp_path)
    workdir = tmp_path / "seat"
    home = resident_mod.resident_home(workdir, "ares")
    boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    try:
        assert os.getpgid(member) == group
        _publish_dead_bridge(home, group, f"{boot} 1", group)
        resident_mod.stop_resident(workdir, "ares", grace_seconds=0.2)
        _wait_until(lambda: not resident_mod.pid_alive(member), timeout=5)
    finally:
        if resident_mod.pid_alive(member):
            os.kill(member, signal.SIGKILL)


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_a_new_bridge_ends_the_engine_a_dead_bridge_left(monkeypatch, tmp_path):
    """SIGKILL the bridge, send a message, then stop: both engines are gone."""
    child_file = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CURSOR_IGNORE_EOF", "1")
    monkeypatch.setenv("FAKE_CURSOR_IGNORE_SIGTERM", "1")
    monkeypatch.setenv("FAKE_CURSOR_CHILD", "1")
    monkeypatch.setenv("FAKE_CURSOR_CHILD_PID", str(child_file))
    monkeypatch.setenv("FAKE_CURSOR_TURN_SECONDS", "30")
    _patch_cursor(monkeypatch, tmp_path)
    workdir = tmp_path / "seat"
    workdir.mkdir()
    home = resident_mod.resident_home(workdir, "ares")
    old_engine = None
    new_engine = None
    child_pid = None
    try:
        assert resident_mod.enqueue_or_start(
            "cursor", "hello", dir_path=workdir, template=_template(),
            model="composer-2.5", effort=None, agent="ares", timeout=30,
            scaffold=False,
        ) == 0
        _wait_until(
            lambda: bool((resident_mod.read_state(home) or {}).get("engine_group"))
            and child_file.is_file(),
            timeout=10,
        )
        state = resident_mod.read_state(home) or {}
        old_engine = state["engine_pid"]
        child_pid = int(child_file.read_text(encoding="utf-8"))
        bridge = resident_mod.read_live_pid(home)
        assert bridge
        os.kill(bridge, signal.SIGKILL)
        os.waitpid(bridge, 0)
        _wait_until(lambda: resident_mod.read_live_pid(home) is None, timeout=5)
        assert resident_mod.pid_alive(old_engine)
        child_file.unlink()
        assert resident_mod.enqueue_or_start(
            "cursor", "again", dir_path=workdir, template=_template(),
            model="composer-2.5", effort=None, agent="ares", timeout=30,
            scaffold=False,
        ) == 0
        _wait_until(lambda: not resident_mod.pid_alive(old_engine), timeout=5)
        _wait_until(lambda: not resident_mod.pid_alive(child_pid), timeout=5)

        def next_engine():
            pid = (resident_mod.read_state(home) or {}).get("engine_pid")
            if isinstance(pid, int) and pid != old_engine:
                return pid
            return None

        new_engine = _wait_until(next_engine, timeout=10)
        resident_mod.stop_resident(workdir, "ares", grace_seconds=0.2)
        _wait_until(lambda: not resident_mod.pid_alive(new_engine), timeout=5)
        assert not resident_mod.pid_alive(old_engine)
    finally:
        _stop(workdir, "ares")
        for pid in (old_engine, new_engine, child_pid):
            if pid and resident_mod.pid_alive(pid):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass


def _stale_record(home: Path, engine_pid: int, token: str, group: int | None) -> None:
    import subprocess

    home.mkdir(parents=True, exist_ok=True)
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    assert not resident_mod.pid_alive(gone.pid)
    state = {
        "pid": gone.pid,
        "engine_pid": engine_pid,
        "engine_start": token,
        "status": "running",
        "turns": 0,
    }
    if group is not None:
        state["engine_group"] = group
    (home / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (home / "pid").write_text(f"{gone.pid}\n", encoding="utf-8")


def test_a_new_bridge_leaves_a_live_pid_alone_without_a_start_token_reader(
    monkeypatch, tmp_path,
):
    """No reader, as on Windows. enqueue must not end a pid it cannot prove."""
    import subprocess
    import time

    victim = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=os.name == "posix",
    )
    try:
        home = resident_mod.resident_home(tmp_path / "seat", "ares")
        _stale_record(home, victim.pid, "recycled", None)
        monkeypatch.setattr(resident_mod, "_has_start_token_reader", lambda: False)
        monkeypatch.setattr(resident_mod, "process_start_token", lambda pid: None)
        real_popen = subprocess.Popen

        class _Bridge:
            def __init__(self, *args, **kwargs):
                gone = real_popen([sys.executable, "-c", "pass"])
                gone.wait()
                self.pid = gone.pid

        monkeypatch.setattr(resident_mod.subprocess, "Popen", _Bridge)
        workdir = tmp_path / "seat"
        workdir.mkdir(exist_ok=True)
        assert resident_mod.enqueue_or_start(
            "cursor", "hello", dir_path=workdir, template=_template(),
            model="composer-2.5", effort=None, agent="ares", timeout=30,
            scaffold=False,
        ) == 0
        time.sleep(0.4)
        assert victim.poll() is None
    finally:
        if victim.poll() is None:
            victim.kill()
            victim.wait(timeout=2)


def test_stop_still_signals_a_recorded_pid_without_a_start_token_reader(
    monkeypatch, tmp_path,
):
    """stop keeps the pid-only signal it had before a reader existed."""
    import subprocess

    victim = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=os.name == "posix",
    )
    try:
        home = resident_mod.resident_home(tmp_path / "seat", "ares")
        _stale_record(home, victim.pid, "recycled", None)
        monkeypatch.setattr(resident_mod, "_has_start_token_reader", lambda: False)
        monkeypatch.setattr(resident_mod, "process_start_token", lambda pid: None)
        resident_mod.stop_resident(tmp_path / "seat", "ares", grace_seconds=0.2)
        _wait_until(lambda: victim.poll() is not None, timeout=5)
    finally:
        if victim.poll() is None:
            victim.kill()
            victim.wait(timeout=2)


@pytest.mark.skipif(os.name != "posix", reason="process signals are POSIX")
def test_a_live_process_under_a_stale_record_is_left_alone(tmp_path):
    import subprocess
    import time

    for name, with_group in (("group", True), ("pid", False)):
        victim = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        try:
            real = resident_mod.process_start_token(victim.pid)
            if not real:
                pytest.skip("no start token on this platform")
            stale = real.split()[0] + " 1"
            assert stale != real
            home = resident_mod.resident_home(tmp_path / name, "ares")
            _stale_record(home, victim.pid, stale, victim.pid if with_group else None)
            resident_mod.stop_resident(tmp_path / name, "ares", grace_seconds=0.2)
            time.sleep(0.6)
            assert victim.poll() is None
        finally:
            if victim.poll() is None:
                victim.kill()
                victim.wait(timeout=2)


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_the_start_token_is_read_once_at_spawn(monkeypatch, tmp_path):
    import threading

    calls: list[tuple] = []
    real = resident_mod.process_start_token

    def wrapped(pid):
        alive = bool(pid) and resident_mod.pid_alive(pid)
        token = real(pid)
        calls.append((pid, alive, token))
        return token

    monkeypatch.setattr(resident_mod, "process_start_token", wrapped)
    monkeypatch.setenv("FAKE_CURSOR_EXIT_AFTER_PROMPT", "1")
    monkeypatch.setenv("FAKE_CURSOR_TURN_SECONDS", "0.05")
    _patch_cursor(monkeypatch, tmp_path)
    workdir = tmp_path / "seat"
    workdir.mkdir()
    home = resident_mod.resident_home(workdir, "ares")
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "hello")
    spec = {
        "engine": "cursor",
        "dir_path": str(workdir),
        "template": _template(),
        "agent": "ares",
        "timeout": 30,
        "scaffold": False,
        "memory": "off",
    }
    resident_mod._run_session(home, spec, threading.Event(), {})
    assert len(calls) == 1
    pid, alive, token = calls[0]
    assert alive
    assert token
    state = resident_mod.read_state(home) or {}
    assert state.get("engine_start") == token
    assert state.get("engine_pid") == pid


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_a_child_dies_after_the_engine_exits_on_eof(monkeypatch, tmp_path):
    """The bridge dies, the engine exits on that EOF, and stop ends its child."""
    child_file = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CURSOR_CHILD", "1")
    monkeypatch.setenv("FAKE_CURSOR_CHILD_PID", str(child_file))
    monkeypatch.setenv("FAKE_CURSOR_TURN_SECONDS", "0.05")
    _patch_cursor(monkeypatch, tmp_path)
    workdir = tmp_path / "seat"
    workdir.mkdir()
    home = resident_mod.resident_home(workdir, "ares")
    engine_pid = None
    child_pid = None
    try:
        assert resident_mod.enqueue_or_start(
            "cursor", "hello", dir_path=workdir, template=_template(),
            model="composer-2.5", effort=None, agent="ares", timeout=30,
            scaffold=False,
        ) == 0
        seen_turn = {"up": False}

        def idle_after_a_turn():
            state = resident_mod.read_state(home) or {}
            if state.get("turns"):
                seen_turn["up"] = True
            return (
                seen_turn["up"]
                and state.get("turns") == 0
                and state.get("engine_group")
                and child_file.is_file()
            )

        _wait_until(idle_after_a_turn, timeout=10)
        state = resident_mod.read_state(home) or {}
        engine_pid = state["engine_pid"]
        child_pid = int(child_file.read_text(encoding="utf-8"))
        bridge = resident_mod.read_live_pid(home)
        assert bridge
        os.kill(bridge, signal.SIGKILL)
        os.waitpid(bridge, 0)
        _wait_until(lambda: not resident_mod.pid_alive(engine_pid), timeout=5)
        assert resident_mod.pid_alive(child_pid)
        resident_mod.stop_resident(workdir, "ares", grace_seconds=0.2)
        _wait_until(lambda: not resident_mod.pid_alive(child_pid), timeout=5)
    finally:
        _stop(workdir, "ares")
        for pid in (engine_pid, child_pid):
            if pid and resident_mod.pid_alive(pid):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_a_failed_write_on_the_main_thread_drops_the_turn(monkeypatch, tmp_path, capsys):
    import threading
    import time

    record = _patch_cursor(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_CURSOR_CLOSE_AFTER_TURN", "1")
    monkeypatch.setenv("FAKE_CURSOR_TURN_SECONDS", "0.05")
    workdir = tmp_path / "seat"
    workdir.mkdir()
    home = resident_mod.resident_home(workdir, "ares")
    (home / "inbox").mkdir(parents=True)
    resident_mod._enqueue_message(home, "first")
    spec = {
        "engine": "cursor",
        "dir_path": str(workdir),
        "template": _template(),
        "agent": "ares",
        "timeout": 60,
        "scaffold": False,
        "memory": "off",
    }
    done = threading.Thread(
        target=resident_mod._run_session,
        args=(home, spec, threading.Event(), {}),
    )
    started = time.monotonic()
    done.start()
    ended_itself = False
    try:
        def first_turn_done():
            state = resident_mod.read_state(home) or {}
            return _rec(record).get("stdin_closed") and state.get("turns") == 0

        _wait_until(first_turn_done, timeout=10)
        resident_mod._enqueue_message(home, "second")
        done.join(timeout=8)
        # The rescue stop below also drops the turn. The session has to
        # end on its own first, or a non-fatal write still passes.
        ended_itself = not done.is_alive()
    finally:
        if done.is_alive():
            _stop(workdir, "ares")
            done.join(timeout=3)
    out = capsys.readouterr().out
    assert ended_itself
    assert not done.is_alive()
    assert "resident engine closed stdin during a turn" in out
    assert "dropped 1 held message(s): failed write" in out
    state = resident_mod.read_state(home) or {}
    assert state.get("turns", 0) == 0
    assert time.monotonic() - started < 8


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_an_idle_stop_sends_no_session_cancel(monkeypatch, tmp_path):
    record = _patch_cursor(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_CURSOR_IGNORE_EOF", "1")
    monkeypatch.setenv("FAKE_CURSOR_TURN_SECONDS", "0.05")
    workdir = tmp_path / "seat"
    workdir.mkdir()
    home = resident_mod.resident_home(workdir, "ares")
    try:
        assert _run(workdir, "hello", "--timeout", "1") == 0
        _wait_until(lambda: resident_mod.read_live_pid(home) is None, timeout=15)
        methods = [msg.get("method") for msg in _rec(record).get("in", [])]
        assert "session/prompt" in methods
        assert "session/cancel" not in methods
    finally:
        _stop(workdir, "ares")


def test_the_fake_rejects_an_option_it_did_not_offer(tmp_path):
    import subprocess

    record = tmp_path / "rec.json"
    env = dict(os.environ)
    env["FAKE_CURSOR_RECORD"] = str(record)
    env["FAKE_CURSOR_PERMISSION"] = "1"
    env["FAKE_CURSOR_PERMISSION_TIMEOUT"] = "2"
    proc = subprocess.Popen(
        [sys.executable, str(FAKE_ACP)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        env=env, text=True,
    )

    def send(msg: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def read() -> dict:
        assert proc.stdout is not None
        return json.loads(proc.stdout.readline())

    try:
        send({"id": 1, "method": "initialize", "params": {}})
        read()
        send({"id": 2, "method": "authenticate", "params": {}})
        read()
        send({"id": 3, "method": "session/new", "params": {}})
        read()
        send({"id": 4, "method": "session/prompt", "params": {
            "prompt": [{"type": "text", "text": "x"}],
        }})
        permission = read()
        assert permission["method"] == "session/request_permission"
        send({"id": permission["id"], "result": {
            "outcome": {"outcome": "selected", "optionId": "not-offered"},
        }})
        _wait_until(
            lambda: _rec(record).get("rejectedOption") == "not-offered",
            timeout=3,
        )
        assert "optionId" not in _rec(record)
    finally:
        proc.kill()
        proc.wait(timeout=2)


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_a_stop_request_cancels_a_busy_cursor_then_signals_at_once(monkeypatch, tmp_path):
    import time

    monkeypatch.setenv("FAKE_CURSOR_TURN_SECONDS", "30")
    monkeypatch.setenv("FAKE_CURSOR_IGNORE_EOF", "1")
    record = _patch_cursor(monkeypatch, tmp_path)
    workdir = tmp_path / "seat"
    workdir.mkdir()
    home = resident_mod.resident_home(workdir, "ares")
    try:
        assert resident_mod.enqueue_or_start(
            "cursor", "hello", dir_path=workdir, template=_template(),
            model="composer-2.5", effort=None, agent="ares", timeout=60,
            scaffold=False,
        ) == 0

        def prompted() -> bool:
            return any(
                isinstance(msg, dict) and msg.get("method") == "session/prompt"
                for msg in (_rec(record).get("in") or [])
            )

        _wait_until(prompted)
        started = time.time()
        assert resident_mod.stop_resident(workdir, "ares") is True

        def stopped() -> bool:
            rec = _rec(record)
            if "sigterm_at" not in rec:
                return False
            return any(
                event.get("method") == "session/cancel"
                for event in (rec.get("events") or [])
            )

        _wait_until(stopped, timeout=3)
        rec = _rec(record)
        assert any(
            isinstance(msg, dict) and msg.get("method") == "session/prompt"
            for msg in (rec.get("in") or [])
        )
        cancel_at = next(
            event["t"]
            for event in rec["events"]
            if event.get("method") == "session/cancel"
        )
        assert cancel_at < rec["sigterm_at"]
        # The cancel wait, then the signal. eof_at is not required: SIGTERM
        # can reap the fake before its reader records stdin closing.
        assert rec["sigterm_at"] - started < resident_mod.ACP_CANCEL_WAIT_SECONDS + 1.0
    finally:
        _stop(workdir, "ares")


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def _stub_memory_engines(monkeypatch, tmp_path: Path) -> Path:
    """PATH stubs for a bridge that runs as its own process.

    The autouse guard patches kick in the pytest process. It does not see
    the bridge. A memory outcome of 0 in that process execs agent, codex,
    claude, or agy from PATH. Each stub writes the marker and exits 1.
    """
    marker = tmp_path / "memory-engine-marker"
    bindir = tmp_path / "memory-stubs"
    bindir.mkdir()
    body = (
        f"#!{sys.executable}\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text(Path(sys.argv[0]).name + '\\n', encoding='utf-8')\n"
        "raise SystemExit(1)\n"
    )
    for name in ("agent", "codex", "claude", "agy"):
        path = bindir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""),
    )
    return marker


@pytest.mark.skipif(os.name != "posix", reason="fake engines are POSIX scripts")
def test_an_engine_that_ignores_sigterm_lets_the_bridge_finish(monkeypatch, tmp_path):
    import time

    monkeypatch.setenv("FAKE_CURSOR_TURN_SECONDS", "30")
    monkeypatch.setenv("FAKE_CURSOR_IGNORE_EOF", "1")
    monkeypatch.setenv("FAKE_CURSOR_IGNORE_SIGTERM", "1")
    marker = _stub_memory_engines(monkeypatch, tmp_path)
    record = _patch_cursor(monkeypatch, tmp_path)
    workdir = tmp_path / "seat"
    workdir.mkdir()
    home = resident_mod.resident_home(workdir, "ares")
    mem = tmp_path / "mem"
    try:
        assert resident_mod.enqueue_or_start(
            "cursor", "hello", dir_path=workdir, template=_template(),
            model="composer-2.5", effort=None, agent="ares", timeout=60,
            scaffold=False, memory="on", memory_home=str(mem),
        ) == 0

        def prompted() -> bool:
            return any(
                isinstance(msg, dict) and msg.get("method") == "session/prompt"
                for msg in (_rec(record).get("in") or [])
            )

        _wait_until(prompted)
        started = time.monotonic()
        assert resident_mod.stop_resident(workdir, "ares") is True
        assert time.monotonic() - started < 8
        log = (home / "resident.log").read_text(encoding="utf-8")
        assert "resident session exited" in log
        assert _memory_exit(mem) is not None
        if list(mem.rglob("queue/*.json")):
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not marker.is_file():
                time.sleep(0.05)
        assert not marker.is_file()
    finally:
        _stop(workdir, "ares")
