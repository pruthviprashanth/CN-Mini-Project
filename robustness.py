#!/usr/bin/env python3
"""Challenging scenarios (needs root):

  sudo python3 experiments/robustness.py

block:  8 users chat; the admin blocks user_5 via REST mid-run. Expect user_5's
        traffic to stop at once while the others are unaffected. The server
        sees only a silent connection, reaped by TCP keepalive (~25 s); after
        that a re-login by user_5 is dropped by the network on the login
        event, and works again once unblocked.
link:   the s1-s2 trunk goes down for 3 s during chat. Expect TCP to retransmit
        and every message to be delivered after the link returns (no app-level
        reconnect), with an RTT spike for messages sent during the outage.

Writes results/robustness.json and results/link_failure_timeseries.csv.
"""
import csv
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mininet.log import setLogLevel  # noqa: E402

from experiments import common  # noqa: E402
from experiments.run_experiments import LOAD, PY, RESULTS, Testbed, log, summarize  # noqa: E402
from topo.topology import SERVER_IP, start_server  # noqa: E402

RELOGIN = r"""
import sys, json, time
sys.path.insert(0, %(root)r)
from chat.client import ChatClient
res = {}
try:
    c = ChatClient(%(srv)r, 5000, timeout=4).connect()
    try:
        c.login("user_5", "loadtest1")
        res["login"] = "ok"
    except Exception as e:
        res["login"] = "failed: %%s" %% e
    # A request sent within a few ms of the login reply can beat the
    # controller's drop flows (the login event is asynchronous), so test both.
    for key, wait in (("msg_immediate", 0), ("msg", 0.5)):
        time.sleep(wait)
        try:
            c.msg("general", "am I back?")
            res[key] = "ok"
        except Exception as e:
            res[key] = "failed: %%s" %% e
except Exception as e:
    res["connect"] = "failed: %%s" %% e
print(json.dumps(res))
"""


def run_load(tb, d, n, duration, during=None):
    """Run n users across c1..c4; call during(start_at) in a thread."""
    os.makedirs(d, exist_ok=True)
    server = start_server(tb.net, users_file=os.path.join(d, "users.json"),
                          log=os.path.join(d, "server.log"))
    start_at = time.time() + 4
    procs, outs = [], []
    per = n // 4
    for i, host in enumerate(("c1", "c2", "c3", "c4")):
        out = os.path.join(d, "load_%s.json" % host)
        outs.append(out)
        procs.append(tb.net.get(host).popen(
            [PY, LOAD, "--server", SERVER_IP, "--first", str(i * per), "--users", str(per),
             "--total", str(n), "--rate", "5", "--room-size", str(n), "--duration", str(duration),
             "--start-at", repr(start_at), "--drain", "8", "--out", out],
            stderr=subprocess.DEVNULL))
    t = None
    if during:
        t = threading.Thread(target=during, args=(start_at,))
        t.start()
    for p in procs:
        p.wait(timeout=duration + 60)
    if t:
        t.join()
    users = []
    for out in outs:
        with open(out) as f:
            users.extend(json.load(f)["users"])
    return server, users, start_at


