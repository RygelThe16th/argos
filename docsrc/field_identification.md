# Identify a solved field

ARGOS separates **plate solving** from **catalogue identification**. This is
important: ASTAP first establishes a mapping between image pixels and sky
coordinates. Only then can ARGOS query, cache and project catalogue objects
onto the frame.

Field identification is an observing aid. It makes the contents of a frame
inspectable and lets the observer retain a target, comparison ensemble and
check star with the session. It does not turn a live frame into a calibrated
measurement or prove that every visible source has a catalogue identity.

## 1. Solve before assigning a role

With a representative full-resolution light frame open, use **Field → Identify
field**. ARGOS invokes the configured ASTAP executable and reads the returned
WCS. Verify the solved centre, plate scale, rotation and overlays against the
image before selecting a target or comparison star.

If markers do not line up with stellar centroids, stop and solve that problem
first. Changing a display stretch, CFA colour mode or zoom must not be used to
"correct" a mismatch: those are display operations, while a solution error is
a coordinate error.

## 2. Control what is drawn

The overlay controls deliberately distinguish layers from selected roles:

The bar has two rows: the object layers, then the display controls.

| Object layer | What it draws |
|---|---|
| **Stars** | [Gaia DR3](https://www.cosmos.esa.int/web/gaia/dr3) stars, enriched with conventional [SIMBAD](https://simbad.cds.unistra.fr/) identities when available. |
| **Variables** | Variable stars from the [AAVSO VSX](https://vsx.aavso.org/) catalogue. |
| **Galaxies** | Galaxies identified by SIMBAD or the bundled Messier/NGC/IC catalogue. |
| **Nebulae + clusters** | Nebulae and stellar clusters from SIMBAD or the bundled Messier/NGC/IC catalogue. |
| **Exoplanets** | Confirmed exoplanet hosts returned by the [NASA Exoplanet Archive](https://exoplanetarchive.ipac.caltech.edu/), cache first. The marker identifies the host, not a resolved planet. |
| **Other objects** | Other physically classified SIMBAD sources, such as radio or X-ray sources. |

| Display control | What it does |
|---|---|
| **Coordinate grid** | RA/Dec grid derived from the solved WCS. |
| **VSP references** | [AAVSO VSP](https://app.aavso.org/vsp/) reference candidates — not necessarily the ensemble you selected. |
| **Selected stars** | The target, selected comparison stars and check stars for this observation. |
| **Labels** | Compact non-overlapping labels. Keep this off in crowded fields and hover or click a marker for its full identity. |

The **faint-limit** control is a catalogue/display budget, not a detection
threshold. A higher magnitude limit can return many more objects, increase the
time needed to identify a field, and make labels unreadable. The maximum object
limit in **Settings → Catalogues · data** constrains the query/rendering budget
for this reason. Start with a useful depth, then increase it only when a source
of interest is still missing.

Filtering one layer does not remove data from the session or alter the FITS.
It only changes what is displayed over the current image.

```{admonition} Not every magnitude on screen is a photometric magnitude
:class: warning

The **Stars** layer shows Gaia $G$ — a very broad white-light band, and *not*
$V$. It is there so you can tell a 12th-magnitude star from a 16th, and it is
excellent for that. It is not what builds the zero point.

Only the **VSP references** carry sequence magnitudes calibrated by the AAVSO
in a standard band, with an AUID. That is why a manually clicked star can be
kept as a comparison and still contribute nothing to the ensemble: it has a
position and a flux, but no calibrated magnitude to difference against. See
[§4 of the method page](differential_photometry.md#4-the-comparison-ensemble).
```

## 3. Catalogue coverage and identity

ARGOS combines an embedded essential object catalogue with local caches and
optional network requests. The exact result depends on the solved footprint,
selected layers, faint limit, object budget and whether the requested service
has cached data for that field.

An uncircled star is not necessarily a failed detection. It may be fainter than
the selected limit, absent from the chosen catalogue, excluded by the object
budget, not returned by an optional service, or too close to another source for
a reliable projected match. Conversely, an annotated name is a catalogue
cross-match, not an assertion about photometric suitability.

Click a marker for the information card. When available it shows a concise
catalogue identity, coordinates, catalogue magnitude(s) and image measurements
such as FWHM/HFD, SNR or saturation. Long identifiers are shortened in the
overlay; the card is the authoritative place to inspect the full identity.

Catalogue requests are explicit. ARGOS can use the network for optional
lookups, but the resulting cache is local and can be inspected or refreshed in
Settings. An offline session uses the embedded catalogue and whatever matching
entries have already been cached; it does not silently upload frames or
telemetry.

## 4. Select the photometry roles

Once the WCS and markers are credible, click an object and assign one role:

| Role | Purpose |
|---|---|
| **Target** | Science source whose differential signal is followed. |
| **Comparison star** | Reference star contributing to the ensemble zero point. |
| **Check star** | A source expected to remain constant, measured against the ensemble as a diagnostic. |

ARGOS may propose comparison candidates after a target has been selected. Treat
this as a ranked starting point, not as a scientific endorsement. Inspect
saturation, isolation, brightness, colour and the future post-processing
requirements; keep the exact selected ensemble recorded with the session.

The same target or comparison selection is visible in the photometry tables and
in the **Selected stars** overlay. Selecting or removing a row updates the image
overlay so the table and the field do not become two conflicting views of the
session.

## 5. During a sequence

The sequence continues to retain field and measurement context on incoming
frames. A live curve or new overlay should be interpreted as an operational
check: common shifts can be caused by clouds, focus, tracking, saturation or a
changing sky background. Use Review to follow a suspicious curve point back to
its source FITS frame.

### Re-centering between frames (opt-in)

An unguided mount drifts. On an EQ wedge one observed run walked about 11′ an
hour, until the comparison stars left the frame after roughly 80 minutes.
**Field → Configure re-centering…** can pull the field back during a sequence.
It is **off by default**.

- **What is held.** ARGOS holds the photometry target at the pixel position it
  had on the first measured frame of the run. That is your framing, not the
  sensor centre. A dither moves that position by the measured dither offset.
- **How drift is measured.** The target is projected through the tracked WCS on
  every photometry frame, so a failed ASTAP solve does not stop the measurement.
- **When it corrects.** Choose either *when the drift passes a threshold* (green
  pixels) or *every N frames* (skipped inside a small deadband).
- **How it moves.** The mount is nudged with MoveAxis pulses **between
  exposures only**. A correction decided on one frame runs after the exposure
  already in progress, then waits the settle time.
- **Calibration.** The first correction calibrates the mount. ARGOS sends one
  short pulse per axis and measures how far the field actually moved, by phase
  correlation of the frames on either side of the move. Nominal rates are not
  trusted: a dither nominally ±180″ moved the field only ~18 px on the Seestar.
- **Each move is measured.** The measured shift is fed to the aperture tracker,
  so the apertures stay on their stars across the jump. It also keeps the
  mount's new reported position from being taken for an external slew.
- **Safety.** One correction is capped (*Largest single correction*).
  Re-centering switches itself off with a warning in two cases: the
  calibration cannot steer both axes, or two corrections in a row leave the
  drift no smaller. It also needs `photometry.track_apertures`.
- **What is recorded.** The Activity log records each decision, each pulse and
  the residual drift after a correction. It also names the first frame after
  every move. `session.json` marks that frame with `"mount_move": "recenter"`
  (or `"calibrate"`).

This changes a principle stated for the manual **Center target** button: a
background plate solve does not normally move the telescope. Leave it off unless
you will watch the first corrections.

ARGOS does not calibrate raw lights with darks, flats or bias frames during this
quick-look identification/measurement path. Keep all raw calibration frames and
perform the final calibrated reduction independently.

## 6. Troubleshooting

| Symptom | First check |
|---|---|
| Marker is not centred on a star | Confirm ASTAP's centre, scale and rotation on a full-resolution frame. |
| Needed source is absent | Check the relevant layer, faint limit and object budget; then check cache/network availability. |
| Labels fill the frame | Hide labels, reduce the faint limit or narrow the layers; inspect individual markers instead. |
| Too few comparison candidates | Confirm the solve and catalogue filters, then select candidates manually and record the rationale for final reduction. |
| Same object has several names | Use the information card's full identity and coordinates; overlay labels are intentionally abbreviated. |

```{seealso}
- {doc}`guide` — the observer-oriented version of this workflow, in the order
  you actually do it.
- {doc}`differential_photometry` — what happens to a star once you have given
  it a role.
- {doc}`references` — every catalogue named on this page, with its citation and
  its acknowledgement requirements.
```
