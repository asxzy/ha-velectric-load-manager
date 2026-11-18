#!/usr/bin/env python3
"""Bounded validation: does retrying the ESP's HTTP service kill it?

The hypothesis to validate:

    The device's HTTP server sends a complete response with Content-Length and
    `Connection: close`, but never actually closes the TCP connection. A client
    that reads until EOF therefore blocks until its own timeout, holding one of
    the device's few TCP slots the whole time. Retry fast enough and the slots
    run out: the device still completes TCP handshakes and still answers /ws,
    but HTTP dies and eventually the watchdog reboots the chip.

This harness proves or disproves that in two modes, and crucially it keeps its
own footprint clean:

  strict   read exactly Content-Length bytes, then close immediately
           -> must NOT accumulate anything on the device

  eof      read until the peer closes, with a short timeout
           -> expected to wedge the device (this is the buggy-client pattern)

Then it measures whether the device degrades. Every step is bounded, health is
checked between steps, and the run stops at the first failure so it cannot
grind the device into the ground.

Usage:
    # does the bad pattern wedge it? (default: 6 requests, stop on first failure)
    python tools/validate_retry_impact.py --host 192.168.1.65 --mode eof

    # does the good pattern survive?
    python tools/validate_retry_impact.py --host 192.168.1.65 --mode strict --count 30

    # full comparison, run strict first
    python tools/validate_retry_impact.py --host 192.168.1.65 --compare
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


async def fetch_strict(host: str, port: int, path: str, timeout: float) -> dict:
    """Correct HTTP client: stop at Content-Length, close immediately."""
    t0 = time.monotonic()
    result = {
        "mode": "strict",
        "ok": False,
        "status": 0,
        "bytes": 0,
        "ms": 0.0,
        "detail": "",
        "eof_seen": None,
    }
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (asyncio.TimeoutError, OSError) as err:
        result["detail"] = f"connect:{type(err).__name__}"
        result["ms"] = round((time.monotonic() - t0) * 1000, 1)
        return result

    try:
        writer.write(
            (
                f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
                "User-Agent: esp-validate/1.0\r\n"
                "Accept: */*\r\nConnection: close\r\n\r\n"
            ).encode()
        )
        await asyncio.wait_for(writer.drain(), timeout=timeout)

        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout)
        header_text = head.decode(errors="replace")
        status = 0
        length = None
        for line in header_text.split("\r\n"):
            low = line.lower()
            if low.startswith("http/"):
                parts = line.split()
                status = int(parts[1]) if len(parts) > 1 else -1
            elif low.startswith("content-length:"):
                length = int(line.split(":", 1)[1].strip())

        body = b""
        if length is not None:
            body = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
        else:
            body = await asyncio.wait_for(reader.read(-1), timeout=timeout)

        result.update(
            ok=status == 200 and bool(body),
            status=status,
            bytes=len(body),
            detail=header_text.split("\r\n")[0],
        )
    except asyncio.TimeoutError:
        result["detail"] = "timeout"
    except asyncio.IncompleteReadError as err:
        result["detail"] = f"short-body:{len(err.partial)}B"
        result["bytes"] = len(err.partial)
    except (ConnectionError, OSError) as err:
        result["detail"] = f"{type(err).__name__}"
    finally:
        # the whole point: close at once, never wait for EOF
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=2)
        except Exception:
            pass
        result["ms"] = round((time.monotonic() - t0) * 1000, 1)
    return result


async def fetch_eof(host: str, port: int, path: str, timeout: float) -> dict:
    """Buggy client: waits for the peer to close, which it never does."""
    t0 = time.monotonic()
    result = {
        "mode": "eof",
        "ok": False,
        "status": 0,
        "bytes": 0,
        "ms": 0.0,
        "detail": "",
        "eof_seen": None,
    }
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (asyncio.TimeoutError, OSError) as err:
        result["detail"] = f"connect:{type(err).__name__}"
        result["ms"] = round((time.monotonic() - t0) * 1000, 1)
        return result

    data = b""
    eof = False
    try:
        writer.write(
            (
                f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
                "User-Agent: esp-validate/1.0\r\n"
                "Accept: */*\r\nConnection: close\r\n\r\n"
            ).encode()
        )
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        while True:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=timeout)
            if not chunk:
                eof = True
                break
            data += chunk
    except asyncio.TimeoutError:
        result["detail"] = "no-eof-timeout"
    except (ConnectionError, OSError) as err:
        result["detail"] = f"{type(err).__name__}"
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=2)
        except Exception:
            pass

    head, _, body = data.partition(b"\r\n\r\n")
    status = 0
    for line in head.decode(errors="replace").split("\r\n"):
        if line.lower().startswith("http/"):
            parts = line.split()
            status = int(parts[1]) if len(parts) > 1 else -1
            break
    result.update(
        ok=status == 200 and bool(body),
        status=status,
        bytes=len(body),
        eof_seen=eof,
        detail=result["detail"] or ("eof" if eof else "no-eof"),
        ms=round((time.monotonic() - t0) * 1000, 1),
    )
    return result


async def main_async(args: argparse.Namespace) -> int:
    modes = ["strict", "eof"] if args.compare else [args.mode]
    summary: dict[str, dict] = {}

    for mode in modes:
        print(
            f"\n=== mode={mode} on {args.host}:{args.port}{args.path} "
            f"(count={args.count}, timeout={args.timeout}s) ==="
        )

        probe = fetch_strict if mode == "strict" else fetch_eof
        results: list[dict] = []
        stop_reason = None

        # confirm the service is up before we start
        baseline = await probe(args.host, args.port, args.path, args.timeout)
        print(
            f"baseline: ok={baseline['ok']} status={baseline['status']} "
            f"{baseline['bytes']}B {baseline['ms']:.0f}ms "
            f"eof={baseline['eof_seen']} ({baseline['detail']})"
        )
        if not baseline["ok"]:
            print("service is not healthy right now; wait for it to recover")
            summary[mode] = {"skipped": True}
            continue

        for i in range(1, args.count + 1):
            r = await probe(args.host, args.port, args.path, args.timeout)
            results.append(r)
            flag = "ok " if r["ok"] else "FAIL"
            if i % args.report_every == 0 or not r["ok"]:
                print(
                    f"  [{i:3d}] {flag} status={r['status']} "
                    f"{r['bytes']:5d}B {r['ms']:7.0f}ms "
                    f"eof={r['eof_seen']} ({r['detail']})",
                    flush=True,
                )
            if not r["ok"]:
                stop_reason = f"request {i} failed: {r['detail']}"
                print(f"  stopping: {stop_reason}")
                break
            await asyncio.sleep(args.gap)

        ok_count = sum(1 for r in results if r["ok"])
        durations = [r["ms"] for r in results if r["ok"]]
        print(
            f"  -> {ok_count}/{len(results)} ok"
            + (f", median {statistics.median(durations):.0f}ms" if durations else "")
        )
        if stop_reason:
            print(f"  -> {stop_reason}")

        summary[mode] = {
            "attempts": len(results),
            "ok": ok_count,
            "stop_reason": stop_reason,
            "median_ms": round(statistics.median(durations), 1) if durations else None,
            "eof_ever_seen": any(r["eof_seen"] for r in results),
            "failures": [r for r in results if not r["ok"]][:5],
        }

        if stop_reason and mode == "eof" and args.compare:
            print("\n  letting the device recover before the next mode ...")
            for _ in range(args.recover):
                health = await fetch_strict(
                    args.host, args.port, args.path, args.timeout
                )
                if health["ok"]:
                    print("  device recovered")
                    break
                await asyncio.sleep(3)

    print("\n=== verdict ===")
    for mode, data in summary.items():
        if data.get("skipped"):
            print(f"{mode}: skipped (device unhealthy at start)")
            continue
        print(
            f"{mode}: {data['ok']}/{data['attempts']} requests succeeded, "
            f"eof_seen={data['eof_ever_seen']}"
        )
        if data["stop_reason"]:
            print(f"   died: {data['stop_reason']}")

    strict = summary.get("strict", {})
    eof = summary.get("eof", {})
    if args.compare and strict and eof:
        if strict.get("stop_reason") is None and eof.get("stop_reason"):
            print("\nCONCLUSION: the strict client survived every request while the")
            print("            EOF-waiting client killed the service. The device")
            print("            never closes connections, so any client that waits")
            print("            for EOF leaks a slot per request. Retries then exhaust")
            print("            the TCP pool -> dead HTTP, eventual watchdog reboot.")
        elif strict.get("stop_reason"):
            print("\nCONCLUSION: even the strict client wedges it. The failure is")
            print("            not (only) the missing FIN; look at response rate,")
            print("            concurrent connections, and heap pressure instead.")
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"json written to {args.json}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=80)
    p.add_argument(
        "--path",
        default="/style.css",
        help="small static file; avoid / which is the biggest page",
    )
    p.add_argument("--mode", choices=("strict", "eof"), default="strict")
    p.add_argument(
        "--compare",
        action="store_true",
        help="run strict then eof and print a differential verdict",
    )
    p.add_argument("--count", type=int, default=30)
    p.add_argument("--gap", type=float, default=0.5, help="seconds between requests")
    p.add_argument("--timeout", type=float, default=6.0)
    p.add_argument("--report-every", type=int, default=5)
    p.add_argument(
        "--recover",
        type=int,
        default=40,
        help="health checks to wait for recovery in --compare mode",
    )
    p.add_argument("--json")
    args = p.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
