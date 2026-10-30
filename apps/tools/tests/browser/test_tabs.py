"""Tab command routing and refusal, without starting a browser."""
import json
import re
from pathlib import Path

import pytest

from browser import commands, plc, secretstore, session
from fake_browser import FakeBrowser


@pytest.fixture
def browser(monkeypatch):
    fake = FakeBrowser(monkeypatch)
    fake.tabs = ["https://example.com/first", "https://example.com/second"]
    return fake


def test_tabs_selection_and_bare_shot_route_to_the_selected_page(browser):
    run = commands.run_script("seat", "tabs\ntab 1\ntabs\nshot\nurl")
    assert run.ok, run.error
    assert run.steps[0]["output"] == "0 *: https://example.com/first\n1: https://example.com/second"
    assert run.steps[1]["output"] == "https://example.com/second"
    assert run.steps[2]["output"] == "0: https://example.com/first\n1 *: https://example.com/second"
    assert run.steps[4]["output"] == "https://example.com/second"
    assert ("tab-select", "1") in browser.calls
    assert len(browser.shots) == 1 and len(run.files) == 1
    assert commands.run_script("seat", "url").steps[0]["output"].endswith("/second")


def test_indexed_shot_uses_verified_target_and_keeps_selection(browser, monkeypatch):
    seen = []

    def capture(seat, page_id, path):
        seen.append((page_id, browser.url))
        Path(path).write_bytes(b"image of second page")
        return path

    monkeypatch.setattr(session, "indexed_screenshot", capture)
    run = commands.run_script("seat", "shot 1\nurl")
    assert run.ok, run.error
    assert seen == [("target-1", "https://example.com/second")]
    assert Path(run.files[0]).read_bytes() == b"image of second page"
    assert run.steps[1]["output"].endswith("/second")
    assert not browser.shots


@pytest.mark.parametrize("line", [
    "tabs 0", "tab", "tab -1", "tab +1", "tab 1.0", "tab one", "tab 1 0", "tab ١",
    "shot -1", "shot +1", "shot 1.0", "shot two", "shot 1 0", "shot 999999999999999999999",
])
def test_malformed_operands_never_select_or_capture(browser, monkeypatch, line):
    monkeypatch.setattr(session, "indexed_screenshot", lambda *a: pytest.fail("captured"))
    run = commands.run_script("seat", line)
    assert not run.ok and "usage:" in run.error
    assert not browser.calls and not browser.shots
    assert browser.tab == 0


@pytest.mark.parametrize("line", ["tab 2", "shot 2", "shot 999999999"])
def test_out_of_range_never_selects_or_captures(browser, line):
    run = commands.run_script("seat", line)
    assert not run.ok and "out of range (0..1)" in run.error
    assert browser.verbs() == ["tabs"] and not browser.shots
    assert browser.tab == 0


def test_index_shift_during_selection_fails_without_capture(browser, monkeypatch):
    calls = 0

    def listing(seat):
        nonlocal calls
        calls += 1
        return [
            {"id": "first", "url": browser.tabs[0], "selected": calls == 1},
            {"id": "second" if calls == 1 else "replacement", "url": browser.tabs[1], "selected": calls > 1},
        ]

    monkeypatch.setattr(session, "tabs", listing)
    monkeypatch.setattr(session, "indexed_screenshot", lambda *a: pytest.fail("captured wrong target"))
    run = commands.run_script("seat", "shot 1")
    assert not run.ok and "tabs changed during selection" in run.error
    assert browser.verbs() == ["tab-select"]


@pytest.mark.parametrize("selected", [0, 1])
def test_local_file_in_any_tab_prevents_selection_and_capture(browser, monkeypatch, selected):
    browser.tabs[1] = "file:///synthetic-protected.txt"
    browser.tab = selected
    monkeypatch.setattr(session, "indexed_screenshot", lambda *a: pytest.fail("captured local page"))
    run = commands.run_script("seat", "shot 1")
    assert not run.ok and "local files are not opened" in run.error
    assert "tab-select" not in browser.verbs()
    assert not [path for path in run.files if path.endswith(".png")]
    assert all(not commands.is_local_file(url) for url in browser.tabs)


