"""Re-centering decisions: shift measurement, calibration, move requests."""

from __future__ import annotations

import math

import numpy as np
import pytest

from argos.core.imaging.recenter import (
    KIND_CALIBRATE,
    KIND_DITHER,
    KIND_RECENTER,
    MODE_EVERY_N,
    MODE_OFF,
    MODE_THRESHOLD,
    AxisCalibration,
    FrameShift,
    Recenterer,
    RecenterSettings,
    frame_shift,
)


def _star_field(shape=(240, 320), shift=(0.0, 0.0), n=60, seed=3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    h, w = shape
    xs, ys = rng.uniform(20, w - 20, n), rng.uniform(20, h - 20, n)
    flux = rng.uniform(200, 3000, n)
    yy, xx = np.mgrid[0:h, 0:w]
    img = np.full(shape, 100.0)
    for x, y, f in zip(xs + shift[0], ys + shift[1], flux):
        img += f * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * 1.2**2))
    return img + rng.normal(0, 3, shape)


def _reliable(dx, dy) -> FrameShift:
    return FrameShift(dx, dy, 50.0)


def _settings(**kw) -> RecenterSettings:
    base = dict(mode=MODE_THRESHOLD, threshold_px=20.0, settle_s=0.0)
    base.update(kw)
    return RecenterSettings(**base)


# A mount like the one measured: axis 0 moves the field mostly in -x,
# axis 1 mostly in +y (px per second at the calibration rate).
_CAL = AxisCalibration((-15.0, 2.0), (1.0, 18.0), 0.05)


@pytest.mark.parametrize("shift", [(0.0, 0.0), (7.3, -4.6), (-18.0, 3.0), (25.4, 19.8)])
def test_frame_shift_recovers_translation(shift):
    a = _star_field()
    b = _star_field(shift=shift)
    s = frame_shift(a, b)
    assert s.reliable
    assert s.dx == pytest.approx(shift[0], abs=0.3)
    assert s.dy == pytest.approx(shift[1], abs=0.3)


def test_frame_shift_flags_unrelated_frames():
    a = _star_field(seed=1)
    b = _star_field(seed=2)
    assert not frame_shift(a, b).reliable


def _fixed_pattern(shape=(240, 320), seed=11) -> np.ndarray:
    """Sensor pattern shared by every uncalibrated frame: hot pixels + banding."""
    rng = np.random.default_rng(seed)
    pattern = np.zeros(shape)
    ys, xs = rng.integers(0, shape[0], 400), rng.integers(0, shape[1], 400)
    pattern[ys, xs] = rng.uniform(300, 3000, 400)
    pattern += rng.normal(0, 4, shape[1])[None, :]  # column banding
    return pattern


def _faint_field(shift=(0.0, 0.0)) -> np.ndarray:
    # Thin cloud: few, faint stars, so the fixed pattern carries most power.
    return 100.0 + (_star_field(shift=shift, n=15, seed=5) - 100.0) * 0.15


def test_frame_shift_is_not_captured_by_the_sensor_pattern():
    pattern = _fixed_pattern()
    s = frame_shift(_faint_field() + pattern, _faint_field(shift=(6.0, -9.0)) + pattern)
    # Either the true shift, or an honest "can't tell" — never a confident 0.
    if s.reliable:
        assert s.dx == pytest.approx(6.0, abs=0.5)
        assert s.dy == pytest.approx(-9.0, abs=0.5)
    assert not (s.reliable and abs(s.dx) <= 1 and abs(s.dy) <= 1)


def test_pattern_only_frames_are_not_a_reliable_zero_shift():
    rng = np.random.default_rng(1)
    pattern = _fixed_pattern()
    a = 100.0 + pattern + rng.normal(0, 3, pattern.shape)
    b = 100.0 + pattern + rng.normal(0, 3, pattern.shape)
    assert not frame_shift(a, b).reliable


def test_settings_from_config_defaults_off_and_rejects_unknown_mode():
    assert not RecenterSettings.from_config(lambda k, d=None: d).enabled
    cfg = {"recenter.mode": "sometimes"}
    assert RecenterSettings.from_config(lambda k, d=None: cfg.get(k, d)).mode == MODE_OFF


def test_off_mode_never_requests_a_move():
    r = Recenterer(_settings(mode=MODE_OFF))
    r.observe((100.0, 100.0))
    req, _ = r.observe((300.0, 100.0))
    assert req is None


def test_first_frame_sets_reference_without_moving():
    r = Recenterer(_settings())
    req, events = r.observe((512.0, 300.0))
    assert req is None and r.reference == (512.0, 300.0)
    assert "holding" in events[0].message


def test_below_threshold_no_move_above_threshold_calibrates_first():
    r = Recenterer(_settings())
    r.observe((500.0, 300.0))
    assert r.observe((515.0, 300.0))[0] is None  # 15 px < 20 px
    req, _ = r.observe((525.0, 300.0))
    assert req is not None and req.kind == KIND_CALIBRATE
    assert req.durations_s == (1.0, 0.0)
    assert r.awaiting_move
    # While the move is outstanding nothing else is requested.
    assert r.observe((540.0, 300.0))[0] is None


