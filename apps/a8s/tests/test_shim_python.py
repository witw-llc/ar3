"""The shims' bash half resolving an interpreter.

python.org's Windows installer ships `python.exe` and no `python3.exe`, so the
bare name the repo used everywhere left a working install with definitions and
shims that die at wake time (#173). The resolution order is python3, python,
then `py -3`, and a candidate has to RUN before it is accepted — on Windows the
first `python` on PATH is often the Microsoft Store alias, a stub that opens the
Store and exits non-zero.

No single machine can exercise the whole chain: a box with a working `python3`
never reaches the second candidate, and a python.org-only box has no `python3`
to reject. So the candidates are faked here and the branches are driven
directly. The shims are bash on every platform, including Git Bash on Windows,
which is what makes this portable.

This covers all four shims rather than a8s alone; it lives here because this is
the suite that already runs real processes, and because the other half of the
same fix — `$PYTHON` in the bundled definitions — is a8s's.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import write_path_executable

REPO_ROOT = Path(__file__).resolve().parents[3]


def _safe_read(path: Path) -> str:
    """Repo-root files are a mixed bag — scripts, VERSION, a PNG one day."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


SHIMS = ["ar3", "a8s", "r4t", "k7e"]
# Resolved before any test narrows PATH, which is the whole point of the setup.
BASH = shutil.which("bash")


