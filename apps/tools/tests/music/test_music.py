"""Unit, runner, and handler tests for tool-music. No network, no model."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import sys
import types
import zipfile

import pytest

from music import _ace_runner as runner
from music import cli, engine, handler, pins, store
from music.engine import MusicError
from music.handler import MusicMessageError, parse_message_body
from music.store import MusicConfigError


def _no_encoder(_name: str) -> None:
    return None


def _fake_which(name: str) -> str | None:
    return "/usr/bin/ffmpeg" if name == "ffmpeg" else None


class TestParse:
    def test_plain_description_is_verbatim(self) -> None:
        body = "Don't stop: a 'jazzy' tune\nwith \"brushes\""
        opts = parse_message_body(body)
        assert opts["text"] == body
        assert opts["duration"] is None

    @pytest.mark.parametrize(
        "body",
        ["--key F#m jazz trio, brushes", "--key=F#m jazz trio, brushes", "--key F#m\njazz trio, brushes"],
    )
    def test_sharp_key_is_not_a_comment(self, body: str) -> None:
        opts = parse_message_body(body)
        assert opts["key"] == "F# minor"
        assert opts["text"] == "jazz trio, brushes"

    def test_hash_in_description_survives(self) -> None:
        opts = parse_message_body("--bpm 90 lo-fi #2, C# bass")
        assert opts["bpm"] == 90
        assert opts["text"] == "lo-fi #2, C# bass"

    def test_leading_options_then_description(self) -> None:
        opts = parse_message_body('--duration 90 --bpm=120 --key "f# minor" --seed 7 dark techno, it\'s loud')
        assert opts["duration"] == 90.0
        assert opts["bpm"] == 120
        assert opts["key"] == "F# minor"
        assert opts["seed"] == 7
        assert opts["text"] == "dark techno, it's loud"

    def test_double_dash_allows_leading_dash_text(self) -> None:
        opts = parse_message_body("-d 30 --format flac -- -ambient drone\nsecond line")
        assert (opts["duration"], opts["format"]) == (30.0, "flac")
        assert opts["text"] == "-ambient drone\nsecond line"

    def test_options_alone_then_next_lines(self) -> None:
        opts = parse_message_body("--model sft\nslow piano ballad")
        assert opts["model"] == "sft"
        assert opts["text"] == "slow piano ballad"

    def test_markdown_list_item_is_text(self) -> None:
        assert parse_message_body("- piano\n- strings")["text"] == "- piano\n- strings"

    @pytest.mark.parametrize("body,needle", [
        ("--tempo 90 jazz", "unknown option"),
        ("--duration 5 jazz", "between 10 and 600"),
        ("--duration", "missing value"),
        ("--bpm fast jazz", "whole number"),
        ("--key H jazz", "invalid key"),
        ("--model huge jazz", "unknown model"),
        ("--format mp3 jazz", "unsupported format"),
        ('--key "C major jazz', "cannot parse"),
    ])
    def test_errors(self, body: str, needle: str) -> None:
        with pytest.raises(MusicMessageError, match=needle):
            parse_message_body(body)


class TestValidators:
    @pytest.mark.parametrize("raw,expected", [
        ("C", "C major"), ("am", "A minor"), ("Bb min", "Bb minor"), ("f#m", "F# minor"),
        ("E♭ major", "Eb major"), ("G Major", "G major"),
    ])
    def test_key(self, raw: str, expected: str) -> None:
        assert store.validate_key(raw) == expected

    def test_caption_limits(self) -> None:
        assert store.validate_caption("  warm   pads\n and bells ") == "warm pads and bells"
        with pytest.raises(MusicConfigError, match="describe"):
            store.validate_caption("   ")
        with pytest.raises(MusicConfigError, match="at most 512"):
            store.validate_caption("x" * 513)


class TestConfig:
    def test_crud(self, tmp_path: Path) -> None:
        assert store.read_config(tmp_path) == {}
        saved = store.update_config(tmp_path, {"duration": "90", "format": "FLAC", "model": None})
        assert saved == {"duration": 90.0, "format": "flac"}
        assert json.loads((tmp_path / "music.json").read_text()) == saved
        assert store.update_config(tmp_path, {}, ["format"]) == {"duration": 90.0}
        with pytest.raises(MusicConfigError, match="unknown config key"):
            store.update_config(tmp_path, {}, ["voice"])
        assert store.clear_config(tmp_path) is True
        assert store.clear_config(tmp_path) is False

    def test_invalid_file(self, tmp_path: Path) -> None:
        (tmp_path / "music.json").write_text('{"duration": 9999}')
        with pytest.raises(MusicConfigError, match="duration"):
            store.read_config(tmp_path)

    def test_cli_config_set_show(self, tmp_path: Path, monkeypatch, capsys) -> None:
        monkeypatch.chdir(tmp_path)
        assert cli.main(["--seat", "m1", "config", "set", "--model", "sft", "--steps", "40"]) == 0
        assert cli.main(["--seat", "m1", "config", "show"]) == 0
        out = capsys.readouterr().out
        assert '"model": "sft"' in out and '"steps": 40' in out
        assert cli.main(["--seat", "m1", "config", "set"]) == 2


class TestRequest:
    def test_precedence_and_model_defaults(self) -> None:
        opts = parse_message_body("--bpm 100 lush strings")
        req = handler.build_request(opts, {"model": "sft"}, {"model": "turbo", "duration": 45.0}, which=_no_encoder)
        assert req == {
            "engine": "ace", "model": "sft", "caption": "lush strings", "duration": 45.0,
            "steps": 50, "format": "wav", "bpm": 100, "key": None, "seed": None,
        }
        req = handler.build_request(parse_message_body("--steps 12 -d 20 x"), {}, {"steps": 30}, which=_fake_which)
        assert (req["steps"], req["duration"], req["format"], req["model"]) == (12, 20.0, "m4a", "turbo")

    def test_m4a_without_encoder(self) -> None:
        with pytest.raises(MusicError, match="afconvert or ffmpeg"):
            engine.resolve_format("m4a", _no_encoder)

    def test_turbo_models_use_shift_three(self) -> None:
        assert pins.MODELS["turbo"].shift == 3.0
        assert pins.MODELS["xl-turbo"].shift == 3.0
        assert pins.MODELS["sft"].shift == 1.0


class TestEngine:
    def test_cache_root_override(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv(engine.CACHE_ENV, raising=False)
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        assert engine.cache_root() == tmp_path / "xdg" / "ar3" / "music"
        monkeypatch.setenv(engine.CACHE_ENV, str(tmp_path / "drive"))
        assert engine.cache_root() == tmp_path / "drive"

    def test_runner_argv_isolated(self, tmp_path: Path) -> None:
        argv = engine.runner_argv(tmp_path / "g", tmp_path / "c", tmp_path / "r.json", tmp_path / "o.json", tmp_path / "x.wav")
        assert argv[:6] == [sys.executable, "-I", "-S", "-X", "utf8", str(engine.RUNNER)]
        assert argv[argv.index("--out") + 1] == str(tmp_path / "x.wav")
        prep = engine.runner_argv(None, tmp_path, tmp_path / "r", tmp_path / "o", None, prepare_only=True)
        assert "--prepare-only" in prep and "--out" not in prep
        assert prep[prep.index("--group") + 1] == ""

    def test_encode_argv(self, tmp_path: Path) -> None:
        wav, out = tmp_path / "a.wav", tmp_path / "a.m4a"
        assert engine.encode_m4a_argv(engine.m4a_encoder(_fake_which), wav, out)[-6:] == [
            str(wav), "-c:a", "aac", "-b:a", "192k", str(out)
        ]
        af = engine.m4a_encoder(lambda n: "/usr/bin/afconvert" if n == "afconvert" else None)
        assert engine.encode_m4a_argv(af, wav, out)[-2:] == [str(wav), str(out)]

    def test_run_ace_passes_request_and_reads_result(self, tmp_path: Path, monkeypatch) -> None:
        seen = {}
        monkeypatch.setattr(engine, "ensure_group", lambda: tmp_path / "group")
        monkeypatch.setenv(engine.CACHE_ENV, str(tmp_path / "cache"))

        def fake_run(argv, *, timeout, cwd=None):
            seen["argv"], seen["timeout"] = argv, timeout
            seen["request"] = json.loads(Path(argv[argv.index("--request") + 1]).read_text())
            Path(argv[argv.index("--result") + 1]).write_text(json.dumps({"seed": 5, "seconds": 28.0}))
            return 0, ""

        monkeypatch.setattr(engine, "run_logged", fake_run)
        meta = engine.run_ace({"model": "turbo", "caption": "x"}, tmp_path / "o.wav", timeout=77)
        assert meta == {"seed": 5, "seconds": 28.0}
        assert seen["request"] == {"model": "turbo", "caption": "x", "generation_timeout": 77}
        assert seen["timeout"] == 77
        assert seen["argv"][seen["argv"].index("--cache") + 1] == str(tmp_path / "cache")

    def test_run_ace_failure_tail(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(engine, "ensure_group", lambda: None)
        monkeypatch.setattr(engine, "run_logged", lambda argv, **kw: (1, "noise\nRuntimeError: checksum mismatch\n"))
        with pytest.raises(MusicError, match="checksum mismatch"):
            engine.run_ace({"model": "turbo"}, tmp_path / "o.wav")

    def test_run_logged_timeout_kills(self, tmp_path: Path) -> None:
        with pytest.raises(MusicError, match="timed out"):
            engine.run_logged([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
        code, tail = engine.run_logged([sys.executable, "-c", "import sys; sys.stderr.write('hi'); sys.exit(3)"], timeout=30)
        assert (code, tail) == (3, "hi")


def _zip(files: dict[str, bytes], top: str = "ACE-Step-1.5-abc") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{top}/README.md", b"readme")
        for rel, data in files.items():
            zf.writestr(f"{top}/{rel}", data)
    return buf.getvalue()


class TestRunnerSource:
    def test_tree_digest_matches_manual(self) -> None:
        entries = [("acestep/b.py", b"b"), ("acestep/a.py", b"a")]
        expected = hashlib.sha256()
        for rel, data in sorted(entries):
            expected.update(rel.encode() + b"\0" + hashlib.sha256(data).hexdigest().encode() + b"\n")
        assert runner.tree_digest(entries) == expected.hexdigest()

    def test_source_entries_filters_and_rejects_traversal(self) -> None:
        entries = runner.source_entries(_zip({"acestep/x.py": b"x", "other/y.py": b"y"}))
        assert entries == [("acestep/x.py", b"x")]
        with pytest.raises(RuntimeError, match="unsafe path"):
            runner.source_entries(_zip({"acestep/../../evil.py": b"e"}))

    def test_ensure_source_verifies_and_caches(self, tmp_path: Path, monkeypatch) -> None:
        files = {"acestep/__init__.py": b"", "acestep/handler.py": b"X = 1\n"}
        digest = runner.tree_digest(sorted(files.items()))
        calls = []
        monkeypatch.setattr(runner, "ACE_SOURCE_TREE_SHA256", digest)
        monkeypatch.setattr(runner, "ACE_SOURCE_FILES", 2)
        monkeypatch.setattr(runner, "_download", lambda url, limit: calls.append(url) or _zip(files))
        first = runner.ensure_source(tmp_path)
        assert (first / "acestep" / "handler.py").read_bytes() == b"X = 1\n"
        assert runner.ensure_source(tmp_path) == first
        assert len(calls) == 1

    def test_ensure_source_rejects_mismatch(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(runner, "_download", lambda url, limit: _zip({"acestep/x.py": b"tampered"}))
        with pytest.raises(RuntimeError, match="digest mismatch"):
            runner.ensure_source(tmp_path)
        assert not list(tmp_path.glob("ace-step-src-*"))


class TestRunnerAssets:
    def _asset(self, data: bytes, local: str = "m/w.bin") -> pins.Asset:
        return pins.Asset("org/repo", "rev", "w.bin", local, len(data), hashlib.sha256(data).hexdigest())

    def _fake_hub(self, monkeypatch, payloads: dict[str, bytes], calls: list) -> None:
        def hf_hub_download(*, repo_id, filename, revision, local_dir):
            calls.append((repo_id, filename, revision))
            dest = Path(local_dir) / filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(payloads[filename])
            return str(dest)

        monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(hf_hub_download=hf_hub_download))

    def test_downloads_verifies_and_skips_rehash(self, tmp_path: Path, monkeypatch) -> None:
        calls: list = []
        asset = self._asset(b"weights")
        self._fake_hub(monkeypatch, {"w.bin": b"weights"}, calls)
        runner.ensure_assets(tmp_path, (asset,))
        assert (tmp_path / "m" / "w.bin").read_bytes() == b"weights"
        assert calls == [("org/repo", "w.bin", "rev")]
        monkeypatch.setattr(runner, "_sha256_file", lambda p: pytest.fail("rehashed a stamped file"))
        runner.ensure_assets(tmp_path, (asset,))
        assert len(calls) == 1
        assert not list(tmp_path.glob(".staging-*"))

    def test_corrupt_local_file_is_replaced(self, tmp_path: Path, monkeypatch) -> None:
        calls: list = []
        asset = self._asset(b"weights")
        (tmp_path / "m").mkdir()
        (tmp_path / "m" / "w.bin").write_bytes(b"WEIGHTS")
        self._fake_hub(monkeypatch, {"w.bin": b"weights"}, calls)
        runner.ensure_assets(tmp_path, (asset,))
        assert (tmp_path / "m" / "w.bin").read_bytes() == b"weights"
        assert len(calls) == 1

    def test_download_mismatch_fails(self, tmp_path: Path, monkeypatch) -> None:
        self._fake_hub(monkeypatch, {"w.bin": b"evil"}, [])
        with pytest.raises(RuntimeError, match="checksum mismatch"):
            runner.ensure_assets(tmp_path, (self._asset(b"weights"),))
        assert not (tmp_path / "m" / "w.bin").exists()


class TestRunnerTrim:
    def test_trailing_silence_is_cut(self) -> None:
        np = pytest.importorskip("numpy")
        rate = 1000
        loud = np.full((5 * rate, 2), 0.2, dtype="float32")
        quiet = np.full((4 * rate, 2), 0.0005, dtype="float32")
        out = runner.trim_trailing_silence(np.concatenate([loud, quiet]), rate)
        assert len(out) == int(5.5 * rate)
        assert abs(float(out[-1, 0])) < 1e-6
        short_tail = np.concatenate([loud, quiet[: rate // 2]])
        assert len(runner.trim_trailing_silence(short_tail, rate)) == len(short_tail)

    def test_opposite_phase_stereo_is_not_silence(self) -> None:
        np = pytest.importorskip("numpy")
        rate = 1000
        t = np.arange(9 * rate) / rate
        tone = (0.2 * np.sin(2 * np.pi * 50 * t)).astype("float32")
        stereo = np.stack([tone, tone], axis=1)
        stereo[5 * rate:, 1] *= -1
        out = runner.trim_trailing_silence(stereo, rate)
        assert len(out) == len(stereo)


class TestHandler:
    def _wake(self, message: str, *, allow: str = "op", workdir: Path) -> int:
        return handler.handle_wake(seat="music", sender="op", allow=allow, message=message, workdir=workdir)

    def test_refuses_sender_outside_allow(self, tmp_path: Path, monkeypatch, capsys) -> None:
        monkeypatch.setenv("AR3_TOOL_REPLY_DIR", str(tmp_path / "reply"))
        assert self._wake("jazz", allow="someone-else", workdir=tmp_path) == 0
        assert "refused" in capsys.readouterr().err

    def test_help(self, tmp_path: Path, monkeypatch, capsys) -> None:
        monkeypatch.setenv("AR3_TOOL_REPLY_DIR", str(tmp_path / "reply"))
        assert self._wake("help", workdir=tmp_path) == 0
        assert "--duration" in capsys.readouterr().out

    def test_bad_option_replies_error(self, tmp_path: Path, monkeypatch, capsys) -> None:
        monkeypatch.setenv("AR3_TOOL_REPLY_DIR", str(tmp_path / "reply"))
        assert self._wake("--duration 2 jazz", workdir=tmp_path) == 1
        assert "between 10 and 600" in capsys.readouterr().out

    def test_success_attaches_audio(self, tmp_path: Path, monkeypatch, capsys) -> None:
        reply = tmp_path / "reply"
        monkeypatch.setenv("AR3_TOOL_REPLY_DIR", str(reply))
        monkeypatch.setattr(engine, "m4a_encoder", lambda which=None: None)
        seen = {}

        def fake_generate(request, out_dir, *, timeout):
            seen["request"], seen["timeout"] = request, timeout
            path = out_dir / f"music.{request['format']}"
            path.write_bytes(b"RIFF")
            return path, {"seed": 11, "seconds": 27.4, "model": "turbo", "device": "cpu"}

        monkeypatch.setattr(handler, "generate_file", fake_generate)
        code = handler.handle_wake(
            seat="music", sender="op", allow="op", workdir=tmp_path,
            message="--duration 30 --seed 11 lo-fi piano", defaults={"steps": "10"}, timeout_raw="900",
        )
        out = capsys.readouterr().out
        assert code == 0
        assert seen["request"]["caption"] == "lo-fi piano"
        assert (seen["request"]["steps"], seen["request"]["format"], seen["timeout"]) == (10, "wav", 900.0)
        assert "27s instrumental" in out and "seed=11" in out
        assert (reply / "music.wav").read_bytes() == b"RIFF"

    def test_generation_error_is_reported(self, tmp_path: Path, monkeypatch, capsys) -> None:
        monkeypatch.setenv("AR3_TOOL_REPLY_DIR", str(tmp_path / "reply"))
        monkeypatch.setattr(engine, "m4a_encoder", lambda which=None: None)

        def boom(request, out_dir, *, timeout):
            raise MusicError("ACE-Step did not initialize")

        monkeypatch.setattr(handler, "generate_file", boom)
        assert self._wake("ambient", workdir=tmp_path) == 1
        assert "did not initialize" in capsys.readouterr().out


def test_pins_are_consistent() -> None:
    for model in pins.MODELS.values():
        locals_ = [a.local for a in pins.SHARED_ASSETS + model.assets]
        assert len(locals_) == len(set(locals_))
        assert all(a.local.startswith(model.checkpoint + "/") for a in model.assets)
        assert all(len(a.sha256) == 64 and a.size > 0 for a in pins.SHARED_ASSETS + model.assets)
        assert not any(a.local.endswith(".py") for a in model.assets)
    assert pins.ACE_SOURCE_COMMIT in pins.ACE_SOURCE_URL
    assert os.path.basename(str(runner.__file__)) == "_ace_runner.py"
