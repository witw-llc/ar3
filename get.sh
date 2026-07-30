#!/bin/sh
# The Ark one-line installer:
#
#   curl -fsSL https://raw.githubusercontent.com/witw-llc/ar3/main/get.sh | sh
#
# Installs the suite into $AR3_DIR (default ~/.ar3) and adds one source line
# to your shell rc. Re-running updates in place. With git on PATH the install
# is a clone that pulls; without git, the latest release unpacks from a
# tarball and re-running replaces it when a newer release exists. Overrides:
#   AR3_DIR=/somewhere     install location
#   AR3_REPO=<url>         source repo (defaults to the public mirror)
#   AR3_CHANNEL=beta       install the development tree instead of the latest
#                          release (requires git and access to the repo)
set -eu

AR3_DIR="${AR3_DIR:-$HOME/.ar3}"
AR3_CHANNEL="${AR3_CHANNEL:-stable}"
case "$AR3_CHANNEL" in
  beta)   AR3_REPO="${AR3_REPO:-https://github.com/witw-llc/ar3-private.git}" ;;
  stable) AR3_REPO="${AR3_REPO:-https://github.com/witw-llc/ar3.git}" ;;
  *) echo "ar3: unknown AR3_CHANNEL '$AR3_CHANNEL' (stable or beta)" >&2; exit 1 ;;
esac

if command -v git >/dev/null 2>&1; then
  if [ -d "$AR3_DIR/.git" ]; then
    echo "Updating The Ark in $AR3_DIR"
    git -C "$AR3_DIR" pull --ff-only
  else
    if [ -e "$AR3_DIR" ]; then
      [ -f "$AR3_DIR/.ar3-release" ] || {
        echo "ar3: $AR3_DIR exists and is not an ar3 install; move it or set AR3_DIR" >&2
        exit 1
      }
      rm -rf "$AR3_DIR"
    fi
    echo "Installing The Ark into $AR3_DIR"
    git clone --depth 1 "$AR3_REPO" "$AR3_DIR"
  fi
else
  if [ "$AR3_CHANNEL" = "beta" ]; then
    echo "ar3: the beta channel requires git" >&2
    exit 1
  fi
  command -v tar >/dev/null 2>&1 || {
    echo "ar3: either git or tar is required" >&2
    exit 1
  }
  base="${AR3_REPO%.git}"
  latest="$(curl -fsSLI -o /dev/null -w '%{url_effective}' "$base/releases/latest")"
  tag="${latest##*/}"
  case "$tag" in
    v[0-9]*) ;;
    *) echo "ar3: could not resolve the latest release from $base" >&2; exit 1 ;;
  esac
  current=""
  [ -f "$AR3_DIR/.ar3-release" ] && current="$(cat "$AR3_DIR/.ar3-release")"
  if [ "$current" = "$tag" ]; then
    echo "The Ark $tag is already installed in $AR3_DIR"
  else
    echo "Installing The Ark $tag into $AR3_DIR (tarball; git not found)"
    tmp="$AR3_DIR.new.$$"
    rm -rf "$tmp"
    mkdir -p "$tmp"
    curl -fsSL "$base/archive/refs/tags/$tag.tar.gz" | tar -xz -C "$tmp" --strip-components=1
    printf '%s\n' "$tag" > "$tmp/.ar3-release"
    rm -rf "$AR3_DIR"
    mv "$tmp" "$AR3_DIR"
  fi
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
