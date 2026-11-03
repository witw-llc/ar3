"""Issue 382 Part A, round 1: every path that reaches an engine carries the
node's deny floor, and unreadable policy is never absent policy.

Stub binaries only. A stub `claude` prints its argv as JSON, so what is asserted
is the argv a child process would really have received, not a unit's return
value.
"""
from __future__ import annotations

import ast
import json
import os
import shlex
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

import dispatch
import engine_memory
import engines
import isolate
import knowledge
import org
import r4t
import rig as rig_mod
import runbook
import state
from conftest import write_path_executable
from engines import copilot
from rig import RigError, access_binary_state, load_deny_floor, load_rig_config
from roster import Member, RosterError, load_roster

R4T_DIR = Path(__file__).resolve().parents[1]
REPO = R4T_DIR.parents[1]
NODE = "acme"
FLOOR = "Bash(touch:*)"

posix_only = pytest.mark.skipif(os.name == "nt", reason="chmod and sh -c are POSIX")
not_root = pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0, reason="root reads everything"
)


def stub_claude(directory: Path, version: str, name: str = "claude") -> Path:
    """A `claude` that answers --version and otherwise prints its argv."""
    return write_path_executable(
        directory, name,
        "import json, sys\n"
        "if '--version' in sys.argv:\n"
        f"    print({version!r})\n"
        "    sys.exit(0)\n"
        "print(json.dumps(sys.argv[1:]))\n",
    )


@pytest.fixture(autouse=True)
def _path_claude(tmp_path_factory, monkeypatch):
    """The verified build on PATH. No test here reaches a real CLI."""
    bin_dir = tmp_path_factory.mktemp("path-bin")
    stub_claude(bin_dir, "2.1.316 (Claude Code)")
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    return bin_dir


def write_config(path: Path, **rigs) -> Path:
    data = {"deny_floor": {"claude": FLOOR}, "throttle": {"min_seconds_between_turn_starts": 0}}
    for name, spec in rigs.items():
        spec = dict(spec)
        spec.setdefault("preset", "claude")
        spec.setdefault("invoke", rig_mod.build_preset_invoke("claude"))
        spec.setdefault("mcp", False)
        data[name] = spec
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def child_argv(output: str) -> list[str]:
    """The stub's JSON line out of whatever a command printed."""
    for line in output.splitlines():
        if line.startswith("["):
            return json.loads(line)
    raise AssertionError(f"no child argv in {output!r}")


def denies_of(argv: list[str]) -> str:
    assert "--disallowedTools" in argv, argv
    return argv[argv.index("--disallowedTools") + 1]


# --- finding 1: unreadable policy is not absent policy ---------------------------


@not_root
class TestUnreadableConfig:
    @pytest.fixture
    def closed(self, tmp_path):
        folder = tmp_path / "private"
        folder.mkdir()
        path = folder / "rigs.json"
        write_config(path, worker={})
        folder.chmod(0)
        yield path
        folder.chmod(0o700)

    def test_a_closed_folder_refuses_both_loaders(self, closed):
        for loader in (load_deny_floor, load_rig_config):
            with pytest.raises(RigError, match="cannot read rig config") as raised:
                loader(closed)
            assert str(closed) in str(raised.value)
            assert "permissions" in str(raised.value)

    def test_a_file_without_read_bits_refuses_both_loaders(self, tmp_path):
        path = write_config(tmp_path / "rigs.json", worker={})
        path.chmod(0)
        try:
            for loader in (load_deny_floor, load_rig_config):
                with pytest.raises(RigError, match="cannot read rig config"):
                    loader(path)
        finally:
            path.chmod(0o600)

    def test_a_directory_in_place_of_the_file_refuses(self, tmp_path):
        path = tmp_path / "rigs.json"
        path.mkdir()
        for loader in (load_deny_floor, load_rig_config):
            with pytest.raises(RigError, match="cannot read rig config"):
                loader(path)

    def test_malformed_json_still_refuses(self, tmp_path):
        path = tmp_path / "rigs.json"
        path.write_text("{nope", encoding="utf-8")
        for loader in (load_deny_floor, load_rig_config):
            with pytest.raises(RigError, match="cannot load rig config"):
                loader(path)

    def test_a_genuinely_absent_file_is_still_no_config(self, tmp_path):
        path = tmp_path / "none" / "rigs.json"
        assert load_deny_floor(path) == {}
        assert load_rig_config(path).missing is True

    def test_a_file_in_place_of_the_folder_is_absent_not_unreadable(self, tmp_path):
        (tmp_path / "blocker").write_text("x", encoding="utf-8")
        assert load_rig_config(tmp_path / "blocker" / "rigs.json").missing is True

    def test_a_write_never_lays_a_fresh_config_over_an_unreadable_one(self, closed):
        with pytest.raises(RigError, match="cannot read rig config"):
            rig_mod.add_preset_rig(closed, "extra", "claude")

    def test_engine_run_refuses_before_any_spawn(self, closed, tmp_path, capsys):
        marker = tmp_path / "spawned"
        write_path_executable(
            tmp_path / "bin", "claude", f"import pathlib\npathlib.Path({str(marker)!r}).touch()\n"
        )
        code = r4t.main([
            "engine", "claude", "run", "--rig-config", str(closed),
            "--dir", str(tmp_path), "--no-scaffold", "hello",
        ])
        assert code == 1
        assert str(closed) in capsys.readouterr().err
        assert not marker.exists()

    def test_the_memory_writer_refuses_an_unreadable_config(self, closed, tmp_path):
        result = run_writer(tmp_path, {"engine": "claude", "model": None,
                                       "context": {"config": str(closed)}})
        assert result.returncode == 1 and "cannot read rig config" in result.stderr
        assert result.stdout == ""

    def test_inline_resolution_cannot_drop_the_floor(self, closed):
        """The reviewer's probe: the loader used to answer `missing`, and an
        inline rig then resolved with no denies."""
        with pytest.raises(RigError):
            load_rig_config(closed)


def python_314() -> str | None:
    for candidate in ("python3.14", "python3.14t"):
        found = shutil.which(candidate)
        if found:
            return found
    uv = shutil.which("uv")
    if uv:
        done = subprocess.run([uv, "python", "find", "3.14"], capture_output=True, text=True)
        if done.returncode == 0 and done.stdout.strip():
            return done.stdout.strip()
    return None


PROBE = textwrap.dedent(
    """\
    import json, pathlib, sys, tempfile
    sys.path[:0] = [{lib!r}, {r4t!r}]
    import rig
    with tempfile.TemporaryDirectory() as td:
        folder = pathlib.Path(td) / "config"
        folder.mkdir()
        path = folder / "rigs.json"
        path.write_text(json.dumps({{"deny_floor": {{"claude": "Bash(touch:*)"}}}}))
        folder.chmod(0)
        try:
            try:
                rig.load_rig_config(path)
            except rig.RigError as exc:
                print("REFUSED", sys.version_info[:2], str(path) in str(exc))
            else:
                print("LOADED")
        finally:
            folder.chmod(0o700)
    """
)


