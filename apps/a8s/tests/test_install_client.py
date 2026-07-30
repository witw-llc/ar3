"""Tests for `a8s install-client`."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from commands import cmd_install_client


def test_install_client_help(capsys):
    rc = cmd_install_client(["--help"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "install-client" in out
    assert "/usr/local/lib/a8s" in out


def test_install_client_requires_root_for_usr_local(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    rc = cmd_install_client([])
    assert rc == 1


def test_install_client_positional_dest(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    lib_dir = tmp_path / "opt" / "a8s"
    bin_dir = tmp_path / "bin"
    assert cmd_install_client([str(lib_dir), "--bin-dir", str(bin_dir)]) == 0
    assert (lib_dir / "a8s.py").is_file()
    assert (bin_dir / "tell").is_file()


def test_install_client_dest_conflicts_with_lib_dir(capsys):
    rc = cmd_install_client(["/tmp/a", "--lib-dir", "/tmp/b"])
    assert rc != 0


def test_install_client_copies_a8s_and_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    bin_dir = tmp_path / "bin"
    lib_dir = tmp_path / "lib" / "a8s"

    assert cmd_install_client(["--bin-dir", str(bin_dir), "--lib-dir", str(lib_dir)]) == 0
    tell_bin = bin_dir / "tell"
    assert tell_bin.is_file()
    assert not tell_bin.is_symlink()
    assert tell_bin.stat().st_mode & 0o777 == 0o755
    assert (lib_dir / "a8s.py").is_file()
    assert (lib_dir / "tell.py").is_file()
    assert not (lib_dir / "tests").exists()
    assert (lib_dir / "tell.py").stat().st_mode & 0o777 == 0o644

    (lib_dir / "tell.py").write_text("# stale\n", encoding="utf-8")
    assert cmd_install_client(["--bin-dir", str(bin_dir), "--lib-dir", str(lib_dir)]) == 0
    assert "stale" not in (lib_dir / "tell.py").read_text(encoding="utf-8")


def test_install_client_replaces_symlink_tell(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    bin_dir = tmp_path / "bin"
    lib_dir = tmp_path / "lib" / "a8s"
    other = tmp_path / "old-tell.sh"
    other.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
    other.chmod(0o755)
    bin_dir.mkdir()
    tell_bin = bin_dir / "tell"
    tell_bin.symlink_to(other)

    assert cmd_install_client(["--bin-dir", str(bin_dir), "--lib-dir", str(lib_dir)]) == 0
    assert tell_bin.is_file()
    assert not tell_bin.is_symlink()
    assert "a8s.py" in tell_bin.read_text(encoding="utf-8")
    proc = subprocess.run([str(tell_bin), "--help"], capture_output=True, text=True, check=False)
    assert proc.returncode == 0
    assert "old" not in proc.stdout


def test_installed_tell_writes_outbox(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    bin_dir = tmp_path / "bin"
    lib_dir = tmp_path / "lib" / "a8s"
    assert cmd_install_client(["--bin-dir", str(bin_dir), "--lib-dir", str(lib_dir)]) == 0

    agent = tmp_path / "agent"
    outbox = agent / ".outbox"
    outbox.mkdir(parents=True)
    proc = subprocess.run(
        [str(bin_dir / "tell"), "GEMINI", "hello from client"],
        cwd=str(agent),
        env={**os.environ, "TELL_OUTBOX_DIR": str(outbox)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    files = list(outbox.glob("*.json"))
    assert len(files) == 1
    msg = json.loads(files[0].read_text(encoding="utf-8"))
    assert msg["to"] == "GEMINI"
    assert msg["content"] == "hello from client"
    assert "from" not in msg
