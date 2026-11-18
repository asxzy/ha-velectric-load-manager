# Validating the "retry storm reboots the ESP" hypothesis

Goal: prove or disprove that repeatedly hitting the device's HTTP service causes
the chip to wedge and/or reboot, and pin down *which* resource runs out.

Everything here is bounded, rate limited, and stops at the first failure. Nothing
writes to the device.

## What has already been observed (2026-11-17)

Two devices, `192.168.1.91` and `192.168.1.65`, on the same firmware family but
with **different websocket behaviour**:

| Probe | `.91` | `.65` |
|---|---|---|
| ICMP ping | replies, ~1 ms | replies, ~110 ms |
| TCP :80 accept | yes | yes |
| `GET /ws` upgrade | `101` | `101` |
| Readings without any request | **yes, ~1 Hz** | **no, silent** |
| Readings after one `0x67` | yes | **yes, exactly one** |
| `GET /style.css` | **times out** | **times out** |
| `Sec-WebSocket-Accept` | RFC 6455 correct | RFC 6455 correct |

So `.91` is a **push** device and `.65` is strictly **request/response**. Any
fix must handle both — removing the request loop outright would leave `.65`
with no data at all.

Both units are currently in the state where **the network stack is alive, the
WebSocket service is alive, and the HTTP service answers nothing**. ICMP never
drops, so the chip is stuck rather than crashing (except when it does reboot;
see the watchdog log below).

A 20-minute watchdog run on `.65` caught a genuine reboot:

```
21:16:02  http_dead_ws_ok -> tcp_accept_no_response   [was 147.7s]
21:16:39  tcp_accept_no_response -> http_dead_ws_ok   [was 38.6s]
21:24:38  http_dead_ws_ok -> icmp_down                [was 106.0s]
21:26:23  icmp_down -> http_dead_ws_ok                [was 107.5s]
!! 3 samples with no ICMP at all -> the device was fully down (reboot signature)
```

That is the reboot you suspected, and it happened without any client traffic
beyond a probe every 20 s. The device is not stable on its own.

Response framing when the HTTP side *was* answering:

```
HTTP/1.1 200 OK
Content-Length: 4309
Content-Type: text/css
Connection: close          <-- promises to close
Accept-Ranges: none
```

...and then the connection stays open forever. Body bytes arrive, but no FIN is
ever sent. A client that honours `Connection: close` by reading until EOF blocks
until its own timeout, holding one TCP slot for the entire time.

That is the mechanism worth validating, because it predicts exactly the
observation above: many clients waiting for an EOF that never comes will eat the
whole PCB pool, after which TCP still accepts (the listen backlog) and `/ws`
still works, but nothing new can be served.

## Tools

All are plain standard library, run with the project venv:

```sh
.venv/bin/python tools/<tool>.py --help
```

| Tool | Question it answers |
|---|---|
| `esp_watchdog.py` | How does it fail, and does ICMP ever drop (= reboot)? |
| `esp_probe.py health` | Is the service up over time? Outage list with durations. |
| `esp_probe.py storm` | Does N attempts/second reproduce the failure? |
| `esp_probe.py mock` | Local fake ESP for testing the integration safely. |
| `esp_socket_ceiling.py` | How many simultaneous connections before HTTP dies? |
| `validate_retry_impact.py` | strict vs EOF-waiting client: which one wedges it? |
| `esp_recovery_cost.py` | How many seconds does **one** request cost? |
| `esp_ws_keepalive.py` | Does `/ws` honour pings? Is deflate enabled? |
| `repro_reconnect_leak.py` | Does the integration's own client leak sockets? |
| `verify_client_footprint.py` | Does the rewritten client stay at 1 connection / 2 bytes? |
| `test_protocol_detection.py` | Push vs request/response firmware handled correctly? |
| `test_integration_smoke.py` | Do all modules import and shut down cleanly? |
| `test_coordinator_behavior.py` | No polling, coalesced publishes, availability flips? |

## Recommended order

Run these in sequence. Stop as soon as you have an answer; each step is
progressively more invasive.

### 1. Fingerprint the failure (safe, 20 min, 1 sample / 20 s)

```sh
.venv/bin/python tools/esp_watchdog.py \
    --host 192.168.1.91 192.168.1.65 --duration 1200 --interval 20
```

What to look for:

- `icmp_down` at any point → the chip really did reboot. That is your reboot.
- `tcp_accept_no_response` / `http_dead_ws_ok` transitions → resource exhaustion
  without a reboot, i.e. a wedged HTTP task rather than a crash.
- the `held_s` on each transition → how long the device stayed broken.

### 2. Confirm the client-behaviour difference (the decisive test)

```sh
# Wait until /style.css answers, then:
.venv/bin/python tools/validate_retry_impact.py --host 192.168.1.65 --compare
```

`strict` reads exactly `Content-Length` bytes and closes at once. `eof` waits for
the peer to close, which it never does. If `strict` survives 30 requests while
`eof` kills the service in a handful, the missing FIN is confirmed as the
multiplier.

### 3. Measure the pool (only after the device has recovered)

```sh
.venv/bin/python tools/esp_socket_ceiling.py --host 192.168.1.65 --kind tcp --max 8
.venv/bin/python tools/esp_socket_ceiling.py --host 192.168.1.65 --kind ws  --max 8
```

