"""ACE-Step engine plumbing for tool-music: dependency group, cache, runner subprocess, encoding."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable

from ar3 import deps as ar3_deps
from ar3.proc import launch_options, terminate_group

from .pins import ACE_SOURCE_COMMIT, MODELS, SHARED_ASSETS

GROUP = "music-ace"
RUNNER = Path(__file__).resolve().parent / "_ace_runner.py"
DEFAULT_TIMEOUT = 3000.0
STDERR_TAIL_BYTES = 64 * 1024
M4A_BITRATE = "192k"


class MusicError(RuntimeError):
    """Raised when generation, installation, or encoding fails."""


CACHE_ENV = "AR3_MUSIC_CACHE"


def cache_root() -> Path:
    """Shared model and source cache: `AR3_MUSIC_CACHE` when set (for example a
    mounted Drive folder on an ephemeral GPU VM), else `XDG_CACHE_HOME/ar3/music`,
    else `~/.cache/ar3/music`."""
    override = os.environ.get(CACHE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "ar3" / "music"


def m4a_encoder(which: Callable[[str], str | None] = shutil.which) -> list[str] | None:
    """The argv prefix that encodes `<in.wav> <out.m4a>`, or None without afconvert or ffmpeg."""
    afconvert = which("afconvert")
    if afconvert:
        return [afconvert, "-f", "m4af", "-d", "aac", "-b", "192000"]
    ffmpeg = which("ffmpeg")
    if ffmpeg:
        return [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i"]
    return None


def resolve_format(requested: str | None, which: Callable[[str], str | None] = shutil.which) -> str:
    if requested:
        if requested == "m4a" and m4a_encoder(which) is None:
            raise MusicError("m4a output needs afconvert or ffmpeg on PATH; use --format wav or flac")
        return requested
    return "m4a" if m4a_encoder(which) is not None else "wav"


def encode_m4a_argv(prefix: list[str], wav: Path, out: Path) -> list[str]:
    if Path(prefix[0]).name.startswith("afconvert"):
        return [*prefix, str(wav), str(out)]
    return [*prefix, str(wav), "-c:a", "aac", "-b:a", M4A_BITRATE, str(out)]


def ensure_group() -> Path | None:
    """The installed `music-ace` group directory, installing it on first use."""
    existing = ar3_deps.ensure_group(GROUP)
    if existing is not None:
        return existing
    try:
        return ar3_deps.require_group(GROUP, reason="tool-music (ace)")
    except Exception as exc:
        raise MusicError(f"cannot install {GROUP}: {exc}") from None


def runner_argv(
    group_dir: Path | None,
    cache: Path,
    request_path: Path,
    result_path: Path,
    out_path: Path | None,
    *,
    prepare_only: bool = False,
) -> list[str]:
    argv = [
        sys.executable,
        "-I",
        "-S",
        "-X",
        "utf8",
        str(RUNNER),
        "--group",
        str(group_dir) if group_dir is not None else "",
        "--cache",
        str(cache),
        "--request",
        str(request_path),
        "--result",
        str(result_path),
    ]
    if prepare_only:
        argv.append("--prepare-only")
    else:
        argv += ["--out", str(out_path)]
    return argv


def run_logged(argv: list[str], *, timeout: float, cwd: Path | None = None) -> tuple[int, str]:
    """Run `argv` in its own process group, mirror its stderr, and return
    `(exit code, stderr tail)`. Kills the group at `timeout` or on interrupt."""
    tail = bytearray()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
            env={**os.environ, "HF_HUB_DISABLE_TELEMETRY": "1"},
            **launch_options(new_session=True),
        )
    except OSError as exc:
        raise MusicError(f"cannot start {argv[0]}: {exc}") from None
    assert proc.stderr is not None

    def drain() -> None:
        fd = proc.stderr.fileno()
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                return
            if not chunk:
                return
            tail.extend(chunk)
            del tail[:-STDERR_TAIL_BYTES]
            try:
                sys.stderr.write(chunk.decode("utf-8", errors="replace"))
                sys.stderr.flush()
            except OSError:
                pass

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    deadline = time.monotonic() + max(1.0, timeout)
    try:
        while proc.poll() is None:
            if time.monotonic() >= deadline:
                terminate_group(proc, grace_seconds=2.0)
                raise MusicError(f"music generation timed out after {timeout:.0f}s")
            time.sleep(0.2)
    except KeyboardInterrupt:
        terminate_group(proc, grace_seconds=1.0)
        raise MusicError("music generation interrupted") from None
    finally:
        reader.join(timeout=2.0)
        proc.stderr.close()
    return proc.returncode or 0, tail.decode("utf-8", errors="replace")


def _failure(code: int, tail: str) -> MusicError:
    lines = [line for line in tail.strip().splitlines() if line.strip()]
    return MusicError("\n".join(lines[-5:]) or f"ACE-Step runner exited with code {code}")


def run_ace(
    request: dict,
    out_path: Path | None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    prepare_only: bool = False,
) -> dict:
    """Run one ACE-Step job (or only fetch its source and weights) and return the runner's result."""
    group_dir = ensure_group()
    cache = cache_root()
    with tempfile.TemporaryDirectory(prefix="tool-music-") as tmp:
        tmp_dir = Path(tmp)
        request_path = tmp_dir / "request.json"
        result_path = tmp_dir / "result.json"
        request_path.write_text(json.dumps({**request, "generation_timeout": int(timeout)}), encoding="utf-8")
        argv = runner_argv(group_dir, cache, request_path, result_path, out_path, prepare_only=prepare_only)
        code, tail = run_logged(argv, timeout=timeout, cwd=tmp_dir)
        if code != 0 or not result_path.is_file():
            raise _failure(code, tail)
        return json.loads(result_path.read_text(encoding="utf-8"))


