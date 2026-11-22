# Test cases

Automated suites (run them; output is pasted at the bottom of this file):

| suite | command | needs | count |
|---|---|---|---|
| protocol + server on loopback | `python3 -m unittest discover -s tests -t .` | nothing | 36 (+ policy) |
| policy + controller (stub datapath) | `~/ryu-venv/bin/python -m unittest discover -s tests -t .` | Ryu venv | 68 total |
| end-to-end on Mininet | `sudo python3 tests/mininet_check.py` | root | 15 checks |
| robustness scenarios | `sudo python3 experiments/robustness.py` | root | 2 scenarios |

## Manual / end-to-end test table

| # | feature | steps | expected | actual |
|---|---|---|---|---|
| 1 | Mininet connectivity | `topology.py check` (pingall through the controller) | 0% loss | 0% loss (7 hosts, 42/42) |
| 2 | Registration | `/register alice pw1234` | `registered alice`; hash, not password, in users.json | as expected (`test_password_hashed_and_persisted`) |
| 3 | Duplicate / invalid registration | register alice twice; user `ab`; password `abc` | `user already exists`; `invalid username`; `password must be 4-128` | as expected |
| 4 | Login, wrong password | `/login alice wrong` | `invalid username or password` | as expected |
| 5 | Duplicate login | log in as alice from a 2nd client | `user already logged in elsewhere` | as expected |
| 6 | Room chat | alice and bob `/join project`, bob sends | only project members receive; no echo to sender | alice received `[project] bob: ...`, carol did not |
| 7 | Private message | `/pm alice psst` from carol | only alice receives | as expected |
| 8 | PM to offline / unknown user | `/pm bob hi` after bob quits; `/pm ghost hi` | `user bob is offline`; `no such user ghost` | as expected |
| 9 | Disconnect cleanup | kill a client process | others get `bob left ...`; removed from `/users`; can log in again | as expected |
| 10 | Malformed / oversized frame | send `garbage\n`; send 64 KB+ without newline | `error`, connection kept; `error` + connection closed | as expected |
| 11 | 30 concurrent clients | `test_concurrent_clients` | all 150 messages delivered | 150/150 |
| 12 | Regular user → queue 2 | log in as alice on c1, dump s1 flows | `tp_dst=5000 actions=set_queue:2,output:6` | as expected |
| 13 | Bulk → queue 3 | `bulk.py source` on bulk host | `priority=50 ... set_queue:3`; tc q3 counter grows | q3 +5.5 MB during 5 s; chat bytes in q1/q2 |
| 14 | VIP login → queue 1 (app event changes network) | log in as `vip_bob` | controller logs `NORMAL -> HIGH (login)`; flows `set_queue:1` | as expected, 4 flows (2 switches × 2 directions) re-installed |
| 15 | Room raises priority | regular user `/join announcements`, then `/leave` | HIGH (queue 1), then back to NORMAL | as expected (`test_room_raises_and_leave_restores`) |
| 16 | Admin block | `POST /policy/block {"user":"alice"}` | alice's flows become `actions=drop`; others unaffected | user_5's last ack at 4.87 s, block at 5.00 s; 0 unacked for the other 7 users |
| 17 | Blocked user re-login | log in again while blocked | dropped on the login event | login reply and a request sent within ms can slip through (race); request after 0.5 s times out |
| 18 | Unblock | `POST /policy/unblock` | flows back to queue 2; user can chat | as expected |
| 19 | Static baseline | `POST /mode {"mode":"static"}` | flows flushed; all chat `set_queue:2`, even VIP | as expected |
| 20 | Flood demotion | flooder sends 7 KB messages in a loop | after 2 polls >2 Mbps: `[FLOOD] ... demoted to LOW`; regular users recover | demoted ~3 s after flood start; regular median RTT ~130 ms → ~1.5 ms |
| 21 | Flood recovery | flooder stops | restored to NORMAL after 5 quiet polls | as expected |
| 22 | Link failure | trunk down 3 s during chat | RTT spike, no loss, no reconnect | 480 sent, 0 unacked, 0 delivery loss, max RTT 3.4 s |
| 23 | Controller down | stop Ryu while server runs | chat server keeps working (events dropped) | `test_controller_down_does_not_block_chat` |
| 24 | Event before first packet | login event arrives before packet-in | first flow installed with the right class | `test_event_before_packet_in` |
| 25 | Counter reset | flow re-added during monitoring | no negative rate | `test_counter_reset_not_negative` |
| 26 | Guest stays LOW after auto-join | log in as `guest_1`; it auto-joins `general` | class LOW, queue 3 (not lifted to NORMAL) | `ShippedPolicyTest`; UI shows `guest_* → LOW`, LOW queue |
| 27 | UI traffic on/off | `sudo python3 ui/server.py`, Start, then Stop | 8 users log in, per-class RTT and queue Mbps shown; Stop logs everyone out, no processes left | as expected (0 leftover processes, 0 open connections) |
| 28 | UI static vs SDN with bulk | Start with bulk, switch mode | SDN: HIGH/NORMAL ~1 ms, LOW ~60 ms; static: all in q2 ~55 ms | as expected |

## Last automated run (2026-10-25)

```
python3 -m unittest discover -s tests -t .            Ran 68 tests  OK (skipped=18: need Ryu)
~/ryu-venv/bin/python -m unittest discover -s tests -t .  Ran 68 tests  OK
sudo python3 tests/mininet_check.py                    15/15 checks passed
sudo python3 experiments/robustness.py                 block: PASS, link failure: PASS
```
