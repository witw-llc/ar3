"""Claude merges a message that arrives at a tool step of a running turn into
that turn and answers the whole turn with one `result` (#378). With
`--replay-user-messages` it echoes each message as it takes it, carrying the
uuid the bridge wrote with it, so the bridge settles exactly the messages the
turn took. A message that arrives during the final reply is not taken; Claude
runs a turn of its own for it. A turn a peer or a channel injects is echoed
too, under an id the bridge never wrote, and settles nothing."""
from __future__ import annotations

import json
import stat
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from ar3.wake_outcome import origin_from_env, take_failures
from engines import resident as r

FLAG = "--replay-user-messages"
REFUSAL = f"error: unknown option '{FLAG}'"


def make_merging_claude(tmp_path: Path) -> tuple[Path, Path]:
    """A fake `claude` that runs one turn at a time the way Claude Code does.

    A turn starts with the oldest waiting message. Until `ctl/end-N` exists
    the turn is "in its tools": every message that arrives joins it. Then
    the turn emits `ctl/inject-tool-N` (a frame, as JSON) if present, writes
    `ctl/replying-N`, and is "in its final reply": a message that arrives now
    waits for a turn of its own. When `ctl/result-N` exists the turn emits
    `ctl/inject-result-N` if present, then one `result`, an error result
    when `ctl/fail-N` exists too.

    With `--replay-user-messages` on the command line each message is echoed
    when taken, with the uuid it was written with (twice with `ctl/dup-echo`).
    `ctl/reject-flag`: exit 1 at start, naming the flag the way Claude Code
    does, without reading input, when the flag is present.
    `ctl/foreign-first`: before taking the first message, emit a channel's
    echo and a result for it, touch `ctl/external-result`, and wait for
    `ctl/release-first`.
    `ctl/always-reject`: the same with or without the flag.
    `ctl/exit-after-take`: echo the first message, then print the same words
    and exit 1.

    `ctl/no-echo`: take the flag but echo nothing.
    `ctl/list-ids`: each result lists the uuids its turn took as
    `user_message_uuids`, at most `ctl/list-cap` of them (default 64).
    `ctl/list-N`: turn N's result carries this JSON value as
    `user_message_uuids` instead; a "$TAKEN" entry stands for the uuids the
    turn took.

    `ctl/received` and `ctl/taken` count stdin messages and messages taken
    for this process; `ctl/taken.log` gets each taken text, one JSON string
    per line; `ctl/argv.jsonl` gets one line per process."""
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    script = tmp_path / "fake-claude"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, os, queue, sys, threading, time
        from pathlib import Path

        ctl = Path({str(ctl)!r})
        FLAG = {FLAG!r}
        with (ctl / "argv.jsonl").open("a") as fh:
            fh.write(json.dumps(sys.argv[1:]) + "\\n")
        if (ctl / "always-reject").exists() or (
                FLAG in sys.argv and (ctl / "reject-flag").exists()):
            time.sleep(0.3)  # long enough for the bridge to drain and write
            sys.stderr.write("error: unknown option '" + FLAG + "'\\n")
            sys.stderr.flush()
            sys.exit(1)
        replay = FLAG in sys.argv
        inbox = queue.Queue()
        out_lock = threading.Lock()
        counts = {{"received": 0, "taken": 0}}

        def count(name):
            counts[name] += 1
            tmp = ctl / (name + ".tmp")
            tmp.write_text(str(counts[name]))
            os.replace(tmp, ctl / name)

        def emit(frame):
            with out_lock:
                sys.stdout.write(json.dumps(frame) + "\\n")
                sys.stdout.flush()

        def emit_file(name):
            if (ctl / name).exists():
                emit(json.loads((ctl / name).read_text()))

        def read_stdin():
            for raw in sys.stdin:
                if not raw.strip():
                    continue
                inbox.put(json.loads(raw))
                count("received")
            inbox.put(None)

        turn_ids = []

        def take(frame):
            turn_ids.append(frame.get("uuid"))
            with (ctl / "taken.log").open("a") as fh:
                fh.write(json.dumps((frame.get("message") or {{}}).get("content")) + "\\n")
            if replay and not (ctl / "no-echo").exists():
                echo = {{"type": "user", "isReplay": True, "session_id": "fake",
                        "parent_tool_use_id": None, "message": frame.get("message")}}
                if "uuid" in frame:
                    echo["uuid"] = frame["uuid"]
                emit(echo)
                if (ctl / "dup-echo").exists():
                    emit(echo)
            count("taken")
            if (ctl / "exit-after-take").exists():
                sys.stderr.write("error: unknown option '" + FLAG + "'\\n")
                sys.stderr.flush()
                os._exit(1)

        def wait_for(name):
            while not (ctl / name).exists():
                time.sleep(0.02)

        threading.Thread(target=read_stdin, daemon=True).start()
        emit({{"type": "system", "subtype": "init", "session_id": "fake"}})
        turn = 0
        eof = False
        while not eof:
            first = inbox.get()
            if first is None:
                break
            if turn == 0 and (ctl / "foreign-first").exists():
                emit({{"type": "user", "isReplay": True, "uuid": "native-channel-1",
                       "origin": {{"kind": "channel", "server": "probe"}},
                       "message": {{"role": "user", "content": "external turn"}}}})
                emit({{"type": "result", "subtype": "success", "is_error": False,
                       "result": "external turn answered"}})
                (ctl / "external-result").touch()
                wait_for("release-first")
            turn += 1
            turn_ids.clear()
            take(first)
            emit({{"type": "assistant", "message": {{"role": "assistant",
                  "content": [{{"type": "tool_use", "id": "t" + str(turn),
                               "name": "Bash", "input": {{}}}}]}}}})
            while not (ctl / f"end-{{turn}}").exists():
                try:
                    item = inbox.get(timeout=0.02)
                except queue.Empty:
                    continue
                if item is None:
                    eof = True
                    break
                take(item)
            emit_file(f"inject-tool-{{turn}}")
            (ctl / f"replying-{{turn}}").touch()
            if eof:
                break
            wait_for(f"result-{{turn}}")
            emit_file(f"inject-result-{{turn}}")
            text = f"answer {{turn}}"
            emit({{"type": "assistant", "message": {{"role": "assistant",
                  "content": [{{"type": "text", "text": text}}]}}}})
            result = {{"type": "result", "subtype": "success",
                      "is_error": False, "result": text}}
            if (ctl / f"fail-{{turn}}").exists():
                result.update(subtype="error_during_execution", is_error=True,
                              result="turn failed")
            if (ctl / f"list-{{turn}}").exists():
                value = json.loads((ctl / f"list-{{turn}}").read_text())
                if isinstance(value, list):
                    expanded = []
                    for entry in value:
                        expanded.extend(turn_ids if entry == "$TAKEN" else [entry])
                    value = expanded
                result["user_message_uuids"] = value
            elif (ctl / "list-ids").exists():
                cap = 64
                if (ctl / "list-cap").exists():
                    cap = int((ctl / "list-cap").read_text())
                result["user_message_uuids"] = turn_ids[:cap]
            emit(result)
            (ctl / f"resulted-{{turn}}").touch()
        """), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script, ctl


def _wait(predicate, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met before timeout")


def _foreign_echo(content: str, echo_id: str = "external-1") -> str:
    """A turn a channel injected, echoed the way Claude echoes stdin."""
    return json.dumps({
        "type": "user", "isReplay": True, "uuid": echo_id, "session_id": "fake",
        "parent_tool_use_id": None,
        "origin": {"kind": "channel", "server": "probe"},
        "message": {"role": "user", "content": content},
    })


class Seat:
    """`_run_session` on a thread against the fake engine. `start` may run
    another session on the same seat after `finish`."""

    def __init__(self, tmp_path: Path, fake: Path, ctl: Path):
        self.tmp_path = tmp_path
        self.ctl = ctl
        self.workdir = tmp_path / "seat"
        self.workdir.mkdir()
        self.failures = tmp_path / "failures"
        self.home = r.resident_home(self.workdir, "probe")
        self.spec = {
            "engine": "claude", "dir_path": str(self.workdir),
            "template": [str(fake), "-p", "{prompt}"], "agent": "probe",
            "scaffold": False, "timeout": 3600,
        }
        self.thread: threading.Thread | None = None
        self.result: dict = {}

    def start(self) -> None:
        self.stop = threading.Event()
        self.result = {}
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        self.result["code"] = r._run_session(self.home, self.spec, self.stop, {"proc": None})

    def enqueue(self, name: str, text: str | None = None) -> None:
        origin = origin_from_env({
            "A8S_TURN_RECIPIENT": "probe",
            "A8S_TURN_ENVELOPES": json.dumps([str(self.tmp_path / f"{name}.json")]),
            "A8S_TURN_OUTCOME_DIR": str(self.failures),
        }, "probe")
        r._enqueue_message(self.home, text or f"message {name}", origin)

    def send(self, name: str, text: str | None = None) -> None:
        self.enqueue(name, text)
        if self.thread is None:
            self.start()

    def _count(self, name: str) -> int:
        try:
            return int((self.ctl / name).read_text())
        except (OSError, ValueError):
            return 0

    def received(self) -> int:
        return self._count("received")

    def taken(self) -> int:
        return self._count("taken")

    def spawns(self) -> list[list[str]]:
        path = self.ctl / "argv.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def turns(self) -> int | None:
        return (r.read_state(self.home) or {}).get("turns")

    def touch(self, *names: str) -> None:
        for name in names:
            (self.ctl / name).touch()

    def put(self, name: str, text: str) -> None:
        (self.ctl / name).write_text(text)

    def finish(self) -> int:
        assert self.thread is not None
        self.stop.set()
        self.thread.join(timeout=20)
        assert not self.thread.is_alive()
        return self.result["code"]

    def failed_envelopes(self) -> list[str]:
        if not self.failures.is_dir():
            return []
        return sorted(
            name for record in take_failures(self.failures, "probe")
            for name in record["envelopes"]
        )


def _three_merged(seat: Seat, texts=(None, None, None)) -> None:
    """a, b and c are each written on their own; b and c arrive while turn 1
    is in its tools, so Claude takes them into turn 1."""
    for count, (name, text) in enumerate(zip(("a", "b", "c"), texts), start=1):
        seat.send(name, text)
        _wait(lambda: seat.received() == count and seat.taken() == count)
    _wait(lambda: seat.turns() == 3)


def _b_waits_during_final_reply(seat: Seat, ctl: Path) -> None:
    """a is turn 1. b is written after turn 1 left its tools, so Claude will
    run a turn of its own for it."""
    seat.send("a")
    _wait(lambda: seat.taken() == 1)
    seat.touch("end-1")
    _wait(lambda: (ctl / "replying-1").exists())
    seat.send("b")
    _wait(lambda: seat.received() == 2 and seat.turns() == 2)


def _after_result_1(seat: Seat, ctl: Path) -> int | None:
    """Release turn 1's result; turn 2 reaching its final reply proves the
    bridge has read that result. The published count then."""
    seat.touch("result-1", "end-2")
    _wait(lambda: (ctl / "replying-2").exists())
    time.sleep(0.2)
    return seat.turns()


def test_one_result_settles_every_message_merged_into_its_turn(tmp_path, capsys):
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    try:
        _three_merged(seat)
        seat.touch("end-1", "result-1")
        _wait(lambda: seat.turns() == 0)
    finally:
        code = seat.finish()
    assert code == 0
    assert seat.failed_envelopes() == []
    # One turn answered all three.
    log = capsys.readouterr().out
    assert "answer 1" in log and "answer 2" not in log


def test_a_message_written_during_the_final_reply_keeps_its_own_turn(tmp_path):
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    try:
        _b_waits_during_final_reply(seat, ctl)
        seat.touch("result-1")
        _wait(lambda: seat.turns() == 1)
        time.sleep(1.0)
        assert seat.turns() == 1
        # A stop while b is still in flight reports b, and only b.
    finally:
        code = seat.finish()
    assert code == 1
    assert seat.failed_envelopes() == ["b.json"]


def test_a_message_written_after_the_result_keeps_its_own_turn(tmp_path):
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    try:
        seat.send("a")
        _wait(lambda: seat.taken() == 1)
        seat.touch("end-1", "result-1")
        _wait(lambda: seat.turns() == 0)
        seat.send("b")
        _wait(lambda: seat.received() == 2)
        _wait(lambda: seat.turns() == 1)
        seat.touch("end-2", "result-2")
        _wait(lambda: seat.turns() == 0)
    finally:
        code = seat.finish()
    assert code == 0
    assert seat.failed_envelopes() == []


def test_a_failed_result_reports_every_merged_message_failed(tmp_path):
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    try:
        _three_merged(seat)
        seat.touch("fail-1", "end-1", "result-1")
        seat.thread.join(timeout=20)
        assert not seat.thread.is_alive()
    finally:
        code = seat.finish()
    assert code == 1
    assert seat.failed_envelopes() == ["a.json", "b.json", "c.json"]


# Provenance: an echo settles only the message the bridge wrote with its id.

def test_external_replay_at_the_tool_step_does_not_settle_a_waiting_message(tmp_path):
    """The reviewer's reproducer: a channel's turn is echoed at the end of
    turn 1's tools, b is written during turn 1's final reply, then turn 1's
    result. b keeps its token, and a stop reports b."""
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    seat.put("inject-tool-1", _foreign_echo("external channel message"))
    try:
        _b_waits_during_final_reply(seat, ctl)
        observed = _after_result_1(seat, ctl)
    finally:
        code = seat.finish()
    assert (observed, code, seat.failed_envelopes()) == (1, 1, ["b.json"])


def test_after_an_external_replay_the_waiting_message_settles_at_its_own_result(tmp_path):
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    seat.put("inject-tool-1", _foreign_echo("external channel message"))
    try:
        _b_waits_during_final_reply(seat, ctl)
        assert _after_result_1(seat, ctl) == 1
        seat.touch("result-2")
        _wait(lambda: seat.turns() == 0)
    finally:
        code = seat.finish()
    assert code == 0
    assert seat.failed_envelopes() == []


def test_a_foreign_replay_with_the_waiting_messages_exact_text_does_not_settle_it(tmp_path):
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    seat.put("inject-result-1", _foreign_echo("message b"))
    try:
        _b_waits_during_final_reply(seat, ctl)
        observed = _after_result_1(seat, ctl)
    finally:
        code = seat.finish()
    assert (observed, code, seat.failed_envelopes()) == (1, 1, ["b.json"])


def test_a_duplicate_echo_settles_its_message_once(tmp_path):
    """Every echo arrives twice. The second echo of a must not count for the
    waiting b."""
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    seat.touch("dup-echo")
    try:
        _b_waits_during_final_reply(seat, ctl)
        observed = _after_result_1(seat, ctl)
    finally:
        code = seat.finish()
    assert (observed, code, seat.failed_envelopes()) == (1, 1, ["b.json"])


def test_identical_messages_each_settle_on_their_own_echo(tmp_path):
    """The same text three times, each its own turn: each result settles one."""
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    same = "the same words"
    try:
        seat.send("a", same)
        _wait(lambda: seat.taken() == 1)
        seat.touch("end-1", "end-2", "end-3")
        _wait(lambda: (ctl / "replying-1").exists())
        seat.send("b", same)
        _wait(lambda: seat.received() == 2)
        seat.send("c", same)
        _wait(lambda: seat.received() == 3 and seat.turns() == 3)
        seat.touch("result-1")
        _wait(lambda: (ctl / "replying-2").exists())
        time.sleep(0.2)
        assert seat.turns() == 2
        seat.touch("result-2")
        _wait(lambda: (ctl / "replying-3").exists())
        time.sleep(0.2)
        assert seat.turns() == 1
    finally:
        code = seat.finish()
    assert (code, seat.failed_envelopes()) == (1, ["c.json"])


def test_identical_merged_messages_all_settle(tmp_path):
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    try:
        _three_merged(seat, texts=("same",) * 3)
        seat.touch("end-1", "result-1")
        _wait(lambda: seat.turns() == 0)
    finally:
        code = seat.finish()
    assert (code, seat.failed_envelopes()) == (0, [])


def test_a_foreign_result_before_any_match_does_not_settle_a_waiting_message(tmp_path):
    """The reviewer's reproducer: a channel's turn is echoed and answered
    before the engine takes a. No echo of a has come yet, and that result
    is not a's."""
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    seat.touch("foreign-first")
    try:
        seat.send("a")
        _wait(lambda: (ctl / "external-result").exists())
        time.sleep(0.3)
        observed = seat.turns()
    finally:
        code = seat.finish()
    assert (observed, code, seat.failed_envelopes()) == (1, 1, ["a.json"])


