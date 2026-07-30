"""pytest scaffolding for r4t."""
from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PKG))


def pytest_configure(config):
    # Registered so a future in-pytest hook for the real-boundary docker test
    # has a home. The current integration lives entirely in tests/docker/ and
    # is driven by run-as.sh (Docker is the only entry point), so nothing under
    # the default suite carries this marker today.
    config.addinivalue_line(
        "markers",
        "isolation_integration: real OS-boundary test; run via "
        "tests/docker/run-as.sh, excluded from the default suite",
    )


@pytest.fixture(autouse=True)
def _restore_tell_outbox_env():
    """r4t's seat/chat CLIs set TELL_OUTBOX_DIR in os.environ (so child `tell`
    processes inherit it). Running a CLI in-process would otherwise leak that
    into later tests — including the a8s `tell` suite when both run together.
    Snapshot and restore around every test so nothing escapes the suite."""
    prior = os.environ.get("TELL_OUTBOX_DIR")
    yield
    if prior is None:
        os.environ.pop("TELL_OUTBOX_DIR", None)
    else:
        os.environ["TELL_OUTBOX_DIR"] = prior


ROSTER_TEXT = textwrap.dedent(
    """\
    # Roster

    Preamble prose that is not a member block.

    ### Gerry
    - **Rig:** leader
    - **Role:** Technical Producer
    - **Cell:** leadership
    - **Leader:** yes

    The Orchestrator. Defends the schedule.

    ### Phil
    - **Rig:** junior-dev
    - **Role:** Lead Backend Engineer

    Grumpy, cynical veteran. Despises feature creep.

    ### Neil
    - **Human:** yes
    - **Address:** neil
    - **Role:** Game Director

    ### Broken
    - **Rig:** ./run-agent.sh --headless
    """
)


@pytest.fixture
def r4t_home(tmp_path, monkeypatch):
    home = tmp_path / "r4t-home"
    monkeypatch.setenv("R4T_HOME", str(home))
    monkeypatch.delenv("TELL_OUTBOX_DIR", raising=False)
    return home


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "ROSTER.md").write_text(ROSTER_TEXT, encoding="utf-8")
    return root


@pytest.fixture
def fake_harness(tmp_path):
    """A tiny harness that records its prompt and echoes — no LLM calls."""
    script = tmp_path / "fake-harness.py"
    out = tmp_path / "harness-calls"
    out.mkdir()
    script.write_text(
        textwrap.dedent(
            f"""\
            import os, sys
            calls_dir = {str(out)!r}
            n = len(os.listdir(calls_dir))
            with open(os.path.join(calls_dir, f"call-{{n:03d}}.txt"), "w") as f:
                f.write(sys.argv[1])
            print("fake harness ran")  # short: stays under the stdout-reply threshold
            """
        ),
        encoding="utf-8",
    )
    return script, out


def base_config(script) -> dict:
    return {
        "_comment": "test config — throttle gates off so unit turns run back to back",
        "throttle": {"max_concurrent": 0, "min_seconds_between_turn_starts": 0},
        "cell_budget_max": 200,
        "cell_budget_earn_per_hour": 100,
        "leader": {
            "invoke": [sys.executable, str(script), "{prompt}"],
            "timeout_seconds": 30,
            "concurrency": 2,
            "budget_max": 100,
            "budget_earn_per_hour": 50,
        },
        "junior-dev": {
            "invoke": [sys.executable, str(script), "{prompt}"],
            "timeout_seconds": 30,
            "concurrency": 1,
            "budget_max": 100,
            "budget_earn_per_hour": 50,
        },
        "pins": {"_comment": "x", "gerry": "leader"},
    }


@pytest.fixture
def rig_config(tmp_path, fake_harness):
    script, _out = fake_harness
    path = tmp_path / "rigs.json"
    path.write_text(json.dumps(base_config(script), indent=2), encoding="utf-8")
    return path


@pytest.fixture
def chatty_harness(tmp_path):
    """A harness that records its prompt, then drops tell-shaped envelopes
    into $TELL_OUTBOX_DIR (the per-turn staging dir) exactly like the real
    `tell` does. Recipients via CHATTY_TO (comma-separated), message via
    CHATTY_BODY, count via CHATTY_SENDS."""
    script = tmp_path / "chatty-harness.py"
    out = tmp_path / "chatty-calls"
    out.mkdir()
    script.write_text(
        textwrap.dedent(
            f"""\
            import json, os, sys, time
            calls_dir = {str(out)!r}
            n = len(os.listdir(calls_dir))
            with open(os.path.join(calls_dir, f"call-{{n:03d}}.txt"), "w") as f:
                f.write(sys.argv[1])
            outbox = os.environ["TELL_OUTBOX_DIR"]
            os.makedirs(outbox, exist_ok=True)
            recipients = os.environ.get("CHATTY_TO", "gerry").split(",")
            body = os.environ.get("CHATTY_BODY", "reply number {{i}}")
            sends = int(os.environ.get("CHATTY_SENDS", "1"))
            for i in range(sends):
                to = recipients[i % len(recipients)]
                msg_id = f"{{time.time_ns():026d}}"
                with open(os.path.join(outbox, msg_id + ".json"), "w") as f:
                    json.dump(
                        {{"id": msg_id, "to": to, "content": body.format(i=i), "files": []}},
                        f,
                    )
            """
        ),
        encoding="utf-8",
    )
    return script, out


@pytest.fixture
def chatty_config(tmp_path, chatty_harness):
    script, _out = chatty_harness
    config = base_config(script)
    config["junior-dev"]["max_sends_per_turn"] = 2
    path = tmp_path / "chatty-rigs.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def tells():
    sent: list[tuple[str, str]] = []

    def capture(agent: str, body: str) -> None:
        sent.append((agent, body))

    return sent, capture


@pytest.fixture
def ctx(r4t_home, repo, rig_config, tells):
    from dispatch import DispatchContext

    _sent, capture = tells
    return DispatchContext(
        root=repo,
        node="acme",
        roster_path=repo / "ROSTER.md",
        config_path=rig_config,
        tell_fn=capture,
    )


@pytest.fixture
def chatty_ctx(r4t_home, repo, chatty_config, tells):
    from dispatch import DispatchContext

    _sent, capture = tells
    return DispatchContext(
        root=repo,
        node="acme",
        roster_path=repo / "ROSTER.md",
        config_path=chatty_config,
        tell_fn=capture,
    )
