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

# The extensionless polyglot is bash-and-PowerShell; Windows cannot exec it
# from a path, which is exactly why `tell.cmd` ships beside it. Running the
# file a Windows user would actually get is more faithful, not less.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
TELL = _REPO_ROOT / ("tell.cmd" if os.name == "nt" else "tell")
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
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores file permission bits",
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


def test_tell_registered_outbox_validates_recipient(fake_home, tmp_path, monkeypatch):
    """The plain a8s send path: writing into a registered outbox, tell feeds the
    a8s router, so the registry is the authority on who may be addressed."""
    from registry import save_registry

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    (agent_root / ".outbox").mkdir()
    save_registry({"SENDER": {"root": str(agent_root)}})
    monkeypatch.chdir(agent_root)

    res = _run_a8s(agent_root, "ghost", "hi")
    assert res.returncode == 1
    assert "no agent or alias named 'ghost'" in res.stderr
    assert list((agent_root / ".outbox").glob("*.json")) == []


def test_tell_case_variant_outbox_spelling_still_validates(
    fake_home, tmp_path, monkeypatch
):
    """On a case-insensitive filesystem (APFS default, NTFS), a differently
    cased spelling of an agent's own registered outbox is the same physical
    directory the a8s router ingests from — `_outbox_is_registered` must
    compare physical identity, not resolved-Path string equality, or a
    re-cased TELL_OUTBOX_DIR dodges registry validation entirely."""
    from registry import save_registry

    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    real_outbox = agent_root / ".outbox"
    real_outbox.mkdir()

    probe = real_outbox / "case-probe.tmp"
    probe.write_text("x", encoding="utf-8")
    is_case_insensitive = (real_outbox / "CASE-PROBE.TMP").is_file()
    probe.unlink()
    if not is_case_insensitive:
        pytest.skip("filesystem is case-sensitive")

    save_registry({"SENDER": {"root": str(agent_root)}})
    monkeypatch.chdir(agent_root)

    variant = agent_root / ".OUTBOX"
    res = _run_a8s(agent_root, "ghost", "hi", env={"TELL_OUTBOX_DIR": str(variant)})
    assert res.returncode == 1, (
        "a case-variant spelling of the agent's own registered outbox "
        "skipped registry validation"
    )
    assert "no agent or alias named 'ghost'" in res.stderr
    assert list(real_outbox.glob("*.json")) == []


def test_tell_staging_outbox_skips_registry_validation(
    fake_home, tmp_path, monkeypatch
):
    """A caged roster member: r4t points TELL_OUTBOX_DIR at a per-turn staging
    dir, and the member's workplace sits inside the registered node's root. The
    recipient is a roster member, not an a8s agent, so tell must stage it and
    leave resolution to the consumer that drains the staging dir."""
    from registry import save_registry

    agent_root = tmp_path / "node"
    workplace = agent_root / "workplace"
    workplace.mkdir(parents=True)
    (agent_root / ".outbox").mkdir()
    save_registry({"NODE": {"root": str(agent_root)}})
    staging = tmp_path / "r4t-state" / "agents" / "roy" / "staging"
    staging.mkdir(parents=True)
    monkeypatch.chdir(workplace)

    res = _run_a8s(workplace, "moss", "over to you", env={"TELL_OUTBOX_DIR": str(staging)})
    assert res.returncode == 0, res.stderr
    _name, msg = _read_outbox(staging)
    assert msg["to"] == "moss"
    # `from` stamping is unchanged — the router force-overwrites it anyway.
    assert msg["from"] == "NODE"
    assert list((agent_root / ".outbox").glob("*.json")) == []


