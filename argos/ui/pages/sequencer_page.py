"""Sequencer mode — plan and run a capture sequence.

The SequencePanel is a dockable workspace: the table stays central, while
target search, visibility and controls are movable.  This page owns panel↔
engine wiring; frames, the log and live light curve stay on Capture.
"""

from __future__ import annotations

from datetime import datetime, timezone

from PyQt6.QtCore import pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from argos.ui import design
from argos.ui.widgets.sequence_panel import SequencePanel
from argos.core.catalog.object_resolver import ResolvedObject
from argos.core.exoplanet.transit import make_transit_sequence, predict_next_transit
from argos.core.photometry.airmass import bjd_tdb, julian_date, utc_from_bjd_tdb
from argos.workers.exoplanet_worker import ExoplanetLookupWorker
from argos.workers.object_resolver_worker import ObjectResolverWorker


class SequencerPage(QWidget):
    """Full-page host for the sequence planner, wired to the engine."""

    target_resolved = pyqtSignal(object)
    science_source_resolved = pyqtSignal(str, object)  # programme, host/target coordinates

    def __init__(self, config, session, engine, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._session = session
        self._engine = engine
        self._object_resolver_worker: ObjectResolverWorker | None = None
        self._exoplanet_worker: ExoplanetLookupWorker | None = None
        self._exoplanet_target = None
        self._transit_window = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            design.SPACING_XL, design.SPACING_LG, design.SPACING_XL, design.SPACING_LG
        )
        outer.setSpacing(design.SPACING_MD)
        # The sidebar calls this screen "Plan"; the heading must agree, or the
        # observer cannot tell whether they are in the right place.
        outer.addWidget(design.HeadingLabel("Plan"))
        self.panel = SequencePanel()
        self.panel.set_config(config)
        outer.addWidget(self.panel, 1)

        # Intents → engine.
        self.panel.start_requested.connect(self._on_start)
        self.panel.pause_requested.connect(engine.pause_sequence)
        self.panel.resume_requested.connect(engine.resume_sequence_run)
        self.panel.stop_requested.connect(engine.stop_sequence)
        self.panel.object_lookup_requested.connect(self._lookup_object)
        self.panel.exoplanet_lookup_requested.connect(self._lookup_exoplanet)
        self.panel.transit_plan_requested.connect(self._prepare_transit_plan)
        # Engine → run feedback.
        engine.sequence_running.connect(self.panel.set_running)
        engine.sequence_paused.connect(self.panel.set_paused)
        engine.sequence_progress.connect(self._on_progress)
        engine.sequence_step.connect(self._on_step)
        # Device-derived vocabulary: spinbox limits from the camera, filter
        # names from the wheel — same sources the Capture forms use.
        session.capabilities_ready.connect(self._on_capabilities)
        session.filterwheel_state.connect(self._on_filterwheel_state)
        session.device_state_changed.connect(self._on_device_state)

    # ------------------------------------------------------------------

    def _on_start(self, plan) -> None:
        if not self._engine.start_sequence(plan):
            # Refused (no camera / already running / AF owns the camera) —
            # the reason is logged; snap the panel button back.
            self.panel.set_running(False)

    def set_target_coordinates(self, name: str, ra_hours: float, dec_degrees: float) -> None:
        """Receive the shared Telescope target for the Plan visibility preview."""
        self.panel.set_target_coordinates(
            name,
            ra_hours,
            dec_degrees,
            float(self._config.get("site.latitude", 0.0) or 0.0),
            float(self._config.get("site.longitude", 0.0) or 0.0),
        )

    def _lookup_object(self, query: str) -> None:
        if self._object_resolver_worker is not None:
            return
        self.panel.set_lookup_busy(True)
        worker = ObjectResolverWorker(query, self)
        self._object_resolver_worker = worker
        worker.resolved.connect(self._on_object_resolved)
        worker.failed.connect(self._on_object_lookup_failed)
        worker.finished.connect(self._finish_object_lookup)
        worker.start()

    @pyqtSlot(object)
    def _on_object_resolved(self, result) -> None:
        self.panel.set_lookup_result(result)
        self.set_target_coordinates(result.name, result.ra_hours, result.dec_degrees)
        # Shell routes this to ImagingPage, which makes the same target visible
        # in Capture and arms (but does not execute) the Telescope slew button.
        self.target_resolved.emit(result)
        self.science_source_resolved.emit("variable", result)

    @pyqtSlot(str)
    def _on_object_lookup_failed(self, message: str) -> None:
        self.panel.set_lookup_error(message)

    def _finish_object_lookup(self) -> None:
        self.panel.set_lookup_busy(False)
        worker = self._object_resolver_worker
        self._object_resolver_worker = None
        if worker is not None:
            worker.deleteLater()

    def _lookup_exoplanet(self, query: str) -> None:
        """Look up an exoplanet and select its *host star* for pointing."""
        if self._exoplanet_worker is not None:
            return
        self.panel.set_exoplanet_lookup_busy(True)
        worker = ExoplanetLookupWorker(query, self)
        self._exoplanet_worker = worker
        worker.resolved.connect(self._on_exoplanet_resolved)
        worker.failed.connect(self._on_exoplanet_lookup_failed)
        worker.finished.connect(self._finish_exoplanet_lookup)
        worker.start()

    def _current_bjd_tdb(self, target) -> float | None:
        """BJD_TDB now at the configured site, for a named host star."""
        site = self._site_coordinates()
        if site is None:
            return None
        lat, lon, elev = site
        return bjd_tdb(
            julian_date(datetime.now(timezone.utc)),
            target.ra_degrees,
            target.dec_degrees,
            lat,
            lon,
            elev,
        )

    def _site_coordinates(self) -> tuple[float, float, float] | None:
        """Return the explicitly saved observing site, never the 0/0 default."""
        if not str(self._config.get("site.name", "") or "").strip():
            return None
        try:
            return (
                float(self._config.get("site.latitude")),
                float(self._config.get("site.longitude")),
                float(self._config.get("site.elevation", 0.0) or 0.0),
            )
        except (TypeError, ValueError):
            return None

    def _local_transit_window(self, target, window):
        """Return topocentric local coverage instants, or ``None`` if unavailable."""
        site = self._site_coordinates()
        if site is None or window is None:
            return None
        lat, lon, elev = site
        start = utc_from_bjd_tdb(
            window.coverage_start_bjd_tdb,
            target.ra_degrees,
            target.dec_degrees,
            lat,
            lon,
            elev,
        )
        mid = utc_from_bjd_tdb(
            window.mid_bjd_tdb, target.ra_degrees, target.dec_degrees, lat, lon, elev
        )
        end = utc_from_bjd_tdb(
            window.coverage_end_bjd_tdb,
            target.ra_degrees,
            target.dec_degrees,
            lat,
            lon,
            elev,
        )
        if start is None or mid is None or end is None:
            return None
        return start.astimezone(), mid.astimezone(), end.astimezone()

    def _show_transit_visibility(self, target, window, local_window) -> None:
        if local_window is None:
            self.set_target_coordinates(target.host_name, target.ra_hours, target.dec_degrees)
            return
        start, _mid, end = local_window
        site = self._site_coordinates()
        if site is None:  # pragma: no cover - guarded by _local_transit_window
            return
        self.panel.set_transit_visibility(
            target.host_name,
            target.ra_hours,
            target.dec_degrees,
            site[0],
            site[1],
            start,
            end,
        )

    @pyqtSlot(object)
    def _on_exoplanet_resolved(self, target) -> None:
        self._exoplanet_target = target
        now_bjd = self._current_bjd_tdb(target)
        try:
            self._transit_window = (
                predict_next_transit(target, now_bjd) if now_bjd is not None else None
            )
        except ValueError:
            self._transit_window = None
        local_window = self._local_transit_window(target, self._transit_window)
        self.panel.set_exoplanet_result(
            target, self._transit_window, local_mid=local_window[1] if local_window else None
        )

        # The telescope slews to the star, never the planet.  This retains the
        # existing explicit/manual GoTo workflow in the Telescope panel.
        host = ResolvedObject(
            name=target.host_name,
            ra_degrees=target.ra_degrees,
            dec_degrees=target.dec_degrees,
            object_type="Exoplanet host",
            source=target.source,
        )
        self._show_transit_visibility(target, self._transit_window, local_window)
        self.target_resolved.emit(host)
        self.science_source_resolved.emit("exoplanet", host)

    @pyqtSlot(str)
    def _on_exoplanet_lookup_failed(self, message: str) -> None:
        self._exoplanet_target = None
        self._transit_window = None
        self.panel.set_exoplanet_error(message)

    def _finish_exoplanet_lookup(self) -> None:
        self.panel.set_exoplanet_lookup_busy(False)
        worker = self._exoplanet_worker
        self._exoplanet_worker = None
        if worker is not None:
            worker.deleteLater()

    def _prepare_transit_plan(self) -> None:
        """Replace the table with a deliberately stable transit time series."""
        target = self._exoplanet_target
        if target is None:
            self.panel.set_exoplanet_error("Find a planet before preparing a transit sequence.")
            return
        settings = self.panel.transit_settings()
        now_bjd = self._current_bjd_tdb(target)
        if now_bjd is None:
            self.panel.set_exoplanet_error(
                "Set and save the observing site in Settings before preparing transit coverage."
            )
            return
        try:
            window = predict_next_transit(
                target, now_bjd, baseline_minutes=float(settings["baseline_minutes"])
            )
            plan = make_transit_sequence(
                target,
                window,
                exposure_s=float(settings["exposure_s"]),
                cadence_s=float(settings["cadence_s"]),
                gain=int(settings["gain"]),
                filter_name=str(settings["filter_name"]),
            )
        except ValueError as exc:
            self.panel.set_exoplanet_error(str(exc))
            return
        self._transit_window = window
        local_window = self._local_transit_window(target, window)
        self.panel.set_exoplanet_result(
            target, window, local_mid=local_window[1] if local_window else None
        )
        self._show_transit_visibility(target, window, local_window)
        self.panel.load_plan(plan)
        self.panel.set_status(
            f"Transit sequence prepared: {plan.steps[0].count} light frames at "
            f"{settings['cadence_s']:.1f}s cadence. Review the start time before running."
        )

    def save_layout(self) -> None:
        self.panel.save_layout()

    def shutdown(self) -> None:
        if self._object_resolver_worker is not None:
            self._object_resolver_worker.wait(9000)  # resolver request timeout is 8 seconds
            self._object_resolver_worker = None
        if self._exoplanet_worker is not None:
            self._exoplanet_worker.wait(13000)  # NASA lookup timeout is 12 seconds
            self._exoplanet_worker = None

    @pyqtSlot(str, int, int, float)
    def _on_progress(self, _obj: str, done: int, total: int, eta_s: float) -> None:
        self.panel.set_progress(done, total, eta_s)

    def _on_step(self, index: int, step) -> None:
        self.panel.set_active_step(index)
        self.panel.set_status(
            f"Step {index + 1}: {step.count}× {step.exposure_s:.1f}s {step.filter_name}"
        )

    @pyqtSlot(object)
    def _on_capabilities(self, caps) -> None:
        self.panel.set_camera_limits(
            caps.gain_min, caps.gain_max, caps.exposure_min, caps.exposure_max
        )

    @pyqtSlot(object)
    def _on_filterwheel_state(self, fw) -> None:
        self.panel.set_filter_options(list(fw.names))

    @pyqtSlot(str, str, str)
    def _on_device_state(self, device: str, state: str, _info: str) -> None:
        self.panel.set_device_state(device, state)
        if device == "camera" and state == "disconnected":
            self.panel.set_camera_limits()  # back to defaults
