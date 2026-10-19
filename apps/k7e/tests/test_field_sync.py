"""The field sync copies a far end read-only and checks its own counts."""
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

SYNC = Path(__file__).resolve().parent / "eval" / "field" / "sync.py"
HEX = "0123456789abcdef0123"

FAKE_A8S = """#!{python}
import json, os, sys
far = os.environ["FAKE_FAR"]
args = sys.argv[1:]
if args[0] == "ls":
    print(open(far + "/ls.txt").read(), end="")
elif args[0] == "vars":
    values = json.load(open(far + "/vars.json")).get(args[1], {{}})
    if not values:
        print(args[1] + ": (no vars)")
    for k, v in values.items():
        print(k + "  " + v)
elif args[0] == "convo":
    agent, since = args[1], args[args.index("--since") + 1]
    rows = [l for l in open(far + "/convo-" + agent + ".jsonl").read().splitlines() if l]
    if not since[0].isdigit() or since[:4] != "1970":
        ids = [json.loads(l)["ulid"] for l in rows]
        rows = rows[ids.index(since) + 1:]
    counter = far + "/calls-" + agent
    calls = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(calls + 1))
    if os.path.exists(far + "/lie-" + agent) and calls % 2 == 1:
        rows.append(json.dumps({{"ulid": "ZZZ"}}))
    if os.path.exists(far + "/break-count-" + agent) and calls % 2 == 1:
        sys.stderr.write("archive locked\\n")
        sys.exit(7)
    if rows:
        print("\\n".join(rows))
else:
    sys.exit(2)
"""

# Takes what ssh takes: options with a value (-l user, -o k=v, -p port,
# -i file), bare flags, then the host, then the command. Linux rsync runs
# `ssh -l user host ...`, macOS rsync `ssh user@host ...`; both must work.
FAKE_SSH = """#!/bin/sh
while [ $# -gt 0 ]; do
  case "$1" in
    -l|-o|-p|-i) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
shift
exec sh -c "$*"
"""


