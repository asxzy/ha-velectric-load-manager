"""Constants for VElectric Load Manager integration."""

from typing import Final

DOMAIN: Final = "velectric_load_manager"

# Configuration keys
CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_NAME: Final = "name"
CONF_VOLTAGE: Final = "voltage"

# Default values
DEFAULT_PORT: Final = 80
DEFAULT_SCAN_INTERVAL: Final = 5  # seconds (reduced from 2s to be less aggressive)
DEFAULT_VOLTAGE: Final = 240  # Standard household voltage

# Device info
MANUFACTURER: Final = "VElectric"
MODEL: Final = "Load Manager"

# Websocket protocol
WS_REQUEST_BYTE: Final = 103  # 'g' command for readings
PACKET_SIZE: Final = 14  # Expected packet size in bytes
PING_INTERVAL: Final = 2.0  # Seconds between pings

# Sensor keys
SENSOR_CT1_CURRENT: Final = "ct1_current"
SENSOR_CT2_CURRENT: Final = "ct2_current"
SENSOR_CT1_POWER: Final = "ct1_power"
SENSOR_CT2_POWER: Final = "ct2_power"
SENSOR_CT1_ENERGY: Final = "ct1_energy"
SENSOR_CT2_ENERGY: Final = "ct2_energy"
SENSOR_CONNECTION_STATUS: Final = "connection_status"

# Sensor names
SENSOR_NAMES = {
    SENSOR_CT1_CURRENT: "CT1 Current",
    SENSOR_CT2_CURRENT: "CT2 Current",
    SENSOR_CT1_POWER: "CT1 Power",
    SENSOR_CT2_POWER: "CT2 Power",
    SENSOR_CT1_ENERGY: "CT1 Energy",
    SENSOR_CT2_ENERGY: "CT2 Energy",
    SENSOR_CONNECTION_STATUS: "Connection Status",
}
