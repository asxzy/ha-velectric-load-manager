"""VElectric Load Manager integration for Home Assistant."""

from __future__ import annotations

import logging
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

PLATFORMS: list[Platform] = [Platform.SENSOR]


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

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()

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

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from VElectric Load Manager."""
        if not self._client:
            self._client = VElectricWebSocketClient(self._host, self._port)

        try:
            if not self._client.is_connected:
                await self._client.connect()

            data = await self._client.get_readings()
            return {
                "ct1_current": data.get("ct1", 0.0),
                "ct2_current": data.get("ct2", 0.0),
                "connection_status": "Connected",
            }
        except Exception as err:
            # Reset client on connection failure to force reconnection
            if self._client:
                await self._client.disconnect()
                self._client = None
            _LOGGER.warning("Error communicating with VElectric device: %s", err)
            raise UpdateFailed(f"Error communicating with device: {err}") from err

    async def async_shutdown(self) -> None:
        """Shutdown the coordinator and close connections."""
        if self._client:
            await self._client.disconnect()
            self._client = None
