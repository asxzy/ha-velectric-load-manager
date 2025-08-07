"""VElectric Load Manager integration for Home Assistant."""
from __future__ import annotations

import asyncio
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
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PING_INTERVAL,
)
from .websocket_client import VElectricWebSocketClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up VElectric Load Manager from a config entry."""
    host = entry.data[CONF_HOST]
    port = entry.data.get(CONF_PORT, 80)
    
    coordinator = VElectricDataUpdateCoordinator(hass, host, port)
    
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
    
    def __init__(self, hass: HomeAssistant, host: str, port: int) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self._host = host
        self._port = port
        self._client: VElectricWebSocketClient | None = None
        self._connected = False
    
    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from VElectric Load Manager."""
        if not self._client:
            self._client = VElectricWebSocketClient(self._host, self._port)
        
        try:
            if not self._connected:
                await self._client.connect()
                self._connected = True
            
            data = await self._client.get_readings()
            return {
                "ct1_current": data.get("ct1", 0.0),
                "ct2_current": data.get("ct2", 0.0),
                "connection_status": "Connected",
            }
        except Exception as err:
            self._connected = False
            _LOGGER.warning("Error communicating with VElectric device: %s", err)
            raise UpdateFailed(f"Error communicating with device: {err}") from err
    
    async def async_shutdown(self) -> None:
        """Shutdown the coordinator and close connections."""
        if self._client:
            await self._client.disconnect()
            self._client = None
        self._connected = False