def test_with_the_flag_a_result_without_a_matching_echo_settles_nothing(tmp_path):
    """The cost of the rule: an engine that took the flag but never echoes
    an id the bridge wrote leaves its messages in flight. Claude Code 2.1.x
    echoes; a CLI that rejects the flag runs without it."""
    fake, ctl = make_merging_claude(tmp_path)
    source = fake.read_text()
    needle = '            echo["uuid"] = frame["uuid"]'
    assert source.count(needle) == 1
    fake.write_text(source.replace(needle, '            echo["uuid"] = "claude-own-id"'))
    seat = Seat(tmp_path, fake, ctl)
    seat.touch("end-1", "result-1")
    try:
        seat.send("a")
        _wait(lambda: (ctl / "replying-1").exists())
        time.sleep(0.5)
        observed = seat.turns()
    finally:
        code = seat.finish()
    assert (observed, code, seat.failed_envelopes()) == (1, 1, ["a.json"])


def test_without_the_flag_each_result_settles_the_oldest_message(tmp_path):
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    seat.spec["echo"] = False
    try:
        _b_waits_during_final_reply(seat, ctl)
        seat.touch("result-1")
        _wait(lambda: seat.turns() == 1)
        seat.touch("end-2", "result-2")
        _wait(lambda: seat.turns() == 0)
    finally:
        code = seat.finish()
    assert FLAG not in seat.spawns()[0]
    assert code == 0
    assert seat.failed_envelopes() == []


