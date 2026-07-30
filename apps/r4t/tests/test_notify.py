"""Issue #337 — dispatch's tell must not depend on the operator's PATH.

`default_tell` once spawned the bare `tell` command, which exists only where
install.sh has edited the shell PATH. On a CI runner or container the spawn
raised FileNotFoundError mid-release, killing dispatch after QUEUED and turning
every wake into a retry loop."""
import subprocess
import sys

import notify


class TestDefaultTell:
    def test_resolves_the_sibling_a8s_entry_point(self):
        assert notify.A8S_PY.is_file()
        assert notify.A8S_PY.name == "a8s.py"

    def test_spawns_path_independent_argv(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            subprocess, "run", lambda argv, **kw: calls.append(argv)
        )
        notify.default_tell("bob", "status green")
        assert calls == [
            [sys.executable, str(notify.A8S_PY), "tell", "bob", "status green"]
        ]
