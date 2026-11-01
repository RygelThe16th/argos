"""PhotometryBatchWorker — offline re-run over a folder of synthetic FITS (WS7).

Guards the batch path's contract: it uses the shared measurement core (so comps
get their catalog mags), emits progress, honours cancel, and writes the
canonical 9-column CSVs that Analyze reloads. Runs the QThread synchronously via
``run()`` to keep the test deterministic (no event loop needed).
"""

from __future__ import annotations

import numpy as np
import pytest
from astropy.io import fits

from argos.core.catalog.targets import ROLE_COMPARISON, ROLE_TARGET, TargetSet, TargetStar
from argos.core.photometry.lightcurve import LightCurve
from argos.core.photometry.params import PhotometryParams
from argos.workers.photometry_batch_worker import (
    BatchRequest,
    BatchResult,
    PhotometryBatchWorker,
)


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    from PyQt6.QtCore import QCoreApplication

    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


class _FakeWCS:
    """Maps (ra_deg, dec_deg) → a preset green-px (x, y)."""

    def __init__(self, mapping):
        self._m = mapping

    def world_to_pixel_deg(self, ra_deg, dec_deg):
        return self._m[(ra_deg, dec_deg)]


def _raw_with_green_stars(green_positions_peaks, sky=200.0, sigma=1.5, green_shape=(60, 60)):
    """Build a raw GRBG frame whose green plane has stars at the given px.

    ``green_plane`` averages G1=raw[0::2,0::2] and G2=raw[1::2,1::2] → a green px
    (gx, gy) maps to raw (2*gx, 2*gy). We paint both greens with the same PSF.
    """
    gh, gw = green_shape
    raw = np.full((gh * 2, gw * 2), sky, dtype=np.float32)
    yy, xx = np.mgrid[0:gh, 0:gw]
    plane = np.zeros((gh, gw), dtype=np.float32)
    for (cx, cy), peak in green_positions_peaks:
        plane += peak * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma**2))
    raw[0::2, 0::2] += plane  # G1
    raw[1::2, 1::2] += plane  # G2
    return raw


def _write_fits(path, raw, date_obs):
    hdu = fits.PrimaryHDU(np.clip(raw, 0, 65535).astype(np.uint16))
    hdu.header["DATE-OBS"] = date_obs
    hdu.header["OBJECT"] = "TST Tau"
    hdu.writeto(path)


@pytest.fixture
def _scene(tmp_path):
    wcs = _FakeWCS(
        {
            (1.0, 1.0): (30.0, 30.0),  # target
            (2.0, 2.0): (10.0, 10.0),  # comp 1
            (3.0, 3.0): (50.0, 50.0),  # comp 2
        }
    )
    ts = TargetSet(object_name="TST Tau")
    ts.set_role(TargetStar(role=ROLE_TARGET, ra_deg=1.0, dec_deg=1.0, auid="T"))
    ts.set_role(
        TargetStar(role=ROLE_COMPARISON, ra_deg=2.0, dec_deg=2.0, auid="C1", mags={"V": 11.0})
    )
    ts.set_role(
        TargetStar(role=ROLE_COMPARISON, ra_deg=3.0, dec_deg=3.0, auid="C2", mags={"V": 11.0})
    )
    stars = [((30.0, 30.0), 20000.0), ((10.0, 10.0), 8000.0), ((50.0, 50.0), 8000.0)]
    paths = []
    for i in range(3):
        raw = _raw_with_green_stars(stars)
        p = tmp_path / f"sub_{i:03d}.fits"
        _write_fits(p, raw, f"2026-07-05T0{i}:00:00")
        paths.append(p)
    out_dir = tmp_path / "targets"
    params = PhotometryParams.from_config(lambda k, d: d)  # all config defaults
    req = BatchRequest(
        fits_paths=paths,
        wcs=wcs,
        target_set=ts,
        params=params,
        out_dir=out_dir,
        object_name="TST Tau",
    )
    return req, out_dir


