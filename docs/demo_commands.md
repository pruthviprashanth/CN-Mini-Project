# Demo commands

Every terminal starts in the project folder:

```bash
cd ~/Desktop/CN-Mini-Project
```

---

## Part A: Start the application (T1–T4)

### T1: SDN controller
```bash
sudo mn -c
~/ryu-venv/bin/ryu-manager sdn/controller.py
```
Wait for `chat controller: mode=sdn monitor=True`. Leave it running: this is the log to show.

### T2: Network + chat server
```bash
sudo python3 topo/topology.py demo
```
Wait for `0% dropped` and the `mininet>` prompt. T1 shows both switches connected.

### T3: Regular user on c1 (queue 2, NORMAL)
```bash
sudo topo/mx.sh c1 python3 chat/client.py --user alice --password pw1234 --register
```

### T4: VIP user on c2 (queue 1, HIGH)
```bash
sudo topo/mx.sh c2 python3 chat/client.py --user vip_bob --password pw1234 --register
```
T1 logs `[CLASS] vip_bob@10.0.0.12:... NORMAL -> HIGH (login)`.

Leave out `--register` if the user already exists in this run.

---

## Part B: Run the experiments

Use a spare terminal (**T5**) for these commands unless stated otherwise.

### B0. One-click traffic UI (the easiest way to show the policies)
In a new terminal (the demo from Part A must be running):
```bash
sudo python3 ui/server.py
```
Open **http://127.0.0.1:8000** in the VM's browser and press **Start traffic**. Eight
simulated users start chatting (2 VIP, 3 regular, 1 regular in `announcements`,
2 guests) with a bulk transfer filling the trunk.

| Look at | What it shows |
|---|---|
| the 3 cards | per class: users, median/p95 RTT, trunk Mbps of that queue |
| the Users table | why the policy chose each class, and the queue actually applied |
| **SDN priority / Static baseline** | switches the mode live: all RTTs jump to ~55 ms in static |
| Controller log | every class change, block, flood demotion, mode change |

With bulk on and SDN mode: HIGH and NORMAL stay near 1 ms, LOW (guests) goes to
50-60 ms. Untick *Background bulk transfer* before starting to show that priority
changes nothing on an idle link. `curl -XPOST localhost:8080/policy/block -d '{"user":"user_2"}'`
turns that user to `DROP` / *stalled*; unblock restores it. Press **Stop traffic** to
end; Ctrl-C the UI server when done.

### B1. Chat between users
Type in T3 or T4:

| Do | Type |
|---|---|
| message to the room | `hello everyone` |
| private message | `/pm alice psst` |
| join a high-priority room | `/join announcements` |
| leave it again | `/leave announcements` |
| list rooms / online users | `/rooms`  `/users` |
| measure round-trip time | `/ping` |

### B2. Show that VIP and regular users get different queues
```bash
sudo ovs-ofctl -O OpenFlow13 dump-flows s1 | grep priority=100
```
alice's lines (10.0.0.11) show `set_queue:2`, vip_bob's (10.0.0.12) show `set_queue:1`.

```bash
curl -s localhost:8080/chat/state | python3 -m json.tool | less
```
Shows each connection with its user, rooms, class and queue (`q` to quit).

### B3. Static baseline vs. SDN priority
```bash
curl -s -XPOST localhost:8080/mode -d '{"mode":"static"}'
```
Send one message from each client, then:
```bash
sudo ovs-ofctl -O OpenFlow13 dump-flows s1 | grep priority=100     # everyone set_queue:2
curl -s -XPOST localhost:8080/mode -d '{"mode":"sdn","monitor":true}'
```
Send a message from each client again, and vip_bob is back in queue 1.

### B4. Block and unblock a user
```bash
curl -s -XPOST localhost:8080/policy/block -d '{"user":"alice"}'
sudo ovs-ofctl -O OpenFlow13 dump-flows s1 | grep priority=100     # alice: actions=drop
```
alice's messages now time out, and vip_bob is unaffected.
```bash
curl -s -XPOST localhost:8080/policy/unblock -d '{"user":"alice"}'
```

