"""Generic ACP session. Product engines subclass `AcpSession` and supply only
what is theirs: the auth method, the MCP servers, and any vendor requests.

The bridge speaks one prompt at a time. This class holds the rest, checks the
handshake, and turns a bad frame into a fatal session instead of an exception
the reader would die on.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path


ACP_PROTOCOL_VERSION = 1
HANDSHAKE_DEADLINE_SECONDS = 20.0
_INIT_ID = 1
_AUTH_ID = 2
_NEW_ID = 3


@dataclass(frozen=True)
class ParsedFrame:
    """What one engine frame means to the bridge. `replies` are lines the
    bridge writes to the engine's stdin at once; `fatal` ends the engine
    because the session cannot run."""

    assistant_text: str = ""
    result_text: str = ""
    error_text: str = ""
    turn_completed: bool = False
    replies: tuple[str, ...] = ()
    fatal: bool = False


def rpc(payload: dict) -> str:
    """One JSON-RPC 2.0 line. Every request and every reply carries the key."""
    body = dict(payload)
    body["jsonrpc"] = "2.0"
    return json.dumps(body)


def permission_outcome(options: object, *, allow: bool) -> dict:
    """Pick an offered option of the wanted kind. Ids are opaque. When the
    kind is absent, cancel. Never invent an id."""
    want = "allow_once" if allow else "reject_once"
    if isinstance(options, list):
        for opt in options:
            if not isinstance(opt, dict) or opt.get("kind") != want:
                continue
            option_id = opt.get("optionId")
            if isinstance(option_id, str) and option_id:
                return {"outcome": {"outcome": "selected", "optionId": option_id}}
    return {"outcome": {"outcome": "cancelled"}}


def _chunk_text(frame: dict) -> str:
    params = frame.get("params")
    if not isinstance(params, dict):
        return ""
    update = params.get("update")
    if not isinstance(update, dict) or update.get("sessionUpdate") != "agent_message_chunk":
        return ""
    content = update.get("content")
    if not isinstance(content, dict) or content.get("type") != "text":
        return ""
    text = content.get("text")
    return text if isinstance(text, str) else ""


class AcpSession:
    """One ACP session. Subclasses override `auth_method`, `mcp_servers`,
    `after_session_new`, and `on_vendor_request`."""

    def __init__(
        self, argv: list[str], dir_path: Path, env: dict[str, str] | None = None,
    ):
        self.argv = list(argv)
        self.cwd = str(Path(dir_path).resolve())
        self.env = dict(os.environ if env is None else env)
        self.session_id: str | None = None
        self.ready = False
        self.busy = False
        self.held: deque[str] = deque()
        self.turn_requests: set[int] = set()
        self.next_id = _NEW_ID + 1
        self.chunks: list[str] = []
        self.error: str | None = None
        self.cancel_sent = False
        self.handshake_done = False
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.saw_init = False
        self.saw_auth = False
        self.created = False
        self.settled = True

    def auth_method(self) -> str | None:
        """Method id to authenticate with, or None when this engine skips it."""
        return None

    def allows_permission(self) -> bool:
        """Select allow_once when True. A product session decides; the default
        rejects. `--force` is Cursor's, so it does not live here."""
        return False

    def mcp_servers(self) -> list:
        return []

    def deadline_error(self) -> str | None:
        # A session.error must not disarm this. The deadline stays armed
        # until the handshake completes; a later fatal frame stops the
        # process on its own path.
        if self.ready or self.handshake_done:
            return None
        if time.monotonic() - self.started < HANDSHAKE_DEADLINE_SECONDS:
            return None
        return (
            f"acp handshake was not ready after {HANDSHAKE_DEADLINE_SECONDS:g}s"
        )

    def opening_lines(self) -> list[str]:
        """initialize only. authenticate follows when the subclass asks and
        the server offered that method. session/new follows that answer."""
        return [rpc({"id": _INIT_ID, "method": "initialize", "params": {
            "protocolVersion": ACP_PROTOCOL_VERSION,
            "clientCapabilities": {},
            "clientInfo": {"name": "r4t", "version": "0"},
        }})]

    def _authenticate_line(self) -> str:
        return rpc({"id": _AUTH_ID, "method": "authenticate",
                    "params": {"methodId": self.auth_method()}})

    def _session_new_line(self) -> str:
        return rpc({"id": _NEW_ID, "method": "session/new", "params": {
            "cwd": self.cwd,
            "mcpServers": self.mcp_servers(),
        }})

    def _offered_auth(self, result: dict) -> list[str]:
        methods = result.get("authMethods")
        if not isinstance(methods, list):
            return []
        offered: list[str] = []
        for item in methods:
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]:
                offered.append(item["id"])
        return offered

    def cancel_line(self) -> str | None:
        """session/cancel for the in-flight turn, or None when nothing is running."""
        with self.lock:
            if not self.busy or not self.session_id:
                return None
            self.cancel_sent = True
            return rpc({
                "method": "session/cancel",
                "params": {"sessionId": self.session_id},
            })

    def turn_lines(self, text: str) -> list[str]:
        with self.lock:
            if not self.ready or self.busy:
                self.held.append(text)
                return []
            return [self._prompt(text)]

    def on_frame(self, frame: dict) -> ParsedFrame:
        with self.lock:
            if self.error:
                return ParsedFrame(error_text=self.error, fatal=True)
            try:
                return self._on_frame(frame)
            except Exception as exc:
                return self._fail(f"acp frame failed the session: {type(exc).__name__}")

    def after_session_new(self, result: dict) -> ParsedFrame | None:
        """Return a frame to send before the session is ready, or None."""
        del result
        return None

    def on_vendor_request(self, frame: dict, method: str) -> ParsedFrame | None:
        del frame, method
        return None

    def on_extra_response(self, frame: dict) -> ParsedFrame | None:
        del frame
        return None

    def _prompt(self, text: str) -> str:
        rid = self.next_id
        self.next_id += 1
        self.turn_requests.add(rid)
        self.busy = True
        self.chunks = []
        return rpc({"id": rid, "method": "session/prompt", "params": {
            "sessionId": self.session_id,
            "prompt": [{"type": "text", "text": text}],
        }})

    def _release(self) -> tuple[str, ...]:
        if self.ready and not self.busy and self.held:
            return (self._prompt(self.held.popleft()),)
        return ()

    def _fail(self, text: str) -> ParsedFrame:
        self.error = text
        self.ready = False
        return ParsedFrame(error_text=text, fatal=True)

    def _maybe_ready(self, pending: ParsedFrame | None = None) -> ParsedFrame:
        if self.error:
            return ParsedFrame(error_text=self.error, fatal=True)
        if not (self.saw_init and self.saw_auth and self.created and self.settled):
            return pending or ParsedFrame()
        if self.ready:
            return pending or ParsedFrame()
        self.ready = True
        self.handshake_done = True
        replies = (pending.replies if pending is not None else ()) + self._release()
        return ParsedFrame(replies=replies)

    def _on_frame(self, frame: dict) -> ParsedFrame:
        if not isinstance(frame, dict):
            return self._fail("acp frame was not an object")
        method = frame.get("method")
        if method is not None and not isinstance(method, str):
            return self._fail("acp method was not a string")
        if method is not None and "id" in frame:
            return self._on_request(frame, method)
        if method == "session/update":
            return self._on_update(frame)
        if method is None:
            return self._on_response(frame)
        return ParsedFrame()

    def _on_update(self, frame: dict) -> ParsedFrame:
        params = frame.get("params")
        if not isinstance(params, dict):
            return self._fail("acp session/update params were not an object")
        sid = params.get("sessionId")
        if self.session_id and sid != self.session_id:
            return self._fail("acp session/update named a different session")
        update = params.get("update")
        if update is not None and not isinstance(update, dict):
            return self._fail("acp session/update was not an object")
        text = _chunk_text(frame)
        if text:
            self.chunks.append(text)
        return ParsedFrame()

    def _on_request(self, frame: dict, method: str) -> ParsedFrame:
        if method == "session/request_permission":
            params = frame.get("params")
            options = params.get("options") if isinstance(params, dict) else None
            if params is not None and not isinstance(params, dict):
                return self._fail("acp permission params were not an object")
            sid = params.get("sessionId") if isinstance(params, dict) else None
            if self.session_id and sid != self.session_id:
                return self._fail(
                    "acp session/request_permission named a different session"
                )
            allow = self.allows_permission()
            result = permission_outcome(options, allow=allow)
            stance = "allowed once" if allow else "rejected"
            if result["outcome"]["outcome"] == "cancelled":
                stance = "cancelled"
            return ParsedFrame(
                error_text=f"acp asked session/request_permission; {stance}",
                replies=(rpc({"id": frame["id"], "result": result}),),
            )
        vendor = self.on_vendor_request(frame, method)
        if vendor is not None:
            return vendor
        return ParsedFrame(
            error_text=f"acp asked {method}; refused",
            replies=(rpc({
                "id": frame["id"],
                "error": {"code": -32601, "message": f"r4t does not serve {method}"},
            }),),
        )

    def _on_response(self, frame: dict) -> ParsedFrame:
        rid = frame.get("id")
        error = frame.get("error")
        message = ""
        if error is not None:
            if isinstance(error, dict):
                message = str(error.get("message") or error)
            else:
                message = str(error)
        if rid == _INIT_ID:
            if error is not None:
                return self._fail(f"acp initialize failed: {message}")
            result = frame.get("result")
            if not isinstance(result, dict):
                return self._fail("acp initialize returned no result")
            version = result.get("protocolVersion")
            if version != ACP_PROTOCOL_VERSION:
                return self._fail(
                    f"acp protocolVersion {version!r} is not {ACP_PROTOCOL_VERSION}"
                )
            self.saw_init = True
            return self._after_initialize(result)
        if rid == _AUTH_ID:
            if error is not None:
                return self._fail(f"acp authenticate failed: {message}")
            if "result" not in frame:
                return self._fail("acp authenticate returned no result")
            self.saw_auth = True
            if self.created:
                return self._maybe_ready()
            return ParsedFrame(replies=(self._session_new_line(),))
        if rid == _NEW_ID:
            return self._on_session_new(frame, message)
        extra = self.on_extra_response(frame)
        if extra is not None:
            return extra
        if rid in self.turn_requests:
            return self._on_prompt_done(frame, message)
        return ParsedFrame()

    def _after_initialize(self, result: dict) -> ParsedFrame:
        method = self.auth_method()
        if method is None:
            self.saw_auth = True
            if self.created:
                return self._maybe_ready()
            return ParsedFrame(replies=(self._session_new_line(),))
        if not isinstance(method, str) or not method:
            return self._fail("acp auth method was not a string")
        offered = self._offered_auth(result)
        if method not in offered:
            shown = ", ".join(offered) or "(none)"
            return self._fail(
                f"acp initialize offered no auth method {method!r}; offered: {shown}"
            )
        return ParsedFrame(replies=(self._authenticate_line(),))

    def _on_session_new(self, frame: dict, message: str) -> ParsedFrame:
        if self.created:
            return ParsedFrame()
        if frame.get("error") is not None:
            return self._fail(f"acp session/new failed: {message}")
        result = frame.get("result")
        if not isinstance(result, dict):
            return self._fail("acp session/new returned no result")
        session_id = result.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            return self._fail("acp session/new returned no session id")
        self.session_id = session_id
        self.created = True
        pending = self.after_session_new(result)
        if pending is not None and pending.fatal:
            return pending
        return self._maybe_ready(pending)

    def _on_prompt_done(self, frame: dict, message: str) -> ParsedFrame:
        self.turn_requests.discard(frame.get("id"))
        self.busy = False
        text = "".join(self.chunks)
        self.chunks = []
        if frame.get("error") is not None:
            return ParsedFrame(
                error_text=f"acp session/prompt failed: {message}",
                turn_completed=True,
                replies=self._release(),
            )
        result = frame.get("result")
        if result is not None and not isinstance(result, dict):
            return self._fail("acp session/prompt result was not an object")
        stop = result.get("stopReason") if isinstance(result, dict) else None
        # agent acp answers our session/cancel with stopReason "cancelled".
        # That turn was interrupted. Leave it in flight so the bridge drops
        # it as a stop instead of recording a clean completion.
        if stop == "cancelled" and self.cancel_sent:
            return ParsedFrame(error_text="acp turn cancelled")
        error = ""
        if stop in ("refusal", "cancelled"):
            error = f"acp turn {stop}"
        return ParsedFrame(
            assistant_text=text,
            result_text=text.strip(),
            error_text=error,
            turn_completed=True,
            replies=self._release(),
        )