def test_tell_warns_when_a_leaked_outbox_dir_points_at_another_seat(
    fake_home, tmp_path, monkeypatch
):
    """The stale-variable hijack: a live seat's TELL_OUTBOX_DIR is inherited by
    a shell sitting inside a DIFFERENT registered seat's root, so mail leaves
    under the wrong name with every check passing. The pair is the tell — two
    registered identities competing for the same send. Warn; never refuse,
    since a deliberate operator may mean it."""
    from registry import save_registry

    seat = tmp_path / "seat"
    seat_outbox = seat / ".outbox"
    seat_outbox.mkdir(parents=True)
    other_seat = tmp_path / "other-seat"
    other_seat.mkdir()
    save_registry({
        "MOSS": {"root": str(seat)},
        "FERN": {"root": str(other_seat)},
    })
    monkeypatch.chdir(other_seat)

    res = _run_a8s(other_seat, "MOSS", "hi", env={"TELL_OUTBOX_DIR": str(seat_outbox)})
    assert res.returncode == 0, res.stderr
    assert "warning" in res.stderr
    assert "MOSS's outbox" in res.stderr
    assert "FERN's" in res.stderr
    assert "unset TELL_OUTBOX_DIR" in res.stderr
    # A warning, not a refusal — the message still goes.
    _name, msg = _read_outbox(seat_outbox)
    assert msg["to"] == "MOSS"


def test_tell_from_an_unregistered_directory_is_not_a_hijack(
    fake_home, tmp_path, monkeypatch
):
    """An explicit `TELL_OUTBOX_DIR` naming a registered agent's outbox, sent
    from a directory that is no registered agent's root, has only one
    identity in play — the env var. That is configuration, not a leak, so it
    stays silent even though the outbox itself is a real agent's."""
    from registry import save_registry

    seat = tmp_path / "seat"
    seat_outbox = seat / ".outbox"
    seat_outbox.mkdir(parents=True)
    save_registry({"MOSS": {"root": str(seat)}})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    res = _run_a8s(elsewhere, "MOSS", "hi", env={"TELL_OUTBOX_DIR": str(seat_outbox)})
    assert res.returncode == 0, res.stderr
    assert "warning" not in res.stderr
    _name, msg = _read_outbox(seat_outbox)
    assert msg["to"] == "MOSS"


def test_tell_from_a_seats_own_root_is_not_a_hijack(fake_home, tmp_path, monkeypatch):
    """The ordinary case a8s itself creates on every wake: the injected
    variable names the seat whose root the agent is working in. Warning here
    would fire on every message the suite sends."""
    from registry import save_registry

    seat = tmp_path / "seat"
    workplace = seat / "workplace"
    workplace.mkdir(parents=True)
    seat_outbox = seat / ".outbox"
    seat_outbox.mkdir()
    save_registry({"MOSS": {"root": str(seat)}})
    monkeypatch.chdir(workplace)

    res = _run_a8s(workplace, "MOSS", "hi", env={"TELL_OUTBOX_DIR": str(seat_outbox)})
    assert res.returncode == 0, res.stderr
    assert "warning" not in res.stderr


def test_tell_shared_root_sibling_is_not_a_hijack(fake_home, tmp_path, monkeypatch):
    """Two nodes rooted at one repo: sending from that root with the variable
    naming one node's own outbox is the owner working in its own root. The
    sibling sharing the root is not a competing identity, so no warning."""
    from registry import save_registry

    repo = tmp_path / "repo"
    moss_outbox = repo / ".outbox"
    moss_outbox.mkdir(parents=True)
    (repo / ".outbox-fern").mkdir()
    save_registry({
        "MOSS": {"root": str(repo)},
        "FERN": {"root": str(repo), "outbox": str(repo / ".outbox-fern")},
    })
    monkeypatch.chdir(repo)

    res = _run_a8s(repo, "FERN", "hi", env={"TELL_OUTBOX_DIR": str(moss_outbox)})
    assert res.returncode == 0, res.stderr
    assert "warning" not in res.stderr
    _name, msg = _read_outbox(moss_outbox)
    assert msg["to"] == "FERN"


def test_tell_staging_outbox_is_not_a_hijack(fake_home, tmp_path, monkeypatch):
    """r4t's per-turn staging dir is deliberately outside any registered seat
    and deliberately not the CWD's own. It is not registered, so it cannot be
    the hijack shape."""
    from registry import save_registry

    seat = tmp_path / "seat"
    (seat / ".outbox").mkdir(parents=True)
    save_registry({"MOSS": {"root": str(seat)}})
    staging = tmp_path / "r4t-state" / "staging"
    staging.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    res = _run_a8s(elsewhere, "moss", "hi", env={"TELL_OUTBOX_DIR": str(staging)})
    assert res.returncode == 0, res.stderr
    assert "warning" not in res.stderr


