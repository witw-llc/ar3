"""Devin quota is read off the interactive `/usage` view.

The parse tests run on a screen captured from Devin CLI 3000.10.67. The
terminal tests put a stand-in `devin` on PATH and drive it through the real
pseudo-terminal path, so they fail if the argv, the typed command, the
temporary directory or the process-group teardown changes.
"""
from __future__ import annotations

import builtins
import json
import os
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import engines
from engines import devin
from engines.base import QuotaError

pytestmark = pytest.mark.skipif(os.name != "posix", reason="pty is POSIX-only")

NOW = datetime(2026, 10, 17, 14, 0, tzinfo=timezone.utc).timestamp()

SCREEN = (
    "❭ /usage\n\n Fetching quota…\n\n"
    "❭ Ask Devin to build features, fix bugs, or work on your code\n\n"
    " Daily   ■■■■■■■■  66% used  · resets in 6h 47m\n\n"
    " Weekly  ■■■■■■■■  63% used  · resets Oct 18, 1:00 AM (UTC-7)\n\n"
    " No quota consumed yet in this session.\n"
)
BANNER = "  v3000.10.67 · Pro · 34% remaining (resets in 6h 47m)\n"


class TestParse:
    def test_both_bars_become_buckets(self):
        result = devin.parse_usage_screen(SCREEN, now=NOW)
        assert result["origin"] == "live"
        assert [b["label"] for b in result["buckets"]] == ["Daily Limit", "Weekly Limit"]
        daily, weekly = result["buckets"]
        assert daily["remaining_fraction"] == pytest.approx(0.34)
        assert weekly["remaining_fraction"] == pytest.approx(0.37)
        assert daily["reset_time"] == "2026-10-17T20:47:00+00:00"
        assert weekly["reset_time"] == "2026-10-18T08:00:00+00:00"

    def test_plan_comes_from_the_banner_when_drawn(self):
        assert devin.parse_usage_screen(BANNER + SCREEN, now=NOW)["plan"] == "Pro"
        assert devin.parse_usage_screen(SCREEN, now=NOW)["plan"] is None

    def test_the_newest_redraw_wins(self):
        stale = SCREEN.replace("66% used", "10% used")
        result = devin.parse_usage_screen(stale + SCREEN, now=NOW)
        assert result["buckets"][0]["remaining_fraction"] == pytest.approx(0.34)

    def test_a_hidden_daily_bar_leaves_the_weekly_one(self):
        weekly_only = "\n".join(l for l in SCREEN.splitlines() if "Daily" not in l)
        result = devin.parse_usage_screen(weekly_only, now=NOW)
        assert [b["label"] for b in result["buckets"]] == ["Weekly Limit"]

    @pytest.mark.parametrize(
        "text, needle",
        [
            (" No quota data available.\n", "No quota data available"),
            (" Timed out fetching quota data.\n", "Timed out fetching quota data"),
            (" Failed to fetch quota: 401\n", "Failed to fetch quota"),
            ("❭ Ask Devin\n", "drew no Daily or Weekly bar"),
        ],
    )
    def test_no_bar_is_a_quota_error_naming_what_the_screen_said(self, text, needle):
        with pytest.raises(QuotaError) as excinfo:
            devin.parse_usage_screen(text, now=NOW)
        assert needle in str(excinfo.value)

    def test_used_over_100_clamps_to_empty(self):
        screen = SCREEN.replace("66% used", "104% used")
        assert devin.parse_usage_screen(screen, now=NOW)["buckets"][0]["remaining_fraction"] == 0.0

    def test_escapes_are_stripped(self):
        raw = "\x1b[2J\x1b[1;1H\x1b[38;5;214m Daily \x1b[0m■■ 5% used · resets in 2h\x1b[K\r".encode()
        text = devin.screen_text(raw)
        assert "\x1b" not in text
        assert devin.parse_usage_screen(text, now=NOW)["buckets"][0]["remaining_fraction"] == pytest.approx(0.95)


class TestResetTime:
    def test_relative_counts_from_now(self):
        assert devin.reset_time("in 1d 2h 3m", now=NOW) == "2026-10-18T16:03:00+00:00"
        assert devin.reset_time("in 45m", now=NOW) == "2026-10-17T14:45:00+00:00"

    def test_a_named_date_takes_its_offset(self):
        assert devin.reset_time("Oct 18, 1:00 PM (UTC+5:30)", now=NOW) == "2026-10-18T07:30:00+00:00"
        assert devin.reset_time("Oct 18, 12:00 AM (UTC-7)", now=NOW) == "2026-10-18T07:00:00+00:00"

    def test_a_date_with_no_year_rolls_into_the_next_year(self):
        december = datetime(2026, 12, 30, 12, tzinfo=timezone.utc).timestamp()
        assert devin.reset_time("Jan 2, 1:00 AM (UTC-8)", now=december) == "2027-01-02T09:00:00+00:00"

    def test_a_date_just_past_can_be_last_year(self):
        new_year = datetime(2027, 1, 1, 2, tzinfo=timezone.utc).timestamp()
        assert devin.reset_time("Dec 31, 11:00 PM (UTC+0)", now=new_year) == "2026-12-31T23:00:00+00:00"

    def test_a_date_just_past_stays_in_this_year(self):
        after = datetime(2026, 10, 18, 9, tzinfo=timezone.utc).timestamp()
        assert devin.reset_time("Oct 18, 1:00 AM (UTC-7)", now=after) == "2026-10-18T08:00:00+00:00"

    def test_feb_29_skips_the_years_without_one(self):
        leap = datetime(2028, 2, 28, 12, tzinfo=timezone.utc).timestamp()
        assert devin.reset_time("Feb 29, 1:00 AM (UTC+0)", now=leap) == "2028-02-29T01:00:00+00:00"

    def test_a_day_no_year_has_is_none(self):
        assert devin.reset_time("Feb 30, 1:00 AM (UTC+0)", now=NOW) is None

    def test_an_unknown_form_is_none(self):
        assert devin.reset_time("tomorrow", now=NOW) is None