@not_root
@pytest.mark.parametrize("which", ["this", "3.14"])
def test_a_closed_folder_refuses_on_python_314_as_well(which, tmp_path):
    """3.14's `Path.is_file()` answers False for an unreadable parent; 3.12
    raises. The loader may do neither."""
    interpreter = sys.executable if which == "this" else python_314()
    if interpreter is None:
        pytest.skip("no python 3.14 installed")
    script = tmp_path / "probe.py"
    script.write_text(PROBE.format(lib=str(REPO / "lib"), r4t=str(R4T_DIR)), encoding="utf-8")
    done = subprocess.run([interpreter, "-I", str(script)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("REFUSED") and done.stdout.strip().endswith("True"), done.stdout


@not_root
class TestOtherPolicyReads:
    def test_an_unreadable_org_config_is_an_error_not_no_isolation(self, tmp_path):
        (tmp_path / "r4t-org.json").write_text('{"isolation": {"run_as": "x"}}', encoding="utf-8")
        (tmp_path / "r4t-org.json").chmod(0)
        try:
            with pytest.raises(org.OrgError, match="cannot read org config"):
                org._read_config(tmp_path)
            loaded = org.load_org(tmp_path)
            assert any("cannot read org config" in e for e in loaded.errors)
            assert any("cannot read org config" in e for e in org.check_org(tmp_path))
        finally:
            (tmp_path / "r4t-org.json").chmod(0o600)

    def test_an_unreadable_runbook_is_not_a_node_without_one(self, tmp_path):
        book = tmp_path / runbook.RUNBOOK_NAME
        book.write_text("---\nisolation: {}\n---\n## Roster\n", encoding="utf-8")
        book.chmod(0)
        try:
            assert runbook.has_runbook(tmp_path) is True
            with pytest.raises(runbook.RunbookError, match="cannot read runbook"):
                runbook.org_settings(tmp_path)
            with pytest.raises(org.OrgError, match="cannot read runbook"):
                org._read_config(tmp_path)
            with pytest.raises(RosterError, match="cannot read"):
                load_roster(book, node=NODE)
        finally:
            book.chmod(0o600)

    def test_an_unreadable_legacy_roster_says_so(self, tmp_path):
        path = tmp_path / "ROSTER.md"
        path.write_text("### Gerry\n- **Rig:** x\n", encoding="utf-8")
        path.chmod(0)
        try:
            with pytest.raises(RosterError, match="cannot read roster"):
                load_roster(path)
        finally:
            path.chmod(0o600)


def test_the_helpers_tell_absent_from_unreadable(tmp_path):
    from ar3.fsio import is_present, read_text_if_present

    assert read_text_if_present(tmp_path / "nope") is None
    assert is_present(tmp_path / "nope") is False
    (tmp_path / "f").write_text("hi", encoding="utf-8")
    assert read_text_if_present(tmp_path / "f") == "hi"
    (tmp_path / "d").mkdir()
    assert is_present(tmp_path / "d") is True
    with pytest.raises(OSError):
        read_text_if_present(tmp_path / "d")


# --- finding 2: background launches carry the floor -----------------------------


def sweep_env(ctx, monkeypatch, config_path, *, named: bool):
    """Run a dream sweep against a stub store and return what the k7e commands
    were handed: {'distill': env, 'compile': env}."""
    ctx.config_path = config_path
    seen: dict[str, dict] = {}

    def fake_k7e(home, *args, **kwargs):
        seen[args[0]] = dict(kwargs.get("extra_env") or {})
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(knowledge, "_run_k7e", fake_k7e)
    state.write_turn_capture(NODE, "phil", "20260730T000000000001Z", "t", "x")
    roster = load_roster(ctx.roster_path)
    phil = roster.find("phil")
    phil.knowledge_on = True
    if named:
        phil.knowledge_distill_rig = "worker"
    else:
        phil.rig = "worker"
    knowledge.dream_sweep(ctx, roster, load_rig_config(config_path))
    return seen


def run_command(command: str) -> list[str]:
    done = subprocess.run(
        command, shell=True, input="summarize", capture_output=True, text=True, timeout=20
    )
    assert done.returncode == 0, done.stderr
    return child_argv(done.stdout)


@posix_only
class TestBackgroundLaunches:
    @pytest.mark.parametrize("named", [False, True], ids=["default", "named"])
    def test_distill_and_compile_children_receive_the_denies(
        self, ctx, monkeypatch, tmp_path, named
    ):
        config = write_config(tmp_path / "rigs2.json", worker={"deny_tools": "Bash(rm:*)"})
        seen = sweep_env(ctx, monkeypatch, config, named=named)
        for verb, variable in (("distill", "K7E_DISTILL_COMMAND"), ("compile", "K7E_COMPILE_COMMAND")):
            argv = run_command(seen[verb][variable])
            assert denies_of(argv) == f"{FLOOR},Bash(rm:*)", (verb, argv)

    @pytest.mark.parametrize("named", [False, True], ids=["default", "named"])
    def test_the_rigs_other_access_lists_ride_the_command(
        self, ctx, monkeypatch, tmp_path, named
    ):
        shared = tmp_path / "shared"
        shared.mkdir()
        config = write_config(
            tmp_path / "rigs2.json",
            worker={"add_dirs": str(shared), "allow_tools": "Bash(ls:*)"},
        )
        seen = sweep_env(ctx, monkeypatch, config, named=named)
        argv = run_command(seen["distill"]["K7E_DISTILL_COMMAND"])
        assert argv[argv.index("--add-dir") + 1] == str(shared)
        assert "Bash(ls:*)" in argv[argv.index("--allowedTools") + 1]
        assert denies_of(argv) == FLOOR

    @pytest.mark.parametrize("named", [False, True], ids=["default", "named"])
    def test_an_older_build_skips_the_pass_instead_of_distilling_bare(
        self, ctx, monkeypatch, tmp_path, named, _path_claude
    ):
        stub_claude(_path_claude, "1.0.0")
        config = write_config(tmp_path / "rigs2.json", worker={})
        seen = sweep_env(ctx, monkeypatch, config, named=named)
        assert "distill" not in seen
        files = (state.roster_dir(NODE) / "log").glob("*.md")
        text = "".join(f.read_text(encoding="utf-8") for f in files)
        assert "DREAM-SKIP phil" in text and "older than" in text

    def test_a_named_rig_the_floor_cannot_reach_is_refused(self, ctx, monkeypatch, tmp_path):
        """A named override with no recorded preset: the floor cannot be
        spelled into its argv, so it never runs."""
        config = tmp_path / "rigs2.json"
        config.write_text(json.dumps({
            "deny_floor": {"claude": FLOOR},
            "worker": {"invoke": ["claude", "-p", "{prompt}"]},
        }), encoding="utf-8")
        seen = sweep_env(ctx, monkeypatch, config, named=True)
        assert seen == {}
        member = Member(name="Phil")
        member.knowledge_distill_rig = "worker"
        rig, problem = knowledge.resolve_distill_rig(member, load_rig_config(config))
        assert rig is None and "no preset" in problem


# --- finding 2: the engine-memory writer ----------------------------------------


def run_writer(tmp_path: Path, spec: dict, extra_env: dict | None = None):
    capture = tmp_path / "memory" / "turns" / "capture.json"
    capture.parent.mkdir(parents=True, exist_ok=True)
    capture.write_text(json.dumps({"writer": spec}), encoding="utf-8")
    env = dict(os.environ, R4T_HOME=str(tmp_path / "state"), HOME=str(tmp_path))
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(R4T_DIR / "engine_memory.py"), "writer", str(capture)],
        input="summarize", capture_output=True, text=True, env=env, timeout=30,
    )