The reported ceiling is the number of slots you have to stay under. Compare the
`tcp` and `ws` runs: if `ws` reaches the same ceiling, then the integration's
long-lived WebSocket *is itself* one of the scarce slots.

### 4. Reproduce the actual retry load (bounded)

```sh
# 1 attempt/s for 60 s, abort after 5 consecutive failures
.venv/bin/python tools/esp_probe.py storm --host 192.168.1.65 --mode ws \
    --rate 1 --duration 60 --abort-after-failures 5
```

If 1/s is harmless, try 2/s, then 4/s. The rate at which it breaks is the budget
you can spend.

### 5. Rule out the integration as a contributor (safe, local only)

```sh
.venv/bin/python tools/repro_reconnect_leak.py --cycles 6
.venv/bin/python tools/test_protocol_detection.py
.venv/bin/python tools/test_integration_smoke.py
.venv/bin/python tools/test_coordinator_behavior.py
```

These run without touching real hardware. `repro_reconnect_leak.py` drove the
original client against a local server and found the client does **not** leak
sockets — `websockets` closes the transport on peer-initiated drops. That result
matters: the pressure came from request rate and concurrency, not a client-side
leak, so the fix targets cadence and connection count rather than cleanup.

### 6. Confirm the rewrite against the devices

```sh
.venv/bin/python tools/verify_client_footprint.py --host 192.168.1.91 --duration 120
.venv/bin/python tools/verify_client_footprint.py --host 192.168.1.65 --duration 120
```

Expect `connections=1` and `sends<=2` on both. `.91` should show readings
arriving continuously; `.65` should show a reading roughly every 8 s.

## Reading the result

| Observation | Conclusion | Action |
|---|---|---|
| ICMP drops, then returns after ~30 s | genuine reboot (panic/brownout/WDT) | shrink concurrency and rate; treat every request as expensive |
| ICMP stays up, HTTP dead, WS alive | PCB/heap exhaustion | stop opening HTTP at all; keep one `/ws` connection |
| HTTP dies at depth *k* | pool size is *k* | never exceed *k*-1 total connections |
| `strict` survives, `eof` dies | missing FIN is the trap | any client must stop at `Content-Length` |
| recovers only on power cycle | leaked PCBs never time out | a restart is the only cure; avoid the state entirely |
| readings arrive unasked | **push firmware** | never poll; hold the socket |
| readings only after `0x67` | **request/response firmware** | poll slowly, ≥8 s |

Both reboot and wedge signatures were observed on real hardware, so the chip is
not stable under any load. The integration's job is to stop being part of the
load.

## What this means for this integration

The original code had a real amplification problem independent of the firmware
bug, and it has now been fixed:

1. **The 2 s request loop was the dominant load.** `PING_INTERVAL = 2.0` sent
   `0x67` every two seconds forever — 30 requests/minute, 43 200/day — to a chip
   whose HTTP task wedges. It was also redundant on `.91`, which pushes by
   itself.

2. **`connect()` retries were gated by the coordinator, not the socket.**
   `_async_update_data` called `self._client.connect()` every scan interval, so
   a device needing tens of seconds to recover received a fresh connection every
   tick. Fixed: the coordinator has `update_interval=None` and one connection is
   held open.

3. **Stale data was indistinguishable from live data.** `get_readings()`
   returned the cache whenever `_connected` was true, and `connection_status`
   read the literal string `"Connected"` regardless of the device's state. Fixed:
   the read loop's timeout is the health signal, and real frames drive the
   entities.

4. **`disconnect()` early-returned** when `_connected` was already false, so the
   cleanup path did nothing and the object was dropped without a close. Fixed:
   `_close_socket()` always closes exactly once.

5. **`scan_interval` never affected device load.** It only set the entity update
   rate while the 2 s loop kept hammering the chip, so raising it "to be gentle"
   did nothing. Removed from the UI rather than left as a lie.

The resulting behaviour, verified against both real devices:

| | before | after |
|---|---|---|
| Connections | new one every scan interval | **1**, held |
| Periodic requests | `0x67` every 2 s, forever | **none** on push firmware |
| Request/response firmware | 2 s polling | polled every 8 s |
| HTTP requests | none, but kept-alive UI traffic hurt | **none, ever** |
| Compression | library default (`deflate`) | **off** |
| Reconnect floor | none (coordinator tick) | **15 s**, doubling to 5 min |
| Bytes sent per connection | — | **2** (`0x69` settings, `0x67` probe) |

`verify_client_footprint.py` confirms it on live hardware: `.91` produced 47
readings from a single connection with **2 bytes written**; `.65` produced
readings on the same connection with only its slow polls.

## Appendix: quick manual checks

```sh
# is TCP even accepting?
nc -z -G 1 -w 1 192.168.1.65 80 && echo open

# does a full HTTP response arrive, and does it close?
curl -sS -m 10 -v http://192.168.1.65/style.css 2>&1 | tail -20

# does /ws handshake and stream?
curl -sS -m 5 -i -N \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  http://192.168.1.65/ws
```

A `curl` that shows `Content-Length` and then hangs until `-m` expires confirms
the missing FIN without any tooling at all.
