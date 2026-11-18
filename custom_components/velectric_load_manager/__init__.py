"""VElectric Load Manager integration for Home Assistant."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_HOST,
    CONF_PORT,
    DOMAIN,
)
from .version import __version__
from .websocket_client import VElectricWebSocketClient

__version__ = __version__

_LOGGER = logging.getLogger(__name__)

# Read-only monitoring integration - no configuration editing capabilities
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

# The device is an ESP with a small TCP pool and an HTTP task that wedges when
# it is pushed. Hold one websocket open; the client decides whether that device
# pushes readings or has to be asked, and only asks the ones that need it.

# How long setup waits for the device's first frame before giving up.
FIRST_FRAME_TIMEOUT = 15.0

# Minimum seconds between state publishes. Frames arrive ~1/s; the recorder and
# state machine do not need that cadence.
PUBLISH_INTERVAL = 5.0


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up VElectric Load Manager from a config entry."""
    host = entry.data[CONF_HOST]
    port = entry.data.get(CONF_PORT, 80)

    coordinator = VElectricDataUpdateCoordinator(hass, entry, host, port)

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        # Setup will be retried. Tear the connection down first, otherwise every
        # retry would leave another websocket task running against a device that
        # is already struggling.
        await coordinator.async_shutdown()
        raise

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()

    return unload_ok


class VElectricDataUpdateCoordinator(DataUpdateCoordinator):
    """Keep one websocket connection open and publish the frames it delivers.

    The coordinator itself never polls: `update_interval` is None and
    `_async_update_data` only runs for the initial setup or an explicit refresh.
    Device traffic is driven entirely by the client's read loop.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        host: str,
        port: int,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=None,  # never poll the device from a timer
        )
        self._host = host
        self._port = port
        self._client: VElectricWebSocketClient | None = None
        # Device frames arrive about once per second. Coalesce them so the state
        # machine and the recorder see a sane cadence, not the wire cadence. This
        # costs the device nothing: it still sends what it sends.
        self._publish_interval = PUBLISH_INTERVAL
        self._publish_handle = None
        self._shutting_down = False

    async def _async_update_data(self) -> dict[str, Any]:
        """Return the latest pushed values, opening the connection if needed."""
        client = self._client
        if client is None:
            client = VElectricWebSocketClient(self._host, self._port)
            client.on_current_reading = self._handle_reading
            client.on_status_change = self._handle_status
            self._client = client
            await client.start()

            # Bounded wait for the first frame. A device that answers nothing is
            # reported as failed, but we keep the single connection and let the
            # client's own backoff handle retries.
            got_frame = await client.wait_for_first_frame(
                timeout=FIRST_FRAME_TIMEOUT
            )
            if not got_frame:
                raise UpdateFailed(
                    f"No data from VElectric device at {self._host}:{self._port}"
                )

        return self._build_data(client)

    def _build_data(self, client: VElectricWebSocketClient) -> dict[str, Any]:
        """Flatten client state into the dict the entities consume."""
        result: dict[str, Any] = {
            "ct1_current": client.current_readings.ct1,
            "ct2_current": client.current_readings.ct2,
            "total_current": client.current_readings.ct1
            + client.current_readings.ct2,
            "connection_status": "Connected" if client.is_connected else "Disconnected",
        }

        for i, load_state in enumerate(client.load_status):
            load_num = i + 1
            result[f"load{load_num}_status"] = load_state.status.value
            result[f"load{load_num}_remaining_time"] = load_state.remaining_time

        if client.settings:
            result["main_supply_breaker"] = client.settings.main_supply_breaker
            result["active_channels"] = client.settings.active_ch
            result["ct_rating"] = client.settings.ct_rating
            result["ct_index"] = client.settings.ct_index
            for i, load_config in enumerate(client.settings.loads):
                load_num = i + 1
                result[f"load{load_num}_breaker"] = load_config.load_breaker
                result[f"load{load_num}_turn_on_delay"] = load_config.turn_on_delay
                result[f"load{load_num}_turn_off_delay"] = load_config.turn_off_delay

        return result

    @callback
    def _handle_reading(self, readings, load_status) -> None:
        """Publish a freshly pushed reading, coalesced to the publish interval."""
        if self._client is None:
            return
        if self._publish_handle is not None:
            return
        self._publish_handle = self.hass.loop.call_later(
            self._publish_interval, self._publish_now
        )

    @callback
    def _publish_now(self) -> None:
        """Push the latest state to the entities."""
        self._publish_handle = None
        if self._client is None:
            return
        self.async_set_updated_data(self._build_data(self._client))

    @callback
    def _handle_status(self, status) -> None:
        """Publish connection state changes."""
        if self._client is None:
            return
        self._cancel_publish()
        if self._shutting_down:
            return
        if status.value == "disconnected":
            # Keep the last values but mark the entities unavailable.
            self.async_set_update_error(
                UpdateFailed("Disconnected from VElectric device")
            )
        else:
            self.async_set_updated_data(self._build_data(self._client))

    @callback
    def _cancel_publish(self) -> None:
        """Cancel a pending coalesced publish."""
        if self._publish_handle is not None:
            self._publish_handle.cancel()
            self._publish_handle = None

    async def async_update_config(self, host: str, port: int) -> None:
        """Update coordinator configuration."""
        connection_changed = self._host != host or self._port != port

        self._host = host
        self._port = port

        if connection_changed:
            client = self._client
            # Detach first so the client's shutdown callback does not report a
            # spurious "device unavailable" for a user-initiated host change.
            self._client = None
            if client:
                await client.async_stop()
            await self.async_request_refresh()

    async def async_shutdown(self) -> None:
        """Shutdown the coordinator and close connections."""
        self._shutting_down = True
        self._cancel_publish()
        client = self._client
        self._client = None
        if client:
            await client.async_stop()
