"""Per-user node autostart: intent state, idempotent start, native entries.

In-process tests use an in-memory backend. The cron tests run the real backend
against a fake `crontab` program on PATH, and the process tests drive real
`a8s` invocations under a disposable home; nothing here reads or writes the
host's startup entries.
"""
from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

import autostart
import commands
from _memory_backend import MemoryBackend, STORE
from commands import (
    cmd_add,
    cmd_exit,
    cmd_kill,
    cmd_ls,
    cmd_remove,
    cmd_restart,
    cmd_start,
    cmd_stop,
)
from registry import load_registry, save_registry

REPO_ROOT = Path(__file__).resolve().parents[3]
A8S = [sys.executable, str(REPO_ROOT / "apps" / "a8s" / "a8s.py")]


@pytest.fixture(autouse=True)
def memory_store():
    STORE.clear()
    MemoryBackend.fail_install = False
    MemoryBackend.fail_remove = False
    yield STORE
    STORE.clear()
    MemoryBackend.fail_install = False
    MemoryBackend.fail_remove = False


@pytest.fixture
def nodes(fake_home, tmp_path, monkeypatch):
    reg = {}
    for name in ("a1", "b2"):
        root = tmp_path / name
        root.mkdir()
        reg[name] = {"root": str(root)}
    save_registry(reg)

    class FakeProc:
        pid = 4242

    spawned: list[list[str]] = []
    class _Subprocess:
        def __getattr__(self, attr):
            return getattr(subprocess, attr)

        @staticmethod
        def Popen(cmd, **_k):
            spawned.append(cmd)
            return FakeProc()

    monkeypatch.setattr("commands.subprocess", _Subprocess())
    monkeypatch.setattr("commands._warn_unresolvable_harnesses", lambda members: None)
    return spawned


def state(name: str) -> dict:
    return autostart.record(name) or {}


class TestLifecycle:
    def test_start_autostart_arms_and_starts(self, nodes, capsys):
        assert cmd_start(["a1", "--autostart"]) == 0
        rec = state("a1")
        assert rec["preference"] and rec["armed"] and rec["epoch"]
        assert STORE["a1"].epoch == rec["epoch"]
        assert len(nodes) == 1
        assert "armed a1" in capsys.readouterr().out

    def test_repeating_keeps_one_entry_and_epoch(self, nodes):
        cmd_start(["a1", "--autostart"])
        first = state("a1")["epoch"]
        cmd_start(["a1", "--autostart"])
        assert state("a1")["epoch"] == first
        assert list(STORE) == ["a1"]

    def test_plain_start_without_opt_in_changes_nothing(self, nodes):
        assert cmd_start(["a1"]) == 0
        assert not state("a1").get("preference") and not state("a1").get("armed") and not STORE

    def test_stop_disarms_even_when_not_running(self, nodes):
        cmd_start(["a1", "--autostart"])
        assert cmd_stop(["a1"]) == 1  # not running, as before
        rec = state("a1")
        assert rec["preference"] and not rec["armed"] and rec["epoch"] is None
        assert not STORE

    def test_plain_start_rearms_the_remembered_preference_with_a_new_epoch(self, nodes):
        cmd_start(["a1", "--autostart"])
        old = state("a1")["epoch"]
        cmd_stop(["a1"])
        assert cmd_start(["a1"]) == 0
        rec = state("a1")
        assert rec["armed"] and rec["epoch"] != old
        assert STORE["a1"].epoch == rec["epoch"]

    def test_no_autostart_clears_entry_and_preference_then_starts(self, nodes):
        cmd_start(["a1", "--autostart"])
        before = len(nodes)
        assert cmd_start(["a1", "--no-autostart"]) == 0
        assert not state("a1").get("preference") and not STORE
        assert len(nodes) == before + 1
        cmd_start(["a1"])
        assert not state("a1").get("preference") and not state("a1").get("armed")

    def test_contradictory_flags_are_a_usage_error(self, nodes):
        assert cmd_start(["a1", "--autostart", "--no-autostart"]) == 2

    def test_restart_keeps_the_entry_armed(self, nodes, monkeypatch):
        cmd_start(["a1", "--autostart"])
        epoch = state("a1")["epoch"]
        monkeypatch.setattr("commands._read_handler_pid", lambda n: os.getpid() if not STORE.get("gone") else None)
        stopped: list[bool] = []
        monkeypatch.setattr(
            "commands._stop_one", lambda name, force, disarm=True: stopped.append(disarm) or 0
        )
        assert cmd_restart(["a1"]) == 0
        assert stopped == [False]
        assert state("a1")["armed"] and state("a1")["epoch"] == epoch

    def test_restart_of_a_stopped_node_rearms_it(self, nodes):
        cmd_start(["a1", "--autostart"])
        cmd_stop(["a1"])
        assert cmd_restart(["a1"]) == 0
        assert state("a1")["armed"]

    def test_kill_disarms_the_concrete_node(self, nodes):
        cmd_start(["a1", "b2", "--autostart"])
        assert cmd_kill(["a1"]) == 0
        assert not state("a1")["armed"] and state("b2")["armed"]
        assert list(STORE) == ["b2"]

    def test_exit_disarms_every_opted_in_node_including_absent_ones(self, nodes):
        cmd_start(["a1", "b2", "--autostart"])
        assert cmd_exit() == 0
        assert not STORE
        assert state("a1")["preference"] and not state("a1")["armed"]

    def test_alias_opt_in_expands_to_current_members_only(self, nodes):
        from registry import save_aliases

        save_aliases({"both": ["a1", "b2"]})
        cmd_start(["both", "--autostart"])
        assert sorted(STORE) == ["a1", "b2"]
        assert len(nodes) == 1  # one shared handler now
        assert autostart.record("both") is None

    def test_stop_of_a_shared_handler_disarms_the_unnamed_sibling(self, nodes, monkeypatch):
        cmd_start(["a1", "b2", "--autostart"])
        pid = os.getpid()
        monkeypatch.setattr("commands._read_handler_pid", lambda n: pid)
        monkeypatch.setattr("commands._running_nodes_by_pid", lambda: {pid: ["a1", "b2"]})
        monkeypatch.setattr("commands.os.kill", lambda *_: None)
        # The POSIX stop signals; the Windows stop would treat this process,
        # which holds the node here, as a handler that never took its request.
        monkeypatch.setattr("commands.IS_WINDOWS", False)
        monkeypatch.setattr("commands._wait_handlers_stopped", lambda *a, **k: True)
        monkeypatch.setattr("commands.run_stop_hook_if_owed", lambda *a: None)
        assert cmd_stop(["a1"]) == 0
        assert not state("b2")["armed"] and not STORE


class TestAdd:
    def test_add_autostart_arms_without_starting(self, fake_home, tmp_path, capsys, nodes):
        d = tmp_path / "n"
        d.mkdir()
        assert cmd_add(["n", str(d), "--autostart"]) == 0
        assert state("n")["armed"] and "n" in STORE
        assert not nodes
        assert "a8s start n" in capsys.readouterr().out
        assert "AUTOSTART" not in load_registry()["n"].get("vars", {})

    def test_vars_still_parse_beside_the_flag(self, fake_home, tmp_path, nodes):
        d = tmp_path / "n"
        d.mkdir()
        assert cmd_add(["n", str(d), "--autostart", "--topic", "x"]) == 0
        assert load_registry()["n"]["vars"] == {"TOPIC": "x"}

    def test_refused_write_leaves_the_node_registered_but_disarmed(
        self, fake_home, tmp_path, capsys, nodes
    ):
        MemoryBackend.fail_install = True
        d = tmp_path / "n"
        d.mkdir()
        assert cmd_add(["n", str(d), "--autostart"]) == 1
        assert "n" in load_registry()
        assert autostart.record("n") is None
        err = capsys.readouterr().err
        assert "not enabled" in err and "rejected" in err

    def test_refused_write_on_start_does_not_start(self, nodes, capsys):
        MemoryBackend.fail_install = True
        assert cmd_start(["a1", "--autostart"]) == 1
        assert not nodes and autostart.record("a1") is None


