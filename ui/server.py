#!/usr/bin/env python3
"""Demo UI: a one-button traffic generator with live per-class results.

  sudo python3 ui/server.py          # then open http://127.0.0.1:8000

Needs the demo already running (controller + `topology.py demo`). Start/Stop
launches steady chat traffic from the client hosts (ui/traffic.py) plus,
optionally, a bulk TCP sender that fills the 10 Mbps trunk. The page shows, per
user, the class and queue the controller chose and the measured round-trip
time, and per class the trunk throughput of its queue.

Root is needed to enter the Mininet hosts (mnexec) and to read tc counters.
Only start/stop and the mode switch are exposed, not arbitrary commands.
"""
import sys
sys.dont_write_bytecode = True          # do not leave root-owned .pyc files in the project

import argparse
import fnmatch
import glob
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from experiments import common  # noqa: E402
from sdn.policy import Policy, QUEUES  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STATS = "/tmp/chat_ui"
TRAFFIC = os.path.join(HERE, "traffic.py")
BULK = os.path.join(ROOT, "experiments", "bulk.py")
SINK_IP = "10.0.0.2"
TRUNK = "s1-eth6"                       # client -> server direction, where bulk also flows
WINDOW = 5.0                            # seconds of RTT samples behind each number
STALL = 3.0                             # no ack for this long = stalled

# Users per client host. "user_ann:announcements" is a regular user who joins a
# HIGH room, so the room policy (not the name) is what raises its priority.
PLAN = {
    "c1": ["vip_1", "user_1", "guest_1"],
    "c2": ["vip_2", "user_2", "guest_2"],
    "c3": ["user_3", "user_ann:announcements"],
}


def pct(values, p):
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def host_pid(host):
    try:
        return subprocess.check_output(["pgrep", "-f", "mininet:%s$" % host]).split()[0].decode()
    except (subprocess.CalledProcessError, IndexError):
        raise RuntimeError("Mininet host %s not found; start the demo first (topology.py demo)" % host)


