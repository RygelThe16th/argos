# Notes on this fork

This fork of [jperret21/argos](https://github.com/jperret21/argos) carries
fixes and features found while observing variable stars with a Seestar S30 Pro
on Ubuntu. None of them is in the upstream project yet. Each one lives on its
own branch, starting from `main` (upstream 0.4.2), so it can become a separate
pull request. `local-test` merges them all for everyday use.

**Status:** each branch has tests, and the full suite passes on `local-test`.
The tooltip, comparison-exclusion and VSX-fallback fixes have been used on real
sessions. Closer comparisons were checked by replaying a real session's pilot
frame. Re-centering and scheduled start still need nights on a real Seestar;
see their notes below.

## Try everything at once

```bash
git clone -b local-test https://github.com/RygelThe16th/argos.git
cd argos
./run.sh
```

`./run.sh` needs [uv](https://docs.astral.sh/uv/), and plate solving needs
[ASTAP](https://www.hnsky.org/astap.htm) as described in the
[README](README.md#install). If the packaged ARGOS (`.deb`/`.dmg`) is also
installed, do not run both at the same time.

To run the tests:

```bash
uv sync --extra dev
uv run --extra dev pytest -q
```

## Branches

| Branch | What it does | Default |
| --- | --- | --- |
| [`fix-tooltip-html-escape`](#fix-tooltip-html-escape) | Marker tooltips show `'` and `"` instead of `&#x27;` / `&quot;` | always on |
| [`fix-comp-star-exclusion`](#fix-comp-star-exclusion) | Automatic comparisons skip stars blended with the target or near a frame edge | always on |
| [`feat-closer-comparisons`](#feat-closer-comparisons) | Automatic comparisons prefer nearby stars about as bright as the target | always on |
| [`fix-resolver-vsx-fallback`](#fix-resolver-vsx-fallback) | Object lookup falls back to AAVSO VSX when CDS is down | always on |
| [`feat-auto-recenter`](#feat-auto-recenter) | Pulls the field back between exposures as the mount drifts | **off** |
| [`feat-scheduled-start`](#feat-scheduled-start) | Starts a sequence at a local time or target altitude, with optional GoTo | **Now** (as before) |
| `local-test` | All of the above merged | |

`feat-closer-comparisons` is built on `fix-comp-star-exclusion`, so it contains
both. The other branches are independent of each other.

### fix-tooltip-html-escape

Hovering a catalogue marker showed escaped text such as
`Dec +44°39&#x27;23.1&quot;`. The tooltip is now rendered as rich text: names
are still escaped, so a catalogue entry cannot inject markup, but `'`, `"` and
aligned columns display correctly. This affects every marker layer.

### fix-comp-star-exclusion

The automatic comparison proposal (from the AAVSO VSP chart) only checked that
a star was inside the frame. On GZ And it picked a star 11″ from the target,
inside the target's aperture and annulus. Measured on the pilot frame, such a
blend looks exactly as good as the target, so it ranked first.

- Candidates closer than *annulus outer radius + aperture radius* to the
  target, or to another comparison, are rejected.
- Candidates closer than *annulus outer radius + edge margin* to a frame edge
  are rejected; an unguided Seestar drifts about 90 green px per hour.
  The margin is `photometry.comparison_edge_margin_px` (default 100 green px),
  set in the Photometry setup panel.
- Every rejected star is logged with its reason. Manual comparison and check
  stars that break these rules, or are known variables, get a warning but are
  kept.
- The comparison table shows each star's separation from the target and its
  distance to the frame edge.

### feat-closer-comparisons

With a faint target, the fixed pilot SNR ≥ 10 gate rejected every reference as
faint as the target. On V1071 Per (target SNR 6.6) it dropped a star 9.8′ away
and within 0.06 mag, and kept stars 1.2–1.5 mag brighter and 11–23′ away. A
nearby reference shares the target's thin cloud and extinction, so it gives a
cleaner differential light curve.

- The SNR gate drops to 0.8 × the target's own pilot SNR when that is lower
  than `comparison_min_snr`.
- Proximity takes 25 % of the ranking score, falling to zero at 30′.

New settings in `photometry`:

| Key | Default | Meaning |
| --- | --- | --- |
| `comparison_min_snr_target_fraction` | `0.8` | SNR gate as a fraction of the target's SNR |
| `comparison_distance_weight` | `0.25` | Share of the score given to proximity (0 = old behaviour) |
| `comparison_distance_scale_arcmin` | `30.0` | Separation at which the proximity score reaches zero |

### fix-resolver-vsx-fallback

When CDS Sesame was unreachable (an outage on 5 November 2026), every lookup in
**Scientific source** failed with *Catalogue unavailable*. Lookup now tries
[AAVSO VSX](https://vsx.aavso.org/) before giving up, so variable stars still
resolve. Confirmed variables get type `V*` and remain photometry targets;
suspected ones can be pointed at but are not photometry targets. Results are
cached as before. While CDS hangs, each new lookup waits for its timeout
(about 15 s) before VSX answers.

### feat-auto-recenter

An unguided mount drifts: one EQ-wedge run walked about 11′ an hour, until the
comparison stars left the frame after roughly 80 minutes. Turn this on in
**Field → Configure re-centering…**.

- ARGOS holds the photometry target where it was on the first measured frame
  (your framing, not the sensor centre), measuring the drift on every
  photometry frame through the tracked WCS.
- It corrects either when the drift passes a threshold, or every N frames
  (skipped inside a small deadband).
- The mount is nudged with MoveAxis pulses **between exposures only**, then
  waits a settle time. Tests check that the mount never moves while the
  camera is exposing.
- The first correction calibrates each axis with one short pulse and measures
  how far the field really moved (by phase correlation). Nominal rates are not
  trusted: a nominal ±180″ dither moved the field only about 18 px.
- Every move is measured the same way and passed to the aperture tracker, so
  the apertures stay on their stars across the jump.
- Safety: each correction is capped, and re-centering turns itself off with a
  warning if calibration cannot steer both axes or two corrections in a row
  do not reduce the drift. It needs `photometry.track_apertures`.
- The Activity log records each decision, pulse and residual, and
  `session.json` marks the first frame after each move with
  `"mount_move": "recenter"` (or `"calibrate"`).
- Phase correlation ignores the sensor's fixed pattern (hot pixels, banding).
  Without that, frames under thin cloud reported a confident zero shift.

The dialog sets the mode, threshold, N, deadband, largest single correction
and settle time. The pulse rate and lengths (`recenter.rate_deg_s`,
`calibration_pulse_s`, `max_pulse_s`) keep their defaults unless edited in the
config file.

**Still to check:** corrections on a real Seestar over a whole night. In the
dither data used for testing, one axis responded weakly. Watch the first
corrections in the Activity log.

### feat-scheduled-start

**Start** in **Sequence control** can hold a plan until a local date and time,
or until the Visibility target climbs above an altitude.

- *Now* is the default and works as before.
- While waiting, the table is locked and the panel counts down. **Stop**
  cancels, and nothing is exposed.
- A time already passed asks whether to start now. An altitude the target will
  not reach in the next 12 hours is refused, showing its peak.
- *GoTo the target at start* (off by default) slews first and waits for the
  slew plus a few seconds. If the mount is offline, refuses, or has not
  arrived within five minutes, the run does not start and the Activity log
  says why. The run then plate-solves from the mount position, so the
  catalogue, saved photometry roles and live photometry follow.

**Still to check:** an unattended start on a real Seestar.

## Keeping up with upstream

```bash
git remote add upstream https://github.com/jperret21/argos.git
git fetch upstream
git diff upstream/main local-test --stat   # everything this fork changes
```
