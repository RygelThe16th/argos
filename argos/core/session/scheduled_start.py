"""ScheduledStart — hold a ready sequence until its start condition, then run it.

Lives on the UI thread next to the engine. ``arm()`` takes a frozen plan and a
:class:`~argos.core.imaging.start_condition.StartCondition`; a 1 s timer
re-evaluates the condition against wall time. When it is due:

1. optionally GoTo the target (the same ``DeviceSession.goto`` as the
   Telescope panel) and wait until the mount reports the slew finished, plus
   a short settle;
2. ``engine.start_sequence(plan)`` — the engine stays the authority that
   accepts or refuses the run.

A GoTo that cannot be made (no mount, refused, never finishes) ends the wait
**without** starting: an unattended run on the wrong field is worse than none.
``cancel()`` at any point before the start guarantees nothing is exposed.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Callable

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from argos.core.imaging import start_condition as sc

#: A mount that never reports Slewing=True is taken as already there after this.
SLEW_START_GRACE_S = 5.0
#: Give up on a GoTo that has not finished after this long.
SLEW_TIMEOUT_S = 300.0
#: Wait after the slew finishes before the first exposure.
SLEW_SETTLE_S = 3.0

_IDLE, _WAITING, _SLEWING, _SETTLING = "idle", "waiting", "slewing", "settling"


class ScheduledStart(QObject):
    """Waits for a start condition, optionally slews, then starts the plan.

    Signals:
        status(str):     observer-facing state line (refreshed every tick).
        active(bool):    True while waiting/slewing; False once started or ended.
        ended(str, str): outcome ("started" | "cancelled" | "failed"), message.
        solve_for_run(bool): True just before a run that follows our GoTo (the
                         slew cleared the solution, so the run must re-solve
                         from scratch); False if that run was then refused.
    """

    status = pyqtSignal(str)
    active = pyqtSignal(bool)
    ended = pyqtSignal(str, str)
    solve_for_run = pyqtSignal(bool)

    def __init__(
        self,
        engine,
        session,
        parent: QObject | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._session = session
        self._now = now or (lambda: datetime.now().astimezone())
        self._monotonic = monotonic
        self._phase = _IDLE
        self._plan = None
        self._cond: sc.StartCondition | None = None
        self._phase_started = 0.0
        self._slew_seen = False
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.tick)

    # ------------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        return self._phase != _IDLE

    def _log(self, level: str, message: str) -> None:
        self._engine.log_message.emit(level, message)

    def arm(self, plan, cond: sc.StartCondition) -> None:
        """Hold ``plan`` until ``cond`` is due (checked immediately)."""
        self._plan, self._cond = plan, cond
        self._phase = _WAITING
        self.active.emit(True)
        detail = sc.check(cond, self._now()).status
        goto = f", GoTo {cond.target.name} first" if cond.goto and cond.target else ""
        self._log("CMD", f"Scheduled start armed — {detail}{goto}. Stop cancels it.")
        self._timer.start()
        self.tick()

    def cancel(self) -> None:
        if self._phase == _IDLE:
            return
        self._finish("cancelled", "Scheduled start cancelled — nothing was exposed.", "INFO")

    # ------------------------------------------------------------------

    def tick(self) -> None:
        if self._phase == _WAITING:
            result = sc.check(self._cond, self._now())
            self.status.emit(result.status)
            if result.due:
                if self._cond.goto:
                    self._begin_goto()
                else:
                    self._launch()
        elif self._phase == _SLEWING:
            self._watch_slew()
        elif self._phase == _SETTLING:
            if self._monotonic() - self._phase_started >= SLEW_SETTLE_S:
                self._launch()

    def _begin_goto(self) -> None:
        target = self._cond.target
        if self._session.telescope is None:
            self._finish(
                "failed",
                "Scheduled start: mount not connected — GoTo impossible, " "sequence not started.",
                "ERROR",
            )
            return
        self.status.emit(f"Slewing to {target.name}…")
        self._log("CMD", f"Scheduled start: GoTo {target.name}")
        if not self._session.goto(target.ra_hours, target.dec_deg):
            self._finish("failed", "Scheduled start: GoTo refused — sequence not started.", "ERROR")
            return
        self._phase = _SLEWING
        self._phase_started = self._monotonic()
        self._slew_seen = False

    def _watch_slew(self) -> None:
        elapsed = self._monotonic() - self._phase_started
        if self._session.telescope is None:
            self._finish(
                "failed",
                "Scheduled start: mount lost during GoTo — sequence not " "started.",
                "ERROR",
            )
            return
        pos = self._session.last_position
        slewing = bool(pos is not None and pos.slewing)
        if slewing:
            self._slew_seen = True
        arrived = (self._slew_seen and not slewing) or (
            not self._slew_seen and elapsed >= SLEW_START_GRACE_S
        )
        if arrived:
            self.status.emit("GoTo finished — settling…")
            self._log("INFO", "Scheduled start: GoTo finished, settling before the first frame")
            self._phase = _SETTLING
            self._phase_started = self._monotonic()
            return
        if elapsed >= SLEW_TIMEOUT_S:
            self._finish(
                "failed",
                f"Scheduled start: GoTo did not finish within {SLEW_TIMEOUT_S:.0f}s — "
                "sequence not started.",
                "ERROR",
            )
            return
        self.status.emit(f"Slewing to {self._cond.target.name}… {elapsed:.0f}s")

    def _launch(self) -> None:
        plan = self._plan
        self._timer.stop()
        self._phase = _IDLE
        self._plan = None
        slewed = bool(self._cond is not None and self._cond.goto)
        if slewed:
            self.solve_for_run.emit(True)
        if self._engine.start_sequence(plan):
            self.active.emit(False)
            self.ended.emit("started", "Scheduled start: sequence started.")
            self._log("OK", "Scheduled start: sequence started.")
        else:
            if slewed:
                self.solve_for_run.emit(False)
            self.active.emit(False)
            self.ended.emit("failed", "Scheduled start: the sequence was refused (see the log).")

    def _finish(self, outcome: str, message: str, level: str) -> None:
        self._timer.stop()
        self._phase = _IDLE
        self._plan = None
        self.active.emit(False)
        self.ended.emit(outcome, message)
        self._log(level, message)
