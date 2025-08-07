# VElectric Load Manager Home Assistant Integration Plan

## Project Overview
Custom Home Assistant integration to read VElectric load manager data via websocket and expose current readings as sensors.

## Current State
- Basic websocket client implemented in `sample.py`
- Connects to VElectric device at ws://192.168.1.112/ws
- Decodes 14-byte packets containing CT1 and CT2 current readings
- Sends periodic requests (byte 103) every 2 seconds

## Integration Architecture

### 1. Home Assistant Custom Integration Structure
```
custom_components/velectric_load_manager/
├── __init__.py          # Integration setup and coordinator
├── manifest.json        # Integration metadata
├── config_flow.py       # Configuration UI
├── const.py            # Constants and defaults
├── sensor.py           # Sensor entities
└── websocket_client.py  # Websocket communication logic
```

### 2. Core Components

#### A. Integration Coordinator (`__init__.py`)
- Manage websocket connection lifecycle
- Handle reconnection logic with exponential backoff
- Coordinate data updates between websocket and sensors
- Implement proper shutdown/cleanup

#### B. Websocket Client (`websocket_client.py`)
- Async websocket connection management
- Decode VElectric protocol (14-byte packets)
- Handle connection errors and automatic reconnection
- Emit data updates to coordinator

#### C. Sensor Entities (`sensor.py`)
- CT1 current sensor (Amperes)
- CT2 current sensor (Amperes)
- Connection status sensor
- Optional: Power calculation sensors (if voltage known)

#### D. Configuration Flow (`config_flow.py`)
- Device IP address input
- Websocket port configuration (default: 80)
- Connection testing during setup
- Options for polling interval

### 3. Implementation Plan

#### Phase 1: Basic Integration Structure
1. Create manifest.json with proper dependencies
2. Implement basic coordinator class
3. Set up configuration flow for device IP
4. Create basic sensor entities

#### Phase 2: Websocket Communication
1. Extract websocket logic from sample.py
2. Implement async websocket client class
3. Add proper error handling and logging
4. Implement reconnection logic

#### Phase 3: Sensor Implementation
1. Create CT1 and CT2 current sensors
2. Add device class and state class attributes
3. Implement proper units of measurement
4. Add connection status indication

#### Phase 4: Configuration & Polish
1. Add configuration options (polling interval, etc.)
2. Implement proper device info
3. Add comprehensive error handling
4. Create documentation and setup instructions

### 4. Technical Details

#### Websocket Protocol
- Connect to `ws://{device_ip}/ws`
- Send byte `103` every 2 seconds for readings
- Receive 14-byte responses with CT data
- Decode using: `struct.unpack_from("<HH", packet, 0)`
- Calculate current: `math.sqrt(raw_value)`

#### Sensor Configuration
```python
SENSORS = {
    "ct1_current": {
        "name": "CT1 Current",
        "unit": "A",
        "device_class": "current",
        "state_class": "measurement",
    },
    "ct2_current": {
        "name": "CT2 Current", 
        "unit": "A",
        "device_class": "current",
        "state_class": "measurement",
    }
}
```

#### Error Handling Strategy
- Connection failures: Exponential backoff reconnection
- Invalid data: Log and skip update
- Device offline: Mark sensors as unavailable
- Configuration errors: Proper user feedback

### 5. Dependencies
- `websockets` library for async websocket communication
- Home Assistant core libraries
- Standard Python libraries (asyncio, struct, math)

### 6. Configuration Schema
```yaml
# configuration.yaml example
velectric_load_manager:
  host: "192.168.1.112"
  port: 80
  scan_interval: 2  # seconds
```

### 7. Testing Strategy
- Unit tests for websocket client
- Mock websocket server for testing
- Integration tests with actual device
- Configuration flow testing

### 8. Future Enhancements
- Support for multiple devices
- Historical data logging
- Power calculation (if voltage available)
- Configurable CT names/labels
- MQTT integration option
- Device discovery via mDNS/SSDP

## Implementation Priority
1. Basic integration structure and configuration
2. Websocket client implementation
3. Sensor entities with proper attributes
4. Error handling and reconnection logic
5. Documentation and testing

## Files to Create/Modify
- `custom_components/velectric_load_manager/` (new directory structure)
- All integration files as outlined above
- Keep `sample.py` as reference implementation