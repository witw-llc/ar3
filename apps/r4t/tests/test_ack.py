"""close_without_reply — the propose/validate/commit protocol (#59)."""
from __future__ import annotations

import json
import re
import time
from dataclasses import replace

import pytest

import ack
import dispatch
import state
import tasks
from dispatch import drain, handle_message
from roster import load_roster
from ulid import new as new_ulid

NODE = "acme"
THREAD = "01J0000000000000000000000A"
PROMPT_THREAD_RE = re.compile(r"\(thread ([0-9A-Z]{26})\)")


def read_log():
    files = (state.roster_dir(NODE) / "log").glob("*.md")
    return "".join(f.read_text(encoding="utf-8") for f in files)


def outbox_envelopes(repo):
    d = repo / ".outbox"
    if not d.is_dir():
        return []
    return [json.loads(f.read_text(encoding="utf-8")) for f in sorted(d.glob("*.json"))]


def proposing(line=None, *, prose="", stated=""):
    """A run_fn whose turn output proposes a close on the thread it was woken
    with — the emission contract as a member actually meets it, read back out
    of the assembled prompt."""

    def run(rig, prompt, cwd, *, env=None, variant=0):
        thread = PROMPT_THREAD_RE.search(prompt).group(1)
        emitted = line.format(thread=thread) if line else (
            f"{ack.VERB} {thread}" + (f" {stated}" if stated else "")
        )
        return 0, ((prose + "\n") if prose else "") + emitted, 1.0, False

    return run


def deliver(ctx, body, *, klass="auto", sender="filedrop", run_fn):
    """One external message to the leader, then one drain pass. The default is
    the eligible shape: machine-classed external mail, so the ledger records a
    relay thread."""
    handle_message(ctx, sender, NODE, body, klass=klass, drain_after=False)
    return drain(ctx, run_fn=run_fn)


def dispatcher_message(ctx, body="The nightly dump ran; nothing needs doing."):
    """A thread in r4t's own voice — machine-originated like a relay, but NOT
    relay-flagged, so the quiet sweep does see it. `dispatcher=True` is the
    dispatcher stamping its own ledger; ingress has no way to pass it."""
    return dispatch._ingest(
        ctx, f"r4t:{NODE}", f"{NODE}:gerry", body, klass="auto", internal=True,
        dispatcher=True,
    )


def member_and_rig(ctx, name="gerry", roster=None):
    roster = roster or load_roster(ctx.roster_path)
    member = roster.find(name)
    rig, _err, _pinned = dispatch.load_rig_config(ctx.config_path).rig_for(member)
    return roster, member, rig


def sweep_nudges(ctx):
    config = replace(
        dispatch.load_rig_config(ctx.config_path), quiet_task_seconds=0.001
    )
    return dispatch._quiet_task_sweep(ctx, config, load_roster(ctx.roster_path))


def quiet(rig, prompt, cwd, *, env=None, variant=0):
    """A turn that says something too short to trigger the stdout fallback."""
    return 0, "noted", 1.0, False


FYI = "Nightly export finished. 412 rows written to the drop."


