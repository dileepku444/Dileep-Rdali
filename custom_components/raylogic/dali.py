"""DIN-DALI-64 protocol parsing helpers.

Status: STATUS-READ ONLY (no control/set commands yet — the TX format
for setting brightness/CCT from Home Assistant has not been captured
from real hardware yet). This module only turns incoming wire lines
into Python dicts; it never sends anything.

Confirmed from Docklight captures on real hardware:

  Plain-dimmable channel push:
      *AR=011A02[brightness]93
      e.g. *AR=011A02F793 -> brightness=0xF7
      Fixed 5-byte payload. Confirmed by watching a single dimmable
      channel fade end-to-end: only byte[3] changed, byte[4] (0x93)
      was constant across the whole fade.

  Dimmable+CCT channel push:
      *AH=120101 98 [brightness][cct_hi][cct_lo]
      e.g. *AH=1201019801A08C -> brightness=0x01, cct=0xA08C (example)
      Confirmed by watching one CCT-capable channel: first a full
      brightness fade (one byte changed, a following 2-byte value
      stayed fixed), then a CCT sweep (that 2-byte value changed,
      brightness stayed fixed). The two fields are independent.

NOT confirmed — needs live verification once running against real
hardware logs (see const.py for the full list):
  - which exact byte in *AH= (and potentially *AR=) is the per-channel
    address when MULTIPLE channels are active. All single-channel test
    captures we have only ever showed ONE channel, so the address byte
    never had a chance to vary and be isolated. A later multi-channel
    capture strongly suggested an address byte sits right after the
    fixed prefix (values increasing steadily like 0x98, 0x9B, 0x9C...
    across channels of the same area) but this was read off a phone
    screenshot, not a raw text export, so exact digits are not 100%
    certain. `addr_byte` below is best-effort and logged at DEBUG so
    it can be checked against real traffic.
  - the CCT byte order (hi/lo) shown above is a guess based on typical
    big-endian Kelvin encoding (0x0A8C = 2700 -> plausible warm-white
    Kelvin value); needs confirming against the app's displayed value.
"""
from __future__ import annotations

import logging
from typing import Optional

_LOGGER = logging.getLogger(__name__)

AR_PREFIX = "011A02"
AH_PREFIX = "120101"


def is_discovery_line(line: str) -> bool:
    """True if this line is a +DR40= DALI discovery/refresh dump."""
    return "+DR40=" in line


def is_dali_ar_line(line: str) -> bool:
    """True if this looks like a DALI plain-dimmable channel push."""
    if "*AR=" not in line:
        return False
    try:
        payload = line.split("*AR=")[1].strip()
        return payload.upper().startswith(AR_PREFIX)
    except IndexError:
        return False


def is_dali_ah_line(line: str) -> bool:
    """True if this looks like a DALI dimmable+CCT channel push."""
    if "*AH=" not in line:
        return False
    try:
        payload = line.split("*AH=")[1].strip()
        return payload.upper().startswith(AH_PREFIX)
    except IndexError:
        return False


def parse_ar(line: str) -> Optional[dict]:
    """Parse a plain-dimmable DALI channel push.

    Confirmed format: 011A02 [brightness] 93  (5 bytes).

    IMPORTANT: unlike *AH= (CCT), this message carries no visible
    address byte at all — the "011A02" prefix stayed byte-for-byte
    identical across two different known addresses tested (address 0
    and address 2), so there is genuinely no way to tell which
    plain-dimmable channel a given *AR= push refers to from the
    message content alone.

    Confirmed with the installer: on this deployment there is only
    ONE plain-dimmable (non-CCT) channel in total — every other
    channel is dimmable+CCT (*AH=). So this ambiguity is a non-issue
    in practice: every *AR= push can only ever be that one channel.
    protocol.py keys all *AR= updates under a single fixed slot (0)
    for exactly this reason. If a future install ever has more than
    one plain-dimmable channel, this will need revisiting (most likely
    via order-matching against a +DR40= dump, the same trick that
    worked for figuring out the CCT/type table).

    Returns {"brightness": int 0-255, "on": bool} or None if the line
    doesn't match the confirmed shape.
    """
    try:
        payload = line.split("*AR=")[1].strip()
        b = bytes.fromhex(payload)
    except (IndexError, ValueError):
        return None

    if len(b) != 5 or b[:3].hex().upper() != "011A02":
        return None

    brightness = b[3]
    return {
        "brightness": brightness,
        "on": brightness < 0xFF,
        "raw": payload,
    }


