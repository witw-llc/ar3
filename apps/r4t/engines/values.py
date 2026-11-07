"""What `--model` and `--effort` accept, per engine, without spending a turn.

Effort vocabularies come from the preset table or the Codex model catalog.
Model lists come from each CLI's discovery interface. Both questions answer `(values, note)`: `values` may be empty when
the provider decides, and `note` then says so. A preset that cannot answer at
all raises ValuesError.
"""
from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time

from ar3.proc import launch_options, terminate_group


from engines.codex import QUOTA_ARGV as CODEX_APP_SERVER_ARGV
from rig import HARNESS_PRESETS, RigError

LIST_TIMEOUT_SECONDS = 30
CLAUDE_ALIASES = ["fable", "opus", "sonnet"]
CLAUDE_NOTE = "claude also accepts a full model name"

CODEX_MAX_FRAME = 1024 * 1024
CODEX_MAX_FRAMES = 20000
CODEX_MAX_PAGES = 50
CODEX_MAX_MODELS = 2000
CODEX_MAX_CURSOR = 4096
CODEX_PAGE_LIMIT = 100
CODEX_CATALOG_NOTE = (
    "values come from the installed codex catalog; "
    "listing a model does not prove this account can use it"
)

NO_LIST_VERB = {
    "copilot": "`--model` is passed through and a bad id fails at spawn with copilot's own message",
    "opencode": "`-m` is passed through as provider/model and a bad id fails at spawn with opencode's own message",
}


class ValuesError(Exception):
    """The preset cannot list the values asked for. The message says why."""


def _preset(preset: str) -> tuple[str, dict]:
    key = (preset or "").strip().lower()
    if key not in HARNESS_PRESETS:
        raise ValuesError(
            f"{preset!r} is not an engine or preset id "
            f"(see `r4t engine list`)"
        )
    return key, HARNESS_PRESETS[key]


def _no_model_filter(key: str, verb: str) -> ValuesError:
    return ValuesError(f"engine {key!r} does not filter {verb} by --model")


def efforts(preset: str, model: str | None = None) -> tuple[list[str], str | None]:
    key, entry = _preset(preset)
    if key == "codex":
        return _codex_efforts(model)
    if model is not None:
        raise _no_model_filter(key, "efforts")
    if entry.get("effort_values"):
        return list(entry["effort_values"]), None
    if entry.get("effort_argv"):
        return [], (
            f"engine {key!r} takes any non-empty effort value; "
            f"the provider decides (for example low, medium, high)"
        )
    hint = "; use --model with [effort=...] for Cursor" if key == "cursor" else ""
    raise ValuesError(f"engine {key!r} does not support --effort{hint}")


def _lines(argv: list[str]) -> list[str]:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=LIST_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            **launch_options(),
        )
    except OSError as exc:
        raise ValuesError(f"could not run `{' '.join(argv)}`: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValuesError(
            f"`{' '.join(argv)}` timed out after {LIST_TIMEOUT_SECONDS}s"
        ) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ValuesError(
            f"`{' '.join(argv)}` failed (exit {proc.returncode}): {detail}"
        )
    return proc.stdout.splitlines()


def _claude_models() -> tuple[list[str], str | None]:
    try:
        text = "\n".join(_lines(["claude", "--help"]))
    except ValuesError:
        return list(CLAUDE_ALIASES), CLAUDE_NOTE
    block = re.search(r"--model <model>(.*?)(?=\n\s*-)", text, re.S)
    found = re.findall(r"'([A-Za-z][\w.\-]*)'", block.group(1)) if block else []
    return (found or list(CLAUDE_ALIASES)), CLAUDE_NOTE


def _agy_models() -> list[str]:
    return [
        line.split("\t", 1)[0].strip()
        for line in _lines(["agy", "models"])
        if "\t" in line and line.split("\t", 1)[0].strip()
    ]


def _cursor_models(binary: str) -> list[str]:
    return [
        line.split(" - ", 1)[0].strip()
        for line in _lines([binary, "models"])
        if " - " in line
    ]


def _devin_models() -> list[str]:
    ids = []
    for line in _lines(["devin", "models", "list"]):
        match = re.match(r"^  (?!aliases:)(\S+)\s{2,}\S", line)
        if match:
            ids.append(match.group(1))
    return ids


