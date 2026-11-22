"""Multi-client TCP chat server (hub-and-spoke relay).

Concurrency model: one reader thread per client plus a per-client outbox
queue drained by its own writer thread, so a slow receiver cannot stall a
room broadcast. All shared state (users, sessions, rooms) sits under one lock;
sends only enqueue, so the lock is never held across socket I/O.
"""
import argparse
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import signal
import socket
import sys
import tempfile
import threading
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chat.notifier import Notifier, DEFAULT_SOCK
from chat.protocol import LineReader, ProtocolError, FrameTooLarge, encode, decode

USER_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
ROOM_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MAX_TEXT = 8000
MAX_PASSWORD = 128
DEFAULT_ROOM = "general"
OUTBOX_MAX = 2000

log = logging.getLogger("server")


class UserStore:
    """Registered users with salted PBKDF2 hashes, persisted as JSON."""

    iterations = 100000

    def __init__(self, path):  # load the users file (if any) and set up the lock
        self.path = path
        self._lock = threading.Lock()
        self._users = {}
        if path and os.path.exists(path):
            with open(path) as f:
                self._users = json.load(f)

    def exists(self, user):  # True if this username is registered
        with self._lock:
            return user in self._users

    def add(self, user, password):  # register a user with a salted PBKDF2 hash; False if the name exists
        """Return False if the user already exists."""
        salt = os.urandom(16)
        # Hashing is deliberately slow, so do it outside the lock.
        digest = self._hash(password, salt, self.iterations)
        with self._lock:
            if user in self._users:
                return False
            self._users[user] = {"salt": salt.hex(), "hash": digest.hex(),
                                 "iterations": self.iterations, "created": time.time()}
            self._save()
        return True

    def check(self, user, password):  # True if the password matches the stored hash (constant-time compare)
        with self._lock:
            rec = self._users.get(user)
        if rec is None:
            return False
        digest = self._hash(password, bytes.fromhex(rec["salt"]), rec["iterations"])
        return hmac.compare_digest(digest.hex(), rec["hash"])

    @staticmethod
    def _hash(password, salt, iterations):  # PBKDF2-SHA256 of the password with the given salt and iteration count
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)

    def _save(self):  # write the users file atomically (temp file, then rename)
        if not self.path:
            return
        # Write-then-rename so a crash never leaves a half-written users file.
        d = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".users-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._users, f, indent=1)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


