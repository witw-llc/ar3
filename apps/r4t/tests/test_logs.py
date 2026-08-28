"""r4t logs — the roster's own event stream, compact by default."""
from __future__ import annotations

import re
from datetime import datetime, timezone

import state
from r4t import main as r4t_main

NODE = "acme"


def seed_log():
    state.append_log(NODE, "## 2026-07-11T10:00:00Z dispatch gerry -> Phil (task 01X hop 1, rig junior-dev)")
    state.append_log(NODE, "### Prompt\n\nYou are Phil, a member of the acme roster.\nSecret persona details here.")
    state.append_log(NODE, "### Output (Phil, exit 0 in 2.0s)")
    state.append_log(NODE, "r4t: RELEASED-internal acme:phil -> acme:gerry thread=01X hop=2")
    state.append_log(NODE, "r4t: SUPPRESSED acme:phil -> acme:gerry thread=01X repeat=2")


def run_logs(*args):
    return r4t_main(["logs", "--node", NODE, *args])


def test_compact_shows_events_hides_transcripts(r4t_home, capsys):
    seed_log()
    assert run_logs() == 0
    out = capsys.readouterr().out
    assert "turn: gerry -> Phil (task 01X hop 1, rig junior-dev)" in out
    assert "done: Phil, exit 0 in 2.0s" in out
    assert "r4t: RELEASED-internal acme:phil -> acme:gerry thread=01X hop=2" in out
    assert "r4t: SUPPRESSED" in out
    assert "Secret persona" not in out
    assert "### Prompt" not in out


def test_full_shows_raw_log(r4t_home, capsys):
    seed_log()
    assert run_logs("--full") == 0
    out = capsys.readouterr().out
    assert "Secret persona details here." in out
    assert "### Prompt" in out


def test_lines_limits_backfill(r4t_home, capsys):
    """The tail counts events, not headers: -n 1 gives one event, and the day
    header still rides above it so the reader knows which day survived."""
    seed_log()
    assert run_logs("-n", "1") == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out[0].startswith("— log day ")
    assert out[1:] == ["r4t: SUPPRESSED acme:phil -> acme:gerry thread=01X repeat=2"]


def test_day_header_names_the_utc_day_and_the_local_zone(r4t_home, zone, capsys):
    """The file is named in UTC because its name is a sort key; the reader is
    somewhere else. Near midnight the two name different days, so both are
    stated rather than one silently standing for the other."""
    # A zone with no daylight saving, so the expected label is the same in
    # every month this suite is ever run in.
    zone("Asia/Kolkata")
    seed_log()
    assert run_logs() == 0
    header = capsys.readouterr().out.splitlines()[0]
    utc_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert header == f"— log day {utc_day} UTC (this machine reads IST)"


def test_status_states_the_zone_the_roster_speaks(r4t_home, repo, zone, capsys):
    """Every prompt this node composes names a zone; `r4t status` names the
    same one, so an operator reading a member's "tomorrow" knows which
    midnight it meant."""
    state.stamp_root(NODE, repo)
    zone("Asia/Kolkata")
    assert r4t_main(["status", "--node", NODE]) == 0
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("time: "))
    assert re.fullmatch(r"time: \d{4}-\d{2}-\d{2} \d{2}:\d{2} IST", line)


def test_no_log_yet(r4t_home, capsys):
    assert run_logs() == 0
    err = capsys.readouterr().err
    assert "no log yet" in err


def seed_two():
    state.append_log(NODE, "## 2026-07-11T10:00:00Z dispatch gerry -> Phil (task 01X hop 1, rig junior-dev)")
    state.append_log(NODE, "### Output (Phil, exit 0 in 2.0s)")
    state.append_log(NODE, "r4t: RESTING gerry — resting (member budget 0)")
    state.append_log(NODE, "r4t: RELEASED-internal acme:phil -> acme:gerry thread=01X hop=2")


def test_agent_filters_to_one_member(r4t_home, repo, capsys):
    state.stamp_root(NODE, repo)
    seed_two()
    assert run_logs("--agent", "phil") == 0
    out = capsys.readouterr().out
    assert "done: Phil, exit 0 in 2.0s" in out
    assert "acme:phil -> acme:gerry" in out
    assert "RESTING gerry" not in out


def test_agent_excludes_other_members(r4t_home, repo, capsys):
    state.stamp_root(NODE, repo)
    seed_two()
    assert run_logs("--agent", "gerry") == 0
    out = capsys.readouterr().out
    assert "RESTING gerry" in out
    assert "done: Phil" not in out


