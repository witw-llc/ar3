"""`r4t lab` — repo-bundled, repeatable experiments on the sandbox chassis.

An experiment is a question packaged under `apps/r4t/experiments/<name>/`:
a machine-readable `experiment.json` manifest, a filled-in `PROTOCOL.md`, and
frozen fixtures. `lab run` executes trials (one hermetic execution of one arm),
appends each to an append-only ledger under `~/.config/r4t/lab/<name>/`, and
`lab report` aggregates the ledger — grouped per arm x *resolved* model, never
pooling across model resolutions — with a bootstrap CI, a paired sign test, a
verdict line, and per-prediction scoring.

PR 1 ships only the `posthoc` class: judge a frozen fixture set with one rig
invocation per trial (no live org). The `org` class is accepted by the loader
but `lab run` refuses it until a follow-up PR.
"""
from __future__ import annotations

import hashlib
import json
import math
import platform
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import ulid
from rig import RigError, default_config_path, load_rig_config
from state import r4t_home, utc_now

R4T_DIR = Path(__file__).resolve().parent
EXPERIMENTS_DIR = R4T_DIR / "experiments"
FAKE_JUDGE = R4T_DIR / "fake-judge.py"

VALID_CLASSES = frozenset({"posthoc", "org"})
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 4242
KAPPA_FLOOR = 0.6
PLACEHOLDER_RE = re.compile(r"<[A-Za-z][^<>]*>")


class LabError(Exception):
    pass


# ----------------------------------------------------------------------------
# Manifest
# ----------------------------------------------------------------------------

@dataclass
class Manifest:
    name: str
    question: str
    cls: str
    arms: dict
    roles: dict
    trials_per_arm: int
    stopping_rule: object
    box_seconds: int
    metrics: list
    predictions: list
    posthoc: dict
    directory: Path
    protocol_path: Path

    def arm_names(self) -> list[str]:
        return list(self.arms.keys())


def experiment_names() -> list[str]:
    if not EXPERIMENTS_DIR.is_dir():
        return []
    return sorted(
        p.name
        for p in EXPERIMENTS_DIR.iterdir()
        if p.is_dir() and (p / "experiment.json").is_file()
    )