def parse_ah(line: str) -> Optional[dict]:
    """Parse a dimmable+CCT DALI channel push.

    CONFIRMED format (verified against a known single address — DALI
    address 0 / app channel 401 — swept through CCT end to end with
    nothing else active on the bus):

        120101 [addr_byte] [brightness] [cct_hi] [cct_lo]   (7 bytes)

        addr_byte  = 0x98 + dali_address   (base offset 152)
        brightness = 0x01..0xFF, inverted (0x01=full-on, 0xFF=off)
        cct        = 2 bytes big-endian, literal Kelvin value

    Returns a dict with a decoded "dali_address" (0-63) when addr_byte
    is a plausible offset value, plus the raw brightness/CCT. Returns
    None if the line is too short/unparseable.
    """
    try:
        payload = line.split("*AH=")[1].strip()
        b = bytes.fromhex(payload)
    except (IndexError, ValueError):
        return None

    if len(b) < 7 or b[:3].hex().upper() != "120101":
        return None

    addr_byte = b[3]
    brightness = b[4]
    cct_kelvin = (b[5] << 8) | b[6]

    dali_address = addr_byte - 0x98
    if not (0 <= dali_address <= 63):
        # Offset formula not matching -> address unreliable, but still
        # return the rest of the data with dali_address=None rather than
        # silently dropping a real status update.
        _LOGGER.debug(
            "DALI *AH= addr_byte 0x%02X outside expected 0x98-0xD7 range "
            "(would decode to address %d) — keeping raw addr_byte only",
            addr_byte, dali_address,
        )
        dali_address = None

    _LOGGER.debug(
        "DALI *AH= raw=%s addr_byte=0x%02X dali_address=%s "
        "brightness=0x%02X cct=%dK",
        payload, addr_byte, dali_address, brightness, cct_kelvin,
    )

    return {
        "addr_byte": addr_byte,
        "dali_address": dali_address,
        "brightness": brightness,
        "on": brightness < 0xFF,
        "color_temp_kelvin": cct_kelvin,
        "raw": payload,
    }


def parse_discovery(line: str) -> list[dict]:
    """Best-effort parse of a +DR40= discovery dump into per-channel records.

    EXPERIMENTAL — record boundaries were reconstructed from screenshots
    across several captures, not a clean raw export, so this should be
    treated as a rough guess, not ground truth. It is only used to learn
    *how many* channels exist and a device-type hint; it is not required
    for status-read updates (those come from *AR=/*AH= pushes directly).

    Returns a list of {"index": int, "type_code": int} dicts.
    """
    records: list[dict] = []
    try:
        payload = line.split("+DR40=")[1].strip()
        data = bytes.fromhex(payload)
    except (IndexError, ValueError):
        return records

    # Header: first 2 bytes look like the gateway's DALI start address
    # (e.g. 0x0191), followed by a few marker bytes before per-channel
    # records begin. This offset is a best guess.
    pos = 5
    index = 0
    while pos + 2 < len(data):
        length = data[pos] if data[pos] in (0x11, 0x12) else None
        if length is None:
            # Doesn't look like a record boundary — bail out rather than
            # guess wrong and desync for the rest of the dump.
            break
        record = data[pos:pos + 1 + length] if pos + 1 + length <= len(data) else None
        if not record or len(record) < 4:
            break
        type_code = (record[2] << 8) | record[3]
        records.append({"index": index, "type_code": type_code})
        pos += 1 + length
        index += 1

    if records:
        _LOGGER.debug("DALI discovery: parsed %d channel record(s) (experimental)", len(records))
    return records
