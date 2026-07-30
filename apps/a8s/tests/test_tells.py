"""Tests for `tells` — the receive-side complement of `tell`.

`tells` resolves the node from `TELL_OUTBOX_DIR` (like `tell`), snapshots the
`.inbox` beside the outbox, then blocks up to `--timeout` for new envelopes.
The end-to-end timeout path is exercised through the repo-root `tells` shim; the
arrival paths inject messages from a background thread while `tells_main` polls.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from core import TELL_OUTBOX_DIR_ENV
from tells import TellsUsageError, parse_tells_argv, tells_main

TELLS = Path(__file__).resolve().parent.parent.parent.parent / "tells"


@pytest.fixture(autouse=True)
def _clear_glow_env(monkeypatch):
    monkeypatch.delenv("A8S_GLOW", raising=False)


def _setup_node(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "node"
    outbox = root / ".outbox"
    inbox = root / ".inbox"
    outbox.mkdir(parents=True)
    inbox.mkdir(parents=True)
    return outbox, inbox


def _drop_inbox(inbox: Path, sender: str, content: str, msg_id: str) -> None:
    msg = {"id": msg_id, "from": sender, "to": "NODE", "content": content, "files": []}
    tmp = inbox / f".{msg_id}.tmp"
    tmp.write_text(json.dumps(msg), encoding="utf-8")
    os.replace(tmp, inbox / f"{msg_id}.json")


def _deliver_after(inbox: Path, delay: float, messages: list[tuple[str, str, str]]) -> threading.Thread:
    def worker() -> None:
        time.sleep(delay)
        for sender, content, msg_id in messages:
            _drop_inbox(inbox, sender, content, msg_id)

    t = threading.Thread(target=worker)
    t.start()
    return t


def test_tells_prints_arriving_message(tmp_path, monkeypatch, capsys):
    outbox, inbox = _setup_node(tmp_path)
    monkeypatch.setenv(TELL_OUTBOX_DIR_ENV, str(outbox))
    t = _deliver_after(inbox, 0.2, [("BOB", "here is the answer", "01MSGARRIVE0000000000000")])
    rc = tells_main([])
    t.join()
    out = capsys.readouterr().out
    assert rc == 0
    assert "BOB: here is the answer" in out


def test_tells_prints_burst(tmp_path, monkeypatch, capsys):
    outbox, inbox = _setup_node(tmp_path)
    monkeypatch.setenv(TELL_OUTBOX_DIR_ENV, str(outbox))
    burst = [
        ("BOB", "first", "01BURST00000000000000000A"),
        ("CAROL", "second", "01BURST00000000000000000B"),
        ("BOB", "third", "01BURST00000000000000000C"),
    ]
    t = _deliver_after(inbox, 0.2, burst)
    rc = tells_main([])
    t.join()
    out = capsys.readouterr().out
    assert rc == 0
    assert "BOB: first" in out
    assert "CAROL: second" in out
    assert "BOB: third" in out


def test_tells_ignores_preexisting_messages(tmp_path, monkeypatch, capsys):
    outbox, inbox = _setup_node(tmp_path)
    monkeypatch.setenv(TELL_OUTBOX_DIR_ENV, str(outbox))
    _drop_inbox(inbox, "BOB", "old news", "01PREEXIST000000000000000")
    rc = tells_main(["--timeout", "0.5"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no message within" in err


def test_tells_timeout_exits_1(tmp_path, monkeypatch, capsys):
    outbox, _inbox = _setup_node(tmp_path)
    monkeypatch.setenv(TELL_OUTBOX_DIR_ENV, str(outbox))
    rc = tells_main(["--timeout", "0.5"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no message within 0.5s" in err


def test_tells_without_outbox_env_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(TELL_OUTBOX_DIR_ENV, raising=False)
    monkeypatch.setenv("A8S_HOME", str(tmp_path / "empty-a8s-home"))
    rc = tells_main([])
    err = capsys.readouterr().err
    assert rc == 1
    assert "cannot receive from this directory" in err


def test_tells_rejects_unknown_arg(tmp_path, monkeypatch, capsys):
    rc = tells_main(["--nope"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "usage: tells" in err


def test_tells_timeout_requires_value(monkeypatch, capsys):
    rc = tells_main(["--timeout"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "--timeout requires seconds" in err


def test_tells_help_exits_0(capsys):
    rc = tells_main(["--help"])
    err = capsys.readouterr().err
    assert rc == 0
    assert "usage: tells" in err
    assert "--follow" in err
    assert "--glow" in err
    assert "--body-max" in err
    assert "heading templates" in err


def test_format_displayed_content_appends_recovery_command(tmp_path):
    from tells import format_displayed_content

    path = tmp_path / "01MSG.json"
    path.write_text("{}", encoding="utf-8")
    body = "abcdefghij"
    out = format_displayed_content(body, path, body_max=4)
    assert out.startswith("abcd\n")
    assert "truncated at 4 chars" in out
    assert "full message:" in out
    assert "python3 -c" in out
    assert str(path.resolve()) in out
    assert "['content']" in out


def test_format_displayed_content_unlimited_when_zero(tmp_path):
    from tells import format_displayed_content

    path = tmp_path / "01MSG.json"
    body = "x" * 100
    assert format_displayed_content(body, path, body_max=0) == body


def test_tells_truncates_long_body_with_recovery_command(tmp_path, monkeypatch, capsys):
    outbox, inbox = _setup_node(tmp_path)
    monkeypatch.setenv(TELL_OUTBOX_DIR_ENV, str(outbox))
    long_body = "HEAD" + ("x" * 200) + "TAIL"
    msg_id = "01LONGBODY000000000000000"
    t = _deliver_after(inbox, 0.2, [("BOB", long_body, msg_id)])
    rc = tells_main(["--body-max", "20"])
    t.join()
    out = capsys.readouterr().out
    assert rc == 0
    assert "BOB: HEAD" in out
    assert "xxxxx" in out
    assert "TAIL" not in out
    assert "truncated at 20 chars" in out
    assert f"{msg_id}.json" in out
    assert "python3 -c" in out


def test_tells_body_max_zero_prints_full_body(tmp_path, monkeypatch, capsys):
    outbox, inbox = _setup_node(tmp_path)
    monkeypatch.setenv(TELL_OUTBOX_DIR_ENV, str(outbox))
    long_body = "y" * 500
    t = _deliver_after(inbox, 0.2, [("BOB", long_body, "01FULLBODY000000000000000")])
    rc = tells_main(["--body-max", "0"])
    t.join()
    out = capsys.readouterr().out
    assert rc == 0
    assert long_body in out
    assert "truncated" not in out


def test_parse_tells_body_max_and_env(monkeypatch):
    from tells import DEFAULT_BODY_MAX_CHARS, TELLS_BODY_MAX_ENV

    monkeypatch.delenv(TELLS_BODY_MAX_ENV, raising=False)
    assert parse_tells_argv([]).body_max == DEFAULT_BODY_MAX_CHARS
    monkeypatch.setenv(TELLS_BODY_MAX_ENV, "500")
    assert parse_tells_argv([]).body_max == 500
    assert parse_tells_argv(["--body-max", "0"]).body_max == 0
    with pytest.raises(TellsUsageError, match="body-max"):
        parse_tells_argv(["--body-max", "-1"])


def test_tells_follow_prints_waves(tmp_path, monkeypatch, capsys):
    outbox, inbox = _setup_node(tmp_path)
    monkeypatch.setenv(TELL_OUTBOX_DIR_ENV, str(outbox))
    sleeps = {"n": 0}

    def fake_sleep(_interval: float) -> None:
        sleeps["n"] += 1
        if sleeps["n"] == 1:
            _drop_inbox(inbox, "BOB", "first", "01FOLLOW000000000000000A")
            return
        if sleeps["n"] == 2:
            _drop_inbox(inbox, "CAROL", "second", "01FOLLOW000000000000000B")
            return
        raise KeyboardInterrupt

    import tells as tells_mod

    monkeypatch.setattr(tells_mod.time, "sleep", fake_sleep)
    rc = tells_main(["-f"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "BOB: first" in out
    assert "CAROL: second" in out


def test_tells_rejects_follow_with_positive_timeout(capsys):
    rc = tells_main(["-f", "--timeout", "30"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "cannot use -f/--follow with a positive --timeout" in err


def test_tells_timeout_zero_follows_like_flag(tmp_path, monkeypatch, capsys):
    outbox, inbox = _setup_node(tmp_path)
    monkeypatch.setenv(TELL_OUTBOX_DIR_ENV, str(outbox))
    sleeps = {"n": 0}

    def fake_sleep(_interval: float) -> None:
        sleeps["n"] += 1
        if sleeps["n"] == 1:
            _drop_inbox(inbox, "BOB", "via zero", "01TIMEOUT000000000000000")
            return
        raise KeyboardInterrupt

    import tells as tells_mod

    monkeypatch.setattr(tells_mod.time, "sleep", fake_sleep)
    rc = tells_main(["--timeout", "0"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "BOB: via zero" in out


def test_parse_tells_timeout_zero_is_follow_forever():
    opts = parse_tells_argv(["--timeout", "0"])
    assert opts.follow_forever is True


def test_parse_tells_rejects_follow_with_positive_timeout():
    with pytest.raises(TellsUsageError, match="cannot use -f/--follow with a positive --timeout"):
        parse_tells_argv(["-f", "--timeout", "30"])


def test_tells_timed_follow_prints_waves(tmp_path, monkeypatch, capsys):
    outbox, inbox = _setup_node(tmp_path)
    monkeypatch.setenv(TELL_OUTBOX_DIR_ENV, str(outbox))
    sleeps = {"n": 0}
    monotonic = {"t": 0.0}

    def fake_sleep(_interval: float) -> None:
        sleeps["n"] += 1
        if sleeps["n"] == 1:
            monotonic["t"] = 0.1
            _drop_inbox(inbox, "BOB", "first", "01TIMED0000000000000000A")
            return
        if sleeps["n"] == 2:
            monotonic["t"] = 0.2
            _drop_inbox(inbox, "CAROL", "second", "01TIMED0000000000000000B")
            return
        monotonic["t"] = 6.0

    import tells as tells_mod

    monkeypatch.setattr(tells_mod.time, "sleep", fake_sleep)
    monkeypatch.setattr(tells_mod.time, "monotonic", lambda: monotonic["t"])
    rc = tells_main(["--timeout", "5"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "BOB: first" in out
    assert "CAROL: second" in out


def test_parse_tells_glow_and_heading_flags():
    opts = parse_tells_argv(["--glow", "dracula", "--heading-in", "### {from}"])
    assert opts.glow_theme == "dracula"
    assert opts.heading_in == "### {from}"
    assert opts.markdown is True


def test_parse_tells_glow_without_theme():
    opts = parse_tells_argv(["--glow"])
    assert opts.glow_theme == "auto"
    assert opts.markdown is True


def test_parse_tells_glow_env(monkeypatch):
    monkeypatch.setenv("A8S_GLOW", "tokyo-night")
    opts = parse_tells_argv([])
    assert opts.glow_theme == "tokyo-night"
    assert opts.markdown is True


def test_tells_markdown_heading_without_glow(tmp_path, monkeypatch, capsys):
    outbox, inbox = _setup_node(tmp_path)
    monkeypatch.setenv(TELL_OUTBOX_DIR_ENV, str(outbox))
    monkeypatch.delenv("A8S_GLOW", raising=False)
    t = _deliver_after(inbox, 0.2, [("BOB", "markdown body", "01MDHEAD00000000000000000")])
    rc = tells_main(["--heading-in", "### from {from}"])
    t.join()
    out = capsys.readouterr().out
    assert rc == 0
    assert "### from BOB" in out
    assert "markdown body" in out
    assert "BOB: markdown body" not in out


def test_tells_glow_renders_through_stream(tmp_path, monkeypatch, capsys):
    outbox, inbox = _setup_node(tmp_path)
    monkeypatch.setenv(TELL_OUTBOX_DIR_ENV, str(outbox))
    monkeypatch.delenv("A8S_GLOW", raising=False)
    writes: list[str] = []

    class FakeGlow:
        def write(self, text: str) -> int:
            writes.append(text)
            return len(text)

        def finalize(self) -> None:
            pass

        def close(self) -> None:
            writes.append("__close__")

    monkeypatch.setattr("convo.open_glow_stdout", lambda theme: (writes.append(f"open:{theme}") or FakeGlow()))

    t = _deliver_after(inbox, 0.2, [("BOB", "glow body", "01GLOWTELL000000000000000")])
    rc = tells_main(["--glow", "dracula"])
    t.join()
    assert rc == 0
    assert "open:dracula" in writes
    assert any("glow body" in w for w in writes)
    assert "__close__" in writes
    assert capsys.readouterr().out == ""


def test_tells_shim_times_out(tmp_path):
    outbox, _inbox = _setup_node(tmp_path)
    env = dict(os.environ)
    env[TELL_OUTBOX_DIR_ENV] = str(outbox)
    res = subprocess.run(
        [str(TELLS), "--timeout", "0.5"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 1
    assert "no message within" in res.stderr


def test_tells_follow_sees_delivery_after_inbox_created(tmp_path, monkeypatch, capsys):
    """``tells -f`` before the handler has created ``.inbox`` must still print."""
    root = tmp_path / "node"
    outbox = root / ".outbox"
    outbox.mkdir(parents=True)
    # Deliberately no .inbox yet — mimics starting tells before ``a8s start``.
    monkeypatch.setenv(TELL_OUTBOX_DIR_ENV, str(outbox))
    sleeps = {"n": 0}

    def fake_sleep(_interval: float) -> None:
        sleeps["n"] += 1
        if sleeps["n"] == 1:
            inbox = root / ".inbox"
            # Handler may replace/recreate; tells already mkdir'd an empty dir.
            inbox.mkdir(parents=True, exist_ok=True)
            _drop_inbox(inbox, "BOB", "after-start", "01LATEINBOX00000000000000")
            return
        raise KeyboardInterrupt

    import tells as tells_mod

    monkeypatch.setattr(tells_mod.time, "sleep", fake_sleep)
    rc = tells_main(["-f"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "BOB: after-start" in out


def test_tells_follow_prints_overwrite_of_seen_filename(tmp_path, monkeypatch, capsys):
    """Proxy overwrite of an existing ULID must not stay invisible to follow."""
    outbox, inbox = _setup_node(tmp_path)
    monkeypatch.setenv(TELL_OUTBOX_DIR_ENV, str(outbox))
    _drop_inbox(inbox, "OLD", "stale", "01SAME0000000000000000000")
    sleeps = {"n": 0}
    real_sleep = time.sleep

    def fake_sleep(_interval: float) -> None:
        sleeps["n"] += 1
        if sleeps["n"] == 1:
            real_sleep(0.01)  # advance mtime; must not use patched time.sleep
            _drop_inbox(inbox, "BOB", "fresh-delivery", "01SAME0000000000000000000")
            return
        raise KeyboardInterrupt

    import tells as tells_mod

    monkeypatch.setattr(tells_mod.time, "sleep", fake_sleep)
    rc = tells_main(["-f"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "BOB: fresh-delivery" in out
    assert "stale" not in out
