"""A node's own environment variables: `a8s vars <name> set env.<NAME> <value>`.

Only an `env.`-prefixed key reaches the child's environment, NAME keeps its
case, and every other var stays argv interpolation. The wake tests run a real
subprocess that dumps its own environment, because the contract is what the
child sees, not what a helper returns.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from commands import (
    _refuse_unresolved_mailboxes,
    _warn_unresolvable_harnesses,
    cmd_add,
    cmd_vars,
    parse_option_tokens,
)
from core import (
    TELL_OUTBOX_DIR_ENV,
    Participant,
    agent_log_path,
    inbox_dir,
    trash_dir,
)
from daemon import _wake_env, attached_loop
from definitions import (
    UndefinedVarsError,
    build_command,
    load_agent_env,
    load_agent_vars,
    validate_var_name,
    wake_env,
    wake_path_repair,
    wake_path_source,
)
from mailbox import ensure_mailboxes
from registry import load_registry, save_registry
from ar3.ulid import new as new_ulid

DUMP_ENV = "import json,os,sys;json.dump(dict(os.environ),open(sys.argv[1],'w'))"


@pytest.fixture
def agent_root(fake_home, tmp_path):
    d = tmp_path / "x"
    d.mkdir()
    return d


def _add(agent_root, name="bob", *flags):
    assert cmd_add([name, str(agent_root), *flags]) == 0


class TestSetListUnset:
    def test_set_list_unset(self, fake_home, agent_root, capsys):
        _add(agent_root)
        capsys.readouterr()
        assert cmd_vars(["bob", "set", "env.OTEL_RESOURCE_ATTRIBUTES", "k=v"]) == 0
        assert capsys.readouterr().out == "bob: set env.OTEL_RESOURCE_ATTRIBUTES=k=v\n"
        assert load_registry()["bob"]["vars"] == {"env.OTEL_RESOURCE_ATTRIBUTES": "k=v"}
        assert load_agent_env("bob") == {"OTEL_RESOURCE_ATTRIBUTES": "k=v"}

        assert cmd_vars(["bob"]) == 0
        assert "env.OTEL_RESOURCE_ATTRIBUTES  k=v" in capsys.readouterr().out

        assert cmd_vars(["bob", "unset", "env.OTEL_RESOURCE_ATTRIBUTES"]) == 0
        assert "vars" not in load_registry()["bob"]
        assert load_agent_env("bob") == {}

    def test_the_prefix_is_case_insensitive_and_name_keeps_its_case(
        self, fake_home, agent_root
    ):
        _add(agent_root)
        assert cmd_vars(["bob", "set", "ENV.Mixed_Case", "a"]) == 0
        assert load_registry()["bob"]["vars"] == {"env.Mixed_Case": "a"}
        assert cmd_vars(["bob", "set", "Env.Mixed_Case", "b"]) == 0
        assert load_registry()["bob"]["vars"] == {"env.Mixed_Case": "b"}
        assert load_agent_env("bob") == {"Mixed_Case": "b"}

    def test_two_names_that_differ_only_in_case_are_two_variables(
        self, fake_home, agent_root
    ):
        _add(agent_root)
        assert cmd_vars(["bob", "set", "env.http_proxy", "lower"]) == 0
        assert cmd_vars(["bob", "set", "env.HTTP_PROXY", "upper"]) == 0
        assert load_agent_env("bob") == {"http_proxy": "lower", "HTTP_PROXY": "upper"}
        assert cmd_vars(["bob", "unset", "ENV.http_proxy"]) == 0
        assert load_agent_env("bob") == {"HTTP_PROXY": "upper"}

    def test_unset_matches_the_name_exactly(self, fake_home, agent_root, capsys):
        _add(agent_root)
        assert cmd_vars(["bob", "set", "env.Foo", "x"]) == 0
        capsys.readouterr()
        assert cmd_vars(["bob", "unset", "env.FOO"]) == 1
        assert "no var named" in capsys.readouterr().err
        assert load_agent_env("bob") == {"Foo": "x"}

    def test_the_listing_shows_plain_vars_then_env_vars(self, fake_home, agent_root, capsys):
        _add(agent_root)
        assert cmd_vars(["bob", "set", "model", "qwen3.6"]) == 0
        assert cmd_vars(["bob", "set", "env.ALPHA", "1"]) == 0
        capsys.readouterr()
        assert cmd_vars(["bob"]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert [ln.split()[0] for ln in lines] == ["MODEL", "env.ALPHA"]

    def test_a_plain_var_named_env_is_still_a_plain_var(self, fake_home, agent_root):
        _add(agent_root)
        assert cmd_vars(["bob", "set", "env", "x"]) == 0
        assert load_registry()["bob"]["vars"] == {"ENV": "x"}
        assert load_agent_vars("bob") == {"ENV": "x"}
        assert load_agent_env("bob") == {}

    def test_add_sets_env_vars_with_either_spelling(self, fake_home, agent_root):
        _add(agent_root, "bob", "--model=m", "--env.Foo=1", "--ENV.BAR", "two=2")
        assert load_registry()["bob"]["vars"] == {
            "MODEL": "m",
            "env.Foo": "1",
            "env.BAR": "two=2",
        }

    def test_add_refuses_an_unusable_env_name(self, fake_home, agent_root, capsys):
        assert cmd_add(["bob", str(agent_root), "--env.A-B=1"]) == 2
        assert "not a usable environment variable name" in capsys.readouterr().err
        assert "bob" not in load_registry()

    def test_the_option_parser_keeps_an_env_name_as_written(self):
        assert parse_option_tokens(["--env.My-Name=x"]) == {"env.My-Name": "x"}
        assert parse_option_tokens(["--base-url=x"]) == {"base_url": "x"}
        with pytest.raises(ValueError, match="duplicate option: --env.A"):
            parse_option_tokens(["--env.A=1", "--env.A=2"])


class TestValidation:
    @pytest.mark.parametrize(
        "key", ["env.", "env.A-B", "env.A B", "env.1ABC", "env.A=B", "env.A\0B", "env.É"]
    )
    def test_an_unusable_name_is_refused(self, fake_home, agent_root, capsys, key):
        _add(agent_root)
        capsys.readouterr()
        assert cmd_vars(["bob", "set", key, "x"]) == 2
        err = capsys.readouterr().err
        assert err.startswith("a8s: env name is not a usable environment variable name")
        assert "vars" not in load_registry()["bob"]

    @pytest.mark.parametrize(
        "name",
        [
            "TELL_OUTBOX_DIR",
            "TELL_FILE_MAX",
            "tell_outbox_dir",
            "A8S_TURN_RECIPIENT",
            "A8S_TURN_ENVELOPES",
            "A8S_TURN_ANYTHING_ELSE",
        ],
    )
    def test_a_routing_owned_name_is_refused_at_set_time(
        self, fake_home, agent_root, capsys, name
    ):
        _add(agent_root)
        capsys.readouterr()
        assert cmd_vars(["bob", "set", f"env.{name}", "x"]) == 2
        err = capsys.readouterr().err
        assert err.startswith(f"a8s: {name} is set by a8s routing")
        assert "vars" not in load_registry()["bob"]

    def test_add_refuses_a_routing_owned_name(self, fake_home, agent_root, capsys):
        assert cmd_add(["bob", str(agent_root), "--env.TELL_OUTBOX_DIR=/x"]) == 2
        assert "set by a8s routing" in capsys.readouterr().err
        assert "bob" not in load_registry()

    def test_a_name_that_resembles_a_routing_name_is_allowed(self):
        assert validate_var_name("env.TELL_OUTBOX") == "env.TELL_OUTBOX"
        assert validate_var_name("env.A8S_TURN") == "env.A8S_TURN"

    def test_a_malformed_registry_name_aborts_the_wake_not_the_process(self):
        with pytest.raises(ValueError, match="not usable as a variable"):
            from definitions import env_vars

            env_vars({"env.A=B": "x"})


class TestNeverInterpolated:
    def test_an_env_var_does_not_satisfy_a_placeholder(self, fake_home, agent_root):
        _add(agent_root)
        assert cmd_vars(["bob", "set", "env.MODEL", "x"]) == 0
        defn = {"invoke": ["tool", "--model", "$MODEL", "$MESSAGE"]}
        with pytest.raises(UndefinedVarsError):
            build_command(
                defn,
                {"from": "a", "to": "bob", "content": "hi"},
                agent_root,
                vars=load_agent_vars("bob"),
            )

    def test_an_optional_placeholder_stays_unset(self, fake_home, agent_root):
        _add(agent_root)
        assert cmd_vars(["bob", "set", "env.MODEL", "x"]) == 0
        defn = {"invoke": ["tool", "--model=$MODEL?", "$MESSAGE"]}
        argv = build_command(
            defn,
            {"from": "a", "to": "bob", "content": "hi"},
            agent_root,
            vars=load_agent_vars("bob"),
        )
        assert argv == ["tool", "hi"]

    def test_an_env_var_is_not_an_argv_var_even_when_unreferenced(self, fake_home, agent_root):
        _add(agent_root, "bob", "--model=m", "--env.Other=1")
        assert load_agent_vars("bob") == {"MODEL": "m"}
        argv = build_command(
            {"invoke": ["tool", "$MODEL", "$MESSAGE"]},
            {"from": "a", "to": "bob", "content": "hi"},
            agent_root,
            vars=load_agent_vars("bob"),
        )
        assert argv == ["tool", "m", "hi"]

    def test_an_env_var_does_not_resolve_a_mailbox_path_field(
        self, fake_home, agent_root, tmp_path, capsys
    ):
        path = tmp_path / "seat.json"
        path.write_text(json.dumps({"invoke": ["x", "$MESSAGE"], "outbox_dir": ".out-$SEAT"}))
        assert cmd_add(["seat", str(agent_root), str(path)]) == 0
        assert cmd_vars(["seat", "set", "env.SEAT", "a"]) == 0
        capsys.readouterr()
        assert _refuse_unresolved_mailboxes(["seat"]) is True
        assert "$SEAT" in capsys.readouterr().err

    def test_an_env_var_does_not_trip_the_live_mailbox_guard(
        self, fake_home, agent_root, tmp_path, monkeypatch
    ):
        path = tmp_path / "seat.json"
        path.write_text(json.dumps({"invoke": ["x", "$MESSAGE"], "outbox_dir": ".out-$SEAT"}))
        assert cmd_add(["seat", str(agent_root), str(path), "--seat=a"]) == 0
        monkeypatch.setattr("commands._read_handler_pid", lambda name: 4242)
        assert cmd_vars(["seat", "set", "SEAT", "b"]) == 1
        assert cmd_vars(["seat", "set", "env.SEAT", "b"]) == 0
        assert cmd_vars(["seat", "unset", "env.SEAT"]) == 0
        assert load_registry()["seat"]["vars"] == {"SEAT": "a"}


class TestWakeEnvLayers:
    def test_the_node_layer_sits_above_definition_env(self, fake_home):
        env = wake_env({"env": {"A": "def", "B": "def"}}, {"A": "node"})
        assert env == {"A": "node", "B": "def"}

    def test_a_node_path_replaces_the_machine_wake_path(self, fake_home, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("A8S_WAKE_PATH", "/machine/bin:/usr/bin")
        assert wake_env({"invoke": ["x"]}, {"PATH": "/node/bin"}) == {"PATH": "/node/bin"}

    def test_a_node_path_replaces_a_definition_path(self, fake_home):
        env = wake_env({"env": {"PATH": "/def/bin"}}, {"PATH": "/node/bin"})
        assert env == {"PATH": "/node/bin"}

    def test_wake_path_still_fills_in_beside_other_node_vars(self, fake_home, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("A8S_WAKE_PATH", "/machine/bin:/usr/bin")
        assert wake_env({"invoke": ["x"]}, {"LANG": "C"}) == {
            "PATH": "/machine/bin:/usr/bin",
            "LANG": "C",
        }

    def test_the_path_source_and_repair_name_the_node_var(self, fake_home):
        definition = {"env": {"PATH": "/def/bin"}}
        assert wake_path_source(definition, {"PATH": "/n"}) == "the node's `env.PATH` var"
        assert wake_path_source(definition) == "`definition.env`"
        repair = wake_path_repair("bob", definition, "claude", {"PATH": "/n"})
        assert "`a8s vars bob set env.PATH <value>`" in repair
        assert "definition.env" not in repair

    def test_the_daemon_layer_reads_the_registry(self, fake_home, agent_root):
        _add(agent_root)
        assert cmd_vars(["bob", "set", "env.Mixed_Case", "node"]) == 0
        p = Participant("bob", agent_root)
        env = _wake_env(p, {"env": {"Mixed_Case": "def", "FROM_DEF": "d"}})
        assert env["Mixed_Case"] == "node"
        assert env["FROM_DEF"] == "d"

    def test_routing_wins_over_a_hand_written_registry_entry(self, fake_home, agent_root):
        _add(agent_root)
        reg = load_registry()
        reg["bob"]["vars"] = {
            f"env.{TELL_OUTBOX_DIR_ENV}": "/somewhere/else",
            "env.A8S_TURN_RECIPIENT": "somebody-else",
        }
        save_registry(reg)
        p = Participant("bob", agent_root, outbox=agent_root / "mail" / ".outbox")
        env = _wake_env(p, {"invoke": ["x"]})
        assert env[TELL_OUTBOX_DIR_ENV] == str(p.outbox_path().resolve())
        assert env["A8S_TURN_RECIPIENT"] == "bob"


def _register(tmp_path: Path, definition: dict, vars_map: dict | None = None) -> Participant:
    root = tmp_path / "a"
    root.mkdir(exist_ok=True)
    defp = tmp_path / "def.json"
    defp.write_text(json.dumps(definition))
    entry: dict = {"root": str(root), "definition": str(defp)}
    if vars_map:
        entry["vars"] = vars_map
    save_registry({"A": entry})
    p = Participant("A", root)
    ensure_mailboxes(p)
    return p


def _dump_definition(out: Path, **extra) -> dict:
    return {"invoke": ["$PYTHON", "-c", DUMP_ENV, str(out)], **extra}


def _queue(content: str = "hello") -> None:
    msg_id = new_ulid()
    envelope = {
        "id": msg_id,
        "date": "2026-04-29T12:00:00Z",
        "from": "Y",
        "to": "A",
        "content": content,
        "files": [],
    }
    (inbox_dir("A") / f"{msg_id}.json").write_text(json.dumps(envelope))


def _wake_once(out: Path) -> dict:
    out.unlink(missing_ok=True)
    _queue()
    attached_loop(["A"], 0.05, single_pass=True)
    assert out.is_file(), agent_log_path("A").read_text()
    return json.loads(out.read_text())


class TestTheWakeSubprocess:
    def test_a_node_env_var_reaches_the_child_with_its_case(self, fake_home, tmp_path):
        out = tmp_path / "env.json"
        _register(
            tmp_path,
            _dump_definition(out),
            {"env.Mixed_Case": "kept", "env.OTEL_RESOURCE_ATTRIBUTES": "k=v"},
        )
        env = _wake_once(out)
        assert env["Mixed_Case"] == "kept"
        assert env["OTEL_RESOURCE_ATTRIBUTES"] == "k=v"

    def test_a_plain_var_never_reaches_the_child(self, fake_home, tmp_path):
        out = tmp_path / "env.json"
        _register(
            tmp_path,
            _dump_definition(out),
            {"MODEL": "m", "env.Declared": "yes"},
        )
        env = _wake_once(out)
        assert env["Declared"] == "yes"
        assert "MODEL" not in env
        assert not any(k.lower().startswith("env.") for k in env)

    def test_a_node_var_beats_definition_env_and_routing_beats_both(
        self, fake_home, tmp_path
    ):
        out = tmp_path / "env.json"
        p = _register(
            tmp_path,
            _dump_definition(
                out,
                env={"SHARED": "definition", "ONLY_DEF": "d", TELL_OUTBOX_DIR_ENV: "/elsewhere"},
            ),
            {"env.SHARED": "node", "env.A8S_TURN_RECIPIENT": "somebody-else"},
        )
        env = _wake_once(out)
        assert env["SHARED"] == "node"
        assert env["ONLY_DEF"] == "d"
        assert env[TELL_OUTBOX_DIR_ENV] == str(p.outbox_path().resolve())
        assert env["A8S_TURN_RECIPIENT"] == "A"

    def test_a_node_path_replaces_wake_path_in_the_child(
        self, fake_home, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("A8S_WAKE_PATH", "/machine/bin:/usr/bin")
        out = tmp_path / "env.json"
        _register(tmp_path, _dump_definition(out), {"env.PATH": "/node/bin"})
        assert _wake_once(out)["PATH"] == "/node/bin"

    def test_a_changed_var_lands_on_the_next_wake_with_no_restart(self, fake_home, tmp_path):
        out = tmp_path / "env.json"
        _register(tmp_path, _dump_definition(out), {"env.Stage": "one"})
        assert _wake_once(out)["Stage"] == "one"

        assert cmd_vars(["A", "set", "env.Stage", "two"]) == 0
        assert _wake_once(out)["Stage"] == "two"

        assert cmd_vars(["A", "unset", "env.Stage"]) == 0
        assert "Stage" not in _wake_once(out)

    def test_a_malformed_registry_name_dead_letters_the_message(self, fake_home, tmp_path):
        out = tmp_path / "env.json"
        _register(tmp_path, _dump_definition(out), {"env.A=B": "x"})
        _queue()
        attached_loop(["A"], 0.05, single_pass=True)
        assert not out.exists()
        log = agent_log_path("A").read_text()
        assert "wake aborted" in log
        assert "1 dead letter(s) left in trash" in log
        assert not list(inbox_dir("A").glob("*.json"))


class TestHarnessProbe:
    def _definition(self, tmp_path: Path) -> Path:
        path = tmp_path / "probe.json"
        path.write_text(json.dumps({"invoke": ["a8s-probe-harness", "-p", "$MESSAGE"]}))
        return path

    def test_a_node_path_that_holds_the_harness_ends_the_warning(
        self, fake_home, agent_root, tmp_path, capsys
    ):
        harness_dir = tmp_path / "bin"
        harness_dir.mkdir()
        exe = harness_dir / "a8s-probe-harness"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        assert cmd_add(["probe", str(agent_root), str(self._definition(tmp_path))]) == 0
        capsys.readouterr()

        _warn_unresolvable_harnesses(["probe"])
        assert "is not on the PATH" in capsys.readouterr().err

        assert cmd_vars(["probe", "set", "env.PATH", str(harness_dir)]) == 0
        capsys.readouterr()
        _warn_unresolvable_harnesses(["probe"])
        assert capsys.readouterr().err == ""

    def test_a_node_path_that_misses_the_harness_names_the_var(
        self, fake_home, agent_root, tmp_path, capsys
    ):
        assert cmd_add(["probe", str(agent_root), str(self._definition(tmp_path))]) == 0
        assert cmd_vars(["probe", "set", "env.PATH", str(tmp_path)]) == 0
        capsys.readouterr()
        _warn_unresolvable_harnesses(["probe"])
        err = capsys.readouterr().err
        assert "`a8s vars probe set env.PATH <value>`" in err
        assert "a8s restart" not in err

    def test_exit_127_names_the_node_var_as_the_source(self, fake_home, tmp_path):
        from daemon import _settle_wake

        p = _register(
            tmp_path,
            {"invoke": ["x", "$MESSAGE"], "env": {"PATH": "/def/bin"}},
            {"env.PATH": "/node/bin"},
        )
        msg_id = new_ulid()
        trash_dir("A").mkdir(parents=True, exist_ok=True)
        trashed = trash_dir("A") / f"{msg_id}.json"
        trashed.write_text(json.dumps({
            "id": msg_id, "from": "Y", "to": "A", "content": "x", "files": [],
        }))
        _settle_wake(p, [trashed], 127)
        log = agent_log_path("A").read_text()
        assert "came from the node's `env.PATH` var: /node/bin" in log


class TestNodeSecretVars:
    """`a8s vars <name> set|unset secret.<NAME>`: stored in `0600` `secrets.json`,
    redacted in `a8s vars` and `a8s health`, never expanded in argv or path fields,
    and injected into the wake environment above `env.<NAME>` and below routing."""

    def test_set_stdin_list_update_unset_and_mode_0600(
        self, fake_home, agent_root, monkeypatch, capsys
    ):
        import io
        import os
        from core import secrets_config_path
        from definitions import load_agent_secrets

        _add(agent_root, "scholar")
        capsys.readouterr()

        monkeypatch.setattr("sys.stdin", io.StringIO("sk-first-secret-9001\n"))
        assert cmd_vars(["scholar", "set", "secret.OPENAI_API_KEY", "-"]) == 0
        out, err = capsys.readouterr()
        assert out == "scholar: set secret.OPENAI_API_KEY\n"
        assert "sk-first-secret-9001" not in out + err
        assert "vars" not in load_registry()["scholar"]
        if os.name == "posix":
            assert (secrets_config_path().stat().st_mode & 0o777) == 0o600
        assert load_agent_secrets("scholar") == {"OPENAI_API_KEY": "sk-first-secret-9001"}
        assert load_agent_env("scholar") == {"OPENAI_API_KEY": "sk-first-secret-9001"}

        assert cmd_vars(["scholar"]) == 0
        listed = capsys.readouterr().out
        assert listed == "secret.OPENAI_API_KEY  [secret]\n"
        assert "sk-first-secret-9001" not in listed

        monkeypatch.setattr("sys.stdin", io.StringIO("sk-updated-secret-9002\r\n"))
        assert cmd_vars(["scholar", "set", "SECRET.OPENAI_API_KEY", "-"]) == 0
        out, err = capsys.readouterr()
        assert out == "scholar: updated secret.OPENAI_API_KEY\n"
        assert "sk-updated-secret-9002" not in out + err
        assert load_agent_secrets("scholar") == {"OPENAI_API_KEY": "sk-updated-secret-9002"}

        assert cmd_vars(["scholar", "unset", "secret.openai_api_key"]) == 1
        assert "no var named" in capsys.readouterr().err
        assert cmd_vars(["scholar", "unset", "Secret.OPENAI_API_KEY"]) == 0
        assert capsys.readouterr().out == "scholar: unset secret.OPENAI_API_KEY\n"
        assert load_agent_secrets("scholar") == {}
        assert json.loads(secrets_config_path().read_text())["nodes"] == {}

    def test_set_prompts_with_getpass_at_tty_when_dash_omitted(
        self, fake_home, agent_root, monkeypatch, capsys
    ):
        from definitions import load_agent_secrets

        class FakeTty:
            def isatty(self):
                return True

        _add(agent_root, "scholar")
        capsys.readouterr()
        asked = []
        monkeypatch.setattr("sys.stdin", FakeTty())
        monkeypatch.setattr(
            "commands.getpass.getpass",
            lambda prompt="": asked.append(prompt) or "sk-tty-secret-7777",
        )
        assert cmd_vars(["scholar", "set", "secret.OPENAI_API_KEY"]) == 0
        out, err = capsys.readouterr()
        assert asked == ["Value for OPENAI_API_KEY: "]
        assert "sk-tty-secret-7777" not in out + err
        assert load_agent_secrets("scholar") == {"OPENAI_API_KEY": "sk-tty-secret-7777"}

    def test_argv_and_add_flag_values_are_refused_without_echoing_the_secret(
        self, fake_home, agent_root, capsys
    ):
        sentinel = "sk-LEAK-SENTINEL-4242"
        assert cmd_add(["bad1", str(agent_root), f"--secret.OPENAI_API_KEY={sentinel}"]) == 2
        assert sentinel not in (capsys.readouterr().err)
        assert "bad1" not in load_registry()

        assert cmd_add(["bad2", str(agent_root), "--SECRET.OPENAI_API_KEY", sentinel]) == 2
        assert sentinel not in (capsys.readouterr().err)

        _add(agent_root, "bob")
        capsys.readouterr()
        assert cmd_vars(["bob", "set", "secret.OPENAI_API_KEY", sentinel]) == 2
        out, err = capsys.readouterr()
        assert sentinel not in out + err
        assert "never passed on the command line" in err

        assert cmd_vars(["bob", "set", "secret.OPENAI_API_KEY", "-", sentinel]) == 2
        out, err = capsys.readouterr()
        assert sentinel not in out + err

    @pytest.mark.parametrize(
        "bad_key",
        [
            "secret.",
            "secret.A-B",
            "secret.1ABC",
            "secret.PATH",
            "secret.path",
            "secret.TELL_OUTBOX_DIR",
            "secret.A8S_TURN_FILES",
        ],
    )
    def test_invalid_routing_and_path_secret_names_are_refused_before_reading_input(
        self, fake_home, agent_root, monkeypatch, capsys, bad_key
    ):
        class Boom:
            def isatty(self):
                return False

            def read(self, *a):
                pytest.fail("stdin was read for an invalid secret name")

        _add(agent_root, "bob")
        capsys.readouterr()
        monkeypatch.setattr("sys.stdin", Boom())
        assert cmd_vars(["bob", "set", bad_key, "-"]) == 2
        assert capsys.readouterr().err

    @pytest.mark.parametrize("bad_stdin", ["", "\n", "\r\n", "abc\0def\n"])
    def test_empty_or_nul_secret_values_are_refused(
        self, fake_home, agent_root, monkeypatch, capsys, bad_stdin
    ):
        import io

        _add(agent_root, "bob")
        capsys.readouterr()
        monkeypatch.setattr("sys.stdin", io.StringIO(bad_stdin))
        assert cmd_vars(["bob", "set", "secret.TOKEN", "-"]) == 2
        assert "abc" not in capsys.readouterr().err

    def test_secret_var_never_expands_in_argv_or_mailbox_paths_and_wins_over_env_in_wake(
        self, fake_home, tmp_path, monkeypatch
    ):
        import io
        from commands import cmd_remove

        out = tmp_path / "env.json"
        p = _register(
            tmp_path,
            _dump_definition(out, env={"SHARED": "def", "DEF_ONLY": "1"}),
            {"MODEL": "m", "env.SHARED": "env-layer"},
        )
        monkeypatch.setattr("sys.stdin", io.StringIO("secret-layer-val\n"))
        assert cmd_vars(["A", "set", "secret.SHARED", "-"]) == 0
        monkeypatch.setattr("sys.stdin", io.StringIO("only-secret-val\n"))
        assert cmd_vars(["A", "set", "secret.OPENAI_API_KEY", "-"]) == 0

        # Never satisfies $OPENAI_API_KEY in invoke argv.
        with pytest.raises(UndefinedVarsError):
            build_command(
                {"invoke": ["tool", "$OPENAI_API_KEY", "$MESSAGE"]},
                {"from": "x", "to": "A", "content": "hi"},
                p.root,
                vars=load_agent_vars("A"),
            )

        child_env = _wake_once(out)
        assert child_env["SHARED"] == "secret-layer-val"
        assert child_env["OPENAI_API_KEY"] == "only-secret-val"
        assert child_env["DEF_ONLY"] == "1"
        assert "MODEL" not in child_env
        assert not any(k.lower().startswith("secret.") for k in child_env)

        # Removing the node wipes its stored secrets.
        assert cmd_remove(["A"]) == 0
        from core import secrets_config_path

        assert json.loads(secrets_config_path().read_text())["nodes"] == {}

    def test_corrupt_secrets_json_refuses_writes_and_aborts_wake_without_leaking(
        self, fake_home, tmp_path, monkeypatch, capsys
    ):
        import io
        from core import secrets_config_path

        out = tmp_path / "env.json"
        _register(tmp_path, _dump_definition(out))
        secrets_config_path().parent.mkdir(parents=True, exist_ok=True)
        raw_corrupt = "{not-json sk-DO-NOT-LEAK-8888"
        secrets_config_path().write_text(raw_corrupt, encoding="utf-8")

        monkeypatch.setattr("sys.stdin", io.StringIO("sk-new-value\n"))
        assert cmd_vars(["A", "set", "secret.TOKEN", "-"]) == 1
        err = capsys.readouterr().err
        assert "secrets.json unreadable" in err
        assert "sk-DO-NOT-LEAK-8888" not in err
        assert secrets_config_path().read_text(encoding="utf-8") == raw_corrupt

        _queue()
        attached_loop(["A"], 0.05, single_pass=True)
        assert not out.exists()
        log = agent_log_path("A").read_text()
        assert "wake aborted" in log
        assert "sk-DO-NOT-LEAK-8888" not in log

    def test_node_health_lists_secret_names_hidden_and_never_prints_values(
        self, fake_home, agent_root, monkeypatch, capsys
    ):
        import io
        import txlog
        from commands import cmd_health

        txlog.log("RUN_START", detail="init")
        _add(agent_root, "bob", "--ALLOW=alice", "--env.PUBLIC_ENV=visible-in-vars")
        monkeypatch.setattr("sys.stdin", io.StringIO("sk-health-secret-5555\n"))
        assert cmd_vars(["bob", "set", "secret.OPENAI_API_KEY", "-"]) == 0
        capsys.readouterr()

        assert cmd_health(["bob"]) == 0
        out, err = capsys.readouterr()
        assert "secret (values hidden): OPENAI_API_KEY" in out
        assert "sk-health-secret-5555" not in out + err

    def test_registry_reconstruction_preserves_same_root_secrets_and_refuses_ambiguous_or_damaged_stores(
        self, fake_home, tmp_path, monkeypatch, capsys
    ):
        import io
        from ar3 import nodes as ar3_nodes
        from core import registry_path, secrets_config_path

        original = tmp_path / "original"
        original.mkdir()
        moved = tmp_path / "moved"
        moved.mkdir()
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir()

        assert cmd_add(["probe", str(original), "filedrop"]) == 0
        monkeypatch.setattr("sys.stdin", io.StringIO("synthetic-retained-token\n"))
        assert cmd_vars(["probe", "set", "secret.TOKEN", "-"]) == 0
        assert load_agent_env("probe")["TOKEN"] == "synthetic-retained-token"

        # Pre-v1 scorch-the-earth reset (rm a8s.json) followed by re-adding the
        # identical name and root preserves stored node secrets automatically.
        registry_path().unlink()
        capsys.readouterr()
        assert cmd_add(["probe", str(original), "filedrop"]) == 0
        assert load_agent_env("probe")["TOKEN"] == "synthetic-retained-token"
        assert ar3_nodes.node_secrets("probe") == {"TOKEN": "synthetic-retained-token"}

        # Re-adding at a different directory refuses without destroying credentials
        # until the operator passes --rebind-secrets or --drop-secrets.
        registry_path().unlink()
        capsys.readouterr()
        assert cmd_add(["probe", str(moved), "filedrop"]) == 1
        err = capsys.readouterr().err
        assert "--rebind-secrets" in err and "--drop-secrets" in err
        assert "probe" not in load_registry()
        assert "synthetic-retained-token" not in err

        # Contradictory flags fail closed with usage exit code 2.
        assert (
            cmd_add(
                ["probe", str(moved), "filedrop", "--rebind-secrets", "--drop-secrets"]
            )
            == 2
        )
        capsys.readouterr()

        # --rebind-secrets moves the retained credentials to the new directory
        # without re-entering any secret value.
        assert cmd_add(["probe", str(moved), "filedrop", "--rebind-secrets"]) == 0
        assert load_agent_env("probe")["TOKEN"] == "synthetic-retained-token"
        assert ar3_nodes.node_secrets("probe") == {"TOKEN": "synthetic-retained-token"}

        # Hand-editing a8s.json to a different root never leaks the former root's token.
        reg = load_registry()
        reg["probe"]["root"] = str(unrelated.resolve())
        save_registry(reg)
        with pytest.raises(ValueError, match="different directory"):
            load_agent_env("probe")
        assert ar3_nodes.node_secrets("probe") == {}

        # --drop-secrets discards the retained secrets cleanly for a replacement root.
        registry_path().unlink()
        capsys.readouterr()
        assert cmd_add(["probe", str(unrelated), "filedrop", "--drop-secrets"]) == 0
        assert load_agent_env("probe") == {}
        assert json.loads(secrets_config_path().read_text())["nodes"] == {}

        # A damaged sibling section in secrets.json fails closed during add AND read,
        # never registering a replacement that could inherit a former owner's token.
        registry_path().unlink()
        secrets_config_path().write_text(
            json.dumps(
                {
                    "remotes": [],
                    "services": {},
                    "nodes": {"probe": {"TOKEN": "synthetic-former-owner-token"}},
                }
            ),
            encoding="utf-8",
        )
        capsys.readouterr()
        assert cmd_add(["probe", str(unrelated), "filedrop"]) == 1
        assert "probe" not in load_registry()
        save_registry({"probe": {"root": str(unrelated.resolve()), "definition": "filedrop"}})
        with pytest.raises(ValueError, match="secrets.json unreadable"):
            load_agent_env("probe")
        assert ar3_nodes.node_secrets("probe") == {}
