"""Unit, handler, protocol, and CLI tests for tool-speak."""
from __future__ import annotations

from array import array
from concurrent.futures import ThreadPoolExecutor
import base64
import cmath
import hashlib
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import urllib.error
import wave

import pytest

import _runtime as rt
from speak import _pocket_runner, cli, engine, handler, markdown, secretstore, store, voices
from speak.engine import SpeakError
from speak.secretstore import SecretStoreError
from speak.store import SpeakConfigError


class TestConfigAndWorkingDirFiles:
    def test_config_crud_and_vendor_bound_custom_voice(self, tmp_path: Path) -> None:
        assert store.read_config(tmp_path) == {}
        saved = store.update_config(
            tmp_path,
            engine="openai",
            voice="voice_custom42",
            audio_format="m4a",
            speed=1.1,
            style="warm, calm",
        )
        assert saved == {
            "engine": "openai",
            "format": "m4a",
            "speed": 1.1,
            "style": "warm, calm",
            "voice": "openai:voice_custom42",
        }
        sel_same = engine.select_engine_and_voice(tmp_path)
        assert (sel_same.engine, sel_same.voice, sel_same.how) == (
            "openai",
            "voice_custom42",
            "custom",
        )

        # Switching saved vendor to elevenlabs must NOT forward openai's custom voice ID.
        store.update_config(tmp_path, engine="elevenlabs")
        sel_switched = engine.select_engine_and_voice(tmp_path)
        assert (sel_switched.engine, sel_switched.voice, sel_switched.how) == (
            "elevenlabs",
            voices.DEFAULT_VOICES["elevenlabs"],
            "default",
        )

        # Switching back to openai restores the custom voice ID.
        store.update_config(tmp_path, engine="openai")
        sel_restored = engine.select_engine_and_voice(tmp_path)
        assert (sel_restored.engine, sel_restored.voice, sel_restored.how) == (
            "openai",
            "voice_custom42",
            "custom",
        )

        store.update_config(tmp_path, unset_keys=["style", "speed"])
        assert "style" not in store.read_config(tmp_path)
        assert store.clear_config(tmp_path) is True
        assert store.read_config(tmp_path) == {}

    def test_replacements_and_pronunciations_crud(self, tmp_path: Path) -> None:
        assert store.set_replacement(tmp_path, r"\br4t\b", "are four tee") == "added"
        assert store.set_replacement(tmp_path, r"\br4t\b", "R four T") == "updated"
        assert store.read_replacements(tmp_path) == [(r"\br4t\b", "R four T")]

        replaced, notes = store.apply_replacements("Run r4t now.", store.read_replacements(tmp_path))
        assert replaced == "Run R four T now."
        assert notes == [r"\br4t\b => R four T (x1)"]

        assert store.set_pronunciation(tmp_path, r"\ba8s\b", "eɪ ɪnfɪnɪti") == "added"
        wrapped, p_notes = store.apply_pronunciations("Check a8s.", store.read_pronunciations(tmp_path))
        assert wrapped == "Check [a8s](/eɪ ɪnfɪnɪti/)."
        assert len(p_notes) == 1

        with pytest.raises(SpeakConfigError):
            store.set_replacement(tmp_path, ".*", "bad-empty-regex")

    def test_secretstore_permissions_precedence_and_redaction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env-fallback-9999")
        val, sens = secretstore.resolve_secret(tmp_path, "OPENAI_API_KEY")
        assert val == "sk-env-fallback-9999"

        secretstore.set_secret(tmp_path, "OPENAI_API_KEY", "sk-file-override-1234")
        val2, sens2 = secretstore.resolve_secret(tmp_path, "OPENAI_API_KEY")
        assert val2 == "sk-file-override-1234"
        scrubbed = secretstore.scrub_text(
            "failed with sk-file-override-1234 and sk-env-fallback-9999", sens2
        )
        assert "sk-file-override-1234" not in scrubbed
        assert "sk-env-fallback-9999" not in scrubbed
        assert scrubbed.count("[REDACTED]") == 2

        if os.name == "posix":
            sfile = secretstore.store_path(tmp_path)
            sfile.chmod(0o644)
            with pytest.raises(SecretStoreError, match="chmod 600"):
                secretstore.read_secrets(tmp_path)
            sfile.chmod(0o600)
            assert secretstore.read_secrets(tmp_path) == {"OPENAI_API_KEY": "sk-file-override-1234"}


