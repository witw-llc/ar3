"""`ar3.proc.process_start_token` on Windows, with the platform forced and
kernel32 mocked. Native Windows behavior is proven by the a8s suite's
real-process tests (`test_a_start_token_names_one_process`)."""
from __future__ import annotations

import ctypes
from types import SimpleNamespace

import pytest

import ar3.proc as proc

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x102


class _FakeDll:
    def __init__(self, **entry_points):
        for name, fn in entry_points.items():
            setattr(self, name, fn)


def _kernel32(monkeypatch, *, handle=0x55, wait=WAIT_TIMEOUT, times=1, created=(9, 7)):
    calls = []

    def open_process(access, inherit, pid):
        calls.append(("OpenProcess", access, inherit, pid))
        return handle

    def get_times(h, created_ref, exited, kernel, user):
        calls.append(("GetProcessTimes", h))
        created_ref._obj.dwHighDateTime, created_ref._obj.dwLowDateTime = created
        return times

    dll = _FakeDll(
        OpenProcess=open_process,
        WaitForSingleObject=lambda h, ms: calls.append(("Wait", h, ms)) or wait,
        GetProcessTimes=get_times,
        CloseHandle=lambda h: calls.append(("CloseHandle", h)) or 1,
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kw: dll, raising=False)
    return calls


def _force_windows(monkeypatch):
    monkeypatch.setattr(proc, "os", SimpleNamespace(name="nt"))


def test_windows_reads_the_creation_time_of_a_live_process(monkeypatch):
    calls = _kernel32(monkeypatch)
    _force_windows(monkeypatch)
    assert proc.process_start_token(42) == "0000000900000007"
    assert calls == [
        ("OpenProcess", PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, 42),
        ("Wait", 0x55, 0),
        ("GetProcessTimes", 0x55),
        ("CloseHandle", 0x55),
    ]


def test_windows_two_starts_of_one_pid_read_differently(monkeypatch):
    _force_windows(monkeypatch)
    _kernel32(monkeypatch, created=(9, 7))
    first = proc.process_start_token(42)
    _kernel32(monkeypatch, created=(9, 8))
    assert proc.process_start_token(42) != first


def test_windows_an_exited_process_held_open_reads_none(monkeypatch):
    calls = _kernel32(monkeypatch, wait=0)
    _force_windows(monkeypatch)
    assert proc.process_start_token(42) is None
    assert ("GetProcessTimes", 0x55) not in calls
    assert calls[-1] == ("CloseHandle", 0x55)


def test_windows_a_process_that_cannot_be_opened_reads_none(monkeypatch):
    calls = _kernel32(monkeypatch, handle=0)
    _force_windows(monkeypatch)
    assert proc.process_start_token(42) is None
    assert [c[0] for c in calls] == ["OpenProcess"]


def test_windows_a_failed_read_reads_none_and_closes_the_handle(monkeypatch):
    calls = _kernel32(monkeypatch, times=0)
    _force_windows(monkeypatch)
    assert proc.process_start_token(42) is None
    assert calls[-1] == ("CloseHandle", 0x55)


@pytest.mark.parametrize("error", [OSError, AttributeError])
def test_windows_with_no_kernel32_reads_none(monkeypatch, error):
    def missing(name, **kw):
        raise error("no kernel32")

    monkeypatch.setattr(ctypes, "WinDLL", missing, raising=False)
    _force_windows(monkeypatch)
    assert proc.process_start_token(42) is None
