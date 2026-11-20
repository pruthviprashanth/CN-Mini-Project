"""Controller logic against a stub datapath that records every OpenFlow message.

Needs Ryu, so run with the Ryu venv:  ~/ryu-venv/bin/python -m unittest tests.test_controller
(skipped under the system Python).
"""
import json
import os
import tempfile
import types
import unittest
from unittest import mock

try:
    from ryu.lib.packet import arp, ethernet, icmp, ipv4, packet, tcp
    from ryu.ofproto import ofproto_v1_3 as ofp
    from ryu.ofproto import ofproto_v1_3_parser as parser
    HAVE_RYU = True
except ImportError:
    HAVE_RYU = False

SRV_MAC, C1_MAC = "00:00:00:00:00:01", "00:00:00:00:00:0b"
SRV_IP, C1_IP = "10.0.0.1", "10.0.0.11"
POLICY = {
    "user_tiers": {"vip_*": "HIGH", "guest_*": "LOW"},
    "room_classes": {"announcements": "HIGH"},
    "monitor": {"poll_interval": 1, "flood_bps": 1000000, "flood_intervals": 2,
                "recover_intervals": 2},
}


class StubDP:
    def __init__(self, dpid):
        self.id = dpid
        self.ofproto = ofp
        self.ofproto_parser = parser
        self.sent = []

    def send_msg(self, msg):
        self.sent.append(msg)

    def take(self, cls=None):
        out = [m for m in self.sent if cls is None or isinstance(m, cls)]
        self.sent = []
        return out


def eth_ip(src_mac, dst_mac, src_ip, dst_ip, l4):
    p = packet.Packet()
    p.add_protocol(ethernet.ethernet(dst=dst_mac, src=src_mac, ethertype=0x0800))
    proto = 6 if isinstance(l4, tcp.tcp) else 1
    p.add_protocol(ipv4.ipv4(src=src_ip, dst=dst_ip, proto=proto))
    p.add_protocol(l4)
    p.serialize()
    return p.data


def flow_info(fm):
    """(priority, match dict, queue or None, out port or None, is_drop)."""
    acts = fm.instructions[0].actions if fm.instructions else []
    q = [a.queue_id for a in acts if isinstance(a, parser.OFPActionSetQueue)]
    o = [a.port for a in acts if isinstance(a, parser.OFPActionOutput)]
    return fm.priority, dict(fm.match.items()), (q or [None])[0], (o or [None])[0], not acts


