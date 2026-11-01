# References and further reading

Everything ARGOS relies on, and everything it hands off to. Each entry says
*what ARGOS actually uses it for*, so you can check a claim against its source
rather than take this documentation's word for it.

```{note}
Where a method is cited below, ARGOS implements the **published formula**, not
the published software. The implementation and its constants are stated in
{doc}`differential_photometry`; the citation tells you where the formula comes
from.
```

(companion-software)=
## Companion software

::::::{grid} 1 1 2 2
:gutter: 2

:::::{grid-item-card} ASTAP
Plate solving. ARGOS shells out to the ASTAP executable and one of its star
databases (two separate downloads, both required) to recover a WCS from the
green plane.

**Han Kleijn** — <https://www.hnsky.org/astap.htm>
:::::

:::::{grid-item-card} Siril
Calibration, registration and stacking of the raw frames ARGOS preserves. This
is where a quick-look curve becomes a reducible dataset.

**Free-Astro team**, GPLv3 — <https://siril.org>
:::::

:::::{grid-item-card} `star_var_script`
The companion reduction script: a Siril script for automatic variable-star
study with the Seestar S30 Pro. ARGOS's run-level uncertainty floor is
deliberately the same model, so the live preview and the final reduction quote
comparable error bars.

**Jules Perret** — <https://github.com/jperret21/star_var_script>
:::::

:::::{grid-item-card} Stellarium
Optional pointing client. ARGOS exposes the Stellarium Telescope Protocol on
TCP 10001, so you can slew by clicking in a planetarium.

**Stellarium team**, GPLv2 — <https://stellarium.org>
:::::
::::::

(standards-and-protocols)=
## Standards and protocols

