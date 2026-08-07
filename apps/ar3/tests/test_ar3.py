"""ar3 — greeter panel assembly and the doctor probe registry.

Everything here is hermetic: suite state lives in tmp homes and every probe is
stubbed, so no test reads real state or executes a real harness CLI.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

import ar3


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
    assert _row(rows, "rigs")[3] == "r4t init"
    assert _row(rows, "rosters")[3] == "r4t init"


def test_r4t_panel_counts_only_rig_entries_not_governance_knobs(homes):
    (homes["r4t"] / "rigs.json").write_text(json.dumps({
        "_notes": ["ignored"],
        "throttle": {"max_concurrent": 1},
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
    assert {"ollama serve", "docker", "git"} <= names


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
