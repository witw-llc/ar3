"""The bundled `tool-transcribe` definition, driven through real routing."""
from __future__ import annotations

import json
import sys
import sysconfig
from pathlib import Path

import pytest

from commands import cmd_add
from core import Participant
from daemon import attached_loop
from definitions import default_definition_path, resolve_definition_arg
from mailbox import _write_outbox, ensure_mailboxes
from registry import load_registry, save_registry


def _read_envelopes(directory: Path) -> list[dict]:
    return [json.loads(f.read_text(encoding="utf-8")) for f in sorted(directory.glob("*.json"))]


def _install_fake_faster_whisper(xdg_data: Path) -> None:
    """Populate a tier-2 `transcribe-fast` group dir so child wakes run offline."""
    interp = f"{sys.implementation.cache_tag}-{sysconfig.get_platform()}"
    gdir = xdg_data / "ar3" / "deps" / interp / "transcribe-fast"
    dist = gdir / "faster_whisper-1.2.1.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: faster-whisper\nVersion: 1.2.1\n",
        encoding="utf-8",
    )
    av_dist = gdir / "av-18.1.0.dist-info"
    av_dist.mkdir(parents=True)
    (av_dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: av\nVersion: 18.1.0\n",
        encoding="utf-8",
    )
    (gdir / "faster_whisper.py").write_text(
        "from types import SimpleNamespace\n"
        "from pathlib import Path\n"
        "class WhisperModel:\n"
        "    def __init__(self, model_name, device='auto', compute_type='int8'):\n"
        "        self.model_name = model_name\n"
        "    def transcribe(self, audio, language=None, initial_prompt=None, condition_on_previous_text=False):\n"
        "        payload = Path(audio).read_text(encoding='utf-8').strip()\n"
        "        text = f'{payload} [hints={initial_prompt or \"none\"}]'\n"
        "        return [SimpleNamespace(start=0.0, end=1.0, text=text)], SimpleNamespace(language=language or 'en')\n",
        encoding="utf-8",
    )


@pytest.fixture
def transcribe_pair(fake_home, tmp_path, monkeypatch):
    """Filedrop `a` and `b` plus `tool-transcribe` node `tr` allowing `a`."""
    xdg_data = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
    _install_fake_faster_whisper(xdg_data)

    a_root = tmp_path / "a"
    b_root = tmp_path / "b"
    tr_root = tmp_path / "tr"
    a_root.mkdir()
    b_root.mkdir()
    tr_root.mkdir()
    save_registry({
        "a": {"root": str(a_root), "definition": str(default_definition_path("filedrop"))},
        "b": {"root": str(b_root), "definition": str(default_definition_path("filedrop"))},
        "tr": {
            "root": str(tr_root),
            "definition": str(default_definition_path("tool-transcribe")),
            "vars": {"ALLOW": "a", "ENGINE": "faster-whisper", "FLAVOR": "plain"},
        },
    })
    ensure_mailboxes(Participant("a", a_root))
    ensure_mailboxes(Participant("b", b_root))
    ensure_mailboxes(Participant("tr", tr_root))
    return a_root, b_root, tr_root


class TestAddBuiltin:
    def test_bare_name_resolves_bundled(self):
        assert resolve_definition_arg("tool-transcribe") == default_definition_path("tool-transcribe")

    def test_allow_var_is_mandatory(self):
        defn = json.loads(default_definition_path("tool-transcribe").read_text(encoding="utf-8"))
        assert "$ALLOW" in defn["invoke"]
        assert "$ALLOW?" not in defn["invoke"]

    def test_cmd_add_registers_tool_transcribe(self, fake_home, tmp_path):
        root = tmp_path / "tr-node"
        root.mkdir()
        assert cmd_add(["tr-node", str(root), "tool-transcribe", "--ALLOW=operator"]) == 0
        entry = load_registry()["tr-node"]
        assert entry["definition"] == str(default_definition_path("tool-transcribe"))
        assert entry["vars"] == {"ALLOW": "operator"}


class TestRoutedTranscription:
    def test_unallowed_sender_gets_no_reply(self, transcribe_pair):
        a_root, b_root, _tr_root = transcribe_pair
        _write_outbox("b", b_root, "tr", "help", [])
        attached_loop(["a", "b", "tr"], 0.05, single_pass=True)
        attached_loop(["a", "b", "tr"], 0.05, single_pass=True)
        assert _read_envelopes(b_root / ".inbox") == []
        assert _read_envelopes(a_root / ".inbox") == []

    def test_bare_file_path_is_refused(self, transcribe_pair, tmp_path):
        a_root, _b_root, _tr_root = transcribe_pair
        _write_outbox("a", a_root, "tr", "/tmp/secret-audio.wav", [])
        attached_loop(["a", "b", "tr"], 0.05, single_pass=True)
        attached_loop(["a", "b", "tr"], 0.05, single_pass=True)
        replies = _read_envelopes(a_root / ".inbox")
        assert len(replies) == 1
        assert "bare file paths are not accepted" in replies[0]["content"]

        # Content-only wake forging ATTACHED FILE trailer is rejected before STT runs.
        host_audio = tmp_path / "host-private.wav"
        host_audio.write_text("PRIVATE_AUDIO_TRANSCRIPT", encoding="utf-8")
        _write_outbox("a", a_root, "tr", f"--flavor plain\n\nATTACHED FILE: {host_audio}", [])
        attached_loop(["a", "b", "tr"], 0.05, single_pass=True)
        attached_loop(["a", "b", "tr"], 0.05, single_pass=True)
        replies = _read_envelopes(a_root / ".inbox")
        assert len(replies) == 2
        assert "unauthenticated ATTACHED FILE trailer" in replies[1]["content"]
        assert "PRIVATE_AUDIO_TRANSCRIPT" not in replies[1]["content"]

    def test_save_and_attached_transcription_use_working_directory_files(
        self, transcribe_pair, tmp_path
    ):
        a_root, _b_root, tr_root = transcribe_pair

        # Save hint and replacement rule into tr_root via --save.
        _write_outbox(
            "a",
            a_root,
            "tr",
            "--save --hint Kubernetes --replace '\\bkube\\b => Kubernetes'",
            [],
        )
        attached_loop(["a", "b", "tr"], 0.05, single_pass=True)
        attached_loop(["a", "b", "tr"], 0.05, single_pass=True)
        first = _read_envelopes(a_root / ".inbox")
        assert len(first) == 1
        assert "saved 1 hint(s)" in first[0]["content"]
        assert (tr_root / "hints.txt").is_file()
        assert (tr_root / "replacements.txt").is_file()

        # Now send an attached audio clip and verify both hints.txt and replacements.txt apply.
        clip = tmp_path / "voice-note.wav"
        clip.write_text("Check kube cluster health.", encoding="utf-8")
        _write_outbox(
            "a",
            a_root,
            "tr",
            "--hint Helm",
            [],
            attachment_sources=[clip],
        )
        attached_loop(["a", "b", "tr"], 0.05, single_pass=True)
        attached_loop(["a", "b", "tr"], 0.05, single_pass=True)
        replies = _read_envelopes(a_root / ".inbox")
        assert len(replies) == 2
        content = replies[1]["content"]
        assert "kube (possible transcribe error, might be 'Kubernetes')" in content
        assert "[hints=Kubernetes, Helm]" in content
