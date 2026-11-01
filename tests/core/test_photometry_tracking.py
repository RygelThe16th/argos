"""Aperture tracking — rigid fit, centroid refinement, rotating-field lock.

Synthetic Gaussian star fields only (Qt-free, no hardware): the tracker must
follow a field that slowly rotates around the frame centre — the alt-az
field-rotation case a single reference solve cannot handle.
"""

from __future__ import annotations

import math

import numpy as np

from argos.core.photometry.tracking import (
    ApertureTracker,
    RigidTransform,
    TrackedWCS,
    fit_rigid,
    fit_translation,
    refine_centroid,
)


def _rotate(x, y, cx, cy, deg):
    a = math.radians(deg)
    dx, dy = x - cx, y - cy
    return (
        cx + math.cos(a) * dx - math.sin(a) * dy,
        cy + math.sin(a) * dx + math.cos(a) * dy,
    )


def _green_with_stars(positions, peak=8000.0, sky=200.0, sigma=1.5, shape=(120, 120)):
    yy, xx = np.mgrid[0 : shape[0], 0 : shape[1]]
    g = np.full(shape, sky, dtype=np.float32)
    for cx, cy in positions:
        g += peak * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma**2))
    return g


# ── refine_centroid ─────────────────────────────────────────────────────────


def test_refine_centroid_finds_offset_star() -> None:
    green = _green_with_stars([(42.3, 57.8)])
    got = refine_centroid(green, 45.0, 55.0, r=6.0)
    assert got is not None
    assert abs(got[0] - 42.3) < 0.2 and abs(got[1] - 57.8) < 0.2


def test_refine_centroid_empty_sky_is_none() -> None:
    green = np.full((60, 60), 200.0, dtype=np.float32)
    assert refine_centroid(green, 30.0, 30.0, r=6.0) is None


def test_refine_centroid_off_frame_is_none() -> None:
    green = _green_with_stars([(5.0, 5.0)])
    assert refine_centroid(green, 2.0, 2.0, r=6.0) is None


# ── fit_rigid ───────────────────────────────────────────────────────────────


def test_fit_rigid_recovers_rotation_and_shift() -> None:
    cx = cy = 50.0
    src = [(20.0, 20.0), (80.0, 25.0), (50.0, 85.0), (75.0, 70.0)]
    dst = [(x + 3.0, y - 2.0) for x, y in (_rotate(sx, sy, cx, cy, 4.0) for sx, sy in src)]
    t = fit_rigid(src, dst, cx, cy)
    assert abs(t.rotation_deg - 4.0) < 1e-6
    assert abs(t.tx - 3.0) < 1e-6 and abs(t.ty + 2.0) < 1e-6
    for (sx, sy), (dx, dy) in zip(src, dst):
        ax, ay = t.apply(sx, sy)
        assert abs(ax - dx) < 1e-6 and abs(ay - dy) < 1e-6


def test_fit_rigid_single_pair_is_translation_only() -> None:
    t = fit_rigid([(10.0, 10.0)], [(12.0, 9.0)], 50.0, 50.0)
    assert t.rotation_deg == 0.0
    assert abs(t.tx - 2.0) < 1e-9 and abs(t.ty + 1.0) < 1e-9


def test_fit_translation_preserves_eq_drift_without_rotation() -> None:
    src = [(20.0, 20.0), (80.0, 25.0), (50.0, 85.0)]
    dst = [(x + 3.0, y - 2.0) for x, y in src]

    t = fit_translation(src, dst, 50.0, 50.0)

    assert t.rotation_deg == 0.0
    assert abs(t.tx - 3.0) < 1e-9 and abs(t.ty + 2.0) < 1e-9


# ── ApertureTracker over a rotating field ───────────────────────────────────

_ANCHORS = [(30.0, 30.0), (90.0, 35.0), (60.0, 95.0)]
_CENTER = (60.0, 60.0)


def test_tracker_follows_slow_field_rotation() -> None:
    total_deg = 8.0
    n_frames = 20
    tracker = ApertureTracker(_ANCHORS, _CENTER, search_r=6.0)
    for i in range(1, n_frames + 1):
        ang = total_deg * i / n_frames
        pos = [_rotate(x, y, *_CENTER, ang) for x, y in _ANCHORS]
        assert tracker.update(_green_with_stars(pos)) == len(_ANCHORS)
    assert abs(tracker.transform.rotation_deg - total_deg) < 0.1

    # A star far from the anchors is placed by the same transform.
    wcs = TrackedWCS(_FakeRefWCS({(1.0, 1.0): (95.0, 90.0)}), tracker)
    expect = _rotate(95.0, 90.0, *_CENTER, total_deg)
    got = wcs.world_to_pixel_deg(1.0, 1.0)
    assert abs(got[0] - expect[0]) < 0.3 and abs(got[1] - expect[1]) < 0.3


def test_tracker_survives_a_blank_frame() -> None:
    tracker = ApertureTracker(_ANCHORS, _CENTER, search_r=6.0)
    pos = [_rotate(x, y, *_CENTER, 2.0) for x, y in _ANCHORS]
    assert tracker.update(_green_with_stars(pos)) == 3
    before = tracker.transform

    # Clouds: no star anywhere. Transform must be kept, not reset.
    assert tracker.update(np.full((120, 120), 200.0, dtype=np.float32)) == 0
    assert tracker.frames_lost == 1
    assert tracker.transform == before

    # Sky clears on the next frame — re-acquires from where it left off.
    pos = [_rotate(x, y, *_CENTER, 2.5) for x, y in _ANCHORS]
    assert tracker.update(_green_with_stars(pos)) == 3
    assert abs(tracker.transform.rotation_deg - 2.5) < 0.1