class TestRemove:
    def test_remove_deletes_the_entry(self, nodes):
        cmd_start(["a1", "--autostart"])
        assert cmd_remove(["a1"]) == 0
        assert not STORE and autostart.record("a1") is None
        assert "a1" not in load_registry()

    def test_remove_cleans_an_entry_the_record_forgot(self, nodes):
        STORE["a1"] = autostart.Entry("a1", "old", "left behind")
        assert cmd_remove(["a1"]) == 0
        assert not STORE

    def test_cleanup_failure_is_an_error_and_keeps_the_node_for_repair(self, nodes, capsys):
        cmd_start(["a1", "--autostart"])
        MemoryBackend.fail_remove = True
        assert cmd_remove(["a1"]) == 1
        assert "a1" in load_registry()
        rec = state("a1")
        assert not rec["armed"] and rec["epoch"] is None
        assert "stays registered" in capsys.readouterr().err
        MemoryBackend.fail_remove = False
        assert cmd_remove(["a1"]) == 0
        assert not STORE

    def test_stop_with_a_refused_removal_reports_and_still_disarms(self, nodes, capsys):
        cmd_start(["a1", "--autostart"])
        MemoryBackend.fail_remove = True
        assert cmd_stop(["a1"]) == 1
        assert not state("a1")["armed"]
        assert "entry not removed" in capsys.readouterr().err


class TestClaim:
    def test_a_current_epoch_claims(self, nodes):
        cmd_start(["a1", "--autostart"])
        epoch = state("a1")["epoch"]
        with autostart.startup_claim("a1", epoch) as ok:
            assert ok

    def test_an_old_epoch_after_stop_and_start_is_refused(self, nodes):
        cmd_start(["a1", "--autostart"])
        old = state("a1")["epoch"]
        cmd_stop(["a1"])
        with autostart.startup_claim("a1", old) as ok:
            assert not ok
        cmd_start(["a1"])
        with autostart.startup_claim("a1", old) as ok:
            assert not ok

    def test_a_removed_and_recreated_node_refuses_the_old_entry(self, nodes, tmp_path):
        cmd_start(["a1", "--autostart"])
        old = state("a1")["epoch"]
        cmd_remove(["a1"])
        d = tmp_path / "a1"
        assert cmd_add(["a1", str(d), "--autostart"]) == 0
        with autostart.startup_claim("a1", old) as ok:
            assert not ok

    def test_stopped_node_cannot_be_claimed_by_a_startup_entry(self, nodes):
        import daemon

        cmd_start(["a1", "--autostart"])
        epoch = state("a1")["epoch"]
        cmd_stop(["a1"])
        assert daemon.claim_without_takeover("a1", epoch) == "refused"
        assert not daemon.pid_path("a1").exists()

    def test_a_held_node_is_left_alone(self, nodes):
        import daemon

        assert daemon.claim_without_takeover("a1") == "claimed"
        pid_file = daemon.pid_path("a1")
        daemon._unlink_pid_files("a1")
        holder = str(os.getppid())  # a live holder that is not us, on every platform
        pid_file.write_text(holder)
        assert daemon.claim_without_takeover("a1") == "held"
        assert pid_file.read_text() == holder
        assert not daemon.detach_request_path("a1").exists()

    def test_start_of_a_running_node_spawns_nothing(self, nodes, monkeypatch, capsys):
        monkeypatch.setattr("commands._read_handler_pid", lambda n: 99)
        assert cmd_start(["a1"]) == 0
        assert not nodes
        assert "a1: already running (PID 99)" in capsys.readouterr().out

    def test_start_children_never_take_over_and_run_keeps_taking_over(self, nodes, monkeypatch):
        cmd_start(["a1"])
        assert "--no-takeover" in nodes[0]
        seen = {}
        monkeypatch.setattr(
            "commands.attached_loop", lambda members, interval, **kw: seen.update(kw) or 0
        )
        from commands import cmd_run

        assert cmd_run(["a1"], 1.0) == 0
        assert seen["takeover"] is True
        assert cmd_run(["--no-takeover", "a1"], 1.0) == 0
        assert seen["takeover"] is False
        monkeypatch.setenv("A8S_HOME", str(autostart.home_dir()))
        assert cmd_run(["--startup", "e", "--startup-home", str(autostart.home_dir()), "a1"], 1.0) == 0
        assert seen["startup_epoch"] == "e" and seen["takeover"] is False


class TestListing:
    def test_plain_ls_is_unchanged(self, nodes, capsys):
        cmd_start(["a1", "--autostart"])
        cmd_ls([])
        assert "AUTOSTART" not in capsys.readouterr().out

    def test_autostart_view_reports_ok_missing_mismatched_and_orphaned(self, nodes, capsys):
        cmd_start(["a1", "b2", "--autostart"])
        STORE.pop("a1")
        STORE["b2"] = autostart.Entry("b2", "other", "")
        STORE["ghost"] = autostart.Entry("ghost", "x", "")
        cmd_ls(["--autostart"])
        out = capsys.readouterr().out
        assert "AUTOSTART" in out and "test event" in out
        lines = {ln.split()[0]: ln for ln in out.splitlines()[1:]}
        assert "missing" in lines["a1"]
        assert "mismatched" in lines["b2"]
        assert "orphaned" in lines["ghost"]