class TestParse:
    def test_bare_proposal(self):
        proposals, malformed = ack.parse(f"{ack.VERB} {THREAD}")
        assert proposals == [(THREAD, "")]
        assert malformed == []

    def test_stated_reason_is_captured_as_color(self):
        proposals, _ = ack.parse(f"{ack.VERB} {THREAD} duplicate of the earlier drop")
        assert proposals == [(THREAD, "duplicate of the earlier drop")]

    def test_one_proposal_per_line(self):
        other = new_ulid()
        proposals, malformed = ack.parse(
            f"here is what I did\n{ack.VERB} {THREAD}\n{ack.VERB} {other}\n"
        )
        assert [t for t, _ in proposals] == [THREAD, other]
        assert malformed == []

    def test_lowercase_thread_is_normalized(self):
        proposals, _ = ack.parse(f"{ack.VERB} {THREAD.lower()}")
        assert proposals == [(THREAD, "")]

    @pytest.mark.parametrize(
        "verb",
        [
            "CLOSE_WITHOUT_RETRY",  # the corruption a 4B member actually emitted
            "close-without-reply",
            "Close_Without_Reply",
            "`close_without_reply`",
            "close_without_response",
        ],
    )
    def test_near_miss_verbs_are_rejected_not_repaired(self, verb):
        proposals, malformed = ack.parse(f"{verb} {THREAD}")
        assert proposals == []
        assert malformed == [f"{verb} {THREAD}"]

    def test_verb_without_a_thread_is_malformed(self):
        proposals, malformed = ack.parse(ack.VERB)
        assert proposals == []
        assert malformed == [ack.VERB]

    def test_junk_thread_is_malformed(self):
        proposals, malformed = ack.parse(f"{ack.VERB} thread-4")
        assert proposals == []
        assert malformed == [f"{ack.VERB} thread-4"]

    def test_verb_mid_sentence_is_prose(self):
        proposals, malformed = ack.parse(
            f"I considered whether to {ack.VERB} {THREAD} and decided not to."
        )
        assert proposals == [] and malformed == []

    def test_prose_only_output_parses_to_nothing(self):
        proposals, malformed = ack.parse("Done. The export looks fine.\n")
        assert proposals == [] and malformed == []

    def test_a_sentence_opening_with_the_verb_is_prose(self):
        # A member explaining the protocol names it at the head of a line. With
        # no thread-shaped token after it there is nothing to close and nothing
        # to log — the sentence is just the answer someone asked for.
        proposals, malformed = ack.parse(
            "Close_without_reply ends an obligation with no message at all."
        )
        assert proposals == [] and malformed == []

    def test_a_fenced_proposal_is_quotation_not_protocol(self):
        proposals, malformed = ack.parse(
            f"Here is the syntax:\n\n```\n{ack.VERB} {THREAD}\n```\n"
        )
        assert proposals == [] and malformed == []

    def test_a_proposal_after_a_closed_fence_still_counts(self):
        proposals, _ = ack.parse(
            f"```\nexample\n```\n{ack.VERB} {THREAD}"
        )
        assert proposals == [(THREAD, "")]


class TestStripProposals:
    def test_protocol_lines_leave_the_transcript(self):
        text = f"I read the drop.\n{ack.VERB} {THREAD}\nNothing else to add."
        assert ack.strip_proposals(text) == "I read the drop.\nNothing else to add."

    def test_malformed_lines_leave_too(self):
        text = f"I read the drop.\nCLOSE_WITHOUT_RETRY {THREAD}"
        assert ack.strip_proposals(text) == "I read the drop."

    def test_prose_naming_the_verb_survives(self):
        text = "Close_without_reply ends an obligation with no message at all."
        assert ack.strip_proposals(text) == text

    def test_a_fenced_protocol_line_is_stripped_too(self):
        # Parsing honors the fence (nothing closes); stripping does not, so the
        # verb line cannot ride out in a body no matter how it is wrapped.
        text = f"The syntax:\n```\n{ack.VERB} {THREAD}\n```"
        assert ack.strip_proposals(text) == "The syntax:\n```\n```"

    def test_an_unbalanced_fence_cannot_shelter_a_protocol_line(self):
        text = f"the log tail:\n```\nrows=412 ok\n{ack.VERB} {THREAD}\n"
        assert ack.VERB not in ack.strip_proposals(text)

    def test_fenced_prose_survives(self):
        text = "example:\n```\nr4t task list\n```"
        assert ack.strip_proposals(text) == text


class TestDisqualifiers:
    def test_plain_notification_is_eligible(self):
        assert ack.disqualifier([{"body": FYI, "class": "auto"}]) is None

    def test_direct_question_overrides(self):
        why = ack.disqualifier([{"body": "The export ran. Does that look right?"}])
        assert why is not None and "question" in why

    def test_assignment_under_fyi_framing_overrides(self):
        # The single false ack a Sonnet-class member produced in the #59
        # experiments: an assignment dressed as an FYI. Framing loses.
        why = ack.disqualifier([
            {"body": "FYI, just a heads up — please rotate the export key today."}
        ])
        assert why is not None and "assignment" in why

    def test_operational_error_overrides(self):
        why = ack.disqualifier([{"body": "the export died", "class": "error"}])
        assert why is not None and "error" in why

    def test_a_later_message_in_the_batch_can_disqualify(self):
        why = ack.disqualifier([
            {"body": FYI, "class": "auto"},
            {"body": "Can you confirm the row count?", "class": "human"},
        ])
        assert why is not None


