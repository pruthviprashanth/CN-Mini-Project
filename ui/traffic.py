#!/usr/bin/env python3
"""Steady chat traffic for the demo UI (ui/server.py starts one per client host).

Runs inside a Mininet host. Each --users entry is NAME or NAME:EXTRA_ROOM; every
user logs in (registering first if needed), optionally joins EXTRA_ROOM, then
sends a message to 'general' --rate times a second until SIGTERM. Every 0.5 s
it writes a snapshot (counters + recent send->ack round-trip times) to --out;
the file system is shared with the UI server, the network is not.
"""
import argparse
import json
import os
import random
import signal
import sys
import threading
import time

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from chat.client import ChatClient, ChatError  # noqa: E402

PASSWORD = "demo1234"
KEEP = 8.0          # seconds of RTT samples kept in the snapshot


class User(threading.Thread):
    def __init__(self, spec, args, stop):
        super(User, self).__init__(daemon=True)
        self.name_, _, self.extra = spec.partition(":")
        self.args, self.stop = args, stop
        self.lock = threading.Lock()
        self.sent = self.acked = self.errors = 0
        self.rtts = []              # (time of ack, ms)
        self.last_ack = None
        self.started = None
        self.state = "connecting"

    def on_reply(self, reply, t_send):
        now = time.time()
        with self.lock:
            if reply.get("type") == "ack":
                self.acked += 1
                self.last_ack = now
                self.rtts.append((now, (now - t_send) * 1000.0))
            else:
                self.errors += 1

    def run(self):
        a = self.args
        try:
            c = ChatClient(a.server, 5000, timeout=10)
            c.connect()
            try:
                c.register(self.name_, PASSWORD)
            except ChatError:
                pass                                # account already exists
            c.login(self.name_, PASSWORD)
            if self.extra:
                c.join(self.extra)
        except Exception as exc:
            self.state = "error: %s" % exc
            return
        self.state = "ok"
        self.started = time.time()
        pad = "x" * max(0, a.size - 30)
        interval = 1.0 / a.rate
        nxt = time.time() + random.random() * interval      # spread users out
        n = 0
        while not self.stop.is_set():
            delay = nxt - time.time()
            if delay > 0 and self.stop.wait(delay):
                break
            t = time.time()
            try:
                c.send_async("msg", lambda r, t=t: self.on_reply(r, t),
                             room="general", text="%s #%d %s" % (self.name_, n, pad), ts=t)
            except OSError:
                self.state = "disconnected"
                return
            with self.lock:
                self.sent += 1
            n += 1
            nxt += interval
            if nxt < time.time() - 1.0:                      # after a stall, do not burst
                nxt = time.time()
        c.quit()


def snapshot(users, host):
    now = time.time()
    out = {}
    for u in users:
        with u.lock:
            u.rtts = [r for r in u.rtts if now - r[0] <= KEEP]
            out[u.name_] = {"state": u.state, "sent": u.sent, "acked": u.acked,
                            "errors": u.errors, "last_ack": u.last_ack, "started": u.started,
                            "rtts": [[round(t, 3), round(ms, 2)] for t, ms in u.rtts]}
    return {"host": host, "ts": now, "users": out}


def write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", required=True, help="host name, used in the snapshot")
    p.add_argument("--users", required=True, help="comma separated NAME[:EXTRA_ROOM]")
    p.add_argument("--out", required=True)
    p.add_argument("--server", default="10.0.0.1")
    p.add_argument("--rate", type=float, default=4.0, help="messages per second per user")
    p.add_argument("--size", type=int, default=150, help="approximate message size in bytes")
    args = p.parse_args()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    users = [User(s, args, stop) for s in args.users.split(",") if s]
    for u in users:
        u.start()
        time.sleep(0.05)            # login hashing is CPU heavy; do not start all at once
    while not stop.is_set():
        write(args.out, snapshot(users, args.name))
        stop.wait(0.5)
    for u in users:
        u.join(timeout=2)
    os._exit(0)                     # a blocked user's socket must not keep us alive


if __name__ == "__main__":
    main()
