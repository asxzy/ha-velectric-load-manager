#!/usr/bin/env python3
"""Prove the reconnect/leak behaviour of the integration's own client.

This runs the real `VElectricWebSocketClient` from
custom_components/velectric_load_manager/websocket_client.py against a local
server that counts sockets, and shows two things:

 1. how many sockets the client leaves open when the server drops a connection
 2. whether those stragglers accumulate across reconnects

Run the same scenario against the device itself afterwards to confirm, but do
the local run first: it costs nothing and cannot reboot real hardware.

    python tools/repro_reconnect_leak.py                 # 5 cycles
    python tools/repro_reconnect_leak.py --cycles 10

Output is a table: cycle | server live sockets | client thinks connected |
leaked. "Leaked" counts server-side sockets the client still holds after the
server ended the session.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib.util
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "custom_components" / "velectric_load_manager"

WS_REQUEST_BYTE = 103


def load_client_class():
    """Import websocket_client.py without importing Home Assistant."""
    pkg_name = "velectric_load_manager"
    if pkg_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            pkg_name, PKG / "__init__.py", submodule_search_locations=[str(PKG)]
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[pkg_name] = module
        # deliberately do NOT exec __init__.py: it pulls in Home Assistant.
        # Only register the package so relative imports inside the submodule work.
    spec = importlib.util.spec_from_file_location(
        f"{pkg_name}.websocket_client", PKG / "websocket_client.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.VElectricWebSocketClient


class CountingServer:
    """Minimal /ws server that counts live and leaked sockets."""

    def __init__(self) -> None:
        self.live = 0
        self.total = 0
        self.peer_closes = 0
        self.server_writers: dict[int, asyncio.StreamWriter] = {}
        self._next_id = 0
        self.server: asyncio.Server | None = None

    async def start(self, port: int = 0) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", port)
        return self.server.sockets[0].getsockname()[1]

    @staticmethod
    def _accept_key(head: bytes) -> bytes:
        key = ""
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"sec-websocket-key:"):
                key = line.split(b":", 1)[1].strip().decode()
                break
        digest = hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
        ).digest()
        return base64.b64encode(digest)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._next_id += 1
        sock_id = self._next_id
        self.total += 1
        self.live += 1
        self.server_writers[sock_id] = writer
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
                return
            if b"Upgrade: websocket" not in head and b"upgrade: websocket" not in head:
                writer.write(b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            accept = self._accept_key(head)
            writer.write(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
            )
            await writer.drain()

            # behave like the firmware: send a readings frame each second,
            # read and discard client frames
            async def pump() -> None:
                for _ in range(3600):
                    frame = (
                        bytes([0x82, 13])
                        + struct.pack("<HHHHH", 1200, 900, 0, 0, 0)
                        + b"\x01\x00\x00"
                    )
                    writer.write(frame)
                    await writer.drain()
                    await asyncio.sleep(1.0)

            pump_task = asyncio.create_task(pump())
            try:
                while True:
                    chunk = await reader.read(4096)
                    if not chunk:
                        self.peer_closes += 1
                        return
            finally:
                pump_task.cancel()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.live -= 1
            self.server_writers.pop(sock_id, None)
            try:
                writer.close()
            except Exception:
                pass

    async def kill_all(self) -> int:
        """Force-close every live connection, like an ESP running out of PCBs."""
        killed = 0
        for writer in list(self.server_writers.values()):
            try:
                writer.transport.abort()
                killed += 1
            except Exception:
                pass
        return killed

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    def snapshot(self) -> tuple[int, int, int]:
        return self.live, self.total, self.peer_closes


async def main_async(args: argparse.Namespace) -> int:
    Client = load_client_class()
    server = CountingServer()
    port = await server.start()
    print(f"counting server on 127.0.0.1:{port}/ws")

    client = Client("127.0.0.1", port)
    rows: list[tuple] = []

    for cycle in range(1, args.cycles + 1):
        try:
            await asyncio.wait_for(client.connect(), timeout=10)
        except Exception as err:
            print(f"cycle {cycle}: connect failed: {err}")
            break

        await asyncio.sleep(args.up)

        _live_before, _total_before, _ = server.snapshot()
        # server-side drop, exactly what a rebooting/recovering ESP does
        killed = await server.kill_all()
        await asyncio.sleep(args.down)

        live_after, total_after, peer_closes = server.snapshot()
        leaked = max(0, live_after - 0)
        rows.append(
            (
                cycle,
                killed,
                live_after,
                leaked,
                client.is_connected,
                client.connection_status.value,
            )
        )
        print(
            f"cycle {cycle}: killed={killed} server_live_after={live_after} "
            f"client_connected={client.is_connected} "
            f"client_status={client.connection_status.value}"
        )

        # the integration's recovery path: disconnect() then reconnect
        try:
            await asyncio.wait_for(client.disconnect(), timeout=10)
        except Exception as err:
            print(f"  disconnect raised: {err!r}")

        live_dc, _, peer_closes2 = server.snapshot()
        if live_dc:
            print(
                f"  after disconnect(): {live_dc} server socket(s) still open "
                f"(client did not close them)  <-- LEAK"
            )
        if peer_closes2:
            print(f"  server saw {peer_closes2} clean client close(s) so far")

    print("\n=== verdict ===")
    if not rows:
        print("no data")
        return 2

    never_closed = server.snapshot()
    print(
        f"server sockets opened={never_closed[1]} "
        f"still open={never_closed[0]} "
        f"client-initiated clean closes={never_closed[2]}"
    )

    if never_closed[2] == 0 and never_closed[1] > 1:
        print("CONFIRMED: the client never closed a single socket itself.")
        print("          Every reconnect therefore consumes a fresh slot on the")
        print("          device. On an ESP with a small LWIP PCB pool this ends")
        print("          in 'accepts TCP, answers nothing' and a watchdog reboot.")
    elif never_closed[0] > 0:
        print(f"CONFIRMED: {never_closed[0]} socket(s) left open by the client.")
    else:
        print("No leak observed in this run.")

    await client.disconnect()
    await server.stop()
    return 0 if never_closed[0] == 0 else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--cycles", type=int, default=5)
    p.add_argument("--up", type=float, default=3.0, help="seconds connected each cycle")
    p.add_argument(
        "--down", type=float, default=2.0, help="seconds after the forced drop"
    )
    args = p.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
