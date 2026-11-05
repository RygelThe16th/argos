# Using ARGOS — a complete session

This guide covers one full observing session, from the settings you fill in
indoors to the folder you hand to Siril at the end. It assumes ARGOS 0.4.2 and a
Seestar telescope.

```{admonition} 0.4.2 is a field-validation release
:class: warning

Operate the telescope attended: there is no weather safety and no restart
recovery. Keep every raw FITS frame, and do not treat an ARGOS curve as
publishable without independent reduction. What the live curve does and does not
mean is set out in {doc}`differential_photometry`.
```

## The night in seven moves

`````{grid} 1 2 2 4
:gutter: 2

````{grid-item-card} 1 · Indoors
Site, observer code, telescope profile, sessions folder. Resolve tonight's
targets while you still have internet.
````

````{grid-item-card} 2 · Seestar app
Power up, run the **GoTo pointing calibration**, focus. ARGOS does not replace
this step.
````

````{grid-item-card} 3 · Connect
Telescope, Camera, Filter Wheel and Focuser must all read *Ready*.
````

````{grid-item-card} 4 · Plan
Resolve the target, check its altitude, build the steps, name the FITS target.
````

````{grid-item-card} 5 · Solve
Identify the field, then click stars to assign Target, Comparison and Check.
````

````{grid-item-card} 6 · Watch
Read the comparisons as well as the target. A shift shared by everything is
weather.
````

````{grid-item-card} 7 · Hand off
Copy the session folder. Reduce it in [Siril](https://siril.org) and
`star_var_script`.
````
`````

## 1. The five screens

Everything below happens in one of five places, chosen from the sidebar on the
left. Learn these now and the rest of the guide has somewhere to hang.

| Screen | What it is for |
|---|---|
| **Connection** | A two-minute setup stop: find the Seestar, connect the four devices. |
| **Observe** | Where the night happens. The image, the camera controls, focus, the mount, the overlays and the live curve are all here, as movable docks. |
| **Plan** | Build and run the acquisition sequence. |
| **Review** | Post-session, offline. Curves, quality trends, frames, AAVSO export. |
| **Settings** | Observer, site, paths, telescope profile, catalogues. |

Two menus matter more than they look:

- **Field →** solving and the catalogue overlays.
- **Photometry →** *Photometry setup…* (choose the target, review the ensemble)
  and *Live curves & diagnostics…* (the curve window).

```{admonition} Prerequisites
:class: note

This guide assumes ARGOS and ASTAP are installed and linked — if not, do
{doc}`install` first; it takes twenty minutes and it is not a field task.

If a term goes past you, {doc}`glossary` defines every one this documentation
uses: AUID, check star, ensemble, zero point, green plane, TG, HFD and the rest.
```

## 2. Before you go out

Do this indoors, with internet. Catalogue lookups are cached, so what you
resolve now still works in the field with no connection.

**Settings → Observatory.** Enter the observer name and, for AAVSO submissions,
the observer code — left unset, every AAVSO export is stamped `XXX`. Search for
your site, pick the right result, correct the altitude if you have a measured
one, and save it as a favourite.

**Settings → Equipment · camera.** Select the telescope you actually own. The
S30 Pro is the reference profile; S30 and S50 are selectable but flagged
*unvalidated* — do not use them for precision photometry until their parameters
have been validated in the field.

**Settings → Files · application.** Choose the **sessions working folder**. FITS
frames, the session record and the measurements are written there.

**Settings → Catalogues · data.** Check the bundled essential catalogue and the
CDS / NASA / AAVSO cache paths. **Refresh** clears only the selected cache; the
next explicit search fetches it again if internet is available. Set the sequence
**presets** folder and the **camera calibration** folder (local PTC files) if you
use them — they are distinct from the catalogue caches and ARGOS never modifies
them on its own. Enable JSONL diagnostic logging only for a run you intend to
debug; those files stay in the session folder and are never transmitted.

**Plan ahead.** While you still have internet, search tonight's targets and
resolve the fields you will need.

```{admonition} Before you close the laptop
:class: tip

- [ ] Observer name **and** AAVSO observer code entered
- [ ] Site saved as a favourite, altitude corrected
- [ ] Telescope profile matches the telescope you own
- [ ] Sessions working folder chosen, with room for the night
- [ ] Tonight's targets resolved, so their coordinates are in the cache
- [ ] ASTAP executable *and* database both confirmed in Settings → Astrometry
```

## 3. Start the telescope in the Seestar app

