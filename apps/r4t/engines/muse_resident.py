"""Muse's MSP resident session, measured on Muse Code 1.4.13."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import replace
from pathlib import Path

from ar3.proc import launch_options

from engines.acp import ParsedFrame, rpc
from engines.held_input import submission_text
from engines.muse import _uuid7
from engines.run import RunError
from rig import MCP_SERVER_NAME, _mcp_command, _mcp_server_env

TELL_TOOL = "mcp__a8s__tell"
HUMAN_AXES = {"filesystem": "sandboxed", "network": "off",
              "approval": "onRequest", "reviewer": "human"}
# Saturation disables grants instead of evicting identities that could be replayed.
MAX_PERMISSION_CALLS = 256
MAX_PERMISSION_CALL_ID_LENGTH = 512


def compose_muse_argv(template, env, mcp_log_path=None):
    if len(template) < 2 or template[1] != "exec":
        raise RunError("muse resident needs a `muse exec` template")
    argv = [template[0], "serve", "--disable-request-user-input"]
    mode = "onRequest"
    grant_tell = False
    env.pop("R4T_SESSION_TELL_GRANT", None)
    i = 2
    while i < len(template):
        token = template[i]
        if token in ("{prompt}", "--user-input-auto-resolve"):
            i += 1
        elif token == "--yolo":
            mode = "allowAll"
            argv += ["--disable-sandbox", "--trust-workspace"]
            i += 1
        elif token in ("--model", "--reasoning-effort", "--approval-mode") and i + 1 < len(template):
            value = template[i + 1]
            if token == "--model":
                env["R4T_SESSION_MODEL"] = value
            elif token == "--reasoning-effort":
                env["R4T_SESSION_EFFORT"] = value
            elif value == "never":
                grant_tell = True
            else:
                raise RunError(f"muse resident cannot carry approval mode {value!r}")
            i += 2
        else:
            raise RunError(f"muse resident cannot carry {token!r} to `muse serve`")
    env["R4T_SESSION_APPROVAL"] = mode
    if grant_tell and mode == "onRequest":
        env["R4T_SESSION_TELL_GRANT"] = "1"
    if mcp_log_path is not None:
        env["A8S_MCP_LOG"] = str(mcp_log_path)
    return argv


class MuseSession:
    def __init__(self, argv, dir_path: Path, env):
        self.argv = list(argv)
        self.cwd = str(dir_path.resolve())
        self.env = dict(env)
        self.mode = self.env.get("R4T_SESSION_APPROVAL", "onRequest")
        self.restricted = self.mode != "allowAll"
        self.grant_tell = self.restricted and self.env.get("R4T_SESSION_TELL_GRANT") == "1"
        self.human_policy_verified = False
        self.tell_calls = {}
        self.tell_items = {}
        self.tell_approvals = {}
        self.tell_saturated = False
        self.model = self.env.get("R4T_SESSION_MODEL")
        self.effort = self.env.get("R4T_SESSION_EFFORT")
        self.session_id = None
        self.turn_id = None
        self.turn_started = False
        self.command_id = None
        self.ready = self.handshake_done = self.busy = False
        self.cancel_sent = False
        self.closing = False
        self.held = deque()
        self.lock = threading.Lock()
        self.next_id = 2
        self.pending = {1: ("initialize", None)}
        self.items = {}
        self.approval_decisions = set()
        self.approvals_reviewing = set()
        self.approvals_resolved = set()
        self.user_inputs_cancelled = set()
        self.error = None
        self.started = time.monotonic()
        self.version = None

    def opening_lines(self):
        capabilities = {"requestedCapabilities": ["sessionMcp"]}
        if self.restricted:
            capabilities["experimentalApi"] = True
        return [rpc({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "r4t", "version": "0"},
            "capabilities": capabilities,
        }})]

    def deadline_error(self):
        if not self.handshake_done and time.monotonic() - self.started >= 20:
            return "muse MSP handshake was not ready after 20s"
        return None

    def _request(self, method, params, *, command=False):
        rid = self.next_id
        self.next_id += 1
        if command:
            params = {"commandId": _uuid7(), **params}
        self.pending[rid] = (method, params.get("commandId"))
        return rpc({"id": rid, "method": method, "params": params})

    def _start(self):
        command = _mcp_command()
        params = {"workspaceRoot": self.cwd, "approvalMode": self.mode,
                  "config": {"mcpServers": {MCP_SERVER_NAME: {
                      "transport": "stdio", "command": command[0], "args": command[1:],
                      "env": _mcp_server_env(self.env), "mode": "required",
                      "framing": "lineDelimitedJson",
                  }}}}
        if self.model:
            params["modelId"] = self.model
        if self.restricted:
            params["permissionProfile"] = ":ask-me"
        return self._request("session/start", params, command=True)

    def _prompt(self, text):
        self._retire_tell_calls()
        params = {"sessionId": self.session_id, "commandId": _uuid7(),
                  "input": [{"type": "text", "text": text}]}
        if self.effort:
            params["reasoningEffort"] = self.effort
        self.command_id = params["commandId"]
        self.turn_id = None
        self.turn_started = False
        self.items = {}
        self.busy = True
        return self._request("turn/start", params)

    def _release(self):
        if self.ready and not self.busy and self.held and not self.cancel_sent:
            return (self._prompt(submission_text(self.held.popleft())),)
        return ()

    def turn_lines(self, text):
        with self.lock:
            if not self.ready or self.busy:
                self.held.append(text)
                return []
            return [self._prompt(text)]

    def cancel_line(self):
        with self.lock:
            if not self.busy or not self.turn_id or not self.turn_started:
                return None
            self.cancel_sent = True
            self._retire_tell_calls()
            return self._request("turn/cancel", {"sessionId": self.session_id,
                                                  "turnId": self.turn_id}, command=True)

    def begin_shutdown(self):
        """Arm the orderly host-unload notification only for settled EOF."""
        with self.lock:
            self.closing = (self.ready and self.handshake_done and not self.error
                            and not self.busy and not self.held)

    def _fail(self, message):
        self.human_policy_verified = False
        self._retire_tell_calls()
        self.error = message
        self.ready = False
        return ParsedFrame(error_text=message, fatal=True)

    def on_frame(self, frame):
        with self.lock:
            if self.error:
                return self._fail(self.error)
            try:
                return self._on_frame(frame)
            except (TypeError, ValueError, KeyError, AttributeError) as exc:
                return self._fail(f"muse MSP malformed frame: {type(exc).__name__}")

    def _on_frame(self, frame):
        if not isinstance(frame, dict):
            return self._fail("muse MSP frame was not an object")
        if "method" not in frame:
            return self._response(frame)
        method, params = frame["method"], frame.get("params")
        if "id" in frame and method not in ("approval/request", "userInput/request"):
            return ParsedFrame(replies=(rpc({"id": frame["id"], "error": {
                "code": -32601, "message": "r4t does not serve client requests",
            }}),))
        if not isinstance(params, dict):
            return self._fail("muse MSP event params were not an object")
        if params.get("sessionId") != self.session_id:
            if "id" in frame:
                return ParsedFrame(replies=(rpc({"id": frame["id"], "error": {
                    "code": -32602, "message": "request belongs to another session",
                }}),))
            return ParsedFrame()
        if self.restricted:
            if (method == "session/permissionProfileChanged"
                    or (method == "session/approvalModeChanged" and params.get("mode") != self.mode)):
                return self._fail("muse restricted session permission policy changed")
            if (method == "approval/reviewing"
                    or (method == "approval/updated"
                        and (params.get("change") or {}).get("kind") in ("reviewStarted", "reviewCompleted"))
                    or (method == "approval/resolved" and params.get("resolvedBy") == "llmJudge")):
                return self._fail("muse restricted session unexpectedly used automated review")
        if method in ("item/started", "item/updated", "item/completed") and self.grant_tell:
            self._observe_tell_item(params.get("item"), method == "item/completed",
                                    "subagentOrigin" in params)
        if "id" in frame:
            parsed = (self._approval(params) if method == "approval/request"
                      else self._user_input(params))
            # Presentation receipt and decision are separate MSP messages.
            return replace(parsed, replies=(rpc({"id": frame["id"], "result": {}}),
                                             *parsed.replies))
        if method == "session/closed":
            if (params.get("reason") == "hostShutdown" and self.closing
                    and self.handshake_done and not self.busy and not self.held):
                self.ready = False
                return ParsedFrame()
            return self._fail(f"muse session closed: {params.get('reason', 'unknown')}")
        if method == "view/gap":
            return self._fail("muse MSP lost session events; turn outcome is unavailable")
        if (method == "turn/unqueued" and self.busy
                and params.get("commandId") == self.command_id):
            turn_id = params.get("turnId")
            if not isinstance(turn_id, str) or not turn_id:
                return self._fail("muse turn/unqueued returned no turn id")
            if self.turn_id is not None and turn_id != self.turn_id:
                return self._fail("muse turn/unqueued returned an inconsistent turn id")
            self.turn_id = turn_id
            self.busy = False
            return replace(self._fail("muse queued turn was reclaimed before it ran"),
                           turn_completed=True, turn_failed=True)
        if method == "turn/started" and params.get("commandId") == self.command_id:
            if not isinstance(params.get("turnId"), str) or not params["turnId"]:
                return self._fail("muse turn/started returned no turn id")
            if self.turn_id is not None and params["turnId"] != self.turn_id:
                return self._fail("muse turn/started returned an inconsistent turn id")
            self.turn_id = params["turnId"]
            self.turn_started = True
        elif method == "item/completed" and self.busy:
            item = params.get("item")
            if not isinstance(item, dict):
                return self._fail("muse MSP item was not an object")
            if item.get("turnId") == self.turn_id and item.get("kind") == "agentMessage":
                if not isinstance(item.get("text"), str):
                    return self._fail("muse MSP agent message had no text")
                self.items[item["itemId"]] = item["text"]
        elif method == "turn/completed" and self.busy and params.get("turnId") == self.turn_id:
            terminal = params.get("terminal")
            if terminal not in ("completed", "failed", "cancelled"):
                return self._fail(f"muse MSP unknown turn terminal {terminal!r}")
            self.busy = False
            self._retire_tell_calls()
            text = "\n".join(self.items.values())
            self.items = {}
            failed = terminal != "completed"
            error = params.get("error") or params.get("reason") or terminal
            message = f"muse turn {terminal}: {error}" if failed else ""
            fatal = failed and not (terminal == "cancelled" and self.cancel_sent)
            if fatal:
                self.error = message
                self.ready = False
            return ParsedFrame(assistant_text=text, result_text=text.strip(),
                               error_text=message, fatal=fatal,
                               turn_completed=True, turn_failed=failed, replies=self._release())
        elif method == "approval/requested":
            return self._approval(params)
        elif method == "approval/updated":
            return self._approval_updated(params)
        elif method == "approval/reviewing":
            self.approvals_reviewing.add(params["approvalId"])
        elif method == "approval/resolved":
            self.approvals_resolved.add(params["approvalId"])
            self.approvals_reviewing.discard(params["approvalId"])
            self._retire_tell_approval(params)
        elif method == "userInput/requested":
            return self._user_input(params)
        elif method == "userInput/settled":
            self.user_inputs_cancelled.add(params["userInputId"])
        return ParsedFrame()

    def _response(self, frame):
        request = self.pending.pop(frame.get("id"), None)
        if request is None:
            return ParsedFrame()
        method, command = request
        if "error" in frame:
            message = f"muse {method} failed: {frame['error']}"
            if method == "turn/start" and command == self.command_id:
                self.busy = False
                self.error = message
                self.ready = False
                return ParsedFrame(error_text=message, turn_completed=True, turn_failed=True,
                                   fatal=True)
            if method == "turn/cancel":
                return ParsedFrame(error_text=message)
            return self._fail(message)
        result = frame.get("result")
        if not isinstance(result, dict):
            return self._fail(f"muse {method} returned no result")
        if method == "initialize":
            if (result.get("schema") or {}).get("version") != 1:
                return self._fail("muse MSP schema version is not 1")
            if "sessionMcp" not in result.get("grantedCapabilities", []):
                return self._fail("muse MSP did not grant sessionMcp; a8s tools unavailable")
            if self.restricted and result.get("experimentalApi") is not True:
                return self._fail("muse MSP did not enable native permission profile verification")
            self.version = (result.get("serverInfo") or {}).get("version")
            initialized = {"method": "initialized"}
            if self.restricted:
                initialized["params"] = {}
            return ParsedFrame(replies=(rpc(initialized), self._start()))
        if method == "session/start":
            session = result.get("session") or {}
            sid = session.get("sessionId")
            if not isinstance(sid, str) or not sid:
                return self._fail("muse session/start returned no session id")
            if (session.get("approvalMode") or {}).get("mode") != self.mode:
                return self._fail("muse session/start did not select the requested approval mode")
            if self.model and session.get("modelId") != self.model:
                return self._fail("muse session/start did not select the requested model")
            if self.restricted:
                state = session.get("permissionState") or {}
                if (self.mode != "onRequest" or state.get("profileId") != ":ask-me"
                        or state.get("reviewerAuthority") != "human"
                        or type(state.get("reviewerAttached")) is not bool
                        or state.get("axes") != HUMAN_AXES):
                    return self._fail("muse session/start did not verify the native human permission profile")
                self.human_policy_verified = True
            self.session_id = sid
            self.ready = self.handshake_done = True
            return ParsedFrame(replies=self._release())
        if method == "turn/start" and command == self.command_id and self.busy:
            # A terminal view event can precede the runtime's final cleanup;
            # our next serial submit is then accepted as queued, not started.
            if result.get("commandId") != command or result.get("status") != "accepted" or result.get("disposition") not in ("started", "queued"):
                return self._fail("muse turn/start did not accept a serial turn")
            turn_id = result.get("turnId")
            if not isinstance(turn_id, str) or not turn_id or (self.turn_id and turn_id != self.turn_id):
                return self._fail("muse turn/start returned an inconsistent turn id")
            self.turn_id = turn_id
        return ParsedFrame()

    @staticmethod
    def _valid_call_id(value):
        return isinstance(value, str) and 0 < len(value) <= MAX_PERMISSION_CALL_ID_LENGTH

    def _bind_tell_identity(self, call_id, item_id, approval_id, turn_id):
        if not self.grant_tell or self.tell_saturated:
            return None
        values = {key: value for key, value in (
            ("call", call_id), ("item", item_id), ("approval", approval_id))
            if self._valid_call_id(value)}
        if not values:
            return None
        indices = {"call": self.tell_calls, "item": self.tell_items,
                   "approval": self.tell_approvals}
        if any(value not in indices[key] and len(indices[key]) >= MAX_PERMISSION_CALLS
               for key, value in values.items()):
            for index in indices.values():
                index.clear()
            self.tell_saturated = True
            return None
        previous = [indices[key][value] for key, value in values.items()
                    if value in indices[key]]
        identity = previous[0] if previous else {"active": True}
        if any(record is not identity for record in previous):
            for record in previous:
                record["active"] = False
        if self._valid_call_id(turn_id):
            values["turn"] = turn_id
        for key, value in values.items():
            if key in identity and identity[key] != value:
                identity["active"] = False
            identity.setdefault(key, value)
            if key in indices:
                indices[key].setdefault(value, identity)
        return identity

    def _retire_tell_calls(self):
        for index in (self.tell_calls, self.tell_items, self.tell_approvals):
            for identity in index.values():
                identity["active"] = False

    def _retire_tell_approval(self, params):
        identity = self._bind_tell_identity(None, params.get("itemId"),
                                            params.get("approvalId"), params.get("turnId"))
        if identity is not None:
            identity["active"] = False

    def _active_tell_turn(self):
        return (self.grant_tell and self.human_policy_verified and self.ready
                and self.handshake_done and self.busy and self.turn_started
                and not self.cancel_sent and not self.closing and self._valid_call_id(self.turn_id))

    def _observe_tell_item(self, item, completed, child):
        if not isinstance(item, dict):
            return
        identity = self._bind_tell_identity(item.get("callId"), item.get("itemId"),
                                            item.get("approvalId"), item.get("turnId"))
        if identity is None:
            return
        valid = (self._active_tell_turn() and not completed and not child
                 and "subagentOrigin" not in item and item.get("kind") == "toolCall"
                 and item.get("tool") == TELL_TOOL and item.get("status") == "inProgress"
                 and item.get("turnId") == self.turn_id
                 and self._valid_call_id(item.get("callId"))
                 and self._valid_call_id(item.get("itemId"))
                 and ("approvalId" not in item or self._valid_call_id(item["approvalId"])))
        if not valid:
            identity["active"] = False

    def _allows_tell(self, params, request):
        identity = self._bind_tell_identity(params.get("toolCallId"), params.get("itemId"),
                                            params.get("approvalId"), params.get("turnId"))
        if identity is None:
            return False
        subject = params.get("subject")
        allowed = (request and self._active_tell_turn() and identity["active"]
                   and params.get("turnId") == self.turn_id
                   and self._valid_call_id(params.get("itemId"))
                   and self._valid_call_id(params.get("toolCallId"))
                   and self._valid_call_id(params.get("approvalId"))
                   and params["currentRequirementId"]["sourceIndex"] == 0
                   and "subagentOrigin" not in params
                   and params.get("protectedWrite") is False and params.get("judgeEscalated") is False
                   and params.get("toolName") == TELL_TOOL and isinstance(subject, dict)
                   and subject.get("kind") == "tool" and subject.get("toolName") == TELL_TOOL)
        identity["active"] = False
        return bool(allowed)

    def _approval(self, params, request=True):
        approval_id = params["approvalId"]
        requirement = params["currentRequirementId"]
        if (not isinstance(approval_id, str) or not isinstance(requirement, dict)
                or requirement.get("approvalId") != approval_id
                or type(requirement.get("sourceIndex")) is not int):
            return self._fail("muse approval has an invalid requirement id")
        key = (approval_id, requirement["sourceIndex"])
        allow_tell = self._allows_tell(params, request)
        if params.get("judgeEscalated") is True:
            self.approvals_reviewing.discard(approval_id)
        if (approval_id in self.approvals_resolved or approval_id in self.approvals_reviewing
                or key in self.approval_decisions):
            return ParsedFrame()
        choices = params.get("availableChoices") or []
        choice = next((c for c in choices if allow_tell and isinstance(c, dict)
                       and c.get("decision") == "approved" and c.get("scope") == "once"
                       and self._valid_call_id(c.get("choiceId"))), None)
        allowed = choice is not None
        if not allowed:
            choice = next((c for c in choices if isinstance(c, dict)
                           and c.get("decision") in ("denied", "abort")
                           and c.get("scope") == "once"), None)
        if not choice or not isinstance(choice.get("choiceId"), str):
            return self._fail("muse approval has no reject-once choice")
        self.approval_decisions.add(key)
        return ParsedFrame(error_text="" if allowed else "muse requested approval; rejected once", replies=(
            self._request("approval/decide", {"sessionId": self.session_id,
                "approvalId": approval_id, "requirementId": requirement,
                "choiceId": choice["choiceId"]}, command=True),))

    def _approval_updated(self, params):
        approval_id = params["approvalId"]
        kind = params["change"]["kind"]
        if kind == "alreadyTerminal":
            self.approvals_resolved.add(approval_id)
            self.approvals_reviewing.discard(approval_id)
            self._retire_tell_approval(params)
        elif kind == "reviewStarted":
            self.approvals_reviewing.add(approval_id)
        elif kind == "reviewCompleted":
            # The reviewer can resolve next; only a request hands off its decision.
            self.approvals_reviewing.discard(approval_id)
        elif kind in ("stageResolved", "restartRecovered"):
            if approval_id not in self.approvals_reviewing and params.get("availableChoices"):
                return self._approval(params, request=False)
        # Persistence and unknown activity do not present a new decision.
        return ParsedFrame()

    def _user_input(self, params):
        user_input_id = params["userInputId"]
        if user_input_id in self.user_inputs_cancelled:
            return ParsedFrame()
        self.user_inputs_cancelled.add(user_input_id)
        return ParsedFrame(error_text="muse requested user input; cancelled", replies=(
            self._request("userInput/cancel", {"sessionId": self.session_id,
                "userInputId": user_input_id, "reason": "unattended resident"}, command=True),))


def probe_muse(argv, cwd, env, timeout=20.0):
    """Verify native session policy without submitting a turn."""
    from engines.resident import stop_engine
    session_env = dict(os.environ)
    session_env.update(env)
    session = MuseSession(argv, cwd, session_env)
    frames = queue.Queue()
    result = {}
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errors:
        try:
            probe_argv = list(argv) if session.restricted else [*argv, "--no-session-log"]
            proc = subprocess.Popen(probe_argv, cwd=str(cwd),
                env={k: v for k, v in session_env.items()
                     if not k.startswith(("R4T_SESSION_", "A8S_TURN_"))},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                text=True, encoding="utf-8", errors="replace",
                **launch_options(new_session=True))
        except OSError as exc:
            return {"error": f"cannot start muse serve: {exc}"}

        def read():
            for line in proc.stdout:
                try:
                    frames.put(json.loads(line))
                except ValueError:
                    continue

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            for line in session.opening_lines():
                proc.stdin.write(line + "\n")
            proc.stdin.flush()
            end = time.monotonic() + timeout
            while not session.ready and time.monotonic() < end:
                try:
                    frame = frames.get(timeout=0.1)
                except queue.Empty:
                    if proc.poll() is not None:
                        break
                    continue
                parsed = session.on_frame(frame)
                if parsed.fatal:
                    result["error"] = parsed.error_text
                    break
                for line in parsed.replies:
                    proc.stdin.write(line + "\n")
                proc.stdin.flush()
            if not session.ready:
                result.setdefault("error", "muse MSP did not finish its handshake")
        except (BrokenPipeError, OSError) as exc:
            result["error"] = f"muse MSP handshake pipe failed: {exc}"
        finally:
            stop_engine(proc, group=proc.pid if os.name != "nt" else None, grace_seconds=10)
            reader.join(timeout=1)
        if result.get("error"):
            errors.seek(0)
            tail = errors.read().strip().splitlines()
            if tail:
                result["error"] += ": " + tail[-1]
    result.update(version=session.version, approval_mode=session.mode, model=session.model)
    return result
