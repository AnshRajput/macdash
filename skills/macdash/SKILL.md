---
name: macdash
description: Check this Mac's health and explain what's slowing it down. Use when the user says their Mac/MacBook is slow, lagging, hot, or out of memory; asks what is using RAM or CPU, what processes or tasks are running, what to close or kill; asks about battery, swap, thermal throttling, disk space, junk files or storage; or asks to install or open the macdash dashboard.
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
- `net` has `in_bps` / `out_bps` (bytes/s over the physical interfaces). `battery.health` has `cycle_count`, `condition` and `max_capacity_pct` (null if unavailable).
- `ports[]` lists listening TCP ports (port, pid, name, cmd).
- `suggestions[]` are ready-made cleanup hints (id, severity, title, detail, pids, mem_mb, `command`). macdash never runs the command. Start from these, then confirm with the user. `leaks[]` lists processes whose memory grew sharply in the last 30 min.
- `leaks[]` entries are `{pid, name, from_mb, to_mb}`.
- `config` is the effective alert thresholds (battery_low, battery_critical, swap_pct, cpu_pct, cpu_checks, disk_free_gb, cooldown_min, notify, leak_mb). `error` is null unless collection is failing.
- `storage` is a short summary: `scanned_at`, `running`, `safe_total_bytes`, `review_total_bytes` and the `top` 3 items. Full detail is in `/api/storage` (section 3).
- `GET /api/history` returns `{interval_s, max_points, points[]}`: up to 720 samples (one every ~10s while viewed, ~30s idle) of `t` (epoch s), `cpu`, `mem_used_pct`, `swap_pct`, `pressure_level`, `battery_pct`, `net_in`, `net_out`.

### How to use the new fields

- **"What should I close?"** Lead with `suggestions[]` (warn first), quote `title`, `mem_mb` and `command`. Add your own findings from `procs[]` only after.
- **"Is it getting worse?"** Fetch `/api/history` and compare the recent points with the older ones (cpu, mem_used_pct, swap_pct, pressure_level). Say whether each is rising, flat or falling.
- **"What's running on port X?"** Look it up in `ports[]` and report `name`, `pid` and `cmd`. If it isn't listed, nothing is listening on it.
- **Battery questions:** include `battery.health` (cycles, condition, max capacity) when relevant.

## 3. Storage review

When the user asks about disk space, junk files, caches or what is eating storage:

1. `curl -s http://127.0.0.1:7777/api/storage`. It returns instantly with the last scan: `scanned_at`, `duration_s`, `running`, `disk` (`total`, `used`, `free`, `purgeable` in bytes), `swap_files_gb`, `items[]` (`id`, `category`, `label`, `path`, `bytes`, `safety`, `command`, `note`), `safe_total_bytes`, `review_total_bytes` and `top_home[]` (biggest folders in `~`).
2. If `scanned_at` is null or old (hours), call `curl -s http://127.0.0.1:7777/api/storage/rescan`. It answers `{"scheduled": true|false}` (false means a scan is already running or one finished under a minute ago). Then poll `/api/storage` every 10-15s until `running` is false. A scan can take a few minutes. The first scan after a start happens about 60s in.
3. Summarize: free space, how much is reclaimable as `safe` versus `review`, then the biggest items with their `command`. Mention `top_home` for space that is not in the catalog. `keep` items are context only (for example the VM volume of swap files, which shrinks after a reboot).
4. **Never delete or run a cleanup command without the user's explicit confirmation.** For `safe` items, propose the exact `command` and wait for a yes. For `review` items (archives, backups, AVDs, VM disks, worktrees, Downloads installers, `.venv`) go item by item: say what it is, read the `note`, check it first (for example `git -C <repo> status` in a worktree, `ollama list`, `docker system df`), and ask. macdash has no delete endpoint; commands only ever run if you run them after a yes.

## 4. Report to the user

Lead with the verdict and the one or two biggest causes. Then give a short table of the top memory and CPU consumers, with what each one actually is, decoded from `cmd`. Flag the usual leftovers that are safe to close:

- dev servers (`next-server`, `vite`, `node`) running from `/tmp` or old worktrees
- booted iOS Simulators
- Docker, Colima or other VMs (`com.apple.Virtualization.VirtualMachine`)
- idle Gradle daemons (`java` under `~/.gradle/daemon`)
- leftover test runners (`flutter_tester`)
- long-running `git pack-objects` or `git push` processes

Each etime value shows how long a process has been running; use it to spot stale ones.

**Never kill a process without the user's explicit confirmation.** Propose exact commands (`kill <pid>`, `xcrun simctl shutdown all`, `colima stop`, `pkill -f GradleDaemon`) and wait for a yes. Don't kill Claude Code sessions or system processes.

Prefer `suggestions[]` over guessing: each already excludes Claude Code and system processes.

Point the user to `http://127.0.0.1:7777` for the live view. The background monitor posts macOS notifications when battery is under 20% (critical under 10%) and not charging, when disk free space is low, when a process leaks memory, when memory pressure is warn or critical, when swap is over 75% full, when thermal state is serious or worse, or when CPU stays over 90% for about 90s.

Uninstall: `sh "<skill base dir>/../../uninstall.sh"`.