class TestAllowList:
    """Eligibility is structural: it reads flags the ledger was born with,
    never the wording of the messages and never the creator's name."""

    def test_relay_thread_is_machine_originated(self):
        assert ack.machine_originated({"relay": True, "creator": "peer"})

    def test_dispatcher_origin_is_machine_originated(self):
        assert ack.machine_originated(
            {"creator": f"r4t:{NODE}", "origin": tasks.ORIGIN_DISPATCHER}
        )

    def test_a_creator_that_merely_looks_like_the_dispatcher_is_not(self):
        # `r4t dispatch --from` takes any string. Without the flag the ledger
        # stamped at open time, the name proves nothing (#83).
        assert not ack.machine_originated({"creator": f"r4t:{NODE}"})

    def test_a_human_thread_is_not(self):
        assert not ack.machine_originated({"creator": "boss"})

    def test_a_peer_members_thread_is_not(self):
        assert not ack.machine_originated({"creator": f"{NODE}:gerry"})


class TestDerivedReason:
    def test_relay_thread_reads_automated(self):
        assert ack.derive_reason({"relay": True, "creator": "peer"}) == (
            ack.REASON_AUTOMATED
        )

    def test_dispatcher_origin_reads_automated(self):
        assert ack.derive_reason({"origin": tasks.ORIGIN_DISPATCHER}) == (
            ack.REASON_AUTOMATED
        )

    def test_everything_else_reads_informational(self):
        assert ack.derive_reason({"creator": "boss"}) == ack.REASON_INFORMATIONAL


class TestDoctrineBullet:
    def test_offered_by_default(self, ctx, r4t_home, fake_harness):
        handle_message(ctx, "boss", NODE, FYI)
        assert ack.VERB in read_log()

    def test_absent_when_the_knob_is_off(self, ctx, repo, r4t_home, fake_harness):
        (repo / "ROSTER.md").write_text(
            "### Gerry\n- **Rig:** leader\n- **Leader:** yes\n- **Ack:** off\n",
            encoding="utf-8",
        )
        handle_message(ctx, "boss", NODE, FYI)
        assert ack.VERB not in read_log()


class TestCommit:
    def test_relay_thread_closes_with_no_message(self, ctx, repo, r4t_home):
        assert deliver(ctx, FYI, run_fn=proposing()) == 1
        (task,) = tasks.list_tasks(NODE)
        assert task["status"] == tasks.STATUS_CLOSED
        assert task["answered"] is True
        assert task["ack"]["member"] == "gerry"
        assert task["ack"]["reason"] == ack.REASON_AUTOMATED
        # The whole point: a closed obligation and not one byte of egress.
        assert outbox_envelopes(repo) == []
        assert state.list_seat_messages(NODE, "neil") == []
        assert state.queue_depth(NODE, "phil") == 0
        log = read_log()
        assert f"r4t: ACK thread={task['id']} gerry reason=" in log
        assert "STDOUT-REPLY" not in log

    def test_prose_alongside_a_close_stays_transcript(self, ctx, repo, r4t_home):
        long_prose = "I read the export summary and filed it. " * 5
        deliver(ctx, FYI, run_fn=proposing(prose=long_prose))
        assert outbox_envelopes(repo) == []
        log = read_log()
        assert "r4t: ACK-QUIET gerry" in log
        assert "STDOUT-REPLY" not in log

    def test_stated_reason_is_recorded_but_not_believed(self, ctx, r4t_home):
        deliver(ctx, FYI, run_fn=proposing(stated="reason=duplicate"))
        (task,) = tasks.list_tasks(NODE)
        assert task["ack"]["stated"] == "reason=duplicate"
        assert task["ack"]["reason"] == ack.REASON_AUTOMATED

    def test_a_thread_r4t_opened_closes(self, ctx, repo, r4t_home):
        dispatcher_message(ctx)
        drain(ctx, run_fn=proposing())
        (task,) = tasks.list_tasks(NODE)
        assert task["status"] == tasks.STATUS_CLOSED
        assert task["ack"]["reason"] == ack.REASON_AUTOMATED
        assert outbox_envelopes(repo) == []

    def test_acked_thread_is_never_nudged(self, ctx, r4t_home, fake_harness):
        # The reason the primitive exists: the quiet sweep can now tell
        # deliberate silence from a dropped ball. A dispatcher thread, because
        # the sweep skips relay threads outright.
        dispatcher_message(ctx)
        drain(ctx, run_fn=proposing())
        assert sweep_nudges(ctx) == []

    def test_the_same_thread_left_open_is_nudged(self, ctx, r4t_home, fake_harness):
        # The control for the test above: without the close, this thread nudges.
        dispatcher_message(ctx)
        drain(ctx, run_fn=quiet)
        (task,) = tasks.list_tasks(NODE)
        assert sweep_nudges(ctx) == [task["id"]]

    def test_tell_and_close_coexist_on_separate_threads(self, ctx, repo, r4t_home):
        handle_message(ctx, "boss", NODE, "First drop landed.", klass="auto",
                       drain_after=False)
        handle_message(ctx, "peer", NODE, "Second drop landed.", klass="auto",
                       drain_after=False)
        threads = sorted(t["id"] for t in tasks.list_tasks(NODE))

        def answer_one_close_the_other(rig, prompt, cwd, *, env=None, variant=0):
            outbox = dispatch.Path(env["TELL_OUTBOX_DIR"])
            msg_id = new_ulid()
            (outbox / f"{msg_id}.json").write_text(
                json.dumps({"id": msg_id, "to": "boss", "content": "got the first"}),
                encoding="utf-8",
            )
            return 0, f"{ack.VERB} {threads[1]}", 1.0, False

        assert drain(ctx, run_fn=answer_one_close_the_other) == 1
        by_id = {t["id"]: t for t in tasks.list_tasks(NODE)}
        assert [e["to"] for e in outbox_envelopes(repo)] == ["boss"]
        assert by_id[threads[1]]["ack"]["reason"] == ack.REASON_AUTOMATED
        assert "ack" not in by_id[threads[0]]


