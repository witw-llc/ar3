"""`a8s mcp serve` — handshake, tool listing, delivery, malformed frames.

The delivery test is the load-bearing one: it drives a real `tools/call`
through the server and reads the envelope `a8s tell` staged in a scratch
`TELL_OUTBOX_DIR`, asserting the body is byte-exact through the hazard
characters that a shell would eat ($ amounts, backticks, backslashes).
"""
from __future__ import annotations

import io
import json

import cli
import mcp_server


def _drive(*requests: dict) -> list[dict]:
    """Run the server over one stdin buffer; return the responses it wrote."""
    stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
    stdout = io.StringIO()
    assert mcp_server.serve(stdin, stdout) == 0
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


def _raw(payload: str) -> list[dict]:
    stdin = io.StringIO(payload)
    stdout = io.StringIO()
    assert mcp_server.serve(stdin, stdout) == 0
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
}
LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


class TestHandshake:
    def test_initialize_announces_server_a8s(self):
        (response,) = _drive(INITIALIZE)
        assert response["id"] == 1
        result = response["result"]
        assert result["protocolVersion"] == mcp_server.PROTOCOL_VERSION
        assert result["serverInfo"]["name"] == "a8s"
        assert "tools" in result["capabilities"]

    def test_initialized_notification_gets_no_response(self):
        responses = _drive(
            INITIALIZE,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            LIST,
        )
        assert [r["id"] for r in responses] == [1, 2]

    def test_stdin_close_exits_cleanly(self):
        assert _raw("") == []


class TestToolListing:
    def test_one_tool_named_tell(self):
        _init, listing = _drive(INITIALIZE, LIST)
        tools = listing["result"]["tools"]
        assert [t["name"] for t in tools] == ["tell"]

    def test_schema_takes_recipient_and_body_strings(self):
        (listing,) = _drive(LIST)
        schema = listing["result"]["tools"][0]["inputSchema"]
        assert schema["required"] == ["recipient", "body"]
        assert schema["properties"]["recipient"]["type"] == "string"
        assert schema["properties"]["body"]["type"] == "string"

    def test_model_facing_name_is_a8s_tell(self):
        assert mcp_server.QUALIFIED_TOOL_NAME == "a8s_tell"


HAZARDS = (
    "The refund is $1.25 and 100% of `whoami` stays literal; "
    "path C:\\temp\\x and $HOME and it's fine"
)


class TestDelivery:
    def _call(self, recipient: str, body: str) -> dict:
        (response,) = _drive(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "tell",
                    "arguments": {"recipient": recipient, "body": body},
                },
            }
        )
        return response["result"]

    def test_call_stages_a_byte_exact_envelope(self, fake_home, tmp_path, monkeypatch):
        outbox = tmp_path / "staging"
        outbox.mkdir()
        monkeypatch.setenv("TELL_OUTBOX_DIR", str(outbox))
        monkeypatch.chdir(tmp_path)

        result = self._call("BOB", HAZARDS)
        assert result["isError"] is False

        staged = sorted(outbox.glob("*.json"))
        assert len(staged) == 1
        envelope = json.loads(staged[0].read_text(encoding="utf-8"))
        assert envelope["to"] == "BOB"
        assert envelope["content"] == HAZARDS

    def test_call_logs_to_a8s_mcp_log(self, fake_home, tmp_path, monkeypatch):
        outbox = tmp_path / "staging"
        outbox.mkdir()
        log = tmp_path / "mcp-calls.jsonl"
        monkeypatch.setenv("TELL_OUTBOX_DIR", str(outbox))
        monkeypatch.setenv("A8S_MCP_LOG", str(log))
        monkeypatch.chdir(tmp_path)

        self._call("BOB", HAZARDS)

        (record,) = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        assert record == {"tool": "tell", "recipient": "BOB", "body": HAZARDS}

    def test_failed_delivery_reports_is_error(self, fake_home, tmp_path, monkeypatch):
        monkeypatch.delenv("TELL_OUTBOX_DIR", raising=False)
        monkeypatch.chdir(tmp_path)

        result = self._call("BOB", "anything")
        assert result["isError"] is True
        assert result["content"][0]["text"]


class TestMalformedRequests:
    def test_unparseable_line_is_skipped_and_the_session_continues(self):
        payload = "not json at all\n" + json.dumps(LIST) + "\n"
        (listing,) = _raw(payload)
        assert listing["result"]["tools"][0]["name"] == "tell"

    def test_blank_lines_and_non_objects_are_skipped(self):
        payload = "\n[1, 2, 3]\n" + json.dumps(LIST) + "\n"
        assert len(_raw(payload)) == 1

    def test_unknown_tool_is_an_error_result(self):
        (response,) = _drive(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "nope", "arguments": {}},
            }
        )
        assert response["result"]["isError"] is True
        assert "nope" in response["result"]["content"][0]["text"]

    def test_missing_arguments_are_an_error_result(self):
        (response,) = _drive(
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "tell"}}
        )
        assert response["result"]["isError"] is True
        assert "recipient" in response["result"]["content"][0]["text"]

    def test_empty_body_is_an_error_result(self):
        (response,) = _drive(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "tell", "arguments": {"recipient": "BOB", "body": ""}},
            }
        )
        assert response["result"]["isError"] is True
        assert "body" in response["result"]["content"][0]["text"]

    def test_unknown_method_gets_an_empty_result(self):
        (response,) = _drive({"jsonrpc": "2.0", "id": 6, "method": "ping"})
        assert response["result"] == {}


class TestCliSurface:
    def test_mcp_is_a_known_command(self):
        assert "mcp" in cli.KNOWN_COMMANDS
        assert "mcp serve" in cli.CLI_EPILOG

    def test_serve_dispatches_to_the_server(self, monkeypatch):
        monkeypatch.setattr(mcp_server, "serve", lambda: 0)
        assert cli.dispatch("mcp", ["serve"], interval=1.0) == 0

    def test_bad_verb_is_a_usage_error(self, capsys):
        assert cli.dispatch("mcp", ["wat"], interval=1.0) == 2
        assert "usage: a8s mcp serve" in capsys.readouterr().err
