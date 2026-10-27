"""Devin ACP using an existing login, with verified session controls.

Devin 3000.11.205 on Linux accepted initialize/session/new without authenticate;
its advertised devin-browser authentication would start login and is never used.
Idle stdin EOF exited 0 in 0.114 seconds. Busy EOF and Windows are unmeasured;
the adapter retains the suite's conservative 10-second quiet-exit allowance.
"""
from __future__ import annotations

from pathlib import Path

from engines.acp import AcpSession, ParsedFrame, acp_mcp_servers, rpc

PROVEN_VERSION = "3000.11.205"
PERMISSION_MODES = {None: "accept-edits", "ask": "ask", "auto": "smart", "bypass": "bypass"}
TELL_IDENTITY = {
    "cognition.ai/toolName": "mcp__a8s__tell",
    "cognition.ai/eventType": "mcp_tool_call",
    "cognition.ai/inferenceToolName": "mcp__a8s__tell",
}
# Retired IDs cannot be evicted and later replayed as new calls. Saturation
# disables grants for this session instead; no history is written to disk.
MAX_PERMISSION_CALLS = 256
MAX_PERMISSION_CALL_ID_LENGTH = 512


def build_template(model, effort, permissions) -> list[str]:
    from engines.run import RunError

    if permissions not in PERMISSION_MODES:
        raise RunError(f"devin resident has no permissions mode {permissions!r}")
    argv = ["devin", "acp", "--permission-mode", PERMISSION_MODES[permissions]]
    if model:
        argv.extend(["--model", model])
    if effort:
        argv.extend(["--effort", effort])
    return argv


def compose_argv(template: list[str], env: dict[str, str], mcp_log_path: Path | None = None) -> list[str]:
    from engines.run import RunError

    if len(template) < 2 or template[1] != "acp":
        raise RunError("devin resident needs a devin acp template")
    for key in ("MODE", "MODEL", "EFFORT"):
        env.pop("R4T_SESSION_" + key, None)
    env["R4T_SESSION_MODE"] = "accept-edits"
    keys = {"--permission-mode": "MODE", "--model": "MODEL", "--effort": "EFFORT"}
    rest = iter(template[2:])
    for flag in rest:
        if flag not in keys:
            raise RunError(f"devin resident cannot carry {flag!r} to devin acp")
        value = next(rest, None)
        if not value:
            raise RunError(f"devin resident {flag} needs a value")
        env["R4T_SESSION_" + keys[flag]] = value
    if env["R4T_SESSION_MODE"] not in PERMISSION_MODES.values():
        raise RunError("devin resident permission mode is not supported")
    if mcp_log_path is not None:
        env["A8S_MCP_LOG"] = str(mcp_log_path)
    return [template[0], "acp"]


def _select(result: dict, key: str) -> dict | None:
    options = result.get("configOptions")
    if not isinstance(options, list):
        return None
    return next((x for x in options if isinstance(x, dict) and x.get("id") == key
                 and x.get("type") == "select"), None)


