#!/usr/bin/env bash
# Source this from your shell rc to put ar3 on PATH.
#
#   source <path-to-repo>/install.sh            # PATH only
#   source <path-to-repo>/install.sh --skills   # PATH + install tool docs as agent skills

# Resolve this file's directory under either zsh or bash.
if [ -n "$ZSH_VERSION" ]; then
  AR3_ROOT="$(cd "$(dirname "${(%):-%x}")" && pwd)"
else
  AR3_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

# The shims at the repo root — ar3, a8s, tell, tells, r4t, k7e — resolve from here.
export AR3_ROOT
# get.sh writes the source line to every rc the user's shells read, and some
# of those read each other (Debian's .profile sources .bashrc), so this runs
# more than once per shell. Prepend only when absent.
case ":$PATH:" in
  *":$AR3_ROOT:"*) ;;
  *) export PATH="$AR3_ROOT:$PATH" ;;
esac

export AR3_CACHE="$HOME/.cache/ar3"
[ -d "$AR3_CACHE" ] || mkdir -p "$AR3_CACHE"

# Install agent skills (symlink docs as skills) only when --skills is passed.
INSTALL_SKILLS=false
for _install_arg in "$@"; do
  if [ "$_install_arg" = "--skills" ]; then
    INSTALL_SKILLS=true
  fi
done

if [ "$INSTALL_SKILLS" = true ]; then
  SKILLS_HASH=$(ls "$AR3_ROOT/docs/"*.md 2>/dev/null | sort | shasum | cut -d' ' -f1)
  SKILLS_CACHE="$AR3_CACHE/skills-hash"
  if [ ! -f "$SKILLS_CACHE" ] || [ "$(cat "$SKILLS_CACHE" 2>/dev/null)" != "$SKILLS_HASH" ]; then
    echo "Updating agent skills..."
    for skill_root in "$HOME/.claude/skills" "$HOME/.cursor/skills"; do
      for skill_dir in "$skill_root/"*/; do
        if [ -L "$skill_dir/SKILL.md" ]; then
          target=$(readlink "$skill_dir/SKILL.md")
          if [[ "$target" == "$AR3_ROOT/docs/"* ]] && [ ! -f "$target" ]; then
            echo "  Removing stale skill: $(basename "$skill_dir")"
            rm -rf "$skill_dir"
          fi
        fi
      done
    done
    for doc in "$AR3_ROOT/docs/"*.md; do
      [ -f "$doc" ] || continue
      name=$(basename "$doc" .md)
      if [ "$(head -1 "$doc")" != "---" ]; then
        continue
      fi
      name_lower=$(echo "$name" | tr '[:upper:]' '[:lower:]')
      if [ -d "$HOME/.claude" ]; then
        skill_dir="$HOME/.claude/skills/$name_lower"
        mkdir -p "$skill_dir"
        ln -sf "$doc" "$skill_dir/SKILL.md"
      fi
      skill_dir="$HOME/.cursor/skills/$name_lower"
      mkdir -p "$skill_dir"
      ln -sf "$doc" "$skill_dir/SKILL.md"
    done
    echo "$SKILLS_HASH" > "$SKILLS_CACHE"
  fi
fi
