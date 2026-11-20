#!/usr/bin/env python3
"""Load generator: K simulated chat users in one process (one thread each).

Run one process per Mininet client host. Users are numbered globally
(--first .. --first+K-1 out of --total); the first --vip-fraction of all
users are named vip_N, the rest user_N. Users are grouped into rooms of
--room-size, so a room's members span several hosts.

Each user connects, registers (or logs in if it exists), joins its room, waits
for the shared --start-at time, then sends --rate msgs/s for --duration s.
Recorded per user: connect+login time, send->ack RTT per message with its send
time, delivery latency of messages received (sender ts vs receive time; all
Mininet hosts share one clock), and sent/acked/delivered/received counts.

--flood turns the process into one abusive user ("flooder", regular tier) that
sends large messages to its own nearly empty room as fast as TCP allows.
"""
import argparse
import json
import os
import random
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chat.client import ChatClient, ChatError  # noqa: E402

PASSWORD = "loadtest1"


def user_name(i, total, vip_fraction):
    return ("vip_%d" if i < int(round(total * vip_fraction)) else "user_%d") % i


class SimUser(threading.Thread):
    def __init__(self, args, index):
        super(SimUser, self).__init__(daemon=True)
        self.args = args
        self.index = index
        self.name_ = user_name(index, args.total, args.vip_fraction)
        self.room = "load_%d" % (index // args.room_size)
        self.rec = {"user": self.name_, "vip": self.name_.startswith("vip_"), "room": self.room,
                    "connect_ms": None, "sent": 0, "acked": 0, "errors": 0, "delivered": 0,
                    "received": 0, "rtt": [], "delivery": [], "error": None,
                    "late_start_s": 0.0}
        self._lock = threading.Lock()

    def on_message(self, msg):
        if msg.get("type") == "msg" and msg.get("room") == self.room:
            now = time.time()
            with self._lock:
                self.rec["received"] += 1
                self.rec["delivery"].append([round(now - self.args.start_at, 4),
                                             round((now - msg["ts"]) * 1000, 3),
                                             msg.get("from", "").startswith("vip_")])

    def on_reply(self, reply, t_send):
        now = time.time()
        with self._lock:
            if reply["type"] == "ack":
                self.rec["acked"] += 1
                self.rec["delivered"] += reply.get("delivered", 0)
                self.rec["rtt"].append([round(t_send - self.args.start_at, 4),
                                        round((now - t_send) * 1000, 3)])
            else:
                self.rec["errors"] += 1

    def run(self):
        a = self.args
        try:
            c = ChatClient(a.server, a.port, on_message=self.on_message, timeout=30)
            t0 = time.time()
            c.connect()
            t_conn = time.time() - t0
            try:
                c.register(self.name_, PASSWORD)
            except ChatError:
                pass                        # already registered
            t1 = time.time()
            c.login(self.name_, PASSWORD)
            self.rec["connect_ms"] = round((t_conn + time.time() - t1) * 1000, 3)
            c.join(self.room)
        except Exception as exc:
            self.rec["error"] = "setup: %s" % exc
            return
        delay = a.start_at - time.time()
        if delay > 0:
            time.sleep(delay)
        else:
            # Not an error: the user still sends, just starts late.
            self.rec["late_start_s"] = round(-delay, 3)
        try:
            if a.flood:
                self._flood(c)
            else:
                self._steady(c)
        except Exception as exc:
            self.rec["error"] = "run: %s" % exc
        # Keep receiving for a moment so in-flight messages are counted.
        deadline = time.time() + a.drain
        while time.time() < deadline:
            with self._lock:
                r = self.rec
                if r["sent"] == r["acked"] + r["errors"]:
                    break
            time.sleep(0.05)
        time.sleep(max(0.0, a.start_at + a.duration + a.drain - time.time()))
        c.close()

    def _send(self, c, text):
        t = time.time()
        # The send time travels in the closure, so an ack that arrives before
        # send_async() returns is still matched.
        c.send_async("msg", lambda reply: self.on_reply(reply, t), room=self.room, text=text, ts=t)
        with self._lock:
            self.rec["sent"] += 1

    def _steady(self, c):
        a = self.args
        interval = 1.0 / a.rate
        pad = "x" * max(0, a.size - 40)
        nxt = a.start_at + random.random() * interval   # spread users over the interval
        end = a.start_at + a.duration
        n = 0
        while True:
            now = time.time()
            if nxt > end:
                break
            if nxt > now:
                time.sleep(nxt - now)
            self._send(c, "%s #%d %s" % (self.name_, n, pad))
            n += 1
            nxt += interval

    def _flood(self, c):
        a = self.args
        text = "F" * a.size
        start = a.start_at + a.flood_start
        end = start + a.flood_duration
        if start > time.time():
            time.sleep(start - time.time())
        while time.time() < end:
            # sendall() blocks once the socket buffer is full, so the flood
            # runs at whatever rate the network gives this connection.
            self._send(c, text)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", default="10.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--first", type=int, default=0, help="global index of this process's first user")
    p.add_argument("--users", type=int, default=5, help="users in this process")
    p.add_argument("--total", type=int, default=None, help="users across all processes")
    p.add_argument("--vip-fraction", type=float, default=0.2)
    p.add_argument("--room-size", type=int, default=10)
    p.add_argument("--rate", type=float, default=2.0, help="messages per second per user")
    p.add_argument("--size", type=int, default=120, help="approximate message size in bytes")
    p.add_argument("--duration", type=float, default=20)
    p.add_argument("--start-at", type=float, default=None, help="epoch time to start sending")
    p.add_argument("--drain", type=float, default=3.0)
    p.add_argument("--flood", action="store_true")
    p.add_argument("--flood-start", type=float, default=0.0)
    p.add_argument("--flood-duration", type=float, default=None)
    p.add_argument("--out", help="write raw JSON results here")
    args = p.parse_args()
    if args.total is None:
        args.total = args.first + args.users
    if args.start_at is None:
        args.start_at = time.time() + 3 + 0.05 * args.users
    if args.flood:
        args.users, args.room_size = 1, 10 ** 9
        args.flood_duration = args.flood_duration or args.duration - args.flood_start
    users = []
    if args.flood:
        u = SimUser(args, 0)
        u.name_, u.room = "flooder", "spam"
        u.rec.update(user="flooder", vip=False, room="spam")
        users.append(u)
    else:
        users = [SimUser(args, args.first + k) for k in range(args.users)]
    for u in users:
        u.start()
        time.sleep(0.01)        # avoid a SYN burst overflowing the accept backlog
    for u in users:
        u.join()
    out = {"args": vars(args), "users": [u.rec for u in users]}
    data = json.dumps(out)
    if args.out:
        with open(args.out, "w") as f:
            f.write(data)
    ok = sum(1 for u in users if not u.rec["error"])
    sys.stderr.write("%d/%d users ok, sent %d acked %d\n" % (
        ok, len(users), sum(u.rec["sent"] for u in users), sum(u.rec["acked"] for u in users)))


if __name__ == "__main__":
    main()
