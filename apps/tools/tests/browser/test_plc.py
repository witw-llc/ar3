import json
import re
import os
import shutil
import stat
import subprocess
import sys
import tempfile

import pytest

from browser import plc
from browser import secretstore



def _left(tmp_path):
    """What a call left in `tmp_path`, apart from the guarded data home."""
    return [name for name in os.listdir(tmp_path) if name != "xdg"]

def test_result_section_stops_at_the_next_header():
    output = "### Result\nhello\n\n### Console\nnoise\n"
    assert plc.result_of(output) == "hello"


def test_result_section_unquotes():
    assert plc.result_of("### Result\n'https://example.com'\n") == "https://example.com"


def test_a_string_result_is_decoded_once_and_exactly():
    output = '### Result\n"a\\nb \\"q\\" \\\\n caf\\u00e9"\n'
    assert plc.result_of(output) == 'a\nb "q" \\n caf\u00e9'


def test_a_quoted_result_that_is_not_json_is_only_unquoted():
    assert plc.result_of('### Result\n"not \\x json"\n') == "not \\x json"


def test_missing_result_section_is_an_error():
    with pytest.raises(plc.BrowserError):
        plc.result_of("nothing useful here")


def test_error_section_fails_the_run_despite_exit_zero(monkeypatch):
    monkeypatch.setattr(plc, "require", lambda: "playwright-cli")
    monkeypatch.setattr(
        plc.subprocess, "run",
        lambda *a, **k: _Completed(0, "### Error\nelement not found\n"),
    )
    with pytest.raises(plc.BrowserError, match="element not found"):
        plc.run("seat", "click", "#nope")


def test_nonzero_exit_reports_stderr(monkeypatch):
    monkeypatch.setattr(plc, "require", lambda: "playwright-cli")
    monkeypatch.setattr(plc.subprocess, "run", lambda *a, **k: _Completed(1, "", "boom"))
    with pytest.raises(plc.BrowserError, match="boom"):
        plc.run("seat", "click", "#nope")


def test_js_string_escapes_quotes_and_newlines():
    assert plc.js_string("it's\n\"here\"") == "it\\'s\\n\\\"here\\\""


def test_sessions_parses_the_list_output(monkeypatch):
    monkeypatch.setattr(plc.shutil, "which", lambda name: "/usr/bin/playwright-cli")
    monkeypatch.setattr(
        plc.subprocess, "run",
        lambda *a, **k: _Completed(0, (
            "### Browsers\n"
            "- garmin:\n"
            "  - status: open\n"
            "  - browser-type: chrome (attached)\n"
            "- work:\n"
            "  - status: open\n"
            "  - browser-type: chrome (attached)\n"
        )),
    )
    assert plc.sessions() == ["garmin", "work"]


def test_result_or_falls_back_to_the_raw_output():
    assert plc.result_or("### Result\nbody\n") == "body"
    assert plc.result_or("plain output\n") == "plain output"


def test_run_code_gives_the_body_lines_of_its_own(monkeypatch):
    sent = []
    monkeypatch.setattr(plc, "run", lambda seat, *args, **kwargs: sent.append(args) or "")
    plc.run_code("seat", "await page.click('#go'); // the last word")
    assert sent[0] == (
        "run-code",
        "async function f(page) {\nawait page.click('#go'); // the last word\n}",
    )


def test_screenshot_rejects_a_success_that_wrote_no_file(monkeypatch, tmp_path):
    monkeypatch.setattr(plc, "run", lambda *a, **k: "### Result\nok\n")
    with pytest.raises(plc.BrowserError, match="wrote no file"):
        plc.screenshot("seat", str(tmp_path / "missing.png"))


def test_screenshot_takes_the_whole_page_only_when_asked(monkeypatch, tmp_path):
    sent = []

    def shoot(seat, *args, **kwargs):
        sent.append(args)
        with open(args[1].split("=", 1)[1], "wb") as handle:
            handle.write(b"png")
        return "### Result\nok\n"

    monkeypatch.setattr(plc, "run", shoot)
    plain, whole = str(tmp_path / "view.png"), str(tmp_path / "whole.png")
    assert plc.screenshot("seat", plain) == plain
    assert plc.screenshot("seat", whole, full_page=True) == whole
    assert sent == [
        ("screenshot", f"--filename={plain}"),
        ("screenshot", f"--filename={whole}", "--full-page"),
    ]


def test_require_without_a_terminal_names_the_install_command(monkeypatch):
    monkeypatch.setattr(plc, "_asked", False)
    monkeypatch.setattr(plc.shutil, "which", lambda name: None)
    monkeypatch.setattr(plc, "_offer_install", lambda: False)
    with pytest.raises(plc.BrowserError, match="npm install -g @playwright/cli"):
        plc.require()


class _Completed:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


VALUE = "hunter2-value"
ORIGIN = "https://app.example.com"
PARTS = [{"scheme": "https", "host": "app.example.com", "port": "", "wildcard": False}]
TYPED = _Completed(0, '{"typed":"https://app.example.com"}\n')


def _spy_cli(monkeypatch, result=TYPED):
    """playwright-cli as a subprocess: each call is recorded with the code file it
    was told to read, read while it exists."""
    seen = []

    def fake(cmd, **kwargs):
        path = [part for part in cmd if part.startswith("--filename=")][0].split("=", 1)[1]
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        seen.append({
            "cmd": list(cmd), "kwargs": kwargs, "path": path, "text": text,
            "file_mode": stat.S_IMODE(os.stat(path).st_mode),
            "dir_mode": stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode),
        })
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(plc, "require", lambda: "playwright-cli")
    monkeypatch.setattr(plc.subprocess, "run", fake)
    return seen