def load_manifest(name: str) -> Manifest:
    directory = EXPERIMENTS_DIR / name
    path = directory / "experiment.json"
    if not path.is_file():
        known = ", ".join(experiment_names()) or "(none)"
        raise LabError(
            f"no experiment {name!r} in {EXPERIMENTS_DIR} (known: {known})"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise LabError(f"cannot load manifest {path}: {e}") from e
    if not isinstance(raw, dict):
        raise LabError(f"manifest {path} must be a JSON object")

    cls = str(raw.get("class", "")).strip().lower()
    if cls not in VALID_CLASSES:
        allowed = ", ".join(sorted(VALID_CLASSES))
        raise LabError(
            f"manifest {name}: unknown class {raw.get('class')!r} (allowed: {allowed})"
        )
    arms = raw.get("arms")
    if not isinstance(arms, dict) or not arms:
        raise LabError(f"manifest {name}: 'arms' must be a non-empty object")
    metrics = raw.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        raise LabError(f"manifest {name}: 'metrics' must be a non-empty list")
    unknown = [m for m in metrics if m not in ALL_METRICS]
    if unknown:
        known = ", ".join(sorted(ALL_METRICS))
        raise LabError(
            f"manifest {name}: unknown metric(s) {', '.join(map(str, unknown))} "
            f"(known: {known})"
        )
    try:
        trials_per_arm = int(raw.get("trials_per_arm", 1))
    except (TypeError, ValueError):
        raise LabError(f"manifest {name}: 'trials_per_arm' must be an integer")
    if trials_per_arm < 1:
        raise LabError(f"manifest {name}: 'trials_per_arm' must be >= 1")

    return Manifest(
        name=name,
        question=str(raw.get("question", "")),
        cls=cls,
        arms=arms,
        roles=raw.get("roles", {}) or {},
        trials_per_arm=trials_per_arm,
        stopping_rule=raw.get("stopping_rule"),
        box_seconds=int(raw.get("box_seconds", 1800)),
        metrics=list(metrics),
        predictions=list(raw.get("predictions", []) or []),
        posthoc=raw.get("posthoc", {}) or {},
        directory=directory,
        protocol_path=directory / "PROTOCOL.md",
    )


def protocol_placeholders(manifest: Manifest) -> list[str]:
    """Unreplaced `<...>` placeholders in the experiment's PROTOCOL.md.
    Pre-registration is not optional: `lab run` refuses while any remain."""
    if not manifest.protocol_path.is_file():
        return ["<PROTOCOL.md missing>"]
    text = manifest.protocol_path.read_text(encoding="utf-8")
    return PLACEHOLDER_RE.findall(text)


# ----------------------------------------------------------------------------
# Ledger + report storage (machine state under ~/.config/r4t/lab/<name>/)
# ----------------------------------------------------------------------------

def lab_dir(name: str) -> Path:
    return r4t_home() / "lab" / name


def ledger_path(name: str) -> Path:
    return lab_dir(name) / "trials.jsonl"


def reports_dir(name: str) -> Path:
    return lab_dir(name) / "reports"


def read_ledger(name: str) -> list[dict]:
    path = ledger_path(name)
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _append_ledger(name: str, row: dict) -> None:
    path = ledger_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


# ----------------------------------------------------------------------------
# Environment capture (spec 4.2)
# ----------------------------------------------------------------------------

def _r4t_git_describe() -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(R4T_DIR), "describe", "--tags", "--always", "--dirty"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return proc.stdout.strip() or "unknown"


def _ollama_version() -> str:
    try:
        proc = subprocess.run(
            ["ollama", "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise LabError(f"could not run `ollama --version`: {e}") from e
    text = (proc.stdout or proc.stderr).strip()
    return text.splitlines()[0] if text else "ollama"


def _ollama_models() -> list[tuple[str, str]]:
    """(name, digest) pairs from `ollama list`."""
    try:
        proc = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise LabError(f"could not run `ollama list`: {e}") from e
    pairs: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            pairs.append((parts[0], parts[1]))
    return pairs


def resolve_ollama_model(model: str) -> tuple[str, str]:
    """Resolve a bare ollama model string (`qwen3.6`) to the (name, digest)
    `ollama run` would serve today. Exact match wins; otherwise the
    `<model>:latest` tag, then any tag of the same repo. Missing -> LabError."""
    model = model.strip()
    models = _ollama_models()
    by_name = {name: digest for name, digest in models}
    if model in by_name:
        return model, by_name[model]
    latest = f"{model}:latest"
    if latest in by_name:
        return latest, by_name[latest]
    for name, digest in models:
        if name.split(":", 1)[0] == model.split(":", 1)[0]:
            return name, digest
    have = ", ".join(name for name, _ in models) or "(none)"
    raise LabError(f"ollama model {model!r} not found (have: {have})")


def _series(model: str) -> str:
    """The repo/series token of a model name (`qwen3.6:latest` -> `qwen3.6`)."""
    return model.split(":", 1)[0].strip().lower()


def resolve_bindings(manifest: Manifest, overrides: dict | None) -> dict:
    """role -> rig name: the manifest default, overridden by `--rig role=rig`.
    An override for an unknown role is a hard error (a typo would silently run
    the default otherwise)."""
    overrides = overrides or {}
    bindings = {
        role: str((cfg or {}).get("rig", "")).strip()
        for role, cfg in manifest.roles.items()
    }
    for role, rig in overrides.items():
        if role not in bindings:
            known = ", ".join(bindings) or "(none)"
            raise LabError(f"--rig {role}={rig}: unknown role (roles: {known})")
        bindings[role] = rig.strip()
    return bindings


def _rig_config_path(rig_config: str | None) -> Path:
    if rig_config:
        return Path(rig_config).expanduser().resolve()
    return default_config_path()


def _rig_preset(config_path: Path, rig_name: str) -> str | None:
    """The recorded preset for a rig, read from the raw config payload."""
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entry = raw.get(rig_name.lower())
    if isinstance(entry, dict):
        preset = entry.get("preset")
        if isinstance(preset, str) and preset.strip():
            return preset.strip().lower()
    return None


def _ollama_model_of(invoke: list) -> str | None:
    """The model token from an ollama-preset invoke (`ollama run MODEL ...`)."""
    argv = invoke[0] if invoke and isinstance(invoke[0], list) else invoke
    try:
        return argv[argv.index("run") + 1]
    except (ValueError, IndexError):
        return None


def _rig_model_of(rig) -> str | None:
    """The explicit model string a non-ollama rig declares: the rig's `model`
    field first, else the concrete value after `--model`/`-m` in its invoke.
    None when the rig names no model — the trial cannot record what it cannot
    resolve."""
    if rig.model and "{model}" not in str(rig.model):
        return str(rig.model).strip()
    argv = rig.pool()[0] if rig.pool() else rig.invoke
    for flag in ("--model", "-m"):
        try:
            candidate = argv[argv.index(flag) + 1]
        except (ValueError, IndexError):
            continue
        if candidate and candidate != "{model}":
            return candidate
    return None


def _binary_version(binary: str) -> tuple[str | None, str | None]:
    """(first `--version` line, note). Version is None with a note when the
    binary has no usable --version — recorded, never invented."""
    try:
        proc = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None, f"{binary} has no usable --version"
    text = (proc.stdout or proc.stderr).strip()
    if proc.returncode != 0 or not text:
        return None, f"{binary} has no usable --version"
    return text.splitlines()[0], None


def resolve_role(
    manifest: Manifest, role: str, rig_name: str, config_path: Path
) -> dict:
    """Bind one role to its rig and resolve the model. Raises LabError (with a
    `(try:)` hint) when the rig is missing/invalid or its harness cannot run.
    A rig that resolves OUTSIDE the manifest pin's series still resolves — the
    caller flags `pin_mismatch` and never pools it (spec 5).

    Ollama rigs resolve to a (name, digest) pair — the frozen tier. Any other
    preset resolves generically: the rig's explicit model string, no digest
    (an ollama-only concept), and the preset binary's --version."""
    pin = str((manifest.roles.get(role) or {}).get("pin", "")).strip()
    if not rig_name:
        raise LabError(f"role {role!r} has no rig binding (set roles.{role}.rig)")
    config = load_rig_config(config_path)
    if config.missing:
        raise LabError(
            f"no rig config at {config_path} "
            f"(try: r4t rig add {rig_name} <preset> --model {pin or 'MODEL'})"
        )
    rig = config.rigs.get(rig_name.lower())
    if rig is None:
        raise LabError(
            f"rig {rig_name!r} not in {config_path} "
            f"(try: r4t rig add {rig_name} <preset> --model {pin or 'MODEL'})"
        )
    if rig.error:
        raise LabError(f"rig {rig_name!r} is invalid: {rig.error}")
    preset = _rig_preset(config_path, rig_name)
    invoke = list(rig.pool()[0]) if rig.pool() else list(rig.invoke)

    if preset == "ollama":
        if not shutil.which("ollama"):
            raise LabError("ollama not on PATH (try: install ollama)")
        model = _ollama_model_of(rig.invoke)
        if not model:
            raise LabError(f"rig {rig_name!r} has no ollama model in its invoke")
        try:
            resolved, digest = resolve_ollama_model(model)
        except LabError as e:
            raise LabError(f"{e} (try: ollama pull {model})") from e
        return {
            "role": role,
            "rig": rig_name,
            "preset": preset,
            "pin": pin,
            "resolved_model": resolved,
            "model_digest": digest,
            "pin_mismatch": bool(pin) and _series(resolved) != _series(pin),
            "invoke": invoke,
            "harness_version": _ollama_version(),
        }

    binary = invoke[0] if invoke else None
    if not binary:
        raise LabError(f"rig {rig_name!r} has an empty invoke")
    if not shutil.which(binary):
        raise LabError(f"{binary} not on PATH (try: install {binary})")
    resolved = _rig_model_of(rig)
    if not resolved:
        raise LabError(
            f"rig {rig_name!r} ({preset}) declares no model — the trial cannot "
            f"record what it cannot resolve "
            f"(try: r4t rig swap {rig_name} {preset or '<preset>'} --model MODEL)"
        )
    version, version_note = _binary_version(binary)
    info = {
        "role": role,
        "rig": rig_name,
        "preset": preset,
        "pin": pin,
        "resolved_model": resolved,
        "model_digest": None,
        "pin_mismatch": bool(pin) and _series(resolved) != _series(pin),
        "invoke": invoke,
        "harness_version": version,
    }
    if version_note:
        info["harness_version_note"] = version_note
    return info


def _fake_role(manifest: Manifest, role: str, rig_name: str) -> dict:
    pin = str((manifest.roles.get(role) or {}).get("pin", "")).strip()
    return {
        "role": role,
        "rig": rig_name or "fake",
        "preset": "fake-judge",
        "pin": pin,
        "resolved_model": "fake-judge",
        "model_digest": "fake",
        "pin_mismatch": False,
        "invoke": [sys.executable, str(FAKE_JUDGE), "{prompt}"],
        "harness_version": "fake",
    }


def capture_environment(
    manifest: Manifest, bindings: dict, config_path: Path, *, fake: bool
) -> dict:
    roles: dict = {}
    for role, rig_name in bindings.items():
        roles[role] = (
            _fake_role(manifest, role, rig_name)
            if fake
            else resolve_role(manifest, role, rig_name, config_path)
        )
    judge = roles.get("judge", {})
    return {
        "r4t_git": _r4t_git_describe(),
        "os": platform.platform(),
        "harness_version": judge.get("harness_version"),
        "model_pin": judge.get("pin"),
        "resolved_model": judge.get("resolved_model"),
        "model_digest": judge.get("model_digest"),
        "pin_mismatch": judge.get("pin_mismatch", False),
        "roles": roles,
    }


def probe_prereqs(
    manifest: Manifest, bindings: dict, config_path: Path, *, fake: bool
) -> tuple[bool, str]:
    """Probe-only: r4t never fixes the operator's setup (isolation principle).
    Returns (ok, message-with-try-hint-on-failure)."""
    if fake:
        return True, "fake judge (no prereqs)"
    try:
        roles = {
            role: resolve_role(manifest, role, rig_name, config_path)
            for role, rig_name in bindings.items()
        }
    except LabError as e:
        return False, str(e)
    parts = []
    for role, info in roles.items():
        flag = " [pin_mismatch]" if info["pin_mismatch"] else ""
        digest = info["model_digest"] or "no digest"
        parts.append(f"{role}={info['rig']} -> {info['resolved_model']} "
                     f"({digest}){flag}")
    return True, "; ".join(parts)


# ----------------------------------------------------------------------------
# Posthoc trial: judge frozen fixtures, one rig invocation per trial
# ----------------------------------------------------------------------------

def _read_fixture(manifest: Manifest, rel: str) -> str:
    path = manifest.directory / rel
    if not path.is_file():
        raise LabError(f"manifest {manifest.name}: fixture {rel} not found at {path}")
    return path.read_text(encoding="utf-8")


def load_questions(manifest: Manifest, arm: str) -> list[dict]:
    spec = manifest.posthoc.get("questions", {}).get(arm)
    if not spec:
        raise LabError(f"manifest {manifest.name}: no questions for arm {arm!r}")
    data = json.loads(_read_fixture(manifest, spec))
    if not isinstance(data, list) or not data:
        raise LabError(f"manifest {manifest.name}: questions for arm {arm!r} must be a non-empty list")
    return data


def load_answers(manifest: Manifest) -> dict:
    spec = manifest.posthoc.get("answers")
    if not spec:
        raise LabError(f"manifest {manifest.name}: no answers fixture declared")
    return json.loads(_read_fixture(manifest, spec))


def load_pairs(manifest: Manifest) -> list[dict]:
    """Within-arm paraphrase pairing: each entry is {"orig", "para", "anchor"}.
    Empty when the experiment declares no `posthoc.pairs` (e.g. E0, which pairs
    across arms instead)."""
    spec = manifest.posthoc.get("pairs")
    if not spec:
        return []
    data = json.loads(_read_fixture(manifest, spec))
    if not isinstance(data, list) or not data:
        raise LabError(f"manifest {manifest.name}: pairs must be a non-empty list")
    for p in data:
        if "orig" not in p or "para" not in p:
            raise LabError(f"manifest {manifest.name}: each pair needs 'orig' and 'para'")
    return data


def build_judge_prompt(manifest: Manifest, arm: str, questions: list[dict]) -> str:
    template = _read_fixture(manifest, manifest.posthoc["judge_prompt"])
    transcript = _read_fixture(manifest, manifest.posthoc["transcript"])
    rendered = "\n".join(f"{q['id']}: {q['text']}" for q in questions)
    persona_spec = manifest.posthoc.get("personas", {}).get(arm)
    persona = _read_fixture(manifest, persona_spec).strip() if persona_spec else ""
    return (
        template
        .replace("{persona}", persona)
        .replace("{transcript}", transcript)
        .replace("{questions}", rendered)
    )


_ANSWER_RE = re.compile(r"(?im)^\s*(Q\d+)\s*[:\.\)]\s*(yes|no)\b")


def parse_answers(output: str, expected_ids: list[str]) -> dict | None:
    """Extract {qid: yes|no} for every expected id. Returns None (parse error)
    if any expected question is missing or unparseable — never a partial dict."""
    found: dict[str, str] = {}
    for qid, verdict in _ANSWER_RE.findall(output):
        qid = qid.upper()
        if qid not in found:
            found[qid] = verdict.lower()
    if all(qid in found for qid in expected_ids):
        return {qid: found[qid] for qid in expected_ids}
    return None


def _run_judge(
    invoke: list[str], prompt: str, timeout: float, cwd: Path
) -> tuple[int, str, str]:
    argv = [prompt if a == "{prompt}" else a for a in invoke]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, cwd=str(cwd)
        )
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    return proc.returncode, proc.stdout, proc.stderr


def _write_trial_report(
    name: str, trial_id: str, arm: str, environment: dict, prompt: str,
    raw_output: str, answers: dict | None, exit_reason: str,
) -> Path:
    rdir = reports_dir(name)
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / f"{trial_id}.raw.txt").write_text(raw_output, encoding="utf-8")
    lines = [
        f"# r4t lab trial {trial_id}",
        "",
        f"- experiment: {name}",
        f"- arm: {arm}",
        f"- stamp: {utc_now()}",
        f"- resolved model: {environment.get('resolved_model')} ({environment.get('model_digest')})",
        f"- harness: {environment.get('harness_version')}",
        f"- exit reason: {exit_reason}",
        "",
        "## Parsed answers",
        "",
    ]
    if answers:
        lines += [f"- {qid}: {val}" for qid, val in answers.items()]
    else:
        lines.append("(unparseable — trial excluded)")
    lines += ["", "## Judge prompt", "", "```", prompt.strip(), "```", "",
              "## Raw judge output", "", "```", raw_output.strip(), "```", ""]
    report_path = rdir / f"{trial_id}.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def run_one_posthoc_trial(
    manifest: Manifest, arm: str, environment: dict, *, log=None,
) -> dict:
    questions = load_questions(manifest, arm)
    expected_ids = [q["id"] for q in questions]
    answers_truth = load_answers(manifest)
    prompt = build_judge_prompt(manifest, arm, questions)

    trial_id = ulid.new()
    invoke = environment["roles"]["judge"]["invoke"]
    # Hermetic per-trial cwd: tool-enabled judge harnesses (observed live with
    # `opencode --auto`) can write scratch files into their working directory —
    # that must never be the operator's repo. The scratch dir dies with the
    # trial.
    workdir = Path(tempfile.mkdtemp(prefix=f"r4t-lab-{manifest.name}-"))
    start = time.time()
    try:
        code, out, err = _run_judge(
            invoke, prompt, float(manifest.box_seconds), workdir
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    wall = time.time() - start
    raw_output = out if out.strip() else err

    excluded = False
    excluded_reason = None
    metrics: dict = {}
    parsed = None
    if err == "timeout" and code == 124:
        exit_reason = "timeout"
        excluded, excluded_reason = True, "timeout"
    elif code != 0:
        exit_reason = "nonzero_exit"
        excluded, excluded_reason = True, "nonzero_exit"
    else:
        parsed = parse_answers(raw_output, expected_ids)
        if parsed is None:
            exit_reason = "parse_error"
            excluded, excluded_reason = True, "parse_error"
        else:
            exit_reason = "ok"
            truth = answers_truth.get(arm, {})
            if "accuracy" in manifest.metrics:
                metrics["accuracy"] = accuracy(parsed, truth)
            if "anchor_accuracy" in manifest.metrics:
                metrics["anchor_accuracy"] = anchor_accuracy(parsed, truth)
            if "paraphrase_consistency" in manifest.metrics:
                pc = paraphrase_consistency(parsed, load_pairs(manifest))
                if pc is not None:
                    metrics["paraphrase_consistency"] = pc

    report_path = _write_trial_report(
        manifest.name, trial_id, arm, environment, prompt, raw_output, parsed, exit_reason,
    )
    row = {
        "trial": trial_id,
        "stamp": utc_now(),
        "experiment": manifest.name,
        "arm": arm,
        "environment": environment,
        "seed": None,
        "metrics": metrics,
        "answers": parsed,
        "exit_reason": exit_reason,
        "wall_clock_seconds": round(wall, 3),
        "report_path": str(report_path),
        "raw_sha256": {"judge_output": hashlib.sha256(raw_output.encode("utf-8")).hexdigest()},
        "excluded": excluded,
        "excluded_reason": excluded_reason,
    }
    _append_ledger(manifest.name, row)
    if log:
        status = exit_reason if not excluded else f"EXCLUDED ({excluded_reason})"
        summary = "  ".join(f"{m}={v:.2f}" for m, v in metrics.items())
        summary = f" {summary}" if summary else ""
        log(f"trial {trial_id} arm {arm}: {status}{summary} in {wall:.1f}s")
    return row


def run_experiment(
    name: str, *, arm: str | None, n: int | None, fake: bool,
    rig_overrides: dict | None = None, rig_config: str | None = None, log=None,
) -> int:
    if log is None:
        def log(msg: str) -> None:
            print(f"lab: {msg}", file=sys.stderr, flush=True)

    manifest = load_manifest(name)

    if manifest.cls == "org":
        log("org-class experiments land in a follow-up PR")
        return 2

    placeholders = protocol_placeholders(manifest)
    if placeholders:
        log(
            f"refusing to run {name}: PROTOCOL.md still has "
            f"{len(placeholders)} unreplaced placeholder(s): "
            f"{', '.join(placeholders[:5])} "
            "(pre-registration is not optional — fill every <...>)"
        )
        return 2

    config_path = _rig_config_path(rig_config)
    bindings = resolve_bindings(manifest, rig_overrides)

    ok, message = probe_prereqs(manifest, bindings, config_path, fake=fake)
    if not ok:
        log(f"prereq check failed: {message}")
        return 2
    log(message)

    if arm is not None and arm not in manifest.arms:
        log(f"unknown arm {arm!r} (arms: {', '.join(manifest.arm_names())})")
        return 2

    per_arm = n if n is not None else manifest.trials_per_arm
    if per_arm < 1:
        log("trial count must be >= 1")
        return 2

    arms = [arm] if arm is not None else manifest.arm_names()
    environment = capture_environment(manifest, bindings, config_path, fake=fake)
    mismatch = " [pin_mismatch]" if environment.get("pin_mismatch") else ""
    digest = environment.get("model_digest") or "no digest"
    log(f"environment: {environment['resolved_model']} ({digest})"
        f"{mismatch}, harness {environment['harness_version']}, r4t {environment['r4t_git']}")

    # Alternate arms so time-drift does not confound one arm (spec 4).
    schedule = [a for _ in range(per_arm) for a in arms]
    excluded = 0
    for i, a in enumerate(schedule, 1):
        log(f"[{i}/{len(schedule)}] running arm {a}")
        row = run_one_posthoc_trial(manifest, a, environment, log=log)
        if row["excluded"]:
            excluded += 1

    kept = len(schedule) - excluded
    log(f"done: {len(schedule)} trial(s), {kept} kept, {excluded} excluded")
    log(f"ledger: {ledger_path(name)}")
    log(f"report: r4t lab report {name}")
    return 0


# ----------------------------------------------------------------------------
# Metrics
# ----------------------------------------------------------------------------

def accuracy(answers: dict, truth: dict) -> float:
    if not truth:
        return 0.0
    correct = sum(1 for qid, val in truth.items() if answers.get(qid) == val)
    return correct / len(truth)


def anchor_accuracy(answers: dict, truth: dict) -> float:
    """Accuracy over only the ground-truth-bearing questions. Debatable
    questions carry a null truth (no defensible right answer) and are skipped —
    scoring them against a made-up key would punish legitimate judgment."""
    scored = {qid: val for qid, val in truth.items() if val is not None}
    if not scored:
        return 0.0
    correct = sum(1 for qid, val in scored.items() if answers.get(qid) == val)
    return correct / len(scored)


def paraphrase_consistency(answers: dict, pairs: list[dict]) -> float | None:
    """Fraction of paraphrase pairs the judge answered the SAME way within one
    trial (original twin == paraphrase twin). None when no pairs are declared.
    This is the primary metric: a rubric that is robust to wording answers a
    question and its reworded twin identically."""
    if not pairs:
        return None
    agree = sum(
        1 for p in pairs
        if answers.get(p["orig"]) is not None
        and answers.get(p["orig"]) == answers.get(p["para"])
    )
    return agree / len(pairs)


def _modal(values: list[str]) -> str:
    counts: dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    # Deterministic tie-break: highest count, then lexical.
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def within_arm_consistency(trials: list[dict]) -> float | None:
    """Per-question modal-answer agreement rate across the arm's trials,
    averaged over questions. None when fewer than 2 kept trials."""
    answers = [t["answers"] for t in trials if t.get("answers")]
    if len(answers) < 2:
        return None
    qids = list(answers[0].keys())
    rates = []
    for qid in qids:
        vals = [a[qid] for a in answers if qid in a]
        if not vals:
            continue
        mode = _modal(vals)
        rates.append(sum(1 for v in vals if v == mode) / len(vals))
    return sum(rates) / len(rates) if rates else None


def arm_modal_answers(trials: list[dict]) -> dict:
    answers = [t["answers"] for t in trials if t.get("answers")]
    if not answers:
        return {}
    qids = list(answers[0].keys())
    return {qid: _modal([a[qid] for a in answers if qid in a]) for qid in qids}


def cross_arm_agreement(
    modal_a: dict, modal_b: dict, pairing: list[tuple[str, str]]
) -> float | None:
    """Fraction of paraphrase pairs where arm A's modal answer equals arm B's.
    None when either arm has no modal answers."""
    if not modal_a or not modal_b or not pairing:
        return None
    agree = sum(1 for qa, qb in pairing if modal_a.get(qa) == modal_b.get(qb))
    return agree / len(pairing)


# Registry: which metric names the loader accepts, and how they are surfaced.
# Per-trial metrics are computed from a single judge output and flow through the
# sign test / bootstrap; aggregate metrics are computed across an arm's trials.
PER_TRIAL_METRICS = ("accuracy", "anchor_accuracy", "paraphrase_consistency")
AGGREGATE_METRICS = ("within_arm_consistency", "cross_arm_agreement")
ALL_METRICS = frozenset(PER_TRIAL_METRICS) | frozenset(AGGREGATE_METRICS)


# ----------------------------------------------------------------------------
# Statistics (stdlib only)
# ----------------------------------------------------------------------------

def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def bootstrap_diff_ci(
    a: list[float], b: list[float], *, resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED, conf: float = 0.95,
) -> tuple[float, float, float]:
    """Difference in means (mean(a) - mean(b)) with a percentile bootstrap CI.
    Deterministic for a fixed seed so `lab report` is reproducible."""
    point = mean(a) - mean(b)
    if not a or not b:
        return point, point, point
    rng = random.Random(seed)
    na, nb = len(a), len(b)
    diffs = []
    for _ in range(resamples):
        sa = sum(a[rng.randrange(na)] for _ in range(na)) / na
        sb = sum(b[rng.randrange(nb)] for _ in range(nb)) / nb
        diffs.append(sa - sb)
    diffs.sort()
    tail = (1 - conf) / 2
    lo = diffs[int(tail * (resamples - 1))]
    hi = diffs[int((1 - tail) * (resamples - 1))]
    return point, lo, hi


def sign_test(a: list[float], b: list[float]) -> dict:
    """Paired sign test: pair by index, count wins/losses (A vs B), ties
    dropped, two-sided binomial p via math.comb at p=0.5."""
    wins = losses = ties = 0
    for x, y in zip(a, b):
        if x > y:
            wins += 1
        elif x < y:
            losses += 1
        else:
            ties += 1
    n = wins + losses
    if n == 0:
        p = 1.0
    else:
        k = min(wins, losses)
        tail = sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n)
        p = min(1.0, 2 * tail)
    return {"wins": wins, "losses": losses, "ties": ties, "n": n, "p": p}


