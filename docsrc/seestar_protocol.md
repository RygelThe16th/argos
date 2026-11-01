# Seestar S30 Pro — Protocol & Control Reference

```{admonition} Where this comes from
:class: note

This is a **reverse-engineering record**, not a vendor specification. ZWO
publishes no protocol documentation for the Seestar. Everything here was
established from three sources: the
[`seestar_alp`](https://github.com/smart-underworld/seestar_alp) project's
source, community protocol notes, and real-device testing against **firmware
7.18** (`fw_ver_int` ~2706+, April–May 2026).

Expect a firmware update to break something. Where a behaviour was confirmed on
hardware, the date is given next to it; treat anything unconfirmed as a
hypothesis.
```

The standard half of this — everything on port 32323 — is plain
[ASCOM Alpaca](https://ascom-standards.org/api/), and ARGOS speaks it through
[alpyca](https://github.com/ASCOMInitiative/alpyca). Only the native JSON-RPC
port is Seestar-specific.

## 1. Hardware Facts

| Property | Value |
|----------|-------|
| Model | ZWO Seestar S30 Pro |
| Sensor | Sony IMX585 |
| Resolution | **3840 × 2160** (width × height) |
| Pixel size | 2.9 µm |
| **Bayer pattern** | **GRBG** ← critical for photometry (NOT RGGB) |
| Science channel | Green (G) — densest sampling in Bayer |
| AAVSO filter code | TG (untransformed Bayer green) |
| Optics | 160 mm f/5.3, quadruplet APO |
| Pixel scale | 3.74 arcsec/pixel |
| Field of view | 3.99° × 2.24° (4.58° diagonal, 239 × 134 arcmin) |
| Mount type | Alt-Az |

## 2. Port Architecture

| Port | Protocol | Status | Purpose |
|------|----------|--------|---------|
| **32323** | HTTP Alpaca REST | **Active — primary** | All hardware control |
| 4700 | JSON-RPC TCP | Silent — unusable | Accepts TCP but never responds to any command |
| 4720 | UDP | No response observed | `scan_iscope` intro. ARGOS sends it on **every** native connect and treats failure as non-fatal (`native_client.py`) — so it is neither "required" nor skippable from the outside |
| 32227 | UDP | No response | Alpaca discovery broadcast — unreliable on S30-Pro |
| 4801 | Binary | Open but unused | Preview frame stream — see plan_live_preview.md |
| 80 | HTTP | Incomplete responses | Internal firmware web UI — not usable |

### Key insight (validated 2026-05-10, firmware 7.18)

**Port 32323 Alpaca is the only working control channel.**

- Port 4700 (JSON-RPC) connects but returns no response to any command.
  The native ZWO app may hold an exclusive lock that cannot be released.
- `MoveAxis` (Alpaca) **works** on firmware 7.18+ despite earlier docs saying otherwise.
  Measured: ~3°/1.5s at 2 deg/s, all 4 directions confirmed.
- No UDP handshake is required. Direct HTTP to port 32323 is sufficient.

## 3. Validated Connection Recipe (2026-05-10)

Tested against real Seestar S30 Pro at 192.168.0.18, firmware 7.18.

### Step 1 — Connect telescope

```http
PUT http://192.168.0.18:32323/api/v1/telescope/0/connected
Body: ClientID=1&ClientTransactionID=1&Connected=true
→ {"ErrorNumber":0, ...}
```

### Step 2 — Read position

```http
GET http://192.168.0.18:32323/api/v1/telescope/0/rightascension?ClientID=1&ClientTransactionID=2
→ {"Value": 22.52, "ErrorNumber": 0, ...}
```

Also works: `altitude`, `azimuth`, `declination`, `tracking`, `slewing`, `atpark`.

### Step 3 — Jog (MoveAxis)

```http
# Start moving North (altitude+) at 2 deg/s
PUT /api/v1/telescope/0/moveaxis
Body: ClientID=1&ClientTransactionID=3&Axis=1&Rate=2.0

# Stop
PUT /api/v1/telescope/0/moveaxis
Body: ClientID=1&ClientTransactionID=4&Axis=1&Rate=0.0
```

Axis mapping: `0` = Azimuth (E/W), `1` = Altitude (N/S).
Positive rate: North / East. Negative rate: South / West.

### Step 4 — Connect camera

```http
PUT http://192.168.0.18:32323/api/v1/camera/0/connected
Body: ClientID=1&ClientTransactionID=5&Connected=true
```

Camera 0 = IMX585 telephoto (science). Camera 1 = wide-angle finder.
GainMin=0, GainMax=600. Default gain: 80.

### Step 5 — Take an exposure

```http
PUT /api/v1/camera/0/startexposure
Body: ClientID=1&ClientTransactionID=6&Duration=10.0&Light=true

# Poll until ready
GET /api/v1/camera/0/imageready   → {"Value": false, ...}  (repeat)
GET /api/v1/camera/0/imageready   → {"Value": true, ...}   (download now)

# Download (slow ~33s via JSON, fast ~3s via ImageBytes)
GET /api/v1/camera/0/imagearray
```

### Python shortcut (uses our wrapper)

```python
from argos.core.alpaca.telescope import Telescope
from argos.core.alpaca.camera import Camera

scope = Telescope("192.168.0.18", 32323)
scope.connect()                         # returns "Seestar S30 Pro_... Telescope"

scope.move_axis(1, 2.0)                 # start moving North
import time; time.sleep(2.0)
scope.stop_axis(1)                      # stop

cam = Camera("192.168.0.18", 32323)
cam.connect()                           # width=3840, height=2160, gain 0-600
cam.set_gain(80)
cam.start_exposure(10.0)
while not cam.is_image_ready():
    time.sleep(0.5)
arr = cam.get_image_array()             # numpy uint16 (height, width)
```

## 4. Alpaca REST API (Port 32323)

Base URL: `http://<telescope_ip>:32323`

### Device map

| Device | Index | Purpose |
|--------|-------|---------|
| Telescope | 0 | Slew, track, park, unpark |
| Camera | 0 | IMX585 telephoto (science camera) |
| Focuser | 0 | Telephoto focuser |
| Focuser | 1 | Wide-angle focuser |
| FilterWheel | 0 | Dark(0) / IR(1) / LP(2) |

### Common query parameters

```
ClientID=42&ClientTransactionID=<n>
```

`ClientTransactionID` is an atomic counter — increment on each call.
Always check `ErrorNumber` in every response: 0 = success, anything else = failure.

## 5. Telescope Control

### GET — Properties

```
GET /api/v1/telescope/0/<property>?ClientID=42&ClientTransactionID=<n>
```

| Property | Type | Description |
|----------|------|-------------|
| `connected` | bool | Connection state |
| `tracking` | bool | Sidereal tracking enabled |
| `slewing` | bool | Slew in progress |
| `atpark` | bool | At park position |
| `athome` | bool | At home position |
| `rightascension` | float | Current RA (decimal hours, in `equatorialsystem`) |
| `declination` | float | Current Dec (decimal degrees, in `equatorialsystem`) |
| `equatorialsystem` | int | Coordinate system of every RA/Dec exchanged: 0 other, 1 topocentric of date, 2 J2000, 3 J2050, 4 B1950 (ASCOM `EquatorialCoordinateType`) |
| `utcdate` | string | Mount clock, ISO-8601 UTC |
| `altitude` | float | Altitude (degrees) |
| `azimuth` | float | Azimuth (degrees) |
| `siderealtime` | float | Local sidereal time (hours) |
| `sitelatitude` | float | Observer latitude |
| `sitelongitude` | float | Observer longitude |

### PUT — Commands

```
PUT /api/v1/telescope/0/<method>
Content-Type: application/x-www-form-urlencoded
Body: ClientID=42&ClientTransactionID=<n>&<params>
```

| Method | Params | Effect |
|--------|--------|--------|
| `connected` | `Connected=true` | Connect to telescope |
| `unpark` | — | Deploy arm → ScopeMoveToHorizon event |
| `park` | — | Park mount (closes arm) |
| `utcdate` | `UTCDate=<ISO-8601>` | Set the mount clock |
| `slewtocoordinatesasync` | `RightAscension=<hours>&Declination=<degrees>` | Async slew to target, in `equatorialsystem` |
| `synctocoordinates` | `RightAscension=<hours>&Declination=<degrees>` | Sync pointing model, in `equatorialsystem` |
| `abortslew` | — | Abort active slew |
| `tracking` | `Tracking=true` | Enable sidereal tracking |

Everything else in ARGOS — catalogues, ASTAP, FITS, Stellarium — is ICRS/J2000.
Since 0.4.2 the conversion happens at the telescope wrapper only: on connect
ARGOS reads `equatorialsystem`, then converts outgoing GoTo and Sync coordinates
into it and incoming positions back to J2000. For topocentric of date that is
the apparent place with right ascension from the true equinox of date (astropy
`TETE`), at the mount's site when the firmware exposes `sitelatitude` and
`sitelongitude`. ARGOS also writes `utcdate` and reads it back; when the mount
clock is still more than 60 s off, GoTo and Sync are refused.

### Alpaca error codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1024 | Property not implemented |
| 1032 | Not initialised (unpark first) |
| 1036 | Action not implemented |
| 1279 | Command rejected (e.g. below horizon) — observed on the device; ARGOS has no specific handler for it |

### MoveAxis — confirmed working on firmware 7.18+

`MoveAxis` was previously believed broken (error 1032). Tested 2026-05-10:
it works correctly. Use it for all manual jogging. Port 4700 is not needed.

```python
# Axis 0 = Azimuth (Primary)   — positive rate = East
# Axis 1 = Altitude (Secondary) — positive rate = North
PUT /api/v1/telescope/0/moveaxis
Body: ClientID=1&ClientTransactionID=<n>&Axis=0&Rate=2.0   # start moving East
# ...hold...
PUT /api/v1/telescope/0/moveaxis
Body: ClientID=1&ClientTransactionID=<n>&Axis=0&Rate=0.0   # stop
```

Measured movement: ~3° per 1.5 s at Rate=2.0 deg/s.
Rate=0.0 stops immediately.

## 6. Camera Control

### GET — Properties

```
GET /api/v1/camera/0/<property>?ClientID=42&ClientTransactionID=<n>
```

| Property | Type | Description |
|----------|------|-------------|
| `gain` | int (0-600) | Current gain value |
| `ccdtemperature` | float | Sensor temperature °C |
| `imageready` | bool | Image ready for download |
| `camerastate` | int | 0=Idle 1=Waiting 2=Exposing 3=Reading 4=Download 5=Error |
| `cameraxsize` | int | 3840 (S30-Pro identifier) |
| `cameraysize` | int | 2160 |

### PUT — Commands

| Method | Params | Effect |
|--------|--------|--------|
| `gain` | `Gain=<0-600>` | Set sensor gain (default: 80) |
| `startexposure` | `Duration=<seconds>&Light=true` | Start exposure |
| `stopexposure` | — | Stop active exposure — ARGOS calls this, never `abortexposure` |

### Image download

```
GET /api/v1/camera/0/imagearray?ClientID=42&ClientTransactionID=<n>
```

- Returns JSON with `Value` field containing int32 array
- Shape: **3840 × 2160** (width × height), flattened 1D
- Download time: ~33 seconds for full 8.3MP frame via JSON
- Reshape: `np.array(value).reshape(2160, 3840)`

### Exposure timing

```
GET /api/v1/camera/0/camerastate  ← poll this
GET /api/v1/camera/0/imageready   ← True when ready
```

Timeouts below come from the external *seevar* project and are **not** used by
ARGOS, which defines its own (for example a 300 s poll bound on a synchronous
mount slew):
- `SLEW_TIMEOUT = 60 s`
- `EXPOSE_TIMEOUT = 120 s`
- `DOWNLOAD_TIMEOUT = 300 s`

## 7. Native JSON-RPC TCP (Port 4700) — Jogging Only

Port 4700 is retained **exclusively** for `scope_speed_move`.
Everything else uses Alpaca.

### Wire format

```python
msg = {"jsonrpc": "2.0", "id": <int>, "method": "<method>", "params": <value>}
wire = (json.dumps(msg) + "\r\n").encode("utf-8")
```

**Note:** Some methods must omit the `params` key entirely (do not send `"params": {}`).
`get_device_state` is one such method — the firmware rejects an explicit empty dict.

### UDP handshake (always attempted, best-effort)

```{note}
Earlier revisions of this page said in one place that the handshake was *not
required* and in another that it was *required*. Neither is what the code does:
`_send_udp_intro()` runs unconditionally before the TCP connect, and a failure
is logged as `Native: UDP intro failed (non-fatal)` and ignored. It costs
nothing, it sometimes helps, and it never blocks the connection.
```

```python
msg = {"id": 1, "method": "scan_iscope", "params": ""}
# Send to port 4720 via UDP before opening TCP socket on 4700
```

### Guest mode / master CLI

After TCP connect, send `set_setting(master_cli=true)` to claim control.
Without this, the device is in observer mode: it sends events (PiStatus, EqModePA…)
but silently ignores all control commands.

```python
{"id": <n>, "method": "set_setting", "params": {"master_cli": true}}
```

Required only when using port 4700. Alpaca has no session lock — no claim needed.

### Manual jogging command

```python
{
    "id": <n>,
    "method": "scope_speed_move",
    "params": {"speed": <int>, "angle": <int>, "dur_sec": <int>}
}
```

| Parameter | Values | Notes |
|-----------|--------|-------|
| `speed` | 500–8000 | 4000 = normal, 8000 = fast |
| `angle` | 0/90/180/270 | 0=North, 90=East, 180=South, 270=West |
| `dur_sec` | int | Mount moves for this duration then auto-stops |

Pattern for held-button jogging:
- Button pressed → send `scope_speed_move(dur_sec=2)`
- Timer every 1500ms → resend to extend movement
- Button released → send `iscope_stop_view`

### Stop command

```python
{"id": <n>, "method": "iscope_stop_view", "params": {}}
```

### Firmware verify injection

| Firmware ver_int | Action |
|-----------------|--------|
| < 2582 | No inject |
| 2583 – 2704 | Add `"verify": true` to params (strict bounds: 2582 and 2705 are excluded) |
| ≥ 2706 (SSL-auth) | No inject (rejected with code 109) |
| 0 (unknown) | No inject (assume modern) |

### Heartbeat (keep TCP alive)

Send `scope_get_equ_coord` every 10s — without it the Seestar closes the connection
after ~20s of inactivity (BrokenPipe on next command).

## 8. Filter Wheel

```
PUT /api/v1/filterwheel/0/position
Body: ClientID=42&ClientTransactionID=<n>&Position=<0-2>
```

| Position | Filter | Use |
|----------|--------|-----|
| 0 | Dark | Calibration / closed shutter |
| 1 | IR | Infrared pass |
| 2 | LP | Light pollution filter |

## 9. Hardware Fingerprinting

To confirm the device is an S30 Pro (not S30 or S50):

```
GET /api/v1/camera/0/cameraxsize?ClientID=42&ClientTransactionID=1
```

- **3840** → S30 Pro (IMX585) ✅
- **1920** → S30 or S50 (IMX662/IMX462)

## 10. Device Discovery

ARGOS tries three layers, in this order:

**1. UDP broadcast** on port 32227 — the standard Alpaca discovery.

**2. Direct HTTP probe** of the last-used host, then the profile's access-point
address, once the broadcast comes back empty:

```
GET http://<ip>:32323/management/v1/configureddevices
```

A 200 response confirms the telescope is online.

**3. TCP sweep** of the local /24 subnet, confirming each candidate with the
same management endpoint.

## 11. Unpark Behaviour

`PUT /api/v1/telescope/0/unpark` deploys the arm — it swings to the horizon
ready for use. The device may emit an unsolicited event as it does so; ARGOS
logs whatever `method` string arrives but does not name or handle any specific
event here.

`park` closes the arm and is the correct safe end state.

**Important:** Alpaca `unpark` does not always open the arm reliably via firmware.
The user may need to use the native Seestar app for the first initialization in a session.
After that, `park` / `unpark` via Alpaca work correctly.

## 12. Safety thresholds — proposed, NOT implemented in 0.4.2

```{danger}
**None of the following exists in ARGOS 0.4.2.** This table is a design note
that was previously presented as behaviour. Do not rely on any of it.

Verified against the source: the string `battery` appears **nowhere** in
`argos/`. No altitude minimum, no pointing-error threshold and no temperature
comparison exist. `CCD-TEMP` is read and recorded — into the FITS header and the
session log — and never acted upon. The only `park()` call is the opt-in
end-of-plan action you choose in the acquisition options.

This is why {doc}`index` says, in its first admonition, that there is no weather
safety and no restart recovery, and that the telescope must be operated
attended. That statement is the accurate one.
```

Retained as a specification for a future release:

| Condition | Proposed threshold | Intended action | Status |
|-----------|-----------|--------|---|
| Sensor temperature | > 55 °C | Park, abort | value read, never compared |
| Battery | < 10 % | Park, alert | **no battery telemetry at all** |
| Target below horizon | altitude < 30° | Skip target | not implemented |
| Pointing error (post-solve) | > 12 arcmin | Retry/skip | not implemented |

## 13. FITS Headers (Correct Values for S30 Pro)

```
BAYERPAT = 'GRBG'        # Sony IMX585 — NOT RGGB
INSTRUME = 'IMX585'
TELESCOP = 'ZWO Seestar S30 Pro'
FOCALLEN = 160            # mm
XPIXSZ  = 2.9            # µm
YPIXSZ  = 2.9
NAXIS1  = 3840            # width
NAXIS2  = 2160            # height
GAIN    = 80              # default gain
```

> **Correction vs earlier assumption:** CLAUDE.md listed BAYERPAT=RGGB and FOCALLEN=150.
> The correct values (from seevar hardware docs) are GRBG and 160 mm.