def test_fill_secret_hands_the_driver_a_file_and_the_value_is_in_no_argv(monkeypatch, tmp_path):
    seen = _spy_cli(monkeypatch)
    plc.fill_secret("seat", "#pw", VALUE, PARTS, str(tmp_path))
    (call,) = seen
    assert call["cmd"][:4] == ["playwright-cli", f"-s={plc.session_name('seat')}", "--raw", "run-code"]
    assert call["cmd"][4] == f"--filename={call['path']}"
    assert len(call["cmd"]) == 5
    assert VALUE not in "".join(call["cmd"])
    assert call["text"] == plc.fill_secret_code("#pw", VALUE, PARTS)
    assert json.dumps(VALUE) in call["text"]


def test_fill_secret_returns_the_origin_it_typed_on(monkeypatch, tmp_path):
    _spy_cli(monkeypatch)
    assert plc.fill_secret("seat", "#pw", VALUE, PARTS, str(tmp_path)) == ORIGIN


@pytest.mark.skipif(os.name == "nt", reason="file modes are POSIX")
def test_fill_secret_keeps_its_file_private(monkeypatch, tmp_path):
    seen = _spy_cli(monkeypatch)
    plc.fill_secret("seat", "#pw", VALUE, PARTS, str(tmp_path))
    (call,) = seen
    assert call["file_mode"] == 0o600
    assert call["dir_mode"] == 0o700
    assert os.path.dirname(os.path.dirname(call["path"])) == str(tmp_path)
    assert os.path.basename(os.path.dirname(call["path"])).startswith(plc.SECRET_DIR_PREFIX)


@pytest.mark.parametrize(
    "result, error",
    [
        (TYPED, None),
        (_Completed(0, '{"refused":"https://evil.example"}\n'), plc.OriginRefused),
        (_Completed(1, "### Error\nTimeoutError\n"), plc.BrowserError),
        (_Completed(0, "### Error\nTimeoutError\n"), plc.BrowserError),
        (_Completed(0, "\n"), plc.BrowserError),
        (subprocess.TimeoutExpired(["playwright-cli"], 30), plc.BrowserError),
    ],
)
def test_fill_secret_removes_its_file_whatever_the_driver_does(monkeypatch, tmp_path, result, error):
    seen = _spy_cli(monkeypatch, result)
    if error:
        with pytest.raises(error):
            plc.fill_secret("seat", "#pw", VALUE, PARTS, str(tmp_path))
    else:
        plc.fill_secret("seat", "#pw", VALUE, PARTS, str(tmp_path))
    assert _left(tmp_path) == []
    assert seen and not os.path.exists(seen[0]["path"])


def test_fill_secret_passes_the_timeout_to_the_call(monkeypatch, tmp_path):
    seen = _spy_cli(monkeypatch)
    plc.fill_secret("seat", "#pw", VALUE, PARTS, str(tmp_path))
    plc.fill_secret("seat", "#pw", VALUE, PARTS, str(tmp_path), timeout=7)
    assert [call["kwargs"]["timeout"] for call in seen] == [30, 7]


def test_fill_secret_writes_its_file_aside_and_renames_it(monkeypatch, tmp_path):
    _spy_cli(monkeypatch)
    replaced = []
    real = os.replace
    monkeypatch.setattr(plc.os, "replace", lambda a, b: (replaced.append((a, b)), real(a, b))[1])
    plc.fill_secret("seat", "#pw", VALUE, PARTS, str(tmp_path))
    (source, target), = replaced
    assert os.path.dirname(source) == os.path.dirname(target)
    assert source.endswith(".tmp") and target.endswith("fill.js")


def test_a_refusal_carries_the_origin_the_field_sat_on(monkeypatch, tmp_path):
    _spy_cli(monkeypatch, _Completed(0, '{"refused":"https://evil.example:8443"}\n'))
    with pytest.raises(plc.OriginRefused) as refused:
        plc.fill_secret("seat", "#pw", VALUE, PARTS, str(tmp_path))
    assert refused.value.origin == "https://evil.example:8443"
    assert isinstance(refused.value, plc.BrowserError)


@pytest.mark.parametrize(
    "output", ["", "garbage", "[]", "{}", '{"typed": 5}', '{"other": "x"}', "### Result\nok", f"echo {VALUE}", f'{{"typed": "{VALUE}", "x"']
)
def test_an_outcome_that_is_not_one_is_an_error_that_does_not_repeat_the_output(monkeypatch, tmp_path, output):
    _spy_cli(monkeypatch, _Completed(0, output + "\n"))
    with pytest.raises(plc.BrowserError, match="returned no outcome") as bad:
        plc.fill_secret("seat", "#pw", VALUE, PARTS, str(tmp_path))
    assert VALUE not in str(bad.value)


@pytest.mark.parametrize("value", ['pa"ss\\word', "line one\nline two", "</script>", "caf\u00e9 \u2028 \u2603", "'; await x; //"])
def test_the_value_is_one_string_literal_whatever_it_holds(monkeypatch, tmp_path, value):
    seen = _spy_cli(monkeypatch)
    plc.fill_secret("seat", "input[name='a']", value, PARTS, str(tmp_path))
    text = seen[0]["text"]
    assert text.count("\n") == plc.fill_secret_code("#pw", "x", PARTS).count("\n")
    literal = text.split(".fill(", 1)[1].rsplit(", {timeout", 1)[0]
    assert json.loads(literal) == value


# --- the check in the file, run as playwright-cli runs it -----------------------------

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="needs node, which playwright-cli needs too")

# playwright-cli runs the file's function in a vm context that holds `page` and these globals, and
# nothing else. The stand-in page records every call, and its field sits in a document of `origin`.
HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync(process.argv[2], 'utf8');
const origin = process.argv[3];
const calls = [];
const element = { ownerDocument: { location: { origin } } };
const locator = {
  evaluate: async (fn, arg, options) => { calls.push(['evaluate', options]); return fn(element, arg); },
  fill: async (value, options) => { calls.push(['fill', value, options]); },
};
const page = {
  locator: (selector) => { calls.push(['locator', selector]); return locator; },
  url: () => { calls.push(['url']); return 'https://app.example.com/page'; },
};
const names = ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'setImmediate', 'clearImmediate',
  'queueMicrotask', 'URL', 'URLSearchParams', 'TextEncoder', 'TextDecoder', 'AbortController', 'AbortSignal',
  'fetch', 'Headers', 'Request', 'Response', 'FormData', 'Blob', 'Buffer', 'crypto', 'atob', 'btoa',
  'structuredClone'];
