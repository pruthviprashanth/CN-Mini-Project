# Part 3: The Network, Experiments and Results

This part covers the emulated network that the chat app and the controller
run on, and the experiments that measure whether the SDN version is better.

| Area | Files | Lines |
|---|---|---|
| A. Network | `topo/topology.py`, `topo/mx.sh` | 156 |
| B. Experiments | `experiments/common.py`, `bulk.py`, `load_client.py`, `run_experiments.py`, `robustness.py`, `plot.py` | 1,053 |
| C. Checks and results | `tests/mininet_check.py`, `results/`, README "Results" | 179 + data |

Contents:

1. [The big picture](#1-the-big-picture)
2. [CN concepts used in Part 3](#2-cn-concepts-used-in-part-3)
3. [Code walkthrough: the network](#3-code-walkthrough-the-network)
4. [Code walkthrough: the experiments](#4-code-walkthrough-the-experiments)
5. [Code walkthrough: the end-to-end check](#5-code-walkthrough-the-end-to-end-check)
6. [Results and what they mean](#6-results-and-what-they-mean)
7. [Limitations](#7-limitations)
8. [Viva questions for Part 3](#8-viva-questions-for-part-3)

---

## 1. The big picture

```
 c1  10.0.0.11 ─┐ s1-eth1                                 s2-eth1 ┌─ srv   10.0.0.1   chat server :5000
 c2  10.0.0.12 ─┤ s1-eth2                                         │
 c3  10.0.0.13 ─┤ s1-eth3   s1-eth6            s2-eth3    s2-eth2 └─ sink  10.0.0.2   bulk receiver :6000
 c4  10.0.0.14 ─┤ s1-eth4 [s1]═══════ 10 Mbps trunk ═══════[s2]
 bulk 10.0.0.100┘ s1-eth5   │                                │
        100 Mbps access     └───── OpenFlow 1.3 (TCP 6653) ──┴──► Ryu controller (127.0.0.1)
        links

 every switch port: 3 HTB queues   q1 HIGH 20% / prio 1   q2 NORMAL 50% / prio 2   q3 LOW 10% / prio 3
```

Part 3 does three jobs:

1. **Build a network** with real protocols but no real hardware (Mininet + Open vSwitch).
2. **Create a bottleneck** on purpose: the s1–s2 trunk is limited to 10 Mbps, so
   traffic can pile up and priority actually matters.
3. **Measure** chat latency, loss and throughput under different conditions,
   and compare the static baseline with the SDN policy.

**Why build the bottleneck on purpose?** Virtual links run at memory speed (several Gbps)
and never congest. Without a bottleneck, every mode gives the same ~1 ms and
there is nothing to compare.

---

## 2. CN concepts used in Part 3

### 2.1 Emulation vs. simulation

- **Simulation** (e.g. ns-3) models packets in a program. Nothing real is sent.
- **Emulation** (Mininet) runs the **real** Linux TCP/IP stack, real
  applications and a real software switch. Only the hardware (separate machines
  and cables) is replaced by kernel features.

So the chat server in this project is the same program that would run on a
real server. The TCP behaviour you measure (retransmission, congestion
control) is real Linux TCP.

### 2.2 How Mininet makes "computers": network namespaces

A **network namespace** gives a process its own private copy of the network stack:
its own interfaces, IP addresses, routing table, ARP table and port numbers.
Mininet starts one shell per host, `bash --norc -is mininet:c1`, inside a new
namespace. Everything started from that shell is "on c1".

- **Separate:** network (IP, ports, routes). That's why c1 cannot reach `127.0.0.1:8080` on the VM.
- **Shared:** filesystem, CPU, clock, process list. That's why the chat server can
  reach the controller through the file `/tmp/chat_sdn_events.sock`, and why
  delivery latency can compare timestamps from different hosts.

### 2.3 How Mininet makes "cables": veth pairs

A **veth pair** is a virtual Ethernet cable: two interfaces where whatever goes
into one comes out of the other. For `c1 — s1`, Mininet creates `c1-eth0` (moved
into c1's namespace) and `s1-eth1` (attached to switch s1).

### 2.4 The switches: Open vSwitch and OpenFlow

**Open vSwitch (OVS)** is a production software switch used in data centres
and clouds. Each switch has a **flow table** of rules. Each rule has three parts:

- **Match:** for example `tcp, nw_src=10.0.0.11, tp_dst=5000`
- **Actions:** for example `set_queue:2, output:6`, or drop
- **Priority, counters and timeouts**

OVS is programmed by the controller over **OpenFlow 1.3**, a TCP connection to
port 6653. This is the **control channel**. It's *out-of-band*: it doesn't use
the data links. The switches are created with `failMode="secure"`, so they
forward nothing on their own and only follow the controller's rules. In
`standalone` mode they would fall back to being ordinary learning switches.

### 2.5 Addressing: IP, subnet, MAC, ARP

- **All hosts are in one subnet,** `10.0.0.0/24` (`/24` = the first 24 bits are the
  network part). So all hosts can reach each other directly at Layer 2, and no
  router is needed.
- **MAC addresses** are set by `autoSetMacs=True` (`00:00:00:00:00:01`, …), which
  makes flow dumps readable.
- **ARP:** before c1 can send to 10.0.0.1 it broadcasts "who has 10.0.0.1?". The
  controller forwards ARP by packet-out only, without installing a flow, so
  every TCP connection still reaches the controller. See `docs/controller_design.md`.

### 2.6 Bandwidth, delay and the bottleneck

- **Bandwidth** = how many bits per second a link can carry.
- **Bottleneck** = the slowest link on a path. It limits the whole path.
  Here: access links are 100 Mbps, the trunk is 10 Mbps. **Every chat message crosses
  the trunk twice** (client → server, server → client), and so does bulk traffic.

The delay of one packet has four parts:

| Delay | Meaning | Size here |
|---|---|---|
| transmission | time to put the bits on the wire = size / bandwidth | 1500 B at 10 Mbps = **1.2 ms** |
| propagation | signal travel time | ~0 (virtual cable) |
| processing | switch/host work | µs |
| **queueing** | waiting behind other packets | **0 to 120 ms**: this is what priority changes |

**Queueing delay example:** if 100 full-size packets are waiting ahead of you on a
10 Mbps link: 100 × 1500 B × 8 = 1.2 Mbit, and 1.2 Mbit ÷ 10 Mbps = **120 ms**.
A chat message of ~200 B needs only ~0.2 ms to transmit, but it can wait 120 ms
in line. Priority queues remove the line, not the transmission time.

### 2.7 Queue length, bufferbloat, `txqueuelen`

Each interface has a queue whose length is `txqueuelen`. Linux uses 1000
packets by default. At 10 Mbps a full 1000-packet queue is **1.2 s** of
waiting. This problem is called **bufferbloat**. The project sets
`txqueuelen 100` on every switch port, so a full queue is at most ~120 ms,
which is realistic for a LAN switch. The HTB leaf queues use this length.

### 2.8 Segmentation offload (TSO/GSO/GRO)

Linux normally hands the NIC "super-packets" up to 64 KB and lets the NIC
(or the kernel, late) split them. On virtual links that means the shaper sees
64 KB blobs, which makes rate limiting and queueing delay unrealistic. The
project turns them off with `ethtool -K dev tso off gso off gro off`, so HTB
sees normal MTU-sized (1500 B) packets.

### 2.9 QoS, traffic shaping and HTB

**QoS (Quality of Service)** means treating traffic classes differently. The tool used
here is **traffic shaping**: limiting how fast packets leave an interface, and in
which order.

**HTB (Hierarchical Token Bucket)** is the Linux shaper that OVS uses
(`type=linux-htb`). Its main ideas:

- **Token bucket:** a class earns "tokens" at its rate. Sending a byte costs a token.
  No tokens means the packet waits. This is how a rate limit is enforced.
- **Classes with `rate` (guaranteed) and `ceil` (maximum):**
  OVS calls them `min-rate` and `max-rate`.
- **Borrowing:** a class that's under its ceil can borrow unused tokens
  from its parent. Classes with a **lower priority number borrow first**.
- **Each class has its own FIFO queue.** Packets of different classes don't
  wait behind each other.
- **Work-conserving:** if only one class has traffic, it can use the whole link
  (because every `ceil` = link rate). No bandwidth is wasted.

This project's numbers (`topo/topology.py:28`):

| OVS queue | tc class | min-rate (trunk) | max-rate | prio | Used for |
|---|---|---|---|---|---|
| q1 HIGH | 1:2 | 20% = 2 Mbps | 10 Mbps | 1 | VIP, admin, `announcements`/`ops` |
| q2 NORMAL | 1:3 | 50% = 5 Mbps | 10 Mbps | 2 | regular chat, ICMP, static mode |
| q3 LOW | 1:4 | 10% = 1 Mbps | 10 Mbps | 3 | bulk, guests, flooders |
| (q0 default) | 1:1 | — | — | — | not used by the controller |

The guarantees add to 80%. The rest is free for borrowing. OVS maps queue `q`
to tc class `1:(q+1)`, which is why `tc -s class show dev s1-eth6` shows `1:2`
for q1.

**Where the queue is applied:** a queue sits on the **outgoing** port. The controller's
action `set_queue:2, output:6` means "leave through port 6 (the trunk), waiting in
queue 2 there". The trunk ports `s1-eth6` and `s2-eth3` are where congestion builds.

### 2.10 TCP behaviour you see in the experiments

- **Congestion control:** a bulk TCP sender keeps increasing its sending rate until
  packets are lost, then backs off. So it **always fills the bottleneck queue**.
  That's why bulk traffic makes chat slow in static mode.
- **Reliable delivery and retransmission:** if a packet is lost (e.g. the link is
  down), TCP resends it after a timeout that doubles each time (exponential backoff). In the
  link-failure test, messages sent during the outage arrive late (up to 3.4 s), but none are lost.
- **Keepalive and `TCP_USER_TIMEOUT`:** how the server notices a silently dropped
  (blocked) client. Part 1 covers these.

### 2.11 Measurement concepts

- **RTT (round-trip time):** from sending a message to receiving the server's `ack`. Measured in
  ms by the client itself.
- **Delivery latency:** from sender's timestamp to the moment another user
  receives it. This works because all Mininet hosts share one clock.
- **Percentiles:** p50 (median) = half the messages were faster; p95 = 95% were faster;
  p99 = the worst 1%. Averages hide bad cases. p95/p99 show what users feel in the
  worst moments, so the results use them.
- **Throughput:** acked messages per second (server), and Mbps (bulk).
- **Loss:** sent − acked (`unacked`), and `1 − received / expected` (`delivery_loss`).
- **Fan-out:** one room message becomes one copy per member. A room of 10 means 9
  outgoing copies.
- **Baseline / controlled experiment:** static and SDN use the **same** topology, links,
  queues and load. Only the controller's policy changes, so any difference is caused by
  the policy.
- **Warm-up:** samples from the first 1 s are skipped (connection setup, flow
  installation).

---

## 3. Code walkthrough: the network

### 3.1 `topo/topology.py`

#### Constants (lines 25–28)

```python
SERVER_IP, SINK_IP, BULK_IP = "10.0.0.1", "10.0.0.2", "10.0.0.100"
QUEUE_SHARES = {1: (0.20, 1), 2: (0.50, 2), 3: (0.10, 3)}
```
`QUEUE_SHARES` maps each queue to (min-rate share, HTB priority). The queue IDs must
match `sdn/policy.py`, because the controller writes `set_queue:<id>`. Sending a
packet to a queue that doesn't exist on the port can drop it, so every port gets all three.

#### `client_ip(i)` (line 30)

Returns `10.0.0.(10+i)`: c1 → 10.0.0.11, c4 → 10.0.0.14.

#### `build_network(...)` (lines 34–60): builds everything

```python
def build_network(n_clients=4, trunk_mbps=10, access_mbps=100, txqueuelen=100,
                  controller_ip="127.0.0.1", controller_port=6653, qos=True):
```
Step by step:

1. `Mininet(controller=None, switch=OVSSwitch, link=Link, autoSetMacs=True, build=False)`:
   an empty network that will use OVS switches and plain veth links (`Link`, not
   Mininet's `TCLink`). Shaping is done by OVS QoS instead, so the controller's
   `set_queue` works.
2. `net.addController("c0", controller=RemoteController, ip=..., port=6653)`: the
   controller is **external** (Ryu, started separately). Mininet only tells the switches where it is.
3. `addSwitch("s1", protocols="OpenFlow13", failMode="secure", dpid="1")`: OpenFlow
   1.3, secure fail mode (no forwarding without a controller), datapath ID 1, which is how the
   controller tells the switches apart. s2 gets dpid 2.
4. **Links, in a fixed order,** because the order decides port numbers:
   - c1..cN → s1 ports 1..N
   - bulk → s1 port N+1 (5)
   - srv → s2 port 1, sink → s2 port 2
   - s1 ↔ s2 last → s1 port N+2 (6), s2 port 3. This is the trunk.
5. `net.build(); net.start()`: creates namespaces and veths, starts OVS bridges, and
   the switches connect to the controller.
6. **Find the trunk interfaces** (`net.linksBetween(s1, s2)`), so they can get the lower rate.
7. **For every switch port:**
   - `rate` = 10 Mbps for trunk ports, 100 Mbps for access ports
   - `_tune(port, 100)`: queue length and offloads
   - `_configure_qos(port, rate)`: HTB with 3 queues
8. Stores `net.trunk_intfs` (used by `show_qos`) and returns the network.

**`--no-qos`** skips step 7's QoS. Then there are no rate limits and no queues. It's used only
for debugging.

#### `_tune(dev, txqueuelen)` (lines 63–70)

```python
subprocess.check_call(["ip", "link", "set", "dev", dev, "txqueuelen", str(txqueuelen)])
subprocess.call(["ethtool", "-K", dev, "tso", "off", "gso", "off", "gro", "off"], ...)
```
Sets the 100-packet queue (section 2.7) and turns off offloads (section 2.8). `ethtool` is optional
(`shutil.which`). Without it the results are slightly less realistic but still run.

#### `_configure_qos(port, rate)` (lines 73–81)

Builds one `ovs-vsctl` command that creates the rows in OVS's database:
```
ovs-vsctl -- set Port s1-eth6 qos=@q
          -- --id=@q create QoS type=linux-htb other-config:max-rate=10000000 queues:1=@q1 queues:2=@q2 queues:3=@q3
          -- --id=@q1 create Queue other-config:min-rate=2000000 other-config:max-rate=10000000 other-config:priority=1
          -- --id=@q2 create Queue other-config:min-rate=5000000 ... priority=2
          -- --id=@q3 create Queue other-config:min-rate=1000000 ... priority=3
```
- **`@q`, `@q1`** are temporary names inside one transaction, so all rows are created
  and linked at once.
- **The result:** OVS turns this into a Linux HTB qdisc on the port, with classes 1:2, 1:3 and 1:4.
- **Max-rate = port rate for every queue** makes HTB work-conserving (section 2.9).

#### `stop_network(net)` (lines 84–90)

`net.stop()` removes hosts, links and switches. QoS and Queue rows in the OVS
database aren't deleted automatically, so they're destroyed explicitly. Otherwise
they'd pile up across runs.

#### `start_server(net, ...)` (lines 93–106)

1. `fresh=True` deletes the old users file, so every run starts clean.
2. `net.get("srv").popen([...server.py, "--users-file", ..., "--controller", "unix:/tmp/chat_sdn_events.sock"])`:
   runs the chat server **inside the `srv` namespace**, so it listens on 10.0.0.1:5000.
3. Waits up to 5 s until the log contains "listening", so clients don't connect too early.

#### `show_qos(net)` (lines 109–112)

Prints `tc -s class show` for the trunk ports. It's used by `check`.

#### `main()` (lines 115–141)

| Action | What it does |
|---|---|
| `demo` (default) | build, wait for switches, **start chat server**, `pingAll`, open the Mininet CLI |
| `cli` | build, `pingAll`, CLI (no chat server) |
| `check` | build, `pingAll`, print tc classes, exit with code 0 if no loss |

- `net.waitConnected(timeout=10)` waits until both switches have connected to the controller.
- `net.pingAll()` sends one ping between every pair of hosts: 7 × 6 = 42 pings.
  It's also how the controller learns where every MAC address is.
- `CLI(net)` gives the `mininet>` prompt. The `finally` block always stops the server
  and cleans up the network, even after Ctrl-C or an error.

### 3.2 `topo/mx.sh`

```bash
pid=$(pgrep -f "mininet:${host}\$" | head -1)   # find the shell that IS host c1
exec mnexec -a "$pid" "$@"                      # run the command in its namespaces
```
Mininet's own `mininet>` prompt is one terminal. `mx.sh` lets you run an
interactive program (the chat client) "on" any host **from any terminal**.
`mnexec` is a Mininet tool. `-a PID` attaches to all of that process's namespaces.
`sudo` is needed because entering a namespace needs root. The `\$` makes sure
`c1` doesn't also match `c10`.

---

## 4. Code walkthrough: the experiments

### 4.1 `experiments/common.py`: shared helpers

| Function | What it does | Why |
|---|---|---|
| `cleanup_mininet()` | `mn -c` | removes leftovers; **must run before the controller**, because `mn -c` also kills `ryu-manager` |
| `start_controller(log, mode, monitor, ...)` | starts Ryu with env vars `CHAT_MODE`, `CHAT_MONITOR`, then polls `GET /health` for up to 15 s | the experiments need a controller they control; fails fast if Ryu exits |
| `stop_controller(proc)` | terminate, or kill after 5 s; delete `/tmp/chat_sdn_events.sock` | the socket was created by root; a later non-root controller couldn't replace it |
| `rest(method, path, body)` | small HTTP client for the REST API | change mode, block users, read state |
| `wait_switches(n=2)` | polls `/health` until 2 datapaths are connected | experiments must not start before the switches are ready |
| `dump_flows(switch)` | `ovs-ofctl -O OpenFlow13 dump-flows` | the checks read flow tables |
| `tc_classes(dev)` | parses `tc -s class show` into `{queue: bytes_sent}` | **proves** traffic went through the right queue; `1:(q+1)` → q |

`RYU` points to `~SUDO_USER/ryu-venv/bin/ryu-manager`. The experiments run as root, but the
venv is in the normal user's home.

### 4.2 `experiments/bulk.py`: background congestion

No `iperf` is needed. This is a tiny TCP tool:

- **`sink`** listens on port 6000 (`listen(64)`). For each connection it starts a thread that
  reads and discards 64 KB chunks until the sender closes.
- **`source`** opens `--streams` (default 4) TCP connections to the sink and calls
  `sendall(64 KB of zeros)` in a loop until `--duration` ends. Then it prints
  `{"bytes", "seconds", "streams", "mbps"}` and optionally writes it to `--out`.

**Why 4 streams?** One TCP flow can back off after a loss and leave the link
briefly idle. Four flows together keep the bottleneck queue full all the time, like a
real busy link.

**How the controller sees it:** port 6000 isn't chat, so it matches the priority-50 rule
"other TCP" and goes to `bulk_class` = LOW (q3), or q2 in static mode.

### 4.3 `experiments/load_client.py`: simulated chat users

One process simulates many users. Each user is a thread (`SimUser`) with its own
TCP connection, so each one is a separate flow in the switches.

**Naming** (`user_name`): users are numbered globally. The first `vip_fraction` (20%)
are `vip_N`, the rest `user_N`. With `--first` and `--total`, four processes (one per host)
share one numbering. With 100 users, c1 runs 0–24, c2 runs 25–49, and so on.

**Rooms:** `room = "load_%d" % (index // room_size)`, so groups of 10 share a room,
spread across hosts. Every message fans out to 9 others.

**`run()`, per user:**

1. `connect()`, then `register` (ignored if it exists), then `login`, then `join(room)`.
   `connect_ms` measures connect + login time.
2. **Wait for `start_at`,** a shared start time, so all users send together. If setup took
   too long, the user records `late_start_s` instead of failing.
3. `_steady()` or `_flood()`.
4. **Drain:** keep receiving until every sent message has an ack or error (max `--drain`
   seconds), so in-flight messages are counted. Then close.

**`_send()`:**
```python
t = time.time()
c.send_async("msg", lambda reply: self.on_reply(reply, t), room=self.room, text=text, ts=t)
```
- The send time `t` is captured in the lambda (a closure). If the ack arrives before
  `send_async` even returns, it's still matched to the right time.
- `ts=t` goes inside the message, so receivers can compute delivery latency.

**`on_reply()`** records RTT = now − t for acks, and counts errors. **`on_message()`** records the
delivery latency of room messages from others.

**`_steady()`:** sends `--rate` messages per second (default 2) of ~`--size` bytes (120).
Each user starts at a random offset in the interval, so users don't all send at the same
instant. The schedule uses `nxt += interval`, so it doesn't drift.

**`_flood()`** (with `--flood`): one user named `flooder` in a private room `spam`
sends `--size`-byte messages (7000 in the experiment) **as fast as TCP allows**. `sendall`
blocks when the socket buffer is full, so the flood rate is whatever the network gives
it, up to ~9.4 Mbps. It's a regular-tier user, so the flood monitor can demote it.

**Output:** JSON with every user's record: `sent`, `acked`, `errors`, `rtt` list,
`delivery` list, `connect_ms`, `late_start_s`, `error`.

### 4.4 `experiments/run_experiments.py`: the experiment runner

#### Statistics (lines 48–86)

- `pct(values, p)`: percentile with linear interpolation (the same method as numpy's default).
- `summarize(users, duration, warmup=1.0)` combines all users into one row: RTT
  mean/p50/p95/p99/max, the same split by VIP and regular, delivery p50/p95/p99,
  connect p50/p95, messages per second, `unacked`, and `delivery_loss`. Samples before
  `warmup` s are dropped (section 2.11).

#### `Testbed` (lines 91–187): one network, many runs

`__init__`:
1. `cleanup_mininet()`, then `start_controller()` (order matters, see 4.1)
2. `build_network(4)`, then `wait_switches()`
3. `pingAll` must give 0% loss, or the run stops
4. starts the bulk **sink** on `sink` once, for all runs

`run(tag, n_users, duration, mode, monitor, bulk, rate, ..., flood)` is one measurement:

1. `POST /mode`: switch to static or SDN. This flushes all flows, so every run starts
   from the same state. Then it waits 1 s.
2. Starts a **fresh chat server** with its own users file and log in the run's folder.
3. `setup = 4.0 + 0.1 × n_users` seconds for everyone to connect and log in. It grows with
   users because PBKDF2 password hashing (100,000 iterations) is CPU-heavy on 2 vCPUs.
4. **If `bulk`,** it starts the bulk source on `bulk` for the whole run, then waits 3 s so TCP
   ramps up and the queues are full **before** chat starts.
5. `start_at = now + setup`, and users are split as evenly as possible over c1–c4
   (`per` list). It starts one `load_client.py` per host with `--first`/`--total`.
6. **If `flood`,** it also starts the flooder on c4, beginning `flood["start"]` s after `start_at`.
7. Waits for all processes (with a timeout), then always kills leftovers and stops the server
   (`finally`).
8. Reads all JSON results, then `summarize`, adding bulk Mbps and flooder Mbps.
9. Saves `controller_state.json` (from `/chat/state`) and `summary.json`, and records the
   controller's `[FLOOD]` transitions, which prove when demotion happened.

#### The two experiments

**`scaling`** (lines 203–221): for each user count in `--users` (10 25 50 100), a new
Testbed, then 4 variants:

| Variant | Mode | Monitor | Bulk |
|---|---|---|---|
| static-idle | static | off | no |
| static-bulk | static | off | **yes** |
| sdn-idle | sdn | on | no |
| sdn-bulk | sdn | on | **yes** |

There are 16 runs of 20 s at 2 messages/s per user. The output is `results/scaling.csv`.

**`flood`** (lines 224–249): 20 users (4 VIP) chat for 60 s. A flooder sends 7 KB
messages from t = 15 s to t = 45 s, in 3 variants: `static`, `sdn-nomonitor`, `sdn-monitor`.
The outputs are `results/flood_summary.csv`, plus `flood_timeseries.csv` with every RTT and
its time, for the timeline plot, and `flood_meta.json` with the flood start and end.

**`main`** checks root, deletes old raw results for the chosen experiment, runs it, and then
`chown`s everything in `results/` back to the normal user. Files created by root
would otherwise be read-only for you.

### 4.5 `experiments/robustness.py`: challenging scenarios

Both scenarios use `run_load()`: 8 users (2 per host), all in one room, 5
messages/s, 12 s, with a function `during(start_at)` running in a thread at the
same time.

**Block scenario** (`block_scenario`):

1. At t = 5 s the "admin" calls `POST /policy/block {"user": "user_5"}`.
2. Waits until the server log says `user_5 disconnected`. The server sees only
   silence, so it's reaped by `TCP_USER_TIMEOUT` after ~20 s.
3. **Re-login while blocked:** a small script (`RELOGIN`) on c2 logs in as user_5 and sends
   two messages, one immediately and one after 0.5 s.
4. Unblock, wait for the second silent session to be reaped, then re-login again.
5. **Pass** if all of these hold:
   - the other 7 users have 0 unacked
   - user_5's last ack is no later than 0.5 s after the block
   - user_5 has unacked messages
   - the 0.5 s message while blocked failed
   - the message after unblock succeeded

**Link scenario** (`link_scenario`):

1. At t = 4 s: `net.configLinkStatus("s1", "s2", "down")`. Both trunk interfaces go down.
2. 3 s later: `"up"`.
3. **Pass** if `unacked == 0` and `delivery_loss == 0`. TCP retransmits everything,
   with no application-level reconnect.
4. Writes `results/link_failure_timeseries.csv` (RTT over time) for the plot.

The output is `results/robustness.json`, and the script prints `block: PASS, link failure: PASS`.

### 4.6 `experiments/plot.py`: the graphs

Reads the CSV/JSON files in `results/` and writes 5 PNGs to `results/plots/`. It doesn't
need root, but does need matplotlib:
```bash
uv run --no-project --with matplotlib python experiments/plot.py
```

| Plot | Function | Shows |
|---|---|---|
| `latency_vs_users.png` | `latency_vs_users` | p50 and p95 RTT vs. users, static vs. SDN; left panel idle, right panel with bulk |
| `bulk_throughput.png` | `bulk_throughput` | bulk Mbps in static vs. SDN, showing bulk isn't starved |
| `flood_timeline.png` | `flood_timeline` | one panel per variant: VIP and regular median RTT per second over time (log scale), with the flood period shaded |
| `vip_vs_regular.png` | `vip_vs_regular` | bar chart of VIP vs. regular p95 RTT during the flood, per variant (log scale) |
| `link_failure.png` | `link_failure` | RTT over time, with the 3 s outage marked |

Colours are fixed per series (static = orange, SDN = blue, SDN monitor-off = green),
so the same variant has the same colour in every plot.

---

## 5. Code walkthrough: the end-to-end check

### `tests/mininet_check.py`: 15 checks on the real network

It starts its own controller, builds the topology, runs real clients and reads the
real flow tables and tc counters.

| # | Check | How it's verified |
|---|---|---|
| 1 | pingall across s1–s2 | `net.pingAll()` = 0% loss |
| 2 | controller learned alice from the login event | `/chat/state` has alice with room `lobby` on her client port |
| 3 | regular user's chat flow uses queue 2 | s1 flow for alice's port has `set_queue:2` |
| 4 | 4 chat flows per connection | 2 switches × 2 directions |
| 5 | bulk flow uses queue 3 | a priority-50 flow has `set_queue:3` |
| 6 | tc: bulk bytes went through queue 3 | q3 counter on `s1-eth6` grew > 1 MB during bulk |
| 7 | tc: chat bytes went through queue 2 | q2 counter grew |
| 8 | tc: VIP chat bytes went through queue 1 | q1 counter grew |
| 9 | bulk throughput limited by the 10 Mbps trunk | 5 < Mbps < 10.5 |
| 10 | vip_ login moved its flow to queue 1 | vip_bob's flow has `set_queue:1` |
| 11 | block installs drop flow | alice's flow ends in `actions=drop` |
| 12 | unblock restores queue 2 | back to `set_queue:2` |
| 13 | static mode: both users in queue 2 | after `POST /mode static` |
| 14 | back to SDN mode: VIP in queue 1 again | after `POST /mode sdn` |
| 15 | server saw both users log in over Mininet | server log has `alice logged in from 10.0.0.11` and `vip_bob ... 10.0.0.12` |

Checks 6–8 are the strongest evidence. They don't just trust the flow rule. They
show **bytes actually moved through each HTB queue** in the kernel.

---

## 6. Results and what they mean

All numbers are from one full run on the 2-vCPU VM (2026-10-25). **Every run delivered every
message:** 0 unacked, 0% loss, in all 16 scaling runs and all 3 flood runs.

### 6.1 Scaling: chat RTT (ms, p50 / p95) vs. users

| Users | Static, idle | SDN, idle | **Static + bulk** | **SDN + bulk** |
|---|---|---|---|---|
| 10 | 1.7 / 3.3 | 1.9 / 4.1 | 53.4 / 75.9 | **1.5 / 2.9** |
| 25 | 1.6 / 3.7 | 1.7 / 3.8 | 23.7 / 35.1 | **1.2 / 3.4** |
| 50 | 1.4 / 3.9 | 1.5 / 4.8 | 24.4 / 31.2 | **1.1 / 2.8** |
| 100 | 1.3 / 4.9 | 1.4 / 5.7 | 26.1 / 33.5 | **1.2 / 5.0** |

How to read it:

- **Idle, both modes the same (~1.5 ms):** SDN policy costs nothing when there's no
  congestion.
- **Static + bulk is slow (24–53 ms median):** chat shares one FIFO with bulk, and waits
  behind up to 100 queued packets (section 2.6: up to 120 ms).
- **SDN + bulk stays at idle speed:** bulk is in q3, chat in q1/q2, with separate FIFOs.
  p95 is **7–26× lower** than static.
- **Bulk isn't starved:** it gets 8.4–8.7 Mbps in both modes. HTB is work-conserving
  and chat needs only a few hundred kbit/s.
- **Server scales linearly:** 20 → 200 acked messages per second, with no errors.
- **Login time grows with users** (0.1 s → 1.2–1.5 s): that's PBKDF2 on 2 CPUs, not the network.

### 6.2 Flood: RTT during the flood (ms, p50 / p95)

| Variant | Regular users | VIP users |
|---|---|---|
| Static | 142.1 / 177.9 | 142.0 / 174.9 |
| SDN, monitor off | 144.5 / 173.5 | **1.4 / 3.8** |
| SDN, monitor on | **1.5 / 72.5** | **1.6 / 3.9** |

- **Static:** the flooder (~9.4 Mbps) fills the one queue, and everyone waits ~142 ms.
- **SDN, monitor off:** VIPs are in q1, away from the flooder, so they're fine. Regular users
  share q2 with the flooder (it's regular-tier), so they're slow.
- **SDN, monitor on:** after 2 polls above 2 Mbps the controller logs
  `flooder rate 9.56 Mbps > 2.00 Mbps for 2 polls: demoted to LOW`. Regular users go
  44 → 94 → 129 ms in the first 3 s, then drop to ~1.5 ms. The p95 of 72.5 ms comes
  entirely from those first ~3 s. The flooder is restored after 5 quiet polls once it stops.
- **The flooder still gets ~9.4 Mbps in every variant.** It's moved, not punished.

### 6.3 Robustness

| Scenario | Result |
|---|---|
| Block | user_5 blocked at 5.00 s, last ack 4.87 s; other 7 users 0 unacked; silent session reaped after 21 s; re-login while blocked: message after 0.5 s times out; after unblock: works. **PASS** |
| Link failure | trunk down 4.0–7.0 s; 480 messages, 0 unacked, 0 loss, max RTT 3.4 s, no reconnects. **PASS** |

The **3.4 s max RTT** is TCP retransmission with exponential backoff: messages sent
during the outage wait for the next retransmission after the link returns.

---

## 7. Limitations

- **Everything is on one VM with 2 vCPUs.** Hosts share the CPU, so at 100 users the
  CPU (PBKDF2, Python threads) affects timing as much as the network. The absolute
  numbers depend on the machine. **The static vs. SDN comparison is what matters.**
- **Single bottleneck, single path.** It's a tree topology. The link test shows TCP riding
  out an outage, not rerouting around it.
- **Delivery latency needs a shared clock.** That works in Mininet, but real machines would need
  clock sync (NTP/PTP).
- **No real propagation delay.** It's LAN-like timing, not internet-scale.
- **One run per configuration,** not repeated runs with confidence intervals.

---

## 8. Viva questions for Part 3

**What is Mininet, and why use it?**
A network emulator. It creates hosts as Linux network namespaces, links as veth
pairs and switches as Open vSwitch, all on one machine, running real protocols
and real applications. It's free, fast and gives a repeatable network for fair
comparisons.

**Emulation vs. simulation?**
Emulation runs real code and a real TCP stack in real time. Simulation models them in a program.
Mininet is emulation.

**Why limit the trunk to 10 Mbps?**
To create a bottleneck. Virtual links never congest, so without one, priority would
make no difference. All chat and bulk traffic crosses the trunk.

**What is queueing delay, and how big can it get here?**
Time spent waiting behind other packets. 100 packets × 1500 B on 10 Mbps = 120 ms.

**Why `txqueuelen 100`?**
The default of 1000 would allow 1.2 s of queueing at 10 Mbps (bufferbloat), which is
unrealistic for a LAN.

**What is HTB?**
Hierarchical Token Bucket, a Linux traffic shaper. Classes have a guaranteed rate and
a ceiling, can borrow unused bandwidth (lower priority number first) and each has its own
FIFO queue.

**Why does SDN help under congestion?**
Bulk TCP keeps the queue full. With separate queues, chat doesn't wait behind bulk
packets. Its delay goes from ~25–50 ms to ~1.5 ms.

**Does bulk lose bandwidth because of the priority?**
No. HTB is work-conserving, and bulk still gets 8.4–8.7 Mbps because chat uses very little.

**What makes static a fair baseline?**
Same topology, same links, same queues, same load. Only the controller's policy differs
(everything goes in q2).

**How do you know traffic really used the right queue?**
`mininet_check.py` reads the kernel's tc byte counters on the trunk. Bulk bytes appear in
q3, regular chat in q2 and VIP chat in q1.

**Why percentiles, not averages?**
Averages hide the slow moments. p95/p99 show what users feel when things go wrong.

**What happens when the link fails?**
TCP retransmits. Messages sent during the 3 s outage arrive late (max 3.4 s) and none
are lost. The app doesn't need to reconnect.

**How do 100 users run on 4 hosts?**
Each user is its own TCP connection with its own port. The controller and switches treat them as
separate flows. The bottleneck is the same either way.

**What would you improve?**
Repeated runs with confidence intervals, multiple paths with rerouting on link failure,
real machines, and real delay on links (`tc netem`).
