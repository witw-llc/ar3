"""Subprocess runner for ACE-Step 1.5 instrumental generation in tool-music.

tool-music starts this file with `python -I -S`, so no site-packages or
dist-packages directory is on `sys.path`: a host-wide package (an old `boto3`
that `accelerate` imports when present) cannot break the import chain. The
runner then appends only the `music-ace` dependency group, the verified
ACE-Step source tree, and this suite's `lib` and `apps/tools` directories.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile

_TOOLS_DIR = Path(__file__).resolve().parent.parent
_LIB_DIR = _TOOLS_DIR.parent.parent / "lib"
for _entry in (str(_LIB_DIR), str(_TOOLS_DIR)):
    if _entry not in sys.path:
        sys.path.append(_entry)

from ar3.locking import file_lock  # noqa: E402
from music.pins import (  # noqa: E402
    ACE_SOURCE_COMMIT,
    ACE_SOURCE_FILES,
    ACE_SOURCE_MAX_BYTES,
    ACE_SOURCE_TREE_SHA256,
    ACE_SOURCE_URL,
    MODELS,
    SHARED_ASSETS,
    Asset,
)

TREE_MARKER = ".tree-sha256"
VERIFIED_FILE = ".verified.json"
SILENCE_RMS = 0.003
SILENCE_WINDOW_SECONDS = 0.05
SILENCE_KEEP_SECONDS = 0.5
FADE_SECONDS = 0.5


def log(message: str) -> None:
    print(f"tool-music: {message}", file=sys.stderr, flush=True)


def tree_digest(entries: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for rel, data in sorted(entries):
        digest.update(rel.encode("utf-8") + b"\0" + hashlib.sha256(data).hexdigest().encode("ascii") + b"\n")
    return digest.hexdigest()


def source_entries(archive: bytes) -> list[tuple[str, bytes]]:
    """`(relpath, bytes)` for every file under `acestep/` in a GitHub source zip."""
    entries = []
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            _top, _sep, rel = info.filename.partition("/")
            if not rel.startswith("acestep/"):
                continue
            parts = Path(rel).parts
            if any(part in ("", ".", "..") for part in parts) or Path(rel).is_absolute():
                raise RuntimeError(f"unsafe path in ACE-Step archive: {info.filename!r}")
            entries.append((rel, zf.read(info)))
    return entries


def _download(url: str, limit: int) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError(f"{url} is larger than {limit} bytes")
    return data


def ensure_source(cache_root: Path) -> Path:
    """Return a directory holding the pinned, tree-verified `acestep/` package."""
    final = cache_root / f"ace-step-src-{ACE_SOURCE_COMMIT}"
    if (final / TREE_MARKER).is_file() and (final / TREE_MARKER).read_text(encoding="ascii").strip() == ACE_SOURCE_TREE_SHA256:
        return final
    log(f"downloading ACE-Step source {ACE_SOURCE_COMMIT[:12]} ...")
    entries = source_entries(_download(ACE_SOURCE_URL, ACE_SOURCE_MAX_BYTES))
    actual = tree_digest(entries)
    if actual != ACE_SOURCE_TREE_SHA256 or len(entries) != ACE_SOURCE_FILES:
        raise RuntimeError(
            f"ACE-Step source digest mismatch: got {actual} ({len(entries)} files), "
            f"expected {ACE_SOURCE_TREE_SHA256} ({ACE_SOURCE_FILES} files)"
        )
    staging = Path(tempfile.mkdtemp(prefix=".ace-step-src-", dir=cache_root))
    try:
        for rel, data in entries:
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        (staging / TREE_MARKER).write_text(ACE_SOURCE_TREE_SHA256 + "\n", encoding="ascii")
        if final.exists():
            shutil.rmtree(final)
        os.replace(staging, final)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return final


def _sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _load_verified(checkpoints: Path) -> dict:
    try:
        data = json.loads((checkpoints / VERIFIED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _stamp(path: Path, sha256: str) -> list:
    st = path.stat()
    return [st.st_size, st.st_mtime_ns, sha256]


def ensure_assets(checkpoints: Path, assets: tuple[Asset, ...]) -> None:
    """Download any missing asset and SHA-256-verify each one. A file whose
    size and mtime match its last verification is not hashed again."""
    checkpoints.mkdir(parents=True, exist_ok=True)
    verified = _load_verified(checkpoints)
    missing: list[Asset] = []
    for asset in assets:
        path = checkpoints / asset.local
        try:
            st = path.stat()
        except OSError:
            missing.append(asset)
            continue
        if verified.get(asset.local) == [st.st_size, st.st_mtime_ns, asset.sha256]:
            continue
        if st.st_size == asset.size and _sha256_file(path) == asset.sha256:
            verified[asset.local] = _stamp(path, asset.sha256)
            continue
        path.unlink(missing_ok=True)
        missing.append(asset)
    if missing:
        from huggingface_hub import hf_hub_download

        total = sum(a.size for a in missing)
        log(f"downloading {len(missing)} model file(s), {total / 1e9:.1f} GB ...")
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=checkpoints))
        try:
            for asset in missing:
                log(f"downloading {asset.repo}/{asset.remote}")
                fetched = Path(hf_hub_download(
                    repo_id=asset.repo,
                    filename=asset.remote,
                    revision=asset.revision,
                    local_dir=str(staging / asset.repo.replace("/", "--")),
                ))
                size = fetched.stat().st_size
                sha = _sha256_file(fetched)
                if size != asset.size or sha != asset.sha256:
                    raise RuntimeError(
                        f"checksum mismatch for {asset.repo}/{asset.remote}: got {sha} ({size}B), "
                        f"expected {asset.sha256} ({asset.size}B)"
                    )
                dest = checkpoints / asset.local
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(fetched, dest)
                verified[asset.local] = _stamp(dest, asset.sha256)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    tmp = checkpoints / f"{VERIFIED_FILE}.tmp"
    tmp.write_text(json.dumps(verified, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, checkpoints / VERIFIED_FILE)


def pick_device(requested: str) -> str:
    import torch

    if requested and requested != "auto":
        return requested
    if torch.cuda.is_available():
        return "cuda"
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"


def trim_trailing_silence(samples, rate: int):
    """Cut a trailing quiet stretch (windowed RMS below SILENCE_RMS) down to
    SILENCE_KEEP_SECONDS and fade the new end. ACE-Step often pads the
    requested length with a few seconds of near-silence."""
    import numpy as np

    window = max(1, int(SILENCE_WINDOW_SECONDS * rate))
    channels = samples if samples.ndim == 2 else samples[:, None]
    count = len(channels) // window
    if count == 0:
        return samples
    frames = channels[: count * window].reshape(count, window, channels.shape[1])
    rms = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1)).max(axis=1)
    loud = np.nonzero(rms >= SILENCE_RMS)[0]
    if loud.size == 0:
        return samples
    keep = min(len(samples), (int(loud[-1]) + 1) * window + int(SILENCE_KEEP_SECONDS * rate))
    if keep >= len(samples) - rate:
        return samples
    out = samples[:keep].copy()
    fade = min(len(out), int(FADE_SECONDS * rate))
    ramp = np.linspace(1.0, 0.0, fade, dtype=out.dtype)
    out[-fade:] = out[-fade:] * (ramp[:, None] if out.ndim == 2 else ramp)
    return out


def generate(request: dict, source_dir: Path, checkpoints: Path, out_path: Path, work: Path) -> dict:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["ACESTEP_CHECKPOINTS_DIR"] = str(checkpoints)
    os.environ["ACESTEP_PROJECT_ROOT"] = str(work)
    os.environ["ACESTEP_GENERATION_TIMEOUT"] = str(int(request.get("generation_timeout") or 600))
    import torch

    device = pick_device(request.get("device") or "auto")
    if device == "cpu":
        torch.set_num_threads(max(1, os.cpu_count() or 1))
    model = MODELS[request["model"]]

    from acestep.handler import AceStepHandler
    from acestep.inference import GenerationConfig, GenerationParams, generate_music

    # Assets are pinned and verified above. ACE-Step's own presence check also
    # demands the 5 Hz LM, which tool-music never loads, and would fetch it unpinned.
    AceStepHandler._ensure_models_present = lambda self, **_kw: None
    started = time.monotonic()
    handler = AceStepHandler()
    status, ok = handler.initialize_service(
        project_root=str(work),
        config_path=model.checkpoint,
        device=device,
    )
    if not ok:
        raise RuntimeError(f"ACE-Step did not initialize: {str(status).strip()[:500]}")
    load_seconds = time.monotonic() - started

    seed = request.get("seed")
    params = GenerationParams(
        caption=request["caption"],
        lyrics="[Instrumental]",
        instrumental=True,
        duration=float(request["duration"]),
        bpm=request.get("bpm"),
        keyscale=request.get("key") or "",
        inference_steps=int(request.get("steps") or model.default_steps),
        seed=-1 if seed is None else int(seed),
        shift=model.shift,
        thinking=False,
        use_cot_metas=False,
        use_cot_caption=False,
        use_cot_language=False,
    )
    config = GenerationConfig(
        batch_size=1,
        use_random_seed=seed is None,
        seeds=None if seed is None else [int(seed)],
        audio_format="wav",
    )
    gen_started = time.monotonic()
    result = generate_music(handler, None, params, config, save_dir=str(work / "out"))
    gen_seconds = time.monotonic() - gen_started
    if not result.success or not result.audios:
        raise RuntimeError(f"ACE-Step generation failed: {result.error or 'no audio'}")
    audio = result.audios[0]

    import soundfile as sf

    samples, rate = sf.read(audio["path"], always_2d=True, dtype="float32")
    samples = trim_trailing_silence(samples, rate)
    sf.write(str(out_path), samples, rate, format=request["format"].upper(), subtype="PCM_16")
    return {
        "device": device,
        "model": model.name,
        "seed": (audio.get("params") or {}).get("seed", seed),
        "sample_rate": rate,
        "channels": int(samples.shape[1]),
        "seconds": round(len(samples) / rate, 2),
        "load_seconds": round(load_seconds, 1),
        "generate_seconds": round(gen_seconds, 1),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="_ace_runner")
    parser.add_argument("--group", default="")
    parser.add_argument("--cache", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--out", default="")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args(argv)

    if args.group and args.group not in sys.path:
        sys.path.append(args.group)
    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    cache_root = Path(args.cache)
    cache_root.mkdir(parents=True, exist_ok=True)
    checkpoints = cache_root / "ace-step-1.5" / "checkpoints"
    model = MODELS[request["model"]]
    with file_lock(cache_root / ".music-cache.lock"):
        source_dir = ensure_source(cache_root)
        ensure_assets(checkpoints, SHARED_ASSETS + model.assets)
    if str(source_dir) not in sys.path:
        sys.path.append(str(source_dir))
    if args.prepare_only:
        meta = {"prepared": True, "model": model.name, "source": str(source_dir), "checkpoints": str(checkpoints)}
    else:
        with tempfile.TemporaryDirectory(prefix="tool-music-work-") as work:
            meta = generate(request, source_dir, checkpoints, Path(args.out), Path(work))
    Path(args.result).write_text(json.dumps(meta) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
