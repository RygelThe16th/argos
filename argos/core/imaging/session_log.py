"""Session log — per-frame QA records persisted to ``session.json`` (§7).

A session is one acquisition run for a target. As each sub is written, its
quality metrics (HFD, FWHM, star count, sky level, peak ADU, eccentricity) are
appended here and the JSON is rewritten atomically, so a crash mid-sequence
still leaves a valid, up-to-date log next to the FITS frames.

Qt-free and I/O-light — unit-tested in isolation. The companion FITS headers
(HFD/NSTARS/SKYLEVEL/FWHM) are written by ``FITSWriter``; this file is the
session-level roll-up used to cull subs and review a night later.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: Schema version, so a later reader can migrate older logs.
SESSION_SCHEMA = 1
#: Conventional file name written inside the session folder.
SESSION_FILENAME = "session.json"


@dataclass
class FrameRecord:
    """Quality summary of one written sub (everything is JSON-safe)."""

    filename: str
    image_type: str  # FITS IMAGETYP, e.g. "Light Frame"
    filter_name: str
    exposure_s: float
    gain: int
    timestamp: str  # ISO-8601 UTC of exposure start
    hfd: float | None = None
    fwhm: float | None = None
    star_count: int | None = None
    sky_adu: float | None = None
    peak_adu: int | None = None
    eccentricity: float | None = None
    #: Set on the first frame after a between-frames re-centering move
    #: ("calibrate" | "recenter"), so the shifted frame can be found later.
    mount_move: str | None = None


@dataclass
class SessionLog:
    """Accumulates :class:`FrameRecord` rows and serialises ``session.json``.

    Attributes:
        object_name: Target name.
        software:    Acquisition software string.
        started_utc: ISO-8601 UTC of session start.
        observer:    Observer name (optional).
        frames:      Per-frame records appended in shooting order.
    """

    object_name: str = ""
    software: str = "Argos"
    started_utc: str = ""
    observer: str = ""
    frames: list[FrameRecord] = field(default_factory=list)

    def add(self, record: FrameRecord) -> None:
        """Append a frame record."""
        self.frames.append(record)

    def summary(self) -> dict:
        """Aggregate stats across light frames (means ignore missing values)."""
        lights = [f for f in self.frames if f.image_type.lower().startswith("light")]

        def _mean(attr: str) -> float | None:
            vals = [getattr(f, attr) for f in lights if getattr(f, attr) is not None]
            return round(sum(vals) / len(vals), 3) if vals else None

        return {
            "frame_count": len(self.frames),
            "light_count": len(lights),
            "mean_hfd": _mean("hfd"),
            "mean_fwhm": _mean("fwhm"),
            "mean_sky_adu": _mean("sky_adu"),
            "mean_star_count": _mean("star_count"),
        }

    def to_dict(self) -> dict:
        """Return the full JSON-safe document."""
        return {
            "schema": SESSION_SCHEMA,
            "object": self.object_name,
            "software": self.software,
            "started_utc": self.started_utc,
            "observer": self.observer,
            "summary": self.summary(),
            "frames": [asdict(f) for f in self.frames],
        }

    def write(self, path: Path) -> None:
        """Atomically write ``session.json`` to ``path``.

        Writes to a temp file in the same directory then ``os.replace`` — so a
        reader never sees a half-written file even if we crash mid-write.

        Args:
            path: Destination ``session.json`` path (parent dirs are created).
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, path)
        logger.debug("session.json updated: %s (%d frame(s))", path, len(self.frames))
