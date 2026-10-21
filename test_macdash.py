"""macdash tests: stdlib unittest, no network, no real notifications. Run: python3 -m unittest -v test_macdash"""
import http.client, json, os, tempfile, threading, time, unittest
from http.server import ThreadingHTTPServer
from unittest import mock

# never read the developer's real ~/.macdash/config.json
os.environ["MACDASH_CONFIG"] = os.path.join(tempfile.gettempdir(), "macdash-test-no-such-config.json")
import macdash as m

# ---- fixtures captured from a real M2 / macOS 26 machine (trimmed), plus edge-case variants ----
PMSET = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=22085731)\t95%; charging; 0:28 remaining present: true\n"
SWAP = "total = 10240.00M  used = 8703.06M  free = 1536.94M  (encrypted)\n"
VMSTAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                3532.
Pages active:                            176529.
Pages inactive:                          170683.
Pages speculative:                         4820.
Pages throttled:                              0.
Pages wired down:                        154584.
Pages purgeable:                              4.
Pages occupied by compressor:            301230.
"""
TOP = """Processes: 700 total, 3 running
Load Avg: 3.1, 3.5, 3.9

PID    MEM    %CPU TIME     COMMAND
17096  4121M  0.0  11:09.33 java
1963   1509M+ 1.2  11:05.08 dart
592    512K   0.0  00:00.00 ControlCe
"""
PS = """    1   0.8 25:35  14432 /sbin/launchd
21791   0.0 12:30  11568 /opt/homebrew/bin/limactl hostagent --pidfile /Users/me/.colima/_lima/colima/ha.pid
39310   2.5 1-02:37:05  3408 /Applications/Xcode.app/Contents/Developer/usr/bin/git clone -q --depth 1 https://github.com/x/y
garbage line
  77   abc 00:10 100 /bad/cpu column
"""
NETSTAT = """Name       Mtu   Network       Address            Ipkts Ierrs     Ibytes    Opkts Oerrs     Obytes  Coll
lo0        16384 <Link#1>                        603745     0  168658430   603745     0  168658430     0
en3        1500  <Link#6>    f6:ef:77:3d:77:86        0     0          0        0     0          0     0
en0        1500  <Link#11>   56:ea:45:f8:1e:16   928209     0 1075734381   330732     0  130744892     0
en0        1500  192.168.1     192.168.1.7       928209     - 1075734381   330732     -  130744892     -
en5*       1500  <Link#12>                          10     0       1000       20     0       2000     0
utun3      1380  <Link#13>                          10     0       9999       20     0       9999     0
"""
LSOF = """COMMAND     PID USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME
ControlCe   592 user   10u  IPv4 0x617c06968ef69888      0t0  TCP *:7000 (LISTEN)
ControlCe   592 user   11u  IPv6 0x1cfcc087598a5aad      0t0  TCP *:7000 (LISTEN)
postgres    721 user    8u  IPv4 0x93e021af5086e9fb      0t0  TCP 127.0.0.1:5432 (LISTEN)
Google\\x20Chrome  800 user  9u IPv6 0x1 0t0 TCP [::1]:9222 (LISTEN)
node        900 user   22u  IPv4 0x2      0t0  TCP 127.0.0.1:3000 (ESTABLISHED)
"""
SIMCTL = """== Devices ==
-- iOS 26.5 --
    iPhone 17 Pro (D3E73CA9-FD23-4F39-88F5-E96179C243A9) (Booted)
    iPad Air 11-inch (M4) (9073477B-91A8-4934-876F-D29B03065CD8) (Booted)
"""
SIMCTL_NONE = "== Devices ==\n-- iOS 26.5 --\n"
SYSPROF = """Power:

    Battery Information:

      Charge Information:
          State of Charge (%): 95
      Health Information:
          Cycle Count: 471
          Condition: Normal
          Maximum Capacity: 86%

    System Power Settings:
