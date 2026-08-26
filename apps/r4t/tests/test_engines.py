from __future__ import annotations

import pytest

import engines
from engines import agy, claude, codex, copilot, cursor
from engines.base import QuotaError, window_label


class TestResolution:
    def test_engine_ids_resolve_to_themselves(self):
        for name in engines.MODULES:
            assert engines.engine_for(name) == name

    def test_ollama_launcher_presets_resolve_to_ollama(self):
        for preset in ("ollama-claude", "ollama-codex", "ollama-copilot", "ollama-opencode"):
            assert engines.engine_for(preset) == "ollama"

    def test_unknown_is_none(self):
        assert engines.engine_for("emacs") is None

    def test_case_and_whitespace_are_forgiven(self):
        assert engines.engine_for("  Codex ") == "codex"

    def test_every_engine_answers_for_quota(self):
        for name in engines.MODULES:
            assert "quota" in engines.capabilities(name)

    def test_only_the_verified_engines_also_answer_for_run(self):
        run_engines = {"claude", "codex", "agy", "copilot", "cursor", "opencode"}
        for name in engines.MODULES:
            expected = ["quota", "run", "check"] if name in run_engines else ["quota"]
            assert engines.capabilities(name) == expected

    def test_capability_resolves_through_presets(self):
        assert engines.capability("ollama-claude", "quota") is engines.ollama.quota

    def test_unknown_verb_or_engine_is_none(self):
        assert engines.capability("codex", "prompt") is None
        assert engines.capability("emacs", "quota") is None


class TestWindowLabel:
    def test_known_durations(self):
        assert window_label(300) == "Five Hour Limit"
        assert window_label(10080) == "Weekly Limit"
        assert window_label(43200) == "Monthly Limit"

    def test_odd_duration_keeps_its_minutes(self):
        assert window_label(1440) == "1440 Min Limit"

    def test_absent_duration(self):
        assert window_label(None) == "Quota"


class TestCodexParse:
    def test_windows_become_buckets(self):
        result = codex.parse_rate_limits(
            {
                "rateLimits": {
                    "planType": "team",
                    "primary": {
                        "usedPercent": 76,
                        "windowDurationMins": 10080,
                        "resetsAt": 1786560934,
                    },
                    "secondary": {
                        "usedPercent": 10,
                        "windowDurationMins": 300,
                        "resetsAt": 1786400000,
                    },
                }
            }
        )
        assert result["plan"] == "team"
        weekly, five_hour = result["buckets"]
        assert weekly["label"] == "Weekly Limit"
        assert weekly["remaining_fraction"] == pytest.approx(0.24)
        assert weekly["reset_time"].startswith("2026-08-12T")
        assert five_hour["label"] == "Five Hour Limit"
        assert five_hour["remaining_fraction"] == pytest.approx(0.90)

    def test_absent_secondary_is_skipped(self):
        result = codex.parse_rate_limits(
            {
                "rateLimits": {
                    "primary": {"usedPercent": 0, "windowDurationMins": 10080},
                    "secondary": None,
                }
            }
        )
        assert len(result["buckets"]) == 1
        assert result["buckets"][0]["reset_time"] is None

    def test_no_windows_raises(self):
        with pytest.raises(QuotaError):
            codex.parse_rate_limits({"rateLimits": {}})


class TestCopilotParse:
    def test_unlimited_seat_reports_credits_not_fractions(self):
        result = copilot.parse_user(
            {
                "copilot_plan": "business",
                "quota_reset_date_utc": "2026-09-01T00:00:00.000Z",
                "quota_snapshots": {
                    "premium_interactions": {
                        "unlimited": True,
                        "percent_remaining": 100.0,
                        "credits_used": 625,
                    }
                },
            }
        )
        bucket = result["buckets"][0]
        assert bucket["label"] == "Premium Requests"
        assert bucket["remaining_fraction"] is None
        assert bucket["reset_time"] == "2026-09-01T00:00:00.000Z"
        assert "625" in result["note"]

    def test_legacy_seat_reports_fractions(self):
        result = copilot.parse_user(
            {
                "copilot_plan": "individual",
                "quota_reset_date": "2026-09-01",
                "quota_snapshots": {
                    "chat": {"unlimited": False, "percent_remaining": 40.0}
                },
            }
        )
        assert result["buckets"][0]["remaining_fraction"] == pytest.approx(0.40)
        assert result["note"] is None

    def test_no_snapshots_raises(self):
        with pytest.raises(QuotaError):
            copilot.parse_user({"copilot_plan": "business"})


