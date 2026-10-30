"""The playwright-cli release the browser tool is proven against, and the two places that read it."""
from ar3 import playwright_cli


def test_the_install_command_is_pinned_to_the_proven_version():
    assert playwright_cli.PLAYWRIGHT_CLI_PACKAGE == f"@playwright/cli@{playwright_cli.PLAYWRIGHT_CLI_VERSION}"
    assert playwright_cli.INSTALL_COMMAND == ("npm", "install", "-g", playwright_cli.PLAYWRIGHT_CLI_PACKAGE)


def test_a_version_is_read_from_the_output_and_compared():
    pinned = playwright_cli.PLAYWRIGHT_CLI_VERSION
    assert playwright_cli.installed_version(f"{pinned}\n") == pinned
    assert playwright_cli.version_note(f"{pinned}\n") is None
    note = playwright_cli.version_note("9.9.9\n")
    assert "9.9.9" in note and pinned in note
    assert "unreadable" in playwright_cli.version_note("no digits here")
    assert "unreadable" in playwright_cli.version_note("")
