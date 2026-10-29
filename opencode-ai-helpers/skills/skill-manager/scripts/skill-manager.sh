#!/usr/bin/env bash
# skill-manager.sh — on-demand activation of OpenCode skills from the
# ai-helpers plugin collections. Keeps standing skill advertisements
# minimal: only this manager plus the currently-activated skills are
# registered at any time.
#
# Activation is a symlink into the OpenCode skills directory; deactivation
# is symlink removal. Both take effect immediately — OpenCode re-scans
# skill sources at every model step. Only symlinks whose target lies under
# AI_HELPERS_ROOT are ever created or removed; operator-maintained skills
# in the config directory are never touched.
#
# Specs:
#   <category>            all skills in that plugin collection
#   <category>/<skill>    one skill within a collection
#   <skill-id>            bare id; refused as ambiguous if it exists in
#                         more than one collection
#
# Usage:
#   skill-manager.sh list [category]
#   skill-manager.sh enable <spec> [<spec>...]
#   skill-manager.sh disable <spec> [<spec>...]
#   skill-manager.sh status
#   skill-manager.sh reset

set -euo pipefail

AI_HELPERS_ROOT="${AI_HELPERS_ROOT:-/opt/ai-helpers}"
PLUGINS_ROOT="${AI_HELPERS_ROOT}/plugins"
SKILLS_DIR="${OPENCODE_CONFIG_DIR:-/opt/opencode-config}/skills"

die() { echo "skill-manager: $*" >&2; exit 1; }

[ -d "$PLUGINS_ROOT" ] || die "plugin catalog not found: $PLUGINS_ROOT (set AI_HELPERS_ROOT)"
[ -d "$SKILLS_DIR" ] || die "skills directory not found: $SKILLS_DIR (set OPENCODE_CONFIG_DIR)"

usage() {
  sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 2
}

# --- catalog helpers -------------------------------------------------------

plugin_dirs() { find "$PLUGINS_ROOT" -mindepth 1 -maxdepth 1 -type d | sort; }

plugin_field() { # <plugin-dir> <field>
  jq -r ".$2 // empty" "$1/.claude-plugin/plugin.json" 2>/dev/null
}

skill_dirs() { # <plugin-dir>
  find -L "$1/skills" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort
}

skill_description() { # <skill-dir>
  python3 -c '
import sys, yaml
text = open(sys.argv[1]).read()
if text.startswith("---"):
    try:
        meta = yaml.safe_load(text.split("---", 2)[1]) or {}
        desc = meta.get("description") or ""
        print(" ".join(str(desc).split())[:160])
    except Exception:
        pass
' "$1/SKILL.md" 2>/dev/null
}

find_skill_dirs() { # <skill-id> — all matching skill dirs across collections
  local id="$1" p
  while IFS= read -r p; do
    if [ -d "$p/skills/$id" ]; then
      printf '%s/skills/%s\n' "$p" "$id"
    fi
  done < <(plugin_dirs)
}

is_our_symlink() { # <link-path> — true when symlink targets AI_HELPERS_ROOT
  local target
  [ -L "$1" ] || return 1
  target="$(readlink "$1")"
  case "$target" in
    "$AI_HELPERS_ROOT"/*) return 0 ;;
    *) return 1 ;;
  esac
}

# --- spec resolution -------------------------------------------------------

resolve_specs() { # echoes one skill dir per line for each spec
  local spec p s matches n
  for spec in "$@"; do
    case "$spec" in
      */*)
        s="$PLUGINS_ROOT/${spec%/*}/skills/${spec##*/}"
        [ -d "$s" ] || die "no such skill: $spec"
        echo "$s"
        ;;
      *)
        if [ -d "$PLUGINS_ROOT/$spec/skills" ]; then
          skill_dirs "$PLUGINS_ROOT/$spec"
        else
          matches="$(find_skill_dirs "$spec")"
          if [ -z "$matches" ]; then
            die "no category or skill named: $spec (try: list)"
          fi
          n="$(printf '%s\n' "$matches" | wc -l)"
          if [ "$n" -gt 1 ]; then
            die "ambiguous skill id '$spec' exists in $n collections:
