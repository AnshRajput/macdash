---
name: macdash
description: Check this Mac's health and explain what's slowing it down. Use when the user says their Mac/MacBook is slow, lagging, hot, or out of memory; asks what is using RAM or CPU, what processes or tasks are running, what to close or kill; asks about battery, swap, thermal throttling, or disk space; or asks to install or open the macdash dashboard.
---

# macdash

macdash is a local, read-only telemetry server on `http://127.0.0.1:7777`. The dashboard is at `/` and the JSON is at `/api/stats`.

## 1. Make sure it's running

```sh
curl -s -m 20 http://127.0.0.1:7777/api/stats -o /dev/null -w "%{http_code}\n"
```

If it doesn't return `200`, run the installer from this plugin. It is two directories above this skill's base directory (`<skill base dir>/../../install.sh`):

```sh
sh "<skill base dir>/../../install.sh"
```

The installer copies the app to `~/.macdash` and registers a LaunchAgent (`dev.macdash`), so macdash starts at login. It also installs `terminal-notifier` via Homebrew if Homebrew is present. Tell the user the installer creates a login item before you run it. The first `/api/stats` call after a start can take 5–20s on a loaded machine.

## 2. Read the stats

```sh
curl -s -m 20 http://127.0.0.1:7777/api/stats
```

Key fields:

- `verdict.level` is `OK`, `STRAINED` or `CRITICAL`, and `verdict.reasons` says why.
- `mem` is in MB (total, wired, active, inactive, compressed, free). It also has `pressure_level` (`1` normal, `2` warn, `4` critical), `pressure` (the same as a word) and `free_pct` (the system free %).
- `swap` has total and used in MB, plus pct.
- `cpu` has `pct` (overall %), `load` (1/5/15-minute averages) and `ncpu`.
- `thermal` has `level` (`0` nominal, `1` fair, `2` serious, `3` critical) and `name`. Level 2 or higher means the Mac is throttling.
- `battery` has present, pct, state, charging, ac and remaining. `present` is false on desktops.
- `disk` has total and free in bytes. `uptime` is in seconds.
- `procs[]` lists processes with pid, name, mem (MB, the real footprint including compressed memory), cpu %, etime, cmd and app.
- `apps[]` is the same data grouped by app.
- `alerts[]` holds recent alerts.

## 3. Report to the user

Lead with the verdict and the one or two biggest causes. Then give a short table of the top memory and CPU consumers, with what each one actually is, decoded from `cmd`. Flag the usual leftovers that are safe to close:

- dev servers (`next-server`, `vite`, `node`) running from `/tmp` or old worktrees
- booted iOS Simulators
- Docker, Colima or other VMs (`com.apple.Virtualization.VirtualMachine`)
- idle Gradle daemons (`java` under `~/.gradle/daemon`)
- leftover test runners (`flutter_tester`)
- long-running `git pack-objects` or `git push` processes

Each etime value shows how long a process has been running; use it to spot stale ones.

**Never kill a process without the user's explicit confirmation.** Propose exact commands (`kill <pid>`, `xcrun simctl shutdown all`, `colima stop`, `pkill -f GradleDaemon`) and wait for a yes. Don't kill Claude Code sessions or system processes.

Point the user to `http://127.0.0.1:7777` for the live view. The background monitor posts macOS notifications when battery is under 20% and not charging, when memory pressure is warn or critical, when swap is over 75% full, when thermal state is serious or worse, or when CPU stays over 90% for about 90s.

Uninstall: `sh "<skill base dir>/../../uninstall.sh"`.
