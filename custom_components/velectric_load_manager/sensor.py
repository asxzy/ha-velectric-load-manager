"""Sensor platform for VElectric Load Manager."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import VElectricDataUpdateCoordinator
from .const import (
    CONF_HOST,
    DOMAIN,
    MANUFACTURER,
    MODEL,
    SENSOR_CT1_CURRENT,
    SENSOR_CT2_CURRENT,
    SENSOR_CONNECTION_STATUS,
    SENSOR_NAMES,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the VElectric Load Manager sensors."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    host = config_entry.data[CONF_HOST]

    entities = [
        VElectricCurrentSensor(coordinator, config_entry, SENSOR_CT1_CURRENT, host),
        VElectricCurrentSensor(coordinator, config_entry, SENSOR_CT2_CURRENT, host),
        VElectricConnectionSensor(
            coordinator, config_entry, SENSOR_CONNECTION_STATUS, host
        ),
    ]

    async_add_entities(entities)


class VElectricBaseSensor(
    CoordinatorEntity[VElectricDataUpdateCoordinator], SensorEntity
):
    """Base class for VElectric Load Manager sensors."""

    def __init__(
        self,
        coordinator: VElectricDataUpdateCoordinator,
        config_entry: ConfigEntry,
        sensor_key: str,
        host: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._sensor_key = sensor_key
        self._host = host
        self._attr_name = SENSOR_NAMES[sensor_key]
        self._attr_unique_id = f"{config_entry.entry_id}_{sensor_key}"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, config_entry.entry_id)},
            name=f"VElectric Load Manager ({host})",
            manufacturer=MANUFACTURER,
            model=MODEL,
            sw_version="1.0",
        )

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.last_update_success


class VElectricCurrentSensor(VElectricBaseSensor):
    """Sensor for current measurements."""

    def __init__(
        self,
        coordinator: VElectricDataUpdateCoordinator,
        config_entry: ConfigEntry,
        sensor_key: str,
        host: str,
    ) -> None:
        """Initialize the current sensor."""
        super().__init__(coordinator, config_entry, sensor_key, host)
        self._attr_device_class = SensorDeviceClass.CURRENT
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
        self._attr_suggested_display_precision = 1

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._sensor_key, 0.0)


class VElectricConnectionSensor(VElectricBaseSensor):
    """Sensor for connection status."""

    def __init__(
        self,
        coordinator: VElectricDataUpdateCoordinator,
        config_entry: ConfigEntry,
        sensor_key: str,
        host: str,
    ) -> None:
        """Initialize the connection sensor."""
        super().__init__(coordinator, config_entry, sensor_key, host)
        self._attr_icon = "mdi:connection"

    @property
    def native_value(self) -> str | None:
        """Return the state of the sensor."""
        if self.coordinator.data is None:
            return "Disconnected"
        return self.coordinator.data.get(self._sensor_key, "Disconnected")