const context = { page };
for (const name of names) context[name] = globalThis[name];
vm.createContext(context);
const fn = vm.runInContext('(' + code + ')', context);
fn(page).then(
  (result) => process.stdout.write(JSON.stringify({ result: JSON.parse(JSON.stringify(result)), calls })),
  (error) => process.stdout.write(JSON.stringify({ error: String(error), calls })),
);
"""


def _run_in_node(tmp_path, code, seen_origin):
    (tmp_path / "harness.js").write_text(HARNESS)
    (tmp_path / "fill.js").write_text(code)
    done = subprocess.run(
        [NODE, str(tmp_path / "harness.js"), str(tmp_path / "fill.js"), seen_origin],
        capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _parts(*origins):
    return [secretstore.origin_parts(secretstore.parse_origin(origin)) for origin in origins]


# (origins the secret allows, origin of the field's own document, typed?)
CHECKS = [
    (["https://app.example.com"], "https://app.example.com", True),
    (["https://app.example.com"], "https://app.example.com:8443", False),
    (["https://app.example.com"], "http://app.example.com", False),
    (["https://app.example.com"], "https://evil.example", False),
    (["https://app.example.com"], "https://app.example.com.evil.example", False),
    (["https://app.example.com"], "https://evilapp.example.com", False),
    (["https://app.example.com"], "https://sub.app.example.com", False),
    (["https://app.example.com"], "https://example.com", False),
    (["https://app.example.com:8443"], "https://app.example.com:8443", True),
    (["https://app.example.com:8443"], "https://app.example.com", False),
    (["https://app.example.com:8443"], "https://app.example.com:9443", False),
    (["http://app.example.com"], "http://app.example.com", True),
    (["http://app.example.com"], "https://app.example.com", False),
    (["https://*.example.org"], "https://a.example.org", True),
    (["https://*.example.org"], "https://a.b.example.org", True),
    (["https://*.example.org"], "https://example.org", False),
    (["https://*.example.org"], "https://evilexample.org", False),
    (["https://*.example.org"], "https://a.example.org.evil.example", False),
    (["https://*.example.org"], "http://a.example.org", False),
    (["https://*.example.org"], "https://a.example.org:8443", False),
    (["https://*.example.org:8443"], "https://a.example.org:8443", True),
    (["https://*.example.org:8443"], "https://a.example.org", False),
    (["https://*.co.uk"], "https://shop.co.uk", True),
    (["https://*.co.uk"], "https://a.shop.co.uk", True),
    (["https://*.co.uk"], "https://co.uk", False),
    (["https://*.co.uk"], "https://shop.uk", False),
    (["http://*.localhost:3000"], "http://app.localhost:3000", True),
    (["http://*.localhost:3000"], "http://localhost:3000", False),
    (["http://*.localhost:3000"], "http://app.localhost:3001", False),
    (["https://app.example.com", "https://*.example.org"], "https://app.example.com", True),
    (["https://app.example.com", "https://*.example.org"], "https://x.example.org", True),
    (["https://app.example.com", "https://*.example.org"], "https://example.org", False),
    (["https://app.example.com", "https://*.example.org"], "https://example.net", False),
    (["http://127.0.0.1:18931"], "http://127.0.0.1:18931", True),
    (["http://127.0.0.1:18931"], "http://127.0.0.1:18932", False),
    (["http://127.0.0.1:18931"], "http://127.0.0.1", False),
    (["http://127.0.0.1:18931"], "http://localhost:18931", False),
    (["http://[::1]:3000"], "http://[::1]:3000", True),
    (["http://[::1]:3000"], "http://[::1]:3001", False),
    (["https://app.example.com"], "null", False),
    (["https://app.example.com"], "file://", False),
    (["https://app.example.com"], "about:blank", False),
    (["https://app.example.com"], "data:text/html,x", False),
    (["https://app.example.com"], "", False),
]


@needs_node
@pytest.mark.parametrize("allowed, seen, typed", CHECKS)
def test_the_value_is_typed_only_on_an_allowed_origin(tmp_path, allowed, seen, typed):
    out = _run_in_node(tmp_path, plc.fill_secret_code("#pw", VALUE, _parts(*allowed)), seen)
    assert "error" not in out, out
    fills = [call for call in out["calls"] if call[0] == "fill"]
    if typed:
        assert out["result"] == {"typed": seen}
        assert fills == [["fill", VALUE, {"timeout": 5000}]]
    else:
        assert out["result"] == {"refused": seen}
        assert fills == []


@needs_node
def test_the_origin_is_asked_of_the_field_and_before_the_fill(tmp_path):
    """The page around the field is on an allowed origin. The field's own document is not."""
    out = _run_in_node(tmp_path, plc.fill_secret_code("aria-ref=f1e5", VALUE, _parts(ORIGIN)), "http://127.0.0.1:18932")
    assert out["result"] == {"refused": "http://127.0.0.1:18932"}
    assert ["url"] not in out["calls"]
    assert [call[0] for call in out["calls"]] == ["locator", "evaluate"]
    assert out["calls"][0] == ["locator", "aria-ref=f1e5"]
    assert out["calls"][1][1] == {"timeout": 5000}


@needs_node
def test_an_allowed_field_is_checked_then_filled_in_that_order(tmp_path):
    out = _run_in_node(tmp_path, plc.fill_secret_code("#pw", VALUE, _parts(ORIGIN)), ORIGIN)
    assert [call[0] for call in out["calls"]] == ["locator", "evaluate", "fill"]


# --- run_code_file: the recipe's own code, from a private file -----------------------------------------


