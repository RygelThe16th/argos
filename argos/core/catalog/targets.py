"""Persistent target / comparison set for a session (notes/photometry_plan.md §5 B4).

Qt-free. The set is the night's selection: the variable target(s), the comparison
stars, and any check stars, each with RA/Dec + catalog id so they can be projected
onto every solved frame and (later) aperture-measured. Persisted as ``targets.json``
(atomic write, mirrors session_log) so it survives restarts.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

ROLE_TARGET = "target"
ROLE_COMPARISON = "comparison"
ROLE_CHECK = "check"
ROLES = (ROLE_TARGET, ROLE_COMPARISON, ROLE_CHECK)

SCHEMA = 1
SELECTION_MANIFEST_SCHEMA = 1
_SAME_SOURCE_ARCSEC = 1.0


@dataclass
class TargetStar:
    """One selected star, identified by AUID when known else by position."""

    role: str
    ra_deg: float
    dec_deg: float
    auid: str | None = None
    name: str | None = None
    source: str = "manual"  # vsx | vsp | manual
    mags: dict = field(default_factory=dict)  # band -> magnitude
    note: str = ""
    chart_id: str | None = None  # VSP chart that supplied ``mags`` (AAVSO CHART)

    def key(self) -> str:
        """Stable identity used to dedup/update (AUID, else rounded position)."""
        if self.auid:
            return f"auid:{self.auid}"
        return f"pos:{self.ra_deg:.5f},{self.dec_deg:.5f}"

    @property
    def display_name(self) -> str:
        # VSP's ``label`` (for example ``114``) encodes a chart magnitude
        # rather than an object name.  Showing it as the reference-star name
        # made both the image overlay and the scientific hand-off ambiguous;
        # use the traceable AUID instead.
        if (
            self.auid
            and self.source in {"vsp", "vsp_auto"}
            and self.name
            and self.name.strip().replace(".", "", 1).isdigit()
        ):
            return self.auid
        return self.name or self.auid or f"{self.ra_deg:.3f},{self.dec_deg:+.3f}"


@dataclass
class TargetSet:
    """The session's selected stars; load/save ``targets.json``."""

    object_name: str = ""
    stars: list[TargetStar] = field(default_factory=list)

    def set_role(self, star: TargetStar) -> None:
        """Add ``star`` or replace the existing one with the same identity."""
        k = star.key()
        for i, s in enumerate(self.stars):
            if s.key() == k:
                self.stars[i] = star
                return
        # A catalogue lookup can enrich a manually selected target with an
        # AUID, changing its key from position to AUID.  Keep that as one
        # unambiguous scientific target rather than silently duplicating it.
        if star.role == ROLE_TARGET:
            for i, current in enumerate(self.stars):
                if current.role != ROLE_TARGET:
                    continue
                if _separation_arcsec(current, star) <= _SAME_SOURCE_ARCSEC:
                    self.stars[i] = _prefer_identified_target(current, star)
                    return
        self.stars.append(star)

    def remove(self, key: str) -> None:
        self.stars = [s for s in self.stars if s.key() != key]

    def by_role(self, role: str) -> list[TargetStar]:
        return [s for s in self.stars if s.role == role]

    def summary(self) -> dict:
        """A display/readiness summary of the selection (Qt-free).

        Differential photometry needs at least one target (T1) and one
        comparison; a check star is recommended but optional. ``complete``
        captures that minimum so the UI can tell the user what is missing.
        """
        targets = self.by_role(ROLE_TARGET)
        comparisons = self.by_role(ROLE_COMPARISON)
        checks = self.by_role(ROLE_CHECK)
        return {
            "object": self.object_name,
            "target": targets[0].display_name if targets else None,
            "n_target": len(targets),
            "n_comparison": len(comparisons),
            "n_check": len(checks),
            "complete": bool(targets) and bool(comparisons),
        }

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA,
            "object": self.object_name,
            "stars": [asdict(s) for s in self.stars],
        }

    def selection_manifest(self, *, generated_by: str = "Argos") -> dict:
        """Return the explicit, auditable target/comparison hand-off.

        ``targets.json`` is mutable application state.  This separate manifest
        records the exact selection active during acquisition.  It is
        deliberately labelled as a live-preview selection: final calibrated
        validation belongs to registered frames in Siril / star_var_script.
        """

        def record(star: TargetStar, index: int) -> dict:
            return {
                "selection_index": index,
                "name": star.display_name,
                "auid": star.auid,
                "ra_deg_j2000": round(float(star.ra_deg), 8),
                "dec_deg_j2000": round(float(star.dec_deg), 8),
                "catalogue_source": star.source,
                "catalogue_magnitudes": dict(star.mags),
                "catalogue_chart_id": star.chart_id,
                "note": star.note,
            }

        roles = (
            ("targets", ROLE_TARGET),
            ("comparison_stars", ROLE_COMPARISON),
            ("check_stars", ROLE_CHECK),
        )
        return {
            "schema": SELECTION_MANIFEST_SCHEMA,
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "generated_by": generated_by,
            "object_name": self.object_name,
            "selection_status": "provisional_live_preview",
            "scientific_note": (
                "Argos acquisition selection. This is a traceable live-preview "
                "ensemble, not a calibrated result. Revalidate comparison and "
                "check stars on registered, calibrated frames in Siril / star_var_script."
            ),
            **{
                key: [record(star, i + 1) for i, star in enumerate(self.by_role(role))]
                for key, role in roles
            },
        }

    def save_selection_manifest(self, path, *, generated_by: str = "Argos") -> None:
        """Atomically write the session photometry selection hand-off."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.selection_manifest(generated_by=generated_by), indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def append_selection_history(self, path, *, generated_by: str = "Argos") -> None:
        """Append one immutable selection snapshot to a session JSONL history."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as history:
            history.write(json.dumps(self.selection_manifest(generated_by=generated_by)) + "\n")

    @classmethod
    def from_dict(cls, d: dict) -> "TargetSet":
        valid = {f.name for f in fields(TargetStar)}
        stars = [
            TargetStar(**{k: v for k, v in s.items() if k in valid}) for s in d.get("stars", [])
        ]
        result = cls(object_name=str(d.get("object", "")))
        # Repair legacy manual + catalogue-enriched duplicates while loading.
        for star in stars:
            result.set_role(star)
        return result

    def save(self, path) -> None:
        """Atomically write ``targets.json`` (temp + os.replace)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, path)

    @classmethod
    def load(cls, path) -> "TargetSet":
        """Load ``targets.json``; return an empty set if missing/unreadable."""
        path = Path(path)
        if not path.exists():
            return cls()
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:  # corrupt file → start fresh, don't crash
            logger.warning("Could not read %s: %s", path, exc)
            return cls()


def comparison_chart_id(stars) -> str | None:
    """The VSP chart every comparison star comes from, else None.

    It is what an AAVSO report puts in CHART. A manual comparison, or two
    charts mixed in one ensemble, leave no single sequence to name.
    """
    charts = {s.chart_id for s in stars if s.role == ROLE_COMPARISON}
    if len(charts) != 1:
        return None
    return charts.pop()


def _separation_arcsec(a: TargetStar, b: TargetStar) -> float:
    """Small-angle separation, sufficient for a one-arcsecond identity merge."""
    import math

    dra = math.radians(a.ra_deg - b.ra_deg) * math.cos(math.radians((a.dec_deg + b.dec_deg) / 2))
    ddec = math.radians(a.dec_deg - b.dec_deg)
    return math.degrees(math.hypot(dra, ddec)) * 3600.0


def _prefer_identified_target(current: TargetStar, incoming: TargetStar) -> TargetStar:
    """Retain catalogue identity when merging two representations of a target."""
    if incoming.auid or not current.auid:
        preferred, fallback = incoming, current
    else:
        preferred, fallback = current, incoming
    return replace(
        preferred,
        auid=preferred.auid or fallback.auid,
        name=preferred.name or fallback.name,
        mags=preferred.mags or fallback.mags,
        note=preferred.note or fallback.note,
    )
