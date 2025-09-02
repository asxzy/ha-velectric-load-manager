"""VElectric Load Manager integration for Home Assistant."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_HOST,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .websocket_client import VElectricWebSocketClient

_LOGGER = logging.getLogger(__name__)

# Read-only monitoring integration - no configuration editing capabilities
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up VElectric Load Manager from a config entry."""
    host = entry.data[CONF_HOST]
    port = entry.data.get(CONF_PORT, 80)
    scan_interval = entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)

    coordinator = VElectricDataUpdateCoordinator(hass, host, port, scan_interval)
    coordinator.config_entry = entry  # Store reference for sensors to access config

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Config updates are handled directly in the options flow to preserve entity state

    # No services registered - integration is read-only

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()

        # No services to unregister - integration is read-only

    return unload_ok


class VElectricDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from VElectric Load Manager."""

    def __init__(
        self, hass: HomeAssistant, host: str, port: int, scan_interval: int
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self._host = host
        self._port = port
        self._scan_interval = scan_interval
        self._client: VElectricWebSocketClient | None = None
        self._connection_failures = 0
        self._last_connection_attempt = 0

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from VElectric Load Manager."""
        if not self._client:
            self._client = VElectricWebSocketClient(self._host, self._port)

        try:
            if not self._client.is_connected:
                # Implement exponential backoff for connection failures
                current_time = time.time()
                if self._connection_failures > 0:
                    backoff_delay = min(
                        2 ** (self._connection_failures - 1), 300
                    )  # Max 5 minutes
                    if current_time - self._last_connection_attempt < backoff_delay:
                        raise UpdateFailed(
                            f"Connection backing off, retry in {backoff_delay - (current_time - self._last_connection_attempt):.0f}s"
                        )

                self._last_connection_attempt = current_time
                await self._client.connect()

            data = await self._client.get_readings()

            # Reset failure count on successful connection
            self._connection_failures = 0

            # Build comprehensive data dict with current readings, load status, and settings
            result = {
                "ct1_current": data.get("ct1", 0.0),
                "ct2_current": data.get("ct2", 0.0),
                "connection_status": "Connected",
            }

            # Add load status data
            for i, load_state in enumerate(self._client.load_status):
                load_num = i + 1
                result[f"load{load_num}_status"] = load_state.status.value
                result[f"load{load_num}_remaining_time"] = load_state.remaining_time

            # Add settings data
            if self._client.settings:
                result["main_supply_breaker"] = (
                    self._client.settings.main_supply_breaker
                )
                result["active_channels"] = self._client.settings.active_ch
                result["ct_rating"] = self._client.settings.ct_rating
                result["ct_index"] = self._client.settings.ct_index

                # Add load configuration
                for i, load_config in enumerate(self._client.settings.loads):
                    load_num = i + 1
                    result[f"load{load_num}_breaker"] = load_config.load_breaker
                    result[f"load{load_num}_turn_on_delay"] = load_config.turn_on_delay
                    result[f"load{load_num}_turn_off_delay"] = (
                        load_config.turn_off_delay
                    )

            return result

        except Exception as err:
            # Increment failure count for exponential backoff
            self._connection_failures += 1

            # Reset client on connection failure to force reconnection
            if self._client:
                await self._client.disconnect()
                self._client = None

            _LOGGER.warning(
                "Error communicating with VElectric device (failure %d): %s",
                self._connection_failures,
                err,
            )
            raise UpdateFailed(f"Error communicating with device: {err}") from err

    async def async_update_config(
        self, host: str, port: int, scan_interval: int
    ) -> None:
        """Update coordinator configuration."""
        # Check if connection settings changed
        connection_changed = self._host != host or self._port != port

        # Update internal settings
        self._host = host
        self._port = port

        # Update scan interval
        if self._scan_interval != scan_interval:
            self._scan_interval = scan_interval
            self.update_interval = timedelta(seconds=scan_interval)

        # If connection settings changed, disconnect and force reconnection
        if connection_changed:
            if self._client:
                await self._client.disconnect()
                self._client = None

            # Reset connection failure tracking
            self._connection_failures = 0
            self._last_connection_attempt = 0

            # Trigger immediate update to test new connection
            await self.async_request_refresh()

    async def async_shutdown(self) -> None:
        """Shutdown the coordinator and close connections."""
        if self._client:
            await self._client.disconnect()
            self._client = None


# All service functions removed for safety - integration is read-only monitoring only
