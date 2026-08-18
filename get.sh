#!/bin/sh
# ar3 one-line installer:
#
#   curl --proto '=https' --tlsv1.2 -fsSL \
#     https://raw.githubusercontent.com/witw-llc/ar3/main/get.sh | sh
#
# Installs the suite into $AR3_DIR (default ~/.ar3) and adds one source line
# to your shell rc. Re-running updates in place. With git on PATH the install
# is a clone that pulls; without git, the latest release unpacks from a
# tarball and re-running replaces it when a newer release exists. Overrides:
#   AR3_DIR=/somewhere     install location
#   AR3_REPO=<url>         source repo (defaults to the public mirror)
#   AR3_CHANNEL=beta       install the development tree instead of the latest
#                          release (requires git and access to the repo)
#   AR3_VERSION=vX.Y.Z     pin to a release tag (git checks it out; tarball
#                          skips latest-resolution and fetches that tag)
#   AR3_SYSTEM=1           machine-wide / agent-user install: clone into
#                          AR3_DIR (default /usr/local/lib/ar3) and symlink
#                          the suite shims into AR3_BIN (default /usr/local/bin)
#                          instead of editing a shell rc
#   AR3_BIN=/usr/local/bin bin dir for AR3_SYSTEM=1 shim symlinks
#
# Wrapped in main() so a truncated pipe cannot run a partial script (nvm /
# ollama / tailscale precedent). When a re-run actually changes the tree and
# a8s has running nodes, finishes with `$AR3_DIR/a8s update` so handlers
# re-exec the new code (cron-schedulable machine updates).
set -eu

