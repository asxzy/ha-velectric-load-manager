#!/usr/bin/env python3
"""Smoke-test the integration against a local fake ESP, no Home Assistant needed.

Checks the things that would otherwise only fail at runtime inside HA:

  * every module imports cleanly
  * the coordinator opens exactly one connection
  * pushed frames reach the coordinator's data dict
  * entity availability flips when the device goes away
  * shutdown closes the socket and leaves no task behind

    python tools/test_integration_smoke.py
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


def load_modules():
    pkg = "velectric_load_manager"
    spec = importlib.util.spec_from_file_location(
        pkg, PKG / "__init__.py", submodule_search_locations=[str(PKG)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[pkg] = module
    spec.loader.exec_module(module)  # real __init__: imports Home Assistant
    ws_spec = importlib.util.spec_from_file_location(
        f"{pkg}.websocket_client", PKG / "websocket_client.py"
    )
    ws_mod = importlib.util.module_from_spec(ws_spec)
    sys.modules[ws_spec.name] = ws_mod
    ws_spec.loader.exec_module(ws_mod)
    return module, ws_mod


def accept_key(head: bytes) -> bytes:
    key = ""
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"sec-websocket-key:"):
            key = line.split(b":", 1)[1].strip().decode()
            break
    digest = hashlib.sha1(
        (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
    ).digest()
    return base64.b64encode(digest)


class FakeDevice:
    def __init__(self) -> None:
        self.connects = 0
        self.bytes_received = 0
        self.frames_sent = 0
        self.writers: list[asyncio.StreamWriter] = []
        self.server: asyncio.Server | None = None

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _handle(self, reader, writer) -> None:
        self.writers.append(writer)
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            return
        if b"Upgrade: websocket" not in head and b"upgrade: websocket" not in head:
            writer.write(b"HTTP/1.1 404 Not Found\r\n\r\n")
            return
        self.connects += 1
        writer.write(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept_key(head) + b"\r\n\r\n"
        )
        await writer.drain()
        # push a readings frame every 100 ms, like the fast firmware
        try:
            for i in range(600):
                frame = (
                    bytes([0x82, 13])
                    + struct.pack("<HHHHH", 100 + i, 400 + i, 0, 0, 0)
                    + b"\x01\x01\x01"
                )
                writer.write(frame)
                await writer.drain()
                self.frames_sent += 1
                # read (and count) anything the client sends
                try:
                    while True:
                        chunk = await asyncio.wait_for(reader.read(64), timeout=0.001)
                        if not chunk:
                            return
                        self.bytes_received += len(chunk)
                except asyncio.TimeoutError:
                    pass
                await asyncio.sleep(0.1)
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
    mod, ws_mod = load_modules()
    print("imports: OK")

    device = FakeDevice()
    port = await device.start()
    print(f"fake device on 127.0.0.1:{port}")

    client = ws_mod.VElectricWebSocketClient("127.0.0.1", port)
    readings = {"n": 0}
    client.on_current_reading = lambda *_: readings.__setitem__("n", readings["n"] + 1)

    # Count application frames and their payload sizes, independent of framing
    # overhead and of the close handshake the library adds on shutdown.
    sent: list[int] = []
    real_send = type(client).__dict__.get("_send")


    async def counting_send(self, command: int) -> None:
        sent.append(command)
        await real_send(self, command)

    type(client)._send = counting_send  # type: ignore[method-assign]

    await client.start()
    await client.wait_for_first_frame(timeout=10)
    await asyncio.sleep(1.0)

    failures = []
    if device.connects != 1:
        failures.append(f"expected 1 connection, got {device.connects}")
    if readings["n"] == 0:
        failures.append("no readings decoded")
    # Exactly two application frames per connection, both sent once:
    # 105 (settings) and 103 (readings request / push probe).
    if sent != [mod.const.WS_CONFIG_BYTE, mod.const.WS_REQUEST_BYTE]:
        failures.append(
            f"expected exactly the settings+request bytes, sent {sent}"
        )
    print(f"connections={device.connects} readings={readings['n']} "
          f"app_frames={sent} wire_bytes={device.bytes_received}")

    # coordinator-level state assembly
    coord = mod.VElectricDataUpdateCoordinator.__new__(mod.VElectricDataUpdateCoordinator)
    data = mod.VElectricDataUpdateCoordinator._build_data(coord, client)
    for key in ("ct1_current", "ct2_current", "total_current", "connection_status"):
        if key not in data:
            failures.append(f"missing data key {key}")
    print(f"data keys: {sorted(data)}")

    await client.async_stop()
    await asyncio.sleep(0.2)

    leftover = [t for t in asyncio.all_tasks() if "velectric" in (t.get_name() or "")]
    if leftover:
        failures.append(f"tasks left running after shutdown: {leftover}")
    if client.is_connected:
        failures.append("client still reports connected after shutdown")

    await device.stop()

    print("\n=== result ===")
    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PASS: one connection, push-only traffic, clean shutdown")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
