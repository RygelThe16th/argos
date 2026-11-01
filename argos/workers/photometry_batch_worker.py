"""PhotometryBatchWorker — re-run differential photometry over saved FITS (WS7).

The one genuinely-unique capability of the deleted Photometry Setup window:
measure a whole folder of already-saved subs against a target set, off the UI
thread. Uses the SAME measurement core as the live path
(:func:`argos.core.photometry.params.measure_frame` → ``measure_targets``), so
comps get their catalog magnitudes and the physics is identical; the only
intentional live/batch difference is the aperture FWHM: this worker measures the
field FWHM once, on the first readable frame, and holds it for the whole series.
Only when no stars are detected does it fall through to ``DEFAULT_FWHM`` — which
gives a *wider* aperture than the floor, not the floor itself.

Emits progress + a per-frame point stream and writes the canonical 9-column
CSVs (:meth:`LightCurve.to_csv`) so Analyze can reload the result. Cancellable
between frames.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

from argos.core.catalog.targets import ROLE_CHECK, ROLE_COMPARISON, TargetSet
from argos.core.imaging.green import green_plane
from argos.core.photometry.airmass import airmass_at, bjd_tdb, julian_date
from argos.core.photometry.lightcurve import LcPoint, LightCurve
from argos.core.photometry.params import PhotometryParams, measure_frame
from argos.core.photometry.tracking import ApertureTracker, TrackedWCS, select_tracking_anchors
from argos.core.photometry.uncertainty import apply_systematic_floor, estimate_systematic_floor
from argos.core.session.diagnostics import SessionDiagnostics
from argos.core.session.types import PhotometryPoint

logger = logging.getLogger(__name__)


@dataclass
class BatchRequest:
    """Inputs for one batch re-run."""

    fits_paths: list[Path]
    wcs: object  # a solved FrameWCS shared by every frame (the reference solve)
    target_set: TargetSet
    params: PhotometryParams
    out_dir: Path  # where the per-target 9-column CSVs are written
    object_name: str = "untitled"
    site: tuple[float | None, float | None, float] = (None, None, 0.0)  # lat, lon, elev
    diagnostics: bool = True  # flight recorder (diagnostics.jsonl in out_dir, P11)


@dataclass
class BatchResult:
    """Outcome of a batch run. ``error`` is empty on success."""

    curves: dict[str, LightCurve] = field(default_factory=dict)
    frames_done: int = 0
    error: str = ""
    rotation_deg: float = 0.0  # accumulated field rotation vs the reference solve
    shift_px: float = 0.0  # accumulated pointing drift vs the reference solve

    @property
    def ok(self) -> bool:
        return not self.error


class PhotometryBatchWorker(QThread):
    """Measure a list of saved FITS against a target set, off the UI thread.

    Signals:
        progress(int, int): frames done, total.
        point(object): a :class:`PhotometryPoint` per measured target/frame.
        finished_batch(object): a :class:`BatchResult` (check ``.ok``).
    """

    progress = pyqtSignal(int, int)
    point = pyqtSignal(object)
    finished_batch = pyqtSignal(object)

    def __init__(self, request: BatchRequest, parent=None) -> None:
        super().__init__(parent)
        self._req = request
        self._cancel = False
        self._tracker: ApertureTracker | None = None
        self._check_keys: set[str] = set()  # curve keys that are check stars (P2)
        self._series_fwhm: float | None = None  # measured once, fixed all series (P7)

    def cancel(self) -> None:
        """Ask the run to stop at the next frame boundary."""
        self._cancel = True

    def run(self) -> None:
        req = self._req
        curves: dict[str, LightCurve] = {}
        total = len(req.fits_paths)
        done = 0
        safe_obj = "".join(c if c.isalnum() or c in "-_" else "_" for c in req.object_name)
        diag = SessionDiagnostics(
            req.out_dir / f"{safe_obj or 'batch'}_diagnostics.jsonl",
            enabled=req.diagnostics,
        )
        try:
            diag.record(
                "event",
                what="batch_start",
                object=req.object_name,
                n_frames=total,
                track_apertures=req.params.track_apertures,
                band=req.params.band,
            )
            for fpath in req.fits_paths:
                if self._cancel:
                    break
                self._process_frame(diag, fpath, done, curves)
                done += 1
                self.progress.emit(done, total)
            self._apply_systematic_floors(curves)
            self._write_csvs(curves)
            result = BatchResult(curves=curves, frames_done=done)
            for key in sorted(self._check_keys):
                lc = curves.get(key)
                if lc is None or len(lc.points) < 2:
                    continue
                mags = [p.mag for p in lc.points]
                mean = sum(mags) / len(mags)
                k_rms = (sum((m - mean) ** 2 for m in mags) / (len(mags) - 1)) ** 0.5
                logger.info("Check star %s: RMS %.4f mag over %d frames", key, k_rms, len(mags))
                diag.record("event", what="check_rms", star=key, rms_mag=k_rms, n=len(mags))
            if self._tracker is not None:
                result.rotation_deg = self._tracker.transform.rotation_deg
                result.shift_px = self._tracker.transform.shift_px
                logger.info(
                    "Batch tracking: field rotated %.2f°, drifted %.1f px over %d frames",
                    result.rotation_deg,
                    result.shift_px,
                    done,
                )
            diag.record(
                "event",
                what="batch_end",
                frames_done=done,
                rotation_deg=result.rotation_deg,
                shift_px=result.shift_px,
            )
            self.finished_batch.emit(result)
        except Exception as exc:  # pragma: no cover - safety net
            logger.exception("Batch photometry crashed")
            diag.record("event", what="batch_crash", error=str(exc))
            self.finished_batch.emit(BatchResult(curves=curves, frames_done=done, error=str(exc)))
        finally:
            diag.close()

    # ------------------------------------------------------------------

    def _process_frame(self, diag, fpath: Path, frame_no: int, curves) -> None:
        """Measure one frame: tracker update, diagnostics records, points."""
        arr, jd = self._read_frame(fpath)
        if arr is None:
            return
        green = green_plane(arr)
        wcs = self._wcs_for_frame(green, fpath.name)
        if self._series_fwhm is None:
            self._series_fwhm = self._measure_series_fwhm(arr, diag)
        diag.record("frame", frame=frame_no, file=fpath.name, jd_utc=jd, fwhm=self._series_fwhm)
        if self._tracker is not None:
            diag.record(
                "tracking",
                frame=frame_no,
                anchors=self._tracker.anchors_used,
                anchors_rejected=self._tracker.anchors_rejected or None,
                residual_px=self._tracker.residual_px,
                rotation_deg=self._tracker.transform.rotation_deg,
                shift_px=self._tracker.transform.shift_px,
                frames_lost=self._tracker.frames_lost,
            )
        results = measure_frame(
            green,
            wcs,
            self._req.target_set,
            self._req.params,
            fwhm=self._series_fwhm,
            on_star=lambda s, phot, x, y: diag.star(frame_no, s, phot, x, y),
        )
        for res in results:
            if res.diff is not None:
                diag.record(
                    "ensemble",
                    frame=frame_no,
                    target=res.star.auid or res.star.display_name,
                    zp=res.diff.zero_point,
                    zp_rms=res.diff.zp_rms,
                    comps_used=res.diff.comps_used,
                    note=res.diff.note or None,
                )
        self._emit_points(results, jd, curves)

    def _measure_series_fwhm(self, raw, diag) -> float | None:
        """Field FWHM from the first readable frame — one aperture per series (P7).

        Time-series practice: a single radius for the whole run, sized from
        the measured seeing, instead of the aperture_min_px floor (saved subs
        carry no FWHM header). None when no stars are detected, which makes the
        aperture fall back to ``DEFAULT_FWHM`` — wider than the floor.
        """
        from argos.core.imaging.metrics import detect_stars

        try:
            fwhm = detect_stars(raw).mean_fwhm
        except Exception:  # pragma: no cover - defensive: bad first frame
            fwhm = None
        r_ap = self._req.params.aperture_px(fwhm)
        logger.info("Series FWHM %.2f px → aperture %.1f px", fwhm or -1, r_ap)
        diag.record("event", what="aperture", fwhm=fwhm, r_ap=r_ap)
        return fwhm

    def _wcs_for_frame(self, green, fname: str):
        """Reference WCS, rotation/drift-corrected by the tracker when it locks.

        The tracker is built lazily on the first frame (it needs the frame
        shape for its pivot) and keeps its last good transform on a frame with
        no usable anchor (clouds), so a later frame can re-acquire.
        """
        if not self._req.params.track_apertures:
            return self._req.wcs
        if self._tracker is None:
            self._tracker = self._build_tracker(green)
        self._tracker.update(green)
        if not self._tracker.anchors_used:
            logger.warning("%s: no anchor star found — apertures unguided", fname)
        return TrackedWCS(self._req.wcs, self._tracker)

    def _build_tracker(self, green) -> ApertureTracker:
        """Anchor the tracker on the stable stars of the set (comps + checks).

        A target's *position* is as stable, but a variable near minimum makes a
        poor centroid — only fall back to targets when there is nothing else.
        """
        tset = self._req.target_set
        candidates = tset.by_role(ROLE_COMPARISON) + tset.by_role(ROLE_CHECK)
        anchors = select_tracking_anchors(green, self._req.wcs, candidates, self._req.params)
        if not anchors:
            anchors = list(tset.stars)
        xy = [self._req.wcs.world_to_pixel_deg(s.ra_deg, s.dec_deg) for s in anchors]
        h, w = green.shape
        search_r = max(8.0, 2.0 * self._req.params.aperture_px(None))
        return ApertureTracker(
            [(float(x), float(y)) for x, y in xy],
            frame_center=(w / 2.0, h / 2.0),
            search_r=search_r,
        )

    def _emit_points(self, results, jd: float | None, curves: dict[str, LightCurve]) -> None:
        """Convert one frame's measurements into curve points + point signals.

        ``results`` carry targets, check stars (P2) and leave-one-out
        comparison vetting curves; checks flow into their own curves/CSVs and
        their keys are remembered for the K-RMS summary (comps are not).
        """
        for res in results:
            # Keep a point when *either* product exists. Requiring the flux
            # ratio discarded good differential magnitudes whenever every
            # comparison was flagged suspect (the ratio path rejects those,
            # the magnitude path does not).
            has_mag = res.diff is not None and res.diff.mag is not None
            has_ratio = res.relative is not None and res.relative.flux_ratio is not None
            if not (has_mag or has_ratio):
                continue
            if res.star.role == ROLE_CHECK:
                self._check_keys.add(res.star.auid or res.star.display_name)
            bjd = self._bjd(jd, res.star)
            pt = LcPoint(
                jd_utc=jd or 0.0,
                mag=res.diff.mag if res.diff and res.diff.mag is not None else float("nan"),
                mag_err=(
                    res.diff.mag_err if res.diff and res.diff.mag_err is not None else float("nan")
                ),
                bjd_tdb=bjd,
                airmass=self._airmass(jd, res.star),
                fwhm=self._series_fwhm,
                sky_adu=res.phot.sky_adu if res.phot else None,
                comps_used=res.diff.comps_used if res.diff else 0,
                relative_comps_used=res.relative.comps_used if res.relative else 0,
                saturated=bool(res.phot and res.phot.saturated),
                suspect=bool(res.phot and res.phot.suspect),
                formal_mag_err=(
                    res.diff.formal_mag_err or res.diff.mag_err
                    if res.diff and res.diff.mag_err is not None
                    else None
                ),
                relative_flux=res.relative.flux_ratio if res.relative else None,
                relative_flux_err=res.relative.flux_ratio_err if res.relative else None,
            )
            key = res.star.auid or res.star.display_name
            lc = curves.setdefault(
                key,
                LightCurve(
                    auid=res.star.auid or "", name=res.star.display_name, role=res.star.role
                ),
            )
            lc.append(pt)
            self.point.emit(
                PhotometryPoint(
                    key=key,
                    name=res.star.display_name,
                    jd=pt.jd_utc,
                    mag=pt.mag,
                    mag_err=pt.mag_err,
                    saturated=pt.saturated,
                    role=res.star.role,
                    relative_flux=pt.relative_flux,
                    relative_flux_err=pt.relative_flux_err,
                )
            )

    @staticmethod
    def _apply_systematic_floors(curves: dict[str, LightCurve]) -> None:
        """Apply the shared star_var_script uncertainty model after a batch."""
        for curve in curves.values():
            floor = estimate_systematic_floor(
                [
                    (point.jd_utc, point.mag, point.formal_mag_err or point.mag_err)
                    for point in curve.points
                    if math.isfinite(point.mag)
                    and math.isfinite(point.formal_mag_err or point.mag_err)
                ]
            )
            if floor is None:
                continue
            for point in curve.points:
                if not math.isfinite(point.mag):
                    continue
                formal = point.formal_mag_err if point.formal_mag_err is not None else point.mag_err
                point.mag_err = apply_systematic_floor(formal, floor)
                point.sigma_syst = floor.sigma_systematic

    def _bjd(self, jd: float | None, star) -> float | None:
        """BJD_TDB for a star at this frame's JD, or None without site/time."""
        lat, lon, elev = self._req.site
        if jd is None or lat is None or lon is None:
            return None
        return bjd_tdb(jd, star.ra_deg, star.dec_deg, float(lat), float(lon), float(elev))

    def _airmass(self, jd: float | None, star) -> float | None:
        """Per-star airmass at this frame's JD (P4), or None without site/time.

        Delegates to :func:`airmass_at`, which the live path now uses too, so
        the two never drift apart.
        """
        lat, lon, _elev = self._req.site
        if jd is None or lat is None or lon is None:
            return None
        return airmass_at(jd, star.ra_deg, star.dec_deg, float(lat), float(lon))

    @staticmethod
    def _read_frame(fpath: Path) -> tuple[np.ndarray | None, float | None]:
        """Read a FITS frame → (float32 array, exposure-midpoint JD or None).

        DATE-OBS is the exposure *start*; half of EXPTIME is added (P5) so a
        30 s sub doesn't carry a 15 s systematic timing bias. Without EXPTIME
        the start JD is kept (and the bias with it — a WARN says so).
        """
        from astropy.io import fits

        try:
            with fits.open(fpath) as hdul:
                data = hdul[0].data
                header = hdul[0].header
        except Exception:
            return None, None
        if data is None:
            return None, None
        arr = np.nan_to_num(np.asarray(data, dtype=np.float32))
        jd = None
        date_str = str(header.get("DATE-OBS", "") or "")
        if date_str:
            try:
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                jd = julian_date(dt)
            except ValueError:
                jd = None
        if jd is not None:
            try:
                jd += float(header["EXPTIME"]) / 2.0 / 86400.0
            except (KeyError, TypeError, ValueError):
                logger.warning("%s: no EXPTIME — JD is exposure start, not midpoint", fpath.name)
        return arr, jd

    def _write_csvs(self, curves: dict[str, LightCurve]) -> None:
        """Write each curve as the canonical 9-column CSV keyed by object+star."""
        for lc in curves.values():
            tag = lc.auid or lc.name or "photometry"
            safe = "".join(
                c if c.isalnum() or c in "-_" else "_" for c in f"{self._req.object_name}_{tag}"
            )
            try:
                lc.to_csv(self._req.out_dir / f"{safe or 'photometry'}.csv")
            except OSError as exc:
                logger.warning("batch photometry.csv: %s", exc)
