"""Addressing — the node is the namespace, and `:name` is the way out.

Three vantages resolve one grammar: a global a8s sender outside every wall,
a member inside the walls writing to its per-turn staging outbox, and the r4t
router reading the `to` a8s handed it. This file is the design's resolution
table as tests, plus the two refusals that give the table teeth (ingress and
the deferred cell), the machine trust ceiling, and `r4t add` end to end.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import runbook
import state
from dispatch import DispatchContext, drain, handle_message
from rig import machine_ceiling, raise_machine_ceiling
from roster import load_roster, resolve_roster_path
from r4t import main as r4t_main

NODE = "acme"


# --- the roster the whole table is read against ------------------------------
#
# Node `acme`, leader `amy`, member `bob` who takes ingress, member `dana` who
# does not, cell `eng`. A global a8s agent named `bob` collides with the member
# — that is the point.

ROSTER = """\
---
name: "acme"
---

## Roster

### amy
- **Rig:** leader
- **Leader:** yes
- **Cell:** eng

### bob
- **Rig:** junior-dev
- **Ingress:** yes
- **Cell:** eng

### dana
- **Rig:** junior-dev

## Cells

### eng
- **Lead:** amy
"""


def write_roster(root: Path, text: str = ROSTER) -> Path:
    path = root / runbook.RUNBOOK_NAME
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def node_dir(tmp_path):
    root = tmp_path / "acme"
    root.mkdir()
    write_roster(root)
    return root


@pytest.fixture
def acme(r4t_home, node_dir, rig_config, tells):
    _sent, capture = tells
    return DispatchContext(
        root=node_dir,
        node=NODE,
        roster_path=node_dir / runbook.RUNBOOK_NAME,
        config_path=rig_config,
        tell_fn=capture,
    )


def queued(name: str) -> int:
    return state.queue_depth(NODE, name)


def dead_reasons() -> list[str]:
    return sorted(r["reason"] for r in state.list_dead_letters(NODE))


def outbox(root: Path) -> list[dict]:
    d = root / ".outbox"
    if not d.is_dir():
        return []
    return [json.loads(f.read_text(encoding="utf-8")) for f in sorted(d.glob("*.json"))]


# --- vantage C: the router resolving what a8s handed it -----------------------


class TestTheRouterResolvesTheAddress:
    """`$RECIPIENT` verbatim -> the member that gets the message. The sender is
    outside every wall, so every row here is ingress-gated."""

    @pytest.mark.parametrize(
        "typed, lands_on",
        [
            ("acme", "amy"),            # bare node -> the leader
            ("acme:amy", "amy"),        # the leader by name
            ("acme:bob", "bob"),        # a member that takes ingress
            ("ACME:Bob", "bob"),        # case is not part of an address
        ],
    )
    def test_it_delivers(self, acme, typed, lands_on):
        handle_message(acme, "clancy", typed, "hello", drain_after=False)
        assert queued(lands_on) == 1
        assert dead_reasons() == []

    @pytest.mark.parametrize(
        "typed, reason",
        [
            ("acme:dana", "no-ingress"),       # a member behind the wall
            ("acme:eng", "cell-deferred"),     # a cell, until #183
            ("acme:nobody", "unknown-recipient"),
        ],
    )
    def test_it_refuses(self, acme, typed, reason):
        handle_message(acme, "clancy", typed, "hello", drain_after=False)
        assert dead_reasons() == [reason]
        assert [queued(n) for n in ("amy", "bob", "dana")] == [0, 0, 0]

    def test_an_alias_delivery_still_enters_at_the_leader(self, acme):
        # a8s may hand over a `to` that is not this node's prefix at all (an
        # alias, the bare agent name). Nothing there named a member, so it is
        # the node's own mail.
        handle_message(acme, "clancy", "everyone", "all hands", drain_after=False)
        assert queued("amy") == 1


class TestTheRefusalsSayWhatToDoInstead:
    def test_a_walled_member_names_the_leader_and_the_field(self, acme, tells):
        sent, _ = tells
        handle_message(acme, "clancy", "acme:dana", "do this", drain_after=False)
        body = "\n".join(b for _, b in sent)
        assert "dana does not accept ingress" in body
        assert "send to acme" in body
        assert "- **Ingress:** yes" in body

    def test_a_cell_names_the_deferral(self, acme, tells):
        sent, _ = tells
        handle_message(acme, "clancy", "acme:eng", "standup in 10", drain_after=False)
        body = "\n".join(b for _, b in sent)
        assert "eng names a cell" in body
        assert "#183" in body

    def test_an_unknown_sub_address_lists_the_members(self, acme, tells):
        sent, _ = tells
        handle_message(acme, "clancy", "acme:nobody", "hi", drain_after=False)
        body = "\n".join(b for _, b in sent)
        assert "has no member or cell named 'nobody'" in body
        assert "amy" in body and "bob" in body

    def test_each_refusal_leaves_one_ticker_line(self, acme, capsys):
        ticker = DispatchContext(
            root=acme.root, node=acme.node, roster_path=acme.roster_path,
            config_path=acme.config_path, tell_fn=acme.tell_fn, ticker=True,
        )
        handle_message(ticker, "clancy", "acme:dana", "hi", drain_after=False)
        handle_message(ticker, "clancy", "acme:eng", "hi", drain_after=False)
        lines = [l for l in capsys.readouterr().out.splitlines() if "REFUSED" in l]
        assert lines == [
            "r4t: REFUSED dana no ingress; from clancy",
            "r4t: REFUSED eng cell fan-out is deferred (#183)",
        ]


# --- vantage B: a member inside the walls ------------------------------------


@pytest.fixture
def inside(r4t_home, node_dir, chatty_config, chatty_harness, tells, monkeypatch):
    """A sender: `inside(member, to)` runs one turn for `member` whose harness
    writes a single tell to `to`, exactly as `tell` inside the cage writes the
    per-turn staging outbox. r4t owns the routing; nothing validates it there.
    """
    _sent, capture = tells
    ctx = DispatchContext(
        root=node_dir,
        node=NODE,
        roster_path=node_dir / runbook.RUNBOOK_NAME,
        config_path=chatty_config,
        tell_fn=capture,
    )
    monkeypatch.setenv("CHATTY_SENDS", "1")
    monkeypatch.setenv("CHATTY_BODY", "from inside the walls")

    def send(member: str, to: str, *, sender: str = "clancy") -> None:
        monkeypatch.setenv("CHATTY_TO", to)
        handle_message(ctx, sender, f"acme:{member}", "your turn", drain_after=False)
        drain(ctx)

    return send


class TestInsideTheWalls:
    def test_a_bare_name_prefers_the_roster_member(self, inside, node_dir):
        # The shadowing ruling: a global a8s agent named `bob` exists, and
        # inside `acme` the member wins. Nothing leaves.
        inside("amy", "bob")
        assert queued("bob") == 1
        assert outbox(node_dir) == []

    def test_a_leading_colon_means_the_global_one(self, inside, node_dir):
        # The escape hatch, and the only case in the grammar where the colon
        # is mandatory: `:bob` is the outside node, not the member.
        inside("amy", ":bob")
        assert queued("bob") == 0
        assert [e["to"] for e in outbox(node_dir)] == ["bob"]

    def test_the_colon_is_stripped_on_the_way_out(self, inside, node_dir):
        # `to` is canonicalized: the marker is relative to the sender's
        # vantage, and once resolved the address is absolute. No recipient
        # ever has to strip a colon to reply.
        inside("amy", ":clancy")
        assert [e["to"] for e in outbox(node_dir)] == ["clancy"]

    def test_an_unknown_bare_name_still_falls_back_outward(self, inside, node_dir):
        inside("amy", "clancy")
        assert [e["to"] for e in outbox(node_dir)] == ["clancy"]

    def test_qualifying_your_own_node_short_circuits(self, inside, node_dir):
        inside("amy", "acme:bob")
        assert queued("bob") == 1
        assert outbox(node_dir) == []

    def test_qualifying_your_own_node_globally_goes_out_and_back(
        self, inside, node_dir
    ):
        # `:acme:bob` is not a short circuit: a leading colon means the
        # address leaves the walls, so it comes back at the ingress gate.
        inside("amy", ":acme:bob")
        assert queued("bob") == 0
        assert [e["to"] for e in outbox(node_dir)] == ["acme:bob"]

    @pytest.mark.parametrize("bad", [":", "::bob"])
    def test_a_colon_that_takes_no_name_is_refused(self, inside, node_dir, bad):
        inside("amy", bad)
        assert outbox(node_dir) == []
        assert "bad-address" in dead_reasons()

    def test_the_deliberate_global_is_not_logged_as_a_typo(self, inside):
        inside("bob", ":clancy")
        log = "".join(
            f.read_text(encoding="utf-8")
            for f in (state.roster_dir(NODE) / "log").glob("*.md")
        )
        assert "UNKNOWN-MEMBER" not in log


# --- the trust ceiling --------------------------------------------------------


BYPASS = """\
## Roster

