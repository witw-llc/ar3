"""Tests for the shared display clock — ar3's foundation layer.

The suite stores UTC and shows local. These tests pin both halves of that:
a stored stamp reads as the machine's wall time, and the zone label survives
the platforms that spell it differently. Every assertion forces `TZ` to a
pinned zone; none of them depend on where the machine running them is.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone, tzinfo

import pytest

from ar3.clock import local_now, stamp, time_stamp, to_local, zone_label

needs_tzset = pytest.mark.skipif(
    not hasattr(time, "tzset"), reason="TZ only takes effect via tzset (not on Windows)"
)


@pytest.fixture
def zone(monkeypatch):
    """Force the process's local zone, and put it back afterwards."""

    def use(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        time.tzset()

    yield use
    monkeypatch.undo()
    time.tzset()


@needs_tzset
class TestStoredStampReadsLocal:
    def test_utc_stamp_reads_as_pacific(self, zone):
        zone("America/Los_Angeles")
        assert stamp("2026-08-16T20:07:00Z") == "2026-08-16 13:07 PDT"

    def test_same_instant_reads_as_berlin(self, zone):
        zone("Europe/Berlin")
        assert stamp("2026-08-16T20:07:00Z") == "2026-08-16 22:07 CEST"

    def test_same_instant_reads_as_kolkata_half_hour_offset(self, zone):
        zone("Asia/Kolkata")
        assert stamp("2026-08-16T20:07:00Z") == "2026-08-17 01:37 IST"

    def test_utc_machine_still_says_which_zone(self, zone):
        zone("UTC")
        assert stamp("2026-08-16T20:07:00Z") == "2026-08-16 20:07 UTC"

    def test_seconds_are_opt_in(self, zone):
        zone("America/Los_Angeles")
        assert stamp("2026-08-16T20:07:31Z") == "2026-08-16 13:07 PDT"
        assert stamp("2026-08-16T20:07:31Z", seconds=True) == "2026-08-16 13:07:31 PDT"

    def test_time_stamp_drops_the_date(self, zone):
        zone("America/Los_Angeles")
        assert time_stamp("2026-08-16T20:07:31Z") == "13:07:31 PDT"

    def test_a_stored_stamp_can_cross_the_day_boundary(self, zone):
        # The whole point: 06:30 UTC is still the previous evening in Pacific,
        # and an agent told "2026-08-17" would answer for the wrong day.
        zone("America/Los_Angeles")
        assert stamp("2026-08-17T06:30:00Z") == "2026-08-16 23:30 PDT"


@needs_tzset
class TestDaylightSaving:
    def test_winter_is_standard_time(self, zone):
        zone("America/Los_Angeles")
        assert stamp("2026-01-16T20:07:00Z") == "2026-01-16 12:07 PST"

    def test_summer_is_daylight_time(self, zone):
        zone("America/Los_Angeles")
        assert stamp("2026-08-16T20:07:00Z") == "2026-08-16 13:07 PDT"

    def test_the_hour_the_clocks_move(self, zone):
        # 2026-11-01 09:00Z is 02:00 PDT; one hour later the same wall clock
        # reads 01:00 PST. Both must carry the label that disambiguates them.
        zone("America/Los_Angeles")
        assert stamp("2026-11-01T08:59:00Z") == "2026-11-01 01:59 PDT"
        assert stamp("2026-11-01T09:01:00Z") == "2026-11-01 01:01 PST"


@needs_tzset
class TestZoneWithoutAnAbbreviation:
    def test_numeric_abbreviation_renders_as_utc_offset(self, zone):
        # Python gives `+07` for this zone, which reads as noise beside `PDT`.
        zone("Asia/Ho_Chi_Minh")
        assert stamp("2026-08-16T20:07:00Z") == "2026-08-17 03:07 UTC+07:00"

    def test_quarter_hour_offset_keeps_its_minutes(self, zone):
        zone("Australia/Eucla")
        assert zone_label(to_local("2026-08-16T20:07:00Z")) == "UTC+08:45"


class TestWindowsLongZoneName:
    """`%Z` gives `Pacific Daylight Time` on Windows, and the Windows seat is
    live. `stamp` converts to the machine's own zone before labelling it, so
    the long-name branch is reached through `zone_label` — exercised here with
    a hand-built tzinfo, on any platform. The end-to-end proof that `stamp`
    renders the fallback is the `Asia/Ho_Chi_Minh` case above."""

    def _zoned(self, name: str, hours: float) -> datetime:
        class NamedZone(tzinfo):
            def utcoffset(self, dt):
                return timedelta(hours=hours)

            def dst(self, dt):
                return timedelta(0)

            def tzname(self, dt):
                return name

        return datetime(2026, 8, 16, 13, 7, tzinfo=NamedZone())

    def test_long_name_falls_back_to_the_offset(self):
        assert zone_label(self._zoned("Pacific Daylight Time", -7)) == "UTC-07:00"

    def test_a_half_hour_windows_zone_keeps_its_minutes(self):
        assert zone_label(self._zoned("India Standard Time", 5.5)) == "UTC+05:30"

    def test_a_short_alphabetic_name_is_kept(self):
        assert zone_label(self._zoned("CEST", 2)) == "CEST"


@needs_tzset
class TestInputForms:
    def test_naive_stored_stamp_is_read_as_utc(self, zone):
        zone("America/Los_Angeles")
        assert stamp("2026-08-16T20:07:00") == "2026-08-16 13:07 PDT"

    def test_explicit_offset_is_honored(self, zone):
        zone("America/Los_Angeles")
        assert stamp("2026-08-16T22:07:00+02:00") == "2026-08-16 13:07 PDT"

    def test_aware_datetime_passes_through(self, zone):
        zone("America/Los_Angeles")
        dt = datetime(2026, 8, 16, 20, 7, tzinfo=timezone.utc)
        assert stamp(dt) == "2026-08-16 13:07 PDT"

    def test_naive_datetime_is_read_as_utc(self, zone):
        zone("America/Los_Angeles")
        assert stamp(datetime(2026, 8, 16, 20, 7)) == "2026-08-16 13:07 PDT"

    def test_lowercase_z_suffix_parses(self, zone):
        zone("America/Los_Angeles")
        assert stamp("2026-08-16T20:07:00z") == "2026-08-16 13:07 PDT"


class TestUnreadableInput:
    def test_garbage_passes_through_verbatim(self):
        # A display surface must never invent a time it could not read.
        assert stamp("not a timestamp") == "not a timestamp"
        assert time_stamp("not a timestamp") == "not a timestamp"

    def test_to_local_reports_the_failure(self):
        assert to_local("not a timestamp") is None
        assert to_local("") is None
        assert to_local(None) is None


@needs_tzset
class TestNow:
    def test_no_argument_means_now(self, zone):
        zone("America/Los_Angeles")
        text = stamp()
        assert text.endswith((" PDT", " PST"))
        assert len(text.split(" ")[0]) == len("2026-08-16")

    def test_local_now_is_aware(self, zone):
        zone("Europe/Berlin")
        now = local_now()
        assert now.tzinfo is not None
        assert now.utcoffset() is not None

    def test_time_stamp_now_is_bare_time(self, zone):
        zone("Europe/Berlin")
        text = time_stamp()
        assert text.endswith((" CEST", " CET"))
        assert len(text.split(" ")[0]) == len("13:07:31")
