#!/usr/bin/env python3
"""End-to-end check on the real Mininet network (needs root):

  sudo python3 tests/mininet_check.py

Starts the controller, builds the topology, and verifies: pingall, chat across
the network, chat in queue 2 / bulk in queue 3 (flow tables and tc counters),
a vip_ login moving its flows to queue 1, and admin block/unblock.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mininet.log import setLogLevel  # noqa: E402

from experiments import common  # noqa: E402
from topo.topology import SERVER_IP, SINK_IP, build_network, start_server, stop_network  # noqa: E402

LOG_DIR = os.environ.get("CHECK_LOG_DIR", "/tmp/chat_check")
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" -- " + detail) if detail else ""))
    sys.stdout.flush()


def chat_flows(switch):
    return [l for l in common.dump_flows(switch).splitlines() if "priority=100" in l]


def queue_of(lines, port):
    for l in lines:
        if "tp_src=%d" % port in l or "tp_dst=%d" % port in l:
            if l.rstrip().endswith("actions=drop"):
                return "drop"
            if "set_queue:" in l:
                return int(l.split("set_queue:")[1].split(",")[0])
    return None


CLIENT_SCRIPT = r"""
import sys, time, json
sys.path.insert(0, %(root)r)
from chat.client import ChatClient
c = ChatClient(%(srv)r, 5000, timeout=10).connect()
print(json.dumps({"port": c.local_addr[1]}), flush=True)
try:
    c.register(%(user)r, "pw1234")
except Exception:
    pass
c.login(%(user)r, "pw1234")
c.join("lobby")
t_end = time.time() + %(secs)f
n = 0
while time.time() < t_end:
    t0 = time.time()
    try:
        c.msg("lobby", "hello %%d from %(user)s" %% n)
    except Exception as e:
        print(json.dumps({"error": str(e)}), flush=True)
        break
    n += 1
    time.sleep(0.2)
print(json.dumps({"sent": n}), flush=True)
c.close()
"""


def start_client(host, user, secs):
    code = CLIENT_SCRIPT % {"root": common.ROOT, "srv": SERVER_IP, "user": user, "secs": secs}
    p = host.popen(["python3", "-u", "-c", code], stdout=-1, stderr=-1)
    line = p.stdout.readline().decode()
    return p, json.loads(line)["port"]


def main():
    setLogLevel("warning")
    os.makedirs(LOG_DIR, exist_ok=True)
    common.cleanup_mininet()
    ctl = common.start_controller(os.path.join(LOG_DIR, "controller.log"))
    net = None
    procs = []
    try:
        net = build_network()
        common.wait_switches()
        loss = net.pingAll(timeout=2)
        check("pingall across s1-s2", loss == 0, "%.0f%% loss" % loss)

        server = start_server(net, log=os.path.join(LOG_DIR, "server.log"))
        procs.append(server)
        c1, c2, bulk, sink = (net.get(h) for h in ("c1", "c2", "bulk", "sink"))

        # --- chat across Mininet, regular user -> queue 2
        p1, port1 = start_client(c1, "alice", 25)
        procs.append(p1)
        time.sleep(2)
        st = common.rest("GET", "/chat/state")
        conn = [c for c in st["connections"] if c["client"].endswith(":%d" % port1)]
        check("controller learned alice from login event",
              conn and conn[0]["user"] == "alice" and "lobby" in conn[0]["rooms"],
              json.dumps(conn[0] if conn else st["connections"])[:300])
        q = queue_of(chat_flows("s1"), port1)
        check("regular user chat flow uses queue 2", q == 2, "s1 set_queue=%s" % q)
        check("chat 5-tuple flows on both switches, both directions",
              conn and len(conn[0]["flows"]) == 4, "%d flows" % (len(conn[0]["flows"]) if conn else 0))

        # --- bulk traffic -> queue 3, and tc counters actually move
        sink_p = sink.popen(["python3", os.path.join(common.ROOT, "experiments", "bulk.py"),
                             "sink"])
        procs.append(sink_p)
        time.sleep(0.5)
        before = common.tc_classes("s1-eth6")
        bulk_p = bulk.popen(["python3", os.path.join(common.ROOT, "experiments", "bulk.py"),
                             "source", "--host", SINK_IP, "--duration", "5", "--streams", "2"],
                            stdout=-1)
        # --- vip login while bulk is running -> queue 1
        p2, port2 = start_client(c2, "vip_bob", 15)
        procs.append(p2)
        out = bulk_p.communicate(timeout=30)[0].decode()
        after = common.tc_classes("s1-eth6")
        delta = {qid: after.get(qid, 0) - before.get(qid, 0) for qid in after}
        print("    trunk s1-eth6 bytes per queue during bulk: %s" % delta)
        bulk_lines = [l for l in common.dump_flows("s1").splitlines() if "priority=50" in l]
        check("bulk flow uses queue 3", any("set_queue:3" in l for l in bulk_lines),
              (bulk_lines[0].strip()[:160] if bulk_lines else "no bulk flow"))
        check("tc: bulk bytes went through queue 3", delta.get(3, 0) > 1000000,
              "q3 +%d bytes" % delta.get(3, 0))
        check("tc: chat bytes went through queue 2", delta.get(2, 0) > 0, "q2 +%d bytes" % delta.get(2, 0))
        check("tc: vip chat bytes went through queue 1", delta.get(1, 0) > 0, "q1 +%d bytes" % delta.get(1, 0))
        mbps = json.loads(out.strip().splitlines()[-1])["mbps"]
        check("bulk throughput limited by 10 Mbps trunk", 5 < mbps < 10.5, "%.2f Mbps" % mbps)
        q2 = queue_of(chat_flows("s1"), port2)
        check("vip_ login moved its flow to queue 1", q2 == 1, "s1 set_queue=%s" % q2)

        # --- block / unblock alice at the network level
        r = common.rest("POST", "/policy/block", {"user": "alice"})
        time.sleep(0.5)
        check("block installs drop flow", r["connections"] == 1 and
              queue_of(chat_flows("s1"), port1) == "drop",
              str([l.strip()[-60:] for l in chat_flows("s1") if str(port1) in l][:1]))
        common.rest("POST", "/policy/unblock", {"user": "alice"})
        time.sleep(0.5)
        check("unblock restores queue 2", queue_of(chat_flows("s1"), port1) == 2)

        # --- static baseline: everything in queue 2
        common.rest("POST", "/mode", {"mode": "static"})
        time.sleep(1.5)
        qs = {queue_of(chat_flows("s1"), port1), queue_of(chat_flows("s1"), port2)}
        check("static mode: both users in queue 2", qs == {2}, "queues %s" % qs)
        common.rest("POST", "/mode", {"mode": "sdn"})
        time.sleep(1.5)
        check("back to sdn mode: vip in queue 1 again", queue_of(chat_flows("s1"), port2) == 1)

        for p in (p1, p2):
            p.wait(timeout=40)
            tail = p.stdout.read().decode().strip().splitlines()
            print("    client output: %s" % tail[-1:])
        srv_log = open(os.path.join(LOG_DIR, "server.log")).read()
        check("server saw both users log in over Mininet",
              "alice logged in from 10.0.0.11" in srv_log and "vip_bob logged in from 10.0.0.12" in srv_log)
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        if net:
            stop_network(net)
        common.stop_controller(ctl)
    failed = [n for n, ok in results if not ok]
    print("\n%d/%d checks passed" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
