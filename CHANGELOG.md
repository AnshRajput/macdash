# Changelog

## 1.1.0

- Cleanup suggestions (stale dev servers, simulators, VMs, Gradle daemons, hung `flutter_tester`/`git`, duplicate Dart analysis servers). Commands are shown, never run.
- Memory leak watch and alert.
- `/api/history`: 2h of CPU, memory, swap, pressure, battery and network samples; TRENDS sparklines in the dashboard.
- Listening ports, network throughput and battery health (cycles, condition, max capacity).
- Low-disk and critical-battery alerts.
- Optional `~/.macdash/config.json` (thresholds, cooldown, `notify`) and `MACDASH_PORT`.
- Adaptive refresh: 3s while in use, 30s idle.
- Fixes: alert cooldown no longer resets on flapping, severity escalation notifies immediately, notification fallback to osascript, plist XML escaping and lint, stats served from the background sample only, log-flood guard, installer prefers a real Python over pyenv shims.
- Unit tests and GitHub Actions CI.

## 1.0.0

- Local dashboard on `127.0.0.1:7777`: verdict, battery, memory, swap, CPU, thermal, disk and process table.
- Background alert monitor with clickable notifications.
- LaunchAgent installer, Claude Code plugin and skill.