# The result's `user_message_uuids`: the stdin ids the turn answered.

def _result_read(seat: Seat, ctl: Path, turn: int) -> int | None:
    """The published count once the bridge has had time to read turn's
    result."""
    _wait(lambda: (ctl / f"resulted-{turn}").exists())
    time.sleep(0.3)
    return seat.turns()


def test_a_result_listing_the_uuids_settles_them_without_echoes(tmp_path):
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    seat.touch("no-echo", "list-ids")
    try:
        _three_merged(seat)
        seat.touch("end-1", "result-1")
        _wait(lambda: seat.turns() == 0)
    finally:
        code = seat.finish()
    assert (code, seat.failed_envelopes()) == (0, [])


def test_a_result_listing_echoed_uuids_settles_each_once(tmp_path):
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    seat.touch("list-ids")
    try:
        _b_waits_during_final_reply(seat, ctl)
        assert _after_result_1(seat, ctl) == 1
        seat.touch("result-2")
        _wait(lambda: seat.turns() == 0)
    finally:
        code = seat.finish()
    assert (code, seat.failed_envelopes()) == (0, [])


def test_a_result_listing_only_a_foreign_uuid_settles_nothing(tmp_path):
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    seat.touch("no-echo", "end-1", "result-1")
    seat.put("list-1", json.dumps(["native-channel-1"]))
    try:
        seat.send("a")
        observed = _result_read(seat, ctl, 1)
    finally:
        code = seat.finish()
    assert (observed, code, seat.failed_envelopes()) == (1, 1, ["a.json"])


