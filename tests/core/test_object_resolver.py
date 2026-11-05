from __future__ import annotations

from pathlib import Path

import pytest

from argos.core.catalog import object_resolver
from argos.core.catalog.object_resolver import (
    ObjectResolutionError,
    ResolvedObject,
    cached_object_suggestions,
    nearby_cached_objects,
    nearby_essential_objects,
    normalize_designation,
    is_variable_object_type,
    object_type_label,
    resolve_nearby_objects,
    resolve_object,
)

_SESAME_XML = """<?xml version='1.0'?>
<Sesame>
  <Target>
    <Resolver name='Simbad'>
      <oname>HD 189733</oname>
      <otype>Star</otype>
      <jradeg>300.18210000</jradeg>
      <jdedeg>22.70990000</jdedeg>
    </Resolver>
  </Target>
</Sesame>
"""

_XX_CYG_XML = """<?xml version='1.0'?>
<Sesame>
  <Target>
    <Resolver name='Simbad'>
      <oname>V* XX Cyg</oname>
      <otype>SX*</otype>
      <jradeg>300.81517513</jradeg>
      <jdedeg>58.95459035</jdedeg>
    </Resolver>
  </Target>
</Sesame>
"""


def test_resolve_object_parses_and_caches_response(tmp_path: Path, monkeypatch) -> None:
    calls = 0

    class Response:
        text = _SESAME_XML

        def raise_for_status(self) -> None:
            pass

    def fake_get(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return Response()

    monkeypatch.setattr(object_resolver.requests, "get", fake_get)
    cache = tmp_path / "resolver.json"

    first = resolve_object("HD 189733", cache_path=cache)
    second = resolve_object("  hd   189733 ", cache_path=cache)

    assert first.name == "HD 189733"
    assert first.ra_hours == pytest.approx(20.01214)
    assert first.dec_degrees == pytest.approx(22.7099)
    assert second == first
    assert calls == 1


def test_resolve_object_rejects_empty_query(tmp_path: Path) -> None:
    with pytest.raises(ObjectResolutionError, match="designation"):
        resolve_object("  ", cache_path=tmp_path / "resolver.json")


def test_normalize_compact_designations() -> None:
    assert normalize_designation("hd189733") == "HD 189733"
    assert normalize_designation("ngc7000") == "NGC 7000"
    assert object_type_label("V*") == "Variable star"
    assert object_type_label("SX*") == "SX Phoenicis variable"
    assert is_variable_object_type("SX*")
    assert not is_variable_object_type("Star")


def test_resolver_keeps_variable_type_out_of_the_designation(tmp_path: Path, monkeypatch) -> None:
    """``V*`` is SIMBAD metadata, never part of the observer-facing name."""

    class Response:
        text = _XX_CYG_XML

        def raise_for_status(self) -> None:
            pass

    monkeypatch.setattr(object_resolver.requests, "get", lambda *_a, **_k: Response())
    cache = tmp_path / "resolver.json"
    result = resolve_object("xx cyg", cache_path=cache)

    assert result.name == "XX Cyg"
    assert result.object_type == "SX*"
    # A pre-fix cache must not preserve the misleading prefix either.
    cache.write_text(
        '{"xxcyg": {"name": "V* XX Cyg", "ra_degrees": 300.81517513, '
        '"dec_degrees": 58.95459035, "object_type": "SX*"}}',
        encoding="utf-8",
    )
    assert resolve_object("XX Cyg", cache_path=cache).name == "XX Cyg"


def test_embedded_catalogue_resolves_common_deep_sky_targets_offline(
    tmp_path: Path, monkeypatch
) -> None:
    def fail_network(*_args, **_kwargs):
        raise AssertionError("built-in catalogue must not query CDS")

    monkeypatch.setattr(object_resolver.requests, "get", fail_network)

    m42 = resolve_object("m42", cache_path=tmp_path / "resolver.json")
    ngc = resolve_object("ngc224", cache_path=tmp_path / "resolver.json")
    ic = resolve_object("ic434", cache_path=tmp_path / "resolver.json")

    assert m42.name == "M 42"
    assert m42.source == "Argos Essential Catalogue"
    assert ngc.name == "NGC 224"
    assert ic.name == "IC 434"


def test_embedded_catalogue_offline_suggestions_and_nearby_match() -> None:
    suggestions = cached_object_suggestions("m4")
    nearby = nearby_essential_objects(10.675, 41.2666667, radius_arcsec=1.0)

    assert "M 42" in suggestions
    assert nearby[0].object.name == "NGC 224"
    assert nearby[0].object.source == "Argos Essential Catalogue"


def test_nearby_cache_is_offline_and_sorted(tmp_path: Path) -> None:
    cache = tmp_path / "resolver.json"
    cache.write_text(
        """{
          "a": {"name": "Far", "ra_degrees": 10.01, "dec_degrees": 20.0},
          "b": {"name": "Target", "ra_degrees": 10.00001, "dec_degrees": 20.0}
        }""",
        encoding="utf-8",
    )

    matches = nearby_cached_objects(10.0, 20.0, radius_arcsec=90.0, cache_path=cache)

    assert [match.object.name for match in matches] == ["Target", "Far"]
    assert matches[0].separation_arcsec < 1.0


def test_nearby_lookup_uses_cache_without_network(tmp_path: Path, monkeypatch) -> None:
    cache = tmp_path / "resolver.json"
    cache.write_text(
        '{"hd": {"name": "HD 189733", "ra_degrees": 300.1821, "dec_degrees": 22.7099}}',
        encoding="utf-8",
    )

    def fail_network(*_args, **_kwargs):
        raise AssertionError("cache match must not query SIMBAD")

    monkeypatch.setattr(object_resolver.requests, "get", fail_network)
    matches = resolve_nearby_objects(300.1821, 22.7099, cache_path=cache)

    assert matches[0].object == ResolvedObject("HD 189733", 300.1821, 22.7099)


def test_nearby_lookup_can_be_strictly_offline(tmp_path: Path, monkeypatch) -> None:
    def fail_network(*_args, **_kwargs):
        raise AssertionError("offline mode must not query SIMBAD")

    monkeypatch.setattr(object_resolver.requests, "get", fail_network)
    assert resolve_nearby_objects(1.0, 2.0, allow_network=False, cache_path=tmp_path / "none") == []


def test_nearby_lookup_parses_simbad_tap_and_caches(tmp_path: Path, monkeypatch) -> None:
    class Response:
        text = "main_id,ra,dec,otype\nHD 189733,300.1821,22.7099,Star\n"

        def raise_for_status(self) -> None:
            pass

    request: dict = {}

    def fake_get(*_args, **kwargs):
        request.update(kwargs)
        return Response()

    monkeypatch.setattr(object_resolver.requests, "get", fake_get)
    cache = tmp_path / "resolver.json"
    matches = resolve_nearby_objects(300.1821, 22.7099, cache_path=cache)

    assert matches[0].object.name == "HD 189733"
    assert matches[0].separation_arcsec == pytest.approx(0.0)
    assert request["params"]["format"] == "csv"
    assert "CONTAINS" in request["params"]["query"]
    assert resolve_object("hd189733", cache_path=cache).name == "HD 189733"


_BET_PER_VSX = {
    "VSXObject": {
        "Name": "bet Per",
        "RA2000": "47.04221",
        "Declination2000": "40.95567",
        "VariabilityType": "EA/SD",
        "Category": "Variable",
    }
}

_UNKNOWN_SESAME_XML = (
    "<?xml version='1.0'?><Sesame><Target><INFO>*** Nothing found</INFO></Target></Sesame>"
)


class _JsonResponse:
    def __init__(self, payload) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self):
        return self._payload