def test_tell_check_reports_the_hijack_shape(fake_home, tmp_path, monkeypatch):
    from registry import save_registry

    seat = tmp_path / "seat"
    seat_outbox = seat / ".outbox"
    seat_outbox.mkdir(parents=True)
    other_seat = tmp_path / "other-seat"
    other_seat.mkdir()
    save_registry({
        "MOSS": {"root": str(seat)},
        "FERN": {"root": str(other_seat)},
    })
    monkeypatch.chdir(other_seat)

    res = _run_a8s(other_seat, "--check", env={"TELL_OUTBOX_DIR": str(seat_outbox)})
    assert res.returncode == 0, res.stderr
    assert "warning:" in res.stdout
    assert "MOSS's outbox" in res.stdout
    assert "FERN's" in res.stdout


def test_tell_check_defers_recipient_on_staging_outbox(
    fake_home, tmp_path, monkeypatch
):
    from registry import save_registry

    agent_root = tmp_path / "node"
    agent_root.mkdir()
    (agent_root / ".outbox").mkdir()
    save_registry({"NODE": {"root": str(agent_root)}})
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.chdir(agent_root)

    res = _run_a8s(agent_root, "--check", "moss", env={"TELL_OUTBOX_DIR": str(staging)})
    assert res.returncode == 0, res.stderr
    assert (
        "recipient 'moss': not checked "
        "(staging outbox — its consumer resolves recipients)"
    ) in res.stdout
    assert list(staging.glob("*.json")) == []


def test_tell_check_reports_unreadable_registry(fake_home, tmp_path, monkeypatch, capsys):
    """A staging outbox prints the same 'not checked' whether the registry is
    reachable-but-empty or unreadable — distinguish the two so an operator
    with a broken registry gets a different signal than one whose outbox is
    legitimately unregistered.

    In-process rather than via the subprocess harness: making the whole
    `.a8s` directory unreadable (the only way to make `registry_path().is_file()`
    raise) also breaks the CLI's unrelated settings load before `tell --check`
    ever runs, so `participants_from_registry` is patched to raise directly.
    """
    import tell as tell_mod

    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setenv(TELL_OUTBOX_DIR_ENV, str(staging))
    monkeypatch.setattr(
        "registry.participants_from_registry",
        lambda: (_ for _ in ()).throw(OSError("permission denied")),
    )

    rc = tell_mod.run_check("moss")
    out = capsys.readouterr().out
    assert rc == 0
    assert "recipient 'moss': not checked (no readable registry)" in out
    assert list(staging.glob("*.json")) == []


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


def test_tell_stdin_utf8_survives_a_locale_codepage(tmp_path):
    """The Windows-seat mojibake: without the stdin re-pin, a UTF-8 body piped
    into a cp1252 process decodes wrong and is STORED wrong — permanent, and
    nothing tells the sender. PYTHONIOENCODING=cp1252 forces the same codec on
    any platform; the envelope must still carry the body byte for byte."""
    (tmp_path / ".outbox").mkdir()
    body = "→ ⇒ 中 é —"
    res = subprocess.run(
        [*A8S_TELL, "gerry", "-"],
        cwd=str(tmp_path),
        capture_output=True,
        input=body.encode("utf-8"),
        env={**_merge_tell_env(tmp_path, None), "PYTHONIOENCODING": "cp1252"},
    )
    assert res.returncode == 0, res.stderr.decode("utf-8", "replace")
    _name, msg = _read_outbox(tmp_path / ".outbox")
    assert msg["content"] == body