def test_agent_is_case_insensitive(r4t_home, repo, capsys):
    state.stamp_root(NODE, repo)
    seed_two()
    assert run_logs("--agent", "PHIL") == 0
    assert "done: Phil" in capsys.readouterr().out


def test_agent_unknown_member_errors(r4t_home, repo, capsys):
    state.stamp_root(NODE, repo)
    seed_two()
    assert run_logs("--agent", "nobody") == 2
    err = capsys.readouterr().err
    assert "no roster member named 'nobody'" in err
    assert "Phil" in err and "Gerry" in err
    assert "(try:" in err


def test_agent_full_prints_captured_turns_newest_last(r4t_home, repo, capsys):
    state.stamp_root(NODE, repo)
    state.write_turn_capture(
        NODE, "phil", "00000000000000000001", "01X",
        "# turn one\n\n## Prompt\n\nFIRST PROMPT\n\n## Output\n\nFIRST OUTPUT\n",
    )
    state.write_turn_capture(
        NODE, "phil", "00000000000000000002", "02Y",
        "# turn two\n\n## Prompt\n\nSECOND PROMPT\n\n## Output\n\nSECOND OUTPUT\n",
    )
    assert run_logs("--agent", "phil", "--full") == 0
    out = capsys.readouterr().out
    assert "FIRST PROMPT" in out and "SECOND OUTPUT" in out
    assert "=====" in out
    # Newest last: turn two appears after turn one.
    assert out.index("FIRST PROMPT") < out.index("SECOND PROMPT")


def test_agent_full_no_turns_yet(r4t_home, repo, capsys):
    state.stamp_root(NODE, repo)
    assert run_logs("--agent", "phil", "--full") == 0
    assert "no captured turns yet" in capsys.readouterr().err


# ---------- several members, or a whole cell ----------

CELL_ROSTER = """\
# Roster

### Gerry
- **Rig:** leader
- **Cell:** core
- **Leader:** yes

### Phil
- **Rig:** junior-dev
- **Cell:** core

### Vela
- **Rig:** junior-dev
- **Cell:** other
"""


def seed_three():
    state.append_log(NODE, "r4t: RESTING gerry — resting (member budget 0)")
    state.append_log(
        NODE, 'r4t: QUEUED acme:gerry -> acme:phil thread=T hop=0 "hi" (depth 1)'
    )
    state.append_log(NODE, "r4t: RESTING vela — resting (member budget 0)")


def test_agent_repeated_filters_to_several_members(r4t_home, repo, capsys):
    (repo / "ROSTER.md").write_text(CELL_ROSTER, encoding="utf-8")
    state.stamp_root(NODE, repo)
    seed_three()
    assert run_logs("--agent", "gerry", "--agent", "phil") == 0
    out = capsys.readouterr().out
    assert "RESTING gerry" in out
    assert "acme:gerry -> acme:phil" in out
    assert "RESTING vela" not in out


def test_cell_filters_to_every_member_in_that_cell(r4t_home, repo, capsys):
    (repo / "ROSTER.md").write_text(CELL_ROSTER, encoding="utf-8")
    state.stamp_root(NODE, repo)
    seed_three()
    assert run_logs("--cell", "core") == 0
    out = capsys.readouterr().out
    assert "RESTING gerry" in out
    assert "acme:gerry -> acme:phil" in out
    assert "RESTING vela" not in out


def test_cell_is_case_insensitive(r4t_home, repo, capsys):
    (repo / "ROSTER.md").write_text(CELL_ROSTER, encoding="utf-8")
    state.stamp_root(NODE, repo)
    seed_three()
    assert run_logs("--cell", "OTHER") == 0
    out = capsys.readouterr().out
    assert "RESTING vela" in out
    assert "RESTING gerry" not in out


def test_cell_unknown_errors(r4t_home, repo, capsys):
    (repo / "ROSTER.md").write_text(CELL_ROSTER, encoding="utf-8")
    state.stamp_root(NODE, repo)
    seed_three()
    assert run_logs("--cell", "nope") == 2
    err = capsys.readouterr().err
    assert "no roster members in cell 'nope'" in err


def test_agent_and_cell_combine_without_duplicates(r4t_home, repo, capsys):
    (repo / "ROSTER.md").write_text(CELL_ROSTER, encoding="utf-8")
    state.stamp_root(NODE, repo)
    seed_three()
    assert run_logs("--agent", "gerry", "--cell", "core") == 0
    out = capsys.readouterr().out.splitlines()
    assert out.count("r4t: RESTING gerry — resting (member budget 0)") == 1
