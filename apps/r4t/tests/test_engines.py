from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import json

import pytest

import engines
from engines import agy, claude, codex, copilot, cursor, muse
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

    def test_every_engine_answers_quota(self):
        for name in engines.MODULES:
            assert "quota" in engines.capabilities(name)

    def test_only_the_verified_engines_also_answer_for_run(self):
        run_engines = {
            "claude", "codex", "agy", "copilot", "cursor", "opencode", "muse",
            "devin",
        }
        for name in engines.MODULES:
            expected = ["quota"]
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
        found = list(cursor.state_db_candidates())
        assert any(
            {"ana", "AppData", "Roaming", "Cursor"} <= set(p.parts) for p in found
        )
        assert any(
            {"bo", "AppData", "Roaming", "Cursor"} <= set(p.parts) for p in found
        )
        # The Linux path stays first — a locally installed IDE outranks a guess.
        assert ".config" in found[0].parts

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
        assert "auth.json" in message

    def test_permission_error_on_one_candidate_does_not_hide_the_next(
        self, monkeypatch, tmp_path
    ):
        # Python 3.12 re-raises EACCES from Path.is_file. A Windows profile
        # this process cannot stat sorts ahead of the one that holds the token.
        blocked, real = tmp_path / "blocked.vscdb", tmp_path / "real.vscdb"
        conn = sqlite3.connect(real)
        conn.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
        conn.execute(
            "INSERT INTO ItemTable VALUES (?, ?)",
            (cursor.ACCESS_TOKEN_KEY, '"tok-real"'),
        )
        conn.commit()
        conn.close()
        real_stat = os.stat

        def stat(path, *args, **kwargs):
            if Path(path) == blocked:
                raise PermissionError(13, "Permission denied")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", stat)
        monkeypatch.setattr(cursor, "state_db_candidates", lambda: [blocked, real])
        assert cursor.state_db() == real
        assert cursor._state_value(cursor.ACCESS_TOKEN_KEY) == "tok-real"

    def test_disk_io_error_retries_immutable(self, monkeypatch, tmp_path):
        # Measured on WSL: mode=ro against a Windows state.vscdb raises
        # "disk I/O error"; mode=ro&immutable=1 reads the token.
        db = tmp_path / "state.vscdb"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
        conn.execute(
            "INSERT INTO ItemTable VALUES (?, ?)",
            (cursor.ACCESS_TOKEN_KEY, '"tok-imm"'),
        )
        conn.commit()
        conn.close()
        real_connect = sqlite3.connect
        seen = []

        def connect(target, *args, **kwargs):
            seen.append(str(target))
            if "immutable=1" not in str(target):
                raise sqlite3.OperationalError("disk I/O error")
            return real_connect(target, *args, **kwargs)

        monkeypatch.setattr(cursor.sqlite3, "connect", connect)
        assert cursor._read_state(db, cursor.ACCESS_TOKEN_KEY) == "tok-imm"
        assert any("mode=ro" in uri and "immutable=1" not in uri for uri in seen)
        assert any("immutable=1" in uri for uri in seen)

    def test_other_operational_errors_do_not_retry_immutable(self, monkeypatch, tmp_path):
        db = tmp_path / "state.vscdb"
        db.write_text("", encoding="utf-8")
        seen = []

        def connect(target, *args, **kwargs):
            seen.append(str(target))
            raise sqlite3.OperationalError("no such table: ItemTable")

        monkeypatch.setattr(cursor.sqlite3, "connect", connect)
        assert cursor._read_state(db, cursor.ACCESS_TOKEN_KEY) is None
        assert len(seen) == 1
        assert "immutable=1" not in seen[0]

    def test_wsl_unreadable_profile_is_skipped(self, monkeypatch, tmp_path):
        monkeypatch.delenv(cursor.STATE_DB_ENV, raising=False)
        monkeypatch.setattr(cursor.sys, "platform", "linux")
        users = tmp_path / "Users"
        (users / "ana").mkdir(parents=True)
        (users / "locked").mkdir()
        monkeypatch.setattr(cursor, "_WSL_USERS_DIR", users)
        real_stat = os.stat

        def stat(path, *args, **kwargs):
            if Path(path).name == "locked":
                raise PermissionError(13, "Permission denied")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", stat)
        found = list(cursor._wsl_candidates())
        assert any(p.parts[-3:] == ("ana", "AppData", "Roaming") for p in found)
        assert all("locked" not in p.parts for p in found)

    def test_cli_auth_file_follows_the_platform(self, monkeypatch, tmp_path):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setattr(cursor.sys, "platform", "linux")
        assert cursor.cli_auth_candidates() == [
            Path.home() / ".config" / "cursor" / "auth.json"
        ]
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert cursor.cli_auth_candidates() == [tmp_path / "cursor" / "auth.json"]
        monkeypatch.setattr(cursor.sys, "platform", "darwin")
        assert cursor.cli_auth_candidates() == [Path.home() / ".cursor" / "auth.json"]
        monkeypatch.setattr(cursor.sys, "platform", "win32")
        monkeypatch.setenv("APPDATA", str(tmp_path))
        assert cursor.cli_auth_candidates() == [tmp_path / "Cursor" / "auth.json"]


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

    def test_policy_rides_config_keys_app_server_applies(self):
        # app-server parses the top-level -s/-a and then ignores them, so the
        # probe states its read-only intent as -c keys after the subcommand.
        argv = codex.QUOTA_ARGV
        assert argv[:2] == ["codex", "app-server"]
        assert 'sandbox_mode="read-only"' in argv
        assert f'approval_policy="{codex.APPROVAL_POLICY}"' in argv
        for ignored in ("-s", "-a", "--sandbox", "--ask-for-approval"):
            assert ignored not in argv

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

    def test_an_argon_model_is_its_own_pool(self):
        result = agy.parse_user_status(
            {
                "userStatus": {
                    "cascadeModelConfigData": {
                        "clientModelConfigs": [
                            {"label": "Gemini 3.1 Pro", "quotaInfo": {"remainingFraction": 0.9}},
                            {"label": "Gemini 4 Argon", "quotaInfo": {"remainingFraction": 0.2}},
                        ]
                    },
                }
            }
        )
        labels = {b["label"]: b["remaining_fraction"] for b in result["buckets"]}
        assert labels == {
            "Gemini Weekly Limit": pytest.approx(0.9),
            "Gemini 4 Argon Weekly Limit": pytest.approx(0.2),
        }

    def test_no_quota_models_raises(self):
        with pytest.raises(QuotaError):
            agy.parse_user_status({"userStatus": {}})


