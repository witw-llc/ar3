"""Rig config — the out-of-repo security boundary.

The roster names a SYMBOLIC rig (`leader`, `junior-dev`, ...). Only this
config, which lives outside the repo (default `~/.config/r4t/rigs.json`,
overridable per-node with --rig-config), maps a rig to an actual argv.
A rig missing from the config fails closed: the member does not run.

Top-level keys are rig names, except these reserved governance keys (all
optional — every knob has a sane default; see README.md for the table):

- `"pins"` — agent name → rig, silently overriding the roster's Rig
  line (an in-repo roster edit can't upgrade a pinned agent).
- `"throttle"` — roster-wide `max_concurrent` + `min_seconds_between_turn_starts`
  gates, enforced before any rig check.
- `"cell_budget_max"` / `"cell_budget_earn_per_hour"` — the shared cell spend
  bucket. A turn costs 1 cell unit; when it is empty no member runs.
- `"quiet_task_seconds"` — an intra-roster thread quiet this long with its
  originator still unanswered wakes the leader with a nudge to reply with
  current state. 0 turns the sweep off; ingress threads are never swept.
- `"log_retention_days"` — how many UTC days of roster transcript `r4t clear`
  keeps; older day files are deleted whole. 0 keeps every day forever.
- `"breaker_cap"` / `"breaker_cooldown_seconds"` — per-member failure breaker:
  consecutive failed turns (nonzero exit or timeout) that trip it, and how
  long turns stay paused per failure before one probe turn is let through.

Per-rig keys (defaults for every member on that rig; per-member override
later): `budget_max` / `budget_earn_per_hour` — the member spend bucket. A
turn costs 1 member unit; when it is empty the member is resting.
`rig_budget_max` / `rig_budget_earn_per_hour` — the MACHINE-GLOBAL rig spend
bucket (absent = no rig gate). A rig maps to a real subscription, so this
ceiling binds every roster on the machine that shares the rig; a turn also costs
1 rig unit and an empty rig bucket rests every member on it, on every roster. If
`rig_budget_max` is set, `rig_budget_earn_per_hour` must be set too — a real
plan always declares a refill rate.

`mcp` — inject the a8s MCP server (`a8s mcp serve`) into every turn on this
rig, using the harness's own per-invocation idiom, and teach the member the
`a8s_tell` tool instead of the shell command. Tri-state: unset takes the
preset's default (on wherever the idiom is invisible to the roster repo — see
`MCP_DEFAULT_ON_IDIOMS`, off for cursor and for presets with no idiom at all),
and an explicit true/false in rigs.json always wins. Presets whose CLI takes
MCP config only globally (agy) or has no tools at all (bare ollama) refuse an
explicit on. Each idiom rides a different channel, so `apply_mcp` states what an
org's isolation boundary has to carry across (`McpPlan`) and the wrapper in
isolate.py honours it.

`echo` — the rig's members never see `tell` or any messaging instructions:
they are simply prompted with the message content and their cleaned stdout is
staged as the one reply, through the same release gates every send passes.
`echo_max_chars` (default 1500) caps that reply's body; longer output is
truncated with the full text attached as a markdown file on the same envelope.

`preset` — the harness preset the rig was created from (`r4t rig add`/`swap`
record it). It defaults the text knobs (`history_max_bytes` /
`history_body_max` / `prompt_body_max`) by the preset's text tier (TEXT_TIERS);
explicit values always win, and a rig with no preset gets the small tier. It
also carries the preset's `continue_argv` — the tokens that make the CLI resume
its own conversation instead of starting from a cold prompt. A rig with no
preset, or one whose preset has no `continue_argv`, cannot continue; a roster
member asking for it fails closed (see `RigConfig.rig_for`).

`framing` — this rig's default for the cautionary line under a member's
`## Knowledge` section: `"default"` (or absent) is the built-in wording,
`"off"` drops the line, any other string is custom wording taken verbatim (no
quote marks needed — the JSON string already delimits it). A member's own
`Framing:` roster line always wins over this default; see
docs/r4t-knowledge.md.

OS-level isolation (`run_as` / `container`) is NOT a rig key — it is a
per-org decision (rigs are machine-global and shared across orgs, so one Unix
user or image serves an org's whole roster). It lives in `r4t-org.json`; see
org.py and docs/r4t-isolation.md.

Keys starting with `_` anywhere are ignored so shipped examples can carry
notes.
"""
from __future__ import annotations

import json
import re
import shlex
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from isolate import Isolation
from roster import (
    KNOWLEDGE_DEFAULT_BUDGET,
    KNOWLEDGE_SIZES,
    FramingSpec,
    Member,
    Roster,
    parse_framing,
)
from state import atomic_write_json, r4t_home

PROMPT_PLACEHOLDER = "{prompt}"
# The directory the turn runs from, for harnesses that take it as an argument
# rather than reading their own cwd. dispatch.run_harness fills it with the
# member's resolved `Workdir:`.
WORKDIR_PLACEHOLDER = "{workdir}"

RESERVED_CONFIG_KEYS = frozenset({
    "pins",
    "throttle",
    "cell_budget_max",
    "cell_budget_earn_per_hour",
    "breaker_cap",
    "breaker_cooldown_seconds",
    "quiet_task_seconds",
    "log_retention_days",
})

