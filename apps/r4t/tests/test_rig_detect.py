"""`r4t rig detect` — one command between a fresh machine and a working rig.

Both probes are stubbed. What these tests assert is the routing over their
answers: which presets become an offer, which become a rig, which are skipped
and why, and what a second run does to a config the first one wrote. Nothing
here touches an installed CLI or a vendor endpoint, so the suite measures the
decision and never the developer's machine.
"""
from __future__ import annotations

import json

import pytest

import detect
import engines
from engines import check as engine_check
from r4t import main as r4t_main


def report(preset: str, *, installed=True, version="1.0.0", verdict=engine_check.ACCEPTED, detail=""):
    return engine_check.EngineReport(
        engine=preset,
        binary=preset,
        version=version if installed else None,
        installed=installed,
        verdict=verdict,
        detail=detail,
    )


def stub_probes(monkeypatch, installed: dict, fuels: dict | None = None):
    """`installed` maps preset -> EngineReport; anything absent is missing.
    `fuels` maps preset -> fuel payload or a QuotaError to raise."""
    fuels = fuels or {}

    def check_engine(preset, **_kwargs):
        return installed.get(preset) or report(
            preset,
            installed=False,
            verdict=engine_check.UNVERIFIABLE,
            detail=f"{preset} is not on PATH",
        )

    def fuel(preset, model=None):
        answer = fuels.get(preset)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            raise engines.QuotaError(f"{preset} answers no quota verb")
        return answer

    monkeypatch.setattr(engine_check, "check_engine", check_engine)
    monkeypatch.setattr(engines, "fuel", fuel)


def gauged(value: float, plan: str | None = None) -> dict:
    return {
        "fuel": value,
        "state": "gauged",
        "plan": plan,
        "origin": "live",
        "age_seconds": None,
        "note": None,
    }


class TestNothingInstalled:
    def test_exit_1_and_says_what_to_install(self, monkeypatch, tmp_path, capsys):
        stub_probes(monkeypatch, {})
        code = r4t_main(["rig", "detect", "--dir", str(tmp_path)])
        out = capsys.readouterr().out
        assert code == 1
        assert "No agent CLI found on PATH" in out
        assert "claude" in out and "copilot" in out
        assert not (tmp_path / "rigs.json").exists()

    def test_installed_but_rejected_is_not_detected(self, monkeypatch, tmp_path, capsys):
        stub_probes(monkeypatch, {
            "codex": report(
                "codex",
                verdict=engine_check.REJECTED,
                detail="unexpected argument '--full-auto'",
            ),
        })
        code = r4t_main(["rig", "detect", "--dir", str(tmp_path)])
        out = capsys.readouterr().out
        assert code == 1
        # A CLI that is present but will not run the composed argv must not be
        # offered as a rig, and the reader is told which one and why.
        assert "No usable engine" in out
        assert "--full-auto" in out

    def test_add_writes_nothing_when_nothing_is_detected(self, monkeypatch, tmp_path):
        stub_probes(monkeypatch, {})
        assert r4t_main(["rig", "detect", "--add", "--dir", str(tmp_path)]) == 1
        assert not (tmp_path / "rigs.json").exists()


class TestTable:
    def test_two_detected_engines_show_fuel_grade_and_add_line(
        self, monkeypatch, tmp_path, capsys
    ):
        stub_probes(
            monkeypatch,
            {
                "claude": report("claude", version="2.1.0 (Claude Code)"),
                "copilot": report("copilot", version="Copilot CLI 1.0.82"),
            },
            {
                "claude": gauged(0.42, plan="Personal (max)"),
                "copilot": gauged(0.9, plan="business"),
            },
        )
        code = r4t_main(["rig", "detect", "--dir", str(tmp_path)])
        out = capsys.readouterr().out
        assert code == 0
        assert "2.1.0 (Claude Code)" in out
        assert "42%" in out and "90%" in out
        assert "Personal (max)" in out
        # copilot is r4t's one officially supported engine; the grade says so.
        copilot_row = next(l for l in out.splitlines() if l.strip().startswith("copilot"))
        assert "official" in copilot_row
        claude_row = next(l for l in out.splitlines() if l.strip().startswith("claude"))
        assert "run-capable" in claude_row
        assert "r4t rig add claude claude" in out
        assert "not detected:" in out

    def test_quota_failure_costs_the_number_not_the_detection(
        self, monkeypatch, tmp_path, capsys
    ):
        stub_probes(
            monkeypatch,
            {"muse": report("muse")},
            {"muse": engines.QuotaError("muse answers no quota verb")},
        )
        code = r4t_main(["rig", "detect", "--dir", str(tmp_path)])
        out = capsys.readouterr().out
        assert code == 0
        assert "n/a" in out
        assert "answers no quota verb" in out

    def test_json_carries_every_probed_preset(self, monkeypatch, tmp_path, capsys):
        stub_probes(monkeypatch, {"claude": report("claude")}, {"claude": gauged(0.5)})
        code = r4t_main(["rig", "detect", "--json", "--dir", str(tmp_path)])
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["detected"] == 1
        presets = {row["preset"]: row for row in payload["engines"]}
        assert set(presets) == set(detect.RUN_ENGINES)
        assert presets["claude"]["detected"] is True
        assert presets["claude"]["fuel"] == 0.5
        assert presets["codex"]["detected"] is False


