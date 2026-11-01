# ARGOS — Architecture

*A differential-photometry controller for the ZWO Seestar S30 Pro — about
33 kLoC in three layers (core 14.0k, ui 16.4k, workers 2.7k).*

```{admonition} The section markers in this document
:class: note

Cross-references written as `§n` — `(§6 C1)` and the like — point into `notes/photometry_plan.md`, the
project's internal working spec. It is in the repository but deliberately not
published here, so those markers are for someone reading the source tree, not
for this website.
```

```{seealso}
{doc}`api/index` documents every module from its docstrings.
{doc}`differential_photometry` covers what the photometry layer computes, and
{doc}`seestar_protocol` the wire format underneath the driver layer.
```

## Why This Exists

The Seestar S30 Pro is a capable little astrograph — 160 mm f/5.3 quadruplet APO,
IMX585 (Starvis 2), 3840×2160 at 2.9 µm. But its native app treats it as an
eyepiece replacement, not a science instrument. ARGOS fills the gap:

- **ASCOM Alpaca** for all hardware control (telescope, camera, focuser, filter wheel)
- **Differential photometry pipeline** — from raw GRBG mosaic to ensemble light curve
- **AAVSO-ready** — VSX/VSP catalogue queries, TG-band photometry, Siril-compatible
  folder structure
- **Live preview with scientific tools** — HFD metrics, plate solving, multi-star
  aperture photometry, all off the UI thread

The Seestar hardware constraints are real: 2.9 µm pixels, 3.74″/pixel scale,
~4.6° FOV, 55 °C thermal veto, and a WiFi bottleneck that makes a full-frame
download take ~5 s. Every design decision below respects those limits.

## Layer Map

Three layers, one dependency direction. This is the core architectural invariant:

```
┌──────────────────────────────────────────────────────────┐
│                    UI (PyQt6)                             │
│  argos/ui/                                         │
│  Panels, widgets, shell, theme, pages                    │
│  Ø network I/O. Ø blocking work on the UI thread.        │
│  Imports: PyQt6, workers, and core (for pure functions)  │
├──────────────────────────────────────────────────────────┤
│                  Workers (QThread)                        │
│  argos/workers/                                    │
│  Bridge: core ↔ UI. Qt signals only.                     │
│  Imports: core/, PyQt6.QtCore                            │
│  Ø UI widgets. Ø requests.                               │
├──────────────────────────────────────────────────────────┤
│                  Core                                     │
│  argos/core/                                       │
│  Business logic, network clients, data pipelines.        │
│  Imports: stdlib, numpy, astropy, requests               │
│  Qt-free except core/session/ — see below.               │
└──────────────────────────────────────────────────────────┘
```

```{important}
**The arrows above are not all one-way, and the diagram used to claim they
were.** Stating it accurately, as of 0.4.2:

| Rule | Status | Audit |
|---|---|---|
| `core` must not import Qt | **holds**, with one documented exception | `grep -rn PyQt6 argos/core/` → 2 hits |
| `workers` must not import widgets | **holds** | `grep -rn "QtWidgets\|QtGui" argos/workers/` → 0 hits |
| `ui` must not do network I/O | **holds** | `grep -rn "requests\|socket\|urllib" argos/ui/` → 0 hits |
| `ui` must reach `core` only through `workers` | **does not hold** | `grep -rn "from argos.core" argos/ui/` → ~62 hits across 28 modules |
| `core` must not import `ui` or `workers` | **holds** | `grep -rn "argos.ui\|argos.workers" argos/core/` → 0 hits |

The `ui → core` edge is pervasive and mostly legitimate: the interface calls
Qt-free pure functions directly — `imaging.stretch`, `imaging.metrics`,
`photometry.airmass`, `exoplanet.transit` and the like — rather than paying a
thread hop for a microsecond of arithmetic. What `workers` exists for is
anything **blocking**: network, disk, plate solving, long frame maths. Read the
rule as *"nothing that can block reaches core from the UI thread"*, not as
*"the UI never imports core"*.

None of this is enforced by a linter. The five greps above are, between them, a
complete audit; running them in CI would be worth more than this paragraph.
```

The Qt exception under `core/`: `acquisition_engine.py` and `device_session.py`
import `QObject`/`pyqtSignal` because they own the acquisition lifecycle and
emit signals directly. Everything else under `core/` — all frame maths,
photometry, catalogues and protocol clients — is genuinely Qt-free and
headless-testable.

