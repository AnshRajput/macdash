#!/bin/sh
LABEL="dev.macdash"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
rm -rf "$HOME/.macdash"
echo "macdash removed (log left at ~/Library/Logs/macdash.log)"
