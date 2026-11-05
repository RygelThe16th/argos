"""Auto-solve arming at sequence start (ImagingPage._on_sequence_running)."""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from argos.ui.pages.imaging_page import ImagingPage  # noqa: E402


class _Sig:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


def _page(wcs=None, auto=False):
    return SimpleNamespace(
        _astrometry=SimpleNamespace(wcs=wcs, auto=auto),
        auto_solve_armed=_Sig(),
        log_message=_Sig(),
        _sequence_context_lbl=SimpleNamespace(clear=lambda: None),
    )


def _run(page, running):
    ImagingPage._on_sequence_running(page, running)


@pytest.mark.parametrize(
    "wcs, after_goto, armed",
    [
        (None, False, False),  # unsolved, no GoTo: unchanged behaviour
        (object(), False, True),  # solved field: unchanged behaviour
        (None, True, True),  # scheduled GoTo cleared the solve: arm anyway
    ],
)
def test_arming_rule(wcs, after_goto, armed):
    page = _page(wcs=wcs)
    ImagingPage.set_solve_for_next_run(page, after_goto)
    _run(page, True)
    assert (page.auto_solve_armed.calls == [(True,)]) is armed
    if armed and after_goto:
        assert "GoTo before the run" in page.log_message.calls[0][1]
    # The user's previous choice (off) is restored after the run.
    _run(page, False)
    if armed:
        assert page.auto_solve_armed.calls[-1] == (False,)


def test_goto_request_is_consumed_by_one_run():
    page = _page()
    ImagingPage.set_solve_for_next_run(page, True)
    _run(page, True)
    _run(page, False)
    page.auto_solve_armed.calls.clear()
    _run(page, True)  # a later, ordinary run on an unsolved field
    assert page.auto_solve_armed.calls == []


def test_withdrawn_request_does_not_arm():
    page = _page()
    ImagingPage.set_solve_for_next_run(page, True)
    ImagingPage.set_solve_for_next_run(page, False)
    _run(page, True)
    assert page.auto_solve_armed.calls == []
