"""Pytest adapter: collect the whole application before assigning modules."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.test_shards import SUITES, digest, partition, require

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from ar3.proc import launch_options


def pytest_addoption(parser):
    parser.addoption("--suite-shard", choices=("1", "2"))
    parser.addoption("--shard-suite", choices=SUITES)
    parser.addoption("--shard-receipt")


def check_options(config, root, suite):
    require(suite in SUITES, "shard suite is required")
    require(len(config.args) == 1 and Path(config.args[0]).resolve() == root / "apps" / suite / "tests", "shards require exactly the whole application test directory")
    for name in ("keyword", "markexpr", "lf", "failedfirst", "deselect", "ignore", "ignore_glob", "pyargs", "stepwise", "stepwise_skip", "maxfail"):
        require(not config.getoption(name, default=None), f"sharding refuses collection/execution filter: {name}")
    require(not config.getini("addopts") and not os.environ.get("PYTEST_ADDOPTS"), "sharding refuses implicit pytest arguments")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(session, config, items):
    root = Path(__file__).resolve().parents[1]
    suite = config.getoption("shard_suite")
    try:
        check_options(config, root, suite)
        require(config.getoption("suite_shard") in ("1", "2") and config.getoption("shard_receipt"), "shard index and receipt are required")
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True, **launch_options()).strip()
        require(sha == os.environ["GITHUB_SHA"], "checkout differs from run SHA")
        # Root-relative canonical IDs regardless of pytest's inferred rootdir.
        full = sorted(str(Path(item.path).resolve().relative_to(root)) + "::" + item.nodeid.split("::", 1)[1] for item in items)
        estimates = json.loads((root / "tools/shard_weights.json").read_text())
        selected = partition(full, suite, estimates["weights"][suite], estimates["default_seconds_per_case"][suite])[int(config.getoption("suite_shard")) - 1]
        config._shard_receipt = {"schema": 1, "sha": sha, "run_id": int(os.environ["GITHUB_RUN_ID"]),
            "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]), "suite": suite,
            "index": int(config.getoption("suite_shard")), "total": 2,
            "mode": "collect-only" if config.option.collectonly else "executed", "full": full, "selected": selected}
        config._shard_executed = set()
        wanted = set(selected)
        keep, removed = [], []
        for item in items:
            canonical = str(Path(item.path).resolve().relative_to(root)) + "::" + item.nodeid.split("::", 1)[1]
            item._shard_canonical = canonical
            (keep if canonical in wanted else removed).append(item)
        items[:] = keep
        config.hook.pytest_deselected(items=removed)
    except (ValueError, KeyError) as error:
        raise pytest.UsageError(str(error)) from error


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    item.config._shard_executed.add(item._shard_canonical)


def pytest_sessionfinish(session, exitstatus):
    config = session.config
    if hasattr(config, "_shard_receipt"):
        receipt = config._shard_receipt
        receipt["executed"] = sorted(config._shard_executed)
        for field in ("full", "selected", "executed"):
            receipt[field + "_digest"] = digest(receipt[field])
        Path(config.getoption("shard_receipt")).write_text(json.dumps(receipt, indent=2) + "\n")
