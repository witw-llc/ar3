"""Ollama vision client for tool-transcribe fancy video narratives."""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_VISION_MODEL = "qwen3.6"


class OllamaError(RuntimeError):
    """Raised when Ollama is unreachable or lacks a vision-capable model."""


def resolve_ollama_url(url: str | None = None) -> str:
    chosen = (url or os.environ.get("OLLAMA_URL") or DEFAULT_OLLAMA_URL).strip()
    return chosen.rstrip("/") or DEFAULT_OLLAMA_URL


def resolve_vision_model(model: str | None = None) -> str:
    chosen = (model or "").strip()
    return chosen or DEFAULT_VISION_MODEL


def _request(
    base_url: str,
    method: str,
    endpoint: str,
    body: dict | None = None,
    *,
    timeout: float = 30.0,
) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}{endpoint}",
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise OllamaError(
            f"HTTP {exc.code} from Ollama {endpoint}: {detail or exc.reason}"
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise OllamaError(
            f"Ollama not reachable at {base_url} ({reason}); start it with: ollama serve"
        ) from None
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise OllamaError(f"invalid JSON from Ollama {endpoint}: {exc}") from None
    return parsed if isinstance(parsed, dict) else {}


def ensure_vision_model(
    model: str,
    *,
    ollama_url: str | None = None,
    timeout: float = 10.0,
) -> None:
    base = resolve_ollama_url(ollama_url)
    _request(base, "GET", "/api/tags", timeout=min(5.0, timeout))
    try:
        info = _request(base, "POST", "/api/show", {"name": model}, timeout=timeout)
    except OllamaError as exc:
        msg = str(exc)
        if "HTTP 404" in msg or "not found" in msg.lower():
            raise OllamaError(
                f"model {model!r} is not installed; pull it with: ollama pull {model}"
            ) from None
        raise
    caps = [str(item) for item in (info.get("capabilities") or ())]
    if "vision" not in caps:
        raise OllamaError(
            f"model {model!r} has no vision capability; install a vision model such as {DEFAULT_VISION_MODEL}"
        )


def probe_ollama(
    *,
    ollama_url: str | None = None,
    vision_model: str | None = None,
) -> tuple[bool, str]:
    """Quick readiness check for `ar3 tool transcribe doctor`."""
    base = resolve_ollama_url(ollama_url)
    model = resolve_vision_model(vision_model)
    try:
        ensure_vision_model(model, ollama_url=base, timeout=1.5)
        return True, f"reachable at {base} (vision model {model!r} ready)"
    except OllamaError as exc:
        return False, str(exc)


def chat_with_images(
    model: str,
    prompt: str,
    image_paths: list[Path],
    *,
    system: str | None = None,
    ollama_url: str | None = None,
    timeout: float = 600.0,
) -> str:
    base = resolve_ollama_url(ollama_url)
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append(
        {
            "role": "user",
            "content": prompt,
            "images": [
                base64.b64encode(p.read_bytes()).decode("ascii")
                for p in image_paths
            ],
        }
    )
    data = _request(
        base,
        "POST",
        "/api/chat",
        {
            "model": model,
            "messages": messages,
            "stream": False,
            "think": False,
        },
        timeout=timeout,
    )
    message = data.get("message") or {}
    content = str(message.get("content") or "").strip()
    if not content:
        raise OllamaError("empty response from vision model")
    return content
