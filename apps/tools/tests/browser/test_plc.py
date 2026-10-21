import pytest

from browser import plc


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