def _muse_models() -> list[str]:
    """Ask `muse serve` for its catalog over MSP `model/list`: a query, so no
    session starts and no model is prompted. The child inherits this
    environment."""
    argv = ["muse", "serve", "--disable-request-user-input"]
    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "r4t", "version": "0"}, "capabilities": {}}},
        {"jsonrpc": "2.0", "method": "initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "model/list", "params": {}},
    ]
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
            **launch_options(new_session=True),
        )
    except OSError as exc:
        raise ValuesError(f"could not run `{' '.join(argv)}`: {exc}") from exc
    lines: queue.Queue = queue.Queue(maxsize=64)
    stopped = threading.Event()
    overflow = threading.Event()

    def pump():
        while not stopped.is_set():
            line = proc.stdout.readline(1024 * 1024 + 1)
            if len(line) > 1024 * 1024:
                overflow.set()
                return
            try:
                lines.put(line or None, timeout=0.1)
            except queue.Full:
                overflow.set()
                return
            if not line:
                return

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    deadline = time.monotonic() + LIST_TIMEOUT_SECONDS
    def send(frame):
        try:
            proc.stdin.write(json.dumps(frame) + "\n")
            proc.stdin.flush()
        except OSError as exc:
            raise ValuesError(f"`{' '.join(argv)}` closed its input: {exc}") from exc

    initialized = False
    try:
        send(frames[0])
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValuesError(f"`{' '.join(argv)}` model/list timed out after {LIST_TIMEOUT_SECONDS}s")
            if overflow.is_set():
                raise ValuesError("muse model/list exceeded the response limit")
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                raise ValuesError(
                    f"`{' '.join(argv)}` model/list timed out after {LIST_TIMEOUT_SECONDS}s"
                ) from None
            if line is None:
                raise ValuesError(f"`{' '.join(argv)}` exited before answering model/list")
            try:
                reply = json.loads(line)
            except ValueError:
                continue
            if not isinstance(reply, dict) or "method" in reply:
                continue
            if not initialized:
                if reply.get("id") != 1:
                    continue
                if "error" in reply or not isinstance(reply.get("result"), dict):
                    raise ValuesError("muse initialization failed")
                initialized = True
                send(frames[1])
                send(frames[2])
                continue
            if reply.get("id") != 2:
                continue
            if "error" in reply:
                raise ValuesError(f"muse model/list failed: {reply['error']}")
            result = reply.get("result")
            rows = result.get("models") if isinstance(result, dict) else None
            if not isinstance(rows, list):
                raise ValuesError("muse model/list returned no models list")
            return [
                row["modelId"] for row in rows
                if isinstance(row, dict) and isinstance(row.get("modelId"), str) and row["modelId"]
            ]
    finally:
        stopped.set()
        try:
            proc.stdin.close()
        except OSError:
            pass
        # Keep the leader unreaped through POSIX group teardown so its pid
        # cannot be reused while the shared helper sends its final signals.
        terminate_group(proc)
        proc.wait()
        reader.join(timeout=0.2)
        if not reader.is_alive():
            proc.stdout.close()


