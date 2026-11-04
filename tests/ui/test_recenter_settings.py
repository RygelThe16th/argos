"""Re-centering tab of the field-identification settings dialog."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from argos.ui.widgets.astrometry_settings import (  # noqa: E402
    SECTION_RECENTER,
    AstrometrySettingsDialog,
)


@pytest.fixture(scope="module", autouse=True)
def _app():
    return QApplication.instance() or QApplication([])


class _Cfg:
    def __init__(self, d=None):
        self.d = dict(d or {})

    def get(self, key, default=None):
        return self.d.get(key, default)

    def set(self, key, value):
        self.d[key] = value

    def save(self):
        pass


def test_recenter_tab_defaults_off_with_inputs_disabled():
    dlg = AstrometrySettingsDialog(_Cfg(), section=SECTION_RECENTER)
    assert dlg._tabs.currentIndex() == 2
    assert dlg._recenter_mode.currentData() == "off"
    assert not dlg._recenter_threshold.isEnabled()
    assert not dlg._recenter_every.isEnabled()


def test_recenter_tab_round_trips_config():
    cfg = _Cfg({"recenter.mode": "every_n", "recenter.every_n": 12})
    dlg = AstrometrySettingsDialog(cfg, section=SECTION_RECENTER)
    assert dlg._recenter_mode.currentData() == "every_n"
    assert dlg._recenter_every.value() == 12
    assert dlg._recenter_every.isEnabled() and not dlg._recenter_threshold.isEnabled()
    dlg._recenter_mode.setCurrentIndex(dlg._recenter_mode.findData("threshold"))
    dlg._recenter_threshold.setValue(35)
    dlg._on_save()
    assert cfg.d["recenter.mode"] == "threshold"
    assert cfg.d["recenter.threshold_px"] == 35.0
    assert cfg.d["recenter.every_n"] == 12
