"""Server integration tests over loopback TCP (no Mininet needed)."""
import json
import os
import queue
import socket
import tempfile
import threading
import time
import unittest

from chat.client import ChatClient, ChatError
from chat.protocol import encode, decode, LineReader, MAX_FRAME
from chat.server import ChatServer, UserStore


class EventSink:
    """Stands in for the controller: collects notifier datagrams."""

    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(path)
        self.sock.settimeout(0.2)
        self.events = []

    def drain(self):
        while True:
            try:
                self.events.append(json.loads(self.sock.recv(65536)))
            except socket.timeout:
                return self.events


class ServerTestBase(unittest.TestCase):
    def setUp(self):
        UserStore.iterations = 1000          # keep tests fast
        self.tmp = tempfile.mkdtemp()
        self.users_file = os.path.join(self.tmp, "users.json")
        self.sock_path = os.path.join(self.tmp, "events.sock")
        self.sink = EventSink(self.sock_path)
        self.server = ChatServer("127.0.0.1", 0, self.users_file, "unix:" + self.sock_path)
        self.server.start()
        self.clients = []

    def tearDown(self):
        for c in self.clients:
            c.close()
        self.server.stop()
        self.sink.sock.close()

    def client(self, user=None, password="secret", register=True):
        inbox = queue.Queue()
        c = ChatClient("127.0.0.1", self.server.port, on_message=inbox.put, timeout=5)
        c.inbox = inbox
        c.connect()
        self.clients.append(c)
        if user:
            if register:
                c.register(user, password)
            c.login(user, password)
        return c

    def expect(self, c, kind, contains=None, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = c.inbox.get(timeout=max(0.0, deadline - time.time()))
            except queue.Empty:
                break
            if msg["type"] == kind and (contains is None or contains in msg.get("text", "")):
                return msg
        self.fail("no %s frame%s received" % (kind, " containing %r" % contains if contains else ""))

    def wait_for(self, pred, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return True
            time.sleep(0.01)
        return False


class RegistrationTest(ServerTestBase):
    def test_register_login(self):
        c = self.client()
        c.register("alice", "secret")
        reply = c.login("alice", "secret")
        self.assertEqual(reply["rooms"], ["general"])

    def test_password_hashed_and_persisted(self):
        self.client("alice", "secret")
        with open(self.users_file) as f:
            data = json.load(f)
        self.assertIn("alice", data)
        self.assertNotIn("secret", json.dumps(data))
        # A fresh store (server restart) still accepts the password.
        self.assertTrue(UserStore(self.users_file).check("alice", "secret"))

    def test_duplicate_registration(self):
        c = self.client()
        c.register("alice", "secret")
        with self.assertRaisesRegex(ChatError, "exists"):
            c.register("alice", "other")

    def test_bad_password(self):
        c = self.client()
        c.register("alice", "secret")
        with self.assertRaisesRegex(ChatError, "invalid"):
            c.login("alice", "wrong")

    def test_unknown_user(self):
        with self.assertRaisesRegex(ChatError, "invalid"):
            self.client().login("nobody", "secret")

    def test_invalid_usernames(self):
        c = self.client()
        for name in ("ab", "x" * 21, "bad name", "semi;colon", "", None):
            with self.assertRaises(ChatError, msg=repr(name)):
                c.request("register", user=name, password="secret")

    def test_short_password(self):
        with self.assertRaisesRegex(ChatError, "password"):
            self.client().register("alice", "abc")

    def test_duplicate_login_rejected(self):
        self.client("alice")
        with self.assertRaisesRegex(ChatError, "already logged in"):
            self.client().login("alice", "secret")

    def test_chat_requires_login(self):
        c = self.client()
        for kind, fields in (("join", {"room": "x"}), ("msg", {"room": "general", "text": "hi"}),
                             ("pm", {"to": "bob", "text": "hi"}), ("users", {})):
            with self.assertRaisesRegex(ChatError, "login required"):
                c.request(kind, **fields)

    def test_ping_without_login(self):
        self.assertEqual(self.client().ping()["type"], "pong")


class RoomTest(ServerTestBase):
    def test_room_broadcast(self):
        a, b, c = self.client("alice"), self.client("bob"), self.client("carol")
        for x in (a, b):
            x.join("lobby")
        ack = a.msg("lobby", "hello lobby")
        self.assertEqual(ack["type"], "ack")
        self.assertEqual(ack["delivered"], 1)
        got = self.expect(b, "msg")
        self.assertEqual((got["room"], got["from"], got["text"]), ("lobby", "alice", "hello lobby"))
        # carol is not in lobby and must not receive it; sender gets no echo.
        a.msg("general", "marker")
        self.assertEqual(self.expect(c, "msg")["text"], "marker")
        self.assertTrue(all(m["type"] != "msg" for m in list(a.inbox.queue)))

    def test_general_auto_join(self):
        a, b = self.client("alice"), self.client("bob")
        self.assertEqual(a.msg("general", "hi")["delivered"], 1)
        self.assertEqual(self.expect(b, "msg")["text"], "hi")

    def test_msg_to_unjoined_room(self):
        a = self.client("alice")
        with self.assertRaisesRegex(ChatError, "not in room"):
            a.msg("secret", "hi")

    def test_leave(self):
        a, b = self.client("alice"), self.client("bob")
        a.join("lobby")
        b.join("lobby")
        b.leave("lobby")
        self.assertEqual(a.msg("lobby", "anyone?")["delivered"], 0)
        with self.assertRaises(ChatError):
            b.leave("lobby")

    def test_rooms_listing_and_cleanup(self):
        a = self.client("alice")
        a.join("lobby")
        r = a.rooms()
        self.assertEqual(r["rooms"], {"general": 1, "lobby": 1})
        self.assertEqual(r["joined"], ["general", "lobby"])
        a.leave("lobby")
        self.assertNotIn("lobby", a.rooms()["rooms"])   # empty rooms are removed

    def test_invalid_room_and_text(self):
        a = self.client("alice")
        for room in ("", "x" * 33, "bad room", "a/b"):
            with self.assertRaises(ChatError):
                a.join(room)
        with self.assertRaises(ChatError):
            a.msg("general", "")
        with self.assertRaisesRegex(ChatError, "longer"):
            a.msg("general", "x" * 8001)

    def test_join_info_notice(self):
        a, b = self.client("alice"), self.client("bob")
        self.assertIn("bob joined general", self.expect(a, "info")["text"])


class PrivateMessageTest(ServerTestBase):
    def test_pm(self):
        a, b, c = self.client("alice"), self.client("bob"), self.client("carol")
        self.assertEqual(a.pm("bob", "psst")["type"], "ack")
        got = self.expect(b, "pm")
        self.assertEqual((got["from"], got["text"]), ("alice", "psst"))
        self.assertTrue(all(m["type"] != "pm" for m in list(c.inbox.queue)))

    def test_pm_offline_and_unknown(self):
        self.client("bob").quit()
        a = self.client("alice")
        with self.assertRaisesRegex(ChatError, "offline"):
            a.pm("bob", "hi")
        with self.assertRaisesRegex(ChatError, "no such user"):
            a.pm("ghost", "hi")

    def test_pm_self(self):
        with self.assertRaises(ChatError):
            self.client("alice").pm("alice", "hi")


class ConnectionTest(ServerTestBase):
    def test_disconnect_cleanup(self):
        a, b = self.client("alice"), self.client("bob")
        b.join("lobby")
        b.close()                                    # abrupt, no quit
        self.assertTrue(self.wait_for(lambda: "bob" not in self.server.online))
        self.assertEqual(a.users(), ["alice"])
        self.assertNotIn("lobby", a.rooms()["rooms"])
        self.expect(a, "info", contains="bob left")
        # bob can log in again after the stale session is gone.
        self.client("bob", register=False)

    def test_quit(self):
        a = self.client("alice")
        a.quit()
        self.assertTrue(self.wait_for(lambda: not self.server.online))

    def test_malformed_frame_keeps_connection(self):
        s = socket.create_connection(("127.0.0.1", self.server.port))
        s.sendall(b"garbage\n" + encode({"type": "ping", "id": 9}))
        frames = self._read_frames(s, 2)
        self.assertEqual([f["type"] for f in frames], ["error", "pong"])
        s.close()

    def test_unknown_type(self):
        with self.assertRaisesRegex(ChatError, "unknown"):
            self.client().request("dance")

    def test_oversized_frame_disconnects(self):
        s = socket.create_connection(("127.0.0.1", self.server.port))
        s.sendall(b"x" * (MAX_FRAME + 10))
        frames = self._read_frames(s, 1)
        self.assertEqual(frames[0]["type"], "error")
        s.settimeout(2)
        self.assertEqual(s.recv(100), b"")           # server closed the connection
        s.close()

    def test_concurrent_clients(self):
        n = 30
        errors = []
        receiver = self.client("watcher")
        receiver.join("busy")

        def worker(i):
            try:
                c = self.client("u%03d" % i)
                c.join("busy")
                for k in range(5):
                    c.msg("busy", "m%d" % k)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.server.online), n + 1)
        self.assertTrue(self.wait_for(
            lambda: sum(1 for m in list(receiver.inbox.queue) if m["type"] == "msg") == n * 5))

    def _read_frames(self, s, n):
        s.settimeout(2)
        r, out = LineReader(), []
        while len(out) < n:
            data = s.recv(65536)
            if not data:
                break
            out.extend(decode(f) for f in r.feed(data))
        return out


class NotifierTest(ServerTestBase):
    def test_events_reach_controller(self):
        a = self.client("alice")
        a.join("lobby")
        a.leave("lobby")
        port = a.local_addr[1]
        a.quit()
        self.assertTrue(self.wait_for(lambda: not self.server.online))
        self.server.notifier.flush()
        evs = [(e["event"], e["room"]) for e in self.sink.drain()]
        self.assertEqual(evs, [("login", None), ("join", "general"), ("join", "lobby"),
                               ("leave", "lobby"), ("leave", "general"), ("logout", None)])
        for e in self.sink.events:
            self.assertEqual((e["user"], e["client_ip"], e["client_port"]),
                             ("alice", "127.0.0.1", port))

    def test_controller_down_does_not_block_chat(self):
        self.sink.sock.close()
        os.unlink(self.sock_path)                    # nobody listening
        a, b = self.client("alice"), self.client("bob")
        self.assertEqual(a.msg("general", "still works")["delivered"], 1)


if __name__ == "__main__":
    unittest.main()