def test_batch_measures_and_writes_uncertainty_aware_csv(_scene) -> None:
    req, out_dir = _scene
    seen = []
    worker = PhotometryBatchWorker(req)
    worker.point.connect(lambda pt: seen.append(pt))
    result_box = []
    worker.finished_batch.connect(result_box.append)
    worker.run()  # synchronous — no event loop

    assert result_box and isinstance(result_box[0], BatchResult)
    result = result_box[0]
    assert result.ok
    assert result.frames_done == 3
    # One target curve + one leave-one-out vetting curve per comparison.
    assert len(result.curves) == 3
    assert {lc.role for lc in result.curves.values()} == {"target", "comparison"}
    curve = result.curves["T"]
    assert curve.role == "target" and len(curve.points) == 3
    assert len(seen) == 9  # a point signal per star per frame
    # The target is brighter than the V=11 comps → calibrated mag < 11.
    assert curve.points[0].mag < 11.0
    # Each comp vetted against the other one recovers its own catalog mag.
    comp_curve = result.curves["C1"]
    assert comp_curve.role == "comparison"
    assert abs(comp_curve.points[0].mag - 11.0) < 0.1

    # Canonical uncertainty-aware CSVs written (target + comps) and reloadable.
    csvs = list(out_dir.glob("*.csv"))
    assert len(csvs) == 3
    header = csvs[0].read_text().splitlines()[0]
    assert header == (
        "jd_utc,bjd_tdb,mag,mag_err,formal_mag_err,sigma_syst,airmass,fwhm,sky_adu,"
        "comps_used,relative_flux,relative_flux_err,relative_comps_used,"
        "saturated,suspect"
    )
    reloaded = LightCurve.from_csv(csvs[0])
    assert len(reloaded.points) == 3


def test_batch_cancel_stops_early(_scene) -> None:
    req, _ = _scene
    worker = PhotometryBatchWorker(req)
    worker.cancel()  # cancel before running → no frames processed
    result_box = []
    worker.finished_batch.connect(result_box.append)
    worker.run()
    assert result_box[0].ok
    assert result_box[0].frames_done == 0
    assert result_box[0].curves == {}


def test_batch_reports_progress(_scene) -> None:
    req, _ = _scene
    worker = PhotometryBatchWorker(req)
    steps = []
    worker.progress.connect(lambda done, total: steps.append((done, total)))
    worker.run()
    assert steps == [(1, 3), (2, 3), (3, 3)]


# ── Field rotation (alt-az) — apertures must follow the rotating field ──────


def _rotate(x, y, cx, cy, deg):
    import math

    a = math.radians(deg)
    dx, dy = x - cx, y - cy
    return (
        cx + math.cos(a) * dx - math.sin(a) * dy,
        cy + math.sin(a) * dx + math.cos(a) * dy,
    )


def _rotating_scene(tmp_path, total_deg=10.0, n_frames=10, track=True):
    """Frames whose star field rotates around the green-frame centre.

    The reference WCS is exact for frame 0 only — the alt-az session case.
    """
    import dataclasses

    ref = {
        (1.0, 1.0): (30.0, 30.0),  # target
        (2.0, 2.0): (90.0, 35.0),  # comp 1
        (3.0, 3.0): (60.0, 95.0),  # comp 2
    }
    center = (60.0, 60.0)
    ts = TargetSet(object_name="ROT Tau")
    ts.set_role(TargetStar(role=ROLE_TARGET, ra_deg=1.0, dec_deg=1.0, auid="T"))
    ts.set_role(
        TargetStar(role=ROLE_COMPARISON, ra_deg=2.0, dec_deg=2.0, auid="C1", mags={"V": 11.0})
    )
    ts.set_role(
        TargetStar(role=ROLE_COMPARISON, ra_deg=3.0, dec_deg=3.0, auid="C2", mags={"V": 11.0})
    )
    paths = []
    for i in range(n_frames):
        ang = total_deg * i / (n_frames - 1)
        stars = [
            (_rotate(30.0, 30.0, *center, ang), 20000.0),
            (_rotate(90.0, 35.0, *center, ang), 8000.0),
            (_rotate(60.0, 95.0, *center, ang), 8000.0),
        ]
        raw = _raw_with_green_stars(stars, green_shape=(120, 120))
        p = tmp_path / f"rot_{i:03d}.fits"
        _write_fits(p, raw, f"2026-07-05T00:{i:02d}:00")
        paths.append(p)
    params = PhotometryParams.from_config(lambda k, d: d)
    if not track:
        params = dataclasses.replace(params, track_apertures=False)
    return BatchRequest(
        fits_paths=paths,
        wcs=_FakeWCS(ref),
        target_set=ts,
        params=params,
        out_dir=tmp_path / "targets",
        object_name="ROT Tau",
    )


