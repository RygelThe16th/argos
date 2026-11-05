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


#: Exclusion reasons reported by :func:`select_comparison_stars`.
EXCLUDED_NEAR_TARGET = "near_target"
EXCLUDED_NEAR_COMPARISON = "near_comparison"
EXCLUDED_NEAR_EDGE = "near_frame_edge"


@dataclass(frozen=True)
class FieldPlacement:
    """Where a star sits relative to the target and to the frame edges.

    ``target_distance_px`` and ``edge_distance_px`` are green-plane pixels on
    the current WCS and are ``None`` without one (or when the star projects
    outside the frame, for ``edge_distance_px``).
    """

    separation_arcsec: float
    target_distance_px: float | None = None
    edge_distance_px: float | None = None


def field_placement(
    target_ra_deg: float,
    target_dec_deg: float,
    ra_deg: float,
    dec_deg: float,
    *,
    wcs=None,
    green_shape: tuple[int, int] | None = None,
) -> FieldPlacement:
    """Separation from the target and distance to the nearest frame edge."""
    separation = separation_arcmin(target_ra_deg, target_dec_deg, ra_deg, dec_deg) * 60.0
    if wcs is None or green_shape is None:
        return FieldPlacement(separation_arcsec=separation)
    star = _green_xy(wcs, ra_deg, dec_deg)
    target = _green_xy(wcs, target_ra_deg, target_dec_deg)
    return FieldPlacement(
        separation_arcsec=separation,
        target_distance_px=math.hypot(star[0] - target[0], star[1] - target[1]),
        edge_distance_px=_edge_distance(star, green_shape),
    )


def _green_xy(wcs, ra_deg: float, dec_deg: float) -> tuple[float, float]:
    x, y = wcs.world_to_pixel_deg(ra_deg, dec_deg)
    return float(x), float(y)


def _edge_distance(xy: tuple[float, float], green_shape: tuple[int, int]) -> float | None:
    """Distance (green px) to the nearest frame edge; ``None`` when outside."""
    gh, gw = green_shape
    x, y = xy
    distance = min(x, y, gw - x, gh - y)
    return distance if distance >= 0.0 else None


@dataclass(frozen=True)
class ComparisonExclusion:
    """A VSP candidate rejected for its position, with an observer-facing reason."""

    auid: str
    reason: str  # one of the ``EXCLUDED_*`` constants
    distance_px: float
    limit_px: float
    other: str | None = None  # the star it is too close to, if any

    def describe(self) -> str:
        if self.reason == EXCLUDED_NEAR_EDGE:
            return (
                f"{self.auid}: {self.distance_px:.0f} px from the frame edge "
                f"(< {self.limit_px:.0f} px edge margin)"
            )
        other = self.other or "target"
        return (
            f"{self.auid}: {self.distance_px:.1f} px from {other} "
            f"(inside the {self.limit_px:.0f} px aperture + annulus)"
        )


@dataclass(frozen=True)
class ComparisonSelection:
    """Result of :func:`select_comparison_stars`."""

    picks: list[TargetStar]
    excluded: list[ComparisonExclusion]


def auto_comparison_stars(
    target_ra_deg: float,
    target_dec_deg: float,
    comparisons: list[ComparisonStar],
    **kwargs,
) -> list[TargetStar]:
    """The best comparisons for a target, as ready-to-save TargetStars.

    Convenience wrapper over :func:`select_comparison_stars` for callers that
    do not report the rejected candidates.
    """
    return select_comparison_stars(target_ra_deg, target_dec_deg, comparisons, **kwargs).picks


