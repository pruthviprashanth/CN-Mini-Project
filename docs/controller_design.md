# SDN controller design (`sdn/controller.py`, `sdn/policy.py`)

Ryu is only the framework (OpenFlow 1.3 parsing, event loop, REST server).
Classification, policy, flow rules and the dynamic behaviour are this project's
code. The decision logic lives in `sdn/policy.py` (plain Python, no Ryu), and
`controller.py` turns those decisions into OpenFlow messages.

## Traffic classes and queues

The topology configures the same three OVS `linux-htb` queues on **every** switch
port, so the controller can always use `set_queue`. Setting a queue that does not
exist on a port can drop packets.

| class | queue | min-rate | HTB priority | who |
|---|---|---|---|---|
| HIGH | 1 | 20 % of port | 1 (borrows first) | `admin`, `vip_*`, members of `announcements`/`ops` |
| NORMAL | 2 | 50 % | 2 | regular users, unauthenticated chat, ICMP |
| LOW | 3 | 10 % | 3 | bulk TCP/UDP, `guest_*`, flooders (demoted) |

Every queue's max-rate is the port rate, so HTB is work-conserving: an idle
class lends its bandwidth to the others. Priority decides who borrows first,
and each class has its own FIFO. A small chat message in queue 1 or 2 never
waits behind a 100-packet bulk backlog in queue 3. That separation is where the
latency gain comes from.

OVS maps queue *q* to tc class `1:(q+1)`. `tc -s class show dev s1-eth6` shows
the byte counters per queue (verified in `tests/mininet_check.py`).

## Flow table (per switch)

| priority | match | actions | installed when |
|---|---|---|---|
| 100 | chat 5-tuple: `ip, tcp, nw_src, nw_dst, tp_src, tp_dst` (one per direction) | `set_queue:<class>, output:<port>` or **drop** | first packet of the connection reaches this switch |
| 50 | other TCP/UDP: `nw_src, nw_dst, proto, tp_dst` | `set_queue:3` (bulk_class) | first packet |
| 40 | other IP (ICMP...): `nw_src, nw_dst, proto` | `set_queue:2` | first packet |
| 0 | everything | `output:CONTROLLER` | switch connect (table-miss) |

Installed flows have a 60 s idle timeout. Chat flows carry cookie `0xC0FFEE01`,
so the monitor polls only those.

**Why no L2 (`eth_dst`) flows:** a MAC-learning flow for ARP/ICMP would also
match TCP packets to the same host. The chat SYN would then be switched without
reaching the controller, and no per-connection flow would ever exist. So ARP is
handled with packet-out only (no flow), and every installed flow is specific
enough that it cannot shadow a different class of traffic.

Forwarding: the controller learns `MAC → port` from packet-ins. An unknown
destination is flooded by packet-out (safe: the topology is a tree). A known
destination gets a flow.

## Classification

- TCP with `tcp_dst == 5000` is client→server (`c2s`), `tcp_src == 5000` is
  server→client (`s2c`). The connection key is the **client** end `(ip, port)`.
- The per-connection record (`Connection`) stores the user, joined rooms, every
  installed flow `(dpid, direction) → (match, out_port)`, the current class, the
  flood override and rates.
- Effective class (`policy.resolve`):
  1. `static` mode → always NORMAL (the baseline ignores all policy)
  2. user blocked → DROP
  3. not logged in → `pre_auth_class` (NORMAL)
  4. otherwise **best of** (user tier via `fnmatch` on `user_tiers`, class of
     every joined room). Rooms can only raise priority. Rooms not listed in
     `room_classes` use `default_room_class`, which is `LOW` in the shipped policy:
     everyone auto-joins `general`, so a `NORMAL` default would lift every
     `guest_*` user to NORMAL and the guest tier would never apply.
  5. flood override → LOW, unless the base class is HIGH (HIGH is exempt)

## App → network: event handling

The chat server sends `login/logout/join/leave` with the client's `(ip, port)`
(see `docs/protocol.md`). The controller listens on the Unix datagram socket in
a `hub.spawn` green thread (Ryu's eventlet patches `socket`, so `recv` yields
instead of blocking the controller). For each event it:

1. updates the connection record (user, rooms)
2. recomputes the class
3. **re-sends a flow-mod for every installed flow of that connection** with the
   new `set_queue` (or empty actions = drop), on every switch where it was
   installed

If an event arrives before the connection's first packet-in, the record already
holds the user/rooms, and the first flow is installed with the right class.

Example (from `/chat/state` → `transitions`):
`vip_bob@10.0.0.12:40210 NORMAL -> HIGH (login)`

## Dynamic behaviour

**Monitor (flood detection).** Every `poll_interval` (1 s) the controller sends
`OFPFlowStatsRequest` filtered by the chat cookie. For each `c2s` flow it
computes `Δbytes·8 / Δt` from consecutive replies (Δt is measured, not assumed),
taking the max over switches (the same flow is counted on s1 and s2). A negative
delta means the flow was re-added and its counter reset; the new count is used.
Hysteresis:

- rate > `flood_bps` (2 Mbps) for `flood_intervals` (2) consecutive polls, and
  class not HIGH → override LOW, re-install, log `[FLOOD] ... demoted`
- while demoted, rate < `flood_bps/2` for `recover_intervals` (5) polls →
  restore, log `[FLOOD] ... restored`

The flooder is moved, not punished: queue 3 can still borrow idle bandwidth. It
just stops sharing a FIFO with normal users.

**Block (unauthorized access).** `POST /policy/block {"user":"x"}` adds `x` to the
blocked set and re-installs all of `x`'s connections as drop flows (both
directions, all switches). A blocked user who logs in again is dropped as soon
as the login event arrives. `POST /policy/unblock` reverses it. This is
enforcement in the network, so the server only sees a silent TCP connection. It
reaps that via keepalive / `TCP_USER_TIMEOUT` (≈20 s, see the README limitations).

**Mode switch (baseline).** `POST /mode {"mode":"static"|"sdn","monitor":bool}`
deletes all flows on every switch, re-adds table-miss and clears the installed
flow lists. Every flow is then re-learned under the new mode. `static` uses
the same topology, queues and shaping with everything in queue 2, so the only
difference between variants is the policy.

## REST API (port 8080)

| method | path | body | effect |
|---|---|---|---|
| GET | `/health` | | mode, monitor, connected datapaths |
| GET | `/chat/state` | | connections (user, rooms, class, queue, rates, flows), stats, events, transitions |
| POST | `/chat/event` | event JSON | same as the Unix-socket path |
| POST | `/mode` | `{"mode":"static","monitor":false}` | flush and switch |
| POST | `/policy/block` | `{"user":"name"}` | drop that user's connections |
| POST | `/policy/unblock` | `{"user":"name"}` | restore |
| POST | `/policy/reload` | | re-read `policy.json`, re-classify everything |

## Configuration

Environment variables (not Ryu CLI options): `CHAT_POLICY` (default
`sdn/policy.json`), `CHAT_MODE` (`sdn`), `CHAT_MONITOR` (`1`), `CHAT_EVENT_SOCK`
(`/tmp/chat_sdn_events.sock`).
