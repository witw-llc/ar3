"""`r4t engine check` — the parse-only probe of a composed argv.

Every engine here is a fake binary on a tmp PATH: the real CLIs are not on a
CI machine, and a check must never reach one anyway.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

import engines
from engines import check as engine_check
from r4t import main as r4t_main


def fake_binary(
    bin_dir: Path,
    name: str,
    *,
    flags: list[str],
    version: str = "9.9.9",
    strict: bool = False,
    calls: Path | None = None,
) -> Path:
    """A stand-in CLI that answers `--version` and prints a help listing of
    `flags`. `strict` makes it reject an unknown long flag the way clap does.
    It records every call when `calls` is given, so a test can prove no probe
    ever asked it to run a turn."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json, os, sys
            args = sys.argv[1:]
            calls_dir = {str(calls) if calls else None!r}
            if calls_dir:
                n = len(os.listdir(calls_dir))
                with open(os.path.join(calls_dir, f"call-{{n:03d}}.json"), "w") as f:
                    json.dump(args, f)
            if "--version" in args:
                print({version!r})
                sys.exit(0)
            known = {flags!r}
            if {strict!r}:
                for a in args:
                    if a.startswith("--") and a != "--help" and a not in known:
                        sys.stderr.write(f"error: unexpected argument '{{a}}' found\\n")
                        sys.exit(2)
            print("Usage: {name} [options]")
            for flag in known:
                print(f"  {{flag}}  does a thing")
            """
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


CLAUDE_FLAGS = [
    "--permission-mode", "--allowedTools", "--exclude-dynamic-system-prompt-sections",
    "--model", "--continue",
]
CODEX_FLAGS = [
    "--sandbox", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
]
OPENCODE_FLAGS = ["--auto", "--dir", "--continue"]


@pytest.fixture
def bin_dir(tmp_path, monkeypatch):
    d = tmp_path / "bin"
    d.mkdir()
    monkeypatch.setenv("PATH", str(d))
    return d


class TestProbeDecoding:
    def test_non_utf8_probe_output_never_crashes_the_check(self, bin_dir):
        """The probe decodes explicitly (utf-8, errors=replace): on Windows
        the locale default is the ANSI code page with strict errors, and a
        CLI's UTF-8 help text crashed `engine check` outright. Found in
        review. A raw 0xE9 byte here proves the replace path on every
        platform."""
        path = bin_dir / "claude"
        path.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            'sys.stdout.buffer.write(b"9.9.9 caf\\xe9\\n")\n'
            f"sys.stdout.buffer.write({' '.join(CLAUDE_FLAGS)!r}.encode())\n"
            'sys.stdout.buffer.write(b"\\n")\n'
        )
        path.chmod(0o755)
        report = engine_check.check_engine("claude")
        assert report.installed is True
        assert report.verdict == engine_check.ACCEPTED
        assert "\ufffd" in (report.version or "")


class TestHelpScan:
    def test_a_composed_argv_the_help_covers_is_accepted(self, bin_dir):
        fake_binary(bin_dir, "claude", flags=CLAUDE_FLAGS)
        report = engine_check.check_engine("claude")
        assert report.verdict == engine_check.ACCEPTED
        assert report.installed is True
        assert report.version == "9.9.9"
        assert report.method == "help scan"

    def test_a_flag_the_help_never_lists_is_rejected_by_name(self, bin_dir, monkeypatch):
        # The silent-failure class: opencode accepts unknown flags without
        # complaint, so its shipped `--dangerously-skip-permissions` ran turns
        # with auto-approval OFF and no error anywhere.
        import rig as rig_module

        fake_binary(bin_dir, "opencode", flags=OPENCODE_FLAGS)
        monkeypatch.setitem(
            rig_module.HARNESS_PRESETS, "opencode",
            {**rig_module.HARNESS_PRESETS["opencode"],
             "invoke": ["opencode", "run", "--dangerously-skip-permissions",
                        "--dir", "{workdir}", "{prompt}"]},
        )
        report = engine_check.check_engine("opencode")
        assert report.verdict == engine_check.REJECTED
        assert "--dangerously-skip-permissions" in report.detail

    def test_flag_matching_respects_word_bounds(self, bin_dir):
        # `--allow-all` must not be satisfied by `--allow-all-tools`.
        fake_binary(bin_dir, "copilot", flags=["--allow-all-tools", "--no-ask-user"])
        assert engine_check.check_engine("copilot").verdict == engine_check.ACCEPTED
        report = engine_check.check_engine("copilot", permissions="bypass")
        assert report.verdict == engine_check.REJECTED
        assert "--allow-all" in report.detail

    def test_the_launchers_scan_only_the_wrapped_clis_half(self, bin_dir, tmp_path):
        # `--model` before the separator is ollama's own; the flags after it
        # are claude's, and those are the half a preset gets wrong.
        calls = tmp_path / "ollama-calls"
        calls.mkdir()
        fake_binary(bin_dir, "ollama", flags=["--model"], calls=calls)
        fake_binary(bin_dir, "claude", flags=CLAUDE_FLAGS)
        report = engine_check.check_engine("ollama-claude")
        assert report.verdict == engine_check.ACCEPTED
        assert report.binary == "ollama"
        # The launcher answered --version and nothing else: no turn was run.
        assert [json.loads(c.read_text()) for c in sorted(calls.iterdir())] == [["--version"]]

    def test_permissions_translation_is_what_gets_checked(self, bin_dir):
        fake_binary(bin_dir, "claude", flags=CLAUDE_FLAGS)
        report = engine_check.check_engine("claude", permissions="bypass")
        assert report.verdict == engine_check.ACCEPTED
        assert "bypassPermissions" in report.argv


class TestParseProbe:
    def test_a_strict_cli_parses_the_argv_itself(self, bin_dir, tmp_path):
        calls = tmp_path / "codex-calls"
        calls.mkdir()
        fake_binary(bin_dir, "codex", flags=CODEX_FLAGS, strict=True, calls=calls)
        report = engine_check.check_engine("codex")
        assert report.verdict == engine_check.ACCEPTED
        assert report.method == "parse probe"
        recorded = [json.loads(c.read_text()) for c in sorted(calls.iterdir())]
        # Version, then the composed argv with --help in place of the prompt.
        assert recorded[0] == ["--version"]
        assert recorded[1][-1] == "--help"
        assert "{prompt}" not in recorded[1]

    def test_a_flag_the_cli_rejects_reports_its_own_error(self, bin_dir, monkeypatch):
        # The loud-failure class: `codex exec --full-auto` stopped parsing on
        # codex-cli 0.147.0 and every codex turn died at argv parse.
        import rig as rig_module

        fake_binary(bin_dir, "codex", flags=CODEX_FLAGS, strict=True)
        monkeypatch.setitem(
            rig_module.HARNESS_PRESETS, "codex",
            {**rig_module.HARNESS_PRESETS["codex"],
             "invoke": ["codex", "exec", "--full-auto", "{prompt}"]},
        )
        report = engine_check.check_engine("codex")
        assert report.verdict == engine_check.REJECTED
        assert "--full-auto" in report.detail

    def test_codex_continuation_drops_the_flag_resume_refuses(self, bin_dir):
        # `codex exec resume` takes no --sandbox; the preset records that, and
        # the probe is what proves it.
        fake_binary(
            bin_dir, "codex",
            flags=CODEX_FLAGS + ["--last", "--include-non-interactive"],
            strict=True,
        )
        report = engine_check.check_engine("codex", continue_conversation=True)
        assert report.verdict == engine_check.ACCEPTED
        assert "--sandbox" not in report.argv


class TestUnverifiable:
    def test_a_missing_binary_is_not_a_failure(self, bin_dir):
        report = engine_check.check_engine("claude")
        assert report.verdict == engine_check.UNVERIFIABLE
        assert report.installed is False
        assert "not on PATH" in report.detail

    def test_a_launcher_without_the_wrapped_cli_is_unverifiable(self, bin_dir):
        fake_binary(bin_dir, "ollama", flags=["--model"])
        report = engine_check.check_engine("ollama-claude")
        assert report.verdict == engine_check.UNVERIFIABLE
        assert "claude is not on PATH" in report.detail


class TestComposition:
    def test_a_mode_below_the_engines_floor_is_rejected_before_any_probe(self, bin_dir):
        fake_binary(bin_dir, "agy", flags=["--dangerously-skip-permissions"])
        report = engine_check.check_engine("agy", permissions="auto")
        assert report.verdict == engine_check.REJECTED
        assert report.method == "composition"
        assert "auto-denies" in report.detail


def engine_cli(*args):
    return r4t_main(["engine", *args])


class TestCheckCli:
    def test_bare_check_covers_every_run_capable_engine(self, bin_dir, capsys):
        code = engine_cli("check")
        out = capsys.readouterr().out
        for engine in engines.run.RUN_ENGINES:
            assert engine in out
        assert "No turn is spent" in out
        assert code == 0  # nothing installed: unverifiable, not failure

    def test_one_engine_can_be_named(self, bin_dir, capsys):
        fake_binary(bin_dir, "claude", flags=CLAUDE_FLAGS)
        assert engine_cli("claude", "check") == 0
        out = capsys.readouterr().out
        assert "claude" in out and "accepted" in out
        assert "codex" not in out

    def test_a_rejected_argv_exits_one(self, bin_dir, capsys, monkeypatch):
        import rig as rig_module

        fake_binary(bin_dir, "codex", flags=CODEX_FLAGS, strict=True)
        monkeypatch.setitem(
            rig_module.HARNESS_PRESETS, "codex",
            {**rig_module.HARNESS_PRESETS["codex"],
             "invoke": ["codex", "exec", "--full-auto", "{prompt}"]},
        )
        assert engine_cli("codex", "check") == 1
        assert "rejected" in capsys.readouterr().out

    def test_json_output_carries_the_argv(self, bin_dir, capsys):
        fake_binary(bin_dir, "claude", flags=CLAUDE_FLAGS)
        assert engine_cli("claude", "check", "--json") == 0
        [payload] = json.loads(capsys.readouterr().out)
        assert payload["engine"] == "claude"
        assert payload["verdict"] == "accepted"
        assert payload["argv"][0] == "claude"

    def test_an_engine_that_cannot_run_cannot_be_checked(self, bin_dir, capsys):
        assert engine_cli("ollama", "check") == 1
        assert "does not support check" in capsys.readouterr().err

    def test_check_is_advertised_as_a_verb(self):
        for name in engines.run.RUN_ENGINES:
            assert engines.capabilities(name) == ["quota", "run", "check"]
