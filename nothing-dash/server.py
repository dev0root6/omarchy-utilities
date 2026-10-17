#!/usr/bin/env python3
"""Minimal system dashboard (btop alternative) served on 127.0.0.1.

Reads everything straight from /proc and /sys, plus systemctl / nbfc for the
boot list and fan. Standard library only, so it runs with /usr/bin/python3.

    GET  /                 the page
    GET  /api/stats        live stats (cpu, memory, temps, fan, battery, net, disk, processes)
    GET  /api/boot         things that start at boot (cached per call, unit stats are live)
    POST /api/kill         {"pid": N}  SIGTERM, only for the user's own processes
"""
import json
import os
import pwd
import re
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST, PORT = "127.0.0.1", 47800
HERE = Path(__file__).resolve().parent
UID = os.getuid()
CLK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")
NCPU = os.cpu_count() or 1

lock = threading.Lock()
prev = {"t": 0.0, "cpu": None, "procs": {}, "net": None, "disk": None}
users = {}


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


def run(*cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def username(uid):
    if uid not in users:
        try:
            users[uid] = pwd.getpwuid(uid).pw_name
        except KeyError:
            users[uid] = str(uid)
    return users[uid]


# ---------------------------------------------------------------- live stats

def cpu_times():
    out = []
    for line in read("/proc/stat").splitlines():
        if not line.startswith("cpu"):
            break
        v = list(map(int, line.split()[1:]))
        idle = v[3] + v[4]
        out.append((sum(v[:8]), idle))
    return out


def cpu_usage(now, last):
    if not last:
        return [0.0] * len(now)
    res = []
    for (t1, i1), (t0, i0) in zip(now, last):
        dt = t1 - t0
        res.append(round(100 * (1 - (i1 - i0) / dt), 1) if dt > 0 else 0.0)
    return res


def memory():
    m = {}
    for line in read("/proc/meminfo").splitlines():
        k, v = line.split(":", 1)
        m[k] = int(v.split()[0]) * 1024
    total, avail = m["MemTotal"], m["MemAvailable"]
    return {
        "total": total, "used": total - avail, "cached": m.get("Cached", 0) + m.get("Buffers", 0),
        "swap_total": m.get("SwapTotal", 0), "swap_used": m.get("SwapTotal", 0) - m.get("SwapFree", 0),
    }


def temps():
    out = {}
    for hw in Path("/sys/class/hwmon").iterdir():
        name = read(hw / "name").strip()
        files = sorted(hw.glob("temp*_input"))
        if not files:
            continue
        val = max(int(read(f, "0") or 0) for f in files) / 1000
        if name in out:
            val = max(val, out[name])
        out[name] = round(val, 1)
    return out


def fan():
    txt = run("nbfc", "status", "-f", "0", timeout=2)
    f = {}
    for line in txt.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            f[k.strip()] = v.strip()
    if not f:
        return None
    mode = read(Path.home() / ".local/state/fan-speed", "auto").strip() or "auto"
    return {
        "speed": float(f.get("Current Fan Speed", 0)),
        "target": float(f.get("Target Fan Speed", 0)),
        "auto": f.get("Auto Control Enabled") == "true",
        "critical": f.get("Critical Mode Enabled") == "true",
        "mode": mode,
    }


def battery():
    for b in Path("/sys/class/power_supply").glob("BAT*"):
        cap = read(b / "capacity").strip()
        if not cap:
            continue
        uw = int(read(b / "power_now", "0").strip() or 0)
        if not uw:
            uw = int(read(b / "current_now", "0").strip() or 0) * int(read(b / "voltage_now", "0").strip() or 0) // 10**6
        return {"pct": int(cap), "status": read(b / "status").strip(), "watts": round(uw / 1e6, 1)}
    return None


def net_bytes():
    rx = tx = 0
    for line in read("/proc/net/dev").splitlines()[2:]:
        name, data = line.split(":", 1)
        if name.strip() == "lo":
            continue
        v = data.split()
        rx += int(v[0])
        tx += int(v[8])
    return rx, tx


def disk_bytes():
    rd = wr = 0
    for line in read("/proc/diskstats").splitlines():
        v = line.split()
        if re.fullmatch(r"nvme\d+n\d+|sd[a-z]+|vd[a-z]+|mmcblk\d+", v[2]):
            rd += int(v[5]) * 512
            wr += int(v[9]) * 512
    return rd, wr


def disk_usage():
    st = os.statvfs("/")
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    return {"total": total, "used": total - free}


def processes(dt, last):
    procs, seen = [], {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        pid = int(d)
        stat = read(f"/proc/{pid}/stat")
        if not stat:
            continue
        r = stat.rfind(")")
        name = stat[stat.find("(") + 1:r]
        f = stat[r + 2:].split()
        ticks = int(f[11]) + int(f[12])
        start = int(f[19])
        rss = int(f[21]) * PAGE
        seen[pid] = (ticks, start)
        p = last.get(pid)
        cpu = 0.0
        if p and p[1] == start and dt > 0:
            cpu = round(100 * (ticks - p[0]) / CLK / dt, 1)
        try:
            uid = os.stat(f"/proc/{pid}").st_uid
        except OSError:
            continue
        cmd = read(f"/proc/{pid}/cmdline").replace("\0", " ").strip()
        procs.append({
            "pid": pid, "name": name, "cmd": cmd[:300] or f"[{name}]", "cpu": cpu, "mem": rss,
            "state": f[0], "threads": int(f[17]), "user": username(uid), "own": uid == UID,
        })
    return procs, seen


def stats():
    with lock:
        now = time.monotonic()
        dt = now - prev["t"] if prev["t"] else 0
        cpu_now = cpu_times()
        cpu = cpu_usage(cpu_now, prev["cpu"])
        procs, seen = processes(dt, prev["procs"])
        net, dsk = net_bytes(), disk_bytes()
        rate = lambda a, b: [round((x - y) / dt) if dt else 0 for x, y in zip(a, b)] if b else [0, 0]
        net_rate, disk_rate = rate(net, prev["net"]), rate(dsk, prev["disk"])
        prev.update(t=now, cpu=cpu_now, procs=seen, net=net, disk=dsk)

    freq = [int(read(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq", "0") or 0) // 1000 for i in range(NCPU)]
    procs.sort(key=lambda p: (p["cpu"], p["mem"]), reverse=True)
    return {
        "time": time.time(),
        "host": socket.gethostname(),
        "kernel": os.uname().release,
        "uptime": float(read("/proc/uptime", "0").split()[0]),
        "load": os.getloadavg(),
        "cpu": {"total": cpu[0], "cores": cpu[1:], "freq": max(freq) if freq else 0},
        "mem": memory(),
        "temps": temps(),
        "fan": fan(),
        "battery": battery(),
        "net": {"rx": net_rate[0], "tx": net_rate[1]},
        "disk": {**disk_usage(), "read": disk_rate[0], "write": disk_rate[1]},
        "tasks": len(procs),
        "procs": procs[:60],
    }


# ---------------------------------------------------------------- boot

boot_cache = {"blame": None, "summary": None}


def boot_timing():
    # systemd-analyze takes a few seconds and never changes until reboot
    if boot_cache["blame"] is None:
        blame = {}
        for line in run("systemd-analyze", "blame", "--no-pager", timeout=15).splitlines():
            parts = line.split()
            if len(parts) >= 2:
                blame[parts[-1]] = " ".join(parts[:-1])
        boot_cache["blame"] = blame
        boot_cache["summary"] = run("systemd-analyze", timeout=15).splitlines()[:1]
    return boot_cache["blame"], boot_cache["summary"]


def to_seconds(s):
    total = 0.0
    for num, unit in re.findall(r"([\d.]+)(min|ms|us|s)", s or ""):
        total += float(num) * {"min": 60, "s": 1, "ms": 1e-3, "us": 1e-6}[unit]
    return round(total, 3)


def unit_list(user):
    base = ["systemctl"] + (["--user"] if user else [])
    names = [l.split()[0] for l in run(*base, "list-unit-files", "--state=enabled", "--no-legend", "--no-pager").splitlines()
             if l.split() and not l.split()[0].endswith("@.service")]
    if not names:
        return []
    props = "Id,Description,ActiveState,SubState,MemoryCurrent,CPUUsageNSec,MainPID,ActiveEnterTimestampMonotonic"
    txt = run(*base, "show", "-p", props, "--", *names, timeout=10)
    out = []
    for block in txt.strip().split("\n\n"):
        u = dict(l.split("=", 1) for l in block.splitlines() if "=" in l)
        if not u.get("Id"):
            continue
        mem = u.get("MemoryCurrent", "")
        cpu = u.get("CPUUsageNSec", "")
        out.append({
            "id": u["Id"], "desc": u.get("Description", ""),
            "active": u.get("ActiveState", ""), "sub": u.get("SubState", ""),
            "mem": int(mem) if mem.isdigit() else None,
            "cpu": int(cpu) / 1e9 if cpu.isdigit() else None,
            "pid": int(u.get("MainPID") or 0),
            "scope": "user" if user else "system",
        })
    return out


def hypr_autostart():
    out = []
    files = [Path.home() / ".local/share/omarchy/default/hypr/autostart.conf", Path.home() / ".config/hypr/autostart.conf"]
    running = {p.split(None, 1)[1] if " " in p else p for p in run("ps", "-eo", "comm=").split()}
    for f in files:
        for line in read(f).splitlines():
            m = re.match(r"\s*exec-once\s*=\s*(.+)", line)
            if not m:
                continue
            cmd = m.group(1).strip()
            words = [w for w in cmd.replace("uwsm-app --", "").split() if not w.startswith("-")]
            prog = os.path.basename(words[0]) if words else cmd
            out.append({
                "id": prog, "desc": cmd, "scope": "session",
                "active": "active" if prog[:15] in running else "inactive", "sub": "",
                "mem": None, "cpu": None, "pid": 0,
            })
    return out


def boot():
    blame, summary = boot_timing()
    units = unit_list(False) + unit_list(True)
    for u in units:
        u["boot"] = to_seconds(blame.get(u["id"]))
    items = units + hypr_autostart()
    for i in items:
        i.setdefault("boot", None)
    return {"summary": summary[0] if summary else "", "items": items}


# ---------------------------------------------------------------- actions

def kill(pid):
    pid = int(pid)
    if os.stat(f"/proc/{pid}").st_uid != UID or pid == os.getpid():
        return {"ok": False, "error": "not your process"}
    os.kill(pid, signal.SIGTERM)
    return {"ok": True}


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def local(self):
        # rejects DNS-rebinding requests that arrive under a foreign Host name
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def do_GET(self):
        if not self.local():
            return self.send(403, {"error": "forbidden"})
        if self.path in ("/", "/index.html"):
            return self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/stats":
            return self.send(200, stats())
        if self.path == "/api/boot":
            return self.send(200, boot())
        self.send(404, {"error": "not found"})

    def do_POST(self):
        # the custom header forces a CORS preflight, which this server never approves,
        # so other websites open in the browser cannot trigger these actions
        if not self.local() or self.headers.get("X-Dash") != "1":
            return self.send(403, {"error": "forbidden"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.path == "/api/kill":
                return self.send(200, kill(body["pid"]))
        except (KeyError, ValueError, OSError) as e:
            return self.send(400, {"ok": False, "error": str(e)})
        self.send(404, {"error": "not found"})


if __name__ == "__main__":
    threading.Thread(target=boot_timing, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
