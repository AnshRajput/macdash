#!/bin/sh
# Install macdash as a per-user LaunchAgent. Safe to re-run (upgrades in place).
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.macdash"
LABEL="dev.macdash"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

# launchd has no shell env, so pick a real interpreter (no pyenv/asdf shims), preferring the shebang's
PY=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 "$(command -v python3 || true)"; do
  [ -x "$c" ] || continue
  r="$("$c" -c 'import sys;print(sys.executable)' 2>/dev/null)" || continue
  case "$r" in */shims/*|*/.pyenv/*|"") continue;; esac
  PY="$r"; break
done
xesc() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

[ "$(uname)" = "Darwin" ] || { echo "macdash only runs on macOS"; exit 1; }
[ -n "$PY" ] || { echo "python3 not found (install Xcode CLT or Homebrew python)"; exit 1; }

# clickable alerts; without it macdash falls back to osascript (attributed to Script Editor)
if ! command -v terminal-notifier >/dev/null 2>&1 && command -v brew >/dev/null 2>&1; then
  brew install terminal-notifier || true
fi

mkdir -p "$DEST" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cp "$SRC/macdash.py" "$SRC/index.html" "$DEST/"

cat > "$PLIST.new" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$(xesc "$PY")</string><string>$(xesc "$DEST")/macdash.py</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$(xesc "$HOME")/Library/Logs/macdash.log</string>
  <key>StandardErrorPath</key><string>$(xesc "$HOME")/Library/Logs/macdash.log</string>
</dict>
</plist>
EOF

# validate before tearing down the running agent
plutil -lint "$PLIST.new" >/dev/null || { rm -f "$PLIST.new"; echo "generated plist is invalid"; exit 1; }
mv "$PLIST.new" "$PLIST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "macdash running → http://127.0.0.1:7777  (first load can take a few seconds)"
