#!/opt/homebrew/bin/python3
"""macdash: local MacBook health dashboard + alerts. Stdlib only, read-only, 127.0.0.1 only."""
import json, os, re, shutil, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = "127.0.0.1", 7777
HERE = os.path.dirname(os.path.abspath(__file__))
CHECK_EVERY, COOLDOWN, CACHE_TTL = 30, 15 * 60, 5
THERMAL = ["nominal", "fair", "serious", "critical"]
PRESSURE = {1: "normal", 2: "warn", 4: "critical"}

ALERTS, LAST_FIRED, STATE = [], {}, {"cpu_streak": 0}
LOCK = threading.Lock()


def sh(cmd, timeout=8):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


# ---------- parsers (covered by --selftest) ----------
def parse_batt(txt):
    m = re.search(r"(\d+)%;\s*([^;]+);\s*([^;\n]*)", txt)
    if not m:
        return {"present": False}
    state = m.group(2).strip()
    rem = re.search(r"(\d+:\d+) remaining", m.group(3))
    return {"present": True, "pct": int(m.group(1)), "state": state,
            "charging": state in ("charging", "finishing charge", "finishing charging"),
            "ac": "AC Power" in txt, "remaining": rem.group(1) if rem else None}


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


def app_of(cmd, name):
    m = re.search(r"/([^/]+)\.app(?:/|$)", cmd)
    return m.group(1) if m else name


# ---------- cached collectors ----------
_cache = {}
def cached(key, fn):
    with LOCK:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
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


def get_thermal():
    out = sh(["osascript", "-l", "JavaScript", "-e",
              'ObjC.import("Foundation"); $.NSProcessInfo.processInfo.thermalState']).strip()
    lvl = int(out) if out.isdigit() and int(out) < 4 else 0
    return {"level": lvl, "name": THERMAL[lvl]}


def get_mem():
    total = sysint("hw.memsize")
    d = parse_vmstat(sh(["vm_stat"]), total)
    lvl = sysint("kern.memorystatus_vm_pressure_level", 1)
    d["pressure_level"], d["pressure"] = lvl, PRESSURE.get(lvl, "normal")
    m = re.search(r"(\d+)%", (sh(["memory_pressure"]).strip().splitlines() or [""])[-1])
    d["free_pct"] = int(m.group(1)) if m else None
    return d


def get_procs():
    ps = {}
    for ln in sh(["ps", "-A", "-o", "pid=,%cpu=,etime=,command="]).splitlines():
        p = ln.split(None, 3)
        if len(p) == 4 and p[0].isdigit():
            ps[int(p[0])] = (float(p[1]), p[2], p[3])
    allcpu = sum(v[0] for v in ps.values())
    procs = []
    for pid, name, mem in parse_top(sh(["top", "-l", "1", "-o", "mem", "-n", "60",
                                         "-stats", "pid,mem,cpu,time,command"], 15)):
        cpu, et, cmd = ps.get(pid, (0.0, "", name))
        full = os.path.basename(re.split(r" -|\.app/Contents/[^ ]*? ", cmd)[0].split(" /")[0])
        if " " not in full and full.startswith(name.rstrip("+-")) and len(full) > len(name):
            name = full  # top truncates names
        procs.append({"pid": pid, "name": name, "mem": round(mem, 1), "cpu": cpu,
                      "etime": et, "cmd": cmd, "app": app_of(cmd, name)})
    # top CPU consumers might not be in the top-60-by-mem list; add the worst few
    have = {p["pid"] for p in procs}
    for pid, (cpu, et, cmd) in sorted(ps.items(), key=lambda kv: -kv[1][0])[:5]:
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
    return {"procs": procs, "apps": sorted(groups.values(), key=lambda g: -g["mem"]), "allcpu": allcpu}


