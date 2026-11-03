"""Nightly auto-update: the state file, the scheduler entries, the night run,
the health check, rollback and recovery. Hermetic: the scheduler is an
in-memory fake (conftest), the OS entries are written against fakes of
`crontab`, `launchctl` and `schtasks`, and git and the installer are modeled,
except where the boundary is git or the filesystem: those recovery tests run
real git in temporary repositories, the real get.sh, and the launcher in a
fresh process."""
from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from shutil import which as shutil_which

import autoupdate as au
import autoupdate_trigger as trig
import cli as ar3
from ar3 import crontab
from _memory_trigger import STORE, MemoryTrigger

_REAL_RUN = subprocess.run
_REAL_SELECT = trig.select


def _root(tmp_path, name="install"):
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    return root


# ---------- state ----------

def test_install_id_names_the_install_and_is_stable(tmp_path):
    a, b = _root(tmp_path, "a"), _root(tmp_path, "b")
    assert au.install_id(a) == au.install_id(a)
    assert au.install_id(a) != au.install_id(b)
    assert len(au.install_id(a)) == 10


def test_state_round_trips_and_is_private(tmp_path):
    root = _root(tmp_path)
    assert au.load(root) == {}
    au.save(root, {"enabled": True, "window": "01:00-02:00"})
    assert au.load(root) == {"enabled": True, "window": "01:00-02:00"}
    if os.name == "posix":
        assert stat.S_IMODE(au.state_path(root).stat().st_mode) == 0o600
    assert au.state_path(root).parent == au.state_dir()
    assert au.state_path(root).name == f"{au.install_id(root)}.json"


def test_unreadable_state_reads_as_empty(tmp_path):
    root = _root(tmp_path)
    au.state_dir().mkdir(parents=True)
    au.state_path(root).write_text("{not json", encoding="utf-8")
    assert au.load(root) == {}
    au.state_path(root).write_text("[1]", encoding="utf-8")
    assert au.load(root) == {}


def test_log_keeps_the_newest_200_lines(tmp_path):
    root = _root(tmp_path)
    for n in range(230):
        au.append_log(root, f"line {n}")
    lines = au.log_path(root).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 200
    assert lines[0] == "line 30" and lines[-1] == "line 229"


def test_the_home_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("AR3_HOME", str(tmp_path / "elsewhere"))
    assert au.state_dir() == tmp_path / "elsewhere" / "update"


# ---------- arming ----------

def test_arm_records_the_install_and_captures_the_environment(tmp_path, monkeypatch):
    root = _root(tmp_path)
    monkeypatch.setenv("PATH", "/captured/bin")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    assert au.arm(root, python="/usr/bin/pythonX") == ""
    state = au.load(root)
    assert state["enabled"] is True and state["window"] == au.DEFAULT_WINDOW
    assert state["env"]["PATH"] == "/captured/bin"
    assert state["env"]["HTTPS_PROXY"] == "http://proxy.invalid:3128"
    entry = STORE[au.install_id(root)]
    assert entry["python"] == "/usr/bin/pythonX" and entry["env"]["PATH"] == "/captured/bin"


def _launched(launcher: Path, monkeypatch) -> list[str] | None:
    """The command the launcher starts, read by running it in this process
    with the start itself replaced."""
    import runpy

    seen = []
    monkeypatch.setattr(subprocess, "call", lambda argv: seen.append(list(argv)) or 0)
    with pytest.raises(SystemExit):
        runpy.run_path(str(launcher))
    return seen[0] if seen else None


