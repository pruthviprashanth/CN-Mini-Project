"""Ryu app: classifies chat traffic and re-programs switches on app events.

Run:  ryu-manager sdn/controller.py
Env:  CHAT_POLICY (policy JSON), CHAT_MODE (sdn|static), CHAT_MONITOR (1|0),
      CHAT_EVENT_SOCK (Unix datagram socket the chat server reports to).

Forwarding is reactive L2 learning, but flows are installed only at the IP
level (5-tuple for chat, src/dst/proto/port for bulk), never on eth_dst, so
every new chat connection reaches the controller and can be classified.
"""
import collections
import json
import os
import socket
import sys
import time

from ryu.app.wsgi import ControllerBase, Response, WSGIApplication, route
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (CONFIG_DISPATCHER, DEAD_DISPATCHER,
                                    MAIN_DISPATCHER, set_ev_cls)
from ryu.lib import hub
from ryu.lib.packet import ethernet, ether_types, ipv4, packet, tcp, udp
from ryu.ofproto import ofproto_v1_3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sdn.policy import DROP, QUEUES, Connection, Policy, observe_rate, resolve  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_POLICY = os.path.join(HERE, "policy.json")
DEFAULT_EVENT_SOCK = "/tmp/chat_sdn_events.sock"

PRIO_MISS, PRIO_IP, PRIO_L4, PRIO_CHAT = 0, 40, 50, 100
COOKIE_CHAT, COOKIE_L4, COOKIE_IP = 0xC0FFEE01, 0xC0FFEE02, 0xC0FFEE03
IDLE_TIMEOUT = 60
CONN_GC_AFTER = 120


