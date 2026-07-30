"""OS-level isolation — run_as and container variants.

Isolation is a PER-ORG setting (the 2026-07-16 ruling): one Unix user or one
image serves an org's whole roster, so it lives in r4t-org.json, not on the
machine-global rig. The org's choice rides to run_harness through the turn env.

Wrapper argv is asserted EXACTLY; the prereq probe and the container kill run
against fake `sudo`/`docker` binaries put on PATH — no real sudo, docker, or
LLM. State stays under the tmp R4T_HOME the shared fixtures set; the live
~/.config/r4t is never touched.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import isolate
import state
from dispatch import DispatchContext, drain, handle_message, run_harness
from isolate import Isolation
from org import ORG_CONFIG_NAME, check_org, load_org
from rig import (
    A8S_PY,
    Rig,
    add_preset_rig,
    apply_mcp,
    load_rig_config,
    set_rig_value,
)
from roster import Member

NODE = "acme"


def _fake_bin(directory: Path, name: str, body: str) -> Path:
    """Write an executable Python stub named `name` into `directory`."""
    path = directory / name
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def fakebin(tmp_path, monkeypatch):
    d = tmp_path / "fakebin"
    d.mkdir()
    monkeypatch.setenv("PATH", str(d) + os.pathsep + os.environ.get("PATH", ""))
    return d


BOOTSTRAP = (
    'export TELL_OUTBOX_DIR="$1"; cd "$2"; n="$3"; shift 3; '
    'while [ "$n" -gt 0 ]; do export "$1"; shift; n=$((n - 1)); done; exec "$@"'
)


class TestWrapRunAs:
    def test_exact_argv(self):
        argv = isolate.wrap_run_as(
            ["claude", "-p", "{hi}"], "agent-x", "/stg/dir", "/work/place"
        )
        assert argv == [
            "sudo", "-u", "agent-x", "bash", "--login", "-c", BOOTSTRAP,
            "_", "/stg/dir", "/work/place", "0", "claude", "-p", "{hi}",
        ]

    def test_env_rides_as_positionals_not_a_command_string(self):
        argv = isolate.wrap_run_as(["h", "a b"], "u", "/s", "/w")
        # The bootstrap is a single -c argument; the harness argv follows as
        # discrete positionals, so a space in an arg can never re-split.
        assert argv[6] == BOOTSTRAP
        assert argv[-2:] == ["h", "a b"]

    def test_env_pass_rides_as_counted_positionals(self):
        argv = isolate.wrap_run_as(
            ["h", "{p}"], "u", "/s", "/w",
            env_pass={"OPENCODE_CONFIG": "/state/mcp/c.json", "K": "a b"},
        )
        assert argv[7:] == [
            "_", "/s", "/w", "2",
            "OPENCODE_CONFIG=/state/mcp/c.json", "K=a b", "h", "{p}",
        ]

    def test_bootstrap_really_exports_what_it_is_handed(self, tmp_path):
        # Functional, not shape: run the bootstrap under a real bash (no sudo)
        # and have the "harness" report the environment it was handed.
        script = tmp_path / "show.py"
        script.write_text(
            "import os\nprint(os.environ.get('OPENCODE_CONFIG', 'unset'))\n"
            "print(os.environ.get('A8S_MCP_LOG', 'unset'))\n"
            "print(os.environ['TELL_OUTBOX_DIR'])\nprint(os.getcwd())\n",
            encoding="utf-8",
        )
        argv = isolate.wrap_run_as(
            [sys.executable, str(script)], "u", tmp_path, tmp_path,
            # A value with a space is the case a quoted command string mangles.
            env_pass={"OPENCODE_CONFIG": "/state/mcp/c.json", "A8S_MCP_LOG": "x y"},
        )
        out = subprocess.run(
            ["bash", "-c", *argv[6:]], capture_output=True, text=True,
            env={"PATH": os.environ["PATH"]},
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.splitlines() == [
            "/state/mcp/c.json", "x y", str(tmp_path), str(tmp_path),
        ]


class TestBuildContainer:
    def test_exact_argv(self):
        argv = isolate.build_container_argv(
            ["claude", "-p", "{hi}"],
            "myimg:latest",
            name="r4t-acme-phil-42",
            staging_dir="/stg",
            workplace="/work",
            tell_outbox="/stg",
            client_dir="/opt/bin",
        )
        assert argv == [
            "docker", "run", "--rm", "--name", "r4t-acme-phil-42",
            "-v", "/work:/work",
            "-w", "/work",
            "-v", "/stg:/stg",
            "-e", "TELL_OUTBOX_DIR=/stg",
            "-v", "/opt/bin:/opt/bin:ro",
            "-e", "PATH=/opt/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "myimg:latest", "claude", "-p", "{hi}",
        ]

    def test_container_args_appended_verbatim_before_image(self):
        argv = isolate.build_container_argv(
            ["h", "{p}"], "img", name="n",
            staging_dir="/s", workplace="/w", tell_outbox="/s",
            container_args=["--gpus", "all", "-v", "/creds:/creds:ro"],
            client_dir="/c",
        )
        i = argv.index("img")
        assert argv[i - 4:i] == ["--gpus", "all", "-v", "/creds:/creds:ro"]
        assert argv[i:] == ["img", "h", "{p}"]

    def test_delivered_dir_mounts_read_only(self):
        argv = isolate.build_container_argv(
            ["h", "{p}"], "img", name="n",
            staging_dir="/s", workplace="/w", tell_outbox="/s",
            delivered_dir="/deliver", client_dir="/c",
        )
        assert "-v" in argv and "/deliver:/deliver:ro" in argv

    def test_container_name_deterministic_with_ts_and_slugs_bad_chars(self):
        assert isolate.container_name("ac me", "Phil/1", ts=7) == "r4t-ac-me-Phil-1-7"


class TestOrgConfigValidation:
    """Isolation now parses out of r4t-org.json (org.py), validated where
    `doorbell_check` is: load_org degrades to no isolation on a bad value,
    check_org reports it."""

    def _write(self, tmp_path, settings: dict) -> Path:
        (tmp_path / ORG_CONFIG_NAME).write_text(json.dumps(settings), encoding="utf-8")
        return tmp_path

    def test_both_set_is_config_error(self, tmp_path):
        self._write(tmp_path, {"run_as": "u", "container": "img"})
        assert any("mutually exclusive" in m for m in check_org(tmp_path))

    def test_both_set_degrades_to_no_isolation(self, tmp_path):
        self._write(tmp_path, {"run_as": "u", "container": "img"})
        assert not load_org(tmp_path).isolation.active  # fail closed: neither applies

    def test_container_args_without_container_errors(self, tmp_path):
        self._write(tmp_path, {"container_args": ["--gpus", "all"]})
        assert any(
            'container_args" set but "container" is not' in m for m in check_org(tmp_path)
        )

    def test_blank_run_as_errors(self, tmp_path):
        self._write(tmp_path, {"run_as": "   "})
        assert any("non-empty username" in m for m in check_org(tmp_path))

    def test_valid_run_as_parses(self, tmp_path):
        self._write(tmp_path, {"run_as": "agent-x"})
        org = load_org(tmp_path)
        assert check_org(tmp_path) == []
        assert org.isolation.run_as == "agent-x" and org.isolation.active

    def test_valid_container_parses_with_args(self, tmp_path):
        self._write(tmp_path, {"container": "img", "container_args": ["-v", "/c:/c:ro"]})
        org = load_org(tmp_path)
        assert check_org(tmp_path) == []
        assert org.isolation.container == "img"
        assert org.isolation.container_args == ["-v", "/c:/c:ro"]

    def test_absent_isolation_is_the_default(self, tmp_path):
        assert not load_org(tmp_path).isolation.active


class TestEnvRoundTrip:
    """The org's choice reaches run_harness through the turn env only — the
    run_fn contract stays (rig, prompt, cwd, env, variant)."""

    def test_run_as_round_trips(self):
        env = Isolation(run_as="agent-x").to_env()
        assert isolate.isolation_from_env(env).run_as == "agent-x"

    def test_container_and_args_round_trip(self):
        env = Isolation(container="img", container_args=["-v", "/c:/c:ro"]).to_env()
        got = isolate.isolation_from_env(env)
        assert got.container == "img" and got.container_args == ["-v", "/c:/c:ro"]

    def test_bare_org_adds_nothing_to_env(self):
        assert Isolation().to_env() == {}
        assert not isolate.isolation_from_env({}).active


class TestSharedDirAssertion:
    def _mode(self, path: Path) -> int:
        return stat.S_IMODE(path.stat().st_mode)

    def test_writable_dir_gets_2770_setgid(self, tmp_path):
        d = tmp_path / "staging"
        isolate.assert_writable_shared_dir(d, os.getgid())
        assert self._mode(d) == 0o2770
        assert d.stat().st_gid == os.getgid()

    def test_readonly_dir_gets_2750_setgid(self, tmp_path):
        d = tmp_path / "delivered"
        isolate.assert_readonly_shared_dir(d, os.getgid())
        assert self._mode(d) == 0o2750

    def test_reasserts_after_tampering(self, tmp_path):
        d = tmp_path / "staging"
        isolate.assert_writable_shared_dir(d, os.getgid())
        d.chmod(0o700)  # an agent (or drift) narrows it
        assert self._mode(d) != 0o2770
        isolate.assert_writable_shared_dir(d, os.getgid())  # re-assert before the next turn
        assert self._mode(d) == 0o2770

    def test_unknown_group_still_sets_mode(self, tmp_path):
        d = tmp_path / "staging"
        isolate.assert_writable_shared_dir(d, None)  # gid None: skip chown, keep mode
        assert self._mode(d) == 0o2770


# ---------- dispatch-level: fail closed, breaker, kill-by-name ----------


ROSTER = textwrap.dedent(
    """\
    # Team

    ### Gerry
    - **Rig:** leader
    - **Leader:** yes

    ### Phil
    - **Rig:** junior-dev
    """
)


def _iso_config(tmp_path, fake_harness) -> Path:
    script, _out = fake_harness
    invoke = [sys.executable, str(script), "{prompt}"]
    payload = {
        "throttle": {"max_concurrent": 0, "min_seconds_between_turn_starts": 0},
        "cell_budget_max": 200,
        "cell_budget_earn_per_hour": 100,
        "leader": {"invoke": invoke, "timeout_seconds": 30, "budget_max": 100, "budget_earn_per_hour": 50},
        "junior-dev": {
            "invoke": invoke, "timeout_seconds": 30, "budget_max": 100,
            "budget_earn_per_hour": 50,
        },
        "pins": {"gerry": "leader"},
    }
    path = tmp_path / "iso-rigs.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def iso_ctx_factory(r4t_home, tmp_path, fake_harness, tells):
    def make(isolation: dict | None = None) -> DispatchContext:
        root = tmp_path / "iso-repo"
        root.mkdir(exist_ok=True)
        (root / "ROSTER.md").write_text(ROSTER, encoding="utf-8")
        _sent, capture = tells
        return DispatchContext(
            root=root,
            node=NODE,
            roster_path=root / "ROSTER.md",
            config_path=_iso_config(tmp_path, fake_harness),
            tell_fn=capture,
            isolation=Isolation(**(isolation or {})),
        )

    return make


class TestRunAsProbeFailsClosed:
    def test_failed_grant_probe_fails_turn_and_requeues_and_trips_breaker(
        self, iso_ctx_factory, fakebin
    ):
        _fake_bin(fakebin, "sudo", "import sys\nsys.exit(1)\n")  # no NOPASSWD grant
        ctx = iso_ctx_factory({"run_as": "agent-x"})

        handle_message(ctx, "acme:gerry", "acme:phil", "do work", drain_after=False)
        ran = drain(ctx, run_fn=run_harness)

        assert ran == 1  # the turn ran and failed closed (not skipped)
        assert state.queue_depth(NODE, "phil") >= 1  # message returned to the queue
        assert state.read_meta(NODE, "phil")["consecutive_failures"] == 1  # breaker counts it

    def test_probe_error_surfaces_the_fix(self, fakebin):
        _fake_bin(fakebin, "sudo", "import sys\nsys.exit(1)\n")
        rig = Rig(name="junior-dev", invoke=["true", "{prompt}"])
        env = {"TELL_OUTBOX_DIR": "/tmp/s", **Isolation(run_as="agent-x").to_env()}
        code, out, _dur, timed = run_harness(rig, "p", Path("/tmp"), env=env)
        assert code == 126 and not timed
        assert "no passwordless sudo" in out and "docs/isolation.md" in out


class TestOrgIsolationAppliesToEveryRig:
    """One org setting wraps every member turn identically, whatever rig runs
    it — the whole point of moving the knob rig -> org."""

    def test_same_run_as_wraps_two_different_rigs_identically(self, tmp_path, fakebin):
        record = tmp_path / "sudo-argv.txt"
        _fake_bin(
            fakebin, "sudo",
            f"""
            import sys
            a = sys.argv[1:]
            # record only the real wrapped invoke (the bootstrap -c string), not
            # the two prereq probes; then exit 0 so probes pass and the run is a
            # no-op we can inspect.
            if "-c" in a and a[a.index("-c") + 1].startswith("export TELL_OUTBOX_DIR"):
                open({str(record)!r}, "a").write(repr(a) + "\\n")
            sys.exit(0)
            """,
        )
        env = dict(os.environ)  # keep PATH so the real wrapped `sudo` resolves the stub
        env["TELL_OUTBOX_DIR"] = str(tmp_path / "stg")
        env.update(Isolation(run_as="agent-x").to_env())
        leader = Rig(name="leader", invoke=["claude-harness", "-p", "{prompt}"])
        junior = Rig(name="junior", invoke=["codex-harness", "exec", "{prompt}"])

        run_harness(leader, "P", tmp_path, env=dict(env))
        run_harness(junior, "P", tmp_path, env=dict(env))

        lines = record.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        recorded = [eval(line) for line in lines]  # noqa: S307 — test-owned stub output
        # Both turns are wrapped by the SAME boundary (sudo -u agent-x ...); only
        # the trailing harness argv differs, proving isolation is rig-agnostic.
        for a in recorded:
            assert a[:5] == ["-u", "agent-x", "bash", "--login", "-c"]
        assert recorded[0][-3:] == ["claude-harness", "-p", "P"]
        assert recorded[1][-3:] == ["codex-harness", "exec", "P"]


class TestRigLevelIsolationIsGone:
    """Rig-level run_as/container ceased to exist (pre-v1 scorch-the-earth). A
    stray key in rigs.json follows rig.py's unknown-key convention: ignored,
    not an error — and it never wraps a turn."""

    def test_rig_run_as_and_container_keys_are_ignored_not_errors(self, tmp_path):
        path = tmp_path / "rigs.json"
        path.write_text(
            json.dumps(
                {"iso": {"invoke": ["h", "{prompt}"], "run_as": "u", "container": "img"}}
            ),
            encoding="utf-8",
        )
        config = load_rig_config(path)
        rig = config.rigs["iso"]
        assert rig.error is None  # unknown keys are ignored, not rejected
        assert not hasattr(rig, "run_as") and not hasattr(rig, "container")
        member = Member(name="Bob", rig="iso")
        resolved, err, _pinned = config.rig_for(member)
        assert resolved is not None and err is None  # the rig still runs; no isolation


class TestContainerTimeoutKill:
    def test_timeout_kills_container_by_name(self, tmp_path, fakebin, monkeypatch):
        record = tmp_path / "docker-kills.txt"
        _fake_bin(
            fakebin, "docker",
            f"""
            import os, sys, time
            args = sys.argv[1:]
            if args and args[0] == "run":
                time.sleep(30)
            elif args and args[0] == "kill":
                open({str(record)!r}, "a").write(args[1] + "\\n")
            """,
        )
        rig = Rig(name="c", invoke=["harness", "{prompt}"], timeout_seconds=0.5)
        env = dict(os.environ)
        env["TELL_OUTBOX_DIR"] = str(tmp_path / "stg")
        env["R4T_NODE"] = "acme"
        env["R4T_MEMBER"] = "phil"
        env.update(Isolation(container="img").to_env())

        _code, _out, _dur, timed_out = run_harness(rig, "p", tmp_path, env=env)

        assert timed_out
        killed = record.read_text(encoding="utf-8").split()
        assert len(killed) == 1
        assert killed[0].startswith("r4t-acme-phil-")


# ---------- the `mcp` knob has to cross the boundary too (#314) ----------


def _mcp_rig(tmp_path, preset: str) -> Rig:
    path = tmp_path / f"mcp-rigs-{preset}.json"
    add_preset_rig(path, "worker", preset, force=True)
    set_rig_value(path, "worker", "mcp", "on")
    return load_rig_config(path).rigs["worker"]


def _mcp_turn_env(tmp_path, isolation: Isolation) -> tuple[dict, Path, Path]:
    """A turn env shaped like dispatch's: a per-member staging outbox, a
    workplace, the router's HOME, and the org's isolation choice."""
    staging = tmp_path / "state" / "worker" / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    workplace = tmp_path / "work"
    workplace.mkdir(exist_ok=True)
    env = dict(os.environ)  # keep PATH so the fake sudo/docker resolve
    env["TELL_OUTBOX_DIR"] = str(staging)
    env["HOME"] = str(tmp_path / "router-home")
    env["R4T_NODE"] = NODE
    env["R4T_MEMBER"] = "worker"
    env.update(isolation.to_env())
    return env, workplace, staging


def _recording_sudo(fakebin, record: Path, *, unreadable: str = "") -> None:
    """A `sudo` stub that answers the prereq probes, optionally fails the
    read-access probe, and records the wrapped invoke instead of running it."""
    _fake_bin(
        fakebin, "sudo",
        f"""
        import sys
        a = sys.argv[1:]
        script = a[a.index("-c") + 1] if "-c" in a else ""
        if script.startswith("for p;"):
            if {unreadable!r}:
                print({unreadable!r})
                sys.exit(1)
            sys.exit(0)
        if script.startswith("export TELL_OUTBOX_DIR"):
            open({str(record)!r}, "w").write(repr(a))
        sys.exit(0)
        """,
    )


def _recording_docker(fakebin, record: Path) -> None:
    _fake_bin(
        fakebin, "docker",
        f"""
        import sys
        a = sys.argv[1:]
        if a and a[0] == "run":
            open({str(record)!r}, "w").write(repr(a))
        """,
    )


def _wrapped_argv(record: Path) -> list[str]:
    return eval(record.read_text(encoding="utf-8"))  # noqa: S307 — test-owned stub


def _server_from_flag(argv: list[str], flag: str) -> dict:
    """The server the wrapped argv carries. codex speaks TOML, and its `-c` has
    to be told apart from the wrapper's own `bash -c`."""
    payloads = [
        argv[i + 1] for i, token in enumerate(argv[:-1])
        if token == flag and ("a8s" in argv[i + 1])
    ]
    assert len(payloads) == 1, f"{flag} payload not carried across: {argv}"
    if flag == "-c":
        return {"toml": payloads[0]}
    return json.loads(payloads[0])["mcpServers"]["a8s"]