def block_scenario(tb, d):
    common.rest("POST", "/mode", {"mode": "sdn", "monitor": True})
    time.sleep(1)
    info = {}

    def admin(start_at):
        time.sleep(max(0, start_at + 5 - time.time()))
        info["block_t"] = time.time() - start_at
        info["block_reply"] = common.rest("POST", "/policy/block", {"user": "user_5"})

    server, users, start_at = run_load(tb, d, 8, 12, admin)
    # The server only sees a silent connection; TCP keepalive reaps it.
    t0 = time.time()
    while time.time() - t0 < 60 and "user_5 disconnected" not in open(os.path.join(d, "server.log")).read():
        time.sleep(0.5)
    info["stale_session_reaped_after_s"] = round(time.time() - start_at - info["block_t"], 1)
    # Re-login attempt from another host while still blocked.
    code = RELOGIN % {"root": common.ROOT, "srv": SERVER_IP}
    out = tb.net.get("c2").popen([PY, "-c", code], stdout=subprocess.PIPE).communicate(timeout=30)[0]
    info["relogin_while_blocked"] = json.loads(out.decode().strip().splitlines()[-1])
    common.rest("POST", "/policy/unblock", {"user": "user_5"})
    # That attempt left its own silent session on the server; let it be reaped.
    t0 = time.time()
    while time.time() - t0 < 60 and open(os.path.join(d, "server.log")).read().count("user_5 disconnected") < 2:
        time.sleep(0.5)
    out = tb.net.get("c2").popen([PY, "-c", code], stdout=subprocess.PIPE).communicate(timeout=30)[0]
    info["relogin_after_unblock"] = json.loads(out.decode().strip().splitlines()[-1])
    server.terminate()
    victim = [u for u in users if u["user"] == "user_5"][0]
    others = [u for u in users if u["user"] != "user_5"]
    last_ack = max([r[0] for r in victim["rtt"]] or [0])
    info.update({
        "blocked_user_sent": victim["sent"], "blocked_user_acked": victim["acked"],
        "blocked_user_last_ack_t": round(last_ack, 2),
        "others": summarize(others, 12),
        "transitions": [t["text"] for t in common.rest("GET", "/chat/state")["transitions"]
                        if t["kind"] in ("block", "class") and "user_5" in t["text"]],
    })
    ok = (info["others"]["unacked"] == 0 and last_ack <= info["block_t"] + 0.5
          and victim["acked"] < victim["sent"]
          and info["relogin_while_blocked"].get("msg", "").startswith("failed")
          and info["relogin_after_unblock"].get("msg") == "ok")
    info["pass"] = ok
    return info


def link_scenario(tb, d):
    common.rest("POST", "/mode", {"mode": "sdn", "monitor": True})
    time.sleep(1)
    info = {}

    def flap(start_at):
        time.sleep(max(0, start_at + 4 - time.time()))
        info["down_t"] = round(time.time() - start_at, 2)
        tb.net.configLinkStatus("s1", "s2", "down")
        time.sleep(3)
        tb.net.configLinkStatus("s1", "s2", "up")
        info["up_t"] = round(time.time() - start_at, 2)

    server, users, _ = run_load(tb, d, 8, 12, flap)
    server.terminate()
    s = summarize(users, 12)
    info.update({"summary": s, "pass": s["unacked"] == 0 and s["delivery_loss"] == 0})
    rows = [{"t": r[0], "rtt_ms": r[1], "user": u["user"]} for u in users for r in u["rtt"]]
    with open(os.path.join(RESULTS, "link_failure_timeseries.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["t", "rtt_ms", "user"])
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r["t"]))
    return info


def main():
    if os.geteuid() != 0:
        sys.exit("run as root (sudo)")
    setLogLevel("warning")
    raw = os.path.join(RESULTS, "raw", "robustness")
    tb = Testbed(raw)
    res = {}
    try:
        log("block scenario")
        res["block"] = block_scenario(tb, os.path.join(raw, "block"))
        log("   %s" % json.dumps({k: v for k, v in res["block"].items() if k != "others"}))
        log("link failure scenario")
        res["link_failure"] = link_scenario(tb, os.path.join(raw, "link"))
        s = res["link_failure"]["summary"]
        log("   down %.1fs up %.1fs: rtt max %s ms, unacked %s, delivery loss %s"
            % (res["link_failure"]["down_t"], res["link_failure"]["up_t"], s["rtt_max"],
               s["unacked"], s["delivery_loss"]))
    finally:
        tb.close()
    with open(os.path.join(RESULTS, "robustness.json"), "w") as f:
        json.dump(res, f, indent=1)
    uid, gid = int(os.environ.get("SUDO_UID", 0)), int(os.environ.get("SUDO_GID", 0))
    if uid:
        for dirpath, dirs, files in os.walk(RESULTS):
            for name in dirs + files + [""]:
                os.chown(os.path.join(dirpath, name), uid, gid)
    log("block: %s, link failure: %s" % ("PASS" if res["block"]["pass"] else "FAIL",
                                         "PASS" if res["link_failure"]["pass"] else "FAIL"))


if __name__ == "__main__":
    main()
