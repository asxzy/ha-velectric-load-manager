#!/usr/bin/env python3
"""Controlled probes for the VElectric (ESP) device web service.

Three sub-commands, all deliberately boring and rate limited so you can raise
the load in small steps instead of nuking the chip:

  health   Sample the HTTP/WS service at a fixed rate and report outages.
           Use this to establish a baseline and to catch reboots/crashes.

  storm    Open connections at a chosen rate (HTTP GET or WS upgrade + the
           0x67 request byte) to reproduce the "retry storm" hypothesis.

  mock     Run a fake, constrained ESP (few sockets, slow writes, small heap)
           so the Home Assistant integration can be exercised against it
           without touching real hardware.

Examples
--------
  # 2 minute baseline, one sample per second, both devices
  python tools/esp_probe.py health --host 192.168.1.91 192.168.1.65 \
      --duration 120 --interval 1.0

  # WS retry load: 1 connection attempt per second for 60 s
  python tools/esp_probe.py storm --host 192.168.1.91 --mode ws \
      --rate 1 --duration 60 --abort-after-failures 5

  # Mock device for HA integration testing
  python tools/esp_probe.py mock --port 8080 --max-sockets 4 --stall 0.2

Everything is standard library. Nothing writes to the device.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import struct
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

WS_REQUEST_BYTE = 103  # 'g' - same byte the integration sends

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def now() -> float:
    return time.monotonic()


async def tcp_probe(host: str, port: int, timeout: float) -> tuple[bool, float, str]:
    """Measure TCP accept latency. Returns (ok, seconds, error)."""
    start = now()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        elapsed = now() - start
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True, elapsed, ""
    except asyncio.TimeoutError:
        return False, now() - start, "timeout"
    except ConnectionRefusedError:
        return False, now() - start, "refused"
    except OSError as err:
        return False, now() - start, f"oserror:{err.errno}"


async def http_get(
    host: str, port: int, path: str, timeout: float
) -> tuple[bool, float, int, bytes, str]:
    """GET a path with `Connection: close`; report status, bytes and elapsed."""
    start = now()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except asyncio.TimeoutError:
        return False, now() - start, 0, b"", "connect-timeout"
    except ConnectionRefusedError:
        return False, now() - start, 0, b"", "refused"
    except OSError as err:
        return False, now() - start, 0, b"", f"oserror:{err.errno}"

    try:
        writer.write(
            (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                "User-Agent: esp-probe/1.0\r\n"
                "Accept: */*\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
        )
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        body = await asyncio.wait_for(reader.read(-1), timeout=timeout)
        elapsed = now() - start
        status = 0
        if body.startswith(b"HTTP/"):
            try:
                status = int(body.split(b" ", 2)[1])
            except (IndexError, ValueError):
                status = -1
        return True, elapsed, status, body, ""
    except asyncio.TimeoutError:
        return False, now() - start, 0, b"", "read-timeout"
    except (ConnectionResetError, ConnectionAbortedError) as err:
        return False, now() - start, 0, b"", f"reset:{err.__class__.__name__}"
    except OSError as err:
        return False, now() - start, 0, b"", f"oserror:{err.errno}"
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


def ws_frame(payload: bytes) -> bytes:
    """Build a masked, binary client frame (single frame, <=125 bytes)."""
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes([0x82, 0x80 | len(payload)]) + mask + masked


async def ws_probe(
    host: str, port: int, path: str = "/ws", timeout: float = 5.0, hold: float = 0.0
) -> tuple[bool, float, str, int]:
    """Do a real WebSocket upgrade, optionally send 0x67, optionally hold.

    Returns (ok, handshake_seconds, detail, bytes_received).
    """
    start = now()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except asyncio.TimeoutError:
        return False, now() - start, "connect-timeout", 0
    except ConnectionRefusedError:
        return False, now() - start, "refused", 0
    except OSError as err:
        return False, now() - start, f"oserror:{err.errno}", 0

    received = 0
    try:
        writer.write(
            (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                "User-Agent: esp-probe/1.0\r\n\r\n"
            ).encode()
        )
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout)
        elapsed = now() - start
        if b"101" not in head.split(b"\r\n", 1)[0]:
            return False, elapsed, head.split(b"\r\n", 1)[0].decode(errors="replace"), 0

        writer.write(ws_frame(bytes([WS_REQUEST_BYTE])))
        await asyncio.wait_for(writer.drain(), timeout=timeout)

        deadline = now() + max(hold, 1.0)
        while now() < deadline:
            try:
                chunk = await asyncio.wait_for(
                    reader.read(4096), timeout=max(0.05, deadline - now())
                )
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            received += len(chunk)
        return True, elapsed, f"bytes={received}", received
    except asyncio.TimeoutError:
        return False, now() - start, "handshake-timeout", received
    except (ConnectionResetError, ConnectionAbortedError) as err:
        return False, now() - start, f"reset:{err.__class__.__name__}", received
    except OSError as err:
        return False, now() - start, f"oserror:{err.errno}", received
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


@dataclass
class Sample:
    t: float
    ok: bool
    latency: float
    detail: str
    status: int = 0
    size: int = 0


@dataclass
class Outage:
    start: float
    end: float | None = None
    samples: int = 1

    @property
    def duration(self) -> float:
        return (self.end or now()) - self.start


@dataclass
class HealthReport:
    samples: list[Sample] = field(default_factory=list)
    outages: list[Outage] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for s in self.samples if s.ok)

    @property
    def failures(self) -> list[Sample]:
        return [s for s in self.samples if not s.ok]


async def cmd_health(args: argparse.Namespace) -> int:
    print(
        f"baseline: {args.duration:.0f}s @ {args.interval:.2f}s, "
        f"GET {args.path or '(tcp only)'} on {len(args.host)} host(s)"
    )
    reports = {h: HealthReport() for h in args.host}
    stop = now() + args.duration
    tick = 0
    while now() < stop:
        tick += 1
        for host in args.host:
            rep = reports[host]
            if args.hold_ws:
                ok, latency, detail, _ = await ws_probe(
                    host, args.port, args.path or "/ws", args.timeout
                )
                sample = Sample(now(), ok, latency, detail)
            elif args.path:
                ok, latency, status, body, detail = await http_get(
                    host, args.port, args.path, args.timeout
                )
                sample = Sample(
                    now(), ok and status == 200, latency, detail, status, len(body)
                )
            else:
                ok, latency, detail = await tcp_probe(host, args.port, args.timeout)
                sample = Sample(now(), ok, latency, detail)

            rep.samples.append(sample)
            if not sample.ok:
                if rep.outages and rep.outages[-1].end is None:
                    rep.outages[-1].samples += 1
                else:
                    rep.outages.append(Outage(sample.t))
            elif rep.outages and rep.outages[-1].end is None:
                rep.outages[-1].end = sample.t
        if args.verbose or tick % 10 == 0:
            line = f"[{tick:4d}] " + "  ".join(
                f"{h}: "
                + (
                    f"{reports[h].samples[-1].latency * 1000:7.1f}ms"
                    f"/{reports[h].samples[-1].size or 0}B"
                    if reports[h].samples[-1].ok
                    else f"FAIL({reports[h].samples[-1].detail})"
                )
                for h in args.host
            )
            print(line, flush=True)
        await asyncio.sleep(max(0.0, args.interval - (now() % args.interval)))

    print("\n=== summary ===")
    exit_code = 0
    for host, rep in reports.items():
        lat = sorted(s.latency for s in rep.samples if s.ok)
        p50 = lat[len(lat) // 2] * 1000 if lat else float("nan")
        p95 = lat[int(len(lat) * 0.95) - 1] * 1000 if lat else float("nan")
        print(
            f"{host}: {rep.ok_count}/{len(rep.samples)} ok, "
            f"p50={p50:.0f}ms p95={p95:.0f}ms, outages={len(rep.outages)}"
        )
        for o in rep.outages:
            state = "ongoing" if o.end is None else "recovered"
            print(f"   outage {o.duration:6.1f}s ({o.samples} samples, {state})")
        if rep.outages:
            exit_code = 2
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    h: {
                        "ok": r.ok_count,
                        "total": len(r.samples),
                        "outages": [
                            {
                                "duration": o.duration,
                                "samples": o.samples,
                                "recovered": o.end is not None,
                            }
                            for o in r.outages
                        ],
                        "failures": Counter(s.detail for s in r.failures),
                    }
                    for h, r in reports.items()
                },
                handle,
                indent=2,
                default=str,
            )
        print(f"json written to {args.json}")
    return exit_code


# ---------------------------------------------------------------------------
# storm
# ---------------------------------------------------------------------------


async def cmd_storm(args: argparse.Namespace) -> int:
    """Open connections at a fixed rate, sequentially, and abort if the
    service stops answering. Sequential is intentional: it models one HA
    instance retrying, not a DDoS."""
    print(
        f"storm: {args.mode} on {args.host}:{args.port} at {args.rate}/s "
        f"for {args.duration:.0f}s (abort after "
        f"{args.abort_after_failures} consecutive failures)"
    )
    interval = 1.0 / args.rate
    stop = now() + args.duration
    results: list[tuple[float, bool, float, str]] = []
    consecutive_fail = 0
    aborted = False

    while now() < stop:
        if args.mode == "ws":
            ok, latency, detail, _ = await ws_probe(
                args.host, args.port, args.path or "/ws", args.timeout
            )
        else:
            ok, latency, status, body, detail = await http_get(
                args.host, args.port, args.path or "/", args.timeout
            )
            ok = ok and status == 200
            detail = detail or f"status={status} bytes={len(body)}"

        results.append((now(), ok, latency, detail))
        if ok:
            consecutive_fail = 0
        else:
            consecutive_fail += 1
            print(
                f"  FAIL ({consecutive_fail}/{args.abort_after_failures}): {detail}",
                flush=True,
            )
            if consecutive_fail >= args.abort_after_failures:
                aborted = True
                print("  aborting: device stopped answering")
                break

        if len(results) % max(1, int(args.rate)) == 0:
            ok_count = sum(1 for r in results[-int(args.rate) :] if r[1])
            lat = [r[2] for r in results[-int(args.rate) :] if r[1]]
            avg = sum(lat) / len(lat) * 1000 if lat else float("nan")
            print(
                f"  t+{args.duration - max(0, stop - now()):5.1f}s "
                f"ok={ok_count}/{len(results[-int(args.rate) :])} "
                f"avg={avg:6.0f}ms",
                flush=True,
            )
        await asyncio.sleep(max(0.0, interval - ((now()) % interval)))

    ok_count = sum(1 for r in results if r[1])
    print("\n=== storm summary ===")
    print(
        f"attempts={len(results)} ok={ok_count} failed={len(results) - ok_count}"
        f" aborted={aborted}"
    )

    if args.recovery:
        print("waiting for recovery (TCP only, 1/s)...")
        for _ in range(args.recovery):
            ok, latency, detail = await tcp_probe(args.host, args.port, args.timeout)
            print(f"  {'up  ' if ok else 'down'} {latency * 1000:7.1f}ms {detail}")
            if ok:
                break
            await asyncio.sleep(1.0)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "mode": args.mode,
                    "rate": args.rate,
                    "attempts": len(results),
                    "ok": ok_count,
                    "aborted": aborted,
                    "failures": Counter(r[3] for r in results if not r[1]),
                },
                handle,
                indent=2,
            )
        print(f"json written to {args.json}")
    return 0 if ok_count == len(results) else 2


# ---------------------------------------------------------------------------
# mock: a deliberately horrible ESP
# ---------------------------------------------------------------------------


MOCK_PAGE = (
    b"""HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n"""
)


async def cmd_mock(args: argparse.Namespace) -> int:
    """Fake ESP: accepts at most `max_sockets` simultaneous connections,
    trickles bytes, and closes early once the budget is spent. Good enough to
    watch what the integration does under stress."""
    live = 0
    total = 0
    dropped = 0
    start = now()
    semaphore = asyncio.Semaphore(args.max_sockets)

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        nonlocal live, total, dropped
        total += 1
        if semaphore.locked():
            # emulate "no free sockets": accept then immediately drop
            dropped += 1
            print(f"  ! socket budget exhausted (dropped={dropped})", flush=True)
            writer.close()
            return
        async with semaphore:
            live += 1
            peer = writer.get_extra_info("peername")
            print(f"  +conn {peer} live={live} total={total}", flush=True)
            try:
                await _serve(reader, writer)
            finally:
                live -= 1

    async def _serve(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), args.timeout
            )
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return

        is_ws = b"Upgrade: websocket" in request or b"upgrade: websocket" in request
        try:
            if is_ws:
                writer.write(
                    b"HTTP/1.1 101 Switching Protocols\r\n"
                    b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n\r\n"
                )
                await writer.drain()
                if args.stall:
                    await asyncio.sleep(args.stall)
                # one 13-byte readings frame, then keep the socket open
                frame = (
                    bytes([0x82, 13])
                    + struct.pack("<HHHHH", 1200, 900, 0, 0, 0)
                    + b"\x01\x00\x00"
                )
                writer.write(frame)
                await writer.drain()
                await asyncio.sleep(args.hold)
            else:
                if args.truncate:
                    # serve only part of the page, like a heap-starved ESP
                    await asyncio.sleep(args.stall)
                    writer.write(MOCK_PAGE + b"<html><body>" + b"x" * 64)
                else:
                    writer.write(MOCK_PAGE + b"<html><body>ok</body></html>")
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(handle, args.bind, args.port)
    print(
        f"mock ESP on ws://{args.bind}:{args.port}/ws "
        f"(max_sockets={args.max_sockets} stall={args.stall}s "
        f"truncate={args.truncate}) - Ctrl-C to stop"
    )
    async with server:
        try:
            await server.serve_forever()
        except asyncio.CancelledError:
            pass
    print(f"served {total} connections in {now() - start:.0f}s")
    return 0


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Controlled health/stress probes for the VElectric ESP device",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--host", nargs="+", required=True, help="device IP(s)")
        p.add_argument("--port", type=int, default=80)
        p.add_argument("--timeout", type=float, default=5.0)
        p.add_argument("--json", help="write a JSON report here")

    health = sub.add_parser("health", help="sample service health, report outages")
    common(health)
    health.add_argument("--duration", type=float, default=120.0)
    health.add_argument("--interval", type=float, default=1.0)
    health.add_argument("--path", default="/", help="HTTP path ('' for TCP only)")
    health.add_argument("--hold-ws", action="store_true", help="probe /ws instead")
    health.add_argument("--verbose", action="store_true")
    health.set_defaults(func=cmd_health)

    storm = sub.add_parser("storm", help="reproduce retry load at a chosen rate")
    common(storm)
    storm.add_argument("--mode", choices=("ws", "http"), default="ws")
    storm.add_argument("--rate", type=float, default=1.0, help="attempts per second")
    storm.add_argument("--duration", type=float, default=60.0)
    storm.add_argument("--path", default=None)
    storm.add_argument("--abort-after-failures", type=int, default=5)
    storm.add_argument(
        "--recovery",
        type=int,
        default=30,
        help="seconds to poll for recovery after abort",
    )
    storm.set_defaults(func=cmd_storm)

    mock = sub.add_parser("mock", help="run a constrained fake ESP")
    mock.add_argument("--bind", default="127.0.0.1")
    mock.add_argument("--port", type=int, default=8080)
    mock.add_argument("--max-sockets", type=int, default=4)
    mock.add_argument("--stall", type=float, default=0.3)
    mock.add_argument("--hold", type=float, default=5.0)
    mock.add_argument("--truncate", action="store_true", default=True)
    mock.add_argument("--timeout", type=float, default=10.0)
    mock.set_defaults(func=cmd_mock)

    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "mock":
        try:
            return asyncio.run(args.func(args))
        except KeyboardInterrupt:
            return 0
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