class TestAgyUsageCommand:
    """The `agy -p "/usage"` print path: all four pools, no session needed."""

    PAYLOAD = {
        "status": "SUCCESS",
        "command": {
            "name": "usage",
            "data": {
                "groups": [
                    {
                        "name": "Gemini Models",
                        "buckets": [
                            {
                                "id": "gemini-weekly",
                                "window": "weekly",
                                "remaining_fraction": 0.89,
                                "reset_time": "2026-09-30T02:24:25Z",
                            },
                            {
                                "id": "gemini-5h",
                                "window": "5h",
                                "remaining_fraction": 0.98,
                                "reset_time": "2026-09-24T22:54:39Z",
                            },
                        ],
                    },
                    {
                        "name": "Claude and GPT models",
                        "buckets": [
                            {
                                "id": "3p-weekly",
                                "window": "weekly",
                                "remaining_fraction": 0.55,
                                "reset_time": "2026-09-25T14:39:37Z",
                            },
                            {
                                "id": "3p-5h",
                                "window": "5h",
                                "remaining_fraction": 1.0,
                                "reset_time": "2026-09-24T23:16:59Z",
                            },
                        ],
                    },
                ]
            },
        },
    }

    def test_all_four_pools_arrive(self):
        result = agy.parse_usage_command(self.PAYLOAD)
        labels = {b["label"]: b for b in result["buckets"]}
        assert set(labels) == {
            "Gemini Weekly Limit",
            "Gemini Five Hour Limit",
            "Claude/GPT Weekly Limit",
            "Claude/GPT Five Hour Limit",
        }
        assert labels["Gemini Weekly Limit"]["remaining_fraction"] == pytest.approx(0.89)
        assert labels["Claude/GPT Five Hour Limit"]["reset_time"] == (
            "2026-09-24T23:16:59Z"
        )
        assert result["origin"] == "live"

    def test_a_pool_with_its_own_group_keeps_its_name(self):
        # The shape agy 1.4.0 prints on a paid plan: the two shared groups
        # and "Gemini 4 Argon" in a group of its own. Matched on "gemini"
        # alone, Argon's row became a second "Gemini Weekly Limit".
        payload = json.loads(json.dumps(self.PAYLOAD))
        payload["command"]["data"]["groups"].append(
            {
                "name": "Gemini 4 Argon",
                "description": "For paid Pro plans, Argon quota is separate.",
                "buckets": [
                    {
                        "id": "gemini-argon-wtu-weekly",
                        "name": "Gemini 4 Argon Weekly Limit",
                        "window": "weekly",
                        "remaining_fraction": 0.9994161128997803,
                        "reset_time": "2026-10-24T00:15:38Z",
                    }
                ],
            }
        )
        result = agy.parse_usage_command(payload)
        labels = [b["label"] for b in result["buckets"]]
        assert labels.count("Gemini Weekly Limit") == 1
        assert labels[-1] == "Gemini 4 Argon Weekly Limit"
        assert result["buckets"][-1]["reset_time"] == "2026-10-24T00:15:38Z"

    def test_no_command_payload_raises(self):
        # An agy too old for machine-readable slash answers prints the table
        # as text — the envelope carries no `command`, which must refuse
        # rather than parse the prose.
        with pytest.raises(QuotaError) as caught:
            agy.parse_usage_command({"status": "SUCCESS", "response": "table"})
        assert "1.1.13" in str(caught.value)

    def test_empty_groups_raise(self):
        with pytest.raises(QuotaError):
            agy.parse_usage_command({"command": {"data": {"groups": []}}})

    def test_quota_prefers_the_print_path(self, monkeypatch):
        monkeypatch.setattr(agy, "_usage_command", lambda: self.PAYLOAD)
        monkeypatch.setattr(agy, "_plan_from_local_api", lambda: "Pro")
        # The language-server path must not be consulted at all when the
        # print path answers.
        monkeypatch.setattr(
            agy,
            "_find_language_server",
            lambda: (_ for _ in ()).throw(AssertionError("consulted")),
        )
        result = agy.quota()
        assert len(result["buckets"]) == 4
        assert result["plan"] == "Pro"

    def test_quota_falls_back_to_the_local_api(self, monkeypatch):
        monkeypatch.setattr(
            agy,
            "_usage_command",
            lambda: (_ for _ in ()).throw(QuotaError("agy is not on PATH")),
        )
        monkeypatch.setattr(agy, "_find_language_server", lambda: (1, "csrf"))
        monkeypatch.setattr(agy, "_listen_ports", lambda pid: [8080])
        monkeypatch.setattr(
            agy,
            "_post_user_status",
            lambda ports, csrf: {
                "userStatus": {
                    "planStatus": {"planInfo": {"planDisplayName": "Pro"}},
                    "cascadeModelConfigData": {
                        "clientModelConfigs": [
                            {
                                "label": "Gemini 3.1 Pro",
                                "quotaInfo": {"remainingFraction": 0.5},
                            }
                        ]
                    },
                }
            },
        )
        result = agy.quota()
        assert result["buckets"][0]["label"] == "Gemini Weekly Limit"

    def test_quota_combines_both_failures(self, monkeypatch):
        monkeypatch.setattr(
            agy,
            "_usage_command",
            lambda: (_ for _ in ()).throw(QuotaError("agy is not on PATH")),
        )
        monkeypatch.setattr(
            agy,
            "_find_language_server",
            lambda: (_ for _ in ()).throw(QuotaError("language server not running")),
        )
        with pytest.raises(QuotaError) as caught:
            agy.quota()
        message = str(caught.value)
        assert "agy is not on PATH" in message
        assert "language server not running" in message


