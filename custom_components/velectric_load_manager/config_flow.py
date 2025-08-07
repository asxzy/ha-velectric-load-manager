"""Config flow for VElectric Load Manager integration."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from typing import Any

import voluptuous as vol
import websockets
from websockets.exceptions import ConnectionClosedError, InvalidURI

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_HOST, CONF_PORT, CONF_NAME, DEFAULT_PORT, DOMAIN


def validate_hostname(hostname: str) -> str:
    """Validate hostname or IP address."""
    hostname = hostname.strip().lower()

    # Check for invalid characters
    if any(char in hostname for char in ["<", ">", '"', "'"]):
        raise vol.Invalid("Invalid characters in hostname")

    try:
        # Try to parse as IP address
        ipaddress.ip_address(hostname)
        return hostname
    except ValueError:
        # Check if it's a valid hostname
        if len(hostname) > 253:
            raise vol.Invalid("Hostname too long")

        hostname = hostname.rstrip(".")
        allowed = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?$")
        if not all(allowed.match(x) for x in hostname.split(".")):
            raise vol.Invalid("Invalid hostname format")

        return hostname


_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): validate_hostname,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): vol.All(
            int, vol.Range(min=1, max=65535)
        ),
        vol.Optional(CONF_NAME): str,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    host = data[CONF_HOST]
    port = data[CONF_PORT]

    # Test websocket connection
    ws_url = f"ws://{host}:{port}/ws"
    try:
        async with asyncio.timeout(10):
            async with websockets.connect(ws_url) as ws:
                # Send a test request
                await ws.send(bytes([103]))
                # Wait for a response
                await asyncio.wait_for(ws.recv(), timeout=5.0)
    except asyncio.TimeoutError as err:
        _LOGGER.error("Connection to VElectric device timed out")
        raise CannotConnect("Connection timeout") from err
    except InvalidURI as err:
        _LOGGER.error("Invalid WebSocket URI: %s", ws_url)
        raise CannotConnect("Invalid device address") from err
    except ConnectionClosedError as err:
        _LOGGER.error("Connection closed by VElectric device")
        raise CannotConnect("Device rejected connection") from err
    except Exception as err:
        _LOGGER.error("Unexpected error connecting to VElectric device: %s", err)
        raise CannotConnect("Connection failed") from err

    # Return info to store in the config entry
    device_name = data.get(CONF_NAME, f"VElectric Load Manager ({host})")
    return {"title": device_name}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for VElectric Load Manager."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(title=info["title"], data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return OptionsFlow(config_entry)


class OptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for VElectric Load Manager."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Update the config entry with new options
            return self.async_create_entry(
                title="",
                data={
                    **self.config_entry.data,
                    CONF_NAME: user_input.get(
                        CONF_NAME, self.config_entry.data.get(CONF_NAME)
                    ),
                },
            )

        # Pre-fill current values
        current_name = self.config_entry.data.get(
            CONF_NAME, f"VElectric Load Manager ({self.config_entry.data[CONF_HOST]})"
        )

        options_schema = vol.Schema(
            {
                vol.Optional(CONF_NAME, default=current_name): str,
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=options_schema,
            errors=errors,
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""
