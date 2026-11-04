"""Sequence panel — dockable multi-step acquisition planning workspace.

The sequence table is the stable centre.  Target search, plan options,
visibility, presets and controls are independent docks which can be moved,
tabbed, resized, or floated.  UI-only: builds a :class:`SequencePlan` from the
editable table and emits ``start/pause/resume/stop`` intents; the
``AcquisitionEngine`` drives the ``SequenceWorker`` and feeds progress back.

Presets: a plan (steps + options) saves/loads as JSON via the sequencer's
``plan_to_dict`` / ``plan_from_dict`` round-trip.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

import pyqtgraph as pg
from PyQt6.QtCore import QByteArray, QDateTime, QSize, Qt, QTimer, QStringListModel, pyqtSignal
from PyQt6.QtGui import QAction, QColor
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QDateTimeEdit,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QProgressBar,
    QStackedWidget,
    QSpinBox,
    QSizePolicy,
    QTableWidget,
    QMainWindow,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from argos.core.imaging.sequencer import (
    ON_COMPLETE_CHOICES,
    SequencePlan,
    SequenceStep,
    estimated_duration_s,
    plan_from_dict,
    plan_to_dict,
)
from argos.core.imaging.sky_geometry import upcoming_night_altitudes
from argos.core.imaging.start_condition import (
    START_ABOVE_ALTITUDE,
    START_AT_TIME,
    START_NOW,
    StartCondition,
    StartTarget,
)
from argos.core.catalog.exoplanets import cached_exoplanet_suggestions
from argos.core.catalog.object_resolver import cached_object_suggestions
from argos.ui import design, theme
from argos.core.catalog.object_resolver import object_type_label
from argos.ui.widgets.dock_host import make_dock, panel_toolbar_qss, style_toggle_action

logger = logging.getLogger(__name__)

_FRAME_TYPES = ("Light", "Dark", "Flat", "Bias")
# Seestar wheel slot names (see alpaca.filterwheel.position_names) — "IR-cut"
# never matched a wheel position, so those steps silently kept the old filter.
_DEFAULT_FILTERS = ("IR", "LP", "Dark")
_COLUMNS = ("✓", "Type", "Filter", "Exp (s)", "Gain", "Count", "Interval (s)", "Dither every")

#: Fallback limits when no camera is connected (the historical hardcodes).
_DEFAULT_GAIN_RANGE = (0, 600)
_DEFAULT_EXPOSURE_RANGE = (0.01, 600.0)
_CFG_LAYOUT = "ui.sequencer.layout"


class _CurrentPageStack(QStackedWidget):
    """Report only the visible page's size instead of the largest page.

    The variable-star search is intentionally much shorter than the transit
    form.  QStackedWidget normally reserves the transit form's height in both
    modes, leaving a large blank panel below the variable-star search.
    """

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        current = self.currentWidget()
        return current.sizeHint() if current is not None else super().sizeHint()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        current = self.currentWidget()
        return current.minimumSizeHint() if current is not None else super().minimumSizeHint()


def _format_duration(seconds: float) -> str:
    m, s = divmod(max(0, int(seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


class TargetVisibilityPlot(QWidget):
    """Compact, local-time altitude preview for the current sequence target."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(design.SPACING_SM)
        axis = pg.DateAxisItem(orientation="bottom")
        self._plot = pg.PlotWidget(axisItems={"bottom": axis})
        self._plot.setBackground(theme.BG2)
        self._plot.setMinimumHeight(145)
        self._plot.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._plot.showGrid(x=False, y=True, alpha=0.2)
        self._plot.getAxis("left").setLabel("Altitude (°)")
        self._plot.getAxis("bottom").setLabel("Local time")
        self._plot.getAxis("left").setTextPen(pg.mkPen(theme.FG_MUTED))
        self._plot.getAxis("bottom").setTextPen(pg.mkPen(theme.FG_MUTED))
        self._curve = self._plot.plot(pen=pg.mkPen(theme.ACCENT, width=2))
        transit_brush = QColor(theme.WARNING).darker(120)
        transit_brush.setAlpha(55)
        self._transit_region = pg.LinearRegionItem(
            values=(0, 0),
            movable=False,
            brush=pg.mkBrush(transit_brush),
            pen=pg.mkPen(theme.WARNING, width=1),
        )
        self._transit_region.setZValue(-5)
        self._transit_region.hide()
        self._plot.addItem(self._transit_region)
        self._horizon = pg.InfiniteLine(
            pos=30,
            angle=0,
            pen=pg.mkPen(theme.FG_MUTED, width=1, style=Qt.PenStyle.DashLine),
        )
        self._plot.addItem(self._horizon)
        self._plot.setYRange(-15, 90, padding=0)
        layout.addWidget(self._plot)
        self._summary = design.MutedLabel("")
        self._summary.setWordWrap(True)
        self._summary.hide()
        layout.addWidget(self._summary)

    def set_target(
        self,
        name: str,
        ra_hours: float,
        dec_degrees: float,
        lat: float,
        lon: float,
        *,
        night_date=None,
    ) -> None:
        samples = upcoming_night_altitudes(ra_hours, dec_degrees, lat, lon, now=night_date)
        if not samples:
            self.clear()
            return
        x = [when.timestamp() for when, _altitude in samples]
        y = [altitude for _when, altitude in samples]
        self._curve.setData(x, y)
        self._plot.setXRange(x[0], x[-1], padding=0.02)
        peak = max(range(len(y)), key=y.__getitem__)
        peak_when, peak_alt = samples[peak]
        self._summary.setText(f"Peak {peak_alt:.1f}° · {peak_when.strftime('%H:%M')} local")
        self._summary.show()

    def clear(self) -> None:
        self._curve.setData([], [])
        self._transit_region.hide()
        self._summary.clear()
        self._summary.hide()

    def set_transit_coverage(self, start, end) -> None:
        """Shade an exoplanet's full requested coverage on the altitude plot."""
        self._transit_region.setRegion((start.timestamp(), end.timestamp()))
        self._transit_region.show()


