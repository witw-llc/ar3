"""LLM-dependent distill tests — require a running ollama instance.

Uses a stateless ollama HTTP wrapper as llm_command (stdin→stdout).
"""
import json
import urllib.request

import pytest

import distill
import engine
from conftest import write_path_executable

pytestmark = pytest.mark.llm

OLLAMA_URL = "http://localhost:11434"


def _ollama_available():
    try:
        urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=2)
        return True
    except Exception:
        return False


@pytest.fixture(autouse=True)
def _skip_if_no_ollama():
    if not _ollama_available():
        pytest.skip("ollama not running")


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Isolated K7E store with a stateless ollama stdin→stdout llm_command,
    on the suite's bottom-line model; nothing smaller is tested."""
    monkeypatch.setenv("K7E_HOME", str(tmp_path))
    monkeypatch.setenv("OLLAMA_URL", OLLAMA_URL)

    engine.reset(tmp_path)
    engine.init()

    wrapper = write_path_executable(tmp_path, "ollama-stdin", (
        "import json, os, sys, urllib.request\n"
        'url = os.environ.get("OLLAMA_URL", "http://localhost:11434")\n'
        'model = os.environ.get("K7E_TEST_LLM_MODEL", "qwen3.6:latest")\n'
        "prompt = sys.stdin.read()\n"
        'data = json.dumps({"model": model, "prompt": prompt, "stream": False, "think": False}).encode()\n'
        'req = urllib.request.Request(f"{url}/api/generate", data=data, headers={"Content-Type": "application/json"})\n'
        "with urllib.request.urlopen(req, timeout=180) as resp:\n"
        '    print(json.loads(resp.read()).get("response", "").strip())\n'
    ))
    monkeypatch.setenv("K7E_LLM_COMMAND", str(wrapper))

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"llm_command": str(wrapper)}))

    return tmp_path


class TestLLMDistill:
    def test_extracts_knowledge_from_plain_text(self, store):
        text = (
            "Our staging cluster's etcd lives on the storage node, not the control "
            "node. When that node rebooted on Tuesday, the cluster went read-only: "
            "running workloads kept going but nothing new scheduled until etcd "
            "came back. Restarts of the storage node now wait for a weekend window."
        )
        path = store / "input.txt"
        path.write_text(text)
        results = distill.distill([str(path)])
        stored = [r for r in results if r["action"] == "stored"]
        assert len(stored) >= 1, f"LLM should extract at least 1 fact, got: {results}"

    def test_extracts_from_conversation(self, store):
        text = (
            "I discovered that the ingest worker crashed with RuntimeError because "
            "its retry hook called asyncio.run() inside the already-running loop. "
            "We fixed it by scheduling the retry with asyncio.ensure_future(), and "
            "the worker's retry hook must never call asyncio.run() again."
        )
        path = store / "convo.txt"
        path.write_text(text)
        results = distill.distill([str(path)])
        stored = [r for r in results if r["action"] == "stored"]
        assert len(stored) >= 1

    def test_dedup_on_second_run(self, store):
        text = (
            "We found that the widget service's deploy key cannot push workflow "
            "files, so since September its deploys push with the release-bot "
            "token instead, and the old key is read-only."
        )
        path = store / "git.txt"
        path.write_text(text)
        results1 = distill.distill([str(path)])
        stored1 = [r for r in results1 if r["action"] == "stored"]
        assert len(stored1) >= 1

        results2 = distill.distill([str(path)])
        new_stored = [r for r in results2 if r["action"] == "stored"]
        assert len(new_stored) <= len(stored1) + 1

    def test_returns_empty_for_noise(self, store):
        text = "Hey, sounds good! Let's sync tomorrow morning."
        path = store / "noise.txt"
        path.write_text(text)
        results = distill.distill([str(path)])
        assert len(results) == 0, f"Noise should not produce entries, got: {results}"
