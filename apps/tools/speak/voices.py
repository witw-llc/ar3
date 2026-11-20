"""Voice catalogs, ordered fallback resolution, and sentence chunking for tool-speak."""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ar3.proc import launch_options

ENGINES = ("say", "pocket", "kokoro", "openai", "elevenlabs", "cartesia")
ENGINE_CHOICES = ("auto", *ENGINES)

VENDOR_SECRETS: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "cartesia": "CARTESIA_API_KEY",
}

OPEN_VOICE_ENGINES = frozenset({"openai", "elevenlabs", "cartesia"})

POCKET_MODEL_REPO = "kyutai/pocket-tts-without-voice-cloning"
POCKET_MODEL_REVISION = "a7056f505a4a8ca72d8b5fb1440071d6f1a35739"
POCKET_MODEL_FILE = "languages/english/model.safetensors"
POCKET_MODEL_SHA256 = "4c56cbd171c63e8a26ea61f5d48b8a0cd8f3c51f4652c0ab9a19bfcced8c931d"
POCKET_MODEL_BYTES = 219029196
POCKET_EMBEDDING_DIR = "languages/english/embeddings"
POCKET_TOKENIZER_REVISION = "00eac05ed3d16bdc3f6b5d598874019c34a89214"
POCKET_TOKENIZER_FILE = "languages/english/tokenizer.json"
POCKET_TOKENIZER_BYTES = 245020
POCKET_TOKENIZER_SHA256 = "f498428e1eafee50492f7be13dc9bfafcfc12e508cd0eb1b01c92ecd5d8c6687"

KOKORO_VOICE_ID_RE = re.compile(r"^[abdefhpijzm][fm]_[a-z0-9_]+$")
SAY_VOICE_LINE_RE = re.compile(
    r"^(.+?)\s+[a-z]{2,3}(?:[-_][A-Za-z0-9]+)+\s*(?:#.*)?$"
)
GENERIC_GENDERS = {"male": "male", "female": "female"}


@dataclass(frozen=True)
class Voice:
    id: str
    name: str
    gender: str | None = None
    aliases: tuple[str, ...] = ()
    sha256: str = ""
    size: int = 0

    @property
    def pocket_embedding(self) -> str:
        return f"{POCKET_EMBEDDING_DIR}/{self.id}.safetensors"


POCKET_CATALOG: tuple[Voice, ...] = (
    Voice("alba", "Alba", sha256="5850f417028fb2bf6de31e5e395e56ee7a21ac0e52ef31aebd4a6a2847d72e9c", size=6244152),
    Voice("anna", "Anna", gender="female", sha256="626a19a37e3cbf5cac1e6893748c3f0dd2081b26b1ce787ae0deb42cb1ddb7f6", size=7620408),
    Voice("azelma", "Azelma", sha256="3073cbb90cd659fbfe15aa38449ddd4588c6d6c1a1de64fa36422bbb95f96b4e", size=7178040),
    Voice("charles", "Charles", gender="male", sha256="007c66f069aa3a564139974e5d4e1e1902ae95d91e76963bee64f4309a75cf8f", size=6195000),
    Voice("eponine", "Eponine", gender="female", sha256="ea923ff050147966af1c7ebd0978fd16f099ead813dfcd1fd2f927d709307f50", size=6932280),
    Voice("eve", "Eve", sha256="4444e6c2671a068ce385bba95fa6dd079e6432e51979ad1bfccd48d618363412", size=6145848),
    Voice("fantine", "Fantine", gender="female", sha256="2c3cde0d2324a9bf4d77dc28f9bfb9fe78e8e1f8c29a412f035fb4855b09fda3", size=6440760),
    Voice("george", "George", sha256="566330f66e20f95efd4fbea1e399e26dd84cf8e3ab1606e0114604e473dbc9fd", size=5900088),
    Voice("jane", "Jane", sha256="f802b7b0ce91578dc5448e1cdf4827695e64a34cc6b2f95989dc335082522887", size=7325496),
    Voice("mary", "Mary", sha256="fc75f7c7cefa8c0b1957ac564037dfaac053dd067ceeffff27982f87314b5a35", size=5949240),
    Voice("michael", "Michael", sha256="9f6538650ed3b664ab847d05244f51f04db22ce7ea00e2b285f51d91d6308ce0", size=7128888),
    Voice("paul", "Paul", gender="male", sha256="940ebd4c051aec0a9f7802f5ba36f3265be68029f5ae01c4b1322d50cfb4e03d", size=6833976),
    Voice("vera", "Vera", gender="female", sha256="fe136ebb9c88b65803e73ab8fc04fcc7faaa5aa790559bdab3d2382826eea44a", size=6686520),
    Voice("javert", "Javert", sha256="2bfe465c053195e9650cbdee889de3c941971ced3f2c69f71ad3e91cde202948", size=6145848),
    Voice("marius", "Marius", sha256="05621a1fa88bfe110063dd6fd206d0262891e374a36a04edc309317e961f14c9", size=6244152),
    Voice("bill_boerst", "Bill Boerst", sha256="a4510681c5ca6ac978b9fee83fc8a0d6569f7627b380caab0c5d23c53e4c0607", size=6539064),
    Voice("caro_davy", "Caro Davy", sha256="90309a130521328aa79f33b5ccc6008ba26163f621967455c0ed5c7b9e2854fd", size=5064504),
    Voice("peter_yearsley", "Peter Yearsley", sha256="f0eb3f23bb8e2ce5203a27ee4369ebeba07eda564cecebd84ccbdb4d9507cbca", size=3639088),
    Voice("stuart_bell", "Stuart Bell", sha256="5f1131db588c711127179a65f9eaaeb23b1ff0f5d460352e066c447858abdeb8", size=5064504),
)

