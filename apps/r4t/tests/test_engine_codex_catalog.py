"""`r4t engine codex models|efforts` — read from a recording fake `codex app-server`."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from engines import values
from r4t import main as r4t_main

FAKE = r'''
import json, os, subprocess, sys, time
cfg = json.load(open(os.environ["FAKE_CODEX_CONFIG"], encoding="utf-8"))
log = open(cfg["log"], "a", encoding="utf-8")
def note(**kw):
    log.write(json.dumps(kw) + "\n"); log.flush()
note(argv=sys.argv[1:])
if cfg.get("pidfile"):
    open(cfg["pidfile"], "w", encoding="utf-8").write(str(os.getpid()))
if cfg.get("child"):
    child = subprocess.Popen([sys.executable, "-c",
        "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)"])
    open(cfg["child"], "w", encoding="utf-8").write(str(child.pid))
def out(obj):
    print(json.dumps(obj), flush=True)
mode = cfg.get("mode", "ok")
pages = cfg["pages"]
for line in sys.stdin:
    msg = json.loads(line)
    note(method=msg.get("method"), id=msg.get("id"), params=msg.get("params"))
    method = msg.get("method")
    if method == "initialize":
        if mode == "init-error":
            out({"id": msg["id"], "error": {"message": "no handshake"}}); continue
        if mode == "silent":
            time.sleep(60)
        if mode == "exit":
            sys.exit(3)
        if mode == "chatty":
            while True: print("noise", flush=True)
        if mode == "big-request":
            out({"method": "serverRequest", "id": "x" * 200000, "params": {}})
            out({"method": "serverRequest", "id": 1, "params": {}})
            out({"id": msg["id"], "result": {"userAgent": "fake"}})
            time.sleep(60)
        out({"method": "serverRequest", "id": 77, "params": {}})
        out({"id": msg["id"], "result": {"userAgent": "fake"}})
    elif method == "model/list":
        cursor = (msg.get("params") or {}).get("cursor")
        if mode == "deaf-pager":
            for k in range(msg["id"], msg["id"] + 60):
                out({"id": k, "result": {"data": [{"model": "m%d" % k, "supportedReasoningEfforts": []}],
                                         "nextCursor": ("%05d" % k) * 800}})
            time.sleep(60)
        if mode == "list-error":
            out({"id": msg["id"], "error": {"code": -1, "message": "catalog broke"}}); continue
        page = pages[int(cursor or 0) % len(pages)]
        out({"id": msg["id"], "method": "approval", "result": {"data": [{"model": "wrong"}]}})
        print("not json", flush=True)
        out({"id": msg["id"], "result": page["result"]})
'''


def entry(model, efforts, **extra):
    return {
        "id": model + "-id", "model": model, "displayName": model.upper(),
        "defaultReasoningEffort": efforts[0] if efforts else "x",
        "supportedReasoningEfforts": [{"reasoningEffort": e, "description": e} for e in efforts],
        "isDefault": False, "hidden": False, **extra,
    }


PAGES = [
    {"result": {"data": [entry("gpt-a", ["low", "high"]), entry("hid", ["low"], hidden=True)], "nextCursor": "1"}},
    {"result": {"data": [entry("gpt-b", ["minimal", "low", "ultra"])], "nextCursor": None}},
]


@pytest.fixture
def codex(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "fake_codex.py"
    script.write_text(FAKE, encoding="utf-8")
    argv = [sys.executable, str(script), "app-server"]
    monkeypatch.setattr(values, "CODEX_APP_SERVER_ARGV", argv)
    log = tmp_path / "calls.jsonl"

    class Fake:
        def configure(self, pages=None, **kw):
            cfg = {"log": str(log), "pages": PAGES if pages is None else pages, **kw}
            path = tmp_path / "cfg.json"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            monkeypatch.setenv("FAKE_CODEX_CONFIG", str(path))

        def calls(self):
            return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

        def methods(self):
            return [c["method"] for c in self.calls() if c.get("method")]

    fake = Fake()
    fake.configure()
    return fake


def run(argv, capsys):
    code = r4t_main(["engine", "codex", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_models_follow_pagination_skip_hidden_and_start_no_thread(codex, capsys):
    code, out, err = run(["models"], capsys)
    assert code == 0
    assert out.split() == ["gpt-a", "gpt-b"]
    assert "does not prove" in err
    assert codex.methods() == ["initialize", "initialized", "model/list", "model/list"]
    lists = [c for c in codex.calls() if c.get("method") == "model/list"]
    assert lists[0]["params"]["includeHidden"] is False and "cursor" not in lists[0]["params"]
    assert lists[1]["params"]["cursor"] == "1"
    assert not any(m.startswith(("thread/", "turn/")) for m in codex.methods())
    assert codex.calls()[0]["argv"] == ["app-server"]


def test_models_json_shape(codex, capsys):
    code, out, _ = run(["models", "--json"], capsys)
    payload = json.loads(out)
    assert code == 0 and payload["values"] == ["gpt-a", "gpt-b"]
    assert "catalog" in payload["note"]


def test_efforts_default_is_the_union_with_a_pointer_to_model(codex, capsys):
    code, out, err = run(["efforts"], capsys)
    assert code == 0
    assert out.split() == ["low", "high", "minimal", "ultra"]
    assert "varies by model" in err and "--model" in err


def test_efforts_with_model_lists_exactly_that_models_efforts(codex, capsys):
    code, out, _ = run(["efforts", "--model", "gpt-b"], capsys)
    assert code == 0 and out.split() == ["minimal", "low", "ultra"]
    code, out, _ = run(["efforts", "--model", "gpt-a", "--json"], capsys)
    assert json.loads(out)["values"] == ["low", "high"]


def test_unknown_or_hidden_model_fails_naming_it(codex, capsys):
    code, out, err = run(["efforts", "--model", "nope"], capsys)
    assert code == 1 and out == ""
    assert "does not list model 'nope'" in err
    assert run(["efforts", "--model", "hid"], capsys)[0] == 1


def test_models_refuses_a_model_filter(codex, capsys):
    code, out, err = run(["models", "--model", "gpt-a"], capsys)
    assert code == 1 and "does not filter models by --model" in err
    assert codex.calls() == []


@pytest.mark.parametrize("preset", ["claude", "muse", "ollama-codex"])
def test_other_engines_reject_the_efforts_model_filter(preset, capsys):
    code = r4t_main(["engine", preset, "efforts", "--model", "x"])
    assert code == 1
    assert "does not filter efforts by --model" in capsys.readouterr().err


def test_ollama_codex_still_discovers_ollama_models(monkeypatch):
    seen = []
    monkeypatch.setattr(values, "_lines", lambda argv: seen.append(argv) or ["NAME ID", "q:latest x"])
    assert values.models("ollama-codex") == (["q:latest"], None)
    assert seen == [["ollama", "list"]]


@pytest.mark.parametrize("pages,match", [
    ([{"result": {"data": []}}], "listed no models"),
    ([{"result": {"data": "x"}}], "no data list"),
    ([{"result": {"data": [{"id": "only-id"}]}}], "without a model string"),
    ([{"result": {"data": [entry("m", ["low"])], "nextCursor": 5}}], "malformed nextCursor"),
    ([{"result": {"data": [entry("m", ["low"])], "nextCursor": "0"}}], "repeated a nextCursor"),
])
def test_malformed_catalogs_fail_clearly(codex, capsys, pages, match):
    codex.configure(pages=pages)
    code, out, err = run(["models"], capsys)
    assert code == 1 and out == "" and match in err


def test_endless_pagination_is_bounded(codex, capsys, monkeypatch):
    monkeypatch.setattr(values, "CODEX_MAX_PAGES", 3)
    codex.configure(pages=[
        {"result": {"data": [entry("m%d" % i, ["low"])], "nextCursor": str((i + 1) % 9 + 10)}}
        for i in range(9)
    ])
    code, _, err = run(["models"], capsys)
    assert code == 1
    assert "within 3 pages" in err


@pytest.mark.parametrize("efforts", [None, "low", ["low", 3], [{"reasoningEffort": ""}]])
def test_malformed_efforts_fail(codex, capsys, efforts):
    row = entry("m", ["low"])
    if efforts is None:
        del row["supportedReasoningEfforts"]
    else:
        row["supportedReasoningEfforts"] = efforts
    codex.configure(pages=[{"result": {"data": [row]}}])
    code, _, err = run(["efforts", "--model", "m"], capsys)
    assert code == 1 and "supportedReasoningEfforts" in err
    assert run(["efforts"], capsys)[0] == 1
    assert run(["models"], capsys)[0] == 0


def test_empty_efforts_are_a_fact_not_a_failure(codex, capsys):
    codex.configure(pages=[{"result": {"data": [entry("none", []), entry("some", ["low", "high"])]}}])
    code, out, _ = run(["efforts"], capsys)
    assert code == 0 and out.split() == ["low", "high"]
    code, out, err = run(["efforts", "--model", "none"], capsys)
    assert code == 0 and out == "" and "no effort options" in err
    code, out, _ = run(["efforts", "--model", "some"], capsys)
    assert code == 0 and out.split() == ["low", "high"]


def test_all_empty_union_is_empty_with_a_note(codex, capsys):
    codex.configure(pages=[{"result": {"data": [entry("none", [])]}}])
    code, out, err = run(["efforts"], capsys)
    assert code == 0 and out == "" and "no effort options" in err


def test_rpc_errors_fail_clearly(codex, capsys):
    codex.configure(mode="list-error")
    code, _, err = run(["models"], capsys)
    assert code == 1 and "model/list failed: catalog broke" in err
    codex.configure(mode="init-error")
    code, _, err = run(["models"], capsys)
    assert code == 1 and "initialize failed: no handshake" in err
    assert codex.methods().count("model/list") == 1


def test_exit_before_answer_fails(codex, capsys):
    codex.configure(mode="exit")
    code, _, err = run(["models"], capsys)
    assert code == 1 and ("exited before answering" in err or "closed its input" in err)


def test_silent_server_times_out_and_is_reaped(codex, capsys, monkeypatch):
    monkeypatch.setattr(values, "LIST_TIMEOUT_SECONDS", 1)
    codex.configure(mode="silent")
    started = time.monotonic()
    code, _, err = run(["models"], capsys)
    assert code == 1 and "timed out" in err
    assert time.monotonic() - started < 10


def test_chatty_server_is_bounded(codex, capsys, monkeypatch):
    monkeypatch.setattr(values, "LIST_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(values, "CODEX_MAX_FRAMES", 200)
    codex.configure(mode="chatty")
    code, _, err = run(["models"], capsys)
    assert code == 1 and ("response limit" in err or "timed out" in err)


def test_missing_cli_fails_clearly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(values, "CODEX_APP_SERVER_ARGV", [str(tmp_path / "no-such-codex")])
    code, _, err = run(["models"], capsys)
    assert code == 1 and "could not run `" in err and "no-such-codex" in err


def _alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except OSError:
        return False


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="reads /proc")
@pytest.mark.parametrize("mode", ["ok", "exit", "list-error"])
def test_a_child_holding_the_pipes_is_terminated(codex, tmp_path, capsys, monkeypatch, mode):
    monkeypatch.setattr(values, "LIST_TIMEOUT_SECONDS", 2)
    pidfile = tmp_path / "child.pid"
    codex.configure(mode=mode, child=str(pidfile))
    started = time.monotonic()
    run(["models"], capsys)
    assert time.monotonic() - started < 10
    child = int(pidfile.read_text(encoding="utf-8"))
    try:
        deadline = time.monotonic() + 3
        while _alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(child), "forked helper survived the catalog read"
    finally:
        if _alive(child):
            os.kill(child, signal.SIGKILL)


def test_launch_owns_a_group(codex, monkeypatch):
    seen = []
    real = values.launch_options
    monkeypatch.setattr(values, "launch_options", lambda **kw: seen.append((kw, real(**kw))) or real(**kw))
    values.models("codex")
    assert len(seen) == 1 and seen[0][0] == {"new_session": True}
    if os.name == "posix":
        assert seen[0][1]["start_new_session"] is True
    else:
        assert seen[0][1]["creationflags"] & 0x00000200


HARNESS = r"""
import json, os, sys, time
sys.path[:0] = json.loads(os.environ["HARNESS_PATH"])
from engines import values
values.CODEX_APP_SERVER_ARGV = json.loads(os.environ["HARNESS_ARGV"])
values.LIST_TIMEOUT_SECONDS = 2
started = time.monotonic()
try:
    values.models("codex")
    print("RESULT ok")
