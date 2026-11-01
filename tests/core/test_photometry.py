"""Tests for comparison-star ranking (differential photometry selection)."""

from __future__ import annotations

from argos.core.catalog import (
    Band,
    ComparisonQuality,
    ComparisonStar,
    VariableStar,
    auto_comparison_stars,
    comparisons_for_variable,
    rank_comparisons,
    separation_arcmin,
)
from argos.core.catalog.targets import ROLE_COMPARISON


def _comp(auid: str, ra: float, dec: float, v: float | None) -> ComparisonStar:
    bands = (Band("V", v),) if v is not None else ()
    return ComparisonStar(
        auid=auid, ra_deg=ra, dec_deg=dec, label=str(int((v or 0) * 10)), bands=bands
    )


def test_separation_arcmin() -> None:
    assert separation_arcmin(10.0, 20.0, 10.0, 20.0) == 0.0
    # 1° of declination = 60 arcmin.
    assert abs(separation_arcmin(10.0, 20.0, 10.0, 21.0) - 60.0) < 1e-6
    # 1 arcmin of RA at the equator ≈ 1 arcmin separation.
    assert abs(separation_arcmin(0.0, 0.0, 1.0 / 60.0, 0.0) - 1.0) < 1e-3


def test_rank_orders_by_separation() -> None:
    target_ra, target_dec = 83.8, -5.4
    far = _comp("FAR", 83.8, -5.0, 12.0)  # 0.4° away
    near = _comp("NEAR", 83.81, -5.40, 12.2)  # ~0.01° away
    mid = _comp("MID", 83.8, -5.25, 11.8)  # 0.15° away
    ranked = rank_comparisons(target_ra, target_dec, [far, near, mid])
    assert [s.star.auid for s in ranked] == ["NEAR", "MID", "FAR"]
    assert ranked[0].separation_arcmin < ranked[1].separation_arcmin < ranked[2].separation_arcmin


def test_rank_mag_tolerance_filters_and_reports_delta() -> None:
    target_ra, target_dec = 0.0, 0.0
    close_mag = _comp("CLOSE", 0.02, 0.0, 12.3)  # |12.3 - 12.0| = 0.3
    far_mag = _comp("FARMAG", 0.01, 0.0, 9.0)  # |9 - 12| = 3.0, dropped by tol
    no_band = _comp("NOBAND", 0.005, 0.0, None)  # unjudgeable in V → dropped under tol
    ranked = rank_comparisons(
        target_ra, target_dec, [close_mag, far_mag, no_band], target_mag=12.0, mag_tol=1.0
    )
    assert [s.star.auid for s in ranked] == ["CLOSE"]
    assert abs(ranked[0].delta_mag - 0.3) < 1e-9


def test_rank_cap() -> None:
    comps = [_comp(f"C{i}", 0.0, i / 100.0, 12.0) for i in range(1, 6)]
    ranked = rank_comparisons(0.0, 0.0, comps, max_results=2)
    assert len(ranked) == 2
    assert ranked[0].star.auid == "C1"  # closest


def test_comparisons_for_variable_uses_brightest_mag() -> None:
    var = VariableStar(name="V", ra_deg=0.0, dec_deg=0.0, max_mag="12.0 V", min_mag="14.0 V")
    good = _comp("GOOD", 0.02, 0.0, 12.4)  # within 1 mag of 12.0
    bad = _comp("BAD", 0.01, 0.0, 15.0)  # 3 mag off → dropped
    ranked = comparisons_for_variable(var, [good, bad], mag_tol=1.0)
    assert [s.star.auid for s in ranked] == ["GOOD"]


class _GridWCS:
    """world_to_pixel_deg that maps degrees to green px at 100 px/°."""

    def world_to_pixel_deg(self, ra_deg: float, dec_deg: float):
        return ra_deg * 100.0, dec_deg * 100.0


def test_auto_comparison_stars_ranks_converts_and_caps() -> None:
    comps = [_comp(f"C{i}", i / 100.0, 0.1, 12.0) for i in range(1, 8)]
    picks = auto_comparison_stars(0.0, 0.1, comps, count=5)
    assert len(picks) == 5
    assert [p.auid for p in picks] == ["C1", "C2", "C3", "C4", "C5"]  # closest first
    assert all(p.role == ROLE_COMPARISON and p.source == "vsp_auto" for p in picks)
    # Catalog magnitudes ride along for the differential ensemble.
    assert picks[0].mags == {"V": 12.0}
    assert picks[0].name is None
    assert picks[0].display_name == comps[0].auid


def test_auto_comparison_stars_keep_the_vsp_chart() -> None:
    comp = ComparisonStar(
        auid="C1",
        ra_deg=0.01,
        dec_deg=0.1,
        label="120",
        bands=(Band("V", 12.0),),
        chart_id="X42585ESI",
    )
    [pick] = auto_comparison_stars(0.0, 0.1, [comp], count=1)
    assert pick.chart_id == "X42585ESI"


def test_auto_comparison_stars_drops_off_frame_stars() -> None:
    # Target near the frame edge (100 px/° on a 100×100 frame): the closest
    # comparison projects outside the frame and must be skipped — an
    # off-frame star can't be measured — while farther in-frame ones make it.
    off = _comp("OFF", 1.06, 0.1, 12.0)  # x = 106 px → outside (+2 px margin)
    on1 = _comp("ON1", 0.85, 0.1, 12.1)
    on2 = _comp("ON2", 0.75, 0.1, 12.2)
    picks = auto_comparison_stars(
        0.95, 0.1, [off, on1, on2], wcs=_GridWCS(), green_shape=(100, 100), count=5
    )
    assert [p.auid for p in picks] == ["ON1", "ON2"]


def test_auto_comparisons_use_pilot_quality_not_only_proximity() -> None:
    """A faint nearest star must lose to a measurable wider-field candidate."""
    close_faint = _comp("FAINT", 0.01, 0.1, 13.8)
    wide_good = _comp("GOOD", 0.40, 0.1, 10.4)
    nearer_good = _comp("GOOD2", 0.08, 0.1, 10.6)
    quality = {
        "FAINT": ComparisonQuality(snr=1.2, inst_mag=-5.0),
        "GOOD": ComparisonQuality(snr=22.0, inst_mag=-9.1),
        "GOOD2": ComparisonQuality(snr=18.0, inst_mag=-9.0),
    }

    picks = auto_comparison_stars(
        0.0,
        0.1,
        [close_faint, wide_good, nearer_good],
        candidate_quality=quality,
        target_inst_mag=-8.2,
        min_snr=10.0,
        max_delta_inst_mag=1.5,
        count=5,
    )

    assert [star.auid for star in picks] == ["GOOD", "GOOD2"]


def test_auto_comparisons_reject_remote_stars_even_when_bright() -> None:
    close = _comp("CLOSE", 0.08, 0.1, 10.6)
    remote = _comp("REMOTE", 0.80, 0.1, 10.4)
    quality = {
        "CLOSE": ComparisonQuality(snr=18.0, inst_mag=-9.0),
        "REMOTE": ComparisonQuality(snr=30.0, inst_mag=-9.1),
    }
    picks = auto_comparison_stars(
        0.0,
        0.1,
        [close, remote],
        candidate_quality=quality,
        target_inst_mag=-8.2,
        min_snr=10.0,
        max_delta_inst_mag=1.5,
        max_separation_arcmin=25.0,
        count=5,
    )
    assert [star.auid for star in picks] == ["CLOSE"]