class Demo:
    def __init__(self):
        self.lock = threading.Lock()
        self.procs = []                 # (host, Popen, log path)
        self.running = False
        self.bulk = False
        self.started = None
        self.rates = {1: 0.0, 2: 0.0, 3: 0.0}
        self._prev = None
        threading.Thread(target=self._sample_queues, daemon=True).start()

    # ---------------------------------------------------------------- traffic

    def _launch(self, host, cmd, name):
        log = os.path.join(STATS, "%s.log" % name)
        logf = open(log, "w")
        p = subprocess.Popen(["mnexec", "-a", host_pid(host)] + cmd, stdout=logf,
                             stderr=subprocess.STDOUT)
        self.procs.append((name, p, log))

    def start(self, bulk):
        with self.lock:
            if self.running:
                return
            for host in list(PLAN) + (["sink", "bulk"] if bulk else []):
                host_pid(host)          # fail early, before launching anything
            shutil.rmtree(STATS, ignore_errors=True)
            os.makedirs(STATS)
            os.chmod(STATS, 0o755)
            for host, users in PLAN.items():
                self._launch(host, ["python3", "-B", TRAFFIC, "--name", host,
                                    "--out", os.path.join(STATS, host + ".json"),
                                    "--users", ",".join(users)], host)
            if bulk:
                self._launch("sink", ["python3", "-B", BULK, "sink"], "sink")
                time.sleep(0.5)         # the sink must be listening before the source starts
                self._launch("bulk", ["python3", "-B", BULK, "source", "--host", SINK_IP,
                                      "--duration", "86400", "--streams", "4"], "bulk")
            self.running, self.bulk, self.started = True, bulk, time.time()

    def stop(self):
        with self.lock:
            procs, self.procs = self.procs, []
            self.running, self.started = False, None
        for _, p, _ in procs:
            if p.poll() is None:
                p.terminate()
        deadline = time.time() + 5
        for _, p, _ in procs:
            try:
                p.wait(max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                p.kill()
        for f in glob.glob(os.path.join(STATS, "*.json")):
            os.unlink(f)

    # ---------------------------------------------------------------- numbers

    def _sample_queues(self):
        while True:
            try:
                now, b = time.time(), common.tc_classes(TRUNK)
                if self._prev:
                    dt = max(0.1, now - self._prev[0])
                    self.rates = {q: max(0.0, (b.get(q, 0) - self._prev[1].get(q, 0)) * 8 / dt / 1e6)
                                  for q in (1, 2, 3)}
                self._prev = (now, b)
            except Exception:
                self._prev, self.rates = None, {1: 0.0, 2: 0.0, 3: 0.0}
            time.sleep(1.0)

    @staticmethod
    def _ctl(method, path, body=None):
        try:
            return common.rest(method, path, body, timeout=1.5)
        except Exception:
            return None

    def _why(self, policy, name, rooms):
        tier = next((("%s → %s" % (pat, k)) for pat, k in policy.user_tiers
                     if fnmatch.fnmatchcase(name, pat)),
                    "default → %s" % policy.default_user_class)
        extra = ["room %s → %s" % (r, policy.room_class(r)) for r in rooms
                 if policy.room_class(r) != policy.default_room_class]
        return " · ".join([tier] + extra)

    def status(self):
        now = time.time()
        health = self._ctl("GET", "/health")
        state = self._ctl("GET", "/chat/state") or {}
        policy = Policy.load(os.path.join(ROOT, "sdn", "policy.json"))
        conns = {}
        for c in state.get("connections", []):     # oldest first, so the newest wins
            if c.get("user") and not c.get("closed"):
                conns[c["user"]] = c
        snaps = {}
        for f in glob.glob(os.path.join(STATS, "*.json")):
            try:
                with open(f) as fh:
                    s = json.load(fh)
                snaps[s["host"]] = s
            except (OSError, ValueError, KeyError):
                pass
        users, per_class = [], {k: [] for k in QUEUES}
        for host, names in PLAN.items():
            for spec in names:
                name = spec.split(":")[0]
                u = (snaps.get(host) or {}).get("users", {}).get(name)
                c = conns.get(name)
                rtts = [ms for t, ms in (u or {}).get("rtts", []) if now - t <= WINDOW]
                klass = c["class"] if c else None
                if klass in per_class:
                    per_class[klass] += rtts
                stalled = bool(u) and u["state"] == "ok" and u["sent"] > 0 and \
                    (now - (u["last_ack"] or u["started"] or now)) > STALL
                users.append({
                    "user": name, "host": host, "class": klass, "queue": c.get("queue") if c else None,
                    "why": self._why(policy, name, c["rooms"] if c else
                                     ([spec.split(":")[1]] if ":" in spec else [])),
                    "p50": pct(rtts, 50), "p95": pct(rtts, 95),
                    "sent": u["sent"] if u else 0, "acked": u["acked"] if u else 0,
                    "errors": u["errors"] if u else 0,
                    "state": ("stalled" if stalled else u["state"]) if u else "off"})
        classes = {k: {"queue": q, "users": sum(1 for x in users if x["class"] == k),
                       "p50": pct(per_class[k], 50), "p95": pct(per_class[k], 95),
                       "mbps": round(self.rates[q], 2) if self.running else 0.0}
                   for k, q in QUEUES.items()}
        problems = []
        with self.lock:
            for name, p, log in self.procs:
                if p.poll() is not None:
                    try:
                        tail = open(log).read().strip().splitlines()[-1:]
                    except OSError:
                        tail = []
                    problems.append("%s exited (%s)" % (name, tail[0] if tail else p.returncode))
            running, bulk, started = self.running, self.bulk, self.started
        events = [e for e in state.get("transitions", []) if e["kind"] != "new"][-12:]
        return {"controller": health is not None,
                "network": subprocess.call(["pgrep", "-f", "mininet:c1$"], stdout=subprocess.DEVNULL) == 0,
                "running": running, "bulk": bulk, "uptime": round(now - started, 1) if started else 0,
                "mode": (health or {}).get("mode"), "bulk_class": (health or {}).get("bulk_class"), "monitor": (health or {}).get("monitor"),
                "classes": classes, "users": users, "events": events, "problems": problems}


DEMO = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/status":
            return self._send(200, DEMO.status())
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        # A custom header cannot be sent cross-site without a CORS preflight, so
        # another web page cannot toggle the traffic.
        if self.headers.get("X-Demo") != "1":
            return self._send(403, {"error": "missing X-Demo header"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.path == "/api/traffic":
                DEMO.start(bool(body.get("bulk", True))) if body.get("on") else DEMO.stop()
            elif self.path == "/api/mode" and body.get("mode") in ("sdn", "static"):
                common.rest("POST", "/mode", {"mode": body["mode"], "monitor": body["mode"] == "sdn"})
            elif self.path == "/api/bulkclass" and body.get("bulk_class") in ("LOW", "NORMAL"):
                common.rest("POST", "/mode", {"bulk_class": body["bulk_class"]})   # flushes flows; they are re-learned
            else:
                return self._send(404, {"error": "not found"})
        except Exception as exc:
            return self._send(500, {"error": str(exc)})
        self._send(200, {"ok": True})


def main():
    global DEMO
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    if os.geteuid() != 0:
        sys.exit("run as root: sudo python3 ui/server.py")
    DEMO = Demo()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=httpd.shutdown).start())
    print("demo UI on http://%s:%d  (Ctrl-C to quit)" % (args.host, args.port), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        DEMO.stop()


if __name__ == "__main__":
    main()
