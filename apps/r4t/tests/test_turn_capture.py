"""Per-member turn capture — one markdown file per turn under agents/<m>/turns/."""
from __future__ import annotations

import state
from dispatch import drain, handle_message, run_harness, run_idle

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


def name_people(ctx, line="People: neil-phone, neil-email"):
    """Give the test roster its roster-level `People:` line, in the preamble
    above the first member block."""
    path = ctx.root / "ROSTER.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            "Preamble prose that is not a member block.",
            f"Preamble prose that is not a member block.\n\n{line}",
        ),
        encoding="utf-8",
    )


def test_a_persons_messages_are_named_in_the_capture(ctx, fake_harness):
    """The prompt renders no class, so the capture is the only place the later
    distill can learn that a person — not machinery — said this. It decides
    whether the turn may supersede what the prompt recalled."""
    name_people(ctx)
    handle_message(ctx, "neil-phone", "acme:phil", "that bug is closed, drop it")
    text = captures()[0].read_text(encoding="utf-8")
    assert "## Human messages" in text
    assert "### From: neil-phone (thread " in text
    assert "that bug is closed, drop it" in text
    # Above the prompt, so a body carrying its own markdown cannot bury it.
    assert text.index("## Human messages") < text.index("\n## Prompt\n\n")


def test_machine_traffic_gets_no_human_section(ctx, fake_harness):
    name_people(ctx)
    handle_message(
        ctx, "neil-phone", "acme:phil", "that bug is closed, drop it", klass="auto"
    )
    text = captures()[0].read_text(encoding="utf-8")
    assert "## Human messages" not in text
    assert "that bug is closed, drop it" in text


def test_a_sender_the_roster_never_named_is_not_a_person(ctx, fake_harness):
    """a8s stamps no class, so an outside seat's mail arrives `human` on the
    wire's silence alone. Only the roster says whose word that is."""
    name_people(ctx)
    handle_message(ctx, "peer-bot", "acme:phil", "that bug is closed, drop it")
    text = captures()[0].read_text(encoding="utf-8")
    assert "## Human messages" not in text
    assert "that bug is closed, drop it" in text


def test_without_a_people_line_nobody_is_a_person(ctx, fake_harness):
    """Fail closed: a roster that names nobody writes no section, and the
    distill that reads the capture retires nothing."""
    handle_message(ctx, "neil-phone", "acme:phil", "that bug is closed, drop it")
    text = captures()[0].read_text(encoding="utf-8")
    assert "## Human messages" not in text
    assert "that bug is closed, drop it" in text


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
        f.read_text(encoding="utf-8") for f in (state.roster_dir(NODE) / "log").glob("*.md")
    )
    assert "WARN turn capture" in log


def fire_review(ctx):
    """Two idle passes: the first climbs the stall ladder, the second fires
    the review. The wall-clock floor is cleared between them so the ladder is
    the only gate under test."""
    review = {}
    for _ in range(2):
        st = state.read_mission_review(NODE)
        if st:
            st.pop("last_review_at", None)
            state.write_mission_review(NODE, st)
        review = run_idle(ctx)["mission_review"]
    return review


def test_a_review_that_delegates_names_who_it_messaged(
    chatty_ctx, chatty_harness, monkeypatch
):
    """#267: a review that hands work out did so on its own reading of the
    mission and whatever it went looking through. One line says it happened
    and to whom, so an operator finds the turn without reading transcripts."""
    monkeypatch.setenv("CHATTY_TO", "phil")
    assert fire_review(chatty_ctx)["fired"] is True
    text = captures("gerry")[-1].read_text(encoding="utf-8")
    assert "- delegated: phil" in text
    assert text.index("- delegated:") < text.index("\n## Prompt\n\n")


def test_a_review_that_delegates_nothing_names_nobody(
    chatty_ctx, chatty_harness, monkeypatch
):
    """"Nothing to delegate" is now a finding the prompt asks for, and the
    capture records it by carrying no line at all."""
    monkeypatch.setenv("CHATTY_SENDS", "0")
    assert fire_review(chatty_ctx)["fired"] is True
    text = captures("gerry")[-1].read_text(encoding="utf-8")
    assert "- delegated:" not in text


def test_an_ordinary_turn_that_sends_mail_is_not_a_delegation(
    chatty_ctx, chatty_harness, monkeypatch
):
    """Only a review is a turn nobody asked for. An ordinary turn's outbound
    mail answers the messages named in the same header."""
    monkeypatch.setenv("CHATTY_TO", "gerry")
    handle_message(chatty_ctx, "acme:gerry", "acme:phil", "report status")
    text = captures()[-1].read_text(encoding="utf-8")
    assert "- delegated:" not in text


def test_the_capture_names_the_root_the_turn_ran_in(chatty_ctx, chatty_harness):
    """The distill reads it to tell a path the turn worked on from a path the
    model merely mentioned."""
    handle_message(chatty_ctx, "acme:gerry", "acme:phil", "report status")
    text = captures()[-1].read_text(encoding="utf-8")
    assert f"- root: {chatty_ctx.workplace.resolve()}" in text


def test_the_capture_names_the_ids_the_member_was_never_shown(ctx, fake_harness):
    """The member's own section carries titles and ages only, so the packer is
    the sole record of which entries the prompt held. `- knowledge:` is where
    that record lands, and the dream pass reads it to ask whether the turn's
    people contradicted any of them."""
    import knowledge

    path = ctx.root / "ROSTER.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "### Phil\n- **Rig:** junior-dev\n",
            "### Phil\n- **Rig:** junior-dev\n- **Knowledge:** on\n",
        ),
        encoding="utf-8",
    )
    home = knowledge.store_home(NODE, "Phil")
    stored = knowledge._run_k7e(
        home, "store", "Deploy key limits",
        "--content", "The deploy key cannot push workflow files.",
    )
    assert stored.returncode == 0, stored.stderr
    node_id = stored.stdout.split()[1].rstrip(":")

    handle_message(ctx, "acme:gerry", "acme:phil", "the deploy key problem again")
    text = captures()[-1].read_text(encoding="utf-8")
    assert f"- knowledge: {node_id}" in text
    assert node_id not in text.split("\n## Output\n")[0].split("\n## Prompt\n")[1]


def test_a_root_with_spaces_is_written_verbatim_on_one_line(ctx, fake_harness):
    """The distill reads the whole line value as the root, so a workdir whose
    name has spaces in it must reach the capture unsplit and unquoted — a root
    the reader truncates matches nothing under itself, and the turn records no
    sources at all."""
    spaced = ctx.root / "Project With Spaces"
    spaced.mkdir()
    path = ctx.root / "ROSTER.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "### Phil\n- **Rig:** junior-dev\n",
            f"### Phil\n- **Rig:** junior-dev\n- **Workdir:** {spaced}\n",
        ),
        encoding="utf-8",
    )
    handle_message(ctx, "acme:gerry", "acme:phil", "report status")
    text = captures()[-1].read_text(encoding="utf-8")
    assert f"\n- root: {spaced.resolve()}\n" in text