class TestMuseParse:
    """`usage/read` over `muse serve`: the last-observed window, or nothing."""

    def test_window_and_weekly_become_buckets(self):
        result = muse.parse_usage(
            {
                "usage": {
                    "tier": "power",
                    "observedAtMs": 1790273739931,
                    "window": {
                        "usedPercent": 40,
                        "windowDurationMins": 300,
                        "resetsAtMs": 1790286036000,
                    },
                    "weekly": {
                        "usedPercent": 16,
                        "resetsAtMs": 1790553600000,
                    },
                }
            }
        )
        assert result["plan"] == "power"
        window, weekly = result["buckets"]
        assert window["label"] == "Five Hour Limit"
        assert window["remaining_fraction"] == pytest.approx(0.60)
        assert window["reset_time"].startswith("2026-09-24T")
        assert weekly["label"] == "Weekly Limit"
        assert weekly["remaining_fraction"] == pytest.approx(0.84)
        assert "observed" in result["note"]

    def test_over_spend_clamps_to_empty(self):
        # usedPercent may exceed 100 — a remainder cannot go below zero.
        result = muse.parse_usage(
            {
                "usage": {
                    "window": {"usedPercent": 140, "windowDurationMins": 300},
                    "weekly": {"usedPercent": 50},
                }
            }
        )
        assert result["buckets"][0]["remaining_fraction"] == 0.0
        assert result["buckets"][0]["reset_time"] is None

    def test_a_cold_host_raises_instead_of_inventing(self):
        # `usage` is omitted, never null, when the host has observed nothing —
        # the honest failure that lets the snapshot fallback answer.
        with pytest.raises(QuotaError) as caught:
            muse.parse_usage({})
        assert "observed no usage" in str(caught.value)

    def test_windows_with_no_fields_raise(self):
        with pytest.raises(QuotaError):
            muse.parse_usage({"usage": {}})


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