class _TextResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        pass


def _fake_network(monkeypatch, *, sesame, vsx):
    """Route requests.get: ``sesame``/``vsx`` are a response or an exception."""
    calls: list[str] = []

    def fake_get(url, *_args, **_kwargs):
        which = "vsx" if url == object_resolver._VSX_URL else "sesame"
        calls.append(which)
        outcome = vsx if which == "vsx" else sesame
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(object_resolver.requests, "get", fake_get)
    return calls


def test_cds_outage_falls_back_to_vsx_and_caches(tmp_path: Path, monkeypatch) -> None:
    calls = _fake_network(
        monkeypatch,
        sesame=object_resolver.requests.ReadTimeout("CDS down"),
        vsx=_JsonResponse(_BET_PER_VSX),
    )
    cache = tmp_path / "resolver.json"

    result = resolve_object("Bet Per", cache_path=cache)

    assert result.name == "bet Per"
    assert result.ra_degrees == pytest.approx(47.04221)
    assert result.dec_degrees == pytest.approx(40.95567)
    assert result.source == "AAVSO VSX"
    assert is_variable_object_type(result.object_type)
    assert calls == ["sesame", "vsx"]
    # Cached: a second lookup needs neither service.
    assert resolve_object("bet  Per", cache_path=cache) == result
    assert calls == ["sesame", "vsx"]


def test_name_unknown_to_sesame_is_tried_in_vsx(tmp_path: Path, monkeypatch) -> None:
    _fake_network(
        monkeypatch,
        sesame=_TextResponse(_UNKNOWN_SESAME_XML),
        vsx=_JsonResponse(_BET_PER_VSX),
    )
    assert resolve_object("bet Per", cache_path=tmp_path / "r.json").source == "AAVSO VSX"


def test_both_unreachable_reports_catalogue_unavailable(tmp_path: Path, monkeypatch) -> None:
    timeout = object_resolver.requests.ConnectTimeout("down")
    _fake_network(monkeypatch, sesame=timeout, vsx=timeout)
    with pytest.raises(ObjectResolutionError, match="Catalogue unavailable"):
        resolve_object("bet Per", cache_path=tmp_path / "r.json")


def test_unknown_everywhere_keeps_sesame_not_found_message(tmp_path: Path, monkeypatch) -> None:
    _fake_network(
        monkeypatch,
        sesame=_TextResponse(_UNKNOWN_SESAME_XML),
        vsx=_JsonResponse({"VSXObject": []}),
    )
    with pytest.raises(ObjectResolutionError, match="No ICRS coordinates"):
        resolve_object("zzqq Foo", cache_path=tmp_path / "r.json")


def test_vsx_suspected_variable_is_not_a_photometry_target(tmp_path: Path, monkeypatch) -> None:
    suspected = {"VSXObject": dict(_BET_PER_VSX["VSXObject"], Category="Suspected")}
    _fake_network(
        monkeypatch,
        sesame=object_resolver.requests.ConnectionError("down"),
        vsx=_JsonResponse(suspected),
    )
    result = resolve_object("bet Per", cache_path=tmp_path / "r.json")
    assert not is_variable_object_type(result.object_type)
