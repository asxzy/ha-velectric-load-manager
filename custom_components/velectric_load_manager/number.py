"""Number platform for VElectric Load Manager."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import VElectricDataUpdateCoordinator
from .const import (
    CONF_HOST,
    CONF_NAME,
    DOMAIN,
    MANUFACTURER,
    MODEL,
    SENSOR_MAIN_BREAKER,
    SENSOR_ACTIVE_CHANNELS,
    SENSOR_LOAD1_BREAKER,
    SENSOR_LOAD2_BREAKER,
    SENSOR_LOAD3_BREAKER,
    SENSOR_LOAD1_TURN_ON_DELAY,
    SENSOR_LOAD2_TURN_ON_DELAY,
    SENSOR_LOAD3_TURN_ON_DELAY,
    SENSOR_LOAD1_TURN_OFF_DELAY,
    SENSOR_LOAD2_TURN_OFF_DELAY,
    SENSOR_LOAD3_TURN_OFF_DELAY,
    SENSOR_NAMES,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the VElectric Load Manager number entities."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    host = config_entry.data[CONF_HOST]
    device_name = config_entry.data.get(CONF_NAME, f"VElectric Load Manager ({host})")

    entities = [
        # Main supply breaker
        VElectricBreakerNumber(
            coordinator, config_entry, SENSOR_MAIN_BREAKER, host, device_name
        ),
        # Active channels
        VElectricChannelsNumber(
            coordinator, config_entry, SENSOR_ACTIVE_CHANNELS, host, device_name
        ),
        # Load breakers
        VElectricBreakerNumber(
            coordinator, config_entry, SENSOR_LOAD1_BREAKER, host, device_name
        ),
        VElectricBreakerNumber(
            coordinator, config_entry, SENSOR_LOAD2_BREAKER, host, device_name
        ),
        VElectricBreakerNumber(
            coordinator, config_entry, SENSOR_LOAD3_BREAKER, host, device_name
        ),
        # Turn on delays (in minutes)
        VElectricDelayNumber(
            coordinator,
            config_entry,
            SENSOR_LOAD1_TURN_ON_DELAY,
            host,
            device_name,
            is_turn_on=True,
        ),
        VElectricDelayNumber(
            coordinator,
            config_entry,
            SENSOR_LOAD2_TURN_ON_DELAY,
            host,
            device_name,
            is_turn_on=True,
        ),
        VElectricDelayNumber(
            coordinator,
            config_entry,
            SENSOR_LOAD3_TURN_ON_DELAY,
            host,
            device_name,
            is_turn_on=True,
        ),
        # Turn off delays (in seconds)
        VElectricDelayNumber(
            coordinator,
            config_entry,
            SENSOR_LOAD1_TURN_OFF_DELAY,
            host,
            device_name,
            is_turn_on=False,
        ),
        VElectricDelayNumber(
            coordinator,
            config_entry,
            SENSOR_LOAD2_TURN_OFF_DELAY,
            host,
            device_name,
            is_turn_on=False,
        ),
        VElectricDelayNumber(
            coordinator,
            config_entry,
            SENSOR_LOAD3_TURN_OFF_DELAY,
            host,
            device_name,
            is_turn_on=False,
        ),
    ]

    async_add_entities(entities)


class VElectricBaseNumber(
    CoordinatorEntity[VElectricDataUpdateCoordinator], NumberEntity
):
    """Base class for VElectric Load Manager number entities."""

    def __init__(
        self,
        coordinator: VElectricDataUpdateCoordinator,
        config_entry: ConfigEntry,
        sensor_key: str,
        host: str,
        device_name: str,
    ) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)
        self._sensor_key = sensor_key
        self._host = host
        self._attr_unique_id = f"{config_entry.entry_id}_{sensor_key}"

        # Create friendly entity names that include device name for multi-device setups
        base_name = SENSOR_NAMES[sensor_key]
        if "VElectric Load Manager" not in device_name:
            self._attr_name = f"{device_name} {base_name}"
        else:
            self._attr_name = base_name

        self._attr_mode = NumberMode.BOX
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, config_entry.entry_id)},
            name=device_name,
            manufacturer=MANUFACTURER,
            model=MODEL,
            sw_version="1.0",
        )

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.last_update_success

    @property
    def native_value(self) -> float | None:
        """Return the current value."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._sensor_key)

    async def async_set_native_value(self, value: float) -> None:
        """Set the value."""
        await self._update_device_setting(value)

    async def _update_device_setting(self, value: float) -> None:
        """Update the device setting - to be implemented by subclasses."""
        raise NotImplementedError


