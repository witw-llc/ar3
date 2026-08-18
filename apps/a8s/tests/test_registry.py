"""Tests for registry.py — schema I/O, alias resolution, sender lookup,
marker-file scan."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from registry import (
    _load_raw_registry,
    _scan_for_markers,
    find_participant,
    load_aliases,
    load_namespaces,
    load_registry,
    parse_name,
    participants_from_registry,
    resolve_name,
    resolve_recipient,
    save_aliases,
    save_namespaces,
    save_registry,
    sender_from_cwd,
    split_namespace_address,
)
from core import Participant, canonical_name, registry_path


# ---------- canonical_name ----------

class TestCanonicalName:
    """Issue #65 — names canonicalize to lowercase at registration boundaries
    (`a8s add`, `a8s alias`) so directory keys collapse and the case-collision
    footgun (two distinct `~/.a8s/agents/<NAME>/` dirs for `claude` vs
    `Claude`) goes away."""

    def test_lowercases(self):
        assert canonical_name("CLAUDE") == "claude"

    def test_strips(self):
        assert canonical_name("  claude  ") == "claude"

    def test_mixed_case(self):
        assert canonical_name("Claude") == "claude"

    def test_alphanumeric_passes(self):
        assert canonical_name("agent42") == "agent42"

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            canonical_name("")

    def test_whitespace_only_rejected(self):
        with pytest.raises(ValueError):
            canonical_name("   ")

    def test_hyphen_accepted(self):
        assert canonical_name("my-device") == "my-device"

    def test_underscore_accepted(self):
        assert canonical_name("foo_bar") == "foo_bar"

    def test_space_rejected(self):
        with pytest.raises(ValueError):
            canonical_name("foo bar")

    def test_leading_separator_rejected(self):
        with pytest.raises(ValueError):
            canonical_name("-foo")
        with pytest.raises(ValueError):
            canonical_name("_foo")


# ---------- low-level I/O ----------

class TestRegistryIO:
    def test_empty_registry_returns_zero_sections(self, fake_home):
        raw = _load_raw_registry()
        assert raw == {"agents": {}, "aliases": {}, "namespaces": {}, "namespace_options": {}}

    def test_save_and_load_agents(self, fake_home):
        save_registry({"CLAUDE": {"root": "/tmp/x", "definition": "/tmp/d.json"}})
        assert load_registry() == {"CLAUDE": {"root": "/tmp/x", "definition": "/tmp/d.json"}}

    def test_save_agents_preserves_aliases(self, fake_home):
        save_aliases({"devs": ["A", "B"]})
        save_registry({"X": {"root": "/r"}})
        assert load_aliases() == {"devs": ["A", "B"]}
        assert load_registry() == {"X": {"root": "/r"}}

    def test_save_aliases_preserves_agents(self, fake_home):
        save_registry({"X": {"root": "/r"}})
        save_aliases({"devs": ["X"]})
        assert load_registry() == {"X": {"root": "/r"}}
        assert load_aliases() == {"devs": ["X"]}

    def test_save_and_load_namespaces(self, fake_home):
        save_namespaces({"acme": "node"})
        assert load_namespaces() == {"acme": "node"}

    def test_save_namespaces_preserves_other_sections(self, fake_home):
        save_registry({"X": {"root": "/r"}})
        save_aliases({"devs": ["X"]})
        save_namespaces({"acme": "X"})
        assert load_registry() == {"X": {"root": "/r"}}
        assert load_aliases() == {"devs": ["X"]}

    def test_save_agents_preserves_namespaces(self, fake_home):
        save_namespaces({"acme": "X"})
        save_registry({"X": {"root": "/r"}})
        assert load_namespaces() == {"acme": "X"}

    def test_corrupt_file_returns_empty(self, fake_home):
        registry_path().write_text("not json")
        assert _load_raw_registry() == {"agents": {}, "aliases": {}, "namespaces": {}, "namespace_options": {}}

    def test_non_dict_top_level_returns_empty(self, fake_home):
        registry_path().write_text("[1, 2, 3]")
        assert _load_raw_registry() == {"agents": {}, "aliases": {}, "namespaces": {}, "namespace_options": {}}

    def test_missing_sections_default_empty(self, fake_home):
        # No migration code (pre-v1): a registry written before namespaces
        # existed just reads back with an empty section.
        registry_path().write_text(json.dumps({"agents": {"X": {"root": "/r"}}}))
        raw = _load_raw_registry()
        assert raw["agents"] == {"X": {"root": "/r"}}
        assert raw["aliases"] == {}
        assert raw["namespaces"] == {}


# ---------- resolve_name ----------

class TestResolveName:
    def test_agent_name_resolves_to_self(self, fake_home):
        save_registry({"CLAUDE": {"root": "/r"}})
        kind, members = resolve_name("CLAUDE")
        assert kind == "agent"
        assert members == ["CLAUDE"]

    def test_case_insensitive(self, fake_home):
        save_registry({"CLAUDE": {"root": "/r"}})
        kind, members = resolve_name("claude")
        assert kind == "agent"
        assert members == ["CLAUDE"]  # canonical casing

    def test_unknown_raises(self, fake_home):
        with pytest.raises(KeyError):
            resolve_name("BOGUS")

    def test_alias_expands(self, fake_home):
        save_registry({"A": {"root": "/a"}, "B": {"root": "/b"}})
        save_aliases({"devs": ["A", "B"]})
        kind, members = resolve_name("devs")
        assert kind == "alias"
        assert members == ["A", "B"]

    def test_nested_alias(self, fake_home):
        save_registry({"A": {"root": "/a"}, "B": {"root": "/b"}, "C": {"root": "/c"}})
        save_aliases({"inner": ["A", "B"], "outer": ["inner", "C"]})
        kind, members = resolve_name("outer")
        assert kind == "alias"
        assert members == ["A", "B", "C"]

    def test_nested_alias_dedupes(self, fake_home):
        save_registry({"A": {"root": "/a"}, "B": {"root": "/b"}})
        save_aliases({"x": ["A"], "y": ["A", "B"], "z": ["x", "y"]})
        kind, members = resolve_name("z")
        # A appears via both x and y; should be listed only once.
        assert members == ["A", "B"]

    def test_cycle_raises(self, fake_home):
        save_registry({"A": {"root": "/a"}})
        # x -> y -> x
        save_aliases({"x": ["y"], "y": ["x"]})
        with pytest.raises(ValueError, match="alias cycle"):
            resolve_name("x")

    def test_dangling_reference_raises(self, fake_home):
        # alias points at an agent that doesn't exist
        save_aliases({"devs": ["MISSING"]})
        with pytest.raises(KeyError):
            resolve_name("devs")


# ---------- split_namespace_address ----------

class TestSplitNamespaceAddress:
    def test_no_colon_returns_none(self):
        assert split_namespace_address("claude") is None

    def test_splits_on_first_colon(self):
        assert split_namespace_address("acme:ops:phil") == ("acme", "ops:phil")

    def test_prefix_canonicalized_sub_verbatim(self):
        assert split_namespace_address("ACME:Phil") == ("acme", "Phil")

    def test_empty_sub_address_raises(self):
        with pytest.raises(ValueError, match="empty sub-address"):
            split_namespace_address("acme:")

    def test_empty_prefix_raises(self):
        with pytest.raises(ValueError, match="invalid prefix"):
            split_namespace_address(":phil")

    def test_invalid_prefix_raises(self):
        with pytest.raises(ValueError, match="invalid prefix"):
            split_namespace_address("s1 l:phil")


# ---------- resolve_name — namespaces (#148) ----------

class TestResolveNamespace:
    def test_colon_address_resolves_to_bound_agent(self, fake_home):
        save_registry({"NODE": {"root": "/r"}})
        save_namespaces({"acme": "NODE"})
        kind, members = resolve_name("acme:phil")
        assert kind == "namespace"
        assert members == ["NODE"]

    def test_prefix_match_is_case_insensitive(self, fake_home):
        save_registry({"NODE": {"root": "/r"}})
        save_namespaces({"acme": "NODE"})
        assert resolve_name("ACME:Phil") == ("namespace", ["NODE"])

    def test_further_colons_are_opaque(self, fake_home):
        save_registry({"NODE": {"root": "/r"}})
        save_namespaces({"acme": "NODE"})
        assert resolve_name("acme:ops:phil") == ("namespace", ["NODE"])

    def test_unknown_prefix_raises_keyerror(self, fake_home):
        with pytest.raises(KeyError):
            resolve_name("bogus:phil")

    def test_empty_sub_address_raises_valueerror(self, fake_home):
        save_registry({"NODE": {"root": "/r"}})
        save_namespaces({"acme": "NODE"})
        with pytest.raises(ValueError, match="empty sub-address"):
            resolve_name("acme:")

    def test_dangling_bound_agent_raises_keyerror(self, fake_home):
        # Same dangling shape as an alias member that no longer exists.
        save_namespaces({"acme": "GONE"})
        with pytest.raises(KeyError, match="unknown agent"):
            resolve_name("acme:phil")

    def test_bare_prefix_resolves_to_bound_agent(self, fake_home):
        save_registry({"NODE": {"root": "/r"}})
        save_namespaces({"acme": "NODE"})
        assert resolve_name("acme") == ("namespace", ["NODE"])

    def test_bare_prefix_case_insensitive(self, fake_home):
        save_registry({"NODE": {"root": "/r"}})
        save_namespaces({"acme": "NODE"})
        assert resolve_name("ACME") == ("namespace", ["NODE"])

    def test_self_owned_namespace_both_forms_resolve_to_node(self, fake_home):
        # #175: a node IS `s1l` — agent `s1l` binds prefix `s1l` to itself.
        # Bare and colon forms both land on the one node; namespace precedence
        # over the agent name is intentional and harmless here.
        save_registry({"s1l": {"root": "/r"}})
        save_namespaces({"s1l": "s1l"})
        assert resolve_name("s1l") == ("namespace", ["s1l"])
        assert resolve_name("s1l:gerry") == ("namespace", ["s1l"])


# ---------- find_participant + sender_from_cwd ----------

class TestFindParticipant:
    def test_finds_by_exact_name(self):
        parts = [Participant("CLAUDE", Path("/r")), Participant("GEMINI", Path("/r2"))]
        assert find_participant(parts, "CLAUDE").name == "CLAUDE"

    def test_case_insensitive(self):
        parts = [Participant("CLAUDE", Path("/r"))]
        assert find_participant(parts, "claude").name == "CLAUDE"

    def test_unknown_returns_none(self):
        assert find_participant([], "X") is None


class TestSenderFromCwd:
    def test_finds_agent_from_inside_root(self, fake_home, tmp_path, monkeypatch):
        agent_dir = tmp_path / "myagent"
        agent_dir.mkdir()
        save_registry({"X": {"root": str(agent_dir)}})
        monkeypatch.chdir(agent_dir)
        result = sender_from_cwd()
        assert result is not None
        name, info = result
        assert name == "X"

    def test_finds_agent_from_subdir(self, fake_home, tmp_path, monkeypatch):
        agent_dir = tmp_path / "myagent"
        sub = agent_dir / "sub" / "deeper"
        sub.mkdir(parents=True)
        save_registry({"X": {"root": str(agent_dir)}})
        monkeypatch.chdir(sub)
        name, _ = sender_from_cwd()
        assert name == "X"

    def test_outside_any_agent_returns_none(self, fake_home, tmp_path, monkeypatch):
        agent_dir = tmp_path / "myagent"
        agent_dir.mkdir()
        save_registry({"X": {"root": str(agent_dir)}})
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        monkeypatch.chdir(outside)
        assert sender_from_cwd() is None


class TestResolveRecipient:
    def test_finds_agent(self, fake_home):
        save_registry({"CLAUDE": {"root": "/r"}})
        result = resolve_recipient("claude")
        assert result is not None
        name, info = result
        assert name == "CLAUDE"

    def test_does_not_resolve_aliases(self, fake_home):
        # resolve_recipient is exact-agent only; alias expansion is resolve_name.
        save_aliases({"devs": ["X"]})
        assert resolve_recipient("devs") is None


# ---------- participants_from_registry ----------

class TestParticipantsFromRegistry:
    def test_empty(self, fake_home):
        assert participants_from_registry() == []

    def test_builds_participants(self, fake_home, tmp_path):
        d1 = tmp_path / "a"; d1.mkdir()
        d2 = tmp_path / "b"; d2.mkdir()
        save_registry({"A": {"root": str(d1)}, "B": {"root": str(d2)}})
        parts = participants_from_registry()
        names = sorted(p.name for p in parts)
        assert names == ["A", "B"]

    def test_skips_entries_with_no_root(self, fake_home):
        save_registry({"A": {"root": "/tmp"}, "B": {}})
        parts = participants_from_registry()
        assert {p.name for p in parts} == {"A"}

    def test_loads_safe_dirs(self, fake_home, tmp_path):
        root = tmp_path / "a"
        root.mkdir()
        drop = tmp_path / "drop"
        drop.mkdir()
        save_registry({"A": {"root": str(root), "safe_dirs": [str(drop), ""]}})
        parts = participants_from_registry()
        assert len(parts) == 1
        assert parts[0].safe_dirs == (drop.resolve(),)

    def test_resolves_outbox_dir_from_definition(self, fake_home, tmp_path):
        import json

        root = tmp_path / "agent"
        root.mkdir()
        external = tmp_path / "external-outbox"
        defn = tmp_path / "def.json"
        defn.write_text(
            json.dumps({"invoke": ["echo", "x"], "outbox_dir": str(external)})
        )
        save_registry({"A": {"root": str(root), "definition": str(defn)}})
        parts = participants_from_registry()
        assert len(parts) == 1
        assert parts[0].outbox_path() == external.resolve()

    def test_resolves_files_dir_from_definition(self, fake_home, tmp_path):
        import json

        root = tmp_path / "agent"
        root.mkdir()
        external = tmp_path / "attachments"
        defn = tmp_path / "def.json"
        defn.write_text(
            json.dumps({"invoke": ["echo", "x"], "files_dir": str(external)})
        )
        save_registry({"A": {"root": str(root), "definition": str(defn)}})
        parts = participants_from_registry()
        assert len(parts) == 1
        assert parts[0].files_path() == external.resolve()

    def test_resolves_inbox_dir_from_definition(self, fake_home, tmp_path):
        import json

        root = tmp_path / "agent"
        root.mkdir()
        external = tmp_path / "external-inbox"
        defn = tmp_path / "def.json"
        defn.write_text(
            json.dumps({"proxy": "file", "inbox_dir": str(external)})
        )
        save_registry({"A": {"root": str(root), "definition": str(defn)}})
        parts = participants_from_registry()
        assert len(parts) == 1
        assert parts[0].inbox_path() == external.resolve()


class TestUnresolvableNodes:
    """A path field naming an unset var makes the node unresolvable, and
    unresolvable means not participating. Falling back to `.outbox` would put
    two nodes on one root back in one directory — silently."""

    def _register(self, tmp_path, spec_by_name):
        import json

        root = tmp_path / "repo"
        root.mkdir(exist_ok=True)
        reg = {}
        for name, spec in spec_by_name.items():
            defn = tmp_path / f"{name}.json"
            body = {"invoke": ["echo", "x"]}
            if spec is not None:
                body["outbox_dir"] = spec
            defn.write_text(json.dumps(body))
            reg[name] = {"root": str(root), "definition": str(defn)}
        save_registry(reg)
        return root

    def test_unresolvable_node_is_skipped(self, fake_home, tmp_path):
        self._register(tmp_path, {"bad": ".outbox-$SEAT", "good": ".outbox-$NODE"})
        assert {p.name for p in participants_from_registry()} == {"good"}

    def test_unresolvable_node_does_not_fall_back_to_the_default(self, fake_home, tmp_path):
        root = self._register(tmp_path, {"bad": ".outbox-$SEAT", "plain": None})
        parts = {p.name: p for p in participants_from_registry()}
        assert "bad" not in parts
        assert parts["plain"].outbox_path() == (root / ".outbox").resolve()

    def test_reason_names_the_field_and_the_var(self, fake_home, tmp_path):
        from registry import unresolved_mailboxes

        self._register(tmp_path, {"bad": ".outbox-$SEAT", "good": ".outbox-$NODE"})
        problems = unresolved_mailboxes()
        assert set(problems) == {"bad"}
        assert "outbox_dir" in problems["bad"]
        assert "$SEAT" in problems["bad"]

    def test_setting_the_var_makes_it_resolve(self, fake_home, tmp_path):
        from registry import unresolved_mailboxes

        root = self._register(tmp_path, {"bad": ".outbox-$SEAT"})
        reg = load_registry()
        reg["bad"]["vars"] = {"SEAT": "a"}
        save_registry(reg)
        assert unresolved_mailboxes() == {}
        parts = participants_from_registry()
        assert parts[0].outbox_path() == (root / ".outbox-a").resolve()


# ---------- parse_name + _scan_for_markers ----------

class TestParseName:
    def test_first_hash_line(self, tmp_path):
        f = tmp_path / "CLAUDE.md"
        f.write_text("# CLAUDE: code review\n\nbody\n")
        assert parse_name(f) == "CLAUDE"

    def test_skips_blank_then_finds(self, tmp_path):
        f = tmp_path / "CLAUDE.md"
        f.write_text("\n\n# Llama: local\n")
        assert parse_name(f) == "Llama"

    def test_no_hash_line_returns_none(self, tmp_path):
        f = tmp_path / "CLAUDE.md"
        f.write_text("plain text\nno headings\n")
        assert parse_name(f) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert parse_name(tmp_path / "nope.md") is None


class TestScanForMarkers:
    def test_finds_marker_in_immediate_child(self, tmp_path):
        d = tmp_path / "agent1"; d.mkdir()
        (d / "CLAUDE.md").write_text("# A: x\n")
        found = _scan_for_markers(tmp_path)
        assert len(found) == 1
        name, kind, dirpath = found[0]
        assert name == "A"
        assert kind == "claude"
        assert dirpath == d.resolve()

    def test_finds_marker_in_root_itself(self, tmp_path):
        (tmp_path / "GEMINI.md").write_text("# G: y\n")
        found = _scan_for_markers(tmp_path)
        assert len(found) == 1
        assert found[0][1] == "agy"

    def test_no_markers(self, tmp_path):
        assert _scan_for_markers(tmp_path) == []

    def test_one_marker_per_dir(self, tmp_path):
        # If both CLAUDE.md and GEMINI.md exist, _scan_for_markers picks the
        # FIRST marker iteration finds (per MARKER_FILES order).
        d = tmp_path / "x"; d.mkdir()
        (d / "CLAUDE.md").write_text("# A: x\n")
        (d / "GEMINI.md").write_text("# B: y\n")
        found = _scan_for_markers(tmp_path)
        # Only one entry per directory (the break in _scan_for_markers).
        assert len(found) == 1

    def test_finds_agents_md_as_opencode_fallback(self, tmp_path):
        d = tmp_path / "agent1"; d.mkdir()
        (d / "AGENTS.md").write_text("# O: opencode helper\n")
        found = _scan_for_markers(tmp_path)
        assert len(found) == 1
        name, kind, dirpath = found[0]
        assert name == "O"
        assert kind == "opencode"
        assert dirpath == d.resolve()

    def test_specific_marker_wins_over_agents_md(self, tmp_path):
        d = tmp_path / "agent1"; d.mkdir()
        (d / "CLAUDE.md").write_text("# A: x\n")
        (d / "AGENTS.md").write_text("# B: y\n")
        found = _scan_for_markers(tmp_path)
        assert len(found) == 1
        assert found[0][1] == "claude"

    def test_cursor_marker_wins_over_agents_md(self, tmp_path):
        d = tmp_path / "agent1"; d.mkdir()
        (d / "CURSOR.md").write_text("# C: x\n")
        (d / "AGENTS.md").write_text("# B: y\n")
        found = _scan_for_markers(tmp_path)
        assert len(found) == 1
        assert found[0][1] == "cursor"
