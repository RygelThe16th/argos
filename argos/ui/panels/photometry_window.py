"""Floating Photometry window (notes/photometry_plan.md §6 C5/C6).

Hosts the live differential light curve + the session-metrics panel in tabs. A
separate top-level window (like the analysis window) so it can sit on a second
monitor during a run. Display only — the page feeds it points; this window owns no
acquisition state.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QSignalBlocker, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from argos.core.catalog.targets import comparison_chart_id
from argos.core.photometry.lightcurve import write_aavso, write_curves_csv
from argos.ui import theme
from argos.ui.widgets.comparison_table import ComparisonEnsembleTable
from argos.ui.widgets.lightcurve_panel import LightCurvePanel
from argos.ui.widgets.metrics_panel import MetricsPanel
from argos.ui.widgets.target_table import TargetTable
from argos.ui.widgets.variable_table import VariableTable


class PhotometrySetupPanel(QWidget):
    """Visible controls for the live aperture-photometry geometry.

    The values describe the exact FWHM-adaptive aperture used by the engine,
    rather than the unrelated click/FWHM inspection circle in the display dock.
    """

    setting_changed = pyqtSignal(str, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        intro = QLabel(
            "Live preview only: Argos measures circular apertures on the raw subs and "
            "estimates the local sky in a surrounding annulus. The aperture follows the "
            "measured FWHM; final calibrated reduction remains in Siril."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{theme.FG_MUTED}; font-size:11px; padding:2px 0 8px;")
        root.addWidget(intro)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)

        self._fwhm_mult = QDoubleSpinBox()
        self._fwhm_mult.setRange(1.0, 6.0)
        self._fwhm_mult.setDecimals(2)
        self._fwhm_mult.setSingleStep(0.25)
        self._fwhm_mult.setSuffix(" × FWHM")
        self._fwhm_mult.setToolTip("Photometric aperture radius; 2–3 × FWHM is a typical start.")
        self._fwhm_mult.valueChanged.connect(
            lambda value: self.setting_changed.emit("photometry.aperture_fwhm_mult", float(value))
        )
        form.addRow("Aperture radius (adaptive)", self._fwhm_mult)

        self._minimum = self._radius_spin(2.0, 30.0)
        self._minimum.setToolTip("Lower limit for the aperture radius in green-plane pixels.")
        self._minimum.valueChanged.connect(
            lambda value: self.setting_changed.emit("photometry.aperture_min_px", float(value))
        )
        form.addRow("Aperture radius floor", self._minimum)

        self._annulus_in = self._radius_spin(3.0, 60.0)
        self._annulus_in.setToolTip("Inner radius of the local-sky annulus.")
        self._annulus_in.valueChanged.connect(
            lambda value: self.setting_changed.emit("photometry.annulus_in_px", float(value))
        )
        form.addRow("Background annulus — inner radius", self._annulus_in)

        self._annulus_out = self._radius_spin(5.0, 80.0)
        self._annulus_out.setToolTip("Outer radius of the local-sky annulus.")
        self._annulus_out.valueChanged.connect(
            lambda value: self.setting_changed.emit("photometry.annulus_out_px", float(value))
        )
        form.addRow("Background annulus — outer radius", self._annulus_out)

        self._comparison_snr = QDoubleSpinBox()
        self._comparison_snr.setRange(3.0, 100.0)
        self._comparison_snr.setDecimals(1)
        self._comparison_snr.setSingleStep(1.0)
        self._comparison_snr.setSuffix(" SNR")
        self._comparison_snr.setToolTip(
            "Automatic comparison candidates below this pilot-frame SNR are rejected."
        )
        self._comparison_snr.valueChanged.connect(
            lambda value: self.setting_changed.emit("photometry.comparison_min_snr", float(value))
        )
        form.addRow("Reference-star minimum S/N", self._comparison_snr)

        self._comparison_delta = QDoubleSpinBox()
        self._comparison_delta.setRange(0.2, 5.0)
        self._comparison_delta.setDecimals(1)
        self._comparison_delta.setSingleStep(0.1)
        self._comparison_delta.setSuffix(" mag")
        self._comparison_delta.setToolTip(
            "Maximum instrumental brightness difference from the target for automatic comparisons."
        )
        self._comparison_delta.valueChanged.connect(
            lambda value: self.setting_changed.emit(
                "photometry.comparison_max_delta_mag", float(value)
            )
        )
        form.addRow("Reference-star brightness difference", self._comparison_delta)

        self._comparison_distance = QDoubleSpinBox()
        self._comparison_distance.setRange(5.0, 120.0)
        self._comparison_distance.setDecimals(0)
        self._comparison_distance.setSingleStep(5.0)
        self._comparison_distance.setSuffix(" ′")
        self._comparison_distance.setToolTip(
            "Maximum target-to-comparison separation. Nearby references reduce "
            "flat-field and field-rotation systematics; Argos will propose fewer "
            "stars rather than fill the ensemble with remote candidates."
        )
        self._comparison_distance.valueChanged.connect(
            lambda value: self.setting_changed.emit(
                "photometry.comparison_max_separation_arcmin", float(value)
            )
        )
        form.addRow("Reference-star maximum separation", self._comparison_distance)
        root.addLayout(form)

        self._effective = QLabel("Current live geometry: waiting for a measured FWHM")
        self._effective.setWordWrap(True)
        self._effective.setStyleSheet(
            f"color:{theme.ACCENT}; font-size:11px; padding:10px 0 2px; font-weight:600;"
        )
        root.addWidget(self._effective)
        note = QLabel(
            "All radii are green-plane pixels. Argos constrains the background annulus "
            "outside the aperture; the selected-star ring uses the current aperture."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{theme.FG_MUTED}; font-size:11px;")
        root.addWidget(note)
        root.addStretch(1)

    @staticmethod
    def _radius_spin(lower: float, upper: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(lower, upper)
        spin.setDecimals(1)
        spin.setSingleStep(0.5)
        spin.setSuffix(" green px")
        return spin

    def set_values(self, cfg, geometry: tuple[float, float, float] | None = None) -> None:
        """Synchronise controls from the configuration without writing it back."""
        values = (
            (self._fwhm_mult, float(cfg("photometry.aperture_fwhm_mult", 2.5))),
            (self._minimum, float(cfg("photometry.aperture_min_px", 4.0))),
            (self._annulus_in, float(cfg("photometry.annulus_in_px", 8.0))),
            (self._annulus_out, float(cfg("photometry.annulus_out_px", 12.0))),
            (self._comparison_snr, float(cfg("photometry.comparison_min_snr", 10.0))),
            (
                self._comparison_delta,
                float(cfg("photometry.comparison_max_delta_mag", 1.5)),
            ),
            (
                self._comparison_distance,
                float(cfg("photometry.comparison_max_separation_arcmin", 25.0)),
            ),
        )
        for widget, value in values:
            blocker = QSignalBlocker(widget)
            widget.setValue(value)
            del blocker
        self.set_geometry(geometry)

    def set_geometry(self, geometry: tuple[float, float, float] | None) -> None:
        if geometry is None:
            self._effective.setText("Current live geometry: waiting for a measured FWHM")
            return
        aperture, inner, outer = geometry
        self._effective.setText(
            f"Current live geometry: aperture {aperture:.1f} px · "
            f"background annulus {inner:.1f}–{outer:.1f} px"
        )


class PhotometryWindow(QWidget):
    """Field-star selection, light curves and diagnostics in one window."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setWindowTitle("Field photometry")
        self.setMinimumSize(780, 620)
        self.resize(960, 700)

        root = QVBoxLayout(self)
        banner = QLabel(
            "Preview — raw, uncalibrated subs. BJD_TDB is recorded when the site and "
            "exposure time are available. Relative flux is a live diagnostic, not a detrended "
            "transit result; publishable reduction remains in Siril."
        )
        banner.setWordWrap(True)
        banner.setStyleSheet(
            f"color:{theme.WARNING}; font-size:11px; background:transparent; padding:4px 2px;"
        )
        root.addWidget(banner)

        self.variables = VariableTable()
        self.lightcurve = LightCurvePanel()
        self.metrics = MetricsPanel()
        self.targets = TargetTable()
        self.comparisons = ComparisonEnsembleTable()
        self.setup = PhotometrySetupPanel()
        self._tabs = QTabWidget()
        # Variables first: picking the target there is the workflow's entry point.
        self._tabs.addTab(self.variables, "Variable stars")
        self._tabs.addTab(self.lightcurve, "Light curve")
        self._tabs.addTab(self.metrics, "Metrics")
        self._tabs.addTab(self.targets, "Targets")
        self._tabs.addTab(self.comparisons, "Comparison stars")
        self._tabs.addTab(self.setup, "Aperture · references")
        root.addWidget(self._tabs, 1)

        footer = QHBoxLayout()
        footer.addStretch()
        self._csv_btn = QPushButton("Export measurements…")
        self._csv_btn.setToolTip("Export target, check-star and comparison measurements")
        self._csv_btn.clicked.connect(self._export_csv)
        footer.addWidget(self._csv_btn)
        self._aavso_btn = QPushButton("Export target (AAVSO)…")
        self._aavso_btn.setToolTip("Export science targets only; comparison curves are diagnostics")
        self._aavso_btn.clicked.connect(self._export_aavso)
        footer.addWidget(self._aavso_btn)
        root.addLayout(footer)

        # Set by the page: the per-target LightCurve objects + the observer code.
        self.lightcurves: dict = {}
        self.obscode = "XXX"
        self.filt = "TG"
        # The selection behind the curves; its comparisons name the AAVSO CHART.
        self._target_stars: list = []

    # ------------------------------------------------------------------
    # Real API (WS7): feed_point / set_export_meta / load_curves
    # ------------------------------------------------------------------

    def set_export_meta(self, obscode: str, filt: str, object_name: str = "") -> None:
        """Stamp the AAVSO/CSV export metadata (observer code + band)."""
        self.obscode = obscode or "XXX"
        self.filt = filt or "TG"
        if object_name:
            self.setWindowTitle(f"Field photometry — {object_name}")

    def show_variable_stars(self) -> None:
        """Make the target-selection workflow the visible entry point."""
        self._tabs.setCurrentWidget(self.variables)

    def feed_point(self, point) -> None:
        """Render one differential point (a typed ``PhotometryPoint``)."""
        self.lightcurve.add_point(
            point.name,
            point.jd,
            point.mag,
            point.mag_err,
            saturated=point.saturated,
            role=point.role,
            relative_flux=point.relative_flux,
            relative_flux_err=point.relative_flux_err,
        )

    def set_targets(self, stars) -> None:
        """Refresh the Targets + Comparisons tabs from the target set."""
        self._target_stars = list(stars)
        self.targets.set_targets(stars)
        self.comparisons.set_targets(stars)

    def load_curves(self, curves: dict, obscode: str = "XXX", filt: str = "TG") -> None:
        """Display finished curves (e.g. reloaded from a session CSV by Analyze).

        ``curves`` maps a key to a :class:`LightCurve`; its points are plotted
        and kept for export. Replaces any currently shown curves.
        """
        self.lightcurves = dict(curves)
        self.obscode = obscode or "XXX"
        self.filt = filt or "TG"
        self.lightcurve.set_curves(self.lightcurves)  # same path as the dock

    def _export_csv(self) -> None:
        curves = [lc for lc in self.lightcurves.values() if lc.points]
        if not curves:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export light curve", str(Path.home() / "photometry.csv"), "CSV (*.csv)"
        )
        if path:
            write_curves_csv(path, curves)  # canonical 9-column schema (+ target)

    def _export_aavso(self) -> None:
        curves = [lc for lc in self.lightcurves.values() if lc.points]
        if not any(lc.role == "target" for lc in curves):
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export AAVSO", str(Path.home() / "aavso.txt"), "Text (*.txt)"
        )
        if path:
            # Check curves only fill KNAME/KMAG; write_aavso never makes them rows.
            write_aavso(
                path,
                curves,
                obscode=self.obscode or "XXX",
                filt=self.filt or "TG",
                chart=comparison_chart_id(self._target_stars),
            )