### amy
- **Engine:** claude --permissions bypass
- **Leader:** yes
"""


class TestTheTrustCeiling:
    def test_the_default_is_auto(self, r4t_home):
        assert machine_ceiling("nobody") == "auto"
        assert machine_ceiling(None) == "auto"

    def test_a_runbook_cannot_raise_its_own_permissions(self, r4t_home, tmp_path):
        root = tmp_path / "n"
        root.mkdir()
        write_roster(root, BYPASS)
        member = runbook.load_for_root(root, node="acme").roster.find("amy")
        assert member.rig_override is None
        assert any("above the trust ceiling" in e for e in member.errors)
        assert any("r4t add <dir> --trust" in e for e in member.errors)

    def test_trust_raises_it_for_that_node_only(self, r4t_home, tmp_path):
        root = tmp_path / "n"
        root.mkdir()
        write_roster(root, BYPASS)
        raise_machine_ceiling("acme")
        assert runbook.load_for_root(root, node="acme").roster.find("amy").errors == []
        assert runbook.load_for_root(root, node="other").roster.find("amy").errors

    def test_a_rig_block_is_capped_too(self, r4t_home, tmp_path):
        root = tmp_path / "n"
        root.mkdir()
        write_roster(
            root,
            "## Roster\n\n### amy\n- **Rig:** big\n- **Leader:** yes\n"
            "\n## Rigs\n\n### big\n- **Engine:** claude --permissions bypass\n",
        )
        rig = runbook.load_for_root(root, node="acme").rigs["big"]
        assert "above the trust ceiling" in (rig.error or "")
        assert rig.permissions is None

    def test_the_ceiling_is_rechecked_every_turn(self, acme, node_dir, tells):
        # An untrusted node whose runbook is edited to `bypass` after
        # registration fails closed at the next wake, not at the next add.
        sent, _ = tells
        handle_message(acme, "clancy", "acme", "before", drain_after=False)
        assert queued("amy") == 1
        write_roster(node_dir, BYPASS)
        handle_message(acme, "clancy", "acme", "after", drain_after=False)
        assert "member-disabled" in dead_reasons()
        assert any("above the trust ceiling" in b for _, b in sent)

    def test_the_machine_config_is_not_capped(self, r4t_home, tmp_path):
        # The ceiling caps what a REPO may ask for. A machine rig is the
        # operator's own out-of-repo choice and is untouched.
        from rig import build_preset_invoke, load_rig_config

        path = tmp_path / "rigs.json"
        path.write_text(
            json.dumps({
                "big": {
                    "preset": "claude",
                    "invoke": build_preset_invoke("claude"),
                    "permissions": "bypass",
                }
            }),
            encoding="utf-8",
        )
        assert load_rig_config(path).rigs["big"].permissions == "bypass"


# --- `r4t add` ----------------------------------------------------------------


@pytest.fixture
def a8s_calls(monkeypatch):
    """What `r4t add` asks a8s to do. Stubbed so the suite never spawns a
    node daemon; the argv IS the contract this commit is about."""
    calls: list[list[str]] = []

    def fake(*argv: str):
        calls.append(list(argv))
        return 0, ""

    monkeypatch.setattr("r4t._a8s", fake)
    return calls


class TestAdd:
    def run(self, *argv):
        return r4t_main(list(argv))

    def test_one_name_is_the_agent_the_namespace_and_the_node(
        self, r4t_home, node_dir, rig_config, a8s_calls
    ):
        assert self.run(
            "add", str(node_dir), "--rig-config", str(rig_config)
        ) == 0
        assert a8s_calls == [
            ["add", "acme", str(node_dir), "r4t"],
            ["namespace", "acme", "acme"],   # the self-bind: no `-node` suffix
            ["start", "acme"],
        ]
        assert state.read_root("acme") == node_dir

    def test_a_built_in_runbook_is_recorded_out_of_the_repo(
        self, r4t_home, tmp_path, a8s_calls
    ):
        root = tmp_path / "bare"
        root.mkdir()
        assert self.run("add", str(root), "triforce") == 0
        assert state.read_runbook("bare") == runbook.BUILTIN_DIR / "triforce.md"
        assert list(root.iterdir()) == []
        assert resolve_roster_path(root, None, "bare").name == "triforce.md"
        assert load_roster(resolve_roster_path(root, None, "bare")).leader().name == "Lead"

    def test_the_dirs_own_runbook_is_not_recorded(
        self, r4t_home, node_dir, rig_config, a8s_calls
    ):
        # Nothing to keep true twice: the file at the node dir is the answer,
        # and it wins over anything `add` was told anyway.
        assert self.run(
            "add", str(node_dir), "--rig-config", str(rig_config)
        ) == 0
        assert state.read_runbook("acme") is None

    def test_a_name_already_on_the_network_is_refused(
        self, r4t_home, node_dir, monkeypatch, capsys, a8s_calls
    ):
        monkeypatch.setattr("r4t.visible_a8s_names", lambda: {"acme": "node"})
        assert self.run("add", str(node_dir)) == 1
        assert a8s_calls == []
        assert "already names an a8s node" in capsys.readouterr().err

    def test_it_refuses_a_name_with_a_colon(self, r4t_home, node_dir, capsys, a8s_calls):
        assert self.run("add", str(node_dir), "--name", "a:b") == 2
        assert "is not a node name" in capsys.readouterr().err

    def test_it_refuses_a_directory_with_no_runbook(
        self, r4t_home, tmp_path, capsys, a8s_calls
    ):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert self.run("add", str(empty)) == 2
        err = capsys.readouterr().err
        assert "carries no r4t.md" in err
        assert "triforce" in err

    def test_it_refuses_an_unknown_runbook_by_listing_the_built_ins(
        self, r4t_home, tmp_path, capsys, a8s_calls
    ):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert self.run("add", str(empty), "nosuch") == 2
        assert "names no built-in runbook" in capsys.readouterr().err

    def test_a_named_runbook_beside_the_dirs_own_is_refused(
        self, r4t_home, node_dir, capsys, a8s_calls
    ):
        assert self.run("add", str(node_dir), "triforce") == 2
        assert "would never run" in capsys.readouterr().err

    def test_a_member_sharing_the_node_name_must_be_the_leader(
        self, r4t_home, tmp_path, capsys, a8s_calls
    ):
        root = tmp_path / "acme"
        root.mkdir()
        write_roster(
            root,
            "## Roster\n\n### amy\n- **Engine:** claude\n- **Leader:** yes\n"
            "\n### acme\n- **Engine:** claude\n",
        )
        assert self.run("add", str(root)) == 2
        assert "shares the node name but is not the leader" in capsys.readouterr().err

    def test_a_broken_runbook_registers_nothing(
        self, r4t_home, tmp_path, capsys, a8s_calls
    ):
        root = tmp_path / "acme"
        root.mkdir()
        write_roster(root, "## Roster\n\n### amy\n- **Engine:** claude\n")
        assert self.run("add", str(root)) == 2
        assert "nothing registered" in capsys.readouterr().err
        # A refused add leaves nothing behind: no phantom roster in
        # `r4t status`, and no ceiling waiting for a node that never arrived.
        assert state.read_runbook("acme") is None
        assert state.read_root("acme") is None
        assert not state.roster_dir("acme").exists()

    def test_bypass_needs_trust(self, r4t_home, tmp_path, capsys, a8s_calls):
        root = tmp_path / "acme"
        root.mkdir()
        write_roster(root, BYPASS)
        assert self.run("add", str(root)) == 2
        assert "above the trust ceiling" in capsys.readouterr().out
        assert state.read_trust("acme") is None

    def test_trust_records_the_ceiling_on_the_machine_not_the_repo(
        self, r4t_home, tmp_path, a8s_calls
    ):
        root = tmp_path / "acme"
        root.mkdir()
        write_roster(root, BYPASS)
        self.run("add", str(root), "--trust")
        assert state.read_trust("acme") == "bypass"
        assert sorted(p.name for p in root.iterdir()) == ["r4t.md"]