def collect():
    ncpu = sysint("hw.ncpu", 1)
    pr = cached("procs", get_procs)
    load = [float(x) for x in re.findall(r"[\d.]+", sh(["sysctl", "-n", "vm.loadavg"]))[:3]]
    bt = re.search(r"sec = (\d+)", sh(["sysctl", "-n", "kern.boottime"]))
    du = shutil.disk_usage("/")
    return {
        "ts": time.time(),
        "battery": cached("batt", lambda: parse_batt(sh(["pmset", "-g", "batt"]))),
        "thermal": cached("therm", get_thermal),
        "mem": cached("mem", get_mem),
        "swap": parse_swap(sh(["sysctl", "-n", "vm.swapusage"])),
        "cpu": {"pct": round(min(100.0, pr["allcpu"] / ncpu), 1), "load": load, "ncpu": ncpu},
        "disk": {"total": du.total, "free": du.free},
        "uptime": int(time.time() - int(bt.group(1))) if bt else None,
        "os": cached("os", lambda: sh(["sw_vers", "-productVersion"]).strip()),
        "procs": pr["procs"], "apps": pr["apps"],
    }


# ---------- alert decisions ----------
def top_name(s, key):
    p = max(s["procs"], key=lambda p: p[key], default=None)
    return "%s (%s)" % (p["app"], ("%d MB" % p["mem"]) if key == "mem" else ("%.0f%% CPU" % p["cpu"])) if p else "unknown"


def conditions(s, cpu_streak):
    """-> list of (type, severity 1|2, text). severity 2 = critical."""
    out, b = [], s["battery"]
    if b.get("present") and b["pct"] < 20 and not b["charging"]:
        out.append(("battery", 2 if b["pct"] < 10 else 1, "Battery low: %d%% and not charging" % b["pct"]))
    m = s["mem"]
    if m["pressure_level"] >= 2:
        out.append(("mem_pressure", 2 if m["pressure_level"] >= 4 else 1,
                    "Memory pressure %s. Top memory: %s" % (m["pressure"].upper(), top_name(s, "mem"))))
    if s["swap"]["total"] and s["swap"]["pct"] > 75:
        out.append(("swap", 2 if s["swap"]["pct"] > 90 else 1,
                    "Swap %.0f%% full. Top memory: %s" % (s["swap"]["pct"], top_name(s, "mem"))))
    if s["thermal"]["level"] >= 2:
        out.append(("thermal", 2 if s["thermal"]["level"] >= 3 else 1,
                    "Thermal %s, Mac is throttling. Top CPU: %s" % (s["thermal"]["name"].upper(), top_name(s, "cpu"))))
    if cpu_streak >= 3:
        out.append(("cpu", 1, "CPU above 90%% for %d checks. Top CPU: %s" % (cpu_streak, top_name(s, "cpu"))))
    return out


def notify(text):
    t = text.replace("\\", "\\\\").replace('"', '\\"')
    sh(["osascript", "-e", 'display notification "%s" with title "MacDash" sound name "Basso"' % t])


def check_once(s=None):
    s = s or collect()
    STATE["cpu_streak"] = STATE["cpu_streak"] + 1 if s["cpu"]["pct"] > 90 else 0
    active = {t: txt for t, _, txt in conditions(s, STATE["cpu_streak"])}
    now = time.time()
    for t in list(LAST_FIRED):
        if t not in active:
            del LAST_FIRED[t]  # condition cleared: reset cooldown
    for t, txt in active.items():
        if now - LAST_FIRED.get(t, 0) >= COOLDOWN:
            LAST_FIRED[t] = now
            ALERTS.append({"ts": now, "type": t, "text": txt})
            del ALERTS[:-50]
            notify(txt)
            print("ALERT", t, txt, flush=True)


def monitor():
    while True:
        try:
            check_once()
        except Exception as e:
            print("monitor error", e, flush=True)
        time.sleep(CHECK_EVERY)


