"""Airmass + Julian date helpers (notes/photometry_plan.md §6 C4).

Qt-free, dependency-light. JD is the standard calendar→Julian-date conversion
for an exposure-midpoint UTC datetime. (BJD_TDB, the publishable standard, is
a post-processing step.)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def airmass_from_altitude(alt_deg: float | None) -> float | None:
    """Airmass for a target at altitude ``alt_deg``.

    One formula project-wide (P4): delegates to Pickering 2002
    (``sky_geometry.compute_airmass`` — the same function the FITS ``AIRMASS``
    header uses. Same formula, but not necessarily the same *input*: the header
    is evaluated at the exposure start from the mount's altitude, so prefer
    :func:`airmass_at` for a light-curve point.) Returns ``None`` at or below
    the horizon.
    """
    if alt_deg is None:
        return None
    from argos.core.imaging.sky_geometry import compute_airmass

    return compute_airmass(float(alt_deg))


def airmass_at(
    jd_utc: float,
    ra_deg: float,
    dec_deg: float,
    lat_deg: float,
    lon_deg: float,
) -> float | None:
    """Airmass of a *target* at ``jd_utc``, from its own coordinates.

    The mount's last reported altitude is sampled at an arbitrary instant and
    describes where the tube points, not where the star is at the exposure
    midpoint. For a light-curve point — which is timed at the midpoint — derive
    the altitude from the target's own position instead.

    Returns ``None`` at or below the horizon.
    """
    from argos.core.imaging.sky_geometry import altitude_at

    alt = altitude_at(jd_utc, ra_deg / 15.0, dec_deg, lat_deg, lon_deg)
    return airmass_from_altitude(alt)


def julian_date(dt: datetime) -> float:
    """Julian date of a UTC ``datetime`` (Fliegel–Van Flandern day number).

    The input should be timezone-aware UTC (the exposure midpoint); the tz is not
    re-converted here — pass UTC.
    """
    a = (14 - dt.month) // 12
    y = dt.year + 4800 - a
    m = dt.month + 12 * a - 3
    jdn = dt.day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045
    frac = (
        (dt.hour - 12) / 24.0
        + dt.minute / 1440.0
        + dt.second / 86400.0
        + dt.microsecond / 86_400_000_000.0
    )
    return jdn + frac


def bjd_tdb(
    jd_utc: float,
    ra_deg: float,
    dec_deg: float,
    lat_deg: float,
    lon_deg: float,
    elev_m: float = 0.0,
) -> float | None:
    """Barycentric Julian Date (TDB) for an exposure-midpoint ``jd_utc``.

    The publishable time standard: UTC→TDB plus the barycentric light-travel-time
    correction for the target's direction from the observing site. Uses astropy
    (imported lazily). Returns ``None`` if astropy can't do the conversion.
    """
    try:
        import astropy.units as u
        from astropy.coordinates import EarthLocation, SkyCoord
        from astropy.time import Time

        t = Time(jd_utc, format="jd", scale="utc")
        loc = EarthLocation.from_geodetic(
            lon=lon_deg * u.deg, lat=lat_deg * u.deg, height=elev_m * u.m
        )
        target = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg)
        ltt = t.light_travel_time(target, kind="barycentric", location=loc)
        return float((t.tdb + ltt).jd)
    except Exception:  # astropy missing / bad inputs → caller falls back to JD
        return None


def utc_from_bjd_tdb(
    bjd: float,
    ra_deg: float,
    dec_deg: float,
    lat_deg: float,
    lon_deg: float,
    elev_m: float = 0.0,
) -> datetime | None:
    """Invert :func:`bjd_tdb` to a topocentric UTC instant.

    A transit catalogue epoch is a barycentric arrival time, while the
    observer needs a clock time at their site.  Starting from astropy's TDB →
    UTC conversion, a few light-travel-time iterations solve that difference
    to well below the precision appropriate for planning a sequence.  ``None``
    means that astropy/site data could not produce a trustworthy conversion.
    """
    try:
        from astropy.time import Time

        when = Time(float(bjd), format="jd", scale="tdb").utc.to_datetime(timezone=timezone.utc)
    except Exception:
        return None
    for _ in range(4):
        calculated = bjd_tdb(julian_date(when), ra_deg, dec_deg, lat_deg, lon_deg, elev_m)
        if calculated is None:
            return None
        correction_s = (float(bjd) - calculated) * 86400.0
        when += timedelta(seconds=correction_s)
        if abs(correction_s) < 0.001:
            break
    return when
