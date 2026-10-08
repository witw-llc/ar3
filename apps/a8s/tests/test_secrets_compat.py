"""A stored secret survives every update (owner ruling, Decisions 2026-10-08)."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "secrets"
MANIFEST = "MANIFEST"
EXCEPTION = (
    "A stored secret survives every update. Changing a frozen fixture or the secret "
    "file's shape takes the owner's explicit exception, recorded in the Decisions "
    "ledger (Decisions, 2026-10-08) before the change is built."
)


FROZEN = {
    "0.1.102": {
        "network.json": "19bfb1fe428142fa92690f73ef7ab073998768ca2fe133488c8ec9cc4c324e95",
        "secrets.json": "7b6d6e5b5206a8a9337cbe84b0cc03b80c8173cd3f6aa412e3f5f1ede701c32c",
    },
}


def _version_key(path: Path) -> tuple[int, ...]:
    return tuple(int(part) for part in path.name.split("."))


def _shapes() -> list[Path]:
    return sorted((p for p in FIXTURES.iterdir() if p.is_dir()), key=_version_key)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _entry_keys(cfg: dict) -> dict:
    return {
        section: {name: sorted(entry) for name, entry in cfg[section].items()}
        for section in ("remotes", "services")
    }


@pytest.fixture
def state_root(tmp_path, monkeypatch):
    monkeypatch.setenv("A8S_HOME", str(tmp_path / "a8s-home"))
    return tmp_path / "a8s-home"


def _install(shape: Path, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for src in shape.iterdir():
        if src.name != MANIFEST:
            shutil.copyfile(src, root / src.name)


def test_there_is_a_frozen_fixture():
    assert _shapes(), "no frozen secret fixtures under fixtures/secrets/"


@pytest.mark.parametrize("shape", _shapes(), ids=lambda p: p.name)
def test_the_current_reader_returns_the_password_of_every_shipped_shape(
    shape, state_root, capsys
):
    import network

    _install(shape, state_root)
    cfg = network.load_network_config()
    assert cfg["remotes"], f"fixture {shape.name} holds no remote"
    for name, spec in cfg["remotes"].items():
        merged = network.merge_remote_secrets(name, spec)
        assert merged.get("pass") == "fixture-password", (
            f"fixture {shape.name}: remote {name!r} lost its stored password. {EXCEPTION}"
        )
    err = capsys.readouterr().err
    assert err == ""
    assert "does not match" not in err


@pytest.mark.parametrize("shape", _shapes(), ids=lambda p: p.name)
def test_a_frozen_fixture_is_unchanged(shape):
    listed = {}
    for line in (shape / MANIFEST).read_text().splitlines():
        digest, _, filename = line.partition("  ")
        listed[filename] = digest
    present = {p.name for p in shape.iterdir() if p.name != MANIFEST}
    assert present == set(listed), (
        f"frozen fixture {shape.name} changed: its files differ from its MANIFEST. "
        + EXCEPTION
    )
    for filename, digest in listed.items():
        assert _digest(shape / filename) == digest, (
            f"frozen fixture {shape.name}/{filename} changed. " + EXCEPTION
        )


@pytest.mark.parametrize("shape", _shapes(), ids=lambda p: p.name)
def test_a_frozen_fixture_matches_the_digests_pinned_here(shape):
    assert shape.name in FROZEN, (
        f"fixture {shape.name} has no digests pinned in this module. " + EXCEPTION
    )
    for filename, digest in FROZEN[shape.name].items():
        assert _digest(shape / filename) == digest, (
            f"frozen fixture {shape.name}/{filename} differs from the digest pinned "
            f"in this module; regenerating a fixture and its MANIFEST is not enough. "
            + EXCEPTION
        )
    assert set(FROZEN[shape.name]) == {
        p.name for p in shape.iterdir() if p.name != MANIFEST
    }


def test_the_writer_still_writes_the_newest_fixture_shape(state_root):
    import network
    from commands import cmd_remote

    assert cmd_remote([
        "hub", "mqtts://broker.example.test:8883", "topic-test",
        "--user", "tester", "--pass", "fixture-password",
    ]) == 0
    newest = _shapes()[-1]
    written = {
        name: json.loads((state_root / name).read_text())
        for name in ("network.json", "secrets.json")
    }
    frozen = {
        name: json.loads((newest / name).read_text())
        for name in ("network.json", "secrets.json")
    }
    for name in written:
        assert _entry_keys(written[name]) == _entry_keys(frozen[name]), (
            f"the writer's {name} no longer has the shape of fixture {newest.name}. "
            f"Add a new fixture directory for the new shape, with a reader for the "
            f"old one. " + EXCEPTION
        )
    assert network.merge_remote_secrets("hub", written["network.json"]["remotes"]["hub"])[
        "pass"
    ] == "fixture-password"