Every night begins in the official Seestar app: power and initialise the
telescope, then complete its **GoTo pointing calibration** before connecting
ARGOS.

Do the initial focus there too. ARGOS's focus tools work and can refocus during
the session, but its autofocus is currently slower. ARGOS does not yet replace
the official app's startup procedure or its pointing calibration.

## 4. Connect

1. Select the telescope in **Telescope · equipment**.
2. Enter the Seestar's **IP address** and Alpaca port — usually `32323`, and
   `10.0.0.1` in Seestar access-point mode — or press **Discover**, which
   searches for the telescope and remembers the last address.
3. Press **Connect equipment**. Telescope, Camera, Filter Wheel and Focuser must
   all read *Ready*.
4. **Show connection and device details** exposes the individual device controls
   and the Stellarium server if you need to diagnose one of them.

Networks, tethering and offline use are covered in {doc}`field_connectivity`.

## 5. The order of the night

This is the sequence, and it is not obvious from the interface. Read it once
before your first session.

```text
Connection      connect · four devices Ready
     │
Observe         Shot          → one test frame, check focus and exposure
     │          Field → Identify field   → the frame is solved
     │          Photometry → Photometry setup…  → click the target
     │                                          → accept or pick comparisons
     │                                          → add a check star
     │
Plan            build the steps · Start sequence
     │
Observe         Photometry → Live curves & diagnostics…  → watch it run
     │
Review          open the session · read the quality trends · export
```

Two points worth stating plainly, because getting them wrong costs a night:

**Solve and assign roles *before* you press Start.** The photometry roles are
what makes a sequence produce a light curve. Frames taken before a target
exists are saved as FITS — nothing is lost — but they produce no measurements,
and they are not re-measured when you assign roles later.

**The frame you solve on comes from the `Shot` button.** In the Acquisition
dock on the Observe screen, set a type of *Light*, an exposure and a gain, and
press **Shot**. That is a single test frame: it is what you focus on, what you
check the exposure level on, and what you solve. The same button provides the
*pilot frame* that automatic comparison selection measures candidates on.

## 6. Plan the sequence

The **Plan** workspace deliberately separates search, plan parameters, altitude,
presets and run controls into movable panels; the step table stays in the centre.

1. The **Scientific source** panel accepts designations. Messier, NGC and IC resolve
   immediately from the bundled catalogue, tolerating lower case, optional
   spaces and common aliases. HD and other names go through CDS Sesame once,
   then cache locally.
2. The result fills the plan's name and coordinates. It does **not** slew:
   verify the target, then command the GoTo from the Observe telescope panel or
   point with Stellarium.
3. **Target visibility** plots altitude across the coming local night, with the
   dashed line at 30°. It is computed for the site in Settings.
4. Build the steps: frame type (Light / Dark / Flat / Bias), filter, exposure,
   gain, count and interval. The estimated duration is an estimate — leave
   margin for downloads and autofocus.
5. **Acquisition options** holds repeat, autofocus cadence and end action. The
   **Target name (FITS)** field is the scientific identifier written to `OBJECT`,
   into the filenames and into the photometry target set — it is *not* the
   session name. A source search fills it; for a manual Stellarium pointing,
   enter it yourself or accept a catalogue suggestion after checking it.
   **Presets** saves and reloads a plan as JSON.
6. **Start sequence** begins the run. Pause finishes the current frame then
   holds; Stop ends the run.
7. **Start** in **Sequence control** chooses when the run begins. The default,
   *Now*, starts at once, as before. You can instead hold the plan until a
   *local time* (date and time), or until the Visibility target climbs *above
   an altitude*. While it waits the table is locked, the panel counts down, and
   **Stop** cancels it with nothing exposed.
   - A time that has already passed asks whether to start now.
   - An altitude the target will not reach in the next 12 hours is refused up
     front.
   - *GoTo the target at start* (off by default) slews to the Visibility target
     and waits for the slew to finish plus a few seconds before the first frame.
     If the mount is offline, refuses the GoTo, or has not arrived within five
     minutes, the sequence is **not** started and the Activity log says why.
     The GoTo clears the previous plate solution, so the run arms auto-solve
     itself. The first frames are solved from the mount position, and the
     catalogue, saved photometry roles and live photometry then follow.
   - Preparing a transit sequence fills in the coverage start time. The mode is
     still *Now* until you choose otherwise.

```{tip}
Keep the acquisition settings stable through a time-series run. Changing
exposure or gain mid-run changes the instrumental system you are measuring in.
```

