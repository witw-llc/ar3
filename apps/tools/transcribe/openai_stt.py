"""OpenAI speech-to-text client for tool-transcribe (stdlib HTTP only)."""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

API_URL = "https://api.openai.com/v1/audio/transcriptions"
KEY_ENV = "OPENAI_API_KEY"
DEFAULT_OPENAI_MODEL = "whisper-1"
TIMED_MODELS = ("whisper-1",)
REDACTED = "[REDACTED]"
MIN_SECRET_LENGTH = 4
MISSING_KEY_HINT = (
    f"{KEY_ENV} is not set (run `a8s vars <node> set secret.{KEY_ENV} -`)"
)


class OpenAISTTError(RuntimeError):
    """Raised when an OpenAI transcription request fails."""


def api_key() -> str | None:
    return (os.environ.get(KEY_ENV) or "").strip() or None


def require_api_key() -> str:
    key = api_key()
    if not key:
        raise OpenAISTTError(MISSING_KEY_HINT)
    return key


def scrub_text(text: str) -> str:
    """Redact the OpenAI key (raw and JSON-escaped) from `text`."""
    key = api_key()
    if not text or not key or len(key) < MIN_SECRET_LENGTH:
        return text
    out = text
    for needle in sorted({key, json.dumps(key)[1:-1]}, key=len, reverse=True):
        out = out.replace(needle, REDACTED)
    return out


def check_timed_model(model: str) -> None:
    if model not in TIMED_MODELS:
        allowed = ", ".join(TIMED_MODELS)
        raise OpenAISTTError(
            f"openai model {model!r} returns no segment timestamps; timed output "
            f"(fancy flavor) needs {allowed} (pass --model {TIMED_MODELS[0]} or --flavor plain)"
        )


def build_multipart(
    fields: list[tuple[str, str]],
    *,
    file_field: str,
    filename: str,
    data: bytes,
    content_type: str,
) -> tuple[bytes, str]:
    boundary = f"ar3-{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            + value.encode("utf-8")
            + b"\r\n"
        )
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _http_error_detail(body: bytes) -> str:
    raw = body.decode("utf-8", errors="replace").strip()
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            raw = err["message"]
        elif isinstance(err, str):
            raw = err
    return scrub_text(raw)[:300] or "request failed"


class _HTTPStatus(Exception):
    """A non-2xx reply, with the start of its body already read."""

    def __init__(self, code: int, body: bytes) -> None:
        super().__init__(code)
        self.code = code
        self.body = body


def _post_within(req: urllib.request.Request, budget: float) -> bytes:
    """Send `req` and read the whole response within `budget` seconds of wall-clock time.

    A urlopen timeout bounds each socket operation, not the request: a server that sends a few
    bytes at a time keeps every read under it. So the request runs in a daemon thread, and the
    caller waits no longer than `budget`. A thread left behind ends at its own socket timeout."""
    if budget <= 0:
        raise TimeoutError("no time left")
    outcome: dict = {}

    def run() -> None:
        try:
            with urllib.request.urlopen(req, timeout=max(0.1, budget)) as resp:
                outcome["raw"] = resp.read()
        except urllib.error.HTTPError as exc:
            # The error body is part of the response: read it here, inside the budget.
            try:
                body = exc.read(8192) or b""
            except BaseException:  # a body that breaks off still names the status
                body = b""
            finally:
                exc.close()
            outcome["error"] = _HTTPStatus(exc.code, body)
        except BaseException as exc:  # handed to the caller below
            outcome["error"] = exc

    worker = threading.Thread(target=run, name="openai-stt-request", daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(budget)
    if worker.is_alive() or time.monotonic() - started > budget:
        raise TimeoutError(f"request passed its {budget:.0f}s budget")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["raw"]


def transcribe_audio(
    path: Path,
    *,
    model: str,
    language: str | None = None,
    prompt: str = "",
    timed: bool = False,
    timeout: float = 600.0,
    url: str = API_URL,
) -> dict:
    """POST one audio file (under the upload limit) and return `{language, text, segments}`."""
    key = require_api_key()
    if timed:
        check_timed_model(model)
    fields: list[tuple[str, str]] = [
        ("model", model),
        ("response_format", "verbose_json" if timed else "json"),
    ]
    if timed:
        fields.append(("timestamp_granularities[]", "segment"))
    if language:
        fields.append(("language", language))
    if prompt:
        fields.append(("prompt", prompt))
    body, content_type = build_multipart(
        fields,
        file_field="file",
        filename=path.name,
        data=path.read_bytes(),
        content_type="audio/mpeg",
    )
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": content_type},
        method="POST",
    )
    try:
        raw = _post_within(req, max(0.0, float(timeout)))
    except _HTTPStatus as exc:
        detail = _http_error_detail(exc.body)
        raise OpenAISTTError(f"openai HTTP {exc.code}: {detail}") from None
    except (socket.timeout, TimeoutError):
        raise OpenAISTTError(f"openai request timed out after {timeout:.0f}s") from None
    except OSError as exc:
        if isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError)):
            raise OpenAISTTError(f"openai request timed out after {timeout:.0f}s") from None
        raise OpenAISTTError(f"openai request failed: {scrub_text(str(exc))}") from None
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        raise OpenAISTTError("openai returned a response that is not JSON") from None
    if not isinstance(payload, dict):
        raise OpenAISTTError("openai returned an unexpected response")
    return {
        "language": str(payload.get("language") or ""),
        "text": str(payload.get("text") or "").strip(),
        "segments": [
            {
                "start": float(seg.get("start") or 0),
                "end": float(seg.get("end") or 0),
                "text": str(seg.get("text") or "").strip(),
            }
            for seg in (payload.get("segments") or [])
            if isinstance(seg, dict)
        ],
    }