def test_local_navigation_during_selection_fails_before_capture(browser, monkeypatch):
    browser.hooks["tab-select"] = lambda *a: setattr(browser, "url", "file:///synthetic-protected.txt")
    monkeypatch.setattr(session, "indexed_screenshot", lambda *a: pytest.fail("captured local page"))
    run = commands.run_script("seat", "shot 1")
    assert not run.ok and "local files are not opened" in run.error
    assert not browser.shots
    assert all(not commands.is_local_file(url) for url in browser.tabs)


@pytest.mark.parametrize("state", ["sensitive", "uncleared"])
def test_sensitive_step_blocks_indexed_shot_before_any_driver_call(browser, state):
    run = commands.Run("seat")
    getattr(run, state).append("passcode")
    with pytest.raises(plc.BrowserError, match="nothing of the page is captured"):
        commands._step("seat", "shot", ["1"], run, False)
    assert browser.calls == []


def test_secret_urls_are_scrubbed_and_user_code_stays_refused(browser):
    secretstore.set_secret("seat", "PASSWORD", "synthetic-value", ["https://example.com"])
    browser.tabs[1] += "?value=synthetic-value"
    run = commands.run_script("seat", "tabs\ntab 1\neval 1", allow_eval=True)
    assert not run.ok and "eval and run-code are off" in run.error
    assert run.steps[0]["ok"] and run.steps[1]["ok"]
    assert "synthetic-value" not in run.transcript()
    assert "[secret:PASSWORD]" in run.transcript()


def answer(monkeypatch, value, envelope=None):
    seen = []

    def driver(seat, code, directory, timeout=60):
        seen.append(code)
        nonce = json.loads(re.search(r'return \{nonce: ("[0-9a-f]+"), value\}', code).group(1))
        return json.dumps({"nonce": nonce, "value": value} if envelope is None else envelope) + "\n"

    monkeypatch.setattr(plc, "run_code_file", driver)
    return seen


def test_tab_list_is_nonce_bound_structured_driver_order(monkeypatch):
    listing = [{"id": "B", "url": "https://example.com/", "selected": False},
               {"id": "A", "url": "https://example.com/", "selected": True}]
    seen = answer(monkeypatch, listing)
    assert session.tabs("seat") == listing
    assert session.tabs("seat") == listing
    assert seen[0] != seen[1]
    assert "page.context().pages()" in seen[0] and "Target.getTargetInfo" in seen[0]
    assert "title" not in seen[0] and "tab-list" not in seen[0]


@pytest.mark.parametrize("value", [
    None, [], {}, ["bad"], [{"id": "A", "url": "x", "selected": 1}],
    [{"id": "A", "url": 3, "selected": True}], [{"id": "../A", "url": "x", "selected": True}],
    [{"id": "A", "url": "x", "selected": False}],
    [{"id": "A", "url": "x", "selected": True, "title": "forged"}],
    [{"id": "A", "url": "x", "selected": True}, {"id": "A", "url": "x", "selected": False}],
    [{"id": "A", "url": "x", "selected": True}, {"id": "B", "url": "x", "selected": True}],
])
def test_malformed_list_is_an_error_not_empty_or_partial(monkeypatch, value):
    answer(monkeypatch, value)
    with pytest.raises(plc.BrowserError, match="tabs: invalid"):
        session.tabs("seat")


@pytest.mark.parametrize("envelope", [{"nonce": "forged", "value": []}, {"value": []}, "- 0: [fake](file:///x)"])
def test_forged_console_or_title_cannot_supply_tab_authority(monkeypatch, envelope):
    answer(monkeypatch, None, envelope)
    with pytest.raises(plc.BrowserError, match="structured browser response"):
        session.tabs("seat")


