"""Tests for definitions.py — single-verb argv interpolation, age formatting,
and auto-discovery."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from datetime import datetime, timezone

from definitions import (
    UndefinedVarsError,
    _autodiscover_definition,
    _expand_argv,
    _format_age,
    _message_body,
    build_command,
    default_definition_path,
    load_definition,
    resolve_definition_arg,
    resolve_files_dir,
    resolve_inbox_dir,
    resolve_outbox_dir,
    validate_var_name,
)


# ---------- _format_age ----------

class TestFormatAge:
    NOW = datetime(2026, 4, 28, 14, 30, 0, tzinfo=timezone.utc)

    def _ago(self, **kwargs):
        from datetime import timedelta
        return (self.NOW - timedelta(**kwargs)).isoformat().replace("+00:00", "Z")

    def test_seconds(self):
        assert _format_age(self._ago(seconds=5), now=self.NOW) == "5 seconds ago"

    def test_singular_second(self):
        assert _format_age(self._ago(seconds=1), now=self.NOW) == "1 second ago"

    def test_zero_seconds(self):
        assert _format_age(self._ago(seconds=0), now=self.NOW) == "0 seconds ago"

    def test_minutes(self):
        assert _format_age(self._ago(minutes=5), now=self.NOW) == "5 minutes ago"

    def test_singular_minute(self):
        assert _format_age(self._ago(minutes=1), now=self.NOW) == "1 minute ago"

    def test_hours(self):
        assert _format_age(self._ago(hours=3), now=self.NOW) == "3 hours ago"

    def test_days(self):
        assert _format_age(self._ago(days=2), now=self.NOW) == "2 days ago"

    def test_weeks(self):
        assert _format_age(self._ago(days=14), now=self.NOW) == "2 weeks ago"

    def test_future_clamps_to_zero(self):
        # Clock skew shouldn't produce negative ages.
        from datetime import timedelta
        future = (self.NOW + timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
        assert _format_age(future, now=self.NOW) == "0 seconds ago"

    def test_empty_string(self):
        assert _format_age("", now=self.NOW) == ""

    def test_unparseable(self):
        assert _format_age("not-a-date", now=self.NOW) == ""

    def test_iso_without_z_suffix(self):
        # `_write_outbox` writes `Z` but accept timezone-aware ISO too.
        ts = "2026-04-28T14:25:00+00:00"
        assert _format_age(ts, now=self.NOW) == "5 minutes ago"


# ---------- _message_body ----------

class TestMessageBody:
    @pytest.fixture
    def files_root(self, tmp_path):
        root = tmp_path / "agent"
        root.mkdir()
        return root / ".files"

    def test_content_only(self, files_root):
        assert _message_body({"content": "hello"}, files_root) == "hello"

    def test_content_with_files(self, files_root):
        msg = {
            "content": "see attached",
            "id": "01JTESTATTACH000000000000",
            "files": [{"filename": "build.log"}, {"filename": "data.csv"}],
        }
        base = files_root / "01JTESTATTACH000000000000"
        assert _message_body(msg, files_root) == (
            "see attached\n\n"
            f"ATTACHED FILE: {(base / 'build.log').resolve()}\n"
            f"ATTACHED FILE: {(base / 'data.csv').resolve()}"
        )

    def test_empty(self, files_root):
        assert _message_body({}, files_root) == ""


# ---------- build_command + _expand_argv ----------

class TestBuildCommand:
    @pytest.fixture
    def agent_root(self, tmp_path):
        root = tmp_path / "agent"
        root.mkdir()
        return root

    def test_substitutes_sender_recipient_message(self, agent_root):
        defn = {"invoke": ["claude", "--continue", "-p", "$SENDER tells $RECIPIENT: $MESSAGE"]}
        msg = {"from": "GERRY", "to": "CLAUDE", "content": "fix this"}
        argv = build_command(defn, msg, agent_root)
        assert argv == ["claude", "--continue", "-p", "GERRY tells CLAUDE: fix this"]

    def test_alias_routed_keeps_alias_in_recipient(self, agent_root):
        # Strict opacity / mailing-list semantics: when the sender wrote
        # `to: devs`, the recipient's $RECIPIENT resolves to "devs".
        defn = {"invoke": ["claude", "-p", "$SENDER tells $RECIPIENT: $MESSAGE"]}
        msg = {"from": "GERRY", "to": "devs", "content": "standup"}
        argv = build_command(defn, msg, agent_root)
        assert argv == ["claude", "-p", "GERRY tells devs: standup"]

    def test_namespace_routed_keeps_full_address_in_recipient(self, agent_root):
        # Issue #148: routing preserves the colon address in `to`, so the
        # bound node's $RECIPIENT carries it verbatim and the node can
        # self-route internally.
        defn = {"invoke": ["claude", "-p", "$SENDER tells $RECIPIENT: $MESSAGE"]}
        msg = {"from": "GERRY", "to": "acme:team:phil", "content": "ping"}
        argv = build_command(defn, msg, agent_root)
        assert argv == ["claude", "-p", "GERRY tells acme:team:phil: ping"]

    def test_missing_invoke_raises(self, agent_root):
        with pytest.raises(ValueError, match="invoke"):
            build_command({}, {"from": "G", "to": "C"}, agent_root)

    def test_a8s_dir_substitution(self, agent_root):
        from core import SCRIPT_DIR
        defn = {"invoke": ["$A8S_DIR/dummy-cli", "$MESSAGE"]}
        msg = {"from": "A", "to": "B", "content": "hi"}
        argv = build_command(defn, msg, agent_root)
        assert argv == [f"{SCRIPT_DIR}/dummy-cli", "hi"]

    def test_definition_path_substitution(self, agent_root):
        defn = {"invoke": ["r4t", "--definition", "$DEFINITION_PATH", "-p", "$MESSAGE"]}
        msg = {"from": "A", "to": "B", "content": "hi"}
        argv = build_command(defn, msg, agent_root, "/path/to/defn.json")
        assert argv == ["r4t", "--definition", "/path/to/defn.json", "-p", "hi"]

    def test_definition_path_defaults_empty(self, agent_root):
        defn = {"invoke": ["r4t", "$DEFINITION_PATH", "$MESSAGE"]}
        argv = build_command(defn, {"from": "A", "to": "B", "content": "hi"}, agent_root)
        assert argv == ["r4t", "", "hi"]

    def test_meta_expands_to_compact_json(self, agent_root):
        # #167: protocol metadata between nodes. a8s carries the object and
        # hands it over verbatim — the vocabulary is the nodes' business.
        defn = {"invoke": ["r4t", "--meta", "$META", "-p", "$MESSAGE"]}
        msg = {"from": "A", "to": "B", "content": "hi", "meta": {"class": "auto"}}
        argv = build_command(defn, msg, agent_root)
        assert argv == ["r4t", "--meta", '{"class":"auto"}', "-p", "hi"]

    def test_meta_absent_expands_empty(self, agent_root):
        defn = {"invoke": ["r4t", "--meta", "$META"]}
        argv = build_command(defn, {"from": "A", "to": "B", "content": "hi"}, agent_root)
        assert argv == ["r4t", "--meta", ""]

    def test_meta_non_object_expands_empty(self, agent_root):
        # A remote cluster wrote the envelope; a scalar `meta` is that
        # boundary's problem, not a crash in the wake.
        defn = {"invoke": ["r4t", "--meta", "$META"]}
        msg = {"from": "A", "to": "B", "content": "hi", "meta": "auto"}
        argv = build_command(defn, msg, agent_root)
        assert argv == ["r4t", "--meta", ""]

    def test_meta_value_is_not_reinterpolated(self, agent_root):
        defn = {"invoke": ["r4t", "--meta", "$META"]}
        msg = {"from": "A", "to": "B", "content": "hi", "meta": {"note": "$SENDER \\ x"}}
        argv = build_command(defn, msg, agent_root)
        assert argv[2] == '{"note":"$SENDER \\\\ x"}'

    def test_bundled_r4t_node_receives_the_class_on_its_argv(self, agent_root):
        # The seam itself: the shipped r4t definition forwards `$META`, so a
        # peer cluster's class reaches `r4t dispatch` without a8s reading it.
        defn = json.loads(default_definition_path("r4t").read_text(encoding="utf-8"))
        msg = {
            "from": "beta", "to": "acme", "content": "roster sync",
            "meta": {"class": "auto"},
        }
        argv = build_command(defn, msg, agent_root)
        assert argv[argv.index("--meta") + 1] == '{"class":"auto"}'

    def test_does_not_mutate_original_argv(self, agent_root):
        defn = {"invoke": ["claude", "-p", "$MESSAGE"]}
        original = list(defn["invoke"])
        build_command(defn, {"from": "A", "to": "B", "content": "hello"}, agent_root)
        assert defn["invoke"] == original

    def test_message_body_includes_files(self, agent_root):
        defn = {"invoke": ["x", "$MESSAGE"]}
        msg_id = "01JTESTATTACH000000000000"
        msg = {
            "from": "GERRY",
            "to": "CLAUDE",
            "content": "review",
            "id": msg_id,
            "files": [{"filename": "x"}],
        }
        argv = build_command(defn, msg, agent_root)
        path = (agent_root / ".files" / msg_id / "x").resolve()
        assert argv == ["x", f"review\n\nATTACHED FILE: {path}"]

    def test_message_body_uses_custom_files_dir(self, tmp_path):
        agent_root = tmp_path / "agent"
        agent_root.mkdir()
        external = tmp_path / "attachments"
        msg_id = "01JTESTATTACH000000000000"
        defn = {"invoke": ["x", "$MESSAGE"], "files_dir": str(external)}
        msg = {
            "from": "GERRY",
            "to": "CLAUDE",
            "content": "review",
            "id": msg_id,
            "files": [{"filename": "x"}],
        }
        argv = build_command(defn, msg, agent_root)
        path = (external / msg_id / "x").resolve()
        assert argv == ["x", f"review\n\nATTACHED FILE: {path}"]

    def test_timestamp_substitution_from_msg_date(self, agent_root):
        defn = {"invoke": ["x", "[$TIMESTAMP] $SENDER: $MESSAGE"]}
        msg = {
            "from": "GERRY",
            "to": "CLAUDE",
            "date": "2026-04-28T14:30:00.000000Z",
            "content": "hi",
        }
        argv = build_command(defn, msg, agent_root)
        assert argv == ["x", "[2026-04-28T14:30:00.000000Z] GERRY: hi"]

    def test_age_substitution_relative_to_now(self, agent_root, monkeypatch):
        from datetime import timedelta
        import definitions as dmod
        frozen = datetime(2026, 4, 28, 14, 35, 0, tzinfo=timezone.utc)
        msg_date = (frozen - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")

        class FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen
        monkeypatch.setattr(dmod, "datetime", FakeDT)

        defn = {"invoke": ["x", "($AGE) $MESSAGE"]}
        msg = {"from": "G", "to": "C", "date": msg_date, "content": "hi"}
        argv = build_command(defn, msg, agent_root)
        assert argv == ["x", "(5 minutes ago) hi"]

    def test_missing_date_yields_empty_age_and_timestamp(self, agent_root):
        defn = {"invoke": ["x", "TS:$TIMESTAMP", "AGE:$AGE", "$MESSAGE"]}
        msg = {"from": "G", "to": "C", "content": "hi"}
        argv = build_command(defn, msg, agent_root)
        assert argv == ["x", "TS:", "AGE:", "hi"]


class TestExpandArgv:
    def test_no_placeholders(self):
        assert _expand_argv(["claude", "-p", "literal"], "S", "R", "M") == [
            "claude", "-p", "literal",
        ]

    def test_message_substitution(self):
        assert _expand_argv(["x", "$MESSAGE", "y"], "", "", "hello") == ["x", "hello", "y"]

    def test_sender_recipient_message_in_one_arg(self):
        assert _expand_argv(["$SENDER->$RECIPIENT: $MESSAGE"], "A", "B", "hi") == ["A->B: hi"]

    def test_timestamp_and_age(self):
        argv = _expand_argv(
            ["[$TIMESTAMP][$AGE] $MESSAGE"],
            "A", "B", "hi",
            timestamp="2026-04-28T14:30:00Z",
            age="5 minutes ago",
        )
        assert argv == ["[2026-04-28T14:30:00Z][5 minutes ago] hi"]

    def test_a8s_dir_substitution(self):
        from core import SCRIPT_DIR
        assert _expand_argv(["$A8S_DIR/x"], "", "", "") == [f"{SCRIPT_DIR}/x"]

    def test_node_var_expands(self):
        assert _expand_argv(
            ["ollama", "--model", "$MODEL", "$MESSAGE"],
            "A",
            "B",
            "hi",
            vars={"MODEL": "qwen3.6"},
        ) == ["ollama", "--model", "qwen3.6", "hi"]

    def test_var_case_insensitive(self):
        assert _expand_argv(
            ["$model"],
            "A",
            "B",
            "hi",
            vars={"MoDeL": "qwen3.6"},
        ) == ["qwen3.6"]

    def test_undefined_var_raises(self):
        with pytest.raises(UndefinedVarsError) as ei:
            _expand_argv(["--model", "$MODEL"], "A", "B", "hi")
        assert ei.value.names == ["MODEL"]
        assert "$MODEL" in str(ei.value)

    def test_os_environ_does_not_fill_var(self, monkeypatch):
        monkeypatch.setenv("MODEL", "from-shell")
        with pytest.raises(UndefinedVarsError):
            _expand_argv(["$MODEL"], "A", "B", "hi")

    def test_builtin_not_overridden_by_vars_map(self):
        assert _expand_argv(
            ["$SENDER"],
            "alice",
            "bob",
            "x",
            vars={"SENDER": "eve"},
        ) == ["alice"]

    def test_lowercase_builtin_still_builtin(self):
        assert _expand_argv(["$message"], "A", "B", "hi") == ["hi"]

    def test_validate_var_name_rejects_builtin(self):
        with pytest.raises(ValueError, match="reserved"):
            validate_var_name("message")

    def test_validate_var_name_canonicalizes(self):
        assert validate_var_name("model") == "MODEL"

# ---------- resolve_definition_arg ----------

class TestResolveDefinitionArg:
    def test_bare_kind(self):
        assert resolve_definition_arg("filedrop") == default_definition_path("filedrop").resolve()

    def test_bare_kind_with_suffix(self):
        assert resolve_definition_arg("filedrop.json") == default_definition_path("filedrop").resolve()

    def test_explicit_file(self, tmp_path):
        custom = tmp_path / "mine.json"
        custom.write_text("{}")
        assert resolve_definition_arg(str(custom)) == custom.resolve()

    def test_bare_r4t_kind(self):
        assert resolve_definition_arg("r4t") == default_definition_path("r4t").resolve()

    def test_bare_name_not_shadowed_by_cwd_file(self, tmp_path, monkeypatch):
        (tmp_path / "r4t").write_text("#!/usr/bin/env bash\n")
        monkeypatch.chdir(tmp_path)
        assert resolve_definition_arg("r4t") == default_definition_path("r4t").resolve()
        assert resolve_definition_arg("./r4t") == (tmp_path / "r4t").resolve()

    def test_unknown_bare_raises(self):
        with pytest.raises(FileNotFoundError):
            resolve_definition_arg("no-such-definition-kind")

    def test_user_installed_bare(self, fake_home):
        from core import user_definitions_dir
        from definitions import list_definition_entries

        d = user_definitions_dir()
        d.mkdir(parents=True)
        custom = d / "my-custom-definition.json"
        custom.write_text('{"invoke": ["echo"]}')
        assert resolve_definition_arg("my-custom-definition") == custom.resolve()
        assert resolve_definition_arg("my-custom-definition.json") == custom.resolve()
        entries = {name: source for name, source, _ in list_definition_entries()}
        assert entries["my-custom-definition"] == "user"
        assert entries["filedrop"] == "builtin"

    def test_builtin_wins_over_user_same_stem(self, fake_home):
        from core import user_definitions_dir

        d = user_definitions_dir()
        d.mkdir(parents=True)
        (d / "filedrop.json").write_text('{"invoke": ["echo", "shadow"]}')
        assert resolve_definition_arg("filedrop") == default_definition_path("filedrop").resolve()


# ---------- _autodiscover_definition ----------

class TestAutodiscoverDefinition:
    def test_single_marker_uses_matching_builtin(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# X\n")
        path, note = _autodiscover_definition(tmp_path)
        assert path == str(default_definition_path("claude"))
        assert "auto-detected via CLAUDE.md" in note

    def test_no_marker_uses_default(self, tmp_path):
        path, note = _autodiscover_definition(tmp_path)
        assert path == str(default_definition_path("default"))
        assert "no marker file" in note

    def test_multiple_markers_uses_default(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# X\n")
        (tmp_path / "GEMINI.md").write_text("# Y\n")
        path, note = _autodiscover_definition(tmp_path)
        assert path == str(default_definition_path("default"))
        assert "multiple markers" in note
        assert "CLAUDE.md" in note and "GEMINI.md" in note

    def test_codex_marker(self, tmp_path):
        (tmp_path / "CODEX.md").write_text("# C\n")
        path, note = _autodiscover_definition(tmp_path)
        assert path == str(default_definition_path("codex"))

    def test_copilot_marker(self, tmp_path):
        # Copilot's marker is its native repo-instructions location, not an
        # invented `COPILOT.md` — see core.MARKER_FILES for rationale.
        gh = tmp_path / ".github"
        gh.mkdir()
        (gh / "copilot-instructions.md").write_text("# CP\n")
        path, note = _autodiscover_definition(tmp_path)
        assert path == str(default_definition_path("copilot"))
        assert "auto-detected via .github/copilot-instructions.md" in note

    def test_cursor_marker(self, tmp_path):
        (tmp_path / "CURSOR.md").write_text("# CR\n")
        path, note = _autodiscover_definition(tmp_path)
        assert path == str(default_definition_path("cursor"))
        assert "auto-detected via CURSOR.md" in note


# ---------- resolve_outbox_dir ----------

class TestResolveOutboxDir:
    def test_default_relative(self, tmp_path):
        root = tmp_path / "agent"
        root.mkdir()
        assert resolve_outbox_dir(root, {}) == (root / ".outbox").resolve()

    def test_explicit_relative(self, tmp_path):
        root = tmp_path / "agent"
        root.mkdir()
        assert resolve_outbox_dir(root, {"outbox_dir": "mail/out"}) == (
            root / "mail" / "out"
        ).resolve()

    def test_absolute(self, tmp_path):
        root = tmp_path / "agent"
        root.mkdir()
        external = tmp_path / "external"
        external.mkdir()
        assert resolve_outbox_dir(root, {"outbox_dir": str(external)}) == external.resolve()

    def test_rejects_non_string(self, tmp_path):
        with pytest.raises(ValueError, match="outbox_dir must be a string"):
            resolve_outbox_dir(tmp_path, {"outbox_dir": 1})

    def test_rejects_empty(self, tmp_path):
        with pytest.raises(ValueError, match="must not be empty"):
            resolve_outbox_dir(tmp_path, {"outbox_dir": "  "})


# ---------- resolve_files_dir ----------

class TestResolveFilesDir:
    def test_default_relative(self, tmp_path):
        root = tmp_path / "agent"
        root.mkdir()
        assert resolve_files_dir(root, {}) == (root / ".files").resolve()

    def test_explicit_relative(self, tmp_path):
        root = tmp_path / "agent"
        root.mkdir()
        assert resolve_files_dir(root, {"files_dir": "incoming"}) == (
            root / "incoming"
        ).resolve()

    def test_absolute(self, tmp_path):
        root = tmp_path / "agent"
        root.mkdir()
        external = tmp_path / "attachments"
        external.mkdir()
        assert resolve_files_dir(root, {"files_dir": str(external)}) == external.resolve()

    def test_rejects_non_string(self, tmp_path):
        with pytest.raises(ValueError, match="files_dir must be a string"):
            resolve_files_dir(tmp_path, {"files_dir": 1})

    def test_rejects_empty(self, tmp_path):
        with pytest.raises(ValueError, match="must not be empty"):
            resolve_files_dir(tmp_path, {"files_dir": "  "})


# ---------- resolve_inbox_dir ----------

class TestResolveInboxDir:
    def test_default_relative(self, tmp_path):
        root = tmp_path / "agent"
        root.mkdir()
        assert resolve_inbox_dir(root, {"proxy": "file"}) == (root / ".inbox").resolve()

    def test_explicit_relative(self, tmp_path):
        root = tmp_path / "agent"
        root.mkdir()
        assert resolve_inbox_dir(root, {"proxy": "file", "inbox_dir": "sync/in"}) == (
            root / "sync" / "in"
        ).resolve()

    def test_absolute(self, tmp_path):
        root = tmp_path / "agent"
        root.mkdir()
        external = tmp_path / "external-inbox"
        external.mkdir()
        assert resolve_inbox_dir(
            root, {"proxy": "file", "inbox_dir": str(external)}
        ) == external.resolve()

    def test_rejects_non_string(self, tmp_path):
        with pytest.raises(ValueError, match="inbox_dir must be a string"):
            resolve_inbox_dir(tmp_path, {"inbox_dir": 1})

    def test_rejects_empty(self, tmp_path):
        with pytest.raises(ValueError, match="must not be empty"):
            resolve_inbox_dir(tmp_path, {"inbox_dir": "  "})


# ---------- load_definition ----------

class TestLoadDefinition:
    def test_loads_explicit_definition(self, fake_home, tmp_path, monkeypatch):
        defn_path = tmp_path / "custom.json"
        defn_path.write_text('{"invoke": ["echo", "$MESSAGE"]}')

        import registry
        registry.save_registry({"X": {"root": str(tmp_path), "definition": str(defn_path)}})

        loaded = load_definition("X")
        assert loaded == {"invoke": ["echo", "$MESSAGE"]}

    def test_falls_back_to_default(self, fake_home):
        # Agent registered with NO definition field — load_definition falls
        # back to the bundled default.json.
        import registry
        registry.save_registry({"X": {"root": "/tmp"}})

        loaded = load_definition("X")
        assert "invoke" in loaded

    def test_missing_file_raises(self, fake_home):
        import registry
        registry.save_registry({"X": {"root": "/tmp", "definition": "/nonexistent.json"}})
        with pytest.raises(FileNotFoundError):
            load_definition("X")


# ---------- batch invoke ----------

class TestPauseSeconds:
    def test_zero_when_missing(self):
        from definitions import pause_seconds
        assert pause_seconds({"invoke": ["x"]}) == 0.0

    def test_returns_positive_float(self):
        from definitions import pause_seconds
        assert pause_seconds({"invoke": ["x"], "pause": 3}) == 3.0
        assert pause_seconds({"invoke": ["x"], "pause": "2.5"}) == 2.5

    def test_zero_or_negative_or_garbage_disables(self):
        from definitions import pause_seconds
        assert pause_seconds({"invoke": ["x"], "pause": 0}) == 0.0
        assert pause_seconds({"invoke": ["x"], "pause": -1}) == 0.0
        assert pause_seconds({"invoke": ["x"], "pause": "soon"}) == 0.0


class TestMaxWakeSeconds:
    def test_none_when_missing(self):
        from definitions import max_wake_seconds
        assert max_wake_seconds({"invoke": ["x"]}) is None

    def test_returns_positive_float(self):
        from definitions import max_wake_seconds
        assert max_wake_seconds({"invoke": ["x"], "max_wake_seconds": 600}) == 600.0
        assert max_wake_seconds({"invoke": ["x"], "max_wake_seconds": "90"}) == 90.0

    def test_zero_or_negative_or_garbage_disables(self):
        from definitions import max_wake_seconds
        assert max_wake_seconds({"invoke": ["x"], "max_wake_seconds": 0}) is None
        assert max_wake_seconds({"invoke": ["x"], "max_wake_seconds": -1}) is None
        assert max_wake_seconds({"invoke": ["x"], "max_wake_seconds": "soon"}) is None


class TestBatchInvoke:
    def test_has_batch_invoke_false_when_missing(self):
        from definitions import has_batch_invoke
        assert has_batch_invoke({"invoke": ["x"]}) is False

    def test_has_batch_invoke_true_when_set(self):
        from definitions import has_batch_invoke
        defn = {"invoke": ["x"], "batch": {"invoke": ["y"]}}
        assert has_batch_invoke(defn) is True

    def test_batch_limit_defaults_to_five(self):
        from definitions import batch_limit
        assert batch_limit({"invoke": ["x"]}) == 5
        assert batch_limit({"invoke": ["x"], "batch": {"invoke": ["y"]}}) == 5

    def test_batch_limit_respects_custom_value(self):
        from definitions import batch_limit
        defn = {"invoke": ["x"], "batch": {"invoke": ["y"], "limit": 3}}
        assert batch_limit(defn) == 3

    def test_batch_limit_tolerates_bad_input(self):
        from definitions import batch_limit
        defn = {"invoke": ["x"], "batch": {"invoke": ["y"], "limit": "nope"}}
        assert batch_limit(defn) == 5

    def test_build_batch_command_appends_one_composed_prompt(self):
        from definitions import BatchEntry, build_batch_command
        entries = [
            BatchEntry({"from": "A", "date": "2026-04-28T14:30:00Z", "content": "hi"}, "a.json"),
            BatchEntry({"from": "B", "date": "2026-04-28T14:29:00Z", "content": "yo"}, "b.json"),
        ]
        defn = {"invoke": ["x"], "batch": {"invoke": ["agent", "--batch", "$RECIPIENT"]}}
        argv = build_batch_command(defn, "neil", entries)
        assert argv[:3] == ["agent", "--batch", "neil"]
        assert len(argv) == 4
        prompt = argv[3]
        assert "receiving messages as 'neil'" in prompt
        assert "A sent" in prompt and "hi" in prompt
        assert "B sent" in prompt and "yo" in prompt

    def test_build_batch_command_empty_entries_still_has_header(self):
        from definitions import build_batch_command
        defn = {"invoke": ["x"], "batch": {"invoke": [
            "echo", "S=$SENDER", "M=$MESSAGE", "T=$TIMESTAMP", "A=$AGE",
        ]}}
        argv = build_batch_command(defn, "neil", [])
        assert argv[:-1] == ["echo", "S=", "M=", "T=", "A="]
        assert "receiving messages as 'neil'" in argv[-1]

    def test_build_batch_command_placeholder_for_unreadable_entry(self):
        from definitions import BatchEntry, build_batch_command
        entries = [
            BatchEntry({"from": "A", "date": "2026-04-28T14:30:00Z", "content": "hi"}, "a.json"),
            BatchEntry(None, "corrupt.json", "Expecting value: line 1 column 1 (char 0)"),
        ]
        defn = {"invoke": ["x"], "batch": {"invoke": ["agent"]}}
        prompt = build_batch_command(defn, "neil", entries)[-1]
        assert "A sent" in prompt and "hi" in prompt
        assert "unreadable message file corrupt.json" in prompt
        assert "Expecting value" in prompt

    def test_format_batch_message_and_placeholder(self):
        from definitions import format_batch_message, format_batch_placeholder
        block = format_batch_message({"from": "A", "date": "2026-04-28T14:30:00Z", "content": "hi"})
        assert block.startswith("----\n")
        assert "A sent" in block and "hi" in block

        placeholder = format_batch_placeholder("bad.json", "boom")
        assert placeholder == "---- [unreadable message file bad.json: boom]"


# ---------- build_idle_command + idle_timeout_seconds ----------

class TestBuildIdleCommand:
    def test_returns_none_when_no_idle(self):
        from definitions import build_idle_command
        assert build_idle_command({"invoke": ["x"]}, "neil") is None

    def test_returns_none_when_idle_invoke_missing(self):
        from definitions import build_idle_command
        assert build_idle_command({"invoke": ["x"], "idle": {"timeout": 60}}, "neil") is None

    def test_expands_recipient_to_agent_name(self):
        from definitions import build_idle_command
        defn = {"invoke": ["x"], "idle": {"timeout": 60, "invoke": ["claude", "$RECIPIENT idle"]}}
        assert build_idle_command(defn, "neil") == ["claude", "neil idle"]

    def test_message_fields_are_empty(self):
        # Idle has no incoming message — sender/message/timestamp/age all blank.
        from definitions import build_idle_command
        defn = {"invoke": ["x"], "idle": {"timeout": 60, "invoke": [
            "echo", "S=$SENDER", "M=$MESSAGE", "T=$TIMESTAMP", "A=$AGE",
        ]}}
        assert build_idle_command(defn, "neil") == [
            "echo", "S=", "M=", "T=", "A=",
        ]

    def test_a8s_dir_substitution_works(self):
        from core import SCRIPT_DIR
        from definitions import build_idle_command
        defn = {"invoke": ["x"], "idle": {"timeout": 60, "invoke": ["$A8S_DIR/check"]}}
        assert build_idle_command(defn, "neil") == [f"{SCRIPT_DIR}/check"]


class TestIdleTimeoutSeconds:
    def test_none_when_no_idle(self):
        from definitions import idle_timeout_seconds
        assert idle_timeout_seconds({"invoke": ["x"]}) is None

    def test_returns_float_when_set(self):
        from definitions import idle_timeout_seconds
        assert idle_timeout_seconds({"idle": {"timeout": 60}}) == 60.0

    def test_string_numeric_parses(self):
        from definitions import idle_timeout_seconds
        assert idle_timeout_seconds({"idle": {"timeout": "120"}}) == 120.0

    def test_zero_or_negative_returns_none(self):
        # Treat 0 and negative as "disabled" — caller skips if None.
        from definitions import idle_timeout_seconds
        assert idle_timeout_seconds({"idle": {"timeout": 0}}) is None
        assert idle_timeout_seconds({"idle": {"timeout": -10}}) is None

    def test_garbage_returns_none(self):
        from definitions import idle_timeout_seconds
        assert idle_timeout_seconds({"idle": {"timeout": "soon"}}) is None
        assert idle_timeout_seconds({"idle": {"timeout": None}}) is None