class Session:
    def __init__(self, server, sock, addr):  # one connected client: socket, address, user, rooms, outbox queue
        self.server = server
        self.sock = sock
        self.ip, self.port = addr[0], addr[1]
        self.user = None
        self.rooms = set()
        self.outbox = queue.Queue(maxsize=OUTBOX_MAX)
        self._closed = threading.Event()

    def __repr__(self):  # readable form like <alice 10.0.0.11:45708> for logs
        return "<%s %s:%d>" % (self.user or "anon", self.ip, self.port)

    def send(self, msg):  # put a message in this client's outbox; disconnect the client if it overflows
        if self._closed.is_set():
            return False
        try:
            self.outbox.put_nowait(encode(msg))
            return True
        except queue.Full:
            log.warning("%r outbox overflow, disconnecting", self)
            self.close(flush=False)
            return False

    def writer(self):  # writer thread: send queued messages to the socket until told to stop
        while True:
            data = self.outbox.get()
            if data is None:
                break
            try:
                self.sock.sendall(data)
            except OSError:
                break
        self._closed.set()
        self._shutdown()

    def close(self, flush=True):  # stop the session; flush=True lets queued replies go out first
        """flush=True lets queued replies (e.g. a final error) go out first;
        flush=False aborts at once (overflowing or dead client)."""
        if self._closed.is_set():
            return
        self._closed.set()
        if not flush:
            self._shutdown()
        # Wake the writer; if the outbox is full, discard to make room.
        while True:
            try:
                self.outbox.put_nowait(None)
                break
            except queue.Full:
                try:
                    self.outbox.get_nowait()
                except queue.Empty:
                    pass

    def _shutdown(self):  # shut down and close the socket
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class ChatServer:
    def __init__(self, host="0.0.0.0", port=5000, users_file="users.json",
                 controller="unix:" + DEFAULT_SOCK):  # set up the empty online / rooms / sessions tables, user store and notifier
        self.host, self.port = host, port
        self.store = UserStore(users_file)
        self.notifier = Notifier(controller)
        self.lock = threading.Lock()
        self.online = {}    # user -> Session
        self.rooms = {}     # room -> set of users
        self.sessions = set()
        self._listener = None
        self._stopping = threading.Event()

    # ---- lifecycle ----

    def bind(self):  # open the listening TCP socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(256)
        self.port = s.getsockname()[1]
        self._listener = s
        log.info("listening on %s:%d (controller events: %s)",
                 self.host, self.port, self.notifier.target)

    def serve_forever(self):  # accept loop: for each client start a writer thread and a reader thread
        if self._listener is None:
            self.bind()
        while not self._stopping.is_set():
            try:
                sock, addr = self._listener.accept()
            except OSError:
                if self._stopping.is_set():
                    break
                raise
            self._configure(sock)
            sess = Session(self, sock, addr)
            with self.lock:
                self.sessions.add(sess)
            threading.Thread(target=sess.writer, name="w-%d" % addr[1], daemon=True).start()
            threading.Thread(target=self._handle, args=(sess,), name="r-%d" % addr[1],
                             daemon=True).start()

    def start(self):  # bind and serve in a background thread (used by tests)
        """Bind and serve in a background thread (used by tests)."""
        self.bind()
        t = threading.Thread(target=self.serve_forever, name="accept", daemon=True)
        t.start()
        return t

    def stop(self):  # stop accepting and close every session
        self._stopping.set()
        if self._listener:
            try:
                self._listener.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._listener.close()
        with self.lock:
            sessions = list(self.sessions)
        for s in sessions:
            s.close(flush=False)

    @staticmethod
    def _configure(sock):  # set TCP options: no-delay, keepalive and a 20 s user timeout
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        # Detect dead peers (e.g. a client blocked by the controller) in ~25 s
        # instead of the 2 h kernel default.
        for opt, val in (("TCP_KEEPIDLE", 10), ("TCP_KEEPINTVL", 5), ("TCP_KEEPCNT", 3)):
            if hasattr(socket, opt):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
        # Keepalive only probes idle connections. If we have unacked data queued
        # (room messages to a client the network now drops), Linux otherwise
        # retransmits for ~15 min (tcp_retries2) before giving up.
        if hasattr(socket, "TCP_USER_TIMEOUT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, 20000)

    # ---- per-connection loop ----

    def _handle(self, sess):  # reader thread for one client: read bytes, split into lines, dispatch each
        reader = LineReader()
        try:
            while True:
                try:
                    data = sess.sock.recv(65536)
                except OSError:
                    break
                if not data:
                    break
                try:
                    frames = reader.feed(data)
                except FrameTooLarge as exc:
                    sess.send({"type": "error", "reason": str(exc)})
                    break
                for raw in frames:
                    try:
                        msg = decode(raw)
                    except ProtocolError as exc:
                        sess.send({"type": "error", "reason": str(exc)})
                        continue
                    if self._dispatch(sess, msg) is False:
                        return
        finally:
            self._disconnect(sess)

    def _dispatch(self, sess, msg):  # route a message to _on_<type>; requires login except for a few types
        kind = msg["type"]
        handler = getattr(self, "_on_" + kind, None)
        rid = msg.get("id")
        if handler is None:
            return sess.send(_err(rid, "unknown request type %r" % kind))
        if kind not in ("register", "login", "ping", "quit") and sess.user is None:
            return sess.send(_err(rid, "login required"))
        try:
            return handler(sess, msg, rid)
        except Exception:
            log.exception("handler %s failed for %r", kind, sess)
            return sess.send(_err(rid, "internal error"))

    def _disconnect(self, sess):  # client left: remove from online/rooms, tell the room, notify the controller
        with self.lock:
            self.sessions.discard(sess)
            user, rooms = sess.user, sorted(sess.rooms)
            if user and self.online.get(user) is sess:
                del self.online[user]
            notices = []
            for room in rooms:
                members = self.rooms.get(room)
                if members is None:
                    continue
                members.discard(user)
                if not members:
                    del self.rooms[room]
                else:
                    notices.append((room, [self.online[u] for u in members if u in self.online]))
            sess.rooms.clear()
        sess.close()
        if user:
            for room, targets in notices:
                for t in targets:
                    t.send({"type": "info", "room": room, "text": "%s left %s" % (user, room)})
            for room in rooms:
                self.notifier.emit("leave", user, sess.ip, sess.port, room)
            self.notifier.emit("logout", user, sess.ip, sess.port)
            log.info("%s disconnected (%s:%d)", user, sess.ip, sess.port)

    # ---- request handlers (return False to close the connection) ----

    def _on_register(self, sess, msg, rid):  # handle register: validate the name and password, create the account
        user, password = msg.get("user"), msg.get("password")
        if not isinstance(user, str) or not USER_RE.match(user):
            return sess.send(_err(rid, "invalid username (3-20 of A-Z a-z 0-9 _)"))
        if not isinstance(password, str) or not 4 <= len(password) <= MAX_PASSWORD:
            return sess.send(_err(rid, "password must be 4-%d characters" % MAX_PASSWORD))
        if not self.store.add(user, password):
            return sess.send(_err(rid, "user already exists"))
        log.info("registered %s", user)
        return sess.send(_ok(rid, user=user))

    def _on_login(self, sess, msg, rid):  # handle login: check the password, refuse duplicates, join 'general', notify controller
        user, password = msg.get("user"), msg.get("password")
        if sess.user is not None:
            return sess.send(_err(rid, "already logged in as %s" % sess.user))
        if not isinstance(user, str) or not isinstance(password, str) \
                or not self.store.check(user, password):
            return sess.send(_err(rid, "invalid username or password"))
        with self.lock:
            if user in self.online:
                return sess.send(_err(rid, "user already logged in elsewhere"))
            self.online[user] = sess
            sess.user = user
        log.info("%s logged in from %s:%d", user, sess.ip, sess.port)
        # Login before join so the controller learns the user's tier first.
        self.notifier.emit("login", user, sess.ip, sess.port)
        self._join(sess, DEFAULT_ROOM)
        return sess.send(_ok(rid, user=user, rooms=sorted(sess.rooms)))

    def _on_join(self, sess, msg, rid):  # handle /join: validate the room name and call _join
        room = msg.get("room")
        if not isinstance(room, str) or not ROOM_RE.match(room):
            return sess.send(_err(rid, "invalid room name (1-32 of A-Z a-z 0-9 _ -)"))
        if room in sess.rooms:
            return sess.send(_err(rid, "already in %s" % room))
        self._join(sess, room)
        return sess.send(_ok(rid, room=room))

    def _join(self, sess, room):  # add the user to a room, announce it, notify the controller
        with self.lock:
            members = self.rooms.setdefault(room, set())
            targets = [self.online[u] for u in members if u in self.online]
            members.add(sess.user)
            sess.rooms.add(room)
        for t in targets:
            t.send({"type": "info", "room": room, "text": "%s joined %s" % (sess.user, room)})
        self.notifier.emit("join", sess.user, sess.ip, sess.port, room)

    def _on_leave(self, sess, msg, rid):  # handle /leave: remove the user from a room, announce it, notify the controller
        room = msg.get("room")
        with self.lock:
            if room not in sess.rooms:
                return sess.send(_err(rid, "not in room %r" % room))
            sess.rooms.discard(room)
            members = self.rooms[room]
            members.discard(sess.user)
            if not members:
                del self.rooms[room]
            targets = [self.online[u] for u in members if u in self.online]
        for t in targets:
            t.send({"type": "info", "room": room, "text": "%s left %s" % (sess.user, room)})
        self.notifier.emit("leave", sess.user, sess.ip, sess.port, room)
        return sess.send(_ok(rid, room=room))

    def _on_msg(self, sess, msg, rid):  # handle a room message: copy it to every other member, reply with the delivered count
        room, text = msg.get("room"), msg.get("text")
        bad = _check_text(text)
        if bad:
            return sess.send(_err(rid, bad))
        with self.lock:
            if room not in sess.rooms:
                return sess.send(_err(rid, "not in room %r" % room))
            targets = [self.online[u] for u in self.rooms[room]
                       if u != sess.user and u in self.online]
        out = {"type": "msg", "room": room, "from": sess.user, "text": text,
               "ts": msg.get("ts", time.time()), "id": rid}
        for t in targets:
            t.send(out)
        return sess.send({"type": "ack", "id": rid, "delivered": len(targets)})

    def _on_pm(self, sess, msg, rid):  # handle a private message: copy to the one target user, or reply with an error
        to, text = msg.get("to"), msg.get("text")
        bad = _check_text(text)
        if bad:
            return sess.send(_err(rid, bad))
        if to == sess.user:
            return sess.send(_err(rid, "cannot message yourself"))
        with self.lock:
            target = self.online.get(to)
        if target is None:
            reason = "user %s is offline" if self.store.exists(str(to)) else "no such user %s"
            return sess.send(_err(rid, reason % to))
        target.send({"type": "pm", "from": sess.user, "text": text,
                     "ts": msg.get("ts", time.time()), "id": rid})
        return sess.send({"type": "ack", "id": rid, "delivered": 1})

    def _on_rooms(self, sess, msg, rid):  # reply with each room's member count and the rooms this user joined
        with self.lock:
            rooms = {r: len(m) for r, m in self.rooms.items()}
        return sess.send({"type": "rooms", "id": rid, "rooms": rooms,
                          "joined": sorted(sess.rooms)})

    def _on_users(self, sess, msg, rid):  # reply with the sorted list of online users
        with self.lock:
            users = sorted(self.online)
        return sess.send({"type": "users", "id": rid, "users": users})

    def _on_ping(self, sess, msg, rid):  # reply with a pong (used by /ping)
        return sess.send({"type": "pong", "id": rid, "ts": time.time()})

    def _on_quit(self, sess, msg, rid):  # acknowledge and close the connection
        sess.send(_ok(rid))
        return False


def _ok(rid, **fields):  # build an 'ok' reply
    fields.update(type="ok", id=rid)
    return fields


def _err(rid, reason):  # build an 'error' reply with a reason
    return {"type": "error", "id": rid, "reason": reason}


def _check_text(text):  # return an error text if the message text is empty or too long, else None
    if not isinstance(text, str) or not text:
        return "text must be a non-empty string"
    if len(text) > MAX_TEXT:
        return "text longer than %d characters" % MAX_TEXT
    return None


def main(argv=None):  # parse arguments, start the server, run until Ctrl-C / SIGTERM
    p = argparse.ArgumentParser(description="TCP chat server")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--users-file", default="users.json")
    p.add_argument("--controller", default="unix:" + DEFAULT_SOCK,
                   help="unix:PATH | http://host:port | none")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    srv = ChatServer(args.host, args.port, args.users_file, args.controller)
    srv.bind()
    signal.signal(signal.SIGTERM, lambda *_: srv.stop())
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()


if __name__ == "__main__":
    main()