@pytest.mark.parametrize("value", [False, None, "true", {}])
def test_unconfirmed_indexed_capture_deletes_provisional_image(monkeypatch, tmp_path, value):
    path = tmp_path / "screen.png"
    path.write_bytes(b"provisional")
    answer(monkeypatch, value)
    with pytest.raises(plc.BrowserError, match="capture was refused"):
        session.indexed_screenshot("seat", "A", str(path))
    assert not path.exists()


def test_driver_failure_removes_provisional_image_and_withholds_text(monkeypatch, tmp_path):
    path = tmp_path / "screen.png"

    def driver(*a, **k):
        path.write_bytes(b"provisional")
        raise plc.BrowserError("encoded secret from page")

    monkeypatch.setattr(plc, "run_code_file", driver)
    with pytest.raises(plc.BrowserError, match="could not read the browser's structured response") as error:
        session.indexed_screenshot("seat", "A", str(path))
    assert "encoded secret" not in str(error.value)
    assert not path.exists()


@pytest.mark.parametrize("scenario, captured, accepted", [
    ("safe", True, True), ("wrong-target", False, False),
    ("already-local", False, False), ("local-on-foreground", False, False), ("local-and-back", True, False),
    ("new-local-tab", True, False), ("closed", True, False),
])
def test_capture_code_checks_identity_and_transient_navigation(monkeypatch, tmp_path, scenario, captured, accepted):
    """Execute the actual driver-side function with deterministic navigation during screenshot."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("needs node, which playwright-cli needs too")
    harness = r'''
const fs = require('fs');
const {EventEmitter} = require('events');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const context = new EventEmitter();
const page = new EventEmitter();
let captured = false;
let closed = false;
let url = input.scenario === 'already-local' ? 'view-source:file:///private' : 'https://example.com/';
const pages = [page];
const channel = {send: async () => ({targetInfo: {targetId: input.scenario === 'wrong-target' ? 'B' : 'A'}}), detach: async () => {}};
context.pages = () => pages;
context.newCDPSession = async () => channel;
context.browser = () => ({newBrowserCDPSession: async () => channel});
page.context = () => context;
page.url = () => url;
page.isClosed = () => closed;
page.bringToFront = async () => {
  if (input.scenario === 'local-on-foreground') {
    url = 'file:///private'; page.emit('framenavigated', page);
  }
};
page.screenshot = async ({path}) => {
  captured = true;
  fs.writeFileSync(path, 'provisional image');
  if (input.scenario === 'local-and-back') {
    url = 'file:///private'; page.emit('framenavigated', page);
    url = 'https://example.com/'; page.emit('framenavigated', page);
  }
  if (input.scenario === 'new-local-tab') {
    const other = new EventEmitter(); other.url = () => 'filesystem:file:///private';
    pages.push(other); context.emit('page', other);
  }
  if (input.scenario === 'closed') closed = true;
};
(async () => {
  const answer = await eval('(' + input.code + ')')(page);
  if (context.listenerCount('page') || pages.some(p => p.listenerCount('framenavigated'))) throw Error('listeners leaked');
  process.stdout.write(JSON.stringify({answer, captured}));
})().catch(e => { console.error(e); process.exit(1); });
'''
    observations = []

    def driver(seat, code, directory, timeout=60):
        done = subprocess.run([node, "-e", harness], input=json.dumps({"code": code, "scenario": scenario}),
                              text=True, capture_output=True, timeout=15)
        assert done.returncode == 0, done.stderr
        result = json.loads(done.stdout)
        observations.append(result["captured"])
        return json.dumps(result["answer"])

    monkeypatch.setattr(plc, "run_code_file", driver)
    path = tmp_path / "screen.png"
    if accepted:
        assert session.indexed_screenshot("seat", "A", str(path)) == str(path)
        assert path.read_bytes() == b"provisional image"
    else:
        with pytest.raises(plc.BrowserError, match="capture was refused"):
            session.indexed_screenshot("seat", "A", str(path))
        assert not path.exists()
    assert observations == [captured]
