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
        for preset in ("claude-ollama", "codex-ollama", "copilot-ollama", "opencode-ollama"):
            assert engines.engine_for(preset) == "ollama"

    def test_unknown_is_none(self):
        assert engines.engine_for("emacs") is None

    def test_case_and_whitespace_are_forgiven(self):
        assert engines.engine_for("  Codex ") == "codex"

    def test_every_engine_answers_for_quota(self):
        for name in engines.MODULES:
            assert "quota" in engines.capabilities(name)

    def test_only_the_verified_five_also_answer_for_run(self):
        run_engines = {"claude", "codex", "agy", "copilot", "cursor"}
        for name in engines.MODULES:
            expected = ["quota", "run"] if name in run_engines else ["quota"]
            assert engines.capabilities(name) == expected

    def test_capability_resolves_through_presets(self):
        assert engines.capability("claude-ollama", "quota") is engines.ollama.quota

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
        assert loaded["age"] == "0m"

    def test_missing_snapshot_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            engines, "snapshot_path", lambda engine: tmp_path / "absent.json"
        )
        assert engines.load_snapshot("codex") is None
