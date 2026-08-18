"""Tests for definitions.py — single-verb argv interpolation, age formatting,
and auto-discovery."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
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
    definition_env,
    harness_is_resolvable,
    harness_program,
    default_definition_path,
    load_definition,
    resolve_definition_arg,
    resolve_files_dir,
    resolve_inbox_dir,
    resolve_outbox_dir,
    validate_var_name,
    wake_env,
    wake_shell,
    wrap_wake_argv,
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

    def test_attachment_unavailable_line(self, files_root):
        msg = {
            "content": "see attached",
            "id": "01JTESTATTACH000000000000",
            "files": [{
                "filename": "report.pdf",
                "error": "ATTACHMENT_UNAVAILABLE",
                "detail": "could not download; contact an administrator",
            }],
        }
        body = _message_body(msg, files_root)
        assert "ATTACHMENT UNAVAILABLE: report.pdf" in body
        assert "contact an administrator" in body
        assert "ATTACHED FILE:" not in body


# ---------- build_command + _expand_argv ----------

class TestBuildCommand:
    @pytest.fixture
    def files_root(self, tmp_path):
        """The node's already-resolved attachment root. `build_command` takes
        it rather than deriving it: `files_dir` interpolates per-node vars, and
        the registry is the one place that resolves a mailbox path."""
        root = tmp_path / "agent" / ".files"
        root.mkdir(parents=True)
        return root

    def test_substitutes_sender_recipient_message(self, files_root):
        defn = {"invoke": ["claude", "--continue", "-p", "$SENDER tells $RECIPIENT: $MESSAGE"]}
        msg = {"from": "GERRY", "to": "CLAUDE", "content": "fix this"}
        argv = build_command(defn, msg, files_root)
        assert argv == ["claude", "--continue", "-p", "GERRY tells CLAUDE: fix this"]

    def test_alias_routed_keeps_alias_in_recipient(self, files_root):
        # Strict opacity / mailing-list semantics: when the sender wrote
        # `to: devs`, the recipient's $RECIPIENT resolves to "devs".
        defn = {"invoke": ["claude", "-p", "$SENDER tells $RECIPIENT: $MESSAGE"]}
        msg = {"from": "GERRY", "to": "devs", "content": "standup"}
        argv = build_command(defn, msg, files_root)
        assert argv == ["claude", "-p", "GERRY tells devs: standup"]

    def test_namespace_routed_keeps_full_address_in_recipient(self, files_root):
        # Issue #148: routing preserves the colon address in `to`, so the
        # bound node's $RECIPIENT carries it verbatim and the node can
        # self-route internally.
        defn = {"invoke": ["claude", "-p", "$SENDER tells $RECIPIENT: $MESSAGE"]}
        msg = {"from": "GERRY", "to": "acme:ops:phil", "content": "ping"}
        argv = build_command(defn, msg, files_root)
        assert argv == ["claude", "-p", "GERRY tells acme:ops:phil: ping"]

    def test_missing_invoke_raises(self, files_root):
        with pytest.raises(ValueError, match="invoke"):
            build_command({}, {"from": "G", "to": "C"}, files_root)

    def test_a8s_dir_substitution(self, files_root):
        from core import SCRIPT_DIR
        defn = {"invoke": ["$A8S_DIR/dummy-cli", "$MESSAGE"]}
        msg = {"from": "A", "to": "B", "content": "hi"}
        argv = build_command(defn, msg, files_root)
        assert argv == [f"{SCRIPT_DIR}/dummy-cli", "hi"]

    def test_definition_path_substitution(self, files_root):
        defn = {"invoke": ["r4t", "--definition", "$DEFINITION_PATH", "-p", "$MESSAGE"]}
        msg = {"from": "A", "to": "B", "content": "hi"}
        argv = build_command(defn, msg, files_root, "/path/to/defn.json")
        assert argv == ["r4t", "--definition", "/path/to/defn.json", "-p", "hi"]

    def test_definition_path_defaults_empty(self, files_root):
        defn = {"invoke": ["r4t", "$DEFINITION_PATH", "$MESSAGE"]}
        argv = build_command(defn, {"from": "A", "to": "B", "content": "hi"}, files_root)
        assert argv == ["r4t", "", "hi"]

    def test_meta_expands_to_compact_json(self, files_root):
        # #167: protocol metadata between nodes. a8s carries the object and
        # hands it over verbatim — the vocabulary is the nodes' business.
        defn = {"invoke": ["r4t", "--meta", "$META", "-p", "$MESSAGE"]}
        msg = {"from": "A", "to": "B", "content": "hi", "meta": {"class": "auto"}}
        argv = build_command(defn, msg, files_root)
        assert argv == ["r4t", "--meta", '{"class":"auto"}', "-p", "hi"]

    def test_meta_absent_expands_empty(self, files_root):
        defn = {"invoke": ["r4t", "--meta", "$META"]}
        argv = build_command(defn, {"from": "A", "to": "B", "content": "hi"}, files_root)
        assert argv == ["r4t", "--meta", ""]

    def test_meta_non_object_expands_empty(self, files_root):
        # A remote cluster wrote the envelope; a scalar `meta` is that
        # boundary's problem, not a crash in the wake.
        defn = {"invoke": ["r4t", "--meta", "$META"]}
        msg = {"from": "A", "to": "B", "content": "hi", "meta": "auto"}
        argv = build_command(defn, msg, files_root)
        assert argv == ["r4t", "--meta", ""]

    def test_meta_value_is_not_reinterpolated(self, files_root):
        defn = {"invoke": ["r4t", "--meta", "$META"]}
        msg = {"from": "A", "to": "B", "content": "hi", "meta": {"note": "$SENDER \\ x"}}
        argv = build_command(defn, msg, files_root)
        assert argv[2] == '{"note":"$SENDER \\\\ x"}'

    def test_bundled_r4t_node_receives_the_class_on_its_argv(self, files_root):
        # The seam itself: the shipped r4t definition forwards `$META`, so a
        # peer cluster's class reaches `r4t dispatch` without a8s reading it.
        defn = json.loads(default_definition_path("r4t").read_text(encoding="utf-8"))
        msg = {
            "from": "beta", "to": "acme", "content": "roster sync",
            "meta": {"class": "auto"},
        }
        argv = build_command(defn, msg, files_root)
        assert argv[argv.index("--meta") + 1] == '{"class":"auto"}'

    def test_does_not_mutate_original_argv(self, files_root):
        defn = {"invoke": ["claude", "-p", "$MESSAGE"]}
        original = list(defn["invoke"])
        build_command(defn, {"from": "A", "to": "B", "content": "hello"}, files_root)
        assert defn["invoke"] == original

    def test_message_body_includes_files(self, files_root):
        defn = {"invoke": ["x", "$MESSAGE"]}
        msg_id = "01JTESTATTACH000000000000"
        msg = {
            "from": "GERRY",
            "to": "CLAUDE",
            "content": "review",
            "id": msg_id,
            "files": [{"filename": "x"}],
        }
        argv = build_command(defn, msg, files_root)
        path = (files_root / msg_id / "x").resolve()
        assert argv == ["x", f"review\n\nATTACHED FILE: {path}"]

    def test_files_dir_in_the_definition_does_not_override_the_given_root(self, tmp_path):
        # `files_dir` is resolved once, by the registry, and handed here as a
        # path. A definition that still names one must not be re-read: doing so
        # would skip the per-node interpolation the caller already applied.
        given = tmp_path / "resolved"
        msg_id = "01JTESTATTACH000000000000"
        defn = {"invoke": ["x", "$MESSAGE"], "files_dir": str(tmp_path / "ignored")}
        msg = {
            "from": "GERRY",
            "to": "CLAUDE",
            "content": "review",
            "id": msg_id,
            "files": [{"filename": "x"}],
        }
        argv = build_command(defn, msg, given)
        path = (given / msg_id / "x").resolve()
        assert argv == ["x", f"review\n\nATTACHED FILE: {path}"]

    def test_timestamp_substitution_from_msg_date(self, files_root):
        defn = {"invoke": ["x", "[$TIMESTAMP] $SENDER: $MESSAGE"]}
        msg = {
            "from": "GERRY",
            "to": "CLAUDE",
            "date": "2026-04-28T14:30:00.000000Z",
            "content": "hi",
        }
        argv = build_command(defn, msg, files_root)
        assert argv == ["x", "[2026-04-28T14:30:00.000000Z] GERRY: hi"]

    def test_age_substitution_relative_to_now(self, files_root, monkeypatch):
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
        argv = build_command(defn, msg, files_root)
        assert argv == ["x", "(5 minutes ago) hi"]

    def test_missing_date_yields_empty_age_and_timestamp(self, files_root):
        defn = {"invoke": ["x", "TS:$TIMESTAMP", "AGE:$AGE", "$MESSAGE"]}
        msg = {"from": "G", "to": "C", "content": "hi"}
        argv = build_command(defn, msg, files_root)
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


class TestNowPlaceholder:
    """`$NOW` is the wake's own local reading. `$TIMESTAMP` stays the stored
    UTC — definitions pick it deliberately because it is machine-readable and
    stable, and rewriting it would rewrite every definition's meaning."""

    @pytest.fixture
    def zone(self, monkeypatch):
        import time as _time

        def use(name: str) -> None:
            monkeypatch.setenv("TZ", name)
            _time.tzset()

        yield use
        monkeypatch.undo()
        _time.tzset()

    def test_now_expands_to_local_time_with_its_zone(self, zone):
        zone("America/Los_Angeles")
        (got,) = _expand_argv(["$NOW"], "A", "B", "hi")
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} P[DS]T", got)

    def test_now_follows_the_machines_zone(self, zone):
        zone("Asia/Kolkata")
        (got,) = _expand_argv(["$NOW"], "A", "B", "hi")
        assert got.endswith(" IST")

    def test_timestamp_stays_the_stored_utc(self, zone):
        zone("America/Los_Angeles")
        assert _expand_argv(
            ["$TIMESTAMP"], "A", "B", "hi", "2026-06-18T14:00:00Z"
        ) == ["2026-06-18T14:00:00Z"]

    def test_now_is_reserved_from_a8s_vars(self):
        with pytest.raises(ValueError, match="reserved"):
            validate_var_name("now")

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


