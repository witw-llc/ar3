"""`ar3 tool browser --node NAME secret list|set|rm`, through the public `ar3` entry in a subprocess.

Every test runs against a temporary registry and home. The sentinel stands for a password: it must reach the
seat's secret store and appear nowhere else.
"""
import json
import os
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
SENTINEL = "s3ntinel-PASSWORD-value-9f2c"
ORIGIN = "https://app.example.com"


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "web-root"
    root.mkdir()
    a8s_home = tmp_path / "a8s"
    a8s_home.mkdir()
    node = {
        "root": str(root),
        "definition": str(REPO / "apps/a8s/definitions/tool-browser.json"),
        "vars": {"ALLOW": "iris"},
    }
    shell = {
        "root": str(root),
        "definition": str(REPO / "apps/a8s/definitions/tool-command-shell.json"),
        "vars": {"ALLOW": "iris"},
    }
    (a8s_home / "a8s.json").write_text(
        json.dumps({"agents": {"web1": node, "shell1": shell}, "aliases": {}, "namespaces": {}})
    )
    xdg = tmp_path / "xdg"
    env = {
        **os.environ,
        "A8S_HOME": str(a8s_home),
        "HOME": str(tmp_path / "home"),
        "XDG_DATA_HOME": str(xdg),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "AR3_TEST_SENTINEL": SENTINEL,
        "AR3_BROWSER_CHROME": str(tmp_path / "no-chrome"),
    }
    for name in ("AR3_BROWSER_SEAT", "TELL_OUTBOX_DIR"):
        env.pop(name, None)
    return env, xdg / "ar3" / "browser"


def spawn(env, argv, stdin=None, text=True):
    """A real process, started with Popen: the suite's guard forbids `subprocess.run`."""
    proc = subprocess.Popen(
        argv, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text
    )
    out, err = proc.communicate(stdin if stdin is not None else ("" if text else b""), timeout=60)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def ar3(env, *args, stdin=None):
    return spawn(env, [sys.executable, str(REPO / "apps/ar3/cli.py"), "tool", "browser", *args], stdin)


def stored(seats, seat="web1"):
    return json.loads((seats / seat / "secrets.json").read_text())["secrets"]


def clean(run):
    assert SENTINEL not in run.stdout and SENTINEL not in run.stderr


def test_set_reads_the_value_from_stdin_and_list_prints_names_only(world):
    env, seats = world
    run = ar3(env, "--node", "web1", "secret", "set", "PASSWORD", "-", "--origin", ORIGIN, stdin=SENTINEL)
    assert run.returncode == 0, run.stderr
    clean(run)
    assert stored(seats)["PASSWORD"] == {"value": SENTINEL, "origins": [ORIGIN]}
    assert oct((seats / "web1" / "secrets.json").stat().st_mode & 0o777) == "0o600"
    listed = ar3(env, "--node", "web1", "secret", "list")
    assert listed.returncode == 0, listed.stderr
    clean(listed)
    assert listed.stdout.split() == ["PASSWORD", ORIGIN]


def test_the_node_option_goes_before_secret_and_a_late_one_is_refused_without_storing(world):
    env, seats = world
    run = ar3(env, "secret", "set", "PASSWORD", "-", "--origin", ORIGIN, "--node", "web1", stdin=SENTINEL)
    assert run.returncode == 2 and "--node" in run.stderr
    clean(run)
    assert not seats.exists()


def test_a_trailing_newline_is_dropped_and_an_inner_one_kept(world):
    env, seats = world
    ar3(env, "--node", "web1", "secret", "set", "PASSWORD", "-", "--origin", ORIGIN, stdin="ab\ncd\n")
    assert stored(seats)["PASSWORD"]["value"] == "ab\ncd"


def test_setting_an_existing_name_again_keeps_its_origins(world):
    env, seats = world
    ar3(env, "--node", "web1", "secret", "set", "PASSWORD", "-", "--origin", ORIGIN, "--origin", "https://b.example.org", stdin="first-value")
    run = ar3(env, "--node", "web1", "secret", "set", "PASSWORD", "-", stdin=SENTINEL)
    assert run.returncode == 0, run.stderr
    clean(run)
    assert stored(seats)["PASSWORD"] == {"value": SENTINEL, "origins": [ORIGIN, "https://b.example.org"]}


def test_a_new_name_needs_an_origin_and_the_refusal_gives_an_example(world):
    env, seats = world
    run = ar3(env, "--node", "web1", "secret", "set", "PASSWORD", "-", stdin=SENTINEL)
    assert run.returncode == 1
    clean(run)
    assert "--origin https://app.example.com" in run.stderr
    assert not (seats / "web1" / "secrets.json").exists()


@pytest.mark.parametrize("value", ["", "\n", "abc"])
def test_an_empty_or_short_value_is_refused_and_stores_nothing(world, value):
    env, seats = world
    run = ar3(env, "--node", "web1", "secret", "set", "PASSWORD", "-", "--origin", ORIGIN, stdin=value)
    assert run.returncode == 1
    assert "at least" in run.stderr
    assert not (seats / "web1" / "secrets.json").exists()


