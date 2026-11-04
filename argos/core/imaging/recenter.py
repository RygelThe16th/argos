"""Plate-solve-referenced re-centering between sequence frames (opt-in).

An unguided mount drifts: on an EQ wedge the field walked ~90 green px/h in
one observed run until the comparison stars left the frame.  This module holds
the Qt-free decisions for pulling the target back:

- **where it should be** — the target's green-plane pixel position on the
  first measured frame of the run (the observer's framing, not the sensor
  centre), shifted by every deliberate dither offset;
- **where it is** — the target projected through the live tracked WCS, which
  is updated on every frame even when ASTAP fails;
- **how the mount responds** — a 2×2 matrix (green px per second of MoveAxis
  at a fixed rate) measured once with one short pulse per axis.  Nominal
  rates cannot be trusted: a ±180″ dither measured only ~18 px on one axis.

The worker executes the returned :class:`MoveRequest` between exposures; the
engine measures what actually happened with :func:`frame_shift` (phase
correlation of the frames either side of the move) and reports it back.

Off by default.  A background solve that moves the telescope is a policy
change from the manual "Center target" button, so it is always explicit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

MODE_OFF = "off"
MODE_THRESHOLD = "threshold"
MODE_EVERY_N = "every_n"
MODES: tuple[str, ...] = (MODE_OFF, MODE_THRESHOLD, MODE_EVERY_N)

KIND_CALIBRATE = "calibrate"
KIND_RECENTER = "recenter"
KIND_DITHER = "dither"

#: Phase-correlation peak significance (σ above the surface mean) below which
#: a measured shift is not trusted.
MIN_SHIFT_SIGNIFICANCE = 8.0
#: The correlation peak must stand this far above the next-best candidate —
#: two unrelated fields still line up a pair of stars somewhere by chance.
MIN_SHIFT_PEAK_RATIO = 2.0
#: A calibrated axis must move at least this many green px per second.
MIN_AXIS_RESPONSE_PX_S = 1.0
#: The two calibrated axes must be at least this far from parallel (degrees).
MIN_AXIS_ANGLE_DEG = 30.0
#: Pulses shorter than this are skipped (mount start/stop latency dominates).
MIN_PULSE_S = 0.05
#: Consecutive corrections that leave the drift no smaller before giving up.
MAX_FAILED_CORRECTIONS = 2


@dataclass(frozen=True)
class RecenterSettings:
    """``recenter.*`` configuration (see ``argos.core.config``)."""

    mode: str = MODE_OFF
    threshold_px: float = 20.0
    every_n: int = 30
    deadband_px: float = 2.0
    rate_deg_s: float = 0.05
    calibration_pulse_s: float = 1.0
    max_pulse_s: float = 5.0
    max_step_px: float = 60.0
    settle_s: float = 3.0

    @property
    def enabled(self) -> bool:
        return self.mode in (MODE_THRESHOLD, MODE_EVERY_N)

    @classmethod
    def from_config(cls, cfg) -> "RecenterSettings":
        mode = str(cfg("recenter.mode", MODE_OFF) or MODE_OFF)
        return cls(
            mode=mode if mode in MODES else MODE_OFF,
            threshold_px=max(1.0, float(cfg("recenter.threshold_px", 20.0))),
            every_n=max(1, int(cfg("recenter.every_n", 30))),
            deadband_px=max(0.0, float(cfg("recenter.deadband_px", 2.0))),
            rate_deg_s=max(0.001, float(cfg("recenter.rate_deg_s", 0.05))),
            calibration_pulse_s=max(0.1, float(cfg("recenter.calibration_pulse_s", 1.0))),
            max_pulse_s=max(0.1, float(cfg("recenter.max_pulse_s", 5.0))),
            max_step_px=max(1.0, float(cfg("recenter.max_step_px", 60.0))),
            settle_s=max(0.0, float(cfg("recenter.settle_s", 3.0))),
        )


@dataclass(frozen=True)
class MoveRequest:
    """One between-frames mount move for the sequence worker.

    ``durations_s`` are signed seconds per axis (primary, secondary) at
    ``rate_deg_s``; the sign selects the MoveAxis direction.
    """

    kind: str
    durations_s: tuple[float, float]
    rate_deg_s: float
    settle_s: float
    drift_px: tuple[float, float] | None = None
    reason: str = ""


@dataclass(frozen=True)
class FrameShift:
    """Translation of frame ``b`` relative to frame ``a`` (green px)."""

    dx: float
    dy: float
    significance: float
    peak_ratio: float = float("inf")

    @property
    def reliable(self) -> bool:
        return (
            self.significance >= MIN_SHIFT_SIGNIFICANCE and self.peak_ratio >= MIN_SHIFT_PEAK_RATIO
        )


def _prepare(plane: np.ndarray) -> np.ndarray:
    a = np.asarray(plane, dtype=np.float32)
    a = a - np.median(a)
    hi = float(np.percentile(a, 99.9))
    a = np.clip(a, 0.0, hi if hi > 0 else None)
    # Taper the borders so the frame edge doesn't correlate with itself.
    h, w = a.shape
    return a * np.outer(np.hanning(h), np.hanning(w)).astype(np.float32)


def frame_shift(a: np.ndarray, b: np.ndarray) -> FrameShift:
    """Measure how far the star field moved from ``a`` to ``b``.

    Phase correlation with a parabolic sub-pixel peak; independent of the
    tracker's search radius, so it survives a move of any size short of
    losing the field.  A positive ``dx`` means stars moved to larger x.
    """
    if a.shape != b.shape:
        return FrameShift(0.0, 0.0, 0.0)
    fa, fb = np.fft.rfft2(_prepare(a)), np.fft.rfft2(_prepare(b))
    cross = fb * np.conj(fa)
    surface = np.fft.irfft2(cross / (np.abs(cross) + 1e-9), s=a.shape)
    iy, ix = np.unravel_index(int(np.argmax(surface)), surface.shape)
    h, w = surface.shape
    peak = float(surface[iy, ix])
    std = float(surface.std())
    significance = (peak - float(surface.mean())) / std if std > 0 else 0.0
    rolled = np.roll(surface, (h // 2 - iy, w // 2 - ix), axis=(0, 1))
    rolled[h // 2 - 4 : h // 2 + 5, w // 2 - 4 : w // 2 + 5] = -np.inf
    runner_up = float(rolled.max())
    peak_ratio = peak / runner_up if runner_up > 0 else float("inf")

    def _sub(m1: float, c: float, p1: float) -> float:
        den = m1 - 2.0 * c + p1
        return 0.5 * (m1 - p1) / den if den != 0 else 0.0

    sx = _sub(surface[iy, (ix - 1) % w], peak, surface[iy, (ix + 1) % w])
    sy = _sub(surface[(iy - 1) % h, ix], peak, surface[(iy + 1) % h, ix])
    dx = ix if ix <= w // 2 else ix - w
    dy = iy if iy <= h // 2 else iy - h
    return FrameShift(float(dx + sx), float(dy + sy), float(significance), float(peak_ratio))


@dataclass
class AxisCalibration:
    """Green px moved per second of MoveAxis on each axis (columns)."""

    axis0: tuple[float, float]
    axis1: tuple[float, float]
    rate_deg_s: float

    def problem(self) -> str | None:
        """Why this calibration can't steer both axes, or ``None``."""
        n0, n1 = math.hypot(*self.axis0), math.hypot(*self.axis1)
        for name, n in (("primary (RA)", n0), ("secondary (Dec)", n1)):
            if n < MIN_AXIS_RESPONSE_PX_S:
                return f"{name} axis moved only {n:.2f} px/s"
        cos = (self.axis0[0] * self.axis1[0] + self.axis0[1] * self.axis1[1]) / (n0 * n1)
        angle = math.degrees(math.acos(max(-1.0, min(1.0, abs(cos)))))
        if angle < MIN_AXIS_ANGLE_DEG:
            return f"axes move the field almost in parallel ({angle:.0f}° apart)"
        return None

    def durations_for(self, shift_x: float, shift_y: float) -> tuple[float, float]:
        """Signed seconds per axis that move the field by ``(shift_x, shift_y)``."""
        m = np.array([[self.axis0[0], self.axis1[0]], [self.axis0[1], self.axis1[1]]])
        t = np.linalg.solve(m, np.array([shift_x, shift_y], dtype=float))
        return float(t[0]), float(t[1])

    def to_dict(self) -> dict:
        return {
            "axis0_px_s": list(self.axis0),
            "axis1_px_s": list(self.axis1),
            "rate_deg_s": self.rate_deg_s,
        }


