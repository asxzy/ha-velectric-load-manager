"""Websocket client for VElectric Load Manager."""

from __future__ import annotations

import asyncio
import logging
import math
import struct

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from .const import PACKET_SIZE, PING_INTERVAL, WS_REQUEST_BYTE

_LOGGER = logging.getLogger(__name__)


class VElectricWebSocketClient:
    """Websocket client for VElectric Load Manager."""

    def __init__(self, host: str, port: int = 80) -> None:
        """Initialize the websocket client."""
        self._host = host
        self._port = port
        self._ws_url = f"ws://{host}:{port}/ws"
        self._websocket: websockets.WebSocketClientProtocol | None = None
        self._connected = False
        self._ping_task: asyncio.Task | None = None
        self._message_task: asyncio.Task | None = None
        self._latest_readings: dict[str, float] = {"ct1": 0.0, "ct2": 0.0}
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Connect to the VElectric device."""
        if self._connected:
            return

        try:
            _LOGGER.debug("Connecting to VElectric device at %s", self._ws_url)
            self._websocket = await websockets.connect(self._ws_url)
            self._connected = True
            _LOGGER.info("Connected to VElectric device at %s", self._ws_url)

            # Start the ping loop and message handler
            self._ping_task = asyncio.create_task(self._ping_loop())
            self._message_task = asyncio.create_task(self._message_handler())

        except Exception as err:
            _LOGGER.error("Failed to connect to VElectric device: %s", err)
            self._connected = False
            raise

    async def disconnect(self) -> None:
        """Disconnect from the VElectric device."""
        if not self._connected:
            return

        _LOGGER.debug("Disconnecting from VElectric device")
        self._connected = False

        # Cancel tasks with timeout protection
        tasks_to_cancel = [self._ping_task, self._message_task]
        for task in tasks_to_cancel:
            if task:
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
        
        self._ping_task = None
        self._message_task = None

        if self._websocket:
            try:
                await asyncio.wait_for(self._websocket.close(), timeout=5.0)
            except asyncio.TimeoutError:
                _LOGGER.warning("WebSocket close timed out")
            finally:
                self._websocket = None

        _LOGGER.info("Disconnected from VElectric device")

    async def get_readings(self) -> dict[str, float]:
        """Get the latest current readings."""
        if not self._connected:
            raise ConnectionError("Not connected to VElectric device")

        async with self._lock:
            return self._latest_readings.copy()

    def decode_currents(self, packet: bytes) -> dict[str, float]:
        """Decode a 14-byte packet and extract current readings for ct1 and ct2."""
        if len(packet) != PACKET_SIZE:
            _LOGGER.warning(
                "Invalid packet size: %d bytes (expected %d)", len(packet), PACKET_SIZE
            )
            return {"ct1": 0.0, "ct2": 0.0}

        try:
            raw1, raw2 = struct.unpack_from("<HH", packet, 0)
            readings = {
                "ct1": round(math.sqrt(raw1), 1),
                "ct2": round(math.sqrt(raw2), 1),
            }
            _LOGGER.debug("Decoded readings: %s", readings)
            return readings
        except struct.error as err:
            _LOGGER.error("Failed to decode packet: %s", err)
            return {"ct1": 0.0, "ct2": 0.0}

    async def _ping_loop(self) -> None:
        """Send periodic ping requests to get readings."""
        while self._connected and self._websocket:
            try:
                await self._websocket.send(bytes([WS_REQUEST_BYTE]))
                _LOGGER.debug("Sent reading request")
                await asyncio.sleep(PING_INTERVAL)
            except (ConnectionClosed, WebSocketException) as err:
                _LOGGER.warning("Connection lost during ping: %s", err)
                self._connected = False
                break
            except Exception as err:
                _LOGGER.error("Error in ping loop: %s", err)
                self._connected = False
                break

    async def _message_handler(self) -> None:
        """Handle incoming websocket messages."""
        if not self._websocket:
            return

        try:
            async for message in self._websocket:
                if (
                    isinstance(message, (bytes, bytearray))
                    and len(message) == PACKET_SIZE
                ):
                    readings = self.decode_currents(message)
                    async with self._lock:
                        self._latest_readings = readings
                    _LOGGER.debug("Updated readings: %s", readings)
                else:
                    _LOGGER.debug("Received unexpected message: %s", message)
        except (ConnectionClosed, WebSocketException) as err:
            _LOGGER.warning("Connection lost in message handler: %s", err)
            self._connected = False
        except Exception as err:
            _LOGGER.error("Error in message handler: %s", err)
            self._connected = False

    @property
    def is_connected(self) -> bool:
        """Return True if connected to the device."""
        return self._connected
