#!/usr/bin/env python3
"""Bulk TCP traffic: a sink that discards data and a source that sends as fast
as TCP allows. Used as background congestion (no iperf dependency).

  sink:    python3 experiments/bulk.py sink --port 6000
  source:  python3 experiments/bulk.py source --host 10.0.0.2 --duration 20 --streams 4
"""
import argparse
import json
import socket
import sys
import threading
import time

CHUNK = 64 * 1024


def sink(args):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", args.port))
    srv.listen(64)

    def drain(conn):
        with conn:
            while conn.recv(CHUNK):
                pass

    while True:
        conn, _ = srv.accept()
        threading.Thread(target=drain, args=(conn,), daemon=True).start()


def source(args):
    payload = b"\0" * CHUNK
    sent = [0] * args.streams
    stop = time.time() + args.duration

    def blast(i):
        try:
            s = socket.create_connection((args.host, args.port), timeout=10)
            while time.time() < stop:
                s.sendall(payload)
                sent[i] += len(payload)
            s.close()
        except OSError as exc:
            sys.stderr.write("stream %d: %s\n" % (i, exc))

    t0 = time.time()
    threads = [threading.Thread(target=blast, args=(i,)) for i in range(args.streams)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - t0
    total = sum(sent)
    result = {"bytes": total, "seconds": round(elapsed, 3), "streams": args.streams,
              "mbps": round(total * 8 / elapsed / 1e6, 3)}
    print(json.dumps(result))
    if args.out:
        with open(args.out, "w") as f:
            json.dump(result, f)


def main():
    p = argparse.ArgumentParser(description="bulk TCP source/sink")
    p.add_argument("role", choices=["sink", "source"])
    p.add_argument("--host", default="10.0.0.2")
    p.add_argument("--port", type=int, default=6000)
    p.add_argument("--duration", type=float, default=20)
    p.add_argument("--streams", type=int, default=4)
    p.add_argument("--out")
    args = p.parse_args()
    (sink if args.role == "sink" else source)(args)


if __name__ == "__main__":
    main()
