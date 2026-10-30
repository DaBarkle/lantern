#!/usr/bin/env bash
# Install Lantern (core, tailscale pack, David's private pack) at user scope. Safe to run twice.
set -euo pipefail
claude plugin marketplace add "$HOME/lantern" 2>&1 | tail -1 || true
claude plugin marketplace add "$HOME/lantern-david" 2>&1 | tail -1 || true
for p in lantern@lantern lantern-tailscale@lantern lantern-david@lantern-david; do
  claude plugin install --scope user "$p" 2>&1 | tail -1
done
echo
echo "Installed. Lantern plugins now enabled:"
claude plugin list 2>/dev/null | grep -i lantern || python3 -c "import json;print({k:v for k,v in json.load(open('$HOME/.claude/settings.json')).get('enabledPlugins',{}).items() if 'lantern' in k})"
echo
echo "Next: open a new Claude Code session anywhere outside ~/hermes. Your first prompt gets Lantern's context; the end of that reply offers /lantern:setup."
echo "Switch back any time with: claude plugin disable lantern@lantern"