### B5. Congestion with bulk traffic
Make sure the mode is SDN (B3). Use two more terminals and **start the sink first**:
```bash
# T6: receiver (Ctrl-C to stop)
sudo topo/mx.sh sink python3 experiments/bulk.py sink

# T7: sender, prints its throughput (about 8.8 Mbps) when done
sudo topo/mx.sh bulk python3 experiments/bulk.py source --duration 30
```
While it runs:
```bash
sudo ovs-ofctl -O OpenFlow13 dump-flows s1 | grep priority=50      # bulk: set_queue:3
tc -s class show dev s1-eth6                                         # bytes per queue (1:2=q1, 1:3=q2, 1:4=q3)
```
Type `/ping` in T3/T4 during the transfer. Repeat in static mode to compare.

### B6. Flood detection
```bash
sudo topo/mx.sh c4 python3 experiments/load_client.py --flood --duration 20
```
Within a few seconds T1 logs
`[FLOOD] flooder@10.0.0.14:... rate ... Mbps > 2.00 Mbps for 2 polls: demoted to LOW`,
and after it stops, `restored`.

### B7. Show the recorded results
```bash
xdg-open results/plots/latency_vs_users.png     # static vs SDN chat latency under bulk load
xdg-open results/plots/flood_timeline.png       # flooder demoted, regular users recover
xdg-open results/plots/vip_vs_regular.png       # VIP vs regular during a flood
xdg-open results/plots/bulk_throughput.png      # bulk is not starved
xdg-open results/plots/link_failure.png         # trunk down 3 s, no message lost
```

### B8. Re-run the full experiments (optional, demo must be stopped)
These start their own controller and network. Stop the demo first (see Part D),
then:
```bash
sudo python3 experiments/run_experiments.py all        # ≈15 min: scaling + flood
sudo python3 experiments/run_experiments.py scaling    # or one at a time
sudo python3 experiments/run_experiments.py flood
sudo python3 experiments/robustness.py                 # ≈2 min: block + link failure
uv run --no-project --with matplotlib python experiments/plot.py   # redraw results/plots/*.png
```
This overwrites `results/`. Copy the folder first to keep the current results.

### B9. Automated tests (demo must be stopped)
```bash
python3 -m unittest discover -s tests -t .                 # 50 tests, no Ryu needed
~/ryu-venv/bin/python -m unittest discover -s tests -t .   # all 68 tests
sudo python3 tests/mininet_check.py                        # 15 end-to-end checks
```

---

## Part C: Changes the examiner may ask for

**Before editing, make a backup:** `cp sdn/policy.json sdn/policy.json.bak`
(undo: `cp sdn/policy.json.bak sdn/policy.json`, then reload).

**Check the JSON after editing:** `python3 -m json.tool sdn/policy.json`
prints the file, or the line with the error (usually a missing or extra comma).

### Live changes (no restart)

#### C1. Make a user VIP / create a new user type
```bash
nano sdn/policy.json
```
Add a line under `user_tiers`. Every line except the last needs a comma:
```json
"user_tiers": {
  "admin":   "HIGH",
  "vip_*":   "HIGH",
  "guest_*": "LOW",
  "alice":   "HIGH"
}
```
Other examples: `"staff_*": "HIGH"` (new prefix), `"bob": "LOW"` (demote one user).
Save with Ctrl-O, Enter, then exit with Ctrl-X. Apply and check:
```bash
curl -s -XPOST localhost:8080/policy/reload
sudo ovs-ofctl -O OpenFlow13 dump-flows s1 | grep priority=100     # alice: set_queue:1
```
T1 logs `[CLASS] alice@... NORMAL -> HIGH (policy reload)`.

#### C2. Make a room high priority
Under `room_classes` in `sdn/policy.json`:
```json
"room_classes": {
  "announcements": "HIGH",
  "ops": "HIGH",
  "project": "HIGH"
}
```
```bash
curl -s -XPOST localhost:8080/policy/reload
```
Anyone who types `/join project` becomes HIGH.

