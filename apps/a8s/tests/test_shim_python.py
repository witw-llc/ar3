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

import json
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


def _ps_literal(text: str) -> str:
    """A PowerShell single-quoted string, which expands nothing at all."""
    return "'" + text.replace("'", "''") + "'"


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


def _run(
    shim: str, bin_dir: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, str(REPO_ROOT / shim), "--version"],
        env={"PATH": str(bin_dir), **(env or {})},
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

_PS_WORKS = (
    "import sys\n"
    # The liveness probe passes `-c ""`; only a real invocation is
    # recorded, so the log names what the shim chose to run.
    "if '-c' in sys.argv: sys.exit(0)\n"
    "open(LOG, 'a', encoding='utf-8').write(NAME + ' ' + ' '.join(sys.argv[1:]) + '\\n')\n"
    "sys.exit(EXIT)\n"
)
# The Store-alias shape: found on PATH, runs nothing, exits non-zero.
_PS_BROKEN = (
    "import sys\n"
    "sys.stderr.write('Python was not found; run without arguments to install\\n')\n"
    "sys.exit(9009)\n"
)


def _ps_candidate(bin_dir, name, log, *, works=True, exit_code=0, source=None):
    """A stand-in interpreter on `bin_dir`, logging its own invocation."""
    if source is None:
        source = (
            _PS_WORKS.replace("LOG", repr(str(log)))
            .replace("NAME", repr(name))
            .replace("EXIT", str(exit_code))
            if works
            else _PS_BROKEN
        )
    return write_path_executable(bin_dir, name, source)