def api_stats():
    s = collect()
    conds = conditions(s, STATE["cpu_streak"])
    lvl = "CRITICAL" if any(c[1] == 2 for c in conds) else "STRAINED" if conds else "OK"
    s["verdict"] = {"level": lvl, "reasons": [c[2] for c in conds]}
    s["alerts"] = list(reversed(ALERTS))
    return s


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        # reject foreign Host headers (DNS rebinding)
        if self.headers.get("Host", "").split(":")[0] not in ("127.0.0.1", "localhost"):
            return self.send_error(403)
        path = self.path.split("?")[0]
        if path == "/":
            body, ct = open(os.path.join(HERE, "index.html"), "rb").read(), "text/html; charset=utf-8"
        elif path == "/api/stats":
            body, ct = json.dumps(api_stats()).encode(), "application/json"
        else:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def selftest():
    b = parse_batt("Now drawing from 'AC Power'\n -InternalBattery-0 (id=22085731)\t86%; charging; 0:48 remaining present: true")
    assert b == {"present": True, "pct": 86, "state": "charging", "charging": True, "ac": True, "remaining": "0:48"}, b
    b = parse_batt("Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t17%; discharging; 1:05 remaining present: true")
    assert b["pct"] == 17 and not b["charging"] and not b["ac"] and b["remaining"] == "1:05"
    assert parse_batt("Now drawing from 'AC Power'\n -InternalBattery-0\t100%; charged; 0:00 remaining")["charging"] is False
    assert parse_batt("Now drawing from 'AC Power'\n") == {"present": False}
    assert parse_batt("Now drawing from 'Battery Power'\n -InternalBattery-0\t50%; discharging; (no estimate) present: true")["remaining"] is None
    s = parse_swap("total = 4096.00M  used = 3277.00M  free = 819.00M  (encrypted)")
    assert s["pct"] == 80.0 and s["total"] == 4096.0, s
    assert parse_swap("total = 0.00M  used = 0.00M  free = 0.00M  (encrypted)")["pct"] == 0
    vm = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free:                                3940.\n"
          "Pages active:                            204648.\nPages inactive:                          200000.\n"
          "Pages speculative:                         60.\nPages wired down:                         98304.\n"
          "Pages occupied by compressor:             65536.\n")
    v = parse_vmstat(vm, 8 * 1024 ** 3)
    assert v["wired"] == 1536 and v["compressed"] == 1024 and v["active"] == round(204648 * 16384 / 1048576)
    assert v["free"] == round(4000 * 16384 / 1048576) and v["total"] == 8192, v
    top = "Processes: 1\n\nPID    MEM    %CPU TIME     COMMAND\n123    1536M+ 4.5  00:01.00 Google Chrome Helper\n9      512K   0.0  00:00.00 launchd\n"
    t = parse_top(top)
    assert t == [(123, "Google Chrome Helper", 1536.0), (9, "launchd", 0.5)], t
    assert app_of("/Applications/Google Chrome.app/Contents/Frameworks/X.app/Contents/MacOS/X", "x") == "Google Chrome"
    assert app_of("/usr/sbin/cfprefsd", "cfprefsd") == "cfprefsd"
    base = {"battery": {"present": True, "pct": 15, "charging": False}, "mem": {"pressure_level": 4, "pressure": "critical"},
            "swap": {"total": 100, "pct": 80}, "thermal": {"level": 2, "name": "serious"},
            "procs": [{"app": "Foo", "mem": 900, "cpu": 50}]}
    c = {x[0]: x for x in conditions(base, 3)}
    assert set(c) == {"battery", "mem_pressure", "swap", "thermal", "cpu"} and "Foo" in c["mem_pressure"][2]
    assert c["mem_pressure"][1] == 2 and c["battery"][1] == 1
    assert conditions(dict(base, battery={"present": False}, mem={"pressure_level": 1}, swap={"total": 0, "pct": 0},
                           thermal={"level": 0}), 2) == []
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--test-alert" in sys.argv:
        notify("Test alert: MacDash notifications are working")
    else:
        threading.Thread(target=monitor, daemon=True).start()
        srv = ThreadingHTTPServer((HOST, PORT), H)
        print("macdash listening on http://%s:%d" % (HOST, PORT), flush=True)
        srv.serve_forever()