class TestAdd:
    def test_creates_one_rig_per_detected_preset(self, monkeypatch, tmp_path, capsys):
        stub_probes(
            monkeypatch,
            {"claude": report("claude"), "codex": report("codex")},
            {"claude": gauged(0.42), "codex": gauged(0.6)},
        )
        code = r4t_main(["rig", "detect", "--add", "--dir", str(tmp_path)])
        out = capsys.readouterr().out
        assert code == 0
        payload = json.loads((tmp_path / "rigs.json").read_text(encoding="utf-8"))
        assert payload["claude"]["preset"] == "claude"
        assert payload["codex"]["preset"] == "codex"
        assert payload["claude"]["invoke"][0] == "claude"
        assert "added" in out

    def test_ollama_is_skipped_with_the_flag_it_wanted(
        self, monkeypatch, tmp_path, capsys
    ):
        stub_probes(
            monkeypatch,
            {"claude": report("claude"), "ollama-claude": report("ollama")},
            {"claude": gauged(0.42), "ollama-claude": gauged(1.0, plan="local")},
        )
        code = r4t_main(["rig", "detect", "--add", "--dir", str(tmp_path)])
        out = capsys.readouterr().out
        assert code == 0
        payload = json.loads((tmp_path / "rigs.json").read_text(encoding="utf-8"))
        assert "claude" in payload
        assert "ollama-claude" not in payload
        skipped = next(l for l in out.splitlines() if "ollama-claude" in l and "skipped" in l)
        assert "--model" in skipped

    def test_rerun_reports_the_existing_rig_and_does_not_replace_it(
        self, monkeypatch, tmp_path, capsys
    ):
        stub_probes(monkeypatch, {"claude": report("claude")}, {"claude": gauged(0.42)})
        assert r4t_main(["rig", "detect", "--add", "--dir", str(tmp_path)]) == 0
        capsys.readouterr()
        # A tuned rig must survive a second detect — the name is the person's
        # now, and a re-run that overwrote it would be a data loss.
        config = tmp_path / "rigs.json"
        payload = json.loads(config.read_text(encoding="utf-8"))
        payload["claude"]["timeout_seconds"] = 42
        config.write_text(json.dumps(payload), encoding="utf-8")

        assert r4t_main(["rig", "detect", "--add", "--dir", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        assert any("exists" in l and "claude" in l for l in out.splitlines())
        after = json.loads(config.read_text(encoding="utf-8"))
        assert after["claude"]["timeout_seconds"] == 42

    def test_without_add_nothing_is_written(self, monkeypatch, tmp_path, capsys):
        stub_probes(monkeypatch, {"claude": report("claude")}, {"claude": gauged(0.42)})
        assert r4t_main(["rig", "detect", "--dir", str(tmp_path)]) == 0
        assert not (tmp_path / "rigs.json").exists()
        assert "r4t rig detect --add" in capsys.readouterr().out

    def test_dir_absent_writes_the_machine_global_config(
        self, monkeypatch, r4t_home, capsys
    ):
        stub_probes(monkeypatch, {"claude": report("claude")}, {"claude": gauged(0.42)})
        assert r4t_main(["rig", "detect", "--add"]) == 0
        # The default target is the config every other r4t command reads; a
        # rigs.json anywhere else would be a rig nothing resolves.
        assert (r4t_home / "rigs.json").is_file()


class TestGrades:
    @pytest.mark.parametrize(
        "preset,grade",
        [
            ("copilot", detect.GRADE_OFFICIAL),
            ("claude", detect.GRADE_RUN_CAPABLE),
            ("ollama-codex", detect.GRADE_NEEDS_MODEL),
        ],
    )
    def test_grade_per_preset(self, preset, grade):
        assert detect.preset_grade(preset) == grade

    def test_every_run_capable_preset_is_graded(self):
        for preset in detect.RUN_ENGINES:
            assert detect.preset_grade(preset) in {
                detect.GRADE_OFFICIAL,
                detect.GRADE_RUN_CAPABLE,
                detect.GRADE_NEEDS_MODEL,
            }
