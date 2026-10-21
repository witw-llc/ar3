"""Fixtures for the gemini-web tool; the fake seats live in gw_support."""
import pytest
from gemini_web import gemini
from gw_support import FakeClock, FakeGeminiSeat, Outbox


@pytest.fixture
def outbox():
    return Outbox()


@pytest.fixture
def clock(monkeypatch):
    """Virtual time for every poll and settle in the driver."""
    fake = FakeClock()
    monkeypatch.setattr(gemini, "_now", fake.now)
    monkeypatch.setattr(gemini, "_sleep", fake.sleep)
    return fake


@pytest.fixture
def seat(tmp_path):
    return FakeGeminiSeat(tmp_path)


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    """A session store that lives and dies with the test."""
    root = tmp_path / "state"
    monkeypatch.setenv("XDG_DATA_HOME", str(root))
    return root
