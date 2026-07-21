"""Raylogic Home Automation integration."""
from __future__ import annotations
import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from .const import DEFAULT_PORT, DOMAIN, PLATFORMS
from .protocol import RaylogicDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    host = entry.data[CONF_HOST]
    port = entry.data.get(CONF_PORT, DEFAULT_PORT)

    device = RaylogicDevice(
        ip=host, port=port,
        state_callback=lambda ip, ch, state: _handle_state_update(
            hass, entry.entry_id, ip, ch, state
        ),
    )

    await asyncio.sleep(2.0)
    connected = await device.connect()
    if not connected:
        _LOGGER.error("Could not connect to Raylogic device at %s", host)
        return False

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = device

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    device: RaylogicDevice = hass.data[DOMAIN].get(entry.entry_id)
    if device:
        await device.disconnect()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


def _handle_state_update(hass, entry_id, ip, ch, state):
    if "available" in state:
        hass.bus.async_fire(
            f"{DOMAIN}_available",
            {"entry_id": entry_id, "available": state["available"]},
        )
        return
    hass.bus.async_fire(
        f"{DOMAIN}_state_update",
        {"entry_id": entry_id, "ip": ip, "channel": ch, "state": state},
    )
    hass.bus.async_fire(
        f"{DOMAIN}_available",
        {"entry_id": entry_id, "available": True},
    )
