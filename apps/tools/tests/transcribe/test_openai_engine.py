"""Tests for the tool-transcribe `openai` engine. HTTP and ffmpeg are mocked; no network."""
from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from pathlib import Path

import pytest

import _runtime as rt
from ar3 import deps as ar3_deps
from transcribe import engine, handler, openai_stt, store
from transcribe.engine import TranscribeError

FAKE_KEY = "sk-test-FAKEKEY0123456789abcdef"


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self, *_a) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _form_fields(req) -> dict[str, list[str]]:
    ctype = req.headers["Content-type"]
    boundary = ctype.split("boundary=", 1)[1].encode()
    fields: dict[str, list[str]] = {}
    for part in req.data.split(b"--" + boundary):
        head, sep, value = part.partition(b"\r\n\r\n")
        if not sep or b'name="' not in head:
            continue
        name = head.split(b'name="', 1)[1].split(b'"', 1)[0].decode()
        if b"filename=" in head:
            value = b"<file>\r\n"
        fields.setdefault(name, []).append(value[:-2].decode("utf-8", errors="replace"))
    return fields


class _FakeApi:
    """Stands in for urlopen; replies with one queued payload per request."""

    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.requests: list = []
        self.timeouts: list[float] = []

    def __call__(self, req, timeout=0):
        self.requests.append(req)
        self.timeouts.append(timeout)
        return _Resp(self.payloads.pop(0))


@pytest.fixture
def key(monkeypatch) -> str:
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    return FAKE_KEY


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    """Replace ffmpeg runs: write a small output file and record each argv."""
    calls: list[list[str]] = []
    sizes = {"full": 1000, "chunk": 500}

    def fake_run(argv, *, timeout=0, env=None):
        calls.append(list(argv))
        dst = Path(argv[-1])
        dst.write_bytes(b"\0" * (sizes["chunk"] if "-ss" in argv else sizes["full"]))
        return 0, "", ""

    monkeypatch.setattr(engine, "run_grouped_command", fake_run)
    return calls, sizes


def _patch_api(monkeypatch, payloads: list[dict]) -> _FakeApi:
    api = _FakeApi(payloads)
    monkeypatch.setattr(openai_stt.urllib.request, "urlopen", api)
    return api


class TestOpenAIEngineSelection:
    @pytest.mark.parametrize("mlx", [True, False])
    def test_auto_never_selects_openai(self, monkeypatch, mlx: bool) -> None:
        monkeypatch.setattr(engine, "mlx_available", lambda: mlx)
        for requested in (None, "", "auto", "AUTO"):
            assert engine.resolve_engine(requested) != "openai"
        assert engine.resolve_engine("openai") == "openai"
        assert engine.resolve_engine("OpenAI") == "openai"

    def test_default_model_mapping_and_no_deps_group(self, monkeypatch) -> None:
        for local in (None, "", "turbo", "large-v3", "large", "tiny", "large-v3-turbo"):
            assert engine.resolve_model("openai", local) == "whisper-1"
        for explicit in ("whisper-1", "gpt-4o-transcribe", "gpt-4o-mini-transcribe", "future-stt-2"):
            assert engine.resolve_model("openai", explicit) == explicit

        def no_deps(*_a, **_kw):
            raise AssertionError("openai must not touch ar3.deps")

        monkeypatch.setattr(ar3_deps, "ensure_group", no_deps)
        monkeypatch.setattr(ar3_deps, "require_group", no_deps)
        assert engine.ensure_engine_group("openai") is None


