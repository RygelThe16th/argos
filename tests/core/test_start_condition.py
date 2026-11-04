"""Scheduled-start rules: time, altitude, GoTo prerequisites."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from argos.core.imaging import start_condition as sc

# Ashikaga-ish site; GZ And (RA 0.6h, Dec +44) and an object that never rises.
_SITE = dict(lat_deg=36.3, lon_deg=139.5)
_GZ_AND = sc.StartTarget("GZ And", 0.66, 44.7, **_SITE)
_SOUTHERN = sc.StartTarget("Southern", 6.4, -75.0, **_SITE)
_NOW = datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc)  # 18:00 JST


def _time(delta_s: float, **kw) -> sc.StartCondition:
    return sc.StartCondition(sc.START_AT_TIME, start_at=_NOW + timedelta(seconds=delta_s), **kw)


def test_now_without_goto_is_immediate():
    assert sc.StartCondition().immediate
    assert not sc.StartCondition(goto=True, target=_GZ_AND).immediate
    assert not _time(60).immediate


def test_time_waits_then_is_due():
    cond = _time(754)
    pending = sc.check(cond, _NOW)
    assert not pending.due and pending.remaining_s == pytest.approx(754)
    assert "12m 34s" in pending.status
    assert sc.check(cond, _NOW + timedelta(seconds=754)).due


def test_past_time_is_reported_not_rejected():
    cond = _time(-60)
    assert sc.problem(cond, _NOW) is None
    assert sc.is_past(cond, _NOW)
    later = cond.now_instead()
    assert later.mode == sc.START_NOW and later.immediate


def test_past_time_with_goto_keeps_goto():
    cond = _time(-60, goto=True, target=_GZ_AND).now_instead()
    assert cond.goto and not cond.immediate


def test_naive_time_rejected():
    cond = sc.StartCondition(sc.START_AT_TIME, start_at=datetime(2026, 11, 4, 21, 0))
    assert "date and time" in sc.problem(cond, _NOW)


def test_altitude_needs_target_and_site():
    assert "target" in sc.problem(sc.StartCondition(sc.START_ABOVE_ALTITUDE), _NOW)
    no_site = sc.StartTarget("GZ And", 0.66, 44.7)
    cond = sc.StartCondition(sc.START_ABOVE_ALTITUDE, target=no_site)
    assert "site" in sc.problem(cond, _NOW)


def test_altitude_never_reached_is_refused_with_peak():
    cond = sc.StartCondition(sc.START_ABOVE_ALTITUDE, min_altitude_deg=30, target=_SOUTHERN)
    message = sc.problem(cond, _NOW)
    assert "does not reach 30°" in message and "highest" in message


def test_altitude_waits_for_crossing_then_due():
    now_alt = sc.target_altitude(_GZ_AND, _NOW)
    limit = now_alt + 10.0  # GZ And is rising in the evening
    cond = sc.StartCondition(sc.START_ABOVE_ALTITUDE, min_altitude_deg=limit, target=_GZ_AND)
    assert sc.problem(cond, _NOW) is None
    pending = sc.check(cond, _NOW)
    assert not pending.due and pending.altitude_deg == pytest.approx(now_alt)
    crossing = sc.crossing_time(cond, _NOW)
    assert crossing is not None and crossing > _NOW
    assert sc.check(cond, crossing).due


def test_altitude_already_above_is_due():
    cond = sc.StartCondition(sc.START_ABOVE_ALTITUDE, min_altitude_deg=0.0, target=_GZ_AND)
    assert sc.check(cond, _NOW).due


def test_goto_needs_target():
    assert "GoTo" in sc.problem(sc.StartCondition(goto=True), _NOW)
