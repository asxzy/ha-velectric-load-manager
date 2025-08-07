"""Config flow for VElectric Load Manager integration."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
import websockets

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_HOST, CONF_PORT, DEFAULT_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
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
    except Exception as err:
        _LOGGER.error("Failed to connect to VElectric device: %s", err)
        raise CannotConnect from err
    
    # Return info to store in the config entry
    return {"title": f"VElectric Load Manager ({host})"}


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


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""