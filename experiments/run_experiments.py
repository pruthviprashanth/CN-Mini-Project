#!/usr/bin/env python3
"""Experiment runner (needs root). Starts its own Ryu controller.

  sudo python3 experiments/run_experiments.py scaling --users 10 25 50 100
  sudo python3 experiments/run_experiments.py flood
  sudo python3 experiments/run_experiments.py all

scaling: for each user count, variants static/sdn x with/without bulk congestion.
flood:   one abusive client floods the client->server path; victims' latency
         over time under static, sdn (monitor off) and sdn (monitor on).

Raw JSON goes to results/raw/, summaries to results/*.csv; plot with plot.py.
"""
import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mininet.log import setLogLevel  # noqa: E402

from experiments import common  # noqa: E402
from topo.topology import SERVER_IP, SINK_IP, build_network, start_server, stop_network  # noqa: E402

RESULTS = os.path.join(common.ROOT, "results")
PY = "python3"
LOAD = os.path.join(common.ROOT, "experiments", "load_client.py")
BULK = os.path.join(common.ROOT, "experiments", "bulk.py")
CLIENT_HOSTS = ("c1", "c2", "c3", "c4")

SCALING_VARIANTS = [("static", False), ("static", True), ("sdn", False), ("sdn", True)]
FLOOD_VARIANTS = [("static", "static", False), ("sdn-nomonitor", "sdn", False),
                  ("sdn-monitor", "sdn", True)]


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg))
    sys.stdout.flush()


# ---------------------------------------------------------------- statistics

def pct(values, p):
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def mean(values):
    return sum(values) / len(values) if values else None


def summarize(users, duration, warmup=1.0):
    """Aggregate per-user records; samples in the first `warmup` s are skipped."""
    def rtts(sel):
        return [r[1] for u in users if sel(u) for r in u["rtt"] if r[0] >= warmup]
    allr = rtts(lambda u: True)
    vip, reg = rtts(lambda u: u["vip"]), rtts(lambda u: not u["vip"])
    deliv = [d[1] for u in users for d in u["delivery"] if d[0] >= warmup]
    conn = [u["connect_ms"] for u in users if u["connect_ms"] is not None]
    expected = sum(u["delivered"] for u in users)
    received = sum(u["received"] for u in users)
    sent, acked = sum(u["sent"] for u in users), sum(u["acked"] for u in users)
    r = lambda x: None if x is None else round(x, 3)  # noqa: E731
    return {
        "users_ok": sum(1 for u in users if not u["error"]), "users": len(users),
        "late_starts": sum(1 for u in users if u.get("late_start_s")),
        "sent": sent, "acked": acked, "errors": sum(u["errors"] for u in users),
        "msgs_per_s": r(acked / duration),
        "rtt_mean": r(mean(allr)), "rtt_p50": r(pct(allr, 50)), "rtt_p95": r(pct(allr, 95)),
        "rtt_p99": r(pct(allr, 99)), "rtt_max": r(max(allr) if allr else None),
        "vip_rtt_p50": r(pct(vip, 50)), "vip_rtt_p95": r(pct(vip, 95)),
        "reg_rtt_p50": r(pct(reg, 50)), "reg_rtt_p95": r(pct(reg, 95)),
        "deliv_p50": r(pct(deliv, 50)), "deliv_p95": r(pct(deliv, 95)), "deliv_p99": r(pct(deliv, 99)),
        "connect_p50": r(pct(conn, 50)), "connect_p95": r(pct(conn, 95)),
        "unacked": sent - acked,
        "delivery_loss": r(1 - received / expected) if expected else None,
    }


# ---------------------------------------------------------------- one run

