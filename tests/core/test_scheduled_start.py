"""ScheduledStart: waits, optional GoTo, never starts on the wrong field."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from argos.core.imaging import start_condition as sc  # noqa: E402
from argos.core.session import scheduled_start as mod  # noqa: E402
from argos.core.session.scheduled_start import ScheduledStart  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _app():
    return QApplication.instance() or QApplication([])


class _Log:
    def __init__(self):
        self.lines = []

    def emit(self, level, message):
        self.lines.append((level, message))


class _Engine:
    def __init__(self, accept=True):
        self.log_message = _Log()
        self.started = []
        self.accept = accept

    def start_sequence(self, plan):
        self.started.append(plan)
        return self.accept


@dataclass
class _Pos:
    slewing: bool


class _Session:
    def __init__(self, telescope=True, goto_ok=True):
        self.telescope = object() if telescope else None
        self.goto_ok = goto_ok
        self.gotos = []
        self.last_position = _Pos(False)

    def goto(self, ra, dec):
        self.gotos.append((ra, dec))
        return self.goto_ok


class _Clock:
    def __init__(self):
        self.wall = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
        self.mono = 0.0

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds


_TARGET = sc.StartTarget("GZ And", 0.66, 44.7, 36.3, 139.5)


def _make(engine=None, session=None):
    clock = _Clock()
    engine = engine or _Engine()
    session = session or _Session()
    sched = ScheduledStart(engine, session, now=lambda: clock.wall, monotonic=lambda: clock.mono)
    sched._timer.stop = lambda: None  # the tests drive tick() themselves
    sched._timer.start = lambda *a: None
    outcomes, active = [], []
    sched.ended.connect(lambda outcome, msg: outcomes.append(outcome))
    sched.active.connect(active.append)
    return sched, clock, engine, session, outcomes, active


def test_waits_until_time_then_starts_once():
    sched, clock, engine, _, outcomes, active = _make()
    sched.arm(
        "PLAN", sc.StartCondition(sc.START_AT_TIME, start_at=clock.wall + timedelta(minutes=3))
    )
    assert active == [True] and engine.started == []
    for _ in range(179):
        clock.advance(1)
        sched.tick()
    assert engine.started == []
    clock.advance(1)
    sched.tick()
    sched.tick()
    assert engine.started == ["PLAN"]
    assert outcomes == ["started"] and active == [True, False]
    assert not sched.is_active


def test_cancel_while_waiting_starts_nothing():
    sched, clock, engine, _, outcomes, _ = _make()
    sched.arm(
        "PLAN", sc.StartCondition(sc.START_AT_TIME, start_at=clock.wall + timedelta(minutes=3))
    )
    sched.cancel()
    clock.advance(600)
    sched.tick()
    assert engine.started == [] and outcomes == ["cancelled"]
    assert any("nothing was exposed" in m for _, m in engine.log_message.lines)


def test_goto_waits_for_slew_and_settle_before_starting():
    sched, clock, engine, session, outcomes, _ = _make()
    sched.arm("PLAN", sc.StartCondition(goto=True, target=_TARGET))
    assert session.gotos == [(0.66, 44.7)]
    session.last_position = _Pos(True)
    for _ in range(20):
        clock.advance(1)
        sched.tick()
    assert engine.started == []  # still slewing
    session.last_position = _Pos(False)
    clock.advance(1)
    sched.tick()  # slew finished → settling
    assert engine.started == []
    clock.advance(mod.SLEW_SETTLE_S)
    sched.tick()
    assert engine.started == ["PLAN"] and outcomes == ["started"]


def test_goto_without_reported_slewing_starts_after_grace():
    sched, clock, engine, session, _, _ = _make()
    sched.arm("PLAN", sc.StartCondition(goto=True, target=_TARGET))
    clock.advance(mod.SLEW_START_GRACE_S - 1)
    sched.tick()
    assert engine.started == []
    clock.advance(1)
    sched.tick()
    clock.advance(mod.SLEW_SETTLE_S)
    sched.tick()
    assert engine.started == ["PLAN"]


@pytest.mark.parametrize(
    "session, reason",
    [(_Session(telescope=False), "not connected"), (_Session(goto_ok=False), "refused")],
)
def test_goto_failure_does_not_start(session, reason):
    sched, clock, engine, _, outcomes, _ = _make(session=session)
    sched.arm("PLAN", sc.StartCondition(goto=True, target=_TARGET))
    assert engine.started == [] and outcomes == ["failed"]
    assert any(level == "ERROR" and reason in m for level, m in engine.log_message.lines)


def test_goto_timeout_does_not_start():
    sched, clock, engine, session, outcomes, _ = _make()
    sched.arm("PLAN", sc.StartCondition(goto=True, target=_TARGET))
    session.last_position = _Pos(True)
    clock.advance(mod.SLEW_TIMEOUT_S)
    sched.tick()
    assert engine.started == [] and outcomes == ["failed"]


def test_mount_lost_during_goto_does_not_start():
    sched, clock, engine, session, outcomes, _ = _make()
    sched.arm("PLAN", sc.StartCondition(goto=True, target=_TARGET))
    session.telescope = None
    clock.advance(1)
    sched.tick()
    assert engine.started == [] and outcomes == ["failed"]


def test_engine_refusal_reported():
    sched, clock, engine, _, outcomes, active = _make(engine=_Engine(accept=False))
    sched.arm("PLAN", sc.StartCondition(sc.START_AT_TIME, start_at=clock.wall))
    assert engine.started == ["PLAN"] and outcomes == ["failed"]
    assert active[-1] is False


def test_status_counts_down():
    sched, clock, _, _, _, _ = _make()
    statuses = []
    sched.status.connect(statuses.append)
    sched.arm(
        "PLAN", sc.StartCondition(sc.START_AT_TIME, start_at=clock.wall + timedelta(seconds=90))
    )
    clock.advance(30)
    sched.tick()
    assert "1m 30s" in statuses[0] and "1m 00s" in statuses[-1]


def test_goto_start_requests_solve_for_the_run():
    sched, clock, engine, session, _, _ = _make()
    requests = []
    sched.solve_for_run.connect(requests.append)
    engine_calls = []
    original = engine.start_sequence

    def start(plan):
        engine_calls.append(list(requests))  # what was requested before the start
        return original(plan)

    engine.start_sequence = start
    sched.arm("PLAN", sc.StartCondition(goto=True, target=_TARGET))
    clock.advance(mod.SLEW_START_GRACE_S)
    sched.tick()
    clock.advance(mod.SLEW_SETTLE_S)
    sched.tick()
    assert engine_calls == [[True]] and requests == [True]


def test_refused_goto_start_withdraws_solve_request():
    sched, clock, engine, _, _, _ = _make(engine=_Engine(accept=False))
    requests = []
    sched.solve_for_run.connect(requests.append)
    sched.arm("PLAN", sc.StartCondition(goto=True, target=_TARGET))
    clock.advance(mod.SLEW_START_GRACE_S)
    sched.tick()
    clock.advance(mod.SLEW_SETTLE_S)
    sched.tick()
    assert requests == [True, False]


def test_time_start_without_goto_requests_nothing():
    sched, clock, _, _, _, _ = _make()
    requests = []
    sched.solve_for_run.connect(requests.append)
    sched.arm("PLAN", sc.StartCondition(sc.START_AT_TIME, start_at=clock.wall))
    assert requests == []