# `continue_argv` is present only where the CLI's own `--help` was read and the
# flag verified against the installed binary; a preset without it does not
# support continuing, which is a fine state. `no_prior_conversation` is the
# regex (matched case-insensitively against a failed turn's output) that says
# the CLI refused to launch because there is no conversation in this directory
# yet — only cursor does that; the others quietly found one and exit 0, so they
# need no pattern. `continue_anchor` places the tokens immediately after that
# argv token instead of at the end, the way `model_anchor` does — a CLI whose
# continuation is a subcommand cannot be appended to a finished argv.
#
# No preset gates continuation. Whether a member continues is the roster's
# `Continue:` flag alone — an explicit operator acceptance of the cache-miss
# risk — because measured production telemetry shows the miss is a
# process-boundary phenomenon no warmth or size heuristic can prevent: a
# resume seconds after a successful turn rewrites the whole conversation at
# the premium rate about 16× as often as staying in-process (Engine pages on
# the wiki hold the tables). `transcript.PROBES` measures what a turn did to
# the cache so `dispatch._log_cache_usage` can report it; measurement, not
# gating.
HARNESS_PRESETS: dict[str, dict] = {
    "claude": {
        "text_tier": "big",
        "description": "Claude Code — matches apps/a8s/definitions/claude.json",
        "a8s_definition": "claude.json",
        "headless": "-p",
        "mcp": "claude-flag",
        "invoke": [
            "claude",
            "--permission-mode",
            "dontAsk",
            "--allowedTools",
            "Bash(tell:*) Read Edit Write Glob Grep WebFetch WebSearch TodoWrite",
            # Moves cwd, environment, memory paths and git status out of the
            # system prompt and into the first user message. Those change per
            # machine and per commit, and they sit at the very front of the
            # prefix, so leaving them there splits the cache across every
            # workdir and every commit a member runs on. Verified in 2.1.220.
            "--exclude-dynamic-system-prompt-sections",
            "-p",
            "{prompt}",
        ],
        "model_argv": ["--model", "{model}"],
        "continue_argv": ["--continue"],
    },
    "codex": {
        # Continuation is the `resume --last` SUBCOMMAND, so it cannot be
        # appended to a finished argv — it goes immediately after `exec`, the
        # same anchor the model flags use, leaving
        # `codex exec resume --last -m MODEL <flags> <prompt>`.
        # `resume` also takes an optional [SESSION_ID] in place of --last: the
        # native way to pin ONE conversation, which is where per-member
        # session pinning (#17) will look.
        "text_tier": "big",
        "description": "OpenAI Codex CLI — matches apps/a8s/definitions/codex.json",
        "a8s_definition": "codex.json",
        "headless": "exec (positional prompt)",
        "mcp": "codex-config",
        "invoke": [
            "codex",
            "exec",
            "--full-auto",
            "--skip-git-repo-check",
            "{prompt}",
        ],
        "model_argv": ["-m", "{model}"],
        "model_anchor": "exec",
        "continue_argv": ["resume", "--last"],
        "continue_anchor": "exec",
    },
    "cursor": {
        "text_tier": "moderate",
        "description": "Cursor Agent CLI (`agent`) — matches apps/a8s/definitions/cursor.json",
        "a8s_definition": "cursor.json",
        "headless": "-p",
        "mcp": "cursor-file",
        "invoke": [
            "agent",
            "-p",
            "--trust",
            "--force",
            "--approve-mcps",
            "{prompt}",
        ],
        "model_argv": ["--model", "{model}"],
        # `agent` reuses the LAST --model it was given when the flag is
        # omitted, so an unpinned invoke inherits invisible machine-global
        # state — a rig founded "modelless" kept riding a usage-limited
        # frontier model. `auto` is the CLI's own way to say "the
        # subscription default", so pin it; an explicit --model still wins.
        "model_default": "auto",
        "continue_argv": ["--continue"],
        "no_prior_conversation": r"no previous chats found",
    },
    "opencode": {
        "text_tier": "moderate",
        "description": (
            "OpenCode 1.17+ — `run` (not `-i`) with --auto for headless repo tools"
        ),
        "a8s_definition": "opencode.json",
        "headless": "run --auto (positional prompt)",
        "mcp": "opencode-env",
        # --dir takes the workdir as an ABSOLUTE path. opencode resolves a
        # relative --dir against $PWD, falling back to its real cwd only when
        # PWD is unset, and a spawned process inherits the PWD of whoever
        # started r4t — so `--dir .` anchored the file tools wherever r4t was
        # invoked from, not the member's `Workdir:`.
        "invoke": [
            "opencode",
            "run",
            "--auto",
            "--dir",
            "{workdir}",
            "{prompt}",
        ],
        "model_argv": ["-m", "{model}"],
        "model_anchor": "run",
        "continue_argv": ["--continue"],
    },
    "opencode-ollama": {
        "text_tier": "small",
        "description": (
            "OpenCode via `ollama launch` — local models, no cloud quota; "
            "requires --model"
        ),
        "a8s_definition": "opencode.json",
        "headless": "ollama launch opencode --model MODEL -- run --auto",
        "mcp": "opencode-env",
        "invoke": [
            "ollama",
            "launch",
            "opencode",
            "--model",
            "{model}",
            "--",
            "run",
            "--auto",
            "--dir",
            "{workdir}",
            "{prompt}",
        ],
        # `ollama launch` passes the appended flag through to opencode, whose
        # own per-directory session store then works normally (verified live
        # against a local model: planted, resumed, and founded cold cleanly).
        "continue_argv": ["--continue"],
    },
    "claude-ollama": {
        "text_tier": "small",
        "description": (
            "Claude Code via `ollama launch` — local models, no cloud quota; "
            "requires --model"
        ),
        "a8s_definition": "claude.json",
        "headless": "ollama launch claude --model MODEL -y -- --permission-mode dontAsk -p",
        "mcp": "claude-flag",
        "invoke": [
            "ollama",
            "launch",
            "claude",
            "--model",
            "{model}",
            "-y",
            "--",
            "--permission-mode",
            "dontAsk",
            "--allowedTools",
            "Bash(tell:*) Read Edit Write Glob Grep WebFetch WebSearch TodoWrite",
            # Kept in step with the `claude` preset. A local runner reuses a
            # stable prefix too, so moving the per-machine facts out of the
            # system prompt is worth the same here as it is against the API.
            "--exclude-dynamic-system-prompt-sections",
            "-p",
            "{prompt}",
        ],
    },
    "codex-ollama": {
        "text_tier": "small",
        "description": (
            "Codex via `ollama launch` — local models, no cloud quota; "
            "requires --model (the launcher owns -m)"
        ),
        "a8s_definition": "codex.json",
        "headless": "ollama launch codex --model MODEL -y -- exec --full-auto",
        "mcp": "codex-config",
        "invoke": [
            "ollama",
            "launch",
            "codex",
            "--model",
            "{model}",
            "-y",
            "--",
            "exec",
            "--full-auto",
            "--skip-git-repo-check",
            "{prompt}",
        ],
    },
    "copilot-ollama": {
        "text_tier": "small",
        "description": (
            "Copilot CLI via `ollama launch` — local models, no cloud quota; "
            "requires --model"
        ),
        "a8s_definition": "copilot.json",
        "headless": "ollama launch copilot --model MODEL -y -- --allow-all-tools -p",
        "mcp": "copilot-flag",
        "invoke": [
            "ollama",
            "launch",
            "copilot",
            "--model",
            "{model}",
            "-y",
            "--",
            "--allow-all-tools",
            "-p",
            "{prompt}",
        ],
    },
    "ollama": {
        "text_tier": "small",
        "description": (
            "Bare `ollama run` — tiny models with no tool use or big context; "
            "replies ride the stdout fallback; requires --model"
        ),
        "headless": "run MODEL PROMPT (positional)",
        "invoke": [
            "ollama",
            "run",
            "{model}",
            "{prompt}",
        ],
    },
    "agy": {
        "text_tier": "big",
        "description": (
            "Antigravity 1.1+ — --print for headless turns; --mode accept-edits "
            "so edits skip the review prompt. --dangerously-skip-permissions "
            "because 1.1.3+ auto-denies command tools in headless --print runs "
            "(toolPermission=request-review can't prompt), which accept-edits no "
            "longer covers — roster members must run tell and git. OS isolation, "
            "not the harness permission layer, is the security boundary. "
            "No --sandbox: it confines child writes to CWD, blocking tell's "
            "staging outbox (see docs/r4t-harness-agy.md)"
        ),
        "a8s_definition": "agy.json",
        "headless": "--print",
        "invoke": [
            "agy",
            "--dangerously-skip-permissions",
            "--mode",
            "accept-edits",
            "--print",
            "{prompt}",
        ],
        "model_argv": ["--model", "{model}"],
        "model_resolver": "agy-live",
        "continue_argv": ["--continue"],
    },
    "copilot": {
        # No continue_argv: `copilot --continue` resumes the machine's most
        # recent session whatever the directory, so members cannot be kept
        # apart. Clean support means pinning `--resume=<session-id>` per
        # member; that is #17, not this preset.
        "text_tier": "moderate",
        "description": "GitHub Copilot CLI — matches apps/a8s/definitions/copilot.json",
        "a8s_definition": "copilot.json",
        "headless": "-p",
        "mcp": "copilot-flag",
        "invoke": [
            "copilot",
            "--allow-all-tools",
            "-p",
            "{prompt}",
        ],
    },
}

DEFAULT_TIMEOUT_SECONDS = 900
DEFAULT_CONCURRENCY = 1
DEFAULT_MAX_SENDS_PER_TURN = 6
DEFAULT_HISTORY_MAX_BYTES = 8192
DEFAULT_HISTORY_BODY_MAX = 2000
DEFAULT_PROMPT_BODY_MAX = 4000

# Text-budget tiers: how much history/prompt a preset's harness can usefully
# carry. `small` is the conservative floor and the fallback for rigs with no
# preset (custom/scripted CLIs — an unknown harness gets the safe values).
TEXT_TIERS: dict[str, dict[str, int]] = {
    "big": {
        "history_max_bytes": 50_000,
        "history_body_max": 12_000,
        "prompt_body_max": 24_000,
    },
    "moderate": {
        "history_max_bytes": 25_000,
        "history_body_max": 6_000,
        "prompt_body_max": 12_000,
    },
    "small": {
        "history_max_bytes": DEFAULT_HISTORY_MAX_BYTES,
        "history_body_max": DEFAULT_HISTORY_BODY_MAX,
        "prompt_body_max": DEFAULT_PROMPT_BODY_MAX,
    },
}


def text_defaults(preset: str | None) -> dict[str, int]:
    """The text-knob defaults for `preset` (small when absent/unknown)."""
    tier = HARNESS_PRESETS.get((preset or "").strip().lower(), {}).get("text_tier")
    return TEXT_TIERS.get(tier or "small", TEXT_TIERS["small"])


# Knowledge inject-budget tiers by harness class:
# local/opencode-class members write smaller, smoothed-over notes at a given
# byte budget, so they default lower; codex/claude default highest. This is
# NOT `text_tier` — agy is a big-context harness but a fast small-effort model
# in K2's measurements, so it sits in the middle here with cursor rather than
# with codex/claude. Anchored on `roster.KNOWLEDGE_SIZES`, so a `large` move
# there moves these defaults too.
KNOWLEDGE_TIER_LOW = frozenset({
    "opencode", "ollama", "opencode-ollama",
    "claude-ollama", "codex-ollama", "copilot-ollama",
})
KNOWLEDGE_TIER_MID = frozenset({"agy", "cursor", "copilot"})
KNOWLEDGE_TIER_HIGH = frozenset({"codex", "claude"})


def knowledge_tier_bytes(preset: str | None) -> int:
    """The Knowledge inject budget (bytes) a bare `on`/`<rig>` line earns from
    `preset`'s harness class. An unknown or absent preset (custom rig) gets
    the global floor, same as `text_defaults`' small tier."""
    p = (preset or "").strip().lower()
    if p in KNOWLEDGE_TIER_HIGH:
        return KNOWLEDGE_SIZES["large"]
    if p in KNOWLEDGE_TIER_MID:
        return KNOWLEDGE_SIZES["medium"]
    if p in KNOWLEDGE_TIER_LOW:
        return KNOWLEDGE_SIZES["small"]
    return KNOWLEDGE_DEFAULT_BUDGET


def is_below_knowledge_floor(preset: str | None) -> bool:
    """True for a harness class the K2 campaign measured smoothing over
    specifics rather than keeping them — `r4t roster check`'s courtesy
    warning, never a gate (models improve; a floor here would age badly)."""
    return (preset or "").strip().lower() in KNOWLEDGE_TIER_LOW


def resolve_knowledge_bytes(member: Member, rig: "Rig | None") -> int:
    """The effective Knowledge inject budget in bytes: the member's explicit
    size wins, else `rig`'s harness tier, else the global default when no rig
    resolved. 0 when Knowledge is off. `rig` is always the member's own turn
    rig — inject happens on the harness that wakes the member, independent of
    any `Knowledge:` distill-rig override (that only steers dreaming)."""
    if not member.knowledge_on:
        return 0
    if member.knowledge_bytes is not None:
        return member.knowledge_bytes
    return knowledge_tier_bytes(rig.preset if rig is not None else None)


