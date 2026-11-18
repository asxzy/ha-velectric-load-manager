#!/usr/bin/env python3
"""Measure the recovery cost of a single HTTP request on an ESP web server.

This is the decisive experiment for "does polling/retrying this HTTP service
kill the device?". It answers one question with a number:

    after ONE successful HTTP GET, how many seconds is the HTTP service
    unusable?

Procedure per round:
  1. idle for --idle seconds
  2. poll cheaply until HTTP answers (so we start from a known-good state)
  3. fire exactly one GET
  4. keep polling with cheap TCP connects + short GETs, recording when the
     service answers again

A wedged server still completes the TCP handshake, so TCP success is NOT proof
of health; we require a full HTTP 200 with a complete body.

Usage:
    python tools/esp_recovery_cost.py --host 192.168.1.91 --rounds 3
    python tools/esp_recovery_cost.py --host 192.168.1.65 --path / --rounds 5

Read the output as: "one GET costs N seconds of blackout". If N is tens of
seconds, a 5 s poll interval with retries will never let the device recover,
which is exactly the reboot loop being suspected.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from esp_probe import http_get, tcp_probe  # noqa: E402


async def http_alive(host: str, port: int, path: str, timeout: float) -> bool:
    ok, _lat, status, body, _detail = await http_get(host, port, path, timeout)
    # require a complete 200 with a body: headers alone are not health
    return bool(ok and status == 200 and body)


async def wait_until_alive(
    host: str, port: int, path: str, timeout: float, budget: float, poll: float
) -> float | None:
    """Poll until HTTP answers. Returns seconds waited, or None on budget out."""
    start = time.monotonic()
    while time.monotonic() - start < budget:
        if await http_alive(host, port, path, timeout):
            return time.monotonic() - start
        await asyncio.sleep(poll)
    return None


async def probe_round(args: argparse.Namespace, index: int) -> dict:
    print(f"\n--- round {index} ---", flush=True)

    print(f"  idling {args.idle:.0f}s ...", flush=True)
    await asyncio.sleep(args.idle)

    alive = await http_alive(args.host, args.port, args.path, args.timeout)
    if not alive:
        print(
            "  device is NOT healthy before the request, waiting for it ...", flush=True
        )
        waited = await wait_until_alive(
            args.host, args.port, args.path, args.timeout, args.recover_budget, 3.0
        )
        if waited is None:
            print(
                f"  still unhealthy after {args.recover_budget:.0f}s; "
                f"skipping round (this itself is the finding)"
            )
            return {"healthy_before": False, "recovery_s": None}
        print(f"  healthy again after {waited:.0f}s", flush=True)

    # one request, timed
    t0 = time.monotonic()
    ok, latency, status, body, detail = await http_get(
        args.host, args.port, args.path, args.timeout
    )
    print(
        f"  single GET -> ok={ok} status={status} bytes={len(body)} "
        f"{latency * 1000:.0f}ms {detail}",
        flush=True,
    )

    # immediately try cheap TCP, then poll HTTP until it answers
    tcp_ok, tcp_lat, tcp_detail = await tcp_probe(args.host, args.port, 2.0)
    print(
        f"  tcp right after -> ok={tcp_ok} {tcp_lat * 1000:.0f}ms {tcp_detail}",
        flush=True,
    )

    deadline = time.monotonic() + args.recover_budget
    recovery = None
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        if await http_alive(args.host, args.port, args.path, args.timeout):
            recovery = time.monotonic() - t0
            break
        await asyncio.sleep(args.poll)
    if recovery is None:
        print(f"  HTTP still dead after {args.recover_budget:.0f}s", flush=True)
    else:
        print(
            f"  HTTP healthy again after {recovery:.1f}s ({attempts} polls)", flush=True
        )

    return {
        "healthy_before": True,
        "request_ok": ok,
        "request_status": status,
        "request_bytes": len(body),
        "request_ms": round(latency * 1000, 1),
        "tcp_ok_after": tcp_ok,
        "recovery_s": None if recovery is None else round(recovery, 1),
    }


async def main_async(args: argparse.Namespace) -> int:
    print(f"recovery-cost test on {args.host}:{args.port}{args.path}")
    print(f"idle={args.idle}s poll={args.poll}s budget={args.recover_budget}s")

    rounds = [await probe_round(args, i + 1) for i in range(args.rounds)]

    values = [r["recovery_s"] for r in rounds if r.get("recovery_s") is not None]
    dead = sum(1 for r in rounds if r.get("recovery_s") is None)
    print("\n=== result ===")
    if values:
        print(
            f"recovery after one GET: min={min(values):.1f}s "
            f"median={statistics.median(values):.1f}s max={max(values):.1f}s"
        )
    if dead:
        print(
            f"{dead}/{len(rounds)} rounds never recovered within "
            f"{args.recover_budget:.0f}s"
        )
    if values and statistics.median(values) > args.idle:
        print(
            "VERDICT: one request costs more downtime than the idle interval "
            "you allowed -> polling faster than this can never succeed"
        )
    elif dead:
        print("VERDICT: the service wedges permanently -> only a restart clears it")

    if args.json:
        Path(args.json).write_text(json.dumps(rounds, indent=2), encoding="utf-8")
        print(f"json written to {args.json}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=80)
    p.add_argument("--path", default="/")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument(
        "--idle", type=float, default=30.0, help="seconds of quiet before each round"
    )
    p.add_argument(
        "--poll",
        type=float,
        default=2.0,
        help="seconds between health checks while waiting",
    )
    p.add_argument("--timeout", type=float, default=8.0)
    p.add_argument("--recover-budget", type=float, default=180.0)
    p.add_argument("--json")
    args = p.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