def test_an_empty_list_and_no_echo_settles_nothing(tmp_path):
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    seat.touch("no-echo", "end-1", "result-1")
    seat.put("list-1", "[]")
    try:
        seat.send("a")
        observed = _result_read(seat, ctl, 1)
    finally:
        code = seat.finish()
    assert (observed, code, seat.failed_envelopes()) == (1, 1, ["a.json"])


def test_an_empty_list_leaves_an_echoed_message_for_a_later_result(tmp_path):
    """a is echoed, but its result lists no stdin id: a stays in flight. The
    next result lists b only; a, echoed and pending, settles with it."""
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    seat.touch("end-1", "result-1", "end-2")
    seat.put("list-1", "[]")
    seat.put("list-2", json.dumps(["$TAKEN"]))
    try:
        seat.send("a")
        assert _result_read(seat, ctl, 1) == 1
        seat.send("b")
        _wait(lambda: (ctl / "replying-2").exists() and seat.turns() == 2)
        seat.touch("result-2")
        _wait(lambda: seat.turns() == 0)
    finally:
        code = seat.finish()
    assert (code, seat.failed_envelopes()) == (0, [])


def test_a_malformed_list_is_no_list_without_echoes(tmp_path):
    """One entry is not a string, so the list is ignored whole: a's own id
    in it does not settle a."""
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    seat.touch("no-echo", "end-1", "result-1")
    seat.put("list-1", json.dumps(["$TAKEN", 5]))
    try:
        seat.send("a")
        observed = _result_read(seat, ctl, 1)
    finally:
        code = seat.finish()
    assert (observed, code, seat.failed_envelopes()) == (1, 1, ["a.json"])