def resolve_framing(member: Member, rig: "Rig | None") -> FramingSpec:
    """The effective `Framing:` choice for a member's Knowledge section:
    the member's own roster line wins when present, else the rig's own
    config default, else the built-in framing (an unset FramingSpec —
    off=False, text=None)."""
    if member.framing is not None:
        return member.framing
    if rig is not None and rig.framing is not None:
        return rig.framing
    return FramingSpec()


DEFAULT_BUDGET_MAX = 8.0
DEFAULT_BUDGET_EARN_PER_HOUR = 4.0
DEFAULT_ECHO_MAX_CHARS = 1500
DEFAULT_MAX_CONCURRENT = 1
DEFAULT_MIN_SECONDS_BETWEEN_TURN_STARTS = 15.0
DEFAULT_CELL_BUDGET_MAX = 16.0
DEFAULT_CELL_BUDGET_EARN_PER_HOUR = 8.0
DEFAULT_BREAKER_CAP = 5
DEFAULT_BREAKER_COOLDOWN_SECONDS = 600.0
DEFAULT_QUIET_TASK_SECONDS = 1800.0
DEFAULT_LOG_RETENTION_DAYS = 14


class RigError(Exception):
    pass


@dataclass
class Throttle:
    """Roster-wide gate applied before any rig check. `max_concurrent` caps
    live turns across ALL rigs (0 = unlimited); the cadence field spaces
    turn STARTS so a human can watch and intervene (0 = no gate)."""

    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    min_seconds_between_turn_starts: float = DEFAULT_MIN_SECONDS_BETWEEN_TURN_STARTS


@dataclass
class Rig:
    name: str
    invoke: list = field(default_factory=list)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    concurrency: int = DEFAULT_CONCURRENCY
    max_sends_per_turn: int = DEFAULT_MAX_SENDS_PER_TURN
    budget_max: float = DEFAULT_BUDGET_MAX
    budget_earn_per_hour: float = DEFAULT_BUDGET_EARN_PER_HOUR
    rig_budget_max: float | None = None
    rig_budget_earn_per_hour: float | None = None
    history_max_bytes: int = DEFAULT_HISTORY_MAX_BYTES
    history_body_max: int = DEFAULT_HISTORY_BODY_MAX
    prompt_body_max: int = DEFAULT_PROMPT_BODY_MAX
    model: str | None = None
    model_resolver: str | None = None
    preset: str | None = None
    echo: bool = False
    echo_max_chars: int = DEFAULT_ECHO_MAX_CHARS
    mcp: bool | None = None
    env: dict[str, str] = field(default_factory=dict)
    framing: FramingSpec | None = None
    error: str | None = None

    @property
    def mcp_idiom(self) -> str | None:
        """How this rig's preset takes an MCP server for ONE invocation. None
        means there is no per-turn path (agy configures MCP only in ~/.gemini;
        bare ollama has no tool use), and the knob refuses to turn on."""
        return HARNESS_PRESETS.get(self.preset or "", {}).get("mcp")

    @property
    def mcp_on(self) -> bool:
        """The effective `mcp` knob — what dispatch acts on."""
        return mcp_enabled(self.mcp, self.preset)

    @property
    def continue_argv(self) -> list[str]:
        """Tokens that make this rig's CLI resume the conversation it already
        has in the turn directory. Empty when the preset (or its absence) gives
        no verified way to continue."""
        return list(HARNESS_PRESETS.get(self.preset or "", {}).get("continue_argv", ()))

    @property
    def continue_anchor(self) -> str | None:
        """The argv token the continue tokens go immediately after. None (every
        flag-shaped CLI) means append at the end; codex needs one because
        `resume --last` is a subcommand and only reads in that position."""
        return HARNESS_PRESETS.get(self.preset or "", {}).get("continue_anchor")

    @property
    def supports_continue(self) -> bool:
        """False unless the tokens have somewhere to go. An anchored preset
        whose invoke was hand-edited past its anchor fails closed here rather
        than building an argv the CLI would read as something else."""
        if not self.continue_argv:
            return False
        anchor = self.continue_anchor
        return anchor is None or all(anchor in argv for argv in self.pool())

    @property
    def cli(self) -> str:
        """The CLI binary this rig drives — what a conversation is keyed on,
        together with the directory. `ollama launch <tool>` drives <tool>."""
        argv = next(iter(self.pool()), [])
        if not argv:
            return ""
        binary = Path(argv[0]).name
        if binary == "ollama" and argv[1:2] == ["launch"] and len(argv) > 2:
            return argv[2]
        return binary

    def had_no_prior_conversation(self, output: str) -> bool:
        """True when a failed turn's output is the CLI saying it had no
        conversation here to continue — the one failure worth retrying cold."""
        pattern = HARNESS_PRESETS.get(self.preset or "", {}).get("no_prior_conversation")
        return bool(pattern) and re.search(pattern, output, re.IGNORECASE) is not None

    def pool(self) -> list[list[str]]:
        """`invoke` is one argv (list of str) or a pool (list of argvs) —
        rotated round-robin per rig so e.g. local-model pools can back one
        rig while agents stay oblivious to what runs them."""
        if self.invoke and isinstance(self.invoke[0], list):
            return self.invoke
        return [self.invoke] if self.invoke else []

    @property
    def pool_size(self) -> int:
        return len(self.pool())

    def argv(
        self,
        prompt: str,
        index: int = 0,
        *,
        continue_conversation: bool = False,
        workdir: str | Path | None = None,
    ) -> list[str]:
        pool = self.pool()
        chosen = pool[index % len(pool)]
        # {workdir} goes in first: the prompt carries message text, so
        # substituting it last keeps a `{workdir}` a member typed from being
        # read as a placeholder.
        if workdir is not None:
            chosen = [a.replace(WORKDIR_PLACEHOLDER, str(workdir)) for a in chosen]
        argv = [a.replace(PROMPT_PLACEHOLDER, prompt) for a in chosen]
        if continue_conversation and self.continue_argv:
            anchor = self.continue_anchor
            at = argv.index(anchor) + 1 if anchor else len(argv)
            argv[at:at] = self.continue_argv
        return argv

    def distill_command(self, workdir: str | Path) -> str | None:
        """A stdin->stdout shell command line for k7e's `K7E_DISTILL_COMMAND`,
        built from this rig's own invoke. k7e pipes the prompt to the
        command's stdin with no shell of its own, and not every harness reads
        stdin as its prompt (agy prints usage instead), so `{prompt}` becomes
        `"$(cat)"` inside an `sh -c` wrapper — the prompt lands in the exact
        argument position the invoke defines, whatever the harness. None when
        the rig has nothing to run, or an agy-class model can't be resolved
        right now."""
        pool = self.pool()
        if not pool:
            return None
        argv = [a.replace(WORKDIR_PLACEHOLDER, str(workdir)) for a in pool[0]]
        if self.model_resolver == "agy-live":
            try:
                resolved = resolve_agy_model(self.model or "")
            except RigError:
                return None
            argv = [resolved if a == "{model}" else a for a in argv]
        inner = " ".join(
            '"$(cat)"'.join(
                shlex.quote(p) if p else "" for p in a.split(PROMPT_PLACEHOLDER)
            )
            for a in argv
        )
        return f"sh -c {shlex.quote(inner)}"


@dataclass
class RigConfig:
    path: Path
    rigs: dict[str, Rig] = field(default_factory=dict)
    pins: dict[str, str] = field(default_factory=dict)
    throttle: Throttle = field(default_factory=Throttle)
    cell_budget_max: float = DEFAULT_CELL_BUDGET_MAX
    cell_budget_earn_per_hour: float = DEFAULT_CELL_BUDGET_EARN_PER_HOUR
    breaker_cap: int = DEFAULT_BREAKER_CAP
    breaker_cooldown_seconds: float = DEFAULT_BREAKER_COOLDOWN_SECONDS
    quiet_task_seconds: float = DEFAULT_QUIET_TASK_SECONDS
    log_retention_days: int = DEFAULT_LOG_RETENTION_DAYS
    missing: bool = False

    def rig_for(self, member: Member) -> tuple[Rig | None, str | None, bool]:
        """Resolve a member to a runnable rig. Returns (rig, error, pinned).
        Any failure fails closed with rig=None and a human-readable error."""
        pinned_rig = self.pins.get(member.name.lower())
        pinned = pinned_rig is not None
        rig_name = pinned_rig if pinned else (member.rig or "")
        if not rig_name:
            return None, f"{member.name} has no Rig line in the roster", pinned
        if self.missing:
            return (
                None,
                f"rig {rig_name!r} not found (fail closed) — "
                f"try: r4t rig add {rig_name} <preset>",
                pinned,
            )
        rig = self.rigs.get(rig_name.lower())
        if rig is None:
            return (
                None,
                f"rig {rig_name!r} not found in {self.path} (fail closed) — "
                f"try: r4t rig add {rig_name} <preset>",
                pinned,
            )
        if rig.error:
            return None, f"rig {rig_name!r} is invalid: {rig.error}", pinned
        if member.continue_conversation and not rig.supports_continue:
            return (
                None,
                f"{member.name} has Continue: on but rig {rig_name!r} does not "
                f"support it (preset {rig.preset or 'none'}; presets that "
                f"continue: {', '.join(continue_presets())}) — "
                f"try: r4t rig swap {rig_name} <preset>",
                pinned,
            )
        return rig, None, pinned


