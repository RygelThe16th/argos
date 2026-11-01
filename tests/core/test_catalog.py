"""Tests for the AAVSO VSX/VSP catalog clients — parsing only, no network.

The JSON samples are trimmed real responses for the M42 field (RA 83.82°,
Dec −5.39°), so the parser is exercised against the actual API shapes.
"""

from __future__ import annotations

import pytest
import requests

from argos.core.catalog import (
    CatalogError,
    ComparisonStar,
    VariableStar,
    vsp_chart,
    vsx_cone_search,
)
from argos.core.catalog import aavso
from argos.core.catalog.aavso import _dms_to_deg, _hms_to_deg

# The offline cache is redirected to tmp_path by the autouse
# _isolated_argos_home fixture in tests/conftest.py.

# --- recorded responses (trimmed) ----------------------------------------- #

_VSX_JSON = {
    "VSXObjects": {
        "VSXObject": [
            {
                "Name": "V0730 Ori",
                "AUID": "000-BDZ-554",
                "RA2000": "83.43637",
                "Declination2000": "-5.35106",
                "VariabilityType": "INS",
                "MaxMag": "14 B",
                "MinMag": "15.3 B",
                "Category": "Variable",
                "OID": "23793",
                "Constellation": "Ori",
            },
            {
                "Name": "NSV 2149",
                "RA2000": "83.42358",
                "Declination2000": "-5.40119",
                "MaxMag": "16.1 V",
                "MinMag": "?",
                "Category": "Suspected",
                "OID": "40773",
                "Constellation": "Ori",
            },
        ]
    }
}

_VSP_JSON = {
    "chartid": "X42585ESI",
    "fov": 40.0,
    "maglimit": 14.0,
    "photometry": [
        {
            "auid": "000-BJX-214",
            "ra": "05:35:58.50",
            "dec": "-05:22:31.2",
            "label": "90",
            "bands": [
                {"band": "V", "mag": 8.998, "error": 0.016},
                {"band": "B", "mag": 8.981, "error": 0.038},
            ],
            "comments": "Suspected variable NSV 2386. Acceptable to use.",
        },
        {
            "auid": "000-BJX-211",
            "ra": "05:35:59.07",
            "dec": "-05:39:58.6",
            "label": "114",
            "bands": [{"band": "V", "mag": 11.401, "error": 0.009}],
            "comments": None,
        },
    ],
}


class _FakeResponse:
    def __init__(self, payload, *, exc=None):
        self._payload = payload
        self._exc = exc

    def raise_for_status(self):
        pass

    def json(self):
        if self._exc:
            raise self._exc
        return self._payload


class _FakeSession:
    """Minimal requests.Session stand-in returning a canned payload."""

    def __init__(self, payload=None, *, raise_exc=None, json_exc=None):
        self._payload = payload
        self._raise_exc = raise_exc
        self._json_exc = json_exc
        self.last_params: dict | None = None

    def get(self, url, params=None, timeout=None):
        self.last_params = params
        if self._raise_exc:
            raise self._raise_exc
        return _FakeResponse(self._payload, exc=self._json_exc)


# --- coord parsing --------------------------------------------------------- #


def test_hms_to_deg() -> None:
    assert abs(_hms_to_deg("05:35:58.50") - 83.99375) < 1e-4
    assert _hms_to_deg("00:00:00") == 0.0


def test_dms_to_deg_is_sign_safe() -> None:
    assert abs(_dms_to_deg("-05:22:31.2") - (-5.375333)) < 1e-5
    assert abs(_dms_to_deg("+12:30:00") - 12.5) < 1e-9


# --- VSX ------------------------------------------------------------------- #


