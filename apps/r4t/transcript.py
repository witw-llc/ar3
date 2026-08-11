"""What a member's last CLI turn did to the provider's cache.

A continuation re-reads the conversation's cached prefix at a fraction of the
input price when it hits, and re-writes the whole conversation at a premium
when it misses. Whether a member continues at all is the roster's `Continue:`
flag — an explicit operator choice, never gated on these numbers — so the
probe's job is measurement: `dispatch._log_cache_usage` turns each completed
turn into a CACHE line, and a continued turn that re-created its own history
into a CACHE-MISS.

The numbers live in files each CLI writes for its own purposes. Every harness
does that differently, so each gets its own probe here and its own research
page on the wiki; a harness with no probe reports nothing.

Probes are best-effort by design. A member behind `run_as` or inside a
container writes its transcript in a home this process cannot read, so the
probe returns None and dispatch logs nothing.
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
        if read + created + fresh == 0:
            # A failed or interrupted run appends a synthetic zero-usage
            # record after the real ones; reading it as the turn's usage
            # reports a large session as empty. Keep scanning for the newest
            # record that measured anything.
            continue
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
