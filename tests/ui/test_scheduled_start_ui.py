"""Sequence control: scheduled-start options, waiting state, cancel, past time."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtCore import QDateTime  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from argos.core.config import _DEFAULTS, Config  # noqa: E402
from argos.core.imaging import start_condition as sc  # noqa: E402
from argos.core.imaging.sequencer import SequenceStep  # noqa: E402
from argos.core.session.acquisition_engine import AcquisitionEngine  # noqa: E402
from argos.core.session.device_session import DeviceSession  # noqa: E402
from argos.ui.pages.sequencer_page import SequencerPage  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def page(tmp_path):
    cfg = Config(dict(_DEFAULTS, sessions_path=str(tmp_path)))
    session = DeviceSession(cfg)
    engine = AcquisitionEngine(cfg, session)
    started = []
    engine.start_sequence = lambda plan: started.append(plan) or True
    p = SequencerPage(cfg, session, engine)
    p.started = started
    p._scheduler._timer.start = lambda *a: None  # tests tick by hand
    yield p
    p._scheduler.cancel()


def _set_mode(panel, mode):
    panel._start_mode.setCurrentIndex(panel._start_mode.findData(mode))


def _plan(page):
    plan = page.panel.to_plan()
    plan.steps = [SequenceStep(count=1)]
    return plan


def test_default_start_is_immediate(page):
    assert page.panel._start_mode.currentData() == sc.START_NOW
    assert not page.panel._start_goto_chk.isChecked()
    page._on_start(_plan(page))
    assert len(page.started) == 1 and not page._scheduler.is_active


def test_future_time_waits_locks_and_stop_cancels(page):
    panel = page.panel
    _set_mode(panel, sc.START_AT_TIME)
    panel._start_time.setDateTime(QDateTime.currentDateTime().addSecs(300))
    page._on_start(_plan(page))
    assert page._scheduler.is_active and page.started == []
    assert not panel._start_btn.isEnabled() and panel._stop_btn.isEnabled()
    assert not panel._table.isEnabled()
    assert "Waiting for" in panel._status_lbl.text()
    page._on_stop()
    assert not page._scheduler.is_active and page.started == []
    assert panel._start_btn.isEnabled() and panel._table.isEnabled()
    assert "cancelled" in panel._status_lbl.text()


@pytest.mark.parametrize("accept, expected", [(False, 0), (True, 1)])
def test_past_time_asks_before_starting(page, accept, expected):
    _set_mode(page.panel, sc.START_AT_TIME)
    page.panel._start_time.setDateTime(QDateTime.currentDateTime().addSecs(-600))
    asked = []
    page._confirm_start_now = lambda when: asked.append(when) or accept
    page._on_start(_plan(page))
    assert len(asked) == 1
    assert len(page.started) == expected
    assert not page._scheduler.is_active


def test_goto_option_needs_a_target(page):
    panel = page.panel
    assert not panel._start_goto_chk.isEnabled()
    page.set_target_coordinates("GZ And", 0.66, 44.7)
    assert panel._start_goto_chk.isEnabled()
    panel._start_goto_chk.setChecked(True)
    cond = page._start_condition()
    assert cond.goto and cond.target.name == "GZ And"
    # No saved site → altitude start is refused with a reason, nothing armed.
    _set_mode(panel, sc.START_ABOVE_ALTITUDE)
    page._on_start(_plan(page))
    assert not page._scheduler.is_active and page.started == []
    assert "site" in panel._status_lbl.text()


def test_goto_now_without_mount_does_not_start(page):
    page.set_target_coordinates("GZ And", 0.66, 44.7)
    page.panel._start_goto_chk.setChecked(True)
    page._on_start(_plan(page))
    assert page.started == []
    assert "mount not connected" in page.panel._status_lbl.text()


def test_prefill_start_time_keeps_mode(page):
    when = datetime.now().astimezone().replace(second=0, microsecond=0) + timedelta(hours=2)
    page.panel.set_scheduled_start_time(when)
    assert page.panel._start_mode.currentData() == sc.START_NOW
    assert page.panel.start_condition().start_at == when