class TestRejection:
    def test_knob_off_closes_nothing(self, ctx, repo, r4t_home):
        (repo / "ROSTER.md").write_text(
            "### Gerry\n- **Rig:** leader\n- **Leader:** yes\n- **Ack:** off\n",
            encoding="utf-8",
        )
        deliver(ctx, FYI, run_fn=proposing())
        (task,) = tasks.list_tasks(NODE)
        assert task["status"] == tasks.STATUS_OPEN
        assert "ack" not in task
        assert "r4t: ACK-REJECT gerry" in read_log()
        assert "Ack is off for this member" in read_log()

    def test_malformed_verb_closes_nothing(self, ctx, repo, r4t_home):
        deliver(ctx, FYI, run_fn=proposing(line="CLOSE_WITHOUT_RETRY {thread}"))
        (task,) = tasks.list_tasks(NODE)
        assert task["status"] == tasks.STATUS_OPEN
        log = read_log()
        assert "malformed proposal" in log
        assert "the verb must be echoed exactly" in log
        # Fails safe in both directions: nothing closed AND nothing sent.
        assert outbox_envelopes(repo) == []

    def test_unknown_thread_closes_nothing(self, ctx, repo, r4t_home):
        stranger = new_ulid()
        deliver(ctx, FYI, run_fn=proposing(line=f"{ack.VERB} {stranger}"))
        (task,) = tasks.list_tasks(NODE)
        assert task["status"] == tasks.STATUS_OPEN
        assert "no such obligation in this turn's batch" in read_log()

    def test_direct_question_overrides_an_eligible_relay(self, ctx, repo, r4t_home):
        # Machine-originated, so it clears the allow-list — and is refused
        # anyway, because the content overrides ride on top of it.
        deliver(ctx, "The export ran. Did it finish clean?", run_fn=proposing())
        (task,) = tasks.list_tasks(NODE)
        assert task["status"] == tasks.STATUS_OPEN
        log = read_log()
        assert "content-override" in log and "direct question" in log
        assert outbox_envelopes(repo) == []

    def test_assignment_under_fyi_framing_overrides_an_eligible_relay(
        self, ctx, r4t_home
    ):
        deliver(
            ctx,
            "FYI only, no rush — please rotate the export key when you can.",
            run_fn=proposing(),
        )
        (task,) = tasks.list_tasks(NODE)
        assert task["status"] == tasks.STATUS_OPEN
        log = read_log()
        assert "content-override" in log and "direct assignment" in log

    def test_rejected_proposal_never_becomes_a_reply(self, ctx, repo, r4t_home):
        # The prose still reaches whoever asked; the fumbled protocol line does
        # not ride along in the body.
        prose = "The export summary is filed under drops/2026-07. " * 3
        deliver(ctx, "Where did the export land?", klass="human", sender="boss",
                run_fn=proposing(prose=prose))
        (envelope,) = outbox_envelopes(repo)
        assert ack.VERB not in envelope["content"]
        assert "export summary is filed" in envelope["content"]

    def test_a_second_proposal_for_a_closed_thread_is_refused(self, ctx, r4t_home):
        deliver(ctx, FYI, run_fn=proposing())
        (task,) = tasks.list_tasks(NODE)
        roster, member, rig = member_and_rig(ctx)
        closed = ack.run(
            ctx, member, rig,
            [{"thread": task["id"], "body": FYI, "class": "auto"}],
            f"{ack.VERB} {task['id']}", roster,
        )
        assert closed == []
        assert "the thread is already closed" in read_log()

    def test_a_plain_assignment_with_no_trigger_words_is_refused(
        self, ctx, repo, r4t_home
    ):
        # The keyword regex sees nothing here — no question mark, none of its
        # verbs — and the wording is irrelevant: the owner opened the thread,
        # so the allow-list never lets it near the content check.
        deliver(
            ctx,
            "Heads up from the ops sync. The export key rotates tonight at "
            "23:00 UTC; swap it on the drop host before the nightly run.",
            klass="human", sender="boss", run_fn=proposing(),
        )
        (task,) = tasks.list_tasks(NODE)
        assert task["status"] == tasks.STATUS_OPEN
        assert "ack" not in task
        assert "not-machine-originated" in read_log()
        assert sweep_nudges(ctx) == [task["id"]]   # and the owner still gets chased

    def test_a_peer_members_thread_is_refused(self, ctx, r4t_home):
        handle_message(ctx, f"{NODE}:phil", f"{NODE}:gerry",
                       "Filed the export summary under drops/2026-07.",
                       klass="auto", drain_after=False)
        drain(ctx, run_fn=proposing())
        (task,) = tasks.list_tasks(NODE)
        assert task["status"] == tasks.STATUS_OPEN
        assert "not-machine-originated" in read_log()

    def test_echo_member_never_proposes(self, ctx, r4t_home):
        roster, member, rig = member_and_rig(ctx)
        echo = replace(rig, echo=True)
        assert ack.run(ctx, member, echo, [], f"{ack.VERB} {THREAD}", roster) == []