class ChatSDNController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {"wsgi": WSGIApplication}

    def __init__(self, *args, **kwargs):  # load policy and env settings, create the tables, start the event listener and monitor
        start_threads = kwargs.pop("start_threads", True)
        wsgi = kwargs.get("wsgi")
        super(ChatSDNController, self).__init__(*args, **kwargs)
        self.policy_path = os.environ.get("CHAT_POLICY", DEFAULT_POLICY)
        self.policy = Policy.load(self.policy_path)
        self.mode = os.environ.get("CHAT_MODE", "sdn")
        self.monitor = os.environ.get("CHAT_MONITOR", "1") not in ("0", "false", "no")
        self.event_sock = os.environ.get("CHAT_EVENT_SOCK", DEFAULT_EVENT_SOCK)
        self.blocked = set(self.policy.blocked_users)
        self.datapaths = {}                 # dpid -> datapath
        self.mac_to_port = {}               # dpid -> {mac: port}
        self.conns = {}                     # (client ip, client port) -> Connection
        self._prev_bytes = {}               # (dpid, key, dir) -> (byte count, time)
        self._round = {}                    # (dpid, key, dir) -> bps this poll round
        self._last_poll = None
        self.history = collections.deque(maxlen=300)       # app events
        self.transitions = collections.deque(maxlen=300)   # class/flood/block/mode changes
        self.stats = collections.Counter()
        if wsgi is not None:
            wsgi.register(ChatRestController, {"app": self})
        if start_threads:
            # Bind here, not in the green thread, so a failure stops startup loudly.
            sock = self._bind_event_socket()
            hub.spawn(self._event_listener, sock)
            hub.spawn(self._monitor_loop)
        self.logger.info("chat controller: mode=%s monitor=%s policy=%s events=%s",
                         self.mode, self.monitor, self.policy_path, self.event_sock)

    # ------------------------------------------------------------------ switches

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features(self, ev):  # a switch connected: remember it and reset its flow table
        dp = ev.msg.datapath
        self.datapaths[dp.id] = dp
        self.mac_to_port.setdefault(dp.id, {})
        self._reset_tables(dp)
        self.logger.info("switch %016x connected", dp.id)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change(self, ev):  # a switch disconnected: forget it
        dp = ev.datapath
        if ev.state == DEAD_DISPATCHER and dp.id in self.datapaths:
            del self.datapaths[dp.id]
            self.logger.info("switch %016x disconnected", dp.id)

    def _reset_tables(self, dp):  # delete every flow on a switch, then add the table-miss rule
        """Delete every flow, then re-add the table-miss entry."""
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(parser.OFPFlowMod(dp, command=ofp.OFPFC_DELETE, table_id=ofp.OFPTT_ALL,
                                      out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                                      match=parser.OFPMatch()))
        dp.send_msg(parser.OFPBarrierRequest(dp))
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self._add_flow(dp, PRIO_MISS, {}, actions, idle=0, cookie=0)

    def _add_flow(self, dp, priority, match, actions, idle=IDLE_TIMEOUT, cookie=0):  # send one flow-mod (add a rule) to a switch; empty actions means drop
        ofp, parser = dp.ofproto, dp.ofproto_parser
        # An empty instruction list means drop.
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)] if actions else []
        dp.send_msg(parser.OFPFlowMod(dp, cookie=cookie, priority=priority, idle_timeout=idle,
                                      match=parser.OFPMatch(**match), instructions=inst))
        self.stats["flow_mods"] += 1

    def _actions(self, dp, klass, out_port):  # build the actions: set_queue + output, or none for DROP
        parser = dp.ofproto_parser
        if klass == DROP:
            return []
        out = [parser.OFPActionOutput(out_port)]
        if out_port == dp.ofproto.OFPP_FLOOD:
            return out
        return [parser.OFPActionSetQueue(QUEUES[klass])] + out

    def _packet_out(self, dp, msg, in_port, actions):  # tell a switch to forward one packet now
        parser = dp.ofproto_parser
        dp.send_msg(parser.OFPPacketOut(dp, buffer_id=dp.ofproto.OFP_NO_BUFFER, in_port=in_port,
                                        actions=actions, data=msg.data))

    # ------------------------------------------------------------------ packet-in

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in(self, ev):  # an unmatched packet arrived: learn the MAC, forward ARP, install chat/bulk/IP rules
        msg = ev.msg
        dp = msg.datapath
        ofp = dp.ofproto
        in_port = msg.match["in_port"]
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype in (ether_types.ETH_TYPE_LLDP, ether_types.ETH_TYPE_IPV6):
            return
        self.stats["packet_in"] += 1
        table = self.mac_to_port.setdefault(dp.id, {})
        table[eth.src] = in_port
        out_port = table.get(eth.dst, ofp.OFPP_FLOOD)

        ip = pkt.get_protocol(ipv4.ipv4)
        # ARP and other non-IP: forward only. Installing an eth_dst flow here
        # would also match later TCP packets and hide them from the controller.
        if ip is None or out_port == ofp.OFPP_FLOOD:
            self._packet_out(dp, msg, in_port, [dp.ofproto_parser.OFPActionOutput(out_port)])
            return

        t = pkt.get_protocol(tcp.tcp)
        u = pkt.get_protocol(udp.udp)
        cp = self.policy.chat_port
        if t is not None and cp in (t.src_port, t.dst_port):
            actions = self._chat_packet(dp, ip, t, out_port)
        elif t is not None or u is not None:
            l4 = t or u
            proto = "tcp" if t is not None else "udp"
            match = {"eth_type": 0x0800, "ip_proto": ip.proto, "ipv4_src": ip.src,
                     "ipv4_dst": ip.dst, proto + "_dst": l4.dst_port}
            klass = "NORMAL" if self.mode == "static" else self.policy.bulk_class
            actions = self._actions(dp, klass, out_port)
            self._add_flow(dp, PRIO_L4, match, actions, cookie=COOKIE_L4)
        else:
            match = {"eth_type": 0x0800, "ip_proto": ip.proto, "ipv4_src": ip.src,
                     "ipv4_dst": ip.dst}
            actions = self._actions(dp, "NORMAL", out_port)
            self._add_flow(dp, PRIO_IP, match, actions, cookie=COOKIE_IP)
        if actions:
            self._packet_out(dp, msg, in_port, actions)

    def _chat_packet(self, dp, ip, t, out_port):  # chat packet: find its connection, resolve its class, install the priority-100 rule
        if t.dst_port == self.policy.chat_port:
            key, direction = (ip.src, t.src_port), "c2s"
        else:
            key, direction = (ip.dst, t.dst_port), "s2c"
        conn = self._conn(key)
        match = {"eth_type": 0x0800, "ip_proto": 6, "ipv4_src": ip.src, "ipv4_dst": ip.dst,
                 "tcp_src": t.src_port, "tcp_dst": t.dst_port}
        conn.flows[(dp.id, direction)] = (match, out_port)
        klass = resolve(self.mode, self.policy, conn, self.blocked)
        if conn.klass is None:
            self._log("new", conn, "connection classified %s" % klass)
        conn.klass = klass
        actions = self._actions(dp, klass, out_port)
        self._add_flow(dp, PRIO_CHAT, match, actions, cookie=COOKIE_CHAT)
        return actions

    def _conn(self, key):  # get (or create) the Connection record for (ip, port)
        conn = self.conns.get(key)
        if conn is None:
            conn = self.conns[key] = Connection(*key)
        return conn

    # ------------------------------------------------------------------ re-classification

    def reapply(self, conn, reason):  # recompute a connection's class and re-send all its flow rules
        """Re-send flow-mods for every flow of this connection with its new class."""
        klass = resolve(self.mode, self.policy, conn, self.blocked)
        if klass != conn.klass:
            self._log("class", conn, "%s -> %s (%s)" % (conn.klass, klass, reason))
        conn.klass = klass
        for (dpid, _direction), (match, out_port) in conn.flows.items():
            dp = self.datapaths.get(dpid)
            if dp is not None:
                self._add_flow(dp, PRIO_CHAT, match, self._actions(dp, klass, out_port),
                               cookie=COOKIE_CHAT)

    def handle_event(self, ev):  # apply a chat-server event (login/logout/join/leave), then reapply
        """Apply one chat-server event: login/logout/join/leave."""
        kind = ev.get("event")
        key = (ev.get("client_ip"), int(ev.get("client_port", 0)))
        self.stats["events"] += 1
        # Events may arrive before the connection's first packet-in; the record
        # created here is what that packet-in will be classified with.
        conn = self._conn(key)
        user, room = ev.get("user"), ev.get("room")
        if kind == "login":
            conn.user = conn.last_user = user
        elif kind == "join" and room:
            conn.rooms.add(room)
        elif kind == "leave":
            conn.rooms.discard(room)
        elif kind == "logout":
            conn.user = None
            conn.rooms.clear()
            conn.closed_at = time.time()
        else:
            self.logger.warning("unknown event %r", ev)
            return
        self.history.append({"ts": time.time(), "kind": "event", "event": kind,
                             "conn": conn.label(), "room": room})
        self.reapply(conn, "%s%s" % (kind, " " + room if room else ""))

    # ------------------------------------------------------------------ admin actions

    def block(self, user):  # block a user: their connections become drop rules
        self.blocked.add(user)
        n = 0
        for conn in list(self.conns.values()):
            if conn.last_user == user:
                self.reapply(conn, "blocked by admin")
                n += 1
        self._log("block", None, "user %s blocked (%d connection(s))" % (user, n))
        return n

    def unblock(self, user):  # unblock a user and restore their rules
        self.blocked.discard(user)
        n = 0
        for conn in list(self.conns.values()):
            if conn.last_user == user:
                self.reapply(conn, "unblocked by admin")
                n += 1
        self._log("block", None, "user %s unblocked (%d connection(s))" % (user, n))
        return n

    def set_mode(self, mode=None, monitor=None, bulk_class=None):  # switch static/sdn mode, the monitor and/or the bulk class, flush all flows
        if bulk_class is not None:
            if bulk_class not in QUEUES:
                raise ValueError("bulk_class must be one of %s" % ", ".join(QUEUES))
        if mode is not None:
            if mode not in ("sdn", "static"):
                raise ValueError("mode must be 'sdn' or 'static'")
            self.mode = mode
        if bulk_class is not None:
            self.policy.bulk_class = bulk_class      # lasts until the next policy reload
        if monitor is not None:
            self.monitor = bool(monitor)
        # Start from a clean slate: every flow is re-learned under the new mode.
        for dp in self.datapaths.values():
            self._reset_tables(dp)
        for conn in self.conns.values():
            conn.flows.clear()
            conn.klass = None
            conn.override, conn.over, conn.under = None, 0, 0
        self._prev_bytes.clear()
        self._round.clear()
        self._log("mode", None, "mode=%s monitor=%s (all flows flushed)" % (self.mode, self.monitor))

    def reload_policy(self):  # re-read policy.json and reclassify every connection
        self.policy = Policy.load(self.policy_path)
        self.blocked |= self.policy.blocked_users
        for conn in self.conns.values():
            self.reapply(conn, "policy reload")
        self._log("policy", None, "policy reloaded from %s" % self.policy_path)

    # ------------------------------------------------------------------ monitor

    def _monitor_loop(self):  # every poll_interval seconds run monitor_tick, forever
        while True:
            hub.sleep(self.policy.poll_interval)
            try:
                self.monitor_tick()
            except Exception:
                self.logger.exception("monitor tick failed")

    def monitor_tick(self):  # judge last round's rates (demote/restore flooders), then request new stats
        """Evaluate the rates gathered since the last tick, then poll again."""
        rates = {}
        for (dpid, key, direction), bps in self._round.items():
            r = rates.setdefault(key, {"c2s": 0.0, "s2c": 0.0})
            r[direction] = max(r[direction], bps)    # same flow seen at each switch
        self._round = {}
        for key, r in rates.items():
            conn = self.conns.get(key)
            if conn is None:
                continue
            conn.rate_s2c = r["s2c"]
            if not (self.monitor and self.mode == "sdn"):
                conn.rate_c2s = r["c2s"]
                continue
            action = observe_rate(self.policy, conn, r["c2s"])
            if action == "demote":
                self._log("flood", conn, "rate %.2f Mbps > %.2f Mbps for %d polls: demoted to LOW"
                          % (r["c2s"] / 1e6, self.policy.flood_bps / 1e6, self.policy.flood_intervals))
                self.reapply(conn, "flood demotion")
            elif action == "restore":
                self._log("flood", conn, "rate %.2f Mbps back under %.2f Mbps: restored"
                          % (r["c2s"] / 1e6, self.policy.flood_bps / 2e6))
                self.reapply(conn, "flood recovered")
        self._gc()
        self._last_poll = time.time()
        for dp in list(self.datapaths.values()):
            parser = dp.ofproto_parser
            dp.send_msg(parser.OFPFlowStatsRequest(dp, table_id=0, cookie=COOKIE_CHAT,
                                                   cookie_mask=0xFFFFFFFFFFFFFFFF,
                                                   match=parser.OFPMatch()))

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply(self, ev):  # switch replied with byte counters: compute each connection's rate in bits/s
        dpid = ev.msg.datapath.id
        now = time.time()
        for stat in ev.msg.body:
            if stat.priority != PRIO_CHAT:
                continue
            m = dict(stat.match.items())
            cp = self.policy.chat_port
            if m.get("tcp_dst") == cp:
                key, direction = (m["ipv4_src"], m["tcp_src"]), "c2s"
            elif m.get("tcp_src") == cp:
                key, direction = (m["ipv4_dst"], m["tcp_dst"]), "s2c"
            else:
                continue
            k = (dpid, key, direction)
            prev = self._prev_bytes.get(k)
            self._prev_bytes[k] = (stat.byte_count, now)
            if prev is None or now - prev[1] < 0.05:
                continue
            delta = stat.byte_count - prev[0]
            if delta < 0:              # flow was re-added and its counters reset
                delta = stat.byte_count
            self._round[k] = delta * 8.0 / (now - prev[1])

    def _gc(self):  # forget connections that have been closed for over 120 s
        now = time.time()
        for key, conn in list(self.conns.items()):
            if conn.closed_at and now - conn.closed_at > CONN_GC_AFTER:
                del self.conns[key]
                for k in [k for k in self._prev_bytes if k[1] == key]:
                    del self._prev_bytes[k]

    # ------------------------------------------------------------------ events socket

    def _bind_event_socket(self):  # create the Unix socket for chat events; stop with the fix if a stale one blocks it
        try:
            os.unlink(self.event_sock)
        except FileNotFoundError:
            pass
        except OSError as exc:
            # Typically a socket left by an earlier run as root: /tmp is sticky,
            # so another user cannot remove it.
            raise SystemExit("cannot remove stale event socket %s (%s); run: sudo rm -f %s"
                             % (self.event_sock, exc.strerror, self.event_sock))
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.bind(self.event_sock)
        os.chmod(self.event_sock, 0o777)    # the chat server may run as another user
        self.logger.info("listening for chat events on %s", self.event_sock)
        return sock

    def _event_listener(self, sock):  # loop: receive JSON events from the socket and pass them to handle_event
        while True:
            data = sock.recv(65536)          # cooperative: hub.patch() made sockets green
            try:
                self.handle_event(json.loads(data.decode("utf-8")))
            except Exception:
                self.logger.exception("bad event %r", data[:200])

    # ------------------------------------------------------------------ state / logging

    def _log(self, kind, conn, text):  # log a [KIND] line and keep it in the transitions list
        line = "%s %s" % (conn.label(), text) if conn else text
        self.logger.info("[%s] %s", kind.upper(), line)
        self.transitions.append({"ts": time.time(), "kind": kind, "text": line})

    def state(self):  # build the status report served by /chat/state
        conns = []
        for conn in sorted(self.conns.values(), key=lambda c: c.created):
            conns.append({
                "client": "%s:%d" % conn.key, "user": conn.user, "last_user": conn.last_user,
                "rooms": sorted(conn.rooms), "class": conn.klass,
                "queue": QUEUES.get(conn.klass), "override": conn.override,
                "rate_c2s_bps": round(conn.rate_c2s), "rate_s2c_bps": round(conn.rate_s2c),
                "closed": conn.closed_at is not None,
                "flows": [{"dpid": dpid, "dir": d, "out_port": port, "match": match}
                          for (dpid, d), (match, port) in sorted(conn.flows.items())],
            })
        return {"mode": self.mode, "monitor": self.monitor, "blocked": sorted(self.blocked),
                "datapaths": sorted(self.datapaths), "connections": conns,
                "stats": dict(self.stats), "history": list(self.history)[-50:],
                "transitions": list(self.transitions)}


