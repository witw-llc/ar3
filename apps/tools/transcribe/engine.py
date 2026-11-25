"""Speech-to-text engines and video frame pipeline for tool-transcribe."""
from __future__ import annotations

import contextlib
import importlib.util
import json
import math
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from ar3 import deps as ar3_deps
from ar3.proc import launch_options, terminate_group

from .ollama import (
    DEFAULT_OLLAMA_URL,
    DEFAULT_VISION_MODEL,
    OllamaError,
    chat_with_images,
    ensure_vision_model,
    resolve_ollama_url,
    resolve_vision_model,
)
from .openai_stt import (
    DEFAULT_OPENAI_MODEL,
    KEY_ENV as OPENAI_KEY_ENV,
    MISSING_KEY_HINT,
    OpenAISTTError,
    api_key as openai_api_key,
    check_timed_model,
    require_api_key,
    transcribe_audio,
)
from .store import (
    apply_replacements,
    hints_path,
    read_hints,
    read_replacements,
    replacements_path,
    split_hints,
    warn_if_loop,
)

DEFAULT_MODEL = "turbo"
DEFAULT_FLAVOR = "auto"
DEFAULT_TIMEOUT = 1200.0
DEFAULT_PARAKEET_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"
PARAKEET_CHUNK_DURATION = 120.0
PARAKEET_OVERLAP_DURATION = 15.0
MAX_FRAMES = 50
MAX_FPS = 1.0
FRAME_SCALE = 512
OPENAI_UPLOAD_LIMIT = 24 * 1024 * 1024
OPENAI_SPLIT_MARGIN = 0.9
_OPENAI_AUDIO_ARGS = ("-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "32k")

_KNOWN_ENGINES = ("auto", "mlx-whisper", "faster-whisper", "parakeet-mlx", "whisper", "openai")
_KNOWN_FLAVORS = ("auto", "plain", "fancy")

_ENGINE_GROUPS = {
    "faster-whisper": ("transcribe-fast", "faster_whisper"),
    "mlx-whisper": ("transcribe-mlx", "mlx_whisper"),
    "parakeet-mlx": ("transcribe-mlx", "parakeet_mlx"),
    "whisper": ("transcribe-whisper", "whisper"),
}

_MLX_WHISPER_MODELS = {
    "tiny": "mlx-community/whisper-tiny",
    "base": "mlx-community/whisper-base",
    "small": "mlx-community/whisper-small",
    "medium": "mlx-community/whisper-medium",
    "large": "mlx-community/whisper-large-v3",
    "large-v2": "mlx-community/whisper-large-v2",
    "large-v3": "mlx-community/whisper-large-v3",
    "turbo": "mlx-community/whisper-large-v3-turbo",
}

_FANCY_SYSTEM = "You are a helpful video and audio analyst."

_FANCY_PROMPT = """\
Analyze this video from frames sampled at {fps} FPS together with the timed audio transcript below.

First write one short paragraph summarizing the video.

Next list each distinct scene in order with its timing. Describe what happens visually and match spoken lines or sound effects to speakers or events in each scene.

End with one short concluding summary paragraph.

{speech}
"""

_ACTIVATE_PREAMBLE = """\
import sys
_g = sys.argv[1]
if _g:
    while _g in sys.path:
        sys.path.remove(_g)
    for _i, _p in enumerate(sys.path):
        if "site-packages" in _p or "dist-packages" in _p:
            sys.path.insert(_i, _g)
            break
    else:
        sys.path.append(_g)
"""

_WHISPER_SNIPPET = _ACTIVATE_PREAMBLE + """\
import contextlib, io, json, whisper
audio, model_name, language, prompt, condition, timed = sys.argv[2:8]
print(f"loading whisper {model_name}...", file=sys.stderr)
model = whisper.load_model(model_name)
with contextlib.redirect_stdout(io.StringIO()):
    result = model.transcribe(
        audio,
        language=language or None,
        initial_prompt=prompt or None,
        condition_on_previous_text=condition == "1",
        fp16=False,
        verbose=False,
    )
if timed == "1":
    out = {
        "language": result.get("language") or "",
        "text": (result.get("text") or "").strip(),
        "segments": [
            {
                "start": float(seg.get("start") or 0),
                "end": float(seg.get("end") or 0),
                "text": (seg.get("text") or "").strip(),
            }
            for seg in (result.get("segments") or [])
        ],
    }
    print(json.dumps(out))
else:
    print((result.get("text") or "").strip())
"""

_MLX_WHISPER_SNIPPET = _ACTIVATE_PREAMBLE + """\
import json, mlx_whisper
audio, model_name, language, prompt, condition, timed = sys.argv[2:8]
print(f"loading mlx-whisper {model_name}...", file=sys.stderr)
kwargs = {
    "path_or_hf_repo": model_name,
    "initial_prompt": prompt or None,
    "condition_on_previous_text": condition == "1",
    "verbose": False,
}
if language:
    kwargs["language"] = language
result = mlx_whisper.transcribe(audio, **kwargs)
if timed == "1":
    out = {
        "language": result.get("language") or "",
        "text": (result.get("text") or "").strip(),
        "segments": [
            {
                "start": float(seg.get("start") or 0),
                "end": float(seg.get("end") or 0),
                "text": (seg.get("text") or "").strip(),
            }
            for seg in (result.get("segments") or [])
        ],
    }
    print(json.dumps(out))
else:
    print((result.get("text") or "").strip())
"""

_FASTER_WHISPER_SNIPPET = _ACTIVATE_PREAMBLE + """\
import json
try:
    import av, io
    try:
        av.open(io.BytesIO(b""), mode="r", metadata_errors="ignore")
    except TypeError:
        _orig_av_open = av.open
        av.open = lambda *a, **kw: _orig_av_open(
            *a, **{k: v for k, v in kw.items() if k != "metadata_errors"}
        )
    except Exception:
        pass
except ImportError:
    pass
from faster_whisper import WhisperModel
audio, model_name, language, prompt, condition, timed = sys.argv[2:8]
print(f"loading faster-whisper {model_name}...", file=sys.stderr)
model = WhisperModel(model_name, device="auto", compute_type="int8")
segments, info = model.transcribe(
    audio,
    language=language or None,
    initial_prompt=prompt or None,
    condition_on_previous_text=condition == "1",
)
seg_list = [
    {"start": float(seg.start), "end": float(seg.end), "text": seg.text.strip()}
    for seg in segments
]
full_text = " ".join(s["text"] for s in seg_list if s["text"]).strip()
if timed == "1":
    print(json.dumps({"language": info.language or "", "text": full_text, "segments": seg_list}))
else:
    print(full_text)
"""

_PARAKEET_SNIPPET = _ACTIVATE_PREAMBLE + """\
import json
from parakeet_mlx import from_pretrained
audio, model_name, timed, chunk_duration, overlap_duration = sys.argv[2:7]
print(f"loading parakeet {model_name}...", file=sys.stderr)
model = from_pretrained(model_name)
result = model.transcribe(
    audio,
    chunk_duration=float(chunk_duration),
    overlap_duration=float(overlap_duration),
)
if timed == "1":
    out = {
        "language": "",
        "text": (result.text or "").strip(),
        "segments": [
            {
                "start": float(s.start),
                "end": float(s.end),
                "text": (s.text or "").strip(),
            }
            for s in (result.sentences or [])
        ],
    }
    print(json.dumps(out))
else:
    print((result.text or "").strip())
"""


class TranscribeError(RuntimeError):
    """Raised when transcription setup or execution fails."""


def mlx_available() -> bool:
    """True on Apple Silicon macOS."""
    if platform.system() != "Darwin":
        return False
    if platform.machine().lower() == "arm64":
        return True
    try:
        proc = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True,
            text=True,
            timeout=5,
            **launch_options(),
        )
        return proc.returncode == 0 and "Apple" in proc.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def resolve_engine(requested: str | None) -> str:
    raw = (requested or "auto").strip().lower() or "auto"
    if raw not in _KNOWN_ENGINES:
        known = ", ".join(_KNOWN_ENGINES)
        raise TranscribeError(f"unknown engine {raw!r} (choose from: {known})")
    if raw != "auto":
        return raw
    return "mlx-whisper" if mlx_available() else "faster-whisper"