@dataclass
class RecenterEvent:
    """Something the engine should tell the observer (level, message)."""

    level: str
    message: str


@dataclass
class Recenterer:
    """Per-run drift bookkeeping and move decisions.

    Call order per measured frame: :meth:`after_move` (only for the first
    frame after a worker-confirmed move), then :meth:`observe`.
    """

    settings: RecenterSettings
    reference: tuple[float, float] | None = None
    calibration: AxisCalibration | None = None
    disabled_reason: str | None = None
    frames_since_correction: int = 0
    _calib_columns: list[tuple[float, float]] = field(default_factory=list)
    _awaiting_move: bool = False
    _last_drift: tuple[float, float] | None = None
    _check_residual: bool = False
    _failed_corrections: int = 0

    @property
    def active(self) -> bool:
        return self.settings.enabled and self.disabled_reason is None

    @property
    def awaiting_move(self) -> bool:
        return self._awaiting_move

    def reset(self) -> None:
        """New run or new target: forget the reference (keep the calibration)."""
        self.reference = None
        self.frames_since_correction = 0
        self._awaiting_move = False
        self._last_drift = None
        self._check_residual = False
        self._failed_corrections = 0
        self._calib_columns = []

    def move_dropped(self) -> None:
        """The worker never executed the request (run ended, mount error)."""
        self._awaiting_move = False

    def after_move(
        self, kind: str, durations_s: tuple[float, float], shift: FrameShift | None
    ) -> list[RecenterEvent]:
        """Account for a move the worker executed before this frame."""
        self._awaiting_move = False
        events: list[RecenterEvent] = []
        if shift is None or not shift.reliable:
            sig = f" (significance {shift.significance:.1f})" if shift else ""
            events.append(
                RecenterEvent("WARN", f"Recenter: could not measure the {kind} move{sig}")
            )
            if kind == KIND_DITHER:
                self.reference = None  # re-anchor on this frame's framing
            if kind == KIND_CALIBRATE:
                self._calib_columns = []
            return events
        if kind == KIND_DITHER:
            if self.reference is not None:
                self.reference = (self.reference[0] + shift.dx, self.reference[1] + shift.dy)
            return events
        if kind == KIND_CALIBRATE:
            axis = len(self._calib_columns)
            seconds = durations_s[axis] if axis < 2 else 0.0
            if seconds == 0:
                self._calib_columns = []
                return events
            self._calib_columns.append((shift.dx / seconds, shift.dy / seconds))
            events.append(
                RecenterEvent(
                    "INFO",
                    f"Recenter: calibration axis {axis} moved ({shift.dx:+.1f}, "
                    f"{shift.dy:+.1f}) px in {seconds:+.2f}s",
                )
            )
            if len(self._calib_columns) == 2:
                cal = AxisCalibration(
                    self._calib_columns[0], self._calib_columns[1], self.settings.rate_deg_s
                )
                self._calib_columns = []
                problem = cal.problem()
                if problem:
                    self.disabled_reason = f"calibration failed — {problem}"
                    events.append(
                        RecenterEvent("WARN", f"Recenter disabled: {self.disabled_reason}")
                    )
                else:
                    self.calibration = cal
                    events.append(
                        RecenterEvent(
                            "OK",
                            "Recenter: calibrated — axis 0 "
                            f"({cal.axis0[0]:+.1f}, {cal.axis0[1]:+.1f}) px/s, axis 1 "
                            f"({cal.axis1[0]:+.1f}, {cal.axis1[1]:+.1f}) px/s",
                        )
                    )
            return events
        # KIND_RECENTER: judged on this frame's drift in observe().
        self._check_residual = True
        return events

    def observe(
        self, target_xy: tuple[float, float] | None, tracking_ok: bool = True
    ) -> tuple[MoveRequest | None, list[RecenterEvent]]:
        """Judge one measured frame; maybe return the next move to make."""
        events: list[RecenterEvent] = []
        if not self.active or self._awaiting_move:
            return None, events
        if target_xy is None or not tracking_ok:
            return None, events
        x, y = float(target_xy[0]), float(target_xy[1])
        if self.reference is None:
            self.reference = (x, y)
            self.frames_since_correction = 0
            events.append(
                RecenterEvent("INFO", f"Recenter: holding the target at ({x:.1f}, {y:.1f}) px")
            )
            return None, events
        self.frames_since_correction += 1
        dx, dy = x - self.reference[0], y - self.reference[1]
        drift = math.hypot(dx, dy)

        if self._check_residual:
            self._check_residual = False
            before = math.hypot(*self._last_drift) if self._last_drift else float("inf")
            events.append(
                RecenterEvent(
                    "INFO",
                    f"Recenter: drift {before:.1f} px → {drift:.1f} px after the correction",
                )
            )
            if drift >= before:
                self._failed_corrections += 1
                if self._failed_corrections >= MAX_FAILED_CORRECTIONS:
                    self.disabled_reason = (
                        f"{self._failed_corrections} corrections in a row did not reduce the drift"
                    )
                    events.append(
                        RecenterEvent("WARN", f"Recenter disabled: {self.disabled_reason}")
                    )
                    return None, events
            else:
                self._failed_corrections = 0

        if not self._due(drift):
            return None, events

        if self.calibration is None:
            request = self._calibration_request()
            events.append(
                RecenterEvent(
                    "INFO",
                    f"Recenter: drift {drift:.1f} px — calibrating mount axis "
                    f"{len(self._calib_columns)}",
                )
            )
            return request, events

        # Move the field by −drift, capped to max_step_px.
        scale = min(1.0, self.settings.max_step_px / drift) if drift > 0 else 1.0
        t0, t1 = self.calibration.durations_for(-dx * scale, -dy * scale)
        cap = self.settings.max_pulse_s
        if max(abs(t0), abs(t1)) > cap:
            shrink = cap / max(abs(t0), abs(t1))
            t0, t1 = t0 * shrink, t1 * shrink
        t0 = t0 if abs(t0) >= MIN_PULSE_S else 0.0
        t1 = t1 if abs(t1) >= MIN_PULSE_S else 0.0
        if t0 == 0.0 and t1 == 0.0:
            return None, events
        self._awaiting_move = True
        self._last_drift = (dx, dy)
        self.frames_since_correction = 0
        return (
            MoveRequest(
                kind=KIND_RECENTER,
                durations_s=(t0, t1),
                rate_deg_s=self.settings.rate_deg_s,
                settle_s=self.settings.settle_s,
                drift_px=(dx, dy),
                reason=f"drift ({dx:+.1f}, {dy:+.1f}) px",
            ),
            events,
        )

    def _due(self, drift: float) -> bool:
        s = self.settings
        if self.calibration is None and self._calib_columns:
            return True  # finish a calibration that is half done
        if s.mode == MODE_THRESHOLD:
            return drift > s.threshold_px
        if s.mode == MODE_EVERY_N:
            return self.frames_since_correction >= s.every_n and drift > s.deadband_px
        return False

    def _calibration_request(self) -> MoveRequest:
        axis = len(self._calib_columns)
        seconds = self.settings.calibration_pulse_s
        durations = (seconds, 0.0) if axis == 0 else (0.0, seconds)
        self._awaiting_move = True
        return MoveRequest(
            kind=KIND_CALIBRATE,
            durations_s=durations,
            rate_deg_s=self.settings.rate_deg_s,
            settle_s=self.settings.settle_s,
            reason=f"calibrate axis {axis}",
        )