def test_a_malformed_list_is_no_list_with_echoes(tmp_path):
    """Treated as absent, not as empty: the echoed a settles."""
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    seat.touch("end-1", "result-1")
    seat.put("list-1", json.dumps("not a list"))
    try:
        seat.send("a")
        _wait(lambda: seat.turns() == 0)
    finally:
        code = seat.finish()
    assert (code, seat.failed_envelopes()) == (0, [])


def _seventy_merged(seat: Seat, monkeypatch) -> None:
    monkeypatch.setattr(r, "POLL_SECONDS", 0.02)
    for count in range(1, 71):
        seat.send(f"m{count:02d}")
        _wait(lambda: seat.taken() == count)
    _wait(lambda: seat.turns() == 70)


def test_a_list_capped_at_64_with_all_70_echoed_settles_all_70(tmp_path, monkeypatch):
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    seat.touch("list-ids")
    try:
        _seventy_merged(seat, monkeypatch)
        seat.touch("end-1", "result-1")
        _wait(lambda: seat.turns() == 0)
    finally:
        code = seat.finish()
    assert (code, seat.failed_envelopes()) == (0, [])


def test_a_list_capped_at_64_without_echoes_leaves_the_rest_in_flight(tmp_path, monkeypatch):
    """The control for the test above: the list alone is not exhaustive."""
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    seat.touch("list-ids", "no-echo")
    try:
        _seventy_merged(seat, monkeypatch)
        seat.touch("end-1", "result-1")
        observed = _result_read(seat, ctl, 1)
    finally:
        code = seat.finish()
    assert (observed, code) == (6, 1)
    assert seat.failed_envelopes() == sorted(f"m{n}.json" for n in range(65, 71))


