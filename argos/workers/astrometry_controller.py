"""AstrometryController — the live page's solve lifecycle + auto-solve policy.

See ``notes/photometry_plan.md`` §4 (Workstream A3). A ``QObject`` that lives on the
UI thread and owns a :class:`SolveWorker`. It centralises *when* and *how* the live
frame is plate-solved so the page doesn't grow ad-hoc solve state:

- at most one solve in flight (new requests are dropped while busy);
- **auto mode** re-solves only when it's worth it — there's no WCS yet, the mount
  moved more than ``astrometry.live_resolve_arcmin``, or ``astrometry.live_resolve_s``
  elapsed (between solves a tracked grid stays valid, so we don't thrash);
- **a failed solve keeps the last good WCS** — the grid never blanks on one miss;
- live solves use a small search radius + bounded timeout and **skip the slow
  whole-sky blind retry** (via :func:`build_solve_settings`), so the cadence can't
  stall the UI.

Signals:
    solved(FrameWCS, WCSOverlay, str): a fresh solution + grid overlay + summary.
    failed(str): the last solve attempt failed (the previous WCS, if any, is kept).
    state(str): short human status for the page ("Plate-solving…", "Idle", …).
"""

from __future__ import annotations

import logging
import time

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal

from argos.core.imaging.astrometry_session import (
    build_solve_settings,
    full_res_scale,
    overlay_for,
    wcs_from_result,
)
from argos.core.imaging.debayer import VIEW_G, extract_plane
from argos.core.imaging.platesolve import angular_separation_deg
from argos.workers.solve_worker import SolveWorker

logger = logging.getLogger(__name__)

#: ``(ra_hours, dec_deg)`` of a pointing or target, or ``None``.
RaDec = tuple[float, float]


