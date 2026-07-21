"""Raylogic H81 dimmer platform."""
from __future__ import annotations
import logging
from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, BR40_CODE_H81
from .protocol import RaylogicDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    device: RaylogicDevice = hass.data[DOMAIN][entry.entry_id]
    if device.br40_code != BR40_CODE_H81:
        return
    entities = [
        RaylogicLight(hass, entry, device, ch_num, state)
        for ch_num, state in device.channel_states.items()
    ]
    if entities:
        _LOGGER.info("Setting up %d H81 channels on %s", len(entities), device.ip)
        async_add_entities(entities)


class RaylogicLight(LightEntity):
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_has_entity_name = False
    _attr_entity_registry_enabled_default = True

    def __init__(self, hass, entry, device: RaylogicDevice, ch_num, initial_state):
        self._hass = hass
        self._entry = entry
        self._device = device
        self._ch_num = ch_num
        suffix = device.ip_suffix
        self._attr_unique_id = f"{device.mac}_h81_ch{ch_num}"
        self._attr_name = f"h81_{suffix}_ch{ch_num}"
        raw = initial_state.get("level", 0xFF)
        self._brightness = self._raw_to_ha(raw)
        self._last_brightness = self._brightness if self._brightness > 1 else 255
        self._is_on = raw < 0xFF

    def _raw_to_ha(self, raw: int) -> int:
        if raw >= 0xFF:
            return 0
        return max(1, min(255, 256 - raw))

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device.mac)},
            name=f"Raylogic DIN-H81 ({self._device.ip})",
            manufacturer="Raylogic",
            model="DIN-H81-RS485 8ch Triac Dimmer",
            sw_version=self._device.fw_version,
        )

    @property
    def available(self): return self._device.is_connected
    @property
    def is_on(self): return self._is_on
    @property
    def brightness(self): return self._brightness

    async def async_turn_on(self, **kwargs: Any):
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if brightness is None:
            brightness = self._last_brightness if self._last_brightness > 1 else 255
        brightness = max(1, min(255, int(brightness)))
        await self._device.set_light(self._ch_num, brightness, on=True)
        self._last_brightness = brightness
        self._brightness = brightness
        self._is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any):
        if self._brightness > 1:
            self._last_brightness = self._brightness
        await self._device.set_light(self._ch_num, self._brightness, on=False)
        self._is_on = False
        self.async_write_ha_state()

    async def async_added_to_hass(self):
        self.async_on_remove(
            self._hass.bus.async_listen(f"{DOMAIN}_state_update", self._on_update)
        )
        self.async_on_remove(
            self._hass.bus.async_listen(f"{DOMAIN}_available", self._on_available)
        )

    @callback
    def _on_update(self, event):
        d = event.data
        if d.get("entry_id") == self._entry.entry_id and d.get("channel") == self._ch_num:
            s = d.get("state", {})
            if "level" in s:
                raw = s["level"]
                self._brightness = self._raw_to_ha(raw)
                self._is_on = raw < 0xFF
            elif "on" in s:
                self._is_on = bool(s["on"])
            self.async_write_ha_state()

    @callback
    def _on_available(self, event):
        if event.data.get("entry_id") == self._entry.entry_id:
            self.async_write_ha_state()
