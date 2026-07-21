"""Config flow for Raylogic integration."""
from __future__ import annotations
import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.data_entry_flow import FlowResult

from .const import DEFAULT_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema({
    vol.Required(CONF_HOST): str,
    vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
})


async def validate_connection(hass, host: str, port: int) -> dict:
    info = {"mac": None, "node": None}
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=5.0,
        )
    except Exception as exc:
        raise ConnectionError(f"Cannot connect to {host}:{port}") from exc

    try:
        data = await asyncio.wait_for(reader.readuntil(b'\r'), timeout=5.0)
        line = data.decode(errors="replace").strip()
        if "*KA=" in line:
            info["node"] = line.split(",")[0].strip()
            ka_part = line.split("*KA=")[1][:8]
            info["mac"] = f"{host.replace('.', '_')}_{ka_part}"
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    await asyncio.sleep(2.0)
    return info


class RaylogicConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input.get(CONF_PORT, DEFAULT_PORT)
            try:
                info = await validate_connection(self.hass, host, port)
            except ConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error connecting to %s", host)
                errors["base"] = "unknown"
            else:
                unique_id = info.get("mac") or host
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Raylogic {host}",
                    data={CONF_HOST: host, CONF_PORT: port, "node": info.get("node")},
                )
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors,
        )
