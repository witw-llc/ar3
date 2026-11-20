"""Markdown segmentation, emphasis cues, and `<!-- speak: ... -->` directives for tool-speak."""
from __future__ import annotations

import math
import re
import shlex
from dataclasses import dataclass
from typing import Iterator, Literal

Style = Literal["plain", "strong", "code"]
BlockKind = Literal["heading", "paragraph", "list_item"]

DEFAULT_PAUSE_MAJOR = 2.0
DEFAULT_PAUSE_MINOR = 1.0
DEFAULT_PAUSE_PARA = 0.4
LIST_PAUSE_CAP = 0.25

_MD_HINT_RE = re.compile(
    r"(?m)"
    r"^(#{1,6}\s|\s{0,3}([-*+]|\d+\.)\s|>\s|```|\||<!--\s*speak:)"
    r"|(\*\*[^*\n]+\*\*|__[^_\n]+__|`[^`\n]+`|<!--\s*speak:)"
)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_RE = re.compile(r"^(\s{0,3})([-*+]|\d+\.)\s+(.*)$")
_HR_RE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MISAKI_RE = re.compile(r"\[[^\]]+\]\(/[^/]+/\)")
_DIRECTIVE_RE = re.compile(r"^<!--\s*speak:(.*?)-->\s*$", re.I)
_INLINE_HTML_COMMENT_RE = re.compile(r"<!--.*?-->")


@dataclass(frozen=True)
class SpeakSpan:
    text: str
    style: Style = "plain"


@dataclass(frozen=True)
class SpeakBlock:
    kind: BlockKind
    level: int
    spans: tuple[SpeakSpan, ...]
    pause_before: float = 0.0
    voice: str | None = None
    delivery_style: str | None = None
    speed_override: float | None = None


@dataclass(frozen=True)
class SpeakPiece:
    text: str
    speed: float
    silence_after: float = 0.0
    voice: str | None = None
    delivery_style: str | None = None


@dataclass(frozen=True)
class PauseConfig:
    major: float = DEFAULT_PAUSE_MAJOR
    minor: float = DEFAULT_PAUSE_MINOR
    para: float = DEFAULT_PAUSE_PARA

    def before_heading(self, level: int) -> float:
        return self.major if level <= 2 else self.minor


def looks_like_markdown(text: str) -> bool:
    return bool(_MD_HINT_RE.search(text))


def _word_edge(text: str, i: int) -> bool:
    return i <= 0 or i >= len(text) or not text[i].isalnum()


def _strip_md_links(text: str) -> str:
    text = _INLINE_HTML_COMMENT_RE.sub("", text)

    def repl(m: re.Match[str]) -> str:
        if _MISAKI_RE.fullmatch(m.group(0)):
            return m.group(0)
        return m.group(1)

    return _MD_LINK_RE.sub(repl, text)


def parse_speak_directive(body: str) -> dict:
    """Parse `key=value` tokens inside `<!-- speak: ... -->`.

    Supported keys: `voice`, `style`, `speed`, `pause`, `reset`.
    """
    out: dict = {}
    try:
        tokens = shlex.split(body.strip())
    except ValueError as exc:
        raise ValueError(f"invalid <!-- speak: ... --> directive: {exc}") from None
    for tok in tokens:
        if tok.lower() == "reset":
            out["reset"] = True
            continue
        if "=" not in tok:
            raise ValueError(
                f"invalid <!-- speak: ... --> token {tok!r} (expected key=value)"
            )
        key, val = tok.split("=", 1)
        key = key.strip().lower()
        val = val.strip()
        if key == "voice":
            if ":" in val:
                raise ValueError(
                    "markdown speak directives cannot switch vendors (`voice=` takes a voice name, not `vendor:voice`)"
                )
            out["voice"] = val or None
        elif key in ("style", "instructions"):
            out["style"] = val or None
        elif key == "speed":
            if not val or val.lower() == "default":
                out["speed"] = None
            else:
                try:
                    sp = float(val)
                except ValueError:
                    raise ValueError(f"invalid speed {val!r} in speak directive") from None
                if not math.isfinite(sp) or sp <= 0:
                    raise ValueError("speed in speak directive must be positive")
                out["speed"] = sp
        elif key == "pause":
            try:
                p = float(val)
            except ValueError:
                raise ValueError(f"invalid pause {val!r} in speak directive") from None
            if not math.isfinite(p) or p < 0:
                raise ValueError("pause in speak directive must be >= 0")
            out["pause"] = p
        else:
            raise ValueError(
                f"unknown speak directive key {key!r} (supported: voice, style, speed, pause, reset)"
            )
    return out