class TestRequiredRoster:
    """The per-obligation guard is not opt-in: `ack.run` cannot be called
    without the roster it needs to evaluate `owes_creator`, and the predicate
    itself denies rather than waves through if it ever sees None."""

    def test_run_without_a_roster_is_a_type_error(self, ctx, r4t_home):
        _roster, member, rig = member_and_rig(ctx)
        with pytest.raises(TypeError):
            ack.run(ctx, member, rig, [], f"{ack.VERB} {THREAD}")

    def test_owes_creator_denies_without_a_roster(self):
        assert not ack.owes_creator(
            NODE, None, {"creator": "boss"}, [{"from": "boss"}]
        )

    def test_a_member_that_does_not_owe_the_creator_only_notes(self, ctx, r4t_home):
        # The probe that made the kwarg a footgun: phil proposing a close on a
        # thread gerry owes. With the roster in hand the ledger stays open.
        handle_message(ctx, "peer", NODE, "Nightly export finished.", klass="auto",
                       drain_after=False)
        (task,) = tasks.list_tasks(NODE)
        roster, member, rig = member_and_rig(ctx, "phil")
        closed = ack.run(
            ctx, member, rig,
            [{"thread": task["id"], "from": f"{NODE}:gerry", "body": "fyi",
              "class": "auto"}],
            f"{ack.VERB} {task['id']}", roster,
        )
        assert closed == []
        assert tasks.load_task(NODE, task["id"])["status"] == tasks.STATUS_OPEN
        assert "ACK-NOTED" in read_log()

    def test_a_creator_that_left_the_roster_still_binds_the_obligation(
        self, ctx, repo, r4t_home
    ):
        # An unknown creator name canonicalizes to itself, so deleting phil from
        # ROSTER.md does not hand phil's thread to whoever proposes next.
        handle_message(ctx, "peer", NODE, "Nightly export finished.", klass="auto",
                       drain_after=False)
        (task,) = tasks.list_tasks(NODE)
        task["creator"] = f"{NODE}:phil"
        tasks.save_task(NODE, task)
        (repo / "ROSTER.md").write_text(
            "### Gerry\n- **Rig:** leader\n- **Leader:** yes\n", encoding="utf-8"
        )
        roster, member, rig = member_and_rig(ctx)
        batch = [{"thread": task["id"], "from": "peer", "body": "fyi",
                  "class": "auto"}]
        assert ack.run(
            ctx, member, rig, batch, f"{ack.VERB} {task['id']}", roster
        ) == []
        batch[0]["from"] = f"{NODE}:phil"
        assert ack.run(
            ctx, member, rig, batch, f"{ack.VERB} {task['id']}", roster
        ) == [task["id"]]


