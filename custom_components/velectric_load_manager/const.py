"""Constants for VElectric Load Manager integration."""

from typing import Final

DOMAIN: Final = "velectric_load_manager"

# Configuration keys
CONF_HOST: Final = "host"
CONF_PORT: Final = "port"

# Default values
DEFAULT_PORT: Final = 80
DEFAULT_SCAN_INTERVAL: Final = 5  # seconds (reduced from 2s to be less aggressive)

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
SENSOR_CONNECTION_STATUS: Final = "connection_status"

# Sensor names
SENSOR_NAMES = {
    SENSOR_CT1_CURRENT: "CT1 Current",
    SENSOR_CT2_CURRENT: "CT2 Current",
    SENSOR_CONNECTION_STATUS: "Connection Status",
}
