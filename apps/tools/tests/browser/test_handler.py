from browser import commands
from browser import handler


def _capture(monkeypatch):
    sent = []

    def send(recipient, body, attachments=()):
        sent.append((recipient, body, list(attachments)))
        return 0

    monkeypatch.setattr(handler.rt, "send_reply", send)
    return sent


def _no_browser(monkeypatch):
    monkeypatch.setattr(
        commands, "run_script",
        lambda *a, **k: pytest_fail("the browser ran for an unlisted sender"),
    )


def test_an_empty_allowlist_accepts_nobody(monkeypatch):
    sent = _capture(monkeypatch)
    _no_browser(monkeypatch)
    assert handler.handle("garmin", "iris", "url", allow="") == 0
    assert sent == []


def test_the_allowlist_is_case_insensitive(monkeypatch):
    sent = _capture(monkeypatch)
    monkeypatch.setattr(commands, "run_script", lambda *a, **k: commands.Run("garmin"))
    assert handler.handle("garmin", "CODEX", "url", allow="Iris, codex") == 0
    assert len(sent) == 1


def test_a_refused_sender_gets_no_reply_and_the_wake_exits_zero(monkeypatch, capsys):
    sent = _capture(monkeypatch)
    _no_browser(monkeypatch)
    assert handler.handle("garmin", "stranger", "go https://example.com", allow="iris") == 0
    assert sent == []
    assert "refused" in capsys.readouterr().err


def test_a_reply_carries_the_transcript_and_the_artifacts(monkeypatch):
    run = commands.Run("garmin")
    run.note("url", "https://example.com/")
    run.attach("/tmp/shot.png")
    monkeypatch.setattr(commands, "run_script", lambda *a, **k: run)
    sent = _capture(monkeypatch)

    assert handler.handle("garmin", "iris", "url", allow="iris") == 0
    recipient, body, files = sent[0]
    assert recipient == "iris"
    assert body.startswith("garmin: ok")
    assert "https://example.com/" in body
    assert files == ["/tmp/shot.png"]


def test_a_failing_verb_still_gets_one_reply_and_exits_zero(monkeypatch):
    run = commands.Run("garmin")
    run.fail("click Submit", "nothing visible matching 'Submit'")
    monkeypatch.setattr(commands, "run_script", lambda *a, **k: run)
    sent = _capture(monkeypatch)
    assert handler.handle("garmin", "iris", "click Submit", allow="iris") == 0
    assert len(sent) == 1
    assert "failed" in sent[0][1]


def test_a_reply_that_cannot_be_sent_is_the_one_thing_that_exits_nonzero(monkeypatch):
    monkeypatch.setattr(commands, "run_script", lambda *a, **k: commands.Run("garmin"))
    monkeypatch.setattr(handler.rt, "send_reply", lambda *a, **k: 1)
    assert handler.handle("garmin", "iris", "url", allow="iris") == 1


def test_attachment_lines_are_not_part_of_the_script(monkeypatch):
    seen = []
    monkeypatch.setattr(
        commands, "run_script",
        lambda seat, body, allow_eval=False: seen.append(body) or commands.Run(seat),
    )
    _capture(monkeypatch)
    handler.handle("garmin", "iris", "url\n\nATTACHED FILE: /tmp/x", allow="iris")
    assert seen == ["url"]


def test_allow_eval_arg_overrides_the_environment(monkeypatch):
    monkeypatch.delenv("AR3_BROWSER_ALLOW_EVAL", raising=False)
    seen = []
    monkeypatch.setattr(
        commands, "run_script",
        lambda seat, body, allow_eval=False: seen.append(allow_eval) or commands.Run(seat),
    )
    _capture(monkeypatch)
    handler.handle("garmin", "iris", "url", allow="iris", allow_eval="1")
    handler.handle("garmin", "iris", "url", allow="iris", allow_eval="0")
    assert seen == [True, False]


def pytest_fail(message):
    raise AssertionError(message)