## Complete Module Map

### `core/` — Python, Qt-free outside `session/`

All frame maths, network protocols, catalogues, and science logic live here.

```
core/
├── alpaca/                        # ASCOM Alpaca HTTP device wrappers
│   ├── client.py                  Low-level GET/PUT with typed error handling
│   ├── discovery.py               UDP broadcast scan (port 32227, unreliable)
│   ├── telescope.py               Slew, track, park, MoveAxis (port 32323)
│   ├── camera.py                  Expose, gain, ImageArray download, ImageBytes
│   ├── focuser.py                 Absolute/relative moves, HFD sweep support
│   └── filterwheel.py             Dark(0) / IR(1) / LP(2) positions
│
├── catalog/                       # Star catalogue access (Qt-free, network-isolated)
│   ├── aavso.py                   AAVSO VSX cone search + VSP chart HTTP clients
│   ├── photometry.py              Comparison-star ranking and pilot-frame quality
│   ├── targets.py                 Persistent target/comparison set for a session
│   ├── gaia.py                    Gaia DR3 cone search + cache
│   ├── offline.py                 Bundled Messier/NGC/IC essential catalogue
│   ├── object_resolver.py         Name → coordinates, offline first then Sesame
│   ├── exoplanets.py              NASA Exoplanet Archive PSCompPars lookup
│   ├── field_objects.py           SIMBAD field inventory by object class
│   └── point_identity.py          What did the user actually click?
│
├── imaging/                       # Frames, FITS, solving, metrics
│   ├── imx585.py                  IMX585 (S30 Pro): EGAIN/read-noise anchors, full well
│   ├── imx662.py                  IMX662 (S30): same reference shape
│   ├── imx462.py                  IMX462 (S50): same reference shape
│   ├── sensor_models.py           Dispatch from a sensor name to its module
│   ├── sensor_reference.py        The interpolated gain/read-noise/full-well lookup
│   ├── focus.py                   V-curve parabola fit → best-focus position
│   ├── debayer.py                 GRBG → super-pixel / bilinear / CFA channels
│   ├── green.py                   Single-source-of-truth green-plane extraction
│   ├── stretch.py                 Histogram-based display stretch + statistics
│   ├── metrics.py                 HFD, FWHM, star detection, eccentricity
│   ├── fits_writer.py             16-bit FITS with science-grade headers
│   ├── sequencer.py               Multi-step acquisition plan (Light/Dark/Flat/Bias)
│   ├── session_log.py             Per-frame QA records → session.json (§7)
│   ├── platesolve.py              ASTAP wrapper → WCS, pointing correction
│   ├── astrometry_session.py      Shared solve lifecycle (live plate solve + manual)
│   └── sky_geometry.py            Airmass, Moon separation, phase, Sun altitude
│
├── photometry/                    # Differential pipeline, all Qt-free (§6)
│   ├── aperture.py                Aperture photometry on green plane (§6 C1)
│   ├── differential.py            Ensemble differential: target / comparison / check
│   ├── lightcurve.py              Time-series accumulator + AAVSO-format CSV export
│   ├── airmass.py                 Julian date, BJD_TDB, airmass (Pickering 2002)
│   ├── session.py                 Measure a target set on one solved frame
│   ├── params.py                  Single source of truth for aperture/noise inputs
│   ├── uncertainty.py             Run-level systematic floor (star_var_script model)
│   ├── quality.py                 Leave-one-out comparison-ensemble assessment
│   └── tracking.py                Follow apertures through field rotation
│
├── exoplanet/                     # Transit prediction
│   └── transit.py                 Ephemeris → contact times, coverage, plan
│
├── hardware/                      # Telescope profiles (new in 0.4.1)
│   ├── profile.py                 TelescopeProfile dataclass: optics, sensor, host
│   ├── catalog.py                 S30 Pro (validated), S30, S50 (unvalidated)
│   ├── detect.py                  Driver/profile mismatch warning at connect
│   └── active.py                  The profile in force for this session
│
├── session/                       # Acquisition lifecycle — the Qt exception
│   ├── types.py                   Shared session dataclasses
│   ├── device_session.py          Device connection lifecycle (QObject)
│   ├── acquisition_engine.py      Frame loop, photometry, target set (QObject)
│   ├── review.py                  Read a finished session folder, read-only
│   └── diagnostics.py             Optional per-frame JSONL flight recorder
│
├── seestar/                       # Native JSON-RPC 2.0 TCP (port 4700)
│   └── native_client.py           scope_speed_move jogging; heartbeat; guest mode
│
├── stellarium/                    # Planetarium integration
│   ├── protocol.py                Stellarium Telescope Protocol v1.0 binary codec
│   └── server.py                  Asyncio TCP server, bridges to Qt worker
│
├── support_bundle.py              Local rotating logs, crash reports and manual,
│                                  privacy-redacted support ZIPs (no network)
│
├── location_resolver.py           Site search → latitude, longitude, elevation
│
└── config.py                      ~/.argos/config.json: observer, astrometry,
                                   photometry params, telescope profile, data
                                   paths, Stellarium and UI state
```

