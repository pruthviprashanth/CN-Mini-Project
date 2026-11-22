# Chat protocol

Transport: one TCP connection per client to the server (`10.0.0.1:5000`).
Framing: **one UTF-8 JSON object per line**, terminated by `\n`. TCP is a byte
stream with no message boundaries, so both sides run a `LineReader`
(`chat/protocol.py`) that buffers bytes and splits on newlines. A frame larger
than **64 KB** is rejected: the server replies `error` and closes the connection,
because the stream can no longer be trusted to resynchronise.

Every frame has a string `type`. Requests may carry an integer `id`; the reply
echoes it, so a client can have several requests in flight and match replies.

## Client → server

| type | fields | reply | notes |
|---|---|---|---|
| `register` | `user`, `password` | `ok` / `error` | user `[A-Za-z0-9_]{3,20}`, password 4–128 chars; does not log in |
| `login` | `user`, `password` | `ok {user, rooms}` | auto-joins `general`; rejects a second login of an online user |
| `join` | `room` | `ok {room}` | room `[A-Za-z0-9_-]{1,32}`, created on first join |
| `leave` | `room` | `ok {room}` | empty rooms are deleted |
| `msg` | `room`, `text`, `ts` | `ack {id, delivered}` | must be in the room; text 1–8000 chars |
| `pm` | `to`, `text`, `ts` | `ack {id, delivered:1}` | `error` if the target is offline or unknown |
| `rooms` | – | `rooms {rooms:{name:count}, joined:[...]}` | |
| `users` | – | `users {users:[...]}` | online users |
| `ping` | – | `pong {ts}` | allowed before login |
| `quit` | – | `ok`, then the server closes | |

Everything except `register`, `login`, `ping`, `quit` needs a login (`error: login required`).

## Server → client

| type | fields | when |
|---|---|---|
| `ok` | `id`, extra fields | request succeeded |
| `error` | `id`, `reason` | request failed (connection stays open, except oversized frames) |
| `ack` | `id`, `delivered` | a `msg`/`pm` was accepted and queued to `delivered` recipients; used to measure response time |
| `msg` | `room`, `from`, `text`, `ts`, `id` | a room message from another member (no echo to the sender) |
| `pm` | `from`, `text`, `ts`, `id` | a private message |
| `info` | `room`, `text` | someone joined/left a room you are in |
| `rooms` / `users` / `pong` | see above | |

`ts` is the sender's clock at send time; Mininet hosts share one kernel clock, so
`receive_time - ts` is the true one-way delivery latency.

Clients match replies only on the reply types (`ok`, `error`, `ack`, `pong`,
`rooms`, `users`). Pushed `msg`/`pm` frames also carry an `id` (the sender's),
which could otherwise collide with the receiver's own request ids.

## Example

```
C: {"type":"login","user":"vip_alice","password":"pw1234","id":1}
S: {"user":"vip_alice","rooms":["general"],"type":"ok","id":1}
C: {"type":"msg","room":"general","text":"hi","ts":1793000000.12,"id":2}
S: {"type":"ack","id":2,"delivered":3}
S: {"type":"msg","room":"general","from":"bob","text":"hello","ts":1793000000.5,"id":7}
```

## Events to the SDN controller

The server reports application events (`chat/notifier.py`) as one JSON datagram each:

```json
{"event":"login","user":"vip_alice","room":null,"client_ip":"10.0.0.11","client_port":41234,"ts":1793000000.1}
```

Events: `login`, `logout`, `join`, `leave`. `(client_ip, client_port)` is the
client end of the TCP connection, exactly what the controller sees in packet
headers, so the controller can map an app user to a network flow.

Transport is a **Unix datagram socket at `/tmp/chat_sdn_events.sock`**. Mininet
hosts have their own network namespace and cannot reach the controller on
127.0.0.1, but they share the root filesystem, so a socket path works from every
host. The server can also POST to the controller's REST `/chat/event`
(`--controller http://host:8080`) for non-Mininet deployments. Sending is
best-effort: a bounded queue and a background thread, errors swallowed, so a
dead controller never slows the chat.
