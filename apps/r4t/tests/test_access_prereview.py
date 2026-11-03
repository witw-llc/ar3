"""Issue 382 Part A, pre-review of 63f24ac: the trust model for in-repo `Env:`,
the trusted wrapper, the Windows lookup, the floor keyed by binary, the shadow
of an invalid machine rig, dangling links, the version gate by name, engine
homes, `engine check` through the launch, and the P3s.

Stub binaries only; no engine, sudo or docker is ever started for real.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

import dispatch
import engines
import isolate
import r4t
import rig as rig_mod
import runbook
import state
from conftest import launch_for, write_path_executable
from engines import check as engine_check
from engines import copilot
from rig import RigError, load_deny_floor, load_rig_config
from roster import Member

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX stubs and chmod")


def shutil_which(name):
    import shutil

    return shutil.which(name)
R4T_DIR = Path(__file__).resolve().parents[1]
FLOOR = "Bash(rm:*)"

RUNBOOK_HEAD = (
    "## Roster\n\n### Lead\n- **Rig:** guarded\n- **Leader:** yes\n\n"
    "## Rigs\n\n### guarded\n"
)


def stub(path: Path, version: str, record: Path, *, help_text: str = "") -> Path:
    """An executable that records its file, argv, cwd and HOME on every call."""
    return write_path_executable(
        path.parent, path.name,
        "import json, os, sys\n"
        f"with open({str(record)!r}, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps({'file': os.path.abspath(sys.argv[0]), 'argv': sys.argv[1:], "
        "'cwd': os.path.realpath(os.getcwd()), 'home': os.environ.get('HOME')}) + '\\n')\n"
        "if '--version' in sys.argv:\n"
        f"    print({version!r})\n"
        "    sys.exit(0)\n"
        "if '--help' in sys.argv:\n"
        f"    print(os.environ.get('HELP_TEXT', {help_text!r}))\n"
        "    sys.exit(0)\n"
        "print('ok')\n",
    )


def rows(record: Path) -> list[dict]:
    if not record.exists():
        return []
    return [json.loads(x) for x in record.read_text(encoding="utf-8").splitlines() if x]


def node_with(tmp_path: Path, block: str, name: str = "node") -> Path:
    node = tmp_path / name
    node.mkdir()
    (node / runbook.RUNBOOK_NAME).write_text(RUNBOOK_HEAD + block + "\n", encoding="utf-8")
    return node


def rig_of(node: Path):
    return runbook.load_for_root(node, validate=False).rigs["guarded"]


# --- A: in-repo Env below bypass is an ALLOWLIST ----------------------------------

REFUSED_NAMES = [
    # runtime and loader initialization
    "NODE_OPTIONS", "BASH_ENV", "PYTHONPATH", "PYTHONSTARTUP", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES",
    # a shell wrapper or script an engine runs
    "CLAUDE_CODE_SHELL_PREFIX", "CLAUDE_ENV_FILE",
    # executable, home, permissions
    "PATH", "PATHEXT", "HOME", "XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "COPILOT_HOME",
    "COPILOT_ALLOW_ALL", "OPENCODE_PERMISSION", "OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT",
    # endpoint, proxy, certificate and credential routing
    "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "GH_TOKEN", "GITHUB_TOKEN",
    "COPILOT_GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
    "REQUESTS_CA_BUNDLE",
    # a name nobody has listed
    "SOME_RANDOM_NAME_7",
]


class Machine:
    """A machine config on disk, beside a node whose runbook is the repo's."""

    def __init__(self, tmp_path, floor=None, **top):
        self.dir = tmp_path
        self.cfg = tmp_path / "machine-rigs.json"
        data = {"deny_floor": floor if floor is not None else {"claude": [FLOOR]}, **top}
        self.cfg.write_text(json.dumps(data), encoding="utf-8")
        self.config = load_rig_config(self.cfg)

    def resolve(self, node):
        book = runbook.load_for_root(node, validate=False)
        return self.config.rig_for(book.roster.find("Lead"))


