"""Aperture tracking across saved frames — field-rotation / drift correction.

A single reference plate solve is only valid at the instant it was made: on an
alt-az mount the field rotates around the frame centre for the rest of the
session, and any mount drifts. Rather than model the rotation from time + site
(parity and clock pitfalls), measure it: each frame, re-centroid the anchor
stars in a small window around their predicted positions, fit the rigid
transform (rotation + translation, no scale) that maps the *reference*
positions onto the measured ones, and use it to place every aperture. The
transform is re-fit from the reference geometry on every frame — prediction
only seeds the search windows — so centroid noise never accumulates, while
consecutive subs rotate by well under a pixel so the windows never lose their
star.

``measure_targets()`` only needs ``world_to_pixel_deg`` from its ``wcs``
argument, so :class:`TrackedWCS` wraps the reference solve and applies the
current transform after projection — the measurement core stays untouched.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)


def select_tracking_anchors(
    green,
    wcs,
    candidates,
    params,
    *,
    min_snr: float = 10.0,
    max_anchors: int = 6,
):
    """Return only bright, clean stars suitable for geometric tracking.

    The tracking constellation is deliberately distinct from the photometric
    ensemble.  A reference can be meaningful after offline calibration yet be
    too weak to centroid in a raw live sub; such a source must never steer the
    WCS transform.
    """
    from argos.core.photometry.aperture import measure_aperture

    r_ap = params.aperture_px(None)
    r_in = max(params.annulus_in_px, r_ap + 1.0)
    r_out = max(params.annulus_out_px, r_in + 2.0)
    qualified: list[tuple[float, object]] = []
    for star in candidates:
        x, y = wcs.world_to_pixel_deg(star.ra_deg, star.dec_deg)
        phot = measure_aperture(
            green,
            float(x),
            float(y),
            r_ap,
            r_in,
            r_out,
            egain=params.egain,
            read_noise_e=params.read_noise_e,
            sat_adu=params.sat_adu,
        )
        if phot is None or phot.saturated or phot.suspect or phot.snr < min_snr:
            continue
        qualified.append((phot.snr, star))
    qualified.sort(key=lambda item: item[0], reverse=True)
    return [star for _snr, star in qualified[:max_anchors]]


def refine_centroid(green: np.ndarray, x: float, y: float, r: float) -> tuple[float, float] | None:
    """Flux-weighted centroid within radius ``r`` of ``(x, y)``, or None.

    A local background (median of the window's outer ring) is subtracted first
    so sky level and gradients don't pull the centroid. Returns None when the
    window falls outside the frame, carries no flux, or the centroid lands
    outside the search radius (no star there — clouds, or the prediction is
    already lost).
    """
    measurement = _refine_centroid_measurement(green, x, y, r)
    return (measurement.x, measurement.y) if measurement is not None else None


@dataclass(frozen=True)
class _CentroidMeasurement:
    """Centroid plus its local background-subtracted signal (internal)."""

    x: float
    y: float
    signal: float


def _refine_centroid_measurement(
    green: np.ndarray, x: float, y: float, r: float
) -> _CentroidMeasurement | None:
    """Centroid with the signal used to reject a lost anchor locking on noise."""
    h, w = green.shape
    px, py = int(round(x)), int(round(y))
    ri = max(2, int(math.ceil(r)))
    y0, y1 = py - ri, py + ri + 1
    x0, x1 = px - ri, px + ri + 1
    if y0 < 0 or x0 < 0 or y1 > h or x1 > w:
        return None

    win = green[y0:y1, x0:x1].astype(np.float32)
    yy, xx = np.mgrid[y0:y1, x0:x1]
    dist2 = (yy - y) ** 2 + (xx - x) ** 2
    aperture = dist2 <= r * r
    ring = win[aperture & (dist2 > (r - 1.0) ** 2)]
    if ring.size == 0:
        return None
    local_bg = float(np.median(ring))

    sub = np.where(aperture, win - local_bg, 0.0)
    np.clip(sub, 0.0, None, out=sub)
    flux = float(sub.sum())
    if flux <= 0.0:
        return None

    cx = float((xx * sub).sum() / flux)
    cy = float((yy * sub).sum() / flux)
    if (cx - x) ** 2 + (cy - y) ** 2 > r * r:
        return None
    return _CentroidMeasurement(cx, cy, flux)


@dataclass(frozen=True)
class RigidTransform:
    """2D rotation by ``theta_rad`` about ``(cx, cy)`` followed by ``(tx, ty)``."""

    theta_rad: float
    tx: float
    ty: float
    cx: float
    cy: float

    @classmethod
    def identity(cls, cx: float, cy: float) -> "RigidTransform":
        return cls(0.0, 0.0, 0.0, cx, cy)

    def apply(self, x: float, y: float) -> tuple[float, float]:
        c, s = math.cos(self.theta_rad), math.sin(self.theta_rad)
        dx, dy = x - self.cx, y - self.cy
        return (
            self.cx + c * dx - s * dy + self.tx,
            self.cy + s * dx + c * dy + self.ty,
        )

    def invert(self, x: float, y: float) -> tuple[float, float]:
        """Map a current-frame point back into the reference frame."""
        c, s = math.cos(self.theta_rad), math.sin(self.theta_rad)
        # Undo the post-rotation translation, then rotate by -theta around
        # the same pivot.  This is the inverse of :meth:`apply`.
        dx, dy = x - self.cx - self.tx, y - self.cy - self.ty
        return self.cx + c * dx + s * dy, self.cy - s * dx + c * dy

    @property
    def rotation_deg(self) -> float:
        return math.degrees(self.theta_rad)

    @property
    def shift_px(self) -> float:
        return math.hypot(self.tx, self.ty)


def fit_rigid(
    src: list[tuple[float, float]],
    dst: list[tuple[float, float]],
    cx: float,
    cy: float,
) -> RigidTransform:
    """Least-squares rotation+translation mapping ``src`` → ``dst`` (no scale).

    Closed-form orthogonal Procrustes on the centred point sets. With a single
    pair the rotation is indeterminate, so it degrades to pure translation.
    ``(cx, cy)`` is only the pivot the result is expressed about (any rigid
    transform can be written about any pivot); it does not affect the fit.
    """
    if len(src) != len(dst) or not src:
        raise ValueError("fit_rigid needs matched, non-empty point lists")

    sx = np.array([p[0] for p in src], dtype=np.float64)
    sy = np.array([p[1] for p in src], dtype=np.float64)
    dx = np.array([p[0] for p in dst], dtype=np.float64)
    dy = np.array([p[1] for p in dst], dtype=np.float64)

    if len(src) == 1:
        theta = 0.0
    else:
        psx, psy = sx - sx.mean(), sy - sy.mean()
        pdx, pdy = dx - dx.mean(), dy - dy.mean()
        theta = math.atan2(
            float((psx * pdy - psy * pdx).sum()),
            float((psx * pdx + psy * pdy).sum()),
        )

    # Translation: what's left after rotating the source about the pivot.
    c, s = math.cos(theta), math.sin(theta)
    rx = cx + c * (sx - cx) - s * (sy - cy)
    ry = cy + s * (sx - cx) + c * (sy - cy)
    return RigidTransform(
        theta_rad=theta,
        tx=float((dx - rx).mean()),
        ty=float((dy - ry).mean()),
        cx=cx,
        cy=cy,
    )


def fit_translation(
    src: list[tuple[float, float]],
    dst: list[tuple[float, float]],
    cx: float,
    cy: float,
) -> RigidTransform:
    """Least-squares translation-only mapping from ``src`` to ``dst``.

    An equatorial mount has no field rotation, but it can still have small
    guide/periodic drift.  Constraining this fit prevents centroid noise from
    inventing a rotation and moving apertures at the edges of the sensor.
    """
    if len(src) != len(dst) or not src:
        raise ValueError("fit_translation needs matched, non-empty point lists")
    return RigidTransform(
        theta_rad=0.0,
        tx=float(np.mean([dx - sx for (sx, _sy), (dx, _dy) in zip(src, dst)])),
        ty=float(np.mean([dy - sy for (_sx, sy), (_dx, dy) in zip(src, dst)])),
        cx=cx,
        cy=cy,
    )


class ApertureTracker:
    """Follow a fixed star geometry through a time-ordered frame sequence.

    Args:
        anchors_xy: Reference pixel positions (green px) of the anchor stars —
            comparisons/checks: bright and astrometrically stable. (A variable's
            position is stable too, but its centroid gets noisy at minimum.)
        frame_center: Pivot the transform is expressed about, ``(cx, cy)``.
        search_r: Centroid search radius in px. Must exceed the worst-case
            per-frame motion (sub-pixel) plus prediction error; the default is
            generous without risking a lock onto a neighbouring star.
    """

    #: The worst anchor is only examined when its residual exceeds this floor
    #: (px) — centroid noise on a faint anchor must never trigger a rejection.
    RESIDUAL_FLOOR_PX = 2.0
    #: …and it is dropped only when refitting without it shrinks the median
    #: residual below this fraction of the original. A residuals-vs-threshold
    #: test can't work here: a least-squares rigid fit smears a lone outlier's
    #: error over *all* anchors, so with 3-4 anchors the outlier's own residual
    #: never stands out. The leave-worst-out improvement is decisive instead.
    REJECT_IMPROVEMENT = 0.3
    #: A real guide star can dim in cloud, but not by an order of magnitude
    #: between consecutive science frames.  Below this fraction of its first
    #: measured signal, a centroid is almost certainly a noise/hot-pixel lock.
    MIN_SIGNAL_FRACTION = 0.20

    def __init__(
        self,
        anchors_xy: list[tuple[float, float]],
        frame_center: tuple[float, float],
        search_r: float = 8.0,
    ) -> None:
        self._anchors = [(float(x), float(y)) for x, y in anchors_xy]
        self._reference_signal: list[float | None] = [None] * len(self._anchors)
        self._search_r = float(search_r)
        self.transform = RigidTransform.identity(*frame_center)
        self.anchors_used = 0
        self.anchors_rejected = 0  # dropped by the last residual clip
        self.anchors_signal_rejected = 0  # measurements rejected as a lost anchor
        self.residual_px = 0.0  # median |fit − measured| of the kept anchors
        self.frames_lost = 0  # consecutive frames with no anchor found

    def update(self, green: np.ndarray, *, allow_rotation: bool = True) -> int:
        """Re-fit the transform on one frame; returns anchors matched.

        On zero matches (clouds, slew glitch) the previous transform is kept —
        the next frame's windows still sit where the stars were last seen.
        With ≥3 matches, the worst-fitting anchor is dropped (P8) when its
        residual exceeds RESIDUAL_FLOOR_PX *and* refitting without it improves
        the median residual by REJECT_IMPROVEMENT — one wrong lock (hot pixel,
        neighbouring star) must not skew every aperture of the frame.

        ``allow_rotation=False`` is for an EQ mount: it retains the genuine
        frame translation while forbidding a spurious rotation fit.
        """
        matched_ref: list[tuple[float, float]] = []
        matched_meas: list[tuple[float, float]] = []
        for index, ref in enumerate(self._anchors):
            pred = self.transform.apply(*ref)
            meas = _refine_centroid_measurement(green, pred[0], pred[1], self._search_r)
            if meas is not None:
                baseline = self._reference_signal[index]
                if baseline is None:
                    self._reference_signal[index] = meas.signal
                elif meas.signal < baseline * self.MIN_SIGNAL_FRACTION:
                    self.anchors_signal_rejected += 1
                    continue
                matched_ref.append(ref)
                matched_meas.append((meas.x, meas.y))

        self.anchors_rejected = 0
        if not matched_ref:
            self.anchors_used = 0
            self.frames_lost += 1
            return 0
        self.frames_lost = 0

        fit = fit_rigid if allow_rotation else fit_translation
        t = fit(matched_ref, matched_meas, self.transform.cx, self.transform.cy)
        residuals = self._residuals(t, matched_ref, matched_meas)

        if len(matched_ref) >= 3 and max(residuals) > self.RESIDUAL_FLOOR_PX:
            worst = int(np.argmax(residuals))
            kept_ref = matched_ref[:worst] + matched_ref[worst + 1 :]
            kept_meas = matched_meas[:worst] + matched_meas[worst + 1 :]
            t2 = fit(kept_ref, kept_meas, self.transform.cx, self.transform.cy)
            res2 = self._residuals(t2, kept_ref, kept_meas)
            if float(np.median(res2)) < self.REJECT_IMPROVEMENT * float(np.median(residuals)):
                logger.warning(
                    "Tracker: anchor at (%.0f, %.0f) rejected (%.1f px off the "
                    "consensus of the other %d) — refit",
                    matched_ref[worst][0],
                    matched_ref[worst][1],
                    residuals[worst],
                    len(kept_ref),
                )
                self.anchors_rejected = 1
                matched_ref, matched_meas = kept_ref, kept_meas
                t, residuals = t2, res2

        self.transform = t
        self.anchors_used = len(matched_ref)
        self.residual_px = float(np.median(residuals))
        return self.anchors_used

    @staticmethod
    def _residuals(t: RigidTransform, refs, meas) -> list[float]:
        return [
            math.hypot(m[0] - t.apply(*r)[0], m[1] - t.apply(*r)[1]) for r, m in zip(refs, meas)
        ]


class TrackedWCS:
    """Reference solve + live rigid correction, quacking like a FrameWCS.

    It preserves both directions of the :class:`FrameWCS` interface.  Catalogue
    overlays use ``world_to_pixel_deg``; field-geometry code needs the inverse
    direction to recover the current frame centre.
    """

    def __init__(self, wcs, tracker: ApertureTracker) -> None:
        self._wcs = wcs
        self._tracker = tracker

    def world_to_pixel_deg(self, ra_deg: float, dec_deg: float) -> tuple[float, float]:
        x, y = self._wcs.world_to_pixel_deg(ra_deg, dec_deg)
        return self._tracker.transform.apply(float(x), float(y))

    def pixel_to_radec(self, x: float, y: float) -> tuple[float, float]:
        reference_x, reference_y = self._tracker.transform.invert(float(x), float(y))
        return self._wcs.pixel_to_radec(reference_x, reference_y)
