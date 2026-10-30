"""Coverage proofs reject omissions, overlapping shards and stale artifacts."""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("test_shards_impl", ROOT / "tools/test_shards.py")
shards = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shards)
SHA = "a" * 40


def inventory(suite):
    return sorted(f"apps/{suite}/tests/test_{module}.py::test_{n}" for module in ("new", "old", "hot") for n in range(3))


def receipts():
    result = []
    for suite in shards.SUITES:
        full = inventory(suite)
        for index, selected in enumerate(shards.partition(full, suite, {}), 1):
            receipt = dict(schema=1, sha=SHA, run_id=123, run_attempt=2, suite=suite,
                           index=index, total=2, mode="executed", full=full, selected=selected, executed=selected[:])
            for field in ("full", "selected", "executed"):
                receipt[field + "_digest"] = shards.digest(receipt[field])
            result.append(receipt)
    return result


def refresh(receipt):
    for field in ("full", "selected", "executed"):
        receipt[field + "_digest"] = shards.digest(receipt[field])


def test_partition_deterministic_whole_modules_and_includes_unknown_files():
    full = inventory("a8s")
    weights = {"apps/a8s/tests/test_hot.py": 100}
    first, second = shards.partition(full, "a8s", weights)
    assert [first, second] == shards.partition(full, "a8s", weights)
    assert set(first).isdisjoint(second) and sorted(first + second) == full
    assert {n.split("::")[0] for n in first}.isdisjoint(n.split("::")[0] for n in second)
    assert any("test_new.py" in n for n in first + second)


def test_valid_coverage():
    result = shards.validate_receipts(receipts(), SHA, 123, 2)
    assert all(v["complete"] and v["count"] == 9 for v in result.values())


@pytest.mark.parametrize("change", ["missing", "extra", "duplicate", "sha", "attempt", "suite", "index", "digest", "overlap", "omitted", "different-full", "duplicate-node", "invalid-node", "unexecuted", "collect-only", "split-module"])
def test_rejects_incomplete_or_untrusted_receipts(change):
    rows = receipts()
    row = rows[0]
    if change == "missing": rows.pop()
    elif change == "extra": rows.append(copy.deepcopy(row))
    elif change == "duplicate": rows[1] = copy.deepcopy(row)
    elif change == "sha": row["sha"] = "b" * 40
    elif change == "attempt": row["run_attempt"] = 1
    elif change == "suite": row["suite"] = "tools"
    elif change == "index": row["index"] = True
    elif change == "digest": row["full_digest"] = "0" * 64
    elif change == "overlap":
        rows[1]["selected"] = row["selected"][:]; rows[1]["executed"] = row["selected"][:]; refresh(rows[1])
    elif change == "omitted":
        row["selected"].pop(); row["executed"] = row["selected"][:]; refresh(row)
    elif change == "different-full": row["full"] = row["full"][:-1]; refresh(row)
    elif change == "duplicate-node": row["full"] = row["full"] + row["full"][:1]; refresh(row)
    elif change == "invalid-node": row["full"][0] = "../secret"; refresh(row)
    elif change == "unexecuted": row["executed"].pop(); refresh(row)
    elif change == "collect-only": row["mode"] = "collect-only"
    else:
        node = row["selected"].pop()
        rows[1]["selected"] = sorted(rows[1]["selected"] + [node])
        for r in rows[:2]: r["executed"] = r["selected"][:]; refresh(r)
    with pytest.raises(ValueError): shards.validate_receipts(rows, SHA, 123, 2)


@pytest.mark.parametrize("sha,run,attempt", [("bad", 123, 2), (SHA, True, 2), (SHA, 123, 0)])
def test_invalid_identity(sha, run, attempt):
    with pytest.raises(ValueError): shards.validate_receipts(receipts(), sha, run, attempt)


def test_empty_lane_refused():
    with pytest.raises(ValueError): shards.partition(["apps/a8s/tests/test_one.py::test_a"], "a8s", {})


@pytest.mark.parametrize("change", ["missing", "extra", "duplicate", "expired", "oversized"])
def test_api_artifact_set_strict(change):
    rows = [{"name": n, "expired": False, "size_in_bytes": 100} for n in shards.artifact_names(123, 2)]
    shards.validate_artifacts(rows, 123, 2)
    if change == "missing": rows.pop()
    elif change == "extra": rows.append(dict(name="shard-tools-1-123-2", expired=False, size_in_bytes=100))
    elif change == "duplicate": rows.append(rows[0])
    elif change == "expired": rows[0]["expired"] = True
    else: rows[0]["size_in_bytes"] = shards.LIMIT + 1
    with pytest.raises(ValueError): shards.validate_artifacts(rows, 123, 2)


def test_downloaded_json_is_strict_and_bounded(tmp_path):
    for row in receipts():
        folder = tmp_path / f"shard-{row['suite']}-{row['index']}-123-2"
        folder.mkdir(); (folder / "shard.json").write_text(json.dumps(row))
    assert shards.load_receipts(tmp_path, SHA, 123, 2)["a8s"]["complete"]
    path = next(tmp_path.iterdir()) / "shard.json"
    path.write_text('{"schema":1,"schema":1}')
    with pytest.raises(ValueError, match="duplicate JSON key"): shards.load_receipts(tmp_path, SHA, 123, 2)
    path.write_bytes(b" " * (shards.LIMIT + 1))
    with pytest.raises(ValueError, match="oversized"): shards.load_receipts(tmp_path, SHA, 123, 2)


def plugin():
    # Import as the workflow does, without adding a tools package dependency.
    import sys
    sys.path.insert(0, str(ROOT))
    try:
        from tools import shard_plugin
        return shard_plugin
    finally:
        sys.path.pop(0)


@pytest.mark.parametrize("name", ["keyword", "markexpr", "lf", "failedfirst", "deselect", "ignore", "ignore_glob", "pyargs", "stepwise", "stepwise_skip", "maxfail"])
def test_plugin_rejects_filters(name, monkeypatch):
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    config = SimpleNamespace(args=[str(ROOT / "apps/a8s/tests")],
        getoption=lambda key, default=None: key == name, getini=lambda key: [])
    with pytest.raises(ValueError, match="filter"):
        plugin().check_options(config, ROOT, "a8s")


@pytest.mark.parametrize("args", [["apps/a8s/tests/test_network.py"], ["apps/a8s/tests/", "apps/r4t/tests/"], ["apps/a8s/tests/test_network.py::test_one"]])
def test_plugin_rejects_narrow_or_extra_roots(args):
    config = SimpleNamespace(args=args)
    with pytest.raises(ValueError, match="whole application"):
        plugin().check_options(config, ROOT, "a8s")


def test_plugin_records_skipped_test_protocol_start():
    config = SimpleNamespace(_shard_executed=set())
    item = SimpleNamespace(config=config, _shard_canonical="canonical")
    plugin().pytest_runtest_protocol(item, None)
    assert config._shard_executed == {"canonical"}
