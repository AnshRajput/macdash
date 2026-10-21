# Changelog

## 1.2.0

- Storage review: a background scanner (own thread, niced `du`, per-path timeout, first scan about 60s after start, then every 6h, or on demand) measures a built-in catalog of caches and junk (Xcode, simulators, Android, Gradle, npm/pnpm/yarn/pip/pub/CocoaPods/Homebrew, Library caches and logs, Trash, old installers, iOS backups, Docker/Colima, Ollama) plus project build artifacts and stale agent worktrees under configurable `storage_roots`.
- STORAGE panel (STO-11): disk bar with purgeable space and swap files, reclaimable SAFE versus REVIEW totals, sortable list with SAFE / REVIEW / KEEP badges, COPY command, filter tabs and RESCAN.
- `GET /api/storage` and `GET /api/storage/rescan`; `/api/stats` gains a `storage` summary and a "Reclaimable storage" suggestion above 5 GB; the low-disk alert names the biggest safe item.
- Commands are suggestions only. macdash still never deletes or runs anything.
- Skill: new "Storage review" section.

## 1.1.0

- Cleanup suggestions (stale dev servers, simulators, VMs, Gradle daemons, hung `flutter_tester`/`git`, duplicate Dart analysis servers). Commands are shown, never run.
- Memory leak watch and alert.
- Uptime shown as `46M` / `3H 12M` / `2D 4H 1M` instead of an ambiguous `00:00:46`.
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