def test_tell_registered_echo_survives_a_raising_stdout(fake_home, tmp_path):
    """The send must exit 0 once the envelope is committed, no matter what the
    console does with the echo line. cp1252:surrogateescape is the raising
    stdout state the strict-only floor missed in the field: the echo's arrow
    crashed a SUCCESSFUL send, the exit code lied, and a retrying caller
    would double-send."""
    from registry import save_registry

    seat = tmp_path / "seat"
    outbox = seat / ".outbox"
    outbox.mkdir(parents=True)
    save_registry({"MOSS": {"root": str(seat)}, "CLARK": {"root": str(tmp_path / "clark")}})
    res = subprocess.run(
        [*A8S_TELL, "clark", "over → there"],
        cwd=str(seat),
        capture_output=True,
        env={
            **_merge_tell_env(seat, None, outbox=outbox),
            "PYTHONIOENCODING": "cp1252:surrogateescape",
        },
    )
    assert res.returncode == 0, res.stderr.decode("utf-8", "replace")
    assert b"\\u2192" in res.stdout
    _name, msg = _read_outbox(outbox)
    assert msg["content"] == "over → there"


def test_tell_without_a_message_fails_instead_of_waiting(tmp_path):
    """The five-hour hang, reproduced. Reading stdin whenever it was not a
    terminal meant an agent that forgot the body got a process that waited
    rather than an error — `isatty()` is false for a harness pipe that is held
    open and never closed, which is what an agent's stdin is. The sender saw
    no output and read that as delivery.

    The read end of a real pipe is passed as stdin and the write end is kept
    open here, so the child gets exactly that shape. `wait(timeout=...)` is
    the assertion: against the old code it raises.
    """
    (tmp_path / ".outbox").mkdir()
    read_fd, write_fd = os.pipe()
    try:
        proc = subprocess.Popen(
            [*A8S_TELL, "gerry"],
            cwd=str(tmp_path),
            stdin=read_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_merge_tell_env(tmp_path, None),
        )
        try:
            out, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise AssertionError(
                "tell waited on a stdin nobody was going to close"
            )
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert proc.returncode == 2, (proc.returncode, err)
    assert "no message" in err
    assert not list((tmp_path / ".outbox").glob("*.json")), "it sent something"



def _tell_with_stdin(tmp_path, producer_argv, wait_sec=None):
    """Run `tell` with a real pipe fed by a real producer, the way a shell does."""
    (tmp_path / ".outbox").mkdir(exist_ok=True)
    env = _merge_tell_env(tmp_path, None)
    if wait_sec is not None:
        env["TELL_STDIN_WAIT_SEC"] = str(wait_sec)
    producer = subprocess.Popen(producer_argv, stdout=subprocess.PIPE)
    try:
        res = subprocess.run(
            [*A8S_TELL, "gerry"], cwd=str(tmp_path), stdin=producer.stdout,
            capture_output=True, text=True, env=env, timeout=30,
        )
    finally:
        producer.stdout.close()
        producer.wait()
    return res


def test_a_piped_body_needs_no_dash(tmp_path):
    """The friction this restores. Refusing every pipe was the safe end of a
    choice and it cost the ordinary `echo hi | tell bob`."""
    res = _tell_with_stdin(tmp_path, [sys.executable, "-c", "print('piped body')"])
    assert res.returncode == 0, res.stderr
    _name, msg = _read_outbox(tmp_path / ".outbox")
    assert msg["content"] == "piped body"


def test_a_long_piped_body_arrives_whole(tmp_path):
    """The reason the deadline covers the FIRST CHARACTER and not the read. A
    clock over the whole body truncates a long message, which is the silent
    failure the deadline was added to avoid — so the fix would have become the
    defect, in a shape no short test can see."""
    res = _tell_with_stdin(
        tmp_path, [sys.executable, "-c", "print('L' * 200000)"]
    )
    assert res.returncode == 0, res.stderr
    _name, msg = _read_outbox(tmp_path / ".outbox")
    assert len(msg["content"].strip()) == 200000


def test_a_slow_producer_still_gets_its_body_sent(tmp_path):
    """A producer that takes a moment to say anything is still a producer. It
    has to beat the deadline on its first character, and nothing after.

    The deadline is raised for this case rather than the sleep shortened. Two
    seconds is a wall clock: on a loaded machine it expires before the sleeping
    producer speaks, the refusal is correct, and the test fails for a reason
    that has nothing to do with the contract it is testing. A Windows run found
    exactly that, arriving as a thirty-second timeout rather than an assertion.
    """
    res = _tell_with_stdin(
        tmp_path,
        [sys.executable, "-c",
         "import time,sys; time.sleep(1); sys.stdout.write('slow body'); sys.stdout.flush()"],
        wait_sec=30,
    )
    assert res.returncode == 0, res.stderr
    _name, msg = _read_outbox(tmp_path / ".outbox")
    assert msg["content"] == "slow body"


