"""Sensor platform for VElectric Load Manager."""

from __future__ import annotations

import logging
import time
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent, UnitOfPower, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import VElectricDataUpdateCoordinator
from .const import (
    CONF_HOST,
    CONF_NAME,
    CONF_VOLTAGE,
    DEFAULT_VOLTAGE,
    DOMAIN,
    MANUFACTURER,
    MODEL,
    SENSOR_CT1_CURRENT,
    SENSOR_CT2_CURRENT,
    SENSOR_CT1_POWER,
    SENSOR_CT2_POWER,
    SENSOR_CT1_ENERGY,
    SENSOR_CT2_ENERGY,
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
    device_name = config_entry.data.get(CONF_NAME, f"VElectric Load Manager ({host})")
    voltage = config_entry.options.get(CONF_VOLTAGE, DEFAULT_VOLTAGE)

    entities = [
        VElectricCurrentSensor(
            coordinator, config_entry, SENSOR_CT1_CURRENT, host, device_name
        ),
        VElectricCurrentSensor(
            coordinator, config_entry, SENSOR_CT2_CURRENT, host, device_name
        ),
        VElectricPowerSensor(
            coordinator, config_entry, SENSOR_CT1_POWER, SENSOR_CT1_CURRENT, host, device_name, voltage
        ),
        VElectricPowerSensor(
            coordinator, config_entry, SENSOR_CT2_POWER, SENSOR_CT2_CURRENT, host, device_name, voltage
        ),
        VElectricEnergySensor(
            coordinator, config_entry, SENSOR_CT1_ENERGY, SENSOR_CT1_POWER, host, device_name
        ),
        VElectricEnergySensor(
            coordinator, config_entry, SENSOR_CT2_ENERGY, SENSOR_CT2_POWER, host, device_name
        ),
        VElectricConnectionSensor(
            coordinator, config_entry, SENSOR_CONNECTION_STATUS, host, device_name
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
        device_name: str,
    ) -> None:
        """Initialize the sensor."""
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


class VElectricCurrentSensor(VElectricBaseSensor):
    """Sensor for current measurements."""

    def __init__(
        self,
        coordinator: VElectricDataUpdateCoordinator,
        config_entry: ConfigEntry,
        sensor_key: str,
        host: str,
        device_name: str,
    ) -> None:
        """Initialize the current sensor."""
        super().__init__(coordinator, config_entry, sensor_key, host, device_name)
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


class VElectricPowerSensor(VElectricBaseSensor):
    """Sensor for power calculations (Current * Voltage)."""

    def __init__(
        self,
        coordinator: VElectricDataUpdateCoordinator,
        config_entry: ConfigEntry,
        sensor_key: str,
        current_sensor_key: str,
        host: str,
        device_name: str,
        voltage: float,
    ) -> None:
        """Initialize the power sensor."""
        super().__init__(coordinator, config_entry, sensor_key, host, device_name)
        self._current_sensor_key = current_sensor_key
        self._voltage = voltage
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = UnitOfPower.WATT
        self._attr_suggested_display_precision = 1

    @property
    def native_value(self) -> float | None:
        """Return the calculated power (Current * Voltage)."""
        if self.coordinator.data is None:
            return None
        current = self.coordinator.data.get(self._current_sensor_key, 0.0)
        if current is None:
            return None
        return current * self._voltage


class VElectricEnergySensor(VElectricBaseSensor):
    """Sensor for energy calculations using Riemann sum integration."""

    def __init__(
        self,
        coordinator: VElectricDataUpdateCoordinator,
        config_entry: ConfigEntry,
        sensor_key: str,
        power_sensor_key: str,
        host: str,
        device_name: str,
    ) -> None:
        """Initialize the energy sensor."""
        super().__init__(coordinator, config_entry, sensor_key, host, device_name)
        self._power_sensor_key = power_sensor_key
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_suggested_display_precision = 3
        
        # Initialize state tracking for Riemann sum integration
        self._last_update_time = None
        self._last_power_value = None
        self._energy_total = 0.0

    @property
    def native_value(self) -> float | None:
        """Return the accumulated energy using Riemann sum integration."""
        if self.coordinator.data is None:
            return self._energy_total
        
        # Get current power value (calculated from current sensor)
        current_power = self._get_power_value()
        if current_power is None:
            return self._energy_total
            
        current_time = time.time()
        
        # If this is not the first reading and we have a valid previous reading
        if (self._last_update_time is not None and 
            self._last_power_value is not None and 
            current_time > self._last_update_time):
            
            # Calculate time delta in hours
            time_delta_hours = (current_time - self._last_update_time) / 3600.0
            
            # Use trapezoidal rule for integration: (P1 + P2) / 2 * dt
            avg_power = (self._last_power_value + current_power) / 2.0
            energy_delta_kwh = (avg_power * time_delta_hours) / 1000.0  # Convert W*h to kWh
            
            self._energy_total += energy_delta_kwh
            
        # Update state for next calculation
        self._last_update_time = current_time
        self._last_power_value = current_power
        
        return self._energy_total

    def _get_power_value(self) -> float | None:
        """Get the power value from coordinator data."""
        if self.coordinator.data is None:
            return None
        
        # Get current value and calculate power
        current_key = self._power_sensor_key.replace('_power', '_current')
        current = self.coordinator.data.get(current_key, 0.0)
        if current is None:
            return None
            
        # Get voltage from config (fallback to default)
        voltage = self.coordinator.config_entry.options.get(CONF_VOLTAGE, DEFAULT_VOLTAGE)
        return current * voltage


class VElectricConnectionSensor(VElectricBaseSensor):
    """Sensor for connection status."""

    def __init__(
        self,
        coordinator: VElectricDataUpdateCoordinator,
        config_entry: ConfigEntry,
        sensor_key: str,
        host: str,
        device_name: str,
    ) -> None:
        """Initialize the connection sensor."""
        super().__init__(coordinator, config_entry, sensor_key, host, device_name)
        self._attr_icon = "mdi:connection"

    @property
    def native_value(self) -> str | None:
        """Return the state of the sensor."""
        if not self.coordinator.last_update_success:
            return "Connection Failed"
        if self.coordinator.data is None:
            return "No Data"
        return self.coordinator.data.get(self._sensor_key, "Unknown")