class TestClaudeParse:
    def test_limits_array_is_the_product_surface(self):
        # Shape captured live 2026-08-08: model-scoped weeklies ride the
        # `limits` array, not the top-level utilization windows.
        result = claude.parse_usage(
            {
                "five_hour": {"utilization": 0.0, "resets_at": None},
                "seven_day": {"utilization": 60.0, "resets_at": "2026-08-09T23:59:59+00:00"},
                "limits": [
                    {"kind": "session", "group": "session", "percent": 0,
                     "severity": "normal", "resets_at": None},
                    {"kind": "weekly_all", "group": "weekly", "percent": 60,
                     "severity": "normal", "resets_at": "2026-08-09T23:59:59+00:00"},
                    {"kind": "weekly_scoped", "group": "weekly", "percent": 100,
                     "severity": "critical",
                     "resets_at": "2026-08-09T23:59:59+00:00",
                     "scope": {"model": {"id": None, "display_name": "Fable"}}},
                ],
            }
        )
        labels = {b["label"]: b for b in result["buckets"]}
        assert labels["Five Hour Limit"]["remaining_fraction"] == pytest.approx(1.0)
        assert labels["Weekly Limit"]["remaining_fraction"] == pytest.approx(0.40)
        fable = labels["Weekly Limit (Fable)"]
        assert fable["remaining_fraction"] == 0.0
        assert fable["severity"] == "critical"

    def test_utilization_windows_are_the_fallback(self):
        result = claude.parse_usage(
            {
                "five_hour": {"utilization": 33.0, "resets_at": "2026-08-08T20:00:00+00:00"},
                "seven_day": {"utilization": 13.0, "resets_at": "2026-08-13T07:00:00+00:00"},
                "seven_day_opus": None,
            }
        )
        labels = {b["label"]: b for b in result["buckets"]}
        assert labels["Five Hour Limit"]["remaining_fraction"] == pytest.approx(0.67)
        assert labels["Weekly Limit"]["remaining_fraction"] == pytest.approx(0.87)
        assert "Weekly Limit (Opus)" not in labels

    def test_empty_payload_raises(self):
        with pytest.raises(QuotaError):
            claude.parse_usage({})


class TestCursorParse:
    # Shape captured live 2026-08-08 from GetCurrentPeriodUsage.
    LIVE = {
        "billingCycleStart": "1785544578000",
        "billingCycleEnd": "1788222978000",
        "planUsage": {
            "totalSpend": 16251,
            "includedSpend": 7000,
            "bonusSpend": 9251,
            "limit": 7000,
            "autoPercentUsed": 14.21625,
            "apiPercentUsed": 44.345454545454544,
            "totalPercentUsed": 17.858241758241757,
        },
    }

    def test_live_shape_parses(self):
        result = cursor.parse_period_usage(self.LIVE, "pro_plus")
        assert result["plan"] == "pro_plus"
        labels = {b["label"]: b for b in result["buckets"]}
        assert labels["Included Total"]["remaining_fraction"] == pytest.approx(0.8214, abs=1e-3)
        assert labels["Included API"]["remaining_fraction"] == pytest.approx(0.5565, abs=1e-3)
        assert labels["Included Total"]["reset_time"].startswith("2026-09-01T")
        assert "bonus spend" in result["note"]

    def test_ms_epoch_string_cycle_end(self):
        assert cursor._cycle_end({"billingCycleEnd": "1788222978000"}).startswith("2026-09-01T")
        assert cursor._cycle_end({}) is None

    def test_overdrawn_bucket_floors_at_zero(self):
        payload = {"planUsage": {"totalPercentUsed": 130.0}}
        result = cursor.parse_period_usage(payload, None)
        assert result["buckets"][0]["remaining_fraction"] == 0.0

    def test_no_percent_fields_raises(self):
        with pytest.raises(QuotaError):
            cursor.parse_period_usage({"planUsage": {}}, None)


