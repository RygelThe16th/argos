# Argos 0.4.2 — photometry corrections

> Branch: `release/0.4.2`, cut from `main` with `release/0.4.1` merged in.
> Last updated: 2026-11-01.

A patch release with no new features. It exists because writing the 0.4.1
documentation against the source turned up five measurement defects, none of
which were visible from the interface — the curve looked the same, the file
looked the same, and the numbers were wrong. Three fixes outside photometry
joined it later: GoTo coordinates, aperture tracking on an EQ mount, and the
Observe window freezing on dense fields.

If you have exports from 0.4.1 or earlier, **three of the five affect files you
already hold**. Re-running the batch measurement on the saved frames
regenerates them correctly.

---

## What was wrong

| Defect | Effect on your data | Affects old exports |
|---|---|---|
| Offline batch photometry used an electron gain of **1.0** whenever `camera.egain_table` was empty — the default | ADU treated as electrons: every batch SNR and every formal error wrong by the gain (1.723 e⁻/ADU for an IMX585 at gain 80) | **yes** |
| A point was emitted only when the relative-flux ratio existed | A run whose comparisons were all flagged *suspect* silently lost good differential magnitudes — no row, no note | **yes** |
| `comps_used` reported the flux-ratio ensemble | The wrong ensemble size stated beside every `mag` | **yes** |
| The *suspect* flag never reached the export | Measured, plotted, then dropped: a hot pixel drifting through an aperture left no trace in the file | file only |
| Airmass came from the mount's last reported altitude | Sampled at an arbitrary instant while the point is timed at the exposure midpoint | file only |

The ratio path rejects *suspect* comparisons and the magnitude path does not,
which is why the first three interact: the same flag that shrank one ensemble
also decided whether a row existed at all.

## What changed

- Offline gain falls back to the sensor reference, as the live path already
  did. The two paths now resolve the same $g$ for the same frame.
- A point is kept when **either** product exists. Only a frame that produced
  neither a magnitude nor a ratio is dropped.
- `comps_used` counts the magnitude ensemble; the ratio's own count is exported
  beside it as the new `relative_comps_used` rather than in its place.
- `suspect` is a column. It still removes nothing — the observer decides.
- New `airmass_at()` derives airmass from the target's own coordinates at the
  point's JD. The frame-level value remains the fallback when no site is set.

Both the live and the offline batch paths carried copies of three of these
defects. They now share `airmass_at()` rather than each holding an inline copy,
so they cannot drift apart again.

## Deliberately not changed

Two items were judgement calls about the science, not bugs, and are documented
rather than silently altered:

- **The no-FWHM aperture is 7.5 green px** (~56″ on an S30 Pro), not the 4 px
  floor three docstrings claimed. Widening or narrowing it changes a
  measurement, so the arithmetic is untouched and pinned by a test; the
  comments now state what it actually does.
- **`camera.linearity_max_adu = 50000`** cannot be reconciled with the
  project's own EGAIN/FULLWELL tables at any shipped gain, so the saturation
  flag effectively never fires. Resolving it needs a **gain measured from a
  pair of flats**, not a guess. Recorded as an open defect.

## AAVSO export

Added after the cut: the export left two fields the AAVSO asks for as `na`,
although Argos had the data.

- `CHART` names the VSP chart the comparison stars came from — VSP returns its
  ID, and Argos used to discard it. It stays `na` when an ensemble mixes charts
  or holds a comparison picked by hand. Comparisons saved before this change
  carry no chart until they are picked again.
- `KNAME`/`KMAG` carry the check star: its AUID and its ensemble magnitude on
  the same frame. It is still never written as an observation of its own.
- The chart ID is kept in `targets.json` and in the
  `photometry_selection.json` hand-off (`catalogue_chart_id`), so
  star_var_script reports it too.

## Mount: GoTo and Sync coordinates

ASCOM takes `SlewToCoordinates` and `SyncToCoordinates` in the driver's
`EquatorialSystem`, and most drivers report topocentric of date. Everything in
ARGOS — catalogues, ASTAP, FITS, Stellarium — is ICRS/J2000, and 0.4.1 sent it
unconverted.

- On connect, ARGOS reads `EquatorialSystem` and converts at the telescope
  wrapper only: outgoing GoTo and Sync into the driver's system, positions read
  back into J2000. The Activity log names the system on every connect.
- Topocentric of date is converted to astropy's `TETE` frame (true equator,
  true equinox of date), at the mount's site when the firmware exposes it.
  **The first version of this fix used `CIRS`**, which counts right ascension
  from the CIO and so leaves out precession along the equator: measured on
  2026-11-01, CIRS and TETE differ by 16–21′ on the sky for Vega, an equatorial
  star and a star at −30°, and for Vega CIRS was further from the true place
  than raw J2000. Corrected before tagging; a test now pins the right ascension
  sent for an equatorial star to the precession since J2000 (3.075 s/yr), which
  CIRS fails.
- ARGOS writes the mount clock and reads it back. Some Seestar firmware accepts
  the write and keeps a stale clock, which turns an EQ GoTo into an hour-angle
  error; above 60 s of offset GoTo and Sync are refused, with a message saying
  by how many hours.
- Astropy's IERS auto-download is off for these conversions, so a live GoTo
  never waits on the network.

## Aperture tracking on an EQ mount

There is no field rotation on an equatorial mount, but the rigid fit still
found some in centroid noise and turned every aperture about the sensor centre,
moving stars near the edges. Live, in the `EQ` and `EQ (GEM)` alignment modes,
the tracker now fits a translation only.

**Not done for the offline batch re-measurement**: saved frames do not record
the mount's alignment mode, so it cannot tell and still fits the rigid
transform. Recording the mode in the FITS header would close this; it is a
format change, left for a later version.

## Observe window on dense fields

Catalogue delivery, plate solves and aperture tracking each re-projected the
whole overlay synchronously, and together they held the Qt event loop long
enough to show the busy cursor. Redraws now go through a 75 ms timer that folds
a burst into one projection, and the Gaia–SIMBAD identity match runs in NumPy
blocks of 256 stars instead of nested loops.

## Also

- Alpaca docstrings called port 4700 the default; it is the native JSON-RPC
  port. These are published through autodoc and contradicted the architecture
  page.
- The Plan screen's heading said "Sequencer" while the sidebar said "Plan".
- Splash typo: "Devellloped".

## State of the branch

- 575 tests pass, 71 skipped; `ruff` and `black` clean.
- Sphinx builds with `-W` and no warnings.
- The documentation now describes 0.4.2 behaviour, with a *Before 0.4.2* box
  at each of the two places where an old file reads differently
  (`differential_photometry` §3.3 and §8). The guide, the protocol page and the
  configuration table cover the mount clock, the coordinate conversion and EQ
  tracking.
- `main`'s site work since the cut is merged in.

## Release

- [x] Tag `v0.4.2`. The Release workflow builds the DMG and the Debian package
      and attaches them to a **draft** release.
- [ ] At the telescope: the Activity log names the
      coordinate system the Seestar reports on connect, and a GoTo on a known
      star lands it at the centre of the frame — in EQ mode as well as alt-az.
- [x] Download both installers from the draft, then publish it — published
      2026-11-01.
- [x] Update the download names, sizes, date and release-tag links in
      `README.md`, `docsrc/install.md` and the public site
      (`docs/install.html`, `docs/index.html`). They point at the 0.4.1
      artefacts until then on purpose: those are the files that exist, and
      the sizes can only be read from the published ones.
- [x] Merge into `main`, which redeploys the site and the documentation.
