"""r4t chat — Textual front end over the seat, and the team's control plane.

One window: a health header fed by the same verdict engine as `r4t status`,
a clickable member status panel (who is active, resting, or broken and how
deep each queue is), a conversation pane (seat messages and what you say)
beside a fly-on-the-wall activity pane, and an input line that speaks as the
roster human. Sends run on a background thread — dispatch turns are
synchronous and slow, and the wall must keep scrolling while a rig thinks.

Gemba attach (`/attach <member>` or clicking a member) opens a read-only live
view of one member: every message it receives as it is enqueued, and its turn
output streaming as it comes out (tailed from agents/<member>/live.log).
Attaching is observation only — it never sends to the member; the composer
keeps talking to the seat's usual counterparties.

Importing this module requires textual; cmd_chat falls back to the line
UI in chat.py when it is missing.
"""
from __future__ import annotations

import queue
import sys
import threading
from datetime import datetime

from rich.markdown import Markdown
from rich.padding import Padding
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Footer, Input, RichLog, Static

import state
import tasks as taskmod
import verdict
from chat import (
    ACTIVE,
    BROKEN,
    RESTING,
    MemberWatch,
    SeatError,
    SeatFeed,
    handle_command,
    load_seat,
    member_backfill,
    member_statuses,
    send_as_human,
    sender_label,
)
from dispatch import DispatchContext
from rig import RigError, load_rig_config
from roster import Member, Roster

FEED_POLL_SECONDS = 0.5
HEADER_REFRESH_SECONDS = 5.0
MAX_HEADER_VERDICTS = 4
MAX_HEADER_TASKS = 3

KIND_STYLE = {
    "in": "cyan", "you": "green", "sys": "yellow", "act": "dim",
    "recv": "magenta", "out": "white",
}
LEVEL_STYLE = {verdict.OK: "green", verdict.WARN: "yellow", verdict.BAD: "red"}
STATE_STYLE = {ACTIVE: "green", RESTING: "yellow", BROKEN: "red"}


class MemberRow(Static):
    """A clickable member line in the status panel: click to attach a read-only
    gemba view of that member."""

    class Clicked(Message):
        def __init__(self, name: str) -> None:
            self.name = name
            super().__init__()

    def __init__(self, name: str) -> None:
        super().__init__(id=f"member-{name.lower()}")
        self.member_name = name

    def on_click(self) -> None:
        self.post_message(self.Clicked(self.member_name))


def budget_bar(level: float, budget: float, width: int = 8) -> str:
    """Fill by how much spend budget is LEFT: a full bar is a fresh member,
    an empty bar is a resting one."""
    frac = 0.0 if budget <= 0 else max(0.0, min(1.0, level / budget))
    filled = round(frac * width)
    return "█" * filled + "░" * (width - filled)