class TestVoiceFallbackAndMarkdownDirectives:
    def test_voice_stack_resolution_and_say_installed_voices(self) -> None:
        res = voices.resolve_vendor_preference_list(
            "openai:Ember, pocket:Male",
            "message",
            "pocket",
            secret_resolver=lambda _: "sk-ok",
            which=lambda _: None,
        )
        assert (res.engine, res.voice, res.how) == ("pocket", "charles", "gender")
        assert res.missed == ("openai:Ember",)

        say_out = (
            "Ava (Premium)       en_US    # Hello\n"
            "Samantha            en_US    # Hello\n"
        )
        say_cat = voices.parse_say_voice_list(say_out)
        say_res = voices.resolve_for_engine(
            "MissingVoice, Ava", "say", "message", say_voices=say_cat
        )
        assert say_res.voice == "Ava (Premium)"
        assert say_res.missed == ("MissingVoice",)

    def test_markdown_directives_tailor_blocks_and_forbid_vendor_escalation(self) -> None:
        doc = (
            "<!-- speak: voice=\"cedar\" style=\"warm, calm\" speed=1.1 -->\n"
            "# Section One\n\n"
            "This is **important** news.\n\n"
            "<!-- speak: pause=1.5 voice=\"marin\" style=\"crisp\" -->\n"
            "Second paragraph.\n"
        )
        blocks = markdown.parse_markdown_blocks(doc)
        assert len(blocks) == 3
        assert blocks[0].voice == "cedar"
        assert blocks[0].delivery_style == "warm, calm"
        assert blocks[0].speed_override == 1.1
        assert blocks[2].pause_before == 1.5
        assert blocks[2].voice == "marin"
        assert blocks[2].delivery_style == "crisp"

        openai_pieces = markdown.render_block_pieces(
            blocks, base_speed=1.0, preserve_markdown_emphasis=True
        )
        texts = [p.text for p in openai_pieces if p.text]
        assert texts[1] == "This is **important** news."

        with pytest.raises(ValueError, match="cannot switch vendors"):
            markdown.parse_markdown_blocks("<!-- speak: voice=\"openai:marin\" -->\nHello")