main() {
  AR3_SYSTEM="${AR3_SYSTEM:-}"
  if [ -n "$AR3_SYSTEM" ] && [ "$AR3_SYSTEM" != "0" ]; then
    AR3_DIR="${AR3_DIR:-/usr/local/lib/ar3}"
    AR3_BIN="${AR3_BIN:-/usr/local/bin}"
  else
    AR3_DIR="${AR3_DIR:-$HOME/.ar3}"
    AR3_BIN=""
  fi

  AR3_CHANNEL="${AR3_CHANNEL:-stable}"
  AR3_VERSION="${AR3_VERSION:-}"
  case "$AR3_CHANNEL" in
    beta)   AR3_REPO="${AR3_REPO:-https://github.com/witw-llc/ar3-private.git}" ;;
    stable) AR3_REPO="${AR3_REPO:-https://github.com/witw-llc/ar3.git}" ;;
    *) echo "ar3: unknown AR3_CHANNEL '$AR3_CHANNEL' (stable or beta)" >&2; exit 1 ;;
  esac

  if [ -n "$AR3_VERSION" ]; then
    case "$AR3_VERSION" in
      v[0-9]*) ;;
      *)
        echo "ar3: AR3_VERSION must look like vX.Y.Z (got '$AR3_VERSION')" >&2
        exit 1
        ;;
    esac
  fi

  _needs_root=0
  case "$AR3_DIR" in
    /usr/local|/usr/local/*|/opt|/opt/*) _needs_root=1 ;;
  esac
  if [ -n "$AR3_BIN" ]; then
    case "$AR3_BIN" in
      /usr/local|/usr/local/*|/opt|/opt/*) _needs_root=1 ;;
    esac
  fi
  if [ "$_needs_root" = 1 ]; then
    uid="$(id -u)"
    if [ "$uid" != 0 ]; then
      echo "ar3: installing under $AR3_DIR requires root (sudo AR3_SYSTEM=1 sh get.sh)" >&2
      exit 1
    fi
  fi

  AR3_CHANGED=0

  if command -v git >/dev/null 2>&1; then
    _install_git
  else
    _install_tarball
  fi

  _restart_nodes_if_needed

  if [ -n "$AR3_BIN" ]; then
    _link_system_shims
    echo
    echo "Done. Suite at $AR3_DIR; shims on PATH via $AR3_BIN."
    echo "  $AR3_DIR/ar3 doctor"
    echo "Agent users resolve tell from $AR3_BIN without reading a home clone;"
    echo "set TELL_OUTBOX_DIR when waking them (no registry under their HOME)."
    return 0
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
    printf '\n# ar3 (https://github.com/witw-llc/ar3)\n%s\n' "$SOURCE_LINE" >> "$RC"
    echo "Added to $RC: $SOURCE_LINE"
  fi

  # On Windows the rc line only reaches Git Bash shells. PowerShell and
  # cmd.exe resolve the .cmd/.ps1 shims through the Windows user Path, so
  # add the suite dir there too. Never setx (1024-char truncation, flattens
  # the user/system split) and never a plain [Environment]::…("User") R/W
  # either — that pair reads Path *expanded* and writes it back as REG_SZ,
  # permanently flattening every %USERPROFILE%-style entry and downgrading
  # its registry type on the very first install. Read HKCU\Environment raw
  # (DoNotExpandEnvironmentNames), write back as REG_EXPAND_SZ, and broadcast
  # WM_SETTINGCHANGE ourselves — the pattern rustup, scoop, uv/cargo-dist and
  # winget all converged on independently.
  case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*)
      if command -v cygpath >/dev/null 2>&1 && command -v powershell.exe >/dev/null 2>&1; then
        AR3_WIN_DIR="$(cygpath -w "$AR3_DIR")"
        export AR3_WIN_DIR
        powershell.exe -NoProfile -Command '
          $d = $env:AR3_WIN_DIR
          $regPath = "registry::HKEY_CURRENT_USER\Environment"
          $raw = (Get-Item -LiteralPath $regPath).GetValue("Path", "", "DoNotExpandEnvironmentNames")
          $entries = $raw -split ";" -ne ""
          if ($entries -contains $d) {
            Write-Host "Windows user Path already has: $d"
          } else {
            $new = ($raw.TrimEnd(";") + ";" + $d).TrimStart(";")
            Set-ItemProperty -Type ExpandString -LiteralPath $regPath Path $new
            Add-Type -Namespace Win32 -Name NativeMethods -MemberDefinition "
              [DllImport(`"user32.dll`", SetLastError = true, CharSet = CharSet.Auto)]
              public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint Msg, UIntPtr wParam, string lParam, uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);
            "
            $result = [UIntPtr]::Zero
            [Win32.NativeMethods]::SendMessageTimeout([IntPtr]0xffff, 0x1A, [UIntPtr]::Zero, "Environment", 2, 5000, [ref]$result) | Out-Null
            Write-Host "Added to the Windows user Path: $d"
          }' </dev/null
        echo "PowerShell and cmd.exe pick it up in NEW windows (shims: a8s.cmd, a8s.ps1, ...)."
      else
        echo "Windows detected but cygpath or powershell.exe is missing —"
        echo "add $AR3_DIR to the Windows user Path yourself for PowerShell/cmd."
      fi
    ;;
  esac

  echo
  echo "Done. Open a new shell (or run the source line above), then:"
  echo "  $AR3_DIR/ar3 doctor"
  echo "Agent-skill install is opt-in: change the rc line to add --skills."
  echo "Headless/cron shells skip .bashrc — use $AR3_DIR/ar3 (absolute) or"
  echo "source install.sh from .profile; re-run this script to update."
}

_install_git() {
  if [ -d "$AR3_DIR/.git" ]; then
    echo "Updating ar3 in $AR3_DIR"
    before="$(git -C "$AR3_DIR" rev-parse HEAD)"
    if [ -n "$AR3_VERSION" ]; then
      git -C "$AR3_DIR" fetch --depth 1 origin tag "$AR3_VERSION"
      git -C "$AR3_DIR" checkout -f "$AR3_VERSION"
    elif git -C "$AR3_DIR" symbolic-ref -q HEAD >/dev/null 2>&1; then
      git -C "$AR3_DIR" pull --ff-only
    else
      # A prior AR3_VERSION pin left a detached HEAD (and a tag-narrowed fetch
      # refspec); rejoin the default branch with tracking restored.
      branch="$(git -C "$AR3_DIR" ls-remote --symref origin HEAD \
        | awk '$1 == "ref:" {sub("refs/heads/", "", $2); print $2; exit}')"
      [ -n "$branch" ] || branch=main
      git -C "$AR3_DIR" config remote.origin.fetch \
        "+refs/heads/$branch:refs/remotes/origin/$branch"
      git -C "$AR3_DIR" fetch --depth 1 origin "$branch"
      git -C "$AR3_DIR" checkout -f -B "$branch" "origin/$branch"
    fi
    after="$(git -C "$AR3_DIR" rev-parse HEAD)"
    [ "$before" = "$after" ] || AR3_CHANGED=1
  else
    if [ -e "$AR3_DIR" ]; then
      [ -f "$AR3_DIR/.ar3-release" ] || {
        echo "ar3: $AR3_DIR exists and is not an ar3 install; move it or set AR3_DIR" >&2
        exit 1
      }
      rm -rf "$AR3_DIR"
    fi
    echo "Installing ar3 into $AR3_DIR"
    if [ -n "$AR3_VERSION" ]; then
      git clone --depth 1 --branch "$AR3_VERSION" "$AR3_REPO" "$AR3_DIR"
    else
      git clone --depth 1 "$AR3_REPO" "$AR3_DIR"
    fi
    AR3_CHANGED=1
  fi
}

_install_tarball() {
  if [ "$AR3_CHANNEL" = "beta" ]; then
    echo "ar3: the beta channel requires git" >&2
    exit 1
  fi
  command -v tar >/dev/null 2>&1 || {
    echo "ar3: either git or tar is required" >&2
    exit 1
  }
  base="${AR3_REPO%.git}"
  if [ -n "$AR3_VERSION" ]; then
    tag="$AR3_VERSION"
  else
    latest="$(curl --proto '=https' --tlsv1.2 -fsSLI -o /dev/null -w '%{url_effective}' "$base/releases/latest")"
    tag="${latest##*/}"
  fi
  case "$tag" in
    v[0-9]*) ;;
    *) echo "ar3: could not resolve release tag from $base (got '$tag')" >&2; exit 1 ;;
  esac
  if [ -e "$AR3_DIR" ] && [ ! -f "$AR3_DIR/.ar3-release" ] && [ ! -d "$AR3_DIR/.git" ]; then
    echo "ar3: $AR3_DIR exists and is not an ar3 install; move it or set AR3_DIR" >&2
    exit 1
  fi
  current=""
  [ -f "$AR3_DIR/.ar3-release" ] && current="$(cat "$AR3_DIR/.ar3-release")"
  if [ "$current" = "$tag" ]; then
    echo "ar3 $tag is already installed in $AR3_DIR"
  else
    echo "Installing ar3 $tag into $AR3_DIR (tarball; git not found)"
    tmp="$AR3_DIR.new.$$"
    rm -rf "$tmp"
    mkdir -p "$tmp"
    curl --proto '=https' --tlsv1.2 -fsSL "$base/archive/refs/tags/$tag.tar.gz" \
      | tar -xz -C "$tmp" --strip-components=1
    printf '%s\n' "$tag" > "$tmp/.ar3-release"
    rm -rf "$AR3_DIR"
    mv "$tmp" "$AR3_DIR"
    AR3_CHANGED=1
  fi
}

_link_system_shims() {
  mkdir -p "$AR3_BIN"
  for shim in ar3 a8s tell tells r4t k7e; do
    src="$AR3_DIR/$shim"
    dest="$AR3_BIN/$shim"
    if [ ! -f "$src" ]; then
      echo "ar3: missing shim $src" >&2
      exit 1
    fi
    if [ -e "$dest" ] || [ -L "$dest" ]; then
      rm -f "$dest"
    fi
    ln -s "$src" "$dest"
    echo "Linked $dest -> $src"
  done
}

_restart_nodes_if_needed() {
  [ "$AR3_CHANGED" = 1 ] || return 0
  a8s="$AR3_DIR/a8s"
  [ -f "$a8s" ] || return 0
  nodes="$("$a8s" ps -q 2>/dev/null || true)"
  [ -n "$nodes" ] || return 0
  echo "Restarting running a8s nodes so handlers pick up the update…"
  "$a8s" update
}

main "$@"
