"""Pinned ACE-Step 1.5 source tree and model weights for tool-music.

Stdlib only: `_ace_runner.py` imports this module under `python -I -S`.
"""
from __future__ import annotations

from dataclasses import dataclass

ACE_SOURCE_COMMIT = "ca1e85fe9430179831e6bc6be790c332190a3866"
ACE_SOURCE_URL = f"https://codeload.github.com/ace-step/ACE-Step-1.5/zip/{ACE_SOURCE_COMMIT}"
# SHA-256 over sorted `<relpath>\0<sha256 hex>\n` lines for every file under `acestep/`.
# A tree digest instead of an archive digest: GitHub does not promise byte-stable zips.
ACE_SOURCE_TREE_SHA256 = "ebfeb0c01b5e495791c257ec0e00db6b7beb7d4ac68bbf14372cd43e37003036"
ACE_SOURCE_FILES = 616
ACE_SOURCE_MAX_BYTES = 64 * 1024 * 1024

MAIN_REPO = "ACE-Step/Ace-Step1.5"
MAIN_REVISION = "19671f406d603126926c1b7e2adc169acbcade22"
SFT_REPO = "ACE-Step/acestep-v15-sft"
SFT_REVISION = "c410d249e71ea9385a7b586865e65b1473e1098d"
XL_TURBO_REPO = "ACE-Step/acestep-v15-xl-turbo"
XL_TURBO_REVISION = "d4a0b288b83ebb7e25a8c0b32c573c22e134e8ee"

SILENCE_LATENT_SHA256 = "a778e9dd942f5e8b2c09c55370782d318834432b03dabbcdf70e6ed49ad6358b"
SILENCE_LATENT_BYTES = 3841215


@dataclass(frozen=True)
class Asset:
    repo: str
    revision: str
    remote: str
    local: str
    size: int
    sha256: str


def _main(path: str, size: int, sha256: str) -> Asset:
    return Asset(MAIN_REPO, MAIN_REVISION, path, path, size, sha256)


_QWEN = "Qwen3-Embedding-0.6B"
SHARED_ASSETS: tuple[Asset, ...] = (
    _main("config.json", 1968, "74745ff704ea49164c3d2d1c99fc0670f3fc635a869f0aec2d1311e6a52d400a"),
    _main("vae/config.json", 425, "14e019904df567f26df750317a70e2bd08f9f8f3c40ff4a24c97d1cd3f20ccd2"),
    _main(
        "vae/diffusion_pytorch_model.safetensors",
        337431388,
        "da17edb604c40deaf09e9b24974e590d1ca83a374070e5d0884cfa4bed9a99b0",
    ),
    _main(f"{_QWEN}/added_tokens.json", 707, "c0284b582e14987fbd3d5a2cb2bd139084371ed9acbae488829a1c900833c680"),
    _main(f"{_QWEN}/chat_template.jinja", 4116, "87a2728cb8dc9fe424d624542f6060ec05a1d285ebbec578bb078900e33396b5"),
    _main(f"{_QWEN}/config.json", 1359, "bb23c1607cfe059a58d8f0196cf1cebb52082b1056b8e358a579da80a5759420"),
    _main(f"{_QWEN}/merges.txt", 1671853, "8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5"),
    _main(
        f"{_QWEN}/model.safetensors",
        1191586416,
        "0437e45c94563b09e13cb7a64478fc406947a93cb34a7e05870fc8dcd48e23fd",
    ),
    _main(f"{_QWEN}/special_tokens_map.json", 613, "76862e765266b85aa9459767e33cbaf13970f327a0e88d1c65846c2ddd3a1ecd"),
    _main(f"{_QWEN}/tokenizer.json", 11423705, "def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a"),
    _main(f"{_QWEN}/tokenizer_config.json", 5404, "443bfa629eb16387a12edbf92a76f6a6f10b2af3b53d87ba1550adfcf45f7fa0"),
    _main(f"{_QWEN}/vocab.json", 2776833, "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910"),
)


# Turbo checkpoints are distilled for a timestep shift of 3.0; ACE-Step's own
# default (1.0) suits base and SFT checkpoints and is not corrected for turbo.
@dataclass(frozen=True)
class Model:
    name: str
    checkpoint: str
    default_steps: int
    shift: float
    assets: tuple[Asset, ...]

    @property
    def download_bytes(self) -> int:
        return sum(a.size for a in self.assets) + sum(a.size for a in SHARED_ASSETS)


def _dit(repo: str, revision: str, checkpoint: str, root_prefix: str, files: tuple[tuple[str, int, str], ...]) -> tuple[Asset, ...]:
    return tuple(
        Asset(repo, revision, f"{root_prefix}{name}", f"{checkpoint}/{name}", size, sha)
        for name, size, sha in files
    )


MODELS: dict[str, Model] = {
    "turbo": Model(
        "turbo",
        "acestep-v15-turbo",
        8,
        3.0,
        _dit(MAIN_REPO, MAIN_REVISION, "acestep-v15-turbo", "acestep-v15-turbo/", (
            ("config.json", 1968, "74745ff704ea49164c3d2d1c99fc0670f3fc635a869f0aec2d1311e6a52d400a"),
            ("model.safetensors", 4787825604, "3f6e0797fad420a39bd33979eb6e840e30989e34a3794e843d23b60ec6e422d7"),
            ("silence_latent.pt", SILENCE_LATENT_BYTES, SILENCE_LATENT_SHA256),
        )),
    ),
    "sft": Model(
        "sft",
        "acestep-v15-sft",
        50,
        1.0,
        _dit(SFT_REPO, SFT_REVISION, "acestep-v15-sft", "", (
            ("config.json", 1940, "9bb4f832f2e5e6c8bf7bddab3c6a6da1b13c01d072efbf0ed3c830536c473359"),
            ("model.safetensors", 4787825604, "d4dd3a93870f06720027965b90771f529ab02094b3d29e2518f1d5e097e1af7e"),
            ("silence_latent.pt", SILENCE_LATENT_BYTES, SILENCE_LATENT_SHA256),
        )),
    ),
    "xl-turbo": Model(
        "xl-turbo",
        "acestep-v15-xl-turbo",
        8,
        3.0,
        _dit(XL_TURBO_REPO, XL_TURBO_REVISION, "acestep-v15-xl-turbo", "", (
            ("config.json", 2407, "e3665376d46477cbd3e76203cd4e241f11a6a6bb5f88464c49f3c6aae51a918b"),
            ("model.safetensors.index.json", 71471, "0a0c7e153a7bb6f6061b155739e9502cf83977bb959186d4df688595302a9474"),
            ("model-00001-of-00004.safetensors", 4986971456, "e54a8413c981797312f53d512dec9944023d03f42f0740cbe86f62582cc27bdc"),
            ("model-00002-of-00004.safetensors", 4986942776, "b45f788b6dc9f15957cfa3d9b403f25f3f64c5211ecaf18dda45b67ea6470664"),
            ("model-00003-of-00004.safetensors", 4986942808, "a4f6bd572eab79cf5f3e4678796f904c6d6c33680a3e6ed09034d6de7cabea4d"),
            ("model-00004-of-00004.safetensors", 4988483464, "a868b05493c73808408f58b33a903371bb2846b53c8b19bd7476991bc8837259"),
            ("silence_latent.pt", SILENCE_LATENT_BYTES, SILENCE_LATENT_SHA256),
        )),
    ),
}
MODEL_NAMES = tuple(MODELS)
