"""Tests for `tell` — invoked as a subprocess via the repo-root `tell` shim.

The shim delegates to `a8s tell`, which requires `TELL_OUTBOX_DIR` (a8s sets
this on wake). Tests pass it explicitly when exercising tell directly.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from core import TELL_OUTBOX_DIR_ENV, files_dir, inbound_bundle_dir, outbox_bundle_dir, outbox_dir

TELL = Path(__file__).resolve().parent.parent.parent.parent / "tell"
A8S_TELL = [
    sys.executable,
    str(Path(__file__).resolve().parent.parent / "a8s.py"),
    "tell",
]


def _merge_tell_env(
    cwd: Path,
    env: dict[str, str] | None = None,
    *,
    outbox: Path | None = None,
) -> dict[str, str]:
    merged = dict(os.environ)
    extra = dict(env or {})
    if TELL_OUTBOX_DIR_ENV not in extra:
        target = outbox if outbox is not None else cwd / ".outbox"
        if outbox is not None or target.is_dir():
            extra[TELL_OUTBOX_DIR_ENV] = str(target.resolve() if outbox is None else target)
        else:
            # Don't let the host shell's TELL_OUTBOX_DIR leak into negative tests.
            merged.pop(TELL_OUTBOX_DIR_ENV, None)
    merged.update(extra)
    return merged


def _run(
    cwd: Path,
    *args: str,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
    outbox: Path | None = None,
) -> subprocess.CompletedProcess:
    kw: dict = {
        "cwd": str(cwd),
        "capture_output": True,
        "text": True,
        "env": _merge_tell_env(cwd, env, outbox=outbox),
    }
    if stdin is not None:
        kw["input"] = stdin
    return subprocess.run([str(TELL), *args], **kw)


def _run_a8s(
    cwd: Path,
    *args: str,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
    outbox: Path | None = None,
) -> subprocess.CompletedProcess:
    kw: dict = {
        "cwd": str(cwd),
        "capture_output": True,
        "text": True,
        "env": _merge_tell_env(cwd, env, outbox=outbox),
    }
    if stdin is not None:
        kw["input"] = stdin
    return subprocess.run([*A8S_TELL, *args], **kw)


def _read_outbox(outbox: Path) -> tuple[str, dict]:
    files = list(outbox.glob("*.json"))
    assert len(files) == 1, f"expected exactly one outbox file, found {files}"
    return files[0].name, json.loads(files[0].read_text())


def _assert_staged_files(outbox: Path, msg: dict, original_names: list[str]) -> None:
    assert len(msg["files"]) == len(original_names)
    bundle = outbox_bundle_dir(outbox, msg["id"])
    for entry, orig in zip(msg["files"], original_names, strict=True):
        assert entry == {"filename": orig}
        assert (bundle / orig).is_file()


def test_tell_writes_outbox_from_root(tmp_path):
    (tmp_path / ".outbox").mkdir()
    res = _run(tmp_path, "gerry", "hello there")
    assert res.returncode == 0, res.stderr
    name, msg = _read_outbox(tmp_path / ".outbox")
    assert name.endswith(".json")
    assert msg["to"] == "gerry"
    assert msg["content"] == "hello there"
    assert msg["files"] == []
    assert "id" in msg and len(msg["id"]) == 26
    assert "date" in msg and msg["date"].endswith("Z")


def test_a8s_tell_writes_outbox_without_registry(tmp_path):
    (tmp_path / ".outbox").mkdir()
    res = _run_a8s(tmp_path, "gerry", "via a8s tell")
    assert res.returncode == 0, res.stderr
    _name, msg = _read_outbox(tmp_path / ".outbox")
    assert msg["to"] == "gerry"
    assert msg["content"] == "via a8s tell"


def test_tell_requires_tell_outbox_dir_from_subdir(tmp_path):
    outbox = tmp_path / ".outbox"
    outbox.mkdir()
    sub = tmp_path / "deep" / "nested"
    sub.mkdir(parents=True)
    res = _run(sub, "codex", "from below")
    assert res.returncode != 0
    assert "TELL_OUTBOX_DIR is not set" in res.stderr

    res = _run(sub, "codex", "from below", outbox=outbox)
    assert res.returncode == 0, res.stderr
    _name, msg = _read_outbox(outbox)
    assert msg["to"] == "codex"
    assert msg["content"] == "from below"


def test_tell_errors_when_no_outbox(tmp_path):
    res = _run(tmp_path, "anyone", "should fail")
    assert res.returncode != 0
    assert "TELL_OUTBOX_DIR is not set" in res.stderr


def test_tell_help_is_opaque(tmp_path):
    res = _run(tmp_path, "--help")
    assert res.returncode == 0
    assert ".outbox" not in res.stderr
    assert ".temp" not in res.stderr


def test_tell_outbox_dir_locks_over_cwd_outbox(tmp_path):
    locked = tmp_path / "mailbox" / ".outbox"
    locked.mkdir(parents=True)
    cwd_agent = tmp_path / "cwd-agent"
    (cwd_agent / ".outbox").mkdir(parents=True)
    res = _run(
        cwd_agent,
        "gerry",
        "locked send",
        env={"TELL_OUTBOX_DIR": str(locked)},
    )
    assert res.returncode == 0, res.stderr
    assert list((cwd_agent / ".outbox").glob("*.json")) == []
    _name, msg = _read_outbox(locked)
    assert msg["content"] == "locked send"


def test_tell_outbox_dir_creates_when_missing(tmp_path):
    outbox = tmp_path / "mailbox" / ".outbox"
    assert not outbox.exists()
    res = _run(
        tmp_path,
        "gerry",
        "created",
        env={"TELL_OUTBOX_DIR": str(outbox)},
    )
    assert res.returncode == 0, res.stderr
    assert outbox.is_dir()
    _name, msg = _read_outbox(outbox)
    assert msg["content"] == "created"


def test_tell_fails_when_outbox_not_writable(tmp_path):
    outbox = tmp_path / ".outbox"
    outbox.mkdir()
    outbox.chmod(0o555)
    try:
        res = _run(tmp_path, "gerry", "nope", outbox=outbox)
        assert res.returncode != 0
        assert "outbox is unavailable" in res.stderr
    finally:
        outbox.chmod(0o755)


def test_tell_lifts_file_lines_into_files_array(tmp_path):
    (tmp_path / ".outbox").mkdir()
    (tmp_path / "report.pdf").write_text("r")
    (tmp_path / "data.csv").write_text("d")
    res = _run(tmp_path, "gerry", "Here you go.", "FILE: ./report.pdf", f"FILE: {tmp_path / 'data.csv'}")
    assert res.returncode == 0, res.stderr
    _name, msg = _read_outbox(tmp_path / ".outbox")
    assert msg["content"] == "Here you go."
    _assert_staged_files(tmp_path / ".outbox", msg, ["report.pdf", "data.csv"])


def test_tell_handles_inline_newline_file_lines(tmp_path):
    (tmp_path / ".outbox").mkdir()
    (tmp_path / "report.pdf").write_text("r")
    body = "Here you go.\nFILE: ./report.pdf"
    res = _run(tmp_path, "gerry", body)
    assert res.returncode == 0, res.stderr
    _name, msg = _read_outbox(tmp_path / ".outbox")
    assert msg["content"] == "Here you go."
    _assert_staged_files(tmp_path / ".outbox", msg, ["report.pdf"])


def test_tell_omits_from_field_without_registry(tmp_path):
    (tmp_path / ".outbox").mkdir()
    res = _run(tmp_path, "x", "y")
    assert res.returncode == 0, res.stderr
    _name, msg = _read_outbox(tmp_path / ".outbox")
    assert "from" not in msg


def test_tell_ids_are_unique_across_rapid_invocations(tmp_path):
    (tmp_path / ".outbox").mkdir()
    ids = set()
    for i in range(10):
        res = _run(tmp_path, "x", f"msg-{i}")
        assert res.returncode == 0, res.stderr
        ids.update(p.stem for p in (tmp_path / ".outbox").glob("*.json"))
    assert len(ids) == 10


def test_tell_id_is_crockford_base32_ulid(tmp_path):
    (tmp_path / ".outbox").mkdir()
    res = _run(tmp_path, "x", "y")
    assert res.returncode == 0, res.stderr
    _name, msg = _read_outbox(tmp_path / ".outbox")
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", msg["id"]), msg["id"]


def test_tell_no_args_prints_usage(tmp_path):
    res = _run(tmp_path)
    assert res.returncode == 2
    assert "usage: tell" in res.stderr


def test_tell_only_recipient_prints_usage(tmp_path):
    (tmp_path / ".outbox").mkdir()
    res = _run(tmp_path, "gerry")
    assert res.returncode == 2
    assert "usage: tell" in res.stderr


def test_tell_envelope_shape_is_router_compatible(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from mailbox import _split_content_and_files
    finally:
        sys.path.pop(0)
    (tmp_path / ".outbox").mkdir()
    (tmp_path / "x.txt").write_text("x")
    res = _run(tmp_path, "gerry", "header line\nbody line", "FILE: ./x.txt")
    assert res.returncode == 0, res.stderr
    _name, msg = _read_outbox(tmp_path / ".outbox")
    assert msg["to"] == "gerry"
    _assert_staged_files(tmp_path / ".outbox", msg, ["x.txt"])
    assert "header line" in msg["content"]
    assert "body line" in msg["content"]


def test_tell_attach_flag(tmp_path):
    (tmp_path / ".outbox").mkdir()
    (tmp_path / "report.pdf").write_text("r")
    res = _run(tmp_path, "gerry", "--attach", "./report.pdf", "see attached")
    assert res.returncode == 0, res.stderr
    _name, msg = _read_outbox(tmp_path / ".outbox")
    assert msg["content"] == "see attached"
    _assert_staged_files(tmp_path / ".outbox", msg, ["report.pdf"])


def test_tell_file_flag_is_alias_for_attach(tmp_path):
    (tmp_path / ".outbox").mkdir()
    (tmp_path / "data.csv").write_text("d")
    res = _run(tmp_path, "gerry", "--file", "./data.csv", "csv inside")
    assert res.returncode == 0, res.stderr
    _name, msg = _read_outbox(tmp_path / ".outbox")
    _assert_staged_files(tmp_path / ".outbox", msg, ["data.csv"])


def test_tell_attach_before_recipient(tmp_path):
    (tmp_path / ".outbox").mkdir()
    (tmp_path / "a.txt").write_text("a")
    res = _run(tmp_path, "--attach", "./a.txt", "bob", "hello")
    assert res.returncode == 0, res.stderr
    _name, msg = _read_outbox(tmp_path / ".outbox")
    assert msg["to"] == "bob"
    _assert_staged_files(tmp_path / ".outbox", msg, ["a.txt"])


def test_tell_multiple_attachments(tmp_path):
    (tmp_path / ".outbox").mkdir()
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    res = _run(
        tmp_path,
        "gerry",
        "--attach",
        "./a.txt",
        "--file",
        "./b.txt",
        "two files",
    )
    assert res.returncode == 0, res.stderr
    _name, msg = _read_outbox(tmp_path / ".outbox")
    _assert_staged_files(tmp_path / ".outbox", msg, ["a.txt", "b.txt"])


def test_tell_attach_equals_form(tmp_path):
    (tmp_path / ".outbox").mkdir()
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    res = _run(tmp_path, "gerry", "--attach=./a.txt", "--file=./b.txt", "eq form")
    assert res.returncode == 0, res.stderr
    _name, msg = _read_outbox(tmp_path / ".outbox")
    _assert_staged_files(tmp_path / ".outbox", msg, ["a.txt", "b.txt"])


def test_tell_attach_multiple_paths_after_one_flag(tmp_path):
    (tmp_path / ".outbox").mkdir()
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    res = _run(tmp_path, "gerry", "--attach", "./a.txt", "./b.txt", "multi path")
    assert res.returncode == 0, res.stderr
    _name, msg = _read_outbox(tmp_path / ".outbox")
    _assert_staged_files(tmp_path / ".outbox", msg, ["a.txt", "b.txt"])


def test_tell_attach_then_long_prompt_does_not_crash(tmp_path):
    """After --attach, tell probes the next argv as a file. Prompts longer than
    NAME_MAX raise ENAMETOOLONG on some Pythons; that must be message text."""
    (tmp_path / ".outbox").mkdir()
    (tmp_path / "a.txt").write_text("a")
    long_prompt = ("research " + ("detail " * 400)).rstrip()
    assert len(long_prompt) > 255
    res = _run(tmp_path, "gerry", "--attach", "./a.txt", long_prompt)
    assert res.returncode == 0, res.stderr
    _name, msg = _read_outbox(tmp_path / ".outbox")
    assert msg["content"] == long_prompt
    _assert_staged_files(tmp_path / ".outbox", msg, ["a.txt"])


def test_parse_tell_argv_long_token_after_attach_is_message():
    from tell import parse_tell_argv

    long_prompt = "x" * 300
    recipient, attachments, message_argv, check, split = parse_tell_argv(
        ["bob", "--attach", "./nope-missing.txt", long_prompt]
    )
    assert recipient == "bob"
    assert attachments == ["./nope-missing.txt"]
    assert message_argv == [long_prompt]
    assert check is False
    assert split is False


def test_tell_rejects_oversized_attachment_without_split(tmp_path, monkeypatch):
    from core import TELL_FILE_MAX_ENV

    (tmp_path / ".outbox").mkdir()
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 2000)
    monkeypatch.setenv(TELL_FILE_MAX_ENV, "1000")
    res = _run(tmp_path, "gerry", "--attach", str(big), "too big")
    assert res.returncode == 1
    assert "pass --split" in res.stderr
    assert "big.bin" in res.stderr
    assert list((tmp_path / ".outbox").glob("*.json")) == []


def test_tell_split_chunks_oversized_attachment(tmp_path, monkeypatch):
    from core import TELL_FILE_MAX_ENV

    (tmp_path / ".outbox").mkdir()
    big = tmp_path / "big.bin"
    big.write_bytes(b"abcdefghij" * 200)  # 2000 bytes
    monkeypatch.setenv(TELL_FILE_MAX_ENV, "1000")
    res = _run(tmp_path, "gerry", "--attach", str(big), "--split", "chunked")
    assert res.returncode == 0, res.stderr
    assert "splitting" in res.stderr
    _name, msg = _read_outbox(tmp_path / ".outbox")
    names = [e["filename"] for e in msg["files"]]
    assert names == ["big.bin.part001of002", "big.bin.part002of002"]
    outbox = tmp_path / ".outbox"
    bundle = outbox_bundle_dir(outbox, msg["id"])
    assert (bundle / names[0]).stat().st_size == 1000
    assert (bundle / names[1]).stat().st_size == 1000
    assert not (outbox / f".{msg['id']}.parts").exists()


def test_parse_byte_size_suffixes():
    from tell import parse_byte_size

    assert parse_byte_size("1000") == 1000
    assert parse_byte_size("2k") == 2048
    assert parse_byte_size("1MB") == 1024 * 1024
    assert parse_byte_size("50m") == 50 * 1024 * 1024


def test_tell_staged_duplicate_basenames_use_separate_message_dirs(tmp_path):
    (tmp_path / ".outbox").mkdir()
    doc = tmp_path / "untitled.doc"
    doc.write_text("v1")
    res1 = _run(tmp_path, "bob", "--attach", "./untitled.doc", "first")
    assert res1.returncode == 0, res1.stderr
    outbox = tmp_path / ".outbox"
    res2 = _run(tmp_path, "bob", "--attach", "./untitled.doc", "second")
    assert res2.returncode == 0, res2.stderr
    msgs = [json.loads(p.read_text()) for p in outbox.glob("*.json")]
    assert len(msgs) == 2
    assert all(m["files"] == [{"filename": "untitled.doc"}] for m in msgs)
    assert msgs[0]["id"] != msgs[1]["id"]
    for m in msgs:
        assert (outbox_bundle_dir(outbox, m["id"]) / "untitled.doc").is_file()


def test_tell_absolutizes_attach_relative_to_cwd_not_outbox_root(tmp_path):
    agent_root = tmp_path / "agent"
    work = agent_root / "project"
    work.mkdir(parents=True)
    (agent_root / ".outbox").mkdir()
    payload = work / "report.pdf"
    payload.write_text("payload")
    res = _run(work, "bob", "--attach", "report.pdf", "see attached", outbox=agent_root / ".outbox")
    assert res.returncode == 0, res.stderr
    _name, msg = _read_outbox(agent_root / ".outbox")
    _assert_staged_files(agent_root / ".outbox", msg, ["report.pdf"])


def test_tell_absolutized_attach_delivers_after_routing(fake_home, tmp_path):
    from core import Participant, inbox_dir
    from mailbox import ensure_mailboxes, route_outboxes
    from registry import save_registry

    sender_root = tmp_path / "sender"
    recipient_root = tmp_path / "recipient"
    work = sender_root / "project"
    work.mkdir(parents=True)
    (sender_root / ".outbox").mkdir()
    recipient_root.mkdir()
    payload = work / "data.txt"
    payload.write_text("hello file")
    save_registry(
        {"SENDER": {"root": str(sender_root)}, "BOB": {"root": str(recipient_root)}}
    )
    res = _run_a8s(
        work,
        "BOB",
        "--attach",
        "data.txt",
        "see attached",
        outbox=sender_root / ".outbox",
    )
    assert res.returncode == 0, res.stderr
    _name, out_msg = _read_outbox(sender_root / ".outbox")
    msg_id = out_msg["id"]
    sender = Participant("SENDER", sender_root)
    bob = Participant("BOB", recipient_root)
    ensure_mailboxes(sender)
    ensure_mailboxes(bob)
    route_outboxes([sender, bob], all_agents=[sender, bob])
    assert (bob.files_bundle_dir(msg_id) / "data.txt").read_text() == "hello file"
    delivered = json.loads(next(inbox_dir("BOB").iterdir()).read_text())
    assert delivered["files"] == [{"filename": "data.txt"}]


def test_tell_attaches_any_readable_path(tmp_path):
    agent = tmp_path / "agent"
    (agent / ".outbox").mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    res = _run(agent, "bob", "--attach", str(outside), "hi")
    assert res.returncode == 0, res.stderr
    _name, msg = _read_outbox(agent / ".outbox")
    _assert_staged_files(agent / ".outbox", msg, ["outside.txt"])


def test_tell_rejects_missing_attachment(tmp_path):
    (tmp_path / ".outbox").mkdir()
    res = _run(tmp_path, "bob", "--attach", "./missing.txt", "hi")
    assert res.returncode == 1
    assert "not found" in res.stderr


def _outbox_dirs(outbox: Path) -> list[Path]:
    return [p for p in outbox.iterdir() if p.is_dir()]


requires_unprivileged = pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores file permission bits"
)


@requires_unprivileged
def test_tell_staging_failure_removes_partial_bundle(tmp_path):
    (tmp_path / ".outbox").mkdir()
    good = tmp_path / "good.txt"
    good.write_text("ok")
    locked = tmp_path / "locked.bin"
    locked.write_bytes(b"secret")
    locked.chmod(0o000)
    res = _run(
        tmp_path, "bob", "--attach", str(good), "--attach", str(locked), "hi"
    )
    assert res.returncode == 1
    assert "staging failed" in res.stderr
    outbox = tmp_path / ".outbox"
    assert list(outbox.glob("*.json")) == []
    assert _outbox_dirs(outbox) == []


@requires_unprivileged
def test_tell_staging_failure_cleans_split_parts_dir(tmp_path, monkeypatch):
    from core import TELL_FILE_MAX_ENV

    (tmp_path / ".outbox").mkdir()
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 2000)
    locked = tmp_path / "locked.bin"
    locked.write_bytes(b"y")
    locked.chmod(0o000)
    monkeypatch.setenv(TELL_FILE_MAX_ENV, "1000")
    res = _run(
        tmp_path,
        "bob",
        "--attach",
        str(big),
        "--attach",
        str(locked),
        "--split",
        "hi",
    )
    assert res.returncode == 1
    assert "staging failed" in res.stderr
    outbox = tmp_path / ".outbox"
    assert list(outbox.glob("*.json")) == []
    assert _outbox_dirs(outbox) == []
    assert list(outbox.glob(".*.parts")) == []


def test_tell_unknown_recipient_with_attachment_leaves_clean_outbox(
    fake_home, tmp_path, monkeypatch
):
    from registry import save_registry

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    (agent_root / ".outbox").mkdir()
    save_registry({"SENDER": {"root": str(agent_root)}})
    monkeypatch.chdir(agent_root)
    payload = agent_root / "payload.txt"
    payload.write_text("x")

    res = _run_a8s(agent_root, "ghost", "--attach", str(payload), "hi")
    assert res.returncode == 1
    assert "no agent or alias named 'ghost'" in res.stderr
    outbox = agent_root / ".outbox"
    assert list(outbox.glob("*.json")) == []
    assert _outbox_dirs(outbox) == []


def test_tell_success_leaves_only_bundle_and_envelope(tmp_path):
    (tmp_path / ".outbox").mkdir()
    doc = tmp_path / "doc.txt"
    doc.write_text("payload")
    res = _run(tmp_path, "bob", "--attach", str(doc), "hi")
    assert res.returncode == 0, res.stderr
    outbox = tmp_path / ".outbox"
    _name, msg = _read_outbox(outbox)
    _assert_staged_files(outbox, msg, ["doc.txt"])
    assert _outbox_dirs(outbox) == [outbox_bundle_dir(outbox, msg["id"])]


def test_tell_stages_attach_from_cwd_when_outbox_via_tell_outbox_dir(tmp_path):
    outbox = tmp_path / "mailbox" / ".outbox"
    outbox.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    note = workspace / "note.txt"
    note.write_text("x")
    res = _run(
        workspace,
        "bob",
        "--attach",
        "./note.txt",
        "hi",
        env={"TELL_OUTBOX_DIR": str(outbox)},
    )
    assert res.returncode == 0, res.stderr
    _name, msg = _read_outbox(outbox)
    _assert_staged_files(outbox, msg, ["note.txt"])
    assert (outbox_bundle_dir(outbox, msg["id"]) / "note.txt").read_text() == "x"


def test_tell_stdin_dash(tmp_path):
    (tmp_path / ".outbox").mkdir()
    res = _run(tmp_path, "gerry", "-", stdin="payload from stdin\n")
    assert res.returncode == 0, res.stderr
    _name, msg = _read_outbox(tmp_path / ".outbox")
    assert msg["content"] == "payload from stdin"


def test_tell_stdin_auto_detect(tmp_path):
    (tmp_path / ".outbox").mkdir()
    res = _run(tmp_path, "gerry", stdin="auto-detected body")
    assert res.returncode == 0, res.stderr
    _name, msg = _read_outbox(tmp_path / ".outbox")
    assert msg["content"] == "auto-detected body"


def test_tell_stamps_from_when_registered(fake_home, tmp_path, monkeypatch):
    from registry import save_registry

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    (agent_root / ".outbox").mkdir()
    save_registry({"SENDER": {"root": str(agent_root)}, "bob": {"root": str(tmp_path / "bob")}})
    (tmp_path / "bob").mkdir()
    monkeypatch.chdir(agent_root)

    res = _run_a8s(agent_root, "bob", "registered send")
    assert res.returncode == 0, res.stderr
    assert res.stdout.count("tell -> bob:") == 1
    _name, msg = _read_outbox(agent_root / ".outbox")
    assert msg["from"] == "SENDER"
    assert msg["to"] == "bob"


def test_tell_attach_requires_path(tmp_path):
    (tmp_path / ".outbox").mkdir()
    res = _run(tmp_path, "gerry", "--attach")
    assert res.returncode == 2
    assert "--attach requires a path" in res.stderr


def test_tell_check_ok_without_recipient(tmp_path):
    (tmp_path / ".outbox").mkdir()
    res = _run(tmp_path, "--check")
    assert res.returncode == 0, res.stderr
    assert res.stdout.splitlines()[0] == "tell: ok"
    assert f"outbox: {tmp_path.resolve() / '.outbox'}" in res.stdout
    assert list((tmp_path / ".outbox").glob("*.json")) == []


def test_tell_check_validates_recipient(fake_home, tmp_path, monkeypatch):
    from registry import save_registry

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    (agent_root / ".outbox").mkdir()
    bob_root = tmp_path / "bob"
    bob_root.mkdir()
    save_registry({"SENDER": {"root": str(agent_root)}, "bob": {"root": str(bob_root)}})
    monkeypatch.chdir(agent_root)

    res = _run_a8s(agent_root, "--check", "bob")
    assert res.returncode == 0, res.stderr
    assert "recipient 'bob': ok" in res.stdout
    assert list((agent_root / ".outbox").glob("*.json")) == []


def test_tell_check_unknown_recipient_fails(fake_home, tmp_path, monkeypatch):
    from registry import save_registry

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    (agent_root / ".outbox").mkdir()
    save_registry({"SENDER": {"root": str(agent_root)}})
    monkeypatch.chdir(agent_root)

    res = _run_a8s(agent_root, "--check", "ghost")
    assert res.returncode == 1
    assert "no agent or alias named 'ghost'" in res.stderr
    assert list((agent_root / ".outbox").glob("*.json")) == []


def test_tell_check_fails_without_outbox(tmp_path):
    res = _run(tmp_path, "--check")
    assert res.returncode == 1
    assert "TELL_OUTBOX_DIR is not set" in res.stderr


def test_tell_check_reports_outbox_dir(tmp_path):
    outbox = tmp_path / "mailbox" / ".outbox"
    outbox.mkdir(parents=True)
    res = _run(
        tmp_path,
        "--check",
        env={"TELL_OUTBOX_DIR": str(outbox)},
    )
    assert res.returncode == 0, res.stderr
    assert f"outbox: {outbox.resolve()}" in res.stdout


def test_tell_check_creates_outbox_when_outbox_dir_set(tmp_path):
    outbox = tmp_path / "mailbox" / ".outbox"
    assert not outbox.exists()
    res = _run(
        tmp_path,
        "--check",
        env={"TELL_OUTBOX_DIR": str(outbox)},
    )
    assert res.returncode == 0, res.stderr
    assert outbox.is_dir()
    assert list(outbox.glob("*.json")) == []


def test_tell_check_rejects_message_body(tmp_path):
    (tmp_path / ".outbox").mkdir()
    res = _run(tmp_path, "--check", "bob", "hello")
    assert res.returncode == 2
    assert "does not accept a message" in res.stderr


def test_tell_help_omits_check(tmp_path):
    res = _run(tmp_path, "--help")
    assert res.returncode == 0
    assert "--check" not in res.stderr


def _strip_tell_outbox_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != TELL_OUTBOX_DIR_ENV}
    if extra:
        env.update(extra)
    return env


def _run_raw(
    cwd: Path,
    *args: str,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    kw: dict = {
        "cwd": str(cwd),
        "capture_output": True,
        "text": True,
        "env": _strip_tell_outbox_env(env),
    }
    if stdin is not None:
        kw["input"] = stdin
    return subprocess.run([str(TELL), *args], **kw)


class TestTellOutboxDirContract:
    """PR #136 test plan — replaces manual checklist items for tell outbox resolution."""

    def test_without_tell_outbox_dir_fails_clearly(self, tmp_path):
        res = _run_raw(tmp_path, "bob", "hi")
        assert res.returncode == 1
        assert "cannot send from this directory" in res.stderr
        assert "TELL_OUTBOX_DIR is not set" in res.stderr

    def test_wake_injected_env_sufficient_without_cwd_outbox(self, tmp_path):
        """Simulates a8s wake: only TELL_OUTBOX_DIR set, CWD has no .outbox."""
        outbox = tmp_path / "external-outbox"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        res = _run_raw(
            workspace,
            "bob",
            "from wake env",
            env={TELL_OUTBOX_DIR_ENV: str(outbox)},
        )
        assert res.returncode == 0, res.stderr
        assert outbox.is_dir()
        _name, msg = _read_outbox(outbox)
        assert msg["content"] == "from wake env"


