# Demo script (rehearsed command order)

Four terminals, all in the project folder (`cd ~/Desktop/CN-Mini-Project`).
Total time ≈ 10 minutes. Commands in **T1–T4** are typed in that terminal.

> Order matters: `sudo mn -c` kills any running `ryu-manager`, so clean up **before** starting the controller.

## 0. Start (≈1 min)

**T1 – controller**
```bash
sudo mn -c
~/ryu-venv/bin/ryu-manager sdn/controller.py
```
Point at: `chat controller: mode=sdn monitor=True`, `listening for chat events on /tmp/chat_sdn_events.sock`.

**T2 – network + chat server**
```bash
sudo python3 topo/topology.py demo
```
Point at: `pingall` 0% dropped. T1 shows `switch 0000000000000001 connected`
and `...02`. The server runs on `srv` (10.0.0.1:5000), log in `/tmp/chat_server.log`.

## 1. Architecture (≈1 min)

Show the diagram in the README. Clients sit on s1 and the server on s2, behind
one 10 Mbps trunk (the bottleneck). The server tells the controller who is who,
and the controller re-programs both switches.

## 2. Chat across Mininet + regular user in queue 2 (≈2 min)

**T3** – regular user on c1
```bash
sudo topo/mx.sh c1 python3 chat/client.py --user alice --password pw1234 --register
```
**T4** – flow table of s1
```bash
sudo ovs-ofctl -O OpenFlow13 dump-flows s1 | grep priority=100
```
Point at: `tp_dst=5000 actions=set_queue:2,output:6`. Alice's chat flow is in
queue 2 (NORMAL).

## 3. App event changes the network: VIP login → queue 1 (≈2 min) – D1 highlight

**T4** – second client, a VIP, on c2 (run in another terminal, or use T4 and re-open)
```bash
sudo topo/mx.sh c2 python3 chat/client.py --user vip_bob --password pw1234 --register
```
T1 logs `[CLASS] vip_bob@10.0.0.12:xxxxx NORMAL -> HIGH (login)`.
```bash
sudo ovs-ofctl -O OpenFlow13 dump-flows s1 | grep priority=100
curl -s localhost:8080/chat/state | python3 -m json.tool | less    # class/queue per connection
```
Chat between them: type `hi bob` in T3 and it appears in T4. `/pm alice psst` in T4
reaches alice only. `/join announcements` makes even a regular user HIGH (rooms
raise priority). `/leave announcements` puts it back.

## 4. Static baseline vs SDN (≈1 min)

```bash
curl -s -XPOST localhost:8080/mode -d '{"mode":"static"}'
# send a message from each client, then:
sudo ovs-ofctl -O OpenFlow13 dump-flows s1 | grep priority=100     # all set_queue:2
curl -s -XPOST localhost:8080/mode -d '{"mode":"sdn","monitor":true}'
```

## 5. Block an unauthorized user (≈1 min)

```bash
curl -s -XPOST localhost:8080/policy/block   -d '{"user":"alice"}'
sudo ovs-ofctl -O OpenFlow13 dump-flows s1 | grep priority=100     # alice: actions=drop
```
Alice's messages now time out (`! connection problem`). Bob is unaffected.
```bash
curl -s -XPOST localhost:8080/policy/unblock -d '{"user":"alice"}'
```

## 6. Results (≈2 min)

Do not re-run the experiments live (≈15 min). Show `results/plots/*.png` and the
tables in the README:
- `latency_vs_users.png`: SDN keeps chat p95 at a few ms under bulk congestion; static goes to tens of ms
- `flood_timeline.png`: with the monitor on, the flooder is demoted in ~2–3 s and regular users recover
- `vip_vs_regular.png`, `bulk_throughput.png`, `link_failure.png`

Optional live mini-check (≈1 min, after closing the demo with `exit` in T2 and Ctrl-C in T1):
```bash
sudo python3 tests/mininet_check.py      # 15 automated end-to-end checks
```

## Cleanup

`exit` in the Mininet CLI (T2), Ctrl-C in T1, then `sudo mn -c`.
