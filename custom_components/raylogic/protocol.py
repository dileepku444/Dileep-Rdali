"""Raylogic TCP protocol client."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable, Optional

from .const import (
    DEFAULT_PORT, CONNECT_TIMEOUT, SENDER_NODE,
    BR40_CODE_H81, BR40_CODE_RE16, BR40_CODE_FN4, BR40_CODE_RE8, DEVICE_MODELS,
    RE16_MODE_CURTAIN, RE16_MODE_SWITCH, RE16_MODE_UNUSED,
)
from . import dali

_LOGGER = logging.getLogger(__name__)


class RaylogicDevice:
    """Represents a single Raylogic device on the network."""

    def __init__(self, ip: str, port: int = DEFAULT_PORT,
                 state_callback: Optional[Callable] = None):
        self.ip = ip
        self.port = port
        self.state_callback = state_callback

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._connected = False
        self._msg_counter = 0
        self._listen_task: Optional[asyncio.Task] = None
        self._ka_task: Optional[asyncio.Task] = None

        # Device info
        self.node_id: Optional[str] = None
        self.mac: Optional[str] = None
        self.fw_version: Optional[str] = None
        self.br40_code: Optional[int] = None

        # Channel states keyed by 1-based channel index
        self.channel_states: dict[int, dict] = {}

        # --- DALI-64 (additive, separate from br40_code devices) --- #
        # is_dali becomes True the first time we see a "+DR40=" line —
        # never touched for H81/RE16/RE8/FN4, which never send that.
        self.is_dali: bool = False
        # DALI channels keyed by whatever address byte we parsed (or a
        # sequential fallback index if that ever fails) -> state dict.
        self.dali_channels: dict[int, dict] = {}
        self._dali_next_fallback_index = 0
        self._last_ka_hex: Optional[str] = None
        # Global "app channel number" start for this DALI gateway,
        # parsed from its own *KA= message (bytes[5:7], big-endian) —
        # e.g. 0x0191 = 401, matching the app's own "Start address"
        # field exactly. This is the SAME 257-1024 global-channel space
        # every other Raylogic DIN module uses (global_start = 256 +
        # device code) — for DALI the device-code-equivalent is simply
        # embedded directly in the KA message rather than answered via
        # +BR40=. Falls back to 401 (this install's observed value)
        # only if parsing ever fails, so behaviour never regresses to
        # "no channel numbers at all".
        self.dali_global_start: int = 401

    # ------------------------------------------------------------------ #
    # Properties
    # ------------------------------------------------------------------ #

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def device_type(self) -> str:
        """Short model identifier e.g. h81, re16, fn4."""
        if self.br40_code in DEVICE_MODELS:
            return DEVICE_MODELS[self.br40_code][0]
        return "unknown"

    @property
    def device_model(self) -> str:
        """Full product name e.g. DIN-H81-RS485."""
        if self.br40_code in DEVICE_MODELS:
            return DEVICE_MODELS[self.br40_code][1]
        return "Raylogic Device"

    @property
    def device_description(self) -> str:
        """Description e.g. 8ch Triac Dimmer."""
        if self.br40_code in DEVICE_MODELS:
            return DEVICE_MODELS[self.br40_code][2]
        return ""

    @property
    def ip_suffix(self) -> str:
        """Last octet of IP for entity naming."""
        return self.ip.split(".")[-1]

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #

    async def connect(self) -> bool:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.ip, self.port),
                timeout=float(CONNECT_TIMEOUT),
            )
            self._connected = True
            _LOGGER.info("Connected to Raylogic at %s", self.ip)

            await self._drain_initial_push()
            await self._identify()

            if self.br40_code is None:
                for _ in range(3):
                    line = await self._read_line(timeout=2.0)
                    if line and "*KA=" in line:
                        self.node_id = line.split(",")[0].strip()
                        self._extract_br40_code_from_ka(line)
                        break

            await self._sync_time()
            try:
                # Bounded so a device that never answers +BR40= (e.g. a
                # DIN-DALI-64, which uses a completely different +DR40=
                # protocol) can't stall connect() for minutes. H81/RE16/
                # RE8/FN4 already succeed on the very first attempt per
                # the comments in _query_state, so this timeout is never
                # hit for them — purely a safety net for unrecognized
                # devices, DALI included.
                await asyncio.wait_for(self._query_state(), timeout=20.0)
            except asyncio.TimeoutError:
                _LOGGER.info(
                    "Device %s: no +BR40= reply within 20s (expected for "
                    "DIN-DALI-64, which uses +DR40= instead) — continuing",
                    self.ip,
                )

            self._listen_task = asyncio.create_task(self._listen_loop())
            self._ka_task = asyncio.create_task(self._keepalive_loop())

            if self.state_callback:
                self.state_callback(self.ip, None, {"available": True})

            return True

        except Exception as exc:
            _LOGGER.error("Failed to connect to %s: %s", self.ip, exc)
            self._connected = False
            return False

    async def disconnect(self):
        self._connected = False
        for task in (self._listen_task, self._ka_task):
            if task:
                task.cancel()
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass

    async def _reconnect(self):
        _LOGGER.warning("Connection lost to %s, retrying in 30s", self.ip)
        if self.state_callback:
            self.state_callback(self.ip, None, {"available": False})
        await asyncio.sleep(30)
        await self.connect()

    # ------------------------------------------------------------------ #
    # I/O
    # ------------------------------------------------------------------ #

    def _next_msg(self) -> str:
        self._msg_counter = (self._msg_counter % 999) + 1
        return f"{self._msg_counter:03d}"

    async def _send_raw(self, cmd: str):
        if not self._connected or not self._writer:
            return
        try:
            self._writer.write((cmd + "\r").encode())
            await self._writer.drain()
            _LOGGER.debug("TX %s: %s", self.ip, cmd)
        except Exception as exc:
            _LOGGER.error("Send error to %s: %s", self.ip, exc)
            self._connected = False
            asyncio.create_task(self._reconnect())

    async def _send_addressed(self, cmd: str):
        await self._send_raw(f"{SENDER_NODE},{self._next_msg()},{cmd}")

    async def _read_line(self, timeout: float = 2.0) -> Optional[str]:
        try:
            data = await asyncio.wait_for(
                self._reader.readuntil(b'\r'), timeout=timeout
            )
            return data.decode(errors="replace").strip()
        except asyncio.TimeoutError:
            return None
        except asyncio.IncompleteReadError as exc:
            line = exc.partial.decode(errors="replace").strip()
            return line if line else None
        except Exception as exc:
            _LOGGER.error("Read error from %s: %s", self.ip, exc)
            self._connected = False
            asyncio.create_task(self._reconnect())
            return None

    # ------------------------------------------------------------------ #
    # Startup
    # ------------------------------------------------------------------ #

    async def _drain_initial_push(self):
        line = await self._read_line(timeout=3.0)
        if line and "*KA=" in line:
            self.node_id = line.split(",")[0].strip()
            self._extract_br40_code_from_ka(line)

    def _extract_br40_code_from_ka(self, line: str):
        try:
            ka_data = line.split("*KA=")[1]
            _, hex_data = ka_data.split("-")
            data_bytes = bytes.fromhex(hex_data.strip())
            self.br40_code = data_bytes[7]
            _LOGGER.info("Device %s type: %s (0x%02X)",
                         self.ip, self.device_type, self.br40_code)
        except Exception as exc:
            _LOGGER.warning("Could not extract device code from KA: %s", exc)

    async def _identify(self):
        await self._send_raw("?NV11")
        line = await self._read_line(timeout=1.5)
        if line and "+NV11=" in line:
            try:
                data = line.split("+NV11=")[1]
                parts = data.split(",")
                self.mac = parts[0].split("_")[1]
                self.fw_version = parts[1]
            except Exception:
                pass
        elif line and "*KA=" in line:
            self.node_id = line.split(",")[0].strip()
            if self.br40_code is None:
                self._extract_br40_code_from_ka(line)

    async def _sync_time(self):
        now = datetime.now()
        await self._send_raw(f"*SY06={now.strftime('%d%m%y%H%M%S')}")

    async def _query_state(self):
        if self.br40_code is None:
            return
        await asyncio.sleep(1.0)

        # Try the KA-guessed code first, then fall back to every other known
        # code. This only kicks in if the first (normal) attempt gets no
        # +BR40 reply — for devices whose KA-derived code is already correct
        # (H81/RE16/FN4 today), the very first try succeeds and nothing here
        # changes vs. before.
        candidate_codes = [self.br40_code] + [
            c for c in DEVICE_MODELS if c != self.br40_code
        ]

        for code in candidate_codes:
            code_hex = f"01{code:02X}"
            await self._send_addressed(f"?BR40={code_hex}")
            for _ in range(10):
                line = await self._read_line(timeout=5.0)
                if not line:
                    continue
                if "+BR40=" in line:
                    if self._parse_br40(line):
                        _LOGGER.info("State loaded for %s: %d channels",
                                     self.ip, len(self.channel_states))
                        return
                if "*KA=" in line:
                    await self._send_raw("*KA=2")

    # ------------------------------------------------------------------ #
    # Control — H81 Dimmer
    # ------------------------------------------------------------------ #

    async def set_light(self, ch_num: int, ha_brightness: int, on: bool):
        """Set H81 dimmer channel.
        Device level is inverted: 0x01=full, 0xFF=off.
        Format: 01 1A <ch_index> <level> <ch_num>
        """
        ch_index = self.channel_states.get(ch_num, {}).get("ch_index", 0x01)
        if on:
            level = max(1, min(255, 256 - ha_brightness))
        else:
            level = 0xFF
        cmd_hex = f"011A{ch_index:02X}{level:02X}{ch_num:02X}"
        await self._send_addressed(f"*AR={cmd_hex}")
        self.channel_states.setdefault(ch_num, {}).update(
            {"level": level, "on": on}
        )
        if self.state_callback:
            self.state_callback(self.ip, ch_num, self.channel_states[ch_num])

    # ------------------------------------------------------------------ #
    # Control — H82 Relay
    # ------------------------------------------------------------------ #

    @property
    def global_start(self) -> int:
        """Global channel start = 256 + BR40 device code."""
        return 256 + (self.br40_code or 0x01)

    def _global_ch(self, device_ch: int) -> int:
        """Convert 1-based device channel to global channel number."""
        return self.global_start + device_ch - 1

    async def set_cover(self, pair_num: int, action: str):
        """Control RE16 curtain pair.
        addr = 0x80 + (global_ch_open - 256 - 1) // 2 + 1
        action: open / close / stop
        """
        state = self.channel_states.get(pair_num, {})
        timer = state.get("timer", 0x19)
        ch_open = state.get("ch_open", pair_num * 2 - 1)
        global_ch = self._global_ch(ch_open)
        addr = 0x80 + (global_ch - 256 - 1) // 2 + 1
        if action == "open":
            cmd_hex = f"0027{addr:02X}02{timer:02X}"
        elif action == "close":
            cmd_hex = f"0027{addr:02X}01{timer:02X}"
        else:  # stop
            cmd_hex = f"0026{addr:02X}0000"
        await self._send_addressed(f"*AR={cmd_hex}")
        self.channel_states.setdefault(pair_num, {}).update({"action": action})
        if self.state_callback:
            self.state_callback(self.ip, pair_num, self.channel_states[pair_num])

    async def set_switch(self, pair_num: int, on: bool):
        """Control RE16 single relay channel.
        Format confirmed from PCAP: 01 1A <ch_idx> <02=on|01=off> <global_ch-256>
        ch_idx comes from BR40 record byte[0]
        """
        state = self.channel_states.get(pair_num, {})
        ch_num = state.get("ch_num", pair_num)
        ch_idx = state.get("ch_idx", 0x05)
        global_ch = self._global_ch(ch_num)
        val = 0x02 if on else 0x01
        cmd_hex = f"011A{ch_idx:02X}{val:02X}{global_ch - 256:02X}"
        _LOGGER.debug("set_switch pair=%d ch=%d global=%d on=%s cmd=%s",
                      pair_num, ch_num, global_ch, on, cmd_hex)
        await self._send_addressed(f"*AR={cmd_hex}")
        self.channel_states.setdefault(pair_num, {}).update({"on": on})
        if self.state_callback:
            self.state_callback(self.ip, pair_num, self.channel_states[pair_num])

    # ------------------------------------------------------------------ #
    # Control — FN4 Fan
    # ------------------------------------------------------------------ #

    async def set_fan(self, ch_num: int, speed_pct: int):
        """Set FN4 fan speed.
        Format confirmed via Docklight capture: 01 1A <area> <level> <ch_offset>
          ab = 01 (fixed)
          cd = 1A (AREA CHANNEL DIRECT function code)
          ef = Area = the Area this fan channel is CURRENTLY assigned to
               (1-16, configurable in the app — read from BR40 response,
               same as H81's ch_index pattern, NOT fixed to ch_num)
          gh = Level: 01=OFF, 02=Speed1, 03=Speed2, 04=Speed3, 05=Full Speed
          ij = Channel offset = global_ch - 256 (e.g. 0x19 for the first FN4 fan)
        speed_pct: 0/25/50/75/100
        """
        from .const import FAN_SPEEDS
        level = FAN_SPEEDS.get(speed_pct, 0x01)
        area = self.channel_states.get(ch_num, {}).get("ch_index", ch_num)
        global_ch = self._global_ch(ch_num)
        ch_offset = global_ch - 256
        cmd_hex = f"011A{area:02X}{level:02X}{ch_offset:02X}"
        _LOGGER.debug("set_fan ch=%d area=%d level=%02X offset=%02X cmd=%s",
                      ch_num, area, level, ch_offset, cmd_hex)
        await self._send_addressed(f"*AR={cmd_hex}")
        self.channel_states.setdefault(ch_num, {}).update(
            {"speed": speed_pct, "on": speed_pct > 0}
        )
        if self.state_callback:
            self.state_callback(self.ip, ch_num, self.channel_states[ch_num])

    # ------------------------------------------------------------------ #
    # Background loops
    # ------------------------------------------------------------------ #

    async def _listen_loop(self):
        while self._connected:
            line = await self._read_line(timeout=30.0)
            if line:
                self._dispatch_line(line)

    async def _keepalive_loop(self):
        while self._connected:
            await asyncio.sleep(10)
            if self._connected:
                await self._send_raw("*KA=2")

    # ------------------------------------------------------------------ #
    # Parsers
    # ------------------------------------------------------------------ #

    def _dispatch_line(self, line: str):
        if "*KA=" in line:
            self._handle_ka(line)
        elif "+BR40=" in line:
            self._parse_br40(line)
        elif dali.is_discovery_line(line):
            # +DR40= never sent by H81/RE16/RE8/FN4 — safe additive branch.
            self._handle_dali_discovery(line)
        elif self.is_dali and dali.is_dali_ah_line(line):
            self._handle_dali_ah(line)
        elif self.is_dali and dali.is_dali_ar_line(line):
            # Gated on self.is_dali (only ever True for a device that has
            # sent +DR40=) so an H81 unit whose configurable ch_index
            # happens to be 0x02 can never be misrouted here — its *AR=
            # pushes always fall through to the existing _handle_ar below,
            # completely unchanged from before this file was touched.
            self._handle_dali_ar(line)
        elif "*AR=" in line:
            self._handle_ar(line)

    def _handle_ka(self, line: str):
        try:
            self.node_id = line.split(",")[0].strip()
        except Exception:
            pass
        # Stashed so _handle_dali_discovery can pull the embedded start
        # address out of it (see dali.py / global_start below). Purely
        # additive — nothing else reads this field.
        try:
            self._last_ka_hex = line.split("*KA=")[1].split("-", 1)[1].strip()
        except Exception:
            pass

    def _handle_ar(self, line: str):
        """Handle incoming *AR from keypad/other nodes for real-time state."""
        try:
            ar_hex = line.split("*AR=")[1].strip()
            b = bytes.fromhex(ar_hex)
            if len(b) < 5:
                return

            if self.br40_code == BR40_CODE_H81:
                # H81: b[4]=ch_num, b[3]=level(inverted)
                ch_num = b[4]
                level = b[3]
                on = level < 0xFF
                existing = self.channel_states.get(ch_num, {})
                existing.update({"level": level, "on": on})
                self.channel_states[ch_num] = existing
                if self.state_callback:
                    self.state_callback(self.ip, ch_num, existing)

            elif self.br40_code == BR40_CODE_FN4:
                if b[1] == 0x1A:
                    # AREA CHANNEL DIRECT echo: 01 1A <area> <level> <ch_offset>
                    # area = the Area this fan channel is currently assigned to
                    # (configurable, so reverse-lookup by stored ch_index — same
                    # pattern as RE16's ch_idx matching)
                    from .const import FAN_SPEEDS
                    speed_reverse = {v: k for k, v in FAN_SPEEDS.items()}
                    area = b[2]
                    level = b[3]
                    on = level > 0x01
                    speed_pct = speed_reverse.get(level, 0)
                    for ch_num, state in self.channel_states.items():
                        if state.get("ch_index") == area:
                            state.update({"level": level, "on": on, "speed": speed_pct})
                            if self.state_callback:
                                self.state_callback(self.ip, ch_num, state)
                            break

            elif self.br40_code in (BR40_CODE_RE16, BR40_CODE_RE8):
                if b[1] == 0x1A:
                    # Switch relay command: 01 1A <ch_idx> <02=on|01=off> <global_offset>
                    global_offset = b[4]
                    val = b[3]
                    on = val == 0x02
                    # Find entity by global offset
                    for en, state in self.channel_states.items():
                        if state.get("mode") == "switch":
                            ch = state.get("ch_num", 0)
                            if self._global_ch(ch) - 256 == global_offset:
                                state.update({"on": on})
                                if self.state_callback:
                                    self.state_callback(self.ip, en, state)
                                break
                elif b[1] in (0x27, 0x26):
                    # Curtain command: 00 27/26 <addr> <dir> <timer>
                    addr = b[2]
                    if addr >= 0x80:
                        # Reverse addr to find ch_open
                        # addr = 0x80 + (global_ch - 256 - 1)//2 + 1
                        # global_ch - 256 = (addr - 0x81) * 2 + 1
                        global_offset = (addr - 0x81) * 2 + 1
                        for en, state in self.channel_states.items():
                            if state.get("mode") == "curtain":
                                ch = state.get("ch_open", 0)
                                if self._global_ch(ch) - 256 == global_offset:
                                    direction = b[3]
                                    action = {1: "close", 2: "open", 0: "stop"}.get(direction, "stop")
                                    state.update({"action": action})
                                    if self.state_callback:
                                        self.state_callback(self.ip, en, state)
                                    break

        except Exception as exc:
            _LOGGER.debug("AR parse error '%s': %s", line, exc)

    def _parse_br40(self, line: str) -> bool:
        """Parse +BR40 channel state response."""
        try:
            data_hex = line.split("+BR40=")[1].strip()
            data = bytes.fromhex(data_hex)
            if len(data) < 3:
                return False

            device_code = data[1]
            ch_count = data[2]
            records = data[3:]
            record_size = 8

            if len(records) < ch_count * record_size:
                return False

            # The BR40 response is authoritative for device identity — it's
            # the device answering our own query, unlike the *KA push whose
            # fixed-offset parsing can pick the wrong byte on some units
            # (confirmed on DIN-RE8). Sync self.br40_code so entity setup
            # (switch.py/cover.py) and command addressing (global_start) use
            # the correct code. For devices where the KA guess was already
            # right (H81/RE16/FN4), this just reconfirms the same value.
            if device_code in DEVICE_MODELS and self.br40_code != device_code:
                _LOGGER.info(
                    "Correcting device %s type: %s (0x%02X) -> %s (0x%02X)",
                    self.ip, self.device_type, self.br40_code or 0,
                    DEVICE_MODELS[device_code][0], device_code,
                )
                self.br40_code = device_code

            if device_code == BR40_CODE_H81:
                self._parse_br40_h81(records, ch_count, record_size)
            elif device_code in (BR40_CODE_RE16, BR40_CODE_RE8):
                self._parse_br40_re16(records, ch_count, record_size)
            elif device_code == BR40_CODE_FN4:
                self._parse_br40_fn4(records, ch_count, record_size)

            if self.state_callback:
                for ch_num, state in self.channel_states.items():
                    self.state_callback(self.ip, ch_num, state)

            return True

        except Exception as exc:
            _LOGGER.warning("BR40 parse error '%s': %s", line, exc)
            return False

    def _parse_br40_h81(self, records: bytes, ch_count: int, record_size: int):
        """Parse H81 dimmer channel records."""
        for i in range(ch_count):
            r = records[i * record_size:(i + 1) * record_size]
            ch_num = i + 1
            ch_index = r[0]
            level = r[3]   # inverted: 0x01=full, 0xFF=off
            on = level < 0xFF
            self.channel_states[ch_num] = {
                "ch_index": ch_index,
                "level": level,
                "on": on,
            }

    def _parse_br40_re16(self, records: bytes, ch_count: int, record_size: int):
        """Parse RE16 relay channel records.

        Detection logic:
          timer == 0 → single relay switch (no motor timeout needed)
          timer  > 0 → curtain pair (motor needs safety timeout)

        Mode byte is NOT reliable for switch detection:
          mode=02 can mean curtain (e.g. Main Mom room)
          mode=00 can mean switch (e.g. Relay1/Relay2)

        Channels with timer=0 are processed as individual switches.
        Channels with timer>0 are processed as curtain pairs (2 channels each).
        """
        entity_num = 0
        ch = 1
        while ch <= ch_count:
            r = records[(ch - 1) * record_size:ch * record_size]
            ch_idx = r[0]
            timer = r[7] if len(r) > 7 else 0

            entity_num += 1

            if timer == 0:
                # Single relay switch — no timer means on/off load
                self.channel_states[entity_num] = {
                    "mode": "switch",
                    "ch_num": ch,
                    "ch_idx": ch_idx,
                    "entity_num": entity_num,
                    "on": False,
                }
                _LOGGER.debug("RE16 switch %d: CH%d ch_idx=%02X",
                              entity_num, ch, ch_idx)
                ch += 1  # single channel
            else:
                # Curtain pair — has timer for motor protection
                self.channel_states[entity_num] = {
                    "mode": "curtain",
                    "ch_open": ch,
                    "ch_close": ch + 1,
                    "ch_idx": ch_idx,
                    "entity_num": entity_num,
                    "timer": timer,
                    "action": "unknown",
                }
                _LOGGER.debug("RE16 curtain %d: CH%d-%d timer=%ds",
                              entity_num, ch, ch + 1, timer)
                ch += 2  # two channels per curtain pair

    def _parse_br40_fn4(self, records: bytes, ch_count: int, record_size: int):
        """Parse FN4 fan channel records.
        r[0] = ch_index = the Area this channel is CURRENTLY assigned to (1-16,
               configurable in the app, same pattern as H81's ch_index)
        r[3] = level: 01=OFF, 02=Speed1, 03=Speed2, 04=Speed3, 05=Full Speed
        """
        from .const import FAN_SPEEDS
        speed_reverse = {v: k for k, v in FAN_SPEEDS.items()}
        for i in range(ch_count):
            r = records[i * record_size:(i + 1) * record_size]
            ch_num = i + 1
            ch_index = r[0]
            level = r[3]
            on = level > 0x01
            speed_pct = speed_reverse.get(level, 0)
            self.channel_states[ch_num] = {
                "ch_index": ch_index,
                "level": level,
                "on": on,
                "speed": speed_pct,
            }

    # ------------------------------------------------------------------ #
    # DALI-64 — additive, status-read only (see dali.py for format notes)
    # ------------------------------------------------------------------ #

    def _dali_key_for(self, parsed_addr: Optional[int]) -> int:
        """Pick a stable dict key for a DALI channel update.

        We key primarily on the parsed address byte. If parsing ever
        fails to produce one (e.g. for *AR= which has no address byte
        at all in the confirmed 5-byte shape), fall back to a stable
        sequential index so at least single-channel installs keep working
        without crashing — multi-channel installs will need the address
        byte confirmed to be reliable, see dali.py.
        """
        if parsed_addr is not None:
            return parsed_addr
        self._dali_next_fallback_index += 1
        return -self._dali_next_fallback_index  # negative = "unknown address"

    def _handle_dali_discovery(self, line: str):
        """Handle a +DR40= DALI discovery dump.

        Marks this device as DALI (self.is_dali = True) and records raw
        per-record type-code hints for later use. This does not create
        or update channel brightness/CCT state — that comes exclusively
        from the *AR=/*AH= pushes, which are much better confirmed.
        """
        was_dali = self.is_dali
        self.is_dali = True
        if not was_dali:
            if self.br40_code is not None:
                # DALI's *KA= payload has a different layout than H81/RE16/
                # RE8/FN4's, so the generic fixed-offset KA guess can pick
                # a misleading BR40 code (e.g. it happens to land on 0x01,
                # "h81") for a DALI gateway. Clear it now that we know
                # better — this only ever affects a device that has just
                # sent +DR40=, so it can't touch other device types.
                _LOGGER.info(
                    "Device %s: clearing incorrect BR40 guess (0x%02X) — "
                    "this is a DIN-DALI-64, not a BR40-coded device",
                    self.ip, self.br40_code,
                )
                self.br40_code = None
            _LOGGER.info("Device %s identified as DIN-DALI-64 (saw +DR40=)", self.ip)

            # Pull the true global-channel start out of the KA message
            # we stashed, so entity numbering matches this specific
            # installation's actual commissioned start address instead
            # of assuming every DALI gateway starts at 401.
            if self._last_ka_hex:
                try:
                    ka_bytes = bytes.fromhex(self._last_ka_hex)
                    if len(ka_bytes) >= 7:
                        parsed_start = (ka_bytes[5] << 8) | ka_bytes[6]
                        if 257 <= parsed_start <= 1024:
                            self.dali_global_start = parsed_start
                            _LOGGER.info(
                                "Device %s: DALI global channel start = %d "
                                "(parsed from *KA=)", self.ip, parsed_start,
                            )
                except (ValueError, IndexError):
                    pass

        records = dali.parse_discovery(line)
        if records:
            _LOGGER.debug("Device %s: +DR40= reported %d channel record(s) "
                           "(experimental parse, informational only)",
                           self.ip, len(records))

    def _handle_dali_ar(self, line: str):
        """Handle a plain-dimmable DALI channel push (*AR=011A02xx93)."""
        parsed = dali.parse_ar(line)
        if not parsed:
            _LOGGER.debug("Device %s: unparsed DALI *AR= line: %s", self.ip, line)
            return

        # The confirmed 5-byte *AR= shape has no address byte at all, so
        # for now every plain-dimmable channel on a given gateway shares
        # fallback key 0 unless/until a real address byte is confirmed.
        # Fine for single plain-dimmable-channel installs; multi-channel
        # plain-dimmable installs will need this revisited once the
        # address encoding is confirmed against real hardware logs.
        key = 0
        existing = self.dali_channels.get(key, {})
        existing.update({
            "kind": "dimmable",
            "brightness": parsed["brightness"],
            "on": parsed["on"],
        })
        self.dali_channels[key] = existing
        if self.state_callback:
            self.state_callback(self.ip, f"dali_{key}", existing)

    def _handle_dali_ah(self, line: str):
        """Handle a dimmable+CCT DALI channel push (*AH=120101...)."""
        parsed = dali.parse_ah(line)
        if not parsed:
            _LOGGER.debug("Device %s: unparsed DALI *AH= line: %s", self.ip, line)
            return

        # dali_address (0-63) is now confirmed via the 0x98 addr_byte
        # offset (see dali.py); fall back to the raw addr_byte only if
        # that ever comes back None (offset formula not matching).
        key = parsed["dali_address"] if parsed["dali_address"] is not None \
            else self._dali_key_for(parsed.get("addr_byte"))
        existing = self.dali_channels.get(key, {})
        existing.update({
            "kind": "cct",
            "brightness": parsed["brightness"],
            "on": parsed["on"],
            "color_temp_kelvin": parsed["color_temp_kelvin"],
        })
        self.dali_channels[key] = existing
        if self.state_callback:
            self.state_callback(self.ip, f"dali_{key}", existing)