def cohen_kappa(rater1: list[str], rater2: list[str]) -> float | None:
    """Cohen's kappa for two raters over aligned categorical labels. None when
    empty. 1.0 when chance agreement is total (single category on both sides)."""
    if not rater1 or len(rater1) != len(rater2):
        return None
    n = len(rater1)
    po = sum(1 for x, y in zip(rater1, rater2) if x == y) / n
    cats = set(rater1) | set(rater2)
    pe = sum((rater1.count(c) / n) * (rater2.count(c) / n) for c in cats)
    if pe >= 1.0:
        return 1.0
    return (po - pe) / (1 - pe)


def sign_verdict(test: dict) -> str:
    """Direction consistency from a sign test. `consistent (N=x/y same
    direction)` when every non-tie comparison agrees; else mixed."""
    n = test["n"]
    if n == 0:
        return "no separation (all ties)"
    if test["wins"] == n or test["losses"] == n:
        winner = test["wins"] if test["wins"] == n else test["losses"]
        return f"consistent (N={winner}/{n} same direction)"
    return "mixed — needs more trials"


# Prediction ops: `delta_*` compare the between-arm difference (B - A) stored
# under the (metric, "delta") aggregate key; the word-form aliases exist so a
# manifest can read as prose (`"op": "gte"`). Both reduce to `_compare`.
_DELTA_OPS = {"delta_gte": ">=", "delta_gt": ">", "delta_lte": "<=", "delta_lt": "<"}
_OP_ALIASES = {"gte": ">=", "gt": ">", "lte": "<=", "lt": "<"}