def test_vsx_parses_variables() -> None:
    sess = _FakeSession(_VSX_JSON)
    stars = vsx_cone_search(83.82, -5.39, 0.4, session=sess)
    assert len(stars) == 2
    v = stars[0]
    assert isinstance(v, VariableStar)
    assert v.name == "V0730 Ori"
    assert v.auid == "000-BDZ-554"
    assert abs(v.ra_deg - 83.43637) < 1e-5 and abs(v.dec_deg + 5.35106) < 1e-5
    assert v.var_type == "INS"
    assert v.max_mag == "14 B"
    assert not v.is_suspected
    assert stars[1].is_suspected  # NSV 2149, Category=Suspected
    # The cone-search params are formatted as the API expects.
    assert sess.last_params["view"] == "api.list" and sess.last_params["format"] == "json"


def test_vsx_can_drop_suspected() -> None:
    stars = vsx_cone_search(
        83.82, -5.39, 0.4, include_suspected=False, session=_FakeSession(_VSX_JSON)
    )
    assert [s.name for s in stars] == ["V0730 Ori"]


def test_variable_star_brightest_mag_parsing() -> None:
    def mk(m):
        return VariableStar("x", 0, 0, max_mag=m)

    assert mk("13.5 B").brightest_mag == 13.5
    assert mk("<14.5").brightest_mag == 14.5
    assert mk("16.1 V").brightest_mag == 16.1
    assert mk("?").brightest_mag is None
    assert mk(None).brightest_mag is None


def test_vsx_mag_limit_and_cap() -> None:
    rows = [
        {
            "Name": "faint",
            "RA2000": "0",
            "Declination2000": "0",
            "MaxMag": "17 V",
            "Category": "Variable",
        },
        {
            "Name": "bright",
            "RA2000": "0",
            "Declination2000": "0",
            "MaxMag": "9 V",
            "Category": "Variable",
        },
        {
            "Name": "mid",
            "RA2000": "0",
            "Declination2000": "0",
            "MaxMag": "12 V",
            "Category": "Variable",
        },
    ]
    sess = _FakeSession({"VSXObjects": {"VSXObject": rows}})
    stars = vsx_cone_search(0, 0, 0.5, mag_limit=14, session=sess)
    assert [s.name for s in stars] == ["bright", "mid"]  # 17 dropped, brightest first
    assert sess.last_params["tomag"] == "14.00"  # server-side hint sent
    # Cap keeps only the brightest.
    capped = vsx_cone_search(
        0, 0, 0.5, max_results=1, session=_FakeSession({"VSXObjects": {"VSXObject": rows}})
    )
    assert [s.name for s in capped] == ["bright"]


def test_vsx_handles_single_object_and_empty() -> None:
    # Distinct fields per call — identical coords would share a cache entry.
    one = {"VSXObjects": {"VSXObject": _VSX_JSON["VSXObjects"]["VSXObject"][0]}}
    assert len(vsx_cone_search(0, 0, 0.1, session=_FakeSession(one))) == 1
    assert vsx_cone_search(10, 0, 0.1, session=_FakeSession({"VSXObjects": ""})) == []
    assert vsx_cone_search(20, 0, 0.1, session=_FakeSession({})) == []


# --- VSP ------------------------------------------------------------------- #


def test_vsp_parses_comparison_stars() -> None:
    stars = vsp_chart(83.82, -5.39, 40.0, session=_FakeSession(_VSP_JSON))
    assert len(stars) == 2
    c = stars[0]
    assert isinstance(c, ComparisonStar)
    assert c.auid == "000-BJX-214"
    # Sexagesimal → decimal degrees.
    assert abs(c.ra_deg - 83.99375) < 1e-4 and abs(c.dec_deg + 5.375333) < 1e-5
    assert c.label == "90"
    assert c.mag("V") == 8.998 and c.mag("B") == 8.981
    assert c.mag("Rc") is None  # band not present
    assert stars[1].mag("V") == 11.401