#### C3. Change the flood limit or detection speed
Under `monitor` in `sdn/policy.json`:
```json
"monitor": {
  "poll_interval": 1,
  "flood_bps": 1000000,
  "flood_intervals": 3,
  "recover_intervals": 5
}
```
| Key | Meaning |
|---|---|
| `poll_interval` | seconds between checks |
| `flood_bps` | flood limit in bits per second (1000000 = 1 Mbps) |
| `flood_intervals` | polls in a row above the limit before demoting |
| `recover_intervals` | polls in a row below half the limit before restoring |

```bash
curl -s -XPOST localhost:8080/policy/reload
sudo topo/mx.sh c4 python3 experiments/load_client.py --flood --duration 20     # test it
```

#### C4. Block a user permanently
Add the name to `"blocked_users": ["alice"]` in `sdn/policy.json`, then reload.
Removing a name from the list and reloading does **not** unblock it. Use:
```bash
curl -s -XPOST localhost:8080/policy/unblock -d '{"user":"alice"}'
```

#### C5. Turn priority or flood detection off/on
```bash
curl -s -XPOST localhost:8080/mode -d '{"mode":"static"}'                  # no priority
curl -s -XPOST localhost:8080/mode -d '{"mode":"sdn","monitor":false}'      # priority, no flood detection
curl -s -XPOST localhost:8080/mode -d '{"mode":"sdn","monitor":true}'       # everything on
curl -s localhost:8080/health                                               # current mode
```

### Changes that need a restart

To restart the network: `/quit` the clients, `exit` at `mininet>` (T2), then run
T2's command again with the new option and log the clients in again. The
controller in T1 can keep running. **Don't** run `sudo mn -c` while it runs,
because that kills it.

#### C6. Change the trunk speed
```bash
sudo python3 topo/topology.py demo --trunk-mbps 5
```

#### C7. Add more client computers
```bash
sudo python3 topo/topology.py demo --clients 8
sudo topo/mx.sh c7 python3 chat/client.py --user dave --password pw1234 --register
```

#### C8. Run without the queues
```bash
sudo python3 topo/topology.py demo --no-qos
```
No rate limit and no queues; use it only to show the difference.

#### C9. Change the queue percentages
Edit `topo/topology.py` line 28, `(guaranteed share, HTB priority)` per queue:
```python
QUEUE_SHARES = {1: (0.20, 1), 2: (0.50, 2), 3: (0.10, 3)}
```
For example, `3: (0.05, 3)` gives LOW only 5%. Restart T2, then check:
```bash
tc -s class show dev s1-eth6
```

#### C10. Start the controller differently
Ctrl-C in T1, then one of:
```bash
CHAT_MODE=static ~/ryu-venv/bin/ryu-manager sdn/controller.py            # start in static mode
CHAT_MONITOR=0   ~/ryu-venv/bin/ryu-manager sdn/controller.py            # flood detection off
CHAT_POLICY=my_policy.json ~/ryu-venv/bin/ryu-manager sdn/controller.py  # another policy file
```

#### Not a quick change
The chat port (5000) is set in the server, the client and `policy.json`. If
asked, explain where it's set rather than changing it live.

---

## Part D: Stop everything

| Where | Type |
|---|---|
| T3, T4 (clients) | `/quit` |
| T6 (bulk sink, if used) | Ctrl-C |
| T2 (`mininet>`) | `exit` |
| T1 (controller) | Ctrl-C |
| any terminal | `sudo mn -c` |

### If something goes wrong

| Problem | Fix |
|---|---|
| controller says `cannot remove stale event socket` | `sudo rm -f /tmp/chat_sdn_events.sock`, start T1 again |
| `pingall` fails or switches don't connect | stop everything, `sudo mn -c`, start T1 **first**, then T2 |
| `mx.sh` says `no running Mininet host` | T2 isn't running |
| `Address already in use` on port 8080 | an old controller is running: `sudo pkill -f ryu-manager` |
| policy reload does nothing | check the JSON: `python3 -m json.tool sdn/policy.json` |