@posix_only
class TestMemoryWriter:
    def writer_spec(self, config: Path, **spec):
        return {"context": {"config": str(config)}, **spec}

    def test_a_writer_rig_with_a_preset_gets_floor_and_lists(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        config = write_config(
            tmp_path / "rigs.json",
            worker={"deny_tools": "Bash(rm:*)", "add_dirs": str(shared),
                    "allow_tools": "Bash(ls:*)"},
        )
        result = run_writer(tmp_path, self.writer_spec(config, rig="worker"))
        assert result.returncode == 0, result.stderr
        argv = child_argv(result.stdout)
        assert denies_of(argv) == f"{FLOOR},Bash(rm:*)"
        assert argv[argv.index("--add-dir") + 1] == str(shared)
        assert "Bash(ls:*)" in argv[argv.index("--allowedTools") + 1]

    def test_an_engine_only_writer_gets_the_floor(self, tmp_path):
        config = write_config(tmp_path / "rigs.json")
        result = run_writer(
            tmp_path, self.writer_spec(config, engine="claude", model=None, effort=None)
        )
        assert result.returncode == 0, result.stderr
        assert denies_of(child_argv(result.stdout)) == FLOOR

    def test_an_engine_only_writer_with_no_config_at_all_runs_unfloored(self, tmp_path):
        """No config file is no floor, the one case where absent means none."""
        result = run_writer(
            tmp_path, {"engine": "claude", "model": None, "effort": None},
        )
        assert result.returncode == 0, result.stderr
        assert "--disallowedTools" not in child_argv(result.stdout)

    def test_a_writer_rig_without_a_preset_that_the_floor_cannot_reach_is_refused(self, tmp_path):
        config = write_config(tmp_path / "rigs.json")
        data = json.loads(config.read_text())
        data["bare"] = {"invoke": ["claude", "-p", "{prompt}"]}
        config.write_text(json.dumps(data), encoding="utf-8")
        result = run_writer(tmp_path, self.writer_spec(config, rig="bare"))
        assert result.returncode == 1 and "no preset" in result.stderr
        assert result.stdout == ""

    def test_a_writer_on_an_older_build_is_refused(self, tmp_path):
        old = tmp_path / "old-bin"
        stub_claude(old, "1.0.0")
        config = write_config(tmp_path / "rigs.json", worker={})
        result = run_writer(
            tmp_path, self.writer_spec(config, rig="worker"),
            {"PATH": str(old) + os.pathsep + os.environ["PATH"]},
        )
        assert result.returncode == 1 and "older than" in result.stderr
        assert result.stdout == ""

    def test_engine_run_hands_the_memory_writer_the_config_it_was_floored_by(
        self, tmp_path, monkeypatch
    ):
        import engines.run as engine_run

        config = write_config(tmp_path / "custom-rigs.json")
        seen = {}
        monkeypatch.setattr(engine_run, "execute", lambda *a, **k: seen.update(k) or 0)
        code = r4t.main([
            "engine", "claude", "run", "--rig-config", str(config), "--dir", str(tmp_path),
            "--no-scaffold", "--memory", "on", "hi",
        ])
        assert code == 0
        assert seen["memory_rig_context"] == {"config": str(config)}
        assert seen["deny_floor"] == [FLOOR]


# --- finding 3: the probe runs the binary that will be spawned ------------------


def launch_of(rig, variant=0):
    pool = rig.pool()
    return rig_mod.resolve_launch(
        pool[variant % len(pool)][0], dict(os.environ), cwd=os.getcwd()
    )


@posix_only
class TestExactBinary:
    def custom(self, tmp_path, version, **extra):
        old = tmp_path / "old" / "claude"
        stub_claude(old.parent, version)
        invoke = rig_mod.build_preset_invoke("claude")
        invoke[0] = str(old)
        spec = {"preset": "claude", "invoke": invoke, "deny_tools": "Bash(rm:*)", "mcp": False}
        spec.update(extra)
        return rig_mod.rig_from_spec("old", spec), old

    def test_the_selected_absolute_binary_is_the_one_probed(self, tmp_path):
        custom, old = self.custom(tmp_path, "1.0.0")
        assert not custom.error
        _dirs, _state, error = dispatch._access_for_turn(
            custom, launch_of(custom), isolate.Isolation(), None
        )
        assert error and "older than" in error and str(old) in error
        code, out, _, _ = dispatch.run_harness(custom, "x", tmp_path, env=dict(os.environ))
        assert code == 126 and "older than" in out

    def test_an_absolute_newer_binary_is_recorded_with_its_own_path(self, tmp_path):
        custom, old = self.custom(tmp_path, "2.1.316 (Claude Code)")
        _dirs, record, error = dispatch._access_for_turn(
            custom, launch_of(custom), isolate.Isolation(), None
        )
        assert error is None
        assert record["binary"] == str(old) and record["status"] == "verified"

    def test_each_pool_variant_is_probed_by_its_own_binary(self, tmp_path):
        old = tmp_path / "old"
        stub_claude(old, "1.0.0")
        good = rig_mod.build_preset_invoke("claude")
        bad = rig_mod.build_preset_invoke("claude")
        bad[0] = str(old / "claude")
        pooled = rig_mod.rig_from_spec(
            "pool", {"preset": "claude", "invoke": [good, bad],
                     "deny_tools": "Bash(rm:*)", "mcp": False},
        )
        assert not pooled.error, pooled.error
        iso = isolate.Isolation()
        assert dispatch._access_for_turn(pooled, launch_of(pooled, 0), iso, None)[2] is None
        assert "older than" in dispatch._access_for_turn(pooled, launch_of(pooled, 1), iso, None)[2]

    def test_a_launcher_is_never_labelled_verified(self, tmp_path):
        ollama = tmp_path / "launch"
        write_path_executable(ollama, "ollama", "print('ollama 0.1')\n")
        env = dict(os.environ, PATH=str(ollama) + os.pathsep + os.environ["PATH"])
        record = access_binary_state("ollama-claude", rig_mod.resolve_launch("ollama", env, cwd=os.getcwd()))
        assert record["status"].startswith("unverified launcher")
        assert record["version"] is None

    def test_engine_run_probes_the_composed_argv0(self, tmp_path, monkeypatch, capsys):
        import engines.run as engine_run

        calls = []
        real = engine_run.access_binary_state

        def spy(preset, launch, **kwargs):
            calls.append(launch.argv0)
            return real(preset, launch, **kwargs)

        monkeypatch.setattr(engine_run, "access_binary_state", spy)
        code = r4t.main([
            "engine", "claude", "run", "--rig-config", str(write_config(tmp_path / "r.json")),
            "--dir", str(tmp_path), "--no-scaffold", "hi",
        ])
        assert code == 0 and calls == ["claude"]


# --- finding 4: fuel reads the rig's own account --------------------------------


class TestFuelRigEnv:
    @pytest.fixture
    def homes(self, tmp_path, monkeypatch):
        out = {}
        for label in ("router", "child"):
            home = tmp_path / label
            home.mkdir()
            (home / "config.json").write_text(
                json.dumps({"copilotTokens": f"synthetic-{label}"}), encoding="utf-8"
            )
            out[label] = home
        monkeypatch.setenv("COPILOT_HOME", str(out["router"]))
        for name in copilot.TOKEN_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("R4T_HOME", str(tmp_path / "state"))
        used = []
        monkeypatch.setattr(
            copilot, "_direct_user",
            lambda auth: used.append(auth)
            or {"quota_snapshots": {"premium_interactions": {"percent_remaining": 75}}},
        )
        return out, used

    def rig_config(self, tmp_path, env):
        path = tmp_path / "rigs.json"
        rig = {"preset": "copilot", "invoke": rig_mod.build_preset_invoke("copilot")}
        if env:
            rig["env"] = env
        path.write_text(json.dumps({"worker": rig}), encoding="utf-8")
        return path

    def fuel(self, config):
        return r4t.main(["rig", "fuel", "worker", "--rig-config", str(config)])

    def test_the_rig_account_answers_not_the_routers_gh(self, tmp_path, homes, monkeypatch):
        paths, used = homes
        real_which = shutil.which
        monkeypatch.setattr(
            copilot.shutil, "which",
            lambda name, *a, **k: "/usr/bin/gh" if name == "gh" else real_which(name, *a, **k),
        )
        monkeypatch.setattr(
            copilot, "_gh_user", lambda: pytest.fail("the router's gh login was asked")
        )
        config = self.rig_config(tmp_path, {"COPILOT_HOME": str(paths["child"])})
        assert self.fuel(config) == 0
        assert used == ["synthetic-child"]

    def test_a_rig_without_its_own_env_still_uses_the_routers_login(
        self, tmp_path, homes, monkeypatch
    ):
        paths, used = homes
        monkeypatch.setattr(copilot.shutil, "which", lambda name, *a, **k: None)
        assert self.fuel(self.rig_config(tmp_path, None)) == 0
        assert used == ["synthetic-router"]

    def test_a_rig_account_with_no_token_never_falls_back_to_the_router(
        self, tmp_path, homes, monkeypatch, capsys
    ):
        paths, used = homes
        empty = tmp_path / "empty"
        empty.mkdir()
        real_which = shutil.which
        monkeypatch.setattr(
            copilot.shutil, "which",
            lambda name, *a, **k: "/usr/bin/gh" if name == "gh" else real_which(name, *a, **k),
        )
        monkeypatch.setattr(copilot, "_gh_user", lambda: pytest.fail("gh asked"))
        config = self.rig_config(tmp_path, {"COPILOT_HOME": str(empty)})
        assert self.fuel(config) == 1
        assert used == []
        assert "rig's account" in capsys.readouterr().err

    def test_one_accounts_snapshot_is_never_served_for_another(self, tmp_path, homes, monkeypatch):
        paths, used = homes
        monkeypatch.setattr(copilot.shutil, "which", lambda name, *a, **k: None)
        router_env = dict(os.environ)
        child_env = dict(os.environ, COPILOT_HOME=str(paths["child"]))
        assert copilot.account_key(router_env) == ""
        assert copilot.account_key(child_env) != ""
        assert engines.snapshot_path("copilot", copilot.account_key(child_env)) != \
            engines.snapshot_path("copilot")
        engines.quota("copilot", env=child_env)
        assert engines.load_snapshot("copilot", copilot.account_key(child_env)) is not None
        assert engines.load_snapshot("copilot") is None

    def test_an_engine_that_cannot_read_the_rig_home_says_so(self, monkeypatch, tmp_path):
        from engines import claude

        monkeypatch.setattr(
            claude, "quota", lambda: {"origin": "snapshot", "buckets": [], "note": None}
        )
        monkeypatch.setenv("R4T_HOME", str(tmp_path / "state"))
        payload = engines.quota(
            "claude", env=dict(os.environ, CLAUDE_CONFIG_DIR=str(tmp_path / "other"))
        )
        assert "CLAUDE_CONFIG_DIR" in payload["note"] and "router" in payload["note"]


# --- the choke point: no spawn site goes round the floor ---------------------------

# Where each kind of launch may be built. A site not listed here is a new path to
# an engine: route it through the effective rig (`RigConfig.effective_rig` /
# `rig_for`) or the node floor (`deny_floor=`), then list it with the reason.
RIG_ARGV_SITES = {
    # (module, function): how the rig it holds reached the floor
    ("dispatch.py", "_run_harness_turn"): "rig_for -> effective_rig (the caller)",
    ("engine_memory.py", "writer"): "config.effective_rig",
}
DISTILL_COMMAND_SITES = {("knowledge.py", "_bridge_env"): "resolve_distill_rig -> effective_rig"}
RUN_HARNESS_SITES = {("judge.py", "_invoke_chunks"): "_resolve_rig -> effective_rig"}
# Raw machine-rig lookups that start no launch: inspection and display only.
RAW_RIG_LOOKUP_ONLY = {
    ("detect.py", "stale_cursor_rigs"),
    ("r4t.py", "_rig_rows"),
    ("r4t.py", "_print_rig_table"),
    ("r4t.py", "cmd_rig_fuel"),
    ("engine_memory.py", "writer"),  # the source rig is read for its env only
}


def source_trees():
    for path in sorted(list(R4T_DIR.glob("*.py")) + list((R4T_DIR / "engines").glob("*.py"))):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def calls_with_scope(tree):
    """Every Call with the name of its enclosing function."""
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def enclosing(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node.name
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node, enclosing(node)


def called_name(call):
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def names_in(function_node):
    found = set()
    for sub in ast.walk(function_node):
        if isinstance(sub, ast.Name):
            found.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            found.add(sub.attr)
    return found


def functions_by_name(tree):
    return {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def test_every_argv_builder_call_names_its_deny_floor():
    """`deny_floor` has a default so tests can omit it; production may not."""
    builders = {"build_argv", "_build_argv_template", "execute"}
    seen = 0
    for path, tree in source_trees():
        for call, scope in calls_with_scope(tree):
            f = call.func
            engine_execute = called_name(call) != "execute" or (
                isinstance(f, ast.Attribute) and getattr(f.value, "id", "") == "engine_run"
            ) or isinstance(f, ast.Name)
            if called_name(call) in builders and engine_execute and path.name != "check.py":
                seen += 1
                keywords = {k.arg for k in call.keywords}
                assert "deny_floor" in keywords or any(k.arg is None for k in call.keywords), (
                    f"{path.name}:{call.lineno} calls {called_name(call)} without "
                    f"deny_floor= (in {scope}): that launch would drop the node floor"
                )
    assert seen >= 5


def test_check_names_the_floor_it_composes_with():
    tree = ast.parse((R4T_DIR / "engines" / "check.py").read_text(encoding="utf-8"))
    for call, scope in calls_with_scope(tree):
        if called_name(call) == "_build_argv_template":
            assert "deny_floor" in {k.arg for k in call.keywords}


@pytest.mark.parametrize(
    "attribute,allowed",
    [("argv", RIG_ARGV_SITES), ("distill_command", DISTILL_COMMAND_SITES),
     ("run_harness", RUN_HARNESS_SITES)],
)
def test_a_launch_is_built_only_at_a_listed_site(attribute, allowed):
    found = set()
    for path, tree in source_trees():
        if path.name == "rig.py":
            continue
        for call, scope in calls_with_scope(tree):
            f = call.func
            is_method = isinstance(f, ast.Attribute) and f.attr == attribute
            is_function = isinstance(f, ast.Name) and f.id == attribute
            if attribute == "argv" and not is_method:
                continue
            if attribute != "argv" and not (is_method or is_function):
                continue
            if attribute == "run_harness" and path.name == "dispatch.py":
                continue  # the definition's own wrapper chain
            found.add((path.name, scope))
            assert (path.name, scope) in allowed, (
                f"{path.name}:{call.lineno} builds a launch with {attribute} in "
                f"{scope}, which is not a listed site: route it through "
                f"RigConfig.effective_rig and list it with the reason"
            )
    assert found, "the scan found no site at all, so it proves nothing"


def test_a_raw_rig_lookup_is_followed_by_the_floor():
    """Reading a machine rig out of the config by name is how a launch skips
    `effective_rig`. Each function that does it either resolves the effective
    rig too, or is a listed inspection."""
    for path, tree in source_trees():
        if path.name == "rig.py":
            continue
        fns = functions_by_name(tree)
        for node in ast.walk(tree):
            raw = (
                isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
                and isinstance(node.value, ast.Attribute) and node.value.attr == "rigs"
            ) or (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "rigs"
            )
            if not raw:
                continue
            owner = next(
                (name for name, fn in fns.items()
                 if fn.lineno <= node.lineno <= (fn.end_lineno or fn.lineno)),
                None,
            )
            if owner is None:
                continue
            body = names_in(fns[owner])
            ok = (path.name, owner) in RAW_RIG_LOOKUP_ONLY or "effective_rig" in body
            assert ok, (
                f"{path.name}:{node.lineno} {owner} reads a rig by name and never "
                f"calls effective_rig: its launch would skip the deny floor"
            )


# --- round 2: a probe or a cache must see what the spawn sees ---------------------


def stub_identity(directory: Path, version: str) -> Path:
    """A `claude` that reports which build and which HOME it really ran with."""
    return write_path_executable(
        directory, "claude",
        "import json, os, sys\n"
        "if '--version' in sys.argv:\n"
        f"    print({version!r})\n"
        "    sys.exit(0)\n"
        f"print(json.dumps({{'version': {version!r}, 'home': os.environ.get('HOME'),"
        " 'argv': sys.argv[1:]}))\n",
    )


@posix_only
class TestBridgeUsesTheEnvironmentItWasChecked:
    """The distill and compile command runs inside k7e, whose environment is
    the router's. The build record and `add_dirs` are checked against the
    rig's `env`, so the command must carry that env or the check vouches for a
    different process."""

    def layout(self, tmp_path):
        good, old = tmp_path / "good", tmp_path / "old"
        stub_identity(good, "2.1.316 (Claude Code)")
        stub_identity(old, "1.0.0")
        home = tmp_path / "rig-home"
        home.mkdir()
        return good, old, home

    def test_rig_path_and_home_reach_the_generated_commands(
        self, ctx, monkeypatch, tmp_path
    ):
        good, old, home = self.layout(tmp_path)
        base = os.environ["PATH"].split(os.pathsep, 1)[1]  # the stub dir is first
        monkeypatch.setenv("PATH", f"{old}{os.pathsep}{base}")
        config = write_config(
            tmp_path / "rigs2.json",
            worker={"env": {"PATH": f"{good}{os.pathsep}{base}", "HOME": str(home)}},
        )
        seen = sweep_env(ctx, monkeypatch, config, named=False)
        for variable in ("K7E_DISTILL_COMMAND", "K7E_COMPILE_COMMAND"):
            done = subprocess.run(
                seen["distill"][variable], shell=True, input="x", capture_output=True,
                text=True, timeout=20, env=dict(os.environ),  # the router's PATH: old first
            )
            assert done.returncode == 0, done.stderr
            ran = json.loads(done.stdout)
            assert ran["version"].startswith("2.1.316") and ran["home"] == str(home)

    def test_the_check_uses_the_rigs_path_not_the_routers(self, ctx, monkeypatch, tmp_path):
        good, old, home = self.layout(tmp_path)
        base = os.environ["PATH"].split(os.pathsep, 1)[1]
        monkeypatch.setenv("PATH", f"{good}{os.pathsep}{base}")
        config = write_config(
            tmp_path / "rigs2.json",
            worker={"env": {"PATH": f"{old}{os.pathsep}{base}"}},
        )
        seen = sweep_env(ctx, monkeypatch, config, named=False)
        assert "distill" not in seen  # the rig's own PATH finds the old build
        files = (state.roster_dir(NODE) / "log").glob("*.md")
        text = "".join(f.read_text(encoding="utf-8") for f in files)
        assert "DREAM-SKIP phil" in text and "older than" in text

    def test_a_rig_without_env_gets_no_prefix(self):
        rig = rig_mod.rig_from_spec(
            "w", {"preset": "claude", "invoke": rig_mod.build_preset_invoke("claude")}
        )
        assert "export " not in rig.distill_command("/tmp")

    def test_values_are_quoted(self):
        rig = rig_mod.rig_from_spec(
            "w", {"preset": "claude", "invoke": rig_mod.build_preset_invoke("claude"),
                  "env": {"TOKENISH": "a b'$(touch pwned)"}},
        )
        command = rig.distill_command("/tmp")
        done = subprocess.run(
            command.replace("claude", "env | grep ^TOKENISH= ; true #", 1),
            shell=True, input="x", capture_output=True, text=True, cwd="/tmp",
        )
        assert "TOKENISH=a b'$(touch pwned)" in done.stdout
        assert not Path("/tmp/pwned").exists()


@posix_only
class TestLabJudgeVersionGate:
    def bind(self, tmp_path, stub_dir):
        import lab

        write_path_executable  # PATH already has the autouse stub first
        config = write_config(
            tmp_path / "rigs2.json",
            worker={"model": "m", "invoke": rig_mod.build_preset_invoke("claude", model="m")},
        )
        from types import SimpleNamespace

        return lab.resolve_role(SimpleNamespace(roles={}), "judge", "worker", config), lab

    def test_an_older_build_is_refused_like_every_knob_launch(self, tmp_path, _path_claude):
        stub_claude(_path_claude, "1.0.0")
        import lab

        with pytest.raises(lab.LabError, match="older than"):
            self.bind(tmp_path, _path_claude)

    def test_the_verified_build_binds_and_runs_with_the_denies(self, tmp_path, _path_claude):
        role, lab = self.bind(tmp_path, _path_claude)
        code, out, _err = lab._run_judge(
            rig_mod.resolve_launch(role["invoke"][0], dict(os.environ), cwd=tmp_path),
            role["invoke"], "x", 20,
        )
        assert code == 0 and FLOOR in denies_of(child_argv(out))

    def test_an_absolute_old_binary_in_the_invoke_is_the_one_checked(self, tmp_path):
        import lab
        from types import SimpleNamespace

        old = tmp_path / "old"
        stub_claude(old, "1.0.0")
        invoke = rig_mod.build_preset_invoke("claude", model="m")
        invoke[0] = str(old / "claude")
        config = write_config(
            tmp_path / "rigs2.json", worker={"model": "m", "invoke": invoke}
        )
        with pytest.raises(lab.LabError, match="older than"):
            lab.resolve_role(SimpleNamespace(roles={}), "judge", "worker", config)

    def test_a_rig_env_the_run_does_not_apply_is_refused_when_denies_apply(self, tmp_path):
        import lab
        from types import SimpleNamespace

        config = write_config(
            tmp_path / "rigs2.json",
            worker={"model": "m", "invoke": rig_mod.build_preset_invoke("claude", model="m"),
                    "env": {"FOO": "1"}},
        )
        with pytest.raises(lab.LabError, match="sets env"):
            lab.resolve_role(SimpleNamespace(roles={}), "judge", "worker", config)


class TestCacheKeyIsTheAccount:
    @pytest.fixture
    def quiet(self, tmp_path, monkeypatch):
        monkeypatch.setenv("R4T_HOME", str(tmp_path / "state"))
        monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "router"))
        for name in copilot.TOKEN_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(copilot.shutil, "which", lambda *a, **k: None)

    def env(self, **extra):
        return dict(os.environ, **extra)

    def test_two_tokens_in_one_home_get_two_keys(self, quiet):
        one = copilot.account_key(self.env(COPILOT_GITHUB_TOKEN="synthetic-one"))
        two = copilot.account_key(self.env(COPILOT_GITHUB_TOKEN="synthetic-two"))
        assert one and two and one != two
        assert "synthetic" not in one + two

    def test_a_replaced_login_in_one_home_changes_the_key(self, quiet, tmp_path):
        home = tmp_path / "child"
        home.mkdir()
        env = self.env(COPILOT_HOME=str(home))
        (home / "config.json").write_text('{"copilotTokens": "synthetic-a"}', encoding="utf-8")
        first = copilot.account_key(env)
        (home / "config.json").write_text('{"copilotTokens": "synthetic-b"}', encoding="utf-8")
        assert copilot.account_key(env) not in (first, "", None)

    def test_the_same_credential_keeps_one_key(self, quiet):
        env = self.env(GH_TOKEN="synthetic-same")
        assert copilot.account_key(env) == copilot.account_key(dict(env))

    def test_a_rig_account_with_no_readable_credential_has_no_identity(self, quiet, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert copilot.account_key(self.env(COPILOT_HOME=str(empty))) is None

    def test_the_router_account_keeps_the_shared_entry(self, quiet):
        assert copilot.account_key(self.env()) == ""

    def test_another_accounts_snapshot_is_never_served(self, quiet, monkeypatch):
        from engines.base import QuotaError

        monkeypatch.setattr(
            copilot, "_direct_user",
            lambda auth: {"quota_snapshots": {"premium_interactions": {"percent_remaining": 75}}},
        )
        engines.quota("copilot", env=self.env(COPILOT_GITHUB_TOKEN="synthetic-one"))

        def offline(auth):
            raise QuotaError("synthetic offline")

        monkeypatch.setattr(copilot, "_direct_user", offline)
        with pytest.raises(QuotaError):
            engines.quota("copilot", env=self.env(COPILOT_GITHUB_TOKEN="synthetic-two"))
        # ...while the same account still gets its own snapshot.
        again = engines.quota("copilot", env=self.env(COPILOT_GITHUB_TOKEN="synthetic-one"))
        assert again["origin"] == "snapshot"

    def test_an_unidentifiable_account_neither_saves_nor_serves(self, quiet, tmp_path, monkeypatch):
        empty = tmp_path / "empty"
        empty.mkdir()
        from engines.base import QuotaError

        with pytest.raises(QuotaError):
            engines.quota("copilot", env=self.env(COPILOT_HOME=str(empty)))
        assert not list((tmp_path / "state").rglob("copilot*.json"))

    def test_no_file_name_carries_a_token(self, quiet, tmp_path, monkeypatch):
        monkeypatch.setattr(
            copilot, "_direct_user",
            lambda auth: {"quota_snapshots": {"premium_interactions": {"percent_remaining": 75}}},
        )
        engines.quota("copilot", env=self.env(COPILOT_GITHUB_TOKEN="synthetic-secret"))
        names = [p.name for p in (tmp_path / "state").rglob("*")]
        assert any(n.startswith("copilot-") for n in names)
        assert not any("synthetic-secret" in n for n in names)
        assert "synthetic-secret" not in "".join(
            p.read_text(encoding="utf-8") for p in (tmp_path / "state").rglob("copilot-*.json")
        )


class TestOtherProbesSeeTheSpawnEnvironment:
    def test_the_usage_flag_probe_uses_the_rigs_path_and_is_cached_by_file(
        self, tmp_path, monkeypatch
    ):
        flagged, plain = tmp_path / "flagged", tmp_path / "plain"
        write_path_executable(
            flagged, "copilot", "import sys\nprint('--usage-output-file 1.0.105')\n"
        )
        write_path_executable(plain, "copilot", "import sys\nprint('copilot 0.0.1')\n")
        monkeypatch.setattr(copilot, "_usage_support", {})
        base = os.environ["PATH"]
        assert copilot.supports_usage_file(rig_mod.resolve_launch(
            "copilot", dict(os.environ, PATH=f"{flagged}{os.pathsep}{base}"), cwd=tmp_path
        )) is True
        # Same process, same name, a different PATH: not answered from the first.
        assert copilot.supports_usage_file(rig_mod.resolve_launch(
            "copilot", dict(os.environ, PATH=f"{plain}{os.pathsep}{base}"), cwd=tmp_path
        )) is False

    def test_the_agy_model_list_is_read_with_the_spawn_environment(self, tmp_path, monkeypatch):
        write_path_executable(
            tmp_path / "bin", "agy",
            "import os\nprint('x\\t' + os.environ.get('AGY_PROBE', 'router'))\n",
        )
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
        launch = rig_mod.resolve_launch("agy", dict(os.environ, AGY_PROBE="rig"), cwd=tmp_path)
        assert rig_mod.resolve_agy_model("rig", launch=launch) == "rig"

    def test_the_park_probe_looks_on_the_rigs_path(self, tmp_path, r4t_home):
        bin_dir = tmp_path / "rigbin"
        write_path_executable(bin_dir, "tool-only-the-rig-has", "pass\n")
        assert dispatch._probe_resolves("tool-only-the-rig-has") is None
        assert dispatch._probe_resolves("tool-only-the-rig-has", str(bin_dir))
        record = state.park_member(
            "n", "a", reason="r", rig="x", probe="t", probe_path=str(bin_dir)
        )
        assert record["probe_path"] == str(bin_dir)


# --- round 3: the launch is resolved once, and every probe and the spawn use it ---------


def recording_stub(directory: Path, name: str, version: str, record: Path, *, usage=False) -> Path:
    """A CLI that appends the path it was started as, and its argv, to `record`
    on EVERY invocation (version probe, help probe and the real run alike)."""
    return write_path_executable(
        directory, name,
        "import json, os, sys\n"
        f"with open({str(record)!r}, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps({'self': os.path.abspath(sys.argv[0]), 'argv': sys.argv[1:], "
        "'cwd': os.path.realpath(os.getcwd()), 'env': dict(os.environ)}) + '\\n')\n"
        "if '--version' in sys.argv:\n"
        f"    print({version!r})\n"
        "    sys.exit(0)\n"
        "if '--help' in sys.argv:\n"
        f"    print({('--usage-output-file' if usage else '')!r})\n"
        "    sys.exit(0)\n"
        "if '--usage-output-file' in sys.argv and "
        f"not {usage!r}:\n"
        "    print('unsupported --usage-output-file', file=sys.stderr)\n"
        "    sys.exit(2)\n"
        "print(json.dumps(sys.argv[1:]))\n",
    )


def records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@posix_only
class TestOneExecutableForEveryProbeAndTheSpawn:
    """A decoy on PATH, the real file named by the rig. Each launch type must
    start the real file for the version gate, the feature probes and the spawn,
    and never touch the decoy."""

    @pytest.fixture
    def pair(self, tmp_path, monkeypatch):
        decoy_log, real_log = tmp_path / "decoy.jsonl", tmp_path / "real.jsonl"
        decoy_dir, real_dir = tmp_path / "decoy", tmp_path / "real"
        recording_stub(decoy_dir, "claude", "2.1.316 (Claude Code)", decoy_log)
        real = recording_stub(real_dir, "claude", "2.1.316 (Claude Code)", real_log)
        monkeypatch.setenv("PATH", f"{decoy_dir}{os.pathsep}{os.environ['PATH']}")
        return SimpleNamespace(decoy_log=decoy_log, real_log=real_log, real=real, real_dir=real_dir)

    def check(self, pair):
        assert records(pair.decoy_log) == [], "the decoy on PATH was started"
        seen = records(pair.real_log)
        assert {r["self"] for r in seen} == {str(pair.real)}
        assert any("--version" in r["argv"] for r in seen), "no version probe ran"
        assert any("-p" in r["argv"] for r in seen), "the model call never ran"

    def rig_config(self, tmp_path, pair, **extra):
        invoke = rig_mod.build_preset_invoke("claude", model="m")
        invoke[0] = str(pair.real)
        return write_config(
            tmp_path / "rigs3.json",
            worker={"invoke": invoke, "deny_tools": "Bash(rm:*)", **extra},
        )

    def test_a_roster_turn(self, tmp_path, pair):
        rig = load_rig_config(self.rig_config(tmp_path, pair)).rigs["worker"]
        code, out, *_ = dispatch.run_harness(rig, "p", tmp_path, env=dict(os.environ))
        assert code == 0, out
        self.check(pair)

    def test_the_judge(self, tmp_path, pair):
        import judge

        rig = judge._resolve_rig("worker", self.rig_config(tmp_path, pair))
        code, out, *_ = judge.run_harness(rig, "p", tmp_path)
        assert code == 0, out
        self.check(pair)

    def test_the_lab_judge(self, tmp_path, pair):
        import lab

        role = lab.resolve_role(
            SimpleNamespace(roles={}), "judge", "worker",
            self.rig_config(tmp_path, pair, model="m"),
        )
        code, out, _ = lab._run_judge(
            rig_mod.resolve_launch(role["invoke"][0], dict(os.environ), cwd=tmp_path),
            role["invoke"], "p", 20,
        )
        assert code == 0, out
        self.check(pair)

    @pytest.mark.parametrize("named", [False, True], ids=["default", "named"])
    def test_distill_and_compile(self, ctx, monkeypatch, tmp_path, pair, named):
        config = self.rig_config(tmp_path, pair)
        seen = sweep_env(ctx, monkeypatch, config, named=named)
        for variable in ("K7E_DISTILL_COMMAND", "K7E_COMPILE_COMMAND"):
            assert subprocess.run(
                seen["distill"][variable], shell=True, input="x", capture_output=True,
                text=True, timeout=20,
            ).returncode == 0
        self.check(pair)
        runs = [r for r in records(pair.real_log) if "-p" in r["argv"]]
        assert len(runs) == 2  # one distill command, one compile command

    def test_the_memory_writer(self, tmp_path, pair):
        config = write_config(
            tmp_path / "rigs3.json",
            worker={"env": {"PATH": f"{pair.real_dir}{os.pathsep}{os.environ['PATH']}"}},
        )
        result = run_writer(
            tmp_path, {"rig": "worker", "context": {"config": str(config)}},
            {"PATH": f"{tmp_path / 'decoy'}{os.pathsep}{os.environ['PATH']}"},
        )
        assert result.returncode == 0, result.stderr
        self.check(pair)

    def test_engine_run(self, tmp_path, pair, monkeypatch):
        monkeypatch.setenv("PATH", f"{pair.real_dir}{os.pathsep}{os.environ['PATH']}")
        code = r4t.main([
            "engine", "claude", "run", "--rig-config", str(write_config(tmp_path / "r.json")),
            "--dir", str(tmp_path), "--no-scaffold", "hi",
        ])
        assert code == 0
        seen = records(pair.real_log)
        assert {r["self"] for r in seen} == {str(pair.real)}
        assert records(pair.decoy_log) == []
        assert any("--version" in r["argv"] for r in seen)


@posix_only
class TestUsageFlagProbesTheSelectedBinary:
    """Artemis round 3: PATH holds a copilot that takes --usage-output-file, the
    rig invokes an older absolute one that does not."""

    def setup(self, tmp_path, monkeypatch, *, path_usage: bool, abs_usage: bool):
        logs = {k: tmp_path / f"{k}.jsonl" for k in ("path", "abs")}
        recording_stub(tmp_path / "pathbin", "copilot", "1.0.105", logs["path"], usage=path_usage)
        absolute = recording_stub(tmp_path / "absbin", "copilot", "1.0.50", logs["abs"], usage=abs_usage)
        monkeypatch.setenv("PATH", f"{tmp_path / 'pathbin'}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setattr(copilot, "_usage_support", {})
        invoke = rig_mod.build_preset_invoke("copilot")
        invoke[0] = str(absolute)
        rig = rig_mod.rig_from_spec("cp", {"preset": "copilot", "invoke": invoke, "mcp": False})
        return rig, logs, absolute

    def test_the_old_absolute_binary_gets_no_flag_it_cannot_take(self, tmp_path, monkeypatch):
        rig, logs, absolute = self.setup(tmp_path, monkeypatch, path_usage=True, abs_usage=False)
        code, out, *_ = dispatch.run_harness(rig, "t", tmp_path, env=dict(os.environ))
        assert code == 0, out
        assert records(logs["path"]) == []
        runs = [r for r in records(logs["abs"]) if "-p" in r["argv"]]
        assert runs and "--usage-output-file" not in runs[0]["argv"]

    def test_a_supporting_absolute_binary_gets_the_flag(self, tmp_path, monkeypatch):
        rig, logs, absolute = self.setup(tmp_path, monkeypatch, path_usage=False, abs_usage=True)
        code, out, *_ = dispatch.run_harness(rig, "t", tmp_path, env=dict(os.environ))
        assert code == 0, out
        assert records(logs["path"]) == []
        runs = [r for r in records(logs["abs"]) if "-p" in r["argv"]]
        assert runs and "--usage-output-file" in runs[0]["argv"]

    def test_each_pool_variant_is_probed_by_its_own_file(self, tmp_path, monkeypatch):
        rig, logs, absolute = self.setup(tmp_path, monkeypatch, path_usage=True, abs_usage=False)
        newer = recording_stub(tmp_path / "newbin", "copilot", "1.0.105", tmp_path / "new.jsonl", usage=True)
        first = rig_mod.build_preset_invoke("copilot")
        first[0] = str(newer)
        second = rig_mod.build_preset_invoke("copilot")
        second[0] = str(absolute)
        pooled = rig_mod.rig_from_spec("pool", {"preset": "copilot", "invoke": [first, second], "mcp": False})
        for variant, expect in ((0, True), (1, False)):
            code, out, *_ = dispatch.run_harness(
                pooled, "t", tmp_path, env=dict(os.environ), variant=variant
            )
            assert code == 0, out
            ran = [r for r in records(tmp_path / ("new.jsonl" if variant == 0 else "abs.jsonl")) if "-p" in r["argv"]]
            assert ("--usage-output-file" in ran[-1]["argv"]) is expect

    def test_an_isolated_launch_is_never_probed_on_a_guessed_host_path(self, tmp_path, monkeypatch):
        rig, logs, absolute = self.setup(tmp_path, monkeypatch, path_usage=True, abs_usage=True)
        launch = rig_mod.resolve_launch(str(absolute), dict(os.environ), cwd=tmp_path, isolated=True)
        assert launch.binary is None and launch.pin(["x", "y"]) == ["x", "y"]
        assert copilot.supports_usage_file(launch) is False
        record = access_binary_state("copilot", launch)
        assert record["status"] == "unverified isolated"
        assert records(logs["path"]) == [] and records(logs["abs"]) == []


@posix_only
class TestDistillTransportIsIndependentOfTheRigPath:
    def narrow(self, tmp_path, monkeypatch):
        """The reviewer's case: the rig's PATH holds only the CLI's folder."""
        record = tmp_path / "cli.jsonl"
        only = tmp_path / "only-cli"
        recording_stub(only, "claude", "2.1.316 (Claude Code)", record)
        rig = rig_mod.rig_from_spec("w", {
            "preset": "claude", "invoke": rig_mod.build_preset_invoke("claude"),
            "deny_tools": "Bash(touch:*)", "mcp": False,
            "env": {"PATH": str(only), "HOME": str(tmp_path)},
        })
        ctx = SimpleNamespace(isolation=isolate.Isolation(), workplace=tmp_path)
        return knowledge._bridge_env(ctx, Member(name="W", rig="w"), rig), record

    def test_a_path_with_only_the_cli_still_delivers_the_prompt(self, tmp_path, monkeypatch):
        bridge, record = self.narrow(tmp_path, monkeypatch)
        for variable in ("K7E_DISTILL_COMMAND", "K7E_COMPILE_COMMAND"):
            done = subprocess.run(
                bridge[variable], shell=True, input="SYNTHETIC_PROMPT",
                capture_output=True, text=True, timeout=20,
            )
            assert done.returncode == 0 and "not found" not in done.stderr
            assert child_argv(done.stdout)[-1] == "SYNTHETIC_PROMPT"
        ran = [r for r in records(record) if "-p" in r["argv"]]
        assert [r["argv"][-1] for r in ran] == ["SYNTHETIC_PROMPT", "SYNTHETIC_PROMPT"]

    def test_the_control_with_a_full_path_is_the_same(self, tmp_path, monkeypatch):
        record = tmp_path / "cli.jsonl"
        recording_stub(tmp_path / "bin", "claude", "2.1.316 (Claude Code)", record)
        rig = rig_mod.rig_from_spec("w", {
            "preset": "claude", "invoke": rig_mod.build_preset_invoke("claude"),
            "mcp": False, "env": {"PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}"},
        })
        command = rig.distill_command(tmp_path, launch=rig_mod.resolve_launch("claude", {**os.environ, **rig.env}, cwd=tmp_path))
        done = subprocess.run(command, shell=True, input="SYNTHETIC_PROMPT", capture_output=True, text=True, timeout=20)
        assert done.returncode == 0 and child_argv(done.stdout)[-1] == "SYNTHETIC_PROMPT"

    def test_an_empty_transport_fails_before_the_launch(self, tmp_path, monkeypatch):
        bridge, record = self.narrow(tmp_path, monkeypatch)
        for variable in ("K7E_DISTILL_COMMAND", "K7E_COMPILE_COMMAND"):
            done = subprocess.run(
                bridge[variable], shell=True, input="", capture_output=True, text=True, timeout=20,
            )
            assert done.returncode == 66 and "no prompt" in done.stderr
        assert [r for r in records(record) if "-p" in r["argv"]] == []

    def test_a_failed_cat_fails_before_the_launch(self, tmp_path, monkeypatch):
        bridge, record = self.narrow(tmp_path, monkeypatch)
        # The parent has no `cat` at all: the transport itself fails.
        done = subprocess.run(
            bridge["K7E_DISTILL_COMMAND"], shell=True, input="x", capture_output=True,
            text=True, timeout=20, env={"PATH": str(tmp_path / "nothing")},
            executable="/bin/sh",
        )
        assert done.returncode != 0
        assert [r for r in records(record) if "-p" in r["argv"]] == []


# --- the structural rule: probes take a Launch; only the resolver finds a binary ------------

LAUNCH_PROBES = {
    ("rig.py", "access_binary_state"),
    ("rig.py", "agy_model_names"),
    ("rig.py", "resolve_agy_model"),
    ("engines/copilot.py", "supports_usage_file"),
}
# Every other `shutil.which` in the product, with why it is not a probe of a
# launch. A new line here is a new way to certify a binary the spawn may not run.
WHICH_ALLOWED = {
    ("engines/run.py", "resolve_argv0"): "Windows .cmd/.bat shim lookup at spawn; takes the final argv",
    ("engines/run.py", "interpreter_gap"): "diagnostic after an exit 127",
    ("engines/resident.py", "_program_stamp"): "identity stamp of a resident session's program; residents refuse knobs and floor",
    ("engines/codex.py", "quota"): "account quota reader, not a launch",
    ("engines/devin.py", "_usage_screen"): "account quota reader, not a launch",
    ("engines/muse.py", "quota"): "account quota reader, not a launch",
    ("engines/copilot.py", "quota"): "the gh transport of the quota reader, not a launch",
    ("engines/agy.py", "_listen_ports"): "`lsof` utility for the quota reader",
}


def _functions(tree):
    return {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def test_launch_probes_take_a_launch_and_never_resolve_a_binary():
    for relative, name in sorted(LAUNCH_PROBES):
        tree = ast.parse((R4T_DIR / relative).read_text(encoding="utf-8"))
        function = _functions(tree)[name]
        params = [a.arg for a in function.args.args + function.args.kwonlyargs]
        assert "launch" in params, f"{relative}:{name} must take the resolved `launch`"
        assert not {"binary", "argv0", "engine", "env", "child_env"} & set(params), (
            f"{relative}:{name} takes a bare name or environment: it could resolve its own binary"
        )
        for call, _ in calls_with_scope(function):
            assert called_name(call) != "which", f"{relative}:{name} resolves a binary itself"
            assert "environ" not in ast.unparse(call.func) or name == "access_binary_state", (
                f"{relative}:{name} reads the process environment"
            )


def test_shutil_which_appears_only_in_the_resolver_and_listed_places():
    found = set()
    for path, tree in source_trees():
        relative = path.relative_to(R4T_DIR).as_posix()
        for function in _functions(tree).values():
            for call, _ in calls_with_scope(function):
                f = call.func
                if isinstance(f, ast.Attribute) and f.attr == "which":
                    found.add((relative, function.name))
    unlisted = sorted(found - set(WHICH_ALLOWED))
    assert not unlisted, (
        f"shutil.which outside the resolver: {unlisted}. Resolve the launch once "
        f"with rig.resolve_launch and pass it to the probe; or list the place "
        f"with the reason it vouches for no launch."
    )
    assert found == set(WHICH_ALLOWED), (
        f"stale entries in WHICH_ALLOWED: {sorted(set(WHICH_ALLOWED) - found)}"
    )


# --- round 4: the probe and the spawn agree on the binary, the env AND the cwd -----------

IGNORED_BY_THE_INTERPRETER = {"LC_CTYPE", "LC_ALL", "PYTHONCOERCECLOCALE", "PYTHONUTF8"}


def seen_env(row: dict) -> dict:
    return {k: v for k, v in row["env"].items() if k not in IGNORED_BY_THE_INTERPRETER}


def probe_and_run(path: Path) -> tuple[dict, dict]:
    """The version-probe row and the real-run row a recording stub wrote."""
    rows = records(path)
    probes = [r for r in rows if "--version" in r["argv"]]
    runs = [r for r in rows if "-p" in r["argv"]]
    assert probes, "no version probe was recorded"
    assert runs, "the model call was never recorded"
    return probes[0], runs[-1]


def assert_agree(probe: dict, run: dict, *, exact_env: bool = True) -> None:
    """The probe and the spawn ran the same file, in the same directory, with
    the same HOME and the same rig marker; and, where the launch is the only
    thing between them, the very same environment."""
    assert probe["self"] == run["self"]
    assert probe["cwd"] == run["cwd"]
    for name in ("HOME", "RIG_MARKER", "PATH"):
        assert probe["env"].get(name) == run["env"].get(name), name
    if exact_env:
        assert seen_env(probe) == seen_env(run)


@posix_only
class TestProbeAndSpawnAgreeOnEverything:
    """Router cwd different from the member's cwd, a rig HOME and a rig marker
    variable that the parent environment does not have."""

    @pytest.fixture
    def world(self, tmp_path, monkeypatch):
        router, member = tmp_path / "router", tmp_path / "member"
        router.mkdir()
        member.mkdir()
        monkeypatch.chdir(router)
        monkeypatch.setenv("HOME", str(tmp_path / "parent-home"))
        monkeypatch.delenv("RIG_MARKER", raising=False)
        record = tmp_path / "record.jsonl"
        binary = recording_stub(tmp_path / "abs", "claude", "2.1.316 (Claude Code)", record)
        rig_env = {"HOME": str(tmp_path / "rig-home"), "RIG_MARKER": "rig"}
        return SimpleNamespace(
            router=router, member=member, record=record, binary=binary,
            rig_env=rig_env, tmp=tmp_path,
        )

    def config(self, world, **extra):
        invoke = rig_mod.build_preset_invoke("claude", model="m")
        invoke[0] = str(world.binary)
        return write_config(
            world.tmp / "rigs4.json",
            worker={"invoke": invoke, "deny_tools": "Bash(rm:*)", "model": "m",
                    "env": world.rig_env, **extra},
        )

    def test_a_roster_turn(self, world):
        rig = load_rig_config(self.config(world)).rigs["worker"]
        code, out, *_ = dispatch.run_harness(rig, "p", world.member, env=dict(os.environ))
        assert code == 0, out
        probe, run = probe_and_run(world.record)
        assert_agree(probe, run)
        assert probe["cwd"] == os.path.realpath(world.member)
        assert run["env"]["HOME"] == world.rig_env["HOME"]
        assert run["env"]["RIG_MARKER"] == "rig"
        assert run["env"]["PWD"] == str(world.member)

    def test_the_judge_with_no_caller_env(self, world):
        """The real judge path calls run_harness with env=None: the spawn used
        to take the parent's env while the probe took the rig's."""
        import judge

        rig = judge._resolve_rig("worker", self.config(world))
        code, out, *_ = judge.run_harness(rig, "p", world.member)
        assert code == 0, out
        probe, run = probe_and_run(world.record)
        assert_agree(probe, run)
        assert run["env"]["HOME"] == world.rig_env["HOME"]
        assert run["env"]["RIG_MARKER"] == "rig"

    def test_the_judge_leaks_none_of_the_routers_turn_keys(self, world, monkeypatch):
        import judge

        monkeypatch.setenv("TELL_OUTBOX_DIR", str(world.tmp / "router-outbox"))
        rig = judge._resolve_rig("worker", self.config(world))
        code, out, *_ = judge.run_harness(rig, "p", world.member)
        assert code == 0, out
        # Whatever the process env carries, the probe and the run saw the same set.
        probe, run = probe_and_run(world.record)
        assert_agree(probe, run)

    def test_the_lab_judge(self, world):
        import lab

        role = lab.resolve_role(
            SimpleNamespace(roles={}), "judge", "worker",
            write_config(world.tmp / "rigs5.json", worker={
                "invoke": [*rig_mod.build_preset_invoke("claude", model="m")[:0],
                           str(world.binary), *rig_mod.build_preset_invoke("claude", model="m")[1:]],
                "model": "m"}),
        )
        trial = world.tmp / "trial"
        trial.mkdir()
        launch = rig_mod.resolve_launch(role["invoke"][0], dict(os.environ), cwd=trial)
        code, out, _ = lab._run_judge(launch, role["invoke"], "p", 20)
        assert code == 0, out
        probe, run = probe_and_run(world.record)
        assert probe["self"] == run["self"] == str(world.binary)
        assert run["cwd"] == os.path.realpath(trial)

    @pytest.mark.parametrize("named", [False, True], ids=["default", "named"])
    def test_distill_and_compile(self, ctx, monkeypatch, world, named):
        ctx.workplace = world.member
        seen = sweep_env(ctx, monkeypatch, self.config(world), named=named)
        for variable in ("K7E_DISTILL_COMMAND", "K7E_COMPILE_COMMAND"):
            done = subprocess.run(
                seen["distill"][variable], shell=True, input="x", capture_output=True,
                text=True, timeout=20,
            )
            assert done.returncode == 0, done.stderr
        rows = records(world.record)
        probe = [r for r in rows if "--version" in r["argv"]][0]
        runs = [r for r in rows if "-p" in r["argv"]]
        assert len(runs) == 2
        for run in runs:
            assert_agree(probe, run, exact_env=False)
            assert run["env"]["HOME"] == world.rig_env["HOME"]
            assert run["env"]["RIG_MARKER"] == "rig"
            assert run["cwd"] == probe["cwd"] == os.path.realpath(ctx.workplace)

    def test_the_memory_writer(self, world):
        config = write_config(world.tmp / "rigs6.json", worker={
            "env": {**world.rig_env, "PATH": f"{world.binary.parent}{os.pathsep}{os.environ['PATH']}"},
            "deny_tools": "Bash(rm:*)",
        })
        result = run_writer(world.tmp, {"rig": "worker", "context": {"config": str(config)}})
        assert result.returncode == 0, result.stderr
        probe, run = probe_and_run(world.record)
        assert_agree(probe, run)
        assert run["env"]["HOME"] == world.rig_env["HOME"]
        assert run["env"]["RIG_MARKER"] == "rig"
        assert run["cwd"] == os.path.realpath(world.tmp / "memory" / "writer")

    def test_engine_run(self, world, monkeypatch):
        monkeypatch.setenv("PATH", f"{world.binary.parent}{os.pathsep}{os.environ['PATH']}")
        code = r4t.main([
            "engine", "claude", "run", "--rig-config", str(write_config(world.tmp / "r.json")),
            "--dir", str(world.member), "--no-scaffold", "hi",
        ])
        assert code == 0
        probe, run = probe_and_run(world.record)
        assert_agree(probe, run)
        assert run["cwd"] == os.path.realpath(world.member)

    def test_agy_models(self, world):
        record = world.tmp / "agy.jsonl"
        agy = recording_stub(world.tmp / "agybin", "agy", "1.4.12", record)
        launch = rig_mod.resolve_launch(
            "agy", {**os.environ, **world.rig_env, "PATH": str(agy.parent)}, cwd=world.member
        )
        # The stub prints its argv for `models`; the list parse needs a line.
        rig_mod.agy_model_names(launch=launch)
        [row] = records(record)
        assert row["self"] == str(agy) and row["argv"] == ["models"]
        assert row["cwd"] == os.path.realpath(world.member)
        assert row["env"]["HOME"] == world.rig_env["HOME"]
        assert row["env"]["RIG_MARKER"] == "rig"

    def test_the_spawned_env_is_exactly_the_launchs(self, world):
        env = {"PATH": os.environ["PATH"], "ONLY": "this", "HOME": str(world.tmp)}
        launch = rig_mod.resolve_launch(str(world.binary), env, cwd=world.member)
        proc = launch.start([str(world.binary), "-p", "x"], stdout=subprocess.PIPE, text=True)
        proc.communicate()
        [row] = records(world.record)
        assert seen_env(row) == {k: v for k, v in env.items() if k not in IGNORED_BY_THE_INTERPRETER}


@posix_only
class TestRelativeFormsResolveAgainstTheChildsCwd:
    """The router's cwd and the member's differ, each has a `bin/claude`: the
    one the child would start is the one probed and the one started."""

    @pytest.fixture
    def two(self, tmp_path, monkeypatch):
        router, member = tmp_path / "router", tmp_path / "member"
        records_ = {}
        for place, version in ((router, "2.1.316 (Claude Code)"), (member, "1.0.0")):
            log = place.parent / f"{place.name}.jsonl"
            recording_stub(place / "bin", "claude", version, log)
            recording_stub(place, "claude", version, log)
            records_[place.name] = log
        monkeypatch.chdir(router)
        return SimpleNamespace(router=router, member=member, logs=records_)

    def rig(self, argv0):
        invoke = rig_mod.build_preset_invoke("claude")
        invoke[0] = argv0
        return rig_mod.rig_from_spec(
            "rel", {"preset": "claude", "invoke": invoke, "deny_tools": "Bash(touch:*)", "mcp": False}
        )

    @pytest.mark.parametrize(
        "argv0,path",
        [("./bin/claude", None), ("claude", "bin"), ("claude", ""), ("claude", ":/nonexistent")],
        ids=["relative-argv0", "relative-path-entry", "empty-path", "empty-first-entry"],
    )
    def test_the_member_cwds_older_binary_is_the_one_refused(self, two, argv0, path):
        env = dict(os.environ)
        if path is not None:
            env["PATH"] = path
        # (an empty PATH entry means the cwd: the member's own folder)
        code, out, *_ = dispatch.run_harness(self.rig(argv0), "t", two.member, env=env)
        assert code == 126 and "older than" in out, out
        assert str(two.member) in out
        assert not two.logs["router"].exists() or records(two.logs["router"]) == []
        assert [r for r in records(two.logs["member"]) if "-p" in r["argv"]] == []

    @pytest.mark.parametrize(
        "argv0,path", [("./bin/claude", None), ("claude", "bin"), ("claude", "")],
        ids=["relative-argv0", "relative-path-entry", "empty-path"],
    )
    def test_the_control_where_the_members_binary_is_current(self, two, argv0, path, tmp_path):
        log = two.logs["member"]
        for place in (two.member / "bin", two.member):
            recording_stub(place, "claude", "2.1.316 (Claude Code)", log)
        env = dict(os.environ)
        if path is not None:
            env["PATH"] = path
        code, out, *_ = dispatch.run_harness(self.rig(argv0), "t", two.member, env=env)
        assert code == 0, out
        probe, run = probe_and_run(log)
        assert_agree(probe, run, exact_env=False)
        assert probe["self"].startswith(str(two.member))
        assert records(two.logs["router"]) == []

    def test_a_resolved_path_is_always_absolute(self, two):
        for argv0, path in (("./bin/claude", "x"), ("claude", "bin"), ("claude", "")):
            launch = rig_mod.resolve_launch(
                argv0, {"PATH": path}, cwd=two.member
            )
            assert launch.binary and os.path.isabs(launch.binary)
            assert launch.binary.startswith(str(two.member))
            assert os.path.isabs(launch.cwd)


@posix_only
class TestNoHostCallWithoutAResolvedFile:
    """A stub on the HOST path must record zero invocations whenever the
    launch is unresolved or isolated."""

    @pytest.fixture
    def host(self, tmp_path, monkeypatch):
        log = tmp_path / "host.jsonl"
        recording_stub(tmp_path / "hostbin", "agy", "1.4.12", log)
        recording_stub(tmp_path / "hostbin", "claude", "2.1.316 (Claude Code)", log)
        recording_stub(tmp_path / "hostbin", "copilot", "1.0.105", log, usage=True)
        monkeypatch.setenv("PATH", f"{tmp_path / 'hostbin'}{os.pathsep}{os.environ['PATH']}")
        return log

    def isolated(self, name, tmp_path):
        return rig_mod.resolve_launch(name, dict(os.environ), cwd=tmp_path, isolated=True)

    def unresolved(self, name, tmp_path):
        return rig_mod.resolve_launch(
            name, {**os.environ, "PATH": str(tmp_path / "empty")}, cwd=tmp_path
        )

    @pytest.mark.parametrize("make", ["isolated", "unresolved"])
    def test_the_models_list_refuses_instead_of_asking_the_host(self, host, tmp_path, make):
        launch = getattr(self, make)("agy", tmp_path)
        assert launch.binary is None
        with pytest.raises(RigError, match="cannot read `agy models`"):
            rig_mod.resolve_agy_model("Host Model", launch=launch)
        with pytest.raises(RigError):
            rig_mod.agy_model_names(launch=launch)
        assert records(host) == []

    def test_the_direct_launch_positive_control(self, host, tmp_path):
        launch = rig_mod.resolve_launch("agy", dict(os.environ), cwd=tmp_path)
        assert launch.binary and not launch.isolated
        rig_mod.agy_model_names(launch=launch)
        assert [r["argv"] for r in records(host)] == [["models"]]

    @pytest.mark.parametrize("make", ["isolated", "unresolved"])
    def test_no_other_probe_calls_the_host_either(self, host, tmp_path, make):
        for name, preset in (("claude", "claude"), ("copilot", "copilot")):
            launch = getattr(self, make)(name, tmp_path)
            record = access_binary_state(preset, launch)
            assert record["version"] is None
            assert copilot.supports_usage_file(launch) is False
            assert launch.probe(["--version"]) is None
        assert records(host) == []

    def test_an_unresolved_turn_never_falls_back_to_a_host_search(self, host, tmp_path):
        rig = rig_mod.rig_from_spec("x", {
            "preset": "claude", "invoke": rig_mod.build_preset_invoke("claude"),
            "mcp": False, "env": {"PATH": str(tmp_path / "empty")},
        })
        code, out, *_ = dispatch.run_harness(rig, "t", tmp_path, env=dict(os.environ))
        assert code == 127 and "not on PATH" in out
        assert records(host) == []

    def test_an_unresolved_launch_refuses_to_start(self, host, tmp_path):
        launch = self.unresolved("claude", tmp_path)
        with pytest.raises(FileNotFoundError):
            launch.start(["claude", "-p", "x"])
        assert records(host) == []

    def test_an_isolated_agy_turn_does_not_read_the_hosts_model_list(self, host, tmp_path):
        rig = load_rig_config(write_config(tmp_path / "rigs7.json", worker={})).rigs["worker"]
        agy_rig = rig_mod.rig_from_spec("brain", {
            "preset": "agy", "invoke": rig_mod.build_preset_invoke("agy", model="flash"),
            "model": "flash", "model_resolver": "agy-live", "mcp": False,
        })
        env = {**os.environ, **isolate.Isolation(run_as="someone").to_env()}
        code, out, *_ = dispatch.run_harness(agy_rig, "t", tmp_path, env=env)
        assert code == 127 and "did not resolve" in out
        assert records(host) == []


# --- source guard: spawn functions take everything from the launch ----------------------

SPAWN_FUNCTIONS = {
    ("dispatch.py", "_run_harness_turn"),
    ("engines/run.py", "_spawn"),
    ("engine_memory.py", "spawn"),
    ("lab.py", "_run_judge"),
}
FORBIDDEN_IN_SPAWN = {"Popen", "run", "call", "check_output", "execvpe", "execve", "execv"}


def test_spawn_functions_take_binary_env_and_cwd_only_from_the_launch():
    for relative, name in sorted(SPAWN_FUNCTIONS):
        tree = ast.parse((R4T_DIR / relative).read_text(encoding="utf-8"))
        function = _functions(tree)[name]
        params = [a.arg for a in function.args.args + function.args.kwonlyargs]
        assert "launch" in params, f"{relative}:{name} must take the launch"
        assert not {"env", "cwd", "directory", "workdir", "dir_path", "binary"} & set(params), (
            f"{relative}:{name} has a parameter that could diverge from the launch"
        )
        for call, _ in calls_with_scope(function):
            assert called_name(call) not in FORBIDDEN_IN_SPAWN, (
                f"{relative}:{name} starts a process itself; it must go through launch.start"
            )
        for node in ast.walk(function):
            # nothing rebuilds the env after the launch exists
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    text = ast.unparse(target)
                    assert not text.startswith("launch.env") and not text.endswith(".env["), text
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("update", "setdefault", "pop") and "env" in ast.unparse(node.func.value):
                    raise AssertionError(
                        f"{relative}:{name} changes an environment after the launch was built"
                    )


def test_every_resolve_launch_call_names_its_cwd_and_no_launch_is_built_by_hand():
    seen = 0
    for path, tree in source_trees():
        for call, scope in calls_with_scope(tree):
            if called_name(call) == "resolve_launch":
                seen += 1
                assert "cwd" in {k.arg for k in call.keywords}, (
                    f"{path.name}:{call.lineno} resolves a launch without the child's cwd"
                )
            if called_name(call) == "Launch" and path.name != "rig.py":
                raise AssertionError(
                    f"{path.name}:{call.lineno} builds a Launch by hand; use resolve_launch"
                )
    assert seen >= 8


def test_no_probe_falls_back_to_a_bare_engine_name():
    """`agy_model_names` and the usage probe never name a CLI as a string."""
    for relative, name in (("rig.py", "agy_model_names"), ("engines/copilot.py", "supports_usage_file")):
        tree = ast.parse((R4T_DIR / relative).read_text(encoding="utf-8"))
        function = _functions(tree)[name]
        literals = {n.value for n in ast.walk(function) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert not {"agy", "copilot", "claude"} & literals, (
            f"{relative}:{name} names a CLI as a string: a fallback to a host lookup"
        )


def test_launch_start_applies_the_launch_policy(monkeypatch, tmp_path):
    """Launch.start is the one engine spawn, so it carries ar3.proc's launch
    policy itself: the default for every caller, and a new session only when
    asked. The process-launch audit holds the source; this holds the call."""
    seen = []

    def fake_popen(argv, **kw):
        seen.append(kw)
        return object()

    def fake_options(*, new_session=False, **_):
        return {"policy": "applied", "new_session": new_session}

    monkeypatch.setattr(rig_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(rig_mod, "launch_options", fake_options)
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    launch = rig_mod.resolve_launch(str(binary), {"PATH": ""}, cwd=tmp_path)
    launch.start([str(binary), "-p", "x"], stdout=subprocess.PIPE)
    launch.start([str(binary), "-p", "x"], new_session=True)
    assert [kw["policy"] for kw in seen] == ["applied", "applied"]
    assert [kw["new_session"] for kw in seen] == [False, True]
    assert seen[0]["stdout"] is subprocess.PIPE
