"""AAVSO VSX + VSP HTTP clients (Qt-free, network-isolated).

Two public calls, both cone/field queries around a solved field centre:

* :func:`vsx_cone_search` → :class:`VariableStar` list (the *variables*).
* :func:`vsp_chart` → :class:`ComparisonStar` list (the *comparison stars*).

Network/parse failures raise :class:`CatalogError` with a human message; the
worker turns that into a status line instead of crashing the UI. Coordinates are
normalised to **decimal degrees** (J2000) on the way out — VSX already returns
degrees, VSP returns sexagesimal, so both are handled here, once.

**Offline cache.** Every successful response is cached on disk
(``~/.argos/cache/catalog``); a fresh entry short-circuits the network, and a
stale one is served when the network is down — so a field previously observed
(or pre-fetched at home) keeps its variables/comparisons with no internet in
the field (docsrc/field_connectivity.md). Query coordinates are quantised to a
small grid so re-solves of the same field land on the same cache entry; the
cone radius is padded by the worst-case quantisation shift to compensate.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Final endpoints (the bare aavso.org hosts 301-redirect to these).
_VSX_URL = "https://vsx.aavso.org/index.php"
_VSP_URL = "https://app.aavso.org/vsp/api/chart/"

_DEFAULT_TIMEOUT = 20.0

# VSP rejects a chart wider than 180 arcmin when its faint limit is fainter
# than magnitude 12.  The S30/S50 field can be wider than that, but a 180′
# chart around the selected target still supplies a dense, on-sensor ensemble
# without sacrificing the useful V≈12–15 comparison stars.
_VSP_WIDE_FOV_MAG_LIMIT = 12.0
_VSP_WIDE_FOV_MAX_ARCMIN = 180.0

# Offline cache (see module docstring). Tests monkeypatch _CACHE_DIR.
_CACHE_DIR = Path.home() / ".argos" / "cache" / "catalog"
_CACHE_FRESH_S = 7 * 86400.0  # younger than this → no network round-trip

# Quantisation grid for query coordinates — 0.05° = 3′, well under any frame's
# margin once the radius is padded by _CENTER_STEP_DEG.
_CENTER_STEP_DEG = 0.05


class CatalogError(RuntimeError):
    """A catalog query failed (network, HTTP, or unparseable response)."""


def configure_cache_directory(path: Path | str) -> None:
    """Set the persistent VSX/VSP field-cache folder for future queries."""
    global _CACHE_DIR
    _CACHE_DIR = Path(path).expanduser()


# --------------------------------------------------------------------------- #
# Data model                                                                   #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VariableStar:
    """One VSX entry. ``ra_deg``/``dec_deg`` are J2000 decimal degrees."""

    name: str
    ra_deg: float
    dec_deg: float
    auid: str | None = None
    var_type: str | None = None  # VSX VariabilityType (e.g. "EA", "INS")
    category: str | None = None  # "Variable" / "Suspected"
    max_mag: str | None = None  # raw VSX string, band included (e.g. "13.5 B")
    min_mag: str | None = None
    period: float | None = None  # days, when known
    constellation: str | None = None
    oid: str | None = None

    @property
    def is_suspected(self) -> bool:
        return (self.category or "").lower().startswith("suspect")

    @property
    def brightest_mag(self) -> float | None:
        """Numeric part of ``max_mag`` (the brightest magnitude), band ignored.

        VSX strings look like ``"13.5 B"``, ``"<14.5"``, ``"16.1 V"``. Used to
        sort/cut a field down to what a frame could actually show.
        """
        return _leading_float(self.max_mag)


@dataclass(frozen=True)
class Band:
    """One photometric band of a comparison star."""

    band: str  # "V", "B", "Rc", "Ic", …
    mag: float
    error: float | None = None


@dataclass(frozen=True)
class ComparisonStar:
    """One VSP comparison star. ``ra_deg``/``dec_deg`` are J2000 decimal degrees."""

    auid: str
    ra_deg: float
    dec_deg: float
    label: str  # chart label = mag×10 (e.g. "114" → V≈11.4)
    bands: tuple[Band, ...] = ()
    comments: str | None = None
    # VSP chart the star was listed on — the AAVSO asks for it in the CHART
    # field of a report so it can trace (and re-calibrate) the sequence used.
    chart_id: str | None = None

    def mag(self, band: str = "V") -> float | None:
        """Magnitude in ``band`` if measured, else None."""
        for b in self.bands:
            if b.band == band:
                return b.mag
        return None


# --------------------------------------------------------------------------- #
# Coordinate parsing                                                           #
# --------------------------------------------------------------------------- #


def _hms_to_deg(text: str) -> float:
    """``"05:35:58.50"`` (hours) → degrees."""
    h, m, s = (float(p) for p in text.split(":"))
    return (h + m / 60.0 + s / 3600.0) * 15.0


def _dms_to_deg(text: str) -> float:
    """``"-05:22:31.2"`` (degrees) → degrees, sign-safe."""
    text = text.strip()
    sign = -1.0 if text[0] == "-" else 1.0
    d, m, s = (float(p) for p in text.lstrip("+-").split(":"))
    return sign * (d + m / 60.0 + s / 3600.0)


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _leading_float(value: Any) -> float | None:
    """First float in a string like ``"13.5 B"`` / ``"<14.5"`` → 13.5 / 14.5."""
    if not value:
        return None
    import re

    m = re.search(r"-?\d+(?:\.\d+)?", str(value))
    return float(m.group()) if m else None


# --------------------------------------------------------------------------- #
# HTTP                                                                         #
# --------------------------------------------------------------------------- #


def _quantize_deg(value: float, step: float = _CENTER_STEP_DEG) -> float:
    """Snap ``value`` to the cache grid (so re-solves share a cache key)."""
    return round(round(value / step) * step, 4)


def _pad_and_quantize_radius(radius: float, step: float = _CENTER_STEP_DEG) -> float:
    """Grow ``radius`` to cover the worst-case centre-quantisation shift,
    then snap it up to the grid — never smaller than the caller asked for."""
    return round(math.ceil((radius + step) / step) * step, 4)


def _cache_path(url: str, params: dict[str, Any]) -> Path:
    key = hashlib.sha1(json.dumps([url, sorted(params.items())]).encode()).hexdigest()
    return _CACHE_DIR / f"{key}.json"


def _cache_load(path: Path) -> tuple[Any, float] | None:
    """Cached ``(data, age_seconds)``, or None when absent/unreadable."""
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        return entry["data"], time.time() - float(entry["fetched_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _cache_store(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fetched_at": time.time(), "data": data}), encoding="utf-8")
    except OSError as exc:  # a full disk must not sink the query itself
        logger.warning("catalog cache write failed: %s", exc)


def _fetch_json(url: str, params: dict[str, Any], timeout: float, session: Any) -> Any:
    """GET ``url`` and return parsed JSON, or raise :class:`CatalogError`."""
    getter = session.get if session is not None else requests.get
    try:
        resp = getter(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        raise CatalogError(f"catalog request failed: {exc}") from exc
    except ValueError as exc:  # JSON decode
        raise CatalogError(f"catalog returned invalid JSON: {exc}") from exc


def _get_json(url: str, params: dict[str, Any], timeout: float, session: Any) -> Any:
    """Cache-aware GET: fresh cache → no network; network down → stale cache."""
    path = _cache_path(url, params)
    cached = _cache_load(path)
    if cached is not None and cached[1] < _CACHE_FRESH_S:
        logger.debug("catalog cache hit (%.1f h old): %s", cached[1] / 3600.0, path.name)
        return cached[0]
    try:
        data = _fetch_json(url, params, timeout, session)
    except CatalogError as exc:
        if cached is not None:
            logger.warning(
                "catalog unreachable — serving cached result (%.1f days old)",
                cached[1] / 86400.0,
            )
            return cached[0]
        raise CatalogError(f"{exc} (no cached result for this field)") from exc
    _cache_store(path, data)
    return data


# --------------------------------------------------------------------------- #
# VSX — variable stars                                                         #
# --------------------------------------------------------------------------- #


def vsx_cone_search(
    ra_deg: float,
    dec_deg: float,
    radius_deg: float,
    *,
    include_suspected: bool = True,
    mag_limit: float | None = None,
    max_results: int | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    session: Any = None,
) -> list[VariableStar]:
    """Variable stars within ``radius_deg`` of (ra_deg, dec_deg), J2000.

    ``mag_limit`` keeps only stars at least that bright (a dense field like M42
    has >1500 faint variables; a frame shows few). ``max_results`` caps the
    count, keeping the brightest. Results are returned brightest-first.
    """
    # Centre/radius are quantised so re-solves of the same field share a cache
    # entry; the radius padding guarantees the caller's cone stays covered.
    params = {
        "view": "api.list",
        "ra": f"{_quantize_deg(ra_deg):.4f}",
        "dec": f"{_quantize_deg(dec_deg):.4f}",
        "radius": f"{_pad_and_quantize_radius(radius_deg):.4f}",
        "format": "json",
    }
    if mag_limit is not None:
        params["tomag"] = f"{mag_limit:.2f}"  # server-side cut (band-agnostic)
    data = _get_json(_VSX_URL, params, timeout, session)
    stars = [_parse_vsx_object(o) for o in _vsx_rows(data)]
    if not include_suspected:
        stars = [s for s in stars if not s.is_suspected]
    if mag_limit is not None:
        # Keep stars known to be at least that bright; drop fainter. Unknown-mag
        # entries are kept (the server already applied ``tomag``).
        stars = [s for s in stars if (s.brightest_mag is None or s.brightest_mag <= mag_limit)]
    stars.sort(key=lambda s: (s.brightest_mag is None, s.brightest_mag or 0.0))
    if max_results is not None:
        stars = stars[:max_results]
    logger.info(
        "VSX: %d variable(s) within %.3f° of %.4f,%+.4f", len(stars), radius_deg, ra_deg, dec_deg
    )
    return stars


def _vsx_rows(data: Any) -> list[dict]:
    """VSX nests results under VSXObjects.VSXObject — which may be absent, a
    single dict (one hit), or a list. Normalise to a list of dicts."""
    container = (data or {}).get("VSXObjects")
    rows = container.get("VSXObject") if isinstance(container, dict) else None
    if rows is None:
        return []
    return rows if isinstance(rows, list) else [rows]


def _parse_vsx_object(o: dict) -> VariableStar:
    return VariableStar(
        name=str(o.get("Name", "")).strip(),
        ra_deg=float(o["RA2000"]),
        dec_deg=float(o["Declination2000"]),
        auid=(o.get("AUID") or None),
        var_type=(o.get("VariabilityType") or None),
        category=(o.get("Category") or None),
        max_mag=(o.get("MaxMag") or None),
        min_mag=(o.get("MinMag") or None),
        period=_float_or_none(o.get("Period")),
        constellation=(o.get("Constellation") or None),
        oid=(str(o["OID"]) if o.get("OID") is not None else None),
    )


# --------------------------------------------------------------------------- #
# VSP — comparison stars                                                       #
# --------------------------------------------------------------------------- #


def vsp_chart(
    ra_deg: float,
    dec_deg: float,
    fov_arcmin: float,
    *,
    maglimit: float = 16.0,
    target_name: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    session: Any = None,
) -> list[ComparisonStar]:
    """Comparison stars from a VSP photometry chart.

    ``target_name`` asks VSP for the comparison sequence of a known variable
    target, rather than a generic chart around its coordinates. This matters
    when the mount is offset from the requested target: VSX still describes
    the actually imaged field, while VSP must retain the observer's selected
    scientific target to return its calibrated sequence.
    """
    # Quantised like the VSX cone (shared cache entries across re-solves); the
    # fov is padded by the centre grid step (0.05° = 3′) and snapped up to 5′.
    fov_padded = math.ceil((fov_arcmin + 3.0) / 5.0) * 5.0
    if fov_padded > _VSP_WIDE_FOV_MAX_ARCMIN and maglimit > _VSP_WIDE_FOV_MAG_LIMIT:
        logger.info(
            "VSP: limiting %.0f′ field to %.0f′ for maglimit %.1f",
            fov_padded,
            _VSP_WIDE_FOV_MAX_ARCMIN,
            maglimit,
        )
        fov_padded = _VSP_WIDE_FOV_MAX_ARCMIN
    params = {
        "fov": f"{fov_padded:.1f}",
        "maglimit": f"{maglimit:.1f}",
        "format": "json",
        # Ask VSP for every available calibrated band/sequence entry. Without
        # this, valid entries can be omitted by the service's default chart
        # selection for a wide field.
        "all": "on",
    }
    if target_name and target_name.strip():
        params["star"] = target_name.strip()
    else:
        params["ra"] = f"{_quantize_deg(ra_deg):.4f}"
        params["dec"] = f"{_quantize_deg(dec_deg):.4f}"
    data = _get_json(_VSP_URL, params, timeout, session)
    rows = (data or {}).get("photometry") or []
    chart_id = str((data or {}).get("chartid") or "").strip() or None
    stars = [_parse_vsp_star(o, chart_id) for o in rows]
    logger.info(
        "VSP: %d comparison star(s) for %s (fov %.0f')",
        len(stars),
        target_name or f"{ra_deg:.4f},{dec_deg:+.4f}",
        fov_arcmin,
    )
    return stars


def _parse_vsp_star(o: dict, chart_id: str | None = None) -> ComparisonStar:
    bands = tuple(
        Band(band=str(b.get("band", "")), mag=float(b["mag"]), error=_float_or_none(b.get("error")))
        for b in (o.get("bands") or [])
        if b.get("mag") is not None
    )
    return ComparisonStar(
        auid=str(o.get("auid", "")),
        ra_deg=_hms_to_deg(o["ra"]),
        dec_deg=_dms_to_deg(o["dec"]),
        label=str(o.get("label", "")),
        bands=bands,
        comments=(o.get("comments") or None),
        chart_id=chart_id,
    )
