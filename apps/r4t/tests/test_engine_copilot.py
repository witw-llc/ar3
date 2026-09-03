"""Copilot is officially supported, which is a claim about wiring.

The suite's recurring defect is configuration that parses, validates and
documents cleanly with nothing behind it. Each test here fails if one piece of
copilot's wiring is removed, so "officially supported" cannot decay into a name
the tables know and nothing honours. The measured facts each assertion defends
are on the wiki's Engine-Copilot page and in #239.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from engines import copilot
from engines import run as engine_run
from rig import (
    RigError,
    HARNESS_PRESETS,
    add_preset_rig,
    load_rig_config,
    set_rig_value,
    unset_rig_value,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFINITIONS = REPO_ROOT / "apps" / "a8s" / "definitions"


def argv_for(**kwargs):
    base = dict(model=None, timeout=900, workdir=Path("/tmp"))
    base.update(kwargs)
    return engine_run.build_argv("copilot", "go", **base)


class TestHeadlessHardening:
    def test_preset_carries_silent_and_no_auto_update(self):
        invoke = HARNESS_PRESETS["copilot"]["invoke"]
        assert "-s" in invoke
        assert "--no-auto-update" in invoke

    def test_a_composed_turn_carries_them(self):
        argv = argv_for()
        assert "-s" in argv
        assert "--no-auto-update" in argv
        assert "--no-ask-user" in argv

    def test_bypass_keeps_them_while_swapping_the_permission_flag(self):
        argv = argv_for(permissions="bypass")
        assert "--allow-all" in argv and "--allow-all-tools" not in argv
        assert "-s" in argv and "--no-auto-update" in argv


class TestA8sDefinitions:
    @pytest.mark.parametrize(
        "name",
        ["copilot.json", "engine-copilot.json", "engine-copilot-unrestricted.json"],
    )
    def test_definition_exists_and_is_valid_json(self, name):
        data = json.loads((DEFINITIONS / name).read_text(encoding="utf-8"))
        assert data["description"]
        assert data["invoke"]

    def test_the_preset_points_at_a_definition_that_exists(self):
        named = HARNESS_PRESETS["copilot"]["a8s_definition"]
        assert (DEFINITIONS / named).is_file()

    def test_direct_definition_matches_the_preset_shape(self):
        data = json.loads((DEFINITIONS / "copilot.json").read_text(encoding="utf-8"))
        invoke = data["invoke"]
        assert invoke[0] == "copilot"
        for flag in ("--allow-all-tools", "-s", "--no-auto-update", "-p"):
            assert flag in invoke, f"copilot.json drops {flag}"

    def test_engine_definitions_route_through_r4t_engine_run(self):
        for name in ("engine-copilot.json", "engine-copilot-unrestricted.json"):
            data = json.loads((DEFINITIONS / name).read_text(encoding="utf-8"))
            for block in (data, data["batch"], data["idle"]):
                assert block["invoke"][2:5] == ["engine", "copilot", "run"]

    def test_unrestricted_definition_passes_permissions_bypass(self):
        data = json.loads(
            (DEFINITIONS / "engine-copilot-unrestricted.json").read_text(
                encoding="utf-8"
            )
        )
        for block in (data, data["batch"], data["idle"]):
            argv = block["invoke"]
            i = argv.index("run")
            assert argv[i + 1:i + 3] == ["--permissions", "bypass"]


USAGE_SAMPLE = {
    "totalPremiumRequestCost": 1,
    "totalUserRequests": 1,
    "totalNanoAiu": 5612490000,
    "tokenDetails": {
        "input": {"tokenCount": 3},
        "cache_write": {"tokenCount": 24918},
        "output": {"tokenCount": 5},
    },
    "modelMetrics": {
        "gpt-5.6-terra": {"cacheExpiresAt": "2026-09-02T19:41:04.053Z"},
    },
    "currentModel": "gpt-5.6-terra",
    "lastCallInputTokens": 24921,
    "lastCallOutputTokens": 5,
}


def fake_copilot(tmp_path, monkeypatch, *, writes_usage=True, writes_otel=False):
    """A stand-in `copilot` that records its argv and the exporter variable it
    was handed, and writes the files the real CLI would."""
    calls = tmp_path / "copilot-calls.json"
    # Executable with its own shebang, not `python script.py`: r4t splices the
    # per-turn flags in immediately after argv[0], which under the two-element
    # form would hand them to the interpreter instead of to the fake CLI.
    script = tmp_path / "fake-copilot"
    script.write_text(
        "\n".join([
            f"#!{sys.executable}",
            "import json, os, sys",
            f"calls = {str(calls)!r}",
            "otel = os.environ.get('COPILOT_OTEL_FILE_EXPORTER_PATH')",
            "json.dump({'argv': sys.argv[1:], 'otel': otel},",
            "          open(calls, 'w', encoding='utf-8'))",
            f"if {writes_usage!r} and '--usage-output-file' in sys.argv:",
            "    path = sys.argv[sys.argv.index('--usage-output-file') + 1]",
            f"    json.dump({USAGE_SAMPLE!r}, open(path, 'w', encoding='utf-8'))",
            f"if {writes_otel!r} and otel:",
            "    with open(otel, 'w', encoding='utf-8') as handle:",
            "        handle.write('{\"type\": \"span\"}\\n{\"type\": \"metric\"}\\n')",
            "",
        ]),
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setitem(
        HARNESS_PRESETS,
        "copilot",
        {
            **HARNESS_PRESETS["copilot"],
            "invoke": [str(script), "-p", "{prompt}"],
        },
    )
    monkeypatch.setattr(engine_run.copilot_engine, "supports_usage_file", lambda *a: True)
    return calls


class TestUsageFile:
    def test_read_usage_maps_the_measured_shape(self, tmp_path):
        path = tmp_path / "usage.json"
        path.write_text(json.dumps(USAGE_SAMPLE), encoding="utf-8")
        spend = copilot.read_usage(path)
        assert spend["credits"] == 5.6125
        assert spend["model"] == "gpt-5.6-terra"
        assert spend["premium_requests"] == 1
        assert spend["input_tokens"] == 3
        assert spend["output_tokens"] == 5
        assert spend["cache_write_tokens"] == 24918
        assert spend["cache_read_tokens"] is None
        assert spend["cache_expires_at"] == "2026-09-02T19:41:04.053Z"

    def test_read_usage_is_none_when_the_file_never_arrived(self, tmp_path):
        assert copilot.read_usage(tmp_path / "absent.json") is None
        empty = tmp_path / "empty.json"
        empty.write_text("", encoding="utf-8")
        assert copilot.read_usage(empty) is None

    def test_format_spend_is_one_line(self, tmp_path):
        path = tmp_path / "usage.json"
        path.write_text(json.dumps(USAGE_SAMPLE), encoding="utf-8")
        assert copilot.format_spend(copilot.read_usage(path)) == (
            "spend: 5.61 credits · gpt-5.6-terra · cache write 24918 / read 0"
        )

    def test_supported_binary_gets_the_flag(self, tmp_path, monkeypatch):
        monkeypatch.setattr(copilot, "supports_usage_file", lambda *a: True)
        with copilot.turn_instruments("copilot", scratch=tmp_path) as instruments:
            argv = engine_run._build_argv_template(
                "copilot", model=None, timeout=900, workdir=tmp_path,
                instruments=instruments,
            )[0]
        i = argv.index("--usage-output-file")
        assert argv[i + 1] == str(tmp_path / copilot.USAGE_BASENAME)

    def test_an_old_binary_arms_no_usage_file(self, r4t_home, tmp_path, monkeypatch):
        # 1.0.80 rejects the flag as an unknown option and runs no turn, so a
        # preset that always carried it would break every older seat.
        monkeypatch.setattr(copilot, "supports_usage_file", lambda *a: False)
        with copilot.turn_instruments("copilot", scratch=tmp_path) as instruments:
            assert instruments.usage_file is None
            # The exporter is independent of the flag's version boundary.
            assert instruments.otel_file is not None
            assert instruments.armed

    def test_only_an_instrumented_preset_arms_one(self, r4t_home, tmp_path, monkeypatch):
        monkeypatch.setattr(copilot, "supports_usage_file", lambda *a: True)
        for preset in (None, "claude", "ollama-copilot"):
            kind = HARNESS_PRESETS.get(preset or "", {}).get("instruments")
            with copilot.turn_instruments(kind, scratch=tmp_path) as instruments:
                assert instruments.usage_file is None
                assert instruments.otel_file is None
                assert not instruments.armed
                assert instruments.flags() == []

    def test_a_leftover_file_is_cleared_before_the_turn(self, r4t_home, tmp_path, monkeypatch):
        # A turn killed hard leaves its files behind; reading those would
        # report the previous turn's spend as this one's.
        monkeypatch.setattr(copilot, "supports_usage_file", lambda *a: True)
        stale = tmp_path / copilot.USAGE_BASENAME
        stale.write_text(json.dumps(USAGE_SAMPLE), encoding="utf-8")
        with copilot.turn_instruments("copilot", scratch=tmp_path) as instruments:
            assert not stale.exists()
            measured, lines = instruments.measure()
            assert "spend" not in measured  # the stale numbers are gone
            assert lines == ["otel: not written (org telemetry policy or race)"]

    def test_the_preset_does_not_carry_the_flag(self):
        assert "--usage-output-file" not in HARNESS_PRESETS["copilot"]["invoke"]

    def test_a_turn_reports_its_spend(self, r4t_home, tmp_path, monkeypatch, capsys):
        calls = fake_copilot(tmp_path, monkeypatch)
        record: dict = {}
        code = engine_run.execute(
            "copilot", "hi", dir_path=tmp_path, model=None, agent=None,
            timeout=30, scaffold=False, record=record,
        )
        assert code == 0
        assert "--usage-output-file" in json.loads(calls.read_text())["argv"]
        assert record["spend"]["credits"] == 5.6125
        assert "spend: 5.61 credits · gpt-5.6-terra" in capsys.readouterr().err

    def test_a_turn_that_wrote_no_usage_file_reports_nothing(
        self, r4t_home, tmp_path, monkeypatch, capsys
    ):
        fake_copilot(tmp_path, monkeypatch, writes_usage=False)
        record: dict = {}
        engine_run.execute(
            "copilot", "hi", dir_path=tmp_path, model=None, agent=None,
            timeout=30, scaffold=False, record=record,
        )
        assert "spend" not in record
        assert "spend:" not in capsys.readouterr().err

    def test_the_usage_file_is_deleted_after_it_is_read(
        self, r4t_home, tmp_path, monkeypatch
    ):
        fake_copilot(tmp_path, monkeypatch)
        seen: list[str] = []
        original = copilot.read_usage
        monkeypatch.setattr(
            engine_run.copilot_engine,
            "read_usage",
            lambda path: (seen.append(str(path)), original(path))[1],
        )
        engine_run.execute(
            "copilot", "hi", dir_path=tmp_path, model=None, agent=None,
            timeout=30, scaffold=False,
        )
        assert seen and not Path(seen[0]).exists()


class TestSpendFuse:
    def test_a_cap_composes_the_flag(self, tmp_path, monkeypatch):
        monkeypatch.setattr(copilot, "supports_usage_file", lambda *a: False)
        with copilot.turn_instruments(
            "copilot", scratch=tmp_path, max_credits=30
        ) as instruments:
            argv = engine_run._build_argv_template(
                "copilot", model=None, timeout=900, workdir=tmp_path,
                instruments=instruments, max_credits=30,
            )[0]
        assert argv[argv.index("--max-ai-credits") + 1] == "30"

    def test_below_copilots_floor_is_refused(self, tmp_path):
        # `copilot help limits` on 1.0.82: "Minimum: 30 AI credits."
        with pytest.raises(engine_run.RunError, match="below 30"):
            engine_run._build_argv_template(
                "copilot", model=None, timeout=900, workdir=tmp_path, max_credits=29
            )

    def test_an_engine_with_no_fuse_is_refused(self, tmp_path):
        with pytest.raises(engine_run.RunError, match="expresses no spend fuse"):
            engine_run._build_argv_template(
                "claude", model=None, timeout=900, workdir=tmp_path, max_credits=50
            )

    def test_unset_composes_nothing(self, tmp_path):
        argv = engine_run._build_argv_template(
            "copilot", model=None, timeout=900, workdir=tmp_path
        )[0]
        assert "--max-ai-credits" not in argv

    def test_a_rig_can_carry_one(self, tmp_path):
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "cop", "copilot")
        set_rig_value(path, "cop", "max_ai_credits", "50")
        assert load_rig_config(path).rigs["cop"].max_ai_credits == 50
        assert unset_rig_value(path, "cop", "max_ai_credits") is True
        assert load_rig_config(path).rigs["cop"].max_ai_credits is None

    def test_a_rig_below_the_floor_is_refused_at_load(self, tmp_path):
        # The reviewer's control: a roster rig with a fuse copilot rejects
        # must not load, or the roster charges its own budget and then
        # spawns a turn the CLI refuses on the flag.
        path = tmp_path / "rigs.json"
        add_preset_rig(path, "cop", "copilot")
        set_rig_value(path, "cop", "max_ai_credits", "29")
        rig = load_rig_config(path).rigs["cop"]
        assert rig.error is not None and "max_ai_credits: copilot takes no" in rig.error
        assert rig.max_ai_credits is None


class TestOtelExport:
    def test_the_turn_is_handed_its_own_exporter_path(
        self, r4t_home, tmp_path, monkeypatch, capsys
    ):
        calls = fake_copilot(tmp_path, monkeypatch, writes_otel=True)
        record: dict = {}
        engine_run.execute(
            "copilot", "hi", dir_path=tmp_path, model=None, agent=None,
            timeout=30, scaffold=False, record=record,
        )
        handed = json.loads(calls.read_text())["otel"]
        assert handed and handed.endswith(copilot.OTEL_BASENAME)
        assert record["otel"]["records"] == 2
        # Reported at the durable path, not the scratch one it was written to:
        # the file is moved out of the turn's scratch on the way past.
        kept = Path(record["otel"]["path"])
        assert kept.parent == copilot.otel_dir() and kept.is_file()
        assert f"otel: {kept} (2 records)" in capsys.readouterr().err

    def test_an_absent_file_is_noted_and_never_fails_the_turn(
        self, r4t_home, tmp_path, monkeypatch, capsys
    ):
        # The expected outcome on a seat whose organisation telemetry policy
        # redirects the exporter to its own collector.
        fake_copilot(tmp_path, monkeypatch, writes_otel=False)
        record: dict = {}
        code = engine_run.execute(
            "copilot", "hi", dir_path=tmp_path, model=None, agent=None,
            timeout=30, scaffold=False, record=record,
        )
        assert code == 0
        assert record["otel"]["records"] is None
        assert record["otel"]["path"] is None
        assert "otel: not written (org telemetry policy or race)" in (
            capsys.readouterr().err
        )

    def test_the_exported_file_is_kept(self, r4t_home, tmp_path, monkeypatch):
        fake_copilot(tmp_path, monkeypatch, writes_otel=True)
        record: dict = {}
        engine_run.execute(
            "copilot", "hi", dir_path=tmp_path, model=None, agent=None,
            timeout=30, scaffold=False, record=record,
        )
        kept = Path(record["otel"]["path"])
        assert kept.is_file()
        assert kept.parent == copilot.otel_dir()

    def test_another_engine_gets_no_exporter_variable(self, r4t_home, tmp_path):
        with copilot.turn_instruments(None, scratch=tmp_path) as instruments:
            assert instruments.env_for(None) is None
            assert instruments.env_for({"A": "b"}) == {"A": "b"}
            assert instruments.pass_env() == {}


class TestSessionPin:
    def test_a_new_session_is_founded_by_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "copilot-home"))
        argv = engine_run._build_argv_template(
            "copilot", model=None, timeout=900, workdir=tmp_path,
            session="11111111-2222-3333-4444-555555555555",
        )[0]
        i = argv.index("--session-id")
        assert argv[i + 1] == "11111111-2222-3333-4444-555555555555"
        assert not any(a.startswith("--resume") for a in argv)

    def test_an_existing_session_is_resumed_in_the_turns_own_dir(
        self, tmp_path, monkeypatch
    ):
        # A resumed copilot session otherwise runs in the directory it was
        # founded in, whatever the invoking cwd — so -C is not optional.
        home = tmp_path / "copilot-home"
        session = "11111111-2222-3333-4444-555555555555"
        (home / "session-state" / session).mkdir(parents=True)
        monkeypatch.setenv("COPILOT_HOME", str(home))
        argv = engine_run._build_argv_template(
            "copilot", model=None, timeout=900, workdir=tmp_path, session=session,
        )[0]
        assert f"--resume={session}" in argv
        assert argv[argv.index("-C") + 1] == str(tmp_path)
        assert "--session-id" not in argv

    def test_bare_continue_stays_refused(self, tmp_path):
        with pytest.raises(engine_run.RunError) as caught:
            engine_run.build_argv(
                "copilot", "go", model=None, timeout=900, workdir=tmp_path,
                continue_conversation=True,
            )
        message = str(caught.value)
        assert "live session" in message
        assert "--session <uuid>" in message
        # It never names itself as an engine that can continue.
        assert "can: copilot" not in message

    def test_session_and_continue_contradict(self, tmp_path):
        with pytest.raises(engine_run.RunError, match="contradict"):
            engine_run.build_argv(
                "copilot", "go", model=None, timeout=900, workdir=tmp_path,
                continue_conversation=True, session="a-b-c",
            )

    def test_an_engine_with_no_pin_is_refused(self, tmp_path):
        with pytest.raises(engine_run.RunError, match="takes no --session"):
            engine_run.build_argv(
                "claude", "go", model=None, timeout=900, workdir=tmp_path,
                session="a-b-c",
            )


# The second live sample on the wiki's Engine-Copilot page (section 13): a
# token-based-billing Business seat, whose fractions are degenerate and whose
# only real signal is cumulative credits plus the reset date.
USER_SAMPLE = {
    "copilot_plan": "business",
    "quota_reset_date_utc": "2026-10-01T00:00:00.000Z",
    "quota_snapshots": {
        "chat": {"unlimited": True, "percent_remaining": 100, "credits_used": 0},
        "completions": {
            "unlimited": True, "percent_remaining": 100, "credits_used": 0,
        },
        "premium_interactions": {
            "unlimited": True, "percent_remaining": 100, "credits_used": 395,
        },
    },
}


class TestQuotaTransport:
    """`gh` is a convenience, not a prerequisite. A seat authenticated to
    Copilot throughout was told `gh is not on PATH`, which is a wrong answer
    rather than a missing one (#239)."""

    def test_gh_is_used_when_it_is_there(self, monkeypatch):
        monkeypatch.setattr(copilot.shutil, "which", lambda _n: "/usr/bin/gh")
        monkeypatch.setattr(copilot, "_gh_user", lambda: USER_SAMPLE)
        monkeypatch.setattr(
            copilot, "_direct_user", lambda _t: pytest.fail("gh was skipped")
        )
        assert copilot.quota()["plan"] == "business"

    def test_without_gh_an_env_token_carries_the_call(self, monkeypatch):
        monkeypatch.setattr(copilot.shutil, "which", lambda _n: None)
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok-env")
        seen: list[str] = []
        monkeypatch.setattr(
            copilot, "_direct_user", lambda auth: (seen.append(auth), USER_SAMPLE)[1]
        )
        assert copilot.quota()["plan"] == "business"
        assert seen == ["tok-env"]

    def test_the_env_precedence_is_copilots_own(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok-three")
        assert copilot.token() == "tok-three"
        monkeypatch.setenv("GH_TOKEN", "tok-two")
        assert copilot.token() == "tok-two"
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "tok-one")
        assert copilot.token() == "tok-one"

    def test_the_stored_login_is_read_from_a_jsonc_file(self, tmp_path, monkeypatch):
        for name in copilot.TOKEN_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        home = tmp_path / "copilot-home"
        home.mkdir()
        # The real file opens with a comment line, which plain json refuses.
        (home / "config.json").write_text(
            '// User settings\n{"copilotTokens": {"github.com": "tok-stored"}}\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("COPILOT_HOME", str(home))
        assert copilot.token() == "tok-stored"

    def test_no_token_anywhere_says_what_to_do(self, tmp_path, monkeypatch):
        for name in copilot.TOKEN_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "empty"))
        monkeypatch.setattr(copilot.shutil, "which", lambda _n: None)
        with pytest.raises(copilot.QuotaError) as caught:
            copilot.quota()
        assert "COPILOT_GITHUB_TOKEN" in str(caught.value)

    def test_the_token_never_reaches_an_error_message(self, tmp_path, monkeypatch):
        import urllib.error

        monkeypatch.setattr(copilot.shutil, "which", lambda _n: None)
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "secret-token-value")

        def refuse(_request, timeout=None):
            raise urllib.error.HTTPError(
                copilot.USER_ENDPOINT, 401, "Unauthorized", {}, None
            )

        monkeypatch.setattr(copilot.urllib.request, "urlopen", refuse)
        with pytest.raises(copilot.QuotaError) as caught:
            copilot.quota()
        assert "secret-token-value" not in str(caught.value)
        assert "401" in str(caught.value)

    def test_credits_used_is_a_number_beside_the_note(self):
        payload = copilot.parse_user(USER_SAMPLE)
        premium = next(
            b for b in payload["buckets"] if b["label"] == "Premium Requests"
        )
        assert premium["credits_used"] == 395
        assert premium["remaining_fraction"] is None  # degenerate on this plan
        assert "395 credits used" in payload["note"]
