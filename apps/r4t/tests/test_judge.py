"""Post-hoc judge — evidence collection, chunked rig invokes, and both report
surfaces."""
from __future__ import annotations

import io
import json
import sys
import textwrap

import pytest

import judge
import state

NODE = "acme"


@pytest.fixture
def judge_harness(tmp_path):
    """Records its prompt, then replies with $JUDGE_REPLY (default: an empty
    verdict) and exits $JUDGE_EXIT — a rig-shaped stand-in for the LLM."""
    script = tmp_path / "judge-harness.py"
    calls = tmp_path / "judge-calls"
    calls.mkdir()
    script.write_text(
        textwrap.dedent(
            f"""\
            import os, sys
            calls_dir = {str(calls)!r}
            n = len(os.listdir(calls_dir))
            with open(os.path.join(calls_dir, f"call-{{n:03d}}.txt"), "w") as f:
                f.write(sys.argv[1])
            print(os.environ.get("JUDGE_REPLY", '{{"findings": []}}'))
            sys.exit(int(os.environ.get("JUDGE_EXIT", "0")))
            """
        ),
        encoding="utf-8",
    )
    return script, calls


@pytest.fixture
def judge_config(tmp_path, judge_harness):
    script, _calls = judge_harness
    path = tmp_path / "judge-rigs.json"
    payload = {
        "grader": {
            "invoke": [sys.executable, str(script), "{prompt}"],
            "timeout_seconds": 30,
        }
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _seed_capture(member="phil", body="did the work", stamp="0" * 20):
    content = f"# turn {stamp} ({member})\n\n## Prompt\n\nbuild it\n\n## Output\n\n{body}\n"
    return state.write_turn_capture(NODE, member, stamp, "01X", content)


def _reply(findings):
    return json.dumps({"findings": findings})


def _run(config, *, node=NODE, rig="grader", json_mode=False):
    out, err = io.StringIO(), io.StringIO()
    code = judge.run(
        node, rig_name=rig, config_path=config, json_mode=json_mode,
        out=out, err=err,
    )
    return code, out.getvalue(), err.getvalue()


def test_no_captures_refuses_cleanly(r4t_home, judge_config, judge_harness):
    _script, calls = judge_harness
    code, out, err = _run(judge_config)
    assert code == 2
    assert "no turn captures" in err
    assert "(try:" in err
    assert out == ""
    assert not list(calls.iterdir())
    assert not (state.team_dir(NODE) / "judge").exists()


def test_findings_render_as_panel(r4t_home, judge_config, monkeypatch):
    path = _seed_capture()
    monkeypatch.setenv(
        "JUDGE_REPLY",
        _reply([{
            "mode": "FM-3.2", "member": "phil", "turn": path.stem,
            "evidence": "declared done off a 0.29s smoke test",
        }]),
    )
    code, out, err = _run(judge_config)
    assert code == 0
    assert err == ""
    assert f"judge: {NODE}" in out
    assert "✗ FM-3.2 No or incomplete verification" in out
    assert f"phil / {path.stem}: declared done off a 0.29s smoke test" in out
    assert "✓ FM-1.1 Disobey task specification" in out
    assert "✗ 1 finding(s) across 1 mode(s)" in out


def test_reports_persist_under_team_judge_dir(r4t_home, judge_config):
    _seed_capture()
    code, out, _err = _run(judge_config)
    assert code == 0
    home = state.team_dir(NODE) / "judge"
    reports = sorted(home.iterdir())
    assert [p.suffix for p in reports] == [".json", ".md"]
    assert str(reports[1]) in out
    assert home.parent == state.team_dir(NODE)
    assert "agents" not in reports[0].parts[-3:]


def test_json_mode_emits_machine_report(r4t_home, judge_config, monkeypatch):
    path = _seed_capture()
    monkeypatch.setenv(
        "JUDGE_REPLY",
        _reply([{
            "mode": "fm-r.1", "member": "phil", "turn": path.stem,
            "evidence": "waiting on gerry who was waiting on phil",
        }]),
    )
    code, out, _err = _run(judge_config, json_mode=True)
    assert code == 0
    payload = json.loads(out)
    assert payload["node"] == NODE
    assert payload["total_findings"] == 1
    mode = payload["modes"]["FM-R.1"]
    assert mode["count"] == 1
    assert mode["category"] == "R4T"
    assert mode["evidence"][0]["member"] == "phil"
    assert mode["evidence"][0]["turn"] == path.stem
    assert payload["modes"]["FM-1.1"]["count"] == 0


def test_missing_rig_errors_action_first(r4t_home, judge_config):
    _seed_capture()
    code, out, err = _run(judge_config, rig="nope")
    assert code == 2
    assert "rig 'nope' not found" in err
    assert "(try: r4t rig add nope" in err
    assert out == ""


def test_missing_config_errors_action_first(r4t_home, tmp_path):
    _seed_capture()
    code, _out, err = _run(tmp_path / "absent.json")
    assert code == 2
    assert "no rig config" in err
    assert "(try: r4t rig add grader" in err


def test_prompt_carries_taxonomy_captures_and_context(
    r4t_home, judge_config, judge_harness
):
    _script, calls = judge_harness
    _seed_capture(body="the widget is perfectly validated")
    state.append_log(NODE, "r4t: REROUTED acme:phil -> Gerry")
    state.record_dead_letter(
        NODE, reason="budget", sender="acme:phil", to="acme:gerry",
        thread="01X", content="x",
    )
    code, _out, _err = _run(judge_config)
    assert code == 0
    prompt = (calls / "call-000.txt").read_text(encoding="utf-8")
    assert "the widget is perfectly validated" in prompt
    assert "arXiv:2503.13657" in prompt
    assert "FM-R.1 Mutual-wait deadlock" in prompt
    assert "r4t extension" in prompt
    assert "REROUTED" in prompt
    assert "budget" in prompt


def test_prompt_teaches_stdout_fallback_semantics(
    r4t_home, judge_config, judge_harness
):
    _script, calls = judge_harness
    _seed_capture()
    code, _out, _err = _run(judge_config)
    assert code == 0
    prompt = (calls / "call-000.txt").read_text(encoding="utf-8")
    assert "stdout fallback" in prompt
    assert "ONE reply to the sender" in prompt
    assert "recipients other than" in prompt
    assert "genuinely silent or chrome-only" in prompt


def test_chunking_invokes_per_chunk_and_aggregates(
    r4t_home, judge_config, judge_harness, monkeypatch
):
    _script, calls = judge_harness
    monkeypatch.setattr(judge, "CHUNK_MAX_CHARS", 600)
    for i in range(3):
        _seed_capture(body="x" * 400, stamp=f"{i:020d}")
    monkeypatch.setenv(
        "JUDGE_REPLY",
        _reply([{
            "mode": "FM-1.3", "member": "phil", "turn": "t", "evidence": "loop",
        }]),
    )
    code, out, _err = _run(judge_config, json_mode=True)
    assert code == 0
    payload = json.loads(out)
    assert payload["invokes"] == len(list(calls.iterdir()))
    assert payload["invokes"] >= 2
    assert payload["modes"]["FM-1.3"]["count"] == payload["invokes"]


def test_unknown_mode_dropped_with_warning(r4t_home, judge_config, monkeypatch):
    _seed_capture()
    monkeypatch.setenv(
        "JUDGE_REPLY",
        _reply([{"mode": "FM-9.9", "member": "phil", "turn": "t", "evidence": "?"}]),
    )
    code, out, _err = _run(judge_config)
    assert code == 0
    assert "unknown mode 'FM-9.9' dropped" in out
    assert "✓ no findings" in out


def test_harness_failure_is_operational_error(r4t_home, judge_config, monkeypatch):
    _seed_capture()
    monkeypatch.setenv("JUDGE_EXIT", "3")
    code, out, err = _run(judge_config)
    assert code == 2
    assert "invoke failed (exit 3)" in err
    assert "(try:" in err
    assert out == ""


def test_no_parsable_verdict_anywhere_fails(r4t_home, judge_config, monkeypatch):
    _seed_capture()
    monkeypatch.setenv("JUDGE_REPLY", "I could not decide, sorry")
    code, _out, err = _run(judge_config)
    assert code == 2
    assert "no parsable verdict" in err


def test_fenced_json_verdict_parses(r4t_home, judge_config, monkeypatch):
    path = _seed_capture()
    fenced = "Here you go:\n```json\n" + _reply(
        [{"mode": "FM-2.5", "member": "phil", "turn": path.stem, "evidence": "silent"}]
    ) + "\n```\ndone"
    monkeypatch.setenv("JUDGE_REPLY", fenced)
    code, out, _err = _run(judge_config)
    assert code == 0
    assert "✗ FM-2.5 Ignored other agent's input" in out


def test_cli_wiring(r4t_home, judge_config, monkeypatch, capsys):
    import r4t

    _seed_capture()
    code = r4t.main([
        "judge", NODE, "--rig", "grader", "--rig-config", str(judge_config),
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert f"judge: {NODE}" in out
    assert "Summary" in out