def generate_file(request: dict, out_dir: Path, *, timeout: float = DEFAULT_TIMEOUT) -> tuple[Path, dict]:
    """Generate one instrumental track into `out_dir` and return `(path, metadata)`."""
    fmt = request["format"]
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = "music"
    if fmt == "m4a":
        encoder = m4a_encoder()
        if encoder is None:
            raise MusicError("m4a output needs afconvert or ffmpeg on PATH; use --format wav or flac")
        wav = out_dir / f"{stem}.wav"
        started = time.monotonic()
        meta = run_ace({**request, "format": "wav"}, wav, timeout=timeout)
        out = out_dir / f"{stem}.m4a"
        remaining = max(30.0, timeout - (time.monotonic() - started))
        code, tail = run_logged(encode_m4a_argv(encoder, wav, out), timeout=remaining)
        if code != 0 or not out.is_file():
            raise MusicError(tail.strip() or f"m4a encoding exited with code {code}")
        wav.unlink(missing_ok=True)
        return out, meta
    out = out_dir / f"{stem}.{fmt}"
    return out, run_ace(request, out, timeout=timeout)


def doctor_report(workdir: Path, *, model: str = "turbo") -> int:
    """Print readiness of the dependency group, encoder, source, and weights for `model`."""
    from .store import MusicConfigError, read_config

    ok = True
    print(f"python: {sys.executable} ({sys.version.split()[0]})")
    group = ar3_deps.ensure_group(GROUP)
    print(f"{GROUP}: {'installed at ' + str(group) if group else 'not installed (installs on first use)'}")
    encoder = m4a_encoder()
    print(f"m4a encoder: {encoder[0] if encoder else 'none (wav and flac only)'}")
    cache = cache_root()
    source = cache / f"ace-step-src-{ACE_SOURCE_COMMIT}" / ".tree-sha256"
    print(f"ACE-Step source {ACE_SOURCE_COMMIT[:12]}: {'verified' if source.is_file() else 'not fetched'}")
    spec = MODELS[model]
    checkpoints = cache / "ace-step-1.5" / "checkpoints"
    present = [a for a in SHARED_ASSETS + spec.assets if (checkpoints / a.local).is_file()]
    total = len(SHARED_ASSETS) + len(spec.assets)
    print(f"weights ({model}, {spec.download_bytes / 1e9:.1f} GB): {len(present)}/{total} files in {checkpoints}")
    try:
        cfg = read_config(workdir)
        print(f"music.json: {json.dumps(cfg, sort_keys=True) if cfg else 'defaults'}")
    except MusicConfigError as exc:
        ok = False
        print(f"music.json: {exc}")
    return 0 if ok else 1