def score_prediction(prediction: dict, aggregates: dict) -> dict:
    """Score one pre-registered prediction against computed aggregates:
    held / falsified / undecided, plus this report's Brier term."""
    check = prediction.get("check")
    claim = prediction.get("claim", "")
    confidence = prediction.get("confidence")
    result = {"claim": claim, "confidence": confidence, "outcome": "undecided",
              "detail": "", "brier": None}
    if not isinstance(check, dict):
        result["detail"] = "no machine-checkable `check` — scored by hand"
        return result
    metric = check.get("metric")
    op = check.get("op", ">=")
    value = check.get("value")
    scope = check.get("scope", "overall")
    if op in _DELTA_OPS:
        observed = aggregates.get((metric, "delta"))
        base_op, scope_label = _DELTA_OPS[op], "delta"
    else:
        base_op = _OP_ALIASES.get(op, op)
        key = (metric, scope) if scope == "each_arm" else (metric, "overall")
        observed = aggregates.get(key)
        scope_label = scope
    if observed is None:
        result["detail"] = f"{metric} ({scope_label}) not yet computable"
        return result
    held = _compare(observed, base_op, value)
    result["outcome"] = "held" if held else "falsified"
    obs_s = f"{observed:+.3f}" if scope_label == "delta" else f"{observed:.3f}"
    result["detail"] = f"{metric} ({scope_label}) = {obs_s} {op} {value}"
    if isinstance(confidence, (int, float)):
        result["brier"] = (float(confidence) - (1.0 if held else 0.0)) ** 2
    return result


