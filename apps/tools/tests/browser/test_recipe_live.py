"""The fixture recipe on the fixture site, end to end, in a real headless Chrome: through `browser do`,
through `handler.handle` in print mode, and through `ar3 tool --node`. Live: needs Chrome, Node and
playwright-cli, and runs only when AR3_TOOL_BROWSER_LIVE=1. State lives in a temp XDG directory.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from site_fixture import NODE, PASSWORD, USER, Site

pytestmark = pytest.mark.skipif(
    os.environ.get("AR3_TOOL_BROWSER_LIVE") != "1",
    reason="live: set AR3_TOOL_BROWSER_LIVE=1",
)

REPO = Path(__file__).resolve().parents[4]
TOOL = REPO / "apps" / "tools" / "browser"
AR3 = REPO / "apps" / "ar3" / "cli.py"
DEFINITION = REPO / "apps" / "a8s" / "definitions" / "tool-browser.json"
WRONG = "wrong-Q8x2-live"


def _env(tmp_path, base=None):
    env = {k: v for k, v in os.environ.items() if k not in (
        "DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "A8S_TURN_RECIPIENT", "AR3_TOOL_REPLY_DIR",
        "TELL_OUTBOX_DIR", "AR3_BROWSER_ALLOW_EVAL", "FIXTURE_BASE", "FIXTURE_ENTRY_USER", "FIXTURE_ENTRY_PASS",
        "GIVEBACKS_BASE", "GIVEBACKS_CAUSE_ID",
    )}
    env.update(
        XDG_DATA_HOME=str(tmp_path / "data"), XDG_CACHE_HOME=str(tmp_path / "cache"),
        A8S_HOME=str(tmp_path / "a8s"),
    )
    if base:
        env["FIXTURE_BASE"] = base
    return env


def _tool(env, seat, *args, text=None, timeout=180):
    return subprocess.run(
        [sys.executable, str(TOOL), *args, "--seat", seat],
        env=env, input=text, capture_output=True, text=True, timeout=timeout,
    )


def _set(env, seat, name, value, origin):
    done = _tool(env, seat, "secret", "set", name, "--origin", origin, text=value)
    assert done.returncode == 0, (done.stdout, done.stderr)


def _do(env, seat, script, recipes, timeout=180):
    """`browser do` on the recipes directory, with the run as JSON."""
    done = subprocess.run(
        [sys.executable, str(TOOL), "--seat", seat, "do", "-", "--json", "--recipes", str(recipes)],
        env=env, input=script, capture_output=True, text=True, timeout=timeout,
    )
    return done, json.loads(done.stdout.strip().splitlines()[-1])


def _ar3(env, *words, timeout=180):
    return subprocess.run(
        [sys.executable, str(AR3), "tool", "browser", *words],
        env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
    )


def _png_size(path):
    data = Path(path).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", path
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _output(payload, index=-1):
    return payload["steps"][index].get("output", "")


def _error(payload):
    return payload["steps"][-1].get("error", "")


def _files_holding(roots, values):
    """{path: [value, ...]} for every file under `roots` that holds a value, as UTF-8 or UTF-16."""
    forms = {value: (value.encode(), value.encode("utf-16-le")) for value in values}
    found = {}
    for root in roots:
        for directory, _dirs, names in os.walk(root):
            for name in names:
                path = os.path.join(directory, name)
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                try:
                    data = Path(path).read_bytes()
                except OSError:
                    continue
                for value, encodings in forms.items():
                    if any(encoding in data for encoding in encodings):
                        found.setdefault(path, []).append(value)
    return found


def _gone(profile, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        listing = subprocess.run(
            ["pgrep", "-af", "--", f"--user-data-dir={profile}"], capture_output=True, text=True
        ).stdout.strip()
        if not listing:
            return True
        time.sleep(0.25)
    return False


def test_the_fixture_recipe_signs_in_reads_checks_and_stays_on_its_origin(tmp_path, monkeypatch, capsys):
    seat = f"ar3scratch{os.getpid()}recipe"
    other = Site()
    main = Site(other=other.base)
    env = _env(tmp_path, main.base)
    home = tmp_path / "data/ar3/browser" / seat
    profile = home / "chrome"
    recipes = NODE / "recipes"
    seen = []  # everything the tool printed or returned, for the last check
    try:
        _set(env, seat, "FIXTURE_USER", USER, main.base)
        _set(env, seat, "FIXTURE_PASS", PASSWORD, main.base)
        store = home / "secrets.json"

        # The listing and the help come with no browser.
        done, payload = _do(env, seat, "recipe\nrecipe fixture\nrecipe fx help list\n", recipes)
        seen.append(done.stdout + done.stderr)
        assert done.returncode == 0, payload
        assert _output(payload, 0).splitlines() == [
            "fixture (fx)  The fixture site of the recipe tests  [node]", "givebacks (gb)  GiveBacks newsletters",
        ]
        assert f"origins: {main.base}" in _output(payload, 1)
        assert "secrets: FIXTURE_USER, FIXTURE_PASS" in _output(payload, 1)
        assert _output(payload, 2).splitlines()[:3] == [
            "list (read, needs sign-in)", "  List the items as data.", "  usage: recipe fixture list [--limit LIMIT]",
        ]
        assert not profile.exists(), "listing and help opened a browser"
        assert main.hits == []

        # Signed out: the probe says out, and a verb that needs sign-in stops for a person.
        done, payload = _do(env, seat, "recipe fixture status\nrecipe fixture list\n", recipes)
        seen.append(done.stdout + done.stderr)
        assert done.returncode == 1
        assert _output(payload, 0).splitlines()[-1] == "out"
        assert _error(payload) == (
            f"needs a person: fixture is signed out on seat {seat}. "
            f"Run `recipe fixture login`, or sign in by hand in the Chrome of seat {seat}."
        )
        assert [path for path in payload["files"] if path.endswith("failure.txt")]

        # A wrong password: the page refuses it and the verb says so. It does not read as signed in.
        _set(env, seat, "FIXTURE_PASS", WRONG, main.base)
        done, payload = _do(env, seat, "recipe fixture login\n", recipes)
        seen.append(done.stdout + done.stderr)
        assert done.returncode == 1 and "wait-for-url: still at " in _error(payload)
        assert WRONG not in done.stdout + done.stderr
        assert main.sessions == set()
        _set(env, seat, "FIXTURE_PASS", PASSWORD, main.base)

        # Sign in with the stored secrets: typed by name, shown by name.
        done, payload = _do(env, seat, "recipe fixture login\nrecipe fixture status\n", recipes)
        seen.append(done.stdout + done.stderr)
        assert done.returncode == 0, payload
        login = payload["steps"][0]["output"].splitlines()
        assert "  fill-secret #user FIXTURE_USER -> #user <- [secret:FIXTURE_USER]" in login
        assert "  fill-secret #pass FIXTURE_PASS -> #pass <- [secret:FIXTURE_PASS]" in login
        assert login[-1] == "signed in"
        assert _output(payload, 1).splitlines()[-1] == "in"
        assert len(main.sessions) == 1

        # list: a code verb that fetches with the page's cookies; the data is the output and result.json.
        done, payload = _do(env, seat, "recipe fixture list --limit 2\n", recipes)
        seen.append(done.stdout + done.stderr)
        assert done.returncode == 0, payload
        assert _output(payload).splitlines()[-1] == (
            '{"items":[{"id":1,"title":"First item"},{"id":2,"title":"Second item"}]}'
        )
        (result_json,) = [path for path in payload["files"] if path.endswith("result.json")]
        assert json.loads(Path(result_json).read_text()) == {
            "items": [{"id": 1, "title": "First item"}, {"id": 2, "title": "Second item"}],
        }

        # read: a rest positional with no quotes, and the text of a page.
        done, payload = _do(env, seat, "recipe fixture read second item\nrecipe fx read THIRD\n", recipes)
        seen.append(done.stdout + done.stderr)
        assert done.returncode == 0, payload
        assert _output(payload, 0).splitlines()[-1] == "The second item body, with <b>markup</b> shown as text."
        assert _output(payload, 1).splitlines()[-1] == "The third item body."
        done, payload = _do(env, seat, "recipe fixture read nothing like it\n", recipes)
        assert done.returncode == 1
        assert _error(payload).splitlines() == [
            "no item has 'nothing like it' in its title", "usage: recipe fixture read title [title ...]",
        ]
        done, payload = _do(env, seat, "recipe fixture list --limit many\nrecipe fixture nope\nrecipe nope\n", recipes)
        assert done.returncode == 1
        assert _error(payload).splitlines() == [
            "argument --limit: invalid int value: 'many'", "usage: recipe fixture list [--limit LIMIT]",
        ]

        # open: a step verb, with the id and the node's setting bound as one value each.
        done, payload = _do(env, seat, "recipe fixture open --id 3\n", recipes)
        seen.append(done.stdout + done.stderr)
        assert done.returncode == 0, payload
        assert _output(payload).splitlines()[-1] == "Third item"

        # check: the probe, then each read verb with an example.
        done, payload = _do(env, seat, "recipe fixture check\n", recipes)
        seen.append(done.stdout + done.stderr)
        assert done.returncode == 0, payload
        assert _output(payload).splitlines()[-5:] == [
            "status: in", "list: healthy", "read: healthy", "open: healthy", "check: healthy (4)",
        ]

        # Off the origins, by every route: the calls made before any request leave no trace on the other site.
        for how in ("go", "fetch", "http", "tab"):
            before = len(other.hits)
            done, payload = _do(env, seat, f"recipe fixture offsite --how {how}\n", recipes)
            seen.append(done.stdout + done.stderr)
            assert done.returncode == 1, how
            assert "outside the origins of recipe fixture" in _error(payload), (how, _error(payload))
            assert len(other.hits) == before, f"{how} sent a request to the other origin"
        # A click cannot be stopped before the other site answers; the page it lands on fails the verb.
        done, payload = _do(env, seat, "recipe fixture offsite --how click\n", recipes)
        seen.append(done.stdout + done.stderr)
        assert done.returncode == 1
        assert f"click: the page is at '{other.base}/'" in _error(payload)
        assert ("GET", "/") in other.hits

        # An in-page fetch follows no redirect: not to another origin, and not inside the origin either.
        for to, path, followed in (("outside", "/redirect-out", None), ("inside", "/redirect-in", "/public.json")):
            before_other, before_main = len(other.hits), len(main.hits)
            done, payload = _do(env, seat, f"recipe fixture bounce --to {to}\n", recipes)
            seen.append(done.stdout + done.stderr)
            assert done.returncode == 1, to
            assert f"fetch: {main.base}{path} answered with a redirect. A recipe's fetch follows none" in _error(payload), to
            assert len(other.hits) == before_other, f"the redirect {to} was followed to the other origin"
            asked = [asked_path for _method, asked_path in main.hits[before_main:]]
            assert path in asked, (to, asked)
            assert followed is None or followed not in asked, f"the redirect {to} was followed: {asked}"

        # The verb after a click away from the origins finds its way back.
        done, payload = _do(env, seat, "recipe fixture status\n", recipes)
        assert _output(payload, 0).splitlines()[-1] == "in", "the verb after a click away did not find its way back"

        # Through handler.handle in print mode, in this process: the reply prints, the files are copied,
        # and the exit code says whether the script ran to the end.
        from browser import handler, session

        for name, value in env.items():
            if name in ("XDG_DATA_HOME", "XDG_CACHE_HOME", "A8S_HOME", "FIXTURE_BASE"):
                monkeypatch.setenv(name, value)
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.setenv("AR3_TOOL_REPLY_DIR", str(tmp_path / "replies"))
        monkeypatch.chdir(session.scratch_dir(seat))
        # The node is registered in the a8s home of this test, and the handler finds its recipes there by the
        # seat's name: the working directory of the process is the seat's scratch directory, not the node's.
        node_dir = tmp_path / "node"
        shutil.copytree(recipes, node_dir / "recipes")
        (tmp_path / "a8s").mkdir(exist_ok=True)
        (tmp_path / "a8s" / "a8s.json").write_text(json.dumps({
            "agents": {seat: {
                "root": str(node_dir), "definition": str(DEFINITION), "vars": {"env.FIXTURE_BASE": main.base},
            }},
            "aliases": {}, "namespaces": {},
        }))
        capsys.readouterr()
        assert handler.handle(seat, "operator", "recipe fixture list --limit 1", allow="operator") == 0
        printed = capsys.readouterr().out
        seen.append(printed)
        assert printed.startswith(f"{seat}: ok") and '{"items":[{"id":1,"title":"First item"}]}' in printed
        copied = [line.split("artifact: ", 1)[1] for line in printed.splitlines() if line.startswith("artifact: ")]
        assert [Path(path).name for path in copied if path.endswith("result.json")]
        assert handler.handle(seat, "operator", "recipe fixture offsite", allow="operator") == 1
        printed = capsys.readouterr().out
        seen.append(printed)
        assert printed.startswith(f"{seat}: failed: go: refusing ")
        assert handler.handle("a-seat-no-node-has", "operator", "recipe", allow="operator") == 0
        assert "[node]" not in capsys.readouterr().out

        # Through ar3 tool --node: the node's registered root holds the recipes and its vars hold the setting,
        # and the exit code of the tool is the exit code of ar3.
        by_ar3 = _env(tmp_path)
        listed = _ar3(by_ar3, "--node", seat, "--", "recipe")
        seen.append(listed.stdout + listed.stderr)
        assert listed.returncode == 0, (listed.stdout, listed.stderr)
        assert "fixture (fx)  The fixture site of the recipe tests  [node]" in listed.stdout
        read = _ar3(by_ar3, "--node", seat, "--", "recipe", "fixture", "read", "first", "item")
        seen.append(read.stdout + read.stderr)
        assert read.returncode == 0, (read.stdout, read.stderr)
        assert read.stdout.splitlines()[0] == f"{seat}: ok"
        assert "The first item body." in read.stdout
        listed_items = _ar3(by_ar3, "--node", seat, "--", "recipe", "fixture", "list", "--limit", "1")
        seen.append(listed_items.stdout + listed_items.stderr)
        assert listed_items.returncode == 0
        assert [line for line in listed_items.stdout.splitlines() if line.startswith("artifact: ") and line.endswith("result.json")]
        refused = _ar3(by_ar3, "--node", seat, "--", "recipe", "fixture", "offsite", "--how", "fetch")
        seen.append(refused.stdout + refused.stderr)
        assert refused.returncode == 1
        assert f"{seat}: failed: fetch: refusing " in refused.stdout
        nothing = _ar3(by_ar3, "--node", seat, "--", "recipe", "nope")
        assert nothing.returncode == 1 and "unknown recipe: nope. Known recipes: " in nothing.stdout
        assert "fixture" in nothing.stdout
        # The same words with no --node, in the directory of the node, with the variable that every agent's wake
        # carries: no seat of that name is a node, so no recipe of that directory loads.
        elsewhere = subprocess.run(
            [sys.executable, str(AR3), "tool", "browser", "--", "recipe"],
            env={**by_ar3, "A8S_TURN_RECIPIENT": seat}, cwd=node_dir,
            capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
        )
        assert elsewhere.returncode == 0 and "[node]" not in elsewhere.stdout, (elsewhere.stdout, elsewhere.stderr)
        assert "fixture" not in elsewhere.stdout

        # Signing out reads as signed out.
        done, payload = _do(env, seat, "recipe fixture logout\nrecipe fixture status\n", recipes)
        seen.append(done.stdout + done.stderr)
        assert done.returncode == 0, payload
        assert _output(payload, 1).splitlines()[-1] == "out"
        assert main.sessions == set()
        done, payload = _do(env, seat, "recipe fixture read first\n", recipes)
        assert done.returncode == 1 and _error(payload).startswith("needs a person: fixture is signed out")

        # A stop ends a recipe in the middle of a verb.
        waiting = {}

        def slow():
            waiting["done"], waiting["payload"] = _do(env, seat, "recipe fixture slow --seconds 60\n", recipes)

        thread = threading.Thread(target=slow)
        started = time.monotonic()
        thread.start()
        time.sleep(6)
        assert thread.is_alive(), "the slow verb ended by itself"
        assert _tool(env, seat, "close").returncode == 0
        thread.join(40)
        assert not thread.is_alive() and time.monotonic() - started < 50
        assert waiting["done"].returncode == 1
        assert _error(waiting["payload"]) == "seat closed by a8s stop"
        seen.append(waiting["done"].stdout)

        # Close Chrome so the profile is flushed, then look everywhere for the stored values.
        assert _tool(env, seat, "close").returncode == 0
        assert _gone(profile)
        everything = "\n".join(seen)
        assert USER not in everything and PASSWORD not in everything and WRONG not in everything
        assert "[secret:FIXTURE_USER]" in everything
        found = _files_holding([home, tmp_path / "cache", tmp_path / "replies", node_dir], [USER, PASSWORD, WRONG])
        assert set(found) == {str(store)}, found
        assert sorted(found[str(store)]) == sorted([USER, PASSWORD]), "the scan can not see a value, so it proves nothing"
        assert json.loads(store.read_text())["secrets"] == {
            "FIXTURE_PASS": {"value": PASSWORD, "origins": [main.base]},
            "FIXTURE_USER": {"value": USER, "origins": [main.base]},
        }
    finally:
        main.stop()
        other.stop()
        _tool(env, seat, "close")
        _gone(profile)


def test_a_recipe_types_values_it_holds_without_showing_them_and_shoots_the_whole_page(tmp_path):
    """No secret is stored on this seat. The recipe reads two values from the node's settings, hides them
    and types them into the sign-in form. The form accepts them, and they are in no output and no file."""
    seat = f"ar3scratch{os.getpid()}hidden"
    site = Site()
    env = _env(tmp_path, site.base)
    env.update(FIXTURE_ENTRY_USER=USER, FIXTURE_ENTRY_PASS=PASSWORD)
    home = tmp_path / "data/ar3/browser" / seat
    profile = home / "chrome"
    recipes = NODE / "recipes"
    seen = []
    try:
        done, payload = _do(env, seat, "recipe fixture enter\nrecipe fixture status\n", recipes)
        seen.append(done.stdout + done.stderr)
        assert done.returncode == 0, payload
        entered = _output(payload, 0).splitlines()
        assert entered[:4] == [
            "  env FIXTURE_ENTRY_USER", "  env FIXTURE_ENTRY_PASS", "  hide ENTRY_USER", "  hide ENTRY_PASS",
        ]
        assert "  fill_hidden #user -> #user <- [hidden]" in entered
        assert "  fill_hidden #pass -> #pass <- [hidden]" in entered
        assert _output(payload, 1).splitlines()[-1] == "in"
        assert len(site.sessions) == 1, "the form refused the values the recipe typed"

        # A field the driver will not fill: Playwright's call log prints the text it was given, and only
        # the first line of the error comes back.
        done, payload = _do(env, seat, "recipe fixture enter --page /tall --into #locked\n", recipes)
        seen.append(done.stdout + done.stderr)
        assert done.returncode == 1
        assert _error(payload) == "fill_hidden: TimeoutError: locator.fill: Timeout 5000ms exceeded."
        assert "Call log" not in done.stdout + done.stderr

        # A page that does not hold the field: the resolver says so, and the value is not in the message.
        done, payload = _do(env, seat, "recipe fixture enter --into #nothing-here\n", recipes)
        seen.append(done.stdout + done.stderr)
        assert done.returncode == 1 and _error(payload).startswith("fill_hidden: fill: nothing visible matching ")

        # The whole page is taller than the part in view.
        done, payload = _do(env, seat, "recipe fixture shots\n", recipes)
        seen.append(done.stdout + done.stderr)
        assert done.returncode == 0, payload
        assert _output(payload).splitlines()[-3:] == [
            "  screenshot view.png", "  screenshot whole.png (full page)", "two screenshots",
        ]
        (view,) = [path for path in payload["files"] if path.endswith("-view.png")]
        (whole,) = [path for path in payload["files"] if path.endswith("-whole.png")]
        view_height, whole_height = _png_size(view)[1], _png_size(whole)[1]
        assert view_height < 3200 <= whole_height, (view_height, whole_height)

        # One character to a field is no value the scrub can know. playwright-cli follows a click with a
        # snapshot file of the page, and that holds the characters: the control keeps the files and finds them,
        # and the block that deletes them leaves none behind.
        pages = home / "scratch" / ".playwright-cli"

        def typed_in_page_files():
            return sorted(
                path.name for path in pages.glob("page-*.yml")
                if ": Q" in path.read_text(errors="replace") or ": Z" in path.read_text(errors="replace")
            )

        done, payload = _do(env, seat, "recipe fixture boxes --keep\n", recipes)
        seen.append(done.stdout + done.stderr)
        assert done.returncode == 0, payload
        kept = typed_in_page_files()
        assert kept, "playwright-cli wrote no page file that holds the typed characters: the check below proves nothing"
        done, payload = _do(env, seat, "recipe fixture boxes\n", recipes)
        seen.append(done.stdout + done.stderr)
        assert done.returncode == 0, payload
        assert typed_in_page_files() == kept, "the block left a page file that holds what it typed"

        assert _tool(env, seat, "close").returncode == 0
        assert _gone(profile)
        everything = "\n".join(seen)
        assert USER not in everything and PASSWORD not in everything and "[hidden]" in everything
        assert _files_holding([home, tmp_path / "cache"], [USER, PASSWORD]) == {}
        assert not (home / "secrets.json").exists(), "the scan could not see a stored value: there was none"
    finally:
        site.stop()
        _tool(env, seat, "close")
        _gone(profile)