@pytest.mark.parametrize("namespaces", [
    ("AR3_HOME", "XDG_CONFIG_HOME", "A8S_HOME"),
    ("XDG_CONFIG_HOME",),
    ("A8S_HOME",),
])
def test_the_scheduled_run_reads_the_state_and_the_seats_it_was_armed_with(tmp_path, monkeypatch, namespaces):
    custom = tmp_path / "custom"
    monkeypatch.delenv("AR3_HOME")
    for name in namespaces:
        monkeypatch.setenv(name, str(custom / name.lower()))
    clean_home = tmp_path / "scheduler-home"
    clean_home.mkdir()
    if "AR3_HOME" not in namespaces and "XDG_CONFIG_HOME" not in namespaces:
        monkeypatch.setenv("AR3_HOME", str(custom / "armed-ar3"))
    rig = Rig(tmp_path, monkeypatch)
    rig.node("seat-a")
    armed_state = au.state_path(rig.root)
    argv = STORE[au.install_id(rig.root)]["argv"]
    assert argv == [sys.executable, str(au.launcher_path(rig.root))]

    for name in ("AR3_HOME", "XDG_CONFIG_HOME", "A8S_HOME", "R4T_HOME", "K7E_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(clean_home))
    started = _launched(Path(argv[1]), monkeypatch)
    assert started[1] == str(rig.root / "apps" / "ar3" / "cli.py")
    argv = started

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return rig.after_slot

    monkeypatch.setattr(au, "datetime", Clock)
    assert ar3.main(argv[2:]) == 0
    assert not (clean_home / ".config").exists()
    state = json.loads(armed_state.read_text())
    assert state["last_result"]["outcome"] == "updated"
    seen = rig.ps_env[0]
    for name in namespaces:
        if name != "AR3_HOME":
            assert seen[name] == str(custom / name.lower())
    assert "AR3_HOME" not in os.environ and "A8S_HOME" not in os.environ


def test_arm_failure_leaves_the_install_unarmed(tmp_path):
    root = _root(tmp_path)
    MemoryTrigger.fail_install = True
    with pytest.raises(trig.TriggerError):
        au.arm(root)
    assert not au.load(root).get("enabled")


def test_disarm_removes_the_entry_and_records_off(tmp_path):
    root = _root(tmp_path)
    au.arm(root)
    au.disarm(root)
    assert STORE == {}
    assert au.load(root)["enabled"] is False


def test_ensure_arms_a_fresh_install_once(tmp_path):
    root = _root(tmp_path)
    assert au.ensure(root) == "armed"
    assert au.ensure(root) == "present"


def test_ensure_rearms_when_the_entry_is_gone(tmp_path):
    root = _root(tmp_path)
    au.ensure(root)
    STORE.clear()
    assert au.ensure(root) == "armed"
    assert au.install_id(root) in STORE


def test_ensure_respects_off(tmp_path):
    root = _root(tmp_path)
    au.arm(root)
    au.disarm(root)
    assert au.ensure(root) == "off"
    assert STORE == {}


def test_ensure_records_off_when_the_installer_says_so(tmp_path, monkeypatch):
    root = _root(tmp_path)
    monkeypatch.setenv("AR3_AUTO_UPDATE", "0")
    assert au.ensure(root) == "off"
    assert au.load(root)["enabled"] is False and STORE == {}
    monkeypatch.delenv("AR3_AUTO_UPDATE")
    assert au.ensure(root) == "off"


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs permission checks")
def test_ensure_leaves_a_tree_the_account_cannot_write(tmp_path):
    root = _root(tmp_path)
    root.chmod(0o555)
    try:
        assert au.ensure(root) == "system"
    finally:
        root.chmod(0o755)
    assert STORE == {} and au.load(root) == {}


def test_ensure_reports_a_missing_scheduler_without_raising(tmp_path):
    root = _root(tmp_path)
    MemoryTrigger.unavailable = True
    assert au.ensure(root) == "unavailable"


# ---------- cron entry ----------

class FakeCrontab:
    """A `crontab` program on PATH backed by a file."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.dir = tmp_path / "fakebin"
        self.dir.mkdir()
        self.store = tmp_path / "crontab.store"
        self.reject = tmp_path / "crontab.reject"
        script = self.dir / "crontab"
        script.write_text(
            "#!/usr/bin/env bash\n"
            f'S="{self.store}"\n'
            f'if [ -e "{self.reject}" ]; then echo "refused" >&2; exit 1; fi\n'
            'case "$1" in\n'
            '  -l) if [ -e "$S" ]; then cat "$S"; else echo "no crontab for me" >&2; exit 1; fi;;\n'
            '  -r) rm -f "$S";;\n'
            '  -) cat > "$S";;\n'
            "esac\n"
        )
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{self.dir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setattr(subprocess, "run", _REAL_RUN)

    def read(self) -> str:
        return self.store.read_text() if self.store.exists() else ""


@pytest.fixture
def fake_crontab(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("fake crontab is a bash script")
    return FakeCrontab(tmp_path, monkeypatch)


A8S_LINE = "@reboot /usr/bin/python3 /srv/a8s.py start --startup E --startup-home /h n1 # a8s-autostart home=abc node=n1"
OTHER = "0 3 * * * /usr/local/bin/backup --all # nightly\n" + A8S_LINE + "\n"


def _cron(tmp_path):
    return trig.CronTrigger(tmp_path / "bak")


def test_cron_adds_one_line_and_leaves_the_rest_byte_identical(fake_crontab, tmp_path):
    root = _root(tmp_path)
    fake_crontab.store.write_text(OTHER)
    _cron(tmp_path).install("abc1234567", root, "/usr/bin/python3", {"PATH": "/a b/bin:/usr/bin"})
    text = fake_crontab.read()
    assert text.startswith(OTHER)
    added = text[len(OTHER):]
    assert added.count("\n") == 1
    assert added.startswith("*/15 * * * * env 'PATH=/a b/bin:/usr/bin' /usr/bin/python3 ")
    assert f"/usr/bin/python3 {root} >/dev/null 2>&1" in added
    assert added.rstrip().endswith("# ar3-autoupdate install=abc1234567")
    assert not added.startswith("@reboot")


def test_cron_remove_restores_the_original_bytes(fake_crontab, tmp_path):
    root = _root(tmp_path)
    fake_crontab.store.write_text(OTHER)
    cron = _cron(tmp_path)
    cron.install("abc1234567", root, "/usr/bin/python3", {})
    cron.remove("abc1234567")
    assert fake_crontab.read() == OTHER


def test_cron_reinstall_replaces_instead_of_duplicating(fake_crontab, tmp_path):
    root = _root(tmp_path)
    cron = _cron(tmp_path)
    cron.install("abc1234567", root, "/usr/bin/python3", {"PATH": "/one"})
    cron.install("abc1234567", root, "/usr/bin/python3", {"PATH": "/two"})
    assert fake_crontab.read().count("ar3-autoupdate") == 1
    assert "/two" in fake_crontab.read()


def test_cron_keeps_two_installs_apart(fake_crontab, tmp_path):
    cron = _cron(tmp_path)
    cron.install("aaaaaaaaaa", _root(tmp_path, "a"), "/py", {})
    cron.install("bbbbbbbbbb", _root(tmp_path, "b"), "/py", {})
    cron.remove("aaaaaaaaaa")
    assert cron.installed("aaaaaaaaaa") is None
    assert cron.installed("bbbbbbbbbb") is not None


def test_cron_escapes_percent(fake_crontab, tmp_path):
    cron = _cron(tmp_path)
    cron.install("abc1234567", _root(tmp_path, "100%"), "/py", {})
    assert "/100\\% >/dev/null" in fake_crontab.read()


def test_cron_last_entry_of_an_empty_crontab_leaves_none(fake_crontab, tmp_path):
    cron = _cron(tmp_path)
    cron.install("abc1234567", _root(tmp_path), "/py", {})
    cron.remove("abc1234567")
    assert not fake_crontab.store.exists()


def test_cron_rejected_write_is_an_error(fake_crontab, tmp_path):
    fake_crontab.reject.write_text("")
    with pytest.raises(trig.TriggerError):
        _cron(tmp_path).install("abc1234567", _root(tmp_path), "/py", {})


def test_cron_keeps_a_prior_copy(fake_crontab, tmp_path):
    fake_crontab.store.write_text("0 0 * * * x\n")
    _cron(tmp_path).install("abc1234567", _root(tmp_path), "/py", {})
    assert (tmp_path / "bak").read_text() == "0 0 * * * x\n"


def test_cron_without_a_crontab_program_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(trig.TriggerUnavailable, match="needs cron"):
        _cron(tmp_path).install("abc1234567", _root(tmp_path), "/py", {})


def test_cron_takes_the_account_lock_a8s_autostart_takes(fake_crontab, tmp_path, monkeypatch):
    seen = []
    real = crontab.file_lock

    def spy(path, **kw):
        seen.append(path)
        return real(path, **kw)

    monkeypatch.setattr(crontab, "file_lock", spy)
    _cron(tmp_path).install("abc1234567", _root(tmp_path), "/py", {})
    assert seen == [crontab.account_lock_root() / "crontab.lock"]


# ---------- launchd entry ----------

class FakeLaunchctl:
    def __init__(self, monkeypatch, gui=True):
        self.calls = []
        self.gui = gui
        self.loaded = set()
        monkeypatch.setattr(trig, "_run", self.run)
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)

    def run(self, argv):
        self.calls.append(list(argv))
        rc = 0
        if argv[:2] == ["launchctl", "print"]:
            target = argv[2]
            if target == "gui/501":
                rc = 0 if self.gui else 113
            else:
                rc = 0 if target in self.loaded else 113
        elif argv[:2] == ["launchctl", "bootstrap"]:
            self.loaded.add(f"gui/501/{plistlib.loads(Path(argv[3]).read_bytes())['Label']}")
        elif argv[:2] == ["launchctl", "bootout"]:
            self.loaded.discard(argv[2])
        return SimpleNamespace(returncode=rc, stdout="", stderr="")


def test_launchd_writes_a_15_minute_agent_and_bootstraps_it(tmp_path, monkeypatch):
    ctl = FakeLaunchctl(monkeypatch)
    agents = tmp_path / "LA"
    launchd = trig.LaunchdTrigger(agents)
    root = _root(tmp_path)
    assert launchd.install("abc1234567", root, "/usr/bin/python3", {"PATH": "/opt/homebrew/bin:/usr/bin"}) == ""
    plist = plistlib.loads((agents / "com.ar3.autoupdate.abc1234567.plist").read_bytes())
    assert plist["Label"] == "com.ar3.autoupdate.abc1234567"
    assert plist["StartInterval"] == 900
    assert plist["ProgramArguments"] == ["/usr/bin/python3", str(root)]
    assert plist["EnvironmentVariables"] == {"PATH": "/opt/homebrew/bin:/usr/bin"}
    assert ["launchctl", "bootstrap", "gui/501", str(agents / "com.ar3.autoupdate.abc1234567.plist")] in ctl.calls
    assert launchd.installed("abc1234567") is not None


def test_launchd_rearm_boots_the_old_job_out_first(tmp_path, monkeypatch):
    ctl = FakeLaunchctl(monkeypatch)
    launchd = trig.LaunchdTrigger(tmp_path / "LA")
    launchd.install("abc1234567", _root(tmp_path), "/py", {})
    launchd.install("abc1234567", _root(tmp_path), "/py", {})
    verbs = [c[1] for c in ctl.calls if c[1] in ("bootout", "bootstrap")]
    assert verbs == ["bootstrap", "bootout", "bootstrap"]


def test_launchd_without_a_login_session_says_armed_at_next_login(tmp_path, monkeypatch):
    ctl = FakeLaunchctl(monkeypatch, gui=False)
    launchd = trig.LaunchdTrigger(tmp_path / "LA")
    note = launchd.install("abc1234567", _root(tmp_path), "/py", {})
    assert "next login" in note
    assert not any(c[1] == "bootstrap" for c in ctl.calls)
    assert launchd.installed("abc1234567") is not None


def test_launchd_remove_boots_out_and_deletes(tmp_path, monkeypatch):
    ctl = FakeLaunchctl(monkeypatch)
    launchd = trig.LaunchdTrigger(tmp_path / "LA")
    launchd.install("abc1234567", _root(tmp_path), "/py", {})
    launchd.remove("abc1234567")
    assert ["launchctl", "bootout", "gui/501/com.ar3.autoupdate.abc1234567"] in ctl.calls
    assert launchd.installed("abc1234567") is None
    launchd.remove("abc1234567")


# ---------- scheduled task entry ----------

class FakeSchtasks:
    def __init__(self, monkeypatch, tmp_path):
        self.calls = []
        self.tasks = {}
        self.xml_seen = None
        monkeypatch.setattr(trig, "_run", self.run)
        monkeypatch.setattr(trig.shutil, "which", lambda name: "schtasks" if name == "schtasks" else None)

    def run(self, argv):
        self.calls.append(list(argv))
        if argv[0] == "whoami":
            return SimpleNamespace(returncode=0, stdout='"host\\user","S-1-5-21-1-2-3-1001"\n', stderr="")
        if argv[:2] == ["schtasks", "/Create"]:
            self.xml_seen = Path(argv[argv.index("/XML") + 1]).read_text(encoding="utf-16")
            self.tasks[argv[argv.index("/TN") + 1]] = self.xml_seen
        elif argv[:2] == ["schtasks", "/Delete"]:
            self.tasks.pop(argv[argv.index("/TN") + 1], None)
        elif argv[:2] == ["schtasks", "/Query"]:
            found = argv[argv.index("/TN") + 1] in self.tasks
            return SimpleNamespace(returncode=0 if found else 1, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_schtasks_task_repeats_every_15_minutes_for_an_interactive_user(tmp_path, monkeypatch):
    fake = FakeSchtasks(monkeypatch, tmp_path)
    (tmp_path / "py").mkdir()
    (tmp_path / "py" / "python.exe").write_text("")
    (tmp_path / "py" / "pythonw.exe").write_text("")
    task = trig.SchtasksTrigger(tmp_path / "scratch")
    root = _root(tmp_path)
    task.install("abc1234567", root, str(tmp_path / "py" / "python.exe"), {"PATH": "C:\\x"})
    xml = fake.xml_seen
    assert "<Interval>PT15M</Interval>" in xml and "<Duration>P1D</Duration>" in xml
    assert "<LogonType>InteractiveToken</LogonType>" in xml
    assert "<UserId>S-1-5-21-1-2-3-1001</UserId>" in xml
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml
    assert "<ExecutionTimeLimit>PT1H</ExecutionTimeLimit>" in xml
    assert "pythonw.exe</Command>" in xml
    assert f"<Arguments>{root}</Arguments>" in xml
    assert ["schtasks", "/Create", "/TN", "\\ar3\\autoupdate-abc1234567"] == fake.calls[1][:4]
    assert not list((tmp_path / "scratch").glob("*.xml"))
    assert task.installed("abc1234567") == "\\ar3\\autoupdate-abc1234567"


def test_schtasks_remove_deletes_the_task_and_is_idempotent(tmp_path, monkeypatch):
    fake = FakeSchtasks(monkeypatch, tmp_path)
    task = trig.SchtasksTrigger(tmp_path / "scratch")
    task.install("abc1234567", _root(tmp_path), "pythonw.exe", {})
    task.remove("abc1234567")
    assert task.installed("abc1234567") is None
    task.remove("abc1234567")
    assert sum(1 for c in fake.calls if c[:2] == ["schtasks", "/Delete"]) == 1


def test_schtasks_needs_the_program(tmp_path, monkeypatch):
    monkeypatch.setattr(trig.shutil, "which", lambda name: None)
    with pytest.raises(trig.TriggerUnavailable):
        trig.SchtasksTrigger(tmp_path).installed("abc1234567")


# ---------- platform choice ----------

@pytest.mark.parametrize(
    "platform, windows, kind",
    [("linux", False, trig.CronTrigger), ("darwin", False, trig.LaunchdTrigger), ("win32", True, trig.SchtasksTrigger)],
)
def test_select_picks_the_scheduler_of_the_platform(tmp_path, monkeypatch, platform, windows, kind):
    monkeypatch.setattr(trig.sys, "platform", platform)
    monkeypatch.setattr(trig, "IS_WINDOWS", windows)
    assert isinstance(_REAL_SELECT(tmp_path), kind)


def test_select_refuses_an_unknown_platform(tmp_path, monkeypatch):
    monkeypatch.setattr(trig.sys, "platform", "plan9")
    monkeypatch.setattr(trig, "IS_WINDOWS", False)
    with pytest.raises(trig.TriggerUnavailable):
        _REAL_SELECT(tmp_path)


# ---------- window ----------

def test_window_parses_and_rejects():
    assert au.parse_window("02:00-05:00") == (120, 300)
    assert au.parse_window("23:30-01:15") == (1410, 75)
    assert au.window_minutes((120, 300)) == 180
    assert au.window_minutes((1410, 75)) == 105
    for bad in ("", "2:00", "02:00-02:00", "25:00-03:00", "02:00-03:60", "02:00-03:00-04:00", "ab:cd-ef:gh"):
        with pytest.raises(ValueError):
            au.parse_window(bad)


def test_current_night_edges():
    window = au.parse_window("02:00-05:00")
    day = datetime(2026, 11, 3).date()
    assert au.current_night(window, datetime(2026, 11, 3, 1, 59)) is None
    assert au.current_night(window, datetime(2026, 11, 3, 2, 0))[0] == day
    assert au.current_night(window, datetime(2026, 11, 3, 4, 59))[0] == day
    assert au.current_night(window, datetime(2026, 11, 3, 5, 0)) is None


def test_a_window_across_midnight_belongs_to_the_night_it_starts():
    window = au.parse_window("23:00-02:00")
    late = au.current_night(window, datetime(2026, 11, 3, 23, 30))
    early = au.current_night(window, datetime(2026, 11, 4, 0, 30))
    assert late[0] == early[0] == datetime(2026, 11, 3).date()
    assert early[2] == datetime(2026, 11, 4, 2, 0)
    assert au.current_night(window, datetime(2026, 11, 4, 2, 0)) is None
    assert au.current_night(window, datetime(2026, 11, 3, 22, 59)) is None


def test_the_slot_is_stable_within_a_night_and_moves_between_nights(tmp_path):
    root = _root(tmp_path)
    window = au.parse_window("02:00-05:00")
    day = datetime(2026, 11, 3).date()
    assert au.slot_time(root, window, day) == au.slot_time(root, window, day)
    slots = {au.slot_time(root, window, day + timedelta(days=n)) - datetime.combine(day + timedelta(days=n), datetime.min.time())
             for n in range(10)}
    assert len(slots) > 1
    for offset in slots:
        assert timedelta(hours=2) <= offset < timedelta(hours=2, minutes=120)


def test_a_short_window_still_has_a_slot(tmp_path):
    root = _root(tmp_path)
    window = au.parse_window("02:00-02:30")
    assert au.slot_time(root, window, datetime(2026, 11, 3).date()) == datetime(2026, 11, 3, 2, 0)


def test_next_attempt_follows_the_slot_then_the_tick_then_tomorrow(tmp_path):
    root = _root(tmp_path)
    state = {"enabled": True, "window": "02:00-05:00"}
    day = datetime(2026, 11, 3).date()
    slot = au.slot_time(root, au.parse_window("02:00-05:00"), day)
    assert au.next_attempt(root, state, datetime(2026, 11, 3, 0, 5)) == slot
    after = slot + timedelta(minutes=1)
    got = au.next_attempt(root, state, after)
    assert got >= after and got.minute % 15 == 0 and got - after <= timedelta(minutes=15)
    state["done_night"] = day.isoformat()
    tomorrow = au.slot_time(root, au.parse_window("02:00-05:00"), day + timedelta(days=1))
    assert au.next_attempt(root, state, after) == tomorrow
    assert au.next_attempt(root, {"enabled": False}, after) is None


# ---------- the night run ----------

NIGHT = datetime(2026, 11, 3).date()
NOT_REACHED = object()


class Rig:
    """An armed git install on the public channel with one release ahead of
    it, and every outside answer modeled."""

    def __init__(self, tmp_path, monkeypatch):
        self.monkeypatch = monkeypatch
        self.root = _root(tmp_path)
        (self.root / ".git").mkdir()
        (self.root / "get.sh").write_text("#!/bin/sh\n")
        (self.root / "apps" / "ar3").mkdir(parents=True)
        (self.root / "apps" / "ar3" / "cli.py").write_text("")
        (self.root / "VERSION").write_text("0.1.0\n")
        self.refusal = None
        self.git = {
            "symbolic-ref": "main",
            "remote": "https://github.com/witw-llc/ar3.git",
            "rev-parse": "oldsha",
            "status": "",
        }
        self.remote_head = "newsha"
        self.commits = {"oldsha"}
        self.ls_remote = None
        self.ls_remote_calls = []
        self.latest = "0.1.1"
        self.nodes = []
        self.ps = None
        self.installer_result = (0, "Updating ar3\n", False)
        self.installer_calls = []
        self.restart = (0, "updating 0 node(s)\nupdate complete\n")
        self.restart_calls = []
        self.latest_calls = 0
        self.versions = {}
        self.nodes_after = None
        self.git_calls = []
        self.git_reset = (0, "")
        self.tells = []
        self.tell_result = (0, "")
        self.tell_error = None
        self.ps_env = []
        monkeypatch.setattr(au, "HEALTH_POLL_S", 0.0)
        monkeypatch.setattr(au, "HEALTH_STEP_S", 0.0)
        monkeypatch.setattr(ar3, "REPO_ROOT", self.root)
        monkeypatch.setattr(ar3, "update_refusal", lambda r: self.refusal)
        monkeypatch.setattr(ar3, "_git_out", self._git_out)
        monkeypatch.setattr(ar3, "_a8s", lambda: "/fake/a8s")
        monkeypatch.setattr(ar3, "_ps_nodes", self._ps)
        monkeypatch.setattr(au.ar3ver, "latest_public_version", self._latest)
        monkeypatch.setattr(au, "run_installer", self._installer)
        monkeypatch.setattr(ar3.subprocess, "run", self._run)
        self.state = au.arm(self.root) == "" and au.load(self.root)
        self.slot = au.slot_time(self.root, au.parse_window(au.DEFAULT_WINDOW), NIGHT)
        self.after_slot = self.slot + timedelta(minutes=1)

    def _git_out(self, root, *args):
        if args[:2] == ("rev-parse", "--verify"):
            ref = args[-1].removesuffix("^{commit}")
            return ref if ref in self.commits else None
        return self.git.get(args[0])

    def _ps(self, a8s):
        self.ps_env.append({k: os.environ.get(k) for k in ("A8S_HOME", "XDG_CONFIG_HOME", "AR3_HOME")})
        if self.ps is NOT_REACHED:
            return None
        if self.restart_calls and self.nodes_after is not None:
            return self.nodes_after
        return self.nodes

    def _latest(self, timeout=0):
        self.latest_calls += 1
        return self.latest

    def _installer(self, host, root, env, timeout):
        self.installer_calls.append({"env": env, "timeout": timeout, "pending": au.load(root).get("pending")})
        rc, out, timed_out = self.installer_result
        if rc == 0 and self.latest:
            self.install_latest()
        return rc, out, timed_out

    def install_latest(self):
        """What the installer does: the tree moves to the remote's commit, or
        a release unpack to the release it was told to fetch."""
        (self.root / "VERSION").write_text(f"{self.latest}\n")
        self.git["rev-parse"] = self.remote_head
        if (self.root / ".ar3-release").exists():
            (self.root / ".ar3-release").write_text(f"v{self.latest}\n")

    def as_unpack(self):
        """A release unpack at v0.1.0, on a machine with no git."""
        import shutil as _shutil

        _shutil.rmtree(self.root / ".git")
        (self.root / ".ar3-release").write_text("v0.1.0\n")
        self.monkeypatch.setattr(au, "_has_git", lambda env: False)

    def _run(self, argv, **kw):
        name = Path(argv[0]).name
        if argv[-1:] == ["--version"]:
            seen = self.versions.get(name, (self.root / "VERSION").read_text().strip())
            return SimpleNamespace(returncode=0, stdout=f"{name} {seen} (ar3, python 3)\n", stderr="")
        if name == "git" and "ls-remote" in argv:
            self.ls_remote_calls.append((list(argv), kw))
            rc, out = self.ls_remote or (0, f"{self.remote_head}\tHEAD\n")
            return SimpleNamespace(returncode=rc, stdout=out if rc == 0 else "", stderr="" if rc == 0 else out)
        if name == "git":
            self.git_calls.append(list(argv))
            if self.git_reset[0] == 0:
                (self.root / "VERSION").write_text("0.1.0\n")
                self.git["rev-parse"] = argv[-1]
            return SimpleNamespace(returncode=self.git_reset[0], stdout="", stderr=self.git_reset[1])
        if name == "tell":
            if self.tell_error:
                raise self.tell_error
            self.tells.append((list(argv), kw))
            return SimpleNamespace(returncode=self.tell_result[0], stdout="", stderr=self.tell_result[1])
        self.restart_calls.append((list(argv), kw))
        return SimpleNamespace(returncode=self.restart[0], stdout=self.restart[1], stderr="")

    def run(self, now=None, force=False):
        return au.run(ar3, now=now or self.after_slot, force=force)

    def state_now(self):
        return au.load(self.root)

    def log(self):
        try:
            return au.log_path(self.root).read_text().splitlines()
        except OSError:
            return []

    def node(self, name, busy="idle", pid=None):
        pid = pid or 1000 + len(self.nodes)
        self.nodes.append({"name": name, "pid": pid, "busy": busy})


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


def test_outside_the_window_a_tick_does_nothing_and_asks_nobody(rig):
    assert rig.run(datetime(2026, 11, 3, 12, 0)) is None
    assert rig.run(datetime(2026, 11, 3, 5, 0)) is None
    assert rig.latest_calls == 0 and rig.installer_calls == []


def test_before_the_slot_a_tick_exits_without_network(rig):
    assert rig.run(rig.slot - timedelta(minutes=1)) is None
    assert rig.latest_calls == 0


def test_after_the_slot_a_tick_updates_and_restarts(rig, capsys):
    rig.node("seat-a")
    assert rig.run() == "updated"
    result = rig.state_now()["last_result"]
    assert (result["outcome"], result["from"], result["to"]) == ("updated", "0.1.0", "0.1.1")
    assert (rig.root / "VERSION").read_text() == "0.1.1\n"
    assert rig.state_now()["done_night"] == "2026-11-03"
    pending = rig.installer_calls[0]["pending"]
    assert pending["previous"] == {"kind": "git", "ref": "oldsha", "version": "0.1.0"}
    assert pending["target"] == {"kind": "git", "ref": "newsha"} and pending["seats"] == ["seat-a"]
    assert "pending" not in rig.state_now()
    assert rig.state_now()["last_check"]["latest"] == "0.1.1"
    assert capsys.readouterr().out == ""


def test_the_night_is_done_after_an_update(rig):
    rig.run()
    rig.latest_calls = 0
    assert rig.run(rig.after_slot + timedelta(minutes=15)) is None
    assert rig.latest_calls == 0 and len(rig.installer_calls) == 1


def test_the_next_night_runs_again(rig):
    rig.run()
    rig.latest = "0.1.2"
    tomorrow = au.slot_time(rig.root, au.parse_window(au.DEFAULT_WINDOW), NIGHT + timedelta(days=1))
    assert rig.run(tomorrow + timedelta(minutes=1)) == "updated"


def test_no_attempt_starts_after_the_window_closes(rig):
    rig.latest = None
    assert rig.run() == "skip-network"
    assert rig.run(datetime(2026, 11, 3, 5, 0)) is None
    assert rig.run(datetime(2026, 11, 3, 5, 15)) is None


def test_the_installer_pulls_without_restarting_and_never_asks(rig):
    rig.monkeypatch.setenv("AR3_VERSION", "v9.9.9")
    rig.monkeypatch.setenv("AR3_CHANNEL", "beta")
    rig.run()
    call = rig.installer_calls[0]
    env = call["env"]
    assert env["AR3_NO_RESTART"] == "1" and env["AR3_DIR"] == str(rig.root)
    assert env["A8S_UPDATE_WAIT_SECONDS"] == "0"
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"
    assert "AR3_VERSION" not in env and "AR3_CHANNEL" not in env
    assert call["timeout"] == 600


def test_the_restart_is_a8s_update_of_the_new_tree_with_no_wait(rig):
    rig.run()
    argv, kw = rig.restart_calls[0]
    assert argv == [str(rig.root / "a8s"), "update", "--wait", "0"]
    assert kw["stdin"] == subprocess.DEVNULL


def test_the_captured_environment_reaches_the_run(rig):
    state = rig.state_now()
    state["env"] = {"PATH": "/captured/bin", "HTTPS_PROXY": "http://proxy.invalid:1"}
    au.save(rig.root, state)
    seen = {}
    rig.monkeypatch.setattr(au.ar3ver, "latest_public_version", lambda t=0: seen.update(path=os.environ["PATH"]) or "0.1.1")
    before = os.environ["PATH"]
    rig.run()
    assert seen["path"] == "/captured/bin"
    assert rig.installer_calls[0]["env"]["HTTPS_PROXY"] == "http://proxy.invalid:1"
    assert os.environ["PATH"] == before


def test_off_does_nothing_and_asks_nobody(rig):
    au.disarm(rig.root)
    assert rig.run() == "skip-off"
    assert rig.latest_calls == 0 and rig.installer_calls == []


def test_a_second_tick_exits_while_one_runs(rig):
    with au.file_lock(au.lock_path(rig.root)):
        assert rig.run() is None
    assert rig.installer_calls == []


def test_a_busy_seat_holds_the_update_and_the_night_stays_open(rig):
    rig.node("seat-a", "busy")
    assert rig.run() == "skip-busy"
    assert rig.installer_calls == []
    assert "done_night" not in rig.state_now()
    assert "seat-a" in rig.state_now()["last_result"]["detail"]
    rig.nodes[0]["busy"] = "idle"
    assert rig.run(rig.after_slot + timedelta(minutes=15)) == "updated"


def test_a_probe_that_failed_is_not_idle(rig):
    rig.node("seat-a", "unknown")
    assert rig.run() == "skip-busy"


def test_a_busy_member_holds_its_whole_group(rig):
    rig.node("seat-a", "idle", pid=77)
    rig.node("seat-b", "busy", pid=77)
    rig.run()
    detail = rig.state_now()["last_result"]["detail"]
    assert "seat-a" in detail and "seat-b" in detail


def test_ps_that_cannot_answer_is_unknown_and_retries(rig):
    rig.ps = NOT_REACHED
    assert rig.run() == "skip-unknown"
    assert rig.installer_calls == [] and "done_night" not in rig.state_now()


def test_a_seat_that_turns_busy_in_the_gap_is_left_alone_and_a_later_tick_restarts_it(rig):
    rig.node("seat-a")
    rig.restart = (0, "update: seat-a is busy (a turn is in flight) - left on its current handler; run again\n")
    assert rig.run() == "partial"
    state = rig.state_now()
    assert state["pending"]["stage"] == "restart" and "done_night" not in state
    rig.restart = (0, "update complete\n")
    assert rig.run(rig.after_slot + timedelta(minutes=15)) == "updated"
    state = rig.state_now()
    assert "pending" not in state and state["done_night"] == "2026-11-03"
    assert state["last_result"]["from"] == "0.1.0" and state["last_result"]["to"] == "0.1.1"
    assert len(rig.installer_calls) == 1


def test_a_partial_restart_waits_for_idle_before_trying_again(rig):
    rig.node("seat-a")
    rig.restart = (0, "x left on its current handler\n")
    rig.run()
    rig.nodes[0]["busy"] = "busy"
    assert rig.run(rig.after_slot + timedelta(minutes=15)) == "skip-busy"
    assert rig.state_now()["pending"]["stage"] == "restart"


def test_a_failed_restart_is_a_failure(rig):
    rig.restart = (3, "boom\n")
    assert rig.run() == "failed"


# ---------- needs input ----------

def test_a_pull_that_times_out_wants_a_person(rig):
    rig.installer_result = (None, "Cloning...\n", True)
    assert rig.run() == "skip-input"
    assert "done_night" not in rig.state_now()
    assert rig.restart_calls == []


@pytest.mark.parametrize("text", [
    "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
    "Host key verification failed.",
    "git@github.com: Permission denied (publickey).",
    "sudo: a password is required",
    "fatal: could not read Password for 'https://x@host'",
])
def test_output_that_asks_for_input_is_needs_input(rig, text):
    rig.installer_result = (128, text + "\n", False)
    assert rig.run() == "skip-input"
    assert rig.restart_calls == []


def test_the_installer_gets_no_stdin_and_no_terminal(tmp_path, monkeypatch):
    script = tmp_path / "fake-sh"
    script.write_text(
        "#!/bin/sh\n"
        "echo \"prompts=$GIT_TERMINAL_PROMPT ssh=$GIT_SSH_COMMAND dir=$AR3_DIR\"\n"
        "if read line; then echo \"stdin-had-data\"; else echo \"stdin-closed\"; fi\n"
        "if (: < /dev/tty) 2>/dev/null; then echo \"tty-open\"; else echo \"no-tty\"; fi\n"
    )
    script.chmod(0o755)
    root = _root(tmp_path)
    monkeypatch.setattr(ar3, "_posix_sh", lambda: str(script))
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes", "AR3_DIR": str(root)}
    rc, out, timed_out = au.run_installer(ar3, root, env, 20)
    assert (rc, timed_out) == (0, False)
    assert "prompts=0 ssh=ssh -o BatchMode=yes" in out
    assert "stdin-closed" in out and "no-tty" in out


def test_a_hung_installer_is_ended_with_everything_it_started(tmp_path, monkeypatch):
    marker = tmp_path / "child-pid"
    script = tmp_path / "fake-sh"
    script.write_text(f"#!/bin/sh\nsleep 60 &\necho $! > {marker}\nwait\n")
    script.chmod(0o755)
    monkeypatch.setattr(ar3, "_posix_sh", lambda: str(script))
    root = _root(tmp_path)
    rc, out, timed_out = au.run_installer(ar3, root, dict(os.environ), 1.0)
    assert rc is None and timed_out is True
    pid = int(marker.read_text())
    import time
    for _ in range(50):
        if not ar3.pid_alive(pid):
            break
        time.sleep(0.1)
    assert not ar3.pid_alive(pid)


# ---------- skips ----------

def test_the_network_being_down_retries(rig):
    rig.latest = None
    assert rig.run() == "skip-network"
    assert "done_night" not in rig.state_now()
    assert rig.state_now()["last_check"]["ok"] is False
    rig.latest = "0.1.1"
    assert rig.run(rig.after_slot + timedelta(minutes=15)) == "updated"


def test_an_install_at_the_latest_is_a_noop_and_ends_the_night(rig):
    rig.latest = "0.1.0"
    assert rig.run() == "noop"
    assert rig.state_now()["done_night"] == "2026-11-03" and rig.installer_calls == []
    assert rig.run(rig.after_slot + timedelta(minutes=15)) is None


def test_an_install_ahead_of_the_latest_is_a_noop(rig):
    rig.latest = "0.0.9"
    assert rig.run() == "noop"


def test_a_pull_that_finds_nothing_is_a_noop(rig):
    rig.monkeypatch.setattr(au, "run_installer", lambda *a: (0, "Already up to date.\n", False))
    assert rig.run() == "noop"
    assert rig.restart_calls == []


def test_a_release_that_failed_here_is_not_tried_again(rig):
    state = rig.state_now()
    state["bad_release"] = "0.1.1"
    au.save(rig.root, state)
    assert rig.run() == "skip-bad-release"
    assert rig.state_now()["done_night"] == "2026-11-03" and rig.installer_calls == []


def test_a_newer_release_than_the_bad_one_is_tried(rig):
    state = rig.state_now()
    state["bad_release"] = "0.1.1"
    au.save(rig.root, state)
    rig.latest = "0.1.2"
    assert rig.run() == "updated"


def test_a_pinned_install_skips(rig):
    rig.git["symbolic-ref"] = None
    assert rig.run() == "skip-pinned"
    assert rig.state_now()["done_night"] == "2026-11-03" and rig.latest_calls == 0


def test_a_beta_clone_skips(rig):
    rig.git["remote"] = "https://github.com/witw-llc/ar3-private.git"
    assert rig.run() == "skip-channel"
    assert rig.latest_calls == 0


@pytest.mark.parametrize("url", [
    "git@github.com:witw-llc/ar3.git",
    "https://github.com/witw-llc/ar3",
    "https://github.com/witw-llc/ar3.git/",
])
def test_the_public_mirror_in_any_spelling_is_the_stable_channel(rig, url):
    rig.git["remote"] = url
    assert rig.run() == "updated"


def test_a_working_checkout_is_refused_and_the_night_ends(rig):
    rig.refusal = "has uncommitted changes (VERSION)"
    assert rig.run() == "skip-refused"
    assert rig.state_now()["done_night"] == "2026-11-03" and rig.latest_calls == 0
    assert "uncommitted" in rig.state_now()["last_result"]["detail"]


def test_a_release_unpack_is_the_stable_channel_and_snapshots_its_tag(rig):
    rig.as_unpack()
    assert rig.run() == "updated"
    pending = rig.installer_calls[0]["pending"]
    assert pending["previous"] == {"kind": "tarball", "ref": "v0.1.0", "version": "0.1.0"}
    assert pending["target"] == {"kind": "tarball", "ref": "v0.1.1"}
    assert rig.installer_calls[0]["env"]["AR3_VERSION"] == "v0.1.1"
    assert (rig.root / ".ar3-release").read_text() == "v0.1.1\n"


def test_a_tree_that_is_neither_clone_nor_release_skips(rig):
    import shutil as _shutil

    _shutil.rmtree(rig.root / ".git")
    assert rig.run() == "skip-channel"


def test_a_failing_installer_retries_and_stops_after_three_tries(rig):
    rig.installer_result = (1, "fatal: something broke\n", False)
    for n in range(3):
        assert rig.run(rig.after_slot + timedelta(minutes=15 * n)) == "failed"
    assert rig.state_now()["done_night"] == "2026-11-03"
    assert rig.run(rig.after_slot + timedelta(minutes=60)) is None
    assert len(rig.installer_calls) == 3


def test_the_installer_output_is_kept_for_the_operator(rig):
    rig.installer_result = (0, "Updating ar3 in /somewhere\n", False)
    rig.run()
    text = au.last_run_path(rig.root).read_text()
    assert "Updating ar3 in /somewhere" in text and "a8s update" in text


def test_the_log_gets_one_line_per_outcome_change(rig):
    rig.latest = None
    rig.run()
    rig.run(rig.after_slot + timedelta(minutes=15))
    rig.run(rig.after_slot + timedelta(minutes=30))
    assert len(rig.log()) == 1 and "skip-network" in rig.log()[0]
    rig.latest = "0.1.1"
    rig.run(rig.after_slot + timedelta(minutes=45))
    assert len(rig.log()) == 2 and "updated 0.1.0 -> 0.1.1" in rig.log()[1]


def test_now_skips_the_window_but_never_the_busy_check(rig):
    noon = datetime(2026, 11, 3, 12, 0)
    rig.node("seat-a", "busy")
    assert rig.run(noon, force=True) == "skip-busy"
    rig.nodes[0]["busy"] = "idle"
    assert rig.run(noon, force=True) == "updated"


# ---------- health and rollback ----------

def test_a_healthy_update_checks_every_command_and_the_seats(rig):
    rig.node("seat-a")
    assert rig.run() == "updated"
    assert rig.state_now().get("bad_release") is None


def test_a_command_that_reports_the_wrong_version_rolls_the_release_back(rig):
    rig.node("seat-a")
    rig.versions = {"r4t": "0.1.0"}
    seen = []
    inner = rig._run

    def run(argv, **kw):
        out = inner(argv, **kw)
        seen.append(Path(argv[0]).name)
        if "reset" in argv:
            rig.versions = {}
        return out

    rig.monkeypatch.setattr(ar3.subprocess, "run", run)
    assert rig.run() == "rolled-back"
    state = rig.state_now()
    assert state["bad_release"] == "0.1.1" and state["done_night"] == "2026-11-03"
    assert "pending" not in state
    assert rig.git_calls == [["git", "-C", str(rig.root), "reset", "--hard", "oldsha"]]
    assert len(rig.restart_calls) == 2
    assert "r4t --version" in state["last_result"]["detail"]
    assert (rig.root / "VERSION").read_text() == "0.1.0\n"


def test_a_seat_that_does_not_come_back_rolls_the_release_back(rig):
    rig.node("seat-a")
    rig.node("seat-b")
    rig.nodes_after = [rig.nodes[0]]
    seen = {"n": 0}
    inner = rig._ps

    def ps(a8s):
        seen["n"] += 1
        if rig.git_calls:
            return rig.nodes
        return inner(a8s)

    rig.monkeypatch.setattr(ar3, "_ps_nodes", ps)
    assert rig.run() == "rolled-back"
    assert "seat-b not running" in rig.state_now()["last_result"]["detail"]


def test_a_ps_that_will_not_answer_after_the_restart_is_unhealthy_and_holds_the_rollback(rig):
    answers = {"ps": True}

    def ps(a8s):
        return rig.nodes if answers["ps"] else None

    rig.monkeypatch.setattr(ar3, "_ps_nodes", ps)
    rig.monkeypatch.setattr(au, "_restart_nodes", lambda night: (answers.update(ps=False), (0, "ok\n"))[1])
    assert rig.run() == "rollback-partial"
    state = rig.state_now()
    assert "did not answer" in state["last_result"]["detail"] and state["pending"]["stage"] == "rollback"
    assert rig.git_calls == [] and (rig.root / "VERSION").read_text() == "0.1.1\n"
    answers["ps"] = True
    rig.monkeypatch.setattr(au, "_restart_nodes", lambda night: (0, "ok\n"))
    assert rig.run(rig.after_slot + timedelta(minutes=15)) == "rolled-back"
    assert (rig.root / "VERSION").read_text() == "0.1.0\n"


def test_a_rollback_that_does_not_come_up_is_reported_as_failed(rig):
    rig.node("seat-a")
    rig.versions = {"k7e": "9.9.9"}
    assert rig.run() == "rollback-failed"
    state = rig.state_now()
    assert state["bad_release"] == "0.1.1" and state["done_night"] == "2026-11-03"
    assert "did not come up either" in state["last_result"]["detail"]


def test_a_git_reset_that_fails_is_a_failed_rollback(rig):
    rig.versions = {"ar3": "0.0.0"}
    rig.git_reset = (128, "fatal: cannot lock ref")
    assert rig.run() == "rollback-failed"
    assert "git reset exited 128" in rig.state_now()["last_result"]["detail"]
    assert len(rig.restart_calls) == 1


def test_a_release_unpack_rolls_back_by_swapping_the_previous_tree(rig):
    import shutil as _shutil

    rig.as_unpack()
    prev = Path(str(rig.root) + ".prev")
    real_installer = rig._installer

    def installer(host, root, env, timeout):
        _shutil.copytree(root, prev)
        rc = real_installer(host, root, env, timeout)
        (root / ".ar3-release").write_text("v0.1.1\n")
        return rc

    rig.monkeypatch.setattr(au, "run_installer", installer)
    rig.versions = {"a8s": "0.0.0"}
    inner = rig._run

    def run(argv, **kw):
        if argv[-1:] == ["--version"] and (rig.root / ".ar3-release").read_text().strip() == "v0.1.0":
            rig.versions = {}
        return inner(argv, **kw)

    rig.monkeypatch.setattr(ar3.subprocess, "run", run)
    assert rig.run() == "rolled-back"
    assert (rig.root / "VERSION").read_text() == "0.1.0\n"
    assert (rig.root / ".ar3-release").read_text().strip() == "v0.1.0"
    assert not prev.exists() and not Path(str(rig.root) + ".bad").exists()
    assert rig.git_calls == []


def test_a_release_unpack_with_no_previous_tree_is_not_rolled_back(rig):
    rig.as_unpack()
    rig.versions = {"a8s": "0.0.0"}
    assert rig.run() == "rollback-blocked"
    state = rig.state_now()
    assert ".prev is missing: the tree to put back is not v0.1.0" in state["last_result"]["detail"]
    assert state["pending"]["stage"] == "rollback" and (rig.root / "VERSION").read_text() == "0.1.1\n"


def test_with_no_record_of_the_current_tree_nothing_is_pulled(rig):
    rig.git["rev-parse"] = None
    assert rig.run() == "skip-unknown"
    assert rig.installer_calls == [] and rig.restart_calls == []
    assert (rig.root / "VERSION").read_text() == "0.1.0\n"
    state = rig.state_now()
    assert "previous" not in state and "recovery" not in state and "done_night" not in state


def test_a_release_unpack_with_no_tag_is_not_pulled(rig):
    import shutil as _shutil

    _shutil.rmtree(rig.root / ".git")
    (rig.root / ".ar3-release").write_text("\n")
    assert rig.run() == "skip-unknown"
    assert rig.installer_calls == []


def test_the_bad_release_is_not_tried_again_the_next_night(rig):
    rig.versions = {"r4t": "0.0.0"}
    inner = rig._run

    def run(argv, **kw):
        if "reset" in argv:
            rig.versions = {}
        return inner(argv, **kw)

    rig.monkeypatch.setattr(ar3.subprocess, "run", run)
    assert rig.run() == "rolled-back"
    rig.latest_calls = 0
    tomorrow = au.slot_time(rig.root, au.parse_window(au.DEFAULT_WINDOW), NIGHT + timedelta(days=1))
    assert rig.run(tomorrow + timedelta(minutes=1)) == "skip-bad-release"
    assert len(rig.installer_calls) == 1


# ---------- get.sh, for real ----------

GET_SH = Path(__file__).resolve().parents[3] / "get.sh"


def _git(*args, cwd=None):
    return _REAL_RUN(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    )


def _install_git_of_get_sh(tmp_path, root: Path, repo: str):
    """The real `_install_git` of get.sh, run by sh without `main`."""
    text = GET_SH.read_text(encoding="utf-8")
    assert text.rstrip().endswith('main "$@"')
    script = tmp_path / "install-git.sh"
    script.write_text(text.rstrip()[: -len('main "$@"')] + "_install_git\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("AR3_")}
    env.update(AR3_DIR=str(root), AR3_REPO=repo, AR3_VERSION="", HOME=str(tmp_path))
    return _REAL_RUN(["sh", str(script)], env=env, capture_output=True, text=True, timeout=120)


@pytest.fixture
def unpack_and_origin(tmp_path, monkeypatch):
    """A release unpack at 0.1.0 and a local git repository one release ahead."""
    if os.name != "posix" or not shutil_which("git") or not shutil_which("sh"):
        pytest.skip("needs sh and git")
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin)
    (origin / "VERSION").write_text("0.1.1\n")
    _git("add", "VERSION", cwd=origin)
    _git("commit", "-q", "-m", "0.1.1", cwd=origin)
    root = tmp_path / "install"
    root.mkdir()
    (root / "VERSION").write_text("0.1.0\n")
    (root / ".ar3-release").write_text("v0.1.0\n")
    monkeypatch.setattr(ar3, "REPO_ROOT", root)
    return root, origin


def test_a_release_unpack_that_get_sh_turns_into_a_clone_can_be_rolled_back(tmp_path, unpack_and_origin):
    root, origin = unpack_and_origin
    previous = au._tree(au._snapshot(ar3, root))
    assert previous == {"kind": "tarball", "ref": "v0.1.0", "version": "0.1.0"}
    done = _install_git_of_get_sh(tmp_path, root, f"file://{origin}")
    assert done.returncode == 0, done.stderr
    assert (root / ".git").is_dir() and (root / "VERSION").read_text() == "0.1.1\n"
    prev = Path(str(root) + ".prev")
    assert (prev / ".ar3-release").read_text() == "v0.1.0\n"
    night = au.Night(ar3, root, {}, "2026-11-03", datetime(2026, 11, 3, 3, 0), {})
    assert au.rollback(night, previous) == (True, "")
    assert (root / "VERSION").read_text() == "0.1.0\n" and (root / ".ar3-release").is_file()
    assert not (root / ".git").exists() and not prev.exists()


def test_a_clone_that_fails_leaves_the_release_unpack_where_it_was(tmp_path, unpack_and_origin):
    root, _origin = unpack_and_origin
    done = _install_git_of_get_sh(tmp_path, root, f"file://{tmp_path / 'no-such-repo'}")
    assert done.returncode != 0
    assert (root / "VERSION").read_text() == "0.1.0\n" and (root / ".ar3-release").read_text() == "v0.1.0\n"
    assert not (root / ".git").exists() and not Path(str(root) + ".prev").exists()


# ---------- recovery ----------

class _Killed(BaseException):
    """The process ending under the run: a reboot, a kill, a task time limit."""


def _mutating_installer(rig, result, then=None, head=None):
    """An installer that moves the tree to the release it was asked for (or
    to `head`) before it answers `result`, and notes the record it found
    saved when it started."""
    seen = {}

    def installer(host, root, env, timeout):
        rig.installer_calls.append({"env": env, "timeout": timeout})
        seen["pending"] = au.load(root).get("pending")
        rig.install_latest()
        if head:
            rig.git["rev-parse"] = head
        if then:
            then()
        return result

    rig.monkeypatch.setattr(au, "run_installer", installer)
    return seen


def _die():
    raise _Killed()


def _left_over_install(rig):
    """What a run that died inside the installer leaves: its record, and the
    tree moved to the release it was installing."""
    _mutating_installer(rig, (0, "", False), then=_die)
    with pytest.raises(_Killed):
        rig.run()
    rig.monkeypatch.setattr(au, "run_installer", rig._installer)
    assert (rig.root / "VERSION").read_text() == "0.1.1\n"


LATER = timedelta(minutes=15)


@pytest.mark.parametrize("result, outcome", [
    ((1, "error: could not write the shell rc\n", False), "failed"),
    ((None, "Updating ar3\n", True), "skip-input"),
])
def test_an_installer_that_changed_the_tree_and_then_failed_is_put_back(rig, result, outcome):
    rig.node("seat-a")
    seen = _mutating_installer(rig, result)
    assert rig.run() == outcome
    assert seen["pending"]["stage"] == "install"
    assert seen["pending"]["previous"] == {"kind": "git", "ref": "oldsha", "version": "0.1.0"}
    assert seen["pending"]["target"] == {"kind": "git", "ref": "newsha"}
    assert rig.git_calls == [["git", "-C", str(rig.root), "reset", "--hard", "oldsha"]]
    assert (rig.root / "VERSION").read_text() == "0.1.0\n"
    assert rig.restart_calls == []
    state = rig.state_now()
    assert "pending" not in state
    assert "put back" in state["last_result"]["detail"]
    rig.monkeypatch.setattr(au, "run_installer", rig._installer)
    assert rig.run(rig.after_slot + LATER) == "updated"


def test_a_failed_installer_whose_tree_cannot_be_put_back_keeps_the_record(rig):
    _mutating_installer(rig, (1, "boom\n", False))
    rig.git_reset = (128, "fatal: cannot lock ref")
    assert rig.run() == "rollback-failed"
    state = rig.state_now()
    assert state["pending"]["stage"] == "install" and "done_night" not in state
    assert (rig.root / "VERSION").read_text() == "0.1.1\n"
    rig.git_reset = (0, "")
    assert rig.run(rig.after_slot + LATER) == "failed"
    assert (rig.root / "VERSION").read_text() == "0.1.0\n"
    assert "pending" not in rig.state_now() and rig.restart_calls == []


def test_a_run_that_died_inside_the_installer_is_put_back_by_the_next_tick(rig):
    _left_over_install(rig)
    assert rig.state_now()["pending"]["stage"] == "install"
    assert rig.run(rig.after_slot + LATER) == "failed"
    assert (rig.root / "VERSION").read_text() == "0.1.0\n"
    assert rig.restart_calls == [] and "pending" not in rig.state_now()
    assert len(rig.installer_calls) == 1


def _installer_that(rig, then):
    """An installer that does `then` and nothing else."""
    def installer(host, root, env, timeout):
        rig.installer_calls.append({"env": env, "timeout": timeout})
        then()

    rig.monkeypatch.setattr(au, "run_installer", installer)


def test_a_run_that_died_before_the_installer_changed_anything_goes_on(rig):
    _installer_that(rig, _die)
    with pytest.raises(_Killed):
        rig.run()
    rig.monkeypatch.setattr(au, "run_installer", rig._installer)
    assert rig.run(rig.after_slot + LATER) == "updated"
    assert rig.git_calls == [] and "pending" not in rig.state_now()


def test_a_release_unpack_left_half_swapped_is_put_back(rig):
    rig.as_unpack()
    prev = Path(str(rig.root) + ".prev")

    def moved_aside():
        os.rename(rig.root, prev)
        raise _Killed()

    _installer_that(rig, moved_aside)
    with pytest.raises(_Killed):
        rig.run()
    assert not rig.root.exists() and prev.is_dir()
    assert rig.run(rig.after_slot + LATER) == "failed"
    assert (rig.root / ".ar3-release").read_text() == "v0.1.0\n"
    assert not prev.exists() and "pending" not in rig.state_now()


def _bad_release_then(rig, rollback_restart):
    """The new release fails its health check; the rollback's own restart
    answers `rollback_restart`."""
    rig.versions = {"r4t": "0.0.0"}
    inner = rig._run

    def run(argv, **kw):
        if "reset" in argv:
            rig.versions = {}
            rig.restart = rollback_restart
        return inner(argv, **kw)

    rig.monkeypatch.setattr(ar3.subprocess, "run", run)


def test_a_rollback_whose_restart_held_a_busy_seat_is_not_done(rig):
    rig.node("seat-a")
    _bad_release_then(rig, (0, "update: seat-a is busy (a turn is in flight) - left on its current handler; run again\n"))
    assert rig.run() == "rollback-partial"
    assert (rig.root / "VERSION").read_text() == "0.1.0\n"
    state = rig.state_now()
    assert state["pending"]["stage"] == "rollback" and state["bad_release"] == "0.1.1"
    assert "done_night" not in state
    assert "a busy seat is still on 0.1.1" in state["last_result"]["detail"]

    rig.nodes[0]["busy"] = "busy"
    restarts = len(rig.restart_calls)
    assert rig.run(rig.after_slot + LATER) == "skip-busy"
    assert len(rig.restart_calls) == restarts and rig.state_now()["pending"]["stage"] == "rollback"

    rig.nodes[0]["busy"] = "idle"
    rig.restart = (0, "update complete\n")
    assert rig.run(rig.after_slot + 2 * LATER) == "rolled-back"
    state = rig.state_now()
    assert "pending" not in state and state["done_night"] == "2026-11-03"
    assert len(rig.git_calls) == 1 and len(rig.installer_calls) == 1
    assert len(rig.restart_calls) == restarts + 1


def test_status_shows_what_is_pending(rig, capsys):
    rig.node("seat-a")
    _bad_release_then(rig, (3, "boom\n"))
    rig.run()
    assert _main("--auto", "--json") == 0
    assert json.loads(capsys.readouterr().out)["recovery"] == "rollback"
    assert _main("--auto") == 0
    assert "recovery      a rollback is not finished" in capsys.readouterr().out


def test_status_shows_a_pending_restart(rig, capsys):
    rig.node("seat-a")
    rig.restart = (0, "x left on its current handler\n")
    assert rig.run() == "partial"
    assert _main("--auto") == 0
    assert "recovery      a seat is still on the old code" in capsys.readouterr().out


def test_a_pending_rollback_leaves_a_tree_changed_outside_auto_update_alone(rig):
    rig.node("seat-a")
    _bad_release_then(rig, (3, "boom\n"))
    assert rig.run() == "rollback-partial"
    (rig.root / "VERSION").write_text("0.1.2\n")
    rig.git["rev-parse"] = "someone-elses-sha"
    rig.latest = "0.1.2"
    restarts, resets = len(rig.restart_calls), len(rig.git_calls)
    assert rig.run(rig.after_slot + LATER) == "skip-unknown"
    state = rig.state_now()
    assert "not one auto-update made" in state["last_result"]["detail"]
    assert "pending" not in state and "done_night" not in state
    assert len(rig.restart_calls) == restarts and len(rig.git_calls) == resets
    assert (rig.root / "VERSION").read_text() == "0.1.2\n"


def test_a_left_over_record_leaves_a_tree_changed_outside_auto_update_alone(rig):
    _left_over_install(rig)
    (rig.root / "VERSION").write_text("0.1.2\n")
    rig.git["rev-parse"] = "someone-elses-sha"
    assert rig.run(rig.after_slot + LATER) == "skip-unknown"
    state = rig.state_now()
    assert "not one auto-update made" in state["last_result"]["detail"]
    assert "pending" not in state
    assert rig.git_calls == [] and len(rig.installer_calls) == 1 and rig.restart_calls == []
    assert (rig.root / "VERSION").read_text() == "0.1.2\n"


def _update_args():
    return SimpleNamespace(
        auto=None, all_engines=False, engines=None, wait=0.0,
        window=None, tell=None, tell_from=None, now=False, json=False,
    )


def _hand_installer(rig, body, *, head):
    """`ar3 update` runs this as the install's get.sh, for real, with `sh`;
    a run that reaches its end leaves the tree at `head`."""
    (rig.root / "get.sh").write_text(f"#!/bin/sh\n{body}\n")
    inner = ar3.subprocess.run

    def run(argv, **kw):
        if len(argv) == 2 and argv[1] == str(rig.root / "get.sh"):
            done = _REAL_RUN(argv, **kw)
            rig.git["rev-parse"] = head
            return done
        return inner(argv, **kw)

    rig.monkeypatch.setattr(ar3.subprocess, "run", run)


def test_a_manual_update_clears_a_left_over_record_under_the_lock(rig, monkeypatch):
    _left_over_install(rig)
    held = []

    def update(script, wait=None):
        try:
            with au.file_lock(au.lock_path(rig.root), blocking=False):
                held.append(False)
        except BlockingIOError:
            held.append(True)
        return 0

    monkeypatch.setattr(ar3, "_update_suite", update)
    assert ar3.cmd_update(_update_args()) == 0
    assert held == [True]
    assert "pending" not in rig.state_now()
    cleared = [ln for ln in rig.log() if "recovery cleared" in ln]
    assert len(cleared) == 1 and "manual `ar3 update`" in cleared[0]
    assert rig.run(rig.after_slot + LATER) == "noop"
    assert rig.git_calls == [] and (rig.root / "VERSION").read_text() == "0.1.1\n"


@pytest.mark.skipif(os.name != "posix", reason="runs get.sh with sh")
def test_a_manual_update_ends_a_pending_restart_so_no_tick_undoes_it(rig):
    rig.node("seat-a")
    rig.restart = (0, "seat-a left on its current handler\n")
    assert rig.run() == "partial"
    _hand_installer(rig, 'printf "0.1.2\\n" > "$AR3_DIR/VERSION"', head="manualsha")
    assert ar3.cmd_update(_update_args()) == 0
    assert (rig.root / "VERSION").read_text() == "0.1.2\n"
    assert "pending" not in rig.state_now()
    rig.latest, rig.versions = "0.1.2", {"r4t": "0.0.0"}
    restarts = len(rig.restart_calls)
    assert rig.run(rig.after_slot + LATER) == "noop"
    assert rig.git_calls == [] and len(rig.restart_calls) == restarts
    assert (rig.root / "VERSION").read_text() == "0.1.2\n"


@pytest.mark.skipif(os.name != "posix", reason="runs get.sh with sh")
def test_a_manual_update_that_failed_after_the_pull_ends_the_record(rig):
    _left_over_install(rig)
    _hand_installer(rig, 'echo "a8s update failed" >&2; exit 1', head="newsha")
    assert ar3.cmd_update(_update_args()) == 1
    assert "pending" not in rig.state_now()
    assert rig.run(rig.after_slot + LATER) == "noop"
    assert rig.git_calls == [] and (rig.root / "VERSION").read_text() == "0.1.1\n"


def test_get_sh_run_by_hand_ends_the_record(rig, capsys):
    _left_over_install(rig)
    assert _main("--auto", "ensure") == 0
    assert "pending" not in rig.state_now()
    assert any("get.sh ran by hand" in ln for ln in rig.log())
    assert rig.run(rig.after_slot + LATER) == "noop"
    assert rig.git_calls == [] and (rig.root / "VERSION").read_text() == "0.1.1\n"


def test_get_sh_under_a_running_tick_leaves_the_record_to_the_tick(rig):
    _left_over_install(rig)
    with au.file_lock(au.lock_path(rig.root), blocking=False):
        assert _main("--auto", "ensure") == 0
    assert rig.state_now()["pending"]["stage"] == "install"


def test_a_rollback_whose_restart_failed_is_not_done(rig):
    rig.node("seat-a")
    _bad_release_then(rig, (3, "boom\n"))
    assert rig.run() == "rollback-partial"
    state = rig.state_now()
    assert "a8s update exited 3" in state["last_result"]["detail"]
    assert state["pending"]["stage"] == "rollback" and "done_night" not in state
    rig.restart = (0, "update complete\n")
    assert rig.run(rig.after_slot + LATER) == "rolled-back"
    assert "pending" not in rig.state_now()


def test_a_pending_rollback_comes_before_the_network_and_the_bad_release(rig):
    rig.node("seat-a")
    _bad_release_then(rig, (3, "boom\n"))
    rig.run()
    rig.latest = None
    rig.restart = (0, "update complete\n")
    assert rig.run(rig.after_slot + LATER) == "rolled-back"


def test_a_pending_rollback_without_a8s_waits(rig):
    rig.node("seat-a")
    _bad_release_then(rig, (3, "boom\n"))
    rig.run()
    rig.monkeypatch.setattr(ar3, "_a8s", lambda: None)
    restarts = len(rig.restart_calls)
    assert rig.run(rig.after_slot + LATER) == "skip-unknown"
    assert len(rig.restart_calls) == restarts and rig.state_now()["pending"]


def test_no_a8s_to_ask_is_unknown_and_changes_nothing(rig):
    rig.node("seat-a", "busy")
    rig.monkeypatch.setattr(ar3, "_a8s", lambda: None)
    assert rig.run() == "skip-unknown"
    assert rig.installer_calls == [] and rig.restart_calls == []
    assert "done_night" not in rig.state_now()
    assert (rig.root / "VERSION").read_text() == "0.1.0\n"


def test_a_pending_restart_without_a8s_waits(rig):
    rig.node("seat-a")
    rig.restart = (0, "x left on its current handler\n")
    assert rig.run() == "partial"
    rig.monkeypatch.setattr(ar3, "_a8s", lambda: None)
    restarts = len(rig.restart_calls)
    assert rig.run(rig.after_slot + LATER) == "skip-unknown"
    assert len(rig.restart_calls) == restarts and rig.state_now()["pending"]["stage"] == "restart"


# ---------- recovery: the tree is judged by its exact commit ----------

_REAL_GIT_OUT = ar3._git_out


def _real_git(rig):
    """Make the install a real git repository at 0.1.0 with the next release
    committed beside it, and run every git command for real. Returns the two
    commits."""
    import shutil as _shutil

    _shutil.rmtree(rig.root / ".git")
    _git("init", "-q", "-b", "main", cwd=rig.root)
    _git("remote", "add", "origin", "https://github.com/witw-llc/ar3.git", cwd=rig.root)
    _git("add", "-A", cwd=rig.root)
    _git("commit", "-q", "-m", "0.1.0", cwd=rig.root)
    old = _git("rev-parse", "HEAD", cwd=rig.root).stdout.strip()
    _git("checkout", "-q", "-b", "next", cwd=rig.root)
    (rig.root / "VERSION").write_text("0.1.1\n")
    _git("commit", "-q", "-am", "0.1.1", cwd=rig.root)
    new = _git("rev-parse", "HEAD", cwd=rig.root).stdout.strip()
    _git("checkout", "-q", "main", cwd=rig.root)
    rig.remote_head = new
    inner = rig._run

    def run(argv, **kw):
        if Path(argv[0]).name == "git" and "ls-remote" not in argv:
            if "reset" in argv:
                rig.git_calls.append(list(argv))
            return _REAL_RUN(argv, **kw)
        return inner(argv, **kw)

    rig.monkeypatch.setattr(ar3.subprocess, "run", run)
    rig.monkeypatch.setattr(ar3, "_git_out", _REAL_GIT_OUT)
    return old, new


def _head(rig):
    return _git("rev-parse", "HEAD", cwd=rig.root).stdout.strip()


def _pull_then_die(rig, new):
    def installer(host, root, env, timeout):
        rig.installer_calls.append({"env": env, "timeout": timeout})
        _git("reset", "-q", "--hard", new, cwd=root)
        raise _Killed()

    rig.monkeypatch.setattr(au, "run_installer", installer)
    with pytest.raises(_Killed):
        rig.run()
    rig.monkeypatch.setattr(au, "run_installer", rig._installer)


@pytest.mark.skipif(not shutil_which("git"), reason="needs git")
def test_a_commit_a_person_made_at_the_same_version_is_never_reset(rig):
    old, new = _real_git(rig)
    rig.node("seat-a")
    _pull_then_die(rig, new)
    _git("reset", "-q", "--hard", old, cwd=rig.root)
    (rig.root / "notes.txt").write_text("a person's work\n")
    _git("add", "notes.txt", cwd=rig.root)
    _git("commit", "-q", "-m", "work at the same version", cwd=rig.root)
    human = _head(rig)
    assert rig.run(rig.after_slot + LATER) == "skip-unknown"
    assert _head(rig) == human and (rig.root / "notes.txt").read_text() == "a person's work\n"
    assert rig.git_calls == [] and "pending" not in rig.state_now()


@pytest.mark.skipif(not shutil_which("git"), reason="needs git")
def test_a_tracked_file_a_person_changed_after_an_interrupted_update_is_kept(rig):
    old, new = _real_git(rig)
    rig.node("seat-a")
    _pull_then_die(rig, new)
    (rig.root / "get.sh").write_text("#!/bin/sh\n# a person's edit\n")
    assert rig.run(rig.after_slot + LATER) == "skip-unknown"
    assert _head(rig) == new and "a person's edit" in (rig.root / "get.sh").read_text()
    assert rig.git_calls == []


@pytest.mark.skipif(not shutil_which("git"), reason="needs git")
def test_an_interrupted_update_of_a_clean_tree_is_put_back_to_the_exact_commit(rig):
    old, new = _real_git(rig)
    rig.node("seat-a")
    _pull_then_die(rig, new)
    assert rig.run(rig.after_slot + LATER) == "failed"
    assert _head(rig) == old and (rig.root / "VERSION").read_text() == "0.1.0\n"


def test_a_pull_that_lands_on_a_commit_the_run_did_not_record_changes_nothing(rig):
    rig.node("seat-a")
    _mutating_installer(rig, (0, "", False), head="a-newer-push")
    assert rig.run() == "skip-unknown"
    assert rig.restart_calls == [] and rig.git_calls == []
    assert (rig.root / "VERSION").read_text() == "0.1.1\n" and "pending" not in rig.state_now()


def test_a_remote_that_cannot_be_read_installs_nothing(rig):
    rig.ls_remote = (128, "fatal: unable to access the remote\n")
    assert rig.run() == "skip-network"
    assert rig.installer_calls == [] and "pending" not in rig.state_now()
    rig.ls_remote = (128, "fatal: could not read Username: terminal prompts disabled\n")
    assert rig.run(rig.after_slot + LATER) == "skip-input"


def test_the_remote_is_read_with_no_terminal_and_no_prompt(rig):
    rig.run()
    (argv, kw), = rig.ls_remote_calls
    assert argv == ["git", "-C", str(rig.root), "ls-remote", "origin", "HEAD"]
    assert kw["stdin"] is subprocess.DEVNULL and kw["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_a_restart_whose_tree_a_person_replaced_is_left_alone(rig):
    rig.node("seat-a")
    rig.restart = (0, "seat-a left on its current handler\n")
    assert rig.run() == "partial"
    rig.git["rev-parse"] = "a-person-checked-out-this"
    rig.versions = {"r4t": "0.0.0"}
    restarts = len(rig.restart_calls)
    assert rig.run(rig.after_slot + LATER) == "skip-unknown"
    assert len(rig.restart_calls) == restarts and rig.git_calls == []
    assert (rig.root / "VERSION").read_text() == "0.1.1\n" and "pending" not in rig.state_now()


# ---------- recovery: the seats ----------

def test_install_stage_recovery_without_a8s_changes_nothing(rig):
    _left_over_install(rig)
    rig.monkeypatch.setattr(ar3, "_a8s", lambda: None)
    assert rig.run(rig.after_slot + LATER) == "skip-unknown"
    assert rig.git_calls == [] and (rig.root / "VERSION").read_text() == "0.1.1\n"
    assert rig.state_now()["pending"]["stage"] == "install"


def test_install_stage_recovery_waits_for_a_busy_seat(rig):
    rig.node("seat-a")
    _left_over_install(rig)
    rig.nodes[0]["busy"] = "busy"
    assert rig.run(rig.after_slot + LATER) == "skip-busy"
    assert rig.git_calls == [] and (rig.root / "VERSION").read_text() == "0.1.1\n"
    rig.nodes[0]["busy"] = "idle"
    assert rig.run(rig.after_slot + 2 * LATER) == "failed"
    assert (rig.root / "VERSION").read_text() == "0.1.0\n"


def test_a_failed_installer_is_not_put_back_under_a_seat_that_turned_busy(rig):
    rig.node("seat-a")

    def turns_busy():
        rig.nodes[0]["busy"] = "busy"

    _mutating_installer(rig, (1, "boom\n", False), then=turns_busy)
    assert rig.run() == "skip-busy"
    assert rig.git_calls == [] and rig.state_now()["pending"]["stage"] == "install"
    rig.nodes[0]["busy"] = "idle"
    rig.monkeypatch.setattr(au, "run_installer", rig._installer)
    assert rig.run(rig.after_slot + LATER) == "failed"
    assert (rig.root / "VERSION").read_text() == "0.1.0\n" and len(rig.git_calls) == 1


def test_a_release_that_failed_its_check_is_not_rolled_back_under_a_busy_seat(rig):
    rig.node("seat-a")
    rig.versions = {"r4t": "0.0.0"}
    rig.monkeypatch.setattr(
        au, "_restart_nodes", lambda night: (rig.nodes[0].update(busy="busy"), (0, "update complete\n"))[1],
    )
    assert rig.run() == "rollback-partial"
    state = rig.state_now()
    assert state["pending"]["stage"] == "rollback" and state["bad_release"] == "0.1.1"
    assert rig.git_calls == [] and (rig.root / "VERSION").read_text() == "0.1.1\n"
    rig.nodes[0]["busy"] = "idle"
    rig.versions = {}
    rig.monkeypatch.setattr(au, "_restart_nodes", lambda night: (0, "update complete\n"))
    assert rig.run(rig.after_slot + LATER) == "rolled-back"
    assert (rig.root / "VERSION").read_text() == "0.1.0\n"


def test_a_deferred_rollback_still_waits_for_every_seat_that_ran_before(rig):
    rig.node("seat-a")
    _bad_release_then(rig, (3, "restart failed\n"))
    assert rig.run() == "rollback-partial"
    rig.nodes = []
    rig.restart = (0, "updating 0 node(s)\nupdate complete\n")
    outcome = rig.run(rig.after_slot + LATER)
    assert outcome == "rollback-failed"
    assert "seat-a not running" in rig.state_now()["last_result"]["detail"]


def test_a_pending_restart_still_waits_for_every_seat_that_ran_before(rig):
    rig.node("seat-a")
    rig.node("seat-b")
    rig.restart = (0, "seat-b left on its current handler\n")
    assert rig.run() == "partial"
    assert rig.state_now()["pending"]["seats"] == ["seat-a", "seat-b"]
    rig.nodes = [rig.nodes[0]]
    rig.restart = (0, "update complete\n")
    assert rig.run(rig.after_slot + LATER) != "updated"
    state = rig.state_now()
    assert state["bad_release"] == "0.1.1" and "seat-b not running" in state["last_result"]["detail"]


def test_a_pending_restart_needs_no_network(rig):
    rig.node("seat-a")
    rig.restart = (0, "seat-a left on its current handler\n")
    assert rig.run() == "partial"
    rig.latest = None
    rig.restart = (0, "update complete\n")
    assert rig.run(rig.after_slot + LATER) == "updated"


def test_a_pending_restart_is_finished_before_a_newer_release_is_installed(rig):
    rig.node("seat-a")
    rig.restart = (0, "seat-a left on its current handler\n")
    assert rig.run() == "partial"
    rig.latest, rig.remote_head = "0.1.2", "newersha"
    rig.restart = (0, "update complete\n")
    assert rig.run(rig.after_slot + LATER) == "updated"
    result = rig.state_now()["last_result"]
    assert (result["from"], result["to"]) == ("0.1.0", "0.1.1") and len(rig.installer_calls) == 1


# ---------- recovery: a tree moved aside still starts its tick ----------

def test_arming_writes_a_launcher_outside_the_install(rig):
    launcher = au.launcher_path(rig.root)
    assert launcher.is_file() and rig.root not in launcher.parents
    assert STORE[au.install_id(rig.root)]["argv"] == [sys.executable, str(launcher)]


def test_the_launcher_runs_the_copy_left_at_prev_when_the_install_is_gone(rig, monkeypatch):
    prev = Path(str(rig.root) + ".prev")
    os.rename(rig.root, prev)
    started = _launched(au.launcher_path(rig.root), monkeypatch)
    assert started[1] == str(prev / "apps" / "ar3" / "cli.py")
    assert started[2:] == [
        "update", "--auto", "run", "--state-home", str(au.ar3_home().resolve()), "--install", str(rig.root.resolve()),
    ]


def test_ensure_rearms_when_the_launcher_is_gone(rig):
    au.launcher_path(rig.root).unlink()
    assert au.ensure(rig.root) == "armed" and au.launcher_path(rig.root).is_file()


def test_off_removes_the_launcher(rig):
    au.disarm(rig.root)
    assert not au.launcher_path(rig.root).exists()


@pytest.mark.skipif(os.name != "posix", reason="a POSIX a8s stand-in")
def test_a_half_swapped_install_is_put_back_by_a_fresh_scheduled_process(rig, tmp_path, monkeypatch):
    import shutil as _shutil

    repo = Path(__file__).resolve().parents[3]
    _shutil.rmtree(rig.root / "apps")
    _shutil.copytree(repo / "apps" / "ar3", rig.root / "apps" / "ar3", ignore=_shutil.ignore_patterns("tests", "__pycache__"))
    _shutil.copytree(repo / "lib", rig.root / "lib", ignore=_shutil.ignore_patterns("__pycache__"))
    rig.as_unpack()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "a8s").write_text("#!/bin/sh\necho '{\"name\": \"seat-a\", \"pid\": 4242, \"busy\": \"idle\"}'\n")
    (fake_bin / "a8s").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}/usr/bin{os.pathsep}/bin")
    au.arm(rig.root)
    now = datetime.now()
    window = f"{now - timedelta(hours=23):%H:%M}-{now + timedelta(minutes=30):%H:%M}"
    state = rig.state_now()
    state["window"] = window
    au.save(rig.root, state)
    prev = Path(str(rig.root) + ".prev")

    def moved_aside():
        os.rename(rig.root, prev)
        raise _Killed()

    _installer_that(rig, moved_aside)
    with pytest.raises(_Killed):
        rig.run(force=True)
    assert not rig.root.exists() and rig.state_now()["pending"]["stage"] == "install"

    argv = STORE[au.install_id(rig.root)]["argv"]
    done = _REAL_RUN(argv, capture_output=True, text=True, timeout=120,
                     env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)})
    assert done.returncode == 0, done.stderr
    assert (rig.root / ".ar3-release").read_text() == "v0.1.0\n" and not prev.exists()
    state = rig.state_now()
    assert "pending" not in state and state["last_result"]["outcome"] == "failed"
    assert "put back" in state["last_result"]["detail"]


# ---------- recovery: every destructive step re-reads both trees ----------

def _pending(rig, stage, previous, target, **more):
    state = rig.state_now()
    state["pending"] = {
        "stage": stage, "previous": previous, "target": target, "seats": [],
        "from": previous["version"], "to": target["version"], **more,
    }
    if stage == "rollback":
        state["bad_release"] = target["version"]
    au.save(rig.root, state)


def _unpack_installed_beside(rig, prev_tag, prev_version, *, kind="tarball"):
    """A release unpack updated from v0.1.0 to v0.1.1, with `<root>.prev` as
    given: the recorded tree, a different release, a clone, or nothing."""
    import shutil as _shutil

    rig.as_unpack()
    previous = au._tree(au._snapshot(ar3, rig.root))
    prev = Path(str(rig.root) + ".prev")
    if prev_tag is not None:
        _shutil.copytree(rig.root, prev)
        (prev / "VERSION").write_text(f"{prev_version}\n")
        if kind == "git":
            (prev / ".ar3-release").unlink()
            (prev / ".git").mkdir()
        else:
            (prev / ".ar3-release").write_text(f"{prev_tag}\n")
    rig.install_latest()
    return previous, {"kind": "tarball", "ref": "v0.1.1", "version": "0.1.1"}


@pytest.mark.parametrize("stage", ["install", "rollback"])
@pytest.mark.parametrize("prev_tag, prev_version, kind", [
    ("v0.0.9", "0.0.9", "tarball"),
    ("v0.1.0", "0.0.9", "tarball"),
    ("v0.1.0", "0.1.0", "git"),
    (None, None, "tarball"),
])
def test_a_backup_that_is_not_the_recorded_tree_is_never_restored(rig, stage, prev_tag, prev_version, kind):
    rig.node("seat-a")
    previous, target = _unpack_installed_beside(rig, prev_tag, prev_version, kind=kind)
    _pending(rig, stage, previous, target, why="r4t --version did not answer")
    restarts = len(rig.restart_calls)
    assert rig.run() == ("skip-unknown" if stage == "install" else "rollback-blocked")
    assert (rig.root / ".ar3-release").read_text() == "v0.1.1\n" and (rig.root / "VERSION").read_text() == "0.1.1\n"
    assert len(rig.restart_calls) == restarts
    state = rig.state_now()
    assert state["pending"]["stage"] == stage and "nothing is changed" in state["last_result"]["detail"]


@pytest.mark.parametrize("stage", ["install", "rollback"])
def test_a_backup_that_is_the_recorded_tree_is_restored(rig, stage):
    rig.node("seat-a")
    previous, target = _unpack_installed_beside(rig, "v0.1.0", "0.1.0")
    _pending(rig, stage, previous, target, why="r4t --version did not answer")
    assert rig.run() == ("failed" if stage == "install" else "rolled-back")
    assert (rig.root / ".ar3-release").read_text() == "v0.1.0\n" and "pending" not in rig.state_now()


def test_a_commit_the_clone_no_longer_has_is_never_reset_to(rig):
    _left_over_install(rig)
    rig.commits = set()
    assert rig.run(rig.after_slot + LATER) == "skip-unknown"
    assert rig.git_calls == [] and (rig.root / "VERSION").read_text() == "0.1.1\n"
    assert rig.state_now()["pending"]["stage"] == "install"


@pytest.mark.skipif(not shutil_which("git"), reason="needs git")
def test_a_tracked_edit_during_the_health_check_is_never_reset(rig):
    old, new = _real_git(rig)
    rig.node("seat-a")

    def installer(host, root, env, timeout):
        rig.installer_calls.append({"env": env, "timeout": timeout})
        _git("reset", "-q", "--hard", new, cwd=root)
        return 0, "installed", False

    def health(night, target, expected):
        (rig.root / "get.sh").write_text("#!/bin/sh\n# a person's edit\n")
        return False, "r4t --version did not answer"

    rig.monkeypatch.setattr(au, "run_installer", installer)
    rig.monkeypatch.setattr(au, "health", health)
    assert rig.run() == "skip-unknown"
    assert _head(rig) == new and "a person's edit" in (rig.root / "get.sh").read_text()
    assert rig.git_calls == [] and "pending" not in rig.state_now()


@pytest.mark.skipif(not shutil_which("git"), reason="needs git")
def test_a_tracked_edit_while_a_resumed_rollback_asks_the_seats_is_never_reset(rig):
    old, new = _real_git(rig)
    rig.node("seat-a")
    rig.monkeypatch.setattr(au, "_restart_nodes", lambda night: (3, "restart failed\n"))

    def installer(host, root, env, timeout):
        _git("reset", "-q", "--hard", new, cwd=root)
        return 0, "installed", False

    rig.monkeypatch.setattr(au, "run_installer", installer)
    rig.monkeypatch.setattr(au, "health", lambda night, target, expected: (False, "r4t --version did not answer"))
    rig.nodes[0]["busy"] = "idle"
    calls = {"n": 0}
    inner = rig._ps

    def ps(a8s):
        calls["n"] += 1
        if calls["n"] == 2:
            rig.nodes[0]["busy"] = "busy"
        return inner(a8s)

    rig.monkeypatch.setattr(ar3, "_ps_nodes", ps)
    assert rig.run() == "rollback-partial"
    assert _head(rig) == new and rig.state_now()["pending"]["stage"] == "rollback"
    rig.nodes[0]["busy"] = "idle"

    def ps_then_edit(a8s):
        (rig.root / "get.sh").write_text("#!/bin/sh\n# a person's edit\n")
        return inner(a8s)

    rig.monkeypatch.setattr(ar3, "_ps_nodes", ps_then_edit)
    assert rig.run(rig.after_slot + LATER) == "skip-unknown"
    assert _head(rig) == new and "a person's edit" in (rig.root / "get.sh").read_text()
    assert rig.git_calls == []


def test_an_interrupted_manual_update_has_already_ended_the_record(rig, monkeypatch):
    _left_over_install(rig)

    def update(script, wait=None):
        rig.install_latest()
        raise KeyboardInterrupt()

    monkeypatch.setattr(ar3, "_update_suite", update)
    with pytest.raises(KeyboardInterrupt):
        ar3.cmd_update(_update_args())
    assert "pending" not in rig.state_now()
    assert rig.run(rig.after_slot + LATER) == "noop"
    assert rig.git_calls == [] and (rig.root / "VERSION").read_text() == "0.1.1\n"


@pytest.mark.skipif(os.name != "posix", reason="runs get.sh with sh")
def test_a_manual_update_whose_process_dies_has_already_ended_the_record(rig, tmp_path):
    import shutil as _shutil

    repo = Path(__file__).resolve().parents[3]
    _shutil.rmtree(rig.root / "apps")
    _shutil.copytree(repo / "apps" / "ar3", rig.root / "apps" / "ar3", ignore=_shutil.ignore_patterns("tests", "__pycache__"))
    _shutil.copytree(repo / "lib", rig.root / "lib", ignore=_shutil.ignore_patterns("__pycache__"))
    rig.as_unpack()
    _left_over_install(rig)
    (rig.root / "get.sh").write_text('#!/bin/sh\nprintf "0.1.2\\n" > "$AR3_DIR/VERSION"\nkill -9 $PPID\n')
    env = {"PATH": f"/usr/bin{os.pathsep}/bin", "HOME": str(tmp_path), "AR3_HOME": os.environ["AR3_HOME"]}
    done = _REAL_RUN([sys.executable, str(rig.root / "apps" / "ar3" / "cli.py"), "update"],
                     capture_output=True, text=True, timeout=120, env=env)
    assert done.returncode == -9, done.stderr
    assert (rig.root / "VERSION").read_text() == "0.1.2\n"
    assert "pending" not in rig.state_now()


_DESTRUCTIVE = ("rollback", "_restart_nodes", "run_installer")


def test_every_destructive_step_is_reached_only_through_the_guard():
    """Source check: the steps that change the install or restart seats are
    called only inside a lambda handed to `_do`; the reset and the moves of a
    tree live only in the two primitives `rollback` dispatches to."""
    import ast

    tree = ast.parse(Path(au.__file__).read_text(encoding="utf-8"))
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def name(call):
        f = call.func
        return f.id if isinstance(f, ast.Name) else f"{getattr(f.value, 'id', '?')}.{f.attr}" if isinstance(f, ast.Attribute) else ""

    def enclosing_def(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, ast.FunctionDef):
                return node.name
        return None

    def inside_do(node):
        while node in parents:
            parent = parents[node]
            if isinstance(node, ast.Lambda) and isinstance(parent, ast.Call) and name(parent) == "_do":
                return True
            node = parent
        return False

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = name(node)
        if called in _DESTRUCTIVE:
            found.append(called)
            assert inside_do(node), f"{called} at line {node.lineno} does not go through _do"
        if called in ("_git_reset", "_swap_back"):
            assert enclosing_def(node) == "rollback", f"{called} at line {node.lineno} outside rollback"
        if called in ("os.rename", "os.replace", "shutil.rmtree", "shutil.move", "os.remove", "os.rmdir"):
            assert enclosing_def(node) == "_swap_back", f"{called} at line {node.lineno} outside _swap_back"
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value in ("reset", "checkout"):
            assert enclosing_def(node) == "_git_reset", f"git {node.value} at line {node.lineno} outside _git_reset"
    assert sorted(set(found)) == sorted(_DESTRUCTIVE)


def _scenario_update(rig):
    rig.node("seat-a")
    return lambda: rig.run() == "updated"


def _scenario_put_back(rig):
    _mutating_installer(rig, (1, "boom\n", False))
    return lambda: rig.run() == "failed"


def _scenario_rollback(rig):
    rig.node("seat-a")
    _bad_release_then(rig, (0, "update complete\n"))
    return lambda: rig.run() == "rolled-back"


def _scenario_deferred(rig):
    rig.node("seat-a")
    _bad_release_then(rig, (3, "boom\n"))

    def act():
        first = rig.run()
        rig.restart = (0, "update complete\n")
        return (first, rig.run(rig.after_slot + LATER)) == ("rollback-partial", "rolled-back")

    return act


def _scenario_unpack(rig):
    rig.node("seat-a")
    previous, target = _unpack_installed_beside(rig, "v0.1.0", "0.1.0")
    _pending(rig, "rollback", previous, target, why="x")
    return lambda: rig.run() == "rolled-back"


@pytest.mark.parametrize("scenario", [_scenario_update, _scenario_put_back, _scenario_rollback, _scenario_deferred, _scenario_unpack])
def test_each_destructive_step_runs_right_after_its_guard(rig, scenario):
    act = scenario(rig)
    log = []
    real_guard = au._guard

    def guard(night, on_disk, restore):
        stop = real_guard(night, on_disk, restore)
        log.append(("guard", stop is None))
        return stop

    def watched(fn_name):
        real = getattr(au, fn_name)

        def step(*args, **kw):
            assert log and log[-1] == ("guard", True), f"{fn_name} ran without a clearing guard right before it"
            log.append((fn_name, None))
            return real(*args, **kw)

        rig.monkeypatch.setattr(au, fn_name, step)

    rig.monkeypatch.setattr(au, "_guard", guard)
    for fn_name in _DESTRUCTIVE:
        watched(fn_name)
    assert act()
    assert [entry for entry in log if entry[0] != "guard"], "the scenario reached no destructive step"


# ---------- recovery: a rollback that cannot restore is not silent ----------

def _blocked_rollback(rig, prev_tag="v0.0.9", prev_version="0.0.9"):
    rig.node("seat-a")
    previous, target = _unpack_installed_beside(rig, prev_tag, prev_version)
    _pending(rig, "rollback", previous, target, why="r4t --version did not answer")


@pytest.mark.parametrize("prev_tag, reason", [
    ("v0.0.9", ".prev is not the recorded release"),
    (None, ".prev is missing"),
])
def test_a_rollback_whose_restore_is_blocked_says_so(rig, prev_tag, reason):
    _blocked_rollback(rig, prev_tag)
    assert rig.run() == "rollback-blocked"
    result = rig.state_now()["last_result"]
    assert reason in result["detail"] and "0.1.1 failed its health check" in result["detail"]
    assert (result["from"], result["to"]) == ("0.1.0", "0.1.1")


def test_a_rollback_blocked_by_a_missing_commit_says_so(rig):
    rig.node("seat-a")
    rig.install_latest()
    _pending(rig, "rollback", {"kind": "git", "ref": "oldsha", "version": "0.1.0"},
             {"kind": "git", "ref": "newsha", "version": "0.1.1"}, why="r4t --version did not answer")
    rig.commits = set()
    assert rig.run() == "rollback-blocked"
    assert rig.git_calls == []
    assert "the recorded commit is missing" in rig.state_now()["last_result"]["detail"]
    assert (rig.root / "VERSION").read_text() == "0.1.1\n"


def test_an_install_whose_put_back_is_blocked_stays_quiet(told):
    told.node("seat-a")
    previous, target = _unpack_installed_beside(told, "v0.0.9", "0.0.9")
    _pending(told, "install", previous, target)
    assert told.run() == "skip-unknown"
    assert told.tells == []


def test_a_blocked_rollback_is_told_once_a_night(told):
    _blocked_rollback(told)
    assert told.run() == "rollback-blocked"
    (argv, _kw), = told.tells
    assert argv[2].startswith("ar3 auto-update: rollback-blocked 0.1.0 -> 0.1.1: 0.1.1 failed its health check")
    assert ".prev is not the recorded release" in argv[2] and "\n" not in argv[2]
    assert told.run(told.after_slot + LATER) == "rollback-blocked"
    told.nodes[0]["busy"] = "busy"
    assert told.run(told.after_slot + 2 * LATER) == "skip-busy"
    told.nodes[0]["busy"] = "idle"
    assert told.run(told.after_slot + 3 * LATER) == "rollback-blocked"
    assert len(told.tells) == 1
    tomorrow = au.slot_time(told.root, au.parse_window(au.DEFAULT_WINDOW), NIGHT + timedelta(days=1))
    assert told.run(tomorrow + timedelta(minutes=1)) == "rollback-blocked"
    assert len(told.tells) == 2


def test_status_shows_a_blocked_rollback(rig, capsys):
    _blocked_rollback(rig)
    rig.run()
    assert _main("--auto", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["recovery"] == "rollback" and ".prev is not the recorded release" in data["blocked"]
    assert _main("--auto") == 0
    out = capsys.readouterr().out
    assert "recovery      rollback blocked:" in out and "the seats may be on 0.1.1" in out
    line = au.status_line(ar3)
    assert line.startswith("auto-update: rollback blocked (") and ".prev is not the recorded release" in line


def test_the_greeter_and_doctor_show_a_blocked_rollback(rig, monkeypatch, capsys):
    _blocked_rollback(rig)
    rig.run()
    monkeypatch.setattr(ar3, "doctor_results", lambda checks: [])
    ar3.cmd_doctor(SimpleNamespace())
    assert "auto-update: rollback blocked (" in capsys.readouterr().out


# ---------- recovery: the manual handoff fails closed ----------

_PERMS = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0, reason="needs POSIX permission checks as a non-root user",
)


def _manual_watch(rig, monkeypatch):
    """Both manual updaters, replaced by ones that note the call."""
    called = []
    monkeypatch.setattr(ar3, "_update_suite", lambda script, wait=None: called.append("suite") or 0)
    monkeypatch.setattr(ar3, "_update_everything", lambda args, script: called.append("engines") or 0)
    return called


def _engine_args():
    args = _update_args()
    args.engines = ["claude"]
    return args


@_PERMS
@pytest.mark.parametrize("args", [_update_args, _engine_args])
def test_a_manual_update_refuses_when_the_state_cannot_be_read(rig, monkeypatch, capsys, args):
    _left_over_install(rig)
    called = _manual_watch(rig, monkeypatch)
    path = au.state_path(rig.root)
    path.chmod(0)
    try:
        assert ar3.cmd_update(args()) == 1
    finally:
        path.chmod(0o600)
    assert called == []
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and str(path) in err[0] and "fix permissions on" in err[0]
    assert "remove it to discard auto-update recovery" in err[0]
    assert rig.state_now()["pending"]["stage"] == "install"


@_PERMS
@pytest.mark.parametrize("args", [_update_args, _engine_args])
def test_a_manual_update_refuses_when_the_lock_cannot_be_taken(rig, monkeypatch, capsys, args):
    _left_over_install(rig)
    called = _manual_watch(rig, monkeypatch)
    path = au.lock_path(rig.root)
    path.chmod(0)
    try:
        assert ar3.cmd_update(args()) == 1
    finally:
        path.chmod(0o600)
    assert called == []
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "cannot take the auto-update lock" in err[0] and str(path) in err[0]


@_PERMS
@pytest.mark.parametrize("args", [_update_args, _engine_args])
def test_a_manual_update_refuses_when_the_record_cannot_be_ended(rig, monkeypatch, capsys, args):
    _left_over_install(rig)
    called = _manual_watch(rig, monkeypatch)
    directory = au.state_dir()
    directory.chmod(0o555)
    try:
        assert ar3.cmd_update(args()) == 1
    finally:
        directory.chmod(0o755)
    assert called == []
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "cannot write the auto-update state" in err[0] and str(au.state_path(rig.root)) in err[0]
    assert rig.state_now()["pending"]["stage"] == "install"


@pytest.mark.parametrize("args", [_update_args, _engine_args])
def test_a_manual_update_with_no_state_goes_ahead(rig, monkeypatch, capsys, args):
    au.state_path(rig.root).unlink()
    called = _manual_watch(rig, monkeypatch)
    assert ar3.cmd_update(args()) == 0
    assert called == ["suite" if args is _update_args else "engines"]
    assert "ar3 update:" not in capsys.readouterr().err


@_PERMS
def test_a_tick_never_overwrites_a_state_it_cannot_read(rig):
    _left_over_install(rig)
    path = au.state_path(rig.root)
    path.chmod(0)
    try:
        assert rig.run(rig.after_slot + LATER) is None
        assert rig.run(rig.after_slot + LATER, force=True) is None
    finally:
        path.chmod(0o600)
    assert rig.git_calls == [] and rig.state_now()["pending"]["stage"] == "install"


@_PERMS
def test_ar3_and_doctor_say_the_state_cannot_be_read(rig):
    path = au.state_path(rig.root)
    path.chmod(0)
    try:
        line = au.status_line(ar3)
    finally:
        path.chmod(0o600)
    assert line.startswith("auto-update: cannot read the auto-update state") and str(path) in line


def test_a_state_that_is_not_an_object_refuses_the_manual_update(rig, monkeypatch, capsys):
    called = _manual_watch(rig, monkeypatch)
    au.state_path(rig.root).write_text("[]\n")
    assert ar3.cmd_update(_update_args()) == 1
    assert called == [] and "is not a JSON object" in capsys.readouterr().err


# ---------- recovery: get.sh never leaves half a clone ----------

@pytest.mark.skipif(os.name != "posix" or not shutil_which("git"), reason="needs sh and git")
def test_a_conversion_to_a_clone_that_is_killed_leaves_the_release_unpack_in_place(tmp_path, unpack_and_origin):
    root, origin = unpack_and_origin
    real_git = shutil_which("git")
    shim_dir = tmp_path / "killing-git"
    shim_dir.mkdir()
    (shim_dir / "git").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = clone ]; then\n'
        '  for last; do :; done\n'
        '  mkdir -p "$last/.git"\n'
        "  kill -9 $PPID\n"
        "  exit 1\n"
        "fi\n"
        f'exec "{real_git}" "$@"\n'
    )
    (shim_dir / "git").chmod(0o755)
    text = GET_SH.read_text(encoding="utf-8")
    script = tmp_path / "install-git.sh"
    script.write_text(text.rstrip()[: -len('main "$@"')] + "_install_git\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("AR3_")}
    env.update(AR3_DIR=str(root), AR3_REPO=f"file://{origin}", AR3_VERSION="", HOME=str(tmp_path),
               PATH=f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    done = _REAL_RUN(["sh", str(script)], env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode != 0
    assert (root / ".ar3-release").read_text() == "v0.1.0\n" and not (root / ".git").exists()
    assert not Path(str(root) + ".prev").exists()


@pytest.mark.skipif(os.name != "posix" or not shutil_which("git"), reason="needs sh and git")
def test_the_commit_a_conversion_will_clone_is_recorded_before_it_runs(tmp_path, unpack_and_origin, monkeypatch):
    root, origin = unpack_and_origin
    monkeypatch.setattr(ar3.subprocess, "run", _REAL_RUN)
    monkeypatch.setattr(au, "PUBLIC_REPO", f"file://{origin}")
    night = au.Night(ar3, root, {}, "2026-11-03", datetime(2026, 11, 3, 3, 0), {})
    previous = au._tree(au._snapshot(ar3, root))
    target, extra, stop = au._expected(night, previous, "0.1.1", dict(os.environ))
    head = _git("rev-parse", "HEAD", cwd=origin).stdout.strip()
    assert stop is None and extra == {} and target == {"kind": "git", "ref": head}
    pending = {"stage": "install", "previous": previous, "target": target}
    assert _install_git_of_get_sh(tmp_path, root, f"file://{origin}").returncode == 0
    assert au._where(night, pending) == "target"
    _git("-C", str(root), "commit", "-q", "--allow-empty", "-m", "a person's commit")
    assert au._where(night, pending) is None


# ---------- tell ----------

@pytest.fixture
def told(rig, tmp_path, monkeypatch):
    home = tmp_path / "a8s-home"
    home.mkdir()
    monkeypatch.setenv("A8S_HOME", str(home))
    seat_dir = tmp_path / "seat"
    seat_dir.mkdir()
    (home / "a8s.json").write_text(json.dumps({"agents": {"lead": {"root": str(seat_dir)}}, "aliases": {}}))
    state = rig.state_now()
    state["tell"] = {"to": "owner-seat", "from": "lead"}
    state["env"]["A8S_HOME"] = str(home)
    au.save(rig.root, state)
    rig.seat_dir = seat_dir
    return rig


def test_an_update_is_told_from_the_registered_seat(told, monkeypatch):
    monkeypatch.setenv("TELL_OUTBOX_DIR", "/somewhere/else")
    assert told.run() == "updated"
    (argv, kw), = told.tells
    assert argv[:2] == [str(told.root / "tell"), "owner-seat"]
    assert argv[2].startswith("ar3 auto-update: updated 0.1.0 -> 0.1.1")
    assert "\n" not in argv[2]
    assert kw["cwd"] == str(told.seat_dir)
    assert "TELL_OUTBOX_DIR" not in kw["env"]


@pytest.mark.parametrize("setup, outcome", [
    (lambda r: setattr(r, "installer_result", (1, "boom\n", False)), "failed"),
    (lambda r: setattr(r, "installer_result", (None, "", True)), "skip-input"),
])
def test_failures_and_needs_input_are_told(told, setup, outcome):
    setup(told)
    assert told.run() == outcome
    assert len(told.tells) == 1 and outcome in told.tells[0][0][2]


def test_a_rollback_and_a_failed_rollback_are_told(told):
    told.versions = {"r4t": "0.0.0"}
    inner = told._run

    def run(argv, **kw):
        if "reset" in argv:
            told.versions = {}
        return inner(argv, **kw)

    told.monkeypatch.setattr(ar3.subprocess, "run", run)
    assert told.run() == "rolled-back"
    assert "rolled-back" in told.tells[0][0][2]


def test_a_failed_rollback_is_told(told):
    told.versions = {"r4t": "0.0.0"}
    assert told.run() == "rollback-failed"
    assert "rollback-failed" in told.tells[0][0][2]


@pytest.mark.parametrize("quiet", ["noop", "skip-busy", "skip-network", "skip-bad-release", "skip-pinned", "partial"])
def test_routine_outcomes_are_not_told(told, quiet):
    if quiet == "noop":
        told.latest = "0.1.0"
    elif quiet == "skip-busy":
        told.node("seat-a", "busy")
    elif quiet == "skip-network":
        told.latest = None
    elif quiet == "skip-bad-release":
        state = told.state_now()
        state["bad_release"] = "0.1.1"
        au.save(told.root, state)
    elif quiet == "skip-pinned":
        told.git["symbolic-ref"] = None
    else:
        told.node("seat-a")
        told.restart = (0, "left on its current handler\n")
    assert told.run() == quiet
    assert told.tells == []


def test_the_same_outcome_is_told_once_a_night(told):
    told.installer_result = (None, "", True)
    told.run()
    told.run(told.after_slot + timedelta(minutes=15))
    assert len(told.tells) == 1


def test_a_failing_tell_is_logged_and_changes_nothing(told):
    told.tell_result = (1, "no such recipient")
    assert told.run() == "updated"
    assert any("tell failed" in ln for ln in told.log())
    assert told.state_now()["last_result"]["outcome"] == "updated"


def test_a_tell_that_cannot_start_is_logged_and_changes_nothing(told):
    told.tell_error = OSError("no tell here")
    assert told.run() == "updated"
    assert any("tell failed" in ln for ln in told.log())


def test_a_sender_that_is_not_a_registered_node_sends_nothing(told):
    state = told.state_now()
    state["tell"]["from"] = "ghost"
    au.save(told.root, state)
    assert told.run() == "updated"
    assert told.tells == [] and any("not a registered node" in ln for ln in told.log())


def test_no_tell_target_sends_nothing(rig):
    assert rig.run() == "updated"
    assert rig.tells == []


# ---------- the command ----------

def _main(*argv):
    return ar3.main(["update", *argv])


def test_bare_auto_shows_a_fresh_install_as_not_armed(rig, capsys):
    rig.state_now()
    au.state_path(rig.root).unlink()
    assert _main("--auto") == 0
    out = capsys.readouterr().out
    assert out.startswith("auto-update: not armed\n")
    assert "window        02:00-05:00 local" in out
    assert "last check    never" in out and "last result   none yet" in out
    assert "next attempt  none" in out and "tell          nobody" in out


def test_status_json_carries_the_whole_state(rig, capsys):
    rig.run()
    assert _main("--auto", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["state"] == "on" and data["enabled"] is True and data["writable"] is True
    assert data["window"] == "02:00-05:00" and data["install"] == str(rig.root)
    assert data["trigger"] == {"kind": "memory", "installed": True, "error": None}
    assert data["last_result"]["outcome"] == "updated"
    assert data["last_check"]["latest"] == "0.1.1"
    assert data["tell"] is None and data["recovery"] is None
    assert set(data["tonight"]) == {"night", "slot", "done"}
    assert len(data["tonight"]["slot"]) == 5


def test_status_text_names_the_last_result_and_the_tell_target(told, capsys):
    told.run()
    assert _main("--auto") == 0
    out = capsys.readouterr().out
    assert out.startswith("auto-update: on\n")
    assert "trigger       memory: installed" in out
    assert "last result   updated 0.1.0 -> 0.1.1" in out
    assert "tell          owner-seat (from lead)" in out


def test_status_says_when_the_trigger_is_gone(rig, capsys):
    STORE.clear()
    assert _main("--auto", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["state"] == "on, but its trigger is missing" and data["trigger"]["installed"] is False


def test_status_says_when_there_is_no_scheduler(rig, capsys):
    MemoryTrigger.unavailable = True
    assert _main("--auto") == 0
    assert "trigger       unavailable" in capsys.readouterr().out


def test_status_of_a_tree_this_account_cannot_write_is_not_armed(tmp_path, monkeypatch, capsys):
    root = _root(tmp_path)
    monkeypatch.setattr(ar3, "REPO_ROOT", root)
    monkeypatch.setattr(au.os, "access", lambda p, mode: False)
    assert _main("--auto") == 0
    out = capsys.readouterr().out
    assert out.startswith("auto-update: not armed\n") and "not writable by this account" in out
    assert "not armed; this install is not writable" in au.status_line(ar3)


def test_on_arms_with_the_window_and_the_tell_target(tmp_path, monkeypatch, capsys):
    root = _root(tmp_path)
    monkeypatch.setattr(ar3, "REPO_ROOT", root)
    home = tmp_path / "a8s-home"
    home.mkdir()
    monkeypatch.setenv("A8S_HOME", str(home))
    (home / "a8s.json").write_text(json.dumps({"agents": {"lead": {"root": str(tmp_path)}}, "aliases": {}}))
    assert _main("--auto", "on", "--window", "23:00-02:00", "--tell", "owner-seat", "--from", "lead") == 0
    assert "auto-update: on, nightly 23:00-02:00 local" in capsys.readouterr().out
    state = au.load(root)
    assert state["enabled"] is True and state["window"] == "23:00-02:00"
    assert state["tell"] == {"to": "owner-seat", "from": "lead"}
    assert au.install_id(root) in STORE
    assert _main("--auto", "--tell", "none") == 0
    assert "tell" not in au.load(root)


def test_off_removes_the_entry_and_the_run_does_nothing(rig, capsys):
    assert au.install_id(rig.root) in STORE
    assert _main("--auto", "off") == 0
    assert "auto-update: off" in capsys.readouterr().out
    assert STORE == {} and au.load(rig.root)["enabled"] is False
    assert _main("--auto", "run") == 0
    assert rig.latest_calls == 0 and rig.installer_calls == []


def test_on_that_cannot_write_the_entry_says_so(rig, capsys):
    MemoryTrigger.fail_install = True
    assert _main("--auto", "on") == 1
    assert "cannot arm auto-update" in capsys.readouterr().err


def test_ensure_arms_once_and_says_so(tmp_path, monkeypatch, capsys):
    root = _root(tmp_path)
    monkeypatch.setattr(ar3, "REPO_ROOT", root)
    assert _main("--auto", "ensure") == 0
    assert "auto-update: on, nightly 02:00-05:00 local" in capsys.readouterr().out
    assert _main("--auto", "ensure") == 0
    assert capsys.readouterr().out == ""


def test_ensure_never_turns_a_declined_install_back_on(tmp_path, monkeypatch, capsys):
    root = _root(tmp_path)
    monkeypatch.setattr(ar3, "REPO_ROOT", root)
    monkeypatch.setenv("AR3_AUTO_UPDATE", "0")
    assert _main("--auto", "ensure") == 0
    monkeypatch.delenv("AR3_AUTO_UPDATE")
    assert _main("--auto", "ensure") == 0
    assert STORE == {} and capsys.readouterr().out == ""


def test_ensure_with_no_scheduler_exits_zero(tmp_path, monkeypatch, capsys):
    root = _root(tmp_path)
    monkeypatch.setattr(ar3, "REPO_ROOT", root)
    MemoryTrigger.unavailable = True
    assert _main("--auto", "ensure") == 0
    assert "not armed" in capsys.readouterr().err


def test_run_now_prints_what_happened(rig, capsys):
    assert _main("--auto", "run", "--now") == 0
    assert capsys.readouterr().out.startswith("auto-update: updated")


def test_run_prints_nothing_for_the_scheduler(rig, capsys):
    assert _main("--auto", "run") == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("argv", [
    ["--auto", "--engine", "claude"],
    ["--auto", "--all-engines"],
    ["--auto", "on", "--now"],
    ["--auto", "run", "--json"],
    ["--auto", "on", "--state-home", "/somewhere"],
    ["--state-home", "/somewhere"],
    ["--auto", "run", "--window", "01:00-02:00"],
    ["--auto", "off", "--tell", "x", "--from", "y"],
    ["--auto", "--window", "never"],
    ["--auto", "--window", "03:00-03:00"],
    ["--auto", "--tell", "owner-seat"],
    ["--auto", "--from", "lead"],
    ["--auto", "--tell", "owner-seat", "--from", "ghost"],
    ["--window", "01:00-02:00"],
    ["--tell", "x"],
    ["--now"],
    ["--json"],
])
def test_options_that_do_not_belong_together_are_refused(rig, argv, capsys):
    assert _main(*argv) == 2
    assert capsys.readouterr().err.startswith("ar3 update: ")
    assert au.load(rig.root).get("window", au.DEFAULT_WINDOW) == au.DEFAULT_WINDOW


# ---------- arming after a manual update, and the rows ----------

def _manual(rig, monkeypatch, rc=0):
    monkeypatch.setattr(ar3, "_update_suite", lambda script, wait=None: rc)
    return ar3.cmd_update(SimpleNamespace(
        auto=None, all_engines=False, engines=None, wait=0.0,
        window=None, tell=None, tell_from=None, now=False, json=False,
    ))


def test_a_manual_update_arms_an_install_that_has_no_say(rig, monkeypatch, capsys):
    au.state_path(rig.root).unlink()
    STORE.clear()
    assert _manual(rig, monkeypatch) == 0
    assert au.install_id(rig.root) in STORE
    assert "nightly auto-update is on" in capsys.readouterr().out
    assert _manual(rig, monkeypatch) == 0
    assert capsys.readouterr().out == ""


def test_a_manual_update_that_failed_arms_nothing(rig, monkeypatch):
    au.state_path(rig.root).unlink()
    STORE.clear()
    assert _manual(rig, monkeypatch, rc=1) == 1
    assert STORE == {}


def test_a_manual_update_leaves_a_declined_install_alone(rig, monkeypatch, capsys):
    au.disarm(rig.root)
    assert _manual(rig, monkeypatch) == 0
    assert STORE == {} and capsys.readouterr().out == ""


def test_a_manual_update_is_not_failed_by_a_scheduler_that_breaks(rig, monkeypatch, capsys):
    au.state_path(rig.root).unlink()
    STORE.clear()
    monkeypatch.setattr(au, "ensure", lambda root: (_ for _ in ()).throw(RuntimeError("scheduler exploded")))
    assert _manual(rig, monkeypatch) == 0
    assert "not armed: scheduler exploded" in capsys.readouterr().err


def test_the_greeter_says_when_auto_update_is_not_armed(tmp_path, monkeypatch, capsys, homes):
    root = _root(tmp_path)
    monkeypatch.setattr(ar3, "REPO_ROOT", root)
    ar3.cmd_default(None)
    assert "auto-update: not armed (try: ar3 update --auto on)" in capsys.readouterr().out
    au.arm(root)
    ar3.cmd_default(None)
    assert "auto-update: on, nightly 02:00-05:00 local" in capsys.readouterr().out
    au.disarm(root)
    assert au.status_line(ar3).startswith("auto-update: off")


def test_doctor_carries_the_auto_update_line(tmp_path, monkeypatch, capsys):
    root = _root(tmp_path)
    monkeypatch.setattr(ar3, "REPO_ROOT", root)
    monkeypatch.setattr(ar3, "CHECKS", ())
    monkeypatch.setattr(ar3, "update_note", lambda: "pinned")
    ar3.cmd_doctor(None)
    out = capsys.readouterr().out
    assert out.index("suite: pinned") < out.index("auto-update: not armed (try: ar3 update --auto on)")