class TestOpenAIRealtimeAndCloudBackends:
    def test_deprecated_openai_tts_models_are_rejected(self) -> None:
        for bad in ("tts-1", "tts-1-hd", "gpt-4o-mini-tts", "gpt-4o-mini-tts-2025-12-15"):
            with pytest.raises(SpeakError, match="deprecated"):
                engine.resolve_vendor_model("openai", bad)
        assert engine.resolve_vendor_model("openai", None) == "gpt-realtime-2.1-mini"

    def test_openai_realtime_reconnects_on_voice_change_and_updates_style_in_session(self) -> None:
        audio_b64 = base64.b64encode(b"\x10\x00" * 400).decode("ascii")
        sessions: list["VoiceLockedWS"] = []

        class VoiceLockedWS:
            def __init__(self) -> None:
                self.events: list[dict] = [{"type": "session.created"}]
                self.voice: str | None = None
                self.emitted = False
                self.closed = False
                self.sent: list[dict] = []

            def send_json(self, payload: dict) -> None:
                self.sent.append(payload)
                if payload["type"] == "session.update":
                    new_voice = payload["session"]["audio"]["output"]["voice"]
                    if self.emitted and new_voice != self.voice:
                        self.events.append({
                            "type": "error",
                            "error": {"message": "Voice cannot change after audio output"},
                        })
                    else:
                        self.voice = new_voice
                        self.events.append({"type": "session.updated"})
                elif payload["type"] == "response.create":
                    self.emitted = True
                    self.events.extend([
                        {"type": "response.output_audio.delta", "delta": audio_b64},
                        {"type": "response.done", "response": {"status": "completed"}},
                    ])

            def recv_json(self) -> dict:
                return self.events.pop(0)

            def close(self) -> None:
                self.closed = True

        def make_ws(**_kw):
            conn = VoiceLockedWS()
            sessions.append(conn)
            return conn

        pcm = engine.synthesize_openai_pcm(
            [
                {"text": "First **key** line.", "speed": 1.0, "silence_after": 0.1, "voice": "marin", "style": "warm"},
                {"text": "Second line.", "speed": 1.1, "silence_after": 0.0, "voice": "cedar", "style": "crisp"},
                {"text": "Third line.", "speed": 0.95, "silence_after": 0.0, "voice": "cedar", "style": "calm"},
            ],
            voice="marin",
            model="gpt-realtime-2.1-mini",
            api_key="sk-secret-token",
            connect_fn=make_ws,
        )
        assert len(pcm) == (400 * 2) * 3 + int(0.1 * 24000) * 2
        assert len(sessions) == 2
        assert all(s.closed for s in sessions)
        s0_updates = [e for e in sessions[0].sent if e["type"] == "session.update"]
        s1_updates = [e for e in sessions[1].sent if e["type"] == "session.update"]
        assert len(s0_updates) == 1
        assert s0_updates[0]["session"]["audio"]["output"]["voice"] == "marin"
        assert "Delivery style: warm" in s0_updates[0]["session"]["instructions"]
        assert len(s1_updates) == 2
        assert s1_updates[0]["session"]["audio"]["output"]["voice"] == "cedar"
        assert "Delivery style: crisp" in s1_updates[0]["session"]["instructions"]
        assert s1_updates[1]["session"]["audio"]["output"]["voice"] == "cedar"
        assert "Delivery style: calm" in s1_updates[1]["session"]["instructions"]
        creates = [
            e for s in sessions for e in s.sent if e["type"] == "response.create"
        ]
        assert len(creates) == 3
        assert all(c["response"]["conversation"] == "none" for c in creates)

    def test_say_per_section_voice_and_speed_directives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captures: list[tuple[list[str], str]] = []

        def fake_run(argv: list[str], **_kwargs):
            txt = Path(argv[argv.index("-f") + 1]).read_text(encoding="utf-8")
            captures.append((list(argv), txt))
            engine.write_pcm16_wav(Path(argv[argv.index("-o") + 1]), b"\x01\x00" * 240, 22050)
            return 0, "", ""

        catalog = (voices.Voice("Samantha", "Samantha"), voices.Voice("Alex", "Alex"))
        monkeypatch.setattr(
            engine.shutil, "which", lambda cmd: "/fake/say" if cmd == "say" else None
        )
        monkeypatch.setattr(voices, "installed_say_voices", lambda **_kw: catalog)
        monkeypatch.setattr(engine, "run_grouped_command", fake_run)

        out_wav = tmp_path / "say-out.wav"
        engine.synthesize_to_file(
            "First section.\n\n<!-- speak: voice=Alex speed=1.5 -->\nSecond section.\n\n"
            "<!-- speak: voice=default speed=default -->\nThird section.",
            out_wav,
            workdir=tmp_path,
            engine="say",
            voice="Samantha",
            audio_format="wav",
        )
        assert len(captures) == 3
        assert captures[0][0][captures[0][0].index("-v") + 1] == "Samantha"
        assert captures[0][0][captures[0][0].index("-r") + 1] == "175"
        assert captures[0][1] == "First section."
        assert captures[1][0][captures[1][0].index("-v") + 1] == "Alex"
        assert captures[1][0][captures[1][0].index("-r") + 1] == "262"
        assert captures[1][1] == "Second section."
        assert captures[2][0][captures[2][0].index("-v") + 1] == "Samantha"
        assert captures[2][0][captures[2][0].index("-r") + 1] == "175"
        assert captures[2][1] == "Third section."
        with wave.open(str(out_wav), "rb") as w:
            assert w.getframerate() == 22050
            # 3 segments * 240 frames + 2 paragraph pauses * (0.4s * 22050 = 8820 frames)
            assert w.getnframes() == 3 * 240 + 2 * int(round(0.4 * 22050))

    def test_elevenlabs_and_cartesia_http_and_error_scrubbing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen_reqs: list[tuple[str, dict]] = []

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, _n: int) -> bytes:
                return b"\x05\x00" * 240

        def fake_urlopen(req, timeout=60):
            seen_reqs.append((req.full_url, json.loads(req.data.decode("utf-8"))))
            return FakeResp()

        monkeypatch.setattr(engine.urllib.request, "urlopen", fake_urlopen)
        el_pcm = engine.synthesize_elevenlabs_pcm(
            [{"text": "Hello.", "speed": 1.0, "silence_after": 0.0}],
            voice="21m00Tcm4TlvDq8ikWAM",
            model="eleven_flash_v2_5",
            api_key="xi-secret-key",
        )
        ca_pcm = engine.synthesize_cartesia_pcm(
            [{"text": "World.", "speed": 1.1, "silence_after": 0.0}],
            voice="f786b574-daa5-4673-aa0c-cbe3e8534c02",
            model="sonic-3.6",
            api_key="ca-secret-key",
        )
        assert len(el_pcm) == 480 and len(ca_pcm) == 480
        assert "pcm_24000" in seen_reqs[0][0]
        assert seen_reqs[1][1]["output_format"]["encoding"] == "pcm_s16le"

        def fail_urlopen(_req, timeout=60):
            raise urllib.error.HTTPError(
                "https://api.elevenlabs.io/v1/text-to-speech/x",
                401,
                "Unauthorized",
                hdrs=None,
                fp=io.BytesIO(b'{"detail":{"message":"bad key xi-secret-key"}}'),
            )

        monkeypatch.setattr(engine.urllib.request, "urlopen", fail_urlopen)
        with pytest.raises(SpeakError) as exc_info:
            engine.synthesize_elevenlabs_pcm(
                [{"text": "Hi", "speed": 1.0, "silence_after": 0.0}],
                voice="21m00Tcm4TlvDq8ikWAM",
                model="eleven_flash_v2_5",
                api_key="xi-secret-key",
            )
        assert "xi-secret-key" not in str(exc_info.value)
        assert "[REDACTED]" in str(exc_info.value)