@pytest.fixture
def sync():
    spec = importlib.util.spec_from_file_location("field_sync", SYNC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def far(tmp_path, monkeypatch):
    root = tmp_path / "far"
    root.mkdir()
    a8s = root / "a8s"
    a8s.write_text(FAKE_A8S.format(python=sys.executable))
    a8s.chmod(0o755)
    ssh = tmp_path / "fake-ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(0o755)
    monkeypatch.setenv("FAKE_FAR", str(root))
    (root / "vars.json").write_text("{}")
    (root / "ls.txt").write_text("")
    return Far(tmp_path, root, a8s, ssh)


class Far:
    def __init__(self, tmp, root, a8s, ssh):
        self.tmp, self.root, self.a8s, self.ssh = tmp, root, a8s, ssh
        self.dest = tmp / "corpus"
        self.r4t = root / "r4t"
        self.memory = {}
        self.names = []

    def agent(self, name, memory="on", rows=0, turns=0):
        self.names.append(name)
        self.memory[name] = memory
        (self.root / "vars.json").write_text(json.dumps(
            {n: ({"MEMORY": self.memory[n]} if self.memory[n] else {}) for n in self.names}))
        (self.root / "ls.txt").write_text("\n".join(self.names) + "\n")
        self.add_rows(name, rows)
        self.add_turns(name, turns)

    def add_rows(self, name, count):
        path = self.root / f"convo-{name}.jsonl"
        have = len(path.read_text().splitlines()) if path.exists() else 0
        with open(path, "a") as f:
            for i in range(have, have + count):
                f.write(json.dumps({"ulid": f"R{i:06d}", "from": "bob", "to": name,
                                    "content": f"m{i}"}) + "\n")

    def add_turns(self, name, count):
        d = self.r4t / "engine-memory" / f"{name}-{HEX}" / "turns"
        d.mkdir(parents=True, exist_ok=True)
        have = len(list(d.glob("*.json")))
        for i in range(have, have + count):
            (d / f"T{i:06d}.json").write_text(json.dumps({"format": "r4t-memory-turn-v1"}))

    def run(self, sync, *extra, host="user@box"):
        return sync.main(["--dest", str(self.dest), "--host", host, "--a8s", str(self.a8s),
                          "--r4t-home", str(self.r4t), "--ssh", str(self.ssh), *extra])

    def out(self, name, host="user@box"):
        return self.dest / host.replace("@", "-") / name


def manifest(path):
    return json.loads((path / "sync.json").read_text())


needs_rsync = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync is not installed")


@needs_rsync
def test_first_pull_drains_everything_and_writes_the_manifest(sync, far, capsys):
    far.agent("alice", rows=12, turns=3)
    assert far.run(sync, "--agent", "alice") == 0
    out = far.out("alice")
    assert len((out / "convo.jsonl").read_text().splitlines()) == 12
    assert len(list((out / "turns").glob("*.json"))) == 3
    m = manifest(out)
    assert (m["agent"], m["host"], m["cursor"]) == ("alice", "user@box", "R000011")
    rec = m["pulls"][0]
    assert (rec["memory"], rec["rows_added"], rec["rows_total"]) == ("on", 12, 12)
    assert (rec["turns_added"], rec["turns_total"], rec["verified"]) == (3, 3, True)
    assert rec["cursor_before"] is None and rec["cursor_after"] == "R000011"
    assert capsys.readouterr().out.strip() == (
        "alice: memory on, +12 rows (12), +3 turns (3), verified")


@needs_rsync
def test_second_pull_adds_only_what_is_new(sync, far):
    far.agent("alice", rows=5, turns=2)
    assert far.run(sync, "--agent", "alice") == 0
    far.add_rows("alice", 4)
    far.add_turns("alice", 1)
    assert far.run(sync, "--agent", "alice") == 0
    out = far.out("alice")
    ids = [json.loads(l)["ulid"] for l in (out / "convo.jsonl").read_text().splitlines()]
    assert ids == [f"R{i:06d}" for i in range(9)]
    first, second = manifest(out)["pulls"]
    assert second["rows_added"] == 4 and second["rows_total"] == 9
    assert second["turns_added"] == 1 and second["turns_total"] == 3
    assert second["cursor_before"] == "R000004" and second["verified"]


@needs_rsync
def test_a_pull_with_nothing_new_is_still_verified(sync, far):
    far.agent("alice", rows=3, turns=1)
    far.run(sync, "--agent", "alice")
    assert far.run(sync, "--agent", "alice") == 0
    assert manifest(far.out("alice"))["pulls"][1]["rows_added"] == 0


def test_a_stored_row_is_not_appended_twice(sync, far):
    far.agent("alice", memory="off", rows=3)
    far.run(sync, "--agent", "alice")
    path = far.out("alice") / "convo.jsonl"
    first = path.read_text().splitlines()[0]
    far.add_rows("alice", 1)
    (far.root / "convo-alice.jsonl").write_text(
        first + "\n" + (far.root / "convo-alice.jsonl").read_text())
    far.run(sync, "--agent", "alice")
    ids = [json.loads(l)["ulid"] for l in path.read_text().splitlines()]
    assert len(ids) == len(set(ids))


def test_memory_off_skips_turns_and_records_off(sync, far, capsys):
    far.agent("bob", memory="off", rows=4, turns=2)
    assert far.run(sync, "--agent", "bob") == 0
    out = far.out("bob")
    assert not (out / "turns").exists()
    assert manifest(out)["pulls"][0]["memory"] == "off"
    assert "turns skipped" in capsys.readouterr().out


def test_a_missing_memory_var_means_off(sync, far):
    far.agent("bob", memory="", rows=1)
    assert far.run(sync, "--agent", "bob") == 0
    assert manifest(far.out("bob"))["pulls"][0]["memory"] == "off"


@needs_rsync
def test_a_row_count_mismatch_fails_that_agent_only(sync, far, capsys):
    far.agent("alice", memory="off", rows=3)
    far.agent("bob", memory="off", rows=2)
    (far.root / "lie-alice").write_text("")
    assert far.run(sync) == 1
    lines = capsys.readouterr().out.splitlines()
    assert "NOT VERIFIED" in lines[0] and "far end 4, appended 3" in lines[0]
    assert lines[1].endswith("verified") and "NOT" not in lines[1]
    assert manifest(far.out("alice"))["pulls"][0]["verified"] is False
    assert manifest(far.out("bob"))["pulls"][0]["verified"] is True


@needs_rsync
def test_a_pruned_far_end_keeps_local_turns_and_verifies(sync, far):
    far.agent("alice", rows=1, turns=3)
    far.run(sync, "--agent", "alice")
    d = far.r4t / "engine-memory" / f"alice-{HEX}" / "turns"
    next(d.glob("*.json")).unlink()
    assert far.run(sync, "--agent", "alice") == 0
    assert manifest(far.out("alice"))["pulls"][1]["turns_total"] == 3


def test_a_dest_inside_the_repo_is_refused_before_anything_exists(sync, far):
    far.dest = sync.WT / "apps" / "k7e" / "tests" / "scratch-field"
    assert far.run(sync, "--agent", "alice") == 2
    assert not far.dest.exists()


def test_a_bad_agent_name_is_refused_before_anything_exists(sync, far):
    far.agent("alice", rows=1)
    for bad in ("../victim", "a/b", "-x", ".."):
        assert far.run(sync, "--agent", "alice", "--agent", bad) == 2
    assert not far.dest.exists()


def test_a_symlinked_agent_dir_pointing_into_the_repo_is_refused(sync, far):
    base = far.dest / "user-box"
    base.mkdir(parents=True)
    os.symlink(sync.WT / "apps" / "k7e", base / "alice")
    assert far.run(sync, "--agent", "alice") == 2


def test_dry_run_creates_nothing(sync, far, capsys):
    far.agent("alice", rows=2, turns=1)
    assert far.run(sync, "--agent", "alice", "--dry-run") == 0
    assert not far.dest.exists()
    assert "would run" in capsys.readouterr().out
    assert not (far.root / "calls-alice").exists()


def test_an_unreachable_host_is_one_failure_and_no_files(sync, far, capsys):
    far.agent("alice", rows=2)
    bad = far.tmp / "bad-ssh"
    bad.write_text("#!/bin/sh\nexit 255\n")
    bad.chmod(0o755)
    far.ssh = bad
    assert far.run(sync, "--agent", "alice") == 1
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and "unreachable" in out[0]
    assert not far.dest.exists()


def test_agents_come_from_the_far_end_when_none_is_named(sync, far, capsys):
    far.agent("alice", memory="off", rows=1)
    far.agent("bob", memory="off", rows=2)
    assert far.run(sync, "--json") == 0
    rows = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert [r["agent"] for r in rows] == ["alice", "bob"]


def test_the_shim_takes_the_options_linux_rsync_passes(far):
    import subprocess
    done = subprocess.run([str(far.ssh), "-o", "BatchMode=yes", "-l", "user", "box", "echo", "hi"],
                          capture_output=True, text=True)
    assert done.returncode == 0 and done.stdout == "hi\n"
    done = subprocess.run([str(far.ssh), "user@box", "echo hi there"], capture_output=True, text=True)
    assert done.stdout == "hi there\n"


def test_a_failed_count_read_is_a_failure_not_a_verified_pull(sync, far, capsys):
    far.agent("alice", memory="off", rows=2)
    far.agent("bob", memory="off", rows=1)
    far.run(sync)
    capsys.readouterr()
    (far.root / "break-count-alice").write_text("")
    assert far.run(sync) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("alice: FAILED") and "archive locked" in lines[0]
    assert lines[1].endswith("verified")
    assert len(manifest(far.out("alice"))["pulls"]) == 1
    assert len(manifest(far.out("bob"))["pulls"]) == 2


@needs_rsync
def test_a_failed_turns_count_is_a_failure(sync, far, capsys):
    far.agent("alice", rows=1, turns=2)
    shutil.rmtree(far.r4t / "engine-memory" / f"alice-{HEX}" / "turns")
    assert far.run(sync, "--agent", "alice") == 1
    assert "could not count far-end turns" in capsys.readouterr().out
    assert not far.out("alice").exists()


@needs_rsync
def test_memory_on_with_no_home_is_not_verified(sync, far):
    far.agent("alice", rows=1)
    shutil.rmtree(far.r4t / "engine-memory" / f"alice-{HEX}")
    assert far.run(sync, "--agent", "alice") == 1
    pull = manifest(far.out("alice"))["pulls"][0]
    assert pull["verified"] is False and "no memory home" in pull["note"]


@needs_rsync
def test_a_memory_home_with_a_space_in_its_path_is_pulled_whole(sync, far):
    far.r4t = far.tmp / "r4t home"
    far.agent("alice", rows=1, turns=2)
    assert far.run(sync, "--agent", "alice") == 0
    assert manifest(far.out("alice"))["pulls"][0]["turns_total"] == 2


FAKE_RSYNC = """#!/bin/sh
if [ "$1" = --version ]; then echo "$FAKE_RSYNC_VERSION"; exit 0; fi
printf '%s\\n' "$@" > "$FAKE_RSYNC_ARGV"
"""


@pytest.mark.parametrize("version, protect, source", [
    ("openrsync: protocol version 29", [], "user@box:'{home}'/turns/"),
    ("rsync  version 3.2.7  protocol version 31", ["-s"], "user@box:{home}/turns/"),
])
def test_the_source_path_is_quoted_for_openrsync_and_protected_for_rsync(
        sync, far, monkeypatch, version, protect, source):
    far.r4t = far.tmp / "r4t home"
    far.agent("alice", rows=1, turns=2)
    rsync = far.tmp / "fake-rsync"
    rsync.write_text(FAKE_RSYNC)
    rsync.chmod(0o755)
    monkeypatch.setenv("FAKE_RSYNC_VERSION", version)
    monkeypatch.setenv("FAKE_RSYNC_ARGV", str(far.tmp / "argv"))
    far.run(sync, "--agent", "alice", "--rsync", str(rsync))
    home = far.r4t / "engine-memory" / f"alice-{HEX}"
    argv = (far.tmp / "argv").read_text().splitlines()
    assert argv == ["-a", "--ignore-existing", *protect, "-e", str(far.ssh),
                    source.format(home=home), f"{far.out('alice') / 'turns'}/"]