class TestTellRegistryOutboxDiscovery:
    """When TELL_OUTBOX_DIR is unset, a unique configured filedrop may be inferred from CWD."""

    def test_uses_unique_seat_from_cwd(self, fake_home, tmp_path):
        from registry import save_registry

        seat = tmp_path / "neil-macbook"
        bob = tmp_path / "bob"
        seat.mkdir()
        bob.mkdir()
        save_registry({"neil-macbook": {"root": str(seat)}, "bob": {"root": str(bob)}})
        res = _run_raw(seat, "bob", "from seat root")
        assert res.returncode == 0, res.stderr
        _name, msg = _read_outbox(seat / ".outbox")
        assert msg["content"] == "from seat root"
        assert msg.get("from") == "neil-macbook"

    def test_uses_seat_from_subdir(self, fake_home, tmp_path):
        from registry import save_registry

        seat = tmp_path / "neil-macbook"
        bob = tmp_path / "bob"
        nested = seat / "notes"
        nested.mkdir(parents=True)
        bob.mkdir()
        save_registry({"neil-macbook": {"root": str(seat)}, "bob": {"root": str(bob)}})
        res = _run_raw(nested, "bob", "from nested")
        assert res.returncode == 0, res.stderr
        _name, msg = _read_outbox(seat / ".outbox")
        assert msg["content"] == "from nested"

    def test_ambiguous_parent_refuses(self, fake_home, tmp_path):
        from registry import save_registry

        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        save_registry({"a": {"root": str(a)}, "b": {"root": str(b)}})
        res = _run_raw(tmp_path, "bob", "nope")
        assert res.returncode == 1
        assert "multiple filedrops match" in res.stderr
        assert "TELL_OUTBOX_DIR" in res.stderr

    def test_env_wins_over_cwd_match(self, fake_home, tmp_path):
        from registry import save_registry

        seat = tmp_path / "seat"
        bob = tmp_path / "bob"
        other = tmp_path / "other-outbox"
        seat.mkdir()
        bob.mkdir()
        other.mkdir()
        save_registry({"seat": {"root": str(seat)}, "bob": {"root": str(bob)}})
        res = _run_raw(
            seat,
            "bob",
            "locked",
            env={TELL_OUTBOX_DIR_ENV: str(other)},
        )
        assert res.returncode == 0, res.stderr
        assert list((seat / ".outbox").glob("*.json")) == []
        _name, msg = _read_outbox(other)
        assert msg["content"] == "locked"
