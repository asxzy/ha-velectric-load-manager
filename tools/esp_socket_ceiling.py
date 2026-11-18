#!/usr/bin/env python3
"""Find the device's simultaneous-connection ceiling, and what each slot costs.

An ESP's LWIP stack has a fixed number of TCP PCBs (often 5 in Arduino defaults).
This tool holds connections open one at a time and reports what stops working at
each depth:

  * how many simultaneous TCP connections are accepted
  * whether /ws connections count against the same pool as HTTP
  * which resource dies first (HTTP vs WS) as the pool fills
  * whether slots are returned after a clean close

It never exceeds --max and releases everything at the end, including on Ctrl-C.

Usage:
    python tools/esp_socket_ceiling.py --host 192.168.1.65 --max 8
    python tools/esp_socket_ceiling.py --host 192.168.1.65 --kind ws --max 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from esp_probe import http_get  # noqa: E402


async def open_socket(host: str, port: int, kind: str, timeout: float):
    """Open a held connection. Returns (reader, writer, upgraded) or None."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (asyncio.TimeoutError, OSError):
        return None
    if kind == "http":
        # a plain open TCP connection, no request sent: occupies one PCB
        return reader, writer, False
    try:
        writer.write(
            (
                f"GET /ws HTTP/1.1\r\nHost: {host}:{port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"
            ).encode()
        )
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout)
        upgraded = b"101" in head.split(b"\r\n", 1)[0]
        return reader, writer, upgraded
    except (asyncio.TimeoutError, OSError, asyncio.IncompleteReadError):
        writer.close()
        return None


async def health(host: str, port: int, timeout: float) -> tuple[str, int]:
    ok, lat, status, body, detail = await http_get(host, port, "/style.css", timeout)
    if ok and status == 200 and body:
        return f"HTTP ok {len(body)}B {lat * 1000:.0f}ms", len(body)
    return f"HTTP DEAD ({detail or status})", 0


async def main_async(args: argparse.Namespace) -> int:
    print(
        f"socket ceiling test on {args.host}:{args.port} holding {args.kind} "
        f"connections, max={args.max}\n"
    )

    label, _ = await health(args.host, args.port, args.timeout)
    print(f"baseline: {label}")
    if "DEAD" in label:
        print("\nthe device's HTTP service is already dead. Power-cycle it (or wait")
        print("for its watchdog) and re-run when /style.css answers again.")
        return 2

    held: list = []
    rows: list[dict] = []
    ceiling = None
    try:
        for depth in range(1, args.max + 1):
            opened = await open_socket(args.host, args.port, args.kind, args.timeout)
            if opened is None:
                print(
                    f"depth {depth}: could NOT open a new connection -> ceiling reached"
                )
                ceiling = depth - 1
                break
            held.append(opened)
            upgraded = opened[2]
            label, size = await health(args.host, args.port, args.timeout)
            print(
                f"depth {depth}: held={len(held)} opened="
                f"{'ws101' if upgraded else 'tcp'}  {label}",
                flush=True,
            )
            rows.append(
                {"depth": depth, "upgraded": upgraded, "http": label, "bytes": size}
            )
            if "DEAD" in label:
                print(
                    f"   -> HTTP died at depth {depth}: each held connection "
                    f"costs one of the device's TCP slots"
                )
                ceiling = depth
                break
            await asyncio.sleep(args.settle)
    finally:
        for _reader, writer, _up in held:
            try:
                writer.close()
            except Exception:
                pass
        print(f"\nreleased {len(held)} connection(s)")

    print("waiting for the device to reclaim slots ...")
    recovered_at = None
    for i in range(args.recover):
        await asyncio.sleep(2)
        label, _ = await health(args.host, args.port, args.timeout)
        if i % 3 == 0 or "ok" in label:
            print(f"  +{(i + 1) * 2:3d}s  {label}")
        if "ok" in label:
            recovered_at = (i + 1) * 2
            break

    print("\n=== result ===")
    if ceiling is not None:
        print(f"usable simultaneous connections before HTTP dies: ~{ceiling}")
    else:
        print(f"no ceiling found up to {args.max} simultaneous connections")
    if recovered_at is not None:
        print(f"slots reclaimed {recovered_at}s after release")
    else:
        print(
            "did not recover -> the device needs a restart; leaked PCBs do not time out"
        )

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "kind": args.kind,
                    "ceiling": ceiling,
                    "recovered_after_s": recovered_at,
                    "rows": rows,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"json written to {args.json}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=80)
    p.add_argument("--kind", choices=("tcp", "ws"), default="tcp")
    p.add_argument("--max", type=int, default=8)
    p.add_argument("--settle", type=float, default=1.5)
    p.add_argument("--timeout", type=float, default=6)
    p.add_argument("--recover", type=int, default=20)
    p.add_argument("--json")
    args = p.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