class TestOriginTrust:
    """`r4t dispatch --from` takes any sender string, so the allow-list must not
    read 'the dispatcher opened this' out of one."""

    def test_a_sender_named_like_the_dispatcher_gets_no_allow_list(
        self, ctx, repo, r4t_home
    ):
        handle_message(ctx, f"r4t:{NODE}", NODE, "Rotate the export key tonight.",
                       klass="human", drain_after=False)
        drain(ctx, run_fn=proposing())
        (task,) = tasks.list_tasks(NODE)
        assert task["creator"] == f"r4t:{NODE}"       # the name was accepted
        assert task["origin"] == ""                   # the claim was not
        assert task["status"] == tasks.STATUS_OPEN
        assert "ack" not in task
        assert "not-machine-originated" in read_log()
        assert outbox_envelopes(repo) == []
        assert sweep_nudges(ctx) == [task["id"]]      # still chased, as it should be

    def test_the_dispatchers_own_thread_carries_the_flag(self, ctx, r4t_home):
        dispatcher_message(ctx)
        (task,) = tasks.list_tasks(NODE)
        assert task["origin"] == tasks.ORIGIN_DISPATCHER

    def test_a_nudge_does_not_stamp_the_thread_it_rides(self, ctx, r4t_home,
                                                        fake_harness):
        # The sweep speaks in r4t's voice on the OWNER's thread. The flag is
        # stamped at open time only, so the backstop cannot make its own target
        # closeable.
        handle_message(ctx, "boss", NODE, "Please confirm the rotation plan.",
                       klass="human", drain_after=False)
        drain(ctx, run_fn=quiet)
        (task,) = tasks.list_tasks(NODE)
        assert sweep_nudges(ctx) == [task["id"]]
        (task,) = tasks.list_tasks(NODE)
        assert task["origin"] == ""


class TestSharedThreads:
    """A thread id travels down a delegation chain, so 'this thread' names one
    conversation and several obligations. Only the member the creator is
    waiting on may end it."""

    def delegating(self, thread_holder):
        def run(rig, prompt, cwd, *, env=None, variant=0):
            if env["R4T_MEMBER"].lower() == "gerry":
                outbox = dispatch.Path(env["TELL_OUTBOX_DIR"])
                msg_id = new_ulid()
                (outbox / f"{msg_id}.json").write_text(
                    json.dumps({
                        "id": msg_id, "to": "phil",
                        "content": "Context for you: the dump is on the calendar.",
                        "files": [],
                    }),
                    encoding="utf-8",
                )
                return 0, "delegated", 1.0, False
            thread = PROMPT_THREAD_RE.search(prompt).group(1)
            thread_holder.append(thread)
            return 0, f"{ack.VERB} {thread}", 1.0, False

        return run

    def test_a_downstream_member_cannot_close_the_originators_obligation(
        self, ctx, repo, r4t_home
    ):
        seen: list[str] = []
        dispatcher_message(ctx)
        while drain(ctx, run_fn=self.delegating(seen)):   # gerry, then phil
            pass
        (task,) = tasks.list_tasks(NODE)
        assert seen == [task["id"]]                  # phil really did hold it
        assert task["status"] == tasks.STATUS_OPEN
        assert "ack" not in task
        assert [n["member"] for n in task["ack_notes"]] == ["phil"]
        assert "ACK-NOTED" in read_log()
        assert sweep_nudges(ctx) == [task["id"]]     # the obligation still stands

    def test_the_noting_member_keeps_its_stdout_fallback(self, ctx, repo, r4t_home):
        # Suppression follows the close, so a member whose proposal was only
        # noted is still rescued by the fallback.
        answer = "The dump landed in drops/2026-07 on the primary host. " * 3

        def run(rig, prompt, cwd, *, env=None, variant=0):
            if env["R4T_MEMBER"].lower() == "gerry":
                outbox = dispatch.Path(env["TELL_OUTBOX_DIR"])
                msg_id = new_ulid()
                (outbox / f"{msg_id}.json").write_text(
                    json.dumps({"id": msg_id, "to": "phil", "content": "look at this",
                                "files": []}),
                    encoding="utf-8",
                )
                return 0, "delegated", 1.0, False
            thread = PROMPT_THREAD_RE.search(prompt).group(1)
            return 0, f"{answer}\n{ack.VERB} {thread}", 1.0, False

        dispatcher_message(ctx)
        while drain(ctx, run_fn=run):
            pass
        log = read_log()
        assert "ACK-NOTED" in log
        assert "STDOUT-REPLY phil" in log
        assert "ACK-QUIET phil" not in log