def _compare(observed: float, op: str, value: float) -> bool:
    if op == ">=":
        return observed >= value
    if op == ">":
        return observed > value
    if op == "<=":
        return observed <= value
    if op == "<":
        return observed < value
    if op == "==":
        return observed == value
    raise LabError(f"unknown prediction op {op!r}")


# ----------------------------------------------------------------------------
# Aggregation + report (spec 5)
# ----------------------------------------------------------------------------

def _pairing(manifest: Manifest) -> list[tuple[str, str]]:
    """Paraphrase pairing: arm A's i-th question <-> arm B's i-th question."""
    arms = manifest.arm_names()
    if len(arms) < 2:
        return []
    qa = [q["id"] for q in load_questions(manifest, arms[0])]
    qb = [q["id"] for q in load_questions(manifest, arms[1])]
    return list(zip(qa, qb))


def aggregate(manifest: Manifest, rows: list[dict]) -> dict:
    """Group by resolved model (never pooled across resolutions), then by arm.
    Returns {digest: {"resolved": str, "arms": {arm: {...}}, "metrics": {...},
    "aggregates": {(metric, scope): value}}}."""
    by_model: dict[str, dict] = {}
    for row in rows:
        env = row.get("environment", {})
        digest = env.get("model_digest")
        resolved = env.get("resolved_model", "?")
        # Digest is the frozen-tier identity (ollama); rigs without one group
        # by resolved model string — distinct models never pool under a shared
        # null digest.
        key = digest if digest else f"model:{resolved}"
        model = by_model.setdefault(
            key, {"resolved": resolved, "digest": digest, "rows": []}
        )
        model["rows"].append(row)

    pairing = _pairing(manifest) if len(manifest.arm_names()) >= 2 else []
    pair_list = load_pairs(manifest)
    arms = manifest.arm_names()

    for model in by_model.values():
        kept = [r for r in model["rows"] if not r.get("excluded")]
        excluded = [r for r in model["rows"] if r.get("excluded")]
        model["excluded"] = excluded
        per_arm: dict[str, dict] = {}
        for a in arms:
            arm_rows = [r for r in kept if r.get("arm") == a]
            entry = {
                "n": len(arm_rows),
                "within_arm_consistency": within_arm_consistency(arm_rows),
                "modal": arm_modal_answers(arm_rows),
                "rows": arm_rows,
            }
            for m in PER_TRIAL_METRICS:
                entry[m] = [r["metrics"].get(m) for r in arm_rows
                            if r.get("metrics", {}).get(m) is not None]
            per_arm[a] = entry
        model["arms"] = per_arm

        aggregates: dict = {}
        wac_values = [per_arm[a]["within_arm_consistency"] for a in arms
                      if per_arm[a]["within_arm_consistency"] is not None]
        if wac_values:
            aggregates[("within_arm_consistency", "overall")] = mean(wac_values)
            aggregates[("within_arm_consistency", "each_arm")] = min(wac_values)

        # Per-trial aggregate metrics: overall (mean of arm means), each_arm
        # (worst arm), and the between-arm delta (B - A) that `delta_*`
        # predictions score against.
        for m in ("paraphrase_consistency", "anchor_accuracy"):
            if m not in manifest.metrics:
                continue
            arm_means = {a: mean(per_arm[a][m]) for a in arms if per_arm[a][m]}
            if len(arm_means) == len(arms):
                aggregates[(m, "overall")] = mean(list(arm_means.values()))
                aggregates[(m, "each_arm")] = min(arm_means.values())
                if len(arms) >= 2:
                    aggregates[(m, "delta")] = arm_means[arms[1]] - arm_means[arms[0]]

        # kappa_floor: chance-corrected paraphrase agreement per arm (the two
        # question halves as two raters), worst arm reported against the floor.
        if pair_list and "paraphrase_consistency" in manifest.metrics:
            arm_kappa: dict = {}
            for a in arms:
                modal = per_arm[a]["modal"]
                r1 = [modal.get(p["orig"]) for p in pair_list]
                r2 = [modal.get(p["para"]) for p in pair_list]
                if all(x is not None for x in r1 + r2):
                    arm_kappa[a] = cohen_kappa(r1, r2)
            model["arm_kappa"] = arm_kappa
            valid = [k for k in arm_kappa.values() if k is not None]
            if len(valid) == len(arms):
                aggregates[("kappa_floor", "overall")] = min(valid)

        if pairing and len(arms) >= 2 and "cross_arm_agreement" in manifest.metrics:
            caa = cross_arm_agreement(
                per_arm[arms[0]]["modal"], per_arm[arms[1]]["modal"], pairing
            )
            if caa is not None:
                aggregates[("cross_arm_agreement", "overall")] = caa
                model["cross_arm"] = {
                    "value": caa,
                    "kappa": cohen_kappa(
                        [per_arm[arms[0]]["modal"].get(qa) for qa, _ in pairing],
                        [per_arm[arms[1]]["modal"].get(qb) for _, qb in pairing],
                    ),
                }
        model["aggregates"] = aggregates
    return by_model


