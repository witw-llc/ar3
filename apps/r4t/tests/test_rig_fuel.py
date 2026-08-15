"""`r4t rig fuel` — the rig's tank as one number.

An engine has dials; only a rig has a tank, because which dials constrain the
next turn depends on the model the rig pins. These tests drive the CLI end to
end against canned engine payloads, so what is asserted is the selection and
the arithmetic over the bucket shape, never a live endpoint.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import engines
import state
from r4t import main as r4t_main

CLAUDE_PAYLOAD = {
    "origin": "live",
    "plan": "Personal (max)",
    "buckets": [
        {
            "label": "Five Hour Limit",
            "remaining_fraction": 0.9,
            "reset_time": "2026-08-15T18:00:00+00:00",
        },
        {
            "label": "Weekly Limit",
            "remaining_fraction": 0.4,
            "reset_time": "2026-08-20T00:00:00+00:00",
        },
        {
            "label": "Weekly Limit (Fable)",
            "remaining_fraction": 0.1,
            "reset_time": "2026-08-20T00:00:00+00:00",
        },
    ],
    "note": None,
}


@pytest.fixture(autouse=True)
def _snapshots_stay_in_tmp(monkeypatch, tmp_path):
    """`quota` writes every live answer to ~/.config/r4t/quota; a test must
    not touch the developer's real one."""
    monkeypatch.setattr(
        engines, "snapshot_path", lambda engine: tmp_path / f"{engine}.json"
    )


@pytest.fixture
def answers(monkeypatch):
    """Install a canned answer (or a refusal) for one engine component."""

    def install(engine: str = "claude", payload: dict | None = None, error=None):
        def fake() -> dict:
            if error is not None:
                raise engines.QuotaError(error)
            return copy.deepcopy(CLAUDE_PAYLOAD if payload is None else payload)

        monkeypatch.setattr(engines.MODULES[engine], "quota", fake)

    return install


def write_rigs(tmp_path: Path, entry: dict, name: str = "cheap") -> Path:
    path = tmp_path / "rigs.json"
    path.write_text(json.dumps({name: entry}), encoding="utf-8")
    return path


def claude_rig(model: str | None = None, **extra) -> dict:
    invoke = ["claude", "{prompt}"]
    if model:
        invoke = ["claude", "--model", model, "{prompt}"]
    return {"preset": "claude", "invoke": invoke, **extra}


def fuel_cli(config: Path, *extra, name: str = "cheap") -> int:
    return r4t_main(["rig", "fuel", name, "--rig-config", str(config), *extra])