class TestSnapshotPathFollowsR4tHome:
    """Rigs and rosters relocate wholesale with R4T_HOME (state.py); the quota
    cache must follow the same rule. A stranger's first `rig detect` must not
    touch state it did not create (#262)."""

    def _isolate(self, tmp_path, monkeypatch):
        r4t_home_dir = tmp_path / "r4t-home"
        real_home = tmp_path / "real-home"
        monkeypatch.setenv("R4T_HOME", str(r4t_home_dir))
        monkeypatch.setenv("HOME", str(real_home))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        return r4t_home_dir, real_home

    def test_snapshot_path_resolves_under_r4t_home(self, tmp_path, monkeypatch):
        r4t_home_dir, _real_home = self._isolate(tmp_path, monkeypatch)
        assert engines.snapshot_path("codex") == r4t_home_dir / "quota" / "codex.json"

    def test_a_live_quota_check_writes_under_r4t_home_not_the_real_config_home(
        self, tmp_path, monkeypatch
    ):
        r4t_home_dir, real_home = self._isolate(tmp_path, monkeypatch)
        monkeypatch.setattr(
            codex, "quota",
            lambda: {"engine": "codex", "origin": "live", "buckets": [], "note": None},
        )

        result = engines.quota("codex")

        assert result["origin"] == "live"
        written = r4t_home_dir / "quota" / "codex.json"
        assert written.is_file()
        real_default_quota_dir = real_home / ".config" / "r4t" / "quota"
        assert not real_default_quota_dir.exists()

    def test_sandboxs_advertised_home_is_where_the_quota_path_lands(
        self, tmp_path, monkeypatch
    ):
        # `r4t sandbox` (sandbox.py:run_sandbox) advertises a throwaway home by
        # setting R4T_HOME to <tmp>/r4t-home for the run's own process — the
        # same mechanism exercised above, pinned here to the sandbox's own
        # naming so a future sandbox refactor that drops the env var trips
        # this test too.
        sandbox_tmp = tmp_path / "r4t-sandbox-abc123"
        monkeypatch.setenv("R4T_HOME", str(sandbox_tmp / "r4t-home"))
        monkeypatch.setenv("HOME", str(tmp_path / "real-home"))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

        path = engines.snapshot_path("claude")

        assert path.is_relative_to(sandbox_tmp)


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
            lambda target, spend=False: {"engine": "codex", "origin": origin, "buckets": [],
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


class TestClaudeTokenRefresh:
    NOW_MS = 1_800_000_000_000

    @staticmethod
    def _creds(token, expires_at=None):
        oauth = {"accessToken": token}
        if expires_at is not None:
            oauth["expiresAt"] = expires_at
        return {"claudeAiOauth": oauth}

    def _wire(self, monkeypatch, stored, usage, doctor=None):
        """stored: credential dicts the fake Keychain answers in turn (the last
        repeats). usage: statuses (int) or payloads (dict) per usage request."""
        import io
        import subprocess
        import urllib.error

        log = {"doctor": 0, "usage": [], "reads": 0}
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        def keychain():
            log["reads"] += 1
            return stored[min(log["reads"], len(stored)) - 1] if stored else None

        def run(argv, **kwargs):
            if argv[:2] == ["claude", "doctor"]:
                log["doctor"] += 1
                if isinstance(doctor, BaseException):
                    raise doctor
                return subprocess.CompletedProcess(argv, doctor or 0, b"", b"")
            return subprocess.CompletedProcess(argv, 0, "2.1.0 (Claude Code)\n", "")

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(request, timeout=None):
            if request.full_url == claude.PROFILE_URL:
                raise urllib.error.URLError("no profile in tests")
            token = request.get_header("Authorization").removeprefix("Bearer ")
            log["usage"].append(token)
            answer = usage[len(log["usage"]) - 1]
            if isinstance(answer, int):
                raise urllib.error.HTTPError(request.full_url, answer, "x", {}, None)
            import json
            return Response(json.dumps(answer).encode())

        monkeypatch.setattr(claude, "_keychain_credentials", keychain)
        monkeypatch.setattr(claude, "_file_credentials", lambda: None)
        monkeypatch.setattr(claude.subprocess, "run", run)
        monkeypatch.setattr(claude.urllib.request, "urlopen", urlopen)
        return log

    OK = {"five_hour": {"utilization": 10.0, "resets_at": None}}

    def test_expiry_is_at_or_before_now_and_unknown_is_not_expired(self):
        now = self.NOW_MS / 1000
        assert claude._token_expired(self._creds("t", self.NOW_MS), now)
        assert claude._token_expired(self._creds("t", self.NOW_MS - 1), now)
        assert not claude._token_expired(self._creds("t", self.NOW_MS + 1), now)
        assert not claude._token_expired(self._creds("t"), now)
        assert not claude._token_expired(self._creds("t", "soon"), now)
        assert not claude._token_expired({}, now)

    def test_a_valid_stored_token_never_runs_doctor(self, monkeypatch):
        future = int((__import__("time").time() + 3600) * 1000)
        log = self._wire(monkeypatch, [self._creds("good", future)], [self.OK])
        result = claude.quota()
        assert log["doctor"] == 0
        assert log["usage"] == ["good"]
        assert result["note"] is None

    def test_an_expired_token_runs_doctor_once_and_uses_the_new_token(self, monkeypatch):
        log = self._wire(
            monkeypatch,
            [self._creds("old", 1000), self._creds("new", 9_999_999_999_999)],
            [self.OK],
        )
        result = claude.quota()
        assert log["doctor"] == 1
        assert log["usage"] == ["new"]
        assert result["note"] == "token refreshed by claude doctor"

    def test_a_401_on_an_unexpired_token_refreshes_and_retries_once(self, monkeypatch):
        far = 9_999_999_999_999
        log = self._wire(
            monkeypatch,
            [self._creds("stale", far), self._creds("fresh", far)],
            [401, self.OK],
        )
        result = claude.quota()
        assert log["doctor"] == 1
        assert log["usage"] == ["stale", "fresh"]
        assert result["note"] == "token refreshed by claude doctor"

    def test_a_401_after_the_refresh_names_auth_login_and_doctor_ran_once(self, monkeypatch):
        far = 9_999_999_999_999
        log = self._wire(
            monkeypatch,
            [self._creds("stale", far), self._creds("fresh", far)],
            [401, 401],
        )
        with pytest.raises(QuotaError, match="claude auth login"):
            claude.quota()
        assert log["doctor"] == 1
        assert len(log["usage"]) == 2

    def test_a_401_right_after_an_expiry_refresh_does_not_refresh_twice(self, monkeypatch):
        log = self._wire(
            monkeypatch,
            [self._creds("old", 1000), self._creds("new", 9_999_999_999_999)],
            [401],
        )
        with pytest.raises(QuotaError, match="claude auth login"):
            claude.quota()
        assert log["doctor"] == 1
        assert len(log["usage"]) == 1

    @pytest.mark.parametrize(
        "failure",
        [OSError("no claude"), __import__("subprocess").TimeoutExpired("claude", 45), 1],
    )
    def test_a_failing_doctor_names_auth_login(self, monkeypatch, failure):
        log = self._wire(monkeypatch, [self._creds("old", 1000)], [self.OK], doctor=failure)
        with pytest.raises(QuotaError, match="claude auth login"):
            claude.quota()
        assert log["usage"] == []

    def test_an_env_token_is_used_as_given_and_a_401_never_runs_doctor(self, monkeypatch):
        log = self._wire(monkeypatch, [], [401])
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "from-env")
        with pytest.raises(QuotaError, match="set a fresh CLAUDE_CODE_OAUTH_TOKEN"):
            claude.quota()
        assert log["doctor"] == 0
        assert log["usage"] == ["from-env"]

    def _http_error(self, monkeypatch, status, body):
        import io
        import urllib.error

        def urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, status, "x", {}, io.BytesIO(body)
            )

        monkeypatch.setattr(claude.urllib.request, "urlopen", urlopen)

    def test_an_http_error_with_a_json_body_names_the_endpoints_error_type(self, monkeypatch):
        self._http_error(
            monkeypatch,
            403,
            b'{"type":"error","error":{"type":"permission_error","message":"OAuth '
            b'authentication is currently not allowed for this organization."}}',
        )
        with pytest.raises(QuotaError) as caught:
            claude._fetch_usage("t")
        assert str(caught.value) == (
            "usage endpoint returned HTTP 403 (permission_error: OAuth authentication "
            "is currently not allowed for this organization.)"
        )

    def test_an_http_error_with_a_non_json_body_keeps_the_plain_message(self, monkeypatch):
        self._http_error(monkeypatch, 502, b"<html>bad gateway</html>")
        with pytest.raises(QuotaError) as caught:
            claude._fetch_usage("t")
        assert str(caught.value) == "usage endpoint returned HTTP 502"