class TestCursorStateDb:
    """The token lives in the IDE's state database, and the IDE puts it in a
    different place on every platform — a macOS-only path made the engine
    unusable anywhere else."""

    def test_each_platform_looks_under_its_own_app_data_root(self, monkeypatch):
        monkeypatch.delenv(cursor.STATE_DB_ENV, raising=False)
        seen = {}
        for platform in ("darwin", "win32", "linux"):
            monkeypatch.setattr(cursor.sys, "platform", platform)
            seen[platform] = cursor.state_db_candidates()[0]
        assert "Application Support" in str(seen["darwin"])
        assert "Roaming" in str(seen["win32"])
        assert ".config" in str(seen["linux"])
        assert len({str(p) for p in seen.values()}) == 3

    def test_every_platform_ends_at_the_same_database(self, monkeypatch):
        monkeypatch.delenv(cursor.STATE_DB_ENV, raising=False)
        for platform in ("darwin", "win32", "linux"):
            monkeypatch.setattr(cursor.sys, "platform", platform)
            assert cursor.state_db_candidates()[0].name == "state.vscdb"

    def test_env_override_wins_and_is_the_only_candidate(self, monkeypatch, tmp_path):
        named = tmp_path / "windows-side.vscdb"
        monkeypatch.setenv(cursor.STATE_DB_ENV, str(named))
        monkeypatch.setattr(cursor.sys, "platform", "linux")
        assert cursor.state_db_candidates() == [named]

    def test_state_db_is_none_when_nothing_is_on_disk(self, monkeypatch, tmp_path):
        monkeypatch.setenv(cursor.STATE_DB_ENV, str(tmp_path / "absent.vscdb"))
        assert cursor.state_db() is None

    def test_missing_database_names_where_it_looked_and_the_override(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv(cursor.STATE_DB_ENV, str(tmp_path / "absent.vscdb"))
        with pytest.raises(QuotaError) as caught:
            cursor._access_token()
        message = str(caught.value)
        assert "absent.vscdb" in message
        assert cursor.STATE_DB_ENV in message
        assert "cursor-agent" in message


class _DeadCodex:
    """A codex that complains on stderr and never answers. `broken` closes the
    pipe under the first write, which is what a CLI that rejects its own argv
    does; otherwise the writes land and stdout just ends."""

    def __init__(self, *, broken: bool):
        self.stdin = self
        self.stdout = iter(())
        self._broken = broken

    def write(self, text: str) -> None:
        if self._broken:
            raise BrokenPipeError(32, "Broken pipe")

    def flush(self) -> None:
        pass

    def kill(self) -> None:
        pass


def _fake_codex(monkeypatch, complaint: str, *, broken: bool) -> None:
    def popen(_argv, **kwargs):
        kwargs["stderr"].write(complaint)
        kwargs["stderr"].flush()
        return _DeadCodex(broken=broken)

    monkeypatch.setattr(codex.subprocess, "Popen", popen)


class TestCodexProbeArgv:
    """`untrusted` was a valid approval policy until it was not, and the CLI
    rejects an unknown one outright — with stderr discarded that read as a
    login timeout."""

    def test_approval_policy_is_one_every_codex_accepts(self):
        assert codex.APPROVAL_POLICY in ("on-request", "never")

    def test_last_line_is_the_complaint_not_the_warnings(self):
        assert codex._last_line("WARN: no bubblewrap\nerror: invalid value") == (
            "error: invalid value"
        )
        assert codex._last_line("  \n\n") == ""

    def test_a_broken_pipe_carries_the_complaint_too(self, monkeypatch):
        # A CLI that refuses its own argv can be gone before the first write
        # lands. That path used to raise before the stderr file was read, so it
        # discarded exactly the text this fix exists to surface.
        _fake_codex(
            monkeypatch, "WARN: no bubblewrap\nerror: unexpected argument\n", broken=True
        )

        with pytest.raises(QuotaError) as caught:
            codex._rpc_rate_limits()

        message = str(caught.value)
        assert "pipe broke" in message
        assert "error: unexpected argument" in message

    def test_the_no_answer_path_still_carries_it(self, monkeypatch):
        _fake_codex(monkeypatch, "error: invalid value 'untrusted'\n", broken=False)

        with pytest.raises(QuotaError) as caught:
            codex._rpc_rate_limits()

        message = str(caught.value)
        assert "error: invalid value 'untrusted'" in message
        assert "codex login status" in message


class TestAgyParse:
    def test_models_group_into_pools(self):
        result = agy.parse_user_status(
            {
                "userStatus": {
                    "planStatus": {"planInfo": {"planDisplayName": "Pro"}},
                    "cascadeModelConfigData": {
                        "clientModelConfigs": [
                            {
                                "label": "Gemini 3.1 Pro",
                                "quotaInfo": {
                                    "remainingFraction": 0.98,
                                    "resetTime": "2026-08-08T20:45:46Z",
                                },
                            },
                            {
                                "label": "Claude Sonnet 4.5",
                                "quotaInfo": {"remainingFraction": 1.0},
                            },
                            {
                                "label": "Claude Opus 5",
                                "quotaInfo": {"remainingFraction": 0.5},
                            },
                        ]
                    },
                }
            }
        )
        assert result["plan"] == "Pro"
        labels = {b["label"]: b for b in result["buckets"]}
        assert labels["Gemini Weekly Limit"]["remaining_fraction"] == pytest.approx(0.98)
        # The pool reports its most-exhausted model.
        assert labels["Claude/GPT Five Hour Limit"]["remaining_fraction"] == pytest.approx(0.5)

    def test_no_quota_models_raises(self):
        with pytest.raises(QuotaError):
            agy.parse_user_status({"userStatus": {}})


class TestSnapshotRoundTrip:
    def test_save_then_load_reports_age(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            engines, "snapshot_path", lambda engine: tmp_path / f"{engine}.json"
        )
        engines.save_snapshot("codex", {"engine": "codex", "origin": "live", "buckets": []})
        loaded = engines.load_snapshot("codex")
        assert loaded["origin"] == "snapshot"
        assert "age" not in loaded
        assert isinstance(loaded["age_seconds"], float)
        assert loaded["age_seconds"] < 5

    def test_the_human_age_string_belongs_to_the_renderer(self):
        assert engines.format_age(0) == "0m"
        assert engines.format_age(125) == "2m"
        assert engines.format_age(7500) == "2h 5m"
        assert engines.format_age(200000) == "2d 7h"
        assert engines.format_age(None) == "?"

    def test_missing_snapshot_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            engines, "snapshot_path", lambda engine: tmp_path / "absent.json"
        )
        assert engines.load_snapshot("codex") is None
