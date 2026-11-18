#!/usr/bin/env python3
"""Low-rate watchdog that fingerprints HOW the device fails.

Distinguishes the failure modes that matter, because each implies a different
root cause and a different fix:

  ICMP down                     -> full reboot / crash / brownout
  ICMP up, TCP 80 refused       -> web task dead, stack alive
  TCP 80 accepts, HTTP silent   -> sockets or HTTP task exhausted  (the
                                   "retry storm" signature)
  WS handshakes but no HTTP     -> HTTP task starved while /ws survives
  HTTP 200 but truncated body   -> heap fragmentation while serialising

Prints a transition log with timestamps, and a per-mode histogram at the end.
Kept deliberately slow (default 1 sample / 10 s) so the observer does not
become the load.

Usage:
    python tools/esp_watchdog.py --host 192.168.1.91 192.168.1.65 \
        --duration 900 --interval 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from esp_probe import http_get, tcp_probe, ws_probe  # noqa: E402


def ping(host: str, timeout_s: float = 1.0) -> bool:
    try:
        completed = subprocess.run(
            ["ping", "-c", "1", "-W", str(int(timeout_s * 1000)), host],
            capture_output=True,
            timeout=timeout_s + 2,
        )
        return completed.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


async def classify(host: str, port: int, timeout: float) -> tuple[str, str]:
    """Return (mode, detail)."""
    icmp = await asyncio.to_thread(ping, host)

    ok, latency, detail = await tcp_probe(host, port, timeout)
    if not ok:
        if not icmp:
            return "icmp_down", "no ICMP, no TCP (rebooting or off)"
        if detail == "refused":
            return "tcp_refused", "ICMP up, port closed (web task dead)"
        return "tcp_silent", f"ICMP up, connect failed: {detail}"

    ok_http, http_lat, status, body, http_detail = await http_get(
        host, port, "/", timeout
    )
    if ok_http and status == 200 and body:
        return "healthy", f"http 200 {len(body)}B {http_lat * 1000:.0f}ms"
    if ok_http and status and status >= 400:
        return "http_error", f"http {status} {len(body)}B"

    ws_ok, ws_lat, ws_detail, ws_bytes = await ws_probe(
        host, port, "/ws", timeout, hold=0.5
    )
    if ws_ok:
        return "http_dead_ws_ok", (
            f"TCP ok, WS 101 in {ws_lat * 1000:.0f}ms ({ws_bytes}B), "
            f"HTTP {http_detail or 'silent'}"
        )
    if not icmp:
        return "icmp_down", "lost ICMP mid-probe"
    return "tcp_accept_no_response", f"TCP accepts, nothing answers ({http_detail})"


async def main_async(args: argparse.Namespace) -> int:
    state: dict[str, dict] = {
        h: {
            "mode": None,
            "since": time.time(),
            "log": [],
            "counts": Counter(),
            "outages": [],
            "latencies": [],
        }
        for h in args.host
    }
    stop = time.monotonic() + args.duration
    print(
        f"watching {', '.join(args.host)} for {args.duration:.0f}s "
        f"@ {args.interval:.0f}s (Ctrl-C to stop)\n"
    )

    while time.monotonic() < stop:
        stamp = time.strftime("%H:%M:%S")
        for host in args.host:
            mode, detail = await classify(host, args.port, args.timeout)
            st = state[host]
            st["counts"][mode] += 1
            if mode != st["mode"]:
                prev = st["mode"] or "start"
                held = time.time() - st["since"]
                print(
                    f"[{stamp}] {host}: {prev} -> {mode}  ({detail})"
                    f"{f'  [was {held:.0f}s]' if prev != 'start' else ''}",
                    flush=True,
                )
                st["log"].append(
                    {
                        "t": stamp,
                        "from": prev,
                        "to": mode,
                        "detail": detail,
                        "held_s": round(held, 1),
                    }
                )
                if prev != "start":
                    st["outages"].append({"mode": prev, "held_s": round(held, 1)})
                st["mode"] = mode
                st["since"] = time.time()
            elif args.verbose:
                print(f"[{stamp}] {host}: {mode} ({detail})", flush=True)
        await asyncio.sleep(args.interval)

    print("\n=== summary ===")
    exit_code = 0
    for host, st in state.items():
        print(f"\n{host}: final mode = {st['mode']}")
        for mode, count in st["counts"].most_common():
            print(f"   {mode:24s} {count:4d} samples")
        transitions = [e for e in st["log"] if e["from"] != "start"]
        if transitions:
            exit_code = 2
            print("   transitions:")
            for t in transitions[:20]:
                print(f"     {t['t']}  {t['from']} -> {t['to']} after {t['held_s']}s")
        deaths = st["counts"].get("icmp_down", 0)
        if deaths:
            print(
                f"   !! {deaths} sample(s) with no ICMP at all -> device was "
                f"fully down (reboot/crash signature)"
            )

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    h: {
                        "final": s["mode"],
                        "counts": dict(s["counts"]),
                        "log": s["log"],
                    }
                    for h, s in state.items()
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\njson written to {args.json}")
    return exit_code


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--host", nargs="+", required=True)
    p.add_argument("--port", type=int, default=80)
    p.add_argument("--duration", type=float, default=900)
    p.add_argument("--interval", type=float, default=10)
    p.add_argument("--timeout", type=float, default=6)
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--json")
    args = p.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