def default_config_path() -> Path:
    return r4t_home() / "rigs.json"


def preset_names() -> list[str]:
    return sorted(HARNESS_PRESETS)


def continue_presets() -> list[str]:
    """Presets whose CLI can resume its own conversation."""
    return [n for n in preset_names() if HARNESS_PRESETS[n].get("continue_argv")]


# --- the `mcp` knob: one stdio server, five harness idioms -------------------
#
# The server definition is MEMBER-AGNOSTIC. A harness spawns its stdio servers
# as children of the turn process, which already carries the per-turn
# TELL_OUTBOX_DIR, so one blob serves every member on every node. Where the
# idiom accepts `env`/`cwd` they are pinned anyway, so the outbox is stated
# rather than inferred.
#
# Each idiom rides a different channel, and an OS boundary (isolate.py) keeps
# only what it is told to keep: argv passes through untouched, environment and
# files do not. So the injection is boundary-aware — it states what has to cross
# in an McpPlan the wrapper consumes, and names the image's own interpreter when
# the turn runs in a container, where the router's is not on the filesystem.

A8S_PY = Path(__file__).resolve().parent.parent / "a8s" / "a8s.py"

MCP_SERVER_NAME = "a8s"
# What claude's permission layer calls the tool the model sees as `a8s_tell`.
MCP_CLAUDE_TOOL = "mcp__a8s__tell"
OPENCODE_CONFIG_BASENAME = "mcp-opencode.json"
# Per-member dir the file idioms write into: a sibling of the turn's staging
# outbox rather than the member state dir itself, so a container can mount it
# read-only without exposing history or turn transcripts, and so a `.json`
# config is never mistaken for a staged envelope.
MCP_CONFIG_DIRNAME = "mcp"


@dataclass
class McpPlan:
    """One turn's injection, plus what it needs from the isolation boundary:
    `env_pass` are the variables the harness must still have on the far side of
    an `env_reset`/container, `mount_dirs` what a container has to see, and
    `read_paths` what the boundary's user must be able to read for the server to
    start at all."""

    argv: list[str]
    env_pass: dict[str, str] = field(default_factory=dict)
    mount_dirs: list[Path] = field(default_factory=list)
    read_paths: list[Path] = field(default_factory=list)


def mcp_presets() -> list[str]:
    """Presets that can take the MCP server for a single invocation."""
    return [n for n in preset_names() if HARNESS_PRESETS[n].get("mcp")]


# Idioms the roster repo never sees: a flag, a `-c` override, or a config file
# under the member's own state dir. Those default the knob ON — the tell-arms
# experiment measured the tool eliminating the no-send failure class outright
# (20/20 against 9/20), so an untouched rig has to be the one that sends.
# `cursor-file` writes `.cursor/mcp.json` into the working tree: writing a file
# into the user's repo is a different consent level than passing a flag, so
# cursor stays opt-in.
MCP_DEFAULT_ON_IDIOMS = frozenset({
    "claude-flag",
    "codex-config",
    "copilot-flag",
    "opencode-env",
})


def mcp_default(preset: str | None) -> bool:
    """Whether the knob is on for a preset whose rig says nothing about it."""
    return HARNESS_PRESETS.get(preset or "", {}).get("mcp") in MCP_DEFAULT_ON_IDIOMS


def mcp_enabled(mcp: bool | None, preset: str | None) -> bool:
    """Where the tri-state resolves, for dispatch and for the CLI's display
    alike: an explicit true/false in rigs.json wins, unset takes the preset's
    default. A preset with no idiom resolves off silently — only an explicit on
    is an error (see `mcp_unsupported_reason`)."""
    return mcp_default(preset) if mcp is None else mcp


def mcp_unsupported_reason(preset: str | None) -> str:
    if preset == "agy":
        return "agy reads MCP config only from ~/.gemini, never per invocation"
    if preset == "ollama":
        return "bare `ollama run` has no tool use at all"
    if preset:
        return f"preset {preset!r} has no per-invocation MCP idiom"
    return "the rig records no preset, so there is no idiom to inject with"


def _mcp_command(*, in_container: bool = False) -> list[str]:
    """How to start the server. A container has its own filesystem: the router's
    interpreter path is not in the image, but the a8s client dir is mounted at
    the same absolute path and the image already needs a `python3` for the `tell`
    shim, so inside one the interpreter resolves from the image's PATH."""
    python = "python3" if in_container else sys.executable
    return [python, str(A8S_PY), "mcp", "serve"]


_MCP_SERVER_ENV_KEYS = ("TELL_OUTBOX_DIR", "HOME", "A8S_HOME", "A8S_MCP_LOG")
# Behind an OS boundary the router's HOME and A8S_HOME are another user's home or
# absent from the image: pinning them points the server at paths it cannot read
# and promises a registry it will not find. The outbox is the one path the
# boundary does carry across, and `tell` writes the envelope from that alone.
_MCP_ISOLATED_SERVER_ENV_KEYS = ("TELL_OUTBOX_DIR", "A8S_MCP_LOG")


def _mcp_server_env(env: dict, *, isolated: bool = False) -> dict[str, str]:
    """What the server needs whatever the client's env policy. Clients differ
    on how much of their own environment they hand a stdio child, so the outbox
    it must write into is stated explicitly."""
    keys = _MCP_ISOLATED_SERVER_ENV_KEYS if isolated else _MCP_SERVER_ENV_KEYS
    pinned = {k: env.get(k, "") for k in keys}
    return {k: v for k, v in pinned.items() if v}


def _mcp_server_entry(env: dict, command: list[str], *, isolated: bool = False) -> dict:
    return {
        "command": command[0],
        "args": command[1:],
        "env": _mcp_server_env(env, isolated=isolated),
    }


def _mcp_servers_json(env: dict, command: list[str], *, isolated: bool = False) -> str:
    """The `mcpServers` object claude, copilot and cursor all read."""
    entry = _mcp_server_entry(env, command, isolated=isolated)
    return json.dumps({"mcpServers": {MCP_SERVER_NAME: entry}})


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _mcp_codex_override(
    env: dict, cwd: Path, command: list[str], *, isolated: bool = False
) -> str:
    """codex takes config as `-c key=<TOML value>`, and its server table is the
    one idiom that also accepts an explicit cwd."""
    args = ", ".join(_toml_string(a) for a in command[1:])
    server_env = _mcp_server_env(env, isolated=isolated)
    pinned = ", ".join(f"{k} = {_toml_string(v)}" for k, v in server_env.items())
    return (
        f"mcp_servers.{MCP_SERVER_NAME}={{"
        f"command = {_toml_string(command[0])}, "
        f"args = [{args}], "
        f"env = {{{pinned}}}, "
        f"cwd = {_toml_string(str(cwd))}"
        "}"
    )


def _mcp_opencode_config(env: dict, command: list[str], *, isolated: bool = False) -> str:
    return json.dumps(
        {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                MCP_SERVER_NAME: {
                    "type": "local",
                    "command": command,
                    "enabled": True,
                    "environment": _mcp_server_env(env, isolated=isolated),
                }
            },
        },
        indent=2,
    )


def _add_read_bits(path: Path, extra: int) -> None:
    """Grant read (and for a dir, traverse) to group and other without touching
    any other bit the operator set — setgid on a shared workplace survives."""
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode | extra != mode:
        path.chmod(mode | extra)


