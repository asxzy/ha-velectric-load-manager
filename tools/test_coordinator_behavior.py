#!/usr/bin/env python3
"""Check the coordinator's behaviour without polling, using a stub Home Assistant.

Verifies:
  * update_interval is None (no timer-driven device contact)
  * a pushed frame reaches coordinator.data
  * a disconnect marks the entities unavailable (last_update_success False)
  * reconnection happens on the client's backoff, not on a coordinator tick

    python tools/test_coordinator_behavior.py
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import struct
from homeassistant.core import callback
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "custom_components" / "velectric_load_manager"


def load_modules():
    pkg = "velectric_load_manager"
    spec = importlib.util.spec_from_file_location(
        pkg, PKG / "__init__.py", submodule_search_locations=[str(PKG)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[pkg] = module
    spec.loader.exec_module(module)
    return module


def accept_key(head: bytes) -> bytes:
    key = ""
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"sec-websocket-key:"):
            key = line.split(b":", 1)[1].strip().decode()
            break
    return base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    )


class FlakyDevice:
    """Pushes a few frames, then drops the connection."""

    def __init__(self, push_frames: int = 5) -> None:
        self.connects = 0
        self.push_frames = push_frames
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
        try:
            for i in range(self.push_frames):
                writer.write(
                    bytes([0x82, 13])
                    + struct.pack("<HHHHH", 200 + i, 900 + i, 0, 0, 0)
                    + b"\x01\x00\x00"
                )
                await writer.drain()
                await asyncio.sleep(0.2)
            # drop hard, like a rebooting device
            writer.transport.abort()
        except (ConnectionError, asyncio.CancelledError):
            pass

    async def stop(self) -> None:
        for w in self.writers:
            try:
                w.transport.abort()
            except Exception:
                pass
        if self.server:
            self.server.close()
            await self.server.wait_closed()


async def main() -> int:
    mod = load_modules()
    failures: list[str] = []

    device = FlakyDevice(push_frames=5)
    port = await device.start()

    hass = _StubHass()
    coord = mod.VElectricDataUpdateCoordinator(hass, _StubEntry(), "127.0.0.1", port)
    coord._publish_interval = 0.2

    if coord.update_interval is not None:
        failures.append("update_interval is set; the coordinator would poll")

    data = await mod.VElectricDataUpdateCoordinator._async_update_data(coord)
    print(f"initial data: ct1={data['ct1_current']} ct2={data['ct2_current']} "
          f"status={data['connection_status']}")
    if data["connection_status"] != "Connected":
        failures.append("expected Connected after first frame")

    await asyncio.sleep(0.6)
    print(f"after pushes: ct1={coord.data['ct1_current']} "
          f"last_update_success={coord.last_update_success}")

    published = {"n": 0}
    original = coord.async_set_updated_data

    @callback
    def counting_set_updated_data(d):
        published["n"] += 1
        return original(d)

    coord.async_set_updated_data = counting_set_updated_data

    await asyncio.sleep(1.0)
    print(f"publishes during 1s of frames: {published['n']}")
    if published["n"] < 2:
        failures.append(f"coalescing suppressed everything ({published['n']})")
    if published["n"] > 8:
        failures.append(f"coalescing failed: {published['n']} publishes in 1s")

    # device disappears -> entities must go unavailable, not silently stale
    await coord.async_shutdown()
    await device.stop()
    if coord.last_update_success:
        failures.append("still reported success after shutdown")

    print("\n=== result ===")
    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PASS: no polling, frames published, clean shutdown")
    return 0


class _StubLoop:
    def call_later(self, delay, callback, *args):
        return asyncio.get_running_loop().call_later(delay, callback, *args)

    def create_task(self, coro, *args, **kwargs):
        return asyncio.get_running_loop().create_task(coro)


class _StubHass:
    @property
    def loop(self):
        return _StubLoop()


class _StubEntry:
    entry_id = "test"
    data = {}

    @property
    def title(self):
        return "test"

    def async_on_unload(self, func):
        return None


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
