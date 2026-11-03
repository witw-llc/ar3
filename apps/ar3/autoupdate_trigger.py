"""The OS entry that wakes `ar3 update --auto run` every 15 minutes.

One entry per install, named for the install id: a cron line on Linux, a
LaunchAgent on macOS, a scheduled task on Windows. The entry only starts the
command; the window, the slot and every other decision live in `autoupdate`.
Cron shares the account's crontab lock with a8s autostart (`ar3.crontab`).
"""
from __future__ import annotations

import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from ar3 import crontab
from ar3.fsio import atomic_write_text
from ar3.proc import launch_options

IS_WINDOWS = os.name == "nt"
TICK_SECONDS = 900
CRON_TAG = "ar3-autoupdate"


class TriggerError(Exception):
    """The entry could not be inspected, written or removed."""


class TriggerUnavailable(TriggerError):
    """The platform has no scheduler to use."""


def command(launcher: Path, python: str) -> list[str]:
    """The run: the launcher `autoupdate.arm` wrote outside the install. It
    names the install and the ar3 home whose state armed it, because a
    scheduler starts it with none of the operator's environment."""
    return [python, str(launcher)]


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=60,
            **launch_options(windows=IS_WINDOWS),
        )
    except FileNotFoundError as e:
        raise TriggerError(f"{argv[0]} is not installed") from e
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TriggerError(f"{argv[0]} failed: {e}") from e


class Trigger:
    name = ""

    def install(self, install_id: str, launcher: Path, python: str, env: dict[str, str]) -> str:
        """Write the entry. Returns a note for the operator, or ''."""
        raise NotImplementedError

    def remove(self, install_id: str) -> None:
        raise NotImplementedError

    def installed(self, install_id: str) -> str | None:
        """What the scheduler holds for this install, or None."""
        raise NotImplementedError


class CronTrigger(Trigger):
    name = "cron"

    def __init__(self, backup: Path):
        self.backup = backup

    @staticmethod
    def _tail(install_id: str) -> str:
        return f"# {CRON_TAG} install={install_id}"

    def _owned(self, install_id: str):
        tail = self._tail(install_id)
        return lambda line: line.endswith(tail) and line.lstrip()[:1] != "#"

    def line(self, install_id: str, launcher: Path, python: str, env: dict[str, str]) -> str:
        cmd = " ".join(shlex.quote(a) for a in command(launcher, python))
        # The tick restores PATH from state; embedding it can exceed cron's command limit.
        text = f"*/15 * * * * {cmd} >/dev/null 2>&1".replace("%", "\\%")
        return f"{text} {self._tail(install_id)}"

    def check(self) -> None:
        if shutil.which("crontab") is None:
            raise TriggerUnavailable(
                "auto-update on Linux needs cron, and `crontab` is not installed; "
                "install a cron package"
            )

    def _rewrite(self, install_id: str, add: str | None) -> None:
        try:
            crontab.rewrite(self._owned(install_id), add, self.backup)
        except crontab.CrontabError as e:
            raise TriggerError(str(e)) from e

    def install(self, install_id, launcher, python, env):
        self.check()
        self._rewrite(install_id, self.line(install_id, launcher, python, env))
        return ""

    def remove(self, install_id):
        self.check()
        self._rewrite(install_id, None)

    def installed(self, install_id):
        self.check()
        try:
            text = crontab.read()
        except crontab.CrontabError as e:
            raise TriggerError(str(e)) from e
        owned = self._owned(install_id)
        return next((ln for ln in text.splitlines() if owned(ln)), None)