class TestFlagsAndEnv:
    def test_startup_env_keeps_proxy_and_ca_out_of_the_entry(self, nodes, monkeypatch, fake_home):
        monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@proxy:3128")
        monkeypatch.setenv("SSL_CERT_FILE", "/etc/ca.pem")
        cmd_start(["a1", "--autostart"])
        assert "secret" not in STORE["a1"].detail
        saved = autostart.env_path()
        assert saved.is_file()
        if os.name == "posix":
            assert stat.S_IMODE(saved.stat().st_mode) == 0o600
        monkeypatch.delenv("HTTPS_PROXY")
        monkeypatch.delenv("SSL_CERT_FILE")
        autostart.load_startup_env("a1")
        assert os.environ["HTTPS_PROXY"] == "http://user:secret@proxy:3128"
        assert os.environ["SSL_CERT_FILE"] == "/etc/ca.pem"

    def test_load_never_overrides_the_environment(self, nodes, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://a")
        cmd_start(["a1", "--autostart"])
        monkeypatch.setenv("HTTPS_PROXY", "http://b")
        autostart.load_startup_env("a1")
        assert os.environ["HTTPS_PROXY"] == "http://b"

    def test_lifecycle_flag_parser(self):
        assert autostart.split_lifecycle_flags(["a", "--startup", "E", "--startup-home", "H"]) == (
            ["a"], None, "E", "H",
        )
        with pytest.raises(ValueError):
            autostart.split_lifecycle_flags(["--startup"])


# ---------- native backends ----------

@pytest.fixture
def spec(fake_home):
    return autostart.EntrySpec(
        node="n1", epoch="EPOCH", home=str(autostart.home_dir()),
        python="/opt/my python/bin/python3", entrypoint="/srv/a 8s/a8s.py", foreground=False,
    )


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

    def read(self) -> str:
        return self.store.read_text() if self.store.exists() else ""


@pytest.fixture
def crontab(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("fake crontab is a bash script")
    return FakeCrontab(tmp_path, monkeypatch)


class TestCronBackend:
    def test_install_preserves_unrelated_lines_byte_for_byte(self, crontab, spec):
        original = "# mine\n\n0 3 * * * /usr/bin/backup --now   # nightly\n@reboot echo hi%there\n"
        crontab.store.write_text(original)
        backend = autostart.CronBackend()
        backend.install(spec)
        assert crontab.read().startswith(original)
        owned = crontab.read()[len(original):]
        assert owned.startswith("@reboot ") and "a8s-autostart" in owned
        backend.remove("n1")
        assert crontab.read() == original

    def test_quoting_spaces_and_percent(self, crontab, spec):
        import dataclasses

        spec = dataclasses.replace(spec, node="n1")
        autostart.CronBackend().install(spec)
        line = crontab.read().splitlines()[0]
        assert "'/opt/my python/bin/python3'" in line
        assert "%" not in line.replace("\\%", "")
        pct = dataclasses.replace(spec, entrypoint="/srv/100%/a8s.py")
        autostart.CronBackend().install(pct)
        assert "/srv/100\\%/a8s.py" in crontab.read()

    def test_reinstall_replaces_instead_of_duplicating(self, crontab, spec):
        import dataclasses

        b = autostart.CronBackend()
        b.install(spec)
        b.install(dataclasses.replace(spec, epoch="E2"))
        assert crontab.read().count("a8s-autostart") == 1
        assert b.entries()["n1"].epoch == "E2"

    def test_removing_the_last_entry_of_an_empty_crontab_leaves_none(self, crontab, spec):
        b = autostart.CronBackend()
        b.install(spec)
        b.remove("n1")
        assert not crontab.store.exists()

    def test_two_config_homes_keep_separate_entries(self, crontab, spec, tmp_path, monkeypatch):
        b = autostart.CronBackend()
        b.install(spec)
        other = tmp_path / "other-home"
        monkeypatch.setenv("A8S_HOME", str(other))
        assert "n1" not in b.entries()
        b.install(autostart.EntrySpec("n1", "E9", str(other), "p", "e", False))
        assert crontab.read().count("a8s-autostart") == 2
        b.remove("n1")
        monkeypatch.delenv("A8S_HOME")
        assert "n1" in autostart.CronBackend().entries() or crontab.read().count("a8s-autostart") == 1

    def test_a_rejected_write_is_an_error(self, crontab, spec):
        crontab.reject.write_text("")
        with pytest.raises(autostart.AutostartError, match="failed|rejected"):
            autostart.CronBackend().install(spec)

    def test_an_absent_crontab_program_is_a_clear_error(self, spec, monkeypatch):
        monkeypatch.setenv("PATH", "/nonexistent")
        with pytest.raises(autostart.AutostartError, match="needs cron"):
            autostart.CronBackend().install(spec)

    def test_a_prior_copy_is_kept(self, crontab, spec):
        crontab.store.write_text("0 0 * * * x\n")
        autostart.CronBackend().install(spec)
        assert (autostart.home_dir() / "autostart-crontab.bak").read_text() == "0 0 * * * x\n"

    def test_enable_through_the_real_backend_reports_a_refusal(
        self, crontab, nodes, monkeypatch, capsys
    ):
        monkeypatch.setattr(autostart, "select_backend", lambda: autostart.CronBackend())
        crontab.reject.write_text("")
        assert cmd_start(["a1", "--autostart"]) == 1
        assert "not enabled" in capsys.readouterr().err
        assert autostart.record("a1") is None


class TestLaunchdBackend:
    def test_plist_runs_at_load_without_keepalive(self, spec, tmp_path, monkeypatch):
        import dataclasses

        monkeypatch.setattr(autostart, "_run_quiet", lambda argv: None)
        b = autostart.LaunchdBackend(tmp_path / "LaunchAgents")
        fg = dataclasses.replace(spec, foreground=True)
        b.install(fg)
        plist = plistlib.loads(b.path("n1").read_bytes())
        assert plist["RunAtLoad"] is True and "KeepAlive" not in plist
        assert plist["ProgramArguments"][2:4] == ["run", "--no-takeover"]
        assert b.entries()["n1"].epoch == "EPOCH"
        b.remove("n1")
        assert not b.entries() and not b.path("n1").exists()


class TestSchtasksBackend:
    def test_task_xml_is_a_logon_task_with_no_stored_password(self, spec, monkeypatch):
        import dataclasses

        monkeypatch.setattr(autostart.SchtasksBackend, "_user_sid", lambda self: "S-1-5-21-1-2-3-1001")
        xml = autostart.SchtasksBackend()._xml(dataclasses.replace(spec, foreground=True))
        assert "<LogonTrigger>" in xml and "InteractiveToken" in xml
        assert "LeastPrivilege" in xml and "<ExecutionTimeLimit>PT0S" in xml
        assert "Password" not in xml and "SYSTEM" not in xml
        assert "--no-takeover" in xml and "--startup EPOCH" in xml


# ---------- real processes ----------

class Rig:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.crontab = FakeCrontab(tmp_path, monkeypatch)
        self.env = {
            **os.environ,
            "A8S_HOME": str(self.home),
            "HOME": str(tmp_path),
            "PATH": os.environ["PATH"],
            "A8S_TEST_LOCK_ROOT": str(tmp_path / "account-locks"),
        }
        (tmp_path / "account-locks").mkdir()
        self.cmd = [sys.executable, str(Path(__file__).resolve().parent / "_a8s_isolated.py")]
        self.env.pop("TELL_OUTBOX_DIR", None)
        root = tmp_path / "node"
        root.mkdir()
        self.a8s("config", "set", "loop_interval", "0.2")
        assert self.a8s("add", "n1", str(root)).returncode == 0

    def a8s(self, *args: str, env=None, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*self.cmd, *args], env=env or self.env, capture_output=True, text=True, timeout=timeout
        )

    def pid_file(self) -> Path:
        return self.home / "agents" / "n1" / "pid"

    def wait_attached(self, seconds: float = 20) -> int:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.pid_file().is_file() and self.pid_file().read_text().strip():
                return int(self.pid_file().read_text())
            time.sleep(0.05)
        pytest.fail("node never attached")

    def cron_command(self) -> str:
        line = next(ln for ln in self.crontab.read().splitlines() if "a8s-autostart" in ln)
        return line[len("@reboot "):line.index(" # a8s-autostart")].replace("\\%", "%")

    def startup_event(self) -> subprocess.CompletedProcess:
        """What cron does at boot: the entry's command under a thin environment."""
        return subprocess.run(
            self.cron_command(), shell=True, capture_output=True, text=True, timeout=60,
            env={"HOME": str(self.tmp), "PATH": "/usr/bin:/bin"},
        )


@pytest.fixture
def rig(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("process rig needs POSIX signals")
    r = Rig(tmp_path, monkeypatch)
    yield r
    r.a8s("exit")
    pidf = r.pid_file()
    if pidf.is_file():
        try:
            os.kill(int(pidf.read_text()), 9)
        except (OSError, ValueError):
            pass


class TestProcesses:
    def test_boot_event_starts_the_node_under_a_thin_environment(self, rig):
        rc = rig.a8s("start", "n1", "--autostart")
        assert rc.returncode == 0, rc.stderr
        first = rig.wait_attached()
        assert rig.a8s("stop", "n1").returncode == 0
        assert "a8s-autostart" not in rig.crontab.read()
        assert rig.a8s("start", "n1", "--autostart").returncode == 0
        rig.wait_attached()
        assert rig.a8s("stop", "n1", "--force").returncode == 0
        assert first

    def test_kill_disarms_so_the_entry_command_refuses(self, rig):
        assert rig.a8s("start", "n1", "--autostart").returncode == 0
        rig.wait_attached()
        cmd = rig.cron_command()
        assert rig.a8s("kill", "n1").returncode == 0
        assert not rig.pid_file().is_file()
        subprocess.run(
            cmd, shell=True, timeout=60, env={"HOME": str(rig.tmp), "PATH": "/usr/bin:/bin"}
        )
        time.sleep(1.5)
        assert not rig.pid_file().is_file()

    def test_a_stop_survives_the_next_startup_event(self, rig):
        assert rig.a8s("start", "n1", "--autostart").returncode == 0
        rig.wait_attached()
        stale_cmd = rig.cron_command()
        assert rig.a8s("stop", "n1").returncode == 0
        assert not rig.pid_file().is_file()
        subprocess.run(
            stale_cmd, shell=True, timeout=60,
            env={"HOME": str(rig.tmp), "PATH": "/usr/bin:/bin"},
        )
        time.sleep(1.5)
        assert not rig.pid_file().is_file()

    def test_the_boot_command_starts_a_node_after_the_host_lost_its_processes(self, rig):
        assert rig.a8s("start", "n1", "--autostart").returncode == 0
        pid = rig.wait_attached()
        os.kill(pid, 9)  # a host shutdown leaves the entry and the stale pid file
        time.sleep(0.3)
        done = rig.startup_event()
        assert done.returncode == 0, done.stderr
        deadline = time.time() + 20
        new = None
        while time.time() < deadline:
            try:
                new = int(rig.pid_file().read_text())
            except (OSError, ValueError):
                new = None
            if new and new != pid:
                break
            time.sleep(0.1)
        assert new and new != pid
        assert rig.a8s("ls", "--autostart").stdout.count("ok") == 1

    def test_simultaneous_startup_and_manual_starts_leave_exactly_one_handler(self, rig):
        thin = {"HOME": str(rig.tmp), "PATH": "/usr/bin:/bin"}
        assert rig.a8s("start", "n1", "--autostart").returncode == 0
        rig.a8s("stop", "n1")
        assert rig.a8s("start", "n1", "--autostart").returncode == 0
        procs = [
            subprocess.Popen(rig.cron_command(), shell=True, env=thin),
            subprocess.Popen([*rig.cmd, "start", "n1"], env=rig.env),
            subprocess.Popen([*rig.cmd, "start", "n1"], env=rig.env),
        ]
        for p in procs:
            p.wait(timeout=60)
        first = rig.wait_attached()
        time.sleep(2.0)
        assert int(rig.pid_file().read_text()) == first
        handlers = subprocess.run(
            ["pgrep", "-f", "a8s.py run .*--no-takeover.* n1"], capture_output=True, text=True
        ).stdout.split()
        mine = [p for p in handlers if Path(f"/proc/{p}/environ").exists()
                and f"A8S_HOME={rig.home}".encode() in Path(f"/proc/{p}/environ").read_bytes()]
        assert mine == [str(first)]

    def test_start_on_a_running_node_does_not_restart_it(self, rig):
        assert rig.a8s("start", "n1").returncode == 0
        pid = rig.wait_attached()
        out = rig.a8s("start", "n1")
        assert out.returncode == 0 and "already running" in out.stdout
        time.sleep(0.5)
        assert int(rig.pid_file().read_text()) == pid
        os.kill(pid, 0)

    def test_restart_replaces_the_process(self, rig):
        assert rig.a8s("start", "n1").returncode == 0
        pid = rig.wait_attached()
        assert rig.a8s("restart", "n1").returncode == 0
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                if int(rig.pid_file().read_text()) != pid:
                    return
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        pytest.fail("restart did not replace the handler")

    def test_public_run_takes_over_a_running_node(self, rig):
        assert rig.a8s("start", "n1").returncode == 0
        pid = rig.wait_attached()
        runner = subprocess.Popen([*rig.cmd, "run", "n1"], env=rig.env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    if int(rig.pid_file().read_text()) == runner.pid:
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(0.1)
            else:
                pytest.fail("run did not take over")
        finally:
            runner.terminate()
            runner.wait(timeout=30)
        assert pid != runner.pid

    def test_remove_cleans_the_cron_entry(self, rig):
        assert rig.a8s("start", "n1", "--autostart").returncode == 0
        rig.wait_attached()
        assert rig.a8s("stop", "n1").returncode == 0
        assert rig.a8s("start", "n1", "--autostart").returncode == 0
        rig.wait_attached()
        assert rig.a8s("stop", "n1").returncode == 0
        assert rig.a8s("remove", "n1").returncode == 0
        assert "a8s-autostart" not in rig.crontab.read()


class TestReviewRegressions:
    def test_pid_publication_is_atomic_under_a_forced_create_write_ordering(self, fake_home, monkeypatch):
        import threading

        import daemon

        created, resume = threading.Event(), threading.Event()
        real_write, first = os.write, {}

        pid_bytes = str(os.getpid()).encode()

        def paused(fd, data):
            # Only the pid write: on Windows `file_lock` first writes a byte
            # to give the lock a region, before the claim is in flight.
            if threading.get_ident() == first.get("id") and data == pid_bytes:
                created.set()
                assert resume.wait(5)
            return real_write(fd, data)

        result = {}

        def contender():
            first["id"] = threading.get_ident()
            result["first"] = daemon._try_atomic_claim("probe", os.getpid())

        def reader():
            result["reader"] = daemon._read_handler_pid("probe")
            result["second"] = daemon._try_atomic_claim("probe", os.getpid())

        monkeypatch.setattr(os, "write", paused)
        worker = threading.Thread(target=contender)
        worker.start()
        assert created.wait(5)
        rival = threading.Thread(target=reader)
        rival.start()
        rival.join(0.5)
        assert rival.is_alive(), "a contender must wait for the in-flight claim"
        resume.set()
        worker.join(5)
        rival.join(5)
        assert result["first"] is True and result["second"] is False
        assert result["reader"] == os.getpid()
        assert daemon._read_handler_pid("probe") == os.getpid()

    def test_ls_autostart_rejects_unknown_options(self, nodes, capsys):
        assert cmd_ls(["--autostart", "--bogus"]) == 2
        assert "unrecognized arguments: --bogus" in capsys.readouterr().err

    def test_ls_autostart_q_lists_names(self, nodes, capsys):
        assert cmd_ls(["--autostart", "-q"]) == 0
        assert capsys.readouterr().out.split() == ["a1", "b2"]

    def test_modified_command_is_a_mismatch_not_ok(self, nodes, capsys):
        cmd_start(["a1", "--autostart"])
        capsys.readouterr()
        STORE["a1"] = autostart.Entry("a1", state("a1")["epoch"], "x", ("/bin/evil", "--startup", state("a1")["epoch"]))
        cmd_ls(["--autostart"])
        assert "mismatched" in capsys.readouterr().out

    def test_remove_fails_when_inspection_fails_without_a_record(self, nodes, monkeypatch, capsys):
        def broken():
            raise autostart.AutostartError("crontab -l failed")

        class Bad(MemoryBackend):
            def entries(self):
                broken()

        monkeypatch.setattr(autostart, "select_backend", lambda: Bad())
        assert cmd_remove(["a1"]) == 1
        assert "a1" in load_registry()

    def test_remove_with_no_facility_and_no_record_succeeds(self, nodes, monkeypatch):
        def none():
            raise autostart.AutostartUnavailable("no cron")

        monkeypatch.setattr(autostart, "select_backend", none)
        assert cmd_remove(["a1"]) == 0

    def test_malformed_cron_lines_report_instead_of_raising(self, crontab, spec):
        hid = autostart.home_id()
        crontab.store.write_text(
            f"@reboot '/unterminated # a8s-autostart home={hid} node=n1\n"
            f"@reboot /x --startup # a8s-autostart home={hid} node=n2\n"
        )
        found = autostart.CronBackend().entries()
        assert found["n1"].epoch is None and found["n2"].epoch is None

    def test_malformed_plist_types_report_instead_of_raising(self, tmp_path, fake_home):
        import plistlib as pl

        b = autostart.LaunchdBackend(tmp_path / "LA")
        b.agents_dir.mkdir()
        b.path("n1").write_bytes(pl.dumps({"ProgramArguments": "nope"}))
        b.path("n2").write_bytes(pl.dumps(["x"]))
        found = b.entries()
        assert found["n1"].epoch is None and found["n2"].epoch is None

    def test_disabling_launchd_does_not_unload_the_running_job(self, tmp_path, spec, monkeypatch):
        import dataclasses

        calls = []
        monkeypatch.setattr(autostart, "_run", lambda argv, **k: calls.append(argv))
        b = autostart.LaunchdBackend(tmp_path / "LA")
        b.install(dataclasses.replace(spec, foreground=True))
        b.remove("n1")
        assert calls == [] and not b.path("n1").exists()


class TestPidLockDiscipline:
    def test_stale_reader_cannot_delete_a_newer_owner(self, fake_home, monkeypatch):
        import threading

        import daemon

        path = daemon.pid_path("probe")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("99999999")
        real = daemon._unlink_pid_files_locked
        paused, resume, ident = threading.Event(), threading.Event(), {}

        def delayed(name):
            if threading.get_ident() == ident.get("id"):
                paused.set()
                resume.wait(1.0)
            real(name)

        monkeypatch.setattr(daemon, "_unlink_pid_files_locked", delayed)

        def first():
            ident["id"] = threading.get_ident()
            daemon._read_handler_pid("probe")

        worker = threading.Thread(target=first)
        worker.start()
        assert paused.wait(5)
        results = {}
        rival = threading.Thread(
            target=lambda: results.update(a=daemon._try_atomic_claim("probe", os.getpid()))
        )
        rival.start()
        resume.set()
        worker.join(5)
        rival.join(5)
        results["b"] = daemon._try_atomic_claim("probe", os.getpid())
        assert results["a"] is True and results["b"] is False
        assert daemon._read_handler_pid("probe") == os.getpid()

    def test_release_and_clear_hold_the_lock(self, fake_home):
        import daemon
        from commands import _clear_dead_handler_files

        assert daemon._try_atomic_claim("probe", os.getpid())
        _clear_dead_handler_files("probe")
        assert daemon._read_handler_pid("probe") == os.getpid()
        daemon.release("probe")
        assert daemon._read_handler_pid("probe") is None

    def test_final_purge_unloads_a_loaded_launchd_job_but_disable_does_not(self, tmp_path, spec, monkeypatch):
        import dataclasses

        calls = []
        loaded = {"v": True}

        def run(argv, **k):
            calls.append(argv[1])
            if argv[1] == "bootout":
                loaded["v"] = False
            return subprocess.CompletedProcess(argv, 0 if loaded["v"] else 1, "", "")

        monkeypatch.setattr(autostart, "_run", run)
        # launchctl's domain names the uid; Windows has none to read.
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
        b = autostart.LaunchdBackend(tmp_path / "LA")
        b.install(dataclasses.replace(spec, foreground=True))
        b.remove("n1")
        assert "bootout" not in calls
        b.install(dataclasses.replace(spec, foreground=True))
        b.remove("n1", unload=True)
        assert "bootout" in calls and not loaded["v"] and not b.path("n1").exists()


class TestReviewRound3:
    @pytest.mark.skipif(os.name != "posix", reason="POSIX account ownership uses pwd")
    def test_account_lock_root_ignores_caller_environment(self, tmp_path, monkeypatch):
        import pwd
        from types import SimpleNamespace

        account_home = tmp_path / "account"
        account_home.mkdir()
        monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(account_home)))
        roots = set()
        for n in range(3):
            monkeypatch.setenv("TMPDIR", str(tmp_path / f"tmp{n}"))
            monkeypatch.setenv("HOME", str(tmp_path / f"home{n}"))
            monkeypatch.setenv("LOGNAME", f"someone{n}")
            monkeypatch.setenv("A8S_HOME", str(tmp_path / f"cfg{n}"))
            monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / f"xdg{n}"))
            roots.add(autostart._account_lock_root())
        assert roots == {account_home / ".a8s-locks"}
        assert stat.S_IMODE(os.stat(account_home / ".a8s-locks").st_mode) == 0o700

    @pytest.mark.skipif(os.name != "posix", reason="POSIX account ownership uses pwd")
    def test_account_lock_root_refuses_a_shared_directory(self, tmp_path, monkeypatch):
        import pwd
        from types import SimpleNamespace

        (tmp_path / ".a8s-locks").mkdir(mode=0o755)
        os.chmod(tmp_path / ".a8s-locks", 0o755)
        monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))
        with pytest.raises(autostart.AutostartError):
            autostart._account_lock_root()

    def test_two_config_homes_serialize_on_the_same_cron_lock(self, fake_home):
        a = autostart.account_lock_root() / "crontab.lock"
        assert a == autostart.account_lock_root() / "crontab.lock"

    def test_reader_of_a_missing_node_creates_nothing(self, fake_home):
        import daemon

        assert daemon._read_handler_pid("ghost") is None
        assert not daemon.pid_path("ghost").parent.exists()

    def test_reader_in_an_empty_node_directory_returns_none(self, fake_home):
        import daemon

        daemon.pid_path("empty").parent.mkdir(parents=True)
        assert daemon._read_handler_pid("empty") is None

    @pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs permission checks")
    def test_reader_in_a_read_only_node_directory_does_not_hang_or_reap(self, fake_home):
        import daemon

        d = daemon.pid_path("ro").parent
        d.mkdir(parents=True)
        daemon.pid_path("ro").write_text(str(os.getpid()))
        os.chmod(d, 0o500)
        try:
            assert daemon._read_handler_pid("ro") == os.getpid()
        finally:
            os.chmod(d, 0o700)

    @pytest.mark.skipif(not hasattr(__import__("signal"), "pthread_sigmask"), reason="POSIX")
    def test_claim_failure_releases_acquired_nodes_and_restores_the_signal_mask(self, nodes, monkeypatch):
        import signal

        import daemon

        calls = []

        def claim(name, epoch=None, manual=None):
            calls.append(name)
            if len(calls) == 2:
                raise RuntimeError("forced claim failure")
            assert daemon._try_atomic_claim(name, os.getpid())
            return "claimed"

        monkeypatch.setattr(daemon, "claim_without_takeover", claim)
        before = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        with pytest.raises(RuntimeError):
            daemon.attached_loop(["a1", "b2"], 0.1, takeover=False)
        assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == before
        assert daemon._read_handler_pid("a1") is None

    @pytest.mark.skipif(not hasattr(__import__("signal"), "pthread_sigmask"), reason="POSIX")
    def test_handler_installation_failure_releases_and_restores(self, nodes, monkeypatch):
        import signal

        import daemon

        monkeypatch.setattr(daemon, "_make_signal_handler", lambda *a: (_ for _ in ()).throw(RuntimeError("forced")))
        before = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        with pytest.raises(RuntimeError):
            daemon.attached_loop(["a1"], 0.1, takeover=False)
        assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == before
        assert daemon._read_handler_pid("a1") is None


