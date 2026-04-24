# TTN JSON — Home Assistant Integration

A Home Assistant custom integration that subscribes to The Things Network (TTN) v3 MQTT uplink messages and exposes individual decoded payload fields as sensor entities.

## Features

- UI-based setup via config flow (Settings → Add Integration)
- Supports any TTN v3 application and device
- Maps arbitrary nested JSON payload fields to HA sensor entities using a simple slash-delimited path notation
- Options flow — edit topic and sensor fields after setup without reinstalling
- Auto-imports existing `configuration.yaml` entries on first boot
- Full MQTT push (no polling)

---

## Installation

### Via HACS (recommended)

1. In Home Assistant, open **HACS → Integrations**
2. Click the ⋮ menu → **Custom repositories**
3. Enter the repository URL and select category **Integration**
4. Click **Add**, then find and install **TTN JSON**
5. Restart Home Assistant

### Manual

Copy the `custom_components/ttnjson2` folder into your HA `config/custom_components/` directory and restart.

---

## Prerequisites

- The **MQTT integration** must be configured in Home Assistant and connected to your TTN MQTT broker
- TTN MQTT broker details (under your TTN application → Integrations → MQTT):
  - **Server**: `nam1.cloud.thethings.network` (US) or `eu1.cloud.thethings.network` (EU)
  - **Username**: `your-app-id@ttn`
  - **Password**: A TTN API key with read permissions
  - **Port**: 1883 (plain) or 8883 (TLS)
- An **Uplink Payload Formatter** configured in your TTN application so that `decoded_payload` fields are populated

---

## Setup

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **TTN JSON**
3. Fill in the form:

| Field | Description | Example |
|-------|-------------|---------|
| **Device EUI** | Your TTN device ID | `my-tracker-001` |
| **MQTT Topic** | TTN v3 uplink topic — use `<EUI>` as placeholder | `v3/my-app@ttn/devices/<EUI>/up` |
| **Sensor fields** | One per line as `path/to/field:unit` | see below |

### Sensor field format

Fields are specified using a slash-delimited path into the TTN JSON payload, followed by a colon and the unit of measurement:

```
uplink_message/decoded_payload/temperature:°F
uplink_message/decoded_payload/battery:V
uplink_message/decoded_payload/elevation:°
uplink_message/decoded_payload/azimuth:°
uplink_message/rx_metadata/rssi:dB
```

List nodes (like `rx_metadata`, which is an array) are handled automatically — the first element is used.

Each field becomes a separate HA sensor entity named `{device_eui} {field_name}`, for example:
- `sensor.my_tracker_001_temperature`
- `sensor.my_tracker_001_battery`
- `sensor.my_tracker_001_rssi`

---

## Editing after setup

Go to **Settings → Devices & Services → TTN JSON → Configure** to update the MQTT topic or sensor fields for a device. HA will reload the integration automatically.

---

## Legacy configuration.yaml

Existing `configuration.yaml` entries are automatically imported into the config entry registry on first boot — no manual migration needed. The yaml format is still supported:

```yaml
sensor:
  - platform: ttnjson2
    eui: my-tracker-001
    topic: v3/my-app@ttn/devices/<EUI>/up
    values:
      uplink_message/decoded_payload/temperature: "°F"
      uplink_message/decoded_payload/battery: V
      uplink_message/rx_metadata/rssi: dB
```

---

## TTN Payload Formatter example

In your TTN console under **Applications → your-app → Payload Formatters → Uplink**, add a JavaScript formatter that returns named fields:

```javascript
function decodeUplink(input) {
    var b = input.bytes;
    return {
        data: {
            elevation: ((b[0] << 8) | b[1]) / 100.0,
            azimuth:   ((b[2] << 8) | b[3]) / 100.0,
            battery:   ((b[4] << 8) | b[5]) / 1000.0,
            fault:      b[6],
        }
    };
}
```

---

## Troubleshooting

**No sensor values after setup:**
- Check the MQTT integration is connected (Developer Tools → MQTT → Listen to a topic and subscribe to your TTN topic manually to verify messages are arriving)
- Verify your TTN application has a payload formatter configured and `decoded_payload` is populated in the TTN live data view
- Check HA logs (Settings → System → Logs) for `ttnjson2` warnings

**Sensors created but always unavailable:**
- The field path may be wrong — check the exact JSON structure in the TTN live data console and adjust your slash-delimited path accordingly

---

## License

MIT

## Author

[@wdawson61](https://github.com/wdawson61)

---

## Select Entities (Mode Commands)

Select entities send a single uint8 byte as a TTN downlink and reflect the device's actual mode from its uplink reports — no optimistic state.

### Config flow setup

In the **Select Entities** step (step 2 of setup), enter one block per select, separated by a blank line:

```
name=mode
f_port=1
state_path=uplink_message/decoded_payload/mode
AUTO:0
STOW:1
SNOW:2
HAIL:3
```

| Field | Description |
|-------|-------------|
| `name=` | Entity name suffix (full name will be `{eui} mode`) |
| `f_port=` | LoRaWAN FPort for the downlink (default: 1) |
| `state_path=` | Slash-delimited path to the mode field in the uplink JSON |
| `NAME:value` | Symbolic name → uint8 value mapping (hex `0x01` or decimal `1` both accepted) |

Leave the field blank to skip select entities.

### How it works

- **Downlink**: user picks an option in HA → integration encodes the uint8 value as base64 → publishes TTN downlink envelope to `{uplink_topic_base}/down/push`
- **State update**: device receives command, changes mode, sends an unsolicited uplink with the new mode byte → integration reads `state_path`, reverse-maps the byte to the symbolic name, updates the HA entity

The state is never updated optimistically — it only changes when the device confirms via uplink.

### Payload formatter

Keep the TTN formatter simple — just pass the raw byte through:

```javascript
function decodeUplink(input) {
    return {
        data: {
            mode:      input.bytes[0],
            elevation: ((input.bytes[1] << 8) | input.bytes[2]) / 100.0,
            azimuth:   ((input.bytes[3] << 8) | input.bytes[4]) / 100.0,
            battery:   ((input.bytes[5] << 8) | input.bytes[6]) / 1000.0,
        }
    };
}
```

The symbolic mapping (0 → AUTO, 1 → STOW, etc.) lives entirely in the HA integration config.
