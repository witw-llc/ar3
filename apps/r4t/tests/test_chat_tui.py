"""Textual chat TUI tests — headless via textual's pilot. Skipped when
textual is not installed (the CLI falls back to the line UI then too)."""
from __future__ import annotations

import asyncio
import time

import pytest

pytest.importorskip("textual")

import chat_tui
import state
import tasks as taskmod
from chat_tui import ChatApp, budget_bar
from roster import load_roster

NODE = "acme"


@pytest.fixture
def seat(ctx, repo, r4t_home):
    roster = load_roster(repo / "ROSTER.md")
    human = next(m for m in roster.members if m.is_human)
    return ctx, roster, human


def test_budget_bar_shapes():
    assert budget_bar(0.0, 1.0) == "░" * 8
    assert budget_bar(1.0, 1.0) == "█" * 8
    assert budget_bar(0.5, 1.0).count("█") == 4
    assert budget_bar(5.0, 1.0) == "█" * 8
    assert budget_bar(1.0, 0.0) == "░" * 8


def test_tui_consumes_inbox_and_routes_commands(seat, monkeypatch):
    ctx, roster, human = seat
    state.park_seat_message(
        NODE, "Neil", "acme:gerry",
        "[r4t thread=01KX0000000000000000000000 hop=1 auto] ready for review",
    )
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        chat_tui, "send_as_human",
        lambda ctx, human, to, text: calls.append((to, text)),
    )

    async def scenario():
        from textual.widgets import Input, Static

        app = ChatApp(ctx, roster, human)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert len(state.list_seat_messages(NODE, "neil", read=True)) == 1

            header = str(app.query_one("#header", Static).content)
            assert NODE in header and "seat Neil" in header

            composer = app.query_one("#composer", Input)
            composer.value = "/to phil"
            await pilot.press("enter")
            assert app.target == "acme:phil"

            composer.value = "/to nobody"
            await pilot.press("enter")
            assert app.target == "acme:phil"

            composer.value = "ship it"
            await pilot.press("enter")
            deadline = time.time() + 5
            while not calls and time.time() < deadline:
                await asyncio.sleep(0.05)
            assert calls == [("acme:phil", "ship it")]
            assert composer.value == ""

    asyncio.run(scenario())


def test_tui_renders_markdown_bodies(seat):
    ctx, roster, human = seat
    state.park_seat_message(
        NODE, "Neil", "acme:gerry",
        "# Report\n\n**bold claim** and `code()`",
    )

    async def scenario():
        from textual.widgets import RichLog

        app = ChatApp(ctx, roster, human)
        async with app.run_test() as pilot:
            await pilot.pause()
            conv = app.query_one("#conversation", RichLog)
            text = "\n".join(strip.text for strip in conv.lines)
            assert "acme:gerry" in text
            assert "bold claim" in text and "Report" in text
            assert "**" not in text and "# Report" not in text

    asyncio.run(scenario())


def test_tui_message_header_shows_rig_slug(seat):
    ctx, roster, human = seat
    state.park_seat_message(
        NODE, "Neil", "acme:gerry",
        "[r4t thread=01KX0000000000000000000000 hop=1 auto] done",
    )
    state.park_seat_message(
        NODE, "Neil", "external:bot",
        "[r4t thread=01KX0000000000000000000000 hop=1 auto] ping",
    )

    async def scenario():
        from textual.widgets import RichLog

        app = ChatApp(ctx, roster, human)
        async with app.run_test() as pilot:
            await pilot.pause()
            conv = app.query_one("#conversation", RichLog)
            text = "\n".join(strip.text for strip in conv.lines)
            assert "acme:gerry (leader)" in text
            assert "external:bot" in text
            assert "external:bot (" not in text  # non-member: no rig slug

    asyncio.run(scenario())


def test_tui_commands_report_in_conversation(seat):
    ctx, roster, human = seat
    taskmod.ensure_task(NODE, "01KX000000000000000000AAAA", "acme:gerry")

    async def scenario():
        from textual.widgets import Input, RichLog

        app = ChatApp(ctx, roster, human)
        async with app.run_test() as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", Input)
            for value in ("/bogus", "/help", "/threads"):
                composer.value = value
                await pilot.press("enter")
            await pilot.pause()
            conv = app.query_one("#conversation", RichLog)
            text = "\n".join(strip.text for strip in conv.lines)
            assert "unknown command:" in text and "/bogus" in text
            assert "/threads" in text  # /help listing
            assert "creator=acme:gerry" in text  # /threads listing

    asyncio.run(scenario())


def test_tui_header_surfaces_trouble(seat):
    ctx, roster, human = seat
    state.record_dead_letter(
        NODE, reason="pair-repeat", sender="acme:gerry", to="phil",
        thread="01X", content="x",
    )

    async def scenario():
        from textual.widgets import Static

        app = ChatApp(ctx, roster, human)
        async with app.run_test() as pilot:
            await pilot.pause()
            header = str(app.query_one("#header", Static).content)
            assert "pair-repeat" in header

    asyncio.run(scenario())


def test_tui_attach_and_detach_toggles_panes(seat):
    ctx, roster, human = seat

    async def scenario():
        from textual.widgets import Input, RichLog

        app = ChatApp(ctx, roster, human)
        async with app.run_test() as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", Input)
            composer.value = "/attach phil"
            await pilot.press("enter")
            await pilot.pause()
            assert app.attached == "phil"
            assert app.query_one("#attach", RichLog).display is True
            assert app.query_one("#activity", RichLog).display is False

            composer.value = "/detach"
            await pilot.press("enter")
            await pilot.pause()
            assert app.attached is None
            assert app.query_one("#attach", RichLog).display is False
            assert app.query_one("#activity", RichLog).display is True

    asyncio.run(scenario())


def test_tui_click_member_attaches(seat):
    ctx, roster, human = seat

    async def scenario():
        app = ChatApp(ctx, roster, human)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.click("#member-phil")
            await pilot.pause()
            assert app.attached == "phil"

    asyncio.run(scenario())


def test_tui_attach_flag_opens_attached(seat):
    ctx, roster, human = seat

    async def scenario():
        app = ChatApp(ctx, roster, human, attach="phil")
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.attached == "phil"

    asyncio.run(scenario())


def test_tui_attach_backfills_history(seat):
    ctx, roster, human = seat
    state.append_log(
        NODE, 'r4t: QUEUED gerry -> phil thread=T hop=0 "before attach" (depth 1)'
    )
    state.enqueue(NODE, "phil", {"from": "acme:neil", "body": "still queued"})

    async def scenario():
        from textual.widgets import Input, RichLog

        app = ChatApp(ctx, roster, human)
        async with app.run_test() as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", Input)
            composer.value = "/attach phil"
            await pilot.press("enter")
            await pilot.pause()
            text = "\n".join(
                strip.text for strip in app.query_one("#attach", RichLog).lines
            )
            history = text.index("── history ──")
            event = text.index("before attach")
            queued = text.index("queued from acme:neil: still queued")
            live = text.index("── live ──")
            assert history < event < queued < live

    asyncio.run(scenario())
