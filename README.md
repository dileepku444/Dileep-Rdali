# Raylogic Home Assistant Integration

Native local integration for **Raylogic** DIN-Rail and retrofit home automation devices.
Controls dimmers, relay/curtain modules, and fan controllers directly over TCP without
any cloud dependency.

## Supported Devices (RS485/LAN Series)

| Device | Model | HA Entities | Status |
|--------|-------|-------------|--------|
| DIN-H81-RS485 | 8ch Triac Dimmer 1.25A/ch | `light` with brightness | ✅ Tested |
| DIN-RE16-RS485 | 16ch Relay/Curtain Controller | `cover` (open/close/stop) | ✅ Tested |
| DIN-FN4-RS485 | 4ch Fan Dimmer (Hum-Free) | `fan` with 4 speeds | ✅ Tested |
| DIN-HU4-RS485 | 4ch Universal Dimmer | `light` | 🔜 Planned |
| DIN-RE8-RS485 | 8ch Relay/Curtain Controller | `cover`/`switch` | ✅ Tested |
| DIN-DALI-64-RS485 | 64ch DALI Dimmer | `light` (RGB/CCT/white) | 🔜 Planned |
| DIN-F8-RS485 | 8ch Analog 0-10V/PWM | `light` | 🔜 Planned |
| LDX-405-CV-RS485 | 4ch LED Strip 5A/ch | `light` (RGB) | 🔜 Planned |
| MOD-4U | 4ch WiFi Universal | `light`/`switch` | 🔜 Planned |
| MOD-2U | 2ch WiFi Universal | `light`/`switch` | 🔜 Planned |
| MOD-F | 1ch WiFi Fan | `fan` | 🔜 Planned |

## Installation

1. Copy `custom_components/raylogic/` to your HA `config/custom_components/` folder
2. Restart Home Assistant
3. **Settings → Integrations → Add Integration → Raylogic**
4. Enter the IP address of each device — type is auto-detected
5. Repeat for each device on your network

## How It Works

Each Raylogic device has a LAN port and speaks a simple TCP protocol on **port 5550**.
The integration connects directly to each device — no hub, no cloud, no gateway needed.

On connect, the device sends a `*KA` status push that identifies its type:
- `0x01` → DIN-H81 dimmer
- `0x09` → DIN-RE16 relay
- `0x19` → DIN-FN4 fan
- `0x89` → DIN-RE8 relay

The integration then queries full channel state via `?BR40` and listens for real-time
updates from keypads and other controllers on the RS-485 bus.

## Entity Naming

Entities follow the pattern `<model>_<ip_last_octet>_<type><num>`:
- `light.h81_21_ch1` — H81 dimmer at 192.168.100.21, channel 1
- `cover.re16_22_curtain1` — RE16 relay at 192.168.100.22, curtain pair 1
- `fan.fn4_23_fan1` — FN4 fan controller at 192.168.100.23, fan 1
- `cover.re8_25_curtain1` / `switch.re8_25_switch1` — RE8 relay at 192.168.100.25

Rename entities in HA to match your room/zone names.

## Multiple Devices

Add each device IP separately. Multiple units of the same type are distinguished
by IP last octet — `h81_21_ch1`, `h81_24_ch1` etc.

## Protocol Notes

- **Port**: TCP 5550
- **Terminator**: `\r` (carriage return only, not `\r\n`)
- **Local push**: Device broadcasts `*KA` and `*AR` on state changes
- **No cloud**: Works entirely on local network
- **Real-time**: Keypad and app changes reflect in HA within ~1 second

## Relay/Curtain Notes

- RE16 creates 8 cover entities (all 16 channel pairs)
- RE8 uses the same relay/curtain protocol as RE16 (just fewer channels) —
  channels with `timer=0` become `switch` entities, channels with `timer>0`
  become `cover` (curtain) entities, exactly like RE16
- Disable unused pairs in HA entity settings
- Open/Close/Stop only — no position tracking (hardware limitation)
- Motor safety timer is handled by the RE16/RE8 hardware, not HA

## Version History

- v1.0.0 — H81 dimmer support
- v1.1.0 — RE16 relay/curtain support
- v1.2.0 — (next) FN4 fan controller support
- v1.3.0 — RE8 relay/curtain support (0x89)
