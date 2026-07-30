"""Per-member turn capture — one markdown file per turn under agents/<m>/turns/."""
from __future__ import annotations

import state
from dispatch import drain, handle_message, run_harness

NODE = "acme"


def run_one(ctx, sender, to, message, run_fn=run_harness):
    handle_message(ctx, sender, to, message, run_fn=run_fn, drain_after=False)
    return drain(ctx, run_fn=run_fn)


def timeout_run(rig, prompt, cwd, *, env=None, variant=0):
    return 0, "partial output before the hang", 0.05, True


def captures(name="phil"):
    return state.list_turn_captures(NODE, name)


def test_success_captures_prompt_and_output_verbatim(ctx, fake_harness):
    handle_message(ctx, "acme:gerry", "acme:phil", "please build the widget")
    files = captures()
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "## Prompt" in text
    assert "## Output" in text
    # Prompt is captured verbatim: persona and the inbound body both survive.
    assert "Grumpy, cynical veteran" in text
    assert "please build the widget" in text
    # Raw harness stdout is captured pre-cleaning.
    assert "fake harness ran" in text
    assert "- exit: 0" in text
    assert "- timed_out: false" in text
    assert "- rig: junior-dev" in text


def test_timeout_is_captured_with_partial_output(ctx, fake_harness):
    assert run_one(ctx, "acme:gerry", "acme:phil", "hang please", run_fn=timeout_run) == 1
    files = captures()
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "- timed_out: true" in text
    assert "partial output before the hang" in text


def test_retention_prunes_to_fifty(r4t_home):
    for i in range(state.TURN_RETENTION + 5):
        state.write_turn_capture(
            NODE, "phil", f"{i:020d}", "01X", f"# turn {i}\n\n## Prompt\n\np{i}\n"
        )
    files = captures()
    assert len(files) == state.TURN_RETENTION
    # Newest kept, oldest five pruned.
    assert files[0].name.startswith(f"{5:020d}")
    assert files[-1].name.startswith(f"{state.TURN_RETENTION + 4:020d}")


def test_capture_failure_only_warns(ctx, fake_harness, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(state, "write_turn_capture", boom)
    # The turn must still complete despite the capture write failing.
    handle_message(ctx, "acme:gerry", "acme:phil", "carry on")
    log = "".join(
        f.read_text(encoding="utf-8") for f in (state.team_dir(NODE) / "log").glob("*.md")
    )
    assert "WARN turn capture" in log
