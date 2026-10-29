"""Public local commands, with isolated homes and no browser or provider calls."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "node"
    root.mkdir()
    registry = tmp_path / "a8s"
    registry.mkdir()
    chrome = tmp_path / "chrome"
    chrome.write_text("not an executable browser")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    playwright = binaries / "playwright-cli"
    playwright.write_text("#!/bin/sh\necho '### Error'\necho 'test browser is disabled'\nexit 1\n")
    playwright.chmod(0o755)
    nodes = {}
    for name, tool in (("web1", "browser"), ("gem1", "gemini-web"), ("vault1", "vault")):
        nodes[name] = {
            "root": str(root), "definition": f"tool-{tool}",
            "vars": {"ALLOW": "other", "CHROME": str(chrome), "ALLOW_EVAL": "1",
                     "env.LOCAL_PROBE": "node-value", "VAULT_DIR": str(tmp_path / "vault-state")},
        }
    (registry / "a8s.json").write_text(json.dumps({"agents": nodes, "aliases": {}, "namespaces": {}}))
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("AR3_", "A8S_", "TELL_", "VAULT_")) and key != "OPENSSL"}
    env.update(HOME=str(tmp_path / "home"), XDG_CONFIG_HOME=str(tmp_path / "config"),
               XDG_DATA_HOME=str(tmp_path / "data"), A8S_HOME=str(registry),
               AR3_BROWSER_CHROME=str(tmp_path / "no-browser"))
    env["PATH"] = str(binaries) + os.pathsep + env["PATH"]
    return tmp_path, root, env


def run(world, *args, stdin=""):
    base, _root, env = world
    proc = subprocess.Popen([str(REPO / "ar3"), "tool", *args], env=env, cwd=base,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = proc.communicate(stdin, timeout=30)
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


@pytest.mark.parametrize("tool,commands", [
    ("browser", ["open", "close", "status", "doctor", "go", "snap", "console", "do", "idle", "secret"]),
    ("gemini-web", ["sessions", "forget"]),
    ("vault", ["init", "fingerprint", "admin", "voucher", "seat", "client"]),
    ("command-shell", []),
])
def test_help_is_public_and_offline(world, tool, commands):
    base, _root, _env = world
    for command in [None, *commands]:
        tail = [command, "--help"] if command else ["--help"]
        result = run(world, tool, "--node", "missing", *tail)
        assert result.returncode == 0, result.stderr
        assert f"ar3 tool {tool}" in result.stdout
        if command:
            assert "--seat" not in result.stdout
            assert "--node" not in result.stdout
        else:
            assert "MESSAGE" in result.stdout and "--var" in result.stdout
    assert not (base / "data").exists()


def test_gemini_sessions_and_forget_need_no_browser_var(world):
    base, _root, _env = world
    path = base / "data/ar3/gemini-web/gem1/gem1.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"sessions": {"operator": {"url": "https://gemini.google.com/app/test", "created": "now"}}}))
    listed = run(world, "gemini-web", "--node", "gem1", "sessions")
    assert listed.returncode == 0 and "operator" in listed.stdout
    forgotten = run(world, "gemini-web", "--node", "gem1", "forget", "operator")
    assert forgotten.returncode == 0 and "forgot operator" in forgotten.stdout
    assert json.loads(path.read_text()) == {"sessions": {}}
    forced = run(world, "gemini-web", "--node", "gem1", "--", "sessions")
    assert forced.returncode == 2 and "BROWSER_SEAT" in forced.stderr


def test_browser_do_preserves_stdin_node_recipes_and_effective_settings(world):
    base, root, _env = world
    recipes = root / "recipes"
    recipes.mkdir()
    marker = base / "import.json"
    (recipes / "fixture.py").write_text(
        "import os, json\nfrom pathlib import Path\nfrom browser.recipe import Recipe\n"
        f"Path({str(marker)!r}).write_text(json.dumps({{key: os.environ.get(key) for key in "
        "['AR3_BROWSER_ALLOW_EVAL', 'AR3_BROWSER_CHROME', 'LOCAL_PROBE']}))\n"
        "recipe = Recipe('fixture', 'Fixture recipe', origins=['https://example.com'])\n"
    )
    result = run(world, "browser", "--node", "web1", "--var", "ALLOW_EVAL=0",
                 "--var", "env.LOCAL_PROBE=override", "do", "-", "--json", stdin="recipe\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["ok"] and "fixture" in result.stdout
    assert json.loads(marker.read_text()) == {
        "AR3_BROWSER_ALLOW_EVAL": "0", "AR3_BROWSER_CHROME": str(base / "chrome"), "LOCAL_PROBE": "override"}


def test_browser_do_file_is_relative_to_registered_root(world):
    _base, root, _env = world
    (root / "commands.txt").write_text("recipe\n")
    result = run(world, "browser", "--node", "web1", "do", "commands.txt", "--json")
    assert result.returncode == 0 and json.loads(result.stdout)["ok"]


def test_browser_do_rejects_untrusted_recipes_before_import_with_secrets(world):
    base, _root, _env = world
    stored = run(world, "browser", "--seat", "local", "secret", "set", "PASSWORD", "-",
                 "--origin", "https://example.com", stdin="dummy-test-value")
    assert stored.returncode == 0, stored.stderr
    recipes = base / "untrusted"
    recipes.mkdir()
    marker = base / "unsafe-import"
    (recipes / "fixture.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    result = run(world, "browser", "--seat", "local", "do", "-", "--recipes", str(recipes), stdin="recipe\n")
    assert result.returncode == 2 and "--recipes" in result.stderr
    assert not marker.exists()
    assert "dummy-test-value" not in result.stdout + result.stderr


def test_browser_doctor_reads_node_chrome_and_native_override(world):
    base, _root, _env = world
    result = run(world, "browser", "--node", "web1", "doctor")
    assert f"chrome: {base / 'chrome'}" in result.stdout
    override = base / "other-chrome"
    override.touch()
    result = run(world, "browser", "--node", "web1", "doctor", "--chrome", str(override))
    assert f"chrome: {override}" in result.stdout


def test_vault_local_config_and_client_work_without_message_route(world):
    base, _root, _env = world
    result = run(world, "vault", "--node", "vault1", "fingerprint")
    assert result.returncode == 1 and str(base / "vault-state") in result.stderr
    override = base / "override-vault"
    result = run(world, "vault", "--node", "vault1", "--var", f"VAULT_DIR={override}", "admin", "list")
    assert result.returncode == 1 and str(override) in result.stderr
    result = run(world, "vault", "client", "open", "-", stdin="plain response")
    assert result.returncode == 0 and "plain response" in result.stdout


def test_vault_openssl_override_reaches_local_admin(world):
    base, _root, _env = world
    marker = base / "openssl-called"
    stub = base / "openssl-stub"
    stub.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\nraise SystemExit(1)\n")
    stub.chmod(0o755)
    result = run(world, "vault", "--node", "vault1", "--var", f"OPENSSL={stub}", "init")
    assert result.returncode == 1
    assert marker.exists()
    assert not (base / "vault-state/vault.key").exists()


@pytest.mark.parametrize("args", [
    ("browser", "--node", "web1", "--seat", "other", "status"),
    ("browser", "--node", "gem1", "status"),
    ("browser", "--node", "web1", "status", "--seat", "other"),
    ("browser", "--node", "web1", "--attach", "unused", "status"),
    ("vault", "--node", "web1", "admin", "list"),
])
def test_local_selector_and_attachment_errors_are_safe(world, args):
    result = run(world, *args)
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_delimiter_preserves_message_help_and_native_options(world):
    result = run(world, "command-shell", "--", "printf", "'%s'", "--help")
    assert result.returncode == 0 and "--help" in result.stdout
    result = run(world, "browser", "--node", "web1", "--", "secret list")
    assert result.returncode == 1
    assert not (world[0] / "data/ar3/browser/web1/secrets.json").exists()


@pytest.mark.parametrize("sender", ["--help", "-h", "--seat", "--node"])
def test_native_delimiter_keeps_option_shaped_data_on_selected_seat(world, sender):
    base, _root, _env = world
    result = run(world, "gemini-web", "--seat", "chosen", "forget", "--", sender)
    assert result.returncode == 0, result.stderr
    assert f"chosen: had no conversation for {sender}" in result.stdout
    assert not (base / "data/ar3/gemini-web/gemini").exists()


@pytest.mark.parametrize("selector", ["--node", "--seat"])
def test_vault_client_rejects_server_identity_selectors(world, selector):
    result = run(world, "vault", selector, "vault1", "client", "open", "-", stdin="reply")
    assert result.returncode == 2 and "sender's directory" in result.stderr