FLAG_IDIOMS = {
    "claude": "--mcp-config",
    "codex": "-c",
    "copilot": "--additional-mcp-config",
}


class TestMcpCrossesRunAs:
    """Every idiom the knob supports either reaches the harness behind
    `sudo`/`env_reset` or fails the turn — a prompt that teaches `a8s_tell`
    against a server that never started is the one outcome worth refusing."""

    @pytest.mark.parametrize("preset,flag", sorted(FLAG_IDIOMS.items()))
    def test_flag_idioms_ride_argv_through_the_bootstrap(
        self, tmp_path, fakebin, preset, flag
    ):
        record = tmp_path / f"sudo-{preset}.txt"
        _recording_sudo(fakebin, record)
        rig = _mcp_rig(tmp_path, preset)
        env, workplace, _staging = _mcp_turn_env(tmp_path, Isolation(run_as="agent-x"))

        run_harness(rig, "P", workplace, env=env)

        argv = _wrapped_argv(record)
        assert argv[:5] == ["-u", "agent-x", "bash", "--login", "-c"]
        assert flag in argv
        server = _server_from_flag(argv, flag)
        # The router's own interpreter: same filesystem on this side of sudo.
        assert sys.executable in (server.get("command") or server["toml"])

    def test_opencode_env_rides_as_a_re_exported_positional(self, tmp_path, fakebin):
        record = tmp_path / "sudo-opencode.txt"
        _recording_sudo(fakebin, record)
        rig = _mcp_rig(tmp_path, "opencode")
        env, workplace, staging = _mcp_turn_env(tmp_path, Isolation(run_as="agent-x"))

        run_harness(rig, "P", workplace, env=env)

        config = staging.parent / "mcp" / "mcp-opencode.json"
        argv = _wrapped_argv(record)
        # _ staging workplace <count> <NAME=value>... then the harness argv.
        assert argv[6:10] == [
            "_", str(staging), str(workplace), "1"
        ]
        assert argv[10] == f"OPENCODE_CONFIG={config}"
        assert config.is_file()

    def test_config_file_is_readable_by_another_user(self, tmp_path, fakebin):
        _recording_sudo(fakebin, tmp_path / "sudo.txt")
        rig = _mcp_rig(tmp_path, "opencode")
        env, workplace, staging = _mcp_turn_env(tmp_path, Isolation(run_as="agent-x"))
        (staging.parent).chmod(0o700)  # a narrow umask, or drift

        run_harness(rig, "P", workplace, env=env)

        config = staging.parent / "mcp" / "mcp-opencode.json"
        assert stat.S_IMODE(config.stat().st_mode) & 0o044 == 0o044
        assert stat.S_IMODE(config.parent.stat().st_mode) & 0o055 == 0o055

    def test_cursor_file_lands_in_the_workplace_and_is_readable(self, tmp_path, fakebin):
        _recording_sudo(fakebin, tmp_path / "sudo.txt")
        rig = _mcp_rig(tmp_path, "cursor")
        env, workplace, _staging = _mcp_turn_env(tmp_path, Isolation(run_as="agent-x"))

        run_harness(rig, "P", workplace, env=env)

        # The workplace is the one dir both wrappers already give the harness.
        config = workplace / ".cursor" / "mcp.json"
        assert stat.S_IMODE(config.stat().st_mode) & 0o044 == 0o044
        assert json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["a8s"]

    def test_setgid_on_a_shared_workplace_survives_the_mode_fix(self, tmp_path, fakebin):
        _recording_sudo(fakebin, tmp_path / "sudo.txt")
        rig = _mcp_rig(tmp_path, "cursor")
        env, workplace, _staging = _mcp_turn_env(tmp_path, Isolation(run_as="agent-x"))
        (workplace / ".cursor").mkdir()
        (workplace / ".cursor").chmod(0o2770)  # the shared-group workplace

        run_harness(rig, "P", workplace, env=env)

        assert stat.S_IMODE((workplace / ".cursor").stat().st_mode) & 0o2000

    def test_unreadable_server_fails_the_turn_closed(self, tmp_path, fakebin):
        record = tmp_path / "sudo.txt"
        _recording_sudo(fakebin, record, unreadable=str(A8S_PY))
        rig = _mcp_rig(tmp_path, "claude")
        env, workplace, _staging = _mcp_turn_env(tmp_path, Isolation(run_as="agent-x"))

        code, out, _dur, timed = run_harness(rig, "P", workplace, env=env)

        assert code == 126 and not timed
        assert "mcp on" in out and "cannot read" in out and str(A8S_PY) in out
        assert "docs/isolation.md" in out
        assert not record.exists()  # the harness never ran taught-but-toolless

    def test_router_home_is_not_promised_to_the_server(self, tmp_path, fakebin):
        record = tmp_path / "sudo.txt"
        _recording_sudo(fakebin, record)
        rig = _mcp_rig(tmp_path, "claude")
        env, workplace, staging = _mcp_turn_env(tmp_path, Isolation(run_as="agent-x"))

        run_harness(rig, "P", workplace, env=env)

        server = _server_from_flag(_wrapped_argv(record), "--mcp-config")
        # HOME belongs to the router and is unreachable behind the boundary; the
        # outbox is what crosses, and `tell` needs nothing else.
        assert server["env"] == {"TELL_OUTBOX_DIR": str(staging)}


