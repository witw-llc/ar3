"""Subprocess runner for Kyutai Pocket TTS (`pocket-tts==4.0.1`) in tool-speak."""
from __future__ import annotations

from array import array
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import wave

_TOOLS_DIR = Path(__file__).resolve().parent.parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
_LIB_DIR = _TOOLS_DIR.parent.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from speak.voices import (  # noqa: E402
    POCKET_BY_ID,
    POCKET_MODEL_BYTES,
    POCKET_MODEL_FILE,
    POCKET_MODEL_REPO,
    POCKET_MODEL_REVISION,
    POCKET_MODEL_SHA256,
    POCKET_TOKENIZER_BYTES,
    POCKET_TOKENIZER_FILE,
    POCKET_TOKENIZER_REVISION,
    POCKET_TOKENIZER_SHA256,
    chunk_text,
    pocket_cache_path,
)

SAMPLE_RATE = 24000
MAX_CHUNK_CHARS = 300
MIN_SPEED = 0.25
MAX_SPEED = 4.0


def _activate_group_dir(group_dir: str) -> None:
    if not group_dir:
        return
    while group_dir in sys.path:
        sys.path.remove(group_dir)
    for idx, entry in enumerate(sys.path):
        if "site-packages" in entry or "dist-packages" in entry:
            sys.path.insert(idx, group_dir)
            return
    sys.path.append(group_dir)


@dataclass(frozen=True)
class _Asset:
    revision: str
    name: str
    sha256: str
    size: int


def _assets_for(voice_ids: list[str]) -> list[_Asset]:
    out = [
        _Asset(POCKET_MODEL_REVISION, POCKET_MODEL_FILE, POCKET_MODEL_SHA256, POCKET_MODEL_BYTES),
        _Asset(
            POCKET_TOKENIZER_REVISION,
            POCKET_TOKENIZER_FILE,
            POCKET_TOKENIZER_SHA256,
            POCKET_TOKENIZER_BYTES,
        ),
    ]
    seen: set[str] = set()
    for vid in voice_ids:
        if vid in seen:
            continue
        seen.add(vid)
        voice = POCKET_BY_ID[vid]
        out.append(
            _Asset(
                POCKET_MODEL_REVISION,
                voice.pocket_embedding,
                voice.sha256,
                voice.size,
            )
        )
    return out


