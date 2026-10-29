"""Broker fixture failures retain the process's diagnostic and reap it."""
import subprocess
import sys
import time

import pytest

from mqtt_cluster import _broker_process, free_port


def _replace_broker(monkeypatch, script):
    real_popen = subprocess.Popen
    processes = []

    def launch(argv, **kwargs):
        assert argv[0] == "mosquitto"
        process = real_popen([sys.executable, "-c", script], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", launch)
    return processes


def test_early_exit_reports_stderr_and_exit_code(monkeypatch, tmp_path):
    processes = _replace_broker(
        monkeypatch, "import sys; print('cannot read broker config', file=sys.stderr); sys.exit(7)",
    )
    started = time.monotonic()
    with pytest.raises(pytest.fail.Exception, match="code 7.*cannot read broker config"):
        with _broker_process(tmp_path / "broker.conf", free_port(), timeout=5):
            pytest.fail("a dead broker must not reach the test")
    assert time.monotonic() - started < 4
    assert processes[0].returncode == 7


def test_timeout_reports_stderr_and_reaps_broker(monkeypatch, tmp_path):
    processes = _replace_broker(
        monkeypatch,
        "import sys,time; print('listener not ready', file=sys.stderr, flush=True); time.sleep(30)",
    )
    with pytest.raises(pytest.fail.Exception, match="within 0.5s.*listener not ready"):
        with _broker_process(tmp_path / "broker.conf", free_port(), timeout=0.5):
            pytest.fail("an unready broker must not reach the test")
    assert processes[0].returncode is not None


def test_test_failure_still_reaps_ready_broker(monkeypatch, tmp_path):
    port = free_port()
    processes = _replace_broker(
        monkeypatch,
        f"import socket,time; s=socket.socket(); s.bind(('127.0.0.1',{port})); s.listen(); time.sleep(30)",
    )
    with pytest.raises(RuntimeError, match="test body failed"):
        with _broker_process(tmp_path / "broker.conf", port):
            raise RuntimeError("test body failed")
    assert processes[0].returncode is not None
