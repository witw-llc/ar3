"""Shared helpers for two-cluster MQTT integration tests."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

import pytest

_PKG_DIR = Path(__file__).resolve().parent.parent
_RUNNER = Path(__file__).resolve().parent / "cluster_runner.py"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def mqtt_broker(tmp_path):
    """Local mosquitto on a random port. Skips when `mosquitto` is absent."""
    if shutil.which("mosquitto") is None:
        pytest.skip("mosquitto binary not on PATH")
    port = free_port()
    conf = tmp_path / "mosquitto.conf"
    conf.write_text(
        f"listener {port} 127.0.0.1\nallow_anonymous true\npersistence false\n"
    )
    proc = subprocess.Popen(
        ["mosquitto", "-c", str(conf)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        proc.terminate()
        pytest.fail("mosquitto failed to start within 5s")
    yield port
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


def write_network_json(
    a8s_home: Path,
    port: int,
    topic: str,
    client_id: str,
    *,
    storage_url: str | None = None,
) -> None:
    a8s_home.mkdir(parents=True, exist_ok=True)
    cfg: dict = {
        "remotes": {
            "hub": {
                "transport": "mqtt",
                "broker": f"mqtt://127.0.0.1:{port}",
                "topic": topic,
                "client_id": client_id,
            }
        }
    }
    if storage_url is not None:
        cfg["services"] = {
            "fake": {"service": "tempfile_org", "url": storage_url},
        }
    (a8s_home / "network.json").write_text(json.dumps(cfg))


def setup_cluster(
    a8s_home: Path,
    *,
    port: int,
    topic: str,
    client_id: str,
    agents: dict[str, Path],
) -> None:
    """Write network.json + registry for one isolated A8S_HOME."""
    write_network_json(a8s_home, port, topic, client_id)
    from registry import save_registry

    with using_a8s_home(a8s_home):
        reg: dict[str, dict] = {}
        for name, root in agents.items():
            def_path = root / "a8s-proxy.json"
            def_path.write_text(json.dumps({"proxy": "file", "idle": {"timeout": 30}}))
            reg[name] = {
                "root": str(root.resolve()),
                "definition": str(def_path.resolve()),
            }
        save_registry(reg)


@contextmanager
def using_a8s_home(home: Path) -> Iterator[None]:
    prev = os.environ.get("A8S_HOME")
    os.environ["A8S_HOME"] = str(home)
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("A8S_HOME", None)
        else:
            os.environ["A8S_HOME"] = prev


def ensure_agent_mailboxes(home: Path, agents: dict[str, Path]) -> None:
    from core import Participant
    from mailbox import ensure_mailboxes

    with using_a8s_home(home):
        for name, root in agents.items():
            ensure_mailboxes(Participant(name, root))


def write_outbox(home: Path, sender: str, sender_root: Path, to: str, content: str) -> None:
    from mailbox import _write_outbox

    with using_a8s_home(home):
        _write_outbox(sender, sender_root, to, content, [])


def wait_inbox(
    home: Path,
    agent: str,
    *,
    timeout: float = 8.0,
    predicate: Callable[[dict], bool] | None = None,
) -> dict:
    from core import inbox_dir

    deadline = time.time() + timeout
    with using_a8s_home(home):
        while time.time() < deadline:
            for path in sorted(inbox_dir(agent).glob("*.json")):
                body = json.loads(path.read_text())
                if predicate is None or predicate(body):
                    return body
            time.sleep(0.05)
    raise AssertionError(f"no matching inbox message for {agent!r} within {timeout}s")


def wait_convo(
    home: Path,
    *,
    timeout: float = 8.0,
    predicate: Callable[[list[dict]], bool],
) -> list[dict]:
    from convo import load_entries

    deadline = time.time() + timeout
    with using_a8s_home(home):
        while time.time() < deadline:
            rows = load_entries()
            if predicate(rows):
                return rows
            time.sleep(0.05)
    raise AssertionError(f"conversations.sqlite3 condition not met within {timeout}s")


def wait_agent_log(
    a8s_home: Path,
    agent: str,
    substring: str,
    *,
    timeout: float = 8.0,
) -> str:
    path = a8s_home / "agents" / agent / "log.txt"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.is_file():
            text = path.read_text()
            if substring in text:
                return text
        time.sleep(0.05)
    raise AssertionError(f"log line {substring!r} not found for {agent!r} within {timeout}s")


def start_attached_loop(a8s_home: Path, agent: str, *, drain_seconds: float = 0.0) -> subprocess.Popen:
    env = os.environ.copy()
    env["A8S_HOME"] = str(a8s_home)
    env["PYTHONPATH"] = str(_PKG_DIR)
    return subprocess.Popen(
        [
            sys.executable,
            str(_RUNNER),
            str(a8s_home),
            agent,
            "0.05",
            str(drain_seconds),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop_attached_loop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)