class AstrometryController(QObject):
    """Owns the live plate-solve lifecycle + auto-solve policy."""

    solved = pyqtSignal(object, object, str)  # FrameWCS, WCSOverlay, summary
    failed = pyqtSignal(str)
    state = pyqtSignal(str)

    def __init__(self, cfg_get, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cfg = cfg_get
        self._worker: SolveWorker | None = None
        self._auto = False
        self._wcs = None  # platesolve.FrameWCS (last good solution)
        self._green_shape: tuple[int, int] | None = None
        self._last_solve_monotonic = 0.0
        self._last_solve_radec: RaDec | None = None  # mount pointing at the last attempt
        self._pending_target: RaDec | None = None  # target for the in-flight solve's overlay
        # Argos itself nudged the mount (re-centering/dither): rebase the
        # movement check on the next reported position, and drop a solve of a
        # pre-move frame that is still in flight.
        self._rebase_mount = False
        self._discard_inflight = False

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def wcs(self):
        """The last good :class:`FrameWCS`, or ``None``."""
        return self._wcs

    @property
    def green_shape(self) -> tuple[int, int] | None:
        """Green shape of the frame the WCS was solved against — its pixel
        coordinates are only valid for frames of this geometry."""
        return self._green_shape

    def is_busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def set_auto(self, enabled: bool) -> None:
        """Arm/disarm per-frame auto-solving (an in-flight solve still finishes)."""
        self._auto = bool(enabled)
        self.state.emit("Auto-solve on" if self._auto else "Auto-solve off")

    @property
    def auto(self) -> bool:
        return self._auto

    def invalidate(self) -> None:
        """Drop the current WCS — a slew/goto makes the previous solution stale.

        The next frame is then immediately "due" (no WCS), so auto mode re-solves
        on the new pointing instead of drawing a grid that no longer matches.
        """
        self._wcs = None
        self._last_solve_radec = None
        self.state.emit("WCS cleared (slew)")

    # ------------------------------------------------------------------
    # Solve triggers
    # ------------------------------------------------------------------

    def solve_now(
        self,
        raw: np.ndarray,
        green_shape: tuple[int, int],
        mount_radec: RaDec | None = None,
        target_radec: RaDec | None = None,
    ) -> bool:
        """Force a solve (the manual button) — thorough settings, with blind retry.

        Returns ``False`` if a solve is already running.
        """
        if self.is_busy():
            return False
        self._start(raw, green_shape, mount_radec, target_radec, live=False)
        return True

    def on_new_frame(
        self,
        raw: np.ndarray,
        green_shape: tuple[int, int],
        mount_radec: RaDec | None = None,
        target_radec: RaDec | None = None,
    ) -> None:
        """Auto-solve hook — called per processed frame. No-op unless armed + due."""
        if not self._auto or self.is_busy() or raw is None:
            return
        if not self._due(mount_radec):
            return
        self._start(raw, green_shape, mount_radec, target_radec, live=True)

    def _due(self, mount_radec: RaDec | None) -> bool:
        if self._wcs is None:
            return True
        if (time.monotonic() - self._last_solve_monotonic) >= float(
            self._cfg("astrometry.live_resolve_s", 20.0)
        ):
            return True
        if mount_radec is not None and self._last_solve_radec is not None:
            sep_arcmin = (
                angular_separation_deg(
                    mount_radec[0],
                    mount_radec[1],
                    self._last_solve_radec[0],
                    self._last_solve_radec[1],
                )
                * 60.0
            )
            if sep_arcmin >= float(self._cfg("astrometry.live_resolve_arcmin", 2.0)):
                return True
        return False

    def note_internal_move(self) -> None:
        """Argos moved the mount by a known, measured amount between frames.

        The live tracker follows such a move, so the WCS stays usable — but
        the mount's reported pointing changed, which would otherwise read as
        an external slew and clear it. The next reported position becomes the
        new baseline, and an in-flight solve (of a pre-move frame) is dropped.
        """
        self._rebase_mount = True
        if self.is_busy():
            self._discard_inflight = True
        self._last_solve_monotonic = 0.0  # re-solve the first post-move frame

    def mount_moved_since_solution(self, mount_radec: RaDec | None) -> bool:
        """Whether the reported mount position makes the current WCS unsafe.

        ``DeviceSession.slewed`` covers GoTo commands issued by Argos itself,
        but an observer may slew from Stellarium or the telescope's native app.
        In that case retaining the former field's WCS is worse than showing no
        overlay: a target aperture can be drawn on empty sky.  Reuse the same
        movement threshold that arms an automatic re-solve.
        """
        if self._rebase_mount and mount_radec is not None:
            self._rebase_mount = False
            self._last_solve_radec = mount_radec
            return False
        if self._wcs is None or mount_radec is None or self._last_solve_radec is None:
            return False
        sep_arcmin = (
            angular_separation_deg(
                mount_radec[0], mount_radec[1], self._last_solve_radec[0], self._last_solve_radec[1]
            )
            * 60.0
        )
        return sep_arcmin >= float(self._cfg("astrometry.live_resolve_arcmin", 2.0))

    def _start(
        self,
        raw: np.ndarray,
        green_shape: tuple[int, int],
        mount_radec: RaDec | None,
        target_radec: RaDec | None,
        *,
        live: bool,
    ) -> None:
        green = extract_plane(raw, VIEW_G)
        settings = build_solve_settings(self._cfg, green_shape, live=live, mount_radec=mount_radec)
        self._green_shape = green_shape
        self._pending_target = target_radec
        self._last_solve_monotonic = time.monotonic()
        self._last_solve_radec = mount_radec
        self.state.emit("Plate-solving…")
        worker = SolveWorker(green, settings, parent=self)
        worker.solved.connect(self._on_worker_solved)
        # Reap the QThread once it finishes — without this every solve leaks a
        # parented SolveWorker (plus its frame copy) for the page's lifetime.
        worker.finished.connect(lambda w=worker: self._reap_worker(w))
        self._worker = worker
        worker.start()

    def _reap_worker(self, worker: SolveWorker) -> None:
        """Release a finished SolveWorker (its ``solved`` result, if any, was
        already delivered — ``finished`` is emitted after ``run()`` returns)."""
        if self._worker is worker:
            self._worker = None
        worker.deleteLater()

    # ------------------------------------------------------------------
    # Result
    # ------------------------------------------------------------------

    def _on_worker_solved(self, result) -> None:
        self.state.emit("Idle")
        if self._discard_inflight:
            self._discard_inflight = False
            logger.info("Discarding a solve of a frame taken before the mount moved")
            return
        if not result.solved:
            # Keep the previous WCS — a single miss must not blank the grid.
            self.failed.emit(result.message)
            if self._wcs is not None:
                self.state.emit("Solve failed — keeping last WCS")
            return
        wcs = wcs_from_result(result, self._green_shape)
        if wcs is None:
            self.failed.emit("could not build WCS from the solution")
            return
        self._wcs = wcs
        overlay = overlay_for(wcs, self._green_shape, self._cfg, target_radec=self._pending_target)
        self.solved.emit(wcs, overlay, self._summary(result))

    def _summary(self, result) -> str:
        bits = [f"Solved — RA {result.ra_hours:.4f}h", f"Dec {result.dec_deg:+.4f}°"]
        scale = full_res_scale(result)
        if scale is not None:
            bits.append(f"{scale:.2f}″/px")
        if result.rotation_deg is not None:
            bits.append(f"rot {result.rotation_deg:.1f}°")
        if self._pending_target is not None:
            sep = angular_separation_deg(
                result.ra_hours, result.dec_deg, self._pending_target[0], self._pending_target[1]
            )
            bits.append(f"Δtarget {sep * 60.0:.1f}′")
        return "  ".join(bits)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def wait(self, ms: int = 2000) -> None:
        """Block until an in-flight solve finishes (called on page shutdown)."""
        if self.is_busy():
            self._worker.wait(ms)
