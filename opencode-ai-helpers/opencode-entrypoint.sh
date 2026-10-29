#!/bin/bash
set -euo pipefail

# Install baked skills into the mounted OpenCode config directory.
# Copy-if-absent only — operator-maintained skills in the config volume
# are never overwritten; remove a baked skill's directory there to
# re-install an updated version from the image.
BUILTIN_SKILLS="/opt/opencode-builtin-skills"
CONFIG_SKILLS="${OPENCODE_CONFIG_DIR:-/opt/opencode-config}/skills"
if [ -d "$BUILTIN_SKILLS" ] && [ -d "$CONFIG_SKILLS" ]; then
  for src in "$BUILTIN_SKILLS"/*/; do
    [ -d "$src" ] || continue
    name="$(basename "$src")"
    if [ ! -e "$CONFIG_SKILLS/$name" ]; then
      cp -r "$src" "$CONFIG_SKILLS/$name"
    fi
  done
fi

exec opencode "$@"
