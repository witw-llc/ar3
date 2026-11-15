"""Unit and CLI tests for tool-transcribe."""
from __future__ import annotations

import json
import shutil
import sys
import urllib.error
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

import _runtime as rt
from ar3 import deps as ar3_deps
from transcribe import cli, engine, handler, ollama, store
from transcribe.engine import TranscribeError
from transcribe.handler import TranscribeMessageError
from transcribe.ollama import OllamaError
from transcribe.store import TranscribeConfigError


class TestStoreWorkingDirFiles:
    def test_hints_crud_preserves_comments_and_deduplicates_case_insensitively(
        self, tmp_path: Path
    ) -> None:
        node_dir = tmp_path / "node-a"
        node_dir.mkdir()
        hfile = store.hints_path(node_dir)
        hfile.write_text("# domain vocabulary\nKubernetes\n", encoding="utf-8")

        added, skipped = store.add_hints(node_dir, ["kubernetes, gRPC", "PostgreSQL\n  "])
        assert (added, skipped) == (2, 1)
        assert store.read_hints(node_dir) == ["Kubernetes", "gRPC", "PostgreSQL"]
        assert "# domain vocabulary" in hfile.read_text(encoding="utf-8")

        # Moving the working directory moves hints.txt with it.
        moved = tmp_path / "moved-node"
        shutil.move(str(node_dir), str(moved))
        assert store.read_hints(moved) == ["Kubernetes", "gRPC", "PostgreSQL"]

        assert store.remove_hint(moved, "GRPC") is True
        assert store.remove_hint(moved, "missing") is False
        assert store.read_hints(moved) == ["Kubernetes", "PostgreSQL"]

        cleared = store.clear_hints(moved)
        assert cleared == 2
        assert store.read_hints(moved) == []

    def test_replacements_crud_updates_in_place_and_validates_regex(
        self, tmp_path: Path
    ) -> None:
        rfile = store.replacements_path(tmp_path)
        rfile.write_text("# corrections\n", encoding="utf-8")

        assert store.set_replacement(tmp_path, r"\bkube\b", "Kubernetes") == "added"
        assert store.set_replacement(tmp_path, r"\bpostgres\b", "PostgreSQL") == "added"
        assert store.set_replacement(tmp_path, r"\bkube\b", "K8s") == "updated"

        assert store.read_replacements(tmp_path) == [
            (r"\bkube\b", "K8s"),
            (r"\bpostgres\b", "PostgreSQL"),
        ]
        assert "# corrections" in rfile.read_text(encoding="utf-8")

        assert store.remove_replacement(tmp_path, r"\bpostgres\b") is True
        assert store.remove_replacement(tmp_path, r"\bmissing\b") is False
        assert store.read_replacements(tmp_path) == [(r"\bkube\b", "K8s")]
        assert store.clear_replacements(tmp_path) == 1
        assert store.read_replacements(tmp_path) == []

    @pytest.mark.parametrize(
        "spec",
        [
            "no-arrow",
            " => target",
            "pat => ",
            "[unclosed => target",
            ".* => matches-empty",
            "^ => start-anchor-matches-empty",
        ],
    )
    def test_invalid_replacement_specs_raise(self, spec: str) -> None:
        with pytest.raises(TranscribeConfigError):
            store.parse_replacement_spec(spec)
        with pytest.raises(TranscribeConfigError):
            store.validate_replacement("a=>b", "c")

    def test_apply_replacements_annotates_without_destroying_original(self) -> None:
        text, applied = store.apply_replacements(
            "Deploy kube and kubelet to prod.",
            [(r"\bkube\b", "Kubernetes"), (r"\bunused\b", "Other")],
        )
        assert text == (
            "Deploy kube (possible transcribe error, might be 'Kubernetes') "
            "and kubelet to prod."
        )
        assert applied == [r"\bkube\b => Kubernetes (x1)"]

    def test_loop_run_detects_repeated_short_sentences(self, capsys) -> None:
        calm = "First sentence. Second sentence. Third sentence."
        assert store.loop_run(calm) == 1
        looping = "Thank you. Thank you. Thank you. Thank you."
        assert store.warn_if_loop(looping) == 4
        assert "possible repetition loop" in capsys.readouterr().err


