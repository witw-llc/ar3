"""The private `hosted` extension: JSON listings and stdin secrets.

Skipped wholesale when `apps/a8s/ext/hosted.py` is absent, as it is in the
public mirror."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

A8S_DIR = Path(__file__).resolve().parent.parent
A8S = A8S_DIR / "a8s.py"
HOSTED = A8S_DIR / "ext" / "hosted.py"

pytestmark = pytest.mark.skipif(not HOSTED.is_file(), reason="ext/hosted.py is private")

SECRET = "s3cret-Value/with+chars"


class Rig:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.env = {**os.environ, "A8S_HOME": str(self.home), "HOME": str(tmp_path)}
        for key in ("TELL_OUTBOX_DIR", "A8S_EXT_DIR"):
            self.env.pop(key, None)

    def a8s(self, *args: str, stdin: str | None = None):
        return subprocess.run(
            [sys.executable, str(A8S), *args],
            env=self.env, capture_output=True, text=True, timeout=60, input=stdin,
        )

    def ok(self, *args: str, stdin: str | None = None):
        r = self.a8s(*args, stdin=stdin)
        assert r.returncode == 0, r.stderr
        return r

    def node(self, name: str, definition: str | None = None) -> Path:
        root = self.tmp / name
        root.mkdir()
        cmd = ["add", name, str(root)]
        if definition:
            cmd.append(definition)
        self.ok(*cmd)
        return root

    def json(self, *args: str):
        return json.loads(self.ok(*args).stdout)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_ls_json_describes_nodes_with_real_directories(rig):
    drop_root = rig.node("zeta-drop", "filedrop")
    plain_root = rig.node("alpha-plain")
    rig.ok("namespace", "pre", "alpha-plain")
    rows = rig.json("ls", "--json")
    assert [r["name"] for r in rows] == ["alpha-plain", "zeta-drop"]
    plain, drop = rows
    assert plain["status"] == "stopped" and plain["pid"] is None
    assert plain["filedrop"] is False
    assert plain["namespaces"] == ["pre"]
    assert plain["root"] == str(plain_root)
    assert drop["filedrop"] is True
    assert drop["definition"] == "filedrop"
    assert drop["namespaces"] == []
    for row in rows:
        assert set(row) == {
            "name", "status", "pid", "definition", "filedrop",
            "root", "inbox", "outbox", "files", "namespaces",
        }
        for key, leaf in (("inbox", ".inbox"), ("outbox", ".outbox"), ("files", ".files")):
            assert row[key] == str(Path(row["root"]) / leaf)
            assert Path(row[key]).is_absolute()


def test_ls_json_matches_the_inboxes_the_router_uses(rig):
    rig.node("alpha")
    sys.path.insert(0, str(A8S_DIR))
    os.environ["A8S_HOME"] = str(rig.home)
    try:
        import registry

        parts = {p.name: p for p in registry.participants_from_registry()}
    finally:
        os.environ.pop("A8S_HOME", None)
    row = rig.json("ls", "--json")[0]
    assert row["inbox"] == str(parts["alpha"].inbox_path())
    assert row["outbox"] == str(parts["alpha"].outbox_path())
    assert row["files"] == str(parts["alpha"].files_path())


def test_ls_json_with_no_nodes_is_an_empty_array(rig):
    assert rig.json("ls", "--json") == []


def test_ls_json_marks_an_unresolved_node(rig):
    root = rig.tmp / "u"
    root.mkdir()
    definition = rig.tmp / "u.json"
    definition.write_text(json.dumps({"invoke": ["true"], "inbox_dir": "$WHERE/in"}))
    rig.ok("add", "u", str(root), str(definition))
    (row,) = rig.json("ls", "--json")
    assert row["status"] == "unresolved"
    assert row["inbox"] == "" and row["outbox"] == "" and row["files"] == ""


def test_ls_without_json_is_unchanged(rig):
    rig.node("alpha")
    out = rig.ok("ls").stdout
    assert out.splitlines()[0].startswith("NAME")
    assert not out.lstrip().startswith("[")


def test_ls_json_lists_names_heard_over_the_broker(rig):
    rig.node("alpha")
    code = (
        "import txlog\n"
        "txlog.log('RECEIVED_REMOTE', msg_id='01HZZZZZZZZZZZZZZZZZZZZZZZ', "
        "sender='far-away', recipient='alpha', remote='broker')\n"
    )
    probe = subprocess.run(
        [sys.executable, "-c", f"import sys; sys.path[:0]=[{str(A8S_DIR)!r}, {str(A8S_DIR.parent.parent / 'lib')!r}]\n{code}"],
        env=rig.env, capture_output=True, text=True,
    )
    assert probe.returncode == 0, probe.stderr
    rows = rig.json("ls", "--json")
    assert [r["name"] for r in rows] == ["alpha", "far-away"]
    heard = rows[1]
    assert heard["status"] == "remote" and heard["definition"] == "remote"
    assert heard["last_heard"].endswith("Z") and "T" in heard["last_heard"]
    assert set(heard) == {"name", "status", "definition", "last_heard"}


def test_remote_json_hides_the_secret_and_tracks_revision(rig):
    empty = rig.json("remote", "--json")
    assert empty["entries"] == []
    first = empty["revision"]
    assert first == hashlib.sha256(b"\0").hexdigest()

    rig.ok("remote", "hub", "mqtt://broker.example:1883", "topic/x",
           "--user", "alice", "--pass", SECRET)
    folder = rig.tmp / "shared"
    folder.mkdir()
    rig.ok("remote", "drive", str(folder))
    res = rig.a8s("remote", "--json")
    assert SECRET not in res.stdout
    data = json.loads(res.stdout)
    by_name = {e["name"]: e for e in data["entries"]}
    hub = by_name["hub"]
    assert hub["kind"] == "mqtt"
    assert hub["password_set"] is True
    assert hub["paired"] is False
    assert hub["options"]["user"] == "alice"
    assert hub["options"]["broker"] == "mqtt://broker.example:1883"
    assert not {"pass", "password", "transport"} & set(hub["options"])
    assert by_name["drive"]["password_set"] is False
    assert data["revision"] != first

    home = rig.home
    expected = hashlib.sha256(
        (home / "network.json").read_bytes() + b"\0" + (home / "secrets.json").read_bytes()
    ).hexdigest()
    assert data["revision"] == expected
    before = data["revision"]
    rig.ok("remote", "hub", "mqtt://broker.example:1883", "topic/x",
           "--user", "alice", "--pass", "another")
    assert rig.json("remote", "--json")["revision"] != before


def test_storage_json_shows_paired_and_password_state(rig, tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    rig.ok("storage", "box", "webdav://dav.example/path",
           "--base-url", "https://dav.example/path", "--user", "u", "--password", SECRET)
    folder = tmp_path / "shared"
    folder.mkdir()
    rig.ok("remote", "drive", str(folder))
    res = rig.a8s("storage", "--json")
    assert res.returncode == 0, res.stderr
    assert SECRET not in res.stdout
    data = json.loads(res.stdout)
    by_name = {e["name"]: e for e in data["entries"]}
    box = by_name["box"]
    assert box["kind"] == "webdav"
    assert box["password_set"] is True and box["paired"] is False
    assert box["options"]["url"] == "webdav://dav.example/path"
    assert not {"password", "service", "paired"} & set(box["options"])
    paired = [e for e in data["entries"] if e["paired"]]
    for entry in paired:
        assert entry["password_set"] is False
        assert "paired" not in entry["options"]


@pytest.mark.parametrize("verb", ["remote", "storage"])
def test_json_stays_json_when_a_stored_secret_is_unbound(rig, tmp_path, verb):
    rig.ok("remote", "hub", "mqtt://broker.example", "t", "--pass", SECRET)
    rig.ok("storage", "box", "webdav://dav.example/path",
           "--base-url", "https://dav.example/path", "--password", SECRET)
    secrets_path = rig.home / "secrets.json"
    if not secrets_path.is_file():
        secrets_path = next(rig.home.rglob("secrets.json"))
    secrets = json.loads(secrets_path.read_text())
    for section in ("remotes", "services"):
        for entry in secrets[section].values():
            entry.pop("bind", None)
    secrets_path.write_text(json.dumps(secrets))

    res = rig.a8s(verb, "--json")
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    name = "hub" if verb == "remote" else "box"
    assert [e["password_set"] for e in data["entries"] if e["name"] == name] == [False]
    assert repr(name) in res.stderr and "WARN" in res.stderr
    assert SECRET not in res.stdout + res.stderr


def test_remote_and_storage_listings_without_json_are_unchanged(rig):
    assert "no remotes configured" in rig.ok("remote").stdout
    assert "no storage services configured" in rig.ok("storage").stdout


def _load_hosted():
    sys.path.insert(0, str(A8S_DIR))
    spec = importlib.util.spec_from_file_location("hosted_under_test", HOSTED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("verb", ["remote", "storage"])
def test_pass_dash_reads_stdin_and_core_never_sees_a_dash(verb, monkeypatch):
    hosted = _load_hosted()
    seen = []
    monkeypatch.setattr(hosted, f"cmd_{verb}", lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr(sys, "stdin", io.StringIO(SECRET + "\nignored second line\n"))
    argv = ["name", "target", "--user", "u", "--pass", "-", "--region", "r"]
    assert hosted.handle(verb, argv) == 0
    assert seen == [["name", "target", "--user", "u", f"--pass={SECRET}", "--region", "r"]]
    assert argv.count("-") == 1


def test_pass_dash_handles_the_equals_and_password_spellings(monkeypatch):
    hosted = _load_hosted()
    seen = []
    monkeypatch.setattr(hosted, "cmd_storage", lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr(sys, "stdin", io.StringIO("pw\r\n"))
    assert hosted.handle("storage", ["n", "u", "--password=-"]) == 0
    assert seen == [["n", "u", "--password=pw"]]


def test_pass_dash_leaves_other_arguments_to_core(monkeypatch):
    hosted = _load_hosted()
    assert hosted.handle("remote", ["name", "target", "--pass", "inline"]) is None
    assert hosted.handle("storage", []) is None
    assert hosted.handle("tell", ["--pass", "-"]) is None


@pytest.mark.parametrize("stdin", ["", "\n"])
def test_pass_dash_with_no_secret_is_an_error(rig, stdin):
    r = rig.a8s("remote", "hub", "mqtt://b.example:1883", "t", "--pass", "-", stdin=stdin)
    assert r.returncode == 2
    assert "--pass -" in r.stderr
    assert not (rig.home / "secrets.json").exists()


def test_pass_dash_writes_the_same_secret_as_the_inline_form(rig, tmp_path):
    args = ["remote", "hub", "mqtt://b.example:1883", "t", "--user", "alice"]
    inline = rig.ok(*args, "--pass", SECRET)
    stored_inline = (rig.home / "secrets.json").read_text()
    (rig.home / "secrets.json").unlink()
    (rig.home / "network.json").unlink()
    piped = rig.ok(*args, "--pass", "-", stdin=SECRET + "\n")
    assert (rig.home / "secrets.json").read_text() == stored_inline
    assert json.loads(stored_inline)["remotes"]["hub"]["pass"] == SECRET
    assert SECRET not in piped.stdout + piped.stderr
    assert piped.stdout == inline.stdout


def test_storage_pass_dash_writes_the_same_secret_as_the_inline_form(rig):
    args = ["storage", "box", "webdav://dav.example/p", "--base-url", "https://dav.example/p"]
    rig.ok(*args, "--user", "u", "--pass", SECRET)
    stored_inline = (rig.home / "secrets.json").read_text()
    (rig.home / "secrets.json").unlink()
    (rig.home / "network.json").unlink()
    rig.ok(*args, "--user", "u", "--pass", "-", stdin=SECRET + "\n")
    assert (rig.home / "secrets.json").read_text() == stored_inline
    assert json.loads(stored_inline)["services"]["box"]["password"] == SECRET


def test_max_file_bytes_is_a_core_setting(rig):
    out = rig.ok("config", "get", "max_file_bytes").stdout.strip()
    assert out.isdigit()


@pytest.mark.parametrize("verb,section", [("remote", "remotes"), ("storage", "services")])
def test_json_revision_belongs_to_the_entries_it_carries(rig, monkeypatch, capsys, verb, section):
    rig.ok("remote", "hub", "mqtt://old.example", "t")
    rig.ok("storage", "box", "webdav://old.example/path", "--base-url", "https://old.example/path")
    monkeypatch.setenv("A8S_HOME", str(rig.home))
    hosted = _load_hosted()
    net = next(rig.home.rglob("network.json"))
    sec = net.parent / "secrets.json"
    before = net.read_bytes()
    expected = hashlib.sha256(before + b"\0" + (sec.read_bytes() if sec.is_file() else b"")).hexdigest()
    real = hosted.merge_spec_secrets
    state = {"done": False}

    def writer_then_merge(*args, **kwargs):
        if not state["done"]:
            state["done"] = True
            net.write_text(before.decode().replace("old.example", "new.example"))
        return real(*args, **kwargs)

    monkeypatch.setattr(hosted, "merge_spec_secrets", writer_then_merge)
    args = ("remotes", "transport", {"transport"}) if verb == "remote" else (
        "services", "service", {"service", "paired"})
    assert hosted._network(*args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["revision"] == expected
    assert "old.example" in json.dumps(first["entries"])
    assert "new.example" not in json.dumps(first["entries"])

    assert hosted._network(*args) == 0
    second = json.loads(capsys.readouterr().out)
    assert "new.example" in json.dumps(second["entries"])
    assert second["revision"] != first["revision"]


@pytest.mark.parametrize("which", ["secrets.json", "network.json"])
def test_json_stays_one_object_on_stdout_when_a_config_file_is_malformed(rig, which):
    rig.ok("remote", "hub", "mqtt://broker.example", "t")
    (next(rig.home.rglob("network.json")).parent / which).write_text("{")
    res = rig.a8s("remote", "--json")
    assert res.returncode == 0, res.stderr
    assert "revision" in json.loads(res.stdout)
    assert "WARN" in res.stderr and which in res.stderr


def test_ls_json_isolates_a_node_whose_definition_is_corrupt(rig):
    good = rig.tmp / "good.json"
    bad = rig.tmp / "bad.json"
    for d in (good, bad):
        d.write_text(json.dumps({"invoke": ["true"]}))
    rig.node("alpha", str(good))
    rig.node("beta", str(bad))
    bad.write_text("{")
    rows = {r["name"]: r for r in rig.json("ls", "--json")}
    assert rows["beta"]["status"] == "unresolved"
    assert rows["beta"]["inbox"] == ""
    assert rows["alpha"]["status"] == "stopped"
    assert rows["alpha"]["inbox"] != ""
