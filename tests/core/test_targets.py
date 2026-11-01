"""Tests for the persistent target set (Qt-free, no hardware)."""

from __future__ import annotations

from dataclasses import replace

from argos.core.catalog.targets import (
    ROLE_CHECK,
    ROLE_COMPARISON,
    ROLE_TARGET,
    TargetSet,
    TargetStar,
    comparison_chart_id,
)


def _star(role=ROLE_TARGET, auid="000-BBB-001", ra=83.6, dec=22.0, name="NU Ori"):
    return TargetStar(role=role, ra_deg=ra, dec_deg=dec, auid=auid, name=name, source="vsx")


def test_set_role_adds_then_updates_by_identity() -> None:
    ts = TargetSet(object_name="M42")
    ts.set_role(_star(role=ROLE_TARGET))
    assert len(ts.stars) == 1
    # Same AUID → update in place (role change), not a duplicate.
    ts.set_role(_star(role=ROLE_COMPARISON))
    assert len(ts.stars) == 1
    assert ts.stars[0].role == ROLE_COMPARISON


def test_key_falls_back_to_position_without_auid() -> None:
    a = TargetStar(role=ROLE_TARGET, ra_deg=10.0, dec_deg=-5.0)
    b = TargetStar(role=ROLE_TARGET, ra_deg=10.0, dec_deg=-5.0)
    assert a.key() == b.key()  # same position → same identity
    assert a.key() != _star().key()


def test_by_role_and_remove() -> None:
    ts = TargetSet()
    ts.set_role(_star(auid="A", role=ROLE_TARGET))
    ts.set_role(_star(auid="B", role=ROLE_COMPARISON, name="comp"))
    assert len(ts.by_role(ROLE_TARGET)) == 1
    assert len(ts.by_role(ROLE_COMPARISON)) == 1
    ts.remove("auid:A")
    assert [s.auid for s in ts.stars] == ["B"]


def test_json_round_trip(tmp_path) -> None:
    ts = TargetSet(object_name="M42")
    ts.set_role(_star(role=ROLE_TARGET))
    ts.set_role(_star(auid="C", role=ROLE_COMPARISON, name="HD 37041"))
    path = tmp_path / "sub" / "targets.json"
    ts.save(path)  # creates parent dirs, atomic write
    back = TargetSet.load(path)
    assert back.object_name == "M42"
    assert {s.auid for s in back.stars} == {"000-BBB-001", "C"}
    assert back.by_role(ROLE_TARGET)[0].name == "NU Ori"


def test_catalogue_enriched_target_replaces_manual_same_position() -> None:
    ts = TargetSet(object_name="XX Cyg")
    ts.set_role(TargetStar(role=ROLE_TARGET, ra_deg=300.815175, dec_deg=58.954590, name="XX Cyg"))
    ts.set_role(
        TargetStar(
            role=ROLE_TARGET,
            ra_deg=300.815170,
            dec_deg=58.954580,
            auid="000-BCK-301",
            name="XX Cyg",
            source="vsx",
        )
    )
    targets = ts.by_role(ROLE_TARGET)
    assert len(targets) == 1
    assert targets[0].auid == "000-BCK-301"


def test_selection_manifest_is_explicit_and_saved(tmp_path) -> None:
    ts = TargetSet(object_name="XX Cyg")
    ts.set_role(
        _star(role=ROLE_TARGET, auid="000-BCK-301", ra=300.81517, dec=58.95458, name="XX Cyg")
    )
    ts.set_role(
        _star(role=ROLE_COMPARISON, auid="000-BJV-170", ra=300.70292, dec=59.00994, name="104")
    )
    ts.set_role(_star(role=ROLE_CHECK, auid="000-BJV-171", ra=300.89913, dec=58.92267, name="106"))
    path = tmp_path / "photometry_selection.json"
    ts.save_selection_manifest(path, generated_by="Argos test")
    saved = __import__("json").loads(path.read_text())
    assert saved["selection_status"] == "provisional_live_preview"
    assert saved["targets"][0]["auid"] == "000-BCK-301"
    assert saved["comparison_stars"][0]["name"] == "104"
    assert saved["check_stars"][0]["ra_deg_j2000"] == 300.89913

    history = tmp_path / "photometry_selection_history.jsonl"
    ts.append_selection_history(history, generated_by="Argos test")
    ts.append_selection_history(history, generated_by="Argos test")
    records = [__import__("json").loads(line) for line in history.read_text().splitlines()]
    assert len(records) == 2
    assert all(record["targets"][0]["name"] == "XX Cyg" for record in records)