class TestClaudeLiveNeverRefreshes:
    """Live mode reports an expired or rejected stored login; it never runs
    `claude doctor`, which rotates the login and can outlast the caller's
    bound. The plain read keeps refreshing."""

    HINT = "run `claude` once to refresh"
    _wire = TestClaudeTokenRefresh._wire
    _creds = staticmethod(TestClaudeTokenRefresh._creds)
    OK = TestClaudeTokenRefresh.OK

    def test_expired_stored_login_is_unavailable_without_doctor_or_request(
        self, monkeypatch
    ):
        log = self._wire(monkeypatch, [self._creds("old", 1000)], [self.OK])
        with pytest.raises(engines.LiveQuotaError, match=self.HINT):
            claude.quota(live=True)
        assert log["doctor"] == 0 and log["usage"] == []

    def test_rejected_stored_login_is_unavailable_without_doctor_or_retry(
        self, monkeypatch
    ):
        future = int((__import__("time").time() + 3600) * 1000)
        log = self._wire(monkeypatch, [self._creds("stale", future)], [401, self.OK])
        with pytest.raises(engines.LiveQuotaError, match=self.HINT):
            claude.quota(live=True)
        assert log["doctor"] == 0 and log["usage"] == ["stale"]

    def test_through_the_engine_entry_the_instruction_is_the_fixed_one(
        self, tmp_path, monkeypatch
    ):
        TestLiveQuota()._plant(tmp_path, monkeypatch, engine="claude")
        log = self._wire(monkeypatch, [self._creds("old", 1000)], [self.OK])
        with pytest.raises(engines.LiveQuotaError) as caught:
            engines.quota("claude", live=True)
        assert self.HINT in str(caught.value) and "doctor" not in str(caught.value)
        assert log["doctor"] == 0

    def test_valid_login_is_read_live_and_the_default_still_refreshes(
        self, monkeypatch
    ):
        future = int((__import__("time").time() + 3600) * 1000)
        log = self._wire(monkeypatch, [self._creds("good", future)], [self.OK])
        assert claude.quota(live=True)["origin"] == "live"
        assert log["doctor"] == 0
        log = self._wire(
            monkeypatch,
            [self._creds("old", 1000), self._creds("new", 9_999_999_999_999)],
            [self.OK],
        )
        assert claude.quota()["note"] == "token refreshed by claude doctor"
        assert log["doctor"] == 1


