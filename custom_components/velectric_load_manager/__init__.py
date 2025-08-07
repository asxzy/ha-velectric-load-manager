"""VElectric Load Manager integration for Home Assistant."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv, device_registry as dr
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

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.NUMBER]

# Service schemas
SERVICE_SAVE_CONFIG = "save_config"
SERVICE_SEND_CONFIG = "send_config"
SERVICE_UPDATE_LOAD_SETTING = "update_load_setting"
SERVICE_UPDATE_MAIN_SETTINGS = "update_main_settings"

SERVICE_UPDATE_LOAD_SETTING_SCHEMA = vol.Schema(
    {
        vol.Required("load_index"): cv.positive_int,
        vol.Required("setting_name"): vol.In(
            ["load_breaker", "turn_on_delay", "turn_off_delay"]
        ),
        vol.Required("value"): cv.positive_int,
    }
)

SERVICE_UPDATE_MAIN_SETTINGS_SCHEMA = vol.Schema(
    {
        vol.Optional("main_supply_breaker"): cv.positive_int,
        vol.Optional("active_ch"): vol.All(cv.positive_int, vol.Range(min=1, max=3)),
        vol.Optional("ct_index"): vol.All(cv.positive_int, vol.Range(min=0, max=2)),
    }
)


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

    # Register services if this is the first integration instance
    if len(hass.data[DOMAIN]) == 1:
        await _async_register_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()

        # Unregister services if this was the last integration instance
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_SAVE_CONFIG)
            hass.services.async_remove(DOMAIN, SERVICE_SEND_CONFIG)
            hass.services.async_remove(DOMAIN, SERVICE_UPDATE_LOAD_SETTING)
            hass.services.async_remove(DOMAIN, SERVICE_UPDATE_MAIN_SETTINGS)

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
                    backoff_delay = min(2 ** (self._connection_failures - 1), 300)  # Max 5 minutes
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
                err
            )
            raise UpdateFailed(f"Error communicating with device: {err}") from err

    async def async_shutdown(self) -> None:
        """Shutdown the coordinator and close connections."""
        if self._client:
            await self._client.disconnect()
            self._client = None


async def _async_register_services(hass: HomeAssistant) -> None:
    """Register services for VElectric Load Manager."""

    async def save_config_service(call: ServiceCall) -> None:
        """Handle save config service call."""
        try:
            coordinator = await _get_coordinator_from_service_call(hass, call)
            if not coordinator:
                raise ValueError("No VElectric device found")
            if not coordinator._client or not coordinator._client.is_connected:
                raise ValueError("Device not connected")
            await coordinator._client.save_config()
            _LOGGER.info("Configuration saved successfully")
        except Exception as err:
            _LOGGER.error("Failed to save configuration: %s", err)
            hass.components.persistent_notification.create(
                f"Failed to save VElectric configuration: {err}",
                title="VElectric Load Manager",
                notification_id="velectric_save_config_error",
            )

    async def send_config_service(call: ServiceCall) -> None:
        """Handle send config service call."""
        try:
            coordinator = await _get_coordinator_from_service_call(hass, call)
            if not coordinator:
                raise ValueError("No VElectric device found")
            if not coordinator._client or not coordinator._client.is_connected:
                raise ValueError("Device not connected")
            await coordinator._client.send_config_to_server()
            _LOGGER.info("Configuration sent successfully")
        except Exception as err:
            _LOGGER.error("Failed to send configuration: %s", err)
            hass.components.persistent_notification.create(
                f"Failed to send VElectric configuration: {err}",
                title="VElectric Load Manager",
                notification_id="velectric_send_config_error",
            )

    async def update_load_setting_service(call: ServiceCall) -> None:
        """Handle update load setting service call."""
        try:
            coordinator = await _get_coordinator_from_service_call(hass, call)
            if not coordinator:
                raise ValueError("No VElectric device found")
            if not coordinator._client:
                raise ValueError("Device client not available")
            
            load_index = call.data["load_index"]
            setting_name = call.data["setting_name"]
            value = call.data["value"]
            
            if load_index < 0 or load_index > 2:
                raise ValueError("Load index must be 0, 1, or 2")
                
            coordinator._client.update_load_setting(load_index, setting_name, value)
            _LOGGER.info("Load %d setting %s updated to %s", load_index + 1, setting_name, value)
        except Exception as err:
            _LOGGER.error("Failed to update load setting: %s", err)
            hass.components.persistent_notification.create(
                f"Failed to update VElectric load setting: {err}",
                title="VElectric Load Manager",
                notification_id="velectric_load_setting_error",
            )

    async def update_main_settings_service(call: ServiceCall) -> None:
        """Handle update main settings service call."""
        try:
            coordinator = await _get_coordinator_from_service_call(hass, call)
            if not coordinator:
                raise ValueError("No VElectric device found")
            if not coordinator._client:
                raise ValueError("Device client not available")
                
            # Filter out None values and pass to update_settings
            settings = {k: v for k, v in call.data.items() if v is not None}
            if not settings:
                raise ValueError("No settings provided to update")
                
            coordinator._client.update_settings(**settings)
            _LOGGER.info("Main settings updated: %s", settings)
        except Exception as err:
            _LOGGER.error("Failed to update main settings: %s", err)
            hass.components.persistent_notification.create(
                f"Failed to update VElectric main settings: {err}",
                title="VElectric Load Manager",
                notification_id="velectric_main_settings_error",
            )

    hass.services.async_register(DOMAIN, SERVICE_SAVE_CONFIG, save_config_service)
    hass.services.async_register(DOMAIN, SERVICE_SEND_CONFIG, send_config_service)
    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_LOAD_SETTING,
        update_load_setting_service,
        schema=SERVICE_UPDATE_LOAD_SETTING_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_MAIN_SETTINGS,
        update_main_settings_service,
        schema=SERVICE_UPDATE_MAIN_SETTINGS_SCHEMA,
    )


async def _get_coordinator_from_service_call(
    hass: HomeAssistant, call: ServiceCall
) -> VElectricDataUpdateCoordinator | None:
    """Get coordinator from service call device target."""
    if not call.data.get("device_id"):
        # If no device specified, use the first available coordinator
        coordinators = list(hass.data[DOMAIN].values())
        if coordinators:
            return coordinators[0]
        return None

    device_registry = dr.async_get(hass)
    device_id = call.data["device_id"]
    device = device_registry.async_get(device_id)

    if not device:
        return None

    # Find the config entry for this device
    for identifier in device.identifiers:
        if identifier[0] == DOMAIN:
            entry_id = identifier[1]
            return hass.data[DOMAIN].get(entry_id)

    return None
