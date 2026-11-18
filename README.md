# VElectric Load Manager Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://github.com/custom-components/hacs)
[![GitHub release](https://img.shields.io/github/release/asxzy/ha-velectric-load-manager.svg?style=for-the-badge)](https://github.com/asxzy/ha-velectric-load-manager/releases)
[![GitHub license](https://img.shields.io/github/license/asxzy/ha-velectric-load-manager.svg?style=for-the-badge)](LICENSE)

Home Assistant custom integration for VElectric Load Manager devices. Monitor current readings from CT1 and CT2 sensors via websocket connection.

## Features

- **Real-time Current Monitoring**: CT1 and CT2 current sensors with amperage readings
- **Connection Status**: Monitor device connectivity 
- **WebSocket Communication**: Reliable async websocket connection with automatic reconnection
- **Easy Configuration**: Simple setup through Home Assistant UI
- **Robust Error Handling**: Comprehensive error handling and connection recovery

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Go to "Integrations"
3. Click the three dots menu and select "Custom repositories"
4. Add this repository URL: `https://github.com/asxzy/ha-velectric-load-manager`
5. Select category "Integration"
6. Click "Add"
7. Find "VElectric Load Manager" in HACS and install
8. Restart Home Assistant

### Manual Installation

1. Download the latest release from [Releases](https://github.com/asxzy/ha-velectric-load-manager/releases)
2. Extract the files
3. Copy the `custom_components/velectric_load_manager` folder to your Home Assistant `custom_components` directory
4. Restart Home Assistant

## Configuration

1. Go to **Settings** → **Devices & Services**
2. Click **Add Integration**
3. Search for "VElectric Load Manager"
4. Enter your device's IP address and port (default: 80)
5. Click **Submit**

The integration will test the connection and create the following entities:
- `sensor.velectric_ct1_current` - CT1 current reading (A)
- `sensor.velectric_ct2_current` - CT2 current reading (A) 
- `sensor.velectric_connection_status` - Connection status

## Device Requirements

- VElectric Load Manager device with websocket support
- Device accessible on local network
- WebSocket endpoint at `ws://[device-ip]:[port]/ws`

## Device load

The device is an ESP with a very small TCP pool, and its HTTP service wedges if
it is contacted too often. The integration is designed around that:

- **One connection, held open.** No reconnect timers, no polling by the
  coordinator.
- **No pings.** The WebSocket keepalive is disabled; it was a packet per cycle
  for no benefit.
- **No HTTP at all.** The web UI is never touched.

Known firmware behaves in one of two ways, and the integration detects which:

| Firmware | Behaviour | Bytes the integration sends |
|---|---|---|
| Push | sends a readings frame ~1/s on its own | **2**, once, at connect |
| Request/response | silent until asked | 2 at connect, then 1 per 8 s poll |

The push/request split is decided from actual traffic, not a timeout: a readings
frame that arrives while no request is outstanding proves the device pushes, and
polling then stops permanently. A device that only ever answers is polled at
`PULL_INTERVAL` (8 s), which is 4x slower than the old 2 s request loop and
applies only to devices that need it.

- **Reconnects use exponential backoff** starting at 15 s, because the device
  typically needs tens of seconds to recover. A session that delivered no data
  does not reset the backoff.

`docs/validating-esp-retry-impact.md` documents the measurements behind these
choices and the tools under `tools/` used to reproduce them.

## Troubleshooting

### Connection Issues
- Verify device IP address and port
- Check network connectivity to device
- Ensure device websocket endpoint is accessible
- Check Home Assistant logs for detailed error messages

### The device's web UI stopped responding
Expected on this hardware: the HTTP task can wedge while `/ws` keeps working.
Power-cycle the device. The integration itself does not cause this, because it
never issues HTTP requests; see `docs/validating-esp-retry-impact.md`.

### Enable Debug Logging

Add to `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.velectric_load_manager: debug
```

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Support

- [GitHub Issues](https://github.com/asxzy/ha-velectric-load-manager/issues)
- [Home Assistant Community](https://community.home-assistant.io/)

## Changelog

### v1.2.0
- Hold a single websocket connection; no pings and no coordinator polling
- Detect push vs request/response firmware from real traffic; push devices are
  never asked again, request/response devices are polled every 8 s
- Connection is designed for the ESP's small TCP pool: exponential backoff
  starting at 15 s, no HTTP requests, no compression
- Coalesce pushed frames to a 5 s publish interval
- Remove the `scan_interval` option: it never affected device traffic
- Pass `config_entry` to the coordinator (required from HA 2026.8)
- Shut the client down when setup fails, so retries cannot orphan connections

### v1.0.0
- Initial release
- CT1/CT2 current sensors
- WebSocket communication
- Connection status monitoring