def test_vsp_stars_carry_the_chart_id() -> None:
    # The AAVSO asks for this chart ID in the CHART field of a report.
    stars = vsp_chart(83.82, -5.39, 40.0, session=_FakeSession(_VSP_JSON))
    assert {c.chart_id for c in stars} == {"X42585ESI"}
    no_chart = {"photometry": _VSP_JSON["photometry"]}
    stars = vsp_chart(10.0, 20.0, 40.0, session=_FakeSession(no_chart))
    assert {c.chart_id for c in stars} == {None}


def test_vsp_can_request_a_sequence_by_target_name() -> None:
    session = _FakeSession(_VSP_JSON)
    stars = vsp_chart(300.5, 58.75, 240.0, target_name="XX Cyg", session=session)
    assert len(stars) == 2
    assert session.last_params["star"] == "XX Cyg"
    assert session.last_params["all"] == "on"
    # VSP rejects >180′ charts at maglimit 15; preserve useful faint
    # comparison stars by constraining the chart rather than failing silently.
    assert session.last_params["fov"] == "180.0"
    assert "ra" not in session.last_params and "dec" not in session.last_params


def test_vsp_empty_chart() -> None:
    assert vsp_chart(0, 0, 10, session=_FakeSession({"photometry": []})) == []
    assert vsp_chart(0, 0, 10, session=_FakeSession({})) == []


# --- offline cache ---------------------------------------------------------- #


def test_cache_serves_fresh_result_without_network() -> None:
    first = _FakeSession(_VSX_JSON)
    stars = vsx_cone_search(83.82, -5.39, 0.4, session=first)
    assert len(stars) == 2
    # Same query again: served from the fresh cache, no HTTP round-trip.
    offline = _FakeSession(raise_exc=requests.ConnectionError("offline"))
    cached = vsx_cone_search(83.82, -5.39, 0.4, session=offline)
    assert [s.name for s in cached] == [s.name for s in stars]
    assert offline.last_params is None  # session never called


def test_cache_key_survives_resolve_jitter() -> None:
    """A re-solve of the same field lands a few arcsec away — same cache key."""
    vsx_cone_search(83.82, -5.39, 0.4, session=_FakeSession(_VSX_JSON))
    offline = _FakeSession(raise_exc=requests.ConnectionError("offline"))
    jittered = vsx_cone_search(83.821, -5.389, 0.4, session=offline)
    assert len(jittered) == 2 and offline.last_params is None


def test_stale_cache_served_when_network_down(monkeypatch) -> None:
    vsx_cone_search(83.82, -5.39, 0.4, session=_FakeSession(_VSX_JSON))
    monkeypatch.setattr(aavso, "_CACHE_FRESH_S", 0.0)  # everything is stale now
    offline = _FakeSession(raise_exc=requests.ConnectionError("offline"))
    stars = vsx_cone_search(83.82, -5.39, 0.4, session=offline)
    assert len(stars) == 2
    assert offline.last_params is not None  # a refresh was attempted first


def test_radius_padding_never_shrinks_the_cone() -> None:
    sess = _FakeSession(_VSX_JSON)
    vsx_cone_search(83.82, -5.39, 0.4, session=sess)
    assert float(sess.last_params["radius"]) >= 0.4 + aavso._CENTER_STEP_DEG


def test_vsp_cache_roundtrip() -> None:
    vsp_chart(83.82, -5.39, 40.0, session=_FakeSession(_VSP_JSON))
    offline = _FakeSession(raise_exc=requests.ConnectionError("offline"))
    stars = vsp_chart(83.82, -5.39, 40.0, session=offline)
    assert [c.auid for c in stars] == ["000-BJX-214", "000-BJX-211"]


# --- error handling -------------------------------------------------------- #


def test_network_failure_raises_catalog_error() -> None:
    sess = _FakeSession(raise_exc=requests.ConnectionError("offline"))
    with pytest.raises(CatalogError):
        vsx_cone_search(0, 0, 0.1, session=sess)


def test_bad_json_raises_catalog_error() -> None:
    sess = _FakeSession(json_exc=ValueError("not json"))
    with pytest.raises(CatalogError):
        vsp_chart(0, 0, 10, session=sess)