def test_the_pipe_wait_uses_no_api_that_windows_refuses(tmp_path):
    """`select` on Windows is WinSock-backed and takes sockets only, so a pipe
    raises there — `echo hi | tell.cmd bob` would have caught the error and
    exited 2 without sending, on the one platform this batch is about. A
    reviewer caught it from the documented contract; no POSIX run can.

    Asserted on the source because the failure is platform-specific and this
    suite has no Windows runner."""
    source = (Path(__file__).resolve().parent.parent / "tell.py").read_text(
        encoding="utf-8"
    )
    # Code, not prose — the comment above the fix names `select` on purpose.
    code = [
        line for line in source.splitlines()
        if not line.lstrip().startswith("#")
    ]
    offenders = [
        line.strip() for line in code
        if re.search(r"(^|[^\w.])select\s*\.|import\s+select\b", line)
    ]
    assert not offenders, (
        f"tell.py reaches for select, which cannot watch a pipe on "
        f"Windows: {offenders}"
    )

def test_an_undecodable_byte_past_the_buffer_does_not_truncate(tmp_path):
    """The shape a reviewer executed against this exact tree. The first
    character comes out of a decoded buffer, so an invalid byte far enough in
    raises on the SECOND read with a valid prefix already in hand: 9,000 bytes
    of body arrived as one character, exit 0, and the sender was told it went.

    Two things stop it. Stdin escapes an undecodable byte rather than raising
    on it, so the body survives whole and reversibly; and if a read fails
    anyway, the send does not happen. Asserted here on the body, because a
    complete message is the outcome the sender needs — not a clean error."""
    producer = [
        sys.executable, "-c",
        "import sys; sys.stdout.buffer.write(b'A' * 9000 + b'\\xff' + b'B'); "
        "sys.stdout.buffer.flush()",
    ]
    (tmp_path / ".outbox").mkdir(exist_ok=True)
    env = _merge_tell_env(tmp_path, None)
    env["PYTHONIOENCODING"] = "utf-8:strict"
    proc = subprocess.Popen(producer, stdout=subprocess.PIPE)
    try:
        res = subprocess.run(
            [*A8S_TELL, "gerry"], cwd=str(tmp_path), stdin=proc.stdout,
            capture_output=True, text=True, env=env, timeout=30,
        )
    finally:
        proc.stdout.close()
        proc.wait()
    assert res.returncode == 0, res.stderr
    _name, msg = _read_outbox(tmp_path / ".outbox")
    assert msg["content"] == "A" * 9000 + "\\xff" + "B", (
        f"a {len(msg['content'])}-character body arrived for a 9002-byte one"
    )


class TestThePipeDeadlineIsOverridable:
    """The knob has to be tested where it is read, not through a sleeping
    producer. A producer that beats the default deadline beats it whether or
    not the override is honoured, so the integration test cannot prove the
    override works — it passed unchanged against a build that ignored the
    environment entirely."""

    def test_it_reads_the_environment(self, monkeypatch):
        import tell as tell_mod

        monkeypatch.setenv(tell_mod.TELL_STDIN_WAIT_ENV, "30")
        assert tell_mod._stdin_wait_sec() == 30.0

    def test_the_default_stands_when_it_is_unset(self, monkeypatch):
        import tell as tell_mod

        monkeypatch.delenv(tell_mod.TELL_STDIN_WAIT_ENV, raising=False)
        assert tell_mod._stdin_wait_sec() == tell_mod.STDIN_WAIT_SEC

    @pytest.mark.parametrize("value", ["", "nonsense", "0", "-4"])
    def test_a_value_that_is_not_a_deadline_falls_back(self, monkeypatch, value):
        """A zero or a negative would turn the wait off and make every pipe
        look empty, which is the failure the deadline exists to avoid. An
        unreadable value is a typo, not an instruction."""
        import tell as tell_mod

        monkeypatch.setenv(tell_mod.TELL_STDIN_WAIT_ENV, value)
        assert tell_mod._stdin_wait_sec() == tell_mod.STDIN_WAIT_SEC


