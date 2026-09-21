"""Private, portable memory and bounded background distillation for engine turns."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))

from ar3.fsio import atomic_write_text
from ar3.locking import file_lock
from ar3.proc import terminate_group
from ar3.ulid import new as new_ulid
from dispatch import class_from_meta
import knowledge
import state
from rig import knowledge_tier_bytes, load_rig_config, resolve_config_path
from roster import KNOWLEDGE_SIZES

WORKER_SECONDS = 240
JOB_SECONDS = 180
WORKER_BATCH = 5
JOB_ATTEMPTS = 3
JOB_FLOOR_SECONDS = 60


def note(message):
    print(f"r4t memory: {message}", file=sys.stderr)


def store_home(agent, directory, override=None):
    identity = {"agent": agent.strip().casefold()} if agent else {
        "directory": os.path.normcase(str(Path(directory).resolve()))
    }
    if agent is not None and not identity.get("agent"):
        raise ValueError("memory requires a non-empty agent name")
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20]
    label = re.sub(r"[^a-z0-9_.-]", "-", identity.get("agent", "directory"))[:48]
    home = Path(override).expanduser().resolve() if override else state.r4t_home() / "engine-memory" / f"{label}-{digest}"
    with file_lock(home / ".identity.lock"):
        path = home / "identity.json"
        if path.exists():
            if json.loads(path.read_text(encoding="utf-8")) != identity:
                raise ValueError(f"memory home {home} belongs to a different agent or directory")
        else:
            atomic_write_text(path, json.dumps(identity), fsync=True, mode=0o600)
    return home


def node_identity(agent, env):
    # a8s expands $RECIPIENT to the envelope's `to` (an alias or node:member) on
    # message wakes but to the node on batch and idle wakes; the wake env names
    # the node on all three.
    return env.get("A8S_TURN_RECIPIENT") or agent


def routed_context(message, people, env):
    if "A8S_TURN_RECIPIENT" not in env:
        return message, [message], [{"from": "operator", "content": message}]
    paths = json.loads(env.get("A8S_TURN_ENVELOPES", "[]"))
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise ValueError("invalid routed envelope paths")
    trusted = {name.strip().casefold() for name in (people or "").split(",") if name.strip()}
    messages, human = [], []
    for path in paths:
        item = json.loads(Path(path).read_text(encoding="utf-8"))
        sender, body = item.get("from"), item.get("content")
        if not isinstance(sender, str) or not isinstance(body, str):
            raise ValueError("invalid routed sender or content")
        messages.append({"from": sender, "content": body, "id": item.get("id")})
        if sender.casefold() in trusted and class_from_meta(json.dumps(item.get("meta") or {})) == "human":
            human.append(body)
    query = next((m["content"] for m in reversed(messages) if m["content"].strip()), "")
    return query, human, messages


def kick_idle(args, directory):
    if getattr(args, "memory", "off") != "off":
        try:
            home = store_home(node_identity(args.agent, os.environ), directory,
                              getattr(args, "memory_home", None))
            kick(home)
        except (OSError, ValueError) as exc:
            note(f"idle skipped: {exc}")


class Turn:
    def __init__(self, *, engine, model, effort, agent, directory, message,
                 mode, home=None, writer=None, people=None, env=None, rig_context=None):
        self.env = dict(os.environ if env is None else env)
        self.home = store_home(node_identity(agent, self.env), directory, home)
        self.message = message
        self.directory = Path(directory).resolve()
        self.engine, self.model, self.effort = engine, model, effort
        self.writer = writer
        self.rig_context = rig_context
        self.stamp = new_ulid()
        self.budget = knowledge_tier_bytes(engine) if mode == "on" else KNOWLEDGE_SIZES[mode]
        try:
            self.query, self.human, self.messages = routed_context(message, people, self.env)
        except (OSError, ValueError, TypeError) as exc:
            note(f"sender metadata unavailable; corrections disabled: {exc}")
            self.query, self.human, self.messages = message, [], []
        self.pack = knowledge.KnowledgePack()
        kick(self.home)

    def inject(self, prompt):
        self.pack = knowledge.retrieve(self.home, self.query, self.budget, log=note)
        text = "\n".join(self.pack)
        pending = len(list((self.home / "queue").glob("*.json")))
        failed = len(list((self.home / "queue" / "failed").glob("*.json")))
        note(f"home={self.home}; {len(self.pack.ids)} entries, {len(text.encode())}B; "
             f"{pending} pending, {failed} failed")
        failure = self.home / "failure.json"
        if failure.exists():
            try:
                error = json.loads(failure.read_text(encoding="utf-8"))["error"]
            except (OSError, ValueError, KeyError, TypeError):
                error = "failure record unreadable; inspect worker.log"
            note(f"distillation pending: {error}")
        return text + "\n\n" + prompt if text else prompt

    def finish(self, output, exit_code):
        capture = {
            "format": "r4t-memory-turn-v1", "stamp": self.stamp,
            "root": str(self.directory), "input": self.message, "output": output,
            "knowledge": self.pack.ids, "human_messages": self.human,
            "messages": self.messages, "exit": exit_code,
            "writer": {"engine": self.engine, "model": self.model,
                       "effort": self.effort, "rig": self.writer,
                       "context": self.rig_context},
        }
        path = self.home / "turns" / f"{self.stamp}.json"
        atomic_write_text(path, json.dumps(capture, ensure_ascii=False), fsync=True, mode=0o600)
        if exit_code == 0:
            atomic_write_text(self.home / "queue" / path.name,
                              json.dumps({"capture": path.name}), fsync=True, mode=0o600)
            kick(self.home)


def kick(home):
    if not any((home / "queue").glob("*.json")):
        return
    home.mkdir(parents=True, exist_ok=True)
    with (home / "worker.log").open("ab") as log:
        flags = {}
        if os.name == "nt":
            flags["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            flags["start_new_session"] = True
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "worker", str(home)],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         close_fds=True, **flags)


def _command(argv):
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def stop_process(proc):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if proc.poll() is None:
            proc.kill()
    else:
        terminate_group(proc)


def memory_argv(argv):
    from engines.run import resolve_argv0
    argv = resolve_argv0(argv)
    if os.name == "nt" and Path(argv[0]).suffix.lower() in (".cmd", ".bat"):
        if any("\n" in arg or "\r" in arg for arg in argv[1:]):
            raise OSError("multiline memory prompts cannot pass through a Windows .cmd/.bat launcher; use a native engine executable (see AR3 issue #230)")
    return argv


def spawn(argv, directory, timeout, env):
    proc = subprocess.Popen(memory_argv(argv), cwd=directory, env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            start_new_session=os.name != "nt")
    chunks = []

    def stream():
        while True:
            data = os.read(proc.stdout.fileno(), 65536)
            if not data:
                break
            chunks.append(data)
            try:
                if hasattr(sys.stdout, "buffer"):
                    sys.stdout.buffer.write(data)
                else:
                    sys.stdout.write(data.decode("utf-8", errors="replace"))
                sys.stdout.flush()
            except (BrokenPipeError, OSError):
                pass

    reader = threading.Thread(target=stream, daemon=True)
    reader.start()
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_process(proc)
        proc.wait(timeout=5)
        note(f"engine turn timed out after {timeout}s")
        code = 124
    except BaseException:
        stop_process(proc)
        proc.wait(timeout=5)
        raise
    finally:
        reader.join(timeout=2)
        if reader.is_alive():
            stop_process(proc)
            reader.join(timeout=2)
        proc.stdout.close()
    return code, b"".join(chunks).decode("utf-8", errors="replace")


def _run_job(home, capture, timeout):
    bridge = _command([sys.executable, str(Path(__file__).resolve()), "writer", str(capture)])
    env = {**os.environ, "K7E_HOME": str(home), "K7E_DISTILL_COMMAND": bridge}
    argv = [sys.executable, str(knowledge.K7E_ENTRY), "distill", str(capture), "--job", capture.stem]
    proc = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=os.name != "nt")
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_process(proc)
        proc.communicate(timeout=5)
        raise RuntimeError("distillation timed out; capture retained")
    if proc.returncode:
        raise RuntimeError(err.decode(errors="replace")[-2000:] or f"distill exit {proc.returncode}")
    note(out.decode(errors="replace").strip())


def worker(home):
    try:
        with file_lock(home / ".worker.lock", blocking=False):
            deadline = time.monotonic() + WORKER_SECONDS
            for _ in range(WORKER_BATCH):
                pending = sorted((home / "queue").glob("*.json"))
                if not pending or deadline - time.monotonic() < JOB_FLOOR_SECONDS:
                    break
                job = pending[0]
                marker = json.loads(job.read_text(encoding="utf-8"))
                marker["attempts"] = marker.get("attempts", 0) + 1
                # Counted before the run, so a job that kills its worker still exhausts.
                atomic_write_text(job, json.dumps(marker), fsync=True, mode=0o600)
                try:
                    # Resolve relative to the portable home, never the old machine's path.
                    capture = home / "turns" / job.name
                    _run_job(home, capture, min(JOB_SECONDS, deadline - time.monotonic()))
                    job.unlink()
                    (home / "failure.json").unlink(missing_ok=True)
                except Exception as exc:
                    atomic_write_text(home / "failure.json", json.dumps({
                        "job": job.name, "error": str(exc), "time": time.time(),
                        "attempts": marker["attempts"],
                    }), fsync=True)
                    note(f"{job.name}: attempt {marker['attempts']}: {exc}")
                    if marker["attempts"] < JOB_ATTEMPTS:
                        return 1
                    (home / "queue" / "failed").mkdir(exist_ok=True)
                    job.replace(home / "queue" / "failed" / job.name)
                    note(f"{job.name}: moved to queue/failed after {JOB_ATTEMPTS} attempts")
            if time.monotonic() < deadline:
                try:
                    knowledge._run_k7e(home, "embed-pending", timeout=min(30, deadline - time.monotonic()))
                except Exception as exc:
                    note(f"embedding backlog retained: {exc}")
    except BlockingIOError:
        return 0
    kick(home)
    return 0


def writer(capture_path):
    from engines.run import build_argv
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    spec = capture["writer"]
    workdir = capture_path.parent.parent / "writer"
    workdir.mkdir(exist_ok=True)
    prompt = sys.stdin.read()
    env = {k: v for k, v in os.environ.items() if not k.startswith("A8S_TURN_")}
    context = spec.get("context") or {}
    config = None
    if context or spec.get("rig"):
        config = load_rig_config(resolve_config_path(context.get("config")))
    if context.get("rig"):
        original = config.rigs.get(context["rig"].lower())
        if original is None or original.error:
            raise ValueError(f"source rig {context['rig']!r} is missing or invalid")
        env.update(original.env)
    if spec.get("rig"):
        rig = config.rigs.get(spec["rig"].lower())
        if rig is None or rig.error:
            raise ValueError(f"memory writer rig {spec['rig']!r} is missing or invalid")
        if rig.preset:
            argv = build_argv(rig.preset, prompt, model=rig.model, effort=rig.effort,
                              timeout=JOB_SECONDS, workdir=workdir,
                              permissions=rig.permissions, allowed_tools=rig.allowed_tools)
        else:
            argv = rig.argv(prompt, workdir=workdir)
        env.update(rig.env)
    else:
        argv = build_argv(spec["engine"], prompt, model=spec["model"], effort=spec.get("effort"),
                          timeout=JOB_SECONDS, workdir=workdir)
    argv = memory_argv(argv)
    os.chdir(workdir)
    if os.name == "nt":
        return subprocess.call(argv, env=env)
    os.execvpe(argv[0], argv, env)


if __name__ == "__main__":
    try:
        mode, path = sys.argv[1:]
        raise SystemExit(worker(Path(path)) if mode == "worker" else writer(Path(path)))
    except Exception as exc:
        note(str(exc))
        raise SystemExit(1)