@unittest.skipUnless(HAVE_RYU, "Ryu not installed (use ~/ryu-venv/bin/python)")
class ControllerTest(unittest.TestCase):
    def setUp(self):
        from sdn import controller
        self.mod = controller
        fd, self.policy_path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(POLICY, f)
        env = {"CHAT_POLICY": self.policy_path, "CHAT_MODE": "sdn", "CHAT_MONITOR": "1"}
        with mock.patch.dict(os.environ, env):
            self.app = controller.ChatSDNController(start_threads=False)
        self.s1 = StubDP(1)
        self.app.switch_features(types.SimpleNamespace(msg=types.SimpleNamespace(datapath=self.s1)))
        self.s1.take()
        # Teach s1 where the server and client are (server behind port 6, c1 on port 1).
        self.app.mac_to_port[1] = {SRV_MAC: 6, C1_MAC: 1}

    def tearDown(self):
        os.unlink(self.policy_path)

    def packet_in(self, data, in_port, dp=None):
        dp = dp or self.s1
        msg = parser.OFPPacketIn(dp, buffer_id=ofp.OFP_NO_BUFFER, total_len=len(data), reason=0,
                                 table_id=0, cookie=0, match=parser.OFPMatch(in_port=in_port),
                                 data=data)
        self.app.packet_in(types.SimpleNamespace(msg=msg))

    def chat_c2s(self, port=40000):
        self.packet_in(eth_ip(C1_MAC, SRV_MAC, C1_IP, SRV_IP,
                              tcp.tcp(src_port=port, dst_port=5000, bits=tcp.TCP_SYN)), 1)

    def chat_s2c(self, port=40000):
        self.packet_in(eth_ip(SRV_MAC, C1_MAC, SRV_IP, C1_IP,
                              tcp.tcp(src_port=5000, dst_port=port, bits=tcp.TCP_SYN | tcp.TCP_ACK)), 6)

    def event(self, kind, user="alice", room=None, port=40000):
        self.app.handle_event({"event": kind, "user": user, "room": room,
                               "client_ip": C1_IP, "client_port": port})

    def flows(self):
        return [flow_info(m) for m in self.s1.take(parser.OFPFlowMod)]

    # --- switch setup

    def test_switch_connect_resets_and_installs_table_miss(self):
        dp = StubDP(9)
        self.app.switch_features(types.SimpleNamespace(msg=types.SimpleNamespace(datapath=dp)))
        mods = dp.take(parser.OFPFlowMod)
        self.assertEqual(mods[0].command, ofp.OFPFC_DELETE)
        prio, match, _, out, _ = flow_info(mods[1])
        self.assertEqual((prio, match, out), (0, {}, ofp.OFPP_CONTROLLER))

    # --- forwarding without flows

    def test_unknown_destination_floods_without_flow(self):
        self.packet_in(eth_ip(C1_MAC, "00:00:00:00:00:99", C1_IP, "10.0.0.99",
                              tcp.tcp(src_port=1, dst_port=5000)), 1)
        sent = self.s1.take()
        self.assertEqual([m for m in sent if isinstance(m, parser.OFPFlowMod)], [])
        self.assertEqual(sent[0].actions[0].port, ofp.OFPP_FLOOD)

    def test_arp_never_installs_flows(self):
        p = packet.Packet()
        p.add_protocol(ethernet.ethernet(dst=SRV_MAC, src=C1_MAC, ethertype=0x0806))
        p.add_protocol(arp.arp(src_mac=C1_MAC, src_ip=C1_IP, dst_mac=SRV_MAC, dst_ip=SRV_IP))
        p.serialize()
        self.packet_in(p.data, 1)
        sent = self.s1.take()
        self.assertEqual([m for m in sent if isinstance(m, parser.OFPFlowMod)], [])
        self.assertEqual(sent[0].actions[0].port, 6)               # known port, packet-out only

    # --- classification

    def test_chat_flow_is_5tuple_pre_auth_normal(self):
        self.chat_c2s()
        [(prio, match, q, out, _)] = self.flows()
        self.assertEqual(prio, 100)
        self.assertEqual(match, {"eth_type": 0x0800, "ip_proto": 6, "ipv4_src": C1_IP,
                                 "ipv4_dst": SRV_IP, "tcp_src": 40000, "tcp_dst": 5000})
        self.assertEqual((q, out), (2, 6))

    def test_both_directions_tracked(self):
        self.chat_c2s()
        self.chat_s2c()
        conn = self.app.conns[(C1_IP, 40000)]
        self.assertEqual(set(conn.flows), {(1, "c2s"), (1, "s2c")})

    def test_vip_login_reinstalls_flows_in_queue_1(self):
        self.chat_c2s()
        self.chat_s2c()
        self.flows()
        self.event("login", "vip_ann")
        got = self.flows()
        self.assertEqual(len(got), 2)
        self.assertTrue(all(f[0] == 100 and f[2] == 1 for f in got))

    def test_event_before_packet_in(self):
        self.event("login", "vip_early")
        self.assertEqual(self.flows(), [])          # nothing installed yet
        self.chat_c2s()
        self.assertEqual(self.flows()[0][2], 1)

    def test_room_raises_and_leave_restores(self):
        self.chat_c2s()
        self.event("login", "bob")
        self.assertEqual(self.flows()[0][2], 2)
        self.event("join", "bob", "announcements")
        self.assertEqual(self.flows()[0][2], 1)
        self.event("leave", "bob", "announcements")
        self.assertEqual(self.flows()[0][2], 2)

    def test_guest_low_unless_room(self):
        self.chat_c2s()
        self.flows()
        self.event("login", "guest_1")
        self.assertEqual(self.flows()[0][2], 3)
        self.event("join", "guest_1", "announcements")
        self.assertEqual(self.flows()[0][2], 1)

    def test_bulk_and_icmp(self):
        self.packet_in(eth_ip(C1_MAC, SRV_MAC, C1_IP, SRV_IP, tcp.tcp(src_port=1234, dst_port=6000)), 1)
        prio, match, q, _, _ = self.flows()[0]
        self.assertEqual((prio, q, match.get("tcp_dst")), (50, 3, 6000))
        self.packet_in(eth_ip(C1_MAC, SRV_MAC, C1_IP, SRV_IP, icmp.icmp()), 1)
        prio, match, q, _, _ = self.flows()[0]
        self.assertEqual((prio, q, match["ip_proto"]), (40, 2, 1))

    # --- block / mode

    def test_block_drops_and_unblock_restores(self):
        self.chat_c2s()
        self.event("login", "mallory")
        self.flows()
        self.assertEqual(self.app.block("mallory"), 1)
        self.assertTrue(self.flows()[0][4])                         # empty instructions = drop
        self.s1.take()
        self.chat_c2s()                                             # flow expired, packet-in again
        self.assertTrue(self.flows()[0][4])
        self.assertEqual(self.s1.take(parser.OFPPacketOut), [])     # and the packet is not forwarded
        self.app.unblock("mallory")
        self.assertEqual(self.flows()[0][2], 2)

    def test_blocked_user_dropped_on_login(self):
        self.app.block("mallory")
        self.chat_c2s(port=40001)
        self.assertEqual(self.flows()[0][2], 2)                     # pre-auth: unknown yet
        self.event("login", "mallory", port=40001)
        self.assertTrue(self.flows()[0][4])

    def test_static_mode_flushes_and_ignores_policy(self):
        self.chat_c2s()
        self.event("login", "vip_x")
        self.flows()
        self.app.set_mode("static")
        mods = self.s1.take(parser.OFPFlowMod)
        self.assertEqual(mods[0].command, ofp.OFPFC_DELETE)
        self.assertEqual(self.app.conns[(C1_IP, 40000)].flows, {})
        self.chat_c2s()
        self.assertEqual(self.flows()[0][2], 2)
        self.app.set_mode("sdn")
        self.s1.take()
        self.chat_c2s()
        self.assertEqual(self.flows()[0][2], 1)

    def test_bulk_class_switch(self):
        self.app.set_mode(bulk_class="NORMAL")
        self.assertEqual(self.app.policy.bulk_class, "NORMAL")
        self.s1.take()
        self.packet_in(eth_ip(C1_MAC, SRV_MAC, C1_IP, SRV_IP, tcp.tcp(src_port=1234, dst_port=6000)), 1)
        self.assertEqual(self.flows()[0][2], 2)                     # new bulk flow now uses queue 2
        with self.assertRaises(ValueError):
            self.app.set_mode(bulk_class="URGENT")
        self.assertEqual(self.app.policy.bulk_class, "NORMAL")

    def test_bad_mode(self):
        with self.assertRaises(ValueError):
            self.app.set_mode("chaos")

    # --- monitor

    def stats_reply(self, byte_count, port=40000):
        st = parser.OFPFlowStats(table_id=0, duration_sec=1, duration_nsec=0, priority=100,
                                 idle_timeout=60, hard_timeout=0, flags=0, cookie=0,
                                 packet_count=1, byte_count=byte_count,
                                 match=parser.OFPMatch(eth_type=0x0800, ip_proto=6, ipv4_src=C1_IP,
                                                       ipv4_dst=SRV_IP, tcp_src=port, tcp_dst=5000),
                                 instructions=[])
        msg = types.SimpleNamespace(datapath=self.s1, body=[st])
        self.app.flow_stats_reply(types.SimpleNamespace(msg=msg))

    def run_polls(self, byte_counts, t0=1000.0):
        actions = []
        for i, b in enumerate(byte_counts):
            with mock.patch.object(self.mod.time, "time", return_value=t0 + i):
                self.stats_reply(b)
                self.app.monitor_tick()
            actions.append([f[2] for f in self.flows()])
        return actions

    def test_flood_demotion_and_recovery(self):
        self.chat_c2s()
        self.event("login", "alice")
        self.flows()
        mb = 1250000        # 10 Mbit per 1 s poll
        acts = self.run_polls([0, mb, 2 * mb, 3 * mb, 3 * mb, 3 * mb, 3 * mb])
        conn = self.app.conns[(C1_IP, 40000)]
        self.assertEqual(acts[2], [3], acts)                      # demoted after 2 polls over
        self.assertIn([2], acts[3:], acts)                       # restored after 2 quiet polls
        self.assertIsNone(conn.override)
        kinds = [t["text"] for t in self.app.transitions if t["kind"] == "flood"]
        self.assertEqual(len(kinds), 2, kinds)

    def test_flood_exempt_for_high(self):
        self.chat_c2s()
        self.event("login", "vip_fast")
        self.flows()
        mb = 1250000
        acts = self.run_polls([0, mb, 2 * mb, 3 * mb, 4 * mb])
        self.assertEqual([a for a in acts if a], [])

    def test_counter_reset_not_negative(self):
        self.chat_c2s()
        self.event("login", "alice")
        self.run_polls([5000000, 100])
        self.assertGreaterEqual(self.app.conns[(C1_IP, 40000)].rate_c2s, 0)

    def test_monitor_off_measures_but_never_demotes(self):
        self.app.monitor = False
        self.chat_c2s()
        self.event("login", "alice")
        self.flows()
        mb = 1250000
        acts = self.run_polls([0, mb, 2 * mb, 3 * mb])
        self.assertEqual([a for a in acts if a], [])
        self.assertGreater(self.app.conns[(C1_IP, 40000)].rate_c2s, 9e6)


if __name__ == "__main__":
    unittest.main()