# A CLI that does not know the echo flag.

def test_a_cli_that_rejects_the_echo_flag_runs_again_without_it(tmp_path, capsys):
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    seat.touch("reject-flag", "end-1", "result-1")
    for name in ("a", "b", "c"):
        seat.enqueue(name)
    seat.start()
    try:
        _wait(lambda: seat.received() == 1 and seat.turns() == 0)
    finally:
        code = seat.finish()
    assert code == 0
    assert seat.failed_envelopes() == []
    spawns = seat.spawns()
    assert len(spawns) == 2
    assert FLAG in spawns[0] and FLAG not in spawns[1]
    # All three were written once, in one turn, to the second engine.
    assert "answer 1" in capsys.readouterr().out
    assert (seat.home / r.ECHO_REFUSED_NAME).is_file()


def test_the_reviewers_strict_fake_keeps_its_message(tmp_path):
    """The reviewer's fake prints the refusal on stdout and exits 2."""
    fake, ctl = make_merging_claude(tmp_path)
    source = fake.read_text()
    needle = "replay = FLAG in sys.argv\n"
    assert source.count(needle) == 1
    fake.write_text(source.replace(needle, (
        "if FLAG in sys.argv:\n"
        "    print(\"error: unknown option '--replay-user-messages'\", flush=True)\n"
        "    sys.exit(2)\n" + needle
    )))
    seat = Seat(tmp_path, fake, ctl)
    seat.touch("end-1", "result-1")
    try:
        seat.send("a")
        _wait(lambda: seat.received() == 1 and seat.turns() == 0)
    finally:
        code = seat.finish()
    assert (code, seat.failed_envelopes()) == (0, [])