**ASCOM Alpaca** — the REST/JSON device interface ARGOS speaks to the Seestar
(port 32323) and to the simulator. Telescope, Camera, FilterWheel and Focuser
are the four device types used.
ASCOM Initiative — <https://ascom-standards.org/api/> ·
[developer documentation](https://ascom-standards.org/AlpacaDeveloper/Index.htm)

**FITS 3.0** — the file format ARGOS writes: 16-bit unsigned, linear, still in
its Bayer mosaic. The keyword conventions used are catalogued in
{doc}`fits_headers`.
Pence, W. D., Chiappetti, L., Page, C. G., Shaw, R. A., & Stobie, E. 2010,
*A&A*, **524**, A42 — [doi:10.1051/0004-6361/201015362](https://doi.org/10.1051/0004-6361/201015362)

**AAVSO Extended File Format** — the submission format ARGOS's target export
produces. The observer code goes in Settings; left unset, exports are stamped
`XXX`.
AAVSO — <https://www.aavso.org/aavso-extended-file-format>

**TG — the untransformed green band.** ARGOS measures the green plane of the
CFA mosaic and reports it as `TG`, the AAVSO code for an untransformed green
channel. It is *not* Johnson $V$, and no colour transformation is applied.
AAVSO, *DSLR Observing Manual* —
<https://www.aavso.org/dslr-observing-manual>

(catalogues-and-services)=
## Catalogues and services

Queried live when there is internet, then cached locally so the field still
works offline. Cache locations are set in **Settings → Catalogues · data**.

| Source | Used for | Endpoint / citation |
|---|---|---|
| **AAVSO VSX** | Variable-star identity, type, period, magnitude range | <https://vsx.aavso.org/index.php> · Watson, Henden & Price 2006, *SASS*, **25**, 47 |
| **AAVSO VSP** | Comparison-star sequences with AUIDs and calibrated magnitudes | <https://app.aavso.org/vsp/> |
| **Gaia DR3** | Field stars for overlays and identification | ESA TAP service · Gaia Collaboration, Vallenari et al. 2023, *A&A*, **674**, A1 — [doi:10.1051/0004-6361/202243940](https://doi.org/10.1051/0004-6361/202243940) |
| **SIMBAD** | Deep-sky and stellar identity, cross-identifications | <https://simbad.cds.unistra.fr/> · Wenger et al. 2000, *A&AS*, **143**, 9 |
| **CDS Sesame** | Name → coordinates resolution for arbitrary designations | <https://cds.unistra.fr/cgi-bin/nph-sesame/> |
| **NASA Exoplanet Archive** | Transit ephemerides, depth and duration | TAP service · Akeson et al. 2013, *PASP*, **125**, 989 — [doi:10.1086/672273](https://doi.org/10.1086/672273) |

```{admonition} Acknowledging the catalogues
:class: tip

If an ARGOS session leads to a published result, the catalogues carry their own
acknowledgement requirements — VSX and VSP through the AAVSO, Gaia through the
ESA/DPAC statement, SIMBAD and Sesame through CDS, and the Exoplanet Archive
through NASA/IPAC. ARGOS does not do this for you.
```

(methods-and-formulae)=
## Methods and formulae

Each of these is implemented in {mod}`argos.core.photometry` or
{mod}`argos.core.imaging` and is written out, with its constants, in
{doc}`differential_photometry`.

**The CCD equation.** The uncertainty budget of a single aperture measurement:
source photon noise, sky photon noise and read noise, each summed over the
aperture pixels. Implemented in {mod}`argos.core.photometry.aperture`; see
[§3.3](differential_photometry.md#33-noise).
Merline, W. J. & Howell, S. B. 1995, *Experimental Astronomy*, **6**, 163 —
[doi:10.1007/BF00421131](https://doi.org/10.1007/BF00421131) ·
Howell, S. B. 2006, *Handbook of CCD Astronomy*, 2nd ed., Cambridge University
Press.

**Aperture size and growth curves.** Why a 2.5×FWHM radius is not the
SNR-optimal choice, and what it buys instead.
Howell, S. B. 1989, *PASP*, **101**, 616 —
[doi:10.1086/132477](https://doi.org/10.1086/132477) ·
Naylor, T. 1998, *MNRAS*, **296**, 339 —
[doi:10.1046/j.1365-8711.1998.01314.x](https://doi.org/10.1046/j.1365-8711.1998.01314.x)

**MAD as a robust scale estimator.** The factor **1.4826** that converts a
median absolute deviation into a Gaussian-equivalent standard deviation. It
appears three times in ARGOS: rejecting a bad comparison star, estimating the
run-level scatter, and setting the star-detection threshold. The consistency
constant is standard robust-statistics material (Hampel 1974; Huber 1981);
Rousseeuw & Croux is cited here for the *properties* of the estimator, and
argues for Sₙ/Qₙ **instead of** MAD.
Rousseeuw, P. J. & Croux, C. 1993, *JASA*, **88**, 1273 —
[doi:10.1080/01621459.1993.10476408](https://doi.org/10.1080/01621459.1993.10476408)

```{important}
The MAD has ~37 % Gaussian efficiency. With the 10–30 points a short run
provides, any scatter ARGOS estimates through a MAD — including the systematic
floor of {doc}`differential_photometry` §5 — is itself uncertain at the
±20–30 % level. Treat it as an order-of-magnitude floor, not a calibration.
```

**Successive-difference variance estimation.** The observed scatter of a light
curve is estimated from *second* differences, whose variance is $6\sigma^2$ for
independent errors. Differencing removes any locally linear trend, which is what
makes the estimate usable on a genuinely variable star.
von Neumann, J. 1941, *Annals of Mathematical Statistics*, **12**, 367 —
[doi:10.1214/aoms/1177731677](https://doi.org/10.1214/aoms/1177731677)

**Correlated ("red") noise in time-series photometry.** Why the point-to-point
scatter of a light curve *understates* the uncertainty on anything measured over
a longer timescale — a transit depth above all — and the binned-RMS / β-factor
diagnostic that exposes it.
Pont, F., Zucker, S., & Queloz, D. 2006, *MNRAS*, **373**, 231 —
[doi:10.1111/j.1365-2966.2006.11012.x](https://doi.org/10.1111/j.1365-2966.2006.11012.x)

**Scintillation.** The atmospheric noise floor that no amount of exposure on a
small aperture removes, and which dominates bright-star photometry through a
30 mm objective.
Young, A. T. 1967, *AJ*, **72**, 747 —
[doi:10.1086/110303](https://doi.org/10.1086/110303) ·
Osborn, J., Föhring, D., Dhillon, V. S., & Wilson, R. W. 2015, *MNRAS*,
**452**, 1707 —
[doi:10.1093/mnras/stv1400](https://doi.org/10.1093/mnras/stv1400)

**Ensemble differential photometry.** The idea of building a zero point from
many comparison stars rather than one.
Honeycutt, R. K. 1992, *PASP*, **104**, 435 —
[doi:10.1086/133015](https://doi.org/10.1086/133015)

```{warning}
ARGOS does **not** implement Honeycutt's method. Honeycutt solves a global
least-squares system over a whole inhomogeneous set of exposures; ARGOS forms a
clipped mean zero point independently on each frame, which is what a live
preview can do. The citation is context, not attribution.
```

**Airmass.** Pickering's formula, accurate to better than 0.01 airmass down to
1° altitude and finite at the horizon, unlike a naive $\sec z$. The same
function fills the FITS `AIRMASS` header and the light-curve metric, so the two
cannot disagree — see {mod}`argos.core.imaging.sky_geometry`.
Pickering, K. A. 2002, *DIO*, **12**, 20 —
<https://www.dioi.org/vols/wc0.pdf>

**Julian date.** The Fliegel–Van Flandern integer day-number algorithm, used for
the fast per-frame JD; `BJD_TDB` is delegated to astropy.
Fliegel, H. F. & Van Flandern, T. C. 1968, *Communications of the ACM*, **11**,
657 — [doi:10.1145/364096.364097](https://doi.org/10.1145/364096.364097)

**Barycentric Julian Date in TDB.** Why `BJD_TDB` — and not `HJD`, and not
`JD_UTC` — is the time standard a transit or a period study should be reported
in.
Eastman, J., Siverd, R., & Gaudi, B. S. 2010, *PASP*, **122**, 935 —
[doi:10.1086/655938](https://doi.org/10.1086/655938)

**Half-flux diameter (HFD).** The focus metric ARGOS samples during an autofocus
sweep and writes to the `HFD` header: the diameter enclosing half the star's
flux. It is better behaved than FWHM far from focus, which is exactly where an
autofocus routine has to work.
Weber, L. & Brady, S. 2001, *Fast Auto-Focus Method and Software for CCD-based
Telescopes* — the whitepaper behind FocusMax; the publisher's copy is gone, so
this links to the Internet Archive:
[archived PDF](https://web.archive.org/web/20201029012548/https://www.ccdware.com/Files/ITS%20Paper.pdf)

```{admonition} What ARGOS does *not* implement
:class: note

Star detection is a MAD-based threshold followed by 3×3 local maxima — it is
**not** DAOFIND ([Stetson 1987, *PASP*, **99**, 191](https://doi.org/10.1086/131977))
and does not fit a PSF. It exists to count stars,
measure focus and let you click on one; it is not a source-extraction pipeline.
Photometry is aperture photometry only: no PSF fitting, no deblending, no
crowded-field solution.
```

(scientific-context)=
## Scientific context

Background reading for the observing side rather than the code.

**AAVSO Guide to CCD/CMOS Photometry** — the practical reference for comparison
star choice, ensemble construction and what makes an observation submittable.
<https://www.aavso.org/ccd-photometry-guide>

**AAVSO Manual for Visual Observing / VSX target selection** — how to find a
variable worth an evening.
<https://www.aavso.org/observing>

**Exoplanet Transit Database (ETD)**, now part of the VarAstro portal —
amateur transit timing, and a realistic picture of what depth is detectable with
what aperture.
<https://var2.astro.cz/ETD>

**AstroImageJ** — the reference free tool for transit photometry and fitting.
Useful as a cross-check on any ARGOS transit.
Collins, K. A., Kielkopf, J. F., Stassun, K. G., & Hessman, F. V. 2017, *AJ*,
**153**, 77 — [doi:10.3847/1538-3881/153/2/77](https://doi.org/10.3847/1538-3881/153/2/77)

(python-libraries)=
## Python libraries

ARGOS's runtime dependencies, in the order they matter scientifically.

**Astropy** — FITS I/O, WCS, coordinate frames, time scales and the `BJD_TDB`
correction.
Astropy Collaboration 2013, *A&A*, **558**, A33 ·
2018, *AJ*, **156**, 123 ·
2022, *ApJ*, **935**, 167 — <https://www.astropy.org>

**NumPy** — every array operation in the imaging and photometry paths.
Harris, C. R. et al. 2020, *Nature*, **585**, 357 —
[doi:10.1038/s41586-020-2649-2](https://doi.org/10.1038/s41586-020-2649-2)

**Alpyca** — the Python ASCOM Alpaca client library.
<https://github.com/ASCOMInitiative/alpyca>

**PyQt6 / Qt 6** — the desktop interface. Confined to `argos/ui` and
`argos/workers`; see {doc}`ARCHITECTURE`.
<https://www.riverbankcomputing.com/software/pyqt/>

**pyqtgraph** — the live plots: light curve, focus V-curve, quality trends.
<https://www.pyqtgraph.org>

## Citing ARGOS

ARGOS is GPLv3+ software by Jules Perret. There is no paper. If it contributed
to a result, cite it as software with its version and repository:

```text
Perret, J. 2026, ARGOS: differential photometry for smart telescopes,
v0.4.2, https://github.com/jperret21/argos
```

```{important}
An ARGOS live curve is a quick-look on uncalibrated sub-exposures. A result
worth citing anything for came out of a reduction downstream — cite that
reduction's tools too.
```