### `workers/` — QThread Bridges

Each worker encapsulates one background concern. They form the signal fabric
between core logic and the UI.

```
workers/
├── discovery_worker.py            One-shot UDP scan → device list signal
├── polling_worker.py              Mount RA/Dec/Alt/Az every 2 s
├── exposure_worker.py             Continuous live preview loop
├── preview_processor.py           Off-thread: debayer → stretch → star detect
│                                  (latest-frame-wins: drops stale jobs)
├── sequence_worker.py             Executes SequencePlan → FITS files + session log
├── autofocus_worker.py            HFD V-curve: sweep, measure, parabola fit
├── solve_worker.py                ASTAP solve on one frame → SolveResult signal
├── astrometry_controller.py       Auto-solve policy: cadence, mount-distance gate
├── catalog_worker.py              VSX cone search + VSP chart off the UI thread
├── object_resolver_worker.py      CDS Sesame object designation lookup
├── location_resolver_worker.py    Explicit observing-site lookup + elevation
├── photometry_batch_worker.py     Re-measure a saved FITS set off the UI thread
├── exoplanet_worker.py            NASA Exoplanet Archive lookup off the UI thread
├── camera_service.py              Camera ownership between preview/sequence/focus
├── network_monitor.py             Quiet device/internet reachability status
└── stellarium_worker.py           Asyncio event loop → Qt signals
```

### `ui/` — Presentation Only

Layout, colour, widgets. No `requests`, no `socket`, no business logic.

```
ui/
├── shell.py                       Main window, workflow navigation + File/More menus
├── sidebar.py                     Mode switcher (Connection / Observe / Plan / Review / Settings)
├── statusbar.py                   Live status: devices, tracking, last action
├── theme.py                       Siril-inspired equilux dark palette
├── design.py                      Spacing, typography, layout primitives
├── palettes.py                    Named colour palettes the theme selects from
├── splash.py                      Start-up splash while heavy imports load
├── analysis_window.py             Standalone FITS inspector (post-processing)
│
├── pages/                         # Workflow screens
│   ├── connection_page.py         Device connect/discover + Stellarium pairing
│   ├── imaging_page.py            Dockable live view, capture, focus and photometry
│   ├── sequencer_page.py          Target lookup + dockable planning workspace
│   ├── analyze_page.py            Reload/export measurements and inspect FITS
│   └── configuration_page.py      Observer/site, telescope, paths and astrometry
│
├── panels/                        # Floating / modal panels
│   ├── log_panel.py               Session log (§7) viewer
│   ├── manual_control_dialog.py   MoveAxis jogging with direction pads
│   ├── photometry_window.py       Live light curve + comparison table (§6 C5/C6)
│   └── stellarium_card.py         Server on/off, connection status
│
└── widgets/                       # Reusable dock widgets
    ├── fits_viewer.py             PyQtGraph image display, stretch, crosshair
    ├── image_toolbar.py           Debayer view mode / channel toggles
    ├── overlay_bar.py             Catalog markers, grid, FOV, annotations
    ├── histogram_dock.py          R/G/B histograms, black/white/midtone sliders
    ├── camera_dock.py             Single-shot and continuous exposure controls
    ├── focuser_dock.py            Position readout, move, autofocus trigger
    ├── mount_dock.py              RA/Dec, Alt/Az, jog, GoTo, tracking toggle
    ├── sequence_panel.py          Multi-step plan table (Light/Dark/Flat/Bias)
    ├── astrometry_settings.py     Solve parameters: radius, scale hint, timeout
    ├── star_info_card.py          On-click star: coordinates, magnitude, HFD
    ├── target_table.py            Target set: name, RA/Dec, priority, status
    ├── comparison_table.py        In-use comparison/check ensemble
    ├── lightcurve_panel.py        Separate science and comparison-diagnostic plots
    ├── hfd_history_dock.py        Focus HFD/FWHM history
    ├── statistics_dock.py         Detailed image statistics
    ├── metrics_panel.py           Session trending: HFD, SNR, airmass over time
    ├── dock_host.py               Dock layout host: save, restore, reset
    ├── session_review.py          Offline session browser for the Review screen
    ├── target_curve_panel.py      The science curve (target + check star)
    ├── comparison_curve_panel.py  The comparison diagnostic plot
    ├── variable_table.py          Variables identified in the solved field
    └── vcurve.py                  Autofocus V-curve plot with fitted vertex
```