# ---------- path-field interpolation ----------

FIELD_RESOLVERS = [
    ("outbox_dir", resolve_outbox_dir),
    ("inbox_dir", resolve_inbox_dir),
    ("files_dir", resolve_files_dir),
]


class TestPathFieldInterpolation:
    """Per-node vars reach the three mailbox path fields, so two nodes rooted
    at one repo can share a definition and still own separate mailboxes."""

    @pytest.fixture
    def root(self, tmp_path):
        r = tmp_path / "repo"
        r.mkdir()
        return r

    @pytest.mark.parametrize("field,resolve", FIELD_RESOLVERS)
    def test_expands_node_var(self, root, field, resolve):
        got = resolve(root, {field: f".box-$SEAT"}, "codex-ares", {"SEAT": "a"})
        assert got == (root / ".box-a").resolve()

    @pytest.mark.parametrize("field,resolve", FIELD_RESOLVERS)
    def test_expands_dollar_node_with_no_vars(self, root, field, resolve):
        got = resolve(root, {field: ".box-$NODE"}, "codex-ares", {})
        assert got == (root / ".box-codex-ares").resolve()

    @pytest.mark.parametrize("field,resolve", FIELD_RESOLVERS)
    def test_absent_field_keeps_the_default(self, root, field, resolve):
        default = {"outbox_dir": ".outbox", "inbox_dir": ".inbox", "files_dir": ".files"}
        assert resolve(root, {}, "codex-ares", {"SEAT": "a"}) == (
            root / default[field]
        ).resolve()

    @pytest.mark.parametrize("field,resolve", FIELD_RESOLVERS)
    def test_unset_var_raises_and_names_it(self, root, field, resolve):
        with pytest.raises(UndefinedVarsError) as e:
            resolve(root, {field: ".box-$SEAT"}, "codex-ares", {})
        assert "$SEAT" in str(e.value)

    def test_node_builtin_is_not_shadowed_by_a_stored_var(self, root):
        # `$NODE` is the one value guaranteed distinct between two
        # registrations. A var that claims the name cannot take it over.
        got = resolve_outbox_dir(
            root, {"outbox_dir": ".outbox-$NODE"}, "codex-ares", {"NODE": "impostor"}
        )
        assert got == (root / ".outbox-codex-ares").resolve()

    def test_no_partial_expansion(self, root):
        # A path that half-resolves is a plausible directory that is silently
        # the wrong one, and mail routed there is lost with no error anywhere.
        with pytest.raises(UndefinedVarsError) as e:
            resolve_outbox_dir(root, {"outbox_dir": ".out-$A-$B"}, "n", {"A": "x"})
        assert "$B" in str(e.value)
        assert not (root / ".out-x-").exists()

    @pytest.mark.parametrize(
        "builtin", ["SENDER", "RECIPIENT", "MESSAGE", "TIMESTAMP", "AGE", "META"]
    )
    def test_per_message_builtins_are_refused(self, root, builtin):
        # A mailbox path is per-node and resolved long before any message
        # exists. These names mean nothing here.
        with pytest.raises(UndefinedVarsError):
            resolve_outbox_dir(root, {"outbox_dir": f".outbox-${builtin}"}, "n", {})

    def test_expansion_to_empty_is_refused(self, root):
        with pytest.raises(ValueError, match="expanded to empty"):
            resolve_outbox_dir(root, {"outbox_dir": "$SEAT"}, "n", {"SEAT": " "})

    def test_absolute_expansion_still_wins_over_root(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        external = tmp_path / "mounts"
        got = resolve_outbox_dir(
            root, {"outbox_dir": f"{external}/$SEAT"}, "n", {"SEAT": "a"}
        )
        assert got == (external / "a").resolve()

    def test_node_is_reserved_from_a8s_vars(self):
        with pytest.raises(ValueError, match="reserved"):
            validate_var_name("node")


class TestPathFieldForAgent:
    """The `_for_agent` wrappers supply the node name and its registry vars."""

    def _register(self, tmp_path, name, spec, vars=None):
        import registry

        defn = tmp_path / f"{name}.json"
        defn.write_text(json.dumps({"invoke": ["x"], "outbox_dir": spec}))
        root = tmp_path / "repo"
        root.mkdir(exist_ok=True)
        entry = {"root": str(root), "definition": str(defn)}
        if vars:
            entry["vars"] = vars
        reg = registry.load_registry()
        reg[name] = entry
        registry.save_registry(reg)
        return root

    def test_node_name_reaches_the_field(self, fake_home, tmp_path):
        from definitions import resolve_outbox_dir_for_agent

        root = self._register(tmp_path, "codex-ares", ".outbox-$NODE")
        assert resolve_outbox_dir_for_agent("codex-ares", root) == (
            root / ".outbox-codex-ares"
        ).resolve()

    def test_registry_vars_reach_the_field(self, fake_home, tmp_path):
        from definitions import resolve_outbox_dir_for_agent

        root = self._register(tmp_path, "n", ".outbox-$SEAT", {"SEAT": "b"})
        assert resolve_outbox_dir_for_agent("n", root) == (root / ".outbox-b").resolve()

    def test_unset_var_propagates(self, fake_home, tmp_path):
        from definitions import resolve_outbox_dir_for_agent

        root = self._register(tmp_path, "n", ".outbox-$SEAT")
        with pytest.raises(UndefinedVarsError):
            resolve_outbox_dir_for_agent("n", root)


class TestVarsStayOutOfEnv:
    """The argv/env boundary is untouched: a var reaches argv and a path field,
    never the child's environment."""

    def test_definition_env_is_literal(self):
        assert definition_env({"env": {"SEAT": "$SEAT", "P": "$NODE"}}) == {
            "SEAT": "$SEAT",
            "P": "$NODE",
        }


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
    def test_default_three_when_batch_invoke_and_pause_absent(self):
        from definitions import pause_seconds
        assert pause_seconds({"invoke": ["x"], "batch": {"invoke": ["y"]}}) == 3.0

    def test_zero_when_no_batch_invoke(self):
        from definitions import pause_seconds
        assert pause_seconds({"invoke": ["x"]}) == 0.0

    def test_honors_explicit_zero(self):
        from definitions import pause_seconds
        assert pause_seconds({
            "invoke": ["x"], "pause": 0, "batch": {"invoke": ["y"]},
        }) == 0.0

    def test_honors_explicit_number(self):
        from definitions import pause_seconds
        assert pause_seconds({
            "invoke": ["x"], "pause": 1.5, "batch": {"invoke": ["y"]},
        }) == 1.5
        assert pause_seconds({"invoke": ["x"], "pause": "2.5"}) == 2.5
        assert pause_seconds({"invoke": ["x"], "pause": -1}) == 0.0

    def test_garbage_treated_as_absent(self):
        from definitions import pause_seconds
        assert pause_seconds({
            "invoke": ["x"], "pause": "soon", "batch": {"invoke": ["y"]},
        }) == 3.0
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


class TestBundledEngineNode:
    """`engine-claude.json` — the template every engine-backed bare node is
    copied from, so its three wake paths are asserted rather than assumed."""

    @pytest.fixture
    def agent_root(self, tmp_path):
        root = tmp_path / "agent"
        root.mkdir()
        return root

    def _definition(self):
        return json.loads(
            default_definition_path("engine-claude").read_text(encoding="utf-8")
        )

    def test_a_single_message_rides_the_prompt_positional(self, agent_root):
        argv = build_command(
            self._definition(),
            {"from": "neil", "to": "node1", "content": "check the deploy"},
            agent_root,
        )
        assert argv[2:6] == ["engine", "claude", "run", "--agent"]
        # #157's chapter-1 field test: a bare $MESSAGE gives the node no way
        # to know who to answer, so the prompt states the sender too.
        assert argv[-1].endswith("check the deploy")
        # `[$NOW]` leads: a model handed only relative age generalizes a zone
        # it does not live in, and every *tomorrow* it writes lands a day off.
        assert re.fullmatch(
            r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2} \S+\] neil tells node1 \(\): "
            r"check the deploy",
            argv[-1],
        )

    def test_a_batch_of_n_becomes_one_invocation(self):
        # #159's third bullet: three messages must not cost three cold context
        # loads. a8s composes the N envelopes into one prompt and `engine run`
        # takes it as its PROMPT positional — no dispatcher, no new entry
        # point, one turn.
        from definitions import BatchEntry, batch_format, build_batch_command, pause_seconds

        defn = self._definition()
        entries = [
            BatchEntry({"from": "A", "date": "2026-04-28T14:30:00Z", "content": "first"}, "a.json"),
            BatchEntry({"from": "B", "date": "2026-04-28T14:31:00Z", "content": "second"}, "b.json"),
        ]
        argv = build_batch_command(defn, "node1", entries, "/defs/engine-claude.json")
        assert argv[2:] == ["engine", "claude", "run", "--agent", "node1", argv[-1]]
        prompt = argv[-1]
        assert prompt.index("first") < prompt.index("second")  # arrival order
        # The prose form, not `envelopes`: a bare node has no queue to ingest
        # a JSON array into.
        assert batch_format(defn) == "prompt"
        assert pause_seconds(defn) == 3.0  # declaring batch debounces by default

    def test_the_idle_wake_carries_the_latch_flag_and_no_prompt(self):
        from definitions import build_idle_command, idle_timeout_seconds

        defn = self._definition()
        argv = build_idle_command(defn, "node1", "/defs/engine-claude.json")
        assert argv[-3:] == ["--idle", "--agent", "node1"]
        assert idle_timeout_seconds(defn) > 0
        # No message and no prompt: `--idle` picks r4t's own consolidation
        # text, and the latch means only the first quiet tick spends a turn.
        assert "$MESSAGE" not in argv and "" not in argv[2:]


