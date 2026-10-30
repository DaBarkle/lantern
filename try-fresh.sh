#!/usr/bin/env bash
# Experience Lantern as a brand-new user: only the public plugin, empty memory, default depth, no phone topic, no
# private pack. Your real Lantern (memory, pack, settings) is untouched; it is switched off only while this runs.
set -uo pipefail
PLAY="$HOME/lantern-playground"
FRESH="$HOME/.claude/plugins/data/lantern-inline"   # where a folder-loaded plugin keeps its data
if [ "${1:-}" = "--reset" ]; then rm -rf "$FRESH" "$PLAY"; echo "fresh-user data wiped"; fi
mkdir -p "$PLAY" && cd "$PLAY" && [ -d .git ] || git init -q
for p in lantern@lantern lantern-tailscale@lantern lantern-david@lantern-david; do claude plugin disable "$p" >/dev/null 2>&1; done
echo "Fresh-user mode. Your real Lantern is paused until this session ends."
claude --plugin-dir "$HOME/lantern/plugins/lantern" "$@"
for p in lantern@lantern lantern-tailscale@lantern lantern-david@lantern-david; do claude plugin enable "$p" >/dev/null 2>&1; done
echo "Back to your real Lantern (plugins re-enabled)."