class TestLiveQuota:
    """`engines.quota(..., live=True)` is a reading taken now or a failure: a
    saved snapshot can never stand in for it, and a live success never
    becomes one."""

    LIVE = {"origin": "live", "plan": None, "note": None,
            "buckets": [{"label": "Weekly", "remaining_fraction": 0.5,
                         "reset_time": None}]}

    def _plant(self, tmp_path, monkeypatch, engine="codex"):
        home = tmp_path / "r4t-home"
        monkeypatch.setenv("R4T_HOME", str(home))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "COPILOT_HOME",
                     "CLAUDE_CODE_OAUTH_TOKEN", *claude.API_AUTH_VARS):
            monkeypatch.delenv(name, raising=False)
        path = home / "quota" / f"{engine}.json"
        path.parent.mkdir(parents=True)
        body = json.dumps({
            "saved_at": __import__("time").time(),
            "payload": {"engine": engine, "origin": "live",
                        "buckets": [{"label": "Planted",
                                     "remaining_fraction": 0.99}]},
        })
        path.write_text(body, encoding="utf-8")
        return path, body

    def _no_snapshot_io(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("snapshot touched")
        monkeypatch.setattr(engines, "load_snapshot", boom)
        monkeypatch.setattr(engines, "save_snapshot", boom)

    def test_failed_live_never_reads_the_planted_snapshot(self, tmp_path, monkeypatch):
        path, body = self._plant(tmp_path, monkeypatch)
        self._no_snapshot_io(monkeypatch)

        def fail():
            raise QuotaError("codex is not on PATH")
        monkeypatch.setattr(codex, "quota", fail)
        with pytest.raises(QuotaError, match="live quota query failed"):
            engines.quota("codex", live=True)
        assert path.read_text(encoding="utf-8") == body

    def test_live_success_writes_no_snapshot(self, tmp_path, monkeypatch):
        path, body = self._plant(tmp_path, monkeypatch)
        self._no_snapshot_io(monkeypatch)
        monkeypatch.setattr(codex, "quota", lambda: dict(self.LIVE))
        payload = engines.quota("codex", live=True)
        assert payload["engine"] == "codex" and payload["origin"] == "live"
        assert path.read_text(encoding="utf-8") == body

    def test_a_non_live_origin_is_refused(self, tmp_path, monkeypatch):
        self._plant(tmp_path, monkeypatch)
        monkeypatch.setattr(codex, "quota", lambda: {**self.LIVE, "origin": "snapshot"})
        with pytest.raises(QuotaError, match="no live observation"):
            engines.quota("codex", live=True)

    def test_unknown_and_ollama_are_not_applicable(self):
        with pytest.raises(engines.QuotaNotApplicable):
            engines.quota("emacs", live=True)
        with pytest.raises(engines.QuotaNotApplicable):
            engines.quota("ollama-claude", live=True)
        assert engines.quota("ollama")["buckets"][0]["remaining_fraction"] == 1.0

    def test_live_does_not_imply_spend_for_a_turn_minting_engine(self, monkeypatch):
        class Minting:
            QUOTA_SPENDS_TURN = True
            calls = 0

            @classmethod
            def quota(cls):
                cls.calls += 1
                return TestLiveQuota.LIVE

        monkeypatch.setitem(engines.MODULES, "muse", Minting)
        with pytest.raises(QuotaError, match="no free live"):
            engines.quota("muse", live=True)
        assert Minting.calls == 0

    def test_home_override_for_an_env_blind_adapter_refuses(self, tmp_path, monkeypatch):
        self._plant(tmp_path, monkeypatch)

        def never():
            raise AssertionError("queried")
        monkeypatch.setattr(codex, "quota", never)
        with pytest.raises(QuotaError, match="account mismatch"):
            engines.quota("codex", live=True, env={"CODEX_HOME": str(tmp_path / "other")})

    def test_relative_state_home_refuses(self, tmp_path, monkeypatch):
        self._plant(tmp_path, monkeypatch)
        monkeypatch.setenv("CODEX_HOME", "relative/dir")
        monkeypatch.setattr(codex, "quota", lambda: pytest.fail("queried"))
        with pytest.raises(QuotaError, match="account mismatch"):
            engines.quota("codex", live=True)

    @pytest.mark.parametrize("name", [
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
    ])
    def test_claude_api_auth_mode_refuses_without_echoing_the_value(
        self, tmp_path, monkeypatch, name
    ):
        self._plant(tmp_path, monkeypatch)
        monkeypatch.setenv(name, "sk-sentinel-secret")
        monkeypatch.setattr(claude, "quota", lambda: pytest.fail("queried"))
        with pytest.raises(QuotaError, match="account mismatch") as exc:
            engines.quota("claude", live=True)
        assert name in str(exc.value) and "sentinel" not in str(exc.value)

    def test_claude_custom_config_dir_refuses_but_the_default_passes(
        self, tmp_path, monkeypatch
    ):
        self._plant(tmp_path, monkeypatch)
        monkeypatch.setattr(claude, "_credentials", lambda: {})
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
        assert "CLAUDE_CONFIG_DIR" in claude.live_account_problem()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "home" / ".claude"))
        assert claude.live_account_problem() is None
        monkeypatch.delenv("CLAUDE_CONFIG_DIR")
        assert claude.live_account_problem() is None

    def test_claude_env_token_with_a_stored_login_is_ambiguous(
        self, tmp_path, monkeypatch
    ):
        self._plant(tmp_path, monkeypatch)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-sentinel")
        monkeypatch.setattr(
            claude, "_credentials",
            lambda: {"claudeAiOauth": {"accessToken": "stored"}},
        )
        problem = claude.live_account_problem()
        assert "ambiguous" in problem and "sentinel" not in problem
        monkeypatch.setattr(claude, "_credentials", lambda: {})
        assert claude.live_account_problem() is None

    def test_claude_keychain_account_follows_the_user_not_a_moved_home(
        self, tmp_path, monkeypatch
    ):
        self._plant(tmp_path, monkeypatch)
        monkeypatch.setattr(claude, "_credentials", lambda: {})
        monkeypatch.setattr(claude.sys, "platform", "darwin")
        monkeypatch.setattr(claude, "_passwd_home", lambda: str(tmp_path / "real"))
        assert "Keychain" in claude.live_account_problem()

    def test_agy_live_uses_usage_command_only(self, monkeypatch):
        def local_api(*a, **k):
            raise AssertionError("machine-wide language server consulted")
        for name in ("_find_language_server", "_listen_ports", "_post_user_status",
                     "_plan_from_local_api"):
            monkeypatch.setattr(agy, name, local_api)
        good = {"command": {"data": {"groups": [{"name": "Gemini Models", "buckets": [
            {"window": "weekly", "remaining_fraction": 0.4, "reset_time": "2099-01-01T00:00:00+00:00"}]}]}}}
        monkeypatch.setattr(agy, "_usage_command", lambda: good)
        payload = engines.quota("agy", live=True)
        assert payload["plan"] is None and payload["buckets"][0]["remaining_fraction"] == 0.4

        def broken():
            raise QuotaError("agy /usage exited 1")
        monkeypatch.setattr(agy, "_usage_command", broken)
        with pytest.raises(QuotaError, match="agy live quota query failed") as exc:
            engines.quota("agy", live=True)
        assert "exited 1" not in str(exc.value) and "local API" not in str(exc.value)

    def test_opencode_live_is_unavailable_and_delegates_nothing(
        self, tmp_path, monkeypatch
    ):
        self._plant(tmp_path, monkeypatch)
        for module in (claude, codex):
            monkeypatch.setattr(module, "quota", lambda *a, **k: pytest.fail("delegated"))
        with pytest.raises(QuotaError, match="unavailable"):
            engines.quota("opencode", live=True)

    def test_opencode_default_quota_still_delegates(self, monkeypatch, tmp_path):
        from engines import opencode
        auth = tmp_path / "auth.json"
        auth.write_text(json.dumps({"anthropic": {"type": "oauth"}}), encoding="utf-8")
        monkeypatch.setattr(opencode, "AUTH_PATH", auth)
        monkeypatch.setattr(claude, "quota", lambda: dict(self.LIVE))
        assert opencode.quota()["buckets"]