class VElectricBreakerNumber(VElectricBaseNumber):
    """Number entity for breaker ratings."""

    def __init__(
        self,
        coordinator: VElectricDataUpdateCoordinator,
        config_entry: ConfigEntry,
        sensor_key: str,
        host: str,
        device_name: str,
    ) -> None:
        """Initialize the breaker number entity."""
        super().__init__(coordinator, config_entry, sensor_key, host, device_name)
        self._attr_native_min_value = 10
        self._attr_native_max_value = 200
        self._attr_native_step = 1
        self._attr_native_unit_of_measurement = "A"
        self._attr_icon = "mdi:current-ac"

    async def _update_device_setting(self, value: float) -> None:
        """Update the breaker setting on the device."""
        if not self.coordinator._client:
            return

        int_value = int(value)
        if self._sensor_key == SENSOR_MAIN_BREAKER:
            self.coordinator._client.update_settings(main_supply_breaker=int_value)
        else:
            # Determine load index from sensor key
            load_index = (
                int(self._sensor_key.replace("load", "").replace("_breaker", "")) - 1
            )
            self.coordinator._client.update_load_setting(
                load_index, "load_breaker", int_value
            )


class VElectricChannelsNumber(VElectricBaseNumber):
    """Number entity for active channels."""

    def __init__(
        self,
        coordinator: VElectricDataUpdateCoordinator,
        config_entry: ConfigEntry,
        sensor_key: str,
        host: str,
        device_name: str,
    ) -> None:
        """Initialize the channels number entity."""
        super().__init__(coordinator, config_entry, sensor_key, host, device_name)
        self._attr_native_min_value = 1
        self._attr_native_max_value = 3
        self._attr_native_step = 1
        self._attr_icon = "mdi:counter"

    async def _update_device_setting(self, value: float) -> None:
        """Update the active channels setting on the device."""
        if not self.coordinator._client:
            return
        self.coordinator._client.update_settings(active_ch=int(value))


class VElectricDelayNumber(VElectricBaseNumber):
    """Number entity for delay settings."""

    def __init__(
        self,
        coordinator: VElectricDataUpdateCoordinator,
        config_entry: ConfigEntry,
        sensor_key: str,
        host: str,
        device_name: str,
        is_turn_on: bool,
    ) -> None:
        """Initialize the delay number entity."""
        super().__init__(coordinator, config_entry, sensor_key, host, device_name)
        self._is_turn_on = is_turn_on

        if is_turn_on:
            # Turn on delays are in minutes
            self._attr_native_min_value = 1
            self._attr_native_max_value = 60
            self._attr_native_step = 1
            self._attr_native_unit_of_measurement = "min"
        else:
            # Turn off delays are in seconds
            self._attr_native_min_value = 1
            self._attr_native_max_value = 300
            self._attr_native_step = 1
            self._attr_native_unit_of_measurement = "s"

        self._attr_icon = "mdi:timer-cog-outline"

    async def _update_device_setting(self, value: float) -> None:
        """Update the delay setting on the device."""
        if not self.coordinator._client:
            return

        int_value = int(value)
        # Determine load index and setting name from sensor key
        if "load1" in self._sensor_key:
            load_index = 0
        elif "load2" in self._sensor_key:
            load_index = 1
        elif "load3" in self._sensor_key:
            load_index = 2
        else:
            return

        if self._is_turn_on:
            setting_name = "turn_on_delay"
        else:
            setting_name = "turn_off_delay"

        self.coordinator._client.update_load_setting(
            load_index, setting_name, int_value
        )
