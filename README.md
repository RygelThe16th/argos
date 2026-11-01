# ARGOS

**A desktop observing workspace for time-series photometry with smart telescopes.**

ARGOS helps an observer move from a connected telescope to a well-documented
observing session: acquire raw FITS frames, solve and identify the field, keep
the chosen target/comparison/check-star ensemble with the session, and review
what happened afterwards.

The point is to make a small smart telescope useful for measurement without
pretending that a live curve is a final scientific result. ARGOS keeps the raw
data intact. Calibration, registration and final photometry remain a separate,
auditable reduction workflow in Siril and `star_var_script`.

> **0.4.2 is a field-validation release.** Use it with the telescope attended,
> retain every raw FITS frame, and independently review or reduce the data
> before drawing a scientific conclusion.

## What ARGOS 0.4.2 does

- Connects to Seestar equipment through Alpaca, with discovery, pointing,
  framing, focusing controls and sequence capture.
- Writes a durable session record alongside unmodified raw FITS frames,
  including observing context, frame inventory and selected-star roles.
- Uses a local ASTAP installation to recover a WCS and align the solved field
  with catalogue information.
- Identifies and filters Gaia stars, variable stars, exoplanet hosts, deep-sky
  objects and available VSP references; catalogue lookups and caches remain
  under the observer's control.
- Supports target, comparison-star and check-star selection, with a quick-look
  differential-photometry preview during acquisition.
- Opens a completed session in Review: inspect frame-quality trends, curves,
  metadata, source FITS frames and field overlays without reconnecting a
  telescope.
- Creates an explicit, local and redacted support bundle. ARGOS has no
  telemetry, analytics or automatic crash upload.

Read the documentation before the first session:

- [Install and release notes](https://perretjules.com/argos/install.html)
- [First observing session](notes/guide_terrain_0_4_1_fr.md)
- [Identify a solved field](docsrc/field_identification.md)
- [What changed in 0.4.2](notes/release_0_4_2.md)

## Supported scope and roadmap

The **Seestar S30 Pro** is the 0.4.2 reference profile. S30 and S50 profiles
can be selected for connection, framing and exploratory work, but their
camera-specific assumptions still need field validation before they are used
for precision claims.

ARGOS is being extended to the full Seestar range. **DwarfLab support is
planned for the next ARGOS version**; it is not part of 0.4.2.

## Install

Release packages are attached to the
[ARGOS 0.4.2 release](https://github.com/jperret21/argos/releases/tag/v0.4.2):

- **macOS / Apple silicon:** download `Argos-0.4.2-macOS-arm64.dmg`, copy
  ARGOS to Applications and open it locally. The application is not signed
  with an Apple Developer certificate; use **Control-click → Open** for the
  first launch. If macOS reports that it is damaged, remove the quarantine
  attribute deliberately:

  ```bash
  xattr -dr com.apple.quarantine /Applications/Argos.app
  ```

- **Debian-family Linux / x86-64:** download `argos_0.4.2_amd64.deb` and
  install it with:

  ```bash
  sudo apt install ./argos_0.4.2_amd64.deb
  ```

  The Debian package is a technical preview. Validate it on the actual field
  computer before relying on it for a session.

ARGOS does not bundle ASTAP. Install [ASTAP](https://www.hnsky.org/astap.htm)
and an appropriate star database separately, then set both paths in
**Settings → Astrometry**.

## Run from source

```bash
git clone https://github.com/jperret21/argos.git
cd argos
uv sync --extra dev
uv run python main.py
```

The repository includes a mock Alpaca server when no telescope is available:

```bash
# terminal 1 — mock Seestar
uv run python scripts/mock_alpaca_server.py

# terminal 2 — ARGOS
uv run python main.py
```

See [`docsrc/simulator_testing.md`](docsrc/simulator_testing.md) for the
simulator workflow and [`notes/guide_terrain_0_4_1_fr.md`](notes/guide_terrain_0_4_1_fr.md)
for the technical field guide maintained with the source tree.

## Privacy and field diagnostics

ARGOS does not collect telemetry or upload data automatically. Network-backed
catalogue and site lookups are initiated by the observer; local catalogue data
and caches can be inspected in Settings.

**More → Create local support bundle…** creates a ZIP only when requested. It
contains redacted logs and optional redacted session metadata. Raw FITS,
observer identity, site coordinates, network addresses and private paths are
excluded by design. Review the ZIP and share it manually only when you decide
to do so.

## Development, CI and releases

GitHub Actions runs formatting, linting, tests and documentation checks on pull
requests and on pushes to `main` or `release/**`. A version tag such as
`v0.4.2` builds the macOS DMG and Debian package, then creates a GitHub release
draft with both assets. Publishing that draft remains a deliberate human step.

For local checks:

```bash
uv run --extra dev ruff check argos/ tests/ main.py
uv run --extra dev black --check argos/ tests/ main.py
# The suite runs in two processes; a single process can abort on the
# simulator fixtures. This is what CI does.
export QT_QPA_PLATFORM=offscreen
uv run --extra dev pytest -q --deselect tests/ui/test_photometry_window.py
uv run --extra dev pytest -q tests/ui/test_photometry_window.py
uv run --extra docs sphinx-build -W -b html docsrc /tmp/argos-docs
```

Contributions, issue reports and field observations are welcome. See
[`docsrc/CONTRIBUTING.md`](docsrc/CONTRIBUTING.md).

## License

ARGOS is distributed under the [GNU General Public License v3.0](LICENSE).
It does not bundle ASTAP, which remains a separate tool under its own licence.