def test_input_that_is_not_utf8_is_refused_without_a_traceback(world):
    env, seats = world
    run = spawn(
        env,
        [sys.executable, str(REPO / "apps/ar3/cli.py"), "tool", "browser", "--node", "web1",
         "secret", "set", "PASSWORD", "-", "--origin", ORIGIN],
        stdin=b"abcd\xff\xfe" + SENTINEL.encode(), text=False,
    )
    assert run.returncode == 1
    assert b"Traceback" not in run.stderr and SENTINEL.encode() not in run.stderr + run.stdout
    assert b"UTF-8" in run.stderr


def test_a_value_given_as_an_argument_is_refused_and_never_echoed(world):
    env, seats = world
    for args in (
        ("secret", "set", "PASSWORD", SENTINEL, "--origin", ORIGIN),
        ("secret", "set", "PASSWORD", "-", SENTINEL, "--origin", ORIGIN),
        ("secret", "set", "PASSWORD", "-", "--origin", ORIGIN, "--value", SENTINEL),
    ):
        run = ar3(env, "--node", "web1", *args, stdin="")
        assert run.returncode != 0
        clean(run)
    assert not (seats / "web1" / "secrets.json").exists()


def test_a_bad_name_is_a_failure_not_a_traceback(world):
    env, _seats = world
    run = ar3(env, "--node", "web1", "secret", "set", "lower", "-", "--origin", ORIGIN, stdin=SENTINEL)
    assert run.returncode == 1 and "Traceback" not in run.stderr
    clean(run)


def test_rm_removes_a_name_and_a_missing_name_exits_1(world):
    env, seats = world
    ar3(env, "--node", "web1", "secret", "set", "PASSWORD", "-", "--origin", ORIGIN, stdin=SENTINEL)
    assert ar3(env, "--node", "web1", "secret", "rm", "PASSWORD").returncode == 0
    assert stored(seats) == {}
    missing = ar3(env, "--node", "web1", "secret", "rm", "PASSWORD")
    assert missing.returncode == 1 and "not set" in missing.stderr


def test_the_secret_of_one_node_is_not_the_secret_of_another(world):
    env, seats = world
    ar3(env, "--node", "web1", "secret", "set", "PASSWORD", "-", "--origin", ORIGIN, stdin=SENTINEL)
    assert not (seats / "shell1").exists()
    assert (seats / "web1" / "secrets.json").exists()


def test_secret_needs_a_node_and_a_known_verb(world):
    env, seats = world
    run = ar3(env, "secret", "list")
    assert run.returncode == 2 and "--node" in run.stderr
    run = ar3(env, "--node", "web1", "secret")
    assert run.returncode == 2 and "list, set, rm" in run.stderr
    run = ar3(env, "--node", "web1", "secret", "show")
    assert run.returncode == 2
    run = ar3(env, "--node", "nope", "secret", "list")
    assert run.returncode == 2 and "no node named" in run.stderr
    assert not seats.exists()


def test_a_node_that_is_not_a_browser_is_refused(world):
    env, seats = world
    run = ar3(env, "--node", "shell1", "secret", "list")
    assert run.returncode == 2 and "not tool-browser" in run.stderr


def test_secret_admin_starts_no_browser(world, tmp_path):
    env, _seats = world
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    called = tmp_path / "called"
    stub = stub_dir / "playwright-cli"
    stub.write_text(f"#!/bin/sh\necho x >> {called}\nexit 1\n")
    stub.chmod(0o755)
    env = {**env, "PATH": f"{stub_dir}{os.pathsep}{env['PATH']}"}
    ar3(env, "--node", "web1", "secret", "set", "PASSWORD", "-", "--origin", ORIGIN, stdin=SENTINEL)
    ar3(env, "--node", "web1", "secret", "list")
    ar3(env, "--node", "web1", "secret", "rm", "PASSWORD")
    assert not called.exists()


def test_a_routed_script_cannot_reach_the_secret_verbs(world):
    """A message to the node is a script; `secret` is not one of its commands, so nothing is stored."""
    env, seats = world
    run = ar3(env, "--node", "web1", "--", "secret set PASSWORD --origin " + ORIGIN)
    assert run.returncode == 1
    run = ar3(env, "--node", "web1", "--", "secret list\nsecret set PASSWORD")
    assert run.returncode == 1
    assert not (seats / "web1" / "secrets.json").exists()


def test_the_argv_of_the_run_holds_no_value(world, monkeypatch):
    env, _seats = world
    import cli

    seen = []

    class Done:
        def wait(self):
            return 0

    monkeypatch.setattr(cli.subprocess, "Popen", lambda argv, **kw: seen.append((argv, kw)) or Done())
    monkeypatch.setattr(sys, "stdin", open(os.devnull))
    for key in ("A8S_HOME", "XDG_DATA_HOME", "HOME"):
        monkeypatch.setenv(key, env[key])
    code = cli.main(["tool", "browser", "--node", "web1", "secret", "set", "PASSWORD", "-", "--origin", ORIGIN])
    assert code == 0
    argv, kwargs = seen[0]
    assert argv[-5:] == ["-", "--origin", ORIGIN, "--seat", "web1"]
    assert SENTINEL not in " ".join(argv)
    assert "stdin" not in kwargs or kwargs["stdin"] is None