@posix_only
class TestInRepoEnvAllowlist:
    """Below bypass a checked-in `Env:` may set only `IN_REPO_ENV_SAFE` and the
    names the machine grants in `env_allow`. The check runs when the rig is
    resolved; a refused rig never reaches --help or --version."""

    @pytest.fixture
    def world(self, tmp_path, monkeypatch):
        bins = tmp_path / "bin"
        record = tmp_path / "invocations.jsonl"
        for engine in ("claude", "copilot"):
            stub(bins / engine, "2.1.316" if engine == "claude" else "1.0.105", record)
        monkeypatch.setenv("PATH", f"{bins}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.delenv("TELL_OUTBOX_DIR", raising=False)
        return SimpleNamespace(tmp=tmp_path, record=record, bins=bins)

    def node(self, world, engine, env_lines, name="node"):
        block = f"- **Engine:** {engine}\n- **MCP:** off\n" + "".join(
            f"- **Env:** {line}\n" for line in env_lines
        )
        return node_with(world.tmp, block, name)

    @pytest.mark.parametrize("engine", ["claude", "copilot"])
    @pytest.mark.parametrize("name", REFUSED_NAMES)
    def test_everything_else_is_refused_before_any_probe(self, world, engine, name):
        floor = {engine: [FLOOR if engine == "claude" else "shell(touch)"]}
        machine = Machine(world.tmp, floor)
        node = self.node(world, engine, [f"{name}=/repo/x"])
        chosen, error, _ = machine.resolve(node)
        assert chosen is None, "the rig was accepted"
        assert error and "guarded" in error and name in error
        assert "machine rig" in error and "env_allow" in error
        assert error.count("\n") == 0
        assert rows(world.record) == []   # no --version, no --help, no turn

    @pytest.mark.parametrize("engine", ["claude", "copilot", "codex", "cursor", "opencode", "ollama-claude"])
    def test_the_safe_name_is_accepted_for_every_engine_with_one_rule(self, world, engine):
        extra = " --model m" if engine.startswith("ollama") else ""
        node = self.node(world, engine + extra, ["ENABLE_PROMPT_CACHING_1H=1"])
        machine = Machine(world.tmp, {})
        chosen, error, _ = machine.resolve(node)
        assert error is None and chosen.env == {"ENABLE_PROMPT_CACHING_1H": "1"}

    def test_every_safe_list_name_is_accepted(self, world):
        assert rig_mod.IN_REPO_ENV_SAFE == {"ENABLE_PROMPT_CACHING_1H"}
        for name in sorted(rig_mod.IN_REPO_ENV_SAFE):
            node = self.node(world, "claude", [f"{name}=1"], f"n-{name}")
            chosen, error, _ = Machine(world.tmp, {}).resolve(node)
            assert error is None, (name, error)

    def test_no_name_that_routes_runs_or_loads_is_on_the_safe_list(self):
        for name in REFUSED_NAMES:
            assert not rig_mod.in_repo_env_safe(name), name

    def test_one_refused_name_refuses_the_whole_rig_even_beside_a_safe_one(self, world):
        node = self.node(world, "claude", ["ENABLE_PROMPT_CACHING_1H=1", "NODE_OPTIONS=--require=/x"])
        chosen, error, _ = Machine(world.tmp).resolve(node)
        assert chosen is None and "NODE_OPTIONS" in error and "ENABLE_PROMPT" not in error

    def test_at_bypass_everything_is_accepted(self, world):
        node = self.node(world, "claude", [f"{n}=/x" for n in REFUSED_NAMES[:10]], "trusted")
        state.stamp_trust("trusted", "bypass")
        book = runbook.load_for_root(node, node="trusted", validate=False)
        chosen, error, _ = Machine(world.tmp).config.rig_for(book.roster.find("Lead"))
        assert error is None and set(chosen.env) == {n for n in REFUSED_NAMES[:10]}

    def test_a_machine_rig_is_not_held_to_the_repo_rule(self, world):
        spec = {"preset": "claude", "invoke": rig_mod.build_preset_invoke("claude"),
                "env": {"NODE_OPTIONS": "--max-old-space-size=4096", "HTTPS_PROXY": "http://proxy:3128"}}
        rig = rig_mod.rig_from_spec("m", spec)
        assert rig.error is None
        assert Machine(world.tmp).config.effective_rig(rig)[0] is not None

    # -- the operator's grant -------------------------------------------------

    def test_a_name_in_the_machine_env_allow_is_accepted_and_the_repo_chooses_its_value(self, world):
        machine = Machine(world.tmp, env_allow=["NODE_OPTIONS"])
        node = self.node(world, "claude", ["NODE_OPTIONS=--max-old-space-size=2048"])
        chosen, error, _ = machine.resolve(node)
        assert error is None and chosen.env["NODE_OPTIONS"] == "--max-old-space-size=2048"
        # ...and ONLY that name.
        other = self.node(world, "claude", ["NODE_OPTIONS=x", "HTTPS_PROXY=http://p"], "other")
        chosen, error, _ = machine.resolve(other)
        assert chosen is None and "HTTPS_PROXY" in error and "NODE_OPTIONS" not in error

    # -- names match exactly on POSIX -------------------------------------------

    def test_a_lowercase_grant_does_not_admit_the_uppercase_name(self, world):
        machine = Machine(world.tmp, env_allow=["node_options"])
        node = self.node(world, "claude", ["NODE_OPTIONS=--require=/repo/preload.cjs"])
        chosen, error, _ = machine.resolve(node)
        assert chosen is None and "NODE_OPTIONS" in error and "env_allow" in error
        assert rows(world.record) == []   # zero stub calls: no --version, no --help, no turn

    def test_a_grant_admits_exactly_its_own_spelling(self, world):
        machine = Machine(world.tmp, env_allow=["NODE_OPTIONS"])
        node = self.node(world, "claude", ["NODE_OPTIONS=--max-old-space-size=2048"])
        chosen, error, _ = machine.resolve(node)
        assert error is None and set(chosen.env) == {"NODE_OPTIONS"}
        other = self.node(world, "claude", ["node_options=x"], "other")
        chosen, error, _ = machine.resolve(other)
        assert chosen is None and "node_options" in error

    def test_the_lowercase_grant_admits_only_the_lowercase_name(self, world):
        machine = Machine(world.tmp, env_allow=["node_options"])
        node = self.node(world, "claude", ["node_options=x"])
        chosen, error, _ = machine.resolve(node)
        assert error is None and set(chosen.env) == {"node_options"}

    def test_grants_that_differ_only_by_case_stay_distinct(self):
        assert rig_mod.parse_env_allow(["NODE_OPTIONS", "node_options", "NODE_OPTIONS"]) == [
            "NODE_OPTIONS", "node_options"]

    def test_a_wrong_case_safe_name_is_refused(self, world):
        assert rig_mod.in_repo_env_safe("ENABLE_PROMPT_CACHING_1H")
        assert not rig_mod.in_repo_env_safe("enable_prompt_caching_1h")
        node = self.node(world, "claude", ["enable_prompt_caching_1h=1"])
        chosen, error, _ = Machine(world.tmp, {}).resolve(node)
        assert chosen is None and "enable_prompt_caching_1h" in error
        assert rows(world.record) == []

    @pytest.mark.parametrize(
        "value",
        ["NODE_OPTIONS", "NODE_*", ["NODE_*"], ["*"], [""], ["NODE OPTIONS"], [1], [None],
         {"NODE_OPTIONS": True}, "A,B", ["OK", "bad-name"], 5],
        ids=repr,
    )
    def test_an_invalid_env_allow_refuses_every_in_repo_env_with_one_line(self, world, value):
        machine = Machine(world.tmp, env_allow=value)
        assert machine.config.env_allow == [] and machine.config.env_allow_error
        node = self.node(world, "claude", ["ENABLE_PROMPT_CACHING_1H=1"])
        chosen, error, _ = machine.resolve(node)
        assert chosen is None and "env_allow" in error and "\n" not in error
        assert rows(world.record) == []

    def test_an_invalid_env_allow_leaves_a_rig_without_env_alone(self, world):
        machine = Machine(world.tmp, env_allow="bad")
        node = self.node(world, "claude", [])
        chosen, error, _ = machine.resolve(node)
        assert error is None and chosen is not None

    def test_an_unreadable_machine_config_is_not_read_as_no_grants(self, world):
        folder = world.tmp / "closed"
        folder.mkdir()
        cfg = folder / "rigs.json"
        cfg.write_text(json.dumps({"env_allow": ["NODE_OPTIONS"]}), encoding="utf-8")
        folder.chmod(0)
        try:
            if os.geteuid() == 0:
                pytest.skip("root reads everything")
            with pytest.raises(RigError, match="cannot read rig config"):
                load_rig_config(cfg)
        finally:
            folder.chmod(0o700)

    def test_env_allow_is_a_reserved_key_not_a_rig_name(self, tmp_path):
        with pytest.raises(RigError, match="reserved"):
            rig_mod.add_preset_rig(tmp_path / "r.json", "env_allow", "claude")

    # -- a runbook can never add a grant ----------------------------------------

    def test_a_runbook_rig_key_that_names_env_allow_is_refused(self, world):
        block = ("- **Engine:** claude\n- **MCP:** off\n- **Env allow:** NODE_OPTIONS\n"
                 "- **Env:** NODE_OPTIONS=--require=/x\n")
        node = node_with(world.tmp, block, "keyed")
        book = runbook.load_for_root(node, validate=False)
        assert book.rigs["guarded"].error and "envallow" in book.rigs["guarded"].error
        chosen, error, _ = Machine(world.tmp).resolve(node)
        assert chosen is None
        assert rows(world.record) == []

    def test_a_runbook_rig_that_shadows_a_machine_rig_gains_no_grant_from_it(self, world):
        machine = Machine(world.tmp, guarded={
            "preset": "claude", "invoke": rig_mod.build_preset_invoke("claude"),
            "env_allow": ["NODE_OPTIONS"], "env": {"NODE_OPTIONS": "--max-old-space-size=1"},
        })
        assert machine.config.env_allow == []
        node = self.node(world, "claude", ["NODE_OPTIONS=--require=/repo/preload.cjs"])
        chosen, error, _ = machine.resolve(node)
        assert chosen is None and "NODE_OPTIONS" in error
        assert rows(world.record) == []

    def test_an_extends_chain_that_sets_env_allow_gains_nothing(self, world):
        base = world.tmp / "base.md"
        base.write_text("---\nenv_allow: [NODE_OPTIONS]\n---\n## Rigs\n\n### other\n- **Engine:** claude\n",
                        encoding="utf-8")
        node = world.tmp / "chained"
        node.mkdir()
        (node / runbook.RUNBOOK_NAME).write_text(
            "---\nextends: \"../base.md\"\n---\n" + RUNBOOK_HEAD
            + "- **Engine:** claude\n- **MCP:** off\n- **Env:** NODE_OPTIONS=--require=/repo/preload.cjs\n",
            encoding="utf-8")
        chosen, error, _ = Machine(world.tmp).resolve(node)
        assert chosen is None and "NODE_OPTIONS" in error
        assert rows(world.record) == []

    def test_a_runbook_level_env_allow_key_gains_nothing(self, world):
        node = world.tmp / "framed"
        node.mkdir()
        (node / runbook.RUNBOOK_NAME).write_text(
            "---\nenv_allow: [NODE_OPTIONS]\n---\n" + RUNBOOK_HEAD
            + "- **Engine:** claude\n- **MCP:** off\n- **Env:** NODE_OPTIONS=--require=/repo/preload.cjs\n",
            encoding="utf-8")
        book = runbook.load_for_root(node, validate=False)
        assert book.frontmatter.get("env_allow") == ["NODE_OPTIONS"]   # carried as prose, read by nobody
        chosen, error, _ = Machine(world.tmp).resolve(node)
        assert chosen is None and "NODE_OPTIONS" in error
        assert rows(world.record) == []

    # -- the reviewer's runtime preload case, end to end --------------------------

    @pytest.mark.skipif(shutil_which("node") is None, reason="needs node")
    def test_the_node_preload_never_runs(self, world):
        import shutil

        nodebin = shutil.which("node")
        marker = world.tmp / "injected.txt"
        member = world.tmp / "member"
        member.mkdir()
        preload = member / "preload.cjs"
        preload.write_text(
            "require('node:fs').appendFileSync(" + json.dumps(str(marker)) + ", process.argv.join(' ')+'\\n');",
            encoding="utf-8")
        cli = world.bins / "copilot"
        cli.write_text("#!" + nodebin + "\nif(process.argv.includes('--version'))console.log('1.0.105');else console.log('ok');\n",
                       encoding="utf-8")
        cli.chmod(0o755)
        monkey_path = f"{world.bins}{os.pathsep}{os.path.dirname(nodebin)}{os.pathsep}{os.environ['PATH']}"
        machine = Machine(world.tmp, {"copilot": ["shell(touch)"]})
        node = world.tmp / "pre"
        node.mkdir()
        (node / runbook.RUNBOOK_NAME).write_text(
            RUNBOOK_HEAD + f"- **Engine:** copilot\n- **MCP:** off\n- **Env:** NODE_OPTIONS=--require={preload}\n",
            encoding="utf-8")
        chosen, error, _ = machine.resolve(node)
        assert chosen is None and "NODE_OPTIONS" in error
        if chosen is not None:  # pragma: no cover - the failure mode on the old head
            dispatch.run_harness(chosen, "t", member, env={**os.environ, "PATH": monkey_path})
        assert not marker.exists()

    def test_the_docs_example_no_longer_hands_a_token_to_the_repo(self):
        text = (R4T_DIR.parents[1] / "docs" / "r4t-runbook.md").read_text(encoding="utf-8")
        assert "**Env:** GH_TOKEN=" not in text and "TOKEN=${GH_TOKEN" not in text


class TestRelativePathAndHomeValuesAreRefusedAtLoad:
    def spec(self, **env):
        return {"preset": "claude", "invoke": rig_mod.build_preset_invoke("claude"), "env": env}

    @pytest.mark.parametrize("value", ["bin", "", f"{os.sep}usr{os.sep}bin{os.pathsep}bin",
                                       f"{os.pathsep}{os.sep}usr{os.sep}bin", "./bin"])
    def test_a_relative_or_empty_path_entry_is_refused(self, value):
        error = rig_mod.rig_from_spec("r", self.spec(PATH=value)).error
        assert error and "env.PATH" in error and "absolute" in error and "'r'" in error

    def test_an_absolute_path_is_accepted(self, tmp_path):
        value = os.pathsep.join([str(tmp_path), str(tmp_path / "b")])
        assert rig_mod.rig_from_spec("r", self.spec(PATH=value)).error is None

    @pytest.mark.parametrize("variable", ["CLAUDE_CONFIG_DIR", "CODEX_HOME", "COPILOT_HOME"])
    @pytest.mark.parametrize("value", ["rel/home", "~/home", "."])
    def test_a_relative_or_tilde_home_is_refused(self, variable, value):
        error = rig_mod.rig_from_spec("r", self.spec(**{variable: value})).error
        assert error and variable in error and "absolute" in error

    def test_engine_home_refuses_to_resolve_one_in_the_routers_view(self):
        with pytest.raises(RigError, match="not an absolute path"):
            rig_mod.engine_home("copilot", {"COPILOT_HOME": ".copilot-seat", "HOME": "/h"})
        with pytest.raises(RigError, match="not an absolute path"):
            rig_mod.engine_home("copilot", {"COPILOT_HOME": "~/.copilot", "HOME": "/h"})

    @posix_only
    def test_a_turn_whose_env_names_a_relative_home_is_refused_before_spawn(self, tmp_path):
        record = tmp_path / "r.jsonl"
        stub(tmp_path / "bin" / "copilot", "1.0.105", record)
        spec = {"preset": "copilot", "invoke": [str(tmp_path / "bin" / "copilot"), "-p", "{prompt}"],
                "mcp": False}
        chosen = rig_mod.rig_from_spec("c", spec)
        code, out, *_ = dispatch.run_harness(
            chosen, "t", tmp_path, env={**os.environ, "COPILOT_HOME": ".copilot-seat"}
        )
        assert code == 126 and "absolute" in out
        assert rows(record) == []


# --- B: the wrapper is the router's ----------------------------------------------


@posix_only
class TestTrustedWrapper:
    @pytest.fixture
    def world(self, tmp_path, monkeypatch):
        router_bin, rig_bin, member = tmp_path / "router-bin", tmp_path / "rig-bin", tmp_path / "member"
        member.mkdir()
        monkeypatch.delenv("TELL_OUTBOX_DIR", raising=False)
        log = tmp_path / "wrap.jsonl"
        for tool in ("sudo", "docker"):
            stub(router_bin / tool, "router", log)
            stub(rig_bin / tool, "decoy", tmp_path / "decoy.jsonl")
            stub(member / tool, "decoy", tmp_path / "decoy.jsonl")
        monkeypatch.setenv("PATH", f"{router_bin}{os.pathsep}{os.environ['PATH']}")
        rig = rig_mod.rig_from_spec("w", {
            "preset": "claude", "invoke": rig_mod.build_preset_invoke("claude"), "mcp": False,
            "env": {"PATH": f"{rig_bin}{os.pathsep}{os.environ['PATH']}"},
        })
        return SimpleNamespace(rig=rig, member=member, log=log, decoy=tmp_path / "decoy.jsonl",
                               router_bin=router_bin)

    def test_run_as_uses_the_routers_sudo_for_the_probe_and_the_spawn(self, world):
        env = {**os.environ, **isolate.Isolation(run_as="nobody").to_env()}
        code, out, *_ = dispatch.run_harness(world.rig, "t", world.member, env=env)
        assert code == 0, out
        assert rows(world.decoy) == []
        files = {r["file"] for r in rows(world.log)}
        assert files == {str(world.router_bin / "sudo")}
        assert any(r["argv"][:1] == ["-n"] for r in rows(world.log))   # the probe
        assert any(r["argv"][:1] == ["-u"] for r in rows(world.log))   # the spawn

    def test_the_container_uses_the_routers_docker(self, world):
        env = {**os.environ, **isolate.Isolation(container="img").to_env()}
        code, out, *_ = dispatch.run_harness(world.rig, "t", world.member, env=env)
        assert code == 0, out
        assert rows(world.decoy) == []
        assert {r["file"] for r in rows(world.log)} == {str(world.router_bin / "docker")}

    def test_a_router_with_no_sudo_refuses_and_never_asks_the_rig_path(self, world, monkeypatch):
        monkeypatch.setenv("PATH", "/nonexistent-r4t")
        env = {**os.environ, **isolate.Isolation(run_as="nobody").to_env()}
        code, out, *_ = dispatch.run_harness(world.rig, "t", world.member, env=env)
        assert code == 126 and "sudo" in out
        assert rows(world.decoy) == []

    def test_the_wrapper_argv_starts_with_the_trusted_absolute_path(self, world):
        argv = isolate.wrap_run_as(["x"], "bob", "/s", "/w")
        assert argv[0] == str(world.router_bin / "sudo")
        argv = isolate.build_container_argv(["x"], "img", name="n", staging_dir="/s",
                                            workplace="/w", tell_outbox="/s")
        assert argv[0] == str(world.router_bin / "docker")


# --- C: the Windows lookup ---------------------------------------------------------


class TestWindowsLookup:
    @pytest.fixture
    def windows(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        def no_which(*a, **k):
            raise AssertionError("shutil.which was called")
        monkeypatch.setattr("shutil.which", no_which)

    def test_the_routers_cwd_is_never_searched_first(self, tmp_path, windows):
        router, member = tmp_path / "router", tmp_path / "member"
        (member / "bin").mkdir(parents=True)
        router.mkdir()
        (router / "claude.exe").write_text("planted", encoding="utf-8")
        (member / "bin" / "claude.exe").write_text("real", encoding="utf-8")
        os.chdir(router)
        launch = rig_mod.resolve_launch(
            "claude", {"PATH": str(member / "bin")}, cwd=member
        )
        assert launch.binary == str(member / "bin" / "claude.exe")

    def test_the_childs_pathext_decides_the_order(self, tmp_path, windows):
        folder = tmp_path / "bin"
        folder.mkdir()
        (folder / "claude.exe").write_text("x", encoding="utf-8")
        (folder / "claude.cmd").write_text("x", encoding="utf-8")
        for pathext, want in ((".CMD;.EXE", "claude.cmd"), (".EXE;.CMD", "claude.exe")):
            launch = rig_mod.resolve_launch(
                "claude", {"PATH": str(folder), "PATHEXT": pathext}, cwd=tmp_path
            )
            assert Path(launch.binary).name == want

    def test_a_name_with_its_extension_is_tried_as_written(self, tmp_path, windows):
        (tmp_path / "claude.CMD").write_text("x", encoding="utf-8")
        launch = rig_mod.resolve_launch("claude.CMD", {"PATH": str(tmp_path)}, cwd=tmp_path)
        assert launch.binary == str(tmp_path / "claude.CMD")

    def test_a_bare_extensionless_file_is_not_a_windows_executable(self, tmp_path, windows):
        (tmp_path / "claude").write_text("x", encoding="utf-8")
        assert rig_mod.resolve_launch("claude", {"PATH": str(tmp_path)}, cwd=tmp_path).binary is None

    def test_a_relative_entry_and_an_empty_one_read_against_the_childs_cwd(self, tmp_path, windows):
        member = tmp_path / "member"
        (member / "bin").mkdir(parents=True)
        (member / "bin" / "claude.exe").write_text("x", encoding="utf-8")
        (member / "tool.exe").write_text("x", encoding="utf-8")
        os.chdir(tmp_path)
        assert rig_mod.resolve_launch("claude", {"PATH": "bin"}, cwd=member).binary == str(member / "bin" / "claude.exe")
        assert rig_mod.resolve_launch("tool", {"PATH": os.pathsep + "/nonexistent"}, cwd=member).binary == str(member / "tool.exe")


class TestEnvNamesOnWindows:
    """Windows environment names are case-insensitive, so there a grant or a
    safe-list entry matches in any case. Simulated by patching the platform."""

    def refused(self, monkeypatch, tmp_path, grants, name):
        machine = Machine(tmp_path, env_allow=grants)
        node = node_with(tmp_path, f"- **Engine:** claude\n- **MCP:** off\n- **Env:** {name}=x\n")
        book = runbook.load_for_root(node, validate=False)
        rig = book.rigs["guarded"]
        monkeypatch.setattr(sys, "platform", "win32")
        return machine.config.repo_env_problem(rig)

    def test_a_lowercase_grant_admits_the_uppercase_name(self, monkeypatch, tmp_path):
        assert self.refused(monkeypatch, tmp_path, ["node_options"], "NODE_OPTIONS") is None

    def test_an_uppercase_grant_admits_the_lowercase_name(self, monkeypatch, tmp_path):
        assert self.refused(monkeypatch, tmp_path, ["NODE_OPTIONS"], "node_options") is None

    def test_a_grant_still_admits_only_its_own_name(self, monkeypatch, tmp_path):
        assert self.refused(monkeypatch, tmp_path, ["node_options"], "HTTPS_PROXY")

    def test_the_safe_name_matches_in_any_case(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert rig_mod.in_repo_env_safe("enable_prompt_caching_1h")
        assert not rig_mod.in_repo_env_safe("NODE_OPTIONS")

    def test_grants_that_differ_only_by_case_collapse(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert rig_mod.parse_env_allow(["NODE_OPTIONS", "node_options"]) == ["NODE_OPTIONS"]

    def test_a_turn_owned_name_is_refused_in_any_case(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert rig_mod._env_name_problem("tell_outbox_dir")
        assert rig_mod._env_name_problem("r4t_node")


@posix_only
class TestEmptyAndRelativePathEntries:
    def test_an_empty_entry_is_the_childs_cwd_not_the_routers(self, tmp_path):
        router, member = tmp_path / "router", tmp_path / "member"
        router.mkdir()
        member.mkdir()
        record = tmp_path / "r.jsonl"
        stub(router / "claude", "9.9.9", record)
        stub(member / "claude", "2.1.316", record)
        os.chdir(router)
        for path in (os.pathsep + "/nonexistent", "/nonexistent" + os.pathsep, "/nonexistent" + os.pathsep + os.pathsep + "/x"):
            launch = rig_mod.resolve_launch("claude", {"PATH": path}, cwd=member)
            assert launch.binary == str(member / "claude")

    def test_find_executable_reads_nothing_from_this_process(self, tmp_path, monkeypatch):
        from ar3.proc import find_executable

        (tmp_path / "t").write_text("x", encoding="utf-8")
        (tmp_path / "t").chmod(0o755)
        monkeypatch.setenv("PATH", "/nonexistent")
        assert find_executable("t", str(tmp_path), "/") == str(tmp_path / "t")
        assert find_executable("t", "/nonexistent", str(tmp_path)) is None


# --- D: the floor is keyed by the binary ----------------------------------------------


class TestFloorKeyedByBinary:
    def config(self, tmp_path, **rigs):
        path = tmp_path / "rigs.json"
        path.write_text(json.dumps({"deny_floor": {"claude": FLOOR}, **rigs}), encoding="utf-8")
        return load_rig_config(path)

    def effective(self, config, name):
        rig = config.rigs[name]
        if rig.error:
            return None, rig.error
        return config.effective_rig(rig)

    def test_an_unknown_preset_refuses_at_load(self, tmp_path):
        config = self.config(tmp_path, a={"preset": "claude-code", "invoke": ["claude", "-p", "{prompt}"]})
        assert "not a known preset" in config.rigs["a"].error

    def test_a_preset_less_rig_refuses_when_a_floor_exists(self, tmp_path):
        config = self.config(tmp_path, a={"invoke": ["claude", "-p", "{prompt}"]},
                             b={"invoke": ["/x/claude", "-p", "{prompt}"]})
        for name in ("a", "b"):
            eff, why = self.effective(config, name)
            assert eff is None and "no preset" in why

    def test_a_preset_less_rig_runs_when_there_is_no_floor(self, tmp_path):
        path = tmp_path / "rigs.json"
        path.write_text(json.dumps({"a": {"invoke": ["tool", "{prompt}"]}}), encoding="utf-8")
        config = load_rig_config(path)
        assert config.effective_rig(config.rigs["a"])[0] is not None

    @pytest.mark.parametrize(
        "invoke",
        [["env", "claude", "-p", "{prompt}"], ["npx", "@anthropic-ai/claude-code", "-p", "{prompt}"],
         ["agy", "--print", "{prompt}"], ["claude-wrapper", "-p", "{prompt}"]],
        ids=["env", "npx", "another-engine", "wrapper-script"],
    )
    def test_a_launcher_or_other_engine_is_refused_when_a_floor_exists(self, tmp_path, invoke):
        config = self.config(tmp_path, a={"preset": "claude", "invoke": invoke})
        eff, why = self.effective(config, "a")
        assert eff is None and "starts" in why and "launcher" in why

    def test_a_preset_that_disagrees_with_its_binary_is_refused(self, tmp_path):
        config = self.config(tmp_path, a={"preset": "copilot", "invoke": ["claude", "-p", "{prompt}"]})
        eff, why = self.effective(config, "a")
        assert eff is None

    def test_a_mixed_pool_never_gets_flags_for_another_engine(self, tmp_path):
        config = self.config(tmp_path, a={"preset": "claude", "invoke": [
            ["claude", "-p", "{prompt}"], ["agy", "--print", "{prompt}"]]})
        eff, why = self.effective(config, "a")
        assert eff is None and "pool variant 1" in why

    @pytest.mark.parametrize("argv0", ["claude", "claude.cmd", "CLAUDE.EXE", "C:/Tools/claude.exe", "/opt/bin/claude"])
    def test_the_cli_by_its_own_name_passes_with_its_floor(self, tmp_path, argv0):
        config = self.config(tmp_path, a={"preset": "claude", "invoke": [argv0, "-p", "{prompt}"]})
        eff, why = self.effective(config, "a")
        assert why is None and FLOOR in eff.deny_tools

    def test_the_known_launchers_keep_their_anchor(self, tmp_path):
        config = self.config(tmp_path, a={"preset": "ollama-claude",
                                          "invoke": rig_mod.build_preset_invoke("ollama-claude", model="m")})
        eff, why = self.effective(config, "a")
        assert why is None and FLOOR in eff.deny_tools


# --- E: the shadow of an invalid machine rig ---------------------------------------------


class TestShadowOfAnInvalidMachineRig:
    @pytest.mark.parametrize(
        "machine",
        [{"preset": "claude", "invoke": ["claude", "-p"], "deny_tools": "Bash(rm:*)"},
         {"preset": "claude", "invoke": "claude -p {prompt}", "deny_tools": "Bash(rm:*)"},
         {"preset": "claude", "invoke": rig_mod.build_preset_invoke("claude"),
          "deny_tools": "Bash(rm:*)", "timeout_seconds": -1}],
        ids=["no-prompt", "not-a-list", "bad-timeout"],
    )
    def test_the_runbook_rig_does_not_run_without_the_machine_denies(self, tmp_path, machine):
        node = node_with(tmp_path, "- **Engine:** claude\n- **MCP:** off")
        cfg = node / "rigs.json"
        cfg.write_text(json.dumps({"guarded": machine}), encoding="utf-8")
        config = load_rig_config(cfg)
        book = runbook.load_for_root(node, validate=False)
        chosen, error, _ = config.rig_for(book.roster.find("Lead"))
        assert chosen is None and "shadows is invalid" in error

    def test_the_valid_control_still_carries_its_denies(self, tmp_path):
        node = node_with(tmp_path, "- **Engine:** claude\n- **MCP:** off")
        cfg = node / "rigs.json"
        cfg.write_text(json.dumps({"guarded": {"preset": "claude",
            "invoke": rig_mod.build_preset_invoke("claude"), "deny_tools": "Bash(rm:*)"}}), encoding="utf-8")
        chosen, error, _ = load_rig_config(cfg).rig_for(
            runbook.load_for_root(node, validate=False).roster.find("Lead"))
        assert error is None and chosen.deny_tools == ["Bash(rm:*)"]


# --- F: dangling symlinks ------------------------------------------------------------------


@posix_only
class TestDanglingLinks:
    def test_a_dangling_config_file_link_refuses(self, tmp_path):
        cfg = tmp_path / "p" / "rigs.json"
        cfg.parent.mkdir()
        cfg.symlink_to(tmp_path / "unmounted" / "rigs.json")
        for loader in (load_deny_floor, load_rig_config):
            with pytest.raises(RigError, match="leads nowhere"):
                loader(cfg)
        assert rig_mod.rig_config_present(cfg) is True

    def test_a_dangling_parent_link_refuses(self, tmp_path):
        (tmp_path / "parent").symlink_to(tmp_path / "offline" / "r4t")
        for loader in (load_deny_floor, load_rig_config):
            with pytest.raises(RigError, match="leads nowhere"):
                loader(tmp_path / "parent" / "rigs.json")

    def test_a_live_link_is_followed(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        (real / "rigs.json").write_text(json.dumps({"deny_floor": {"claude": FLOOR}}), encoding="utf-8")
        (tmp_path / "link").symlink_to(real)
        assert load_deny_floor(tmp_path / "link" / "rigs.json") == {"claude": [FLOOR]}

    def test_a_missing_file_behind_live_folders_is_still_absent(self, tmp_path):
        assert load_deny_floor(tmp_path / "nope" / "rigs.json") == {}
        assert load_rig_config(tmp_path / "nope" / "rigs.json").missing is True

    def test_the_other_policy_files_refuse_too(self, tmp_path):
        from ar3.fsio import DanglingLink, read_text_if_present

        (tmp_path / "r4t.md").symlink_to(tmp_path / "gone.md")
        with pytest.raises(DanglingLink):
            read_text_if_present(tmp_path / "r4t.md")


# --- G: the version gate by name -----------------------------------------------------------


@posix_only
class TestVersionGateByName:
    def rig(self, binary, **extra):
        invoke = rig_mod.build_preset_invoke("claude")
        invoke[0] = str(binary)
        return rig_mod.rig_from_spec("old", {"preset": "claude", "invoke": invoke, "mcp": False, **extra})

    def test_a_renamed_old_build_is_refused_when_knobs_are_set(self, tmp_path):
        record = tmp_path / "r.jsonl"
        renamed = stub(tmp_path / "p" / "claude-1.0", "1.0.0", record)
        rig = self.rig(renamed, deny_tools="Bash(rm:*)")
        code, out, *_ = dispatch.run_harness(rig, "t", tmp_path, env=dict(os.environ))
        assert code == 126 and "not named like the claude CLI" in out
        assert [r for r in rows(record) if "-p" in r["argv"]] == []

    def test_an_alias_symlink_with_another_name_is_refused_too(self, tmp_path):
        record = tmp_path / "r.jsonl"
        real = stub(tmp_path / "real" / "claude", "1.0.0", record)
        alias = tmp_path / "bin" / "agent-helper"
        alias.parent.mkdir()
        alias.symlink_to(real)
        code, out, *_ = dispatch.run_harness(self.rig(alias, deny_tools="Bash(rm:*)"), "t", tmp_path, env=dict(os.environ))
        assert code == 126 and "not named like" in out

    def test_a_symlink_named_like_the_cli_is_probed_as_the_cli(self, tmp_path):
        record = tmp_path / "r.jsonl"
        real = stub(tmp_path / "real" / "claude-1.0", "1.0.0", record)
        alias = tmp_path / "bin" / "claude"
        alias.parent.mkdir()
        alias.symlink_to(real)
        code, out, *_ = dispatch.run_harness(self.rig(alias, deny_tools="Bash(rm:*)"), "t", tmp_path, env=dict(os.environ))
        assert code == 126 and "older than" in out

    def test_with_no_knobs_a_renamed_binary_runs_as_before(self, tmp_path):
        record = tmp_path / "r.jsonl"
        renamed = stub(tmp_path / "p" / "claude-1.0", "1.0.0", record)
        code, out, *_ = dispatch.run_harness(self.rig(renamed), "t", tmp_path, env=dict(os.environ))
        assert code == 0, out

    def test_the_preset_launcher_is_still_recorded_as_a_launcher(self, tmp_path):
        launch = launch_for("ollama", {"PATH": os.environ["PATH"]}, tmp_path)
        launch = rig_mod.Launch("ollama", "/x/ollama", {}, str(tmp_path))
        assert rig_mod.access_binary_state("ollama-claude", launch)["status"] == "unverified launcher ollama"


# --- I: engine check goes through the launch ------------------------------------------------


@posix_only
class TestEngineCheckUsesTheRunsFile:
    HELP = ("--add-dir --allowedTools --disallowedTools --permission-mode "
            "--exclude-dynamic-system-prompt-sections --print -p --append-system-prompt")

    def layout(self, tmp_path, member_version):
        router, member = tmp_path / "router", tmp_path / "member"
        record = tmp_path / "r.jsonl"
        stub(router / "bin" / "claude", "2.1.316", record, help_text=self.HELP)
        stub(member / "bin" / "claude", member_version, record, help_text=self.HELP)
        os.chdir(router)
        return router, member, record

    def test_check_probes_the_file_a_run_in_the_member_dir_starts(self, tmp_path, monkeypatch):
        router, member, record = self.layout(tmp_path, "1.0.0")
        monkeypatch.setenv("PATH", "/nonexistent")
        monkeypatch.setattr(engine_check, "resolve_launch",
                            lambda argv0, env, cwd, **k: rig_mod.resolve_launch(argv0, {**env, "PATH": "bin"}, cwd=cwd, **k))
        report = engine_check.check_engine("claude", workdir=member, deny_floor={"claude": [FLOOR]})
        files = {r["file"] for r in rows(record)}
        assert files <= {str(member / "bin" / "claude")}, files
        assert report.verdict == engine_check.REJECTED and "older than" in report.detail

    def test_the_control_with_a_current_build_is_accepted_and_still_the_members(self, tmp_path, monkeypatch):
        router, member, record = self.layout(tmp_path, "2.1.316")
        monkeypatch.setattr(engine_check, "resolve_launch",
                            lambda argv0, env, cwd, **k: rig_mod.resolve_launch(argv0, {**env, "PATH": "bin"}, cwd=cwd, **k))
        report = engine_check.check_engine("claude", workdir=member, deny_floor={"claude": [FLOOR]})
        assert {r["file"] for r in rows(record)} == {str(member / "bin" / "claude")}
        assert report.verdict == engine_check.ACCEPTED, report.detail

    def test_a_renamed_binary_is_rejected_by_the_gate(self, tmp_path, monkeypatch):
        record = tmp_path / "r.jsonl"
        stub(tmp_path / "p" / "claude-1.0", "1.0.0", record, help_text=self.HELP)
        launch = rig_mod.resolve_launch(str(tmp_path / "p" / "claude-1.0"), {}, cwd=tmp_path)
        with pytest.raises(RigError, match="not named like"):
            rig_mod.access_binary_state("claude", launch)


# --- J: P3s ---------------------------------------------------------------------------------


@posix_only
class TestP3s:
    def test_the_usage_flag_cache_notices_a_replaced_binary(self, tmp_path, monkeypatch):
        record = tmp_path / "u.jsonl"
        binary = stub(tmp_path / "copilot", "1.0.0", record)
        monkeypatch.setattr(copilot, "_usage_support", {})
        launch = rig_mod.resolve_launch(str(binary), {"HELP_TEXT": "nothing"}, cwd=tmp_path)
        assert copilot.supports_usage_file(launch) is False
        # The same path now holds a build whose help lists the flag.
        stub(tmp_path / "copilot", "1.0.105 padding to change size", record, help_text="--usage-output-file")
        launch = rig_mod.resolve_launch(str(binary), {"HELP_TEXT": "--usage-output-file"}, cwd=tmp_path)
        os.utime(binary, (1, 1))
        assert copilot.supports_usage_file(launch) is True

    def test_distill_refuses_a_launch_with_no_file(self, tmp_path):
        r = rig_mod.rig_from_spec("d", {"preset": "claude", "deny_tools": "Bash(rm:*)",
                                        "invoke": rig_mod.build_preset_invoke("claude"), "mcp": False})
        launch = rig_mod.resolve_launch("claude", {"PATH": str(tmp_path / "nowhere")}, cwd=tmp_path)
        with pytest.raises(RigError, match="no file to start"):
            r.distill_command(tmp_path, launch=launch)

    def test_the_park_probe_reads_the_members_cwd(self, tmp_path):
        router, member = tmp_path / "router", tmp_path / "member"
        record = tmp_path / "p.jsonl"
        stub(router / "bin" / "claude", "2.1.316", record)
        stub(member / "bin" / "claude", "2.1.316", record)
        os.chdir(router)
        assert dispatch._probe_resolves("claude", "bin") is None
        assert dispatch._probe_resolves("claude", "bin", member) == str(member / "bin" / "claude")

    def test_a_park_remembers_the_members_cwd(self, tmp_path, r4t_home):
        record = state.park_member("n", "a", reason="r", rig="x", probe="t",
                                   probe_path="/p", probe_cwd=str(tmp_path))
        assert record["probe_cwd"] == str(tmp_path)


# --- K: the home guard ----------------------------------------------------------------------


class TestHomeGuardModes:
    def moved(self, tmp_path, monkeypatch):
        from ar3 import homeguard

        root = tmp_path / "real"
        root.mkdir()
        monkeypatch.setattr(homeguard, "real_home", lambda app: root)
        guard = homeguard.HomeGuard(("r4t",))
        (root / "quota.json").write_text("{}", encoding="utf-8")
        return guard

    @staticmethod
    def session():
        return SimpleNamespace(exitstatus=0)

    @pytest.mark.parametrize("env", [{"CI": "1"}, {"AR3_TESTS_STRICT_HOME": "1"}])
    def test_it_fails_the_run_in_strict_modes(self, tmp_path, monkeypatch, capsys, env):
        for name in ("CI", "AR3_TESTS_STRICT_HOME", "AR3_TESTS_ALLOW_REAL_HOME"):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        guard, session = self.moved(tmp_path, monkeypatch), self.session()
        guard.finish(session)
        assert session.exitstatus == 1 and "REAL STATE TOUCHED" in capsys.readouterr().err

    def test_it_only_warns_otherwise(self, tmp_path, monkeypatch, capsys):
        for name in ("CI", "AR3_TESTS_STRICT_HOME", "AR3_TESTS_ALLOW_REAL_HOME"):
            monkeypatch.delenv(name, raising=False)
        guard, session = self.moved(tmp_path, monkeypatch), self.session()
        guard.finish(session)
        err = capsys.readouterr().err
        assert session.exitstatus == 0 and "warning" in err and "quota.json" in err

    def test_the_waiver_silences_it(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("AR3_TESTS_ALLOW_REAL_HOME", "1")
        monkeypatch.setenv("CI", "1")
        guard, session = self.moved(tmp_path, monkeypatch), self.session()
        guard.finish(session)
        assert session.exitstatus == 0 and capsys.readouterr().err == ""


# --- source rules ------------------------------------------------------------------------------


def test_only_the_resolvers_look_for_a_file_on_a_path():
    """`find_executable` is called by the launch resolver and the trusted
    wrapper lookup, and nowhere else in the product; `shutil.which` is not in
    the launch resolver at all."""
    callers = set()
    for path in sorted(list(R4T_DIR.glob("*.py")) + list((R4T_DIR / "engines").glob("*.py"))):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            for call in ast.walk(fn):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) \
                        and call.func.id == "find_executable":
                    callers.add((path.name, fn.name))
                if path.name == "rig.py" and fn.name == "resolve_launch":
                    assert not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                                and call.func.attr == "which")
    assert callers == {("rig.py", "resolve_launch"), ("isolate.py", "trusted_tool")}, callers


def test_the_wrapper_tools_are_never_spelled_as_a_bare_name_in_a_spawn_argv():
    tree = ast.parse((R4T_DIR / "isolate.py").read_text(encoding="utf-8"))
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        if fn.name in ("trusted_tool", "_tool_or_name"):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.List) and node.elts and isinstance(node.elts[0], ast.Constant) \
                    and node.elts[0].value in ("sudo", "docker"):
                raise AssertionError(f"isolate.py:{node.lineno} {fn.name} spells {node.elts[0].value!r} bare")