KOKORO_CATALOG: tuple[Voice, ...] = (
    Voice("af_heart", "Heart", gender="female"),
    Voice("af_alloy", "Alloy", gender="female"),
    Voice("af_aoede", "Aoede", gender="female"),
    Voice("af_bella", "Bella", gender="female"),
    Voice("af_jessica", "Jessica", gender="female"),
    Voice("af_kore", "Kore", gender="female"),
    Voice("af_nicole", "Nicole", gender="female"),
    Voice("af_nova", "Nova", gender="female"),
    Voice("af_river", "River", gender="female"),
    Voice("af_sarah", "Sarah", gender="female"),
    Voice("af_sky", "Sky", gender="female"),
    Voice("am_adam", "Adam", gender="male"),
    Voice("am_echo", "Echo", gender="male"),
    Voice("am_eric", "Eric", gender="male"),
    Voice("am_fenrir", "Fenrir", gender="male"),
    Voice("am_liam", "Liam", gender="male"),
    Voice("am_michael", "Michael", gender="male"),
    Voice("am_onyx", "Onyx", gender="male"),
    Voice("am_puck", "Puck", gender="male"),
    Voice("am_santa", "Santa", gender="male"),
    Voice("bf_alice", "Alice", gender="female"),
    Voice("bf_emma", "Emma", gender="female"),
    Voice("bf_isabella", "Isabella", gender="female"),
    Voice("bf_lily", "Lily", gender="female"),
    Voice("bm_daniel", "Daniel", gender="male"),
    Voice("bm_fable", "Fable", gender="male"),
    Voice("bm_george", "George", gender="male"),
    Voice("bm_lewis", "Lewis", gender="male"),
)

OPENAI_CATALOG: tuple[Voice, ...] = (
    Voice("marin", "Marin"),
    Voice("cedar", "Cedar"),
    Voice("coral", "Coral"),
    Voice("ash", "Ash"),
    Voice("alloy", "Alloy"),
    Voice("ballad", "Ballad"),
    Voice("echo", "Echo"),
    Voice("sage", "Sage"),
    Voice("shimmer", "Shimmer"),
    Voice("verse", "Verse"),
)

