# macdash

**Find out why your Mac is slow.** A local dashboard that shows RAM, swap, CPU, thermal throttling, battery, disk and every running task. It sends a macOS notification when the battery is low or the Mac starts struggling. It also ships as a **Claude Code plugin**, so Claude can read the same live data and tell you what to close.

![macdash dashboard](docs/screenshot.png)

- **Lightweight.** It's one Python file and one HTML page, using only the standard library. There are no pip installs, no Electron app and no npm.
- **Private.** It binds to `127.0.0.1` only, is read-only (no kill or exec endpoints), rejects foreign `Host` headers, and makes no network calls.
- **Accurate on Apple Silicon.** Memory figures use `top`'s real footprint, which includes compressed and swapped pages that `ps` RSS misses. Throttling is read from macOS's thermal state, since `pmset -g therm` doesn't work on M-series chips.

## Install

```sh
git clone https://github.com/AnshRajput/macdash.git
cd macdash && sh install.sh
open http://127.0.0.1:7777
```

`install.sh` copies the app to `~/.macdash`, registers a LaunchAgent (`dev.macdash`) so macdash starts at login and restarts if it crashes, and installs `terminal-notifier` via Homebrew (clicking an alert opens the dashboard). You need macOS and `python3`.

Uninstall with `sh uninstall.sh`.

## Use it from Claude Code

```
/plugin marketplace add AnshRajput/macdash
/plugin install macdash@macdash
```

Then just ask: *"why is my Mac lagging?"*, *"what's eating my RAM?"*, or *"what's running right now?"* Claude starts macdash if needed, reads `/api/stats`, explains the biggest causes, and flags the usual leftovers: stale dev servers, booted simulators, Docker/Colima VMs, idle Gradle daemons and runaway `git` processes. It proposes exact commands and **never kills anything without your OK**.

## What it shows

| Panel | Details |
|---|---|
| Verdict | `OK` / `STRAINED` / `CRITICAL`, with the reason and the top offender |
| Battery | Percent, charging state, power source, time remaining |
| Memory | Wired / active / compressed / inactive / free, memory pressure, swap |
| CPU / thermal | Overall %, load averages, cores, thermal state (nominal → critical) |
| Disk / system | Free space, uptime, macOS version |
| Alerts | The last 50 alerts |
| Tasks running | Sortable, filterable process table (memory, CPU, uptime, full command) with a group-by-app toggle |

## Alerts

The checks run every 30s. Each alert type has a 15-minute cooldown, which resets once the condition clears.

- Battery **below 20%** and not charging
- Memory pressure **warn** or **critical**
- Swap **more than 75%** full
- Thermal state **serious** or **critical** (the Mac is throttling)
- CPU **above 90%** for 3 checks in a row

Each notification names the condition and the process most responsible.

## Commands

```sh
launchctl kickstart -k gui/$(id -u)/dev.macdash   # restart
launchctl bootout gui/$(id -u)/dev.macdash        # stop
tail -f ~/Library/Logs/macdash.log                # logs
python3 macdash.py --selftest                     # parser + alert-logic checks
python3 macdash.py --test-alert                   # send a sample notification
```

JSON API: `GET http://127.0.0.1:7777/api/stats`. Stats are refreshed in the background every 3s, so responses are instant.

## License

MIT
