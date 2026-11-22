# Ownership and viva preparation

Solo project: all modules (chat app, controller, topology/QoS, experiments,
docs) are by one author. The modules are separable, so each can be explained
on its own:

| module | files | one-line summary |
|---|---|---|
| Chat app | `chat/` | threaded TCP server with per-client outbox; JSON-lines protocol; notifier reports app events |
| Controller | `sdn/` | reactive IP-level flows, class per connection from user/room policy, re-classify on events, flood monitor, block, static/sdn mode |
| Topology | `topo/topology.py` | 2 switches, 10 Mbps trunk, 3 HTB queues per port via OVS QoS |
| Experiments | `experiments/` | load generator, bulk traffic, runner, robustness scenarios, plots |

## Likely questions, with short answers

**Why TCP and not UDP for chat?** Chat needs reliable, in-order delivery.
TCP provides it, and the link-failure test shows no message is lost even after
a 3 s outage.

**How do you find message boundaries in TCP?** Newline-delimited JSON plus a
`LineReader` that buffers partial data and splits complete lines. Frames over
64 KB are rejected, which caps the per-client buffer.

**How does the server handle many clients?** One reader thread per client, plus
one writer thread draining a bounded outbox queue. A broadcast only enqueues,
so one slow client cannot block a room. If a client's outbox overflows, it is
disconnected. Shared state sits under one lock that is never held during
socket I/O.

**How does the application influence the network?** The server sends
login/join/leave/logout with the client's IP and TCP port to the controller.
The controller maps that 5-tuple to the user, computes a class from
`policy.json`, and re-sends flow-mods with a different `set_queue` (or drop) for
that connection on every switch.

**Why a Unix socket for events?** Mininet hosts are in separate network
namespaces and cannot reach the controller's 127.0.0.1. They share the
filesystem, so `/tmp/chat_sdn_events.sock` works from every host.

**Why not install MAC-learning flows?** An `eth_dst` flow would match the chat
SYN too, so the controller would never see new connections. All flows are
IP-level, and ARP is packet-out only.

**What do the queues actually do?** OVS creates HTB classes on each port. Each
queue has its own FIFO, a guaranteed min-rate and a borrowing priority. Chat in
queue 1/2 never queues behind bulk packets in queue 3. Under static mode, all
traffic shares one FIFO, which is why chat latency jumps to tens of ms.

**Why `txqueuelen 100`?** At 10 Mbps, a 1000-packet queue is ~1.2 s of delay,
which would make all results absurd (bufferbloat).

**How is flooding detected?** Per-flow byte counters from `OFPFlowStatsRequest`
every 1 s give the rate. Above 2 Mbps for 2 polls → demote to LOW. Below
1 Mbps for 5 polls → restore. The hysteresis prevents flapping. HIGH users are
exempt.

**What is the static baseline and why is it fair?** Same topology, same
shaping, same queues, with every flow in queue 2. Only the policy differs, so
the comparison isolates the effect of the SDN logic.

**Why does a blocked user's session linger on the server?** The drop happens in
the switch, so the server sees silence, not a close. Keepalive probes idle
connections. If the server has unacknowledged data queued, `TCP_USER_TIMEOUT`
(20 s) applies instead, because otherwise Linux retransmits for ~15 minutes.

**What happens if the controller dies?** Switches are in `secure` fail mode:
existing flows keep working until they idle out (60 s), and new connections
fail. Chat events are dropped silently (the notifier never blocks the server).

**Why is `default_room_class` LOW? Doesn't that hurt regular users?**
A connection's class is the best of its user tier and its rooms, and everyone
auto-joins `general`. With a NORMAL default, `general` lifted every guest to NORMAL
and the `guest_*` rule never applied (found while building the traffic UI). With
LOW, an unlisted room never raises anyone, while listed rooms (`announcements`,
`ops`) still do. Regular users stay NORMAL through their own tier, so they are
unaffected.

**Limitations?** See the README section. The main ones: reactive setup adds one
controller round-trip per new connection per switch; block enforcement has a
few-ms race window right after login; there is one bottleneck link, not a
multi-path topology.