class TestModelSelectsTheBuckets:
    @pytest.fixture(autouse=True)
    def _claude(self, answers):
        answers()

    def test_an_unscoped_model_ignores_another_familys_weekly(self):
        report = engines.fuel("claude", "claude-opus-5")
        assert report["fuel"] == pytest.approx(0.4)
        assert report["binding_label"] == "Weekly Limit"
        assert [b["label"] for b in report["buckets"]] == [
            "Five Hour Limit",
            "Weekly Limit",
        ]

    def test_an_opus_rig_reads_the_opus_weekly_too(self, answers):
        # The flagship shape: an account carrying a model-scoped weekly for
        # the very model the rig pins reads three dials, not two.
        payload = copy.deepcopy(CLAUDE_PAYLOAD)
        payload["buckets"][2] = {
            "label": "Weekly Limit (Opus)",
            "remaining_fraction": 0.15,
            "reset_time": "2026-08-20T00:00:00+00:00",
        }
        answers(payload=payload)
        report = engines.fuel("claude", "claude-opus-5")
        assert [b["label"] for b in report["buckets"]] == [
            "Five Hour Limit",
            "Weekly Limit",
            "Weekly Limit (Opus)",
        ]
        assert report["fuel"] == pytest.approx(0.15)
        assert report["binding_label"] == "Weekly Limit (Opus)"

    def test_a_fable_rig_burns_the_fable_weekly_as_well(self):
        report = engines.fuel("claude", "fable")
        assert report["fuel"] == pytest.approx(0.1)
        assert report["binding_label"] == "Weekly Limit (Fable)"
        assert len(report["buckets"]) == 3

    def test_a_shared_bucket_constrains_every_family_it_names(self, answers):
        # Label text is server-supplied; one weekly may cover two families.
        answers(
            payload={
                "origin": "live",
                "plan": None,
                "buckets": [
                    {
                        "label": "Sonnet & Opus shared weekly",
                        "remaining_fraction": 0.2,
                        "reset_time": None,
                    }
                ],
                "note": None,
            }
        )
        assert engines.fuel("claude", "claude-sonnet-4-5")["fuel"] == pytest.approx(0.2)
        assert engines.fuel("claude", "claude-opus-5")["fuel"] == pytest.approx(0.2)
        assert engines.fuel("claude", "fable")["state"] == "unconstrained"

    def test_an_unpinned_rig_counts_only_the_unscoped_buckets(self):
        report = engines.fuel("claude", None)
        assert report["model"] is None
        assert report["fuel"] == pytest.approx(0.4)
        assert "Weekly Limit (Fable)" not in [b["label"] for b in report["buckets"]]

    def test_the_binding_bucket_is_the_lowest_one_that_applies(self, answers):
        payload = copy.deepcopy(CLAUDE_PAYLOAD)
        payload["buckets"][0]["remaining_fraction"] = 0.05
        answers(payload=payload)
        report = engines.fuel("claude", "sonnet")
        assert report["binding_label"] == "Five Hour Limit"
        assert report["binding_reset"] == "2026-08-15T18:00:00+00:00"

    def test_pooled_engines_select_by_model_family(self, answers):
        answers(
            "agy",
            {
                "origin": "live",
                "plan": "Pro",
                "buckets": [
                    {
                        "label": "Gemini Weekly Limit",
                        "remaining_fraction": 0.3,
                        "reset_time": None,
                    },
                    {
                        "label": "Claude/GPT Five Hour Limit",
                        "remaining_fraction": 0.7,
                        "reset_time": None,
                    },
                ],
                "note": None,
            },
        )
        assert engines.fuel("agy", "gemini-3.1-pro")["fuel"] == pytest.approx(0.3)
        assert engines.fuel("agy", "Claude Sonnet 4.5")["fuel"] == pytest.approx(0.7)
        # agy's every dial is scoped, so an unpinned rig on it matches none —
        # a null the dispatcher must not read as an empty account.
        unpinned = engines.fuel("agy", None)
        assert unpinned["fuel"] is None
        assert unpinned["state"] == "unconstrained"
        assert unpinned["buckets"] == []

    def test_a_local_engine_is_a_full_tank(self):
        report = engines.fuel("ollama", "qwen3:8b")
        assert report["fuel"] == 1.0
        assert report["state"] == "gauged"
        assert report["quota_engine"] == "ollama"

    def test_a_seat_without_fractions_reports_no_number(self, answers):
        answers(
            "copilot",
            {
                "origin": "live",
                "plan": "enterprise",
                "buckets": [
                    {"label": "Chat", "remaining_fraction": None, "reset_time": None}
                ],
                "note": "unlimited seat",
            },
        )
        report = engines.fuel("copilot", "gpt-5")
        assert report["fuel"] is None
        assert report["state"] == "unlimited"
        assert report["binding_label"] is None
        assert report["binding_reset"] is None

    def test_an_ungauged_bucket_never_reads_as_empty(self, answers):
        answers(
            payload={
                "origin": "live",
                "plan": None,
                "buckets": [
                    {"label": "Chat", "remaining_fraction": None, "reset_time": None},
                    {
                        "label": "Weekly Limit",
                        "remaining_fraction": 0.6,
                        "reset_time": None,
                    },
                ],
                "note": None,
            }
        )
        assert engines.fuel("claude", "sonnet")["fuel"] == pytest.approx(0.6)