ELEVENLABS_CATALOG: tuple[Voice, ...] = (
    Voice("21m00Tcm4TlvDq8ikWAM", "Rachel", gender="female", aliases=("rachel",)),
    Voice("EXAVITQu4vr4xnSDxMaL", "Sarah", gender="female", aliases=("sarah",)),
    Voice("9BWtsMINqrJLrRacOk9x", "Aria", gender="female", aliases=("aria",)),
    Voice("cgSgspJ2msm6clMCkdW9", "Jessica", gender="female", aliases=("jessica",)),
    Voice("JBFqnCBsd6RMkjVDRZzb", "George", gender="male", aliases=("george",)),
    Voice("pNInz6obpgDQGcFmaJgB", "Adam", gender="male", aliases=("adam",)),
    Voice("nPczCjzI2devNBz1zQrb", "Brian", gender="male", aliases=("brian",)),
    Voice("onwK4e9ZLuTAKqWW03F9", "Daniel", gender="male", aliases=("daniel",)),
    Voice("cjVigY5qzO86Huf0OWal", " Eric".strip(), gender="male", aliases=("eric",)),
    Voice("TX3LPaxmHKxFdv7VOQHJ", "Liam", gender="male", aliases=("liam",)),
)

CARTESIA_CATALOG: tuple[Voice, ...] = (
    Voice("f786b574-daa5-4673-aa0c-cbe3e8534c02", "Katie", gender="female", aliases=("katie",)),
    Voice("694f9389-aac1-45b6-b726-9d9369183238", "Sarah", gender="female", aliases=("sarah",)),
    Voice("a0e99841-438c-4a64-b679-ae501e7d6091", "Barbershop Man", gender="male", aliases=("barbershop",)),
    Voice("228fca29-3a0a-435c-8728-5cb483251068", "Kiefer", gender="male", aliases=("kiefer",)),
)

SAY_BUILTIN_CATALOG: tuple[Voice, ...] = tuple(
    Voice(name, name)
    for name in (
        "Samantha",
        "Alex",
        "Daniel",
        "Karen",
        "Moira",
        "Rishi",
        "Tessa",
        "Veena",
        "Fred",
        "Victoria",
        "Albert",
        "Alice",
        "Alva",
        "Amelie",
        "Anna",
        "Carmit",
        "Damayanti",
        "Diego",
        "Eddy",
        "Ellen",
        "Fiona",
        "Flo",
        "Grandma",
        "Grandpa",
        "Ioana",
        "Joana",
        "Jorge",
        "Juan",
        "Kanya",
        "Kyoko",
        "Laura",
        "Lekha",
        "Luca",
        "Luciana",
        "Majed",
        "Mariska",
        "Mei-Jia",
        "Melina",
        "Milena",
        "Monica",
        "Nora",
        "Paulina",
        "Reed",
        "Rocko",
        "Sandy",
        "Satu",
        "Shelley",
        "Sinji",
        "Thomas",
        "Tingting",
        "Xander",
        "Yelda",
        "Yuna",
        "Zosia",
        "Zuzana",
    )
)

CATALOGS: dict[str, tuple[Voice, ...]] = {
    "pocket": POCKET_CATALOG,
    "kokoro": KOKORO_CATALOG,
    "openai": OPENAI_CATALOG,
    "elevenlabs": ELEVENLABS_CATALOG,
    "cartesia": CARTESIA_CATALOG,
}

DEFAULT_VOICES: dict[str, str] = {
    "kokoro": "af_heart",
    "pocket": "alba",
    "openai": "marin",
    "elevenlabs": "21m00Tcm4TlvDq8ikWAM",
    "cartesia": "f786b574-daa5-4673-aa0c-cbe3e8534c02",
}

POCKET_BY_ID: dict[str, Voice] = {v.id: v for v in POCKET_CATALOG}


def parse_say_voice_list(output: str) -> tuple[Voice, ...]:
    voices: list[Voice] = []
    seen: set[str] = set()
    for line in output.splitlines():
        m = SAY_VOICE_LINE_RE.match(line.rstrip())
        if not m:
            continue
        full_name = m.group(1).strip()
        if not full_name or full_name in seen:
            continue
        seen.add(full_name)
        base = full_name.split("(", 1)[0].strip()
        aliases = (base,) if base and base != full_name else ()
        voices.append(Voice(full_name, full_name, aliases=aliases))
    return tuple(voices)


