import os

import pytest


@pytest.fixture(autouse=True)
def _seat_state_in_tmp(tmp_path, monkeypatch):
    """A seat lock is a file under the seat's home. No test may touch the real one."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))


@pytest.fixture(autouse=True)
def _no_real_chrome(request, monkeypatch):
    """Only a live test may start a real Chrome (AR3_TOOL_BROWSER_LIVE=1, the switch
    test_stop_live.py skips on). Any other test that reaches the Chrome launch fails
    and names itself, so a stub that misses a call site cannot leave a browser running.
    A test that stubs `spawn` still runs the real `_launch_chrome` to check its argv."""
    if os.environ.get("AR3_TOOL_BROWSER_LIVE") == "1":
        yield
        return
    from browser import session

    launch, spawn, quiet = session._launch_chrome, session.spawn, session._spawn_quiet
    reached = []

    def refuse(seat):
        if session.spawn is not spawn or session._spawn_quiet is not quiet:
            return launch(seat)
        reached.append(
            f"{request.node.nodeid} reached the real Chrome spawn "
            "(browser.session._launch_chrome). Stub browser.session._start_chrome, "
            "or stub browser.session.spawn to check the launch argv."
        )
        pytest.fail(reached[-1])

    monkeypatch.setattr(session, "_launch_chrome", refuse)
    yield
    # A thread or a handler may swallow the failure above; the test still fails here.
    assert not reached, reached[0]