def parse_inline_spans(text: str) -> tuple[SpeakSpan, ...]:
    text = _strip_md_links(text)
    spans: list[SpeakSpan] = []
    i = 0
    n = len(text)

    def push(raw: str, style: Style) -> None:
        if not raw:
            return
        if spans and style == "plain" and spans[-1].style == "plain":
            spans[-1] = SpeakSpan(spans[-1].text + raw, "plain")
        else:
            spans.append(SpeakSpan(raw, style))

    while i < n:
        misaki = _MISAKI_RE.match(text, i)
        if misaki:
            push(misaki.group(0), "plain")
            i = misaki.end()
            continue
        if text.startswith("**", i):
            end = text.find("**", i + 2)
            if end != -1:
                push(text[i + 2 : end], "strong")
                i = end + 2
                continue
        if text.startswith("__", i) and _word_edge(text, i - 1):
            end = text.find("__", i + 2)
            if end != -1 and _word_edge(text, end + 2):
                push(text[i + 2 : end], "strong")
                i = end + 2
                continue
        if text[i] == "`":
            end = text.find("`", i + 1)
            if end != -1:
                push(text[i + 1 : end], "code")
                i = end + 1
                continue
        if text[i] == "*":
            end = text.find("*", i + 1)
            if end != -1 and end > i + 1:
                push(text[i + 1 : end], "strong")
                i = end + 1
                continue
        if text[i] == "_" and _word_edge(text, i - 1):
            end = text.find("_", i + 1)
            if end != -1 and end > i + 1 and _word_edge(text, end + 1):
                push(text[i + 1 : end], "strong")
                i = end + 1
                continue
        next_special = n
        for token in ("**", "__", "`", "[", "*", "_"):
            pos = text.find(token, i)
            if pos != -1:
                next_special = min(next_special, pos)
        if next_special == i:
            push(text[i], "plain")
            i += 1
            continue
        push(text[i:next_special], "plain")
        i = next_special
    return tuple(spans)


def _pause_for(
    kind: BlockKind, level: int, *, first: bool, pauses: PauseConfig
) -> float:
    if first:
        return 0.0
    if kind == "heading":
        return pauses.before_heading(level)
    if kind == "list_item":
        return min(pauses.para, LIST_PAUSE_CAP)
    return pauses.para


def iter_markdown_blocks(
    markdown: str, pauses: PauseConfig | None = None
) -> Iterator[SpeakBlock]:
    pauses = pauses or PauseConfig()
    fenced = False
    para_lines: list[str] = []
    first = True
    cur_voice: str | None = None
    cur_style: str | None = None
    cur_speed: float | None = None
    pending_pause: float = 0.0

    def take_pause(default_pause: float) -> float:
        nonlocal pending_pause
        p = max(default_pause, pending_pause)
        pending_pause = 0.0
        return p

    def flush_para() -> Iterator[SpeakBlock]:
        nonlocal first, para_lines
        if not para_lines:
            return
        text = " ".join(line.strip() for line in para_lines if line.strip())
        para_lines = []
        if not text:
            return
        spans = parse_inline_spans(text)
        if not spans:
            return
        pause = take_pause(_pause_for("paragraph", 0, first=first, pauses=pauses))
        first = False
        yield SpeakBlock("paragraph", 0, spans, pause, cur_voice, cur_style, cur_speed)

    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            yield from flush_para()
            fenced = not fenced
            continue
        if fenced:
            continue
        directive_match = _DIRECTIVE_RE.match(stripped)
        if directive_match:
            yield from flush_para()
            parsed = parse_speak_directive(directive_match.group(1))
            if parsed.get("reset"):
                cur_voice = None
                cur_style = None
                cur_speed = None
            if "voice" in parsed:
                cur_voice = parsed["voice"]
            if "style" in parsed:
                cur_style = parsed["style"]
            if "speed" in parsed:
                cur_speed = parsed["speed"]
            if "pause" in parsed:
                pending_pause += float(parsed["pause"])
            continue
        if stripped.startswith("|") or _HR_RE.match(line):
            continue
        if re.match(r"^#{1,6}\s*$", stripped):
            yield from flush_para()
            continue
        heading = _HEADING_RE.match(stripped)
        if heading:
            yield from flush_para()
            level = len(heading.group(1))
            spans = parse_inline_spans(heading.group(2).strip())
            if not spans:
                continue
            pause = take_pause(_pause_for("heading", level, first=first, pauses=pauses))
            first = False
            yield SpeakBlock("heading", level, spans, pause, cur_voice, cur_style, cur_speed)
            continue
        list_item = _LIST_RE.match(line)
        if list_item:
            yield from flush_para()
            spans = parse_inline_spans(list_item.group(3).strip())
            if not spans:
                continue
            pause = take_pause(_pause_for("list_item", 0, first=first, pauses=pauses))
            first = False
            yield SpeakBlock("list_item", 0, spans, pause, cur_voice, cur_style, cur_speed)
            continue
        if stripped.startswith(">"):
            para_lines.append(stripped.lstrip("> ").strip())
            continue
        if not stripped:
            yield from flush_para()
            continue
        para_lines.append(line)
    yield from flush_para()


