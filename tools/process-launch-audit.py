#!/usr/bin/env python3
"""Require shared launch policy at production Python subprocess call sites.

Run from any directory. Test fixtures, vendored code and archived experiments
are excluded; they can deliberately exercise raw platform behavior.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {"tests", "experiments", "_vendor", ".venv", "__pycache__", "node_modules"}
LAUNCHES = {"Popen", "run", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
WINDOWS_FLAGS = {"CREATE_NO_WINDOW", "DETACHED_PROCESS", "CREATE_NEW_CONSOLE", "CREATE_NEW_PROCESS_GROUP", "STARTUPINFO", "STARTF_USESHOWWINDOW"}


def audit(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    subprocess_names = {"subprocess"}
    launch_names = set()
    helper_names = set()
    proc_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    subprocess_names.add(alias.asname or alias.name)
                if alias.name == "ar3.proc":
                    proc_names.add(alias.asname or "ar3.proc")
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if node.module == "subprocess" and alias.name in LAUNCHES:
                    launch_names.add(alias.asname or alias.name)
                if node.module == "ar3.proc" and alias.name == "launch_options":
                    helper_names.add(alias.asname or alias.name)
                if node.module == "ar3" and alias.name == "proc":
                    proc_names.add(alias.asname or alias.name)

    def helper(expr):
        if not isinstance(expr, ast.Call):
            return False
        name = ast.unparse(expr.func)
        return name in helper_names or any(name == f"{p}.launch_options" for p in proc_names)

    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in WINDOWS_FLAGS:
            problems.append((node.lineno, "Windows launch policy belongs in ar3.proc"))
        keys = node.keys if isinstance(node, ast.Dict) else [node.slice] if isinstance(node, ast.Subscript) else []
        if any(isinstance(key, ast.Constant) and key.value == "creationflags" for key in keys):
            problems.append((node.lineno, "consumer constructs Windows launch flags"))
        if not isinstance(node, ast.Call):
            continue
        if any(kw.arg in {"creationflags", "startupinfo"} for kw in node.keywords):
            problems.append((node.lineno, "consumer overrides Windows launch policy"))
        func = node.func
        launch = (
            isinstance(func, ast.Name) and func.id in launch_names
            or isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
            and func.value.id in subprocess_names and func.attr in LAUNCHES
        )
        if launch and not any(kw.arg is None and helper(kw.value) for kw in node.keywords):
            problems.append((node.lineno, "subprocess launch lacks ar3.proc.launch_options"))
    return problems


def self_test() -> None:
    cases = [
        ("import subprocess\nsubprocess.run(['x'])", True),
        ("import subprocess as sp\nsp.Popen(['x'])", True),
        ("from subprocess import run as r\nr(['x'])", True),
        ("import subprocess\nsubprocess.run(['x'], **launch_options())", True),
        ("from ar3.proc import launch_options as opts\nimport subprocess\nsubprocess.run(['x'], **opts())", False),
        ("import ar3.proc as proc\nimport subprocess\nsubprocess.run(['x'], **proc.launch_options(interactive=True))", False),
        ("from ar3.proc import launch_options\nimport subprocess\nsubprocess.run(['x'], creationflags=8, **launch_options())", True),
        ("flags = {'creationflags': 8}", True),
        ("x = subprocess.CREATE_NO_WINDOW", True),
    ]
    for source, fails in cases:
        assert bool(audit(source)) == fails, source
    print(f"process launch audit: {len(cases)} self-tests passed")


def main() -> int:
    if sys.argv[1:] == ["--self-test"]:
        self_test()
        return 0
    count = 0
    failures = 0
    for directory in ("apps", "lib", "tools", ".github"):
        for path in sorted((ROOT / directory).rglob("*.py")):
            relative = path.relative_to(ROOT)
            if SKIP.intersection(relative.parts) or relative.as_posix() == "lib/ar3/proc.py":
                continue
            count += 1
            for line, problem in audit(path.read_text(encoding="utf-8")):
                print(f"{relative}:{line}: {problem}")
                failures += 1
    print(f"process launch audit: {count} files, {failures} defects")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
