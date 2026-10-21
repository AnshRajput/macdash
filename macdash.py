#!/opt/homebrew/bin/python3
"""macdash: local MacBook health dashboard + alerts. Stdlib only, read-only, 127.0.0.1 only."""
import json, os, re, shutil, subprocess, sys, threading, time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
try:
    PORT = int(os.environ.get("MACDASH_PORT") or 7777)
except ValueError:
    PORT = 7777
HERE = os.path.dirname(os.path.abspath(__file__))
CHECK_EVERY, CACHE_TTL = 30, 5
FAST, SLOW, ACTIVE_FOR = 3, 30, 60  # refresh seconds while viewed / idle, and the "viewed" window
HIST_EVERY, HIST_MAX = 9, 720      # a point every ~10s while viewed (30s idle), 720 points
PID_EVERY, PID_KEEP, LEAK_WINDOW, LEAK_MIN_SPAN = 60, 3600, 1800, 600
THERMAL = ["nominal", "fair", "serious", "critical"]
PRESSURE = {1: "normal", 2: "warn", 4: "critical"}
DEFAULTS = {"battery_low": 20, "battery_critical": 10, "swap_pct": 75, "cpu_pct": 90, "cpu_checks": 3,
            "disk_free_gb": 10, "cooldown_min": 15, "notify": True, "leak_mb": 500}

ALERTS, LAST_FIRED, STATE = [], {}, {"cpu_streak": 0}
LOCK = threading.Lock()
SNAP = {}                      # latest collect() result; only refresher() writes it
READY, WAKE = threading.Event(), threading.Event()
ACTIVITY = {"t": 0.0}          # last time the dashboard/API was requested
HIST = deque(maxlen=HIST_MAX)  # /api/history ring buffer
HISTLAST = {"t": 0.0}
PIDHIST, PIDLAST = {}, {"t": 0.0}  # pid -> [(t, footprint_mb)], sampled once a minute
NET = {}                       # previous netstat counters


def sh(cmd, timeout=8):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def load_config(path=None):
    """Defaults merged with ~/.macdash/config.json. Bad file or bad value -> default, never raises."""
    path = path or os.environ.get("MACDASH_CONFIG") or os.path.expanduser("~/.macdash/config.json")
    cfg = dict(DEFAULTS)
    try:
        with open(path) as f:
            user = json.load(f)
        if not isinstance(user, dict):
            return cfg
    except (OSError, ValueError):
        return cfg
    for k, d in DEFAULTS.items():
        v = user.get(k, d)
        if isinstance(d, bool):
            ok = isinstance(v, bool)
        else:
            ok = isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= (100 if k.endswith("_pct") or k.startswith("battery_") else 1e9)
        if ok:
            cfg[k] = v
    cfg["cpu_checks"] = max(1, int(cfg["cpu_checks"]))
    return cfg


CFG = load_config()


# ---------- parsers (covered by test_macdash.py) ----------
def parse_batt(txt):
    m = re.search(r"(\d+)%;\s*([^;]+);\s*([^;\n]*)", txt)
    if not m:
        return {"present": False}
    state = m.group(2).strip()
    rem = re.search(r"(\d+:\d+) remaining", m.group(3))
    return {"present": True, "pct": int(m.group(1)), "state": state,
            "charging": state in ("charging", "finishing charge", "finishing charging"),
            "ac": "AC Power" in txt, "remaining": rem.group(1) if rem else None}


def parse_batt_health(txt):
    """system_profiler SPPowerDataType -> {cycle_count, condition, max_capacity_pct} or None."""
    cyc = re.search(r"Cycle Count:\s*(\d+)", txt)
    con = re.search(r"Condition:\s*(.+)", txt)
    cap = re.search(r"Maximum Capacity:\s*(\d+)\s*%", txt)
    if not (cyc or con or cap):
        return None
    return {"cycle_count": int(cyc.group(1)) if cyc else None,
            "condition": con.group(1).strip() if con else None,
            "max_capacity_pct": int(cap.group(1)) if cap else None}


def parse_swap(txt):
    m = re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M", txt)
    if not m:
        return {"total": 0, "used": 0, "pct": 0}
    t, u = float(m.group(1)), float(m.group(2))
    return {"total": t, "used": u, "pct": round(100 * u / t, 1) if t else 0}