class TestOpenAIRequests:
    def test_missing_key_names_the_vars_command(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        audio = tmp_path / "memo.m4a"
        audio.write_bytes(b"fake")
        api = _patch_api(monkeypatch, [])
        with pytest.raises(TranscribeError, match=r"a8s vars <node> set secret\.OPENAI_API_KEY -"):
            engine.transcribe_file(audio, workdir=tmp_path, engine="openai", flavor="plain")
        assert api.requests == []

    def test_plain_request_fields_timeout_and_temp_cleanup(
        self, monkeypatch, tmp_path: Path, key: str, fake_ffmpeg
    ) -> None:
        calls, _sizes = fake_ffmpeg
        api = _patch_api(monkeypatch, [{"text": " Hello team. "}])
        audio = tmp_path / "memo.m4a"
        audio.write_bytes(b"fake")

        out = engine.run_openai_stt(
            audio,
            model="gpt-4o-transcribe",
            language="en",
            prompt="ArgoCD, gRPC",
            timeout=90.0,
        )
        assert out == "Hello team."

        req = api.requests[0]
        assert req.full_url == openai_stt.API_URL
        assert req.get_method() == "POST"
        assert req.headers["Authorization"] == f"Bearer {key}"
        fields = _form_fields(req)
        assert fields["model"] == ["gpt-4o-transcribe"]
        assert fields["response_format"] == ["json"]
        assert fields["language"] == ["en"]
        assert fields["prompt"] == ["ArgoCD, gRPC"]
        assert fields["file"] == ["<file>"]
        assert "timestamp_granularities[]" not in fields
        assert 0 < api.timeouts[0] <= 90.0

        argv = calls[0]
        assert argv[argv.index("-i") + 1] == str(audio)
        assert argv[argv.index("-ac") + 1] == "1"
        assert argv[argv.index("-ar") + 1] == "16000"
        assert argv[-1].endswith(".mp3")
        assert not Path(argv[-1]).parent.exists()

    def test_timed_verbose_json_parses_into_standard_shape(
        self, monkeypatch, tmp_path: Path, key: str, fake_ffmpeg
    ) -> None:
        api = _patch_api(
            monkeypatch,
            [
                {
                    "task": "transcribe",
                    "language": "english",
                    "duration": 3.5,
                    "text": "Hello team. Ship it.",
                    "segments": [
                        {"id": 0, "start": 0.0, "end": 1.5, "text": " Hello team.", "avg_logprob": -0.2},
                        {"id": 1, "start": 1.5, "end": 3.5, "text": " Ship it."},
                    ],
                }
            ],
        )
        audio = tmp_path / "clip.mp4"
        audio.write_bytes(b"fake")

        out = engine.run_openai_stt(audio, model="whisper-1", timed=True)
        payload = engine.parse_timed_stdout(out)
        assert payload == {
            "language": "english",
            "text": "Hello team. Ship it.",
            "segments": [
                {"start": 0.0, "end": 1.5, "text": "Hello team."},
                {"start": 1.5, "end": 3.5, "text": "Ship it."},
            ],
        }
        fields = _form_fields(api.requests[0])
        assert fields["response_format"] == ["verbose_json"]
        assert fields["timestamp_granularities[]"] == ["segment"]
        assert "[Speech Transcript from 1.5 to 3.5 seconds]" in engine.format_timed_speech(payload)

    def test_timed_output_refuses_models_without_segments(
        self, monkeypatch, tmp_path: Path, key: str, fake_ffmpeg
    ) -> None:
        api = _patch_api(monkeypatch, [])
        video = tmp_path / "demo.mp4"
        video.write_bytes(b"fake")
        monkeypatch.setattr(engine.shutil, "which", lambda _b: "/usr/bin/fake")
        monkeypatch.setattr(engine, "has_video_stream", lambda _p: True)

        for model in ("gpt-4o-transcribe", "gpt-4o-mini-transcribe"):
            with pytest.raises(TranscribeError, match="whisper-1"):
                engine.transcribe_file(
                    video, workdir=tmp_path, engine="openai", model=model, flavor="fancy"
                )
            with pytest.raises(TranscribeError, match="segment timestamps"):
                engine.run_openai_stt(video, model=model, timed=True)
        assert api.requests == []

    def test_oversize_audio_splits_and_offsets_segments(
        self, monkeypatch, tmp_path: Path, key: str, fake_ffmpeg
    ) -> None:
        calls, sizes = fake_ffmpeg
        sizes["full"] = 2500
        monkeypatch.setattr(engine, "OPENAI_UPLOAD_LIMIT", 1000)
        monkeypatch.setattr(engine, "probe_duration_seconds", lambda _p, timeout=30.0: 90.0)
        api = _patch_api(
            monkeypatch,
            [
                {"language": "english", "text": f"Part {n}.", "segments": [
                    {"start": 1.0, "end": 4.0, "text": f"Part {n}."},
                ]}
                for n in (1, 2, 3)
            ],
        )
        audio = tmp_path / "long.wav"
        audio.write_bytes(b"fake")

        payload = json.loads(engine.run_openai_stt(audio, model="whisper-1", timed=True))
        assert payload["text"] == "Part 1. Part 2. Part 3."
        assert [(s["start"], s["end"]) for s in payload["segments"]] == [
            (1.0, 4.0),
            (31.0, 34.0),
            (61.0, 64.0),
        ]
        assert len(api.requests) == 3

        chunk_calls = calls[1:]
        assert [c[c.index("-ss") + 1] for c in chunk_calls] == ["0.000", "30.000", "60.000"]
        assert [c[c.index("-t") + 1] for c in chunk_calls[:2]] == ["30.000", "30.000"]
        assert "-t" not in chunk_calls[2]
        assert not Path(calls[0][-1]).parent.exists()

    def test_http_errors_show_status_and_scrub_the_key(
        self, monkeypatch, tmp_path: Path, key: str, fake_ffmpeg
    ) -> None:
        body = json.dumps(
            {"error": {"message": f"Incorrect API key provided: {key}.", "type": "invalid_request_error"}}
        ).encode()

        def reject(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", None, BytesIO(body))

        monkeypatch.setattr(openai_stt.urllib.request, "urlopen", reject)
        audio = tmp_path / "memo.m4a"
        audio.write_bytes(b"fake")
        with pytest.raises(TranscribeError) as caught:
            engine.run_openai_stt(audio, model="whisper-1")
        msg = str(caught.value)
        assert "openai HTTP 401" in msg
        assert "Incorrect API key provided" in msg
        assert key not in msg
        assert openai_stt.REDACTED in msg

        def offline(req, timeout=0):
            raise urllib.error.URLError(f"proxy refused token {key}")

        monkeypatch.setattr(openai_stt.urllib.request, "urlopen", offline)
        with pytest.raises(TranscribeError) as caught:
            engine.run_openai_stt(audio, model="whisper-1")
        assert "openai request failed" in str(caught.value)
        assert key not in str(caught.value)

    def test_handler_reply_scrubs_the_key(self, monkeypatch, tmp_path: Path, key: str) -> None:
        replies: list[str] = []
        monkeypatch.setattr(rt, "send_reply", lambda _to, body, files=(): (replies.append(body) or 0))

        def leaky(*_a, **_kw):
            raise TranscribeError(f"upstream echoed {key}")

        monkeypatch.setattr(handler, "transcribe_file", leaky)
        audio = tmp_path / "memo.m4a"
        audio.write_bytes(b"fake")
        handler.handle_wake(
            seat="tr",
            sender="op",
            allow="op",
            message=f"--engine openai\n{rt.ATTACHED_FILE_PREFIX}{audio}",
            workdir=tmp_path,
            attachments=[audio],
        )
        assert replies and key not in replies[-1]
        assert openai_stt.REDACTED in replies[-1]


class TestOpenAIDeadline:
    """TIMEOUT bounds the whole file: transcode, probe, and every request with its response."""

    @pytest.mark.parametrize("status", [200, 401])
    def test_a_trickled_response_cannot_outlast_the_budget(
        self, monkeypatch, tmp_path: Path, key: str, fake_ffmpeg, status: int
    ) -> None:
        import threading
        import time
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        parts = (
            [b'{"text":', b'"slow', b' response', b' succeeds"}']
            if status == 200
            else [b'{"error":', b'{"message":', b'"slow', b' refusal"}}']
        )

        class Trickle(BaseHTTPRequestHandler):
            def log_message(self, *_a) -> None:
                pass

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(status)
                self.send_header("Content-Length", str(sum(map(len, parts))))
                self.end_headers()
                try:
                    for part in parts:
                        time.sleep(0.4)
                        self.wfile.write(part)
                        self.wfile.flush()
                except OSError:
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        real = engine.transcribe_audio
        url = f"http://127.0.0.1:{server.server_port}/"
        monkeypatch.setattr(engine, "transcribe_audio", lambda p, **kw: real(p, url=url, **kw))
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"fake")
        try:
            started = time.monotonic()
            with pytest.raises(TranscribeError, match="timed out"):
                engine.run_openai_stt(audio, model="whisper-1", timeout=0.8)
            assert time.monotonic() - started < 1.4
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_the_split_probe_gets_only_the_time_left(
        self, monkeypatch, tmp_path: Path, key: str, fake_ffmpeg
    ) -> None:
        _calls, sizes = fake_ffmpeg
        sizes["full"] = 2500
        monkeypatch.setattr(engine, "OPENAI_UPLOAD_LIMIT", 1000)
        budgets: list[float] = []

        def probe(_path, timeout=30.0):
            budgets.append(timeout)
            return 90.0

        monkeypatch.setattr(engine, "probe_duration_seconds", probe)
        _patch_api(monkeypatch, [{"text": f"Part {n}."} for n in (1, 2, 3)])
        audio = tmp_path / "long.wav"
        audio.write_bytes(b"fake")
        engine.run_openai_stt(audio, model="whisper-1", timeout=5)
        assert len(budgets) == 1 and 0 < budgets[0] <= 5


class TestOpenAIHintsAndReplacements:
    def test_hints_reach_prompt_and_replacements_annotate_plain_text(
        self, monkeypatch, tmp_path: Path, key: str, fake_ffmpeg
    ) -> None:
        store.add_hints(tmp_path, ["ArgoCD"])
        store.set_replacement(tmp_path, r"\bargo see dee\b", "ArgoCD")
        monkeypatch.setattr(engine.shutil, "which", lambda _b: "/usr/bin/fake")
        monkeypatch.setattr(engine, "has_video_stream", lambda _p: False)
        api = _patch_api(monkeypatch, [{"text": "We deployed argo see dee today."}])
        audio = tmp_path / "memo.m4a"
        audio.write_bytes(b"fake")

        res = engine.transcribe_file(
            audio,
            workdir=tmp_path,
            engine="openai",
            extra_hints=["Kustomize"],
        )
        assert _form_fields(api.requests[0])["prompt"] == ["ArgoCD, Kustomize"]
        assert _form_fields(api.requests[0])["model"] == ["whisper-1"]
        assert res["engine"] == "openai"
        assert res["model"] == "whisper-1"
        assert res["flavor"] == "plain"
        assert "argo see dee (possible transcribe error, might be 'ArgoCD')" in res["text"]

    def test_replacements_annotate_timed_speech_for_fancy(
        self, monkeypatch, tmp_path: Path, key: str, fake_ffmpeg
    ) -> None:
        store.set_replacement(tmp_path, r"\btee mux\b", "tmux")
        monkeypatch.setattr(engine.shutil, "which", lambda _b: "/usr/bin/fake")
        monkeypatch.setattr(engine, "has_video_stream", lambda _p: True)
        monkeypatch.setattr(engine, "probe_duration_seconds", lambda _p: 20.0)
        monkeypatch.setattr(engine, "ensure_vision_model", lambda *a, **kw: None)
        _patch_api(
            monkeypatch,
            [{"language": "english", "text": "Open tee mux now.", "segments": [
                {"start": 0.0, "end": 2.0, "text": "Open tee mux now."},
            ]}],
        )

        def fake_extract(_path: Path, frames_dir: Path, _fps: float, **_kw) -> list[Path]:
            frames_dir.mkdir(parents=True, exist_ok=True)
            frame = frames_dir / "0001.png"
            frame.write_bytes(b"\x89PNG\r\n")
            return [frame]

        captured: dict = {}

        def fake_chat(_model: str, prompt: str, _images: list[Path], **_kw) -> str:
            captured["prompt"] = prompt
            return "Summary: operator opens tmux."

        monkeypatch.setattr(engine, "extract_frames", fake_extract)
        monkeypatch.setattr(engine, "chat_with_images", fake_chat)
        video = tmp_path / "demo.mp4"
        video.write_bytes(b"fake")

        res = engine.transcribe_file(video, workdir=tmp_path, engine="openai", flavor="auto")
        assert res["flavor"] == "fancy"
        assert res["model"] == "whisper-1"
        assert "[Speech Transcript from 0.0 to 2.0 seconds]" in captured["prompt"]
        assert "might be 'tmux'" in captured["prompt"]


class TestOpenAIDoctor:
    def test_doctor_reports_key_presence_without_value(
        self, monkeypatch, tmp_path: Path, capsys
    ) -> None:
        monkeypatch.setattr(
            openai_stt.urllib.request,
            "urlopen",
            lambda *_a, **_kw: pytest.fail("doctor must not call the network"),
        )
        monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
        assert engine.doctor_report(tmp_path, engine="openai") == 0
        out = capsys.readouterr().out
        assert "engine: openai" in out
        assert "OPENAI_API_KEY: set" in out
        assert FAKE_KEY not in out

        monkeypatch.delenv("OPENAI_API_KEY")
        assert engine.doctor_report(tmp_path, engine="openai") == 0
        out = capsys.readouterr().out
        assert "WARN OPENAI_API_KEY is not set" in out
        assert "a8s vars <node> set secret.OPENAI_API_KEY -" in out
