"""Portable org resolution — where the roster's documents live vs. where it works.

By default a roster lives IN its repo: ROSTER.md and MISSION.md sit at the repo
root, which is also where turns run and commits land. This is the slow-furnace
default — a proven structure graduates INTO the repo.

A portable org splits the two: an org directory holds ROSTER.md + MISSION.md +
a small `r4t-org.json` naming the workplace repo. That lets two org dirs — same
MISSION, different ROSTERs — point at two clones of one project (the A/B case:
a novella-writing experiment with identical intent and different casts). Roster
state never collides because it is keyed per a8s node, not per repo.

Precedence, decided once, here:

- If `<root>/r4t-org.json` exists and names a `repo`, `<root>` is the ORG DIR:
  ROSTER.md and MISSION.md are read from `<root>`, and turns run in `repo`
  (relative `repo` paths resolve against the org dir).
- Otherwise `<root>` is both org dir and workplace — the in-repo default. The
  config file may still be present WITHOUT a `repo` key: it then exists purely
  to carry org settings (below) that must travel with the org, not the machine.

Org settings (every "this-or-that" is a knob with a default and a declared
home — this file is the org-level home):

- `comms` (`open` | `closed`, default `open`): `open` delivers a tell to any
  valid roster member; `closed` reroutes non-tree-adjacent tells through the
  sender's lead (the military model). Info hiding stays at the prompt level in
  both modes — a learned address delivers even when the prompt does not list it.
- `leader_sees_lateral` (bool, default `false`): when on, a lateral (peer)
  delivery also lands a read-only `class=auto` copy on the lead — no turn is
  burned to notify.
- `egress` (bool, default `true`): when on, the topmost leader alone may
  originate external mail; when off, no member may, and external `to`s redirect
  to the top leader (the garden's single voice).
- `doorbell_check` (string, default absent): a shell command run before the org
  may ring an absent human's doorbell. Exit 0 lets the ring through; nonzero
  parks the message without ringing (the gate protects attention, not the
  mailbox). Absent or empty is today's behavior — no gate.
- `run_as` / `container` (+ `container_args`, all default absent): OS-level
  isolation for every member turn. Isolation is a
  per-project decision — one Unix user or one container image serves the org's
  whole roster whatever rig runs a member — so it lives here, not on the
  machine-global rig. `run_as` and `container` are mutually exclusive.
  The mechanics live in isolate.py; dispatch wraps the turn from this setting.

Graduation is trivial: copy ROSTER.md + MISSION.md into the repo and drop the
`repo` key (or the whole file, if it carries no settings). Resolution falls
back to the in-repo default with no other change.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from isolate import Isolation

ORG_CONFIG_NAME = "r4t-org.json"

COMMS_OPEN = "open"
COMMS_CLOSED = "closed"
COMMS_MODES = (COMMS_OPEN, COMMS_CLOSED)


class OrgError(Exception):
    pass


@dataclass
class Org:
    dir: Path
    workplace: Path
    comms: str = COMMS_OPEN
    leader_sees_lateral: bool = False
    egress: bool = True
    doorbell_check: str | None = None
    isolation: Isolation = field(default_factory=Isolation)

    @property
    def is_portable(self) -> bool:
        return self.dir != self.workplace


def org_config_path(root: Path) -> Path:
    return root / ORG_CONFIG_NAME


def _read_config(root: Path) -> dict:
    cfg = org_config_path(root)
    if not cfg.is_file():
        return {}
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise OrgError(f"cannot read org config {cfg}: {e}") from e
    if not isinstance(data, dict):
        raise OrgError(f"org config {cfg} must be a JSON object")
    return data


def _resolve_repo(root: Path, data: dict) -> Path | None:
    raw = str(data.get("repo", "")).strip()
    if not raw:
        return None
    repo = Path(raw).expanduser()
    if not repo.is_absolute():
        repo = (root / repo).resolve()
    return repo


def _parse_bool(raw: object, default: bool, key: str) -> tuple[bool, str | None]:
    if raw is None:
        return default, None
    if not isinstance(raw, bool):
        return default, f'org config "{key}" must be true or false (got {raw!r})'
    return raw, None


def _parse_str(raw: object, key: str) -> tuple[str | None, str | None]:
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        return None, f'org config "{key}" must be a string (got {raw!r})'
    return raw, None


def _parse_isolation(data: dict) -> tuple[Isolation, list[str]]:
    """OS-level isolation for every member turn. `run_as` and `container` are
    mutually exclusive; both set is a config error and the org degrades to no
    isolation (fail closed reports through check_org). `container_args` needs a
    `container` to attach to."""
    errors: list[str] = []
    iso = Isolation()
    run_as = data.get("run_as")
    container = data.get("container")
    if run_as is not None and container is not None:
        errors.append(
            'org config "run_as" and "container" are mutually exclusive; set only one'
        )
    else:
        if run_as is not None:
            if not isinstance(run_as, str) or not run_as.strip():
                errors.append('org config "run_as" must be a non-empty username string')
            else:
                iso.run_as = run_as.strip()
        if container is not None:
            if not isinstance(container, str) or not container.strip():
                errors.append('org config "container" must be a non-empty image string')
            else:
                iso.container = container.strip()
    container_args = data.get("container_args")
    if container_args is not None:
        if not isinstance(container_args, list) or not all(
            isinstance(a, str) for a in container_args
        ):
            errors.append('org config "container_args" must be a list of strings')
        elif iso.container is None:
            errors.append('org config "container_args" set but "container" is not')
        else:
            iso.container_args = list(container_args)
    return iso, errors


def _parse_settings(data: dict) -> tuple[dict, list[str]]:
    errors: list[str] = []
    comms = data.get("comms", COMMS_OPEN)
    if comms not in COMMS_MODES:
        errors.append(f'org config "comms" must be "open" or "closed" (got {comms!r})')
        comms = COMMS_OPEN
    lsl, err = _parse_bool(data.get("leader_sees_lateral"), False, "leader_sees_lateral")
    if err:
        errors.append(err)
    egress, err = _parse_bool(data.get("egress"), True, "egress")
    if err:
        errors.append(err)
    doorbell_check, err = _parse_str(data.get("doorbell_check"), "doorbell_check")
    if err:
        errors.append(err)
    isolation, iso_errors = _parse_isolation(data)
    errors.extend(iso_errors)
    return {
        "comms": comms,
        "leader_sees_lateral": lsl,
        "egress": egress,
        "doorbell_check": doorbell_check,
        "isolation": isolation,
    }, errors


def load_org(root: Path) -> Org:
    """Resolve `root` to (org dir, workplace) and org settings for path building
    and dispatch. Never raises — a malformed org config degrades to the in-repo
    default with default settings; `check_org` is the boundary that reports it."""
    try:
        data = _read_config(root)
    except OrgError:
        data = {}
    try:
        repo = _resolve_repo(root, data)
    except (OSError, ValueError):
        repo = None
    settings, _errors = _parse_settings(data)
    return Org(dir=root, workplace=repo or root, **settings)


def check_org(root: Path) -> list[str]:
    """Validation for `roster check`: a malformed org config, a bad setting
    value, or a workplace repo that does not exist. Empty when there is no org
    config (in-repo) and no problems."""
    try:
        data = _read_config(root)
    except OrgError as e:
        return [str(e)]
    _settings, problems = _parse_settings(data)
    repo = _resolve_repo(root, data)
    if repo is not None and not repo.is_dir():
        problems.append(
            f"org workplace {repo} does not exist (create it or fix {ORG_CONFIG_NAME})"
        )
    return problems
