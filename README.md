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

1. Go to **Settings** ’ **Devices & Services**
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
- Device responds to byte command `103` with 14-byte current data

## Troubleshooting

### Connection Issues
- Verify device IP address and port
- Check network connectivity to device
- Ensure device websocket endpoint is accessible
- Check Home Assistant logs for detailed error messages

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

### v1.0.0
- Initial release
- CT1/CT2 current sensors
- WebSocket communication
- Connection status monitoring