class ChatApp(App):
    TITLE = "r4t chat"

    CSS = """
    Screen { layout: vertical; }
    #header { height: auto; padding: 0 1; background: $panel; }
    #panes { height: 1fr; }
    #roster { width: 26; border-right: solid $panel; padding: 0 1; }
    #roster MemberRow { height: 1; }
    #conversation { width: 2fr; }
    #activity { width: 1fr; border-left: solid $panel; }
    #attach { width: 2fr; border-left: solid $panel; display: none; }
    #composer { dock: bottom; }
    """

    BINDINGS = [("ctrl+q", "quit", "Quit")]

    def __init__(
        self, ctx: DispatchContext, roster: Roster, human: Member, attach: str | None = None
    ):
        super().__init__()
        self.ctx = ctx
        self.roster = roster
        self.human = human
        self.target = ctx.node
        self.feed = SeatFeed(ctx.node, human.name)
        self.sends: queue.Queue[tuple[str, str]] = queue.Queue()
        self.attached: str | None = None
        self.watch: MemberWatch | None = None
        self._pending_attach = attach.lower() if attach else None
        try:
            self.rig_config = load_rig_config(ctx.config_path)
        except RigError:
            self.rig_config = None

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        with Horizontal(id="panes"):
            with Vertical(id="roster"):
                for m in self.roster.members:
                    if not m.is_human and not m.errors:
                        yield MemberRow(m.name)
            yield RichLog(id="conversation", wrap=True)
            yield RichLog(id="activity", wrap=True, max_lines=2000)
            yield RichLog(id="attach", wrap=True, max_lines=4000)
        yield Input(
            id="composer",
            placeholder=f"message {self.target} (leader) — /help for commands",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._emit(
            "sys",
            f"seat: {self.human.name} on {self.ctx.node} — messages go to "
            f"{self.target} (leader). /help for commands.",
        )
        self._pump_feed()
        self._refresh_header()
        self._refresh_roster()
        threading.Thread(target=self._send_worker, daemon=True).start()
        self.set_interval(FEED_POLL_SECONDS, self._poll_feed)
        self.set_interval(HEADER_REFRESH_SECONDS, self._refresh_header)
        self.set_interval(HEADER_REFRESH_SECONDS, self._refresh_roster)
        self.query_one("#composer", Input).focus()
        if self._pending_attach is not None:
            self._attach(self._pending_attach)

    # ---------- output ----------

    def _emit(self, kind: str, text: str) -> None:
        if kind in ("recv", "out"):
            log_id = "#attach"
        elif kind == "act":
            log_id = "#activity"
        else:
            log_id = "#conversation"
        log = self.query_one(log_id, RichLog)
        stamp = datetime.now().strftime("%H:%M:%S")
        for line in text.splitlines():
            log.write(Text.assemble((stamp + " ", "dim"), (line, KIND_STYLE[kind])))

    def _emit_message(self, envelope: dict) -> None:
        """An inbound message: sender header line, then the body as rendered
        markdown — agents write markdown, and raw ** and # are noise."""
        log = self.query_one("#conversation", RichLog)
        stamp = datetime.now().strftime("%H:%M:%S")
        sender = sender_label(self.roster, str(envelope.get("from", "?")))
        body = str(envelope.get("content", ""))
        log.write(Text.assemble((stamp + " ", "dim"), (sender, "bold cyan")))
        log.write(Padding(Markdown(body.strip() or "(empty)"), (0, 0, 1, 9)))

    # ---------- polling ----------

    def _pump_feed(self) -> None:
        for kind, payload in self.feed.poll():
            if kind == "in":
                self._emit_message(payload)
            else:
                self._emit(kind, payload)

    def _poll_feed(self) -> None:
        state.touch_seat_presence(self.ctx.node, self.human.name)
        self._pump_feed()
        self._pump_watch()

    # ---------- control plane: status panel + gemba attach ----------

    def _refresh_roster(self) -> None:
        for row in member_statuses(self.ctx.node, self.roster, self.rig_config):
            try:
                widget = self.query_one(f"#member-{row.name.lower()}", MemberRow)
            except Exception:  # noqa: BLE001 — panel is best-effort
                continue
            marker = "▶ " if row.name.lower() == self.attached else "  "
            label = Text(marker, style="bold cyan" if row.name.lower() == self.attached else "")
            label.append(f"{row.name} ", style=STATE_STYLE.get(row.state, "dim"))
            tail = row.state
            if row.queue:
                tail += f" · {row.queue}q"
            if row.detail:
                tail += f" · {row.detail}"
            label.append(tail, style="dim")
            widget.update(label)

    def _attach(self, name: str) -> None:
        member = self.roster.find(name)
        if member is None or member.is_human or member.errors:
            self._emit("sys", f"no AI member named {name!r} to attach to")
            return
        self.attached = member.name.lower()
        self.watch = MemberWatch(self.ctx.node, self.attached)
        self.query_one("#activity", RichLog).display = False
        attach_log = self.query_one("#attach", RichLog)
        attach_log.display = True
        attach_log.clear()
        self._emit("sys", f"── attached to {self.attached} (read-only) ──")
        self._backfill_attach(attach_log)
        self._pump_watch()
        self._refresh_roster()

    def _backfill_attach(self, attach_log: RichLog) -> None:
        """Seed the attach pane with the member's recent history so a message
        sent before attach-time is visible; the live tail continues below."""
        attach_log.write(Text("── history ──", style="dim"))
        events = member_backfill(self.ctx.node, self.attached)
        if not events:
            attach_log.write(Text("(no recent activity)", style="dim"))
        for kind, text in events:
            stamp = datetime.now().strftime("%H:%M:%S")
            attach_log.write(
                Text.assemble(
                    (stamp + " ", "dim"), (text, KIND_STYLE.get(kind, "white"))
                )
            )
        attach_log.write(Text("── live ──", style="dim"))

    def _detach(self) -> None:
        if self.attached is None:
            return
        self.attached = None
        self.watch = None
        self.query_one("#attach", RichLog).display = False
        self.query_one("#activity", RichLog).display = True
        self._refresh_roster()

    def _pump_watch(self) -> None:
        if self.watch is None:
            return
        for kind, text in self.watch.poll():
            self._emit(kind, text)

    def on_member_row_clicked(self, message: MemberRow.Clicked) -> None:
        self._attach(message.name)

    def _member_budget_rows(self) -> list[tuple[str, float, float]]:
        """(name, level, max) for the members you're talking to. The target is
        a canonical address (bare node = leader); show that member's bucket."""
        if self.rig_config is None:
            return []
        _, sub = self.target.partition(":")[0], self.target.partition(":")[2]
        member = self.roster.find(sub) if sub else self.roster.leader()
        rows: list[tuple[str, float, float]] = []
        if member is not None and not member.is_human and not member.errors:
            rig, _err, _pinned = self.rig_config.rig_for(member)
            if rig is not None:
                level = state.budget_level(
                    self.ctx.node, member.name, rig.budget_max, rig.budget_earn_per_hour
                )
                rows.append((member.name, level, rig.budget_max))
                if rig.rig_budget_max is not None:
                    rig_level = state.rig_budget_level(
                        rig.name, rig.rig_budget_max, rig.rig_budget_earn_per_hour
                    )
                    rows.append((f"rig {rig.name}", rig_level, rig.rig_budget_max))
        return rows

    def _refresh_header(self) -> None:
        verdicts = verdict.team_verdicts(self.ctx.node, self.roster, self.rig_config)
        worst = verdict.worst_level(verdicts)
        open_threads = [
            t for t in taskmod.list_tasks(self.ctx.node)
            if t.get("status") == taskmod.STATUS_OPEN
        ]

        header = Text()
        header.append(self.ctx.node, style="bold")
        header.append(f" · seat {self.human.name} → {self.target}", style="")
        header.append(f" · {len(open_threads)} open thread(s)", style="dim")
        header.append("   ")
        header.append(
            f"{verdict.MARKS[worst]} "
            + {"ok": "healthy", "warn": "attention", "bad": "action needed"}[worst],
            style=LEVEL_STYLE[worst],
        )

        shown = [v for v in verdicts if v.level != verdict.OK]
        for v in shown[:MAX_HEADER_VERDICTS]:
            header.append("\n")
            header.append(f"{verdict.MARKS[v.level]} {v.text}", style=LEVEL_STYLE[v.level])
        if len(shown) > MAX_HEADER_VERDICTS:
            header.append(
                f"\n… {len(shown) - MAX_HEADER_VERDICTS} more (r4t status)", style="dim"
            )
        for name, level, budget_max in self._member_budget_rows()[:MAX_HEADER_TASKS]:
            depth = state.queue_depth(self.ctx.node, name)
            header.append("\n")
            header.append(budget_bar(level, budget_max), style="magenta")
            tail = f" {name} budget {state.fmt_budget(level)}/{state.fmt_budget(budget_max)}"
            if depth:
                tail += f"  {depth} queued"
            header.append(tail, style="dim")
        self.query_one("#header", Static).update(header)

    # ---------- sending ----------

    def _send_worker(self) -> None:
        while True:
            to, text = self.sends.get()
            try:
                note = send_as_human(self.ctx, self.human, to, text)
                if note:
                    self.call_from_thread(self._emit, "sys", note)
            except Exception as e:  # noqa: BLE001 — surface, don't kill the seat
                self.call_from_thread(self._emit, "sys", f"send failed: {e}")

    # ---------- input ----------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        event.input.clear()
        if not line:
            return
        if line.startswith("/"):
            self._command(line)
            return
        self.sends.put((self.target, line))
        self._emit("you", f"you -> {self.target}: {line}")

    def _command(self, line: str) -> None:
        result = handle_command(self.roster, self.ctx.node, self.human, line)
        if result.quit:
            self.exit(0)
            return
        if result.target is not None:
            self.target = result.target
            suffix = " (leader)" if result.target == self.ctx.node else ""
            self.query_one("#composer", Input).placeholder = (
                f"message {self.target}{suffix} — /help for commands"
            )
            self._refresh_header()
        if result.attach is not None:
            self._attach(result.attach)
        if result.detach:
            self._detach()
        if result.lines:
            self._emit("sys", "\n".join(result.lines))


def run_chat_tui(ctx: DispatchContext, attach: str | None = None) -> int:
    try:
        roster, human = load_seat(ctx)
    except SeatError as e:
        print(f"r4t chat: {e}", file=sys.stderr)
        return 2
    app = ChatApp(ctx, roster, human, attach=attach)
    try:
        state.touch_seat_presence(ctx.node, human.name)
        result = app.run()
    finally:
        state.clear_seat_presence(ctx.node, human.name)
    return int(result or 0)
