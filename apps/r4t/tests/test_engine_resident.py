from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import textwrap
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from engines import resident as resident_mod
from engines import run as engine_run
from ar3.proc import terminate_group
from r4t import main as r4t_main


def make_fake_stream_claude(
    tmp_path: Path, *, sleep_per_turn: float = 0.05, late_frame_after: float = 0.0
) -> tuple[Path, Path]:
    """Create an executable fake `claude` that speaks `--input-format stream-json`
    and `--output-format stream-json`, recording its argv, env, and every received
    stdin frame to `record.json`."""
    record_path = tmp_path / "claude-record.json"
    script_path = tmp_path / "fake-claude"
    code = textwrap.dedent(
        f"""\
        #!{sys.executable}
        import json, os, sys, threading, time
        from pathlib import Path

        record_file = Path({str(record_path)!r})
        workdir = Path.cwd()

        def load_rec():
            if record_file.is_file():
                try:
                    return json.loads(record_file.read_text(encoding="utf-8"))
                except Exception:
                    pass
            return {{"spawns": 0, "argv": [], "env": {{}}, "turns": []}}

        rec = load_rec()
        rec["spawns"] = rec.get("spawns", 0) + 1
        rec["argv"] = sys.argv[1:]
        rec["pid"] = os.getpid()
        rec["env"] = {{k: v for k, v in os.environ.items() if k.startswith(("GIT_", "TELL_"))}}
        record_file.write_text(json.dumps(rec), encoding="utf-8")

        sys.stdout.write(json.dumps({{"type": "system", "subtype": "init", "session_id": "fake-sess"}}) + "\\n")
        sys.stdout.flush()

        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            frame = json.loads(line)
            content = (frame.get("message") or {{}}).get("content", "")
            rec = load_rec()
            rec["turns"].append(content)
            record_file.write_text(json.dumps(rec), encoding="utf-8")

            time.sleep({sleep_per_turn})
            if "wrap up and exit" in content:
                (workdir / "STATUS.md").write_text("# Current State\\nWrapped up cleanly.\\n", encoding="utf-8")
                with (workdir / "LESSONS.md").open("a", encoding="utf-8") as lf:
                    lf.write("- Resident wrap-up lesson\\n")

            reply_text = f"ack turn {{len(rec['turns'])}}"
            sys.stdout.write(json.dumps({{
                "type": "assistant",
                "message": {{"role": "assistant", "content": [{{"type": "text", "text": reply_text}}]}},
            }}) + "\\n")
            sys.stdout.write(json.dumps({{
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": reply_text,
            }}) + "\\n")
            sys.stdout.flush()
            if {late_frame_after} and len(rec["turns"]) == 1:
                # The harness wakes itself (a background task ends) with no turn in flight.
                def _late():
                    sys.stdout.write(json.dumps({{
                        "type": "result", "subtype": "success",
                        "is_error": False, "result": "late",
                    }}) + "\\n")
                    sys.stdout.flush()
                threading.Timer({late_frame_after}, _late).start()
        """
    )
    script_path.write_text(code, encoding="utf-8")
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)
    return script_path, record_path


def make_fake_stream_agy(tmp_path: Path, *, sleep_per_turn: float = 0.05) -> tuple[Path, Path]:
    """Create an executable fake `agy` that speaks `agy`'s `--input-format
    stream-json --output-format stream-json --print=` protocol (`event: "user"`
    in, `event: "init"` / `step_update` / `result` out)."""
    record_path = tmp_path / "agy-record.json"
    script_path = tmp_path / "fake-agy"
    code = textwrap.dedent(
        f"""\
        #!{sys.executable}
        import json, os, sys, time
        from pathlib import Path

        record_file = Path({str(record_path)!r})
        workdir = Path.cwd()

        def load_rec():
            if record_file.is_file():
                try:
                    return json.loads(record_file.read_text(encoding="utf-8"))
                except Exception:
                    pass
            return {{"spawns": 0, "argv": [], "env": {{}}, "turns": []}}

        rec = load_rec()
        rec["spawns"] = rec.get("spawns", 0) + 1
        rec["argv"] = sys.argv[1:]
        rec["pid"] = os.getpid()
        rec["env"] = {{k: v for k, v in os.environ.items() if k.startswith(("GIT_", "TELL_"))}}
        record_file.write_text(json.dumps(rec), encoding="utf-8")

        sys.stdout.write(json.dumps({{
            "event": "init",
            "conversation_id": "fake-agy-conv",
            "init": {{"cwd": str(workdir), "permission_mode": "accept-edits"}},
        }}) + "\\n")
        sys.stdout.flush()

        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            frame = json.loads(line)
            assert frame.get("event") == "user", f"unexpected event: {{frame!r}}"
            content = (frame.get("message") or {{}}).get("content", "")
            rec = load_rec()
            rec["turns"].append(content)
            record_file.write_text(json.dumps(rec), encoding="utf-8")

            time.sleep({sleep_per_turn})
            if "wrap up and exit" in content:
                (workdir / "STATUS.md").write_text("# Current State\\nAgy wrapped up cleanly.\\n", encoding="utf-8")
                with (workdir / "LESSONS.md").open("a", encoding="utf-8") as lf:
                    lf.write("- Agy resident wrap-up lesson\\n")

            reply_text = f"agy ack turn {{len(rec['turns'])}}"
            sys.stdout.write(json.dumps({{
                "event": "step_update",
                "step_update": {{
                    "conversation_id": "fake-agy-conv",
                    "step_index": len(rec["turns"]),
                    "state": "DONE",
                    "step_type": "agent_response",
                    "text_delta": reply_text,
                }},
            }}) + "\\n")
            sys.stdout.write(json.dumps({{
                "event": "result",
                "result": {{
                    "conversation_id": "fake-agy-conv",
                    "status": "SUCCESS",
                    "response": reply_text,
                    "num_turns": len(rec["turns"]),
                }},
            }}) + "\\n")
            sys.stdout.flush()
        """
    )
    script_path.write_text(code, encoding="utf-8")
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)
    return script_path, record_path