def _bin(tmp_path: Path) -> Path:
    """A PATH holding only what the shim's own prologue needs, which is
    `dirname` and nothing else.

    Written rather than linked or copied. `os.symlink` needs a privilege
    Windows does not grant an unprivileged process (WinError 1314) unless
    Developer Mode is on — which would make this file pass for whoever has
    that and fail for everyone else, and did hide all twenty cases on the only
    native-Windows seat we have. Copying the system binary is no better: on
    macOS `/usr/bin/dirname` is SIP-protected and the copy runs but prints
    nothing, which surfaces as `cd: null directory` rather than as an error.

    Four lines of shell owe nothing to either platform.
    """
    d = tmp_path / "bin"
    d.mkdir()
    dirname = d / "dirname"
    dirname.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  */*) printf '%s\\n' \"${1%/*}\" ;;\n"
        "  *) printf '.\\n' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    dirname.chmod(0o755)
    return d


def _candidate(bin_dir: Path, name: str, *, works: bool, log: Path) -> None:
    """A stand-in interpreter. `works=False` is the Store-alias shape: found on
    PATH, exits non-zero, runs nothing."""
    path = bin_dir / name
    if works:
        path.write_text(
            "#!/bin/sh\n"
            # The liveness probe passes `-c ""`; only a real invocation is
            # recorded, so the log names what the shim chose to run.
            'case "$1" in -c) exit 0 ;; esac\n'
            f'printf "%s\\n" "{name} $*" >> "{log}"\n'
            "exit 0\n",
            encoding="utf-8",
        )
    else:
        path.write_text(
            "#!/bin/sh\n"
            'printf "Python was not found; run without arguments to install\\n" >&2\n'
            "exit 9009\n",
            encoding="utf-8",
        )
    path.chmod(0o755)


def _run(shim: str, bin_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, str(REPO_ROOT / shim), "--version"],
        env={"PATH": str(bin_dir)},
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(BASH is None, reason="the shims' resolution half is bash")
@pytest.mark.parametrize("shim", SHIMS)
class TestInterpreterResolution:
    def test_python3_is_preferred_when_it_runs(self, shim, tmp_path):
        d, log = _bin(tmp_path), tmp_path / "log"
        _candidate(d, "python3", works=True, log=log)
        _candidate(d, "python", works=True, log=log)
        assert _run(shim, d).returncode == 0
        assert log.read_text().startswith("python3 ")

    def test_a_found_but_unrunnable_python3_falls_through_to_python(
        self, shim, tmp_path
    ):
        """The Store-alias case, and the whole reason the probe runs a
        candidate instead of trusting `command -v`."""
        d, log = _bin(tmp_path), tmp_path / "log"
        _candidate(d, "python3", works=False, log=log)
        _candidate(d, "python", works=True, log=log)
        assert _run(shim, d).returncode == 0
        assert log.read_text().startswith("python ")

    def test_py_is_the_last_resort(self, shim, tmp_path):
        d, log = _bin(tmp_path), tmp_path / "log"
        _candidate(d, "py", works=True, log=log)
        assert _run(shim, d).returncode == 0
        assert log.read_text().startswith("py -3 ")

    def test_every_candidate_unrunnable_reports_rather_than_hangs(
        self, shim, tmp_path
    ):
        d, log = _bin(tmp_path), tmp_path / "log"
        for name in ("python3", "python", "py"):
            _candidate(d, name, works=False, log=log)
        result = _run(shim, d)
        assert result.returncode == 127
        assert "no working python3, python, or py on PATH" in result.stderr
        assert not log.exists()

    def test_nothing_on_path_reports_rather_than_hangs(self, shim, tmp_path):
        result = _run(shim, _bin(tmp_path))
        assert result.returncode == 127
        assert shim in result.stderr


class TestCmdShimsPropagateTheExitCode:
    """Two ways a `.cmd` shim lies, and they pull against each other.

    `cmd.exe` expands `%VAR%` for a whole parenthesised block at parse time,
    so `exit /b %ERRORLEVEL%` written inside `if ... ( ... )` returns the value
    ERRORLEVEL held *before* the block ran. Every AR3 CLI on Windows reported
    success unconditionally.

    Delayed expansion fixes that read and breaks the arguments. `%*` is
    substituted into the line first and `!` is expanded in the result second,
    so `tell bob "ship!"` loses the bang and a body containing `!PATH!` becomes
    environment data. A reviewer found that on the first fix: it traded a false
    exit code for silent argument corruption on every entrypoint.

    Labels answer both. Each interpreter runs on its own line, outside any
    block, and `%ERRORLEVEL%` is read on the next line at that line's own parse
    time — with delayed expansion off.

    Asserted on the file rather than by running it. `cmd.exe` exists on exactly
    one platform, `release.yml` has no Windows job (#164), and the Windows seat
    is the only thing that would otherwise catch a revert. A static check runs
    everywhere and pins the lines that matter.
    """

    # Globbed, not enumerated: a seventh shim added later is covered the day
    # it lands. An enumerated list would ship it unguarded and stay green.
    SHIMS = sorted(path.name for path in REPO_ROOT.glob("*.cmd"))
    # `parametrize` over an empty list yields no tests and reports green, so
    # the glob itself is checked. Six ship today; the floor only moves up.
    MINIMUM_SHIMS = 6

    @staticmethod
    def _lines(name):
        """Executable lines only. A `rem` explaining why `%*` is dangerous is
        not an invocation of `%*`, and counting it as one made every check
        below fail on its own documentation."""
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        return [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().lower().startswith(("rem ", "::"))
        ]

    def test_the_glob_found_the_shims(self):
        assert len(self.SHIMS) >= self.MINIMUM_SHIMS, self.SHIMS

    @pytest.mark.parametrize("name", SHIMS)
    def test_delayed_expansion_is_never_enabled(self, name):
        """The regression this class exists to stop happening twice. A shim
        that turns delayed expansion on eats `!` out of every argument it
        forwards, and nothing downstream can tell that it happened."""
        text = " ".join(self._lines(name)).lower()
        assert "enabledelayedexpansion" not in text, (
            f"{name} enables delayed expansion, so `!` in a forwarded "
            "argument is silently eaten — read %ERRORLEVEL% outside a "
            "parenthesised block instead"
        )

    @pytest.mark.parametrize("name", SHIMS)
    def test_no_line_opens_a_parenthesised_block(self, name):
        """`%ERRORLEVEL%` is only trustworthy where no block encloses it. The
        absence of blocks is what makes the parse-time read correct, so that
        is the property to pin rather than the read itself."""
        offenders = [line for line in self._lines(name) if line.endswith("(")]
        assert not offenders, (
            f"{name} opens a parenthesised block: {offenders} — every %VAR% "
            "inside one is expanded before the block runs"
        )

    @pytest.mark.parametrize("name", SHIMS)
    def test_every_forwarded_invocation_is_followed_by_the_propagating_exit(
        self, name
    ):
        """Position, not just presence. An `exit /b` moved *above* the
        invocation satisfies a presence check and still exits 0 — the Windows
        seat proved that by running it. The invariant is the real one: the
        interpreter runs, then its status is propagated, with nothing in
        between. It also subsumes the other direction — a shim that stopped
        propagating at all fails here."""
        lines = self._lines(name)
        forwards = [i for i, line in enumerate(lines) if "%*" in line]
        assert forwards, f"{name} never forwards its arguments"
        for i in forwards:
            following = lines[i + 1] if i + 1 < len(lines) else "<end of file>"
            assert following.lower() == "exit /b %errorlevel%", (
                f"{name} line {i + 1} forwards arguments and is followed by "
                f"{following!r} — the propagating exit has to come straight after"
            )

    @pytest.mark.parametrize("name", SHIMS)
    def test_every_interpreter_runs_before_it_is_used(self, name):
        """`where python` answers whether a name resolves, not whether it
        works. On Windows the first `python` on PATH is often the Microsoft
        Store alias, which resolves and then exits without running anything —
        so a `where`-gated shim enters that branch, returns the alias's
        failure, and never reaches a working `py -3`. The bash and PowerShell
        halves each run a candidate before believing in it; this half has to
        as well."""
        lines = self._lines(name)
        used = {
            line.split(" ")[0].lower()
            for line in lines
            if "%*" in line
        }
        assert used, f"{name} never forwards its arguments"
        probed = {
            line.split(" ")[0].lower()
            for line in lines
            if '-c "pass"' in line
        }
        assert used <= probed, (
            f"{name} forwards to {sorted(used - probed)} without running it "
            "first — `where` is resolution, not acceptance"
        )
        assert "where " not in " ".join(lines).lower(), (
            f"{name} still gates on `where`, which cannot see a broken alias"
        )


class TestTheGuardsAreRoutedToAJobThatRunsThem:
    """The `.cmd` and `.ps1` guards above are globbed, and they live in the
    a8s suite. The per-PR workflow routes by path, so a change to `r4t.ps1`
    ran the r4t suite and never the guards — and `r4t.cmd` and `k7e.cmd` were
    in no filter at all, so a change to either ran nothing. A guard that can
    stay green by not running is the same defect as a guard that cannot fail.

    Parsed rather than imported: the repo has no YAML reader, and the two
    levels of indentation this block uses are enough to read by hand.
    """

    WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"
    GUARDED = sorted(
        path.name
        for path in REPO_ROOT.iterdir()
        if path.suffix in (".cmd", ".ps1")
    )

    @classmethod
    def _filters(cls):
        lines = cls.WORKFLOW.read_text(encoding="utf-8").splitlines()
        start = next(i for i, line in enumerate(lines) if line.endswith("filters: |"))
        indent = len(lines[start]) - len(lines[start].lstrip())
        groups = {}
        name = None
        for line in lines[start + 1:]:
            if line.strip() and (len(line) - len(line.lstrip())) <= indent:
                break
            body = line.strip()
            if not body or body.startswith("#"):
                continue
            if body.startswith("- "):
                if name is not None:
                    groups[name].append(body[2:].strip().strip("'\""))
            elif body.endswith(":") or " &" in body:
                name = body.split(":")[0].strip()
                groups[name] = []
        return groups

    def test_the_block_was_parsed(self):
        groups = self._filters()
        assert {"shared", "shims", "a8s", "r4t", "k7e", "ar3"} <= set(groups), groups
        assert self.GUARDED, "no shims found to guard"

    def test_the_a8s_job_receives_every_shim_the_guards_glob(self):
        groups = self._filters()
        assert "*shims" in groups["a8s"], (
            "the a8s job holds the globbed shim guards, so the shim filter has "
            "to reach it"
        )
        patterns = groups["shims"]
        for name in self.GUARDED:
            suffix = "*" + name[name.rindex("."):]
            assert suffix in patterns or name in patterns, (
                f"{name} is guarded by a globbed test but no filter routes it "
                f"to the job that runs it: {patterns}"
            )

    def test_each_app_still_runs_its_own_suite_for_its_own_shims(self):
        """Routing every shim to a8s must not cost the app its own run."""
        groups = self._filters()
        for app in ("r4t", "k7e", "ar3"):
            for name in (app, f"{app}.cmd", f"{app}.ps1"):
                if name in self.GUARDED or name in (app,):
                    assert name in groups[app], f"{name} does not run the {app} suite"


PWSH = shutil.which("pwsh")


@pytest.mark.skipif(PWSH is None, reason="the .ps1 shims are PowerShell")
class TestPowerShellShimsResolveAWorkingInterpreter:
    """The `.ps1` half, which is what a PowerShell user actually gets.

    PowerShell prefers `<name>.ps1` over both the `.cmd` and the extensionless
    polyglot, so on Windows these files — not the bash half — are the shim.
    They took the resolution *order* from #173 and not the liveness probe:
    `Get-Command python3` finds the Microsoft Store alias, a stub that opens
    the Store and exits non-zero, and the shim ran it and stopped there
    without ever reaching a working `python`. Reproduced before fixing.

    Driven with `pwsh`, which is PowerShell 7 and cross-platform — so unlike
    the `.cmd` guard above, this one runs for real on the CI runners rather
    than only on the seat that has the platform.
    """

    # Globbed for the same reason as the `.cmd` list, with the same floor.
    SHIMS = sorted(path.name for path in REPO_ROOT.glob("*.ps1"))
    MINIMUM_SHIMS = 6

    WORKS = (
        "import sys\n"
        # The liveness probe passes `-c ""`; only a real invocation is
        # recorded, so the log names what the shim chose to run.
        "if '-c' in sys.argv: sys.exit(0)\n"
        "open(LOG, 'a', encoding='utf-8').write(NAME + ' ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        "sys.exit(EXIT)\n"
    )
    # The Store-alias shape: found on PATH, runs nothing, exits non-zero.
    BROKEN = (
        "import sys\n"
        "sys.stderr.write('Python was not found; run without arguments to install\\n')\n"
        "sys.exit(9009)\n"
    )

    def _candidate(self, bin_dir, name, log, *, works=True, exit_code=0):
        source = (
            self.WORKS.replace("LOG", repr(str(log)))
            .replace("NAME", repr(name))
            .replace("EXIT", str(exit_code))
            if works
            else self.BROKEN
        )
        write_path_executable(bin_dir, name, source)

    def _run(self, shim, bin_dir):
        return subprocess.run(
            [PWSH, "-NoProfile", "-File", str(REPO_ROOT / shim), "--version"],
            # The whole environment minus PATH: pwsh on Windows needs more of it
            # than bash does to start at all, and PATH is the only thing under test.
            env={**os.environ, "PATH": str(bin_dir)},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def test_the_glob_found_the_shims(self):
        assert len(self.SHIMS) >= self.MINIMUM_SHIMS, self.SHIMS

    # Every file carrying the PowerShell probe, `.ps1` and polyglot alike. The
    # polyglots' bash half legitimately passes `-c ""`, so the scan anchors on
    # the PowerShell call rather than on the file.
    PROBE_LINE = re.compile(r"&\s+\$Found\.Source\s+@Probe\s+-c\s+\"(?P<code>[^\"]*)\"")
    PROBED = sorted(
        path.name
        for path in REPO_ROOT.iterdir()
        if path.is_file() and "$Found.Source @Probe" in _safe_read(path)
    )

    def test_the_probe_scan_found_every_shim(self):
        assert len(self.PROBED) >= 10, self.PROBED

    @pytest.mark.parametrize("name", PROBED)
    def test_the_probe_argument_is_never_the_empty_string(self, name):
        """The one thing an executed test here cannot prove.

        Windows PowerShell 5.1 — the `powershell` on a stock box — DROPS an
        empty-string argument to a native command. `-c ""` reaches the
        interpreter as a bare `-c`, which answers "Argument expected for the
        -c option" and exits 2, so every candidate is rejected and every
        command exits 127 on a machine where all three interpreters work.
        PowerShell 7 keeps the argument and returns 0 — and `pwsh` is
        PowerShell 7, so the executed cases in this class, and the CI runners
        they run on, are precisely the version that cannot see it. The Windows
        seat caught it on 5.1 within minutes of the push.

        A static assertion is therefore the only guard that covers 5.1, which
        is the inverse of the usual argument for running the real thing.
        """
        matches = self.PROBE_LINE.findall((REPO_ROOT / name).read_text(encoding="utf-8"))
        assert matches, f"{name} carries the probe but not in the expected shape"
        assert all(code for code in matches), (
            f"{name} probes with an empty `-c` argument, which Windows "
            "PowerShell 5.1 drops — use `-c \"pass\"`"
        )

    @pytest.mark.parametrize("shim", SHIMS)
    def test_python3_is_preferred_when_it_runs(self, shim, tmp_path):
        d, log = tmp_path / "bin", tmp_path / "log"
        self._candidate(d, "python3", log)
        self._candidate(d, "python", log)
        assert self._run(shim, d).returncode == 0
        assert log.read_text(encoding="utf-8").startswith("python3 ")

    @pytest.mark.parametrize("shim", SHIMS)
    def test_a_found_but_unrunnable_python3_falls_through_to_python(self, shim, tmp_path):
        """The defect this class was written for. Before the probe, the shim
        accepted the alias and exited 49 — 9009 truncated to a byte — with the
        working interpreter beside it never tried."""
        d, log = tmp_path / "bin", tmp_path / "log"
        self._candidate(d, "python3", log, works=False)
        self._candidate(d, "python", log)
        assert self._run(shim, d).returncode == 0
        assert log.read_text(encoding="utf-8").startswith("python ")

    @pytest.mark.parametrize("shim", SHIMS)
    def test_py_is_the_last_resort_and_carries_its_flag(self, shim, tmp_path):
        d, log = tmp_path / "bin", tmp_path / "log"
        self._candidate(d, "py", log)
        assert self._run(shim, d).returncode == 0
        assert log.read_text(encoding="utf-8").startswith("py -3 ")

    @pytest.mark.parametrize("shim", SHIMS)
    def test_every_candidate_unrunnable_reports_rather_than_runs_one(self, shim, tmp_path):
        d, log = tmp_path / "bin", tmp_path / "log"
        for name in ("python3", "python", "py"):
            self._candidate(d, name, log, works=False)
        result = self._run(shim, d)
        assert result.returncode == 127
        assert "no working python3, python, or py on PATH" in result.stderr
        assert not log.exists()

    @pytest.mark.parametrize("shim", SHIMS)
    def test_nothing_on_path_names_the_shim(self, shim, tmp_path):
        d = tmp_path / "bin"
        d.mkdir(parents=True)
        result = self._run(shim, d)
        assert result.returncode == 127
        assert shim.removesuffix(".ps1") in result.stderr

    @pytest.mark.parametrize("shim", SHIMS)
    def test_the_interpreters_exit_code_is_what_the_shim_exits(self, shim, tmp_path):
        """The `.cmd` shims lost this outright. Asserted here too, because a
        PowerShell `exit $LASTEXITCODE` reads the value at a different moment
        than the one that broke them."""
        d, log = tmp_path / "bin", tmp_path / "log"
        self._candidate(d, "python3", log, exit_code=42)
        assert self._run(shim, d).returncode == 42
