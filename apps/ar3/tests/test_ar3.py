"""ar3 — greeter panel assembly and the doctor probe registry.

Everything here is hermetic: suite state lives in tmp homes and every probe is
stubbed, so no test reads real state or executes a real harness CLI.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

import cli as ar3


def _row(rows, name):
    for row in rows:
        if row[1] == name:
            return row
    raise AssertionError(f"no {name!r} row in {[r[1] for r in rows]}")


# ---------- rendering ----------

def test_wordmark_is_the_suite_grid():
    assert ar3.WORDMARK == ("A R K", "8 4 7", "S T E")


def test_render_rows_marks_aligns_and_appends_try_hints():
    lines = ar3.render_rows([
        (True, "cli", "a8s -> /somewhere/a8s", None),
        (False, "router", "no agent attached", "a8s start <agent>"),
    ])
    assert lines[0] == "  ✓ cli     a8s -> /somewhere/a8s"
    assert lines[1] == "  ✗ router  no agent attached   (try: a8s start <agent>)"


def test_render_rows_handles_an_empty_section():
    assert ar3.render_rows([]) == ["  (none)"]


# ---------- home resolution matches each product ----------

def test_homes_follow_the_product_env_overrides(homes):
    assert ar3.a8s_home() == homes["a8s"]
    assert ar3.r4t_home() == homes["r4t"]
    assert ar3.k7e_home() == homes["k7e"]


def test_r4t_home_falls_back_to_xdg_config(tmp_path, monkeypatch):
    monkeypatch.delenv("R4T_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert ar3.r4t_home() == tmp_path / "r4t"


def test_k7e_home_falls_back_to_xdg_config(tmp_path, monkeypatch):
    monkeypatch.delenv("K7E_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert ar3.k7e_home() == tmp_path / "k7e"


# ---------- a8s panel ----------

def test_a8s_panel_is_all_misses_on_an_empty_home(homes):
    rows = ar3.a8s_rows()
    assert _row(rows, "cli")[0] is False
    ok, _name, state, hint = _row(rows, "registry")
    assert ok is False
    assert "no registry" in state
    assert hint == "a8s discover <dir>"


def test_a8s_panel_counts_registry_sections_and_reports_idle_router(homes):
    (homes["a8s"] / "a8s.json").write_text(json.dumps({
        "agents": {"one": {"root": "/x"}, "two": {"root": "/y"}},
        "aliases": {"both": ["one", "two"]},
        "namespaces": {},
    }), encoding="utf-8")
    rows = ar3.a8s_rows()
    ok, _name, state, hint = _row(rows, "registry")
    assert ok is True
    assert state == "2 agent(s), 1 alias(es), 0 namespace(s)"
    assert hint is None
    assert _row(rows, "router") == (False, "router", "no agent attached", "a8s start <agent>")


def test_a8s_panel_reports_an_attached_router_from_a_live_pid(homes):
    (homes["a8s"] / "a8s.json").write_text(
        json.dumps({"agents": {"one": {"root": "/x"}}}), encoding="utf-8"
    )
    pid_dir = homes["a8s"] / "agents" / "one"
    pid_dir.mkdir(parents=True)
    (pid_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")
    assert _row(ar3.a8s_rows(), "router") == (True, "router", "attached: one", None)


def test_a8s_panel_ignores_a_stale_pid_file(homes):
    (homes["a8s"] / "a8s.json").write_text(
        json.dumps({"agents": {"one": {"root": "/x"}}}), encoding="utf-8"
    )
    pid_dir = homes["a8s"] / "agents" / "one"
    pid_dir.mkdir(parents=True)
    (pid_dir / "pid").write_text("not-a-pid", encoding="utf-8")
    assert _row(ar3.a8s_rows(), "router")[0] is False


def test_a8s_panel_flags_an_unreadable_registry(homes):
    (homes["a8s"] / "a8s.json").write_text("{ broken", encoding="utf-8")
    ok, _name, state, _hint = _row(ar3.a8s_rows(), "registry")
    assert ok is False
    assert "unreadable" in state


# ---------- r4t panel ----------

def test_r4t_panel_points_at_init_when_nothing_exists(homes):
    rows = ar3.r4t_rows()
    assert _row(rows, "rigs")[3] == "r4t rig add <rig> <preset>"
    assert _row(rows, "rosters")[3] == "r4t add <dir> [<runbook>]"


def test_r4t_panel_counts_only_rig_entries_not_governance_knobs(homes):
    (homes["r4t"] / "rigs.json").write_text(json.dumps({
        "_notes": ["ignored"],
        "throttle": {"min_seconds_between_turn_starts": 0},
        "cell_budget_max": 16,
        "leader": {"invoke": ["claude", "-p", "{prompt}"]},
        "worker": {"invoke": [["opencode", "run", "{prompt}"]]},
    }), encoding="utf-8")
    ok, _name, state, hint = _row(ar3.r4t_rows(), "rigs")
    assert ok is True
    assert state == "2 rig(s): leader, worker"
    assert hint is None


def test_r4t_panel_lists_rosters_under_the_home(homes):
    for node in ("alpha", "beta"):
        (homes["r4t"] / "rosters" / node).mkdir(parents=True)
    assert _row(ar3.r4t_rows(), "rosters")[:3] == (True, "rosters", "2 roster(s): alpha, beta")


# ---------- k7e panel ----------

def test_k7e_panel_hints_the_store_is_created_on_first_write(homes):
    rows = ar3.k7e_rows()
    assert _row(rows, "store")[3] == "k7e store <title>"
    assert not [r for r in rows if r[1] == "index"]


def test_k7e_panel_counts_entries_and_flags_a_missing_index(homes):
    nodes = homes["k7e"] / "nodes"
    nodes.mkdir()
    (nodes / "a.md").write_text("x", encoding="utf-8")
    (nodes / "b.md").write_text("y", encoding="utf-8")
    rows = ar3.k7e_rows()
    assert rows[1][0] is True
    assert "2 entr(ies)" in rows[1][2]
    assert _row(rows, "index") == (False, "index", "no search index", "k7e reindex")


def test_k7e_panel_reports_an_existing_index(homes):
    (homes["k7e"] / "nodes").mkdir()
    (homes["k7e"] / ".index.db").write_bytes(b"0" * 2048)
    ok, _name, state, _hint = _row(ar3.k7e_rows(), "index")
    assert ok is True
    assert state.startswith("2 KiB")


# ---------- greeter ----------

def test_greeter_prints_the_grid_and_one_section_per_product(homes, capsys):
    assert ar3.cmd_default(None) == 0
    out = capsys.readouterr().out
    assert out.startswith("A R K\n8 4 7\nS T E\n")
    for name in ("a8s —", "r4t —", "k7e —"):
        assert name in out
    assert "ar3 doctor" in out


def test_greeter_never_wraps_another_products_verbs():
    """The boundary: ar3's only subcommand is doctor. Suite verbs appear as
    `(try: ...)` hints, never as ar3 subcommands."""
    with pytest.raises(SystemExit):
        ar3.main(["tell", "someone", "hi"])


# ---------- doctor registry ----------

def test_every_check_is_grouped_into_a_rendered_section():
    groups = {check.group for check in ar3.CHECKS}
    assert groups <= {ar3.HARNESS, ar3.SERVICES, ar3.TOOLING}


def test_registry_carries_a_hint_and_probe_for_every_check():
    for check in ar3.CHECKS:
        assert check.hint
        assert callable(check.probe)


def test_registry_covers_the_known_harnesses_and_tools():
    names = {check.name for check in ar3.CHECKS}
    assert {"claude", "agent", "codex", "copilot", "opencode", "agy", "ollama"} <= names
    assert {"ollama serve", "docker", "git", "utf-8 output"} <= names


# ---------- the child's encoding (#275) ----------

def test_the_utf8_probe_says_nothing_is_wrong_off_windows():
    """The launchers set no encoding on POSIX, because a locale is the
    operator's own setting. A probe that reported a finding here would be
    reporting on a choice nobody made."""
    probe = ar3._utf8_report("posix", False, "", "utf-8")
    assert probe.ok
    assert "not applicable" in probe.detail


def test_the_utf8_probe_reads_the_console_the_child_actually_got():
    """A stock Windows console is cp1252, and the suite's own output — arrows,
    em dashes, agent names — raises UnicodeEncodeError on it. Naming the
    stream encoding is what turns that crash into something an operator can
    act on before it happens."""
    probe = ar3._utf8_report("nt", False, "", "cp1252")
    assert not probe.ok
    assert "cp1252" in probe.detail


def test_utf8_mode_satisfies_the_probe():
    probe = ar3._utf8_report("nt", True, "", "utf-8")
    assert probe.ok
    assert "UTF-8 mode on" in probe.detail


def test_a_callers_own_io_encoding_satisfies_the_probe_too():
    """The launchers stand aside for `PYTHONIOENCODING`, so the probe has to
    as well — reporting a fault the caller deliberately configured around
    would send them to change something that is already right."""
    probe = ar3._utf8_report("nt", False, "utf-8", "utf-8")
    assert probe.ok
    assert "PYTHONIOENCODING=utf-8" in probe.detail


def test_an_unknown_stream_encoding_is_named_rather_than_blank():
    probe = ar3._utf8_report("nt", False, "", "")
    assert not probe.ok
    assert "unknown" in probe.detail


def _fake(name, group, ok, core=False):
    return ar3.Check(name, group, lambda: ar3.Probe(ok, "detail"), "do the thing", core=core)


def test_doctor_rows_filter_by_group_and_attach_hints_to_failures():
    checks = (_fake("one", ar3.HARNESS, True), _fake("two", ar3.TOOLING, False))
    results = ar3.doctor_results(checks)
    assert ar3.doctor_rows(results, ar3.HARNESS) == [(True, "one", "detail", None)]
    assert ar3.doctor_rows(results, ar3.TOOLING) == [(False, "two", "detail", "do the thing")]


def test_doctor_failures_reports_core_checks():
    results = ar3.doctor_results((
        _fake("harness", ar3.HARNESS, True),
        _fake("git", ar3.TOOLING, False, core=True),
    ))
    assert ar3.doctor_failures(results) == ["git"]


def test_doctor_failures_requires_at_least_one_harness():
    results = ar3.doctor_results((
        _fake("a", ar3.HARNESS, False),
        _fake("b", ar3.HARNESS, False),
        _fake("git", ar3.TOOLING, True, core=True),
    ))
    assert ar3.doctor_failures(results) == ["at least one agent harness"]


def test_doctor_passes_when_core_and_one_harness_are_green():
    results = ar3.doctor_results((
        _fake("a", ar3.HARNESS, False),
        _fake("b", ar3.HARNESS, True),
        _fake("git", ar3.TOOLING, True, core=True),
    ))
    assert ar3.doctor_failures(results) == []


def test_doctor_exits_nonzero_when_a_core_check_fails(monkeypatch, capsys):
    monkeypatch.setattr(ar3, "CHECKS", (
        _fake("b", ar3.HARNESS, True),
        _fake("git", ar3.TOOLING, False, core=True),
    ))
    assert ar3.cmd_doctor(None) == 1
    out = capsys.readouterr().out
    assert "core prerequisites missing: git" in out
    assert "(try: do the thing)" in out


def test_doctor_exits_zero_when_everything_core_is_green(monkeypatch, capsys):
    monkeypatch.setattr(ar3, "CHECKS", (
        _fake("b", ar3.HARNESS, True),
        _fake("git", ar3.TOOLING, True, core=True),
    ))
    assert ar3.cmd_doctor(None) == 0
    assert "core prerequisites satisfied  (2/2 probes green)" in capsys.readouterr().out


# ---------- probes never hang, never fix ----------

def test_version_probe_misses_when_the_binary_is_absent(monkeypatch):
    monkeypatch.setattr(ar3.shutil, "which", lambda _b: None)
    assert ar3._version_probe("nope")() == ar3.Probe(False, "not on PATH")


def test_version_probe_reports_the_first_output_line(monkeypatch):
    monkeypatch.setattr(ar3.shutil, "which", lambda _b: "/fake/claude")
    monkeypatch.setattr(ar3, "_run", lambda argv, timeout: (0, "\n1.2.3 (Some CLI)\nnoise\n"))
    probe = ar3._version_probe("claude")()
    assert probe.ok is True
    assert probe.detail == "1.2.3 (Some CLI)  (/fake/claude)"


def test_version_probe_treats_a_timeout_as_a_miss(monkeypatch):
    monkeypatch.setattr(ar3.shutil, "which", lambda _b: "/fake/hangs")
    monkeypatch.setattr(ar3, "_run", lambda argv, timeout: (None, ""))
    assert ar3._version_probe("hangs")().ok is False


def test_run_returns_none_when_the_command_times_out(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="slow", timeout=0.2)

    monkeypatch.setattr(ar3.subprocess, "run", timeout)
    assert ar3._run(["slow"], 0.2) == (None, "")


def test_run_returns_none_when_the_binary_cannot_be_executed(monkeypatch):
    def missing(*_args, **_kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(ar3.subprocess, "run", missing)
    assert ar3._run(["gone"], 1.0) == (None, "")


def test_ollama_probe_parses_the_model_table(monkeypatch):
    monkeypatch.setattr(ar3.shutil, "which", lambda _b: "/fake/ollama")
    table = "NAME    ID    SIZE\nsmall:latest  abc  1 GB\nbig:7b  def  4 GB\n"
    monkeypatch.setattr(ar3, "_run", lambda argv, timeout: (0, table))
    probe = ar3._ollama_probe()
    assert probe.ok is True
    assert probe.detail == "2 model(s): small:latest, big:7b"


def test_ollama_probe_reports_an_unreachable_server(monkeypatch):
    monkeypatch.setattr(ar3.shutil, "which", lambda _b: "/fake/ollama")
    monkeypatch.setattr(ar3, "_run", lambda argv, timeout: (1, "connection refused"))
    probe = ar3._ollama_probe()
    assert probe.ok is False
    assert "unreachable" in probe.detail


def test_docker_probe_separates_a_missing_binary_from_a_dead_daemon(monkeypatch):
    monkeypatch.setattr(ar3.shutil, "which", lambda _b: None)
    assert ar3._docker_probe().detail == "not on PATH"
    monkeypatch.setattr(ar3.shutil, "which", lambda _b: "/fake/docker")
    monkeypatch.setattr(ar3, "_run", lambda argv, timeout: (1, "cannot connect"))
    assert "daemon unreachable" in ar3._docker_probe().detail


def test_git_probe_requires_identity_configuration(monkeypatch):
    monkeypatch.setattr(ar3.shutil, "which", lambda _b: "/fake/git")

    def fake_run(argv, timeout):
        if argv[1] == "--version":
            return 0, "git version 2.0.0"
        return 0, "" if argv[-1] == "user.email" else "someone"

    monkeypatch.setattr(ar3, "_run", fake_run)
    probe = ar3._git_probe()
    assert probe.ok is False
    assert probe.detail == "git version 2.0.0, unset: user.email"


# ---------- the spawn-env cross-link (#121) ----------

def _path_check(name, detail):
    return ar3.Check(name, ar3.HARNESS, lambda: ar3.Probe(False, detail), "install it")


def test_doctor_links_an_invisible_harness_to_the_node_spawn_env(monkeypatch, capsys):
    # A harness this shell cannot see is one no node started from this shell
    # can see either, unless the node was given a PATH of its own — otherwise
    # the failure lands hours later at a wake nobody is watching.
    monkeypatch.setattr(ar3, "CHECKS", (
        _path_check("claude", "not on PATH"),
        _path_check("codex", "not on PATH"),
    ))
    monkeypatch.setattr(ar3, "update_note", lambda: "pinned")
    ar3.cmd_doctor(None)
    out = capsys.readouterr().out
    assert "claude, codex not visible from this shell" in out
    assert "a8s start" in out
    assert "wake_path" in out


def test_a_harness_that_answered_badly_is_not_a_path_note(monkeypatch, capsys):
    # Present but broken is a different problem, and saying "PATH" about it
    # would send the operator to the wrong place.
    monkeypatch.setattr(ar3, "CHECKS", (_path_check("claude", "--version exited 1"),))
    monkeypatch.setattr(ar3, "update_note", lambda: "pinned")
    ar3.cmd_doctor(None)
    assert "not visible from this shell" not in capsys.readouterr().out


def test_no_note_when_every_harness_resolves(monkeypatch, capsys):
    monkeypatch.setattr(ar3, "CHECKS", (
        ar3.Check("claude", ar3.HARNESS, lambda: ar3.Probe(True, "1.0 (/usr/bin/claude)"), "h"),
    ))
    monkeypatch.setattr(ar3, "update_note", lambda: "pinned")
    ar3.cmd_doctor(None)
    assert "not visible from this shell" not in capsys.readouterr().out


# ---------- update ----------

def _update_args(**kw):
    import argparse

    base = {"all_engines": False, "engines": None}
    base.update(kw)
    return argparse.Namespace(**base)


def _checkout(tmp_path, monkeypatch, **answers):
    """A tree that looks like a git checkout, with git's answers modeled. This
    suite spawns no processes, so `_git_out` is the seam: what is under test is
    which combination of answers stops an update, not git's own behaviour.

    Keys are the git subcommand; None means git exited non-zero, which is how
    a detached HEAD reports itself.
    """
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    table = {
        "rev-parse": "true",
        "status": "",
        "HEAD": "main",
        "origin": None,
        "describe": None,
    }
    table.update(answers)

    def git(_root, *args):
        if args[0] in ("rev-parse", "status", "describe"):
            return table[args[0]]
        return table["origin" if args[-1].startswith("refs/") else "HEAD"]

    monkeypatch.setattr(ar3, "_git_out", git)
    return root


def test_update_allows_a_tree_that_is_not_a_checkout(tmp_path):
    assert ar3.update_refusal(tmp_path) is None


def test_update_refuses_a_git_dir_git_will_not_vouch_for(tmp_path, monkeypatch):
    # A disowned .git and one git cannot read — missing binary, timeout,
    # dubious ownership — are the same answer here, and one of them is a
    # working checkout. get.sh force-resets the tree, so the ambiguous case
    # has to stop rather than proceed.
    root = _checkout(tmp_path, monkeypatch, **{"rev-parse": None})
    refusal = ar3.update_refusal(root)
    assert refusal is not None
    assert "work tree" in refusal


def test_update_refuses_when_git_will_not_report_status(tmp_path, monkeypatch):
    # Clean reports as "", so None here means git failed to answer at all.
    # Reading that as clean is how work in progress gets overwritten.
    root = _checkout(tmp_path, monkeypatch, status=None)
    refusal = ar3.update_refusal(root)
    assert refusal is not None
    assert "status" in refusal


def test_update_refuses_a_dirty_tree(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, status=" M VERSION")
    refusal = ar3.update_refusal(root)
    assert refusal is not None
    assert "uncommitted changes" in refusal


def test_update_refuses_a_working_branch(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, HEAD="0.1.99")
    refusal = ar3.update_refusal(root)
    assert refusal is not None
    # It must name the branch, or the operator cannot tell which tree it means.
    assert "0.1.99" in refusal


def test_update_allows_the_default_branch(tmp_path, monkeypatch):
    assert ar3.update_refusal(_checkout(tmp_path, monkeypatch)) is None


def test_update_honours_a_remote_default_that_is_not_main(tmp_path, monkeypatch):
    root = _checkout(tmp_path, monkeypatch, HEAD="trunk", origin="origin/trunk")
    assert ar3.update_refusal(root) is None


def test_update_allows_a_detached_head_on_a_release_tag(tmp_path, monkeypatch):
    # What an AR3_VERSION pin leaves behind; get.sh rejoins the branch itself.
    root = _checkout(tmp_path, monkeypatch, HEAD=None, describe="v0.1.75")
    assert ar3.update_refusal(root) is None


def test_update_refuses_a_detached_head_on_an_arbitrary_tag(tmp_path, monkeypatch):
    # `describe --exact-match` answers for any tag, but get.sh only accepts
    # AR3_VERSION as `v[0-9]*`, so only that grammar can be a pin it created.
    # A bookmark named "wip" is a working state wearing a tag.
    root = _checkout(tmp_path, monkeypatch, HEAD=None, describe="wip")
    refusal = ar3.update_refusal(root)
    assert refusal is not None
    assert "wip" in refusal


def test_update_refuses_a_detached_head_that_is_not_a_tag(tmp_path, monkeypatch):
    # A developer parked on an unpushed commit reports no branch, exactly as a
    # version pin does. Only the pin lands on a tag, and get.sh would force
    # this tree back onto the default branch.
    root = _checkout(tmp_path, monkeypatch, HEAD=None, describe=None)
    refusal = ar3.update_refusal(root)
    assert refusal is not None
    assert "AR3_VERSION pin" in refusal


def _installed(tmp_path, version="0.1.0"):
    root = tmp_path / "install"
    root.mkdir()
    (root / "get.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    return root


class _Done:
    def __init__(self, returncode, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def _sh_on_path(monkeypatch, path="/bin/sh"):
    """What every POSIX box and every Git Bash shell look like: `sh` resolves.
    Pinned rather than left to the host, so the suite reads the same on a
    Mac runner and on a Windows checkout."""
    monkeypatch.setattr(ar3.shutil, "which", lambda name: path if name == "sh" else None)


LAUNCHER = ("bin", "sh.exe")
BARE = ("usr", "bin", "sh.exe")


def _git_for_windows(tmp_path, monkeypatch, *shims):
    """A Windows box whose PATH has git and no sh — PowerShell or cmd.exe with
    Git for Windows installed. `git --exec-path` answers three levels under
    the install root; `shims` says which of the tree's two sh.exe copies
    exist there. No shims means git is absent too."""
    root = tmp_path / "Git"
    for rel in shims:
        exe = root.joinpath(*rel)
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("", encoding="utf-8")
    exec_path = str(root / "mingw64" / "libexec" / "git-core") if shims else None
    monkeypatch.setattr(ar3, "IS_WINDOWS", True)
    monkeypatch.setattr(ar3.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        ar3, "_git_out",
        lambda _root, *args: exec_path if args == ("--exec-path",) else None,
    )
    return root


def _recording_run(seen):
    def fake_run(argv, env=None, **kw):
        seen["argv"] = argv
        seen["dir"] = (env or {}).get("AR3_DIR")
        return _Done(0)

    return fake_run


def test_update_runs_the_local_installer_against_this_copy(tmp_path, monkeypatch, capsys):
    root = _installed(tmp_path)
    monkeypatch.setattr(ar3, "REPO_ROOT", root)
    _sh_on_path(monkeypatch)
    seen = {}
    monkeypatch.setattr(ar3.subprocess, "run", _recording_run(seen))
    assert ar3.cmd_update(_update_args()) == 0
    assert seen["argv"] == ["/bin/sh", str(root / "get.sh")]
    # Without this, get.sh updates whatever lives at ~/.ar3 instead of the
    # copy the operator actually invoked.
    assert seen["dir"] == str(root)


def test_update_on_windows_runs_the_installer_through_git_bash_launcher(tmp_path, monkeypatch):
    # PowerShell and cmd.exe carry Git's cmd\ directory, not usr\bin, so a
    # bare `sh` is [WinError 2] there. bin\sh.exe is the launcher that puts
    # /usr/bin on the child's PATH; it wins over the bare interpreter beside it.
    root = _installed(tmp_path)
    monkeypatch.setattr(ar3, "REPO_ROOT", root)
    git = _git_for_windows(tmp_path, monkeypatch, LAUNCHER, BARE)
    seen = {}
    monkeypatch.setattr(ar3.subprocess, "run", _recording_run(seen))
    assert ar3.cmd_update(_update_args()) == 0
    assert seen["argv"] == [str(git / "bin" / "sh.exe"), str(root / "get.sh")]


def test_update_on_windows_falls_back_to_the_bare_interpreter(tmp_path, monkeypatch):
    root = _installed(tmp_path)
    monkeypatch.setattr(ar3, "REPO_ROOT", root)
    git = _git_for_windows(tmp_path, monkeypatch, BARE)
    seen = {}
    monkeypatch.setattr(ar3.subprocess, "run", _recording_run(seen))
    assert ar3.cmd_update(_update_args()) == 0
    assert seen["argv"][0] == str(git / "usr" / "bin" / "sh.exe")


def test_update_on_windows_without_git_says_what_to_install(tmp_path, monkeypatch, capsys):
    root = _installed(tmp_path)
    monkeypatch.setattr(ar3, "REPO_ROOT", root)
    _git_for_windows(tmp_path, monkeypatch)

    def explode(*a, **k):
        raise AssertionError("installer ran with no sh to run it")

    monkeypatch.setattr(ar3.subprocess, "run", explode)
    assert ar3.cmd_update(_update_args()) == 1
    err = capsys.readouterr().err
    assert "Git for Windows" in err
    assert "WinError" not in err


def test_update_on_posix_does_not_read_git_for_a_shell(tmp_path, monkeypatch, capsys):
    # The git-tree walk is a Windows answer to a Windows PATH; a POSIX box
    # with no `sh` at all is not something to paper over from here.
    root = _installed(tmp_path)
    monkeypatch.setattr(ar3, "REPO_ROOT", root)
    monkeypatch.setattr(ar3, "IS_WINDOWS", False)
    monkeypatch.setattr(ar3.shutil, "which", lambda _name: None)

    def no_git(*a):
        raise AssertionError("git consulted for a shell on POSIX")

    monkeypatch.setattr(ar3, "_git_out", no_git)
    assert ar3.cmd_update(_update_args()) == 1
    assert "no sh" in capsys.readouterr().err


def test_update_reports_the_version_it_moved_to(tmp_path, monkeypatch, capsys):
    root = _installed(tmp_path, "0.1.0")
    _sh_on_path(monkeypatch)

    def fake_run(argv, env=None, **kw):
        (root / "VERSION").write_text("0.1.9\n", encoding="utf-8")
        return _Done(0)

    monkeypatch.setattr(ar3, "REPO_ROOT", root)
    monkeypatch.setattr(ar3.subprocess, "run", fake_run)
    assert ar3.cmd_update(_update_args()) == 0
    assert "0.1.0 -> 0.1.9" in capsys.readouterr().out


def test_update_says_so_when_nothing_moved(tmp_path, monkeypatch, capsys):
    root = _installed(tmp_path, "0.1.0")
    monkeypatch.setattr(ar3, "REPO_ROOT", root)
    _sh_on_path(monkeypatch)
    monkeypatch.setattr(ar3.subprocess, "run", lambda *a, **k: _Done(0))
    assert ar3.cmd_update(_update_args()) == 0
    assert "already at 0.1.0" in capsys.readouterr().out


def test_update_propagates_installer_failure(tmp_path, monkeypatch, capsys):
    root = _installed(tmp_path)
    monkeypatch.setattr(ar3, "REPO_ROOT", root)
    _sh_on_path(monkeypatch)
    monkeypatch.setattr(ar3.subprocess, "run", lambda *a, **k: _Done(3))
    assert ar3.cmd_update(_update_args()) == 3
    # No invented success line on top of the installer's own complaint.
    assert "->" not in capsys.readouterr().out


def test_update_without_an_installer_says_where_to_get_one(tmp_path, monkeypatch, capsys):
    root = tmp_path / "install"
    root.mkdir()
    monkeypatch.setattr(ar3, "REPO_ROOT", root)
    assert ar3.cmd_update(_update_args()) == 1
    assert "get.sh" in capsys.readouterr().err


def test_update_refusal_stops_before_the_installer_runs(tmp_path, monkeypatch, capsys):
    repo = _checkout(tmp_path, monkeypatch, HEAD="0.1.99")
    (repo / "get.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(ar3, "REPO_ROOT", repo)

    def explode(*a, **k):
        raise AssertionError("installer ran against a working checkout")

    monkeypatch.setattr(ar3.subprocess, "run", explode)
    assert ar3.cmd_update(_update_args()) == 1
    assert "0.1.99" in capsys.readouterr().err


# ---------- engine updates ----------

def _engine_path(tmp_path, *parts):
    path = tmp_path.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return str(path)


def _which(binary_map):
    def which(name):
        return binary_map.get(name)

    return which


def _pipeline(tmp_path, monkeypatch, nodes=(), binaries=None, fail=(),
              shared=None, interrupt=(), interrupt_drops=(), aliases=None):
    """Stage an install copy with a8s on PATH and record every spawned
    command in order. `nodes` is what `a8s ps` reports; `binaries` maps
    engine names to paths; `fail` is the argv prefix that exits 3.
    `shared` maps a node to siblings riding its handler PID — one stop
    takes them all down; `aliases` is written to the fake registry for
    restart-target matching; `interrupt` is the argv prefix that raises
    KeyboardInterrupt, with `interrupt_drops` naming nodes the
    interrupted command still took down before the signal propagated."""
    root = _installed(tmp_path)
    monkeypatch.setattr(ar3, "REPO_ROOT", root)
    a8s = _engine_path(tmp_path, "bin", "a8s")
    table = dict(binaries or {})
    table["a8s"] = a8s
    table["sh"] = "/bin/sh"
    monkeypatch.setattr(ar3.shutil, "which", _which(table))
    home = tmp_path / "a8s-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("A8S_HOME", str(home))
    (home / "a8s.json").write_text(
        json.dumps(
            {
                "agents": {n: {"root": "/r"} for n in nodes},
                "aliases": aliases or {},
            }
        ),
        encoding="utf-8",
    )
    seen = []
    live = set(nodes)
    shared = dict(shared or {})
    pids = {n: 9000 + i for i, n in enumerate(nodes)}
    for leader, siblings in shared.items():
        for sib in siblings:
            pids[sib] = pids[leader]

    def fake_run(argv, env=None, **kw):
        seen.append(list(argv))
        if any(list(argv[: len(f)]) == list(f) for f in interrupt):
            live.difference_update(interrupt_drops)
            raise KeyboardInterrupt
        if argv[:2] == [a8s, "ps"]:
            rows = "".join(
                f"{n}   {pids[n]}   1m   /r\n" for n in nodes if n in live
            )
            return _Done(0, stdout="NAME   PID   UPTIME   ROOT\n" + rows)
        if argv[:2] == [a8s, "stop"]:
            target = argv[2]
            if target not in live:
                return _Done(1)
            live.discard(target)
            live.difference_update(shared.get(target, ()))
            return _Done(0)
        if argv[:2] == [a8s, "start"]:
            if any(list(argv[: len(f)]) == list(f) for f in fail):
                return _Done(3)
            live.add(argv[2])
            return _Done(0)
        if any(list(argv[: len(f)]) == list(f) for f in fail):
            return _Done(3)
        return _Done(0)

    monkeypatch.setattr(ar3.subprocess, "run", fake_run)
    return root, a8s, seen


def test_install_method_reads_a_brew_cellar_path():
    manager, package, _nm = ar3.install_method("/opt/homebrew/Cellar/ollama/0.34.0/bin/ollama")
    assert (manager, package) == ("brew", "ollama")


def test_install_method_reads_a_brew_cask_path():
    manager, package, _nm = ar3.install_method("/opt/homebrew/Caskroom/copilot-cli/1.0.36/copilot")
    assert (manager, package) == ("brew-cask", "copilot-cli")


def test_install_method_reads_an_npm_global_path():
    manager, package, _nm = ar3.install_method(
        "/opt/homebrew/lib/node_modules/opencode-ai/bin/opencode.exe"
    )
    assert (manager, package) == ("npm", "opencode-ai")


def test_install_method_joins_a_scoped_npm_package():
    manager, package, _nm = ar3.install_method(
        "/opt/homebrew/lib/node_modules/@openai/codex/bin/codex.js"
    )
    assert (manager, package) == ("npm", "@openai/codex")


def test_install_method_says_nothing_for_a_self_installed_binary():
    assert ar3.install_method("/home/u/.local/bin/claude") == (None, None, None)


def _npm_wrapper(tmp_path, stem, package):
    """A prefix laid out the way npm writes a global install on Windows:
    `<prefix>/<stem>.cmd` beside `<prefix>/node_modules/<package>` — the
    wrapper is a script, so no symlink chain reaches the package."""
    wrapper = _engine_path(tmp_path, f"{stem}.cmd")
    pkg = tmp_path / "node_modules" / package
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text(
        '{"name": "%s", "bin": {"%s": "bin/%s.js"}}' % (package, stem, stem),
        encoding="utf-8",
    )
    return wrapper


def test_install_method_reads_an_npm_cmd_wrapper(tmp_path):
    wrapper = _npm_wrapper(tmp_path, "codex", "@openai/codex")
    manager, package, node_modules = ar3.install_method(wrapper)
    assert (manager, package) == ("npm", "@openai/codex")
    assert node_modules == tmp_path / "node_modules"


def test_install_method_ignores_a_wrapper_without_the_bin_entry(tmp_path):
    # A script beside node_modules whose name no package publishes is not
    # an npm global — whatever installed it owns its update.
    wrapper = _engine_path(tmp_path, "mystery.cmd")
    pkg = tmp_path / "node_modules" / "something-else"
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text(
        '{"name": "something-else", "bin": {"something-else": "bin/x.js"}}',
        encoding="utf-8",
    )
    assert ar3.install_method(wrapper) == (None, None, None)


def test_engine_update_refuses_a_missing_binary(monkeypatch):
    monkeypatch.setattr(ar3.shutil, "which", _which({}))
    argv, _env, refusal = ar3.engine_update("claude")
    assert argv is None
    assert "not on PATH" in refusal


def test_engine_update_prefers_the_self_update_verb(tmp_path, monkeypatch):
    binary = _engine_path(tmp_path, ".local", "bin", "claude")
    monkeypatch.setattr(ar3.shutil, "which", _which({"claude": binary}))
    argv, env, refusal = ar3.engine_update("claude")
    assert refusal is None
    assert argv == [binary, "update"]
    assert env == {}


def test_engine_update_muse_uses_the_sync_update_env(tmp_path, monkeypatch):
    # muse has no update verb; the launcher only updates itself on an
    # invocation when MUSE_SYNC_UPDATE=1 forces the check.
    binary = _engine_path(tmp_path, ".local", "bin", "muse")
    monkeypatch.setattr(ar3.shutil, "which", _which({"muse": binary}))
    argv, env, refusal = ar3.engine_update("muse")
    assert refusal is None
    assert argv == [binary, "--version"]
    assert env == {"MUSE_SYNC_UPDATE": "1"}


def test_engine_update_prefers_brew_over_the_self_verb(tmp_path, monkeypatch):
    # A package-managed install is updated by its manager; the self-update
    # verb would lay a second unmanaged copy beside it.
    binary = _engine_path(tmp_path, "Cellar", "opencode", "1.0.0", "bin", "opencode")
    brew = _engine_path(tmp_path, "bin", "brew")
    monkeypatch.setattr(
        ar3.shutil, "which", _which({"opencode": binary, "brew": brew})
    )
    argv, _env, refusal = ar3.engine_update("opencode")
    assert refusal is None
    assert argv == [brew, "upgrade", "opencode"]


def test_engine_update_brew_install_without_brew_on_path(tmp_path, monkeypatch):
    binary = _engine_path(tmp_path, "Cellar", "opencode", "1.0.0", "bin", "opencode")
    monkeypatch.setattr(ar3.shutil, "which", _which({"opencode": binary}))
    argv, _env, refusal = ar3.engine_update("opencode")
    assert argv is None
    assert "brew is not on PATH" in refusal


def test_engine_update_brew_cask(tmp_path, monkeypatch):
    binary = _engine_path(tmp_path, "Caskroom", "copilot-cli", "1.0.36", "copilot")
    brew = _engine_path(tmp_path, "bin", "brew")
    monkeypatch.setattr(
        ar3.shutil, "which", _which({"copilot": binary, "brew": brew})
    )
    argv, _env, refusal = ar3.engine_update("copilot")
    assert refusal is None
    assert argv == [brew, "upgrade", "--cask", "copilot-cli"]


def test_engine_update_npm_global(tmp_path, monkeypatch):
    binary = _engine_path(tmp_path, "lib", "node_modules", "@openai", "codex", "bin", "codex.js")
    npm = _engine_path(tmp_path, "bin", "npm")
    monkeypatch.setattr(ar3.shutil, "which", _which({"codex": binary, "npm": npm}))
    argv, _env, refusal = ar3.engine_update("codex")
    assert refusal is None
    assert argv == [npm, "install", "-g", "--prefix", str(tmp_path), "@openai/codex@latest"]


def test_engine_update_npm_cmd_wrapper_uses_its_own_prefix(tmp_path, monkeypatch):
    # The npm the PATH finds may belong to another global root (a Node
    # version manager's); --prefix pins the detected install so the
    # update refreshes the binary that was selected rather than laying a
    # second copy elsewhere.
    wrapper = _npm_wrapper(tmp_path, "codex", "@openai/codex")
    npm = _engine_path(tmp_path, "bin", "npm")
    monkeypatch.setattr(ar3.shutil, "which", _which({"codex": wrapper, "npm": npm}))
    argv, _env, refusal = ar3.engine_update("codex")
    assert refusal is None
    assert argv == [npm, "install", "-g", "--prefix", str(tmp_path), "@openai/codex@latest"]


def test_engine_update_npm_install_without_npm_on_path(tmp_path, monkeypatch):
    binary = _engine_path(tmp_path, "lib", "node_modules", "opencode-ai", "bin", "opencode")
    monkeypatch.setattr(ar3.shutil, "which", _which({"opencode": binary}))
    argv, _env, refusal = ar3.engine_update("opencode")
    assert argv is None
    assert "npm is not on PATH" in refusal


def test_engine_update_npm_sudo_when_the_root_is_not_writable(tmp_path, monkeypatch):
    binary = _engine_path(tmp_path, "lib", "node_modules", "opencode-ai", "bin", "opencode")
    npm = _engine_path(tmp_path, "bin", "npm")
    sudo = _engine_path(tmp_path, "bin", "sudo")
    monkeypatch.setattr(
        ar3.shutil,
        "which",
        _which({"opencode": binary, "npm": npm, "sudo": sudo}),
    )
    monkeypatch.setattr(ar3.os, "access", lambda *_a, **_k: False)
    argv, _env, refusal = ar3.engine_update("opencode")
    assert refusal is None
    assert argv == [sudo, npm, "install", "-g", "--prefix", str(tmp_path), "opencode-ai@latest"]


def test_engine_update_npm_without_sudo_names_the_problem(tmp_path, monkeypatch):
    # Windows has no sudo; an unwritable global root there is a refusal,
    # not a failed exec.
    binary = _engine_path(tmp_path, "lib", "node_modules", "opencode-ai", "bin", "opencode")
    npm = _engine_path(tmp_path, "bin", "npm")
    monkeypatch.setattr(ar3.shutil, "which", _which({"opencode": binary, "npm": npm}))
    monkeypatch.setattr(ar3.os, "access", lambda *_a, **_k: False)
    argv, _env, refusal = ar3.engine_update("opencode")
    assert argv is None
    assert "cannot write" in refusal


def test_engine_update_no_known_method(tmp_path, monkeypatch):
    # An engine with no self-update verb and no package manager marker —
    # ollama installed by its curl script, say — is a refusal, not a guess.
    binary = _engine_path(tmp_path, ".local", "bin", "ollama")
    monkeypatch.setattr(ar3.shutil, "which", _which({"ollama": binary}))
    argv, _env, refusal = ar3.engine_update("ollama")
    assert argv is None
    assert "no known update method" in refusal


def test_update_engine_runs_the_engine_then_the_suite(tmp_path, monkeypatch):
    binary = _engine_path(tmp_path, ".local", "bin", "claude")
    root, a8s, seen = _pipeline(tmp_path, monkeypatch, binaries={"claude": binary})
    assert ar3.cmd_update(_update_args(engines=["claude"])) == 0
    assert seen == [
        [a8s, "ps"],
        [binary, "update"],
        ["/bin/sh", str(root / "get.sh")],
        [a8s, "ps"],
    ]


def test_update_engine_accepts_engine_ids_for_binary_names(tmp_path, monkeypatch):
    # `cursor` is the engine id; `agent` is the binary doctor probes.
    binary = _engine_path(tmp_path, ".local", "bin", "agent")
    root, a8s, seen = _pipeline(tmp_path, monkeypatch, binaries={"agent": binary})
    assert ar3.cmd_update(_update_args(engines=["cursor"])) == 0
    assert [binary, "update"] in seen


def test_update_engine_ollama_variants_resolve_to_ollama(tmp_path, monkeypatch):
    binary = _engine_path(tmp_path, "Cellar", "ollama", "0.34.0", "bin", "ollama")
    brew = _engine_path(tmp_path, "bin", "brew")
    root, a8s, seen = _pipeline(
        tmp_path, monkeypatch, binaries={"ollama": binary, "brew": brew}
    )
    assert ar3.cmd_update(_update_args(engines=["ollama-codex"])) == 0
    assert [brew, "upgrade", "ollama"] in seen


def test_update_engine_refuses_an_unknown_name(tmp_path, monkeypatch, capsys):
    _pipeline(tmp_path, monkeypatch)
    assert ar3.cmd_update(_update_args(engines=["notanengine"])) == 2
    assert "notanengine" in capsys.readouterr().err


def test_update_engine_and_all_engines_contradict(capsys):
    assert ar3.cmd_update(_update_args(engines=["claude"], all_engines=True)) == 2
    assert "contradict" in capsys.readouterr().err


def test_update_all_engines_skips_missing_binaries(tmp_path, monkeypatch):
    # --all-engines updates what is present; absent engines are not failures.
    binary = _engine_path(tmp_path, ".local", "bin", "claude")
    root, a8s, seen = _pipeline(tmp_path, monkeypatch, binaries={"claude": binary})
    assert ar3.cmd_update(_update_args(all_engines=True)) == 0
    assert [binary, "update"] in seen


def test_update_all_engines_with_none_installed_says_so(tmp_path, monkeypatch, capsys):
    _pipeline(tmp_path, monkeypatch)
    assert ar3.cmd_update(_update_args(all_engines=True)) == 1
    assert "no engines" in capsys.readouterr().err


def test_update_engine_reports_a_failed_update(tmp_path, monkeypatch, capsys):
    binary = _engine_path(tmp_path, ".local", "bin", "claude")
    _pipeline(tmp_path, monkeypatch, binaries={"claude": binary}, fail=[(binary,)])
    assert ar3.cmd_update(_update_args(engines=["claude"])) == 1
    assert "exit 3" in capsys.readouterr().err


def test_update_engine_counts_a_missing_binary_as_a_failure(tmp_path, monkeypatch, capsys):
    # Explicitly asked for and not installed is a failure, unlike the
    # --all-engines skip.
    _pipeline(tmp_path, monkeypatch)
    assert ar3.cmd_update(_update_args(engines=["claude"])) == 1
    assert "not on PATH" in capsys.readouterr().err


def test_update_stops_nodes_before_engines_and_restarts_them(tmp_path, monkeypatch):
    # The agent-machine pipeline in order: nodes down, engines, suite,
    # nodes back — only the ones that were running.
    binary = _engine_path(tmp_path, ".local", "bin", "claude")
    root, a8s, seen = _pipeline(
        tmp_path, monkeypatch, nodes=("N1", "N2"), binaries={"claude": binary}
    )
    assert ar3.cmd_update(_update_args(all_engines=True)) == 0
    assert seen == [
        [a8s, "ps"],
        [a8s, "stop", "N1"],
        [a8s, "ps"],
        [a8s, "stop", "N2"],
        [a8s, "ps"],
        [binary, "update"],
        ["/bin/sh", str(root / "get.sh")],
        [a8s, "ps"],
        [a8s, "start", "N1"],
        [a8s, "start", "N2"],
    ]


def test_update_restarts_nodes_even_when_an_update_fails(tmp_path, monkeypatch):
    # A failed engine update must not leave a machine dark.
    binary = _engine_path(tmp_path, ".local", "bin", "claude")
    root, a8s, seen = _pipeline(
        tmp_path,
        monkeypatch,
        nodes=("N1",),
        binaries={"claude": binary},
        fail=[(binary,)],
    )
    assert ar3.cmd_update(_update_args(all_engines=True)) == 1
    assert [a8s, "start", "N1"] in seen


def test_update_engine_refusal_stops_before_nodes_or_updates(tmp_path, monkeypatch):
    # A working checkout refuses the whole pipeline: no nodes cycled, no
    # engine touched.
    repo = _checkout(tmp_path, monkeypatch, HEAD="0.1.99")
    (repo / "get.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(ar3, "REPO_ROOT", repo)

    def explode(*a, **k):
        raise AssertionError("ran a command against a working checkout")

    monkeypatch.setattr(ar3.subprocess, "run", explode)
    assert ar3.cmd_update(_update_args(all_engines=True)) == 1


def test_update_restarts_siblings_a_shared_handler_took_down(tmp_path, monkeypatch):
    # `a8s stop worker-a` detaches the whole handler process — worker-b
    # rides the same PID and goes down with it, so it must come back too
    # even though its own stop was never invoked.
    binary = _engine_path(tmp_path, ".local", "bin", "claude")
    root, a8s, seen = _pipeline(
        tmp_path,
        monkeypatch,
        nodes=("worker-a", "worker-b"),
        binaries={"claude": binary},
        shared={"worker-a": ("worker-b",)},
    )
    assert ar3.cmd_update(_update_args(all_engines=True)) == 0
    assert [a8s, "stop", "worker-b"] not in seen
    assert [a8s, "start", "worker-a"] in seen
    assert [a8s, "start", "worker-b"] in seen


def test_update_interrupt_during_stop_still_restarts_stopped_nodes(
    tmp_path, monkeypatch
):
    # Ctrl+C while a slow stop waits for a wake: N1 is already down and
    # must come back even though the pipeline never ran.
    a8s_path = str(tmp_path / "bin" / "a8s")
    binary = _engine_path(tmp_path, ".local", "bin", "claude")
    root, a8s, seen = _pipeline(
        tmp_path,
        monkeypatch,
        nodes=("N1", "N2"),
        binaries={"claude": binary},
        interrupt=[(a8s_path, "stop", "N2")],
    )
    with pytest.raises(KeyboardInterrupt):
        ar3.cmd_update(_update_args(all_engines=True))
    assert [a8s, "start", "N1"] in seen
    # N2 never went down in the fake, so it must not be double-started.
    assert [a8s, "start", "N2"] not in seen


def test_update_interrupt_reconciles_a_node_the_stop_took_down(
    tmp_path, monkeypatch
):
    # The interrupted stop did detach N2 before the signal propagated —
    # the restart reconcile pass finds it down and brings it back.
    a8s_path = str(tmp_path / "bin" / "a8s")
    binary = _engine_path(tmp_path, ".local", "bin", "claude")
    root, a8s, seen = _pipeline(
        tmp_path,
        monkeypatch,
        nodes=("N1", "N2"),
        binaries={"claude": binary},
        interrupt=[(a8s_path, "stop", "N2")],
        interrupt_drops=("N2",),
    )
    with pytest.raises(KeyboardInterrupt):
        ar3.cmd_update(_update_args(all_engines=True))
    assert [a8s, "start", "N1"] in seen
    assert [a8s, "start", "N2"] in seen


def test_update_restart_failures_reach_the_exit_status(tmp_path, monkeypatch, capsys):
    # A node that will not start after its stop must not exit 0 — the
    # machine is left darker than the update found it.
    a8s_path = str(tmp_path / "bin" / "a8s")
    binary = _engine_path(tmp_path, ".local", "bin", "claude")
    root, a8s, seen = _pipeline(
        tmp_path,
        monkeypatch,
        nodes=("N1", "N2"),
        binaries={"claude": binary},
        fail=[(a8s_path, "start", "N2")],
    )
    assert ar3.cmd_update(_update_args(all_engines=True)) == 1
    assert "start exited 3" in capsys.readouterr().err
    assert [a8s, "start", "N1"] in seen


def test_update_restarts_a_shared_handler_through_its_alias(tmp_path, monkeypatch):
    # worker-a and worker-b rode one handler launched via the `devs`
    # alias — its node tag is `worker-a,worker-b` and that tag feeds the
    # remote session identity. Singleton starts would reconnect under
    # different identities and strand the group's queued messages, so
    # the restart goes through the alias that owns the group.
    binary = _engine_path(tmp_path, ".local", "bin", "claude")
    root, a8s, seen = _pipeline(
        tmp_path,
        monkeypatch,
        nodes=("worker-a", "worker-b"),
        binaries={"claude": binary},
        shared={"worker-a": ("worker-b",)},
        aliases={"devs": ["worker-a", "worker-b"]},
    )
    assert ar3.cmd_update(_update_args(all_engines=True)) == 0
    assert [a8s, "start", "devs"] in seen
    assert [a8s, "start", "worker-a"] not in seen
    assert [a8s, "start", "worker-b"] not in seen


def test_update_group_without_a_matching_alias_starts_each_member(
    tmp_path, monkeypatch
):
    # A group with no exact alias can't re-form its session identity —
    # per-member starts are the fallback, same as `a8s update`.
    binary = _engine_path(tmp_path, ".local", "bin", "claude")
    root, a8s, seen = _pipeline(
        tmp_path,
        monkeypatch,
        nodes=("worker-a", "worker-b"),
        binaries={"claude": binary},
        shared={"worker-a": ("worker-b",)},
        aliases={"devs": ["worker-a", "worker-c"]},
    )
    assert ar3.cmd_update(_update_args(all_engines=True)) == 0
    assert [a8s, "start", "worker-a"] in seen
    assert [a8s, "start", "worker-b"] in seen
    assert [a8s, "start", "devs"] not in seen


def test_update_restart_targets_expand_nested_aliases():
    # `devs` → `staff` → {worker-a, worker-b}: the expansion mirrors
    # resolve_name, so a nested alias still matches its group.
    agents = {"worker-a", "worker-b", "other"}
    aliases = {"devs": ["staff"], "staff": ["worker-a", "worker-b"]}
    groups = {9000: ["worker-a", "worker-b"], 9001: ["other"]}
    targets = ar3._restart_targets(
        groups, ["worker-a", "worker-b", "other"], agents, aliases
    )
    assert targets == ["other", "devs"]


def test_update_restart_targets_skip_a_cyclic_alias():
    agents = {"worker-a", "worker-b"}
    aliases = {"loop": ["loop"], "devs": ["worker-a", "worker-b"]}
    groups = {9000: ["worker-a", "worker-b"]}
    targets = ar3._restart_targets(
        groups, ["worker-a", "worker-b"], agents, aliases
    )
    assert targets == ["devs"]