def parse_vmstat(txt, total_bytes):
    ps = int((re.search(r"page size of (\d+) bytes", txt) or [0, 16384])[1])
    def pg(label):
        m = re.search(r"^%s:\s+(\d+)" % label, txt, re.M)
        return int(m.group(1)) if m else 0
    mb = lambda p: round(p * ps / 1048576)
    d = {"wired": mb(pg("Pages wired down")), "active": mb(pg("Pages active")),
         "inactive": mb(pg("Pages inactive")),
         "compressed": mb(pg("Pages occupied by compressor")),
         "free": mb(pg("Pages free") + pg("Pages speculative"))}
    d["total"] = round(total_bytes / 1048576)
    return d


def parse_top_mem(v):
    m = re.match(r"([\d.]+)([KMGB])", v)
    if not m:
        return 0.0
    return float(m.group(1)) * {"K": 1 / 1024, "M": 1, "G": 1024, "B": 1 / 1048576}[m.group(2)]


def parse_top(txt):
    rows, seen = [], False
    for ln in txt.splitlines():
        if ln.startswith("PID"):
            seen = True
            continue
        p = ln.split(None, 4)
        if seen and len(p) == 5 and p[0].isdigit():
            rows.append((int(p[0]), p[4].strip(), parse_top_mem(p[1])))
    return rows