### What to actually put in those boxes

The step table asks for numbers and does not suggest any. Here is a defensible
starting point — **a starting point, not a recommendation for your target**.
Take a `Shot` and check it before committing the night.

| Setting | Start with | Why |
|---|---|---|
| **Filter** | **IR** | The IR-cut position is the broad one. `LP` narrows the passband, which changes what your instrumental green *is*; never mix the two in one run, and do not use `LP` for photometry unless you know why you are. |
| **Exposure** | 10–20 s | Long enough to be sky-limited rather than read-noise-limited; short enough that a mag-9 star does not saturate and that field rotation does not smear. ARGOS's own default is 10 s. |
| **Gain** | 80 | The shipped default. Higher gain buys read noise, not signal, and costs dynamic range. |
| **Count** | enough to cover the event | For a variable: at least a full cycle for a short-period star, or two to three hours of a longer one. Below ~30 frames the run-level scatter estimate does not exist and your error bars are photon-only. |
| **Interval** | 0 | Back-to-back. Download already costs several seconds per frame. |

```{admonition} Check the peak, not the picture
:class: important

The one thing to verify on your test `Shot`: click your target and read the
**peak ADU** on its star card. Too low and you are measuring noise; near the top
of the range and you are clipping the very star you came for. Adjust exposure
first, gain second.

Bear in mind that ARGOS's own saturation flag is not currently trustworthy — the
threshold is not reconciled with the sensor's full well
({doc}`differential_photometry` §3.5). Your eye on the peak value is the better
guard.
```

```{admonition} How long is enough?
:class: tip

Scintillation alone puts roughly **15 mmag** on a single 20-second sub through a
30 mm aperture at airmass 1.5, no matter how bright the star
({doc}`differential_photometry` §9). If the amplitude you are chasing is
0.3 mag, one frame sees it. If it is 0.02 mag, you need to average tens of
frames and you need the night to cooperate. Decide which case you are in before
you drive somewhere.
```

### Calibration frames — take them, ARGOS will not remind you

ARGOS applies no calibration. But the session folder has `darks/`, `flats/` and
`biases/` because **Siril will need them**, and the only chance to take them is
while you are still out there.

The filter wheel makes two of the three easy: its first position is a physical
**Dark** slot, so darks and biases need no cap and no cover.

| Frame | How | How many |
|---|---|---|
| **Darks** | Filter `Dark`, *same exposure and same gain as your lights*, same temperature — so take them at the end of the run, not at home | 15–20 |
| **Biases** | Filter `Dark`, shortest exposure the camera allows, same gain | 20–30 |
| **Flats** | Needs an even light source over the *objective*: twilight sky, or a panel. The telescope is sealed, so there is no other way in | 15–20, at half saturation |

```{note}
Flats are the awkward one on a Seestar, and if you skip them Siril will still
calibrate with darks and biases alone. That is worth doing: darks remove the hot
pixels that {doc}`differential_photometry` §3.5 describes faking a dip. Flats
remove the vignetting gradient — which matters most when your comparison stars
are far from your target, and is exactly why ARGOS keeps the ensemble within
25 arcmin in the first place.
```

## 7. Observe

**Observe** is the session workspace. Acquisition, Telescope, Focusing,
Image display, Image statistics, Focus diagnostics (HFD/FWHM), Activity log and
Differential photometry are docks: drag them by their title bar, stack them, or
detach them onto a second screen. The filter wheel has no dock of its own — the
filter is a selector inside **Acquisition**, and in the sequence rows.
**View → Reset Window Layout** restores the default arrangement at next start.

- **File → Open FITS image…** opens an existing frame for inspection and field
  identification.
- **Field → Identify field** solves the current image; the ASTAP options and the
  catalogue controls live under the same **Field** menu.
- In the Telescope panel, command the GoTo only after checking the name, the
  coordinates and the intended target.
- GoTo and Sync are refused when the mount's clock is more than 60 s from the
  computer's after ARGOS has set it — some Seestar firmware accepts the time
  and keeps its old one. The Activity log says by how many hours. Correct the
  date and time in the Seestar app, then reconnect.
- The context bar under the image shows the frame name and session progress;
  detailed statistics stay in their own panel.

```{important}
Display is not data. Debayer mode, stretch and channel are preview settings. The
FITS written to disk stays linear 16-bit CFA with `BAYERPAT='GRBG'`.
```

## 8. Choose the photometry roles