def test_calibration_two_axes_then_correction_moves_against_drift():
    r = Recenterer(_settings())
    r.observe((500.0, 300.0))
    req, _ = r.observe((525.0, 300.0))
    # Axis 0 pulse: the field moved by the axis-0 column.
    r.after_move(KIND_CALIBRATE, req.durations_s, _reliable(*_CAL.axis0))
    req, _ = r.observe((525.0 + _CAL.axis0[0], 300.0 + _CAL.axis0[1]))
    assert req.kind == KIND_CALIBRATE and req.durations_s == (0.0, 1.0)
    events = r.after_move(KIND_CALIBRATE, req.durations_s, _reliable(*_CAL.axis1))
    assert r.calibration is not None
    assert any("calibrated" in e.message for e in events)
    x = 525.0 + _CAL.axis0[0] + _CAL.axis1[0]
    y = 300.0 + _CAL.axis0[1] + _CAL.axis1[1]
    req, _ = r.observe((x, y))
    assert req.kind == KIND_RECENTER
    # Applying the requested durations through the true response cancels the drift.
    t0, t1 = req.durations_s
    moved_x = _CAL.axis0[0] * t0 + _CAL.axis1[0] * t1
    moved_y = _CAL.axis0[1] * t0 + _CAL.axis1[1] * t1
    assert x + moved_x == pytest.approx(500.0, abs=1e-6)
    assert y + moved_y == pytest.approx(300.0, abs=1e-6)


def _calibrated(**kw) -> Recenterer:
    r = Recenterer(_settings(**kw))
    r.calibration = _CAL
    return r


def test_correction_capped_by_max_step():
    r = _calibrated(max_step_px=10.0, max_pulse_s=100.0)
    r.observe((500.0, 300.0))
    req, _ = r.observe((600.0, 300.0))
    t0, t1 = req.durations_s
    moved = math.hypot(
        _CAL.axis0[0] * t0 + _CAL.axis1[0] * t1, _CAL.axis0[1] * t0 + _CAL.axis1[1] * t1
    )
    assert moved == pytest.approx(10.0, abs=1e-6)


def test_correction_capped_by_max_pulse():
    r = _calibrated(max_step_px=1000.0, max_pulse_s=0.5)
    r.observe((500.0, 300.0))
    req, _ = r.observe((600.0, 300.0))
    assert max(abs(t) for t in req.durations_s) == pytest.approx(0.5)


def test_every_n_mode_corrects_on_cadence_outside_deadband():
    r = _calibrated(mode=MODE_EVERY_N, every_n=3, deadband_px=2.0)
    r.observe((500.0, 300.0))
    assert r.observe((503.0, 300.0))[0] is None
    assert r.observe((503.0, 300.0))[0] is None
    req, _ = r.observe((503.0, 300.0))
    assert req is not None and req.kind == KIND_RECENTER


def test_every_n_mode_skips_inside_deadband():
    r = _calibrated(mode=MODE_EVERY_N, every_n=1, deadband_px=2.0)
    r.observe((500.0, 300.0))
    assert r.observe((501.0, 300.5))[0] is None


def test_dither_shifts_the_reference():
    r = _calibrated()
    r.observe((500.0, 300.0))
    r.after_move(KIND_DITHER, (1.0, 1.0), _reliable(3.0, 19.0))
    assert r.reference == (503.0, 319.0)
    # The dithered position itself is not drift.
    assert r.observe((503.0, 319.0))[0] is None


def test_unmeasurable_dither_reanchors():
    r = _calibrated()
    r.observe((500.0, 300.0))
    events = r.after_move(KIND_DITHER, (1.0, 1.0), FrameShift(0, 0, 2.0))
    assert r.reference is None and events[0].level == "WARN"


def test_degenerate_calibration_disables():
    r = Recenterer(_settings())
    r.observe((500.0, 300.0))
    req, _ = r.observe((530.0, 300.0))
    r.after_move(KIND_CALIBRATE, req.durations_s, _reliable(0.2, 0.1))  # axis 0 barely moves
    req, _ = r.observe((530.0, 300.0))
    events = r.after_move(KIND_CALIBRATE, req.durations_s, _reliable(1.0, 18.0))
    assert r.calibration is None and not r.active
    assert "primary" in r.disabled_reason
    assert any(e.level == "WARN" for e in events)
    assert r.observe((600.0, 300.0))[0] is None


def test_two_failed_corrections_disable():
    r = _calibrated()
    r.observe((500.0, 300.0))
    for _ in range(2):
        req, _ = r.observe((530.0, 300.0))
        if req is None:
            break
        r.after_move(KIND_RECENTER, req.durations_s, _reliable(0, 0))
    # Residual after the second correction is still 30 px → disabled.
    r.observe((530.0, 300.0))
    assert not r.active
    assert "did not reduce" in r.disabled_reason


def test_successful_correction_reports_residual():
    r = _calibrated()
    r.observe((500.0, 300.0))
    req, _ = r.observe((530.0, 300.0))
    r.after_move(KIND_RECENTER, req.durations_s, _reliable(-30.0, 0.0))
    req, events = r.observe((500.5, 300.0))
    assert req is None and r.active
    assert any("30.0 px → 0.5 px" in e.message for e in events)


def test_reset_keeps_calibration():
    r = _calibrated()
    r.observe((500.0, 300.0))
    r.reset()
    assert r.reference is None and r.calibration is _CAL