def _ps_run(shim, bin_dir, env=None):
    # The whole environment minus PATH: pwsh on Windows needs more of it than
    # bash does to start at all, and PATH is the only thing under test. A None
    # value removes a variable, which is how a caller says "nothing was set"
    # rather than "an empty string was set" — a distinction the launchers act on.
    merged = {**os.environ, "PATH": str(bin_dir), **(env or {})}
    return subprocess.run(
        [PWSH, "-NoProfile", "-File", str(REPO_ROOT / shim), "--version"],
        env={key: value for key, value in merged.items() if value is not None},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


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
        _ps_candidate(d, "python3", log)
        _ps_candidate(d, "python", log)
        assert _ps_run(shim, d).returncode == 0
        assert log.read_text(encoding="utf-8").startswith("python3 ")

    @pytest.mark.parametrize("shim", SHIMS)
    def test_a_found_but_unrunnable_python3_falls_through_to_python(self, shim, tmp_path):
        """The defect this class was written for. Before the probe, the shim
        accepted the alias and exited 49 — 9009 truncated to a byte — with the
        working interpreter beside it never tried."""
        d, log = tmp_path / "bin", tmp_path / "log"
        _ps_candidate(d, "python3", log, works=False)
        _ps_candidate(d, "python", log)
        assert _ps_run(shim, d).returncode == 0
        assert log.read_text(encoding="utf-8").startswith("python ")

    @pytest.mark.parametrize("shim", SHIMS)
    def test_py_is_the_last_resort_and_carries_its_flag(self, shim, tmp_path):
        d, log = tmp_path / "bin", tmp_path / "log"
        _ps_candidate(d, "py", log)
        assert _ps_run(shim, d).returncode == 0
        assert log.read_text(encoding="utf-8").startswith("py -3 ")

    @pytest.mark.parametrize("shim", SHIMS)
    def test_every_candidate_unrunnable_reports_rather_than_runs_one(self, shim, tmp_path):
        d, log = tmp_path / "bin", tmp_path / "log"
        for name in ("python3", "python", "py"):
            _ps_candidate(d, name, log, works=False)
        result = _ps_run(shim, d)
        assert result.returncode == 127
        assert "no working python3, python, or py on PATH" in result.stderr
        assert not log.exists()

    @pytest.mark.parametrize("shim", SHIMS)
    def test_nothing_on_path_names_the_shim(self, shim, tmp_path):
        d = tmp_path / "bin"
        d.mkdir(parents=True)
        result = _ps_run(shim, d)
        assert result.returncode == 127
        assert shim.removesuffix(".ps1") in result.stderr

    @pytest.mark.parametrize("shim", SHIMS)
    def test_the_interpreters_exit_code_is_what_the_shim_exits(self, shim, tmp_path):
        """The `.cmd` shims lost this outright. Asserted here too, because a
        PowerShell `exit $LASTEXITCODE` reads the value at a different moment
        than the one that broke them."""
        d, log = tmp_path / "bin", tmp_path / "log"
        _ps_candidate(d, "python3", log, exit_code=42)
        assert _ps_run(shim, d).returncode == 42


# A body that survives this survives PowerShell: `$HOME` is expansion, a
# backtick is PowerShell's escape character, both quote styles are string
# delimiters, the interior newlines are what a line-at-a-time forward
# reflows, and the non-ASCII run is what a console code page mangles. Nothing
# hangs on the outer whitespace — `tell` strips that either way.
PIPED_BODY = (
    "line one $HOME `backtick` \"quoted\" 'single'\n"
    "line two — naïve ünïcode ¥ 日本語\n"
    "line three ends without a newline"
)


@pytest.mark.skipif(PWSH is None, reason="the .ps1 shims are PowerShell")
class TestPowerShellShimsForwardPipedStdin:
    """A here-string piped to `tell.ps1` staged a blank body (#274).

    Pipeline input reaches a PowerShell script as the `$input` enumerator and
    is *not* attached to a native command the script starts, so the launcher
    handed python an empty stdin and `tell <name> -` read nothing. The field
    seat that hit it saw the same body arrive whole when it went straight to
    `python.exe apps/a8s/a8s.py tell <name> -`, which is what named the
    launcher rather than tell.

    The obvious fix is a second defect. Piping `$input` unconditionally hands
    python an immediate EOF when there was no PowerShell pipeline, and stdin
    can be a real stream anyway — an inherited console, or a producer attached
    to the shell that started pwsh. `test_an_inherited_stdin_still_reaches_
    the_message` is the control for that, and it fails on the unconditional
    form. PowerShell has no `<` operator, so that inheritance, not a redirect,
    is the shape the case takes.
    """

    # The verb each launcher needs before the recipient.
    VERBS = {"tell.ps1": "", "a8s.ps1": "tell "}
    DRIVEN = sorted(VERBS)

    @staticmethod
    def _env(tmp_path, outbox):
        """No registry near this. `TELL_OUTBOX_DIR` alone makes tell a staging
        writer, which is the shape r4t already relies on, so nothing here
        needs a node to exist or a name to resolve."""
        env = {**os.environ}
        env["TELL_OUTBOX_DIR"] = str(outbox)
        env["HOME"] = str(tmp_path / "home")
        env["USERPROFILE"] = str(tmp_path / "home")
        env["A8S_HOME"] = str(tmp_path / "a8s-home")
        env.pop("XDG_CONFIG_HOME", None)
        return env

    # What a script writes to a native command is encoded in $OutputEncoding.
    # ASCII is the default on Windows PowerShell 5.1, the `powershell` on a
    # stock box, and it is the setting that flattened the non-ASCII run before
    # the launcher had started python. Every executed case here drives it, so
    # the caller under test is the one the field seat had — pre-setting UTF-8
    # in the caller measures a machine nobody runs.
    LEGACY_ENCODER = "[System.Text.Encoding]::ASCII"
    UTF8_ENCODER = "[System.Text.UTF8Encoding]::new($false)"

    def _tell(self, shim, tmp_path, invocation, *, stdin=None, encoder=LEGACY_ENCODER):
        outbox = tmp_path / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        (tmp_path / "home").mkdir(parents=True, exist_ok=True)
        body_file = tmp_path / "body.txt"
        body_file.write_text(PIPED_BODY, encoding="utf-8")
        script = (
            "$ErrorActionPreference = 'Stop'\n"
            f"$OutputEncoding = {encoder}\n"
            f"$body = [IO.File]::ReadAllText({_ps_literal(str(body_file))})\n"
            + invocation.format(
                shim=_ps_literal(str(REPO_ROOT / shim)),
                verb=self.VERBS[shim],
                body=_ps_literal(str(body_file)),
            )
            + "\nexit $LASTEXITCODE\n"
        )
        result = subprocess.run(
            [PWSH, "-NoProfile", "-Command", script],
            env=self._env(tmp_path, outbox),
            input="" if stdin is None else stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        return result, outbox

    @staticmethod
    def _staged(outbox):
        files = sorted(outbox.glob("*.json"))
        assert len(files) == 1, f"expected one staged message, found {files}"
        return json.loads(files[0].read_text(encoding="utf-8"))

    @pytest.mark.parametrize("shim", DRIVEN)
    def test_a_piped_body_reaches_the_message_whole(self, shim, tmp_path):
        """The reported defect. Before the fix `content` was the empty string
        and the exit code was 0, so nothing said the body had been lost."""
        result, outbox = self._tell(shim, tmp_path, "$body | & {shim} {verb}bob -")
        assert result.returncode == 0, result.stderr
        assert self._staged(outbox)["content"] == PIPED_BODY

    @pytest.mark.parametrize("shim", DRIVEN)
    def test_an_inherited_stdin_still_reaches_the_message(self, shim, tmp_path):
        """The control on the wrong fix. Stdin here is a live stream the
        launcher inherited, with no PowerShell pipeline in front of it:
        `$MyInvocation.ExpectingInput` is false and `$input` is empty. Piping
        it anyway closes python's stdin before it reads a byte, which is how
        an interactive `tell <name> -` would be lost to the fix for #274."""
        result, outbox = self._tell(
            shim, tmp_path, "& {shim} {verb}bob -", stdin=PIPED_BODY
        )
        assert result.returncode == 0, result.stderr
        assert self._staged(outbox)["content"] == PIPED_BODY

    @pytest.mark.parametrize("shim", DRIVEN)
    def test_an_inline_body_is_unchanged(self, shim, tmp_path):
        """Nothing on the pipeline, nothing waiting on stdin."""
        result, outbox = self._tell(shim, tmp_path, "& {shim} {verb}bob 'inline body'")
        assert result.returncode == 0, result.stderr
        assert self._staged(outbox)["content"] == "inline body"

    @pytest.mark.parametrize("encoder", [LEGACY_ENCODER, UTF8_ENCODER])
    @pytest.mark.parametrize("shim", DRIVEN)
    def test_the_callers_output_encoding_never_reaches_the_body(
        self, shim, tmp_path, encoder
    ):
        """The launcher hands python UTF-8 whatever the caller writes in.

        `$OutputEncoding` decides the bytes a script gives a native command,
        and the caller sets it, so the em dash and the CJK run arrived as `?`
        under the ASCII default — a substitution `PYTHONUTF8=1` in the child
        cannot undo, because the characters were gone before python started.
        The launcher sets its own encoder inside the forwarding branch.

        Asserted on the encoded bytes, and separately on a leading U+FEFF: an
        encoder that emits the UTF-8 preamble would put three bytes of BOM at
        the head of the message body, where they read as content.
        """
        result, outbox = self._tell(
            shim, tmp_path, "$body | & {shim} {verb}bob -", encoder=encoder
        )
        assert result.returncode == 0, result.stderr
        content = self._staged(outbox)["content"]
        assert not content.startswith("\ufeff"), "a BOM became the first character"
        assert content.encode("utf-8") == PIPED_BODY.encode("utf-8")


class TestEveryPowerShellLauncherForwardsPipedStdin:
    """The executed cases above drive two launchers. Twelve files carry the
    PowerShell tail, and `ar3`/`r4t`/`k7e` can grow a stdin-reading verb
    without anyone revisiting a shim. Globbed and asserted on the source so a
    launcher added later is covered the day it lands, and so the polyglots'
    PowerShell half — which no `.ps1` case drives — is covered at all.
    """

    LAUNCHERS = sorted(
        path.name
        for path in REPO_ROOT.iterdir()
        if path.is_file() and "exit $LASTEXITCODE" in _safe_read(path)
    )
    MINIMUM_LAUNCHERS = 12

    def test_the_scan_found_every_launcher(self):
        assert len(self.LAUNCHERS) >= self.MINIMUM_LAUNCHERS, self.LAUNCHERS

    @pytest.mark.parametrize("name", LAUNCHERS)
    def test_the_forward_is_gated_on_expecting_input(self, name):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "$MyInvocation.ExpectingInput" in text, (
            f"{name} starts its child without forwarding pipeline input — a "
            "here-string piped to it reaches python as an empty stdin"
        )
        piped = [line for line in text.splitlines() if "$input |" in line]
        assert len(piped) == 1, (
            f"{name} pipes $input on {len(piped)} lines; exactly one belongs, "
            "inside the ExpectingInput branch"
        )
        assert text.index("$MyInvocation.ExpectingInput") < text.index("$input |"), (
            f"{name} pipes $input before the gate that decides whether there "
            "is any — an empty $input is an immediate EOF"
        )

    @pytest.mark.parametrize("name", LAUNCHERS)
    def test_the_forward_sets_its_own_utf8_encoder(self, name):
        """The bytes on that pipe are the caller's `$OutputEncoding` until the
        launcher says otherwise, and on Windows PowerShell 5.1 the caller's is
        ASCII. Pinned to the BOM-less constructor and to its position: after
        the gate so a bare call is untouched, before the pipe so it is in
        force when the bytes are written."""
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assigned = [
            line for line in text.splitlines() if line.strip().startswith("$OutputEncoding =")
        ]
        assert assigned == ["    $OutputEncoding = [System.Text.UTF8Encoding]::new($false)"], (
            f"{name} forwards the pipeline in whatever encoding the caller "
            "set; ASCII is the default on Windows PowerShell 5.1 and turns an "
            "em dash into `?` before python starts. `::new($false)` is the "
            "constructor that emits no BOM — a BOM would become message body."
        )
        gate = text.index("$MyInvocation.ExpectingInput")
        assert gate < text.index("$OutputEncoding =") < text.index("$input |"), (
            f"{name} sets $OutputEncoding outside the forwarding branch or "
            "after the pipe it applies to"
        )


# ---------- AR3_PYTHON and the child's encoding (#275) ----------


@pytest.mark.skipif(BASH is None, reason="the shims' resolution half is bash")
@pytest.mark.parametrize("shim", SHIMS)
class TestAr3PythonIsTriedBeforePath:
    """`AR3_PYTHON` names the interpreter outright, ahead of PATH.

    A desktop harness on Windows had no usable python on the PATH its shell
    handed the launchers, and prepending its own bundled interpreter was the
    only thing that made them resolve. Editing PATH to suit one app is not
    something a boot skill can do reliably, so the suite takes the pointer
    directly.
    """

    def test_it_is_used_before_anything_on_path(self, shim, tmp_path):
        d, log = _bin(tmp_path), tmp_path / "log"
        _candidate(d, "python3", works=True, log=log)
        bundled = tmp_path / "bundled"
        bundled.mkdir()
        _candidate(bundled, "bundled-python", works=True, log=log)
        result = _run(
            shim, d, {"AR3_PYTHON": str(bundled / "bundled-python")}
        )
        assert result.returncode == 0, result.stderr
        assert log.read_text().startswith("bundled-python ")

    def test_a_path_that_does_not_exist_falls_through_and_says_so(self, shim, tmp_path):
        """A stale variable is a typo, not an instruction. Obeying it would
        break a machine whose PATH is fine; failing outright would break it
        harder. The launcher says which variable it disbelieved and carries
        on, because the name is the one thing the operator cannot guess from
        `no working python3, python, or py on PATH`."""
        d, log = _bin(tmp_path), tmp_path / "log"
        _candidate(d, "python3", works=True, log=log)
        missing = str(tmp_path / "nowhere" / "python")
        result = _run(shim, d, {"AR3_PYTHON": missing})
        assert result.returncode == 0, result.stderr
        assert f"AR3_PYTHON={missing}" in result.stderr
        assert log.read_text().startswith("python3 ")

    def test_a_path_that_runs_nothing_falls_through_too(self, shim, tmp_path):
        """The Store-alias shape again: it resolves, so `command -v` believes
        it. The probe is what makes the override safe to honour first."""
        d, log = _bin(tmp_path), tmp_path / "log"
        _candidate(d, "python3", works=True, log=log)
        bundled = tmp_path / "bundled"
        bundled.mkdir()
        _candidate(bundled, "bundled-python", works=False, log=log)
        result = _run(
            shim, d, {"AR3_PYTHON": str(bundled / "bundled-python")}
        )
        assert result.returncode == 0, result.stderr
        assert "AR3_PYTHON=" in result.stderr
        assert log.read_text().startswith("python3 ")


@pytest.mark.skipif(PWSH is None, reason="the .ps1 shims are PowerShell")
@pytest.mark.parametrize("shim", sorted(path.name for path in REPO_ROOT.glob("*.ps1")))
class TestAr3PythonIsTriedBeforePathInPowerShell:
    """The same override on the half a Windows user actually runs."""

    def test_it_is_used_before_anything_on_path(self, shim, tmp_path):
        d, log = tmp_path / "bin", tmp_path / "log"
        _ps_candidate(d, "python3", log)
        bundled = tmp_path / "bundled"
        exe = _ps_candidate(bundled, "bundled-python", log)
        result = _ps_run(shim, d, {"AR3_PYTHON": str(exe)})
        assert result.returncode == 0, result.stderr
        assert log.read_text(encoding="utf-8").startswith("bundled-python ")

    def test_a_path_that_does_not_exist_falls_through_and_says_so(self, shim, tmp_path):
        d, log = tmp_path / "bin", tmp_path / "log"
        _ps_candidate(d, "python3", log)
        missing = str(tmp_path / "nowhere" / "python")
        result = _ps_run(shim, d, {"AR3_PYTHON": missing})
        assert result.returncode == 0, result.stderr
        assert f"AR3_PYTHON={missing}" in result.stderr
        assert log.read_text(encoding="utf-8").startswith("python3 ")

    def test_a_path_that_runs_nothing_falls_through_too(self, shim, tmp_path):
        d, log = tmp_path / "bin", tmp_path / "log"
        _ps_candidate(d, "python3", log)
        bundled = tmp_path / "bundled"
        exe = _ps_candidate(bundled, "bundled-python", log, works=False)
        result = _ps_run(shim, d, {"AR3_PYTHON": str(exe)})
        assert result.returncode == 0, result.stderr
        assert "AR3_PYTHON=" in result.stderr
        assert log.read_text(encoding="utf-8").startswith("python3 ")


# Reports the child's own view of the two encoding variables rather than the
# argv, so what the launcher exported is what gets asserted.
_PS_ENV_REPORT = """import os, sys
if '-c' in sys.argv: sys.exit(0)
open(LOG, 'a', encoding='utf-8').write(
    'PYTHONUTF8=' + os.environ.get('PYTHONUTF8', '<unset>')
    + ' PYTHONIOENCODING=' + os.environ.get('PYTHONIOENCODING', '<unset>')
    + ' utf8_mode=' + str(sys.flags.utf8_mode) + '\\n')
sys.exit(0)
"""


@pytest.mark.skipif(PWSH is None, reason="the .ps1 shims are PowerShell")
@pytest.mark.parametrize("shim", sorted(path.name for path in REPO_ROOT.glob("*.ps1")))
class TestWindowsLaunchersRunTheChildInUtf8:
    """A stock Windows console is cp1252, and the suite's own output — arrows,
    em dashes, agent names — raised `UnicodeEncodeError` in the child until a
    field seat set `PYTHONIOENCODING` by hand. UTF-8 mode is set for the child
    rather than the console because the launcher owns the child and does not
    own the console.

    Windows-shaped, but asserted through `pwsh`, which is PowerShell 7 and
    cross-platform: the variable is exported the same way everywhere, so the
    property runs on every CI leg instead of only on the seat with the
    platform. `release.yml` has no Windows job (#164).
    """

    def _report(self, shim, tmp_path, env):
        d, log = tmp_path / "bin", tmp_path / "log"
        _ps_candidate(
            d, "python3", log, source=_PS_ENV_REPORT.replace("LOG", repr(str(log)))
        )
        result = _ps_run(shim, d, env)
        assert result.returncode == 0, result.stderr
        return log.read_text(encoding="utf-8").strip()

    def test_the_child_gets_utf8_mode_when_the_caller_chose_nothing(self, shim, tmp_path):
        report = self._report(
            shim, tmp_path, {"PYTHONUTF8": None, "PYTHONIOENCODING": None}
        )
        assert "PYTHONUTF8=1" in report
        assert "utf8_mode=1" in report

    def test_a_caller_who_set_utf8_mode_keeps_their_value(self, shim, tmp_path):
        """`PYTHONUTF8=0` is a deliberate opt-out, and a launcher that
        overwrites it is choosing for the operator."""
        report = self._report(
            shim, tmp_path, {"PYTHONUTF8": "0", "PYTHONIOENCODING": None}
        )
        assert "PYTHONUTF8=0" in report

    def test_pythonioencoding_alone_is_left_to_decide(self, shim, tmp_path):
        """The two knobs answer the same question. Setting UTF-8 mode on top
        of a caller's `PYTHONIOENCODING=cp932` would silently override it."""
        report = self._report(
            shim, tmp_path, {"PYTHONUTF8": None, "PYTHONIOENCODING": "cp932"}
        )
        assert "PYTHONUTF8=<unset>" in report


class TestEveryWindowsLauncherCarriesTheOverrideAndTheEncoding:
    """The executed cases drive bash and `pwsh`. `cmd.exe` exists on one
    platform, so its half is asserted on the source — the same trade the
    exit-code guard above makes, for the same reason.

    Globbed with a floor rather than enumerated, so a launcher added later is
    covered the day it lands.
    """

    OVERRIDE = sorted(
        path.name
        for path in REPO_ROOT.iterdir()
        if path.is_file() and "no working python3, python, or py" in _safe_read(path)
    )
    MINIMUM_OVERRIDE = 16
    CMD = sorted(path.name for path in REPO_ROOT.glob("*.cmd"))
    PS1 = sorted(path.name for path in REPO_ROOT.glob("*.ps1"))

    def test_the_scan_found_every_resolving_launcher(self):
        assert len(self.OVERRIDE) >= self.MINIMUM_OVERRIDE, self.OVERRIDE

    @pytest.mark.parametrize("name", OVERRIDE)
    def test_the_override_is_probed_and_named_when_it_fails(self, name):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "AR3_PYTHON" in text, (
            f"{name} resolves an interpreter but honours no AR3_PYTHON, so a "
            "harness with its own python has to edit PATH to be seen"
        )
        assert "AR3_PYTHON=" in text, (
            f"{name} does not name AR3_PYTHON in what it prints; a stale "
            "variable then reads as a broken PATH"
        )
        assert '-c "pass"' in text, f"{name} accepts an interpreter it never ran"

    @pytest.mark.parametrize("name", CMD + PS1)
    def test_the_windows_launchers_set_utf8_for_the_child(self, name):
        """Windows only. The bash half is deliberately left alone: a POSIX
        locale is the operator's own setting and is already UTF-8 nearly
        everywhere."""
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assign = (
            'set "PYTHONUTF8=1"' if name.endswith(".cmd") else '$env:PYTHONUTF8 = "1"'
        )
        assert "PYTHONUTF8" in text and "PYTHONIOENCODING" in text, (
            f"{name} starts a child on Windows without asking for UTF-8, so "
            "the suite's own output crashes it on a cp1252 console"
        )
        assert text.index("PYTHONIOENCODING") < text.index(assign), (
            f"{name} sets UTF-8 mode without reading PYTHONIOENCODING first, "
            "so a caller's own encoding choice is overwritten"
        )