FAKE = textwrap.dedent(
    """\
    import json, os, sys, termios, time, tty
    record = os.environ["FAKE_DEVIN_RECORD"]
    mode = os.environ.get("FAKE_DEVIN_MODE", "ok")
    with open(record, "w", encoding="utf-8") as fh:
        json.dump({"argv": sys.argv[1:], "cwd": os.getcwd(), "pid": os.getpid(),
                   "isatty": os.isatty(0)}, fh)
    tty.setraw(0)
    out = sys.stdout
    if mode != "silent":
        out.write("\\x1b[2J\\x1b[H  v3000.10.67 \\u00b7 Pro \\u00b7 34% remaining\\r\\n")
        out.write("\\x1b[1m\\u276d\\x1b[0m Ask Devin to build features\\r\\n")
        out.flush()
    typed = ""
    while True:
        ch = os.read(0, 1).decode()
        if ch == "\\r":
            with open(record + ".typed", "w", encoding="utf-8") as fh:
                fh.write(typed)
            if typed == "/usage" and mode == "ok":
                out.write(" Fetching quota\\u2026\\r\\n")
                out.flush()
                time.sleep(0.3)
                out.write(" \\x1b[33mDaily\\x1b[0m   \\u25a0\\u25a0  66% used  \\u00b7 resets in 6h 47m\\r\\n")
                out.write(" Weekly  \\u25a0\\u25a0  63% used  \\u00b7 resets Oct 18, 1:00 AM (UTC-7)\\r\\n")
            elif mode == "nodata":
                out.write(" No quota data available.\\r\\n")
            out.flush()
            typed = ""
        else:
            typed += ch
    """
)


@pytest.fixture
def fake_devin(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "devin"
    script.write_text(f"#!{sys.executable}\n{FAKE}", encoding="utf-8")
    script.chmod(0o755)
    record = tmp_path / "record.json"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_DEVIN_RECORD", str(record))
    return record


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class TestTerminal:
    def test_reads_the_bars_through_a_real_pty(self, fake_devin, tmp_path):
        result = devin.quota()
        assert [b["label"] for b in result["buckets"]] == ["Daily Limit", "Weekly Limit"]
        assert result["buckets"][0]["remaining_fraction"] == pytest.approx(0.34)
        record = json.loads(fake_devin.read_text(encoding="utf-8"))
        assert record["argv"] == ["--respect-workspace-trust", "false"]
        assert record["isatty"] is True
        assert Path(record["cwd"]).name.startswith("r4t-devin-quota-")
        assert not Path(record["cwd"]).exists()
        assert Path(str(fake_devin) + ".typed").read_text(encoding="utf-8") == "/usage"

    def test_the_process_group_is_ended(self, fake_devin):
        devin.quota()
        pid = json.loads(fake_devin.read_text(encoding="utf-8"))["pid"]
        deadline = time.monotonic() + 3
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(pid)

    def test_through_the_dispatcher(self, fake_devin, monkeypatch, tmp_path):
        monkeypatch.setenv("R4T_HOME", str(tmp_path / "r4t"))
        result = engines.quota("devin")
        assert result["buckets"][1]["label"] == "Weekly Limit"

    def test_no_data_is_named(self, fake_devin, monkeypatch):
        monkeypatch.setenv("FAKE_DEVIN_MODE", "nodata")
        with pytest.raises(QuotaError) as excinfo:
            devin.quota()
        assert "No quota data available" in str(excinfo.value)

    def test_no_prompt_times_out_and_still_ends_the_process(self, fake_devin, monkeypatch):
        monkeypatch.setenv("FAKE_DEVIN_MODE", "silent")
        monkeypatch.setattr(devin, "BOOT_TIMEOUT_S", 1)
        with pytest.raises(QuotaError) as excinfo:
            devin.quota()
        assert "did not appear" in str(excinfo.value)
        pid = json.loads(fake_devin.read_text(encoding="utf-8"))["pid"]
        deadline = time.monotonic() + 3
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(pid)

    def test_missing_binary(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PATH", str(tmp_path))
        with pytest.raises(QuotaError) as excinfo:
            devin.quota()
        assert "not on PATH" in str(excinfo.value)


def test_a_platform_without_pty_refuses(monkeypatch):
    real_import = builtins.__import__

    def no_pty(name, *args, **kwargs):
        if name == "pty":
            raise ImportError("no pty")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pty)
    with pytest.raises(QuotaError) as excinfo:
        devin.quota()
    assert "pseudo-terminal" in str(excinfo.value)
