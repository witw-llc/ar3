#!/bin/sh
# The Ark one-line installer:
#
#   curl -fsSL https://raw.githubusercontent.com/witw-llc/ar3/main/get.sh | sh
#
# Clones the suite into $AR3_DIR (default ~/.ar3) and adds one source line
# to your shell rc. Re-running updates the clone in place. Overrides:
#   AR3_DIR=/somewhere    install location
#   AR3_REPO=<url>        clone source (defaults to the public repo)
set -eu

AR3_DIR="${AR3_DIR:-$HOME/.ar3}"
AR3_REPO="${AR3_REPO:-https://github.com/witw-llc/ar3.git}"

command -v git >/dev/null 2>&1 || {
  echo "ar3: git is required (try: xcode-select --install, or apt/dnf install git)" >&2
  exit 1
}

if [ -d "$AR3_DIR/.git" ]; then
  echo "Updating The Ark in $AR3_DIR"
  git -C "$AR3_DIR" pull --ff-only
else
  echo "Installing The Ark into $AR3_DIR"
  git clone --depth 1 "$AR3_REPO" "$AR3_DIR"
fi

case "${SHELL:-/bin/sh}" in
  */zsh)  RC="$HOME/.zshrc" ;;
  */bash) RC="$HOME/.bashrc" ;;
  *)      RC="$HOME/.profile" ;;
esac

SOURCE_LINE=". \"$AR3_DIR/install.sh\""
if grep -qsF "$AR3_DIR/install.sh" "$RC"; then
  echo "Shell rc already sources install.sh ($RC)"
else
  printf '\n# The Ark (https://github.com/witw-llc/ar3)\n%s\n' "$SOURCE_LINE" >> "$RC"
  echo "Added to $RC: $SOURCE_LINE"
fi

echo
echo "Done. Open a new shell (or run the source line above), then:"
echo "  ar3 doctor"
echo "Agent-skill install is opt-in: change the rc line to add --skills."
