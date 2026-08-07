"""Tests for commands.py — focused on the canonicalization invariant added
for issue #65 (lowercase canonical key at registration time, regardless of
the casing the user typed) and the per-agent kill / no-orphan rule from
issue #68."""
from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import pytest

from commands import (
    cmd_add,
    cmd_alias,
    cmd_definitions,
    cmd_kill,
    cmd_logs,
    cmd_ls,
    cmd_namespace,
    cmd_ps,
    cmd_namespaces,
    cmd_remote,
    cmd_remove,
    cmd_restart,
    cmd_start,
    cmd_stop,
    cmd_storage,
    cmd_tell,
    cmd_trace,
    cmd_unalias,
    cmd_unnamespace,
    cmd_unremote,
    cmd_unstorage,
    cmd_update,
    cmd_vars,
    _update_restart_targets,
    _warn_unresolvable_harnesses,
    parse_option_tokens,
)
from core import Participant, TELL_OUTBOX_DIR_ENV, agent_dir, agent_log_path, files_dir, kill_request_path, outbox_bundle_dir, outbox_dir, pid_path, user_definitions_dir
from mailbox import ensure_mailboxes
from network import load_network_config, merge_spec_secrets, save_network_config
from registry import load_aliases, load_namespaces, load_registry, save_aliases, save_namespaces, save_registry
from definitions import resolve_definition_arg


@pytest.fixture
def agent_root(fake_home, tmp_path):
    d = tmp_path / "x"
    d.mkdir()
    return d


