"""A stored secret signs in to a real page, only on its origins, and its value reaches no reply, no command line and no file but the store. Live: needs Chrome and playwright-cli."""
import hashlib
import http.server
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from browser import plc

pytestmark = pytest.mark.skipif(
    os.environ.get("AR3_TOOL_BROWSER_LIVE") != "1",
    reason="live: set AR3_TOOL_BROWSER_LIVE=1",
)

TOOL = Path(__file__).resolve().parents[2] / "browser"

# Made-up values. The password has the characters a snapshot has to quote.
PASSWORD = '@Zq7"live\\password: #4417'
PIN = "pin-live-8820"
WRONG = "wrong-live-value-3351"
CONTROL = "argv-control-9954"

GUARD = (
    "eval and run-code are off on a seat that holds secrets; they could read a field a secret was "
    "typed into. Remove the secrets (browser secret rm) or use a seat without them."
)

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Sign in</title></head><body>
<form onsubmit="return false">
  <label for="user">Username</label><input id="user" type="text">
  <label for="pw">Password</label><input id="pw" type="password">
  <label for="pin">PIN</label><input id="pin" type="password">
  <label for="locked">Locked</label><input id="locked" type="text" readonly>
  <button id="go" type="button">Sign in</button>
</form>
<p id="result">waiting</p>
<script>
const WANT = %(want)s;
async function sha(text) {
  const bytes = new TextEncoder().encode(text);
  const hash = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(hash)).map(b => b.toString(16).padStart(2, '0')).join('');
}
document.getElementById('go').addEventListener('click', async () => {
  const pw = document.getElementById('pw').value;
  const pin = document.getElementById('pin').value;
  console.log('typed ' + pw);
  const ok = (await sha(pw)) === WANT.pw && (await sha(pin)) === WANT.pin;
  document.getElementById('result').textContent = 'result: ' + (ok ? 'ok' : 'bad');
});
</script></body></html>
"""

SIGN_IN = """\
go {url}
fill #user alice
fill-secret #pw PASSWORD
fill-secret #pin PIN
snap
click #go
wait 1
text #result
snap
console
"""


FRAME = """<!doctype html>
<html><head><meta charset="utf-8"><title>Frame host</title></head><body>
<label for="own">Own</label><input id="own" type="password">
<iframe id="inner" src="%(inner)s" width="600" height="300"></iframe>
</body></html>
"""


def _serve(inner=None):
    """The form, on 127.0.0.1 and a port of its own, so each server is its own origin. The page
    holds the SHA-256 of the right values, not the values. `/frame` is a page that frames `inner`."""
    want = {
        "pw": hashlib.sha256(PASSWORD.encode()).hexdigest(),
        "pin": hashlib.sha256(PIN.encode()).hexdigest(),
    }
    form = (PAGE % {"want": json.dumps(want)}).encode()
    frame = (FRAME % {"inner": inner or "about:blank"}).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = frame if self.path.startswith("/frame") else form
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class ArgvWatcher:
    """Reads every process's command line over and over while a block runs, and
    remembers which of the `needles` it ever saw in one."""

    def __init__(self, needles):
        self.needles = needles
        self.seen = set()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        while not self._stop.is_set():
            listing = subprocess.run(["ps", "-axww", "-o", "args="], capture_output=True, text=True).stdout
            self.seen.update(needle for needle in self.needles if needle in listing)
            time.sleep(0.01)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()


def _env(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
    env.update(
        XDG_DATA_HOME=str(tmp_path / "data"),
        XDG_CACHE_HOME=str(tmp_path / "cache"),
        A8S_HOME=str(tmp_path / "a8s"),
    )
    return env


def _tool(env, seat, *args, text=None, timeout=180):
    return subprocess.run(
        [sys.executable, str(TOOL), *args, "--seat", seat],
        env=env, input=text, capture_output=True, text=True, timeout=timeout,
    )


def _script(env, seat, text):
    done = subprocess.run(
        [sys.executable, str(TOOL), "--seat", seat, "do", "-", "--json"],
        env=env, input=text, capture_output=True, text=True, timeout=180,
    )
    return done, json.loads(done.stdout.strip().splitlines()[-1])


def _set(env, seat, name, value, *origins):
    flags = [flag for origin in origins for flag in ("--origin", origin)]
    stored = _tool(env, seat, "secret", "set", name, *flags, text=value)
    assert stored.returncode == 0, (stored.stdout, stored.stderr)
    assert value not in stored.stdout + stored.stderr


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def _files_holding(roots, values):
    """{path: [value, ...]} for every file under `roots` that holds a value, as UTF-8 or UTF-16."""
    forms = {value: (value.encode(), value.encode("utf-16-le")) for value in values}
    found = {}
    for root in roots:
        for directory, _dirs, names in os.walk(root):
            for name in names:
                path = os.path.join(directory, name)
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                try:
                    data = Path(path).read_bytes()
                except OSError:
                    continue
                for value, encodings in forms.items():
                    if any(encoding in data for encoding in encodings):
                        found.setdefault(path, []).append(value)
    return found


def _gone(profile, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        listing = subprocess.run(
            ["pgrep", "-af", "--", f"--user-data-dir={profile}"], capture_output=True, text=True
        ).stdout.strip()
        if not listing:
            return True
        time.sleep(0.25)
    return False


def test_a_stored_secret_signs_in_and_its_value_reaches_nothing_but_the_store(tmp_path):
    seat = f"ar3scratch{os.getpid()}secret"
    env = _env(tmp_path)
    home = tmp_path / "data/ar3/browser" / seat
    profile = home / "chrome"
    other = _serve()
    server = _serve(inner=f"http://127.0.0.1:{other.server_address[1]}/")
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    elsewhere = f"http://127.0.0.1:{other.server_address[1]}/"
    allowed = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        _set(env, seat, "PASSWORD", PASSWORD, allowed, "https://*.example.org")
        _set(env, seat, "PIN", PIN, allowed)
        _set(env, seat, "WRONG", WRONG, allowed)
        listed = _tool(env, seat, "secret", "list")
        assert listed.stdout.splitlines() == [
            f"PASSWORD {allowed} https://*.example.org", f"PIN {allowed}", f"WRONG {allowed}",
        ], listed
        assert not any(value in listed.stdout + listed.stderr for value in (PASSWORD, PIN, WRONG))
        wide = _tool(env, seat, "secret", "set", "WIDE", "--origin", "https://*.com", text="wide-value-1234")
        assert wide.returncode == 1 and "at least two labels" in wide.stderr
        assert "wide-value-1234" not in wide.stdout + wide.stderr
        assert _tool(env, seat, "secret", "list").stdout == listed.stdout
        store = home / "secrets.json"
        assert _mode(store) == 0o600
        assert _mode(home) == 0o700

        # A positive control for the watcher: a plain `fill` does put its text in an argv.
        with ArgvWatcher([CONTROL]) as control:
            plain, payload = _script(env, seat, f"go {url}\nfill #user {CONTROL}\n")
        assert payload["ok"], payload
        assert control.seen == {CONTROL}, "the watcher cannot see a command line, so it proves nothing"

        # The sign-in: the page's own script checks the value that was typed.
        with ArgvWatcher([PASSWORD, PIN, WRONG]) as watch:
            done, payload = _script(env, seat, SIGN_IN.format(url=url))
        assert done.returncode == 0 and payload["ok"], (payload, done.stderr)
        steps = {step["command"]: step for step in payload["steps"]}
        assert steps["fill-secret #pw PASSWORD"]["output"] == "#pw <- [secret:PASSWORD]"
        assert steps["fill-secret #pin PIN"]["output"] == "#pin <- [secret:PIN]"
        assert steps["text #result"]["output"] == "result: ok"
        assert watch.seen == set(), f"a value was in a command line: {watch.seen}"

        everything = done.stdout
        assert PASSWORD not in everything and PIN not in everything
        snapshots = [Path(p) for p in payload["files"] if p.endswith("snapshot.txt")]
        consoles = [Path(p) for p in payload["files"] if p.endswith("console.log")]
        assert len(snapshots) == 2 and len(consoles) == 1
        for snapshot in snapshots:
            body = snapshot.read_text()
            assert "[secret:PASSWORD]" in body and "[secret:PIN]" in body
            assert PASSWORD not in body and PIN not in body
        console = consoles[0].read_text()
        assert "typed [secret:PASSWORD]" in console and PASSWORD not in console
        assert "typed [secret:PASSWORD]" in steps["console"]["output"]

        # The page checks for real: a wrong value reads as wrong.
        wrong, payload = _script(
            env, seat, f"go {url}\nfill-secret #pw WRONG\nfill-secret #pin PIN\nclick #go\nwait 1\ntext #result\n"
        )
        assert wrong.returncode == 0, payload
        assert payload["steps"][-1]["output"] == "result: bad"
        assert WRONG not in wrong.stdout

        # An agent works from refs: it takes a snapshot, reads a ref off it, and names that ref.
        _, payload = _script(env, seat, f"go {url}\nsnap\n")
        snapshot = Path([p for p in payload["files"] if p.endswith("snapshot.txt")][0]).read_text()
        refs = {
            label: re.search(rf'textbox "{label}"(?: \[active\])? \[ref=(\w+)\]', snapshot).group(1)
            for label in ("Password", "PIN")
        }
        by_ref, payload = _script(
            env, seat,
            f"fill-secret {refs['Password']} PASSWORD\nfill-secret {refs['PIN']} PIN\nclick #go\nwait 1\ntext #result\n",
        )
        assert by_ref.returncode == 0 and payload["ok"], payload
        assert payload["steps"][0]["output"] == f"{refs['Password']} <- [secret:PASSWORD]"
        assert payload["steps"][-1]["output"] == "result: ok"
        assert PASSWORD not in by_ref.stdout and PIN not in by_ref.stdout

        # The same form on another origin (another port is another origin): nothing is typed.
        refused_origin = (
            f"fill-secret: secret PASSWORD may not be typed on origin {elsewhere.rstrip('/')}; "
            f"allowed origins: {allowed}, https://*.example.org"
        )
        second, payload = _script(env, seat, f"go {elsewhere}\nfill-secret #pw PASSWORD\n")
        assert second.returncode == 1
        assert payload["steps"][-1]["error"] == refused_origin
        assert PASSWORD not in second.stdout
        failure = Path([p for p in payload["files"] if p.endswith("failure.txt")][0]).read_text()
        assert "[secret:PASSWORD]" not in failure
        assert re.search(r'textbox "Password"(?: \[active\])? \[ref=\w+\]\n', failure), failure
        typed_nothing, payload = _script(env, seat, "click #go\nwait 1\ntext #result\n")
        assert payload["steps"][-1]["output"] == "result: bad"

        # A cross-origin frame on a page whose own origin is allowed counts as its own origin.
        _, payload = _script(env, seat, f"go {url}frame\nsnap\n")
        framed = Path([p for p in payload["files"] if p.endswith("snapshot.txt")][0]).read_text()
        own = re.search(r'textbox "Own"(?: \[active\])? \[ref=(\w+)\]', framed).group(1)
        inside = re.search(r'textbox "Password"(?: \[active\])? \[ref=(\w+)\]', framed).group(1)
        assert own != inside
        framed_run, payload = _script(env, seat, f"fill-secret {own} PIN\nfill-secret {inside} PASSWORD\n")
        assert framed_run.returncode == 1
        assert payload["steps"][0]["ok"] and payload["steps"][0]["output"] == f"{own} <- [secret:PIN]"
        assert payload["steps"][1]["error"] == refused_origin
        assert PASSWORD not in framed_run.stdout and PIN not in framed_run.stdout

        # eval and run-code are off on a seat that holds a secret, with ALLOW_EVAL on. fill-secret still works.
        allow_eval = dict(env, AR3_BROWSER_ALLOW_EVAL="1")
        for line in ("eval 1 + 1", "run-code <<END\nreturn 1;\nEND"):
            guarded, payload = _script(allow_eval, seat, line + "\n")
            assert guarded.returncode == 1 and payload["steps"][0]["error"] == GUARD, line
        still, payload = _script(
            allow_eval, seat,
            f"go {url}\nfill-secret #pw PASSWORD\nfill-secret #pin PIN\nclick #go\nwait 1\ntext #result\n",
        )
        assert still.returncode == 0 and payload["steps"][-1]["output"] == "result: ok", payload

        # The state directory is out of reach of a script. The tool opens no local file, so `go` refuses a file:
        # URL whatever it names, and upload and drop refuse a path inside the state directory.
        for line in (f"go file://{store}", f"go file://{home}/"):
            reach, payload = _script(env, seat, line + "\n")
            assert reach.returncode == 1 and "local files are not opened" in payload["steps"][0]["error"], line
        for line in (f"upload {store}", f"drop #pw {store}"):
            reach, payload = _script(env, seat, line + "\n")
            assert reach.returncode == 1 and "browser state directory" in payload["steps"][0]["error"], line

        # A fill that fails: Playwright prints the value it was given in the call log of the error, so on a seat that
        # holds a secret the error is its first line and a note, and the call log is not shown.
        locked, payload = _script(env, seat, f"go {url}\nfill-secret #locked PIN\n")
        assert locked.returncode == 1
        failed = payload["steps"][-1]["error"]
        assert failed == "### Error\nTimeoutError: " + plc.DRIVER_MESSAGE_WITHHELD, failed
        assert "fill(" not in failed and "not editable" not in failed
        assert PIN not in locked.stdout
        for path in payload["files"]:
            assert PIN.encode() not in Path(path).read_bytes()

        # A name nobody stored is refused with the fix, before the page is touched.
        missing, payload = _script(env, seat, "fill-secret #pw NOPE\n")
        assert missing.returncode == 1
        assert payload["steps"][0]["error"] == (
            f"secret NOPE is not set for seat {seat}; "
            f"the operator sets it with `browser secret set NOPE --origin ORIGIN --seat {seat}`"
        )

        # A run closes a seat directory that an older run left open.
        scratch = home / "scratch"
        os.chmod(home, 0o775)
        os.chmod(scratch, 0o775)
        assert _script(env, seat, "url\n")[0].returncode == 0
        assert _mode(home) == 0o700 and _mode(scratch) == 0o700

        # Close Chrome so the profile is flushed, then look in every file for every value.
        assert _tool(env, seat, "close").returncode == 0
        assert _gone(profile)
        found = _files_holding([home, tmp_path / "cache"], [PASSWORD, PIN, WRONG])
        assert set(found) <= {str(store)}, found
        assert json.loads(store.read_text())["secrets"] == {
            "PASSWORD": {"value": PASSWORD, "origins": [allowed, "https://*.example.org"]},
            "PIN": {"value": PIN, "origins": [allowed]},
            "WRONG": {"value": WRONG, "origins": [allowed]},
        }

        prefs = json.loads((profile / "Default" / "Preferences").read_text())
        assert prefs["credentials_enable_service"] is False
        assert prefs["profile"]["password_manager_leak_detection"] is False

        # The fix the refusal names: with the secrets removed, the two verbs work again.
        for name in ("PASSWORD", "PIN", "WRONG"):
            assert _tool(env, seat, "secret", "rm", name).returncode == 0
        given_back, payload = _script(allow_eval, seat, "eval 1 + 1\nrun-code <<END\nreturn 2;\nEND\n")
        assert given_back.returncode == 0, payload
        assert [step["output"] for step in payload["steps"]] == ["2", "2"]
    finally:
        server.shutdown()
        other.shutdown()
        _tool(env, seat, "close")
        _gone(profile)
