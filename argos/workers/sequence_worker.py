"""SequenceWorker — executes a multi-step acquisition plan in a QThread.

Consumes a :class:`~argos.core.imaging.sequencer.SequencePlan`, expands it
to frames, drives the camera/filter wheel, and writes science-grade FITS files
into the Siril-compatible session folder. All blocking work (network, disk,
polling) happens here, off the UI thread; the UI subscribes to the signals.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QThread, pyqtSignal

from argos.core.alpaca.camera import Camera
from argos.core.alpaca.client import AlpacaError
from argos.core.alpaca.filterwheel import FilterWheel, position_names
from argos.core.alpaca.telescope import Telescope
from argos.core.imaging.fits_writer import FITSWriter, FrameContext
from argos.core.imaging.metrics import detect_stars, frame_metrics
from argos.core.imaging.sequencer import (
    FrameSpec,
    SequencePlan,
    expand_plan,
    total_frames,
)
from argos.core.imaging.session_log import (
    SESSION_FILENAME,
    FrameRecord,
    SessionLog,
)

logger = logging.getLogger(__name__)

OBSERVATION_FILENAME = "observation.json"

#: Extra time on top of the exposure before a frame is considered timed out.
_DOWNLOAD_MARGIN_S = 15.0
#: Polling interval while waiting for ImageReady.
_POLL_MS = 200
#: Max time to wait for the filter wheel to settle after a move.
_FILTER_SETTLE_S = 20.0
#: Max time to wait for the controller to resume after an autofocus request —
#: a lost AF must never hang the night's run.
_AF_RESUME_TIMEOUT_S = 120.0
#: Dither move: axis rate (deg/s) and pulse duration — a few arcminutes of
#: offset, enough to move stars across pixels without losing the field.
_DITHER_RATE_DEG_S = 0.05
_DITHER_PULSE_S = 1.0
#: Settle time after a dither move before the next exposure.
_DITHER_SETTLE_S = 3.0

#: Callback that builds a fresh FrameContext for the current frame. Called with
#: keyword args ``object_name`` and ``filter_name``; returns a FrameContext.
FrameContextProvider = Callable[..., FrameContext]


def _resolve_filter_position(filter_name: str) -> int | None:
    """Best-effort map of a filter name to a wheel position index.

    Slots come from the active telescope profile, so a model with a different
    wheel resolves against its own names. Returns ``None`` when no match is
    found — including for a model with no internal wheel at all.
    """
    try:
        for idx, name in position_names().items():
            if str(name).lower() == filter_name.lower():
                return int(idx)
    except Exception:  # pragma: no cover - defensive against unexpected shapes
        return None
    return None


class SequenceWorker(QThread):
    """Runs a :class:`SequencePlan` to completion (or until stopped).

    Signals:
        step_started(int, object):       step_index, SequenceStep — a new step begins.
        frame_started(int, int, object): frames_done, total, FrameSpec — before exposing.
        frame_saved(str, object):        absolute path, FrameRecord — a frame was written.
        progress(int, int, float):       frames_done, total, eta_seconds.
        autofocus_due():                 the controller should run autofocus, then call
                                         :meth:`resume_after_autofocus`.
        mount_nudged(str, object):       kind ("dither" | "calibrate" | "recenter"),
                                         info dict — the mount moved between frames
                                         and has settled; emitted before the next
                                         exposure starts.
        error_occurred(str):             a recoverable/terminal error message.
        finished(bool):                  True if the plan completed fully, else False.
    """

    step_started = pyqtSignal(int, object)
    frame_started = pyqtSignal(int, int, object)
    frame_saved = pyqtSignal(str, object)
    progress = pyqtSignal(int, int, float)
    frame_image = pyqtSignal(object)  # full uint16 array, for live display
    autofocus_due = pyqtSignal()
    mount_nudged = pyqtSignal(str, object)
    paused = pyqtSignal(bool)  # the run is holding at a frame boundary
    error_occurred = pyqtSignal(str)
    finished = pyqtSignal(bool)

    def __init__(
        self,
        camera: Camera,
        telescope: Telescope | None,
        filterwheel: FilterWheel | None,
        plan: SequencePlan,
        frame_context_provider: FrameContextProvider,
        base_dir: Path,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._camera = camera
        self._telescope = telescope
        self._filterwheel = filterwheel
        self._plan = plan
        self._make_context = frame_context_provider
        self._base_dir = Path(base_dir)
        self._stop_flag = False
        self._resume = threading.Event()
        # Pause gate: set = run, cleared = hold at the next frame boundary.
        self._pause_gate = threading.Event()
        self._pause_gate.set()
        self._run_id = ""
        # One pending between-frames move (re-centering), set from the UI
        # thread and consumed at the next frame boundary.
        self._move_lock = threading.Lock()
        self._pending_move = None
        self._next_frame_move: str | None = None

    # ------------------------------------------------------------------ #
    # Control
    # ------------------------------------------------------------------ #

    def stop(self) -> None:
        """Request a clean stop; also unblocks an autofocus or pause wait."""
        self._stop_flag = True
        self._resume.set()
        self._pause_gate.set()

    def pause(self) -> None:
        """Hold the run at the next frame boundary (the current exposure
        completes and is saved — a half-exposed frame is worthless)."""
        self._pause_gate.clear()

    def resume(self) -> None:
        """Release a pause."""
        self._pause_gate.set()

    def is_paused(self) -> bool:
        return not self._pause_gate.is_set()

    def resume_after_autofocus(self) -> None:
        """Resume the loop after the controller has finished autofocus."""
        self._resume.set()

    def request_move(self, request) -> None:
        """Queue a :class:`~argos.core.imaging.recenter.MoveRequest`.

        Thread-safe. The move runs after the frame being exposed has been
        saved — never during an exposure. A newer request replaces an
        unexecuted one.
        """
        with self._move_lock:
            self._pending_move = request

    # ------------------------------------------------------------------ #
    # Thread body
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        completed = False
        try:
            completed = self._run()
        except AlpacaError as exc:
            self.error_occurred.emit(str(exc))
        except Exception as exc:  # pragma: no cover - safety net
            logger.exception("Sequence error")
            self.error_occurred.emit(str(exc))
        finally:
            self.finished.emit(completed)

    def _run(self) -> bool:
        total = total_frames(self._plan)
        done = 0
        started = time.monotonic()
        current_filter: str | None = None
        current_step = -1
        frames_since_af = 0
        self._session_log: SessionLog | None = None
        self._session_root: Path | None = None
        self._run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        logger.info("Sequence start: %d frame(s) → %s", total, self._base_dir)

        for spec in expand_plan(self._plan):
            if self._stop_flag or not self._wait_if_paused():
                return False

            if spec.step_index != current_step:
                current_step = spec.step_index
                self.step_started.emit(current_step, self._plan.steps[current_step])

            # Wheel move: Light/Flat go to the step's filter, Dark/Bias to the
            # opaque slot (spec.wheel_filter). Autofocus-on-change only makes
            # sense when the new slot passes light.
            wanted = spec.wheel_filter
            if self._filterwheel is not None and wanted != current_filter:
                self._set_filter(wanted)
                if (
                    spec.needs_filter
                    and current_filter is not None
                    and self._plan.autofocus_on_filter_change
                ):
                    self._await_autofocus()
                    frames_since_af = 0
                    if self._stop_flag:
                        return False
                current_filter = wanted

            # Periodic autofocus.
            if self._plan.autofocus_every_n > 0 and frames_since_af >= self._plan.autofocus_every_n:
                self._await_autofocus()
                frames_since_af = 0
                if self._stop_flag:
                    return False

            self.frame_started.emit(done, total, spec)
            frame_boundary = time.monotonic()
            path, record = self._shoot_one(spec)
            if path is None:
                return False  # stop requested or error already emitted

            done += 1
            frames_since_af += 1
            self.frame_saved.emit(path, record)

            elapsed = time.monotonic() - started
            avg = elapsed / done
            self.progress.emit(done, total, avg * (total - done))

            if spec.dither_every > 0 and done % spec.dither_every == 0:
                self._dither(done)
                if self._stop_flag:
                    return False

            if spec.is_light:
                self._apply_pending_move(done)
                if self._stop_flag:
                    return False

            wait_s = spec.interval_s
            if spec.cadence_s is not None:
                # Transit cadence is start-to-start. Acquisition/download time
                # consumes the budget; a slow camera is never hidden by adding
                # another idle delay after it.
                wait_s = max(0.0, spec.cadence_s - (time.monotonic() - frame_boundary))
            if wait_s > 0 and not self._interruptible_sleep(wait_s):
                return False

        return not self._stop_flag

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _dither(self, done: int = 0) -> None:
        """Nudge the mount a few arcminutes so stars land on fresh pixels.

        A short MoveAxis pulse on both axes with alternating sign, then a
        settle wait. No-op without a mount. Best-effort: a failed nudge must
        never abort the run.
        """
        if self._telescope is None:
            return
        self._dither_sign = -getattr(self, "_dither_sign", 1)
        sign = self._dither_sign
        try:
            self._telescope.move_axis(0, sign * _DITHER_RATE_DEG_S)
            self._telescope.move_axis(1, -sign * _DITHER_RATE_DEG_S)
            self._interruptible_sleep(_DITHER_PULSE_S)
            self._telescope.move_axis(0, 0.0)
            self._telescope.move_axis(1, 0.0)
            logger.info(
                "Dither: ±%.0f″ nudge, settling %.0fs",
                _DITHER_RATE_DEG_S * _DITHER_PULSE_S * 3600,
                _DITHER_SETTLE_S,
            )
            self._interruptible_sleep(_DITHER_SETTLE_S)
            self.mount_nudged.emit(
                "dither",
                {
                    "durations_s": (sign * _DITHER_PULSE_S, -sign * _DITHER_PULSE_S),
                    "rate_deg_s": _DITHER_RATE_DEG_S,
                    "next_frame": done + 1,
                },
            )
        except Exception:  # pragma: no cover - best effort, never abort the run
            logger.warning("Dither move failed — continuing without", exc_info=True)
            try:
                self._telescope.move_axis(0, 0.0)
                self._telescope.move_axis(1, 0.0)
            except Exception:
                pass

    def _apply_pending_move(self, done: int) -> None:
        """Run a queued re-centering move at this frame boundary.

        Both axes start together; each stops after its own duration. The
        settle wait follows, then :attr:`mount_nudged` tells the engine which
        frame is the first one after the move. Best-effort: a failed move is
        logged and the run continues.
        """
        with self._move_lock:
            request, self._pending_move = self._pending_move, None
        if request is None:
            return
        if self._telescope is None:
            self.mount_nudged.emit(request.kind, {"skipped": "no mount", "next_frame": done + 1})
            return
        rate = abs(float(request.rate_deg_s))
        pulses = [
            (abs(t), axis, math.copysign(rate, t))
            for axis, t in enumerate(request.durations_s)
            if t != 0.0
        ]
        try:
            for _, axis, axis_rate in pulses:
                self._telescope.move_axis(axis, axis_rate)
            elapsed = 0.0
            for seconds, axis, _ in sorted(pulses):
                self._interruptible_sleep(seconds - elapsed)
                elapsed = seconds
                self._telescope.move_axis(axis, 0.0)
            logger.info(
                "Recenter (%s): axis pulses %+.2fs / %+.2fs at %.3f°/s, settling %.0fs",
                request.kind,
                request.durations_s[0],
                request.durations_s[1],
                rate,
                request.settle_s,
            )
            self._interruptible_sleep(request.settle_s)
        except Exception as exc:
            logger.warning("Recenter move failed — continuing without", exc_info=True)
            for _, axis, _ in pulses:
                try:
                    self._telescope.move_axis(axis, 0.0)
                except Exception:
                    pass
            self.mount_nudged.emit(request.kind, {"skipped": str(exc), "next_frame": done + 1})
            return
        self._next_frame_move = request.kind
        self.mount_nudged.emit(
            request.kind,
            {
                "durations_s": tuple(request.durations_s),
                "rate_deg_s": rate,
                "next_frame": done + 1,
            },
        )

    def _wait_if_paused(self) -> bool:
        """Hold while the pause gate is down; False if stopped meanwhile."""
        if self._pause_gate.is_set():
            return True
        self.paused.emit(True)
        logger.info("Sequence paused")
        self._pause_gate.wait()  # released by resume() or stop()
        self.paused.emit(False)
        logger.info("Sequence resumed")
        return not self._stop_flag

    def _set_filter(self, filter_name: str) -> None:
        pos = _resolve_filter_position(filter_name)
        if pos is None:
            logger.warning("No wheel position matches filter '%s' — skipping", filter_name)
            return
        self._filterwheel.set_position(pos)
        # The move is async (the wheel reports position -1 while turning); wait
        # for it to settle so we never expose mid-rotation.
        deadline = time.monotonic() + _FILTER_SETTLE_S
        while self._filterwheel.get_position() == -1:
            if self._stop_flag or time.monotonic() > deadline:
                break
            self.msleep(_POLL_MS)
        logger.info("Filter → %s (position %d)", filter_name, pos)

    def _read_back_gain(self, requested: int) -> int:
        """Gain the driver reports after apply; requested when unreadable.

        A driver without Gain (ASCOM sim) leaves us nothing to verify — keep
        the requested value (the IMX585 noise-model headers need *a* gain) but
        say so. A readable driver that differs wins over the plan.
        """
        actual = self._camera.try_get_gain()
        if actual is None:
            logger.warning(
                "Gain %d not verifiable (driver has no Gain) — header unverified", requested
            )
            return requested
        if actual != requested:
            logger.warning(
                "Gain read-back %d != requested %d — headers record %d", actual, requested, actual
            )
        return actual

    def _read_back_filter(self, requested: str) -> str:
        """Filter name the wheel actually sits on; requested without a wheel."""
        if self._filterwheel is None:
            return requested
        try:
            actual = self._filterwheel.position_name()
        except Exception as exc:
            logger.warning("Filter read-back failed (%s) — header unverified", exc)
            return requested
        if actual == "Moving…" or not actual:
            logger.warning("Wheel still moving at exposure start — header unverified")
            return requested
        if actual.lower() != requested.lower():
            logger.warning(
                "Filter read-back '%s' != planned '%s' — headers record '%s'",
                actual,
                requested,
                actual,
            )
        return actual

    def _shoot_one(self, spec: FrameSpec) -> tuple[str | None, FrameRecord | None]:
        """Expose, wait for ImageReady, write FITS + session record.

        Returns ``(path, FrameRecord)``, or ``(None, None)`` on stop/timeout.
        """
        start_dt = datetime.now(timezone.utc)
        self._camera.set_gain(spec.gain)
        # P1 header truthfulness: read the achieved state back from the
        # devices — a header must never record a setting the hardware refused.
        gain_actual = self._read_back_gain(spec.gain)
        filter_actual = self._read_back_filter(spec.wheel_filter)
        self._camera.start_exposure(spec.exposure_s, light=spec.is_light)

        deadline = time.monotonic() + spec.exposure_s + _DOWNLOAD_MARGIN_S
        while not self._camera.is_image_ready():
            if self._stop_flag:
                self._safe_stop_exposure()
                return None, None
            if time.monotonic() > deadline:
                self.error_occurred.emit(
                    f"Exposure timeout after {spec.exposure_s + _DOWNLOAD_MARGIN_S:.0f}s"
                )
                return None, None
            self.msleep(_POLL_MS)

        arr = self._camera.get_image_array()
        end_dt = datetime.now(timezone.utc)
        self.frame_image.emit(arr)

        # Per-frame QA metrics (§7) — computed here so they land in the FITS
        # header *and* the session log. Best-effort: a bad frame must not abort.
        metrics = fwhm = ecc = None
        try:
            metrics = frame_metrics(arr)
            field = detect_stars(arr)
            fwhm, ecc = field.mean_fwhm, field.mean_eccentricity
        except Exception:  # pragma: no cover - metrics are best-effort
            logger.warning("Frame metrics failed — writing frame without QA", exc_info=True)

        ctx = self._make_context(object_name=self._plan.object_name, filter_name=filter_actual)
        self._software = ctx.software
        if metrics is not None:
            ctx.hfd = metrics.hfd
            ctx.star_count = metrics.star_count
            ctx.sky_adu = metrics.sky_adu
            ctx.fwhm = fwhm
            ctx.eccentricity = ecc

        folder = FITSWriter.session_folder(
            self._base_dir,
            self._plan.object_name,
            start_dt,
            spec.image_type,
            filter_actual,
            run_id=self._run_id,
        )
        filename = FITSWriter.build_filename(
            self._plan.object_name,
            spec.image_type,
            start_dt,
            spec.exposure_s,
            filter_actual,
            spec.frame_index,
            gain_actual,
        )
        path = folder / filename
        FITSWriter.write(
            arr,
            path,
            start_dt,
            end_dt,
            spec.exposure_s,
            gain_actual,
            spec.image_type,
            context=ctx,
            overwrite=False,
        )

        record = FrameRecord(
            filename=filename,
            image_type=spec.image_type,
            filter_name=filter_actual,
            exposure_s=spec.exposure_s,
            gain=gain_actual,
            timestamp=start_dt.isoformat(),
            hfd=metrics.hfd if metrics else None,
            fwhm=fwhm,
            star_count=metrics.star_count if metrics else None,
            sky_adu=round(metrics.sky_adu, 1) if metrics else None,
            peak_adu=metrics.peak_adu if metrics else None,
            eccentricity=ecc,
            mount_move=self._next_frame_move,
        )
        self._next_frame_move = None
        self._append_session(start_dt, record)
        return str(path), record

    def _append_session(self, start_dt: datetime, record: FrameRecord) -> None:
        """Add a frame to the session log and rewrite ``session.json`` atomically."""
        if self._session_log is None:
            self._session_root = FITSWriter.session_root(
                self._base_dir, self._plan.object_name, start_dt, run_id=self._run_id
            )
            self._session_log = SessionLog(
                object_name=self._plan.object_name,
                software=self._software,
                started_utc=start_dt.isoformat(),
            )
            self._write_observation_metadata()
        self._session_log.add(record)
        try:
            self._session_log.write(self._session_root / SESSION_FILENAME)
        except OSError:  # pragma: no cover - disk hiccup must not abort the run
            logger.warning("Could not write %s", SESSION_FILENAME, exc_info=True)

    def _write_observation_metadata(self) -> None:
        """Persist an optional science-planning hand-off beside the raw FITS.

        The data are a declared plan, not a post-processing result.  In
        particular, a transit sidecar preserves the published ephemeris and
        requested coverage without pretending that Argos fitted a transit.
        """
        if not self._plan.metadata or self._session_root is None:
            return
        payload = {
            "schema_version": 1,
            "object_name": self._plan.object_name,
            **self._plan.metadata,
        }
        try:
            path = self._session_root / OBSERVATION_FILENAME
            path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        except OSError:  # pragma: no cover - disk hiccup must not abort the run
            logger.warning("Could not write %s", OBSERVATION_FILENAME, exc_info=True)

    def _safe_stop_exposure(self) -> None:
        try:
            self._camera.stop_exposure()
        except Exception:  # pragma: no cover - best effort on abort
            pass

    def _await_autofocus(self) -> None:
        """Signal the controller and block until resumed (or stopped).

        Bounded wait: if the controller never calls
        :meth:`resume_after_autofocus` (lost AF worker, UI bug), abort the
        sequence after ``_AF_RESUME_TIMEOUT_S`` instead of hanging all night.
        """
        self._resume.clear()
        self.autofocus_due.emit()
        # Set by resume_after_autofocus() or stop(); False on timeout.
        if not self._resume.wait(_AF_RESUME_TIMEOUT_S):
            logger.error(
                "Autofocus handshake timed out after %.0fs — aborting sequence",
                _AF_RESUME_TIMEOUT_S,
            )
            self.error_occurred.emit(
                f"Autofocus did not resume within {_AF_RESUME_TIMEOUT_S:.0f}s — sequence aborted"
            )
            self._stop_flag = True

    def _interruptible_sleep(self, seconds: float) -> bool:
        """Sleep in small slices; return False if a stop was requested."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._stop_flag:
                return False
            self.msleep(_POLL_MS)
        return True