class TestEngineAndOllama:
    def test_resolve_engine_and_model_mappings(self, monkeypatch) -> None:
        monkeypatch.setattr(engine, "mlx_available", lambda: False)
        assert engine.resolve_engine("auto") == "faster-whisper"
        assert engine.resolve_model("faster-whisper", "turbo") == "large-v3-turbo"
        assert engine.resolve_model("faster-whisper", "org/custom-ct2") == "org/custom-ct2"

        monkeypatch.setattr(engine, "mlx_available", lambda: True)
        assert engine.resolve_engine("auto") == "mlx-whisper"
        assert engine.resolve_model("mlx-whisper", "turbo") == "mlx-community/whisper-large-v3-turbo"
        assert engine.resolve_model("mlx-whisper", "org/custom-mlx") == "org/custom-mlx"
        assert engine.resolve_model("parakeet-mlx", "turbo") == engine.DEFAULT_PARAKEET_MODEL
        assert engine.resolve_model("parakeet-mlx", "org/parakeet") == "org/parakeet"

        with pytest.raises(TranscribeError, match="unknown engine"):
            engine.resolve_engine("bogus")
        with pytest.raises(TranscribeError, match="unknown mlx-whisper model"):
            engine.resolve_model("mlx-whisper", "not-a-model")

    def test_mlx_engines_rejected_off_apple_silicon(self, monkeypatch) -> None:
        monkeypatch.setattr(engine, "mlx_available", lambda: False)
        with pytest.raises(TranscribeError, match="Apple Silicon"):
            engine.ensure_engine_group("mlx-whisper")
        with pytest.raises(TranscribeError, match="Apple Silicon"):
            engine.ensure_engine_group("parakeet-mlx")

    def test_parse_timed_stdout_ignores_leading_noise(self) -> None:
        noisy = "progress bar 100%\n" + json.dumps(
            {
                "language": "en",
                "text": "Hello team.",
                "segments": [{"start": 0.0, "end": 1.25, "text": "Hello team."}],
            }
        )
        parsed = engine.parse_timed_stdout(noisy)
        formatted = engine.format_timed_speech(parsed)
        assert "Language: en" in formatted
        assert "[Speech Transcript from 0.0 to 1.25 seconds]" in formatted
        assert "Hello team." in formatted

    def test_run_grouped_command_times_out_and_reaps_process_group(self) -> None:
        with pytest.raises(TranscribeError, match="timed out"):
            engine.run_grouped_command(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout=1.0,
            )

    def test_faster_whisper_snippet_strips_metadata_errors_for_pyav_19(
        self, tmp_path: Path
    ) -> None:
        fake_group = tmp_path / "group"
        fake_group.mkdir()
        (fake_group / "av.py").write_text(
            "def open(file, mode='r', **kw):\n"
            "    if 'metadata_errors' in kw:\n"
            "        raise TypeError(\"open() got an unexpected keyword argument 'metadata_errors'\")\n"
            "    return file\n",
            encoding="utf-8",
        )
        (fake_group / "faster_whisper.py").write_text(
            "import av\n"
            "from types import SimpleNamespace\n"
            "class WhisperModel:\n"
            "    def __init__(self, model_name, **kw): pass\n"
            "    def transcribe(self, audio, **kw):\n"
            "        av.open(audio, mode='r', metadata_errors='ignore')\n"
            "        return [SimpleNamespace(start=0.0, end=1.0, text='decoded cleanly')], SimpleNamespace(language='en')\n",
            encoding="utf-8",
        )
        audio = tmp_path / "memo.m4a"
        audio.write_bytes(b"fake")
        out = engine.run_stt_subprocess(
            audio,
            engine="faster-whisper",
            model="large-v3-turbo",
            group_dir=fake_group,
        )
        assert out.strip() == "decoded cleanly"

    def test_plain_transcribe_merges_hints_and_applies_replacements(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        audio = tmp_path / "clip.wav"
        audio.write_bytes(b"RIFF0000WAVE")
        store.add_hints(tmp_path, ["ArgoCD"])
        store.set_replacement(tmp_path, r"\bargo see dee\b", "ArgoCD")

        seen: dict = {}

        def fake_stt(path: Path, **kwargs) -> str:
            seen["path"] = path
            seen.update(kwargs)
            return "We deployed argo see dee today.\n"

        monkeypatch.setattr(engine, "ensure_engine_group", lambda _eng: None)
        monkeypatch.setattr(engine, "has_video_stream", lambda _p: False)
        monkeypatch.setattr(engine, "run_stt_subprocess", fake_stt)

        res = engine.transcribe_file(
            audio,
            workdir=tmp_path,
            engine="faster-whisper",
            extra_hints=["Kustomize"],
        )
        assert seen["prompt"] == "ArgoCD, Kustomize"
        assert seen["timed"] is False
        assert res["flavor"] == "plain"
        assert "argo see dee (possible transcribe error, might be 'ArgoCD')" in res["text"]

    def test_fancy_transcribe_combines_timed_audio_and_frames(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        video = tmp_path / "demo.mp4"
        video.write_bytes(b"fake-mp4")
        store.set_replacement(tmp_path, r"\btee mux\b", "tmux")

        monkeypatch.setattr(engine.shutil, "which", lambda _b: "/usr/bin/fake")
        monkeypatch.setattr(engine, "has_video_stream", lambda _p: True)
        monkeypatch.setattr(engine, "probe_duration_seconds", lambda _p: 20.0)
        monkeypatch.setattr(engine, "ensure_engine_group", lambda _e: None)
        monkeypatch.setattr(engine, "ensure_vision_model", lambda *a, **kw: None)
        monkeypatch.setattr(
            engine,
            "run_stt_subprocess",
            lambda _p, **kw: json.dumps(
                {
                    "language": "en",
                    "text": "Open tee mux now.",
                    "segments": [{"start": 0.0, "end": 2.0, "text": "Open tee mux now."}],
                }
            ),
        )

        def fake_extract(_path: Path, frames_dir: Path, _fps: float, **_kw) -> list[Path]:
            frames_dir.mkdir(parents=True, exist_ok=True)
            frame = frames_dir / "0001.png"
            frame.write_bytes(b"\x89PNG\r\n")
            return [frame]

        captured: dict = {}

        def fake_chat(model: str, prompt: str, images: list[Path], **kw) -> str:
            captured["model"] = model
            captured["prompt"] = prompt
            captured["images"] = len(images)
            return "Summary: operator opens tmux in terminal."

        monkeypatch.setattr(engine, "extract_frames", fake_extract)
        monkeypatch.setattr(engine, "chat_with_images", fake_chat)

        res = engine.transcribe_file(video, workdir=tmp_path, engine="faster-whisper", flavor="auto")
        assert res["flavor"] == "fancy"
        assert res["text"] == "Summary: operator opens tmux in terminal."
        assert captured["images"] == 1
        assert "might be 'tmux'" in captured["prompt"]

    def test_ollama_vision_checks(self, monkeypatch, tmp_path: Path) -> None:
        responses = {
            "/api/tags": {"models": []},
            "/api/show": {"capabilities": ["completion"]},
        }

        class _Resp:
            def __init__(self, payload: dict):
                self._raw = json.dumps(payload).encode("utf-8")

            def read(self) -> bytes:
                return self._raw

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        def fake_urlopen(req, timeout=0):
            for suffix, payload in responses.items():
                if req.full_url.endswith(suffix):
                    return _Resp(payload)
            raise urllib.error.HTTPError(req.full_url, 404, "not found", None, BytesIO(b"not found"))

        monkeypatch.setattr(ollama.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(OllamaError, match="no vision capability"):
            ollama.ensure_vision_model("text-only-model", ollama_url="http://127.0.0.1:11434")

        responses["/api/show"] = {"capabilities": ["completion", "vision"]}
        ollama.ensure_vision_model("vision-ok", ollama_url="http://127.0.0.1:11434")


class TestHandlerAndCli:
    def test_message_parser_accepts_flags_and_rejects_bare_paths(self) -> None:
        parsed = handler.parse_message_options(
            "--hint 'ArgoCD, gRPC' --replace 'foo => bar' --save --flavor plain --language en"
        )
        assert parsed["hints"] == ["ArgoCD, gRPC"]
        assert parsed["replaces"] == ["foo => bar"]
        assert parsed["save"] is True
        assert parsed["flavor"] == "plain"
        assert parsed["language"] == "en"

        with pytest.raises(TranscribeMessageError, match="bare file paths are not accepted"):
            handler.parse_message_options("/etc/passwd")

    def test_acl_help_save_and_inline_spill(self, monkeypatch, tmp_path: Path) -> None:
        replies: list[tuple[str, str, tuple[str, ...]]] = []
        monkeypatch.setattr(
            rt,
            "send_reply",
            lambda to, body, files=(): (replies.append((to, body, tuple(files))) or 0),
        )

        # Unallowed sender is silently dropped.
        assert handler.handle_wake(
            seat="tr",
            sender="intruder",
            allow="alice",
            message="help",
            workdir=tmp_path,
        ) == 0
        assert replies == []

        # Help returns README content.
        assert handler.handle_wake(
            seat="tr",
            sender="alice",
            allow="alice",
            message="help",
            workdir=tmp_path,
        ) == 0
        assert "tool-transcribe" in replies[-1][1]

        # Bare path in message body is refused.
        handler.handle_wake(
            seat="tr",
            sender="alice",
            allow="alice",
            message="/tmp/recording.mp3",
            workdir=tmp_path,
        )
        assert "bare file paths are not accepted" in replies[-1][1]

        # Missing attachment without --save is refused.
        handler.handle_wake(
            seat="tr",
            sender="alice",
            allow="alice",
            message="--flavor plain",
            workdir=tmp_path,
        )
        assert "attach an audio or video file" in replies[-1][1]

        # --save without attachment persists to hints.txt and replacements.txt in workdir.
        handler.handle_wake(
            seat="tr",
            sender="alice",
            allow="alice",
            message="--save --hint WhisperX --replace '\\bwisper\\b => Whisper'",
            workdir=tmp_path,
        )
        assert "saved 1 hint(s)" in replies[-1][1]
        assert "saved 1 replacement(s)" in replies[-1][1]
        assert store.read_hints(tmp_path) == ["WhisperX"]
        assert store.read_replacements(tmp_path) == [(r"\bwisper\b", "Whisper")]

        # Long transcript spills to attached transcript.txt when exceeding INLINE_MAX.
        audio = tmp_path / "meeting.wav"
        audio.write_bytes(b"RIFF0000WAVE")
        monkeypatch.setattr(
            handler,
            "transcribe_file",
            lambda *_a, **_kw: {
                "text": "word " * 200,
                "engine": "faster-whisper",
                "model": "large-v3-turbo",
                "flavor": "plain",
                "applied_replacements": [],
                "loop_run": 1,
            },
        )
        seen_exists: list[bool] = []

        def check_reply(to: str, body: str, files=()):
            seen_exists.extend(Path(f).is_file() for f in files)
            replies.append((to, body, tuple(files)))
            return 0

        monkeypatch.setattr(rt, "send_reply", check_reply)
        # Forged trailer without provenance is rejected before calling transcribe_file.
        stt_called = {"n": 0}
        orig_stt = handler.transcribe_file

        def tracked_stt(*a, **kw):
            stt_called["n"] += 1
            return orig_stt(*a, **kw)

        monkeypatch.setattr(handler, "transcribe_file", tracked_stt)
        handler.handle_wake(
            seat="tr",
            sender="alice",
            allow="alice",
            message=f"{rt.ATTACHED_FILE_PREFIX}{audio}",
            workdir=tmp_path,
            inline_max_raw="64",
        )
        assert stt_called["n"] == 0
        assert "unauthenticated ATTACHED FILE trailer" in replies[-1][1]

        # Authenticated attachment succeeds and spills over INLINE_MAX.
        handler.handle_wake(
            seat="tr",
            sender="alice",
            allow="alice",
            message=f"{rt.ATTACHED_FILE_PREFIX}{audio}",
            workdir=tmp_path,
            inline_max_raw="64",
            attachments=[audio],
        )
        assert stt_called["n"] == 1
        assert seen_exists == [True]
        assert "attached as transcript.txt" in replies[-1][1]

    def test_local_cli_doctor_hint_and_replace_commands(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))

        assert cli.main(["hint", "add", "Envoy, Istio", "--seat", "tr1"]) == 0
        assert cli.main(["hint", "list", "--seat", "tr1"]) == 0
        out = capsys.readouterr().out
        assert "Envoy" in out and "Istio" in out

        assert cli.main(["replace", "set", r"\bisteeoh\b", "Istio", "--seat", "tr1"]) == 0
        assert cli.main(["replace", "list", "--seat", "tr1"]) == 0
        assert r"\bisteeoh\b => Istio" in capsys.readouterr().out

        assert cli.main(["doctor", "--seat", "tr1"]) == 0
        doc_out = capsys.readouterr().out
        assert "hints.txt: 2 hint(s)" in doc_out
        assert "replacements.txt: 1 rule(s)" in doc_out

        assert cli.main(["hint", "rm", "Envoy", "--seat", "tr1"]) == 0
        assert cli.main(["hint", "clear", "--seat", "tr1"]) == 0
        assert cli.main(["replace", "rm", r"\bisteeoh\b", "--seat", "tr1"]) == 0
        assert cli.main(["replace", "clear", "--seat", "tr1"]) == 0