class _CodexServer:
    """One `codex app-server` child, read through a bounded queue.

    Every wait is measured against one monotonic deadline set at launch, so a
    silent, chatty or wedged child ends in a ValuesError and never a hang."""

    def __init__(self):
        self.argv = list(CODEX_APP_SERVER_ARGV)
        self.label = "codex app-server"
        self.deadline = time.monotonic() + LIST_TIMEOUT_SECONDS
        self.timeout = LIST_TIMEOUT_SECONDS
        self.frames = 0
        self.writer = None
        try:
            self.proc = subprocess.Popen(
                self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                **launch_options(new_session=True),
            )
        except OSError as exc:
            raise ValuesError(f"could not run `{' '.join(self.argv)}`: {exc}") from exc
        self.lines: queue.Queue = queue.Queue(maxsize=64)
        self.stopped = threading.Event()
        self.overflow = threading.Event()
        self.reader = threading.Thread(target=self._pump, daemon=True)
        self.reader.start()

    def _pump(self):
        while not self.stopped.is_set():
            try:
                line = self.proc.stdout.readline(CODEX_MAX_FRAME + 1)
            except (OSError, ValueError):
                line = ""
            if len(line) > CODEX_MAX_FRAME:
                self.overflow.set()
                return
            while not self.stopped.is_set():
                try:
                    self.lines.put(line or None, timeout=0.1)
                    break
                except queue.Full:
                    if time.monotonic() > self.deadline:
                        self.overflow.set()
                        return
            if not line:
                return

    def fail(self, message: str) -> ValuesError:
        return ValuesError(message)

    def send(self, frame: dict) -> None:
        """Write one frame from a daemon thread and wait for it only until the
        launch deadline, so a child that stops reading stdin cannot hang the
        caller. At most one write is ever pending, and none is retried."""
        if self.writer is not None and self.writer.is_alive():
            raise self.fail(f"{self.label} stopped reading its input")
        if time.monotonic() >= self.deadline:
            raise self.fail(f"{self.label} timed out after {self.timeout}s")
        data = json.dumps(frame) + "\n"
        problem: list = []

        def write():
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                problem.append(exc)

        self.writer = threading.Thread(target=write, daemon=True)
        self.writer.start()
        self.writer.join(timeout=max(0.0, self.deadline - time.monotonic()))
        if self.writer.is_alive():
            raise self.fail(f"{self.label} timed out after {self.timeout}s")
        if problem:
            raise self.fail(f"`{' '.join(self.argv)}` closed its input ({problem[0]})") from problem[0]

    def request(self, ident: int, method: str, params: dict) -> dict:
        """Send one request and return the `result` of the response carrying
        the same id. Server requests (a `method` field) never count as
        responses; they are ignored, since no thread or turn exists for the
        server to ask about."""
        self.send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
        while True:
            if self.overflow.is_set():
                raise self.fail(f"{self.label} {method} exceeded the response limit")
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise self.fail(f"{self.label} {method} timed out after {self.timeout}s")
            try:
                line = self.lines.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                continue
            if line is None:
                raise self.fail(f"{self.label} exited before answering {method}")
            self.frames += 1
            if self.frames > CODEX_MAX_FRAMES:
                raise self.fail(f"{self.label} {method} exceeded the response limit")
            try:
                reply = json.loads(line)
            except ValueError:
                continue
            if not isinstance(reply, dict):
                continue
            if "method" in reply:
                continue
            if reply.get("id") != ident or isinstance(reply.get("id"), bool):
                continue
            if "error" in reply:
                error = reply["error"]
                text = error.get("message") if isinstance(error, dict) else error
                raise ValuesError(f"{self.label} {method} failed: {str(text)[:300]}")
            result = reply.get("result")
            if not isinstance(result, dict):
                raise ValuesError(f"{self.label} {method} returned no result object")
            if time.monotonic() >= self.deadline:
                raise self.fail(f"{self.label} {method} timed out after {self.timeout}s")
            return result

    def close(self):
        self.stopped.set()
        # Killing the group first breaks the pipes, which wakes a blocked
        # writer or reader; a buffered stream is closed only once its worker
        # is gone, because close() would wait on the lock that worker holds.
        # The leader stays unreaped through group teardown so its pid cannot
        # be reused while the final signals go out.
        terminate_group(self.proc)
        self.proc.wait()
        for worker in (self.writer, self.reader):
            if worker is not None:
                worker.join(timeout=0.5)
        for stream, worker in ((self.proc.stdin, self.writer), (self.proc.stdout, self.reader)):
            if worker is None or not worker.is_alive():
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass


