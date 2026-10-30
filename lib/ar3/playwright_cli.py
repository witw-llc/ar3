"""The playwright-cli release the browser tool is proven against.

The tool and `ar3 doctor` both read it here, so the install command and the version check cannot
disagree. A newer or older playwright-cli may change the Chrome flags it adds on its own, so the
install is pinned and a different installed version shows in both doctors.
"""
import re

PLAYWRIGHT_CLI_VERSION = "0.1.22"
PLAYWRIGHT_CLI_PACKAGE = f"@playwright/cli@{PLAYWRIGHT_CLI_VERSION}"
INSTALL_COMMAND = ("npm", "install", "-g", PLAYWRIGHT_CLI_PACKAGE)


def installed_version(output):
    """The version in `playwright-cli --version` output, or None."""
    found = re.search(r"\d+\.\d+\.\d+\S*", output or "")
    return found.group(0) if found else None


def version_note(output):
    """None when the installed version is the pinned one; otherwise a sentence that says which differs."""
    found = installed_version(output)
    if found == PLAYWRIGHT_CLI_VERSION:
        return None
    shown = found or "an unreadable version"
    return f"playwright-cli {shown} is installed; the browser tool is proven against {PLAYWRIGHT_CLI_VERSION}"