def _write_if_changed(path: Path, text: str) -> None:
    """Write a config a harness reads back. The reader may be another Unix user
    (`run_as`) or root inside a container, so the file does not stay at the
    router's umask — it carries paths and a command, never a secret."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _add_read_bits(path.parent, 0o055)
    if not (path.is_file() and path.read_text(encoding="utf-8") == text):
        path.write_text(text, encoding="utf-8")
    _add_read_bits(path, 0o044)


def _write_cursor_mcp(
    cwd: Path, env: dict, command: list[str], *, isolated: bool = False
) -> Path:
    """cursor has no per-invocation flag, so the server rides a file in the
    effective cwd. Any other server already configured there is preserved."""
    path = cwd / ".cursor" / "mcp.json"
    payload: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, dict):
            payload = loaded
    servers = payload.get("mcpServers")
    payload["mcpServers"] = {
        **(servers if isinstance(servers, dict) else {}),
        MCP_SERVER_NAME: _mcp_server_entry(env, command, isolated=isolated),
    }
    _write_if_changed(path, json.dumps(payload, indent=2))
    return path


def _mcp_config_dir(env: dict, cwd: Path) -> Path:
    """Where a config file the harness reads from disk goes: an `mcp` dir beside
    the member's per-turn staging outbox, so the knob writes nothing into the
    roster repo and a container mounts one dir that holds nothing else."""
    staging = env.get("TELL_OUTBOX_DIR", "")
    return Path(staging).parent / MCP_CONFIG_DIRNAME if staging else cwd


def _mcp_splice_at(argv: list[str]) -> int:
    """Where the driven CLI's own flags start: after the `--` that separates
    `ollama launch` from the tool it wraps, else right after the binary."""
    return argv.index("--") + 1 if "--" in argv else 1


def apply_mcp(
    rig: Rig, argv: list[str], env: dict, cwd: Path, isolation: Isolation | None = None
) -> McpPlan:
    """Inject the a8s MCP server into one turn with the harness's own idiom.
    `env` is updated in place and whatever config file the idiom needs is
    written; the returned plan carries the argv to run plus what the org's
    isolation boundary has to let through for the server to actually start."""
    idiom = rig.mcp_idiom
    if not idiom:
        return McpPlan(argv=argv)
    in_container = bool(isolation and isolation.container)
    isolated = bool(isolation and isolation.active)
    command = _mcp_command(in_container=in_container)
    plan = McpPlan(argv=list(argv))
    argv = plan.argv
    if not in_container:
        # Inside a container the interpreter comes from the image and the script
        # from a mount r4t already makes; outside one, both are the router's own
        # files and the boundary's user has to be able to read them.
        plan.read_paths += [Path(command[0]), A8S_PY]
    at = _mcp_splice_at(argv)
    if idiom == "claude-flag":
        argv[at:at] = ["--mcp-config", _mcp_servers_json(env, command, isolated=isolated)]
        # `dontAsk` never prompts, so a tool missing from --allowedTools is
        # silently denied — the MCP tool has to be named there too.
        for i, token in enumerate(argv):
            if token == "--allowedTools" and i + 1 < len(argv):
                if MCP_CLAUDE_TOOL not in argv[i + 1]:
                    argv[i + 1] = f"{argv[i + 1]} {MCP_CLAUDE_TOOL}".strip()
                break
    elif idiom == "codex-config":
        argv[at:at] = ["-c", _mcp_codex_override(env, cwd, command, isolated=isolated)]
    elif idiom == "copilot-flag":
        argv[at:at] = [
            "--additional-mcp-config", _mcp_servers_json(env, command, isolated=isolated)
        ]
    elif idiom == "opencode-env":
        path = _mcp_config_dir(env, cwd) / OPENCODE_CONFIG_BASENAME
        _write_if_changed(path, _mcp_opencode_config(env, command, isolated=isolated))
        # OPENCODE_CONFIG_CONTENT is not an option: `ollama launch` sets it for
        # provider+model and clobbers anything r4t puts there (measured).
        env["OPENCODE_CONFIG"] = str(path)
        # The variable is the whole idiom: a boundary that resets the environment
        # must re-export it, and a container must be able to see the file.
        plan.env_pass["OPENCODE_CONFIG"] = str(path)
        plan.mount_dirs.append(path.parent)
        plan.read_paths.append(path)
    elif idiom == "cursor-file":
        # The file lands in the effective cwd — the workplace, which both
        # wrappers already give the harness — so only its mode has to hold.
        plan.read_paths.append(_write_cursor_mcp(cwd, env, command, isolated=isolated))
    return plan


def _effective_cwd(member: Member, workplace: Path) -> Path:
    """The directory the member's turns run from, mirroring
    dispatch.resolve_workdir: the resolved `Workdir:` when set, else the
    workplace root."""
    if not member.workdir:
        return workplace.resolve()
    p = Path(member.workdir).expanduser()
    if not p.is_absolute():
        p = workplace / p
    return p.resolve()


def continue_collisions(roster: Roster, config: RigConfig, workplace: Path) -> list[str]:
    """Warn (never block) where `Continue: on` will cross wires with a member.

    A CLI keys its conversation on the directory it runs from, so two members
    driving the SAME CLI from the SAME effective directory (the workplace root,
    or the resolved `Workdir:` when set) land in one conversation — reading
    each other's turns and overwriting each other's tail. The other member does
    not have to be continuing to clobber it; it only has to run the same CLI
    there. Distinct workdirs keep members on one CLI apart."""
    seats: list[tuple[Member, Rig, Path]] = []
    for m in roster.members:
        if m.is_human or m.errors:
            continue
        rig, _err, _pinned = config.rig_for(m)
        if rig is not None:
            seats.append((m, rig, _effective_cwd(m, workplace)))
    out: list[str] = []
    for member, rig, cwd in seats:
        if not member.continue_conversation:
            continue
        others = [
            o.name for o, r, c in seats
            if o is not member and r.cli == rig.cli and c == cwd
        ]
        if others:
            out.append(
                f"{member.name}: Continue: on, but {', '.join(others)} also run "
                f"{rig.cli!r} in {cwd} — one CLI keeps one conversation per "
                f"directory, so their turns will land in {member.name}'s "
                f"(try: another CLI, or a distinct Workdir:)"
            )
    return out


def format_preset_invoke(preset: str) -> str:
    """The invoke a bare `rig add <preset>` produces, for display. Presets whose
    argv carries an inline `{model}` show the placeholder — they have no bare
    form to display."""
    entry = HARNESS_PRESETS[preset]
    if any("{model}" in arg for arg in entry["invoke"]):
        return " ".join(entry["invoke"])
    return " ".join(build_preset_invoke(preset))


def build_preset_invoke(preset: str, *, model: str | None = None) -> list[str]:
    """Materialize a preset argv for a given --model.

    Three shapes, keyed off the preset's metadata:

    - Inline `{model}` presets (ollama, opencode-ollama): --model is REQUIRED
      and substituted straight into the placeholder — the CLI has no default.
    - `model_argv` presets with a live resolver (agy): splice the flag pair but
      keep the `{model}` placeholder so dispatch re-resolves the friendly string
      against `agy models` before every turn (the display names drift as agy
      ships new versions, so a value baked in at add-time would go stale).
    - `model_argv` presets without a resolver (claude/codex/cursor/opencode):
      splice the flag pair with the resolved value now. --model is OPTIONAL —
      absent, the base argv is returned and the CLI's own default applies,
      unless the preset names a `model_default` to stand in (cursor, whose CLI
      would otherwise reuse the last model used on the machine).
    """
    preset_key = preset.strip().lower()
    if preset_key not in HARNESS_PRESETS:
        known = ", ".join(preset_names())
        raise RigError(f"unknown preset {preset!r}; choose one of: {known}")
    entry = HARNESS_PRESETS[preset_key]
    model_value = (model or "").strip() or entry.get("model_default", "")

    if any("{model}" in arg for arg in entry["invoke"]):
        if not model_value:
            raise RigError(f"preset {preset_key!r} requires --model")
        return [model_value if arg == "{model}" else arg for arg in entry["invoke"]]

    argv = list(entry["invoke"])
    if not model_value:
        return argv

    model_argv = entry.get("model_argv")
    if not model_argv:
        raise RigError(f"preset {preset_key!r} does not support --model")
    spliced = "{model}" if entry.get("model_resolver") else model_value
    flag_pair = [spliced if arg == "{model}" else arg for arg in model_argv]
    anchor = entry.get("model_anchor")
    insert_at = argv.index(anchor) + 1 if anchor else 1
    argv[insert_at:insert_at] = flag_pair
    return argv


AGY_MODELS_TIMEOUT_SECONDS = 10

# Effort/thinking suffix ranking used to break ties when a friendly string
# matches several display names (e.g. `flash` hits Flash Low/Medium/High).
_EFFORT_RANK = {"thinking": 4, "high": 3, "medium": 2, "low": 1}


def _model_tokens(text: str) -> list[str]:
    """Lowercase and split on runs of dashes/whitespace, dropping any wrapping
    parens so `-` and ` ` are interchangeable: `gemini-3.5-flash` and
    `Gemini 3.5 Flash` tokenize identically."""
    return [t.strip("()") for t in re.split(r"[-\s]+", text.strip().lower()) if t.strip("()")]


def _effort_rank(tokens: list[str]) -> int:
    return max((_EFFORT_RANK.get(t, 0) for t in tokens), default=0)


def fuzzy_match_model(query: str, names: list[str]) -> str:
    """Resolve a friendly --model string to one exact display name.

    A name matches when every query token is a substring of some name token,
    after both sides are normalized by `_model_tokens` (case-insensitive, dashes
    and spaces treated as the same separator, parens stripped). So `sonnet`,
    `claude-sonnet`, and the exact `Claude Sonnet 4.6 (Thinking)` all resolve;
    `gpt-oss-120b` matches `GPT-OSS 120B (Medium)`.

    Tie-break when several names match, in order: fewest extra tokens (tightest
    match) → highest effort/thinking suffix (thinking > high > medium > low) →
    alphabetical. A miss raises RigError listing the available names — agy
    silently ignores unknown --model strings, so an unresolved value must never
    be passed through.
    """
    q = _model_tokens(query)
    if not q:
        raise RigError("empty --model value; nothing to match")
    scored: list[tuple[int, int, str]] = []
    for name in names:
        name_tokens = _model_tokens(name)
        if all(any(tok in cand for cand in name_tokens) for tok in q):
            scored.append((len(name_tokens) - len(q), -_effort_rank(name_tokens), name))
    if not scored:
        listing = "\n".join(f"  {n}" for n in names)
        raise RigError(
            f"--model {query!r} matched no agy model. Available:\n{listing}\n"
            f"(try: r4t rig swap <rig> agy --model <one of the above>)"
        )
    scored.sort()
    return scored[0][2]


def agy_model_names(timeout: float = AGY_MODELS_TIMEOUT_SECONDS) -> list[str]:
    """The current `agy models` display names, one per line. Errors loudly —
    never returns a partial or fabricated list."""
    try:
        proc = subprocess.run(
            ["agy", "models"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise RigError(f"could not run `agy models` to resolve --model: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise RigError(
            f"`agy models` timed out after {timeout:g}s while resolving --model"
        ) from e
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RigError(f"`agy models` failed (exit {proc.returncode}): {detail}")
    names = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not names:
        raise RigError("`agy models` returned no models to match against")
    return names


def resolve_agy_model(
    query: str, *, timeout: float = AGY_MODELS_TIMEOUT_SECONDS, names: list[str] | None = None
) -> str:
    """Fuzzy-match `query` against the live `agy models` list. Nothing is cached:
    the list is re-fetched per call so it stays current as agy ships versions."""
    if names is None:
        names = agy_model_names(timeout)
    return fuzzy_match_model(query, names)


def _validate_rig_name(name: str) -> str:
    key = name.strip().lower()
    if not key:
        raise RigError("rig name is required")
    if key in RESERVED_CONFIG_KEYS:
        raise RigError(f"{key!r} is a reserved rig config key, not a rig name")
    return key


def _load_config_payload(path: Path) -> dict:
    """A missing file is an EMPTY config, not the `r4t init` starter payload —
    seeding starter rigs here made a fresh `rig add leader ...` collide
    with a phantom 'leader' the user never created."""
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise RigError(f"cannot load rig config {path}: {e}") from e
        if not isinstance(data, dict):
            raise RigError(f"rig config {path} must be a JSON object")
        return data
    return {
        "_notes": (
            "Created by `r4t rig add`. Rig names are SYMBOLIC — ROSTER.md "
            "Rig lines reference them. See `r4t rig presets` and "
            "docs/r4t-rigs.md."
        ),
    }


def add_preset_rig(
    path: Path,
    rig_name: str,
    preset: str,
    *,
    model: str | None = None,
    force: bool = False,
) -> str:
    """Add or replace a symbolic rig from a named CLI preset. Returns rig key."""
    rig_key = _validate_rig_name(rig_name)
    preset_key = preset.strip().lower()
    if preset_key not in HARNESS_PRESETS:
        known = ", ".join(preset_names())
        raise RigError(f"unknown preset {preset!r}; choose one of: {known}")
    payload = _load_config_payload(path)
    if rig_key in payload and not rig_key.startswith("_") and not force:
        raise RigError(
            f"rig {rig_key!r} already exists in {path} (pass --force to replace)"
        )
    entry = HARNESS_PRESETS[preset_key]
    invoke = build_preset_invoke(preset_key, model=model)
    note = (
        f"Added by `r4t rig add` from preset {preset_key!r} "
        f"({entry['description']})."
    )
    if model:
        note += f" model={model.strip()}."
    rig_entry: dict = {
        "_notes": note,
        "preset": preset_key,
        "invoke": invoke,
    }
    if model and entry.get("model_resolver"):
        rig_entry["model"] = model.strip()
        rig_entry["model_resolver"] = entry["model_resolver"]
    payload[rig_key] = rig_entry
    atomic_write_json(path, payload)
    return rig_key


def swap_preset_rig(
    path: Path,
    rig_name: str,
    preset: str,
    *,
    model: str | None = None,
) -> str:
    """Switch an existing rig's invoke to a preset's, preserving every other
    key (concurrency, timeout_seconds, ...). Returns rig key."""
    rig_key = _validate_rig_name(rig_name)
    preset_key = preset.strip().lower()
    if preset_key not in HARNESS_PRESETS:
        known = ", ".join(preset_names())
        raise RigError(f"unknown preset {preset!r}; choose one of: {known}")
    payload = _load_config_payload(path)
    existing = payload.get(rig_key)
    if not isinstance(existing, dict):
        raise RigError(
            f"no rig {rig_key!r} to swap in {path} "
            f"(try: r4t rig add {rig_key} {preset_key})"
        )
    entry = HARNESS_PRESETS[preset_key]
    invoke = build_preset_invoke(preset_key, model=model)
    note = f"Swapped to preset {preset_key!r} by `r4t rig swap`."
    if model:
        note += f" model={model.strip()}."
    existing["_notes"] = note
    existing["preset"] = preset_key
    existing["invoke"] = invoke
    # A swap replaces the harness wholesale, so stale model resolution from the
    # previous preset must not linger. The updated `preset` re-resolves the
    # text-tier defaults; explicit knob values in the entry still win.
    existing.pop("model", None)
    existing.pop("model_resolver", None)
    if model and entry.get("model_resolver"):
        existing["model"] = model.strip()
        existing["model_resolver"] = entry["model_resolver"]
    atomic_write_json(path, payload)
    return rig_key


def remove_rig(path: Path, rig_name: str) -> str:
    """Delete a symbolic rig from the config. Returns the removed key.

    Fails loudly if the rig is absent — the same shape `swap` uses for an
    unknown rig. Usage checks (roster/pin references) live in the CLI layer,
    which can reach the roster."""
    rig_key = _validate_rig_name(rig_name)
    payload = _load_config_payload(path)
    if rig_key not in payload or rig_key.startswith("_"):
        raise RigError(
            f"no rig {rig_key!r} to remove in {path} (try: r4t rig list)"
        )
    del payload[rig_key]
    atomic_write_json(path, payload)
    return rig_key


# --- the `env` map: harness knobs that ride the turn environment -------------
#
# A rig may carry static NAME=value pairs handed to its harness on every turn —
# the harness CLIs expose real knobs there (the first case is claude's
# ENABLE_PROMPT_CACHING_1H). Doctrine is FRUGAL: an entry earns its place with a
# documented reason, because this is the one rig key whose effect r4t cannot see.
#
# The turn environment is r4t's own channel: TELL_OUTBOX_DIR points at the
# member's staging outbox, PWD is pinned to the member's workdir, and the R4T_*
# family carries node, member, isolation and continue state. A rig naming one of
# those would steer dispatch from a config file, so the name is refused where it
# is written rather than silently losing to the turn.
TURN_OWNED_ENV = ("TELL_OUTBOX_DIR", "PWD")
TURN_OWNED_ENV_PREFIX = "R4T_"
ENV_SETTING_PREFIX = "env."
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _env_name_problem(name: str) -> str | None:
    """Why `name` cannot be a rig env entry, or None if it can."""
    if not _ENV_NAME_RE.match(name):
        return f"{name!r} is not a usable environment variable name"
    if name in TURN_OWNED_ENV or name.startswith(TURN_OWNED_ENV_PREFIX):
        return (
            f"{name} belongs to the turn, not the rig — r4t sets it per member "
            f"and a rig may not override it"
        )
    return None


def _split_env_key(key: str) -> str | None:
    """The variable name in an `env.<NAME>` setting key, else None. Only the
    prefix case-folds; environment variable names are case-sensitive."""
    if key[: len(ENV_SETTING_PREFIX)].lower() != ENV_SETTING_PREFIX:
        return None
    return key[len(ENV_SETTING_PREFIX):].strip()


CONFIGURABLE_INT_KEYS = (
    "concurrency",
    "history_max_bytes",
    "history_body_max",
    "prompt_body_max",
    "echo_max_chars",
)
CONFIGURABLE_FLOAT_KEYS = ("rig_budget_max", "rig_budget_earn_per_hour")
CONFIGURABLE_BOOL_KEYS = ("echo", "mcp")
CONFIGURABLE_RIG_KEYS = (
    "concurrency",
    "rig_budget_max",
    "rig_budget_earn_per_hour",
    "history_max_bytes",
    "history_body_max",
    "prompt_body_max",
    "model",
    "echo",
    "echo_max_chars",
    "mcp",
)


@dataclass
class RigSetting:
    """One effective rig setting and where it comes from. `explicit` is True
    only when the value is written in rigs.json; inherited tier defaults and
    built-in defaults are never materialized (so `rig swap` can re-resolve)."""

    key: str
    value: object
    source: str
    explicit: bool

    def display(self) -> str:
        if self.value is None:
            return "unset"
        if isinstance(self.value, bool):
            return "true" if self.value else "false"
        if isinstance(self.value, float):
            return f"{self.value:g}"
        return str(self.value)


def resolve_config_path(raw: str | None) -> Path:
    if raw:
        return Path(raw).expanduser().resolve()
    return default_config_path()


def _unknown_setting_error(key: str) -> RigError:
    valid = ", ".join(CONFIGURABLE_RIG_KEYS) + ", env.<NAME>"
    return RigError(
        f"unknown rig setting {key!r} "
        f"(try: r4t rig set <rig> <key> <value> with one of: {valid})"
    )


def _normalize_setting_key(key: str) -> tuple[str, str | None]:
    """(canonical setting key, env variable name or None). Raises for a key no
    rig has."""
    key = key.strip()
    name = _split_env_key(key)
    if name is not None:
        problem = _env_name_problem(name)
        if problem:
            raise RigError(
                f"{problem} "
                f"(try: r4t rig set <rig> env.ENABLE_PROMPT_CACHING_1H 1)"
            )
        return f"{ENV_SETTING_PREFIX}{name}", name
    key = key.lower()
    if key not in CONFIGURABLE_RIG_KEYS:
        raise _unknown_setting_error(key)
    return key, None


def setting_label(key: str) -> str:
    """The canonical spelling of a setting key, for CLI messages. Scalar keys
    case-fold; an `env.<NAME>` key keeps NAME as written."""
    return _normalize_setting_key(key)[0]


def _entry_env(entry: dict) -> dict:
    """The rig entry's live env map, or an empty stand-in when it has none."""
    raw = entry.get("env")
    return raw if isinstance(raw, dict) else {}