After a field identification, click a star in the image. What the card shows
depends on what the click matched: for a catalogue object, its identity, type,
magnitude range, period, AUID and RA/Dec; for a source matching no catalogue
entry, the measured quantities instead — FWHM, HFD, SNR and peak ADU. Drag its
title to move it; resize it from its bottom-right corner.

| Button | Scientific meaning |
|---|---|
| **Target** | The variable or transit host whose differential magnitude is measured. |
| **Comparison star** | A reference star that builds the ensemble zero point. |
| **Check star** | A star expected to be constant, measured against the ensemble to expose its drift. |
| **Remove** | Takes this star out of the saved set. |
| **Dismiss** | Closes the card only; roles already assigned are kept. |

**Check star** does not check the focus — it assigns a scientific role. The role
buttons stay disabled until the field is solved and the star has reliable
coordinates: a wrong WCS gives a wrong identity.

The recommended flow is: solve the field, choose a target, let ARGOS propose
comparisons or pick them explicitly, then add a check star.

```{admonition} How many comparison stars? Aim for five.
:class: important

Three different numbers appear in the settings and they mean three different
things:

| | |
|---|---|
| **2** (`min_comparisons`) | Below this a measurement is *annotated*, not hidden. It is a floor, not a target. |
| **3** | Fewer than three *stable* comparisons and the ensemble reports **not ready** in the Comparison quality panel. |
| **5** (`auto_comparisons`) | What the automatic proposal will add for you, and the right thing to aim for. |

Two comparisons is a bad ensemble: the outlier rejection of
{doc}`differential_photometry` §4.2 needs three to work at all, and with one the
error bar is meaningless. Take the five ARGOS offers unless you have a reason
not to.

One more thing the interface will not tell you: **match their colour to your
target.** The zero point differences a catalogue *V* magnitude against an
instrumental green one, so a colour mismatch drifts through the night looking
like variability. VSP publishes B and V for every sequence star — keep the
ensemble within about 0.3 mag of the target in $B-V$.
```

A measurement with fewer comparisons than the floor is flagged as fragile rather
than hidden. How the ensemble is built, clipped and
propagated into an uncertainty is described in {doc}`differential_photometry`.

Selecting stars in a crowded field, catalogue depth and what an unmatched source
means are covered in {doc}`field_identification`. For a transit, see
{doc}`exoplanet_transits`.

## 9. Read the live curve

The **Field photometry** window — **Photometry → Live curves & diagnostics…** —
contains:

- **Light curve** — target and check star on the scientific plot; comparisons on
  their own diagnostic plot, labelled *Δmag from own median*. **Show
  uncertainties** toggles the error bars without changing the data.
- **Variable stars** — the variables identified in the field.
- **Metrics** — airmass, FWHM, sky background, HFD, star count and sensor
  temperature.
- **Targets** and **Comparison stars** — the set currently in use.
- **Aperture · references** — the measurement geometry and the VSP references.
- **Export measurements…** — a CSV of target, check and comparisons that
  preserves the roles and can be reopened in Review.
- **Export target (AAVSO)…** — the scientific target only.

```{warning}
Comparison curves are diagnostics. Never submit one as an AAVSO observation.
```

```{admonition} Reading the curve honestly
:class: important

The error bars include a run-level systematic floor measured from the curve
itself, so they are not photon-noise-only optimism — but the measurement is
still on **uncalibrated** sub-exposures in an instrumental band. A dip you can
see is worth reducing properly; a dip at the level of the scatter is not a
detection. The method, and its limits, are in {doc}`differential_photometry`.
```

A shift common to the target *and* the comparisons means cloud, tracking, focus
or saturation — not variability. That is what the comparison diagnostic plot is
for.

## 10. What ends up on disk

A sequence is organised for Siril under the configured sessions folder:

```text
<sessions>/
└── <timestamp>_<OBJECT>/
    ├── lights/
    ├── darks/
    ├── flats/
    ├── biases/
    ├── session.json
    ├── photometry_quality.json
    └── diagnostics/
```

`photometry_quality.json` is the leave-one-out assessment of the comparison
ensemble described in {doc}`differential_photometry`.

Live measurements and target sets are also kept under `<sessions>/targets/`.
When you report an observation, keep the raw FITS, `session.json`, the CSV
exports and any `*_diagnostics.jsonl` together.