class TestCli:
    @pytest.fixture(autouse=True)
    def _claude(self, answers):
        answers()

    def test_text_names_the_number_the_engine_and_the_binding_dial(
        self, r4t_home, tmp_path, capsys
    ):
        config = write_rigs(tmp_path, claude_rig(model="opus"))
        assert fuel_cli(config) == 0
        out = capsys.readouterr().out
        assert "cheap — fuel 0.40" in out
        assert "engine: claude (model: opus)" in out
        assert " *Weekly Limit: 40% remaining" in out
        assert "Fable" not in out

    def test_an_unpinned_rig_says_so(self, r4t_home, tmp_path, capsys):
        config = write_rigs(tmp_path, claude_rig())
        assert fuel_cli(config) == 0
        assert "model: the preset's default" in capsys.readouterr().out

    def test_json_carries_the_rig_the_number_and_what_was_counted(
        self, r4t_home, tmp_path, capsys
    ):
        config = write_rigs(tmp_path, claude_rig(model="fable"))
        assert fuel_cli(config, "--json") == 0
        report = json.loads(capsys.readouterr().out)
        assert list(report) == [
            "rig",
            "preset",
            "quota_engine",
            "model",
            "fuel",
            "state",
            "binding_label",
            "binding_reset",
            "origin",
            "age_seconds",
            "plan",
            "buckets",
            "note",
        ]
        assert report["rig"] == "cheap"
        assert report["preset"] == "claude"
        assert report["quota_engine"] == "claude"
        assert report["model"] == "fable"
        assert report["fuel"] == pytest.approx(0.1)
        assert report["state"] == "gauged"
        assert report["binding_label"] == "Weekly Limit (Fable)"
        assert report["binding_reset"] == "2026-08-20T00:00:00+00:00"
        assert report["age_seconds"] is None
        assert len(report["buckets"]) == 3

    def test_json_preset_matches_what_rig_run_calls_engine(
        self, r4t_home, tmp_path, capsys
    ):
        # `rig run --json` reports the preset id under `engine`; fuel names
        # the same value `preset` and keeps `quota_engine` for the component
        # that actually answered — one ollama-* launcher proves they differ.
        config = write_rigs(
            tmp_path,
            {"preset": "ollama-claude", "invoke": ["ollama", "launch", "{prompt}"]},
        )
        assert fuel_cli(config, "--json") == 0
        report = json.loads(capsys.readouterr().out)
        assert report["preset"] == "ollama-claude"
        assert report["quota_engine"] == "ollama"

    def test_an_aged_answer_says_where_it_came_from(
        self, r4t_home, tmp_path, capsys, answers
    ):
        engines.save_snapshot("claude", CLAUDE_PAYLOAD)
        answers(error="usage endpoint unreachable")
        config = write_rigs(tmp_path, claude_rig(model="opus"))
        assert fuel_cli(config) == 0
        out = capsys.readouterr().out
        assert "fuel 0.40" in out
        assert "source: snapshot from 0m ago" in out
        assert "live check failed" in out

    def test_the_aged_json_carries_seconds_not_a_human_string(
        self, r4t_home, tmp_path, capsys, answers
    ):
        engines.save_snapshot("claude", CLAUDE_PAYLOAD)
        answers(error="usage endpoint unreachable")
        config = write_rigs(tmp_path, claude_rig(model="opus"))
        assert fuel_cli(config, "--json") == 0
        report = json.loads(capsys.readouterr().out)
        assert report["origin"] == "snapshot"
        assert report["quota_engine"] == "claude"
        assert isinstance(report["age_seconds"], float)
        assert "age" not in report

    def test_duplicate_labels_get_exactly_one_star(
        self, r4t_home, tmp_path, capsys, answers
    ):
        answers(
            payload={
                "origin": "live",
                "plan": None,
                "buckets": [
                    {"label": "Weekly Limit", "remaining_fraction": 0.8,
                     "reset_time": None},
                    {"label": "Weekly Limit", "remaining_fraction": 0.2,
                     "reset_time": None},
                ],
                "note": None,
            }
        )
        config = write_rigs(tmp_path, claude_rig(model="opus"))
        assert fuel_cli(config) == 0
        out = capsys.readouterr().out
        assert out.count("*Weekly Limit") == 1
        assert " *Weekly Limit: 20% remaining" in out

    def test_an_unconstrained_rig_says_so_instead_of_a_number(
        self, r4t_home, tmp_path, capsys, answers
    ):
        answers(
            "agy",
            {
                "origin": "live",
                "plan": "Pro",
                "buckets": [
                    {"label": "Gemini Weekly Limit", "remaining_fraction": 0.3,
                     "reset_time": None}
                ],
                "note": None,
            },
        )
        config = write_rigs(tmp_path, {"preset": "agy", "invoke": ["agy", "{prompt}"]})
        assert fuel_cli(config) == 0
        out = capsys.readouterr().out
        assert "fuel unknown (unconstrained)" in out
        assert "no bucket constrains this model" in out

    def test_reading_the_gauge_never_charges_the_rigs_budget(
        self, r4t_home, tmp_path, capsys
    ):
        config = write_rigs(
            tmp_path,
            claude_rig(model="opus", rig_budget_max=4, rig_budget_earn_per_hour=20),
        )
        assert fuel_cli(config) == 0
        assert state.rig_budget_level("cheap", 4.0, 0.0) == pytest.approx(4.0)

    def test_an_engine_that_cannot_answer_exits_one_with_its_remedy(
        self, r4t_home, tmp_path, capsys, answers
    ):
        answers(error="is this machine logged in?")
        config = write_rigs(tmp_path, claude_rig(model="opus"))
        assert fuel_cli(config) == 1
        assert "is this machine logged in?" in capsys.readouterr().err

    def test_unknown_rig_names_presets_and_add(self, r4t_home, tmp_path, capsys):
        config = write_rigs(tmp_path, claude_rig())
        assert fuel_cli(config, name="nope") == 1
        err = capsys.readouterr().err
        assert "r4t rig add nope" in err

    def test_rig_with_no_preset_points_at_swap(self, r4t_home, tmp_path, capsys):
        config = write_rigs(tmp_path, {"invoke": ["something", "{prompt}"]})
        assert fuel_cli(config) == 1
        err = capsys.readouterr().err
        assert "has no preset" in err
        assert "r4t rig swap cheap" in err

    def test_a_rig_with_no_run_verb_still_has_a_tank(
        self, r4t_home, tmp_path, capsys
    ):
        config = write_rigs(
            tmp_path, {"preset": "ollama", "invoke": ["ollama", "run", "{prompt}"]}
        )
        assert fuel_cli(config) == 0
        assert "fuel 1.00" in capsys.readouterr().out