def render_report(manifest: Manifest, rows: list[dict]) -> str:
    marks = {True: "✓", False: "✗", None: "•"}
    lines: list[str] = []
    lines.append(f"experiment: {manifest.name}")
    lines.append(f"question: {manifest.question}")
    lines.append(f"ledger: {ledger_path(manifest.name)}")
    lines.append("")
    if not rows:
        lines.append("No trials yet.  (try: r4t lab run "
                     f"{manifest.name})")
        return "\n".join(lines) + "\n"

    by_model = aggregate(manifest, rows)
    arms = manifest.arm_names()

    for model in by_model.values():
        mismatch = any(
            r.get("environment", {}).get("pin_mismatch") for r in model["rows"]
        )
        flag = "  [pin_mismatch — outside intended series]" if mismatch else ""
        digest = model["digest"] or "no digest"
        lines.append(f"Model  {model['resolved']} ({digest}){flag}")
        per_arm = model["arms"]
        pertrial = [m for m in manifest.metrics if m in PER_TRIAL_METRICS]
        for a in arms:
            info = per_arm[a]
            cells = [f"{info['n']} kept"]
            for m in pertrial:
                vals = info[m]
                cells.append(f"{m} {mean(vals):.3f} (n={len(vals)})" if vals else f"{m} n/a")
            lines.append(f"  arm {a}: " + " · ".join(cells))
        if model["excluded"]:
            reasons: dict[str, int] = {}
            for r in model["excluded"]:
                reasons[r.get("excluded_reason", "?")] = reasons.get(r.get("excluded_reason", "?"), 0) + 1
            lines.append("  excluded: " + ", ".join(f"{k}×{v}" for k, v in reasons.items()))
        lines.append("")

        # Per per-trial metric: A vs B effect size + bootstrap CI + sign test.
        if len(arms) >= 2:
            for m in pertrial:
                a_vals = per_arm[arms[0]][m]
                b_vals = per_arm[arms[1]][m]
                if not (a_vals and b_vals):
                    continue
                point, lo, hi = bootstrap_diff_ci(a_vals, b_vals)
                test = sign_test(a_vals, b_vals)
                lines.append(f"  Pattern — {m} ({arms[0]} vs {arms[1]})")
                lines.append(f"    effect size: {point:+.3f}  95% CI [{lo:+.3f}, {hi:+.3f}] (bootstrap 10k)")
                lines.append(f"    sign test: {test['wins']}W-{test['losses']}L-{test['ties']}T "
                             f"(n={test['n']}, two-sided p={test['p']:.3f})")
                lines.append(f"    verdict: {sign_verdict(test)}")
                lines.append("")

        # Aggregate metrics readout.
        agg = model["aggregates"]
        if ("within_arm_consistency", "overall") in agg:
            per = "  ".join(
                f"{a}={per_arm[a]['within_arm_consistency']:.3f}"
                for a in arms if per_arm[a]["within_arm_consistency"] is not None
            )
            lines.append(f"  within_arm_consistency: {agg[('within_arm_consistency','overall')]:.3f} "
                         f"(per arm: {per})")
        if ("paraphrase_consistency", "delta") in agg:
            per = "  ".join(
                f"{a}={mean(per_arm[a]['paraphrase_consistency']):.3f}"
                for a in arms if per_arm[a]["paraphrase_consistency"]
            )
            lines.append(
                f"  paraphrase_consistency: overall {agg[('paraphrase_consistency','overall')]:.3f}  "
                f"delta(B-A) {agg[('paraphrase_consistency','delta')]:+.3f}  (per arm: {per})"
            )
        if model.get("arm_kappa"):
            ks = "  ".join(
                f"{a} κ={('%.3f' % k) if k is not None else 'n/a'}"
                for a, k in model["arm_kappa"].items()
            )
            floor = agg.get(("kappa_floor", "overall"))
            floor_s = (f"  min κ={floor:.3f} "
                       f"({'≥' if floor >= KAPPA_FLOOR else '<'} {KAPPA_FLOOR} floor)"
                       if floor is not None else "")
            lines.append(f"  paraphrase κ (chance-corrected): {ks}{floor_s}")
        if "cross_arm" in model:
            caa = model["cross_arm"]
            k = caa["kappa"]
            k_s = f"κ={k:.3f} ({'≥' if (k is not None and k >= KAPPA_FLOOR) else '<'} {KAPPA_FLOOR} floor)" if k is not None else "κ=n/a"
            lines.append(f"  cross_arm_agreement: {caa['value']:.3f}  {k_s}")
        lines.append("")

        # Predictions scored against this model's aggregates.
        lines.append("  Predictions")
        if not manifest.predictions:
            lines.append("    (none pre-registered)")
        brier_terms = []
        for pred in manifest.predictions:
            scored = score_prediction(pred, agg)
            mark = {"held": True, "falsified": False, "undecided": None}[scored["outcome"]]
            conf = scored["confidence"]
            conf_s = f" @conf {conf}" if conf is not None else ""
            line = f"    {marks[mark]} {scored['outcome']}{conf_s}: {scored['claim']}"
            lines.append(line)
            if scored["detail"]:
                lines.append(f"        {scored['detail']}")
            if scored["brier"] is not None:
                brier_terms.append(scored["brier"])
        if brier_terms:
            lines.append(f"    Brier (this report): {mean(brier_terms):.4f} "
                         f"over {len(brier_terms)} scored prediction(s)")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ----------------------------------------------------------------------------