def _rig_entry(path: Path, rig_name: str) -> tuple[str, dict, dict]:
    """Return (rig_key, entry, payload) for a rig that must already exist."""
    rig_key = _validate_rig_name(rig_name)
    payload = _load_config_payload(path)
    entry = payload.get(rig_key)
    if not isinstance(entry, dict):
        raise RigError(
            f"no rig {rig_key!r} in {path} (try: r4t rig add {rig_key} <preset>)"
        )
    return rig_key, entry, payload


def _entry_preset(entry: dict) -> str | None:
    preset = entry.get("preset")
    if isinstance(preset, str) and preset.strip():
        return preset.strip().lower()
    return None


def _resolve_setting(entry: dict, key: str) -> RigSetting:
    preset = _entry_preset(entry)
    if key == "model":
        if entry.get("model"):
            return RigSetting("model", str(entry["model"]), "explicit", True)
        return RigSetting(
            "model", None, "preset default" if preset else "built-in default", False
        )
    if key == "concurrency":
        if "concurrency" in entry:
            return RigSetting(key, int(entry["concurrency"]), "explicit", True)
        return RigSetting(key, DEFAULT_CONCURRENCY, "built-in default", False)
    if key == "mcp":
        if key in entry:
            return RigSetting(key, bool(entry[key]), "explicit", True)
        return RigSetting(
            key,
            mcp_enabled(None, preset),
            f"from preset {preset}" if preset else "built-in default",
            False,
        )
    if key in CONFIGURABLE_BOOL_KEYS:
        if key in entry:
            return RigSetting(key, bool(entry[key]), "explicit", True)
        return RigSetting(key, False, "built-in default", False)
    if key == "echo_max_chars":
        if "echo_max_chars" in entry:
            return RigSetting(key, int(entry["echo_max_chars"]), "explicit", True)
        return RigSetting(key, DEFAULT_ECHO_MAX_CHARS, "built-in default", False)
    if key in CONFIGURABLE_FLOAT_KEYS:
        if key in entry:
            return RigSetting(key, float(entry[key]), "explicit", True)
        return RigSetting(key, None, "built-in default", False)
    if key in entry:
        return RigSetting(key, int(entry[key]), "explicit", True)
    if preset and HARNESS_PRESETS.get(preset, {}).get("text_tier"):
        return RigSetting(key, text_defaults(preset)[key], f"from preset {preset}", False)
    return RigSetting(key, text_defaults(None)[key], "built-in default", False)


