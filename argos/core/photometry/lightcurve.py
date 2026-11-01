"""Light-curve accumulator + CSV export (notes/photometry_plan.md §6 C3).

One :class:`LightCurve` per target; points are appended as subs are measured and
written to ``photometry.csv`` (the hand-off to post-processing). Qt-free.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
import math
from pathlib import Path

_COLUMNS = (
    "jd_utc",
    "bjd_tdb",
    "mag",
    "mag_err",
    "formal_mag_err",
    "sigma_syst",
    "airmass",
    "fwhm",
    "sky_adu",
    "comps_used",
    "relative_flux",
    "relative_flux_err",
    "relative_comps_used",
    "saturated",
    "suspect",
)

#: Session export retains the identity and role of every measured star.  Older
#: exports used only ``target`` and could not be reloaded without merging all
#: series into one curve.
_MULTI_COLUMNS = ("star_id", "role", "name", "auid", *_COLUMNS)


@dataclass
class LcPoint:
    """One light-curve point (exposure-midpoint JD_UTC; BJD_TDB when site known)."""

    jd_utc: float
    mag: float
    mag_err: float
    bjd_tdb: float | None = None
    airmass: float | None = None
    fwhm: float | None = None
    sky_adu: float | None = None
    #: Comparisons behind ``mag`` — the magnitude ensemble.
    comps_used: int = 0
    saturated: bool = False
    #: Aperture peak lacked PSF support (hot pixel / cosmic ray suspicion).
    #: Recorded, never used to drop a point: the observer decides.
    suspect: bool = False
    formal_mag_err: float | None = None
    sigma_syst: float | None = None
    relative_flux: float | None = None
    relative_flux_err: float | None = None
    #: Comparisons behind ``relative_flux``. Filtered differently from
    #: ``comps_used`` (the ratio path also rejects *suspect* stars), so the two
    #: legitimately differ on the same row.
    relative_comps_used: int = 0


@dataclass
class LightCurve:
    """A star's differential light curve (preview).

    ``role`` mirrors the TargetStar role ("target" / "check" / "comparison")
    so display surfaces can group series; reloaded CSVs default to "target"
    (the historical behaviour — role is a display hint, not science data).
    """

    auid: str = ""
    name: str = ""
    points: list[LcPoint] = field(default_factory=list)
    role: str = "target"

    def append(self, point: LcPoint) -> None:
        self.points.append(point)

    def to_csv(self, path) -> None:
        """Write the curve to ``path`` (parent dirs created)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(_COLUMNS)
            for p in self.points:
                writer.writerow(_row(p))

    def to_aavso(self, path, **kwargs) -> None:
        """Write this curve in AAVSO Extended File Format (ensemble photometry)."""
        write_aavso(path, [self], **kwargs)

    @classmethod
    def from_csv(cls, path, auid: str = "", name: str = "") -> "LightCurve":
        """Reload a curve written by :meth:`to_csv` (round-trips it).

        Lets a finished session be reopened for review/export without re-running
        the night. Unknown/blank optional columns become ``None``; unparseable
        rows are skipped rather than raising, so a partial file still loads.
        """
        path = Path(path)
        lc = cls(auid=auid, name=name or Path(path).stem)
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    lc.append(_point_from_row(row))
                except (TypeError, ValueError):
                    continue
        return lc


def _row(p: LcPoint) -> list:
    """One CSV row, in ``_COLUMNS`` order, for a light-curve point."""
    return [
        p.jd_utc,
        "" if p.bjd_tdb is None else p.bjd_tdb,
        p.mag,
        p.mag_err,
        "" if p.formal_mag_err is None else p.formal_mag_err,
        "" if p.sigma_syst is None else p.sigma_syst,
        "" if p.airmass is None else p.airmass,
        "" if p.fwhm is None else p.fwhm,
        "" if p.sky_adu is None else p.sky_adu,
        p.comps_used,
        "" if p.relative_flux is None else p.relative_flux,
        "" if p.relative_flux_err is None else p.relative_flux_err,
        p.relative_comps_used,
        int(p.saturated),
        int(p.suspect),
    ]