# CLI entry points (called from r4t.py)
# ----------------------------------------------------------------------------

def cmd_list() -> int:
    names = experiment_names()
    if not names:
        print(f"No experiments under {EXPERIMENTS_DIR}")
        return 0
    print(f"Experiments  ({EXPERIMENTS_DIR})")
    config_path = default_config_path()
    for name in names:
        try:
            manifest = load_manifest(name)
        except LabError as e:
            print(f"  ✗ {name}  (invalid: {e})")
            continue
        bindings = resolve_bindings(manifest, None)
        ok, message = probe_prereqs(manifest, bindings, config_path, fake=False)
        mark = "✓" if ok else "✗"
        roles = "  ".join(
            f"{role}={(cfg or {}).get('rig', '?')}(pin {(cfg or {}).get('pin', '?')})"
            for role, cfg in manifest.roles.items()
        )
        placeholders = protocol_placeholders(manifest)
        proto = "" if not placeholders else f"  [PROTOCOL: {len(placeholders)} placeholder(s)]"
        print(f"  {mark} {name}  [{manifest.cls}]  {roles}{proto}")
        print(f"      {message}")
    return 0


def cmd_run(
    name: str, *, arm: str | None, n: int | None, fake: bool,
    rig_overrides: dict | None = None, rig_config: str | None = None,
) -> int:
    try:
        return run_experiment(
            name, arm=arm, n=n, fake=fake,
            rig_overrides=rig_overrides, rig_config=rig_config,
        )
    except (LabError, RigError) as e:
        print(f"lab run: {e}", file=sys.stderr)
        return 2


def cmd_report(name: str) -> int:
    try:
        manifest = load_manifest(name)
    except LabError as e:
        print(f"lab report: {e}", file=sys.stderr)
        return 2
    rows = read_ledger(name)
    sys.stdout.write(render_report(manifest, rows))
    return 0


def cmd_ledger(name: str, *, as_json: bool) -> int:
    try:
        load_manifest(name)
    except LabError as e:
        print(f"lab ledger: {e}", file=sys.stderr)
        return 2
    rows = read_ledger(name)
    if as_json:
        json.dump(rows, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if not rows:
        print(f"(no trials yet — try: r4t lab run {name})")
        return 0
    for row in rows:
        flag = "EXCLUDED" if row.get("excluded") else "ok"
        acc = row.get("metrics", {}).get("accuracy")
        acc_s = f" acc={acc:.2f}" if acc is not None else ""
        print(f"{row['stamp']}  {row['trial']}  arm {row['arm']}  "
              f"{row['exit_reason']} [{flag}]{acc_s}  "
              f"{row['environment'].get('resolved_model')}")
    return 0