class TestQueuedManualStart:
    """A detached start child that has not attached yet obeys later intent."""

    @pytest.fixture
    def held(self, rig, monkeypatch):
        barrier = rig.tmp / "barrier"
        barrier.mkdir()
        monkeypatch.setenv("A8S_HOME", str(rig.home))
        monkeypatch.setenv("A8S_TEST_BARRIER", str(barrier))
        monkeypatch.setattr("commands.ENTRYPOINT", Path(__file__).resolve().parent / "_a8s_barrier.py")
        monkeypatch.setattr("commands._warn_unresolvable_harnesses", lambda members: None)
        children: list[int] = []
        exits: dict[int, int] = {}

        def reap(pid: int) -> None:
            try:
                _, status = os.waitpid(pid, 0)
                exits[pid] = os.waitstatus_to_exitcode(status)
            except ChildProcessError:
                exits[pid] = -1

        def start() -> int:
            import io
            from contextlib import redirect_stdout

            out = io.StringIO()
            with redirect_stdout(out):
                assert cmd_start(["n1"]) == 0
            pid = int(out.getvalue().split("PID")[1].split()[0])
            children.append(pid)
            import threading

            threading.Thread(target=reap, args=(pid,), daemon=True).start()
            deadline = time.time() + 20
            while not (barrier / f"ready.{pid}").exists():
                assert time.time() < deadline, "child never reached the barrier"
                time.sleep(0.02)
            return pid

        def release_and_wait(pid: int) -> int:
            (barrier / "go").write_text("")
            deadline = time.time() + 20
            while time.time() < deadline:
                if pid in exits:
                    return exits[pid]
                time.sleep(0.05)
            pytest.fail("child did not exit")

        yield rig, start, release_and_wait
        (barrier / "go").write_text("")
        for pid in children:
            try:
                os.kill(pid, 15)
            except OSError:
                pass

    def test_stop_after_start_cancels_the_queued_child(self, held):
        rig, start, release = held
        pid = start()
        rig.a8s("stop", "n1")
        assert release(pid) == 0
        assert not rig.pid_file().exists()

    def test_kill_after_start_cancels_the_queued_child(self, held):
        rig, start, release = held
        pid = start()
        assert rig.a8s("kill", "n1").returncode == 0
        assert release(pid) == 0
        assert not rig.pid_file().exists()

    def test_exit_after_start_cancels_the_queued_child(self, held):
        rig, start, release = held
        pid = start()
        assert rig.a8s("exit").returncode == 0
        assert release(pid) == 0
        assert not rig.pid_file().exists()

    def test_a_fresh_start_claims(self, held):
        rig, start, release = held
        pid = start()
        (rig.tmp / "barrier" / "go").write_text("")
        assert rig.wait_attached() == pid
        assert rig.a8s("stop", "n1").returncode == 0
        assert release(pid) == 0

    def test_a_newer_start_supersedes_an_older_queued_child(self, held):
        rig, start, release = held
        old = start()
        new = start()
        (rig.tmp / "barrier" / "go").write_text("")
        assert rig.wait_attached() == new
        assert release(old) == 0
        assert rig.a8s("stop", "n1").returncode == 0
        assert release(new) == 0

    def test_remove_and_recreate_refuses_the_old_token(self, held):
        rig, start, release = held
        pid = start()
        assert rig.a8s("remove", "n1").returncode == 0
        assert rig.a8s("add", "n1", str(rig.tmp / "node")).returncode == 0
        assert release(pid) == 0
        assert not rig.pid_file().exists()

    def test_stop_cancels_with_no_pid_and_no_preference(self, nodes):
        token = autostart.new_manual_claim(["a1"])
        assert state("a1")["manual"] == token and not state("a1").get("preference")
        cmd_stop(["a1"])
        assert autostart.record("a1") is None
        import daemon

        assert daemon.claim_without_takeover("a1", None, token) == "refused"

    def test_restart_does_not_revive_an_invalidated_child_and_keeps_preference(self, nodes):
        cmd_start(["a1", "--autostart"])
        old = autostart.new_manual_claim(["a1"])
        cmd_restart(["a1"])
        import daemon

        assert state("a1")["preference"] and state("a1")["armed"]
        assert daemon.claim_without_takeover("a1", None, old) == "refused"