class SequencePanel(QWidget):
    """Editable, observer-arrangeable sequence planning workspace."""

    start_requested = pyqtSignal(object)  # SequencePlan
    object_name_changed = pyqtSignal(str)
    pause_requested = pyqtSignal()
    resume_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    object_lookup_requested = pyqtSignal(str)
    exoplanet_lookup_requested = pyqtSignal(str)
    transit_plan_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._filters = list(_DEFAULT_FILTERS)
        self._gain_range = _DEFAULT_GAIN_RANGE
        self._exposure_range = _DEFAULT_EXPOSURE_RANGE
        self._running = False
        self._paused = False
        self._waiting = False
        # Visibility target (name, ra_h, dec_d, lat, lon) — the GoTo / altitude
        # start reference; None until a target is resolved.
        self._start_target: tuple[str, float, float, float, float] | None = None
        self._active_row = -1
        self._device_states: dict[str, str] = {}
        self._build_ui()
        self._add_row()  # start with one editable row
        self._refresh_estimate()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        body = QVBoxLayout(self)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        # The Sequencer is a real dockable workspace rather than a fixed
        # right-hand rail.  The table remains the stable centre; every planning
        # aid can be moved, tabbed, resized or floated to another monitor.
        self._workspace = QMainWindow()
        self._workspace.setWindowFlags(Qt.WindowType.Widget)
        self._workspace.setDockNestingEnabled(True)
        self._workspace.setDockOptions(
            QMainWindow.DockOption.AnimatedDocks
            | QMainWindow.DockOption.AllowTabbedDocks
            | QMainWindow.DockOption.AllowNestedDocks
            | QMainWindow.DockOption.GroupedDragging
        )

        # ── Stable centre: sequence table + row editing ────────────────
        steps = QWidget()
        left = QVBoxLayout(steps)
        left.setContentsMargins(
            design.SPACING_MD, design.SPACING_MD, design.SPACING_MD, design.SPACING_MD
        )
        left.setSpacing(design.SPACING_SM)

        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(list(_COLUMNS))
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for col in (1, 2):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        for col in range(3, len(_COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setMinimumHeight(120)
        self._table.setMaximumHeight(120)
        self._table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._table.setHorizontalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)
        left.addWidget(self._table)

        edit_row = QHBoxLayout()
        edit_row.setSpacing(design.SPACING_SM)
        for label, slot, tip in (
            ("＋ Add", self._add_row, "Add a step"),
            ("⧉ Dup", self._duplicate_row, "Duplicate the selected step"),
            ("－ Remove", self._remove_row, "Remove the selected step"),
            ("↑", self._move_up, "Move step up"),
            ("↓", self._move_down, "Move step down"),
        ):
            btn = design.SecondaryButton(label)
            btn.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            if label in {"↑", "↓"}:
                btn.setMinimumWidth(42)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            edit_row.addWidget(btn)
        edit_row.addStretch()
        self._estimate_lbl = design.MutedLabel("")
        self._estimate_lbl.setWordWrap(True)
        self._estimate_lbl.setToolTip(
            "Exposures + intervals + per-frame overhead; autofocus passes not counted"
        )
        edit_row.addWidget(self._estimate_lbl)
        left.addStretch(1)
        left.addLayout(edit_row)
        self._workspace.setCentralWidget(steps)

        # ── Individually dockable planning panels ──────────────────────
        search_panel = QWidget()
        search_l = QVBoxLayout(search_panel)
        search_l.setContentsMargins(0, 0, 0, 0)
        search_l.setSpacing(design.SPACING_SM)
        search_row = QHBoxLayout()
        search_row.setSpacing(design.SPACING_SM)
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("T CrB, RR Lyr, V404 Cyg…")
        self._search_edit.setToolTip(
            "Resolve the variable star's designation to its observing coordinates"
        )
        self._object_suggestion_model = QStringListModel(self)
        self._object_completer = QCompleter(self._object_suggestion_model, self)
        self._object_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._object_completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._search_edit.setCompleter(self._object_completer)
        self._search_edit.textEdited.connect(self._refresh_object_suggestions)
        self._search_edit.returnPressed.connect(self._request_object_lookup)
        search_row.addWidget(self._search_edit, 1)
        self._search_btn = design.SecondaryButton("Find")
        self._search_btn.clicked.connect(self._request_object_lookup)
        search_row.addWidget(self._search_btn)
        search_l.addLayout(search_row)
        self._search_result = design.MutedLabel("")
        self._search_result.setWordWrap(True)
        self._search_result.hide()
        search_l.addWidget(self._search_result)

        plan_panel = QWidget()
        plan_l = QVBoxLayout(plan_panel)
        plan_l.setContentsMargins(0, 0, 0, 0)
        opts = QGridLayout()
        opts.setHorizontalSpacing(design.SPACING_MD)
        opts.setVerticalSpacing(design.SPACING_SM)
        opts.setColumnStretch(1, 1)
        opts.addWidget(design.MutedLabel("Target name (FITS)"), 0, 0)
        self._object_edit = QLineEdit()
        self._object_edit.setPlaceholderText("M42, T CrB…")
        self._object_edit.setToolTip(
            "Target identifier written to FITS OBJECT and used for this run's target set. "
            "It is not the observation-session name."
        )
        self._object_edit.editingFinished.connect(self._on_object_edited)
        opts.addWidget(self._object_edit, 0, 1)
        opts.addWidget(design.MutedLabel("Repeat plan"), 1, 0)
        self._repeat_spin = QSpinBox()
        self._repeat_spin.setRange(1, 999)
        self._repeat_spin.valueChanged.connect(self._refresh_estimate)
        opts.addWidget(self._repeat_spin, 1, 1)
        opts.addWidget(design.MutedLabel("Autofocus every"), 2, 0)
        self._af_spin = QSpinBox()
        self._af_spin.setRange(0, 999)
        self._af_spin.setSuffix(" frames (0=off)")
        opts.addWidget(self._af_spin, 2, 1)
        self._af_filter_chk = QCheckBox("Autofocus on filter change")
        opts.addWidget(self._af_filter_chk, 3, 0, 1, 2)
        opts.addWidget(design.MutedLabel("End of run"), 4, 0)
        self._on_complete_combo = QComboBox()
        self._on_complete_combo.addItems(ON_COMPLETE_CHOICES)
        self._on_complete_combo.setToolTip(
            "Mount action after FULL completion only — never after a stop or error"
        )
        opts.addWidget(self._on_complete_combo, 4, 1)
        plan_l.addLayout(opts)

        visibility_panel = QWidget()
        visibility_l = QVBoxLayout(visibility_panel)
        visibility_l.setContentsMargins(0, 0, 0, 0)
        self._visibility_plot = TargetVisibilityPlot()
        visibility_l.addWidget(self._visibility_plot)

        # A transit is planned differently from a generic catalogue target:
        # the planet name resolves to its host star, and the observer needs an
        # uninterrupted light-only cadence plus out-of-transit baselines.
        transit_panel = QWidget()
        transit_l = QVBoxLayout(transit_panel)
        transit_l.setContentsMargins(0, 0, 0, 0)
        transit_l.setSpacing(design.SPACING_SM)
        transit_search = QHBoxLayout()
        transit_search.setSpacing(design.SPACING_SM)
        self._transit_edit = QLineEdit()
        self._transit_edit.setPlaceholderText("HD 189733 b, WASP-12 b…")
        self._transit_edit.setToolTip(
            "Search the NASA Exoplanet Archive for a published transit ephemeris"
        )
        self._exoplanet_suggestion_model = QStringListModel(self)
        self._exoplanet_completer = QCompleter(self._exoplanet_suggestion_model, self)
        self._exoplanet_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._exoplanet_completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._transit_edit.setCompleter(self._exoplanet_completer)
        self._transit_edit.textEdited.connect(self._refresh_exoplanet_suggestions)
        self._transit_edit.returnPressed.connect(self._request_exoplanet_lookup)
        transit_search.addWidget(self._transit_edit, 1)
        self._transit_search_btn = design.SecondaryButton("Find planet")
        self._transit_search_btn.clicked.connect(self._request_exoplanet_lookup)
        transit_search.addWidget(self._transit_search_btn)
        transit_l.addLayout(transit_search)
        self._transit_result = design.MutedLabel("")
        self._transit_result.setWordWrap(True)
        self._transit_result.hide()
        transit_l.addWidget(self._transit_result)
        transit_opts = QGridLayout()
        transit_opts.setHorizontalSpacing(design.SPACING_MD)
        transit_opts.setVerticalSpacing(design.SPACING_SM)
        transit_opts.setColumnStretch(1, 1)
        transit_opts.addWidget(design.MutedLabel("Baseline"), 0, 0)
        self._transit_baseline_spin = QSpinBox()
        self._transit_baseline_spin.setRange(0, 240)
        self._transit_baseline_spin.setValue(60)
        self._transit_baseline_spin.setSuffix(" min each side")
        self._transit_baseline_spin.setToolTip(
            "Out-of-transit coverage before ingress and after egress"
        )
        transit_opts.addWidget(self._transit_baseline_spin, 0, 1)
        transit_opts.addWidget(design.MutedLabel("Exposure"), 1, 0)
        self._transit_exposure_spin = QDoubleSpinBox()
        self._transit_exposure_spin.setRange(*self._exposure_range)
        self._transit_exposure_spin.setDecimals(2)
        self._transit_exposure_spin.setValue(10.0)
        self._transit_exposure_spin.setSuffix(" s")
        transit_opts.addWidget(self._transit_exposure_spin, 1, 1)
        transit_opts.addWidget(design.MutedLabel("Cadence"), 2, 0)
        self._transit_cadence_spin = QDoubleSpinBox()
        self._transit_cadence_spin.setRange(0.01, 3600.0)
        self._transit_cadence_spin.setDecimals(1)
        self._transit_cadence_spin.setValue(13.0)
        self._transit_cadence_spin.setSuffix(" s")
        self._transit_cadence_spin.setToolTip(
            "Start-to-start cadence; includes the exposure and any idle interval"
        )
        transit_opts.addWidget(self._transit_cadence_spin, 2, 1)
        transit_opts.addWidget(design.MutedLabel("Gain"), 3, 0)
        self._transit_gain_spin = QSpinBox()
        self._transit_gain_spin.setRange(*self._gain_range)
        self._transit_gain_spin.setValue(80)
        transit_opts.addWidget(self._transit_gain_spin, 3, 1)
        transit_opts.addWidget(design.MutedLabel("Filter"), 4, 0)
        self._transit_filter_combo = QComboBox()
        self._transit_filter_combo.addItems(self._filters)
        self._transit_filter_combo.setToolTip(
            "Keep one filter for the entire transit; do not change filters mid-series"
        )
        transit_opts.addWidget(self._transit_filter_combo, 4, 1)
        transit_l.addLayout(transit_opts)
        self._prepare_transit_btn = design.SuccessButton("Prepare transit sequence")
        self._prepare_transit_btn.setEnabled(False)
        self._prepare_transit_btn.setToolTip(
            "Replace the table with one uninterrupted light-only acquisition step"
        )
        self._prepare_transit_btn.clicked.connect(self.transit_plan_requested)
        transit_l.addLayout(design.button_row(self._prepare_transit_btn))

        # One source card governs both programmes.  Once selected, both use
        # the same target set, comparison ensemble, visibility and sequence
        # table; only exoplanets need an additional ephemeris/cadence step.
        source_panel = QWidget()
        source_l = QVBoxLayout(source_panel)
        source_l.setContentsMargins(0, 0, 0, 0)
        source_l.setSpacing(design.SPACING_SM)
        self._source_kind = QComboBox()
        self._source_kind.addItem("Variable star", "variable")
        self._source_kind.addItem("Exoplanet transit", "exoplanet")
        self._source_kind.setToolTip(
            "Both programmes use the same target and comparison-star workflow"
        )
        source_l.addWidget(self._source_kind)
        self._source_stack = _CurrentPageStack()
        self._source_stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._source_stack.addWidget(search_panel)
        self._source_stack.addWidget(transit_panel)
        self._source_kind.currentIndexChanged.connect(self._source_stack.setCurrentIndex)
        self._source_kind.currentIndexChanged.connect(
            lambda _index: self._source_stack.updateGeometry()
        )
        source_l.addWidget(self._source_stack)

        presets_panel = QWidget()
        presets_l = QVBoxLayout(presets_panel)
        presets_l.setContentsMargins(0, 0, 0, 0)
        preset_row = QHBoxLayout()
        preset_row.setSpacing(design.SPACING_SM)
        save_btn = design.SecondaryButton("Save preset…")
        save_btn.clicked.connect(self._on_save_preset)
        load_btn = design.SecondaryButton("Load preset…")
        load_btn.clicked.connect(self._on_load_preset)
        preset_row.addWidget(save_btn, 1)
        preset_row.addWidget(load_btn, 1)
        presets_l.addLayout(preset_row)

        run_panel = QWidget()
        run_l = QVBoxLayout(run_panel)
        run_l.setContentsMargins(0, 0, 0, 0)
        # When to start. "Now" is the historical behaviour; a time or an
        # altitude holds the frozen plan until it is due (Stop cancels).
        start_opts = QGridLayout()
        start_opts.setHorizontalSpacing(design.SPACING_MD)
        start_opts.setVerticalSpacing(design.SPACING_SM)
        start_opts.setColumnStretch(1, 1)
        start_opts.addWidget(design.MutedLabel("Start"), 0, 0)
        self._start_mode = QComboBox()
        self._start_mode.addItem("Now", START_NOW)
        self._start_mode.addItem("At a local time", START_AT_TIME)
        self._start_mode.addItem("When the target is above…", START_ABOVE_ALTITUDE)
        self._start_mode.setToolTip("Hold the sequence until a time or a target altitude")
        start_opts.addWidget(self._start_mode, 0, 1)
        self._start_time = QDateTimeEdit()
        self._start_time.setCalendarPopup(True)
        self._start_time.setDisplayFormat("yyyy-MM-dd HH:mm")
        self._start_time.setDateTime(QDateTime.currentDateTime().addSecs(3600))
        self._start_time.setToolTip("Local time of this computer")
        start_opts.addWidget(self._start_time, 1, 1)
        self._start_alt = QDoubleSpinBox()
        self._start_alt.setRange(0.0, 89.0)
        self._start_alt.setDecimals(0)
        self._start_alt.setValue(30.0)
        self._start_alt.setSuffix("° altitude")
        self._start_alt.setToolTip("Start once the Visibility target climbs above this altitude")
        start_opts.addWidget(self._start_alt, 2, 1)
        self._start_goto_chk = QCheckBox("GoTo the target at start")
        self._start_goto_chk.setToolTip(
            "Slew to the Visibility target, wait for the slew to finish, then expose. "
            "If the GoTo fails the sequence does not start."
        )
        start_opts.addWidget(self._start_goto_chk, 3, 0, 1, 2)
        run_l.addLayout(start_opts)
        self._start_mode.currentIndexChanged.connect(self._sync_start_options)
        self._sync_start_options()

        self._start_btn = design.SuccessButton("▶  Start sequence")
        self._start_btn.clicked.connect(self._on_start)
        self._pause_btn = design.SecondaryButton("⏸  Pause")
        self._pause_btn.setToolTip("Hold after the current frame; Resume releases it")
        self._pause_btn.clicked.connect(self._on_pause_clicked)
        self._pause_btn.setEnabled(False)
        self._stop_btn = design.DangerButton("■  Stop")
        self._stop_btn.clicked.connect(self.stop_requested)
        self._stop_btn.setEnabled(False)
        run_l.addLayout(design.button_row(self._start_btn))
        run_l.addLayout(design.button_row(self._pause_btn, self._stop_btn))

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        run_l.addWidget(self._progress)
        self._status_lbl = design.MutedLabel("")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.hide()
        run_l.addWidget(self._status_lbl)
        self._readiness_lbl = design.MutedLabel("Camera required")
        self._readiness_lbl.setToolTip("Sequence readiness")
        run_l.addWidget(self._readiness_lbl)

        self._docks = {
            "source": make_dock("Scientific source", source_panel, object_name="sequencer.source"),
            "plan": make_dock("Acquisition options", plan_panel, object_name="sequencer.plan"),
            "visibility": make_dock(
                "Target visibility",
                visibility_panel,
                object_name="sequencer.visibility",
                scroll=False,
            ),
            "presets": make_dock("Presets", presets_panel, object_name="sequencer.presets"),
            "run": make_dock("Sequence control", run_panel, object_name="sequencer.run"),
        }
        # These are compact forms, but they still need enough height to avoid
        # showing a scrollbar in the default layout.  The observer can give
        # them more room; the visibility plot remains the elastic panel.
        self._docks["source"].setMinimumHeight(118)
        self._docks["plan"].setMinimumHeight(215)
        self._apply_default_layout()
        self._restore_layout()
        body.addWidget(self._build_panel_bar())
        body.addWidget(self._workspace, 1)
        self._refresh_readiness()

    def _build_panel_bar(self) -> QToolBar:
        """Reveal/recover planning panels after the observer rearranges them."""
        bar = QToolBar()
        bar.setMovable(False)
        bar.setStyleSheet(panel_toolbar_qss())
        for key, label in (
            ("source", "Scientific source"),
            ("plan", "Acquisition options"),
            ("visibility", "Visibility"),
            ("presets", "Presets"),
            ("run", "Sequence control"),
        ):
            action = style_toggle_action(self._docks[key].toggleViewAction(), label)
            action.setToolTip("Drag the panel title to arrange it; double-click to detach it.")
            bar.addAction(action)
        reset = QAction("Reset panels", bar)
        reset.setToolTip("Restore the default Sequencer panel arrangement")
        reset.triggered.connect(self.reset_layout)
        bar.addAction(reset)
        return bar

    def _apply_default_layout(self) -> None:
        workspace = self._workspace
        right = Qt.DockWidgetArea.RightDockWidgetArea
        bottom = Qt.DockWidgetArea.BottomDockWidgetArea
        workspace.addDockWidget(right, self._docks["source"])
        workspace.splitDockWidget(
            self._docks["source"], self._docks["visibility"], Qt.Orientation.Vertical
        )
        workspace.splitDockWidget(
            self._docks["visibility"], self._docks["plan"], Qt.Orientation.Vertical
        )
        workspace.addDockWidget(bottom, self._docks["run"])
        workspace.addDockWidget(bottom, self._docks["presets"])
        workspace.tabifyDockWidget(self._docks["run"], self._docks["presets"])
        self._docks["run"].raise_()
        # Forms keep a compact share of the rail; the visibility graph receives
        # the useful elastic space.  Defer until Qt has laid out the workspace.
        QTimer.singleShot(0, self._balance_default_docks)

    def _balance_default_docks(self) -> None:
        self._workspace.resizeDocks(
            [self._docks["source"], self._docks["visibility"], self._docks["plan"]],
            [115, 330, 250],
            Qt.Orientation.Vertical,
        )
        self._workspace.resizeDocks(
            [self._docks["run"]],
            [108],
            Qt.Orientation.Vertical,
        )

    def _restore_layout(self) -> None:
        """Restore the observer's panel arrangement if it was saved previously."""
        blob = self._config.get(_CFG_LAYOUT) if hasattr(self, "_config") else None
        if not blob:
            return
        try:
            self._workspace.restoreState(QByteArray(base64.b64decode(blob)))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Sequencer layout restore failed: %s", exc)

    def set_config(self, config) -> None:
        """Attach persistence after construction without coupling this UI to Config."""
        self._config = config
        self._restore_layout()

    def save_layout(self) -> None:
        if hasattr(self, "_config"):
            state = bytes(self._workspace.saveState())
            self._config.set(_CFG_LAYOUT, base64.b64encode(state).decode())

    def reset_layout(self) -> None:
        if hasattr(self, "_config"):
            self._config.set(_CFG_LAYOUT, None)
        self._apply_default_layout()

    def _request_object_lookup(self) -> None:
        self.object_lookup_requested.emit(self._search_edit.text().strip())

    def _refresh_object_suggestions(self, query: str) -> None:
        self._object_suggestion_model.setStringList(cached_object_suggestions(query))

    def source_kind(self) -> str:
        """The observing programme selected in the unified source card."""
        return str(self._source_kind.currentData() or "variable")

    def _request_exoplanet_lookup(self) -> None:
        self.exoplanet_lookup_requested.emit(self._transit_edit.text().strip())

    def _refresh_exoplanet_suggestions(self, query: str) -> None:
        self._exoplanet_suggestion_model.setStringList(cached_exoplanet_suggestions(query))

    def set_lookup_busy(self, busy: bool) -> None:
        self._search_edit.setEnabled(not busy)
        self._search_btn.setEnabled(not busy)
        if busy:
            self._search_result.setText("Searching catalogue…")
            self._search_result.show()

    def set_lookup_result(self, result) -> None:
        object_type = object_type_label(result.object_type)
        type_suffix = f" · {object_type}" if object_type else ""
        self._search_result.setText(
            f"{result.name}{type_suffix}\nRA {result.ra_hours:.5f} h · Dec {result.dec_degrees:+.5f}°"
        )
        self._search_result.show()

    def set_lookup_error(self, message: str) -> None:
        self._search_result.setText(message)
        self._search_result.show()

    def set_exoplanet_lookup_busy(self, busy: bool) -> None:
        self._transit_edit.setEnabled(not busy)
        self._transit_search_btn.setEnabled(not busy)
        if busy:
            self._transit_result.setText("Searching NASA Exoplanet Archive…")
            self._transit_result.show()

    def set_exoplanet_result(self, target, window=None, local_mid=None) -> None:
        """Show a published ephemeris and its current coverage window."""
        duration = (
            f"{target.duration_hours:.2f} h"
            if target.duration_hours is not None
            else "not published"
        )
        depth = (
            f"{target.depth_percent:.3g}%" if target.depth_percent is not None else "not published"
        )
        text = (
            f"{target.planet_name} — host: {target.host_name}\n"
            f"P {target.period_days:.8f} d · duration {duration} · depth {depth}\n"
            f"Epoch: {target.epoch_bjd_tdb:.6f} ({target.epoch_system})"
        )
        if window is not None:
            text += (
                f"\nNext mid-transit: BJD_TDB {window.mid_bjd_tdb:.6f} "
                f"(E={window.epoch_number}); coverage {window.coverage_hours:.2f} h."
            )
            if local_mid is not None:
                text += f"\nApprox. local midpoint: {local_mid.strftime('%Y-%m-%d %H:%M %Z')}."
        else:
            text += "\nObserving site required for coverage."
        self._transit_result.setText(text)
        self._transit_result.show()
        self._prepare_transit_btn.setEnabled(window is not None)

    def set_exoplanet_error(self, message: str) -> None:
        self._transit_result.setText(message)
        self._transit_result.show()
        self._prepare_transit_btn.setEnabled(False)

    def transit_settings(self) -> dict[str, float | int | str]:
        """Return the observer-selected, stable cadence settings."""
        return {
            "baseline_minutes": float(self._transit_baseline_spin.value()),
            "exposure_s": float(self._transit_exposure_spin.value()),
            "cadence_s": float(self._transit_cadence_spin.value()),
            "gain": int(self._transit_gain_spin.value()),
            "filter_name": self._transit_filter_combo.currentText(),
        }

    # ------------------------------------------------------------------
    # Row management
    # ------------------------------------------------------------------

    def _add_row(self, step: SequenceStep | None = None) -> None:
        step = step or SequenceStep()
        r = self._table.rowCount()
        self._table.insertRow(r)

        chk = QCheckBox()
        chk.setChecked(step.enabled)
        chk.toggled.connect(self._refresh_estimate)
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(chk)
        self._table.setCellWidget(r, 0, wrap)

        type_combo = QComboBox()
        type_combo.addItems(_FRAME_TYPES)
        type_combo.setCurrentText(step.frame_type)
        self._table.setCellWidget(r, 1, type_combo)

        filter_combo = QComboBox()
        filter_combo.addItems(self._filters)
        idx = filter_combo.findText(step.filter_name)
        if idx >= 0:
            filter_combo.setCurrentIndex(idx)
        self._table.setCellWidget(r, 2, filter_combo)

        exp = QDoubleSpinBox()
        exp.setRange(*self._exposure_range)
        exp.setDecimals(2)
        exp.setValue(step.exposure_s)
        exp.setProperty("cadence_s", step.cadence_s)
        exp.valueChanged.connect(self._refresh_estimate)
        self._table.setCellWidget(r, 3, exp)

        gain = QSpinBox()
        gain.setRange(*self._gain_range)
        gain.setValue(step.gain)
        self._table.setCellWidget(r, 4, gain)

        count = QSpinBox()
        count.setRange(1, 9999)
        count.setValue(step.count)
        count.valueChanged.connect(self._refresh_estimate)
        self._table.setCellWidget(r, 5, count)

        interval = QDoubleSpinBox()
        interval.setRange(0.0, 3600.0)
        interval.setDecimals(1)
        interval.setValue(step.interval_s)
        interval.setToolTip("Idle delay after each frame of this step")
        interval.valueChanged.connect(self._refresh_estimate)
        self._table.setCellWidget(r, 6, interval)

        dither = QSpinBox()
        dither.setRange(0, 999)
        dither.setValue(step.dither_every)
        dither.setSpecialValueText("off")
        dither.setToolTip("Nudge the mount a few arcminutes every N frames (0 = off)")
        self._table.setCellWidget(r, 7, dither)

        self._table.selectRow(r)
        self._sync_table_height()
        self._refresh_estimate()

    def _selected_row(self) -> int:
        rows = self._table.selectionModel().selectedRows()
        if rows:
            return rows[0].row()
        return self._table.rowCount() - 1

    def _duplicate_row(self) -> None:
        r = self._selected_row()
        if r < 0:
            return
        self._add_row(self._read_step(r))

    def _remove_row(self) -> None:
        r = self._selected_row()
        if r >= 0 and self._table.rowCount() > 1:
            self._table.removeRow(r)
            self._sync_table_height()
            self._refresh_estimate()

    def _sync_table_height(self) -> None:
        """Fit short plans without turning the empty table into the main UI."""
        content_height = self._table.horizontalHeader().height()
        content_height += sum(self._table.rowHeight(row) for row in range(self._table.rowCount()))
        content_height += 2 * self._table.frameWidth() + design.SPACING_SM
        height = max(120, min(420, content_height))
        self._table.setMinimumHeight(height)
        self._table.setMaximumHeight(height)

    def _move_up(self) -> None:
        self._swap_rows(self._selected_row(), self._selected_row() - 1)

    def _move_down(self) -> None:
        self._swap_rows(self._selected_row(), self._selected_row() + 1)

    def _swap_rows(self, a: int, b: int) -> None:
        if a < 0 or b < 0 or a >= self._table.rowCount() or b >= self._table.rowCount():
            return
        step_a, step_b = self._read_step(a), self._read_step(b)
        self._write_step(a, step_b)
        self._write_step(b, step_a)
        self._table.selectRow(b)

    # ------------------------------------------------------------------
    # Read / write a row <-> SequenceStep
    # ------------------------------------------------------------------

    def _read_step(self, r: int) -> SequenceStep:
        chk = self._table.cellWidget(r, 0).findChild(QCheckBox)
        return SequenceStep(
            enabled=chk.isChecked() if chk else True,
            frame_type=self._table.cellWidget(r, 1).currentText(),
            filter_name=self._table.cellWidget(r, 2).currentText(),
            exposure_s=float(self._table.cellWidget(r, 3).value()),
            gain=int(self._table.cellWidget(r, 4).value()),
            count=int(self._table.cellWidget(r, 5).value()),
            interval_s=float(self._table.cellWidget(r, 6).value()),
            cadence_s=self._table.cellWidget(r, 3).property("cadence_s"),
            dither_every=int(self._table.cellWidget(r, 7).value()),
        )

    def _write_step(self, r: int, step: SequenceStep) -> None:
        chk = self._table.cellWidget(r, 0).findChild(QCheckBox)
        if chk:
            chk.setChecked(step.enabled)
        self._table.cellWidget(r, 1).setCurrentText(step.frame_type)
        self._table.cellWidget(r, 2).setCurrentText(step.filter_name)
        self._table.cellWidget(r, 3).setValue(step.exposure_s)
        self._table.cellWidget(r, 3).setProperty("cadence_s", step.cadence_s)
        self._table.cellWidget(r, 4).setValue(step.gain)
        self._table.cellWidget(r, 5).setValue(step.count)
        self._table.cellWidget(r, 6).setValue(step.interval_s)
        self._table.cellWidget(r, 7).setValue(step.dither_every)

    def to_plan(self) -> SequencePlan:
        steps = [self._read_step(r) for r in range(self._table.rowCount())]
        return SequencePlan(
            steps=steps,
            object_name=self._object_edit.text().strip(),
            repeat=int(self._repeat_spin.value()),
            autofocus_every_n=int(self._af_spin.value()),
            autofocus_on_filter_change=self._af_filter_chk.isChecked(),
            on_complete=self._on_complete_combo.currentText(),
        )

    def load_plan(self, plan: SequencePlan) -> None:
        """Replace the whole editor state with ``plan`` (preset load)."""
        self._table.setRowCount(0)
        for step in plan.steps or [SequenceStep()]:
            self._add_row(step)
        self.set_object_name(plan.object_name, emit=True)
        self._repeat_spin.setValue(max(1, plan.repeat))
        self._af_spin.setValue(max(0, plan.autofocus_every_n))
        self._af_filter_chk.setChecked(plan.autofocus_on_filter_change)
        idx = self._on_complete_combo.findText(plan.on_complete)
        self._on_complete_combo.setCurrentIndex(max(0, idx))
        self._refresh_estimate()

    def set_object_name(self, name: str, *, emit: bool = False) -> None:
        """Set the shared observation object without feeding a signal loop."""
        value = (name or "").strip()
        if self._object_edit.text() == value:
            return
        self._object_edit.setText(value)
        self._refresh_readiness()
        if emit:
            self._on_object_edited()

    def set_target_coordinates(
        self, name: str, ra_hours: float, dec_degrees: float, site_lat: float, site_lon: float
    ) -> None:
        """Update the visibility card from a Telescope catalogue resolution."""
        self.set_object_name(name)
        self._set_start_target(name, ra_hours, dec_degrees, site_lat, site_lon)
        self._visibility_plot.set_target(name, ra_hours, dec_degrees, site_lat, site_lon)

    def set_transit_visibility(
        self,
        name: str,
        ra_hours: float,
        dec_degrees: float,
        site_lat: float,
        site_lon: float,
        coverage_start,
        coverage_end,
    ) -> None:
        """Show the altitude curve for the transit night and shade its coverage."""
        midpoint = coverage_start + (coverage_end - coverage_start) / 2
        self.set_object_name(name)
        self._set_start_target(name, ra_hours, dec_degrees, site_lat, site_lon)
        self._visibility_plot.set_target(
            name,
            ra_hours,
            dec_degrees,
            site_lat,
            site_lon,
            night_date=midpoint,
        )
        self._visibility_plot.set_transit_coverage(coverage_start, coverage_end)

    def _on_object_edited(self) -> None:
        self._refresh_readiness()
        self.object_name_changed.emit(self._object_edit.text().strip())

    # ------------------------------------------------------------------
    # Presets (JSON on disk)
    # ------------------------------------------------------------------

    def _presets_dir(self) -> str:
        configured = (
            self._config.get("data_paths.sequence_presets_directory", "")
            if hasattr(self, "_config")
            else ""
        )
        d = Path(configured).expanduser() if configured else Path.home() / "Argos" / "sequences"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def _on_save_preset(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save sequence preset", self._presets_dir(), "Sequence preset (*.json)"
        )
        if not path:
            return
        if not path.endswith(".json"):
            path += ".json"
        try:
            Path(path).write_text(json.dumps(plan_to_dict(self.to_plan()), indent=2))
            self.set_status(f"Preset saved: {Path(path).name}")
        except OSError as exc:
            self.set_status(f"Preset save failed: {exc}")

    def _on_load_preset(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load sequence preset", self._presets_dir(), "Sequence preset (*.json)"
        )
        if not path:
            return
        try:
            plan = plan_from_dict(json.loads(Path(path).read_text()))
        except (OSError, ValueError, TypeError) as exc:
            self.set_status(f"Preset load failed: {exc}")
            return
        self.load_plan(plan)
        self.set_status(f"Preset loaded: {Path(path).name}")

    # ------------------------------------------------------------------
    # Public API (called by ImagingPage)
    # ------------------------------------------------------------------

    def set_camera_limits(
        self,
        gain_min: int | None = None,
        gain_max: int | None = None,
        exposure_min: float | None = None,
        exposure_max: float | None = None,
    ) -> None:
        """Retarget the per-step exposure/gain spinboxes to the connected
        camera's driver-reported limits; ``None``s restore the defaults."""
        if gain_min is not None and gain_max is not None and gain_max > gain_min:
            self._gain_range = (int(gain_min), int(gain_max))
        else:
            self._gain_range = _DEFAULT_GAIN_RANGE
        if exposure_min is not None and exposure_max is not None:
            lo = max(float(exposure_min), _DEFAULT_EXPOSURE_RANGE[0])
            if exposure_max > lo:
                self._exposure_range = (lo, float(exposure_max))
            else:
                self._exposure_range = _DEFAULT_EXPOSURE_RANGE
        else:
            self._exposure_range = _DEFAULT_EXPOSURE_RANGE
        for r in range(self._table.rowCount()):
            self._table.cellWidget(r, 3).setRange(*self._exposure_range)
            self._table.cellWidget(r, 4).setRange(*self._gain_range)
        self._transit_exposure_spin.setRange(*self._exposure_range)
        self._transit_gain_spin.setRange(*self._gain_range)

    def set_filter_options(self, names: list[str]) -> None:
        self._filters = list(names or _DEFAULT_FILTERS)
        for r in range(self._table.rowCount()):
            combo = self._table.cellWidget(r, 2)
            current = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(self._filters)
            idx = combo.findText(current)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            combo.blockSignals(False)
        current = self._transit_filter_combo.currentText()
        self._transit_filter_combo.blockSignals(True)
        self._transit_filter_combo.clear()
        self._transit_filter_combo.addItems(self._filters)
        idx = self._transit_filter_combo.findText(current)
        if idx >= 0:
            self._transit_filter_combo.setCurrentIndex(idx)
        self._transit_filter_combo.blockSignals(False)

    # ------------------------------------------------------------------
    # Scheduled start
    # ------------------------------------------------------------------

    def _set_start_target(self, name, ra_hours, dec_degrees, site_lat, site_lon) -> None:
        self._start_target = (
            str(name),
            float(ra_hours),
            float(dec_degrees),
            float(site_lat),
            float(site_lon),
        )
        self._sync_start_options()

    def _sync_start_options(self, *_args) -> None:
        mode = self._start_mode.currentData()
        self._start_time.setVisible(mode == START_AT_TIME)
        self._start_alt.setVisible(mode == START_ABOVE_ALTITUDE)
        has_target = self._start_target is not None
        self._start_goto_chk.setEnabled(has_target and not self._waiting)
        if not has_target:
            self._start_goto_chk.setChecked(False)
            self._start_goto_chk.setToolTip("Find the target first (its coordinates are used)")
        else:
            self._start_goto_chk.setToolTip(
                f"Slew to {self._start_target[0]}, wait for the slew to finish, then "
                "expose. If the GoTo fails the sequence does not start."
            )

    def start_condition(self) -> StartCondition:
        """The observer's start options (mode, time/altitude, GoTo)."""
        target = None
        if self._start_target is not None:
            # The page substitutes the explicitly saved site (never 0/0).
            name, ra, dec, lat, lon = self._start_target
            target = StartTarget(name, ra, dec, lat, lon)
        start_at = self._start_time.dateTime().toPyDateTime().astimezone()
        return StartCondition(
            mode=self._start_mode.currentData(),
            start_at=start_at,
            min_altitude_deg=float(self._start_alt.value()),
            goto=self._start_goto_chk.isChecked(),
            target=target,
        )

    def set_scheduled_start_time(self, when) -> None:
        """Prefill the start time (e.g. a transit's coverage start); mode unchanged."""
        local = when.astimezone()
        self._start_time.setDateTime(QDateTime.fromSecsSinceEpoch(int(local.timestamp())))

    def set_waiting(self, waiting: bool) -> None:
        """A scheduled start holds the plan: lock editing, Stop cancels."""
        self._waiting = waiting
        self._start_btn.setEnabled(not waiting and not self._running)
        self._stop_btn.setEnabled(waiting or self._running)
        self._table.setEnabled(not waiting and not self._running)
        for widget in (self._start_mode, self._start_time, self._start_alt):
            widget.setEnabled(not waiting)
        self._sync_start_options()
        if not waiting and not self._running:
            self.set_status("")

    def set_running(self, running: bool) -> None:
        self._running = running
        self._start_btn.setEnabled(not running and not self._waiting)
        self._pause_btn.setEnabled(running)
        self._stop_btn.setEnabled(running or self._waiting)
        self._table.setEnabled(not running and not self._waiting)
        self._progress.setVisible(running)
        if not running:
            self.set_status("")
            self.set_paused(False)
            self.set_active_step(-1)

    def set_device_state(self, device: str, state: str) -> None:
        """Update the observer-facing pre-flight summary.

        The start action remains available for offline plan editing and for
        calibration-only runs; the engine stays the authority that accepts or
        refuses a run.  This is deliberately guidance, not a second validator.
        """
        self._device_states[device] = state
        self._refresh_readiness()

    def set_paused(self, paused: bool) -> None:
        """Reflect the worker's pause state on the button."""
        self._paused = paused
        self._pause_btn.setText("▶  Resume" if paused else "⏸  Pause")
        if paused:
            self.set_status("Paused")

    def set_active_step(self, index: int) -> None:
        """Highlight the running step's row (-1 clears)."""
        self._active_row = index
        for r in range(self._table.rowCount()):
            for col in range(self._table.columnCount()):
                w = self._table.cellWidget(r, col)
                if w is not None:
                    w.setStyleSheet(f"background:{theme.SURFACE_3};" if r == index else "")

    def set_progress(self, done: int, total: int, eta_seconds: float) -> None:
        self._progress.setRange(0, max(1, total))
        self._progress.setValue(done)
        self._progress.setFormat(f"%v / {total}")
        self._status_lbl.setText(f"Frame {done}/{total} — ETA {_format_duration(eta_seconds)}")

    def set_status(self, text: str) -> None:
        self._status_lbl.setText(text)
        self._status_lbl.setVisible(bool(text))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _refresh_estimate(self, *_args) -> None:
        try:
            est = estimated_duration_s(self.to_plan())
        except (AttributeError, RuntimeError):
            return  # a row is mid-construction
        self._estimate_lbl.setText(f"≈ {_format_duration(est)} total" if est > 0 else "")

    def _refresh_readiness(self) -> None:
        """Explain the next useful action without turning the Run card into a log."""
        camera_state = self._device_states.get("camera")
        object_name = self._object_edit.text().strip()
        if camera_state != "connected":
            message = "Camera required"
            color = theme.WARNING
        elif not object_name:
            message = "Object name required"
            color = theme.WARNING
        else:
            mount_state = self._device_states.get("mount")
            message = "Ready" if mount_state == "connected" else "Ready · telescope offline"
            color = theme.SUCCESS if mount_state == "connected" else theme.WARNING
        self._readiness_lbl.setText(message)
        self._readiness_lbl.setStyleSheet(
            f"color:{color}; font-size:{design.FONT_SIZE_SMALL}px; background:transparent;"
        )

    def _on_pause_clicked(self) -> None:
        if self._paused:
            self.resume_requested.emit()
        else:
            self.pause_requested.emit()

    def _on_start(self) -> None:
        plan = self.to_plan()
        if not any(s.enabled and s.count > 0 for s in plan.steps):
            self.set_status("Add an enabled step")
            self._status_lbl.setStyleSheet(f"color:{theme.WARNING};")
            return
        self._status_lbl.setStyleSheet("")
        self.start_requested.emit(plan)
