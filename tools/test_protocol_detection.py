#!/usr/bin/env python3
"""Verify the client adapts to both firmware behaviours.

Device A ("push"): sends readings every 200 ms without being asked.
Device B ("pull"): sends nothing until it receives 0x67, then one frame.

The client must end up sending exactly two bytes to either device (settings +
initial readings request) and never poll the push device, while still keeping
the pull device updated at a slow, fixed cadence.

    python tools/test_protocol_detection.py
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "custom_components" / "velectric_load_manager"


def accept_key(head: bytes) -> bytes:
    key = ""
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"sec-websocket-key:"):
            key = line.split(b":", 1)[1].strip().decode()
            break
    return base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    )


def load():
    pkg = "velectric_load_manager"
    spec = importlib.util.spec_from_file_location(
        pkg, PKG / "__init__.py", submodule_search_locations=[str(PKG)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[pkg] = module
    spec.loader.exec_module(module)
    return module


def readings_frame(i: int = 0) -> bytes:
    return (
        bytes([0x82, 13])
        + struct.pack("<HHHHH", 100 + i, 400 + i, 0, 0, 0)
        + b"\x01\x00\x00"
    )


class Device:
    """mode='push' streams; mode='pull' answers each 0x67 with one frame."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.requests: list[int] = []
        self.connects = 0
        self.frames_out = 0
        self.writers: list = []
        self.server = None

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _handle(self, reader, writer) -> None:
        self.writers.append(writer)
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            return
        self.connects += 1
        writer.write(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Accept: "
            + accept_key(head)
            + b"\r\n\r\n"
        )
        await writer.drain()

        # background streamer for push mode
        async def stream() -> None:
            i = 0
            while True:
                writer.write(readings_frame(i))
                await writer.drain()
                self.frames_out += 1
                i += 1
                await asyncio.sleep(0.2)

        task = None
        if self.mode == "push":
            task = asyncio.create_task(stream())
        try:
            while True:
                # read client frames (masked, single byte payload)
                hdr = await reader.read(2)
                if len(hdr) < 2:
                    return
                masked = hdr[1] & 0x80
                length = hdr[1] & 0x7F
                if length == 126:
                    ext = await reader.read(2)
                    length = int.from_bytes(ext, "big")
                if masked:
                    mask = await reader.read(4)
                payload = await reader.read(length)
                if masked and payload:
                    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                if payload:
                    cmd = payload[0]
                    self.requests.append(cmd)
                    if cmd == 103 and self.mode == "pull":
                        writer.write(readings_frame(0))
                        await writer.drain()
                        self.frames_out += 1
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if task:
                task.cancel()

    async def stop(self) -> None:
        for w in self.writers:
            try:
                w.transport.abort()
            except Exception:
                pass
        if self.server:
            self.server.close()
            await self.server.wait_closed()


async def run(mode: str, seconds: float) -> tuple[Device, dict, list[int]]:
    mod = load()
    device = Device(mode)
    port = await device.start()

    client = mod.VElectricWebSocketClient("127.0.0.1", port)
    readings = {"n": 0}
    client.on_current_reading = lambda *_: readings.__setitem__("n", readings["n"] + 1)

    sent: list[int] = []
    real_send = mod.VElectricWebSocketClient._send

    async def counting_send(self, command: int) -> None:
        sent.append(command)
        await real_send(self, command)

    mod.VElectricWebSocketClient._send = counting_send

    await client.start()
    await asyncio.sleep(seconds)
    await client.async_stop()
    await device.stop()
    return device, readings, sent


async def main() -> int:
    failures: list[str] = []

    print("== push firmware ==")
    dev, readings, sent = await run("push", 16)
    print(f"  client sent {sent}  readings={readings['n']}  "
          f"device pushed={dev.frames_out}")
    if sent != [105, 103]:
        failures.append(f"push device: expected [105, 103], got {sent}")
    if readings["n"] == 0:
        failures.append("push device: no readings decoded")

    print("== request/response firmware ==")
    dev2, readings2, sent2 = await run("pull", 22)
    print(f"  client sent {sent2}  readings={readings2['n']}  "
          f"device answered={dev2.frames_out}")
    if sent2[:2] != [105, 103]:
        failures.append(f"pull device: expected handshake [105,103], got {sent2[:2]}")
    polls = [c for c in sent2[2:] if c == 103]
    if not polls:
        failures.append("pull device: never polled; it would show no data")
    # 22 s at an 8 s interval -> handshake + at most 3 polls
    if len(polls) > 3:
        failures.append(f"pull device: polled {len(polls)}x, too aggressive")
    if readings2["n"] == 0:
        failures.append("pull device: no readings decoded")

    print("\n=== result ===")
    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PASS: push device never polled, pull device polled slowly")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
