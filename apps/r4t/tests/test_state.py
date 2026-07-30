from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import state
from state import (
    AgentLock,
    CELL_BUDGET_KEY,
    append_history,
    budget_charge,
    budget_level,
    budget_seconds_until,
    claim_queue,
    count_rig_locks,
    enqueue,
    fmt_budget,
    history_path,
    list_dead_letters,
    list_queue,
    live_locks,
    members_with_queue,
    prepare_staging,
    prune_stale_locks,
    queue_depth,
    read_history,
    read_queue,
    record_dead_letter,
    record_velocity,
    staged_envelopes,
    team_dir,
)

NODE = "acme"


class TestHistory:
    def test_append_creates(self, r4t_home):
        append_history(NODE, "Phil", "## turn 1\n\nhello")
        assert read_history(NODE, "phil").startswith("## turn 1")

    def test_appends_in_order(self, r4t_home):
        append_history(NODE, "phil", "## turn 1\n\na")
        append_history(NODE, "phil", "## turn 2\n\nb")
        text = read_history(NODE, "phil")
        assert text.index("turn 1") < text.index("turn 2")

    def test_truncates_oldest_first_at_boundaries(self, r4t_home):
        for i in range(10):
            append_history(NODE, "phil", f"## turn {i}\n\n{'x' * 300}", max_bytes=1000)
        text = read_history(NODE, "phil")
        assert len(text.encode("utf-8")) <= 1000
        assert "turn 9" in text
        assert "turn 0" not in text
        assert text.startswith("## ")

    def test_single_oversize_entry_survives(self, r4t_home):
        append_history(NODE, "phil", "## big\n\n" + "y" * 2000, max_bytes=1000)
        assert "## big" in read_history(NODE, "phil")

    def test_home_relocates_with_env(self, r4t_home):
        append_history(NODE, "phil", "## x\n\nhi")
        assert history_path(NODE, "phil").is_relative_to(r4t_home)


class TestHome:
    def test_default_is_xdg_config(self, tmp_path, monkeypatch):
        monkeypatch.delenv("R4T_HOME", raising=False)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert state.r4t_home() == tmp_path / ".config" / "r4t"

    def test_xdg_config_home_respected(self, tmp_path, monkeypatch):
        monkeypatch.delenv("R4T_HOME", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert state.r4t_home() == tmp_path / "xdg" / "r4t"

    def test_r4t_home_env_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("R4T_HOME", str(tmp_path / "custom"))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert state.r4t_home() == tmp_path / "custom"


class TestLocks:
    def test_acquire_release(self, r4t_home):
        lock = AgentLock(NODE, "phil")
        assert lock.acquire("junior-dev")
        assert not AgentLock(NODE, "phil").acquire("junior-dev")
        lock.release()
        assert AgentLock(NODE, "phil").acquire("junior-dev")

    def test_stale_lock_is_stolen(self, r4t_home):
        path = state.agent_dir(NODE, "phil") / ".lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": 99999999, "rig": "t"}), encoding="utf-8")
        assert AgentLock(NODE, "phil").acquire("junior-dev")

    def test_live_locks_and_rig_count(self, r4t_home):
        AgentLock(NODE, "phil").acquire("junior-dev")
        AgentLock(NODE, "marcus").acquire("junior-dev")
        AgentLock(NODE, "gerry").acquire("leader")
        assert len(live_locks(NODE)) == 3
        assert count_rig_locks(NODE, "junior-dev") == 2
        assert count_rig_locks(NODE, "LEADER") == 1

    def test_prune_stale_locks(self, r4t_home):
        AgentLock(NODE, "gerry").acquire("leader")
        dead = state.agent_dir(NODE, "phil") / ".lock"
        dead.parent.mkdir(parents=True, exist_ok=True)
        dead.write_text(json.dumps({"pid": 99999999, "rig": "t"}), encoding="utf-8")
        corrupt = state.agent_dir(NODE, "zoe") / ".lock"
        corrupt.parent.mkdir(parents=True, exist_ok=True)
        corrupt.write_text("not json", encoding="utf-8")
        assert prune_stale_locks(NODE) == 2
        assert not dead.exists()
        assert not corrupt.exists()
        assert len(live_locks(NODE)) == 1