class TestMcpCrossesContainer:
    """A container keeps only what `docker run` is told to carry, and its
    filesystem is the image's — the injection has to name both."""

    @pytest.mark.parametrize("preset,flag", sorted(FLAG_IDIOMS.items()))
    def test_flag_idioms_name_the_image_interpreter(self, tmp_path, fakebin, preset, flag):
        record = tmp_path / f"docker-{preset}.txt"
        _recording_docker(fakebin, record)
        rig = _mcp_rig(tmp_path, preset)
        env, workplace, _staging = _mcp_turn_env(tmp_path, Isolation(container="img"))

        run_harness(rig, "P", workplace, env=env)

        argv = _wrapped_argv(record)
        assert flag in argv
        server = _server_from_flag(argv, flag)
        command = server.get("command") or server["toml"]
        # The router's interpreter path is not in the image; the a8s client dir
        # is mounted at the same path, and `python3` resolves from the image.
        assert sys.executable not in command
        assert "python3" in command
        assert str(A8S_PY) in str(server.get("args") or server["toml"])

    def test_opencode_gets_the_env_and_a_mount_of_its_own_dir(self, tmp_path, fakebin):
        record = tmp_path / "docker-opencode.txt"
        _recording_docker(fakebin, record)
        rig = _mcp_rig(tmp_path, "opencode")
        env, workplace, staging = _mcp_turn_env(tmp_path, Isolation(container="img"))

        run_harness(rig, "P", workplace, env=env)

        config = staging.parent / "mcp" / "mcp-opencode.json"
        argv = _wrapped_argv(record)
        assert f"OPENCODE_CONFIG={config}" in argv
        assert argv[argv.index(f"OPENCODE_CONFIG={config}") - 1] == "-e"
        mount = f"{config.parent}:{config.parent}:ro"
        assert mount in argv and argv[argv.index(mount) - 1] == "-v"
        # The mount carries the config and nothing else — no history, no
        # transcripts from the member state dir above it.
        assert f"{staging.parent}:{staging.parent}:ro" not in argv
        assert list(config.parent.iterdir()) == [config]

    def test_org_container_args_still_win(self, tmp_path, fakebin):
        record = tmp_path / "docker-args.txt"
        _recording_docker(fakebin, record)
        rig = _mcp_rig(tmp_path, "opencode")
        env, workplace, _staging = _mcp_turn_env(
            tmp_path,
            Isolation(container="img", container_args=["-e", "OPENCODE_CONFIG=/theirs"]),
        )

        run_harness(rig, "P", workplace, env=env)

        argv = _wrapped_argv(record)
        ours = next(a for a in argv if a.startswith("OPENCODE_CONFIG=") and a != "OPENCODE_CONFIG=/theirs")
        # docker takes the last value, so the org's own args are still the
        # override of last resort.
        assert argv.index(ours) < argv.index("OPENCODE_CONFIG=/theirs") < argv.index("img")
        assert argv[argv.index("img") - 2:argv.index("img")] == [
            "-e", "OPENCODE_CONFIG=/theirs"
        ]

    def test_cursor_file_rides_the_workplace_mount(self, tmp_path, fakebin):
        record = tmp_path / "docker-cursor.txt"
        _recording_docker(fakebin, record)
        rig = _mcp_rig(tmp_path, "cursor")
        env, workplace, _staging = _mcp_turn_env(tmp_path, Isolation(container="img"))

        run_harness(rig, "P", workplace, env=env)

        assert (workplace / ".cursor" / "mcp.json").is_file()
        assert f"{workplace}:{workplace}" in _wrapped_argv(record)