def test_run_code_file_hands_the_driver_a_private_file_and_gives_back_its_output(monkeypatch, tmp_path):
    seen = _spy_cli(monkeypatch, _Completed(0, '{"value":1}\n'))
    output = plc.run_code_file("seat", "async (page) => ({value: 1})", str(tmp_path))
    (call,) = seen
    assert output == '{"value":1}\n'
    assert call["cmd"] == ["playwright-cli", f"-s={plc.session_name('seat')}", "--raw", "run-code", f"--filename={call['path']}"]
    assert call["text"] == "async (page) => ({value: 1})"
    assert os.path.basename(os.path.dirname(call["path"])).startswith(plc.CODE_DIR_PREFIX)
    assert os.path.basename(call["path"]) == "code.js"
    assert call["kwargs"]["timeout"] == 60


@pytest.mark.skipif(os.name == "nt", reason="file modes are POSIX")
def test_run_code_file_keeps_its_file_private_and_removes_it(monkeypatch, tmp_path):
    seen = _spy_cli(monkeypatch)
    plc.run_code_file("seat", "async (page) => 1", str(tmp_path))
    (call,) = seen
    assert call["file_mode"] == 0o600 and call["dir_mode"] == 0o700
    assert _left(tmp_path) == []


@pytest.mark.parametrize(
    "result",
    [
        _Completed(1, "### Error\nboom\n"),
        _Completed(0, "### Error\nboom\n"),
        subprocess.TimeoutExpired(["playwright-cli"], 60),
    ],
)
def test_run_code_file_removes_its_file_whatever_the_driver_does(monkeypatch, tmp_path, result):
    seen = _spy_cli(monkeypatch, result)
    with pytest.raises(plc.BrowserError):
        plc.run_code_file("seat", "async (page) => 1", str(tmp_path))
    assert _left(tmp_path) == [] and not os.path.exists(seen[0]["path"])


def test_run_code_file_passes_its_timeout_to_the_call(monkeypatch, tmp_path):
    seen = _spy_cli(monkeypatch)
    plc.run_code_file("seat", "async (page) => 1", str(tmp_path), timeout=9)
    assert seen[0]["kwargs"]["timeout"] == 9


def test_code_longer_than_an_argument_may_be_still_runs_because_it_is_a_file(monkeypatch, tmp_path):
    seen = _spy_cli(monkeypatch)
    body = "x" * 400_000
    plc.run_code_file("seat", f"async (page) => '{body}'", str(tmp_path))
    assert body in seen[0]["text"]
    assert sum(len(part) for part in seen[0]["cmd"]) < 500


def test_run_code_file_writes_its_file_aside_and_renames_it(monkeypatch, tmp_path):
    _spy_cli(monkeypatch)
    replaced = []
    real = os.replace
    monkeypatch.setattr(plc.os, "replace", lambda a, b: (replaced.append((a, b)), real(a, b))[1])
    plc.run_code_file("seat", "async (page) => 1", str(tmp_path))
    (source, target), = replaced
    assert os.path.dirname(source) == os.path.dirname(target)
    assert source.endswith(".tmp") and target.endswith("code.js")
    assert f".{os.getpid()}." in source


@pytest.mark.parametrize(
    "output, value",
    [
        ('{"value":[1,"a"]}\n', {"value": [1, "a"]}),
        ('noise\n{"value":null}\n\n', {"value": None}),
        ('"text"\n', "text"),
        ("7\n", 7),
        ("\n", None),
        ("", None),
    ],
)
def test_result_json_is_the_last_line_of_the_output_parsed(output, value):
    assert plc.result_json(output) == value


def test_result_json_of_something_else_is_an_error_that_does_not_repeat_it():
    with pytest.raises(plc.BrowserError, match="not JSON") as bad:
        plc.result_json("### Result\nsecret-looking text\n")
    assert "secret-looking" not in str(bad.value)


@pytest.mark.parametrize("separator", [" ", " ", "\x85", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e"])
def test_result_json_is_not_cut_by_a_character_that_python_calls_a_line_break(separator):
    """JSON.stringify leaves U+2028 and U+2029 in a string as they are, and a page can put any of these in an id or
    a text. The result is one line of the output, and only a newline ends it."""
    value = {"value": {"selector": f"#a{separator}b", "text": f"x{separator}"}}
    output = "noise\n" + json.dumps(value, ensure_ascii=False) + "\n"
    assert plc.result_json(output) == value


# --- the environment and the umask of the driver ------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "PLAYWRIGHT_MCP_OUTPUT_DIR", "playwright_mcp_output_dir", "PLAYWRIGHT_MCP_CONFIG", "PLAYWRIGHT_MCP_SAVE_SESSION",
    "PLAYWRIGHT_MCP_SAVE_TRACE", "PLAYWRIGHT_MCP_SAVE_VIDEO", "PWTEST_SOCKETS_DIR", "PWTEST_DAEMON_SESSION_DIR",
    "DEBUG", "DEBUG_FILE", "debug",
])
def test_a_variable_that_moves_or_adds_to_what_the_driver_writes_is_not_passed_on(monkeypatch, name):
    monkeypatch.setenv(name, "/somewhere")
    monkeypatch.setenv("KEPT", "yes")
    env = plc.cli_env()
    assert env.get(name) != "/somewhere" and "KEPT" in env


@pytest.fixture
def short_root():
    """A data home short enough that its `ipc` directory holds a socket path (`plc.IPC_DIR_MAX_BYTES`)."""
    import tempfile
    path = tempfile.mkdtemp(prefix="ar3t-", dir="/tmp" if os.name == "posix" else None)
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.mark.parametrize("name", ["PWTEST_SOCKETS_DIR", "pwtest_sockets_dir"])
def test_the_daemon_socket_directory_is_the_browsers_own_whatever_the_caller_sets(monkeypatch, short_root, name):
    monkeypatch.setenv("XDG_DATA_HOME", short_root)
    monkeypatch.setenv(name, "/tmp/elsewhere")
    env = plc.cli_env()
    assert env[plc.IPC_ENV] == os.path.join(short_root, "ar3", "browser", "ipc")
    assert [key for key in env if key.upper() == plc.IPC_ENV] == [plc.IPC_ENV]


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX")
def test_the_socket_directory_is_made_private_and_a_loose_one_is_tightened(monkeypatch, short_root):
    monkeypatch.setenv("XDG_DATA_HOME", short_root)
    path = plc.private_ipc_dir()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o700
    os.chmod(path, 0o777)
    plc.private_ipc_dir()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o700