def rig_settings(path: Path, rig_name: str) -> list[RigSetting]:
    """Every configurable setting for a rig, effective value + source. The
    `env` map has no defaults to inherit, so only the pairs the rig carries
    show up."""
    _, entry, _ = _rig_entry(path, rig_name)
    rows = [_resolve_setting(entry, key) for key in CONFIGURABLE_RIG_KEYS]
    rows += [
        RigSetting(f"{ENV_SETTING_PREFIX}{name}", str(value), "explicit", True)
        for name, value in sorted(_entry_env(entry).items())
    ]
    return rows


def rig_setting(path: Path, rig_name: str, key: str) -> RigSetting:
    key, env_name = _normalize_setting_key(key)
    _, entry, _ = _rig_entry(path, rig_name)
    if env_name is not None:
        env_map = _entry_env(entry)
        if env_name in env_map:
            return RigSetting(key, str(env_map[env_name]), "explicit", True)
        return RigSetting(key, None, "not set", False)
    return _resolve_setting(entry, key)


_BOOL_WORDS = {"true": True, "on": True, "false": False, "off": False}


def _parse_setting_bool(key: str, raw: object) -> bool:
    flag = _BOOL_WORDS.get(str(raw).strip().lower())
    if flag is None:
        raise RigError(
            f"{key} must be true or false, got {raw!r} "
            f"(try: r4t rig set <rig> {key} true)"
        )
    return flag


def _parse_setting_number(key: str, raw: object) -> int | float:
    text = str(raw).strip()
    try:
        number = float(text)
    except ValueError:
        raise RigError(
            f"{key} must be a number, got {raw!r} "
            f"(try: r4t rig set <rig> {key} <number>)"
        )
    if number <= 0:
        raise RigError(f"{key} must be a positive number, got {raw!r}")
    if key in CONFIGURABLE_INT_KEYS:
        if number != int(number):
            raise RigError(f"{key} must be a whole number, got {raw!r}")
        return int(number)
    return number


def set_rig_model(path: Path, rig_name: str, model: str) -> None:
    """Re-resolve a rig's invoke for a new --model through its recorded preset,
    exactly the way `rig add --model` does. agy keeps its live resolver; a rig
    with no recorded preset errors, because there is nothing to substitute into."""
    rig_key, entry, payload = _rig_entry(path, rig_name)
    preset = _entry_preset(entry)
    if preset is None:
        raise RigError(
            f"rig {rig_key!r} has no recorded preset to re-resolve model through "
            f"(try: r4t rig swap {rig_key} <preset> --model {model})"
        )
    entry["invoke"] = build_preset_invoke(preset, model=model)
    entry.pop("model", None)
    entry.pop("model_resolver", None)
    if HARNESS_PRESETS.get(preset, {}).get("model_resolver"):
        entry["model"] = model.strip()
        entry["model_resolver"] = HARNESS_PRESETS[preset]["model_resolver"]
    atomic_write_json(path, payload)


def set_rig_value(path: Path, rig_name: str, key: str, value: object) -> RigSetting:
    """Write one explicit rig setting. Numeric keys validate as numbers; `model`
    re-resolves the invoke through the preset; `env.<NAME>` writes one static
    harness variable. Returns the resulting setting."""
    key, env_name = _normalize_setting_key(key)
    if env_name is not None:
        text = str(value)
        _, entry, payload = _rig_entry(path, rig_name)
        entry["env"] = {**_entry_env(entry), env_name: text}
        atomic_write_json(path, payload)
        return RigSetting(key, text, "explicit", True)
    rig_key = _validate_rig_name(rig_name)
    if key == "model":
        model = str(value).strip()
        set_rig_model(path, rig_key, model)
        return RigSetting("model", model, "explicit", True)
    if key in CONFIGURABLE_BOOL_KEYS:
        flag = _parse_setting_bool(key, value)
        rig_key, entry, payload = _rig_entry(path, rig_name)
        if key == "mcp" and flag:
            preset = _entry_preset(entry)
            if not HARNESS_PRESETS.get(preset or "", {}).get("mcp"):
                raise RigError(
                    f"leave mcp off for rig {rig_key!r}: "
                    f"{mcp_unsupported_reason(preset)} "
                    f"(try: r4t rig swap {rig_key} <one of: {', '.join(mcp_presets())}>)"
                )
        entry[key] = flag
        atomic_write_json(path, payload)
        return RigSetting(key, flag, "explicit", True)
    number = _parse_setting_number(key, value)
    _, entry, payload = _rig_entry(path, rig_name)
    entry[key] = number
    atomic_write_json(path, payload)
    return RigSetting(key, number, "explicit", True)


def unset_rig_value(path: Path, rig_name: str, key: str) -> bool:
    """Drop an explicit setting so it falls back to preset tier / built-in
    default. Returns True if something was removed, False if it was not
    explicitly set (a friendly no-op). `model` re-resolves the invoke to the
    preset's base."""
    key, env_name = _normalize_setting_key(key)
    rig_key, entry, payload = _rig_entry(path, rig_name)
    if env_name is not None:
        env_map = _entry_env(entry)
        if env_name not in env_map:
            return False
        del env_map[env_name]
        # An empty map is no map: nothing is inherited, so the key would only
        # sit in rigs.json saying nothing.
        if not env_map:
            del entry["env"]
        atomic_write_json(path, payload)
        return True
    if key == "model":
        preset = _entry_preset(entry)
        if preset is None:
            raise RigError(
                f"rig {rig_key!r} has no recorded preset (try: r4t rig list)"
            )
        base_invoke = build_preset_invoke(preset)
        had_model = "model" in entry or entry.get("invoke") != base_invoke
        entry["invoke"] = base_invoke
        entry.pop("model", None)
        entry.pop("model_resolver", None)
        if had_model:
            atomic_write_json(path, payload)
        return had_model
    if key not in entry:
        return False
    del entry[key]
    atomic_write_json(path, payload)
    return True


def _positive_number(raw: object, default: float) -> tuple[float, str | None]:
    if raw is None:
        return default, None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return default, f"expected a number, got {raw!r}"
    if raw <= 0:
        return default, f"must be positive, got {raw!r}"
    return float(raw), None


def _normalize_invoke(invoke: object) -> tuple[list, str | None]:
    """Accept one argv (list of str) or a pool (list of argvs). Every argv
    must be non-empty strings with a {prompt} placeholder somewhere."""
    if not isinstance(invoke, list) or not invoke:
        return [], "invoke must be a non-empty list"
    if all(isinstance(a, str) for a in invoke):
        variants: list[list[str]] = [invoke]
        flat = True
    elif all(isinstance(a, list) for a in invoke):
        variants = invoke
        flat = False
    else:
        return [], "invoke must be one argv (strings) or a pool (list of argvs)"
    for i, argv in enumerate(variants):
        if not argv or not all(isinstance(a, str) for a in argv):
            return [], f"invoke variant {i} must be a non-empty list of strings"
        if not any(PROMPT_PLACEHOLDER in a for a in argv):
            return [], f"invoke variant {i} has no {{prompt}} placeholder"
    return (list(invoke) if flat else [list(v) for v in variants]), None


