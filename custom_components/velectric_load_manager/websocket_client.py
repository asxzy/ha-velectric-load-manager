"""Websocket client for VElectric Load Manager.

Design notes (see docs/validating-esp-retry-impact.md for the measurements):

* The device has a very small TCP pool and its HTTP task wedges when it is
  pushed. This module therefore opens **one** connection and keeps it.
* No periodic PING. The old 2 s 0x67 request loop was 30 requests/minute of pure
  overhead on hardware that cannot afford it.
* The two known firmwares behave differently, so the client measures which one
  it is talking to instead of assuming:
      * one pushes a readings frame about once per second on its own
      * one is strictly request/response and stays silent until asked
  At connect time the client sends exactly one settings read and one readings
  request. Detection is then event based: a readings frame that arrives while no
  request is outstanding proves the device pushes, and polling stops forever. A
  device that only ever answers is polled at `PULL_INTERVAL`, which is
  deliberately slow. Measuring beats guessing, so a device that is merely slow is
  never mistaken for a push device.
* Reconnection is driven by the read loop failing or data going stale, never by
  a timer, and is rate limited by an exponential backoff whose floor exceeds the
  device's observed recovery time.
* Everything is bounded: tiny frame size limit, no compression, short timeouts.
"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from .const import (
    BACKOFF_MAX,
    BACKOFF_MIN,
    MAX_FRAME_SIZE,
    PACKET_SIZE,
    PULL_INTERVAL,
    READ_TIMEOUT,
    STALE_TIMEOUT,
    WS_CONFIG_BYTE,
    WS_REQUEST_BYTE,
)

_LOGGER = logging.getLogger(__name__)


class ConnectionStatus(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"


class LoadStatus(Enum):
    OFF = "off"
    ON = "on"
    WAIT_OFF = "wait-off"
    WAIT_ON = "wait-on"


@dataclass
class LoadConfig:
    """Configuration for a single load channel"""

    load_breaker: int  # Load breaker rating in amps
    turn_on_delay: int  # Delay before turning on load (seconds for wait-on status)
    turn_off_delay: int  # Delay before turning off load (seconds)


@dataclass
class LoadState:
    """Current state of a load channel"""

    status: LoadStatus
    remaining_time: Optional[int] = None  # Remaining time in seconds for wait states


@dataclass
class Settings:
    """VElectric Load Manager configuration settings"""

    main_supply_breaker: int = 100  # Main supply breaker rating in amps
    loads: list[LoadConfig] = None  # Configuration for each load channel (up to 3)
    active_ch: int = 2  # Number of active channels
    ct_index: int = 0  # Current transformer index
    ct_rating: int = 100  # CT rating in amps
    scale: int = 1  # Scaling factor


@dataclass
class CurrentReadings:
    """Current readings from CT1 and CT2"""

    ct1: float  # CT1 current reading in amps
    ct2: float  # CT2 current reading in amps


class VElectricWebSocketClient:
    """Single-connection websocket client for VElectric Load Manager.

    Call `start()` once; it owns the connection for the lifetime of the object.
    Readings are delivered through `on_current_reading`.
    """

    def __init__(
        self,
        host: str,
        port: int = 80,
    ) -> None:
        """Initialize the websocket client."""
        self._host = host
        self._port = port
        self._ws_url = f"ws://{host}:{port}/ws"

        self._websocket: Optional[websockets.WebSocketClientProtocol] = None
        self._task: Optional[asyncio.Task] = None
        self._closing = False
        self._connected = False
        self._first_frame = asyncio.Event()
        self._frames_seen = 0

        # Protocol behaviour, learned at connect time. None until decided.
        # True  = device pushes on its own, never ask again.
        # False = device is request/response, ask at PULL_INTERVAL.
        self._pushes: bool | None = None

        self._latest_readings: dict[str, float] = {"ct1": 0.0, "ct2": 0.0}

        default_loads = [
            LoadConfig(load_breaker=60, turn_on_delay=6, turn_off_delay=10),
            LoadConfig(load_breaker=60, turn_on_delay=8, turn_off_delay=8),
            LoadConfig(load_breaker=60, turn_on_delay=10, turn_off_delay=6),
        ]
        self.settings = Settings(loads=default_loads)

        self.current_readings = CurrentReadings(ct1=0.0, ct2=0.0)
        self.load_status = [LoadState(LoadStatus.OFF) for _ in range(3)]
        self.connection_status = ConnectionStatus.DISCONNECTED
        self.last_frame_time: float | None = None

        self.on_status_change: Optional[Callable] = None
        self.on_current_reading: Optional[Callable] = None
        self.on_settings_update: Optional[Callable] = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Open the connection and keep it open in the background."""
        if self._task and not self._task.done():
            return
        self._closing = False
        self._task = asyncio.create_task(self._run(), name="velectric-ws")

    async def async_stop(self) -> None:
        """Stop the background task and close the socket."""
        self._closing = True
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self._close_socket()
        self._set_status(ConnectionStatus.DISCONNECTED)

    async def wait_for_first_frame(self, timeout: float) -> bool:
        """Block until the device sends anything, or the timeout expires."""
        try:
            await asyncio.wait_for(self._first_frame.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # -- connection loop ---------------------------------------------------

    async def _run(self) -> None:
        """Connect, read until failure, back off, repeat.

        Reconnection is driven by the connection failing or going stale. There is
        no timer that pokes the device.
        """
        backoff = BACKOFF_MIN
        while not self._closing:
            frames_before = self._frames_seen
            try:
                await self._connect_once()
                await self._read_until_closed()
            except asyncio.CancelledError:
                raise
            except (OSError, asyncio.TimeoutError, ConnectionError) as err:
                _LOGGER.debug("Connection attempt failed: %s", err)
            except (ConnectionClosed, WebSocketException) as err:
                _LOGGER.debug("Connection closed: %s", err)
            except Exception as err:  # noqa: BLE001 - keep the task alive
                _LOGGER.warning("Unexpected websocket error: %s", err)
            finally:
                await self._close_socket()

            if self._closing:
                break

            self._set_status(ConnectionStatus.DISCONNECTED)

            # Only a session that actually delivered data proves the device is
            # healthy. A handshake alone is not enough: a wedged ESP still
            # completes the upgrade and then says nothing forever.
            if self._frames_seen > frames_before:
                backoff = BACKOFF_MIN
            else:
                _LOGGER.debug(
                    "Session produced no data; backing off %.0fs", backoff
                )

            _LOGGER.debug("Reconnecting in %.0fs", backoff)
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                raise
            backoff = min(backoff * 2, BACKOFF_MAX)

    async def _connect_once(self) -> None:
        """One connection attempt, with the leanest possible options."""
        self._set_status(ConnectionStatus.CONNECTING)
        self._websocket = await websockets.connect(
            self._ws_url,
            open_timeout=READ_TIMEOUT,
            close_timeout=2,
            ping_interval=None,  # no WS-level keepalive packet every cycle
            ping_timeout=None,
            compression=None,  # no deflate: saves heap on the device
            max_size=MAX_FRAME_SIZE,
            max_queue=1,  # never buffer (and never ask for) a backlog
        )
        self._connected = True
        self._set_status(ConnectionStatus.CONNECTED)

        # Exactly two bytes per connection, both sent once:
        #   105 asks for the settings frame
        #   103 asks for one readings frame
        # The readings request doubles as the probe: if frames keep arriving
        # after it, the device pushes on its own and we never ask again.
        await self._send(WS_CONFIG_BYTE)
        await self._send(WS_REQUEST_BYTE)

    async def _read_until_closed(self) -> None:
        """Read frames, learning whether the device pushes or must be asked.

        Detection is event based, not time based. We track whether a readings
        request is outstanding (`awaiting_readings`):

            a readings frame while one is outstanding -> its answer
            a readings frame with none outstanding      -> the device pushed it

        A pushed frame is proof the device generates data on its own, so polling
        stops permanently. This cannot misclassify a merely slow device. The
        12-byte settings frame is handled separately because it answers the
        one-off 0x69, not the readings request.
        """
        ws = self._websocket
        if ws is None:
            return

        loop = asyncio.get_running_loop()
        next_pull = loop.time() + PULL_INTERVAL
        last_frame_at = loop.time()
        # The readings request sent by _connect_once is still outstanding, but
        # its answer is ambiguous: push firmware often emits a frame right after
        # connecting too. So it cannot decide the protocol on its own.
        awaiting_readings = True
        pending_from_connect = True

        while not self._closing:
            now = loop.time()

            # Only request/response firmware ever gets another request.
            if self._pushes is not True and now >= next_pull:
                await self._send(WS_REQUEST_BYTE)
                awaiting_readings = True
                pending_from_connect = False
                next_pull = loop.time() + PULL_INTERVAL

            # A device that stops answering is as dead as one that disconnects.
            if now - last_frame_at > STALE_TIMEOUT:
                _LOGGER.debug("No frame for %.0fs, reconnecting", STALE_TIMEOUT)
                return

            # Wake in time to poll if we are still polling; otherwise wait.
            if self._pushes is True:
                timeout = STALE_TIMEOUT
            else:
                timeout = max(0.05, min(STALE_TIMEOUT, next_pull - loop.time()))

            try:
                message = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                continue

            if not isinstance(message, (bytes, bytearray)):
                continue

            frame = bytes(message)
            if len(frame) == 12:
                # Settings frame: always the answer to our one-off 0x69.
                self._process_binary_message(frame)
                last_frame_at = loop.time()
                continue

            # A readings frame. If a poll we sent is outstanding, this is its
            # answer. The very first one cannot decide anything, because push
            # firmware may also answer. A frame that arrives while we have sent
            # nothing for a while is proof the device pushes on its own.
            if awaiting_readings:
                awaiting_readings = False
                if pending_from_connect:
                    pending_from_connect = False
                elif self._pushes is None:
                    self._pushes = False
                    _LOGGER.info(
                        "Device only answers requests; polling every %.0fs",
                        PULL_INTERVAL,
                    )
            elif self._pushes is not True:
                self._pushes = True
                _LOGGER.info(
                    "Device pushes readings on its own; "
                    "no further requests will be sent"
                )

            last_frame_at = loop.time()
            self._process_binary_message(frame)

    async def _send(self, command: int) -> None:
        """Send a single-byte command if the socket is usable."""
        ws = self._websocket
        if ws is None or not self._connected:
            return
        try:
            await ws.send(struct.pack("B", command))
        except (ConnectionClosed, WebSocketException, OSError) as err:
            _LOGGER.debug("Send failed: %s", err)
            raise

    async def _close_socket(self) -> None:
        """Close the socket exactly once, whatever state we are in."""
        ws = self._websocket
        self._websocket = None
        self._connected = False
        if ws is None:
            return
        try:
            await ws.close()
        except (ConnectionClosed, WebSocketException, OSError):
            pass
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Error while closing socket: %s", err)

    # -- state -------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """Return True if connected to the device."""
        return self._connected

    @property
    def last_frame_age(self) -> float | None:
        """Seconds since the last frame, or None if nothing has arrived yet."""
        if self.last_frame_time is None:
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        return loop.time() - self.last_frame_time

    def _set_status(self, status: ConnectionStatus) -> None:
        if status is self.connection_status:
            return
        self.connection_status = status
        if self.on_status_change:
            self.on_status_change(status)

    # -- decoding ----------------------------------------------------------

    def decode_currents(self, packet: bytes) -> dict[str, float]:
        """Decode a 14-byte packet and extract current readings for ct1 and ct2."""
        if len(packet) != PACKET_SIZE:
            _LOGGER.warning(
                "Invalid packet size: %d bytes (expected %d)", len(packet), PACKET_SIZE
            )
            return {"ct1": 0.0, "ct2": 0.0}

        try:
            raw1, raw2 = struct.unpack_from("<HH", packet, 0)
            return {
                "ct1": round(math.sqrt(raw1), 1),
                "ct2": round(math.sqrt(raw2), 1),
            }
        except struct.error as err:
            _LOGGER.error("Failed to decode packet: %s", err)
            return {"ct1": 0.0, "ct2": 0.0}

    def _process_binary_message(self, data: bytes) -> None:
        """Dispatch a frame by its length.

        Two shapes exist:
          12 bytes -> settings/configuration
          >=13     -> readings and load status
          14 bytes -> legacy current-only
        """
        self.last_frame_time = asyncio.get_running_loop().time()
        self._frames_seen += 1
        if not self._first_frame.is_set():
            self._first_frame.set()
        if len(data) == 12:
            self._process_settings_message(data)
        else:
            self._process_readings_message(data)

    def _process_settings_message(self, data: bytes) -> None:
        """Parse the 12-byte settings frame."""
        main_breaker = data[0]

        loads = []
        for i in range(3):
            base_idx = i * 3 + 1
            loads.append(
                LoadConfig(
                    load_breaker=data[base_idx],
                    turn_on_delay=data[base_idx + 1],
                    turn_off_delay=data[base_idx + 2],
                )
            )

        active_ch = data[10]
        ct_index = data[11]

        self.settings = Settings(
            main_supply_breaker=main_breaker,
            loads=loads,
            active_ch=active_ch,
            ct_index=ct_index,
            ct_rating=100 * (ct_index + 1),
            scale=1,
        )

        if self.on_settings_update:
            self.on_settings_update(self.settings)

    def _process_readings_message(self, data: bytes) -> None:
        """Parse a readings frame and update state."""
        if len(data) < 13:
            if len(data) == PACKET_SIZE:
                readings = self.decode_currents(data)
                self._latest_readings = readings
                self.current_readings = CurrentReadings(
                    ct1=readings["ct1"], ct2=readings["ct2"]
                )
                if self.on_current_reading:
                    self.on_current_reading(self.current_readings, self.load_status)
            return

        ct1_raw = struct.unpack("<H", data[0:2])[0]
        ct2_raw = struct.unpack("<H", data[2:4])[0]

        self.current_readings = CurrentReadings(
            ct1=round(math.sqrt(ct1_raw), 1),
            ct2=round(math.sqrt(ct2_raw), 1),
        )
        self._latest_readings = {
            "ct1": self.current_readings.ct1,
            "ct2": self.current_readings.ct2,
        }

        load_counters = [
            struct.unpack("<H", data[4:6])[0],
            struct.unpack("<H", data[6:8])[0],
            struct.unpack("<H", data[8:10])[0],
        ]
        load_status_bytes = [data[10], data[11], data[12]]

        turn_on_delays = [load.turn_on_delay for load in self.settings.loads]
        turn_off_delays = [load.turn_off_delay for load in self.settings.loads]

        self.load_status = []
        for i in range(3):
            status_byte = load_status_bytes[i]
            remaining_time = None
            if status_byte == 1:
                status = LoadStatus.ON
            elif status_byte == 2:
                status = LoadStatus.WAIT_OFF
                remaining_time = max(0, turn_off_delays[i] - load_counters[i])
            elif status_byte == 3:
                status = LoadStatus.WAIT_ON
                remaining_time = max(0, turn_on_delays[i] * 60 - load_counters[i])
            else:
                status = LoadStatus.OFF
            self.load_status.append(
                LoadState(status=status, remaining_time=remaining_time)
            )

        if self.on_current_reading:
            self.on_current_reading(self.current_readings, self.load_status)

    def get_state_dict(self) -> dict:
        """Get current state as dictionary for serialization"""
        return {
            "connection_status": self.connection_status.value,
            "current_readings": asdict(self.current_readings),
            "load_status": [asdict(load) for load in self.load_status],
            "settings": asdict(self.settings),
            "host": self._host,
            "port": self._port,
        }