@pytest.mark.skipif(os.name != "posix", reason="links and owners are POSIX here")
def test_a_socket_directory_that_is_a_link_or_another_users_stops_the_driver(monkeypatch, tmp_path, short_root):
    monkeypatch.setenv("XDG_DATA_HOME", short_root)
    link = os.path.join(short_root, "ar3", "browser", "ipc")
    os.makedirs(os.path.dirname(link))
    (tmp_path / "target").mkdir(mode=0o700)
    os.symlink(tmp_path / "target", link)
    ran = []
    monkeypatch.setattr(plc, "require", lambda: "playwright-cli")
    monkeypatch.setattr(plc.subprocess, "run", lambda cmd, **kwargs: ran.append(cmd))
    with pytest.raises(plc.BrowserError, match="socket directory"):
        plc.run("seat", "snapshot")
    os.unlink(link)
    plc.private_ipc_dir()
    monkeypatch.setattr(plc.os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(plc.BrowserError, match="socket directory"):
        plc.run("seat", "snapshot")
    assert ran == []


@pytest.mark.parametrize("name", [
    "PLAYWRIGHT_MCP_VIEWPORT_SIZE", "PLAYWRIGHT_CLI_SESSION_NAME", "PATH", "HOME", "XDG_CACHE_HOME", "DISPLAY",
    "DEBUG_COLORS_OFF", "MY_DEBUG",
])
def test_the_rest_of_the_environment_is_passed_on(monkeypatch, name):
    monkeypatch.setenv(name, "x")
    assert name in plc.cli_env()


@pytest.mark.skipif(os.name != "posix", reason="a umask is POSIX")
def test_the_driver_runs_with_a_umask_that_keeps_its_files_for_the_os_user(monkeypatch):
    calls = []
    monkeypatch.setattr(plc, "require", lambda: "playwright-cli")
    monkeypatch.setattr(plc.shutil, "which", lambda name: "/usr/bin/playwright-cli")
    monkeypatch.setattr(plc.subprocess, "run", lambda cmd, **kwargs: calls.append(kwargs) or _Completed(0, "### Result\nok\n"))
    plc.run("seat", "snapshot")
    plc.sessions()
    assert [call["umask"] for call in calls] == [0o077, 0o077]


@pytest.mark.skipif(os.name != "posix", reason="a umask is POSIX")
def test_a_file_the_driver_makes_is_mode_0600_and_a_directory_0700(tmp_path):
    """A real child process, started with the options of the driver: what it creates is for its user only."""
    script = (
        "import os, pathlib, sys\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "(root / 'd').mkdir()\n"
        "(root / 'd' / 'f').write_text('x')\n"
    )
    subprocess.run([sys.executable, "-c", script, str(tmp_path)], check=True, **plc._driver_options())
    assert stat.S_IMODE(os.stat(tmp_path / "d").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(tmp_path / "d" / "f").st_mode) == 0o600


def test_an_excerpt_of_output_ends_with_an_ellipsis_when_it_was_cut():
    assert plc._excerpt("short") == "short"
    assert plc._excerpt("x" * 200) == "x" * 200
    assert plc._excerpt("x" * 201) == "x" * 200 + "\u2026"


@pytest.mark.parametrize("make", [
    lambda output: plc.result_of(output),
    lambda output: plc.parse_json(output),
])
def test_a_value_cut_in_the_message_about_odd_output_is_found_by_the_scrub(make):
    """The message quotes the first 200 characters. A value that straddles the cut shows its start, and the ellipsis is what
    lets the scrub find that start."""
    value = "alex.tester@example.com"
    output = "x" * 190 + value
    with pytest.raises(plc.BrowserError) as raised:
        make(output)
    scrubbed = secretstore.Scrubber({"USER": {"value": value, "origins": ["https://example.com"]}}).scrub(str(raised.value))
    assert "alex.t" not in scrubbed and "[secret:USER]" in scrubbed


# --- the call log of a driver error on a seat that holds a secret (round 4, item 0) -------------------------------------
#
# Artemis's chain: a page renders JSON.stringify(JSON.stringify({user: input.value})) in a button, an overlay makes the
# click time out, and Playwright's error carries a preview of the button: the text of the element cut at 50 characters,
# with an ellipsis. The value is there under two layers of JSON and cut in two, which the scrub (a cut value in ONE
# layer, a whole value in several) cannot know. So the error of the driver is cut by structure, to its first line.

CHAIN_VALUE = 'Vault"Key\\qwerty1234567890qwertyuiop-Zx9'
CHAIN_SEAT = "chain-seat"


def _ansi(lines):
    """Playwright's call log, as playwright-cli prints it: each line in the codes of a dim colour."""
    return "".join(f"\x1b[2m{line}\x1b[22m\n" for line in lines)


def js_stringify(value):
    """`JSON.stringify(value)`: no space after a colon or a comma, and non-ASCII text as it is."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _preview_of_text(text, cap=50):
    """What Playwright makes of the text of an element in a preview (`trimStringWithEllipsis(text, 50)`)."""
    return text if len(text) <= cap else text[: cap - 1] + "…"


def overlay_error(value, banner="### Error\n"):
    """The error of the click that an overlay stops, for a page that shows `JSON.stringify(JSON.stringify({user: value}))`
    in the button. (The shape is the one captured from playwright-cli 0.1.22 on Chrome 155.)"""
    shown = _preview_of_text(js_stringify(js_stringify({"user": value})))
    return banner + "TimeoutError: Timeout 5000ms exceeded.\nCall log:\n" + _ansi([
        "  - waiting for locator('#b')",
        f'    - locator resolved to <button id="b">{shown}</button>',
        "  - attempting click action",
        "    2 × waiting for element to be visible, enabled and stable",
        "      - element is visible, enabled and stable",
        "      - scrolling into view if needed",
        "      - done scrolling",
        '      - <div id="overlay"></div> intercepts pointer events',
        "    - retrying click action",
        "    - waiting 20ms",
    ])


def strict_mode_error(value, locator="locator('.x')", pad=440):
    """A strict-mode violation: the first line names the locator the caller gave, and the list that follows previews each
    element, its attributes cut at 500 characters (here `pad` characters of other attributes come first)."""
    attrs = f' class="x" data-pad="{"p" * pad}" data-v="{js_stringify(js_stringify({"user": value}))}"'
    cut = attrs if len(attrs) <= 500 else attrs[:499] + "…"
    return (
        f"### Error\nError: locator.click: Error: strict mode violation: {locator} resolved to 2 elements:\n"
        f"    1) <button{cut}>One two three four</button> aka getByRole('button', {{ name: 'One two three four' }})\n"
        "    2) <button class=\"x\" data-v=\"beta\">Two</button> aka getByRole('button', { name: 'Two', exact: true })\n\n"
        "Call log:\n  - waiting for locator('.x')\n"
    )


def navigation_error(value, pad=150):
    """The wait for a navigation: the address of the page, cut at 200 characters, with the value in its query under two
    layers of escapes (after `pad` characters of path)."""
    from urllib.parse import quote

    address = "http://127.0.0.1:8080/" + "p" * pad + "?q=" + quote(quote(value, safe=""), safe="")
    shown = address if len(address) <= 200 else address[:199] + "…"
    return (
        "### Error\nTimeoutError: Timeout 5000ms exceeded.\nCall log:\n"
        + _ansi(["  - waiting for locator('#go')", f'  - waiting for "{shown}" navigation to finish...'])
    )


def _driver_says(monkeypatch, stdout, stderr="", code=1):
    monkeypatch.setattr(plc, "require", lambda: "playwright-cli")
    monkeypatch.setattr(plc.subprocess, "run", lambda *a, **k: _Completed(code, stdout, stderr))


def _error_of(seat, *args):
    with pytest.raises(plc.BrowserError) as raised:
        plc.run(seat, *args)
    return str(raised.value)


def _scrubber(value=CHAIN_VALUE):
    return secretstore.Scrubber({"USER": {"value": value, "origins": ["https://example.com"]}})


def _windows(value, size=8):
    """The stretches of `size` characters of the value, in the shapes it takes as it is, under one and two layers of JSON,
    and under one and two of a URL's percent escapes."""
    from urllib.parse import quote

    forms = {
        value, json.dumps(value)[1:-1], json.dumps(json.dumps(value))[1:-1], json.dumps(json.dumps(value)[1:-1])[1:-1],
        quote(value, safe=""), quote(quote(value, safe=""), safe=""),
    }
    return {form[at: at + size] for form in forms for at in range(max(1, len(form) - size))}


def _leaks(text, value=CHAIN_VALUE):
    return sorted(window for window in _windows(value) if window in text)


def test_the_fixture_of_the_chain_is_what_the_driver_printed():
    """The line of the preview, captured from playwright-cli for the button of the chain."""
    captured = (
        '\x1b[2m    - locator resolved to <button id="b">"{\\"user\\":\\"Vault\\\\\\"Key\\\\\\\\qwerty1234567890qwer…</button>\x1b[22m\n'
    )
    assert captured in overlay_error(CHAIN_VALUE)
    assert len(_preview_of_text(js_stringify(js_stringify({"user": CHAIN_VALUE})))) == 50


@pytest.mark.parametrize("make", [overlay_error, strict_mode_error, navigation_error])
def test_the_scrub_alone_leaves_the_start_of_a_value_that_is_cut_under_two_layers(make):
    """The positive control: the old code scrubbed the error of the driver and kept all of it, and the preview showed
    the start of the value, 30 characters of it. A cut value is found in one layer, a whole value in several, and a cut
    value in two layers is neither. (The reason the error is cut by structure, and the scrub is not made to know it.)
    The same for the list of a strict-mode violation (attributes cut at 500) and the wait for a navigation (the address
    cut at 200)."""
    left = _scrubber().scrub(make(CHAIN_VALUE))
    assert _leaks(left), "the scrub does not know a value that is cut under two layers"
    if make is overlay_error:
        assert "qwerty1234567890qwer" in left and len(_leaks(left)) > 10


@pytest.mark.parametrize("make", [overlay_error, strict_mode_error, navigation_error])
@pytest.mark.parametrize("branch", ["nonzero-exit", "error-section"])
def test_a_driver_error_on_a_seat_that_holds_a_secret_is_its_kind_and_a_note(monkeypatch, make, branch):
    """The kind of the error is all that stays; every line of the driver, with its previews and lists, is withheld.
    Both ways the driver reports a failure (a non-zero exit, an error block on exit 0) get the same."""
    raw = make(CHAIN_VALUE)
    if branch == "nonzero-exit":
        _driver_says(monkeypatch, raw, code=1)
    else:
        _driver_says(monkeypatch, raw + "\n### Page\n- Page URL: x\n", code=0)
    with plc.protecting(CHAIN_SEAT, _scrubber()):
        message = _error_of(CHAIN_SEAT, "click", "e2")
    kind = "TimeoutError" if raw.splitlines()[1].startswith("TimeoutError") else "Error"
    expected_lines = ([] if branch == "error-section" else ["### Error"]) + [f"{kind}: {plc.DRIVER_MESSAGE_WITHHELD}"]
    assert message.splitlines() == expected_lines
    assert _leaks(message) == [] and "intercepts pointer events" not in message and "Call log" not in message


def test_a_strict_mode_violation_shows_no_locator_and_no_element(monkeypatch):
    _driver_says(monkeypatch, strict_mode_error(CHAIN_VALUE, "locator('.pick-me')"))
    with plc.protecting(CHAIN_SEAT, _scrubber()):
        message = _error_of(CHAIN_SEAT, "click", "e2")
    assert message.splitlines()[-1] == f"Error: {plc.DRIVER_MESSAGE_WITHHELD}"
    assert "pick-me" not in message and "<button" not in message and "aka getByRole" not in message


def frame_error(value):
    """A selector that resolved to the wrong element: Playwright names that element, with its preview, in the FIRST line
    of the error (Artemis's probe at b217177: `wait-for "#debug >> internal:control=enter-frame >> button"` on a page
    that shows JSON of JSON of the value in a button)."""
    shown = _preview_of_text(js_stringify(js_stringify({"user": value})))
    return (
        "### Error\nError: page.waitForSelector: Error: Selector \"#debug\" resolved to "
        f'<button id="debug">{shown}</button>, <iframe> was expected\nCall log:\n'
        + _ansi(["  - waiting for locator('#debug').contentFrame().locator('button') to be visible"])
    )


def test_a_preview_in_the_first_line_of_an_error_is_withheld_and_the_first_line_alone_leaks(monkeypatch):
    _driver_says(monkeypatch, frame_error(CHAIN_VALUE))
    with plc.protecting(CHAIN_SEAT, _scrubber()):
        message = _error_of(CHAIN_SEAT, "wait-for", "#debug")
    assert message == f"### Error\nError: {plc.DRIVER_MESSAGE_WITHHELD}" and _leaks(message) == []
    first_line = frame_error(CHAIN_VALUE).splitlines()[1]
    assert len(_leaks(first_line)) > 10, "the control: keeping the first line, as b217177 did, leaks the start of the value"


@pytest.mark.parametrize("make", [overlay_error, strict_mode_error, navigation_error])
@pytest.mark.parametrize("code", [0, 1])
def test_a_seat_that_holds_no_secret_keeps_the_whole_driver_error(monkeypatch, make, code):
    raw = make(CHAIN_VALUE)
    _driver_says(monkeypatch, raw, code=code)
    message = _error_of("a-seat-with-no-secret", "click", "e2")
    assert message == (raw.strip() if code else raw.split("\n", 1)[1].strip()), "the header is the driver's only on a non-zero exit"
    assert "Call log" in message and "\u2026" in message
    with plc.protecting("a-seat-with-no-secret", secretstore.Scrubber({})):
        assert _error_of("a-seat-with-no-secret", "click", "e2") == message, "a scrubber with no value protects nothing"


def test_a_value_that_a_script_hid_withholds_the_call_log_from_then_on(monkeypatch):
    _driver_says(monkeypatch, overlay_error("anything"))
    scrubber = secretstore.Scrubber({})
    with plc.protecting(CHAIN_SEAT, scrubber):
        assert "Call log" in _error_of(CHAIN_SEAT, "click", "e2")
        scrubber.hide("CODE", "48291657")
        message = _error_of(CHAIN_SEAT, "click", "e2")
    assert "Call log" not in message and message.endswith(plc.DRIVER_MESSAGE_WITHHELD)
    assert "Call log" in _error_of(CHAIN_SEAT, "click", "e2"), "the script is over"


def test_another_seat_is_not_held_to_the_secrets_of_this_one(monkeypatch):
    _driver_says(monkeypatch, overlay_error("anything"))
    with plc.protecting(CHAIN_SEAT, _scrubber()):
        assert "Call log" in _error_of("someone-else", "click", "e2")


@pytest.mark.parametrize("text", [
    "### Error\nError: Ref e99 not found in the current page snapshot.",
    "### Error\nError: net::ERR_UNSAFE_PORT at http://127.0.0.1:1/\n\n",
    "playwright-cli failed",
    "Error: boom",
])
def test_an_error_of_one_line_is_withheld_too_on_a_seat_that_holds_a_secret(monkeypatch, text):
    """A short error can name an element as well; only its kind stays."""
    _driver_says(monkeypatch, text)
    with plc.protecting(CHAIN_SEAT, _scrubber()):
        message = _error_of(CHAIN_SEAT, "click", "e2")
    header = ["### Error"] if text.startswith("### Error") else []
    assert message.splitlines() == header + [f"Error: {plc.DRIVER_MESSAGE_WITHHELD}"]


def test_the_error_of_the_chain_is_clean_in_the_transcript_of_a_run_and_leaks_in_the_old_code(monkeypatch):
    """Artemis's chain end to end, below the browser: the error goes through `plc.run` and into the transcript of a run,
    which scrubs it. With the first line only, nothing of the value is there. With the old code (the error whole, then
    the scrub) 30 characters of it are: the control."""
    from browser import commands

    _driver_says(monkeypatch, overlay_error(CHAIN_VALUE))
    scrubber = _scrubber()

    def transcript():
        run = commands.Run(CHAIN_SEAT, scrubber)
        with plc.protecting(CHAIN_SEAT, scrubber):
            try:
                plc.run(CHAIN_SEAT, "click", "e2")
            except plc.BrowserError as exc:
                run.fail("click #b", str(exc))
        return run.transcript()

    clean = transcript()
    assert _leaks(clean) == [] and f"TimeoutError: {plc.DRIVER_MESSAGE_WITHHELD}" in clean and "Timeout 5000ms" not in clean
    monkeypatch.setattr(plc, "driver_error", lambda seat, text: text)  # the old code
    old = transcript()
    assert "qwerty1234567890qwer" in old and len(_leaks(old)) > 10, "the old code leaks the start of the value"


@pytest.mark.parametrize("make", [
    lambda seat: plc.result_of("x" * 190 + CHAIN_VALUE, seat),
    lambda seat: plc.parse_json("x" * 190 + CHAIN_VALUE, seat),
])
def test_the_message_about_odd_output_quotes_nothing_on_a_seat_that_holds_a_secret(make):
    """The tool's own cut of 200 characters is a cut before the scrub as well: a value under two layers that it cut in two
    is not one the scrub finds. So the message names the fact and not the text."""
    twice = json.dumps(json.dumps(CHAIN_VALUE))[1:-1]
    with pytest.raises(plc.BrowserError) as plain:
        make(CHAIN_SEAT)
    assert "x" * 100 in str(plain.value), "with no secret held, the message quotes the text as it did"
    with plc.protecting(CHAIN_SEAT, _scrubber()):
        with pytest.raises(plc.BrowserError) as held:
            make(CHAIN_SEAT)
        with pytest.raises(plc.BrowserError) as anywhere:
            make(None)  # no seat known: any seat that holds a secret closes it
    for raised in (held, anywhere):
        assert plc.EXCERPT_WITHHELD in str(raised.value) and "xxxx" not in str(raised.value) and twice[:20] not in str(raised.value)


def test_the_message_about_a_screenshot_that_wrote_nothing_quotes_nothing_on_a_seat_that_holds_a_secret(monkeypatch, tmp_path):
    monkeypatch.setattr(plc, "require", lambda: "playwright-cli")
    monkeypatch.setattr(plc.subprocess, "run", lambda *a, **k: _Completed(0, "### Result\n" + "y" * 50 + CHAIN_VALUE))
    with pytest.raises(plc.BrowserError, match="yyyy"):
        plc.screenshot(CHAIN_SEAT, str(tmp_path / "none.png"))
    with plc.protecting(CHAIN_SEAT, _scrubber()):
        with pytest.raises(plc.BrowserError) as held:
            plc.screenshot(CHAIN_SEAT, str(tmp_path / "none.png"))
    assert plc.EXCERPT_WITHHELD in str(held.value) and "yyyy" not in str(held.value)


def test_the_message_about_odd_output_of_a_seat_is_closed_by_its_own_secrets_and_not_by_those_of_another(monkeypatch):
    """`evaluate` names its seat: the output that is not a result is quoted when the seat holds no secret, whatever another
    seat holds, and withheld when it does."""
    monkeypatch.setattr(plc, "require", lambda: "playwright-cli")
    monkeypatch.setattr(plc.subprocess, "run", lambda *a, **k: _Completed(0, "odd output " + "z" * 30))
    with plc.protecting("the-other-seat", _scrubber()):
        with pytest.raises(plc.BrowserError, match="zzzz"):
            plc.evaluate("no-secret-seat", "1 + 1")
        with plc.protecting("the-seat-that-holds-one", _scrubber()):
            with pytest.raises(plc.BrowserError) as held:
                plc.evaluate("the-seat-that-holds-one", "1 + 1")
    assert plc.EXCERPT_WITHHELD in str(held.value) and "zzzz" not in str(held.value)


def test_seats_the_driver_would_give_one_socket_get_different_sessions():
    # playwright-cli turns every character outside letters, digits and `-` into `-` in the socket name.
    seats = ["a_b", "a-b", "a.b", "A-B", "a b", "a--b", "a_b_", "a-b-"]
    names = [plc.session_name(seat) for seat in seats]
    assert len(set(names)) == len(seats)
    assert len({name.lower() for name in names}) == len(seats)


def test_a_session_name_is_stable_and_safe_for_a_socket():
    assert plc.session_name("garmin") == plc.session_name("garmin")
    for seat in ["garmin", "a_b", "UPPER.case", "x" * 300, "../..", "", "naïve"]:
        name = plc.session_name(seat)
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) and len(name) <= 40


