"""When a scheduled sequence may start — Qt-free decisions.

The Sequencer can hold a ready plan until a local clock time, or until the
target climbs above an altitude, and optionally GoTo the target first.  The
waiting itself lives in :class:`argos.core.session.scheduled_start.ScheduledStart`;
this module only answers "is it time yet?" and "can it ever be?", so the rules
are unit-tested without a timer or a telescope.

The clock is compared against wall time on every tick, never counted down, so
a computer that sleeps or has its clock corrected still starts on time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from argos.core.imaging.sky_geometry import altitude_at

START_NOW = "now"
START_AT_TIME = "time"
START_ABOVE_ALTITUDE = "altitude"
START_MODES: tuple[str, ...] = (START_NOW, START_AT_TIME, START_ABOVE_ALTITUDE)

#: How far ahead the altitude rule looks for a crossing before refusing.
ALTITUDE_LOOKAHEAD = timedelta(hours=12)
_ALTITUDE_STEP = timedelta(minutes=5)


@dataclass(frozen=True)
class StartTarget:
    """Where the target is (J2000) and where we observe from."""

    name: str
    ra_hours: float
    dec_deg: float
    lat_deg: float | None = None
    lon_deg: float | None = None

    @property
    def has_site(self) -> bool:
        return self.lat_deg is not None and self.lon_deg is not None


@dataclass(frozen=True)
class StartCondition:
    """What the observer asked for in Sequence control."""

    mode: str = START_NOW
    start_at: datetime | None = None  # timezone-aware
    min_altitude_deg: float = 30.0
    goto: bool = False
    target: StartTarget | None = None

    @property
    def immediate(self) -> bool:
        return self.mode == START_NOW and not self.goto

    def now_instead(self) -> "StartCondition":
        """Same options, but start immediately (the observer accepted a past time)."""
        return StartCondition(START_NOW, None, self.min_altitude_deg, self.goto, self.target)


@dataclass(frozen=True)
class StartCheck:
    """One evaluation of a condition at an instant."""

    due: bool
    status: str
    remaining_s: float | None = None
    altitude_deg: float | None = None


def _jd(when: datetime) -> float:
    return when.astimezone(timezone.utc).timestamp() / 86400.0 + 2440587.5


def target_altitude(target: StartTarget, when: datetime) -> float:
    return altitude_at(_jd(when), target.ra_hours, target.dec_deg, target.lat_deg, target.lon_deg)


def format_remaining(seconds: float) -> str:
    seconds = max(0, int(math.ceil(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h {minutes:02d}m {secs:02d}s" if hours else f"{minutes}m {secs:02d}s"


def problem(cond: StartCondition, now: datetime) -> str | None:
    """Why the condition cannot be armed (``None`` when it can).

    A start time already past is not a problem here — the caller asks the
    observer whether to start now (see :func:`is_past`).
    """
    if cond.goto and cond.target is None:
        return "GoTo at start needs a target — find the object first."
    if cond.mode == START_AT_TIME:
        if cond.start_at is None or cond.start_at.tzinfo is None:
            return "Set a start date and time."
        return None
    if cond.mode == START_ABOVE_ALTITUDE:
        target = cond.target
        if target is None:
            return "Altitude start needs a target — find the object first."
        if not target.has_site:
            return "Altitude start needs the observing site — set it in Settings."
        if crossing_time(cond, now) is None:
            peak = max(
                target_altitude(target, now + _ALTITUDE_STEP * i)
                for i in range(int(ALTITUDE_LOOKAHEAD / _ALTITUDE_STEP) + 1)
            )
            return (
                f"{target.name} does not reach {cond.min_altitude_deg:.0f}° in the next "
                f"{ALTITUDE_LOOKAHEAD.total_seconds() / 3600:.0f} h (highest {peak:.1f}°)."
            )
    return None


def is_past(cond: StartCondition, now: datetime) -> bool:
    return cond.mode == START_AT_TIME and cond.start_at is not None and cond.start_at <= now


def crossing_time(cond: StartCondition, now: datetime) -> datetime | None:
    """First sampled instant (from ``now``) the target is above the limit."""
    target = cond.target
    if target is None or not target.has_site:
        return None
    steps = int(ALTITUDE_LOOKAHEAD / _ALTITUDE_STEP)
    for i in range(steps + 1):
        when = now + _ALTITUDE_STEP * i
        if target_altitude(target, when) >= cond.min_altitude_deg:
            return when
    return None


def check(cond: StartCondition, now: datetime) -> StartCheck:
    """Evaluate the condition at ``now``."""
    if cond.mode == START_AT_TIME and cond.start_at is not None:
        remaining = (cond.start_at - now).total_seconds()
        local = cond.start_at.astimezone().strftime("%H:%M:%S")
        if remaining <= 0:
            return StartCheck(True, f"Start time {local} reached", 0.0)
        return StartCheck(False, f"Waiting for {local} — {format_remaining(remaining)}", remaining)
    if cond.mode == START_ABOVE_ALTITUDE and cond.target is not None and cond.target.has_site:
        alt = target_altitude(cond.target, now)
        if alt >= cond.min_altitude_deg:
            return StartCheck(
                True, f"{cond.target.name} at {alt:.1f}° ≥ {cond.min_altitude_deg:.0f}°", 0.0, alt
            )
        crossing = crossing_time(cond, now)
        eta = f", about {crossing.astimezone().strftime('%H:%M')}" if crossing else ""
        remaining = (crossing - now).total_seconds() if crossing else None
        return StartCheck(
            False,
            f"Waiting for {cond.target.name} to reach {cond.min_altitude_deg:.0f}° "
            f"(now {alt:.1f}°{eta})",
            remaining,
            alt,
        )
    return StartCheck(True, "Starting now", 0.0)