```{note}
`pages/_placeholder.py` is a stub for screens not yet built and is deliberately
omitted above.
```

## Communication Protocols

### ASCOM Alpaca — HTTP REST (port **32323**)

The primary control channel. Every hardware operation goes through this:

```
http://<device>:32323/api/v1/<device_type>/<index>/<property|method>
```

| Device        | Index | Purpose                            |
|---------------|-------|------------------------------------|
| Telescope     | 0     | Slew, track, park, MoveAxis        |
| Camera        | 0     | IMX585 science camera              |
| Focuser       | 0     | Telephoto focuser                  |
| Focuser       | 1     | Wide-angle focuser (addressable, unused)   |
| FilterWheel   | 0     | Dark(0) / IR(1) / LP(2)            |

**Port note.** The Alpaca server is on **32323**; 4700 is the native JSON-RPC
port. `config.py` defaults to 32323.

MoveAxis — reported broken in earlier firmware — works correctly on 7.18+.
Use it for all manual jogging; the native JSON-RPC path is no longer needed.

**Image download.** Two paths exist:
- *Fast:* binary `ImageBytes` buffer (~3 s for 8.3 MPx)
- *Fallback:* JSON `ImageArray` (~33 s, used when binary dimensions mismatch)

The wrapper in `core/alpaca/camera.py` detects the mismatch and falls back
transparently, logging which path was taken.

### Native JSON-RPC — TCP (port 4700)

Retained exclusively for `scope_speed_move` jogging on firmware where MoveAxis
was broken (pre-7.18). On current firmware this port accepts TCP but returns
no response — the ZWO native app may hold an exclusive lock.

Keep-alive: send `scope_get_equ_coord` every 10 s or the Seestar closes the
connection (BrokenPipe on next command).

### Stellarium Telescope Protocol — TCP (port 10001)

Asyncio server speaking the v1.0 binary protocol. Lets Stellarium drive the
Seestar mount as a standard telescope. The binary codec is in
`core/stellarium/protocol.py`; the asyncio event loop runs on a QThread via
`workers/stellarium_worker.py`.

### Device Discovery

`core/alpaca/discovery.py` tries three layers, in this order:

1. **Alpaca UDP broadcast** on port 32227 — the standard mechanism. Fast when it
   works; silently swallowed by phone hotspots and isolated access points.
2. **Direct HTTP probe** of the last-used host, then `10.0.0.1` (access-point
   mode):
   ```
   GET http://<ip>:32323/management/v1/configureddevices
   ```
   A candidate is accepted on a 2xx response whose body parses as JSON. The
   device *count* is not checked — the Seestar exposes five device endpoints,
   and no code anywhere asserts a number.
3. **TCP sweep of the local /24**, confirming each candidate against the same
   management endpoint.

So discovery works on networks that drop broadcasts; it just takes longer. The
observer-facing version of this is in {doc}`field_connectivity`.

## Threading Model

```
┌─ Main thread ──────────────────────────────────────────┐
│  Qt event loop. Never blocks. Never does I/O.           │
│  Receives signals from workers, updates widgets.        │
└─────────────────────────────────────────────────────────┘
         ▲ signals ▲                ▲ signals ▲
         │         │                │         │
┌────────┴─────────┴──┐   ┌────────┴─────────┴──┐
│  QThread (long-lived)│   │  QThread (per-task) │
│  • exposure_worker   │   │  • solve_worker     │
│  • polling_worker    │   │  • catalog_worker   │
│  • sequence_worker   │   │  • autofocus_worker │
│  • stellarium_worker │   │  • exoplanet_worker │
└──────────────────────┘   └─────────────────────┘

The right-hand column is *not* a `QThreadPool`: those are ordinary `QThread`s
created per task and reaped on completion. `QThreadPool.globalInstance()` is
used at three call sites, all under `core/session/`:
`acquisition_engine.py:1559` (an anonymous `QRunnable` for asynchronous FITS
writing), and `device_session.py:641` and `:653`, which run the named
`_JogRunnable` and `_FilterMoveRunnable`.
```