Copy the whole session folder before reducing it in
[Siril](https://siril.org) and
[`star_var_script`](https://github.com/jperret21/star_var_script) — the folder layout is already
the one Siril's sequence discovery expects, so a copy is all it takes. The
naming rules and the full header list are in {doc}`fits_headers`.

## 11. Review a finished session

1. Select **Review** in the sidebar, then **Open session…**, and choose the
   folder that contains `session.json` — not the `lights/` subfolder.
2. Check the frame count, the filters and the warnings, then the FWHM, HFD,
   sky-background and temperature trends. These describe the ARGOS session; they
   do not replace a scientific reduction. Review's docks are Source light curve,
   Frame quality — FWHM, Comparison stars, Comparison quality, Frames, Session
   metadata and AAVSO export.
3. **Comparison quality** reports the latest leave-one-out assessment: one row
   per comparison with its sample count, scatter, median formal error and a live
   status of *Stable*, *Noisy* or *Unstable*. Select a row to show its curve. A
   star that never leaves *Waiting for 10 points* simply has too few usable
   measurements to judge.
4. The curves shown are the differential previews recorded during acquisition.
   Hover a point for its values; select it and press **Open source frame** to
   open that FITS in the viewer. The **Frames** tab gives the same access by
   double-click, with astrometry and full inspection available there. Nothing is
   written back to the raw frames.

Review works with no telescope connected. Set the AAVSO observer code in
Settings before exporting; `XXX` means it is missing.

## 12. Can I submit this to the AAVSO?

The documentation warns you repeatedly not to treat a live curve as a
publishable measurement, and then offers an **Export target (AAVSO)…** button.
Both are right, and here is how they fit together.

**What the export is.** An AAVSO Extended File Format file: your observer code,
one row per point, the differential magnitude and its uncertainty, `JD_UTC` as
the time (which is what AAVSO asks for), the band `TG`, `CNAME=ENSEMBLE`, and
`TRANS=NO` — an explicit declaration that the magnitudes are untransformed. Only
curves whose role is *target* are written: a comparison or check curve can never
leave through this door as an observation.

Your check star, if you chose one, goes into `KNAME`/`KMAG`: its AUID and its
ensemble magnitude on the same frame, which is what the AAVSO asks for with
ensemble photometry. `CHART` names the VSP chart the comparison stars came
from, so the AAVSO knows which sequence you used; it stays `na` when the
ensemble includes a star picked by hand, or stars from two different charts.
Comparison stars saved before this version carry no chart, so pick them again
to fill `CHART`.

**What it is not.** Calibrated. There is no dark, no flat, no bias and no colour
transformation, the zero point is cross-band, and the uncertainties do not
contain slow systematics. See {doc}`differential_photometry` §10 for the full
list.

```{admonition} The honest answer
:class: important

**Do not submit the live export as your observation of record.** Treat it as
what it is: a field check that the night worked, a preview to see whether the
star did anything, and a well-formed template of your own metadata.

The submission comes from the *reduction* — calibrate and register the raw
frames you kept, re-do the photometry there, and submit that. The raw frames are
the deliverable of an ARGOS night; the curve is the reason you know the night
was worth reducing.

If you do submit an ARGOS-derived measurement, describe how it was obtained:
uncalibrated sub-exposures, untransformed TG against V sequence magnitudes.
ARGOS writes `na` into the `NOTES` column, so you have to add that yourself
before uploading. An observation with an honest note is useful. An uncalibrated
observation presented as a calibrated one is worse than none.
```

Practical submission is through the AAVSO's own
[WebObs](https://www.aavso.org/webobs) upload; the file format is documented
under [Extended File Format](https://www.aavso.org/aavso-extended-file-format).
Neither is part of ARGOS.

## 13. Reporting a problem

Start with **More → Create local support bundle…**. The ZIP is created locally,
at your request. It contains redacted technical logs and, if you choose, the
session's JSON/CSV/JSONL metadata. It never includes raw FITS frames, the
observer's identity, coordinates or network address, and ARGOS never transmits
it. Check its contents before sharing it.

A useful report names the exact version, the platform, what you did, what you
expected and what happened — with the log excerpt around the failure.

```{seealso}
- {doc}`field_identification` — working in a crowded field, catalogue depth,
  and what an unmatched source means.
- {doc}`exoplanet_transits` — what a transit run needs that a variable-star run
  does not.
- {doc}`field_connectivity` — networks, tethering, and observing with no
  internet at all.
- {doc}`glossary` — every term used here, defined once.
- {doc}`references` — every catalogue, standard and formula ARGOS relies on,
  with links.
```