def test_batch_tracks_field_rotation(tmp_path) -> None:
    req = _rotating_scene(tmp_path, total_deg=10.0, n_frames=10, track=True)
    result_box = []
    worker = PhotometryBatchWorker(req)
    worker.finished_batch.connect(result_box.append)
    worker.run()

    result = result_box[0]
    assert result.ok and result.frames_done == 10
    # The tracker measured the session's rotation…
    assert abs(result.rotation_deg - 10.0) < 0.5
    # …and the target stayed in its aperture on every frame, at a steady mag.
    curve = next(iter(result.curves.values()))
    assert len(curve.points) == 10
    mags = [p.mag for p in curve.points]
    assert max(mags) - min(mags) < 0.05


def test_batch_without_tracking_corrupts_the_rotating_curve(tmp_path) -> None:
    """Control: the same rotating scene, tracking off — the reference WCS
    alone cannot follow the field, so the apertures slide off the stars and
    the light curve acquires a spurious fade (~0.4 mag here, a plausible
    'eclipse'). This is the artefact the tracker exists to remove."""
    req = _rotating_scene(tmp_path, total_deg=10.0, n_frames=10, track=False)
    result_box = []
    worker = PhotometryBatchWorker(req)
    worker.finished_batch.connect(result_box.append)
    worker.run()

    result = result_box[0]
    assert result.ok and result.rotation_deg == 0.0  # no tracker ran
    curve = next(iter(result.curves.values()))
    mags = [p.mag for p in curve.points]
    assert max(mags) - min(mags) > 0.2  # constant star reads as variable


def test_batch_writes_diagnostics_jsonl(tmp_path) -> None:
    """P11: the flight recorder captures every comp's raw behaviour per frame,
    the ensemble zero-point and the tracker state, in parseable JSONL."""
    import json

    req = _rotating_scene(tmp_path, total_deg=10.0, n_frames=10, track=True)
    worker = PhotometryBatchWorker(req)
    result_box = []
    worker.finished_batch.connect(result_box.append)
    worker.run()

    diag_files = list((tmp_path / "targets").glob("*_diagnostics.jsonl"))
    assert len(diag_files) == 1
    docs = [json.loads(line) for line in diag_files[0].read_text().splitlines()]

    by_kind = {}
    for d in docs:
        by_kind.setdefault(d["kind"], []).append(d)

    # One star record per star (1 target + 2 comps) per frame.
    assert len(by_kind["star"]) == 3 * 10
    comp_records = [d for d in by_kind["star"] if d["role"] == "comparison"]
    assert len(comp_records) == 2 * 10
    assert all("inst_mag" in d and "x" in d and "y" in d for d in comp_records)

    # Ensemble health per calibrated star per frame — the target against the
    # full ensemble (2 comps) + each comp leave-one-out (1 comp).
    assert len(by_kind["ensemble"]) == 3 * 10
    target_records = [d for d in by_kind["ensemble"] if d["comps_used"] == 2]
    assert len(target_records) == 10
    assert len(by_kind["tracking"]) == 10
    final_rot = by_kind["tracking"][-1]["rotation_deg"]
    assert abs(final_rot - result_box[0].rotation_deg) < 1.5  # last-frame vs final
    assert len(by_kind["frame"]) == 10
    whats = [d["what"] for d in by_kind["event"]]
    assert whats[0] == "batch_start" and whats[-1] == "batch_end"


def test_batch_diagnostics_can_be_disabled(tmp_path) -> None:
    import dataclasses

    req = _rotating_scene(tmp_path, total_deg=2.0, n_frames=3, track=True)
    req = dataclasses.replace(req, diagnostics=False)
    worker = PhotometryBatchWorker(req)
    worker.run()
    assert not list((tmp_path / "targets").glob("*_diagnostics.jsonl"))


