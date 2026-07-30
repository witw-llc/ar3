"""`r4t lab` — manifest loading, the fake end-to-end pipeline, aggregation
math, exclusion logging, and the never-pool-across-models invariant. No live
LLM is ever invoked; the fake judge (deterministic) drives the pipeline."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import lab

E0 = "e0-noise-floor"
E5A = "e5a-persona-presence"


# ----------------------------------------------------------------------------
# Manifest validation
# ----------------------------------------------------------------------------

def _write_experiment(root: Path, name: str, manifest: dict, protocol: str = "clean") -> Path:
    d = root / name
    (d / "fixtures").mkdir(parents=True)
    (d / "experiment.json").write_text(json.dumps(manifest), encoding="utf-8")
    (d / "PROTOCOL.md").write_text(protocol, encoding="utf-8")
    return d


def _base_manifest(**over) -> dict:
    m = {
        "name": "x",
        "question": "q",
        "class": "posthoc",
        "arms": {"A": "a", "B": "b"},
        "roles": {"judge": {"rig": "local", "pin": "qwen3.6"}},
        "trials_per_arm": 2,
        "stopping_rule": None,
        "box_seconds": 60,
        "metrics": ["accuracy"],
        "predictions": [],
    }
    m.update(over)
    return m


@pytest.fixture
def experiments(tmp_path, monkeypatch):
    root = tmp_path / "experiments"
    root.mkdir()
    monkeypatch.setattr(lab, "EXPERIMENTS_DIR", root)
    return root


def test_unknown_class_rejected(experiments):
    _write_experiment(experiments, "bad", _base_manifest(**{"class": "quantum"}))
    with pytest.raises(lab.LabError, match="unknown class"):
        lab.load_manifest("bad")


def test_unknown_metric_rejected(experiments):
    _write_experiment(experiments, "bad", _base_manifest(metrics=["nonsense"]))
    with pytest.raises(lab.LabError, match="unknown metric"):
        lab.load_manifest("bad")


def test_org_class_accepted_by_loader_but_run_refuses(experiments, tmp_path, monkeypatch):
    monkeypatch.setenv("R4T_HOME", str(tmp_path / "home"))
    _write_experiment(experiments, "org1", _base_manifest(**{"class": "org"}))
    manifest = lab.load_manifest("org1")
    assert manifest.cls == "org"
    rc = lab.run_experiment("org1", arm=None, n=1, fake=True, log=lambda m: None)
    assert rc == 2  # "org-class experiments land in a follow-up PR"


def test_protocol_placeholder_refuses_to_run(experiments, tmp_path, monkeypatch):
    monkeypatch.setenv("R4T_HOME", str(tmp_path / "home"))
    _write_experiment(
        experiments, "ph", _base_manifest(),
        protocol="Owner: <who blesses this>\nStop: <duration>\n",
    )
    manifest = lab.load_manifest("ph")
    assert lab.protocol_placeholders(manifest)  # placeholders detected
    logs: list[str] = []
    rc = lab.run_experiment("ph", arm=None, n=1, fake=True, log=logs.append)
    assert rc == 2
    assert any("placeholder" in m for m in logs)


def test_e0_manifest_has_no_placeholders():
    manifest = lab.load_manifest(E0)
    assert lab.protocol_placeholders(manifest) == []
    assert manifest.cls == "posthoc"
    assert manifest.roles["judge"]["rig"] == "local"
    assert manifest.roles["judge"]["pin"] == "qwen3.6"


# ----------------------------------------------------------------------------
# Fake end-to-end pipeline
# ----------------------------------------------------------------------------

def test_fake_run_produces_ledger_and_report(tmp_path, monkeypatch):
    monkeypatch.setenv("R4T_HOME", str(tmp_path / "home"))
    rc = lab.run_experiment(E0, arm=None, n=3, fake=True, log=lambda m: None)
    assert rc == 0

    rows = lab.read_ledger(E0)
    assert len(rows) == 6  # 3 per arm, alternated
    row = rows[0]
    for key in ("trial", "stamp", "arm", "environment", "metrics",
                "exit_reason", "wall_clock_seconds", "report_path",
                "raw_sha256", "excluded"):
        assert key in row
    assert row["environment"]["roles"]["judge"]["resolved_model"] == "fake-judge"
    assert len(row["raw_sha256"]["judge_output"]) == 64  # sha256 hex
    assert Path(row["report_path"]).is_file()

    manifest = lab.load_manifest(E0)
    report = lab.render_report(manifest, rows)
    assert "within_arm_consistency" in report
    assert "cross_arm_agreement" in report
    assert "Predictions" in report
    # Deterministic fake: perfect consistency, both predictions held.
    assert "held" in report
    assert "falsified" not in report


def test_fake_run_single_arm(tmp_path, monkeypatch):
    monkeypatch.setenv("R4T_HOME", str(tmp_path / "home"))
    rc = lab.run_experiment(E0, arm="A", n=2, fake=True, log=lambda m: None)
    assert rc == 0
    rows = lab.read_ledger(E0)
    assert len(rows) == 2
    assert {r["arm"] for r in rows} == {"A"}


def test_posthoc_judge_runs_in_hermetic_cwd(tmp_path, monkeypatch):
    """A tool-enabled judge that writes into its cwd (observed live with
    `opencode --auto`) must land in the trial's own temp dir — never the
    operator's cwd — and that dir dies with the trial."""
    monkeypatch.setenv("R4T_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("R4T_LAB_FAKE_SCRATCH", "1")
    operator_cwd = tmp_path / "operator"
    operator_cwd.mkdir()
    monkeypatch.chdir(operator_cwd)

    rc = lab.run_experiment(E0, arm="A", n=1, fake=True, log=lambda m: None)
    assert rc == 0
    rows = lab.read_ledger(E0)
    assert len(rows) == 1 and not rows[0]["excluded"]  # scratch line is noise

    raw = (Path(rows[0]["report_path"]).parent / f"{rows[0]['trial']}.raw.txt")
    scratch_line = next(
        line for line in raw.read_text(encoding="utf-8").splitlines()
        if line.startswith("scratch: ")
    )
    scratch = Path(scratch_line.removeprefix("scratch: "))
    assert scratch.parent != operator_cwd  # never the operator's repo
    assert "r4t-lab-" in scratch.parent.name  # the trial's own workspace
    assert not scratch.exists()  # gone with teardown
    assert not scratch.parent.exists()
    assert list(operator_cwd.iterdir()) == []  # operator cwd untouched


def test_unparseable_output_excluded_not_dropped(tmp_path, monkeypatch):
    monkeypatch.setenv("R4T_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("R4T_LAB_FAKE_PARSE_ERROR", "1")
    rc = lab.run_experiment(E0, arm="A", n=2, fake=True, log=lambda m: None)
    assert rc == 0
    rows = lab.read_ledger(E0)
    assert len(rows) == 2  # recorded, never silently discarded
    assert all(r["excluded"] for r in rows)
    assert all(r["excluded_reason"] == "parse_error" for r in rows)
    assert all(r["exit_reason"] == "parse_error" for r in rows)
    assert all(r["metrics"] == {} for r in rows)


# ----------------------------------------------------------------------------
# Statistics
# ----------------------------------------------------------------------------

def test_sign_test_mixed():
    t = lab.sign_test([1, 1, 1, 0], [0, 0, 0, 1])
    assert t["wins"] == 3 and t["losses"] == 1 and t["n"] == 4
    assert t["p"] == pytest.approx(2 * (1 + 4) / 16)
    assert "mixed" in lab.sign_verdict(t)


def test_sign_test_consistent():
    t = lab.sign_test([1, 1, 1], [0, 0, 0])
    assert t["wins"] == 3 and t["losses"] == 0
    assert lab.sign_verdict(t) == "consistent (N=3/3 same direction)"


def test_sign_test_all_ties():
    t = lab.sign_test([0.5, 0.5], [0.5, 0.5])
    assert t["n"] == 0 and t["p"] == 1.0
    assert lab.sign_verdict(t) == "no separation (all ties)"


def test_bootstrap_ci_deterministic_and_bounds():
    a = [1.0, 1.0, 1.0, 0.75]
    b = [0.5, 0.75, 0.5, 0.5]
    r1 = lab.bootstrap_diff_ci(a, b)
    r2 = lab.bootstrap_diff_ci(a, b)
    assert r1 == r2  # seeded -> reproducible
    point, lo, hi = r1
    assert point == pytest.approx(lab.mean(a) - lab.mean(b))
    assert lo <= point <= hi


def test_cohen_kappa_perfect_and_chance():
    assert lab.cohen_kappa(["yes", "no", "yes"], ["yes", "no", "yes"]) == pytest.approx(1.0)
    # 50% observed agreement with 50% chance agreement -> kappa 0.
    k = lab.cohen_kappa(["yes", "no", "yes", "no"], ["yes", "no", "no", "yes"])
    assert k == pytest.approx(0.0)
    assert lab.cohen_kappa([], []) is None


def test_within_arm_consistency():
    trials = [
        {"answers": {"Q1": "yes", "Q2": "no"}},
        {"answers": {"Q1": "yes", "Q2": "yes"}},
        {"answers": {"Q1": "yes", "Q2": "no"}},
    ]
    # Q1: 3/3 agree (1.0); Q2: modal "no" 2/3 -> 0.667; mean 0.833.
    assert lab.within_arm_consistency(trials) == pytest.approx((1.0 + 2 / 3) / 2)
    assert lab.within_arm_consistency(trials[:1]) is None  # need >= 2


def test_cross_arm_agreement():
    modal_a = {"Q1": "yes", "Q2": "no"}
    modal_b = {"Q5": "yes", "Q6": "yes"}
    pairing = [("Q1", "Q5"), ("Q2", "Q6")]
    assert lab.cross_arm_agreement(modal_a, modal_b, pairing) == pytest.approx(0.5)


def test_score_prediction_held_and_falsified():
    aggregates = {("cross_arm_agreement", "overall"): 0.9,
                  ("within_arm_consistency", "each_arm"): 0.5}
    held = lab.score_prediction(
        {"claim": "c", "confidence": 0.7,
         "check": {"metric": "cross_arm_agreement", "op": ">=", "value": 0.75}},
        aggregates,
    )
    assert held["outcome"] == "held"
    assert held["brier"] == pytest.approx((0.7 - 1.0) ** 2)

    falsified = lab.score_prediction(
        {"claim": "c", "confidence": 0.6,
         "check": {"metric": "within_arm_consistency", "op": ">=",
                   "value": 0.75, "scope": "each_arm"}},
        aggregates,
    )
    assert falsified["outcome"] == "falsified"
    assert falsified["brier"] == pytest.approx((0.6 - 0.0) ** 2)

    undecided = lab.score_prediction({"claim": "c", "confidence": 0.5}, aggregates)
    assert undecided["outcome"] == "undecided"
    assert undecided["brier"] is None


# ----------------------------------------------------------------------------
# Never pool across resolved models (spec 5)
# ----------------------------------------------------------------------------

def _fake_row(arm, digest, resolved, answers, accuracy, mismatch=False):
    return {
        "trial": f"t-{digest}-{arm}-{accuracy}",
        "arm": arm,
        "environment": {"resolved_model": resolved, "model_digest": digest,
                        "pin_mismatch": mismatch},
        "metrics": {"accuracy": accuracy},
        "answers": answers,
        "excluded": False,
        "excluded_reason": None,
    }


def test_aggregate_never_pools_across_models():
    manifest = lab.load_manifest(E0)
    a_ans = {"Q1": "yes", "Q2": "no", "Q3": "yes", "Q4": "no"}
    b_ans = {"Q5": "yes", "Q6": "no", "Q7": "yes", "Q8": "no"}
    rows = [
        _fake_row("A", "digest1", "qwen3.6:latest", a_ans, 1.0),
        _fake_row("A", "digest1", "qwen3.6:latest", a_ans, 1.0),
        _fake_row("B", "digest1", "qwen3.6:latest", b_ans, 1.0),
        _fake_row("B", "digest1", "qwen3.6:latest", b_ans, 1.0),
        _fake_row("A", "digest2", "qwen3:1.7b", a_ans, 0.5, mismatch=True),
        _fake_row("B", "digest2", "qwen3:1.7b", b_ans, 0.5, mismatch=True),
    ]
    by_model = lab.aggregate(manifest, rows)
    assert set(by_model) == {"digest1", "digest2"}  # separate columns
    assert by_model["digest1"]["arms"]["A"]["n"] == 2
    assert by_model["digest2"]["arms"]["A"]["n"] == 1

    report = lab.render_report(manifest, rows)
    assert "qwen3.6:latest" in report
    assert "qwen3:1.7b" in report
    assert "pin_mismatch" in report  # flagged, own column


def test_report_empty_ledger():
    manifest = lab.load_manifest(E0)
    report = lab.render_report(manifest, [])
    assert "No trials yet" in report


# ----------------------------------------------------------------------------
# E5a: within-arm paraphrase metrics, persona injection, delta predictions
# ----------------------------------------------------------------------------

def test_anchor_accuracy_skips_null_truth():
    # Null-truth (debatable) questions are not scored; only anchors count.
    truth = {"Q1": "yes", "Q2": "no", "Q3": None, "Q4": None}
    answers = {"Q1": "yes", "Q2": "no", "Q3": "yes", "Q4": "no"}
    assert lab.anchor_accuracy(answers, truth) == pytest.approx(1.0)
    answers_bad = {"Q1": "no", "Q2": "no", "Q3": "yes", "Q4": "no"}
    assert lab.anchor_accuracy(answers_bad, truth) == pytest.approx(0.5)
    assert lab.anchor_accuracy(answers, {"Q3": None}) == 0.0  # no anchors


def test_paraphrase_consistency():
    pairs = [{"orig": "Q1", "para": "Q7"}, {"orig": "Q2", "para": "Q8"},
             {"orig": "Q3", "para": "Q9"}]
    # Q1/Q7 agree, Q2/Q8 agree, Q3/Q9 disagree -> 2/3.
    answers = {"Q1": "yes", "Q7": "yes", "Q2": "no", "Q8": "no",
               "Q3": "yes", "Q9": "no"}
    assert lab.paraphrase_consistency(answers, pairs) == pytest.approx(2 / 3)
    assert lab.paraphrase_consistency(answers, []) is None


def test_score_prediction_delta_and_alias_ops():
    aggregates = {("paraphrase_consistency", "delta"): 0.25,
                  ("kappa_floor", "overall"): 0.55}
    big = lab.score_prediction(
        {"claim": "B beats A by >=0.20", "confidence": 0.45,
         "check": {"metric": "paraphrase_consistency", "op": "delta_gte", "value": 0.20}},
        aggregates,
    )
    assert big["outcome"] == "held"
    assert "+0.250" in big["detail"]

    floor = lab.score_prediction(
        {"claim": "both arms >= floor", "confidence": 0.5,
         "check": {"metric": "kappa_floor", "op": "gte", "value": 0.6}},
        aggregates,
    )
    assert floor["outcome"] == "falsified"  # 0.55 < 0.6, gte alias resolves

    positive = lab.score_prediction(
        {"claim": "any positive", "confidence": 0.6,
         "check": {"metric": "paraphrase_consistency", "op": "delta_gte", "value": 0.0}},
        aggregates,
    )
    assert positive["outcome"] == "held"


def test_e5a_manifest_and_persona_only_line_differs():
    manifest = lab.load_manifest(E5A)
    assert lab.protocol_placeholders(manifest) == []
    assert manifest.cls == "posthoc"
    assert manifest.roles["judge"]["rig"] == "local"
    assert manifest.metrics == ["paraphrase_consistency",
                                "within_arm_consistency", "anchor_accuracy"]
    # Only the persona line moves between arms: everything after it is identical.
    qa = lab.load_questions(manifest, "A")
    qb = lab.load_questions(manifest, "B")
    pa = lab.build_judge_prompt(manifest, "A", qa)
    pb = lab.build_judge_prompt(manifest, "B", qb)
    assert "Grace Hopper" in pb and "Grace Hopper" not in pa
    assert pa.split("\n", 1)[1] == pb.split("\n", 1)[1]  # rubric body identical


# ----------------------------------------------------------------------------
# Non-ollama judge rigs (posthoc on any preset)
# ----------------------------------------------------------------------------

def _cloud_config(tmp_path, invoke, name="cloud", preset="opencode") -> Path:
    path = tmp_path / "rigs.json"
    path.write_text(json.dumps({name: {"preset": preset, "invoke": invoke}}),
                    encoding="utf-8")
    return path


def test_non_ollama_rig_resolves_environment(tmp_path, monkeypatch):
    config = _cloud_config(
        tmp_path,
        ["opencode", "run", "-m", "opencode/nemotron-3-ultra-free", "--auto", "{prompt}"],
    )
    monkeypatch.setattr(lab.shutil, "which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(lab, "_binary_version", lambda b: ("opencode 1.17.0", None))
    manifest = lab.load_manifest(E0)  # pin qwen3.6
    info = lab.resolve_role(manifest, "judge", "cloud", config)
    assert info["preset"] == "opencode"
    assert info["resolved_model"] == "opencode/nemotron-3-ultra-free"
    assert info["model_digest"] is None  # digest is an ollama-only concept
    assert info["harness_version"] == "opencode 1.17.0"
    assert info["pin_mismatch"] is True  # outside the qwen3.6 series
    assert "harness_version_note" not in info


def test_non_ollama_rig_without_version_notes_it(tmp_path, monkeypatch):
    config = _cloud_config(tmp_path, ["mycli", "--model", "some/model", "{prompt}"],
                           preset="codex")
    monkeypatch.setattr(lab.shutil, "which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(lab, "_binary_version",
                        lambda b: (None, f"{b} has no usable --version"))
    manifest = lab.load_manifest(E0)
    info = lab.resolve_role(manifest, "judge", "cloud", config)
    assert info["harness_version"] is None
    assert info["harness_version_note"] == "mycli has no usable --version"


def test_non_ollama_rig_without_model_refuses(tmp_path, monkeypatch):
    config = _cloud_config(tmp_path, ["opencode", "run", "--auto", "{prompt}"])
    monkeypatch.setattr(lab.shutil, "which", lambda b: f"/usr/bin/{b}")
    manifest = lab.load_manifest(E0)
    with pytest.raises(lab.LabError, match="declares no model"):
        lab.resolve_role(manifest, "judge", "cloud", config)


def test_null_digest_models_never_pool():
    manifest = lab.load_manifest(E0)
    a_ans = {"Q1": "yes", "Q2": "no", "Q3": "yes", "Q4": "no"}
    rows = [
        _fake_row("A", None, "opencode/nemotron-3-ultra-free", a_ans, 1.0, mismatch=True),
        _fake_row("A", None, "opencode/other-model", a_ans, 0.5, mismatch=True),
        _fake_row("A", "digest1", "qwen3.6:latest", a_ans, 1.0),
    ]
    by_model = lab.aggregate(manifest, rows)
    # Two null-digest models land in separate columns, never a shared one.
    assert len(by_model) == 3
    resolved = {m["resolved"] for m in by_model.values()}
    assert resolved == {"opencode/nemotron-3-ultra-free", "opencode/other-model",
                        "qwen3.6:latest"}
    report = lab.render_report(manifest, rows)
    assert "opencode/nemotron-3-ultra-free (no digest)" in report
    assert "pin_mismatch" in report


def test_e5a_fake_run_reports_paraphrase_and_kappa(tmp_path, monkeypatch):
    monkeypatch.setenv("R4T_HOME", str(tmp_path / "home"))
    rc = lab.run_experiment(E5A, arm=None, n=3, fake=True, log=lambda m: None)
    assert rc == 0
    rows = lab.read_ledger(E5A)
    assert len(rows) == 6
    assert all("paraphrase_consistency" in r["metrics"] for r in rows)
    assert all("anchor_accuracy" in r["metrics"] for r in rows)

    manifest = lab.load_manifest(E5A)
    report = lab.render_report(manifest, rows)
    assert "paraphrase_consistency" in report
    assert "paraphrase κ (chance-corrected)" in report
    assert "delta(B-A)" in report
    # The delta and floor predictions are machine-scored (held/falsified).
    assert "kappa_floor" in report
