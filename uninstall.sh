#!/bin/bash
# Stop and remove the rescodex LaunchAgent; local state is kept.
set -euo pipefail

LABEL="com.codex.quota-resume"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
STATE_DIR="$HOME/Library/Application Support/codex-quota-resume"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
echo "UNINSTALLED: $LABEL"
echo "state kept at: $STATE_DIR"
echo "delete that directory to forget the sent history completely"