class TestMcpWithoutIsolation:
    """A bare org keeps the plain shape: the router's interpreter, its HOME
    pinned, and nothing extra asked of any wrapper."""

    def test_plain_turn_pins_home_and_the_router_interpreter(self, tmp_path):
        rig = _mcp_rig(tmp_path, "claude")
        env, workplace, _staging = _mcp_turn_env(tmp_path, Isolation())
        plan = apply_mcp(rig, rig.argv("P"), env, workplace, Isolation())

        server = _server_from_flag(plan.argv, "--mcp-config")
        assert server["command"] == sys.executable
        assert server["env"]["HOME"] == env["HOME"]
        assert plan.env_pass == {} and plan.mount_dirs == []

    def test_a_preset_without_an_idiom_asks_nothing_of_the_boundary(self, tmp_path):
        rig = Rig(name="agy-rig", invoke=["agy", "{prompt}"], preset="agy", mcp=True)
        env, workplace, _staging = _mcp_turn_env(tmp_path, Isolation(run_as="agent-x"))
        plan = apply_mcp(rig, rig.argv("P"), env, workplace, Isolation(run_as="agent-x"))

        assert plan.argv == rig.argv("P")
        assert not plan.env_pass and not plan.mount_dirs and not plan.read_paths


# ---------- the rig's `env` map has to cross the boundary too (#284) ----------