def test_is_open_asks_for_the_session_of_the_seat_not_of_a_lookalike(monkeypatch):
    monkeypatch.setattr(plc, "sessions", lambda: [plc.session_name("a_b")])
    assert plc.is_open("a_b")
    assert not plc.is_open("a-b")
    assert not plc.is_open("a.b")


def test_a_state_root_too_long_for_a_socket_gets_a_short_private_directory(monkeypatch, tmp_path):
    long_root = tmp_path / ("d" * 80)
    monkeypatch.setenv("XDG_DATA_HOME", str(long_root))
    run = tempfile.mkdtemp(prefix="r", dir="/tmp")
    monkeypatch.setenv("XDG_RUNTIME_DIR", run)
    path = plc.ipc_dir()
    assert os.path.dirname(path) == os.path.join(run, f"ar3-browser-{os.geteuid()}")
    assert len(os.fsencode(path)) <= plc.IPC_DIR_MAX_BYTES
    assert plc.private_ipc_dir() == path
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o700
    monkeypatch.setenv("XDG_DATA_HOME", str(long_root) + "x")
    assert plc.ipc_dir() != path
    shutil.rmtree(run)


def test_a_runtime_directory_too_long_too_falls_back_to_the_system_temp_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ("d" * 80)))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / ("r" * 80)))
    path = plc.ipc_dir()
    assert path.startswith(tempfile.gettempdir()) and len(os.fsencode(path)) <= plc.IPC_DIR_MAX_BYTES


@pytest.mark.skipif(os.name != "posix", reason="owners are POSIX here")
def test_a_short_directory_another_user_made_first_stops_the_driver(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ("d" * 80)))
    run = tempfile.mkdtemp(prefix="r", dir="/tmp")
    monkeypatch.setenv("XDG_RUNTIME_DIR", run)
    os.mkdir(os.path.join(run, f"ar3-browser-{os.geteuid()}"), 0o777)
    real = os.lstat
    def foreign(path, *a, **k):
        info = real(path, *a, **k)
        if os.path.basename(path).startswith("ar3-browser-"):
            return os.stat_result((info.st_mode, info.st_ino, info.st_dev, info.st_nlink, os.geteuid() + 1, *info[5:]))
        return info
    monkeypatch.setattr(plc.os, "lstat", foreign)
    try:
        with pytest.raises(plc.BrowserError, match="socket directory"):
            plc.private_ipc_dir()
    finally:
        shutil.rmtree(run)