### The preview chain (three threads, latest-frame-wins)

```{graphviz} diagrams/preview_chain.dot
:align: center
```

1. **LivePreviewWorker** (QThread, in `exposure_worker.py`) starts the camera exposure via Alpaca HTTP, polls
   `ImageReady`, and downloads the raw uint16 array.
2. **PreviewProcessor** (QThread) receives the raw array via the `frame_ready` signal
   and computes the display render, star field, frame metrics, and histograms — all
   off the UI thread.
3. The **UI thread** receives a `ProcessedFrame` dataclass and applies the final
   display stretch, overlays, and histogram update. These are cheap enough
   (numpy uint8 → QImage) to run on the main thread.

**Latest-frame-wins.** If frames arrive faster than `PreviewProcessor` can
process them, stale jobs are silently dropped. The processor keeps only the
most recently submitted `(raw, view)` and skips any intermediate ones.

This is the single most important performance architecture in the app.
Without it, a 5-second image download would freeze the UI. With it, the
preview stays responsive even when the camera is dumping frames faster
than the display can consume them.

## Sequence Execution

One plan becomes a stream of frames through `expand_plan()`, and
`workers/sequence_worker.py` runs each one through the same six steps:

```{graphviz} diagrams/sequence_execution.dot
```

Autofocus runs on a filter change or every *N* frames, whichever the plan asks
for; the exposure step polls `ImageReady` rather than blocking; and the FITS
write is handed to a `QRunnable` so the next frame can start immediately.

## Data Flow — End to End

```
Seestar S30 Pro
    │
    ├── Alpaca HTTP :32323 ──► core/alpaca/ ──► workers/ ──(signals)──► ui/
    │   telescope / camera / focuser / filter wheel
    │
    ├── JSON-RPC TCP :4700 ──► core/seestar/ ──► workers/ ──► (jog only)
    │   scope_speed_move (legacy path)
    │
    ├── Stellarium TCP :10001 ──► core/stellarium/ ──► stellarium_worker ──► ui/
    │   planetarium sync
    │
    └── AAVSO VSX+VSP (HTTPS) ──► core/catalog/ ──► catalog_worker ──► ui/
        variable + comparison star data

FITS output:
    core/imaging/fits_writer.py ──► ~/Argos/sessions/{date}_{target}/
```

## Photometry Pipeline (Preview)

This runs live during an acquisition session — no calibration frames, no
post-processing. The publishable pipeline (darks, flats, bias) runs later
in Siril or a separate reduction script.

From a selected star to an exported row, the call chain is:

```{graphviz} diagrams/photometry_pipeline.dot
```

What each stage computes — and what it leaves out — is in
{doc}`differential_photometry`.

```
Raw frame (uint16, 3840×2160, GRBG)
    │
    ▼
green.py  ──► green plane (1920×1080, (G1+G2)/2 per 2×2 tile)
    │
    ▼
platesolve.py ──► WCS (via ASTAP)
    │
    ▼
catalog/aavso.py ──► variable + comparison stars from VSX/VSP
    │
    ▼
photometry/aperture.py ──► aperture sums for all targets
    │
    ▼
photometry/differential.py ──► ensemble differential magnitudes
    │
    ▼
photometry/lightcurve.py ──► time-series accumulator
```

Each module is unit-testable in isolation. The full chain runs off the UI
thread through the acquisition engine, ASTAP/catalog workers and the batch
photometry worker; the live window only renders the resulting measurements.

## Configuration

File: `~/.argos/config.json`

Key sections:

| Path | Key | Default | Notes |
|------|-----|---------|-------|
| `alpaca` | `host` / `port` | `""` / `32323` | One device IP address and Alpaca port; discovery fills the same endpoint |
| `sessions_path` | — | `~/Argos/sessions/` | Output; Siril-compatible layout |
| `observer` | `name` / `obscode` | empty | FITS observer identity and AAVSO export code |
| `site` | `name` / `latitude` / `longitude` / `elevation` / `favorites` | empty / 0 | FITS site metadata, visibility and airmass/Moon geometry |
| `camera` | `adc_bits` / `full_well_adu` | 12 / 60000 | **Deprecated** — migrated to the telescope profile on first load; kept so an older ARGOS can still read the file |
| `astrometry` | `astap_path` / `database_path` / `database` | `""` / `""` / `""` | ASTAP executable, optional star-database folder and database set |
| `catalog` | `mag_limit` / `max_results` | 15.0 / 250 | VSX cone search bounds |
| `photometry` | `aperture_fwhm_mult` / etc. | 2.5 / ... | Aperture radii in FWHM units |
| `diagnostics` | `enabled` | `false` | Optional local per-frame JSONL diagnostics; never uploaded |

## Session Layout (Siril-Compatible)

Written by `core/imaging/fits_writer.py`. The folder names, the filename pattern
and all 65 header keywords are specified in one place — {doc}`fits_headers` —
and that page is generated against the writer. **Do not duplicate them here**: a
second copy of this layout drifted out of date once already and contradicted the
writer in every field.

In brief: `{RUN}_{OBJECT}/` at the top, lower-case plural `lights/ darks/
flats/ biases/` beneath it as Siril expects, `.fit` files with no timestamp in
the name, plus `session.json`, `photometry_quality.json` and `diagnostics/`.

## Data Model

```{graphviz} diagrams/data_model.dot
:align: center
```

### File persistence

| File | Format | Schema | Lifetime |
|------|--------|--------|----------|
| `~/.argos/config.json` | JSON | `Config` | Permanent |
| `session.json` | JSON | `SessionLog` + `FrameRecord`[] | One session |
| `targets.json` | JSON | `TargetSet` + `TargetStar`[] | One session |
| `*.fits` | FITS | 16-bit uint, full headers | Per frame |
| `photometry.csv` | CSV | `LcPoint` columns | Per target |

### Key dataclasses

| Class | Module | Purpose |
|---|---|---|
| `Config` | `core/config.py` | Application settings (observer, camera, astrometry, photometry) |
| `SequencePlan` | `core/imaging/sequencer.py` | Acquisition plan (multiple steps) |
| `FrameSpec` | `core/imaging/sequencer.py` | One frame to shoot |
| `FrameWCS` | `core/imaging/platesolve.py` | Pixel ↔ celestial mapping |
| `SolveResult` | `core/imaging/platesolve.py` | ASTAP outcome |
| `TargetSet` | `core/catalog/targets.py` | Session's selected stars |
| `TargetStar` | `core/catalog/targets.py` | One star with role (target/comp/check) |
| `AperturePhot` | `core/photometry/aperture.py` | Raw aperture measurement |
| `DiffResult` | `core/photometry/differential.py` | Calibrated differential magnitude |
| `LightCurve` | `core/photometry/lightcurve.py` | Accumulated points for a target |
| `SessionLog` | `core/imaging/session_log.py` | Per-frame QA records |

## Key Constants (from Hardware)

| Quantity | Value | Source |
|----------|-------|--------|
| Focal length | 160 mm | seevar hardware docs |
| Aperture | f/5.3 | 160 / 30.0 mm |
| Sensor | Sony IMX585 (Starvis 2) | — |
| Bayer pattern | **GRBG** (not RGGB) | critical for photometry |
| Pixel size | 2.9 µm | IMX585 datasheet |
| Resolution | 3840 × 2160 | 8.3 MPx |
| Pixel scale | 3.74″/px | 206.265 × 2.9 / 160 |
| Field of view | 3.99° × 2.24° (4.58° diagonal) | `hardware/profile.py` `fov_deg` |
| Saturation flag | `camera.linearity_max_adu`, 50 000 ADU | **not reconciled with `EGAIN`/`FULLWELL`** — see {doc}`differential_photometry` §3.5 |
| AAVSO filter code | TG | untransformed Bayer green |

```{warning}
Earlier revisions of this table quoted a 4.6° field of view and a "55 °C thermal
veto". The first was the *diagonal* presented as the width; the second describes
software that does not exist — nothing in `argos/` compares a temperature
against a threshold. See {doc}`seestar_protocol` §12.
```