def test_batch_exports_the_check_star_curve(tmp_path) -> None:
    """P2: the K star gets its own curve + CSV and a check_rms summary event."""
    import dataclasses
    import json

    from argos.core.catalog.targets import ROLE_CHECK

    req = _rotating_scene(tmp_path, total_deg=2.0, n_frames=5, track=True)
    ts = req.target_set
    # Promote comp C2 (at green px 60,95) to check star.
    ts.set_role(TargetStar(role=ROLE_CHECK, ra_deg=3.0, dec_deg=3.0, auid="K1", mags={"V": 11.0}))
    req = dataclasses.replace(req, target_set=ts)

    worker = PhotometryBatchWorker(req)
    result_box = []
    worker.finished_batch.connect(result_box.append)
    worker.run()

    result = result_box[0]
    assert result.ok
    assert "K1" in result.curves  # the K curve exists…
    k_curve = result.curves["K1"]
    assert len(k_curve.points) == 5
    k_mags = [p.mag for p in k_curve.points]
    assert max(k_mags) - min(k_mags) < 0.05  # …and the constant K star is flat

    csv_names = [p.name for p in (tmp_path / "targets").glob("*.csv")]
    assert any("K1" in n for n in csv_names)

    diag_file = next((tmp_path / "targets").glob("*_diagnostics.jsonl"))
    events = [
        json.loads(line)
        for line in diag_file.read_text().splitlines()
        if json.loads(line)["kind"] == "event"
    ]
    rms_events = [e for e in events if e["what"] == "check_rms"]
    assert len(rms_events) == 1 and rms_events[0]["star"] == "K1"
    assert rms_events[0]["rms_mag"] < 0.05


def test_batch_jd_is_exposure_midpoint(tmp_path) -> None:
    """P5: DATE-OBS is exposure start; EXPTIME/2 must be added to the JD."""
    raw = _raw_with_green_stars([((30.0, 30.0), 20000.0)])
    p = tmp_path / "sub.fits"
    hdu = fits.PrimaryHDU(np.clip(raw, 0, 65535).astype(np.uint16))
    hdu.header["DATE-OBS"] = "2026-07-05T00:00:00"
    hdu.header["EXPTIME"] = 30.0
    hdu.writeto(p)

    _, jd = PhotometryBatchWorker._read_frame(p)
    from argos.core.photometry.airmass import julian_date
    from datetime import datetime, timezone

    start_jd = julian_date(datetime(2026, 7, 5, 0, 0, 0, tzinfo=timezone.utc))
    assert abs(jd - (start_jd + 15.0 / 86400.0)) < 1e-9


def test_batch_points_carry_airmass_when_site_known(tmp_path) -> None:
    """P4: with a site configured, every batch point gets a Pickering airmass."""

    # Circumpolar target/comps (dec≈+89) — always above the horizon from 46°N.
    ref = {
        (10.0, 89.0): (30.0, 30.0),
        (20.0, 89.1): (10.0, 10.0),
        (30.0, 89.2): (50.0, 50.0),
    }
    ts = TargetSet(object_name="POLE")
    ts.set_role(TargetStar(role=ROLE_TARGET, ra_deg=10.0, dec_deg=89.0, auid="T"))
    ts.set_role(
        TargetStar(role=ROLE_COMPARISON, ra_deg=20.0, dec_deg=89.1, auid="C1", mags={"V": 11.0})
    )
    ts.set_role(
        TargetStar(role=ROLE_COMPARISON, ra_deg=30.0, dec_deg=89.2, auid="C2", mags={"V": 11.0})
    )
    stars = [((30.0, 30.0), 20000.0), ((10.0, 10.0), 8000.0), ((50.0, 50.0), 8000.0)]
    paths = []
    for i in range(2):
        p = tmp_path / f"sub_{i}.fits"
        _write_fits(p, _raw_with_green_stars(stars), f"2026-07-05T0{i}:00:00")
        paths.append(p)
    req = BatchRequest(
        fits_paths=paths,
        wcs=_FakeWCS(ref),
        target_set=ts,
        params=PhotometryParams.from_config(lambda k, d: d),
        out_dir=tmp_path / "targets",
        object_name="POLE",
        site=(46.0, 6.0, 400.0),
    )
    result_box = []
    worker = PhotometryBatchWorker(req)
    worker.finished_batch.connect(result_box.append)
    worker.run()

    curve = next(iter(result_box[0].curves.values()))
    for pt in curve.points:
        assert pt.airmass is not None and 1.0 <= pt.airmass < 3.0
        assert pt.bjd_tdb is not None


