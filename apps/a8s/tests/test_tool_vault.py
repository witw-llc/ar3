"""The bundled `tool-vault` definition, driven through real routing.

`a8s add <name> <dir> tool-vault --ALLOW=...` yields a working node: the
definition invokes the a8s-sibling package at `$A8S_DIR/../tools/vault`, which
answers a plaintext `/public-key` through the same outbox path `tell` uses.
There is no ACL: every sender is served under its own name.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from commands import cmd_add
from core import Participant, outbox_dir
from daemon import attached_loop
from definitions import default_definition_path, resolve_definition_arg
from mailbox import _write_outbox, ensure_mailboxes
from registry import load_registry, save_registry

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("openssl") is None,
    reason="the vault shells out to POSIX openssl",
)


def _read_envelopes(directory: Path) -> list[dict]:
    return [json.loads(f.read_text(encoding="utf-8")) for f in sorted(directory.glob("*.json"))]


def _setup(tmp_path):
    a_root = tmp_path / "a"
    v_root = tmp_path / "v"
    a_root.mkdir()
    v_root.mkdir()
    save_registry({
        "a": {"root": str(a_root), "definition": str(default_definition_path("filedrop"))},
        "vault": {
            "root": str(v_root),
            "definition": str(default_definition_path("tool-vault")),
            "vars": {"VAULT_DIR": str(tmp_path / "state")},
        },
    })
    ensure_mailboxes(Participant("a", a_root))
    ensure_mailboxes(Participant("vault", v_root))
    return a_root, v_root


def _init_vault(tmp_path):
    import subprocess
    import sys

    tool = Path(__file__).resolve().parents[2] / "tools" / "vault"
    env = dict(os.environ, VAULT_DIR=str(tmp_path / "state"))
    subprocess.run([sys.executable, str(tool), "init"], check=True, env=env, capture_output=True)


class TestAddBuiltin:
    def test_bare_name_resolves_bundled(self, fake_home, tmp_path):
        root = tmp_path / "probe"
        root.mkdir()
        assert cmd_add(["probe", str(root), "tool-vault"]) == 0
        assert load_registry()["probe"]["definition"] == str(default_definition_path("tool-vault"))

    def test_definition_requires_allow_var(self):
        path = resolve_definition_arg("tool-vault")
        argv = json.loads(path.read_text())["invoke"]
        assert not any("ALLOW" in a for a in argv)
        assert "--dir=$VAULT_DIR?" in argv and "--openssl=$OPENSSL?" in argv


class TestWake:
    def test_public_key_reply_carries_the_key(self, fake_home, tmp_path):
        a_root, v_root = _setup(tmp_path)
        _init_vault(tmp_path)
        _write_outbox("a", a_root, "vault", "/public-key", [])

        assert attached_loop(["a", "vault"], 0.1, single_pass=True) == 0
        replies = _read_envelopes(outbox_dir(v_root))
        assert len(replies) == 1
        assert replies[0]["to"] == "a"
        assert "public key attached" in replies[0]["content"]
        assert [f["filename"] for f in replies[0]["files"]] == ["vault.pem"]
        pem = outbox_dir(v_root) / replies[0]["id"] / "vault.pem"
        assert pem.read_text().startswith("-----BEGIN PUBLIC KEY-----")

    def test_connect_without_a_key_is_refused(self, fake_home, tmp_path):
        """Through routing: a sender with no key and no voucher gets a refusal, not a session.
        (Voucher refusal and per-sender isolation are covered in apps/tools/tests/vault.)"""
        a_root, v_root = _setup(tmp_path)
        _init_vault(tmp_path)
        _write_outbox("a", a_root, "vault", "/connect", [])

        assert attached_loop(["a", "vault"], 0.1, single_pass=True) == 0
        replies = _read_envelopes(outbox_dir(v_root))
        assert len(replies) == 1
        assert "/connect needs exactly one attached public key" in replies[0]["content"]