def parse_etime(v):
    """ps etime [[dd-]hh:]mm:ss -> seconds (0 if unparseable)."""
    try:
        d, _, t = v.rpartition("-")
        parts = [int(x) for x in t.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        return int(d or 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
    except ValueError:
        return 0


def parse_ps(txt):
    """ps -A -o pid=,%cpu=,etime=,rss=,command= -> {pid: (cpu, etime, rss_mb, command)}; bad rows skipped."""
    out = {}
    for ln in txt.splitlines():
        p = ln.split(None, 4)
        try:
            if len(p) == 5 and p[0].isdigit():
                out[int(p[0])] = (float(p[1]), p[2], round(int(p[3]) / 1024, 1), p[4])
        except ValueError:
            continue
    return out


def parse_netstat(txt):
    """netstat -ibn -> (bytes_in, bytes_out) summed over physical en* interfaces (Link rows only)."""
    ib = ob = 0
    for ln in txt.splitlines():
        p = ln.split()
        if len(p) >= 10 and re.fullmatch(r"en\d+", p[0]) and p[2].startswith("<Link#"):
            try:  # parse from the right: the MAC address column is sometimes absent
                ib, ob = ib + int(p[-5]), ob + int(p[-2])
            except ValueError:
                pass
    return ib, ob


def parse_lsof(txt, ps=None):
    """lsof -nP -iTCP -sTCP:LISTEN -> [{port,pid,name,cmd}] deduped by port+pid, sorted by port."""
    seen = {}
    for ln in txt.splitlines():
        p = ln.split(None, 2)
        m = re.search(r":(\d+) \(LISTEN\)\s*$", ln)
        if len(p) < 3 or not p[1].isdigit() or not m:
            continue
        pid, port = int(p[1]), int(m.group(1))
        seen.setdefault((port, pid), {"port": port, "pid": pid, "name": p[0].replace("\\x20", " "),
                                      "cmd": (ps or {}).get(pid, (0, 0, 0, ""))[3]})
    return sorted(seen.values(), key=lambda r: (r["port"], r["pid"]))


def parse_simctl_booted(txt):
    return re.findall(r"^\s+(.+?) \([0-9A-Fa-f-]{36}\) \(Booted\)", txt, re.M)


def app_of(cmd, name):
    m = re.search(r"/([^/]+)\.app(?:/|$)", cmd)
    return m.group(1) if m else name


# ---------- cached collectors ----------
_cache = {}
def cached(key, fn, ttl=CACHE_TTL):
    with LOCK:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    val = fn()
    with LOCK:
        _cache[key] = (time.time(), val)
    return val


def sysint(name, default=0):
    try:
        return int(sh(["sysctl", "-n", name]).strip())
    except ValueError:
        return default


def hwint(name, default=0):
    """Constant hardware values: read once, cached for the run."""
    return cached("hw:" + name, lambda: sysint(name, default), 10 ** 9)


def get_thermal():
    out = sh(["osascript", "-l", "JavaScript", "-e",
              'ObjC.import("Foundation"); $.NSProcessInfo.processInfo.thermalState']).strip()
    lvl = int(out) if out.isdigit() and int(out) < 4 else 0
    return {"level": lvl, "name": THERMAL[lvl]}


def get_mem():
    total = hwint("hw.memsize")
    d = parse_vmstat(sh(["vm_stat"]), total)
    lvl = sysint("kern.memorystatus_vm_pressure_level", 1)
    d["pressure_level"], d["pressure"] = lvl, PRESSURE.get(lvl, "normal")
    m = re.search(r"(\d+)%", (sh(["memory_pressure"]).strip().splitlines() or [""])[-1])
    d["free_pct"] = int(m.group(1)) if m else None
    return d


def get_procs():
    ps = parse_ps(sh(["ps", "-A", "-o", "pid=,%cpu=,etime=,rss=,command="]))
    allcpu = sum(v[0] for v in ps.values())
    procs = []
    for pid, name, mem in parse_top(sh(["top", "-l", "1", "-o", "mem", "-n", "60",
                                         "-stats", "pid,mem,cpu,time,command"], 15)):
        cpu, et, _, cmd = ps.get(pid, (0.0, "", 0, name))
        full = os.path.basename(re.split(r" -|\.app/Contents/[^ ]*? ", cmd)[0].split(" /")[0])
        if " " not in full and full.startswith(name.rstrip("+-")) and len(full) > len(name):
            name = full  # top truncates names
        procs.append({"pid": pid, "name": name, "mem": round(mem, 1), "cpu": cpu,
                      "etime": et, "cmd": cmd, "app": app_of(cmd, name)})
    # top CPU consumers might not be in the top-60-by-mem list; add the worst few
    have = {p["pid"] for p in procs}
    for pid, (cpu, et, _, cmd) in sorted(ps.items(), key=lambda kv: -kv[1][0])[:5]:
        if pid not in have and cpu > 0:
            nm = os.path.basename(cmd.split(" -")[0].split(" --")[0]) or cmd
            procs.append({"pid": pid, "name": nm, "mem": 0, "cpu": cpu, "etime": et,
                          "cmd": cmd, "app": app_of(cmd, nm)})
    groups = {}
    for p in procs:
        g = groups.setdefault(p["app"], {"name": p["app"], "n": 0, "mem": 0, "cpu": 0})
        g["n"] += 1; g["mem"] += p["mem"]; g["cpu"] += p["cpu"]
    for g in groups.values():
        g["mem"], g["cpu"] = round(g["mem"], 1), round(g["cpu"], 1)
    return {"procs": procs, "apps": sorted(groups.values(), key=lambda g: -g["mem"]),
            "allcpu": allcpu, "ps": ps}


def net_rate(prev, cur, now):
    """Counter deltas -> bytes/s. prev = (t, in, out) or None. Counter reset -> 0 for that tick."""
    if not prev or now - prev[0] < 0.5:
        return 0, 0
    dt = now - prev[0]
    return max(0, round((cur[0] - prev[1]) / dt)), max(0, round((cur[1] - prev[2]) / dt))


def get_net(now):
    cur = parse_netstat(sh(["netstat", "-ibn"]))
    i, o = net_rate(NET.get("prev"), cur, now)
    NET["prev"] = (now, cur[0], cur[1])
    return {"in_bps": i, "out_bps": o}


def build_plist(ps, procs):
    """Full process table for the suggestion rules: top footprint where known, else RSS."""
    mem = {p["pid"]: p["mem"] for p in procs}
    return {pid: {"pid": pid, "cmd": cmd, "etime_s": parse_etime(et), "mem": mem.get(pid) or rss,
                  "name": os.path.basename(cmd.split(" -")[0].split(" /")[0]) or cmd}
            for pid, (cpu, et, rss, cmd) in ps.items()}


def update_pidhist(hist, plist, now):
    for pid in list(hist):
        if pid not in plist:
            del hist[pid]  # dead
    for pid, p in plist.items():
        if p["mem"] > 0:
            h = hist.setdefault(pid, [])
            if h and p["etime_s"] + 120 < now - h[0][0]:
                h.clear()  # pid reused by a younger process
            h.append((now, p["mem"]))
            while h and now - h[0][0] > PID_KEEP:
                h.pop(0)


# Never suggest touching these (Claude Code, window server, kernel, launchd, system daemons).
PROTECTED = re.compile(r"claude|WindowServer|kernel_task|(?:^|/)launchd\b|^/System/(?!.*Virtualization)|^/usr/(?:libexec|sbin)/", re.I)
DEV = re.compile(r"next-server|\bvite\b|webpack|\bnode\b.*\bdev\b")
VM = re.compile(r"com\.apple\.Virtualization\.VirtualMachine|qemu-system|colima|limactl|com\.docker|orbstack", re.I)
GIT = re.compile(r"(?:^|/)git\s+(?:-\S+\s+)*(?:pack-objects|push|fetch)\b")
DARTLS = re.compile(r"dart.*language-server|analysis_server")


def detect_leaks(hist, plist, now, leak_mb):
    """Footprint grew > leak_mb AND > 50% over the last 30 min (needs >= 10 min of samples)."""
    out = []
    for pid, h in hist.items():
        win = [x for x in h if now - x[0] <= LEAK_WINDOW]
        if pid not in plist or len(win) < 2 or win[-1][0] - win[0][0] < LEAK_MIN_SPAN:
            continue
        a, b = win[0][1], win[-1][1]
        if b - a > leak_mb and b > a * 1.5 and not PROTECTED.search(plist[pid]["cmd"]):
            out.append({"pid": pid, "name": plist[pid]["name"], "from_mb": round(a), "to_mb": round(b)})
    return sorted(out, key=lambda x: x["from_mb"] - x["to_mb"])  # biggest growth first


def vm_owner(cmds):
    for pat, owner in (("-avd", "Android emulator"), ("orbstack", "OrbStack"), ("colima", "Colima"), ("lima", "Lima"), ("docker", "Docker Desktop"),
                       ("/utm.app", "UTM"), ("qemu", "QEMU"), ("parallels", "Parallels")):
        if any(pat in c.lower() for c in cmds):
            return owner
    return None


def suggest(plist, sims, leaks, cfg=None):
    """Cleanup hints. `command` is only ever shown to the user, macdash never runs it."""
    cfg = cfg or CFG
    ps = [p for p in plist.values() if not PROTECTED.search(p["cmd"])]
    out = []
    def add(id, sev, title, detail, procs, command):
        out.append({"id": id, "severity": sev, "title": title, "detail": detail,
                    "pids": sorted(p["pid"] for p in procs),
                    "mem_mb": round(sum(p["mem"] for p in procs)), "command": command})
    kill = lambda procs: "kill " + " ".join(str(p["pid"]) for p in sorted(procs, key=lambda p: p["pid"]))
    dev = [p for p in ps if DEV.search(p["cmd"]) and (re.search(r"/(?:private/)?tmp/", p["cmd"]) or p["etime_s"] > 86400)]
    if dev:
        add("dev-servers", "warn", "%d stale dev server%s" % (len(dev), "s" * (len(dev) > 1)),
            "Running from a temp dir or for over 24h: " + ", ".join(sorted({p["name"] for p in dev})), dev, kill(dev))
    if sims:
        add("ios-simulators", "warn", "%d iOS simulator%s booted" % (len(sims), "s" * (len(sims) > 1)),
            ", ".join(sims), [], "xcrun simctl shutdown all")
    vms = [p for p in plist.values() if VM.search(p["cmd"]) and not re.search(r"claude|launchd", p["cmd"], re.I)]
    groups = {}
    for p in vms:
        groups.setdefault(vm_owner([p["cmd"]]), []).append(p)
    orphans = groups.pop(None, [])  # e.g. Virtualization.framework VM: belongs to the single framework-based owner, if any
    fw = [o for o in groups if o not in ("Android emulator", "QEMU")]
    if orphans and len(fw) == 1:
        groups[fw[0]] += orphans
        orphans = []
    if orphans:
        groups[None] = orphans
    for owner, g in groups.items():
        cmd = {"Colima": "colima stop", "OrbStack": "orbctl stop", "Android emulator": "adb emu kill",
               "Docker Desktop": "osascript -e 'quit app \"Docker\"'"}.get(
            owner, "ps -o pid,etime,command -p " + ",".join(str(p["pid"]) for p in g))
        mem = sum(p["mem"] for p in g)
        add("vms" if len(groups) == 1 else "vms-" + re.sub(r"\W+", "", owner or "unknown").lower(),
            "warn" if mem > 2048 else "info", "Virtual machine running" + (" (%s)" % owner if owner else ""),
            "%d process%s%s" % (len(g), "es" * (len(g) > 1), "" if owner else ", owner not detected"), g, cmd)
    gr = [p for p in ps if "GradleDaemon" in p["cmd"] and "java" in p["cmd"]]
    if gr:
        add("gradle", "info", "%d Gradle daemon%s" % (len(gr), "s" * (len(gr) > 1)),
            "Idle daemons keep their heap until stopped", gr, "pkill -f GradleDaemon")
    ft = [p for p in ps if "flutter_tester" in p["cmd"] and p["etime_s"] > 3600]
    if ft:
        add("flutter-tester", "warn", "flutter_tester running over 1h", "Likely a hung test run", ft, kill(ft))
    git = [p for p in ps if GIT.search(p["cmd"]) and p["etime_s"] > 1800]
    if git:
        add("git-stuck", "warn", "git running over 30 min", ", ".join(sorted({p["cmd"][:60] for p in git})), git, kill(git))
    dls = sorted((p for p in ps if DARTLS.search(p["cmd"])), key=lambda p: -p["mem"])
    if len(dls) > 1:
        add("dart-analysis", "info", "%d Dart analysis servers" % len(dls),
            "Usually one per editor window; command keeps the biggest (likely busiest), check the others first", dls, kill(dls[1:]))
    for l in leaks:
        if l["pid"] not in plist or PROTECTED.search(plist[l["pid"]]["cmd"]):
            continue
        add("leak-%d" % l["pid"], "warn", "%s memory growing" % l["name"],
            "%d MB to %d MB in the last 30 min (leak_mb=%s)" % (l["from_mb"], l["to_mb"], cfg["leak_mb"]),
            [plist[l["pid"]]], "kill %d" % l["pid"])
    return out


def collect():
    now = time.time()
    ncpu = hwint("hw.ncpu", 1)
    pr = cached("procs", get_procs)
    load = [float(x) for x in re.findall(r"[\d.]+", sh(["sysctl", "-n", "vm.loadavg"]))[:3]]
    bt = re.search(r"sec = (\d+)", sh(["sysctl", "-n", "kern.boottime"]))
    du = shutil.disk_usage("/")
    batt = dict(cached("batt", lambda: parse_batt(sh(["pmset", "-g", "batt"]))))
    batt["health"] = cached("health", lambda: parse_batt_health(sh(["system_profiler", "SPPowerDataType"], 20)), 600) \
        if batt.get("present") else None
    plist = build_plist(pr["ps"], pr["procs"])
    if now - PIDLAST["t"] >= PID_EVERY:
        PIDLAST["t"] = now
        update_pidhist(PIDHIST, plist, now)
    leaks = detect_leaks(PIDHIST, plist, now, CFG["leak_mb"])
    sims = cached("sims", lambda: parse_simctl_booted(sh(["xcrun", "simctl", "list", "devices", "booted"])), 30)
    return {
        "ts": now,
        "battery": batt,
        "thermal": cached("therm", get_thermal),
        "mem": cached("mem", get_mem),
        "swap": parse_swap(sh(["sysctl", "-n", "vm.swapusage"])),
        "cpu": {"pct": round(min(100.0, pr["allcpu"] / ncpu), 1), "load": load, "ncpu": ncpu},
        "disk": {"total": du.total, "free": du.free},
        "net": get_net(now),
        "uptime": int(now - int(bt.group(1))) if bt else None,
        "os": cached("os", lambda: sh(["sw_vers", "-productVersion"]).strip(), 86400),
        "procs": pr["procs"], "apps": pr["apps"],
        "ports": cached("ports", lambda: parse_lsof(sh(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"], 10), pr["ps"]), 15),
        "leaks": leaks,
        "suggestions": suggest(plist, sims, leaks, CFG),
    }


def history_point(s):
    m, b = s["mem"], s["battery"]
    used = m["wired"] + m["active"] + m["compressed"]
    return {"t": round(s["ts"]), "cpu": s["cpu"]["pct"],
            "mem_used_pct": round(100 * used / m["total"], 1) if m.get("total") else None,
            "swap_pct": s["swap"]["pct"], "pressure_level": m["pressure_level"],
            "battery_pct": b["pct"] if b.get("present") else None,
            "net_in": s["net"]["in_bps"], "net_out": s["net"]["out_bps"]}


# ---------- alert decisions ----------
def top_name(s, key):
    p = max(s["procs"], key=lambda p: p[key], default=None)
    if p and not p[key]:
        p = None  # top sample failed (all zero): don't blame a random process
    return "%s (%s)" % (p["app"], ("%d MB" % p["mem"]) if key == "mem" else ("%.0f%% CPU" % p["cpu"])) if p else "unknown"


def conditions(s, cpu_streak, cfg=None):
    """-> list of (type, severity 1|2, text). severity 2 = critical."""
    cfg = cfg or CFG
    out, b = [], s["battery"]
    if b.get("present") and not b["charging"]:
        if b["pct"] < cfg["battery_critical"]:
            out.append(("battery_critical", 2, "Battery critical: %d%% and not charging" % b["pct"]))
        elif b["pct"] < cfg["battery_low"]:
            out.append(("battery", 1, "Battery low: %d%% and not charging" % b["pct"]))
    m = s["mem"]
    if m["pressure_level"] >= 2:
        out.append(("mem_pressure", 2 if m["pressure_level"] >= 4 else 1,
                    "Memory pressure %s. Top memory: %s" % (m["pressure"].upper(), top_name(s, "mem"))))
    sw = s["swap"]
    if sw["total"] and sw["pct"] > cfg["swap_pct"]:
        out.append(("swap", 2 if sw["pct"] > 90 else 1,
                    "Swap %.0f%% full. Top memory: %s" % (sw["pct"], top_name(s, "mem"))))
    if s["thermal"]["level"] >= 2:
        out.append(("thermal", 2 if s["thermal"]["level"] >= 3 else 1,
                    "Thermal %s, Mac is throttling. Top CPU: %s" % (s["thermal"]["name"].upper(), top_name(s, "cpu"))))
    if cpu_streak >= cfg["cpu_checks"]:
        out.append(("cpu", 1, "CPU above %g%% for %d checks. Top CPU: %s" % (cfg["cpu_pct"], cpu_streak, top_name(s, "cpu"))))
    free = s.get("disk", {}).get("free")
    if free is not None and free < cfg["disk_free_gb"] * 1024 ** 3:
        out.append(("disk_low", 2 if free < cfg["disk_free_gb"] * 1024 ** 3 / 2 else 1,
                    "Disk low: %.1f GB free" % (free / 1024 ** 3)))
    if s.get("leaks"):
        l = s["leaks"][0]
        out.append(("leak", 1, "Possible memory leak: %s grew %d MB to %d MB in 30 min" % (l["name"], l["from_mb"], l["to_mb"])))
    return out


TN = shutil.which("terminal-notifier") or "/opt/homebrew/bin/terminal-notifier"


def notify(text):
    # terminal-notifier: click opens the dashboard. osascript fallback is attributed to
    # Script Editor, so clicking it opens Script Editor's file picker.
    if os.access(TN, os.X_OK):
        try:
            if subprocess.run([TN, "-title", "MacDash", "-message", text, "-sound", "Basso", "-group", "macdash",
                               "-open", "http://127.0.0.1:%d/" % PORT], capture_output=True, timeout=8).returncode == 0:
                return
        except Exception:
            pass
    t = text.replace("\\", "\\\\").replace('"', '\\"')
    sh(["osascript", "-e", 'display notification "%s" with title "MacDash" sound name "Basso"' % t])


def check_once(s=None, now=None):
    s = s or collect()
    now = time.time() if now is None else now
    STATE["cpu_streak"] = STATE["cpu_streak"] + 1 if s["cpu"]["pct"] > CFG["cpu_pct"] else 0
    active = {(t, sev): txt for t, sev, txt in conditions(s, STATE["cpu_streak"], CFG)}
    cool = CFG["cooldown_min"] * 60
    for k in list(LAST_FIRED):
        if now - LAST_FIRED[k] >= cool:
            del LAST_FIRED[k]  # prune only on expiry, so flapping can't dodge the cooldown
    for (t, sev), txt in active.items():
        if (t, sev) in LAST_FIRED:
            continue
        LAST_FIRED[(t, sev)] = now
        with LOCK:
            ALERTS.append({"ts": now, "type": t, "text": txt})
            del ALERTS[:-50]
        print("ALERT", t, txt, flush=True)
        if CFG["notify"]:
            notify(txt)


def refresher():
    last = (None, 0)
    while True:
        try:
            s = collect()
            SNAP["s"], SNAP["err"] = s, None
            if s["ts"] - HISTLAST["t"] >= HIST_EVERY:
                HISTLAST["t"] = s["ts"]
                HIST.append(history_point(s))
            READY.set()
        except Exception as e:
            msg, now = "%s: %s" % (type(e).__name__, e), time.time()
            SNAP["err"] = msg
            if msg != last[0] or now - last[1] >= 60:  # don't flood the log
                print("collect error", msg, flush=True)
                last = (msg, now)
        WAKE.wait(FAST if time.time() - ACTIVITY["t"] < ACTIVE_FOR else SLOW)
        WAKE.clear()


def touch():
    if time.time() - ACTIVITY["t"] >= ACTIVE_FOR:
        WAKE.set()  # idle -> viewed: refresh now instead of waiting out the slow interval
    ACTIVITY["t"] = time.time()


def snapshot(timeout=0):
    """Latest snapshot or None. Never collects in the caller's thread."""
    return dict(SNAP["s"]) if READY.wait(timeout) and SNAP.get("s") else None


def monitor():
    READY.wait()
    while True:
        try:
            s = snapshot()
            if s:
                check_once(s)
        except Exception as e:
            print("monitor error", e, flush=True)
        time.sleep(CHECK_EVERY)


def api_stats(s):
    pct = s["cpu"]["pct"]
    conds = conditions(s, STATE["cpu_streak"] if pct > CFG["cpu_pct"] else 0, CFG)  # live CPU, not the 30s-old streak
    lvl = "CRITICAL" if any(c[1] == 2 for c in conds) else "STRAINED" if conds else "OK"
    s["verdict"] = {"level": lvl, "reasons": [c[2] for c in conds]}
    with LOCK:
        s["alerts"] = list(reversed(ALERTS))
    s["config"] = CFG
    s["error"] = SNAP.get("err")
    return s


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body, ct):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # reject foreign Host headers (DNS rebinding)
        if self.headers.get("Host", "").split(":")[0] not in ("127.0.0.1", "localhost"):
            return self.send_error(403)
        path = self.path.split("?")[0]
        if path not in ("/", "/api/stats", "/api/history"):
            return self.send_error(404)
        touch()
        if path == "/":
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self.reply(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/history":
            return self.reply(200, json.dumps({"interval_s": 10, "max_points": HIST_MAX, "points": list(HIST)}).encode(),
                              "application/json")
        s = snapshot(20)
        if s is None:  # first collect still running (or failing)
            return self.reply(503, json.dumps({"error": SNAP.get("err") or "warming up"}).encode(), "application/json")
        self.reply(200, json.dumps(api_stats(s)).encode(), "application/json")


def selftest():
    import unittest
    sys.path.insert(0, HERE)
    r = unittest.main(module="test_macdash", argv=["macdash", "-v"], exit=False)
    sys.exit(0 if r.result.wasSuccessful() else 1)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--test-alert" in sys.argv:
        notify("Test alert: MacDash notifications are working")
    else:
        threading.Thread(target=refresher, daemon=True).start()
        threading.Thread(target=monitor, daemon=True).start()
        srv = ThreadingHTTPServer((HOST, PORT), H)
        print("macdash listening on http://%s:%d" % (HOST, PORT), flush=True)
        srv.serve_forever()