# Mirrors RUN_ENGINES in apps/r4t/engines/run.py. a8s tests do not import r4t
# (its ulid module shadows a8s's own — see apps/r4t/tests/run's separate
# invocation), so the source of truth is duplicated here rather than imported.
RUN_ENGINE_IDS = (
    "claude", "codex", "agy", "copilot", "cursor", "opencode",
    "ollama-claude", "ollama-codex", "ollama-opencode",
)
OLLAMA_ENGINE_IDS = tuple(e for e in RUN_ENGINE_IDS if e.startswith("ollama-"))

# What `--permissions bypass` changes in the engine's OWN argv, per engine:
# (tokens the base loses, tokens bypass adds). Empty on both sides means the
# composed argv is identical to the base's, because the base preset already
# carries that engine's strongest mode — the state each -unrestricted
# description states in words. Mirrors apps/r4t/rig.py's PERMISSION_TRANSLATION
# for the same reason RUN_ENGINE_IDS mirrors RUN_ENGINES: a8s tests cannot
# import r4t in-process.
BYPASS_ARGV_DELTA = {
    "claude": (("dontAsk",), ("bypassPermissions",)),
    "ollama-claude": (("dontAsk",), ("bypassPermissions",)),
    "codex": (
        ("--sandbox", "workspace-write"),
        ("--dangerously-bypass-approvals-and-sandbox",),
    ),
    "ollama-codex": (
        ("--sandbox", "workspace-write"),
        ("--dangerously-bypass-approvals-and-sandbox",),
    ),
    "copilot": (("--allow-all-tools",), ("--allow-all",)),
    "agy": ((), ()),
    "cursor": ((), ()),
    "opencode": ((), ()),
    "ollama-opencode": ((), ()),
}

