#!/bin/sh
# Install macdash as a per-user LaunchAgent. Safe to re-run (upgrades in place).
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.macdash"
LABEL="dev.macdash"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$(command -v python3 || true)"

[ "$(uname)" = "Darwin" ] || { echo "macdash only runs on macOS"; exit 1; }
[ -n "$PY" ] || { echo "python3 not found (install Xcode CLT or Homebrew python)"; exit 1; }

# clickable alerts; without it macdash falls back to osascript (attributed to Script Editor)
if ! command -v terminal-notifier >/dev/null 2>&1 && command -v brew >/dev/null 2>&1; then
  brew install terminal-notifier || true
fi

mkdir -p "$DEST" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cp "$SRC/macdash.py" "$SRC/index.html" "$DEST/"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$DEST/macdash.py</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/macdash.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/macdash.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "macdash running → http://127.0.0.1:7777  (first load can take a few seconds)"