def _wait_until(predicate, timeout: float = 10.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        val = predicate()
        if val:
            return val
        time.sleep(interval)
    raise AssertionError("condition not met before timeout")


def _stop(workdir: Path, agent: str) -> None:
    pid = resident_mod.read_live_pid(resident_mod.resident_home(workdir, agent))
    if pid is not None:
        terminate_group(pid)


def _reaped(pid: int) -> bool:
    """The bridge is this test process's own child, so it lingers as a zombie
    (and reads alive) until reaped; the r4t CLI exits at once and never hits it."""
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return not resident_mod.pid_alive(pid)
    return done == pid


def _turns(record_file: Path) -> list[str]:
    if not record_file.is_file():
        return []
    return json.loads(record_file.read_text(encoding="utf-8")).get("turns", [])


class TestComposeResidentArgv:
    def test_injects_stream_json_and_mcp_config_and_allowed_tool(self, tmp_path):
        template, _ = engine_run._build_argv_template(
            "claude",
            model="sonnet",
            timeout=900,
            workdir=tmp_path,
            resident=True,
        )
        composed = resident_mod.compose_resident_argv(
            template,
            {"TELL_OUTBOX_DIR": "/tmp/outbox", "HOME": "/tmp/home"},
            engine="claude",
            mcp_log_path=tmp_path / "mcp.jsonl",
        )
        assert composed[0] == "claude"
        assert "--input-format" in composed
        assert composed[composed.index("--input-format") + 1] == "stream-json"
        assert "--output-format" in composed
        assert composed[composed.index("--output-format") + 1] == "stream-json"
        assert "--verbose" in composed
        assert "-p" in composed
        assert "{prompt}" not in composed

        assert "--mcp-config" in composed
        mcp_payload = json.loads(composed[composed.index("--mcp-config") + 1])
        server = mcp_payload["mcpServers"]["a8s"]
        assert server["args"][-2:] == ["mcp", "serve"]
        assert server["env"]["TELL_OUTBOX_DIR"] == "/tmp/outbox"
        assert server["env"]["A8S_MCP_LOG"] == str(tmp_path / "mcp.jsonl")

        allowed = composed[composed.index("--allowedTools") + 1]
        assert "mcp__a8s__tell" in allowed

    def test_composes_agy_stream_json_argv(self, tmp_path):
        template, _ = engine_run._build_argv_template(
            "agy",
            model=None,
            timeout=900,
            workdir=tmp_path,
            resident=True,
        )
        composed = resident_mod.compose_resident_argv(
            template,
            {"TELL_OUTBOX_DIR": "/tmp/outbox"},
            engine="agy",
        )
        assert composed[0] == "agy"
        assert "--dangerously-skip-permissions" in composed
        assert "--mode" in composed
        assert composed[composed.index("--mode") + 1] == "accept-edits"
        assert "--print-timeout" in template
        assert "--print-timeout" not in composed
        assert "900s" not in composed
        assert "--input-format" in composed
        assert composed[composed.index("--input-format") + 1] == "stream-json"
        assert "--output-format" in composed
        assert composed[composed.index("--output-format") + 1] == "stream-json"
        assert "--print=" in composed
        assert "--print" not in composed
        assert "{prompt}" not in composed

    def test_rejects_non_resident_engines(self, tmp_path):
        for engine in ("cursor", "copilot", "opencode", "muse", "devin"):
            with pytest.raises(engine_run.RunError, match="does not support --resident"):
                engine_run._build_argv_template(
                    engine, model=None, timeout=900, workdir=tmp_path, resident=True
                )

    def test_rejects_continue_and_session_with_resident(self, tmp_path):
        with pytest.raises(engine_run.RunError, match="cannot be combined"):
            engine_run._build_argv_template(
                "claude",
                model=None,
                timeout=900,
                workdir=tmp_path,
                continue_conversation=True,
                resident=True,
            )


class TestResidentBootAndWrapupPrompts:
    def test_boot_prompt_includes_agent_name_mcp_tell_and_status_paths(self, tmp_path):
        prompt = resident_mod.resident_boot_prompt(
            tmp_path,
            "[2026-10-16 12:00 PDT] alice tells bob (1s ago): hello there",
            agent="bob",
            resident_seconds=2700,
            lessons_cap=200,
            lessons_cap_bytes=35840,
        )
        assert "You are 'bob'" in prompt
        assert "a8s_tell" in prompt
        assert "45m of quiet" in prompt
        assert str(tmp_path / "STATUS.md") in prompt
        assert str(tmp_path / "LESSONS.md") in prompt
        assert prompt.endswith("alice tells bob (1s ago): hello there")

    def test_agy_boot_prompt_uses_cli_tell_step(self, tmp_path):
        prompt = resident_mod.resident_boot_prompt(
            tmp_path,
            "[2026-10-16 12:00 PDT] alice tells bob (1s ago): hello agy",
            agent="bob",
            engine="agy",
            resident_seconds=2700,
            lessons_cap=200,
            lessons_cap_bytes=35840,
        )
        assert "You are 'bob'" in prompt
        assert "tell <recipient> - <<'EOF'" in prompt
        assert "45m of quiet" in prompt
        assert prompt.endswith("alice tells bob (1s ago): hello agy")


    def numbered(self, prompt: str) -> list[str]:
        return [line for line in prompt.split("\n\nRouted input:")[0].split("\n")[1:]]

    def lessons_over_the_cap(self, seat: Path) -> Path:
        path = seat / "LESSONS.md"
        path.write_text("".join(f"- lesson {i}\n" for i in range(181)), encoding="utf-8")
        return path

    def test_boot_prompt_shares_the_first_steps_with_the_one_shot_prelude(self, tmp_path):
        one_shot = engine_run.scaffold_prompt(
            tmp_path, "hi", agent="bob", tell_step=engine_run.MCP_TELL_STEP
        )
        resident = resident_mod.resident_boot_prompt(
            tmp_path, "hi", agent="bob",
            lessons_cap=200, lessons_cap_bytes=35840,
        )
        assert resident.startswith("Smart cold boot (resident session):\n")
        assert one_shot.startswith("Smart cold boot:\n")
        assert self.numbered(resident)[:3] == self.numbered(one_shot)[:3]
        assert self.numbered(resident)[2] == f"3. {engine_run.MCP_TELL_STEP}"
        assert "4. Resident session: it stays open while a turn is running" in resident
        assert "Stay idle and exit" in one_shot and "Stay idle and exit" not in resident

    def test_agy_boot_prompt_carries_the_cli_tell_step_in_the_shared_slot(self, tmp_path):
        prompt = resident_mod.resident_boot_prompt(
            tmp_path, "hi", agent="bob", engine="agy",
            lessons_cap=200, lessons_cap_bytes=35840,
        )
        assert self.numbered(prompt)[2] == f"3. {engine_run.CLI_TELL_STEP}"

    def test_boot_prompt_without_an_agent_has_no_tell_step(self, tmp_path):
        prompt = resident_mod.resident_boot_prompt(
            tmp_path, "hi", agent=None,
            lessons_cap=200, lessons_cap_bytes=35840,
        )
        assert "Reply only by message" not in prompt
        assert "a8s convo" not in prompt
        assert "2. Resident session" in prompt

    def test_boot_prompt_carries_the_consolidation_note_when_copies_are_given(
        self, tmp_path
    ):
        lessons = self.lessons_over_the_cap(tmp_path)
        copies = engine_run.prepare_lessons_copies(tmp_path)
        prompt = resident_mod.resident_boot_prompt(
            tmp_path, "hi", agent="bob",
            lessons_cap=200, lessons_cap_bytes=35840, copies=copies,
        )
        head, _, _ = prompt.partition("\n\nRouted input:\n")
        _, _, note = head.partition("\n\n")
        assert note.startswith(f"Consolidate {lessons} before you answer any message. ")
        assert str(copies[0]) in note
        assert prompt.endswith("Routed input:\nhi")

    def test_boot_prompt_prelude_does_not_move_when_the_note_appears(self, tmp_path):
        kwargs = dict(agent="bob", lessons_cap=200, lessons_cap_bytes=35840)
        bare = resident_mod.resident_boot_prompt(tmp_path, "hi", **kwargs)
        self.lessons_over_the_cap(tmp_path)
        copies = engine_run.prepare_lessons_copies(tmp_path)
        loud = resident_mod.resident_boot_prompt(tmp_path, "hi", copies=copies, **kwargs)
        warn = resident_mod.resident_boot_prompt(tmp_path, "hi", copies=None, **kwargs)
        prelude = bare.split("\n\nRouted input:")[0]
        assert "Consolidate" not in bare
        assert loud.startswith(prelude + "\n\nConsolidate ")
        assert warn.startswith(prelude + "\n\n")
        assert "r4t could not save a copy of it" in warn
        assert "Consolidate" not in warn

    def test_wrapup_prompt_carries_the_lesson_line_rule(self, tmp_path):
        prompt = resident_mod.wrapup_prompt(tmp_path, 2700)
        assert engine_run.LESSON_LINE_RULE in prompt
        assert "wrap up and exit" in prompt
        assert "quiet for 45m" in prompt
        assert "Never edit AGENTS.md." in prompt


class TestResidentLifecycle:
    def test_burst_of_messages_reuses_single_claude_process_and_wraps_up_at_expiry(
        self, monkeypatch, tmp_path
    ):
        fake_bin, record_file = make_fake_stream_claude(tmp_path, sleep_per_turn=0.05)
        monkeypatch.setitem(
            engine_run.HARNESS_PRESETS,
            "claude",
            {
                **engine_run.HARNESS_PRESETS["claude"],
                "invoke": [str(fake_bin), "--permission-mode", "dontAsk", "--allowedTools", "Read", "-p", "{prompt}"],
            },
        )

        workdir = tmp_path / "seat"
        workdir.mkdir()
        home = resident_mod.resident_home(workdir, "ares")

        try:
            rc1 = r4t_main([
                "engine", "claude", "run",
                "--resident",
                "--timeout", "2",
                "--dir", str(workdir),
                "--agent", "ares",
                "--git-name", "Ares (agent)",
                "--git-email", "ares@example.com",
                "first message from alice",
            ])
            assert rc1 == 0

            _wait_until(lambda: record_file.is_file() and len(json.loads(record_file.read_text()).get("turns", [])) >= 1)

            rc2 = r4t_main([
                "engine", "claude", "run",
                "--resident",
                "--timeout", "2",
                "--dir", str(workdir),
                "--agent", "ares",
                "second message from bob",
            ])
            assert rc2 == 0

            _wait_until(
                lambda: resident_mod.read_live_pid(home) is None
                and len(json.loads(record_file.read_text()).get("turns", [])) >= 3,
                timeout=10.0,
            )

            rec = json.loads(record_file.read_text(encoding="utf-8"))
            assert rec["spawns"] == 1
            assert rec["env"].get("GIT_AUTHOR_NAME") == "Ares (agent)"
            assert rec["env"].get("GIT_AUTHOR_EMAIL") == "ares@example.com"
            assert len(rec["turns"]) == 3
            assert "first message from alice" in rec["turns"][0]
            assert "a8s_tell" in rec["turns"][0]
            assert "second message from bob" in rec["turns"][1]
            assert "wrap up and exit" in rec["turns"][2]
            assert (workdir / "STATUS.md").is_file()
            assert "Wrapped up cleanly." in (workdir / "STATUS.md").read_text(encoding="utf-8")
            assert (workdir / "LESSONS.md").is_file()
        finally:
            _stop(workdir, "ares")

    def test_burst_of_messages_reuses_single_agy_process_and_wraps_up_at_expiry(
        self, monkeypatch, tmp_path
    ):
        fake_bin, record_file = make_fake_stream_agy(tmp_path, sleep_per_turn=0.05)
        monkeypatch.setitem(
            engine_run.HARNESS_PRESETS,
            "agy",
            {
                **engine_run.HARNESS_PRESETS["agy"],
                "invoke": [
                    str(fake_bin),
                    "--dangerously-skip-permissions",
                    "--mode",
                    "accept-edits",
                    "--print",
                    "{prompt}",
                ],
            },
        )

        workdir = tmp_path / "seat-agy"
        workdir.mkdir()
        home = resident_mod.resident_home(workdir, "vela")

        try:
            rc1 = r4t_main([
                "engine", "agy", "run",
                "--resident",
                "--timeout", "2",
                "--dir", str(workdir),
                "--agent", "vela",
                "--git-name", "Vela (agent)",
                "--git-email", "vela@example.com",
                "first message for vela",
            ])
            assert rc1 == 0

            _wait_until(
                lambda: record_file.is_file()
                and len(json.loads(record_file.read_text()).get("turns", [])) >= 1
            )

            rc2 = r4t_main([
                "engine", "agy", "run",
                "--resident",
                "--timeout", "2",
                "--dir", str(workdir),
                "--agent", "vela",
                "second message for vela",
            ])
            assert rc2 == 0

            _wait_until(
                lambda: resident_mod.read_live_pid(home) is None
                and len(json.loads(record_file.read_text()).get("turns", [])) >= 3,
                timeout=10.0,
            )

            rec = json.loads(record_file.read_text(encoding="utf-8"))
            assert rec["spawns"] == 1
            assert "--input-format" in rec["argv"]
            assert "--output-format" in rec["argv"]
            assert "--print=" in rec["argv"]
            assert rec["env"].get("GIT_AUTHOR_NAME") == "Vela (agent)"
            assert rec["env"].get("GIT_AUTHOR_EMAIL") == "vela@example.com"
            assert len(rec["turns"]) == 3
            assert "first message for vela" in rec["turns"][0]
            assert "tell <recipient> - <<'EOF'" in rec["turns"][0]
            assert "second message for vela" in rec["turns"][1]
            assert "wrap up and exit" in rec["turns"][2]
            assert (workdir / "STATUS.md").is_file()
            assert "Agy wrapped up cleanly." in (workdir / "STATUS.md").read_text(encoding="utf-8")
            assert (workdir / "LESSONS.md").is_file()
        finally:
            _stop(workdir, "vela")

    @pytest.mark.parametrize("scaffold", [True, False])
    def test_a_session_over_the_lessons_cap_saves_a_copy_and_leaves_the_file(
        self, monkeypatch, tmp_path, scaffold
    ):
        fake_bin, record_file = make_fake_stream_claude(tmp_path, sleep_per_turn=0.05)
        monkeypatch.setitem(
            engine_run.HARNESS_PRESETS,
            "claude",
            {**engine_run.HARNESS_PRESETS["claude"], "invoke": [str(fake_bin), "-p", "{prompt}"]},
        )
        workdir = tmp_path / "seat"
        workdir.mkdir()
        original = "".join(f"- lesson {i}\n" for i in range(181)).encode("utf-8")
        (workdir / "LESSONS.md").write_bytes(original)
        home = resident_mod.resident_home(workdir, "ares")
        try:
            assert r4t_main([
                "engine", "claude", "run", "--resident", "--timeout", "1",
                "--dir", str(workdir), "--agent", "ares",
                *([] if scaffold else ["--no-scaffold"]), "first",
            ]) == 0
            _wait_until(lambda: len(_turns(record_file)) >= 1)
            first = _turns(record_file)[0]
            _wait_until(
                lambda: resident_mod.read_live_pid(home) is None
                and len(_turns(record_file)) >= (2 if scaffold else 1),
                timeout=20.0,
            )
            copies = list((workdir / "archive").glob("LESSONS-*.md"))
            if scaffold:
                assert "Consolidate " in first
                [copy] = copies
                assert copy.read_bytes() == original
                assert str(copy) in first
            else:
                assert "Consolidate" not in first
                assert copies == []
            assert (workdir / "LESSONS.md").read_bytes().startswith(original)
        finally:
            _stop(workdir, "ares")

    def _patch_claude(self, monkeypatch, fake_bin):
        monkeypatch.setitem(
            engine_run.HARNESS_PRESETS,
            "claude",
            {**engine_run.HARNESS_PRESETS["claude"], "invoke": [str(fake_bin), "-p", "{prompt}"]},
        )

    def test_a_turn_in_flight_past_the_window_is_not_cut_off(self, monkeypatch, tmp_path):
        # Negative control for the old wall-clock cut: the turn runs 2.5s, the
        # window is 1s. The session must not enter `closing` while the turn runs.
        fake_bin, record_file = make_fake_stream_claude(tmp_path, sleep_per_turn=2.5)
        self._patch_claude(monkeypatch, fake_bin)
        workdir = tmp_path / "seat"
        workdir.mkdir()
        home = resident_mod.resident_home(workdir, "ares")
        try:
            assert r4t_main(["engine", "claude", "run", "--resident", "--timeout", "1",
                             "--dir", str(workdir), "--agent", "ares", "slow work"]) == 0
            _wait_until(lambda: len(_turns(record_file)) >= 1)
            started = time.monotonic()
            while time.monotonic() - started < 1.8:
                assert (resident_mod.read_state(home) or {}).get("status") != "closing"
                assert len(_turns(record_file)) == 1
                time.sleep(0.1)
            # Positive control: once the result arrives and the window passes quiet, wrap-up is sent.
            _wait_until(lambda: len(_turns(record_file)) >= 2, timeout=15.0)
            assert "wrap up and exit" in _turns(record_file)[1]
        finally:
            _stop(workdir, "ares")

    def test_a_late_frame_restarts_the_quiet_window(self, monkeypatch, tmp_path):
        # Window 2s. The engine speaks again 1.5s after its turn, with no turn
        # in flight. The wrap-up must come a full window after that frame.
        fake_bin, record_file = make_fake_stream_claude(
            tmp_path, sleep_per_turn=0.05, late_frame_after=1.5
        )
        self._patch_claude(monkeypatch, fake_bin)
        workdir = tmp_path / "seat"
        workdir.mkdir()
        try:
            assert r4t_main(["engine", "claude", "run", "--resident", "--timeout", "2",
                             "--dir", str(workdir), "--agent", "ares", "go"]) == 0
            _wait_until(lambda: len(_turns(record_file)) >= 1)
            t0 = time.monotonic()
            _wait_until(lambda: len(_turns(record_file)) >= 2, timeout=15.0)
            elapsed = time.monotonic() - t0
            assert "wrap up and exit" in _turns(record_file)[1]
            # Without the reset the wrap-up lands near 2.0s; with it, near 3.5s.
            assert elapsed >= 2.8, elapsed
        finally:
            _stop(workdir, "ares")

    def test_quiet_for_the_window_with_no_turn_in_flight_sends_the_wrapup(
        self, monkeypatch, tmp_path
    ):
        fake_bin, record_file = make_fake_stream_claude(tmp_path, sleep_per_turn=0.05)
        self._patch_claude(monkeypatch, fake_bin)
        workdir = tmp_path / "seat"
        workdir.mkdir()
        try:
            assert r4t_main(["engine", "claude", "run", "--resident", "--timeout", "1",
                             "--dir", str(workdir), "--agent", "ares", "hello"]) == 0
            _wait_until(lambda: len(_turns(record_file)) >= 2, timeout=15.0)
            turns = _turns(record_file)
            assert "hello" in turns[0]
            assert "quiet for" in turns[1] and "wrap up and exit" in turns[1]
        finally:
            _stop(workdir, "ares")

    def test_message_during_wrapup_waits_for_the_next_session_never_a_second_engine(
        self, monkeypatch, tmp_path
    ):
        # A slow wrap-up turn holds the session in `closing`; a message that
        # lands then must queue, and the same bridge serves it with a fresh
        # engine only after the first has exited.
        fake_bin, record_file = make_fake_stream_claude(tmp_path, sleep_per_turn=1.0)
        monkeypatch.setitem(
            engine_run.HARNESS_PRESETS,
            "claude",
            {**engine_run.HARNESS_PRESETS["claude"], "invoke": [str(fake_bin), "-p", "{prompt}"]},
        )
        workdir = tmp_path / "seat"
        workdir.mkdir()
        home = resident_mod.resident_home(workdir, "ares")
        run = ["engine", "claude", "run", "--resident", "--timeout", "1",
               "--dir", str(workdir), "--agent", "ares"]
        try:
            assert r4t_main([*run, "first"]) == 0
            bridge = resident_mod.read_live_pid(home)
            _wait_until(lambda: (resident_mod.read_state(home) or {}).get("status") == "closing")
            assert r4t_main([*run, "during wrap-up"]) == 0
            assert resident_mod.read_live_pid(home) == bridge

            _wait_until(
                lambda: resident_mod.read_live_pid(home) is None and len(_turns(record_file)) >= 4,
                timeout=20.0,
            )
            rec = json.loads(record_file.read_text(encoding="utf-8"))
            assert rec["spawns"] == 2
            turns = rec["turns"]
            assert "first" in turns[0]
            assert "wrap up and exit" in turns[1]
            assert "Smart cold boot" in turns[2] and "during wrap-up" in turns[2]
            assert "wrap up and exit" in turns[3]
            assert not any((home / "inbox").glob("*.json"))
        finally:
            _stop(workdir, "ares")

    def test_resident_dir_ignores_itself(self, monkeypatch, tmp_path):
        fake_bin, _record = make_fake_stream_claude(tmp_path)
        monkeypatch.setitem(
            engine_run.HARNESS_PRESETS,
            "claude",
            {**engine_run.HARNESS_PRESETS["claude"], "invoke": [str(fake_bin), "-p", "{prompt}"]},
        )
        workdir = tmp_path / "seat"
        workdir.mkdir()
        try:
            assert r4t_main(["engine", "claude", "run", "--resident", "--timeout", "1",
                             "--dir", str(workdir), "--agent", "ares", "hi"]) == 0
            ignore = workdir / resident_mod.RESIDENT_DIR_NAME / ".gitignore"
            assert ignore.read_text(encoding="utf-8") == "*\n"
        finally:
            _stop(workdir, "ares")

    def test_engine_that_never_reads_drops_its_messages_and_the_bridge_exits(
        self, monkeypatch, tmp_path
    ):
        dead = tmp_path / "dead-engine"
        dead.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(3)\n", encoding="utf-8")
        dead.chmod(dead.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setitem(
            engine_run.HARNESS_PRESETS,
            "claude",
            {**engine_run.HARNESS_PRESETS["claude"], "invoke": [str(dead), "-p", "{prompt}"]},
        )
        workdir = tmp_path / "seat"
        workdir.mkdir()
        home = resident_mod.resident_home(workdir, "ares")
        assert r4t_main(["engine", "claude", "run", "--resident", "--dir", str(workdir),
                         "--agent", "ares", "nobody reads this"]) == 0
        _wait_until(lambda: resident_mod.read_live_pid(home) is None, timeout=10.0)
        assert not any((home / "inbox").glob("*.json"))

    def test_stop_ends_bridge_and_engine_now_with_no_wrapup_and_drops_the_queue(
        self, monkeypatch, tmp_path, capsys
    ):
        fake_bin, record_file = make_fake_stream_claude(tmp_path, sleep_per_turn=0.05)
        monkeypatch.setitem(
            engine_run.HARNESS_PRESETS,
            "claude",
            {**engine_run.HARNESS_PRESETS["claude"], "invoke": [str(fake_bin), "-p", "{prompt}"]},
        )
        workdir = tmp_path / "seat"
        workdir.mkdir()
        home = resident_mod.resident_home(workdir, "ares")
        try:
            assert r4t_main(["engine", "claude", "run", "--resident", "--timeout", "600",
                             "--dir", str(workdir), "--agent", "ares", "hello"]) == 0
            _wait_until(lambda: len(_turns(record_file)) >= 1)
            bridge = resident_mod.read_live_pid(home)
            engine = json.loads(record_file.read_text(encoding="utf-8"))["pid"]
            (home / "inbox" / "01ZZZZZZZZZZZZZZZZZZZZZZZZ.json").write_text(
                json.dumps({"message": "queued, never read"}), encoding="utf-8"
            )

            assert r4t_main(["engine", "claude", "stop", "--dir", str(workdir), "--agent", "ares"]) == 0
            assert "resident session stopped" in capsys.readouterr().out
            assert resident_mod.read_live_pid(home) is None
            _wait_until(lambda: _reaped(bridge) and not resident_mod.pid_alive(engine))
            assert not any((home / "inbox").glob("*.json"))
            assert not any("wrap up and exit" in t for t in _turns(record_file))

            assert r4t_main(["engine", "claude", "stop", "--dir", str(workdir), "--agent", "ares"]) == 0
            assert "resident session not running" in capsys.readouterr().out
        finally:
            _stop(workdir, "ares")

    def test_stop_refuses_an_engine_with_no_resident_mode(self, tmp_path, capsys):
        assert r4t_main(["engine", "cursor", "stop", "--dir", str(tmp_path)]) == 2
        assert "no resident session" in capsys.readouterr().err

    def test_idle_wake_never_boots_or_feeds_a_resident_session(self, monkeypatch, tmp_path):
        fake_bin, record_file = make_fake_stream_claude(tmp_path)
        monkeypatch.setitem(
            engine_run.HARNESS_PRESETS,
            "claude",
            {**engine_run.HARNESS_PRESETS["claude"], "invoke": [str(fake_bin), "-p", "{prompt}"]},
        )
        workdir = tmp_path / "seat"
        workdir.mkdir()
        home = resident_mod.resident_home(workdir, "ares")
        assert r4t_main(["engine", "claude", "run", "--resident", "--idle",
                         "--dir", str(workdir), "--agent", "ares"]) == 0
        assert resident_mod.read_live_pid(home) is None
        assert not record_file.exists()


def _slow_engine(tmp_path: Path) -> Path:
    """An engine that never reads its input and never exits on its own."""
    engine = tmp_path / "slow-engine"
    engine.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(120)\n")
    engine.chmod(0o700)
    return engine


def _kill(*pids) -> None:
    for pid in pids:
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


@pytest.mark.skipif(os.name != "posix", reason="process groups and fake engines are POSIX")
class TestResidentStopOwnership:
    def _enqueue(self, workdir: Path, engine: Path, message: str) -> int:
        return resident_mod.enqueue_or_start(
            "claude", message, dir_path=workdir, template=[str(engine), "-p", "{prompt}"],
            model=None, effort=None, agent="ares", timeout=600, scaffold=False,
        )

    def test_a_bridge_started_while_stop_waits_for_the_lock_is_stopped_not_orphaned(
        self, monkeypatch, tmp_path
    ):
        engine = _slow_engine(tmp_path)
        home = resident_mod.resident_home(tmp_path, "ares")
        started: list[int] = []
        real_lock = resident_mod.file_lock
        first = [True]

        @contextmanager
        def enqueue_wins_the_race(path, **kw):
            if first[0]:
                first[0] = False
                self._enqueue(tmp_path, engine, "arrived while stop waits for the lock")
                _wait_until(lambda: (resident_mod.read_state(home) or {}).get("engine_pid"))
                started.extend([resident_mod.read_live_pid(home),
                                resident_mod.read_state(home)["engine_pid"]])
            with real_lock(path, **kw):
                yield

        monkeypatch.setattr(resident_mod, "file_lock", enqueue_wins_the_race)
        try:
            assert resident_mod.stop_resident(tmp_path, "ares", grace_seconds=2) is True
            bridge, eng = started
            _wait_until(lambda: _reaped(bridge) and not resident_mod.pid_alive(eng))
            assert resident_mod.read_live_pid(home) is None
        finally:
            _kill(*started)

    def test_a_message_to_a_stopping_session_is_dropped(self, tmp_path):
        engine = _slow_engine(tmp_path)
        home = resident_mod.resident_home(tmp_path, "ares")
        home.mkdir(parents=True)
        resident_mod._write_pid_files(home, os.getpid())
        resident_mod._write_state(home, pid=os.getpid(), engine_pid=None, status="stopping")
        assert self._enqueue(tmp_path, engine, "too late") == 0
        assert resident_mod.read_live_pid(home) == os.getpid()
        assert not any((home / "inbox").glob("*.json"))

    def test_stop_at_engine_publication_ends_the_engine(self, tmp_path):
        engine = _slow_engine(tmp_path)
        home = resident_mod.resident_home(tmp_path, "ares")
        home.mkdir(parents=True)
        resident_mod._spec_file(home).write_text(json.dumps({
            "engine": "claude", "dir_path": str(tmp_path), "agent": "ares", "timeout": 600,
            "template": [str(engine), "-p", "{prompt}"], "scaffold": False,
        }))
        resident_mod._enqueue_message(home, "one")
        gate = tmp_path / "engine-spawned"
        wrapper = tmp_path / "bridge.py"
        wrapper.write_text(textwrap.dedent("""
            import os, sys, time
            from pathlib import Path
            from engines import resident as r
            home, gate = Path(sys.argv[1]), Path(sys.argv[2])
            r._write_pid_files(home, os.getpid())
            r._write_state(home, pid=os.getpid(), engine_pid=None, status="starting")
            real = r._write_state
            def pause(home, **kw):
                if kw["status"] == "running":
                    gate.write_text(str(kw["engine_pid"]))
                    time.sleep(120)
                return real(home, **kw)
            r._write_state = pause
            r.serve_resident(home)
        """))
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
        bridge = subprocess.Popen(
            [sys.executable, str(wrapper), str(home), str(gate)], env=env,
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        eng = None
        try:
            _wait_until(gate.exists)
            eng = int(gate.read_text())
            assert resident_mod.stop_resident(
                tmp_path, "ares", grace_seconds=0.2, lock_seconds=0.5
            ) is True
            bridge.wait(timeout=5)
            _wait_until(lambda: not resident_mod.pid_alive(eng))
            assert resident_mod.read_live_pid(home) is None
        finally:
            _kill(eng, bridge.pid)
            bridge.wait(timeout=5)

    def test_stop_ends_the_engine_of_a_bridge_that_died_outright(self, tmp_path):
        engine = _slow_engine(tmp_path)
        home = resident_mod.resident_home(tmp_path, "ares")
        self._enqueue(tmp_path, engine, "hello")
        bridge = resident_mod.read_live_pid(home)
        eng = _wait_until(lambda: (resident_mod.read_state(home) or {}).get("engine_pid"))
        try:
            os.kill(bridge, signal.SIGKILL)
            _wait_until(lambda: _reaped(bridge))
            assert resident_mod.pid_alive(eng)
            assert resident_mod.stop_resident(tmp_path, "ares", grace_seconds=0.2) is False
            _wait_until(lambda: not resident_mod.pid_alive(eng))
        finally:
            _kill(bridge, eng)

    def test_stop_ends_the_tools_an_engine_started(self, tmp_path):
        marker = tmp_path / "tool.pid"
        engine = tmp_path / "engine-with-tool"
        engine.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import subprocess, sys, time
            from pathlib import Path
            tool = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
            Path({str(marker)!r}).write_text(str(tool.pid))
            time.sleep(120)
        """))
        engine.chmod(0o700)
        home = resident_mod.resident_home(tmp_path, "ares")
        self._enqueue(tmp_path, engine, "one")
        bridge = resident_mod.read_live_pid(home)
        tool = None
        try:
            tool = int(_wait_until(lambda: marker.is_file() and marker.read_text()))
            assert resident_mod.stop_resident(tmp_path, "ares", grace_seconds=8) is True
            _wait_until(lambda: _reaped(bridge))
            _wait_until(lambda: not resident_mod.pid_alive(tool))
        finally:
            _kill(bridge, tool)


class TestResidentStatus:
    """`r4t engine <id> status`: what `a8s ps` reads through `status.invoke`."""

    def _live_home(self, tmp_path, **state):
        home = resident_mod.resident_home(tmp_path, "ares")
        home.mkdir(parents=True)
        resident_mod._write_pid_files(home, os.getpid())
        resident_mod._write_state(home, pid=os.getpid(), engine_pid=None, **state)
        return home

    def _run(self, tmp_path, capsys):
        code = r4t_main(["engine", "claude", "status", "--dir", str(tmp_path), "--agent", "ares"])
        return code, capsys.readouterr().out.strip()

    def test_a_turn_in_flight_is_busy(self, tmp_path, capsys):
        self._live_home(tmp_path, status="running", turns=1)
        assert self._run(tmp_path, capsys) == (0, "busy")

    def test_no_turn_in_flight_is_resident(self, tmp_path, capsys):
        self._live_home(tmp_path, status="running", turns=0)
        assert self._run(tmp_path, capsys) == (0, "resident")

    def test_a_state_without_turns_is_resident(self, tmp_path, capsys):
        home = self._live_home(tmp_path, status="running")
        state = json.loads((home / "state.json").read_text(encoding="utf-8"))
        state.pop("turns")
        (home / "state.json").write_text(json.dumps(state), encoding="utf-8")
        assert self._run(tmp_path, capsys) == (0, "resident")

    def test_closing_with_no_turn_is_resident(self, tmp_path, capsys):
        self._live_home(tmp_path, status="closing")
        assert self._run(tmp_path, capsys) == (0, "resident")

    def test_starting_with_a_queued_message_is_busy(self, tmp_path, capsys):
        home = self._live_home(tmp_path, status="starting")
        assert self._run(tmp_path, capsys) == (0, "resident")
        (home / "inbox").mkdir(exist_ok=True)
        (home / "inbox" / "1.json").write_text("{}", encoding="utf-8")
        assert self._run(tmp_path, capsys) == (0, "busy")

    def test_stopping_prints_nothing_and_exits_1(self, tmp_path, capsys):
        self._live_home(tmp_path, status="stopping", turns=2)
        assert self._run(tmp_path, capsys) == (1, "")

    def test_a_dead_bridge_prints_nothing_and_exits_1(self, tmp_path, capsys):
        home = self._live_home(tmp_path, status="running", turns=1)
        resident_mod._pid_file(home).write_text("4194300\n", encoding="utf-8")
        assert self._run(tmp_path, capsys) == (1, "")

    def test_no_session_at_all_exits_1(self, tmp_path, capsys):
        assert self._run(tmp_path, capsys) == (1, "")

    def test_an_engine_without_a_resident_session_is_refused(self, tmp_path, capsys):
        assert r4t_main(["engine", "gemini", "status", "--dir", str(tmp_path),
                         "--agent", "ares"]) == 2

    def test_the_bridge_publishes_turns_while_a_turn_runs(self, monkeypatch, tmp_path):
        fake_bin, record_file = make_fake_stream_claude(tmp_path, sleep_per_turn=2.0)
        monkeypatch.setitem(
            engine_run.HARNESS_PRESETS,
            "claude",
            {**engine_run.HARNESS_PRESETS["claude"], "invoke": [str(fake_bin), "-p", "{prompt}"]},
        )
        workdir = tmp_path / "seat"
        workdir.mkdir()
        home = resident_mod.resident_home(workdir, "ares")
        try:
            assert r4t_main(["engine", "claude", "run", "--resident", "--timeout", "30",
                             "--dir", str(workdir), "--agent", "ares", "hello"]) == 0
            _wait_until(lambda: (resident_mod.read_state(home) or {}).get("turns") == 1)
            assert resident_mod.status_resident(workdir, "ares") == "busy"
            _wait_until(lambda: (resident_mod.read_state(home) or {}).get("turns") == 0,
                        timeout=15.0)
            assert resident_mod.status_resident(workdir, "ares") == "resident"
        finally:
            _stop(workdir, "ares")
