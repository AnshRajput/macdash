# macdash
Local MacBook health dashboard + alerts. Stdlib only, binds 127.0.0.1:7777, read-only.
- Open: http://127.0.0.1:7777
- Install/start: `cp com.ansh.macdash.plist ~/Library/LaunchAgents/ && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ansh.macdash.plist`
- Stop: `launchctl bootout gui/$(id -u)/com.ansh.macdash`
- Restart: `launchctl kickstart -k gui/$(id -u)/com.ansh.macdash`
- Uninstall: stop, then `rm ~/Library/LaunchAgents/com.ansh.macdash.plist`
- Logs: `~/Library/Logs/macdash.log`
- Checks: `python3 macdash.py --selftest`, `python3 macdash.py --test-alert`