def test_vsp_chart_id_is_saved_and_handed_off(tmp_path) -> None:
    ts = TargetSet(object_name="XX Cyg")
    comp = replace(_star(role=ROLE_COMPARISON, auid="000-BJV-170"), chart_id="X42585ESI")
    ts.set_role(comp)
    ts.save(tmp_path / "targets.json")
    assert TargetSet.load(tmp_path / "targets.json").stars[0].chart_id == "X42585ESI"
    manifest = ts.selection_manifest()
    assert manifest["comparison_stars"][0]["catalogue_chart_id"] == "X42585ESI"


def test_comparison_chart_id_needs_one_chart_for_every_comparison() -> None:
    c1 = replace(_star(role=ROLE_COMPARISON, auid="C1"), chart_id="X1")
    c2 = replace(_star(role=ROLE_COMPARISON, auid="C2"), chart_id="X1")
    target = _star(role=ROLE_TARGET, auid="T")  # no chart: targets do not count
    assert comparison_chart_id([target, c1, c2]) == "X1"
    assert comparison_chart_id([c1, replace(c2, chart_id="X2")]) is None  # two charts
    assert comparison_chart_id([c1, replace(c2, chart_id=None)]) is None  # manual comp
    assert comparison_chart_id([target]) is None


def test_load_missing_returns_empty(tmp_path) -> None:
    assert TargetSet.load(tmp_path / "nope.json").stars == []


def test_from_dict_ignores_unknown_keys() -> None:
    ts = TargetSet.from_dict(
        {"object": "X", "stars": [{"role": "target", "ra_deg": 1.0, "dec_deg": 2.0, "future": 9}]}
    )
    assert ts.stars[0].ra_deg == 1.0  # unknown 'future' key dropped, no crash


def test_display_name_prefers_name_then_auid() -> None:
    assert _star(name="NU Ori").display_name == "NU Ori"
    assert TargetStar(role="check", ra_deg=1, dec_deg=2, auid="Z").display_name == "Z"
    assert (
        TargetStar(
            role=ROLE_COMPARISON,
            ra_deg=1,
            dec_deg=2,
            auid="000-ABC-123",
            name="114",  # VSP chart magnitude code, not an object name
            source="vsp_auto",
        ).display_name
        == "000-ABC-123"
    )


def test_summary_counts_roles_and_readiness() -> None:
    empty = TargetSet(object_name="M42").summary()
    assert empty["target"] is None and empty["complete"] is False

    ts = TargetSet(object_name="M42")
    ts.set_role(_star(auid="A", role=ROLE_TARGET, name="NU Ori"))
    # Target alone is not yet enough for differential photometry.
    assert ts.summary()["complete"] is False
    ts.set_role(_star(auid="B", role=ROLE_COMPARISON, name="comp1"))
    ts.set_role(_star(auid="C", role=ROLE_COMPARISON, name="comp2"))
    ts.set_role(_star(auid="K", role=ROLE_CHECK, name="check1"))

    s = ts.summary()
    assert s["object"] == "M42"
    assert s["target"] == "NU Ori"
    assert s["n_comparison"] == 2
    assert s["n_check"] == 1
    assert s["complete"] is True
