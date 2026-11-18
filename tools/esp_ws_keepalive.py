#!/usr/bin/env python3
"""Check whether the device's /ws endpoint honours WebSocket control pings,
and whether it accepts permessage-deflate.

Why this matters: the `websockets` client library used by the integration sends
a protocol-level PING every 20 s by default and declares the connection dead if
no PONG arrives within another 20 s. If the ESP's server never answers pings,
every connection is killed ~20-40 s after it opens. The integration then
reconnects, and because its `disconnect()` early-returns when `_connected` is
already False, the old socket is never closed. That is a self-sustaining
reconnect loop -> leaked PCBs -> dead HTTP service.

Usage:
    python tools/esp_ws_keepalive.py --host 192.168.1.91 --hold 60
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


async def probe(host: str, port: int, path: str, hold: float, ping_every: float) -> int:
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(
        (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "Sec-WebSocket-Extensions: permessage-deflate; client_max_window_bits\r\n"
            "User-Agent: esp-probe/1.0\r\n\r\n"
        ).encode()
    )
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=8)
    text = head.decode(errors="replace")
    print("handshake:")
    for line in text.strip().splitlines():
        print(f"  {line}")

    deflate = "permessage-deflate" in text.lower()
    print(
        f"\npermessage-deflate accepted: {deflate}"
        f"{'  <-- costs the ESP heap on every frame' if deflate else ''}"
    )

    if b"101" not in head.split(b"\r\n", 1)[0]:
        writer.close()
        return 2

    # request readings like the integration does
    writer.write(bytes([0x82, 0x81]) + b"\x00\x00\x00\x00" + bytes([103]))
    await writer.drain()

    print(f"\nholding for {hold:.0f}s, ping every {ping_every:.0f}s ...")
    pongs = 0
    data_frames = 0
    started = time.monotonic()
    next_ping = started + ping_every
    buf = b""

    try:
        while time.monotonic() - started < hold:
            timeout = max(0.05, min(next_ping, started + hold) - time.monotonic())
            try:
                chunk = await asyncio.wait_for(reader.read(1024), timeout=timeout)
            except asyncio.TimeoutError:
                chunk = b""
            if chunk:
                buf += chunk
                # frame scan
                while len(buf) >= 2:
                    b0, b1 = buf[0], buf[1]
                    opcode = b0 & 0x0F
                    length = b1 & 0x7F
                    offset = 2
                    if length == 126:
                        if len(buf) < 4:
                            break
                        length = int.from_bytes(buf[2:4], "big")
                        offset = 4
                    elif length == 127:
                        if len(buf) < 10:
                            break
                        length = int.from_bytes(buf[2:10], "big")
                        offset = 10
                    if len(buf) < offset + length:
                        break
                    payload = buf[offset : offset + length]
                    buf = buf[offset + length :]
                    if opcode == 0x9:
                        pongs += 1
                        print(
                            f"  t+{time.monotonic() - started:5.1f}s server sent PING "
                            f"({len(payload)}B)"
                        )
                    elif opcode == 0xA:
                        pongs += 1
                        print(f"  t+{time.monotonic() - started:5.1f}s server PONG")
                    elif opcode == 0x8:
                        print(
                            f"  t+{time.monotonic() - started:5.1f}s server CLOSED "
                            f"code={int.from_bytes(payload[:2], 'big') if len(payload) >= 2 else '?'}"
                        )
                        return 0
                    else:
                        data_frames += 1

            if time.monotonic() >= next_ping:
                # masked ping, empty payload
                writer.write(bytes([0x89, 0x80]) + b"\x00\x00\x00\x00")
                await writer.drain()
                print(f"  t+{time.monotonic() - started:5.1f}s -> sent PING")
                next_ping += ping_every
    except (ConnectionResetError, ConnectionAbortedError) as err:
        print(f"\nconnection reset after {time.monotonic() - started:.1f}s: {err}")
        return 2
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    print(
        f"\nresult: {pongs} PONG(s) received, {data_frames} data frame(s) in "
        f"{time.monotonic() - started:.0f}s"
    )
    if pongs == 0 and data_frames == 0:
        print(
            "VERDICT: server answers neither PING nor PONG -> the websockets "
            "client will declare this dead after ping_interval+ping_timeout"
        )
    elif pongs == 0:
        print(
            "VERDICT: server sends data but no PONG -> keepalive will kill the "
            "connection; pass ping_interval=None"
        )
    else:
        print("VERDICT: keepalive works")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=80)
    p.add_argument("--path", default="/ws")
    p.add_argument("--hold", type=float, default=60.0)
    p.add_argument("--ping-every", type=float, default=5.0)
    args = p.parse_args()
    return asyncio.run(
        probe(args.host, args.port, args.path, args.hold, args.ping_every)
    )


if __name__ == "__main__":
    sys.exit(main())