def installed_say_voices(
    *,
    which: Callable[[str], str | None] | None = None,
) -> tuple[Voice, ...]:
    which_fn = which or shutil.which
    say_bin = which_fn("say")
    if not say_bin:
        return SAY_BUILTIN_CATALOG
    try:
        proc = subprocess.run(
            [say_bin, "-v", "?"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            **launch_options(),
        )
    except (OSError, subprocess.SubprocessError):
        return SAY_BUILTIN_CATALOG
    if proc.returncode != 0 or not proc.stdout.strip():
        return SAY_BUILTIN_CATALOG
    parsed = parse_say_voice_list(proc.stdout)
    return parsed if parsed else SAY_BUILTIN_CATALOG


def pocket_cache_path(hf_home: Path, revision: str, name: str) -> Path:
    repo_dir = "models--" + POCKET_MODEL_REPO.replace("/", "--")
    return Path(hf_home) / "hub" / repo_dir / "snapshots" / revision / name


def normal_voice_key(text: str) -> str:
    return " ".join(re.split(r"[\s_.\-]+", text.strip().lower())).strip()


def parse_voice_entry(entry: str) -> tuple[str | None, str]:
    text = entry.strip()
    if ":" in text:
        head, tail = text.split(":", 1)
        vendor = head.strip().lower()
        if vendor in ENGINES:
            return vendor, tail.strip()
    return None, text


def parse_voice_prefix(spec: str) -> str | None:
    parts = [e.strip() for e in (spec or "").split(",") if e.strip()]
    if len(parts) != 1 or ":" not in parts[0]:
        return None
    head = parts[0].split(":", 1)[0].strip().lower()
    return head if head in ENGINES else None


def has_vendor_qualified_entry(spec: str | None) -> bool:
    if not spec:
        return False
    return any(
        parse_voice_entry(part)[0] is not None
        for part in spec.split(",")
        if part.strip()
    )


def vendor_available(
    engine: str,
    *,
    secret_resolver: Callable[[str], str | None],
    which: Callable[[str], str | None] | None = None,
) -> tuple[bool, str]:
    which_fn = which or shutil.which
    if engine == "say":
        if which_fn("say"):
            return True, ""
        return False, "say not found"
    if engine in ("kokoro", "pocket"):
        return True, ""
    secret_name = VENDOR_SECRETS.get(engine)
    if secret_name:
        val = secret_resolver(secret_name)
        if val and val.strip():
            return True, ""
        return (
            False,
            f"{secret_name} is not set (set it with `a8s vars <node> set secret.{secret_name} -` "
            f"or `ar3 tool speak --node <node> secret set {secret_name} -`)",
        )
    return False, f"unknown engine {engine!r}"


def _voice_names(voice: Voice) -> list[str]:
    out: list[str] = []
    for raw in (voice.id, voice.name, *voice.aliases):
        norm = normal_voice_key(raw)
        if norm and norm not in out:
            out.append(norm)
    raw_id = voice.id.strip().lower()
    if raw_id and raw_id not in out:
        out.append(raw_id)
    return out


def match_catalog_voice(
    query: str, catalog: tuple[Voice, ...]
) -> tuple[Voice, str] | None:
    key = normal_voice_key(query)
    raw_key = query.strip().lower()
    if not key and not raw_key:
        return None
    if key in GENERIC_GENDERS:
        wanted = GENERIC_GENDERS[key]
        for voice in catalog:
            if voice.gender == wanted:
                return voice, "gender"
        return None
    for voice in catalog:
        if voice.id.lower() == raw_key:
            return voice, "exact"
    for how, pred in (
        ("exact", lambda n: n == key),
        ("prefix", lambda n: n.startswith(key)),
        ("contains", lambda n: key in n),
    ):
        for voice in catalog:
            if any(pred(n) for n in _voice_names(voice)):
                return voice, how
    return None


@dataclass(frozen=True)
class VoiceResolution:
    engine: str
    voice: str | None
    source: str
    how: str = ""
    matched_entry: str = ""
    missed: tuple[str, ...] = ()


def resolve_for_engine(
    spec: str | None,
    engine: str,
    source: str,
    *,
    allow_custom: bool | None = None,
    which: Callable[[str], str | None] | None = None,
    say_voices: tuple[Voice, ...] | None = None,
) -> VoiceResolution:
    default_id = DEFAULT_VOICES.get(engine)
    if spec is None or not spec.strip():
        default_src = "system default" if engine == "say" else "built-in default"
        return VoiceResolution(engine, default_id, default_src, "default")

    raw_entries = [e.strip() for e in spec.split(",") if e.strip()]
    if (
        engine == "kokoro"
        and raw_entries
        and all(KOKORO_VOICE_ID_RE.fullmatch(e) for e in raw_entries)
    ):
        joined = ",".join(raw_entries)
        how = "blend" if len(raw_entries) > 1 else "exact"
        return VoiceResolution(engine, joined, source, how, joined)

    if allow_custom is None:
        single_vendor = (
            parse_voice_entry(raw_entries[0])[0] if len(raw_entries) == 1 else None
        )
        allow_custom = len(raw_entries) == 1 and (
            source in ("cli", "message", "var") or single_vendor == engine
        )

    missed: list[str] = []
    catalog = (
        (say_voices if say_voices is not None else installed_say_voices(which=which))
        if engine == "say"
        else CATALOGS.get(engine, ())
    )

    for raw in raw_entries:
        entry_vendor, query = parse_voice_entry(raw)
        if entry_vendor is not None and entry_vendor != engine:
            missed.append(raw)
            continue
        if not query:
            return VoiceResolution(
                engine, default_id, source, "default", raw, tuple(missed)
            )
        if engine == "say" and KOKORO_VOICE_ID_RE.match(query):
            missed.append(raw)
            continue
        if engine == "kokoro" and KOKORO_VOICE_ID_RE.fullmatch(query):
            return VoiceResolution("kokoro", query, source, "exact", raw, tuple(missed))
        found = match_catalog_voice(query, catalog)
        if found is not None:
            return VoiceResolution(
                engine, found[0].id, source, found[1], raw, tuple(missed)
            )
        if (
            engine in OPEN_VOICE_ENGINES
            and allow_custom
            and normal_voice_key(query) not in GENERIC_GENDERS
            and not KOKORO_VOICE_ID_RE.match(query)
        ):
            return VoiceResolution(engine, query, source, "custom", raw, tuple(missed))
        missed.append(raw)

    fallback_src = "system default" if engine == "say" else "built-in default"
    return VoiceResolution(
        engine, default_id, fallback_src, "default", "", tuple(missed)
    )


def resolve_vendor_preference_list(
    spec: str,
    source: str,
    fallback_engine: str,
    *,
    secret_resolver: Callable[[str], str | None],
    allow_custom: bool | None = None,
    which: Callable[[str], str | None] | None = None,
    say_voices: tuple[Voice, ...] | None = None,
) -> VoiceResolution:
    raw_entries = [e.strip() for e in spec.split(",") if e.strip()]
    single_stack = len(raw_entries) == 1
    missed: list[str] = []
    for raw in raw_entries:
        entry_vendor, query = parse_voice_entry(raw)
        target_engine = entry_vendor or fallback_engine
        ok, _reason = vendor_available(
            target_engine, secret_resolver=secret_resolver, which=which
        )
        if not ok:
            missed.append(raw)
            continue
        entry_allow_custom = (
            allow_custom
            if allow_custom is not None
            else (single_stack and (source in ("cli", "message", "var") or entry_vendor == target_engine))
        )
        single = resolve_for_engine(
            f"{target_engine}:{query}" if entry_vendor else query,
            target_engine,
            source,
            allow_custom=entry_allow_custom,
            which=which,
            say_voices=say_voices,
        )
        if single.how != "default" or not query:
            return VoiceResolution(
                target_engine,
                single.voice,
                single.source,
                single.how,
                raw,
                tuple(missed),
            )
        missed.append(raw)
    fallback_default = DEFAULT_VOICES.get(fallback_engine)
    fallback_src = (
        "system default" if fallback_engine == "say" else "built-in default"
    )
    return VoiceResolution(
        fallback_engine, fallback_default, fallback_src, "default", "", tuple(missed)
    )


def chunk_text(text: str, limit: int = 300) -> list[str]:
    chunks: list[str] = []
    offset = 0
    n = len(text)
    while offset < n:
        start = offset
        while start < n and text[start].isspace():
            start += 1
        if start >= n:
            break
        end = min(n, start + limit)
        if end < n:
            passage = text[start:end]
            sentences = list(re.finditer(r"[.!?](?:[\"')\]]*)\s+", passage))
            words = list(re.finditer(r"\s+", passage))
            boundaries = sentences or words
            if boundaries:
                end = start + boundaries[-1].end()
        if not text[end:].strip():
            end = n
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        offset = max(end, start + 1)
    return chunks
