"""OS-level isolation for turn invocation — run-as-user and container variants.

The through-line: harness sandbox flags are not a
security boundary; the operating system is. An ORG may name `run_as` (a Unix
user with no sudo) or `container` (an image) and every member turn runs fully
permissive INSIDE that boundary regardless of rig — isolation is a per-project
decision, so it lives with the org (org.py), not the machine-global rig.
Machinery outside, hands inside: r4t/a8s code runs as the operator, and the
boundary applies at the moment a member's turn invoke runs. r4t never provisions
the boundary — it verifies operator-provisioned prerequisites and fails closed
with an action-first error — except for the shared message dirs, which are
r4t's own state and are re-asserted to the correct owner-group/mode before
every turn.

Both wrappers are pure argv builders so the exact shape is unit-testable
against fake `sudo`/`docker` binaries; nothing here shells out except the
prereq probes, the member-home file read/write, and the container kill, which
run the real tool by name.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

if sys.platform != "win32":
    import pwd
else:
    pwd = None  # type: ignore[assignment]

PROBE_TIMEOUT_SECONDS = 10
_POSIX_HOST_REQUIRED = "run_as/container isolation requires a POSIX host"

# The org's isolation choice rides to run_harness through the turn env (like
# R4T_NODE/R4T_MEMBER), so the run_fn contract stays (rig, prompt, cwd, env,
# variant) — dispatch never has to widen it or thread a new positional.
ENV_RUN_AS = "R4T_RUN_AS"
ENV_CONTAINER = "R4T_CONTAINER"
ENV_CONTAINER_ARGS = "R4T_CONTAINER_ARGS"

# The bash the wrapped `sudo` runs: env cannot survive sudoers env_reset, so
# TELL_OUTBOX_DIR rides as $1 and the workplace as $2; $3 counts the further
# `NAME=value` positionals to re-export (the `mcp` knob's env idioms), each one
# a single word so a value with spaces cannot re-split; `exec "$@"` hands the
# remaining positionals to the harness verbatim — no quoted command string, so
# quoting bugs are structurally impossible.
_RUN_AS_BOOTSTRAP = (
    'export TELL_OUTBOX_DIR="$1"; cd "$2"; n="$3"; shift 3; '
    'while [ "$n" -gt 0 ]; do export "$1"; shift; n=$((n - 1)); done; exec "$@"'
)

# A standard system PATH the container shim prepends the a8s client dir to, so
# an unmodified `tell` resolves inside the image. Operators can override with a
# later `-e PATH=...` in container_args (docker keeps the last value).
_CONTAINER_BASE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class IsolationError(Exception):
    pass


def _require_posix_isolation() -> None:
    if sys.platform == "win32":
        raise IsolationError(_POSIX_HOST_REQUIRED)


@dataclass
class Isolation:
    """The org's OS-level boundary. Applies to EVERY
    member turn of the org, whatever rig runs it — one Unix user or one image
    per org. `run_as` and `container` are mutually exclusive; the parse in
    org.py rejects both-set, so a live Isolation carries at most one."""

    run_as: str | None = None
    container: str | None = None
    container_args: list = field(default_factory=list)

    @property
    def active(self) -> bool:
        return bool(self.run_as or self.container)

    def to_env(self) -> dict[str, str]:
        """The env keys run_harness reads to wrap a turn. Empty when isolation
        is off, so a bare org adds nothing to the turn environment."""
        env: dict[str, str] = {}
        if self.run_as:
            env[ENV_RUN_AS] = self.run_as
        elif self.container:
            env[ENV_CONTAINER] = self.container
            if self.container_args:
                env[ENV_CONTAINER_ARGS] = json.dumps(self.container_args)
        return env


def isolation_from_env(env: dict | None) -> Isolation:
    """Reconstruct the org's Isolation from the turn env. Trusted internal
    round-trip of `Isolation.to_env`; a malformed container_args degrades to
    none rather than raising inside a turn."""
    env = env or {}
    run_as = (env.get(ENV_RUN_AS) or "").strip() or None
    if run_as:
        return Isolation(run_as=run_as)
    container = (env.get(ENV_CONTAINER) or "").strip() or None
    if not container:
        return Isolation()
    args: list = []
    raw = env.get(ENV_CONTAINER_ARGS) or ""
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            args = [str(a) for a in parsed]
    return Isolation(container=container, container_args=args)


def a8s_client_dir() -> Path:
    """The repo bin root that holds the `tell` client shim — mounted read-only
    into a container so `tell` resolves on PATH. isolate.py lives at
    apps/r4t/, so two parents up is the bin root."""
    return Path(__file__).resolve().parents[2]


# ---------- run_as ----------

def wrap_run_as(
    argv: list[str],
    user: str,
    staging_dir: str | Path,
    workplace: str | Path,
    *,
    env_pass: dict[str, str] | None = None,
) -> list[str]:
    """Wrap one harness argv in `sudo -u <user>`. `env_pass` names the extra
    environment the harness itself needs across the boundary — anything not
    listed is gone, because sudoers `env_reset` keeps nothing."""
    _require_posix_isolation()
    pairs = [f"{k}={v}" for k, v in (env_pass or {}).items()]
    return [
        "sudo", "-u", user, "bash", "--login", "-c",
        _RUN_AS_BOOTSTRAP, "_", str(staging_dir), str(workplace),
        str(len(pairs)), *pairs, *argv,
    ]


def _run_probe(
    argv: list[str], *, stdin_text: str | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT_SECONDS,
    )


def probe_run_as(user: str, workplace: str | Path) -> str | None:
    """Verify the two operator-provisioned prerequisites for `run_as`: a
    passwordless sudo grant to `user`, and a workplace writable by `user`.
    Returns None when both pass, else an action-first error carrying the fix.
    Never provisions anything."""
    if sys.platform == "win32":
        return _POSIX_HOST_REQUIRED
    try:
        grant = _run_probe(["sudo", "-n", "-u", user, "true"])
    except (OSError, subprocess.TimeoutExpired) as e:
        return (
            f"cannot probe sudo to {user!r}: {e} "
            f"(try: install sudo and grant the router NOPASSWD — see "
            f"docs/r4t-isolation.md)"
        )
    if grant.returncode != 0:
        detail = (grant.stderr or grant.stdout or "").strip()
        return (
            f"no passwordless sudo to user {user!r}"
            + (f": {detail}" if detail else "")
            + " (try: add a NOPASSWD sudoers grant for the wake command — see "
            "docs/r4t-isolation.md)"
        )
    probe = f".r4t-write-probe.{os.getpid()}.{time.time_ns()}"
    try:
        write = _run_probe([
            "sudo", "-n", "-u", user, "bash", "--login", "-c",
            'f="$1/$2"; touch "$f" && rm -f "$f"', "_", str(workplace), probe,
        ])
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"cannot probe workplace write as {user!r}: {e}"
    if write.returncode != 0:
        detail = (write.stderr or write.stdout or "").strip()
        return (
            f"workplace {workplace} not writable by user {user!r}"
            + (f": {detail}" if detail else "")
            + " (try: give the agent user's group g+ws on the workplace — see "
            "docs/r4t-isolation.md)"
        )
    return None


def probe_readable_as(user: str, paths: list[str | Path]) -> str | None:
    """Verify the agent user can read every path a per-turn injection depends on
    (the `mcp` knob's server script, its interpreter, its config file). Returns
    None when all pass, else an action-first error naming the first path that
    does not. A tool the prompt teaches but the boundary cannot start is worse
    than no tool at all, so this fails the turn instead of degrading it."""
    if not paths:
        return None
    if sys.platform == "win32":
        return _POSIX_HOST_REQUIRED
    args = [str(p) for p in paths]
    try:
        probe = _run_probe([
            "sudo", "-n", "-u", user, "bash", "--login", "-c",
            'for p; do [ -r "$p" ] || { echo "$p"; exit 1; }; done', "_", *args,
        ])
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"cannot probe read access as {user!r}: {e}"
    if probe.returncode != 0:
        blocked = (probe.stdout or "").strip().splitlines()
        first = blocked[-1] if blocked else args[0]
        return (
            f"user {user!r} cannot read {first} "
            f"(try: chmod -R a+rX on the r4t checkout and its python, or turn "
            f"the rig's mcp knob off — see docs/r4t-isolation.md)"
        )
    return None


# A file in the agent user's OWN home, reached through the grant `run_as`
# already requires. `$HOME` is expanded by the member's login shell rather than
# the router's — sudoers `env_reset` drops the router's environment, which is
# what makes an otherwise-global harness config per-member. The path rides as
# $1 and the content on stdin, so nothing is quoted into a command string.
_HOME_FILE_READ = 'p="$HOME/$1"; [ -e "$p" ] || exit 0; cat "$p"'
_HOME_FILE_WRITE = 'p="$HOME/$1"; mkdir -p "$(dirname "$p")" && cat > "$p"'


def _home_file_argv(user: str, script: str, relpath: str) -> list[str]:
    return [
        "sudo", "-n", "-u", user, "bash", "--login", "-c", script, "_", relpath,
    ]


def read_home_file_as(user: str, relpath: str) -> str:
    """The text of `relpath` under `user`'s own home, or "" when it is not there
    yet. Anything else raises: a file r4t is about to merge into and write back
    must be read, never guessed at."""
    _require_posix_isolation()
    try:
        got = _run_probe(_home_file_argv(user, _HOME_FILE_READ, relpath))
    except (OSError, subprocess.TimeoutExpired) as e:
        raise IsolationError(f"cannot read ~{user}/{relpath}: {e}") from e
    if got.returncode != 0:
        detail = (got.stderr or got.stdout or "").strip()
        raise IsolationError(
            f"cannot read ~{user}/{relpath}"
            + (f": {detail}" if detail else "")
            + " (try: grant the router NOPASSWD sudo to that user — see "
            "docs/r4t-isolation.md)"
        )
    return got.stdout


def write_home_file_as(user: str, relpath: str, content: str) -> None:
    """Write `content` to `relpath` under `user`'s own home, creating the parent
    dirs. The router has no business writing into another user's home directly,
    so the write goes through the same sudo grant the isolation already needs."""
    _require_posix_isolation()
    try:
        wrote = _run_probe(
            _home_file_argv(user, _HOME_FILE_WRITE, relpath), stdin_text=content
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise IsolationError(f"cannot write ~{user}/{relpath}: {e}") from e
    if wrote.returncode != 0:
        detail = (wrote.stderr or wrote.stdout or "").strip()
        raise IsolationError(
            f"cannot write ~{user}/{relpath}"
            + (f": {detail}" if detail else "")
            + " (try: grant the router NOPASSWD sudo to that user — see "
            "docs/r4t-isolation.md)"
        )


def agent_gid(user: str) -> int | None:
    """The agent user's primary gid, for group-owning the shared dirs. None if
    the user is unknown (the sudo probe reports that failure first)."""
    _require_posix_isolation()
    try:
        return pwd.getpwnam(user).pw_gid
    except KeyError:
        return None


def assert_writable_shared_dir(path: str | Path, gid: int | None) -> None:
    """Re-assert r4t's writable message channel: owned by the router, group set
    to the agent's group, mode 2770 setgid so the agent writes envelopes and
    everything the agent creates stays group-owned. Idempotent; called before
    every turn, not just at setup — re-assertion is what made the precedent
    robust against drift."""
    _require_posix_isolation()
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    if gid is not None:
        os.chown(p, -1, gid)
    os.chmod(p, 0o2770)


def assert_readonly_shared_dir(path: str | Path, gid: int | None) -> None:
    """Re-assert a dir the agent may only READ (delivered files): router-owned,
    agent's group, mode 2750 setgid, no group write bit."""
    _require_posix_isolation()
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    if gid is not None:
        os.chown(p, -1, gid)
    os.chmod(p, 0o2750)


# ---------- container ----------

_SLUG_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", (text or "").strip()) or "x"


def container_name(node: str, member: str, ts: int | None = None) -> str:
    """Deterministic per-turn container name so a timeout can kill by name."""
    stamp = ts if ts is not None else time.time_ns()
    return f"r4t-{_slug(node)}-{_slug(member)}-{stamp}"


def build_container_argv(
    argv: list[str],
    image: str,
    *,
    name: str,
    staging_dir: str | Path,
    workplace: str | Path,
    tell_outbox: str | Path,
    container_args: list[str] | None = None,
    delivered_dir: str | Path | None = None,
    client_dir: str | Path | None = None,
    extra_env: dict[str, str] | None = None,
    extra_ro_dirs: list[str | Path] | None = None,
) -> list[str]:
    """`docker run --rm` with the workplace bind-mounted rw at the same path and
    used as workdir, the staging dir rw at the same path with TELL_OUTBOX_DIR
    injected (no env_reset inside a container), the a8s client ro with a PATH
    shim, an optional delivered-files dir ro, `extra_env`/`extra_ro_dirs` for
    what a per-turn injection needs to see inside (the `mcp` knob), then the
    org's container_args verbatim, then the image and the harness argv. r4t
    never builds, pulls, or inspects the image — a missing image is an ordinary
    turn failure."""
    _require_posix_isolation()
    client = Path(client_dir) if client_dir is not None else a8s_client_dir()
    cmd = [
        "docker", "run", "--rm", "--name", name,
        "-v", f"{workplace}:{workplace}",
        "-w", str(workplace),
        "-v", f"{staging_dir}:{staging_dir}",
        "-e", f"TELL_OUTBOX_DIR={tell_outbox}",
        "-v", f"{client}:{client}:ro",
        "-e", f"PATH={client}:{_CONTAINER_BASE_PATH}",
    ]
    if delivered_dir is not None:
        cmd += ["-v", f"{delivered_dir}:{delivered_dir}:ro"]
    for d in extra_ro_dirs or []:
        cmd += ["-v", f"{d}:{d}:ro"]
    for key, value in (extra_env or {}).items():
        cmd += ["-e", f"{key}={value}"]
    # Last flag wins in docker, so the org's own args can still override any of
    # the above (the option-passthrough principle).
    cmd += list(container_args or [])
    cmd += [image, *argv]
    return cmd


def kill_container(name: str) -> None:
    """Best-effort kill a container by name after a rig timeout; `--rm` reaps
    it. Never raises — the turn already failed."""
    try:
        subprocess.run(
            ["docker", "kill", name],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
