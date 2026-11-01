# Differential photometry — method and equations

This page states exactly what ARGOS computes, with the equations as they are
implemented. Every formula below is traceable to a module in
{mod}`argos.core.photometry`; the API reference gives the signatures.

```{admonition} Scope
:class: important

ARGOS measures **raw, uncalibrated sub-exposures**. No dark, flat or bias is
applied, and no colour transformation to a standard photometric system is
performed. The result is a **quick-look** light curve: enough to see that a
variable is varying, that comparison stars are stable and that the night is
usable — not a publishable measurement. Calibration, registration and final
photometry belong to Siril and `star_var_script`, on the raw frames ARGOS
preserves.
```

```{seealso}
Every formula below carries a citation to where it comes from; the full list,
with links, is in {doc}`references`. Every constant is a configuration key, and
they are gathered in {ref}`§11 <all-parameters>` at the end of this page.
```

## 1. The measurement grid: the green plane

Every geometric and photometric quantity is defined on one grid, so a pixel in
the astrometric solution is the same pixel that is measured.

The sensor is a colour (CFA) detector with a `GRBG` Bayer pattern. ARGOS forms
the **green plane** as the per-tile average of the two green samples:

$$
G(i, j) = \tfrac{1}{2}\left[\,\text{raw}(2i,\,2j) + \text{raw}(2i{+}1,\,2j{+}1)\,\right]
$$

The plane has shape $(H/2,\ W/2)$; odd dimensions are cropped by one row or
column so the two greens line up. Green is used because it is the densest
sampling of the Bayer mosaic and therefore the best signal-to-noise channel for
stars.

```{note}
One green pixel spans two raw pixels. The plate solver reports its scale per
green pixel; ARGOS converts that to the full-resolution scale before showing it
in the interface, so the two never disagree on screen. It is **not** written to
the FITS file: the saved frames carry no WCS and no plate-scale keyword at all
(see {doc}`fits_headers`).
```

Averaging the two greens is not free. It broadens the effective PSF by up to
half a green pixel — about 3.7″ on an S30 Pro — and it bakes in any G1/G2
response imbalance, a common CMOS artefact. Both are accepted in exchange for
$\sqrt{2}$ in signal-to-noise and a grid that lines up exactly with the
astrometric solution.

Two consequences matter scientifically:

- The green plane is **not** a photometric $V$ band. It is an instrumental
  passband, recorded as `TG` — the AAVSO code for an untransformed green
  channel, defined in the [DSLR Observing
  Manual](https://www.aavso.org/dslr-observing-manual). Do not report it as $V$.
- Interpolated debayering is never used for measurement. Debayer mode, stretch
  and channel selection are display settings; the data pipeline reads the CFA
  array.

## 2. Astrometry: from pixels to sky

Plate solving is delegated to ASTAP, which solves the green plane and returns a
WCS. The WCS is what makes identification possible: catalogue positions
(Gaia DR3, AAVSO VSX, SIMBAD, the NASA Exoplanet Archive, AAVSO VSP) are
projected into pixel coordinates, and a click on a star is projected back to
$(\alpha, \delta)$.

This is *astrometric* only. ARGOS does not perform differential astrometry in
the sense of measuring proper motion or parallax; the solution exists to answer
"which star is this?" and "where is my aperture?", frame after frame.

Because the field rotates on an alt-azimuth mount, apertures are re-derived
from the WCS on each solved frame rather than held at fixed pixel coordinates.

## 3. Aperture photometry

Implemented in {mod}`argos.core.photometry.aperture`.

### 3.1 Geometry

The aperture radius adapts to the seeing measured on the frame:

$$
r_\text{ap} = \max\left(r_\text{min},\ k \cdot \text{FWHM}\right)
$$

with $k = 2.5$ and $r_\text{min} = 4$ green px by default. The sky annulus has
default radii $r_\text{in} = 8$ and $r_\text{out} = 12$ green px, constrained so
that the annulus never touches the aperture:

$$
r_\text{in} \ge r_\text{ap} + 1, \qquad r_\text{out} \ge r_\text{in} + 2
$$

When a frame carries no measured FWHM — re-measuring a saved sub, for instance —
a fallback FWHM of 3.0 green px is used. Note what that gives:
$\max(4,\ 2.5 \times 3.0) = 7.5$ px. It does **not** fall back to $r_\text{min}$.

```{warning}
**The live run and an offline re-measurement are not the same measurement.**
Live frames carry a measured FWHM, so the aperture is typically at or near the
4 px floor. A re-measurement of saved subs has no FWHM and uses 7.5 px — 1.9×
the radius, 3.5× the area, with correspondingly more sky and more neighbours.
Do not overlay a live curve and a re-measured one and read the difference as
astrophysics.
```

### Aperture size on the sky

The radii above are in **green-plane pixels**, each spanning two raw pixels.
In angular terms, for the shipped profiles:

| Profile | ″ / green px | $r_\text{ap}$ = 4 px | fallback 7.5 px | annulus 8–12 px |
|---|---|---|---|---|
| S30 Pro (160 mm) | 7.48 | **29.9″** | 56.1″ | 60–90″ |
| S30 (150 mm) | 7.98 | 31.9″ | 59.8″ | 64–96″ |
| S50 (250 mm) | 4.79 | 19.1″ | 35.9″ | 38–57″ |

```{admonition} A 30-arcsecond aperture is enormous — check for neighbours
:class: warning

This is the error source least visible on screen and most likely to ruin a
curve. At 30″ radius, any field within ~20° of the galactic plane will put a
second star inside somebody's aperture, and several inside the sky annulus.

A blended companion is *mostly* harmless while the seeing is constant: it adds
a fixed offset that differential photometry absorbs. It stops being harmless the
moment the FWHM changes, because the fraction of the companion's light inside
the aperture then changes too — producing a variation correlated with seeing
that looks exactly like astrophysics. If your target's brightness tracks the
FWHM trend in the Metrics tab, suspect a blend before you suspect the star.

Before assigning a role, zoom in and look at the aperture circle.
```

**Why 2.5 × FWHM?** It is not the signal-to-noise optimum, which sits nearer
1×FWHM ([Howell 1989](https://doi.org/10.1086/132477); optimal extraction in
[Naylor 1998](https://doi.org/10.1046/j.1365-8711.1998.01314.x)). A wide
aperture is chosen deliberately: it captures essentially all of the flux, so the
measurement is insensitive to seeing-dependent aperture losses — which is what
matters when you are differencing frames taken over hours. The price is sky
noise and blending, paid knowingly.

### 3.2 Sky and flux

The sky level $\tilde{s}$ is the **median** of the annulus pixels, not the mean:
a median is insensitive to a star that happens to fall in the annulus. With
$n_\text{pix}$ pixels inside the aperture,

$$
F = \sum_{(i,j)\,\in\,\text{ap}} I(i,j) \;-\; n_\text{pix}\,\tilde{s}
$$

in ADU.

### 3.3 Noise

The uncertainty is the CCD equation, evaluated in electrons through the gain
$g$ (e⁻/ADU). With $F_e = g F$, $s_e = g\tilde{s}$ and read noise $R$ (e⁻ RMS,
default 1.5):

$$
\sigma_F^2 = F_e + n_\text{pix}\left(s_e + R^2\right)
$$

The three terms are the star's own photon noise, the sky photon noise and the
read noise, each summed over the aperture. The signal-to-noise ratio is
$\text{SNR} = F_e / \sigma_F$.

```{admonition} Source — the CCD equation
:class: seealso

Merline & Howell (1995); Howell, *Handbook of CCD Astronomy* (2006). Full
citations in {doc}`references`.

Three terms of the textbook equation are absent.

**The uncertainty of the sky estimate itself.** The published equation carries a
factor $\left(1 + n_\text{pix}/n_\text{ann}\right)$ on the background terms,
because $\tilde{s}$ is measured from a finite annulus and its own error
propagates into every aperture pixel. ARGOS omits it. Because the sky is a
*median* rather than a mean, the correct factor is closer to
$1 + \tfrac{\pi}{2}\,n_\text{pix}/n_\text{ann}$. With the shipped geometry that
is about **1.3** for a live frame at $r_\text{ap} = 4$, and about **2.3** for
the 7.5 px fallback — so the sky-plus-read variance is understated by 30 % live,
and by more than a factor of two on a re-measurement.

**Dark current** is never measured: its *signal* is absorbed into the annulus
median and subtracted with the sky, but its *shot noise* is not counted — small
on short uncooled sub-exposures against a bright sky, and larger if you observe
cold and dark.

**Digitisation noise** is negligible next to a 1.5 e⁻ read noise.

All three would have to be restored for a calibrated reduction. The first is the
one that matters most here, and it means $\sigma_F$ is an **underestimate** —
which is part of why the run-level floor of §5 exists.

```{admonition} Where does $g$ come from — and when is it wrong?
:class: important

During a live run ARGOS resolves the electron gain in order: the
`camera.egain_table` entry for the current gain setting, else the driver's
`ElectronsPerADU`, else the sensor reference curve for the profile's sensor.
That value is what goes into `EGAIN` in the FITS header.

An **offline re-measurement** of saved subs resolves the same order minus the
driver step, which needs a live handle: `camera.egain_table`, else the sensor
reference curve. Both paths therefore agree on the same $g$ for the same frame.

:::{admonition} Before 0.4.2
:class: warning

The offline path read `camera.egain_table` alone, and that table is empty by
default — so $g$ fell back to **1.0** and ADU were treated as electrons. Every
SNR and every $\sigma_m$ produced by a batch re-measurement under 0.4.1 or
earlier is wrong by the gain, which is 1.723 e⁻/ADU for an IMX585 at gain 80,
not 1.0. Re-measure those runs rather than trusting their error bars.
:::
```

```{admonition} The read noise is one number for three sensors
:class: warning

`photometry.read_noise_e` is a single configured constant, 1.5 e⁻ by default.
The `RDNOISE` value ARGOS writes into the *same frame's* header is looked up per
sensor and per gain, and ranges from about 0.5 to 6.9 e⁻ across the shipped
profiles. The default matches no supported sensor at a typical gain. For
sky-limited exposures this barely moves $\sigma_F$; for short or dark-sky
frames it does. Set it from the sensor reference for your gain if the read term
is not negligible.
```

### 3.4 Instrumental magnitude

$$
m_\text{inst} = -2.5 \log_{10} F
$$

undefined for $F \le 0$, which is reported as a missing measurement rather than
silently clamped. Its uncertainty follows from differentiating the logarithm:

$$
\sigma_{m} = \frac{2.5}{\ln 10}\cdot\frac{\sigma_F}{F_e} \simeq 1.0857\,\frac{\sigma_F}{F_e}
$$

### 3.5 Two flags, and exactly what they do

**Saturation.** Any aperture pixel at or above `camera.linearity_max_adu`
(50 000 ADU by default) flags the measurement.

```{admonition} The 50 000 ADU default is not reconciled with the sensor tables
:class: danger

ARGOS writes both `EGAIN` (e⁻/ADU) and `FULLWELL` (e⁻) into every frame from its
own sensor references. Those three numbers ought to satisfy
$\text{EGAIN} \times \texttt{linearity\_max\_adu} \le \text{FULLWELL}$. They do
not, at any shipped gain:

| Sensor | Gain | EGAIN | FULLWELL | Well fills at |
|---|---|---|---|---|
| IMX585 (S30 Pro) | 0 | 3.00 | 38 000 e⁻ | 12 700 ADU |
| IMX585 (S30 Pro) | 100 | 1.50 | 38 000 e⁻ | 25 300 ADU |
| IMX662 (S30) | 0 | 6.00 | 38 200 e⁻ | 6 400 ADU |
| IMX462 (S50) | 80 | 1.10 | 11 200 e⁻ | 10 200 ADU |

Read literally, the flag fires *after* the well is already full — which means it
does not protect you. The likely cause is a unit mismatch: the sensor is a
12-bit ADC whose output is scaled into a 16-bit file, and it is not recorded
anywhere whether `EGAIN` is referenced to a stored 16-bit ADU or to an ADC
count. Until that is resolved, **do not treat 50 000 ADU as a physical linearity
limit**, and judge saturation from the peak ADU shown on the star card against
your own bias/flat experience instead. This is an open defect, recorded here
rather than papered over.
```

A second limitation: saturation is tested on the **averaged** green plane
$(G_1{+}G_2)/2$, so a star that saturates one green sample but not the other can
average below the threshold and escape the flag.

**Missing PSF support.** A real star spreads over neighbouring pixels; a hot
pixel or a cosmic-ray hit does not. When the brightest aperture pixel exceeds
the sky by more than $10\sigma_\text{sky}$ while its four neighbours average
less than 10 % of that excess, the point is marked *suspect*. Since the live
curve measures raw subs, an uncorrected hot pixel drifting through an aperture
would otherwise paint a convincing artificial dip with nothing to point at.

($\sigma_\text{sky}$ here is the annulus **standard deviation**, while the sky
level is its median. A star sitting in the annulus inflates that σ and makes the
test fire less often — the threshold is more conservative than it looks.)

```{admonition} What the flags actually gate — read this before trusting them
:class: warning

Neither flag removes a bad point from your target's curve. Both are recorded;
only some of them act.

| | *saturated* | *suspect* |
|---|---|---|
| Excludes a **comparison** from the magnitude zero point | yes | **no** |
| Excludes a **comparison** from the relative-flux sum | yes | yes |
| Excludes it from automatic selection and from tracking anchors | yes | yes |
| Removes a flagged **target** point from the curve | **no** | **no** |
| Appears as a column in `photometry.csv` | yes | yes |

So a comparison star flagged only as *suspect* still contributes to the
magnitude ensemble, and a *suspect* target point is still measured, plotted and
exported. What changed in 0.4.2 is that you can now **see** it afterwards: the
flag is a column, so a suspicious point can be found in the file rather than
only in the live view. Filtering on it is still your decision, not ARGOS's.
```

## 4. The comparison ensemble

Implemented in {mod}`argos.core.photometry.differential`.

### 4.1 Zero point

Each comparison star contributes the difference between its catalogue magnitude
and its instrumental magnitude. The zero point is their mean:

$$
\text{ZP} = \frac{1}{n}\sum_{k=1}^{n}\left(m_{\text{cat},k} - m_{\text{inst},k}\right)
$$

and the ensemble scatter about it is the sample RMS

$$
\sigma_\text{ZP} = \sqrt{\frac{1}{n-1}\sum_{k=1}^{n}\left(d_k - \text{ZP}\right)^2},
\qquad d_k = m_{\text{cat},k} - m_{\text{inst},k}
$$

With a single comparison this expression has no $n-1$ to divide by. The code
reports $\sigma_\text{ZP} = 0$ — but the scatter is **undefined**, not zero, and
the distinction is the entire point: the ensemble term then vanishes from
$\sigma_\text{formal}$, so the worst possible ensemble produces the *smallest*
error bar. Never read a one-comparison error bar as a measurement of anything.

With two to five comparisons the situation is better but not good: a sample RMS
on $n = 3$ is itself uncertain by roughly 50 %, and $\sigma_\text{ZP}/\sqrt{n}$
inherits that. This is a second reason the run-level floor of §5 exists.

```{admonition} The zero point is cross-band, by construction
:class: danger

There is no TG catalogue magnitude. AAVSO VSP sequences publish B, V, Rc and Ic;
ARGOS measures an untransformed green instrumental magnitude. So when the band
requested (`photometry.default_band`, `TG` by default) is not in the sequence,
the code falls back to **V**, and every zero point is

$$
\text{ZP} = \operatorname{mean}\left(V_\text{cat} - \text{TG}_\text{inst}\right)
$$

This is the accepted practice for untransformed green photometry — it is what
`TG` *means*, and it is why the AAVSO defines the code. But it makes a colour
term the dominant systematic of the whole method, and it is not optional or
occasional: it is on every frame.

**The practical consequence.** Any difference in colour between your target and
your ensemble propagates straight into the differential magnitude, and its size
changes with airmass — so a red target against a blue ensemble drifts through
the night in a way that looks like variability. VSP publishes B and V for every
sequence star, so you can compute $B-V$: **match the ensemble's mean colour to
the target within roughly 0.3 mag in $B-V$** and this mostly goes away. ARGOS
does not do this for you, and does not warn you.
```

```{note}
The idea of an *ensemble* zero point rather than a single comparison is
Honeycutt's (1992). ARGOS does not implement his method: Honeycutt solves one
global least-squares system across a whole inhomogeneous set of exposures, while
ARGOS forms a clipped mean independently on each frame — which is what a live
preview can do, frame by frame, with no knowledge of the frames still to come.
```

### 4.2 Robust rejection

With three or more comparisons, outliers are clipped against the **median and
the MAD**, not the mean and the RMS: in a small ensemble a single bad
comparison inflates the RMS enough to hide inside it. A pair is rejected when

$$
\left|d_k - \operatorname{med}(d)\right| \;>\; \max\left(2.5 \times 1.4826\,\operatorname{MAD}(d),\ 0.01\ \text{mag}\right)
$$

The factor 1.4826 converts the MAD to a Gaussian-equivalent standard deviation
([Rousseeuw & Croux 1993](https://doi.org/10.1080/01621459.1993.10476408)).
The 10 mmag floor matters: with three or four near-identical comparisons the MAD
can approach zero, and photon noise alone must not be allowed to reject a
perfectly good star. At most two iterations run, and clipping stops if fewer
than two pairs would remain.

### 4.3 Differential magnitude

$$
m = m_{\text{inst,target}} + \text{ZP}
$$

with the formal uncertainty combining the target's own photon error and the
standard error of the ensemble:

$$
\sigma_\text{formal} = \sqrt{\sigma_{m,\text{target}}^{2} + \frac{\sigma_\text{ZP}^{2}}{n}}
$$

Fewer than `photometry.min_comparisons` (2 by default) kept comparisons does not
suppress the measurement; it annotates it. A curve built on one comparison star
is still shown, and still labelled as such.

### 4.4 Relative flux, for transits

A transit needs no catalogue magnitudes — only a stable ratio. ARGOS therefore
also computes

$$
\rho = \frac{F_\text{target}}{\sum_k F_{\text{comp},k}}
$$

against the **sum** of the comparison fluxes, which has the correct ratio
statistics. Propagating fractional errors,

$$
\frac{\sigma_\rho}{\rho} = \sqrt{
  \left(\frac{1}{\text{SNR}_\text{target}}\right)^{2} +
  \frac{\sum_k \left(F_k/\text{SNR}_k\right)^{2}}{\left(\sum_k F_k\right)^{2}}
}
$$

```{important}
Two different weightings are in play, and they are not equivalent. The zero
point of §4.1 is an **unweighted mean of magnitude differences** — a comparison
two magnitudes fainter counts exactly as much as the brightest one. The ratio
$\rho$ is a **sum of fluxes**, which is the photon-optimal weighting. The flux
sum is the statistically better estimator; the magnitude mean is kept because it
is what produces a calibrated magnitude against catalogue values. If the two
paths disagree on a night, the flux ratio is the one to believe about shape.
```

$\rho$ as computed is deliberately **not** normalised to an out-of-transit
baseline and **not** detrended: both are post-processing decisions that depend
on the baseline you actually observed.

```{warning}
The **Relative flux** display in the transit panel does normalise — to the
median of the series currently shown. That is a *display* convenience and it has
a failure mode: if most of your run is in transit, the median sits inside the
event, the baseline is pulled down and the depth looks shallower than it is.
Judge depth from an exported, un-normalised series with a real out-of-transit
baseline.
```

### 4.5 Automatic selection

When a target is chosen and the ensemble is still empty, ARGOS proposes up to
five comparisons from the AAVSO VSP sequence. Each candidate is measured on a
pilot frame and must pass every gate:

- not saturated, and not flagged *suspect* by the PSF-support test of §3.5;
- SNR $\ge 10$ on that pilot frame;
- **instrumental** magnitude within 1.5 mag of the target's — measured on the
  same frame, not compared through catalogue magnitudes, so the gate is about
  landing in the same part of the detector's response;
- separation under 25 arcmin.

The separation limit is deliberate — across a very wide raw field, remote
references are vulnerable to flat-field residuals and differential tracking
error, and a small validated ensemble beats a large scattered one.

Candidates that clear every gate are then **ranked**, and the ranking is not
what the gates suggest:

$$
\text{score} = 0.50\,S_\text{SNR} + 0.40\,S_{\Delta m} + 0.10\,S_\text{dist}
$$

with $S_\text{SNR} = \min(\text{SNR}/\max(20, 2\,\text{SNR}_\text{min}), 1)$,
$S_{\Delta m} = 1 - \Delta m_\text{inst}/\Delta m_\text{max}$ and
$S_\text{dist} = 1 - \theta/120'$. Because that last term is normalised over
120′ while the hard cut is at 25′, distance contributes at most 0.02 of the
final score inside the allowed region — proximity is essentially not a
criterion. Signal-to-noise and brightness match are. If a pilot measurement is
unavailable the code falls back to nearest-first instead.

A manually clicked star is never replaced by this proposal. It is kept, but
because it has no AUID and no catalogue magnitude it cannot contribute a zero
point, so ARGOS complements it rather than leaving the target unusable.

## 5. The run-level systematic floor

Implemented in {mod}`argos.core.photometry.uncertainty`. This is the part that
makes the error bars honest.

The formal error of §3.4 counts photons, and — per §3.3 — undercounts even
those. It does not count scintillation, guiding jitter, aperture losses,
flat-field residuals, differential extinction or focus drift, which for a bright
star through a small aperture are usually the dominant terms. Left alone, the
curve would carry error bars several times too small.

ARGOS measures the real scatter instead of assuming it, using **second
differences**. For independent errors,

$$
\operatorname{Var}\left(m_{i-1} - 2m_i + m_{i+1}\right) = 6\,\sigma^{2}
$$

so a robust estimate of the per-point scatter is

$$
\sigma_\text{real} = \frac{1.4826\,\operatorname{MAD}\left(m_{i-1} - 2m_i + m_{i+1}\right)}{\sqrt{6}}
$$

The second difference is what makes this work on a *variable* star: it removes
any locally linear trend, so the intrinsic variation of the target does not
inflate the noise estimate. Estimating a variance from successive differences
rather than from residuals about a fitted model goes back to
[von Neumann (1941)](https://doi.org/10.1214/aoms/1177731677).

```{admonition} What this estimator can and cannot see
:class: danger

Differencing is a high-pass filter, and that cuts both ways. A component
varying on a timescale $T$ much longer than the cadence $\Delta t$ is suppressed
by roughly $(\Delta t / T)^2$ — which is exactly why the target's own
variability does not contaminate it, and exactly why **it cannot measure the
slow systematics listed above**.

- **Captured:** scintillation (white above ~1 s), frame-to-frame guiding jitter,
  aperture-loss noise, photon noise — everything varying frame to frame.
- **Not captured:** flat-field residuals as the field rotates, differential
  extinction, colour × airmass, focus drift — everything varying over the night.

$\sigma_\text{real}$ is therefore a **high-frequency** scatter, and
$\sigma_\text{sys}$ is the white excess over the formal error, not a measurement
of "scintillation plus flat-field plus tracking". For anything you measure over
a long baseline — a transit depth above all — the relevant uncertainty is set by
correlated red noise on that timescale, which this number does not contain and
will underestimate. Bin your residuals and watch whether the RMS falls as
$1/\sqrt{N}$; where it stops falling is your real floor
([Pont, Zucker & Queloz 2006](https://doi.org/10.1111/j.1365-2966.2006.11012.x)).

And because it comes from a MAD on 10–30 points, $\sigma_\text{real}$ is itself
uncertain at the ±20–30 % level.
```

The systematic term is what the observed scatter has in excess of the formal
errors, and the final uncertainty adds it in quadrature:

$$
\sigma_\text{sys} = \sqrt{\max\left(0,\ \sigma_\text{real}^{2} - \operatorname{med}(\sigma_\text{formal})^{2}\right)},
\qquad
\sigma_\text{final} = \sqrt{\sigma_\text{formal}^{2} + \sigma_\text{sys}^{2}}
$$

Each point keeps its own formal error, so a genuinely noisy frame stays noisier
than its neighbours; the floor is a run-level addition, not a replacement.

At least **ten** valid measurements are needed for the automatic estimate. Below
that, the floor is either taken from the explicit configuration value or omitted
entirely, and the curve shows formal errors only.

```{admonition} One uncertainty contract
:class: note

This is the same robust systematic-floor method used by `star_var_script`. The
live preview and the final reduction therefore quote uncertainties computed the
same way, which is what makes them comparable at all.
```

## 6. Is the ensemble any good? The leave-one-out check

Implemented in {mod}`argos.core.photometry.quality`.

A bad reference star poisons every point of the night, and it does so silently:
the target curve simply moves. ARGOS therefore assesses each comparison against
the others, without changing anything the observer selected.

The insight is that the work is already done. Every comparison star is itself
measured differentially against the *other* comparisons — a leave-one-out curve.
If that curve is not flat, something in the ensemble is wrong.

```{warning}
**The test detects, it does not localise** — not with the ensemble sizes ARGOS
works with. With three to five comparisons, one bad star sits in the reference
set of every *other* star's leave-one-out curve, so it makes all of them wander.
A single red flag usually means "this ensemble has a problem", not "this star is
the problem". Localisation needs an ensemble large enough that each
leave-one-out subset is still dominated by good stars.

What to do with a flagged ensemble: drop the worst offender, let the assessment
re-run, and see whether the others go quiet. If they do, you found it.
```

For each comparison, over its valid points (not saturated, with finite time,
magnitude and error), ARGOS computes the second-difference scatter
$\sigma_\text{real}$ of §5 and the median formal error, then classifies:

| Status | Condition |
|---|---|
| *insufficient data* | fewer than 10 valid points, or no scatter estimate |
| **Noisy** | median formal error $> 0.10$ mag — the star is too faint or too poorly measured to judge |
| **Unstable** | scatter $> 0.10$ mag — the star moves relative to the others |
| **Stable** | neither of the above |

The ensemble as a whole is then reported as *insufficient data* while any
comparison is still unassessed, **not ready** while fewer than three
comparisons are stable, and *live-preview consistent* otherwise.

The report is written to `photometry_quality.json` in the session folder and
shown live in the **Comparison quality** panel, with the criteria that produced
it recorded alongside the verdicts.

```{warning}
"Stable" means only that the *live preview* has enough internally consistent
samples. It is not a calibrated-photometry certification. Confirm the ensemble
on registered, calibrated frames in Siril and `star_var_script`.
```

## 7. Time and airmass

Implemented in {mod}`argos.core.photometry.airmass`.

The timestamp of a measurement is the **exposure midpoint**, converted to
Julian date with the Fliegel–Van Flandern day number:

$$
\text{JD} = \text{JDN} + \frac{h - 12}{24} + \frac{\text{min}}{1440} + \frac{s}{86400}
$$

`BJD_TDB` — the publishable time standard, applying the barycentric
light-travel-time correction for the target's direction from the observing site
— is computed through astropy when the site and exposure time are available.

```{tip}
For **transit timing and long-baseline period work**, report `BJD_TDB` — not
`HJD`, not `JD_UTC`. The difference reaches several seconds for `HJD` and `UTC`
accumulates leap seconds
([Eastman, Siverd & Gaudi 2010](https://doi.org/10.1086/655938)).

For an **AAVSO submission**, `JD_UTC` is what is asked for, and it is what
ARGOS's AAVSO export writes (`#DATE=JD`). Both columns are in
`photometry.csv`; use the one your destination expects.
```

Airmass uses the [Pickering (2002)](https://www.dioi.org/vols/wc0.pdf) formula
— better than 0.01 airmass down to 1° altitude, and finite at the horizon where
$\sec z$ diverges:

$$
X = \frac{1}{\sin\!\left(h + \dfrac{244}{165 + 47\,h^{1.1}}\right)}, \qquad h \text{ in degrees}
$$

It is undefined at or below the horizon — the function returns nothing rather
than a large number, so a frame taken through the treetops has no airmass rather
than a fictional one.

```{warning}
The header and the light-curve point use the **same formula** but not the same
instant, and they will not always agree. `AIRMASS` in the FITS header is
computed for the exposure **start** from the mount's reported altitude. The
light-curve point is timed at the **midpoint** and derives its airmass from the
target's own coordinates at that JD (`airmass_at()`), which is the number an
extinction fit wants. On a long exposure near the horizon the two differ by
more than rounding.

The point falls back to the frame-level value when no observing site is
configured — without a latitude and longitude there is no altitude to compute.
```

## 8. What the export actually contains

**Export measurements…** writes one row per star per frame. The multi-star form
prefixes `star_id`, `role`, `name` and `auid`; the rest is the same in both.

| Column | Unit | Meaning |
|---|---|---|
| `jd_utc` | day | Julian date of the exposure **midpoint**, UTC |
| `bjd_tdb` | day | Barycentric JD in TDB — empty when the site is unknown |
| `mag` | mag | the differential magnitude of §4.3 |
| `mag_err` | mag | the **final** uncertainty: formal and systematic combined (§5) |
| `formal_mag_err` | mag | the photon-only error of §3.4, before the floor |
| `sigma_syst` | mag | the run-level systematic floor that was added |
| `airmass` | — | from the target's coordinates at this point's JD (see §7) |
| `fwhm` | green px | frame FWHM. **Multiply by 2 for full-resolution pixels**, then by the plate scale for arcsec |
| `sky_adu` | ADU | annulus median for this star on this frame |
| `comps_used` | count | comparisons behind `mag` in this row — the zero-point ensemble of §4.1 |
| `relative_flux` | — | $\rho$ of §4.4, un-normalised |
| `relative_flux_err` | — | its propagated error |
| `relative_comps_used` | count | comparisons behind $\rho$ in this row |
| `saturated` | bool | the §3.5 saturation flag |
| `suspect` | bool | the §3.5 PSF-support flag |

```{note}
**The two ensemble counts are both here because they genuinely differ.** The
ratio path rejects *suspect* comparisons and the magnitude path does not
(§3.5), so `relative_comps_used` is often smaller than `comps_used` on the same
row. Each count belongs to the quantity beside it: read `comps_used` with
`mag`, `relative_comps_used` with `relative_flux`.
```

A row is written whenever **either** product exists. A frame whose comparisons
were all flagged *suspect* still yields a differential magnitude, so it still
yields a row — with an empty `relative_flux` and `relative_comps_used = 0`.
Only a frame that produced neither a magnitude nor a ratio is dropped.

```{admonition} Before 0.4.2 — check your existing exports
:class: warning

Three of the columns above behaved differently in 0.4.1 and earlier, and none
of the differences were visible from the interface:

- `comps_used` carried the **flux-ratio** count, not the magnitude one. A file
  from 0.4.1 states the wrong ensemble size beside every `mag`.
- There was **no `suspect` column** and no `relative_comps_used`.
- A row was written **only when $\rho$ existed**. A run in which every
  comparison was flagged *suspect* silently lost its magnitudes — no row, no
  note. If an old curve has gaps you never explained, compare its row count
  against the frame count before concluding the star did something.

Re-running the batch measurement on the saved frames regenerates the file
correctly.
```

## 9. The floor you cannot get under

Before the list of limits, one number that is not in the code and should govern
your expectations. Atmospheric **scintillation** sets a noise floor that no
exposure time and no photometric skill removes, and it scales as
$D^{-2/3}$ — punishing for a 30 mm objective. Young's approximation,

$$
\sigma_\text{scint} \simeq 0.09\, D^{-2/3}\, X^{1.75}\, e^{-h/8000}\, (2t)^{-1/2}
$$

($D$ in cm, $X$ airmass, $h$ site elevation in m, $t$ exposure in s), evaluated
for a site at 200 m:

| Telescope | $X$ = 1.0, 20 s | $X$ = 1.5, 20 s | $X$ = 1.5, 60 s | $X$ = 2.0, 20 s |
|---|---|---|---|---|
| S30 / S30 Pro (30 mm) | 7 mmag | **15 mmag** | 8 mmag | 24 mmag |
| S50 (50 mm) | 5 mmag | **10 mmag** | 6 mmag | 17 mmag |

Read that as: on a 20-second sub at airmass 1.5, an S30 Pro cannot do better
than about 15 mmag per frame *no matter how bright the star*. Binning helps —
scintillation is white on these timescales — so ten frames average to ~5 mmag.
Observing high beats observing long: the $X^{1.75}$ term costs more than the
exposure term buys.

This is theory, not a measurement of your telescope. Its practical use is as a
sanity check: if your measured scatter on a bright star is far *below* the
figure above, suspect that something is smoothing your data; if it is far above,
the limit is not the atmosphere and is worth chasing.
[Young (1967)](https://doi.org/10.1086/110303);
[Osborn et al. (2015)](https://doi.org/10.1093/mnras/stv1400) for how variable
the coefficient really is from site to site.

## 10. Known limits in 0.4.2

- **No calibration.** No dark, flat or bias. Flat-field residuals are the main
  reason the comparison ensemble is kept within 25 arcmin.
- **A cross-band zero point.** $V_\text{cat} - \text{TG}_\text{inst}$, with no
  colour transformation (§4.1). This is the largest systematic in the method.
- **An incomplete noise model.** The sky-estimate term is missing, so
  $\sigma_\text{formal}$ is an underestimate by ~30 % live and more offline
  (§3.3).
- **A high-frequency-only systematic floor.** $\sigma_\text{sys}$ does not
  contain red noise (§5), so long-baseline quantities — transit depths above all
  — carry error bars that are too small.
- **An unreconciled saturation threshold** (§3.5), and flags that do less than
  they appear to.
- **Wide apertures.** 30″ radius on an S30 Pro, with no deblending and no PSF
  fitting (§3.1).
- **Comparison curves are diagnostics.** The check star exists to expose
  ensemble drift. Never submit a comparison curve as an observation.
- **Attended operation.** There is no weather safety and no restart recovery.

```{admonition} What you *can* honestly say
:class: tip

The list above is long, so here is the other side of it, plainly. An ARGOS run
is a legitimate **detection and monitoring** measurement: that a star varied,
by roughly how much, with a timing you can trust to the second, against
references you have evidence were stable. That is a real observation and worth
recording.

What it is not is a **calibrated** measurement: an untransformed magnitude on
an absolute scale, or a depth or amplitude quoted to a precision the error bars
here can support. That comes from the reduction, on the raw frames ARGOS kept
for you.
```

The corresponding hand-off is described in {doc}`guide` — copy the whole session
folder before reducing it.

(all-parameters)=
## 11. Every constant on this page

All of them are configuration keys, editable in **Settings** and stored in the
JSON config. The defaults below are those of 0.4.2.

| Key | Default | Sets |
|---|---|---|
| `photometry.aperture_fwhm_mult` | `2.5` | $k$, the aperture-to-FWHM factor — §3.1 |
| `photometry.aperture_min_px` | `4` | $r_\text{min}$, the aperture floor in green px — §3.1 |
| `photometry.annulus_in_px` | `8` | $r_\text{in}$, sky annulus inner radius — §3.1 |
| `photometry.annulus_out_px` | `12` | $r_\text{out}$, sky annulus outer radius — §3.1 |
| `photometry.read_noise_e` | `1.5` | $R$, read noise in e⁻ RMS — §3.3 |
| `camera.linearity_max_adu` | `50000` | the saturation flag threshold — §3.5 |
| `photometry.default_band` | `TG` | the band label written to the exports — §1 |
| `photometry.min_comparisons` | `2` | below this, a measurement is annotated, not hidden — §4.3 |
| `photometry.auto_comparisons` | `5` | how many comparisons the automatic proposal may add — §4.5 |
| `photometry.comparison_min_snr` | `10.0` | pilot-frame SNR gate — §4.5 |
| `photometry.comparison_max_delta_mag` | `1.5` | instrumental-magnitude gate, in mag — §4.5 |
| `photometry.comparison_max_separation_arcmin` | `25.0` | how far a reference may sit from the target — §4.5 |
| `photometry.comparison_validation_min_points` | `10` | points needed before a comparison can be judged — §6 |
| `photometry.comparison_validation_max_scatter_mag` | `0.10` | above this scatter, *Unstable* — §6 |
| `photometry.comparison_validation_max_formal_error_mag` | `0.10` | above this median error, *Noisy* — §6 |
| `photometry.systematic_floor_mag` | `None` | `None` derives the floor from the curve; a number imposes it — §5 |
| `photometry.track_apertures` | `True` | whether apertures follow a transform fitted between frames or are re-derived from the WCS alone. Live, on a mount in EQ mode the fit is a translation only, since there is no field rotation to follow; in alt-az mode it is rigid (rotation and translation). The offline batch re-measurement does not know the mount mode — saved frames do not record it — and always fits the rigid transform |
| `camera.egain_table` | `{}` (empty) | $g$ in e⁻/ADU per gain setting. Empty is fine: the live path asks the driver then the sensor reference, and since 0.4.2 the offline path falls back to that same reference (§3.3) |

```{warning}
Changing these changes what the numbers mean. Widening
`comparison_max_separation_arcmin` buys more references at the cost of
flat-field residuals; raising `comparison_max_delta_mag` mixes stars from
different parts of the detector's response. If you change one, record it —
`photometry_quality.json` stores the validation criteria that were in force, but
nothing stores the aperture geometry for you.
```
