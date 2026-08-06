"""What a member's CLI conversation currently costs to continue.

`Continue: on` is worth money only while the provider still holds the
conversation's prefix in cache. A warm continuation re-reads that prefix at a
fraction of the input price; a cold one re-sends the whole conversation and
pays a premium to write it back. The gap between those two is the difference
between a cheap wake and a wake that eats a day's quota.

Two independent things make a continuation expensive, and only one of them is
about time:

- **Age.** The cache is a sliding window refreshed by each read. Past it, the
  entire accumulated prefix is re-processed as new.
- **Size.** A large conversation gets re-written even while it is being used,
  because the cache breakpoints move as it grows. Waking a huge session often
  enough to keep it warm does not help; it just keeps the liability alive.

`dispatch` needs numbers for both, and the numbers live in files each CLI
writes for its own purposes. Every harness does that differently, so each gets
its own probe here and its own research page on the wiki. A harness with no
probe reports nothing and its conversation is never gated — unmeasured is not
the same as safe, so its knobs stay unset in `rig.HARNESS_PRESETS` until
somebody does the reading.

Probes are best-effort by design. A member behind `run_as` or inside a
container writes its transcript in a home this process cannot read, so the
probe returns None and dispatch keeps its existing behaviour.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

# How far back to look for the newest turn's usage record. Transcripts reach
# tens of megabytes; the rows we want are at the end, and a bounded read keeps
# the probe off the critical path of every turn.
TAIL_BYTES = 1024 * 1024


@dataclass(frozen=True)
class Conversation:
    """One CLI conversation, measured as of its most recent turn."""

    path: Path
    size_bytes: int
    #: Prefix the last turn processed — what the next continuation carries
    #: before it adds anything. Zero when the transcript records no usage.
    context_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    ephemeral_1h_tokens: int
    ephemeral_5m_tokens: int

    @property
    def measured(self) -> bool:
        """False when the file was found but carried no usage record — a
        conversation that has not completed a turn yet."""
        return self.context_tokens > 0

    def over_limit(
        self, max_tokens: int | None, max_bytes: int | None
    ) -> str | None:
        """Why this conversation is too expensive to continue, or None.

        The token cap is the real guard. The byte cap is the fallback for a
        transcript whose usage records are missing or unparseable, where size
        on disk is the only signal left.
        """
        if max_tokens and self.context_tokens > max_tokens:
            return f"context {self.context_tokens} tokens > {max_tokens}"
        if max_bytes and self.size_bytes > max_bytes:
            return f"transcript {self.size_bytes} bytes > {max_bytes}"
        return None


def _tail_lines(path: Path, limit: int = TAIL_BYTES) -> list[str]:
    """The last whole lines of a file, newest last."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > limit:
                fh.seek(size - limit)
                fh.readline()  # discard the partial line the seek landed in
            raw = fh.read()
    except OSError:
        return []
    return raw.decode("utf-8", errors="replace").splitlines()


def _claude_conversation(workdir: Path) -> Conversation | None:
    """Claude Code keeps one JSONL per session under its config directory,
    in a folder named for the working directory with `/` and `.` flattened
    to `-`. The newest file is the one `--continue` would resume."""
    raw_home = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    home = Path(raw_home).expanduser() if raw_home else Path.home() / ".claude"
    slug = re.sub(r"[/.]", "-", str(workdir))
    folder = home / "projects" / slug
    try:
        sessions = [p for p in folder.glob("*.jsonl") if p.is_file()]
    except OSError:
        return None
    if not sessions:
        return None
    newest = max(sessions, key=lambda p: p.stat().st_mtime)
    try:
        size = newest.stat().st_size
    except OSError:
        return None

    for line in reversed(_tail_lines(newest)):
        try:
            usage = (json.loads(line).get("message") or {}).get("usage")
        except (json.JSONDecodeError, AttributeError):
            continue
        if not isinstance(usage, dict):
            continue
        read = int(usage.get("cache_read_input_tokens") or 0)
        created = int(usage.get("cache_creation_input_tokens") or 0)
        fresh = int(usage.get("input_tokens") or 0)
        split = usage.get("cache_creation")
        split = split if isinstance(split, dict) else {}
        return Conversation(
            path=newest,
            size_bytes=size,
            context_tokens=read + created + fresh,
            cache_read_tokens=read,
            cache_creation_tokens=created,
            ephemeral_1h_tokens=int(split.get("ephemeral_1h_input_tokens") or 0),
            ephemeral_5m_tokens=int(split.get("ephemeral_5m_input_tokens") or 0),
        )

    return Conversation(newest, size, 0, 0, 0, 0, 0)


#: Preset name -> probe. Adding one is the code half of an engine's research
#: page; the knobs in `rig.HARNESS_PRESETS` are the other half.
PROBES = {
    "claude": _claude_conversation,
}


def probe(preset: str | None, workdir: Path) -> Conversation | None:
    """Measure the conversation `preset` would continue in `workdir`.

    None means "no answer": no probe for this harness, no transcript yet, or
    a transcript this process cannot read. Callers treat all three the same —
    they do not gate on a number they do not have.
    """
    fn = PROBES.get(preset or "")
    if fn is None:
        return None
    try:
        return fn(workdir)
    except OSError:
        return None