class TestQueue:
    def test_enqueue_and_read_in_arrival_order(self, r4t_home):
        enqueue(NODE, "phil", {"from": "gerry", "body": "one", "thread": "t1"})
        enqueue(NODE, "phil", {"from": "marcus", "body": "two", "thread": "t2"})
        bodies = [e["body"] for e in read_queue(NODE, "phil")]
        assert bodies == ["one", "two"]
        assert queue_depth(NODE, "phil") == 2

    def test_duplicate_collapse_bumps_repeats(self, r4t_home):
        enqueue(NODE, "phil", {"from": "gerry", "body": "Deploy the  fix"})
        enqueue(NODE, "phil", {"from": "GERRY", "body": "deploy   the fix\n"})
        queue = read_queue(NODE, "phil")
        assert len(queue) == 1
        assert queue[0]["repeats"] == 2

    def test_collapse_only_against_newest(self, r4t_home):
        enqueue(NODE, "phil", {"from": "gerry", "body": "same"})
        enqueue(NODE, "phil", {"from": "marcus", "body": "different"})
        enqueue(NODE, "phil", {"from": "gerry", "body": "same"})
        # newest is marcus/"different", so gerry/"same" is a fresh entry
        assert queue_depth(NODE, "phil") == 3

    def test_different_sender_does_not_collapse(self, r4t_home):
        enqueue(NODE, "phil", {"from": "gerry", "body": "same"})
        enqueue(NODE, "phil", {"from": "marcus", "body": "same"})
        assert queue_depth(NODE, "phil") == 2

    def test_claim_removes_and_returns_all(self, r4t_home):
        enqueue(NODE, "phil", {"from": "gerry", "body": "one"})
        enqueue(NODE, "phil", {"from": "gerry-2", "body": "two"})
        claimed = claim_queue(NODE, "phil")
        assert [c["body"] for c in claimed] == ["one", "two"]
        assert queue_depth(NODE, "phil") == 0
        assert list_queue(NODE, "phil") == []

    def test_members_with_queue(self, r4t_home):
        enqueue(NODE, "phil", {"from": "gerry", "body": "x"})
        enqueue(NODE, "gerry", {"from": "neil", "body": "y"})
        assert members_with_queue(NODE) == ["gerry", "phil"]


class TestVelocity:
    def test_rows_and_header(self, r4t_home):
        record_velocity(
            NODE,
            agent="phil",
            rig="junior-dev",
            thread="01ABC",
            hop=1,
            duration_seconds=1.234,
            exit_code=0,
        )
        record_velocity(
            NODE,
            agent="gerry",
            rig="leader",
            thread="01DEF",
            hop=0,
            duration_seconds=2.0,
            exit_code=1,
        )
        lines = (team_dir(NODE) / "velocity.csv").read_text().splitlines()
        assert lines[0] == state.VELOCITY_HEADER.strip()
        assert len(lines) == 3
        assert "phil,junior-dev,01ABC,1,1.23,0" in lines[1]

    def test_field_quoting(self, r4t_home):
        record_velocity(
            NODE,
            agent='we,"ird',
            rig="t",
            thread="x",
            hop=0,
            duration_seconds=0,
            exit_code=0,
        )
        lines = (team_dir(NODE) / "velocity.csv").read_text().splitlines()
        assert '"we,""ird"' in lines[1]


class TestLogRetention:
    def _days(self, offsets: list[int]) -> list[str]:
        today = datetime.now(timezone.utc)
        log_dir = team_dir(NODE) / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        names = []
        for offset in offsets:
            day = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
            (log_dir / f"{day}.md").write_text("turn\n", encoding="utf-8")
            names.append(day)
        return names

    def test_keeps_the_window_and_drops_older_days(self, r4t_home):
        today, yesterday, old, older = self._days([0, 1, 2, 30])
        assert state.prune_day_logs(NODE, 2) == [older, old]
        kept = sorted(p.stem for p in (team_dir(NODE) / "log").glob("*.md"))
        assert kept == sorted([yesterday, today])

    def test_zero_keeps_everything(self, r4t_home):
        self._days([0, 400])
        assert state.prune_day_logs(NODE, 0) == []
        assert len(list((team_dir(NODE) / "log").glob("*.md"))) == 2

    def test_non_day_files_are_left_alone(self, r4t_home):
        self._days([90])
        (team_dir(NODE) / "log" / "notes.md").write_text("keep me", encoding="utf-8")
        assert len(state.prune_day_logs(NODE, 1)) == 1
        assert (team_dir(NODE) / "log" / "notes.md").is_file()

    def test_no_log_dir_is_a_no_op(self, r4t_home):
        assert state.prune_day_logs(NODE, 14) == []