except values.ValuesError as exc:
    print("RESULT", exc)
print("ELAPSED", time.monotonic() - started)
"""


@pytest.mark.parametrize("mode", ["big-request", "deaf-pager"])
def test_a_child_that_stops_reading_stdin_cannot_hang(codex, tmp_path, mode):
    pidfile = tmp_path / "server.pid"
    codex.configure(mode=mode, pidfile=str(pidfile))
    env = dict(os.environ, HARNESS_PATH=json.dumps(sys.path), HARNESS_ARGV=json.dumps(values.CODEX_APP_SERVER_ARGV))
    done = subprocess.run([sys.executable, "-c", HARNESS], env=env, capture_output=True,
                          text=True, timeout=30)
    assert "RESULT" in done.stdout and "timed out after 2s" in done.stdout, done.stdout + done.stderr
    elapsed = float(done.stdout.split("ELAPSED")[1])
    assert elapsed < 8
    pid = int(pidfile.read_text(encoding="utf-8"))
    if os.path.isdir("/proc"):
        deadline = time.monotonic() + 3
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(pid), "server survived cleanup"


def test_verbs_and_list_show_codex_discovery(capsys):
    assert values.verbs("codex") == ["efforts", "models"]
    assert r4t_main(["engine", "list"]) == 0
    assert any("codex" in l and "models" in l for l in capsys.readouterr().out.splitlines())