def test_the_retry_without_the_flag_happens_once(tmp_path):
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    seat.touch("always-reject")
    for name in ("a", "b"):
        seat.enqueue(name)
    seat.start()
    seat.thread.join(timeout=20)
    code = seat.finish()
    assert code == 1
    assert len(seat.spawns()) == 2
    # Reported once each, by the second session.
    assert seat.failed_envelopes() == ["a.json", "b.json"]


def test_an_exit_after_input_was_taken_is_not_a_flag_rejection(tmp_path):
    """The flag's refusal words after the engine took a message: no retry,
    the message is reported failed as for any engine exit."""
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    seat.touch("exit-after-take")
    seat.send("a")
    seat.thread.join(timeout=20)
    code = seat.finish()
    assert code == 1
    assert len(seat.spawns()) == 1
    assert seat.failed_envelopes() == ["a.json"]
    assert not (seat.home / r.ECHO_REFUSED_NAME).exists()


def _deny_inbox_writes(monkeypatch, seat: Seat, fail_at) -> list[str]:
    """Inbox writes from now on fail at the attempts numbered in `fail_at`
    (1 is the first); `None` fails them all."""
    original = r.atomic_write_text
    attempted: list[str] = []

    def write(path, text, *args, **kwargs):
        if Path(path).parent == r._inbox_dir(seat.home):
            attempted.append(Path(path).name)
            if fail_at is None or len(attempted) in fail_at:
                raise PermissionError("inbox temporarily unwritable")
        return original(path, text, *args, **kwargs)

    monkeypatch.setattr(r, "atomic_write_text", write)
    return attempted


def _capture_run(seat: Seat) -> None:
    def run():
        try:
            seat.result["code"] = r._run_session(seat.home, seat.spec, seat.stop, {"proc": None})
        except Exception as exc:  # the assertions name it
            seat.result["exception"] = repr(exc)
    seat._run = run


def _taken_texts(ctl: Path) -> list[str]:
    path = ctl / "taken.log"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []


@pytest.mark.parametrize("fail_at", [1, 2])
def test_a_requeue_write_error_loses_no_origin(tmp_path, monkeypatch, fail_at):
    """The reviewer's probe: after a genuine refusal, writing one of the two
    drained messages back fails. That one is reported failed once; the
    other is answered once by the second engine; neither is lost."""
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    seat.touch("reject-flag", "end-1", "result-1")
    for name in ("a", "b"):
        seat.enqueue(name)
    attempted = _deny_inbox_writes(monkeypatch, seat, {fail_at})
    _capture_run(seat)
    seat.start()
    try:
        _wait(lambda: "code" in seat.result or "exception" in seat.result
              or (len(seat.spawns()) == 2 and (ctl / "resulted-1").exists()
                  and seat.turns() == 0))
    finally:
        code = seat.finish()
    assert "exception" not in seat.result
    # The lost message makes the session a failure even though the kept
    # message was answered by the retry.
    assert code == 1
    assert len(attempted) == 2
    spawns = seat.spawns()
    assert len(spawns) == 2 and FLAG not in spawns[1]
    # a and b share an enqueue millisecond, so either may be written back
    # first. Whichever write failed is reported; the other is answered.
    failed = seat.failed_envelopes()
    assert len(failed) == 1
    lost = failed[0].removesuffix(".json")
    kept = {"a": "b", "b": "a"}[lost]
    # The refusing engine took nothing, so nothing was settled before the
    # requeue; the second engine took only the message put back, once.
    assert _taken_texts(ctl) == [f"message {kept}"]
    assert list(r._inbox_dir(seat.home).glob("*.json")) == []