def parse_markdown_blocks(
    markdown: str, pauses: PauseConfig | None = None
) -> list[SpeakBlock]:
    return list(iter_markdown_blocks(markdown, pauses))


def map_span_text(blocks: list[SpeakBlock], transform) -> list[SpeakBlock]:
    out: list[SpeakBlock] = []
    for block in blocks:
        spans = tuple(
            SpeakSpan(transform(span.text), span.style) for span in block.spans
        )
        spans = tuple(s for s in spans if s.text.strip())
        if not spans:
            continue
        out.append(
            SpeakBlock(
                block.kind,
                block.level,
                spans,
                block.pause_before,
                block.voice,
                block.delivery_style,
                block.speed_override,
            )
        )
    return out


def _say_strong(text: str) -> str:
    parts: list[str] = []
    for token in re.finditer(r"\S+|\s+", text):
        chunk = token.group(0)
        if chunk.isspace():
            parts.append(chunk)
        else:
            parts.append(f"[[emph +]]{chunk}")
    return "".join(parts)


def render_say_text(blocks: list[SpeakBlock], *, emphasis: bool = True) -> str:
    parts: list[str] = []
    for block in blocks:
        if block.pause_before > 0:
            ms = max(1, int(round(block.pause_before * 1000)))
            parts.append(f"[[slnc {ms}]]")
        chunk: list[str] = []
        for span in block.spans:
            text = span.text
            if not text:
                continue
            if not emphasis or span.style == "plain":
                chunk.append(text)
                continue
            if span.style == "code":
                chunk.append(f"[[slnc 120]]{text}[[slnc 120]]")
                continue
            chunk.append(_say_strong(text))
        spoken = "".join(chunk).strip()
        if spoken:
            parts.append(spoken)
    return "\n".join(parts)


def render_kokoro_pieces(
    blocks: list[SpeakBlock],
    *,
    base_speed: float = 1.0,
    emphasis: bool = True,
) -> list[SpeakPiece]:
    pieces: list[SpeakPiece] = []
    for block in blocks:
        eff_base = block.speed_override if block.speed_override is not None else base_speed
        if block.pause_before > 0:
            pieces.append(SpeakPiece("", eff_base, block.pause_before, block.voice, block.delivery_style))
        spans = [s for s in block.spans if s.text.strip()]
        for i, span in enumerate(spans):
            text = span.text.strip()
            if span.style == "strong" and emphasis:
                speed = eff_base * 0.92
                silence_before = 0.08
            elif span.style == "code" and emphasis:
                speed = eff_base * 0.85
                silence_before = 0.12
            else:
                speed = eff_base
                silence_before = 0.0
            already_paused = pieces and not pieces[-1].text.strip()
            if silence_before and not already_paused:
                pieces.append(SpeakPiece("", speed, silence_before, block.voice, block.delivery_style))
            trailing = 0.12 if span.style == "code" and emphasis else 0.0
            if i < len(spans) - 1 and trailing == 0.0:
                trailing = 0.05 if span.style != "plain" else 0.0
            pieces.append(SpeakPiece(text, speed, trailing, block.voice, block.delivery_style))
    return [p for p in pieces if p.text.strip() or p.silence_after > 0]


def render_block_pieces(
    blocks: list[SpeakBlock],
    *,
    base_speed: float = 1.0,
    preserve_markdown_emphasis: bool = False,
) -> list[SpeakPiece]:
    pieces: list[SpeakPiece] = []
    for block in blocks:
        eff_speed = block.speed_override if block.speed_override is not None else base_speed
        if preserve_markdown_emphasis:
            parts: list[str] = []
            for span in block.spans:
                if span.style == "strong" and span.text.strip():
                    lead = span.text[: len(span.text) - len(span.text.lstrip())]
                    tail = span.text[len(span.text.rstrip()) :]
                    parts.append(f"{lead}**{span.text.strip()}**{tail}")
                else:
                    parts.append(span.text)
            text = "".join(parts).strip()
        else:
            text = "".join(span.text for span in block.spans).strip()
        if not text:
            continue
        if block.pause_before > 0:
            pieces.append(SpeakPiece("", eff_speed, block.pause_before, block.voice, block.delivery_style))
        pieces.append(SpeakPiece(text, eff_speed, 0.0, block.voice, block.delivery_style))
    return pieces