def _sha256_file(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def ensure_verified_assets(hf_home: Path, voice_ids: list[str] | str) -> None:
    """Ensure pinned Pocket TTS weights and voice embeddings match exact SHA-256."""
    ids = [voice_ids] if isinstance(voice_ids, str) else list(voice_ids)
    hub_dir = hf_home / "hub"
    assets = _assets_for(ids)

    missing: list[_Asset] = []
    for asset in assets:
        p = pocket_cache_path(hf_home, asset.revision, asset.name)
        try:
            st = p.stat()
        except OSError:
            missing.append(asset)
            continue
        if st.st_size != asset.size or _sha256_file(p) != asset.sha256:
            p.unlink(missing_ok=True)
            missing.append(asset)

    if not missing:
        return

    from huggingface_hub import hf_hub_download

    for asset in missing:
        print(f"downloading {asset.name} ({POCKET_MODEL_REPO})...", file=sys.stderr)
        downloaded = Path(
            hf_hub_download(
                repo_id=POCKET_MODEL_REPO,
                filename=asset.name,
                revision=asset.revision,
                cache_dir=str(hub_dir),
            )
        )
        actual_size = downloaded.stat().st_size
        actual_sha = _sha256_file(downloaded)
        if actual_size != asset.size or actual_sha != asset.sha256:
            downloaded.unlink(missing_ok=True)
            raise RuntimeError(
                f"checksum mismatch for {asset.name}: got {actual_sha} ({actual_size}B), "
                f"expected {asset.sha256} ({asset.size}B)"
            )


def load_pocket_model(hf_home: Path):
    os.environ["HF_HUB_OFFLINE"] = "1"
    import torch
    from pocket_tts import TTSModel

    threads = min(4, max(1, os.cpu_count() or 2))
    torch.set_num_threads(threads)
    return TTSModel.load_model()


def synth_chunk(model, state, text: str) -> bytes:
    import torch

    chunks = chunk_text(text, MAX_CHUNK_CHARS)
    if not chunks:
        return b""
    tensors = []
    with torch.inference_mode():
        for part in chunks:
            part_clean = part.strip()
            if not part_clean:
                continue
            max_tokens = min(500, max(64, int(len(part_clean) * 2.5)))
            chunk = model.generate_audio(
                state,
                part_clean,
                max_tokens=max_tokens,
                frames_after_eos=3,
                copy_state=True,
            )
            if chunk.numel() == 0 or not torch.isfinite(chunk).all():
                raise RuntimeError("Pocket TTS generated empty or non-finite audio")
            tensors.append(chunk.reshape(-1))
        if not tensors:
            raise RuntimeError("Pocket TTS produced no audio")
        audio = torch.cat(tensors).clamp(-1.0, 1.0)
    return (audio * 32767.0).round().to(torch.int16).numpy().astype("<i2").tobytes()


def _best_wsola_offset(
    samples: array,
    energy_prefix: list[int],
    ref_pos: int,
    ideal_pos: int,
    corr_len: int,
    delta: int,
    max_start: int,
) -> int:
    lo = max(0, ideal_pos - delta)
    hi = min(max_start, ideal_pos + delta)
    if lo >= hi:
        return max(0, min(max_start, ideal_pos))

    ref_energy = energy_prefix[ref_pos + corr_len] - energy_prefix[ref_pos]
    if ref_energy <= 0:
        return max(0, min(max_start, ideal_pos))

    def score_at(cand: int, stride: int) -> float:
        cand_energy = energy_prefix[cand + corr_len] - energy_prefix[cand]
        if cand_energy <= 0:
            return -2.0
        dot = 0
        for i in range(0, corr_len, stride):
            dot += samples[ref_pos + i] * samples[cand + i]
        norm = math.sqrt(float(ref_energy) * float(cand_energy))
        corr = (dot * stride) / norm if norm > 0.0 else 0.0
        return corr - 1e-4 * (abs(cand - ideal_pos) / max(1, delta))

    coarse_scores: list[tuple[float, int]] = []
    for cand in range(lo, hi + 1, 2):
        coarse_scores.append((score_at(cand, 4), cand))
    coarse_scores.sort(reverse=True)

    best_pos = max(0, min(max_start, ideal_pos))
    best_score = -1e9
    checked: set[int] = set()
    for _coarse_val, peak in coarse_scores[:4]:
        r_lo = max(lo, peak - 4)
        r_hi = min(hi, peak + 4)
        for cand in range(r_lo, r_hi + 1):
            if cand in checked:
                continue
            checked.add(cand)
            sc = score_at(cand, 1)
            if sc > best_score:
                best_score = sc
                best_pos = cand
    return best_pos


def scale_pcm16_tempo(
    pcm: bytes, speed: float, rate: int = SAMPLE_RATE
) -> bytes:
    """Pitch-preserving WSOLA time-scale modification for 16-bit LE mono PCM."""
    if abs(speed - 1.0) <= 1e-3 or len(pcm) < 4:
        return pcm
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if sys.byteorder != "little":
        samples.byteswap()
    n_in = len(samples)
    n_out = max(1, int(round(n_in / speed)))
    if n_out == n_in:
        return pcm

    frame_len = max(128, int(round(rate * 0.040)))
    if frame_len % 2:
        frame_len += 1
    hop_s = frame_len // 2
    delta = max(32, int(round(rate * 0.015)))

    if n_in < frame_len * 2 or n_out < frame_len:
        out_short = array(
            "h",
            (
                samples[min(n_in - 1, int(i * (n_in - 1) / max(1, n_out - 1)))]
                for i in range(n_out)
            ),
        )
        if sys.byteorder != "little":
            out_short.byteswap()
        return out_short.tobytes()

    energy_prefix = [0] * (n_in + 1)
    running = 0
    for idx, val in enumerate(samples):
        running += val * val
        energy_prefix[idx + 1] = running

    window = [
        0.5 * (1.0 - math.cos(2.0 * math.pi * k / frame_len))
        for k in range(frame_len)
    ]
    max_start = n_in - frame_len
    acc = [0.0] * (n_out + frame_len)
    norm = [0.0] * (n_out + frame_len)

    chosen_pos = 0
    out_pos = 0
    while out_pos < n_out:
        if out_pos == 0:
            chosen_pos = 0
        else:
            ideal_pos = int(round(out_pos * speed))
            ref_pos = min(max(0, chosen_pos + hop_s), max_start)
            chosen_pos = _best_wsola_offset(
                samples,
                energy_prefix,
                ref_pos=ref_pos,
                ideal_pos=ideal_pos,
                corr_len=hop_s,
                delta=delta,
                max_start=max_start,
            )
        for k in range(frame_len):
            w = window[k]
            acc[out_pos + k] += samples[chosen_pos + k] * w
            norm[out_pos + k] += w
        out_pos += hop_s

    out = array("h", [0] * n_out)
    for i in range(n_out):
        w = norm[i]
        val = acc[i] / w if w > 1e-6 else float(samples[min(n_in - 1, int(i * speed))])
        ival = int(round(val))
        out[i] = -32768 if ival < -32768 else (32767 if ival > 32767 else ival)
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


def _resolve_piece_speed(raw_speed: object, warned: set[float]) -> float:
    speed = float(raw_speed if raw_speed is not None else 1.0)
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError(f"invalid speed {raw_speed!r} (must be positive)")
    clamped = max(MIN_SPEED, min(MAX_SPEED, round(speed, 3)))
    if abs(clamped - speed) > 1e-3 and speed not in warned:
        warned.add(speed)
        print(
            f"tool-speak: clamping pocket speed {speed:g} to {clamped:g} "
            f"(supported: {MIN_SPEED:g}-{MAX_SPEED:g})",
            file=sys.stderr,
        )
    return clamped


def main(argv: list[str]) -> int:
    group_dir, manifest_path, out_path, default_voice_id = argv[:4]
    _activate_group_dir(group_dir)
    pieces = json.loads(Path(manifest_path).read_text(encoding="utf-8"))

    needed_voices: list[str] = [default_voice_id]
    for piece in pieces:
        pv = piece.get("voice")
        if pv and pv not in needed_voices:
            needed_voices.append(pv)
    for vid in needed_voices:
        if vid not in POCKET_BY_ID:
            print(f"tool-speak: unknown Pocket voice {vid!r}", file=sys.stderr)
            return 2

    hf_home = Path(
        os.environ.get("HF_HOME") or (Path.home() / ".cache" / "huggingface")
    ).expanduser()
    ensure_verified_assets(hf_home, needed_voices)
    model = load_pocket_model(hf_home)
    states: dict[str, object] = {}
    for vid in needed_voices:
        emb = pocket_cache_path(
            hf_home, POCKET_MODEL_REVISION, POCKET_BY_ID[vid].pocket_embedding
        )
        states[vid] = model.get_state_for_audio_prompt(emb)

    pcm_parts: list[bytes] = []
    warned_speeds: set[float] = set()
    for i, piece in enumerate(pieces):
        text = (piece.get("text") or "").strip()
        speed = _resolve_piece_speed(piece.get("speed", 1.0), warned_speeds)
        silence_after = float(piece.get("silence_after", 0.0))
        vid = piece.get("voice") or default_voice_id
        if text:
            raw_pcm = synth_chunk(model, states[vid], text)
            pcm_parts.append(scale_pcm16_tempo(raw_pcm, speed, SAMPLE_RATE))
            print(f"  piece {i + 1}", file=sys.stderr)
        if silence_after > 0:
            frames = max(0, int(round(silence_after * SAMPLE_RATE)))
            if frames:
                pcm_parts.append(b"\x00\x00" * frames)

    full_pcm = b"".join(pcm_parts)
    if not full_pcm:
        print("tool-speak: Pocket TTS produced no audio", file=sys.stderr)
        return 1
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(full_pcm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