class DevinSession(AcpSession):
    def __init__(self, argv, dir_path, env=None):
        super().__init__(argv, dir_path, env)
        self.mode = self.env.get("R4T_SESSION_MODE", "accept-edits")
        self.model_value = None
        self.pins = [("mode", self.mode)]
        if self.env.get("R4T_SESSION_MODEL"):
            self.pins.append(("model", self.env["R4T_SESSION_MODEL"]))
        if self.env.get("R4T_SESSION_EFFORT"):
            self.pins.append(("thought_level", self.env["R4T_SESSION_EFFORT"]))
        self.expected = {}
        self.pin_id = None
        self.settled = False
        self.tell_calls: dict[str, int | None] = {}
        self.tell_turn: int | None = None
        self.tell_saturated = False

    def mcp_servers(self):
        return acp_mcp_servers(self.env)

    def _active_tell_turn(self):
        return (self.ready and self.busy and self.session_id and not self.cancel_sent
                and self.tell_turn is not None and self.tell_turn in self.turn_requests)

    @staticmethod
    def _call_id(call):
        call_id = call.get("toolCallId") if isinstance(call, dict) else None
        return (call_id if isinstance(call_id, str)
                and 0 < len(call_id) <= MAX_PERMISSION_CALL_ID_LENGTH else None)

    def _remember_call(self, call_id):
        if self.tell_saturated or call_id in self.tell_calls:
            return False
        if len(self.tell_calls) >= MAX_PERMISSION_CALLS:
            self.tell_calls.clear()
            self.tell_saturated = True
            return False
        self.tell_calls[call_id] = None
        return True

    @staticmethod
    def _identity_conflicts(call):
        meta = call.get("_meta")
        return (("_meta" in call and not isinstance(meta, dict))
                or (isinstance(meta, dict) and any(k in meta and meta[k] != v
                                                  for k, v in TELL_IDENTITY.items())))

    def _retire_tell_turn(self):
        self.tell_turn = None
        for call_id in self.tell_calls:
            self.tell_calls[call_id] = None

    def _prompt(self, text):
        self._retire_tell_turn()
        self.tell_turn = self.next_id
        return super()._prompt(text)

    def cancel_line(self):
        line = super().cancel_line()
        with self.lock:
            self._retire_tell_turn()
        return line

    def _fail(self, text):
        self._retire_tell_turn()
        return super()._fail(text)

    def _on_update(self, frame):
        parsed = super()._on_update(frame)
        if parsed.fatal or self.mode != "accept-edits":
            return parsed
        update = frame["params"].get("update") or {}
        event = update.get("sessionUpdate")
        if event not in ("tool_call", "tool_call_update"):
            return parsed
        call_id = self._call_id(update)
        if call_id is None:
            return parsed
        first = self._remember_call(call_id)
        if self.tell_saturated:
            return parsed
        if not self._active_tell_turn():
            self.tell_calls[call_id] = None
            return parsed
        meta = update.get("_meta")
        if event == "tool_call":
            self.tell_calls[call_id] = (
                self.tell_turn if first and isinstance(meta, dict)
                and all(meta.get(k) == v for k, v in TELL_IDENTITY.items()) else None
            )
        elif self._identity_conflicts(update):
            self.tell_calls[call_id] = None
        if update.get("status") not in (None, "pending", "in_progress"):
            self.tell_calls[call_id] = None
        return parsed

    def allows_permission(self, params):
        if self.mode == "bypass":
            return self.ready
        call = params.get("toolCall")
        call_id = self._call_id(call)
        if call_id is None:
            return False
        allowed = (self.mode == "accept-edits" and self._active_tell_turn()
                   and params.get("sessionId") == self.session_id
                   and not self.tell_saturated and not self._identity_conflicts(call)
                   and call.get("status") in (None, "pending", "in_progress")
                   and self.tell_calls.get(call_id) == self.tell_turn)
        self._remember_call(call_id)
        if not self.tell_saturated:
            self.tell_calls[call_id] = None
        return bool(allowed)

    def _next_pin(self, result):
        for key, value in self.expected.items():
            select = _select(result, key)
            if select is None or select.get("currentValue") != value:
                return self._fail(f"devin {key} pin was not confirmed as {value!r}")
        model = _select(result, "model")
        if model:
            self.model_value = model.get("currentValue")
        if not self.pins:
            self.settled = True
            return self._maybe_ready()
        key, value = self.pins.pop(0)
        select = _select(result, key)
        options = select.get("options") if select else None
        if not isinstance(options, list) or not any(
            isinstance(x, dict) and x.get("value") == value for x in options
        ):
            return self._fail(f"devin catalog has no {key} value {value!r}")
        self.expected[key] = value
        self.pin_id = self.next_id
        self.next_id += 1
        return ParsedFrame(replies=(rpc({
            "id": self.pin_id, "method": "session/set_config_option",
            "params": {"sessionId": self.session_id, "configId": key, "value": value},
        }),))

    def after_session_new(self, result):
        return self._next_pin(result)

    def on_extra_response(self, frame):
        if self.pin_id is None or frame.get("id") != self.pin_id:
            return None
        self.pin_id = None
        if frame.get("error") is not None:
            return self._fail(f"devin config pin failed: {frame['error']}")
        result = frame.get("result")
        if not isinstance(result, dict):
            return self._fail("devin config pin returned no result")
        return self._next_pin(result)

    def _on_prompt_done(self, frame, message):
        self._retire_tell_turn()
        result = frame.get("result")
        stop = result.get("stopReason") if isinstance(result, dict) else None
        failed = frame.get("error") is not None or (
            stop in ("refusal", "cancelled") and not self.cancel_sent
        )
        if not failed:
            return super()._on_prompt_done(frame, message)
        self.turn_requests.discard(frame.get("id"))
        self.busy = False
        self.chunks = []
        self.ready = False
        self.error = (f"devin session/prompt failed: {message}"
                      if frame.get("error") is not None else f"devin turn {stop}")
        return ParsedFrame(error_text=self.error, turn_completed=True,
                           turn_failed=True, fatal=True)


def unused_encode(_text):
    raise RuntimeError("devin turns go through DevinSession.turn_lines")


def unused_parse(_frame):
    raise RuntimeError("devin frames go through DevinSession.on_frame")