def _codex_catalog() -> list[dict]:
    """Every visible `model/list` entry from the installed Codex app-server.
    A query only: no thread and no turn is ever started."""
    server = _CodexServer()
    try:
        server.request(1, "initialize", {"clientInfo": {"name": "r4t", "version": "0"}})
        server.send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        entries: list[dict] = []
        seen: set[str] = set()
        cursor = None
        for page in range(CODEX_MAX_PAGES):
            params = {"limit": CODEX_PAGE_LIMIT, "includeHidden": False}
            if cursor is not None:
                params["cursor"] = cursor
            result = server.request(2 + page, "model/list", params)
            rows = result.get("data")
            if not isinstance(rows, list):
                raise ValuesError("codex model/list returned no data list")
            for row in rows:
                if (not isinstance(row, dict) or not isinstance(row.get("model"), str)
                        or not row["model"].strip()):
                    raise ValuesError("codex model/list returned an entry without a model string")
                if row.get("hidden") is True:
                    continue
                entries.append(row)
            if len(entries) > CODEX_MAX_MODELS:
                raise ValuesError("codex model/list exceeded the model limit")
            cursor = result.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor or len(cursor) > CODEX_MAX_CURSOR:
                raise ValuesError("codex model/list returned a malformed nextCursor")
            if cursor in seen:
                raise ValuesError("codex model/list repeated a nextCursor")
            seen.add(cursor)
        else:
            raise ValuesError(f"codex model/list did not finish within {CODEX_MAX_PAGES} pages")
        if not entries:
            raise ValuesError("the codex catalog listed no models")
        return entries
    finally:
        server.close()


def _unique(items) -> list[str]:
    return list(dict.fromkeys(items))


NO_EFFORTS_NOTE = "the catalog lists no effort options"


def _entry_efforts(row: dict) -> list[str]:
    rows = row.get("supportedReasoningEfforts")
    found = [
        item["reasoningEffort"] for item in rows
        if isinstance(item, dict) and isinstance(item.get("reasoningEffort"), str)
        and item["reasoningEffort"].strip()
    ] if isinstance(rows, list) else []
    if not isinstance(rows, list) or len(found) != len(rows):
        raise ValuesError(f"codex model {row['model']!r} has malformed supportedReasoningEfforts")
    return found


def _codex_models() -> tuple[list[str], str | None]:
    return _unique(row["model"] for row in _codex_catalog()), CODEX_CATALOG_NOTE


def _codex_efforts(model: str | None) -> tuple[list[str], str | None]:
    catalog = _codex_catalog()
    if model is not None:
        rows = [row for row in catalog if row["model"] == model]
        if not rows:
            raise ValuesError(
                f"codex does not list model {model!r} (see `r4t engine codex models`)"
            )
        found = _unique(e for row in rows for e in _entry_efforts(row))
        note = CODEX_CATALOG_NOTE if found else f"{NO_EFFORTS_NOTE} for {model!r}; " + CODEX_CATALOG_NOTE
        return found, note
    union = _unique(e for row in catalog for e in _entry_efforts(row))
    if not union:
        return [], f"{NO_EFFORTS_NOTE} for any model; " + CODEX_CATALOG_NOTE
    return union, (
        "effort support varies by model; this is the union across the codex "
        "catalog, so use --model MODEL for one model's efforts. "
        + CODEX_CATALOG_NOTE
    )


def _ollama_models() -> list[str]:
    rows = _lines(["ollama", "list"])
    return [row.split()[0] for row in rows[1:] if row.strip()]


def has_models(preset: str) -> bool:
    key = (preset or "").strip().lower()
    return key in HARNESS_PRESETS and key not in NO_LIST_VERB


def models(preset: str, model: str | None = None) -> tuple[list[str], str | None]:
    key, entry = _preset(preset)
    if model is not None:
        raise _no_model_filter(key, "models")
    if key == "codex":
        return _codex_models()
    if key in NO_LIST_VERB:
        raise ValuesError(f"{key} has no list verb; {NO_LIST_VERB[key]}")
    if key == "claude":
        return _claude_models()
    if key == "agy":
        values = _agy_models()
    elif key == "cursor":
        values = _cursor_models(entry["invoke"][0])
    elif key == "devin":
        values = _devin_models()
    elif key == "muse":
        values = _muse_models()
    else:
        values = _ollama_models()
    if not values:
        raise ValuesError(f"the {key} CLI listed no models")
    return values, None


def verbs(preset: str) -> list[str]:
    key = (preset or "").strip().lower()
    entry = HARNESS_PRESETS.get(key, {})
    return (["efforts"] if entry.get("effort_argv") else []) + (
        ["models"] if has_models(key) else []
    )