def _parse_rig(name: str, raw: object) -> Rig:
    rig = Rig(name=name.lower())
    if not isinstance(raw, dict):
        rig.error = "rig definition must be an object"
        return rig
    invoke, err = _normalize_invoke(raw.get("invoke"))
    if err:
        rig.error = err
        return rig
    rig.invoke = invoke
    rig.preset = _entry_preset(raw)

    resolver = raw.get("model_resolver")
    if resolver is not None:
        rig.model_resolver = str(resolver)
        rig.model = str(raw.get("model") or "").strip() or None

    problems: list[str] = []
    rig.timeout_seconds, err = _positive_number(
        raw.get("timeout_seconds"), DEFAULT_TIMEOUT_SECONDS
    )
    if err:
        problems.append(f"timeout_seconds: {err}")
    concurrency, err = _positive_number(raw.get("concurrency"), DEFAULT_CONCURRENCY)
    if err:
        problems.append(f"concurrency: {err}")
    rig.concurrency = int(concurrency)
    max_sends, err = _positive_number(
        raw.get("max_sends_per_turn"), DEFAULT_MAX_SENDS_PER_TURN
    )
    if err:
        problems.append(f"max_sends_per_turn: {err}")
    rig.max_sends_per_turn = int(max_sends)
    rig.budget_max, err = _positive_number(raw.get("budget_max"), DEFAULT_BUDGET_MAX)
    if err:
        problems.append(f"budget_max: {err}")
    rig.budget_earn_per_hour, err = _positive_number(
        raw.get("budget_earn_per_hour"), DEFAULT_BUDGET_EARN_PER_HOUR
    )
    if err:
        problems.append(f"budget_earn_per_hour: {err}")

    # History/prompt sizing rides the rig, defaulted by the preset's text tier
    # — a 0.6B local member and an agy seat should not share a history budget.
    # Explicit values in rigs.json win; a rig with no `preset` (custom CLI)
    # gets the conservative small tier.
    defaults = text_defaults(rig.preset)
    for knob in ("history_max_bytes", "history_body_max", "prompt_body_max"):
        value, err = _positive_number(raw.get(knob), defaults[knob])
        if err:
            problems.append(f"{knob}: {err}")
        setattr(rig, knob, int(value))

    raw_echo = raw.get("echo")
    if raw_echo is not None:
        if not isinstance(raw_echo, bool):
            problems.append(f"echo: expected true or false, got {raw_echo!r}")
        else:
            rig.echo = raw_echo
    echo_max, err = _positive_number(raw.get("echo_max_chars"), DEFAULT_ECHO_MAX_CHARS)
    if err:
        problems.append(f"echo_max_chars: {err}")
    rig.echo_max_chars = int(echo_max)

    # A rig-level Framing default: same three forms as the roster line
    # (roster.parse_framing), but unquoted — the value is already a JSON
    # string, so there is no "off"/"default" keyword collision to guard
    # against with quote marks.
    raw_framing = raw.get("framing")
    if raw_framing is not None:
        if not isinstance(raw_framing, str):
            problems.append(f"framing: expected a string, got {raw_framing!r}")
        else:
            rig.framing = parse_framing(raw_framing, quoted=False)

    # A hand-edited `mcp: true` on a preset with no per-invocation idiom fails
    # the rig closed rather than running turns that quietly have no tool.
    raw_mcp = raw.get("mcp")
    if raw_mcp is not None:
        if not isinstance(raw_mcp, bool):
            problems.append(f"mcp: expected true or false, got {raw_mcp!r}")
        elif raw_mcp and not rig.mcp_idiom:
            problems.append(
                f"mcp: {mcp_unsupported_reason(rig.preset)} "
                f"(try: r4t rig swap {rig.name} <one of: {', '.join(mcp_presets())}>)"
            )
        else:
            rig.mcp = raw_mcp

    # A rig env entry the turn owns, or a value that is not a plain string,
    # fails the rig closed here — the operator hears about it at `rig get` /
    # `roster check` / the first turn rather than losing the entry silently.
    raw_env = raw.get("env")
    if raw_env is not None:
        if not isinstance(raw_env, dict):
            problems.append('env: expected an object of "NAME": "value" pairs')
        else:
            for name, value in raw_env.items():
                problem = _env_name_problem(str(name))
                if problem:
                    problems.append(f"env: {problem}")
                elif not isinstance(value, str):
                    problems.append(f"env.{name}: expected a string, got {value!r}")
                else:
                    rig.env[str(name)] = value

    # The rig spend bucket is opt-in: absent leaves both None and the rig gate
    # off. If present, both knobs are required — a real subscription always
    # declares a refill rate, and a max without one would rest forever.
    raw_rig_max = raw.get("rig_budget_max")
    raw_rig_earn = raw.get("rig_budget_earn_per_hour")
    if raw_rig_max is not None:
        rig.rig_budget_max, err = _positive_number(raw_rig_max, 0.0)
        if err:
            problems.append(f"rig_budget_max: {err}")
        if raw_rig_earn is None:
            problems.append("rig_budget_max set but rig_budget_earn_per_hour missing")
        else:
            rig.rig_budget_earn_per_hour, err = _positive_number(raw_rig_earn, 0.0)
            if err:
                problems.append(f"rig_budget_earn_per_hour: {err}")
    elif raw_rig_earn is not None:
        problems.append("rig_budget_earn_per_hour set but rig_budget_max missing")

    if problems:
        rig.error = "; ".join(problems)
    return rig


def _non_negative_number(raw: object, default: float, label: str) -> float:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
        raise RigError(f"{label} must be a non-negative number, got {raw!r}")
    return float(raw)


def _parse_throttle(raw: object) -> Throttle:
    if not isinstance(raw, dict):
        raise RigError('"throttle" must be an object')
    return Throttle(
        max_concurrent=int(
            _non_negative_number(
                raw.get("max_concurrent"),
                DEFAULT_MAX_CONCURRENT,
                "throttle.max_concurrent",
            )
        ),
        min_seconds_between_turn_starts=_non_negative_number(
            raw.get("min_seconds_between_turn_starts"),
            DEFAULT_MIN_SECONDS_BETWEEN_TURN_STARTS,
            "throttle.min_seconds_between_turn_starts",
        ),
    )


def load_rig_config(path: Path) -> RigConfig:
    if not path.is_file():
        return RigConfig(path=path, missing=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise RigError(f"cannot load rig config {path}: {e}") from e
    if not isinstance(data, dict):
        raise RigError(f"rig config {path} must be a JSON object")

    config = RigConfig(path=path)
    for key, value in data.items():
        if key.startswith("_"):
            continue
        if key == "pins":
            if isinstance(value, dict):
                config.pins = {
                    str(agent).lower(): str(rig).strip().lower()
                    for agent, rig in value.items()
                    if not str(agent).startswith("_")
                }
            continue
        if key == "throttle":
            config.throttle = _parse_throttle(value)
            continue
        if key == "breaker_cap":
            n = _non_negative_number(value, 0, key)
            if n <= 0:
                raise RigError(f"{key} must be positive, got {value!r}")
            setattr(config, key, int(n))
            continue
        if key == "log_retention_days":
            config.log_retention_days = int(
                _non_negative_number(value, DEFAULT_LOG_RETENTION_DAYS, key)
            )
            continue
        if key == "quiet_task_seconds":
            # 0 is OFF, matching what the sweep has always done with <= 0.
            # The loader used to reject it, so the obvious way to disable the
            # sweep was a config error — and a config error fails the WHOLE
            # dispatch path, which is an outage.
            config.quiet_task_seconds = _non_negative_number(
                value, DEFAULT_QUIET_TASK_SECONDS, key
            )
            continue
        if key in (
            "cell_budget_max",
            "cell_budget_earn_per_hour",
            "breaker_cooldown_seconds",
        ):
            n = _non_negative_number(value, 0, key)
            if n <= 0:
                raise RigError(f"{key} must be positive, got {value!r}")
            setattr(config, key, n)
            continue
        config.rigs[key.lower()] = _parse_rig(key, value)
    return config


def default_config_payload() -> dict:
    """The `r4t init` starter config: two symbolic rigs on the cheapest
    common harness CLI, plus notes for swapping in other CLIs. Every governance
    knob is left to its default."""
    return {
        "_notes": [
            "Generated by `r4t init`. Rig names are SYMBOLIC — the roster's",
            "Rig lines reference them; only this out-of-repo file says what",
            "actually runs. Swap invoke for your CLI, or run:",
            "  r4t rig presets",
            "  r4t rig add <rig> <preset>",
            "Presets mirror apps/a8s/definitions/ (claude, codex, cursor, ...).",
            "invoke may also be a LIST of argvs (a pool, rotated round-robin).",
            "All governance knobs default sanely; see docs/r4t-rigs.md.",
        ],
        "leader": {
            "invoke": ["opencode", "run", "--auto", "--dir", "{workdir}", "{prompt}"],
        },
        "member": {
            "invoke": ["opencode", "run", "--auto", "--dir", "{workdir}", "{prompt}"],
        },
    }
