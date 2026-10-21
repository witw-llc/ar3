"""The one place this program talks to a browser.

Everything the driver does to a page is an the browser tool command script run in
one seat, and every one of those runs goes through `BrowserSeat.run`. Nothing
else in this codebase starts a process, so a test hands the rest of the code a
fake with the same two-method surface and the whole driver runs with no
browser and no network.

The seat is the browser tool's, not this program's: it owns the Chrome profile a
person signed in to Gemini by hand. This module knows how to ask it to run a
script and how to read the transcript it answers with. It knows nothing about
Gemini.
"""
import json
import os
import subprocess
import sys

# The browser tool, by its path inside the suite: never PATH, never a separate
# install.
BROWSER_TOOL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "browser"
)
RUN_TIMEOUT = 300


class BrowserError(Exception):
    """The seat could not be driven — not "the page said no"."""


class Transcript:
    """One script's run: what each step did, and what it produced.

    The shape is the browser tool's `do --json` payload, which is the same `Run`
    object its own wake handler replies with.
    """

    def __init__(self, ok, steps, files):
        self.ok = ok
        self.steps = steps
        self.files = files

    @classmethod
    def from_payload(cls, payload):
        return cls(
            ok=bool(payload.get("ok")),
            steps=list(payload.get("steps") or []),
            files=list(payload.get("files") or []),
        )

    @property
    def error(self):
        for step in self.steps:
            if not step.get("ok"):
                return step.get("error") or f"{step.get('command')} failed"
        return None

    def outputs(self, verb):
        """Outputs of the successful steps that ran `verb`, in script order.

        A step that produced nothing is an empty string rather than a gap:
        callers line the list up against the selectors they asked for.
        """
        found = []
        for step in self.steps:
            command = str(step.get("command") or "")
            if command.split(None, 1)[:1] == [verb] and step.get("ok"):
                found.append(str(step.get("output") or ""))
        return found


class BrowserSeat:
    """Runs command scripts in one the browser tool seat."""

    def __init__(self, seat, timeout=RUN_TIMEOUT):
        self.seat = seat
        self.timeout = timeout

    def run(self, script):
        argv = [sys.executable, BROWSER_TOOL, "-s", self.seat, "do", "-", "--json"]
        try:
            proc = subprocess.run(
                argv, input=script, capture_output=True, text=True, timeout=self.timeout
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            if isinstance(exc, OSError):
                raise BrowserError(f"the browser tool could not be run ({exc})") from None
            raise BrowserError(
                f"browser seat {self.seat!r} did not finish within {self.timeout}s"
            ) from None
        return self._parse(proc)

    def _parse(self, proc):
        """The JSON payload, whatever else the run printed.

        A nonzero exit is not an error here: `do` exits 1 for a script that
        failed a step, and that transcript is exactly what the caller needs to
        read. Only an answer with no payload in it is a failure of the seat.
        """
        for line in reversed((proc.stdout or "").splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if isinstance(payload, dict) and "steps" in payload:
                return Transcript.from_payload(payload)
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise BrowserError(
            f"browser seat {self.seat!r} returned no run (exit {proc.returncode}): "
            + (detail[-1] if detail else "no output")
        )