class TestCmdAddCanonicalization:
    def test_uppercase_input_stored_lowercase(self, agent_root):
        rc = cmd_add(["CLAUDE", str(agent_root)])
        assert rc == 0
        reg = load_registry()
        assert "claude" in reg
        assert "CLAUDE" not in reg

    def test_mixed_case_collision_rejected(self, agent_root, tmp_path, capsys):
        assert cmd_add(["claude", str(agent_root)]) == 0
        other = tmp_path / "y"
        other.mkdir()
        # Re-add under a different casing — should be rejected as duplicate
        # rather than producing a second registry entry.
        rc = cmd_add(["Claude", str(other)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "already exists" in err
        # Registry still has exactly one entry.
        assert list(load_registry().keys()) == ["claude"]

    def test_directory_path_uses_canonical_key(self, agent_root):
        cmd_add(["CLAUDE", str(agent_root)])
        # Directory derived from canonical (lowercase) key.
        assert agent_dir("claude").exists() or not agent_dir("CLAUDE").exists()
        # The actual on-disk dir is materialized lazily by ensure_mailboxes,
        # so just check that resolution paths agree.
        assert agent_dir("claude") == agent_dir("CLAUDE".lower())

    def test_invalid_name_rejected(self, agent_root, capsys):
        rc = cmd_add(["foo bar", str(agent_root)])
        assert rc == 2
        err = capsys.readouterr().err
        assert "alphanumeric" in err

    def test_empty_name_rejected(self, agent_root, capsys):
        rc = cmd_add(["", str(agent_root)])
        assert rc == 2


class TestCmdAddBundledDefinition:
    def test_bare_kind_resolves_bundled(self, fake_home, agent_root, capsys):
        from definitions import default_definition_path

        rc = cmd_add(["neil-macbook", str(agent_root), "filedrop"])
        assert rc == 0
        out = capsys.readouterr().out
        assert str(default_definition_path("filedrop")) in out
        assert load_registry()["neil-macbook"]["definition"] == str(default_definition_path("filedrop"))

    def test_bare_kind_with_json_suffix(self, fake_home, agent_root):
        from definitions import default_definition_path

        assert cmd_add(["seat", str(agent_root), "filedrop.json"]) == 0
        assert load_registry()["seat"]["definition"] == str(default_definition_path("filedrop"))

    def test_bare_r4t_kind(self, fake_home, agent_root, capsys):
        from definitions import default_definition_path

        assert cmd_add(["solo-node", str(agent_root), "r4t"]) == 0
        assert load_registry()["solo-node"]["definition"] == str(
            default_definition_path("r4t")
        )
        capsys.readouterr()
        assert cmd_ls([]) == 0
        assert "r4t" in capsys.readouterr().out

    def test_unknown_bare_kind_lists_available(self, fake_home, agent_root, capsys):
        rc = cmd_add(["seat", str(agent_root), "not-a-real-def"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "unknown definition: not-a-real-def" in err
        assert "filedrop" in err and "r4t" in err

    def test_unknown_path_reports_not_a_file(self, fake_home, agent_root, tmp_path, capsys):
        missing = tmp_path / "nope.json"
        rc = cmd_add(["seat", str(agent_root), str(missing)])
        assert rc == 1
        err = capsys.readouterr().err
        assert f"not a file: {missing}" in err
        assert "unknown definition" not in err

    def test_explicit_path_still_works(self, fake_home, agent_root, tmp_path):
        custom = tmp_path / "custom.json"
        custom.write_text('{"proxy": "file"}')
        assert cmd_add(["seat", str(agent_root), str(custom)]) == 0
        assert load_registry()["seat"]["definition"] == str(custom.resolve())

    def test_add_with_var_flag(self, fake_home, agent_root, capsys):
        from definitions import default_definition_path

        rc = cmd_add([
            "bob", str(agent_root), "ollama-opencode", "--model=qwen3.6",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "var: MODEL=qwen3.6" in out
        reg = load_registry()["bob"]
        assert reg["definition"] == str(default_definition_path("ollama-opencode"))
        assert reg["vars"] == {"MODEL": "qwen3.6"}

    def test_add_var_flag_case_insensitive(self, fake_home, agent_root):
        assert cmd_add([
            "bob", str(agent_root), "ollama-opencode", "--Model=qwen3.6",
        ]) == 0
        assert load_registry()["bob"]["vars"]["MODEL"] == "qwen3.6"

    def test_add_rejects_bad_var_flag(self, fake_home, agent_root, capsys):
        rc = cmd_add(["bob", str(agent_root), "filedrop", "--model"])
        assert rc == 2
        assert "missing value for --model" in capsys.readouterr().err

    def test_add_takes_a_var_the_space_way_too(self, fake_home, agent_root):
        # `a8s storage` and `a8s remote` have always wanted the space form and
        # `a8s add` has always wanted `=`. Nobody can be expected to remember
        # which is which, so both work everywhere now.
        assert cmd_add(["bob", str(agent_root), "filedrop", "--Model", "qwen3.6"]) == 0
        assert load_registry()["bob"]["vars"]["MODEL"] == "qwen3.6"

    def test_a_var_the_space_way_without_a_definition(self, fake_home, agent_root):
        # The definition is positional, so the first `--` has to be the
        # boundary or the option value would be read as the definition.
        assert cmd_add(["bob", str(agent_root), "--Model", "qwen3.6"]) == 0
        assert load_registry()["bob"]["vars"]["MODEL"] == "qwen3.6"

    def test_two_positionals_is_still_a_usage_error(self, fake_home, agent_root, capsys):
        rc = cmd_add(["bob", str(agent_root), "filedrop", "extra"])
        assert rc == 2
        assert "usage: a8s add" in capsys.readouterr().err

    def test_add_rejects_var_named_builtin(self, fake_home, agent_root, capsys):
        rc = cmd_add(["bob", str(agent_root), "filedrop", "--message=x"])
        assert rc == 2
        assert "reserved" in capsys.readouterr().err


class TestCmdAddAliasCollision:
    def test_alias_then_agent_with_same_name_rejected(self, fake_home, tmp_path, agent_root, capsys):
        # First agent registered.
        other = tmp_path / "other"; other.mkdir()
        cmd_add(["claude", str(other)])
        # Create an alias.
        assert cmd_alias(["devs", "claude"]) == 0
        # Try to register a new agent named "DEVS" — must collide with the
        # alias namespace, rejected.
        rc = cmd_add(["DEVS", str(agent_root)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "alias" in err.lower()


class TestCmdAliasCanonicalization:
    def test_alias_name_canonicalized(self, fake_home, agent_root):
        cmd_add(["claude", str(agent_root)])
        rc = cmd_alias(["DEVS", "Claude"])
        assert rc == 0
        aliases = load_aliases()
        assert "devs" in aliases
        assert aliases["devs"] == ["claude"]

    def test_alias_collides_with_agent_name(self, fake_home, agent_root, capsys):
        cmd_add(["claude", str(agent_root)])
        # Try to create an alias whose name (lowercased) matches an agent.
        rc = cmd_alias(["CLAUDE", "claude"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "agent already exists" in err

    def test_unknown_member_rejected(self, fake_home, capsys):
        rc = cmd_alias(["devs", "nobody"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "unknown member" in err


class TestCmdAliasShowOne:
    def test_show_one_alias_lists_members(self, fake_home, tmp_path, agent_root, capsys):
        cmd_add(["claude", str(agent_root)])
        other = tmp_path / "g"; other.mkdir()
        cmd_add(["gemini", str(other)])
        cmd_alias(["devs", "claude"])
        cmd_alias(["devs", "gemini"])
        capsys.readouterr()
        rc = cmd_alias(["devs"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "devs:" in out
        assert "claude" in out
        assert "gemini" in out

    def test_show_one_alias_with_dashed_name(self, fake_home, agent_root, capsys):
        cmd_add(["claude", str(agent_root)])
        cmd_alias(["bin-test", "claude"])
        capsys.readouterr()
        rc = cmd_alias(["bin-test"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "bin-test:" in out
        assert "claude" in out

    def test_show_one_alias_case_insensitive(self, fake_home, agent_root, capsys):
        cmd_add(["claude", str(agent_root)])
        cmd_alias(["devs", "claude"])
        capsys.readouterr()
        rc = cmd_alias(["DEVS"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "devs:" in out

    def test_show_unknown_alias_errors(self, fake_home, capsys):
        rc = cmd_alias(["nobody"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "no alias" in err

    def test_show_invalid_name_errors(self, fake_home, capsys):
        rc = cmd_alias(["bad name with spaces"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "alphanumeric" in err


class TestCmdNamespace:
    """Issue #148 — prefix routing. Mirrors the `alias` surface: list / show /
    bind, plus `unnamespace` for removal. The bind target must be a registered
    agent (single delivery — the opposite of alias fan-out)."""

    def test_bind_stores_canonical_lowercase(self, agent_root):
        cmd_add(["node", str(agent_root)])
        rc = cmd_namespace(["ACME", "NODE"])
        assert rc == 0
        assert load_namespaces() == {"acme": "node"}

    def test_rebind_overwrites(self, agent_root, tmp_path, capsys):
        cmd_add(["node", str(agent_root)])
        other = tmp_path / "y"
        other.mkdir()
        cmd_add(["other", str(other)])
        cmd_namespace(["acme", "node"])
        rc = cmd_namespace(["acme", "other"])
        assert rc == 0
        assert load_namespaces() == {"acme": "other"}
        assert "rebound" in capsys.readouterr().out

    def test_target_must_be_registered(self, fake_home, capsys):
        rc = cmd_namespace(["acme", "ghost"])
        assert rc == 1
        assert "unknown agent" in capsys.readouterr().err
        assert load_namespaces() == {}

    def test_target_must_not_be_alias(self, agent_root, capsys):
        cmd_add(["node", str(agent_root)])
        cmd_alias(["devs", "node"])
        rc = cmd_namespace(["acme", "devs"])
        assert rc == 1
        assert "not an alias" in capsys.readouterr().err
        assert load_namespaces() == {}

    def test_prefix_may_match_own_agent(self, agent_root, capsys):
        # A node owning its own namespace (#175): agent `s1l` binds prefix
        # `s1l` to itself so cross-wall traffic is attributed to `s1l`.
        cmd_add(["s1l", str(agent_root)])
        rc = cmd_namespace(["s1l", "s1l"])
        assert rc == 0
        assert load_namespaces() == {"s1l": "s1l"}

    def test_prefix_collides_with_other_agent(self, agent_root, tmp_path, capsys):
        cmd_add(["node", str(agent_root)])
        other = tmp_path / "other"
        other.mkdir()
        cmd_add(["s1l", str(other)])
        # Binding prefix `node` to a *different* agent would shadow the `node`
        # agent on a bare `tell node`, so it's still rejected.
        rc = cmd_namespace(["node", "s1l"])
        assert rc == 1
        assert "agent already exists" in capsys.readouterr().err

    def test_prefix_collides_with_alias(self, agent_root, capsys):
        cmd_add(["node", str(agent_root)])
        cmd_alias(["devs", "node"])
        rc = cmd_namespace(["devs", "node"])
        assert rc == 1
        assert "alias already exists" in capsys.readouterr().err

    def test_invalid_prefix_rejected(self, agent_root, capsys):
        cmd_add(["node", str(agent_root)])
        rc = cmd_namespace(["acme:x", "node"])
        assert rc == 2

    def test_show_one(self, agent_root, capsys):
        cmd_add(["node", str(agent_root)])
        cmd_namespace(["acme", "node"])
        capsys.readouterr()
        rc = cmd_namespace(["acme"])
        assert rc == 0
        assert "acme: -> node" in capsys.readouterr().out

    def test_show_unknown(self, fake_home, capsys):
        rc = cmd_namespace(["ghost"])
        assert rc == 1
        assert "no namespace named" in capsys.readouterr().err

    def test_list(self, agent_root, capsys):
        cmd_add(["node", str(agent_root)])
        cmd_namespace(["acme", "node"])
        capsys.readouterr()
        rc = cmd_namespaces()
        assert rc == 0
        out = capsys.readouterr().out
        assert "acme" in out
        assert "node" in out

    def test_list_flags_dangling_binding(self, fake_home, capsys):
        save_namespaces({"acme": "gone"})
        rc = cmd_namespaces()
        assert rc == 0
        assert "unknown agent" in capsys.readouterr().out


class TestCmdUnnamespace:
    def test_remove_case_insensitive(self, agent_root):
        cmd_add(["node", str(agent_root)])
        cmd_namespace(["acme", "node"])
        rc = cmd_unnamespace(["ACME"])
        assert rc == 0
        assert load_namespaces() == {}

    def test_unknown(self, fake_home, capsys):
        rc = cmd_unnamespace(["ghost"])
        assert rc == 1
        assert "no namespace named" in capsys.readouterr().err

    def test_usage(self, fake_home, capsys):
        assert cmd_unnamespace([]) == 2


class TestNamespaceCollisionsElsewhere:
    """Aliases stay disjoint from namespaces in both directions. Agent names
    may match a prefix only when it's the agent's own namespace (#175); a
    prefix bound to a different agent still blocks the name. Removing an agent
    unbinds its prefixes (no orphans)."""

    def test_add_rejects_namespace_bound_to_other_agent(self, fake_home, tmp_path, capsys):
        node = tmp_path / "node"
        node.mkdir()
        cmd_add(["node", str(node)])
        cmd_namespace(["acme", "node"])
        other = tmp_path / "other"
        other.mkdir()
        rc = cmd_add(["acme", str(other)])
        assert rc == 1
        assert "namespace already exists" in capsys.readouterr().err
        assert "acme" not in load_registry()

    def test_add_allows_own_namespace_prefix(self, fake_home, tmp_path):
        # A dangling self-namespace (`s1l` -> `s1l` with no agent yet) doesn't
        # block re-materializing the node; `tell s1l` still lands on `s1l`.
        save_namespaces({"s1l": "s1l"})
        root = tmp_path / "s1l"
        root.mkdir()
        rc = cmd_add(["s1l", str(root)])
        assert rc == 0
        assert "s1l" in load_registry()

    def test_alias_rejects_existing_namespace_prefix(self, fake_home, tmp_path, capsys):
        node = tmp_path / "node"
        node.mkdir()
        cmd_add(["node", str(node)])
        cmd_namespace(["acme", "node"])
        rc = cmd_alias(["acme", "node"])
        assert rc == 1
        assert "namespace already exists" in capsys.readouterr().err
        assert load_aliases() == {}

    def test_remove_unbinds_namespaces(self, fake_home, tmp_path, capsys):
        node = tmp_path / "node"
        node.mkdir()
        cmd_add(["node", str(node)])
        cmd_namespace(["acme", "node"])
        rc = cmd_remove(["node"])
        assert rc == 0
        assert "unbound namespaces: acme" in capsys.readouterr().out
        assert load_namespaces() == {}


class TestCmdRemove:
    def test_unknown_agent_rejected(self, fake_home, capsys):
        rc = cmd_remove(["nobody"])
        assert rc == 1
        assert "no agent" in capsys.readouterr().err

    def test_invalid_name_rejected(self, fake_home, capsys):
        rc = cmd_remove(["foo bar"])
        assert rc == 2
        assert "alphanumeric" in capsys.readouterr().err

    def test_usage_on_wrong_arity(self, fake_home, capsys):
        assert cmd_remove([]) == 2
        assert cmd_remove(["a", "b"]) == 2

    def test_running_handler_blocks_removal(self, fake_home, agent_root, capsys):
        cmd_add(["claude", str(agent_root)])
        # Claim claude under our own (live) pid.
        pid_path("claude").parent.mkdir(parents=True, exist_ok=True)
        pid_path("claude").write_text(str(os.getpid()))
        rc = cmd_remove(["claude"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "running" in err
        # Registry untouched.
        assert "claude" in load_registry()

    def test_basic_removal_wipes_dir_and_registry(self, fake_home, agent_root):
        cmd_add(["claude", str(agent_root)])
        # Materialize the agent dir so we can verify it's wiped.
        agent_dir("claude").mkdir(parents=True, exist_ok=True)
        (agent_dir("claude") / "log.txt").write_text("hi")
        rc = cmd_remove(["claude"])
        assert rc == 0
        assert "claude" not in load_registry()
        assert not agent_dir("claude").exists()

    def test_case_insensitive(self, fake_home, agent_root):
        cmd_add(["claude", str(agent_root)])
        rc = cmd_remove(["Claude"])
        assert rc == 0
        assert load_registry() == {}

    def test_cascade_prunes_alias_member(self, fake_home, tmp_path, agent_root, capsys):
        cmd_add(["claude", str(agent_root)])
        other = tmp_path / "g"; other.mkdir()
        cmd_add(["gemini", str(other)])
        cmd_alias(["devs", "claude"])
        cmd_alias(["devs", "gemini"])
        rc = cmd_remove(["claude"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "pruned from aliases" in out
        # Alias remains with just gemini.
        assert load_aliases() == {"devs": ["gemini"]}

    def test_cascade_drops_now_empty_alias(self, fake_home, agent_root, capsys):
        cmd_add(["claude", str(agent_root)])
        cmd_alias(["devs", "claude"])
        rc = cmd_remove(["claude"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "dropped now-empty aliases" in out
        assert load_aliases() == {}


class TestCmdUnaliasCaseInsensitive:
    def test_unalias_with_different_case(self, fake_home, agent_root):
        cmd_add(["claude", str(agent_root)])
        cmd_alias(["devs", "claude"])
        # Use uppercase to remove — should match canonical lowercase entry.
        rc = cmd_unalias(["DEVS"])
        assert rc == 0
        assert load_aliases() == {}


class TestCmdKillPerAgent:
    """`a8s kill <name>` writes a kill-request file and SIGUSR1s the holder.
    Tests stub `os.kill` so we don't actually signal a real process; we
    verify the file mechanics + that the SIGUSR1 was directed at the
    holder pid."""

    def test_writes_kill_request_and_signals_holder(self, fake_home, tmp_path, monkeypatch, capsys):
        d = tmp_path / "x"; d.mkdir()
        save_registry({"claude": {"root": str(d)}})
        # Pre-attach claude to a foreign live pid.
        pid_path("claude").parent.mkdir(parents=True, exist_ok=True)
        pid_path("claude").write_text(str(os.getppid()))

        signaled = []
        def fake_kill(pid, sig):
            signaled.append((pid, sig))
            # Simulate the holder honoring the request: unlink the pid file.
            if sig == signal.SIGUSR1:
                pid_path("claude").unlink()
        monkeypatch.setattr("commands.os.kill", fake_kill)

        rc = cmd_kill(["claude"])
        assert rc == 0
        # SIGUSR1 went to the holder.
        assert (os.getppid(), signal.SIGUSR1) in signaled
        # No SIGTERM escalation (holder responded).
        assert not any(s == signal.SIGTERM for _, s in signaled)
        # Kill-request file was cleared at the end.
        assert not kill_request_path("claude").is_file()
        # Output includes the request notice.
        out = capsys.readouterr().out
        assert "kill request" in out

    def test_escalates_to_sigterm_on_unresponsive_holder(self, fake_home, tmp_path, monkeypatch, capsys):
        d = tmp_path / "x"; d.mkdir()
        save_registry({"claude": {"root": str(d)}})
        pid_path("claude").parent.mkdir(parents=True, exist_ok=True)
        pid_path("claude").write_text(str(os.getppid()))

        signaled = []
        def fake_kill(pid, sig):
            signaled.append((pid, sig))
            # DON'T release — simulate a wedged holder.
        monkeypatch.setattr("commands.os.kill", fake_kill)
        # Tighten the timeout so the test isn't slow.
        monkeypatch.setattr("commands.KILL_TIMEOUT_S", 0.3)
        monkeypatch.setattr("commands.KILL_POLL_S", 0.05)

        rc = cmd_kill(["claude"])
        assert rc == 1
        # Both SIGUSR1 and the SIGTERM escalation got delivered.
        sigs = {s for _, s in signaled}
        assert signal.SIGUSR1 in sigs
        assert signal.SIGTERM in sigs
        err = capsys.readouterr().err
        assert "did not honor kill" in err

    def test_not_running_is_no_op(self, fake_home, tmp_path, capsys):
        d = tmp_path / "x"; d.mkdir()
        save_registry({"claude": {"root": str(d)}})
        # No pid file → not running.
        rc = cmd_kill(["claude"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "not running" in out


class TestCmdStopAndRestart:
    def test_stop_waits_until_pid_gone(self, fake_home, tmp_path, monkeypatch, capsys):
        d = tmp_path / "x"; d.mkdir()
        save_registry({"claude": {"root": str(d)}})
        holder = os.getppid()
        pid_path("claude").parent.mkdir(parents=True, exist_ok=True)
        pid_path("claude").write_text(str(holder))

        signaled = []
        polls = {"n": 0}

        def fake_kill(pid, sig):
            if sig == 0:
                if not pid_path("claude").is_file():
                    raise ProcessLookupError()
                return
            signaled.append((pid, sig))

        def fake_alive(pid):
            polls["n"] += 1
            if polls["n"] >= 3:
                pid_path("claude").unlink(missing_ok=True)
                return False
            return pid == holder

        monkeypatch.setattr("commands.os.kill", fake_kill)
        monkeypatch.setattr("daemon.os.kill", fake_kill)
        monkeypatch.setattr("core.os.kill", fake_kill)
        monkeypatch.setattr("commands._pid_alive", fake_alive)
        monkeypatch.setattr("daemon._pid_alive", lambda pid: pid_path("claude").is_file())
        monkeypatch.setattr("core._pid_alive", lambda pid: pid_path("claude").is_file())
        monkeypatch.setattr("commands.STOP_POLL_S", 0.01)
        monkeypatch.setattr("commands.STOP_WAIT_S", 2.0)

        rc = cmd_stop(["claude"])
        assert rc == 0
        assert signaled == [(holder, signal.SIGTERM)]
        out = capsys.readouterr().out
        assert "waiting" in out
        assert "stopped" in out

    def test_stop_force_sends_second_sigterm(self, fake_home, tmp_path, monkeypatch, capsys):
        d = tmp_path / "x"; d.mkdir()
        save_registry({"claude": {"root": str(d)}})
        holder = os.getppid()
        pid_path("claude").parent.mkdir(parents=True, exist_ok=True)
        pid_path("claude").write_text(str(holder))

        signaled = []

        def fake_kill(pid, sig):
            if sig == 0:
                if not pid_path("claude").is_file():
                    raise ProcessLookupError()
                return
            signaled.append((pid, sig))
            if len(signaled) >= 2:
                pid_path("claude").unlink(missing_ok=True)

        monkeypatch.setattr("commands.os.kill", fake_kill)
        monkeypatch.setattr("daemon.os.kill", fake_kill)
        monkeypatch.setattr("core.os.kill", fake_kill)
        monkeypatch.setattr("commands._pid_alive", lambda pid: pid_path("claude").is_file())
        monkeypatch.setattr("commands.STOP_POLL_S", 0.01)
        monkeypatch.setattr("commands.STOP_FORCE_WAIT_S", 2.0)

        rc = cmd_stop(["claude", "--force"])
        assert rc == 0
        assert signaled == [(holder, signal.SIGTERM), (holder, signal.SIGTERM)]
        assert "second SIGTERM" in capsys.readouterr().out

    def test_stop_timeout_suggests_force(self, fake_home, tmp_path, monkeypatch, capsys):
        d = tmp_path / "x"; d.mkdir()
        save_registry({"claude": {"root": str(d)}})
        holder = os.getppid()
        pid_path("claude").parent.mkdir(parents=True, exist_ok=True)
        pid_path("claude").write_text(str(holder))

        def fake_kill(pid, sig):
            if sig == 0:
                return
            return None

        monkeypatch.setattr("commands.os.kill", fake_kill)
        monkeypatch.setattr("daemon.os.kill", fake_kill)
        monkeypatch.setattr("core.os.kill", fake_kill)
        monkeypatch.setattr("commands._pid_alive", lambda pid: True)
        monkeypatch.setattr("daemon._pid_alive", lambda pid: True)
        monkeypatch.setattr("commands.STOP_POLL_S", 0.01)
        monkeypatch.setattr("commands.STOP_WAIT_S", 0.05)

        rc = cmd_stop(["claude"])
        assert rc == 1
        assert "try `a8s stop --force`" in capsys.readouterr().err

    def test_restart_stops_then_starts(self, fake_home, tmp_path, monkeypatch, capsys):
        d = tmp_path / "x"; d.mkdir()
        save_registry({"claude": {"root": str(d)}})
        holder = os.getppid()
        pid_path("claude").parent.mkdir(parents=True, exist_ok=True)
        pid_path("claude").write_text(str(holder))

        calls = []

        def fake_kill(pid, sig):
            if sig == 0:
                if not pid_path("claude").is_file():
                    raise ProcessLookupError()
                return
            calls.append(("kill", pid, sig))
            pid_path("claude").unlink(missing_ok=True)

        class FakeProc:
            pid = 99999

        def fake_popen(*a, **k):
            calls.append(("popen", a[0]))
            return FakeProc()

        monkeypatch.setattr("commands.os.kill", fake_kill)
        monkeypatch.setattr("daemon.os.kill", fake_kill)
        monkeypatch.setattr("core.os.kill", fake_kill)
        monkeypatch.setattr("commands._pid_alive", lambda pid: False)
        monkeypatch.setattr("commands.subprocess.Popen", fake_popen)
        monkeypatch.setattr("commands.STOP_POLL_S", 0.01)

        rc = cmd_restart(["claude"])
        assert rc == 0
        assert any(c[0] == "kill" for c in calls)
        assert any(c[0] == "popen" for c in calls)
        out = capsys.readouterr().out
        assert "started claude" in out

    def test_restart_when_not_running_just_starts(self, fake_home, tmp_path, monkeypatch, capsys):
        d = tmp_path / "x"; d.mkdir()
        save_registry({"claude": {"root": str(d)}})

        class FakeProc:
            pid = 42

        monkeypatch.setattr(
            "commands.subprocess.Popen", lambda *a, **k: FakeProc()
        )
        rc = cmd_restart(["claude"])
        assert rc == 0
        assert "started claude as PID 42" in capsys.readouterr().out


class TestCmdUpdate:
    def test_no_nodes_running(self, fake_home, capsys):
        assert cmd_update([]) == 0
        out = capsys.readouterr().out
        assert "conversation housekeeping" in out
        assert "no nodes running" in out

    def test_housekeeping_prunes_when_no_nodes_running(self, fake_home, capsys):
        from convo import load_entries, record
        from settings import set_setting

        set_setting("convo_max_rows", 2)
        for i in range(4):
            record(
                {"id": f"01UPDATE{i}", "from": "A", "to": "B", "content": str(i)},
                recipients=["B"],
            )
        assert cmd_update([]) == 0
        assert [row["content"] for row in load_entries()] == ["2", "3"]
        assert "pruned 2" in capsys.readouterr().out

    def test_prefers_alias_for_shared_pid(self, fake_home, tmp_path):
        a = tmp_path / "a"; a.mkdir()
        b = tmp_path / "b"; b.mkdir()
        save_registry({"claude": {"root": str(a)}, "gemini": {"root": str(b)}})
        save_aliases({"devs": ["claude", "gemini"]})
        assert _update_restart_targets({100: ["claude", "gemini"]}) == ["devs"]

    def test_individuals_when_no_alias_match(self, fake_home, tmp_path):
        a = tmp_path / "a"; a.mkdir()
        b = tmp_path / "b"; b.mkdir()
        save_registry({"claude": {"root": str(a)}, "gemini": {"root": str(b)}})
        assert _update_restart_targets({100: ["claude"], 200: ["gemini"]}) == [
            "claude",
            "gemini",
        ]

    def test_update_restarts_running(self, fake_home, tmp_path, monkeypatch, capsys):
        d = tmp_path / "x"; d.mkdir()
        save_registry({"claude": {"root": str(d)}})
        holder = os.getppid()
        pid_path("claude").parent.mkdir(parents=True, exist_ok=True)
        pid_path("claude").write_text(str(holder))

        restarts = []

        def fake_restart(args):
            restarts.append(list(args))
            pid_path("claude").unlink(missing_ok=True)
            return 0

        monkeypatch.setattr("commands.cmd_restart", fake_restart)
        # Also need _read_handler_pid to see the running node before restart
        rc = cmd_update([])
        assert rc == 0
        assert restarts == [["claude"]]
        out = capsys.readouterr().out
        assert "update complete" in out

    def test_update_passes_force(self, fake_home, tmp_path, monkeypatch):
        d = tmp_path / "x"; d.mkdir()
        save_registry({"claude": {"root": str(d)}})
        pid_path("claude").parent.mkdir(parents=True, exist_ok=True)
        pid_path("claude").write_text(str(os.getppid()))

        restarts = []
        monkeypatch.setattr(
            "commands.cmd_restart",
            lambda args: restarts.append(list(args)) or 0,
        )
        assert cmd_update(["--force"]) == 0
        assert restarts == [["claude", "--force"]]


def _claim(name: str) -> None:
    """Write a live pid file so `name` reads as running (the pytest process
    is alive, so its own pid passes the liveness check)."""
    p = pid_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(os.getpid()))


class TestCmdLs:
    """`a8s ls` lists every registered node, running or not — docker-style."""

    def test_empty_registry_prints_hint(self, fake_home, capsys):
        rc = cmd_ls([])
        assert rc == 0
        assert "no nodes registered" in capsys.readouterr().out

    def test_lists_running_and_stopped(self, fake_home, tmp_path, capsys):
        a = tmp_path / "a"; a.mkdir()
        b = tmp_path / "b"; b.mkdir()
        save_registry({"claude": {"root": str(a)}, "gemini": {"root": str(b)}})
        _claim("claude")  # gemini left stopped
        rc = cmd_ls([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "NAME" in out and "STATUS" in out and "ROOT" in out
        assert f"running (pid {os.getpid()})" in out
        assert "stopped" in out
        # Both nodes appear.
        assert "claude" in out and "gemini" in out

    def test_quiet_prints_names_only(self, fake_home, tmp_path, capsys):
        a = tmp_path / "a"; a.mkdir()
        b = tmp_path / "b"; b.mkdir()
        save_registry({"claude": {"root": str(a)}, "gemini": {"root": str(b)}})
        _claim("claude")
        rc = cmd_ls(["-q"])
        assert rc == 0
        out = capsys.readouterr().out
        assert out.splitlines() == ["claude", "gemini"]
        # No header, no status decoration.
        assert "STATUS" not in out and "running" not in out

    def test_namespace_column_appears_when_bound(self, fake_home, tmp_path, capsys):
        a = tmp_path / "a"; a.mkdir()
        save_registry({"claude": {"root": str(a)}})
        save_namespaces({"acme": "claude"})
        rc = cmd_ls([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "NAMESPACES" in out
        assert "acme:" in out

    def test_no_namespace_column_when_none_bound(self, fake_home, tmp_path, capsys):
        a = tmp_path / "a"; a.mkdir()
        save_registry({"claude": {"root": str(a)}})
        rc = cmd_ls([])
        assert rc == 0
        assert "NAMESPACES" not in capsys.readouterr().out


class TestCmdPs:
    """`a8s ps` lists only running node processes — docker-style."""

    def test_lists_running_only(self, fake_home, tmp_path, capsys):
        a = tmp_path / "a"; a.mkdir()
        b = tmp_path / "b"; b.mkdir()
        save_registry({"claude": {"root": str(a)}, "gemini": {"root": str(b)}})
        _claim("gemini")  # claude left stopped
        rc = cmd_ps([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "NAME" in out and "PID" in out and "UPTIME" in out
        assert "gemini" in out
        assert "claude" not in out

    def test_quiet_prints_names_only(self, fake_home, tmp_path, capsys):
        a = tmp_path / "a"; a.mkdir()
        save_registry({"claude": {"root": str(a)}})
        _claim("claude")
        rc = cmd_ps(["-q"])
        assert rc == 0
        out = capsys.readouterr().out
        assert out.splitlines() == ["claude"]
        assert "PID" not in out

    def test_empty_state_hints_at_ls(self, fake_home, tmp_path, capsys):
        a = tmp_path / "a"; a.mkdir()
        save_registry({"claude": {"root": str(a)}})  # registered but stopped
        rc = cmd_ps([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "no nodes running" in out
        assert "a8s ls" in out

    def test_quiet_empty_is_silent(self, fake_home, tmp_path, capsys):
        save_registry({})
        rc = cmd_ps(["-q"])
        assert rc == 0
        assert capsys.readouterr().out == ""


class TestCmdDefinitions:
    def test_list_includes_builtins(self, fake_home, capsys):
        rc = cmd_definitions([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "NAME" in out and "SOURCE" in out and "PATH" in out
        assert "builtin" in out
        assert "filedrop" in out

    def test_add_then_resolve_and_rm(self, fake_home, tmp_path, capsys):
        src = tmp_path / "my-custom-definition.json"
        src.write_text('{"invoke": ["echo", "hi"]}')
        rc = cmd_definitions(["add", str(src)])
        assert rc == 0
        dest = user_definitions_dir() / "my-custom-definition.json"
        assert dest.is_file()
        assert resolve_definition_arg("my-custom-definition") == dest.resolve()
        capsys.readouterr()
        rc = cmd_definitions(["ls"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "my-custom-definition" in out
        assert "user" in out
        rc = cmd_definitions(["rm", "my-custom-definition"])
        assert rc == 0
        assert not dest.exists()

    def test_add_rejects_builtin_name(self, fake_home, tmp_path, capsys):
        src = tmp_path / "filedrop.json"
        src.write_text('{"invoke": ["echo"]}')
        rc = cmd_definitions(["add", str(src)])
        assert rc == 1
        assert "conflicts with a repo built-in" in capsys.readouterr().err
        assert not (user_definitions_dir() / "filedrop.json").exists()

    def test_rm_rejects_builtin(self, fake_home, capsys):
        rc = cmd_definitions(["remove", "claude"])
        assert rc == 1
        assert "cannot remove built-in" in capsys.readouterr().err

    def test_add_rejects_invalid_json(self, fake_home, tmp_path, capsys):
        src = tmp_path / "broken.json"
        src.write_text("{not json")
        rc = cmd_definitions(["add", str(src)])
        assert rc == 1
        assert "not valid JSON" in capsys.readouterr().err

    def test_add_then_bare_add_agent(self, fake_home, tmp_path, agent_root):
        src = tmp_path / "my-custom-definition.json"
        src.write_text('{"invoke": ["echo"]}')
        assert cmd_definitions(["add", str(src)]) == 0
        assert cmd_add(["alice", str(agent_root), "my-custom-definition"]) == 0
        reg = load_registry()
        assert Path(reg["alice"]["definition"]).name == "my-custom-definition.json"


class TestCmdVars:
    def test_set_list_unset(self, fake_home, agent_root, capsys):
        assert cmd_add(["bob", str(agent_root)]) == 0
        capsys.readouterr()
        assert cmd_vars(["bob", "set", "MODEL", "qwen3.6"]) == 0
        assert load_registry()["bob"]["vars"]["MODEL"] == "qwen3.6"
        capsys.readouterr()
        assert cmd_vars(["bob"]) == 0
        out = capsys.readouterr().out
        assert "MODEL" in out and "qwen3.6" in out
        assert cmd_vars(["bob", "unset", "MODEL"]) == 0
        assert "vars" not in load_registry()["bob"]

    def test_vars_case_insensitive(self, fake_home, agent_root):
        assert cmd_add(["bob", str(agent_root)]) == 0
        assert cmd_vars(["bob", "set", "model", "qwen3.6"]) == 0
        assert load_registry()["bob"]["vars"] == {"MODEL": "qwen3.6"}
        assert cmd_vars(["bob", "unset", "MoDeL"]) == 0
        assert "vars" not in load_registry()["bob"]

    def test_set_rejects_builtin_name(self, fake_home, agent_root, capsys):
        assert cmd_add(["bob", str(agent_root)]) == 0
        capsys.readouterr()
        assert cmd_vars(["bob", "set", "message", "x"]) == 2
        assert "reserved" in capsys.readouterr().err

    def test_build_command_uses_registry_vars(self, fake_home, agent_root):
        from definitions import build_command, load_agent_vars

        assert cmd_add(["bob", str(agent_root)]) == 0
        assert cmd_vars(["bob", "set", "model", "qwen3.6"]) == 0
        defn = {"invoke": ["x", "--model", "$MODEL", "$MESSAGE"]}
        argv = build_command(
            defn,
            {"from": "a", "to": "bob", "content": "hi"},
            agent_root,
            vars=load_agent_vars("bob"),
        )
        assert argv == ["x", "--model", "qwen3.6", "hi"]

    def test_lowercase_placeholder_matches_var(self, fake_home, agent_root):
        from definitions import build_command, load_agent_vars

        assert cmd_add(["bob", str(agent_root)]) == 0
        assert cmd_vars(["bob", "set", "MODEL", "qwen3.6"]) == 0
        defn = {"invoke": ["x", "$model"]}
        argv = build_command(
            defn,
            {"from": "a", "to": "bob", "content": "hi"},
            agent_root,
            vars=load_agent_vars("bob"),
        )
        assert argv == ["x", "qwen3.6"]


class TestHarnessWarningAtStart:
    """`a8s start` hands its own environment to the handler, which hands it to
    every wake. So a node's PATH is whatever the shell that started it happened
    to have, permanently, until restart — and the failure surfaces hours later
    at the first wake, in a shell the operator is no longer looking at (#121).
    """

    def _register(self, agent_root, tmp_path, invoke, *, idle=None):
        spec = {"description": "probe", "invoke": invoke}
        if idle is not None:
            spec["idle"] = {"timeout": 3600, "invoke": idle}
        path = tmp_path / "probe-definition.json"
        path.write_text(json.dumps(spec))
        assert cmd_add(["probe", str(agent_root), str(path)]) == 0

    def test_an_unresolvable_harness_is_named(self, fake_home, agent_root, tmp_path, capsys):
        self._register(agent_root, tmp_path, ["a8s-no-such-harness-xyz", "-p", "$MESSAGE"])
        capsys.readouterr()
        _warn_unresolvable_harnesses(["probe"])
        err = capsys.readouterr().err
        assert "a8s-no-such-harness-xyz" in err
        assert "PATH" in err

    def test_it_sees_through_a_wrapper(self, fake_home, agent_root, tmp_path, capsys):
        # The case the FileNotFoundError guard could never catch: argv[0]
        # resolves, and the failure happens inside flock.
        self._register(agent_root, tmp_path, ["flock", "/tmp/a8s.lock", "a8s-no-such-harness-xyz"])
        capsys.readouterr()
        _warn_unresolvable_harnesses(["probe"])
        err = capsys.readouterr().err
        assert "a8s-no-such-harness-xyz" in err
        assert "flock" not in err  # the wrapper is not the problem

    def test_a_resolvable_harness_says_nothing(self, fake_home, agent_root, tmp_path, capsys):
        self._register(agent_root, tmp_path, ["python3", "-c", "pass"])
        capsys.readouterr()
        _warn_unresolvable_harnesses(["probe"])
        assert capsys.readouterr().err == ""

    def test_an_idle_invoke_is_probed_too(self, fake_home, agent_root, tmp_path, capsys):
        self._register(
            agent_root, tmp_path, ["python3", "-c", "pass"],
            idle=["a8s-no-such-idle-harness-xyz", "clear"],
        )
        capsys.readouterr()
        _warn_unresolvable_harnesses(["probe"])
        err = capsys.readouterr().err
        assert "a8s-no-such-idle-harness-xyz" in err
        assert "idle.invoke" in err

    def test_a_shell_string_is_not_second_guessed(self, fake_home, agent_root, tmp_path, capsys):
        self._register(agent_root, tmp_path, ["sh", "-c", "a8s-no-such-harness-xyz"])
        capsys.readouterr()
        _warn_unresolvable_harnesses(["probe"])
        assert capsys.readouterr().err == ""

    def test_an_unexpanded_var_is_not_probed(self, fake_home, agent_root, tmp_path, capsys):
        # `$HARNESS` becomes a real path per wake via `a8s vars`, so there is
        # nothing to resolve here and a warning would be pure noise.
        self._register(agent_root, tmp_path, ["$HARNESS", "-p", "$MESSAGE"])
        capsys.readouterr()
        _warn_unresolvable_harnesses(["probe"])
        assert capsys.readouterr().err == ""

    def test_an_unknown_member_is_not_fatal(self, fake_home, capsys):
        # Warning, never a refusal — a node that cannot wake is still worth
        # having attached, and a broken definition has its own diagnostics.
        _warn_unresolvable_harnesses(["no-such-agent"])
        assert capsys.readouterr().err == ""

    def test_the_warning_names_the_knobs_that_fix_it(
        self, fake_home, agent_root, tmp_path, capsys
    ):
        self._register(agent_root, tmp_path, ["a8s-no-such-harness-xyz"])
        capsys.readouterr()
        _warn_unresolvable_harnesses(["probe"])
        err = capsys.readouterr().err
        assert "definition.env" in err
        assert "wake_path" in err

    def test_a_declared_path_that_resolves_it_ends_the_warning(
        self, fake_home, agent_root, tmp_path, capsys
    ):
        # The probe runs against the environment the wake will actually get,
        # so a node fixed by the knob stops nagging.
        harness_dir = tmp_path / "bin"
        harness_dir.mkdir()
        exe = harness_dir / "a8s-probe-harness"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        path = tmp_path / "probe-definition.json"
        path.write_text(json.dumps({
            "invoke": ["a8s-probe-harness", "-p", "$MESSAGE"],
            "env": {"PATH": str(harness_dir)},
        }))
        assert cmd_add(["probe", str(agent_root), str(path)]) == 0
        capsys.readouterr()
        _warn_unresolvable_harnesses(["probe"])
        assert capsys.readouterr().err == ""

    def test_wake_path_alone_ends_the_warning(
        self, fake_home, agent_root, tmp_path, capsys, monkeypatch
    ):
        harness_dir = tmp_path / "bin"
        harness_dir.mkdir()
        exe = harness_dir / "a8s-probe-harness"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        self._register(agent_root, tmp_path, ["a8s-probe-harness", "-p", "$MESSAGE"])
        capsys.readouterr()
        _warn_unresolvable_harnesses(["probe"])
        assert "a8s-probe-harness" in capsys.readouterr().err
        from settings import set_setting

        set_setting("wake_path", str(harness_dir))
        _warn_unresolvable_harnesses(["probe"])
        assert capsys.readouterr().err == ""

    def test_a_login_shell_node_is_not_second_guessed(
        self, fake_home, agent_root, tmp_path, capsys
    ):
        # The opt-in exists for a PATH that cannot be written down, so nothing
        # here can predict what the rc will produce.
        path = tmp_path / "probe-definition.json"
        path.write_text(json.dumps({
            "invoke": ["a8s-no-such-harness-xyz"],
            "wake_shell": "login",
        }))
        assert cmd_add(["probe", str(agent_root), str(path)]) == 0
        capsys.readouterr()
        _warn_unresolvable_harnesses(["probe"])
        assert capsys.readouterr().err == ""


class _StartedProc:
    pid = 99999


def _fake_popen(*a, **k):
    return _StartedProc()


def _never_spawn(*a, **k):
    raise AssertionError("start should have refused before spawning")


class TestWakeShellAtStart:
    """`wake_shell` is POSIX-only, and a knob that silently does nothing is the
    failure it exists to end — so `a8s start` refuses rather than shrugs."""

    def _register(self, agent_root, tmp_path, wake_shell):
        path = tmp_path / "shell-definition.json"
        path.write_text(json.dumps({
            "invoke": ["python3", "-c", "pass"],
            "wake_shell": wake_shell,
        }))
        assert cmd_add(["probe", str(agent_root), str(path)]) == 0

    def test_windows_refuses_the_start(
        self, fake_home, agent_root, tmp_path, capsys, monkeypatch
    ):
        self._register(agent_root, tmp_path, "login")
        monkeypatch.setattr("definitions.IS_WINDOWS", True)
        monkeypatch.setattr("commands.subprocess.Popen", _never_spawn)
        capsys.readouterr()
        assert cmd_start(["probe"]) == 1
        err = capsys.readouterr().err
        assert "POSIX-only" in err
        assert "wake_path" in err

    def test_a_typo_refuses_the_start(
        self, fake_home, agent_root, tmp_path, capsys, monkeypatch
    ):
        self._register(agent_root, tmp_path, "interactive")
        monkeypatch.setattr("commands.subprocess.Popen", _never_spawn)
        capsys.readouterr()
        assert cmd_start(["probe"]) == 1
        assert "login" in capsys.readouterr().err

    def test_posix_starts_normally(
        self, fake_home, agent_root, tmp_path, capsys, monkeypatch
    ):
        self._register(agent_root, tmp_path, "login")
        monkeypatch.setattr("commands.subprocess.Popen", _fake_popen)
        capsys.readouterr()
        assert cmd_start(["probe"]) == 0


class TestWakePathCapture:
    """`a8s add` runs in the operator's own working shell, so that shell's PATH
    is correct by construction at exactly that moment. Recording it there is
    what stops the *start* shell's provenance from mattering (#121)."""

    def test_the_first_add_records_this_shells_path(
        self, fake_home, agent_root, monkeypatch
    ):
        from settings import get_setting

        monkeypatch.setenv("PATH", "/operator/bin:/usr/bin")
        assert cmd_add(["alpha", str(agent_root)]) == 0
        assert get_setting("wake_path") == "/operator/bin:/usr/bin"

    def test_a_later_add_never_overwrites(self, fake_home, agent_root, monkeypatch):
        from settings import get_setting

        monkeypatch.setenv("PATH", "/operator/bin")
        assert cmd_add(["alpha", str(agent_root)]) == 0
        monkeypatch.setenv("PATH", "/some/cron/path")
        assert cmd_add(["beta", str(agent_root)]) == 0
        assert get_setting("wake_path") == "/operator/bin"

    def test_an_operator_set_value_outranks_the_capture(
        self, fake_home, agent_root, monkeypatch
    ):
        from settings import get_setting, load_settings_file

        monkeypatch.setenv("A8S_WAKE_PATH", "/chosen/bin")
        monkeypatch.setenv("PATH", "/operator/bin")
        assert cmd_add(["alpha", str(agent_root)]) == 0
        assert "wake_path" not in load_settings_file()
        assert get_setting("wake_path") == "/chosen/bin"


class TestParseOptionTokens:
    """One parser behind `a8s add`, `a8s remote` and `a8s storage`.

    The three used to disagree: `add` demanded `--KEY=value` and the other two
    demanded `--opt value`. The disagreement was not merely annoying, it was
    silent — `--user=me --password=x` parsed as a single option literally named
    `user=me` whose value was `--password=x`, so the error named an option
    nobody typed and the password never reached the config.
    """

    def test_both_spellings_mean_the_same_thing(self):
        assert parse_option_tokens(["--user", "alice"]) == {"user": "alice"}
        assert parse_option_tokens(["--user=alice"]) == {"user": "alice"}

    def test_the_reported_failure(self):
        # The operator's actual line. Every option must survive it.
        assert parse_option_tokens([
            "--base_url=https://files.example.com",
            "--prefix=_a8s_",
            "--user=alice",
            "--password=s3cret",
        ]) == {
            "base_url": "https://files.example.com",
            "prefix": "_a8s_",
            "user": "alice",
            "password": "s3cret",
        }

    def test_a_flag_is_never_swallowed_as_a_value(self):
        with pytest.raises(ValueError) as e:
            parse_option_tokens(["--base_url", "--user=alice"])
        msg = str(e.value)
        assert "missing value for --base_url" in msg
        assert "--base_url=<value>" in msg  # and how to mean it on purpose

    def test_the_equals_form_can_carry_a_leading_dash(self):
        assert parse_option_tokens(["--prefix=--odd"]) == {"prefix": "--odd"}

    def test_an_empty_value_is_a_value(self):
        assert parse_option_tokens(["--prefix="]) == {"prefix": ""}

    def test_a_value_may_contain_equals(self):
        assert parse_option_tokens(["--token=a=b=c"]) == {"token": "a=b=c"}

    def test_dashes_in_a_key_become_underscores(self):
        assert parse_option_tokens(["--base-url=x"]) == {"base_url": "x"}
        assert parse_option_tokens(["--base-url", "x"]) == {"base_url": "x"}

    def test_aliases_apply_to_both_spellings(self):
        al = {"pass": "password"}
        assert parse_option_tokens(["--pass=x"], aliases=al) == {"password": "x"}
        assert parse_option_tokens(["--pass", "x"], aliases=al) == {"password": "x"}

    def test_a_duplicate_is_refused_across_spellings(self):
        with pytest.raises(ValueError, match="duplicate option: --user"):
            parse_option_tokens(["--user=a", "--user", "b"])

    def test_a_single_dash_says_what_to_type_instead(self):
        with pytest.raises(ValueError, match=r"try --user"):
            parse_option_tokens(["-user", "alice"])

    def test_a_bare_word_is_not_an_option(self):
        with pytest.raises(ValueError, match="expected --<opt>"):
            parse_option_tokens(["user", "alice"])

    def test_a_missing_name_before_equals(self):
        with pytest.raises(ValueError, match="missing option name"):
            parse_option_tokens(["--=alice"])

    def test_a_trailing_option_with_no_value(self):
        with pytest.raises(ValueError, match="missing value for --user"):
            parse_option_tokens(["--user"])

    def test_no_tokens_is_no_options(self):
        assert parse_option_tokens([]) == {}


class TestCmdRemote:
    """Remote management mirrors `cmd_alias`'s shape:
        a8s remote                 — list all
        a8s remote <name>          — show one
        a8s remote <name> <broker> <topic> [--<k> <v> ...]   — add or overwrite
    Removal is `a8s unremote <name>` (parallel to `unalias`)."""

    def test_list_empty(self, fake_home, capsys):
        rc = cmd_remote([])
        assert rc == 0
        assert "no remotes configured" in capsys.readouterr().out

    def test_set_then_list(self, fake_home, capsys):
        rc = cmd_remote(["hub", "mqtt://broker:1883", "a8s/test"])
        assert rc == 0
        cfg = load_network_config()
        assert cfg["remotes"]["hub"]["transport"] == "mqtt"
        assert cfg["remotes"]["hub"]["broker"] == "mqtt://broker:1883"
        assert cfg["remotes"]["hub"]["topic"] == "a8s/test"
        capsys.readouterr()  # discard prior
        cmd_remote([])
        out = capsys.readouterr().out
        assert "hub" in out
        assert "mqtt" in out

    def test_show_one(self, fake_home, capsys):
        cmd_remote(["hub", "mqtt://x", "t"])
        capsys.readouterr()  # discard prior
        rc = cmd_remote(["hub"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "hub: " in out
        assert "mqtt://x" in out

    def test_show_unknown(self, fake_home, capsys):
        rc = cmd_remote(["nope"])
        assert rc == 1
        assert "no remote named" in capsys.readouterr().err

    def test_set_overwrites_existing(self, fake_home, capsys):
        # Unlike alias-add (which is additive), remote-set replaces. Two
        # invocations of `remote <name> <b> <t>` leave only the second.
        cmd_remote(["hub", "mqtt://old", "old-topic"])
        capsys.readouterr()
        rc = cmd_remote(["hub", "mqtt://new", "new-topic", "--user", "alice"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "updated remote hub" in out
        spec = load_network_config()["remotes"]["hub"]
        assert spec["broker"] == "mqtt://new"
        assert spec["topic"] == "new-topic"
        assert spec["user"] == "alice"

    def test_set_passes_arbitrary_options_to_spec(self, fake_home):
        from network import load_secrets_config

        rc = cmd_remote([
            "hub", "mqtts://x", "t",
            "--user", "alice", "--pass", "secret",
            "--keepalive", "120",
        ])
        assert rc == 0
        spec = load_network_config()["remotes"]["hub"]
        # Non-secrets stay in network.json; pass goes to secrets.json.
        assert spec["user"] == "alice"
        assert "pass" not in spec
        assert spec["keepalive"] == "120"
        assert load_secrets_config()["remotes"]["hub"]["pass"] == "secret"

    def test_set_rejects_dangling_option(self, fake_home, capsys):
        rc = cmd_remote(["hub", "mqtt://x", "t", "--user"])
        assert rc == 2
        assert "missing value" in capsys.readouterr().err

    def test_set_rejects_bare_value(self, fake_home, capsys):
        rc = cmd_remote(["hub", "mqtt://x", "t", "alice"])
        assert rc == 2
        assert "expected --<opt>" in capsys.readouterr().err

    def test_set_rejects_duplicate_option(self, fake_home, capsys):
        rc = cmd_remote(["hub", "mqtt://x", "t", "--user", "a", "--user", "b"])
        assert rc == 2
        assert "duplicate option" in capsys.readouterr().err

    def test_set_invalid_name(self, fake_home, capsys):
        rc = cmd_remote(["with space", "mqtt://x", "t"])
        assert rc == 2
        assert "must be alphanumeric" in capsys.readouterr().err

    def test_secret_is_masked_in_show(self, fake_home, capsys):
        cmd_remote(["hub", "mqtts://x", "t", "--pass", "TOPSECRET"])
        capsys.readouterr()
        cmd_remote(["hub"])
        out = capsys.readouterr().out
        assert "TOPSECRET" not in out
        assert "--pass=***" in out

    def test_pass_stored_in_secrets_not_network(self, fake_home):
        from core import secrets_config_path
        from network import load_secrets_config

        cmd_remote(["hub", "mqtts://x", "t", "--user", "alice", "--pass", "TOPSECRET"])
        net = load_network_config()["remotes"]["hub"]
        assert "pass" not in net
        assert net["user"] == "alice"
        secrets = load_secrets_config()["remotes"]["hub"]
        assert secrets["pass"] == "TOPSECRET"
        mode = secrets_config_path().stat().st_mode & 0o777
        assert mode == 0o600

    def test_pass_optional_and_preserved_on_rewrite(self, fake_home):
        from network import load_secrets_config

        cmd_remote(["hub", "mqtt://x", "t", "--pass", "KEEPME"])
        cmd_remote(["hub", "mqtt://y", "t2", "--user", "bob"])
        net = load_network_config()["remotes"]["hub"]
        assert net["broker"] == "mqtt://y"
        assert net["user"] == "bob"
        assert "pass" not in net
        assert load_secrets_config()["remotes"]["hub"]["pass"] == "KEEPME"

    def test_unremote_clears_secrets(self, fake_home):
        from network import load_secrets_config

        cmd_remote(["hub", "mqtt://x", "t", "--pass", "X"])
        cmd_unremote(["hub"])
        assert "hub" not in load_network_config()["remotes"]
        assert "hub" not in load_secrets_config()["remotes"]


class TestCmdUnremote:
    def test_remove(self, fake_home):
        cmd_remote(["hub", "mqtt://x", "t"])
        rc = cmd_unremote(["hub"])
        assert rc == 0
        assert "hub" not in load_network_config()["remotes"]

    def test_unknown(self, fake_home, capsys):
        rc = cmd_unremote(["nope"])
        assert rc == 1
        assert "no remote named" in capsys.readouterr().err

    def test_usage(self, fake_home, capsys):
        rc = cmd_unremote([])
        assert rc == 2
        assert "usage:" in capsys.readouterr().err


class TestCmdTellRemoteRecipient:
    """When remotes are configured, `tell <name>` should accept names that
    don't exist locally — the recipient may live on another cluster and
    the receive-side filter will pick it up there. With no remotes
    configured, an unknown recipient is a hard error (no path)."""

    def _setup_sender(self, fake_home, tmp_path, monkeypatch):
        sender_root = tmp_path / "sender"
        sender_root.mkdir()
        (sender_root / ".outbox").mkdir()
        save_registry({"sender": {"root": str(sender_root)}})
        ensure_mailboxes(Participant("sender", sender_root))
        monkeypatch.chdir(sender_root)
        monkeypatch.setenv(TELL_OUTBOX_DIR_ENV, str(sender_root / ".outbox"))
        return sender_root

    def test_unknown_recipient_with_no_remotes_rejected(self, fake_home, tmp_path, monkeypatch, capsys):
        self._setup_sender(fake_home, tmp_path, monkeypatch)
        rc = cmd_tell(["GHOST", "hi"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "no agent or alias named" in err

    def test_unknown_recipient_with_remotes_accepted(self, fake_home, tmp_path, monkeypatch):
        sender_root = self._setup_sender(fake_home, tmp_path, monkeypatch)
        # Configure a remote so the receive-side filter path is available.
        save_network_config({"remotes": {"hub": {"transport": "mqtt", "broker": "mqtt://x", "topic": "t"}}})
        rc = cmd_tell(["GHOST", "hi from sender"])
        assert rc == 0
        # Outbox file written; the routing pass will publish it. The `to`
        # field preserves the user-typed name (mailing-list semantics).
        outbox_files = list(outbox_dir(sender_root).iterdir())
        assert len(outbox_files) == 1
        import json as _json
        msg = _json.loads(outbox_files[0].read_text())
        assert msg["to"] == "GHOST"
        assert msg["content"] == "hi from sender"


class TestCmdTellNamespace:
    """Issue #148 — colon recipients validate against the namespaces map,
    with the same remote fallback as unknown agents (the binding may live
    on another cluster)."""

    def _setup_sender(self, fake_home, tmp_path, monkeypatch):
        sender_root = tmp_path / "sender"
        sender_root.mkdir()
        (sender_root / ".outbox").mkdir()
        node_root = tmp_path / "node"
        node_root.mkdir()
        save_registry({
            "sender": {"root": str(sender_root)},
            "node": {"root": str(node_root)},
        })
        ensure_mailboxes(Participant("sender", sender_root))
        monkeypatch.chdir(sender_root)
        monkeypatch.setenv(TELL_OUTBOX_DIR_ENV, str(sender_root / ".outbox"))
        return sender_root

    def test_bound_prefix_accepted_to_canonicalizes_prefix_only(self, fake_home, tmp_path, monkeypatch):
        import json as _json
        sender_root = self._setup_sender(fake_home, tmp_path, monkeypatch)
        save_namespaces({"acme": "node"})
        rc = cmd_tell(["ACME:Ops:Phil", "hi"])
        assert rc == 0
        outbox_files = list(outbox_dir(sender_root).iterdir())
        assert len(outbox_files) == 1
        msg = _json.loads(outbox_files[0].read_text())
        # The prefix canonicalizes like any name; the sub-address is verbatim.
        assert msg["to"] == "acme:Ops:Phil"

    def test_unknown_prefix_with_no_remotes_rejected(self, fake_home, tmp_path, monkeypatch, capsys):
        self._setup_sender(fake_home, tmp_path, monkeypatch)
        rc = cmd_tell(["ghost:phil", "hi"])
        assert rc == 1
        assert "no namespace bound for" in capsys.readouterr().err

    def test_unknown_prefix_with_remotes_accepted(self, fake_home, tmp_path, monkeypatch):
        import json as _json
        sender_root = self._setup_sender(fake_home, tmp_path, monkeypatch)
        save_network_config({"remotes": {"hub": {"transport": "mqtt", "broker": "mqtt://x", "topic": "t"}}})
        rc = cmd_tell(["ghost:phil", "hi"])
        assert rc == 0
        msg = _json.loads(next(outbox_dir(sender_root).iterdir()).read_text())
        assert msg["to"] == "ghost:phil"

    def test_empty_sub_address_rejected(self, fake_home, tmp_path, monkeypatch, capsys):
        self._setup_sender(fake_home, tmp_path, monkeypatch)
        save_namespaces({"acme": "node"})
        rc = cmd_tell(["acme:", "hi"])
        assert rc == 1
        assert "empty sub-address" in capsys.readouterr().err

    def test_bare_prefix_routes_with_to_equal_prefix(self, fake_home, tmp_path, monkeypatch):
        import json as _json
        sender_root = self._setup_sender(fake_home, tmp_path, monkeypatch)
        save_namespaces({"acme": "node"})
        rc = cmd_tell(["ACME", "hi"])
        assert rc == 0
        msg = _json.loads(next(outbox_dir(sender_root).iterdir()).read_text())
        assert msg["to"] == "acme"

    def test_bare_prefix_accepted_with_remotes_configured(self, fake_home, tmp_path, monkeypatch):
        import json as _json
        sender_root = self._setup_sender(fake_home, tmp_path, monkeypatch)
        save_namespaces({"acme": "node"})
        save_network_config({"remotes": {"hub": {"transport": "mqtt", "broker": "mqtt://x", "topic": "t"}}})
        rc = cmd_tell(["acme", "hi"])
        assert rc == 0
        msg = _json.loads(next(outbox_dir(sender_root).iterdir()).read_text())
        assert msg["to"] == "acme"

# ---------- storage services (issue #90) ----------


class TestCmdStorage:
    """Mirrors `TestCmdRemote`. Same surface shape, configured under
    `network.json`'s `services` map instead of `remotes`."""

    def test_list_empty(self, fake_home, capsys):
        rc = cmd_storage([])
        assert rc == 0
        assert "no storage services configured" in capsys.readouterr().out

    def test_set_then_list(self, fake_home, capsys):
        rc = cmd_storage(["tempfile", "https://tempfile.org"])
        assert rc == 0
        cfg = load_network_config()
        assert cfg["services"]["tempfile"]["service"] == "tempfile_org"
        assert cfg["services"]["tempfile"]["url"] == "https://tempfile.org"
        capsys.readouterr()
        cmd_storage([])
        out = capsys.readouterr().out
        assert "tempfile" in out
        assert "tempfile_org" in out

    def test_show_one(self, fake_home, capsys):
        cmd_storage(["tempfile", "https://tempfile.org"])
        capsys.readouterr()
        rc = cmd_storage(["tempfile"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "tempfile: " in out
        assert "https://tempfile.org" in out

    def test_show_unknown(self, fake_home, capsys):
        rc = cmd_storage(["nope"])
        assert rc == 1
        assert "no storage named" in capsys.readouterr().err

    def test_set_overwrites_existing(self, fake_home, capsys):
        cmd_storage(["tempfile", "https://tempfile.org", "--expiry_hours", "6"])
        capsys.readouterr()
        rc = cmd_storage(["tempfile", "https://tempfile.org", "--expiry_hours", "24"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "updated storage tempfile" in out
        spec = load_network_config()["services"]["tempfile"]
        assert spec["expiry_hours"] == "24"

    def test_set_passes_arbitrary_options_to_spec(self, fake_home):
        rc = cmd_storage([
            "tempfile", "https://tempfile.org",
            "--expiry_hours", "48", "--timeout_s", "60",
        ])
        assert rc == 0
        spec = load_network_config()["services"]["tempfile"]
        assert spec["expiry_hours"] == "48"
        assert spec["timeout_s"] == "60"

    def test_pass_is_an_alias_for_password(self, fake_home):
        # `a8s remote` takes --pass, so the same finger habit reaches here.
        rc = cmd_storage([
            "fm", "webdav://dav.example.com/dav",
            "--base-url", "https://files.example.com",
            "--user", "alice@example.com", "--pass", "s3cret",
        ])
        assert rc == 0
        spec = load_network_config()["services"]["fm"]
        assert "pass" not in spec and "password" not in spec
        merged = merge_spec_secrets("services", "fm", dict(spec))
        assert merged["password"] == "s3cret"

    def test_blank_prefix_means_no_prefix(self, fake_home):
        rc = cmd_storage([
            "fm", "webdav://dav.example.com/dav/_a8s_",
            "--base-url", "https://files.example.com/_a8s_", "--prefix", "",
        ])
        assert rc == 0
        assert load_network_config()["services"]["fm"]["prefix"] == ""

    def test_set_rejects_unknown_url(self, fake_home, capsys):
        rc = cmd_storage(["weird", "https://example.com"])
        assert rc == 2
        assert "no storage service matches URL" in capsys.readouterr().err

    def test_set_rejects_dangling_option(self, fake_home, capsys):
        rc = cmd_storage(["tempfile", "https://tempfile.org", "--expiry_hours"])
        assert rc == 2
        assert "missing value" in capsys.readouterr().err

    def test_set_rejects_bare_value(self, fake_home, capsys):
        rc = cmd_storage(["tempfile", "https://tempfile.org", "12"])
        assert rc == 2
        assert "expected --<opt>" in capsys.readouterr().err

    def test_set_rejects_duplicate_option(self, fake_home, capsys):
        rc = cmd_storage([
            "tempfile", "https://tempfile.org",
            "--expiry_hours", "6", "--expiry_hours", "24",
        ])
        assert rc == 2
        assert "duplicate option" in capsys.readouterr().err

    def test_set_invalid_name(self, fake_home, capsys):
        rc = cmd_storage(["with space", "https://tempfile.org"])
        assert rc == 2
        assert "must be alphanumeric" in capsys.readouterr().err

    @pytest.mark.parametrize("flag", ["-h", "--help", "help"])
    def test_help_prints_usage_to_stdout(self, fake_home, capsys, flag):
        rc = cmd_storage([flag])
        assert rc == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        # The kinds and their options are the whole point — an unsupported
        # option is only discoverable from here.
        for kind in ("tempfile_org", "s3", "file_sync", "webdav"):
            assert kind in captured.out
        assert "--base-url" in captured.out

    def test_set_accepts_dashed_option_names(self, fake_home):
        rc = cmd_storage([
            "drive", "file:///tmp/drive-sync",
            "--base-url", "https://cdn.example/a8s",
        ])
        assert rc == 0
        # Services declare options as Python identifiers; the CLI spelling
        # must not decide whether the config loads.
        assert load_network_config()["services"]["drive"]["base_url"] == (
            "https://cdn.example/a8s"
        )

    def test_set_rejects_unknown_option_at_write_time(self, fake_home, capsys):
        rc = cmd_storage([
            "drive", "file:///tmp/drive-sync",
            "--base-url", "https://cdn.example/a8s", "--bogus", "x",
        ])
        assert rc == 2
        assert "unknown option(s) bogus" in capsys.readouterr().err
        assert "drive" not in load_network_config()["services"]

    def test_set_rejects_missing_required_option(self, fake_home, capsys):
        rc = cmd_storage(["drive", "file:///tmp/drive-sync"])
        assert rc == 2
        assert "base_url is required" in capsys.readouterr().err
        assert "drive" not in load_network_config()["services"]

    def test_password_goes_to_secrets_not_network_json(self, fake_home, capsys):
        from network import load_secrets_config

        rc = cmd_storage([
            "fm", "webdav://dav.example.com/dav/a8s",
            "--base-url", "https://files.example.com/a8s",
            "--user", "me@example.com", "--password", "hunter2",
        ])
        assert rc == 0
        spec = load_network_config()["services"]["fm"]
        assert "password" not in spec
        assert spec["user"] == "me@example.com"
        assert load_secrets_config()["services"]["fm"]["password"] == "hunter2"
        assert "hunter2" not in capsys.readouterr().out

    def test_configured_service_sees_its_secret(self, fake_home):
        from network import load_services

        cmd_storage([
            "fm", "webdav://dav.example.com/dav/a8s",
            "--base-url", "https://files.example.com/a8s",
            "--user", "me@example.com", "--password", "hunter2",
        ])
        svc = load_services()[0]
        assert svc.id == "fm"
        assert svc._auth_header() == "Basic bWVAZXhhbXBsZS5jb206aHVudGVyMg=="


class TestCmdUnstorage:
    def test_remove(self, fake_home):
        cmd_storage(["tempfile", "https://tempfile.org"])
        rc = cmd_unstorage(["tempfile"])
        assert rc == 0
        assert "tempfile" not in load_network_config()["services"]

    def test_unknown(self, fake_home, capsys):
        rc = cmd_unstorage(["nope"])
        assert rc == 1
        assert "no storage named" in capsys.readouterr().err

    def test_usage(self, fake_home, capsys):
        rc = cmd_unstorage([])
        assert rc == 2
        assert "usage:" in capsys.readouterr().err


# ---------- join_args (FILE:-lifting argv joiner) ----------


class TestJoinTellArgs:
    """`tell` accepts the message body as one or more argv elements. An LLM
    that splits the FILE: tag onto its own argument used to silently lose
    the attachment because the joined string had no newline before FILE:.
    `join_args` lifts FILE:-leading argv elements onto their own line so
    trailing-FILE: detection in `_split_content_and_files` recognizes them."""

    def test_plain_join_unchanged(self):
        from tell import join_args

        assert join_args(["hello", "world"]) == "hello world"

    def test_single_arg_unchanged(self):
        from tell import join_args

        assert join_args(["just a message"]) == "just a message"

    def test_file_promoted_to_own_line(self):
        from tell import join_args

        assert join_args(["msg", "FILE: ./x"]) == "msg\nFILE: ./x"

    def test_bare_file_only(self):
        from tell import join_args

        assert join_args(["FILE: ./x"]) == "FILE: ./x"

    def test_multiple_files(self):
        from tell import join_args

        assert join_args(["body", "FILE: ./a", "FILE: ./b"]) == "body\nFILE: ./a\nFILE: ./b"

    def test_file_with_leading_whitespace_still_detected(self):
        from tell import join_args

        assert join_args(["msg", "  FILE: ./x"]) == "msg\nFILE: ./x"

    def test_file_substring_in_body_unchanged(self):
        from tell import join_args

        assert join_args(["see FILE: x in middle"]) == "see FILE: x in middle"


class TestCmdTellWithSplitFileArg:
    """End-to-end: `cmd_tell` with FILE: as a separate argv element should
    produce an outbox message with the file extracted."""

    def test_split_file_arg_extracts_attachment(self, fake_home, tmp_path, monkeypatch):
        sender_root = tmp_path / "sender"
        sender_root.mkdir()
        (sender_root / ".outbox").mkdir()
        save_registry({"sender": {"root": str(sender_root)}, "alice": {"root": str(tmp_path / "alice")}})
        (tmp_path / "alice").mkdir()
        ensure_mailboxes(Participant("sender", sender_root))
        monkeypatch.chdir(sender_root)
        monkeypatch.setenv(TELL_OUTBOX_DIR_ENV, str(sender_root / ".outbox"))
        (sender_root / "report.pdf").write_text("doc")

        rc = cmd_tell(["alice", "Here is the doc.", "FILE: ./report.pdf"])
        assert rc == 0
        outbox_files = list(outbox_dir(sender_root).glob("*.json"))
        assert len(outbox_files) == 1
        import json as _json
        msg = _json.loads(outbox_files[0].read_text())
        assert msg["content"] == "Here is the doc."
        assert len(msg["files"]) == 1
        assert "path" not in msg["files"][0]
        assert msg["files"][0]["filename"] == "report.pdf"
        assert (outbox_bundle_dir(outbox_dir(sender_root), msg["id"]) / "report.pdf").is_file()


class TestCmdLogs:
    def test_single_agent_preserves_append_order(self, fake_home, tmp_path, capsys):
        root = tmp_path / "x"; root.mkdir()
        save_registry({"claude": {"root": str(root)}})
        log = agent_log_path("claude")
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            "2026-01-01T12:00:02Z later timestamp first in file\n"
            "2026-01-01T12:00:01Z earlier timestamp second in file\n"
            "legacy line without timestamp prefix\n"
        )
        assert cmd_logs(["claude"]) == 0
        out = capsys.readouterr().out
        assert out.splitlines() == [
            "2026-01-01T12:00:02Z later timestamp first in file",
            "2026-01-01T12:00:01Z earlier timestamp second in file",
            "legacy line without timestamp prefix",
        ]

    def test_multi_agent_merge_sorts_by_timestamp(self, fake_home, tmp_path, capsys):
        a_root = tmp_path / "a"; a_root.mkdir()
        b_root = tmp_path / "b"; b_root.mkdir()
        save_registry({"claude": {"root": str(a_root)}, "gemini": {"root": str(b_root)}})
        agent_log_path("claude").parent.mkdir(parents=True, exist_ok=True)
        agent_log_path("gemini").parent.mkdir(parents=True, exist_ok=True)
        agent_log_path("claude").write_text("2026-01-01T12:00:03Z from claude\n")
        agent_log_path("gemini").write_text("2026-01-01T12:00:01Z from gemini\n")
        assert cmd_logs(["claude", "gemini"]) == 0
        out = capsys.readouterr().out.splitlines()
        assert out == [
            "2026-01-01T12:00:01Z from gemini",
            "2026-01-01T12:00:03Z from claude",
        ]

    def test_tail_keeps_last_lines_of_single_agent_log(self, fake_home, tmp_path, capsys):
        root = tmp_path / "x"; root.mkdir()
        save_registry({"claude": {"root": str(root)}})
        log = agent_log_path("claude")
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("line1\nline2\nline3\n")
        assert cmd_logs(["claude", "--tail", "2"]) == 0
        assert capsys.readouterr().out.splitlines() == ["line2", "line3"]


class TestCmdTrace:
    def test_prints_correlated_boundaries(self, fake_home, capsys):
        from txlog import log
        from ulid import new as new_ulid

        msg_id = new_ulid()
        log("PUBLISHED", msg_id=msg_id, sender="alice", recipient="bob", remote="mqtt")
        log(
            "DELIVERY_RECEIPT",
            msg_id=msg_id,
            sender="alice",
            recipient="bob",
            remote="mqtt",
            detail="inbox_write",
        )

        assert cmd_trace([msg_id.lower()]) == 0
        output = capsys.readouterr().out
        assert f"trace {msg_id}" in output
        assert "PUBLISHED" in output
        assert "DELIVERY_RECEIPT" in output
        assert "detail=inbox_write" in output

    def test_rejects_invalid_id(self, fake_home, capsys):
        assert cmd_trace(["not-a-ulid"]) == 2
        assert "usage: a8s trace <ULID>" in capsys.readouterr().err

    def test_reports_no_events(self, fake_home, capsys):
        from ulid import new as new_ulid

        msg_id = new_ulid()
        assert cmd_trace([msg_id]) == 1
        assert f"no transaction events for {msg_id}" in capsys.readouterr().err
