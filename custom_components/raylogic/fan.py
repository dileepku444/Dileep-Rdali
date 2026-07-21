"""Raylogic FN4 fan platform."""
from __future__ import annotations
import logging
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, BR40_CODE_FN4
from .protocol import RaylogicDevice

_LOGGER = logging.getLogger(__name__)
VALID_SPEEDS = [25, 50, 75, 100]

# TURN_ON/TURN_OFF were added to FanEntityFeature in HA 2024.8.
# getattr keeps this working (no crash) on older HA cores too.
_FEATURE_TURN_ON = getattr(FanEntityFeature, "TURN_ON", 0)
_FEATURE_TURN_OFF = getattr(FanEntityFeature, "TURN_OFF", 0)


async def async_setup_entry(hass, entry, async_add_entities):
    device: RaylogicDevice = hass.data[DOMAIN][entry.entry_id]
    if device.br40_code != BR40_CODE_FN4:
        return
    entities = [
        RaylogicFan(hass, entry, device, ch_num, state)
        for ch_num, state in device.channel_states.items()
    ]
    if entities:
        _LOGGER.info("Setting up %d FN4 fans on %s", len(entities), device.ip)
        async_add_entities(entities)


class RaylogicFan(FanEntity):
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | _FEATURE_TURN_ON
        | _FEATURE_TURN_OFF
    )
    _attr_has_entity_name = False
    _attr_entity_registry_enabled_default = True

    def __init__(self, hass, entry, device: RaylogicDevice, ch_num, initial_state):
        self._hass = hass
        self._entry = entry
        self._device = device
        self._ch_num = ch_num
        suffix = device.ip_suffix
        self._attr_unique_id = f"{device.mac}_fn4_fan{ch_num}"
        self._attr_name = f"fn4_{suffix}_fan{ch_num}"
        self._speed_pct = initial_state.get("speed", 0)
        self._is_on = initial_state.get("on", False)

    @property
    def device_info(self):
        return DeviceInfo(
            identifiers={(DOMAIN, self._device.mac)},
            name=f"Raylogic DIN-FN4 ({self._device.ip})",
            manufacturer="Raylogic",
            model="DIN-FN4-RS485 4ch Fan Dimmer",
            sw_version=self._device.fw_version,
        )

    @property
    def available(self): return self._device.is_connected
    @property
    def is_on(self): return self._is_on
    @property
    def percentage(self): return self._speed_pct if self._is_on else 0
    @property
    def speed_count(self): return len(VALID_SPEEDS)

    def _snap(self, pct): return min(VALID_SPEEDS, key=lambda x: abs(x - pct))

    async def async_turn_on(self, percentage=None, preset_mode=None, **kwargs: Any):
        speed = self._snap(percentage if percentage else 25)
        await self._device.set_fan(self._ch_num, speed)
        self._speed_pct = speed; self._is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any):
        await self._device.set_fan(self._ch_num, 0)
        self._is_on = False; self._speed_pct = 0
        self.async_write_ha_state()

    async def async_set_percentage(self, percentage: int):
        if percentage == 0:
            await self.async_turn_off(); return
        speed = self._snap(percentage)
        await self._device.set_fan(self._ch_num, speed)
        self._speed_pct = speed; self._is_on = True
        self.async_write_ha_state()

    async def async_added_to_hass(self):
        self.async_on_remove(
            self._hass.bus.async_listen(f"{DOMAIN}_state_update", self._on_update)
        )

    @callback
    def _on_update(self, event):
        d = event.data
        if d.get("entry_id") == self._entry.entry_id and d.get("channel") == self._ch_num:
            s = d.get("state", {})
            if "speed" in s: self._speed_pct = s["speed"]
            if "on" in s: self._is_on = s["on"]
            self.async_write_ha_state()
