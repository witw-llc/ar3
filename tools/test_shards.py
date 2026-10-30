"""Deterministic whole-module partitions and data-only coverage receipts."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

SUITES = ("a8s", "r4t")
LIMIT = 4 * 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(nodes):
    return hashlib.sha256(json.dumps(nodes, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def nodes_valid(nodes, suite):
    require(isinstance(nodes, list) and 0 < len(nodes) <= 20000, "invalid node inventory size")
    require(all(isinstance(n, str) and len(n) <= 2048
                and re.fullmatch(r"apps/" + suite + r"/tests/(?:[A-Za-z0-9_-]+/)*test_[A-Za-z0-9_-]+\.py::[^\r\n]+", n)
                for n in nodes), "noncanonical node ID")
    require(nodes == sorted(set(nodes)), "unsorted or duplicate node IDs")


def partition(nodes, suite, weights, default_seconds_per_case=1.0):
    nodes_valid(nodes, suite)
    modules = {}
    for node in nodes:
        modules.setdefault(node.split("::", 1)[0], []).append(node)
    # Estimates affect allocation only, never membership. New modules always
    # participate, using a deterministic per-case fallback.
    costs = {module: weights.get(module, len(items) * default_seconds_per_case) for module, items in modules.items()}
    require(all(isinstance(v, (int, float)) and 0 < v < 100000 for v in costs.values()), "invalid weight")
    lanes, loads = [[], []], [0.0, 0.0]
    for module in sorted(modules, key=lambda m: (-costs[m], m)):
        lane = min(range(2), key=lambda i: (loads[i], i))
        lanes[lane].extend(modules[module])
        loads[lane] += costs[module]
    require(all(lanes), "both shards must be nonempty")
    return [sorted(lane) for lane in lanes]


def artifact_names(run_id, attempt):
    return {f"shard-{suite}-{index}-{run_id}-{attempt}" for suite in SUITES for index in (1, 2)}


def validate_artifacts(rows, run_id, attempt):
    suffix = f"-{run_id}-{attempt}"
    rows = [r for r in rows if r["name"].startswith("shard-") and r["name"].endswith(suffix)]
    names = [r["name"] for r in rows]
    require(len(names) == len(set(names)) and set(names) == artifact_names(run_id, attempt), "missing, duplicate or extra shard artifacts")
    require(all(not r.get("expired", True) and 0 < r.get("size_in_bytes", 0) <= LIMIT for r in rows), "expired or oversized shard artifact")


def load_receipts(directory, sha, run_id, attempt):
    directory = Path(directory)
    expected = artifact_names(run_id, attempt)
    require({p.name for p in directory.iterdir()} == expected, "unexpected downloaded artifact set")
    receipts = []
    for name in sorted(expected):
        folder = directory / name
        require(not folder.is_symlink() and folder.is_dir() and {p.name for p in folder.iterdir()} == {"shard.json"}, "unexpected shard artifact files")
        path = folder / "shard.json"
        require(not path.is_symlink() and path.stat().st_size <= LIMIT, "oversized or linked shard receipt")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                require(key not in result, "duplicate JSON key")
                result[key] = value
            return result
        receipt = json.loads(path.read_bytes(), object_pairs_hook=unique)
        require(name == f"shard-{receipt.get('suite')}-{receipt.get('index')}-{run_id}-{attempt}", "artifact identity mismatch")
        receipts.append(receipt)
    return validate_receipts(receipts, sha, run_id, attempt)


def validate_receipts(receipts, sha, run_id, attempt):
    require(bool(re.fullmatch(r"[0-9a-f]{40}", sha)), "invalid checkout SHA")
    require(type(run_id) is int and run_id > 0 and type(attempt) is int and attempt > 0, "invalid run identity")
    require(len(receipts) == 4, "requires exactly four shard receipts")
    by_suite = {}
    fields = {"schema", "sha", "run_id", "run_attempt", "suite", "index", "total", "mode", "full", "selected", "executed", "full_digest", "selected_digest", "executed_digest"}
    for receipt in receipts:
        require(isinstance(receipt, dict) and set(receipt) == fields, "invalid receipt schema")
        suite, index = receipt["suite"], receipt["index"]
        require(suite in SUITES and type(index) is int and index in (1, 2), "invalid shard identity")
        require(type(receipt["schema"]) is int and receipt["schema"] == 1 and type(receipt["total"]) is int and receipt["total"] == 2 and receipt["mode"] == "executed", "receipt did not execute a full shard")
        require(receipt["sha"] == sha and type(receipt["run_id"]) is int and receipt["run_id"] == run_id
                and type(receipt["run_attempt"]) is int and receipt["run_attempt"] == attempt, "receipt belongs to another checkout or attempt")
        require((suite, index) not in by_suite, "duplicate shard identity")
        for field in ("full", "selected", "executed"):
            nodes_valid(receipt[field], suite)
            require(receipt[field + "_digest"] == digest(receipt[field]), "invalid inventory digest")
        require(receipt["executed"] == receipt["selected"], "selected tests did not all execute")
        by_suite[suite, index] = receipt
    coverage = {}
    for suite in SUITES:
        require((suite, 1) in by_suite and (suite, 2) in by_suite, "missing shard identity")
        left, right = by_suite[suite, 1], by_suite[suite, 2]
        require(left["full"] == right["full"], "shards collected different full inventories")
        a, b = set(left["selected"]), set(right["selected"])
        require(not a & b and a | b == set(left["full"]), "shards must be disjoint and exhaustive")
        require(not {n.split("::", 1)[0] for n in a} & {n.split("::", 1)[0] for n in b}, "module split between shards")
        coverage[suite] = {"count": len(left["full"]), "full_digest": left["full_digest"], "complete": True}
    return coverage