def test_when_no_message_can_be_put_back_each_is_reported_once_and_nothing_reruns(
    tmp_path, monkeypatch,
):
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    seat.touch("reject-flag")
    for name in ("a", "b"):
        seat.enqueue(name)
    attempted = _deny_inbox_writes(monkeypatch, seat, None)
    _capture_run(seat)
    seat.start()
    seat.thread.join(timeout=15)
    assert not seat.thread.is_alive()
    assert seat.result == {"code": 1}
    assert len(attempted) == 2
    # The refusing engine read nothing, so nothing was settled before the
    # requeue; and no second engine started.
    assert seat.received() == 0 and _taken_texts(ctl) == []
    assert len(seat.spawns()) == 1
    assert seat.failed_envelopes() == ["a.json", "b.json"]
    assert list(r._inbox_dir(seat.home).glob("*.json")) == []


def test_a_remembered_refusal_holds_until_the_binary_changes(tmp_path):
    fake, ctl = make_merging_claude(tmp_path)
    seat = Seat(tmp_path, fake, ctl)
    seat.home.mkdir(parents=True, exist_ok=True)
    r._mark_echo_refused(seat.home, [str(fake)], FLAG)
    seat.touch("end-1", "result-1")
    try:
        seat.send("a")
        _wait(lambda: seat.received() == 1 and seat.turns() == 0)
    finally:
        assert seat.finish() == 0
    assert FLAG not in seat.spawns()[-1]

    # An update to the binary forgets the refusal.
    fake.write_text(fake.read_text() + "\n# updated\n")
    (ctl / "received").unlink()
    seat.enqueue("b")
    seat.start()
    try:
        _wait(lambda: seat.received() == 1 and seat.turns() == 0)
    finally:
        assert seat.finish() == 0
    assert FLAG in seat.spawns()[-1]
    assert seat.failed_envelopes() == []


def test_a_launcher_refusal_also_keys_on_the_claude_on_path(tmp_path, monkeypatch):
    """`ollama launch claude -- ...` runs the same adapter: an update to
    either the launcher or `claude` forgets the refusal."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "ollama"
    claude = bin_dir / "claude"
    for path in (launcher, claude):
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    home = tmp_path / "home"
    home.mkdir()
    argv = [str(launcher), "launch", "claude", "--", FLAG]
    r._mark_echo_refused(home, argv, FLAG)
    assert r._echo_refused(home, argv, FLAG)
    claude.write_text("#!/bin/sh\n# updated\n")
    assert not r._echo_refused(home, argv, FLAG)


def test_claude_resident_argv_asks_for_the_echo(tmp_path):
    argv = r.compose_resident_argv(
        ["claude", "-p", "{prompt}"], {"HOME": str(tmp_path)}, engine="claude",
    )
    assert FLAG in argv


def test_a_written_message_carries_its_id_and_only_an_echo_reports_one():
    line = json.loads(r._encode_claude_user_turn("hi", "id-1"))
    assert line["uuid"] == "id-1"
    assert "uuid" not in json.loads(r._encode_claude_user_turn("hi"))
    echoed = r._parse_claude_frame({
        "type": "user", "isReplay": True, "uuid": "id-1",
        "message": {"role": "user", "content": "hi"},
    })
    assert echoed.echo == "id-1" and not echoed.turn_completed
    tool_result = r._parse_claude_frame({
        "type": "user", "uuid": "id-2",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
        ]},
    })
    assert tool_result.echo is None
