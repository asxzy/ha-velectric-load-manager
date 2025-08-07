"""Binary sensor platform for VElectric Load Manager."""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
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
    SENSOR_LOAD1_STATUS,
    SENSOR_LOAD2_STATUS,
    SENSOR_LOAD3_STATUS,
    SENSOR_NAMES,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the VElectric Load Manager binary sensors."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    host = config_entry.data[CONF_HOST]
    device_name = config_entry.data.get(CONF_NAME, f"VElectric Load Manager ({host})")

    entities = [
        VElectricLoadBinarySensor(
            coordinator, config_entry, SENSOR_LOAD1_STATUS, host, device_name
        ),
        VElectricLoadBinarySensor(
            coordinator, config_entry, SENSOR_LOAD2_STATUS, host, device_name
        ),
        VElectricLoadBinarySensor(
            coordinator, config_entry, SENSOR_LOAD3_STATUS, host, device_name
        ),
    ]

    async_add_entities(entities)


class VElectricLoadBinarySensor(
    CoordinatorEntity[VElectricDataUpdateCoordinator], BinarySensorEntity
):
    """Binary sensor for load status (on/off)."""

    def __init__(
        self,
        coordinator: VElectricDataUpdateCoordinator,
        config_entry: ConfigEntry,
        sensor_key: str,
        host: str,
        device_name: str,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self._sensor_key = sensor_key
        self._host = host
        self._attr_unique_id = f"{config_entry.entry_id}_{sensor_key}"

        # Create friendly sensor names that include device name for multi-device setups
        base_name = SENSOR_NAMES[sensor_key]
        if "VElectric Load Manager" not in device_name:
            self._attr_name = f"{device_name} {base_name}"
        else:
            self._attr_name = base_name

        self._attr_device_class = BinarySensorDeviceClass.RUNNING
        self._attr_icon = "mdi:power-socket"

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
    def is_on(self) -> bool | None:
        """Return true if the load is on."""
        if self.coordinator.data is None:
            return None

        status = self.coordinator.data.get(self._sensor_key)
        if status is None:
            return None

        # Load is considered "on" if status is "on" or in a wait state
        return status in ["on", "wait-on", "wait-off"]

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Return the state attributes."""
        if self.coordinator.data is None:
            return None

        status = self.coordinator.data.get(self._sensor_key)
        if status is None:
            return None

        attributes = {"status": status}

        # Add remaining time if in a wait state
        load_num = self._sensor_key.replace("_status", "_remaining_time")
        remaining_time = self.coordinator.data.get(load_num)
        if remaining_time is not None and remaining_time > 0:
            attributes["remaining_time_seconds"] = remaining_time

        return attributes