_REPO_ROOT = Path(__file__).resolve().parents[3]
_R4T_DIR = _REPO_ROOT / "apps" / "r4t"

_PERMISSION_PROBE = """
import json, sys
import rig
out = {}
for engine in json.loads(sys.argv[1]):
    try:
        base = rig.build_preset_invoke(engine, model="MODEL")
    except rig.RigError:
        base = rig.build_preset_invoke(engine)
    bypass, _ = rig.apply_permissions(base, engine, "bypass")
    out[engine] = [base, bypass]
print(json.dumps(out))
"""


@pytest.fixture(scope="session")
def engine_argv_pairs():
    """`{engine: [base argv, bypass argv]}` composed by r4t's own permission
    table, in a subprocess: r4t's modules shadow a8s's inside this process."""
    proc = subprocess.run(
        [sys.executable, "-c", _PERMISSION_PROBE, json.dumps(list(RUN_ENGINE_IDS))],
        cwd=_R4T_DIR,
        env={**os.environ, "PYTHONPATH": f"{_R4T_DIR}{os.pathsep}{_REPO_ROOT}"},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


class TestBundledEngineDefinitions:
    """Every RUN_ENGINES id ships its own `engine-<id>.json`, usable as-is —
    the owner ruling behind #157's follow-up: a built-in living in a hidden
    directory must work with `a8s add name ./dir engine-<id>` unedited, not
    tell the user to copy-and-edit claude's."""

    @pytest.fixture
    def agent_root(self, tmp_path):
        root = tmp_path / "agent"
        root.mkdir()
        return root

    def _definition(self, engine_id):
        return json.loads(
            default_definition_path(f"engine-{engine_id}").read_text(encoding="utf-8")
        )

    def _unrestricted(self, engine_id):
        return json.loads(
            default_definition_path(f"engine-{engine_id}-unrestricted").read_text(
                encoding="utf-8"
            )
        )

    @pytest.mark.parametrize("engine_id", RUN_ENGINE_IDS)
    def test_bundled_definition_exists(self, engine_id):
        assert default_definition_path(f"engine-{engine_id}").is_file()

    @pytest.mark.parametrize("engine_id", RUN_ENGINE_IDS)
    def test_definition_parses_through_the_loader(self, engine_id, agent_root):
        defn = self._definition(engine_id)
        # ollama-* engines have no default model: the a8s var must be
        # supplied, same as a real `a8s add ... --model=...` node.
        node_vars = {"MODEL": "qwen3.6"} if engine_id in OLLAMA_ENGINE_IDS else None

        # A single-message wake — proves `invoke` interpolates cleanly.
        argv = build_command(
            defn, {"from": "neil", "to": "node1", "content": "hi"}, agent_root,
            vars=node_vars,
        )
        assert argv[2:5] == ["engine", engine_id, "run"]

        # A batch wake — proves `batch.invoke` interpolates with no message.
        from definitions import BatchEntry, build_batch_command, build_idle_command

        entries = [
            BatchEntry(
                {"from": "A", "date": "2026-04-28T14:30:00Z", "content": "hi"}, "a.json"
            ),
        ]
        batch_argv = build_batch_command(defn, "node1", entries, vars=node_vars)
        assert batch_argv[2:5] == ["engine", engine_id, "run"]

        # An idle wake — proves `idle.invoke` interpolates with no message.
        idle_argv = build_idle_command(defn, "node1", vars=node_vars)
        assert idle_argv[2:5] == ["engine", engine_id, "run"]

    @pytest.mark.parametrize("engine_id", RUN_ENGINE_IDS)
    def test_single_invoke_ends_with_the_sender_carrying_prompt(self, engine_id):
        # #157's chapter-1 field test: a bare `$MESSAGE` gives the node no
        # way to know who to answer. Every bundled definition's single-wake
        # prompt states the sender, same shape as codex.json / cursor.json.
        defn = self._definition(engine_id)
        assert defn["invoke"][-1] == "[$NOW] $SENDER tells $RECIPIENT ($AGE): $MESSAGE"

    @pytest.mark.parametrize("engine_id", RUN_ENGINE_IDS)
    def test_batch_and_idle_invokes_carry_no_prompt(self, engine_id):
        defn = self._definition(engine_id)
        for block in (defn["batch"]["invoke"], defn["idle"]["invoke"]):
            assert "$MESSAGE" not in block
            assert not any("$SENDER" in a for a in block)

    @pytest.mark.parametrize("engine_id", OLLAMA_ENGINE_IDS)
    def test_ollama_engines_carry_model_on_every_wake(self, engine_id):
        defn = self._definition(engine_id)
        for block in (defn["invoke"], defn["batch"]["invoke"], defn["idle"]["invoke"]):
            assert "--model" in block
            assert "$MODEL" in block

    @pytest.mark.parametrize("engine_id", RUN_ENGINE_IDS)
    def test_unrestricted_variant_lifts_permissions_on_every_wake(
        self, engine_id, agent_root
    ):
        # `a8s add amos ~/agents/amos engine-cursor-unrestricted`: the same
        # node as engine-<id>, invoked at --permissions bypass. The stance
        # belongs on the invoke line, visible in the definition the operator
        # chose by name — never a default the base variant grows.
        from definitions import BatchEntry, build_batch_command, build_idle_command

        defn = self._unrestricted(engine_id)
        node_vars = {"MODEL": "qwen3.6"} if engine_id in OLLAMA_ENGINE_IDS else None
        entries = [
            BatchEntry(
                {"from": "A", "date": "2026-04-28T14:30:00Z", "content": "hi"}, "a.json"
            ),
        ]
        composed = (
            build_command(
                defn, {"from": "neil", "to": "node1", "content": "hi"}, agent_root,
                vars=node_vars,
            ),
            build_batch_command(defn, "node1", entries, vars=node_vars),
            build_idle_command(defn, "node1", vars=node_vars),
        )
        for argv in composed:
            assert argv[2:5] == ["engine", engine_id, "run"]
            assert argv[argv.index("--permissions") + 1] == "bypass"
        assert defn["invoke"][-1] == "[$NOW] $SENDER tells $RECIPIENT ($AGE): $MESSAGE"

    @pytest.mark.parametrize("engine_id", RUN_ENGINE_IDS)
    def test_unrestricted_description_states_the_cost(self, engine_id):
        # The trigger, not just the flag: an unrestricted node runs on mail
        # from anyone who can reach its inbox.
        description = self._unrestricted(engine_id)["description"]
        assert "untrusted inbound mail" in description
        assert "its own machine and its own account" in description

    @pytest.mark.parametrize("engine_id", OLLAMA_ENGINE_IDS)
    def test_unrestricted_ollama_descriptions_keep_the_model_prerequisite(
        self, engine_id
    ):
        # The variant interpolates $MODEL too, so dropping the prerequisite
        # leaves an UndefinedVarsError with nothing pointing at the fix.
        assert "MODEL" in self._unrestricted(engine_id)["description"]

    @pytest.mark.parametrize("engine_id", RUN_ENGINE_IDS)
    def test_base_definition_carries_no_permission_stance(self, engine_id):
        # The other half of the -unrestricted promise: the stance is something
        # the operator chooses by name, so the base variants never grow it.
        defn = self._definition(engine_id)
        for block in (defn["invoke"], defn["batch"]["invoke"], defn["idle"]["invoke"]):
            assert "--permissions" not in block

    @pytest.mark.parametrize("engine_id", RUN_ENGINE_IDS)
    def test_bypass_changes_the_engine_argv_exactly_as_described(
        self, engine_id, engine_argv_pairs
    ):
        # `--permissions bypass` reaches the engine CLI as different flags per
        # engine, and for four of the nine as no flags at all. Each variant's
        # description states its own case, so pin both: a real-flag engine
        # against its documented token swap, a no-op engine against argv
        # identity — which turns a future PermissionRule change into a failure
        # here rather than a description that has quietly stopped being true.
        base, bypass = engine_argv_pairs[engine_id]
        removed, added = BYPASS_ARGV_DELTA[engine_id]
        if not removed and not added:
            assert bypass == base
        else:
            assert Counter(base) - Counter(bypass) == Counter(removed)
            assert Counter(bypass) - Counter(base) == Counter(added)

    @pytest.mark.parametrize("engine_id", RUN_ENGINE_IDS)
    def test_description_names_no_copy_this_file_instruction(self, engine_id):
        # The owner ruling this test enforces: templates must ALSO be usable
        # as-is, so a built-in's own description must not tell the reader to
        # copy it before it will work.
        description = self._definition(engine_id)["description"].lower()
        assert "copy this file" not in description
        assert "copy the file" not in description


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

    def test_batch_prompt_opens_with_the_local_time(self, monkeypatch):
        """The real fix for the UTC hallucination is the text the model reads.
        The batch prompt is composed by a8s itself, so it says it outright."""
        import time as _time
        from definitions import build_batch_prompt

        monkeypatch.setenv("TZ", "Asia/Kolkata")
        _time.tzset()
        try:
            first = build_batch_prompt("neil", []).splitlines()[0]
        finally:
            monkeypatch.undo()
            _time.tzset()
        assert re.fullmatch(
            r"Local time is \d{4}-\d{2}-\d{2} \d{2}:\d{2} IST\. Every date and "
            r"time you read or write is this zone unless it carries an "
            r"explicit offset\.",
            first,
        )

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

    def test_batch_format_defaults_to_prompt(self):
        from definitions import batch_format
        assert batch_format({"invoke": ["x"]}) == "prompt"
        assert batch_format({"invoke": ["x"], "batch": {"invoke": ["y"]}}) == "prompt"
        assert batch_format({
            "invoke": ["x"],
            "batch": {"invoke": ["y"], "format": "prompt"},
        }) == "prompt"

    def test_batch_format_unknown_falls_back_to_prompt(self):
        from definitions import batch_format
        for garbage in ("json", "ENVELOPE", "  ", 12, None, True, []):
            defn = {"invoke": ["x"], "batch": {"invoke": ["y"], "format": garbage}}
            assert batch_format(defn) == "prompt"

    def test_batch_format_envelopes_case_insensitive(self):
        from definitions import batch_format
        for word in ("envelopes", "Envelopes", "ENVELOPES", " envelopes "):
            defn = {"invoke": ["x"], "batch": {"invoke": ["y"], "format": word}}
            assert batch_format(defn) == "envelopes"

    def test_prompt_path_byte_identical_with_explicit_format(self):
        from definitions import BatchEntry, build_batch_command
        entries = [
            BatchEntry(
                {"from": "A", "date": "2026-04-28T14:30:00Z", "content": "hi",
                 "to": "neil", "meta": {"class": "auto"}},
                "a.json",
            ),
        ]
        bare = {"invoke": ["x"], "batch": {"invoke": ["agent", "--batch"]}}
        explicit = {
            "invoke": ["x"],
            "batch": {"invoke": ["agent", "--batch"], "format": "prompt"},
        }
        assert build_batch_command(bare, "neil", entries) == (
            build_batch_command(explicit, "neil", entries)
        )

    def test_envelopes_format_appends_parseable_json(self):
        from definitions import BatchEntry, build_batch_command
        import json
        entries = [
            BatchEntry(
                {
                    "from": "alice",
                    "to": "acme:phil",
                    "date": "2026-04-28T14:30:00Z",
                    "content": "do the thing",
                    "meta": {"class": "auto", "extra": 1},
                },
                "a.json",
            ),
            BatchEntry(
                {
                    "from": "bob",
                    "to": "acme",
                    "date": "2026-04-28T14:29:00Z",
                    "content": "also this",
                    "meta": {},
                },
                "b.json",
            ),
        ]
        defn = {
            "invoke": ["x"],
            "batch": {
                "invoke": ["r4t", "dispatch", "--batch"],
                "format": "envelopes",
            },
        }
        argv = build_batch_command(defn, "acme", entries)
        assert argv[:3] == ["r4t", "dispatch", "--batch"]
        assert len(argv) == 4
        payload = json.loads(argv[-1])
        assert isinstance(payload, list) and len(payload) == 2
        assert payload[0]["from"] == "alice"
        assert payload[0]["to"] == "acme:phil"
        assert payload[0]["content"] == "do the thing"
        assert payload[0]["meta"] == {"class": "auto", "extra": 1}
        assert payload[1]["from"] == "bob"
        assert payload[1]["to"] == "acme"
        assert payload[1]["content"] == "also this"

    def test_envelopes_unreadable_appears_as_marker(self):
        from definitions import BatchEntry, build_batch_command
        import json
        entries = [
            BatchEntry(
                {"from": "A", "to": "acme", "content": "hi"}, "a.json",
            ),
            BatchEntry(None, "corrupt.json", "Expecting value: line 1"),
        ]
        defn = {
            "invoke": ["x"],
            "batch": {"invoke": ["agent"], "format": "envelopes"},
        }
        payload = json.loads(build_batch_command(defn, "acme", entries)[-1])
        assert payload[0]["from"] == "A"
        assert payload[1] == {
            "_unreadable": "corrupt.json",
            "error": "Expecting value: line 1",
        }
        assert len(payload) == 2


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


class TestHarnessProgram:
    """What a node actually needs installed (#121).

    The spawn guard only ever saw `argv[0]`, so a definition that wraps its
    harness — and the real ones do, for locking and timeouts — resolved
    `argv[0]` fine and failed inside the wrapper. The operator got
    `flock: failed to execute claude: No such file or directory`, which names
    the wrong program and says nothing about PATH.
    """

    @pytest.mark.parametrize("argv,expected", [
        (["claude", "-p", "$MESSAGE"], "claude"),
        (["flock", "/tmp/a8s.lock", "claude", "-p"], "claude"),
        (["flock", "-w", "5", "/tmp/a8s.lock", "claude"], "claude"),
        (["timeout", "240", "agy", "--print"], "agy"),
        (["timeout", "-k", "5", "30", "claude"], "claude"),
        (["nice", "-n", "10", "timeout", "60", "cursor-agent"], "cursor-agent"),
        (["env", "FOO=bar", "codex", "exec"], "codex"),
        (["env", "-u", "FOO", "BAR=1", "gemini"], "gemini"),
        (["nohup", "flock", "/tmp/l", "timeout", "30", "opencode", "run"], "opencode"),
        (["/usr/local/bin/h4l", "dispatch"], "/usr/local/bin/h4l"),
    ])
    def test_it_looks_through_the_wrapper(self, argv, expected):
        assert harness_program(argv) == expected

    @pytest.mark.parametrize("argv", [
        ["sh", "-c", "claude -p x"],
        ["bash", "-lc", "claude"],
        ["/bin/sh", "-c", "anything"],
    ])
    def test_a_shell_string_is_declined_not_guessed(self, argv):
        # The command lives inside the -c string. Naming the wrong thing is
        # worse than saying nothing, so the probe stays quiet.
        assert harness_program(argv) is None

    @pytest.mark.parametrize("argv", [[], ["timeout"], ["flock", "/tmp/only-a-lock"]])
    def test_an_argv_with_no_command_left_is_none(self, argv):
        assert harness_program(argv) is None

    def test_a_wrapper_chain_deeper_than_we_unpick_gives_up(self):
        argv = ["nice", "nice", "nice", "nice", "nice", "claude"]
        assert harness_program(argv) is None


class TestHarnessIsResolvable:
    def test_a_program_on_path_resolves(self):
        assert harness_is_resolvable("python3") is True

    def test_a_program_not_on_path_does_not(self):
        assert harness_is_resolvable("a8s-no-such-harness-xyz") is False

    def test_an_empty_path_resolves_nothing(self):
        # The reported failure: a non-login shell whose PATH lacks the rc
        # entries. Same binary, same machine, different environment.
        assert harness_is_resolvable("python3", {"PATH": ""}) is False

    def test_an_absolute_path_is_checked_directly(self, tmp_path):
        exe = tmp_path / "harness"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        assert harness_is_resolvable(str(exe), {"PATH": ""}) is True

    def test_an_absolute_path_that_is_not_executable_does_not(self, tmp_path):
        exe = tmp_path / "harness"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o644)
        assert harness_is_resolvable(str(exe)) is False

    def test_nothing_resolves_nothing(self):
        assert harness_is_resolvable("") is False

    def test_a_relative_path_is_checked_against_the_wake_cwd(self, tmp_path):
        # The reported failure: invoke `./curtis` with the node's root
        # registered. A wake runs with CWD set to that root, so the probe
        # must judge `./curtis` there — not in whatever directory the
        # operator happened to run `a8s start` from.
        exe = tmp_path / "curtis"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        assert harness_is_resolvable("./curtis", {"PATH": ""}, cwd=tmp_path) is True

    def test_a_relative_path_missing_from_the_wake_cwd_does_not(self, tmp_path):
        assert harness_is_resolvable("./curtis", {"PATH": ""}, cwd=tmp_path) is False

    def test_a_relative_path_with_no_cwd_falls_back_to_process_cwd(
        self, tmp_path, monkeypatch
    ):
        exe = tmp_path / "curtis"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        monkeypatch.chdir(tmp_path)
        assert harness_is_resolvable("./curtis", {"PATH": ""}) is True


