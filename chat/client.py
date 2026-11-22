"""Chat client: a reusable ChatClient library class plus an interactive CLI.

The library is also used by the load generator in experiments/.
"""
import argparse
import itertools
import os
import socket
import sys
import threading
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chat.protocol import LineReader, ProtocolError, REPLY_TYPES, encode, decode


class ChatError(Exception):
    """The server answered a request with an error frame."""


class ChatClient:
    """Thread-safe client. A reader thread dispatches replies by request id;
    everything else (msg, pm, info) goes to on_message."""

    def __init__(self, host, port=5000, on_message=None, on_disconnect=None, timeout=10.0):  # store settings and set up the request-id counter and pending-reply table
        self.host, self.port = host, port
        self.on_message = on_message
        self.on_disconnect = on_disconnect
        self.timeout = timeout
        self.user = None
        self.sock = None
        self._ids = itertools.count(1)
        self._pending = {}        # id -> callback(reply)
        self._plock = threading.Lock()
        self._wlock = threading.Lock()
        self._closed = threading.Event()

    def connect(self):  # open the TCP connection to the server and start the reader thread
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(None)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(target=self._reader, name="chat-reader", daemon=True).start()
        return self

    @property
    def local_addr(self):  # this client's own (ip, port), the connection's identity
        return self.sock.getsockname()

    # ---- request plumbing ----

    def send_async(self, kind, callback=None, **fields):  # send a request with a new id and register a callback; does not wait
        """Send a request without waiting; callback(reply) runs on the reader thread."""
        rid = next(self._ids)
        fields.update(type=kind, id=rid)
        data = encode(fields)
        if callback is not None:
            with self._plock:
                self._pending[rid] = callback
        try:
            with self._wlock:
                self.sock.sendall(data)
        except OSError:
            with self._plock:
                self._pending.pop(rid, None)
            raise
        return rid

    def request(self, kind, **fields):  # send a request and wait for its reply; raises ChatError on an error reply
        """Send a request and block for its reply; raise ChatError on 'error'."""
        done = threading.Event()
        box = []

        def cb(reply):  # store the reply and wake up request()
            box.append(reply)
            done.set()

        rid = self.send_async(kind, cb, **fields)
        if not done.wait(self.timeout):
            with self._plock:
                self._pending.pop(rid, None)
            raise TimeoutError("no reply to %s within %.1fs" % (kind, self.timeout))
        reply = box[0]
        if reply["type"] == "error":
            raise ChatError(reply.get("reason", "error"))
        return reply

    def _reader(self):  # reader thread: split incoming bytes into messages and dispatch them
        reader = LineReader()
        try:
            while True:
                data = self.sock.recv(65536)
                if not data:
                    break
                for raw in reader.feed(data):
                    try:
                        msg = decode(raw)
                    except ProtocolError:
                        continue
                    self._dispatch(msg)
        except (OSError, ProtocolError):
            pass
        finally:
            self._closed.set()
            with self._plock:
                pending, self._pending = self._pending, {}
            for cb in pending.values():
                cb({"type": "error", "reason": "disconnected"})
            if self.on_disconnect:
                self.on_disconnect()

    def _dispatch(self, msg):  # replies go to the waiting request by id; everything else goes to on_message
        cb = None
        if msg["type"] in REPLY_TYPES and msg.get("id") is not None:
            with self._plock:
                cb = self._pending.pop(msg["id"], None)
        if cb is not None:
            cb(msg)
        elif self.on_message:
            self.on_message(msg)

    def close(self):  # shut down and close the socket
        if self.sock is None:
            return
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        self._closed.wait(1.0)

    @property
    def closed(self):  # True once the connection is closed
        return self._closed.is_set()

    # ---- convenience API ----

    def register(self, user, password):  # ask the server to create an account
        return self.request("register", user=user, password=password)

    def login(self, user, password):  # ask the server to log in
        reply = self.request("login", user=user, password=password)
        self.user = user
        return reply

    def join(self, room):  # ask to join a room
        return self.request("join", room=room)

    def leave(self, room):  # ask to leave a room
        return self.request("leave", room=room)

    def msg(self, room, text):  # send a room message with a send timestamp
        return self.request("msg", room=room, text=text, ts=time.time())

    def pm(self, to, text):  # send a private message to one user
        return self.request("pm", to=to, text=text, ts=time.time())

    def rooms(self):  # ask for the room list
        return self.request("rooms")

    def users(self):  # ask for the online users
        return self.request("users")["users"]

    def ping(self):  # send a ping (the server replies with a pong)
        return self.request("ping")

    def quit(self):  # tell the server we are leaving, then close
        try:
            self.request("quit")
        except (ChatError, TimeoutError, OSError):
            pass
        self.close()