"""


def snap(**kw):
    """A healthy snapshot; override pieces per test."""
    s = {"ts": 1000.0, "battery": {"present": True, "pct": 80, "charging": False},
         "mem": {"pressure_level": 1, "pressure": "normal", "wired": 1000, "active": 2000, "compressed": 1000,
                 "inactive": 500, "free": 500, "total": 8192},
         "swap": {"total": 4096, "used": 100, "pct": 2.4}, "thermal": {"level": 0, "name": "nominal"},
         "cpu": {"pct": 10, "load": [1, 1, 1], "ncpu": 8}, "disk": {"total": 10 ** 12, "free": 500 * 1024 ** 3},
         "net": {"in_bps": 1, "out_bps": 2}, "procs": [{"app": "Foo", "mem": 900, "cpu": 50}], "leaks": []}
    s.update(kw)
    return s


def proc(pid, cmd, etime_s=0, mem=100):
    return {"pid": pid, "cmd": cmd, "etime_s": etime_s, "mem": mem, "name": os.path.basename(cmd.split(" ")[0])}


class Parsers(unittest.TestCase):
    def test_batt(self):
        self.assertEqual(m.parse_batt(PMSET), {"present": True, "pct": 95, "state": "charging", "charging": True,
                                               "ac": True, "remaining": "0:28"})
        b = m.parse_batt("Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t17%; discharging; 1:05 remaining present: true")
        self.assertEqual((b["pct"], b["charging"], b["ac"], b["remaining"]), (17, False, False, "1:05"))
        self.assertFalse(m.parse_batt("Now drawing from 'AC Power'\n -InternalBattery-0\t100%; charged; 0:00 remaining")["charging"])
        self.assertEqual(m.parse_batt("Now drawing from 'AC Power'\n"), {"present": False})  # desktop / CI runner
        self.assertIsNone(m.parse_batt("x\n -InternalBattery-0\t50%; discharging; (no estimate) present: true")["remaining"])

    def test_batt_health(self):
        self.assertEqual(m.parse_batt_health(SYSPROF), {"cycle_count": 471, "condition": "Normal", "max_capacity_pct": 86})
        self.assertIsNone(m.parse_batt_health("Power:\n\n    System Power Settings:\n"))
        self.assertIsNone(m.parse_batt_health(""))
        p = m.parse_batt_health("Cycle Count: 12\n")
        self.assertEqual((p["cycle_count"], p["condition"], p["max_capacity_pct"]), (12, None, None))
        self.assertEqual(m.parse_batt_health("Condition: Service Recommended\nMaximum Capacity: 79 %")["condition"], "Service Recommended")

    def test_swap(self):
        s = m.parse_swap(SWAP)
        self.assertEqual((s["total"], s["pct"]), (10240.0, 85.0))
        self.assertEqual(m.parse_swap("total = 0.00M  used = 0.00M  free = 0.00M")["pct"], 0)
        self.assertEqual(m.parse_swap("")["total"], 0)

    def test_vmstat(self):
        v = m.parse_vmstat(VMSTAT, 8 * 1024 ** 3)
        self.assertEqual(v["wired"], round(154584 * 16384 / 1048576))
        self.assertEqual(v["free"], round((3532 + 4820) * 16384 / 1048576))
        self.assertEqual((v["total"], v["compressed"] > 0), (8192, True))
        self.assertEqual(m.parse_vmstat("", 0)["wired"], 0)  # unparseable -> zeros, no crash

    def test_top(self):
        self.assertEqual(m.parse_top(TOP), [(17096, "java", 4121.0), (1963, "dart", 1509.0), (592, "ControlCe", 0.5)])
        self.assertEqual(m.parse_top(""), [])
        self.assertEqual(m.parse_top_mem("weird"), 0.0)

    def test_etime(self):
        self.assertEqual(m.parse_etime("25:35"), 1535)
        self.assertEqual(m.parse_etime("1-02:37:05"), 86400 + 2 * 3600 + 37 * 60 + 5)
        self.assertEqual(m.parse_etime("02:00:00"), 7200)
        self.assertEqual(m.parse_etime("junk"), 0)

    def test_ps_skips_bad_rows(self):
        ps = m.parse_ps(PS)
        self.assertEqual(sorted(ps), [1, 21791, 39310])  # garbage and non-numeric %cpu skipped, no ValueError
        self.assertEqual(ps[39310][1:3], ("1-02:37:05", 3.3))
        self.assertTrue(ps[1][3].endswith("launchd"))

    def test_netstat(self):
        # only Link rows of en<N>: skips lo0, utun, en5* (down), the IP-alias duplicate rows
        self.assertEqual(m.parse_netstat(NETSTAT), (1075734381, 130744892))
        self.assertEqual(m.parse_netstat(""), (0, 0))
        # Link row without a MAC address column
        self.assertEqual(m.parse_netstat("en1 1500 <Link#8> 10 0 500 20 0 700 0"), (500, 700))

    def test_net_rate(self):
        self.assertEqual(m.net_rate(None, (100, 100), 10), (0, 0))
        self.assertEqual(m.net_rate((0, 0, 0), (1000, 500), 10), (100, 50))
        self.assertEqual(m.net_rate((0, 5000, 5000), (100, 100), 10), (0, 0))  # counter reset
        self.assertEqual(m.net_rate((9.9, 0, 0), (100, 100), 10), (0, 0))  # too soon

    def test_lsof(self):
        r = m.parse_lsof(LSOF, {721: (0, "", 0, "/opt/homebrew/bin/postgres -D x")})
        self.assertEqual([(x["port"], x["pid"]) for x in r], [(5432, 721), (7000, 592), (9222, 800)])
        self.assertEqual(r[0]["cmd"], "/opt/homebrew/bin/postgres -D x")
        self.assertEqual(r[1]["name"], "ControlCe")
        self.assertEqual(r[2]["name"], "Google Chrome")  # lsof \x20 escape decoded
        self.assertEqual(sum(1 for x in r if x["port"] == 7000), 1)  # IPv4+IPv6 deduped
        self.assertEqual(m.parse_lsof("COMMAND PID\n"), [])

    def test_simctl(self):
        self.assertEqual(m.parse_simctl_booted(SIMCTL), ["iPhone 17 Pro", "iPad Air 11-inch (M4)"])
        self.assertEqual(m.parse_simctl_booted(SIMCTL_NONE), [])
        self.assertEqual(m.parse_simctl_booted(""), [])  # no simctl on CI

    def test_app_of(self):
        self.assertEqual(m.app_of("/Applications/Google Chrome.app/Contents/Frameworks/X.app/Contents/MacOS/X", "x"), "Google Chrome")
        self.assertEqual(m.app_of("/usr/sbin/cfprefsd", "cfprefsd"), "cfprefsd")


class Config(unittest.TestCase):
    def cfg(self, content):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(content)
        self.addCleanup(os.unlink, f.name)
        return m.load_config(f.name)

    def test_missing(self):
        self.assertEqual(m.load_config("/nonexistent/x.json"), m.DEFAULTS)

    def test_invalid(self):
        for bad in ("{not json", "[1,2]", '"str"', "", "null"):
            self.assertEqual(self.cfg(bad), m.DEFAULTS, bad)

    def test_partial_and_bad_values(self):
        c = self.cfg('{"swap_pct": 50, "notify": false, "cpu_pct": "high", "leak_mb": -5, "cpu_checks": 0, "extra": 1}')
        self.assertEqual((c["swap_pct"], c["notify"]), (50, False))
        self.assertEqual((c["cpu_pct"], c["leak_mb"]), (90, 500))  # wrong type / negative -> default
        self.assertEqual(c["cpu_checks"], 3)  # zero -> default
        self.assertEqual(self.cfg('{"cpu_pct": 1000}')["cpu_pct"], 90)  # out of range -> default
        self.assertNotIn("extra", c)
        self.assertEqual(self.cfg('{"notify": 1}')["notify"], True)  # ints are not bools

    def test_env_path(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write('{"disk_free_gb": 3}')
        self.addCleanup(os.unlink, f.name)
        with mock.patch.dict(os.environ, {"MACDASH_CONFIG": f.name}):
            self.assertEqual(m.load_config()["disk_free_gb"], 3)


class Conditions(unittest.TestCase):
    def types(self, s, streak=0, cfg=None):
        return {c[0]: c for c in m.conditions(s, streak, dict(m.DEFAULTS, **(cfg or {})))}

    def test_healthy(self):
        self.assertEqual(self.types(snap()), {})

    def test_everything_bad(self):
        s = snap(battery={"present": True, "pct": 15, "charging": False}, mem={"pressure_level": 4, "pressure": "critical"},
                 swap={"total": 100, "pct": 80}, thermal={"level": 2, "name": "serious"},
                 disk={"free": 5 * 1024 ** 3}, leaks=[{"pid": 1, "name": "Foo", "from_mb": 100, "to_mb": 900}])
        c = self.types(s, 3)
        self.assertEqual(set(c), {"battery", "mem_pressure", "swap", "thermal", "cpu", "disk_low", "leak"})
        self.assertEqual((c["mem_pressure"][1], c["battery"][1], c["disk_low"][1]), (2, 1, 1))
        self.assertIn("Foo", c["mem_pressure"][2])

    def test_battery_critical_is_own_type(self):
        c = self.types(snap(battery={"present": True, "pct": 9, "charging": False}))
        self.assertEqual(list(c), ["battery_critical"])
        self.assertEqual(c["battery_critical"][1], 2)
        self.assertEqual(self.types(snap(battery={"present": True, "pct": 9, "charging": True})), {})
        self.assertEqual(self.types(snap(battery={"present": False})), {})  # CI runner

    def test_config_thresholds(self):
        s = snap(battery={"present": True, "pct": 35, "charging": False}, swap={"total": 100, "pct": 55},
                 disk={"free": 40 * 1024 ** 3})
        self.assertEqual(self.types(s), {})
        c = self.types(s, 0, {"battery_low": 40, "swap_pct": 50, "disk_free_gb": 50})
        self.assertEqual(set(c), {"battery", "swap", "disk_low"})
        self.assertEqual(self.types(snap(battery={"present": True, "pct": 35, "charging": False}), 0,
                                    {"battery_low": 40, "battery_critical": 36})["battery_critical"][1], 2)
        self.assertEqual(self.types(snap(), 2, {"cpu_checks": 2}).keys(), {"cpu"})
        self.assertEqual(self.types(snap(), 2).keys(), set())

    def test_disk_low_severity(self):
        self.assertEqual(self.types(snap(disk={"free": 4 * 1024 ** 3}))["disk_low"][1], 2)

    def test_swap_zero_total_ignored(self):
        self.assertEqual(self.types(snap(swap={"total": 0, "pct": 0})), {})


class Alerting(unittest.TestCase):
    def setUp(self):
        m.ALERTS.clear(); m.LAST_FIRED.clear(); m.STATE["cpu_streak"] = 0
        self.cfg = mock.patch.object(m, "CFG", dict(m.DEFAULTS))
        self.cfg.start()
        self.notify = mock.patch.object(m, "notify").start()
        self.addCleanup(mock.patch.stopall)

    def swap(self, pct):
        return snap(swap={"total": 100, "pct": pct})

    def test_cooldown_and_flapping(self):
        m.check_once(self.swap(80), now=0)
        m.check_once(self.swap(80), now=30)
        self.assertEqual(self.notify.call_count, 1)
        m.check_once(snap(), now=60)           # condition clears for one check...
        m.check_once(self.swap(80), now=90)    # ...and comes back: still inside the 15 min cooldown
        self.assertEqual(self.notify.call_count, 1)
        m.check_once(self.swap(80), now=15 * 60 + 1)
        self.assertEqual(self.notify.call_count, 2)
        self.assertEqual(len(m.ALERTS), 2)

    def test_escalation_fires_inside_cooldown(self):
        m.check_once(self.swap(80), now=0)
        m.check_once(self.swap(95), now=30)
        self.assertEqual(self.notify.call_count, 2)
        m.check_once(snap(battery={"present": True, "pct": 15, "charging": False}), now=40)
        m.check_once(snap(battery={"present": True, "pct": 9, "charging": False}), now=50)
        self.assertEqual([a["type"] for a in m.ALERTS][-2:], ["battery", "battery_critical"])

    def test_cpu_streak(self):
        hot = snap(cpu={"pct": 95, "load": [1], "ncpu": 8})
        for i in range(2):
            m.check_once(hot, now=i)
        self.assertEqual((self.notify.call_count, m.STATE["cpu_streak"]), (0, 2))
        m.check_once(hot, now=2)
        self.assertEqual(self.notify.call_count, 1)
        m.check_once(snap(), now=3)
        self.assertEqual(m.STATE["cpu_streak"], 0)

    def test_notify_false_records_but_is_silent(self):
        m.CFG["notify"] = False
        m.check_once(self.swap(80), now=0)
        self.assertEqual((self.notify.call_count, len(m.ALERTS)), (0, 1))

    def test_alert_trim(self):
        for i in range(60):
            m.check_once(self.swap(80), now=i * 1000)
        self.assertEqual(len(m.ALERTS), 50)

    def test_notify_falls_back_to_osascript(self):
        mock.patch.stopall()
        with mock.patch.object(m, "TN", "/nonexistent/tn"), mock.patch.object(m, "sh") as sh:
            m.notify('say "hi"')
            self.assertEqual(sh.call_args[0][0][0], "osascript")
        with mock.patch.object(m, "TN", "/bin/sh"), mock.patch.object(m.subprocess, "run") as run, \
                mock.patch.object(m, "sh") as sh:  # exists + executable but exits non-zero
            run.return_value.returncode = 1
            m.notify("x")
            self.assertEqual(sh.call_args[0][0][0], "osascript")
            run.return_value.returncode = 0
            sh.reset_mock()
            m.notify("x")
            sh.assert_not_called()


class Suggestions(unittest.TestCase):
    def ids(self, plist, sims=(), leaks=()):
        return {x["id"]: x for x in m.suggest({p["pid"]: p for p in plist}, list(sims), list(leaks), dict(m.DEFAULTS))}

    def test_dev_servers(self):
        r = self.ids([proc(1, "node /tmp/app/node_modules/.bin/next-server", 10),
                      proc(2, "node /Users/me/proj/node_modules/.bin/vite", 10),            # fresh, in home: fine
                      proc(3, "node /Users/me/proj/node_modules/.bin/vite --port 3000", 90000),  # >24h
                      proc(4, "node /private/tmp/x/dev-server.js dev", 5)])
        self.assertEqual(r["dev-servers"]["pids"], [1, 3, 4])
        self.assertEqual(r["dev-servers"]["command"], "kill 1 3 4")
        self.assertNotIn("dev-servers", self.ids([proc(2, "node /Users/me/proj/node_modules/.bin/vite", 10)]))

    def test_simulators(self):
        r = self.ids([], ["iPhone 17"])
        self.assertEqual(r["ios-simulators"]["command"], "xcrun simctl shutdown all")
        self.assertNotIn("ios-simulators", self.ids([], []))

    def test_vms(self):
        r = self.ids([proc(1, "/opt/homebrew/bin/limactl hostagent --pidfile /Users/x/.colima/_lima/colima/ha.pid", 5, 3000),
                      proc(2, "/System/Library/Frameworks/Virtualization.framework/x/com.apple.Virtualization.VirtualMachine", 5, 100)])
        v = r["vms"]
        self.assertIn("Colima", v["title"])
        self.assertEqual((v["command"], v["severity"], v["pids"]), ("colima stop", "warn", [1, 2]))  # VM joins its owner
        r = self.ids([proc(1, "/opt/homebrew/bin/limactl hostagent --pidfile /x/colima/ha.pid", 5, 100),
                      proc(3, "qemu-system-aarch64-headless -avd Pixel", 5, 6000)])
        self.assertEqual((r["vms-colima"]["pids"], r["vms-androidemulator"]["command"]), ([1], "adb emu kill"))
        u = self.ids([proc(9, "/Users/x/bin/qemu-system-aarch64 -m 4G", 5, 500)])["vms"]
        self.assertIn("QEMU", u["title"]); self.assertEqual(u["severity"], "info")
        o = self.ids([proc(9, "/Applications/Foo.app/Contents/MacOS/com.apple.Virtualization.VirtualMachine", 5, 500)])["vms"]
        self.assertIn("owner not detected", o["detail"])
        self.assertTrue(o["command"].startswith("ps "))

    def test_gradle_flutter_git_dart(self):
        r = self.ids([proc(1, "/usr/bin/java -Xmx2g org.gradle.launcher.daemon.bootstrap.GradleDaemon 8.5"),
                      proc(2, "/x/flutter_tester --vm-service", 7200), proc(3, "/x/flutter_tester", 100),
                      proc(4, "/usr/bin/git pack-objects --stdout", 2000), proc(5, "/usr/bin/git push origin", 10),
                      proc(6, "/usr/bin/git status", 5000),
                      proc(7, "/x/dart language-server --protocol=lsp", 500, 900), proc(8, "/x/dart language-server", 100),
                      proc(9, "/x/dart language-server", 900)])
        self.assertEqual(r["gradle"]["command"], "pkill -f GradleDaemon")
        self.assertEqual(r["flutter-tester"]["pids"], [2])
        self.assertEqual(r["git-stuck"]["pids"], [4])
        self.assertEqual(r["dart-analysis"]["pids"], [7, 8, 9])
        self.assertEqual(r["dart-analysis"]["command"], "kill 8 9")  # keeps the biggest (pid 7)
        self.assertNotIn("dart-analysis", self.ids([proc(7, "/x/dart language-server")]))

    def test_protected_never_suggested(self):
        r = self.ids([proc(1, "/Users/x/.claude/local/claude node /tmp/x vite", 99999),
                      proc(2, "node /tmp/a/claude-code dev", 99999),
                      proc(3, "/System/Library/PrivateFrameworks/WindowServer webpack", 99999),
                      proc(4, "/sbin/launchd colima", 99999), proc(5, "/usr/libexec/x GradleDaemon java", 5)],
                     leaks=[{"pid": 1, "name": "claude", "from_mb": 1, "to_mb": 9999}])
        self.assertEqual(r, {})  # nothing, not even the leak entry for the protected pid

    def test_shape(self):
        for x in self.ids([proc(1, "node /tmp/x vite", 5)]).values():
            self.assertEqual(set(x), {"id", "severity", "title", "detail", "pids", "mem_mb", "command"})
            self.assertIn(x["severity"], ("info", "warn"))

    def test_leak_suggestion(self):
        leaks = [{"pid": 5, "name": "Foo", "from_mb": 200, "to_mb": 900}]
        r = self.ids([proc(5, "/x/Foo", 5, 900)], leaks=leaks)["leak-5"]
        self.assertEqual((r["pids"], r["command"], r["mem_mb"]), ([5], "kill 5", 900))


class Leaks(unittest.TestCase):
    NOW = 100000.0

    def run_leak(self, samples, cmd="/x/Foo", leak_mb=500):
        plist = {7: proc(7, cmd)}
        return m.detect_leaks({7: [(self.NOW - a, mb) for a, mb in samples]}, plist, self.NOW, leak_mb)

    def test_detects(self):
        r = self.run_leak([(1500, 400), (900, 800), (0, 1200)])
        self.assertEqual(r, [{"pid": 7, "name": "Foo", "from_mb": 400, "to_mb": 1200}])

    def test_needs_both_absolute_and_relative_growth(self):
        self.assertEqual(self.run_leak([(1500, 400), (0, 850)]), [])      # +450 MB < 500
        self.assertEqual(self.run_leak([(1500, 8000), (0, 8800)]), [])    # +800 MB but only +10%
        self.assertEqual(len(self.run_leak([(1500, 400), (0, 850)], leak_mb=400)), 1)

    def test_window_and_span(self):
        self.assertEqual(self.run_leak([(3000, 100), (1700, 600), (0, 650)]), [])  # old low sample is outside 30 min
        self.assertEqual(self.run_leak([(120, 100), (0, 1500)]), [])               # < 10 min of history
        self.assertEqual(self.run_leak([(0, 1500)]), [])
        self.assertEqual(self.run_leak([(1500, 1200), (0, 400)]), [])              # shrinking

    def test_dead_and_protected(self):
        self.assertEqual(m.detect_leaks({7: [(self.NOW - 1500, 100), (self.NOW, 1500)]}, {}, self.NOW, 500), [])
        self.assertEqual(self.run_leak([(1500, 100), (0, 1500)], cmd="/Users/x/claude"), [])

    def test_pidhist_update(self):
        h = {1: [(0, 10.0)], 2: [(0, 10.0)]}
        m.update_pidhist(h, {2: proc(2, "/x", mem=20), 3: proc(3, "/y", mem=0)}, 5000)
        self.assertNotIn(1, h)                       # dead pid dropped
        self.assertEqual(h[2], [(5000, 20)])         # sample older than 60 min trimmed, new added
        self.assertNotIn(3, h)                       # no footprint -> not tracked

    def test_build_plist_prefers_top_footprint(self):
        pl = m.build_plist({5: (1.0, "01:00", 12.0, "/x/Foo -a"), 6: (0, "2-00:00:00", 8.0, "/x/Bar")},
                           [{"pid": 5, "mem": 900}])
        self.assertEqual((pl[5]["mem"], pl[6]["mem"], pl[6]["etime_s"], pl[5]["name"]), (900, 8.0, 172800, "Foo"))


class History(unittest.TestCase):
    def test_point_shape(self):
        p = m.history_point(snap())
        self.assertEqual(set(p), {"t", "cpu", "mem_used_pct", "swap_pct", "pressure_level", "battery_pct", "net_in", "net_out"})
        self.assertEqual(p["mem_used_pct"], round(100 * 4000 / 8192, 1))
        self.assertIsNone(m.history_point(snap(battery={"present": False}))["battery_pct"])

    def test_ring_size(self):
        self.assertEqual(m.HIST.maxlen, 720)
        self.assertEqual(m.HIST_EVERY * m.HIST.maxlen >= 2 * 3600 * 0.9, True)


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), m.H)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def setUp(self):
        m.SNAP.clear(); m.READY.clear(); m.HIST.clear(); m.ALERTS.clear(); m.STATE["cpu_streak"] = 0
        m.HIST.append(m.history_point(snap()))

    def req(self, method="GET", path="/", host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest(method, path, skip_host=host is not None)
        if host is not None:
            c.putheader("Host", host)
        c.endheaders()
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body

    def test_host_check(self):
        self.assertEqual(self.req(host="evil.example.com")[0], 403)
        self.assertEqual(self.req(path="/api/stats", host="evil.example.com:7777")[0], 403)
        self.assertEqual(self.req(host="localhost:1234")[0], 200)

    def test_methods_and_paths(self):
        self.assertEqual(self.req("POST", "/api/stats")[0], 501)
        self.assertEqual(self.req("DELETE", "/")[0], 501)
        self.assertEqual(self.req(path="/nope")[0], 404)
        self.assertEqual(self.req(path="/api/kill")[0], 404)

    def test_index(self):
        st, body = self.req()
        self.assertEqual(st, 200)
        self.assertIn(b"MACDASH", body)

    def test_stats_warming_up_is_503(self):
        with mock.patch.object(m.READY, "wait", return_value=False):
            st, body = self.req(path="/api/stats")
        self.assertEqual(st, 503)
        self.assertIn("error", json.loads(body))

    def test_stats_keys(self):
        m.SNAP["s"] = snap(suggestions=[], ports=[], os="26.0", uptime=5, apps=[])
        m.READY.set()
        st, body = self.req(path="/api/stats?x=1")
        d = json.loads(body)
        self.assertEqual(st, 200)
        for k in ("ts", "battery", "thermal", "mem", "swap", "cpu", "disk", "net", "procs", "apps", "ports", "suggestions",
                  "leaks", "verdict", "alerts", "config", "error"):
            self.assertIn(k, d, k)
        self.assertEqual(d["config"], m.CFG)
        self.assertEqual(set(d["net"]), {"in_bps", "out_bps"})
        self.assertEqual(d["verdict"], {"level": "OK", "reasons": []})

    def test_verdict_uses_live_cpu_not_stale_streak(self):
        m.SNAP["s"] = snap(cpu={"pct": 20, "load": [1], "ncpu": 8})
        m.READY.set()
        m.STATE["cpu_streak"] = 5   # monitor last saw a hot CPU, but the live sample is calm
        self.assertEqual(json.loads(self.req(path="/api/stats")[1])["verdict"]["level"], "OK")
        m.SNAP["s"] = snap(cpu={"pct": 99, "load": [1], "ncpu": 8})
        self.assertEqual(json.loads(self.req(path="/api/stats")[1])["verdict"]["level"], "STRAINED")

    def test_history(self):
        st, body = self.req(path="/api/history")
        d = json.loads(body)
        self.assertEqual(st, 200)
        self.assertEqual(set(d), {"interval_s", "max_points", "points"})
        self.assertEqual(len(d["points"]), 1)
        self.assertIn("mem_used_pct", d["points"][0])
        self.assertNotIn("history", json.loads(self.req(path="/api/stats")[1]) if m.READY.is_set() else {})

    def test_touch_wakes_idle_refresher(self):
        m.ACTIVITY["t"] = 0.0
        m.WAKE.clear()
        self.req(path="/api/history")
        self.assertTrue(m.WAKE.is_set())
        m.WAKE.clear()


class Misc(unittest.TestCase):
    def test_port_env(self):
        import importlib
        with mock.patch.dict(os.environ, {"MACDASH_PORT": "8123"}):
            self.assertEqual(importlib.reload(m).PORT, 8123)
        with mock.patch.dict(os.environ, {"MACDASH_PORT": "abc"}):
            self.assertEqual(importlib.reload(m).PORT, 7777)
        importlib.reload(m)
        self.assertEqual(m.HOST, "127.0.0.1")

    def test_no_exec_surface(self):
        self.assertFalse(hasattr(m.H, "do_POST") or hasattr(m.H, "do_PUT") or hasattr(m.H, "do_DELETE"))


# ---------- storage review ----------
def mkfile(path, kb=8, age_days=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * 1024 * kb)
    if age_days:
        t = time.time() - age_days * 86400
        os.utime(path, (t, t))


class StorageCatalog(unittest.TestCase):
    def test_catalog_shape(self):
        ids = [c[0] for c in m.CATALOG]
        self.assertEqual(len(ids), len(set(ids)))
        for id, cat, label, paths, safety, cmd, note in m.CATALOG:
            self.assertIn(safety, m.SAFETY, id)
            self.assertTrue(paths and all(p.startswith(("~/", "/")) for p in paths), id)
            self.assertTrue(cmd and label and cat, id)

    def test_catalog_is_portable(self):
        for c in m.CATALOG:
            for p in c[3]:
                self.assertNotIn("/Users/", p)

    def test_catalog_covers_known_junk(self):
        paths = " ".join(p for c in m.CATALOG for p in c[3])
        for want in ("DerivedData", "Xcode/Archives", "DeviceSupport", "CoreSimulator/Devices", "CoreSimulator/Caches", ".android/avd", "system-images",
                     ".gradle/caches", ".gradle/wrapper", ".npm", "pnpm", "Yarn", "pip", ".pub-cache", "CocoaPods", "Homebrew", "~/.cache",
                     "Library/Caches", "Library/Logs", ".Trash", "MobileSync/Backup", ".colima", ".ollama"):
            self.assertIn(want, paths)

    def test_safety_classification(self):
        by = {c[0]: c for c in m.CATALOG}
        for id in ("xcode-archives", "android-avd", "ios-backups", "colima", "ollama", "coresim-devices"):
            self.assertEqual(by[id][4], "review", id)
        for id in ("xcode-derived", "npm", "gradle-caches", "trash", "lib-caches"):
            self.assertEqual(by[id][4], "safe", id)
        self.assertEqual(by["photos"][4], "keep")
        for c in m.CATALOG:  # official tools first; rm -rf only on pure caches (never review/keep)
            if c[4] != "safe":
                self.assertNotIn("rm -rf", c[5], c[0])
        self.assertEqual(by["npm"][5], "npm cache clean --force")

    def test_artifact_rules(self):
        for k, (need, safety, cmd, note) in m.ARTIFACTS.items():
            self.assertIn(safety, m.SAFETY)
            self.assertTrue("{p}" in cmd or "{d}" in cmd, k)
        self.assertEqual(m.ARTIFACTS[".venv"][1], "review")
        self.assertEqual(m.ARTIFACTS["node_modules"][1], "safe")

    def test_roots_config(self):
        self.assertEqual(m.DEFAULTS["storage_roots"], ["~/engineering", "~/Projects", "~/Developer", "~/code", "~/src"])
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"storage_roots": ["~/x", "/y"]}, f)
        self.assertEqual(m.load_config(f.name)["storage_roots"], ["~/x", "/y"])
        for bad in ("nope", [1, 2], None):
            with open(f.name, "w") as g:
                json.dump({"storage_roots": bad}, g)
            self.assertEqual(m.load_config(f.name)["storage_roots"], m.DEFAULTS["storage_roots"])
        os.unlink(f.name)


class StorageScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.realpath(self.tmp.name)
        h = self.home
        mkfile(h + "/Library/Caches/foo/a.bin", 64)
        mkfile(h + "/Library/Caches/Homebrew/b.bin", 32)
        mkfile(h + "/.npm/_cacache/c", 16)
        mkfile(h + "/Library/Developer/Xcode/DerivedData/App/d", 16)
        mkfile(h + "/.android/avd/Pixel.avd/userdata.img", 16)
        mkfile(h + "/Downloads/old.dmg", 100, age_days=30)
        mkfile(h + "/Downloads/fresh.dmg", 100)
        mkfile(h + "/Downloads/notes.txt", 100, age_days=30)
        p = h + "/engineering/app"
        mkfile(p + "/package.json", 1)
        mkfile(p + "/node_modules/pkg/index.js", 64)
        mkfile(p + "/pubspec.yaml", 1)
        mkfile(p + "/build/out.bin", 64)
        mkfile(p + "/.git/node_modules/x", 64)           # inside .git: never reported
        mkfile(p + "/.claude/worktrees/wt1/src/a.dart", 64)
        mkfile(p + "/.claude/worktrees/wt1/node_modules/z", 64)  # counted in the worktree, not listed twice
        os.symlink(h + "/engineering", p + "/loop")      # symlink loop
        os.symlink(p + "/node_modules", h + "/engineering/linked_nm")
        os.makedirs(h + "/engineering/secret/node_modules")
        os.chmod(h + "/engineering/secret", 0)           # unreadable
        mkfile(h + "/engineering/a/b/c/d/e/node_modules/deep", 8)   # depth 6: found
        mkfile(h + "/engineering/a/b/c/d/e/f/node_modules/toodeep", 8)  # depth 7: beyond the cap
        pats = [mock.patch.object(m, "PROJ_MIN", 0), mock.patch.object(m, "disk_info", return_value={"total": 100, "used": 60, "free": 40, "purgeable": 5}),
                mock.patch.object(m, "swap_files_bytes", return_value=2 * 1024 ** 3)]
        for pt in pats:
            pt.start()
            self.addCleanup(pt.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: os.chmod(h + "/engineering/secret", 0o755))  # runs first: tmp cleanup needs access

    def scan(self, **kw):
        return m.scan_storage(home=self.home, roots=["~/engineering", "~/nope"], **kw)

    def test_payload_shape(self):
        r = self.scan()
        for k in ("scanned_at", "duration_s", "running", "disk", "swap_files_gb", "items", "safe_total_bytes", "review_total_bytes", "top_home"):
            self.assertIn(k, r)
        self.assertEqual(r["swap_files_gb"], 2.0)
        self.assertFalse(r["running"])
        for i in r["items"]:
            self.assertEqual(set(i), {"id", "category", "label", "path", "bytes", "safety", "command", "note"})
            self.assertIn(i["safety"], m.SAFETY)
            self.assertGreater(i["bytes"], 0)
        self.assertEqual([i["bytes"] for i in r["items"]], sorted((i["bytes"] for i in r["items"]), reverse=True))
        self.assertEqual(r["safe_total_bytes"], sum(i["bytes"] for i in r["items"] if i["safety"] == "safe"))
        self.assertEqual(r["review_total_bytes"], sum(i["bytes"] for i in r["items"] if i["safety"] == "review"))
        self.assertTrue(r["top_home"] and all(set(t) == {"path", "bytes"} for t in r["top_home"]))
        self.assertNotIn(self.home, [t["path"] for t in r["top_home"]])

    def test_finds_catalog_items(self):
        by = {i["id"]: i for i in self.scan()["items"]}
        for id in ("lib-caches", "homebrew", "npm", "xcode-derived", "android-avd", "downloads-installers", "vm-swap"):
            self.assertIn(id, by)
        self.assertEqual(by["npm"]["safety"], "safe")
        self.assertEqual(by["android-avd"]["safety"], "review")
        self.assertEqual(by["downloads-installers"]["safety"], "review")
        self.assertEqual(by["vm-swap"]["safety"], "keep")
        self.assertNotIn("xcode-archives", by)   # missing path skipped

    def test_nested_not_double_counted(self):
        by = {i["id"]: i for i in self.scan()["items"]}
        self.assertEqual(by["lib-caches"]["path"], self.home + "/Library/Caches")
        self.assertIn("Excludes", by["lib-caches"]["note"])
        self.assertLess(by["lib-caches"]["bytes"], m.du_bytes(self.home + "/Library/Caches"))

    def test_downloads_installers_only_old(self):
        self.assertEqual(m.downloads_installers(self.home), (100 * 1024, 1))

    def test_project_sweep(self):
        items = [i for i in self.scan()["items"] if i["id"].startswith("proj-")]
        paths = {i["path"]: i for i in items}
        p = self.home + "/engineering/app"
        self.assertEqual(paths[p + "/node_modules"]["safety"], "safe")
        self.assertIn("rm -rf", paths[p + "/node_modules"]["command"])
        self.assertIn("flutter clean", paths[p + "/build"]["command"])
        wt = paths[p + "/.claude/worktrees/wt1"]
        self.assertEqual((wt["safety"], wt["category"]), ("review", "worktree"))
        self.assertIn("git -C", wt["command"])
        self.assertIn("worktree remove", wt["command"])
        for bad in (p + "/.git/node_modules", p + "/loop", self.home + "/engineering/linked_nm", p + "/.claude/worktrees/wt1/node_modules",
                    self.home + "/engineering/secret/node_modules", self.home + "/engineering/a/b/c/d/e/f/node_modules"):
            self.assertNotIn(bad, paths)
        self.assertIn(self.home + "/engineering/a/b/c/d/e/node_modules", paths)

    def test_symlink_loop_terminates(self):
        t = time.time()
        self.scan()
        self.assertLess(time.time() - t, 30)

    def test_catalog_symlink_not_followed(self):
        real = self.home + "/real_npm"
        mkfile(real + "/big", 512)
        os.rename(self.home + "/.npm", self.home + "/.npm_old")
        os.symlink(real, self.home + "/.npm")
        self.assertNotIn("npm", {i["id"] for i in self.scan()["items"]})

    def test_timeout_skips_one_path(self):
        real = m.du_bytes
        def du(path):
            if path.endswith("DerivedData"):
                raise m.subprocess.TimeoutExpired("du", 1)
            return real(path)
        r = self.scan(du=du)
        self.assertIn(self.home + "/Library/Developer/Xcode/DerivedData", r["skipped"])
        ids = {i["id"] for i in r["items"]}
        self.assertNotIn("xcode-derived", ids)
        self.assertIn("npm", ids)

    def test_budget_exhausted_skips_rest(self):
        with mock.patch.object(m, "STO_BUDGET", -1):
            r = self.scan()
        self.assertTrue(r["skipped"])
        self.assertEqual([i["id"] for i in r["items"] if i["id"] not in ("downloads-installers", "vm-swap")], [])

    def test_du_bytes(self):
        self.assertIsNone(m.du_bytes(self.home + "/does-not-exist"))
        self.assertGreaterEqual(m.du_bytes(self.home + "/Library/Caches/foo"), 64 * 1024)
        with mock.patch.object(m.subprocess, "run", side_effect=m.subprocess.TimeoutExpired("du", 1)):
            with self.assertRaises(m.subprocess.TimeoutExpired):
                m.du_bytes("/x")
        with mock.patch.object(m.subprocess, "run") as run:
            run.return_value.stdout = "12\t/x\n"
            self.assertEqual(m.du_bytes("/x"), 12 * 1024)
            self.assertEqual(run.call_args[0][0][:3], ["nice", "-n", "19"])  # low priority, one filesystem
            self.assertIn("-skx", run.call_args[0][0])

    def test_publish_gets_items_before_home_walk(self):
        seen = []
        r = self.scan(publish=lambda p: seen.append(dict(p)))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["top_home"], [])
        self.assertEqual(seen[0]["items"], r["items"])
        self.assertTrue(r["top_home"])

    def test_scan_never_deletes(self):
        walk = lambda: sorted(os.path.join(d, f) for d, _, fs in os.walk(self.home) for f in fs)
        before = walk()
        self.scan()
        self.assertEqual(before, walk())

    def test_net_nested(self):
        a = {"path": "/x", "bytes": 100, "note": ""}
        b = {"path": "/x/y", "bytes": 30, "note": ""}
        c = {"path": "/xy", "bytes": 5, "note": ""}
        m.net_nested([a, b, c])
        self.assertEqual((a["bytes"], b["bytes"], c["bytes"]), (70, 30, 5))


class StorageApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), m.H)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def setUp(self):
        m.STORAGE.update(result=None, running=False, finished=0.0)
        m.STO_WAKE.clear()
        m.SNAP.clear(); m.READY.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def req(self, path, method="GET"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, json.loads(body) if body.startswith(b"{") else body

    def result(self, safe_gb=0.0):
        return {"scanned_at": 1000.0, "duration_s": 41.2, "running": False, "disk": {"total": 10, "used": 6, "free": 4, "purgeable": None},
                "swap_files_gb": 1.0, "safe_total_bytes": int(safe_gb * 1024 ** 3), "review_total_bytes": 7, "top_home": [], "skipped": [],
                "items": [{"id": "npm", "category": "dev-cache", "label": "npm cache", "path": "/h/.npm", "bytes": int(safe_gb * 1024 ** 3),
                           "safety": "safe", "command": "npm cache clean --force", "note": ""},
                          {"id": "vm-swap", "category": "system", "label": "VM", "path": "/vm", "bytes": 9, "safety": "keep", "command": "-", "note": ""}]}

    def test_storage_before_first_scan(self):
        st, d = self.req("/api/storage")
        self.assertEqual(st, 200)
        self.assertIsNone(d["scanned_at"])
        self.assertEqual(d["items"], [])
        self.assertIn("running", d)

    def test_storage_shape_and_running_flag(self):
        m.STORAGE.update(result=self.result(1), running=True)
        st, d = self.req("/api/storage?x=1")
        self.assertEqual(st, 200)
        for k in ("scanned_at", "duration_s", "running", "disk", "swap_files_gb", "items", "safe_total_bytes", "review_total_bytes", "top_home"):
            self.assertIn(k, d)
        self.assertTrue(d["running"])
        self.assertEqual(d["items"][0]["id"], "npm")

    def test_rescan_shape_and_throttle(self):
        st, d = self.req("/api/storage/rescan")
        self.assertEqual((st, d), (200, {"scheduled": True}))
        self.assertTrue(m.STO_WAKE.is_set())
        self.assertTrue(m.STORAGE["running"])
        self.assertEqual(self.req("/api/storage/rescan")[1], {"scheduled": False})    # already running
        m.STORAGE.update(running=False, finished=time.time())
        self.assertEqual(self.req("/api/storage/rescan")[1], {"scheduled": False})    # finished under 60s ago
        m.STORAGE["finished"] = time.time() - 61
        self.assertEqual(self.req("/api/storage/rescan")[1], {"scheduled": True})

    def test_rescan_throttle_unit(self):
        m.STORAGE.update(running=False, finished=100.0)
        self.assertEqual(m.storage_rescan(now=160.0), {"scheduled": False})
        self.assertEqual(m.storage_rescan(now=161.0), {"scheduled": True})

    def test_no_endpoint_deletes_anything(self):
        victim = os.path.join(self.tmp.name, "keepme")
        mkfile(victim)
        res = self.result(1)
        res["items"][0].update(path=victim, command="rm -rf " + victim)
        m.STORAGE.update(result=res)
        m.SNAP["s"] = snap(suggestions=[], ports=[], os="26.0", uptime=5, apps=[]); m.READY.set()
        for p in ("/api/storage", "/api/storage/rescan", "/api/stats"):
            self.req(p)
        for p in ("/api/storage/delete", "/api/storage/clean", "/api/storage/npm", "/api/delete", "/api/clean"):
            self.assertEqual(self.req(p)[0], 404, p)
        for meth in ("POST", "DELETE", "PUT"):
            self.assertEqual(self.req("/api/storage", meth)[0], 501)
        self.assertTrue(os.path.exists(victim))

    def test_source_has_no_delete_calls(self):
        with open(m.__file__) as f:
            src = f.read()
        for bad in ("os.remove", "os.unlink", "shutil.rmtree", "os.rmdir", "send2trash", "os.system", "shell=True"):
            self.assertNotIn(bad, src)

    def test_stats_has_storage_summary(self):
        m.SNAP["s"] = snap(suggestions=[], ports=[], os="26.0", uptime=5, apps=[]); m.READY.set()
        _, d = self.req("/api/stats")
        self.assertEqual(set(d["storage"]), {"scanned_at", "running", "safe_total_bytes", "review_total_bytes", "top"})
        self.assertIsNone(d["storage"]["scanned_at"])
        self.assertNotIn("storage-reclaimable", [x["id"] for x in d["suggestions"]])

    def test_stats_top3_excludes_keep_and_suggests_over_5gb(self):
        res = self.result(6)
        res["items"] = [dict(res["items"][0], id="i%d" % n, bytes=(9 - n) * 1024 ** 3) for n in range(5)] + [res["items"][1]]
        m.STORAGE.update(result=res)
        m.SNAP["s"] = snap(suggestions=[], ports=[], os="26.0", uptime=5, apps=[]); m.READY.set()
        _, d = self.req("/api/stats")
        self.assertEqual(len(d["storage"]["top"]), 3)
        self.assertTrue(all(t["safety"] != "keep" for t in d["storage"]["top"]))
        s = [x for x in d["suggestions"] if x["id"] == "storage-reclaimable"]
        self.assertEqual(len(s), 1)
        self.assertIn("~6 GB", s[0]["title"])
        m.STORAGE.update(result=self.result(5))   # exactly 5 GB is not "more than 5"
        self.assertNotIn("storage-reclaimable", [x["id"] for x in self.req("/api/stats")[1]["suggestions"]])

    def test_disk_low_mentions_top_safe_item(self):
        s = snap(disk={"free": 4 * 1024 ** 3})
        txt = {t: x for t, _, x in m.conditions(s, 0)}["disk_low"]
        self.assertEqual(txt, "Disk low: 4.0 GB free")
        m.STORAGE.update(result=self.result(3))
        txt = {t: x for t, _, x in m.conditions(s, 0)}["disk_low"]
        self.assertIn("Biggest safe clean: npm cache (3.0 GB): npm cache clean --force", txt)

    def test_loop_scans_in_own_thread_and_records(self):
        with mock.patch.object(m, "scan_storage", return_value=self.result(1)), mock.patch.object(m, "STO_FIRST", 0), \
                mock.patch.object(m, "STO_EVERY", 3600):
            threading.Thread(target=m.storage_loop, daemon=True).start()
            for _ in range(100):
                if m.STORAGE["result"] and not m.STORAGE["running"]:
                    break
                time.sleep(0.05)
        self.assertEqual(m.STORAGE["result"]["duration_s"], 41.2)
        self.assertFalse(m.STORAGE["running"])
        self.assertGreater(m.STORAGE["finished"], 0)


if __name__ == "__main__":
    unittest.main()
