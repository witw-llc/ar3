"""Every home variable is floored per test, and an inherited override does not
survive fixture setup (see lib/ar3/homeguard.py)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from ar3 import homeguard


def test_inner_every_home_variable_is_a_throwaway():
    assert homeguard.still_launched() == [], "an inherited home survived the fixture"
    for name in homeguard.HOME_VARS:
        assert os.environ.get(name) != homeguard.LAUNCHED[name] or not homeguard.LAUNCHED[name]


def test_a_launch_with_inherited_overrides_writes_nothing_there(tmp_path):
    here = Path(__file__).resolve()
    code, output, written = homeguard.sentinel_run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         f"{here.name}::test_inner_every_home_variable_is_a_throwaway"],
        cwd=here.parent, root=tmp_path / "sentinels",
    )
    assert code == 0, output
    assert written == [], written