class TestDefinitionEnv:
    """`definition.env` is OS environment for the wake, and nothing else.

    The knob it is constantly confused with is `a8s vars`, which substitutes
    `$NAME` into argv and never reaches the child's environment. Keeping the
    two apart is why nothing here expands.
    """

    def test_absent_is_empty(self):
        assert definition_env({"invoke": ["x"]}) == {}

    def test_pairs_come_through_literally(self):
        env = definition_env({"env": {"PATH": "/opt/bin:/usr/bin", "LANG": "C"}})
        assert env == {"PATH": "/opt/bin:/usr/bin", "LANG": "C"}

    def test_a_dollar_name_is_a_value_not_a_placeholder(self):
        assert definition_env({"env": {"PATH": "$HOME/bin"}}) == {"PATH": "$HOME/bin"}

    def test_a_non_object_is_refused(self):
        with pytest.raises(ValueError, match="object"):
            definition_env({"env": ["PATH=/usr/bin"]})

    def test_a_non_string_value_is_refused(self):
        with pytest.raises(ValueError, match="string"):
            definition_env({"env": {"TELL_FILE_MAX": 50}})

    @pytest.mark.parametrize("key", ["", "A=B", "A\0B", 3])
    def test_a_name_that_is_not_a_variable_name_is_refused(self, key):
        with pytest.raises(ValueError):
            definition_env({"env": {key: "x"}})


