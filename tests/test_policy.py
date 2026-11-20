import os
import unittest

from sdn.policy import DROP, Connection, Policy, best, observe_rate, resolve

POLICY = {
    "user_tiers": {"admin": "HIGH", "vip_*": "HIGH", "guest_*": "LOW"},
    "room_classes": {"announcements": "HIGH"},
    "monitor": {"poll_interval": 1, "flood_bps": 1000000, "flood_intervals": 2,
                "recover_intervals": 3},
}


def conn(user=None, rooms=()):
    c = Connection("10.0.0.11", 40000)
    c.user = c.last_user = user
    c.rooms = set(rooms)
    return c


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.p = Policy(POLICY)

    def test_best(self):
        self.assertEqual(best("LOW", "NORMAL"), "NORMAL")
        self.assertEqual(best("LOW", "HIGH", "NORMAL"), "HIGH")

    def test_user_tiers(self):
        self.assertEqual(self.p.user_class("admin"), "HIGH")
        self.assertEqual(self.p.user_class("vip_7"), "HIGH")
        self.assertEqual(self.p.user_class("guest_x"), "LOW")
        self.assertEqual(self.p.user_class("alice"), "NORMAL")
        self.assertEqual(self.p.user_class("VIP_7"), "NORMAL")    # case-sensitive

    def test_rooms_only_raise(self):
        self.assertEqual(self.p.base_class("guest_x", {"general"}), "NORMAL")  # default room NORMAL
        self.assertEqual(self.p.base_class("alice", {"announcements"}), "HIGH")
        self.assertEqual(self.p.base_class("vip_1", {"general"}), "HIGH")

    def test_guest_without_rooms_low(self):
        self.assertEqual(self.p.base_class("guest_x", set()), "LOW")

    def test_pre_auth(self):
        self.assertEqual(resolve("sdn", self.p, conn(), set()), "NORMAL")

    def test_static_ignores_policy(self):
        self.assertEqual(resolve("static", self.p, conn("vip_1"), set()), "NORMAL")
        self.assertEqual(resolve("static", self.p, conn("bad"), {"bad"}), "NORMAL")

    def test_block_survives_logout(self):
        c = conn("bad", {"general"})
        self.assertEqual(resolve("sdn", self.p, c, {"bad"}), DROP)
        c.user = None
        self.assertEqual(resolve("sdn", self.p, c, {"bad"}), DROP)

    def test_unknown_class_rejected(self):
        with self.assertRaises(ValueError):
            Policy({"bulk_class": "URGENT"})


class FloodHysteresisTest(unittest.TestCase):
    def setUp(self):
        self.p = Policy(POLICY)

    def test_demote_then_restore(self):
        c = conn("alice", {"general"})
        self.assertIsNone(observe_rate(self.p, c, 5e6))
        self.assertEqual(observe_rate(self.p, c, 5e6), "demote")
        self.assertEqual(resolve("sdn", self.p, c, set()), "LOW")
        # Between half and full threshold: stays demoted (hysteresis).
        for _ in range(5):
            self.assertIsNone(observe_rate(self.p, c, 0.8e6))
        self.assertIsNone(observe_rate(self.p, c, 0.1e6))
        self.assertIsNone(observe_rate(self.p, c, 0.1e6))
        self.assertEqual(observe_rate(self.p, c, 0.1e6), "restore")
        self.assertEqual(resolve("sdn", self.p, c, set()), "NORMAL")

    def test_burst_resets_counter(self):
        c = conn("alice")
        observe_rate(self.p, c, 5e6)
        observe_rate(self.p, c, 0)            # one quiet poll resets the streak
        self.assertIsNone(observe_rate(self.p, c, 5e6))

    def test_high_is_exempt(self):
        c = conn("vip_1")
        for _ in range(10):
            self.assertIsNone(observe_rate(self.p, c, 50e6))
        self.assertEqual(resolve("sdn", self.p, c, set()), "HIGH")


class ShippedPolicyTest(unittest.TestCase):
    """The real sdn/policy.json, with every user in 'general' as after login."""

    def setUp(self):
        self.p = Policy.load(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                          "sdn", "policy.json"))

    def test_guest_stays_low_after_auto_join(self):
        # general is joined on login; it must not lift a guest to NORMAL
        self.assertEqual(self.p.base_class("guest_1", {"general"}), "LOW")

    def test_other_tiers_in_general(self):
        self.assertEqual(self.p.base_class("user_1", {"general"}), "NORMAL")
        self.assertEqual(self.p.base_class("vip_1", {"general"}), "HIGH")

    def test_listed_room_still_raises(self):
        self.assertEqual(self.p.base_class("user_ann", {"general", "announcements"}), "HIGH")
        self.assertEqual(self.p.base_class("guest_1", {"general", "ops"}), "HIGH")


if __name__ == "__main__":
    unittest.main()