# ---- interactive CLI ----

HELP = """commands:
  /register USER PASS   create an account
  /login USER PASS      log in (auto-joins 'general')
  /join ROOM            join (and switch to) a room
  /leave ROOM           leave a room
  /room ROOM            switch current room (must be joined)
  /pm USER TEXT         private message
  /rooms                list rooms
  /users                list online users
  /ping                 measure round-trip time
  /quit                 disconnect
  anything else         sent to the current room"""


def _print(line):  # print a line and flush so it appears at once
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _show(msg):  # print an incoming message, PM, notice or error
    t = msg["type"]
    if t == "msg":
        _print("[%s] %s: %s" % (msg["room"], msg["from"], msg["text"]))
    elif t == "pm":
        _print("[pm from %s] %s" % (msg["from"], msg["text"]))
    elif t == "info":
        _print("* " + msg["text"])
    elif t == "error":
        _print("! " + msg.get("reason", "error"))
    else:
        _print("? %s" % msg)


def run_cli(args):  # the command loop: connect, log in, read typed lines, run /commands
    state = {"room": None, "quitting": False}
    client = ChatClient(args.host, args.port, on_message=_show,
                        on_disconnect=lambda: state["quitting"] or _print("* disconnected from server"))
    try:
        client.connect()
    except OSError as exc:
        _print("cannot connect to %s:%d: %s" % (args.host, args.port, exc))
        return 1
    _print("connected to %s:%d as %s:%d; /help for commands"
           % ((args.host, args.port) + tuple(client.local_addr)))

    def do_login(user, password):  # log in and make 'general' the current room
        client.login(user, password)
        state["room"] = "general"
        _print("* logged in as %s, current room: general" % user)

    if args.user and args.password:
        try:
            if args.register:
                try:
                    client.register(args.user, args.password)
                except ChatError as exc:
                    if "exists" not in str(exc):
                        raise
            do_login(args.user, args.password)
        except ChatError as exc:
            _print("! " + str(exc))

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if client.closed:
            break
        try:
            if not line.startswith("/"):
                if state["room"] is None:
                    _print("! log in first (/login USER PASS)")
                    continue
                client.msg(state["room"], line)
                continue
            parts = line.split(" ", 2)
            cmd, rest = parts[0], parts[1:]
            if cmd == "/help":
                _print(HELP)
            elif cmd == "/register" and len(rest) == 2:
                client.register(*rest)
                _print("* registered %s" % rest[0])
            elif cmd == "/login" and len(rest) == 2:
                do_login(*rest)
            elif cmd == "/join" and len(rest) == 1:
                client.join(rest[0])
                state["room"] = rest[0]
                _print("* joined %s (now current)" % rest[0])
            elif cmd == "/leave" and len(rest) == 1:
                client.leave(rest[0])
                if state["room"] == rest[0]:
                    state["room"] = "general" if rest[0] != "general" else None
                _print("* left %s" % rest[0])
            elif cmd == "/room" and len(rest) == 1:
                joined = client.rooms()["joined"]
                if rest[0] not in joined:
                    _print("! not in %s (joined: %s)" % (rest[0], ", ".join(joined)))
                else:
                    state["room"] = rest[0]
                    _print("* current room: %s" % rest[0])
            elif cmd == "/pm" and len(rest) == 2:
                client.pm(rest[0], rest[1])
            elif cmd == "/rooms":
                r = client.rooms()
                for name, n in sorted(r["rooms"].items()):
                    mark = "*" if name in r["joined"] else " "
                    _print(" %s %-20s %d member(s)" % (mark, name, n))
            elif cmd == "/users":
                _print("online: " + ", ".join(client.users()))
            elif cmd == "/ping":
                t0 = time.time()
                client.ping()
                _print("* rtt %.2f ms" % ((time.time() - t0) * 1000))
            elif cmd == "/quit":
                break
            else:
                _print("! bad command; /help for usage")
        except ChatError as exc:
            _print("! " + str(exc))
        except (TimeoutError, OSError) as exc:
            _print("! connection problem: %s" % exc)
            if client.closed:
                break
    state["quitting"] = True
    client.quit()
    return 0


def main(argv=None):  # parse command-line arguments and start the CLI
    p = argparse.ArgumentParser(description="interactive chat client")
    p.add_argument("--host", default="10.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--user", help="log in automatically as this user")
    p.add_argument("--password")
    p.add_argument("--register", action="store_true",
                   help="register --user first (ignored if it already exists)")
    return run_cli(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
