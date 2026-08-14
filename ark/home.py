"""One config-home resolver for every Ark app.

Every product's state root resolves the same way: the app's own override env
var wins outright; otherwise `XDG_CONFIG_HOME` (or its `~/.config` default)
picks the base and the app name picks the leaf. a8s alone carries one more
step — a pre-XDG `~/.a8s` layout predates this unification — behind an
explicit opt-in `legacy` param, so existing a8s installs keep resolving
without a migration while r4t and k7e never grow one.

Adopting `XDG_CONFIG_HOME` for a8s (it previously only ever looked under
`~/.config/a8s` or the legacy `~/.a8s`, never the env var) is a deliberate
behavior change: one doctrine for config-home resolution across the suite,
ruled by the owner, and fine to ship pre-1.0 under the scorch-the-earth
policy — there is no migration path to preserve.
"""
from __future__ import annotations

import os
from pathlib import Path


def app_home(app: str, env_override: str | None, legacy: Path | None = None) -> Path:
    """State-root directory for `app`.

    Resolution order:
      1. `env_override` (the caller's own e.g. `A8S_HOME`) if set
      2. `XDG_CONFIG_HOME/<app>` (or `~/.config/<app>` when unset) if that
         directory already exists
      3. `legacy` if given and it already exists (a pre-XDG install)
      4. `XDG_CONFIG_HOME/<app>` (or `~/.config/<app>`) — the default for new
         installs, whether or not it exists yet

    Does not create the directory — callers that write should mkdir.
    """
    override = (env_override or "").strip()
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    config = base / app
    if config.is_dir():
        return config
    if legacy is not None and legacy.is_dir():
        return legacy
    return config
