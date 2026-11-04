"""AcquisitionEngine unit tests — no hardware, no event loop.

A ``QApplication`` is required because the engine is a ``QObject``; signals
are observed through direct connections into plain lists.
"""

from __future__ import annotations

import numpy as np
import pytest
from PyQt6.QtWidgets import QApplication

from argos.core.config import _DEFAULTS, Config
from argos.core.session.acquisition_engine import AcquisitionEngine
from argos.core.session.device_session import DeviceSession


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_ingest_frame_feeds_the_live_pipeline(qapp, tmp_path) -> None:
    """Offline replay: an opened FITS must land exactly where a camera frame
    would — authoritative full-res raw + a frame_ready for the display."""
    cfg = Config(dict(_DEFAULTS, sessions_path=str(tmp_path)))
    engine = AcquisitionEngine(cfg, DeviceSession(cfg))
    frames = []
    engine.frame_ready.connect(frames.append)

    raw = np.arange(64, dtype=np.uint16).reshape(8, 8)
    engine.ingest_frame(raw)

    assert len(frames) == 1
    assert frames[0].full is raw and frames[0].preview is raw
    assert frames[0].decimated is False
    # Solve/photometry read the same authoritative frame.
    assert engine._last_raw is raw and engine._last_raw_decimated is False


class _TangentWCS:
    """Small-field tangent-plane approximation (≈8″/green px, target centred)."""

    def __init__(self, ra0: float, dec0: float, scale_arcsec: float = 8.0) -> None:
        self.ra0, self.dec0, self.scale = ra0, dec0, scale_arcsec

    def world_to_pixel_deg(self, ra_deg: float, dec_deg: float):
        import math

        dx = (ra_deg - self.ra0) * math.cos(math.radians(self.dec0)) * 3600.0 / self.scale
        dy = (dec_deg - self.dec0) * 3600.0 / self.scale
        return 960.0 + dx, 540.0 + dy


def _gz_and_engine(tmp_path):
    from argos.core.catalog.aavso import Band, ComparisonStar

    cfg = Config(dict(_DEFAULTS, sessions_path=str(tmp_path)))
    engine = AcquisitionEngine(cfg, DeviceSession(cfg))
    engine._astrometry._wcs = _TangentWCS(33.05875, 44.65944)
    engine._green_shape = (1080, 1920)
    engine._comparisons = [
        ComparisonStar(
            "000-BPM-472", 33.057416666666676, 44.656416666666665, "118", (Band("V", 11.848),)
        ),
        ComparisonStar(
            "000-BPM-471", 33.14383333333334, 44.77311111111111, "113", (Band("V", 11.301),)
        ),
    ]
    logs: list[tuple[str, str]] = []
    engine.log_message.connect(lambda level, text: logs.append((level, text)))
    return engine, logs


def test_auto_selection_excludes_a_star_blended_with_the_target(qapp, tmp_path) -> None:
    """GZ And acceptance: 000-BPM-472 (11″ from the target) is never proposed."""
    from argos.core.catalog.targets import ROLE_COMPARISON, ROLE_TARGET, TargetStar

    engine, logs = _gz_and_engine(tmp_path)
    engine.set_target_role(
        TargetStar(
            role=ROLE_TARGET, ra_deg=33.05875, dec_deg=44.65944, auid="000-BDC-868", name="GZ And"
        )
    )
    comps = [s.auid for s in engine.target_set().by_role(ROLE_COMPARISON)]
    assert comps == ["000-BPM-471"]
    excluded = [text for _level, text in logs if "comparison excluded" in text]
    assert len(excluded) == 1 and "000-BPM-472" in excluded[0]
    assert "from target" in excluded[0]


def test_manual_references_breaking_the_rules_are_kept_with_a_warning(qapp, tmp_path) -> None:
    from argos.core.catalog.targets import ROLE_CHECK, ROLE_COMPARISON, ROLE_TARGET, TargetStar

    engine, logs = _gz_and_engine(tmp_path)
    engine._comparisons = []  # no automatic proposal: only manual choices
    engine.set_target_role(
        TargetStar(role=ROLE_TARGET, ra_deg=33.05875, dec_deg=44.65944, name="GZ And")
    )
    blended = TargetStar(
        role=ROLE_COMPARISON,
        ra_deg=33.057416666666676,
        dec_deg=44.656416666666665,
        auid="000-BPM-472",
        source="vsp",
        mags={"V": 11.848},
    )
    # ≈ 4.3 arcmin above the target at 8″/px → y ≈ 1040, 40 px from the edge.
    near_edge = TargetStar(role=ROLE_CHECK, ra_deg=33.05875, dec_deg=45.7706, name="Edge check")
    variable = TargetStar(
        role=ROLE_COMPARISON, ra_deg=33.10, dec_deg=44.70, name="NSV 896", source="vsx"
    )
    for star in (blended, near_edge, variable):
        engine.set_target_role(star)

    roles = {s.display_name: s.role for s in engine.target_set().stars}
    assert roles["000-BPM-472"] == ROLE_COMPARISON  # the observer's choice is kept
    warnings = [text for level, text in logs if level == "WARN" and "Photometry:" in text]
    assert any("000-BPM-472" in w and "from the target" in w for w in warnings)
    assert any("Edge check" in w and "frame edge" in w for w in warnings)
    assert any("NSV 896" in w and "variable" in w for w in warnings)

    # Re-assigning the target re-checks the existing manual references.
    logs.clear()
    engine.set_target_role(
        TargetStar(role=ROLE_TARGET, ra_deg=33.05875, dec_deg=44.65944, name="GZ And")
    )
    assert any("000-BPM-472" in text for level, text in logs if level == "WARN")
