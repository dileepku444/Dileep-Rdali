"""Raylogic DIN-DALI-64 status sensors (status-read only, no control yet).

This platform only creates entities for devices identified as DALI
(device.is_dali). It never touches H81/RE16/RE8/FN4 devices — those
keep using light.py/cover.py/fan.py/switch.py exactly as before.
"""
from __future__ import annotations
import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN
from .protocol import RaylogicDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    device: RaylogicDevice = hass.data[DOMAIN][entry.entry_id]

    entities = []

    def _add_for_key(key, state):
        if any(getattr(e, "_dali_key", None) == key for e in entities):
            return
        new_entities = [DaliBrightnessSensor(hass, entry, device, key, state)]
        if state.get("kind") == "cct":
            new_entities.append(DaliColorTempSensor(hass, entry, device, key, state))
        entities.extend(new_entities)
        return new_entities

    # Pick up anything already known at setup time (rare — DALI
    # discovery normally arrives a few seconds AFTER platform setup via
    # the listen loop, not before, so this is usually empty on the
    # first pass and that's expected/fine).
    initial_entities = []
    for key, state in device.dali_channels.items():
        new = _add_for_key(key, state)
        if new:
            initial_entities.extend(new)
    if initial_entities:
        _LOGGER.info("Setting up %d DALI-64 status sensor(s) on %s",
                      len(initial_entities), device.ip)
        async_add_entities(initial_entities)

    # IMPORTANT: this listener is registered unconditionally, even if
    # device.is_dali is still False right now (the normal case — +DR40=
    # hasn't arrived yet at platform-setup time). Bailing out early here
    # when is_dali is False would mean entities NEVER get created once
    # discovery does complete a few seconds later, since nothing would
    # ever be listening for it.
    @callback
    def _on_new_state(event):
        d = event.data
        if d.get("entry_id") != entry.entry_id:
            return
        ch = d.get("channel")
        if not isinstance(ch, str) or not ch.startswith("dali_"):
            return
        key = ch[len("dali_"):]
        try:
            key = int(key)
        except ValueError:
            pass
        if key not in device.dali_channels:
            return
        state = device.dali_channels[key]
        new_entities = _add_for_key(key, state)
        if new_entities:
            _LOGGER.info("Adding %d new DALI-64 status sensor(s) on %s "
                         "(channel %s)", len(new_entities), device.ip, ch)
            async_add_entities(new_entities)

    entry.async_on_unload(
        hass.bus.async_listen(f"{DOMAIN}_state_update", _on_new_state)
    )


class _DaliBaseSensor(SensorEntity):
    _attr_has_entity_name = False
    _attr_entity_registry_enabled_default = True

    def __init__(self, hass, entry, device: RaylogicDevice, key, initial_state):
        self._hass = hass
        self._entry = entry
        self._device = device
        self._dali_key = key
        self._channel_name = f"dali_{key}"

    def _app_channel_label(self, key) -> str:
        """Render the internal dict key as the Raylogic app's familiar
        channel number. Raylogic's DIN modules all share one global
        channel space (257-1024); each gateway is commissioned with its
        own start address within that range (this device's is read
        live from its own *KA= message — see protocol.py
        dali_global_start — not assumed to always be 401).
        Falls back to the raw key if it isn't a plain 0-63 address
        (e.g. the fallback negative "unknown address" keys).
        """
        if isinstance(key, int) and 0 <= key <= 63:
            return str(self._device.dali_global_start + key)
        return str(key)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._device.mac}_dali")},
            name=f"Raylogic DIN-DALI-64 ({self._device.ip})",
            manufacturer="Raylogic",
            model="DIN-DALI-64 (status-read only)",
            sw_version=self._device.fw_version,
        )

    @property
    def available(self):
        return self._device.is_connected

    async def async_added_to_hass(self):
        self.async_on_remove(
            self._hass.bus.async_listen(f"{DOMAIN}_state_update", self._on_update)
        )
        self.async_on_remove(
            self._hass.bus.async_listen(f"{DOMAIN}_available", self._on_available)
        )

    @callback
    def _on_available(self, event):
        if event.data.get("entry_id") == self._entry.entry_id:
            self.async_write_ha_state()


class DaliBrightnessSensor(_DaliBaseSensor):
    _attr_native_unit_of_measurement = None
    _attr_icon = "mdi:brightness-6"

    def __init__(self, hass, entry, device, key, initial_state):
        super().__init__(hass, entry, device, key, initial_state)
        self._attr_unique_id = f"{device.mac}_dali_{key}_brightness"
        self._attr_name = f"dali_{device.ip_suffix}_ch{self._app_channel_label(key)}_brightness"
        self._value = self._raw_to_pct(initial_state.get("brightness", 0xFF))

    @staticmethod
    def _raw_to_pct(raw: int) -> int:
        # Same inverted convention as H81: 0x01=full, 0xFF=off.
        if raw >= 0xFF:
            return 0
        return round((0xFF - raw) / 0xFF * 100)

    @property
    def native_value(self):
        return self._value

    @callback
    def _on_update(self, event):
        d = event.data
        if d.get("entry_id") != self._entry.entry_id:
            return
        if d.get("channel") != self._channel_name:
            return
        s = d.get("state", {})
        if "brightness" in s:
            self._value = self._raw_to_pct(s["brightness"])
            self.async_write_ha_state()


class DaliColorTempSensor(_DaliBaseSensor):
    # CONFIRMED: literal Kelvin value (see dali.py / const.py notes).
    _attr_native_unit_of_measurement = "K"
    _attr_device_class = "temperature"  # informational only; not a real temp sensor
    _attr_icon = "mdi:thermometer"

    def __init__(self, hass, entry, device, key, initial_state):
        super().__init__(hass, entry, device, key, initial_state)
        self._attr_unique_id = f"{device.mac}_dali_{key}_color_temp_k"
        self._attr_name = f"dali_{device.ip_suffix}_ch{self._app_channel_label(key)}_color_temp_k"
        self._value = initial_state.get("color_temp_kelvin", 0)

    @property
    def native_value(self):
        return self._value

    @callback
    def _on_update(self, event):
        d = event.data
        if d.get("entry_id") != self._entry.entry_id:
            return
        if d.get("channel") != self._channel_name:
            return
        s = d.get("state", {})
        if "color_temp_kelvin" in s:
            self._value = s["color_temp_kelvin"]
            self.async_write_ha_state()
