"""pytest scaffolding for the tool nodes.

Puts `apps/tools/` on the path so tests import a tool module and its shared
runtime directly (`import command_shell`, `import _runtime`), and `lib/` for the
`ar3` foundation. No test imports an a8s module: a tool's contract with a8s is
the wake argv and the `a8s tell` CLI, and the runtime reaches that CLI by
subprocess, so the reply path is exercised through a real `TELL_OUTBOX_DIR`.

`state_guard` is a plugin of the whole session: every test runs with state directories of its own, and a
test that reaches the real ones, or a real Chrome, fails. See its docstring for why it is not a fixture of
a conftest beside the tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PKG))
sys.path.insert(0, str(_PKG.parent.parent / "lib"))

pytest_plugins = ["state_guard"]