def write_curves_csv(path, curves) -> None:
    """Write several curves to one lossless, session-scoped CSV export."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(_MULTI_COLUMNS)
        for index, lc in enumerate(curves, start=1):
            label = lc.name or lc.auid or "TARGET"
            star_id = lc.auid or f"{lc.role}:{label}:{index}"
            for p in lc.points:
                writer.writerow([star_id, lc.role, label, lc.auid, *_row(p)])


def read_curves_csv(path) -> dict[str, LightCurve]:
    """Restore every curve in a session export, preserving role and identity.

    Single-curve and legacy files remain supported; they become one target
    curve keyed by the supplied name or filename stem.
    """
    path = Path(path)
    curves: dict[str, LightCurve] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                point = _point_from_row(row)
            except (TypeError, ValueError):
                continue
            name = row.get("name") or row.get("target") or path.stem
            auid = row.get("auid") or ""
            role = row.get("role") or "target"
            key = row.get("star_id") or auid or f"{role}:{name}"
            curve = curves.setdefault(key, LightCurve(auid=auid, name=name, role=role))
            curve.append(point)
    return curves


def _point_from_row(row: dict[str, str]) -> LcPoint:
    """Build a point from either the current or legacy CSV schema."""
    mag_err = float(row["mag_err"])
    return LcPoint(
        jd_utc=float(row["jd_utc"]),
        mag=float(row["mag"]),
        mag_err=mag_err,
        bjd_tdb=_opt_float(row.get("bjd_tdb")),
        airmass=_opt_float(row.get("airmass")),
        fwhm=_opt_float(row.get("fwhm")),
        sky_adu=_opt_float(row.get("sky_adu")),
        comps_used=int(row.get("comps_used") or 0),
        relative_comps_used=int(row.get("relative_comps_used") or 0),
        saturated=bool(int(row.get("saturated") or 0)),
        suspect=bool(int(row.get("suspect") or 0)),
        formal_mag_err=_opt_float(row.get("formal_mag_err")) or mag_err,
        sigma_syst=_opt_float(row.get("sigma_syst")),
        relative_flux=_opt_float(row.get("relative_flux")),
        relative_flux_err=_opt_float(row.get("relative_flux_err")),
    )


def _opt_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def write_aavso(
    path,
    curves,
    *,
    obscode: str = "XXX",
    filt: str = "TG",
    chart: str | None = None,
    software: str = "Argos",
) -> None:
    """Write one or more :class:`LightCurve` to an AAVSO Extended File.

    A *preview* export — DATE is the JD_UTC midpoint, MTYPE=STD, comparison is the
    ensemble (CNAME=ENSEMBLE). Calibrated mags + BJD_TDB come from post-processing.

    Only *target* curves become rows. The first *check* curve fills KNAME/KMAG with
    its ensemble magnitude on the same frame — the check star the spec asks for
    with ensemble photometry. ``chart`` is the VSP chart of the comparison
    sequence (CHART).
    """
    check = next((lc for lc in curves if lc.role == "check" and lc.points), None)
    kname = _aavso_field(check.auid or check.name) if check is not None else None
    # Target and check points of one frame share its JD.
    kmags = {p.jd_utc: p.mag for p in check.points if math.isfinite(p.mag)} if kname else {}
    chart_field = _aavso_field(chart) or "na"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        f.write("#TYPE=EXTENDED\n")
        f.write(f"#OBSCODE={obscode}\n")
        f.write(f"#SOFTWARE={software}\n")
        f.write("#DELIM=,\n#DATE=JD\n#OBSTYPE=CCD\n")
        f.write(
            "#NAME,DATE,MAG,MERR,FILT,TRANS,MTYPE,CNAME,CMAG,KNAME,KMAG,"
            "AMASS,GROUP,CHART,NOTES\n"
        )
        # Comparison and check curves are ensemble diagnostics, not science
        # observations. Never silently write them as AAVSO target rows.
        for lc in curves:
            if lc.role != "target":
                continue
            name = (lc.name or lc.auid or "TARGET").upper()
            for p in lc.points:
                if not (math.isfinite(p.mag) and math.isfinite(p.mag_err)):
                    continue
                amass = "na" if p.airmass is None else f"{p.airmass:.3f}"
                kmag = kmags.get(p.jd_utc)
                check_fields = "na,na" if kmag is None else f"{kname},{kmag:.4f}"
                f.write(
                    f"{name},{p.jd_utc:.6f},{p.mag:.4f},{p.mag_err:.4f},{filt},NO,STD,"
                    f"ENSEMBLE,na,{check_fields},{amass},na,{chart_field},na\n"
                )


def _aavso_field(value, limit: int = 20) -> str | None:
    """``value`` as a KNAME/CHART field (at most ``limit`` characters, no comma)."""
    value = str(value or "").strip()
    if not value or len(value) > limit or "," in value:
        return None
    return value
