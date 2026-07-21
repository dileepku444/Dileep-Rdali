"""Raylogic H82 cover (curtain) platform."""
from __future__ import annotations
import logging

from homeassistant.components.cover import CoverDeviceClass, CoverEntity, CoverEntityFeature
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
        RaylogicCover(hass, entry, device, pair_num, state)
        for pair_num, state in device.channel_states.items()
        if state.get("mode") in ("curtain", "blind")
    ]
    if entities:
        _LOGGER.info("Setting up %d H82 curtains on %s", len(entities), device.ip)
        async_add_entities(entities)


class RaylogicCover(CoverEntity):
    _attr_device_class = CoverDeviceClass.CURTAIN
    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
    )
    _attr_has_entity_name = False
    _attr_entity_registry_enabled_default = True

    def __init__(self, hass, entry, device: RaylogicDevice, pair_num, initial_state):
        self._hass = hass
        self._entry = entry
        self._device = device
        self._pair_num = pair_num
        suffix = device.ip_suffix
        self._attr_unique_id = f"{device.mac}_{device.device_type}_curtain{pair_num}"
        self._attr_name = f"{device.device_type}_{suffix}_curtain{pair_num}"
        self._is_closed = None
        self._is_opening = False
        self._is_closing = False

    @property
    def device_info(self) -> DeviceInfo:
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
    def is_closed(self): return self._is_closed
    @property
    def is_opening(self): return self._is_opening
    @property
    def is_closing(self): return self._is_closing

    async def async_open_cover(self, **kwargs):
        await self._device.set_cover(self._pair_num, "open")
        self._is_opening = True
        self._is_closing = False
        self._is_closed = False
        self.async_write_ha_state()

    async def async_close_cover(self, **kwargs):
        await self._device.set_cover(self._pair_num, "close")
        self._is_closing = True
        self._is_opening = False
        self._is_closed = True
        self.async_write_ha_state()

    async def async_stop_cover(self, **kwargs):
        await self._device.set_cover(self._pair_num, "stop")
        self._is_opening = False
        self._is_closing = False
        self._is_closed = None
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
        if d.get("entry_id") == self._entry.entry_id and d.get("channel") == self._pair_num:
            action = d.get("state", {}).get("action")
            if action == "open":
                self._is_opening = True; self._is_closing = False; self._is_closed = False
            elif action == "close":
                self._is_closing = True; self._is_opening = False; self._is_closed = True
            elif action == "stop":
                self._is_opening = False; self._is_closing = False; self._is_closed = None
            self.async_write_ha_state()

    @callback
    def _on_available(self, event):
        if event.data.get("entry_id") == self._entry.entry_id:
            self.async_write_ha_state()