def resolve_model(engine: str, requested: str | None) -> str:
    model = (requested or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    if engine == "mlx-whisper":
        if "/" in model:
            return model
        mapped = _MLX_WHISPER_MODELS.get(model)
        if mapped:
            return mapped
        known = ", ".join(sorted(_MLX_WHISPER_MODELS))
        raise TranscribeError(
            f"unknown mlx-whisper model {model!r} (use one of: {known}, or org/repo)"
        )
    if engine == "parakeet-mlx":
        if model in _MLX_WHISPER_MODELS:
            return DEFAULT_PARAKEET_MODEL
        return model
    if engine == "faster-whisper":
        if "/" in model:
            return model
        if model == "turbo":
            return "large-v3-turbo"
        return model
    if engine == "openai":
        if model in _MLX_WHISPER_MODELS or model == "large-v3-turbo":
            return DEFAULT_OPENAI_MODEL
        return model
    return model


def resolve_flavor_choice(requested: str | None) -> str:
    raw = (requested or DEFAULT_FLAVOR).strip().lower() or DEFAULT_FLAVOR
    if raw not in _KNOWN_FLAVORS:
        known = ", ".join(_KNOWN_FLAVORS)
        raise TranscribeError(f"unknown flavor {raw!r} (choose from: {known})")
    return raw


def ensure_engine_group(engine: str) -> Path | None:
    """Resolve or install the on-demand `ar3.deps` group for `engine`."""
    if engine == "openai":
        return None
    if engine in ("mlx-whisper", "parakeet-mlx") and not mlx_available():
        raise TranscribeError(
            f"engine {engine!r} requires Apple Silicon macOS (use faster-whisper or auto)"
        )
    group_name, mod_name = _ENGINE_GROUPS[engine]
    existing = ar3_deps.ensure_group(group_name)
    if existing is not None:
        return existing
    if importlib.util.find_spec(mod_name) is not None:
        return None
    try:
        return ar3_deps.require_group(group_name, reason=f"tool-transcribe ({engine})")
    except Exception as exc:
        raise TranscribeError(f"cannot install {group_name} for {engine}: {exc}") from None


@contextlib.contextmanager
def _forward_stop_signals(get_proc):
    """Forward SIGTERM/SIGINT on the main thread to the active child group."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    saved: list[tuple[int, object]] = []

    def _handler(signum: int, _frame: object) -> None:
        proc = get_proc()
        if proc is not None:
            terminate_group(proc, grace_seconds=0.3)
        raise KeyboardInterrupt(f"stopped by signal {signum}")

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            prev = signal.signal(sig, _handler)
            saved.append((sig, prev))
        except (OSError, ValueError):
            pass
    try:
        yield
    finally:
        for sig, prev in saved:
            try:
                signal.signal(sig, prev)
            except (OSError, ValueError):
                pass


def run_grouped_command(
    argv: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run `argv` in its own process group with bounded pipes and timeout cleanup."""
    holder: list[subprocess.Popen | None] = [None]
    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []

    def _drain(stream, sink: list[bytes], mirror_stderr: bool = False) -> None:
        fd = stream.fileno()
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                return
            if not chunk:
                return
            sink.append(chunk)
            if mirror_stderr:
                try:
                    sys.stderr.write(chunk.decode("utf-8", errors="replace"))
                    sys.stderr.flush()
                except OSError:
                    pass

    with _forward_stop_signals(lambda: holder[0]):
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env or {**os.environ, "PYTHONUTF8": "1"},
                **launch_options(new_session=True),
            )
        except OSError as exc:
            raise TranscribeError(f"cannot start {argv[0]}: {exc}") from None
        holder[0] = proc
        assert proc.stdout is not None and proc.stderr is not None
        t_out = threading.Thread(target=_drain, args=(proc.stdout, out_chunks, False), daemon=True)
        t_err = threading.Thread(target=_drain, args=(proc.stderr, err_chunks, True), daemon=True)
        t_out.start()
        t_err.start()

        deadline = time.monotonic() + max(1.0, float(timeout))
        timed_out = False
        try:
            while proc.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    terminate_group(proc, grace_seconds=0.5)
                    break
                time.sleep(min(0.1, remaining))
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                terminate_group(proc, grace_seconds=0.1)
        except KeyboardInterrupt as exc:
            terminate_group(proc, grace_seconds=0.3)
            raise TranscribeError("transcription interrupted") from exc
        finally:
            terminate_group(proc, grace_seconds=0.1)
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
            t_out.join(timeout=1.0)
            t_err.join(timeout=1.0)

    if timed_out:
        raise TranscribeError(f"transcription timed out after {timeout:.0f}s")

    stdout = b"".join(out_chunks).decode("utf-8", errors="replace")
    stderr = b"".join(err_chunks).decode("utf-8", errors="replace")
    return proc.returncode or 0, stdout, stderr