def test_batch_aperture_sized_from_measured_fwhm(tmp_path) -> None:
    """P7: the series aperture comes from the first frame's measured FWHM
    (σ=1.5 px Gaussians → FWHM ≈ 3.5 px → r_ap ≈ 8.8 px), not the 4 px floor,
    and stays fixed for the whole run."""
    import json

    req = _rotating_scene(tmp_path, total_deg=2.0, n_frames=4, track=True)
    worker = PhotometryBatchWorker(req)
    worker.run()

    diag_file = next((tmp_path / "targets").glob("*_diagnostics.jsonl"))
    docs = [json.loads(line) for line in diag_file.read_text().splitlines()]
    (ap_event,) = [d for d in docs if d["kind"] == "event" and d["what"] == "aperture"]
    assert ap_event["fwhm"] is not None and 2.5 < ap_event["fwhm"] < 4.5
    assert ap_event["r_ap"] > 6.0  # 2.5 × FWHM, not the 4 px floor
    # Every frame reports the same frozen FWHM.
    frame_fwhms = {d.get("fwhm") for d in docs if d["kind"] == "frame"}
    assert frame_fwhms == {ap_event["fwhm"]}


# --------------------------------------------------------------------------- #
# Regressions from the 0.4.1 documentation review
# --------------------------------------------------------------------------- #


def _res(mag, ratio, *, diff_comps=0, rel_comps=0, suspect=False):
    """A minimal stand-in for one TargetResult, as _emit_points consumes it."""
    from types import SimpleNamespace

    return SimpleNamespace(
        star=SimpleNamespace(
            auid="000-AAA-001",
            display_name="XX Cyg",
            role=ROLE_TARGET,
            ra_deg=300.0,
            dec_deg=40.0,
        ),
        diff=(
            None
            if mag is None
            else SimpleNamespace(mag=mag, mag_err=0.02, formal_mag_err=0.02, comps_used=diff_comps)
        ),
        relative=(
            None
            if ratio is None
            else SimpleNamespace(flux_ratio=ratio, flux_ratio_err=0.001, comps_used=rel_comps)
        ),
        phot=SimpleNamespace(sky_adu=200.0, saturated=False, suspect=suspect),
    )


def _worker_for_emit(_scene):
    req, _ = _scene
    return PhotometryBatchWorker(req)


def test_batch_keeps_a_magnitude_with_no_flux_ratio(_scene) -> None:
    """The ratio path rejects suspect comparisons; the magnitude path does not.

    A frame where every comparison was flagged suspect used to lose its
    differential magnitude entirely — no row, no warning.
    """
    worker = _worker_for_emit(_scene)
    curves: dict = {}
    worker._emit_points([_res(mag=10.5, ratio=None, diff_comps=3)], 2451545.0, curves)

    assert len(curves) == 1
    point = next(iter(curves.values())).points[0]
    assert point.mag == 10.5
    assert point.relative_flux is None
    assert point.comps_used == 3


def test_batch_comps_used_counts_the_magnitude_ensemble(_scene) -> None:
    """The two ensembles are filtered differently and must be reported apart."""
    worker = _worker_for_emit(_scene)
    curves: dict = {}
    worker._emit_points([_res(mag=10.5, ratio=0.42, diff_comps=4, rel_comps=2)], 2451545.0, curves)

    point = next(iter(curves.values())).points[0]
    assert point.comps_used == 4  # behind `mag`
    assert point.relative_comps_used == 2  # behind `relative_flux`


def test_batch_records_the_suspect_flag(_scene) -> None:
    """Recorded, never acted on — the observer decides."""
    worker = _worker_for_emit(_scene)
    curves: dict = {}
    worker._emit_points([_res(mag=10.5, ratio=0.42, suspect=True)], 2451545.0, curves)

    point = next(iter(curves.values())).points[0]
    assert point.suspect is True
    assert point.mag == 10.5  # kept, not dropped


def test_batch_drops_only_a_result_with_neither_product(_scene) -> None:
    worker = _worker_for_emit(_scene)
    curves: dict = {}
    worker._emit_points([_res(mag=None, ratio=None)], 2451545.0, curves)
    assert curves == {}