def select_comparison_stars(
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
    distance_weight: float = 0.10,
    distance_scale_arcmin: float = 120.0,
    max_delta_inst_mag: float | None = None,
    max_separation_arcmin: float | None = None,
    min_separation_px: float | None = None,
    edge_margin_px: float | None = None,
    occupied_radec: tuple[tuple[str, float, float], ...] = (),
) -> ComparisonSelection:
    """The ``count`` best comparisons for a target, plus the positional rejects.

    Closest-first via :func:`rank_comparisons`. Stars outside the current
    frame are dropped when ``wcs`` + ``green_shape`` are given — an off-frame
    comparison can never be measured, and it would poison the aperture
    tracker's anchor set.

    With a WCS, two positional gates are also applied, both in green px:

    * ``min_separation_px`` — a candidate this close to the target (or to an
      already chosen comparison, or to a star in ``occupied_radec``) shares
      its aperture or background annulus. It cannot be measured on its own
      and it biases the target, so it is rejected whatever its pilot quality
      (a star blended with the target even *looks* ideal: same SNR, same
      instrumental magnitude).
    * ``edge_margin_px`` — a candidate this close to a frame edge leaves the
      field as soon as the mount drifts.

    Ranking weighs pilot SNR and brightness match against proximity;
    ``distance_weight`` is proximity's share of the score, falling to zero at
    ``distance_scale_arcmin``.

    ``occupied_radec`` lists ``(name, ra_deg, dec_deg)`` of comparisons that
    are already in the ensemble; a candidate within 1″ of one is the same star
    and is left to the caller's de-duplication.
    """
    distance_weight = min(max(distance_weight, 0.0), 1.0)
    use_geometry = wcs is not None and green_shape is not None
    target_xy = _green_xy(wcs, target_ra_deg, target_dec_deg) if use_geometry else None
    excluded: list[ComparisonExclusion] = []
    candidates: list[tuple[float, float, ComparisonStar, tuple[float, float] | None]] = []
    for scored in rank_comparisons(target_ra_deg, target_dec_deg, comparisons):
        c = scored.star
        if max_separation_arcmin is not None and scored.separation_arcmin > max_separation_arcmin:
            continue
        xy: tuple[float, float] | None = None
        if use_geometry:
            if project_points(wcs, green_shape, [(c.ra_deg, c.dec_deg)])[0] is None:
                continue
            xy = _green_xy(wcs, c.ra_deg, c.dec_deg)
            if min_separation_px is not None:
                to_target = math.hypot(xy[0] - target_xy[0], xy[1] - target_xy[1])
                if to_target < min_separation_px:
                    excluded.append(
                        ComparisonExclusion(
                            c.auid, EXCLUDED_NEAR_TARGET, to_target, min_separation_px
                        )
                    )
                    continue
            if edge_margin_px is not None:
                edge = _edge_distance(xy, green_shape)
                if edge is None or edge < edge_margin_px:
                    excluded.append(
                        ComparisonExclusion(
                            c.auid, EXCLUDED_NEAR_EDGE, max(edge or 0.0, 0.0), edge_margin_px
                        )
                    )
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
            # SNR and brightness match share what proximity leaves, 5:4. A
            # nearby reference sees the same thin cloud and the same part of
            # the flat as the target, so proximity is a real criterion too.
            snr_score = min(float(quality.snr) / max(20.0, min_snr * 2.0), 1.0)
            mag_score = (
                max(0.0, 1.0 - delta_inst / max_delta_inst_mag)
                if delta_inst is not None and max_delta_inst_mag
                else 0.5
            )
            distance_score = max(
                0.0, 1.0 - scored.separation_arcmin / max(distance_scale_arcmin, 1e-6)
            )
            rest = 1.0 - distance_weight
            score = (
                rest * (5.0 / 9.0) * snr_score
                + rest * (4.0 / 9.0) * mag_score
                + distance_weight * distance_score
            )
        else:
            # Legacy/offline behaviour remains closest-first when no pilot
            # measurement is available.
            score = -scored.separation_arcmin
        candidates.append((score, scored.separation_arcmin, c, xy))

    candidates.sort(key=lambda item: (-item[0], item[1]))
    # Comparisons must not share an aperture or annulus with each other
    # either. Stars already in the ensemble claim their place first; within
    # the proposal the better-ranked candidate wins.
    claimed: list[tuple[str, tuple[float, float]]] = []
    if use_geometry and min_separation_px is not None:
        claimed = [(name, _green_xy(wcs, ra, dec)) for name, ra, dec in occupied_radec]
    picks: list[TargetStar] = []
    for _score, _separation, c, xy in candidates:
        if xy is not None and min_separation_px is not None:
            clash = None
            for name, other_xy in claimed:
                distance = math.hypot(xy[0] - other_xy[0], xy[1] - other_xy[1])
                if distance >= min_separation_px:
                    continue
                if name == c.auid or _same_position(c, name, occupied_radec):
                    continue  # the same star; de-duplication is the caller's job
                clash = (name, distance)
                break
            if clash is not None:
                excluded.append(
                    ComparisonExclusion(
                        c.auid, EXCLUDED_NEAR_COMPARISON, clash[1], min_separation_px, clash[0]
                    )
                )
                continue
            claimed.append((c.auid, xy))
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
    return ComparisonSelection(picks=picks, excluded=excluded)


def _same_position(
    candidate: ComparisonStar, name: str, occupied: tuple[tuple[str, float, float], ...]
) -> bool:
    """Whether ``candidate`` is the occupied star ``name`` itself (within 1″)."""
    for other, ra, dec in occupied:
        if other == name:
            return separation_arcmin(candidate.ra_deg, candidate.dec_deg, ra, dec) < 1.0 / 60.0
    return False


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
