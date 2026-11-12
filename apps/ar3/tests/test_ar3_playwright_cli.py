"""The default playwright-cli install command and version reader shared by `ar3` and `tool-browser`."""
from ar3 import playwright_cli


def test_the_install_command_uses_latest_by_default():
    assert playwright_cli.PLAYWRIGHT_CLI_PACKAGE == "@playwright/cli@latest"
    assert playwright_cli.INSTALL_COMMAND == ("npm", "install", "-g", "@playwright/cli@latest")


def test_installed_version_is_read_from_output():
    assert playwright_cli.installed_version("0.1.24\n") == "0.1.24"
    assert playwright_cli.installed_version("playwright-cli 1.2.0-rc.1\n") == "1.2.0-rc.1"
    assert playwright_cli.installed_version("no digits here") is None
    assert playwright_cli.installed_version("") is None