def test_an_undecodable_byte_through_the_dash_is_not_a_lone_surrogate(tmp_path):
    """The same floor, on the path that was already shipping. Under
    `PYTHONUTF8=1` the interpreter's stdin handler is surrogateescape, which
    does not raise — it produces a LONE SURROGATE, and `tell x -` wrote that
    into an envelope under exit 0. The content cannot be re-encoded to UTF-8 by
    anything downstream, so the envelope is poisoned where it sits.

    Measured on Windows by a field seat, on the `-` path, which no part of the
    pipe work touched. Both raising handlers are floored for that reason."""
    (tmp_path / ".outbox").mkdir(exist_ok=True)
    env = _merge_tell_env(tmp_path, None)
    env["PYTHONUTF8"] = "1"
    producer = subprocess.Popen(
        [sys.executable, "-c",
         "import sys; sys.stdout.buffer.write(b'A' * 9000 + b'\\xff' + b'B'); "
         "sys.stdout.buffer.flush()"],
        stdout=subprocess.PIPE,
    )
    try:
        res = subprocess.run(
            [*A8S_TELL, "gerry", "-"], cwd=str(tmp_path), stdin=producer.stdout,
            capture_output=True, text=True, env=env, timeout=30,
        )
    finally:
        producer.stdout.close()
        producer.wait()
    assert res.returncode == 0, res.stderr
    _name, msg = _read_outbox(tmp_path / ".outbox")
    assert msg["content"] == "A" * 9000 + "\\xff" + "B"
    # The point of the escape: what came out survives a round trip.
    msg["content"].encode("utf-8")


class TestAPipeThatFailsMidReadSendsNothing:
    """The prefix must never be committed. A reader thread that swallows its
    own exception and hands back what it already had is a silent truncation
    with an exit code of zero, which is the family this whole change is about.
    """

    class _FailsAfterTheFirstCharacter:
        def __init__(self, error):
            self.error = error
            self.reads = 0

        def isatty(self):
            return False

        def read(self, size=-1):
            self.reads += 1
            if self.reads == 1:
                return "A"
            raise self.error

    def test_the_exception_crosses_back_to_the_caller(self, monkeypatch):
        import tell as tell_mod

        monkeypatch.setattr(
            tell_mod.sys, "stdin",
            self._FailsAfterTheFirstCharacter(ValueError("bad byte")),
        )
        with pytest.raises(tell_mod.TellStdinError):
            tell_mod._read_pipe_before(5.0)

    def test_an_os_error_is_fatal_too(self, monkeypatch):
        import tell as tell_mod

        monkeypatch.setattr(
            tell_mod.sys, "stdin",
            self._FailsAfterTheFirstCharacter(OSError("pipe went away")),
        )
        with pytest.raises(tell_mod.TellStdinError):
            tell_mod._read_pipe_before(5.0)

    def test_tell_exits_nonzero_and_writes_no_envelope(self, tmp_path, monkeypatch):
        import tell as tell_mod

        outbox = tmp_path / ".outbox"
        outbox.mkdir()
        monkeypatch.setenv("TELL_OUTBOX_DIR", str(outbox))
        monkeypatch.setattr(
            tell_mod.sys, "stdin",
            self._FailsAfterTheFirstCharacter(ValueError("bad byte")),
        )
        rc = tell_mod.tell_main(["gerry"])
        assert rc != 0
        assert not list(outbox.glob("*.json")), "it sent a truncated body"


def test_a_body_still_arrives_through_the_dash(tmp_path):
    """The positive control beside it: closing the door on implicit stdin must
    not close the documented one, which every form in the docs already uses."""
    (tmp_path / ".outbox").mkdir()
    res = _run(tmp_path, "gerry", "-", stdin="the body\n")
    assert res.returncode == 0, res.stderr
    _name, msg = _read_outbox(tmp_path / ".outbox")
    assert msg["content"] == "the body"


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