class TestPocketRunnerAndWSOLA:
    def test_synthesis_cache_can_be_updated_in_generation_worker(self) -> None:
        torch = pytest.importorskip("torch")

        class ThreadedModel:
            def generate_audio(self, state, text, **kwargs):
                cache = state.clone()

                def generate():
                    with torch.no_grad():
                        cache[0] = 0.5
                    return cache

                with ThreadPoolExecutor(max_workers=1) as worker:
                    return worker.submit(generate).result(timeout=10)

        state = torch.zeros(2)
        model = ThreadedModel()
        for _ in range(2):
            pcm = _pocket_runner.synth_chunk(model, state, "Hello there")
            assert pcm == b"\x00\x40\x00\x00"
            assert torch.equal(state, torch.zeros(2))

    def test_pocket_checksum_verification_and_wsola_pitch_preservation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rate = 24000
        tone = array(
            "h",
            (round(12000 * math.sin(2.0 * math.pi * 100.0 * i / rate)) for i in range(rate)),
        )
        if sys.byteorder != "little":
            tone.byteswap()

        def amp(samples: array, freq: float) -> float:
            step = cmath.exp(-2j * math.pi * freq / rate)
            phase = 1.0 + 0.0j
            total = 0.0 + 0.0j
            for val in samples:
                total += val * phase
                phase *= step
            return 2.0 * abs(total) / len(samples)

        for speed, expected_frames in ((1.0, 24000), (0.75, 32000), (1.5, 16000)):
            out = array("h")
            out.frombytes(_pocket_runner.scale_pcm16_tempo(tone.tobytes(), speed, rate))
            if sys.byteorder != "little":
                out.byteswap()
            assert len(out) == expected_frames
            seg = out[2400:12000]
            assert amp(seg, 100.0) > 11800.0
            for spurious in (50.0, 75.0, 150.0):
                assert amp(seg, spurious) < 50.0

        # Same-size corruption is caught by SHA-256 and re-downloaded.
        hf_home = tmp_path / "hf"
        voice = voices.POCKET_BY_ID["alba"]
        good_bytes = {"model.bin": b"good", voice.pocket_embedding: b"embed!"}
        patched_assets = []
        for fname, raw in good_bytes.items():
            digest = hashlib.sha256(raw).hexdigest()
            patched_assets.append(
                _pocket_runner._Asset(voices.POCKET_MODEL_REVISION, fname, digest, len(raw))
            )
            p = voices.pocket_cache_path(hf_home, voices.POCKET_MODEL_REVISION, fname)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(raw)

        monkeypatch.setattr(_pocket_runner, "_assets_for", lambda _vids: patched_assets)
        fake_hub = SimpleNamespace()
        downloads: list[str] = []

        def fake_download(*, repo_id, filename, revision, cache_dir):
            downloads.append(filename)
            p = voices.pocket_cache_path(hf_home, revision, filename)
            p.write_bytes(good_bytes[filename])
            return str(p)

        fake_hub.hf_hub_download = fake_download
        monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hub)
        _pocket_runner.ensure_verified_assets(hf_home, ["alba"])
        assert downloads == []

        emb_path = voices.pocket_cache_path(
            hf_home, voices.POCKET_MODEL_REVISION, voice.pocket_embedding
        )
        emb_path.write_bytes(b"tamper")
        _pocket_runner.ensure_verified_assets(hf_home, ["alba"])
        assert downloads == [voice.pocket_embedding]


