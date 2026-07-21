"""Raylogic H82 switch platform (single relay channels)."""
from __future__ import annotations
import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, BR40_CODE_RE16, BR40_CODE_RE8
from .protocol import RaylogicDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    device: RaylogicDevice = hass.data[DOMAIN][entry.entry_id]
    if device.br40_code not in (BR40_CODE_RE16, BR40_CODE_RE8):
        return
    entities = [
        RaylogicSwitch(hass, entry, device, entity_num, state)
        for entity_num, state in device.channel_states.items()
        if state.get("mode") == "switch"
    ]
    if entities:
        _LOGGER.info("Setting up %d H82 switches on %s", len(entities), device.ip)
        async_add_entities(entities)


class RaylogicSwitch(SwitchEntity):
    _attr_has_entity_name = False
    _attr_entity_registry_enabled_default = True

    def __init__(self, hass, entry, device: RaylogicDevice, entity_num, initial_state):
        self._hass = hass
        self._entry = entry
        self._device = device
        self._entity_num = entity_num
        suffix = device.ip_suffix
        self._attr_unique_id = f"{device.mac}_{device.device_type}_switch{entity_num}"
        self._attr_name = f"{device.device_type}_{suffix}_switch{entity_num}"
        self._is_on = initial_state.get("on", False)

    @property
    def device_info(self):
        if self._device.br40_code == BR40_CODE_RE8:
            name = f"Raylogic DIN-RE8 ({self._device.ip})"
            model = "DIN-RE8-RS485 8ch Relay"
        else:
            name = f"Raylogic DIN-RE16 ({self._device.ip})"
            model = "DIN-RE16-RS485 16ch Relay"
        return DeviceInfo(
            identifiers={(DOMAIN, self._device.mac)},
            name=name,
            manufacturer="Raylogic",
            model=model,
            sw_version=self._device.fw_version,
        )

    @property
    def available(self): return self._device.is_connected
    @property
    def is_on(self): return self._is_on

    async def async_turn_on(self, **kwargs):
        await self._device.set_switch(self._entity_num, True)
        self._is_on = True; self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        await self._device.set_switch(self._entity_num, False)
        self._is_on = False; self.async_write_ha_state()

    async def async_added_to_hass(self):
        self.async_on_remove(
            self._hass.bus.async_listen(f"{DOMAIN}_state_update", self._on_update)
        )

    @callback
    def _on_update(self, event):
        d = event.data
        if d.get("entry_id") == self._entry.entry_id and d.get("channel") == self._entity_num:
            s = d.get("state", {})
            if "on" in s: self._is_on = bool(s["on"]); self.async_write_ha_state()
