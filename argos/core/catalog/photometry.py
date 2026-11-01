"""Comparison-star selection for differential photometry (Qt-free).

Given a variable star (the target) and the field's VSP comparison stars, rank the
comparisons for photometry of that target. The standard differential-photometry
criteria are: comparisons **close** to the target (same region of the chip, same
airmass/vignetting) and of **similar brightness**. VSP already guarantees they
are non-variable and calibrated, so this module just scores + orders them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

from argos.core.imaging.astrometry_session import project_points

from .aavso import ComparisonStar, VariableStar
from .targets import ROLE_COMPARISON, TargetStar


def separation_arcmin(ra1_deg: float, dec1_deg: float, ra2_deg: float, dec2_deg: float) -> float:
    """Great-circle separation between two J2000 points, in arcminutes."""
    r1, d1, r2, d2 = (math.radians(v) for v in (ra1_deg, dec1_deg, ra2_deg, dec2_deg))
    a = (
        math.sin((d2 - d1) / 2.0) ** 2
        + math.cos(d1) * math.cos(d2) * math.sin((r2 - r1) / 2.0) ** 2
    )
    return math.degrees(2.0 * math.asin(min(1.0, math.sqrt(a)))) * 60.0


@dataclass(frozen=True)
class ScoredComparison:
    """A comparison star scored against a target."""

    star: ComparisonStar
    separation_arcmin: float
    delta_mag: float | None  # |comp − target| in the chosen band, if both known


@dataclass(frozen=True)
class ComparisonQuality:
    """A candidate's quality measured on the current pilot frame.

    Catalogue magnitudes say whether a star is calibrated; they do not say
    whether it is measurable through the current filter, focus and sky.
    ``AcquisitionEngine`` supplies these values after plate-solving a real
    frame, while this module remains UI- and hardware-free.
    """

    snr: float
    inst_mag: float | None
    saturated: bool = False
    suspect: bool = False


def rank_comparisons(
    target_ra_deg: float,
    target_dec_deg: float,
    comparisons: list[ComparisonStar],
    *,
    target_mag: float | None = None,
    band: str = "V",
    mag_tol: float | None = None,
    max_results: int | None = None,
) -> list[ScoredComparison]:
    """Rank ``comparisons`` for photometry of a target, closest first.

    ``target_mag`` + ``mag_tol`` (if both given) drops comparisons whose ``band``
    magnitude differs from the target by more than ``mag_tol`` (and any lacking
    that band, since they can't be judged). ``max_results`` caps the list.
    """
    scored: list[ScoredComparison] = []
    for c in comparisons:
        sep = separation_arcmin(target_ra_deg, target_dec_deg, c.ra_deg, c.dec_deg)
        cmag = c.mag(band)
        delta = abs(cmag - target_mag) if (cmag is not None and target_mag is not None) else None
        if mag_tol is not None and target_mag is not None:
            if delta is None or delta > mag_tol:
                continue  # too far in brightness, or unmeasured in this band
        scored.append(ScoredComparison(star=c, separation_arcmin=sep, delta_mag=delta))
    scored.sort(key=lambda s: s.separation_arcmin)
    if max_results is not None:
        scored = scored[:max_results]
    return scored


def auto_comparison_stars(
    target_ra_deg: float,
    target_dec_deg: float,
    comparisons: list[ComparisonStar],
    *,
    wcs=None,
    green_shape: tuple[int, int] | None = None,
    count: int = 5,
    candidate_quality: Mapping[str, ComparisonQuality] | None = None,
    target_inst_mag: float | None = None,
    min_snr: float = 0.0,
    max_delta_inst_mag: float | None = None,
    max_separation_arcmin: float | None = None,
) -> list[TargetStar]:
    """The ``count`` best comparisons for a target, as ready-to-save TargetStars.

    Closest-first via :func:`rank_comparisons`. Stars outside the current
    frame are dropped when ``wcs`` + ``green_shape`` are given — an off-frame
    comparison can never be measured, and it would poison the aperture
    tracker's anchor set.
    """
    candidates: list[tuple[float, float, ComparisonStar]] = []
    for scored in rank_comparisons(target_ra_deg, target_dec_deg, comparisons):
        c = scored.star
        if max_separation_arcmin is not None and scored.separation_arcmin > max_separation_arcmin:
            continue
        if wcs is not None and green_shape is not None:
            if project_points(wcs, green_shape, [(c.ra_deg, c.dec_deg)])[0] is None:
                continue
        quality = candidate_quality.get(c.auid) if candidate_quality is not None else None
        if candidate_quality is not None:
            # A pilot frame is authoritative: never fill the requested count
            # with a star the camera cannot measure reliably.
            if quality is None or quality.saturated or quality.suspect or quality.snr < min_snr:
                continue
            delta_inst = (
                abs(quality.inst_mag - target_inst_mag)
                if quality.inst_mag is not None and target_inst_mag is not None
                else None
            )
            if max_delta_inst_mag is not None and (
                delta_inst is None or delta_inst > max_delta_inst_mag
            ):
                continue
            # Quality and brightness similarity matter more than angular
            # proximity. Distance is intentionally only a gentle preference:
            # a wide Seestar field often contains a much better comparison
            # than the nearest faint catalogue star.
            snr_score = min(float(quality.snr) / max(20.0, min_snr * 2.0), 1.0)
            mag_score = (
                max(0.0, 1.0 - delta_inst / max_delta_inst_mag)
                if delta_inst is not None and max_delta_inst_mag
                else 0.5
            )
            distance_score = max(0.0, 1.0 - scored.separation_arcmin / 120.0)
            score = 0.50 * snr_score + 0.40 * mag_score + 0.10 * distance_score
        else:
            # Legacy/offline behaviour remains closest-first when no pilot
            # measurement is available.
            score = -scored.separation_arcmin
        candidates.append((score, scored.separation_arcmin, c))

    candidates.sort(key=lambda item: (-item[0], item[1]))
    picks: list[TargetStar] = []
    for _score, _separation, c in candidates:
        picks.append(
            TargetStar(
                role=ROLE_COMPARISON,
                ra_deg=c.ra_deg,
                dec_deg=c.dec_deg,
                auid=c.auid,
                # ``label`` is VSP's magnitude×10 chart code, not a star
                # designation.  The AUID is the unambiguous identifier that
                # must travel into the selection manifest and photometry CSV.
                name=None,
                source="vsp_auto",
                mags={b.band: b.mag for b in c.bands},
                chart_id=c.chart_id,
            )
        )
        if len(picks) >= count:
            break
    return picks


def comparisons_for_variable(
    variable: VariableStar,
    comparisons: list[ComparisonStar],
    *,
    band: str = "V",
    mag_tol: float | None = None,
    max_results: int | None = None,
) -> list[ScoredComparison]:
    """:func:`rank_comparisons` for a :class:`VariableStar`, using its brightest
    magnitude as the reference for the (optional) magnitude-similarity filter."""
    return rank_comparisons(
        variable.ra_deg,
        variable.dec_deg,
        comparisons,
        target_mag=variable.brightest_mag,
        band=band,
        mag_tol=mag_tol,
        max_results=max_results,
    )
