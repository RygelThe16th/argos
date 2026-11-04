"""Tests for the AstrometryController auto-solve policy (no ASTAP, no solving).

These exercise the pure decision logic (``_due``) + state (``set_auto`` /
``invalidate``). A ``QApplication`` is required because the controller is a
``QObject``; no event loop is run and no solve is started.
"""

from __future__ import annotations

import time

import pytest
from PyQt6.QtWidgets import QApplication

from argos.workers.astrometry_controller import AstrometryController


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _cfg(d: dict):
    return lambda key, default: d.get(key, default)


def test_due_when_no_wcs(qapp) -> None:
    c = AstrometryController(_cfg({}))
    assert c._due(None) is True  # no WCS yet → always due


def test_not_due_within_cadence(qapp) -> None:
    c = AstrometryController(_cfg({"astrometry.live_resolve_s": 100}))
    c._wcs = object()
    c._last_solve_monotonic = time.monotonic()
    c._last_solve_radec = (5.0, 22.0)
    assert c._due((5.0, 22.0)) is False  # fresh + same pointing → not due


def test_due_after_cadence(qapp) -> None:
    c = AstrometryController(_cfg({"astrometry.live_resolve_s": 0.0}))
    c._wcs = object()
    c._last_solve_monotonic = time.monotonic() - 5.0
    assert c._due(None) is True  # cadence elapsed


def test_due_on_mount_move(qapp) -> None:
    c = AstrometryController(
        _cfg({"astrometry.live_resolve_s": 1e6, "astrometry.live_resolve_arcmin": 2.0})
    )
    c._wcs = object()
    c._last_solve_monotonic = time.monotonic()
    c._last_solve_radec = (5.0, 22.0)
    assert c._due((5.0, 22.2)) is True  # 0.2° ≈ 12′ move > 2′ threshold


def test_mount_move_marks_an_existing_wcs_unsafe(qapp) -> None:
    """External telescope slews must not leave target apertures on old pixels."""
    c = AstrometryController(_cfg({"astrometry.live_resolve_arcmin": 2.0}))
    c._wcs = object()
    c._last_solve_radec = (5.0, 22.0)
    assert not c.mount_moved_since_solution((5.0, 22.01))
    assert c.mount_moved_since_solution((5.0, 22.2))


def test_invalidate_clears_wcs(qapp) -> None:
    c = AstrometryController(_cfg({}))
    c._wcs = object()
    c.invalidate()
    assert c.wcs is None


def test_set_auto_toggles(qapp) -> None:
    c = AstrometryController(_cfg({}))
    assert c.auto is False
    c.set_auto(True)
    assert c.auto is True


def test_on_new_frame_noop_when_not_armed(qapp) -> None:
    # Not armed → never touches the (None) raw frame, so no crash.
    c = AstrometryController(_cfg({}))
    c.on_new_frame(None, (10, 10))  # auto off → returns immediately


def test_internal_move_rebases_mount_check_instead_of_clearing(qapp) -> None:
    ctrl = AstrometryController(lambda key, default=None: default)
    ctrl._wcs = object()
    ctrl._last_solve_radec = (3.0, 41.0)
    ctrl.note_internal_move()
    # The mount now reports a pointing 3′ away: our own move, not a slew.
    assert not ctrl.mount_moved_since_solution((3.0, 41.05))
    assert ctrl._last_solve_radec == (3.0, 41.05)
    # A later external move is still detected.
    assert ctrl.mount_moved_since_solution((3.0, 41.2))
