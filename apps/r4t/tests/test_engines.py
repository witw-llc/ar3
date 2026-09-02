from __future__ import annotations

import sqlite3

import pytest

import engines
from engines import agy, claude, codex, copilot, cursor
from engines.base import QuotaError, window_label


# Engines whose CLI exposes no way to read remaining subscription without
# spending a turn, so their module implements no quota verb.
QUOTALESS = {"muse"}


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

    def test_an_engine_answers_quota_only_when_its_module_implements_one(self):
        # muse is the first engine with no usage surface to read at all —
        # engines/muse.py says why it therefore defines no quota(). Naming it
        # here keeps this a guard: an engine that loses its quota check by
        # accident still fails, and adding muse to QUOTALESS is the deliberate
        # act that records a second one.
        for name in engines.MODULES:
            answers = "quota" in engines.capabilities(name)
            assert answers is (name not in QUOTALESS)

    def test_only_the_verified_engines_also_answer_for_run(self):
        run_engines = {
            "claude", "codex", "agy", "copilot", "cursor", "opencode", "muse",
        }
        for name in engines.MODULES:
            expected = [] if name in QUOTALESS else ["quota"]
            if name in run_engines:
                expected += ["run", "check"]
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

    def test_wsl_windows_profiles_are_candidates_on_linux(self, monkeypatch, tmp_path):
        # Measured working from a WSL seat: the CLI runs on Linux while the IDE
        # that holds the token is installed Windows-side.
        monkeypatch.delenv(cursor.STATE_DB_ENV, raising=False)
        monkeypatch.setattr(cursor.sys, "platform", "linux")
        users = tmp_path / "Users"
        (users / "ana").mkdir(parents=True)
        (users / "bo").mkdir(parents=True)
        monkeypatch.setattr(cursor, "_WSL_USERS_DIR", users)
        found = [str(p) for p in cursor.state_db_candidates()]
        assert any("ana/AppData/Roaming/Cursor" in p for p in found)
        assert any("bo/AppData/Roaming/Cursor" in p for p in found)
        # The Linux path stays first — a locally installed IDE outranks a guess.
        assert ".config" in found[0]

    def test_wsl_candidates_absent_off_linux_and_without_mnt_c(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.delenv(cursor.STATE_DB_ENV, raising=False)
        monkeypatch.setattr(cursor, "_WSL_USERS_DIR", tmp_path / "nope")
        monkeypatch.setattr(cursor.sys, "platform", "linux")
        assert cursor._wsl_candidates() == []
        monkeypatch.setattr(cursor.sys, "platform", "darwin")
        assert cursor._wsl_candidates() == []

    def test_a_database_with_a_token_outranks_one_without(self, monkeypatch, tmp_path):
        # Several Windows profiles can each hold a Cursor install; only one was
        # ever signed in, and merely existing is not what makes a database useful.
        empty, real = tmp_path / "empty.vscdb", tmp_path / "real.vscdb"
        for path, token in ((empty, None), (real, "tok-123")):
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
            if token:
                conn.execute(
                    "INSERT INTO ItemTable VALUES (?, ?)",
                    (cursor.ACCESS_TOKEN_KEY, f'"{token}"'),
                )
            conn.commit()
            conn.close()
        monkeypatch.delenv(cursor.STATE_DB_ENV, raising=False)
        monkeypatch.setattr(cursor, "state_db_candidates", lambda: [empty, real])
        assert cursor.state_db() == real
        assert cursor._state_value(cursor.ACCESS_TOKEN_KEY) == "tok-123"

    def test_a_tokenless_database_is_still_named_when_it_is_all_there_is(
        self, monkeypatch, tmp_path
    ):
        empty = tmp_path / "empty.vscdb"
        conn = sqlite3.connect(empty)
        conn.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
        conn.commit()
        conn.close()
        monkeypatch.setattr(cursor, "state_db_candidates", lambda: [empty])
        # Falls back so the error can say "this one has no token" rather than
        # "there is no database", which are different problems.
        assert cursor.state_db() == empty
        with pytest.raises(QuotaError) as caught:
            cursor._access_token()
        assert "no access token" in str(caught.value)

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


class TestCodexAuthHint:
    """A CLI signed in with an API key is signed in — it just has no
    subscription, and so no rate limit to report. The server calls that
    "authentication required", which reads as a broken login."""

    def _answer(self, message):
        return {"id": 2, "error": {"message": message}}

    def test_authentication_error_says_which_sign_in_carries_quota(self, monkeypatch):
        monkeypatch.setattr(
            codex,
            "_rpc_rate_limits",
            lambda: (_ for _ in ()).throw(
                QuotaError(
                    "account/rateLimits/read: chatgpt authentication required "
                    "to read rate limits (an API-key sign-in has no subscription "
                    "quota to report; `codex login status` names the current one, "
                    "and a ChatGPT account login is what carries rate limits)"
                )
            ),
        )
        monkeypatch.setattr(codex.shutil, "which", lambda _n: "/usr/bin/codex")
        with pytest.raises(QuotaError) as caught:
            codex.quota()
        message = str(caught.value)
        assert "API-key" in message
        assert "codex login status" in message

    def test_an_unrelated_error_gets_no_hint(self):
        # The hint is for the one failure it explains, not decoration on every
        # error the server can return.
        assert "API-key" not in codex._error_hint("rate limit window missing")
        assert "API-key" in codex._error_hint("chatgpt Authentication required")

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


class TestStaleSnapshotIsNotAnAnswer:
    """#218. A failed live check used to be demoted to a `note:` printed under
    a plausible set of numbers, with exit 0. One engine failed on every
    invocation for eleven days behind a reading that said
    `0% remaining · resets 2026-08-18` — served a week after that reset."""

    def _stub(self, monkeypatch, tmp_path, saved_at, reset_time=None):
        import json as _json
        import time as _time

        bucket = {"label": "Weekly", "remaining_fraction": 0.0}
        if reset_time is not None:
            bucket["reset_time"] = reset_time
        path = tmp_path / "codex.json"
        path.write_text(
            _json.dumps({
                "saved_at": saved_at,
                "payload": {"engine": "codex", "origin": "live",
                            "buckets": [bucket]},
            }),
            encoding="utf-8",
        )
        monkeypatch.setattr(engines, "snapshot_path", lambda engine: path)

        class Boom:
            @staticmethod
            def quota():
                raise engines.QuotaError("no state database")

        monkeypatch.setitem(engines.MODULES, "codex", Boom)
        return _time

    def test_a_snapshot_inside_the_bound_still_answers(self, tmp_path, monkeypatch):
        t = self._stub(monkeypatch, tmp_path, __import__("time").time() - 60)
        payload = engines.quota("codex")
        assert payload["origin"] == "snapshot"
        assert "live check failed" in payload["note"]
        assert t  # a fresh fallback is the behaviour worth keeping

    def test_a_snapshot_past_the_bound_refuses_rather_than_misinform(
        self, tmp_path, monkeypatch
    ):
        import time as _time

        self._stub(
            monkeypatch, tmp_path,
            _time.time() - (engines.SNAPSHOT_MAX_AGE_SECONDS + 3600),
        )
        with pytest.raises(engines.QuotaError) as exc:
            engines.quota("codex")
        # The live failure must survive, not be replaced by the age complaint.
        assert "no state database" in str(exc.value)
        assert "may already have been reset" in str(exc.value)

    def test_the_age_bound_backstops_only_buckets_with_no_stated_reset(self):
        # The bound is a backstop, not a proof. A snapshot can be taken a
        # minute before a reset, so age never establishes that none was
        # crossed; `reset_passed` is what answers that.
        assert engines.SNAPSHOT_MAX_AGE_SECONDS < 5 * 3600

    def _reset_in(self, **delta):
        import datetime as _dt

        return (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(**delta)).isoformat()

    def test_a_passed_reset_is_refused_even_well_inside_the_age_bound(
        self, tmp_path, monkeypatch
    ):
        """The premise the age bound rested on is false: a snapshot taken near
        the end of a window is young and already wrong. Three hours old, inside
        the four-hour bound, and its stated reset passed 85 minutes ago."""
        import time as _time

        self._stub(
            monkeypatch, tmp_path,
            _time.time() - 3 * 3600,
            self._reset_in(minutes=-85),
        )
        with pytest.raises(engines.QuotaError) as exc:
            engines.quota("codex")
        assert "no state database" in str(exc.value)
        assert "has passed" in str(exc.value)

    def test_a_reset_still_ahead_answers_from_the_same_age(
        self, tmp_path, monkeypatch
    ):
        """The other direction of the same discrimination. Identical age; only
        the reset moved. Without this, refusing everything would pass too."""
        import time as _time

        self._stub(
            monkeypatch, tmp_path,
            _time.time() - 3 * 3600,
            self._reset_in(minutes=85),
        )
        payload = engines.quota("codex")
        assert payload["origin"] == "snapshot"

    def test_a_reset_still_ahead_does_not_excuse_a_snapshot_past_the_bound(
        self, tmp_path, monkeypatch
    ):
        """The two guards are independent and both apply to every snapshot. A
        reset in the future says the window has not turned over; it says
        nothing about how much of it was spent in the hours since the reading."""
        import time as _time

        self._stub(
            monkeypatch, tmp_path,
            _time.time() - (engines.SNAPSHOT_MAX_AGE_SECONDS + 3600),
            self._reset_in(hours=4),
        )
        with pytest.raises(engines.QuotaError) as exc:
            engines.quota("codex")
        assert "past the" in str(exc.value)

    def test_an_unparseable_reset_falls_back_to_the_age_bound(
        self, tmp_path, monkeypatch
    ):
        import time as _time

        self._stub(monkeypatch, tmp_path, _time.time() - 60, "not a timestamp")
        assert engines.quota("codex")["origin"] == "snapshot"

    def test_a_snapshot_a_moment_ahead_is_clock_jitter_and_still_answers(
        self, tmp_path, monkeypatch
    ):
        """The other direction. A slewing clock puts a snapshot fractions of a
        second ahead; refusing that would be a spurious quota failure."""
        import time as _time

        self._stub(monkeypatch, tmp_path, _time.time() + 5)
        assert engines.quota("codex")["origin"] == "snapshot"

    def test_a_snapshot_dated_in_the_future_proves_nothing_and_is_refused(
        self, tmp_path, monkeypatch
    ):
        """`max(0, now - saved_at)` clamped a moved clock to an age of nothing,
        which read as the freshest possible snapshot."""
        import time as _time

        self._stub(monkeypatch, tmp_path, _time.time() + 10 * 3600)
        with pytest.raises(engines.QuotaError) as exc:
            engines.quota("codex")
        assert "in the future" in str(exc.value)


class TestTheCaveatPrecedesTheNumbers:
    """A note printed under the figures is read after the reader believed them."""

    def test_note_renders_above_the_buckets(self):
        text = engines.format_text({
            "engine": "codex",
            "origin": "snapshot",
            "age_seconds": 120,
            "note": "live check failed: no state database",
            "buckets": [{"label": "Weekly", "remaining_fraction": 0.0}],
        })
        lines = text.splitlines()
        note_at = next(i for i, l in enumerate(lines) if "note:" in l)
        bucket_at = next(i for i, l in enumerate(lines) if "Weekly" in l)
        assert note_at < bucket_at


class TestQuotaExitCodes:
    """Three states need three exits. With only two, a caller could tell a
    working engine from a broken one solely by reading prose — which is how one
    engine failed for eleven days behind a plausible-looking reading."""

    def _serve(self, monkeypatch, origin):
        monkeypatch.setattr(
            engines, "quota",
            lambda target: {"engine": "codex", "origin": origin, "buckets": [],
                            "note": None if origin == "live" else "live check failed: x"},
        )

    def test_a_live_answer_exits_zero(self, monkeypatch, capsys):
        import r4t as r4t_mod

        self._serve(monkeypatch, "live")
        assert r4t_mod.main(["engine", "codex", "quota"]) == 0
        capsys.readouterr()

    def test_a_snapshot_served_after_failure_exits_distinctly(self, monkeypatch, capsys):
        import r4t as r4t_mod

        self._serve(monkeypatch, "snapshot")
        rc = r4t_mod.main(["engine", "codex", "quota"])
        assert rc == r4t_mod.QUOTA_EXIT_STALE
        assert rc not in (0, 1), "must differ from both live and no-answer"
        capsys.readouterr()