def _env_rig(env_map: dict[str, str], invoke: list[str] | None = None) -> Rig:
    return Rig(name="worker", invoke=invoke or ["claude", "-p", "{prompt}"], env=env_map)


class TestRigEnvReachesTheHarness:
    """A rig env entry is worthless if it stops at the isolation wrapper, so the
    same three shapes the `mcp` idioms are asserted through carry it too."""

    def test_plain_turn_hands_it_to_the_harness_process(self, tmp_path):
        script = tmp_path / "show-env.py"
        script.write_text(
            "import os\nprint(os.environ.get('ENABLE_PROMPT_CACHING_1H', 'unset'))\n",
            encoding="utf-8",
        )
        rig = _env_rig(
            {"ENABLE_PROMPT_CACHING_1H": "1"},
            invoke=[sys.executable, str(script), "{prompt}"],
        )
        env, workplace, _staging = _mcp_turn_env(tmp_path, Isolation())

        code, out, _dur, timed = run_harness(rig, "P", workplace, env=env)

        assert (code, timed) == (0, False)
        assert out.strip() == "1"

    def test_the_workdir_pin_still_wins(self, tmp_path):
        script = tmp_path / "show-pwd.py"
        script.write_text("import os\nprint(os.environ['PWD'])\n", encoding="utf-8")
        # A rig carrying PWD fails closed at parse time (test_rig), so the only
        # way here is in-memory — and the pin holds whatever a Rig object says.
        rig = _env_rig(
            {"PWD": "/nowhere"}, invoke=[sys.executable, str(script), "{prompt}"]
        )
        env, workplace, _staging = _mcp_turn_env(tmp_path, Isolation())

        _code, out, _dur, _timed = run_harness(rig, "P", workplace, env=env)

        assert out.strip() == str(workplace)

    def test_run_as_re_exports_it_past_env_reset(self, tmp_path, fakebin):
        record = tmp_path / "sudo.txt"
        _recording_sudo(fakebin, record)
        rig = _env_rig({"ENABLE_PROMPT_CACHING_1H": "1", "NOTE": "a b"})
        env, workplace, staging = _mcp_turn_env(tmp_path, Isolation(run_as="agent-x"))

        run_harness(rig, "P", workplace, env=env)

        argv = _wrapped_argv(record)
        assert argv[6:12] == [
            "_", str(staging), str(workplace), "2",
            "ENABLE_PROMPT_CACHING_1H=1", "NOTE=a b",
        ]
        assert argv[12:] == ["claude", "-p", "P"]

    def test_container_gets_it_as_a_dash_e_before_the_image(self, tmp_path, fakebin):
        record = tmp_path / "docker.txt"
        _recording_docker(fakebin, record)
        rig = _env_rig({"ENABLE_PROMPT_CACHING_1H": "1"})
        env, workplace, _staging = _mcp_turn_env(tmp_path, Isolation(container="img"))

        run_harness(rig, "P", workplace, env=env)

        argv = _wrapped_argv(record)
        at = argv.index("ENABLE_PROMPT_CACHING_1H=1")
        assert argv[at - 1] == "-e"
        assert at < argv.index("img")

    def test_it_rides_alongside_the_mcp_idiom_env(self, tmp_path, fakebin):
        record = tmp_path / "sudo-both.txt"
        _recording_sudo(fakebin, record)
        rig = _mcp_rig(tmp_path, "opencode")
        rig.env = {"ENABLE_PROMPT_CACHING_1H": "1"}
        env, workplace, staging = _mcp_turn_env(tmp_path, Isolation(run_as="agent-x"))

        run_harness(rig, "P", workplace, env=env)

        config = staging.parent / "mcp" / "mcp-opencode.json"
        argv = _wrapped_argv(record)
        assert argv[9] == "2"
        assert set(argv[10:12]) == {
            "ENABLE_PROMPT_CACHING_1H=1", f"OPENCODE_CONFIG={config}"
        }

    def test_the_mcp_idiom_wins_its_own_variable(self, tmp_path, fakebin):
        record = tmp_path / "sudo-clash.txt"
        _recording_sudo(fakebin, record)
        rig = _mcp_rig(tmp_path, "opencode")
        rig.env = {"OPENCODE_CONFIG": "/theirs"}
        env, workplace, staging = _mcp_turn_env(tmp_path, Isolation(run_as="agent-x"))

        run_harness(rig, "P", workplace, env=env)

        config = staging.parent / "mcp" / "mcp-opencode.json"
        argv = _wrapped_argv(record)
        # r4t's own per-turn injection is not something a rig knob can unseat.
        assert argv[9] == "1"
        assert argv[10] == f"OPENCODE_CONFIG={config}"
        assert env["OPENCODE_CONFIG"] == str(config)

    def test_a_bare_rig_asks_the_wrapper_for_nothing(self, tmp_path, fakebin):
        record = tmp_path / "sudo-bare.txt"
        _recording_sudo(fakebin, record)
        env, workplace, staging = _mcp_turn_env(tmp_path, Isolation(run_as="agent-x"))

        run_harness(_env_rig({}), "P", workplace, env=env)

        assert _wrapped_argv(record)[6:] == [
            "_", str(staging), str(workplace), "0", "claude", "-p", "P",
        ]


class TestStatusRowRendering:
    def test_isolation_tag(self):
        from r4t import _isolation_tag

        assert _isolation_tag(Isolation(run_as="agent-x")) == "[user:agent-x]"
        assert _isolation_tag(Isolation(container="img:1")) == "[container:img:1]"
        assert _isolation_tag(Isolation()) == ""

    def test_status_header_shows_the_org_boundary(self, r4t_home, tmp_path, fake_harness, capsys):
        # The badge is one org-level line now, not a per-rig tag.
        from r4t import main as r4t_main

        org_dir = tmp_path / "iso-repo"
        org_dir.mkdir()
        (org_dir / "ROSTER.md").write_text(ROSTER, encoding="utf-8")
        (org_dir / ORG_CONFIG_NAME).write_text(
            json.dumps({"run_as": "agent-x"}), encoding="utf-8"
        )
        state.stamp_root(NODE, org_dir)
        cfg = _iso_config(tmp_path, fake_harness)
        rc = r4t_main(["status", "--node", NODE, "--rig-config", str(cfg)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "isolation: [user:agent-x]" in out