class TestWakeEnv:
    """`wake_path` is the fallback under `definition.env`, and both sit under
    the routing variables a8s injects (see daemon `_wake_env`)."""

    def test_no_knobs_means_inherit(self, fake_home):
        assert wake_env({"invoke": ["x"]}) == {}

    def test_wake_path_fills_in_a_missing_path(self, fake_home, monkeypatch):
        monkeypatch.setenv("A8S_WAKE_PATH", "/opt/bin:/usr/bin")
        assert wake_env({"invoke": ["x"]}) == {"PATH": "/opt/bin:/usr/bin"}

    def test_a_declared_path_beats_wake_path(self, fake_home, monkeypatch):
        monkeypatch.setenv("A8S_WAKE_PATH", "/machine/bin")
        assert wake_env({"env": {"PATH": "/node/bin"}}) == {"PATH": "/node/bin"}

    def test_wake_path_still_fills_in_beside_other_declared_vars(
        self, fake_home, monkeypatch
    ):
        monkeypatch.setenv("A8S_WAKE_PATH", "/machine/bin")
        assert wake_env({"env": {"LANG": "C"}}) == {
            "PATH": "/machine/bin",
            "LANG": "C",
        }

    def test_a_blank_wake_path_is_inherit(self, fake_home, monkeypatch):
        monkeypatch.setenv("A8S_WAKE_PATH", "   ")
        assert wake_env({"invoke": ["x"]}) == {}


