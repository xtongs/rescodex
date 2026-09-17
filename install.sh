#!/bin/bash
# Install the rescodex watcher as a macOS LaunchAgent (runs every 60 seconds).
set -euo pipefail

LABEL="com.codex.quota-resume"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
STATE_DIR="$HOME/Library/Application Support/codex-quota-resume"

PYTHON3="$(command -v python3 || true)"
[ -n "$PYTHON3" ] || { echo "error: python3 not found" >&2; exit 1; }

CODEX="$(command -v codex || true)"
[ -z "$CODEX" ] && [ -x "$HOME/.local/bin/codex" ] && CODEX="$HOME/.local/bin/codex"
[ -n "$CODEX" ] || { echo "error: codex CLI not found" >&2; exit 1; }

echo "python3: $PYTHON3"
echo "codex:   $CODEX"
"$PYTHON3" "$PROJECT_DIR/watcher.py" --self-test

mkdir -p "$STATE_DIR" "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON3</string>
        <string>$PROJECT_DIR/watcher.py</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$(dirname "$CODEX"):$(dirname "$PYTHON3"):/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>CODEX_BIN</key>
        <string>$CODEX</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>StartInterval</key>
    <integer>60</integer>
    <key>StandardOutPath</key>
    <string>$STATE_DIR/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$STATE_DIR/launchd.err.log</string>
</dict>
</plist>
EOF

plutil -lint "$PLIST" >/dev/null

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"
launchctl kickstart "gui/$UID/$LABEL"

echo
echo "INSTALLED: $LABEL (checks every 60s)"
echo "state/log: $STATE_DIR"
sleep 2
"$PYTHON3" "$PROJECT_DIR/watcher.py" --status