class ChatRestController(ControllerBase):
    def __init__(self, req, link, data, **config):  # REST controller: keep a reference to the app
        super(ChatRestController, self).__init__(req, link, data, **config)
        self.app = data["app"]

    @staticmethod
    def _json(obj, status=200):  # wrap an object as a JSON web response
        return Response(status=status, content_type="application/json", charset="utf-8",
                        body=json.dumps(obj, indent=1).encode("utf-8"))

    @staticmethod
    def _body(req):  # parse the request body as JSON, or None if invalid
        try:
            return json.loads(req.body.decode("utf-8") or "{}")
        except ValueError:
            return None

    @route("chat", "/health", methods=["GET"])
    def health(self, req, **kwargs):  # GET /health: mode, monitor, connected switches
        return self._json({"ok": True, "mode": self.app.mode, "monitor": self.app.monitor,
                           "bulk_class": self.app.policy.bulk_class, "datapaths": sorted(self.app.datapaths)})

    @route("chat", "/chat/state", methods=["GET"])
    def chat_state(self, req, **kwargs):  # GET /chat/state: full controller state
        return self._json(self.app.state())

    @route("chat", "/chat/event", methods=["POST"])
    def chat_event(self, req, **kwargs):  # POST /chat/event: accept a chat event over HTTP
        ev = self._body(req)
        if not isinstance(ev, dict) or "event" not in ev:
            return self._json({"error": "expected an event object"}, 400)
        self.app.handle_event(ev)
        return self._json({"ok": True})

    @route("chat", "/mode", methods=["POST"])
    def mode(self, req, **kwargs):  # POST /mode: switch static/sdn and the monitor
        body = self._body(req)
        if not isinstance(body, dict):
            return self._json({"error": "bad JSON"}, 400)
        try:
            self.app.set_mode(body.get("mode"), body.get("monitor"), body.get("bulk_class"))
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"ok": True, "mode": self.app.mode, "monitor": self.app.monitor,
                           "bulk_class": self.app.policy.bulk_class})

    @route("chat", "/policy/block", methods=["POST"])
    def block(self, req, **kwargs):  # POST /policy/block: block a user
        body = self._body(req)
        if not isinstance(body, dict) or not body.get("user"):
            return self._json({"error": "expected {\"user\": ...}"}, 400)
        return self._json({"ok": True, "connections": self.app.block(body["user"])})

    @route("chat", "/policy/unblock", methods=["POST"])
    def unblock(self, req, **kwargs):  # POST /policy/unblock: unblock a user
        body = self._body(req)
        if not isinstance(body, dict) or not body.get("user"):
            return self._json({"error": "expected {\"user\": ...}"}, 400)
        return self._json({"ok": True, "connections": self.app.unblock(body["user"])})

    @route("chat", "/policy/reload", methods=["POST"])
    def reload(self, req, **kwargs):  # POST /policy/reload: re-read policy.json
        try:
            self.app.reload_policy()
        except (OSError, ValueError) as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"ok": True})