class TestProtocolProse:
    """Writing about the protocol is not speaking it."""

    def test_a_sentence_naming_the_verb_reaches_the_asker_intact(
        self, ctx, repo, r4t_home
    ):
        prose = (
            "You asked how the new disposition works, so here is the short "
            "version.\n"
            "Close_without_reply ends an obligation with no message at all.\n"
            "It only applies when nothing is asked of the member, which is not "
            "the case for your question."
        )
        handle_message(ctx, "boss", NODE, "How does the new close disposition work?",
                       klass="human", drain_after=False)
        drain(ctx, run_fn=lambda *a, **k: (0, prose, 1.0, False))
        (envelope,) = outbox_envelopes(repo)
        assert "ends an obligation with no message" in envelope["content"]
        assert "malformed proposal" not in read_log()

    def test_a_fenced_verb_line_closes_nothing(self, ctx, repo, r4t_home):
        def run(rig, prompt, cwd, *, env=None, variant=0):
            thread = PROMPT_THREAD_RE.search(prompt).group(1)
            return 0, (
                "Here is the syntax you asked about — one line of its own, the "
                "thread id copied from the message header:\n\n"
                f"```\n{ack.VERB} {thread}\n```\n"
            ), 1.0, False

        # An eligible relay thread: only the fence stands between the member
        # and a close, and the thread ends up closed the ordinary way instead —
        # by the answer the fallback delivers.
        deliver(ctx, FYI, run_fn=run)
        (task,) = tasks.list_tasks(NODE)
        assert "ack" not in task
        assert "r4t: ACK thread=" not in read_log()
        (envelope,) = outbox_envelopes(repo)
        # The prose reaches the asker; the verb line does not ride along, fenced
        # or not — a delivered body is somebody else's prompt.
        assert "one line of its own" in envelope["content"]
        assert ack.VERB not in envelope["content"]

    def test_an_unbalanced_fence_cannot_ship_the_verb_in_a_body(
        self, ctx, repo, r4t_home
    ):
        # The probe: a member pastes a log tail, forgets the closing fence, and
        # the rest of the turn reads as quotation. Parsing skips it (nothing
        # closes) and stripping does not (nothing leaks).
        def run(rig, prompt, cwd, *, env=None, variant=0):
            thread = PROMPT_THREAD_RE.search(prompt).group(1)
            return 0, (
                "The export landed in drops/2026-07 on the primary host, "
                "here is the tail of the log:\n"
                "```\n"
                "rows=412 ok\n"
                f"{ack.VERB} {thread}\n"
            ), 1.0, False

        handle_message(ctx, "boss", NODE, "Where did last night's export land?",
                       klass="human", drain_after=False)
        drain(ctx, run_fn=run)
        (envelope,) = outbox_envelopes(repo)
        assert ack.VERB not in envelope["content"]
        assert "rows=412 ok" in envelope["content"]


class TestPerObligationQuiet:
    def test_a_close_on_one_thread_still_delivers_the_answer_owed_another(
        self, ctx, repo, r4t_home
    ):
        handle_message(ctx, "peer", NODE, "Nightly export finished, 412 rows.",
                       klass="auto", drain_after=False)
        handle_message(ctx, "boss", NODE, "Where did last night's export land?",
                       klass="human", drain_after=False)
        threads = {t["creator"]: t["id"] for t in tasks.list_tasks(NODE)}
        answer = "It landed in drops/2026-07 on the primary host. " * 3

        def run(rig, prompt, cwd, *, env=None, variant=0):
            return 0, f"{answer}\n{ack.VERB} {threads['peer']}", 1.0, False

        drain(ctx, run_fn=run)
        (envelope,) = outbox_envelopes(repo)
        assert envelope["to"] == "boss"
        assert "landed in drops/2026-07" in envelope["content"]
        assert ack.VERB not in envelope["content"]
        by_id = {t["id"]: t for t in tasks.list_tasks(NODE)}
        assert by_id[threads["peer"]]["ack"]["member"] == "gerry"
        assert "ack" not in by_id[threads["boss"]]
        log = read_log()
        assert "STDOUT-REPLY" in log
        assert "ACK-QUIET" not in log

    def test_a_close_on_the_answered_thread_still_suppresses_the_fallback(
        self, ctx, repo, r4t_home
    ):
        prose = "I read the drop and filed it under drops/2026-07. " * 3
        deliver(ctx, FYI, run_fn=proposing(prose=prose))
        assert outbox_envelopes(repo) == []
        log = read_log()
        assert "ACK-QUIET gerry" in log
        assert "STDOUT-REPLY" not in log