$matches"
          fi
          echo "$matches"
        fi
        ;;
    esac
  done
}

# --- commands --------------------------------------------------------------

cmd_list() {
  local filter="${1:-}" p name desc ver n s found=0
  while IFS= read -r p; do
    name="$(basename "$p")"
    if [ -n "$filter" ] && [ "$name" != "$filter" ]; then continue; fi
    found=1
    desc="$(plugin_field "$p" description)"
    ver="$(plugin_field "$p" version)"
    n="$(skill_dirs "$p" | wc -l)"
    printf '%s %s — %s — %d skill(s)\n' \
      "$name" "${ver:+($ver)}" "${desc:-no description}" "$n"
    if [ -n "$filter" ]; then
      while IFS= read -r s; do
        printf '    %s — %s\n' "$(basename "$s")" "$(skill_description "$s")"
      done < <(skill_dirs "$p")
    fi
  done < <(plugin_dirs)
  if [ -n "$filter" ] && [ "$found" -eq 0 ]; then
    die "no such category: $filter (try: list)"
  fi
}

cmd_enable() {
  [ $# -ge 1 ] || die "enable requires at least one spec"
  local dirs s id link dup enabled=0 skipped=0
  dirs="$(resolve_specs "$@")"
  while IFS= read -r s; do
    [ -n "$s" ] || continue
    id="$(basename "$s")"
    link="$SKILLS_DIR/$id"
    if is_our_symlink "$link"; then
      skipped=$((skipped + 1))
      continue
    fi
    if [ -e "$link" ] || [ -L "$link" ]; then
      die "refusing to overwrite existing non-catalog entry: $id"
    fi
    dup="$(find_skill_dirs "$id" | grep -v "^$s$" || true)"
    if [ -n "$dup" ]; then
      echo "note: '$id' also exists in: $(echo "$dup" | tr '\n' ' ')(this activation wins)" >&2
    fi
    ln -s "$s" "$link"
    enabled=$((enabled + 1))
  done <<< "$dirs"
  echo "enabled $enabled skill(s), $skipped already active — live on the next model step"
}

cmd_disable() {
  [ $# -ge 1 ] || die "disable requires at least one spec"
  local dirs s id link removed=0 inactive=0
  dirs="$(resolve_specs "$@")"
  while IFS= read -r s; do
    [ -n "$s" ] || continue
    id="$(basename "$s")"
    link="$SKILLS_DIR/$id"
    if is_our_symlink "$link"; then
      rm "$link"
      removed=$((removed + 1))
    else
      inactive=$((inactive + 1))
    fi
  done <<< "$dirs"
  echo "deactivated $removed skill(s), $inactive were not active"
}

cmd_status() {
  local link target id category count=0
  for link in "$SKILLS_DIR"/*; do
    is_our_symlink "$link" || continue
    target="$(readlink "$link")"
    id="$(basename "$link")"
    category="$(basename "$(dirname "$(dirname "$target")")")"
    printf '%s (%s) — %s\n' "$id" "$category" "$(skill_description "$target")"
    count=$((count + 1))
  done
  echo "— $count catalog skill(s) activated; each is advertised to the model at every step"
}

cmd_reset() {
  local link count=0
  for link in "$SKILLS_DIR"/*; do
    is_our_symlink "$link" || continue
    rm "$link"
    count=$((count + 1))
  done
  echo "deactivated $count catalog skill(s); operator-maintained skills untouched"
}

# --- dispatch ---------------------------------------------------------------

[ $# -ge 1 ] || usage
case "$1" in
  list)    shift; cmd_list "$@" ;;
  enable)  shift; cmd_enable "$@" ;;
  disable) shift; cmd_disable "$@" ;;
  status)  shift; cmd_status "$@" ;;
  reset)   shift; cmd_reset "$@" ;;
  *)       usage ;;
esac
