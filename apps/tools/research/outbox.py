"""Working-directory reply outbox (`.research/pending/`) for tool-research.

A deep-research run spends paid API calls. If `tell` fails transiently when
delivering the finished report, the report and its attachments are held in
`<workdir>/.research/pending/` and retried on the next wake or `idle` hook.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from ar3.locking import file_lock

CLAIM = ".claim-"
FILES_SUFFIX = ".files"
RECLAIM_SECONDS = 300.0


def _files_dir(entry_path: Path) -> Path:
    base = str(entry_path).split(CLAIM, 1)[0]
    if base.endswith(".json"):
        base = base[: -len(".json")]
    return Path(base + FILES_SUFFIX)


def _held_files(entry_path: Path, names: list[str] | None) -> list[str]:
    directory = _files_dir(entry_path)
    found: list[str] = []
    for name in names or ():
        candidate = directory / os.path.basename(str(name))
        if candidate.is_file():
            found.append(str(candidate))
    return found


class Outbox:
    """Undelivered replies stored in `<workdir>/.research/pending/`."""

    def __init__(self, workdir: Path):
        self.workdir = Path(workdir).resolve()
        self.path = self.workdir / ".research" / "pending"
        self.lock_path = self.workdir / ".research" / ".pending.lock"

    def keep(self, recipient: str, body: str, files: tuple[str, ...] | list[str] = ()) -> Path:
        self.path.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self.path),
            prefix=f"{time.time():014.3f}-",
            suffix=".tmp",
            delete=False,
        )
        tmp_path = Path(handle.name)
        final = Path(str(tmp_path)[: -len(".tmp")] + ".json")
        kept, lost = self._copy_files(_files_dir(final), files)
        if lost:
            body = body.rstrip() + "\n\n--\n" + "\n".join(lost)
        payload = {
            "to": recipient,
            "body": body,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "files": kept,
        }
        try:
            with handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
            os.replace(tmp_path, final)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        return final

    @staticmethod
    def _copy_files(directory: Path, files: tuple[str, ...] | list[str]) -> tuple[list[str], list[str]]:
        kept: list[str] = []
        lost: list[str] = []
        for source in files or ():
            name = os.path.basename(str(source))
            try:
                directory.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(str(source), str(directory / name))
            except OSError as exc:
                lost.append(f"{name} could not be held for redelivery ({exc}).")
                continue
            kept.append(name)
        return kept, lost

    def waiting(self) -> list[tuple[Path, str, str, list[str]]]:
        if not self.path.is_dir():
            return []
        try:
            names = sorted(p.name for p in self.path.iterdir() if p.name.endswith(".json"))
        except OSError:
            return []
        held: list[tuple[Path, str, str, list[str]]] = []
        for name in names:
            full = self.path / name
            try:
                payload = json.loads(full.read_text(encoding="utf-8"))
                held.append(
                    (
                        full,
                        str(payload["to"]),
                        str(payload["body"]),
                        _held_files(full, payload.get("files")),
                    )
                )
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return held

    def flush(self, send) -> tuple[int, int]:
        """Deliver held replies in order. Returns `(delivered, remaining)`."""
        if not self.path.is_dir():
            return 0, 0
        try:
            with file_lock(self.lock_path, blocking=False):
                self._recover()
                return self._send_each(send)
        except BlockingIOError:
            return 0, len(self.waiting())
        except OSError:
            return 0, len(self.waiting())

    def _send_each(self, send) -> tuple[int, int]:
        delivered = 0
        for entry_path, recipient, body, files in self.waiting():
            claimed = self._claim(entry_path)
            if claimed is None:
                continue
            try:
                ok = int(send(recipient, body, files) or 0) == 0
            except OSError:
                self._restore(claimed, entry_path)
                break
            except BaseException:
                self._restore(claimed, entry_path)
                raise
            if not ok:
                self._restore(claimed, entry_path)
                break
            self.drop(claimed)
            delivered += 1
        return delivered, len(self.waiting())

    @staticmethod
    def _claim(entry_path: Path) -> Path | None:
        claimed = Path(
            f"{entry_path}{CLAIM}{time.time():.3f}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        )
        try:
            os.replace(entry_path, claimed)
        except OSError:
            return None
        return claimed

    @staticmethod
    def _claimed_at(name: str) -> float | None:
        try:
            return float(name.split(CLAIM, 1)[1].split("-", 1)[0])
        except (IndexError, ValueError):
            return None

    def _recover(self) -> None:
        try:
            entries = list(self.path.iterdir())
        except OSError:
            return
        for item in entries:
            if CLAIM not in item.name:
                continue
            taken = self._claimed_at(item.name)
            if taken is not None and time.time() - taken < RECLAIM_SECONDS:
                continue
            target = self.path / item.name.split(CLAIM, 1)[0]
            try:
                os.replace(item, target)
            except OSError:
                continue

    @staticmethod
    def _restore(claimed: Path, original: Path) -> None:
        try:
            os.replace(claimed, original)
        except OSError:
            pass

    @staticmethod
    def drop(entry_path: Path) -> None:
        shutil.rmtree(_files_dir(entry_path), ignore_errors=True)
        entry_path.unlink(missing_ok=True)