def has_video_stream(path: Path) -> bool:
    if shutil.which("ffprobe") is None:
        return False
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                str(path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            **launch_options(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.strip() == "video" for line in proc.stdout.splitlines())


def probe_duration_seconds(path: Path, *, timeout: float = 30.0) -> float | None:
    if shutil.which("ffprobe") is None:
        return None
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(0.1, float(timeout)),
            **launch_options(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    try:
        duration = float(raw)
    except ValueError:
        return None
    return duration if duration > 0 else None


def calc_fps(path: Path, max_frames: int = MAX_FRAMES, max_fps: float = MAX_FPS) -> float:
    duration = probe_duration_seconds(path)
    if duration is None:
        return max_fps
    fps = max_frames / duration
    return max_fps if fps > max_fps else round(max(0.01, fps), 2)


def extract_frames(
    path: Path,
    frames_dir: Path,
    fps: float,
    *,
    scale: int = FRAME_SCALE,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[Path]:
    if shutil.which("ffmpeg") is None:
        raise TranscribeError("ffmpeg not found on PATH; install ffmpeg to extract video frames")
    frames_dir.mkdir(parents=True, exist_ok=True)
    pattern = frames_dir / "%04d.png"
    rc, out, err = run_grouped_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(path),
            "-vf",
            f"fps={fps},scale='min({scale},iw):-2'",
            str(pattern),
        ],
        timeout=timeout,
    )
    if rc != 0:
        detail = (err or out).strip()
        raise TranscribeError(detail or "ffmpeg frame extraction failed")
    frames = sorted(frames_dir.glob("*.png"))
    if not frames:
        raise TranscribeError("no frames extracted from video")
    return frames


def parse_timed_stdout(stdout: str) -> dict:
    text = (stdout or "").strip()
    if not text:
        raise TranscribeError("empty timed transcript output")
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            payload = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    raise TranscribeError("timed transcript output was not valid JSON")


def format_timed_speech(payload: dict) -> str:
    language = str(payload.get("language") or "").strip()
    text = str(payload.get("text") or "").strip()
    lines = [
        f"Language: {language}" if language else "Language: unknown",
        f"Full Speech Transcript: {text}",
        "",
    ]
    for seg in payload.get("segments") or ():
        start = round(float(seg.get("start") or 0.0), 2)
        end = round(float(seg.get("end") or 0.0), 2)
        seg_text = str(seg.get("text") or "").strip()
        lines.append(f"[Speech Transcript from {start} to {end} seconds]")
        lines.append(seg_text)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def run_stt_subprocess(
    path: Path,
    *,
    engine: str,
    model: str,
    group_dir: Path | None,
    language: str | None = None,
    prompt: str = "",
    timed: bool = False,
    condition_on_previous: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    if engine == "openai":
        return run_openai_stt(
            path,
            model=model,
            language=language,
            prompt=prompt,
            timed=timed,
            timeout=timeout,
        )
    group_arg = str(group_dir) if group_dir is not None else ""
    timed_flag = "1" if timed else "0"
    if engine == "parakeet-mlx":
        argv = [
            sys.executable,
            "-c",
            _PARAKEET_SNIPPET,
            group_arg,
            str(path),
            model,
            timed_flag,
            str(PARAKEET_CHUNK_DURATION),
            str(PARAKEET_OVERLAP_DURATION),
        ]
    else:
        snippet = {
            "mlx-whisper": _MLX_WHISPER_SNIPPET,
            "faster-whisper": _FASTER_WHISPER_SNIPPET,
            "whisper": _WHISPER_SNIPPET,
        }[engine]
        argv = [
            sys.executable,
            "-c",
            snippet,
            group_arg,
            str(path),
            model,
            language or "",
            prompt,
            "1" if condition_on_previous else "0",
            timed_flag,
        ]
    rc, stdout, stderr = run_grouped_command(argv, timeout=timeout)
    if rc != 0:
        tail = "\n".join((stderr or stdout).strip().splitlines()[-5:]).strip()
        raise TranscribeError(tail or f"{engine} exited with code {rc}")
    return stdout


def _openai_transcode(
    src: Path,
    dst: Path,
    *,
    timeout: float,
    start: float | None = None,
    duration: float | None = None,
) -> None:
    argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if start is not None:
        argv += ["-ss", f"{start:.3f}"]
    if duration is not None:
        argv += ["-t", f"{duration:.3f}"]
    argv += ["-i", str(src), *_OPENAI_AUDIO_ARGS, str(dst)]
    rc, out, err = run_grouped_command(argv, timeout=timeout)
    if rc != 0 or not dst.is_file():
        detail = (err or out).strip()
        raise TranscribeError(detail or f"ffmpeg audio transcode failed (exit {rc})")


def _openai_chunks(audio: Path, tmp: Path, remaining) -> list[tuple[float, Path]]:
    """Split `audio` into time chunks under the upload limit; return `(start_seconds, path)` pairs."""
    size = audio.stat().st_size
    if size <= OPENAI_UPLOAD_LIMIT:
        return [(0.0, audio)]
    duration = probe_duration_seconds(audio, timeout=min(30.0, remaining()))
    remaining()
    if duration is None:
        raise TranscribeError(
            "audio exceeds the openai upload limit and ffprobe cannot read its duration to split it"
        )
    count = math.ceil(size / (OPENAI_UPLOAD_LIMIT * OPENAI_SPLIT_MARGIN))
    span = duration / count
    chunks: list[tuple[float, Path]] = []
    for idx in range(count):
        start = idx * span
        part = tmp / f"chunk-{idx:03d}.mp3"
        _openai_transcode(
            audio,
            part,
            timeout=remaining(),
            start=start,
            duration=span if idx < count - 1 else None,
        )
        if part.stat().st_size > OPENAI_UPLOAD_LIMIT:
            raise TranscribeError(f"audio chunk {idx + 1} still exceeds the openai upload limit")
        chunks.append((start, part))
    return chunks


def run_openai_stt(
    path: Path,
    *,
    model: str,
    language: str | None = None,
    prompt: str = "",
    timed: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Transcode, split if needed, and transcribe `path` with the OpenAI API.

    Returns the same stdout shape as the local engine subprocesses: plain text,
    or a `{language, text, segments}` JSON line when `timed`.
    """
    deadline = time.monotonic() + max(1.0, float(timeout))

    def remaining() -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TranscribeError(f"transcription timed out after {timeout:.0f}s")
        return left

    results: list[tuple[float, dict]] = []
    try:
        require_api_key()
        if timed:
            check_timed_model(model)
        with tempfile.TemporaryDirectory(prefix="ar3-transcribe-openai-") as tmp_dir:
            tmp = Path(tmp_dir)
            audio = tmp / "audio.mp3"
            _openai_transcode(Path(path), audio, timeout=remaining())
            for start, part in _openai_chunks(audio, tmp, remaining):
                res = transcribe_audio(
                    part,
                    model=model,
                    language=language,
                    prompt=prompt,
                    timed=timed,
                    timeout=remaining(),
                )
                results.append((start, res))
                remaining()
    except OpenAISTTError as exc:
        raise TranscribeError(str(exc)) from None

    text = " ".join(res["text"] for _start, res in results if res["text"]).strip()
    if not timed:
        return text
    detected = next((res["language"] for _start, res in results if res["language"]), "")
    segments = [
        {
            "start": round(seg["start"] + start, 3),
            "end": round(seg["end"] + start, 3),
            "text": seg["text"],
        }
        for start, res in results
        for seg in res["segments"]
    ]
    return json.dumps({"language": detected, "text": text, "segments": segments})


def transcribe_file(
    path: Path,
    *,
    workdir: Path,
    engine: str | None = None,
    model: str | None = None,
    language: str | None = None,
    flavor: str | None = None,
    vision_model: str | None = None,
    ollama_url: str | None = None,
    extra_hints: list[str] | tuple[str, ...] = (),
    extra_replacements: list[tuple[str, str]] | tuple[tuple[str, str], ...] = (),
    condition_on_previous: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """Transcribe one audio or video file and return structured output."""
    target = Path(path)
    if not target.is_file():
        raise TranscribeError(f"file not found: {target}")

    engine_name = resolve_engine(engine)
    model_name = resolve_model(engine_name, model)
    flavor_choice = resolve_flavor_choice(flavor)

    if engine_name == "openai":
        try:
            require_api_key()
        except OpenAISTTError as exc:
            raise TranscribeError(str(exc)) from None

    if engine_name != "faster-whisper" and shutil.which("ffmpeg") is None:
        raise TranscribeError(
            f"ffmpeg not found on PATH (required by {engine_name}); install ffmpeg or use --engine faster-whisper"
        )

    if flavor_choice == "fancy":
        if shutil.which("ffprobe") is None or shutil.which("ffmpeg") is None:
            raise TranscribeError(
                "--flavor fancy requires both ffmpeg and ffprobe on PATH"
            )
        if not has_video_stream(target):
            raise TranscribeError(
                f"{target.name} has no video stream (use --flavor plain or auto)"
            )
        effective_flavor = "fancy"
    elif flavor_choice == "plain":
        effective_flavor = "plain"
    else:
        effective_flavor = "fancy" if has_video_stream(target) else "plain"

    if engine_name == "openai" and effective_flavor == "fancy":
        try:
            check_timed_model(model_name)
        except OpenAISTTError as exc:
            raise TranscribeError(str(exc)) from None

    resolved_vision = resolve_vision_model(vision_model)
    resolved_ollama_url = resolve_ollama_url(ollama_url)
    if effective_flavor == "fancy":
        try:
            ensure_vision_model(
                resolved_vision,
                ollama_url=resolved_ollama_url,
                timeout=min(15.0, timeout),
            )
        except OllamaError as exc:
            raise TranscribeError(str(exc)) from None

    file_hints = read_hints(workdir)
    merged_hints_list = file_hints + split_hints(extra_hints)
    prompt = ", ".join(merged_hints_list)
    eff_language = (language or "").strip() or None
    eff_condition = bool(condition_on_previous)

    if engine_name == "parakeet-mlx":
        prompt = ""
        eff_language = None
        eff_condition = False

    pairs = read_replacements(workdir) + list(extra_replacements)
    group_dir = ensure_engine_group(engine_name)

    if effective_flavor == "plain":
        raw_out = run_stt_subprocess(
            target,
            engine=engine_name,
            model=model_name,
            group_dir=group_dir,
            language=eff_language,
            prompt=prompt,
            timed=False,
            condition_on_previous=eff_condition,
            timeout=timeout,
        )
        text, applied = apply_replacements(raw_out.strip(), pairs)
        run_len = warn_if_loop(text)
        return {
            "text": text,
            "engine": engine_name,
            "model": model_name,
            "flavor": "plain",
            "applied_replacements": applied,
            "loop_run": run_len,
        }

    fps = calc_fps(target)
    timed_out = run_stt_subprocess(
        target,
        engine=engine_name,
        model=model_name,
        group_dir=group_dir,
        language=eff_language,
        prompt=prompt,
        timed=True,
        condition_on_previous=eff_condition,
        timeout=timeout,
    )
    payload = parse_timed_stdout(timed_out)
    speech = format_timed_speech(payload)
    speech, applied = apply_replacements(speech, pairs)
    run_len = warn_if_loop(str(payload.get("text") or "").strip())
    if not speech.strip():
        speech = "No audio transcript available. Describe the visual scenes from the frames."

    user_prompt = _FANCY_PROMPT.format(fps=fps, speech=speech)
    with tempfile.TemporaryDirectory(prefix="ar3-transcribe-frames-") as tmp_dir:
        frames = extract_frames(
            target,
            Path(tmp_dir) / "frames",
            fps,
            timeout=timeout,
        )
        try:
            narrative = chat_with_images(
                resolved_vision,
                user_prompt,
                frames,
                system=_FANCY_SYSTEM,
                ollama_url=resolved_ollama_url,
                timeout=min(600.0, timeout),
            )
        except OllamaError as exc:
            raise TranscribeError(str(exc)) from None

    return {
        "text": narrative,
        "engine": engine_name,
        "model": model_name,
        "flavor": "fancy",
        "applied_replacements": applied,
        "loop_run": run_len,
    }


def doctor_report(
    workdir: Path,
    *,
    engine: str | None = None,
    vision_model: str | None = None,
    ollama_url: str | None = None,
) -> int:
    """Print local readiness checks without making network or model calls."""
    wd = Path(workdir).resolve()
    print(f"workdir: {wd}")
    try:
        chosen = resolve_engine(engine)
        print(f"  ok   engine: {chosen} (apple_silicon_mlx={'yes' if mlx_available() else 'no'})")
    except TranscribeError as exc:
        print(f"  WARN engine: {exc}")
        chosen = "faster-whisper"

    if chosen == "openai":
        print("  ok   deps: none (openai sends audio to the OpenAI API)")
        if openai_api_key():
            print(f"  ok   {OPENAI_KEY_ENV}: set")
        else:
            print(f"  WARN {MISSING_KEY_HINT}")
    else:
        group_name, mod_name = _ENGINE_GROUPS[chosen]
        group_ok = (
            ar3_deps.ensure_group(group_name) is not None
            or importlib.util.find_spec(mod_name) is not None
        )
        print(
            f"  {'ok  ' if group_ok else 'NOTE'} deps ({group_name}): "
            + ("installed" if group_ok else f"not installed yet — installs on first run or via `ar3 deps {group_name}`")
        )

    for binary, note in (
        ("ffmpeg", "required for non-faster-whisper engines and fancy video"),
        ("ffprobe", "required for video stream detection and fancy video"),
        ("ollama", f"optional for fancy video (url={resolve_ollama_url(ollama_url) or DEFAULT_OLLAMA_URL}, model={resolve_vision_model(vision_model) or DEFAULT_VISION_MODEL})"),
    ):
        found = shutil.which(binary)
        status = "ok  " if found else "NOTE"
        print(f"  {status} {binary}: {found or f'not found ({note})'}")

    h_path = hints_path(wd)
    r_path = replacements_path(wd)
    try:
        h_count = len(read_hints(wd))
        print(f"  ok   hints.txt: {h_count} hint(s) ({h_path})")
    except Exception as exc:
        print(f"  WARN hints.txt: {exc}")
    try:
        r_count = len(read_replacements(wd))
        print(f"  ok   replacements.txt: {r_count} rule(s) ({r_path})")
    except Exception as exc:
        print(f"  WARN replacements.txt: {exc}")
    return 0
