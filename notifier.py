"""Best-effort reporting of application events to the SDN controller.

The chat path must never wait on the controller, so events go through a
bounded queue drained by a background thread, and every error is swallowed.

Default transport is a Unix datagram socket: Mininet hosts live in separate
network namespaces and cannot reach the controller's 127.0.0.1, but they share
the filesystem, so a socket path in /tmp is reachable from every host.
"""
import json
import logging
import queue
import socket
import threading
import time
import urllib.request

DEFAULT_SOCK = "/tmp/chat_sdn_events.sock"
EVENTS = ("login", "logout", "join", "leave")

log = logging.getLogger("notifier")


class Notifier:
    def __init__(self, target="unix:" + DEFAULT_SOCK, maxsize=10000):  # pick the transport (unix socket / http / none) and start the sender thread
        """target: 'unix:PATH', 'http://host:port[/chat/event]' or 'none'."""
        self.target = target
        self._queue = queue.Queue(maxsize=maxsize)
        self._send = None
        self.sent = 0
        self.failed = 0
        self.dropped = 0
        if target and target != "none":
            if target.startswith("unix:"):
                self._path = target[len("unix:"):]
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                self._send = self._send_unix
            elif target.startswith("http://"):
                url = target.rstrip("/")
                self._url = url if url.endswith("/chat/event") else url + "/chat/event"
                self._send = self._send_http
            else:
                raise ValueError("controller target must be unix:PATH, http://... or none")
            threading.Thread(target=self._run, name="notifier", daemon=True).start()

    def emit(self, event, user, client_ip, client_port, room=None):  # queue one app event (login, join, ...) for the controller; never blocks the server
        if self._send is None:
            return
        ev = {"event": event, "user": user, "room": room,
              "client_ip": client_ip, "client_port": client_port, "ts": time.time()}
        try:
            self._queue.put_nowait(ev)
        except queue.Full:
            self.dropped += 1

    def flush(self, timeout=2.0):  # wait until queued events have been handed over (used by tests)
        """Wait until queued events were handed to the transport (for tests)."""
        deadline = time.time() + timeout
        while self._queue.unfinished_tasks and time.time() < deadline:
            time.sleep(0.01)

    def _run(self):  # sender thread: send queued events, count failures, never crash
        while True:
            ev = self._queue.get()
            try:
                self._send(json.dumps(ev).encode("utf-8"))
                self.sent += 1
            except Exception as exc:  # controller down or not listening yet
                self.failed += 1
                log.debug("event %s not delivered: %s", ev["event"], exc)
            finally:
                self._queue.task_done()

    def _send_unix(self, data):  # send the event as a datagram on the Unix socket file
        self._sock.sendto(data, self._path)

    def _send_http(self, data):  # send the event as an HTTP POST to the controller's REST API
        req = urllib.request.Request(self._url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=1.0).close()
