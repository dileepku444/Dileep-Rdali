"""Constants for the Raylogic integration."""

DOMAIN = "raylogic"

# Network
DEFAULT_PORT = 5550
CONNECT_TIMEOUT = 5
RECONNECT_DELAY = 30

# ------------------------------------------------------------------ #
# BR40 device codes (byte[1] of BR40 response header)
# Discovered via protocol reverse engineering
# ------------------------------------------------------------------ #
BR40_CODE_H81   = 0x01   # DIN-H81-RS485  — 8ch Triac Dimmer
BR40_CODE_RE16  = 0x09   # DIN-RE16-RS485 — 16ch Relay/Curtain
BR40_CODE_FN4   = 0x19   # DIN-FN4-RS485  — 4ch Fan Dimmer
BR40_CODE_RE8   = 0x89   # DIN-RE8-RS485  — 8ch Relay/Curtain (from capture: +BR40=0189080202030100000070... byte[1]=0x89, byte[2]=0x08 channels)

# To be discovered via capture:
# BR40_CODE_HU4   = ?    # DIN-HU4-RS485  — 4ch Universal Dimmer
# BR40_CODE_F8    = ?    # DIN-F8-RS485   — 8ch Analog/DALI/PWM
# BR40_CODE_DALI  = ?    # DIN-DALI-64    — 64ch DALI Dimmer
# BR40_CODE_LDX   = ?    # LDX-405-CV     — 4ch LED Strip

# Device model names (for UI display and entity naming)
DEVICE_MODELS = {
    0x01: ("h81",  "DIN-H81-RS485",  "8ch Triac Dimmer"),
    0x09: ("re16", "DIN-RE16-RS485", "16ch Relay/Curtain Controller"),
    0x19: ("fn4",  "DIN-FN4-RS485",  "4ch Fan Dimmer"),
    0x89: ("re8",  "DIN-RE8-RS485",  "8ch Relay/Curtain Controller"),
}

# ------------------------------------------------------------------ #
# RE16 relay channel modes (byte[1] of each BR40 channel record)
# ------------------------------------------------------------------ #
RE16_MODE_UNUSED  = 0x00
RE16_MODE_CURTAIN = 0x01
RE16_MODE_SWITCH  = 0x02

# ------------------------------------------------------------------ #
# Fan speeds: HA percentage → raw device level
# Confirmed via Docklight capture on DIN-FN4:
#   *AR=011A010119 = OFF      (level 01)
#   *AR=011A010219 = Speed 1  (level 02)
#   *AR=011A010319 = Speed 2  (level 03)
#   *AR=011A010419 = Speed 3  (level 04)
#   *AR=011A010519 = Speed 4 / Full (level 05)
# ------------------------------------------------------------------ #
FAN_SPEEDS = {0: 0x01, 25: 0x02, 50: 0x03, 75: 0x04, 100: 0x05}

# ------------------------------------------------------------------ #
# DIN-DALI-64 — separate protocol family from BR40-coded devices
# (H81/RE16/RE8/FN4). Not identified via br40_code at all — instead
# detected by the presence of a "+DR40=" message, which none of the
# other device types ever send.
#
# Up to 64 individually-addressable DALI channels (0-63) per gateway.
# Each channel is either:
#   - plain dimmable  -> pushed as "*AR=..."
#   - dimmable + CCT  -> pushed as "*AH=..."
#   - dimmable + RGB  -> format not captured yet, not supported
#
# CONFIRMED from Docklight captures on real hardware:
#   *AR=011A02[brightness]93                     (5 bytes total)
#   *AH=120101[addr_byte][brightness][cct_hi][cct_lo]  (7 bytes total)
#
#   Verified against a known single address (DALI address 0 / app channel
#   401, CCT-swept end to end while nothing else was active):
#     addr_byte  = 0x98 + dali_address   (base offset 0x98 = 152)
#     brightness = single byte, 0x01=full-on .. 0xFF=off (inverted, same
#                  convention as H81/RE16 elsewhere in this integration)
#     cct        = 2 bytes, big-endian, LITERAL KELVIN VALUE (not scaled/
#                  encoded) — observed sweeping cleanly 2700K -> 6500K,
#                  matching the DALI-standard warm/cool range confirmed
#                  independently from a DaliExplorer project export
#                  (ctc_warmest=2702K, ctc_coolest=6535K).
#
#   *AR= (plain-dimmable) address encoding is NOT yet confirmed the same
#   way — the only *AR= captures so far were all a single fixed-address
#   channel, so its address byte position/offset is still a guess. Needs
#   one more known-single-address test on a Type-6 (dimmable-only, no
#   CCT) channel to nail down, the same way *AH= was just confirmed.
#
#   +DR40= discovery dump: record index == DALI address (0,1,2,3,...
#   sequential), record value == that channel's current CCT in Kelvin,
#   or 0x0000 if the channel has no CCT capability (plain dimmable).
#   Confirmed by matching two live captures against a DaliExplorer
#   project export showing the same non-default CCT value (3831K) on
#   the same two addresses.
# ------------------------------------------------------------------ #
DALI_DISCOVERY_MARKER = "+DR40="
DALI_AR_PREFIX = "011A02"   # plain-dimmable channel push prefix
DALI_AH_PREFIX = "120101"   # dimmable+CCT channel push prefix
DALI_AH_ADDR_OFFSET = 0x98  # confirmed: addr_byte = 0x98 + dali_address

# STILL NOT CONFIRMED:
#   - *AR= (plain-dimmable) carries no visible address byte at all
#     (confirmed identical "011A02" prefix across two different known
#     addresses) — a non-issue on THIS install since the customer has
#     confirmed only one plain-dimmable channel exists in total, but
#     would need order-matching against +DR40= on an install with more
#     than one plain-dimmable channel.
#   - the on-demand command to request a +DR40= dump on demand (it has
#     only been observed arriving on its own after '*AR=01' keep-alive)
#   - RGB channel format (no capture available yet)
#   - TX command format for SETTING brightness/CCT from HA (this is
#     why DALI is status-read only for now — no set_dali_* methods)
#
# This section is purely additive. It must never change behaviour for
# H81 (BR40_CODE_H81), RE16/RE8 (BR40_CODE_RE16/RE8) or FN4
# (BR40_CODE_FN4) — those devices never emit "+DR40=" or "*AH=", so
# none of this code path is ever reached for them.

# Platforms
PLATFORMS = ["light", "cover", "fan", "switch", "sensor"]

# Sender node ID (app uses 003)
SENDER_NODE = "003"