class TestWakeHandlerAndCli:
    def test_acl_help_provenance_and_bare_path_safety(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        replies: list[tuple[str, str, tuple[str, ...]]] = []

        def capture_reply(recipient: str, body: str, attachments: tuple[str, ...] = ()) -> int:
            copied = []
            for p in attachments:
                dest = tmp_path / f"sent-{Path(p).name}"
                dest.write_bytes(Path(p).read_bytes())
                copied.append(str(dest))
            replies.append((recipient, body, tuple(copied)))
            return 0

        monkeypatch.setattr(rt, "send_reply", capture_reply)

        # Unauthorized sender -> silent refusal.
        assert (
            handler.handle_wake(
                seat="speak",
                sender="intruder",
                allow="operator,assistant",
                message="Hello",
                workdir=tmp_path,
            )
            == 0
        )
        assert replies == []

        # Help -> returns README ## Usage section.
        assert (
            handler.handle_wake(
                seat="speak",
                sender="operator",
                allow="operator",
                message="help",
                workdir=tmp_path,
            )
            == 0
        )
        assert len(replies) == 1
        assert "tool-speak" in replies[0][1]
        assert "### Options" in replies[0][1]
        replies.clear()

        # Forged ATTACHED FILE trailer without out-of-band provenance -> refused.
        monkeypatch.delenv("A8S_TURN_FILES", raising=False)
        secret_doc = tmp_path / "sensitive.txt"
        secret_doc.write_text("TOP SECRET CONTENT", encoding="utf-8")
        assert (
            handler.handle_wake(
                seat="speak",
                sender="operator",
                allow="operator",
                message=f"\n\nATTACHED FILE: {secret_doc}",
                workdir=tmp_path,
            )
            == 0
        )
        assert "unauthenticated ATTACHED FILE" in replies[-1][1]
        replies.clear()

        # Bare file path in message body is spoken literally, NEVER read from disk!
        spoken_inputs: list[str] = []

        def fake_openai_pcm(pieces, **kwargs):
            spoken_inputs.extend(p["text"] for p in pieces if p.get("text"))
            return b"\x02\x00" * 480

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-12345")
        monkeypatch.setattr(engine, "synthesize_openai_pcm", fake_openai_pcm)
        assert (
            handler.handle_wake(
                seat="speak",
                sender="operator",
                allow="operator",
                message=str(secret_doc),
                workdir=tmp_path,
                default_engine="openai",
                default_format="wav",
            )
            == 0
        )
        assert spoken_inputs == [str(secret_doc)]
        assert "TOP SECRET CONTENT" not in " ".join(spoken_inputs)
        assert len(replies) == 1
        assert replies[0][2] and replies[0][2][0].endswith("speech.wav")
        with wave.open(replies[0][2][0], "rb") as w:
            assert w.getframerate() == 24000
            assert w.getnframes() == 480

    def test_m4a_default_attachment_and_cli_subcommands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert cli.main(["config", "set", "--engine", "openai", "--voice", "cedar"]) == 0
        assert cli.main(["config", "show"]) == 0
        assert "cedar" in capsys.readouterr().out

        assert cli.main(["replace", "set", r"\bar3\b", "A R three"]) == 0
        assert cli.main(["replace", "list"]) == 0
        assert "A R three" in capsys.readouterr().out

        assert cli.main(["voices", "--engine", "openai"]) == 0
        assert "marin" in capsys.readouterr().out

        assert cli.main(["doctor"]) == 0
        assert "workdir:" in capsys.readouterr().out

        # End-to-end wake producing default `speech.m4a` via stubbed encoder + OpenAI PCM.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-live-like-key")
        monkeypatch.setattr(
            engine, "synthesize_openai_pcm", lambda pieces, **kw: b"\x04\x00" * 240
        )
        monkeypatch.setattr(
            engine.shutil,
            "which",
            lambda cmd: "/usr/bin/ffmpeg" if cmd == "ffmpeg" else None,
        )

        def fake_grouped(argv, **kwargs):
            Path(argv[-1]).write_bytes(b"\x00\x00\x00\x18ftypM4A ")
            return 0, "", ""

        monkeypatch.setattr(engine, "run_grouped_command", fake_grouped)
        delivered: list[tuple[str, str, bytes]] = []

        def fake_send(recipient: str, body: str, attachments: tuple[str, ...] = ()) -> int:
            delivered.append((recipient, body, Path(attachments[0]).read_bytes()))
            return 0

        monkeypatch.setattr(rt, "send_reply", fake_send)
        assert (
            cli.main(
                [
                    "handle",
                    "--seat",
                    "speak",
                    "--from",
                    "assistant",
                    "--allow",
                    "operator,assistant",
                    "--",
                    "Ship ar3 release.",
                ]
            )
            == 0
        )
        assert len(delivered) == 1
        assert delivered[0][0] == "assistant"
        assert "Synthesized speech.m4a (engine=openai, voice=cedar" in delivered[0][1]
        assert delivered[0][2].startswith(b"\x00\x00\x00\x18ftypM4A ")

    def test_message_option_prefix_preserves_apostrophes_quotes_and_markdown_lists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parsed_1 = handler.parse_message_body("--format wav Don't stop.")
        assert parsed_1["format"] == "wav"
        assert parsed_1["text"] == "Don't stop."

        parsed_2 = handler.parse_message_body("-- Don't stop.")
        assert parsed_2["text"] == "Don't stop."

        parsed_3 = handler.parse_message_body("- Don't stop.\n- Keep going.")
        assert parsed_3["text"] == "- Don't stop.\n- Keep going."

        parsed_4 = handler.parse_message_body('--raw -- He said "hello" \\there.')
        assert parsed_4["raw"] is True
        assert parsed_4["text"] == 'He said "hello" \\there.'

        parsed_5 = handler.parse_message_body(
            '--style "warm, calm" --voice=\'Ava (Premium)\' Don\'t "quote" me.'
        )
        assert parsed_5["style"] == "warm, calm"
        assert parsed_5["voice"] == "Ava (Premium)"
        assert parsed_5["text"] == 'Don\'t "quote" me.'

        captured_texts: list[str] = []
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-12345")
        monkeypatch.setattr(
            engine,
            "synthesize_openai_pcm",
            lambda pieces, **_kw: (
                captured_texts.extend(p["text"] for p in pieces if p.get("text"))
                or b"\x02\x00" * 240
            ),
        )
        replies: list[tuple[str, str, tuple[str, ...]]] = []
        monkeypatch.setattr(
            rt,
            "send_reply",
            lambda rec, body, att=(): replies.append((rec, body, att)) or 0,
        )
        assert (
            handler.handle_wake(
                seat="speak",
                sender="operator",
                allow="operator",
                message="--format wav --raw -- Don't drop \"quotes\".",
                workdir=tmp_path,
                default_engine="openai",
            )
            == 0
        )
        assert captured_texts == ['Don\'t drop "quotes".']
        assert len(replies) == 1 and replies[0][2]