class TestWakeShell:
    def test_absent_is_none(self):
        assert wake_shell({"invoke": ["x"]}) is None

    @pytest.mark.parametrize("raw", ["login", "LOGIN", "  Login  "])
    def test_login_is_the_one_value(self, raw):
        assert wake_shell({"wake_shell": raw}) == "login"

    def test_blank_is_none(self):
        assert wake_shell({"wake_shell": "  "}) is None

    @pytest.mark.parametrize("raw", ["interactive", "bash", "yes"])
    def test_any_other_word_is_a_typo_not_a_meaning(self, raw):
        with pytest.raises(ValueError, match="login"):
            wake_shell({"wake_shell": raw})

    def test_a_non_string_is_refused(self):
        with pytest.raises(ValueError, match="login"):
            wake_shell({"wake_shell": True})


class TestWrapWakeArgv:
    ARGV = ["claude", "-p", "hello world"]

    def test_no_opt_in_leaves_argv_alone(self):
        assert wrap_wake_argv({"invoke": ["x"]}, self.ARGV) == self.ARGV

    def test_the_wrap_uses_the_operators_own_shell(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/usr/bin/zsh")
        wrapped = wrap_wake_argv({"wake_shell": "login"}, self.ARGV)
        assert wrapped[0] == "/usr/bin/zsh"

    def test_the_flags_are_one_word_with_c_last(self, monkeypatch):
        # `-c -l "cmd"` runs `-l` as the command and makes `cmd` `$0`.
        monkeypatch.setenv("SHELL", "/bin/bash")
        wrapped = wrap_wake_argv({"wake_shell": "login"}, self.ARGV)
        assert wrapped == ["/bin/bash", "-ilc", "claude -p 'hello world'"]

    def test_an_unset_shell_falls_back_to_sh(self, monkeypatch):
        monkeypatch.delenv("SHELL", raising=False)
        assert wrap_wake_argv({"wake_shell": "login"}, self.ARGV)[0] == "/bin/sh"

    def test_windows_is_refused_not_ignored(self, monkeypatch):
        monkeypatch.setattr("definitions.IS_WINDOWS", True)
        with pytest.raises(ValueError, match="POSIX-only"):
            wrap_wake_argv({"wake_shell": "login"}, self.ARGV)

    def test_the_message_survives_the_shell_parse(self, monkeypatch):
        # The wrap puts a shell parse where there is none today, so a message
        # off the wire has to round-trip byte-exact through it.
        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("no bash on this machine")
        monkeypatch.setenv("SHELL", bash)
        body = "quotes ' \" backtick ` dollar $HOME\nsecond line"
        wrapped = wrap_wake_argv({"wake_shell": "login"}, ["printf", "%s", body])
        out = subprocess.run(wrapped, capture_output=True, text=True).stdout
        assert out.endswith(body)