class TestLiveErrorBoundary:
    """An ordinary adapter QuotaError may quote a provider body or stderr; the
    live path replaces it with fixed text, and only LiveQuotaError passes."""

    SECRET = "sentinel-secret-7d41"

    def _clean(self, tmp_path, monkeypatch):
        TestLiveQuota()._plant(tmp_path, monkeypatch)
        for name in ("R4T_CURSOR_STATE_DB",):
            monkeypatch.delenv(name, raising=False)

    @pytest.mark.parametrize("engine, module", [
        ("codex", codex), ("claude", claude),
    ])
    def test_ordinary_error_is_replaced_by_fixed_engine_text(
        self, tmp_path, monkeypatch, engine, module
    ):
        self._clean(tmp_path, monkeypatch)

        def leaky():
            raise QuotaError(f"HTTP 500 body: {self.SECRET}")
        monkeypatch.setattr(module, "quota", leaky)
        with pytest.raises(QuotaError) as exc:
            engines.quota(engine, live=True)
        text = str(exc.value)
        assert self.SECRET not in text and text.startswith(f"{engine} live quota query failed")
        assert isinstance(exc.value, engines.LiveQuotaError)

    def test_unexpected_exception_is_also_replaced(self, tmp_path, monkeypatch):
        self._clean(tmp_path, monkeypatch)

        def boom():
            raise ValueError(self.SECRET)
        monkeypatch.setattr(codex, "quota", boom)
        with pytest.raises(QuotaError) as exc:
            engines.quota("codex", live=True)
        assert self.SECRET not in str(exc.value)

    def test_public_json_never_carries_provider_text(self, tmp_path, monkeypatch, capsys):
        import r4t as r4t_mod
        self._clean(tmp_path, monkeypatch)
        monkeypatch.setattr(
            codex, "quota", lambda: (_ for _ in ()).throw(QuotaError(self.SECRET))
        )
        rc = r4t_mod.main(["engine", "codex", "quota", "--live", "--json"])
        out = capsys.readouterr()
        assert rc == 1 and self.SECRET not in out.out + out.err

    def test_safe_subtype_keeps_its_fixed_repair_text(self, tmp_path, monkeypatch):
        self._clean(tmp_path, monkeypatch)

        def safe():
            raise engines.LiveQuotaError("codex needs an upgrade")
        monkeypatch.setattr(codex, "quota", safe)
        with pytest.raises(QuotaError, match="needs an upgrade"):
            engines.quota("codex", live=True)

    def test_default_path_keeps_the_adapter_text(self, tmp_path, monkeypatch):
        monkeypatch.setenv("R4T_HOME", str(tmp_path / "empty-r4t"))
        monkeypatch.setattr(
            codex, "quota", lambda: (_ for _ in ()).throw(QuotaError("raw detail"))
        )
        with pytest.raises(QuotaError, match="raw detail"):
            engines.quota("codex")

    def test_agy_error_text_is_not_relayed(self, tmp_path, monkeypatch):
        self._clean(tmp_path, monkeypatch)
        monkeypatch.setattr(
            agy, "_usage_command",
            lambda: (_ for _ in ()).throw(QuotaError(self.SECRET)),
        )
        with pytest.raises(QuotaError) as exc:
            engines.quota("agy", live=True)
        assert self.SECRET not in str(exc.value)

    def test_account_guards_never_contain_values(self, tmp_path, monkeypatch):
        self._clean(tmp_path, monkeypatch)
        monkeypatch.setenv("CODEX_HOME", f"~/{self.SECRET}")
        monkeypatch.setattr(codex, "quota", lambda: pytest.fail("queried"))
        with pytest.raises(QuotaError, match="account mismatch") as exc:
            engines.quota("codex", live=True)
        assert "CODEX_HOME" in str(exc.value) and self.SECRET not in str(exc.value)

    def test_cursor_live_is_unavailable_without_any_lookup(self, tmp_path, monkeypatch):
        from engines import cursor
        self._clean(tmp_path, monkeypatch)
        for name in ("token_sources", "_token_sources", "_fetch_quota", "state_db",
                     "state_db_candidates", "cli_auth_candidates"):
            if hasattr(cursor, name):
                monkeypatch.setattr(cursor, name, lambda *a, **k: pytest.fail("looked"))
        monkeypatch.setattr(
            cursor.urllib.request, "urlopen", lambda *a, **k: pytest.fail("queried")
        )
        with pytest.raises(QuotaError, match="live binding unsupported"):
            engines.quota("cursor", live=True)


