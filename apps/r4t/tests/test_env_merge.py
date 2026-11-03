"""Issue 382 Part A, round 7: environment names have ONE canonical key.

Windows environment names are case-insensitive, so a rig `env` is canonicalized
(upper case, as Python's own `os.environ` does) when it is loaded, every final
environment is built by `rig.merge_env`, and every reader that is handed an env
dict reads it through the same view. POSIX names stay exactly as written.

Windows is simulated by patching `sys.platform`; stubs and temporary folders
only, no engine, network or real account.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

import engines
import rig as rig_mod
import runbook
from engines import copilot
from rig import engine_home, load_rig_config, merge_env, resolve_launch

HEAD = (
    "## Roster\n\n### Lead\n- **Rig:** guarded\n- **Leader:** yes\n\n"
    "## Rigs\n\n### guarded\n- **Engine:** copilot\n- **MCP:** off\n"
)


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")


@pytest.fixture
def world(tmp_path, monkeypatch):
    router, account, member = tmp_path / "router", tmp_path / "account", tmp_path / "member"
    for p in (router / ".copilot", account, member):
        p.mkdir(parents=True)
    (router / ".copilot" / "config.json").write_text(
        json.dumps({"copilotTokens": "DUMMY_ROUTER"}), encoding="utf-8")
    (account / "config.json").write_text(
        json.dumps({"copilotTokens": "DUMMY_ACCOUNT"}), encoding="utf-8")
    (account / "session-state" / "abc").mkdir(parents=True)
    monkeypatch.setattr(os, "environ", {"HOME": str(router), "PATH": "/usr/bin:/bin"})
    return type("W", (), {"tmp": tmp_path, "router": router, "account": account,
                          "member": member})


def chosen_rig(world, grants, lines):
    cfg = world.tmp / "rigs.json"
    cfg.write_text(json.dumps(
        {"env_allow": grants, "deny_floor": {"copilot": ["shell(touch)"]}}), encoding="utf-8")
    (world.member / runbook.RUNBOOK_NAME).write_text(
        HEAD + "".join(f"- **Env:** {x}\n" for x in lines), encoding="utf-8")
    book = runbook.load_for_root(world.member, validate=False)
    return load_rig_config(cfg).rig_for(book.roster.find("Lead"))


# -- the helper ---------------------------------------------------------------


class TestMergeEnv:
    def test_a_later_layer_replaces_an_earlier_one_whatever_the_case(self, windows):
        assert merge_env({"path": "a", "X": "1"}, {"PATH": "b"}, None) == {"PATH": "b", "X": "1"}
        assert merge_env({"PATH": "a"}, {"Path": "b"}) == {"PATH": "b"}

    def test_the_result_has_one_canonical_key_per_name(self, windows):
        merged = merge_env({"Copilot_Home": "/a", "COPILOT_HOME": "/b"}, {"x": "1"})
        assert merged == {"COPILOT_HOME": "/b", "X": "1"}

    def test_a_caller_dict_that_is_not_canonical_is_canonicalized(self, windows):
        assert merge_env({"home": "/h"}) == {"HOME": "/h"}

    def test_posix_names_are_never_renamed_and_stay_distinct(self):
        merged = merge_env({"HOME": "/h"}, {"home": "/x"}, {"HOME": "/y"})
        assert merged == {"HOME": "/y", "home": "/x"}


# -- the rig env is canonical at load, and a same-layer alias is refused -------


class TestRigEnvAtLoad:
    SPEC = {"preset": "claude", "invoke": None}

    def spec(self, env):
        return {"preset": "claude", "invoke": rig_mod.build_preset_invoke("claude"), "env": env}

    def test_windows_machine_rig_env_is_stored_upper_case(self, windows):
        rig = rig_mod.rig_from_spec("m", self.spec({"ENABLE_prompt_caching_1h": "1"}))
        assert rig.error is None and rig.env == {"ENABLE_PROMPT_CACHING_1H": "1"}

    def test_windows_two_names_that_differ_only_by_case_are_refused_with_one_line(self, windows):
        rig = rig_mod.rig_from_spec("m", self.spec({"Path": "/a", "PATH": "/b"}))
        assert rig.error and "Path and PATH name the same variable" in rig.error
        assert "\n" not in rig.error

    def test_windows_a_runbook_rig_with_two_spellings_is_refused(self, windows, world):
        chosen, error, _ = chosen_rig(world, ["PATH"], ["Path=/a", "PATH=/b"])
        assert chosen is None and "same variable" in error

    def test_posix_keeps_both_names_as_written(self):
        rig = rig_mod.rig_from_spec("m", self.spec({"home": "/a", "HOME": "/b"}))
        assert rig.error is None and rig.env == {"home": "/a", "HOME": "/b"}

    def test_the_config_writer_replaces_and_drops_every_alias_on_windows(self, windows, tmp_path):
        cfg = tmp_path / "r.json"
        rig_mod.add_preset_rig(cfg, "m", "claude")
        rig_mod.set_rig_value(cfg, "m", "env.Foo", "1")
        rig_mod.set_rig_value(cfg, "m", "env.FOO", "2")
        assert json.loads(cfg.read_text())["m"]["env"] == {"FOO": "2"}
        assert rig_mod.unset_rig_value(cfg, "m", "env.foo")
        assert "env" not in json.loads(cfg.read_text())["m"]


# -- end to end: the admitted alias reaches every reader -----------------------


class TestAdmittedAliasReachesTheReaders:
    @pytest.mark.parametrize("spelling", ["COPILOT_HOME", "copilot_home", "Copilot_Home"])
    def test_every_reader_sees_the_rigs_home(self, windows, world, spelling):
        chosen, error, _ = chosen_rig(
            world, ["COPILOT_HOME"], [f"{spelling}={world.account}"])
        assert chosen is not None, error
        launch = resolve_launch("", merge_env(os.environ, chosen.env), cwd=world.member,
                                isolated=True)
        assert list(launch.env).count("COPILOT_HOME") == 1
        assert engine_home("copilot", launch.env) == world.account
        assert copilot.token(launch.env) == "DUMMY_ACCOUNT"
        assert copilot.account_scoped(launch.env)
        assert copilot.account_key(launch.env) not in ("", None)
        assert copilot.session_exists("abc", launch.env)

    def test_the_path_the_rig_set_is_the_path_the_launch_looks_up(self, windows, world):
        tools = world.tmp / "tools"
        tools.mkdir()
        stub = tools / "tool.xyz"
        stub.write_text("#!/bin/sh\n", encoding="utf-8")
        chosen, error, _ = chosen_rig(world, ["PATH"], [f"Path={tools}"])
        assert chosen is not None, error
        # The caller's own lowercase `pathext` is the PATHEXT the lookup reads.
        launch = resolve_launch(
            "tool", merge_env({"pathext": ".XYZ"}, os.environ, chosen.env), cwd=world.member)
        assert launch.env["PATH"] == str(tools) and "Path" not in launch.env
        assert launch.binary == str(stub)

    def test_a_lowercase_pathext_from_the_caller_is_the_one_the_launch_reads(self, windows, world):
        launch = resolve_launch("", {"pathext": ".EXE;.CMD", "Path": "/x"}, cwd=world.member,
                                isolated=True)
        assert launch.env["PATHEXT"] == ".EXE;.CMD" and launch.env["PATH"] == "/x"

    def test_posix_home_and_HOME_stay_distinct_and_the_exact_name_wins(self, world):
        env = {"HOME": str(world.router), "copilot_home": str(world.account)}
        assert engine_home("copilot", env) == world.router / ".copilot"
        assert copilot.token(env) == "DUMMY_ROUTER"
        assert not copilot.account_scoped(env)


# -- readers that never build a Launch ----------------------------------------


class TestReaderOnlyPaths:
    @pytest.fixture
    def seen(self, monkeypatch):
        calls = []
        monkeypatch.setattr(copilot.shutil, "which", lambda name: None)
        monkeypatch.setattr(copilot, "_direct_user", lambda auth: calls.append(auth) or {
            "quota_snapshots": {"premium_interactions": {"percent_remaining": 40}}})
        return calls

    def test_fuel_reads_the_rigs_account_through_a_lowercase_alias(self, windows, world, seen):
        payload = engines.fuel("copilot", env={"HOME": str(world.router),
                                               "copilot_home": str(world.account)})
        assert seen == ["DUMMY_ACCOUNT"] and payload["fuel"] == pytest.approx(0.4)

    def test_quota_reads_the_rigs_account_through_a_lowercase_alias(self, windows, world, seen):
        engines.quota("copilot", env={"HOME": str(world.router),
                                      "copilot_home": str(world.account)})
        assert seen == ["DUMMY_ACCOUNT"]

    def test_a_lowercase_token_alias_makes_the_account_scoped(self, windows, world):
        env = {"HOME": str(world.router), "gh_token": "synthetic"}
        assert copilot.token(env) == "synthetic" and copilot.account_scoped(env)

    def test_the_session_lookup_follows_a_lowercase_alias(self, windows, world):
        env = {"HOME": str(world.router), "copilot_home": str(world.account)}
        assert copilot.session_exists("abc", env)
        assert not copilot.session_exists("abc", {"HOME": str(world.router)})

    def test_the_claude_transcript_home_follows_a_lowercase_alias(self, windows, world):
        env = {"HOME": str(world.router), "claude_config_dir": str(world.account)}
        assert engine_home("claude", env) == world.account
        assert rig_mod.child_home({"home": "/h"}) == "/h"
        assert rig_mod.home_env_problem({"codex_home": "relative"})

    def test_posix_readers_stay_exact(self, world):
        env = {"HOME": str(world.router), "claude_config_dir": str(world.account)}
        assert engine_home("claude", env) == world.router / ".claude"
        assert not rig_mod.home_env_problem({"codex_home": "relative"})


# -- every final-env merge goes through the helper (source scan) ---------------

APP = Path(__file__).resolve().parent.parent
RAW_MERGES = [
    r"\{\s*\*\*\s*(os\.environ|env\b|\(env|[\w.]*\.env\b)",   # {**os.environ, **rig.env}
    r"\.update\(\s*[\w.]*\benv\s*\)",                          # env.update(rig.env)
    r"\.update\(\s*(identity|extra_env|mcp_env|[\w.]*instrument_env|[\w.]*to_env)\b",
    r"\*\*\s*self\.pass_env\(\)",
]
MERGE_SITES = {   # file -> function names whose final env is built by merge_env
    "dispatch.py": ["run_harness", "_probe_resolves"],
    "engine_memory.py": ["writer"],
    "knowledge.py": [],
    "r4t.py": [],
    "engines/run.py": [],
    "engines/resident.py": [],
    "engines/muse_resident.py": [],
    "engines/copilot.py": [],
    "rig.py": ["resolve_launch"],
}


def sources():
    for path in sorted(APP.glob("*.py")) + sorted((APP / "engines").glob("*.py")):
        yield path.relative_to(APP).as_posix(), path.read_text(encoding="utf-8")


def test_no_environment_layer_is_merged_by_hand():
    found = []
    for name, text in sources():
        for number, line in enumerate(text.splitlines(), 1):
            code = line.split("#", 1)[0]
            if any(re.search(p, code) for p in RAW_MERGES):
                found.append(f"{name}:{number}: {line.strip()}")
    assert not found, "merge environment layers with rig.merge_env:\n" + "\n".join(found)


def test_every_known_merge_site_calls_the_helper():
    texts = dict(sources())
    for name in MERGE_SITES:
        assert "merge_env(" in texts[name], name
    for func in ("run_harness", "_probe_resolves"):
        body = texts["dispatch.py"].split(f"def {func}(", 1)[1].split("\ndef ", 1)[0]
        assert "merge_env(" in body, func