class Testbed:
    def __init__(self, raw_dir, n_clients=4):
        self.raw_dir = raw_dir
        os.makedirs(raw_dir, exist_ok=True)
        common.cleanup_mininet()
        self.ctl = common.start_controller(os.path.join(raw_dir, "controller.log"))
        self.net = build_network(n_clients)
        common.wait_switches()
        loss = self.net.pingAll(timeout=2)
        if loss:
            raise RuntimeError("pingall lost %.0f%%" % loss)
        self.sink = self.net.get("sink").popen([PY, BULK, "sink"])

    def close(self):
        if self.sink.poll() is None:
            self.sink.terminate()
        stop_network(self.net)
        common.stop_controller(self.ctl)

    def run(self, tag, n_users, duration, mode, monitor, bulk=False, rate=2.0, size=120,
            room_size=10, flood=None):
        d = os.path.join(self.raw_dir, tag)
        os.makedirs(d, exist_ok=True)
        common.rest("POST", "/mode", {"mode": mode, "monitor": monitor})
        time.sleep(1.0)
        server = start_server(self.net, users_file=os.path.join(d, "users.json"),
                              log=os.path.join(d, "server.log"))
        procs, outs = [], []
        # Registration + login cost is dominated by PBKDF2 hashing on the shared
        # CPU, so the setup window grows with the number of users.
        setup = 4.0 + 0.1 * n_users
        bulk_p = None
        try:
            if bulk:
                bulk_secs = 3 + setup + duration + 1
                bulk_p = self.net.get("bulk").popen(
                    [PY, BULK, "source", "--host", SINK_IP, "--streams", "4",
                     "--duration", str(bulk_secs), "--out", os.path.join(d, "bulk.json")])
                time.sleep(3)           # let TCP ramp up and fill the queues
            start_at = time.time() + setup
            per = [n_users // len(CLIENT_HOSTS) + (1 if i < n_users % len(CLIENT_HOSTS) else 0)
                   for i in range(len(CLIENT_HOSTS))]
            first = 0
            for host, k in zip(CLIENT_HOSTS, per):
                if k == 0:
                    continue
                out = os.path.join(d, "load_%s.json" % host)
                outs.append(out)
                procs.append(self.net.get(host).popen(
                    [PY, LOAD, "--server", SERVER_IP, "--first", str(first), "--users", str(k),
                     "--total", str(n_users), "--rate", str(rate), "--size", str(size),
                     "--room-size", str(room_size), "--duration", str(duration),
                     "--start-at", repr(start_at), "--out", out],
                    stderr=subprocess.PIPE))
                first += k
            if flood:
                out = os.path.join(d, "load_flooder.json")
                procs.append(self.net.get("c4").popen(
                    [PY, LOAD, "--server", SERVER_IP, "--flood", "--size", str(flood["size"]),
                     "--duration", str(duration), "--flood-start", str(flood["start"]),
                     "--flood-duration", str(flood["duration"]),
                     "--start-at", repr(start_at), "--out", out], stderr=subprocess.PIPE))
            for p in procs:
                _, err = p.communicate(timeout=setup + duration + 60)
                if err:
                    log("   load: %s" % err.decode().strip())
            if bulk_p:
                bulk_p.wait(timeout=60)
        finally:
            for p in procs + ([bulk_p] if bulk_p else []):
                if p.poll() is None:
                    p.kill()
            server.terminate()
            server.wait(5)
        users = []
        for out in outs:
            with open(out) as f:
                users.extend(json.load(f)["users"])
        res = {"tag": tag, "users_n": n_users, "mode": mode, "monitor": monitor,
               "bulk": bulk, "duration": duration, "rate": rate}
        res.update(summarize(users, duration))
        if bulk and os.path.exists(os.path.join(d, "bulk.json")):
            with open(os.path.join(d, "bulk.json")) as f:
                res["bulk_mbps"] = json.load(f)["mbps"]
        if flood and os.path.exists(os.path.join(d, "load_flooder.json")):
            with open(os.path.join(d, "load_flooder.json")) as f:
                fl = json.load(f)["users"][0]
            res["flood_msgs"] = fl["acked"]
            res["flood_mbps"] = round(fl["acked"] * flood["size"] * 8 / flood["duration"] / 1e6, 3)
        state = common.rest("GET", "/chat/state")
        with open(os.path.join(d, "controller_state.json"), "w") as f:
            json.dump(state, f, indent=1)
        res["controller_history"] = [h["text"] for h in state["transitions"]
                                     if h["kind"] == "flood"]
        with open(os.path.join(d, "summary.json"), "w") as f:
            json.dump(res, f, indent=1)
        return res, users


