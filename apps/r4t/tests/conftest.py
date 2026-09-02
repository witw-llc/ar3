"""pytest scaffolding for r4t."""
from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PKG))
# `ar3` sits in `<repo>/lib`, shared by every app. Put it on the path here
# rather than relying on some r4t module having run first.
sys.path.append(str(_PKG.parent.parent / "lib"))


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
def _no_var_cache_leak():
    """A runbook reads a node's a8s vars once per process. A process is one
    wake in production and the whole suite under pytest, so the cache is
    emptied between tests."""
    import runbook

    runbook.clear_vars_cache()
    yield
    runbook.clear_vars_cache()


@pytest.fixture
def zone(monkeypatch):
    """Force the zone every rendered stamp reads in.

    Not via `TZ`. `time.tzset()` does not exist on Windows and the C library
    there never consults `TZ`, so a fixture built that way raises
    AttributeError before a single assertion runs.

    `ar3.clock`'s two conversion points are redirected instead. `to_local` is
    wrapped rather than replaced, so the stored-stamp parsing stays the real
    one and only the final conversion moves. `dispatch` is patched as well
    because it does `from ar3.clock import local_now`, and that copies the
    binding — rebinding the name in `ar3.clock` alone would leave `dispatch`
    pointing at the original. `stamp` and `zone_label` need no such treatment:
    they resolve `local_now` from their own module's globals when called.

    What this does not prove is that the display clock reads the *machine's*
    zone. That is `ar3.clock`'s own contract, tested in `test_ar3_clock.py`
    where `TZ` is the subject rather than the setup.
    """
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    import dispatch
    from ar3 import clock

    real_to_local = clock.to_local

    def use(name: str) -> None:
        tz = ZoneInfo(name)

        def local_now() -> datetime:
            return datetime.now(timezone.utc).astimezone(tz)

        def to_local(ts):
            dt = real_to_local(ts)
            return None if dt is None else dt.astimezone(tz)

        monkeypatch.setattr(clock, "local_now", local_now)
        monkeypatch.setattr(clock, "to_local", to_local)
        monkeypatch.setattr(dispatch, "local_now", local_now)

    return use


@pytest.fixture(autouse=True)
def _no_ollama(monkeypatch):
    """Member stores drive k7e as a subprocess, which reaches for ollama when
    one answers. Point every test at a dead port so the suite measures r4t and
    not whichever models the developer happens to be running."""
    monkeypatch.setenv("OLLAMA_URL", "http://localhost:99999")


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
    - **Ingress:** yes

    Grumpy, cynical veteran. Despises feature creep.

    ### Broken
    - **Rig:** ./run-agent.sh --headless
    """
)


@pytest.fixture
def r4t_home(tmp_path, monkeypatch):
    home = tmp_path / "r4t-home"
    monkeypatch.setenv("R4T_HOME", str(home))
    monkeypatch.delenv("TELL_OUTBOX_DIR", raising=False)
    # `roster check` asks a8s which names are visible from this host. Point it
    # at an empty state root: a test's result must not depend on what the
    # developer happens to have registered.
    a8s_home = tmp_path / "a8s-home"
    a8s_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("A8S_HOME", str(a8s_home))
    return home


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "ROSTER.md").write_text(ROSTER_TEXT, encoding="utf-8")
    return root


def write_path_executable(directory: Path, name: str, source: str) -> Path:
    """Put a Python stub on PATH under `name`, for product code that resolves a
    binary with `shutil.which` and execs it by name.

    POSIX gets the stub itself, shebang and exec bit. Windows can do neither: it
    cannot exec a shebang file, and `shutil.which` there matches only PATHEXT
    extensions, so a bare `claude` is not even found. The stub lands as
    `<name>.py` beside a `<name>.cmd` launcher, which is what PATHEXT resolves
    and what Windows can run. Returns the path product code will find.
    """
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        (directory / f"{name}.py").write_text(source, encoding="utf-8")
        launcher = directory / f"{name}.cmd"
        launcher.write_text(
            "@echo off\r\n"
            f'"{sys.executable}" "%~dp0{name}.py" %*\r\n'
            "exit /b %ERRORLEVEL%\r\n",
            encoding="utf-8",
        )
        return launcher
    path = directory / name
    path.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


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
            with open(os.path.join(calls_dir, f"call-{{n:03d}}.txt"), "w", encoding="utf-8", newline="") as f:
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
            with open(os.path.join(calls_dir, f"call-{{n:03d}}.txt"), "w", encoding="utf-8", newline="") as f:
                f.write(sys.argv[1])
            outbox = os.environ["TELL_OUTBOX_DIR"]
            os.makedirs(outbox, exist_ok=True)
            recipients = os.environ.get("CHATTY_TO", "gerry").split(",")
            body = os.environ.get("CHATTY_BODY", "reply number {{i}}")
            sends = int(os.environ.get("CHATTY_SENDS", "1"))
            for i in range(sends):
                to = recipients[i % len(recipients)]
                msg_id = f"{{time.time_ns():026d}}"
                with open(os.path.join(outbox, msg_id + ".json"), "w", encoding="utf-8", newline="") as f:
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