class TestLiveQuotaCli:
    def _run(self, argv, capsys):
        import r4t as r4t_mod
        rc = r4t_mod.main(argv)
        out = capsys.readouterr()
        return rc, out.out, out.err

    def _fail(self, monkeypatch):
        def fail():
            raise QuotaError("codex is not on PATH")
        monkeypatch.setattr(codex, "quota", fail)

    def test_live_failure_exits_one_with_json_and_default_still_serves_stale(
        self, tmp_path, monkeypatch, capsys
    ):
        import r4t as r4t_mod
        path, _ = TestLiveQuota()._plant(tmp_path, monkeypatch)
        self._fail(monkeypatch)
        rc, out, _err = self._run(["engine", "codex", "quota", "--live", "--json"], capsys)
        assert rc == 1
        assert json.loads(out) == {
            "origin": "unavailable", "engine": "codex", "applicable": True,
            "code": "query_failed",
            "reason": "codex live quota query failed — sign in with `codex login`, then retry",
        }
        assert "Planted" not in out
        rc, out, _err = self._run(["engine", "codex", "quota", "--json"], capsys)
        assert rc == r4t_mod.QUOTA_EXIT_STALE
        assert json.loads(out)["origin"] == "snapshot"
        assert path.is_file()

    def test_live_success_prints_the_payload_and_exits_zero(
        self, tmp_path, monkeypatch, capsys
    ):
        path, body = TestLiveQuota()._plant(tmp_path, monkeypatch)
        monkeypatch.setattr(codex, "quota", lambda: dict(TestLiveQuota.LIVE))
        rc, out, _err = self._run(["engine", "codex", "quota", "--live", "--json"], capsys)
        assert rc == 0 and json.loads(out)["origin"] == "live"
        assert path.read_text(encoding="utf-8") == body

    def test_live_snapshot_origin_is_rejected(self, tmp_path, monkeypatch, capsys):
        TestLiveQuota()._plant(tmp_path, monkeypatch)
        monkeypatch.setattr(codex, "quota", lambda: {**TestLiveQuota.LIVE, "origin": "snapshot"})
        rc, out, _err = self._run(["engine", "codex", "quota", "--live", "--json"], capsys)
        assert rc == 1 and json.loads(out)["origin"] == "unavailable"

    def test_account_mismatch_makes_no_query(self, tmp_path, monkeypatch, capsys):
        TestLiveQuota()._plant(tmp_path, monkeypatch)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "other"))
        monkeypatch.setattr(claude, "_credentials", lambda: {})
        monkeypatch.setattr(claude, "quota", lambda: pytest.fail("queried"))
        rc, out, _err = self._run(["engine", "claude", "quota", "--live", "--json"], capsys)
        data = json.loads(out)
        assert rc == 1 and data["applicable"] is True
        assert data["reason"].startswith("account mismatch")
        assert data["code"] == "account_mismatch"

    def test_unknown_and_local_engines_are_not_applicable(self, capsys):
        rc, out, _err = self._run(["engine", "emacs", "quota", "--live", "--json"], capsys)
        data = json.loads(out)
        assert rc == 1 and data["applicable"] is False and data["engine"] is None
        rc, out, _err = self._run(["engine", "ollama", "quota", "--live", "--json"], capsys)
        data = json.loads(out)
        assert rc == 1 and data["applicable"] is False and data["engine"] == "ollama"

    def test_live_failure_without_json_goes_to_stderr(self, tmp_path, monkeypatch, capsys):
        TestLiveQuota()._plant(tmp_path, monkeypatch)
        self._fail(monkeypatch)
        rc, out, err = self._run(["engine", "codex", "quota", "--live"], capsys)
        assert rc == 1 and out == "" and "live quota query failed" in err and "not on PATH" not in err

    @pytest.mark.parametrize("argv", [
        ["engine", "codex", "run", "hi", "--live"],
        ["engine", "codex", "check", "--live"],
        ["engine", "check", "--live"],
        ["engine", "list", "--live"],
        ["engine", "codex", "models", "--live"],
        ["engine", "codex", "--live"],
    ])
    def test_live_flag_rejects_other_actions(self, argv, capsys):
        rc, _out, err = self._run(argv, capsys)
        assert rc == 2 and "--live" in err

    def test_help_documents_live(self, capsys):
        import r4t as r4t_mod
        with pytest.raises(SystemExit):
            r4t_mod.main(["engine", "--help"])
        assert "--live" in capsys.readouterr().out


class TestLiveSigtermAndBudget:
    OUTER_S = 60  # a8s's quota-read bound; its SIGTERM teardown grace is 3s
    SIGTERM_GRACE_S = 3

    def test_handler_raises_systemexit_and_the_previous_one_comes_back(
        self, monkeypatch
    ):
        import signal

        import r4t as r4t_mod

        sentinel = lambda *a: None  # noqa: E731
        previous = signal.signal(signal.SIGTERM, sentinel)
        ran = []

        def fake(name, live=False):
            try:
                os.kill(os.getpid(), signal.SIGTERM)
                pytest.fail("SIGTERM did not interrupt the read")
            finally:
                ran.append("finally")

        monkeypatch.setattr(engines, "quota", fake)
        try:
            with pytest.raises(SystemExit) as caught:
                r4t_mod.main(["engine", "codex", "quota", "--live"])
            assert caught.value.code == 128 + signal.SIGTERM
            assert ran == ["finally"]
            assert signal.getsignal(signal.SIGTERM) is sentinel
        finally:
            signal.signal(signal.SIGTERM, previous)

    def test_the_handler_is_restored_after_an_ordinary_failure_too(self, monkeypatch):
        import signal

        import r4t as r4t_mod

        before = signal.getsignal(signal.SIGTERM)

        def fail(name, live=False):
            raise engines.QuotaError("x")

        monkeypatch.setattr(engines, "quota", fail)
        assert r4t_mod.main(["engine", "codex", "quota", "--live"]) == 1
        assert signal.getsignal(signal.SIGTERM) == before

    def test_inner_budgets_fit_the_outer_bound_with_room_for_teardown(self):
        from engines import agy, codex, devin, muse

        teardown = muse.TEARDOWN_GRACE_S + muse.REAP_TIMEOUT_S
        budgets = {
            "muse": (muse.VERSION_TIMEOUT_S + teardown)
            + (muse.LIVE_TIMEOUT_S + teardown),
            "devin": devin.BOOT_TIMEOUT_S + 1.0 + devin.USAGE_TIMEOUT_S + 0.5 + 2,
            "agy": agy.USAGE_CMD_TIMEOUT_S,
            "codex": codex.TIMEOUT_S,
            # two version probes (10s each) around the usage and profile calls
            "claude": 2 * 10 + 2 * claude.TIMEOUT_S,
        }
        for engine, seconds in budgets.items():
            assert seconds < self.OUTER_S, (engine, seconds)
        assert budgets["muse"] == 35
        assert teardown <= self.SIGTERM_GRACE_S - 0.5