class TestReviewFixes:
    def _update_module(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "ar3_front_door_cli", REPO_ROOT / "apps" / "ar3" / "cli.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_update_stop_keeps_autostart_and_surfaces_stop_failures(self, nodes, monkeypatch, capsys):
        cli = self._update_module()
        cmd_start(["a1", "--autostart"])
        armed = state("a1")
        argvs: list[list[str]] = []

        class Done:
            def __init__(self, rc):
                self.returncode = rc

        def fake_run(argv, **_k):
            argvs.append(list(argv))
            cmd_stop(argv[2:])  # the node is not really running, so its code is not the point
            return Done(0)

        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        monkeypatch.setattr(cli, "_running_nodes", lambda a8s: [])
        stopped: list[str] = []
        cli._stop_nodes("a8s", ["a1"], stopped)
        assert "--keep-autostart" in argvs[0]
        assert stopped == ["a1"]
        assert state("a1") == armed and STORE["a1"].epoch == armed["epoch"]

        MemoryBackend.fail_remove = True
        monkeypatch.setattr(cli.subprocess, "run", lambda argv, **_k: Done(1))
        stopped.clear()
        cli._stop_nodes("a8s", ["a1"], stopped)
        assert stopped == [] and "stop exited 1" in capsys.readouterr().err

    def test_plain_stop_still_disarms(self, nodes):
        cmd_start(["a1", "--autostart"])
        cmd_stop(["a1"])
        assert not state("a1")["armed"]

    def test_later_shell_without_proxy_keeps_every_nodes_snapshot(self, nodes, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://user:pw@proxy")
        monkeypatch.setenv("SSL_CERT_FILE", "/ca.pem")
        cmd_start(["a1", "--autostart"])
        monkeypatch.delenv("HTTPS_PROXY")
        monkeypatch.delenv("SSL_CERT_FILE")
        cmd_start(["b2", "--autostart"])
        cmd_stop(["a1"])
        cmd_start(["a1"])
        cmd_restart(["a1"])
        autostart.load_startup_env("a1")
        assert os.environ["HTTPS_PROXY"] == "http://user:pw@proxy"
        monkeypatch.delenv("HTTPS_PROXY")
        autostart.load_startup_env("b2")
        assert "HTTPS_PROXY" not in os.environ

    def test_explicit_values_update_only_that_node_and_merge(self, nodes, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://one")
        monkeypatch.setenv("SSL_CERT_FILE", "/ca.pem")
        cmd_start(["a1", "b2", "--autostart"])
        monkeypatch.delenv("SSL_CERT_FILE")
        monkeypatch.setenv("HTTPS_PROXY", "http://two")
        cmd_start(["a1", "--autostart"])
        for name in ("HTTPS_PROXY", "SSL_CERT_FILE"):
            monkeypatch.delenv(name, raising=False)
        autostart.load_startup_env("a1")
        assert os.environ["HTTPS_PROXY"] == "http://two" and os.environ["SSL_CERT_FILE"] == "/ca.pem"
        monkeypatch.delenv("HTTPS_PROXY")
        autostart.load_startup_env("b2")
        assert os.environ["HTTPS_PROXY"] == "http://one"

    def test_refused_enable_captures_nothing(self, nodes, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://one")
        MemoryBackend.fail_install = True
        cmd_start(["a1", "--autostart"])
        assert not autostart.env_path().exists()

    def test_no_autostart_starts_when_the_host_has_no_facility(self, nodes, monkeypatch):
        def unavailable():
            raise autostart.AutostartUnavailable("no crontab")

        monkeypatch.setattr(autostart, "select_backend", unavailable)
        assert cmd_start(["a1", "--no-autostart"]) == 0
        assert len(nodes) == 1

    def test_no_autostart_fails_closed_for_a_known_entry_without_facility(self, nodes, monkeypatch, capsys):
        cmd_start(["a1", "--autostart"])

        def unavailable():
            raise autostart.AutostartUnavailable("no crontab")

        monkeypatch.setattr(autostart, "select_backend", unavailable)
        before = len(nodes)
        assert cmd_start(["a1", "--no-autostart"]) == 1
        assert len(nodes) == before and "not disabled" in capsys.readouterr().err
        assert not state("a1")["armed"]

    def test_stuck_stale_pid_file_cannot_hold_the_autostart_lock(self, nodes, monkeypatch):
        import daemon

        monkeypatch.setattr(daemon, "CLAIM_RETRY_S", 0.3)
        monkeypatch.setattr(daemon, "_try_atomic_claim", lambda name, pid: False)
        monkeypatch.setattr(daemon, "_read_handler_pid", lambda name: None)
        cmd_start(["a1", "--autostart"])
        started = time.monotonic()
        assert daemon.claim_without_takeover("a1", state("a1")["epoch"]) == "stuck"
        assert time.monotonic() - started < 5
        with autostart._lock():
            pass

    def test_claim_needs_no_hard_links(self, fake_home, monkeypatch):
        import daemon

        def no_link(*a, **k):
            raise PermissionError("hard links unsupported")

        monkeypatch.setattr(os, "link", no_link)
        assert daemon._try_atomic_claim("probe", os.getpid())
        assert not daemon._try_atomic_claim("probe", os.getpid())
        assert daemon._read_handler_pid("probe") == os.getpid()
        assert not [p for p in daemon.pid_path("probe").parent.iterdir() if p.name.endswith(".claim")]

    def test_failed_add_autostart_says_the_node_is_registered(self, fake_home, tmp_path, capsys, nodes):
        MemoryBackend.fail_install = True
        d = tmp_path / "n"
        d.mkdir()
        assert cmd_add(["n", str(d), "--autostart"]) == 1
        assert "n is registered; only autostart failed" in capsys.readouterr().err
        assert "n" in load_registry()

    def test_run_rejects_flags_it_would_ignore(self, nodes, capsys):
        from commands import cmd_run

        assert cmd_run(["a1", "--autostart"], 1.0) == 2
        assert cmd_run(["a1", "--no-autostart"], 1.0) == 2
        assert cmd_run(["a1", "--startup-home", "/x"], 1.0) == 2
        err = capsys.readouterr().err
        assert "belong to `a8s start`" in err and "--startup-home needs --startup" in err

    def test_cancelled_manual_claim_is_not_reported_as_unarmed(self, nodes, monkeypatch, capsys):
        import daemon

        token = autostart.new_manual_claim(["a1"])
        cmd_stop(["a1"])
        monkeypatch.setattr(daemon, "run_loop", lambda *a, **k: 0, raising=False)
        assert daemon.attached_loop(["a1"], 1.0, takeover=False, manual_claim=token) == 0
        err = capsys.readouterr().err
        assert "cancelled this start" in err and "not armed" not in err


class TestSchtasksIdentity:
    def _backend(self, monkeypatch, stdout, rc=0):
        class P:
            returncode = rc
            stderr = ""

        P.stdout = stdout
        monkeypatch.setattr(autostart, "_run", lambda argv, **k: P)
        return autostart.SchtasksBackend()

    def test_task_uses_the_sid_not_environment_names(self, spec, monkeypatch):
        import dataclasses

        monkeypatch.setenv("USERNAME", "mallory")
        monkeypatch.setenv("USERDOMAIN", "EVIL")
        b = self._backend(monkeypatch, '"HOST\\me","S-1-5-21-1-2-3-1001"\r\n')
        xml = b._xml(dataclasses.replace(spec, foreground=True))
        assert xml.count("<UserId>S-1-5-21-1-2-3-1001</UserId>") == 2
        assert "mallory" not in xml and "EVIL" not in xml

    @pytest.mark.parametrize("stdout,rc", [("", 0), ("garbage", 0), ('"a","S-1-5-21-9"', 1)])
    def test_unknown_identity_fails_closed(self, spec, monkeypatch, stdout, rc):
        import dataclasses

        b = self._backend(monkeypatch, stdout, rc)
        with pytest.raises(autostart.AutostartError, match="SID"):
            b._xml(dataclasses.replace(spec, foreground=True))

    def test_install_refuses_a_console_interpreter(self, spec, monkeypatch):
        import dataclasses

        monkeypatch.setattr(autostart.shutil, "which", lambda n: "schtasks")
        with pytest.raises(autostart.AutostartError, match="pythonw.exe"):
            autostart.SchtasksBackend().install(
                dataclasses.replace(spec, foreground=True, python="C:/Py/python.exe")
            )

    def test_launcher_prefers_pythonw_beside_the_interpreter(self, tmp_path, monkeypatch):
        (tmp_path / "pythonw.exe").write_text("")
        monkeypatch.setattr(autostart.sys, "executable", str(tmp_path / "python.exe"))
        assert autostart.SchtasksBackend().launcher() == str(tmp_path / "pythonw.exe")


class TestEnvFollowups:
    def test_capture_failure_disarms_and_reports(self, nodes, monkeypatch, capsys):
        cmd_start(["b2", "--autostart"])
        monkeypatch.setenv("HTTPS_PROXY", "http://one")

        def boom(saved):
            raise OSError("disk full")

        monkeypatch.setattr(autostart, "_write_env_file", boom)
        assert cmd_start(["a1", "--autostart"]) == 1
        err = capsys.readouterr().err
        assert "could not be saved" in err and "disk full" in err
        assert autostart.record("a1") is None and "a1" not in STORE
        assert not nodes[-1:] or nodes[-1][-1] != "a1"
        # an earlier opt-in keeps its preference but is disarmed and inert
        assert cmd_start(["b2", "--autostart"]) == 1
        rec = state("b2")
        assert rec["preference"] and not rec["armed"] and rec["epoch"] is None
        assert "b2" not in STORE

    def test_capture_failure_with_stuck_entry_is_inert_and_named(self, nodes, monkeypatch, capsys):
        monkeypatch.setenv("HTTPS_PROXY", "http://one")
        monkeypatch.setattr(autostart, "_write_env_file", lambda s: (_ for _ in ()).throw(OSError("ro")))
        monkeypatch.setattr(MemoryBackend, "remove", lambda self, n, unload=False: (_ for _ in ()).throw(
            autostart.AutostartError("refused")))
        assert cmd_start(["a1", "--autostart"]) == 1
        err = capsys.readouterr().err
        assert "inert" in err and "refused" in err
        epoch = STORE["a1"].epoch
        with autostart.startup_claim("a1", epoch) as ok:
            assert not ok

    def test_snapshot_read_waits_through_capture(self, nodes, monkeypatch):
        import threading

        monkeypatch.setenv("HTTPS_PROXY", "http://old")
        cmd_start(["a1", "--autostart"])
        monkeypatch.setenv("HTTPS_PROXY", "http://new")
        in_capture, release = threading.Event(), threading.Event()
        real = autostart._capture_startup_env

        def slow(keys):
            in_capture.set()
            assert release.wait(5)
            real(keys)

        monkeypatch.setattr(autostart, "_capture_startup_env", slow)
        writer = threading.Thread(target=lambda: autostart.enable(["a1"], capture_env=True))
        writer.start()
        assert in_capture.wait(5)
        seen = {}

        def reader():
            import os as _os
            local = {}
            real_set = _os.environ.setdefault
            monkeypatch.setattr(_os.environ, "setdefault", lambda k, v: local.setdefault(k, v))
            autostart.load_startup_env("a1")
            seen.update(local)

        t = threading.Thread(target=reader)
        t.start()
        t.join(0.5)
        assert t.is_alive()
        release.set()
        writer.join(5)
        t.join(5)
        assert seen["HTTPS_PROXY"] == "http://new"


def _refuse_state_writes(monkeypatch):
    def refuse(_nodes):
        raise PermissionError(13, "Permission denied", "autostart.json")
    monkeypatch.setattr(autostart, "_write_state", refuse)


class TestStopWhenStateIsUnwritable:
    def test_disable_reports_a_failed_state_write_as_an_autostart_error(self, nodes, monkeypatch):
        assert autostart.enable(["a1"]) is None
        _refuse_state_writes(monkeypatch)
        with pytest.raises(autostart.AutostartError, match="may start again"):
            autostart.disable(["a1"])
        assert state("a1")["armed"] is True

    def test_invalidate_manual_reports_a_failed_state_write(self, nodes, monkeypatch):
        autostart.new_manual_claim(["a1"])
        _refuse_state_writes(monkeypatch)
        with pytest.raises(autostart.AutostartError, match="pending start"):
            autostart.invalidate_manual(["a1"])
        assert state("a1")["manual"]

    def test_a_lock_that_cannot_be_taken_is_reported_too(self, nodes, monkeypatch):
        autostart.new_manual_claim(["a1"])
        def locked():
            raise PermissionError(13, "Permission denied", "autostart.lock")
        monkeypatch.setattr(autostart, "_lock", locked)
        with pytest.raises(autostart.AutostartError):
            autostart.invalidate_manual(["a1"])
        with pytest.raises(autostart.AutostartError):
            autostart.disable(["a1"], forget_preference=True)

    def test_disarm_all_tries_both_steps_and_reports_both(self, nodes, monkeypatch):
        autostart.enable(["a1"])
        autostart.new_manual_claim(["a1"])
        _refuse_state_writes(monkeypatch)
        with pytest.raises(autostart.AutostartError) as exc:
            autostart.disarm_all()
        assert "pending start" in str(exc.value) and "may start again" in str(exc.value)

    @pytest.mark.parametrize("verb", ["stop", "kill"])
    def test_cli_verbs_exit_nonzero_without_a_traceback(self, nodes, monkeypatch, capsys, verb):
        autostart.enable(["a1", "b2"])
        autostart.new_manual_claim(["a1"])
        _refuse_state_writes(monkeypatch)
        rc = (cmd_stop if verb == "stop" else cmd_kill)(["a1"] if verb == "kill" else ["a1", "b2"])
        err = capsys.readouterr().err
        assert rc != 0
        assert "Traceback" not in err
        assert "pending start not cancelled" in err and "entry not removed" in err

    def test_exit_exits_nonzero_without_a_traceback(self, nodes, monkeypatch, capsys):
        autostart.enable(["a1"])
        _refuse_state_writes(monkeypatch)
        assert cmd_exit() != 0
        assert "Traceback" not in capsys.readouterr().err

    @pytest.mark.skipif(os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                        reason="a read-only directory needs POSIX and a non-root user")
    @pytest.mark.parametrize("verb,extra", [("stop", []), ("stop", ["--force"]), ("kill", []), ("exit", [])])
    def test_a_real_handler_still_stops_and_the_verb_fails(self, rig, verb, extra):
        assert rig.a8s("start", "n1", "--autostart").returncode == 0
        pid = rig.wait_attached()
        rig.home.chmod(0o555)
        try:
            res = rig.a8s(verb, *([] if verb == "exit" else ["n1"]), *extra)
        finally:
            rig.home.chmod(0o755)
        assert res.returncode != 0
        assert "Traceback" not in res.stderr
        assert "may start again" in res.stderr
        deadline = time.time() + 20
        while time.time() < deadline and rig.pid_file().is_file():
            time.sleep(0.1)
        assert not rig.pid_file().is_file()
        if verb in ("stop", "exit"):
            with pytest.raises(OSError):
                os.kill(pid, 0)
        assert state_from(rig)["armed"] is True


class TestStopWhenStateIsUnreadable:
    BAD = {"malformed": "{not json", "wrong-shape": "[1, 2]", "nodes-not-object": '{"nodes": []}', "node-not-object": '{"nodes": {"a1": 1}}'}

    @pytest.fixture(params=sorted(BAD))
    def corrupt(self, request):
        autostart.state_path().parent.mkdir(parents=True, exist_ok=True)
        autostart.state_path().write_text(self.BAD[request.param], encoding="utf-8")

    def test_a_missing_file_is_still_empty_and_stop_succeeds(self, nodes):
        assert not autostart.state_path().exists()
        assert autostart.disable(["a1"]) is None
        assert autostart.invalidate_manual(["a1"]) is None
        assert autostart.disarm_all() is None

    def test_explicit_shutdown_paths_report_corrupt_state(self, nodes, corrupt):
        with pytest.raises(autostart.AutostartError, match="may start again"):
            autostart.disable(["a1"])
        with pytest.raises(autostart.AutostartError, match="may start again"):
            autostart.disable(["a1"], forget_preference=True)
        with pytest.raises(autostart.AutostartError):
            autostart.invalidate_manual(["a1"])
        with pytest.raises(autostart.AutostartError):
            autostart.disarm_all()

    def test_the_lenient_readers_still_see_nothing(self, nodes, corrupt):
        assert autostart.record("a1") is None
        assert autostart.opted_in() == []

    def test_an_unreadable_file_is_reported(self, nodes, monkeypatch):
        autostart.enable(["a1"])
        real = Path.read_text
        def deny(self, *a, **k):
            if self == autostart.state_path():
                raise PermissionError(13, "Permission denied", str(self))
            return real(self, *a, **k)
        monkeypatch.setattr(Path, "read_text", deny)
        with pytest.raises(autostart.AutostartError, match="cannot be read"):
            autostart.disable(["a1"])
        with pytest.raises(autostart.AutostartError, match="cannot be read"):
            autostart.disarm_all()

    def test_a_reread_under_the_lock_is_checked_too(self, nodes, monkeypatch):
        autostart.enable(["a1"])
        real = autostart._read_state
        calls = []
        def flaky(strict=False):
            calls.append(strict)
            if len(calls) > 1:
                raise autostart.AutostartError("unreadable on reread")
            return real(strict)
        monkeypatch.setattr(autostart, "_read_state", flaky)
        with pytest.raises(autostart.AutostartError, match="unreadable on reread"):
            autostart.disable(["a1"])
        assert calls == [True, True]
        monkeypatch.setattr(autostart, "_read_state", real)
        assert state("a1")["armed"] is True

    @pytest.mark.parametrize("verb", ["stop", "kill", "exit"])
    def test_cli_verbs_exit_nonzero_without_a_traceback(self, nodes, corrupt, capsys, verb):
        rc = {"stop": lambda: cmd_stop(["a1"]), "kill": lambda: cmd_kill(["a1"]), "exit": cmd_exit}[verb]()
        err = capsys.readouterr().err
        assert rc != 0 and "Traceback" not in err

    def test_no_autostart_fails_closed(self, nodes, corrupt, capsys):
        assert commands._autostart_before_start(["a1"], "off") == 1
        assert "not disabled" in capsys.readouterr().err

    @pytest.mark.skipif(os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                        reason="a read-only file needs POSIX and a non-root user")
    @pytest.mark.parametrize("verb", ["stop", "kill", "exit"])
    def test_a_real_handler_still_stops_and_the_verb_fails(self, rig, verb):
        assert rig.a8s("start", "n1", "--autostart").returncode == 0
        pid = rig.wait_attached()
        path = rig.home / "autostart.json"
        path.chmod(0)
        try:
            res = rig.a8s(verb, *([] if verb == "exit" else ["n1"]))
        finally:
            path.chmod(0o644)
        assert res.returncode != 0
        assert "Traceback" not in res.stderr
        assert "cannot be read" in res.stderr
        deadline = time.time() + 20
        while time.time() < deadline and rig.pid_file().is_file():
            time.sleep(0.1)
        assert not rig.pid_file().is_file()
        if verb in ("stop", "exit"):
            with pytest.raises(OSError):
                os.kill(pid, 0)


def state_from(rig) -> dict:
    return json.loads((rig.home / "autostart.json").read_text())["nodes"]["n1"]