def test_identity_transform_is_a_no_op() -> None:
    t = RigidTransform.identity(60.0, 60.0)
    x, y = t.apply(12.3, 45.6)
    assert abs(x - 12.3) < 1e-9 and abs(y - 45.6) < 1e-9
    assert t.rotation_deg == 0.0 and t.shift_px == 0.0


def test_rigid_transform_inverse_round_trips() -> None:
    t = RigidTransform(theta_rad=math.radians(4.0), tx=3.0, ty=-2.0, cx=60.0, cy=60.0)
    x, y = t.apply(95.0, 90.0)
    rx, ry = t.invert(x, y)
    assert abs(rx - 95.0) < 1e-9 and abs(ry - 90.0) < 1e-9


class _FakeRefWCS:
    """Maps (ra_deg, dec_deg) → a preset green-px (x, y)."""

    def __init__(self, mapping):
        self._m = mapping

    def world_to_pixel_deg(self, ra_deg, dec_deg):
        return self._m[(ra_deg, dec_deg)]

    def pixel_to_radec(self, x, y):
        for radec, pixel in self._m.items():
            if abs(x - pixel[0]) < 1e-8 and abs(y - pixel[1]) < 1e-8:
                return radec
        raise KeyError((x, y))


def test_project_points_through_tracked_wcs_follows_the_field() -> None:
    """W1 (field bug 2026-07-11): catalog markers are projected with
    ``project_points`` — through a TrackedWCS they must land on the rotated
    star positions, not freeze at the reference-solve position."""
    from argos.core.imaging.astrometry_session import project_points

    tracker = ApertureTracker(_ANCHORS, _CENTER, search_r=6.0)
    ang = 3.0
    pos = [_rotate(x, y, *_CENTER, ang) for x, y in _ANCHORS]
    assert tracker.update(_green_with_stars(pos)) == len(_ANCHORS)

    ref = _FakeRefWCS({(1.0, 1.0): (95.0, 90.0), (2.0, 2.0): (30.0, 40.0)})
    got = project_points(TrackedWCS(ref, tracker), (120, 120), [(1.0, 1.0), (2.0, 2.0)])
    for g, (rx, ry) in zip(got, [(95.0, 90.0), (30.0, 40.0)]):
        ex, ey = _rotate(rx, ry, *_CENTER, ang)
        assert g is not None
        assert abs(g[0] - ex) < 0.3 and abs(g[1] - ey) < 0.3


def test_tracked_wcs_inverse_uses_reference_coordinates() -> None:
    tracker = ApertureTracker(_ANCHORS, _CENTER, search_r=6.0)
    assert tracker.update(_green_with_stars([(x + 3.0, y - 2.0) for x, y in _ANCHORS])) == 3
    ref = _FakeRefWCS({(1.0, 1.0): (95.0, 90.0)})
    wcs = TrackedWCS(ref, tracker)

    current_x, current_y = wcs.world_to_pixel_deg(1.0, 1.0)

    assert wcs.pixel_to_radec(current_x, current_y) == (1.0, 1.0)


def test_tracker_rejects_a_bad_anchor() -> None:
    """P8: one anchor locked on the wrong thing (hot pixel, neighbour) must
    not skew the transform when two clean anchors plus one more agree."""
    anchors = [(30.0, 30.0), (90.0, 35.0), (60.0, 95.0), (95.0, 85.0)]
    # Wide search window: the displaced star is measured where it really is,
    # not truncated by the window edge.
    tracker = ApertureTracker(anchors, _CENTER, search_r=10.0)

    ang = 1.5
    pos = [_rotate(x, y, *_CENTER, ang) for x, y in anchors]
    pos[3] = (pos[3][0] + 4.2, pos[3][1] - 4.2)  # 4th star displaced ~6 px
    assert tracker.update(_green_with_stars(pos)) == 3  # bad anchor dropped
    assert tracker.anchors_rejected == 1
    assert abs(tracker.transform.rotation_deg - ang) < 0.2
    assert tracker.residual_px < 0.5


def test_tracker_keeps_all_clean_anchors() -> None:
    tracker = ApertureTracker(_ANCHORS, _CENTER, search_r=6.0)
    pos = [_rotate(x, y, *_CENTER, 1.0) for x, y in _ANCHORS]
    assert tracker.update(_green_with_stars(pos)) == 3
    assert tracker.anchors_rejected == 0
    assert tracker.residual_px < 0.3


def test_tracker_rejects_anchor_that_fades_to_noise() -> None:
    """A lost guide star must not be re-centred on a weak background feature."""
    anchors = [(30.0, 30.0), (90.0, 35.0), (60.0, 95.0), (95.0, 85.0)]
    tracker = ApertureTracker(anchors, _CENTER, search_r=8.0)
    assert tracker.update(_green_with_stars(anchors, peak=8000.0)) == 4

    angle = 1.0
    positions = [_rotate(x, y, *_CENTER, angle) for x, y in anchors]
    # The last source is replaced by a feature with 5% of its guide signal:
    # it remains centroidable but is not a reliable anchor.
    frame = _green_with_stars(positions[:3], peak=8000.0)
    frame += _green_with_stars([positions[3]], peak=400.0, sky=0.0)
    assert tracker.update(frame) == 3
    assert tracker.anchors_signal_rejected == 1
    assert abs(tracker.transform.rotation_deg - angle) < 0.2