class TestVelocityRotation:
    def _rows(self, stamps: list[str]) -> None:
        path = team_dir(NODE) / "velocity.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = "".join(f"{s},phil,junior-dev,01ABC,0,1.00,0\n" for s in stamps)
        path.write_text(state.VELOCITY_HEADER + rows, encoding="utf-8")

    def _now_month(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def test_finished_months_move_out_current_month_stays(self, r4t_home):
        live = f"{self._now_month()}-01T00:00:00Z"
        self._rows(["2020-01-05T00:00:00Z", "2020-02-05T00:00:00Z", live])
        assert state.rotate_velocity(NODE) == ["2020-01", "2020-02"]
        remaining = (team_dir(NODE) / "velocity.csv").read_text().splitlines()
        assert remaining[0] == state.VELOCITY_HEADER.strip()
        assert len(remaining) == 2 and live in remaining[1]
        archived = (team_dir(NODE) / "velocity-2020-01.csv").read_text().splitlines()
        assert archived[0] == state.VELOCITY_HEADER.strip()
        assert len(archived) == 2 and "2020-01-05" in archived[1]

    def test_appends_to_an_existing_archive(self, r4t_home):
        self._rows(["2020-01-05T00:00:00Z"])
        state.rotate_velocity(NODE)
        self._rows(["2020-01-09T00:00:00Z"])
        assert state.rotate_velocity(NODE) == ["2020-01"]
        archived = (team_dir(NODE) / "velocity-2020-01.csv").read_text().splitlines()
        assert len(archived) == 3

    def test_current_month_only_is_a_no_op(self, r4t_home):
        self._rows([f"{self._now_month()}-01T00:00:00Z"])
        before = (team_dir(NODE) / "velocity.csv").read_text()
        assert state.rotate_velocity(NODE) == []
        assert (team_dir(NODE) / "velocity.csv").read_text() == before

    def test_no_file_is_a_no_op(self, r4t_home):
        assert state.rotate_velocity(NODE) == []


class TestDeadLetters:
    def test_record_and_list(self, r4t_home):
        record_dead_letter(
            NODE, reason="quota", sender="acme:phil", to="gerry",
            thread="01ABC", content="too chatty",
        )
        record_dead_letter(
            NODE, reason="pair-repeat", sender="acme:phil", to="gerry",
            thread="01ABC", content="again", count=3,
        )
        records = list_dead_letters(NODE)
        assert len(records) == 2
        by_reason = {r["reason"]: r for r in records}
        assert by_reason["quota"]["from"] == "acme:phil"
        assert by_reason["quota"]["count"] == 1
        assert by_reason["pair-repeat"]["count"] == 3
        assert all(r["time"] for r in records)

    def test_content_capped(self, r4t_home):
        record_dead_letter(
            NODE, reason="quota", sender="a", to="b", thread="t", content="x" * 5000,
        )
        assert len(list_dead_letters(NODE)[0]["content"]) == 2000


class TestBudgets:
    def test_starts_full(self, r4t_home):
        assert budget_level(NODE, "phil", 8.0, 4.0) == 8.0

    def test_charge_deducts_and_floors_at_zero(self, r4t_home):
        now = 1_000_000.0
        assert budget_charge(NODE, "phil", 8.0, 0.0, 3.0, now=now) == 5.0
        assert budget_charge(NODE, "phil", 8.0, 0.0, 100.0, now=now) == 0.0

    def test_a_turn_costs_one_unit(self, r4t_home):
        now = 1_000_000.0
        assert budget_charge(NODE, "phil", 8.0, 0.0, now=now) == 7.0
        assert budget_charge(NODE, "phil", 8.0, 0.0, now=now) == 6.0

    def test_earns_back_over_time_capped_at_max(self, r4t_home):
        start = 1_000_000.0
        budget_charge(NODE, "phil", 8.0, 4.0, 8.0, now=start)  # empty
        # 4 units/hour: after 30 min the bucket holds 2
        assert budget_level(NODE, "phil", 8.0, 4.0, now=start + 1800) == 2.0
        # after a long while it caps at the max
        assert budget_level(NODE, "phil", 8.0, 4.0, now=start + 100000) == 8.0

    def test_seconds_until_ready(self, r4t_home):
        start = 1_000_000.0
        budget_charge(NODE, "phil", 8.0, 4.0, 8.0, now=start)  # empty
        # needs 1 unit at 4/hour -> 900s
        assert budget_seconds_until(NODE, "phil", 8.0, 4.0, now=start) == 900.0
        # already ≥1 -> 0
        budget_charge(NODE, "phil", 8.0, 4.0, 0.0, now=start + 3600)
        assert budget_seconds_until(NODE, "phil", 8.0, 4.0, now=start + 3600) == 0.0

    def test_cell_bucket_is_independent(self, r4t_home):
        now = 1_000_000.0
        budget_charge(NODE, CELL_BUDGET_KEY, 16.0, 0.0, 5.0, now=now)
        assert budget_level(NODE, CELL_BUDGET_KEY, 16.0, 0.0, now=now) == 11.0
        assert budget_level(NODE, "phil", 8.0, 0.0, now=now) == 8.0


class TestRigBucket:
    def test_lives_at_r4t_home_root_not_under_a_team(self, r4t_home):
        state.rig_budget_charge("agy", 20.0, 0.0, 1.0)
        assert state.rig_buckets_path() == r4t_home / "rig-buckets.json"
        assert state.rig_buckets_path().is_file()

    def test_shared_key_charged_from_anywhere(self, r4t_home):
        now = 1_000_000.0
        assert state.rig_budget_charge("agy", 20.0, 0.0, 1.0, now=now) == 19.0
        assert state.rig_budget_charge("agy", 20.0, 0.0, 1.0, now=now) == 18.0
        assert state.rig_budget_level("agy", 20.0, 0.0, now=now) == 18.0

    def test_drain_empties_the_bucket(self, r4t_home):
        now = 1_000_000.0
        state.rig_budget_drain("agy", now=now)
        assert state.rig_budget_level("agy", 20.0, 0.0, now=now) == 0.0

    def test_refills_over_time(self, r4t_home):
        start = 1_000_000.0
        state.rig_budget_drain("agy", now=start)
        assert state.rig_budget_seconds_until("agy", 20.0, 20.0, now=start) == 180.0
        assert state.rig_budget_level("agy", 20.0, 20.0, now=start + 3600) == 20.0

    def test_concurrent_charge_is_atomic(self, r4t_home):
        # Many threads charge the ONE machine-global bucket at once; the
        # lock-serialized read-modify-write must lose no decrements.
        import threading

        state.rig_budget_charge("agy", 1000.0, 0.0, 0.0)  # seed at full

        def hit():
            for _ in range(20):
                state.rig_budget_charge("agy", 1000.0, 0.0, 1.0)

        threads = [threading.Thread(target=hit) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 10 threads * 20 charges = 200 units spent, exactly.
        assert state.rig_budget_level("agy", 1000.0, 0.0) == 800.0


class TestFmtBudget:
    def test_whole_number_drops_decimal(self):
        assert fmt_budget(8.0) == "8"

    def test_fraction_keeps_one_decimal(self):
        assert fmt_budget(7.5) == "7.5"


class TestStaging:
    def test_prepare_wipes_leftovers(self, r4t_home):
        d = prepare_staging(NODE, "phil")
        (d / "stale.json").write_text("{}", encoding="utf-8")
        d2 = prepare_staging(NODE, "phil")
        assert d2 == d
        assert not list(d.iterdir())

    def test_staged_envelopes_sorted_json_only(self, r4t_home):
        d = prepare_staging(NODE, "phil")
        (d / "002.json").write_text("{}", encoding="utf-8")
        (d / "001.json").write_text("{}", encoding="utf-8")
        (d / "ignore.txt").write_text("", encoding="utf-8")
        (d / "001").mkdir()
        names = [p.name for p in staged_envelopes(NODE, "phil")]
        assert names == ["001.json", "002.json"]


def test_live_log_reset_and_tail(r4t_home):
    path = state.reset_live_log(NODE, "phil")
    assert path.read_text(encoding="utf-8") == ""
    with path.open("a", encoding="utf-8") as f:
        f.write("hello\n")
    chunk, offset = state.read_live_log_tail(NODE, "phil", 0)
    assert chunk == "hello\n" and offset == 6
    assert state.read_live_log_tail(NODE, "phil", offset) == ("", 6)
    # a new turn truncates the file; a stale offset restarts from the top
    state.reset_live_log(NODE, "phil")
    assert state.read_live_log_tail(NODE, "phil", offset) == ("", 0)