class TestNudgeThread:
    def test_the_leader_cannot_close_the_sweeps_nudge(self, ctx, repo, r4t_home):
        # The backstop must not be closable by the member it is chasing: the
        # nudge rides the owner's thread, and the owner opened it.
        handle_message(ctx, "boss", NODE, "Please confirm the rotation plan.",
                       klass="human", drain_after=False)
        drain(ctx, run_fn=quiet)
        (task,) = tasks.list_tasks(NODE)
        assert sweep_nudges(ctx) == [task["id"]]
        drain(ctx, run_fn=proposing())               # gerry answers the nudge with a close
        (task,) = tasks.list_tasks(NODE)
        assert task["status"] == tasks.STATUS_OPEN
        assert "ack" not in task
        assert "not-machine-originated" in read_log()


class TestReopen:
    def test_a_new_inbound_reopens_an_acked_thread(self, ctx, repo, r4t_home):
        # An ack ends the obligations the thread carried, not the ones it has
        # not carried yet — otherwise a later message on it is invisible to the
        # sweep forever.
        dispatcher_message(ctx)
        drain(ctx, run_fn=proposing())
        (task,) = tasks.list_tasks(NODE)
        assert task["status"] == tasks.STATUS_CLOSED
        # Internal traffic is what reuses a thread id — external mail always
        # opens a fresh one.
        dispatch._ingest(
            ctx, f"{NODE}:phil", f"{NODE}:gerry",
            "Following up on that dump — I still need the drop path.",
            klass="auto", internal=True, thread=task["id"],
        )
        (task,) = tasks.list_tasks(NODE)
        assert task["status"] == tasks.STATUS_OPEN
        assert task["answered"] is False
        assert "ack" not in task
        assert [n["member"] for n in task["ack_notes"]] == ["gerry"]
        log = read_log()
        assert f"r4t: ACK thread={task['id']}" in log
        assert f"r4t: ACK-REOPENED thread={task['id']}" in log
        time.sleep(0.01)
        assert sweep_nudges(ctx) == [task["id"]]

    def test_a_same_turn_delegation_reopens_and_says_so(self, ctx, repo, r4t_home):
        # The close commits before staging is released, and a staged intra-roster
        # tell inherits the batch's thread — so a turn that closes T and also
        # delegates on T undoes its own ack. Safe (the obligation stays open),
        # but the day log has to say both things happened (#83).
        handle_message(ctx, "peer", NODE, "Nightly export finished, 412 rows.",
                       klass="auto", drain_after=False)
        (task,) = tasks.list_tasks(NODE)
        thread = task["id"]

        def run(rig, prompt, cwd, *, env=None, variant=0):
            if env["R4T_MEMBER"].lower() == "gerry":
                outbox = dispatch.Path(env["TELL_OUTBOX_DIR"])
                msg_id = new_ulid()
                (outbox / f"{msg_id}.json").write_text(
                    json.dumps({"id": msg_id, "to": "phil",
                                "content": "Filing this export note with you.",
                                "files": []}),
                    encoding="utf-8",
                )
                return 0, f"{ack.VERB} {thread}", 1.0, False
            return 0, "noted", 1.0, False

        drain(ctx, run_fn=run)
        (task,) = tasks.list_tasks(NODE)
        log = read_log()
        assert f"r4t: ACK thread={thread}" in log
        assert f"r4t: ACK-REOPENED thread={thread}" in log
        assert "supersedes gerry's close" in log
        assert task["status"] == tasks.STATUS_OPEN
        assert "ack" not in task
        assert task["ack_notes"][0]["superseded_at"]

    def test_a_thread_closed_by_a_real_answer_stays_closed(self, ctx, repo, r4t_home):
        handle_message(ctx, "boss", NODE, "Where did the export land?",
                       klass="human", drain_after=False)
        (task,) = tasks.list_tasks(NODE)
        tasks.close_task(NODE, task["id"])
        dispatch._ingest(ctx, f"{NODE}:phil", f"{NODE}:gerry", "Thanks.",
                         klass="auto", internal=True, thread=task["id"])
        (task,) = tasks.list_tasks(NODE)
        assert task["status"] == tasks.STATUS_CLOSED


class TestKnobHygiene:
    def test_a_bad_ack_value_errors_and_still_defaults_on(self, repo):
        (repo / "ROSTER.md").write_text(
            "### Gerry\n- **Rig:** leader\n- **Leader:** yes\n- **Ack:** sometimes\n",
            encoding="utf-8",
        )
        member = load_roster(repo / "ROSTER.md").find("gerry")
        assert member.errors and "Ack must be on or off" in member.errors[0]
        assert member.ack is True   # erroring member is disabled; the knob still reads on