def write_csv(path, rows):
    keys = []
    for r in rows:
        keys += [k for k in r if k not in keys and k != "controller_history"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    log("wrote %s" % path)


# ---------------------------------------------------------------- experiments

def scaling(args):
    rows = []
    raw = os.path.join(RESULTS, "raw", "scaling")
    for n in args.users:
        tb = Testbed(os.path.join(raw, "n%d" % n))
        try:
            for mode, bulk in SCALING_VARIANTS:
                tag = "%s-%s" % (mode, "bulk" if bulk else "idle")
                log("scaling n=%d %s" % (n, tag))
                res, _ = tb.run(tag, n, args.duration, mode, monitor=(mode == "sdn"),
                                bulk=bulk, rate=args.rate)
                log("   rtt p50 %s p95 %s ms | vip p95 %s reg p95 %s | %s msg/s | loss %s | bulk %s Mbps"
                    % (res["rtt_p50"], res["rtt_p95"], res["vip_rtt_p95"], res["reg_rtt_p95"],
                       res["msgs_per_s"], res["delivery_loss"], res.get("bulk_mbps")))
                rows.append(res)
        finally:
            tb.close()
        write_csv(os.path.join(RESULTS, "scaling.csv"), rows)
    return rows


def flood(args):
    rows, series = [], []
    raw = os.path.join(RESULTS, "raw", "flood")
    fl = {"size": 7000, "start": args.flood_start, "duration": args.flood_duration}
    tb = Testbed(raw)
    try:
        for name, mode, monitor in FLOOD_VARIANTS:
            log("flood %s" % name)
            res, users = tb.run(name, args.flood_users, args.flood_total, mode, monitor, flood=fl)
            res["variant"] = name
            log("   reg p95 %s ms, vip p95 %s ms, flooder %s Mbps; %s"
                % (res["reg_rtt_p95"], res["vip_rtt_p95"], res.get("flood_mbps"),
                   res["controller_history"]))
            rows.append(res)
            for u in users:
                for t, rtt in u["rtt"]:
                    series.append({"variant": name, "group": "vip" if u["vip"] else "regular",
                                   "t": t, "rtt_ms": rtt})
    finally:
        tb.close()
    write_csv(os.path.join(RESULTS, "flood_summary.csv"), rows)
    write_csv(os.path.join(RESULTS, "flood_timeseries.csv"), series)
    with open(os.path.join(RESULTS, "flood_meta.json"), "w") as f:
        json.dump({"flood_start": fl["start"], "flood_end": fl["start"] + fl["duration"],
                   "duration": args.flood_total}, f)
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("experiment", choices=["scaling", "flood", "all"])
    p.add_argument("--users", type=int, nargs="+", default=[10, 25, 50, 100])
    p.add_argument("--duration", type=float, default=20)
    p.add_argument("--rate", type=float, default=2.0)
    p.add_argument("--flood-users", type=int, default=20)
    p.add_argument("--flood-total", type=float, default=60)
    p.add_argument("--flood-start", type=float, default=15)
    p.add_argument("--flood-duration", type=float, default=30)
    args = p.parse_args()
    if os.geteuid() != 0:
        sys.exit("run as root (sudo)")
    setLogLevel("warning")
    os.makedirs(RESULTS, exist_ok=True)
    if args.experiment in ("scaling", "all"):
        shutil.rmtree(os.path.join(RESULTS, "raw", "scaling"), ignore_errors=True)
        scaling(args)
    if args.experiment in ("flood", "all"):
        shutil.rmtree(os.path.join(RESULTS, "raw", "flood"), ignore_errors=True)
        flood(args)
    # Files were created as root; hand them back to the invoking user.
    uid, gid = int(os.environ.get("SUDO_UID", 0)), int(os.environ.get("SUDO_GID", 0))
    if uid:
        for dirpath, dirs, files in os.walk(RESULTS):
            for name in dirs + files + [""]:
                os.chown(os.path.join(dirpath, name), uid, gid)


if __name__ == "__main__":
    main()