class LaunchdTrigger(Trigger):
    name = "launchd"

    def __init__(self, agents_dir: Path | None = None):
        self.agents_dir = agents_dir or Path.home() / "Library" / "LaunchAgents"

    def label(self, install_id: str) -> str:
        return f"com.ar3.autoupdate.{install_id}"

    def path(self, install_id: str) -> Path:
        return self.agents_dir / f"{self.label(install_id)}.plist"

    def _domain(self) -> str:
        return f"gui/{os.getuid()}"

    def _has_gui(self) -> bool:
        try:
            return _run(["launchctl", "print", self._domain()]).returncode == 0
        except TriggerError:
            return False

    def _loaded(self, install_id: str) -> bool:
        try:
            return _run(["launchctl", "print", f"{self._domain()}/{self.label(install_id)}"]).returncode == 0
        except TriggerError:
            return False

    def install(self, install_id, launcher, python, env):
        plist = {
            "Label": self.label(install_id),
            "ProgramArguments": command(launcher, python),
            "StartInterval": TICK_SECONDS,
            "RunAtLoad": True,
        }
        if env.get("PATH"):
            plist["EnvironmentVariables"] = {"PATH": env["PATH"]}
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        try:
            atomic_write_text(self.path(install_id), plistlib.dumps(plist).decode())
        except OSError as e:
            raise TriggerError(f"could not write {self.path(install_id)}: {e}") from e
        if not self._has_gui():
            return "armed at next login (no graphical login session is running)"
        if self._loaded(install_id):
            _run(["launchctl", "bootout", f"{self._domain()}/{self.label(install_id)}"])
        proc = _run(["launchctl", "bootstrap", self._domain(), str(self.path(install_id))])
        if proc.returncode != 0:
            raise TriggerError(f"launchctl bootstrap failed: {(proc.stderr or proc.stdout).strip()}")
        return ""

    def remove(self, install_id):
        if self._loaded(install_id):
            try:
                _run(["launchctl", "bootout", f"{self._domain()}/{self.label(install_id)}"])
            except TriggerError:
                pass
        try:
            self.path(install_id).unlink(missing_ok=True)
        except OSError as e:
            raise TriggerError(f"could not remove {self.path(install_id)}: {e}") from e

    def installed(self, install_id):
        path = self.path(install_id)
        if not path.is_file():
            return None
        return str(path)


class SchtasksTrigger(Trigger):
    name = "task-scheduler"

    def __init__(self, scratch: Path):
        self.scratch = scratch

    def task_name(self, install_id: str) -> str:
        return f"\\ar3\\autoupdate-{install_id}"

    def interpreter(self, python: str) -> str:
        """The task runs with no console: `pythonw.exe` beside the interpreter."""
        gui = Path(python).with_name("pythonw.exe")
        return str(gui) if gui.is_file() or Path(python).name.lower() == "pythonw.exe" else python

    def _user_sid(self) -> str:
        proc = _run(["whoami", "/user", "/fo", "csv", "/nh"])
        found = re.search(r'"(S-1-\d+(?:-\d+)+)"\s*$', proc.stdout.strip())
        if proc.returncode != 0 or not found:
            raise TriggerError("cannot determine the current user's SID; refusing to write a scheduled task")
        return found.group(1)

    def xml(self, launcher: Path, python: str) -> str:
        user = xml_escape(self._user_sid())
        argv = command(launcher, self.interpreter(python))
        args = subprocess.list2cmdline(argv[1:])
        return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers><CalendarTrigger><StartBoundary>2000-01-01T00:00:00</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay><Repetition><Interval>PT15M</Interval><Duration>P1D</Duration><StopAtDurationEnd>false</StopAtDurationEnd></Repetition></CalendarTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{xml_escape(argv[0])}</Command><Arguments>{xml_escape(args)}</Arguments></Exec></Actions>
</Task>
"""

    def check(self) -> None:
        if shutil.which("schtasks") is None:
            raise TriggerUnavailable("auto-update on Windows needs schtasks, which was not found")

    def install(self, install_id, launcher, python, env):
        self.check()
        xml_file = self.scratch / f"autoupdate-{install_id}.xml"
        xml_file.parent.mkdir(parents=True, exist_ok=True)
        xml_file.write_text(self.xml(launcher, python), encoding="utf-16")
        try:
            proc = _run(["schtasks", "/Create", "/TN", self.task_name(install_id), "/XML", str(xml_file), "/F"])
        finally:
            xml_file.unlink(missing_ok=True)
        if proc.returncode != 0:
            raise TriggerError(f"schtasks rejected the task: {(proc.stderr or proc.stdout).strip()}")
        return ""

    def remove(self, install_id):
        self.check()
        if self.installed(install_id) is None:
            return
        proc = _run(["schtasks", "/Delete", "/TN", self.task_name(install_id), "/F"])
        if proc.returncode != 0:
            raise TriggerError(f"schtasks could not delete the task: {(proc.stderr or proc.stdout).strip()}")

    def installed(self, install_id):
        self.check()
        proc = _run(["schtasks", "/Query", "/TN", self.task_name(install_id)])
        return self.task_name(install_id) if proc.returncode == 0 else None


def select(scratch: Path) -> Trigger:
    """The scheduler of this platform. `scratch` is a directory this install
    owns (its update state directory)."""
    if sys.platform.startswith("linux"):
        return CronTrigger(scratch / "crontab.bak")
    if sys.platform == "darwin":
        return LaunchdTrigger()
    if IS_WINDOWS:
        return SchtasksTrigger(scratch)
    raise TriggerUnavailable(f"auto-update is not supported on {sys.platform}")
