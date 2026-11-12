"""The playwright-cli package install command and version reader.

The browser tool and `ar3 doctor` both read the install command here so they cannot disagree.
By default, dependency installation uses `@playwright/cli@latest`, and both doctors report the
installed version.
"""
import re

PLAYWRIGHT_CLI_PACKAGE = "@playwright/cli@latest"
INSTALL_COMMAND = ("npm", "install", "-g", PLAYWRIGHT_CLI_PACKAGE)


def installed_version(output):
    """The version in `playwright-cli --version` output, or None."""
    found = re.search(r"\d+\.\d+\.\d+\S*", output or "")
    return found.group(0) if found else None
