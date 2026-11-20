"""Traffic classes, the policy file and per-connection decisions.

Pure Python with no Ryu imports, so the decision logic can be unit-tested and
read on its own; controller.py turns these decisions into OpenFlow messages.
"""
import fnmatch
import json
import time

# Class name -> OVS queue id configured on every switch port by the topology.
QUEUES = {"HIGH": 1, "NORMAL": 2, "LOW": 3}
RANK = {"LOW": 1, "NORMAL": 2, "HIGH": 3}
DROP = "DROP"

DEFAULTS = {
    "chat_port": 5000,
    "user_tiers": {},
    "default_user_class": "NORMAL",
    "room_classes": {},
    "default_room_class": "NORMAL",
    "bulk_class": "LOW",
    "pre_auth_class": "NORMAL",
    "blocked_users": [],
    "monitor": {"poll_interval": 1, "flood_bps": 2000000,
                "flood_intervals": 2, "recover_intervals": 5},
}


def best(*classes):  # return the highest-ranking class (HIGH > NORMAL > LOW)
    return max(classes, key=lambda c: RANK[c])


class Policy:
    def __init__(self, data=None):  # merge policy.json over the defaults and validate the class names
        data = dict(data or {})
        merged = dict(DEFAULTS)
        merged.update(data)
        merged["monitor"] = dict(DEFAULTS["monitor"], **data.get("monitor", {}))
        self.raw = merged
        self.chat_port = int(merged["chat_port"])
        self.user_tiers = list(merged["user_tiers"].items())    # first match wins
        self.default_user_class = merged["default_user_class"]
        self.room_classes = list(merged["room_classes"].items())
        self.default_room_class = merged["default_room_class"]
        self.bulk_class = merged["bulk_class"]
        self.pre_auth_class = merged["pre_auth_class"]
        self.blocked_users = set(merged["blocked_users"])
        m = merged["monitor"]
        self.poll_interval = float(m["poll_interval"])
        self.flood_bps = float(m["flood_bps"])
        self.flood_intervals = int(m["flood_intervals"])
        self.recover_intervals = int(m["recover_intervals"])
        for c in [self.default_user_class, self.default_room_class, self.bulk_class,
                  self.pre_auth_class] + [c for _, c in self.user_tiers + self.room_classes]:
            if c not in QUEUES:
                raise ValueError("unknown traffic class %r" % c)

    @classmethod
    def load(cls, path):  # build a Policy from a JSON file
        with open(path) as f:
            return cls(json.load(f))

    def user_class(self, user):  # class for a username: first matching pattern in user_tiers (vip_*, ...), else default
        for pattern, klass in self.user_tiers:
            if fnmatch.fnmatchcase(user, pattern):
                return klass
        return self.default_user_class

    def room_class(self, room):  # class for a room name, from room_classes
        for pattern, klass in self.room_classes:
            if fnmatch.fnmatchcase(room, pattern):
                return klass
        return self.default_room_class

    def base_class(self, user, rooms):  # best of the user's tier and all joined rooms; rooms can only raise priority
        """Class from identity alone: best of the user's tier and joined rooms.
        Rooms can only raise priority, never lower it."""
        if user is None:
            return self.pre_auth_class
        return best(self.user_class(user), *[self.room_class(r) for r in rooms])


class Connection:
    """One chat TCP connection, identified by the client's (ip, tcp port)."""

    def __init__(self, ip, port):  # create the record for one chat connection (client ip and port)
        self.ip, self.port = ip, port
        self.user = None
        self.last_user = None      # kept after logout so a block still applies
        self.rooms = set()
        self.flows = {}            # (dpid, "c2s"|"s2c") -> (match dict, out_port)
        self.klass = None          # class of the flows currently installed
        self.override = None       # "LOW" while demoted for flooding
        self.over = 0              # consecutive polls above the flood threshold
        self.under = 0             # consecutive polls below half the threshold
        self.rate_c2s = 0.0
        self.rate_s2c = 0.0
        self.created = time.time()
        self.closed_at = None

    @property
    def key(self):  # the connection's identity: (ip, port)
        return (self.ip, self.port)

    def label(self):  # readable name like vip_bob@10.0.0.12:40210 for logs
        return "%s@%s:%d" % (self.user or self.last_user or "?", self.ip, self.port)


def resolve(mode, policy, conn, blocked):  # final class: static -> NORMAL, blocked -> DROP, flood override -> LOW, else base class
    """Effective class for a connection's flows (a queue name or DROP)."""
    if mode == "static":
        return "NORMAL"            # baseline: same queues, no policy at all
    if conn.last_user is not None and conn.last_user in blocked:
        return DROP
    base = policy.base_class(conn.user, conn.rooms)
    if conn.override == "LOW" and base != "HIGH":
        return "LOW"
    return base


def observe_rate(policy, conn, rate_bps):  # flood check: returns 'demote' / 'restore' / None using the 2-polls and 5-polls hysteresis
    """Feed one poll's client-to-server rate; return 'demote', 'restore' or None.

    Hysteresis: demote after flood_intervals polls above flood_bps, restore only
    after recover_intervals polls below half of it, so a borderline sender
    does not flap between queues.
    """
    conn.rate_c2s = rate_bps
    if conn.override is None:
        if rate_bps > policy.flood_bps and policy.base_class(conn.user, conn.rooms) != "HIGH":
            conn.over += 1
        else:
            conn.over = 0
        if conn.over >= policy.flood_intervals:
            conn.override, conn.over, conn.under = "LOW", 0, 0
            return "demote"
    else:
        if rate_bps < policy.flood_bps / 2:
            conn.under += 1
        else:
            conn.under = 0
        if conn.under >= policy.recover_intervals:
            conn.override, conn.over, conn.under = None, 0, 0
            return "restore"
    return None
